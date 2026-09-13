# DVR Kiosk Developer & AI Agent Guide

This document covers build procedures, code conventions, and verification
steps. It is one of four:

| Document | Purpose |
| :--- | :--- |
| [PROJECT.md](../PROJECT.md) | What this is, why it's built this way, hard-won facts |
| [DEVELOPERS.md](../DEVELOPERS.md) | Deploy workflow, debugging techniques, open items |
| [AGENTS.md](../AGENTS.md) | Rules and constraints for AI coding agents |
| **This file** | Build commands, coding standards, verification |

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

### 3.4 Browser Wall (`/wall`) Verification

The page is cheap only while its guards hold, and all three fail silently:

```bash
# 1. Request rate should equal (visible tiles x governor thumb fps), no more.
#    Watch the access log while the page is open.
# 2. Background the tab -- the rate must fall to zero, not merely drop.
# 3. In portrait, swipe between pages -- the tile count polling must stay at
#    one page's worth, never the sum of both.
```

A tile that shows its label placeholder forever while requests are flowing
means frames are arriving but never being swapped in — check that the
double-buffered `Image` swap is intact rather than a direct `src` assignment.

### 3.5 Power Schedule Verification

The scheduler is easy to break in ways that are silent for hours, so test it
explicitly after touching `stop_streams()`, `launch_grid()`,
`apply_schedule_state()`, `sync_power_schedule()`, or `grid_watchdog()`:

```bash
# Back up first, then set a boundary ~2 minutes out
cp /root/kiosk_schedule.json /root/kiosk_schedule.json.bak
```

Watch `STATUS` across both boundaries. The off transition must reach
`streams=0` **and stay there** — if streams reappear, `grid_watchdog` is
resurrecting them because `current_mode` was left stale (see
[ARCHITECTURE.md](ARCHITECTURE.md) §4). Restore the real schedule afterwards.

### 3.6 Optional Nightly Reboot

Units ship installed but disabled, since reboot policy is a site decision:

```bash
sudo systemctl enable --now dvr-kiosk-reboot.timer
systemctl list-timers dvr-kiosk-reboot.timer
```

Set `OnCalendar` to a time inside the display's off-window. Treat it as a
safety net, not a fix — a nightly reboot masks stale-state bugs without
addressing the resources they waste in between.

---

## 4. Coding Standards & Behavioral Rules
1. **Never Break Video Reference Chains**: Always decode all H.264/H.265 frames via `avcodec_receive_frame`. Only throttle post-decode processing (`sws_scale`).
2. **Never Tear Down Sockets for Layout Switches**: Keep background substreams actively connected and adjust compositor pointers (`LAYOUT`, `FULLSCREEN`) to switch views in $<1\text{ms}$. Use `DECODE` to pause decoding while staying connected.
3. **Preserve Hysteresis**: Any CPU governor changes must preserve asymmetric recovery windows ($\ge 15$ seconds sustained low load before stepping up).
4. **Never Open 16 Long-Lived Connections From One Page**: The WSGI server runs a fixed worker pool (`threads=16`). A browser view that opens one MJPEG stream per tile holds a worker each for the life of the page and starves every other client. Tile views poll `<img>` instead, and chain each request off the previous one's completion so requests cannot queue. See [ARCHITECTURE.md](ARCHITECTURE.md) §3b.
5. **Move `current_mode` With the Wall**: Every function that starts or stops the wall must update `current_mode` itself, not rely on its callers. See [ARCHITECTURE.md](ARCHITECTURE.md) §4 — violating this is silent and self-sustaining.
