"""
فلاتر موحّدة لكل صفحات النظام + تصدير النتائج بعد الفلترة.
تُحفظ آخر فلاتر لكل صفحة في جلسة المستخدم.
"""
import csv
import io

from flask import Response, request, session

# كل المفاتيح المدعومة؛ كل صفحة تختار ما يناسبها
ALL_KEYS = ["q", "customer_id", "rep_id", "date_from", "date_to", "due_from", "due_to",
            "method", "status", "kind", "item_id", "disc_min", "disc_max",
            "days_min", "days_max", "move_type", "entry_type"]


def get_filters(page, keys):
    """يقرأ الفلاتر من الرابط، أو من الجلسة إن لم تُرسل، ويحفظ آخر استخدام."""
    store = f"flt_{page}"
    if request.args.get("clear"):
        session.pop(store, None)
        return {k: "" for k in keys}

    sent = any(k in request.args for k in keys) or request.args.get("apply")
    if sent:
        f = {k: (request.args.get(k) or "").strip() for k in keys}
        session[store] = f
    else:
        saved = session.get(store) or {}
        f = {k: saved.get(k, "") for k in keys}
    return f


def as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def as_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class Where:
    """بنّاء شروط SQL بسيط وآمن."""

    def __init__(self, *base):
        self.parts = list(base)
        self.args = []

    def add(self, sql, *args):
        self.parts.append(sql)
        self.args.extend(args)
        return self

    def eq(self, col, val, cast=as_int):
        v = cast(val) if cast else (val or None)
        if v not in (None, ""):
            self.add(f"{col} = ?", v)
        return self

    def txt(self, col, val):
        if val:
            self.add(f"{col} = ?", val)
        return self

    def gte(self, col, val, cast=as_float):
        v = cast(val)
        if v is not None:
            self.add(f"{col} >= ?", v)
        return self

    def lte(self, col, val, cast=as_float):
        v = cast(val)
        if v is not None:
            self.add(f"{col} <= ?", v)
        return self

    def date_between(self, col, a, b):
        if a:
            self.add(f"date({col}) >= date(?)", a)
        if b:
            self.add(f"date({col}) <= date(?)", b)
        return self

    def search(self, cols, val):
        if val:
            like = f"%{val}%"
            self.add("(" + " OR ".join(f"{c} LIKE ?" for c in cols) + ")", *([like] * len(cols)))
        return self

    def sql(self, keyword="WHERE"):
        return (keyword + " " + " AND ".join(self.parts)) if self.parts else ""

    def tail(self):
        """للاستخدام بعد شرط موجود: يبدأ بـ AND."""
        return (" AND " + " AND ".join(self.parts)) if self.parts else ""


def wants_export():
    return request.args.get("export") == "csv"


def csv_response(filename, columns, rows):
    """
    columns: [(العنوان, المفتاح أو دالة)]
    يُصدَّر بترميز UTF-8 مع BOM ليفتح صحيحاً في Excel العربي.
    """
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([c[0] for c in columns])
    for r in rows:
        line = []
        for _, key in columns:
            v = key(r) if callable(key) else (r[key] if key in r.keys() else "")
            line.append("" if v is None else v)
        w.writerow(line)
    data = "\ufeff" + buf.getvalue()
    return Response(data, mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}.csv"'})
