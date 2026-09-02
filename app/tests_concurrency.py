"""
اختبارات التزامن على PostgreSQL — تعيد نفس السيناريوهات التي كشفت خلل المرحلة الأولى.

تُشغَّل على قاعدة تجريبية قابلة للتخلص فقط:
    MABEE3AT_DB_URL=postgresql://... python3 tests_concurrency.py

كل اختبار يفتح اتصالات مستقلة (كما لو كانوا موظفين مختلفين على أجهزة مختلفة)
ويستدعي دوال engine.py نفسها بلا أي تعديل.
"""
import os
import sys
import threading
import uuid
from datetime import date, timedelta

# لاحقة فريدة لكل تشغيل: تجعل الاختبارات قابلة للإعادة على نفس القاعدة
# دون أن تلتقط شيكات خلّفها تشغيل سابق.
RUN = uuid.uuid4().hex[:6].upper()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ok = fail = 0


def check(label, got, want):
    global ok, fail
    good = (abs(float(got) - float(want)) < 0.011
            if isinstance(want, (int, float)) and isinstance(got, (int, float))
            and not isinstance(want, bool) else got == want)
    print(f"  {'✓' if good else '✗'} {label}: {got}" + ("" if good else f"  (المتوقع {want})"))
    if good:
        ok += 1
    else:
        fail += 1


def run_isolated(fn, n):
    """
    يشغّل fn في n خيوط، لكل خيط سياق تطبيق واتصال قاعدة بيانات مستقل تماماً،
    فيحاكي موظفين مختلفين يعملون في نفس اللحظة.
    """
    from app import app
    results, errors = [], []

    def worker(i):
        try:
            with app.app_context():
                results.append(fn(i))
        except Exception as e:
            errors.append(f"{type(e).__name__}: {e}")

    th = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    [t.start() for t in th]
    [t.join() for t in th]
    return results, errors


def main():
    url = os.environ.get("MABEE3AT_DB_URL", "")
    if not url.startswith(("postgres://", "postgresql://")):
        raise SystemExit("هذه الاختبارات تحتاج MABEE3AT_DB_URL لقاعدة PostgreSQL تجريبية.")

    from app import app
    import engine as E
    from db import commit, q, q1

    with app.app_context():
        admin = dict(q1("SELECT * FROM users WHERE username='admin'"))

    # ---------------------------------------------------------
    print("\n[A] تحصيلات متزامنة على نفس العميل")
    with app.app_context():
        cust = q1("""SELECT c.id, c.name,
                       SUM(m_round(i.total-i.returned_total-i.collected_total,2)) bal
                     FROM customers c JOIN invoices i ON i.customer_id=c.id AND i.status='posted'
                     GROUP BY c.id, c.name HAVING SUM(i.total-i.returned_total-i.collected_total) > 5000
                     ORDER BY bal DESC LIMIT 1""")
        before = q1("SELECT COALESCE(SUM(collected_total),0) s FROM invoices")["s"]
        alloc_before = q1("SELECT COALESCE(SUM(amount),0) s FROM allocations")["s"]
        target = float(cust["bal"])
        # البذرة تحتوي سيناريو مقصود (مرتجع بعد تحصيل كامل) برصيد سالب.
        # المقياس هو ألا يظهر تحصيل زائد *جديد* بفعل التزامن.
        over_before = {r["ref_no"] for r in q(
            """SELECT ref_no FROM invoices WHERE status='posted'
               AND m_round(total-returned_total-collected_total,2) < -0.011""")}
    print(f"    العميل {cust['name']} برصيد {target:,.2f} — ٨ محاسبين كل منهم يحصّل الرصيد كاملاً")

    def collect(i):
        r = E.create_collection({"customer_id": cust["id"],
                                 "collection_date": date.today().isoformat(),
                                 "method": "cash", "amount": target}, admin)
        commit()
        return r[2]                      # المبلغ المخصَّص فعلياً

    allocated, errs = run_isolated(collect, 8)
    with app.app_context():
        after = q1("SELECT COALESCE(SUM(collected_total),0) s FROM invoices")["s"]
        alloc_after = q1("SELECT COALESCE(SUM(amount),0) s FROM allocations")["s"]
        over_after = {r["ref_no"] for r in q(
            """SELECT ref_no FROM invoices WHERE status='posted'
               AND m_round(total-returned_total-collected_total,2) < -0.011""")}
        over = sorted(over_after - over_before)
        dup = q1("""SELECT COUNT(*) n FROM (SELECT ref_no FROM collections
                    GROUP BY ref_no HAVING COUNT(*)>1) x""")["n"]
    check("لا أخطاء تزامن", len(errs), 0)
    if errs:
        print("     ", errs[:2])
    check("لا تحصيل زائد جديد بفعل التزامن", len(over), 0)
    check("مجموع ما خُصم = مجموع ما خُصّص",
          round(float(after - before), 2), round(float(alloc_after - alloc_before), 2))
    check("المخصَّص لا يتجاوز الرصيد المتاح", round(sum(allocated), 2) <= target + 0.011, True)
    check("لا أرقام مرجعية مكررة", dup, 0)

    # ---------------------------------------------------------
    print("\n[B1] تحصيل مكرر لنفس الشيك")
    with app.app_context():
        c2 = q1("SELECT id FROM customers LIMIT 1")
        due = (date.today() + timedelta(days=30)).isoformat()
        E.create_collection({"customer_id": c2["id"],
                             "collection_date": date.today().isoformat(),
                             "method": "cheque", "amount": 500,
                             "cheque_number": f"CLR-{RUN}", "cheque_due_date": due}, admin)
        commit()
        chq1 = q1("SELECT * FROM cheques WHERE cheque_number=?", (f"CLR-{RUN}",))
        n_alloc = q1("""SELECT COUNT(*) n FROM allocations a JOIN collections co
                        ON co.id=a.source_id WHERE co.cheque_id=?""", (chq1["id"],))["n"]

    def clear_only(i):
        E.clear_cheque(chq1["id"], admin, due)
        commit()
        return "cleared"

    wins, _ = run_isolated(clear_only, 6)
    with app.app_context():
        earn = q1("""SELECT COUNT(*) n FROM commission_entries ce
                     JOIN allocations a ON a.id=ce.allocation_id
                     JOIN collections co ON co.id=a.source_id
                     WHERE co.cheque_id=? AND ce.entry_type='earn'""", (chq1["id"],))["n"]
        st1 = q1("SELECT status FROM cheques WHERE id=?", (chq1["id"],))["status"]
    check("٦ محاولات تحصيل متزامنة → واحدة فقط نجحت", len(wins), 1)
    check("الحالة النهائية: تم التحصيل", st1, "cleared")
    check("قيد عمولة واحد لكل تخصيص بلا تكرار", earn, n_alloc)

    print("\n[B2] تحصيل وارتداد متزامنان")
    with app.app_context():
        E.create_collection({"customer_id": c2["id"],
                             "collection_date": date.today().isoformat(),
                             "method": "cheque", "amount": 500,
                             "cheque_number": f"MIX-{RUN}", "cheque_due_date": due}, admin)
        commit()
        chq2 = q1("SELECT * FROM cheques WHERE cheque_number=?", (f"MIX-{RUN}",))

    def mixed(i):
        if i % 2:
            E.clear_cheque(chq2["id"], admin, due)
            commit()
            return "clear"
        E.bounce_cheque(chq2["id"], admin, date.today().isoformat(), "تزامن")
        commit()
        return "bounce"

    acts, _ = run_isolated(mixed, 6)
    with app.app_context():
        st2 = q1("SELECT status FROM cheques WHERE id=?", (chq2["id"],))["status"]
        left = q1("""SELECT COUNT(*) n FROM allocations a JOIN collections co
                     ON co.id=a.source_id WHERE co.cheque_id=?""", (chq2["id"],))["n"]
        earn2 = q1("""SELECT COUNT(*) n FROM commission_entries ce
                      JOIN allocations a ON a.id=ce.allocation_id
                      JOIN collections co ON co.id=a.source_id
                      WHERE co.cheque_id=? AND ce.entry_type='earn'""", (chq2["id"],))["n"]
    # تسلسل «تحصيل ثم ارتداد» صحيح تجارياً (شيك حُصّل ثم ارتد)،
    # فالمقياس هو ألا يتكرر أي فعل، لا أن ينجح فعل واحد فقط.
    check("محاولة تحصيل واحدة نجحت على الأكثر", acts.count("clear") <= 1, True)
    check("محاولة ارتداد واحدة نجحت على الأكثر", acts.count("bounce") <= 1, True)
    check("الحالة النهائية صالحة", st2 in ("cleared", "bounced"), True)
    if st2 == "bounced":
        check("الارتداد أزال كل تخصيصات الشيك", left, 0)
        check("لا قيود عمولة سارية بعد الارتداد", earn2, 0)
    print(f"     الأفعال الناجحة: {sorted(acts)} | الحالة: {st2}")

    print("\n[C] توليد أرقام مرجعية متزامن")
    def make_ref(i):
        from db import next_ref
        r = next_ref("invoice")
        commit()
        return r

    refs, errs = run_isolated(make_ref, 12)
    check("كل الطلبات نجحت", len(refs), 12)
    check("لا رقم مكرر", len(set(refs)), len(refs))
    import re as _re
    check("الصيغة محفوظة (INV-YYYY-NNNNNN)",
          all(_re.fullmatch(r"INV-\d{4}-\d{6}", r) for r in refs), True)
    nums = sorted(int(r.split("-")[-1]) for r in refs)
    check("الأرقام متتابعة بلا فجوات", nums == list(range(nums[0], nums[0] + len(nums))), True)

    # ---------------------------------------------------------
    print("\n[D] فواتير متزامنة لعملاء مختلفين")
    with app.app_context():
        custs = [r["id"] for r in q("SELECT id FROM customers ORDER BY id LIMIT 6")]

    def make_inv(i):
        _, ref = E.create_aggregate_invoice({
            "customer_id": custs[i % len(custs)], "invoice_date": date.today().isoformat(),
            "total_amount": 1000 + i, "external_ref": f"INV-{RUN}-{i}"}, admin)
        commit()
        return ref

    inv_refs, errs = run_isolated(make_inv, 6)
    with app.app_context():
        dup = q1("""SELECT COUNT(*) n FROM (SELECT ref_no FROM invoices
                    GROUP BY ref_no HAVING COUNT(*)>1) x""")["n"]
    check("كل الفواتير حُفظت", len(inv_refs), 6)
    check("لا أرقام فواتير مكررة", dup, 0)

    # ---------------------------------------------------------
    print("\n[E] المهمة اليومية تحت التزامن")
    with app.app_context():
        from db import set_setting
        set_setting("last_daily_run", "1900-01-01")
        commit()
        n_before = q1("""SELECT COUNT(*) n FROM commission_entries
                         WHERE entry_type='late_reversal'""")["n"]

    def daily(i):
        r = E.run_daily_jobs(admin)
        commit()
        return r

    res, errs = run_isolated(daily, 6)
    with app.app_context():
        n_after = q1("""SELECT COUNT(*) n FROM commission_entries
                        WHERE entry_type='late_reversal'""")["n"]
    check("لا أخطاء", len(errs), 0)
    check("تشغيل فعلي واحد فقط", sum(1 for r in res if r is not None), 1)
    check("لا قيود عكس مكررة", n_after, n_before)

    print(f"\n{'-'*54}\nالنتيجة: نجح {ok} / فشل {fail}\n")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
