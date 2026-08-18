// Inspector panel: camera list, contextual detail for the selection,
// map settings when nothing is selected, and the sticky save bar.
// Every numeric commit goes through store.edit() so it is undoable.

import { CARDINALS, cardinalOf, clamp01, mapAspect, unitToFt } from "./geometry.js";

function el(tag, attrs = {}, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k === "text") n.textContent = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v);
  }
  for (const c of children) if (c) n.appendChild(c);
  return n;
}

export class Inspector {
  constructor(panelEl, store, view, renderer, tools) {
    this.el = panelEl;
    this.store = store;
    this.view = view;
    this.renderer = renderer;
    this.tools = tools;
    this.selection = null;
    this.listEl = el("div", { class: "me-camlist" });
    this.detailEl = el("div", { class: "me-detail" });
    this.saveBar = el("div", { class: "me-savebar" });
    panelEl.appendChild(this.listEl);
    panelEl.appendChild(this.detailEl);
    panelEl.appendChild(this.saveBar);
    store.subscribe(() => { this.renderList(); this.renderDetail(); this.renderSaveBar(); });
  }

  setSelection(sel) {
    this.selection = sel;
    this.renderList();
    this.renderDetail();
  }

  // ---- Field helpers ---------------------------------------------------

  _num({ label, value, min, max, step, unit, disabled, commit }) {
    const input = el("input", { type: "number", class: "me-num" });
    if (min !== undefined) input.min = min;
    if (max !== undefined) input.max = max;
    if (step !== undefined) input.step = step;
    input.value = value === undefined || value === null || Number.isNaN(value)
      ? "" : String(value);
    if (disabled) input.disabled = true;
    input.addEventListener("change", () => {
      const v = input.value === "" ? null : parseFloat(input.value);
      if (v !== null && Number.isNaN(v)) return;
      commit(v);
    });
    input.addEventListener("keydown", (ev) => ev.stopPropagation());
    return el("label", { class: "me-field" },
      el("span", { class: "me-field-label", text: label }),
      input,
      unit ? el("span", { class: "me-field-unit", text: unit }) : null);
  }

  _select({ label, value, options, disabled, commit }) {
    const sel = el("select", { class: "me-num" });
    for (const o of options) {
      const opt = el("option", { value: o.value, text: o.label });
      if (String(o.value) === String(value ?? "")) opt.selected = true;
      sel.appendChild(opt);
    }
    if (disabled) sel.disabled = true;
    sel.addEventListener("change", () => commit(sel.value));
    return el("label", { class: "me-field" },
      el("span", { class: "me-field-label", text: label }), sel);
  }

  // ---- Camera list -----------------------------------------------------

  renderList() {
    const doc = this.store.doc;
    if (!doc) return;
    this.listEl.textContent = "";
    this.listEl.appendChild(el("div", { class: "me-sec-title", text: "Cameras" }));
    for (const cam of this.store.availableCameras) {
      const entry = (doc.camera_layout || {})[cam];
      const placed = entry && entry.x !== undefined;
      const selected = this.selection?.kind === "camera" && this.selection.camera === cam;
      const row = el("div", {
        class: "me-camrow" + (selected ? " sel" : ""),
        onclick: () => {
          this.tools.select({ kind: "camera", camera: cam });
        },
      },
      el("span", {
        class: "me-camdot" + (placed ? " placed" : ""),
        text: entry && entry.locked ? "🔒" : "●",
      }),
      el("span", { class: "me-camname", text: cam }),
      placed
        ? el("span", {
          class: "me-camaz",
          text: entry.azimuth !== undefined
            ? `${Math.round(entry.azimuth)}° ${cardinalOf(entry.azimuth)}` : "unaimed",
        })
        : el("button", {
          class: "btn-primary me-place", text: "Place",
          onclick: (ev) => { ev.stopPropagation(); this.placeCamera(cam); },
        }));
      this.listEl.appendChild(row);
    }
  }

  placeCamera(cam) {
    const v = this.view.view;
    const cx = v.x + v.w / 2, cy = v.y + this.view.viewH() / 2;
    this.store.edit(`place ${cam}`, (doc) => {
      if (!doc.camera_layout) doc.camera_layout = {};
      doc.camera_layout[cam] = {
        x: +clamp01(cx).toFixed(4),
        y: +clamp01(cy).toFixed(4),
      };
    }, ["camera_layout"]);
    this.tools.select({ kind: "camera", camera: cam });
  }

  // ---- Detail ----------------------------------------------------------

  renderDetail() {
    this.detailEl.textContent = "";
    const doc = this.store.doc;
    if (!doc) return;
    if (this.selection?.kind === "camera") this._cameraDetail(this.selection.camera);
    else if (this.selection?.kind === "secure") this._secureDetail();
    else this._mapDetail();
  }

  _ftFields(container, get, set, locked) {
    // Position in feet when the map is scaled, raw units otherwise.
    const doc = this.store.doc;
    const ft = unitToFt(doc);
    const p = get();
    if (ft) {
      container.appendChild(this._num({
        label: "x", unit: "ft", step: 0.1, disabled: locked,
        value: p.x === undefined ? null : +(p.x * ft.x).toFixed(1),
        commit: (v) => v !== null && set({ x: clamp01(v / ft.x) }),
      }));
      container.appendChild(this._num({
        label: "y", unit: "ft", step: 0.1, disabled: locked,
        value: p.y === undefined ? null : +(p.y * ft.y).toFixed(1),
        commit: (v) => v !== null && set({ y: clamp01(v / ft.y) }),
      }));
    } else {
      container.appendChild(this._num({
        label: "x", unit: "·", step: 0.005, min: 0, max: 1, disabled: locked,
        value: p.x === undefined ? null : p.x,
        commit: (v) => v !== null && set({ x: clamp01(v) }),
      }));
      container.appendChild(this._num({
        label: "y", unit: "·", step: 0.005, min: 0, max: 1, disabled: locked,
        value: p.y === undefined ? null : p.y,
        commit: (v) => v !== null && set({ y: clamp01(v) }),
      }));
    }
  }

  _cameraDetail(cam) {
    const doc = this.store.doc;
    const entry = (doc.camera_layout || {})[cam] || {};
    const optics = (doc.camera_optics || {})[cam] || {};
    const locked = !!entry.locked;
    const d = this.detailEl;

    d.appendChild(el("div", { class: "me-sec-title" },
      el("span", { text: cam }),
      el("button", {
        class: "btn-neutral me-lockbtn",
        text: locked ? "Unlock" : "Lock",
        title: "Lock = no drags, nudges or edits until unlocked. Saved with Save.",
        onclick: () => {
          this.store.edit(`${locked ? "unlock" : "lock"} ${cam}`, (dd) => {
            const e = dd.camera_layout?.[cam];
            if (!e) return;
            if (locked) delete e.locked; else e.locked = true;
          }, ["camera_layout"]);
        },
      })));

    const snap = el("img", {
      class: "me-snap", alt: cam,
      src: `/api/${encodeURIComponent(cam)}/latest.jpg?h=180`,
    });
    d.appendChild(snap);

    const grid = el("div", { class: "me-fields" });
    this._ftFields(grid,
      () => entry,
      (patch) => this.store.edit(`edit ${cam} position`, (dd) => {
        const e = dd.camera_layout[cam];
        if (patch.x !== undefined) e.x = +patch.x.toFixed(4);
        if (patch.y !== undefined) e.y = +patch.y.toFixed(4);
      }, ["camera_layout"]),
      locked || entry.x === undefined);
    grid.appendChild(this._num({
      label: "azimuth", unit: "°", min: 0, max: 359.9, step: 1,
      disabled: locked,
      value: entry.azimuth,
      commit: (v) => this.store.edit(`aim ${cam}`, (dd) => {
        const e = dd.camera_layout?.[cam];
        if (!e) return;
        if (v === null) { delete e.azimuth; return; }
        e.azimuth = +(((v % 360) + 360) % 360).toFixed(1);
        if (e.fov === undefined) e.fov = optics.hfov || 90;
      }, ["camera_layout"]),
    }));
    grid.appendChild(this._num({
      label: "field of view", unit: "°", min: 10, max: 360, step: 5,
      disabled: locked,
      value: entry.fov,
      commit: (v) => v !== null && this.store.edit(`fov ${cam}`, (dd) => {
        const e = dd.camera_layout?.[cam];
        if (e) e.fov = +Math.min(360, Math.max(10, v)).toFixed(1);
      }, ["camera_layout"]),
    }));
    d.appendChild(grid);

    d.appendChild(el("div", { class: "me-sec-sub", text: "Optics (rig facts)" }));
    const og = el("div", { class: "me-fields" });
    const opticsEdit = (label, fn) => this.store.edit(label, (dd) => {
      if (!dd.camera_optics) dd.camera_optics = {};
      if (!dd.camera_optics[cam]) dd.camera_optics[cam] = {};
      fn(dd.camera_optics[cam]);
    }, ["camera_optics"]);
    og.appendChild(this._num({
      label: "HFOV", unit: "°", min: 10, max: 360, step: 1, disabled: locked,
      value: optics.hfov,
      commit: (v) => opticsEdit(`edit ${cam} hfov`, (o) => {
        if (v === null) delete o.hfov; else o.hfov = +v.toFixed(1);
      }),
    }));
    og.appendChild(this._num({
      label: "mount", unit: "ft", min: 1, max: 500, step: 0.5, disabled: locked,
      value: optics.mount_ft,
      commit: (v) => opticsEdit(`edit ${cam} mount`, (o) => {
        if (v === null) delete o.mount_ft; else o.mount_ft = +v.toFixed(1);
      }),
    }));
    og.appendChild(this._num({
      label: "tilt", unit: "° down", min: -90, max: 90, step: 1, disabled: locked,
      value: optics.tilt_deg,
      commit: (v) => opticsEdit(`edit ${cam} tilt`, (o) => {
        if (v === null) delete o.tilt_deg; else o.tilt_deg = +v.toFixed(1);
      }),
    }));
    og.appendChild(this._num({
      label: "VFOV", unit: "°", min: 5, max: 180, step: 1, disabled: locked,
      value: optics.vfov,
      commit: (v) => opticsEdit(`edit ${cam} vfov`, (o) => {
        if (v === null) delete o.vfov; else o.vfov = +v.toFixed(1);
      }),
    }));
    const presets = window.LENS_PRESETS || [];
    if (presets.length) {
      og.appendChild(this._select({
        label: "lens", disabled: locked,
        value: optics.lens ?? "",
        options: [{ value: "", label: "—" }].concat(
          presets.map((p) => ({ value: p.id ?? p.name, label: p.name ?? p.id }))),
        commit: (v) => opticsEdit(`edit ${cam} lens`, (o) => {
          if (!v) { delete o.lens; return; }
          o.lens = v;
          const preset = presets.find((p) => String(p.id ?? p.name) === v);
          if (preset && preset.hfov) o.hfov = preset.hfov;
        }),
      }));
    }
    og.appendChild(this._select({
      label: "faces", disabled: locked,
      value: optics.faces ?? "",
      options: [{ value: "", label: "—" }].concat(
        CARDINALS.map((c) => ({ value: c, label: c }))),
      commit: (v) => opticsEdit(`edit ${cam} faces`, (o) => {
        if (!v) delete o.faces; else o.faces = v;
      }),
    }));
    d.appendChild(og);

    d.appendChild(el("div", { class: "me-actions" },
      el("button", {
        class: "btn-neutral", text: "Remove from map", disabled: locked || undefined,
        onclick: () => this.store.edit(`remove ${cam} from map`, (dd) => {
          if (dd.camera_layout) delete dd.camera_layout[cam];
        }, ["camera_layout"]),
      })));
  }

  _secureDetail() {
    const doc = this.store.doc;
    const sa = doc.secure_area;
    const d = this.detailEl;
    d.appendChild(el("div", { class: "me-sec-title", text: "Secure area" }));
    if (!sa) {
      d.appendChild(el("p", { class: "help", text: "No secure area drawn." }));
      return;
    }
    const ft = unitToFt(doc);
    const grid = el("div", { class: "me-fields" });
    for (const k of ["x0", "y0", "x1", "y1"]) {
      const axis = k[0] === "x" ? "x" : "y";
      const scale = ft ? ft[axis] : 1;
      grid.appendChild(this._num({
        label: k, unit: ft ? "ft" : "·", step: ft ? 0.1 : 0.005,
        value: +(sa[k] * scale).toFixed(ft ? 1 : 4),
        commit: (v) => v !== null && this.store.edit("edit secure area", (dd) => {
          if (dd.secure_area) dd.secure_area[k] = +clamp01(v / scale).toFixed(4);
        }, ["secure_area"]),
      }));
    }
    d.appendChild(grid);
    d.appendChild(el("div", { class: "me-actions" },
      el("button", {
        class: "btn-neutral", text: "Clear secure area",
        onclick: () => {
          this.store.edit("clear secure area", (dd) => { dd.secure_area = null; },
            ["secure_area"]);
          this.tools.select(null);
        },
      })));
  }

  _mapDetail() {
    const doc = this.store.doc;
    const d = this.detailEl;
    d.appendChild(el("div", { class: "me-sec-title", text: "Map" }));

    const grid = el("div", { class: "me-fields" });
    grid.appendChild(this._num({
      label: "map width", unit: "ft", min: 10, max: 100000, step: 5,
      value: doc.map_scale_ft,
      commit: (v) => this.store.edit("set map width", (dd) => {
        dd.map_scale_ft = v && v > 0 ? v : null;
      }, ["map_scale_ft"]),
    }));
    if (doc.floorplan) {
      grid.appendChild(this._num({
        label: "rotate plan", unit: "°", min: -360, max: 360, step: 0.5,
        value: doc.floorplan.rotation_deg,
        commit: (v) => this.store.edit("rotate floorplan", (dd) => {
          const r = ((parseFloat(v) || 0) % 360 + 360) % 360;
          if (r) dd.floorplan.rotation_deg = +r.toFixed(1);
          else delete dd.floorplan.rotation_deg;
        }, ["floorplan"]),
      }));
    }
    grid.appendChild(this._select({
      label: "grid",
      value: String(this.renderer.gridFt() || ""),
      options: [
        { value: "", label: "off" },
        { value: "1", label: "1 ft" }, { value: "2", label: "2 ft" },
        { value: "5", label: "5 ft" }, { value: "10", label: "10 ft" },
      ],
      commit: (v) => this.renderer.setGridFt(parseFloat(v) || 0),
    }));
    d.appendChild(grid);

    const dim = el("input", {
      type: "range", min: "0", max: "0.8", step: "0.05",
      value: String(this.renderer.dimFloorplan),
    });
    dim.addEventListener("input", () => this.renderer.setDim(parseFloat(dim.value)));
    d.appendChild(el("label", { class: "me-field me-dim" },
      el("span", { class: "me-field-label", text: "dim plan" }), dim));

    // Floorplan upload / remove.
    const file = el("input", {
      type: "file", accept: "image/png,image/jpeg,image/webp",
      style: "display:none",
    });
    file.addEventListener("change", () => this._uploadFloorplan(file));
    d.appendChild(file);
    d.appendChild(el("div", { class: "me-actions" },
      el("button", {
        class: "btn-neutral",
        text: doc.floorplan ? "Replace floorplan…" : "Upload floorplan…",
        onclick: () => file.click(),
      }),
      doc.floorplan ? el("button", {
        class: "btn-neutral", text: "Remove floorplan",
        onclick: () => this.store.edit("remove floorplan", (dd) => {
          dd.floorplan = null;
        }, ["floorplan"]),
      }) : null,
      el("button", {
        class: "btn-neutral", text: "Reload Frigate config",
        title: "Re-sync camera and zone names from Frigate",
        onclick: async (ev) => {
          const btn = ev.target;
          btn.disabled = true;
          try {
            await window.SC.fetchJson("/v1/push/frigate-config/refresh", { method: "POST" });
            await this.store.reload();
          } catch (e) {
            this.flash("config reload failed: " + e.message);
          }
          btn.disabled = false;
        },
      })));
    d.appendChild(el("p", {
      class: "help me-hint",
      text: "Select a camera to edit it. Drag its dot to move, the knob to aim, " +
        "a wedge edge to widen. Shift bypasses grid snap, Alt bypasses angle snap.",
    }));
  }

  async _uploadFloorplan(fileInput) {
    const f = fileInput.files && fileInput.files[0];
    if (!f) return;
    fileInput.value = "";
    let resp;
    try {
      resp = await fetch("/v1/push/floorplan", {
        method: "POST",
        headers: { "Content-Type": f.type || "application/octet-stream" },
        body: f,
      });
    } catch (e) {
      this.flash("upload failed: " + e.message);
      return;
    }
    if (!resp.ok) {
      this.flash("upload failed: HTTP " + resp.status);
      return;
    }
    // The upload endpoint writes floorplan metadata server-side; re-pull so
    // doc/savedDoc agree with it.
    await this.store.reload();
  }

  flash(msg) {
    this._flashMsg = msg;
    this.renderSaveBar();
    clearTimeout(this._flashTimer);
    this._flashTimer = setTimeout(() => {
      this._flashMsg = null;
      this.renderSaveBar();
    }, 6000);
  }

  // ---- Save bar --------------------------------------------------------

  renderSaveBar() {
    const s = this.store;
    const bar = this.saveBar;
    bar.textContent = "";
    const dirty = s.dirty();
    bar.classList.toggle("dirty", dirty);

    const undoBtn = el("button", {
      class: "btn-neutral me-undo",
      text: s.canUndo() ? `Undo ${s.undoLabel()}` : "Undo",
      onclick: () => s.undo(),
    });
    undoBtn.disabled = !s.canUndo();
    const redoBtn = el("button", {
      class: "btn-neutral me-undo",
      text: s.canRedo() ? `Redo ${s.redoLabel()}` : "Redo",
      onclick: () => s.redo(),
    });
    redoBtn.disabled = !s.canRedo();
    bar.appendChild(el("div", { class: "me-undoredo" }, undoBtn, redoBtn));

    if (this._flashMsg) {
      bar.appendChild(el("div", { class: "me-savemsg", text: this._flashMsg }));
    }

    if (dirty) {
      const saveBtn = el("button", {
        class: "btn-primary", text: "Save",
        onclick: () => this.doSave(saveBtn),
      });
      bar.appendChild(el("div", { class: "me-saverow" },
        el("span", { class: "me-dirtymark", text: "Unsaved changes" }),
        saveBtn,
        el("button", {
          class: "btn-neutral", text: "Discard",
          onclick: async () => { await this.store.reload(); },
        })));
    }
  }

  async doSave(btn) {
    btn.disabled = true;
    const r = await this.store.save();
    btn.disabled = false;
    if (r.ok) { this.flash("Saved."); return; }
    if (r.conflict) { this._conflictDialog(); return; }
    this.flash("Save failed: " + r.error);
  }

  // Scale tool finished a reference line: ask its real-world length and
  // derive the map width. A diagonal line is fine — the aspect-corrected
  // length is what's equated to the entered feet.
  scaleDialog(line) {
    const overlay = el("div", { class: "me-overlay" });
    const input = el("input", { type: "number", min: "0.5", step: "0.5", class: "me-num" });
    const apply = () => {
      const ft = parseFloat(input.value);
      if (!ft || ft <= 0) return;
      overlay.remove();
      const a = mapAspect(this.store.doc);
      const dx = line.x1 - line.x0, dy = (line.y1 - line.y0) * a;
      const unitLen = Math.hypot(dx, dy);
      if (unitLen < 1e-6) return;
      this.store.edit("calibrate scale", (dd) => {
        dd.map_scale_ft = +(ft / unitLen).toFixed(1);
        if (dd.floorplan) {
          dd.floorplan.calibration = {
            x0: +line.x0.toFixed(4), y0: +line.y0.toFixed(4),
            x1: +line.x1.toFixed(4), y1: +line.y1.toFixed(4),
            length_ft: ft,
          };
        }
      }, ["map_scale_ft", "floorplan"]);
      this.flash(`Map width set to ${(this.store.doc.map_scale_ft || 0).toFixed(0)} ft.`);
    };
    input.addEventListener("keydown", (ev) => {
      ev.stopPropagation();
      if (ev.key === "Enter") apply();
      if (ev.key === "Escape") overlay.remove();
    });
    const box = el("div", { class: "me-dialog" },
      el("p", { text: "How long is the line you just drew, in feet?" }),
      el("div", { class: "me-actions" },
        input,
        el("button", { class: "btn-primary", text: "Set scale", onclick: apply }),
        el("button", {
          class: "btn-neutral", text: "Cancel",
          onclick: () => overlay.remove(),
        })));
    overlay.appendChild(box);
    document.body.appendChild(overlay);
    input.focus();
  }

  _conflictDialog() {
    const overlay = el("div", { class: "me-overlay" });
    const box = el("div", { class: "me-dialog" },
      el("p", { text: "Settings changed elsewhere while you were editing." }),
      el("div", { class: "me-actions" },
        el("button", {
          class: "btn-neutral", text: "Reload & lose my edits",
          onclick: async () => { overlay.remove(); await this.store.reload(); },
        }),
        el("button", {
          class: "btn-primary", text: "Overwrite with mine",
          onclick: async () => {
            overlay.remove();
            const r = await this.store.save({ force: true });
            this.flash(r.ok ? "Saved." : "Save failed: " + (r.error || "conflict"));
          },
        })));
    overlay.appendChild(box);
    document.body.appendChild(overlay);
  }
}
