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
- **Hardware decoding with automatic fallback** (`src/hwdec.c`): packets are still read with libavformat; only the decode step is swappable. In `auto` mode each stream tries, in order:

  | Backend | Uses | Typical boards |
  | :--- | :--- | :--- |
  | `gst-v4l2sl` | GStreamer `v4l2slh264dec` / `v4l2slh265dec` (stateless V4L2) | Allwinner H3/H5/H6/H616 (cedrus), mainline Rockchip, NXP |
  | `gst-nv` | GStreamer `nvv4l2decoder` | NVIDIA Jetson |
  | `v4l2m2m` | FFmpeg `h264_v4l2m2m` (stateful V4L2) | Raspberry Pi 2-4, Amlogic, Qualcomm |
  | `rkmpp` | FFmpeg `h264_rkmpp` (if the FFmpeg build has it) | Rockchip vendor kernels |
  | `vaapi` / `cuda` | FFmpeg hwaccels | Intel/AMD and NVIDIA PCs |
  | `software` | libavcodec | everything (Raspberry Pi 5 has no H.264 block) |

  Mainline FFmpeg cannot drive stateless decoders, which is why that path goes through GStreamer. A backend is skipped without trying when the stream is larger than it supports (1080p default for SBC decoders, `--hw-max-pixels`), when it isn't installed, or after repeated failures. A hardware decoder that errors, falls behind the stream, or goes silent hands its stream to software in place (no reconnect) and is retried for that stream after 5 minutes. Allwinner's tiled `NV12_32L32` output is untiled before scaling. Policy: `--decoder`, the `DECODER` socket command, or `"decoder"` in `dvr_config.json`; `--hw-max-streams` caps concurrent hardware sessions; `dvrwall --selftest` lists what a machine has. `STATUS` reports each stream's decoder.

### 2.2 `go2rtc` Stream Gateway
- Handles camera manufacturer protocols (DVRIP, Hikvision, Dahua, RTSP).
- Serves low-latency RTSP feeds locally on `rtsp://127.0.0.1:8554/` for `dvrwall`.
- **Source types** (`type` in `dvr_config.json`, default `dvrip`): a multi-channel DVR (`dvrip://`), a single RTSP camera (the URL as given, optional separate `url_main`), or a single ONVIF camera (`onvif://`, go2rtc resolves the stream). Single cameras are stored as one-channel entries, so everything downstream — `dvrwall`, profiles, labels, `/wall` — keeps addressing `<key>_ch1` with no special cases. Credentials are percent-encoded into URLs and every YAML value is quoted, so user input can't break or inject into `go2rtc.yaml`.
- **PTZ** (ONVIF only, `onvif_util.py`, standard library SOAP): moves are `ContinuousMove` velocities that the browser renews ~1/s while held; a server-side timer sends `Stop` if renewals stop (1.5 s), and the UI sends at most 4 moves/s, latest-wins.

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
| `DECODE` | `<url1> ...` (empty = all) | Decode only these streams; pause the rest. |
| `LAYOUT` | `<url1> <url2> ...` | Set on-screen grid composite layout. |
| `FULLSCREEN` | `<url>` | Switch display to 1x1 full-screen layout. |
| `STRIDE` | `<1\|2\|4\|8>` | Set post-decode scale-skip stride. |
| `FPS` | `<fps>` | Set compositor target display framerate. |
| `JPEGFPS` | `<thumb> <main>` | Set thumbnail and mainstream JPEG cache rate. |
| `DECODER` | `<auto\|software\|backend>` | Decoder policy for streams opened from now on. |
| `STATUS` | None | Return real-time JSON status of all streams. |
| `CLEAR` | None | Blank TV display while keeping decoders warm. |
| `STOP` | None | Stop all decoder streams. |
| `BLANK` | None | Stop decoders and power down display (`FB_BLANK_POWERDOWN`). |

---

## 3b. Browser Wall (`/wall`)

`/wall` renders the same thing the TV shows, in a browser. It holds no state of
its own: `/api/wall/layout` returns the active profile's channels in order plus
the kiosk's current mode, and the page rebuilds only when that payload changes.
Following the kiosk rather than re-deriving a layout is what keeps the browser
view from drifting away from the physical wall.

Frame delivery is the part worth understanding, because the obvious
implementation does not survive this hardware:

| Choice | Reason |
| :--- | :--- |
| Polled `<img>`, not MJPEG | A 16-tile MJPEG view holds 16 connections open for the life of the page. The WSGI server runs a fixed worker pool of the same order, so one such viewer starves every other client, dashboard included. |
| Double-buffered swap | The frame loads into a detached `Image` and is swapped in only once decoded. Assigning `src` directly blanks the element until the new frame arrives, which at these rates leaves every tile permanently mid-load and painting nothing. |
| Next request issued on completion | Each tile schedules its next fetch from the previous fetch's handler, never from a free-running timer, so a slow board lowers the frame rate instead of accumulating in-flight requests. Same latest-frame-wins rule the compositor uses. |
| Offscreen tiles stop entirely | Tiles on a non-visible portrait page, and every tile while the tab is hidden, drop their `src` and cancel their timer. A backgrounded phone costs the board nothing. |
| Rate follows the governor | The poll period comes from the governor's published thumbnail fps, so the browser backs off in step with the encoder instead of demanding frames that are not being produced. |
| Failing channels back off | A channel that errors repeatedly is retried on a widening delay, so an unreachable DVR does not cost as much as a working one. |

The snapshot cache TTL is derived from the same governor tier: caching a frame
for longer than one encoder frame period only serves stale pixels, and caching
for less just re-fetches an identical frame.

## 4. Wall State Machine & Power Schedule

`dvr_control.py` holds a single module-level `current_mode` that is the
authoritative record of what the wall is doing:

| Value | Meaning |
| :--- | :--- |
| `"stopped"` | No decoders connected, display blanked. |
| `"grid"` | Multi-tile composite on screen. |
| `"fullscreen"` | Single channel on screen (mainstream). |

### The invariant

> **Every code path that starts or stops the wall must move `current_mode`
> with it, in the same function that performs the action.**

This is not cosmetic bookkeeping — two subsystems make decisions from it:

1. `grid_watchdog()` re-asserts the decode roster on a timer whenever the mode
   is `"grid"`. If a stop leaves the mode stale, the watchdog reconnects every
   stream within seconds and silently undoes the shutdown. The wall then burns
   full decode CPU behind a blanked screen.
2. `apply_schedule_state()` decides whether a scheduled transition is needed by
   comparing against the mode. A stale mode makes the scheduled turn-on a
   silent no-op, leaving the display black for the entire window.

Both failures are silent and self-sustaining: nothing errors, nothing logs, and
the state cannot recover on its own. Ownership therefore lives in
`stop_streams()` / `launch_grid()` / `launch_fullscreen()` rather than in their
callers, so a new caller cannot forget it.

### Scheduling behaviour

- `sync_power_schedule()` polls every 10s and acts **on boundary crossings
  only**, not continuously. That is deliberate: a manual on/off from the
  dashboard is honoured for the remainder of the window instead of being
  reverted on the next poll.
- Immediately after a boundary there is a short confirmation window
  (`SCHEDULE_CONFIRM_SEC`) during which a transition that did not take effect
  is retried — this recovers a transition lost to a restarting compositor or a
  transient control-socket failure.
- Any manual dashboard action calls `cancel_schedule_confirm()`, so that retry
  can never fight a deliberate override.
- Both transitions log to stdout, so a wall in an unexpected state can be
  diagnosed from the service log rather than by inference.

### Optional nightly reboot

`systemd/dvr-kiosk-reboot.{timer,service}` are installed but **not enabled** —
whether an appliance reboots on a schedule is a site policy decision. Enable
with `systemctl enable --now dvr-kiosk-reboot.timer`, and set `OnCalendar` to a
time inside the display's off-window so nothing is on screen when it happens.

Treat it as a safety net, not a correctness mechanism. A nightly reboot masks
stale-state bugs (a fresh process starts at `"stopped"`) without fixing the
wasted resources they cause in between.
