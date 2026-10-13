"""Dynamic DVR and stream definitions with persistence, on-demand bandwidth toggle,
and support for both substream (subtype=1) and mainstream (subtype=0)."""

import json
import os
import subprocess
import threading
from urllib.parse import quote, urlsplit, urlunsplit

CONFIG_PATHS = [
    "/etc/dvr-kiosk/dvr_config.json",
    "/root/dvr_config.json",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "dvr_config.json"),
    "dvr_config.json"
]

DEFAULT_DVR_PASS = ""

DEFAULT_DVRS = {
    "dvr1": {"label": "DVR 1", "ip": "192.168.1.100", "port": 3456, "channels": 8, "password": "", "enabled": True},
}

# Source types. "dvrip" is a multi-channel DVR (the original and default, so
# configs written before types existed keep working unchanged). "rtsp" and
# "onvif" are single cameras: they are stored as one-channel entries so every
# other part of the system (profiles, labels, health, snapshots, the wall)
# keeps addressing them as <key>/1 without knowing they are different.
SOURCE_TYPES = ("dvrip", "rtsp", "onvif")

_lock = threading.Lock()

def _get_config_path():
    for p in CONFIG_PATHS:
        if os.path.exists(p):
            return p
    if os.name != 'nt':
        if os.path.exists("/etc/dvr-kiosk"):
            return "/etc/dvr-kiosk/dvr_config.json"
        if os.path.exists("/root"):
            return "/root/dvr_config.json"
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "dvr_config.json")

def load_dvrs():
    path = _get_config_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and "dvrs" in data:
                    return data["dvrs"]
        except Exception as e:
            print(f"[dvr_config] Error loading {path}: {e}")
    # Return default clone
    return {k: dict(v) for k, v in DEFAULT_DVRS.items()}

def save_dvrs(dvrs_dict):
    path = _get_config_path()
    with _lock:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            # Write-then-rename so a crash mid-save can never leave a truncated
            # config (which would silently fall back to DEFAULT_DVRS).
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"dvrs": dvrs_dict}, f, indent=2)
            os.replace(tmp, path)
            sync_go2rtc()
            return True
        except Exception as e:
            print(f"[dvr_config] Error saving {path}: {e}")
            return False

# Global runtime cache
DVRS = load_dvrs()
DVR_ORDER = list(DVRS.keys())

def refresh_cache():
    global DVRS, DVR_ORDER
    DVRS = load_dvrs()
    DVR_ORDER = list(DVRS.keys())

def get_dvrs():
    refresh_cache()
    return DVRS

def get_dvr_order():
    refresh_cache()
    return DVR_ORDER

def stream_name(dvr_key, ch, mainstream=False):
    suffix = "_main" if mainstream else ""
    return f"{dvr_key}_ch{ch}{suffix}"

def source_type(d):
    t = (d or {}).get("type", "dvrip")
    return t if t in SOURCE_TYPES else "dvrip"

def _cred(s):
    """Percent-encode a URL userinfo component, so passwords containing
    @ : / # ? don't break the URL (or the YAML line it ends up on)."""
    return quote(str(s or ""), safe="")

def dvrip_url(dvr_key, ch, subtype=1):
    refresh_cache()
    d = DVRS.get(dvr_key)
    if not d:
        return ""
    pwd = _cred(d.get("password", DEFAULT_DVR_PASS))
    ip = d.get("ip", "127.0.0.1")
    port = d.get("port", 3456)
    return f"dvrip://admin:{pwd}@{ip}:{port}?channel={ch - 1}&subtype={subtype}"

def source_urls(d, ch):
    """(substream_url, mainstream_url) that go2rtc should pull for one channel."""
    t = source_type(d)
    if t == "rtsp":
        sub = d.get("url", "")
        return sub, d.get("url_main") or sub
    if t == "onvif":
        auth = ""
        if d.get("user") or d.get("password"):
            auth = f"{_cred(d.get('user', ''))}:{_cred(d.get('password', ''))}@"
        base = f"onvif://{auth}{d.get('ip', '')}:{d.get('port', 80)}"
        # go2rtc's ONVIF source picks the media profile by index; cameras list
        # the main profile first and the sub profile second.
        return f"{base}?subtype=1", f"{base}?subtype=0"
    pwd = _cred(d.get("password", DEFAULT_DVR_PASS))
    ip = d.get("ip", "127.0.0.1")
    port = d.get("port", 3456)
    base = f"dvrip://admin:{pwd}@{ip}:{port}?channel={ch - 1}"
    return f"{base}&subtype=1", f"{base}&subtype=0"

def mask_url(url):
    """URL with any password replaced, safe to show in the UI or logs."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if parts.password is None:
        return url
    host = parts.hostname or ""
    if parts.port:
        host += f":{parts.port}"
    netloc = f"{parts.username or ''}:***@{host}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))

def all_channels(enabled_only=True):
    """Yield (dvr_key, ch) for every nominal channel slot across DVRs."""
    refresh_cache()
    for key in DVR_ORDER:
        d = DVRS.get(key, {})
        if enabled_only and not d.get("enabled", True):
            continue
        for ch in range(1, d.get("channels", 0) + 1):
            yield key, ch

def channel_label(dvr_key, ch):
    refresh_cache()
    d = DVRS.get(dvr_key)
    if not d:
        return f"{dvr_key.upper()} CH{ch}"
    return f"{d.get('label', dvr_key.upper())} CH{ch}"

def toggle_dvr(dvr_key, enabled):
    refresh_cache()
    if dvr_key in DVRS:
        DVRS[dvr_key]["enabled"] = bool(enabled)
        save_dvrs(DVRS)
        return True
    return False

def add_or_update_dvr(dvr_key, label, ip, port, channels, password=DEFAULT_DVR_PASS, enabled=True):
    refresh_cache()
    key = str(dvr_key).strip().lower()
    if not key:
        return False
    DVRS[key] = {
        "label": str(label).strip() or key.upper(),
        "ip": str(ip).strip(),
        "port": int(port) if str(port).isdigit() else 3456,
        "channels": int(channels) if str(channels).isdigit() else 8,
        "password": str(password).strip() if password else DEFAULT_DVR_PASS,
        "enabled": bool(enabled)
    }
    save_dvrs(DVRS)
    return True

def add_or_update_camera(key, label, ctype, enabled=True, **fields):
    """Single camera (rtsp or onvif), stored as a one-channel source.

    Validation is the caller's job; this only normalises and persists. ip/port
    are always filled in -- for rtsp they come from the URL -- so the existing
    TCP reachability probe works for cameras without knowing about them.
    """
    refresh_cache()
    key = str(key).strip().lower()
    if not key or ctype not in ("rtsp", "onvif"):
        return False
    entry = {"type": ctype, "label": str(label).strip() or key.upper(),
             "channels": 1, "enabled": bool(enabled)}
    if ctype == "rtsp":
        url = fields.get("url", "").strip()
        parts = urlsplit(url)
        entry.update(url=url, ip=parts.hostname or "",
                     port=parts.port or (322 if parts.scheme == "rtsps" else 554))
        if fields.get("url_main"):
            entry["url_main"] = fields["url_main"].strip()
    else:
        port = str(fields.get("port", "80")).strip()
        entry.update(ip=str(fields.get("ip", "")).strip(),
                     port=int(port) if port.isdigit() else 80,
                     user=str(fields.get("user", "")).strip(),
                     password=str(fields.get("password", "")))
        # Keep PTZ details learned earlier; they are re-probed on demand.
        old = DVRS.get(key, {})
        for k in ("ptz", "ptz_xaddr", "media_xaddr", "profile_token"):
            if k in old and old.get("ip") == entry["ip"]:
                entry[k] = old[k]
    DVRS[key] = entry
    save_dvrs(DVRS)
    return True

def set_fields(dvr_key, **fields):
    """Merge extra fields (e.g. learned PTZ endpoints) into an entry."""
    refresh_cache()
    if dvr_key not in DVRS:
        return False
    DVRS[dvr_key].update(fields)
    save_dvrs(DVRS)
    return True

def delete_dvr(dvr_key):
    refresh_cache()
    if dvr_key in DVRS:
        del DVRS[dvr_key]
        save_dvrs(DVRS)
        return True
    return False

def sync_go2rtc():
    """Generates /etc/dvr-kiosk/go2rtc.yaml (and /root/go2rtc.yaml) for all enabled DVRs."""
    yaml_paths = ["/etc/dvr-kiosk/go2rtc.yaml", "/root/go2rtc.yaml"]
    if os.name == 'nt':
        return
    try:
        lines = ["streams:"]
        for key, d in DVRS.items():
            if not d.get("enabled", True):
                continue
            for ch in range(1, d.get("channels", 0) + 1):
                sub, main = source_urls(d, ch)
                # JSON string literals are valid YAML double-quoted scalars, so
                # a user-typed URL can't inject YAML structure.
                # Substream for kiosk grid & thumbnails
                lines.append(f"  {key}_ch{ch}: {json.dumps(sub)}")
                # Mainstream for full-screen viewing
                lines.append(f"  {key}_ch{ch}_main: {json.dumps(main)}")

        yaml_content = "\n".join(lines) + "\n"
        for path in yaml_paths:
            parent = os.path.dirname(path)
            if os.path.exists(parent):
                with open(path, "w", encoding="utf-8") as f:
                    f.write(yaml_content)
        
        # Restart or reload go2rtc
        subprocess.run(["systemctl", "restart", "go2rtc.service"], check=False)
    except Exception as e:
        print(f"[dvr_config] Failed to sync go2rtc: {e}")

