// Scrub viewer: canvas timeline + sprite-sheet scrubbing over the same /v1
// endpoints the iOS client uses (docs/scrub-cache-and-proxy-spec.md).
//
// Timeline vocabulary mirrors the app's reel: motion wash in the theme's
// muted tone, lane-colored event marks, --live for the now-line.
(function () {
  var camSel = document.getElementById("sv-camera");
  var winSel = document.getElementById("sv-window");
  var canvas = document.getElementById("sv-timeline");
  var frame = document.getElementById("sv-frame");
  var clock = document.getElementById("sv-clock");
  var frigateLink = document.getElementById("sv-frigate-link");
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
    if (frigateLink) {
      frigateLink.href = "/review?id=" + encodeURIComponent(state.camera) +
        "&recording.timestamp=" + Math.floor(t);
    }
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

  var dragging = false;
  canvas.addEventListener("pointerdown", function (e) { dragging = true; showFrame(timeAt(e)); });
  window.addEventListener("pointerup", function () { dragging = false; });
  canvas.addEventListener("pointermove", function (e) {
    if (dragging || e.buttons === 0) showFrame(timeAt(e));
  });
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
