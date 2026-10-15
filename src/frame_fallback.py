"""Bounded go2rtc snapshot fallback.

go2rtc's /api/frame.jpeg starts a fresh ffmpeg decoder for every request
(~17 MB for a substream, far more for a 4 MP mainstream) and only exits once
it has waited for a keyframe. Unbounded, a dashboard full of tiles for a slow
remote DVR stacked up dozens of them and ran the board out of memory.

Rules here: drop rather than wait. A request that can't start right now gets
nothing (the caller serves its cached image or a placeholder); a decode that
runs past its deadline is abandoned, and the ffmpeg behind it is killed by
the reaper. Lost frames are fine; a stuck board is not.
"""
import ipaddress
import os
import signal
import socket
import threading
import time
import urllib.request
from functools import lru_cache

PER_DECODE_MB = 20          # measured ~17 MB RSS per substream ffmpeg
MIN_FREE_MB = 80            # never start a decode below this
LAN_DEADLINE = 4.0
REMOTE_DEADLINE = 7.0       # connect over WAN + wait for a keyframe
RESULT_TTL = 10.0           # reuse a fallback frame instead of decoding again
FAIL_BACKOFF = (10, 20, 40, 60)
REAP_AFTER = 15.0           # kill go2rtc's mjpeg ffmpegs older than this

_lock = threading.Lock()
_running = 0
_inflight = set()
_fails = {}                 # key -> (count, retry_after)
_results = {}               # key -> (jpeg, time)
_override = None            # workers set by config; None = automatic


def set_override(n):
    global _override
    try:
        _override = max(0, int(n)) if n is not None else None
    except (TypeError, ValueError):
        _override = None


def _mem_available_mb():
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return None


def worker_limit():
    """How many decodes may run at once on this board, right now."""
    if _override is not None:
        return _override
    cores = os.cpu_count() or 1
    limit = max(1, cores // 2)
    free = _mem_available_mb()
    if free is not None:
        if free < MIN_FREE_MB:
            return 0
        limit = min(limit, max(1, int(free * 0.25) // PER_DECODE_MB))
    try:
        if os.getloadavg()[0] > cores * 1.5:
            return 0
    except (OSError, AttributeError):
        pass
    return limit


@lru_cache(maxsize=64)
def is_remote(host):
    """True when the source is outside this board's own /24 (or a hostname)."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True
    if ip.is_loopback:
        return False
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((str(ip), 9))          # no packet is sent for UDP connect
        local = ipaddress.ip_address(s.getsockname()[0])
        s.close()
    except OSError:
        return True
    return ipaddress.ip_network(f"{local}/24", strict=False) != \
        ipaddress.ip_network(f"{ip}/24", strict=False)


def cached(key):
    with _lock:
        r = _results.get(key)
    return r[0] if r and time.time() - r[1] < RESULT_TTL else None


def fetch(key, url, remote=False):
    """One JPEG via go2rtc, or None immediately if it can't run now."""
    global _running
    hit = cached(key)
    if hit:
        return hit
    now = time.time()
    with _lock:
        fail = _fails.get(key)
        if fail and now < fail[1]:
            return None
        if key in _inflight or _running >= worker_limit():
            return None
        _inflight.add(key)
        _running += 1
    deadline = now + (REMOTE_DEADLINE if remote else LAN_DEADLINE)
    data = None
    try:
        # go2rtc answers only once the frame is decoded, so the wait is all
        # in the first read; the socket timeout bounds it, and anything that
        # still lingers server-side is the reaper's job.
        with urllib.request.urlopen(url, timeout=deadline - time.time()) as resp:
            data = resp.read()
        if time.time() > deadline + 1:
            data = None           # too late to be worth showing
    except Exception:
        data = None
    finally:
        with _lock:
            _inflight.discard(key)
            _running -= 1
            if data and len(data) > 100:
                _results[key] = (data, time.time())
                _fails.pop(key, None)
            else:
                n = (_fails.get(key, (0, 0))[0]) + 1
                _fails[key] = (n, time.time() + FAIL_BACKOFF[min(n, len(FAIL_BACKOFF)) - 1])
                data = None
    return data


def forget(prefix=""):
    """Drop cached state (e.g. after a source is edited)."""
    with _lock:
        for d in (_fails, _results):
            for k in [k for k in d if k.startswith(prefix)]:
                d.pop(k, None)


# ---- reaper: go2rtc never gives up on a frame.jpeg decode by itself ----

def _proc_comm(pid):
    try:
        with open(f"/proc/{pid}/comm") as f:
            return f.read().strip()
    except OSError:
        return ""


def reap_once():
    """Kill go2rtc's snapshot ffmpegs that outlived REAP_AFTER. Returns count."""
    try:
        with open("/proc/uptime") as f:
            uptime = float(f.read().split()[0])
        hz = os.sysconf("SC_CLK_TCK")
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except (OSError, ValueError, AttributeError):
        return 0
    killed = 0
    for pid in pids:
        try:
            with open(f"/proc/{pid}/stat") as f:
                stat = f.read()
            fields = stat[stat.rindex(")") + 2:].split()
            ppid, start = int(fields[1]), int(fields[19])
            if not _proc_comm(pid).startswith("ffmpeg") or _proc_comm(ppid) != "go2rtc":
                continue
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                args = f.read().split(b"\0")
            if b"mjpeg" not in args:
                continue          # only snapshot conversions, never live streams
            if uptime - start / hz > REAP_AFTER:
                os.kill(int(pid), signal.SIGKILL)
                killed += 1
        except (OSError, ValueError, IndexError):
            continue
    return killed


def _reaper():
    while True:
        time.sleep(3)
        try:
            n = reap_once()
            if n:
                print(f"[frame_fallback] killed {n} stuck snapshot decoder(s)", flush=True)
        except Exception as e:
            print(f"[frame_fallback] reaper: {e}", flush=True)


def start_reaper():
    if os.name != "nt" and os.path.isdir("/proc"):
        threading.Thread(target=_reaper, daemon=True, name="frame-reaper").start()
