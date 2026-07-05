"use strict";
// BOM builder client. One file drives both the project-list page (#bom-index)
// and the per-project builder page (#bom-app). Plain fetch + DOM, no framework.

function esc(v) {
  if (v === null || v === undefined) return "";
  return String(v).replace(/[&<>"]/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]
  ));
}

async function api(method, url, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(url, opts);
  let data = null;
  try { data = await res.json(); } catch (_) { /* no body */ }
  if (!res.ok) {
    const detail = data && data.detail ? JSON.stringify(data.detail) : res.statusText;
    throw new Error(detail);
  }
  return data;
}

function money(v, currency) {
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  if (Number.isNaN(n)) return esc(v);
  return `${n.toFixed(2)} ${currency || ""}`.trim();
}

// --- Project-list page -------------------------------------------------------

function initIndex() {
  document.querySelectorAll("tr.row-link").forEach((tr) => {
    tr.addEventListener("click", (e) => {
      if (e.target.tagName === "A") return;
      window.location.href = tr.dataset.href;
    });
  });

  const form = document.getElementById("new-project");
  if (!form) return;
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const payload = {};
    form.querySelectorAll("input[name], select[name]").forEach((el) => {
      if (el.value !== "") payload[el.name] = el.value;
    });
    try {
      const res = await api("POST", "/bom/projects", payload);
      window.location.href = `/bom/${res.project.slug}`;
    } catch (err) {
      alert("Could not create project: " + err.message);
    }
  });
}

// --- Per-project builder page ------------------------------------------------

function initProject() {
  const app = document.getElementById("bom-app");
  const slug = app.dataset.slug;
  const itemForm = document.getElementById("item-form");
  const body = document.getElementById("bom-items-body");
  const rollupEl = document.getElementById("bom-rollup");
  let items = [];
  let currency = "USD";

  function serializeItem() {
    const payload = { extra: {} };
    itemForm.querySelectorAll("input[name], select[name]").forEach((el) => {
      const name = el.name;
      if (!name || name === "__item_id") return;
      if (el.value === "") return; // omit empties so server defaults apply
      if (el.dataset.extra) payload.extra[name] = el.value;
      else payload[name] = el.value;
    });
    return payload;
  }

  function fillItem(item) {
    itemForm.querySelectorAll("input[name], select[name]").forEach((el) => {
      if (el.name === "__item_id") return;
      const v = item[el.name];
      el.value = v === null || v === undefined ? "" : v;
    });
    itemForm.querySelector("[name=__item_id]").value = item.id;
    document.getElementById("item-submit").textContent = "Save changes";
    document.getElementById("item-reset").hidden = false;
    const badge = document.getElementById("edit-badge");
    badge.textContent = "editing #" + item.item_no;
    badge.hidden = false;
    itemForm.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  function resetItem() {
    itemForm.reset();
    itemForm.querySelector("[name=__item_id]").value = "";
    document.getElementById("item-submit").textContent = "Add part";
    document.getElementById("item-reset").hidden = true;
    document.getElementById("edit-badge").hidden = true;
  }

  function renderRollup(r) {
    const cards = [
      ["Populated lines", r.populated_lines],
      ["Missing MPN", r.missing_mpn_lines],
      ["Missing DPN", r.missing_dpn_lines],
      ["Needs review", r.needs_review_lines],
      ["High/critical risk", r.high_risk_lines],
      ["Cost / assembly", money(r.estimated_cost_per_assembly, r.currency)],
      ["Extended buy cost", money(r.estimated_extended_buy_cost, r.currency)],
    ];
    rollupEl.innerHTML = cards.map(([label, val]) => `
      <div class="stat-card">
        <div class="stat-label">${esc(label)}</div>
        <div class="stat-value">${esc(val)}</div>
      </div>`).join("");
  }

  function reviewClass(status) {
    if (status === "Approved") return "ok";
    if (status === "Rejected" || status === "Blocked") return "noise";
    if (status === "Needs Review" || status === "Unchecked" || !status) return "warn";
    return "muted";
  }

  function renderTable() {
    body.innerHTML = "";
    const installed = (p) => ["YES", "OPT", "VAR"].includes((p || "").toUpperCase());
    items.forEach((it) => {
      const tr = document.createElement("tr");
      if (!installed(it.populate)) tr.classList.add("dnp");
      const dist = [it.preferred_distributor, it.preferred_dpn]
        .filter((x) => x && x !== "TBD").join(" / ") || "—";
      const qtyCell = it.quantity_check === "CHECK"
        ? `<span class="cell-class warn">${esc(it.qty_per_assembly)}</span>`
        : esc(it.qty_per_assembly);
      tr.innerHTML = `
        <td>${esc(it.item_no)}</td>
        <td>${esc(it.designator)}</td>
        <td>${qtyCell}</td>
        <td>${esc(it.part_category)}</td>
        <td>${esc(it.value)}</td>
        <td>${esc(it.mpn)}</td>
        <td>${esc(it.manufacturer)}</td>
        <td>${esc(dist)}</td>
        <td>${money(it.unit_cost, "")}</td>
        <td>${esc(it.buy_quantity)}</td>
        <td>${money(it.extended_line_cost, "")}</td>
        <td>${esc(it.lifecycle_status)}</td>
        <td><span class="cell-class ${reviewClass(it.review_status)}">${esc(it.review_status || "—")}</span></td>
        <td class="row-actions">
          <button type="button" class="link-btn" data-edit="${it.id}">edit</button>
          <button type="button" class="link-btn danger" data-del="${it.id}">del</button>
        </td>`;
      body.appendChild(tr);
    });
    body.querySelectorAll("[data-edit]").forEach((b) =>
      b.addEventListener("click", () => {
        const it = items.find((x) => x.id === Number(b.dataset.edit));
        if (it) fillItem(it);
      }));
    body.querySelectorAll("[data-del]").forEach((b) =>
      b.addEventListener("click", () => delItem(Number(b.dataset.del))));

    document.getElementById("bom-empty").hidden = items.length > 0;
    document.getElementById("item-count").textContent =
      items.length ? `(${items.length})` : "";
  }

  async function load() {
    const res = await api("GET", `/bom/${slug}/items`);
    items = res.items;
    currency = res.project.currency || "USD";
    renderRollup(res.rollup);
    renderTable();
  }

  async function delItem(id) {
    if (!confirm("Delete this line item?")) return;
    try {
      await api("DELETE", `/bom/${slug}/items/${id}`);
      await load();
    } catch (err) {
      alert("Delete failed: " + err.message);
    }
  }

  itemForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const id = itemForm.querySelector("[name=__item_id]").value;
    const payload = serializeItem();
    try {
      if (id) await api("PUT", `/bom/${slug}/items/${id}`, payload);
      else await api("POST", `/bom/${slug}/items`, payload);
      resetItem();
      await load();
    } catch (err) {
      alert("Save failed: " + err.message);
    }
  });
  document.getElementById("item-reset").addEventListener("click", resetItem);

  // Build-config editor
  const configForm = document.getElementById("config-form");
  configForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const payload = {};
    configForm.querySelectorAll("input[name], select[name]").forEach((el) => {
      if (el.value !== "") payload[el.name] = el.value;
    });
    try {
      await api("PUT", `/bom/${slug}/config`, payload);
      await load();
      alert("Build config saved.");
    } catch (err) {
      alert("Save failed: " + err.message);
    }
  });
  document.getElementById("delete-project").addEventListener("click", async () => {
    if (!confirm("Delete this whole BOM project and all its line items?")) return;
    try {
      await api("DELETE", `/bom/${slug}`);
      window.location.href = "/bom";
    } catch (err) {
      alert("Delete failed: " + err.message);
    }
  });

  // KiCad import
  document.getElementById("do-import").addEventListener("click", async () => {
    const text = document.getElementById("kicad-csv").value.trim();
    if (!text) { alert("Paste a KiCad CSV first."); return; }
    try {
      const res = await api("POST", `/bom/${slug}/import/kicad`, { csv_text: text });
      document.getElementById("kicad-csv").value = "";
      document.getElementById("import-panel").open = false;
      await load();
      alert(`Imported ${res.added} line(s).`);
    } catch (err) {
      alert("Import failed: " + err.message);
    }
  });

  // Panel toggles
  document.getElementById("toggle-import").addEventListener("click", () => {
    const p = document.getElementById("import-panel");
    p.open = !p.open;
    if (p.open) p.scrollIntoView({ behavior: "smooth", block: "nearest" });
  });
  document.getElementById("toggle-config").addEventListener("click", () => {
    const p = document.getElementById("config-panel");
    p.open = !p.open;
    if (p.open) p.scrollIntoView({ behavior: "smooth", block: "nearest" });
  });

  load().catch((err) => alert("Could not load BOM: " + err.message));
}

document.addEventListener("DOMContentLoaded", () => {
  if (document.getElementById("bom-index")) initIndex();
  if (document.getElementById("bom-app")) initProject();
});
