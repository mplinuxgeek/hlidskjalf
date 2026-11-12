"""Runtime configuration, all from environment variables.

Host paths are configurable so the same code runs bare-metal (defaults) or in a
container where the host's /proc, /sys and / are bind-mounted elsewhere.
"""

import os


def _env(name, default):
    v = os.environ.get(name)
    return default if v is None or v == "" else v


BIND = _env("HLID_BIND", "0.0.0.0")
PORT = int(_env("HLID_PORT", "8899"))

HOST_PROC = _env("HOST_PROC", "/proc").rstrip("/")
HOST_SYS = _env("HOST_SYS", "/sys").rstrip("/")
# Prefix under which the host's root filesystem is visible ("/" bare-metal).
HOST_ROOT = _env("HOST_ROOT", "/").rstrip("/")
SA_DIR = _env("HLID_SA_DIR", "/var/log/sa")

PODMAN_SOCK = _env("HLID_PODMAN_SOCK", "/run/podman/podman.sock")
LIBVIRT_URI = _env("HLID_LIBVIRT_URI", "qemu:///system")

NUT_HOST = _env("HLID_NUT_HOST", "none")  # "none" or "" = UPS disabled
NUT_PORT = int(_env("HLID_NUT_PORT", "3493"))
NUT_UPS = _env("HLID_NUT_UPS", "ups")  # "" or "auto" = first UPS reported

# Host switcher: "name=url,name=url"; empty = no switcher.
HOSTS = _env("HLID_HOSTS", "")

# Address used to build links to container UIs (defaults to the address the
# browser used to reach the dashboard).
LINK_HOST = _env("HLID_LINK_HOST", "")

SAMPLE_INTERVAL = float(_env("HLID_SAMPLE_INTERVAL", "2"))
LIVE_POINTS = int(_env("HLID_LIVE_POINTS", "300"))  # 10 min at 2 s
SMART_INTERVAL = int(_env("HLID_SMART_INTERVAL", "600"))
ENABLE_SMART = _env("HLID_SMART", "1") != "0"
ENABLE_VMS = _env("HLID_VMS", "1") != "0"
ENABLE_GPU = _env("HLID_GPU", "1") != "0"
# Where small state (GPU history) is persisted; empty = memory only.
DATA_DIR = _env("HLID_DATA_DIR", "")
# Output of the privileged helper (hlidskjalf.helper): podman, SMART and DRM
# client data the unprivileged dashboard cannot read itself.
HELPER_DIR = _env("HLID_HELPER_DIR", "/run/hlidskjalf-helper")


def hp(path):
    """Host /proc path."""
    return HOST_PROC + path[len("/proc"):] if path.startswith("/proc") else path


def hs(path):
    """Host /sys path."""
    return HOST_SYS + path[len("/sys"):] if path.startswith("/sys") else path


def hr(path):
    """Host root path (for files on the host filesystem)."""
    if not HOST_ROOT:
        return path
    return HOST_ROOT + path


def parse_hosts():
    out = []
    for part in HOSTS.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, url = part.split("=", 1)
        out.append({"name": name.strip(), "url": url.strip()})
    return out
