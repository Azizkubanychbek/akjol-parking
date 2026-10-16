"""SQLite-база: заезды, оплаты, whitelist, журнал."""
import sqlite3
import threading
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plate TEXT NOT NULL,
    plate_key TEXT NOT NULL,
    entry_time TEXT NOT NULL,
    exit_time TEXT,
    status TEXT NOT NULL DEFAULT 'open',      -- open | closed
    paid_until TEXT,                          -- до какого момента оплачено (время оплаты)
    paid_total INTEGER NOT NULL DEFAULT 0,
    close_reason TEXT,
    note TEXT
);
CREATE INDEX IF NOT EXISTS ix_sessions_key ON sessions(plate_key, status);
CREATE INDEX IF NOT EXISTS ix_sessions_exit ON sessions(exit_time);

CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    camera TEXT NOT NULL,
    plate TEXT NOT NULL,
    amount INTEGER NOT NULL,
    duration_seconds INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | success | expired | cancelled
    method TEXT,                              -- qr | cash | card | card_kkm
    megapay_id TEXT,
    qr_image TEXT,
    qr_text TEXT,
    qr_error TEXT,
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    paid_time TEXT,
    receipt_status TEXT NOT NULL DEFAULT 'none', -- none | pending | done | failed | skipped
    receipt_fd INTEGER,
    receipt_error TEXT,
    receipt_attempts INTEGER NOT NULL DEFAULT 0,
    receipt_next_try TEXT
);
CREATE INDEX IF NOT EXISTS ix_payments_status ON payments(status);
CREATE INDEX IF NOT EXISTS ix_payments_receipt ON payments(receipt_status);

CREATE TABLE IF NOT EXISTS whitelist (
    plate_key TEXT PRIMARY KEY,
    plate TEXT NOT NULL,
    note TEXT,
    created TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    camera TEXT,
    plate TEXT,
    message TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS blacklist (
    plate_key TEXT PRIMARY KEY,
    plate TEXT NOT NULL,
    reason TEXT,
    created TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tg_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    text TEXT NOT NULL,
    markup TEXT,
    created TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def now_str(dt=None):
    return (dt or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


def parse_dt(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S") if s else None


class DB:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._add_columns("payments", {"receipt_qr": "TEXT", "receipt_cache_n": "INTEGER NOT NULL DEFAULT 0",
                                       "card_cache_id": "TEXT"})

    def _add_columns(self, table, cols):
        have = {r[1] for r in self.conn.execute("PRAGMA table_info(%s)" % table)}
        for name, ddl in cols.items():
            if name not in have:
                self.conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, ddl))

    def query(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        rows = self.query(sql, args)
        return rows[0] if rows else None

    def execute(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args).lastrowid

    # --- журнал ---
    def event(self, kind, message, camera=None, plate=None):
        self.execute(
            "INSERT INTO events(ts, kind, camera, plate, message) VALUES (?,?,?,?,?)",
            (now_str(), kind, camera, plate, message),
        )

    # --- meta ---
    def get_meta(self, key, default=None):
        r = self.one("SELECT value FROM meta WHERE key=?", (key,))
        return r["value"] if r else default

    def set_meta(self, key, value):
        self.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", (key, value))
