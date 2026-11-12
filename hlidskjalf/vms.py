"""libvirt domains via read-only `virsh -r`."""

import os
import re
import struct
import time
import xml.etree.ElementTree as ET

from . import config as C
from .util import log, read, run

STATES = {0: "no state", 1: "running", 2: "blocked", 3: "paused", 4: "shutting down",
          5: "shut off", 6: "crashed", 7: "suspended"}


def virsh(*args, timeout=10):
    return run(["virsh", "-r", "-q", "-c", C.LIBVIRT_URI, *args], timeout=timeout)


def _disk_sizes(path):
    """(virtual capacity, allocated on disk) for a file-backed disk."""
    hpth = C.hr(path)
    try:
        st = os.stat(hpth)
    except OSError:
        return None, None
    alloc = st.st_blocks * 512
    cap = st.st_size
    try:
        with open(hpth, "rb") as f:
            hdr = f.read(32)
        if hdr[:4] == b"QFI\xfb":
            cap = struct.unpack(">Q", hdr[24:32])[0]
    except OSError:
        pass
    return cap, alloc


def _arp():
    out = {}
    for line in (read(C.hp("/proc/net/arp")) or "").splitlines()[1:]:
        p = line.split()
        if len(p) >= 4 and p[3] != "00:00:00:00:00:00" and p[2] != "0x0":
            out[p[3].lower()] = p[0]
    return out


class VMs:
    def __init__(self):
        self.xml_cache = {}   # name -> (ts, info)
        self.prev = {}        # name -> (mono, cpu.time ns, net rx, tx, blk r, w)
        self.lease_cache = {}
        self.snap_cache = {}  # name -> (ts, [snapshot names])

    def _xml(self, name):
        c = self.xml_cache.get(name)
        if c and time.time() - c[0] < 300:
            return c[1]
        rc, out = virsh("dumpxml", name)
        info = {"vcpus": 0, "mem": 0, "nics": [], "disks": [], "os": "", "firmware": ""}
        if rc == 0 and out.strip():
            try:
                x = ET.fromstring(out)
                v = x.find("vcpu")
                info["vcpus"] = int(v.text) if v is not None and v.text else 0
                m = x.find("memory")
                if m is not None and m.text:
                    unit = (m.get("unit") or "KiB").lower()
                    mul = {"b": 1, "bytes": 1, "kib": 1024, "k": 1024, "mib": 1 << 20, "m": 1 << 20,
                           "gib": 1 << 30, "g": 1 << 30}.get(unit, 1024)
                    info["mem"] = int(m.text) * mul
                ost = x.find("os")
                if ost is not None:
                    info["firmware"] = ost.get("firmware", "") or ("uefi" if ost.find("loader") is not None else "bios")
                for iface in x.findall("devices/interface"):
                    mac = iface.find("mac")
                    src = iface.find("source")
                    model = iface.find("model")
                    info["nics"].append({
                        "mac": mac.get("address", "").lower() if mac is not None else "",
                        "type": iface.get("type", ""),
                        "source": (src.get("bridge") or src.get("network") or src.get("dev") or "") if src is not None else "",
                        "model": model.get("type", "") if model is not None else "",
                    })
                for d in x.findall("devices/disk"):
                    if d.get("device") not in ("disk", None):
                        continue
                    src = d.find("source")
                    tgt = d.find("target")
                    path = ""
                    if src is not None:
                        path = src.get("file") or src.get("dev") or src.get("name") or ""
                    info["disks"].append({"path": path, "target": tgt.get("dev", "") if tgt is not None else "",
                                          "bus": tgt.get("bus", "") if tgt is not None else ""})
                meta = x.find("metadata")
                if meta is not None:
                    for el in meta.iter():
                        if el.tag.endswith("}os") and el.get("id"):
                            info["os"] = el.get("id").rstrip("/").split("/")[-1]
            except ET.ParseError as e:
                log.warning("dumpxml %s: %s", name, e)
        self.xml_cache[name] = (time.time(), info)
        return info

    def collect(self):
        rc, names_out = virsh("list", "--all", "--name")
        if rc != 0:
            return {"available": False, "error": "virsh failed (libvirt not reachable?)", "vms": []}
        names = [n.strip() for n in names_out.splitlines() if n.strip()]
        _, auto_out = virsh("list", "--all", "--autostart", "--name")
        autostart = {n.strip() for n in auto_out.splitlines() if n.strip()}
        stats = {}
        if names:
            rc, out = virsh("domstats", "--raw", "--state", "--cpu-total", "--balloon",
                            "--interface", "--block", timeout=15)
            cur = None
            for line in out.splitlines():
                line = line.strip()
                m = re.match(r"^Domain: '(.*)'$", line)
                if m:
                    cur = stats.setdefault(m.group(1), {})
                elif cur is not None and "=" in line:
                    k, v = line.split("=", 1)
                    cur[k] = v
        arp = _arp()
        now = time.monotonic()
        vms = []
        for n in names:
            st = stats.get(n, {})
            info = self._xml(n)
            state_code = int(st.get("state.state", 5))
            state = STATES.get(state_code, "unknown")
            cpu_ns = int(st.get("cpu.time", 0) or 0)

            def ssum(prefix, suffix):
                tot = 0
                cnt = int(st.get(f"{prefix}.count", 0) or 0)
                for i in range(cnt):
                    tot += int(st.get(f"{prefix}.{i}.{suffix}", 0) or 0)
                return tot

            rx, tx = ssum("net", "rx.bytes"), ssum("net", "tx.bytes")
            br, bw = ssum("block", "rd.bytes"), ssum("block", "wr.bytes")
            cpu_pct = net_rx = net_tx = blk_r = blk_w = None
            p = self.prev.get(n)
            if p and state == "running":
                dt = now - p[0]
                if dt > 0:
                    cpu_pct = round(max(0, cpu_ns - p[1]) / (dt * 1e7), 1)
                    net_rx, net_tx = max(0, rx - p[2]) / dt, max(0, tx - p[3]) / dt
                    blk_r, blk_w = max(0, br - p[4]) / dt, max(0, bw - p[5]) / dt
            if state == "running":
                self.prev[n] = (now, cpu_ns, rx, tx, br, bw)
            else:
                self.prev.pop(n, None)
            vcpus = info["vcpus"] or 1

            disks = []
            for d in info["disks"]:
                cap, alloc = _disk_sizes(d["path"]) if d["path"].startswith("/") else (None, None)
                # prefer libvirt's numbers when running
                for i in range(int(st.get("block.count", 0) or 0)):
                    if st.get(f"block.{i}.name") == d["target"]:
                        cap = int(st.get(f"block.{i}.capacity", cap or 0) or 0) or cap
                        alloc = int(st.get(f"block.{i}.physical", alloc or 0) or 0) or alloc
                disks.append({**d, "capacity": cap, "allocated": alloc})

            nics = []
            for nic in info["nics"]:
                nics.append({**nic, "ip": arp.get(nic["mac"], "")})
            if state == "running" and nics and not any(x["ip"] for x in nics):
                ips = self._leases(n)
                for x in nics:
                    x["ip"] = ips.get(x["mac"], "")

            mem_max = int(st.get("balloon.maximum", 0) or 0) * 1024 or info["mem"]
            mem_cur = int(st.get("balloon.current", 0) or 0) * 1024 or mem_max
            avail = int(st.get("balloon.available", 0) or 0) * 1024
            unused = int(st.get("balloon.unused", 0) or 0) * 1024
            usable = int(st.get("balloon.usable", 0) or 0) * 1024
            guest_used = (avail - usable) if avail and usable else ((avail - unused) if avail and unused else None)
            vms.append({
                "name": n, "state": state, "autostart": n in autostart,
                "vcpus": vcpus, "mem": mem_cur, "mem_max": mem_max,
                "mem_used": guest_used if state == "running" else None,
                "rss": int(st.get("balloon.rss", 0) or 0) * 1024 or None,
                "cpu_pct": cpu_pct,  # % of one host core
                "cpu_vcpu_pct": round(cpu_pct / vcpus, 1) if cpu_pct is not None else None,
                "net_rx": net_rx, "net_tx": net_tx, "blk_r": blk_r, "blk_w": blk_w,
                "disks": disks, "nics": nics, "os": info["os"], "firmware": info["firmware"],
                "snapshots": self._snapshots(n),
            })
        order = {"running": 0, "paused": 1}
        vms.sort(key=lambda v: (order.get(v["state"], 2), v["name"]))
        return {"available": True, "vms": vms, "ts": time.time()}

    def _snapshots(self, name):
        """Snapshot names for a domain (cached 60 s); None if virsh fails."""
        c = self.snap_cache.get(name)
        if c and time.time() - c[0] < 60:
            return c[1]
        rc, out = virsh("snapshot-list", "--name", name, timeout=5)
        snaps = [l.strip() for l in out.splitlines() if l.strip()] if rc == 0 else None
        self.snap_cache[name] = (time.time(), snaps)
        return snaps

    def _leases(self, name):
        c = self.lease_cache.get(name)
        if c and time.time() - c[0] < 120:
            return c[1]
        ips = {}
        for src in ("lease", "arp"):
            rc, out = virsh("domifaddr", name, "--source", src, timeout=5)
            for line in out.splitlines():
                p = line.split()
                if len(p) >= 4 and p[2] == "ipv4":
                    ips.setdefault(p[1].lower(), p[3].split("/")[0])
            if ips:
                break
        self.lease_cache[name] = (time.time(), ips)
        return ips

    def safe_collect(self):
        if not C.ENABLE_VMS:
            return {"available": False, "error": "disabled", "vms": []}
        try:
            return self.collect()
        except Exception as e:
            log.warning("vms: %s", e)
            return {"available": False, "error": str(e), "vms": []}
