"""Small helpers: file reads, subprocess with timeout, background workers."""

import json
import logging
import os
import subprocess
import threading
import time

log = logging.getLogger("hlidskjalf")


def read(path, default=None):
    try:
        with open(path, "r", errors="replace") as f:
            return f.read()
    except OSError:
        return default


def read1(path, default=None):
    v = read(path)
    return default if v is None else v.strip()


def readint(path, default=None):
    v = read1(path)
    if v is None:
        return default
    try:
        return int(v)
    except ValueError:
        return default


def helper_json(name, max_age):
    """A file written by the privileged helper, or None if missing or older
    than max_age seconds (helper not installed or not running)."""
    from . import config as C
    path = os.path.join(C.HELPER_DIR, name)
    try:
        if time.time() - os.stat(path).st_mtime > max_age:
            return None
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_json_atomic(path, obj):
    """Write JSON via a temp file + rename so readers never see a partial file."""
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, separators=(",", ":"))
    os.replace(tmp, path)


def listdir(path):
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


def run(cmd, timeout=10, env=None):
    """Run a command, return (rc, stdout). Never raises."""
    try:
        p = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, env=env, stdin=subprocess.DEVNULL,
        )
        return p.returncode, p.stdout.decode("utf-8", "replace")
    except FileNotFoundError:
        return 127, ""
    except subprocess.TimeoutExpired:
        log.warning("timeout: %s", " ".join(cmd))
        return 124, ""
    except OSError as e:
        log.warning("run %s: %s", cmd[0], e)
        return 126, ""


def parse_range(s):
    """'0-3,8,10-11' -> [0,1,2,3,8,10,11]"""
    out = []
    for part in (s or "").strip().split(","):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


class Worker(threading.Thread):
    """Calls fn() every `interval` seconds in a daemon thread; keeps the
    latest result (or error) so request handlers never wait on slow work."""

    def __init__(self, name, fn, interval, initial=None, delay=0.0):
        super().__init__(name=name, daemon=True)
        self.fn = fn
        self.interval = interval
        self.value = initial
        self.error = None
        self.updated = 0.0
        self.delay = delay
        self.duration = 0.0
        self._wake = threading.Event()

    def kick(self):
        self._wake.set()

    def run(self):
        if self.delay:
            time.sleep(self.delay)
        while True:
            t0 = time.monotonic()
            try:
                self.value = self.fn()
                self.error = None
            except Exception as e:  # keep running whatever happens
                log.exception("worker %s failed", self.name)
                self.error = f"{type(e).__name__}: {e}"
            self.updated = time.time()
            self.duration = time.monotonic() - t0
            self._wake.wait(max(0.5, self.interval - self.duration))
            self._wake.clear()


def call_with_timeout(fn, timeout, default=None):
    """Run fn in a helper thread; give up after timeout (thread is left to
    finish on its own - used for statvfs on possibly hung NFS mounts)."""
    box = {}

    def target():
        try:
            box["v"] = fn()
        except Exception as e:
            box["e"] = e

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return default, "timeout"
    if "e" in box:
        return default, str(box["e"])
    return box.get("v"), None
