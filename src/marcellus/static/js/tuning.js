// Tuning panel + effective-configuration table: GET/PUT /v1/tuning.
// Separate document/rev/save cycle from the policy editor (zones.js/
// policy.js) -- this file owns its own #tuning-save button and never
// touches #save-btn or SC.policy.
(function () {
  var el = SC.el;
  var fetchJson = SC.fetchJson;

  var sectionsWrap = document.getElementById("tuning-sections");
  var restartBanner = document.getElementById("tuning-restart-banner");
  var saveBtn = document.getElementById("tuning-save");
  var saveState = document.getElementById("tuning-state");
  var configTable = document.getElementById("config-table");
  if (!sectionsWrap || !saveBtn) return; // page without the tuning markup

  var rev = null;
  var knobsByKey = {}; // key -> knob row from the last GET/PUT
  var overrides = {}; // key -> pending value to send on save
  var pendingKeys = {}; // key -> true while a reset (removal) is queued
  var invalidKeys = {}; // key -> true while that row's control holds an unparsable value
  var dirty = false;

  var showBanner = SC.banner(restartBanner);

  function updateSaveState() {
    var invalidCount = Object.keys(invalidKeys).length;
    if (invalidCount) {
      saveBtn.disabled = true;
      saveState.textContent = "fix " + invalidCount + " invalid value(s)";
      return;
    }
    saveBtn.disabled = false;
    if (dirty) saveState.textContent = "unsaved changes";
  }

  function markDirty() {
    dirty = true;
    updateSaveState();
  }

  function setRowInvalid(key, row, isInvalid) {
    if (isInvalid) {
      invalidKeys[key] = true;
      row.classList.add("invalid");
    } else {
      delete invalidKeys[key];
      row.classList.remove("invalid");
    }
    updateSaveState();
  }

  function showRestartBanner(keys) {
    if (keys && keys.length) {
      showBanner("Restart required for: " + keys.join(", "), true);
    } else {
      restartBanner.style.display = "none";
    }
  }

  function describeErrorBody(errJson) {
    // Server sends {"detail": {"error": "invalid_tuning", "detail": [...]}};
    // render the inner array, joined, falling back to whatever we got.
    try {
      var d = errJson && errJson.detail;
      if (d && Array.isArray(d.detail)) return d.detail.join("; ");
    } catch (e) { /* fall through */ }
    return null;
  }

  // ---- control builders, one per Knob.kind ----
  // onChange(value) sets a pending override; onRemove() clears it (reverts
  // to base value); onInvalidChange(bool) is only used by controls whose
  // text can fail to parse (number, json).

  function numberControl(knob, value, onChange, onRemove, onInvalidChange) {
    var input = el("input", {
      type: "number",
      step: knob.kind === "int" ? "1" : "any",
    });
    if (knob.min !== null && knob.min !== undefined) input.setAttribute("min", knob.min);
    if (knob.max !== null && knob.max !== undefined) input.setAttribute("max", knob.max);
    input.value = value === null || value === undefined ? "" : value;
    input.addEventListener("change", function () {
      if (input.value.trim() === "") {
        onInvalidChange(false);
        onRemove();
        return;
      }
      var n = knob.kind === "int" ? parseInt(input.value, 10) : parseFloat(input.value);
      if (isNaN(n)) {
        onInvalidChange(true);
        return;
      }
      onInvalidChange(false);
      onChange(n);
    });
    return input;
  }

  function boolControl(knob, value, onChange) {
    var box = el("input", { type: "checkbox" });
    box.checked = !!value;
    box.addEventListener("change", function () { onChange(box.checked); });
    return box;
  }

  function enumControl(knob, value, onChange) {
    var sel = el("select", {});
    var choices = knob.choices || [];
    if (value !== null && value !== undefined && choices.indexOf(value) === -1) {
      var unknown = el("option", { value: value, text: value + " (unrecognized)" });
      unknown.disabled = true;
      unknown.selected = true;
      sel.appendChild(unknown);
    }
    choices.forEach(function (c) {
      var opt = el("option", { value: c, text: c });
      if (c === value) opt.selected = true;
      sel.appendChild(opt);
    });
    sel.addEventListener("change", function () { onChange(sel.value); });
    return sel;
  }

  function textControl(knob, value, onChange, onRemove) {
    var input = el("input", { type: "text" });
    input.value = value === null || value === undefined ? "" : value;
    input.addEventListener("change", function () {
      if (input.value === "") {
        onRemove();
        return;
      }
      onChange(input.value);
    });
    return input;
  }

  function listStrControl(knob, value, onChange) {
    var input = el("input", { type: "text" });
    input.value = (value || []).join(", ");
    input.addEventListener("change", function () {
      var parts = input.value.split(",").map(function (s) { return s.trim(); })
        .filter(function (s) { return s.length; });
      onChange(parts);
    });
    return input;
  }

  function dictIntControl(knob, value, onChange) {
    var wrap = el("div", { class: "tuning-dict" });
    var current = Object.assign({}, value || {});

    function renderRow(k) {
      var row = el("span", { class: "tuning-dict-item" });
      row.appendChild(el("span", { class: "help", text: k + ": " }));
      var input = el("input", { type: "number", step: "1" });
      input.value = current[k];
      input.addEventListener("change", function () {
        if (input.value.trim() === "") {
          delete current[k];
          onChange(Object.assign({}, current));
          renderAll();
          return;
        }
        var n = parseInt(input.value, 10);
        if (isNaN(n)) return;
        current[k] = n;
        onChange(Object.assign({}, current));
      });
      row.appendChild(input);
      return row;
    }

    function renderAll() {
      wrap.textContent = "";
      Object.keys(current).sort().forEach(function (k) { wrap.appendChild(renderRow(k)); });

      var addRow = el("span", { class: "tuning-dict-item tuning-dict-add" });
      var keyInput = el("input", { type: "text", placeholder: "key" });
      var valInput = el("input", { type: "number", step: "1", placeholder: "value" });
      var addBtn = el("button", { type: "button", text: "add" });
      addBtn.addEventListener("click", function () {
        var k = keyInput.value.trim();
        if (!k) return;
        var n = parseInt(valInput.value, 10);
        if (isNaN(n)) return;
        current[k] = n;
        onChange(Object.assign({}, current));
        renderAll();
      });
      addRow.appendChild(keyInput);
      addRow.appendChild(valInput);
      addRow.appendChild(addBtn);
      wrap.appendChild(addRow);
    }

    renderAll();
    return wrap;
  }

  function jsonControl(knob, value, onChange, onRemove, onInvalidChange) {
    var ta = el("textarea", { class: "tuning-json", rows: "3" });
    ta.value = JSON.stringify(value === undefined ? null : value, null, 2);
    ta.addEventListener("change", function () {
      if (ta.value.trim() === "") {
        onInvalidChange(false);
        onRemove();
        return;
      }
      try {
        var parsed = JSON.parse(ta.value);
        ta.classList.remove("invalid");
        onInvalidChange(false);
        onChange(parsed);
      } catch (e) {
        ta.classList.add("invalid");
        onInvalidChange(true);
      }
    });
    return ta;
  }

  function pairListControl(knob, value, onChange, onRemove, onInvalidChange) {
    // One "camera_a, camera_b" pair per line -- friendlier than raw JSON for
    // a list[list[str]] knob (encounters.adjacency/not_adjacent).
    var ta = el("textarea", { class: "tuning-pairlist", rows: "3" });
    ta.value = (value || []).map(function (pair) { return pair.join(", "); }).join("\n");
    ta.addEventListener("change", function () {
      var text = ta.value.trim();
      if (text === "") {
        onInvalidChange(false);
        onRemove();
        return;
      }
      var pairs = [];
      var lines = text.split("\n");
      for (var i = 0; i < lines.length; i++) {
        var line = lines[i].trim();
        if (!line) continue;
        var parts = line.split(",").map(function (s) { return s.trim(); })
          .filter(function (s) { return s.length; });
        if (parts.length !== 2 || parts[0] === parts[1]) {
          ta.classList.add("invalid");
          onInvalidChange(true);
          return;
        }
        pairs.push(parts);
      }
      ta.classList.remove("invalid");
      onInvalidChange(false);
      onChange(pairs);
    });
    return ta;
  }

  function buildControl(knob, value, onChange, onRemove, onInvalidChange) {
    switch (knob.kind) {
      case "int":
      case "float":
        return numberControl(knob, value, onChange, onRemove, onInvalidChange);
      case "bool":
        return boolControl(knob, value, onChange);
      case "enum":
        return enumControl(knob, value, onChange);
      case "list_str":
        return listStrControl(knob, value, onChange);
      case "dict_int":
        return dictIntControl(knob, value, onChange);
      case "json":
        return jsonControl(knob, value, onChange, onRemove, onInvalidChange);
      case "pair_list":
        return pairListControl(knob, value, onChange, onRemove, onInvalidChange);
      default: // str, path, url, secret -- editable ones are plain text
        return textControl(knob, value, onChange, onRemove);
    }
  }

  // ---- rendering ----

  function sourceBadge(source) {
    return el("span", { class: "badge src-" + source, text: source });
  }

  function pendingSource(knob) {
    // Where a knob's value will come from once a reset/removal is applied.
    if (knob.locked) return "env";
    if (knob.value !== knob.default) return "yaml";
    return "default";
  }

  function renderKnobRow(knob) {
    var row = el("div", { class: "tuning-row", "data-key": knob.key });
    var label = el("div", { class: "tuning-label" }, [
      el("code", { text: knob.field }),
    ]);
    if (knob.help) label.appendChild(el("div", { class: "help", text: knob.help }));
    row.appendChild(label);

    var controlWrap = el("div", { class: "tuning-control" });

    function applyOverride(newValue) {
      overrides[knob.key] = newValue;
      delete pendingKeys[knob.key];
      badge.className = "badge src-override";
      badge.textContent = "override";
      resetLink.style.display = "";
      markDirty();
    }

    function removeOverride() {
      delete overrides[knob.key];
      pendingKeys[knob.key] = true;
      var src = pendingSource(knob);
      badge.className = "badge src-" + src;
      badge.textContent = src;
      resetLink.style.display = "none";
      markDirty();
    }

    function setInvalid(isInvalid) {
      setRowInvalid(knob.key, row, isInvalid);
    }

    if (!knob.editable || knob.locked) {
      controlWrap.appendChild(el("span", { class: "tuning-value", text: String(knob.value) }));
    } else {
      var control = buildControl(knob, knob.value, applyOverride, removeOverride, setInvalid);
      controlWrap.appendChild(control);
    }
    row.appendChild(controlWrap);

    var badge = sourceBadge(knob.locked ? "env" : knob.source);
    row.appendChild(badge);

    if (!knob.live) row.appendChild(el("span", { class: "badge restart", text: "restart" }));

    var resetLink = el("a", { href: "#", class: "tuning-reset", text: "reset" });
    resetLink.style.display = knob.source === "override" && knob.editable ? "" : "none";
    resetLink.addEventListener("click", function (ev) {
      ev.preventDefault();
      delete overrides[knob.key];
      pendingKeys[knob.key] = true;
      resetLink.style.display = "none";
      setRowInvalid(knob.key, row, false);

      var src = pendingSource(knob);
      badge.className = "badge src-" + src;
      badge.textContent = src;

      controlWrap.textContent = "";
      if (!knob.editable || knob.locked) {
        controlWrap.appendChild(el("span", { class: "tuning-value", text: String(knob.value) }));
      } else {
        // We don't know the underlying yaml value client-side, only the
        // knob's declared default -- restore to that and let a save/reload
        // pick up the true yaml value from the server.
        var restored = buildControl(knob, knob.default, applyOverride, removeOverride, setInvalid);
        controlWrap.appendChild(restored);
        var note = el("div", { class: "help", text: "restored to default — save to confirm" });
        controlWrap.appendChild(note);
      }
      markDirty();
    });
    row.appendChild(resetLink);
    return row;
  }

  // ---- Location block (location.latitude/longitude/label) ----
  // One custom block instead of three generic rows: coordinates, the saved
  // label, and an address lookup that runs in the browser against
  // OpenStreetMap's Nominatim. Edits go into the same `overrides` dict as
  // every other knob and are sent by "Save tuning"; nothing is stored by the
  // lookup itself.
  var NOMINATIM = "https://nominatim.openstreetmap.org/search?format=jsonv2&limit=5&q=";
  var LOC_KEYS = ["location.latitude", "location.longitude", "location.label"];

  function roundCoord(n) { return Math.round(n * 10000) / 10000; }

  function renderLocationBlock(grid, data) {
    var latKnob = knobsByKey["location.latitude"];
    var lonKnob = knobsByKey["location.longitude"];
    var labelKnob = knobsByKey["location.label"];
    if (!latKnob || !lonKnob || !labelKnob) return;
    var locked = LOC_KEYS.some(function (k) { return knobsByKey[k] && knobsByKey[k].locked; });

    var block = el("div", { class: "loc-block tuning-control", "data-key": "location" });

    function numField(labelText, knob) {
      var input = el("input", { type: "number", step: "any", min: knob.min, max: knob.max });
      input.value = knob.value === null || knob.value === undefined ? "" : knob.value;
      input.disabled = locked;
      var wrap = el("label", { class: "loc-field" }, [
        el("span", { class: "help", text: labelText }),
        input,
      ]);
      return { wrap: wrap, input: input };
    }
    var lat = numField("Latitude", latKnob);
    var lon = numField("Longitude", lonKnob);
    block.appendChild(el("div", { class: "loc-fields" }, [lat.wrap, lon.wrap]));

    var label = labelKnob.value || "";
    var labelLine = el("div", { class: "help" });
    function showLabel() {
      labelLine.textContent = label ? "Saved as: " + label : "";
      labelLine.style.display = label ? "" : "none";
    }
    showLabel();
    block.appendChild(labelLine);

    // Push the three fields into `overrides`, or flag the block invalid.
    function commit() {
      var latText = lat.input.value.trim();
      var lonText = lon.input.value.trim();
      if (latText === "" && lonText === "") {
        LOC_KEYS.forEach(function (k) { delete overrides[k]; pendingKeys[k] = true; });
        setRowInvalid("location", block, false);
        markDirty();
        return;
      }
      var la = parseFloat(latText);
      var lo = parseFloat(lonText);
      var ok = latText !== "" && lonText !== "" && !isNaN(la) && !isNaN(lo) &&
        la >= -90 && la <= 90 && lo >= -180 && lo <= 180;
      setRowInvalid("location", block, !ok);
      if (!ok) return;
      overrides["location.latitude"] = la;
      overrides["location.longitude"] = lo;
      delete pendingKeys["location.latitude"];
      delete pendingKeys["location.longitude"];
      if (label) {
        overrides["location.label"] = label;
        delete pendingKeys["location.label"];
      } else {
        delete overrides["location.label"];
        pendingKeys["location.label"] = true;
      }
      markDirty();
    }

    // Typing coordinates by hand means the old label no longer describes them.
    [lat.input, lon.input].forEach(function (input) {
      input.addEventListener("change", function () {
        label = "";
        showLabel();
        commit();
      });
    });

    var addrInput = el("input", { type: "text", placeholder: "e.g. 1600 Pennsylvania Ave, Washington" });
    addrInput.disabled = locked;
    var lookupBtn = el("button", { type: "button", class: "btn-neutral", text: "Look up" });
    lookupBtn.disabled = locked;
    var addrWrap = el("label", { class: "loc-field loc-address" }, [
      el("span", { class: "help", text: "Address" }),
      addrInput,
    ]);
    block.appendChild(el("div", { class: "loc-fields" }, [addrWrap, lookupBtn]));
    block.appendChild(el("div", {
      class: "help",
      text: "Looking up an address sends it from your browser to OpenStreetMap. " +
        "Or type the coordinates yourself.",
    }));
    var msg = el("div", { class: "help" });
    var results = el("div", { class: "loc-results" });
    block.appendChild(msg);
    block.appendChild(results);

    var lookupSeq = 0;
    async function lookup() {
      var q = addrInput.value.trim();
      if (!q) return;
      var seq = ++lookupSeq;
      results.textContent = "";
      msg.textContent = "Looking up...";
      var matches;
      try {
        var resp = await fetch(NOMINATIM + encodeURIComponent(q));
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        matches = await resp.json();
      } catch (err) {
        if (seq !== lookupSeq) return;
        msg.textContent = "Couldn't reach OpenStreetMap (" + err.message + "). " +
          "Check your connection, or type the coordinates yourself.";
        return;
      }
      if (seq !== lookupSeq) return;
      matches = (Array.isArray(matches) ? matches : []).filter(function (m) {
        return !isNaN(parseFloat(m.lat)) && !isNaN(parseFloat(m.lon));
      }).slice(0, 5);
      if (!matches.length) {
        msg.textContent = "No matches for that address. Try fewer words, or type the coordinates yourself.";
        return;
      }
      msg.textContent = "Pick the right match:";
      matches.forEach(function (m) {
        var btn = el("button", { type: "button", class: "btn-neutral loc-match", text: m.display_name });
        btn.addEventListener("click", function () {
          lat.input.value = roundCoord(parseFloat(m.lat));
          lon.input.value = roundCoord(parseFloat(m.lon));
          label = m.display_name;
          showLabel();
          results.textContent = "";
          msg.textContent = "Filled in. Press Save tuning to keep it.";
          commit();
        });
        results.appendChild(btn);
      });
    }
    lookupBtn.addEventListener("click", lookup);
    addrInput.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter") {
        ev.preventDefault();
        lookup();
      }
    });

    var loc = (data && data.location) || {};
    if (loc.sun_up === true || loc.sun_up === false) {
      block.appendChild(el("div", {
        class: "help loc-sun",
        text: loc.sun_up ? "Sun is up" : "Sun is down",
      }));
    }
    if (locked) {
      block.appendChild(el("div", {
        class: "help", text: "Pinned by an environment variable; change it there.",
      }));
    }
    grid.appendChild(block);
  }

  function renderSections(data) {
    knobsByKey = {};
    (data.knobs || []).forEach(function (k) { knobsByKey[k.key] = k; });
    (data.sections || []).forEach(function (sec) {
      var grid = document.getElementById("tuning-grid-" + (sec.name || "root"));
      if (!grid) return;
      grid.textContent = "";
      var rows = (data.knobs || []).filter(function (k) { return k.section === sec.name; });
      if (sec.name === "location" && rows.length) {
        renderLocationBlock(grid, data);
        return;
      }
      if (!rows.length) {
        var det = grid.closest ? grid.closest("details") : null;
        if (det) det.style.display = "none";
        return;
      }
      rows.forEach(function (k) { grid.appendChild(renderKnobRow(k)); });
    });
  }

  function renderConfigTable(data) {
    if (!configTable) return;
    configTable.textContent = "";
    configTable.classList.remove("skeleton");
    var rows = (data.knobs || []).filter(function (k) { return !k.editable; });
    var wrap = el("div", { class: "table-scroll" });
    var table = el("table", { class: "motion-table" });
    var thead = el("thead", {}, [
      el("tr", {}, [el("th", { text: "key" }), el("th", { text: "value" }), el("th", { text: "source" })]),
    ]);
    table.appendChild(thead);
    var tbody = el("tbody", {});
    rows.forEach(function (k) {
      tbody.appendChild(el("tr", {}, [
        el("td", { text: k.key }),
        el("td", { text: k.value === null || k.value === undefined ? "—" : String(k.value) }),
        el("td", {}, [sourceBadge(k.locked ? "env" : k.source)]),
      ]));
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
    configTable.appendChild(wrap);
  }

  // ---- load / save ----

  function resetLocalOverrides(data) {
    // Seed from the server's stored override dict, never from `knob.value`
    // (which is the *effective* value -- for dict_int it's the merged
    // default+override dict, and for everything else the display value
    // regardless of source).
    overrides = Object.assign({}, data.overrides || {});
    pendingKeys = {};
    invalidKeys = {};
  }

  async function loadFromServer() {
    var data = await fetchJson("/v1/tuning");
    rev = data.rev;
    resetLocalOverrides(data);
    sectionsWrap.classList.remove("skeleton");
    renderSections(data);
    renderConfigTable(data);
    showRestartBanner(data.pending_restart);
    updateSaveState();
    return data;
  }

  async function doSave(useRev) {
    return fetch("/v1/tuning", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ rev: useRev, overrides: overrides }),
    });
  }

  saveBtn.addEventListener("click", async function () {
    if (Object.keys(invalidKeys).length) return;
    saveBtn.disabled = true;
    saveState.textContent = "saving...";
    var resp;
    try {
      resp = await doSave(rev);
    } catch (err) {
      saveState.textContent = "error: " + err.message;
      saveBtn.disabled = false;
      return;
    }
    if (resp.status === 409) {
      saveBtn.disabled = false;
      saveState.textContent = "conflict — resolve to continue";
      SC.conflictDialog({
        onReload: async function () {
          try {
            await loadFromServer();
            dirty = false;
            saveState.textContent = "reloaded";
          } catch (err) {
            saveState.textContent = "error: " + err.message;
          }
        },
        onOverwrite: async function () {
          saveState.textContent = "saving...";
          try {
            var fresh = await fetchJson("/v1/tuning");
            var resp2 = await doSave(fresh.rev);
            if (!resp2.ok) {
              var errJson2 = await resp2.json();
              throw new Error(describeErrorBody(errJson2) || JSON.stringify(errJson2.detail || errJson2));
            }
            var j2 = await resp2.json();
            rev = j2.rev;
            resetLocalOverrides(j2);
            renderSections(j2);
            renderConfigTable(j2);
            showRestartBanner(j2.pending_restart);
            dirty = false;
            saveState.textContent = "saved ✓";
          } catch (err) {
            saveState.textContent = "error: " + err.message;
          }
        },
      });
      return;
    }
    try {
      if (!resp.ok) {
        var msg = "HTTP " + resp.status;
        try {
          var errJson = await resp.json();
          msg = describeErrorBody(errJson) || JSON.stringify(errJson.detail || errJson);
        } catch (e) { /* keep status text */ }
        throw new Error(msg);
      }
      var okJson = await resp.json();
      rev = okJson.rev;
      resetLocalOverrides(okJson);
      renderSections(okJson);
      renderConfigTable(okJson);
      showRestartBanner(okJson.pending_restart);
      dirty = false;
      saveState.textContent = "saved ✓";
    } catch (err) {
      saveState.textContent = "error: " + err.message;
    }
    saveBtn.disabled = false;
    updateSaveState();
  });

  window.addEventListener("beforeunload", function (ev) {
    if (dirty) {
      ev.preventDefault();
      ev.returnValue = "";
    }
  });

  (async function init() {
    try {
      await loadFromServer();
    } catch (err) {
      saveState.textContent = "error: " + err.message;
    }
  })();
})();
