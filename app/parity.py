"""
أداة تطابق مالي: تشغّل نفس البيانات الحتمية على SQLite و PostgreSQL
ثم تقارن كل مخرج مالي صفاً بصف.

الاستعمال:
    python3 parity.py                 # يبني القاعدتين ويقارن
    python3 parity.py --report out.json

أي فرق — ولو 0.01 — يُعدّ فشلاً ويُطبع بالتفصيل.
لا تُستخدم إلا بيانات تجريبية على قواعد قابلة للتخلص.
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# كل استعلام يمثّل مخرجاً مالياً. مكتوبة بلهجة SQLite وتُترجَم تلقائياً.
CHECKS = {
    "إجماليات الفواتير": """
        SELECT ref_no, invoice_kind, invoice_date, due_date, discount_pct, credit_days,
               payment_method, subtotal, discount_amount, total, external_ref
        FROM invoices ORDER BY ref_no""",

    "أرصدة الفواتير": """
        SELECT ref_no, total, returned_total, collected_total, late_reversed,
               ROUND(total - returned_total - collected_total, 2) outstanding, status
        FROM invoices ORDER BY ref_no""",

    "الفواتير التاريخية الإجمالية": """
        SELECT ref_no, external_ref, subtotal, discount_pct, discount_amount, total
        FROM invoices WHERE invoice_kind = 'aggregate' ORDER BY ref_no""",

    "بنود الفواتير": """
        SELECT i.ref_no, it.code, il.qty, il.unit_price, il.unit_cost, il.line_total
        FROM invoice_lines il JOIN invoices i ON i.id = il.invoice_id
        JOIN items it ON it.id = il.item_id ORDER BY i.ref_no, it.code""",

    "التحصيلات": """
        SELECT ref_no, collection_date, method, amount, allocated_total, status
        FROM collections ORDER BY ref_no""",

    "تخصيصات FIFO": """
        SELECT a.source_type, a.alloc_date, a.amount, i.ref_no invoice_ref
        FROM allocations a JOIN invoices i ON i.id = a.invoice_id
        ORDER BY a.alloc_date, i.ref_no, a.amount, a.source_type""",

    "المرتجعات": """
        SELECT r.ref_no, r.return_date, i.ref_no invoice_ref, r.subtotal,
               r.discount_pct, r.total, r.status
        FROM returns r JOIN invoices i ON i.id = r.invoice_id ORDER BY r.ref_no""",

    "بنود المرتجعات": """
        SELECT r.ref_no, it.code, rl.qty, rl.unit_price, rl.line_total
        FROM return_lines rl JOIN returns r ON r.id = rl.return_id
        JOIN items it ON it.id = rl.item_id ORDER BY r.ref_no, it.code""",

    "المخزون": """
        SELECT code, name, ROUND(qty_available, 4) qty_available, sale_price, cost_price
        FROM v_item_stock ORDER BY code""",

    "حركات المخزون": """
        SELECT sm.move_date, it.code, sm.move_type, sm.qty, sm.unit_cost, sm.unit_price
        FROM stock_moves sm JOIN items it ON it.id = sm.item_id
        ORDER BY sm.move_date, it.code, sm.move_type, sm.qty""",

    "دورة حياة الشيكات": """
        SELECT ref_no, cheque_number, amount, received_date, due_date, status,
               cleared_date, auto_cleared, bounce_date, bounce_reason
        FROM cheques ORDER BY ref_no""",

    "قيود العمولة والبونص": """
        SELECT i.ref_no invoice_ref, ce.entry_type, ce.base_amount, ce.discount_pct,
               ce.commission_pct, ce.commission_amt, ce.days_taken, ce.bonus_pct,
               ce.bonus_amt, ce.basis_from, ce.basis_to, ce.basis_label,
               ce.recognized_on, ce.status
        FROM commission_entries ce JOIN invoices i ON i.id = ce.invoice_id
        ORDER BY i.ref_no, ce.entry_type, ce.recognized_on, ce.base_amount,
                 ce.commission_amt, ce.bonus_amt""",

    "عكس عمولة التأخير": """
        SELECT i.ref_no, ce.base_amount, ce.commission_amt, ce.bonus_amt,
               ce.basis_to, ce.recognized_on
        FROM commission_entries ce JOIN invoices i ON i.id = ce.invoice_id
        WHERE ce.entry_type = 'late_reversal' ORDER BY i.ref_no, ce.base_amount""",

    "نتائج المندوبين": """
        SELECT r.code, r.name,
               ROUND(SUM(ce.commission_amt), 2) commission,
               ROUND(SUM(ce.bonus_amt), 2) bonus,
               ROUND(SUM(ce.commission_amt + ce.bonus_amt), 2) total
        FROM commission_entries ce JOIN reps r ON r.id = ce.rep_id
        GROUP BY r.id, r.code, r.name ORDER BY r.code""",

    "أرصدة العملاء": """
        SELECT c.code, c.name,
               ROUND(SUM(i.total - i.returned_total - i.collected_total), 2) balance
        FROM customers c JOIN invoices i ON i.customer_id = c.id AND i.status = 'posted'
        GROUP BY c.id, c.code, c.name ORDER BY c.code""",

    "أعمار الديون": """
        SELECT i.ref_no, c.code, i.due_date,
               ROUND(i.total - i.returned_total - i.collected_total, 2) outstanding,
               CAST(julianday('now') - julianday(i.due_date) AS INTEGER) late_days
        FROM invoices i JOIN customers c ON c.id = i.customer_id
        WHERE i.status = 'posted'
          AND ROUND(i.total - i.returned_total - i.collected_total, 2) > 0
        ORDER BY i.ref_no""",

    "المبيعات بالقيمة حسب النوع": """
        SELECT invoice_kind, COUNT(*) n, ROUND(SUM(total), 2) net,
               ROUND(SUM(collected_total), 2) collected
        FROM invoices WHERE status = 'posted' GROUP BY invoice_kind ORDER BY invoice_kind""",

    "مبيعات الأصناف": """
        SELECT it.code, ROUND(SUM(il.qty), 4) qty,
               ROUND(SUM(il.line_total), 2) gross,
               ROUND(SUM(il.line_total * (1 - i.discount_pct / 100.0)), 2) net
        FROM invoice_lines il JOIN invoices i ON i.id = il.invoice_id
        JOIN items it ON it.id = il.item_id
        WHERE i.status = 'posted' AND i.invoice_kind = 'detailed'
        GROUP BY it.id, it.code ORDER BY it.code""",

    "الأرقام المرجعية": """
        SELECT 'invoice' k, ref_no FROM invoices
        UNION ALL SELECT 'collection', ref_no FROM collections
        UNION ALL SELECT 'return', ref_no FROM returns
        UNION ALL SELECT 'cheque', ref_no FROM cheques
        ORDER BY k, ref_no""",

    "لقطات القواعد": """
        SELECT ref_no, rules_snapshot FROM invoices ORDER BY ref_no""",
}


def collect(env):
    """يشغّل كل الاستعلامات في عملية منفصلة بالبيئة المطلوبة."""
    code = (
        "import json,os,sys;sys.path.insert(0,%r);"
        "from app import app;from db import q;"
        "CH=json.load(open(%r));out={}\n"
        "with app.app_context():\n"
        "    for k,sql in CH.items():\n"
        "        out[k]=[{c:(round(v,6) if isinstance(v,float) else v) for c,v in dict(r).items()}"
        " for r in q(sql)]\n"
        "print(json.dumps(out,ensure_ascii=False,default=str))"
    ) % (HERE, os.path.join(HERE, "_parity_checks.json"))
    with open(os.path.join(HERE, "_parity_checks.json"), "w", encoding="utf-8") as f:
        json.dump(CHECKS, f, ensure_ascii=False)
    e = dict(os.environ)
    e.update(env)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       env=e, cwd=HERE)
    if r.returncode:
        raise SystemExit(f"فشل جمع البيانات:\n{r.stderr[-2500:]}")
    return json.loads(r.stdout)


def compare(a, b):
    report, failures = {}, 0
    for name in CHECKS:
        ra, rb = a.get(name, []), b.get(name, [])
        diffs = []
        if len(ra) != len(rb):
            diffs.append({"نوع": "عدد الصفوف", "sqlite": len(ra), "postgres": len(rb)})
        for i, (x, y) in enumerate(zip(ra, rb)):
            for col in x:
                xv, yv = x[col], y.get(col)
                if isinstance(xv, float) and isinstance(yv, float):
                    if abs(xv - yv) > 1e-9:
                        diffs.append({"صف": i, "عمود": col, "sqlite": xv, "postgres": yv})
                elif xv != yv:
                    diffs.append({"صف": i, "عمود": col, "sqlite": xv, "postgres": yv})
        report[name] = {"صفوف": len(ra), "فروق": diffs}
        failures += len(diffs)
    return report, failures


def main():
    pg_url = os.environ.get("PARITY_PG_URL")
    if not pg_url:
        raise SystemExit("حدّد PARITY_PG_URL لقاعدة PostgreSQL تجريبية قابلة للتخلص.")
    sq_path = os.environ.get("PARITY_SQLITE", "/tmp/parity_sqlite.db")

    print("جمع النتائج من SQLite…")
    a = collect({"SALES_DB": sq_path, "MABEE3AT_DB_URL": ""})
    print("جمع النتائج من PostgreSQL…")
    b = collect({"MABEE3AT_DB_URL": pg_url})

    report, failures = compare(a, b)
    print(f"\n{'المخرج المالي':<34} {'صفوف':>6}  الحالة")
    print("-" * 62)
    for name, r in report.items():
        mark = "✅ متطابق" if not r["فروق"] else f"❌ {len(r['فروق'])} فرق"
        print(f"{name:<34} {r['صفوف']:>6}  {mark}")
        for d in r["فروق"][:5]:
            print(f"      {d}")
    print("-" * 62)
    print(f"إجمالي الفروق: {failures}")

    if "--report" in sys.argv:
        out = sys.argv[sys.argv.index("--report") + 1]
        with open(out, "w", encoding="utf-8") as f:
            json.dump({"فروق_كلية": failures, "تفاصيل": report}, f,
                      ensure_ascii=False, indent=2)
        print(f"التقرير: {out}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
