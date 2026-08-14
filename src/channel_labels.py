import json
import os
import threading

import dvr_config

STORE_PATH = "/root/channel_labels.json"
if not os.path.exists(os.path.dirname(STORE_PATH)):
    STORE_PATH = os.path.join(os.path.dirname(__file__), "channel_labels.json")

_lock = threading.Lock()


def _load():
    if not os.path.exists(STORE_PATH):
        return {}
    try:
        with open(STORE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(data):
    tmp = STORE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, STORE_PATH)


def get_labels():
    with _lock:
        return _load()


def get_label(dvr_key, ch):
    with _lock:
        data = _load()
        key = f"{dvr_key}:{ch}"
        if key in data and data[key].strip():
            return data[key].strip()
    return dvr_config.channel_label(dvr_key, ch)


def set_label(dvr_key, ch, label):
    with _lock:
        data = _load()
        key = f"{dvr_key}:{ch}"
        if label and label.strip():
            data[key] = label.strip()
        elif key in data:
            del data[key]
        _save(data)


def set_labels(label_map):
    with _lock:
        data = _load()
        for key, val in label_map.items():
            if val and val.strip():
                data[key] = val.strip()
            elif key in data:
                del data[key]
        _save(data)
