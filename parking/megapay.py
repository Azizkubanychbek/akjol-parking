"""QR-оплата Megapay (Megacom)."""
import base64
import json
import ssl
import urllib.error
import urllib.request

ID_KEYS = ("qrTransactionId", "transactionId", "qrId", "paymentId", "uuid", "id")
IMAGE_KEYS = ("qrImage", "qrCodeImage", "image", "imageBase64", "qrBase64", "qr")
# Megapay site-api отвечает {"data": "https://megapay.kg/get#000201...", "qrTransactionId": "...", "status": "SUCCESS"}
TEXT_KEYS = ("data", "qrData", "qrString", "qrText", "payload", "qrLink", "link", "url")
STATUS_KEYS = ("status", "paymentStatus", "transactionStatus", "state")
SUCCESS = {"success", "successful", "paid", "completed", "complete", "approved", "succeeded", "done"}
FAIL = {"fail", "failed", "rejected", "canceled", "cancelled", "error", "declined"}


def find_key(obj, names):
    """Ищет первое непустое значение по одному из ключей (в любой вложенности)."""
    if isinstance(obj, dict):
        for n in names:
            v = obj.get(n)
            if v not in (None, "", [], {}) and not isinstance(v, (dict, list)):
                return v
        for v in obj.values():
            r = find_key(v, names)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_key(v, names)
            if r is not None:
                return r
    return None


def classify_status(value, cfg=None):
    cfg = cfg or {}
    success = {s.lower() for s in cfg.get("success_values", [])} or SUCCESS
    fail = {s.lower() for s in cfg.get("fail_values", [])} or FAIL
    v = str(value or "").strip().lower()
    if v in success:
        return "success"
    if v in fail:
        return "fail"
    return "pending"


class MegapayError(Exception):
    pass


class Megapay:
    def __init__(self, cfg, log):
        self.cfg = cfg
        self.log = log
        self.ctx = None if cfg.get("verify_ssl", True) else ssl._create_unverified_context()

    @property
    def enabled(self):
        return bool(self.cfg.get("enabled"))

    def _request(self, method, path, body=None):
        url = self.cfg["base"].rstrip("/") + path
        token = base64.b64encode(
            ("%s:%s" % (self.cfg["login"], self.cfg["password"])).encode("utf-8")
        ).decode("ascii")
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", "Basic " + token)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.get("timeout", 15), context=self.ctx) as r:
                raw = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raise MegapayError("HTTP %s: %s" % (e.code, e.read(500).decode("utf-8", "replace")))
        except Exception as e:  # noqa: BLE001
            raise MegapayError(str(e))
        try:
            return json.loads(raw)
        except ValueError:
            return {"raw": raw}

    def generate(self, amount, plate, payment_id):
        fmt = {"plate": plate, "payment_id": payment_id, "amount": amount}
        body = {
            "qrType": "DYNAMIC",
            "serviceId": self.cfg["service_id"],
            "destination": self.cfg.get("destination", "").format(**fmt),
            "paymentAmount": amount,
            "paymentComment": self.cfg.get("comment", "Парковка {plate}").format(**fmt),
            "amountEditable": False,
            "destinationEditable": False,
            "imageSize": self.cfg.get("image_size", 512),
        }
        resp = self._request("POST", self.cfg.get("generate_path", "/qr/united/generate/v2"), body)
        self.log("megapay", "Ответ генерации QR: %s" % _short(resp))
        mid = find_key(resp, ID_KEYS)
        image, text = None, None
        img = find_key(resp, IMAGE_KEYS)
        if isinstance(img, str):
            if img.startswith("data:image") or img.startswith("http"):
                image = img
            elif len(img) > 200:
                image = "data:image/png;base64," + img
            else:
                text = img
        if not image and not text:
            t = find_key(resp, TEXT_KEYS)
            if isinstance(t, str):
                text = t
        if not image and not text:
            raise MegapayError("в ответе нет QR: %s" % _short(resp))
        return {"id": str(mid) if mid is not None else None, "image": image, "text": text}

    def status(self, megapay_id):
        """Возвращает 'success' | 'fail' | 'pending'."""
        path = self.cfg.get("status_path") or ""
        if not path or not megapay_id:
            return "pending"
        path = path.format(id=megapay_id, service_id=self.cfg["service_id"])
        method = self.cfg.get("status_method", "GET").upper()
        body = None
        if method == "POST":
            body = json.loads(
                json.dumps(self.cfg.get("status_body", {"id": "{id}"})).replace("{id}", str(megapay_id))
            )
        resp = self._request(method, path, body)
        self.log("megapay", "Ответ статуса %s: %s" % (megapay_id, _short(resp, 300)))
        # верхний "status" у Megapay — статус самого запроса (как у generate: "SUCCESS"),
        # статус платежа ищем сначала внутри data
        data = resp.get("data") if isinstance(resp, dict) else None
        key = self.cfg.get("status_key")
        if key:
            value = find_key(resp, (key,))
        elif isinstance(data, (dict, list)):
            value = find_key(data, STATUS_KEYS)
        else:
            value = None
        return classify_status(value, self.cfg)


def _short(obj, n=600):
    s = json.dumps(obj, ensure_ascii=False)
    # не пишем в журнал base64-картинку целиком
    return s if len(s) <= n else s[:n] + "...(%d симв.)" % len(s)
