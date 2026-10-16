"""Разовый перенос белого списка из старого Parking.Api в основной список (постоянные), без уведомлений.

python tools\import_whitelist.py [http://127.0.0.1:5000/api/EntryStatus]
"""
import json
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from parking.db import DB  # noqa: E402
from parking.whitelist import Whitelist  # noqa: E402

url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:5000/api/EntryStatus"
cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8-sig"))
wl = Whitelist(DB(os.path.join(ROOT, cfg.get("database", "parking.db"))), os.path.join(ROOT, "whitelist2.txt"))
with urllib.request.urlopen(url, timeout=15) as r:
    data = json.loads(r.read().decode("utf-8", "replace"))
if isinstance(data, dict):
    data = next((v for v in data.values() if isinstance(v, list)), [])
added = 0
for item in data:
    if not isinstance(item, dict):
        continue
    plate = next((item[k] for k in ("plateNumber", "PlateNumber", "number", "Number", "carNumber",
                                    "plate", "Plate", "gosNumber") if item.get(k)), None)
    status = next((item[k] for k in ("status", "Status", "entryStatus", "EntryStatus") if k in item), 1)
    if plate and str(status) == "1":
        try:
            wl.add(plate, "permanent", "перенос из Parking.Api", "импорт")
            added += 1
        except ValueError:
            print("пропущен:", plate)
print("Перенесено: %d из %d" % (added, len(data)))
