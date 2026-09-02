"""
منطق الحسابات المالية:
  - تخصيص التحصيلات على الفواتير بطريقة FIFO
  - عمولة المندوب حسب نسبة خصم الفاتورة
  - بونص المندوب حسب مدة التحصيل
  - قاعدة الشيكات: الاحتساب بتاريخ الاستحقاق فقط
كل القواعد تُقرأ من الإعدادات، وتُجمَّد (snapshot) على الفاتورة وقت إنشائها.
"""
import json
from datetime import date, datetime, timedelta

from db import begin_write, ex, get_setting, next_ref, q, q1, audit

MONEY = 2


def d(s):
    """تحويل نص تاريخ إلى date."""
    if isinstance(s, date):
        return s
    if isinstance(s, datetime):
        return s.date()
    return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()


def money(x):
    return round(float(x or 0) + 0.0, MONEY)


def today_str():
    return date.today().isoformat()


# ============================================================
#  الإعدادات الافتراضية
# ============================================================
DEFAULT_COMMISSION_RULES = [
    # العمولة تُحدَّد بنسبة خصم الفاتورة
    {"label": "خصم حتى 20%", "min_discount": 0.0, "max_discount": 20.0, "commission_pct": 2.0},
    {"label": "خصم 25% فأكثر", "min_discount": 20.001, "max_discount": 100.0, "commission_pct": 1.0},
]

DEFAULT_BONUS_TIERS = [
    {"label": "تحصيل فوري / نفس اليوم", "up_to_days": 0, "bonus_pct": 3.0},
    {"label": "من 1 إلى 7 أيام", "up_to_days": 7, "bonus_pct": 3.0},
    {"label": "من 8 إلى 15 يوم", "up_to_days": 15, "bonus_pct": 2.5},
    {"label": "حتى 30 يوم", "up_to_days": 30, "bonus_pct": 2.0},
    {"label": "حتى 45 يوم", "up_to_days": 45, "bonus_pct": 1.5},
    {"label": "حتى 60 يوم", "up_to_days": 60, "bonus_pct": 1.0},
    {"label": "حتى 75 يوم", "up_to_days": 75, "bonus_pct": 0.5},
    {"label": "أكثر من 75 يوم", "up_to_days": 99999, "bonus_pct": 0.0},
]

# نقطة البداية لحساب مدة التحصيل حسب وسيلة الدفع
DEFAULT_BONUS_BASIS = {
    "cash": "invoice_date",
    "credit": "invoice_date",
    "cheque": "invoice_date",
    "transfer": "invoice_date",
}

DEFAULT_STOCK_POLICY = {"block_oversell": True, "admin_can_override": True}

# عكس العمولة والبونص عند تأخر التحصيل
DEFAULT_LATE_POLICY = {
    "enabled": True,
    "grace_days": 15,               # سماحية افتراضية بعد مدة السداد
    "bonus_basis": "tier_at_deadline",   # tier_at_deadline | best_tier | none
    "restore_on_collection": False,      # هل تُعاد العمولة لو حُصّل المتأخر لاحقاً
}


def load_rules():
    return {
        "commission_rules": get_setting("commission_rules", DEFAULT_COMMISSION_RULES),
        "bonus_tiers": get_setting("bonus_tiers", DEFAULT_BONUS_TIERS),
        "bonus_basis": get_setting("bonus_basis", DEFAULT_BONUS_BASIS),
        "late_policy": get_setting("late_policy", DEFAULT_LATE_POLICY),
    }


def seed_default_settings():
    from db import set_setting
    if get_setting("commission_rules") is None:
        set_setting("commission_rules", DEFAULT_COMMISSION_RULES)
    if get_setting("bonus_tiers") is None:
        set_setting("bonus_tiers", DEFAULT_BONUS_TIERS)
    if get_setting("bonus_basis") is None:
        set_setting("bonus_basis", DEFAULT_BONUS_BASIS)
    if get_setting("stock_policy") is None:
        set_setting("stock_policy", DEFAULT_STOCK_POLICY)
    if get_setting("late_policy") is None:
        set_setting("late_policy", DEFAULT_LATE_POLICY)


# ============================================================
#  بحث القواعد
# ============================================================
def commission_pct_for(discount_pct, rules):
    dp = float(discount_pct or 0)
    for r in sorted(rules, key=lambda x: float(x["min_discount"])):
        if float(r["min_discount"]) <= dp <= float(r["max_discount"]) + 1e-9:
            return float(r["commission_pct"]), r.get("label", "")
    return 0.0, "لا توجد قاعدة مطابقة"


def bonus_pct_for(days, tiers):
    n = max(0, int(days))
    for t in sorted(tiers, key=lambda x: int(x["up_to_days"])):
        if n <= int(t["up_to_days"]):
            return float(t["bonus_pct"]), t.get("label", "")
    return 0.0, "خارج الشرائح"


# ============================================================
#  حساب العمولة والبونص لعملية تحصيل مخصصة على فاتورة
# ============================================================
def compute_commission(invoice, base_amount, method, collection_date, cheque_due_date=None):
    """
    يرجع dict بكل تفاصيل الحساب.
    قاعدة الشيكات: التاريخ المرجعي النهائي هو تاريخ استحقاق الشيك وليس تاريخ التحصيل.
    """
    snap = json.loads(invoice["rules_snapshot"])
    rules = snap.get("commission_rules", DEFAULT_COMMISSION_RULES)
    tiers = snap.get("bonus_tiers", DEFAULT_BONUS_TIERS)
    basis_cfg = snap.get("bonus_basis", DEFAULT_BONUS_BASIS)

    anchor_field = basis_cfg.get(method, "invoice_date")
    basis_from = d(invoice["invoice_date"] if anchor_field == "invoice_date" else invoice["due_date"])
    anchor_label = "تاريخ الفاتورة" if anchor_field == "invoice_date" else "تاريخ الاستحقاق"

    if method == "cheque":
        if not cheque_due_date:
            raise ValueError("الشيك بدون تاريخ استحقاق")
        basis_to = d(cheque_due_date)
        to_label = "تاريخ استحقاق الشيك"
        recognized_on = basis_to.isoformat()
    else:
        basis_to = d(collection_date)
        to_label = "تاريخ التحصيل"
        recognized_on = basis_to.isoformat()

    days = (basis_to - basis_from).days
    days = max(0, days)

    c_pct, c_label = commission_pct_for(invoice["discount_pct"], rules)
    b_pct, b_label = bonus_pct_for(days, tiers)

    c_amt = money(base_amount * c_pct / 100.0)
    b_amt = money(base_amount * b_pct / 100.0)

    trace = (
        f"الأساس {money(base_amount):,.2f} | خصم الفاتورة {invoice['discount_pct']}% ← عمولة {c_pct}% ({c_label}) = {c_amt:,.2f}"
        f" ‖ المدة {days} يوم من {anchor_label} ({basis_from}) إلى {to_label} ({basis_to}) ← بونص {b_pct}% ({b_label}) = {b_amt:,.2f}"
    )
    if method == "cheque":
        trace += " ‖ شيك: الاحتساب بتاريخ الاستحقاق وليس تاريخ التحصيل الفعلي."

    return {
        "base_amount": money(base_amount),
        "discount_pct": float(invoice["discount_pct"]),
        "commission_pct": c_pct,
        "commission_amt": c_amt,
        "days_taken": days,
        "bonus_pct": b_pct,
        "bonus_amt": b_amt,
        "basis_from": basis_from.isoformat(),
        "basis_to": basis_to.isoformat(),
        "basis_label": f"{anchor_label} ← {to_label}",
        "recognized_on": recognized_on,
        "status": "earned" if d(recognized_on) <= date.today() else "accrued",
        "calc_trace": trace,
    }


# ============================================================
#  المخزون
# ============================================================
def item_qty(item_id):
    r = q1("SELECT COALESCE(SUM(qty),0) AS s FROM stock_moves WHERE item_id = ?", (item_id,))
    return float(r["s"] or 0)


def add_stock_move(move_date, item_id, move_type, qty, unit_cost=0, unit_price=0,
                   ref_type=None, ref_id=None, ref_no=None, notes=None, user_id=None):
    begin_write()   # قفل كتابة حصري: يمنع كاتبَين متزامنَين على نفس الأرصدة
    ex("""INSERT INTO stock_moves(move_date,item_id,move_type,qty,unit_cost,unit_price,
                                  ref_type,ref_id,ref_no,notes,created_by)
          VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
       (move_date, item_id, move_type, qty, unit_cost, unit_price,
        ref_type, ref_id, ref_no, notes, user_id))


# ============================================================
#  تخصيص FIFO
# ============================================================
def open_invoices(customer_id):
    """الفواتير المستحقة مرتبة من الأقدم (FIFO)."""
    return q("""SELECT * FROM invoices
                WHERE customer_id = ? AND status = 'posted'
                  AND ROUND(total - returned_total - collected_total, 2) > 0
                ORDER BY date(invoice_date) ASC, id ASC""", (customer_id,))


def outstanding_of(inv):
    return money(inv["total"] - inv["returned_total"] - inv["collected_total"])


def preview_fifo(customer_id, amount):
    """معاينة توزيع مبلغ على الفواتير قبل الحفظ."""
    rest = money(amount)
    out = []
    for inv in open_invoices(customer_id):
        if rest <= 0:
            break
        o = outstanding_of(inv)
        take = min(o, rest)
        if take <= 0:
            continue
        out.append({"invoice_id": inv["id"], "ref_no": inv["ref_no"],
                    "invoice_date": inv["invoice_date"], "outstanding": o,
                    "amount": money(take)})
        rest = money(rest - take)
    return out, money(rest)


# ============================================================
#  ترحيل الفاتورة
# ============================================================
def create_invoice(data, lines, user, override_stock=False):
    """
    data: customer_id, invoice_date, discount_pct, credit_days, payment_method, notes,
          is_historical, invoice_kind ('detailed' | 'aggregate'), external_ref
          + للفاتورة الإجمالية: total_amount (القيمة النهائية بعد الخصم)
    lines: [{item_id, qty, unit_price}]  — تُتجاهل في الفاتورة الإجمالية
    """
    begin_write()   # قفل كتابة حصري: يمنع كاتبَين متزامنَين على نفس الأرصدة
    if (data.get("invoice_kind") or "detailed") == "aggregate":
        return create_aggregate_invoice(data, user)

    cust = q1("SELECT * FROM customers WHERE id = ?", (data["customer_id"],))
    if not cust:
        raise ValueError("العميل غير موجود")
    rep_id = data.get("rep_id") or cust["rep_id"]
    if not rep_id:
        raise ValueError("لا يوجد مندوب مسؤول عن العميل")

    policy = get_setting("stock_policy", DEFAULT_STOCK_POLICY)
    subtotal = 0.0
    prepared = []
    for ln in lines:
        item = q1("SELECT * FROM items WHERE id = ?", (ln["item_id"],))
        if not item:
            raise ValueError("صنف غير موجود")
        qty = float(ln["qty"])
        if qty <= 0:
            continue
        price = float(ln.get("unit_price") or item["sale_price"])
        avail = item_qty(item["id"])
        if policy.get("block_oversell", True) and qty > avail and not override_stock:
            raise ValueError(
                f"الكمية المطلوبة من «{item['name']}» ({qty:g}) أكبر من المتاح ({avail:g}). "
                "يحتاج تجاوز المخزون صلاحية مدير.")
        lt = money(qty * price)
        subtotal += lt
        prepared.append((item, qty, price, lt))

    if not prepared:
        raise ValueError("لا توجد بنود في الفاتورة")

    disc = float(data.get("discount_pct") if data.get("discount_pct") not in (None, "")
                 else cust["default_discount_pct"])
    days = int(data.get("credit_days") if data.get("credit_days") not in (None, "")
               else cust["default_credit_days"])
    method = data.get("payment_method") or cust["default_payment_method"]

    subtotal = money(subtotal)
    disc_amt = money(subtotal * disc / 100.0)
    total = money(subtotal - disc_amt)
    inv_date = data["invoice_date"]
    due = (d(inv_date) + timedelta(days=days)).isoformat()

    snapshot = load_rules()
    ref = next_ref("invoice", d(inv_date).year)

    cur = ex("""INSERT INTO invoices(ref_no,invoice_date,customer_id,rep_id,invoice_kind,external_ref,
                    discount_pct,credit_days,payment_method,due_date,subtotal,discount_amount,total,
                    status,is_historical,rules_snapshot,notes,created_by,posted_at)
                VALUES (?,?,?,?,'detailed',?,?,?,?,?,?,?,?,'posted',?,?,?,?,datetime('now'))""",
             (ref, inv_date, cust["id"], rep_id, data.get("external_ref") or None,
              disc, days, method, due,
              subtotal, disc_amt, total, 1 if data.get("is_historical") else 0,
              json.dumps(snapshot, ensure_ascii=False), data.get("notes"), user["id"]))
    inv_id = cur.lastrowid

    for item, qty, price, lt in prepared:
        ex("""INSERT INTO invoice_lines(invoice_id,item_id,qty,unit_price,unit_cost,line_total)
              VALUES (?,?,?,?,?,?)""", (inv_id, item["id"], qty, price, item["cost_price"], lt))
        add_stock_move(inv_date, item["id"], "sale", -qty, item["cost_price"], price,
                       "invoice", inv_id, ref, None, user["id"])

    audit("create", "invoice", inv_id, ref,
          {"kind": "detailed", "total": total, "discount_pct": disc,
           "override_stock": bool(override_stock)})
    return inv_id, ref


def create_aggregate_invoice(data, user):
    """
    فاتورة تاريخية إجمالية: قيمة نهائية بدون أصناف ولا حركة مخزون.
    تدخل في رصيد العميل و FIFO والعمولة والبونص وتقارير المبيعات بالقيمة،
    ولا تظهر في تقارير مبيعات الأصناف.
    """
    begin_write()   # قفل كتابة حصري: يمنع كاتبَين متزامنَين على نفس الأرصدة
    cust = q1("SELECT * FROM customers WHERE id = ?", (data["customer_id"],))
    if not cust:
        raise ValueError("العميل غير موجود")
    rep_id = data.get("rep_id") or cust["rep_id"]
    if not rep_id:
        raise ValueError("لا يوجد مندوب مسؤول عن العميل")

    total = money(data.get("total_amount"))
    if total <= 0:
        raise ValueError("القيمة الإجمالية للفاتورة يجب أن تكون أكبر من صفر")

    disc = float(data.get("discount_pct") if data.get("discount_pct") not in (None, "")
                 else cust["default_discount_pct"])
    if disc >= 100:
        raise ValueError("نسبة الخصم يجب أن تكون أقل من 100%")
    days = int(data.get("credit_days") if data.get("credit_days") not in (None, "")
               else cust["default_credit_days"])
    method = data.get("payment_method") or cust["default_payment_method"]
    inv_date = data["invoice_date"]
    due = data.get("due_date") or (d(inv_date) + timedelta(days=days)).isoformat()

    # القيمة المدخلة نهائية بعد الخصم؛ نستنتج ما قبل الخصم للاتساق مع باقي النظام
    subtotal = money(total / (1 - disc / 100.0)) if disc else total
    disc_amt = money(subtotal - total)

    snapshot = load_rules()
    ref = next_ref("invoice", d(inv_date).year)
    cur = ex("""INSERT INTO invoices(ref_no,invoice_date,customer_id,rep_id,invoice_kind,external_ref,
                    discount_pct,credit_days,payment_method,due_date,subtotal,discount_amount,total,
                    status,is_historical,rules_snapshot,notes,created_by,posted_at)
                VALUES (?,?,?,?,'aggregate',?,?,?,?,?,?,?,?,'posted',1,?,?,?,datetime('now'))""",
             (ref, inv_date, cust["id"], rep_id, data.get("external_ref") or None,
              disc, days, method, due, subtotal, disc_amt, total,
              json.dumps(snapshot, ensure_ascii=False), data.get("notes"), user["id"]))
    inv_id = cur.lastrowid
    audit("create", "invoice", inv_id, ref,
          {"kind": "aggregate", "total": total, "discount_pct": disc,
           "external_ref": data.get("external_ref")})
    return inv_id, ref


# ============================================================
#  ترحيل المرتجع
# ============================================================
def create_return(data, lines, user):
    begin_write()   # قفل كتابة حصري: يمنع كاتبَين متزامنَين على نفس الأرصدة
    inv = q1("SELECT * FROM invoices WHERE id = ?", (data["invoice_id"],))
    if not inv:
        raise ValueError("الفاتورة الأصلية غير موجودة")
    if inv["status"] != "posted":
        raise ValueError("لا يمكن الارتجاع من فاتورة غير مرحّلة")
    if inv["invoice_kind"] == "aggregate":
        raise ValueError("الفاتورة التاريخية الإجمالية بلا أصناف، فلا يمكن عمل مرتجع أصناف عليها. "
                         "استخدم فاتورة إجمالية بقيمة سالبة أو سجّل التسوية يدوياً.")

    ret_date = data["return_date"]
    if d(ret_date) < d(inv["invoice_date"]):
        raise ValueError("تاريخ المرتجع أقدم من تاريخ الفاتورة")

    subtotal = 0.0
    prepared = []
    for ln in lines:
        qty = float(ln["qty"] or 0)
        if qty <= 0:
            continue
        il = q1("""SELECT il.*, i.name FROM invoice_lines il JOIN items i ON i.id = il.item_id
                   WHERE il.invoice_id = ? AND il.item_id = ?""", (inv["id"], ln["item_id"]))
        if not il:
            raise ValueError("الصنف غير موجود في الفاتورة الأصلية")
        already = q1("""SELECT COALESCE(SUM(rl.qty),0) AS s FROM return_lines rl
                        JOIN returns r ON r.id = rl.return_id
                        WHERE r.invoice_id = ? AND rl.item_id = ? AND r.status='posted'""",
                     (inv["id"], ln["item_id"]))["s"]
        if qty + float(already) > float(il["qty"]) + 1e-9:
            raise ValueError(f"كمية المرتجع من «{il['name']}» أكبر من المباع (المتبقي {float(il['qty'])-float(already):g})")
        lt = money(qty * il["unit_price"])
        subtotal += lt
        prepared.append((il, qty, il["unit_price"], lt))

    if not prepared:
        raise ValueError("لا توجد بنود في المرتجع")

    subtotal = money(subtotal)
    total = money(subtotal * (1 - float(inv["discount_pct"]) / 100.0))  # نفس خصم الفاتورة
    ref = next_ref("return", d(ret_date).year)

    cur = ex("""INSERT INTO returns(ref_no,return_date,invoice_id,customer_id,rep_id,
                    subtotal,discount_pct,total,status,notes,created_by)
                VALUES (?,?,?,?,?,?,?,?,'posted',?,?)""",
             (ref, ret_date, inv["id"], inv["customer_id"], inv["rep_id"],
              subtotal, inv["discount_pct"], total, data.get("notes"), user["id"]))
    ret_id = cur.lastrowid

    for il, qty, price, lt in prepared:
        ex("""INSERT INTO return_lines(return_id,item_id,qty,unit_price,unit_cost,line_total)
              VALUES (?,?,?,?,?,?)""", (ret_id, il["item_id"], qty, price, il["unit_cost"], lt))
        add_stock_move(ret_date, il["item_id"], "return_in", qty, il["unit_cost"], price,
                       "return", ret_id, ref, None, user["id"])

    # الأثر المالي على الفاتورة
    cur = ex("""UPDATE invoices SET returned_total = ROUND(returned_total + ?, 2)
                WHERE id = ?
                  AND ROUND(total - returned_total - collected_total, 2) >= -0.011""",
       (total, inv["id"]))
    ex("""INSERT INTO allocations(source_type,source_id,invoice_id,amount,alloc_date)
          VALUES ('return',?,?,?,?)""", (ret_id, inv["id"], total, ret_date))

    _reverse_commission_if_overcollected(inv["id"], ret_id, ret_date)

    audit("create", "return", ret_id, ref, {"invoice": inv["ref_no"], "total": total})
    return ret_id, ref


def _reverse_commission_if_overcollected(invoice_id, ret_id, ret_date):
    """لو المرتجع خلّى المحصَّل أكبر من صافي الفاتورة، تُعكَس العمولة على الفرق."""
    inv = q1("SELECT * FROM invoices WHERE id = ?", (invoice_id,))
    net = money(inv["total"] - inv["returned_total"])
    over = money(inv["collected_total"] - net)
    if over <= 0.009:
        return
    rows = q("""SELECT * FROM commission_entries
                WHERE invoice_id = ? ORDER BY id DESC""", (invoice_id,))
    base_sum = sum(r["base_amount"] for r in rows) or 0
    if base_sum <= 0:
        return
    c_sum = sum(r["commission_amt"] for r in rows)
    b_sum = sum(r["bonus_amt"] for r in rows)
    ratio = min(1.0, over / base_sum)
    c_rev = money(-c_sum * ratio)
    b_rev = money(-b_sum * ratio)
    if abs(c_rev) < 0.005 and abs(b_rev) < 0.005:
        return
    ex("""INSERT INTO commission_entries(invoice_id,rep_id,entry_type,base_amount,discount_pct,
              commission_pct,commission_amt,days_taken,bonus_pct,bonus_amt,
              basis_from,basis_to,basis_label,recognized_on,status,calc_trace)
          VALUES (?,?,'reversal',?,?,?,?,?,?,?,?,?,?,?,'earned',?)""",
       (invoice_id, inv["rep_id"], money(-over), inv["discount_pct"],
        rows[0]["commission_pct"], c_rev, rows[0]["days_taken"], rows[0]["bonus_pct"], b_rev,
        inv["invoice_date"], ret_date, "عكس عمولة بسبب مرتجع", ret_date,
        f"مرتجع بعد التحصيل: عكس نسبي على {money(over):,.2f} من قيمة العملية."))


# ============================================================
#  التحصيلات
# ============================================================
def create_collection(data, user):
    """
    data: customer_id, collection_date, method, amount, notes
          + للشيك: cheque_number, bank_name, cheque_due_date
    """
    begin_write()   # قفل كتابة حصري: يمنع كاتبَين متزامنَين على نفس الأرصدة
    cust = q1("SELECT * FROM customers WHERE id = ?", (data["customer_id"],))
    if not cust:
        raise ValueError("العميل غير موجود")
    amount = money(data["amount"])
    if amount <= 0:
        raise ValueError("قيمة التحصيل يجب أن تكون أكبر من صفر")
    method = data["method"]
    coll_date = data["collection_date"]
    rep_id = data.get("rep_id") or cust["rep_id"]

    cheque_id = None
    cheque_due = None
    if method == "cheque":
        cheque_due = data.get("cheque_due_date")
        if not cheque_due:
            raise ValueError("تاريخ استحقاق الشيك مطلوب — عليه يُحسب البونص والعمولة")
        cref = next_ref("cheque", d(coll_date).year)
        cur = ex("""INSERT INTO cheques(ref_no,cheque_number,bank_name,customer_id,amount,
                        received_date,due_date,status,notes,created_by)
                    VALUES (?,?,?,?,?,?,?, 'pending', ?, ?)""",
                 (cref, data.get("cheque_number"), data.get("bank_name"), cust["id"], amount,
                  coll_date, cheque_due, data.get("notes"), user["id"]))
        cheque_id = cur.lastrowid
        audit("receive", "cheque", cheque_id, cref,
              {"amount": amount, "due_date": cheque_due, "status": "pending"}, user=user)

    ref = next_ref("collection", d(coll_date).year)
    cur = ex("""INSERT INTO collections(ref_no,collection_date,customer_id,rep_id,method,amount,
                    allocated_total,cheque_id,status,notes,created_by)
                VALUES (?,?,?,?,?,?,0,?, 'posted', ?, ?)""",
             (ref, coll_date, cust["id"], rep_id, method, amount, cheque_id,
              data.get("notes"), user["id"]))
    coll_id = cur.lastrowid

    # --- FIFO ---
    allocated = 0.0
    for inv in open_invoices(cust["id"]):
        rest = money(amount - allocated)
        if rest <= 0:
            break
        take = money(min(outstanding_of(inv), rest))
        if take <= 0:
            continue
        acur = ex("""INSERT INTO allocations(source_type,source_id,invoice_id,amount,alloc_date)
                     VALUES ('collection',?,?,?,?)""", (coll_id, inv["id"], take, coll_date))
        alloc_id = acur.lastrowid
        # تحديث شرطي: يفشل إن تغيّر المتبقي بين القراءة والكتابة بدل أن يكتب فوقه
        cur = ex("""UPDATE invoices SET collected_total = ROUND(collected_total + ?, 2)
                    WHERE id = ?
                      AND ROUND(total - returned_total - collected_total, 2) >= ?""",
                 (take, inv["id"], take))
        if cur.rowcount != 1:
            raise ValueError(
                f"تغيّر رصيد الفاتورة {inv['ref_no']} أثناء الحفظ — أُلغيت العملية بالكامل. "
                "أعد تحميل الصفحة وحاول مرة أخرى.")

        # الشيك يخصم من الرصيد فوراً، لكن لا عمولة ولا بونص إلا عند تاريخ الاستحقاق
        if method != "cheque":
            calc = compute_commission(inv, take, method, coll_date, None)
            write_commission(alloc_id, inv, calc, "earn")
        allocated = money(allocated + take)

    ex("UPDATE collections SET allocated_total = ? WHERE id = ?", (allocated, coll_id))
    audit("create", "collection", coll_id, ref,
          {"amount": amount, "allocated": allocated, "method": method,
           "cheque_due": cheque_due}, user=user)
    return coll_id, ref, allocated, money(amount - allocated)


def write_commission(alloc_id, inv, calc, entry_type="earn"):
    return ex("""INSERT INTO commission_entries(allocation_id,invoice_id,rep_id,entry_type,base_amount,
                     discount_pct,commission_pct,commission_amt,days_taken,bonus_pct,bonus_amt,
                     basis_from,basis_to,basis_label,recognized_on,status,calc_trace)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (alloc_id, inv["id"], inv["rep_id"], entry_type, calc["base_amount"],
               calc["discount_pct"], calc["commission_pct"], calc["commission_amt"],
               calc["days_taken"], calc["bonus_pct"], calc["bonus_amt"], calc["basis_from"],
               calc["basis_to"], calc["basis_label"], calc["recognized_on"], calc["status"],
               calc["calc_trace"])).lastrowid


def clear_cheque(cheque_id, user, when=None, auto=False):
    """
    تحويل الشيك إلى «تم التحصيل» واحتساب العمولة والبونص وقتها،
    محسوبة على تاريخ استحقاق الشيك لا على تاريخ التحصيل الفعلي.
    """
    begin_write()   # قفل كتابة حصري: يمنع كاتبَين متزامنَين على نفس الأرصدة
    ch = q1("SELECT * FROM cheques WHERE id = ?", (cheque_id,))
    if not ch:
        raise ValueError("الشيك غير موجود")
    if ch["status"] != "pending":
        raise ValueError("لا يمكن تحصيل شيك حالته ليست «معلّق»")
    when = when or ch["due_date"]
    coll = q1("SELECT * FROM collections WHERE cheque_id = ?", (cheque_id,))
    n = 0
    if coll:
        for a in q("""SELECT * FROM allocations
                      WHERE source_type='collection' AND source_id=?""", (coll["id"],)):
            if q1("SELECT 1 FROM commission_entries WHERE allocation_id=?", (a["id"],)):
                continue
            inv = q1("SELECT * FROM invoices WHERE id=?", (a["invoice_id"],))
            calc = compute_commission(inv, a["amount"], "cheque",
                                      coll["collection_date"], ch["due_date"])
            write_commission(a["id"], inv, calc, "earn")
            n += 1
    cur = ex("""UPDATE cheques SET status='cleared', cleared_date=?, auto_cleared=?
                WHERE id=? AND status='pending'""",
             (when, 1 if auto else 0, cheque_id))
    if cur.rowcount != 1:
        raise ValueError("تغيّرت حالة الشيك أثناء الحفظ — أُلغيت العملية.")
    audit("clear", "cheque", cheque_id, ch["ref_no"],
          {"cleared_date": when, "auto": bool(auto), "commission_entries": n}, user=user)
    return n


def bounce_cheque(cheque_id, user, when=None, reason=None):
    """
    ارتداد شيك: إعادة قيمته لرصيد العميل، وعكس العمولة والبونص إن كانا احتُسبا،
    مع حفظ تاريخ الارتداد وسببه.
    """
    begin_write()   # قفل كتابة حصري: يمنع كاتبَين متزامنَين على نفس الأرصدة
    ch = q1("SELECT * FROM cheques WHERE id = ?", (cheque_id,))
    if not ch:
        raise ValueError("الشيك غير موجود")
    if ch["status"] == "bounced":
        raise ValueError("الشيك مسجَّل كمرتد بالفعل")
    when = when or today_str()
    coll = q1("SELECT * FROM collections WHERE cheque_id = ?", (cheque_id,))
    if coll:
        for a in q("""SELECT * FROM allocations
                      WHERE source_type='collection' AND source_id=?""", (coll["id"],)):
            # إعادة القيمة إلى رصيد العميل عبر الفاتورة
            ex("UPDATE invoices SET collected_total = ROUND(collected_total - ?,2) WHERE id=?",
               (a["amount"], a["invoice_id"]))
            ce = q1("SELECT * FROM commission_entries WHERE allocation_id=? AND entry_type='earn'",
                    (a["id"],))
            if ce:
                ex("""INSERT INTO commission_entries(invoice_id,rep_id,entry_type,base_amount,
                          discount_pct,commission_pct,commission_amt,days_taken,bonus_pct,bonus_amt,
                          basis_from,basis_to,basis_label,recognized_on,status,calc_trace)
                      VALUES (?,?,'reversal',?,?,?,?,?,?,?,?,?,?,?,'earned',?)""",
                   (a["invoice_id"], ce["rep_id"], -ce["base_amount"], ce["discount_pct"],
                    ce["commission_pct"], -ce["commission_amt"], ce["days_taken"],
                    ce["bonus_pct"], -ce["bonus_amt"], ce["basis_from"], when,
                    "عكس بسبب ارتداد شيك", when,
                    f"ارتد الشيك {ch['ref_no']} بتاريخ {when}"
                    + (f" — السبب: {reason}" if reason else "")
                    + " فتم عكس العمولة والبونص بالكامل."))
            ex("UPDATE commission_entries SET allocation_id=NULL WHERE allocation_id=?", (a["id"],))
            ex("DELETE FROM allocations WHERE id=?", (a["id"],))
        ex("UPDATE collections SET status='void', allocated_total=0 WHERE id=?", (coll["id"],))
    cur = ex("""UPDATE cheques SET status='bounced', bounce_date=?, bounce_reason=?,
                    cleared_date=NULL
                WHERE id=? AND status IN ('pending','cleared')""",
             (when, reason, cheque_id))
    if cur.rowcount != 1:
        raise ValueError("تغيّرت حالة الشيك أثناء الحفظ — أُلغيت العملية.")
    audit("bounce", "cheque", cheque_id, ch["ref_no"],
          {"bounce_date": when, "amount": ch["amount"]}, reason=reason, user=user)


def set_cheque_status(cheque_id, status, user, when=None, reason=None):
    if status == "cleared":
        return clear_cheque(cheque_id, user, when)
    if status == "bounced":
        return bounce_cheque(cheque_id, user, when, reason)
    raise ValueError("حالة غير معروفة")


def settle_due_cheques(user=None, as_of=None):
    """
    فحص يومي: كل شيك معلّق حلّ أجله يتحول تلقائياً إلى «تم التحصيل»
    وتُحتسب عمولته وبونصه على تاريخ الاستحقاق، ما لم يسجّله المستخدم كمرتد.
    """
    as_of = as_of or today_str()
    user = user or {"id": None, "full_name": "النظام (فحص يومي)"}
    done = 0
    for ch in q("""SELECT * FROM cheques WHERE status='pending' AND date(due_date) <= date(?)
                   ORDER BY due_date""", (as_of,)):
        clear_cheque(ch["id"], user, ch["due_date"], auto=True)
        done += 1
    return done


# ============================================================
#  عكس العمولة والبونص بسبب تأخر التحصيل
# ============================================================
def grace_days_for(cust_row, policy):
    g = cust_row["grace_days"] if cust_row and cust_row["grace_days"] is not None else None
    return int(g if g is not None else policy.get("grace_days", 15))


def invoice_deadline(inv, cust, policy):
    """نهاية مدة السداد + السماحية."""
    return d(inv["invoice_date"]) + timedelta(days=int(inv["credit_days"])
                                              + grace_days_for(cust, policy))


def late_scan(as_of=None, include_ok=False):
    """
    يرجع قائمة بالفواتير المتأخرة مع تاريخ نهاية السماحية والرصيد المتبقي
    وقيمة العكس المستحقة (بدون تنفيذ) — تُستخدم في التقرير وفي التنفيذ.
    """
    as_of = d(as_of or today_str())
    rows = []
    for inv in q("""SELECT inv.*, c.name cust, c.grace_days cust_grace, r.name rep
                    FROM invoices inv JOIN customers c ON c.id=inv.customer_id
                    JOIN reps r ON r.id=inv.rep_id
                    WHERE inv.status='posted'"""):
        snap = json.loads(inv["rules_snapshot"])
        policy = snap.get("late_policy", get_setting("late_policy", DEFAULT_LATE_POLICY))
        grace = int(inv["cust_grace"] if inv["cust_grace"] is not None
                    else policy.get("grace_days", 15))
        deadline = d(inv["invoice_date"]) + timedelta(days=int(inv["credit_days"]) + grace)
        outstanding = money(inv["total"] - inv["returned_total"] - inv["collected_total"])
        pending = money(outstanding - inv["late_reversed"])
        late = as_of > deadline
        if not late and not include_ok:
            continue
        rows.append({
            "invoice": inv, "ref_no": inv["ref_no"], "cust": inv["cust"], "rep": inv["rep"],
            "invoice_date": inv["invoice_date"], "due_date": inv["due_date"],
            "credit_days": inv["credit_days"], "grace_days": grace,
            "deadline": deadline.isoformat(), "outstanding": outstanding,
            "already_reversed": money(inv["late_reversed"]),
            "to_reverse": max(0.0, pending), "is_late": late,
            "days_past_deadline": (as_of - deadline).days,
            "policy": policy,
        })
    return rows


def run_late_reversals(user=None, as_of=None):
    """
    ينشئ قيداً سالباً للعمولة والبونص على الرصيد غير المحصَّل بعد نهاية السماحية فقط.
    آمن للتكرار: يعكس الفرق الجديد فقط ولا يكرر القيد على نفس الرصيد.
    """
    as_of = as_of or today_str()
    user = user or {"id": None, "full_name": "النظام (فحص يومي)"}
    made = 0
    for row in late_scan(as_of):
        policy = row["policy"]
        if not policy.get("enabled", True):
            continue
        inv = row["invoice"]
        snap = json.loads(inv["rules_snapshot"])
        amount = row["to_reverse"]

        if amount <= 0.009:
            # حُصِّل جزء بعد العكس — إعادة العمولة اختيارية
            recovered = money(inv["late_reversed"] - row["outstanding"])
            if recovered > 0.009 and policy.get("restore_on_collection", False):
                _restore_late_reversal(inv, snap, recovered, as_of, user)
                made += 1
            continue

        c_pct, c_label = commission_pct_for(inv["discount_pct"], snap["commission_rules"])
        basis = policy.get("bonus_basis", "tier_at_deadline")
        if basis == "none":
            b_pct, b_label = 0.0, "بدون بونص"
        elif basis == "best_tier":
            b_pct = max(float(t["bonus_pct"]) for t in snap["bonus_tiers"])
            b_label = "أعلى شريحة"
        else:
            days = int(inv["credit_days"]) + row["grace_days"]
            b_pct, b_label = bonus_pct_for(days, snap["bonus_tiers"])

        c_amt = money(-amount * c_pct / 100.0)
        b_amt = money(-amount * b_pct / 100.0)
        trace = (f"تأخر التحصيل: مدة السداد {inv['credit_days']} يوم + سماحية {row['grace_days']} يوم "
                 f"= نهاية المهلة {row['deadline']}. الرصيد غير المحصَّل {amount:,.2f} "
                 f"← عكس عمولة {c_pct}% ({c_label}) = {c_amt:,.2f} وبونص {b_pct}% ({b_label}) = {b_amt:,.2f}.")
        ex("""INSERT INTO commission_entries(invoice_id,rep_id,entry_type,base_amount,discount_pct,
                  commission_pct,commission_amt,days_taken,bonus_pct,bonus_amt,
                  basis_from,basis_to,basis_label,recognized_on,status,calc_trace)
              VALUES (?,?,'late_reversal',?,?,?,?,?,?,?,?,?,?,?,'earned',?)""",
           (inv["id"], inv["rep_id"], money(-amount), inv["discount_pct"], c_pct, c_amt,
            row["days_past_deadline"], b_pct, b_amt, inv["invoice_date"], row["deadline"],
            "عكس عمولة وبونص بسبب تأخر التحصيل", as_of, trace))
        cur = ex("""UPDATE invoices SET late_reversed = ROUND(late_reversed + ?,2)
                    WHERE id = ? AND ROUND(late_reversed,2) = ?""",
                 (amount, inv["id"], round(inv["late_reversed"], 2)))
        if cur.rowcount != 1:
            raise ValueError(f"عُكست عمولة الفاتورة {inv['ref_no']} في نفس اللحظة من مسار آخر.")
        audit("late_reversal", "invoice", inv["id"], inv["ref_no"],
              {"amount": amount, "commission": c_amt, "bonus": b_amt,
               "deadline": row["deadline"]}, user=user)
        made += 1
    return made


def _restore_late_reversal(inv, snap, recovered, as_of, user):
    """إعادة العمولة المعكوسة عند تحصيل المتأخر — مفعّلة فقط لو الإعداد يسمح."""
    c_pct, _ = commission_pct_for(inv["discount_pct"], snap["commission_rules"])
    policy = snap.get("late_policy", DEFAULT_LATE_POLICY)
    days = int(inv["credit_days"]) + int(policy.get("grace_days", 15))
    b_pct, _ = bonus_pct_for(days, snap["bonus_tiers"])
    ex("""INSERT INTO commission_entries(invoice_id,rep_id,entry_type,base_amount,discount_pct,
              commission_pct,commission_amt,days_taken,bonus_pct,bonus_amt,
              basis_from,basis_to,basis_label,recognized_on,status,calc_trace)
          VALUES (?,?,'late_reversal',?,?,?,?,?,?,?,?,?,?,?,'earned',?)""",
       (inv["id"], inv["rep_id"], money(recovered), inv["discount_pct"], c_pct,
        money(recovered * c_pct / 100.0), days, b_pct, money(recovered * b_pct / 100.0),
        inv["invoice_date"], as_of, "إعادة عمولة بعد تحصيل المتأخر", as_of,
        f"حُصّل {recovered:,.2f} من رصيد سبق عكس عمولته، والإعدادات تسمح بإعادتها."))
    ex("UPDATE invoices SET late_reversed = ROUND(late_reversed - ?,2) WHERE id=?",
       (recovered, inv["id"]))
    audit("late_restore", "invoice", inv["id"], inv["ref_no"], {"amount": recovered}, user=user)


# ============================================================
#  الفحص اليومي الآلي
# ============================================================
def run_daily_jobs(user=None, force=False):
    """
    يشغَّل مرة واحدة يومياً: تحصيل الشيكات المستحقة ثم عكس المتأخرات.
    الحارس ذرّي: يُؤخذ قفل الكتابة أولاً ثم يُعاد فحص التاريخ داخل المعاملة،
    فلا يشغّله جهازان في نفس اللحظة فتتضاعف قيود العكس.
    """
    from db import set_setting
    stamp = today_str()
    if not force and get_setting("last_daily_run") == stamp:
        return None

    begin_write()
    # إعادة الفحص بعد امتلاك القفل — بينهما قد يكون مسار آخر قد شغّله بالفعل
    if not force and get_setting("last_daily_run") == stamp:
        return None
    cleared = settle_due_cheques(user)
    reversed_n = run_late_reversals(user)
    refresh_accrued_commissions()
    set_setting("last_daily_run", stamp)
    if cleared or reversed_n:
        audit("daily_job", "system", None, stamp,
              {"cheques_cleared": cleared, "late_reversals": reversed_n}, user=user)
    return {"cheques_cleared": cleared, "late_reversals": reversed_n}


def refresh_accrued_commissions():
    """تحويل العمولات المؤجلة (شيكات) إلى مستحقة عند وصول تاريخ الاستحقاق."""
    ex("""UPDATE commission_entries SET status='earned'
          WHERE status='accrued' AND date(recognized_on) <= date('now')""")
