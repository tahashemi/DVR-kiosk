# AGENTS.md — brief for AI coding agents

Read [PROJECT.md](PROJECT.md) first for context and
[DEVELOPERS.md](DEVELOPERS.md) for deploy/debug mechanics. This file covers
what agents specifically keep getting wrong on this codebase.

---

## The single most important thing

**This project runs on real hardware watching real premises. Measure before
you conclude, and verify on the device before you claim something works.**

Nearly every serious mistake in this repo's history came from confident
reasoning that was never checked against the machine. Several fixes were
applied to the wrong file entirely because the symptom was misattributed. The
tools to check are cheap and documented in DEVELOPERS.md — use them.

If you cannot verify something, say so plainly. "I changed X and it should
fix Y" is fine. "I fixed Y" without having watched Y stop happening is not.

## Constraints you must not violate

1. **Never skip decoding frames.** The cameras emit Baseline-profile H.264
   (IPPP, no B-frames): every frame is a reference frame. Dropping one
   corrupts everything downstream until the next keyframe. Throttle
   *post-decode* work (`sws_scale`) instead — that is what the STRIDE
   mechanism is for.
2. **Never tear down stream connections to change what's displayed.** Opening
   a stream costs far more than decoding it (RTSP handshake +
   `avformat_find_stream_info` + keyframe wait), and it forces `go2rtc` to
   re-establish its upstream camera connection too. Doing sixteen at once pegs
   every core and pushes the board into thermal throttling. Use `LAYOUT` /
   `FULLSCREEN` to change the view and `DECODE` to pause decoding while
   staying connected.
3. **Every path that starts or stops the wall must move `current_mode`.** See
   ARCHITECTURE §4. Violating this is silent and self-sustaining — it already
   caused a wall that stayed black all day while burning full CPU.
4. **Fullscreen must show the real mainstream.** It is easy to make fullscreen
   "efficient" by pointing the compositor at an already-decoded substream —
   that yields an upscaled low-resolution image and defeats the purpose.
   Verify by checking the served JPEG's actual dimensions.
5. **Don't queue frames anywhere.** Latest-frame-wins throughout. Any buffering
   becomes unbounded lag on this hardware.

## Measurement rules

- `ps` shows **lifetime-average** CPU. Use `/proc/<pid>/stat` deltas.
- Load average lags a full minute. Use `/proc/stat` deltas for anything
  reactive.
- Check `cooling_device*/cur_state` before concluding software is slow.
- `ping` is not a reachability test on the reference device — ICMP is
  filtered. Use SSH.
- Confirm what is actually deployed with `md5sum` on both sides; do not assume
  the repo matches the device.

## Git and secrets

- **Commits are authored by the repository owner only.** Do not add
  `Co-Authored-By`, "Generated with", or any agent attribution.
- **Never commit device specifics**: IPs, hostnames, passwords, camera names,
  site details. Docs describe mechanisms generically. Config with real values
  lives outside the repo or in gitignored files.
- Before committing, check what you staged. `git grep -iE
  "(password|secret|token)"` is a cheap sanity check.
- Scratch and test files go outside the repo directory entirely (see
  DEVELOPERS.md), not merely gitignored.

## Working style that fits this project

- **Change one thing at a time.** Several past bugs were self-inflicted by
  bundled changes where the regression could not be attributed.
- **Prefer the smallest fix at the actual root cause.** A recurring failure
  here was fixing symptoms in Python when the bug was in C, repeatedly, for
  the same underlying issue.
- **State uncertainty honestly and correct yourself.** If a measurement
  contradicts something you said earlier, say so and move on — a wrong
  diagnosis left standing costs more than the correction does.
- **Don't reset credentials, delete data, or reboot the device** to make a
  test convenient. Ask.
- Destructive or hard-to-reverse actions on a live device need confirmation
  first — this box is in service.

## Quick reference

```bash
echo "STATUS" | nc -U /run/dvrwall.sock | python3 -m json.tool   # wall truth
journalctl -u dvrwall.service | grep -c SEGV                     # stability
tail -20 /var/log/dvr_kiosk.log                                  # app log
cat /sys/class/thermal/cooling_device0/cur_state                 # 0 = healthy
```
