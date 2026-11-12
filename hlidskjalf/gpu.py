"""GPU (Intel i915/xe primarily, amdgpu best-effort) and RAPL power.

Sources, all read-only:
  * i915 PMU via perf_event_open (ctypes): per-engine busy ns, actual and
    requested frequency, rc6 residency. Needs CAP_PERFMON (and, under SELinux,
    an unconfined label).
  * DRM fdinfo (/proc/<pid>/fdinfo, drm-engine-* / drm-cycles-*): per-client
    engine time, mapped to containers via /proc/<pid>/cgroup. Also the
    fallback for engine busy when the PMU is not available (xe, amdgpu).
  * sysfs: frequency limits, rc6 residency, amdgpu gpu_busy_percent.
  * powercap RAPL energy counters: package / core / uncore (= iGPU) watts.

Everything degrades to "available: False" on hosts without a GPU.
"""

import collections
import ctypes
import gzip
import json
import os
import re
import struct
import threading
import time

from . import config as C
from .util import helper_json, listdir, log, read, read1

ENGINE_NAMES = {"rcs": "Render/3D", "bcs": "Blitter", "vcs": "Video", "vecs": "VideoEnhance",
                "ccs": "Compute"}
# fdinfo engine class -> PMU-style class prefix
FD_CLASS = {"render": "rcs", "copy": "bcs", "video": "vcs", "video-enhance": "vecs",
            "video_enhance": "vecs", "compute": "ccs", "rcs": "rcs", "bcs": "bcs", "vcs": "vcs",
            "vecs": "vecs", "ccs": "ccs", "gfx": "rcs", "dec": "vcs", "enc": "vcs"}
CLASS_ORDER = ["rcs", "ccs", "vcs", "vecs", "bcs"]
LIBPOD = re.compile(r"libpod-(?:payload-)?([0-9a-f]{64})")
NR_PERF_EVENT_OPEN = {"x86_64": 298, "aarch64": 241}.get(os.uname().machine, 298)


def _engine_label(key):
    m = re.match(r"^([a-z]+?)(\d+)$", key)
    if not m:
        return ENGINE_NAMES.get(key, key)
    return ENGINE_NAMES.get(m.group(1), m.group(1))


def _pci_name(vendor, device):
    """Look the device up in the host's pci.ids (hwdata), if present."""
    v, d = vendor.lower().replace("0x", ""), device.lower().replace("0x", "")
    for path in (C.hr("/usr/share/hwdata/pci.ids"), C.hr("/usr/share/misc/pci.ids"), "/usr/share/hwdata/pci.ids"):
        try:
            f = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with f:
            in_vendor = False
            for line in f:
                if not line or line[0] == "#":
                    continue
                if line[0] != "\t":
                    if in_vendor:
                        break
                    in_vendor = line.startswith(v + "  ")
                elif in_vendor and line.startswith("\t" + d + "  "):
                    name = line.strip()[len(d) + 2:]
                    # "Alder Lake-P GT2 [Iris Xe Graphics]" -> keep both parts
                    return name
    return ""


class _Perf:
    """Minimal perf_event_open reader for uncore-style PMUs (one fd per event)."""

    def __init__(self):
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.libc.syscall.restype = ctypes.c_long

    def open(self, typ, config, cpu=0):
        attr = bytearray(128)
        struct.pack_into("IIQ", attr, 0, typ, 128, config)
        buf = (ctypes.c_char * 128).from_buffer(attr)
        fd = self.libc.syscall(NR_PERF_EVENT_OPEN, buf, -1, cpu, -1, 0x8)  # PERF_FLAG_FD_CLOEXEC
        if fd < 0:
            e = ctypes.get_errno()
            raise OSError(e, os.strerror(e))
        return fd

    @staticmethod
    def read(fd):
        return struct.unpack("Q", os.read(fd, 8))[0]


def scan_drm_fds():
    """(pid, fd) pairs holding a /dev/dri device. Other users' fds need
    CAP_SYS_PTRACE; the unprivileged dashboard gets them from the helper."""
    found = []
    proc = C.hp("/proc")
    for pid in os.listdir(proc):
        if not pid.isdigit():
            continue
        d = f"{proc}/{pid}/fd"
        try:
            fds = os.listdir(d)
        except OSError:
            continue
        for fd in fds:
            try:
                link = os.readlink(f"{d}/{fd}")
            except OSError:
                continue
            if link.startswith("/dev/dri/"):
                found.append((pid, fd))
    return found


def read_drm_clients(pairs):
    """Parse DRM fdinfo: ({(pdev, client id): (pid, {cls: busy}, {cls: total cycles})},
    {cls: engine capacity}). Deduplicated by client id (dup'ed / inherited fds)."""
    seen, capacity = {}, {}
    for pid, fd in pairs:
        txt = read(C.hp(f"/proc/{pid}/fdinfo/{fd}"))
        if not txt or "drm-client-id" not in txt:
            continue
        kv = {}
        for line in txt.splitlines():
            k, _, v = line.partition(":")
            if k.startswith("drm-"):
                kv[k] = v.strip()
        key = (kv.get("drm-pdev", ""), kv.get("drm-client-id"))
        if key in seen:
            continue
        vals, totals = {}, {}
        for k, v in kv.items():
            if k.startswith("drm-engine-capacity-"):
                cls = FD_CLASS.get(k[len("drm-engine-capacity-"):])
                if cls:
                    capacity[cls] = int(v) if v.isdigit() else 1
            elif k.startswith("drm-engine-"):
                cls = FD_CLASS.get(k[len("drm-engine-"):])
                if cls:
                    vals[cls] = vals.get(cls, 0) + int(v.split()[0])
            elif k.startswith("drm-total-cycles-"):
                cls = FD_CLASS.get(k[len("drm-total-cycles-"):])
                if cls:
                    totals[cls] = int(v.split()[0])
            elif k.startswith("drm-cycles-"):
                cls = FD_CLASS.get(k[len("drm-cycles-"):])
                if cls:
                    vals[cls] = vals.get(cls, 0) + int(v.split()[0])
        if vals:
            seen[key] = (pid, vals, totals)
    return seen, capacity


class Rapl:
    """Package / core / uncore (iGPU) power from powercap energy counters."""

    def __init__(self):
        self.zones = []  # (key, label, path, max_range)
        base = C.hs("/sys/class/powercap")
        for z in sorted(listdir(base)):
            if not z.startswith("intel-rapl:") or z.count(":") > 2:
                continue
            p = f"{base}/{z}"
            name = (read1(f"{p}/name") or "").strip()
            if not os.path.exists(f"{p}/energy_uj"):
                continue
            if name.startswith("package"):
                key, label = ("pkg" if name == "package-0" else name), "CPU package"
            elif name == "core":
                key, label = "core", "CPU cores"
            elif name == "uncore":
                key, label = "gpu", "iGPU (uncore)"
            elif name == "dram":
                key, label = "dram", "DRAM"
            elif name == "psys":
                key, label = "psys", "Platform"
            else:
                continue
            if any(k == key for k, *_ in self.zones):
                continue
            mx = read1(f"{p}/max_energy_range_uj")
            self.zones.append((key, label, f"{p}/energy_uj", int(mx) if mx and mx.isdigit() else 2 ** 32))
        self.prev = None

    def sample(self):
        if not self.zones:
            return None
        now = time.monotonic()
        cur = {}
        for key, _, path, _ in self.zones:
            v = read1(path)
            if v and v.isdigit():
                cur[key] = int(v)
        prev, self.prev = self.prev, (now, cur)
        if not prev or not cur:
            return None
        dt = now - prev[0]
        out = {}
        for key, _, _, mx in self.zones:
            if key in cur and key in prev[1]:
                d = cur[key] - prev[1][key]
                if d < 0:
                    d += mx
                out[key] = round(d / dt / 1e6, 2)
        return out or None

    def labels(self):
        return {k: lbl for k, lbl, *_ in self.zones}


class GPU:
    def __init__(self):
        self.card = None
        self.info = {"available": False}
        self.perf = None
        self.pmu = {}          # name -> fd
        self.pmu_prev = None
        self.pmu_error = None
        self.rc6_prev = None
        self.clients = {}      # (pid, fd) -> owner key
        self.client_prev = {}  # client key -> (mono, {cls: value}, {cls: total_cycles})
        self.scan_at = 0
        self.pid_owner = {}    # pid -> (owner, kind)
        self.ct_names = {}     # container id -> name (set by containers collector)
        self.capacity = {}
        self.helper_ts = None
        self.helper_last = ({}, {})
        self.lock = threading.Lock()
        try:
            self._discover()
        except Exception as e:  # never break the sampler
            log.warning("gpu discovery: %s", e)
            self.info = {"available": False, "error": str(e)}

    # ---- discovery -----------------------------------------------------
    def _discover(self):
        base = C.hs("/sys/class/drm")
        cards = []
        for c in sorted(listdir(base)):
            if not re.match(r"^card\d+$", c):
                continue
            drv_link = f"{base}/{c}/device/driver"
            try:
                drv = os.path.basename(os.readlink(drv_link))
            except OSError:
                continue
            cards.append((c, drv))
        if not cards:
            self.info = {"available": False, "error": "no DRM devices"}
            return
        # prefer Intel/AMD render-capable devices
        rank = {"i915": 0, "xe": 0, "amdgpu": 1, "nouveau": 2, "nvidia": 2}
        cards.sort(key=lambda x: rank.get(x[1], 5))
        card, drv = cards[0]
        dev = f"{base}/{card}/device"
        vendor = (read1(f"{dev}/vendor") or "").strip()
        device = (read1(f"{dev}/device") or "").strip()
        pci = os.path.basename(os.path.realpath(dev))
        name = _pci_name(vendor, device) if vendor and device else ""
        short = name
        m = re.search(r"\[(.+?)\]", name)
        if m:
            short = m.group(1)
        self.card = card
        self.drv = drv
        self.dev = dev
        self.base = f"{base}/{card}"
        self.pci = pci
        engines = []
        if drv == "i915":
            engines = sorted(listdir(f"{self.base}/engine"), key=self._engine_sort)
        self.info = {
            "available": True, "card": card, "driver": drv, "pci": pci,
            "vendor": vendor, "device": device,
            "name": short or f"{drv} GPU {device}", "full_name": name,
            "engines": self._engine_list(engines),
        }
        self._freq_paths()
        self._open_pmu(engines)
        if not self.info["engines"]:
            # learn classes from fdinfo on first client scan
            self.info["engines_from"] = "fdinfo"
        log.info("gpu: %s (%s, %s) engines=%s pmu=%s", self.info["name"], drv, pci,
                 [e["key"] for e in self.info["engines"]], "ok" if self.pmu else self.pmu_error)

    @staticmethod
    def _engine_list(keys):
        cls = [re.sub(r"\d+$", "", k) for k in keys]
        out = []
        for k, c in zip(keys, cls):
            label = _engine_label(k)
            if cls.count(c) > 1:
                label += " " + k[len(c):]
            out.append({"key": k, "label": label, "cls": c})
        return out

    @staticmethod
    def _engine_sort(e):
        m = re.match(r"^([a-z]+?)(\d+)$", e)
        cls, n = (m.group(1), int(m.group(2))) if m else (e, 0)
        return (CLASS_ORDER.index(cls) if cls in CLASS_ORDER else 9, n)

    def _freq_paths(self):
        b = self.base
        if self.drv == "i915":
            self.f_act = f"{b}/gt_act_freq_mhz"
            self.f_cur = f"{b}/gt_cur_freq_mhz"
            self.f_max = f"{b}/gt_max_freq_mhz"
            self.f_rp0 = f"{b}/gt_RP0_freq_mhz"
            self.f_min = f"{b}/gt_min_freq_mhz"
            self.rc6_path = f"{b}/gt/gt0/rc6_residency_ms"
            if not os.path.exists(self.rc6_path):
                self.rc6_path = f"{b}/power/rc6_residency_ms"
        elif self.drv == "xe":
            g = f"{self.dev}/tile0/gt0"
            self.f_act = f"{g}/freq0/act_freq"
            self.f_cur = f"{g}/freq0/cur_freq"
            self.f_max = f"{g}/freq0/max_freq"
            self.f_rp0 = f"{g}/freq0/rp0_freq"
            self.f_min = f"{g}/freq0/min_freq"
            self.rc6_path = f"{g}/gtidle/idle_residency_ms"
        else:
            self.f_act = self.f_cur = self.f_max = self.f_rp0 = self.f_min = None
            self.rc6_path = None
        rp0 = read1(self.f_rp0) if self.f_rp0 else None
        mx = read1(self.f_max) if self.f_max else None
        mn = read1(self.f_min) if self.f_min else None
        self.info["freq_max"] = int(rp0) if rp0 and rp0.isdigit() else None
        self.info["freq_limit"] = int(mx) if mx and mx.isdigit() else None
        self.info["freq_min"] = int(mn) if mn and mn.isdigit() else None

    def _open_pmu(self, engines):
        if self.drv != "i915":
            self.pmu_error = f"no PMU reader for {self.drv}"
            return
        # i915 PMU is named "i915" for the integrated GPU, "i915_<pci>" for others
        pdir = None
        for cand in ("i915", "i915_" + self.pci.replace(":", "_")):
            p = C.hs(f"/sys/bus/event_source/devices/{cand}")
            if os.path.isdir(p):
                pdir = p
                break
        if not pdir:
            self.pmu_error = "i915 PMU not found"
            return
        try:
            typ = int(read1(f"{pdir}/type"))
            self.perf = _Perf()
            want = [f"{e}-busy" for e in engines] + ["actual-frequency", "requested-frequency", "rc6-residency"]
            cpumask = (read1(f"{pdir}/cpumask") or "0").split(",")[0].split("-")[0]
            cpu = int(cpumask) if cpumask.isdigit() else 0
            for ev in want:
                spec = read1(f"{pdir}/events/{ev}")
                if not spec:
                    continue
                m = re.search(r"config=(0x[0-9a-f]+|\d+)", spec)
                if not m:
                    continue
                self.pmu[ev] = self.perf.open(typ, int(m.group(1), 0), cpu)
        except (OSError, ValueError, TypeError) as e:
            for fd in self.pmu.values():
                os.close(fd)
            self.pmu = {}
            self.pmu_error = f"perf_event_open: {e}"

    # ---- clients (fdinfo) --------------------------------------------------
    def _scan_clients(self):
        self.clients = scan_drm_fds()
        live = {p for p, _ in self.clients}
        for pid in list(self.pid_owner):
            if pid not in live:
                del self.pid_owner[pid]

    def _owner(self, pid):
        o = self.pid_owner.get(pid)
        if o:
            return o
        cg = read(C.hp(f"/proc/{pid}/cgroup")) or ""
        m = LIBPOD.search(cg)
        if m:
            cid = m.group(1)
            o = ("ct:" + cid, "container")
        else:
            comm = (read1(C.hp(f"/proc/{pid}/comm")) or "?").strip()
            unit = re.search(r"/([^/]+\.service)", cg)
            o = ("proc:" + comm, unit.group(1) if unit else "host")
        self.pid_owner[pid] = o
        return o

    def _read_clients(self, now):
        """Per-owner engine-class busy %, plus per-class totals for fallback.
        Uses the helper's snapshot when present (refreshed every ~15 s)."""
        snap = helper_json("gpu-clients.json", 60)
        if snap is None:
            if now - self.scan_at > 10:
                self.scan_at = now
                try:
                    self._scan_clients()
                except OSError as e:
                    log.warning("gpu client scan: %s", e)
            seen, cap = read_drm_clients(self.clients)
            seen = {k: v for k, v in seen.items() if not k[0] or k[0] == self.pci}
            self.capacity.update(cap)
            return self._client_deltas(seen, now)
        if snap.get("ts") != self.helper_ts:
            self.helper_ts = snap.get("ts")
            self.capacity.update(snap.get("capacity") or {})
            seen = {(c["pdev"], c["id"]): (str(c["pid"]), c["vals"], c["totals"])
                    for c in snap.get("clients") or [] if not c["pdev"] or c["pdev"] == self.pci}
            self.helper_last = self._client_deltas(seen, snap["ts"])
        return self.helper_last

    def _client_deltas(self, seen, now):
        owners = {}
        cls_tot = {}
        new_prev = {}
        for key, (pid, vals, totals) in seen.items():
            prev = self.client_prev.get(key)
            new_prev[key] = (now, vals, totals)
            if not prev:
                continue
            dt = now - prev[0]
            if dt <= 0:
                continue
            owner, kind = self._owner(pid)
            o = owners.setdefault(owner, {"kind": kind, "e": {}, "n": 0})
            o["n"] += 1
            for cls, v in vals.items():
                pv = prev[1].get(cls)
                if pv is None or v < pv:
                    continue
                if totals.get(cls) is not None and prev[2].get(cls) is not None:
                    dtot = totals[cls] - prev[2][cls]
                    pct = 100 * (v - pv) / dtot if dtot > 0 else 0
                else:
                    pct = 100 * (v - pv) / (dt * 1e9)
                pct /= max(1, self.capacity.get(cls, 1))
                o["e"][cls] = o["e"].get(cls, 0) + pct
                cls_tot[cls] = cls_tot.get(cls, 0) + pct
        self.client_prev = new_prev
        return owners, cls_tot

    def _owner_label(self, owner):
        kind, _, ident = owner.partition(":")
        if kind == "ct":
            return self.ct_names.get(ident, ident[:12])
        return ident

    # ---- sample ----------------------------------------------------------
    def sample(self):
        if not self.card:
            return None
        now = time.monotonic()
        out = {}
        # PMU
        if self.pmu:
            try:
                cur = {k: _Perf.read(fd) for k, fd in self.pmu.items()}
            except OSError as e:
                log.warning("gpu pmu read: %s", e)
                cur = None
            prev, self.pmu_prev = self.pmu_prev, (now, cur) if cur else None
            if prev and cur:
                dt = now - prev[0]
                eng = {}
                for k, v in cur.items():
                    d = v - prev[1].get(k, v)
                    if k.endswith("-busy"):
                        eng[k[:-5]] = round(min(100.0, 100 * d / (dt * 1e9)), 1)
                    elif k == "actual-frequency":
                        out["f"] = round(d / dt)
                    elif k == "requested-frequency":
                        out["rf"] = round(d / dt)
                    elif k == "rc6-residency":
                        out["rc6"] = round(min(100.0, 100 * d / (dt * 1e9)), 1)
                out["e"] = eng
        # sysfs frequency / rc6 fallbacks
        if "f" not in out and self.f_act:
            v = read1(self.f_act)
            if v and v.isdigit():
                out["f"] = int(v)
        if "rf" not in out and self.f_cur:
            v = read1(self.f_cur)
            if v and v.isdigit():
                out["rf"] = int(v)
        if "rc6" not in out and self.rc6_path:
            v = read1(self.rc6_path)
            if v and v.isdigit():
                prev, self.rc6_prev = self.rc6_prev, (now, int(v))
                if prev:
                    out["rc6"] = round(min(100.0, max(0.0, (int(v) - prev[1]) / ((now - prev[0]) * 10))), 1)
        if self.drv == "amdgpu":
            v = read1(f"{self.dev}/gpu_busy_percent")
            if v and v.isdigit():
                out.setdefault("e", {})["rcs0"] = float(v)
        # clients
        owners, cls_tot = self._read_clients(now)
        if "e" not in out and cls_tot is not None:
            # no PMU: engine-class busy from the sum over clients
            out["e"] = {cls + "0": round(min(100.0, v), 1) for cls, v in cls_tot.items()}
            for cls in self.capacity:
                out["e"].setdefault(cls + "0", 0.0)
            if not self.info["engines"] and out["e"]:
                self.info["engines"] = self._engine_list(sorted(out["e"], key=self._engine_sort))
        clients = {}
        for owner, o in owners.items():
            clients[self._owner_label(owner)] = {
                "k": o["kind"], "n": o["n"],
                "e": {c: round(v, 1) for c, v in o["e"].items()},
            }
        out["c"] = clients
        return out

    def static(self):
        i = dict(self.info)
        i["pmu"] = bool(self.pmu)
        i["pmu_error"] = self.pmu_error
        i["capacity"] = dict(self.capacity)
        return i


class GpuHistory:
    """Minute buckets of GPU + RAPL power, kept 7 days in memory and persisted
    to HLID_DATA_DIR so a restart does not lose them."""

    KEEP = 7 * 24 * 60 + 10

    def __init__(self):
        self.rows = collections.deque(maxlen=self.KEEP)
        self.acc = None
        self.lock = threading.Lock()
        self.path = os.path.join(C.DATA_DIR, "gpu-history.json.gz") if C.DATA_DIR else None
        self.saved_at = time.monotonic()
        self._load()

    def _load(self):
        if not self.path:
            return
        try:
            with gzip.open(self.path, "rt") as f:
                rows = json.load(f)
            cutoff = time.time() - 7 * 86400
            self.rows.extend(r for r in rows if r.get("t", 0) >= cutoff)
            log.info("gpu history: loaded %d minutes", len(self.rows))
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning("gpu history load: %s", e)

    def save(self):
        if not self.path:
            return
        try:
            with self.lock:
                rows = list(self.rows)
            tmp = self.path + ".tmp"
            with gzip.open(tmp, "wt", compresslevel=5) as f:
                json.dump(rows, f, separators=(",", ":"))
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("gpu history save: %s", e)

    def add(self, t, g, pw):
        minute = int(t // 60) * 60
        a = self.acc
        if a and a["t"] != minute:
            self._flush()
            a = None
        if a is None:
            a = self.acc = {"t": minute, "n": 0, "e": {}, "f": [0, 0], "rc6": [0, 0], "pw": {}, "c": {}}
        a["n"] += 1
        if g:
            for k, v in (g.get("e") or {}).items():
                a["e"][k] = a["e"].get(k, 0) + v
            if g.get("f") is not None:
                a["f"][0] += g["f"]
                a["f"][1] += 1
            if g.get("rc6") is not None:
                a["rc6"][0] += g["rc6"]
                a["rc6"][1] += 1
            for name, c in (g.get("c") or {}).items():
                busy = max(c["e"].values()) if c["e"] else 0
                a["c"][name] = a["c"].get(name, 0) + busy
        for k, v in (pw or {}).items():
            s = a["pw"].setdefault(k, [0, 0])
            s[0] += v
            s[1] += 1

    def _flush(self):
        a = self.acc
        n = a["n"] or 1
        row = {"t": a["t"],
               "e": {k: round(v / n, 1) for k, v in a["e"].items()},
               "f": round(a["f"][0] / a["f"][1]) if a["f"][1] else None,
               "rc6": round(a["rc6"][0] / a["rc6"][1], 1) if a["rc6"][1] else None,
               "pw": {k: round(s[0] / s[1], 2) for k, s in a["pw"].items() if s[1]},
               "c": {k: round(v / n, 1) for k, v in a["c"].items() if v / n >= 0.05}}
        with self.lock:
            self.rows.append(row)
        if time.monotonic() - self.saved_at > 600:
            self.saved_at = time.monotonic()
            threading.Thread(target=self.save, daemon=True).start()

    def build(self, span, step):
        now = time.time()
        start = (int(now - span) // step) * step
        with self.lock:
            rows = [r for r in self.rows if r["t"] >= start]
        nb = int((now - start) // step) + 1
        buckets = [[] for _ in range(nb)]
        for r in rows:
            i = int((r["t"] - start) // step)
            if 0 <= i < nb:
                buckets[i].append(r)
        ekeys = sorted({k for r in rows for k in r["e"]})
        pkeys = sorted({k for r in rows for k in r["pw"]})
        ctot = collections.Counter()
        for r in rows:
            for k, v in r["c"].items():
                ctot[k] += v
        ckeys = [k for k, _ in ctot.most_common(6)]

        def avg(vals):
            vals = [v for v in vals if v is not None]
            return round(sum(vals) / len(vals), 2) if vals else None

        out = {"t": [], "e": {k: [] for k in ekeys}, "f": [], "rc6": [],
               "pw": {k: [] for k in pkeys}, "c": {k: [] for k in ckeys}}
        for i, b in enumerate(buckets):
            out["t"].append(start + i * step)
            for k in ekeys:
                out["e"][k].append(avg([r["e"].get(k) for r in b]))
            out["f"].append(avg([r["f"] for r in b]))
            out["rc6"].append(avg([r["rc6"] for r in b]))
            for k in pkeys:
                out["pw"][k].append(avg([r["pw"].get(k) for r in b]))
            for k in ckeys:
                out["c"][k].append(avg([r["c"].get(k, 0) for r in b]) if b else None)
        out["minutes"] = len(rows)
        return out
