import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from parking import plates  # noqa: E402
from parking.cameras import Camera, parse_event_text  # noqa: E402
from parking.core import Parking  # noqa: E402
from parking.db import DB, now_str  # noqa: E402
from parking.megapay import classify_status, find_key  # noqa: E402
from parking.tariff import calc_fee  # noqa: E402

M = 60
H = 3600
D = 86400


class TariffTest(unittest.TestCase):
    def test_examples_from_customer(self):
        self.assertEqual(calc_fee(15 * M), 0)            # 15:00 -> 15:15
        self.assertEqual(calc_fee(16 * M), 50)           # 15:00 -> 15:16
        self.assertEqual(calc_fee(60 * M), 50)           # 15:00 -> 16:00
        self.assertEqual(calc_fee(61 * M), 100)          # 15:00 -> 16:01
        self.assertEqual(calc_fee(24 * H), 1200)         # 15:00 -> 15:00 след. дня
        self.assertEqual(calc_fee(24 * H + M), 1500)     # 15:01 след. дня
        self.assertEqual(calc_fee(48 * H), 1500)
        self.assertEqual(calc_fee(48 * H + M), 1800)
        self.assertEqual(calc_fee(72 * H + M), 2100)

    def test_edges(self):
        self.assertEqual(calc_fee(0), 0)
        self.assertEqual(calc_fee(15 * M + 59), 0)      # 15 мин 59 сек — ещё бесплатно
        self.assertEqual(calc_fee(2 * H + 40), 100)      # 2 ч 00 мин 40 сек — как на экране
        self.assertEqual(calc_fee(23 * H + M), 1200)


class PlatesTest(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(plates.normalize("01 kg 123 abc"), "01KG123ABC")
        self.assertEqual(plates.normalize("01КG123АВС"), "01KG123ABC")  # кириллица
        self.assertEqual(plates.key("06978ATH"), plates.key("6978ATH"))
        self.assertFalse(plates.is_valid("无车牌"))
        self.assertFalse(plates.is_valid("Unknown"))
        self.assertFalse(plates.is_valid("привет"))

    def test_close_match(self):
        self.assertTrue(plates.close_match("01KG123ABC", "01KG128ABC"))
        self.assertTrue(plates.close_match("01KG123ABC", "01KG123AB"))
        self.assertFalse(plates.close_match("01KG123ABC", "01KG777ABC"))


class MegapayParseTest(unittest.TestCase):
    def test_find_and_status(self):
        resp = {"code": 0, "data": {"qrTransactionId": 77, "qrImage": "x" * 300}}
        self.assertEqual(find_key(resp, ("qrTransactionId", "id")), 77)
        self.assertEqual(classify_status("SUCCESS"), "success")
        self.assertEqual(classify_status("Pending"), "pending")
        self.assertEqual(classify_status("REJECTED"), "fail")


class MegapayGenerateTest(unittest.TestCase):
    def test_real_response_shape(self):
        from parking.megapay import Megapay
        m = Megapay({"base": "x", "login": "a", "password": "b", "service_id": 6533}, lambda *a: None)
        sent = {}

        def fake(method, path, body=None):
            sent.update(body)
            return {"data": "https://megapay.kg/get#000201010212", "message": None,
                    "qrTransactionId": "40f715926d26444b880172784fa9796d", "status": "SUCCESS"}
        m._request = fake
        r = m.generate(100, "01KG200TST", 1)
        self.assertEqual(r, {"id": "40f715926d26444b880172784fa9796d", "image": None,
                             "text": "https://megapay.kg/get#000201010212"})
        self.assertEqual((sent["paymentAmount"], sent["amountEditable"]), (100, False))


class FakeCam:
    def __init__(self, cfg, api, on_plate, log):
        self.cfg, self.name, self.role = cfg, cfg["name"], cfg["role"]
        self.opened = []

    def open_barrier(self, plate=""):
        self.opened.append(plate)
        return True, "OK"

    def start(self):
        pass

    def stop(self):
        pass

    def status(self):
        return {"name": self.name}


class FakeMegapay:
    def __init__(self):
        self.cfg = {"enabled": True, "timeout_seconds": 300, "status_path": "/x"}
        self.paid = set()
        self.n = 0

    enabled = True

    def generate(self, amount, plate, pid):
        self.n += 1
        return {"id": "M%d" % pid, "image": None, "text": "QRDATA%d" % pid}

    def status(self, mid):
        return "success" if mid in self.paid else "pending"


class FakeKKM:
    def __init__(self, fail=False):
        self.cfg = {"enabled": True, "max_attempts": 3}
        self.receipts = []
        self.fail = fail

    enabled = True

    def receipt(self, payment, pay_type, paid, title, cache_id, timeout=None):
        self.cache_ids.append(cache_id)
        from parking.kkm import KKMError
        if self.fail:
            raise KKMError("нет связи с ККМ")
        self.receipts.append((payment["id"], payment["amount"], pay_type, paid, title))
        if not paid and self.card_result is not None:
            r = self.card_result
            if isinstance(r, Exception):
                raise r
            return r
        return {"status": 0, "fdNumber": 100 + payment["id"], "qrCode": "http://tax/%d" % payment["id"]}

    card_result = None
    aborted = 0
    cache_ids = None

    def call(self, path, body=None, timeout=None):
        if path == "bills/abortPosAction":
            self.aborted += 1
        return {"status": 0}


CFG = {
    "cameras": [
        {"name": "Въезд", "role": "enter", "host": "x"},
        {"name": "Выезд", "role": "exit", "host": "x"},
        {"name": "Въезд WL", "role": "wl_enter", "host": "x"},
    ],
    "parking": {"dedupe_seconds": 0},
}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(os.path.join(self.tmp.name, "t.db"))
        self.mp = FakeMegapay()
        self.kkm = FakeKKM()
        self.kkm.cache_ids = []
        self.notes = []
        self.hidden = os.path.join(self.tmp.name, "whitelist2.txt")
        self.app = Parking(CFG, self.db, self.mp, self.kkm, camera_cls=FakeCam, async_barrier=False,
                           hidden_whitelist_path=self.hidden, notify=self.notes.append)
        self.app._generate_qr_async = self.app._generate_qr  # синхронно в тестах
        self.enter = self.app.cameras["Въезд"]
        self.exit = self.app.cameras["Выезд"]

    def tearDown(self):
        self.db.conn.close()
        self.tmp.cleanup()

    def backdate(self, plate, minutes):
        t = now_str(datetime.now() - timedelta(minutes=minutes))
        self.db.execute("UPDATE sessions SET entry_time=? WHERE plate_key=? AND status='open'",
                        (t, plates.key(plate)))



class FlowTest(Base):
    def test_free_exit(self):
        self.app.on_plate(self.enter, "01KG123ABC")
        self.assertEqual(self.enter.opened, ["01KG123ABC"])
        self.backdate("01KG123ABC", 10)
        self.app.on_plate(self.exit, "01KG123ABC")
        self.assertEqual(self.exit.opened, ["01KG123ABC"])
        self.assertEqual(self.app.screen_state()["state"], "stop")

    def test_paid_by_qr(self):
        self.app.on_plate(self.enter, "01KG123ABC")
        self.backdate("01KG123ABC", 61)
        self.app.on_plate(self.exit, "01KG123ABC")
        self.assertEqual(self.exit.opened, [])
        s = self.app.screen_state()
        self.assertEqual((s["state"], s["amount"], s["qr_text"]), ("qr", 100, "QRDATA1"))
        # повторное распознавание той же машины не создаёт новый QR
        self.app.on_plate(self.exit, "01KG123ABC")
        self.assertEqual(self.mp.n, 1)
        self.mp.paid.add("M1")
        self.app.poll_payments()
        self.assertEqual(self.exit.opened, ["01KG123ABC"])
        self.assertEqual(self.app.screen_state()["state"], "paid")
        self.app.process_receipts()
        self.assertEqual(self.kkm.receipts, [(1, 100, 1, True, "QR Megapay")])
        # оплата не задваивается
        self.app.poll_payments()
        self.assertEqual(len(self.exit.opened), 1)
        # повторно камера видит машину после оплаты — открыть без новой оплаты
        self.app.on_plate(self.exit, "01KG123ABC")
        self.assertEqual(len(self.exit.opened), 2)
        self.assertEqual(self.mp.n, 1)

    def test_fuzzy_exit_and_cash(self):
        self.app.on_plate(self.enter, "01KG123ABC")
        self.backdate("01KG123ABC", 25 * 60)
        self.app.on_plate(self.exit, "01KG128ABC")  # ошибка распознавания в 1 символ
        p = self.app.state()["current"][0]
        self.assertEqual(p["amount"], 1500)
        self.app.pay_cash(p["id"])
        self.app.process_receipts()
        self.assertEqual(self.kkm.receipts[0][2:], (0, True, "Наличные"))
        self.assertEqual(self.exit.opened, ["01KG123ABC"])  # номер из записи въезда

    def test_late_qr_payment_after_other_car(self):
        self.app.on_plate(self.enter, "AAA111")
        self.app.on_plate(self.enter, "BBB222")
        self.backdate("AAA111", 120)
        self.backdate("BBB222", 120)
        self.app.on_plate(self.exit, "AAA111")
        self.app.on_plate(self.exit, "BBB222")  # первая машина отъехала, подъехала вторая
        self.mp.paid.add("M1")                 # первая всё-таки оплатила
        self.app.poll_payments()
        self.assertEqual(self.exit.opened, [])  # шлагбаум не открываем — у него BBB222
        self.assertEqual(self.app.screen_state()["plate"], "BBB222")
        # AAA111 вернулась к шлагбауму через минуту — оплачено, выпускаем
        self.app.on_plate(self.exit, "AAA111")
        self.assertEqual(self.exit.opened, ["AAA111"])

    def test_unknown_and_whitelist(self):
        self.app.on_plate(self.exit, "ZZZ999")
        self.assertEqual(self.exit.opened, [])
        self.assertEqual(self.app.state()["unknown"][0]["plate"], "ZZZ999")
        self.assertEqual(self.app.screen_state()["state"], "message")
        pid = self.app.bill("Выезд", "ZZZ999", 300)["id"]
        self.assertEqual(self.app.screen_state()["amount"], 300)
        self.app.release_free(pid)
        self.assertEqual(self.exit.opened, ["ZZZ999"])

        wl = self.app.cameras["Въезд WL"]
        self.app.on_plate(wl, "06978ATH")
        self.assertEqual(wl.opened, [])
        self.app.wl.add("6978ATH", "permanent", "Тест", "Telegram")
        self.assertIn("6978ATH", self.notes[0])
        self.app.on_plate(wl, "06978ATH")
        self.assertEqual(wl.opened, ["06978ATH"])
        self.app.on_plate(self.enter, "6978ATH")
        self.backdate("6978ATH", 300)
        self.app.on_plate(self.exit, "6978ATH")
        self.assertEqual(self.exit.opened[-1], "6978ATH")

    def test_receipt_retry(self):
        self.kkm.fail = True
        self.app.on_plate(self.enter, "AAA111")
        self.backdate("AAA111", 30)
        self.app.on_plate(self.exit, "AAA111")
        pid = self.app.state()["current"][0]["id"]
        self.app.pay_cash(pid)
        self.app.process_receipts()
        self.assertEqual(self.app.payment(pid)["receipt_status"], "pending")
        self.kkm.fail = False
        self.app.retry_receipt(pid)
        self.app.process_receipts()
        self.assertEqual(self.app.payment(pid)["receipt_status"], "done")
        # нет связи -> повтор с тем же cacheId (не задвоить чек)
        self.assertEqual(self.kkm.cache_ids[-2:], ["akjol-pay-%d-0" % pid] * 2)

    def test_expiry(self):
        self.app.on_plate(self.enter, "AAA111")
        self.backdate("AAA111", 30)
        self.app.on_plate(self.exit, "AAA111")
        self.db.execute("UPDATE payments SET created=?", (now_str(datetime.now() - timedelta(seconds=301)),))
        self.app.poll_payments()
        self.assertEqual(self.app.screen_state()["state"], "stop")
        # оплатил чуть позже — всё равно засчитываем
        self.mp.paid.add("M1")
        self.app.poll_payments()
        self.assertEqual(self.app.payment(1)["status"], "success")


class WhitelistTest(Base):
    def test_hidden_file_not_logged_or_listed(self):
        wl = self.app.cameras["Въезд WL"]
        with open(self.hidden, "w", encoding="utf-8") as f:
            f.write("01KG555XYZ какой-то комментарий\n")
        self.app.on_plate(wl, "01KG555XYZ")
        self.assertEqual(wl.opened, ["01KG555XYZ"])
        self.assertEqual(self.app.wl.list(), [])
        self.assertIsNone(self.db.one("SELECT * FROM events WHERE plate='01KG555XYZ'"))
        self.assertEqual(self.notes, [])
        # на общем выезде бесплатно, без слова whitelist
        self.app.on_plate(self.enter, "01KG555XYZ")
        self.backdate("01KG555XYZ", 300)
        self.app.on_plate(self.exit, "01KG555XYZ")
        self.assertEqual(self.exit.opened, ["01KG555XYZ"])
        msgs = " ".join(e["message"] for e in self.db.query("SELECT message FROM events"))
        self.assertNotIn("whitelist", msgs.lower())

    def test_month_expiry(self):
        r = self.app.wl.add("01KG777AAA", "month", "Иван", "сайт")
        self.assertTrue(r["expires"] > now_str())
        self.assertEqual(self.app.wl.check(plates.key("01KG777AAA")), "main")
        self.db.execute("UPDATE whitelist SET expires=?", (now_str(datetime.now() - timedelta(minutes=1)),))
        self.assertIsNone(self.app.wl.check(plates.key("01KG777AAA")))
        self.assertEqual(self.app.wl.expire(), 1)
        self.assertIn("закончился месяц", self.notes[-1])
        self.assertTrue(self.app.wl.remove("XXX111", "a", "b") is False)

    def test_add_month_dates(self):
        from parking.whitelist import add_month
        self.assertEqual(add_month(datetime(2026, 1, 31)), datetime(2026, 2, 28))
        self.assertEqual(add_month(datetime(2026, 12, 15)), datetime(2027, 1, 15))

    def test_telegram_flow(self):
        import hashlib
        from parking.telegram import TelegramBot
        sent = []
        bot = TelegramBot({"enabled": True, "token": "x"},
                          {"admin_login": "admin", "admin_password_sha256": hashlib.sha256(b"admin2026").hexdigest()},
                          self.db, lambda *a: None)
        bot.api = lambda m, p=None, timeout=15: {"ok": True}
        orig_send = bot.send
        bot.send = lambda chat, text, markup=None: (sent.append((chat, text, markup)), orig_send(chat, text, markup))
        bot.wl = self.app.wl
        self.app.wl.notify = bot.notify
        msg = lambda t: {"message": {"message_id": 1, "chat": {"id": 5, "type": "private"},  # noqa: E731
                                     "from": {"first_name": "Азиз"}, "text": t}}
        bot.handle(msg("/start"))
        bot.handle(msg("admin"))
        bot.handle(msg("wrong"))
        self.assertIn("Неверный", sent[-1][1])
        bot.handle(msg("admin"))
        bot.handle(msg("admin2026"))
        self.assertIn("Вход выполнен", sent[-1][1])
        bot.handle(msg("01 kg 321 bbb"))
        cb = sent[-1][2]["inline_keyboard"][0][1]["callback_data"]
        bot.handle({"callback_query": {"id": "q", "data": cb, "from": {"first_name": "Азиз"},
                                       "message": {"message_id": 2, "chat": {"id": 5}}}})
        row = self.app.wl.list()[0]
        self.assertEqual((row["plate"], row["category"], row["source"]), ("01KG321BBB", "month", "Telegram"))
        # уведомление сохранено в очереди для авторизованного чата
        out = self.db.query("SELECT chat_id, text FROM tg_outbox")
        self.assertEqual(out[-1]["chat_id"], 5)
        self.assertIn("01KG321BBB", out[-1]["text"])
        # неавторизованный не может добавлять
        bot.handle({"message": {"message_id": 3, "chat": {"id": 9, "type": "private"}, "from": {}, "text": "/start"}})
        bot.handle({"callback_query": {"id": "q", "data": "add|permanent|HACK111", "from": {},
                                       "message": {"message_id": 4, "chat": {"id": 9}}}})
        self.assertEqual(len(self.app.wl.list()), 1)

    def test_site(self):
        import hashlib
        import http.client
        import json as _json
        from parking import wlsite
        cfg = {"admin_login": "admin", "admin_password_sha256": hashlib.sha256(b"admin2026").hexdigest(),
               "site_host": "127.0.0.1", "site_port": 0}
        srv = wlsite.serve(self.app.wl, cfg)
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        def call(path, data=None, cookie=None):
            c = http.client.HTTPConnection("127.0.0.1", srv.server_port, timeout=5)
            h = {"Content-Type": "application/json"}
            if cookie:
                h["Cookie"] = cookie
            c.request("POST" if data is not None else "GET", path, _json.dumps(data) if data is not None else None, h)
            r = c.getresponse()
            return r.status, _json.loads(r.read() or b"{}"), r.getheader("Set-Cookie")

        self.assertEqual(call("/api/add", {"plate": "AAA111", "category": "permanent"})[0], 401)
        self.assertEqual(call("/api/login", {"login": "admin", "password": "bad"})[0], 401)
        st, _, ck = call("/api/login", {"login": "admin", "password": "admin2026"})
        self.assertEqual(st, 200)
        ck = ck.split(";")[0]
        self.assertEqual(call("/api/add", {"plate": "AAA111", "category": "month"}, ck)[0], 200)
        self.assertEqual(call("/api/list", None, ck)[1][0]["plate"], "AAA111")
        self.assertIn("сайт", self.notes[-1])
        self.assertEqual(call("/api/remove", {"plate": "AAA111"}, ck)[1], {"ok": True})
        srv.shutdown()


class ExtraTest(Base):
    def test_blacklist(self):
        self.app.blacklist_add("01KG666BAD", "не платил")
        self.app.on_plate(self.enter, "01KG666BAD")
        self.assertEqual(self.enter.opened, [])
        st = self.app.state()
        self.assertEqual(st["alerts"][0]["plate"], "01KG666BAD")
        self.assertIn("ЧЁРНОГО СПИСКА", self.notes[-1])
        wl = self.app.cameras["Въезд WL"]
        self.app.wl.add("01KG666BAD", "permanent", "x", "x")
        self.app.on_plate(wl, "01KG666BAD")
        self.assertEqual(wl.opened, [])  # чёрный список важнее белого
        self.app.dismiss_alert(st["alerts"][0]["id"])
        self.app.blacklist_remove("01KG666BAD")
        self.app.on_plate(self.enter, "01KG666BAD")
        self.assertEqual(self.enter.opened, ["01KG666BAD"])

    def test_operator_corrects_plate(self):
        self.app.on_plate(self.enter, "01KG123ABC")
        self.backdate("01KG123ABC", 90)
        self.app.on_plate(self.exit, "01KG999ZZZ")  # камера прочитала совсем не то
        self.assertEqual(self.app.state()["unknown"][0]["plate"], "01KG999ZZZ")
        with self.assertRaises(ValueError):
            self.app.bill("Выезд", "01KG999ZZZ")      # без суммы и без въезда — нельзя
        r = self.app.bill("Выезд", "01KG123ABC")       # оператор исправил номер
        self.assertEqual(r["amount"], 100)
        self.assertEqual(self.app.screen_state()["qr_text"], "QRDATA%d" % r["id"])
        # повторное «QR» снимает старый счёт и делает новый
        r2 = self.app.bill("Выезд", "01KG123ABC")
        self.assertEqual(self.app.payment(r["id"])["status"], "cancelled")
        self.mp.paid.add("M%d" % r2["id"])
        self.app.poll_payments()
        self.assertEqual(self.exit.opened, ["01KG123ABC"])
        self.app.process_receipts()
        self.assertEqual(self.app.screen_state()["receipt_qr"], "http://tax/%d" % r2["id"])

    def test_restart_restores_qr(self):
        self.app.on_plate(self.enter, "AAA111")
        self.backdate("AAA111", 30)
        self.app.on_plate(self.exit, "AAA111")
        app2 = Parking(CFG, self.db, self.mp, self.kkm, camera_cls=FakeCam, async_barrier=False,
                       hidden_whitelist_path=self.hidden)
        self.assertEqual(app2.screen_state()["state"], "qr")
        app2._generate_qr_async = app2._generate_qr
        r = app2.bill("Выезд", "AAA111")
        self.assertEqual([p["status"] for p in self.db.query("SELECT status FROM payments ORDER BY id")],
                         ["cancelled", "pending"])
        self.assertEqual(r["id"], 2)

    def test_edit_session_plate(self):
        self.app.on_plate(self.enter, "01KG12ABC")
        sid = self.db.one("SELECT id FROM sessions")["id"]
        self.app.set_session_plate(sid, "01KG777XYZ")
        self.backdate("01KG777XYZ", 5)
        self.app.on_plate(self.exit, "01KG777XYZ")
        self.assertEqual(self.exit.opened, ["01KG777XYZ"])

    def test_terminal_card_and_abort(self):
        self.kkm.cfg["terminal_auto"] = True
        self.kkm.card_result = {"status": 0, "fdNumber": 555, "qrCode": "http://tax/card"}
        self.app.on_plate(self.enter, "AAA111")
        self.backdate("AAA111", 30)
        self.app.on_plate(self.exit, "AAA111")
        for _ in range(50):
            if self.exit.opened:
                break
            time.sleep(0.05)
        p = self.app.payment(1)
        self.assertEqual((p["status"], p["method"], p["receipt_fd"]), ("success", "card_kkm", 555))
        self.assertEqual(self.exit.opened, ["AAA111"])
        # QR оплачен, пока терминал ждёт карту — терминал отменяется
        self.app.terminal_busy = 2
        self.app.on_plate(self.enter, "BBB222")
        self.backdate("BBB222", 30)
        self.kkm.cfg["terminal_auto"] = False
        self.app.on_plate(self.exit, "BBB222")
        self.app.terminal_busy = 2
        self.mp.paid.add("M2")
        self.app.poll_payments()
        time.sleep(0.1)
        self.assertEqual(self.kkm.aborted, 1)


class ReviewRegressionTest(Base):
    def test_old_qr_paid_opens_and_cancels_new(self):
        self.app.on_plate(self.enter, "AAA111")
        self.backdate("AAA111", 90)
        self.app.on_plate(self.exit, "AAA111")       # QR #1
        r2 = self.app.bill("Выезд", "AAA111")         # оператор перевыставил: QR #2
        self.mp.paid.add("M1")                       # клиент оплатил старый #1
        self.app.poll_payments()
        self.assertEqual(self.exit.opened, ["AAA111"])
        self.assertEqual(self.app.payment(r2["id"])["status"], "cancelled")
        self.assertEqual(self.app.screen_state()["state"], "paid")

    def test_paid_after_expiry_car_still_waiting(self):
        self.app.on_plate(self.enter, "AAA111")
        self.backdate("AAA111", 30)
        self.app.on_plate(self.exit, "AAA111")
        self.db.execute("UPDATE payments SET created=?", (now_str(datetime.now() - timedelta(seconds=301)),))
        self.app.poll_payments()                     # QR истёк
        self.mp.paid.add("M1")
        self.app.poll_payments()                     # оплата пришла позже, машина стоит
        self.assertEqual(self.exit.opened, ["AAA111"])

    def test_hidden_leaves_no_trace_anywhere(self):
        with open(self.hidden, "w") as f:
            f.write("01KG999HID\n")
        self.app.on_plate(self.enter, "01KG999HID")
        self.app.on_plate(self.exit, "01KG999HID")
        self.assertEqual(self.enter.opened, ["01KG999HID"])
        self.assertEqual(self.exit.opened, ["01KG999HID"])
        self.assertEqual(self.db.query("SELECT * FROM sessions"), [])
        self.assertEqual(self.db.query("SELECT * FROM events WHERE plate LIKE '%HID%' OR message LIKE '%HID%'"), [])
        self.assertIs(self.app.on_plate(self.exit, "01KG999HID", manual=True), False)

    def test_callback_needs_secret(self):
        self.app.on_plate(self.enter, "AAA111")
        self.backdate("AAA111", 30)
        self.app.on_plate(self.exit, "AAA111")
        self.assertFalse(self.app.megapay_callback({"qrTransactionId": "M1", "status": "success"}))
        self.assertEqual(self.exit.opened, [])
        self.mp.cfg["callback_secret"] = "s3cret"
        self.assertTrue(self.app.megapay_callback({"qrTransactionId": "M1", "status": "success", "secret": "s3cret"}))

    def test_password_change_logs_out_bot(self):
        import hashlib
        from parking.telegram import TelegramBot
        wl_cfg = {"admin_login": "admin", "admin_password_sha256": hashlib.sha256(b"admin2026").hexdigest()}
        bot = TelegramBot({"enabled": True, "token": "x"}, wl_cfg, self.db, lambda *a: None)
        bot.api = lambda m, p=None, timeout=15: {"ok": True}
        bot.wl = self.app.wl
        msg = lambda t: {"message": {"message_id": 1, "chat": {"id": 7, "type": "private"}, "from": {}, "text": t}}  # noqa
        for t in ("/start", "admin", "admin2026"):
            bot.handle(msg(t))
        bot.notify("x")
        self.assertEqual(self.db.one("SELECT COUNT(*) AS n FROM tg_outbox WHERE text='x'")["n"], 1)
        wl_cfg["admin_password_sha256"] = hashlib.sha256(b"new").hexdigest()
        bot.notify("y")
        self.assertEqual(self.db.one("SELECT COUNT(*) AS n FROM tg_outbox WHERE text='y'")["n"], 0)
        self.assertFalse(bot.wl is None)
        self.assertFalse(__import__("parking.whitelist", fromlist=["x"]).check_password(wl_cfg, "админ", "x"))


EVENT = ('Code=TrafficJunction;action=Pulse;index=0;data={\n   "Object" : { "Text" : "01KG777AAA" },\n'
         '   "TrafficCar" : { "PlateNumber" : "01KG777AAA" }\n}\n')


class DahuaStreamTest(unittest.TestCase):
    def test_parse(self):
        ev = parse_event_text(EVENT)
        self.assertEqual(ev["code"], "TrafficJunction")
        self.assertEqual(ev["data"]["TrafficCar"]["PlateNumber"], "01KG777AAA")

    def test_stream_and_open(self):
        calls = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                calls.append(self.path)
                if "openStrobe" in self.path:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"OK\r\n")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=myboundary")
                self.end_headers()
                for body in ("Heartbeat\r\n", EVENT):
                    b = body.encode()
                    self.wfile.write(b"--myboundary\r\nContent-Type: text/plain\r\nContent-Length: %d\r\n\r\n" % len(b))
                    self.wfile.write(b + b"\r\n")
                    self.wfile.flush()
                time.sleep(1)

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        got = []
        cam = Camera({"name": "c", "role": "exit", "host": "127.0.0.1", "port": srv.server_port},
                     {}, lambda c, p, **kw: got.append(p), lambda *a: None)
        cam.start()
        for _ in range(30):
            if got:
                break
            time.sleep(0.1)
        cam.stop()
        self.assertEqual(got, ["01KG777AAA"])
        ok, _ = cam.open_barrier("01KG777AAA")
        self.assertTrue(ok)
        self.assertIn("plateNumber=01KG777AAA", calls[-1])
        srv.shutdown()


if __name__ == "__main__":
    unittest.main()
