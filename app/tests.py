"""اختبارات القواعد المالية. شغّلها بعد seed.py:  python3 tests.py"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import engine as E
from app import app
from datetime import date, timedelta

from db import ENGINE, commit, ex, q, q1

ok = fail = 0


def check(label, got, want):
    global ok, fail
    if isinstance(want, float) or isinstance(got, float):
        good = abs(float(got) - float(want)) < 0.011
    else:
        good = got == want
    print(("  ✓ " if good else "  ✗ ") + f"{label}: {got}" + ("" if good else f"  (المتوقع {want})"))
    ok, fail = (ok + 1, fail) if good else (ok, fail + 1)


with app.app_context():
    admin = dict(q1("SELECT * FROM users WHERE username='admin'"))
    rules = E.DEFAULT_COMMISSION_RULES
    tiers = E.DEFAULT_BONUS_TIERS

    print("\n[1] العمولة حسب نسبة خصم الفاتورة")
    for disc, want in [(0, 2.0), (10, 2.0), (20, 2.0), (25, 1.0), (27, 1.0), (28, 1.0), (35, 1.0)]:
        check(f"خصم {disc}%", E.commission_pct_for(disc, rules)[0], want)

    print("\n[2] البونص حسب مدة التحصيل")
    for days, want in [(0, 3.0), (1, 3.0), (7, 3.0), (8, 2.5), (15, 2.5), (16, 2.0), (30, 2.0),
                       (31, 1.5), (45, 1.5), (46, 1.0), (60, 1.0), (61, 0.5), (75, 0.5),
                       (76, 0.0), (90, 0.0), (200, 0.0)]:
        check(f"{days} يوم", E.bonus_pct_for(days, tiers)[0], want)

    print("\n[3] قاعدة الشيكات: الاحتساب بتاريخ الاستحقاق لا بتاريخ التحصيل")
    inv = q1("SELECT * FROM invoices WHERE discount_pct=25 LIMIT 1")
    a = E.compute_commission(inv, 10000, "cheque", collection_date=inv["invoice_date"],
                             cheque_due_date=E.d(inv["invoice_date"]).replace().isoformat())
    b = E.compute_commission(inv, 10000, "cheque",
                             collection_date=inv["invoice_date"],
                             cheque_due_date=(E.d(inv["invoice_date"]) + E.timedelta(days=50)).isoformat())
    check("شيك مستحق نفس يوم الفاتورة → بونص", a["bonus_pct"], 3.0)
    check("نفس تاريخ التحصيل لكن استحقاق بعد 50 يوم → بونص", b["bonus_pct"], 1.0)
    check("العمولة ثابتة بخصم 25%", b["commission_pct"], 1.0)
    check("تاريخ استحقاق العمولة = استحقاق الشيك", b["recognized_on"], b["basis_to"])

    print("\n[4] الأرصدة: كل فاتورة = الإجمالي − المرتجعات − المحصَّل")
    bad = 0
    for r in q("""SELECT inv.id, inv.ref_no, inv.total, inv.returned_total, inv.collected_total,
                    (SELECT COALESCE(SUM(amount),0) FROM allocations
                     WHERE invoice_id=inv.id AND source_type='collection') alloc,
                    (SELECT COALESCE(SUM(amount),0) FROM allocations
                     WHERE invoice_id=inv.id AND source_type='return') ret
                  FROM invoices inv WHERE inv.status='posted'"""):
        if abs(r["alloc"] - r["collected_total"]) > 0.011 or abs(r["ret"] - r["returned_total"]) > 0.011:
            bad += 1
            print("    اختلاف في", r["ref_no"])
    check("تطابق التخصيصات مع أرصدة الفواتير", bad, 0)

    print("\n[5] FIFO: لا فاتورة أحدث تُسدَّد قبل أقدم منها مفتوحة")
    viol = 0
    for c in q("SELECT DISTINCT customer_id FROM invoices WHERE status='posted'"):
        rows = q("""SELECT inv.invoice_date, inv.id,
                      ROUND(inv.total-inv.returned_total-inv.collected_total,2) out,
                      inv.collected_total
                    FROM invoices inv WHERE customer_id=? AND status='posted'
                    ORDER BY date(inv.invoice_date), inv.id""", (c["customer_id"],))
        seen_open = False
        for r in rows:
            if seen_open and r["collected_total"] > 0.011:
                viol += 1
            if r["out"] > 0.011:
                seen_open = True
    check("عدد مخالفات FIFO", viol, 0)

    print("\n[6] عكس العمولة عند المرتجع بعد التحصيل وارتداد الشيك")
    revs = q("SELECT * FROM commission_entries WHERE entry_type='reversal'")
    check("توجد قيود عكسية", len(revs) > 0, True)
    check("قيم القيود العكسية سالبة",
          all(r["commission_amt"] <= 0 and r["bonus_amt"] <= 0 for r in revs), True)

    print("\n[7] القواعد مجمَّدة على الفواتير القديمة")
    snap = json.loads(q1("SELECT rules_snapshot FROM invoices LIMIT 1")["rules_snapshot"])
    check("snapshot يحتوي قواعد العمولة", "commission_rules" in snap, True)
    check("snapshot يحتوي شرائح البونص", "bonus_tiers" in snap, True)

    print("\n[8] المخزون = مجموع الحركات")
    check("لا صنف برصيد مفقود",
          len(q("SELECT * FROM v_item_stock WHERE qty_available IS NULL")), 0)


    print("\n[9] الفاتورة التاريخية الإجمالية")
    aggs = q("SELECT * FROM invoices WHERE invoice_kind='aggregate'")
    check("عدد الفواتير الإجمالية", len(aggs), 5)
    check("لا بنود أصناف عليها", q1("""SELECT COUNT(*) n FROM invoice_lines il
             JOIN invoices i ON i.id=il.invoice_id WHERE i.invoice_kind='aggregate'""")["n"], 0)
    check("لا حركة مخزون منها", q1("""SELECT COUNT(*) n FROM stock_moves
             WHERE ref_type='invoice' AND ref_id IN
             (SELECT id FROM invoices WHERE invoice_kind='aggregate')""")["n"], 0)
    a = q1("SELECT * FROM invoices WHERE external_ref='A-1180'")
    check("القيمة المدخلة هي الصافي النهائي", a["total"], 18500.0)
    check("قبل الخصم − الخصم = الصافي", round(a["subtotal"] - a["discount_amount"], 2), 18500.0)
    check("تدخل في FIFO والتحصيل", a["collected_total"] > 0, True)
    check("لها قيود عمولة", q1("SELECT COUNT(*) n FROM commission_entries WHERE invoice_id=?",
                               (a["id"],))["n"] > 0, True)

    print("\n[10] الشيك: خصم فوري من الرصيد بلا عمولة حتى الاستحقاق")
    # HAVING لا يقبل الاسم المستعار في PostgreSQL — نكرر التعبير ليعمل على المحرّكين
    cust = q1("""SELECT c.*, SUM(i.total-i.returned_total-i.collected_total) bal
                 FROM customers c JOIN invoices i ON i.customer_id=c.id AND i.status='posted'
                 GROUP BY c.id
                 HAVING SUM(i.total-i.returned_total-i.collected_total) > 5000
                 ORDER BY SUM(i.total-i.returned_total-i.collected_total) DESC LIMIT 1""")

    def bal(cid):
        return q1("""SELECT COALESCE(SUM(total-returned_total-collected_total),0) b
                     FROM invoices WHERE customer_id=? AND status='posted'""", (cid,))["b"]

    before = bal(cust["id"])
    future = (date.today() + timedelta(days=40)).isoformat()
    coll_id, _, alloc, _ = E.create_collection({
        "customer_id": cust["id"], "collection_date": date.today().isoformat(),
        "method": "cheque", "amount": 3000, "cheque_number": "TST-1",
        "cheque_due_date": future}, admin)
    check("خُصّص الشيك على فواتير مفتوحة", alloc > 0, True)
    check("خُصم من الرصيد فور الاستلام", round(before - bal(cust["id"]), 2), round(alloc, 2))
    chq = q1("SELECT * FROM cheques WHERE cheque_number='TST-1'")
    check("حالته معلّق", chq["status"], "pending")
    check("لا عمولة عند الاستلام", q1("""SELECT COUNT(*) n FROM commission_entries ce
             JOIN allocations a ON a.id=ce.allocation_id
             WHERE a.source_type='collection' AND a.source_id=?""", (coll_id,))["n"], 0)
    check("لا يُحصَّل قبل أوانه", E.settle_due_cheques(admin), 0)
    check("أُنشئت قيود عمولة عند التحصيل", E.clear_cheque(chq["id"], admin, future, auto=True) > 0, True)
    ce = q1("""SELECT ce.* FROM commission_entries ce JOIN allocations a ON a.id=ce.allocation_id
               WHERE a.source_id=? LIMIT 1""", (coll_id,))
    check("العمولة محسوبة بتاريخ الاستحقاق", ce["recognized_on"], future)

    print("\n[11] ارتداد الشيك: إعادة القيمة وعكس العمولة")
    back = bal(cust["id"])
    earned = q1("""SELECT COALESCE(SUM(ce.commission_amt+ce.bonus_amt),0) s
                   FROM commission_entries ce JOIN allocations a ON a.id=ce.allocation_id
                   WHERE a.source_type='collection' AND a.source_id=?""", (coll_id,))["s"]
    rev_before = q1("""SELECT COALESCE(SUM(commission_amt+bonus_amt),0) s
                       FROM commission_entries WHERE basis_label='عكس بسبب ارتداد شيك'""")["s"]
    E.bounce_cheque(chq["id"], admin, date.today().isoformat(), "اختبار")
    check("عادت القيمة إلى رصيد العميل", round(bal(cust["id"]) - back, 2), round(alloc, 2))
    chq2 = q1("SELECT * FROM cheques WHERE cheque_number='TST-1'")
    check("سُجّل تاريخ الارتداد وسببه",
          bool(chq2["bounce_date"]) and chq2["bounce_reason"] == "اختبار", True)
    rev_after = q1("""SELECT COALESCE(SUM(commission_amt+bonus_amt),0) s
                      FROM commission_entries WHERE basis_label='عكس بسبب ارتداد شيك'""")["s"]
    check("قيمة العكس = العمولة المحتسبة بالسالب", round(rev_after - rev_before, 2), round(-earned, 2))
    check("حُذفت تخصيصات الشيك المرتد",
          q1("SELECT COUNT(*) n FROM allocations WHERE source_type='collection' AND source_id=?",
             (coll_id,))["n"], 0)

    print("\n[12] عكس العمولة والبونص لتأخر التحصيل")
    late = [r for r in E.late_scan() if r["is_late"]]
    check("توجد فواتير تجاوزت المهلة", len(late) > 0, True)
    smp = late[0]
    check("نهاية المهلة = الفاتورة + مدة السداد + السماحية", smp["deadline"],
          (E.d(smp["invoice_date"]) + timedelta(days=smp["credit_days"] + smp["grace_days"])).isoformat())
    revs = q("SELECT * FROM commission_entries WHERE entry_type='late_reversal'")
    check("أُنشئت قيود عكس التأخير", len(revs) > 0, True)
    check("كل القيود سالبة",
          all(r["commission_amt"] <= 0 and r["bonus_amt"] <= 0 for r in revs), True)
    check("مسمّاة بالوصف المطلوب", revs[0]["basis_label"], "عكس عمولة وبونص بسبب تأخر التحصيل")
    n_before = len(revs)
    E.run_late_reversals(admin)
    check("لا تكرار للقيد على نفس الرصيد",
          q1("SELECT COUNT(*) n FROM commission_entries WHERE entry_type='late_reversal'")["n"],
          n_before)
    ir = q1("SELECT * FROM invoices WHERE late_reversed > 0 LIMIT 1")
    check("العكس على الرصيد غير المحصَّل فقط", ir["late_reversed"] <=
          round(ir["total"] - ir["returned_total"] - ir["collected_total"], 2) + 0.011, True)

    print("\n[13] العمولة على مستوى الدفعة لا الفاتورة كاملة")
    multi = q1("""SELECT invoice_id, COUNT(*) n FROM allocations WHERE source_type='collection'
                  GROUP BY invoice_id HAVING COUNT(*) > 1 LIMIT 1""")
    check("توجد فاتورة محصَّلة على دفعات متعددة", multi is not None, True)
    parts = q("""SELECT a.amount, ce.base_amount FROM allocations a
                 JOIN commission_entries ce ON ce.allocation_id=a.id
                 WHERE a.invoice_id=?""", (multi["invoice_id"],))
    inv_t = q1("SELECT total FROM invoices WHERE id=?", (multi["invoice_id"],))["total"]
    check("كل قيد على الجزء المسدَّد فقط",
          all(abs(p["amount"] - p["base_amount"]) < 0.011 for p in parts), True)
    check("لا قيد بقيمة الفاتورة كاملة",
          all(abs(p["base_amount"] - inv_t) > 0.011 for p in parts), True)

    print("\n[14] المرتجع ممنوع على الفاتورة الإجمالية")
    try:
        E.create_return({"invoice_id": a["id"], "return_date": date.today().isoformat()},
                        [{"item_id": 1, "qty": 1}], admin)
        check("رُفض المرتجع على فاتورة بلا أصناف", False, True)
    except ValueError:
        check("رُفض المرتجع على فاتورة بلا أصناف", True, True)


    # القسمان 15 و16 يفحصان آليات SQLite نفسها (PRAGMA و BEGIN IMMEDIATE).
    # المكافئ في PostgreSQL هو القفل الاستشاري، ويُختبر في tests_concurrency.py.
    if ENGINE != "sqlite":
        print("\n[15][16] فحوص أقفال SQLite — تُتخطّى على PostgreSQL")
        print("          المكافئ: pg_advisory_xact_lock، مغطّى في tests_concurrency.py")
    else:
        print("\n[15] التزامن: إعدادات القفل مفعّلة")
        from db import get_db
        dbc = get_db()
        check("وضع WAL مفعّل",
              dbc.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
        check("مهلة انتظار القفل مضبوطة",
              dbc.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000, True)
        check("المعاملات تحت تحكم يدوي", dbc.isolation_level, None)

        print("\n[16] التزامن: كاتبان لا يكتبان فوق بعضهما")
        import sqlite3 as _sq
        import threading as _th
        from db import DB_PATH as _DBP

        tgt = q1("""SELECT id, ref_no, ROUND(total-returned_total-collected_total,2) o
                    FROM invoices WHERE status='posted'
                      AND ROUND(total-returned_total-collected_total,2) > 500 LIMIT 1""")

        def _conn():
            cx = _sq.connect(_DBP, timeout=15)
            cx.row_factory = _sq.Row
            cx.execute("PRAGMA busy_timeout=15000")
            return cx

        applied = []

        def _writer():
            cx = _conn()
            try:
                cx.execute("BEGIN IMMEDIATE")          # نفس ما يفعله begin_write()
                o = cx.execute("""SELECT ROUND(total-returned_total-collected_total,2) o
                                  FROM invoices WHERE id=?""", (tgt["id"],)).fetchone()["o"]
                cur = cx.execute("""UPDATE invoices SET collected_total=ROUND(collected_total+?,2)
                                    WHERE id=? AND ROUND(total-returned_total-collected_total,2) >= ?""",
                                 (o, tgt["id"], o))
                applied.append(o if cur.rowcount == 1 else 0.0)
                cx.commit()
            except Exception:
                applied.append(0.0)
            finally:
                cx.close()

        dbc.commit()                                   # حرّر القفل قبل اختبار الكتّاب
        th = [_th.Thread(target=_writer) for _ in range(2)]
        [x.start() for x in th]
        [x.join() for x in th]
        # الكاتب الثاني يقرأ الرصيد بعد اعتماد الأول، فلا يجد ما يخصّصه
        check("مجموع ما خُصم = الرصيد مرة واحدة لا مرتين",
              round(sum(applied), 2), round(tgt["o"], 2))
        left = q1("""SELECT ROUND(total-returned_total-collected_total,2) o
                     FROM invoices WHERE id=?""", (tgt["id"],))["o"]
        check("لا تحصيل زائد بعد كاتبين متزامنين", left >= -0.011, True)
        # إعادة الحالة كما كانت
        ex("UPDATE invoices SET collected_total=ROUND(collected_total-?,2) WHERE id=?",
           (tgt["o"], tgt["id"]))
        commit()

    print("\n[17] الحارس اليومي لا يتكرر (المحرّكان معاً)")
    n1 = q1("SELECT COUNT(*) n FROM commission_entries WHERE entry_type='late_reversal'")["n"]
    E.run_daily_jobs(admin)
    E.run_daily_jobs(admin)
    n2 = q1("SELECT COUNT(*) n FROM commission_entries WHERE entry_type='late_reversal'")["n"]
    check("تشغيلان متتاليان لا ينشئان قيوداً مكررة", n2, n1)


    print("\n[18] المعرّفات المُعادة من الإدراج (آمنة مع مجمّع الاتصالات)")
    cur = ex("""INSERT INTO items(code,name,color,size,sale_price,cost_price)
                VALUES (?,?,?,?,?,?)""", ("ZID-1", "صنف اختبار المعرّف", "أبيض", "M", 10, 5))
    new_id = cur.lastrowid
    commit()
    check("lastrowid ليس فارغاً", new_id is not None, True)
    row = q1("SELECT code FROM items WHERE id=?", (new_id,))
    check("المعرّف يشير إلى الصف الصحيح فعلاً", row["code"] if row else None, "ZID-1")

    cur2 = ex("INSERT INTO reps(code,name,phone) VALUES (?,?,?)",
              ("ZR-1", "مندوب اختبار", "0100"))
    rid2 = cur2.lastrowid
    commit()
    check("معرّف المندوب صحيح",
          q1("SELECT code FROM reps WHERE id=?", (rid2,))["code"], "ZR-1")
    check("معرّفان متتاليان مختلفان", new_id != rid2, True)

    # إدراجات متتابعة: كل واحد يعيد معرّفه هو، لا معرّف غيره
    ids = []
    for i in range(5):
        ids.append(ex("""INSERT INTO items(code,name,sale_price,cost_price)
                         VALUES (?,?,?,?)""", (f"ZID-B{i}", f"دفعة {i}", 1, 1)).lastrowid)
    commit()
    codes = [q1("SELECT code FROM items WHERE id=?", (i,))["code"] for i in ids]
    check("كل معرّف يطابق صفّه في إدراج متتابع",
          codes, [f"ZID-B{i}" for i in range(5)])
    ex("DELETE FROM items WHERE code LIKE 'ZID-%'")
    ex("DELETE FROM reps WHERE code='ZR-1'")
    commit()

    print("\n[19] تهيئة أول مدير")
    from app import admin_exists
    check("يوجد مدير في البيانات التجريبية", admin_exists(), True)
    c = app.test_client()
    r = c.get("/setup", follow_redirects=False)
    check("/setup يعيد التوجيه بعد وجود مدير", r.status_code, 302)
    r = c.post("/setup", data={"username": "hacker", "password": "aVeryLongPass1",
                               "password2": "aVeryLongPass1"}, follow_redirects=False)
    check("منع إنشاء مدير ثانٍ عبر /setup", r.status_code, 302)
    check("لم يُنشأ الحساب",
          q1("SELECT 1 FROM users WHERE username='hacker'") is None, True)

    print("\n[20] الأمان: الكوكيز وفحص المصدر")
    check("الكوكي HttpOnly", app.config["SESSION_COOKIE_HTTPONLY"], True)
    check("الكوكي SameSite=Strict", app.config["SESSION_COOKIE_SAMESITE"], "Strict")
    check("وضع التصحيح مطفأ", app.debug, False)
    c2 = app.test_client()
    c2.post("/login", data={"username": "admin", "password": "123456"})
    r = c2.post("/collections/new",
                data={"customer_id": "1", "collection_date": date.today().isoformat(),
                      "method": "cash", "amount": "1"},
                headers={"Origin": "https://evil.example.com"})
    check("رُفض طلب من مصدر خارجي", r.status_code, 403)

    print("\n[21] فحص الصحة")
    r = app.test_client().get("/healthz")
    check("/healthz يعمل بلا تسجيل دخول", r.status_code, 200)
    check("لا يكشف إعدادات", "DB_URL" not in r.get_data(as_text=True), True)

    print(f"\nالمحرّك: {ENGINE}")
    print(f"النتيجة: نجح {ok} / فشل {fail}\n")
    sys.exit(1 if fail else 0)
