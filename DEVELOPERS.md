# DEVELOPERS.md — working on this thing

Practical guide: how to change something, get it onto a device, and prove it
worked. For *why* the system is shaped this way read
[PROJECT.md](PROJECT.md); for build commands and coding standards see
[docs/DEVELOPER_GUIDE.md](docs/DEVELOPER_GUIDE.md); for the wall state machine
see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Repository layout

The git repository is intentionally **one subfolder** of a larger working
directory, so scratch files and deploy helpers cannot be committed by accident:

```
<workdir>/
  DVR-KIOSK-GIT/   <- this repo; the only thing that is pushed
  tools/           <- deploy/SSH helpers (device credentials live here)
  scratch/         <- test scripts, measurements, fetched copies of live files
```

Keep it that way. Anything holding device credentials, live config, or
throwaway test output belongs in `tools/` or `scratch/`, never in the repo.

## Deploying to a device

There is no CI. You copy files to the box, restart the unit, and verify.

```bash
# Python: no build step
python3 -m py_compile src/dvr_control.py     # always, before copying
scp src/dvr_control.py root@<device>:/root/dvr_control.py
ssh root@<device> 'systemctl restart dvr-kiosk.service'

# C: build ON the device (matches its ffmpeg ABI)
scp src/dvrwall.c root@<device>:/root/dvrwall/dvrwall.c
ssh root@<device> 'cd /root/dvrwall && make'
ssh root@<device> 'systemctl stop dvrwall.service \
    && cp dvrwall /usr/local/bin/dvrwall \
    && systemctl start dvrwall.service'
```

**`cp` to `/usr/local/bin/dvrwall` fails with "Text file busy" unless the
service is stopped first.** Stop, copy, start — in that order.

### Transfers over an unreliable link

Copying a large file to a device on flaky Wi-Fi has bitten this project hard,
and the failure is nastier than it looks: **a dropped SFTP transfer leaves the
remote file truncated to zero bytes**, so `dvr_control.py` becomes an empty
file and the service cannot start at all. Observed dying consistently around
32KB of a 90KB file.

The obvious workaround — a fresh connection per chunk — trips sshd's
`MaxStartups` and fails with `Error reading SSH protocol banner`. (Check
`fail2ban-client status sshd` to confirm that is throttling and not a ban.)

`tools/piput.py` handles both: it reuses one connection for as long as it
survives, reconnects with a delay only on an actual drop, resumes rather than
restarting, and assembles remotely with an md5 check so the destination is
either the old file or the complete new one — never partial. Use it for
anything larger than a few KB, or just fix the network path first.

## Debugging techniques that actually work here

### Talk to the compositor directly

`STATUS` is the ground truth for what the wall is doing — far better than
inferring from CPU or from the dashboard:

```bash
echo "STATUS" | nc -U /run/dvrwall.sock | python3 -m json.tool
```

Key fields: `blanked`, per-stream `connected` / `paused` / `have_frame` /
`age_ms` / `frames`. Rising `age_ms` means a decoder is falling behind;
`frames` deltas over a known interval give real per-stream fps.

### Measure CPU correctly

- **`ps` reports lifetime-average CPU**, so it happily shows a big number for a
  process that is currently idle. Use `/proc/<pid>/stat` deltas (fields 14/15)
  over a known interval.
- **Load average lags by a minute.** A real four-core spike may barely move it
  for 10s. Sample `/proc/stat` deltas for anything reactive.
- **Per-thread breakdown** via `/proc/<pid>/task/*/stat` is how you separate
  decode cost from compositing cost. Thread names are not set, so identify
  threads by their CPU profile: the decoders are the many similar high-CPU
  ones.

### Check thermals before blaming code

```bash
cat /sys/class/thermal/thermal_zone0/temp        # millidegrees
cat /sys/class/thermal/cooling_device0/cur_state # 0 == not throttled
cat /sys/devices/system/cpu/cpufreq/policy0/scaling_cur_freq
```

A hot board silently clamps its clock and everything looks mysteriously slow.

### Verify what's on the TV without a TV

`/dev/fb0` is readable. Hash a region repeatedly to measure the real update
rate. Two caveats learned the hard way: sample a *large* region (a small window
on a static camera scene produces byte-identical frames and reads as "no
update"), and remember a tile only changes when its decoder produces a new
frame.

### Confirm what's deployed

Do not assume the repo matches the device. `md5sum` both sides — it is far
cheaper than transferring the file to diff it.

## Verifying a change

Whatever you touched, confirm it on hardware. Useful checks:

- **No regression sweep**: grid renders, double-click fullscreen shows *HD*
  (not an upscaled substream — check the served JPEG's dimensions), exit
  returns to grid, drag-and-drop reorders, profiles switch, schedule fires.
- **Stability**: `journalctl -u dvrwall.service | grep -c SEGV` stays 0 across
  a soak, and the PID does not change.
- **Schedule**: temporarily set `on_time` / `off_time` a couple of minutes out
  (back up `kiosk_schedule.json` first), then watch `STATUS` across both
  boundaries. Off must reach `streams=0` **and stay there** — if streams come
  back, the mode invariant is broken again (see ARCHITECTURE §4).

## Open items

- **Manual-override behaviour is unverified since the schedule fix.** The
  intent is that a manual dashboard on/off is honoured until the next boundary.
  The logic is written to preserve it (`api_kiosk_stop()` deliberately does not
  touch `scheduler_last_state`), but it has not been exercised end-to-end
  because the WebUI admin password is not known — see below.
- **Admin password is unknown on the reference device.** `set_password.py`
  generates a random initial password when `auth_config.json` is missing, so a
  redeploy can silently change it. Reset via the `dvr-kiosk` CLI on the box.
- **TLS termination is not yet configured upstream.** Until an upstream proxy
  terminates it, dashboard traffic including login is plaintext on the LAN.
- **Kernel update pending a reboot** on the reference device.
- **Wi-Fi link on the reference device flaps** (`CTRL-EVENT-BEACON-LOSS`),
  which is what makes large transfers unreliable.
- **Hardware decode remains unused** — would require a patched ffmpeg with the
  v4l2-request hwaccel (see PROJECT.md).
