// Camera calibration: per-camera heading vectors + layout map.
(function () {
  var banner = document.getElementById("cameras-banner");
  var mapEl = document.getElementById("layout-map");
  var saveBtn = document.getElementById("save-btn");
  var saveState = document.getElementById("save-state");
  var suggestBtn = document.getElementById("suggest-btn");
  var suggestDiff = document.getElementById("suggest-diff");
  var radiusInput = document.getElementById("suggest-radius");
  var radiusLabel = document.getElementById("suggest-radius-label");

  var doc = null;
  var cameras = [];
  // camera -> {hfov, mount_ft, tilt_deg, vfov?, faces?, lens?}. Alias of
  // doc.camera_optics (settings-backed since onboarding): edits here are
  // edits to the document the Save button PUTs.
  var placements = {};
  var lensPresets = window.LENS_PRESETS || [];
  var placeMode = null;      // camera waiting for a map click to be placed
  var calibrateStart = null; // first click of the scale reference line
  var calibrating = false;

  // ---- View window (zoom/pan) ----------------------------------------
  // The visible window in unit map coords; h == w because the viewBox is
  // a unit square stretched by the container's CSS aspect-ratio (map
  // aspect lives in the scale math, never in the viewBox). Lives outside
  // renderMap so redraws never reset the view.
  var view = { x: 0, y: 0, w: 1 };
  // Screen-constant size: a stroke/font/radius expressed as a viewBox
  // fraction must shrink as the window narrows or it balloons on zoom.
  function sz(v) { return v * view.w; }
  // THE client->unit mapping. Every map gesture goes through here so
  // zoom/pan can never desync a drag.
  function clientToUnit(ev) {
    var r = mapEl.getBoundingClientRect();
    return {
      x: view.x + ((ev.clientX - r.left) / r.width) * view.w,
      y: view.y + ((ev.clientY - r.top) / r.height) * view.w,
    };
  }
  function unitToPct(u, axis) {
    return ((u - (axis === "y" ? view.y : view.x)) / view.w) * 100;
  }
  function clampView() {
    view.w = Math.min(1, Math.max(1 / 8, view.w));
    view.x = Math.min(1 - view.w, Math.max(0, view.x));
    view.y = Math.min(1 - view.w, Math.max(0, view.y));
  }
  function resetView() {
    view = { x: 0, y: 0, w: 1 };
    renderMap();
  }

  // ---- Layers ---------------------------------------------------------
  // The active layer owns the map's clicks and drags; visibility toggles
  // (View group) apply regardless. Persisted so a calibration session
  // survives reloads.
  var LAYERS = ["cameras", "areas", "calibrate", "view"];
  var activeLayer = localStorage.getItem("cam_layer") || "cameras";
  if (LAYERS.indexOf(activeLayer) === -1) activeLayer = "cameras";

  function setLayer(name) {
    activeLayer = name;
    localStorage.setItem("cam_layer", name);
    document.body.dataset.layer = name;
    var seg = document.getElementById("layer-seg");
    Array.prototype.forEach.call(seg.querySelectorAll(".vbtn"), function (b) {
      b.classList.toggle("active", b.dataset.layer === name);
    });
    // Leaving a layer cancels the modes it owns — a stale placeMode or
    // half-drawn calibration line must not ambush the next layer's clicks.
    if (name !== "cameras") placeMode = null;
    if (name !== "calibrate") {
      calibrating = false;
      calibrateStart = null;
      if (typeof closeLandmarkMode === "function" && landmarkMode) {
        closeLandmarkMode();
      }
    }
    if (typeof renderMap === "function" && doc) renderMap();
  }

  Array.prototype.forEach.call(
    document.getElementById("layer-seg").querySelectorAll(".vbtn"),
    function (b) {
      b.addEventListener("click", function () { setLayer(b.dataset.layer); });
    }
  );
  document.body.dataset.layer = activeLayer;
  (function syncSegInitial() {
    var seg = document.getElementById("layer-seg");
    Array.prototype.forEach.call(seg.querySelectorAll(".vbtn"), function (b) {
      b.classList.toggle("active", b.dataset.layer === activeLayer);
    });
  })();

  var CARDINAL_DEG = {
    N: 0, NNE: 22.5, NE: 45, ENE: 67.5, E: 90, ESE: 112.5, SE: 135, SSE: 157.5,
    S: 180, SSW: 202.5, SW: 225, WSW: 247.5, W: 270, WNW: 292.5, NW: 315, NNW: 337.5,
  };

  function defaultFov(camera) {
    var p = placements[camera];
    return p && p.hfov ? p.hfov : 90;
  }
  function defaultAzimuth(camera) {
    var p = placements[camera];
    return p && p.faces !== undefined && CARDINAL_DEG[p.faces] !== undefined
      ? CARDINAL_DEG[p.faces] : 0;
  }

  function showBanner(text, isError) {
    banner.textContent = text;
    banner.style.display = "block";
    banner.style.color = isError ? "var(--warn, #e8a735)" : "var(--muted)";
  }

  function markDirty() { saveState.textContent = "unsaved changes"; }

  async function fetchJson(url, opts) {
    var resp = await fetch(url, opts);
    var raw = await resp.text();
    var data;
    try { data = JSON.parse(raw); }
    catch (e) { throw new Error("HTTP " + resp.status + " — " + raw.slice(0, 200)); }
    if (resp.status === 401) {
      throw new Error("Not authorized — open Frigate and log in first, then reload this page.");
    }
    if (!resp.ok) {
      var detail = data && data.detail;
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail || data));
    }
    return data;
  }

  var SVG_NS = "http://www.w3.org/2000/svg";

  // ---- Layout map (top-down, north = up) -----------------------------

  var detailPanel = document.getElementById("camera-detail");
  var clearSecureBtn = document.getElementById("clear-secure");
  var detailName = document.getElementById("detail-name");
  var detailAzimuth = document.getElementById("detail-azimuth");
  var detailFov = document.getElementById("detail-fov");
  var selectedCamera = null;
  var DEFAULT_FOV = 90;

  function reach() { return parseFloat(radiusInput.value); }

  function layoutEntry(camera, i) {
    var layout = doc.camera_layout || {};
    return layout[camera] || {
      x: 0.08 + 0.84 * (i / Math.max(1, cameras.length - 1)),
      y: 0.92,
    };
  }

  function ensureEntry(camera, i) {
    if (!doc.camera_layout) doc.camera_layout = {};
    if (!doc.camera_layout[camera]) {
      var p = layoutEntry(camera, i);
      doc.camera_layout[camera] = { x: +p.x.toFixed(4), y: +p.y.toFixed(4) };
    }
    return doc.camera_layout[camera];
  }

  // Azimuth: compass degrees, 0 = north = up on the map, clockwise.
  // Map coords have y DOWN, so direction = (sin az, -cos az).
  function azDir(azDeg) {
    var r = (azDeg * Math.PI) / 180;
    return { x: Math.sin(r), y: -Math.cos(r) };
  }

  var CARDINALS = [
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
  ];
  function cardinalOf(azDeg) {
    return CARDINALS[Math.round((azDeg % 360) / 22.5) % 16];
  }

  function wedgePath(pos, azDeg, fovDeg, r) {
    var a0 = ((azDeg - fovDeg / 2) * Math.PI) / 180;
    var a1 = ((azDeg + fovDeg / 2) * Math.PI) / 180;
    var p0 = { x: pos.x + Math.sin(a0) * r, y: pos.y - Math.cos(a0) * r };
    var p1 = { x: pos.x + Math.sin(a1) * r, y: pos.y - Math.cos(a1) * r };
    var large = fovDeg > 180 ? 1 : 0;
    return (
      "M " + pos.x + " " + pos.y +
      " L " + p0.x + " " + p0.y +
      " A " + r + " " + r + " 0 " + large + " 1 " + p1.x + " " + p1.y +
      " Z"
    );
  }

  function pointerAzimuth(ev, entry) {
    var p = clientToUnit(ev);
    var dx = p.x - entry.x, dy = p.y - entry.y;
    if (Math.hypot(dx, dy) < sz(0.01)) return null;
    return ((Math.atan2(dx, -dy) * 180) / Math.PI + 360) % 360;
  }

  function svgLine(svg, x1, y1, x2, y2, attrs) {
    var line = document.createElementNS(SVG_NS, "line");
    line.setAttribute("x1", x1); line.setAttribute("y1", y1);
    line.setAttribute("x2", x2); line.setAttribute("y2", y2);
    Object.keys(attrs).forEach(function (k) { line.setAttribute(k, attrs[k]); });
    svg.appendChild(line);
    return line;
  }

  function renderMap() {
    mapEl.textContent = "";

    // Wedge layer + compass rose. The direction arrow and the two wedge
    // edges are the manipulation surfaces — same feel as drawing the
    // snapshot arrows: grab the arrow to aim, grab an edge to widen.
    var svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", view.x + " " + view.y + " " + view.w + " " + view.w);
    svg.setAttribute("preserveAspectRatio", "none");
    svg.style.cssText = "position:absolute;inset:0;width:100%;height:100%;pointer-events:none";
    // Floorplan as the first SVG child (not a CSS background) so it pans
    // and zooms with the geometry.
    var fpImg = floorplanHref();
    if (fpImg) {
      var img = document.createElementNS(SVG_NS, "image");
      img.setAttribute("x", 0); img.setAttribute("y", 0);
      img.setAttribute("width", 1); img.setAttribute("height", 1);
      img.setAttribute("preserveAspectRatio", "none");
      img.setAttribute("href", fpImg);
      svg.appendChild(img);
    }
    if (heatmapURL) {
      var heat = document.createElementNS(SVG_NS, "image");
      heat.setAttribute("x", 0); heat.setAttribute("y", 0);
      heat.setAttribute("width", 1); heat.setAttribute("height", 1);
      heat.setAttribute("preserveAspectRatio", "none");
      heat.setAttribute("href", heatmapURL);
      svg.appendChild(heat);
    }
    var defs = document.createElementNS(SVG_NS, "defs");
    var marker = document.createElementNS(SVG_NS, "marker");
    marker.setAttribute("id", "map-arrowhead");
    marker.setAttribute("viewBox", "0 0 10 10");
    marker.setAttribute("refX", "8"); marker.setAttribute("refY", "5");
    marker.setAttribute("markerWidth", "4.5"); marker.setAttribute("markerHeight", "4.5");
    marker.setAttribute("orient", "auto-start-reverse");
    var tip = document.createElementNS(SVG_NS, "path");
    tip.setAttribute("d", "M 0 0 L 10 5 L 0 10 z");
    tip.setAttribute("fill", "var(--accent, #ffb454)");
    marker.appendChild(tip);
    defs.appendChild(marker);
    svg.appendChild(defs);
    drawZoneOverlays(svg); // under the camera groups: zones are ground truth
    drawFootprints(svg);   // true projected coverage (View: Footprints)
    drawRingsGrid(svg);

    function dragOn(el, camera, i, apply) {
      if (!el.style.pointerEvents) el.style.pointerEvents = "stroke";
      el.style.touchAction = "none";
      el.addEventListener("pointerdown", function (ev) {
        if (activeLayer !== "cameras") return; // aim drags are Cameras-layer only
        ev.stopPropagation();
        ev.preventDefault();
        // Select WITHOUT re-rendering here, and track the gesture on
        // window: renderMap() rebuilds the SVG mid-drag, so listeners on
        // the (detached) element itself would go silent after the first
        // frame — the "moves a tiny bit then stops" bug.
        selectedCamera = camera;
        function move(mv) {
          if (pinchActive) return;
          var e = ensureEntry(camera, i);
          var az = pointerAzimuth(mv, e);
          if (az === null) return;
          apply(e, az);
          renderMap();
        }
        function up() {
          window.removeEventListener("pointermove", move);
          window.removeEventListener("pointerup", up);
          markDirty();
          renderMap();
        }
        window.addEventListener("pointermove", move);
        window.addEventListener("pointerup", up);
      });
    }

    cameras.forEach(function (camera, i) {
      // Unplaced cameras still get a ghost arrow at their default spot —
      // grabbing it both places and aims them in one motion.
      var entry = (doc.camera_layout || {})[camera] || layoutEntry(camera, i);
      var hasAim = entry.azimuth !== undefined;
      var az = hasAim ? entry.azimuth : defaultAzimuth(camera);
      var fov = entry.fov || defaultFov(camera);
      var r = reach();
      var selected = camera === selectedCamera;

      // Everything this camera draws lives in one group so selecting
      // another camera can dim it as a unit.
      var camGroup = document.createElementNS(SVG_NS, "g");
      if (selectedCamera && !selected) camGroup.setAttribute("opacity", "0.22");
      // Outside the Cameras layer the geometry is display-only: hits must
      // fall through to the map background (pan, secure drag, measure).
      if (activeLayer !== "cameras") camGroup.style.pointerEvents = "none";
      svg.appendChild(camGroup);

      if (hasAim && !(footprintsToggle && footprintsToggle.checked)) {
        // Soft view gradient: the pie's direction carried to the map edge,
        // fading out — reads as "what this camera can see beyond its
        // interaction pie" without adding more hard lines.
        var far = Math.max(
          Math.hypot(entry.x, entry.y),
          Math.hypot(1 - entry.x, entry.y),
          Math.hypot(entry.x, 1 - entry.y),
          Math.hypot(1 - entry.x, 1 - entry.y)
        );
        var grad = document.createElementNS(SVG_NS, "radialGradient");
        grad.setAttribute("id", "fov-fade-" + i);
        grad.setAttribute("gradientUnits", "userSpaceOnUse");
        grad.setAttribute("cx", entry.x); grad.setAttribute("cy", entry.y);
        grad.setAttribute("r", far);
        [[0, 0.22], [0.45, 0.10], [1, 0]].forEach(function (stop) {
          var s = document.createElementNS(SVG_NS, "stop");
          s.setAttribute("offset", stop[0]);
          s.setAttribute("stop-color", "var(--accent, #ffb454)");
          s.setAttribute("stop-opacity", stop[1]);
          grad.appendChild(s);
        });
        defs.appendChild(grad);
        var fade = document.createElementNS(SVG_NS, "path");
        fade.setAttribute("d", wedgePath(entry, az, fov, far));
        fade.setAttribute("fill", "url(#fov-fade-" + i + ")");
        fade.setAttribute("stroke", "none");
        fade.style.pointerEvents = "none";
        camGroup.appendChild(fade);
      }

      if (hasAim) {
        var coverage = coverageToggle && coverageToggle.checked;
        var path = document.createElementNS(SVG_NS, "path");
        path.setAttribute("d", wedgePath(entry, az, fov, r));
        path.setAttribute("fill", "var(--accent, #ffb454)");
        path.setAttribute(
          "fill-opacity", coverage ? "0.38" : (selected ? "0.28" : "0.14")
        );
        path.setAttribute("stroke", "none");
        // The whole pie is a rotation surface: drag anywhere inside it to
        // swing the aim — the biggest possible target.
        path.style.pointerEvents = "fill";
        path.style.cursor = "grab";
        dragOn(path, camera, i, function (e, pointerAz) {
          e.azimuth = +pointerAz.toFixed(1);
        });
        camGroup.appendChild(path);

        // The two wedge edges: visible thin lines + invisible fat grab
        // lines. Dragging an edge sets the width symmetrically.
        [az - fov / 2, az + fov / 2].forEach(function (edgeAz) {
          var d = azDir(edgeAz);
          var ex = entry.x + d.x * r, ey = entry.y + d.y * r;
          svgLine(camGroup, entry.x, entry.y, ex, ey, {
            stroke: "var(--accent, #ffb454)", "stroke-opacity": "0.55",
            "stroke-width": sz(0.004), "stroke-dasharray": sz(0.012) + " " + sz(0.008),
          });
          var grab = svgLine(camGroup, entry.x, entry.y, ex, ey, {
            stroke: "transparent", "stroke-width": sz(0.035),
          });
          grab.style.cursor = "col-resize";
          dragOn(grab, camera, i, function (e, pointerAz) {
            var half = Math.abs(((pointerAz - e.azimuth + 540) % 360) - 180);
            e.fov = +Math.min(360, Math.max(10, half * 2)).toFixed(1);
          });
        });
      }

      var dir = azDir(az);

      // The camera itself: a filled dot exactly where it's mounted.
      // Selected = accent, others = muted. The label pill hangs below it.
      var pt = document.createElementNS(SVG_NS, "circle");
      pt.setAttribute("cx", entry.x); pt.setAttribute("cy", entry.y);
      pt.setAttribute("r", sz(selected ? 0.014 : 0.010));
      pt.setAttribute("fill", selected ? "var(--accent, #ffb454)" : "var(--muted, #8f9fb8)");
      pt.setAttribute("stroke", "var(--surface, #111)");
      pt.setAttribute("stroke-width", sz(0.004));
      pt.style.pointerEvents = "none";
      camGroup.appendChild(pt);

      // The dot is the natural thing to grab to MOVE the camera — an
      // invisible fat hit circle over it does exactly that (the pie
      // underneath would otherwise swallow the drag and rotate instead).
      var moveGrab = document.createElementNS(SVG_NS, "circle");
      moveGrab.setAttribute("cx", entry.x); moveGrab.setAttribute("cy", entry.y);
      moveGrab.setAttribute("r", sz(0.03));
      moveGrab.setAttribute("fill", "transparent");
      moveGrab.style.pointerEvents = "fill";
      moveGrab.style.cursor = "move";
      moveGrab.style.touchAction = "none";
      moveGrab.setAttribute("aria-label", "move " + camera);
      moveGrab.addEventListener("pointerdown", function (ev) {
        if (activeLayer !== "cameras") return;
        ev.stopPropagation();
        ev.preventDefault();
        selectedCamera = camera;
        var movedPt = false;
        function mm(mv) {
          if (pinchActive) return;
          movedPt = true;
          var p = clientToUnit(mv);
          var e = ensureEntry(camera, i);
          e.x = +Math.min(1, Math.max(0, p.x)).toFixed(4);
          e.y = +Math.min(1, Math.max(0, p.y)).toFixed(4);
          renderMap();
        }
        function uu() {
          window.removeEventListener("pointermove", mm);
          window.removeEventListener("pointerup", uu);
          if (movedPt) markDirty();
          renderMap();
        }
        window.addEventListener("pointermove", mm);
        window.addEventListener("pointerup", uu);
      });
      camGroup.appendChild(moveGrab);

      // Rotation wheel: shown only for the selected camera, so "turn" has
      // its own visible control distinct from "move" (dragging the pill).
      // Drag anywhere on the ring — or its knob — to swing the azimuth.
      if (selected) {
        var rw = sz(0.055);
        var ring = document.createElementNS(SVG_NS, "circle");
        ring.setAttribute("cx", entry.x); ring.setAttribute("cy", entry.y);
        ring.setAttribute("r", rw);
        ring.setAttribute("fill", "none");
        ring.setAttribute("stroke", "var(--accent, #ffb454)");
        ring.setAttribute("stroke-opacity", "0.7");
        ring.setAttribute("stroke-width", sz(0.005));
        ring.setAttribute("stroke-dasharray", sz(0.01) + " " + sz(0.008));
        ring.style.pointerEvents = "none";
        camGroup.appendChild(ring);
        var ringGrab = document.createElementNS(SVG_NS, "circle");
        ringGrab.setAttribute("cx", entry.x); ringGrab.setAttribute("cy", entry.y);
        ringGrab.setAttribute("r", rw);
        ringGrab.setAttribute("fill", "none");
        ringGrab.setAttribute("stroke", "transparent");
        ringGrab.setAttribute("stroke-width", sz(0.025));
        ringGrab.style.cursor = "grab";
        ringGrab.setAttribute("aria-label", "aim " + camera);
        dragOn(ringGrab, camera, i, function (e, pointerAz) {
          e.azimuth = +pointerAz.toFixed(1);
          if (e.fov === undefined) e.fov = defaultFov(camera);
        });
        camGroup.appendChild(ringGrab);
        // Knob sits on the ring at the current aim.
        var knob = document.createElementNS(SVG_NS, "circle");
        knob.setAttribute("cx", entry.x + dir.x * rw);
        knob.setAttribute("cy", entry.y + dir.y * rw);
        knob.setAttribute("r", sz(0.013));
        knob.setAttribute("fill", "var(--accent, #ffb454)");
        knob.setAttribute("stroke", "var(--surface, #111)");
        knob.setAttribute("stroke-width", sz(0.004));
        knob.style.pointerEvents = "fill";
        knob.style.cursor = "grab";
        dragOn(knob, camera, i, function (e, pointerAz) {
          e.azimuth = +pointerAz.toFixed(1);
          if (e.fov === undefined) e.fov = defaultFov(camera);
        });
        camGroup.appendChild(knob);
      }

      if (hasAim && selected) {
        // Cardinal readout floats just outside the wheel: "SW 225°".
        var lx = entry.x + dir.x * sz(0.11);
        var ly = entry.y + dir.y * sz(0.11);
        var text = document.createElementNS(SVG_NS, "text");
        text.setAttribute("x", lx);
        text.setAttribute("y", ly);
        text.setAttribute("text-anchor", "middle");
        text.setAttribute("dominant-baseline", "middle");
        text.setAttribute("font-size", sz(0.028));
        text.setAttribute("fill", "var(--accent, #ffb454)");
        text.setAttribute("stroke", "var(--surface, #111)");
        text.setAttribute("stroke-width", sz(0.006));
        text.setAttribute("paint-order", "stroke");
        text.textContent = cardinalOf(az) + " " + Math.round(az) + "°";
        camGroup.appendChild(text);
      }
    });
    drawTrails(svg);
    drawCalibrationLine(svg);
    // The live + landmark layers are PERSISTENT nodes re-appended into
    // each rebuilt SVG — their children update without touching renderMap,
    // so they never fight drag gestures (see the dragOn comment).
    svg.appendChild(liveLayer);
    svg.appendChild(landmarkLayer);
    svg.appendChild(measureLayer);
    if (measurePts.length === 2) drawMeasure();
    // Coverage view: darken the ground so unlit (unwatched) area reads as
    // the blind spots.
    // backgroundColor, not the background shorthand — the shorthand would
    // wipe the floorplan's backgroundImage.
    mapEl.style.backgroundColor = (coverageToggle && coverageToggle.checked)
      ? "var(--deep, #0a0e14)" : "var(--surface)";
    mapEl.appendChild(svg);

    // Secure area rectangle (drawn by dragging empty map space).
    if (doc.secure_area) {
      var sa = doc.secure_area;
      var rect = document.createElementNS(SVG_NS, "rect");
      rect.setAttribute("x", Math.min(sa.x0, sa.x1));
      rect.setAttribute("y", Math.min(sa.y0, sa.y1));
      rect.setAttribute("width", Math.abs(sa.x1 - sa.x0));
      rect.setAttribute("height", Math.abs(sa.y1 - sa.y0));
      rect.setAttribute("fill", "var(--ok, #4caf82)");
      rect.setAttribute("fill-opacity", "0.10");
      rect.setAttribute("stroke", "var(--ok, #4caf82)");
      rect.setAttribute("stroke-width", sz(0.004));
      rect.setAttribute("stroke-dasharray", sz(0.015) + " " + sz(0.01));
      svg.appendChild(rect);
      var saLabel = document.createElementNS(SVG_NS, "text");
      saLabel.setAttribute("x", Math.min(sa.x0, sa.x1) + sz(0.012));
      saLabel.setAttribute("y", Math.min(sa.y0, sa.y1) + sz(0.035));
      saLabel.setAttribute("font-size", sz(0.026));
      saLabel.setAttribute("fill", "var(--ok, #4caf82)");
      saLabel.textContent = "secure area";
      svg.appendChild(saLabel);
    }
    clearSecureBtn.style.display = doc.secure_area ? "inline-block" : "none";
    syncSecureControls();

    var north = document.createElement("div");
    north.textContent = "N ↑";
    north.className = "help";
    north.style.cssText = "position:absolute;top:4px;left:8px;font-weight:bold";
    mapEl.appendChild(north);

    cameras.forEach(function (camera, i) {
      var pos = layoutEntry(camera, i);
      var isSel = camera === selectedCamera;
      var dot = document.createElement("div");
      dot.textContent = camera;
      // Hangs below the dot marking the exact mount point; dragging the
      // pill MOVES the camera (turning is the selected camera's wheel).
      dot.style.cssText =
        "position:absolute;transform:translate(-50%," + (isSel ? "30px" : "9px") + ");" +
        "padding:1px 7px;background:var(--surface-2);border:1px solid " +
        (isSel ? "var(--accent, #ffb454)" : "var(--stroke)") + ";" +
        "border-radius:999px;font-size:0.62em;cursor:move;user-select:none;" +
        "touch-action:none;white-space:nowrap;" +
        (isSel
          ? "box-shadow:0 0 10px var(--accent, #ffb454);z-index:2;"
          : selectedCamera ? "opacity:0.35;" : "");
      dot.style.left = unitToPct(pos.x, "x") + "%";
      dot.style.top = unitToPct(pos.y, "y") + "%";
      if (pos.x < view.x || pos.x > view.x + view.w ||
          pos.y < view.y || pos.y > view.y + view.w) {
        dot.style.display = "none";
      }
      dot.addEventListener("pointerdown", function (ev) {
        if (activeLayer !== "cameras") return;
        // Gesture tracked on window — the pill survives (no mid-drag
        // rebuild here), but window listeners are the uniform, un-killable
        // pattern all three drags share.
        ev.preventDefault();
        selectedCamera = camera;
        var moved = false;
        function move(mv) {
          if (pinchActive) return;
          moved = true;
          var p = clientToUnit(mv);
          var x = Math.min(1, Math.max(0, p.x));
          var y = Math.min(1, Math.max(0, p.y));
          var entry = ensureEntry(camera, i);
          entry.x = +x.toFixed(4);
          entry.y = +y.toFixed(4);
          dot.style.left = unitToPct(x, "x") + "%";
          dot.style.top = unitToPct(y, "y") + "%";
        }
        function up() {
          window.removeEventListener("pointermove", move);
          window.removeEventListener("pointerup", up);
          if (moved) markDirty();
          renderMap();
        }
        window.addEventListener("pointermove", move);
        window.addEventListener("pointerup", up);
      });
      mapEl.appendChild(dot);
    });
    syncDetailPanel();
  }

  // ---- Map scale, coverage view, walk trails --------------------------

  var mapScaleInput = document.getElementById("map-scale");
  var coverageToggle = document.getElementById("coverage-toggle");
  var trailsSelect = document.getElementById("trails-window");
  var trailsNote = document.getElementById("trails-note");
  var trailTracks = []; // fetched capture tracks for the current window

  mapScaleInput.addEventListener("change", function () {
    var v = parseFloat(mapScaleInput.value);
    doc.map_scale_ft = v > 0 ? v : null;
    markDirty();
    renderMap();
  });
  coverageToggle.addEventListener("change", renderMap);

  // The map's height/width ratio: the floorplan's pixel aspect when one is
  // uploaded, 1 for the square default map. Mirror of ground.map_aspect.
  function mapAspect() {
    var fp = doc && doc.floorplan;
    return fp && fp.w && fp.h ? fp.h / fp.w : 1;
  }

  // Mirror of ground.world_position: image point -> map coords, using the
  // camera's rig facts (hfov/mount/tilt), pie azimuth, and map scale.
  function worldPosition(camera, xNorm, yNorm) {
    var entry = (doc.camera_layout || {})[camera];
    var p = placements[camera];
    var scale = doc.map_scale_ft;
    if (!entry || entry.azimuth === undefined || !p || !p.hfov || !p.mount_ft ||
        p.tilt_deg === undefined || !scale) return null;
    // Vendor-published vfov wins over the 16:9 derivation (mirror of
    // ground.camera_ground).
    var vfov = p.vfov ||
      (2 * Math.atan((9 / 16) * Math.tan((p.hfov * Math.PI) / 360)) * 180) / Math.PI;
    var dep = p.tilt_deg + (yNorm - 0.5) * vfov;
    if (dep < 1) return null;
    var forward = p.mount_ft / Math.tan((dep * Math.PI) / 180);
    if (forward > 150 || forward < 0) return null;
    var lateral = (xNorm - 0.5) * 2 * forward * Math.tan((p.hfov * Math.PI) / 360);
    var rad = (entry.azimuth * Math.PI) / 180;
    return {
      x: entry.x + (forward * Math.sin(rad) + lateral * Math.cos(rad)) / scale,
      y: entry.y + (forward * -Math.cos(rad) + lateral * Math.sin(rad)) / (scale * mapAspect()),
    };
  }

  trailsSelect.addEventListener("change", async function () {
    trailTracks = [];
    trailsNote.textContent = "";
    if (!trailsSelect.value) { buildHeatmap(); renderMap(); return; }
    if (!doc.map_scale_ft) {
      trailsNote.textContent = "set map width first";
      trailsSelect.value = "";
      return;
    }
    trailsNote.textContent = "loading...";
    try {
      var data = await fetchJson("/replay/capture-window?minutes=" + trailsSelect.value);
      trailTracks = data.tracks || [];
      buildHeatmap();
      renderMap();
    } catch (err) {
      trailsNote.textContent = "error: " + err.message;
    }
  });

  var TRAIL_COLORS = ["#ffb454", "#4caf82", "#6aa5ff", "#e86a6a", "#c98add",
                      "#57c7c7", "#d8c25a", "#f08cba", "#9dbf60", "#8f9fb8"];

  function drawTrails(svg) {
    if (!trailTracks.length) return;
    var drawn = 0, skippedCams = {};
    trailTracks.forEach(function (track) {
      var pts = [];
      (track.points || []).forEach(function (p) {
        var w = worldPosition(track.camera, p[0], p[1]);
        if (w) pts.push(w);
      });
      if (pts.length < 2) {
        skippedCams[track.camera] = true;
        return;
      }
      var poly = document.createElementNS(SVG_NS, "polyline");
      poly.setAttribute(
        "points",
        pts.map(function (w) { return w.x.toFixed(4) + "," + w.y.toFixed(4); }).join(" ")
      );
      poly.setAttribute("fill", "none");
      var color = TRAIL_COLORS[cameras.indexOf(track.camera) % TRAIL_COLORS.length];
      poly.setAttribute("stroke", color);
      poly.setAttribute("stroke-width", sz(track.label === "person" ? 0.006 : 0.003));
      poly.setAttribute("stroke-opacity", track.label === "person" ? "0.9" : "0.35");
      poly.setAttribute("stroke-linejoin", "round");
      svg.appendChild(poly);
      drawn++;
    });
    var skipped = Object.keys(skippedCams);
    trailsNote.textContent = drawn + " trail(s)" +
      (skipped.length ? " — no projection for: " + skipped.join(", ") : "");
  }

  // ---- Zone overlays + live fused positions --------------------------

  var zonesToggle = document.getElementById("zones-toggle");
  var liveToggle = document.getElementById("live-toggle");
  var liveNote = document.getElementById("live-note");
  var zoneOverlays = null; // server-projected polygons; null = not fetched yet
  // Persistent SVG group re-appended by renderMap; the poll rewrites only
  // its children so live updates never rebuild the interactive map.
  var liveLayer = document.createElementNS(SVG_NS, "g");
  liveLayer.setAttribute("id", "live-layer");
  liveLayer.setAttribute("pointer-events", "none");
  var liveTimer = null;
  var liveTrails = {}; // fused-object key -> recent positions (client-side fade)

  zonesToggle.addEventListener("change", async function () {
    if (zonesToggle.checked && zoneOverlays === null) {
      try {
        var data = await fetchJson("/v1/push/map/zones");
        zoneOverlays = data.zones || [];
        if (!zoneOverlays.length) {
          liveNote.textContent = "no projectable zones (need placed cameras + scale)";
        }
      } catch (err) {
        liveNote.textContent = "zones error: " + err.message;
        zonesToggle.checked = false;
        return;
      }
    }
    renderMap();
  });

  function drawZoneOverlays(svg) {
    if (!zonesToggle || !zonesToggle.checked || !zoneOverlays) return;
    zoneOverlays.forEach(function (z) {
      var poly = document.createElementNS(SVG_NS, "polygon");
      poly.setAttribute("points", z.points.map(function (p) {
        return p[0] + "," + p[1];
      }).join(" "));
      poly.setAttribute("fill", z.color);
      poly.setAttribute("fill-opacity", "0.12");
      poly.setAttribute("stroke", z.color);
      poly.setAttribute("stroke-opacity", "0.5");
      poly.setAttribute("stroke-width", sz(0.003));
      poly.setAttribute("stroke-linejoin", "round");
      var title = document.createElementNS(SVG_NS, "title");
      title.textContent = z.name + " (" + z.camera + ")" +
        (z.clipped ? " — clipped at range limit" : "");
      poly.appendChild(title);
      svg.appendChild(poly);
    });
  }

  // ---- Footprints, rings/grid, heatmap (View visualizations) ----------

  var footprintsToggle = document.getElementById("footprints-toggle");
  var ringsToggle = document.getElementById("rings-toggle");
  var heatmapToggle = document.getElementById("heatmap-toggle");
  var footprintData = null; // fetched projected footprints; null = not loaded
  var heatmapURL = null;    // cached data-URL, rebuilt on trails change only

  footprintsToggle.addEventListener("change", async function () {
    if (footprintsToggle.checked && footprintData === null) {
      try {
        var data = await fetchJson("/v1/push/map/footprints");
        footprintData = data.footprints || [];
        if (!footprintData.length) {
          liveNote.textContent = "no projectable footprints (need placed cameras + scale)";
        }
      } catch (err) {
        liveNote.textContent = "footprints error: " + err.message;
        footprintsToggle.checked = false;
        return;
      }
    }
    renderMap();
  });

  function drawFootprints(svg) {
    if (!footprintsToggle || !footprintsToggle.checked || !footprintData) return;
    footprintData.forEach(function (f) {
      var color = TRAIL_COLORS[cameras.indexOf(f.camera) % TRAIL_COLORS.length];
      var poly = document.createElementNS(SVG_NS, "polygon");
      poly.setAttribute("points", f.points.map(function (p) {
        return p[0] + "," + p[1];
      }).join(" "));
      poly.setAttribute("fill", color);
      poly.setAttribute("fill-opacity", "0.18");
      poly.setAttribute("stroke", color);
      poly.setAttribute("stroke-opacity", "0.6");
      poly.setAttribute("stroke-width", sz(0.003));
      poly.setAttribute("stroke-linejoin", "round");
      var title = document.createElementNS(SVG_NS, "title");
      title.textContent = f.camera + " ground footprint" +
        (f.clipped ? " (clipped at horizon/range)" : "");
      poly.appendChild(title);
      svg.appendChild(poly);
    });
  }

  ringsToggle.addEventListener("change", renderMap);

  function drawRingsGrid(svg) {
    if (!ringsToggle || !ringsToggle.checked) return;
    var scale = doc.map_scale_ft;
    if (!scale) return;
    var aspect = mapAspect();
    // Grid pitch adapts to zoom so lines never crowd.
    var gridFt = view.w > 0.5 ? 25 : view.w > 0.2 ? 10 : 5;
    var stepX = gridFt / scale;
    var stepY = gridFt / (scale * aspect);
    var g = document.createElementNS(SVG_NS, "g");
    g.setAttribute("pointer-events", "none");
    var x, y;
    for (x = Math.ceil(view.x / stepX) * stepX; x <= view.x + view.w; x += stepX) {
      svgLine(g, x, view.y, x, view.y + view.w, {
        stroke: "var(--muted, #8f9fb8)", "stroke-opacity": "0.12",
        "stroke-width": sz(0.0015),
      });
    }
    for (y = Math.ceil(view.y / stepY) * stepY; y <= view.y + view.w; y += stepY) {
      svgLine(g, view.x, y, view.x + view.w, y, {
        stroke: "var(--muted, #8f9fb8)", "stroke-opacity": "0.12",
        "stroke-width": sz(0.0015),
      });
    }
    var gLabel = document.createElementNS(SVG_NS, "text");
    gLabel.setAttribute("x", view.x + sz(0.012));
    gLabel.setAttribute("y", view.y + view.w - sz(0.015));
    gLabel.setAttribute("font-size", sz(0.02));
    gLabel.setAttribute("fill", "var(--muted, #8f9fb8)");
    gLabel.textContent = "grid " + gridFt + " ft";
    g.appendChild(gLabel);
    // Range rings around the selected camera.
    var entry = selectedCamera && (doc.camera_layout || {})[selectedCamera];
    if (entry) {
      [10, 25, 50, 100].forEach(function (r) {
        var el = document.createElementNS(SVG_NS, "ellipse");
        el.setAttribute("cx", entry.x); el.setAttribute("cy", entry.y);
        el.setAttribute("rx", r / scale);
        el.setAttribute("ry", r / (scale * aspect));
        el.setAttribute("fill", "none");
        el.setAttribute("stroke", "var(--accent, #ffb454)");
        el.setAttribute("stroke-opacity", "0.35");
        el.setAttribute("stroke-width", sz(0.0025));
        el.setAttribute("stroke-dasharray", sz(0.008) + " " + sz(0.006));
        g.appendChild(el);
        var t = document.createElementNS(SVG_NS, "text");
        t.setAttribute("x", entry.x);
        t.setAttribute("y", entry.y - r / (scale * aspect) - sz(0.006));
        t.setAttribute("text-anchor", "middle");
        t.setAttribute("font-size", sz(0.02));
        t.setAttribute("fill", "var(--accent, #ffb454)");
        t.setAttribute("fill-opacity", "0.7");
        t.textContent = r + " ft";
        g.appendChild(t);
      });
    }
    svg.appendChild(g);
  }

  heatmapToggle.addEventListener("change", function () {
    if (heatmapToggle.checked && !trailTracks.length) {
      trailsNote.textContent = "pick a Walks window to feed the heatmap";
    }
    buildHeatmap();
    renderMap();
  });

  function buildHeatmap() {
    // Rebuilt only on toggle/trails change — never per renderMap frame.
    heatmapURL = null;
    if (!heatmapToggle || !heatmapToggle.checked || !trailTracks.length) return;
    var aspect = mapAspect();
    var W = 512, H = Math.max(64, Math.round(512 * aspect));
    var canvas = document.createElement("canvas");
    canvas.width = W; canvas.height = H;
    var ctx = canvas.getContext("2d");
    // Accumulate projected points into a count grid (coarser than the
    // canvas so a walk reads as a ribbon, not pinpricks).
    var GW = 128, GH = Math.max(16, Math.round(128 * aspect));
    var counts = new Float32Array(GW * GH);
    var maxC = 0;
    trailTracks.forEach(function (track) {
      (track.points || []).forEach(function (p) {
        var w = worldPosition(track.camera, p[0], p[1]);
        if (!w || w.x < 0 || w.x > 1 || w.y < 0 || w.y > 1) return;
        var gx = Math.min(GW - 1, Math.floor(w.x * GW));
        var gy = Math.min(GH - 1, Math.floor(w.y * GH));
        counts[gy * GW + gx] += 1;
        if (counts[gy * GW + gx] > maxC) maxC = counts[gy * GW + gx];
      });
    });
    if (!maxC) return;
    var logMax = Math.log(1 + maxC);
    var cellW = W / GW, cellH = H / GH;
    for (var gy = 0; gy < GH; gy++) {
      for (var gx = 0; gx < GW; gx++) {
        var c = counts[gy * GW + gx];
        if (!c) continue;
        var v = Math.log(1 + c) / logMax; // log-normalized 0..1
        // transparent -> amber -> red
        var red = 255;
        var green = Math.round(180 * (1 - v * 0.8));
        var grad = ctx.createRadialGradient(
          (gx + 0.5) * cellW, (gy + 0.5) * cellH, 0,
          (gx + 0.5) * cellW, (gy + 0.5) * cellH, cellW * 1.6
        );
        grad.addColorStop(0, "rgba(" + red + "," + green + ",0," + (0.5 * v + 0.12) + ")");
        grad.addColorStop(1, "rgba(" + red + "," + green + ",0,0)");
        ctx.fillStyle = grad;
        ctx.fillRect((gx - 1.5) * cellW, (gy - 1.5) * cellH, cellW * 4, cellH * 4);
      }
    }
    heatmapURL = canvas.toDataURL();
  }

  var LIVE_COLORS = {
    person: "#e86a6a", car: "#6aa5ff", truck: "#6aa5ff", motorcycle: "#6aa5ff",
    bicycle: "#57c7c7", dog: "#4caf82", cat: "#4caf82",
  };

  function updateLiveLayer(objects) {
    liveLayer.textContent = "";
    var seen = {};
    objects.forEach(function (o) {
      var key = (o.track_ids || []).slice().sort().join("+");
      seen[key] = true;
      var trail = liveTrails[key] = liveTrails[key] || [];
      var last = trail[trail.length - 1];
      if (!last || last.x !== o.x || last.y !== o.y) trail.push({ x: o.x, y: o.y });
      if (trail.length > 15) trail.shift();
      var color = LIVE_COLORS[o.label] || "var(--accent, #ffb454)";
      if (trail.length > 1) {
        var tp = document.createElementNS(SVG_NS, "polyline");
        tp.setAttribute("points", trail.map(function (p) {
          return p.x.toFixed(4) + "," + p.y.toFixed(4);
        }).join(" "));
        tp.setAttribute("fill", "none");
        tp.setAttribute("stroke", color);
        tp.setAttribute("stroke-opacity", "0.45");
        tp.setAttribute("stroke-width", sz(0.004));
        tp.setAttribute("stroke-linejoin", "round");
        liveLayer.appendChild(tp);
      }
      var dot = document.createElementNS(SVG_NS, "circle");
      dot.setAttribute("cx", o.x); dot.setAttribute("cy", o.y);
      dot.setAttribute("r", sz(0.009));
      dot.setAttribute("fill", color);
      dot.setAttribute("stroke", "var(--surface, #111)");
      dot.setAttribute("stroke-width", sz(0.003));
      liveLayer.appendChild(dot);
      var text = document.createElementNS(SVG_NS, "text");
      text.setAttribute("x", o.x); text.setAttribute("y", o.y + sz(0.032));
      text.setAttribute("text-anchor", "middle");
      text.setAttribute("font-size", sz(0.022));
      text.setAttribute("fill", color);
      text.setAttribute("stroke", "var(--surface, #111)");
      text.setAttribute("stroke-width", sz(0.005));
      text.setAttribute("paint-order", "stroke");
      // >1 camera = geometry fused these sightings into one object.
      text.textContent = (o.label || "object") +
        ((o.cameras || []).length > 1 ? " ×" + o.cameras.length : "");
      liveLayer.appendChild(text);
    });
    Object.keys(liveTrails).forEach(function (k) {
      if (!seen[k]) delete liveTrails[k];
    });
  }

  async function pollLive() {
    try {
      var data = await fetchJson("/v1/push/map/live");
      updateLiveLayer(data.objects || []);
      liveNote.textContent = data.objects.length
        ? data.objects.length + " live" : "live: quiet";
    } catch (err) {
      liveNote.textContent = "live error: " + err.message;
    }
  }

  function stopLive() {
    if (liveTimer) { clearInterval(liveTimer); liveTimer = null; }
  }

  liveToggle.addEventListener("change", function () {
    if (liveToggle.checked) {
      if (!doc.map_scale_ft) {
        liveNote.textContent = "set map width first";
        liveToggle.checked = false;
        return;
      }
      pollLive();
      liveTimer = setInterval(pollLive, 1000);
    } else {
      stopLive();
      liveTrails = {};
      liveLayer.textContent = "";
      liveNote.textContent = "";
    }
  });

  // Don't poll a hidden tab; resume where the user left off.
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) stopLive();
    else if (liveToggle.checked && !liveTimer) liveTimer = setInterval(pollLive, 1000);
  });

  // ---- Floorplan + scale calibration ---------------------------------

  var floorplanFile = document.getElementById("floorplan-file");
  var floorplanRemove = document.getElementById("floorplan-remove");
  var floorplanNote = document.getElementById("floorplan-note");
  var calibrateBtn = document.getElementById("calibrate-btn");

  function floorplanHref() {
    // The floorplan renders as the SVG's first <image> (renderMap), not a
    // CSS background, so it pans/zooms with the geometry.
    var fp = doc && doc.floorplan;
    if (!fp || !fp.ext) return null;
    return "/v1/push/floorplan?ts=" + encodeURIComponent(fp.uploaded_at || "");
  }

  function applyFloorplan() {
    var fp = doc.floorplan;
    if (fp && fp.ext) {
      mapEl.style.aspectRatio = fp.w + " / " + fp.h;
      floorplanRemove.style.display = "inline-block";
      calibrateBtn.style.display = "inline-block";
    } else {
      mapEl.style.aspectRatio = "";
      floorplanRemove.style.display = "none";
      calibrateBtn.style.display = "none";
    }
  }

  floorplanFile.addEventListener("change", async function () {
    var file = floorplanFile.files && floorplanFile.files[0];
    if (!file) return;
    floorplanNote.textContent = "uploading...";
    try {
      // Raw bytes, no multipart — the server identifies the image by its
      // own magic bytes and persists doc.floorplan itself.
      var data = await fetchJson("/v1/push/floorplan", { method: "POST", body: file });
      doc.floorplan = data.floorplan;
      floorplanNote.textContent =
        "uploaded — draw the map scale with Calibrate scale.";
      applyFloorplan();
      renderMap();
    } catch (err) {
      floorplanNote.textContent = "error: " + err.message;
    }
    floorplanFile.value = "";
  });

  floorplanRemove.addEventListener("click", async function () {
    try {
      await fetchJson("/v1/push/floorplan", { method: "DELETE" });
      doc.floorplan = null;
      calibrating = false;
      calibrateStart = null;
      floorplanNote.textContent = "floorplan removed.";
      applyFloorplan();
      renderMap();
    } catch (err) {
      floorplanNote.textContent = "error: " + err.message;
    }
  });

  calibrateBtn.addEventListener("click", function () {
    calibrating = !calibrating;
    calibrateStart = null;
    calibrateBtn.textContent = calibrating ? "Cancel calibration" : "Calibrate scale";
    floorplanNote.textContent = calibrating
      ? "click both ends of a feature whose real length you know (fence, driveway...)"
      : "";
    mapEl.style.cursor = calibrating ? "crosshair" : "";
  });

  function handleCalibrateClick(p) {
    if (!calibrateStart) {
      calibrateStart = p;
      floorplanNote.textContent = "now click the other end of the feature";
      return;
    }
    var a = calibrateStart, b = p;
    calibrateStart = null;
    calibrating = false;
    calibrateBtn.textContent = "Calibrate scale";
    mapEl.style.cursor = "";
    // Line length in width-normalized units (y stretched by the map's
    // aspect), so length_ft / that = the map's real-world width.
    var norm = Math.hypot(b.x - a.x, (b.y - a.y) * mapAspect());
    if (norm < 0.01) {
      floorplanNote.textContent = "line too short — try again.";
      return;
    }
    var answer = window.prompt("Real length of the drawn line, in feet:");
    var lengthFt = parseFloat(answer);
    if (!(lengthFt > 0)) {
      floorplanNote.textContent = "calibration cancelled.";
      renderMap();
      return;
    }
    doc.map_scale_ft = +(lengthFt / norm).toFixed(1);
    if (doc.floorplan) {
      doc.floorplan.calibration = {
        x0: +a.x.toFixed(4), y0: +a.y.toFixed(4),
        x1: +b.x.toFixed(4), y1: +b.y.toFixed(4),
        length_ft: +lengthFt.toFixed(1),
      };
    }
    mapScaleInput.value = doc.map_scale_ft;
    floorplanNote.textContent =
      "map width = " + doc.map_scale_ft + " ft — remember to Save.";
    markDirty();
    renderMap();
  }

  function drawCalibrationLine(svg) {
    var cal = doc.floorplan && doc.floorplan.calibration;
    if (!cal) return;
    svgLine(svg, cal.x0, cal.y0, cal.x1, cal.y1, {
      stroke: "var(--ok, #4caf82)", "stroke-width": "0.004",
      "stroke-dasharray": "0.008 0.008", "stroke-opacity": "0.7",
    });
    var label = document.createElementNS(SVG_NS, "text");
    label.setAttribute("x", (cal.x0 + cal.x1) / 2);
    label.setAttribute("y", (cal.y0 + cal.y1) / 2 - 0.012);
    label.setAttribute("text-anchor", "middle");
    label.setAttribute("font-size", sz(0.024));
    label.setAttribute("fill", "var(--ok, #4caf82)");
    label.textContent = cal.length_ft + " ft";
    svg.appendChild(label);
  }

  // ---- Onboarding ----------------------------------------------------

  var onboardSection = document.getElementById("onboard-section");
  var onboardCards = document.getElementById("onboard-cards");

  function lensById(id) {
    for (var i = 0; i < lensPresets.length; i++) {
      if (lensPresets[i].id === id) return lensPresets[i];
    }
    return null;
  }

  function hfovFromFocal(lens, focalMm) {
    return (2 * Math.atan(lens.sensor_width_mm /
      (2 * (focalMm + lens.focal_offset_mm))) * 180) / Math.PI;
  }

  function renderOnboarding() {
    var pending = cameras.filter(function (c) { return !placements[c]; });
    onboardSection.style.display = pending.length ? "block" : "none";
    onboardCards.textContent = "";
    pending.forEach(function (camera) {
      var card = document.createElement("div");
      card.className = "stat-card";
      card.style.minWidth = "300px";

      var label = document.createElement("div");
      label.className = "stat-label";
      label.textContent = camera;
      card.appendChild(label);

      var img = document.createElement("img");
      img.src = "/api/" + encodeURIComponent(camera) + "/latest.jpg?h=180";
      img.alt = camera;
      img.style.cssText = "display:block;width:100%;border-radius:4px;margin-top:0.4em";
      card.appendChild(img);

      function row(labelText, input, suffix) {
        var l = document.createElement("label");
        l.className = "help";
        l.style.cssText = "display:block;margin-top:0.4em";
        l.appendChild(document.createTextNode(labelText + " "));
        l.appendChild(input);
        if (suffix) l.appendChild(document.createTextNode(" " + suffix));
        card.appendChild(l);
        return l;
      }

      var lensSel = document.createElement("select");
      lensPresets.forEach(function (lens) {
        var opt = document.createElement("option");
        opt.value = lens.id;
        opt.textContent = lens.label;
        lensSel.appendChild(opt);
      });
      lensSel.value = "custom";
      row("lens", lensSel);

      var focal = document.createElement("input");
      focal.type = "range";
      focal.step = "0.1";
      var focalRow = row("zoom", focal);
      var focalLabel = document.createElement("span");
      focalRow.appendChild(focalLabel);

      var hfov = document.createElement("input");
      hfov.type = "number";
      hfov.min = "11"; hfov.max = "360"; hfov.step = "1";
      hfov.className = "num-4e";
      hfov.value = "90";
      row("field of view", hfov, "°");

      function syncLens() {
        var lens = lensById(lensSel.value);
        var isVari = lens && lens.type === "varifocal";
        focalRow.style.display = isVari ? "block" : "none";
        if (isVari) {
          focal.min = lens.focal_min; focal.max = lens.focal_max;
          focal.value = lens.focal_min;
          hfov.value = Math.round(hfovFromFocal(lens, lens.focal_min));
          focalLabel.textContent = " " + lens.focal_min + " mm";
        } else if (lens && lens.type === "fixed") {
          hfov.value = lens.hfov;
        }
      }
      lensSel.addEventListener("change", syncLens);
      focal.addEventListener("input", function () {
        var lens = lensById(lensSel.value);
        if (lens && lens.type === "varifocal") {
          hfov.value = Math.round(hfovFromFocal(lens, parseFloat(focal.value)));
          focalLabel.textContent = " " + focal.value + " mm";
        }
      });
      syncLens();

      var mount = document.createElement("input");
      mount.type = "number";
      mount.min = "1"; mount.max = "500"; mount.step = "0.5";
      mount.className = "num-4e";
      mount.value = "10";
      row("mounting height", mount, "ft");

      var tilt = document.createElement("input");
      tilt.type = "number";
      tilt.min = "-90"; tilt.max = "90"; tilt.step = "1";
      tilt.className = "num-4e";
      tilt.value = "12";
      row("tilt (down-angle)", tilt, "°");

      var faces = document.createElement("select");
      CARDINALS.forEach(function (c) {
        var opt = document.createElement("option");
        opt.value = c;
        opt.textContent = c;
        faces.appendChild(opt);
      });
      row("faces", faces);

      var btn = document.createElement("button");
      btn.textContent = "Onboard → place on map";
      btn.className = "btn-primary";
      btn.style.marginTop = "0.5em";
      btn.addEventListener("click", function () {
        var h = parseFloat(hfov.value), m = parseFloat(mount.value),
            t = parseFloat(tilt.value);
        if (!(h > 10 && h <= 360) || !(m > 0 && m <= 500) || !(t >= -90 && t <= 90)) {
          showBanner("onboard " + camera + ": HFOV 10–360°, mount 0–500 ft, tilt -90–90°.", true);
          return;
        }
        if (!doc.camera_optics) doc.camera_optics = {};
        var entry = { hfov: h, mount_ft: m, tilt_deg: t, faces: faces.value };
        if (lensSel.value !== "custom") entry.lens = lensSel.value;
        doc.camera_optics[camera] = entry;
        placements = doc.camera_optics;
        if (!doc.camera_layout) doc.camera_layout = {};
        if (!doc.camera_layout[camera]) {
          // Start at the camera's spread-out ghost spot, not map center —
          // center would stack every onboarded-but-unplaced camera on the
          // same pixel.
          var ghost = layoutEntry(camera, cameras.indexOf(camera));
          doc.camera_layout[camera] = {
            x: +ghost.x.toFixed(4), y: +ghost.y.toFixed(4),
            azimuth: CARDINAL_DEG[faces.value],
            fov: +h.toFixed(1),
          };
        }
        setLayer("cameras");
        placeMode = camera;
        showBanner("Click the map where " + camera + " is mounted.", false);
        markDirty();
        renderOnboarding();
        renderMap();
      });
      card.appendChild(btn);
      onboardCards.appendChild(card);
    });
  }

  function mapUnit(ev) {
    var p = clientToUnit(ev);
    return {
      x: Math.min(1, Math.max(0, p.x)),
      y: Math.min(1, Math.max(0, p.y)),
    };
  }

  // ---- Zoom + pan -----------------------------------------------------

  var viewResetBtn = document.getElementById("view-reset");

  function syncViewReset() {
    viewResetBtn.style.display = view.w < 1 ? "inline-block" : "none";
  }

  mapEl.addEventListener("wheel", function (ev) {
    ev.preventDefault();
    var p = clientToUnit(ev);
    var oldW = view.w;
    view.w = view.w * (ev.deltaY > 0 ? 1.15 : 1 / 1.15);
    clampView();
    // Keep the point under the cursor stationary.
    view.x = p.x - (p.x - view.x) * (view.w / oldW);
    view.y = p.y - (p.y - view.y) * (view.w / oldW);
    clampView();
    syncViewReset();
    renderMap();
  }, { passive: false });

  mapEl.addEventListener("dblclick", function () {
    resetView();
    syncViewReset();
  });
  viewResetBtn.addEventListener("click", function () {
    resetView();
    syncViewReset();
  });

  function startPan(ev) {
    ev.preventDefault();
    var last = { x: ev.clientX, y: ev.clientY };
    var rect = null;
    function move(mv) {
      if (pinchActive) return; // second finger landed: pinch owns the view
      rect = mapEl.getBoundingClientRect(); // fresh: layout can shift
      view.x -= ((mv.clientX - last.x) / rect.width) * view.w;
      view.y -= ((mv.clientY - last.y) / rect.height) * view.w;
      last = { x: mv.clientX, y: mv.clientY };
      clampView();
      renderMap();
    }
    function up() {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    }
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  }

  // ---- Pinch zoom (touch) ---------------------------------------------
  // Tracked in the capture phase so a pinch wins even when the first
  // finger landed on a camera element; single-finger gestures check
  // pinchActive and go quiet while two fingers are down.
  var touchPts = {};
  var pinchActive = false;
  var pinchStart = null; // {dist, w, mid:{x,y unit}}

  function touchIds() { return Object.keys(touchPts); }

  mapEl.addEventListener("pointerdown", function (ev) {
    if (ev.pointerType !== "touch") return;
    touchPts[ev.pointerId] = { x: ev.clientX, y: ev.clientY };
    var ids = touchIds();
    if (ids.length === 2) {
      var a = touchPts[ids[0]], b = touchPts[ids[1]];
      var rect = mapEl.getBoundingClientRect();
      pinchActive = true;
      pinchStart = {
        dist: Math.hypot(a.x - b.x, a.y - b.y),
        w: view.w,
        mid: {
          x: view.x + (((a.x + b.x) / 2 - rect.left) / rect.width) * view.w,
          y: view.y + (((a.y + b.y) / 2 - rect.top) / rect.height) * view.w,
        },
      };
      ev.stopPropagation();
    }
  }, true);

  window.addEventListener("pointermove", function (ev) {
    if (ev.pointerType !== "touch" || !touchPts[ev.pointerId]) return;
    touchPts[ev.pointerId] = { x: ev.clientX, y: ev.clientY };
    if (!pinchActive) return;
    var ids = touchIds();
    if (ids.length < 2) return;
    var a = touchPts[ids[0]], b = touchPts[ids[1]];
    var dist = Math.hypot(a.x - b.x, a.y - b.y);
    if (dist < 10 || !pinchStart.dist) return;
    var rect = mapEl.getBoundingClientRect();
    view.w = pinchStart.w * (pinchStart.dist / dist);
    clampView();
    // Keep the pinch midpoint's map location under the fingers.
    var midClient = {
      x: ((a.x + b.x) / 2 - rect.left) / rect.width,
      y: ((a.y + b.y) / 2 - rect.top) / rect.height,
    };
    view.x = pinchStart.mid.x - midClient.x * view.w;
    view.y = pinchStart.mid.y - midClient.y * view.w;
    clampView();
    syncViewReset();
    renderMap();
  });

  function endTouch(ev) {
    if (ev.pointerType !== "touch") return;
    delete touchPts[ev.pointerId];
    if (touchIds().length < 2) {
      pinchActive = false;
      pinchStart = null;
    }
  }
  window.addEventListener("pointerup", endTouch);
  window.addEventListener("pointercancel", endTouch);

  // ---- Measure tool (View layer) --------------------------------------

  var measureBtn = document.getElementById("measure-btn");
  var measureNote = document.getElementById("measure-note");
  var measureArmed = false;
  var measurePts = []; // 0, 1 (anchored, previewing) or 2 (frozen) points
  var measureLayer = document.createElementNS(SVG_NS, "g");
  measureLayer.setAttribute("id", "measure-layer");
  measureLayer.setAttribute("pointer-events", "none");

  function measureDistFt(a, b) {
    // Same aspect-corrected math as the scale-calibration line.
    var scale = doc.map_scale_ft || 0;
    return Math.hypot((b.x - a.x) * scale, (b.y - a.y) * scale * mapAspect());
  }

  function drawMeasure(previewPt) {
    measureLayer.textContent = "";
    if (!measurePts.length) return;
    var a = measurePts[0];
    var b = measurePts[1] || previewPt;
    if (!b) return;
    var line = document.createElementNS(SVG_NS, "line");
    line.setAttribute("x1", a.x); line.setAttribute("y1", a.y);
    line.setAttribute("x2", b.x); line.setAttribute("y2", b.y);
    line.setAttribute("stroke", "var(--warn, #e8a735)");
    line.setAttribute("stroke-width", sz(0.004));
    line.setAttribute("stroke-dasharray", sz(0.014) + " " + sz(0.008));
    measureLayer.appendChild(line);
    [a, b].forEach(function (p) {
      var c = document.createElementNS(SVG_NS, "circle");
      c.setAttribute("cx", p.x); c.setAttribute("cy", p.y);
      c.setAttribute("r", sz(0.006));
      c.setAttribute("fill", "var(--warn, #e8a735)");
      measureLayer.appendChild(c);
    });
    var t = document.createElementNS(SVG_NS, "text");
    t.setAttribute("x", (a.x + b.x) / 2);
    t.setAttribute("y", (a.y + b.y) / 2 - sz(0.015));
    t.setAttribute("text-anchor", "middle");
    t.setAttribute("font-size", sz(0.026));
    t.setAttribute("fill", "var(--warn, #e8a735)");
    t.setAttribute("stroke", "var(--surface, #111)");
    t.setAttribute("stroke-width", sz(0.005));
    t.setAttribute("paint-order", "stroke");
    t.textContent = measureDistFt(a, b).toFixed(1) + " ft";
    measureLayer.appendChild(t);
  }

  function clearMeasure() {
    measureArmed = false;
    measurePts = [];
    measureLayer.textContent = "";
    measureNote.textContent = "";
    mapEl.style.cursor = "";
    window.removeEventListener("pointermove", measurePreview);
  }

  function measurePreview(mv) {
    if (measurePts.length === 1) drawMeasure(mapUnit(mv));
  }

  function handleMeasureClick(p) {
    if (measurePts.length >= 2) { // third click starts over
      measurePts = [];
      measureLayer.textContent = "";
    }
    measurePts.push(p);
    if (measurePts.length === 1) {
      window.addEventListener("pointermove", measurePreview);
      measureNote.textContent = "click the far end";
    } else {
      window.removeEventListener("pointermove", measurePreview);
      drawMeasure();
      measureNote.textContent = measureDistFt(measurePts[0], measurePts[1]).toFixed(1) + " ft";
    }
  }

  measureBtn.addEventListener("click", function () {
    if (!doc.map_scale_ft) {
      measureNote.textContent = "set the map scale first";
      return;
    }
    if (measureArmed || measurePts.length) { clearMeasure(); return; }
    setLayer("view");
    measureArmed = true;
    mapEl.style.cursor = "crosshair";
    measureNote.textContent = "click the first point";
  });

  function isMapBackground(ev) {
    return ev.target === mapEl || ev.target.tagName === "svg" ||
      ev.target.tagName === "image";
  }

  var secureDrawArmed = false;
  var secureRedrawBtn = document.getElementById("secure-redraw");

  function syncSecureControls() {
    secureRedrawBtn.style.display = doc && doc.secure_area ? "inline-block" : "none";
    secureRedrawBtn.textContent = secureDrawArmed ? "Cancel redraw" : "Redraw secure area";
  }

  secureRedrawBtn.addEventListener("click", function () {
    secureDrawArmed = !secureDrawArmed;
    mapEl.style.cursor = secureDrawArmed ? "crosshair" : "";
    syncSecureControls();
  });

  function secureAreaDrag(ev) {
    // mapUnit per move (never a cached rect): a pan/zoom mid-drag must not
    // un-anchor the rectangle.
    var start = mapUnit(ev);
    var moved = false;
    mapEl.setPointerCapture(ev.pointerId);
    function move(mv) {
      if (pinchActive) return;
      var p = mapUnit(mv);
      if (Math.hypot(p.x - start.x, p.y - start.y) < sz(0.02)) return;
      moved = true;
      doc.secure_area = {
        x0: +start.x.toFixed(4), y0: +start.y.toFixed(4),
        x1: +p.x.toFixed(4), y1: +p.y.toFixed(4),
      };
      renderMap();
    }
    function up() {
      mapEl.removeEventListener("pointermove", move);
      mapEl.removeEventListener("pointerup", up);
      if (moved) {
        markDirty();
        secureDrawArmed = false; // one redraw per arm — then locked again
        mapEl.style.cursor = "";
        syncSecureControls();
      }
    }
    mapEl.addEventListener("pointermove", move);
    mapEl.addEventListener("pointerup", up);
  }

  // The per-layer map dispatcher. Per-element camera drags (pie, ring,
  // moveGrab, pills) stopPropagation before this fires and are themselves
  // gated to the Cameras layer.
  mapEl.addEventListener("pointerdown", function (ev) {
    if (ev.button === 1) { // middle-button pan works in every layer/mode
      startPan(ev);
      return;
    }
    if (activeLayer === "cameras") {
      if (placeMode) {
        var cam = placeMode;
        placeMode = null;
        var p = mapUnit(ev);
        if (!doc.camera_layout) doc.camera_layout = {};
        var entry = doc.camera_layout[cam] || {};
        entry.x = +p.x.toFixed(4);
        entry.y = +p.y.toFixed(4);
        doc.camera_layout[cam] = entry;
        selectedCamera = cam;
        showBanner(cam + " placed — drag its arrow to aim, then Save.", false);
        markDirty();
        renderMap();
        return;
      }
      if (isMapBackground(ev) && selectedCamera) {
        selectedCamera = null;
        renderMap();
      }
      return;
    }
    if (activeLayer === "calibrate") {
      if (landmarkMode) { handleLandmarkMapClick(mapUnit(ev)); return; }
      if (calibrating) { handleCalibrateClick(mapUnit(ev)); return; }
      return;
    }
    if (activeLayer === "areas") {
      // Locked once set: an existing rectangle only redraws after an
      // explicit "Redraw" (accidental drags kept wrecking it).
      if (isMapBackground(ev) && (!doc.secure_area || secureDrawArmed)) {
        secureAreaDrag(ev);
      }
      return;
    }
    // View layer: measure when armed, else drag pans.
    if (measureArmed) { handleMeasureClick(mapUnit(ev)); return; }
    if (isMapBackground(ev)) startPan(ev);
  });

  function typingInField() {
    var el = document.activeElement;
    if (!el) return false;
    return el.tagName === "INPUT" || el.tagName === "TEXTAREA" ||
      el.tagName === "SELECT" || el.isContentEditable;
  }

  // Escape cancels the active layer's mode; arrows/brackets fine-tune the
  // selected camera (Cameras layer only, never while typing in a field).
  document.addEventListener("keydown", function (ev) {
    if (ev.key !== "Escape" && activeLayer === "cameras" && selectedCamera &&
        doc && !typingInField()) {
      var nudgeFt = ev.shiftKey ? 1.0 : 0.1;
      var scale = doc.map_scale_ft;
      var e, i;
      if (scale &&
          ["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].indexOf(ev.key) !== -1) {
        ev.preventDefault();
        i = cameras.indexOf(selectedCamera);
        e = ensureEntry(selectedCamera, i);
        var dx = ev.key === "ArrowLeft" ? -1 : ev.key === "ArrowRight" ? 1 : 0;
        var dy = ev.key === "ArrowUp" ? -1 : ev.key === "ArrowDown" ? 1 : 0;
        e.x = +Math.min(1, Math.max(0, e.x + (dx * nudgeFt) / scale)).toFixed(4);
        e.y = +Math.min(1, Math.max(
          0, e.y + (dy * nudgeFt) / (scale * mapAspect()))).toFixed(4);
        markDirty();
        renderMap();
        return;
      }
      if (ev.key === "[" || ev.key === "]") {
        ev.preventDefault();
        i = cameras.indexOf(selectedCamera);
        e = ensureEntry(selectedCamera, i);
        var step = (ev.shiftKey ? 5 : 0.5) * (ev.key === "[" ? -1 : 1);
        e.azimuth = +(((e.azimuth || 0) + step + 360) % 360).toFixed(1);
        if (e.fov === undefined) e.fov = defaultFov(selectedCamera);
        markDirty();
        renderMap();
        return;
      }
    }
    if (ev.key !== "Escape") return;
    if (activeLayer === "cameras" && placeMode) { placeMode = null; renderMap(); return; }
    if (activeLayer === "areas" && secureDrawArmed) {
      secureDrawArmed = false;
      mapEl.style.cursor = "";
      syncSecureControls();
      return;
    }
    if (activeLayer === "calibrate") {
      if (landmarkMode && landmarkPending) {
        landmarkPending = null;
        drawLandmarkMarkers();
        landmarkStatus();
        return;
      }
      if (calibrating) {
        calibrating = false;
        calibrateStart = null;
        calibrateBtn.textContent = "Calibrate scale";
        mapEl.style.cursor = "";
        return;
      }
    }
    if (activeLayer === "view" && (measureArmed || measurePts.length)) {
      clearMeasure();
      return;
    }
    if (selectedCamera) { selectedCamera = null; renderMap(); }
  });

  document.getElementById("placement-fov-btn").addEventListener("click", function () {
    var changed = 0;
    cameras.forEach(function (camera) {
      var entry = (doc.camera_layout || {})[camera];
      var p = placements[camera];
      if (!entry || !p || !p.hfov) return;
      if (entry.fov !== p.hfov) { entry.fov = p.hfov; changed++; }
    });
    if (changed) { markDirty(); renderMap(); }
    suggestDiff.textContent = changed
      ? "set " + changed + " pie width(s) from placement HFOV — remember to Save."
      : "all placed pies already match placement HFOV.";
  });

  clearSecureBtn.addEventListener("click", function () {
    doc.secure_area = null;
    markDirty();
    renderMap();
  });

  function syncDetailPanel() {
    if (!selectedCamera) { detailPanel.style.display = "none"; return; }
    var entry = (doc.camera_layout || {})[selectedCamera];
    detailPanel.style.display = "block";
    detailName.textContent = selectedCamera;
    detailAzimuth.value = entry && entry.azimuth !== undefined ? entry.azimuth : "";
    detailFov.value = entry && entry.fov !== undefined ? entry.fov : defaultFov(selectedCamera);
    document.getElementById("detail-cardinal").textContent =
      entry && entry.azimuth !== undefined ? cardinalOf(entry.azimuth) : "";
    var p = placements[selectedCamera];
    detailHfov.value = p && p.hfov !== undefined ? p.hfov : "";
    detailMount.value = p && p.mount_ft !== undefined ? p.mount_ft : "";
    detailTilt.value = p && p.tilt_deg !== undefined ? p.tilt_deg : "";
    document.getElementById("detail-placement").textContent = p
      ? (p.faces ? "faces " + p.faces : "")
      : "not onboarded — no rig facts";
    // Position in feet from the map's top-left (x -> east, y -> south).
    var scale = doc.map_scale_ft;
    var haveFt = entry && scale;
    detailX.disabled = detailY.disabled = !haveFt;
    detailX.value = haveFt ? (entry.x * scale).toFixed(1) : "";
    detailY.value = haveFt ? (entry.y * scale * mapAspect()).toFixed(1) : "";
    detailX.title = detailY.title = haveFt
      ? "feet from the map's top-left corner"
      : "set the map scale to edit position in feet";
  }

  var detailX = document.getElementById("detail-x");
  var detailY = document.getElementById("detail-y");
  detailX.addEventListener("change", function () {
    if (!selectedCamera || !doc.map_scale_ft) return;
    var e = ensureEntry(selectedCamera, cameras.indexOf(selectedCamera));
    e.x = +Math.min(1, Math.max(0, (parseFloat(detailX.value) || 0) / doc.map_scale_ft)).toFixed(4);
    markDirty();
    renderMap();
  });
  detailY.addEventListener("change", function () {
    if (!selectedCamera || !doc.map_scale_ft) return;
    var e = ensureEntry(selectedCamera, cameras.indexOf(selectedCamera));
    var ftPerUnit = doc.map_scale_ft * mapAspect();
    e.y = +Math.min(1, Math.max(0, (parseFloat(detailY.value) || 0) / ftPerUnit)).toFixed(4);
    markDirty();
    renderMap();
  })

  var detailHfov = document.getElementById("detail-hfov");
  var detailMount = document.getElementById("detail-mount");
  var detailTilt = document.getElementById("detail-tilt");

  function opticsEntry() {
    if (!selectedCamera) return null;
    if (!doc.camera_optics) doc.camera_optics = {};
    if (!doc.camera_optics[selectedCamera]) {
      doc.camera_optics[selectedCamera] = { hfov: 90, mount_ft: 10, tilt_deg: 12 };
    }
    placements = doc.camera_optics;
    return doc.camera_optics[selectedCamera];
  }

  detailHfov.addEventListener("change", function () {
    var e = opticsEntry();
    if (!e) return;
    e.hfov = Math.min(360, Math.max(10.5, parseFloat(detailHfov.value) || 90));
    markDirty();
    renderMap();
  });
  detailMount.addEventListener("change", function () {
    var e = opticsEntry();
    if (!e) return;
    e.mount_ft = Math.min(500, Math.max(0.5, parseFloat(detailMount.value) || 10));
    markDirty();
    renderMap();
  });
  detailTilt.addEventListener("change", function () {
    var e = opticsEntry();
    if (!e) return;
    e.tilt_deg = Math.min(90, Math.max(-90, parseFloat(detailTilt.value) || 0));
    markDirty();
    renderMap();
  });

  detailAzimuth.addEventListener("change", function () {
    if (!selectedCamera) return;
    var e = ensureEntry(selectedCamera, cameras.indexOf(selectedCamera));
    e.azimuth = ((parseFloat(detailAzimuth.value) || 0) % 360 + 360) % 360;
    if (e.fov === undefined) e.fov = defaultFov(selectedCamera);
    markDirty();
    renderMap();
  });
  detailFov.addEventListener("change", function () {
    if (!selectedCamera) return;
    var e = ensureEntry(selectedCamera, cameras.indexOf(selectedCamera));
    e.fov = Math.min(360, Math.max(10, parseFloat(detailFov.value) || defaultFov(selectedCamera)));
    markDirty();
    renderMap();
  });

  // ---- Neighbor suggestions -----------------------------------------

  function currentPairs() {
    var table = doc.camera_neighbors || {};
    var pairs = {};
    Object.keys(table).forEach(function (cam) {
      (table[cam] || []).forEach(function (other) {
        pairs[[cam, other].sort().join("↔")] = true;
      });
    });
    return pairs;
  }

  function sectorContains(entry, px, py, r) {
    var dx = px - entry.x, dy = py - entry.y;
    var d = Math.hypot(dx, dy);
    if (d > r) return false;
    if (entry.azimuth === undefined) return true; // no pie: plain circle
    if (d < 1e-6) return true;
    var pointAz = ((Math.atan2(dx, -dy) * 180) / Math.PI + 360) % 360;
    var diff = Math.abs(((pointAz - entry.azimuth + 540) % 360) - 180);
    return diff <= (entry.fov || DEFAULT_FOV) / 2;
  }

  function sectorsOverlap(a, b, r) {
    // Cameras watch the same ground when their view sectors intersect:
    // apex-in-sector catches close pairs, sampled interior points catch
    // side-by-side pairs looking at the same spot.
    if (sectorContains(a, b.x, b.y, r) || sectorContains(b, a.x, a.y, r)) return true;
    var STEPS_ANG = 9, STEPS_RAD = 4;
    for (var s = 0; s < 2; s++) {
      var from = s === 0 ? a : b, into = s === 0 ? b : a;
      var fov = from.azimuth === undefined ? 360 : (from.fov || DEFAULT_FOV);
      var az = from.azimuth === undefined ? 0 : from.azimuth;
      for (var i = 0; i <= STEPS_ANG; i++) {
        var ang = ((az - fov / 2 + (fov * i) / STEPS_ANG) * Math.PI) / 180;
        for (var j = 1; j <= STEPS_RAD; j++) {
          var rr = (r * j) / STEPS_RAD;
          var px = from.x + Math.sin(ang) * rr;
          var py = from.y - Math.cos(ang) * rr;
          if (sectorContains(into, px, py, r)) return true;
        }
      }
    }
    return false;
  }

  suggestBtn.addEventListener("click", function () {
    var layout = doc.camera_layout || {};
    var placed = cameras.filter(function (c) { return layout[c]; });
    var radius = parseFloat(radiusInput.value);
    var suggested = {};
    placed.forEach(function (a, i) {
      placed.slice(i + 1).forEach(function (b) {
        if (sectorsOverlap(layout[a], layout[b], radius)) {
          suggested[[a, b].sort().join("↔")] = true;
        }
      });
    });
    var existing = currentPairs();
    var add = Object.keys(suggested).filter(function (p) { return !existing[p]; });
    var remove = Object.keys(existing).filter(function (p) { return !suggested[p]; });
    if (!placed.length) {
      suggestDiff.textContent = "Place cameras on the map first.";
      return;
    }
    suggestDiff.textContent =
      (add.length ? "would add: " + add.join(", ") + ". " : "") +
      (remove.length ? "would remove: " + remove.join(", ") + ". " : "") +
      (!add.length && !remove.length ? "no changes — layout matches current neighbors." : "");
    if (add.length || remove.length) {
      var apply = document.createElement("button");
      apply.textContent = "Apply";
      apply.className = "btn-primary";
      apply.style.marginLeft = "0.6em";
      apply.addEventListener("click", function () {
        var table = {};
        Object.keys(suggested).forEach(function (pair) {
          var parts = pair.split("↔");
          var list = table[parts[0]] || [];
          list.push(parts[1]);
          table[parts[0]] = list;
        });
        doc.camera_neighbors = table;
        suggestDiff.textContent = "applied — remember to Save.";
        markDirty();
      });
      suggestDiff.appendChild(apply);
    }
  });

  radiusInput.addEventListener("input", function () {
    radiusLabel.textContent = radiusInput.value;
    renderMap(); // wedges are drawn at view-reach radius
  });

  // ---- Auto-tune aim from capture history ----------------------------

  var autotuneBtn = document.getElementById("autotune-btn");
  var autotuneDiff = document.getElementById("autotune-diff");

  autotuneBtn.addEventListener("click", async function () {
    autotuneBtn.disabled = true;
    autotuneDiff.textContent = "tuning... (replaying the capture window)";
    try {
      var body = await fetchJson("/v1/push/map/autotune?minutes=240", { method: "POST" });
      var report = body.report;
      var totalPairs = Object.keys(report.pair_counts || {}).reduce(function (n, k) {
        return n + report.pair_counts[k];
      }, 0);
      autotuneDiff.textContent =
        "RMS " + report.rms_before_ft + " → " + report.rms_after_ft +
        " ft over " + totalPairs + " pairs (" + body.elapsed_s + "s). ";
      var changed = [];
      Object.keys(report.cameras).sort().forEach(function (cam) {
        var c = report.cameras[cam];
        var dAz = Math.abs(((c.azimuth_after - c.azimuth_before + 540) % 360) - 180);
        var dTilt = Math.abs(c.tilt_after - c.tilt_before);
        if (dAz < 0.2 && dTilt < 0.2) return;
        changed.push(cam);
        var line = document.createElement("div");
        line.textContent = cam + ": azimuth " + c.azimuth_before + "° → " +
          c.azimuth_after + "°, tilt " + c.tilt_before + "° → " +
          c.tilt_after + "° (" + c.pairs + " pairs)";
        autotuneDiff.appendChild(line);
      });
      (report.warnings || []).forEach(function (w) {
        var line = document.createElement("div");
        line.textContent = "⚠ " + w;
        autotuneDiff.appendChild(line);
      });
      if (!changed.length) {
        autotuneDiff.appendChild(document.createTextNode(
          "no meaningful corrections — aim already agrees with the data."
        ));
      } else {
        var apply = document.createElement("button");
        apply.textContent = "Apply";
        apply.className = "btn-primary";
        apply.style.marginLeft = "0.6em";
        apply.addEventListener("click", function () {
          changed.forEach(function (cam) {
            var c = report.cameras[cam];
            if ((doc.camera_layout || {})[cam]) {
              doc.camera_layout[cam].azimuth = c.azimuth_after;
            }
            if ((doc.camera_optics || {})[cam]) {
              doc.camera_optics[cam].tilt_deg = c.tilt_after;
            }
          });
          autotuneDiff.textContent = "applied — remember to Save.";
          markDirty();
          renderMap();
        });
        autotuneDiff.appendChild(apply);
      }
    } catch (err) {
      autotuneDiff.textContent = "auto-tune error: " + err.message;
    }
    autotuneBtn.disabled = false;
  });

  // ---- Landmark calibrator: measure HFOV/azimuth/tilt per camera ------

  var landmarkBtn = document.getElementById("landmark-btn");
  var landmarkSection = document.getElementById("landmark-section");
  var landmarkInstructions = document.getElementById("landmark-instructions");
  var landmarkSnap = document.getElementById("landmark-snapshot");
  var landmarkWrap = document.getElementById("landmark-snapshot-wrap");
  var landmarkSolveBtn = document.getElementById("landmark-solve");
  var landmarkUndoBtn = document.getElementById("landmark-undo");
  var landmarkCancelBtn = document.getElementById("landmark-cancel");
  var landmarkResult = document.getElementById("landmark-result");
  var landmarkMode = null;    // camera being calibrated, or null
  var landmarkMatches = [];   // completed {u, v, mx, my}
  var landmarkPending = null; // image click waiting for its map click
  // Persistent SVG group re-appended by renderMap (same pattern as
  // liveLayer) so markers survive map redraws.
  var landmarkLayer = document.createElementNS(SVG_NS, "g");
  landmarkLayer.setAttribute("id", "landmark-layer");
  landmarkLayer.setAttribute("pointer-events", "none");

  function landmarkStatus() {
    var n = landmarkMatches.length;
    if (landmarkPending) {
      landmarkInstructions.textContent =
        "Point " + (n + 1) + ": now click the SAME spot on the floorplan map.";
    } else {
      landmarkInstructions.textContent =
        "Click a ground landmark in the snapshot (gate post, path corner...), " +
        "then the same spot on the map. " + n + " matched — " +
        (n >= 2 ? "ready to solve; more points = better." : "need at least 2.");
    }
    landmarkSolveBtn.disabled = landmarkMatches.length < 2 || !!landmarkPending;
  }

  function drawLandmarkMarkers() {
    // Snapshot dots (numbered divs over the img).
    landmarkWrap.querySelectorAll(".lm-dot").forEach(function (d) { d.remove(); });
    landmarkLayer.textContent = "";
    var all = landmarkMatches.concat(landmarkPending ? [landmarkPending] : []);
    all.forEach(function (m, i) {
      var d = document.createElement("div");
      d.className = "lm-dot";
      d.textContent = i + 1;
      d.style.cssText =
        "position:absolute;transform:translate(-50%,-50%);width:18px;height:18px;" +
        "border-radius:50%;background:var(--accent, #ffb454);color:#000;" +
        "font-size:11px;font-weight:bold;text-align:center;line-height:18px;" +
        "pointer-events:none;left:" + m.u * 100 + "%;top:" + m.v * 100 + "%";
      landmarkWrap.appendChild(d);
      if (m.mx === undefined) return;
      var c = document.createElementNS(SVG_NS, "circle");
      c.setAttribute("cx", m.mx); c.setAttribute("cy", m.my);
      c.setAttribute("r", sz(0.010));
      c.setAttribute("fill", "var(--accent, #ffb454)");
      c.setAttribute("stroke", "#000");
      c.setAttribute("stroke-width", sz(0.002));
      landmarkLayer.appendChild(c);
      var t = document.createElementNS(SVG_NS, "text");
      t.setAttribute("x", m.mx); t.setAttribute("y", m.my + sz(0.005));
      t.setAttribute("text-anchor", "middle");
      t.setAttribute("font-size", sz(0.018));
      t.setAttribute("fill", "#000");
      t.textContent = i + 1;
      landmarkLayer.appendChild(t);
    });
  }

  function closeLandmarkMode() {
    landmarkMode = null;
    landmarkMatches = [];
    landmarkPending = null;
    landmarkSection.style.display = "none";
    landmarkResult.textContent = "";
    drawLandmarkMarkers();
  }

  landmarkBtn.addEventListener("click", function () {
    if (!selectedCamera) return;
    var entry = (doc.camera_layout || {})[selectedCamera];
    if (!entry || entry.azimuth === undefined || !doc.map_scale_ft) {
      landmarkResult.textContent = "";
      showBanner(
        "Landmark calibrate needs the camera placed + aimed and the map scale set.",
        true
      );
      return;
    }
    var lmCam = selectedCamera;
    setLayer("calibrate"); // landmark clicks live on the Calibrate layer
    selectedCamera = lmCam;
    landmarkMode = lmCam;
    landmarkMatches = [];
    landmarkPending = null;
    landmarkResult.textContent = "";
    landmarkSection.style.display = "block";
    // Through the sidecar's Frigate proxy — same session cookie.
    landmarkSnap.src = "/api/" + encodeURIComponent(landmarkMode) +
      "/latest.jpg?h=720&cache=" + (window.performance ? performance.now() : "");
    drawLandmarkMarkers();
    landmarkStatus();
    landmarkSection.scrollIntoView({ behavior: "smooth", block: "nearest" });
  });

  landmarkSnap.addEventListener("pointerdown", function (ev) {
    if (!landmarkMode || landmarkPending) return;
    ev.preventDefault();
    var rect = landmarkSnap.getBoundingClientRect();
    landmarkPending = {
      u: Math.min(1, Math.max(0, (ev.clientX - rect.left) / rect.width)),
      v: Math.min(1, Math.max(0, (ev.clientY - rect.top) / rect.height)),
    };
    drawLandmarkMarkers();
    landmarkStatus();
  });

  function handleLandmarkMapClick(p) {
    if (!landmarkPending) return; // ignore stray map clicks while calibrating
    landmarkPending.mx = +p.x.toFixed(4);
    landmarkPending.my = +p.y.toFixed(4);
    landmarkMatches.push(landmarkPending);
    landmarkPending = null;
    drawLandmarkMarkers();
    landmarkStatus();
  }

  landmarkUndoBtn.addEventListener("click", function () {
    if (landmarkPending) landmarkPending = null;
    else landmarkMatches.pop();
    drawLandmarkMarkers();
    landmarkStatus();
  });

  landmarkCancelBtn.addEventListener("click", closeLandmarkMode);

  landmarkSolveBtn.addEventListener("click", async function () {
    landmarkSolveBtn.disabled = true;
    landmarkResult.textContent = "solving...";
    try {
      var cam = landmarkMode;
      var report = await fetchJson("/v1/push/map/landmark-solve", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ camera: cam, matches: landmarkMatches }),
      });
      landmarkResult.textContent =
        cam + ": HFOV " + report.hfov_before + "° → " + report.hfov_after +
        "°, azimuth " + report.azimuth_before + "° → " + report.azimuth_after +
        "°, tilt " + report.tilt_before + "° → " + report.tilt_after +
        "° — fit error " + report.rms_ft + " ft (per point: " +
        report.residual_ft.join(", ") + "). ";
      var worst = Math.max.apply(null, report.residual_ft);
      if (worst > 8) {
        var warn = document.createElement("div");
        warn.textContent = "⚠ point " +
          (report.residual_ft.indexOf(worst) + 1) + " fits poorly (" + worst +
          " ft) — mismatched click? Undo it and re-add.";
        landmarkResult.appendChild(warn);
      }
      var apply = document.createElement("button");
      apply.textContent = "Apply";
      apply.className = "btn-primary";
      apply.style.marginLeft = "0.6em";
      apply.addEventListener("click", function () {
        var o = opticsEntry(cam);
        o.hfov = report.hfov_after;
        o.tilt_deg = report.tilt_after;
        if (report.vfov_after !== null && o.vfov) o.vfov = report.vfov_after;
        var entry = (doc.camera_layout || {})[cam];
        if (entry) {
          entry.azimuth = report.azimuth_after;
          entry.fov = report.hfov_after; // keep the pie honest too
        }
        markDirty();
        closeLandmarkMode();
        renderMap();
        showBanner(cam + " calibrated from landmarks — remember to Save.", false);
      });
      landmarkResult.appendChild(apply);
    } catch (err) {
      landmarkResult.textContent = "solve error: " + err.message;
    }
    landmarkSolveBtn.disabled = landmarkMatches.length < 2;
  });

  // ---- Reload Frigate config -----------------------------------------

  var configRefreshBtn = document.getElementById("config-refresh-btn");
  var configRefreshState = document.getElementById("config-refresh-state");
  configRefreshBtn.addEventListener("click", async function () {
    configRefreshBtn.disabled = true;
    configRefreshState.textContent = "syncing from Frigate...";
    try {
      var data = await fetchJson("/v1/push/frigate-config/refresh", { method: "POST" });
      if (data.changed) {
        configRefreshState.textContent = "config updated — reloading page...";
        window.location.reload();
        return;
      }
      configRefreshState.textContent = "already up to date (" +
        (data.cameras || []).length + " cameras).";
    } catch (err) {
      configRefreshState.textContent = "error: " + err.message;
    }
    configRefreshBtn.disabled = false;
  });

  // ---- Save ----------------------------------------------------------

  saveBtn.addEventListener("click", async function () {
    saveBtn.disabled = true;
    saveState.textContent = "saving...";
    try {
      // The camera maps are sticky server-side when absent — always send
      // them explicitly so this page can also clear entries.
      doc.camera_headings = doc.camera_headings || {};
      doc.camera_layout = doc.camera_layout || {};
      doc.camera_neighbors = doc.camera_neighbors || {};
      doc.camera_optics = doc.camera_optics || {};
      await fetchJson("/v1/push/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(doc),
      });
      saveState.textContent = "saved ✓";
      footprintData = null; // rig facts may have changed; refetch on demand
      if (footprintsToggle.checked) {
        try {
          footprintData = (await fetchJson("/v1/push/map/footprints")).footprints || [];
        } catch (fpErr) { footprintData = null; }
        renderMap();
      }
    } catch (err) {
      saveState.textContent = "error: " + err.message;
    }
    saveBtn.disabled = false;
  });

  (async function init() {
    try {
      var data = await fetchJson("/v1/push/settings");
      doc = data.settings;
      cameras = data.available_cameras || [];
      doc.camera_optics = doc.camera_optics || data.placement_deployments || {};
      placements = doc.camera_optics;
      mapScaleInput.value = doc.map_scale_ft || "";
      applyFloorplan();
      renderOnboarding();
      renderMap();
      if (!cameras.length) showBanner("No cameras found in the Frigate config.", false);
    } catch (err) {
      showBanner(err.message, true);
    }
  })();
})();
