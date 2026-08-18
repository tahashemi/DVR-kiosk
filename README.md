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

- 🏛️ **[System Architecture](docs/ARCHITECTURE.md)**: Compositor internals, UNIX socket IPC protocol, and stream pipeline.
- 🛡️ **[Security & Hardening](docs/SECURITY.md)**: Reverse proxy TLS termination, fail2ban, session token auth, and credential isolation.
- 👩‍💻 **[Developer & Agent Guide](docs/DEVELOPER_GUIDE.md)**: Local compilation, testing workflows, socket commands, and coding standards.

---

## 📄 License

Distributed under the MIT License. See `LICENSE` for more information.
