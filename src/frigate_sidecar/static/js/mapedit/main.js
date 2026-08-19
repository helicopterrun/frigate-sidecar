// Bootstrap for the CAD-style /cameras map editor.

import { Store } from "./state.js";
import { MapView } from "./view.js";
import { Renderer } from "./renderer.js";
import { Tools } from "./tools.js";
import { Inspector } from "./inspector.js";

const stage = document.getElementById("me-stage");
const panel = document.getElementById("me-panel");
const toolbarEl = document.getElementById("me-toolbar");
const banner = document.getElementById("me-banner");

const store = new Store();
const view = new MapView(stage, () => store.doc);
const renderer = new Renderer(stage, store, view);

let inspector = null;
const tools = new Tools(store, view, renderer, {
  onSelect: (sel) => {
    if (inspector) inspector.setSelection(sel);
    // On a phone, selecting something on the map is a request to edit it:
    // raise the sheet out of peek so the detail form is visible.
    if (sel && isPhone() && sheetState === "peek") setSheet("half");
  },
  onToolChange: (name) => {
    syncToolbar(name);
    if (name !== "landmark" && inspector?.landmark) inspector.cancelLandmark();
  },
  onScaleLine: (line) => inspector && inspector.scaleDialog(line),
  onLandmarkMapClick: (p) => inspector && inspector.landmarkMapClick(p),
});
inspector = new Inspector(panel, store, view, renderer, tools);

// ---- Mobile bottom sheet -----------------------------------------------
// Under 820px the inspector becomes a bottom sheet over the map with three
// snap states. The handle drags it; a tap toggles peek <-> half. The classes
// are inert on desktop (all sheet CSS lives inside the media query).

const isPhone = () => window.matchMedia("(max-width: 820px)").matches;
const SHEET_STATES = ["peek", "half", "full"];
let sheetState = "half";

function setSheet(s) {
  sheetState = s;
  for (const st of SHEET_STATES) panel.classList.toggle("me-sheet-" + st, st === s);
}
setSheet("half");

const handle = document.createElement("div");
handle.className = "me-sheet-handle";
handle.appendChild(Object.assign(document.createElement("span"), { className: "me-sheet-pill" }));
panel.prepend(handle);

handle.addEventListener("pointerdown", (ev) => {
  ev.preventDefault();
  const frameH = panel.parentElement.getBoundingClientRect().height;
  const startH = panel.getBoundingClientRect().height;
  const startY = ev.clientY;
  let moved = false;
  panel.classList.add("me-sheet-dragging");
  const move = (mv) => {
    if (!moved && Math.abs(mv.clientY - startY) < 6) return;
    moved = true;
    const h = Math.min(frameH * 0.9, Math.max(64, startH + (startY - mv.clientY)));
    panel.style.height = h + "px";
  };
  const up = () => {
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", up);
    window.removeEventListener("pointercancel", up);
    panel.classList.remove("me-sheet-dragging");
    if (!moved) {
      setSheet(sheetState === "peek" ? "half" : "peek");
      return;
    }
    const f = panel.getBoundingClientRect().height / frameH;
    panel.style.height = "";
    setSheet(f < 0.28 ? "peek" : f < 0.65 ? "half" : "full");
  };
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up);
  window.addEventListener("pointercancel", up);
});

// ---- Toolbar -----------------------------------------------------------

const TOOL_BUTTONS = [
  { id: "select", label: "▲", title: "Select / move / aim (Esc)" },
  { id: "area", label: "▢", title: "Draw secure area (drag once)" },
  { id: "scale", label: "⇤⇥", title: "Calibrate scale: drag a line of known length" },
];
const ACTION_BUTTONS = [
  { id: "zoom-in", label: "+", title: "Zoom in", run: () => view.zoom(1 / 1.3) },
  { id: "zoom-out", label: "−", title: "Zoom out", run: () => view.zoom(1.3) },
  { id: "fit", label: "⤢", title: "Fit to plan (F, double-click)", run: () => view.fit() },
  { id: "full", label: "⛶", title: "Fullscreen map", run: () => view.toggleFull() },
];

const toolBtns = new Map();
for (const t of TOOL_BUTTONS) {
  const b = document.createElement("button");
  b.className = "me-toolbtn";
  b.textContent = t.label;
  b.title = t.title;
  b.addEventListener("click", () => tools.setTool(t.id));
  toolbarEl.appendChild(b);
  toolBtns.set(t.id, b);
}
toolbarEl.appendChild(Object.assign(document.createElement("div"), { className: "me-toolgap" }));
for (const a of ACTION_BUTTONS) {
  const b = document.createElement("button");
  b.className = "me-toolbtn";
  b.textContent = a.label;
  b.title = a.title;
  b.addEventListener("click", a.run);
  toolbarEl.appendChild(b);
}

function syncToolbar(active) {
  for (const [id, b] of toolBtns) b.classList.toggle("active", id === active);
}
syncToolbar("select");

// ---- Keyboard ----------------------------------------------------------

document.addEventListener("keydown", (ev) => {
  const mod = ev.metaKey || ev.ctrlKey;
  if (mod && ev.key.toLowerCase() === "z") {
    ev.preventDefault();
    if (ev.shiftKey) store.redo(); else store.undo();
    return;
  }
  if (ev.key === "Escape") {
    if (view.isFull() && !tools.drag) { view.toggleFull(); return; }
    tools.cancel();
    return;
  }
  if (ev.key.toLowerCase() === "f" && !mod) { view.fit(); return; }
  // Arrow nudges: one grid step (Shift = 5 steps) on the selected camera.
  const sel = renderer.selection;
  if (sel?.kind === "camera" && ev.key.startsWith("Arrow")) {
    const cam = sel.camera;
    const e = (store.doc.camera_layout || {})[cam];
    if (!e || e.locked || e.x === undefined) return;
    ev.preventDefault();
    const step = tools._gridStep() * (ev.shiftKey ? 5 : 1);
    const d = {
      ArrowUp: [0, -step], ArrowDown: [0, step],
      ArrowLeft: [-step, 0], ArrowRight: [step, 0],
    }[ev.key];
    if (!d) return;
    store.edit(`nudge ${cam}`, (doc) => {
      const en = doc.camera_layout[cam];
      en.x = +Math.min(1, Math.max(0, en.x + d[0])).toFixed(4);
      en.y = +Math.min(1, Math.max(0, en.y + d[1])).toFixed(4);
    }, ["camera_layout"]);
  }
});

window.addEventListener("beforeunload", (ev) => {
  if (store.dirty()) {
    ev.preventDefault();
    ev.returnValue = "";
  }
});

// ---- Boot --------------------------------------------------------------

(async () => {
  try {
    await store.load();
  } catch (e) {
    banner.style.display = "";
    banner.textContent = "Failed to load settings: " + e.message;
    return;
  }
  stage.classList.remove("skeleton");
  view.fit();
})();
