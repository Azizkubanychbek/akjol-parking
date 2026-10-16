"""HTTP: панель оператора (/), экран оплаты (/screen) и JSON API."""
import base64
import json
import os
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .kkm import KKMError
from .tariff import calc_fee, format_duration

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
PUBLIC = {"/screen", "/api/screen", "/static/screen.html", "/static/qrcode.min.js"}
TYPES = {".html": "text/html; charset=utf-8", ".js": "application/javascript", ".css": "text/css",
         ".png": "image/png", ".ico": "image/x-icon"}


def make_handler(app):
    cfg = app.cfg
    password = cfg.get("server", {}).get("panel_password") or ""

    class Handler(BaseHTTPRequestHandler):
        server_version = "AkjolParking/1.0"

        def log_message(self, fmt, *args):  # без шума в консоли
            pass

        # --- ответы ---
        def send_json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def send_file(self, name):
            path = os.path.normpath(os.path.join(STATIC, name))
            if not path.startswith(STATIC) or not os.path.isfile(path):
                return self.send_json({"error": "not found"}, 404)
            with open(path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", TYPES.get(os.path.splitext(path)[1], "application/octet-stream"))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

        def authorized(self, path):
            if not password or path in PUBLIC:
                return True
            h = self.headers.get("Authorization", "")
            if h.startswith("Basic "):
                try:
                    user_pw = base64.b64decode(h[6:]).decode("utf-8")
                    if user_pw.split(":", 1)[-1] == password:
                        return True
                except Exception:  # noqa: BLE001
                    pass
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Akjol parking"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False

        def body(self):
            n = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(n).decode("utf-8") if n else ""
            if not raw:
                return {}
            try:
                return json.loads(raw)
            except ValueError:
                return {k: v[0] for k, v in parse_qs(raw).items()}

        # --- маршруты ---
        def do_GET(self):
            self.route("GET")

        def do_POST(self):
            self.route("POST")

        def do_DELETE(self):
            self.route("DELETE")

        def route(self, method):
            u = urlparse(self.path)
            path, q = u.path.rstrip("/") or "/", {k: v[0] for k, v in parse_qs(u.query).items()}
            if not self.authorized(path):
                return
            if method in ("POST", "DELETE") and \
                    not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return self.send_json({"error": "нужен JSON"}, 415)
            try:
                if method == "GET" and path == "/":
                    return self.send_file("panel.html")
                if method == "GET" and path == "/screen":
                    return self.send_file("screen.html")
                if method == "GET" and path.startswith("/static/"):
                    return self.send_file(path[len("/static/"):])
                data = self.body() if method in ("POST", "DELETE") else {}
                data.update(q)
                result = self.api(method, path, data)
                if result is None:
                    return self.send_json({"error": "not found"}, 404)
                self.send_json(result)
            except (ValueError, KeyError) as e:
                self.send_json({"error": str(e)}, 400)
            except KKMError as e:
                self.send_json({"error": str(e)}, 502)
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                self.send_json({"error": "%s: %s" % (type(e).__name__, e)}, 500)

        def api(self, method, path, d):
            db = app.db
            if path == "/api/screen":
                s = app.screen_state()
                s["screen"] = cfg.get("screen", {})
                return s
            if path == "/api/state":
                return app.state()
            if path == "/api/sessions":
                if d.get("status", "open") == "open":
                    return app.open_sessions()
                return db.query("SELECT * FROM sessions ORDER BY id DESC LIMIT ?", (int(d.get("limit", 200)),))
            if path == "/api/events":
                if d.get("plate"):
                    return db.query("SELECT * FROM events WHERE plate LIKE ? ORDER BY id DESC LIMIT 300",
                                    ("%" + d["plate"].upper() + "%",))
                return db.query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (int(d.get("limit", 200)),))
            if path == "/api/payments":
                day = d.get("day")
                if day:
                    return db.query("SELECT id, session_id, camera, plate, amount, duration_seconds, status, method, "
                                    "created, paid_time, receipt_status, receipt_fd, receipt_error, qr_error "
                                    "FROM payments WHERE substr(created,1,10)=? ORDER BY id DESC", (day,))
                return db.query("SELECT id, session_id, camera, plate, amount, duration_seconds, status, method, "
                                 "created, paid_time, receipt_status, receipt_fd, receipt_error, qr_error "
                                 "FROM payments ORDER BY id DESC LIMIT 200")
            if path == "/api/report":
                return app.report(d["day"])
            if path == "/api/tariff":
                secs = int(float(d.get("minutes", 0)) * 60)
                return {"fee": calc_fee(secs, app.tariff), "duration": format_duration(secs),
                        "tariff": app.tariff}

            if method == "GET":
                if path == "/api/whitelist":
                    return app.wl.list()
                if path == "/api/blacklist":
                    return db.query("SELECT * FROM blacklist ORDER BY plate")
                return None

            # --- действия ---
            # белый список здесь только для просмотра: менять — через Telegram-бота или сайт
            if path == "/api/barrier/open":
                app.manual_open(d["camera"], d.get("plate", ""), bool(d.get("release_unknown")))
                return {"ok": True}
            if path == "/api/payment/cash":
                return {"ok": app.pay_cash(int(d["id"]))}
            if path == "/api/payment/qr":
                return {"ok": app.mark_paid(int(d["id"]), "qr")}
            if path == "/api/payment/card":
                return {"ok": app.pay_card(int(d["id"]))}
            if path == "/api/payment/card_kkm":
                return {"ok": app.pay_card_kkm(int(d["id"]))}
            if path == "/api/payment/cancel":
                return {"ok": app.cancel_payment(int(d["id"]))}
            if path == "/api/payment/free":
                return {"ok": app.release_free(int(d["id"]))}
            if path == "/api/payment/receipt":
                app.retry_receipt(int(d["id"]))
                return {"ok": True}
            if path in ("/api/exit/bill", "/api/charge"):
                return app.bill(d["camera"], d["plate"], d.get("amount"))
            if path == "/api/camera/snap":
                return {"ok": app.snap(d["camera"])}
            if path == "/api/session/plate":
                return {"ok": app.set_session_plate(int(d["id"]), d["plate"])}
            if path == "/api/blacklist" and method == "DELETE":
                app.blacklist_remove(d["plate"])
                return {"ok": True}
            if path == "/api/blacklist":
                return {"plate": app.blacklist_add(d["plate"], d.get("reason", ""))}
            if path == "/api/alert/dismiss":
                app.dismiss_alert(d.get("id"))
                return {"ok": True}
            if path == "/api/session/add":
                return {"id": app.add_session(d["plate"], d.get("entry_time") or None)}
            if path == "/api/session/close":
                return {"ok": app.close_session_manual(int(d["id"]))}
            if path == "/api/simulate":
                if not cfg.get("server", {}).get("allow_simulate", True):
                    raise ValueError("симуляция выключена")
                cam = app.cameras[d["camera"]]
                app.last_seen.clear()
                if hasattr(cam, "last_plate"):
                    cam.last_plate = d["plate"].upper()
                    cam.last_plate_time = time.strftime("%Y-%m-%d %H:%M:%S")
                app.on_plate(cam, d["plate"])
                return {"ok": True}
            if path.startswith("/api/kkm/"):
                return self.kkm(path[len("/api/kkm/"):], d)
            return None

        def kkm(self, action, d):
            k = app.kkm
            if action == "state":
                return k.call("shifts/getState")
            if action == "close_day":
                return app.close_day()
            if action == "open_day":
                k.ensure_shift()
                return {"ok": True}
            if action == "print_last_document":
                return k.call("commons/printLastDocument")
            if action == "print_last_z":
                return k.call("commons/printLastZReport")
            if action == "x_report":
                return k.call("shifts/printXReport")
            if action in ("deposit", "withdrawal"):
                amount = "%.2f" % float(d["amount"])
                k.ensure_shift()
                r = k.call("bills/registerDeposit" if action == "deposit" else "bills/registerWithdrawal",
                           {"amount": amount})
                app.log("kkm", "%s: %s сом" % ("Внесение" if action == "deposit" else "Изъятие", amount))
                return r
            return None

    return Handler


def serve(app):
    s = app.cfg.get("server", {})
    ThreadingHTTPServer.allow_reuse_address = os.name != "nt"  # на Windows SO_REUSEADDR пускает второй экземпляр
    httpd = ThreadingHTTPServer((s.get("host", "0.0.0.0"), int(s.get("port", 8100))), make_handler(app))
    httpd.daemon_threads = True
    return httpd
