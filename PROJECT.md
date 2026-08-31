# PROJECT.md — orientation for anyone picking this up

Start here. This is the "what is this and why is it like that" document.
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) has the technical detail,
[DEVELOPERS.md](DEVELOPERS.md) has the working practices, and
[AGENTS.md](AGENTS.md) is the brief for AI coding agents.

---

## What this is

A CCTV video wall appliance for low-power hardware. It drives a TV over HDMI
showing a grid of camera channels, plus a phone-friendly web dashboard for
layout editing and fullscreen viewing.

Three processes:

| Process | Language | Job |
| :--- | :--- | :--- |
| `go2rtc` | Go (vendored binary) | Talks camera protocols (DVRIP etc.), re-serves as local RTSP |
| `dvrwall` | C | Decodes streams, composites, blits to `/dev/fb0`, serves JPEG/MJPEG |
| `dvr_control.py` | Python (Flask + waitress) | Web dashboard, profiles, schedule, load governor |

## The constraint that explains most decisions

The reference target is a **single-board computer with ~4 weak ARM cores,
~512MB RAM, and no usable hardware video decode.** Almost every non-obvious
design choice traces back to that:

- **Why a custom C compositor instead of ffmpeg/mpv/a browser?** Not raw
  decode speed — libavcodec does the decoding either way. It's control over
  *concurrency* and *frame dropping*. The original ffmpeg approach opened its
  inputs sequentially (~8s each, ~130s for 16 tiles) and its filter graph
  buffered rather than dropped, so streams drifted tens of seconds behind and
  never recovered.
- **Why "latest frame wins" everywhere?** Every decoder keeps only its newest
  frame. Frames arriving faster than they're displayed are decoded and
  discarded, so sockets never back up and latency stays bounded to one frame
  period. Queuing anywhere in this pipeline turns into unbounded lag.
- **Why substreams for the grid and mainstream only for fullscreen?** One
  1080-line mainstream costs roughly as much CPU as all sixteen low-res
  substreams combined. Measured, not estimated.
- **Why is TLS not handled here?** Encryption on these cores competes with
  decoding. The app serves plain HTTP and expects an upstream reverse proxy to
  terminate TLS, honouring `X-Forwarded-Proto`.
- **Why so much attention to *transitions* rather than steady state?** Steady
  state is comfortable. The expensive moments are connection storms: opening a
  stream (RTSP handshake, `avformat_find_stream_info`, keyframe wait) costs far
  more than decoding it, and doing sixteen at once pegs every core.

## Hard-won facts worth not rediscovering

These were each established by measurement on real hardware, and several
contradict reasonable-sounding intuitions:

- **You cannot skip decoding frames.** The cameras emit Baseline-profile H.264
  (IPPP, no B-frames), so *every* frame is a reference frame. `AVDISCARD_NONREF`
  drops nothing, and skipping a P-frame corrupts everything after it. What you
  *can* skip is the post-decode `sws_scale` — worth ~45% of per-frame cost.
- **Decode dominates; display is nearly free.** Compositing all tiles to the
  framebuffer measures ~2% CPU against ~156% for the decoders. Throttling the
  display rate saves almost nothing, because by then the expensive work is
  already done.
- **Hardware decode is not available on the reference board.** The VPU exists
  and its driver loads, but exposes the *stateless* V4L2 Request API
  (`V4L2_PIX_FMT_H264_SLICE`). Stock ffmpeg's `h264_v4l2m2m` is a *stateful*
  wrapper and cannot drive it, and Debian's ffmpeg ships no `v4l2request`
  hwaccel. Using it means building a patched ffmpeg.
- **Thermal throttling is invisible in CPU-usage numbers.** A board sitting at
  93°C silently clamps its own clock ~22% below rated speed. Check
  `/sys/class/thermal/cooling_device*/cur_state` before concluding software is
  slow.
- **`ps` reports lifetime-average CPU, not instantaneous.** It will happily
  show a busy number for an idle process. Use `/proc/<pid>/stat` deltas.
- **Load average is a 1-minute lagging average.** It keeps climbing after a
  burst ends. For control loops, sample `/proc/stat` instead.

## Current state

Working and verified on hardware: the wall, fullscreen with real mainstream,
drag-and-drop layout editing, profiles, channel labels, the power schedule, the
CPU load governor, and the WebUI.

Known open items are tracked in [DEVELOPERS.md](DEVELOPERS.md#open-items).

## Non-goals

- Recording or playback — the DVRs already do that; this is a live wall.
- Motion detection, analytics, object detection.
- Cloud connectivity of any kind.
- Serving TLS itself (see above).
