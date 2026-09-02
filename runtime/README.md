# runtime/

نسخة بايثون مستقلة مضمّنة مع البرنامج، فلا يحتاج جهاز المستخدم إلى تثبيت بايثون.

- `python-win-x64/` — CPython 3.12.14 لويندوز x64
  من [astral-sh/python-build-standalone](https://github.com/astral-sh/python-build-standalone)
  إصدار `20260825`، بناء `install_only_stripped`، رخصة PSF (انظر `LICENSE.txt` بالداخل).

حُذفت منها المكوّنات غير المستخدمة (Tcl/Tk، حزمة الاختبارات، IDLE، ملفات التطوير)
لتصغير حجم الحزمة. مكتبة `sqlite3` وشهادات SSL باقية لأن التطبيق يعتمد عليهما.

`main.js` يبحث عن `runtime/python-win-x64/python.exe` أولاً، ويستخدم بايثون
المثبّت على الجهاز فقط إن لم يجد النسخة المضمّنة.
