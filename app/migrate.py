"""
ترحيل قاعدة البيانات إلى الإصدار 2 — يحافظ على كل البيانات الموجودة.
شغّله مرة واحدة:  python migrate.py
تشغيله أكثر من مرة آمن (يتخطى ما هو منفَّذ بالفعل).
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import DB_PATH  # noqa: E402


def cols(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def add_col(conn, table, name, decl):
    if name not in cols(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        print(f"  + {table}.{name}")


def table_sql(conn, table):
    r = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                     (table,)).fetchone()
    return r[0] if r else ""


def script(conn, sql):
    """ينفّذ عدة تعليمات بدون executescript (الذي يقطع المعاملة الجارية)."""
    for stmt in [s.strip() for s in sql.split(";")]:
        if stmt:
            conn.execute(stmt)


def rebuild(conn, table, new_ddl, marker):
    """يعيد بناء جدول لتوسيع قيود CHECK مع نقل كل الصفوف."""
    if marker in table_sql(conn, table):
        return
    old = cols(conn, table)
    conn.execute(f"ALTER TABLE {table} RENAME TO _old_{table}")
    script(conn, new_ddl)
    keep = [c for c in cols(conn, table) if c in old]
    cl = ",".join(keep)
    n = conn.execute(f"INSERT INTO {table}({cl}) SELECT {cl} FROM _old_{table}").rowcount
    conn.execute(f"DROP TABLE _old_{table}")
    print(f"  ↻ أعيد بناء {table} ({n} صف)")


NEW_INVOICES = """
CREATE TABLE invoices (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ref_no          TEXT NOT NULL UNIQUE,
    invoice_date    TEXT NOT NULL,
    customer_id     INTEGER NOT NULL REFERENCES customers(id),
    rep_id          INTEGER NOT NULL REFERENCES reps(id),
    invoice_kind    TEXT NOT NULL DEFAULT 'detailed' CHECK (invoice_kind IN ('detailed','aggregate')),
    external_ref    TEXT,
    discount_pct    REAL NOT NULL DEFAULT 0,
    credit_days     INTEGER NOT NULL DEFAULT 0,
    payment_method  TEXT NOT NULL CHECK (payment_method IN ('cash','credit','cheque','transfer')),
    due_date        TEXT NOT NULL,
    subtotal        REAL NOT NULL DEFAULT 0,
    discount_amount REAL NOT NULL DEFAULT 0,
    total           REAL NOT NULL DEFAULT 0,
    returned_total  REAL NOT NULL DEFAULT 0,
    collected_total REAL NOT NULL DEFAULT 0,
    late_reversed   REAL NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'posted' CHECK (status IN ('draft','posted','void')),
    is_historical   INTEGER NOT NULL DEFAULT 0,
    rules_snapshot  TEXT NOT NULL,
    notes           TEXT,
    created_by      INTEGER REFERENCES users(id),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    posted_at       TEXT
);
CREATE INDEX IF NOT EXISTS ix_inv_cust ON invoices(customer_id, invoice_date);
CREATE INDEX IF NOT EXISTS ix_inv_rep  ON invoices(rep_id, invoice_date);
CREATE INDEX IF NOT EXISTS ix_inv_kind ON invoices(invoice_kind);
"""

NEW_CUSTOMERS = """
CREATE TABLE customers (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    code                    TEXT NOT NULL UNIQUE,
    name                    TEXT NOT NULL,
    rep_id                  INTEGER REFERENCES reps(id),
    phone                   TEXT,
    address                 TEXT,
    default_discount_pct    REAL NOT NULL DEFAULT 0,
    default_credit_days     INTEGER NOT NULL DEFAULT 0,
    default_payment_method  TEXT NOT NULL DEFAULT 'credit'
                            CHECK (default_payment_method IN ('cash','credit','cheque','transfer')),
    grace_days              INTEGER,
    is_active               INTEGER NOT NULL DEFAULT 1,
    created_at              TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

NEW_COLLECTIONS = """
CREATE TABLE collections (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ref_no          TEXT NOT NULL UNIQUE,
    collection_date TEXT NOT NULL,
    customer_id     INTEGER NOT NULL REFERENCES customers(id),
    rep_id          INTEGER NOT NULL REFERENCES reps(id),
    method          TEXT NOT NULL CHECK (method IN ('cash','credit','cheque','transfer')),
    amount          REAL NOT NULL,
    allocated_total REAL NOT NULL DEFAULT 0,
    cheque_id       INTEGER REFERENCES cheques(id),
    status          TEXT NOT NULL DEFAULT 'posted' CHECK (status IN ('posted','void')),
    notes           TEXT,
    created_by      INTEGER REFERENCES users(id),
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

NEW_COMMISSION = """
CREATE TABLE commission_entries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    allocation_id   INTEGER REFERENCES allocations(id),
    invoice_id      INTEGER NOT NULL REFERENCES invoices(id),
    rep_id          INTEGER NOT NULL REFERENCES reps(id),
    entry_type      TEXT NOT NULL DEFAULT 'earn'
                    CHECK (entry_type IN ('earn','reversal','late_reversal')),
    base_amount     REAL NOT NULL,
    discount_pct    REAL NOT NULL,
    commission_pct  REAL NOT NULL,
    commission_amt  REAL NOT NULL,
    days_taken      INTEGER,
    bonus_pct       REAL NOT NULL,
    bonus_amt       REAL NOT NULL,
    basis_from      TEXT,
    basis_to        TEXT,
    basis_label     TEXT,
    recognized_on   TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'earned' CHECK (status IN ('accrued','earned')),
    calc_trace      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_comm_rep ON commission_entries(rep_id, recognized_on);
"""

VIEWS = """
CREATE VIEW v_item_stock AS
SELECT i.id AS item_id, i.code, i.name, i.color, i.size, i.sale_price, i.cost_price,
       COALESCE(SUM(sm.qty), 0) AS qty_available
FROM items i LEFT JOIN stock_moves sm ON sm.item_id = i.id GROUP BY i.id;

CREATE VIEW v_invoice_balance AS
SELECT inv.id AS invoice_id, inv.ref_no, inv.invoice_date, inv.due_date, inv.invoice_kind,
       inv.customer_id, inv.rep_id, inv.total, inv.returned_total, inv.collected_total,
       ROUND(inv.total - inv.returned_total - inv.collected_total, 2) AS outstanding
FROM invoices inv WHERE inv.status = 'posted';
"""


def migrate(path=None):
    path = path or DB_PATH
    if not os.path.exists(path):
        print("لا توجد قاعدة بيانات — استخدم seed.py لإنشاء واحدة جديدة.")
        return
    conn = sqlite3.connect(path)
    conn.isolation_level = None          # تحكم يدوي كامل في المعاملة
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("PRAGMA legacy_alter_table = ON")  # لا يعيد كتابة مراجع FK عند إعادة التسمية
    print("ترحيل قاعدة البيانات…")
    conn.execute("BEGIN")
    # العروض تعتمد على الجداول، تُحذف وتُعاد بعد إعادة البناء
    conn.execute("DROP VIEW IF EXISTS v_invoice_balance")
    conn.execute("DROP VIEW IF EXISTS v_item_stock")

    rebuild(conn, "invoices", NEW_INVOICES, "invoice_kind")
    rebuild(conn, "customers", NEW_CUSTOMERS, "grace_days")
    rebuild(conn, "collections", NEW_COLLECTIONS, "'transfer'")
    rebuild(conn, "commission_entries", NEW_COMMISSION, "late_reversal")

    add_col(conn, "cheques", "bounce_date", "TEXT")
    add_col(conn, "cheques", "bounce_reason", "TEXT")
    add_col(conn, "cheques", "auto_cleared", "INTEGER NOT NULL DEFAULT 0")

    script(conn, VIEWS)
    conn.execute("COMMIT")
    bad = conn.execute("PRAGMA foreign_key_check").fetchall()
    conn.close()
    print("تم الترحيل بنجاح." if not bad else f"تحذير: مشاكل مفاتيح خارجية {bad[:3]}")


if __name__ == "__main__":
    migrate()
