/* Placement planner — pure-physics camera siting calculator.
 * Mirrors frigate_sidecar/analysis/optics.py (kept in sync by tests/test_optics.py).
 * focal/zoom -> HFOV -> object pixels at distance, with a px-vs-distance chart
 * and a top-down FOV wedge (range rings = object good/marginal reach). */
(function () {
  "use strict";

  var P = JSON.parse(document.getElementById("plan-presets").textContent);
  var byId = {};
  P.lenses.forEach(function (l) { byId["lens:" + l.id] = l; });
  P.resolutions.forEach(function (r) { byId["res:" + r.id] = r; });
  P.objects.forEach(function (o) { byId["obj:" + o.id] = o; });

  var $ = function (id) { return document.getElementById(id); };

  // ---- state ----
  var state = {
    lensId: "dahua-5442-vf",
    focal: 2.7,
    hfovOverride: null, // measured HFOV from a deployed quick-pick (until focal/lens changes)
    customHfov: 90,
    resId: "sub-720",
    objId: "face",
    dist: 20,
    obj: { width_ft: 0.5, aspect: 1.4, target_px: 80 }, // editable copy
  };

  // ---- geometry (mirror of optics.py) ----
  function hfovFromFocal(f, w, f0) {
    return (Math.atan(w / (2 * (f + (f0 || 0)))) * 2) * 180 / Math.PI;
  }
  function focalFromHfov(hfov, w, f0) {
    return w / (2 * Math.tan(hfov * Math.PI / 360)) - (f0 || 0);
  }
  function pxPerFt(detW, hfov, dist) {
    return detW / (2 * dist * Math.tan(hfov * Math.PI / 360));
  }
  function objWidthPx(widthFt, detW, hfov, dist) { return widthFt * pxPerFt(detW, hfov, dist); }
  function maxDistFt(widthFt, detW, hfov, targetPx) {
    return widthFt * detW / (2 * targetPx * Math.tan(hfov * Math.PI / 360));
  }

  function lens() { return byId["lens:" + state.lensId]; }
  function res() { return byId["res:" + state.resId]; }

  function effectiveHfov() {
    var l = lens();
    if (l.type === "custom") return state.customHfov;
    if (state.hfovOverride != null) return state.hfovOverride;
    if (l.type === "varifocal") return hfovFromFocal(state.focal, l.sensor_width_mm, l.focal_offset_mm);
    return l.hfov;
  }

  // ---- populate selects ----
  function fillSelect(el, items, value, render) {
    el.innerHTML = "";
    items.forEach(function (it) {
      var o = document.createElement("option");
      o.value = it.id;
      o.textContent = render(it);
      el.appendChild(o);
    });
    if (value != null) el.value = value;
  }

  fillSelect($("sel-lens"), P.lenses, state.lensId, function (l) { return l.label; });
  fillSelect($("sel-res"), P.resolutions, state.resId, function (r) { return r.label; });
  fillSelect($("sel-object"), P.objects, state.objId, function (o) { return o.label; });
  P.cameras.forEach(function (c) {
    var o = document.createElement("option");
    o.value = c.id;
    o.textContent = c.id + " (" + c.hfov + "°, " + c.faces + ")";
    $("sel-camera").appendChild(o);
  });

  // ---- lens-type-dependent control visibility ----
  function syncLensControls() {
    var l = lens();
    $("wrap-focal").style.display = l.type === "varifocal" ? "" : "none";
    $("wrap-hfov-custom").style.display = l.type === "custom" ? "" : "none";
    if (l.type === "varifocal") {
      $("in-focal").min = l.focal_min;
      $("in-focal").max = l.focal_max;
    }
  }

  function loadObject() {
    var o = byId["obj:" + state.objId];
    state.obj = { width_ft: o.width_ft, aspect: o.aspect, target_px: o.target_px };
    $("in-owidth").value = o.width_ft;
    $("in-oaspect").value = o.aspect;
    $("in-otarget").value = o.target_px;
  }

  // ---- main recompute ----
  function recompute() {
    var hfov = effectiveHfov();
    var detW = res().w;
    var o = state.obj;
    var good = maxDistFt(o.width_ft, detW, hfov, o.target_px);
    var marg = maxDistFt(o.width_ft, detW, hfov, o.target_px / 2);

    $("out-hfov").textContent = hfov.toFixed(1);
    $("out-maxdist").textContent = good.toFixed(1);
    $("out-maxdist-marg").textContent = marg.toFixed(1);
    $("out-pxft").textContent = pxPerFt(detW, hfov, state.dist).toFixed(1);
    if (lens().type === "varifocal") $("focal-val").textContent = (+state.focal).toFixed(1);

    // distance panel
    var wpx = objWidthPx(o.width_ft, detW, hfov, state.dist);
    var hpx = wpx * o.aspect;
    $("out-w").textContent = wpx.toFixed(0);
    $("out-h").textContent = hpx.toFixed(0);
    $("out-area").textContent = Math.round(wpx * hpx).toLocaleString();
    var v = $("out-verdict");
    if (wpx >= o.target_px) { v.textContent = "good"; v.className = "stat-value cell-class ok"; }
    else if (wpx >= o.target_px / 2) { v.textContent = "marginal"; v.className = "stat-value cell-class warn"; }
    else { v.textContent = "too small"; v.className = "stat-value cell-class noise"; }

    drawChart(hfov, detW, o, good, marg);
    drawWedge(hfov, good, marg);
  }

  // ---- px-vs-distance chart ----
  function drawChart(hfov, detW, o, good, marg) {
    var W = 520, H = 300, ml = 46, mr = 14, mt = 12, mb = 34;
    var x0 = ml, x1 = W - mr, y0 = H - mb, y1 = mt;
    var xmax = Math.max(marg * 1.25, state.dist * 1.1, 15);
    var ymax = Math.max(o.target_px * 2.5, objWidthPx(o.width_ft, detW, hfov, Math.max(state.dist, 1)) * 1.1, 10);
    var sx = function (d) { return x0 + (d / xmax) * (x1 - x0); };
    var sy = function (p) { return y0 - (Math.min(p, ymax) / ymax) * (y0 - y1); };
    var parts = [];
    parts.push('<rect x="0" y="0" width="' + W + '" height="' + H + '" fill="#0f1115"/>');
    // axes
    parts.push(line(x0, y0, x1, y0, "#2a2f3a"));
    parts.push(line(x0, y0, x0, y1, "#2a2f3a"));
    // gridlines + x labels
    for (var gx = 0; gx <= xmax; gx += niceStep(xmax)) {
      parts.push(line(sx(gx), y0, sx(gx), y1, "#1a1d26"));
      parts.push(txt(sx(gx), y0 + 14, gx.toFixed(0), "#6b7280", "middle"));
    }
    // threshold lines
    parts.push(line(x0, sy(o.target_px), x1, sy(o.target_px), "#14532d", "4 3"));
    parts.push(txt(x1, sy(o.target_px) - 4, "target " + o.target_px + "px", "#4ade80", "end"));
    parts.push(line(x0, sy(o.target_px / 2), x1, sy(o.target_px / 2), "#78350f", "4 3"));
    parts.push(txt(x1, sy(o.target_px / 2) - 4, "½ target", "#fbbf24", "end"));
    // curve
    var pts = [];
    for (var i = 0; i <= 120; i++) {
      var d = xmax * i / 120;
      if (d < 0.5) continue;
      pts.push(sx(d).toFixed(1) + "," + sy(objWidthPx(o.width_ft, detW, hfov, d)).toFixed(1));
    }
    parts.push('<polyline points="' + pts.join(" ") + '" fill="none" stroke="#3b82f6" stroke-width="2"/>');
    // crossover markers
    if (good <= xmax) parts.push(dot(sx(good), sy(o.target_px), "#4ade80"));
    if (marg <= xmax) parts.push(dot(sx(marg), sy(o.target_px / 2), "#fbbf24"));
    // current distance marker
    parts.push(line(sx(state.dist), y0, sx(state.dist), y1, "#e6e6e6", "2 3"));
    parts.push(txt(sx(state.dist), y1 + 10, state.dist + "ft", "#e6e6e6", "middle"));
    // y label
    parts.push(txt(14, (y0 + y1) / 2, "px wide", "#6b7280", "middle", "rotate(-90 14 " + ((y0 + y1) / 2) + ")"));
    parts.push(txt((x0 + x1) / 2, H - 4, "distance (ft)", "#6b7280", "middle"));
    $("plan-chart").innerHTML = parts.join("");
  }

  // ---- top-down FOV wedge (adapted from the coverage-map renderer) ----
  function drawWedge(hfov, good, marg) {
    var CX = 180, CY = 330, RMAX = 285;
    var rawScale = Math.max(marg * 1.12, state.dist * 1.1, 8);
    var step = niceStep(rawScale);
    var scaleFt = step * Math.ceil(rawScale / step); // round up to a clean ring step
    var rpx = function (ft) { return Math.min(ft / scaleFt, 1) * RMAX; };
    function edge(deg, r) {
      var a = deg * Math.PI / 180;
      return { x: CX + Math.sin(a) * r, y: CY - Math.cos(a) * r };
    }
    function arc(deg1, deg2, r, color, dash, width) {
      var p1 = edge(deg1, r), p2 = edge(deg2, r);
      var large = (deg2 - deg1) > 180 ? 1 : 0;
      return '<path d="M ' + f(p1.x) + ' ' + f(p1.y) + ' A ' + f(r) + ' ' + f(r) + ' 0 ' +
        large + ' 1 ' + f(p2.x) + ' ' + f(p2.y) + '" fill="none" stroke="' + color +
        '" stroke-width="' + (width || 1) + '"' + (dash ? ' stroke-dasharray="' + dash + '"' : "") + "/>";
    }
    var half = hfov / 2;
    var lEdge = edge(-half, RMAX), rEdge = edge(half, RMAX);
    var large = hfov > 180 ? 1 : 0;
    var parts = [];
    parts.push('<rect x="0" y="0" width="360" height="360" fill="#0f1115"/>');
    // wedge fill
    parts.push('<path d="M ' + CX + ' ' + CY + ' L ' + f(lEdge.x) + ' ' + f(lEdge.y) +
      ' A ' + RMAX + ' ' + RMAX + ' 0 ' + large + ' 1 ' + f(rEdge.x) + ' ' + f(rEdge.y) +
      ' Z" fill="#3b82f6" fill-opacity="0.16" stroke="#3b82f6" stroke-opacity="0.5" stroke-width="1"/>');
    // neutral distance rings, labelled in feet (read distance straight off the wedge)
    for (var rft = step; rft <= scaleFt + 0.01; rft += step) {
      parts.push(arc(-half, half, rpx(rft), "#2a2f3a", null, 1));
      parts.push(txt(CX - 5, CY - rpx(rft) + 3, rft + " ft", "#6b7280", "end"));
    }
    // object reach arcs (green = good, amber = marginal), labelled to the right
    if (rpx(marg) <= RMAX) {
      parts.push(arc(-half, half, rpx(marg), "#fbbf24", "4 3", 1.6));
      parts.push(txt(CX + 6, CY - rpx(marg) + 3, "marg " + marg.toFixed(0) + " ft", "#fbbf24", "start"));
    }
    parts.push(arc(-half, half, rpx(good), "#4ade80", null, 2));
    parts.push(txt(CX + 6, CY - rpx(good) + 3, "good " + good.toFixed(0) + " ft", "#4ade80", "start"));
    // current distance dot on the centerline
    if (rpx(state.dist) <= RMAX) {
      parts.push(dot(CX, CY - rpx(state.dist), "#e6e6e6"));
    }
    // camera apex
    parts.push('<circle cx="' + CX + '" cy="' + CY + '" r="4" fill="#3b82f6" stroke="#fff" stroke-width="1.2"/>');
    parts.push(txt(CX, CY + 18, hfov.toFixed(0) + "° HFOV", "#8a92a6", "middle"));
    $("plan-wedge").innerHTML = parts.join("");
  }

  // ---- tiny SVG helpers ----
  function f(n) { return (+n).toFixed(1); }
  function line(x1, y1, x2, y2, color, dash) {
    return '<line x1="' + f(x1) + '" y1="' + f(y1) + '" x2="' + f(x2) + '" y2="' + f(y2) +
      '" stroke="' + color + '"' + (dash ? ' stroke-dasharray="' + dash + '"' : "") + "/>";
  }
  function txt(x, y, s, color, anchor, transform) {
    return '<text x="' + f(x) + '" y="' + f(y) + '" fill="' + color + '" font-size="11" font-family="sans-serif"' +
      ' text-anchor="' + (anchor || "start") + '"' + (transform ? ' transform="' + transform + '"' : "") + ">" + s + "</text>";
  }
  function dot(x, y, color) {
    return '<circle cx="' + f(x) + '" cy="' + f(y) + '" r="3.5" fill="' + color + '" stroke="#0f1115" stroke-width="1"/>';
  }
  function niceStep(max) {
    var raw = max / 6;
    var pow = Math.pow(10, Math.floor(Math.log10(raw)));
    var n = raw / pow;
    return (n >= 5 ? 5 : n >= 2 ? 2 : 1) * pow;
  }

  // ---- events ----
  $("sel-lens").addEventListener("change", function () {
    state.lensId = this.value;
    state.hfovOverride = null;
    var l = lens();
    if (l.type === "varifocal") state.focal = l.focal_min;
    syncLensControls();
    recompute();
  });
  $("sel-camera").addEventListener("change", function () {
    var picked = this.value;
    var c = P.cameras.find(function (x) { return x.id === picked; });
    if (!c) return;
    state.lensId = c.lens;
    state.resId = c.res;
    state.hfovOverride = c.hfov; // honor the measured value
    $("sel-lens").value = c.lens;
    $("sel-res").value = c.res;
    var l = lens();
    if (l.type === "varifocal") {
      state.focal = clamp(focalFromHfov(c.hfov, l.sensor_width_mm, l.focal_offset_mm), l.focal_min, l.focal_max);
      $("in-focal").value = state.focal;
    } else if (l.type === "custom") {
      state.customHfov = c.hfov; $("in-hfov").value = c.hfov;
    }
    syncLensControls();
    recompute();
  });
  $("in-focal").addEventListener("input", function () {
    state.focal = +this.value; state.hfovOverride = null; recompute();
  });
  $("in-hfov").addEventListener("input", function () { state.customHfov = +this.value; recompute(); });
  $("sel-res").addEventListener("change", function () { state.resId = this.value; recompute(); });
  $("sel-object").addEventListener("change", function () { state.objId = this.value; loadObject(); recompute(); });
  $("in-dist").addEventListener("input", function () {
    state.dist = +this.value; $("dist-val").textContent = (+this.value).toFixed(1).replace(/\.0$/, ""); recompute();
  });
  ["in-owidth", "in-oaspect", "in-otarget"].forEach(function (id) {
    $(id).addEventListener("input", function () {
      state.obj = {
        width_ft: +$("in-owidth").value || 0.01,
        aspect: +$("in-oaspect").value || 0.01,
        target_px: +$("in-otarget").value || 1,
      };
      recompute();
    });
  });

  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

  // ---- init ----
  syncLensControls();
  loadObject();
  recompute();
})();
