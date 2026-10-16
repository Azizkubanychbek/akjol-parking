"""Telegram-бот: управление основным белым списком и уведомления."""
import json
import threading
import time
import urllib.error
import urllib.request

from . import plates
from .db import now_str
from .whitelist import CATEGORIES, check_password, fmt_date

SCHEMA = """
CREATE TABLE IF NOT EXISTS tg_users (
    chat_id INTEGER PRIMARY KEY,
    name TEXT,
    authorized INTEGER NOT NULL DEFAULT 0,
    state TEXT,
    pending TEXT,
    failed INTEGER NOT NULL DEFAULT 0,
    blocked_until REAL NOT NULL DEFAULT 0,
    updated TEXT
);
"""

BTN_ADD, BTN_DEL, BTN_LIST, BTN_CHECK = "➕ Добавить", "➖ Удалить", "📋 Список", "🔎 Проверить"
KEYBOARD = {"keyboard": [[{"text": BTN_ADD}, {"text": BTN_DEL}], [{"text": BTN_LIST}, {"text": BTN_CHECK}]],
            "resize_keyboard": True}
MAX_FAILS, BLOCK_SECONDS = 5, 1800


class TelegramBot:
    def __init__(self, cfg, wl_cfg, db, log):
        self.cfg = cfg
        self.wl_cfg = wl_cfg
        self.db = db
        self.log = log
        self.wl = None  # задаётся после создания Whitelist
        self.base = "https://api.telegram.org/bot%s/" % cfg.get("token", "")
        self.wakeup = threading.Event()
        self._stop = threading.Event()
        db.conn.executescript(SCHEMA)
        db._add_columns("tg_users", {"auth_hash": "TEXT"})

    def _hash(self):
        return (self.wl_cfg.get("admin_password_sha256") or "").lower()

    @property
    def enabled(self):
        return bool(self.cfg.get("enabled") and self.cfg.get("token"))

    def api(self, method, params=None, timeout=15):
        req = urllib.request.Request(self.base + method, data=json.dumps(params or {}).encode("utf-8"),
                                     method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    # --- уведомления ---
    def notify(self, text):
        if not self.enabled:
            return
        chats = {r["chat_id"] for r in self.db.query(
            "SELECT chat_id FROM tg_users WHERE authorized=1 AND auth_hash=?", (self._hash(),))}
        chats |= {int(c) for c in self.cfg.get("notify_chat_ids", [])}
        for c in chats:
            self.send(c, text)

    def send(self, chat_id, text, markup=None):
        """Сообщение кладётся в базу и уйдёт, как только будет интернет (хоть через сутки)."""
        self.db.execute("INSERT INTO tg_outbox(chat_id, text, markup, created) VALUES (?,?,?,?)",
                        (chat_id, text, json.dumps(markup) if markup else None, now_str()))
        self.wakeup.set()

    def flush_outbox(self):
        for m in self.db.query("SELECT * FROM tg_outbox ORDER BY id LIMIT 50"):
            params = {"chat_id": m["chat_id"], "text": m["text"]}
            if m["markup"]:
                params["reply_markup"] = json.loads(m["markup"])
            try:
                self.api("sendMessage", params)
            except urllib.error.HTTPError as e:
                if 400 <= e.code < 500 and e.code != 429:  # чат удалён/бот заблокирован — не повторять
                    self.db.execute("DELETE FROM tg_outbox WHERE id=?", (m["id"],))
                    self.log("error", "Telegram: сообщение в %s не принято (%s)" % (m["chat_id"], e.code))
                    continue
                return False
            except Exception:  # noqa: BLE001  нет интернета — попробуем позже
                return False
            self.db.execute("DELETE FROM tg_outbox WHERE id=?", (m["id"],))
        return True

    def _sender(self):
        while not self._stop.is_set():
            self.wakeup.wait(10)
            self.wakeup.clear()
            if not self.flush_outbox():
                self._stop.wait(10)

    # --- приём сообщений ---
    def start(self):
        if not self.enabled:
            return
        threading.Thread(target=self._sender, name="tg-send", daemon=True).start()
        threading.Thread(target=self._poll, name="tg-poll", daemon=True).start()

    def stop(self):
        self._stop.set()

    def _poll(self):
        offset = int(self.db.get_meta("tg_offset", "0"))
        while not self._stop.is_set():
            try:
                r = self.api("getUpdates", {"offset": offset, "timeout": 25,
                                            "allowed_updates": ["message", "callback_query"]}, timeout=35)
                for u in r.get("result", []):
                    offset = u["update_id"] + 1
                    self.db.set_meta("tg_offset", str(offset))
                    try:
                        self.handle(u)
                    except Exception as e:  # noqa: BLE001
                        self.log("error", "Telegram: ошибка обработки: %s" % e)
            except Exception:  # noqa: BLE001
                self._stop.wait(5)

    def _user(self, chat_id, name):
        u = self.db.one("SELECT * FROM tg_users WHERE chat_id=?", (chat_id,))
        if not u:
            self.db.execute("INSERT INTO tg_users(chat_id, name, updated) VALUES (?,?,?)", (chat_id, name, now_str()))
            u = self.db.one("SELECT * FROM tg_users WHERE chat_id=?", (chat_id,))
        if u["authorized"] and u["auth_hash"] != self._hash():
            u["authorized"] = 0  # пароль сменили — вход по старому паролю больше не действует
        return u

    def _set(self, chat_id, **kw):
        kw["updated"] = now_str()
        self.db.execute("UPDATE tg_users SET %s WHERE chat_id=?" % ", ".join("%s=?" % k for k in kw),
                        tuple(kw.values()) + (chat_id,))

    @staticmethod
    def _name(frm):
        n = " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x) or "без имени"
        return n + (" @" + frm["username"] if frm.get("username") else "")

    def handle(self, u):
        if "callback_query" in u:
            return self._callback(u["callback_query"])
        msg = u.get("message")
        if not msg or msg.get("chat", {}).get("type") != "private" or "text" not in msg:
            return
        chat, text = msg["chat"]["id"], msg["text"].strip()
        name = self._name(msg.get("from", {}))
        user = self._user(chat, name)
        if user["blocked_until"] > time.time():
            return self.send(chat, "Слишком много неверных попыток. Попробуйте позже.")

        if text in ("/start", "/login") or (not user["authorized"] and not user["state"]):
            self._set(chat, authorized=0, state="login", name=name)
            return self.send(chat, "Белый список парковки «Акжол».\nВведите логин:", {"remove_keyboard": True})
        if text == "/logout":
            self._set(chat, authorized=0, state=None)
            return self.send(chat, "Вы вышли. /start — войти снова.", {"remove_keyboard": True})
        if user["state"] == "login":
            self._set(chat, state="password", pending=text)
            return self.send(chat, "Введите пароль:")
        if user["state"] == "password":
            try:  # пароль не оставляем в переписке
                self.api("deleteMessage", {"chat_id": chat, "message_id": msg["message_id"]})
            except Exception:  # noqa: BLE001
                pass
            if check_password(self.wl_cfg, user["pending"], text):
                self._set(chat, authorized=1, state=None, pending=None, failed=0, auth_hash=self._hash())
                self.log("whitelist", "Telegram: вход %s" % name)
                return self.send(chat, "Вход выполнен. Отправьте номер машины или выберите действие.", KEYBOARD)
            fails = user["failed"] + 1
            self._set(chat, state="login", pending=None, failed=0 if fails >= MAX_FAILS else fails,
                      blocked_until=time.time() + BLOCK_SECONDS if fails >= MAX_FAILS else 0)
            self.log("alert", "Telegram: неверный пароль от %s" % name)
            return self.send(chat, "Неверный логин или пароль. Введите логин:")
        if not user["authorized"]:
            return self.send(chat, "Сначала войдите: /start")

        # --- авторизован ---
        if text == BTN_ADD:
            self._set(chat, state="add")
            return self.send(chat, "Отправьте номер машины для добавления:")
        if text == BTN_DEL:
            self._set(chat, state="del")
            return self.send(chat, "Отправьте номер машины для удаления:")
        if text == BTN_CHECK:
            self._set(chat, state="check")
            return self.send(chat, "Отправьте номер для проверки:")
        if text == BTN_LIST:
            self._set(chat, state=None)
            return self._send_list(chat)
        if text.startswith("/"):
            return self.send(chat, "Команды: /start, /logout. Или кнопки ниже.", KEYBOARD)

        plate = plates.normalize(text)
        if not plates.is_valid(plate):
            return self.send(chat, "Не похоже на номер машины: %s" % text, KEYBOARD)
        state = user["state"]
        self._set(chat, state=None)
        if state == "del":
            ok = self.wl.remove(plate, name, "Telegram")
            return self.send(chat, "Удалён: %s" % plate if ok else "%s нет в белом списке" % plate, KEYBOARD)
        if state == "check":
            return self.send(chat, self._describe(plate), KEYBOARD)
        # по умолчанию — добавление, спрашиваем категорию
        self.send(chat, "%s — на какой срок?" % plate, {"inline_keyboard": [[
            {"text": "Постоянный", "callback_data": "add|permanent|" + plate},
            {"text": "На 1 месяц", "callback_data": "add|month|" + plate}]]})

    def _callback(self, cq):
        chat = cq["message"]["chat"]["id"]
        name = self._name(cq.get("from", {}))
        try:
            self.api("answerCallbackQuery", {"callback_query_id": cq["id"]})
        except Exception:  # noqa: BLE001
            pass
        user = self._user(chat, name)
        if not user["authorized"]:
            return self.send(chat, "Сначала войдите: /start")
        parts = (cq.get("data") or "").split("|")
        if len(parts) == 3 and parts[0] == "add":
            r = self.wl.add(parts[2], parts[1], name, "Telegram")
            try:
                self.api("editMessageReplyMarkup", {"chat_id": chat, "message_id": cq["message"]["message_id"],
                                                    "reply_markup": {"inline_keyboard": []}})
            except Exception:  # noqa: BLE001
                pass
            # уведомление всем уже ушло из Whitelist.add; этому чату — клавиатура
            self.send(chat, "Готово: %s — %s" % (r["plate"], CATEGORIES[r["category"]]), KEYBOARD)

    def _describe(self, plate):
        r = self.db.one("SELECT * FROM whitelist WHERE plate_key=?", (plates.key(plate),))
        if not r:
            return "%s нет в белом списке" % plate
        return "%s — %s%s\nДобавил: %s (%s), %s" % (
            r["plate"], CATEGORIES.get(r["category"], r["category"]),
            (", до " + fmt_date(r["expires"])) if r["expires"] else "",
            r["added_by"] or "—", r["source"] or "—", fmt_date(r["created"]))

    def _send_list(self, chat):
        rows = self.wl.list()
        if not rows:
            return self.send(chat, "Белый список пуст.", KEYBOARD)
        lines = ["%s — %s" % (r["plate"], "постоянный" if r["category"] == "permanent"
                                          else "до " + fmt_date(r["expires"])) for r in rows]
        chunk = "Белый список (%d):" % len(rows)
        for line in lines:
            if len(chunk) + len(line) > 3800:
                self.send(chat, chunk)
                chunk = ""
            chunk += "\n" + line
        self.send(chat, chunk, KEYBOARD)
