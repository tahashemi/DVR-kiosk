# Hybrid Stream Engine, Fullscreen Transition, and Pool Status Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate black fullscreen modal, blank WebUI grid channels, and the "0/8 Online" pool display bug by implementing a high-performance hybrid stream engine with instant-paint fullscreen transitions and reachability-based status.

**Architecture:** WebUI Grid (≤16) and Pool (28+) use 1.5s in-place snapshot polling (`/api/snapshot/<dvr>/<ch>?t=...`) to avoid browser HTTP/1.1 6-connection limits. Fullscreen uses a dedicated persistent 1080p MJPEG stream with instant 2-tier fallback (<50ms paint) and asynchronous TV synchronization. Channel status decouples DVR network reachability from on-screen TV decode rosters.

**Tech Stack:** Python 3, Flask, Waitress, Vanilla JS (DOM & Sortable.js), dvrwall C Compositor, go2rtc RTSP.

## Global Constraints
- Target Hardware: Raspberry Pi 4 / CM4 (ARM target).
- Live Host: `192.168.1.100`.
- Python Files: `DVR-KIOSK-GIT/src/dvr_control.py`, `DVR-KIOSK-GIT/src/wall.py`.
- Git Tracking: `c:\Projects\DVR-kiosk\DVR-KIOSK-GIT`.

---

### Task 1: WebUI Grid & Channel Pool Stream Engine

**Files:**
- Modify: `DVR-KIOSK-GIT/src/dvr_control.py`

**Interfaces:**
- Consumes: `/api/snapshot/<dvr>/<ch>?t=<timestamp>`
- Produces: In-place thumbnail refresh loop `refreshAllThumbs()` without DOM teardown or persistent socket lockup.

- [ ] **Step 1: Update `tileEl` in `dvr_control.py` to use snapshot polling for all tiles**

Modify `tileEl` around line 880:
```javascript
  img.src = '/api/snapshot/' + c.dvr + '/' + c.ch + '?t=' + Date.now();
  img.onload = () => { 
    img.style.display = ''; 
    offline.style.display = 'none'; 
  };
  img.onerror = () => { 
    // Fall back gracefully without permanently breaking tile
    offline.style.display = 'block'; 
  };
```

- [ ] **Step 2: Implement unified `refreshAllThumbs()` in WebUI JS**

Replace `refreshPoolThumbs()` and kiosk grid live connections with a unified 1.5s timer:
```javascript
let thumbRefreshTimer = null;
const THUMB_REFRESH_INTERVAL_MS = 1500;

function refreshAllThumbs() {
  if (isWebFullscreenActive) return;
  const now = Date.now();
  const imgs = document.querySelectorAll('#kioskGrid img.live-thumb, #pool img.live-thumb');
  for (const img of imgs) {
    const tile = img.closest('.tile');
    if (!tile || !tile.dataset.dvr || !tile.dataset.ch) continue;
    img.src = '/api/snapshot/' + tile.dataset.dvr + '/' + tile.dataset.ch + '?t=' + now;
  }
}
```

- [ ] **Step 3: Wire `refreshAllThumbs()` interval in `init()`**

Ensure `setInterval(refreshAllThumbs, THUMB_REFRESH_INTERVAL_MS)` runs continuously in the background.

- [ ] **Step 4: Verify syntax**

Run: `python -m py_compile DVR-KIOSK-GIT/src/dvr_control.py`
Expected: Clean compilation with 0 errors.

---

### Task 2: Instant-Paint Fullscreen Transition & Hardware Synchronization

**Files:**
- Modify: `DVR-KIOSK-GIT/src/dvr_control.py`
- Modify: `DVR-KIOSK-GIT/src/wall.py`

**Interfaces:**
- Consumes: `/api/kiosk/fullscreen`, `/api/live/<dvr>/<ch>?main=1`, `/api/stream/<dvr>/<ch>/main.jpg`
- Produces: Instant 2-tier fallback fullscreen modal with 0ms black screen and asynchronous hardware TV switching.

- [ ] **Step 1: Optimize `launch_fullscreen` in `dvr_control.py`**

Ensure `launch_fullscreen` runs asynchronously without blocking the `/api/kiosk/fullscreen` HTTP endpoint, and handles dvrwall socket commands safely:
```python
@app.route('/api/kiosk/fullscreen', methods=['POST'])
@require_auth
def api_kiosk_fullscreen():
    body = request.get_json(force=True)
    dvr = body.get("dvr")
    ch = int(body.get("ch", 1))
    threading.Thread(target=launch_fullscreen, args=(dvr, ch), daemon=True).start()
    return jsonify({"ok": True})
```

- [ ] **Step 2: Implement 2-Tier Instant-Paint in WebUI `fullscreen(dvr, ch)`**

Update `fullscreen(dvr, ch)` in `dvr_control.py`:
```javascript
async function fullscreen(dvr, ch) {
  isWebFullscreenActive = true;
  const label = channelLabel(dvr, ch);

  const overlay = document.getElementById('webFullscreenOverlay');
  const title = document.getElementById('webFsTitle');
  const video = document.getElementById('webFsVideo');
  const img = document.getElementById('webFsImg');

  title.textContent = label + ' (HD Mainstream)';
  overlay.style.display = 'flex';
  video.style.display = 'none';
  img.style.display = 'block';

  // 1. Instant paint fallback preview frame (<50ms)
  const fallbackSrc = () => '/api/snapshot/' + dvr + '/' + ch + '?t=' + Date.now();
  const liveSrc = () => '/api/live/' + dvr + '/' + ch + '?main=1&t=' + Date.now();
  
  img.src = fallbackSrc();

  // 2. Trigger hardware TV fullscreen switch
  fetch('/api/kiosk/fullscreen', {
    method: 'POST', 
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({dvr, ch})
  }).catch(() => {});

  // 3. Connect to live 1080p MJPEG stream with retry
  clearTimeout(fsReconnectTimer);
  let liveAttemptActive = false;

  const tryConnectLive = () => {
    if (!isWebFullscreenActive) return;
    const probe = new Image();
    probe.onload = () => {
      if (!isWebFullscreenActive) return;
      img.src = probe.src;
    };
    probe.onerror = () => {
      if (!isWebFullscreenActive) return;
      clearTimeout(fsReconnectTimer);
      fsReconnectTimer = setTimeout(tryConnectLive, 1500);
    };
    probe.src = liveSrc();
  };

  fsReconnectTimer = setTimeout(tryConnectLive, 500);
  await refreshStatus();
}
```

- [ ] **Step 3: Update `closeWebFullscreen()`**

Ensure `closeWebFullscreen()` cleanly terminates live probes and issues `/api/kiosk/grid`:
```javascript
async function closeWebFullscreen() {
  isWebFullscreenActive = false;
  clearTimeout(fsReconnectTimer);
  const overlay = document.getElementById('webFullscreenOverlay');
  const img = document.getElementById('webFsImg');
  overlay.style.display = 'none';
  img.src = '';
  
  try {
    await fetch('/api/kiosk/grid', {method: 'POST'});
    setStatus('Returned to grid');
  } catch(e) {}
  await refreshStatus();
  refreshAllThumbs();
}
```

---

### Task 3: Pool Status Decoupling & Multi-DVR Accurate Online Count

**Files:**
- Modify: `DVR-KIOSK-GIT/src/dvr_control.py`

**Interfaces:**
- Consumes: `healthCache.dvrs`, `healthCache.streams`
- Produces: Accurate `onlineCount` and `getTileStatus()` across all DVRs.

- [ ] **Step 1: Update `getTileStatus(dvr, ch)` in `dvr_control.py`**

```javascript
function getTileStatus(dvr, ch) {
  const dvrInfo = (healthCache && healthCache.dvrs) ? healthCache.dvrs[dvr] : null;
  const streamInfo = (healthCache && healthCache.streams) ? healthCache.streams[dvr + '/' + ch] : null;

  const isReachable = dvrInfo ? dvrInfo.reachable : true;
  const isDecoding = streamInfo && streamInfo.connected && streamInfo.have_frame;

  if (!isReachable) {
    return { cls: 'offline', title: 'DVR Unreachable / Offline', online: false };
  }
  if (isDecoding) {
    return { cls: 'online', title: 'Live on TV (' + streamInfo.age_ms + 'ms)', online: true, liveTv: true };
  }
  return { cls: 'online', title: 'Ready (On-Demand)', online: true, liveTv: false };
}
```

- [ ] **Step 2: Update `renderPool()` header counts**

Ensure `onlineCount` counts all channels on reachable DVRs:
```javascript
      for (const c of byDvr[dvr]) {
        const st = getTileStatus(c.dvr, c.ch);
        if (st.online) onlineCount++;
        const inProfile = inProfileKeys.has(tileKey(c));
        if (currentPoolFilter === 'online' && !st.online) continue;
        if (currentPoolFilter === 'ingrid' && !inProfile) continue;
        if (currentPoolSearch) {
          const q = currentPoolSearch.toLowerCase();
          const lbl = (c.label || '').toLowerCase();
          if (!lbl.includes(q)) continue;
        }
        matchingChannels.push({ c, inProfile });
      }
```

- [ ] **Step 3: Verify Python syntax**

Run: `python -m py_compile DVR-KIOSK-GIT/src/dvr_control.py`
Expected: 0 errors.

---

### Task 4: Dynamic Adaptive FPS & CPU Load Governor (<80% Target)

**Files:**
- Modify: `DVR-KIOSK-GIT/src/dvr_control.py`
- Modify: `DVR-KIOSK-GIT/src/wall.py`

**Interfaces:**
- Consumes: `os.getloadavg()`, `wall.set_fps(target_fps)`
- Produces: Dynamic framerate tuning background loop `cpu_load_governor()`.

- [ ] **Step 1: Implement `set_fps(fps)` in `wall.py`**

```python
def set_fps(target_fps):
    """Dynamically adjust dvrwall compositor target FPS."""
    return _send(f"FPS {int(target_fps)}")
```

- [ ] **Step 2: Implement universal progressive `cpu_load_governor()` background worker in `dvr_control.py`**

```python
def get_normalized_cpu_load_pct():
    """Universal normalized CPU utilization across any OS and core count."""
    try:
        load1, _, _ = os.getloadavg()
        cores = os.cpu_count() or 1
        return (load1 / cores) * 100.0
    except Exception:
        return 0.0

def cpu_load_governor():
    """Dynamically scales TV compositor and WebUI polling FPS using a multi-tier ladder."""
    current_target_fps = 15
    while True:
        try:
            load_pct = get_normalized_cpu_load_pct()
            # Multi-tier progressive throttle ladder:
            if load_pct > 92.0:
                new_fps = 2    # Tier 5: Emergency survival mode
            elif load_pct > 88.0:
                new_fps = 5    # Tier 4: Heavy load
            elif load_pct > 80.0:
                new_fps = 8    # Tier 3: High load
            elif load_pct > 70.0:
                new_fps = 12   # Tier 2: Moderate load
            elif load_pct < 60.0:
                new_fps = 20   # Tier 1: Maximum performance

            if new_fps != current_target_fps:
                current_target_fps = new_fps
                wall.set_fps(current_target_fps)
        except Exception:
            pass
        time.sleep(8)
```

- [ ] **Step 3: Start `cpu_load_governor()` in `start_background_workers()`**

Ensure `threading.Thread(target=cpu_load_governor, daemon=True).start()` is spawned at startup.

---

### Task 5: Automated Verification, Pi Deployment, and Git Sync

**Files:**
- Deploy to: `/root/dvr_control.py` on Pi `192.168.1.100`
- Sync to: `c:\Projects\DVR-kiosk\dvr_control.py`

- [ ] **Step 1: Upload updated files to Raspberry Pi**

Upload `dvr_control.py` and `wall.py` using `pisftp.py` or paramiko.

- [ ] **Step 2: Restart services on Raspberry Pi**

Run: `systemctl restart dvr-kiosk.service`

- [ ] **Step 3: Test live HTTP endpoints**

Verify `/api/health`, `/api/kiosk/status`, and `/api/snapshot/dvr1/1` return HTTP 200.

- [ ] **Step 4: Commit and Push**

Commit changes to `DVR-KIOSK-GIT` and push to GitHub.
