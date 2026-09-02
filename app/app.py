"""نظام إدارة مبيعات — التطبيق الرئيسي."""
import json
import os
from datetime import date, timedelta
from functools import wraps

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import engine as E
from db import (audit, begin_write, close_db, commit, ex, get_setting, init_db, q, q1,
                rollback, set_setting)
from filters import Where, csv_response, get_filters, wants_export

app = Flask(__name__)
CLOUD = os.environ.get("MABEE3AT_ENV", "").lower() == "cloud"

_secret = os.environ.get("SECRET_KEY")
if not _secret:
    if CLOUD:
        raise RuntimeError("SECRET_KEY مطلوب عند التشغيل السحابي — لا مفتاح افتراضي.")
    _secret = "dev-only-not-for-cloud"
app.secret_key = _secret

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,      # لا وصول من جافاسكربت
    SESSION_COOKIE_SAMESITE="Strict",  # لا تُرسل الجلسة من موقع آخر
    SESSION_COOKIE_SECURE=CLOUD,       # HTTPS فقط في السحابة
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    MAX_CONTENT_LENGTH=4 * 1024 * 1024,
    JSON_AS_ASCII=False,
)
app.debug = False                      # لا يُفعَّل مصحّح Werkzeug أبداً في السحابة
app.teardown_appcontext(close_db)

ROLE_NAMES = {"admin": "مدير", "accountant": "محاسب", "rep": "مندوب"}
METHODS = {"cash": "كاش", "credit": "كريدت (آجل)", "cheque": "شيك", "transfer": "تحويل بنكي"}
MOVE_TYPES = {"opening": "رصيد افتتاحي", "purchase": "إضافة/شراء", "sale": "بيع",
              "return_in": "مرتجع وارد", "adjustment": "تسوية"}
KINDS = {"detailed": "تفصيلية بأصناف", "aggregate": "تاريخية إجمالية"}
ENTRY_TYPES = {"earn": "استحقاق", "reversal": "عكس", "late_reversal": "عكس تأخر التحصيل"}

INV_STATUSES = [("open", "مفتوحة"), ("partial", "محصَّلة جزئياً"), ("paid", "محصَّلة بالكامل"),
                ("late", "متأخرة"), ("void", "ملغاة")]
CHQ_STATUSES = [("pending", "معلّق"), ("cleared", "تم التحصيل"), ("bounced", "مرتد")]
CHQ_LABELS = dict(CHQ_STATUSES)
DOC_STATUSES = [("posted", "مرحَّل"), ("void", "ملغى")]
LATE_STATUSES = [("late", "متأخرة"), ("ok", "داخل المهلة"), ("reversed", "سبق عكسها")]


# ============================================================
#  المصادقة والصلاحيات
# ============================================================
def current_user():
    if "uid" not in session:
        return None
    if not hasattr(g, "_user"):
        g._user = q1("SELECT * FROM users WHERE id = ? AND is_active = 1", (session["uid"],))
    return g._user


def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if not current_user():
            return redirect(url_for("login", next=request.path))
        return f(*a, **k)
    return w


def roles(*allowed):
    def deco(f):
        @wraps(f)
        def w(*a, **k):
            u = current_user()
            if not u:
                return redirect(url_for("login", next=request.path))
            if u["role"] not in allowed:
                flash("هذه الصفحة تحتاج صلاحية أعلى.", "error")
                return redirect(url_for("dashboard"))
            return f(*a, **k)
        return w
    return deco


def my_rep_id():
    u = current_user()
    return u["rep_id"] if u and u["role"] == "rep" else None


def sources():
    """قوائم العملاء والمندوبين والأصناف المستخدمة في شريط الفلاتر."""
    rid = my_rep_id()
    custs = q(f"""SELECT id, code, name FROM customers
                  {'WHERE rep_id=%d' % rid if rid else ''} ORDER BY name""")
    reps = [] if rid else q("SELECT id, name FROM reps ORDER BY name")
    items = q("SELECT item_id, code, name FROM v_item_stock ORDER BY name")
    return custs, reps, items


def scope(w, col="rep_id"):
    """يحصر النتائج على بيانات المندوب إن كان المستخدم مندوباً."""
    rid = my_rep_id()
    if rid:
        w.add(f"{col} = ?", rid)
    return w


@app.context_processor
def inject():
    return {
        "user": current_user(), "ROLE_NAMES": ROLE_NAMES, "METHODS": METHODS,
        "MOVE_TYPES": MOVE_TYPES, "KINDS": KINDS, "ENTRY_TYPES": ENTRY_TYPES,
        "CHQ_LABELS": CHQ_LABELS, "today": date.today().isoformat(),
        "demo_mode": os.environ.get("MABEE3AT_DEMO", "").lower() in ("1", "true", "yes"),
        "fmt": lambda x: f"{float(x or 0):,.2f}",
    }


@app.before_request
def verify_origin():
    """
    حماية من تزوير الطلبات عبر المواقع: كل طلب يغيّر الحالة يجب أن يأتي
    من نفس الموقع. مع SameSite=Strict يشكّلان طبقتين مستقلتين، دون الحاجة
    إلى تعديل أي من القوالب الثلاثين.
    """
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return None
    origin = request.headers.get("Origin") or request.headers.get("Referer")
    if not origin:
        if CLOUD:
            abort(403)
        return None
    from urllib.parse import urlparse
    if urlparse(origin).netloc != request.host:
        abort(403)
    return None


@app.route("/healthz")
def healthz():
    """فحص صحة للمنصّة — لا يكشف أي معلومة عن الإعدادات أو القاعدة."""
    try:
        q1("SELECT 1 AS ok")
        return {"status": "ok"}, 200
    except Exception:
        return {"status": "degraded"}, 503


@app.errorhandler(500)
@app.errorhandler(Exception)
def handle_error(err):
    """لا تتسرّب آثار بايثون ولا متغيّرات البيئة إلى المتصفح."""
    from werkzeug.exceptions import HTTPException
    if isinstance(err, HTTPException):
        return err
    app.logger.exception("خطأ غير متوقّع")
    try:
        rollback()
    except Exception:
        pass
    return render_template("error.html"), 500


@app.before_request
def daily_jobs():
    """فحص يومي آلي: تحصيل الشيكات المستحقة + عكس عمولة المتأخرات."""
    if request.endpoint in (None, "static", "login", "logout") or "uid" not in session:
        return
    try:
        if E.run_daily_jobs(dict(current_user()) if current_user() else None):
            commit()
    except Exception as err:  # لا نُعطّل الصفحة بسبب المهمة اليومية
        rollback()
        app.logger.warning("daily job failed: %s", err)


# ============================================================
#  تهيئة أول مدير
# ============================================================
def admin_exists():
    return q1("SELECT 1 FROM users WHERE role='admin' AND is_active=1") is not None


@app.route("/setup", methods=["GET", "POST"])
def setup():
    """
    إنشاء أول حساب مدير. لا يعمل إلا إذا لم يوجد أي مدير نشط.

    الحماية من التزامن: begin_write() يأخذ قفلاً حصرياً (BEGIN IMMEDIATE في
    SQLite، pg_advisory_xact_lock في PostgreSQL) ثم يُعاد فحص وجود مدير
    *داخل* المعاملة. فلو ضغط شخصان في نفس اللحظة، ينجح واحد فقط
    ويرى الثاني أن النظام صار مهيّأً.
    """
    if admin_exists():
        flash("النظام مهيّأ بالفعل. سجّل الدخول بحسابك.", "error")
        return redirect(url_for("login"))

    if request.method == "POST":
        fm = request.form
        username = (fm.get("username") or "").strip()
        pw = fm.get("password") or ""
        pw2 = fm.get("password2") or ""
        errors = []
        if len(username) < 3:
            errors.append("اسم المستخدم ثلاثة أحرف على الأقل.")
        if len(pw) < 10:
            errors.append("كلمة المرور عشرة محارف على الأقل.")
        if pw != pw2:
            errors.append("كلمتا المرور غير متطابقتين.")
        if pw.lower() in ("123456", "password", "admin", username.lower()):
            errors.append("كلمة المرور ضعيفة جداً.")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("setup.html", username=username)

        try:
            begin_write()
            if admin_exists():                      # إعادة الفحص بعد امتلاك القفل
                rollback()
                flash("أنشأ شخص آخر حساب المدير للتو. سجّل الدخول.", "error")
                return redirect(url_for("login"))
            cur = ex("""INSERT INTO users(username, password_hash, full_name, role, is_active)
                        VALUES (?,?,?,'admin',1)""",
                     (username, generate_password_hash(pw),
                      (fm.get("full_name") or "مدير النظام").strip()))
            audit("bootstrap", "user", cur.lastrowid, username,
                  {"role": "admin", "note": "أول مدير — تهيئة أولى"},
                  user={"id": None, "full_name": "تهيئة أولى"})
            commit()
        except Exception as err:
            rollback()
            flash(f"تعذّرت التهيئة: {err}", "error")
            return render_template("setup.html", username=username)

        flash("تم إنشاء حساب المدير. سجّل الدخول الآن.", "ok")
        return redirect(url_for("login"))

    return render_template("setup.html", username="")


@app.route("/login", methods=["GET", "POST"])
def login():
    if not admin_exists():
        return redirect(url_for("setup"))
    if request.method == "POST":
        u = q1("SELECT * FROM users WHERE username = ? AND is_active = 1",
               (request.form["username"].strip(),))
        if u and check_password_hash(u["password_hash"], request.form["password"]):
            session.clear()
            session["uid"] = u["id"]
            session["full_name"] = u["full_name"]
            audit("login", "user", u["id"], u["username"], None, user=dict(u))
            commit()
            return redirect(request.args.get("next") or url_for("dashboard"))
        flash("اسم المستخدم أو كلمة المرور غير صحيحة.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ============================================================
#  لوحة التحكم
# ============================================================
@app.route("/")
@login_required
def dashboard():
    rid = my_rep_id()
    rep_f = " AND inv.rep_id = %d" % rid if rid else ""

    kpi = {}
    kpi["sales"] = q1(f"SELECT COALESCE(SUM(total),0) s FROM invoices inv WHERE status='posted'{rep_f}")["s"]
    kpi["aggregate"] = q1(f"""SELECT COALESCE(SUM(total),0) s FROM invoices inv
                              WHERE status='posted' AND invoice_kind='aggregate'{rep_f}""")["s"]
    kpi["collected"] = q1(f"SELECT COALESCE(SUM(collected_total),0) s FROM invoices inv WHERE status='posted'{rep_f}")["s"]
    kpi["outstanding"] = q1(f"""SELECT COALESCE(SUM(total-returned_total-collected_total),0) s
                                FROM invoices inv WHERE status='posted'{rep_f}""")["s"]

    overdue = q(f"""SELECT inv.*, c.name cust, r.name rep,
                      ROUND(inv.total-inv.returned_total-inv.collected_total,2) outstanding,
                      CAST(julianday('now') - julianday(inv.due_date) AS INTEGER) late_days
                    FROM invoices inv JOIN customers c ON c.id=inv.customer_id
                    JOIN reps r ON r.id=inv.rep_id
                    WHERE inv.status='posted' AND date(inv.due_date) < date('now')
                      AND ROUND(inv.total-inv.returned_total-inv.collected_total,2) > 0{rep_f}
                    ORDER BY inv.due_date ASC LIMIT 15""")

    ch_f = f" AND ch.customer_id IN (SELECT id FROM customers WHERE rep_id={rid})" if rid else ""
    cheques = q(f"""SELECT ch.*, c.name cust,
                      CAST(julianday(ch.due_date) - julianday('now') AS INTEGER) days_left
                    FROM cheques ch JOIN customers c ON c.id=ch.customer_id
                    WHERE ch.status='pending'{ch_f}
                    ORDER BY ch.due_date ASC LIMIT 15""")

    low = q("SELECT * FROM v_item_stock WHERE qty_available <= 0 ORDER BY qty_available LIMIT 8")
    comm = q1(f"""SELECT COALESCE(SUM(commission_amt),0) c, COALESCE(SUM(bonus_amt),0) b
                  FROM commission_entries ce
                  WHERE status='earned'{' AND ce.rep_id=%d' % rid if rid else ''}""")
    late_rows = [r for r in E.late_scan()
                 if r["to_reverse"] > 0.009 and (not rid or r["invoice"]["rep_id"] == rid)]
    return render_template("dashboard.html", kpi=kpi, overdue=overdue, cheques=cheques,
                           low=low, comm=comm, late_n=len(late_rows),
                           late_amt=sum(r["to_reverse"] for r in late_rows))


# ============================================================
#  الأصناف
# ============================================================
@app.route("/items")
@login_required
def items():
    f = get_filters("items", ["q"])
    w = Where().search(["code", "name", "color", "size"], f["q"])
    rows = q(f"SELECT * FROM v_item_stock {w.sql()} ORDER BY code", w.args)
    if wants_export():
        return csv_response("items", [("الكود", "code"), ("الاسم", "name"), ("اللون", "color"),
                                      ("المقاس", "size"), ("سعر البيع", "sale_price"),
                                      ("التكلفة", "cost_price"), ("المتاح", "qty_available")], rows)
    return render_template("items.html", rows=rows, f=f)


@app.route("/items/new", methods=["GET", "POST"])
@app.route("/items/<int:iid>/edit", methods=["GET", "POST"])
@roles("admin", "accountant")
def item_form(iid=None):
    item = q1("SELECT * FROM items WHERE id=?", (iid,)) if iid else None
    if request.method == "POST":
        fm = request.form
        vals = (fm["code"].strip(), fm["name"].strip(), fm.get("color"), fm.get("size"),
                float(fm.get("sale_price") or 0), float(fm.get("cost_price") or 0))
        try:
            if item:
                ex("UPDATE items SET code=?,name=?,color=?,size=?,sale_price=?,cost_price=? WHERE id=?",
                   vals + (iid,))
                audit("update", "item", iid, fm["code"], {"before": dict(item)})
            else:
                cur = ex("INSERT INTO items(code,name,color,size,sale_price,cost_price) VALUES (?,?,?,?,?,?)", vals)
                audit("create", "item", cur.lastrowid, fm["code"])
            commit()
            flash("تم حفظ الصنف.", "ok")
            return redirect(url_for("items"))
        except Exception as err:
            rollback()
            flash(f"تعذّر الحفظ: {err}", "error")
    return render_template("item_form.html", item=item)


# ============================================================
#  العملاء
# ============================================================
@app.route("/customers")
@login_required
def customers():
    f = get_filters("customers", ["q", "rep_id", "method", "disc_min", "disc_max"])
    w = Where()
    scope(w, "c.rep_id")
    w.eq("c.rep_id", f["rep_id"]).txt("c.default_payment_method", f["method"])
    w.gte("c.default_discount_pct", f["disc_min"]).lte("c.default_discount_pct", f["disc_max"])
    w.search(["c.code", "c.name", "c.phone"], f["q"])
    rows = q(f"""SELECT c.*, r.name rep_name,
                   (SELECT COALESCE(SUM(total-returned_total-collected_total),0)
                    FROM invoices WHERE customer_id=c.id AND status='posted') balance,
                   (SELECT COALESCE(SUM(amount),0) FROM cheques
                    WHERE customer_id=c.id AND status='pending') pending_cheques
                 FROM customers c LEFT JOIN reps r ON r.id=c.rep_id
                 {w.sql()} ORDER BY c.code""", w.args)
    if wants_export():
        return csv_response("customers", [
            ("الكود", "code"), ("الاسم", "name"), ("المندوب", "rep_name"),
            ("خصم افتراضي", "default_discount_pct"), ("مدة السداد", "default_credit_days"),
            ("وسيلة الدفع", lambda r: METHODS.get(r["default_payment_method"], "")),
            ("سماحية خاصة", "grace_days"), ("الرصيد", "balance"),
            ("شيكات معلّقة", "pending_cheques")], rows)
    custs, reps, its = sources()
    return render_template("customers.html", rows=rows, f=f, reps=reps)


@app.route("/customers/new", methods=["GET", "POST"])
@app.route("/customers/<int:cid>/edit", methods=["GET", "POST"])
@roles("admin", "accountant")
def customer_form(cid=None):
    cust = q1("SELECT * FROM customers WHERE id=?", (cid,)) if cid else None
    reps = q("SELECT * FROM reps WHERE is_active=1 ORDER BY name")
    if request.method == "POST":
        fm = request.form
        grace = fm.get("grace_days")
        vals = (fm["code"].strip(), fm["name"].strip(), int(fm["rep_id"]), fm.get("phone"),
                fm.get("address"), float(fm.get("default_discount_pct") or 0),
                int(fm.get("default_credit_days") or 0),
                fm.get("default_payment_method") or "credit",
                int(grace) if grace not in (None, "") else None)
        try:
            if cust:
                ex("""UPDATE customers SET code=?,name=?,rep_id=?,phone=?,address=?,
                      default_discount_pct=?,default_credit_days=?,default_payment_method=?,
                      grace_days=? WHERE id=?""", vals + (cid,))
                audit("update", "customer", cid, fm["code"], {"before": dict(cust)})
            else:
                cur = ex("""INSERT INTO customers(code,name,rep_id,phone,address,
                            default_discount_pct,default_credit_days,default_payment_method,grace_days)
                            VALUES (?,?,?,?,?,?,?,?,?)""", vals)
                audit("create", "customer", cur.lastrowid, fm["code"])
            commit()
            flash("تم حفظ العميل.", "ok")
            return redirect(url_for("customers"))
        except Exception as err:
            rollback()
            flash(f"تعذّر الحفظ: {err}", "error")
    default_grace = get_setting("late_policy", E.DEFAULT_LATE_POLICY).get("grace_days", 15)
    return render_template("customer_form.html", cust=cust, reps=reps, default_grace=default_grace)


# ============================================================
#  المندوبون
# ============================================================
@app.route("/reps")
@roles("admin", "accountant")
def reps_list():
    rows = q("""SELECT r.*, (SELECT COUNT(*) FROM customers WHERE rep_id=r.id) n_cust,
                  (SELECT COALESCE(SUM(total),0) FROM invoices WHERE rep_id=r.id AND status='posted') sales,
                  (SELECT COALESCE(SUM(commission_amt+bonus_amt),0) FROM commission_entries
                   WHERE rep_id=r.id AND status='earned') earned
                FROM reps r ORDER BY r.code""")
    return render_template("reps.html", rows=rows)


@app.route("/reps/new", methods=["GET", "POST"])
@app.route("/reps/<int:rid>/edit", methods=["GET", "POST"])
@roles("admin")
def rep_form(rid=None):
    rep = q1("SELECT * FROM reps WHERE id=?", (rid,)) if rid else None
    if request.method == "POST":
        fm = request.form
        vals = (fm["code"].strip(), fm["name"].strip(), fm.get("phone"), fm.get("hire_date") or None)
        try:
            if rep:
                ex("UPDATE reps SET code=?,name=?,phone=?,hire_date=? WHERE id=?", vals + (rid,))
                audit("update", "rep", rid, fm["code"])
            else:
                cur = ex("INSERT INTO reps(code,name,phone,hire_date) VALUES (?,?,?,?)", vals)
                audit("create", "rep", cur.lastrowid, fm["code"])
            commit()
            flash("تم حفظ المندوب.", "ok")
            return redirect(url_for("reps_list"))
        except Exception as err:
            rollback()
            flash(f"تعذّر الحفظ: {err}", "error")
    return render_template("rep_form.html", rep=rep)


# ============================================================
#  الفواتير
# ============================================================
INVOICE_SELECT = """SELECT inv.*, c.name cust, c.code cust_code, r.name rep,
      ROUND(inv.total-inv.returned_total-inv.collected_total,2) outstanding,
      CAST(julianday('now') - julianday(inv.due_date) AS INTEGER) late_days
    FROM invoices inv JOIN customers c ON c.id=inv.customer_id JOIN reps r ON r.id=inv.rep_id"""

OUT_EXPR = "ROUND(inv.total-inv.returned_total-inv.collected_total,2)"


def invoice_status_clause(w, status):
    if status == "open":
        w.add(f"inv.status='posted' AND {OUT_EXPR} > 0.009 AND inv.collected_total <= 0.009")
    elif status == "partial":
        w.add(f"inv.status='posted' AND {OUT_EXPR} > 0.009 AND inv.collected_total > 0.009")
    elif status == "paid":
        w.add(f"inv.status='posted' AND {OUT_EXPR} <= 0.009")
    elif status == "late":
        w.add(f"inv.status='posted' AND {OUT_EXPR} > 0.009 AND date(inv.due_date) < date('now')")
    elif status == "void":
        w.add("inv.status='void'")
    return w


@app.route("/invoices")
@login_required
def invoices():
    keys = ["q", "customer_id", "rep_id", "date_from", "date_to", "due_from", "due_to",
            "method", "status", "kind", "disc_min", "disc_max"]
    f = get_filters("invoices", keys)
    w = Where()
    scope(w, "inv.rep_id")
    w.eq("inv.customer_id", f["customer_id"]).eq("inv.rep_id", f["rep_id"])
    w.date_between("inv.invoice_date", f["date_from"], f["date_to"])
    w.date_between("inv.due_date", f["due_from"], f["due_to"])
    w.txt("inv.payment_method", f["method"]).txt("inv.invoice_kind", f["kind"])
    w.gte("inv.discount_pct", f["disc_min"]).lte("inv.discount_pct", f["disc_max"])
    w.search(["inv.ref_no", "inv.external_ref", "c.name", "c.code", "inv.notes"], f["q"])
    invoice_status_clause(w, f["status"])
    rows = q(f"{INVOICE_SELECT} {w.sql()} ORDER BY date(inv.invoice_date) DESC, inv.id DESC LIMIT 500",
             w.args)
    if wants_export():
        return csv_response("invoices", [
            ("الرقم", "ref_no"), ("النوع", lambda r: KINDS[r["invoice_kind"]]),
            ("مرجع خارجي", "external_ref"), ("التاريخ", "invoice_date"), ("العميل", "cust"),
            ("المندوب", "rep"), ("الخصم %", "discount_pct"),
            ("طريقة الدفع", lambda r: METHODS.get(r["payment_method"], "")),
            ("الاستحقاق", "due_date"), ("الإجمالي", "total"), ("المرتجعات", "returned_total"),
            ("المحصَّل", "collected_total"), ("المتبقي", "outstanding")], rows)
    custs, reps, its = sources()
    totals = {"total": sum(r["total"] for r in rows),
              "outstanding": sum(r["outstanding"] for r in rows),
              "collected": sum(r["collected_total"] for r in rows)}
    return render_template("invoices.html", rows=rows, f=f, customers=custs, reps=reps,
                           statuses=INV_STATUSES, totals=totals)


@app.route("/invoices/new", methods=["GET", "POST"])
@roles("admin", "accountant", "rep")
def invoice_new():
    rid = my_rep_id()
    custs = q(f"SELECT * FROM customers WHERE is_active=1 {'AND rep_id=%d' % rid if rid else ''} ORDER BY name")
    its = q("SELECT * FROM v_item_stock ORDER BY name")
    kind = request.values.get("invoice_kind", "detailed")
    if request.method == "POST":
        fm = request.form
        data = {
            "customer_id": int(fm["customer_id"]), "invoice_date": fm["invoice_date"],
            "discount_pct": fm.get("discount_pct"), "credit_days": fm.get("credit_days"),
            "payment_method": fm.get("payment_method"), "notes": fm.get("notes"),
            "is_historical": fm.get("is_historical"), "invoice_kind": kind,
            "external_ref": fm.get("external_ref"), "due_date": fm.get("due_date") or None,
            "total_amount": fm.get("total_amount"),
            "rep_id": int(fm["rep_id"]) if fm.get("rep_id") else None,
        }
        lines = []
        if kind == "detailed":
            for i, item_id in enumerate(fm.getlist("item_id")):
                if item_id and fm.getlist("qty")[i]:
                    lines.append({"item_id": int(item_id), "qty": float(fm.getlist("qty")[i]),
                                  "unit_price": float(fm.getlist("unit_price")[i] or 0)})
        override = bool(fm.get("override_stock")) and current_user()["role"] == "admin"
        try:
            iid, ref = E.create_invoice(data, lines, dict(current_user()), override_stock=override)
            commit()
            flash(f"تم ترحيل الفاتورة {ref}.", "ok")
            return redirect(url_for("invoice_view", iid=iid))
        except Exception as err:
            rollback()
            flash(str(err), "error")
    reps = q("SELECT * FROM reps WHERE is_active=1 ORDER BY name")
    return render_template("invoice_form.html", custs=custs, items=its, reps=reps, kind=kind)


@app.route("/invoices/<int:iid>")
@login_required
def invoice_view(iid):
    inv = q1(f"{INVOICE_SELECT} WHERE inv.id=?", (iid,))
    if not inv:
        abort(404)
    if my_rep_id() and inv["rep_id"] != my_rep_id():
        abort(403)
    lines = q("""SELECT il.*, i.code, i.name, i.color, i.size FROM invoice_lines il
                 JOIN items i ON i.id=il.item_id WHERE il.invoice_id=?""", (iid,))
    allocs = q("""SELECT a.*, co.ref_no coll_ref, co.method, co.collection_date,
                    ch.cheque_number, ch.due_date chq_due, ch.status chq_status,
                    r.ref_no ret_ref
                  FROM allocations a
                  LEFT JOIN collections co ON co.id=a.source_id AND a.source_type='collection'
                  LEFT JOIN cheques ch ON ch.id=co.cheque_id
                  LEFT JOIN returns r ON r.id=a.source_id AND a.source_type='return'
                  WHERE a.invoice_id=? ORDER BY a.alloc_date, a.id""", (iid,))
    comms = q("SELECT * FROM commission_entries WHERE invoice_id=? ORDER BY id", (iid,))
    snap = json.loads(inv["rules_snapshot"])
    cust = q1("SELECT * FROM customers WHERE id=?", (inv["customer_id"],))
    policy = snap.get("late_policy", get_setting("late_policy", E.DEFAULT_LATE_POLICY))
    deadline = E.invoice_deadline(inv, cust, policy).isoformat()
    trail = q("SELECT * FROM audit_log WHERE entity='invoice' AND entity_id=? ORDER BY id", (iid,))
    return render_template("invoice_view.html", inv=inv, lines=lines, allocs=allocs,
                           comms=comms, snap=snap, deadline=deadline, trail=trail)


@app.route("/invoices/<int:iid>/void", methods=["POST"])
@roles("admin")
def invoice_void(iid):
    inv = q1("SELECT * FROM invoices WHERE id=?", (iid,))
    if inv["collected_total"] > 0 or inv["returned_total"] > 0:
        flash("لا يمكن إلغاء فاتورة عليها تحصيلات أو مرتجعات. أوقف التحصيلات أولاً.", "error")
        return redirect(url_for("invoice_view", iid=iid))
    ex("UPDATE invoices SET status='void' WHERE id=?", (iid,))
    for ln in q("SELECT * FROM invoice_lines WHERE invoice_id=?", (iid,)):
        E.add_stock_move(date.today().isoformat(), ln["item_id"], "adjustment", ln["qty"],
                         ln["unit_cost"], ln["unit_price"], "invoice_void", iid, inv["ref_no"],
                         "إلغاء فاتورة", current_user()["id"])
    audit("void", "invoice", iid, inv["ref_no"], {"total": inv["total"]},
          request.form.get("reason"))
    commit()
    flash("تم إلغاء الفاتورة وتسجيلها في سجل التدقيق.", "ok")
    return redirect(url_for("invoices"))


# ============================================================
#  المرتجعات
# ============================================================
@app.route("/returns")
@login_required
def returns():
    keys = ["q", "customer_id", "rep_id", "date_from", "date_to", "status", "disc_min", "disc_max"]
    f = get_filters("returns", keys)
    w = Where()
    scope(w, "r.rep_id")
    w.eq("r.customer_id", f["customer_id"]).eq("r.rep_id", f["rep_id"])
    w.date_between("r.return_date", f["date_from"], f["date_to"])
    w.txt("r.status", f["status"])
    w.gte("r.discount_pct", f["disc_min"]).lte("r.discount_pct", f["disc_max"])
    w.search(["r.ref_no", "inv.ref_no", "c.name", "c.code"], f["q"])
    rows = q(f"""SELECT r.*, c.name cust, inv.ref_no inv_ref, rp.name rep
                 FROM returns r JOIN customers c ON c.id=r.customer_id
                 JOIN invoices inv ON inv.id=r.invoice_id JOIN reps rp ON rp.id=r.rep_id
                 {w.sql()} ORDER BY date(r.return_date) DESC, r.id DESC LIMIT 500""", w.args)
    if wants_export():
        return csv_response("returns", [
            ("الرقم", "ref_no"), ("التاريخ", "return_date"), ("الفاتورة", "inv_ref"),
            ("العميل", "cust"), ("المندوب", "rep"), ("قبل الخصم", "subtotal"),
            ("الخصم %", "discount_pct"), ("الصافي", "total")], rows)
    custs, reps, its = sources()
    return render_template("returns.html", rows=rows, f=f, customers=custs, reps=reps,
                           statuses=DOC_STATUSES)


@app.route("/returns/new", methods=["GET", "POST"])
@roles("admin", "accountant")
def return_new():
    invs = q("""SELECT inv.id, inv.ref_no, inv.invoice_date, inv.total, c.name cust
                FROM invoices inv JOIN customers c ON c.id=inv.customer_id
                WHERE inv.status='posted' AND inv.invoice_kind='detailed'
                ORDER BY date(inv.invoice_date) DESC LIMIT 500""")
    if request.method == "POST":
        fm = request.form
        lines = []
        for i, item_id in enumerate(fm.getlist("item_id")):
            qv = fm.getlist("qty")[i]
            if item_id and qv and float(qv) > 0:
                lines.append({"item_id": int(item_id), "qty": float(qv)})
        try:
            _, ref = E.create_return({"invoice_id": int(fm["invoice_id"]),
                                      "return_date": fm["return_date"],
                                      "notes": fm.get("notes")}, lines, dict(current_user()))
            commit()
            flash(f"تم ترحيل المرتجع {ref}.", "ok")
            return redirect(url_for("returns"))
        except Exception as err:
            rollback()
            flash(str(err), "error")
    return render_template("return_form.html", invs=invs)


# ============================================================
#  التحصيلات
# ============================================================
@app.route("/collections")
@login_required
def collections():
    keys = ["q", "customer_id", "rep_id", "date_from", "date_to", "method", "status"]
    f = get_filters("collections", keys)
    w = Where()
    scope(w, "co.rep_id")
    w.eq("co.customer_id", f["customer_id"]).eq("co.rep_id", f["rep_id"])
    w.date_between("co.collection_date", f["date_from"], f["date_to"])
    w.txt("co.method", f["method"]).txt("co.status", f["status"])
    w.search(["co.ref_no", "c.name", "c.code", "ch.cheque_number", "co.notes"], f["q"])
    rows = q(f"""SELECT co.*, c.name cust, r.name rep, ch.due_date cheque_due,
                   ch.status cheque_status, ch.cheque_number, ch.id chq_id
                 FROM collections co JOIN customers c ON c.id=co.customer_id
                 JOIN reps r ON r.id=co.rep_id LEFT JOIN cheques ch ON ch.id=co.cheque_id
                 {w.sql()} ORDER BY date(co.collection_date) DESC, co.id DESC LIMIT 500""", w.args)
    if wants_export():
        return csv_response("collections", [
            ("الرقم", "ref_no"), ("التاريخ", "collection_date"), ("العميل", "cust"),
            ("المندوب", "rep"), ("الوسيلة", lambda r: METHODS.get(r["method"], "")),
            ("رقم الشيك", "cheque_number"), ("استحقاق الشيك", "cheque_due"),
            ("حالة الشيك", lambda r: CHQ_LABELS.get(r["cheque_status"] or "", "")),
            ("القيمة", "amount"), ("المخصَّص", "allocated_total"), ("الحالة", "status")], rows)
    custs, reps, its = sources()
    totals = {"amount": sum(r["amount"] for r in rows if r["status"] == "posted"),
              "allocated": sum(r["allocated_total"] for r in rows if r["status"] == "posted")}
    return render_template("collections.html", rows=rows, f=f, customers=custs, reps=reps,
                           statuses=DOC_STATUSES, totals=totals)


@app.route("/collections/new", methods=["GET", "POST"])
@roles("admin", "accountant", "rep")
def collection_new():
    rid = my_rep_id()
    custs = q(f"SELECT * FROM customers WHERE is_active=1 {'AND rep_id=%d' % rid if rid else ''} ORDER BY name")
    if request.method == "POST":
        fm = request.form
        try:
            _, ref, alloc, rest = E.create_collection({
                "customer_id": int(fm["customer_id"]), "collection_date": fm["collection_date"],
                "method": fm["method"], "amount": float(fm["amount"]),
                "cheque_number": fm.get("cheque_number"), "bank_name": fm.get("bank_name"),
                "cheque_due_date": fm.get("cheque_due_date"), "notes": fm.get("notes"),
            }, dict(current_user()))
            commit()
            msg = f"تم تسجيل الدفعة {ref} وتخصيص {alloc:,.2f} على أقدم الفواتير."
            if rest > 0:
                msg += f" تبقّى {rest:,.2f} كرصيد دائن للعميل."
            if fm["method"] == "cheque":
                msg += " الشيك مسجَّل «معلّق»: خُصم من الرصيد ولن تُحتسب عمولته إلا في تاريخ استحقاقه."
            flash(msg, "ok")
            return redirect(url_for("collections"))
        except Exception as err:
            rollback()
            flash(str(err), "error")
    return render_template("collection_form.html", custs=custs)


@app.route("/api/fifo-preview")
@login_required
def fifo_preview():
    cid = int(request.args.get("customer_id", 0))
    amt = float(request.args.get("amount") or 0)
    plan, rest = E.preview_fifo(cid, amt)
    return jsonify({"plan": plan, "unallocated": rest})


@app.route("/api/customer/<int:cid>")
@login_required
def api_customer(cid):
    c = q1("SELECT * FROM customers WHERE id=?", (cid,))
    if not c:
        abort(404)
    bal = q1("""SELECT COALESCE(SUM(total-returned_total-collected_total),0) b
                FROM invoices WHERE customer_id=? AND status='posted'""", (cid,))["b"]
    return jsonify({"discount_pct": c["default_discount_pct"],
                    "credit_days": c["default_credit_days"],
                    "payment_method": c["default_payment_method"],
                    "rep_id": c["rep_id"], "balance": round(bal, 2)})


@app.route("/api/invoice/<int:iid>/lines")
@login_required
def api_invoice_lines(iid):
    inv = q1("SELECT * FROM invoices WHERE id=?", (iid,))
    rows = q("""SELECT il.item_id, i.code, i.name, i.color, i.size, il.qty, il.unit_price,
                  COALESCE((SELECT SUM(rl.qty) FROM return_lines rl JOIN returns r ON r.id=rl.return_id
                            WHERE r.invoice_id=il.invoice_id AND rl.item_id=il.item_id
                              AND r.status='posted'),0) returned
                FROM invoice_lines il JOIN items i ON i.id=il.item_id WHERE il.invoice_id=?""", (iid,))
    return jsonify({"discount_pct": inv["discount_pct"], "invoice_date": inv["invoice_date"],
                    "lines": [dict(r) for r in rows]})


# ============================================================
#  الشيكات
# ============================================================
@app.route("/cheques")
@login_required
def cheques():
    keys = ["q", "customer_id", "rep_id", "date_from", "date_to", "due_from", "due_to", "status"]
    f = get_filters("cheques", keys)
    w = Where()
    scope(w, "c.rep_id")
    w.eq("ch.customer_id", f["customer_id"]).eq("c.rep_id", f["rep_id"])
    w.date_between("ch.received_date", f["date_from"], f["date_to"])
    w.date_between("ch.due_date", f["due_from"], f["due_to"])
    w.txt("ch.status", f["status"])
    w.search(["ch.ref_no", "ch.cheque_number", "ch.bank_name", "c.name", "c.code"], f["q"])
    rows = q(f"""SELECT ch.*, c.name cust, r.name rep, co.ref_no coll_ref,
                   CAST(julianday(ch.due_date) - julianday('now') AS INTEGER) days_left
                 FROM cheques ch JOIN customers c ON c.id=ch.customer_id
                 LEFT JOIN reps r ON r.id=c.rep_id
                 LEFT JOIN collections co ON co.cheque_id=ch.id
                 {w.sql()} ORDER BY ch.status, ch.due_date""", w.args)
    if wants_export():
        return csv_response("cheques", [
            ("المرجع", "ref_no"), ("رقم الشيك", "cheque_number"), ("البنك", "bank_name"),
            ("العميل", "cust"), ("المندوب", "rep"), ("القيمة", "amount"),
            ("تاريخ الاستلام", "received_date"), ("تاريخ الاستحقاق", "due_date"),
            ("الحالة", lambda r: CHQ_LABELS.get(r["status"], "")),
            ("تاريخ التحصيل", "cleared_date"), ("تلقائي", "auto_cleared"),
            ("تاريخ الارتداد", "bounce_date"), ("سبب الارتداد", "bounce_reason")], rows)
    custs, reps, its = sources()
    totals = {s: sum(r["amount"] for r in rows if r["status"] == s)
              for s in ("pending", "cleared", "bounced")}
    return render_template("cheques.html", rows=rows, f=f, customers=custs, reps=reps,
                           statuses=CHQ_STATUSES, totals=totals)


@app.route("/cheques/<int:chid>/<status>", methods=["POST"])
@roles("admin", "accountant")
def cheque_status(chid, status):
    try:
        E.set_cheque_status(chid, status, dict(current_user()),
                            request.form.get("when") or None,
                            request.form.get("reason") or None)
        commit()
        flash("تم تحديث حالة الشيك وتسجيلها في سجل التدقيق.", "ok")
    except Exception as err:
        rollback()
        flash(str(err), "error")
    return redirect(request.referrer or url_for("cheques"))


@app.route("/jobs/run", methods=["POST"])
@roles("admin")
def jobs_run():
    res = E.run_daily_jobs(dict(current_user()), force=True)
    commit()
    flash(f"تم الفحص اليومي: حُصّل {res['cheques_cleared']} شيك مستحق، "
          f"وأُنشئ {res['late_reversals']} قيد عكس لتأخر التحصيل.", "ok")
    return redirect(request.referrer or url_for("dashboard"))


# ============================================================
#  المخزون
# ============================================================
@app.route("/stock")
@login_required
def stock():
    f = get_filters("stock", ["q", "item_id", "date_from", "date_to", "move_type"])
    w = Where()
    w.eq("sm.item_id", f["item_id"]).txt("sm.move_type", f["move_type"])
    w.date_between("sm.move_date", f["date_from"], f["date_to"])
    w.search(["i.code", "i.name", "sm.ref_no", "sm.notes"], f["q"])
    moves = q(f"""SELECT sm.*, i.code, i.name FROM stock_moves sm JOIN items i ON i.id=sm.item_id
                  {w.sql()} ORDER BY date(sm.move_date) DESC, sm.id DESC LIMIT 600""", w.args)
    if wants_export():
        return csv_response("stock_moves", [
            ("التاريخ", "move_date"), ("الكود", "code"), ("الصنف", "name"),
            ("النوع", lambda r: MOVE_TYPES.get(r["move_type"], "")), ("الكمية", "qty"),
            ("التكلفة", "unit_cost"), ("سعر البيع", "unit_price"), ("المستند", "ref_no"),
            ("ملاحظات", "notes")], moves)
    stocks = q("SELECT * FROM v_item_stock ORDER BY name")
    custs, reps, its = sources()
    return render_template("stock.html", moves=moves, stocks=stocks, f=f, items=its)


@app.route("/stock/new", methods=["GET", "POST"])
@roles("admin", "accountant")
def stock_new():
    its = q("SELECT * FROM v_item_stock ORDER BY name")
    if request.method == "POST":
        fm = request.form
        try:
            n = 0
            for i, item_id in enumerate(fm.getlist("item_id")):
                qv = fm.getlist("qty")[i]
                if not item_id or not qv or float(qv) == 0:
                    continue
                E.add_stock_move(fm["move_date"], int(item_id), fm["move_type"], float(qv),
                                 float(fm.getlist("unit_cost")[i] or 0),
                                 float(fm.getlist("unit_price")[i] or 0),
                                 "manual", None, None, fm.get("notes"), current_user()["id"])
                n += 1
            audit("create", "stock_move", None, fm["move_type"], {"lines": n, "date": fm["move_date"]})
            commit()
            flash(f"تم تسجيل {n} حركة مخزون بتاريخ {fm['move_date']}.", "ok")
            return redirect(url_for("stock"))
        except Exception as err:
            rollback()
            flash(str(err), "error")
    return render_template("stock_form.html", items=its)


# ============================================================
#  الإدخال السريع المتتابع
# ============================================================
QUICK_MODES = {"invoice": "فاتورة تاريخية إجمالية", "collection": "تحصيل", "cheque": "شيك"}


def customer_balance(cid):
    r = q1("""SELECT COALESCE(SUM(total-returned_total-collected_total),0) b
              FROM invoices WHERE customer_id=? AND status='posted'""", (cid,))
    return round(r["b"], 2)


@app.route("/quick", methods=["GET", "POST"])
@roles("admin", "accountant", "rep")
def quick():
    rid = my_rep_id()
    custs = q(f"SELECT * FROM customers WHERE is_active=1 {'AND rep_id=%d' % rid if rid else ''} ORDER BY name")
    reps = q("SELECT * FROM reps WHERE is_active=1 ORDER BY name")
    st = session.get("quick") or {"customer_id": None, "rep_id": None, "method": "cash",
                                  "payment_method": "credit", "credit_days": "", "discount_pct": "",
                                  "log": [], "opening_balance": None}
    mode = request.values.get("mode", "invoice")

    if request.method == "POST":
        fm = request.form
        if fm.get("reset"):
            session.pop("quick", None)
            flash("تم إنهاء جلسة الإدخال السريع.", "ok")
            return redirect(url_for("quick", mode=mode))
        try:
            cid = int(fm["customer_id"])
            if st.get("customer_id") != cid:
                st["opening_balance"] = customer_balance(cid)
                st["log"] = []
            st["customer_id"] = cid
            st["rep_id"] = int(fm["rep_id"]) if fm.get("rep_id") else None
            user = dict(current_user())

            if mode == "invoice":
                st["payment_method"] = fm.get("payment_method") or "credit"
                st["credit_days"] = fm.get("credit_days") or ""
                st["discount_pct"] = fm.get("discount_pct") or ""
                _, ref = E.create_aggregate_invoice({
                    "customer_id": cid, "rep_id": st["rep_id"], "invoice_date": fm["invoice_date"],
                    "total_amount": fm["total_amount"], "discount_pct": fm.get("discount_pct"),
                    "credit_days": fm.get("credit_days"), "due_date": fm.get("due_date") or None,
                    "payment_method": fm.get("payment_method"), "notes": fm.get("notes"),
                    "external_ref": fm.get("external_ref"),
                }, user)
                st["log"].append({"kind": "فاتورة إجمالية", "ref": ref, "date": fm["invoice_date"],
                                  "amount": round(float(fm["total_amount"]), 2)})
                msg = f"تم حفظ الفاتورة {ref}."
            elif mode == "collection":
                st["method"] = fm.get("method") or "cash"
                _, ref, alloc, rest = E.create_collection({
                    "customer_id": cid, "rep_id": st["rep_id"],
                    "collection_date": fm["collection_date"], "method": fm["method"],
                    "amount": float(fm["amount"]), "notes": fm.get("notes"),
                }, user)
                st["log"].append({"kind": "تحصيل " + METHODS[fm["method"]], "ref": ref,
                                  "date": fm["collection_date"],
                                  "amount": -round(float(fm["amount"]), 2)})
                msg = f"تم حفظ الدفعة {ref} وتخصيص {alloc:,.2f} على أقدم الفواتير."
                if rest > 0:
                    msg += f" تبقّى {rest:,.2f} رصيداً دائناً."
            else:
                _, ref, alloc, rest = E.create_collection({
                    "customer_id": cid, "rep_id": st["rep_id"],
                    "collection_date": fm["received_date"], "method": "cheque",
                    "amount": float(fm["amount"]), "cheque_number": fm.get("cheque_number"),
                    "bank_name": fm.get("bank_name"), "cheque_due_date": fm["cheque_due_date"],
                    "notes": fm.get("notes"),
                }, user)
                st["log"].append({
                    "kind": f"شيك {fm.get('cheque_number') or ''} مستحق {fm['cheque_due_date']}",
                    "ref": ref, "date": fm["received_date"],
                    "amount": -round(float(fm["amount"]), 2)})
                msg = f"تم حفظ الشيك ضمن الدفعة {ref} بحالة «معلّق»."
            commit()
            session["quick"] = st
            session.modified = True
            flash(msg, "ok")
        except Exception as err:
            rollback()
            flash(str(err), "error")
        return redirect(url_for("quick", mode=mode))

    cust = q1("SELECT * FROM customers WHERE id=?", (st["customer_id"],)) if st.get("customer_id") else None
    now_balance = customer_balance(cust["id"]) if cust else None
    return render_template("quick.html", custs=custs, reps=reps, st=st, mode=mode,
                           modes=QUICK_MODES, cust=cust, now_balance=now_balance)


# ============================================================
#  الإعدادات وسجل التدقيق
# ============================================================
@app.route("/settings", methods=["GET", "POST"])
@roles("admin")
def settings_page():
    if request.method == "POST":
        fm = request.form
        try:
            rules = []
            for i, lab in enumerate(fm.getlist("cr_label")):
                if fm.getlist("cr_max")[i] == "":
                    continue
                rules.append({"label": lab, "min_discount": float(fm.getlist("cr_min")[i]),
                              "max_discount": float(fm.getlist("cr_max")[i]),
                              "commission_pct": float(fm.getlist("cr_pct")[i])})
            tiers = []
            for i, lab in enumerate(fm.getlist("bt_label")):
                if fm.getlist("bt_days")[i] == "":
                    continue
                tiers.append({"label": lab, "up_to_days": int(fm.getlist("bt_days")[i]),
                              "bonus_pct": float(fm.getlist("bt_pct")[i])})
            basis = {m: fm.get(f"basis_{m}", "invoice_date")
                     for m in ("cash", "credit", "cheque", "transfer")}
            policy = {"block_oversell": bool(fm.get("block_oversell")), "admin_can_override": True}
            late = {"enabled": bool(fm.get("late_enabled")),
                    "grace_days": int(fm.get("grace_days") or 15),
                    "bonus_basis": fm.get("late_bonus_basis", "tier_at_deadline"),
                    "restore_on_collection": bool(fm.get("restore_on_collection"))}
            before = {"commission_rules": get_setting("commission_rules"),
                      "bonus_tiers": get_setting("bonus_tiers"),
                      "late_policy": get_setting("late_policy")}
            uid = current_user()["id"]
            set_setting("commission_rules", rules, uid)
            set_setting("bonus_tiers", tiers, uid)
            set_setting("bonus_basis", basis, uid)
            set_setting("stock_policy", policy, uid)
            set_setting("late_policy", late, uid)
            audit("update", "settings", None, "rules",
                  {"before": before, "after": {"commission_rules": rules, "bonus_tiers": tiers,
                                               "late_policy": late}})
            commit()
            flash("تم حفظ القواعد. الفواتير القديمة تحتفظ بالقواعد المطبقة وقت إنشائها.", "ok")
        except Exception as err:
            rollback()
            flash(f"تعذّر الحفظ: {err}", "error")
        return redirect(url_for("settings_page"))
    return render_template("settings.html",
                           rules=get_setting("commission_rules", E.DEFAULT_COMMISSION_RULES),
                           tiers=get_setting("bonus_tiers", E.DEFAULT_BONUS_TIERS),
                           basis=get_setting("bonus_basis", E.DEFAULT_BONUS_BASIS),
                           policy=get_setting("stock_policy", E.DEFAULT_STOCK_POLICY),
                           late=get_setting("late_policy", E.DEFAULT_LATE_POLICY))


@app.route("/audit")
@roles("admin")
def audit_page():
    f = get_filters("audit", ["q", "date_from", "date_to"])
    w = Where()
    w.date_between("at", f["date_from"], f["date_to"])
    w.search(["user_name", "action", "entity", "entity_ref", "details", "reason"], f["q"])
    rows = q(f"SELECT * FROM audit_log {w.sql()} ORDER BY id DESC LIMIT 600", w.args)
    if wants_export():
        return csv_response("audit_log", [
            ("الوقت", "at"), ("المستخدم", "user_name"), ("الإجراء", "action"),
            ("الكيان", "entity"), ("المرجع", "entity_ref"), ("التفاصيل", "details"),
            ("السبب", "reason")], rows)
    return render_template("audit.html", rows=rows, f=f)


# ============================================================
#  التقارير
# ============================================================
@app.route("/reports/sales")
@login_required
def rep_sales():
    keys = ["customer_id", "rep_id", "item_id", "date_from", "date_to", "method", "kind",
            "disc_min", "disc_max"]
    f = get_filters("sales", keys)
    if not f["date_from"]:
        f["date_from"] = (date.today() - timedelta(days=365)).isoformat()
    if not f["date_to"]:
        f["date_to"] = date.today().isoformat()

    def base_where():
        w = Where("inv.status='posted'")
        scope(w, "inv.rep_id")
        w.eq("inv.customer_id", f["customer_id"]).eq("inv.rep_id", f["rep_id"])
        w.date_between("inv.invoice_date", f["date_from"], f["date_to"])
        w.txt("inv.payment_method", f["method"]).txt("inv.invoice_kind", f["kind"])
        w.gte("inv.discount_pct", f["disc_min"]).lte("inv.discount_pct", f["disc_max"])
        return w

    w1 = base_where()
    by_kind = q(f"""SELECT inv.invoice_kind kind, COUNT(*) n, SUM(inv.total) net,
                      SUM(inv.returned_total) rets, SUM(inv.collected_total) collected
                    FROM invoices inv {w1.sql()} GROUP BY inv.invoice_kind""", w1.args)
    w2 = base_where()
    by_rep = q(f"""SELECT r.name, COUNT(*) n,
                     SUM(CASE WHEN inv.invoice_kind='detailed' THEN inv.total ELSE 0 END) detailed,
                     SUM(CASE WHEN inv.invoice_kind='aggregate' THEN inv.total ELSE 0 END) aggregate,
                     SUM(inv.total) net
                   FROM invoices inv JOIN reps r ON r.id=inv.rep_id
                   {w2.sql()} GROUP BY r.id ORDER BY net DESC""", w2.args)
    w3 = base_where()
    by_cust = q(f"""SELECT c.name, COUNT(*) n,
                      SUM(CASE WHEN inv.invoice_kind='detailed' THEN inv.total ELSE 0 END) detailed,
                      SUM(CASE WHEN inv.invoice_kind='aggregate' THEN inv.total ELSE 0 END) aggregate,
                      SUM(inv.total) net
                    FROM invoices inv JOIN customers c ON c.id=inv.customer_id
                    {w3.sql()} GROUP BY c.id ORDER BY net DESC""", w3.args)

    # تقرير الأصناف: الفواتير التفصيلية فقط — الإجمالية بلا أصناف
    w4 = base_where()
    w4.add("inv.invoice_kind='detailed'")
    w4.eq("il.item_id", f["item_id"])
    by_item = q(f"""SELECT i.code, i.name, i.color, i.size, SUM(il.qty) qty,
                      SUM(il.line_total) gross,
                      SUM(il.line_total*(1-inv.discount_pct/100.0)) net
                    FROM invoice_lines il JOIN invoices inv ON inv.id=il.invoice_id
                    JOIN items i ON i.id=il.item_id {w4.sql()}
                    GROUP BY i.id ORDER BY net DESC""", w4.args)

    if wants_export():
        return csv_response("sales_by_item", [
            ("الكود", "code"), ("الصنف", "name"), ("اللون", "color"), ("المقاس", "size"),
            ("الكمية", "qty"), ("قبل الخصم", "gross"), ("الصافي", "net")], by_item)

    custs, reps, its = sources()
    return render_template("rep_sales.html", by_item=by_item, by_rep=by_rep, by_cust=by_cust,
                           by_kind={r["kind"]: r for r in by_kind}, f=f,
                           customers=custs, reps=reps, items=its)


@app.route("/reports/statement")
@login_required
def rep_statement():
    f = get_filters("statement", ["customer_id"])
    rid = my_rep_id()
    custs = q(f"SELECT * FROM customers {'WHERE rep_id=%d' % rid if rid else ''} ORDER BY name")
    cid = f["customer_id"]
    rows, cust, totals, chq = [], None, {}, {}
    if cid:
        cust = q1("SELECT * FROM customers WHERE id=?", (cid,))
        if not cust:
            abort(404)
        if rid and cust["rep_id"] != rid:
            abort(403)
        for r in q("""SELECT invoice_date dt, ref_no, invoice_kind kc, total amt, id
                      FROM invoices WHERE customer_id=? AND status='posted'""", (cid,)):
            rows.append({"dt": r["dt"], "ref_no": r["ref_no"], "amt": r["amt"], "id": r["id"],
                         "kind": "فاتورة إجمالية" if r["kc"] == "aggregate" else "فاتورة"})
        for r in q("""SELECT return_date dt, ref_no, -total amt, id
                      FROM returns WHERE customer_id=? AND status='posted'""", (cid,)):
            rows.append({"dt": r["dt"], "ref_no": r["ref_no"], "amt": r["amt"], "id": r["id"],
                         "kind": "مرتجع"})
        for r in q("""SELECT co.collection_date dt, co.ref_no, -co.amount amt, co.id, co.method
                      FROM collections co WHERE co.customer_id=? AND co.status='posted'""", (cid,)):
            rows.append({"dt": r["dt"], "ref_no": r["ref_no"], "amt": r["amt"], "id": r["id"],
                         "kind": "تحصيل " + METHODS.get(r["method"], "")})
        rows.sort(key=lambda x: (x["dt"], x["kind"]))
        bal = 0.0
        for r in rows:
            bal = round(bal + r["amt"], 2)
            r["balance"] = bal
        for s in ("pending", "cleared", "bounced"):
            lst = q("SELECT * FROM cheques WHERE customer_id=? AND status=? ORDER BY due_date",
                    (cid, s))
            chq[s] = {"rows": lst, "total": round(sum(x["amount"] for x in lst), 2)}
        totals = {
            "invoices": sum(r["amt"] for r in rows if r["kind"].startswith("فاتورة")),
            "returns": -sum(r["amt"] for r in rows if r["kind"] == "مرتجع"),
            "collections": -sum(r["amt"] for r in rows if r["kind"].startswith("تحصيل")),
            "balance": bal,
            "open_balance": round(bal + chq["pending"]["total"], 2),
            "pending_cheques": chq["pending"]["total"],
        }
        if wants_export():
            return csv_response("statement", [
                ("التاريخ", "dt"), ("النوع", "kind"), ("المستند", "ref_no"),
                ("مدين", lambda r: r["amt"] if r["amt"] > 0 else ""),
                ("دائن", lambda r: -r["amt"] if r["amt"] < 0 else ""),
                ("الرصيد", "balance")], rows)
    return render_template("rep_statement.html", custs=custs, cust=cust, rows=rows,
                           totals=totals, chq=chq, f=f)


@app.route("/reports/aging")
@login_required
def rep_aging():
    f = get_filters("aging", ["q", "customer_id", "rep_id", "due_from", "due_to", "kind"])
    w = Where("inv.status='posted'", f"{OUT_EXPR} > 0")
    scope(w, "inv.rep_id")
    w.eq("inv.customer_id", f["customer_id"]).eq("inv.rep_id", f["rep_id"])
    w.date_between("inv.due_date", f["due_from"], f["due_to"])
    w.txt("inv.invoice_kind", f["kind"])
    w.search(["inv.ref_no", "c.name", "c.code"], f["q"])
    rows = q(f"""SELECT inv.ref_no, inv.invoice_kind, inv.invoice_date, inv.due_date,
                   c.name cust, r.name rep, {OUT_EXPR} outstanding,
                   CAST(julianday('now') - julianday(inv.due_date) AS INTEGER) late
                 FROM invoices inv JOIN customers c ON c.id=inv.customer_id
                 JOIN reps r ON r.id=inv.rep_id {w.sql()} ORDER BY late DESC""", w.args)
    if wants_export():
        return csv_response("aging", [
            ("الفاتورة", "ref_no"), ("النوع", lambda r: KINDS[r["invoice_kind"]]),
            ("التاريخ", "invoice_date"), ("الاستحقاق", "due_date"), ("العميل", "cust"),
            ("المندوب", "rep"), ("التأخير", "late"), ("المستحق", "outstanding")], rows)
    buckets = {"غير مستحق": 0.0, "1-30": 0.0, "31-60": 0.0, "61-90": 0.0, "أكثر من 90": 0.0}
    for r in rows:
        late = r["late"] or 0
        k = ("غير مستحق" if late <= 0 else "1-30" if late <= 30 else "31-60" if late <= 60
             else "61-90" if late <= 90 else "أكثر من 90")
        buckets[k] += r["outstanding"]
    custs, reps, its = sources()
    return render_template("rep_aging.html", rows=rows, buckets=buckets, f=f,
                           customers=custs, reps=reps)


COMM_JOIN = """FROM commission_entries ce JOIN reps r ON r.id=ce.rep_id
               JOIN invoices inv ON inv.id=ce.invoice_id
               JOIN customers c ON c.id=inv.customer_id
               LEFT JOIN allocations al ON al.id=ce.allocation_id
               LEFT JOIN collections co ON co.id=al.source_id AND al.source_type='collection'
               LEFT JOIN cheques ch ON ch.id=co.cheque_id"""


@app.route("/reports/commissions")
@login_required
def rep_commissions():
    keys = ["q", "customer_id", "rep_id", "date_from", "date_to", "method", "entry_type",
            "days_min", "days_max", "disc_min", "disc_max", "kind"]
    f = get_filters("commissions", keys)
    if not f["date_from"]:
        f["date_from"] = (date.today() - timedelta(days=365)).isoformat()
    if not f["date_to"]:
        f["date_to"] = date.today().isoformat()
    w = Where()
    scope(w, "ce.rep_id")
    w.date_between("ce.recognized_on", f["date_from"], f["date_to"])
    w.eq("ce.rep_id", f["rep_id"]).eq("inv.customer_id", f["customer_id"])
    w.txt("co.method", f["method"]).txt("inv.invoice_kind", f["kind"])
    w.gte("ce.days_taken", f["days_min"]).lte("ce.days_taken", f["days_max"])
    w.gte("ce.discount_pct", f["disc_min"]).lte("ce.discount_pct", f["disc_max"])
    if f["entry_type"] == "accrued":
        w.add("ce.status='accrued'")
    elif f["entry_type"]:
        w.txt("ce.entry_type", f["entry_type"])
    w.search(["inv.ref_no", "co.ref_no", "c.name", "c.code"], f["q"])
    rows = q(f"""SELECT ce.*, r.name rep, inv.ref_no inv_ref, inv.invoice_kind, inv.invoice_date,
                   c.name cust, co.ref_no coll_ref, co.method, co.collection_date,
                   ch.cheque_number, ch.due_date chq_due
                 {COMM_JOIN} {w.sql()} ORDER BY ce.recognized_on DESC, ce.id DESC LIMIT 800""",
             w.args)
    if wants_export():
        return csv_response("commissions", [
            ("تاريخ الاستحقاق", "recognized_on"), ("المندوب", "rep"),
            ("نوع القيد", lambda r: ENTRY_TYPES.get(r["entry_type"], "")),
            ("الحالة", lambda r: "معلّقة" if r["status"] == "accrued" else "مستحقة"),
            ("الفاتورة", "inv_ref"), ("نوع الفاتورة", lambda r: KINDS[r["invoice_kind"]]),
            ("العميل", "cust"), ("الدفعة", "coll_ref"),
            ("الوسيلة", lambda r: METHODS.get(r["method"] or "", "")),
            ("رقم الشيك", "cheque_number"), ("استحقاق الشيك", "chq_due"),
            ("قيمة العملية", "base_amount"), ("خصم %", "discount_pct"),
            ("عمولة %", "commission_pct"), ("العمولة", "commission_amt"),
            ("أيام", "days_taken"), ("بونص %", "bonus_pct"), ("البونص", "bonus_amt"),
            ("طريقة الحساب", "calc_trace")], rows)
    summary = q(f"""SELECT r.name, r.id,
                      SUM(CASE WHEN ce.entry_type='earn' AND ce.status='earned'
                               THEN ce.commission_amt+ce.bonus_amt ELSE 0 END) earned,
                      SUM(CASE WHEN ce.status='accrued'
                               THEN ce.commission_amt+ce.bonus_amt ELSE 0 END) accrued,
                      SUM(CASE WHEN ce.entry_type='reversal'
                               THEN ce.commission_amt+ce.bonus_amt ELSE 0 END) reversed,
                      SUM(CASE WHEN ce.entry_type='late_reversal'
                               THEN ce.commission_amt+ce.bonus_amt ELSE 0 END) late_rev,
                      SUM(ce.commission_amt) comm, SUM(ce.bonus_amt) bonus,
                      SUM(ce.commission_amt+ce.bonus_amt) total
                    {COMM_JOIN} {w.sql()} GROUP BY r.id ORDER BY total DESC""", w.args)
    custs, reps, its = sources()
    return render_template("rep_commissions.html", rows=rows, summary=summary, f=f,
                           customers=custs, reps=reps)


@app.route("/reports/payments")
@login_required
def rep_payments():
    """تفاصيل كل دفعة وتخصيصها على الفواتير."""
    keys = ["q", "customer_id", "rep_id", "date_from", "date_to", "method", "kind",
            "days_min", "days_max"]
    f = get_filters("payments", keys)
    days_expr = "CAST(julianday(a.alloc_date) - julianday(inv.invoice_date) AS INTEGER)"
    w = Where("a.source_type='collection'", "co.status='posted'")
    scope(w, "co.rep_id")
    w.eq("co.customer_id", f["customer_id"]).eq("co.rep_id", f["rep_id"])
    w.date_between("co.collection_date", f["date_from"], f["date_to"])
    w.txt("co.method", f["method"]).txt("inv.invoice_kind", f["kind"])
    w.gte(days_expr, f["days_min"]).lte(days_expr, f["days_max"])
    w.search(["co.ref_no", "inv.ref_no", "c.name", "ch.cheque_number"], f["q"])
    rows = q(f"""SELECT a.id, a.alloc_date, a.amount, co.ref_no coll_ref, co.method,
                   co.collection_date, co.amount payment_total, c.name cust, r.name rep,
                   inv.ref_no inv_ref, inv.invoice_kind, inv.invoice_date, inv.total inv_total,
                   {OUT_EXPR} inv_outstanding,
                   ch.cheque_number, ch.due_date chq_due, ch.status chq_status,
                   {days_expr} days_taken,
                   ce.commission_amt, ce.bonus_amt, ce.status comm_status
                 FROM allocations a
                 JOIN collections co ON co.id=a.source_id
                 JOIN invoices inv ON inv.id=a.invoice_id
                 JOIN customers c ON c.id=co.customer_id
                 JOIN reps r ON r.id=co.rep_id
                 LEFT JOIN cheques ch ON ch.id=co.cheque_id
                 LEFT JOIN commission_entries ce ON ce.allocation_id=a.id
                 {w.sql()} ORDER BY date(a.alloc_date) DESC, a.id DESC LIMIT 800""", w.args)
    if wants_export():
        return csv_response("payments", [
            ("تاريخ الدفعة", "collection_date"), ("رقم الدفعة", "coll_ref"),
            ("الوسيلة", lambda r: METHODS.get(r["method"], "")), ("قيمة الدفعة", "payment_total"),
            ("العميل", "cust"), ("المندوب", "rep"), ("الفاتورة", "inv_ref"),
            ("نوع الفاتورة", lambda r: KINDS[r["invoice_kind"]]),
            ("تاريخ الفاتورة", "invoice_date"), ("قيمة الفاتورة", "inv_total"),
            ("الجزء المسدَّد", "amount"), ("المتبقي على الفاتورة", "inv_outstanding"),
            ("مدة التحصيل", "days_taken"), ("رقم الشيك", "cheque_number"),
            ("استحقاق الشيك", "chq_due"),
            ("حالة الشيك", lambda r: CHQ_LABELS.get(r["chq_status"] or "", "")),
            ("العمولة", "commission_amt"), ("البونص", "bonus_amt")], rows)
    custs, reps, its = sources()
    totals = {"allocated": sum(r["amount"] for r in rows),
              "comm": sum((r["commission_amt"] or 0) + (r["bonus_amt"] or 0) for r in rows)}
    return render_template("rep_payments.html", rows=rows, f=f, customers=custs, reps=reps,
                           totals=totals)


@app.route("/reports/late")
@login_required
def rep_late():
    """المتأخرات: الاستحقاق، نهاية السماحية، الرصيد المتبقي، وقيمة العكس."""
    f = get_filters("late", ["q", "customer_id", "rep_id", "status"])
    rid = my_rep_id()
    out = []
    for r in E.late_scan(include_ok=True):
        inv = r["invoice"]
        if rid and inv["rep_id"] != rid:
            continue
        if f["customer_id"] and str(inv["customer_id"]) != f["customer_id"]:
            continue
        if f["rep_id"] and str(inv["rep_id"]) != f["rep_id"]:
            continue
        if f["status"] == "late" and not r["is_late"]:
            continue
        if f["status"] == "ok" and r["is_late"]:
            continue
        if f["status"] == "reversed" and r["already_reversed"] <= 0.009:
            continue
        if f["q"] and f["q"] not in (r["ref_no"] or "") and f["q"] not in (r["cust"] or ""):
            continue
        if r["outstanding"] <= 0.009 and r["already_reversed"] <= 0.009:
            continue
        row = {k: v for k, v in r.items() if k not in ("invoice", "policy")}
        row["kind"] = inv["invoice_kind"]
        out.append(row)
    out.sort(key=lambda x: -x["days_past_deadline"])
    if wants_export():
        return csv_response("late_invoices", [
            ("الفاتورة", "ref_no"), ("النوع", lambda r: KINDS[r["kind"]]), ("العميل", "cust"),
            ("المندوب", "rep"), ("تاريخ الفاتورة", "invoice_date"),
            ("تاريخ الاستحقاق", "due_date"), ("مدة السداد", "credit_days"),
            ("السماحية", "grace_days"), ("نهاية المهلة", "deadline"),
            ("الرصيد المتبقي", "outstanding"), ("سبق عكسه", "already_reversed"),
            ("قيمة العكس المستحقة", "to_reverse"),
            ("أيام بعد المهلة", "days_past_deadline")], out)
    custs, reps, its = sources()
    totals = {"outstanding": sum(r["outstanding"] for r in out if r["is_late"]),
              "reversed": sum(r["already_reversed"] for r in out),
              "pending": sum(r["to_reverse"] for r in out if r["is_late"])}
    return render_template("rep_late.html", rows=out, f=f, customers=custs, reps=reps,
                           totals=totals, statuses=LATE_STATUSES)


@app.route("/reports/stock")
@login_required
def rep_stock():
    f = get_filters("stockrep", ["item_id", "date_from", "date_to"])
    item_id = f["item_id"]
    stocks = q("SELECT * FROM v_item_stock ORDER BY name")
    running = []
    if item_id:
        w = Where("sm.item_id = ?")
        w.args.append(int(item_id))
        w.date_between("sm.move_date", f["date_from"], f["date_to"])
        moves = q(f"""SELECT sm.*, i.name FROM stock_moves sm JOIN items i ON i.id=sm.item_id
                      {w.sql()} ORDER BY date(sm.move_date), sm.id""", w.args)
        bal = 0.0
        for m in moves:
            bal += m["qty"]
            running.append({**dict(m), "balance": round(bal, 2)})
        if wants_export():
            return csv_response("item_card", [
                ("التاريخ", "move_date"), ("النوع", lambda r: MOVE_TYPES.get(r["move_type"], "")),
                ("المستند", "ref_no"), ("الكمية", "qty"), ("الرصيد", "balance"),
                ("التكلفة", "unit_cost"), ("سعر البيع", "unit_price")], running)
    total_value = round(sum(s["qty_available"] * s["cost_price"] for s in stocks), 2)
    custs, reps, its = sources()
    return render_template("rep_stock.html", stocks=stocks, moves=running, item_id=item_id,
                           total_value=total_value, f=f, items=its)


@app.cli.command("init-db")
def cli_init():
    init_db()
    print("تم إنشاء قاعدة البيانات.")


@app.cli.command("daily")
def cli_daily():
    """المهمة اليومية — للتشغيل من cron."""
    res = E.run_daily_jobs(force=True)
    commit()
    print(f"شيكات محصَّلة: {res['cheques_cleared']} — قيود عكس تأخير: {res['late_reversals']}")


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
