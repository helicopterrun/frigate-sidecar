// Manual event-offset calibration (Settings › Event alignment › Calibrate…).
// Pick a recent event, click the recording frame that matches its opening
// moment, fine-nudge ±250 ms, save. Exposes SC.calib.open(camera, seedMs,
// configMs, onSaved) for eventalign.js. The convention matches the backend:
// recording_time = detection_time + offset_ms/1000, so the frame fetched at
// event.start + offset showing the event's start means the offset is right.
(function () {
  var COARSE_SPAN_MS = 8000; // ±8 s at 1 s steps
  var COARSE_STEP_MS = 1000;
  var FINE_SPAN_MS = 1000; // ±1 s at 250 ms steps after a coarse pick
  var FINE_STEP_MS = 250;

  var overlay = null;
  var state = null; // {camera, configMs, onSaved, events, event, offsetMs, fine, fineCenterMs}

  function frameUrl(camera, ts) {
    return "/analysis/annotation-offset/frame/" + encodeURIComponent(camera)
      + "?ts=" + ts.toFixed(3);
  }

  function fmtMs(ms) {
    return (ms >= 0 ? "+" : "−") + (Math.abs(ms) / 1000).toFixed(2) + " s";
  }

  function fmtClock(ts) {
    return new Date(ts * 1000).toLocaleString(undefined, {
      month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
    });
  }

  function close() {
    if (overlay) overlay.remove();
    overlay = null;
    state = null;
    document.removeEventListener("keydown", onKey);
  }

  function onKey(e) {
    if (e.key === "Escape") close();
  }

  function frameImg(ts, cls) {
    var wrap = SC.el("div", { class: "calib-cell " + (cls || "") });
    var img = SC.el("img", { src: frameUrl(state.camera, ts), loading: "lazy", alt: "" });
    img.addEventListener("error", function () {
      wrap.classList.add("missing");
      wrap.appendChild(SC.el("span", { class: "calib-missing", text: "no recording" }));
      img.remove();
    });
    wrap.appendChild(img);
    return wrap;
  }

  function renderStrip() {
    var strip = overlay.querySelector("#calib-strip");
    strip.textContent = "";
    if (!state.event) return;
    var start = state.event.start_time;
    var center = state.fine ? state.fineCenterMs : state.offsetMs;
    var span = state.fine ? FINE_SPAN_MS : COARSE_SPAN_MS;
    var step = state.fine ? FINE_STEP_MS : COARSE_STEP_MS;
    for (var off = center - span; off <= center + span; off += step) {
      (function (offsetMs) {
        var cell = frameImg(start + offsetMs / 1000);
        if (offsetMs === state.offsetMs) cell.classList.add("selected");
        cell.appendChild(SC.el("span", { class: "calib-off", text: fmtMs(offsetMs) }));
        cell.addEventListener("click", function () {
          state.offsetMs = offsetMs;
          if (!state.fine) {
            state.fine = true;
            state.fineCenterMs = offsetMs;
          }
          renderStrip();
          renderOffset();
        });
        strip.appendChild(cell);
      })(off);
    }
    var zoom = overlay.querySelector("#calib-zoom");
    zoom.textContent = state.fine ? "wider (1 s steps)" : "";
    zoom.style.display = state.fine ? "" : "none";
  }

  function renderOffset() {
    overlay.querySelector("#calib-offset").textContent = fmtMs(state.offsetMs);
    var preview = overlay.querySelector("#calib-preview");
    preview.textContent = "";
    if (state.event) {
      preview.appendChild(
        frameImg(state.event.start_time + state.offsetMs / 1000, "calib-large")
      );
    }
  }

  function selectEvent(ev) {
    state.event = ev;
    state.fine = false;
    var ref = overlay.querySelector("#calib-ref");
    ref.textContent = "";
    ref.appendChild(SC.el("img", {
      src: "/api/events/" + encodeURIComponent(ev.id) + "/thumbnail.jpg",
      alt: "event thumbnail",
    }));
    overlay.querySelectorAll("#calib-events .calib-event").forEach(function (el) {
      el.classList.toggle("selected", el.dataset.id === ev.id);
    });
    renderStrip();
    renderOffset();
  }

  function renderEvents() {
    var row = overlay.querySelector("#calib-events");
    row.textContent = "";
    if (!state.events.length) {
      row.appendChild(SC.el("div", {
        class: "empty",
        text: "no recent events with snapshots on this camera",
      }));
      return;
    }
    state.events.forEach(function (ev) {
      var card = SC.el("div", { class: "calib-event" });
      card.dataset.id = ev.id;
      card.appendChild(SC.el("img", {
        src: "/api/events/" + encodeURIComponent(ev.id) + "/thumbnail.jpg",
        loading: "lazy", alt: "",
      }));
      card.appendChild(SC.el("span", {
        class: "calib-off",
        text: (ev.sub_label || ev.label) + " · " + fmtClock(ev.start_time),
      }));
      card.addEventListener("click", function () { selectEvent(ev); });
      row.appendChild(card);
    });
  }

  function nudge(deltaMs) {
    state.offsetMs += deltaMs;
    if (state.fine && Math.abs(state.offsetMs - state.fineCenterMs) > FINE_SPAN_MS) {
      state.fineCenterMs = state.offsetMs;
    }
    renderStrip();
    renderOffset();
  }

  async function save(button) {
    if (!state.event) return;
    button.disabled = true;
    var offsets = {};
    offsets[state.camera] = Math.round(state.offsetMs);
    try {
      var resp = await fetch("/analysis/annotation-offset/apply", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ offsets: offsets }),
      });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      SC.toast("offset saved: " + fmtMs(state.offsetMs));
      var cb = state.onSaved;
      close();
      if (cb) cb();
    } catch (e) {
      SC.toast("save failed: " + e);
      button.disabled = false;
    }
  }

  async function open(camera, seedMs, configMs, onSaved) {
    close();
    state = {
      camera: camera,
      configMs: configMs || 0,
      onSaved: onSaved || null,
      events: [],
      event: null,
      offsetMs: seedMs || 0,
      fine: false,
      fineCenterMs: 0,
    };

    var saveBtn = SC.el("button", {
      class: "btn-primary",
      text: state.configMs ? "Save anyway" : "Save offset",
    });
    saveBtn.addEventListener("click", function () { save(saveBtn); });
    var closeBtn = SC.el("button", { class: "btn-neutral", text: "✕" });
    closeBtn.addEventListener("click", close);
    var zoomBtn = SC.el("button", { class: "btn-neutral", id: "calib-zoom" });
    zoomBtn.addEventListener("click", function () {
      state.fine = false;
      renderStrip();
    });

    var nudges = SC.el("div", { class: "calib-nudges" });
    [["−1 s", -1000], ["−250 ms", -250]].forEach(function (n) {
      var b = SC.el("button", { class: "btn-neutral", text: n[0] });
      b.addEventListener("click", function () { nudge(n[1]); });
      nudges.appendChild(b);
    });
    nudges.appendChild(SC.el("span", { class: "calib-offset-label" }, [
      SC.el("span", { text: "offset: " }),
      SC.el("b", { id: "calib-offset" }),
    ]));
    [["+250 ms", 250], ["+1 s", 1000]].forEach(function (n) {
      var b = SC.el("button", { class: "btn-neutral", text: n[0] });
      b.addEventListener("click", function () { nudge(n[1]); });
      nudges.appendChild(b);
    });
    nudges.appendChild(zoomBtn);
    nudges.appendChild(saveBtn);

    var panel = SC.el("div", { class: "calib-panel" }, [
      SC.el("div", { class: "calib-head" }, [
        SC.el("h3", { text: "Calibrate " + camera }),
        closeBtn,
      ]),
      SC.el("p", { class: "help", text:
        "Pick a recent event, then click the recording frame that matches the "
        + "event's opening moment. Fine-tune with the nudges and save." }),
      SC.el("div", { class: "calib-events", id: "calib-events" }),
      SC.el("div", { class: "calib-compare" }, [
        SC.el("div", { class: "calib-side" }, [
          SC.el("div", { class: "calib-caption", text: "event start (detect clock)" }),
          SC.el("div", { class: "calib-ref", id: "calib-ref" }),
        ]),
        SC.el("div", { class: "calib-side" }, [
          SC.el("div", { class: "calib-caption", text: "recording at start + offset" }),
          SC.el("div", { class: "calib-ref", id: "calib-preview" }),
        ]),
      ]),
      SC.el("div", { class: "calib-strip", id: "calib-strip" }),
      nudges,
    ]);
    if (state.configMs) {
      panel.insertBefore(SC.el("div", { class: "calib-warn", text:
        "Frigate's config sets detect.annotation_offset " + fmtMs(state.configMs)
        + " for this camera, which overrides anything saved here. To use your "
        + "calibrated value, set it in Frigate's config.yml instead." },
      ), panel.querySelector(".calib-events"));
    }

    overlay = SC.el("div", { class: "calib-overlay" }, [panel]);
    overlay.addEventListener("click", function (e) {
      if (e.target === overlay) close();
    });
    document.body.appendChild(overlay);
    document.addEventListener("keydown", onKey);
    renderOffset();

    var events;
    try {
      events = await SC.fetchJson(
        "/analysis/annotation-offset/events?camera=" + encodeURIComponent(camera)
      );
    } catch (e) {
      SC.toast("could not list events: " + e);
      events = [];
    }
    if (!state) return; // closed while fetching
    state.events = events;
    renderEvents();
    if (events.length) selectEvent(events[0]);
  }

  window.SC = window.SC || {};
  SC.calib = { open: open };
})();
