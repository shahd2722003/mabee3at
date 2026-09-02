# تشغيل النظام على PostgreSQL

## اختيار المحرّك

```bash
# SQLite (الوضع الحالي — الافتراضي)
python3 server.py

# PostgreSQL
export MABEE3AT_DB_URL="postgresql://user:pass@host:5432/mabee3at"
python3 seed.py      # ينشئ المخطّط والدوال والبيانات التجريبية
python3 server.py
```

`MABEE3AT_DB_URL` قياسي وغير مرتبط بأي مزوّد — يعمل مع Render أو Supabase
أو خادم PostgreSQL خاص بلا أي تغيير في الكود.

## المعمارية

منطق الأعمال في `engine.py` و`app.py` يكتب SQL **بلهجة SQLite دائماً**.
`dialect.py` يترجمه إلى PostgreSQL عند التنفيذ داخل `db.py`.
لا توجد نسخة ثانية من منطق الأعمال، ولا فرع خاص بمحرّك في `engine.py`.

## الملفات

| الملف | الدور |
|---|---|
| `dialect.py` | ترجمة SQL + الدوال المساعدة في PostgreSQL |
| `schema_pg.sql` | مخطّط PostgreSQL مولَّد من `schema.sql` |
| `parity.py` | مقارنة كل مخرج مالي بين المحرّكين |
| `tests_concurrency.py` | اختبارات التزامن على PostgreSQL |

## الاختبارات

```bash
# مالية — على المحرّكين
MABEE3AT_DB_URL= python3 tests.py                 # SQLite
MABEE3AT_DB_URL=postgresql://... python3 tests.py # PostgreSQL

# تزامن (PostgreSQL فقط)
MABEE3AT_DB_URL=postgresql://... python3 tests_concurrency.py

# تطابق مالي بين المحرّكين
PARITY_PG_URL=postgresql://... python3 parity.py --report parity.json
```

## ثلاث فروق حرجة عولجت

| السلوك | SQLite | PostgreSQL الخام | المعالجة |
|---|---|---|---|
| `ROUND(2.675,2)` | 2.67 | 2.68 عبر `numeric` | دالة `m_round` — مثبتة على 21,023 قيمة |
| `julianday('now')` | يحمل كسر اليوم | `CURRENT_DATE` = منتصف الليل | `m_julianday_now()` |
| `CAST(x AS INTEGER)` | يقصّ نحو الصفر | يقرّب | `trunc()::bigint` |

الثلاثة كانت ستغيّر الأرقام بصمت بلا أي رسالة خطأ.

## الأقفال

`BEGIN IMMEDIATE` لا مكافئ له في PostgreSQL ولم يُنسخ.
البديل: `pg_advisory_xact_lock` على مستوى المعاملة، يُحرَّر تلقائياً عند
الاعتماد أو الإلغاء. التحديثات الشرطية بفحص `rowcount` تعمل على المحرّكين.

الأرقام المرجعية: `UPDATE counters SET seq=seq+1 ... RETURNING seq` —
جملة ذرّية واحدة بلا نافذة سباق. الأرقام التاريخية لا يُعاد ترقيمها.
