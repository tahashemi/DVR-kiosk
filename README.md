# 🎥 DVR Kiosk & Remote Wall Controller

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Platform: Linux](https://img.shields.io/badge/Platform-Linux%20%7C%20ARM64%20%7C%20ARMv7%20%7C%20x86__64-orange.svg)]()
[![Hardware: Universal](https://img.shields.io/badge/Hardware-Orange%20Pi%20%7C%20Raspberry%20Pi%20%7C%20Rockchip%20%7C%20x86-brightgreen.svg)]()

A high-performance, hardware-accelerated **Multi-DVR Kiosk & Web Controller** designed for Linux Single-Board Computers (Orange Pi, Raspberry Pi, Rockchip RK3588, Allwinner) and standard x86 edge servers.

Combines an ultra-efficient C-based HDMI video wall compositor (`dvrwall`) with an adaptive dynamic load governor and a responsive WebUI controller.

---

## 🌟 Key Features

- 🖥️ **Hardware-Accelerated 16-Tile Wall**: Custom C/libavcodec compositor rendering directly to `/dev/fb0` with sub-25ms latency.
- ⚡ **Dynamic Post-Decode Scale-Skip Governor**:
  - Decode pipeline maintains 100% P-frame DPB reference integrity (0% video glitches).
  - Skips CPU-intensive `sws_scale` color conversions under load (`1-of-2`, `1-of-4`, `1-of-8` stride), saving up to 60% CPU on low-power ARM cores.
  - Asymmetric recovery hysteresis prevents framerate oscillation.
- 🔍 **Instant Zero-Churn Fullscreen**: Seamlessly switches between 16-channel grid and 1x1 fullscreen view in $<1\text{ms}$ without tearing down or reconnecting RTSP decoders.
- 🌐 **Reverse-Proxy Ready**: Plain HTTP on port 80 designed for zero-crypto CPU overhead, with full upstream TLS offloading (pfSense HAProxy, Nginx, Traefik).
- 🎯 **Touch & Drag-and-Drop Layout Editor**: Interactive grid customization with custom channel labeling and persistent profiles.
- 🔒 **Hardened Security**:
  - `fail2ban` brute-force protection with 24-hour ban.
  - Salted password authentication with cryptographically secure session tokens.
  - Zero-credential git architecture.
- 🕗 **Operating-Hours Power Schedule**: Configurable daily on/off window that drops every decoder connection when the wall is off, so an idle wall costs essentially no CPU. Manual on/off from the dashboard is honoured until the next boundary.
- 🔁 **Optional Nightly Reboot**: Systemd timer shipped installed-but-disabled (`systemctl enable --now dvr-kiosk-reboot.timer`), with a configurable `OnCalendar`.
- 🖥️ **Browser Video Wall (`/wall`)**: Chrome-free page that mirrors the TV exactly — same profile order, and it follows the kiosk into fullscreen. Landscape renders the full grid edge to edge; portrait paginates into swipeable pages so tiles stay readable on a phone. Frames are polled `<img>` rather than MJPEG (16 MJPEG streams would occupy the entire web-server thread pool), each tile chains its next request off the previous one so requests never queue, offscreen tiles stop polling entirely, and the poll rate follows the CPU governor's current tier.
- 📷 **Standalone Cameras (RTSP & ONVIF)**: Besides multi-channel DVRs, add a single camera from **Settings → Add DVR or Camera** — paste an `rtsp://` address, or pick an ONVIF camera found by a network scan (WS-Discovery). A **Test** button grabs one frame before saving so typos never become black tiles. Cameras are relayed through go2rtc like everything else, so the wall, profiles, labels and `/wall` treat them as ordinary channels. Credentials are never sent back to the browser.
- 🕹️ **PTZ Control for ONVIF Cameras**: In full view, drag on the picture to look around (drag distance = speed), wheel/pinch to zoom, or use the auto-hiding pad; saved views appear as one-tap chips. Every move is renewed while held and the server stops the camera itself if renewals stop, so a closed tab or dropped Wi-Fi can't leave it spinning. ONVIF is spoken with the standard library only — no heavy SOAP stack on a 512 MB board.
- ⌨️ **Interactive CLI Tool (`dvr-kiosk`)**: Built-in terminal dashboard for password changes, service restarts, and configuration management.

---

## 🚀 Quick Install (1-Line Universal Installer)

Run this command on your Linux device (Orange Pi, Raspberry Pi, Debian, Ubuntu, x86):

```bash
curl -fsSL https://raw.githubusercontent.com/tahashemi/DVR-kiosk/main/install.sh | sudo bash
```

### What the installer handles automatically:
1. Detects CPU architecture (`ARM64`, `ARMv7`, `x86_64`).
2. Installs FFmpeg development libraries, Python 3, and build essentials.
3. Compiles the high-speed `dvrwall` C compositor.
4. Sets up `go2rtc` and Python virtual environment.
5. Configures security hardening (`fail2ban`, token auth).
6. Installs and starts all systemd background services (`go2rtc.service`, `dvrwall.service`, `dvr-kiosk.service`).
7. Installs the interactive `dvr-kiosk` CLI command.

---

## 🔄 Updating (1-Line, Code Only)

Pull the latest application code from GitHub onto an installed device:

```bash
curl -fsSL https://raw.githubusercontent.com/tahashemi/DVR-kiosk/main/update.sh | sudo bash
```

It finds where the running service loads its code from, syntax-checks the new version before swapping anything, backs up every replaced file, and rolls back automatically if the service doesn't come back healthy. It never touches configuration, credentials, go2rtc or systemd units. `BRANCH=dev` selects another branch.

---

## 🗑️ Quick Uninstall (1-Line Complete Removal)

To completely remove DVR Kiosk, all systemd services (`dvr-kiosk`, `dvrwall`, `go2rtc`), binaries, and configurations:

```bash
curl -fsSL https://raw.githubusercontent.com/tahashemi/DVR-kiosk/main/uninstall.sh | sudo bash
```

---

## 💻 Interactive CLI Management

Simply type `dvr-kiosk` in your terminal to open the interactive management menu:

```bash
$ sudo dvr-kiosk
======================================================================
                  DVR KIOSK - SYSTEM MANAGEMENT MENU                  
======================================================================
  [1] Change Admin Password
  [2] View Service Status (dvr-kiosk, dvrwall, go2rtc, fail2ban)
  [3] Restart All Services
  [4] View Active DVRs & Channel Layout
  [5] Enable / Disable Remote DVR (Save WAN Bandwidth)
  [6] View Security & fail2ban Status
  [7] Create Emergency Backup & Rollback
  [0] Exit
======================================================================
```

---

## 📚 Documentation

**Start here if you're new to the project:**

- 🧭 **[PROJECT.md](PROJECT.md)**: What this is, why it's designed this way, and the hard-won facts worth not rediscovering.

**Then, depending on what you're doing:**

- 🏛️ **[System Architecture](docs/ARCHITECTURE.md)**: Compositor internals, UNIX socket IPC protocol, stream pipeline, and the wall state machine.
- 🔧 **[DEVELOPERS.md](DEVELOPERS.md)**: Deploy workflow, debugging techniques that actually work on this hardware, and open items.
- 🤖 **[AGENTS.md](AGENTS.md)**: Constraints and working rules for AI coding agents.
- 🛡️ **[Security & Hardening](docs/SECURITY.md)**: Reverse proxy TLS termination, fail2ban, session token auth, and credential isolation.
- 👩‍💻 **[Developer & Agent Guide](docs/DEVELOPER_GUIDE.md)**: Local compilation, testing workflows, socket commands, and coding standards.

---

## 📄 License

Distributed under the MIT License. See `LICENSE` for more information.
