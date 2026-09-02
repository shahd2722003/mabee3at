"""
بيانات تجريبية تغطي كل السيناريوهات: الخصومات، التحصيل كاش/كريدت/شيكات،
المرتجعات قبل وبعد التحصيل، شرائح البونص كاملة، وارتداد شيك.

الترتيب المتّبع كما هو مطلوب:
  1) الأصناف  2) فواتير تاريخية  3) مرتجعات تاريخية  4) الرصيد الافتتاحي للمخزون
"""
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from werkzeug.security import generate_password_hash

import engine as E
from app import app
from db import DB_PATH, ENGINE, commit, ex, get_db, init_db, q1

T = date.today()


def ago(n):
    return (T - timedelta(days=n)).isoformat()


def ahead(n):
    return (T + timedelta(days=n)).isoformat()


def run():
    """
    بيانات تجريبية حتمية — نفس النتيجة على SQLite و PostgreSQL في نفس اليوم.

    حماية: يرفض العمل على قاعدة فيها مستخدمون بالفعل ما لم يُسمح صراحةً،
    حتى لا تُمحى بيانات قائمة بالخطأ.
    """
    if os.environ.get("MABEE3AT_ALLOW_RESEED", "").lower() not in ("1", "true", "yes"):
        try:
            with app.app_context():
                if q1("SELECT 1 FROM users LIMIT 1"):
                    raise SystemExit(
                        "القاعدة تحتوي مستخدمين بالفعل. seed.py يمحو كل شيء.\n"
                        "لو كنت متأكداً: MABEE3AT_ALLOW_RESEED=1 python seed.py")
        except SystemExit:
            raise
        except Exception:
            pass          # قاعدة فارغة أو غير مهيّأة — المتابعة آمنة
    if ENGINE == "postgres":
        init_db()                      # ينشئ المخطّط والدوال في قاعدة فارغة
        with app.app_context():
            E.seed_default_settings()
            commit()
            build()
            commit()
        print("تم إنشاء البيانات التجريبية في PostgreSQL")
        return
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    with app.app_context():
        init_db(get_db())
        E.seed_default_settings()
        commit()
        build()
        commit()
    print(f"تم إنشاء البيانات التجريبية في: {os.path.abspath(DB_PATH)}")


def build():
    # ---------- المندوبون والمستخدمون ----------
    reps = [("R01", "محمد سعيد", "01001112233"),
            ("R02", "أحمد فؤاد", "01002223344"),
            ("R03", "هالة منصور", "01003334455")]
    rid = {}
    for code, name, ph in reps:
        rid[code] = ex("INSERT INTO reps(code,name,phone,hire_date) VALUES (?,?,?,?)",
                       (code, name, ph, ago(900))).lastrowid

    pw = generate_password_hash("123456")
    users = [("admin", "محمود عبد الله", "admin", None),
             ("accountant", "سلوى إبراهيم", "accountant", None),
             ("rep1", "محمد سعيد", "rep", rid["R01"]),
             ("rep2", "أحمد فؤاد", "rep", rid["R02"])]
    for un, fn, role, r in users:
        ex("""INSERT INTO users(username,password_hash,full_name,role,rep_id)
              VALUES (?,?,?,?,?)""", (un, pw, fn, role, r))
    admin = dict(q1("SELECT * FROM users WHERE username='admin'"))

    # ---------- 1) الأصناف ----------
    items = [
        ("SH-1001", "قميص قطن رجالي", "أبيض", "L", 340, 195),
        ("SH-1002", "قميص قطن رجالي", "أزرق", "M", 340, 195),
        ("SH-1003", "قميص كتان", "بيج", "XL", 420, 250),
        ("TS-2001", "تي شيرت بولو", "أسود", "L", 260, 140),
        ("TS-2002", "تي شيرت بولو", "كحلي", "M", 260, 140),
        ("PT-3001", "بنطلون جينز", "أزرق غامق", "32", 560, 330),
        ("PT-3002", "بنطلون جينز", "أسود", "34", 560, 330),
        ("PT-3003", "بنطلون قماش", "رمادي", "36", 480, 285),
        ("DR-4001", "فستان صيفي", "أحمر", "M", 720, 410),
        ("DR-4002", "فستان سواريه", "أسود", "L", 1150, 690),
        ("JK-5001", "جاكيت شتوي", "بني", "L", 1350, 820),
        ("AC-6001", "شال حرير", "متعدد", "مقاس واحد", 190, 95),
    ]
    iid = {}
    for c, n, col, sz, sp, cp in items:
        iid[c] = ex("""INSERT INTO items(code,name,color,size,sale_price,cost_price)
                       VALUES (?,?,?,?,?,?)""", (c, n, col, sz, sp, cp)).lastrowid

    # ---------- العملاء ----------
    custs = [
        ("C001", "محلات النور للملابس", "R01", 20, 30, "credit"),
        ("C002", "بوتيك ريم", "R01", 25, 45, "cheque"),
        ("C003", "معرض الأندلس", "R02", 27, 60, "credit"),
        ("C004", "هايبر ستايل", "R02", 28, 30, "cheque"),
        ("C005", "كاش أند كاري الحرية", "R03", 20, 0, "cash"),
        ("C006", "شركة التجارة العامة", "R03", 30, 90, "credit"),
        ("C007", "محلات السلام", "R01", 0, 15, "cash"),
    ]
    cid = {}
    for c, n, r, dsc, dys, pm in custs:
        cid[c] = ex("""INSERT INTO customers(code,name,rep_id,default_discount_pct,
                       default_credit_days,default_payment_method,phone)
                       VALUES (?,?,?,?,?,?,?)""",
                    (c, n, rid[r], dsc, dys, pm, "0100000000")).lastrowid

    # ---------- 2) فواتير تاريخية ----------
    # (يُسمح بتجاوز المخزون لأن الرصيد الافتتاحي يُدخل لاحقاً — الخطوة 4)
    def inv(cust, days_ago, lines, discount=None, credit=None, method=None):
        return E.create_invoice({
            "customer_id": cid[cust], "invoice_date": ago(days_ago),
            "discount_pct": discount, "credit_days": credit,
            "payment_method": method, "is_historical": True,
            "notes": "فاتورة تاريخية",
        }, [{"item_id": iid[c], "qty": q, "unit_price": p} for c, q, p in lines],
            admin, override_stock=True)

    I = {}
    I["A"] = inv("C005", 150, [("TS-2001", 40, 260), ("TS-2002", 30, 260)])            # 20% كاش
    I["B"] = inv("C001", 140, [("SH-1001", 50, 340), ("PT-3001", 20, 560)])            # 20%
    I["C"] = inv("C001", 120, [("SH-1002", 40, 340), ("AC-6001", 60, 190)])            # 20%
    I["D"] = inv("C003", 115, [("DR-4001", 25, 720), ("DR-4002", 10, 1150)])           # 27%
    I["E"] = inv("C003", 100, [("PT-3002", 30, 560)])                                  # 27%
    I["F"] = inv("C006", 95, [("JK-5001", 20, 1350), ("PT-3003", 25, 480)])            # 30%
    I["G"] = inv("C006", 88, [("SH-1003", 35, 420)])                                   # 30%
    I["H"] = inv("C007", 110, [("TS-2001", 15, 260), ("AC-6001", 20, 190)])            # 0%
    I["J"] = inv("C002", 105, [("DR-4001", 18, 720), ("SH-1001", 25, 340)])            # 25% شيك
    I["K"] = inv("C004", 70, [("PT-3001", 30, 560), ("JK-5001", 8, 1350)])             # 28% شيك
    I["L"] = inv("C001", 45, [("SH-1001", 30, 340)])                                   # FIFO جزئي
    I["M"] = inv("C001", 30, [("PT-3003", 20, 480)])                                   # FIFO جزئي
    I["N"] = inv("C002", 20, [("DR-4002", 6, 1150)], discount=25)                      # شيك مستقبلي
    I["P"] = inv("C003", 12, [("TS-2002", 25, 260)], discount=25, credit=30)           # خصم معدّل داخل الفاتورة
    I["Q"] = inv("C004", 5, [("SH-1002", 20, 340)])                                    # مفتوحة

    # ---------- 3) مرتجعات تاريخية ----------
    # مرتجع قبل التحصيل: يقلّل رصيد الفاتورة
    E.create_return({"invoice_id": I["D"][0], "return_date": ago(108),
                     "notes": "مرتجع مقاسات"}, [{"item_id": iid["DR-4001"], "qty": 5}], admin)
    # مرتجع على فاتورة كاش قديمة (سيؤدي لعكس عمولة بعد التحصيل)
    E.create_return({"invoice_id": I["B"][0], "return_date": ago(60),
                     "notes": "مرتجع عيوب خامة"}, [{"item_id": iid["SH-1001"], "qty": 6}], admin)

    # ---------- 4) الرصيد الافتتاحي للمخزون ----------
    for c, n, col, sz, sp, cp in items:
        E.add_stock_move(ago(160), iid[c], "opening", 300, cp, sp,
                         "opening", None, "OPEN-2026", "رصيد افتتاحي", admin["id"])
    # إضافة شراء لاحق
    for c in ("SH-1001", "PT-3001", "DR-4001", "JK-5001"):
        E.add_stock_move(ago(40), iid[c], "purchase", 120,
                         dict(q1("SELECT * FROM items WHERE code=?", (c,)))["cost_price"],
                         dict(q1("SELECT * FROM items WHERE code=?", (c,)))["sale_price"],
                         "purchase", None, "PO-1042", "توريد دفعة جديدة", admin["id"])
    # تسوية جرد بالسالب
    E.add_stock_move(ago(15), iid["AC-6001"], "adjustment", -12, 95, 190,
                     "count", None, "ADJ-07", "فرق جرد", admin["id"])

    # ---------- فواتير تاريخية إجمالية (بدون أصناف) ----------
    def agg(cust, days_ago, amount, discount=None, credit=None, method=None, ref=None):
        return E.create_aggregate_invoice({
            "customer_id": cid[cust], "invoice_date": ago(days_ago), "total_amount": amount,
            "discount_pct": discount, "credit_days": credit, "payment_method": method,
            "external_ref": ref, "notes": "مرحّلة من الدفاتر الورقية",
        }, admin)

    G = {}
    G["A"] = agg("C001", 200, 18500, ref="A-1180")
    G["B"] = agg("C001", 185, 9750, ref="A-1194")
    G["C"] = agg("C003", 175, 24300, ref="A-1201")
    G["D"] = agg("C006", 260, 31000, credit=90, ref="A-1102")   # ستتأخر ← عكس عمولة
    G["E"] = agg("C002", 40, 12600, method="cheque", ref="A-1350")

    # ---------- التحصيلات: تغطية كل شرائح البونص ----------
    def coll(cust, days_ago, method, amount, **kw):
        return E.create_collection({"customer_id": cid[cust], "collection_date": ago(days_ago),
                                    "method": method, "amount": amount, **kw}, admin)

    def total_of(key):
        r = q1("SELECT total, returned_total FROM invoices WHERE id=?", (I[key][0],))
        return round(r["total"] - r["returned_total"], 2)

    # 0 يوم → بونص 3% ، خصم 20% → عمولة 2%
    coll("C005", 150, "cash", total_of("A"))
    # 5 أيام → 3% ، خصم 20% → 2%   (سيُعكس جزء منه لاحقاً بسبب المرتجع)
    coll("C001", 135, "cash", total_of("B") + 1200)
    # 12 يوم → 2.5%
    coll("C001", 108, "credit", total_of("C"))
    # 25 يوم → 2% ، خصم 27% → 1%
    coll("C003", 90, "credit", total_of("D"))
    # 40 يوم → 1.5%
    coll("C003", 60, "credit", total_of("E"))
    # 55 يوم → 1% ، خصم 30% → 1%
    coll("C006", 40, "credit", total_of("F"))
    # 70 يوم → 0.5%
    coll("C006", 18, "credit", total_of("G"))
    # 100 يوم → 0% ، خصم 0% → 2%
    coll("C007", 10, "credit", total_of("H"))

    # شيك مستحق في الماضي: العمولة تُحسب بتاريخ الاستحقاق (60 يوم من الفاتورة) لا بتاريخ الاستلام
    coll("C002", 100, "cheque", total_of("J"), cheque_number="512477",
         bank_name="بنك القاهرة", cheque_due_date=ago(45))
    # شيك مستحق في المستقبل: العمولة مؤجلة حتى تاريخ الاستحقاق
    coll("C002", 15, "cheque", total_of("N"), cheque_number="512901",
         bank_name="بنك القاهرة", cheque_due_date=ahead(25))
    # تحصيل على دفعات متعددة لفاتورة إجمالية واحدة (كاش ثم تحويل ثم شيك)
    coll("C001", 195, "cash", 6000)
    coll("C001", 188, "transfer", 7500)
    coll("C001", 180, "cheque", 5000, cheque_number="640012",
         bank_name="بنك مصر", cheque_due_date=ago(150))
    coll("C003", 120, "cash", 10000)

    # شيك سيرتد
    coll("C004", 60, "cheque", total_of("K"), cheque_number="778110",
         bank_name="البنك الأهلي", cheque_due_date=ago(20))
    # تحويل بنكي
    coll("C003", 5, "transfer", 4000)

    # تحصيل جزئي يوزَّع FIFO على أكثر من فاتورة (L ثم M)
    coll("C001", 3, "cash", round(total_of("L") + total_of("M") * 0.4, 2))

    # الفحص اليومي: الشيكات المستحقة تتحول تلقائياً إلى «تم التحصيل» وتُحتسب عمولتها
    E.settle_due_cheques(admin)

    # ارتداد شيك بعد تحصيله تلقائياً: يعكس التخصيص والعمولة ويعيد القيمة للرصيد
    ch = q1("SELECT * FROM cheques WHERE cheque_number='778110'")
    E.bounce_cheque(ch["id"], admin, ago(18), "رصيد غير كافٍ")

    # مرتجع بعد التحصيل الكامل → عكس عمولة نسبي
    E.create_return({"invoice_id": I["A"][0], "return_date": ago(20),
                     "notes": "مرتجع بعد التحصيل"},
                    [{"item_id": iid["TS-2001"], "qty": 8}], admin)

    # ---------- الفحص اليومي: تحصيل الشيكات المستحقة وعكس عمولة المتأخرات ----------
    E.run_daily_jobs(admin, force=True)
    E.refresh_accrued_commissions()


if __name__ == "__main__":
    run()
