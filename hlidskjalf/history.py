"""History from sysstat data files via `sadf -j`.

Past days are parsed once (cached by file mtime); today's file is read
incrementally with `sadf -s <last time>`. Range responses are precomputed
by a background worker so requests never wait on sadf.
"""

import datetime as dt
import gzip
import json
import os
import re
import threading
import time

from . import config as C
from .util import log, run

SKIP_NET = re.compile(r"^(lo|veth|vnet|tap|cali|flannel|cni)")
PHYS_DISK = re.compile(r"^(nvme\d+n\d+|sd[a-z]+|vd[a-z]+|xvd[a-z]+|hd[a-z]+|mmcblk\d+)$")
# No -p: whole-disk names are stored in the file already, and -p needs
# /dev/mapper (absent in the container) only to prettify dm-N.
SADF_ARGS = ["-u", "-r", "-q", "-n", "DEV", "-d", "-m", "TEMP,FAN"]

RANGES = {"1h": (3600, 60), "6h": (6 * 3600, 60), "24h": (86400, 120), "7d": (7 * 86400, 600)}


class Day:
    def __init__(self, path, date):
        self.path, self.date = path, date
        self.sig = None
        self.rows = []   # list of compact dicts
        self.last_t = 0

    def _parse(self, stats):
        rows = []
        for r in stats:
            ts = r.get("timestamp") or {}
            try:
                t = time.mktime(time.strptime(f"{ts['date']} {ts['time']}", "%Y-%m-%d %H:%M:%S"))
            except (KeyError, ValueError):
                continue
            if t <= self.last_t:
                continue
            row = {"t": t}
            cl = r.get("cpu-load") or r.get("cpu-load-all") or []
            for c in cl:
                if c.get("cpu") == "all":
                    row["cu"] = c.get("user", 0) + c.get("nice", 0)
                    row["cs"] = c.get("system", 0) + c.get("irq", 0) + c.get("soft", 0)
                    row["cw"] = c.get("iowait", 0)
                    row["c"] = 100 - c.get("idle", 0) - c.get("iowait", 0)
            m = r.get("memory") or {}
            if "avail" in m:
                row["ma"] = m["avail"] * 1024
                row["mu"] = m.get("memused", 0) * 1024
            q = r.get("queue") or {}
            if "ldavg-1" in q:
                row["l"] = [q.get("ldavg-1"), q.get("ldavg-5"), q.get("ldavg-15")]
            net = {}
            for n in (r.get("network") or {}).get("net-dev") or []:
                name = n.get("iface", "")
                if SKIP_NET.match(name):
                    continue
                net[name] = (n.get("rxkB", 0) * 1024, n.get("txkB", 0) * 1024)
            row["n"] = net
            rd = wr = 0.0
            util = {}
            for d in r.get("disk") or []:
                name = d.get("disk-device", "")
                if PHYS_DISK.match(name):
                    rd += d.get("rkB", 0) * 1024
                    wr += d.get("wkB", 0) * 1024
                    util[name] = d.get("util-percent", 0)
            row["d"] = (rd, wr)
            row["du"] = util
            temps = {}
            counter = {}
            pm = r.get("power-management") or {}
            for tm in pm.get("temperature") or []:
                dev = tm.get("device", "?")
                k = counter.get(dev, 0)
                counter[dev] = k + 1
                temps[f"{dev}#{k}"] = tm.get("degC")
            row["tp"] = temps
            fans = {}
            counter = {}
            for f in pm.get("fan-speed") or []:
                dev = f.get("device", "?")
                k = counter.get(dev, 0)
                counter[dev] = k + 1
                fans[f"{dev}#{k}"] = f.get("rpm")
            row["fn"] = fans
            rows.append(row)
        return rows

    def refresh(self, today):
        try:
            st = os.stat(self.path)
        except OSError:
            self.rows, self.sig = [], None
            return
        sig = (st.st_mtime, st.st_size)
        if sig == self.sig:
            return
        cmd = ["sadf", "-j", "-T"]
        incremental = today and self.rows and self.sig is not None and sig[1] >= self.sig[1]
        if incremental:
            # sadf uses the first record at/after -s only as the baseline for interval
            # stats and does not print it, so start at the last record we already have.
            # (Starting at last_t + 1 silently dropped every other new sample.)
            cmd += ["-s", time.strftime("%H:%M:%S", time.localtime(self.last_t))]
        else:
            self.rows, self.last_t = [], 0
        cmd += [self.path, "--"] + SADF_ARGS
        rc, out = run(cmd, timeout=120)
        if rc != 0 or not out.strip():
            log.warning("sadf %s rc=%s", self.path, rc)
            return
        try:
            host = json.loads(out)["sysstat"]["hosts"][0]
        except (ValueError, KeyError, IndexError):
            log.warning("sadf %s: bad json", self.path)
            return
        if host.get("file-date") != self.date.isoformat():
            # stale file from a previous month (saDD reused)
            self.rows, self.sig = [], sig
            return
        new = self._parse(host.get("statistics") or [])
        self.rows.extend(new)
        if self.rows:
            self.last_t = self.rows[-1]["t"]
        self.sig = sig


class History:
    def __init__(self, sampler):
        self.sampler = sampler
        self.days = {}
        self.cache = {}   # range -> gzip bytes
        self.lock = threading.Lock()
        self.ready = False
        self.error = None

    def _day(self, date):
        path = os.path.join(C.SA_DIR, f"sa{date.day:02d}")
        d = self.days.get(date)
        if d is None:
            d = self.days[date] = Day(path, date)
        return d

    def refresh(self):
        if not os.path.isdir(C.SA_DIR):
            self.error = f"no sysstat data dir {C.SA_DIR}"
            return
        today = dt.date.today()
        keep = set()
        for i in range(8):
            date = today - dt.timedelta(days=i)
            keep.add(date)
            self._day(date).refresh(today=(i == 0))
        for k in list(self.days):
            if k not in keep:
                del self.days[k]
        built = {}
        for name in RANGES:
            data = self.build(name)
            built[name] = gzip.compress(json.dumps(data, separators=(",", ":")).encode(), 5)
        with self.lock:
            self.cache = built
        self.ready = True
        self.error = None

    def build(self, name):
        span, step = RANGES[name]
        now = time.time()
        start = (int(now - span) // step) * step
        nb = int((now - start) // step) + 1
        rows = []
        for date in sorted(self.days):
            for r in self.days[date].rows:
                if r["t"] >= start:
                    rows.append(r)
        latest = self.sampler.latest or {}
        mem_total = (latest.get("mem") or {}).get("total") or 0
        sensor_map = {s["lm"]: s for s in self.sampler.sensors}

        buckets = [[] for _ in range(nb)]
        for r in rows:
            i = int((r["t"] - start) // step)
            if 0 <= i < nb:
                buckets[i].append(r)

        def avg(vals):
            vals = [v for v in vals if v is not None]
            return round(sum(vals) / len(vals), 2) if vals else None

        def mx(vals):
            vals = [v for v in vals if v is not None]
            return max(vals) if vals else None

        out = {"range": name, "step": step, "t": [start + i * step for i in range(nb)],
               "cpu": [], "cpu_user": [], "cpu_sys": [], "cpu_iow": [], "mem_pct": [], "mem_used": [],
               "load1": [], "load5": [], "load15": [], "disk_r": [], "disk_w": [],
               "net": {}, "temps": {}, "fans": {}, "points": len(rows)}
        nets = sorted({k for r in rows for k in r["n"]})
        temps = sorted(k for k in {k for r in rows for k in r["tp"]}
                       if k not in sensor_map or sensor_map[k]["primary"])
        fans = sorted({k for r in rows for k in r["fn"]})
        for k in nets:
            out["net"][k] = {"rx": [], "tx": []}
        tmap = {}
        for k in temps:
            s = sensor_map.get(k)
            sid = s["id"] if s else re.sub(r"[^a-z0-9]+", "_", k.lower())
            tmap[k] = sid
            out["temps"][sid] = []
        for k in fans:
            out["fans"][k] = []
        for b in buckets:
            out["cpu"].append(avg([r.get("c") for r in b]))
            out["cpu_user"].append(avg([r.get("cu") for r in b]))
            out["cpu_sys"].append(avg([r.get("cs") for r in b]))
            out["cpu_iow"].append(avg([r.get("cw") for r in b]))
            ma = avg([r.get("ma") for r in b])
            if ma is not None and mem_total:
                out["mem_used"].append(int(mem_total - ma))
                out["mem_pct"].append(round(100 * (mem_total - ma) / mem_total, 1))
            else:
                out["mem_used"].append(None)
                out["mem_pct"].append(None)
            for j, key in enumerate(("load1", "load5", "load15")):
                out[key].append(avg([r["l"][j] for r in b if "l" in r]))
            out["disk_r"].append(avg([r["d"][0] for r in b if "d" in r]))
            out["disk_w"].append(avg([r["d"][1] for r in b if "d" in r]))
            for k in nets:
                out["net"][k]["rx"].append(avg([r["n"][k][0] for r in b if k in r["n"]]))
                out["net"][k]["tx"].append(avg([r["n"][k][1] for r in b if k in r["n"]]))
            for k in temps:
                out["temps"][tmap[k]].append(avg([r["tp"].get(k) for r in b]))
            for k in fans:
                out["fans"][k].append(avg([r["fn"].get(k) for r in b]))
        # peak values for the summary row
        out["peak"] = {"cpu": mx(out["cpu"]), "mem_pct": mx(out["mem_pct"]), "load1": mx(out["load1"])}
        return out

    def get(self, name):
        with self.lock:
            return self.cache.get(name)
