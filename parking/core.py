"""Логика парковки: въезд, выезд, оплата, чеки, whitelist."""
import json
import threading
import time
from datetime import datetime, timedelta

from . import plates
from .cameras import Camera
from .db import now_str, parse_dt
from .kkm import KKMError, PAY_CASH, PAY_CASHLESS, PRINTER_STATUSES
from .megapay import MegapayError
from .tariff import calc_fee, format_duration
from .whitelist import Whitelist

# способ оплаты -> (payType в чеке, подпись)
RECEIPT_PAY = {
    "qr": (PAY_CASHLESS, "QR Megapay"),
    "cash": (PAY_CASH, "Наличные"),
    "card": (PAY_CASHLESS, "Банковская карта"),
}

DEFAULT_PARKING = {
    "dedupe_seconds": 10,          # один и тот же номер с одной камеры чаще не обрабатываем
    "reentry_window_seconds": 120,  # повторный въезд в это окно — просто открыть, не заводить заново
    "paid_grace_minutes": 15,       # после оплаты/выезда повторное распознавание на выезде — открыть
    "fuzzy_exit_match": True,       # на выезде допускать ошибку распознавания в 1 символ
    "whitelist_free_on_main": True,  # whitelist бесплатно и на общем въезде/выезде
    "unknown_message_seconds": 20,
}


class Parking:
    def __init__(self, cfg, db, megapay, kkm, camera_cls=Camera, async_barrier=True,
                 hidden_whitelist_path="whitelist2.txt", notify=None):
        self.cfg = cfg
        self.p = dict(DEFAULT_PARKING)
        self.p.update(cfg.get("parking", {}))
        self.tariff = cfg.get("tariff", {})
        self.db = db
        self.megapay = megapay
        self.kkm = kkm
        self.async_barrier = async_barrier
        self.lock = threading.RLock()
        self.current = {}      # камера выезда -> id ожидающей оплаты
        self.unknown = {}      # камера выезда -> номер без записи о въезде
        self.screen_msg = None  # (текст, до какого времени)
        self.paid_flash = None  # {amount, plate, until}
        self.last_seen = {}
        self.notify = notify or (lambda text: None)
        self.wl = Whitelist(db, hidden_whitelist_path, notify, self.log)
        self.at_exit = {}         # камера выезда -> (id заезда машины у шлагбаума, время)
        self.alerts = []          # чёрный список у шлагбаума — показываем оператору
        self.terminal_busy = None  # id оплаты, по которой ждёт терминал (через ККМ)
        self.receipt_wakeup = threading.Event()
        self._stop = threading.Event()
        self.cameras = {}
        for c in cfg.get("cameras", []):
            cam = camera_cls(c, cfg.get("camera_api"), self.on_plate, self.log)
            self.cameras[cam.name] = cam
        exits = [c.name for c in self.cameras.values() if c.role == "exit"]
        self.screen_camera = cfg.get("screen", {}).get("camera") or (exits[0] if exits else None)
        self._restore_current()
        if self.db.get_meta("last_close_day") is None:
            # при первом запуске не закрываем смену сразу, но и сегодняшнее закрытие не пропускаем
            now, t = datetime.now(), self.kkm.cfg.get("close_day_time") or "23:59"
            day = now if now.strftime("%H:%M") >= t else now - timedelta(days=1)
            self.db.set_meta("last_close_day", day.strftime("%Y-%m-%d"))

    def _restore_current(self):
        """После перезапуска возвращаем на экран неоплаченный QR, который ещё не истёк."""
        timeout = self.megapay.cfg.get("timeout_seconds", 300)
        since = now_str(datetime.now() - timedelta(seconds=timeout))
        for cam in self.cameras.values():
            if cam.role != "exit":
                continue
            p = self.db.one("SELECT id FROM payments WHERE camera=? AND status='pending' AND created>=? "
                            "ORDER BY id DESC LIMIT 1", (cam.name, since))
            if p:
                self.current[cam.name] = p["id"]

    # ------------------------------------------------------------------ служебное
    def log(self, kind, message, camera=None, plate=None):
        self.db.event(kind, message, camera, plate)
        print("%s [%s] %s" % (now_str(), kind, message), flush=True)

    def start(self):
        for cam in self.cameras.values():
            cam.start()
        for target, name in ((self._payment_loop, "payments"), (self._receipt_loop, "receipts"),
                             (self._close_day_loop, "close-day"), (self._whitelist_loop, "whitelist")):
            threading.Thread(target=target, name=name, daemon=True).start()
        self.log("system", "Программа запущена")

    def stop(self):
        self._stop.set()
        self.receipt_wakeup.set()
        for cam in self.cameras.values():
            cam.stop()

    def open_barrier(self, cam, plate, reason, quiet=False):
        def do():
            ok, body = cam.open_barrier(plate)
            if quiet:  # второй белый список — без следов в журнале
                if not ok:
                    self.log("error", "Шлагбаум %s НЕ открылся: %s" % (cam.name, body), cam.name)
            elif ok:
                self.log("barrier", "Шлагбаум %s открыт: %s" % (cam.name, reason), cam.name, plate)
            else:
                self.log("error", "Шлагбаум %s НЕ открылся (%s): %s" % (cam.name, reason, body), cam.name, plate)
        if self.async_barrier:
            threading.Thread(target=do, daemon=True).start()
        else:
            do()

    def _whitelist_loop(self):
        while not self._stop.wait(60):
            try:
                self.wl.expire()
            except Exception as e:  # noqa: BLE001
                self.log("error", "Белый список: %s" % e)

    def session(self, sid):
        return self.db.one("SELECT * FROM sessions WHERE id=?", (sid,))

    def payment(self, pid):
        return self.db.one("SELECT * FROM payments WHERE id=?", (pid,))

    def _close_session(self, sid, reason, when=None):
        self.db.execute(
            "UPDATE sessions SET status='closed', exit_time=?, close_reason=? WHERE id=?",
            (now_str(when), reason, sid),
        )

    def _find_open_session(self, key):
        s = self.db.one(
            "SELECT * FROM sessions WHERE plate_key=? AND status='open' ORDER BY id DESC LIMIT 1", (key,)
        )
        if s or not self.p["fuzzy_exit_match"]:
            return s
        cands = [r for r in self.db.query("SELECT * FROM sessions WHERE status='open'")
                 if plates.close_match(r["plate_key"], key)]
        return cands[0] if len(cands) == 1 else None

    # ------------------------------------------------------------------ камеры
    def on_plate(self, cam, plate, manual=False):
        if not plates.is_valid(plate):
            return
        plate = plates.normalize(plate)
        k = plates.key(plate)
        t = time.time()
        with self.lock:
            if len(self.last_seen) > 5000:
                self.last_seen = {key: v for key, v in self.last_seen.items() if t - v < 3600}
            last = self.last_seen.get((cam.name, k), 0)
            self.last_seen[(cam.name, k)] = t
            if t - last < self.p["dedupe_seconds"] and not manual:
                return
        handler = {
            "enter": self._on_enter,
            "exit": self._on_exit,
            "wl_enter": self._on_wl_enter,
            "wl_exit": self._on_wl_exit,
        }.get(cam.role)
        if handler:
            return handler(cam, plate, k)

    # ------------------------------------------------------------------ чёрный список
    def blacklisted(self, key):
        if self.wl.check(key) == "hidden":
            return None
        return self.db.one("SELECT * FROM blacklist WHERE plate_key=?", (key,))

    def _blacklist_alert(self, cam, plate, row, opened):
        text = "⛔ %s из ЧЁРНОГО СПИСКА у шлагбаума «%s»%s%s" % (
            plate, cam.name, (" — " + row["reason"]) if row["reason"] else "",
            ", выпуск только после оплаты" if opened is None else "" if opened else ", шлагбаум НЕ открыт")
        with self.lock:
            self.alerts = [a for a in self.alerts if a["plate"] != plate][-9:] + [
                {"id": int(time.time() * 1000), "camera": cam.name, "plate": plate,
                 "reason": row["reason"], "time": now_str(), "opened": opened}]
        self.log("alert", text, cam.name, plate)
        if self.cfg.get("telegram", {}).get("notify_blacklist", True):
            self.notify(text)

    def blacklist_add(self, plate, reason=""):
        plate = plates.normalize(plate)
        if not plates.is_valid(plate):
            raise ValueError("неверный номер: %r" % plate)
        self.db.execute("INSERT OR REPLACE INTO blacklist(plate_key, plate, reason, created) VALUES (?,?,?,?)",
                        (plates.key(plate), plate, reason or "", now_str()))
        self.log("blacklist", "Чёрный список: добавлен %s%s" % (plate, (" — " + reason) if reason else ""),
                 None, plate)
        return plate

    def blacklist_remove(self, plate):
        k = plates.key(plate)
        self.db.execute("DELETE FROM blacklist WHERE plate_key=?", (k,))
        self.log("blacklist", "Чёрный список: удалён %s" % plates.normalize(plate), None, plates.normalize(plate))

    def dismiss_alert(self, alert_id=None):
        with self.lock:
            self.alerts = [] if alert_id is None else [a for a in self.alerts if a["id"] != int(alert_id)]

    def _on_enter(self, cam, plate, k):
        now = datetime.now()
        if self.wl.check(k) == "hidden":
            self.open_barrier(cam, plate, "", quiet=True)  # второй список: без записи и журнала
            return False
        bl = self.blacklisted(k)
        if bl:
            return self._blacklist_alert(cam, plate, bl, False)
        wl = self.wl.check(k) == "main"
        with self.lock:
            s = self.db.one(
                "SELECT * FROM sessions WHERE plate_key=? AND status='open' ORDER BY id DESC LIMIT 1", (k,)
            )
            if s:
                age = (now - parse_dt(s["entry_time"])).total_seconds()
                if age < self.p["reentry_window_seconds"]:
                    self.open_barrier(cam, plate, "повторное распознавание на въезде")
                    return
                self._close_session(s["id"], "reentry", now)
                self.log("alert", "%s: въезд без зафиксированного выезда, старый заезд с %s закрыт без оплаты"
                         % (plate, s["entry_time"]), cam.name, plate)
            self.db.execute(
                "INSERT INTO sessions(plate, plate_key, entry_time, note) VALUES (?,?,?,?)",
                (plate, k, now_str(now), "whitelist" if wl else None),
            )
        self.log("enter", "Въезд %s%s" % (plate, " (whitelist)" if wl else ""), cam.name, plate)
        self.open_barrier(cam, plate, "въезд")

    def _on_exit(self, cam, plate, k):
        now = datetime.now()
        new_pid = None
        with self.lock:
            wl = self.wl.check(k) if self.p["whitelist_free_on_main"] else None
            if wl:
                for s in self.db.query("SELECT id FROM sessions WHERE plate_key=? AND status='open'", (k,)):
                    self._close_session(s["id"], "whitelist" if wl == "main" else "free", now)
                self.unknown.pop(cam.name, None)
                if wl == "main":
                    self.log("exit", "Выезд %s (whitelist, бесплатно)" % plate, cam.name, plate)
                    self.open_barrier(cam, plate, "whitelist")
                    return
                self.open_barrier(cam, plate, "", quiet=True)
                return False

            bl = self.blacklisted(k)
            if bl:
                self._blacklist_alert(cam, plate, bl, None)
            s = self._find_open_session(k)
            if not s:
                grace = now - timedelta(minutes=self.p["paid_grace_minutes"])
                recent = self.db.one(
                    "SELECT * FROM sessions WHERE plate_key=? AND status='closed' AND exit_time>=? "
                    "AND close_reason IN ('paid','free','operator','whitelist') ORDER BY id DESC LIMIT 1",
                    (k, now_str(grace)),
                )
                if recent:
                    self.log("exit", "%s: уже оплачено/выпущен в %s — открываю" % (plate, recent["exit_time"]),
                             cam.name, plate)
                    self.open_barrier(cam, plate, "повторное распознавание после оплаты")
                    return
                self.unknown[cam.name] = {"plate": plate, "time": now_str(now)}
                self.screen_msg = ("Обратитесь к оператору",
                                   time.time() + self.p["unknown_message_seconds"])
                self.log("alert", "%s: нет записи о въезде — нужен оператор" % plate, cam.name, plate)
                return

            self.unknown.pop(cam.name, None)
            self.at_exit[cam.name] = (s["id"], time.time())
            cur = self.current.get(cam.name)
            if cur:
                p = self.payment(cur)
                if p and p["status"] == "pending" and p["session_id"] == s["id"]:
                    return  # QR для этой машины уже на экране
            start = parse_dt(s["paid_until"] or s["entry_time"])
            dur = (now - start).total_seconds()
            fee = calc_fee(dur, self.tariff)
            if fee == 0:
                reason = "paid" if s["paid_until"] else "free"
                self._close_session(s["id"], reason, now)
                if cur:
                    self.current.pop(cam.name, None)
                self.log("exit", "Выезд %s, %s — бесплатно" % (plate, format_duration(dur)), cam.name, plate)
                self.open_barrier(cam, plate, "выезд без оплаты")
                return
            new_pid = self._create_payment(s["id"], cam.name, s["plate"], fee, dur, now)
        self.log("exit", "Выезд %s, %s — к оплате %d сом" % (plate, format_duration(dur), fee), cam.name, plate)
        self._generate_qr_async(new_pid)
        self._start_terminal(new_pid)

    def _on_wl_enter(self, cam, plate, k):
        wl = self.wl.check(k)
        if wl == "hidden":
            self.open_barrier(cam, plate, "", quiet=True)
            return False
        bl = self.blacklisted(k)
        if bl:
            self._blacklist_alert(cam, plate, bl, False)
        elif wl == "main":
            self.log("enter", "Whitelist въезд %s — пропущен" % plate, cam.name, plate)
            self.open_barrier(cam, plate, "whitelist")
        else:
            self.log("denied", "Whitelist въезд %s — нет в списке, не открываю" % plate, cam.name, plate)

    def _on_wl_exit(self, cam, plate, k):
        hidden = self.wl.check(k) == "hidden"
        if not hidden:
            self.log("exit", "Whitelist выезд %s" % plate, cam.name, plate)
        if cam.cfg.get("open_on_plate"):
            self.open_barrier(cam, plate, "whitelist выезд", quiet=hidden)
        return False if hidden else None

    # ------------------------------------------------------------------ оплата
    def _create_payment(self, sid, camera, plate, amount, dur, now=None):
        ts = now_str(now)
        pid = self.db.execute(
            "INSERT INTO payments(session_id, camera, plate, amount, duration_seconds, created, updated) "
            "VALUES (?,?,?,?,?,?,?)",
            (sid, camera, plate, int(amount), int(dur), ts, ts),
        )
        old = self.current.get(camera)
        if old and old != pid:
            self.log("payment", "Оплата #%d снята с экрана (у шлагбаума другая машина), QR остаётся действительным"
                     % old, camera)
        self.current[camera] = pid
        return pid

    def _generate_qr_async(self, pid):
        if not self.megapay.enabled:
            return
        threading.Thread(target=self._generate_qr, args=(pid,), daemon=True).start()

    def _generate_qr(self, pid):
        p = self.payment(pid)
        try:
            qr = self.megapay.generate(p["amount"], p["plate"], pid)
            self.db.execute(
                "UPDATE payments SET megapay_id=?, qr_image=?, qr_text=?, qr_error=NULL, updated=? WHERE id=?",
                (qr["id"], qr["image"], qr["text"], now_str(), pid),
            )
            if not qr["id"]:
                self.log("error", "Megapay: в ответе нет id транзакции, статус оплаты проверить нельзя", p["camera"])
        except MegapayError as e:
            self.db.execute("UPDATE payments SET qr_error=?, updated=? WHERE id=?", (str(e), now_str(), pid))
            self.log("error", "Megapay: QR не создан для #%d: %s" % (pid, e), p["camera"], p["plate"])

    def mark_paid(self, pid, method, receipt_resp=None):
        """Фиксирует оплату. Если машина у шлагбаума — открывает. Возвращает True, если оплата новая."""
        with self.lock:
            p = self.payment(pid)
            if not p or p["status"] == "success":
                return False
            ts = now_str()
            if receipt_resp is not None:
                rstatus, fd, rqr = "done", receipt_resp.get("fdNumber"), receipt_resp.get("qrCode")
            else:
                rstatus, fd, rqr = ("pending" if self.kkm.enabled else "skipped"), None, None
            self.db.execute(
                "UPDATE payments SET status='success', method=?, paid_time=?, updated=?, "
                "receipt_status=?, receipt_fd=?, receipt_qr=? WHERE id=?",
                (method, ts, ts, rstatus, fd, rqr, pid),
            )
            self.db.execute(
                "UPDATE sessions SET paid_total=paid_total+?, paid_until=? WHERE id=?",
                (p["amount"], ts, p["session_id"]),
            )
            cur = self.current.get(p["camera"])
            cur_p = self.payment(cur) if cur and cur != pid else None
            if cur == pid:
                at_barrier = True
            elif cur_p and cur_p["session_id"] == p["session_id"]:
                # оплачен старый QR той же машины — новый снимаем, чтобы не заплатили дважды
                self._cancel_locked(cur_p)
                at_barrier = True
            elif cur is None:
                # QR истёк/снят, но та же машина всё ещё у шлагбаума (стоит, камера не срабатывает)
                sid_t = self.at_exit.get(p["camera"])
                at_barrier = bool(sid_t and sid_t[0] == p["session_id"] and time.time() - sid_t[1] < 1800)
            else:
                at_barrier = False
            if at_barrier:
                self.current.pop(p["camera"], None)
                s = self.session(p["session_id"])
                if s and s["status"] == "open":
                    self._close_session(s["id"], "paid")
                self.paid_flash = {"amount": p["amount"], "plate": p["plate"], "pid": pid,
                                   "until": time.time() + self.cfg.get("screen", {}).get("paid_seconds", 8)}
        self.log("payment", "Оплачено #%d: %s, %d сом (%s)%s" % (
            pid, p["plate"], p["amount"], method, "" if at_barrier else " — машина не у шлагбаума"),
            p["camera"], p["plate"])
        if at_barrier and p["camera"] in self.cameras:
            self.open_barrier(self.cameras[p["camera"]], p["plate"], "оплачено")
        if method != "card_kkm":
            self._abort_terminal(pid)
        self.receipt_wakeup.set()
        return True

    # ------------------------------------------------------------------ терминал через ККМ
    def _start_terminal(self, pid):
        """Параллельно с QR запускаем оплату картой на терминале, подключённом к ККМ."""
        if pid and self.kkm.enabled and self.kkm.cfg.get("terminal_auto"):
            threading.Thread(target=self._terminal_pay, args=(pid,), daemon=True).start()

    def _terminal_pay(self, pid):
        for _ in range(60):  # ждём, пока терминал освободится после предыдущей машины
            with self.lock:
                if self.terminal_busy is None:
                    p = self.payment(pid)
                    if not p or p["status"] != "pending":
                        return
                    self.terminal_busy = pid
                    break
            time.sleep(0.5)
        else:
            return
        try:
            resp = self._card_via_kkm(p)
            if not self.mark_paid(pid, "card_kkm", resp):
                self.log("alert", "ВНИМАНИЕ: оплата картой #%d прошла после другой оплаты — проверьте возврат"
                         % pid, p["camera"], p["plate"])
        except KKMError as e:
            if (self.payment(pid) or {}).get("status") == "pending":
                self.log("payment", "Терминал #%d: %s" % (pid, e), p["camera"], p["plate"])
        finally:
            with self.lock:
                if self.terminal_busy == pid:
                    self.terminal_busy = None

    def _card_via_kkm(self, p):
        """Оплата картой через ККМ. Если связь оборвалась посреди оплаты, исход неизвестен:
        следующая попытка идёт с тем же cacheId — ККМ вернёт результат, а не спишет второй раз."""
        cid = p.get("card_cache_id") or "akjol-card-%d-%d" % (p["id"], time.time())
        self.db.execute("UPDATE payments SET card_cache_id=? WHERE id=?", (cid, p["id"]))
        try:
            resp = self.kkm.receipt(p, PAY_CASHLESS, False, "Банковская карта", cid,
                                    timeout=self.kkm.cfg.get("card_timeout", 180))
        except KKMError as e:
            if e.status is None:
                self.log("alert", "Связь с ККМ оборвалась во время оплаты картой #%d (%s, %d сом). Исход неизвестен — "
                         "проверьте терминал/«Копия последнего чека» и повторите «Карта через ККМ»: второй раз "
                         "не спишет" % (p["id"], p["plate"], p["amount"]), p["camera"], p["plate"])
            else:
                self.db.execute("UPDATE payments SET card_cache_id=NULL WHERE id=?", (p["id"],))
            raise
        return resp

    def _abort_terminal(self, pid):
        if self.terminal_busy != pid:
            return

        def do():
            try:
                self.kkm.call("bills/abortPosAction", timeout=10)
            except KKMError:
                pass
        threading.Thread(target=do, daemon=True).start()

    def _payment_loop(self):
        mcfg = self.megapay.cfg
        while not self._stop.wait(mcfg.get("poll_seconds", 3)):
            try:
                self.poll_payments()
            except Exception as e:  # noqa: BLE001
                self.log("error", "Проверка оплат: %s" % e)

    def poll_payments(self):
        timeout = self.megapay.cfg.get("timeout_seconds", 300)
        now = datetime.now()
        rows = self.db.query(
            "SELECT * FROM payments WHERE status IN ('pending','expired','cancelled') AND created>=?",
            (now_str(now - timedelta(seconds=timeout * 2)),),
        )
        # старые pending (например, после перезапуска) тоже снимаем
        rows += self.db.query(
            "SELECT * FROM payments WHERE status='pending' AND created<?",
            (now_str(now - timedelta(seconds=timeout * 2)),),
        )
        for p in rows:
            age = (now - parse_dt(p["created"])).total_seconds()
            if p["status"] == "pending" and age > timeout:
                with self.lock:
                    self.db.execute("UPDATE payments SET status='expired', updated=? WHERE id=? AND status='pending'",
                                    (now_str(), p["id"]))
                    if self.current.get(p["camera"]) == p["id"]:
                        self.current.pop(p["camera"], None)
                self.log("payment", "Оплата #%d (%s) не поступила за %d мин — QR снят"
                         % (p["id"], p["plate"], timeout // 60), p["camera"], p["plate"])
            if p["megapay_id"] and self.megapay.enabled and age <= timeout * 2:
                try:
                    st = self.megapay.status(p["megapay_id"])
                except MegapayError as e:
                    self.db.execute("UPDATE payments SET qr_error=? WHERE id=?", ("статус: %s" % e, p["id"]))
                    continue
                if st == "success":
                    self.mark_paid(p["id"], "qr")

    def megapay_callback(self, data):
        """Уведомление от Megapay (если настроят callback): ищем транзакцию и статус.
        Работает только с секретом в адресе (megapay.callback_secret), иначе кто угодно «оплатит» сам себе."""
        import hmac
        from .megapay import ID_KEYS, STATUS_KEYS, classify_status, find_key
        secret = self.megapay.cfg.get("callback_secret") or ""
        if not secret or not hmac.compare_digest(str(data.get("secret", "")).encode(), secret.encode()):
            self.log("alert", "Megapay callback отклонён: нет/неверный секрет")
            return False
        mid = find_key(data, ID_KEYS)
        st = classify_status(find_key(data, STATUS_KEYS), self.megapay.cfg)
        self.log("megapay", "Callback: %s" % json.dumps(data, ensure_ascii=False)[:500])
        if mid is None or st != "success":
            return False
        p = self.db.one("SELECT id FROM payments WHERE megapay_id=?", (str(mid),))
        return bool(p) and self.mark_paid(p["id"], "qr")

    # ------------------------------------------------------------------ действия оператора
    def pay_cash(self, pid):
        return self.mark_paid(pid, "cash")

    def pay_card(self, pid):
        """Оплата картой на отдельном терминале — оператор подтверждает вручную."""
        return self.mark_paid(pid, "card")

    def pay_card_kkm(self, pid):
        """Оплата картой через терминал, подключённый к ККМ: ККМ сама проводит оплату и пробивает чек."""
        with self.lock:
            p = self.payment(pid)
            if not p or p["status"] == "success":
                raise ValueError("оплата не найдена или уже проведена")
            if self.terminal_busy is not None:
                raise ValueError("терминал уже ждёт оплату картой")
            self.terminal_busy = pid
        try:
            resp = self._card_via_kkm(p)
        finally:
            with self.lock:
                if self.terminal_busy == pid:
                    self.terminal_busy = None
        return self.mark_paid(pid, "card_kkm", resp)

    def cancel_payment(self, pid):
        with self.lock:
            p = self.payment(pid)
            if not p or p["status"] == "success":
                return False
            self._cancel_locked(p)
        self.log("payment", "Оплата #%d отменена оператором" % pid, p["camera"], p["plate"])
        return True

    def release_free(self, pid):
        """Выпустить без оплаты."""
        p = self.payment(pid)
        if not p:
            return False
        self.cancel_payment(pid)
        with self.lock:
            s = self.session(p["session_id"])
            if s and s["status"] == "open":
                self._close_session(s["id"], "operator")
        self.log("exit", "%s выпущен оператором без оплаты (%d сом)" % (p["plate"], p["amount"]),
                 p["camera"], p["plate"])
        if p["camera"] in self.cameras:
            self.open_barrier(self.cameras[p["camera"]], p["plate"], "оператор, без оплаты")
        return True

    def _cancel_locked(self, p):
        self.db.execute("UPDATE payments SET status='cancelled', updated=? WHERE id=? AND status='pending'",
                        (now_str(), p["id"]))
        if self.current.get(p["camera"]) == p["id"]:
            self.current.pop(p["camera"], None)
        self._abort_terminal(p["id"])

    def bill(self, camera, plate, amount=None):
        """Оператор выставляет оплату по номеру (исправленному вручную или без записи о въезде).
        Текущий QR у этого шлагбаума снимается, создаётся новый — на экран и терминал."""
        plate = plates.normalize(plate)
        if not plates.is_valid(plate):
            raise ValueError("неверный номер")
        if camera not in self.cameras:
            raise ValueError("нет такой камеры")
        amount = int(amount) if amount not in (None, "", 0, "0") else None
        if amount is not None and amount < 0:
            raise ValueError("неверная сумма")
        now = datetime.now()
        with self.lock:
            old = self.current.get(camera)
            if old:
                self._cancel_locked(self.payment(old))
            k = plates.key(plate)
            s = self._find_open_session(k)
            if s:
                dur = (now - parse_dt(s["paid_until"] or s["entry_time"])).total_seconds()
                fee = amount if amount is not None else calc_fee(dur, self.tariff)
            else:
                if amount is None:
                    raise ValueError("Нет записи о въезде %s — укажите сумму" % plate)
                sid = self.db.execute(
                    "INSERT INTO sessions(plate, plate_key, entry_time, note) VALUES (?,?,?,?)",
                    (plate, k, now_str(now), "без записи о въезде"))
                s, dur, fee = self.session(sid), 0, amount
            self.unknown.pop(camera, None)
            for old_p in self.db.query("SELECT * FROM payments WHERE session_id=? AND status='pending'",
                                       (s["id"],)):
                self._cancel_locked(old_p)
            if fee == 0:
                self._close_session(s["id"], "free", now)
                pid = None
            else:
                pid = self._create_payment(s["id"], camera, s["plate"], fee, dur, now)
        if pid is None:
            self.log("exit", "Оператор: %s — бесплатно, открываю" % plate, camera, plate)
            self.open_barrier(self.cameras[camera], plate, "выезд без оплаты")
            return {"free": True}
        self.log("payment", "Оператор выставил оплату %s: %d сом" % (s["plate"], fee), camera, s["plate"])
        self._generate_qr_async(pid)
        self._start_terminal(pid)
        return {"id": pid, "amount": fee, "plate": s["plate"]}

    def snap(self, camera):
        ok, body = self.cameras[camera].snap()
        if not ok:
            raise ValueError("камера не ответила: %s" % body)
        return True

    def set_session_plate(self, sid, plate):
        plate = plates.normalize(plate)
        if not plates.is_valid(plate):
            raise ValueError("неверный номер")
        s = self.session(sid)
        if not s:
            raise ValueError("заезд не найден")
        self.db.execute("UPDATE sessions SET plate=?, plate_key=? WHERE id=?", (plate, plates.key(plate), sid))
        self.log("enter", "Номер заезда исправлен: %s → %s" % (s["plate"], plate), None, plate)
        return True

    def manual_open(self, camera, plate="", release_unknown=False):
        cam = self.cameras[camera]
        if release_unknown:
            u = self.unknown.pop(camera, None)
            if u:
                plate = u["plate"]
        self.log("barrier", "Ручное открытие %s%s" % (camera, (" для " + plate) if plate else ""), camera, plate)
        self.open_barrier(cam, plate, "вручную")

    def add_session(self, plate, entry_time=None):
        plate = plates.normalize(plate)
        if not plates.is_valid(plate):
            raise ValueError("неверный номер")
        et = parse_dt(entry_time) if entry_time else datetime.now()
        sid = self.db.execute(
            "INSERT INTO sessions(plate, plate_key, entry_time, note) VALUES (?,?,?,?)",
            (plate, plates.key(plate), now_str(et), "добавлен вручную"),
        )
        self.log("enter", "Заезд %s (с %s) добавлен вручную" % (plate, now_str(et)), None, plate)
        return sid

    def close_session_manual(self, sid):
        with self.lock:
            s = self.session(sid)
            if not s or s["status"] != "open":
                return False
            self._close_session(sid, "manual")
            for p in self.db.query("SELECT * FROM payments WHERE session_id=? AND status='pending'", (sid,)):
                self._cancel_locked(p)
        self.log("exit", "Заезд %s закрыт оператором" % s["plate"], None, s["plate"])
        return True

    # ------------------------------------------------------------------ чеки ККМ
    def retry_receipt(self, pid):
        self.db.execute(
            "UPDATE payments SET receipt_status='pending', receipt_attempts=0, receipt_next_try=NULL "
            "WHERE id=? AND status='success' AND method IN ('qr','cash','card') AND receipt_status!='done'", (pid,))
        self.receipt_wakeup.set()

    def _receipt_loop(self):
        while not self._stop.is_set():
            self.receipt_wakeup.wait(10)
            self.receipt_wakeup.clear()
            if self._stop.is_set():
                return
            try:
                self.process_receipts()
            except Exception as e:  # noqa: BLE001
                self.log("error", "Чеки: %s" % e)

    def process_receipts(self):
        if not self.kkm.enabled:
            return
        rows = self.db.query(
            "SELECT * FROM payments WHERE receipt_status='pending' AND "
            "(receipt_next_try IS NULL OR receipt_next_try<=?) ORDER BY id", (now_str(),))
        for p in rows:
            pay_type, title = RECEIPT_PAY.get(p["method"], RECEIPT_PAY["card"])
            cache_id = "akjol-pay-%d-%d" % (p["id"], p["receipt_cache_n"])
            try:
                resp = self.kkm.receipt(p, pay_type, True, title, cache_id)
                note = None
                if resp.get("status") in PRINTER_STATUSES:
                    note = "чек зарегистрирован, но не напечатан: %s" % resp.get("errorMessage")
                self.db.execute(
                    "UPDATE payments SET receipt_status='done', receipt_fd=?, receipt_qr=?, receipt_error=? "
                    "WHERE id=?", (resp.get("fdNumber"), resp.get("qrCode"), note, p["id"]))
                self.log("receipt", "Чек #%d: %d сом, ФД %s" % (p["id"], p["amount"], resp.get("fdNumber")),
                         None, p["plate"])
            except KKMError as e:
                attempts = p["receipt_attempts"] + 1
                limit = self.kkm.cfg.get("max_attempts", 0)  # 0 — пробовать, пока не появится связь
                failed = bool(limit) and attempts >= limit
                nxt = datetime.now() + timedelta(seconds=min(600, 15 * 2 ** min(attempts, 6)))
                # ККМ ответила ошибкой — она могла её закэшировать, следующая попытка с новым cacheId;
                # нет связи — тот же cacheId, чтобы не пробить чек дважды
                cache_n = p["receipt_cache_n"] + (1 if e.status is not None else 0)
                self.db.execute(
                    "UPDATE payments SET receipt_status=?, receipt_attempts=?, receipt_error=?, "
                    "receipt_next_try=?, receipt_cache_n=? WHERE id=?",
                    ("failed" if failed else "pending", attempts, str(e), now_str(nxt), cache_n, p["id"]))
                if attempts == 1 or failed:
                    self.log("error", "Чек #%d не пробит: %s" % (p["id"], e), None, p["plate"])
                if e.status is None:
                    return  # ККМ недоступна — остальные попробуем позже

    def _close_day_loop(self):
        last_try = 0
        while not self._stop.wait(30):
            t = self.kkm.cfg.get("close_day_time")
            if not self.kkm.enabled or not t:
                continue
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")
            if now.strftime("%H:%M") < t or self.db.get_meta("last_close_day") == today:
                continue
            if time.time() - last_try < 300:
                continue
            last_try = time.time()
            try:
                self.close_day()
                self.db.set_meta("last_close_day", today)
            except KKMError as e:
                self.log("error", "Автозакрытие смены: %s" % e)

    def close_day(self):
        st = self.kkm.day_state()
        if st.get("dayState") != 1:
            self.log("kkm", "Закрытие смены: смена уже закрыта")
            return st
        resp = self.kkm.close_day()
        self.log("kkm", "Смена №%s закрыта (Z-отчёт)" % st.get("shiftNumber"))
        return resp

    # ------------------------------------------------------------------ для экрана и панели
    def screen_state(self):
        now = time.time()
        if self.paid_flash and self.paid_flash["until"] > now:
            p = self.payment(self.paid_flash["pid"]) or {}
            return {"state": "paid", "amount": self.paid_flash["amount"], "plate": self.paid_flash["plate"],
                    "receipt_qr": p.get("receipt_qr")}
        pid = self.current.get(self.screen_camera)
        p = self.payment(pid) if pid else None
        if p and p["status"] == "pending":
            timeout = self.megapay.cfg.get("timeout_seconds", 300)
            left = timeout - (datetime.now() - parse_dt(p["created"])).total_seconds()
            return {
                "state": "qr", "id": p["id"], "amount": p["amount"], "plate": p["plate"],
                "duration": format_duration(p["duration_seconds"]),
                "qr_image": p["qr_image"], "qr_text": p["qr_text"],
                "qr_error": p["qr_error"] if not (p["qr_image"] or p["qr_text"]) else None,
                "megapay": self.megapay.enabled, "seconds_left": max(0, int(left)),
            }
        if self.screen_msg and self.screen_msg[1] > now:
            return {"state": "message", "text": self.screen_msg[0]}
        return {"state": "stop"}

    def state(self):
        cur = []
        for cam, pid in list(self.current.items()):
            p = self.payment(pid)
            if p:
                p = {k: v for k, v in p.items() if k != "qr_image"}
                p["duration"] = format_duration(p["duration_seconds"])
                cur.append(p)
        today = datetime.now().strftime("%Y-%m-%d")
        return {
            "now": now_str(),
            "cameras": [c.status() for c in self.cameras.values()],
            "current": cur,
            "unknown": [dict(v, camera=k) for k, v in self.unknown.items()],
            "inside": self.db.one("SELECT COUNT(*) AS n FROM sessions WHERE status='open'")["n"],
            "today": self.report(today),
            "receipt_problems": self.db.one(
                "SELECT COUNT(*) AS n FROM payments WHERE receipt_status IN ('failed') "
                "OR (receipt_status='pending' AND receipt_attempts>0)")["n"],
            "megapay_status_check": bool(self.megapay.cfg.get("status_path")),
            "card_via_kkm": bool(self.kkm.enabled and self.kkm.cfg.get("card_via_kkm")),
            "terminal_auto": bool(self.kkm.enabled and self.kkm.cfg.get("terminal_auto")),
            "alerts": list(self.alerts),
            "tg_outbox": self.db.one("SELECT COUNT(*) AS n FROM tg_outbox")["n"],
        }

    def report(self, day):
        rows = self.db.query(
            "SELECT method, COUNT(*) AS n, SUM(amount) AS total FROM payments "
            "WHERE status='success' AND substr(paid_time,1,10)=? GROUP BY method", (day,))
        entries = self.db.one("SELECT COUNT(*) AS n FROM sessions WHERE substr(entry_time,1,10)=?", (day,))["n"]
        exits = self.db.one("SELECT COUNT(*) AS n FROM sessions WHERE substr(exit_time,1,10)=?", (day,))["n"]
        return {
            "day": day, "entries": entries, "exits": exits,
            "by_method": {r["method"]: {"count": r["n"], "total": r["total"]} for r in rows},
            "total": sum(r["total"] or 0 for r in rows),
        }

    def open_sessions(self):
        now = datetime.now()
        out = []
        for s in self.db.query("SELECT * FROM sessions WHERE status='open' ORDER BY entry_time DESC"):
            dur = (now - parse_dt(s["paid_until"] or s["entry_time"])).total_seconds()
            s["duration"] = format_duration((now - parse_dt(s["entry_time"])).total_seconds())
            s["fee_now"] = calc_fee(dur, self.tariff)
            out.append(s)
        return out
