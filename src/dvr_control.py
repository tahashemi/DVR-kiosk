import functools
import json
import os
import secrets
import socket
import ssl
import threading
import time
import urllib.request

from flask import Flask, jsonify, render_template_string, request, Response, make_response
import waitress
from waitress.server import create_server

import dvr_config
import channel_labels
import profiles
import schedule
import wall
import set_password

app = Flask(__name__)

WALL_HTTP_PORT = 8590  # dvrwall loopback live-thumbnail server
GO2RTC_HTTP_PORT = 1984  # go2rtc loopback streaming server

current_mode = "stopped"  # "grid" | "fullscreen" | "stopped"
grid_launch_lock = threading.Lock()
grid_ready_at = 0
active_channels_cache = []
fullscreen_target = None
wall_status_cache = None
scheduler_last_state = None

# ---- Security & Authentication State ----
SESSION_COOKIE_NAME = "dvr_session"
SESSION_DURATION_SEC = 30 * 24 * 3600  # 30 days
sessions_lock = threading.Lock()
active_sessions = {}  # token -> {"user": str, "expires": float, "ip": str}

rate_limit_lock = threading.Lock()
failed_logins = {}  # ip -> [timestamps]
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_WINDOW_SEC = 600  # 10 minutes


def get_client_ip():
    """Extract real client IP handling HAProxy / reverse proxy headers."""
    if request.headers.get("X-Forwarded-For"):
        return request.headers.get("X-Forwarded-For").split(",")[0].strip()
    if request.headers.get("X-Real-IP"):
        return request.headers.get("X-Real-IP").strip()
    return request.remote_addr or "127.0.0.1"


def is_secure_connection():
    """Detect HTTPS directly or via reverse proxy header."""
    proto = request.headers.get("X-Forwarded-Proto", "").lower()
    return request.is_secure or proto == "https"


def is_rate_limited(ip):
    now = time.time()
    with rate_limit_lock:
        attempts = failed_logins.get(ip, [])
        # Filter attempts within window
        attempts = [t for t in attempts if now - t < LOCKOUT_WINDOW_SEC]
        failed_logins[ip] = attempts
        return len(attempts) >= MAX_FAILED_ATTEMPTS


def record_login_attempt(ip, success):
    now = time.time()
    with rate_limit_lock:
        if success:
            failed_logins.pop(ip, None)
        else:
            attempts = failed_logins.get(ip, [])
            attempts.append(now)
            failed_logins[ip] = attempts


def create_session(username, ip):
    token = secrets.token_hex(32)
    expires = time.time() + SESSION_DURATION_SEC
    with sessions_lock:
        active_sessions[token] = {
            "user": username,
            "expires": expires,
            "ip": ip,
            "created": time.time()
        }
    return token


def validate_session(token):
    if not token:
        return None
    now = time.time()
    with sessions_lock:
        sess = active_sessions.get(token)
        if not sess:
            return None
        if now > sess["expires"]:
            active_sessions.pop(token, None)
            return None
        return sess["user"]


def delete_session(token):
    with sessions_lock:
        active_sessions.pop(token, None)


def require_auth(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        token = request.cookies.get(SESSION_COOKIE_NAME)
        if not token and request.headers.get("Authorization"):
            auth_hdr = request.headers.get("Authorization", "")
            if auth_hdr.startswith("Bearer "):
                token = auth_hdr[7:].strip()

        user = validate_session(token)
        if not user:
            if request.path.startswith("/api/"):
                return jsonify({"error": "unauthorized", "login_required": True}), 401
            # Return unauthenticated view for dashboard
            return render_template_string(HTML, authenticated=False, user=None)
        
        request.authenticated_user = user
        return f(*args, **kwargs)
    return decorated


def get_all_active_channels():
    return [{"dvr": k, "ch": c, "mainstream": False} for k, c in dvr_config.all_channels(enabled_only=True)]


def stop_streams():
    """Dashboard 'Turn Off Kiosk': full teardown."""
    try:
        wall.stop()
    except wall.WallError as e:
        print(f"stop_streams: {e}", flush=True)


_reach_cache = {}
_reach_lock = threading.Lock()
REACH_TTL_OK = 30
REACH_TTL_FAIL = 60

# Channel-pool demand tracking: dvrwall decodes 24/7 whatever CHANNELS lists,
# so an "always decode every enabled channel" roster costs one substream's
# worth of CPU per channel regardless of whether the dashboard pool is ever
# open to see it (measured: ~250% of 400% for a 20-channel fleet). Track the
# last time each channel's thumbnail was actually requested so ensure_roster()
# can decode only the on-screen grid plus recently-viewed pool channels.
_channel_demand = {}
_demand_lock = threading.Lock()
CHANNEL_DEMAND_WINDOW_SEC = 90   # generous vs dvrwall's 3s JPEG-encode window
                                 # on purpose -- reconnecting a stream costs
                                 # several seconds, so this avoids thrashing
                                 # connect/disconnect while someone is just
                                 # glancing around the pool intermittently.


def note_channel_demand(dvr_key, ch):
    with _demand_lock:
        _channel_demand[f"{dvr_key}:{ch}"] = time.time()


def dvr_reachable(dvr_key):
    """Cheap TCP reachability probe, cached."""
    now = time.time()
    with _reach_lock:
        cached = _reach_cache.get(dvr_key)
        if cached:
            ok, checked_at = cached
            if now - checked_at < (REACH_TTL_OK if ok else REACH_TTL_FAIL):
                return ok

    d = dvr_config.DVRS.get(dvr_key)
    if not d or not d.get("enabled", True):
        return False
    ok = False
    try:
        ip = d.get("ip")
        port = d.get("port", 3456)
        with socket.create_connection((ip, port), timeout=1.5):
            ok = True
    except OSError:
        ok = False

    with _reach_lock:
        _reach_cache[dvr_key] = (ok, now)
    return ok


def grid_watchdog():
    global wall_status_cache
    wall_was_down = False
    last_roster_refresh = 0.0
    while True:
        try:
            st = wall.status()
        except Exception:
            st = None

        if st is None:
            wall_was_down = True
            wall_status_cache = None
        else:
            wall_status_cache = st
            if wall_was_down:
                wall_was_down = False
                print("grid_watchdog: dvrwall reconnected, reasserting state", flush=True)
                if current_mode == "grid":
                    threading.Thread(target=launch_grid).start()
                elif current_mode == "fullscreen" and fullscreen_target:
                    threading.Thread(
                        target=launch_fullscreen,
                        args=(fullscreen_target["dvr"], fullscreen_target["ch"]),
                    ).start()

        # Periodically re-issue CHANNELS in grid mode so channels the pool
        # has started (or stopped) demanding get connected (or dropped)
        # without needing a full profile switch. A no-op when nothing
        # changed (see ensure_roster()'s docstring).
        if current_mode == "grid" and time.time() - last_roster_refresh > 15:
            last_roster_refresh = time.time()
            try:
                ensure_roster()
            except Exception as e:
                print(f"grid_watchdog: roster refresh failed: {e}", flush=True)

        time.sleep(10)


def get_all_roster_channels():
    """All enabled channels across all DVRs in the system."""
    return [{"dvr": k, "ch": c, "mainstream": False} for k, c in dvr_config.all_channels(enabled_only=True)]


def ensure_roster():
    """Keep dvrwall's decoding roster matched to what's actually needed.

    Grid mode: the on-screen grid's channels, plus any pool channel whose
    thumbnail was actually requested within CHANNEL_DEMAND_WINDOW_SEC (see
    note_channel_demand()) -- not the whole enabled fleet. dvrwall decodes
    24/7 whatever this sends regardless of whether anyone's looking (measured:
    ~250% of 400% CPU for a 20-channel "always decode everything" roster), so
    an idle pool costs nothing and only actually-viewed channels are paid
    for. Fullscreen mode: ONLY that one channel's mainstream -- every
    substream connection is dropped, so CPU/bandwidth actually goes down
    while viewing fullscreen instead of adding a 21st stream on top of the
    other 20 (which is also what made the TV keep showing the wrong stream:
    dvrwall was juggling 21 concurrent connections instead of just switching
    to the one that matters). Calling this with an unchanged channel list is
    a cheap no-op (see roster_set()'s comment in dvrwall.c), so it's safe to
    call it repeatedly to refresh demand -- see grid_watchdog()."""
    if current_mode == "fullscreen" and fullscreen_target:
        all_chans = [{
            "dvr": fullscreen_target["dvr"],
            "ch": fullscreen_target["ch"],
            "mainstream": True,
        }]
    else:
        wanted = set()
        for c in (active_channels_cache or []):
            dvr_key, ch = c.get("dvr"), c.get("ch")
            if dvr_key is not None and ch is not None:
                wanted.add((dvr_key, ch))
        now = time.time()
        with _demand_lock:
            demanded_keys = [k for k, ts in _channel_demand.items() if now - ts < CHANNEL_DEMAND_WINDOW_SEC]
        for key in demanded_keys:
            dvr_key, _, ch_s = key.partition(":")
            try:
                wanted.add((dvr_key, int(ch_s)))
            except ValueError:
                continue
        enabled = set(dvr_config.all_channels(enabled_only=True))
        wanted &= enabled   # never roster a disabled/removed channel
        all_chans = [{"dvr": d, "ch": c, "mainstream": False} for d, c in wanted]
    if all_chans:
        try:
            wall.set_channels(all_chans)
        except wall.WallError as e:
            print(f"ensure_roster: {e}", flush=True)


def launch_grid():
    """(Re)build the hardware kiosk wall grid from the active profile's channels."""
    global current_mode, active_channels_cache, grid_ready_at, fullscreen_target
    with grid_launch_lock:
        try:
            current_mode = "grid"
            fullscreen_target = None
            chans = profiles.get_active_channels()
            # Filter to enabled DVR channels only
            enabled_keys = set(k for k, v in dvr_config.get_dvrs().items() if v.get("enabled", True))
            filtered_chans = [c for c in chans if c.get("dvr") in enabled_keys]
            # active_channels_cache must be set before ensure_roster() -- it's
            # what tells ensure_roster() which channels are on-screen so they
            # get rostered even if nobody has "demanded" them via the pool yet.
            active_channels_cache = list(chans)
            ensure_roster()
            wall.set_layout(filtered_chans if filtered_chans else chans)
            grid_ready_at = time.time()
        except wall.WallError as e:
            print(f"launch_grid: {e}", flush=True)


def launch_fullscreen(dvr, ch):
    """Switch hardware kiosk wall to 1x1 1080p HD mainstream fullscreen on TV."""
    global current_mode, fullscreen_target
    with grid_launch_lock:
        try:
            current_mode = "fullscreen"
            fullscreen_target = {"dvr": dvr, "ch": ch}
            ensure_roster()
            wall.set_fullscreen(dvr, ch, mainstream=True)
        except wall.WallError as e:
            print(f"launch_fullscreen: {e}", flush=True)


def stream_health():
    """Summary of go2rtc / dvrwall's view of every channel in the system.

    Reads wall_status_cache (refreshed every ~10s by grid_watchdog's
    background poll) instead of calling wall.status() directly. This is on
    the hot path of /api/health and /api/kiosk/status, hit by every
    dashboard poll from every open tab -- a synchronous call here used to
    mean each of waitress's worker threads could independently block on the
    compositor's control socket, which is what turned a stuck/dead
    compositor into a fully unresponsive dashboard (see wall.py's module
    docstring)."""
    res = {}
    raw = wall_status_cache
    if not raw or not isinstance(raw, dict):
        return res
    st_map = {}
    for s in raw.get("streams", []):
        name = s.get("name") or s.get("url", "").rsplit("/", 1)[-1]
        st_map[name] = s
    for dvr_key, ch in dvr_config.all_channels(enabled_only=False):
        name = dvr_config.stream_name(dvr_key, ch)
        st = st_map.get(name) or {}
        res[f"{dvr_key}/{ch}"] = {
            "connected": bool(st.get("connected")),
            "have_frame": bool(st.get("have_frame")),
            "age_ms": int(st.get("age_ms", -1)),
        }
    return res


def apply_schedule_state(should_be_on):
    if should_be_on:
        if current_mode == "stopped":
            launch_grid()
    else:
        stop_streams()


def sync_power_schedule():
    """Background loop enforcing the schedule config."""
    global scheduler_last_state
    while True:
        try:
            cfg = schedule.get()
            if cfg.get("enabled", True):
                should_be_on = schedule.desired_state(cfg)
                if scheduler_last_state is None or scheduler_last_state != should_be_on:
                    apply_schedule_state(should_be_on)
                    scheduler_last_state = should_be_on
            else:
                scheduler_last_state = None
        except Exception as e:
            print(f"sync_power_schedule error: {e}", flush=True)
        time.sleep(10)


def get_normalized_cpu_load_pct():
    """Universal normalized CPU utilization across any OS and core count."""
    try:
        load1, _, _ = os.getloadavg()
        cores = os.cpu_count() or 1
        return (load1 / cores) * 100.0
    except Exception:
        return 0.0


def cpu_load_governor():
    """Dynamically scales TV compositor and WebUI polling FPS using a multi-tier ladder."""
    current_target_fps = 15
    while True:
        try:
            load_pct = get_normalized_cpu_load_pct()
            # Multi-tier progressive throttle ladder:
            if load_pct > 92.0:
                new_fps = 2    # Tier 5: Emergency survival mode
            elif load_pct > 88.0:
                new_fps = 5    # Tier 4: Heavy load
            elif load_pct > 80.0:
                new_fps = 8    # Tier 3: High load
            elif load_pct > 70.0:
                new_fps = 12   # Tier 2: Moderate load
            elif load_pct < 60.0:
                new_fps = 20   # Tier 1: Maximum performance
            else:
                new_fps = current_target_fps

            if new_fps != current_target_fps:
                current_target_fps = new_fps
                wall.set_fps(current_target_fps)
        except Exception:
            pass
        time.sleep(8)


def start_background_workers():
    threading.Thread(target=grid_watchdog, daemon=True).start()
    threading.Thread(target=sync_power_schedule, daemon=True).start()
    threading.Thread(target=cpu_load_governor, daemon=True).start()



HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>DVR Kiosk Remote Controller</title>
<script src="/static/Sortable.min.js"></script>
<style>
  :root {
    --bg: #090d16;
    --surface-1: #131a29;
    --surface-2: #1c263b;
    --surface-3: #26334d;
    --accent: #3b82f6;
    --accent-glow: rgba(59, 130, 246, 0.35);
    --text: #f8fafc;
    --text-dim: #94a3b8;
    --line: rgba(255, 255, 255, 0.08);
    --line-bright: rgba(255, 255, 255, 0.18);
    --danger: #ef4444;
    --success: #10b981;
    --warning: #f59e0b;
    --font-main: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    --font-head: "Outfit", -apple-system, sans-serif;
    --tap: 44px;
  }

  * { box-sizing: border-box; margin: 0; padding: 0; user-select: none; -webkit-user-select: none; }
  
  body {
    background: var(--bg); color: var(--text); font-family: var(--font-main);
    display: flex; flex-direction: column; min-height: 100vh; overflow-x: hidden;
  }

  /* Global Status Notification */
  #status {
    position: fixed; top: 12px; right: 12px; background: var(--surface-3); color: #fff;
    padding: 8px 16px; border-radius: 20px; font-size: 13px; font-weight: 500;
    box-shadow: 0 8px 24px rgba(0,0,0,0.5); border: 1px solid var(--line-bright);
    z-index: 5000; opacity: 0; transform: translateY(-10px); transition: all 0.25s ease;
    pointer-events: none;
  }
  #status.show { opacity: 1; transform: translateY(0); }

  /* Login Screen */
  .login-container {
    display: flex; align-items: center; justify-content: center; min-height: 100vh; padding: 20px;
    background: radial-gradient(circle at 50% 30%, #17243c 0%, var(--bg) 70%);
  }
  .login-card {
    background: var(--surface-1); border: 1px solid var(--line-bright); border-radius: 20px;
    padding: 32px 28px; width: 100%; max-width: 380px; box-shadow: 0 24px 60px rgba(0,0,0,0.8);
    display: flex; flex-direction: column; gap: 20px; text-align: center;
  }
  .login-card h1 {
    font-family: var(--font-head); font-size: 24px; font-weight: 700; margin-bottom: 4px;
    background: linear-gradient(135deg, #fff 30%, var(--accent)); -webkit-background-clip: text; -webkit-text-fill-color: transparent;
  }
  .login-card p { font-size: 13px; color: var(--text-dim); }
  .login-field { display: flex; flex-direction: column; gap: 6px; text-align: left; }
  .login-field label { font-size: 12px; font-weight: 600; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.5px; }
  .login-input {
    background: var(--surface-2); border: 1px solid var(--line); color: #fff;
    padding: 10px 14px; border-radius: 10px; font-size: 14px; outline: none; transition: border-color 0.2s ease;
    user-select: auto; -webkit-user-select: auto;
  }
  .login-input:focus { border-color: var(--accent); box-shadow: 0 0 0 2px var(--accent-glow); }
  .login-btn {
    background: var(--accent); color: #fff; border: none; padding: 12px; border-radius: 10px;
    font-size: 14px; font-weight: 700; cursor: pointer; transition: all 0.2s ease; margin-top: 8px;
  }
  .login-btn:hover { filter: brightness(1.1); box-shadow: 0 0 16px var(--accent-glow); }
  .login-btn:active { transform: scale(0.98); }
  .login-notice {
    font-size: 11px; color: var(--text-dim); line-height: 1.4; background: var(--surface-2);
    padding: 10px; border-radius: 8px; border: 1px solid var(--line);
  }

  /* Top Navigation Bar */
  header {
    background: var(--surface-1); border-bottom: 1px solid var(--line);
    padding: 8px 12px; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; z-index: 100;
  }
  h1 { font-family: var(--font-head); font-size: 18px; font-weight: 700; background: linear-gradient(135deg, #fff 30%, var(--text-dim)); -webkit-background-clip: text; -webkit-text-fill-color: transparent; white-space: nowrap; margin-right: 4px; }
  
  #modeBadge {
    padding: 4px 10px; border-radius: 12px; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px;
  }
  #modeBadge.live-grid { background: rgba(16, 185, 129, 0.15); color: var(--success); border: 1px solid rgba(16, 185, 129, 0.3); }
  #modeBadge.live-fullscreen { background: rgba(59, 130, 246, 0.15); color: var(--accent); border: 1px solid rgba(59, 130, 246, 0.3); }
  #modeBadge.live-off { background: rgba(239, 68, 68, 0.15); color: var(--danger); border: 1px solid rgba(239, 68, 68, 0.3); }

  select {
    background: var(--surface-2); color: var(--text); border: 1px solid var(--line);
    padding: 6px 12px; border-radius: 8px; font-size: 13px; font-weight: 500; min-height: 36px; outline: none; flex: 1; max-width: 160px;
  }
  select:focus { border-color: var(--accent); }

  button {
    display: inline-flex; align-items: center; justify-content: center; gap: 6px;
    background: var(--surface-2); color: var(--text); border: 1px solid var(--line);
    padding: 6px 14px; border-radius: 8px; font-size: 13px; font-weight: 600; min-height: 36px;
    cursor: pointer; transition: all 0.15s ease; touch-action: manipulation;
  }
  button:hover { background: var(--surface-3); border-color: var(--line-bright); }
  button:active { transform: scale(0.97); }
  button.primary { background: var(--accent); border-color: var(--accent); color: #fff; box-shadow: 0 0 12px var(--accent-glow); }
  button.primary:hover { filter: brightness(1.1); }
  button.danger { background: rgba(239, 68, 68, 0.15); color: var(--danger); border-color: rgba(239, 68, 68, 0.3); }
  button.danger:hover { background: var(--danger); color: #fff; }
  button.ghost { background: transparent; border-color: transparent; }
  button.ghost:hover { background: var(--surface-2); }
  button.icon { width: 36px; height: 36px; padding: 0; border-radius: 8px; font-size: 16px; }

  #hdrMenu {
    position: absolute; display: none; background: var(--surface-2); border: 1px solid var(--line);
    border-radius: 10px; padding: 6px 0; z-index: 1000; box-shadow: 0 12px 32px rgba(0,0,0,0.5); min-width: 160px; backdrop-filter: blur(8px);
  }
  #hdrMenu div {
    padding: 10px 16px; font-size: 13px; cursor: pointer; transition: background 0.15s ease; display: flex; align-items: center; gap: 8px;
  }
  #hdrMenu div:hover { background: var(--surface-3); }
  #hdrMenu div.disabled { opacity: 0.4; pointer-events: none; }
  #hdrMenu div.danger-item { color: var(--danger); }

  /* Modal Dialogs */
  .modal-overlay {
    position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.75);
    z-index: 2500; display: flex; align-items: center; justify-content: center; padding: 16px; backdrop-filter: blur(4px);
  }
  .modal-card {
    background: var(--surface-1); border: 1px solid var(--line-bright); border-radius: 16px;
    width: 100%; max-width: 520px; max-height: 90vh; overflow-y: auto; box-shadow: 0 20px 50px rgba(0,0,0,0.7);
  }
  .modal-head {
    padding: 16px; border-bottom: 1px solid var(--line); display: flex; align-items: center; justify-content: space-between;
  }
  .modal-head h2 { font-family: var(--font-head); font-size: 16px; font-weight: 700; }
  .modal-body { padding: 16px; display: flex; flex-direction: column; gap: 16px; }
  .modal-sec { background: var(--surface-2); border: 1px solid var(--line); border-radius: 12px; padding: 14px; }
  .modal-sec h3 { font-size: 13px; font-weight: 700; margin-bottom: 10px; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.5px; display: flex; align-items: center; justify-content: space-between; }
  
  .sw-row { display: flex; align-items: center; justify-content: space-between; font-size: 14px; font-weight: 500; margin-bottom: 12px; }
  .time-row { display: flex; gap: 12px; margin-bottom: 12px; }
  .time-row label { flex: 1; display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--text-dim); }
  .time-row input[type="time"] { background: var(--surface-1); color: #fff; border: 1px solid var(--line); padding: 8px; border-radius: 8px; font-size: 14px; outline: none; }

  /* iOS Style Switch */
  .switch {
    position: relative; display: inline-block; width: 44px; height: 24px; flex-shrink: 0;
  }
  .switch input { opacity: 0; width: 0; height: 0; }
  .slider {
    position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0;
    background-color: #334155; transition: .25s ease; border-radius: 24px;
  }
  .slider:before {
    position: absolute; content: ""; height: 18px; width: 18px; left: 3px; bottom: 3px;
    background-color: white; transition: .25s ease; border-radius: 50%;
  }
  input:checked + .slider { background-color: var(--success); }
  input:checked + .slider:before { transform: translateX(20px); }

  .dvr-item {
    display: flex; align-items: center; justify-content: space-between; padding: 10px 12px;
    background: var(--surface-1); border: 1px solid var(--line); border-radius: 8px; margin-bottom: 8px; gap: 10px;
  }
  .dvr-info { display: flex; flex-direction: column; gap: 2px; }
  .dvr-title { font-weight: 700; font-size: 13px; }
  .dvr-sub { font-size: 11px; color: var(--text-dim); }
  .dvr-controls { display: flex; align-items: center; gap: 10px; }

  /* Main Layout */
  main { flex: 1; display: flex; flex-direction: column; gap: 0; overflow: hidden; }
  
  .pane { display: flex; flex-direction: column; min-height: 0; position: relative; }
  #gridPane { flex: 1; min-height: 280px; max-height: 55vh; border-bottom: 1px solid var(--line); background: var(--bg); }
  #poolPane { flex: 1; min-height: 240px; background: var(--surface-1); }

  .pane-head {
    padding: 10px 14px; background: var(--surface-1); border-bottom: 1px solid var(--line);
    font-family: var(--font-head); font-size: 13px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px;
    color: var(--text-dim); display: flex; align-items: center; justify-content: space-between;
  }

  /* Filter Bar & Search */
  .pool-tools {
    padding: 8px 12px; background: var(--surface-2); border-bottom: 1px solid var(--line);
    display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
  }
  .filter-tabs { display: flex; background: var(--surface-1); padding: 2px; border-radius: 8px; border: 1px solid var(--line); }
  .tab-btn {
    padding: 4px 10px; border-radius: 6px; font-size: 12px; font-weight: 600; border: none; background: transparent; color: var(--text-dim); min-height: 28px;
  }
  .tab-btn.active { background: var(--accent); color: #fff; }
  .search-input {
    flex: 1; min-width: 120px; background: var(--surface-1); border: 1px solid var(--line); color: #fff;
    padding: 4px 10px; border-radius: 8px; font-size: 12px; outline: none; min-height: 28px;
    user-select: auto; -webkit-user-select: auto;
  }
  .search-input:focus { border-color: var(--accent); }

  .scroll { flex: 1; overflow-y: auto; padding: 12px; -webkit-overflow-scrolling: touch; }

  /* Tile Grids */
  .kiosk-grid {
    display: grid; grid-template-columns: repeat(auto-fill, minmax(130px, 1fr)); gap: 10px;
  }
  .pool-dvr { margin-bottom: 16px; }
  .pool-dvr h2 { font-size: 13px; font-weight: 700; color: var(--text-dim); margin-bottom: 8px; display: flex; align-items: center; justify-content: space-between; }
  .pool-channels { display: grid; grid-template-columns: repeat(auto-fill, minmax(110px, 1fr)); gap: 8px; }

  /* Tile Styling */
  .tile {
    position: relative; background: var(--surface-2); border: 1px solid var(--line); border-radius: 10px;
    aspect-ratio: 16/9; overflow: hidden; display: flex; align-items: center; justify-content: center;
    box-shadow: 0 4px 12px rgba(0,0,0,0.3); transition: transform 0.15s ease, border-color 0.15s ease;
  }
  .tile:hover { border-color: var(--line-bright); }
  .tile.is-fullscreen { border: 2px solid var(--accent); box-shadow: 0 0 16px var(--accent-glow); }
  .tile.in-profile { opacity: 1; filter: none; }
  .tile.in-profile:hover { opacity: 1; filter: none; }

  /* Drag handle & Touch Safety */
  .dragHandle {
    position: absolute; top: 4px; left: 4px; width: 28px; height: 28px;
    background: rgba(0,0,0,0.65); color: rgba(255,255,255,0.85); border-radius: 6px;
    display: none; align-items: center; justify-content: center; font-size: 14px;
    cursor: grab; z-index: 5; touch-action: none; backdrop-filter: blur(4px);
  }
  
  body.editing-active .dragHandle { display: flex; }

  .tile img.live-thumb { width: 100%; height: 100%; object-fit: cover; pointer-events: none; }
  .tile .offline { font-size: 11px; font-weight: 600; color: var(--danger); text-transform: uppercase; letter-spacing: 0.5px; }

  .tile .label {
    position: absolute; bottom: 0; left: 0; right: 0; background: linear-gradient(transparent, rgba(0,0,0,0.85));
    padding: 12px 6px 4px; font-size: 11px; font-weight: 600; color: #fff; text-shadow: 0 1px 3px rgba(0,0,0,0.8);
    pointer-events: none; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; z-index: 2;
  }

  .tile .menuBtn {
    position: absolute; top: 0; right: 0; width: 32px; height: 32px; display: flex; align-items: center; justify-content: center;
    font-size: 16px; color: rgba(255,255,255,0.85); background: rgba(0,0,0,0.65); border-bottom-left-radius: 8px;
    z-index: 3; touch-action: manipulation; cursor: pointer; backdrop-filter: blur(4px);
  }
  .tile .menuBtn:hover { background: var(--accent); color: #fff; }
  .sortable-ghost { opacity: 0.35; filter: grayscale(1); }

  /* Context Menu */
  #ctxmenu {
    position: fixed; display: none; background: var(--surface-2); border: 1px solid var(--line); border-radius: 10px;
    padding: 6px 0; z-index: 2000; box-shadow: 0 12px 32px rgba(0,0,0,0.6); max-width: 90vw; backdrop-filter: blur(8px);
  }
  #ctxmenu div {
    padding: 12px 18px; cursor: pointer; font-size: 14px; white-space: nowrap; min-height: var(--tap);
    display: flex; align-items: center; transition: background 0.15s ease;
  }
  #ctxmenu div:hover { background: var(--accent); color: #fff; }

  /* Responsive layout */
  @media (min-width: 768px) {
    header { padding: 10px 16px; }
    select { max-width: 220px; }
    main { flex-direction: row; overflow: hidden; height: calc(100vh - 58px); }
    #gridPane { flex: 0 0 50%; max-height: none; height: 100%; border-right: 1px solid var(--line); border-bottom: none; overflow: hidden; }
    #poolPane { flex: 1 1 50%; height: 100%; overflow: hidden; }
    .kiosk-grid { grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 12px; }
    .pool-channels { grid-template-columns: repeat(auto-fill, minmax(120px, 1fr)); gap: 10px; }
  }
</style>
</head>
<body>
  <span id="status"></span>

  {% if not authenticated %}
  <!-- Login Screen -->
  <div class="login-container">
    <div class="login-card">
      <div>
        <h1>DVR KIOSK</h1>
        <p>Enter your credentials to access system controls</p>
      </div>
      <div class="login-field">
        <label>Username</label>
        <input type="text" id="loginUser" class="login-input" placeholder="admin" autocomplete="username">
      </div>
      <div class="login-field">
        <label>Password</label>
        <input type="password" id="loginPass" class="login-input" placeholder="••••••••" autocomplete="current-password">
      </div>
      <button class="login-btn" id="btnLogin">Sign In</button>
    </div>
  </div>

  <script>
    document.getElementById('btnLogin').addEventListener('click', async () => {
      const user = document.getElementById('loginUser').value.trim();
      const pass = document.getElementById('loginPass').value;
      if (!user || !pass) {
        alert('Please enter username and password');
        return;
      }
      try {
        const r = await fetch('/api/login', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({username: user, password: pass})
        });
        const d = await r.json();
        if (r.ok && d.ok) {
          window.location.reload();
        } else {
          alert(d.error || 'Invalid credentials');
        }
      } catch(e) {
        alert('Connection error');
      }
    });
    document.getElementById('loginPass').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') document.getElementById('btnLogin').click();
    });
  </script>

  {% else %}
  <!-- Authenticated Dashboard -->
  <header>
    <h1>DVR Kiosk</h1>
    <span id="modeBadge">...</span>
    <select id="profileSelect"></select>
    <button class="ghost" id="btnEditMode">✏️ Edit Layout</button>
    <button class="primary" id="btnSave" style="display:none;">Save</button>
    <button class="danger" id="btnPower">Turn Off</button>
    <button class="ghost icon" id="btnSettingsGear" title="Settings & DVR Management">⚙️</button>
    <button class="ghost icon" id="btnHdrMenu" aria-label="More options">⋮</button>
  </header>

  <div id="hdrMenu">
    <div id="hmNewProfile">New profile</div>
    <div id="hmRenameProfile">Rename profile</div>
    <div id="hmDeleteProfile" class="danger-item">Delete profile</div>
    <div id="hmExitFullscreen">Exit fullscreen</div>
    <div id="hmLogout" class="danger-item">🚪 Sign Out</div>
  </div>

  <!-- Synchronized Web Fullscreen Overlay (Shared Hardware Decode & Live Stream) -->
  <div id="webFullscreenOverlay" class="modal-overlay" style="display:none; padding:0; background:#000; z-index:4000; width:100vw; height:100vh; position:fixed; top:0; left:0; right:0; bottom:0;">
    <div style="position:relative; width:100%; height:100%; display:flex; align-items:center; justify-content:center; background:#000; overflow:hidden;">
      <div style="position:absolute; top:12px; left:16px; right:16px; display:flex; align-items:center; justify-content:space-between; z-index:50; pointer-events:none;">
        <span id="webFsTitle" style="font-size:15px; font-weight:700; color:#fff; font-family:var(--font-head); text-shadow:0 2px 4px rgba(0,0,0,0.9); background:rgba(0,0,0,0.65); padding:6px 14px; border-radius:8px; backdrop-filter:blur(4px);"></span>
        <button id="btnCloseWebFs" class="ghost icon" style="font-size:24px; color:#fff; width:44px; height:44px; background:rgba(0,0,0,0.7); border-radius:50%; border:1px solid rgba(255,255,255,0.3); backdrop-filter:blur(8px); pointer-events:auto; cursor:pointer;">✕</button>
      </div>
      <video id="webFsVideo" style="width:100%; height:100%; max-width:100vw; max-height:100vh; object-fit:contain; background:#000; aspect-ratio:16/9;" autoplay muted playsinline controls></video>
      <img id="webFsImg" style="display:none; width:100%; height:100%; max-width:100vw; max-height:100vh; object-fit:contain; background:#000; aspect-ratio:16/9;" src="">
    </div>
  </div>

  <!-- Settings & Multi-DVR Modal -->
  <div id="settingsModal" class="modal-overlay" style="display:none;">
    <div class="modal-card">
      <div class="modal-head">
        <h2>Settings & DVR Management</h2>
        <button class="ghost icon" id="btnCloseSettings">✕</button>
      </div>
      <div class="modal-body">
        <!-- DVR Management & Bandwidth Control -->
        <section class="modal-sec">
          <h3>DVR Units & Bandwidth Control</h3>
          <p style="font-size:12px; color:var(--text-dim); margin-bottom:12px;">Toggle remote DVRs OFF on demand to completely stop WAN bandwidth consumption.</p>
          <div id="dvrManagerList"></div>
          
          <div style="margin-top:12px; border-top:1px solid var(--line); padding-top:10px;">
            <button id="btnShowAddDvr" class="ghost" style="width:100%; font-size:12px; border:1px dashed var(--line-bright);">➕ Add New DVR</button>
            <div id="addDvrForm" style="display:none; flex-direction:column; gap:8px; margin-top:10px;">
              <input type="text" id="addDvrKey" placeholder="Identifier key (e.g. dvr3)" class="search-input">
              <input type="text" id="addDvrLabel" placeholder="Display Name (e.g. Warehouse WAN)" class="search-input">
              <div style="display:flex; gap:8px;">
                <input type="text" id="addDvrIp" placeholder="IP / Hostname" class="search-input" style="flex:2;">
                <input type="number" id="addDvrPort" placeholder="Port (3456)" class="search-input" value="3456" style="flex:1;">
                <input type="number" id="addDvrChs" placeholder="Channels (8)" class="search-input" value="8" style="flex:1;">
              </div>
              <input type="password" id="addDvrPass" placeholder="DVR Password (leave blank for site default)" class="search-input">
              <button id="btnSubmitAddDvr" class="primary" style="width:100%;">Save DVR</button>
            </div>
          </div>
        </section>

        <!-- Power Schedule -->
        <section class="modal-sec">
          <h3>Power Schedule</h3>
          <label class="sw-row">
            <span>Enable Automatic Schedule</span>
            <input type="checkbox" id="modalSchedEnabled" style="width:20px; height:20px; accent-color:var(--accent);">
          </label>
          <div class="time-row">
            <label><span>Turn ON Time</span><input type="time" id="modalSchedOn" value="07:00"></label>
            <label><span>Turn OFF Time</span><input type="time" id="modalSchedOff" value="19:00"></label>
          </div>
          <p id="modalSchedNext" style="font-size:12px; color:var(--text-dim); margin:0 0 12px;"></p>
          <button id="btnModalSaveSched" class="primary" style="width:100%;">Save Schedule</button>
        </section>
        
        <!-- Channel Labels -->
        <section class="modal-sec">
          <h3>Camera Channel Names</h3>
          <p style="font-size:12px; color:var(--text-dim); margin:0 0 10px;">Customize labels for cameras across the Web UI.</p>
          <div id="modalChannelNames" style="max-height: 180px; overflow-y: auto; padding-right: 4px; display: flex; flex-direction: column; gap: 8px; margin-bottom: 12px;"></div>
          <button id="btnSaveChannelNames" class="primary" style="width:100%;">Save Camera Names</button>
        </section>
        
        <!-- Security & SSH Management Notice -->
        <div style="font-size:12px; color:var(--text-dim); background:var(--surface-2); border:1px solid var(--line); border-radius:12px; padding:12px; line-height:1.5;">
          🔒 <strong>Security Policy</strong>: Passwords cannot be modified via Web UI. To change credentials or manage users, connect via SSH and type: <code style="background:rgba(0,0,0,0.4); padding:2px 6px; border-radius:4px; color:#fff; font-family:monospace;">dvr-kiosk</code>
        </div>
      </div>
    </div>
  </div>

  <main>
    <div class="pane" id="gridPane">
      <div class="pane-head"><span>Kiosk Grid</span><span style="font-weight:normal; text-transform:none;">Enable Edit Mode to reorder</span></div>
      <div class="scroll"><div class="kiosk-grid" id="kioskGrid"></div></div>
    </div>
    <div class="pane" id="poolPane">
      <div class="pane-head"><span>Channel Pool</span><span style="font-weight:normal; text-transform:none;">Enable Edit Mode to add</span></div>
      <div class="pool-tools">
        <div class="filter-tabs">
          <button class="tab-btn active" data-filter="all">All</button>
          <button class="tab-btn" data-filter="online">Online</button>
          <button class="tab-btn" data-filter="ingrid">In Grid</button>
        </div>
        <input type="search" id="poolSearch" placeholder="Search cameras..." class="search-input">
      </div>
      <div class="scroll"><div id="pool"></div></div>
    </div>
  </main>

  <div id="ctxmenu">
    <div id="ctxRemove">Remove from kiosk</div>
    <div id="ctxFullscreen">Show fullscreen on kiosk</div>
    <div id="ctxRename">Rename camera...</div>
  </div>

<script>
let CHANNELS = [];
let PROFILES = {};
let ACTIVE = null;
let kioskChannels = [];
let kioskMode = null;
let fullscreenTarget = null;
let healthCache = null;
let currentPoolFilter = 'all';
let currentPoolSearch = '';
let editMode = false;

let isWebFullscreenActive = false;
let tilePollInterval = null;

let kioskSortable = null;
let poolSortables = [];

function tileKey(c) { return c.dvr + ':' + c.ch; }

function getTileStatus(dvr, ch) {
  const dvrInfo = (healthCache && healthCache.dvrs) ? healthCache.dvrs[dvr] : null;
  const streamInfo = (healthCache && healthCache.streams) ? healthCache.streams[dvr + '/' + ch] : null;

  const isReachable = dvrInfo ? dvrInfo.reachable : true;
  const isDecoding = streamInfo && streamInfo.connected && streamInfo.have_frame;

  if (!isReachable) {
    return { cls: 'offline', title: 'DVR Unreachable / Offline', online: false };
  }
  if (isDecoding) {
    return { cls: 'online', title: 'Live on TV (' + streamInfo.age_ms + 'ms)', online: true, liveTv: true };
  }
  return { cls: 'online', title: 'Ready (On-Demand)', online: true, liveTv: false };
}

function tileEl(c, inProfile) {
  const div = document.createElement('div');
  const isFs = fullscreenTarget && fullscreenTarget.dvr === c.dvr && fullscreenTarget.ch === c.ch;
  div.className = 'tile' + (inProfile ? ' in-profile' : '') + (isFs ? ' is-fullscreen' : '');
  div.dataset.dvr = c.dvr;
  div.dataset.ch = c.ch;

  const dragHandle = document.createElement('div');
  dragHandle.className = 'dragHandle';
  dragHandle.textContent = '⋮⋮';
  dragHandle.title = 'Drag to reorder or add to grid';

  const img = document.createElement('img');
  img.loading = 'lazy';
  img.className = 'live-thumb';
  img.src = '/api/snapshot/' + c.dvr + '/' + c.ch + '?t=' + Date.now();
  img.onload = () => { img.style.display = ''; offline.style.display = 'none'; };
  img.onerror = () => { offline.style.display = 'block'; };

  const offline = document.createElement('div');
  offline.className = 'offline';
  offline.textContent = 'offline';
  offline.style.display = 'none';

  const label = document.createElement('div');
  label.className = 'label';
  label.textContent = c.label;

  const menuBtn = document.createElement('div');
  menuBtn.className = 'menuBtn';
  menuBtn.textContent = '⋮';
  menuBtn.setAttribute('role', 'button');
  menuBtn.setAttribute('aria-label', 'Channel options: ' + c.label);
  menuBtn.tabIndex = 0;
  menuBtn.addEventListener('click', (e) => showCtxMenu(e, c, inProfile));

  div.appendChild(dragHandle);
  div.appendChild(img);
  div.appendChild(offline);
  div.appendChild(label);
  div.appendChild(menuBtn);

  div.addEventListener('dblclick', () => fullscreen(c.dvr, c.ch));
  div.addEventListener('contextmenu', (e) => showCtxMenu(e, c, inProfile));
  return div;
}

function toggleEditMode(forceState) {
  if (typeof forceState === 'boolean') {
    editMode = forceState;
  } else {
    editMode = !editMode;
  }
  
  const btn = document.getElementById('btnEditMode');
  const btnSave = document.getElementById('btnSave');
  const body = document.body;
  
  if (editMode) {
    btn.textContent = '✓ Done';
    btn.className = 'primary';
    btnSave.style.display = 'inline-flex';
    body.classList.add('editing-active');
  } else {
    btn.textContent = '✏️ Edit Layout';
    btn.className = 'ghost';
    btnSave.style.display = 'none';
    body.classList.remove('editing-active');
  }
  
  refreshSortableStates();
}

function refreshSortableStates() {
  const isEnabled = editMode;
  if (kioskSortable) kioskSortable.option('disabled', !isEnabled);
  for (const s of poolSortables) {
    try { s.option('disabled', !isEnabled); } catch(e) {}
  }
}

document.getElementById('btnEditMode').addEventListener('click', () => toggleEditMode());

let lastPoolRenderSig = null;

function renderPool(force = true) {
  if (isWebFullscreenActive) return; // Background render suspended during fullscreen
  const pool = document.getElementById('pool');
  if (!pool) return;

  const sig = CHANNELS.map(c => tileKey(c) + ':' + (getTileStatus(c.dvr, c.ch).online ? '1' : '0')).join(',') + '|' +
              kioskChannels.map(tileKey).slice().sort().join(',') + '|' +
              currentPoolFilter + '|' + currentPoolSearch;
  if (!force && sig === lastPoolRenderSig) return;
  lastPoolRenderSig = sig;

  pool.innerHTML = '';
  poolSortables = [];
  const byDvr = {};
  for (const c of CHANNELS) {
    (byDvr[c.dvr] = byDvr[c.dvr] || []).push(c);
  }
  const inProfileKeys = new Set(kioskChannels.map(tileKey));
  const dvrKeys = Object.keys(byDvr);

  for (const dvr of dvrKeys) {
    try {
      const section = document.createElement('div');
      section.className = 'pool-dvr';
      
      let onlineCount = 0;
      const matchingChannels = [];

      for (const c of byDvr[dvr]) {
        const st = getTileStatus(c.dvr, c.ch);
        if (st.online) onlineCount++;
        
        const inProfile = inProfileKeys.has(tileKey(c));
        
        if (currentPoolFilter === 'online' && !st.online) continue;
        if (currentPoolFilter === 'ingrid' && !inProfile) continue;
        if (currentPoolSearch) {
          const q = currentPoolSearch.toLowerCase();
          const lbl = (c.label || '').toLowerCase();
          if (!lbl.includes(q)) continue;
        }
        
        matchingChannels.push({ c, inProfile });
      }

      if (matchingChannels.length === 0) continue;

      const header = document.createElement('h2');
      const dvrLabel = (byDvr[dvr][0] && byDvr[dvr][0].dvrLabel) ? byDvr[dvr][0].dvrLabel : dvr.toUpperCase();
      header.innerHTML = `<span>${dvrLabel}</span><span style="font-weight:normal; font-size:11px; opacity:0.8;">${onlineCount}/${byDvr[dvr].length} Online</span>`;

      const grid = document.createElement('div');
      grid.className = 'pool-channels';
      grid.dataset.dvr = dvr;

      for (const item of matchingChannels) {
        grid.appendChild(tileEl(item.c, item.inProfile));
      }

      section.appendChild(header);
      section.appendChild(grid);
      pool.appendChild(section);

      if (typeof Sortable !== 'undefined') {
        const s = new Sortable(grid, {
          group: { name: 'channels', pull: 'clone', put: false },
          sort: false,
          disabled: !editMode,
          animation: 150,
          handle: '.dragHandle',
          filter: '.menuBtn',
          preventOnFilter: false,
          delay: 0,
          touchStartThreshold: 3,
          forceFallback: true,
          fallbackOnBody: true,
          fallbackTolerance: 3,
        });
        poolSortables.push(s);
      }
    } catch (e) {
      console.error('pool section render failed for', dvr, e);
    }
  }
}

let lastKioskGridSig = null;

function renderKioskGrid(force = true) {
  if (isWebFullscreenActive) return; // Background render suspended during fullscreen
  const el = document.getElementById('kioskGrid');
  if (!el) return;

  const sig = kioskChannels.map(tileKey).join(',') + '|' +
              (fullscreenTarget ? tileKey(fullscreenTarget) : '');
  if (!force && sig === lastKioskGridSig) return;
  lastKioskGridSig = sig;

  el.innerHTML = '';
  for (const c of kioskChannels) {
    const full = CHANNELS.find(x => x.dvr === c.dvr && x.ch === c.ch) || c;
    el.appendChild(tileEl(full, true));
  }
  refreshKioskSortable();
}

function refreshKioskSortable() {
  const el = document.getElementById('kioskGrid');
  if (!el || typeof Sortable === 'undefined') return;
  if (kioskSortable) {
    try { kioskSortable.destroy(); } catch(e) {}
  }
  kioskSortable = new Sortable(el, {
    group: 'channels',
    disabled: !editMode,
    animation: 150,
    handle: '.dragHandle',
    filter: '.menuBtn',
    preventOnFilter: false,
    delay: 0,
    touchStartThreshold: 3,
    // Native HTML5 drag-and-drop (Sortable's default) is unreliable inside
    // scrollable containers (both panes here are overflow-y:auto) and on
    // some mobile browsers -- forceFallback makes Sortable simulate the
    // drag itself via pointer events instead of relying on the browser's
    // native DnD, which is the standard fix for "drag just doesn't start."
    // fallbackOnBody moves the drag helper to <body> instead of leaving it
    // inside the scrollable/overflow:hidden container, where it would
    // otherwise get clipped/invisible -- without this, forceFallback alone
    // makes dragging look worse than plain native DnD, not better.
    forceFallback: true,
    fallbackOnBody: true,
    fallbackTolerance: 3,
    onAdd: syncKioskFromDom,
    onSort: syncKioskFromDom,
  });
}

function syncKioskFromDom() {
  const el = document.getElementById('kioskGrid');
  const seen = new Set();
  kioskChannels = Array.from(el.children).map(div => ({
    dvr: div.dataset.dvr, ch: parseInt(div.dataset.ch, 10)
  })).filter(c => {
    const k = tileKey(c);
    if (seen.has(k)) return false;
    seen.add(k);
    return true;
  });
  renderPool();
}

let ctxTarget = null;
function showCtxMenu(e, c, inProfile) {
  e.preventDefault();
  e.stopPropagation();
  ctxTarget = c;
  const menu = document.getElementById('ctxmenu');
  document.getElementById('ctxRemove').style.display = inProfile ? 'block' : 'none';
  const x = Math.min(e.pageX, window.innerWidth - 190);
  const y = Math.min(e.pageY, window.innerHeight - 120);
  menu.style.left = Math.max(4, x) + 'px';
  menu.style.top = Math.max(4, y) + 'px';
  menu.style.display = 'block';
}
document.addEventListener('click', () => { 
  const ctx = document.getElementById('ctxmenu');
  if (ctx) ctx.style.display = 'none'; 
});

document.getElementById('ctxRemove').addEventListener('click', () => {
  kioskChannels = kioskChannels.filter(c => tileKey(c) !== tileKey(ctxTarget));
  renderKioskGrid();
  renderPool();
});
document.getElementById('ctxFullscreen').addEventListener('click', () => {
  fullscreen(ctxTarget.dvr, ctxTarget.ch);
});
document.getElementById('ctxRename').addEventListener('click', async () => {
  if (!ctxTarget) return;
  const current = channelLabel(ctxTarget.dvr, ctxTarget.ch);
  const dvrLabel = ctxTarget.dvrLabel || ctxTarget.dvr.toUpperCase();
  const newName = prompt('New camera name for ' + dvrLabel + ' CH' + ctxTarget.ch + ':', current);
  if (newName === null) return;
  
  const r = await fetch('/api/channels/' + ctxTarget.dvr + '/' + ctxTarget.ch, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({label: newName})
  });
  if (r.ok) {
    setStatus('Renamed camera to ' + (newName.trim() || (dvrLabel + ' CH' + ctxTarget.ch)));
    await loadChannels();
    renderKioskGrid();
    renderPool();
  } else {
    setStatus('Failed to rename camera');
  }
});

let statusHideTimer = null;
function setStatus(msg) {
  const el = document.getElementById('status');
  if (!el) return;
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(statusHideTimer);
  statusHideTimer = setTimeout(() => { el.classList.remove('show'); }, 3000);
}

/* ---- Synchronized Mainstream Fullscreen (Real-Time Zero-Lag Video) ---- */
let fsPollTimer = null;
let currentFsTarget = null;

function refreshFsFrame() {
  if (!isWebFullscreenActive || !currentFsTarget) return;
  const img = document.getElementById('webFsImg');
  if (!img) return;
  const now = Date.now();
  const nextImg = new Image();
  nextImg.onload = () => {
    if (!isWebFullscreenActive) return;
    img.src = nextImg.src;
    clearTimeout(fsPollTimer);
    fsPollTimer = setTimeout(refreshFsFrame, 600);
  };
  nextImg.onerror = () => {
    if (!isWebFullscreenActive) return;
    clearTimeout(fsPollTimer);
    fsPollTimer = setTimeout(refreshFsFrame, 1500);
  };
  nextImg.src = '/api/snapshot/' + currentFsTarget.dvr + '/' + currentFsTarget.ch + '?t=' + now;
}

async function fullscreen(dvr, ch) {
  isWebFullscreenActive = true;
  currentFsTarget = { dvr, ch };
  const label = channelLabel(dvr, ch);

  const overlay = document.getElementById('webFullscreenOverlay');
  const title = document.getElementById('webFsTitle');
  const video = document.getElementById('webFsVideo');
  const img = document.getElementById('webFsImg');

  title.textContent = label + ' (HD Mainstream)';
  overlay.style.display = 'flex';
  video.style.display = 'none';
  img.style.display = 'block';

  // 1. Instant paint fallback preview frame (<50ms)
  img.src = '/api/snapshot/' + dvr + '/' + ch + '?t=' + Date.now();

  // 2. Tell Kiosk Wall to switch hardware TV output to 1080p mainstream immediately
  fetch('/api/kiosk/fullscreen', {
    method: 'POST', 
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({dvr, ch})
  }).then(() => setStatus('Kiosk showing ' + label + ' (HD Mainstream)')).catch(() => {});

  // 3. Start non-buffering real-time frame polling loop (0 lag, <10% CPU)
  clearTimeout(fsPollTimer);
  fsPollTimer = setTimeout(refreshFsFrame, 500);

  await refreshStatus();
}

async function closeWebFullscreen() {
  isWebFullscreenActive = false;
  currentFsTarget = null;
  clearTimeout(fsPollTimer);
  const overlay = document.getElementById('webFullscreenOverlay');
  const video = document.getElementById('webFsVideo');
  const img = document.getElementById('webFsImg');
  img.onerror = null;
  overlay.style.display = 'none';
  video.pause();
  video.src = '';
  img.src = '';
  
  try {
    await fetch('/api/kiosk/grid', {method: 'POST'});
    setStatus('Returned to grid');
  } catch(e) {}
  await refreshStatus();
  refreshAllThumbs();
}

document.getElementById('btnCloseWebFs').addEventListener('click', closeWebFullscreen);
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && document.getElementById('webFullscreenOverlay').style.display !== 'none') {
    closeWebFullscreen();
  }
});

function channelLabel(dvr, ch) {
  const c = CHANNELS.find(x => x.dvr === dvr && x.ch === ch);
  return c ? c.label : (dvr + ' ch' + ch);
}

async function refreshStatus() {
  if (isWebFullscreenActive) return; // Prevent background polling overhead while in fullscreen
  try {
    const hRes = await fetch('/api/health');
    if (hRes.ok) {
      healthCache = await hRes.json();
    }
  } catch(e) {}

  try {
    const r = await fetch('/api/kiosk/status');
    if (r.ok) {
      const data = await r.json();
      kioskMode = data.mode;
      fullscreenTarget = data.fullscreen;

      const badge = document.getElementById('modeBadge');
      if (badge) {
        badge.className = '';
        if (kioskMode === 'grid') {
          badge.textContent = 'Live Grid';
          badge.classList.add('live-grid');
        } else if (kioskMode === 'fullscreen') {
          badge.textContent = 'Fullscreen ' + (fullscreenTarget ? channelLabel(fullscreenTarget.dvr, fullscreenTarget.ch) : '');
          badge.classList.add('live-fullscreen');
        } else {
          badge.textContent = 'Kiosk OFF';
          badge.classList.add('live-off');
        }
      }

      const btnPower = document.getElementById('btnPower');
      if (btnPower) {
        if (kioskMode === 'stopped') {
          btnPower.textContent = 'Turn On';
          btnPower.className = 'primary';
        } else {
          btnPower.textContent = 'Turn Off';
          btnPower.className = 'danger';
        }
      }

      const hmExit = document.getElementById('hmExitFullscreen');
      if (hmExit) {
        hmExit.classList.toggle('disabled', kioskMode !== 'fullscreen');
      }
    }
  } catch(e) {}

  if (!editMode) {
    renderKioskGrid(false);
    renderPool(false);
  }
}

let allThumbsTimer = null;

function refreshAllThumbs() {
  if (isWebFullscreenActive) return;
  const now = Date.now();
  const imgs = document.querySelectorAll('#kioskGrid img.live-thumb, #pool img.live-thumb');
  for (const img of imgs) {
    const tile = img.closest('.tile');
    if (!tile || !tile.dataset.dvr || !tile.dataset.ch) continue;
    img.src = '/api/snapshot/' + tile.dataset.dvr + '/' + tile.dataset.ch + '?t=' + now;
  }
}

function showHdrMenu() {
  const menu = document.getElementById('hdrMenu');
  const btn = document.getElementById('btnHdrMenu');
  const r = btn.getBoundingClientRect();
  const x = Math.min(r.left, window.innerWidth - 200);
  menu.style.left = Math.max(4, x) + 'px';
  menu.style.top = (r.bottom + 4) + 'px';
  menu.style.display = 'block';
}
document.getElementById('btnHdrMenu').addEventListener('click', (e) => {
  e.stopPropagation();
  showHdrMenu();
});
document.addEventListener('click', () => { 
  const hm = document.getElementById('hdrMenu');
  if (hm) hm.style.display = 'none'; 
});

document.getElementById('hmExitFullscreen').addEventListener('click', async () => {
  if (kioskMode !== 'fullscreen') return;
  closeWebFullscreen();
});

document.getElementById('hmLogout').addEventListener('click', async () => {
  await fetch('/api/logout', {method: 'POST'});
  window.location.reload();
});

document.getElementById('btnPower').addEventListener('click', async () => {
  if (kioskMode === 'stopped') {
    await fetch('/api/kiosk/grid', {method: 'POST'});
    setStatus('Kiosk on');
  } else {
    await fetch('/api/kiosk/stop', {method: 'POST'});
    setStatus('Kiosk off');
  }
  await refreshStatus();
});

/* ---- Settings & Multi-DVR Manager Handlers ---- */
document.getElementById('btnSettingsGear').addEventListener('click', async () => {
  await loadSettingsModal();
  document.getElementById('settingsModal').style.display = 'flex';
});

document.getElementById('btnCloseSettings').addEventListener('click', () => {
  document.getElementById('settingsModal').style.display = 'none';
});

document.getElementById('btnShowAddDvr').addEventListener('click', () => {
  const f = document.getElementById('addDvrForm');
  f.style.display = f.style.display === 'none' ? 'flex' : 'none';
});

async function loadSettingsModal() {
  try {
    const r = await fetch('/api/schedule');
    const cfg = await r.json();
    document.getElementById('modalSchedEnabled').checked = cfg.enabled;
    document.getElementById('modalSchedOn').value = cfg.on_time;
    document.getElementById('modalSchedOff').value = cfg.off_time;
  } catch(e) {}

  // Populate DVR Management List
  try {
    const dRes = await fetch('/api/dvr/list');
    const dData = await dRes.json();
    const dvrList = document.getElementById('dvrManagerList');
    dvrList.innerHTML = '';

    for (const [key, d] of Object.entries(dData.dvrs)) {
      const row = document.createElement('div');
      row.className = 'dvr-item';
      row.innerHTML = `
        <div class="dvr-info">
          <div class="dvr-title">${d.label || key.toUpperCase()}</div>
          <div class="dvr-sub">${d.ip}:${d.port} • ${d.channels} Channels</div>
        </div>
        <div class="dvr-controls">
          <label class="switch" title="Toggle DVR ON/OFF (Save WAN Bandwidth)">
            <input type="checkbox" ${d.enabled ? 'checked' : ''} data-dvr="${key}" class="dvr-toggle-input">
            <span class="slider"></span>
          </label>
          <button class="danger icon" style="width:30px; height:30px; font-size:12px;" data-del-dvr="${key}" title="Delete DVR">🗑</button>
        </div>
      `;
      dvrList.appendChild(row);
    }

    // Bind DVR Toggle Events
    document.querySelectorAll('.dvr-toggle-input').forEach(inp => {
      inp.addEventListener('change', async (e) => {
        const dvrKey = e.target.dataset.dvr;
        const enabled = e.target.checked;
        const resp = await fetch('/api/dvr/toggle', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({dvr: dvrKey, enabled: enabled})
        });
        if (resp.ok) {
          setStatus(`${dvrKey.toUpperCase()} ${enabled ? 'Enabled' : 'Disabled (WAN saved)'}`);
          await loadChannels();
          renderKioskGrid();
          renderPool();
        }
      });
    });

    // Bind DVR Delete Events
    document.querySelectorAll('[data-del-dvr]').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        const dvrKey = e.target.dataset.delDvr;
        if (!confirm(`Delete DVR '${dvrKey}'?`)) return;
        const resp = await fetch('/api/dvr/delete', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({dvr: dvrKey})
        });
        if (resp.ok) {
          setStatus(`DVR '${dvrKey}' deleted`);
          await loadSettingsModal();
          await loadChannels();
          renderKioskGrid();
          renderPool();
        }
      });
    });
  } catch(e) {}

  // Populate Channel Names Manager
  const container = document.getElementById('modalChannelNames');
  container.innerHTML = '';
  for (const c of CHANNELS) {
    const row = document.createElement('div');
    row.style.cssText = 'display:flex; align-items:center; justify-content:space-between; gap:10px; font-size:13px;';
    
    const lbl = document.createElement('span');
    lbl.textContent = (c.dvrLabel || c.dvr.toUpperCase()) + ' CH' + c.ch;
    lbl.style.cssText = 'font-weight:600; color:var(--text-dim); min-width:110px;';
    
    const inp = document.createElement('input');
    inp.type = 'text';
    inp.className = 'search-input';
    inp.value = c.customLabel || '';
    inp.placeholder = c.defaultLabel;
    inp.dataset.dvr = c.dvr;
    inp.dataset.ch = c.ch;
    
    row.appendChild(lbl);
    row.appendChild(inp);
    container.appendChild(row);
  }
}

document.getElementById('btnSubmitAddDvr').addEventListener('click', async () => {
  const key = document.getElementById('addDvrKey').value.trim();
  const label = document.getElementById('addDvrLabel').value.trim();
  const ip = document.getElementById('addDvrIp').value.trim();
  const port = parseInt(document.getElementById('addDvrPort').value.trim(), 10) || 3456;
  const channels = parseInt(document.getElementById('addDvrChs').value.trim(), 10) || 8;
  const password = document.getElementById('addDvrPass').value.trim();

  if (!key || !ip) {
    alert('Identifier and IP are required');
    return;
  }

  const r = await fetch('/api/dvr/add', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({dvr: key, label, ip, port, channels, password})
  });
  if (r.ok) {
    setStatus(`DVR '${label || key}' added!`);
    document.getElementById('addDvrForm').style.display = 'none';
    await loadSettingsModal();
    await loadChannels();
    renderKioskGrid();
    renderPool();
  } else {
    alert('Failed to add DVR');
  }
});

const btnSaveChNames = document.getElementById('btnSaveChannelNames');
if (btnSaveChNames) {
  btnSaveChNames.addEventListener('click', async () => {
    const inputs = document.querySelectorAll('#modalChannelNames input');
    const labels = {};
    inputs.forEach(inp => {
      labels[inp.dataset.dvr + ':' + inp.dataset.ch] = inp.value.trim();
    });
    const r = await fetch('/api/channels/labels', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(labels)
    });
    if (r.ok) {
      setStatus('Saved camera names');
      await loadChannels();
      renderKioskGrid();
      renderPool();
    } else {
      setStatus('Failed to save names');
    }
  });
}

document.getElementById('btnModalSaveSched').addEventListener('click', async () => {
  const enabled = document.getElementById('modalSchedEnabled').checked;
  const on_time = document.getElementById('modalSchedOn').value;
  const off_time = document.getElementById('modalSchedOff').value;
  const r = await fetch('/api/schedule', {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({enabled, on_time, off_time})
  });
  if (r.ok) {
    setStatus('Power schedule saved');
    await refreshStatus();
  }
});

// Search and Filter Tab listeners
document.querySelectorAll('.filter-tabs .tab-btn').forEach(btn => {
  btn.addEventListener('click', (e) => {
    document.querySelectorAll('.filter-tabs .tab-btn').forEach(b => b.classList.remove('active'));
    e.target.classList.add('active');
    currentPoolFilter = e.target.dataset.filter;
    renderPool();
  });
});

document.getElementById('poolSearch').addEventListener('input', (e) => {
  currentPoolSearch = e.target.value.trim();
  renderPool();
});

async function loadChannels() {
  const r = await fetch('/api/channels');
  CHANNELS = await r.json();
}

async function loadProfiles() {
  const r = await fetch('/api/profiles');
  const data = await r.json();
  ACTIVE = data.active;
  PROFILES = data.profiles;
  const sel = document.getElementById('profileSelect');
  sel.innerHTML = '';
  for (const [key, name] of Object.entries(PROFILES)) {
    const opt = document.createElement('option');
    opt.value = key; opt.textContent = name;
    if (key === ACTIVE) opt.selected = true;
    sel.appendChild(opt);
  }
}

async function loadActiveProfileChannels() {
  const r = await fetch('/api/profiles/' + ACTIVE);
  const data = await r.json();
  kioskChannels = data.channels;
}

async function init() {
  try {
    await loadChannels();
    await loadProfiles();
    await loadActiveProfileChannels();
    renderKioskGrid();
    renderPool();
  } catch(e) {
    console.error('init load failed:', e);
  }
  try {
    await refreshStatus();
  } catch(e) {}

  setInterval(refreshStatus, 5000);
  clearInterval(allThumbsTimer);
  allThumbsTimer = setInterval(refreshAllThumbs, 1500);
}

document.getElementById('profileSelect').addEventListener('change', async (e) => {
  const key = e.target.value;
  await fetch('/api/profiles/' + key + '/activate', {method: 'POST'});
  ACTIVE = key;
  await loadActiveProfileChannels();
  renderKioskGrid();
  renderPool();
  setStatus('Switched to ' + PROFILES[key]);
  await refreshStatus();
});

document.getElementById('btnSave').addEventListener('click', async () => {
  await fetch('/api/profiles/' + ACTIVE, {
    method: 'PUT', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({channels: kioskChannels})
  });
  await fetch('/api/kiosk/grid', {method: 'POST'});
  toggleEditMode(false);
  setStatus('Saved & applied');
  await refreshStatus();
});

document.getElementById('hmNewProfile').addEventListener('click', async () => {
  const name = prompt('New profile name:');
  if (!name) return;
  const key = name.toLowerCase().replace(/[^a-z0-9]+/g, '_') + '_' + Date.now();
  await fetch('/api/profiles/' + key, {
    method: 'PUT', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({name, channels: kioskChannels})
  });
  await loadProfiles();
  document.getElementById('profileSelect').value = key;
  document.getElementById('profileSelect').dispatchEvent(new Event('change'));
});

document.getElementById('hmRenameProfile').addEventListener('click', async () => {
  const name = prompt('New name:', PROFILES[ACTIVE]);
  if (!name) return;
  await fetch('/api/profiles/' + ACTIVE + '/rename', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({name})
  });
  await loadProfiles();
});

document.getElementById('hmDeleteProfile').addEventListener('click', async () => {
  if (!confirm('Delete profile "' + PROFILES[ACTIVE] + '"?')) return;
  await fetch('/api/profiles/' + ACTIVE, {method: 'DELETE'});
  await loadProfiles();
  await loadActiveProfileChannels();
  renderKioskGrid();
  renderPool();
});

init();
</script>
{% endif %}
</body>
</html>
"""

# ---- Static & Security Middleware ----

@app.after_request
def add_security_headers(resp):
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    resp.headers["X-XSS-Protection"] = "1; mode=block"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return resp


@app.route('/static/Sortable.min.js')
def static_sortable():
    candidates = [
        os.path.join(os.path.dirname(__file__), "static", "Sortable.min.js"),
        os.path.join(os.path.dirname(__file__), "Sortable.min.js"),
        "/opt/dvr-kiosk/src/static/Sortable.min.js",
        "/opt/dvr-kiosk/src/Sortable.min.js",
        "/etc/dvr-kiosk/Sortable.min.js",
    ]
    for js_path in candidates:
        if os.path.exists(js_path):
            with open(js_path, "r", encoding="utf-8") as f:
                return Response(f.read(), mimetype="application/javascript")
    return "not found", 404



# ---- Authentication API Endpoints ----

@app.route('/api/login', methods=['POST'])
def api_login():
    ip = get_client_ip()
    if is_rate_limited(ip):
        return jsonify({"error": "Too many failed attempts. Please wait 10 minutes.", "locked": True}), 429

    body = request.get_json(force=True) or {}
    username = str(body.get("username", "")).strip()
    password = str(body.get("password", ""))

    db = set_password.load_auth_db()
    users = db.get("users", {})

    stored_hash = users.get(username)
    if stored_hash and set_password.verify_password(stored_hash, password):
        record_login_attempt(ip, success=True)
        token = create_session(username, ip)
        
        resp = make_response(jsonify({"ok": True, "user": username}))
        secure_cookie = is_secure_connection()
        resp.set_cookie(
            SESSION_COOKIE_NAME,
            token,
            max_age=SESSION_DURATION_SEC,
            httponly=True,
            samesite="Lax",
            secure=secure_cookie,
            path="/"
        )
        return resp
    else:
        record_login_attempt(ip, success=False)
        return jsonify({"error": "Invalid username or password"}), 401


@app.route('/api/logout', methods=['POST'])
def api_logout():
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        delete_session(token)
    resp = make_response(jsonify({"ok": True}))
    resp.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return resp


@app.route('/api/auth_status')
def api_auth_status():
    token = request.cookies.get(SESSION_COOKIE_NAME)
    user = validate_session(token)
    return jsonify({"authenticated": bool(user), "user": user})


# ---- Application & Dashboard Routes ----

@app.route('/')
def home():
    token = request.cookies.get(SESSION_COOKIE_NAME)
    user = validate_session(token)
    return render_template_string(HTML, authenticated=bool(user), user=user)


# ---- Multi-DVR Management Endpoints ----

@app.route('/api/dvr/list')
@require_auth
def api_dvr_list():
    dvrs = dvr_config.get_dvrs()
    safe_dvrs = {}
    for k, v in dvrs.items():
        safe_dvrs[k] = {
            "label": v.get("label", k),
            "ip": v.get("ip"),
            "port": v.get("port", 3456),
            "channels": v.get("channels", 8),
            "enabled": v.get("enabled", True),
            "reachable": dvr_reachable(k)
        }
    return jsonify({"dvrs": safe_dvrs, "order": dvr_config.get_dvr_order()})


@app.route('/api/dvr/toggle', methods=['POST'])
@require_auth
def api_dvr_toggle():
    body = request.get_json(force=True)
    dvr_key = body.get("dvr")
    enabled = bool(body.get("enabled"))
    ok = dvr_config.toggle_dvr(dvr_key, enabled)
    if ok:
        ensure_roster()
        if current_mode == "grid":
            launch_grid()
        return jsonify({"ok": True})
    return jsonify({"error": "DVR not found"}), 404


@app.route('/api/dvr/add', methods=['POST'])
@require_auth
def api_dvr_add():
    body = request.get_json(force=True)
    key = body.get("dvr", "").strip().lower()
    label = body.get("label", "").strip()
    ip = body.get("ip", "").strip()
    port = body.get("port", 3456)
    channels = body.get("channels", 8)
    password = body.get("password", dvr_config.DEFAULT_DVR_PASS)

    if not key or not ip:
        return jsonify({"error": "Key and IP are required"}), 400

    ok = dvr_config.add_or_update_dvr(key, label, ip, port, channels, password, enabled=True)
    if ok:
        ensure_roster()
        return jsonify({"ok": True})
    return jsonify({"error": "Failed to add DVR"}), 500


@app.route('/api/dvr/delete', methods=['POST'])
@require_auth
def api_dvr_delete():
    body = request.get_json(force=True)
    key = body.get("dvr")
    ok = dvr_config.delete_dvr(key)
    if ok:
        ensure_roster()
        return jsonify({"ok": True})
    return jsonify({"error": "DVR not found"}), 404


# ---- Channel & Stream Endpoints ----

@app.route('/api/channels')
@require_auth
def api_channels():
    out = []
    labels = channel_labels.get_labels()
    dvrs = dvr_config.get_dvrs()
    for dvr_key, ch in dvr_config.all_channels(enabled_only=True):
        default_lbl = dvr_config.channel_label(dvr_key, ch)
        key = f"{dvr_key}:{ch}"
        custom_lbl = labels.get(key, "")
        resolved_lbl = custom_lbl.strip() if custom_lbl.strip() else default_lbl
        out.append({
            "dvr": dvr_key,
            "ch": ch,
            "label": resolved_lbl,
            "defaultLabel": default_lbl,
            "customLabel": custom_lbl,
            "dvrLabel": dvrs.get(dvr_key, {}).get("label", dvr_key.upper()),
        })
    return jsonify(out)


@app.route('/api/channels/<dvr_key>/<int:ch>', methods=['PUT'])
@require_auth
def api_channel_put(dvr_key, ch):
    body = request.get_json(force=True)
    channel_labels.set_label(dvr_key, ch, body.get("label", ""))
    return jsonify({"ok": True})


@app.route('/api/channels/labels', methods=['POST'])
@require_auth
def api_channel_labels_post():
    body = request.get_json(force=True)
    channel_labels.set_labels(body)
    return jsonify({"ok": True})


_SNAPSHOT_CACHE = {}
_CACHE_LOCK = threading.Lock()
SNAPSHOT_TTL_SEC = 2.0


@app.route('/api/snapshot/<dvr_key>/<int:ch>')
@app.route('/api/snapshot/<dvr_key>/<int:ch>.jpg')
@require_auth
def api_snapshot(dvr_key, ch):
    """One-shot JPEG snapshot for a channel, cached in memory for 2.0s to minimize CPU load."""
    if dvr_key not in dvr_config.get_dvrs():
        return "unknown dvr", 404
    if not dvr_reachable(dvr_key):
        return "dvr unreachable", 503

    note_channel_demand(dvr_key, ch)   # keeps this channel in the decode roster
                                        # while the pool is actively viewing it

    key = f"{dvr_key}:{ch}"
    now = time.time()
    with _CACHE_LOCK:
        cached = _SNAPSHOT_CACHE.get(key)
        if cached and (now - cached[1] < SNAPSHOT_TTL_SEC):
            return Response(cached[0], mimetype="image/jpeg")

    name = dvr_config.stream_name(dvr_key, ch, mainstream=False)
    main_name = dvr_config.stream_name(dvr_key, ch, mainstream=True)

    # 1. Fast path: dvrwall shared memory JPEG cache (substream or active mainstream)
    for candidate in (name, main_name):
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{WALL_HTTP_PORT}/jpeg/{candidate}")
            with urllib.request.urlopen(req, timeout=1.2) as resp:
                data = resp.read()
            if data and len(data) > 100:
                with _CACHE_LOCK:
                    _SNAPSHOT_CACHE[key] = (data, now)
                return Response(data, mimetype="image/jpeg")
        except Exception:
            pass

    # 2. Fallback: go2rtc frame API (for pool channels not currently in kiosk grid)
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{GO2RTC_HTTP_PORT}/api/frame.jpeg?src={name}")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = resp.read()
        if data and len(data) > 100:
            with _CACHE_LOCK:
                _SNAPSHOT_CACHE[key] = (data, now)
            return Response(data, mimetype="image/jpeg")
    except Exception:
        pass

    # 3. Last resort fallback: return last known cached snapshot even if slightly older than TTL
    with _CACHE_LOCK:
        cached = _SNAPSHOT_CACHE.get(key)
        if cached and cached[0]:
            return Response(cached[0], mimetype="image/jpeg")

    return "stream not ready", 503


@app.route('/api/stream/<dvr_key>/<int:ch>/main.mp4')
@app.route('/api/stream/<dvr_key>/<int:ch>/main')
@require_auth
def api_stream_main_mp4(dvr_key, ch):
    """Continuous 720p/1080p HD MP4 video feed for WebUI fullscreen modal via go2rtc."""
    if dvr_key not in dvr_config.get_dvrs():
        return "unknown dvr", 404
    if not dvr_reachable(dvr_key):
        return "dvr unreachable", 503
    
    stream_id = dvr_config.stream_name(dvr_key, ch, mainstream=True)
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{GO2RTC_HTTP_PORT}/api/stream.mp4?src={stream_id}")
        upstream = urllib.request.urlopen(req, timeout=12)
        content_type = upstream.headers.get("Content-Type", 'video/mp4; codecs="avc1.420029"')

        def stream():
            try:
                while True:
                    chunk = upstream.read(16384)
                    if not chunk:
                        break
                    yield chunk
            finally:
                upstream.close()

        resp = Response(stream(), mimetype=content_type)
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp
    except Exception as e:
        return str(e), 502


@app.route('/api/stream/<dvr_key>/<int:ch>/main.jpg')
@require_auth
def api_stream_main_jpg(dvr_key, ch):
    """Real-time live 1080p snapshot from dvrwall memory cache (0 lag) or go2rtc."""
    if dvr_key not in dvr_config.get_dvrs():
        return "unknown dvr", 404
    if not dvr_reachable(dvr_key):
        return "dvr unreachable", 503
    
    stream_id = dvr_config.stream_name(dvr_key, ch, mainstream=True)
    # 1. Fast path: dvrwall live memory JPEG cache (0ms lag, matches TV output exactly)
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{WALL_HTTP_PORT}/jpeg/{stream_id}")
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            data = resp.read()
        resp_obj = Response(data, mimetype='image/jpeg')
        resp_obj.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp_obj
    except Exception:
        pass

    # 2. Fast fallback to live substream while mainstream is connecting
    sub_id = dvr_config.stream_name(dvr_key, ch, mainstream=False)
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{WALL_HTTP_PORT}/jpeg/{sub_id}")
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            data = resp.read()
        resp_obj = Response(data, mimetype='image/jpeg')
        resp_obj.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp_obj
    except Exception:
        pass

    # 3. Fallback: go2rtc frame API
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{GO2RTC_HTTP_PORT}/api/frame.jpeg?src={stream_id}")
        with urllib.request.urlopen(req, timeout=3) as upstream:
            data = upstream.read()
        resp = Response(data, mimetype='image/jpeg')
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp
    except Exception as e:
        return str(e), 502


@app.route('/api/live/<dvr_key>/<int:ch>')
@require_auth
def api_live(dvr_key, ch):
    """Continuous MJPEG feed for a channel, proxied from dvrwall."""
    if dvr_key not in dvr_config.get_dvrs():
        return "unknown dvr", 404
    if not dvr_reachable(dvr_key):
        return "dvr unreachable", 503
    main = request.args.get("main", "0") == "1"
    name = dvr_config.stream_name(dvr_key, ch, mainstream=main)
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{WALL_HTTP_PORT}/mjpeg/{name}")
        upstream = urllib.request.urlopen(req, timeout=8)
        content_type = upstream.headers.get("Content-Type", "multipart/x-mixed-replace; boundary=dvrwallframe")

        def stream():
            try:
                while True:
                    chunk = upstream.read(8192)
                    if not chunk:
                        break
                    yield chunk
            finally:
                upstream.close()

        resp = Response(stream(), mimetype=content_type)
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp
    except Exception as e:
        return str(e), 502


@app.route('/api/health')
@require_auth
def api_health():
    dvr_status = {}
    for key, d in dvr_config.get_dvrs().items():
        dvr_status[key] = {
            "label": d.get("label", key),
            "ip": d.get("ip"),
            "enabled": d.get("enabled", True),
            "reachable": dvr_reachable(key)
        }
    
    st_health = stream_health()
    sched_cfg = schedule.get()
    nxt = schedule.next_transition(sched_cfg)
    sched_info = {
        "enabled": sched_cfg.get("enabled", True),
        "on_time": sched_cfg.get("on_time", "07:00"),
        "off_time": sched_cfg.get("off_time", "19:00"),
        "next": {"edge": nxt[0], "time": nxt[1]} if nxt else None
    }
    
    return jsonify({
        "dvrs": dvr_status,
        "streams": st_health,
        "schedule": sched_info
    })


@app.route('/api/profiles')
@require_auth
def api_profiles():
    active, names = profiles.list_profiles()
    return jsonify({"active": active, "profiles": names})


@app.route('/api/profiles/<key>')
@require_auth
def api_profile_get(key):
    p = profiles.get_profile(key)
    if not p:
        return "not found", 404
    return jsonify(p)


@app.route('/api/profiles/<key>', methods=['PUT'])
@require_auth
def api_profile_put(key):
    body = request.get_json(force=True)
    name = body.get("name") or (profiles.get_profile(key) or {}).get("name") or key
    channels = body["channels"]
    profiles.save_profile(key, name, channels)
    return jsonify({"ok": True})


@app.route('/api/profiles/<key>/rename', methods=['POST'])
@require_auth
def api_profile_rename(key):
    body = request.get_json(force=True)
    profiles.rename_profile(key, body["name"])
    return jsonify({"ok": True})


@app.route('/api/profiles/<key>', methods=['DELETE'])
@require_auth
def api_profile_delete(key):
    try:
        profiles.delete_profile(key)
    except ValueError as e:
        return str(e), 400
    return jsonify({"ok": True})


@app.route('/api/profiles/<key>/activate', methods=['POST'])
@require_auth
def api_profile_activate(key):
    try:
        profiles.set_active(key)
    except KeyError:
        return "not found", 404
    threading.Thread(target=launch_grid).start()
    return jsonify({"ok": True})


@app.route('/api/kiosk/grid', methods=['POST'])
@require_auth
def api_kiosk_grid():
    threading.Thread(target=launch_grid).start()
    return jsonify({"ok": True})


@app.route('/api/kiosk/fullscreen', methods=['POST'])
@require_auth
def api_kiosk_fullscreen():
    body = request.get_json(force=True)
    dvr = body.get("dvr")
    ch = int(body.get("ch", 1))
    threading.Thread(target=launch_fullscreen, args=(dvr, ch), daemon=True).start()
    return jsonify({"ok": True})


@app.route('/api/kiosk/stop', methods=['POST'])
@require_auth
def api_kiosk_stop():
    global current_mode, fullscreen_target
    current_mode = "stopped"
    fullscreen_target = None
    stop_streams()
    return jsonify({"ok": True})


@app.route('/api/kiosk/status')
@require_auth
def api_kiosk_status():
    return jsonify({
        "mode": current_mode,
        "fullscreen": fullscreen_target,
        "health": stream_health(),
        "wall": wall_status_cache is not None,
    })


@app.route('/api/schedule')
@require_auth
def api_schedule_get():
    cfg = schedule.get()
    nxt = schedule.next_transition(cfg)
    return jsonify({
        "enabled": cfg["enabled"],
        "on_time": cfg["on_time"],
        "off_time": cfg["off_time"],
        "next": {"edge": nxt[0], "time": nxt[1]} if nxt else None,
    })


@app.route('/api/schedule', methods=['PUT'])
@require_auth
def api_schedule_put():
    global scheduler_last_state
    body = request.get_json(force=True)
    try:
        cfg = schedule.set(
            bool(body.get("enabled")),
            body["on_time"],
            body["off_time"],
        )
    except (ValueError, KeyError) as e:
        return str(e), 400

    want_on = schedule.desired_state(cfg)
    apply_schedule_state(want_on)
    scheduler_last_state = want_on
    return jsonify({"ok": True})


def run_server():
    """Serve the dashboard over plain HTTP on port 80 (TLS offloaded to pfSense / reverse proxy)."""
    print("[*] Starting lightweight plain HTTP server on port 80 (TLS offloaded to pfSense)...", flush=True)
    try:
        waitress.serve(app, host='0.0.0.0', port=80, threads=16)
    except Exception as e:
        print(f"[!] Failed to bind port 80 ({e}), falling back to port 8080...", flush=True)
        waitress.serve(app, host='0.0.0.0', port=8080, threads=16)


if __name__ == '__main__':
    start_background_workers()
    _sched_cfg = schedule.get()
    scheduler_last_state = schedule.desired_state(_sched_cfg)
    apply_schedule_state(scheduler_last_state)

    run_server()


