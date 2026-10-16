"""Сменить пароль белого списка (бот и сайт): python tools\set_whitelist_password.py НОВЫЙ_ПАРОЛЬ"""
import hashlib
import json
import os
import sys

path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")
cfg = json.load(open(path, encoding="utf-8-sig"))
cfg.setdefault("whitelist", {})["admin_password_sha256"] = hashlib.sha256(sys.argv[1].encode("utf-8")).hexdigest()
with open(path, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
print("Пароль изменён. Перезапустите программу.")
