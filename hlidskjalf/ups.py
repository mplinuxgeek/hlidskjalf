"""NUT (upsd) read-only client: plain-text protocol on TCP 3493."""

import collections
import socket
import time

from . import config as C

STATUS_WORDS = {
    "OL": "On line", "OB": "On battery", "LB": "Low battery", "HB": "High battery",
    "RB": "Replace battery", "CHRG": "Charging", "DISCHRG": "Discharging",
    "BYPASS": "Bypass", "CAL": "Calibrating", "OFF": "Offline", "OVER": "Overloaded",
    "TRIM": "Trimming voltage", "BOOST": "Boosting voltage", "FSD": "Forced shutdown",
}


class _Nut:
    def __init__(self, host, port, timeout=3):
        self.s = socket.create_connection((host, port), timeout=timeout)
        self.f = self.s.makefile("rb")

    def cmd(self, line):
        self.s.sendall(line.encode() + b"\n")

    def readline(self):
        return self.f.readline().decode("utf-8", "replace").rstrip("\r\n")

    def list(self, what):
        self.cmd(f"LIST {what}")
        lines = []
        while True:
            line = self.readline()
            if not line:
                raise RuntimeError("connection closed")
            if line.startswith("ERR"):
                raise RuntimeError(line)
            if line.startswith("BEGIN"):
                continue
            if line.startswith("END"):
                return lines
            lines.append(line)

    def close(self):
        try:
            self.cmd("LOGOUT")
        except OSError:
            pass
        self.s.close()


def _val(line):
    # VAR ups battery.charge "100"
    p = line.split(" ", 3)
    if len(p) < 4:
        return None, None
    return p[2], p[3].strip().strip('"')


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class UPS:
    def __init__(self):
        self.hist = collections.deque(maxlen=360)  # 1 h at 10 s

    def collect(self):
        if not C.NUT_HOST or C.NUT_HOST == "none":
            return {"available": False, "error": "disabled"}
        try:
            n = _Nut(C.NUT_HOST, C.NUT_PORT)
        except OSError as e:
            return {"available": False, "error": f"{C.NUT_HOST}:{C.NUT_PORT}: {e}"}
        try:
            name = C.NUT_UPS
            if not name or name == "auto":
                ups = n.list("UPS")
                if not ups:
                    return {"available": False, "error": "no UPS on server"}
                name = ups[0].split()[1]
            v = {}
            for line in n.list(f"VAR {name}"):
                k, val = _val(line)
                if k:
                    v[k] = val
        except (OSError, RuntimeError) as e:
            return {"available": False, "error": str(e)}
        finally:
            n.close()
        status = v.get("ups.status", "")
        words = status.split()
        load = _num(v.get("ups.load"))
        nominal = _num(v.get("ups.realpower.nominal")) or _num(v.get("ups.power.nominal"))
        watts = _num(v.get("ups.realpower"))
        if watts is None and load is not None and nominal:
            watts = round(load / 100 * nominal)
        out = {
            "available": True, "name": name, "server": f"{C.NUT_HOST}:{C.NUT_PORT}",
            "model": " ".join(x for x in (v.get("device.mfr") or v.get("ups.mfr"), v.get("device.model") or v.get("ups.model")) if x),
            "status": status,
            "status_text": ", ".join(STATUS_WORDS.get(w, w) for w in words),
            "on_battery": "OB" in words, "low_battery": "LB" in words,
            "charge": _num(v.get("battery.charge")),
            "runtime": _num(v.get("battery.runtime")),
            "load": load, "watts": watts, "nominal_watts": nominal,
            "input_voltage": _num(v.get("input.voltage")),
            "output_voltage": _num(v.get("output.voltage")),
            "battery_voltage": _num(v.get("battery.voltage")),
            "input_frequency": _num(v.get("input.frequency")),
            "test_result": v.get("ups.test.result", ""),
            "battery_date": next((d for d in (v.get("battery.date"), v.get("battery.mfr.date"))
                                  if d and any(ch.isdigit() for ch in d)), ""),
            "ts": time.time(),
        }
        self.hist.append([round(time.time()), out["charge"], load])
        out["hist"] = list(self.hist)
        return out
