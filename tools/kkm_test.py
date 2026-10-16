"""Проверка ККМ: состояние, открытие смены и тестовый чек на 1 сом.

python tools\kkm_test.py            — только состояние (ничего не пробивает)
python tools\kkm_test.py receipt    — пробить тестовый чек на 1 сом (наличные)
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from parking.kkm import KKM, PAY_CASH  # noqa: E402

cfg = json.load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json"),
                     encoding="utf-8-sig"))
k = KKM(cfg["kkm"])
show = lambda r: print(json.dumps(r, ensure_ascii=False, indent=2))  # noqa: E731
print("ККМ:", cfg["kkm"]["url"], "РНМ", cfg["kkm"]["registration_number"])
show(k.call("commons/getRegistrationStatus"))
show(k.call("shifts/getDayState"))
if len(sys.argv) > 1 and sys.argv[1] == "receipt":
    import time
    pay = {"id": int(time.time()), "amount": 1, "plate": "TEST"}
    print("Тело чека:")
    cid = "akjol-test-%d" % pay["id"]
    show(k.build_receipt(pay, PAY_CASH, True, "Наличные", cid))
    show(k.receipt(pay, PAY_CASH, True, "Наличные", cid))
