"""Проверка Megapay: создать QR на 1 сом и показать ответ целиком.

python tools\megapay_test.py              — создать QR, вывести ответ
python tools\megapay_test.py status ID    — проверить статус (нужен megapay.status_path в config.json)
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from parking.megapay import Megapay  # noqa: E402

cfg = json.load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json"),
                     encoding="utf-8-sig"))
m = Megapay(cfg["megapay"], lambda k, msg: print("[%s] %s" % (k, msg)))
if len(sys.argv) > 2 and sys.argv[1] == "status":
    print("Статус:", m.status(sys.argv[2]))
else:
    r = m.generate(1, "TEST", 0)
    print("id транзакции:", r["id"])
    print("картинка:", (r["image"] or "")[:80], "...")
    print("текст QR:", r["text"])
