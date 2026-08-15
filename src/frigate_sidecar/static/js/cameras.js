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

  // ---- Layout map ----------------------------------------------------

  function renderMap() {
    mapEl.textContent = "";
    var layout = doc.camera_layout || {};
    cameras.forEach(function (camera, i) {
      var pos = layout[camera] || {
        x: 0.08 + 0.84 * (i / Math.max(1, cameras.length - 1)),
        y: 0.92,
      };
      var dot = document.createElement("div");
      dot.textContent = camera;
      dot.style.cssText =
        "position:absolute;transform:translate(-50%,-50%);padding:2px 8px;" +
        "background:var(--surface-2);border:1px solid var(--stroke);" +
        "border-radius:999px;font-size:0.75em;cursor:grab;user-select:none;" +
        "touch-action:none;white-space:nowrap";
      dot.style.left = pos.x * 100 + "%";
      dot.style.top = pos.y * 100 + "%";
      dot.addEventListener("pointerdown", function (ev) {
        dot.setPointerCapture(ev.pointerId);
        function move(mv) {
          var rect = mapEl.getBoundingClientRect();
          var x = Math.min(1, Math.max(0, (mv.clientX - rect.left) / rect.width));
          var y = Math.min(1, Math.max(0, (mv.clientY - rect.top) / rect.height));
          dot.style.left = x * 100 + "%";
          dot.style.top = y * 100 + "%";
          if (!doc.camera_layout) doc.camera_layout = {};
          doc.camera_layout[camera] = { x: +x.toFixed(4), y: +y.toFixed(4) };
        }
        function up(uv) {
          dot.removeEventListener("pointermove", move);
          dot.removeEventListener("pointerup", up);
          markDirty();
        }
        dot.addEventListener("pointermove", move);
        dot.addEventListener("pointerup", up);
      });
      mapEl.appendChild(dot);
    });
  }

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

  suggestBtn.addEventListener("click", function () {
    var layout = doc.camera_layout || {};
    var placed = cameras.filter(function (c) { return layout[c]; });
    var radius = parseFloat(radiusInput.value);
    var suggested = {};
    placed.forEach(function (a, i) {
      placed.slice(i + 1).forEach(function (b) {
        var d = Math.hypot(layout[a].x - layout[b].x, layout[a].y - layout[b].y);
        if (d <= radius) suggested[[a, b].sort().join("↔")] = true;
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
