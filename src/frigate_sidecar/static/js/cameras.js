// Camera calibration: per-camera heading vectors + layout map.
(function () {
  var banner = document.getElementById("cameras-banner");
  var cardsEl = document.getElementById("camera-cards");
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

  // ---- Heading cards -------------------------------------------------

  var SVG_NS = "http://www.w3.org/2000/svg";
  var cardRefs = {}; // camera -> {svg, status}

  // Mirror of the sidecar's derived_camera_heading: world direction
  // camera -> secure-area center, decomposed into the camera's view axis
  // (ahead => up in frame) and right axis.
  function derivedHeading(camera) {
    var entry = (doc.camera_layout || {})[camera];
    var area = doc.secure_area;
    if (!entry || entry.azimuth === undefined || !area) return null;
    var cx = (area.x0 + area.x1) / 2, cy = (area.y0 + area.y1) / 2;
    var wx = cx - entry.x, wy = cy - entry.y;
    if (Math.hypot(wx, wy) < 1e-6) return null;
    var rad = (entry.azimuth * Math.PI) / 180;
    var along = wx * Math.sin(rad) + wy * -Math.cos(rad);
    var rightC = wx * Math.cos(rad) + wy * Math.sin(rad);
    var n = Math.hypot(along, rightC);
    if (n < 1e-6) return null;
    return { dx: rightC / n, dy: -along / n };
  }

  function effectiveHeading(camera) {
    var manual = (doc.camera_headings || {})[camera];
    if (manual) return { vec: manual, auto: false };
    var derived = derivedHeading(camera);
    if (derived) return { vec: derived, auto: true };
    return { vec: null, auto: false };
  }

  function drawArrow(svg, vec, isAuto) {
    // vec: {dx, dy} unit vector; draw centered, length 0.18 in unit space.
    while (svg.lastChild && svg.lastChild.tagName !== "defs") {
      svg.removeChild(svg.lastChild);
    }
    if (!vec) return;
    var cx = 0.5, cy = 0.5, len = 0.18;
    var x2 = cx + vec.dx * len, y2 = cy + vec.dy * len;
    var line = document.createElementNS(SVG_NS, "line");
    line.setAttribute("x1", cx); line.setAttribute("y1", cy);
    line.setAttribute("x2", x2); line.setAttribute("y2", y2);
    line.setAttribute("stroke", "var(--accent, #ffb454)");
    line.setAttribute("stroke-width", "0.012");
    if (isAuto) {
      line.setAttribute("stroke-dasharray", "0.03 0.018");
      line.setAttribute("stroke-opacity", "0.8");
    }
    line.setAttribute("marker-end", "url(#arrowhead)");
    svg.appendChild(line);
    var dot = document.createElementNS(SVG_NS, "circle");
    dot.setAttribute("cx", cx); dot.setAttribute("cy", cy);
    dot.setAttribute("r", "0.015");
    dot.setAttribute("fill", "var(--accent, #ffb454)");
    svg.appendChild(dot);
  }

  function refreshCard(camera) {
    var refs = cardRefs[camera];
    if (!refs) return;
    var eff = effectiveHeading(camera);
    drawArrow(refs.svg, eff.vec, eff.auto);
    refs.status.textContent = eff.vec
      ? (eff.auto ? "auto — derived from map pie + secure area" : "manual arrow")
      : "no direction — draw here, or aim its pie with a secure area drawn";
  }

  function refreshAllCards() {
    Object.keys(cardRefs).forEach(refreshCard);
  }

  function renderCard(camera) {
    var card = document.createElement("div");
    card.className = "stat-card";
    card.style.minWidth = "300px";

    var label = document.createElement("div");
    label.className = "stat-label";
    label.textContent = camera;
    card.appendChild(label);

    var wrap = document.createElement("div");
    wrap.style.cssText = "position:relative;margin-top:0.4em;touch-action:none";
    var img = document.createElement("img");
    img.src = "/api/" + encodeURIComponent(camera) + "/latest.jpg?h=270";
    img.alt = camera;
    img.style.cssText = "display:block;width:100%;border-radius:4px";
    wrap.appendChild(img);

    var svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 1 1");
    svg.setAttribute("preserveAspectRatio", "none");
    svg.style.cssText = "position:absolute;inset:0;width:100%;height:100%;cursor:crosshair";
    var defs = document.createElementNS(SVG_NS, "defs");
    var marker = document.createElementNS(SVG_NS, "marker");
    marker.setAttribute("id", "arrowhead");
    marker.setAttribute("viewBox", "0 0 10 10");
    marker.setAttribute("refX", "8"); marker.setAttribute("refY", "5");
    marker.setAttribute("markerWidth", "5"); marker.setAttribute("markerHeight", "5");
    marker.setAttribute("orient", "auto-start-reverse");
    var tip = document.createElementNS(SVG_NS, "path");
    tip.setAttribute("d", "M 0 0 L 10 5 L 0 10 z");
    tip.setAttribute("fill", "var(--accent, #ffb454)");
    marker.appendChild(tip);
    defs.appendChild(marker);
    svg.appendChild(defs);
    wrap.appendChild(svg);
    card.appendChild(wrap);

    var status = document.createElement("div");
    status.className = "help";
    status.style.margin = "0.3em 0 0";
    card.appendChild(status);
    cardRefs[camera] = { svg: svg, status: status };
    refreshCard(camera);

    var dragStart = null;
    function toUnit(ev) {
      var rect = wrap.getBoundingClientRect();
      return {
        x: Math.min(1, Math.max(0, (ev.clientX - rect.left) / rect.width)),
        y: Math.min(1, Math.max(0, (ev.clientY - rect.top) / rect.height)),
      };
    }
    svg.addEventListener("pointerdown", function (ev) {
      dragStart = toUnit(ev);
      svg.setPointerCapture(ev.pointerId);
    });
    svg.addEventListener("pointermove", function (ev) {
      if (!dragStart) return;
      var p = toUnit(ev);
      var dx = p.x - dragStart.x, dy = p.y - dragStart.y;
      var len = Math.hypot(dx, dy);
      if (len > 0.02) drawArrow(svg, { dx: dx / len, dy: dy / len });
    });
    svg.addEventListener("pointerup", function (ev) {
      if (!dragStart) return;
      var p = toUnit(ev);
      var dx = p.x - dragStart.x, dy = p.y - dragStart.y;
      var len = Math.hypot(dx, dy);
      dragStart = null;
      if (len <= 0.02) return; // a click, not a drag
      if (!doc.camera_headings) doc.camera_headings = {};
      doc.camera_headings[camera] = {
        dx: +(dx / len).toFixed(4), dy: +(dy / len).toFixed(4),
      };
      refreshCard(camera);
      markDirty();
    });

    var clear = document.createElement("button");
    clear.textContent = "Clear";
    clear.title = "Remove the manual arrow (falls back to the map-derived one if available)";
    clear.className = "btn-primary";
    clear.style.marginTop = "0.4em";
    clear.addEventListener("click", function () {
      if (doc.camera_headings) delete doc.camera_headings[camera];
      refreshCard(camera);
      markDirty();
    });
    card.appendChild(clear);
    return card;
  }

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
    var rect = mapEl.getBoundingClientRect();
    var px = (ev.clientX - rect.left) / rect.width;
    var py = (ev.clientY - rect.top) / rect.height;
    var dx = px - entry.x, dy = py - entry.y;
    if (Math.hypot(dx, dy) < 0.01) return null;
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
    svg.setAttribute("viewBox", "0 0 1 1");
    svg.setAttribute("preserveAspectRatio", "none");
    svg.style.cssText = "position:absolute;inset:0;width:100%;height:100%;pointer-events:none";
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

    function dragOn(el, camera, i, apply) {
      if (!el.style.pointerEvents) el.style.pointerEvents = "stroke";
      el.style.touchAction = "none";
      el.addEventListener("pointerdown", function (ev) {
        ev.stopPropagation();
        ev.preventDefault();
        // Select WITHOUT re-rendering here, and track the gesture on
        // window: renderMap() rebuilds the SVG mid-drag, so listeners on
        // the (detached) element itself would go silent after the first
        // frame — the "moves a tiny bit then stops" bug.
        selectedCamera = camera;
        function move(mv) {
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
      svg.appendChild(camGroup);

      if (hasAim) {
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
            "stroke-width": "0.004", "stroke-dasharray": "0.012 0.008",
          });
          var grab = svgLine(camGroup, entry.x, entry.y, ex, ey, {
            stroke: "transparent", "stroke-width": "0.035",
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
      pt.setAttribute("r", selected ? "0.014" : "0.010");
      pt.setAttribute("fill", selected ? "var(--accent, #ffb454)" : "var(--muted, #8f9fb8)");
      pt.setAttribute("stroke", "var(--surface, #111)");
      pt.setAttribute("stroke-width", "0.004");
      pt.style.pointerEvents = "none";
      camGroup.appendChild(pt);

      // The dot is the natural thing to grab to MOVE the camera — an
      // invisible fat hit circle over it does exactly that (the pie
      // underneath would otherwise swallow the drag and rotate instead).
      var moveGrab = document.createElementNS(SVG_NS, "circle");
      moveGrab.setAttribute("cx", entry.x); moveGrab.setAttribute("cy", entry.y);
      moveGrab.setAttribute("r", "0.03");
      moveGrab.setAttribute("fill", "transparent");
      moveGrab.style.pointerEvents = "fill";
      moveGrab.style.cursor = "move";
      moveGrab.style.touchAction = "none";
      moveGrab.setAttribute("aria-label", "move " + camera);
      moveGrab.addEventListener("pointerdown", function (ev) {
        ev.stopPropagation();
        ev.preventDefault();
        selectedCamera = camera;
        var movedPt = false;
        function mm(mv) {
          movedPt = true;
          var rect = mapEl.getBoundingClientRect();
          var e = ensureEntry(camera, i);
          e.x = +Math.min(1, Math.max(0, (mv.clientX - rect.left) / rect.width)).toFixed(4);
          e.y = +Math.min(1, Math.max(0, (mv.clientY - rect.top) / rect.height)).toFixed(4);
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
        var rw = 0.055;
        var ring = document.createElementNS(SVG_NS, "circle");
        ring.setAttribute("cx", entry.x); ring.setAttribute("cy", entry.y);
        ring.setAttribute("r", rw);
        ring.setAttribute("fill", "none");
        ring.setAttribute("stroke", "var(--accent, #ffb454)");
        ring.setAttribute("stroke-opacity", "0.7");
        ring.setAttribute("stroke-width", "0.005");
        ring.setAttribute("stroke-dasharray", "0.01 0.008");
        ring.style.pointerEvents = "none";
        camGroup.appendChild(ring);
        var ringGrab = document.createElementNS(SVG_NS, "circle");
        ringGrab.setAttribute("cx", entry.x); ringGrab.setAttribute("cy", entry.y);
        ringGrab.setAttribute("r", rw);
        ringGrab.setAttribute("fill", "none");
        ringGrab.setAttribute("stroke", "transparent");
        ringGrab.setAttribute("stroke-width", "0.025");
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
        knob.setAttribute("r", "0.013");
        knob.setAttribute("fill", "var(--accent, #ffb454)");
        knob.setAttribute("stroke", "var(--surface, #111)");
        knob.setAttribute("stroke-width", "0.004");
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
        var lx = entry.x + dir.x * 0.11;
        var ly = entry.y + dir.y * 0.11;
        var text = document.createElementNS(SVG_NS, "text");
        text.setAttribute("x", lx);
        text.setAttribute("y", ly);
        text.setAttribute("text-anchor", "middle");
        text.setAttribute("dominant-baseline", "middle");
        text.setAttribute("font-size", "0.028");
        text.setAttribute("fill", "var(--accent, #ffb454)");
        text.setAttribute("stroke", "var(--surface, #111)");
        text.setAttribute("stroke-width", "0.006");
        text.setAttribute("paint-order", "stroke");
        text.textContent = cardinalOf(az) + " " + Math.round(az) + "°";
        camGroup.appendChild(text);
      }
    });
    drawTrails(svg);
    drawCalibrationLine(svg);
    // The live layer is a PERSISTENT node re-appended into each rebuilt
    // SVG — the 1 Hz poll updates its children without touching renderMap,
    // so live dots never fight drag gestures (see the dragOn comment).
    svg.appendChild(liveLayer);
    // Coverage view: darken the ground so unlit (unwatched) area reads as
    // the blind spots.
    // backgroundColor, not the background shorthand — the shorthand would
    // wipe the floorplan's backgroundImage.
    mapEl.style.backgroundColor = (coverageToggle && coverageToggle.checked)
      ? "var(--deep, #0a0e14)" : "var(--surface)";
    mapEl.appendChild(svg);
    refreshAllCards(); // pie/secure edits change the derived arrows live

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
      rect.setAttribute("stroke-width", "0.004");
      rect.setAttribute("stroke-dasharray", "0.015 0.01");
      svg.appendChild(rect);
      var saLabel = document.createElementNS(SVG_NS, "text");
      saLabel.setAttribute("x", Math.min(sa.x0, sa.x1) + 0.012);
      saLabel.setAttribute("y", Math.min(sa.y0, sa.y1) + 0.035);
      saLabel.setAttribute("font-size", "0.026");
      saLabel.setAttribute("fill", "var(--ok, #4caf82)");
      saLabel.textContent = "secure area";
      svg.appendChild(saLabel);
    }
    clearSecureBtn.style.display = doc.secure_area ? "inline-block" : "none";

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
      dot.style.left = pos.x * 100 + "%";
      dot.style.top = pos.y * 100 + "%";
      dot.addEventListener("pointerdown", function (ev) {
        // Gesture tracked on window — the pill survives (no mid-drag
        // rebuild here), but window listeners are the uniform, un-killable
        // pattern all three drags share.
        ev.preventDefault();
        selectedCamera = camera;
        var moved = false;
        function move(mv) {
          moved = true;
          var rect = mapEl.getBoundingClientRect();
          var x = Math.min(1, Math.max(0, (mv.clientX - rect.left) / rect.width));
          var y = Math.min(1, Math.max(0, (mv.clientY - rect.top) / rect.height));
          var entry = ensureEntry(camera, i);
          entry.x = +x.toFixed(4);
          entry.y = +y.toFixed(4);
          dot.style.left = x * 100 + "%";
          dot.style.top = y * 100 + "%";
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
    if (!trailsSelect.value) { renderMap(); return; }
    if (!doc.map_scale_ft) {
      trailsNote.textContent = "set map width first";
      trailsSelect.value = "";
      return;
    }
    trailsNote.textContent = "loading...";
    try {
      var data = await fetchJson("/replay/capture-window?minutes=" + trailsSelect.value);
      trailTracks = data.tracks || [];
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
      poly.setAttribute("stroke-width", track.label === "person" ? "0.006" : "0.003");
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
      poly.setAttribute("stroke-width", "0.003");
      poly.setAttribute("stroke-linejoin", "round");
      var title = document.createElementNS(SVG_NS, "title");
      title.textContent = z.name + " (" + z.camera + ")" +
        (z.clipped ? " — clipped at range limit" : "");
      poly.appendChild(title);
      svg.appendChild(poly);
    });
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
        tp.setAttribute("stroke-width", "0.004");
        tp.setAttribute("stroke-linejoin", "round");
        liveLayer.appendChild(tp);
      }
      var dot = document.createElementNS(SVG_NS, "circle");
      dot.setAttribute("cx", o.x); dot.setAttribute("cy", o.y);
      dot.setAttribute("r", "0.009");
      dot.setAttribute("fill", color);
      dot.setAttribute("stroke", "var(--surface, #111)");
      dot.setAttribute("stroke-width", "0.003");
      liveLayer.appendChild(dot);
      var text = document.createElementNS(SVG_NS, "text");
      text.setAttribute("x", o.x); text.setAttribute("y", o.y + 0.032);
      text.setAttribute("text-anchor", "middle");
      text.setAttribute("font-size", "0.022");
      text.setAttribute("fill", color);
      text.setAttribute("stroke", "var(--surface, #111)");
      text.setAttribute("stroke-width", "0.005");
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

  function applyFloorplan() {
    var fp = doc.floorplan;
    if (fp && fp.ext) {
      mapEl.style.backgroundImage = "url(/v1/push/floorplan?ts=" +
        encodeURIComponent(fp.uploaded_at || "") + ")";
      mapEl.style.backgroundSize = "100% 100%";
      mapEl.style.aspectRatio = fp.w + " / " + fp.h;
      floorplanRemove.style.display = "inline-block";
      calibrateBtn.style.display = "inline-block";
    } else {
      mapEl.style.backgroundImage = "";
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
    label.setAttribute("font-size", "0.024");
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
    var rect = mapEl.getBoundingClientRect();
    return {
      x: Math.min(1, Math.max(0, (ev.clientX - rect.left) / rect.width)),
      y: Math.min(1, Math.max(0, (ev.clientY - rect.top) / rect.height)),
    };
  }

  // Drawing the secure area: a drag that STARTS on empty map background
  // (not a dot, wedge, or arrow) sketches the rectangle corner-to-corner.
  // Place mode (onboarding) and scale calibration claim the click first.
  mapEl.addEventListener("pointerdown", function (ev) {
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
    if (calibrating) {
      handleCalibrateClick(mapUnit(ev));
      return;
    }
    if (ev.target !== mapEl) return;
    var rect = mapEl.getBoundingClientRect();
    function unit(e) {
      return {
        x: Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width)),
        y: Math.min(1, Math.max(0, (e.clientY - rect.top) / rect.height)),
      };
    }
    var start = unit(ev);
    var moved = false;
    mapEl.setPointerCapture(ev.pointerId);
    function move(mv) {
      var p = unit(mv);
      if (Math.hypot(p.x - start.x, p.y - start.y) < 0.02) return;
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
      if (moved) markDirty();
      else if (selectedCamera) { selectedCamera = null; renderMap(); }
    }
    mapEl.addEventListener("pointermove", move);
    mapEl.addEventListener("pointerup", up);
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
  }

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
      cardsEl.textContent = "";
      cameras.forEach(function (camera) {
        cardsEl.appendChild(renderCard(camera));
      });
      applyFloorplan();
      renderOnboarding();
      renderMap();
      if (!cameras.length) showBanner("No cameras found in the Frigate config.", false);
    } catch (err) {
      showBanner(err.message, true);
    }
  })();
})();
