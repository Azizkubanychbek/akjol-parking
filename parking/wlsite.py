"""Сайт основного белого списка — отдельный порт, можно открыть в интернет (проброс порта на роутере).
Только список, вход по логину/паролю. Панель оператора здесь недоступна."""
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .whitelist import check_password

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
TOKEN_TTL = 12 * 3600
MAX_FAILS, BLOCK_SECONDS = 10, 900


def make_handler(wl, cfg, callback=None):
    tokens = {}   # token -> истекает
    fails = {}    # ip -> (кол-во, заблокирован до)
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "AkjolWL/1.0"

        def log_message(self, fmt, *args):
            pass

        def ip(self):
            return self.client_address[0]

        def send_json(self, obj, code=200, cookie=None):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if cookie is not None:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(body)

        def token(self):
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "wl_token":
                    return v
            return None

        def authed(self):
            t = self.token()
            with lock:
                exp = tokens.get(t)
                if exp and exp > time.time():
                    return True
                tokens.pop(t, None)
            return False

        def body(self):
            n = min(int(self.headers.get("Content-Length", "0") or 0), 10000)
            try:
                return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
            except ValueError:
                return {}

        def do_GET(self):
            if self.path.split("?")[0] in ("/", "/index.html"):
                with open(os.path.join(STATIC, "wl.html"), "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-Frame-Options", "DENY")
                self.end_headers()
                return self.wfile.write(body)
            if self.path.startswith("/api/megapay/callback"):
                return self._callback({})
            if self.path == "/api/list":
                if not self.authed():
                    return self.send_json({"error": "auth"}, 401)
                return self.send_json(wl.list())
            self.send_json({"error": "not found"}, 404)

        def _callback(self, body):
            """Уведомление Megapay об оплате: без логина, но с секретом в адресе (?secret=...)."""
            q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            q.update(body if isinstance(body, dict) else {})
            ok = bool(callback) and callback(q)
            return self.send_json({"ok": bool(ok)})

        def do_POST(self):
            if self.path.startswith("/api/megapay/callback"):
                return self._callback(self.body())
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return self.send_json({"error": "нужен JSON"}, 415)
            d = self.body()
            ip = self.ip()
            if self.path == "/api/login":
                with lock:
                    n, until = fails.get(ip, (0, 0))
                    if until > time.time():
                        return self.send_json({"error": "Слишком много попыток, подождите 15 минут"}, 429)
                if check_password(cfg, d.get("login"), d.get("password")):
                    t = secrets.token_urlsafe(32)
                    with lock:
                        tokens[t] = time.time() + TOKEN_TTL
                        fails.pop(ip, None)
                    return self.send_json({"ok": True}, cookie="wl_token=%s; HttpOnly; SameSite=Strict; Path=/; "
                                                                "Max-Age=%d" % (t, TOKEN_TTL))
                with lock:
                    n += 1
                    fails[ip] = (0, time.time() + BLOCK_SECONDS) if n >= MAX_FAILS else (n, 0)
                wl.log("alert", "Сайт белого списка: неверный пароль с %s" % ip)
                return self.send_json({"error": "Неверный логин или пароль"}, 401)
            if self.path == "/api/logout":
                with lock:
                    tokens.pop(self.token(), None)
                return self.send_json({"ok": True}, cookie="wl_token=; Path=/; Max-Age=0")
            if not self.authed():
                return self.send_json({"error": "auth"}, 401)
            try:
                if self.path == "/api/add":
                    return self.send_json(wl.add(d.get("plate", ""), d.get("category", ""), "сайт, IP " + ip, "сайт"))
                if self.path == "/api/remove":
                    return self.send_json({"ok": wl.remove(d.get("plate", ""), "сайт, IP " + ip, "сайт")})
            except ValueError as e:
                return self.send_json({"error": str(e)}, 400)
            self.send_json({"error": "not found"}, 404)

    return Handler


def serve(wl, cfg, callback=None):
    httpd = ThreadingHTTPServer((cfg.get("site_host", "0.0.0.0"), int(cfg.get("site_port", 8101))),
                                make_handler(wl, cfg, callback))
    httpd.daemon_threads = True
    return httpd
