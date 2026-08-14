"""Named, saveable kiosk grid layouts. Persisted to disk so the active one
survives power loss / restart (loaded fresh by dvr_control.py on startup)."""
import json
import os
import threading

STORE_PATH = "/root/kiosk_profiles.json"
_lock = threading.Lock()

# Seeded once on first run to match the grid that was already live and
# working before this feature existed -- nothing changes for existing users
# until they actually edit something.
_DEFAULT_STORE = {
    "active": "default",
    "profiles": {
        "default": {
            "name": "Default",
            "channels": [
                {"dvr": "dvr1", "ch": 1}, {"dvr": "dvr1", "ch": 2}, {"dvr": "dvr1", "ch": 3},
                {"dvr": "dvr1", "ch": 4}, {"dvr": "dvr1", "ch": 5}, {"dvr": "dvr1", "ch": 6},
                {"dvr": "dvr2", "ch": 1}, {"dvr": "dvr2", "ch": 2}, {"dvr": "dvr2", "ch": 3},
                {"dvr": "tavakol", "ch": 1}, {"dvr": "tavakol", "ch": 3}, {"dvr": "tavakol", "ch": 4},
                {"dvr": "tavakol", "ch": 5}, {"dvr": "tavakol", "ch": 6}, {"dvr": "tavakol", "ch": 7},
                {"dvr": "tavakol", "ch": 8},
            ],
        }
    },
}


def _load():
    if not os.path.exists(STORE_PATH):
        _save(_DEFAULT_STORE)
        return json.loads(json.dumps(_DEFAULT_STORE))
    with open(STORE_PATH) as f:
        return json.load(f)


def _save(store):
    tmp = STORE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(store, f, indent=2)
    os.replace(tmp, STORE_PATH)


def list_profiles():
    with _lock:
        store = _load()
        return store["active"], {k: v["name"] for k, v in store["profiles"].items()}


def get_active_channels():
    with _lock:
        store = _load()
        return store["profiles"][store["active"]]["channels"]


def get_profile(key):
    with _lock:
        store = _load()
        return store["profiles"].get(key)


def set_active(key):
    with _lock:
        store = _load()
        if key not in store["profiles"]:
            raise KeyError(key)
        store["active"] = key
        _save(store)


def save_profile(key, name, channels):
    """Create or overwrite a profile's name/channel list."""
    with _lock:
        store = _load()
        store["profiles"][key] = {"name": name, "channels": channels}
        _save(store)


def rename_profile(key, new_name):
    with _lock:
        store = _load()
        if key not in store["profiles"]:
            raise KeyError(key)
        store["profiles"][key]["name"] = new_name
        _save(store)


def delete_profile(key):
    with _lock:
        store = _load()
        if key not in store["profiles"]:
            raise KeyError(key)
        if len(store["profiles"]) == 1:
            raise ValueError("cannot delete the last remaining profile")
        del store["profiles"][key]
        if store["active"] == key:
            store["active"] = next(iter(store["profiles"]))
        _save(store)
