"""PTZ controls shared by the dashboard full view and the /wall solo view.

Injected as two strings (PTZ_CSS, PTZ_JS) so both pages use one
implementation. Usage from page JS:

    PTZ.attach(stageElement, cameraKey)   // stage must be position:relative
    PTZ.detach()

Interaction design:
  * Primary: press and drag on the picture. The press point becomes a virtual
    joystick; direction = drag direction, speed = drag distance. A plain
    tap/click does nothing, so nobody moves a camera by accident.
  * Zoom: mouse wheel, two-finger pinch, or + / - keys.
  * Secondary: a compact pad (8 directions, home, zoom, speed) that fades out
    after 3 s idle, for people who prefer buttons and for accessibility.
  * Presets as chips; "Save view" stores the current position; long-press a
    chip to rename or delete it.
  * The picture lags 1-2 s behind reality, so every control lights up the
    instant it is pressed and a "moving" pill shows until stop is confirmed.

Network discipline for this board: moves are sent at most 4/s, never queued
(only the newest velocity is ever sent), renewed about once a second while
held, and the server stops the camera itself if renewals stop arriving.
"""

PTZ_CSS = r"""
.ptz-layer { position:absolute; inset:0; z-index:20; touch-action:none; cursor:grab;
  -webkit-user-select:none; user-select:none; }
.ptz-layer.dragging { cursor:grabbing; }
.ptz-ring { position:absolute; width:120px; height:120px; margin:-60px 0 0 -60px; border-radius:50%;
  border:2px solid rgba(255,255,255,.45); background:rgba(0,0,0,.18); pointer-events:none;
  display:none; z-index:21; }
.ptz-ring.on { display:block; }
.ptz-ring i { position:absolute; left:50%; top:50%; width:18px; height:18px; margin:-9px 0 0 -9px;
  border-radius:50%; background:rgba(255,255,255,.9); box-shadow:0 0 12px rgba(0,0,0,.6); }
.ptz-ui { position:absolute; right:16px; bottom:16px; z-index:30; display:flex; flex-direction:column;
  align-items:flex-end; gap:10px; transition:opacity .35s; font:600 13px/1.2 system-ui,-apple-system,"Segoe UI",sans-serif;
  color:#fff; }
.ptz-ui.idle { opacity:0; pointer-events:none; }
.ptz-ui.disabled .ptz-pad, .ptz-ui.disabled .ptz-chips { opacity:.35; pointer-events:none; }
.ptz-chips { display:flex; flex-wrap:wrap; justify-content:flex-end; gap:6px; max-width:min(520px, 90vw); }
.ptz-chip { border:1px solid rgba(255,255,255,.25); background:rgba(20,20,24,.72); color:#fff;
  border-radius:999px; padding:7px 13px; font:inherit; cursor:pointer; backdrop-filter:blur(6px);
  -webkit-user-select:none; user-select:none; touch-action:manipulation; }
.ptz-chip:hover { background:rgba(60,60,70,.85); }
.ptz-chip.save { border-style:dashed; color:#ffd36b; }
.ptz-chip kbd { opacity:.55; font:inherit; margin-right:5px; }
.ptz-row { display:flex; align-items:center; gap:10px; }
.ptz-pad { position:relative; width:150px; height:150px; border-radius:50%;
  background:rgba(20,20,24,.72); border:1px solid rgba(255,255,255,.18); backdrop-filter:blur(6px);
  display:grid; grid-template:repeat(3,1fr)/repeat(3,1fr); padding:8px; box-sizing:border-box; }
.ptz-b { border:0; background:transparent; color:#fff; font-size:18px; border-radius:50%; cursor:pointer;
  display:flex; align-items:center; justify-content:center; touch-action:none;
  -webkit-user-select:none; user-select:none; transition:background .12s, transform .12s; }
.ptz-b:hover { background:rgba(255,255,255,.12); }
.ptz-b.on { background:#3b82f6; transform:scale(.92); }
.ptz-b.home { font-size:15px; background:rgba(255,255,255,.08); }
.ptz-side { display:flex; flex-direction:column; gap:8px; }
.ptz-side .ptz-b { width:44px; height:44px; background:rgba(20,20,24,.72);
  border:1px solid rgba(255,255,255,.18); backdrop-filter:blur(6px); font-size:20px; }
.ptz-side .ptz-b.small { font-size:12px; font-weight:700; }
.ptz-pill { position:absolute; left:50%; top:64px; transform:translateX(-50%); z-index:30;
  background:rgba(20,20,24,.8); color:#fff; padding:6px 14px; border-radius:999px;
  font:600 12px system-ui,sans-serif; display:none; backdrop-filter:blur(6px); pointer-events:none; }
.ptz-pill.on { display:block; }
.ptz-pill.warn { background:rgba(180,110,0,.9); }
.ptz-pill.err { background:rgba(190,40,40,.92); }
.ptz-help { position:absolute; right:16px; bottom:200px; z-index:31; background:rgba(20,20,24,.92);
  color:#ddd; border-radius:12px; padding:12px 16px; font:12px/1.7 system-ui,sans-serif; display:none;
  border:1px solid rgba(255,255,255,.15); }
.ptz-help.on { display:block; }
.ptz-help b { color:#fff; display:inline-block; min-width:72px; }
.ptz-badge { position:absolute; top:6px; right:6px; z-index:5; background:rgba(59,130,246,.85); color:#fff;
  font:700 10px system-ui,sans-serif; padding:2px 6px; border-radius:6px; letter-spacing:.04em; pointer-events:none; }
@media (hover:none) {
  .ptz-pad { width:168px; height:168px; }
  .ptz-side .ptz-b { width:52px; height:52px; }
  .ptz-chip { padding:10px 15px; }
}
@media (orientation:portrait) and (max-width:700px) {
  .ptz-ui { right:50%; transform:translateX(50%); align-items:center; bottom:20px; }
  .ptz-chips { justify-content:center; }
  .ptz-help { right:50%; transform:translateX(50%); bottom:250px; }
}
"""

PTZ_JS = r"""
var PTZ = (function () {
  var SPEEDS = [0.25, 0.5, 1.0], SPEED_NAMES = ['Slow', 'Normal', 'Fast'];
  var MIN_GAP = 250, RENEW = 1000, IDLE = 3000, DEADZONE = 12;
  var s = null;   /* active session */

  function el(tag, cls, html) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html != null) e.innerHTML = html;
    return e;
  }
  function esc(t) { return String(t).replace(/[&<>"]/g, function (c) {
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }
  function loadSpeed() { try { var r = localStorage.getItem('ptzSpeed'); var v = r === null ? 1 : +r; return v >= 0 && v < 3 ? v : 1; } catch (e) { return 1; } }
  function saveSpeed(v) { try { localStorage.setItem('ptzSpeed', v); } catch (e) {} }

  function api(path, body) {
    return fetch('/api/ptz/' + encodeURIComponent(s.key) + path, body === undefined ? {} : {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
        return j;
      });
    });
  }

  /* ---- pill / toast ---- */
  function pill(text, kind, ms) {
    if (!s) return;
    var p = s.pill;
    clearTimeout(s.pillTimer);
    p.textContent = text;
    p.className = 'ptz-pill on' + (kind ? ' ' + kind : '');
    if (ms) s.pillTimer = setTimeout(function () { if (s && s.pill === p) p.className = 'ptz-pill'; }, ms);
  }
  function clearPill() { if (s) { clearTimeout(s.pillTimer); s.pill.className = 'ptz-pill'; } }
  function fail(err) {
    if (!s) return;
    pill(err.message || 'Camera not responding', 'err', 4000);
    /* Back off: controls grey out, then a quiet re-check brings them back. */
    s.ui.classList.add('disabled');
    clearTimeout(s.retryTimer);
    s.retryTimer = setTimeout(function () { if (s) loadPresets(true); }, 5000);
  }

  /* ---- velocity engine: latest-wins, throttled, self-renewing ---- */
  function setVel(p, t, z) {
    if (!s) return;
    var v = s.want;
    if (Math.abs(v[0] - p) < 0.03 && Math.abs(v[1] - t) < 0.03 && Math.abs(v[2] - z) < 0.03 &&
        (p || t || z) === (v[0] || v[1] || v[2])) return;
    s.want = [p, t, z];
    pump();
  }
  function moving() { return !!(s && (s.want[0] || s.want[1] || s.want[2])); }

  function pump() {
    if (!s || s.inflight) { if (s) s.dirty = true; return; }
    clearTimeout(s.sendTimer);
    var wait = MIN_GAP - (Date.now() - s.lastSent);
    if (moving() && wait > 0) { s.sendTimer = setTimeout(pump, wait); return; }
    send();
  }
  function send() {
    var sess = s, v = s.want.slice(), stopping = !(v[0] || v[1] || v[2]);
    if (stopping && !sess.sentMoving) return;        /* already stopped */
    sess.inflight = true; sess.dirty = false; sess.lastSent = Date.now();
    clearTimeout(sess.renewTimer);
    if (!stopping) pill('Moving…', '', 0);
    var req = stopping ? api('/stop', {}) : api('/move', {pan: v[0], tilt: v[1], zoom: v[2]});
    req.then(function (j) {
      if (s !== sess) return;
      sess.sentMoving = !stopping;
      if (stopping) clearPill();
      else if (j.other_viewer) pill('Someone else is also moving this camera', 'warn', 3000);
    }, function (e) { if (s === sess) { sess.sentMoving = false; sess.want = [0, 0, 0]; fail(e); } })
    .then(function () {
      if (s !== sess) return;
      sess.inflight = false;
      if (sess.dirty) { pump(); return; }
      if (moving()) sess.renewTimer = setTimeout(function () { if (s === sess && moving()) send(); }, RENEW);
    });
  }
  function stopNow() { if (s) { s.want = [0, 0, 0]; pump(); setHeld(null); } }

  /* ---- UI activity / idle fade ---- */
  function wake() {
    if (!s) return;
    s.ui.classList.remove('idle');
    clearTimeout(s.idleTimer);
    s.idleTimer = setTimeout(function () {
      if (s && !moving() && !s.help.classList.contains('on')) s.ui.classList.add('idle');
    }, IDLE);
  }

  /* ---- pad buttons ---- */
  var DIRS = [['↖',-1,1],['↑',0,1],['↗',1,1],['←',-1,0],null,['→',1,0],['↙',-1,-1],['↓',0,-1],['↘',1,-1]];
  function setHeld(btn) {
    if (!s) return;
    if (s.held && s.held !== btn) s.held.classList.remove('on');
    s.held = btn;
    if (btn) btn.classList.add('on');
  }
  function holdButton(btn, vec) {
    btn.addEventListener('pointerdown', function (e) {
      e.preventDefault(); e.stopPropagation();
      try { btn.setPointerCapture(e.pointerId); } catch (x) {}
      var k = SPEEDS[s.speed];
      setHeld(btn); wake();
      setVel(vec[0] * k, vec[1] * k, vec[2] * k);
    });
    var end = function (e) { if (s && s.held === btn) { e.stopPropagation(); stopNow(); } };
    btn.addEventListener('pointerup', end);
    btn.addEventListener('pointercancel', end);
    btn.addEventListener('lostpointercapture', end);
  }

  function buildPad() {
    var pad = el('div', 'ptz-pad');
    DIRS.forEach(function (d) {
      if (!d) {
        var h = el('button', 'ptz-b home', '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 11l9-7 9 7"/><path d="M5 10v10h14V10"/></svg>');
        h.title = 'Home position (H)';
        h.addEventListener('click', function (e) { e.stopPropagation(); goHome(); });
        pad.appendChild(h);
        return;
      }
      var b = el('button', 'ptz-b', d[0]);
      b.setAttribute('aria-label', 'Move ' + d[0]);
      holdButton(b, [d[1] / (d[1] && d[2] ? 1.2 : 1), d[2] / (d[1] && d[2] ? 1.2 : 1), 0]);
      pad.appendChild(b);
    });
    return pad;
  }

  function buildSide() {
    var side = el('div', 'ptz-side');
    var zin = el('button', 'ptz-b', '+'); zin.title = 'Zoom in (+)';
    var zout = el('button', 'ptz-b', '−'); zout.title = 'Zoom out (−)';
    holdButton(zin, [0, 0, 1]); holdButton(zout, [0, 0, -1]);
    var sp = el('button', 'ptz-b small', ''); sp.title = 'Movement speed';
    function paint() { sp.textContent = SPEED_NAMES[s.speed]; }
    sp.addEventListener('click', function (e) {
      e.stopPropagation(); s.speed = (s.speed + 1) % 3; saveSpeed(s.speed); paint(); wake();
    });
    var q = el('button', 'ptz-b small', '?'); q.title = 'Shortcuts';
    q.addEventListener('click', function (e) { e.stopPropagation(); s.help.classList.toggle('on'); wake(); });
    side.appendChild(zin); side.appendChild(zout); side.appendChild(sp); side.appendChild(q);
    s.paintSpeed = paint;
    return side;
  }

  /* ---- presets ---- */
  function loadPresets(quiet) {
    var sess = s;
    return api('/presets').then(function (j) {
      if (s !== sess) return;
      sess.ui.classList.remove('disabled');
      sess.presets = j.presets || [];
      renderChips();
    }, function (e) { if (s === sess && !quiet) fail(e); else if (s === sess) {
      clearTimeout(sess.retryTimer);
      sess.retryTimer = setTimeout(function () { if (s === sess) loadPresets(true); }, 10000);
    } });
  }
  function renderChips() {
    var c = s.chips; c.innerHTML = '';
    s.presets.forEach(function (p, i) {
      var b = el('button', 'ptz-chip', (i < 9 ? '<kbd>' + (i + 1) + '</kbd>' : '') + esc(p.name));
      var timer = null, longFired = false;
      b.addEventListener('pointerdown', function (e) {
        e.stopPropagation(); longFired = false;
        timer = setTimeout(function () { longFired = true; editPreset(p); }, 600);
      });
      var cancel = function () { clearTimeout(timer); };
      b.addEventListener('pointerup', cancel); b.addEventListener('pointerleave', cancel);
      b.addEventListener('contextmenu', function (e) { e.preventDefault(); });
      b.addEventListener('click', function (e) {
        e.stopPropagation();
        if (!longFired) gotoPreset(p);
      });
      c.appendChild(b);
    });
    var save = el('button', 'ptz-chip save', '★ Save view');
    save.title = 'Save the current position as a preset';
    save.addEventListener('click', function (e) {
      e.stopPropagation();
      var name = prompt('Name this view (e.g. Gate, Parking):');
      if (!name || !name.trim()) return;
      api('/presets', {action: 'save', name: name.trim()}).then(function () {
        pill('Saved “' + name.trim() + '”', '', 2000); loadPresets();
      }, fail);
    });
    c.appendChild(save);
  }
  function gotoPreset(p) {
    stopNow();
    pill('Going to ' + p.name + '…', '', 2500);
    api('/presets', {action: 'goto', token: p.token}).catch(fail);
  }
  function goHome() {
    stopNow(); pill('Going home…', '', 2500);
    api('/home', {}).catch(fail);
  }
  function editPreset(p) {
    var name = prompt('Rename “' + p.name + '”\n(clear the name to delete this view)', p.name);
    if (name === null) return;
    if (!name.trim()) {
      if (!confirm('Delete view “' + p.name + '”?')) return;
      api('/presets', {action: 'remove', token: p.token}).then(function () { loadPresets(); }, fail);
    } else if (name.trim() !== p.name) {
      api('/presets', {action: 'save', name: name.trim(), token: p.token}).then(function () { loadPresets(); }, fail);
    }
  }

  /* ---- drag-on-picture joystick + pinch + wheel ---- */
  function bindStage() {
    var layer = s.layer, ring = s.ring, knob = ring.firstChild;
    var pts = {}, origin = null, active = false, pinch0 = 0;

    function radius() { var r = layer.getBoundingClientRect(); return Math.max(60, Math.min(r.width, r.height) / 5); }

    layer.addEventListener('pointerdown', function (e) {
      if (e.pointerType === 'mouse' && e.button !== 0) return;
      pts[e.pointerId] = {x: e.clientX, y: e.clientY};
      try { layer.setPointerCapture(e.pointerId); } catch (x) {}
      var ids = Object.keys(pts);
      if (ids.length === 1) {
        var r = layer.getBoundingClientRect();
        origin = {x: e.clientX, y: e.clientY, rx: e.clientX - r.left, ry: e.clientY - r.top};
        active = false;
      } else if (ids.length === 2) {
        /* second finger: switch from pan to pinch-zoom */
        var a = pts[ids[0]], b = pts[ids[1]];
        pinch0 = Math.hypot(a.x - b.x, a.y - b.y);
        ring.classList.remove('on'); active = false; origin = null;
        setVel(0, 0, 0);
      }
      wake();
    });
    layer.addEventListener('pointermove', function (e) {
      wake();
      if (!pts[e.pointerId]) return;
      pts[e.pointerId] = {x: e.clientX, y: e.clientY};
      var ids = Object.keys(pts), k = SPEEDS[s.speed];
      if (ids.length === 2 && pinch0) {
        var a = pts[ids[0]], b = pts[ids[1]];
        var ratio = Math.hypot(a.x - b.x, a.y - b.y) / pinch0;
        var z = ratio > 1.08 ? k : ratio < 0.92 ? -k : 0;
        setVel(0, 0, z);
        return;
      }
      if (!origin) return;
      var dx = e.clientX - origin.x, dy = e.clientY - origin.y, d = Math.hypot(dx, dy);
      if (!active && d < DEADZONE) return;          /* a tap is not a move */
      if (!active) {
        active = true; layer.classList.add('dragging');
        ring.style.left = origin.rx + 'px'; ring.style.top = origin.ry + 'px';
        ring.classList.add('on');
      }
      var R = radius(), m = Math.min(1, d / R);
      var ux = d ? dx / d : 0, uy = d ? dy / d : 0;
      knob.style.transform = 'translate(' + (ux * m * 50) + 'px,' + (uy * m * 50) + 'px)';
      /* gentle curve: fine control near the centre, full speed at the edge */
      var sp = m * m * k;
      setVel(+(ux * sp).toFixed(2), +(-uy * sp).toFixed(2), 0);
    });
    function up(e) {
      delete pts[e.pointerId];
      if (Object.keys(pts).length === 0) {
        origin = null; pinch0 = 0;
        if (active || moving()) stopNow();
        active = false; layer.classList.remove('dragging'); ring.classList.remove('on');
        knob.style.transform = '';
      }
    }
    layer.addEventListener('pointerup', up);
    layer.addEventListener('pointercancel', up);

    var wheelTimer = null;
    layer.addEventListener('wheel', function (e) {
      e.preventDefault(); wake();
      var k = SPEEDS[s.speed];
      setVel(0, 0, e.deltaY < 0 ? k : -k);
      clearTimeout(wheelTimer);
      wheelTimer = setTimeout(stopNow, 300);
    }, {passive: false});
  }

  /* ---- keyboard ---- */
  var KEYS = {ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, 1], ArrowDown: [0, -1]};
  function keyVel() {
    var p = 0, t = 0, k = SPEEDS[s.speed];
    for (var key in s.keys) if (KEYS[key]) { p += KEYS[key][0]; t += KEYS[key][1]; }
    var z = (s.keys['+'] || s.keys['=']) ? 1 : (s.keys['-'] || s.keys['_']) ? -1 : 0;
    setVel(p * k, t * k, z * k);
  }
  function onKey(e) {
    if (!s || e.ctrlKey || e.metaKey || e.altKey) return;
    var tag = (e.target.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select') return;
    var k = e.key, down = e.type === 'keydown';
    if (KEYS[k] || k === '+' || k === '=' || k === '-' || k === '_') {
      e.preventDefault(); wake();
      if (down) { if (s.keys[k]) return; s.keys[k] = 1; } else delete s.keys[k];
      keyVel();
    } else if (down && (k === 'h' || k === 'H')) { goHome(); }
    else if (down && /^[1-9]$/.test(k) && s.presets[+k - 1]) { gotoPreset(s.presets[+k - 1]); }
  }

  /* ---- safety: anything that loses the user's attention stops the camera ---- */
  function onBlur() { if (s) { s.keys = {}; if (moving()) stopNow(); } }
  function onVis() { if (document.hidden) onBlur(); }
  function onOut(e) { if (!e.relatedTarget && s && s.held == null && moving() && !Object.keys(s.keys).length) stopNow(); }

  function attach(stage, key) {
    detach();
    s = {key: key, stage: stage, want: [0, 0, 0], lastSent: 0, inflight: false, dirty: false,
         sentMoving: false, speed: loadSpeed(), presets: [], keys: {}, held: null};
    s.layer = el('div', 'ptz-layer');
    s.ring = el('div', 'ptz-ring', '<i></i>');
    s.pill = el('div', 'ptz-pill');
    s.help = el('div', 'ptz-help',
      '<b>Drag</b> on the picture to look around<br>' +
      '<b>Wheel / pinch</b> zoom<br>' +
      '<b>Arrows</b> move &nbsp; <b>+ / −</b> zoom<br>' +
      '<b>H</b> home &nbsp; <b>1–9</b> saved views<br>' +
      '<b>Hold a view</b> to rename or delete it');
    s.ui = el('div', 'ptz-ui');
    s.chips = el('div', 'ptz-chips');
    var row = el('div', 'ptz-row');
    row.appendChild(buildPad()); row.appendChild(buildSide());
    s.ui.appendChild(s.chips); s.ui.appendChild(row);
    s.paintSpeed();
    [s.layer, s.ring, s.pill, s.help, s.ui].forEach(function (n) { stage.appendChild(n); });
    s.ui.addEventListener('pointerdown', function (e) { e.stopPropagation(); wake(); });
    bindStage();
    document.addEventListener('keydown', onKey);
    document.addEventListener('keyup', onKey);
    window.addEventListener('blur', onBlur);
    document.addEventListener('visibilitychange', onVis);
    document.addEventListener('mouseout', onOut);
    document.addEventListener('mousemove', wake);
    wake();
    renderChips();
    loadPresets();
  }

  function detach() {
    if (!s) return;
    var sess = s;
    if (sess.sentMoving || moving()) {
      fetch('/api/ptz/' + encodeURIComponent(sess.key) + '/stop', {method: 'POST',
        headers: {'Content-Type': 'application/json'}, body: '{}', keepalive: true}).catch(function () {});
    }
    [sess.sendTimer, sess.renewTimer, sess.idleTimer, sess.pillTimer, sess.retryTimer].forEach(clearTimeout);
    [sess.layer, sess.ring, sess.pill, sess.help, sess.ui].forEach(function (n) { if (n && n.parentNode) n.parentNode.removeChild(n); });
    document.removeEventListener('keydown', onKey);
    document.removeEventListener('keyup', onKey);
    window.removeEventListener('blur', onBlur);
    document.removeEventListener('visibilitychange', onVis);
    document.removeEventListener('mouseout', onOut);
    document.removeEventListener('mousemove', wake);
    s = null;
  }

  /* Ask the server whether a camera has PTZ; resolves true/false, never throws. */
  function supported(key) {
    return fetch('/api/ptz/' + encodeURIComponent(key) + '/presets')
      .then(function (r) { return r.ok; }, function () { return false; });
  }

  return {attach: attach, detach: detach, supported: supported,
          active: function () { return s ? s.key : null; }};
})();
"""
