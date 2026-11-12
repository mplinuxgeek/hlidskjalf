"""Rootful podman containers.

Data comes from the podman REST API on the unix socket when this process can
open it (running as root), otherwise from podman.json written by the
privileged helper (hlidskjalf.helper). The socket gives full control of
podman, so the unprivileged dashboard is never given access to it."""

import http.client
import json
import os
import re
import socket
import ssl
import time

from . import config as C
from .util import helper_json, listdir, log, read

API = "/v4.0.0/libpod"


class _UnixConn(http.client.HTTPConnection):
    def __init__(self, path, timeout=5):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self._path)
        self.sock = s


def podman_get(path, timeout=5):
    c = _UnixConn(C.PODMAN_SOCK, timeout)
    try:
        c.request("GET", API + path)
        r = c.getresponse()
        body = r.read()
        if r.status != 200:
            raise RuntimeError(f"podman {path}: HTTP {r.status}")
        return json.loads(body)
    finally:
        c.close()


def _listening_tcp():
    """inode -> (ip, port) for LISTEN sockets in the host netns."""
    out = {}
    for fn, v6 in (("/proc/net/tcp", False), ("/proc/net/tcp6", True)):
        txt = read(C.hp(fn)) or ""
        for line in txt.splitlines()[1:]:
            p = line.split()
            if len(p) < 10 or p[3] != "0A":
                continue
            ip_hex, port_hex = p[1].split(":")
            port = int(port_hex, 16)
            if v6:
                if ip_hex == "0" * 32:
                    ip = "::"
                elif ip_hex == "00000000000000000000000001000000":
                    ip = "::1"
                elif ip_hex.startswith("0000000000000000FFFF0000"):
                    b = bytes.fromhex(ip_hex[24:])[::-1]
                    ip = ".".join(str(x) for x in b)
                else:
                    ip = "v6"
            else:
                b = bytes.fromhex(ip_hex)[::-1]
                ip = ".".join(str(x) for x in b)
            out[p[9]] = (ip, port)
    return out


def _is_loopback(ip):
    return ip.startswith("127.") or ip == "::1"


def cgroup_dir(pid):
    """Host cgroup v2 directory of a process, or None."""
    for line in (read(C.hp(f"/proc/{pid}/cgroup")) or "").splitlines():
        if line.startswith("0::"):
            return C.hs("/sys/fs/cgroup") + line[3:].strip()
    return None


def listening_ports(cgdir, listen):
    """Non-loopback TCP ports listened on by processes in a cgroup. Reading
    other users' /proc/<pid>/fd needs CAP_SYS_PTRACE, so the helper does this
    for the unprivileged dashboard."""
    procs = read(f"{cgdir}/cgroup.procs") or ""
    ports = {}
    for pid in (p for p in procs.split() if p.isdigit()):
        fd_dir = C.hp(f"/proc/{pid}/fd")
        for fd in listdir(fd_dir):
            try:
                link = os.readlink(f"{fd_dir}/{fd}")
            except OSError:
                continue
            if link.startswith("socket:["):
                ino = link[8:-1]
                if ino in listen:
                    ip, port = listen[ino]
                    if not _is_loopback(ip):
                        ports.setdefault(port, ip)
    return [{"port": p, "proto": "tcp", "ip": ip} for p, ip in sorted(ports.items())]


def parse_inspect(d):
    """The fields we use from `GET /containers/{id}/json`."""
    hc = d.get("HostConfig") or {}
    st = d.get("State") or {}
    health = st.get("Health") or st.get("Healthcheck") or {}
    last = ""
    if health.get("Log"):
        last = (health["Log"][-1].get("Output") or "").strip()[:200]
    return {
        "netmode": hc.get("NetworkMode", ""),
        "restart_policy": (hc.get("RestartPolicy") or {}).get("Name", ""),
        "failing_streak": health.get("FailingStreak", 0),
        "health_log": last,
        "image_name": d.get("ImageName", ""),
        "mem_limit": hc.get("Memory") or 0,
    }


class SocketSource:
    """Direct podman API access (needs root)."""

    def __init__(self):
        self.inspect_cache = {}   # id -> (startedAt, info)
        self.images = {}
        self.images_at = 0
        self.ports_cache = {}     # id -> (ts, [ports])
        self.listen = None

    def containers(self):
        self.listen = None
        lst = podman_get("/containers/json?all=true")
        ids = {c.get("Id") for c in lst}
        for k in list(self.inspect_cache):
            if k not in ids:
                del self.inspect_cache[k]
        return lst

    def image_map(self):
        if time.time() - self.images_at >= 120 or not self.images:
            self.images = {im.get("Id", ""): im for im in podman_get("/images/json?all=true", timeout=10)}
            self.images_at = time.time()
        return self.images

    def inspect(self, cid, started):
        c = self.inspect_cache.get(cid)
        if c and c[0] == started:
            return c[1]
        try:
            info = parse_inspect(podman_get(f"/containers/{cid}/json"))
        except Exception as e:
            log.warning("inspect %s: %s", cid[:12], e)
            return {}
        self.inspect_cache[cid] = (started, info)
        return info

    def host_ports(self, cid, cgdir):
        c = self.ports_cache.get(cid)
        if c and time.time() - c[0] < 60:
            return c[1]
        if self.listen is None:
            self.listen = _listening_tcp()
        out = listening_ports(cgdir, self.listen)
        self.ports_cache[cid] = (time.time(), out)
        return out


class HelperSource:
    """podman.json from the privileged helper."""

    def __init__(self, snap):
        self.snap = snap

    def containers(self):
        return self.snap.get("containers") or []

    def image_map(self):
        return {im.get("Id", ""): im for im in self.snap.get("images") or []}

    def inspect(self, cid, started):
        return self.snap.get("inspect", {}).get(cid) or {}

    def host_ports(self, cid, cgdir):
        return [dict(p) for p in self.snap.get("host_ports", {}).get(cid) or []]


class Containers:
    def __init__(self, sampler):
        self.sampler = sampler
        self.socket_src = SocketSource()
        self.probe_cache = {}     # (ip, port) -> (ts, scheme)
        self.error = None

    def _probe(self, ip, port):
        key = (ip, port)
        c = self.probe_cache.get(key)
        if c and time.time() - c[0] < 900:
            return c[1]
        target = "127.0.0.1" if ip in ("0.0.0.0", "::", "", "v6") else ip
        scheme = None
        # TLS first: many HTTPS servers answer plain HTTP with an HTTP 400.
        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with socket.create_connection((target, port), timeout=0.6) as raw:
                raw.settimeout(0.8)
                with ctx.wrap_socket(raw, server_hostname="localhost"):
                    scheme = "https"
        except (OSError, ssl.SSLError, ValueError):
            pass
        if scheme is None:
            try:
                with socket.create_connection((target, port), timeout=0.6) as s:
                    s.settimeout(0.8)
                    s.sendall(b"HEAD / HTTP/1.0\r\nHost: localhost\r\n\r\n")
                    if s.recv(5) == b"HTTP/":
                        scheme = "http"
            except OSError:
                pass
        self.probe_cache[key] = (time.time(), scheme)
        return scheme

    def collect(self, src):
        lst = src.containers()
        imgs = src.image_map()
        targets = {}
        out = []
        for c in lst:
            cid = c.get("Id", "")
            name = (c.get("Names") or [cid[:12]])[0]
            labels = c.get("Labels") or {}
            started = c.get("StartedAt") or 0
            state = c.get("State", "")
            ins = src.inspect(cid, started)
            pid = c.get("Pid") or 0
            cgdir = cgroup_dir(pid) if state == "running" and pid else None
            if cgdir:
                targets[cid] = cgdir

            ref = c.get("Image", "") or ins.get("image_name", "")
            img = imgs.get(c.get("ImageID", "")) or {}
            repo, tag, digest = ref, "", ""
            if "@" in repo:
                repo, digest = repo.split("@", 1)
            m = re.match(r"^(.*?)(?::([^:/]+))?$", repo)
            if m:
                repo, tag = m.group(1), m.group(2) or ""
            if not tag:
                for rt in img.get("RepoTags") or []:
                    if rt.rsplit(":", 1)[0] == repo:
                        tag = rt.rsplit(":", 1)[1]
                        break
            if not digest:
                for rd in img.get("RepoDigests") or []:
                    if "@" in rd:
                        digest = rd.split("@", 1)[1]
                        break
            short_repo = re.sub(r"^(docker\.io/)(library/)?", "", repo)
            version = labels.get("org.opencontainers.image.version") or \
                (img.get("Labels") or {}).get("org.opencontainers.image.version") or ""

            ports = []
            for p in c.get("Ports") or []:
                hp_ = p.get("host_port")
                rng = p.get("range") or 1
                for i in range(rng):
                    ports.append({"port": hp_ + i, "proto": p.get("protocol", "tcp"),
                                  "ip": p.get("host_ip") or "0.0.0.0",
                                  "cport": p.get("container_port", 0) + i})
            host_net = ins.get("netmode") == "host" or (not c.get("Networks") and not ports and state == "running")
            if host_net and state == "running" and cgdir:
                ports = src.host_ports(cid, cgdir)
            for p in ports:
                if p["proto"] == "tcp" and not _is_loopback(p["ip"]):
                    p["scheme"] = self._probe(p["ip"], p["port"])

            exited_at = c.get("ExitedAt") or 0
            out.append({
                "id": cid, "short_id": cid[:12], "name": name,
                "state": state, "status": c.get("Status", ""),
                "health": c.get("Status", "") if c.get("Status") in ("healthy", "unhealthy", "starting") else "",
                "failing_streak": ins.get("failing_streak", 0),
                "health_log": ins.get("health_log", "") if c.get("Status") == "unhealthy" else "",
                "started": started if started > 0 else None,
                "exited": exited_at if exited_at > 0 else None,
                "exit_code": c.get("ExitCode", 0),
                "restarts": c.get("Restarts", 0),
                "image": short_repo, "tag": tag,
                "digest": digest.replace("sha256:", "")[:12] if digest else "",
                "version": version,
                "unit": labels.get("PODMAN_SYSTEMD_UNIT", ""),
                "host_net": host_net,
                "ports": ports,
                "mem_limit": ins.get("mem_limit", 0),
                "pod": c.get("PodName", ""),
            })
        self.sampler.ct_targets = targets
        if getattr(self.sampler, "gpu", None):
            self.sampler.gpu.ct_names = {c["id"]: c["name"] for c in out}
        order = {"running": 0, "paused": 1, "restarting": 1, "created": 2, "exited": 3}
        out.sort(key=lambda c: (order.get(c["state"], 4), c["name"]))
        return {"available": True, "containers": out, "ts": time.time()}

    def safe_collect(self):
        snap = helper_json("podman.json", 60)
        if snap is not None:
            src = HelperSource(snap)
        elif os.access(C.PODMAN_SOCK, os.R_OK | os.W_OK):
            src = self.socket_src
        else:
            return {"available": False, "containers": [],
                    "error": "no podman data: hlidskjalf-helper is not running"}
        try:
            return self.collect(src)
        except Exception as e:
            log.warning("podman: %s", e)
            return {"available": False, "error": str(e), "containers": []}
