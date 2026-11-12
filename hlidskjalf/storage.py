"""Filesystems, block devices, btrfs, md RAID, SMART and NICs."""

import json
import os
import re
import threading
import time

from . import config as C
from .util import call_with_timeout, helper_json, listdir, read, read1, readint, run

REAL_FS = {"ext2", "ext3", "ext4", "xfs", "btrfs", "vfat", "exfat", "f2fs", "zfs", "ntfs3",
           "ntfs", "bcachefs", "nfs", "nfs4", "cifs", "smb3", "fuse.mergerfs", "fuse.sshfs",
           "fuseblk", "jfs", "reiserfs"}
NET_FS = {"nfs", "nfs4", "cifs", "smb3", "fuse.sshfs"}
SKIP_MP = re.compile(r"^/(proc|sys|dev|run/(user|containers|netns)|var/lib/(containers|docker)/.*/(overlay|merged))")
PHYS = re.compile(r"^(nvme\d+n\d+|sd[a-z]+|vd[a-z]+|hd[a-z]+|mmcblk\d+)$")


def _unescape(s):
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)


def mountinfo():
    txt = read(C.hp("/proc/1/mountinfo")) or read(C.hp("/proc/self/mountinfo")) or ""
    out = []
    for line in txt.splitlines():
        pre, _, post = line.partition(" - ")
        p = pre.split()
        q = post.split()
        if len(p) < 6 or len(q) < 2:
            continue
        out.append({
            "majmin": p[2], "root": _unescape(p[3]), "mp": _unescape(p[4]),
            "opts": p[5], "fstype": q[0], "source": _unescape(q[1]),
            "super": q[2] if len(q) > 2 else "",
        })
    return out


def _dm_info(majmin):
    base = C.hs(f"/sys/dev/block/{majmin}")
    name = read1(f"{base}/dm/name")
    uuid = read1(f"{base}/dm/uuid", "") or ""
    kname = os.path.basename(os.path.realpath(base))
    slaves = listdir(f"{base}/slaves")
    info = {"kname": kname, "slaves": slaves}
    if name:
        info["dm"] = name
        if uuid.startswith("LVM-"):
            # VG-LV with '-' in names escaped as '--'
            parts = re.split(r"(?<!-)-(?!-)", name, maxsplit=1)
            if len(parts) == 2:
                info["vg"], info["lv"] = (x.replace("--", "-") for x in parts)
            info["kind"] = "lvm"
        elif uuid.startswith("CRYPT-"):
            info["kind"] = "crypt"
        else:
            info["kind"] = "dm"
    elif kname.startswith("md"):
        info["kind"] = "md"
    return info


class Storage:
    def __init__(self):
        self.smart = {}          # dev -> data
        self.smart_lock = threading.Lock()
        self.statvfs_busy = set()
        self.last_fs = {}
        self.ip_cache = (0, {})

    # ---------------- filesystems ----------------
    def filesystems(self):
        seen = set()
        out = []
        for m in mountinfo():
            fs = m["fstype"]
            if fs not in REAL_FS and not fs.startswith("fuse."):
                continue
            if SKIP_MP.match(m["mp"]) or m["mp"].startswith("/host/"):
                continue
            key = (m["majmin"], m["root"]) if fs not in NET_FS else (m["source"],)
            if key in seen:
                continue
            seen.add(key)
            mp = m["mp"]
            ent = {"mount": mp, "fstype": fs, "source": m["source"],
                   "ro": m["opts"].split(",")[0] == "ro",
                   "net": fs in NET_FS}
            if not ent["net"] and m["majmin"].split(":")[0] != "0":
                ent.update(_dm_info(m["majmin"]))
            if fs == "btrfs":
                sv = re.search(r"subvol=([^,]+)", m["super"])
                if sv:
                    ent["subvol"] = sv.group(1)
            path = C.hr(mp) if C.HOST_ROOT else mp
            if mp in self.statvfs_busy:
                ent["stale"] = True
                prev = self.last_fs.get(mp)
                if prev:
                    ent.update({k: prev[k] for k in ("size", "used", "avail") if k in prev})
                out.append(ent)
                continue

            def sv_call(path=path, mp=mp):
                self.statvfs_busy.add(mp)
                try:
                    return os.statvfs(path)
                finally:
                    self.statvfs_busy.discard(mp)

            st, err = call_with_timeout(sv_call, 2.0 if ent["net"] else 5.0)
            if st is None:
                ent["error"] = err
                if err == "timeout":
                    self.statvfs_busy.add(mp)
                    ent["stale"] = True
            else:
                size = st.f_blocks * st.f_frsize
                avail = st.f_bavail * st.f_frsize
                free = st.f_bfree * st.f_frsize
                ent.update({"size": size, "used": size - free, "avail": avail,
                            "inodes_pct": round(100 * (st.f_files - st.f_ffree) / st.f_files, 1) if st.f_files else None})
                self.last_fs[mp] = ent
            out.append(ent)
        out.sort(key=lambda e: (e["net"], e["mount"] != "/", e["mount"]))
        return out

    # ---------------- block devices ----------------
    def block_devices(self):
        disks = []
        for name in listdir(C.hs("/sys/block")):
            if not PHYS.match(name):
                continue
            b = C.hs(f"/sys/block/{name}")
            real = os.path.realpath(b)
            tran = "nvme" if name.startswith("nvme") else "usb" if "/usb" in real else \
                "virtio" if name.startswith("vd") else "sata"
            model = read1(f"{b}/device/model", "") or ""
            parts = []
            for p in listdir(b):
                if p.startswith(name) and os.path.exists(f"{b}/{p}/partition"):
                    parts.append({"name": p, "size": (readint(f"{b}/{p}/size", 0) or 0) * 512,
                                  "holders": listdir(f"{b}/{p}/holders")})
            disks.append({
                "name": name, "model": model.strip(), "tran": tran,
                "size": (readint(f"{b}/size", 0) or 0) * 512,
                "rotational": read1(f"{b}/queue/rotational") == "1",
                "serial": (read1(f"{b}/device/serial", "") or "").strip(),
                "parts": parts,
                "removable": read1(f"{b}/removable") == "1",
            })
        return disks

    def md_arrays(self):
        out = []
        for name in listdir(C.hs("/sys/block")):
            if not name.startswith("md"):
                continue
            md = C.hs(f"/sys/block/{name}/md")
            if not os.path.isdir(md):
                continue
            members = []
            for d in listdir(md):
                if d.startswith("dev-"):
                    slot = read1(f"{md}/{d}/slot", "")
                    members.append({"dev": d[4:], "state": read1(f"{md}/{d}/state", ""),
                                    "slot": int(slot) if slot.isdigit() else None,  # "none" = spare
                                    "errors": readint(f"{md}/{d}/errors", 0)})
            members.sort(key=lambda m: (m["slot"] is None, m["slot"] or 0, m["dev"]))
            speed = read1(f"{md}/sync_speed", "")
            bm = readint(f"{md}/bitmap/chunksize", 0)
            out.append({
                "name": name, "level": read1(f"{md}/level", ""),
                "state": read1(f"{md}/array_state", ""),
                "raid_disks": readint(f"{md}/raid_disks"),
                "degraded": readint(f"{md}/degraded", 0),
                "sync_action": read1(f"{md}/sync_action", ""),
                "sync_completed": read1(f"{md}/sync_completed", ""),
                "sync_speed": int(speed) * 1024 if speed.isdigit() else None,  # sysfs is KiB/s
                "last_sync_action": read1(f"{md}/last_sync_action", ""),
                "mismatch_cnt": readint(f"{md}/mismatch_cnt"),  # sectors found differing by the last check
                "metadata": read1(f"{md}/metadata_version", ""),
                "uuid": read1(f"{md}/uuid", ""),
                "consistency": read1(f"{md}/consistency_policy", ""),
                "bitmap_chunk": bm or None,
                "chunk_size": readint(f"{md}/chunk_size", 0) or None,
                "size": (readint(C.hs(f"/sys/block/{name}/size"), 0) or 0) * 512,
                "members": members,
            })
        return out

    def btrfs(self, fslist):
        out = []
        root = C.hs("/sys/fs/btrfs")
        for uuid in listdir(root):
            if uuid == "features" or not os.path.isdir(f"{root}/{uuid}"):
                continue
            b = f"{root}/{uuid}"
            devs = listdir(f"{b}/devices")
            alloc = {}
            for kind in ("data", "metadata", "system"):
                a = f"{b}/allocation/{kind}"
                profiles = [p for p in listdir(a) if os.path.isdir(f"{a}/{p}")]
                alloc[kind] = {
                    "total": readint(f"{a}/total_bytes", 0),
                    "used": readint(f"{a}/bytes_used", 0),
                    "disk_total": readint(f"{a}/disk_total", 0),
                    "disk_used": readint(f"{a}/disk_used", 0),
                    "profiles": profiles,
                }
            devinfo = []
            for did in listdir(f"{b}/devinfo"):
                errs = {}
                for line in (read(f"{b}/devinfo/{did}/error_stats") or "").splitlines():
                    k, _, v = line.partition(" ")
                    if v.strip().isdigit():
                        errs[k] = int(v)
                devinfo.append({"devid": did, "missing": read1(f"{b}/devinfo/{did}/missing") == "1",
                                "errors": errs})
            dev_list = []
            for d in devs:
                sz = readint(C.hs(f"/sys/class/block/{d}/size"), 0) or 0
                dev_list.append({"name": d, "size": sz * 512})
            mounts = [f["mount"] for f in fslist if f["fstype"] == "btrfs"
                      and os.path.basename(f.get("source", "")) in devs]
            total_dev = sum(d["size"] for d in dev_list)
            out.append({
                "uuid": uuid, "label": read1(f"{b}/label", "") or "",
                "devices": dev_list, "devinfo": devinfo, "alloc": alloc,
                "mounts": mounts, "device_size": total_dev,
                "allocated": sum(a["disk_total"] or 0 for a in alloc.values()),
                "errors": sum(sum(d["errors"].values()) for d in devinfo),
            })
        return out

    # ---------------- SMART ----------------
    def smart_scan(self):
        """SMART needs CAP_SYS_RAWIO / CAP_SYS_ADMIN. Unprivileged, take the
        helper's smart.json; as root (or in the helper), run smartctl."""
        if os.geteuid() != 0:
            snap = helper_json("smart.json", 3 * C.SMART_INTERVAL + 60)
            with self.smart_lock:
                self.smart = (snap or {}).get("disks") or {}
            return time.time()
        for d in self.block_devices():
            if d["removable"] or d["tran"] == "virtio":
                continue
            dev = f"/dev/{d['name']}"
            rc, out = run(["smartctl", "-j", "-a", "-n", "standby", dev], timeout=30)
            try:
                j = json.loads(out) if out.strip() else {}
            except ValueError:
                j = {}
            info = self._parse_smart(j, rc)
            info["name"] = d["name"]
            with self.smart_lock:
                prev = self.smart.get(d["name"])
                if info.get("standby") and prev and not prev.get("error"):
                    prev = dict(prev)
                    prev["standby"] = True
                    self.smart[d["name"]] = prev
                else:
                    self.smart[d["name"]] = info
        return time.time()

    @staticmethod
    def _parse_smart(j, rc):
        msgs = " ".join(m.get("string", "") for m in (j.get("smartctl", {}).get("messages") or []))
        if "STANDBY" in msgs.upper() or "SLEEP" in msgs.upper():
            return {"standby": True, "ts": time.time()}
        if not j or (rc & 2 and "smart_status" not in j):
            return {"error": msgs or f"smartctl rc={rc}", "ts": time.time()}
        info = {
            "ts": time.time(),
            "model": j.get("model_name", ""),
            "serial": j.get("serial_number", ""),
            "firmware": j.get("firmware_version", ""),
            "passed": (j.get("smart_status") or {}).get("passed"),
            "temp": (j.get("temperature") or {}).get("current"),
            "hours": (j.get("power_on_time") or {}).get("hours"),
            "cycles": j.get("power_cycle_count"),
            "rpm": j.get("rotation_rate"),
            "capacity": (j.get("user_capacity") or {}).get("bytes"),
        }
        nv = j.get("nvme_smart_health_information_log")
        if nv:
            info.update({
                "type": "nvme",
                "wear_pct": nv.get("percentage_used"),
                "spare_pct": nv.get("available_spare"),
                "written": (nv.get("data_units_written") or 0) * 512000,
                "read": (nv.get("data_units_read") or 0) * 512000,
                "media_errors": nv.get("media_errors"),
                "unsafe_shutdowns": nv.get("unsafe_shutdowns"),
                "critical_warning": nv.get("critical_warning"),
                "err_log": nv.get("num_err_log_entries"),
            })
        else:
            attrs = {}
            for a in (j.get("ata_smart_attributes") or {}).get("table") or []:
                attrs[a.get("id")] = a
            def raw(i):
                a = attrs.get(i)
                return (a.get("raw") or {}).get("value") if a else None
            wear = None
            eu = j.get("endurance_used") or {}
            if eu.get("current_percent") is not None:
                wear = eu["current_percent"]
            else:
                for i in (177, 231, 233, 202, 169):
                    a = attrs.get(i)
                    if a and a.get("value") is not None and a.get("value") <= 100:
                        wear = 100 - a["value"] if i != 202 else a["value"]
                        break
            lba = raw(241)
            info.update({
                "type": "ssd" if (j.get("rotation_rate") == 0) else "hdd",
                "wear_pct": wear if info.get("rpm") == 0 else None,
                "reallocated": raw(5), "pending": raw(197), "uncorrectable": raw(198),
                "crc_errors": raw(199),
                "written": lba * (j.get("logical_block_size") or 512) if lba else None,
            })
        return info

    def smart_snapshot(self):
        with self.smart_lock:
            return dict(self.smart)

    # ---------------- network ----------------
    def _ips(self):
        if time.time() - self.ip_cache[0] < 30:
            return self.ip_cache[1]
        rc, out = run(["ip", "-j", "addr", "show"], timeout=5)
        ips = {}
        try:
            for i in json.loads(out) if rc == 0 else []:
                ips[i["ifname"]] = [f"{a['local']}/{a['prefixlen']}" for a in i.get("addr_info", [])
                                    if a.get("scope") == "global"]
        except (ValueError, KeyError):
            pass
        self.ip_cache = (time.time(), ips)
        return ips

    @staticmethod
    def default_iface():
        for line in (read(C.hp("/proc/net/route")) or "").splitlines()[1:]:
            p = line.split()
            if len(p) > 2 and p[1] == "00000000":
                return p[0]
        return None

    def nics(self):
        default = self.default_iface()
        ips = self._ips()
        out = []
        for n in listdir(C.hs("/sys/class/net")):
            if n == "lo" or n.startswith(("veth", "vnet", "tap")):
                continue
            b = C.hs(f"/sys/class/net/{n}")
            is_bridge = os.path.isdir(f"{b}/bridge")
            virtual = "/virtual/" in os.path.realpath(b)
            speed = readint(f"{b}/speed")
            master = os.path.basename(os.path.realpath(f"{b}/master")) if os.path.exists(f"{b}/master") else ""
            driver = os.path.basename(os.path.realpath(f"{b}/device/driver")) if os.path.exists(f"{b}/device/driver") else ""
            members = [x for x in listdir(f"{b}/brif")] if is_bridge else []
            out.append({
                "name": n, "state": read1(f"{b}/operstate", ""),
                "speed": speed if speed and speed > 0 else None,
                "mtu": readint(f"{b}/mtu"), "mac": read1(f"{b}/address", ""),
                "kind": "bridge" if is_bridge else ("virtual" if virtual else "physical"),
                "master": master, "driver": driver,
                "members": [m for m in members if not m.startswith(("veth", "vnet", "tap"))],
                "taps": len([m for m in members if m.startswith(("veth", "vnet", "tap"))]),
                "ips": ips.get(n, []), "default": n == default,
                "rx_total": readint(f"{b}/statistics/rx_bytes", 0),
                "tx_total": readint(f"{b}/statistics/tx_bytes", 0),
                "errors": (readint(f"{b}/statistics/rx_errors", 0) or 0) + (readint(f"{b}/statistics/tx_errors", 0) or 0),
            })
        kind_order = {"bridge": 0, "physical": 1, "virtual": 2}
        out.sort(key=lambda x: (not x["default"], x["state"] != "up", kind_order[x["kind"]], x["name"]))
        return out

    def collect(self):
        fs = self.filesystems()
        return {
            "filesystems": fs,
            "disks": self.block_devices(),
            "md": self.md_arrays(),
            "btrfs": self.btrfs(fs),
            "nics": self.nics(),
            "ts": time.time(),
        }
