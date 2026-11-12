"""In-process live sampler: /proc + /sys every SAMPLE_INTERVAL seconds into a
ring buffer. Cheap reads only - no subprocesses here."""

import collections
import re
import threading
import time

from . import config as C
from .gpu import GPU, GpuHistory, Rapl
from .sysinfo import discover_sensors
from .util import log, read, read1

SKIP_NET = re.compile(r"^(lo|veth|vnet|tap|cali|flannel|cni)")
PHYS_DISK = re.compile(r"^(nvme\d+n\d+|sd[a-z]+|vd[a-z]+|xvd[a-z]+|hd[a-z]+|mmcblk\d+)$")
SKIP_DISK = re.compile(r"^(loop|ram|zram|sr|fd)")


def _r(x, n=1):
    return None if x is None else round(x, n)


class Sampler(threading.Thread):
    def __init__(self, topo):
        super().__init__(name="sampler", daemon=True)
        self.topo = topo
        self.buf = collections.deque(maxlen=C.LIVE_POINTS)
        self.seq = 0
        self.lock = threading.Lock()
        self.latest = None
        self.prev = None
        self.sensors, self.fans = discover_sensors()
        self.sensor_scan = time.monotonic()
        # container id -> cgroup dir (absolute, host sys prefixed); set by
        # the containers collector.
        self.ct_targets = {}
        self.gpu = GPU() if C.ENABLE_GPU else None
        self.rapl = Rapl()
        self.gpu_hist = GpuHistory()

    # ---- raw readers -------------------------------------------------
    def _stat(self):
        cpus = {}
        ctxt = 0
        for line in (read(C.hp("/proc/stat")) or "").splitlines():
            if line.startswith("cpu"):
                p = line.split()
                vals = [int(x) for x in p[1:9]]
                cpus[p[0]] = vals
            elif line.startswith("ctxt"):
                ctxt = int(line.split()[1])
        return cpus, ctxt

    def _meminfo(self):
        m = {}
        for line in (read(C.hp("/proc/meminfo")) or "").splitlines():
            k, _, v = line.partition(":")
            v = v.split()
            if v:
                m[k] = int(v[0]) * 1024
        return m

    def _net(self):
        out = {}
        for line in (read(C.hp("/proc/net/dev")) or "").splitlines()[2:]:
            name, _, rest = line.partition(":")
            name = name.strip()
            if SKIP_NET.match(name):
                continue
            p = rest.split()
            out[name] = (int(p[0]), int(p[8]))
        return out

    def _disks(self):
        out = {}
        for line in (read(C.hp("/proc/diskstats")) or "").splitlines():
            p = line.split()
            if len(p) < 14:
                continue
            name = p[2]
            if SKIP_DISK.match(name):
                continue
            if not (PHYS_DISK.match(name) or name.startswith(("md", "dm-"))):
                continue
            out[name] = (int(p[5]), int(p[9]), int(p[12]), int(p[3]) + int(p[7]))
        return out

    def _cgroups(self):
        out = {}
        for cid, cg in list(self.ct_targets.items()):
            usec = None
            for line in (read(f"{cg}/cpu.stat") or "").splitlines():
                if line.startswith("usage_usec"):
                    usec = int(line.split()[1])
                    break
            if usec is None:
                continue
            mem = read1(f"{cg}/memory.current")
            inactive = 0
            for line in (read(f"{cg}/memory.stat") or "").splitlines():
                if line.startswith("inactive_file "):
                    inactive = int(line.split()[1])
                    break
            try:
                mem = max(0, int(mem) - inactive)
            except (TypeError, ValueError):
                mem = None
            out[cid] = (usec, mem)
        return out

    def _read_all(self):
        cpus, ctxt = self._stat()
        return {
            "mono": time.monotonic(),
            "cpus": cpus, "ctxt": ctxt,
            "net": self._net(), "disk": self._disks(),
            "cg": self._cgroups(),
        }

    # ---- sample ------------------------------------------------------
    def sample(self):
        now = time.time()
        cur = self._read_all()
        prev, self.prev = self.prev, cur
        if prev is None:
            return None
        dt = cur["mono"] - prev["mono"]
        if dt <= 0:
            return None

        def busy(a, b):
            # user nice system idle iowait irq softirq steal
            da = [y - x for x, y in zip(a, b)]
            tot = sum(da) or 1
            idle = da[3] + da[4]
            return da, tot, idle

        s = {"t": round(now, 2)}
        if "cpu" in cur["cpus"] and "cpu" in prev["cpus"]:
            da, tot, idle = busy(prev["cpus"]["cpu"], cur["cpus"]["cpu"])
            s["cpu"] = _r(100 * (tot - idle) / tot)
            s["cpu_user"] = _r(100 * (da[0] + da[1]) / tot)
            s["cpu_sys"] = _r(100 * (da[2] + da[5] + da[6]) / tot)
            s["cpu_iow"] = _r(100 * da[4] / tot)
            s["cpu_steal"] = _r(100 * da[7] / tot)
        per = []
        for c in self.topo["cpus"]:
            k = f"cpu{c['cpu']}"
            if k in cur["cpus"] and k in prev["cpus"]:
                _, tot, idle = busy(prev["cpus"][k], cur["cpus"][k])
                per.append(_r(100 * (tot - idle) / tot, 0))
            else:
                per.append(None)
        s["cpus"] = per
        s["ctxt"] = int((cur["ctxt"] - prev["ctxt"]) / dt)

        freqs = []
        for c in self.topo["cpus"]:
            f = read1(C.hs(f"/sys/devices/system/cpu/cpu{c['cpu']}/cpufreq/scaling_cur_freq"))
            freqs.append(int(f) // 1000 if f and f.isdigit() else None)
        s["freq"] = freqs

        la = (read1(C.hp("/proc/loadavg")) or "0 0 0").split()
        s["load"] = [float(la[0]), float(la[1]), float(la[2])]
        if len(la) > 3 and "/" in la[3]:
            s["procs"] = int(la[3].split("/")[1])

        m = self._meminfo()
        total = m.get("MemTotal", 0)
        avail = m.get("MemAvailable", m.get("MemFree", 0))
        s["mem"] = {
            "total": total, "used": total - avail, "avail": avail,
            "cached": m.get("Cached", 0) + m.get("SReclaimable", 0),
            "buffers": m.get("Buffers", 0), "free": m.get("MemFree", 0),
            "shared": m.get("Shmem", 0),
            "swap_total": m.get("SwapTotal", 0),
            "swap_used": m.get("SwapTotal", 0) - m.get("SwapFree", 0),
            "zswap": m.get("Zswapped", 0),
        }
        s["mem_pct"] = _r(100 * (total - avail) / total) if total else None

        net = {}
        for k, (rx, tx) in cur["net"].items():
            if k in prev["net"]:
                p = prev["net"][k]
                net[k] = [int(max(0, rx - p[0]) / dt), int(max(0, tx - p[1]) / dt)]
        s["net"] = net

        disk = {}
        rd = wr = 0
        for k, (rs, ws, io_ms, ios) in cur["disk"].items():
            if k in prev["disk"]:
                p = prev["disk"][k]
                r = int(max(0, rs - p[0]) * 512 / dt)
                w = int(max(0, ws - p[1]) * 512 / dt)
                util = min(100.0, max(0, io_ms - p[2]) / (dt * 10))
                disk[k] = [r, w, _r(util, 0), int(max(0, ios - p[3]) / dt)]
                if PHYS_DISK.match(k):
                    rd += r
                    wr += w
        s["disk"] = disk
        s["disk_total"] = [rd, wr]

        if time.monotonic() - self.sensor_scan > 300:
            self.sensors, self.fans = discover_sensors()
            self.sensor_scan = time.monotonic()
        temps = {}
        for t in self.sensors:
            v = read1(t["path"])
            if v and v.lstrip("-").isdigit():
                temps[t["id"]] = round(int(v) / 1000, 1)
        s["temps"] = temps
        fans = {}
        for f in self.fans:
            v = read1(f["path"])
            if v and v.isdigit():
                fans[f["id"]] = int(v)
        s["fans"] = fans

        ct = {}
        for cid, (usec, mem) in cur["cg"].items():
            if cid in prev["cg"]:
                pu = prev["cg"][cid][0]
                ct[cid] = [_r(max(0, usec - pu) / (dt * 1e4)), mem]
        s["ct"] = ct

        g = None
        if self.gpu:
            try:
                g = self.gpu.sample()
            except Exception as e:  # keep the core sampler alive
                log.warning("gpu sample: %s", e)
        if g is not None:
            s["gpu"] = g
        pw = self.rapl.sample()
        if pw:
            s["pw"] = pw
        if g is not None or pw:
            self.gpu_hist.add(now, g, pw)
        return s

    def run(self):
        while True:
            t0 = time.monotonic()
            try:
                s = self.sample()
                if s:
                    with self.lock:
                        self.seq += 1
                        s["seq"] = self.seq
                        self.buf.append(s)
                        self.latest = s
            except Exception:
                log.exception("sampler")
            time.sleep(max(0.2, C.SAMPLE_INTERVAL - (time.monotonic() - t0)))

    def since(self, seq):
        with self.lock:
            if seq <= 0:
                return list(self.buf)
            return [s for s in self.buf if s["seq"] > seq]
