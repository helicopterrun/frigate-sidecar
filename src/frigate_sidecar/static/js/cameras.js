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
  var placements = {}; // camera -> {hfov, mount_ft, tilt_deg, faces} from the placement page

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
    clear.className = "test-push";
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

    function dragOn(el, camera, i, apply) {
      if (!el.style.pointerEvents) el.style.pointerEvents = "stroke";
      el.style.touchAction = "none";
      el.addEventListener("pointerdown", function (ev) {
        ev.stopPropagation();
        selectCamera(camera);
        el.setPointerCapture(ev.pointerId);
        function move(mv) {
          var e = ensureEntry(camera, i);
          var az = pointerAzimuth(mv, e);
          if (az === null) return;
          apply(e, az);
          renderMap();
        }
        function up() {
          el.removeEventListener("pointermove", move);
          el.removeEventListener("pointerup", up);
          markDirty();
        }
        el.addEventListener("pointermove", move);
        el.addEventListener("pointerup", up);
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

      if (hasAim) {
        var path = document.createElementNS(SVG_NS, "path");
        path.setAttribute("d", wedgePath(entry, az, fov, r));
        path.setAttribute("fill", "var(--accent, #ffb454)");
        path.setAttribute("fill-opacity", selected ? "0.28" : "0.14");
        path.setAttribute("stroke", "none");
        // The whole pie is a rotation surface: drag anywhere inside it to
        // swing the aim — the biggest possible target.
        path.style.pointerEvents = "fill";
        path.style.cursor = "grab";
        dragOn(path, camera, i, function (e, pointerAz) {
          e.azimuth = +pointerAz.toFixed(1);
        });
        svg.appendChild(path);

        // The two wedge edges: visible thin lines + invisible fat grab
        // lines. Dragging an edge sets the width symmetrically.
        [az - fov / 2, az + fov / 2].forEach(function (edgeAz) {
          var d = azDir(edgeAz);
          var ex = entry.x + d.x * r, ey = entry.y + d.y * r;
          svgLine(svg, entry.x, entry.y, ex, ey, {
            stroke: "var(--accent, #ffb454)", "stroke-opacity": "0.55",
            "stroke-width": "0.004", "stroke-dasharray": "0.012 0.008",
          });
          var grab = svgLine(svg, entry.x, entry.y, ex, ey, {
            stroke: "transparent", "stroke-width": "0.035",
          });
          grab.style.cursor = "col-resize";
          dragOn(grab, camera, i, function (e, pointerAz) {
            var half = Math.abs(((pointerAz - e.azimuth + 540) % 360) - 180);
            e.fov = +Math.min(360, Math.max(10, half * 2)).toFixed(1);
          });
        });
      }

      // Direction arrow — the primary aim control, drawn like the snapshot
      // arrows. Uncalibrated cameras get a faint north-pointing ghost you
      // grab to set the first aim.
      var dir = azDir(az);
      var ax = entry.x + dir.x * r * 0.72, ay = entry.y + dir.y * r * 0.72;
      var arrow = svgLine(svg, entry.x, entry.y, ax, ay, {
        stroke: "var(--accent, #ffb454)",
        "stroke-opacity": hasAim ? "0.95" : "0.35",
        "stroke-width": "0.008",
        "marker-end": "url(#map-arrowhead)",
      });
      var grabArrow = svgLine(svg, entry.x, entry.y, ax, ay, {
        stroke: "transparent", "stroke-width": "0.05",
      });
      grabArrow.style.cursor = "alias";
      grabArrow.setAttribute("aria-label", "aim " + camera);
      dragOn(grabArrow, camera, i, function (e, pointerAz) {
        e.azimuth = +pointerAz.toFixed(1);
        if (e.fov === undefined) e.fov = defaultFov(camera);
      });

      if (hasAim) {
        // Cardinal readout floats just past the arrow tip: "SW 225°".
        var lx = entry.x + dir.x * (r * 0.72 + 0.045);
        var ly = entry.y + dir.y * (r * 0.72 + 0.045);
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
        svg.appendChild(text);
      }
    });
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
      var dot = document.createElement("div");
      dot.textContent = camera;
      dot.style.cssText =
        "position:absolute;transform:translate(-50%,-50%);padding:2px 8px;" +
        "background:var(--surface-2);border:1px solid " +
        (camera === selectedCamera ? "var(--accent, #ffb454)" : "var(--stroke)") + ";" +
        "border-radius:999px;font-size:0.75em;cursor:grab;user-select:none;" +
        "touch-action:none;white-space:nowrap";
      dot.style.left = pos.x * 100 + "%";
      dot.style.top = pos.y * 100 + "%";
      dot.addEventListener("pointerdown", function (ev) {
        selectCamera(camera);
        dot.setPointerCapture(ev.pointerId);
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
          dot.removeEventListener("pointermove", move);
          dot.removeEventListener("pointerup", up);
          if (moved) { markDirty(); renderMap(); }
        }
        dot.addEventListener("pointermove", move);
        dot.addEventListener("pointerup", up);
      });
      mapEl.appendChild(dot);
    });
    syncDetailPanel();
  }

  function selectCamera(camera) {
    if (selectedCamera !== camera) {
      selectedCamera = camera;
      renderMap();
    }
  }

  // Drawing the secure area: a drag that STARTS on empty map background
  // (not a dot, wedge, or arrow) sketches the rectangle corner-to-corner.
  mapEl.addEventListener("pointerdown", function (ev) {
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
    document.getElementById("detail-placement").textContent = p
      ? "placement: " + p.hfov + "° HFOV · " + p.mount_ft + "ft mount · "
        + p.tilt_deg + "° down · faces " + p.faces
      : "";
  }

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
      apply.className = "test-push";
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

  // ---- Save ----------------------------------------------------------

  saveBtn.addEventListener("click", async function () {
    saveBtn.disabled = true;
    saveState.textContent = "saving...";
    try {
      // All three camera maps are sticky server-side when absent — always
      // send them explicitly so this page can also clear entries.
      doc.camera_headings = doc.camera_headings || {};
      doc.camera_layout = doc.camera_layout || {};
      doc.camera_neighbors = doc.camera_neighbors || {};
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
      placements = data.placement_deployments || {};
      cardsEl.textContent = "";
      cameras.forEach(function (camera) {
        cardsEl.appendChild(renderCard(camera));
      });
      renderMap();
      if (!cameras.length) showBanner("No cameras found in the Frigate config.", false);
    } catch (err) {
      showBanner(err.message, true);
    }
  })();
})();
