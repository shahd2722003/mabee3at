-- ============================================================
--  نظام إدارة مبيعات شركة ملابس  |  مخطط قاعدة البيانات
--  SQLite (متوافق مع الترحيل إلى PostgreSQL بتعديلات بسيطة)
-- ============================================================
PRAGMA foreign_keys = ON;

-- ---------- المستخدمون والصلاحيات ----------
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    full_name     TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('admin','accountant','rep')),
    rep_id        INTEGER REFERENCES reps(id),
    is_active     INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------- المندوبون ----------
CREATE TABLE IF NOT EXISTS reps (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    code       TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL,
    phone      TEXT,
    hire_date  TEXT,
    is_active  INTEGER NOT NULL DEFAULT 1,
    notes      TEXT
);

-- ---------- الأصناف ----------
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    color       TEXT,
    size        TEXT,
    sale_price  REAL NOT NULL DEFAULT 0,
    cost_price  REAL NOT NULL DEFAULT 0,
    is_active   INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_items_name ON items(name);

-- ---------- العملاء ----------
CREATE TABLE IF NOT EXISTS customers (
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
    grace_days              INTEGER,             -- سماحية خاصة بالعميل (NULL = الافتراضي العام)
    is_active               INTEGER NOT NULL DEFAULT 1,
    created_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------- الفواتير ----------
-- invoice_date = التاريخ التاريخي/الفعلي للعملية (لا يختلط بـ created_at)
CREATE TABLE IF NOT EXISTS invoices (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ref_no          TEXT NOT NULL UNIQUE,
    invoice_date    TEXT NOT NULL,
    customer_id     INTEGER NOT NULL REFERENCES customers(id),
    rep_id          INTEGER NOT NULL REFERENCES reps(id),
    invoice_kind    TEXT NOT NULL DEFAULT 'detailed' CHECK (invoice_kind IN ('detailed','aggregate')),
    external_ref    TEXT,                        -- رقم مرجعي خارجي اختياري
    discount_pct    REAL NOT NULL DEFAULT 0,     -- قابل للتعديل داخل الفاتورة فقط
    credit_days     INTEGER NOT NULL DEFAULT 0,  -- قابل للتعديل داخل الفاتورة فقط
    payment_method  TEXT NOT NULL CHECK (payment_method IN ('cash','credit','cheque','transfer')),
    due_date        TEXT NOT NULL,
    subtotal        REAL NOT NULL DEFAULT 0,
    discount_amount REAL NOT NULL DEFAULT 0,
    total           REAL NOT NULL DEFAULT 0,     -- الإجمالي بعد الخصم
    returned_total  REAL NOT NULL DEFAULT 0,     -- إجمالي المرتجعات على الفاتورة
    collected_total REAL NOT NULL DEFAULT 0,     -- إجمالي المخصص لها من التحصيلات
    late_reversed   REAL NOT NULL DEFAULT 0,     -- الرصيد الذي سبق عكس عمولته للتأخير
    status          TEXT NOT NULL DEFAULT 'posted' CHECK (status IN ('draft','posted','void')),
    is_historical   INTEGER NOT NULL DEFAULT 0,
    rules_snapshot  TEXT NOT NULL,               -- JSON: قواعد العمولة/البونص وقت الإنشاء
    notes           TEXT,
    created_by      INTEGER REFERENCES users(id),
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    posted_at       TEXT
);
CREATE INDEX IF NOT EXISTS ix_inv_cust ON invoices(customer_id, invoice_date);
CREATE INDEX IF NOT EXISTS ix_inv_rep  ON invoices(rep_id, invoice_date);
CREATE INDEX IF NOT EXISTS ix_inv_kind ON invoices(invoice_kind);

CREATE TABLE IF NOT EXISTS invoice_lines (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    item_id    INTEGER NOT NULL REFERENCES items(id),
    qty        REAL NOT NULL,
    unit_price REAL NOT NULL,
    unit_cost  REAL NOT NULL DEFAULT 0,
    line_total REAL NOT NULL
);

-- ---------- المرتجعات ----------
CREATE TABLE IF NOT EXISTS returns (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ref_no       TEXT NOT NULL UNIQUE,
    return_date  TEXT NOT NULL,
    invoice_id   INTEGER NOT NULL REFERENCES invoices(id),
    customer_id  INTEGER NOT NULL REFERENCES customers(id),
    rep_id       INTEGER NOT NULL REFERENCES reps(id),
    subtotal     REAL NOT NULL DEFAULT 0,
    discount_pct REAL NOT NULL DEFAULT 0,
    total        REAL NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'posted' CHECK (status IN ('posted','void')),
    notes        TEXT,
    created_by   INTEGER REFERENCES users(id),
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS return_lines (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    return_id  INTEGER NOT NULL REFERENCES returns(id) ON DELETE CASCADE,
    item_id    INTEGER NOT NULL REFERENCES items(id),
    qty        REAL NOT NULL,
    unit_price REAL NOT NULL,
    unit_cost  REAL NOT NULL DEFAULT 0,
    line_total REAL NOT NULL
);

-- ---------- المخزون ----------
-- qty موجبة = دخول، سالبة = خروج
CREATE TABLE IF NOT EXISTS stock_moves (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    move_date  TEXT NOT NULL,
    item_id    INTEGER NOT NULL REFERENCES items(id),
    move_type  TEXT NOT NULL CHECK (move_type IN ('opening','purchase','sale','return_in','adjustment')),
    qty        REAL NOT NULL,
    unit_cost  REAL NOT NULL DEFAULT 0,
    unit_price REAL NOT NULL DEFAULT 0,
    ref_type   TEXT,
    ref_id     INTEGER,
    ref_no     TEXT,
    notes      TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_stock_item ON stock_moves(item_id, move_date);

-- ---------- الشيكات ----------
CREATE TABLE IF NOT EXISTS cheques (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ref_no        TEXT NOT NULL UNIQUE,
    cheque_number TEXT,
    bank_name     TEXT,
    customer_id   INTEGER NOT NULL REFERENCES customers(id),
    amount        REAL NOT NULL,
    received_date TEXT NOT NULL,
    due_date      TEXT NOT NULL,   -- الأساس الوحيد لحساب العمولة والبونص
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','cleared','bounced')),
    cleared_date  TEXT,
    auto_cleared  INTEGER NOT NULL DEFAULT 0,
    bounce_date   TEXT,
    bounce_reason TEXT,
    notes         TEXT,
    created_by    INTEGER REFERENCES users(id),
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ---------- التحصيلات ----------
CREATE TABLE IF NOT EXISTS collections (
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

-- ---------- تخصيص التحصيلات والمرتجعات على الفواتير (FIFO) ----------
CREATE TABLE IF NOT EXISTS allocations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type   TEXT NOT NULL CHECK (source_type IN ('collection','return')),
    source_id     INTEGER NOT NULL,
    invoice_id    INTEGER NOT NULL REFERENCES invoices(id),
    amount        REAL NOT NULL,
    alloc_date    TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_alloc_inv ON allocations(invoice_id);

-- ---------- قيود العمولة والبونص ----------
CREATE TABLE IF NOT EXISTS commission_entries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    allocation_id   INTEGER REFERENCES allocations(id),
    invoice_id      INTEGER NOT NULL REFERENCES invoices(id),
    rep_id          INTEGER NOT NULL REFERENCES reps(id),
    entry_type      TEXT NOT NULL DEFAULT 'earn'
                    CHECK (entry_type IN ('earn','reversal','late_reversal')),
    base_amount     REAL NOT NULL,          -- قيمة العملية المحصلة
    discount_pct    REAL NOT NULL,
    commission_pct  REAL NOT NULL,
    commission_amt  REAL NOT NULL,
    days_taken      INTEGER,
    bonus_pct       REAL NOT NULL,
    bonus_amt       REAL NOT NULL,
    basis_from      TEXT,   -- التاريخ المرجعي (تاريخ الفاتورة/الاستحقاق)
    basis_to        TEXT,   -- تاريخ التحصيل أو تاريخ استحقاق الشيك
    basis_label     TEXT,
    recognized_on   TEXT NOT NULL,          -- تاريخ استحقاق العمولة
    status          TEXT NOT NULL DEFAULT 'earned' CHECK (status IN ('accrued','earned')),
    calc_trace      TEXT,                   -- شرح نصي لطريقة الحساب
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_comm_rep ON commission_entries(rep_id, recognized_on);

-- ---------- الإعدادات ----------
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,   -- JSON
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_by INTEGER REFERENCES users(id)
);

-- ---------- سجل التدقيق ----------
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL DEFAULT (datetime('now')),
    user_id     INTEGER REFERENCES users(id),
    user_name   TEXT,
    action      TEXT NOT NULL,
    entity      TEXT NOT NULL,
    entity_id   INTEGER,
    entity_ref  TEXT,
    details     TEXT,
    reason      TEXT
);
CREATE INDEX IF NOT EXISTS ix_audit_at ON audit_log(at);

-- ---------- عدادات الأرقام المرجعية ----------
CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    seq  INTEGER NOT NULL DEFAULT 0
);

-- ---------- عروض مساعدة ----------
DROP VIEW IF EXISTS v_item_stock;
CREATE VIEW v_item_stock AS
SELECT i.id AS item_id,
       i.code, i.name, i.color, i.size, i.sale_price, i.cost_price,
       COALESCE(SUM(sm.qty), 0) AS qty_available
FROM items i
LEFT JOIN stock_moves sm ON sm.item_id = i.id
GROUP BY i.id;

DROP VIEW IF EXISTS v_invoice_balance;
CREATE VIEW v_invoice_balance AS
SELECT inv.id AS invoice_id,
       inv.ref_no, inv.invoice_date, inv.due_date, inv.invoice_kind, inv.customer_id, inv.rep_id,
       inv.total,
       inv.returned_total,
       inv.collected_total,
       ROUND(inv.total - inv.returned_total - inv.collected_total, 2) AS outstanding
FROM invoices inv
WHERE inv.status = 'posted';
