// Scrub viewer: canvas timeline + sprite-sheet scrubbing over the same /v1
// endpoints the iOS client uses (docs/scrub-cache-and-proxy-spec.md).
//
// Timeline vocabulary mirrors the app's reel: motion wash in the theme's
// muted tone, lane-colored event marks, --live for the now-line.
//
// Phones get the app's reel interaction instead of the desktop bar: a
// vertical drum under a fixed center aperture (drag down = earlier), with
// momentum on release, snap to the nearest cached frame, tappable event
// marks, and fine scrubbing by dragging the image itself.
(function () {
  var camSel = document.getElementById("sv-camera");
  var winSel = document.getElementById("sv-window");
  var canvas = document.getElementById("sv-timeline");
  var frame = document.getElementById("sv-frame");
  var clock = document.getElementById("sv-clock");
  var frigateLink = document.getElementById("sv-frigate-link");
  var frigateLinkPhone = document.getElementById("sv-frigate-link-phone");
  if (!camSel || !canvas) return;

  var state = {
    camera: camSel.value,
    windowS: parseFloat(winSel.value),
    start: 0,
    end: 0,
    reel: null,
    sheets: [],       // ascending by start, finest-first within a timestamp
    cursor: null,     // current scrub time (s epoch)
    images: {},       // url -> HTMLImageElement (browser cache does real work)
  };

  var LANES = { person: "--lane-person", car: "--lane-vehicle", vehicle: "--lane-vehicle",
                dog: "--lane-animal", cat: "--lane-animal", bird: "--lane-animal",
                package: "--lane-package" };

  // Reel (phone) mode: vertical drum, fixed center cursor.
  var vertical = window.matchMedia("(max-width: 720px)").matches;

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  async function load() {
    state.camera = camSel.value;
    state.windowS = parseFloat(winSel.value);
    state.end = Date.now() / 1000;
    state.start = state.end - state.windowS;
    var scale = Math.max(10, Math.round(state.windowS / 720));
    var [reelResp, sheetsResp] = await Promise.all([
      fetch("/v1/reel/" + encodeURIComponent(state.camera) +
            "?start=" + state.start + "&end=" + state.end + "&motion_scale=" + scale),
      fetch("/v1/scrub/" + encodeURIComponent(state.camera) +
            "/sheets?start=" + state.start + "&end=" + state.end),
    ]);
    if (!reelResp.ok || !sheetsResp.ok) return;
    state.reel = await reelResp.json();
    var sheets = (await sheetsResp.json()).sheets;
    // Finest tier first so lookup picks the highest-cadence sheet covering t.
    sheets.sort(function (a, b) { return a.interval - b.interval || a.start - b.start; });
    state.sheets = sheets;
    if (state.cursor === null || state.cursor < state.start || state.cursor > state.end) {
      state.cursor = state.end - 60;
    }
    draw();
    showFrame(state.cursor);
  }

  function draw() {
    if (!state.reel) return;
    if (vertical) { drawVertical(); return; }
    var dpr = window.devicePixelRatio || 1;
    var w = canvas.clientWidth || canvas.parentElement.clientWidth;
    var h = 72;
    canvas.width = w * dpr;
    canvas.height = h * dpr;
    var ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);
    var span = state.end - state.start;
    function x(t) { return ((t - state.start) / span) * w; }

    ctx.fillStyle = cssVar("--deep");
    ctx.fillRect(0, 0, w, h);

    // Recorded spans.
    ctx.fillStyle = cssVar("--surface-2");
    (state.reel.recorded || []).forEach(function (r) {
      ctx.fillRect(x(r[0]), 0, Math.max(1, x(r[1]) - x(r[0])), h);
    });

    // Motion wash (tide): normalized bars from the bottom.
    var m = state.reel.motion;
    if (m && m.values && m.values.length) {
      var max = Math.max.apply(null, m.values.map(function (v) { return v || 0; })) || 1;
      ctx.fillStyle = cssVar("--muted-3");
      ctx.globalAlpha = 0.55;
      var bw = Math.max(1, w / m.values.length);
      m.values.forEach(function (v, i) {
        if (!v) return;
        var bh = Math.max(1, (v / max) * (h - 22));
        ctx.fillRect(x(m.start + i * m.interval), h - bh, bw, bh);
      });
      ctx.globalAlpha = 1;
    }

    // Event marks: lane-colored ticks along the top.
    (state.reel.events || []).forEach(function (ev) {
      var lane = LANES[ev.label];
      ctx.fillStyle = lane ? cssVar(lane) : cssVar("--muted");
      var x0 = x(ev.start);
      var x1 = x(ev.end === null ? state.end : ev.end);
      ctx.fillRect(x0, 4, Math.max(2, x1 - x0), 5);
    });

    // Frame coverage: thin strip above the motion floor.
    ctx.fillStyle = cssVar("--accent-2");
    ctx.globalAlpha = 0.5;
    (state.reel.frames || []).forEach(function (f) {
      var x0 = x(f.start);
      var x1 = x(f.start + f.interval * f.count);
      ctx.fillRect(x0, h - 3, Math.max(1, x1 - x0), 3);
    });
    ctx.globalAlpha = 1;

    // Cursor + live edge.
    ctx.fillStyle = cssVar("--live");
    ctx.fillRect(x(state.end) - 1.5, 0, 1.5, h);
    if (state.cursor !== null) {
      ctx.fillStyle = cssVar("--accent");
      ctx.fillRect(x(state.cursor) - 1, 0, 2, h);
    }
  }

  // --- Vertical reel (phones) ----------------------------------------------
  // Time flows down: past above the center aperture, future below (matching
  // the app's "Past ↑ / Future ↓"). The drum pans under a fixed cursor.

  function visibleSpan() {
    // Enough context to orient, fine enough to aim: at most 30 min on screen.
    return Math.min(state.windowS / 6, 1800);
  }

  function drawVertical() {
    var dpr = window.devicePixelRatio || 1;
    var w = canvas.clientWidth || canvas.parentElement.clientWidth;
    var h = canvas.clientHeight || 300;
    canvas.width = w * dpr;
    canvas.height = h * dpr;
    var ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);
    var pxPerS = h / visibleSpan();
    var cursor = state.cursor === null ? state.end : state.cursor;
    function y(t) { return h / 2 + (t - cursor) * pxPerS; }

    ctx.fillStyle = cssVar("--deep");
    ctx.fillRect(0, 0, w, h);

    // Recorded spans.
    ctx.fillStyle = cssVar("--surface-2");
    (state.reel.recorded || []).forEach(function (r) {
      var y0 = y(r[0]);
      var y1 = y(r[1]);
      if (y1 < 0 || y0 > h) return;
      ctx.fillRect(0, y0, w, Math.max(1, y1 - y0));
    });

    // Time gridlines + labels on the left.
    var tick = state.windowS <= 3600 ? 300 : state.windowS <= 21600 ? 900 : 3600;
    ctx.font = "10px ui-monospace, monospace";
    ctx.textBaseline = "middle";
    var t0 = Math.floor((cursor - visibleSpan()) / tick) * tick;
    for (var gt = t0; gt < cursor + visibleSpan(); gt += tick) {
      var gy = y(gt);
      if (gy < 0 || gy > h) continue;
      ctx.fillStyle = cssVar("--stroke-soft");
      ctx.fillRect(0, gy, w, 1);
      ctx.fillStyle = cssVar("--muted-2");
      var d = new Date(gt * 1000);
      ctx.fillText(
        ("0" + d.getHours()).slice(-2) + ":" + ("0" + d.getMinutes()).slice(-2),
        4, gy - 7);
    }

    // Motion wash: horizontal bars growing from the left.
    var m = state.reel.motion;
    if (m && m.values && m.values.length) {
      var max = Math.max.apply(null, m.values.map(function (v) { return v || 0; })) || 1;
      ctx.fillStyle = cssVar("--muted-3");
      ctx.globalAlpha = 0.5;
      var bh = Math.max(1, m.interval * pxPerS);
      m.values.forEach(function (v, i) {
        if (!v) return;
        var by = y(m.start + i * m.interval);
        if (by < -bh || by > h) return;
        ctx.fillRect(0, by, (v / max) * (w * 0.45), bh);
      });
      ctx.globalAlpha = 1;
    }

    // Event marks: lane-colored bars along the right edge (tap to jump).
    (state.reel.events || []).forEach(function (ev) {
      var lane = LANES[ev.label];
      ctx.fillStyle = lane ? cssVar(lane) : cssVar("--muted");
      var y0 = y(ev.start);
      var y1 = y(ev.end === null ? state.end : ev.end);
      if (y1 < 0 || y0 > h) return;
      ctx.fillRect(w - 16, y0, 8, Math.max(4, y1 - y0));
    });

    // Live edge.
    var ly = y(state.end);
    if (ly >= 0 && ly <= h) {
      ctx.fillStyle = cssVar("--live");
      ctx.fillRect(0, ly - 1, w, 2);
    }

    // Center aperture: cursor line + readout.
    ctx.fillStyle = cssVar("--accent");
    ctx.fillRect(0, h / 2 - 1, w, 2);
    var label = new Date(cursor * 1000).toLocaleTimeString();
    ctx.font = "12px ui-monospace, monospace";
    var tw = ctx.measureText(label).width + 14;
    ctx.fillStyle = cssVar("--surface");
    ctx.strokeStyle = cssVar("--accent");
    ctx.beginPath();
    ctx.roundRect((w - tw) / 2, h / 2 - 22, tw, 18, 6);
    ctx.fill();
    ctx.stroke();
    ctx.fillStyle = cssVar("--accent");
    ctx.fillText(label, (w - tw) / 2 + 7, h / 2 - 13);
  }

  function clampCursor(t) {
    return Math.min(state.end, Math.max(state.start, t));
  }

  function snapCursor() {
    var s = sheetFor(state.cursor);
    if (!s) return;
    var idx = Math.round((state.cursor - s.start) / s.interval);
    showFrame(clampCursor(s.start + idx * s.interval));
  }

  function bindVerticalGestures() {
    var lastY = null;
    var lastT = 0;
    var velocity = 0; // px/s, +down
    var coasting = null;

    function stopCoast() {
      if (coasting) { cancelAnimationFrame(coasting); coasting = null; }
    }

    canvas.style.touchAction = "none";
    canvas.addEventListener("pointerdown", function (e) {
      stopCoast();
      lastY = e.clientY;
      lastT = performance.now();
      velocity = 0;
      canvas.setPointerCapture(e.pointerId);
      // A tap near an event mark jumps to that event.
      var rect = canvas.getBoundingClientRect();
      if (e.clientX - rect.left > rect.width - 32) {
        var h = rect.height;
        var pxPerS = h / visibleSpan();
        var tAt = state.cursor + (e.clientY - rect.top - h / 2) / pxPerS;
        var best = null;
        (state.reel.events || []).forEach(function (ev) {
          var d = Math.abs(ev.start - tAt);
          if (d < 30 / pxPerS && (best === null || d < Math.abs(best.start - tAt))) best = ev;
        });
        if (best) { showFrame(clampCursor(best.start)); lastY = null; return; }
      }
    });
    canvas.addEventListener("pointermove", function (e) {
      if (lastY === null) return;
      var dy = e.clientY - lastY;
      var now = performance.now();
      if (now > lastT) velocity = 0.6 * velocity + 0.4 * (dy / ((now - lastT) / 1000));
      lastY = e.clientY;
      lastT = now;
      var pxPerS = (canvas.clientHeight || 300) / visibleSpan();
      // Drag down = earlier (turn the drum toward you).
      showFrame(clampCursor(state.cursor - dy / pxPerS));
    });
    function release() {
      if (lastY === null) return;
      lastY = null;
      var pxPerS = (canvas.clientHeight || 300) / visibleSpan();
      function coast() {
        velocity *= 0.94;
        if (Math.abs(velocity) < 30) { snapCursor(); coasting = null; return; }
        showFrame(clampCursor(state.cursor - (velocity / 60) / pxPerS));
        coasting = requestAnimationFrame(coast);
      }
      if (Math.abs(velocity) > 220) coasting = requestAnimationFrame(coast);
      else snapCursor();
    }
    canvas.addEventListener("pointerup", release);
    canvas.addEventListener("pointercancel", release);

    // Fine scrub: drag the image horizontally, one cached frame per 14px.
    var fineX = null;
    frame.style.touchAction = "pan-y";
    frame.addEventListener("pointerdown", function (e) { fineX = e.clientX; });
    frame.addEventListener("pointermove", function (e) {
      if (fineX === null) return;
      var dx = e.clientX - fineX;
      var s = sheetFor(state.cursor);
      var step = s ? s.interval : 5;
      if (Math.abs(dx) >= 14) {
        fineX = e.clientX;
        showFrame(clampCursor(state.cursor + (dx > 0 ? step : -step)));
      }
    });
    frame.addEventListener("pointerup", function () { fineX = null; });
    frame.addEventListener("pointercancel", function () { fineX = null; });
  }

  function sheetFor(t) {
    for (var i = 0; i < state.sheets.length; i++) {
      var s = state.sheets[i];
      if (t >= s.start && t < s.start + s.interval * s.count) return s;
    }
    return null;
  }

  function showFrame(t) {
    state.cursor = t;
    var s = sheetFor(t);
    var d = new Date(t * 1000);
    clock.textContent = d.toLocaleString();
    var reviewUrl = "/review?id=" + encodeURIComponent(state.camera) +
      "&recording.timestamp=" + Math.floor(t);
    if (frigateLink) frigateLink.href = reviewUrl;
    if (frigateLinkPhone) frigateLinkPhone.href = reviewUrl;
    if (!s) {
      frame.style.backgroundImage = "none";
      frame.textContent = "no frame cached for this moment";
      draw();
      return;
    }
    frame.textContent = "";
    var idx = Math.floor((t - s.start) / s.interval);
    var col = idx % s.cols;
    var row = Math.floor(idx / s.cols);
    frame.style.width = "100%";
    frame.style.aspectRatio = s.cell_w + " / " + s.cell_h;
    frame.style.backgroundImage = "url('" + s.url + "')";
    frame.style.backgroundSize = (s.cols * 100) + "% " + (s.rows * 100) + "%";
    frame.style.backgroundPosition =
      (s.cols > 1 ? (col * 100) / (s.cols - 1) : 0) + "% " +
      (s.rows > 1 ? (row * 100) / (s.rows - 1) : 0) + "%";
    preload(t, s);
    draw();
  }

  function preload(t, current) {
    // Warm the neighbouring sheets so stepping across a boundary is instant.
    [t - current.interval * current.cols * current.rows,
     t + current.interval * current.cols * current.rows].forEach(function (tt) {
      var s = sheetFor(tt);
      if (s && !state.images[s.url]) {
        var img = new Image();
        img.src = s.url;
        state.images[s.url] = img;
      }
    });
  }

  function timeAt(evt) {
    var rect = canvas.getBoundingClientRect();
    var frac = (evt.clientX - rect.left) / rect.width;
    return state.start + Math.min(1, Math.max(0, frac)) * (state.end - state.start);
  }

  if (vertical) {
    bindVerticalGestures();
  } else {
    var dragging = false;
    canvas.addEventListener("pointerdown", function (e) { dragging = true; showFrame(timeAt(e)); });
    window.addEventListener("pointerup", function () { dragging = false; });
    canvas.addEventListener("pointermove", function (e) {
      if (dragging || e.buttons === 0) showFrame(timeAt(e));
    });
  }
  window.addEventListener("keydown", function (e) {
    if (state.cursor === null) return;
    var s = sheetFor(state.cursor);
    var step = s ? s.interval : 5;
    if (e.key === "ArrowLeft") { showFrame(state.cursor - step); e.preventDefault(); }
    if (e.key === "ArrowRight") { showFrame(state.cursor + step); e.preventDefault(); }
  });
  window.addEventListener("resize", draw);
  camSel.addEventListener("change", load);
  winSel.addEventListener("change", load);
  load();
})();
