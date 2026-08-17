// Zone handling settings: place classes, per-zone overrides, camera neighbors.
(function () {
  var PLACES = ["street", "yard", "doors", "private", "off_limits"];
  var PLACE_LABELS = {
    // Display names mirror the app's (AttentionSettingsModel.swift) so the
    // two surfaces speak one vocabulary.
    street: "Public",
    yard: "Semi-private",
    doors: "Entry / exit",
    private: "Private",
    off_limits: "Restricted",
  };
  var SUBJECTS = ["person", "vehicle", "animal", "thing"];
  var LEVELS = ["log", "quiet", "notify", "urgent"];
  var LEVEL_LABELS = { log: "Logged", quiet: "Noted", notify: "Announced", urgent: "Urgent" };

  var banner = document.getElementById("zones-banner");
  var zonesList = document.getElementById("zones-list");
  var neighborsList = document.getElementById("neighbors-list");
  var saveBtn = document.getElementById("save-btn");
  var saveState = document.getElementById("save-state");

  var doc = null; // the settings document we mutate and PUT back
  var dirty = false;

  function showBanner(text, isError) {
    banner.textContent = text;
    banner.style.display = "block";
    banner.style.color = isError ? "var(--warn, #e8a735)" : "var(--muted)";
  }

  function markDirty() {
    dirty = true;
    saveState.textContent = "unsaved changes";
  }

  async function fetchJson(url, opts) {
    var resp = await fetch(url, opts);
    // Text first: an HTML error page must surface readably, not as a
    // SyntaxError (same idiom as replay.js).
    var raw = await resp.text();
    var data;
    try {
      data = JSON.parse(raw);
    } catch (e) {
      throw new Error("HTTP " + resp.status + " — " + raw.slice(0, 200));
    }
    if (resp.status === 401) {
      throw new Error("Not authorized — open Frigate and log in first, then reload this page.");
    }
    if (!resp.ok) {
      var detail = data && data.detail;
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail || data));
    }
    return data;
  }

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === "text") node.textContent = attrs[k];
      else if (k === "class") node.className = attrs[k];
      else node.setAttribute(k, attrs[k]);
    });
    (children || []).forEach(function (c) { node.appendChild(c); });
    return node;
  }

  function renderZone(zone) {
    var card = el("div", { class: "stat-card", style: "min-width:280px" });
    var title = el("div", { class: "stat-label" });
    var configured = (doc.zone_names || {})[zone.zone] || "";
    title.appendChild(el("strong", { text: configured || zone.friendly_name || zone.zone }));
    if (configured || zone.friendly_name) {
      title.appendChild(el("span", {
        class: "help", text: "  (" + zone.zone + ")", style: "margin-left:0.4em",
      }));
    }
    card.appendChild(title);
    card.appendChild(el("div", {
      class: "help", text: "cameras: " + zone.cameras.join(", "),
      style: "margin:0.25em 0 0.5em",
    }));

    // Display name for notification copy ("Person in {name}"). Sidecar-side
    // (settings.zone_names); wins over Frigate's friendly_name.
    var nameRow = el("div", { style: "margin:0.25em 0" });
    nameRow.appendChild(el("span", { text: "Name: ", class: "help" }));
    var nameInput = el("input", {
      type: "text", placeholder: zone.friendly_name || "e.g. the back walkway",
      style: "width:180px",
    });
    nameInput.value = configured;
    nameInput.addEventListener("input", function () {
      if (!doc.zone_names) doc.zone_names = {};
      var v = nameInput.value.trim();
      if (v) doc.zone_names[zone.zone] = v;
      else delete doc.zone_names[zone.zone];
      markDirty();
    });
    nameRow.appendChild(nameInput);
    card.appendChild(nameRow);

    // Place class picker.
    var classRow = el("div", { style: "margin:0.25em 0" });
    classRow.appendChild(el("span", { text: "Place: ", class: "help" }));
    var select = el("select", {});
    var current = (doc.zone_classes || {})[zone.zone] || "";
    var guessOpt = el("option", {
      value: "", text: "guess: " + PLACE_LABELS[zone.guessed_class],
    });
    select.appendChild(guessOpt);
    PLACES.forEach(function (p) {
      var opt = el("option", { value: p, text: PLACE_LABELS[p] });
      if (p === current) opt.selected = true;
      select.appendChild(opt);
    });
    select.addEventListener("change", function () {
      if (!doc.zone_classes) doc.zone_classes = {};
      if (select.value) doc.zone_classes[zone.zone] = select.value;
      else delete doc.zone_classes[zone.zone];
      markDirty();
    });
    classRow.appendChild(select);
    card.appendChild(classRow);

    // Per-subject overrides.
    var ovRow = el("div", { style: "margin:0.25em 0" });
    SUBJECTS.forEach(function (subject) {
      var wrap = el("label", { style: "margin-right:0.6em;white-space:nowrap" });
      wrap.appendChild(el("span", { class: "help", text: subject + " " }));
      var sel = el("select", {});
      sel.appendChild(el("option", { value: "", text: "inherit" }));
      var ov = ((doc.zone_overrides || {})[zone.zone] || {})[subject] || "";
      LEVELS.forEach(function (lvl) {
        var opt = el("option", { value: lvl, text: LEVEL_LABELS[lvl] });
        if (lvl === ov) opt.selected = true;
        sel.appendChild(opt);
      });
      sel.addEventListener("change", function () {
        if (!doc.zone_overrides) doc.zone_overrides = {};
        var row = doc.zone_overrides[zone.zone] || {};
        if (sel.value) row[subject] = sel.value;
        else delete row[subject];
        if (Object.keys(row).length) doc.zone_overrides[zone.zone] = row;
        else delete doc.zone_overrides[zone.zone];
        markDirty();
      });
      wrap.appendChild(sel);
      ovRow.appendChild(wrap);
    });
    card.appendChild(ovRow);
    return card;
  }

  function neighborSet(camera) {
    // Symmetric closure for display; edits write only the explicit map.
    var table = doc.camera_neighbors || {};
    var out = {};
    (table[camera] || []).forEach(function (n) { out[n] = true; });
    Object.keys(table).forEach(function (cam) {
      if ((table[cam] || []).indexOf(camera) !== -1) out[cam] = true;
    });
    delete out[camera];
    return out;
  }

  function toggleNeighbor(a, b, on) {
    if (!doc.camera_neighbors) doc.camera_neighbors = {};
    var table = doc.camera_neighbors;
    if (on) {
      var list = table[a] || [];
      if (list.indexOf(b) === -1) list.push(b);
      table[a] = list;
    } else {
      // Unticking must break the pair in BOTH declared directions, or the
      // symmetric closure resurrects it.
      [[a, b], [b, a]].forEach(function (pair) {
        var list = table[pair[0]] || [];
        var i = list.indexOf(pair[1]);
        if (i !== -1) list.splice(i, 1);
        if (!list.length) delete table[pair[0]];
        else table[pair[0]] = list;
      });
    }
    markDirty();
  }

  function renderNeighbors(cameras) {
    neighborsList.textContent = "";
    cameras.forEach(function (camera) {
      var row = el("div", { style: "margin:0.35em 0" });
      row.appendChild(el("strong", { text: camera, style: "margin-right:0.6em" }));
      var linked = neighborSet(camera);
      cameras.forEach(function (other) {
        if (other === camera) return;
        var label = el("label", { style: "margin-right:0.6em;white-space:nowrap" });
        var box = el("input", { type: "checkbox" });
        box.checked = !!linked[other];
        box.addEventListener("change", function () {
          toggleNeighbor(camera, other, box.checked);
          renderNeighbors(cameras); // re-render so the mirror row updates
        });
        label.appendChild(box);
        label.appendChild(document.createTextNode(" " + other));
        row.appendChild(label);
      });
      neighborsList.appendChild(row);
    });
  }

  saveBtn.addEventListener("click", async function () {
    saveBtn.disabled = true;
    saveState.textContent = "saving...";
    try {
      // Send the whole settings doc; camera_neighbors is sticky server-side
      // when absent, so it must always be sent explicitly (even empty).
      if (!doc.camera_neighbors) doc.camera_neighbors = {};
      await fetchJson("/v1/push/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(doc),
      });
      dirty = false;
      saveState.textContent = "saved ✓";
    } catch (err) {
      saveState.textContent = "error: " + err.message;
    }
    saveBtn.disabled = false;
  });

  // ---- Export / import (instance-to-instance sync via the browser) ----

  var syncState = document.getElementById("sync-state");

  document.getElementById("export-btn").addEventListener("click", async function () {
    try {
      // Fresh GET: export what the engine is actually running, not the
      // page's possibly-unsaved draft.
      var data = await fetchJson("/v1/push/settings");
      var blob = new Blob(
        [JSON.stringify(data.settings, null, 2)], { type: "application/json" }
      );
      var a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "push-settings-" + location.hostname + ".json";
      a.click();
      URL.revokeObjectURL(a.href);
      syncState.textContent = "exported";
    } catch (err) {
      syncState.textContent = "export error: " + err.message;
    }
  });

  document.getElementById("import-file").addEventListener("change", async function (ev) {
    var file = ev.target.files && ev.target.files[0];
    ev.target.value = ""; // allow re-picking the same file
    if (!file) return;
    syncState.textContent = "importing...";
    try {
      var imported = JSON.parse(await file.text());
      if (typeof imported !== "object" || !imported || Array.isArray(imported)) {
        throw new Error("not a settings document");
      }
      await fetchJson("/v1/push/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(imported),
      });
      syncState.textContent = "imported ✓ — reloading";
      location.reload();
    } catch (err) {
      syncState.textContent = "import error: " + err.message;
    }
  });

  (async function init() {
    try {
      var data = await fetchJson("/v1/push/settings");
      doc = data.settings;
      zonesList.textContent = "";
      (data.available_zones || []).forEach(function (zone) {
        zonesList.appendChild(renderZone(zone));
      });
      renderNeighbors(data.available_cameras || []);
      if (!(data.available_zones || []).length) {
        showBanner("No zones found in the Frigate config.", false);
      }
    } catch (err) {
      showBanner(err.message, true);
    }
  })();
})();
