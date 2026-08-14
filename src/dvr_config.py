"""Dynamic DVR and stream definitions with persistence, on-demand bandwidth toggle,
and support for both substream (subtype=1) and mainstream (subtype=0)."""

import json
import os
import subprocess
import threading

CONFIG_PATHS = [
    "/root/dvr_config.json",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "dvr_config.json"),
    "dvr_config.json"
]

DEFAULT_DVR_PASS = ""

DEFAULT_DVRS = {
    "dvr1": {"label": "DVR 1", "ip": "192.168.1.100", "port": 3456, "channels": 8, "password": "", "enabled": True},
}

_lock = threading.Lock()

def _get_config_path():
    for p in CONFIG_PATHS:
        if os.path.exists(p):
            return p
    # Default to /root/dvr_config.json on Linux, else local dvr_config.json
    if os.name != 'nt' and os.path.exists("/root"):
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
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"dvrs": dvrs_dict}, f, indent=2)
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

def dvrip_url(dvr_key, ch, subtype=1):
    refresh_cache()
    d = DVRS.get(dvr_key)
    if not d:
        return ""
    pwd = d.get("password", DEFAULT_DVR_PASS)
    ip = d.get("ip", "127.0.0.1")
    port = d.get("port", 3456)
    return f"dvrip://admin:{pwd}@{ip}:{port}?channel={ch - 1}&subtype={subtype}"

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

def delete_dvr(dvr_key):
    refresh_cache()
    if dvr_key in DVRS:
        del DVRS[dvr_key]
        save_dvrs(DVRS)
        return True
    return False

def sync_go2rtc():
    """Generates /root/go2rtc.yaml for all enabled DVRs (both substream and mainstream) and reloads go2rtc."""
    yaml_path = "/root/go2rtc.yaml"
    if not os.path.exists("/root") and os.name == 'nt':
        return
    try:
        lines = ["streams:"]
        for key, d in DVRS.items():
            if not d.get("enabled", True):
                continue
            pwd = d.get("password", DEFAULT_DVR_PASS)
            ip = d.get("ip", "127.0.0.1")
            port = d.get("port", 3456)
            for ch in range(1, d.get("channels", 0) + 1):
                # Substream (subtype=1) for kiosk grid & thumbnails
                lines.append(f"  {key}_ch{ch}: dvrip://admin:{pwd}@{ip}:{port}?channel={ch - 1}&subtype=1")
                # Mainstream (subtype=0) for 720p full-screen viewing
                lines.append(f"  {key}_ch{ch}_main: dvrip://admin:{pwd}@{ip}:{port}?channel={ch - 1}&subtype=0")
        
        with open(yaml_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        
        # Restart or reload go2rtc
        subprocess.run(["systemctl", "restart", "go2rtc.service"], check=False)
    except Exception as e:
        print(f"[dvr_config] Failed to sync go2rtc: {e}")
