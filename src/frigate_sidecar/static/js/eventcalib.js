// Manual event-offset calibration (Settings › Event alignment › Calibrate…).
// Two equal panes — the event's full-frame snapshot (bbox drawn) vs the
// recording at start+offset — with a hold-to-blink compare, a filmstrip for
// coarse picking, and ±250 ms/±1 s nudges (also arrow keys). Exposes
// SC.calib.open(camera, seedMs, configMs, onSaved) for eventalign.js.
// Convention matches the backend: recording_time = detection_time +
// offset_ms/1000, so the frame at event.start + offset showing the snapshot's
// moment means the offset is right.
(function () {
  var STEPS = [1000, 250]; // strip step choices, ms
  var SPAN_CELLS = 8; // cells either side of the strip's centre

  var overlay = null;
  var state = null; // {camera, configMs, onSaved, events, event, offsetMs, stepMs}

  function frameUrl(camera, ts) {
    return "/analysis/annotation-offset/frame/" + encodeURIComponent(camera)
      + "?ts=" + ts.toFixed(3);
  }

  function snapshotUrl(id) {
    return "/analysis/annotation-offset/snapshot/" + encodeURIComponent(id);
  }

  function thumbUrl(id) {
    return "/analysis/annotation-offset/thumbnail/" + encodeURIComponent(id);
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
    document.removeEventListener("keyup", onKeyUp);
  }

  function onKey(e) {
    if (!state) return;
    if (e.key === "Escape") { close(); return; }
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      e.preventDefault();
      nudge((e.key === "ArrowLeft" ? -1 : 1) * (e.shiftKey ? 1000 : 250));
    }
    if (e.key === "b" || e.key === "B") setBlink(true);
  }

  function onKeyUp(e) {
    if (e.key === "b" || e.key === "B") setBlink(false);
  }

  function setBlink(on) {
    var ghost = overlay && overlay.querySelector("#calib-ghost");
    if (ghost) ghost.style.opacity = on ? "1" : "0";
  }

  // ---- rendering ----------------------------------------------------------

  function renderStrip() {
    var strip = overlay.querySelector("#calib-strip");
    strip.textContent = "";
    if (!state.event) return;
    var start = state.event.start_time;
    var step = state.stepMs;
    // Centre the strip on the current offset, snapped to the step grid.
    var center = Math.round(state.offsetMs / step) * step;
    var selectedCell = null;
    for (var off = center - SPAN_CELLS * step; off <= center + SPAN_CELLS * step; off += step) {
      (function (offsetMs) {
        var cell = SC.el("div", { class: "calib-cell" });
        var img = SC.el("img", {
          src: frameUrl(state.camera, start + offsetMs / 1000), loading: "lazy", alt: "",
        });
        img.addEventListener("error", function () {
          cell.classList.add("missing");
          cell.appendChild(SC.el("span", { class: "calib-missing", text: "no recording" }));
          img.remove();
        });
        cell.appendChild(img);
        cell.appendChild(SC.el("span", { class: "calib-off", text: fmtMs(offsetMs) }));
        if (offsetMs === state.offsetMs) {
          cell.classList.add("selected");
          selectedCell = cell;
        } else if (offsetMs === center && !selectedCell) {
          // The offset sits between this step's cells (e.g. −2.60 s on a 1 s
          // grid): anchor the scroll on the nearest cell, unmarked.
          selectedCell = cell;
        }
        cell.addEventListener("click", function () {
          state.offsetMs = offsetMs;
          if (state.stepMs === 1000) setStep(250); // picking coarse zooms fine
          else { renderStrip(); renderOffset(); }
        });
        strip.appendChild(cell);
      })(off);
    }
    if (selectedCell) {
      selectedCell.scrollIntoView({ inline: "center", block: "nearest" });
    }
    STEPS.forEach(function (step2) {
      var b = overlay.querySelector("#calib-step-" + step2);
      if (b) b.classList.toggle("active", state.stepMs === step2);
    });
  }

  function setStep(stepMs) {
    state.stepMs = stepMs;
    renderStrip();
    renderOffset();
  }

  function renderOffset() {
    overlay.querySelector("#calib-offset").textContent = fmtMs(state.offsetMs);
    var caption = overlay.querySelector("#calib-frame-caption");
    caption.textContent = "recording at " + fmtMs(state.offsetMs);
    var img = overlay.querySelector("#calib-frame");
    var missing = overlay.querySelector("#calib-frame-missing");
    if (state.event) {
      missing.style.display = "none";
      img.style.display = "";
      img.src = frameUrl(state.camera, state.event.start_time + state.offsetMs / 1000);
    }
  }

  function selectEvent(ev) {
    state.event = ev;
    overlay.querySelector("#calib-snap").src = snapshotUrl(ev.id);
    overlay.querySelector("#calib-ghost").src = snapshotUrl(ev.id);
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
      card.appendChild(SC.el("img", { src: thumbUrl(ev.id), loading: "lazy", alt: "" }));
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
    renderStrip();
    renderOffset();
  }

  async function save(button) {
    if (!state.event) return;
    button.disabled = true;
    var ms = Math.round(state.offsetMs);
    try {
      var resp;
      if (state.configMs) {
        // Config-pinned camera: write where the value is authoritative --
        // Frigate's own config -- and let Frigate restart to pick it up.
        resp = await fetch("/analysis/annotation-offset/apply-config", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ camera: state.camera, offset_ms: ms }),
        });
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        SC.toast("saved to Frigate config: " + fmtMs(ms)
          + " — Frigate is restarting (~30 s)");
      } else {
        var offsets = {};
        offsets[state.camera] = ms;
        resp = await fetch("/analysis/annotation-offset/apply", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ offsets: offsets }),
        });
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        SC.toast("offset saved: " + fmtMs(ms));
      }
      var cb = state.onSaved;
      close();
      if (cb) cb();
    } catch (e) {
      SC.toast("save failed: " + e);
      button.disabled = false;
    }
  }

  // ---- construction -------------------------------------------------------

  function holdToBlink() {
    var b = SC.el("button", { class: "btn-neutral", text: "hold to compare (B)" });
    ["pointerdown", "touchstart"].forEach(function (evt) {
      b.addEventListener(evt, function (e) { e.preventDefault(); setBlink(true); });
    });
    ["pointerup", "pointerleave", "touchend", "touchcancel"].forEach(function (evt) {
      b.addEventListener(evt, function () { setBlink(false); });
    });
    return b;
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
      stepMs: 1000,
    };

    var saveBtn = SC.el("button", {
      class: "btn-primary",
      text: state.configMs ? "Save to Frigate config" : "Save offset",
    });
    saveBtn.addEventListener("click", function () { save(saveBtn); });
    var closeBtn = SC.el("button", { class: "btn-neutral", text: "✕" });
    closeBtn.addEventListener("click", close);

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
    nudges.appendChild(saveBtn);

    // The step toggle: the coarse/fine state, visible and directly settable.
    var stepWrap = SC.el("div", { class: "calib-step" }, [
      SC.el("span", { class: "calib-caption", text: "step" }),
    ]);
    STEPS.forEach(function (step) {
      var b = SC.el("button", {
        class: "btn-neutral calib-step-btn", id: "calib-step-" + step,
        text: step >= 1000 ? (step / 1000) + " s" : step + " ms",
      });
      b.addEventListener("click", function () { setStep(step); });
      stepWrap.appendChild(b);
    });

    var pair = SC.el("div", { class: "calib-pair" }, [
      SC.el("div", { class: "calib-pane" }, [
        SC.el("div", { class: "calib-caption", text: "what Frigate saw · event start" }),
        SC.el("div", { class: "calib-pane-img" }, [
          SC.el("img", { id: "calib-snap", alt: "event snapshot" }),
        ]),
      ]),
      SC.el("div", { class: "calib-pane" }, [
        SC.el("div", { class: "calib-caption", id: "calib-frame-caption", text: "recording" }),
        SC.el("div", { class: "calib-pane-img" }, [
          SC.el("img", { id: "calib-frame", alt: "recording frame" }),
          SC.el("span", {
            id: "calib-frame-missing", class: "calib-missing",
            style: "display:none", text: "no recording at this moment",
          }),
          SC.el("img", { id: "calib-ghost", class: "calib-ghost", alt: "" }),
        ]),
      ]),
    ]);

    var panel = SC.el("div", { class: "calib-panel" }, [
      SC.el("div", { class: "calib-head" }, [
        SC.el("h3", { text: "Calibrate " + camera }),
        closeBtn,
      ]),
      SC.el("p", { class: "help", text:
        "Find the recording frame where the scene matches the snapshot — that "
        + "offset is this camera's clock skew. Arrow keys nudge ±250 ms "
        + "(Shift = 1 s); hold the compare button to blink the two." }),
      SC.el("div", { class: "calib-events", id: "calib-events" }),
      pair,
      SC.el("div", { class: "calib-blinkrow" }, [holdToBlink()]),
      stepWrap,
      SC.el("div", { class: "calib-strip", id: "calib-strip" }),
      nudges,
    ]);
    if (state.configMs) {
      panel.insertBefore(SC.el("div", { class: "calib-warn", text:
        "This camera's offset lives in Frigate's config ("
        + fmtMs(state.configMs) + "). Saving writes the new value there and "
        + "restarts Frigate (~30 s) so its own annotation overlay is fixed too." },
      ), panel.querySelector(".calib-events"));
    }

    var frameImg = panel.querySelector("#calib-frame");
    frameImg.addEventListener("error", function () {
      frameImg.style.display = "none";
      panel.querySelector("#calib-frame-missing").style.display = "";
    });

    overlay = SC.el("div", { class: "calib-overlay" }, [panel]);
    overlay.addEventListener("click", function (e) {
      if (e.target === overlay) close();
    });
    document.body.appendChild(overlay);
    document.addEventListener("keydown", onKey);
    document.addEventListener("keyup", onKeyUp);

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
    // Auto-pick: a person is the clearest moving subject; fall back to newest.
    var pick = null;
    for (var i = 0; i < events.length; i++) {
      if (events[i].label === "person") { pick = events[i]; break; }
    }
    if (!pick) pick = events[0];
    if (pick) selectEvent(pick);
  }

  window.SC = window.SC || {};
  SC.calib = { open: open };
})();
