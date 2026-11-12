"""Physical build inventory for the hardware report: board, BIOS, CPU, memory
modules, PCI cards, network adapters and USB devices.

Everything except the memory modules and slot names comes from world-readable
sysfs. Those two need the raw SMBIOS table (root only), which the privileged
helper parses with privileged() and writes to hardware.json.

Serial numbers and MAC addresses are deliberately left out: the report is
meant to be shared.
"""

import os
import re
import struct
import time

from . import config as C
from .util import helper_json, listdir, read, read1

JUNK = {"", "to be filled by o.e.m.", "default string", "system product name", "system version", "none",
        "not specified", "unknown", "no dimm", "undefined", "not available", "n/a", "0000", "empty"}


def _clean(s):
    s = (s or "").strip()
    return "" if s.lower() in JUNK else s


# ---------------- SMBIOS (helper, root) ----------------

def smbios_structs(raw):
    """Split a raw SMBIOS table into (type, formatted bytes, strings)."""
    out, i, n = [], 0, len(raw)
    while i + 4 <= n:
        typ, length = raw[i], raw[i + 1]
        if length < 4 or i + length > n:
            break
        data = raw[i:i + length]
        j = i + length
        strings = []
        while j < n and raw[j] != 0:
            end = raw.find(b"\0", j)
            if end < 0:
                end = n
            strings.append(raw[j:end].decode("latin-1", "replace").strip())
            j = end + 1
        # the string set ends with an extra NUL (two NULs when there are no strings)
        j += 1 if strings else 2
        out.append((typ, data, strings))
        if typ == 127:
            break
        i = j
    return out


def _s(data, strings, off):
    if off >= len(data):
        return ""
    idx = data[off]
    return _clean(strings[idx - 1]) if 0 < idx <= len(strings) else ""


def _w(data, off):
    return struct.unpack_from("<H", data, off)[0] if off + 2 <= len(data) else 0


MEM_TYPES = {0x12: "DDR", 0x13: "DDR2", 0x18: "DDR3", 0x1A: "DDR4", 0x1B: "LPDDR", 0x1C: "LPDDR2",
             0x1D: "LPDDR3", 0x1E: "LPDDR4", 0x22: "DDR5", 0x23: "LPDDR5"}
FORM_FACTORS = {0x09: "DIMM", 0x0D: "SODIMM", 0x0B: "Row of chips", 0x0F: "FB-DIMM"}


def dmi_memory(structs):
    out = []
    for typ, d, st in structs:
        if typ != 17 or len(d) < 0x15:
            continue
        size = _w(d, 0x0C)
        if size == 0x7FFF and len(d) >= 0x20:
            mib = struct.unpack_from("<I", d, 0x1C)[0] & 0x7FFFFFFF
        elif size in (0, 0xFFFF):
            mib = 0
        else:
            mib = (size & 0x7FFF) // 1024 if size & 0x8000 else size   # bit 15: value is in KiB
        maker = _s(d, st, 0x17)
        if re.fullmatch(r"[0-9A-Fa-f]{4}", maker):   # bare JEDEC id the firmware did not translate
            maker = ""
        speed = _w(d, 0x15)
        conf = _w(d, 0x20)
        out.append({
            "locator": _s(d, st, 0x10), "bank": _s(d, st, 0x11),
            "size_mib": mib,
            "form": FORM_FACTORS.get(d[0x0E], ""),
            "type": MEM_TYPES.get(d[0x12], ""),
            "speed": speed if 0 < speed < 0xFFFF else None,
            "configured_speed": conf if 0 < conf < 0xFFFF else None,
            "manufacturer": maker, "part": _s(d, st, 0x1A),
            "rank": (d[0x1B] & 0x0F) or None if len(d) > 0x1B else None,
        })
    return out


def dmi_slots(structs):
    """Slot designations keyed by PCI address, for slots the firmware describes properly."""
    out = {}
    for typ, d, st in structs:
        if typ != 9 or len(d) < 0x11:
            continue
        seg, bus, devfn = _w(d, 0x0D), d[0x0F], d[0x10]
        if bus in (0, 0xFF):   # unset (0xFF) or the placeholder 00:00.0 some firmware fills in
            continue
        name = _s(d, st, 0x04)
        if name:
            out[f"{seg:04x}:{bus:02x}:{devfn >> 3:02x}"] = name
    return out


def privileged():
    """Called by the helper as root."""
    with open(C.hs("/sys/firmware/dmi/tables/DMI"), "rb") as f:
        structs = smbios_structs(f.read())
    return {"ts": time.time(), "memory": dmi_memory(structs), "slots": dmi_slots(structs)}


# ---------------- pci.ids / usb.ids ----------------

def _ids_file(name):
    for p in (f"/usr/share/hwdata/{name}", f"/usr/share/misc/{name}", f"/usr/share/{name}"):
        txt = read(C.hr(p))
        if txt:
            return txt
    return ""


def lookup_ids(name, wanted, classes=()):
    """Names for the (vendor, device) pairs in `wanted` and the class codes in
    `classes`, from one pass over an ids file. Missing entries are simply absent."""
    vendors, devices, cls = {}, {}, {}
    want_v = {v for v, _ in wanted}
    cur_v = cur_c = None
    in_classes = False
    for line in _ids_file(name).splitlines():
        if not line or line[0] == "#":
            continue
        if line.startswith("C "):
            in_classes, cur_c = True, line[2:4].lower()
            if cur_c in {c[:2] for c in classes}:
                cls[cur_c] = line[6:].strip()
            continue
        if in_classes:
            if line[0] == "\t" and line[1] != "\t" and cur_c:
                code = cur_c + line[1:3].lower()
                if code in classes:
                    cls[code] = line[5:].strip()
            elif line[0] != "\t":
                cur_c = None   # other trailing sections (AT, HID, ...) are not needed
            continue
        if line[0] != "\t":
            cur_v = line[:4].lower()
            if cur_v in want_v:
                vendors[cur_v] = line[6:].strip()
        elif line[1] != "\t" and cur_v in want_v:
            dev = line[1:5].lower()
            if (cur_v, dev) in wanted:
                devices[(cur_v, dev)] = line[7:].strip()
    return vendors, devices, cls


SHORT_VENDOR = (
    (" Corporation", ""), (" Co., Ltd.", ""), (" Co Ltd", ""), (", Inc.", ""), (", Inc", ""), (" Inc.", ""), (" Corp.", ""),
    (" Technology", ""), (" Electronics", ""), (" Semiconductor", ""), (" (Hangzhou) Ltd.", ""), (" Ltd.", ""),
)


def _vendor(v):
    for a, b in SHORT_VENDOR:
        v = v.replace(a, b)
    return v.strip()


# ---------------- unprivileged collectors ----------------

CHASSIS = {3: "Desktop", 4: "Low-profile desktop", 5: "Pizza box", 6: "Mini tower", 7: "Tower", 8: "Portable",
           9: "Laptop", 10: "Notebook", 13: "All-in-one", 17: "Main server chassis", 23: "Rack-mount chassis",
           28: "Blade", 31: "Convertible", 35: "Mini PC", 36: "Stick PC"}


def board():
    base = C.hs("/sys/class/dmi/id")
    g = lambda f: _clean(read1(f"{base}/{f}"))   # noqa: E731
    try:
        chassis = CHASSIS.get(int(read1(f"{base}/chassis_type") or 0), "")
    except ValueError:
        chassis = ""
    return {
        "system": " ".join(x for x in (g("sys_vendor"), g("product_name")) if x),
        "system_version": g("product_version"),
        "board": " ".join(x for x in (g("board_vendor"), g("board_name")) if x),
        "board_version": g("board_version"),
        "bios_vendor": g("bios_vendor"), "bios_version": g("bios_version"),
        "bios_date": g("bios_date"), "bios_release": g("bios_release"),
        "chassis": chassis,
    }


def cpu_extra():
    out = {"arch": os.uname().machine, "microcode": "", "sockets": 1, "caches": []}
    txt = read(C.hp("/proc/cpuinfo")) or ""
    m = re.search(r"^microcode\s*:\s*(\S+)", txt, re.M)
    if m:
        out["microcode"] = m.group(1)
    out["sockets"] = len(set(re.findall(r"^physical id\s*:\s*(\d+)", txt, re.M))) or 1
    base = C.hs("/sys/devices/system/cpu/cpu0/cache")
    for d in listdir(base):
        if not d.startswith("index"):
            continue
        level, typ, size = read1(f"{base}/{d}/level"), read1(f"{base}/{d}/type"), read1(f"{base}/{d}/size")
        if level and size:
            name = f"L{level}" + {"Data": "d", "Instruction": "i"}.get(typ, "")
            out["caches"].append(f"{name} {size}")
    return out


def _hex(path):
    v = read1(path) or ""
    return v[2:].lower() if v.startswith("0x") else v.lower()


def pci_devices(slots):
    """Every PCI function except bridges. `onboard` marks devices on the root
    bus (chipset / CPU integrated); the rest sit behind a PCIe root port: add-in
    cards, M.2 modules, and controllers soldered to the board."""
    base = C.hs("/sys/bus/pci/devices")
    raw = []
    for addr in listdir(base):
        cls = _hex(f"{base}/{addr}/class")   # ccsspp
        if len(cls) != 6 or cls[:2] == "06":
            continue
        try:
            real = os.path.realpath(f"{base}/{addr}")
        except OSError:
            continue
        parents = [p for p in real.split("/")[:-1] if re.fullmatch(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]", p)]
        try:
            driver = os.path.basename(os.readlink(f"{base}/{addr}/driver"))
        except OSError:
            driver = ""
        speed, width = read1(f"{base}/{addr}/current_link_speed") or "", read1(f"{base}/{addr}/current_link_width") or ""
        link = ""
        if parents and speed and width not in ("", "0"):
            link = f"{speed.replace(' PCIe', '').replace('.0 ', ' ')} x{width}"
        raw.append({"addr": addr, "cls": cls[:4], "vendor": _hex(f"{base}/{addr}/vendor"),
                    "device": _hex(f"{base}/{addr}/device"), "driver": driver, "link": link,
                    "onboard": not parents, "slot": slots.get(addr[:-2], "")})
    vendors, devices, classes = lookup_ids("pci.ids", {(d["vendor"], d["device"]) for d in raw}, {d["cls"] for d in raw})
    out = []
    for d in raw:
        v = _vendor(vendors.get(d["vendor"], d["vendor"]))
        name = devices.get((d["vendor"], d["device"]), f"device {d['device']}")
        out.append({"addr": d["addr"], "class": classes.get(d["cls"]) or classes.get(d["cls"][:2]) or f"class {d['cls']}",
                    "class_code": d["cls"], "name": f"{v} {name}", "id": f"{d['vendor']}:{d['device']}",
                    "driver": d["driver"], "link": d["link"], "onboard": d["onboard"], "slot": d["slot"]})
    return out


def net_adapters(pci):
    by_addr = {d["addr"]: d for d in pci}
    base = C.hs("/sys/class/net")
    out = []
    for n in listdir(base):
        dev = f"{base}/{n}/device"
        if not os.path.exists(dev):
            continue   # bridges, taps, veth, lo
        try:
            real = os.path.realpath(dev)
            driver = os.path.basename(os.readlink(f"{dev}/driver"))
        except OSError:
            continue
        addr = os.path.basename(real)
        bus = os.path.basename(os.path.realpath(f"{dev}/subsystem"))
        p = by_addr.get(addr)
        try:
            speed = int(read1(f"{base}/{n}/speed") or -1)
        except ValueError:
            speed = -1
        out.append({"name": n, "bus": bus, "addr": addr if p else "", "model": p["name"] if p else "",
                    "driver": driver, "speed": speed if speed > 0 else None,
                    "wireless": os.path.isdir(f"{base}/{n}/wireless") or os.path.isdir(f"{base}/{n}/phy80211")})
    return out


def usb_devices():
    base = C.hs("/sys/bus/usb/devices")
    raw = []
    for d in listdir(base):
        p = f"{base}/{d}"
        vid, pid = read1(f"{p}/idVendor"), read1(f"{p}/idProduct")
        if not vid or not pid or read1(f"{p}/bDeviceClass") == "09":   # interfaces and hubs
            continue
        raw.append({"port": d, "vid": vid.lower(), "pid": pid.lower(), "speed": read1(f"{p}/speed") or "",
                    "manufacturer": _clean(read1(f"{p}/manufacturer")), "product": _clean(read1(f"{p}/product"))})
    vendors, devices, _ = lookup_ids("usb.ids", {(d["vid"], d["pid"]) for d in raw})
    out = []
    for d in raw:
        v = d["manufacturer"] or _vendor(vendors.get(d["vid"], ""))
        name = d["product"] or devices.get((d["vid"], d["pid"]), "")
        out.append({"port": d["port"], "id": f"{d['vid']}:{d['pid']}", "speed": d["speed"],
                    "name": " ".join(x for x in (v, name) if x) or "Unknown device"})
    return out


_cache = {"at": 0.0, "value": None}


def collect():
    """The /api/hardware payload, cached for a minute."""
    now = time.time()
    if _cache["value"] is not None and now - _cache["at"] < 60:
        return _cache["value"]
    priv = helper_json("hardware.json", 7 * 86400) or {}
    pci = pci_devices(priv.get("slots") or {})
    value = {
        "ts": now,
        "board": board(),
        "cpu": cpu_extra(),
        "memory": priv.get("memory"),   # None: helper not installed, modules unknown
        "pci": pci,
        "net": net_adapters(pci),
        "usb": usb_devices(),
    }
    _cache.update(at=now, value=value)
    return value
