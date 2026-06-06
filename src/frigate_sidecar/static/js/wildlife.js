/* Wildlife trail-cam gallery — client-side poller for the Pi's wildlife-cam API.
 *
 * Talks to the API under a same-origin reverse-proxy prefix (default
 * `/wildlifecam`, set server-side via window.WILDLIFE_API_BASE). NPM injects the
 * X-API-Token for the mutating endpoints, so nothing secret lives here. Override
 * the base with ?api=<base> for LAN-direct testing (read endpoints are open;
 * controls need the proxy's token injection). Contract: wildlife-cam docs/API.md.
 */
(() => {
  "use strict";

  const API =
    new URLSearchParams(location.search).get("api") ||
    window.WILDLIFE_API_BASE ||
    "/wildlifecam";

  const LIMIT = 120; // snapshots to pull per refresh
  const POLL_MS = 45000; // ~1 frame/cam/min upstream; 45s keeps it fresh-ish
  // Stills mode expects newest_snapshot_age_s ≈ 60–90s; flag past 5 min.
  // Freshness is judged with the SERVER's age field, never the browser clock —
  // the Pi and this web server can have different clocks/timezones (per API.md).
  const STALE_AGE_S = 5 * 60;

  const STREAM_LABELS = {
    cam: "IMX415",
    cam2: "IMX708",
  };

  const $ = (id) => document.getElementById(id);
  const grid = $("wl-grid");
  const empty = $("wl-empty");
  const msg = $("wl-msg");

  let stream = "all"; // 'all' | 'cam' | 'cam2'
  let items = []; // last rendered snapshot list (for lightbox nav)
  let lbIndex = -1;
  let pollTimer = null;

  // ---- helpers ---------------------------------------------------------------

  const apiUrl = (path) => API.replace(/\/$/, "") + path;
  // Snapshot `url`/`raw_url` are relative to the API origin → prefix the base.
  const mediaUrl = (rel) => API.replace(/\/$/, "") + rel;

  function setMsg(text, kind) {
    msg.textContent = text || "";
    msg.className = "wl-msg" + (kind ? " " + kind : "");
  }

  function fmtBytes(n) {
    if (!n && n !== 0) return "—";
    const mb = n / (1024 * 1024);
    return mb >= 1 ? mb.toFixed(1) + " MB" : (n / 1024).toFixed(0) + " KB";
  }

  // Format a server-supplied age (seconds). We deliberately do NOT compute age
  // from the client clock — see STALE_AGE_S note and wildlife-cam docs/API.md.
  function fmtAgeSec(sec) {
    if (sec == null || isNaN(sec)) return "—";
    sec = Math.max(0, sec);
    if (sec < 90) return Math.round(sec) + "s ago";
    if (sec < 5400) return Math.round(sec / 60) + "m ago";
    if (sec < 172800) return Math.round(sec / 3600) + "h ago";
    return Math.round(sec / 86400) + "d ago";
  }

  function timeShort(iso) {
    // "2026-06-05T18:14:52" → "06-05 18:14:52"
    if (!iso) return "";
    const [d, t] = iso.split("T");
    return (d ? d.slice(5) + " " : "") + (t || "");
  }

  // ---- snapshots / gallery ---------------------------------------------------

  async function loadSnapshots() {
    const q = new URLSearchParams({ limit: String(LIMIT) });
    if (stream !== "all") q.set("stream", stream);
    let data;
    try {
      const res = await fetch(apiUrl("/api/snapshots?" + q.toString()), {
        cache: "no-store",
      });
      if (!res.ok) throw new Error("HTTP " + res.status);
      data = await res.json();
    } catch (err) {
      setMsg("Snapshot fetch failed: " + err.message, "err");
      return;
    }
    items = Array.isArray(data) ? data : [];
    renderGrid();
    $("wl-count").textContent =
      items.length + (stream === "all" ? "" : " · " + (STREAM_LABELS[stream] || stream));
    if (msg.classList.contains("err")) setMsg("");
  }

  function renderGrid() {
    grid.replaceChildren();
    empty.hidden = items.length > 0;
    const frag = document.createDocumentFragment();
    items.forEach((s, i) => {
      const tile = document.createElement("div");
      tile.className = "wl-tile";
      tile.tabIndex = 0;
      tile.addEventListener("click", () => openLightbox(i));
      tile.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          openLightbox(i);
        }
      });

      const img = document.createElement("img");
      img.className = "wl-thumb";
      img.loading = "lazy";
      img.src = mediaUrl(s.url);
      img.alt = `${s.stream} ${s.time_iso}`;

      const meta = document.createElement("div");
      meta.className = "wl-meta";
      meta.innerHTML =
        `<div class="wl-meta-row"><span class="wl-time">${timeShort(s.time_iso)}</span>` +
        `<span class="wl-chip ${s.night ? "night" : "day"}">${s.night ? "🌙 night" : "☀ day"}</span></div>` +
        `<div class="wl-meta-row"><span class="wl-chip cam">${STREAM_LABELS[s.stream] || s.stream}</span>` +
        `<span class="wl-exp">${s.exposure || ""} · g${s.gain || "?"}</span></div>`;

      tile.append(img, meta);
      frag.append(tile);
    });
    grid.append(frag);
  }

  // ---- lightbox --------------------------------------------------------------

  function openLightbox(i) {
    lbIndex = i;
    renderLightbox();
    $("wl-lightbox").hidden = false;
  }

  function renderLightbox() {
    const s = items[lbIndex];
    if (!s) return;
    $("wl-lb-img").src = mediaUrl(s.url);
    $("wl-lb-img").alt = `${s.stream} ${s.time_iso}`;
    const cap = $("wl-lb-cap");
    const raw = s.raw_url
      ? `<a href="${mediaUrl(s.raw_url)}" download>⬇ DNG (${fmtBytes(s.size)} jpg)</a>`
      : `<span>no raw</span>`;
    cap.innerHTML =
      `<span>${STREAM_LABELS[s.stream] || s.stream}</span>` +
      `<span>${s.time_iso}</span>` +
      `<span>${s.night ? "🌙 night" : "☀ day"} · ${s.exposure || "?"} · gain ${s.gain || "?"}</span>` +
      raw;
  }

  function closeLightbox() {
    $("wl-lightbox").hidden = true;
    lbIndex = -1;
  }

  function stepLightbox(d) {
    if (lbIndex < 0) return;
    lbIndex = (lbIndex + d + items.length) % items.length;
    renderLightbox();
  }

  // ---- status strip ----------------------------------------------------------

  async function loadStatus() {
    // Disk, capture-timer state, and freshness all come from /api/stats +
    // /api/streaming. Freshness uses the server's own newest_snapshot_age_s so
    // it's immune to clock/timezone skew between the Pi and this web server.
    try {
      const [statsRes, streamRes] = await Promise.all([
        fetch(apiUrl("/api/stats"), { cache: "no-store" }),
        fetch(apiUrl("/api/streaming"), { cache: "no-store" }),
      ]);
      if (statsRes.ok) {
        const st = await statsRes.json();
        $("wl-disk").textContent =
          st.disk_free_gb != null
            ? `${st.disk_free_gb.toFixed(0)} / ${st.disk_total_gb.toFixed(0)} GB`
            : "—";
        const el = $("wl-latest");
        const age = st.newest_snapshot_age_s;
        if (age != null) {
          el.textContent = fmtAgeSec(age);
          if (st.newest_snapshot_ts) {
            el.title = new Date(st.newest_snapshot_ts * 1000).toLocaleString();
          }
          el.className = "stat-value" + (age > STALE_AGE_S ? " stale" : "");
        } else {
          el.textContent = "—";
          el.className = "stat-value";
        }
      }
      if (streamRes.ok) {
        const sm = await streamRes.json();
        const el = $("wl-running");
        if (sm.streaming) {
          el.textContent = "live mode (stills paused)";
          el.className = "stat-value stale";
        } else {
          el.textContent = sm.snapshots_running ? "running" : "stopped";
          el.className = "stat-value" + (sm.snapshots_running ? "" : " bad");
        }
      }
    } catch (_err) {
      /* status is best-effort; the gallery itself surfaces fetch errors */
    }
  }

  // ---- controls --------------------------------------------------------------

  async function captureNow() {
    const btn = $("wl-capture");
    btn.disabled = true;
    setMsg("Capturing…");
    try {
      const res = await fetch(apiUrl("/api/capture"), { method: "POST" });
      const body = await res.json().catch(() => ({}));
      if (!res.ok || body.ok === false) {
        throw new Error(body.error || "HTTP " + res.status);
      }
      setMsg("Capture fired — refreshing…", "ok");
      // Give the Pi a moment to write JPEG+DNG+JSON before we re-poll.
      setTimeout(() => {
        loadSnapshots();
        loadStatus();
        setMsg("");
      }, 3500);
    } catch (err) {
      const hint =
        /40[13]/.test(err.message) ? " (token not injected at proxy?)" : "";
      setMsg("Capture failed: " + err.message + hint, "err");
    } finally {
      btn.disabled = false;
    }
  }

  const SETTING_FIELDS = [
    ["night_shutter_us", "number"],
    ["night_gain", "number"],
    ["lux_day", "number"],
    ["jpeg_quality", "number"],
    ["save_raw", "bool"],
  ];

  async function loadSettings() {
    try {
      const res = await fetch(apiUrl("/api/settings"), { cache: "no-store" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const s = await res.json();
      for (const [key, type] of SETTING_FIELDS) {
        const el = $("set-" + key);
        if (!el || s[key] == null) continue;
        if (type === "bool") el.checked = !!s[key];
        else el.value = s[key];
      }
      setMsg("");
    } catch (err) {
      setMsg("Could not load settings: " + err.message, "err");
    }
  }

  async function saveSettings(ev) {
    ev.preventDefault();
    const payload = {};
    for (const [key, type] of SETTING_FIELDS) {
      const el = $("set-" + key);
      if (!el) continue;
      if (type === "bool") payload[key] = el.checked;
      else if (el.value !== "") payload[key] = Number(el.value);
    }
    setMsg("Saving settings…");
    try {
      const res = await fetch(apiUrl("/api/settings"), {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const merged = await res.json();
      for (const [key, type] of SETTING_FIELDS) {
        const el = $("set-" + key);
        if (!el || merged[key] == null) continue;
        if (type === "bool") el.checked = !!merged[key];
        else el.value = merged[key];
      }
      setMsg("Settings saved — applies next capture.", "ok");
    } catch (err) {
      const hint = /40[13]/.test(err.message) ? " (token not injected at proxy?)" : "";
      setMsg("Save failed: " + err.message + hint, "err");
    }
  }

  // ---- polling ---------------------------------------------------------------

  function refreshAll() {
    loadSnapshots();
    loadStatus();
  }

  function startPolling() {
    stopPolling();
    if ($("wl-auto").checked && !document.hidden) {
      pollTimer = setInterval(refreshAll, POLL_MS);
    }
  }
  function stopPolling() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = null;
  }

  // ---- wiring ----------------------------------------------------------------

  function init() {
    // Camera filter segmented control
    $("wl-streamseg").addEventListener("click", (e) => {
      const btn = e.target.closest(".vbtn");
      if (!btn) return;
      stream = btn.dataset.stream;
      $("wl-streamseg")
        .querySelectorAll(".vbtn")
        .forEach((b) => b.classList.toggle("active", b === btn));
      loadSnapshots();
    });

    $("wl-refresh").addEventListener("click", refreshAll);
    $("wl-capture").addEventListener("click", captureNow);
    $("wl-auto").addEventListener("change", startPolling);

    // Settings panel
    const sform = $("wl-settings");
    $("wl-settings-toggle").addEventListener("click", () => {
      sform.hidden = !sform.hidden;
      if (!sform.hidden) loadSettings();
    });
    $("wl-settings-reload").addEventListener("click", loadSettings);
    sform.addEventListener("submit", saveSettings);

    // Lightbox
    $("wl-lb-close").addEventListener("click", closeLightbox);
    $("wl-lb-prev").addEventListener("click", () => stepLightbox(-1));
    $("wl-lb-next").addEventListener("click", () => stepLightbox(1));
    $("wl-lightbox").addEventListener("click", (e) => {
      if (e.target.id === "wl-lightbox") closeLightbox();
    });
    document.addEventListener("keydown", (e) => {
      if ($("wl-lightbox").hidden) return;
      if (e.key === "Escape") closeLightbox();
      else if (e.key === "ArrowLeft") stepLightbox(-1);
      else if (e.key === "ArrowRight") stepLightbox(1);
    });

    // Pause polling when the tab is hidden (be a good LAN citizen).
    document.addEventListener("visibilitychange", startPolling);

    refreshAll();
    startPolling();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
