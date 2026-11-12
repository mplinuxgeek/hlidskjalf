"""Static-ish host facts: identity, CPU topology, hwmon sensor discovery."""

import os
import re

from . import config as C
from .util import listdir, parse_range, read, read1, readint


def os_release():
    txt = read(C.hr("/etc/os-release")) or read("/etc/os-release") or ""
    d = {}
    for line in txt.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            d[k] = v.strip().strip('"')
    return d


def hostname():
    h = read1(C.hr("/etc/hostname")) if C.HOST_ROOT else None
    if not h:
        h = read1(C.hp("/proc/sys/kernel/hostname")) or "unknown"
    return h


def cpu_topology():
    """Per logical CPU: core id, package, type (P/E on hybrid Intel)."""
    model = ""
    for line in (read(C.hp("/proc/cpuinfo")) or "").splitlines():
        if line.startswith("model name"):
            model = line.split(":", 1)[1].strip()
            break
    model = re.sub(r"\((R|TM)\)", "", model)
    model = re.sub(r"\s+CPU\s+@.*$", "", model)
    model = re.sub(r"\s+\d+-Core Processor", "", model)
    model = re.sub(r"^\d+(th|st|nd|rd) Gen ", "", model)
    model = re.sub(r"\s+", " ", model).strip()

    online = parse_range(read1(C.hs("/sys/devices/system/cpu/online"), "0"))
    pset = set(parse_range(read1(C.hs("/sys/devices/cpu_core/cpus"), "")))
    eset = set(parse_range(read1(C.hs("/sys/devices/cpu_atom/cpus"), "")))
    hybrid = bool(pset and eset)
    cpus = []
    for n in online:
        base = C.hs(f"/sys/devices/system/cpu/cpu{n}")
        cpus.append({
            "cpu": n,
            "core": readint(f"{base}/topology/core_id", n),
            "pkg": readint(f"{base}/topology/physical_package_id", 0),
            "type": ("P" if n in pset else "E" if n in eset else "") if hybrid else "",
            "max_mhz": (readint(f"{base}/cpufreq/cpuinfo_max_freq", 0) or 0) // 1000,
        })
    cores = {}
    for c in cpus:
        cores.setdefault((c["pkg"], c["core"]), []).append(c["cpu"])
    return {
        "model": model,
        "threads": len(cpus),
        "cores": len(cores),
        "hybrid": hybrid,
        "cpus": cpus,
        "max_mhz": max([c["max_mhz"] for c in cpus] or [0]),
    }


def dmi():
    base = C.hs("/sys/class/dmi/id")
    vendor = read1(f"{base}/sys_vendor") or read1(f"{base}/board_vendor") or ""
    product = read1(f"{base}/product_name") or read1(f"{base}/board_name") or ""
    junk = {"to be filled by o.e.m.", "default string", "system product name", "none"}
    if product.lower() in junk:
        product = read1(f"{base}/board_name") or ""
    if vendor.lower() in junk:
        vendor = read1(f"{base}/board_vendor") or ""
    vendor = VENDORS.get(vendor, vendor)
    return " ".join(x for x in (vendor, product) if x and x.lower() not in junk)


def installed_ram_gib():
    """Installed RAM in whole GiB, from the online memory blocks in sysfs.

    MemTotal is what the kernel can use (installed minus firmware and iGPU
    reservations); the memory blocks cover the physical address ranges, which
    fall a few hundred MiB short of the DIMM total because of address-space
    holes, so the sum is rounded to the nearest GiB. No root needed.
    """
    base = C.hs("/sys/devices/system/memory")
    try:
        block = int(read1(f"{base}/block_size_bytes") or "", 16)
    except ValueError:
        return None
    online = sum(1 for d in listdir(base)
                 if re.fullmatch(r"memory\d+", d) and read1(f"{base}/{d}/online") == "1")
    return round(block * online / 2**30) or None


def natural(s):
    return [int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", s)]


VENDORS = {"Micro Computer (HK) Tech Limited": "Minisforum", "To Be Filled By O.E.M.": "",
           "Dell Inc.": "Dell", "LENOVO": "Lenovo", "Hewlett-Packard": "HP", "HP": "HP",
           "ASUSTeK COMPUTER INC.": "ASUS", "Gigabyte Technology Co., Ltd.": "Gigabyte",
           "Micro-Star International Co., Ltd.": "MSI", "QEMU": "QEMU"}


def _short_model(m):
    m = " " + (m or "") + " "
    for junk in (" SSD ", " M.2 ", " NVMe ", " SAMSUNG "):
        m = m.replace(junk, " ")
    return re.sub(r"\s+", " ", m).strip()


def _lm_chip_name(name, dev):
    """Approximate libsensors chip name (what sadf -m TEMP reports), e.g.
    coretemp-isa-0000, nvme-pci-0100, spd5118-i2c-13-52, acpitz-acpi-0."""
    parts = dev.split("/")
    pci = [p for p in parts if re.fullmatch(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]", p)]
    base = os.path.basename(dev)
    m = re.fullmatch(r"(\d+)-([0-9a-f]{4})", base)
    if m:
        return f"{name}-i2c-{int(m.group(1))}-{int(m.group(2), 16):02x}"
    if name == "acpitz" or base.startswith("LNXTHERM"):
        return f"{name}-acpi-0"
    if pci:
        b, sl_fn = pci[-1].split(":")[1:]
        slot, fn = sl_fn.split(".")
        return f"{name}-pci-{(int(b, 16) << 8) | (int(slot, 16) << 3) | int(fn):04x}"
    return f"{name}-isa-0000"


def discover_sensors():
    """Walk hwmon; return temperature + fan sensors with friendly labels.

    group: cpu | core | nvme | disk | ram | board | gpu | other
    """
    temps, fans = [], []
    root = C.hs("/sys/class/hwmon")
    dimm = 0
    for hw in listdir(root):
        d = f"{root}/{hw}"
        name = read1(f"{d}/name", hw)
        dev = os.path.realpath(f"{d}/device") if os.path.exists(f"{d}/device") else ""
        devbase = os.path.basename(dev)
        model = ""
        if name == "nvme":
            # device -> /sys/.../nvme/nvme0 (controller)
            model = read1(f"{d}/device/model", "")
            ctrl = devbase if devbase.startswith("nvme") else ""
            if not ctrl:
                for x in listdir(f"{d}/device/nvme"):
                    ctrl = x
            devbase = ctrl or devbase
        elif name == "drivetemp":
            model = read1(f"{d}/device/model", "")
            blk = listdir(f"{d}/device/block")
            devbase = blk[0] if blk else devbase
        if name in ("spd5118", "jc42"):
            dimm += 1
        files = listdir(d)
        lm_chip = _lm_chip_name(name, dev)
        temp_idx = sorted(int(m.group(1)) for m in (re.fullmatch(r"temp(\d+)_input", f) for f in files) if m)
        for ordinal, idx in enumerate(temp_idx):
            idx = str(idx)
            raw_label = read1(f"{d}/temp{idx}_label", "") or ""
            crit = readint(f"{d}/temp{idx}_crit")
            mx = readint(f"{d}/temp{idx}_max")
            group, label, primary = "other", f"{name} {raw_label or 'temp' + idx}", True
            if name in ("coretemp",):
                if raw_label.startswith("Package"):
                    group, label = "cpu", "CPU package"
                elif raw_label.startswith("Core"):
                    group, label, primary = "core", raw_label, False
            elif name in ("k10temp", "zenpower"):
                if raw_label in ("Tctl", "Tdie") or not raw_label:
                    group, label = "cpu", "CPU " + (raw_label or "")
                else:
                    group, label, primary = "core", raw_label, False
            elif name == "nvme":
                group = "nvme"
                label = f"{devbase} {_short_model(model)}".strip()
                if raw_label and raw_label != "Composite":
                    label += f" ({raw_label.lower()})"
                    primary = False
            elif name == "drivetemp":
                group, label = "disk", f"{devbase} {_short_model(model)}".strip()
            elif name in ("spd5118", "jc42"):
                group, label = "ram", f"DIMM {dimm}"
            elif name == "acpitz":
                group, label = "board", f"ACPI zone{'' if idx == '1' else ' ' + idx}"
            elif name.startswith("pch_"):
                group, label = "board", "PCH"
            elif name in ("amdgpu", "nouveau", "radeon", "i915", "xe"):
                group, label = "gpu", f"GPU {raw_label or name}"
            elif name.startswith(("nct", "it8", "f71", "w83", "asus", "dell", "thinkpad")):
                group, label = "board", raw_label or f"{name} temp{idx}"
            elif name == "iwlwifi_1" or name.startswith("iwlwifi"):
                group, label = "other", "Wi-Fi"
            elif name in ("r8169_0_100:00", "mt7921_phy0") or "phy" in name or "mdio" in name:
                group, label = "other", f"NIC {raw_label or name}"
            sid = re.sub(r"[^a-z0-9]+", "_", f"{name}_{devbase}_{idx}".lower()).strip("_")
            temps.append({
                "id": sid, "label": label, "group": group, "primary": primary,
                "chip": name, "raw_label": raw_label, "lm": f"{lm_chip}#{ordinal}",
                "path": f"{d}/temp{idx}_input",
                "crit": crit / 1000 if crit and crit < 200000 else None,
                "max": mx / 1000 if mx and mx < 200000 else None,
            })
        for f in files:
            m = re.fullmatch(r"fan(\d+)_input", f)
            if not m:
                continue
            idx = m.group(1)
            raw_label = read1(f"{d}/fan{idx}_label", "") or ""
            sid = re.sub(r"[^a-z0-9]+", "_", f"fan_{name}_{idx}".lower())
            fans.append({"id": sid, "label": raw_label or f"Fan {idx}", "chip": name,
                         "path": f"{d}/fan{idx}_input"})
    order = {"cpu": 0, "nvme": 1, "disk": 2, "ram": 3, "board": 4, "gpu": 5, "other": 6, "core": 7}
    temps.sort(key=lambda s: (order.get(s["group"], 9), not s["primary"], natural(s["label"])))
    return temps, fans


def static_info():
    osr = os_release()
    return {
        "hostname": hostname(),
        "os": osr.get("PRETTY_NAME") or osr.get("NAME", "Linux"),
        "os_id": osr.get("ID", ""),
        "kernel": read1(C.hp("/proc/sys/kernel/osrelease"), ""),
        "machine": dmi(),
        "cpu": cpu_topology(),
        "ram_gib": installed_ram_gib(),
    }
