"""
طبقة اللهجة: تعزل كل ما يخص محرّك قاعدة البيانات عن منطق الأعمال.

الهدف أن يعمل نفس engine.py ونفس app.py حرفياً على:
  - SQLite     (الوضع الحالي والاختبارات المرجعية)
  - PostgreSQL (قاعدة الإنتاج المستقبلية)

كل استعلامات التطبيق مكتوبة بلهجة SQLite. هذه الوحدة تترجمها إلى PostgreSQL
عند التنفيذ، فلا يُنسخ منطق الأعمال ولا تُنشأ نسخة ثانية من engine.py.
"""
import re

SQLITE = "sqlite"
POSTGRES = "postgres"


# ============================================================
#  ترجمة SQL
# ============================================================
def _split_args(s):
    """يقسّم قائمة وسائط دالة مع احترام الأقواس والنصوص المتداخلة."""
    parts, depth, cur, quote = [], 0, [], None
    for ch in s:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "'\"":
            quote = ch
            cur.append(ch)
        elif ch == "(":
            depth += 1
            cur.append(ch)
        elif ch == ")":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return [p.strip() for p in parts]


def _rewrite_calls(sql, name, handler):
    """
    يبدّل كل استدعاء لدالة باسم معيّن باستخدام مطابقة أقواس حقيقية،
    فيتعامل مع الحالات المتداخلة مثل ROUND(SUM(a-b), 2).
    """
    out, i, low = [], 0, sql.lower()
    target = name.lower() + "("
    while True:
        j = low.find(target, i)
        # لا نلمس اسماً أطول مثل my_round(
        while j > 0 and (sql[j - 1].isalnum() or sql[j - 1] == "_"):
            j = low.find(target, j + 1)
        if j == -1:
            out.append(sql[i:])
            return "".join(out)
        out.append(sql[i:j])
        k = j + len(target)
        depth = 1
        quote = None
        while k < len(sql) and depth:
            ch = sql[k]
            if quote:
                if ch == quote:
                    quote = None
            elif ch in "'\"":
                quote = ch
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            k += 1
        inner = sql[j + len(target):k - 1]
        out.append(handler(_split_args(inner)))
        i = k


def _round(args):
    """ROUND(x, n) → دالة m_round المطابقة لسلوك SQLite بتة ببتة."""
    if len(args) == 1:
        return f"m_round({args[0]}, 0)"
    return f"m_round({args[0]}, {args[1]})"


def _julianday(args):
    """
    julianday(x) → عدد اليوم اليولياني كعدد عشري، مطابقاً لـ SQLite تماماً.

    ملاحظة مهمة: julianday('now') في SQLite يحمل كسر اليوم (الساعة الحالية)،
    ولا يساوي منتصف ليل اليوم. استبدالها بـ CURRENT_DATE يغيّر عدد الأيام
    المتبقية بمقدار يوم كامل، فيجب محاكاة السلوك الأصلي حرفياً.
    """
    a = args[0].strip()
    if a.lower() in ("'now'", '"now"'):
        return "m_julianday_now()"
    return f"m_julianday({a})"


def _date(args):
    a = args[0].strip()
    if a.lower() in ("'now'", '"now"'):
        return "CURRENT_DATE"
    return f"({a})::date"


def _datetime(args):
    a = args[0].strip()
    if a.lower() in ("'now'", '"now"'):
        return "to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS')"
    return f"to_char(({a})::timestamp, 'YYYY-MM-DD HH24:MI:SS')"


_CASTINT = "CAST("


def _cast_int(args):
    """
    CAST(x AS INTEGER): SQLite يقصّ نحو الصفر، بينما PostgreSQL يقرّب.
    الفرق يظهر في عدّ الأيام السالبة (فواتير لم تستحق بعد).
    """
    inner = args[0]
    m = re.search(r"(?is)^(.*)\s+AS\s+INTEGER\s*$", inner)
    if not m:
        return f"CAST({inner})"
    return f"trunc(({m.group(1).strip()}))::bigint"


def _placeholders(sql):
    """
    ? → %s مع تجاهل علامات الاستفهام داخل النصوص.

    وتُضاعَف كل علامة % حرفية (مثل LIKE 'ABC-%') لأن psycopg يفسّرها
    كبداية محدّد ويرفض الجملة. المضاعفة تُلغى عند التنفيذ فتعود % واحدة.
    """
    out, quote = [], None
    for ch in sql:
        if quote:
            out.append("%%" if ch == "%" else ch)
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
            out.append(ch)
        elif ch == "?":
            out.append("%s")
        elif ch == "%":
            out.append("%%")
        else:
            out.append(ch)
    return "".join(out)


_LIKE = re.compile(r"(?<![A-Za-z_])LIKE(?![A-Za-z_])", re.I)


def to_postgres(sql):
    """يترجم استعلاماً مكتوباً بلهجة SQLite إلى PostgreSQL."""
    sql = _rewrite_calls(sql, "CAST", _cast_int)
    sql = _rewrite_calls(sql, "ROUND", _round)
    sql = _rewrite_calls(sql, "julianday", _julianday)
    sql = _rewrite_calls(sql, "datetime", _datetime)
    sql = _rewrite_calls(sql, "date", _date)
    # LIKE في SQLite غير حسّاس لحالة الأحرف اللاتينية؛ ILIKE يطابق ذلك
    sql = _LIKE.sub("ILIKE", sql)
    sql = sql.replace("INSERT OR IGNORE INTO", "INSERT INTO")
    sql = _order_nulls(sql)
    return _placeholders(sql)


def _order_nulls(sql):
    """
    ترتيب NULL: SQLite يضعها أولاً تصاعدياً وأخيراً تنازلياً، وPostgreSQL بالعكس.
    يُطبَّق على ORDER BY الخارجي فقط (عمق أقواس صفر) حتى لا يُمس ترتيب داخل استعلام فرعي.
    """
    low, depth, quote, idx = sql.lower(), 0, None, -1
    i = 0
    while i < len(sql):
        ch = sql[i]
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and low.startswith("order by", i):
            idx = i
        i += 1
    if idx == -1:
        return sql
    rest = sql[idx + len("order by"):]
    # نهاية جملة الترتيب: LIMIT خارجي أو نهاية النص
    d, qt, cut = 0, None, len(rest)
    j = 0
    lr = rest.lower()
    while j < len(rest):
        ch = rest[j]
        if qt:
            if ch == qt:
                qt = None
        elif ch in "'\"":
            qt = ch
        elif ch == "(":
            d += 1
        elif ch == ")":
            d -= 1
        elif d == 0 and lr.startswith("limit", j):
            cut = j
            break
        j += 1
    body, tail = rest[:cut], rest[cut:]
    if "nulls" in body.lower():
        return sql
    items = []
    for part in _split_args(body):
        p = part.strip()
        if not p:
            continue
        items.append(p + (" NULLS FIRST" if re.search(r"(?i)\bDESC\b", p) else " NULLS LAST"))
    if not items:
        return sql
    return sql[:idx] + "ORDER BY " + ", ".join(items) + " " + tail


# ============================================================
#  الدوال المساعدة داخل PostgreSQL
# ============================================================
# m_round يطابق ROUND في SQLite تماماً:
#   SQLite يقرّب القيمة الثنائية الدقيقة، ونصف الحالات بعيداً عن الصفر.
#   التحويل إلى numeric في PostgreSQL يمرّ بأقصر تمثيل عشري فيغيّر النتيجة
#   (مثال: ROUND(2.675,2) = 2.67 في SQLite و 2.68 عبر numeric).
#   نقطة النصف الحقيقية عند d خانات تحدث فقط إذا كانت القيمة مضاعفاً فردياً
#   لـ 1/2^(d+1)، والضرب في قوة 2 دقيق تماماً في الثنائي فالكشف موثوق.
PG_FUNCTIONS = """
CREATE OR REPLACE FUNCTION m_round(v double precision, d integer)
RETURNS double precision LANGUAGE plpgsql IMMUTABLE STRICT AS $fn$
DECLARE w double precision; fmt text;
BEGIN
  w := v * power(2::float8, d + 1);
  IF w = trunc(w) AND mod(abs(trunc(w))::numeric, 2::numeric) = 1 THEN
    RETURN (trunc(v * power(10::float8, d))
            + CASE WHEN v >= 0 THEN 1 ELSE -1 END) / power(10::float8, d);
  END IF;
  fmt := 'FM99999999999999990' || CASE WHEN d > 0 THEN '.' || repeat('0', d) ELSE '' END;
  RETURN to_char(v, fmt)::float8;
END $fn$;
"""

# julianday مطابق لـ SQLite: عدد يولياني عشري، و'now' يحمل كسر اليوم بتوقيت UTC.
PG_FUNCTIONS += """
CREATE OR REPLACE FUNCTION m_julianday(v text)
RETURNS double precision LANGUAGE sql IMMUTABLE STRICT AS $fn$
  SELECT EXTRACT(EPOCH FROM (v::timestamp)) / 86400.0 + 2440587.5
$fn$;

CREATE OR REPLACE FUNCTION m_julianday(v timestamp)
RETURNS double precision LANGUAGE sql IMMUTABLE STRICT AS $fn$
  SELECT EXTRACT(EPOCH FROM v) / 86400.0 + 2440587.5
$fn$;

CREATE OR REPLACE FUNCTION m_julianday(v date)
RETURNS double precision LANGUAGE sql IMMUTABLE STRICT AS $fn$
  SELECT EXTRACT(EPOCH FROM v::timestamp) / 86400.0 + 2440587.5
$fn$;

CREATE OR REPLACE FUNCTION m_julianday_now()
RETURNS double precision LANGUAGE sql STABLE AS $fn$
  SELECT EXTRACT(EPOCH FROM (now() AT TIME ZONE 'utc')) / 86400.0 + 2440587.5
$fn$;
"""

# مفتاح قفل استشاري واحد يسلسل كل العمليات المالية.
# هذا هو المكافئ الصحيح لـ BEGIN IMMEDIATE في SQLite: لا يستطيع كاتبان
# أن يقرآ نفس الرصيد ثم يكتبا فوق بعضهما. لا علاقة له بـ WAL.
WRITE_LOCK_KEY = 728_314_509
