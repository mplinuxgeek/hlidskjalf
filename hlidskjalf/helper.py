"""Privileged one-shot collector, run as root by hlidskjalf-helper.timer.

It gathers the data the unprivileged dashboard is not allowed to read and
writes it as JSON into HLID_HELPER_DIR (/run/hlidskjalf-helper, group
hlidskjalf, mode 0750):

  podman.json       container list, inspect data, images, host-network ports
                    (podman API socket + /proc/<pid>/fd)
  gpu-clients.json  DRM fdinfo counters per client (other users' fdinfo)
  smart.json        smartctl results, refreshed every HLID_SMART_INTERVAL
  hardware.json     memory modules and slot names from the SMBIOS table,
                    refreshed hourly

It takes no input from the dashboard: no sockets, no arguments, no files read
from the output directory except its own previous podman.json / smart.json
(owned by root, not writable by the dashboard). A compromised dashboard can
therefore only read what this writes, never make it do anything.

Usage: python3 -m hlidskjalf.helper
"""

import logging
import os
import sys
import time

from . import config as C
from .containers import _listening_tcp, cgroup_dir, listening_ports, parse_inspect, podman_get
from .gpu import read_drm_clients, scan_drm_fds
from .storage import Storage
from .util import log, write_json_atomic


def _prev(name):
    import json
    try:
        with open(os.path.join(C.HELPER_DIR, name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def podman():
    if not os.path.exists(C.PODMAN_SOCK):
        return
    prev = _prev("podman.json")
    prev_started = prev.get("started") or {}
    prev_inspect = prev.get("inspect") or {}
    try:
        lst = podman_get("/containers/json?all=true")
    except (ConnectionRefusedError, FileNotFoundError):
        # A socket file left behind while podman.socket is stopped: podman is
        # simply not in use here, which is not a failure of the helper.
        log.info("podman: socket %s is not listening", C.PODMAN_SOCK)
        return
    images = podman_get("/images/json?all=true", timeout=10)
    inspect, started, host_ports = {}, {}, {}
    listen = None
    for c in lst:
        cid = c.get("Id", "")
        st = c.get("StartedAt") or 0
        started[cid] = st
        # Inspect only changes on restart, except the health fields; refresh
        # running containers that have a health check, reuse the rest.
        if prev_started.get(cid) == st and cid in prev_inspect and \
                c.get("Status") not in ("healthy", "unhealthy", "starting"):
            inspect[cid] = prev_inspect[cid]
        else:
            try:
                inspect[cid] = parse_inspect(podman_get(f"/containers/{cid}/json"))
            except Exception as e:
                log.warning("inspect %s: %s", cid[:12], e)
                continue
        pid = c.get("Pid") or 0
        if c.get("State") == "running" and pid and inspect[cid].get("netmode") == "host" \
                or (c.get("State") == "running" and pid and not c.get("Networks") and not c.get("Ports")):
            cg = cgroup_dir(pid)
            if cg:
                if listen is None:
                    listen = _listening_tcp()
                host_ports[cid] = listening_ports(cg, listen)
    keep = ("Id", "Names", "Labels", "StartedAt", "State", "Status", "Pid", "Image", "ImageID",
            "Ports", "Networks", "ExitedAt", "ExitCode", "Restarts", "PodName")
    images_keep = ("Id", "RepoTags", "RepoDigests", "Labels")
    write_json_atomic(os.path.join(C.HELPER_DIR, "podman.json"), {
        "ts": time.time(),
        "containers": [{k: c.get(k) for k in keep} for c in lst],
        "images": [{k: im.get(k) for k in images_keep} for im in images],
        "inspect": inspect, "started": started, "host_ports": host_ports,
    })


def gpu_clients():
    seen, capacity = read_drm_clients(scan_drm_fds())
    write_json_atomic(os.path.join(C.HELPER_DIR, "gpu-clients.json"), {
        "ts": time.time(),
        "capacity": capacity,
        "clients": [{"pdev": k[0], "id": k[1], "pid": int(pid), "vals": vals, "totals": totals}
                    for k, (pid, vals, totals) in seen.items()],
    })


def smart():
    path = os.path.join(C.HELPER_DIR, "smart.json")
    try:
        if time.time() - os.stat(path).st_mtime < C.SMART_INTERVAL - 30:
            return
    except OSError:
        pass
    st = Storage()
    st.smart = _prev("smart.json").get("disks") or {}  # keeps data for disks in standby
    st.smart_scan()
    write_json_atomic(path, {"ts": time.time(), "disks": st.smart})


def hardware():
    path = os.path.join(C.HELPER_DIR, "hardware.json")
    try:
        if time.time() - os.stat(path).st_mtime < 3600:
            return
    except OSError:
        pass
    from .hardware import privileged
    write_json_atomic(path, privileged())


def main():
    logging.basicConfig(level=os.environ.get("HLID_LOG", "WARNING"),
                        format="%(levelname)s %(name)s: %(message)s")
    if os.geteuid() != 0:
        sys.exit("hlidskjalf.helper must run as root (see deploy/hlidskjalf-helper.service)")
    os.makedirs(C.HELPER_DIR, mode=0o750, exist_ok=True)
    os.umask(0o027)
    rc = 0
    for name, fn, enabled in (("podman", podman, True), ("gpu", gpu_clients, C.ENABLE_GPU),
                              ("smart", smart, C.ENABLE_SMART), ("hardware", hardware, True)):
        if not enabled:
            continue
        try:
            fn()
        except Exception as e:
            log.warning("%s: %s", name, e)
            rc = 1
    sys.exit(rc)


if __name__ == "__main__":
    main()
