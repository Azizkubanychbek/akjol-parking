"""Парковка «Акжол» — запуск.

python app.py [config.json]
"""
import json
import os
import shutil
import sys
import threading

from parking.core import Parking
from parking.db import DB
from parking.kkm import KKM
from parking.megapay import Megapay
from parking.telegram import TelegramBot
from parking.web import serve
from parking import wlsite

HERE = os.path.dirname(os.path.abspath(__file__))


def load_config(path):
    if not os.path.exists(path):
        shutil.copy(os.path.join(HERE, "config.example.json"), path)
        print("Создан %s из примера — проверьте настройки." % path)
    with open(path, encoding="utf-8-sig") as f:  # Блокнот может сохранить с BOM
        return json.load(f)


_lock_file = None


def single_instance(path):
    """Вторая копия программы не запускается (иначе двойные QR и чеки)."""
    global _lock_file
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _lock_file = open(path, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            _lock_file.seek(0)
            msvcrt.locking(_lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(_lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


class Tee:
    """Пишет вывод и в консоль (если есть), и в logs/app.log — программа запускается без окна."""
    def __init__(self, path, console):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path) and os.path.getsize(path) > 5 * 1024 * 1024:
            os.replace(path, path + ".1")
        self.f = open(path, "a", encoding="utf-8", buffering=1)
        self.console = console

    def write(self, s):
        self.f.write(s)
        if self.console:
            try:
                self.console.write(s)
            except Exception:  # noqa: BLE001
                pass

    def flush(self):
        self.f.flush()
        if self.console:
            try:
                self.console.flush()
            except Exception:  # noqa: BLE001
                pass


def main():
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout = sys.stderr = Tee(os.path.join(HERE, "logs", "app.log"), sys.stdout)
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "config.json")
    if not single_instance(os.path.join(HERE, "logs", os.path.basename(cfg_path) + ".lock")):
        print("Программа уже запущена — вторая копия не нужна.", flush=True)
        sys.exit(3)
    cfg = load_config(cfg_path)
    db = DB(os.path.join(HERE, cfg.get("database", "parking.db")))
    wl_cfg = cfg.get("whitelist", {})
    bot = TelegramBot(cfg.get("telegram", {}), wl_cfg, db, lambda k, m: db.event(k, m))
    app = Parking(cfg, db, Megapay(cfg.get("megapay", {}), lambda k, m: db.event(k, m)), KKM(cfg.get("kkm", {})),
                  hidden_whitelist_path=os.path.join(HERE, "whitelist2.txt"), notify=bot.notify)
    bot.wl = app.wl
    bot.log = app.log
    httpd = serve(app)
    site = wlsite.serve(app.wl, wl_cfg, app.megapay_callback)
    threading.Thread(target=site.serve_forever, name="wl-site", daemon=True).start()
    app.start()
    bot.start()
    s = cfg.get("server", {})
    print("Панель оператора: http://127.0.0.1:%s/   Экран оплаты: http://127.0.0.1:%s/screen"
          % (s.get("port", 8100), s.get("port", 8100)), flush=True)
    print("Сайт белого списка: http://127.0.0.1:%s/" % wl_cfg.get("site_port", 8101), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()
        bot.stop()


if __name__ == "__main__":
    main()
