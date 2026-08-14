#!/usr/bin/env python3
"""
Interactive CLI Management Tool for DVR Kiosk System.
Run simply by typing `dvr-kiosk` in SSH terminal.
"""
import sys
import os
import getpass
import subprocess
import set_password
import dvr_config

def clear_screen():
    os.system('clear' if os.name != 'nt' else 'cls')

def banner():
    print("=" * 58)
    print("             DVR KIOSK INTERACTIVE CONTROL PANEL")
    print("=" * 58)

def menu():
    print("\n  [1] Change / Set Web UI User Password")
    print("  [2] List Configured Web UI Users")
    print("  [3] Add New Web UI User")
    print("  [4] Delete Web UI User")
    print("  [5] System & Service Status (Kiosk, go2rtc, Fail2ban)")
    print("  [6] Restart All DVR Services")
    print("  [7] Emergency Rollback to Clean Backup")
    print("  [0] Exit")
    print("=" * 58)

def change_password():
    print("\n--- Change / Set Web UI Password ---")
    db = set_password.load_auth_db()
    users = list(db.get("users", {}).keys())
    if not users:
        print("[-] No users found. Please add a user first.")
        return
    print("Existing users:", ", ".join(users))
    username = input(f"Enter username [{users[0]}]: ").strip() or users[0]
    if username not in db.get("users", {}):
        print(f"[-] User '{username}' does not exist.")
        return
    pwd = getpass.getpass(f"Enter new password for '{username}': ")
    confirm = getpass.getpass("Confirm new password: ")
    if pwd != confirm:
        print("[!] Error: Passwords do not match!")
        return
    if len(pwd) < 6:
        print("[!] Warning: Password is shorter than 6 characters.")
    
    db["users"][username] = set_password.hash_password(pwd)
    set_password.save_auth_db(db)
    print(f"\n[✓] Password for '{username}' successfully updated!\n")

def list_users():
    print("\n--- Configured Web UI Users ---")
    db = set_password.load_auth_db()
    users = db.get("users", {})
    print(f"Total users: {len(users)}")
    for u in users:
        print(f"  • {u}")
    print()

def add_user():
    print("\n--- Add New Web UI User ---")
    username = input("Enter new username: ").strip()
    if not username:
        print("[!] Username cannot be empty.")
        return
    db = set_password.load_auth_db()
    if username in db.get("users", {}):
        print(f"[!] User '{username}' already exists. Use Option 1 to change password.")
        return
    pwd = getpass.getpass(f"Enter password for '{username}': ")
    confirm = getpass.getpass("Confirm password: ")
    if pwd != confirm:
        print("[!] Error: Passwords do not match!")
        return
    db["users"][username] = set_password.hash_password(pwd)
    set_password.save_auth_db(db)
    print(f"\n[✓] User '{username}' created successfully!\n")

def delete_user():
    print("\n--- Delete Web UI User ---")
    db = set_password.load_auth_db()
    users = db.get("users", {})
    if len(users) <= 1:
        print("[!] Cannot delete the only remaining user.")
        return
    print("Existing users:", ", ".join(users.keys()))
    username = input("Enter username to delete: ").strip()
    if username in users:
        confirm = input(f"Are you sure you want to delete '{username}'? (y/N): ").strip().lower()
        if confirm == 'y':
            del users[username]
            set_password.save_auth_db(db)
            print(f"\n[✓] User '{username}' deleted.\n")
    else:
        print(f"[-] User '{username}' not found.\n")

def service_status():
    print("\n--- Service Status ---")
    if os.name != 'nt':
        subprocess.run(["systemctl", "status", "dvr-kiosk.service", "go2rtc.service", "dvrwall.service", "fail2ban.service", "--no-pager"])
    else:
        print("Running on Windows dev environment.")

def restart_services():
    print("\n--- Restarting Services ---")
    if os.name != 'nt':
        print("[*] Restarting go2rtc, dvrwall, and dvr-kiosk...")
        subprocess.run(["systemctl", "restart", "go2rtc.service", "dvrwall.service", "dvr-kiosk.service"])
        print("[✓] Services restarted successfully.")
    else:
        print("Restart simulated on Windows.")

def emergency_rollback():
    print("\n--- Emergency Rollback ---")
    confirm = input("Are you sure you want to rollback to the clean backup? (y/N): ").strip().lower()
    if confirm == 'y':
        import rollback
        rollback.rollback_local()
        rollback.rollback_remote()

def main():
    while True:
        banner()
        menu()
        try:
            choice = input("Select an option [0-7]: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nExiting. Goodbye!")
            sys.exit(0)
        
        if choice == '1':
            change_password()
        elif choice == '2':
            list_users()
        elif choice == '3':
            add_user()
        elif choice == '4':
            delete_user()
        elif choice == '5':
            service_status()
        elif choice == '6':
            restart_services()
        elif choice == '7':
            emergency_rollback()
        elif choice == '0':
            print("Exiting. Goodbye!")
            sys.exit(0)
        else:
            print("[!] Invalid option. Please enter 0-7.")
        
        try:
            input("\nPress Enter to return to menu...")
        except (KeyboardInterrupt, EOFError):
            sys.exit(0)
        clear_screen()

if __name__ == "__main__":
    main()
