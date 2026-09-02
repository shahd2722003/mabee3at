"""
نقطة الدخول للنشر السحابي — خادم WSGI إنتاجي، لا خادم Flask التطويري.

Render وغيره يمرّران المنفذ في متغيّر البيئة PORT.
    python wsgi.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

VENDOR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vendor")
if os.path.isdir(VENDOR) and VENDOR not in sys.path:
    sys.path.insert(0, VENDOR)

from app import app  # noqa: E402
from db import init_db
init_db()

if __name__ == "__main__":
    from waitress import serve

    port = int(os.environ.get("PORT", "10000"))
    threads = int(os.environ.get("MABEE3AT_THREADS", "4"))
    print(f"Mabee3at يستمع على 0.0.0.0:{port} عبر waitress ({threads} خيوط)", flush=True)
    serve(app, host="0.0.0.0", port=port, threads=threads,
          ident="Mabee3at", clear_untrusted_proxy_headers=True)
