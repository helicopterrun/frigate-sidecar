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

  function drawArrow(svg, vec) {
    // vec: {dx, dy} unit vector; draw centered, length 0.3 in unit space.
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
    line.setAttribute("marker-end", "url(#arrowhead)");
    svg.appendChild(line);
    var dot = document.createElementNS(SVG_NS, "circle");
    dot.setAttribute("cx", cx); dot.setAttribute("cy", cy);
    dot.setAttribute("r", "0.015");
    dot.setAttribute("fill", "var(--accent, #ffb454)");
    svg.appendChild(dot);
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

    drawArrow(svg, (doc.camera_headings || {})[camera] || null);

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
      drawArrow(svg, doc.camera_headings[camera]);
      markDirty();
    });

    var clear = document.createElement("button");
    clear.textContent = "Clear";
    clear.className = "test-push";
    clear.style.marginTop = "0.4em";
    clear.addEventListener("click", function () {
      if (doc.camera_headings) delete doc.camera_headings[camera];
      drawArrow(svg, null);
      markDirty();
    });
    card.appendChild(clear);
    return card;
  }

  // ---- Layout map (top-down, north = up) -----------------------------

  var detailPanel = document.getElementById("camera-detail");
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

  function renderMap() {
    mapEl.textContent = "";

    // Wedge layer + compass rose.
    var svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 1 1");
    svg.setAttribute("preserveAspectRatio", "none");
    svg.style.cssText = "position:absolute;inset:0;width:100%;height:100%;pointer-events:none";
    cameras.forEach(function (camera, i) {
      var entry = (doc.camera_layout || {})[camera];
      if (!entry || entry.azimuth === undefined) return;
      var path = document.createElementNS(SVG_NS, "path");
      path.setAttribute("d", wedgePath(entry, entry.azimuth, entry.fov || DEFAULT_FOV, reach()));
      path.setAttribute("fill", "var(--accent, #ffb454)");
      path.setAttribute("fill-opacity", camera === selectedCamera ? "0.28" : "0.14");
      path.setAttribute("stroke", "var(--accent, #ffb454)");
      path.setAttribute("stroke-opacity", "0.5");
      path.setAttribute("stroke-width", "0.003");
      svg.appendChild(path);
    });
    mapEl.appendChild(svg);

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

      // Aim handle: sits on the wedge bisector; drag to point the pie.
      var entry = (doc.camera_layout || {})[camera];
      var az = entry && entry.azimuth !== undefined ? entry.azimuth : 0;
      var dir = azDir(az);
      var hx = pos.x + dir.x * 0.07, hy = pos.y + dir.y * 0.07;
      var handle = document.createElement("div");
      handle.title = "drag to aim " + camera;
      handle.style.cssText =
        "position:absolute;transform:translate(-50%,-50%);width:12px;height:12px;" +
        "border-radius:50%;background:var(--accent, #ffb454);cursor:alias;" +
        "touch-action:none;opacity:" + (entry && entry.azimuth !== undefined ? "1" : "0.45");
      handle.style.left = hx * 100 + "%";
      handle.style.top = hy * 100 + "%";
      handle.addEventListener("pointerdown", function (ev) {
        ev.stopPropagation();
        selectCamera(camera);
        handle.setPointerCapture(ev.pointerId);
        function move(mv) {
          var rect = mapEl.getBoundingClientRect();
          var px = (mv.clientX - rect.left) / rect.width;
          var py = (mv.clientY - rect.top) / rect.height;
          var e = ensureEntry(camera, i);
          var dx = px - e.x, dy = py - e.y;
          if (Math.hypot(dx, dy) < 0.01) return;
          // atan2 with north=up, clockwise.
          e.azimuth = +(((Math.atan2(dx, -dy) * 180) / Math.PI + 360) % 360).toFixed(1);
          if (e.fov === undefined) e.fov = DEFAULT_FOV;
          renderMap();
        }
        function up() {
          handle.removeEventListener("pointermove", move);
          handle.removeEventListener("pointerup", up);
          markDirty();
        }
        handle.addEventListener("pointermove", move);
        handle.addEventListener("pointerup", up);
      });
      mapEl.appendChild(handle);
    });
    syncDetailPanel();
  }

  function selectCamera(camera) {
    if (selectedCamera !== camera) {
      selectedCamera = camera;
      renderMap();
    }
  }

  function syncDetailPanel() {
    if (!selectedCamera) { detailPanel.style.display = "none"; return; }
    var entry = (doc.camera_layout || {})[selectedCamera];
    detailPanel.style.display = "block";
    detailName.textContent = selectedCamera;
    detailAzimuth.value = entry && entry.azimuth !== undefined ? entry.azimuth : "";
    detailFov.value = entry && entry.fov !== undefined ? entry.fov : DEFAULT_FOV;
  }

  detailAzimuth.addEventListener("change", function () {
    if (!selectedCamera) return;
    var e = ensureEntry(selectedCamera, cameras.indexOf(selectedCamera));
    e.azimuth = ((parseFloat(detailAzimuth.value) || 0) % 360 + 360) % 360;
    if (e.fov === undefined) e.fov = DEFAULT_FOV;
    markDirty();
    renderMap();
  });
  detailFov.addEventListener("change", function () {
    if (!selectedCamera) return;
    var e = ensureEntry(selectedCamera, cameras.indexOf(selectedCamera));
    e.fov = Math.min(360, Math.max(10, parseFloat(detailFov.value) || DEFAULT_FOV));
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
