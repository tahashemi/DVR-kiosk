"""Kiosk power schedule -- e.g. off 19:00-07:00 to save power overnight.
Persisted to disk (atomic write, same pattern as profiles.py) so it survives
power loss / restart."""
import json
import os
import threading
from datetime import datetime, time as dtime

STORE_PATH = "/root/kiosk_schedule.json"
_lock = threading.Lock()

_DEFAULT = {
    "enabled": False,
    "on_time": "07:00",
    "off_time": "19:00",
}


def _load():
    if not os.path.exists(STORE_PATH):
        _save(_DEFAULT)
        return dict(_DEFAULT)
    with open(STORE_PATH) as f:
        d = json.load(f)
    # tolerate a partially-written / older-shape file
    out = dict(_DEFAULT)
    out.update({k: d[k] for k in _DEFAULT if k in d})
    return out


def _save(cfg):
    tmp = STORE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, STORE_PATH)


def get():
    with _lock:
        return _load()


def set(enabled, on_time, off_time):
    """Validates HH:MM and persists. Raises ValueError on bad input."""
    _parse(on_time)
    _parse(off_time)
    cfg = {"enabled": bool(enabled), "on_time": on_time, "off_time": off_time}
    with _lock:
        _save(cfg)
    return cfg


def _parse(hhmm):
    h, m = hhmm.split(":")
    h, m = int(h), int(m)
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"bad time: {hhmm}")
    return dtime(h, m)


def desired_state(cfg, now=None):
    """True if the kiosk should be ON right now per this schedule.

    The on-window is [on_time, off_time). If on_time >= off_time the window
    wraps past midnight (e.g. on=07:00, off=19:00 is the *off* window
    19:00-07:00, so the on-window 07:00-19:00 does NOT wrap; on=19:00,
    off=07:00 WOULD wrap, meaning "on" overnight instead)."""
    if not cfg["enabled"]:
        return True
    now = now or datetime.now()
    t = now.time()
    on_t = _parse(cfg["on_time"])
    off_t = _parse(cfg["off_time"])
    if on_t < off_t:
        return on_t <= t < off_t
    elif on_t > off_t:
        return t >= on_t or t < off_t
    else:
        return True  # on_time == off_time -- degenerate, treat as always-on


def next_transition(cfg, now=None):
    """('on'|'off', "HH:MM") for the UI -- which edge comes next and when."""
    if not cfg["enabled"]:
        return None
    now = now or datetime.now()
    on = desired_state(cfg, now)
    return ("off", cfg["off_time"]) if on else ("on", cfg["on_time"])
