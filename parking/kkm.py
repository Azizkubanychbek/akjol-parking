"""Фискальная касса (NewCas Fiscal API)."""
import json
import urllib.request

# payItems.payType
PAY_CASH = 0
PAY_CASHLESS = 1

# коды ответа, при которых документ уже зарегистрирован, но не напечатан
PRINTER_STATUSES = (9, 10)


class KKMError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class KKM:
    def __init__(self, cfg):
        self.cfg = cfg

    @property
    def enabled(self):
        return bool(self.cfg.get("enabled"))

    def call(self, path, body=None, timeout=None):
        payload = dict(body or {})
        payload["registrationNumber"] = self.cfg["registration_number"]
        url = self.cfg["url"].rstrip("/") + "/fiscal/" + path.strip("/") + "/"
        req = urllib.request.Request(
            url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), method="POST"
        )
        req.add_header("Content-Type", "application/json; charset=utf-8")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.cfg.get("timeout", 60)) as r:
                resp = json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:  # noqa: BLE001
            raise KKMError("нет связи с ККМ: %s" % e)
        status = resp.get("status")
        if status not in (0,) + PRINTER_STATUSES:
            raise KKMError(
                "ККМ: статус %s, %s (коды %s/%s)"
                % (status, resp.get("errorMessage"), resp.get("extCode"), resp.get("extCode2")),
                status,
            )
        return resp

    # --- смена ---
    def ensure_shift(self):
        st = self.call("shifts/getDayState")
        if st.get("dayState") == 1 and st.get("isShiftExpired"):
            self.close_day()
            st = {"dayState": 0}
        if st.get("dayState") != 1:
            self.call("shifts/openDay", {"cashierName": self.cfg["cashier_name"], "ignoreOpenShift": True})

    def close_day(self):
        return self.call("shifts/closeDay", {"cashierName": self.cfg["cashier_name"]})

    def day_state(self):
        return self.call("shifts/getDayState")

    # --- чек ---
    def build_receipt(self, payment, pay_type, paid, title, cache_id):
        amount = "%d" % payment["amount"]
        name = self.cfg.get("item_name", "Услуги парковки")
        if self.cfg.get("item_with_plate", True):
            name = "%s %s" % (name, payment["plate"])
        item = {
            "count": "1",
            "price": amount,
            "total": amount,
            "itemName": name,
            "article": self.cfg.get("article", "PARKING"),
            "calcType": self.cfg.get("calc_type", 1),
            "payType": 0,
            "unit": self.cfg.get("unit", "усл"),
        }
        for k in ("vat_num", "st_num"):
            if self.cfg.get(k) is not None:
                item[{"vat_num": "vatNum", "st_num": "stNum"}[k]] = self.cfg[k]
        body = {
            "recType": 1,
            # cacheId: ККМ не выполнит повторно запрос с тем же id, а вернёт прошлый ответ —
            # защита от двойного чека, если ответ потерялся по сети
            "cacheId": cache_id,
            "goods": [item],
            "payItems": [{"payType": pay_type, "total": amount, "title": title, "paid": paid}],
        }
        if self.cfg.get("tax_system") is not None:
            body["taxSystem"] = self.cfg["tax_system"]
        if self.cfg.get("bottom_text"):
            body["bottomText"] = self.cfg["bottom_text"]
        return body

    def receipt(self, payment, pay_type, paid, title, cache_id, timeout=None):
        """Пробивает чек прихода. Возвращает ответ ККМ (fdNumber, qrCode...)."""
        self.ensure_shift()
        return self.call("bills/openAndCloseRec", self.build_receipt(payment, pay_type, paid, title, cache_id),
                         timeout)
