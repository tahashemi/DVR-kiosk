# DVR Kiosk Architecture & Technical Reference

DVR Kiosk is a high-performance, hardware-accelerated video wall compositor and web management suite designed to run multi-camera CCTV walls on low-power Single Board Computers (Raspberry Pi, Orange Pi, Rockchip RK3588, Allwinner) and x86 edge devices.

---

## 1. High-Level Architecture Overview

The system consists of three decoupled daemon processes communicating over local UNIX domain sockets and HTTP APIs:

```
                  +----------------------------------------------+
                  |              DVRs / IP Cameras               |
                  |     (RTSP / DVRIP / ONVIF Protocols)         |
                  +----------------------+-----------------------+
                                         |
                                         v
                  +----------------------------------------------+
                  |               go2rtc Gateway                 |
                  |   - Multi-protocol stream normalization      |
                  |   - High-speed RTSP proxy (:8554)            |
                  |   - WebRTC / MSE local endpoints             |
                  +----------------------+-----------------------+
                                         |
                                         v RTSP (127.0.0.1:8554)
                  +----------------------------------------------+
                  |          dvrwall (Native C Compositor)       |
                  |   - Hardware blitting direct to /dev/fb0     |
                  |   - 16x concurrent H.264 decoders (FFmpeg)   |
                  |   - Dynamic Scale-Skip Governor (STRIDE)     |
                  |   - High-speed MJPEG cache on :8590          |
                  +----------------------+-----------------------+
                                         ^
                                         | UNIX Socket (/run/dvrwall.sock)
                  +----------------------+-----------------------+
                  |     dvr_control.py (Web & Governor Daemon)   |
                  |   - Plain HTTP on Port 80 (Waitress/Flask)   |
                  |   - Universal Dynamic CPU Load Governor      |
                  |   - Profile, Layout & Scheduler Management   |
                  |   - Asymmetric recovery hysteresis           |
                  +----------------------+-----------------------+
                                         ^
                                         | HTTP (:80)
                  +----------------------+-----------------------+
                  |         Upstream Reverse Proxy / Client      |
                  |   (pfSense / HAProxy / Nginx TLS Term.)      |
                  +----------------------------------------------+
```

---

## 2. Core Subsystems

### 2.1 `dvrwall` (Native C Compositor)
- **Direct Framebuffer Output**: Blits decoded frames directly to `/dev/fb0`, bypassing X11, Wayland, and heavy window managers.
- **Double-Buffered Zero-Copy Swapping**: Decoders scale directly into `slot_back` buffer and perform an atomic pointer swap with front `slot`, eliminating memory copying locks.
- **Dynamic Post-Decode Scale-Skip Governor**:
  - `avcodec_receive_frame` processes every packet to keep the H.264 DPB reference chain 100% intact.
  - Skips `sws_scale` (YUV420P $\rightarrow$ BGRA conversion) based on `GLOBAL_SCALE_STRIDE` (`1-of-1`, `1-of-2`, `1-of-4`, `1-of-8`).
  - Saves $\sim 45\%$ per-frame compute cost on low-power ARM cores under heavy load.
- **Zero-Churn Layout Switching**: Layout changes and fullscreen toggles modify compositor blit pointers in $<1\text{ms}$ without destroying and reconnecting RTSP decoder threads.

### 2.2 `go2rtc` Stream Gateway
- Handles camera manufacturer protocols (DVRIP, Hikvision, Dahua, RTSP).
- Serves low-latency RTSP feeds locally on `rtsp://127.0.0.1:8554/` for `dvrwall`.

### 2.3 `dvr_control.py` & CPU Load Governor
- **Dynamic CPU Load Governor**:
  - Computes normalized CPU load from `/proc/stat` deltas.
  - Triggers aggressive step-down at $>80\%$ load (`STRIDE 2` or `STRIDE 4`).
  - Employs **asymmetric hysteresis**: requires $\ge 15$ continuous seconds below $60\%$ before stepping up, preventing framerate thrashing.
- **Web UI & Control Server**:
  - Serves responsive mobile-friendly dashboard on port 80.
  - Native support for reverse proxies via `X-Forwarded-Proto` and `X-Forwarded-For`.

---

## 3. UNIX Socket IPC Protocol (`/run/dvrwall.sock`)

| Command | Arguments | Description |
| :--- | :--- | :--- |
| `CHANNELS` | `<url1> <url2> ...` | Set active background decoding roster. |
| `LAYOUT` | `<url1> <url2> ...` | Set on-screen grid composite layout. |
| `FULLSCREEN` | `<url>` | Switch display to 1x1 full-screen layout. |
| `STRIDE` | `<1\|2\|4\|8>` | Set post-decode scale-skip stride. |
| `FPS` | `<fps>` | Set compositor target display framerate. |
| `JPEGFPS` | `<thumb> <main>` | Set thumbnail and mainstream JPEG cache rate. |
| `STATUS` | None | Return real-time JSON status of all streams. |
| `CLEAR` | None | Blank TV display while keeping decoders warm. |
| `STOP` | None | Stop all decoder streams. |
| `BLANK` | None | Stop decoders and power down display (`FB_BLANK_POWERDOWN`). |
