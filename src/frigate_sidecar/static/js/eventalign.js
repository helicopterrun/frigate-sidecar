// Event-clock alignment (Settings › Event alignment): measure the per-camera
// detect-vs-record skew and apply it as a sidecar-side offset. Polls the
// measurement job while it runs — template matching takes minutes.
(function () {
  var btn = document.getElementById("align-measure");
  var stateEl = document.getElementById("align-state");
  var resultsEl = document.getElementById("align-results");
  if (!btn || !resultsEl) return;

  var pollTimer = null;
  var lastState = null;

  // Seed the calibrator with whatever offset is currently in effect.
  function openCalibrator(cam) {
    var st = lastState || {};
    var config = (st.config_ms || {})[cam] || 0;
    var seed = config || (st.applied_ms || {})[cam] || 0;
    SC.calib.open(cam, seed, config, refresh);
  }

  function fmtMs(ms) {
    if (ms === null || ms === undefined) return "—";
    return (ms >= 0 ? "+" : "") + (ms / 1000).toFixed(2) + " s";
  }

  function render(state) {
    var results = state.results || [];
    var applied = state.applied_ms || {};
    var config = state.config_ms || {};
    resultsEl.textContent = "";
    // Cameras with an applied/config offset but no fresh measurement still show.
    var cams = {};
    results.forEach(function (r) { cams[r.camera] = r; });
    Object.keys(applied).forEach(function (c) { if (!cams[c]) cams[c] = { camera: c }; });
    var names = Object.keys(cams).sort();
    if (!names.length) {
      var empty = SC.el("div", { class: "empty", text: state.running
        ? "measuring — comparing event thumbnails against recordings…"
        : "no measurements yet — press Measure cameras" });
      resultsEl.appendChild(empty);
      return;
    }

    var table = SC.el("table", { class: "motion-table" });
    var head = SC.el("tr");
    ["camera", "measured", "spread", "events", "confidence", "in effect", ""].forEach(function (h) {
      head.appendChild(SC.el("th", { text: h }));
    });
    table.appendChild(SC.el("thead", {}, [head]));
    var body = SC.el("tbody");

    names.forEach(function (cam) {
      var r = cams[cam];
      var tr = SC.el("tr");
      tr.appendChild(SC.el("td", { text: cam }));
      tr.appendChild(SC.el("td", { text: fmtMs(r.suggested_offset_ms) }));
      tr.appendChild(SC.el("td", {
        text: r.iqr_ms !== null && r.iqr_ms !== undefined ? "±" + (r.iqr_ms / 1000).toFixed(2) + " s" : "—",
      }));
      tr.appendChild(SC.el("td", {
        text: r.n_contributing_events !== undefined
          ? r.n_contributing_events + "/" + r.n_qualifying_events : "—",
      }));
      var conf = r.confidence || "—";
      var confCls = conf === "high" ? "ok" : conf === "med" ? "warn" : "muted";
      tr.appendChild(SC.el("td", {}, [SC.el("span", { class: "cell-class " + confCls, text: conf })]));

      var effect;
      if (config[cam]) {
        effect = "config " + fmtMs(config[cam]);
      } else if (applied[cam]) {
        effect = "applied " + fmtMs(applied[cam]);
      } else {
        effect = "none";
      }
      tr.appendChild(SC.el("td", { text: effect }));

      var actions = SC.el("td");
      var canApply = r.suggested_offset_ms !== null && r.suggested_offset_ms !== undefined
        && !config[cam] && r.suggested_offset_ms !== (applied[cam] || 0);
      if (canApply) {
        var apply = SC.el("button", { class: "btn-primary", text: "Apply" });
        apply.addEventListener("click", function () {
          applyOffsets({ [cam]: r.suggested_offset_ms }, apply);
        });
        actions.appendChild(apply);
      } else if (!config[cam] && applied[cam]) {
        var clear = SC.el("button", { class: "btn-neutral", text: "Clear" });
        clear.addEventListener("click", function () {
          applyOffsets({ [cam]: 0 }, clear);
        });
        actions.appendChild(clear);
      }
      var calibrate = SC.el("button", { class: "btn-neutral", text: "Calibrate…" });
      calibrate.addEventListener("click", function () { openCalibrator(cam); });
      actions.appendChild(calibrate);
      tr.appendChild(actions);
      body.appendChild(tr);
    });
    table.appendChild(body);
    resultsEl.appendChild(table);

    var pending = names.filter(function (cam) {
      var r = cams[cam];
      return r.suggested_offset_ms !== null && r.suggested_offset_ms !== undefined
        && r.confidence !== "insufficient" && !config[cam]
        && r.suggested_offset_ms !== (applied[cam] || 0);
    });
    if (pending.length > 1) {
      var all = SC.el("button", { class: "btn-primary", text: "Apply all suggested" });
      all.addEventListener("click", function () {
        var offsets = {};
        pending.forEach(function (cam) { offsets[cam] = cams[cam].suggested_offset_ms; });
        applyOffsets(offsets, all);
      });
      resultsEl.appendChild(SC.el("div", { class: "section-block" }, [all]));
    }
  }

  // Top-of-section camera dropdown: reaches cameras with no measurement and
  // no applied offset (the table only shows measured/applied ones).
  var camSelect = document.getElementById("calib-camera");
  var camOpen = document.getElementById("calib-open");
  function renderCameraPicker(state) {
    if (!camSelect || !camOpen) return;
    var names = (state.cameras || []).slice();
    (state.results || []).forEach(function (r) {
      if (names.indexOf(r.camera) < 0) names.push(r.camera);
    });
    Object.keys(state.applied_ms || {}).forEach(function (c) {
      if (names.indexOf(c) < 0) names.push(c);
    });
    names.sort();
    var current = camSelect.value;
    camSelect.textContent = "";
    names.forEach(function (c) {
      camSelect.appendChild(SC.el("option", { value: c, text: c }));
    });
    if (names.indexOf(current) >= 0) camSelect.value = current;
    camSelect.disabled = camOpen.disabled = !names.length;
  }
  if (camOpen) {
    camOpen.addEventListener("click", function () {
      if (camSelect && camSelect.value) openCalibrator(camSelect.value);
    });
  }

  async function refresh() {
    var state;
    try {
      state = await SC.fetchJson("/analysis/annotation-offset/state");
    } catch (e) {
      stateEl.textContent = "state unavailable";
      return;
    }
    lastState = state;
    renderCameraPicker(state);
    btn.disabled = !!state.running;
    if (state.running) {
      stateEl.textContent = "measuring… this takes a few minutes";
      if (!pollTimer) pollTimer = setInterval(refresh, 5000);
    } else {
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
      stateEl.textContent = state.error ? "failed: " + state.error
        : state.measured_at ? "measured " + new Date(state.measured_at * 1000).toLocaleString()
        : "";
    }
    render(state);
  }

  async function applyOffsets(offsets, button) {
    button.disabled = true;
    try {
      var resp = await fetch("/analysis/annotation-offset/apply", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ offsets: offsets }),
      });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      SC.toast("offset applied");
    } catch (e) {
      SC.toast("apply failed: " + e);
    }
    refresh();
  }

  btn.addEventListener("click", async function () {
    btn.disabled = true;
    try {
      var resp = await fetch("/analysis/annotation-offset/measure?days=3", { method: "POST" });
      if (!resp.ok && resp.status !== 409) throw new Error("HTTP " + resp.status);
    } catch (e) {
      SC.toast("could not start: " + e);
    }
    refresh();
  });

  refresh();
})();
