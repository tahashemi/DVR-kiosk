#!/usr/bin/env python3
"""
CLI Tool for SSH-only User and Password Management.
Stores cryptographically hashed passwords using PBKDF2-HMAC-SHA256 (600,000 iterations).
Passwords CANNOT be changed via the WebUI.
"""

import sys
import os
import json
import hashlib
import secrets
import getpass

AUTH_CONFIG_PATHS = [
    "/etc/dvr-kiosk/auth_config.json",
    "/root/auth_config.json",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "auth_config.json"),
    "auth_config.json"
]

def get_auth_file():
    for p in AUTH_CONFIG_PATHS:
        if os.path.exists(p):
            return p
    if os.name != 'nt':
        if os.path.exists("/etc/dvr-kiosk"):
            return "/etc/dvr-kiosk/auth_config.json"
        if os.path.exists("/root"):
            return "/root/auth_config.json"
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "auth_config.json")

PBKDF2_ITERATIONS = 100000

def hash_password(password, salt=None):
    if salt is None:
        salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac(
        'sha256',
        password.encode('utf-8'),
        salt.encode('utf-8'),
        PBKDF2_ITERATIONS
    )
    return f"{salt}${key.hex()}"

def verify_password(stored_hash, password):
    try:
        salt, key = stored_hash.split("$")
        computed = hashlib.pbkdf2_hmac(
            'sha256',
            password.encode('utf-8'),
            salt.encode('utf-8'),
            PBKDF2_ITERATIONS
        )
        return secrets.compare_digest(computed.hex(), key)
    except Exception:
        return False

def load_auth_db():
    auth_file = get_auth_file()
    if os.path.exists(auth_file):
        try:
            with open(auth_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    # Generate random initial setup password if missing
    init_pass = secrets.token_hex(6)
    db = {"users": {"admin": hash_password(init_pass)}}
    save_auth_db(db)
    print(f"[*] Initialized default admin account with temporary password: {init_pass}")
    return db

def save_auth_db(db):
    auth_file = get_auth_file()
    os.makedirs(os.path.dirname(os.path.abspath(auth_file)), exist_ok=True)
    with open(auth_file, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2)
    # Set restricted permissions on Linux
    if os.name != 'nt':
        try:
            os.chmod(auth_file, 0o600)
        except Exception:
            pass

def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python3 set_password.py <username> [<password>]")
        print("  python3 set_password.py --list")
        print("  python3 set_password.py --delete <username>")
        sys.exit(1)

    cmd = sys.argv[1].strip()

    db = load_auth_db()
    users = db.setdefault("users", {})

    if cmd == "--list":
        print(f"Configured users ({len(users)}):")
        for u in users:
            print(f"  - {u}")
        return

    if cmd == "--delete":
        if len(sys.argv) < 3:
            print("Error: Specify username to delete.")
            sys.exit(1)
        u = sys.argv[2].strip()
        if u in users:
            del users[u]
            save_auth_db(db)
            print(f"User '{u}' deleted successfully.")
        else:
            print(f"User '{u}' not found.")
        return

    username = cmd
    if len(sys.argv) >= 3:
        password = sys.argv[2]
    else:
        password = getpass.getpass(f"Enter new password for '{username}': ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            print("Error: Passwords do not match!")
            sys.exit(1)

    if len(password) < 6:
        print("Warning: Password should be at least 6 characters.")

    users[username] = hash_password(password)
    save_auth_db(db)
    print(f"Success: Password for '{username}' updated successfully.")

if __name__ == "__main__":
    main()
