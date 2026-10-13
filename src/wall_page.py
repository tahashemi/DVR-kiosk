"""The /wall page: a browser mirror of whatever the TV is currently showing.

Kept in its own module because it is a self-contained view with no server
logic of its own -- dvr_control.py renders WALL_HTML and serves the data it
asks for via /api/wall/layout.

Design notes that matter on this hardware:

* Frames arrive as polled <img> requests, not MJPEG. A 16-tile MJPEG view
  would hold 16 of waitress's 16 worker threads open for as long as the page
  is up, which starves the dashboard for every other client. Polling costs one
  short-lived request per frame instead.
* Each tile issues its next request from the previous request's load/error
  handler, never from a free-running timer. A slow board therefore yields a
  lower frame rate rather than an ever-growing queue of in-flight requests --
  the same latest-frame-wins rule the compositor uses internally.
* Tiles that are not on screen (the other portrait page, or any tile while the
  tab is backgrounded) drop their src and stop polling entirely.
* The poll rate follows the CPU governor's published thumb fps, so when the
  board heats up the browser backs off in step with the encoder.
"""

WALL_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover, user-scalable=no">
<meta name="theme-color" content="#000000">
<title>Video Wall</title>
<style>
  :root { --gap: 2px; --chrome: rgba(10,12,16,.82); --fg: #e8eaed; }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html, body {
    margin: 0; padding: 0; height: 100%; overflow: hidden;
    background: #000; color: var(--fg);
    font: 13px/1.35 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  }
  body.idle { cursor: none; }

  #stage { position: fixed; inset: 0; display: flex; }
  #pages { display: flex; width: 100%; height: 100%; }
  .page {
    flex: 0 0 100%; width: 100%; height: 100%;
    display: grid; gap: var(--gap); padding: var(--gap);
    grid-template-columns: repeat(var(--cols), 1fr);
    grid-template-rows: repeat(var(--rows), 1fr);
  }

  .tile { position: relative; background: #06070a; overflow: hidden; border-radius: 2px; }
  .tile img { width: 100%; height: 100%; display: block; object-fit: contain; background: #06070a; }
  .tile img:not(.ready) { opacity: 0; }
  .tile .ph {
    position: absolute; inset: 0; display: flex; flex-direction: column;
    align-items: center; justify-content: center; gap: 6px;
    color: #5b6270; font-size: 11px; letter-spacing: .04em; text-align: center; padding: 8px;
  }
  .tile img.ready ~ .ph { display: none; }
  .tile .ph .dot { width: 7px; height: 7px; border-radius: 50%; background: #39404d; }
  .tile.offline .ph .dot { background: #8a4b52; }

  .cap {
    position: absolute; left: 0; right: 0; bottom: 0;
    padding: 10px 7px 4px; font-size: 11px; font-weight: 500;
    color: #dfe3e8; text-shadow: 0 1px 3px #000;
    background: linear-gradient(transparent, rgba(0,0,0,.6));
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    opacity: 0; transition: opacity .18s; pointer-events: none;
  }
  body.show-labels .cap { opacity: 1; }

  @media (orientation: portrait) {
    #pages {
      overflow-x: auto; overflow-y: hidden;
      scroll-snap-type: x mandatory;
      -webkit-overflow-scrolling: touch;
      scrollbar-width: none;
    }
    #pages::-webkit-scrollbar { display: none; }
    .page { scroll-snap-align: start; scroll-snap-stop: always; }
    .cap { font-size: 10px; padding-bottom: 3px; }
  }

  body.solo .cap { font-size: 14px; }

  #bar {
    position: fixed; top: 0; left: 0; right: 0; z-index: 20;
    display: flex; align-items: center; gap: 12px;
    padding: 8px 12px; padding-top: calc(8px + env(safe-area-inset-top));
    background: var(--chrome); backdrop-filter: blur(8px);
    border-bottom: 1px solid rgba(255,255,255,.07);
    transform: translateY(-110%); transition: transform .22s ease;
  }
  body.chrome #bar { transform: none; }
  #bar .sp { flex: 1; }
  #bar b { font-weight: 600; letter-spacing: .02em; }
  #bar .meta { color: #8b93a1; font-size: 11px; font-variant-numeric: tabular-nums; }
  #bar a, #bar button {
    appearance: none; border: 1px solid rgba(255,255,255,.14);
    background: rgba(255,255,255,.06); color: var(--fg);
    border-radius: 7px; padding: 6px 11px; font: inherit; font-size: 12px;
    cursor: pointer; text-decoration: none;
  }
  #bar button:hover, #bar a:hover { background: rgba(255,255,255,.12); }

  /* On a phone the bar must stay one line -- wrapping pushes it over the top
     row of tiles. The title is the first thing to go, then the meta text. */
  @media (max-width: 620px) {
    #bar { gap: 8px; padding: 6px 10px; padding-top: calc(6px + env(safe-area-inset-top)); }
    #bar b { display: none; }
    #bar .meta { font-size: 10px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    #bar a, #bar button { padding: 5px 9px; font-size: 11px; }
  }
  @media (max-width: 400px) {
    #bar .meta { display: none; }
  }

  #dots {
    position: fixed; z-index: 20; left: 0; right: 0;
    bottom: calc(10px + env(safe-area-inset-bottom));
    display: none; justify-content: center; gap: 7px; pointer-events: none;
  }
  @media (orientation: portrait) { body.multipage #dots { display: flex; } }
  #dots i {
    width: 6px; height: 6px; border-radius: 50%;
    background: rgba(255,255,255,.28); transition: background .2s, width .2s;
  }
  #dots i.on { background: #fff; width: 16px; border-radius: 3px; }

  #msg {
    position: fixed; inset: 0; z-index: 30; display: none;
    align-items: center; justify-content: center; text-align: center;
    background: #000; color: #7d8592; padding: 24px; font-size: 14px;
  }
  #msg.on { display: flex; }
  {% raw %}/*PTZ_CSS*/{% endraw %}
</style>
</head>
<body>

<div id="bar">
  <b>Video Wall</b>
  <span class="meta" id="meta"></span>
  <span class="sp"></span>
  <button id="btnLabels" type="button">Labels</button>
  <button id="btnFs" type="button">Fullscreen</button>
  <a href="/">Dashboard</a>
</div>

<div id="stage"><div id="pages"></div></div>
<div id="dots"></div>
<div id="msg"></div>

<script>
"use strict";
{% raw %}/*PTZ_JS*/{% endraw %}

var PAGES = document.getElementById('pages');
var DOTS  = document.getElementById('dots');
var MSG   = document.getElementById('msg');
var META  = document.getElementById('meta');

var tiles = [];
var perPage = 16;
var solo = null;
var thumbFps = 3;
var curPage = 0;
var sig = '';

function isPortrait() {
  return window.matchMedia('(orientation: portrait)').matches;
}

/* Polling. The next request is issued from the previous one's handler, never
   from a free-running timer, so a slow board lowers the frame rate instead of
   accumulating in-flight requests. */
function schedule(t) {
  if (t.timer) { clearTimeout(t.timer); t.timer = null; }
  if (!t.visible || document.hidden) return;
  var period = 1000 / Math.max(1, thumbFps);
  var wait = Math.max(0, period - (Date.now() - t.startedAt));
  t.timer = setTimeout(function () { fetchFrame(t); }, wait);
}

/* Double-buffered: the frame is fetched into a detached Image first and only
   swapped into the visible <img> once it has decoded. Assigning src directly
   blanks the element until the new frame arrives, which at these frame rates
   means a tile that is permanently mid-load and never paints at all. */
function fetchFrame(t) {
  if (!t.visible || document.hidden) return;
  t.startedAt = Date.now();

  var url = '/api/snapshot/' + encodeURIComponent(t.dvr) + '/' + t.ch + '?t=' + t.startedAt;
  var loader = new Image();
  loader.decoding = 'async';
  t.loader = loader;

  loader.onload = function () {
    if (t.loader !== loader || !t.visible) return;   /* superseded or hidden */
    t.img.src = loader.src;                          /* already cached: instant */
    onFrame(t, true);
  };
  loader.onerror = function () {
    if (t.loader !== loader) return;
    onFrame(t, false);
  };
  loader.src = url;
}

function onFrame(t, ok) {
  if (ok) {
    t.img.classList.add('ready');
    t.el.classList.remove('offline');
    t.fails = 0;
  } else {
    t.fails++;
    if (t.fails > 2) {
      t.el.classList.add('offline');
      t.img.classList.remove('ready');
      /* Back a failing channel off rather than retrying at full rate: an
         unreachable DVR must not cost as much as a working one. */
      t.startedAt = Date.now() + Math.min(8000, t.fails * 1000);
    }
  }
  schedule(t);
}

/* Only tiles the user is actually looking at poll. In portrait that leaves
   half the wall idle at any moment, which is the point of paginating rather
   than shrinking every tile. */
function updateVisibility() {
  for (var i = 0; i < tiles.length; i++) {
    var t = tiles[i];
    var want = !document.hidden && (solo ? t.isSolo : (t.page === curPage));
    if (want && !t.visible) {
      t.visible = true; t.startedAt = 0; fetchFrame(t);
    } else if (!want && t.visible) {
      t.visible = false;
      if (t.timer) { clearTimeout(t.timer); t.timer = null; }
      t.loader = null;                    /* orphan any in-flight fetch */
      t.img.removeAttribute('src');
      t.img.classList.remove('ready');
    }
  }
}

function gridShape(n) {
  var cols = Math.ceil(Math.sqrt(n));
  return { cols: cols, rows: Math.ceil(n / cols) };
}

function makeTile(spec, page, isSolo) {
  var el = document.createElement('div');
  el.className = 'tile';

  var img = document.createElement('img');
  img.alt = spec.label;
  img.decoding = 'async';

  var ph = document.createElement('div');
  ph.className = 'ph';
  var dot = document.createElement('span'); dot.className = 'dot';
  var nm = document.createElement('span'); nm.textContent = spec.label;
  ph.appendChild(dot); ph.appendChild(nm);

  var cap = document.createElement('div');
  cap.className = 'cap';
  cap.textContent = spec.label;

  el.appendChild(img); el.appendChild(ph); el.appendChild(cap);
  if (spec.ptz && !isSolo) {
    var badge = document.createElement('div');
    badge.className = 'ptz-badge';
    badge.textContent = 'PTZ';
    el.appendChild(badge);
  }

  var t = {
    dvr: spec.dvr, ch: spec.ch, label: spec.label,
    el: el, img: img, page: page, isSolo: isSolo,
    visible: false, timer: null, loader: null, startedAt: 0, fails: 0
  };
  return t;
}

function build(data) {
  PTZ.detach();
  for (var i = 0; i < tiles.length; i++) if (tiles[i].timer) clearTimeout(tiles[i].timer);
  tiles = [];
  PAGES.innerHTML = '';
  DOTS.innerHTML = '';
  curPage = 0;
  PAGES.scrollLeft = 0;

  var list = data.tiles || [];

  solo = null;
  if (data.mode === 'fullscreen' && data.fullscreen) {
    var fs = data.fullscreen;
    for (var j = 0; j < list.length; j++) {
      if (list[j].dvr === fs.dvr && list[j].ch === fs.ch) { solo = list[j]; break; }
    }
    if (!solo) solo = { dvr: fs.dvr, ch: fs.ch, label: fs.label || (fs.dvr + ' CH' + fs.ch), online: true, ptz: !!fs.ptz };
  }
  document.body.classList.toggle('solo', !!solo);

  if (data.mode === 'stopped' || (!list.length && !solo)) {
    MSG.textContent = 'The wall is off. Turn it on from the dashboard.';
    MSG.classList.add('on');
    document.body.classList.remove('multipage');
    return;
  }
  MSG.classList.remove('on');

  if (solo) {
    var page = document.createElement('div');
    page.className = 'page';
    page.style.setProperty('--cols', 1);
    page.style.setProperty('--rows', 1);
    var st = makeTile(solo, 0, true);
    tiles.push(st);
    page.appendChild(st.el);
    PAGES.appendChild(page);
    document.body.classList.remove('multipage');
    updateVisibility();
    /* The TV is showing one PTZ camera: this view becomes its remote control. */
    if (solo.ptz) PTZ.attach(st.el, solo.dvr);
    return;
  }

  perPage = isPortrait() ? 8 : list.length;
  var pageCount = Math.max(1, Math.ceil(list.length / perPage));
  document.body.classList.toggle('multipage', pageCount > 1);

  for (var p = 0; p < pageCount; p++) {
    var slice = list.slice(p * perPage, (p + 1) * perPage);
    var pg = document.createElement('div');
    pg.className = 'page';
    var shape = isPortrait()
      ? { cols: 2, rows: Math.ceil(slice.length / 2) }
      : gridShape(list.length);
    pg.style.setProperty('--cols', shape.cols);
    pg.style.setProperty('--rows', shape.rows);
    for (var k = 0; k < slice.length; k++) {
      var tt = makeTile(slice[k], p, false);
      tiles.push(tt);
      pg.appendChild(tt.el);
    }
    PAGES.appendChild(pg);

    var d = document.createElement('i');
    if (p === 0) d.className = 'on';
    DOTS.appendChild(d);
  }
  updateVisibility();
}

PAGES.addEventListener('scroll', function () {
  if (!isPortrait()) return;
  var w = PAGES.clientWidth || 1;
  var p = Math.round(PAGES.scrollLeft / w);
  if (p === curPage) return;
  curPage = p;
  for (var i = 0; i < DOTS.children.length; i++) {
    DOTS.children[i].className = (i === curPage ? 'on' : '');
  }
  updateVisibility();
}, { passive: true });

async function sync() {
  try {
    var r = await fetch('/api/wall/layout', { cache: 'no-store' });
    if (r.status === 401 || r.status === 403) { location.href = '/'; return; }
    if (!r.ok) return;
    var d = await r.json();

    thumbFps = Math.max(1, d.thumb_fps || 3);
    META.textContent = d.mode + ' · ' + (d.tiles || []).length + ' ch · ' + thumbFps + ' fps';

    var next = JSON.stringify([
      d.mode,
      d.fullscreen,
      (d.tiles || []).map(function (t) { return t.dvr + '/' + t.ch + '/' + t.label + (t.ptz ? '/ptz' : ''); })
    ]);
    if (next !== sig) { sig = next; build(d); }
  } catch (e) { /* transient -- the next tick retries */ }
}

var idleTimer = null;
function wake() {
  document.body.classList.add('chrome');
  document.body.classList.remove('idle');
  clearTimeout(idleTimer);
  idleTimer = setTimeout(function () {
    document.body.classList.remove('chrome');
    document.body.classList.add('idle');
  }, 2800);
}
['mousemove', 'touchstart', 'keydown'].forEach(function (ev) {
  document.addEventListener(ev, wake, { passive: true });
});

document.getElementById('btnLabels').addEventListener('click', function (e) {
  e.stopPropagation();
  var on = document.body.classList.toggle('show-labels');
  try { localStorage.setItem('wallLabels', on ? '1' : '0'); } catch (_) {}
  wake();
});

function toggleFs() {
  if (document.fullscreenElement) {
    document.exitFullscreen();
  } else if (document.documentElement.requestFullscreen) {
    document.documentElement.requestFullscreen();
  }
}
document.getElementById('btnFs').addEventListener('click', function (e) {
  e.stopPropagation(); toggleFs(); wake();
});
document.addEventListener('dblclick', function (e) {
  /* Double-tapping PTZ buttons must not flip browser fullscreen. */
  if (e.target.closest && e.target.closest('.ptz-ui')) return;
  toggleFs();
});
document.addEventListener('keydown', function (e) {
  if (e.key === 'f' || e.key === 'F') toggleFs();
  if (e.key === 'l' || e.key === 'L') document.body.classList.toggle('show-labels');
  if (e.key === 'Escape' && !document.fullscreenElement) location.href = '/';
});

/* Stop every request the moment the tab is backgrounded: a phone in a pocket
   must not keep the board encoding frames nobody is looking at. */
document.addEventListener('visibilitychange', function () {
  updateVisibility();
  if (!document.hidden) sync();
});

window.addEventListener('resize', function () {
  var nowPortrait = isPortrait();
  if (document.body.dataset.portrait !== (nowPortrait ? '1' : '0')) {
    document.body.dataset.portrait = nowPortrait ? '1' : '0';
    sig = '';
    sync();
  }
});

try { if (localStorage.getItem('wallLabels') !== '0') document.body.classList.add('show-labels'); } catch (_) {}
document.body.dataset.portrait = isPortrait() ? '1' : '0';
wake();
sync();
setInterval(sync, 5000);
</script>
</body>
</html>
"""

from ptz_ui import PTZ_CSS, PTZ_JS  # noqa: E402  (spliced in; see ptz_ui.py)

WALL_HTML = WALL_HTML.replace("/*PTZ_CSS*/", PTZ_CSS).replace("/*PTZ_JS*/", PTZ_JS)
