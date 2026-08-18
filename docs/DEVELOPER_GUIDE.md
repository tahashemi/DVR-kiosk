# DVR Kiosk Developer & AI Agent Guide

This document provides developer guidelines, code conventions, build procedures, and verification steps for contributors and AI agents working on the DVR Kiosk codebase.

---

## 1. Project Structure & Code Locations

```
DVR-KIOSK-GIT/
├── Makefile               # C compilation build instructions
├── install.sh             # Universal cross-platform installer (SBC & x86)
├── uninstall.sh           # Clean service & binary removal
├── requirements.txt       # Python dependencies (Flask, Waitress, etc.)
├── docs/
│   ├── ARCHITECTURE.md    # System architecture & socket protocol
│   ├── SECURITY.md        # Security model & reverse proxy setup
│   └── DEVELOPER_GUIDE.md # Developer workflows & verification
├── src/
│   ├── dvrwall.c          # High-performance C framebuffer compositor
│   ├── dvr_control.py     # Main WebUI backend & dynamic CPU governor
│   ├── wall.py            # Python client for dvrwall UNIX socket
│   ├── dvr_config.py      # DVR & camera configuration loader
│   ├── profiles.py        # Kiosk layout profile management
│   ├── schedule.py        # Operating hours power scheduler
│   ├── set_password.py    # CLI admin password utility
│   └── dvr_kiosk_cli.py   # Interactive terminal management menu
└── systemd/               # Systemd unit files (go2rtc, dvrwall, dvr-kiosk)
```

---

## 2. Building & Compiling `dvrwall`

### Prerequisites (Debian / Ubuntu / Armbian / Raspberry Pi OS):
```bash
sudo apt-get update
sudo apt-get install -y build-essential libavformat-dev libavcodec-dev libavutil-dev libswscale-dev
```

### Build Commands:
```bash
# Clean and compile
make clean
make

# Install to /usr/local/bin/
sudo make install
```

---

## 3. Testing & Verification Workflows

### 3.1 Python Syntax & Module Validation
```bash
python3 -m py_compile src/dvr_control.py src/wall.py src/profiles.py src/schedule.py
```

### 3.2 UNIX Control Socket Smoke Test
```bash
# Test status query
echo "STATUS" | nc -U /run/dvrwall.sock

# Test scale stride adjustment
echo "STRIDE 2" | nc -U /run/dvrwall.sock
echo "STRIDE 1" | nc -U /run/dvrwall.sock

# Test framerate adjustment
echo "FPS 12" | nc -U /run/dvrwall.sock
```

### 3.3 Security & Secret Audit Before Git Commit
Always verify that no live passwords or personal tokens exist in tracked files:
```bash
git grep -iE "(password|secret|token)" | grep -v "auth_config"
```

---

## 4. Coding Standards & Behavioral Rules
1. **Never Break Video Reference Chains**: Always decode all H.264/H.265 frames via `avcodec_receive_frame`. Only throttle post-decode processing (`sws_scale`).
2. **Never Tear Down Sockets for Layout Switches**: Keep background substreams actively connected and adjust compositor pointers (`LAYOUT`, `FULLSCREEN`) to switch views in $<1\text{ms}$.
3. **Preserve Hysteresis**: Any CPU governor changes must preserve asymmetric recovery windows ($\ge 15$ seconds sustained low load before stepping up).
