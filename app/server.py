"""
نقطة تشغيل الخادم — يستخدمها وضعان:
  - تطبيق سطح المكتب (Electron): يمرّر MABEE3AT_PORT محدَّداً مسبقاً.
  - التشغيل في المتصفح (Start-Mabee3at.bat): يختار منفذاً حراً ويفتح المتصفح.
لا تغيّر أي منطق في التطبيق — تشغّل نفس تطبيق Flask على منفذ محلي فقط.
"""
import os
import socket
import sys
import threading
import webbrowser

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

# مكتبات التطبيق المرفقة (Flask وتوابعه) — تعمل حتى لو لم يُمرَّر PYTHONPATH
VENDOR = os.path.join(os.path.dirname(BASE), "vendor")
if os.path.isdir(VENDOR) and VENDOR not in sys.path:
    sys.path.insert(0, VENDOR)

from app import app  # noqa: E402
from db import DB_PATH, init_db  # noqa: E402

def port_is_free(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def pick_port(host, preferred):
    """يستخدم المنفذ المطلوب، وإن كان مشغولاً يجرّب ما بعده."""
    for candidate in range(preferred, preferred + 25):
        if port_is_free(host, candidate):
            return candidate
    raise SystemExit("كل المنافذ من %d إلى %d مشغولة." % (preferred, preferred + 24))


if __name__ == "__main__":
    host = os.environ.get("MABEE3AT_HOST", "127.0.0.1")

    if os.environ.get("MABEE3AT_PORT"):
        # Electron يحجز منفذاً حراً قبل التشغيل ويمرّره، فيُستخدم كما هو
        port = int(os.environ["MABEE3AT_PORT"])
    else:
        port = pick_port(host, int(os.environ.get("MABEE3AT_BASE_PORT", "5000")))

    if not os.path.exists(DB_PATH):
        with app.app_context():
            init_db()
        print("أُنشئت قاعدة بيانات جديدة في: %s" % DB_PATH, flush=True)
    print("قاعدة البيانات: %s" % DB_PATH, flush=True)

    url = "http://%s:%d/" % ("127.0.0.1" if host == "0.0.0.0" else host, port)
    print("عنوان البرنامج: %s" % url, flush=True)

    if os.environ.get("MABEE3AT_OPEN_BROWSER") == "1":
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    app.run(host=host, port=port, debug=False, threaded=True, use_reloader=False)
