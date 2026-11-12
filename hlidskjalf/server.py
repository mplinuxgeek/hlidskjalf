"""HTTP server: static page + JSON endpoints. All data comes from caches kept
fresh by background workers, so handlers return immediately."""

import gzip
import json
import logging
import mimetypes
import os
import resource
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import __version__
from . import config as C
from . import hardware
from .containers import Containers
from .history import RANGES, History
from .sampler import Sampler
from .storage import Storage
from .sysinfo import static_info
from .ups import UPS
from .util import Worker, log, read1

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
STARTED = time.time()


class SelfStats:
    """CPU% of this process (+ its subprocesses) averaged over >=10 s windows."""

    def __init__(self):
        self.prev = None
        self.pct = None

    def get(self):
        ru = resource.getrusage(resource.RUSAGE_SELF)
        ch = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu = ru.ru_utime + ru.ru_stime + ch.ru_utime + ch.ru_stime
        now = time.monotonic()
        if self.prev is None:
            self.prev = (now, cpu)
        elif now - self.prev[0] >= 10:
            self.pct = round(100 * (cpu - self.prev[1]) / (now - self.prev[0]), 2)
            self.prev = (now, cpu)
        rss = None
        for line in (read1("/proc/self/status", "") or "").splitlines():
            if line.startswith("VmRSS:"):
                rss = int(line.split()[1]) * 1024
        return {"cpu_pct": self.pct, "rss": rss, "uptime": time.time() - STARTED,
                "cpu_total_s": round(cpu, 1)}


class App:
    def __init__(self):
        self.info = static_info()
        self.sampler = Sampler(self.info["cpu"])
        self.ct = Containers(self.sampler)
        from .vms import VMs
        self.vm = VMs()
        self.st = Storage()
        self.ups = UPS()
        self.hist = History(self.sampler)
        self.selfstats = SelfStats()
        self.w_ct = Worker("containers", self.ct.safe_collect, 5)
        self.w_vm = Worker("vms", self.vm.safe_collect, 10, delay=0.5)
        self.w_st = Worker("storage", self.st.collect, 30, delay=1)
        self.w_smart = Worker("smart", self.st.smart_scan if C.ENABLE_SMART else (lambda: None),
                              C.SMART_INTERVAL if os.geteuid() == 0 else 60, delay=3)
        self.w_ups = Worker("ups", self.ups.collect, 10)
        self.w_hist = Worker("history", self.hist.refresh, 60, delay=4)
        self.gpu_cache = {}
        self.w_ghist = Worker("gpu-history", self._gpu_hist, 60, delay=6)
        self.workers = [self.w_ct, self.w_vm, self.w_st, self.w_smart, self.w_ups, self.w_hist, self.w_ghist]

    def _gpu_hist(self):
        gh = self.sampler.gpu_hist
        for rng, (span, step) in RANGES.items():
            body = json.dumps(gh.build(span, step), separators=(",", ":")).encode()
            self.gpu_cache[rng] = gzip.compress(body, 5)
        return len(gh.rows)

    def start(self):
        self.sampler.start()
        for w in self.workers:
            w.start()

    def info_payload(self):
        i = dict(self.info)
        i["uptime"] = float((read1(C.hp("/proc/uptime"), "0") or "0").split()[0])
        i["boot"] = time.time() - i["uptime"]
        i["version"] = __version__
        i["hosts"] = C.parse_hosts()
        i["link_host"] = C.LINK_HOST
        i["sensors"] = [{k: s[k] for k in ("id", "label", "group", "primary", "crit", "max", "lm")}
                        for s in self.sampler.sensors]
        i["fans"] = [{k: f[k] for k in ("id", "label")} for f in self.sampler.fans]
        i["ranges"] = list(RANGES)
        i["sample_interval"] = C.SAMPLE_INTERVAL
        i["gpu"] = self.sampler.gpu.static() if self.sampler.gpu else {"available": False, "error": "disabled"}
        i["power"] = self.sampler.rapl.labels()
        i["default_iface"] = Storage.default_iface()  # lets the UI label the network tile before the first snapshot
        return i

    def snapshot(self):
        st = self.w_st.value or {}
        return {
            "ts": time.time(),
            "uptime": float((read1(C.hp("/proc/uptime"), "0") or "0").split()[0]),
            "containers": self.w_ct.value or {"available": False, "error": "loading", "containers": []},
            "vms": self.w_vm.value or {"available": False, "error": "loading", "vms": []},
            "storage": st,
            "smart": self.st.smart_snapshot(),
            "ups": self.w_ups.value or {"available": False, "error": "loading"},
            "history_ready": self.hist.ready,
            "self": self.selfstats.get(),
            "workers": {w.name: {"updated": w.updated, "ms": round(w.duration * 1000), "error": w.error}
                        for w in self.workers},
        }


APP: App = None


class Handler(BaseHTTPRequestHandler):
    server_version = "hlidskjalf"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 30  # idle keep-alive / slow clients cannot hold a thread forever

    def log_message(self, fmt, *args):
        if os.environ.get("HLID_ACCESS_LOG"):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body, ctype="application/json", gz_ok=True, cache="no-store", pre_gz=False):
        if isinstance(body, str):
            body = body.encode()
        accept_gz = "gzip" in (self.headers.get("Accept-Encoding") or "")
        enc = None
        if pre_gz:
            if accept_gz:
                enc = "gzip"
            else:
                body = gzip.decompress(body)
        elif gz_ok and accept_gz and len(body) > 1400:
            body = gzip.compress(body, 5)
            enc = "gzip"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                         "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        if enc:
            self.send_header("Content-Encoding", enc)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj):
        self._send(200, json.dumps(obj, separators=(",", ":"), default=str))

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = u.path
        try:
            if p in ("/", "/index.html"):
                return self._static("index.html")
            if p.startswith("/static/"):
                return self._static(p[len("/static/"):])
            if p == "/api/info":
                return self._json(APP.info_payload())
            if p == "/api/live":
                since = int((q.get("since") or ["0"])[0] or 0)
                samples = APP.sampler.since(since)
                return self._json({"seq": APP.sampler.seq, "samples": samples})
            if p == "/api/snapshot":
                return self._json(APP.snapshot())
            if p == "/api/history":
                rng = (q.get("range") or ["1h"])[0]
                if rng not in RANGES:
                    return self._send(400, '{"error":"bad range"}')
                data = APP.hist.get(rng)
                if data is None:
                    return self._send(503, json.dumps({"error": APP.hist.error or "history loading"}))
                return self._send(200, data, pre_gz=True)
            if p == "/api/gpu/history":
                rng = (q.get("range") or ["1h"])[0]
                if rng not in RANGES:
                    return self._send(400, '{"error":"bad range"}')
                data = APP.gpu_cache.get(rng)
                if data is None:
                    return self._send(503, '{"error":"gpu history loading"}')
                return self._send(200, data, pre_gz=True)
            if p == "/api/hardware":
                return self._json(hardware.collect())
            if p == "/api/health":
                return self._json({"ok": True, "version": __version__})
            self._send(404, '{"error":"not found"}')
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            log.exception("request %s", p)
            try:
                self._send(500, '{"error":"internal error"}')
            except OSError:
                pass

    def do_POST(self):
        self._send(405, '{"error":"read-only"}')

    do_PUT = do_DELETE = do_PATCH = do_POST

    def _static(self, rel):
        path = os.path.normpath(os.path.join(STATIC, rel))
        if not path.startswith(STATIC + os.sep) or not os.path.isfile(path):
            return self._send(404, "not found", "text/plain")
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if path.endswith(".woff2"):
            ctype = "font/woff2"
        if ctype.startswith("text/") or ctype.endswith(("javascript", "json")):
            ctype += "; charset=utf-8"
        with open(path, "rb") as f:
            body = f.read()
        cache = "public, max-age=86400" if "/vendor/" in path else "no-cache"
        self._send(200, body, ctype, gz_ok=not path.endswith(".woff2"), cache=cache)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32

    def handle_error(self, request, client_address):
        import sys
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return  # client went away; not worth a traceback
        super().handle_error(request, client_address)


def main():
    global APP
    logging.basicConfig(level=os.environ.get("HLID_LOG", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    APP = App()
    APP.start()
    addr = (C.BIND, C.PORT)
    if ":" in C.BIND:
        import socket
        Server.address_family = socket.AF_INET6
    httpd = Server(addr, Handler)

    import signal

    def _term(*_):
        APP.sampler.gpu_hist.save()
        os._exit(0)
    signal.signal(signal.SIGTERM, _term)
    log.info("hlidskjalf %s on http://%s:%d (host %s)", __version__, C.BIND, C.PORT, APP.info["hostname"])
    try:
        httpd.serve_forever(poll_interval=1.0)
    except KeyboardInterrupt:
        pass
