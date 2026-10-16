"""Камеры Dahua ITC по HTTP (порт 80): поток событий распознавания и открытие шлагбаума."""
import json
import re
import socket
import threading
import time
import urllib.parse
import urllib.request

from . import plates

DEFAULT_API = {
    "events_path": "/cgi-bin/eventManager.cgi?action=attach&codes=[TrafficJunction]&heartbeat=5",
    "open_path": "/cgi-bin/trafficSnap.cgi?action=openStrobe&channel={channel}"
                 "&info.openType=Normal&info.plateNumber={plate}",
    "snap_path": "/cgi-bin/trafficSnap.cgi?action=manSnap&channel={channel}",
    "event_codes": ["TrafficJunction", "TrafficManualSnap"],
    "read_timeout": 30,
}


def extract_plate(data):
    if not isinstance(data, dict):
        return ""
    for path in (("TrafficCar", "PlateNumber"), ("Object", "Text"), ("PlateNumber",)):
        cur = data
        for k in path:
            cur = cur.get(k) if isinstance(cur, dict) else None
        if isinstance(cur, str) and cur.strip():
            return cur.strip()
    return ""


def parse_event_text(text):
    """Разбирает часть потока eventManager: 'Code=X;action=Pulse;index=0;data={...}'."""
    m = re.search(r"Code=([^;\s]+);action=([^;\s]+)", text)
    if not m:
        return None
    code, action = m.group(1), m.group(2)
    data = {}
    i = text.find("data=")
    if i >= 0:
        raw = text[i + 5:].strip()
        end = raw.rfind("}")
        if end >= 0:
            try:
                data = json.loads(raw[: end + 1])
            except ValueError:
                data = {}
    return {"code": code, "action": action, "data": data}


class Camera:
    def __init__(self, cfg, api_cfg, on_plate, log):
        self.cfg = cfg
        self.api = dict(DEFAULT_API)
        self.api.update(api_cfg or {})
        self.name = cfg["name"]
        self.role = cfg["role"]
        self.channel = cfg.get("channel", 1)
        self.base = "http://%s:%s" % (cfg["host"], cfg.get("port", 80))
        pm = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        pm.add_password(None, self.base, cfg.get("username", "admin"), cfg.get("password", ""))
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPDigestAuthHandler(pm), urllib.request.HTTPBasicAuthHandler(pm)
        )
        self.on_plate = on_plate
        self.log = log
        self.connected = False
        self.last_plate = None
        self.last_plate_time = None
        self.last_error = None
        self._stop = threading.Event()

    def status(self):
        return {
            "name": self.name,
            "role": self.role,
            "host": self.cfg["host"],
            "enabled": self.cfg.get("enabled", True),
            "connected": self.connected,
            "last_plate": self.last_plate,
            "last_plate_time": self.last_plate_time,
            "last_error": self.last_error,
        }

    # --- шлагбаум ---
    def open_barrier(self, plate=""):
        if not self.cfg.get("enabled", True):
            return True, "камера выключена в настройках — тест, шлагбаум не подключён"
        path = self.api["open_path"].format(
            channel=self.channel, plate=urllib.parse.quote(plate or "")
        )
        try:
            r = self.opener.open(self.base + path, timeout=5)
            body = r.read(2000).decode("utf-8", "replace").strip()
            ok = r.status == 200 and "error" not in body.lower()
            return ok, body
        except Exception as e:  # noqa: BLE001
            return False, str(e)

    def snap(self):
        """Ручной снимок: камера распознаёт номер заново и присылает событие TrafficManualSnap."""
        if not self.cfg.get("enabled", True):
            return False, "камера выключена в настройках (тестовый режим)"
        try:
            r = self.opener.open(self.base + self.api["snap_path"].format(channel=self.channel), timeout=5)
            body = r.read(2000).decode("utf-8", "replace").strip()
            return r.status == 200 and "error" not in body.lower(), body
        except Exception as e:  # noqa: BLE001
            return False, str(e)

    # --- поток событий ---
    def start(self):
        if not self.cfg.get("enabled", True):
            return
        threading.Thread(target=self._run, name="cam-" + self.name, daemon=True).start()

    def stop(self):
        self._stop.set()

    def _run(self):
        delay = 2
        while not self._stop.is_set():
            try:
                self._listen()
                delay = 2
            except socket.timeout as e:
                if self.connected:
                    # поток был, камера просто долго молчит (нет машин) — тихо переподключаемся
                    continue
                if self.last_error != "таймаут":
                    self.log("camera", "Камера %s: нет связи (%s)" % (self.name, e or "таймаут"), self.name)
                self.last_error = "таймаут"
            except Exception as e:  # noqa: BLE001
                if self.connected or self.last_error != str(e):
                    self.log("camera", "Камера %s: нет связи (%s)" % (self.name, e), self.name)
                self.last_error = str(e)
            self.connected = False
            self._stop.wait(delay)
            delay = min(delay * 2, 30)

    def _listen(self):
        r = self.opener.open(self.base + self.api["events_path"], timeout=self.api["read_timeout"])
        if not self.connected:
            self.log("camera", "Камера %s: подключено" % self.name, self.name)
        self.connected = True
        self.last_error = None
        pending = None  # накопление события без multipart-разметки
        while not self._stop.is_set():
            line = r.readline()
            if not line:
                raise ConnectionError("поток закрыт камерой")
            s = line.decode("utf-8", "replace")
            st = s.strip()
            if st.startswith("--"):
                pending = None
                headers = {}
                while True:
                    h = r.readline()
                    if not h:
                        raise ConnectionError("поток закрыт камерой")
                    hs = h.decode("utf-8", "replace").strip()
                    if not hs:
                        break
                    if ":" in hs:
                        k, v = hs.split(":", 1)
                        headers[k.strip().lower()] = v.strip()
                n = int(headers.get("content-length", "0") or 0)
                if n > 0:
                    body = _read_exact(r, n)
                    if "image" not in headers.get("content-type", ""):
                        self._handle_text(body.decode("utf-8", "replace"))
                continue
            if pending is None and st.startswith("Code="):
                pending = s
            elif pending is not None:
                pending += s
            if pending is not None:
                if "data=" not in pending or pending.count("{") <= pending.count("}"):
                    self._handle_text(pending)
                    pending = None

    def _handle_text(self, text):
        ev = parse_event_text(text)
        if not ev or ev["code"] not in self.api["event_codes"]:
            return
        if ev["action"] not in ("Pulse", "Start"):
            return
        plate = extract_plate(ev["data"])
        if not plates.is_valid(plate):
            return  # «нет номера» — не подставляем оператору
        try:
            shown = self.on_plate(self, plate, manual=ev["code"] == "TrafficManualSnap")
            if shown is not False:  # False — номер из второго списка, нигде не показываем
                self.last_plate = plates.normalize(plate)
                self.last_plate_time = time.strftime("%Y-%m-%d %H:%M:%S")
        except Exception as e:  # noqa: BLE001
            self.log("error", "Ошибка обработки номера %s: %s" % (plate, e), self.name, plate)


def _read_exact(r, n):
    chunks = []
    while n > 0:
        b = r.read(n)
        if not b:
            raise ConnectionError("поток закрыт камерой")
        chunks.append(b)
        n -= len(b)
    return b"".join(chunks)
