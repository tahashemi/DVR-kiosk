#!/usr/bin/env bash
# ==============================================================================
# DVR Kiosk - Uninstaller Script
# ==============================================================================
set -e

if [ "$EUID" -ne 0 ]; then
  echo "Please run as root (sudo bash uninstall.sh)"
  exit 1
fi

echo "[*] Stopping and disabling services..."
systemctl stop dvr-kiosk.service dvrwall.service go2rtc.service || true
systemctl disable dvr-kiosk.service dvrwall.service go2rtc.service || true

echo "[*] Removing systemd service files..."
rm -f /etc/systemd/system/dvr-kiosk.service
rm -f /etc/systemd/system/dvrwall.service
rm -f /etc/systemd/system/go2rtc.service
systemctl daemon-reload

echo "[*] Removing binaries and CLI..."
rm -f /usr/local/bin/dvrwall
rm -f /usr/local/bin/go2rtc
rm -f /usr/local/bin/dvr-kiosk

read -p "Do you also want to remove configurations in /etc/dvr-kiosk and /opt/dvr-kiosk? (y/N): " choice
if [[ "$choice" =~ ^[Yy]$ ]]; then
  rm -rf /opt/dvr-kiosk
  rm -rf /etc/dvr-kiosk
  echo "[✓] Configs and installation directory removed."
fi

echo "[✓] DVR Kiosk has been successfully uninstalled."
