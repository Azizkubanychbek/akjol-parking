"""Белые списки.

1. Основной — в базе. Добавляют/удаляют только через Telegram-бота и сайт (логин/пароль),
   категории: постоянный или на 1 месяц. Обо всех изменениях — уведомление в Telegram.
2. Дополнительный — текстовый файл, номера постоянные. Нигде не показывается и не логируется.
"""
import calendar
import hashlib
import hmac
import os
import threading
from datetime import datetime

from . import plates
from .db import now_str, parse_dt

CATEGORIES = {"permanent": "постоянный", "month": "на 1 месяц"}


def add_month(dt):
    y, m = (dt.year + 1, 1) if dt.month == 12 else (dt.year, dt.month + 1)
    return dt.replace(year=y, month=m, day=min(dt.day, calendar.monthrange(y, m)[1]))


def fmt_date(s):
    d = parse_dt(s)
    return d.strftime("%d.%m.%Y %H:%M") if d else ""


def check_password(cfg, login, password):
    want = (cfg.get("admin_password_sha256") or "").lower().encode()
    got = hashlib.sha256(str(password or "").encode("utf-8")).hexdigest().encode()
    login_ok = hmac.compare_digest(str(login or "").encode("utf-8"), cfg.get("admin_login", "admin").encode("utf-8"))
    return login_ok and bool(want) and hmac.compare_digest(got, want)


class Whitelist:
    def __init__(self, db, hidden_path, notify=None, log=None):
        self.db = db
        self.hidden_path = hidden_path
        self.notify = notify or (lambda text: None)
        self.log = log or (lambda *a, **k: None)
        self._hidden = set()
        self._hidden_mtime = None
        self._lock = threading.Lock()
        self._migrate()
        if not os.path.exists(hidden_path):
            open(hidden_path, "a", encoding="utf-8").close()

    def _migrate(self):
        cols = {r["name"] for r in self.db.query("PRAGMA table_info(whitelist)")}
        for name, ddl in (("category", "TEXT NOT NULL DEFAULT 'permanent'"), ("expires", "TEXT"),
                          ("added_by", "TEXT"), ("source", "TEXT")):
            if name not in cols:
                self.db.execute("ALTER TABLE whitelist ADD COLUMN %s %s" % (name, ddl))

    # --- скрытый файл ---
    def _hidden_keys(self):
        try:
            mtime = os.path.getmtime(self.hidden_path)
        except OSError:
            return set()
        with self._lock:
            if mtime != self._hidden_mtime:
                keys = set()
                try:
                    with open(self.hidden_path, "rb") as f:
                        raw = f.read()
                except OSError:
                    return self._hidden
                try:
                    text = raw.decode("utf-8-sig")
                except UnicodeDecodeError:
                    text = raw.decode("cp1251", errors="replace")  # Блокнот в кодировке ANSI
                for line in text.splitlines():
                    parts = line.replace(",", " ").replace(";", " ").split()
                    if parts and plates.is_valid(parts[0]):
                        keys.add(plates.key(parts[0]))
                self._hidden, self._hidden_mtime = keys, mtime
            return self._hidden

    # --- проверка ---
    def check(self, key):
        """Возвращает None (нет в списках), 'main' или 'hidden'."""
        r = self.db.one("SELECT expires FROM whitelist WHERE plate_key=?", (key,))
        if r and (not r["expires"] or r["expires"] > now_str()):
            return "main"
        if key in self._hidden_keys():
            return "hidden"
        return None

    # --- основной список ---
    def list(self):
        rows = self.db.query("SELECT plate, category, expires, added_by, source, created FROM whitelist "
                             "ORDER BY plate")
        now = now_str()
        for r in rows:
            r["category_name"] = CATEGORIES.get(r["category"], r["category"])
            r["active"] = not r["expires"] or r["expires"] > now
        return rows

    def add(self, plate, category, by, source):
        plate = plates.normalize(plate)
        if not plates.is_valid(plate):
            raise ValueError("неверный номер: %r" % plate)
        if category not in CATEGORIES:
            raise ValueError("неверная категория")
        now = datetime.now()
        expires = now_str(add_month(now)) if category == "month" else None
        self.db.execute(
            "INSERT OR REPLACE INTO whitelist(plate_key, plate, note, created, category, expires, added_by, source) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (plates.key(plate), plate, "", now_str(now), category, expires, by, source))
        text = "✅ %s добавлен в белый список — %s%s\nКто: %s (%s)" % (
            plate, CATEGORIES[category], (", до " + fmt_date(expires)) if expires else "", by, source)
        self.log("whitelist", text.replace("\n", ". "), None, plate)
        self.notify(text)
        return {"plate": plate, "category": category, "expires": expires}

    def remove(self, plate, by, source):
        plate = plates.normalize(plate)
        k = plates.key(plate)
        if not self.db.one("SELECT 1 AS x FROM whitelist WHERE plate_key=?", (k,)):
            return False
        self.db.execute("DELETE FROM whitelist WHERE plate_key=?", (k,))
        text = "❌ %s удалён из белого списка\nКто: %s (%s)" % (plate, by, source)
        self.log("whitelist", text.replace("\n", ". "), None, plate)
        self.notify(text)
        return True

    def expire(self):
        """Удаляет просроченные «на 1 месяц» и сообщает в Telegram."""
        rows = self.db.query("SELECT plate, plate_key, expires FROM whitelist WHERE expires IS NOT NULL "
                             "AND expires<=?", (now_str(),))
        for r in rows:
            self.db.execute("DELETE FROM whitelist WHERE plate_key=?", (r["plate_key"],))
            text = "⌛ %s: закончился месяц в белом списке (%s), номер удалён" % (r["plate"], fmt_date(r["expires"]))
            self.log("whitelist", text, None, r["plate"])
            self.notify(text)
        return len(rows)
