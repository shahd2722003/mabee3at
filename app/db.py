"""
طبقة الوصول لقاعدة البيانات — تدعم SQLite و PostgreSQL بنفس الواجهة.

يُختار المحرّك من متغيّر البيئة:
    MABEE3AT_DB_URL=postgresql://user:pass@host/db   → PostgreSQL
    (غير محدَّد)                                      → SQLite على SALES_DB

منطق الأعمال في engine.py و app.py يكتب SQL بلهجة SQLite دائماً،
ووحدة dialect.py تترجمه إلى PostgreSQL عند التنفيذ. لا توجد نسخة ثانية
من منطق الأعمال ولا فرع خاص بمحرّك داخل engine.py.
"""
import json
import os
import re
import sqlite3
from flask import g

import dialect

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("SALES_DB", os.path.join(BASE_DIR, "..", "data", "sales.db"))
SCHEMA_PATH = os.path.join(BASE_DIR, "schema.sql")
PG_SCHEMA_PATH = os.path.join(BASE_DIR, "schema_pg.sql")

DB_URL = os.environ.get("MABEE3AT_DB_URL", "").strip()
ENGINE = dialect.POSTGRES if DB_URL.startswith(("postgres://", "postgresql://")) else dialect.SQLITE

# مهلة انتظار القفل: الكاتب الثاني ينتظر بدل أن يفشل فوراً
BUSY_TIMEOUT_MS = 15000


# الجداول التي لا تحتوي عمود id، فلا يُضاف لها RETURNING
_NO_ID_TABLES = {"counters", "settings"}
_INSERT_RE = re.compile(r"^\s*INSERT\s+INTO\s+([A-Za-z_][A-Za-z_0-9]*)", re.I)


class PgCursor:
    """
    يغلّف مؤشّر psycopg ليقدّم نفس واجهة sqlite3.Cursor التي يستعملها التطبيق:
    rowcount و lastrowid و fetchone/fetchall.

    lastrowid يأتي من RETURNING id في نفس جملة الإدراج، لا من lastval().
    lastval() جملة منفصلة، وقد تُنفَّذ على اتصال آخر خلف مجمّع اتصالات
    يعمل بوضع المعاملة، فتُرجع معرّفاً خاطئاً بصمت.
    """

    __slots__ = ("_cur", "_inserted_id")

    def __init__(self, cur, inserted_id=None):
        self._cur = cur
        self._inserted_id = inserted_id

    @property
    def rowcount(self):
        return self._cur.rowcount

    @property
    def lastrowid(self):
        if self._inserted_id is None:
            raise RuntimeError(
                "لا يوجد معرّف مُعاد: الجملة ليست INSERT في جدول له عمود id، "
                "أو لم تُدرَج أي صفوف.")
        return self._inserted_id

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def __iter__(self):
        return iter(self._cur)


class PgConnection:
    """اتصال PostgreSQL بواجهة sqlite3.Connection التي يعتمد عليها التطبيق."""

    def __init__(self, conn):
        self.raw = conn
        self.raw.autocommit = True          # المعاملات تُدار يدوياً عبر begin_write
        self._in_tx = False

    @property
    def in_transaction(self):
        return self._in_tx

    def execute(self, sql, params=()):
        pg_sql = dialect.to_postgres(sql)
        inserted = None
        m = _INSERT_RE.match(pg_sql)
        wants_id = (m and m.group(1).lower() not in _NO_ID_TABLES
                    and "returning" not in pg_sql.lower())
        if wants_id:
            pg_sql += " RETURNING id"
        cur = self.raw.cursor()
        # بلا معاملات: نمرّر None حتى لا يفحص psycopg الجملة بحثاً عن محدّدات،
        # فتمرّ علامات % الحرفية كما هي.
        cur.execute(pg_sql, tuple(params) if params else None)
        if wants_id:
            row = cur.fetchone()
            inserted = row["id"] if row else None
        return PgCursor(cur, inserted)

    def begin(self):
        self.raw.autocommit = False
        self._in_tx = True

    def commit(self):
        if self._in_tx:
            self.raw.commit()
            self.raw.autocommit = True
            self._in_tx = False

    def rollback(self):
        if self._in_tx:
            self.raw.rollback()
            self.raw.autocommit = True
            self._in_tx = False

    def close(self):
        """يعيد الاتصال إلى المجمّع بدل إغلاقه، بعد ضمان عدم ترك معاملة مفتوحة."""
        try:
            if self._in_tx:
                self.raw.rollback()
                self._in_tx = False
            self.raw.autocommit = True
        except Exception:
            pass          # اتصال ميت: المجمّع سيتخلّص منه عند الفحص
        try:
            get_pool().putconn(self.raw)
        except Exception:
            try:
                self.raw.close()
            except Exception:
                pass


# ---------- مجمّع اتصالات PostgreSQL ----------
# فتح اتصال جديد لكل طلب مقبول محلياً، لكنه عبر الشبكة يضيف مصافحة TLS
# كاملة لكل نقرة. المجمّع يعيد استخدام الاتصالات ويتحقق من صلاحيتها قبل
# تسليمها، وهو ضروري لأن قواعد البيانات المُدارة قد تُعاد تشغيلها فجأة.
_pool = None
POOL_MIN = int(os.environ.get("MABEE3AT_POOL_MIN", "1"))
POOL_MAX = int(os.environ.get("MABEE3AT_POOL_MAX", "5"))


def get_pool():
    global _pool
    if _pool is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool
        _pool = ConnectionPool(
            DB_URL,
            min_size=POOL_MIN,
            max_size=POOL_MAX,
            kwargs={"row_factory": dict_row, "autocommit": True},
            # يتحقق من حياة الاتصال قبل تسليمه بدل أن يفشل الطلب
            check=ConnectionPool.check_connection,
            timeout=15,          # مهلة انتظار اتصال حر
            max_lifetime=1800,   # يجدّد الاتصالات كل نصف ساعة
            max_idle=300,
            open=True,
        )
    return _pool


def close_pool():
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def _connect_pg():
    return PgConnection(get_pool().getconn())


def _connect_sqlite():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    # isolation_level=None: تحكّم يدوي كامل في المعاملات.
    conn = sqlite3.connect(DB_PATH, timeout=BUSY_TIMEOUT_MS / 1000.0,
                           isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    # WAL: القرّاء لا يعطّلون الكاتب ولا العكس
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


def get_db():
    if "db" not in g:
        g.db = _connect_pg() if ENGINE == dialect.POSTGRES else _connect_sqlite()
    return g.db


def begin_write():
    """
    يفتح معاملة كتابة حصرية تسلسل العمليات المالية.

    SQLite     : BEGIN IMMEDIATE يأخذ قفل الكتابة فوراً.
    PostgreSQL : لا مكافئ لـ BEGIN IMMEDIATE. نفتح معاملة ثم نأخذ قفلاً
                 استشارياً على مستوى المعاملة (pg_advisory_xact_lock)،
                 وهو يُحرَّر تلقائياً عند الاعتماد أو الإلغاء.

    آمن للاستدعاء المتداخل: لا يفتح معاملة داخل معاملة.
    """
    db = get_db()
    if not db.in_transaction:
        if ENGINE == dialect.POSTGRES:
            db.begin()
            db.raw.cursor().execute("SELECT pg_advisory_xact_lock(%s)",
                                    (dialect.WRITE_LOCK_KEY,))
        else:
            db.execute("BEGIN IMMEDIATE")
    return db


def rollback():
    db = g.get("db")
    if db is not None and db.in_transaction:
        db.rollback()


def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        # أي معاملة لم تُعتمد صراحةً تُلغى بالكامل — لا كتابات جزئية
        if db.in_transaction:
            db.rollback()
        db.close()


def init_db(conn=None):
    if ENGINE == dialect.POSTGRES:
        return _init_pg()
    own = conn is None
    if own:
        os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.commit()
    if own:
        conn.close()


def _init_pg():
    """ينشئ المخطّط والدوال المساعدة في PostgreSQL."""
    import psycopg
    with psycopg.connect(DB_URL, autocommit=True) as c:
        # الدوال المساعدة أولاً — العروض تستعملها
        c.execute(dialect.PG_FUNCTIONS)
        with open(PG_SCHEMA_PATH, encoding="utf-8") as f:
            c.execute(f.read())


# ---------- اختصارات ----------
def q(sql, args=()):
    return get_db().execute(sql, args).fetchall()


def q1(sql, args=()):
    return get_db().execute(sql, args).fetchone()


def ex(sql, args=()):
    cur = get_db().execute(sql, args)
    return cur


def commit():
    db = get_db()
    if db.in_transaction:
        db.commit()


# ---------- الأرقام المرجعية ----------
PREFIXES = {
    "invoice": "INV",
    "return": "RET",
    "collection": "REC",
    "cheque": "CHQ",
    "stock": "STK",
}


def next_ref(name, year=None):
    """
    يولّد رقماً مرجعياً متسلسلاً مثل INV-2026-000123.
    يجب أن يُستدعى داخل معاملة كتابة حصرية، وإلا أمكن أن يحصل
    طلبان متزامنان على نفس الرقم.
    """
    db = begin_write()
    if ENGINE == dialect.POSTGRES:
        db.execute("INSERT INTO counters(name, seq) VALUES (?, 0) ON CONFLICT (name) DO NOTHING",
                   (name,))
    else:
        db.execute("INSERT OR IGNORE INTO counters(name, seq) VALUES (?, 0)", (name,))
    # زيادة وقراءة في جملة ذرّية واحدة: لا نافذة سباق بين القراءة والكتابة.
    # RETURNING مدعوم في PostgreSQL و SQLite 3.35+.
    seq = db.execute("UPDATE counters SET seq = seq + 1 WHERE name = ? RETURNING seq",
                     (name,)).fetchone()["seq"]
    from datetime import date
    y = year or date.today().year
    return f"{PREFIXES.get(name, name.upper()[:3])}-{y}-{seq:06d}"


# ---------- الإعدادات ----------
def get_setting(key, default=None):
    row = q1("SELECT value FROM settings WHERE key = ?", (key,))
    if not row:
        return default
    return json.loads(row["value"])


def set_setting(key, value, user_id=None):
    ex(
        """INSERT INTO settings(key, value, updated_at, updated_by)
           VALUES (?, ?, datetime('now'), ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                updated_at = excluded.updated_at, updated_by = excluded.updated_by""",
        (key, json.dumps(value, ensure_ascii=False), user_id),
    )


# ---------- سجل التدقيق ----------
def audit(action, entity, entity_id=None, entity_ref=None, details=None, reason=None, user=None):
    from flask import has_request_context, session
    u = user or {}
    sess = session if has_request_context() else {}
    uid = u.get("id") or sess.get("uid")
    uname = u.get("full_name") or sess.get("full_name")
    ex(
        """INSERT INTO audit_log(user_id, user_name, action, entity, entity_id, entity_ref, details, reason)
           VALUES (?,?,?,?,?,?,?,?)""",
        (uid, uname, action, entity, entity_id, entity_ref,
         json.dumps(details, ensure_ascii=False) if isinstance(details, (dict, list)) else details,
         reason),
    )
