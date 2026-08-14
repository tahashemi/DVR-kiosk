#!/usr/bin/env bash
# ==============================================================================
# DVR Kiosk - Universal Uninstaller Script
# Removes all systemd services, binaries, configs, and installation files.
# ==============================================================================
set -e

GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m' # No Color

if [ "$EUID" -ne 0 ]; then
  echo -e "${RED}[ERROR] Please run as root (use sudo).${NC}"
  exit 1
fi

PURGE=false
if [[ "$*" == *"--purge"* ]] || [[ "$*" == *"-y"* ]] || [ ! -t 0 ]; then
  PURGE=true
fi

echo -e "${BLUE}[*] Stopping & Disabling DVR Kiosk Services...${NC}"
systemctl stop dvr-kiosk.service dvrwall.service go2rtc.service 2>/dev/null || true
systemctl disable dvr-kiosk.service dvrwall.service go2rtc.service 2>/dev/null || true

echo -e "${BLUE}[*] Removing Systemd Service Files...${NC}"
rm -f /etc/systemd/system/dvr-kiosk.service
rm -f /etc/systemd/system/dvrwall.service
rm -f /etc/systemd/system/go2rtc.service
systemctl daemon-reload 2>/dev/null || true
systemctl reset-failed 2>/dev/null || true

echo -e "${BLUE}[*] Removing Binaries & CLI Symlinks...${NC}"
rm -f /usr/local/bin/dvrwall
rm -f /usr/local/bin/go2rtc
rm -f /usr/local/bin/dvr-kiosk

if [ "$PURGE" = false ]; then
  read -p "Do you also want to remove configurations in /etc/dvr-kiosk and /opt/dvr-kiosk? (y/N): " choice
  if [[ "$choice" =~ ^[Yy]$ ]]; then
    PURGE=true
  fi
fi

if [ "$PURGE" = true ]; then
  echo -e "${BLUE}[*] Purging Application Directories & Configs...${NC}"
  rm -rf /opt/dvr-kiosk
  rm -rf /etc/dvr-kiosk
  rm -rf /root/dvr_config.json /root/go2rtc.yaml /root/auth_config.json 2>/dev/null || true
fi

echo -e "${GREEN}[✓] DVR Kiosk has been completely removed from the system.${NC}"
