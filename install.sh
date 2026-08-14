#!/usr/bin/env bash
# ==============================================================================
# DVR Kiosk - Universal Linux Turnkey Installer
# Supports: Raspberry Pi (3/4/5), Orange Pi (3B/5/Zero), Radxa, Ubuntu, Debian
# ==============================================================================
set -e

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${BLUE}"
echo "================================================================"
echo "          DVR KIOSK & REMOTE CONTROLLER INSTALLER               "
echo "================================================================"
echo -e "${NC}"

# 1. Check Root Permissions
if [ "$EUID" -ne 0 ]; then
  echo -e "${RED}[ERROR] This installer must be run as root (use sudo).${NC}"
  exit 1
fi

INSTALL_DIR="/opt/dvr-kiosk"
CONFIG_DIR="/etc/dvr-kiosk"
CERTS_DIR="${CONFIG_DIR}/certs"

echo -e "${GREEN}[1/8] Detecting System Architecture...${NC}"
ARCH=$(uname -m)
case "$ARCH" in
  x86_64)
    GO2RTC_ARCH="linux_amd64"
    ;;
  aarch64|arm64)
    GO2RTC_ARCH="linux_arm64"
    ;;
  armv7l|armhf)
    GO2RTC_ARCH="linux_arm"
    ;;
  armv6l)
    GO2RTC_ARCH="linux_armv6"
    ;;
  i386|i686)
    GO2RTC_ARCH="linux_i386"
    ;;
  *)
    echo -e "${RED}[ERROR] Unsupported architecture: ${ARCH}${NC}"
    exit 1
    ;;
esac
echo -e "  -> Architecture: ${ARCH} (go2rtc binary: ${GO2RTC_ARCH})"

echo -e "${GREEN}[2/8] Installing System Dependencies...${NC}"
apt-get update -qq
apt-get install -y --no-install-recommends \
  build-essential \
  gcc \
  make \
  libavformat-dev \
  libavcodec-dev \
  libavutil-dev \
  libswscale-dev \
  python3 \
  python3-pip \
  python3-venv \
  fail2ban \
  unattended-upgrades \
  openssl \
  curl \
  git \
  ca-certificates

echo -e "${GREEN}[3/8] Setting Up Directory Structure...${NC}"
mkdir -p "${INSTALL_DIR}"
mkdir -p "${CONFIG_DIR}"
mkdir -p "${CERTS_DIR}"

# Copy files from installer repo
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -d "${SCRIPT_DIR}/src" ]; then
  cp -r "${SCRIPT_DIR}/src" "${INSTALL_DIR}/"
  cp -r "${SCRIPT_DIR}/systemd" "${INSTALL_DIR}/"
  cp "${SCRIPT_DIR}/Makefile" "${INSTALL_DIR}/"
  cp "${SCRIPT_DIR}/requirements.txt" "${INSTALL_DIR}/"
else
  # If running via standalone curl, clone git repo
  git clone https://github.com/tahashemi/DVR-kiosk.git "${INSTALL_DIR}"
fi

echo -e "${GREEN}[4/8] Compiling Native C Compositor (dvrwall)...${NC}"
make -C "${INSTALL_DIR}" clean
make -C "${INSTALL_DIR}"
make -C "${INSTALL_DIR}" install

echo -e "${GREEN}[5/8] Downloading go2rtc Streaming Engine...${NC}"
GO2RTC_URL="https://github.com/AlexxIT/go2rtc/releases/latest/download/go2rtc_${GO2RTC_ARCH}"
curl -fSL -o /usr/local/bin/go2rtc "${GO2RTC_URL}"
chmod +x /usr/local/bin/go2rtc

echo -e "${GREEN}[6/8] Configuring Python Virtual Environment...${NC}"
python3 -m venv "${INSTALL_DIR}/venv"
"${INSTALL_DIR}/venv/bin/pip" install --upgrade pip -q
"${INSTALL_DIR}/venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt" -q

echo -e "${GREEN}[7/8] Generating SSL Certificates & Security Hardening...${NC}"
if [ ! -f "${CERTS_DIR}/dvr-kiosk.pem" ]; then
  openssl req -x509 -newkey rsa:2048 -keyout "${CERTS_DIR}/dvr-kiosk-key.pem" \
    -out "${CERTS_DIR}/dvr-kiosk.pem" -days 3650 -nodes -subj "/CN=dvr-kiosk" 2>/dev/null
  chmod 600 "${CERTS_DIR}/dvr-kiosk-key.pem"
fi

# Configure fail2ban
cat << 'EOF' > /etc/fail2ban/jail.d/dvr-kiosk.local
[sshd]
enabled = true
port = ssh
filter = sshd
maxretry = 4
findtime = 600
bantime = 86400
EOF
systemctl restart fail2ban || true

# Setup interactive CLI tool
ln -sf "${INSTALL_DIR}/src/dvr_kiosk_cli.py" /usr/local/bin/dvr-kiosk
chmod +x /usr/local/bin/dvr-kiosk "${INSTALL_DIR}/src/dvr_kiosk_cli.py"

echo -e "${GREEN}[8/8] Installing & Starting Systemd Services...${NC}"
cp "${INSTALL_DIR}/systemd/go2rtc.service" /etc/systemd/system/
cp "${INSTALL_DIR}/systemd/dvrwall.service" /etc/systemd/system/
cp "${INSTALL_DIR}/systemd/dvr-kiosk.service" /etc/systemd/system/

systemctl daemon-reload
systemctl enable go2rtc.service dvrwall.service dvr-kiosk.service
systemctl restart go2rtc.service dvrwall.service dvr-kiosk.service

IP=$(hostname -I | awk '{print $1}')

echo -e "${BLUE}"
echo "================================================================"
echo "           ✓ DVR KIOSK SUCCESSFULLY INSTALLED!                  "
echo "================================================================"
echo -e "${NC}"
echo -e "Access your WebUI at:"
echo -e "  ${GREEN}https://${IP}:${NC} (Default port 443 / 80)"
echo ""
echo -e "CLI Management:"
echo -e "  Type ${YELLOW}dvr-kiosk${NC} anytime in your terminal for interactive menu"
echo -e "  (Password changes, service restarts, DVR configurations)"
echo "================================================================"
