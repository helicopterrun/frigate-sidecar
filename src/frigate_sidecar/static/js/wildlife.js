/* Wildlife trail-cam page — client-side poller for the Pi's wildlife-cam API.
 *
 * Tab-based: Snapshots · Events · Recordings · Stacks · Live · Settings, with a
 * persistent status strip (PIR / disk / freshness) above the tabs.
 *
 * Talks to the read+control API under a same-origin reverse-proxy prefix
 * (default `/wildlifecam`, set server-side via window.WILDLIFE_API_BASE); NPM
 * injects the X-API-Token for mutating endpoints, so nothing secret lives here.
 * go2rtc (Live) is reached under a second same-origin prefix
 * (window.WILDLIFE_GO2RTC_BASE) so it works over HTTPS without mixed content.
 * Override either with ?api=<base> / ?go2rtc=<base> for LAN-direct testing.
 * Contract: wildlife-cam docs/API.md.
 */
(() => {
  "use strict";

  // ---- config & state --------------------------------------------------------

  const qs = new URLSearchParams(location.search);
  const API = qs.get("api") || window.WILDLIFE_API_BASE || "/wildlifecam";
  const GO2RTC = qs.get("go2rtc") || window.WILDLIFE_GO2RTC_BASE || "/wildlifecam-go2rtc/";

  const SNAP_LIMIT = 240; // snapshots to pull (covers several bursts)
  const EVENTS_LIMIT = 50;
  const REC_LIMIT = 60;
  const CONTENT_POLL_MS = 45000; // active content tab cadence
  const PIR_POLL_MS = 3000; // live sensor state — fast
  const STATUS_POLL_MS = 30000; // disk/freshness/streaming — slow, always on
  const STALE_AGE_S = 5 * 60; // flag the feed stale past 5 min (server-relative)

  const STREAM_LABELS = { cam: "IMX415", cam2: "IMX708" };
  const CAM_LABELS = { cam: "IMX415 · low-light", cam2: "IMX708 · autofocus" };

  let streams = ["cam", "cam2"]; // from /api/config
  let recStreams = ["cam"]; // streams that actually record (segments > 0)
  let snapStream = "all"; // Snapshots camera filter: 'all' | <stream>
  let activeTab = "snapshots";
  let liveBuilt = false;

  let snapGroups = []; // grouped snapshots for the Snapshots grid
  let clipEvents = []; // last fetched PIR events
  let lastClipsSig = "";
  let lastRecsSig = "";
  let lastStacksSig = "";
  const expandedRecs = new Set();

  let lbList = []; // lightbox: [{ src, alt, cap, stack? }]
  let lbIndex = -1;

  let pirDetected = false;
  let lastServerTs = null; // server wall-clock; ages computed against it, never Date.now()

  let contentTimer = null;
  let pirTimer = null;
  let statusTimer = null;

  const $ = (id) => document.getElementById(id);
  const msg = () => $("wl-msg");

  // ---- shared helpers --------------------------------------------------------

  const apiUrl = (path) => API.replace(/\/$/, "") + path;
  const mediaUrl = (rel) => API.replace(/\/$/, "") + rel; // /snap, /media are API-relative
  // Event clips & recording segments stream through the SIDECAR proxies (not the
  // API base): NPM doesn't reliably forward those media paths.
  const clipUrl = (p) => (p ? "/wildlife/media/" + p.replace(/^\/+media\/+/, "") : "");
  const posterUrl = (p) => (p ? "/wildlife/poster/" + p.replace(/^\/+media\/+/, "") : "");
  const stackedUrl = (p) => (p ? "/wildlife/stacked/" + p.replace(/^\/+stacked\/+/, "") : "");
  const go2rtcUrl = (src) =>
    GO2RTC.replace(/\/$/, "") + "/stream.html?src=" + encodeURIComponent(src);

  function setMsg(text, kind) {
    const m = msg();
    m.textContent = text || "";
    m.className = "wl-msg" + (kind ? " " + kind : "");
  }

  function fmtBytes(n) {
    if (!n && n !== 0) return "—";
    const mb = n / (1024 * 1024);
    return mb >= 1 ? mb.toFixed(1) + " MB" : (n / 1024).toFixed(0) + " KB";
  }

  // Format a SERVER-supplied age (seconds). Never compute age from the client
  // clock — the Pi and this web server can disagree on time/zone (per API.md).
  function fmtAgeSec(sec) {
    if (sec == null || isNaN(sec)) return "—";
    sec = Math.max(0, sec);
    if (sec < 90) return Math.round(sec) + "s ago";
    if (sec < 5400) return Math.round(sec / 60) + "m ago";
    if (sec < 172800) return Math.round(sec / 3600) + "h ago";
    return Math.round(sec / 86400) + "d ago";
  }

  function timeShort(iso) {
    if (!iso) return "";
    const [d, t] = iso.split("T");
    return (d ? d.slice(5) + " " : "") + (t ? t.slice(0, 8) : "");
  }

  function fmtEventTime(ts) {
    if (ts == null) return "";
    const local = new Date(ts * 1000).toLocaleString([], {
      month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit",
    });
    if (lastServerTs != null) return `${local} · ${fmtAgeSec(lastServerTs - ts)}`;
    return local;
  }

  function fmtLux(v) {
    if (v == null || isNaN(v)) return "—";
    if (v < 1) return v.toFixed(2);
    if (v < 100) return v.toFixed(1);
    return Math.round(v).toLocaleString();
  }

  const streamLabel = (s) => STREAM_LABELS[s] || s || "";
  const camLabel = (s) => CAM_LABELS[s] || s || "event";

  // Extract a YYYYMMDD_HHMMSS event id from a snap/still path or filename.
  function eventIdFrom(url) {
    const m = /snap_(\d{8})_(\d{6})/.exec(url || "");
    return m ? `${m[1]}_${m[2]}` : null;
  }
  function streamFromStill(url, mode) {
    const m = /\/snap\/([^/]+)\//.exec(url || "");
    if (m) return m[1];
    return mode === "night" ? "cam" : mode === "day" ? "cam2" : null;
  }

  // ---- lightbox --------------------------------------------------------------

  function openLightbox(list, i) {
    lbList = Array.isArray(list) ? list : [];
    lbIndex = i;
    renderLightbox();
    $("wl-lightbox").hidden = false;
  }
  function renderLightbox() {
    const it = lbList[lbIndex];
    if (!it) return;
    $("wl-lb-img").src = it.src;
    $("wl-lb-img").alt = it.alt || "";
    const cap = $("wl-lb-cap");
    cap.innerHTML = it.cap || "";
    if (it.stack) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "wl-btn wl-lb-stack";
      b.textContent = "✨ Recover / Stack";
      b.addEventListener("click", (e) => {
        e.stopPropagation();
        requestStack({ stream: it.stack.stream, event: it.stack.event, method: "mean", trigger: b });
      });
      cap.append(b);
    }
  }
  function closeLightbox() {
    $("wl-lightbox").hidden = true;
    lbIndex = -1;
  }
  function stepLightbox(d) {
    if (lbIndex < 0 || !lbList.length) return;
    lbIndex = (lbIndex + d + lbList.length) % lbList.length;
    renderLightbox();
  }

  // ---- status strip ----------------------------------------------------------

  async function loadStatus() {
    try {
      const [statsRes, streamRes] = await Promise.all([
        fetch(apiUrl("/api/stats"), { cache: "no-store" }),
        fetch(apiUrl("/api/streaming"), { cache: "no-store" }),
      ]);
      if (statsRes.ok) {
        const st = await statsRes.json();
        if (st.server_ts != null) lastServerTs = st.server_ts;
        $("wl-disk").textContent =
          st.disk_free_gb != null
            ? `${st.disk_free_gb.toFixed(0)} / ${st.disk_total_gb.toFixed(0)} GB`
            : "—";
        const el = $("wl-latest");
        const age = st.newest_snapshot_age_s;
        if (age != null) {
          el.textContent = fmtAgeSec(age);
          if (st.newest_snapshot_ts) el.title = new Date(st.newest_snapshot_ts * 1000).toLocaleString();
          el.className = "stat-value" + (age > STALE_AGE_S ? " stale" : "");
        } else {
          el.textContent = "—";
          el.className = "stat-value";
        }
      }
      if (streamRes.ok) {
        const sm = await streamRes.json();
        // keep the Live toggle in sync if it's around
        const tgl = $("wl-live-toggle");
        if (tgl && document.activeElement !== tgl) tgl.checked = !!sm.streaming;
      }
    } catch (_err) {
      /* best-effort; the active tab surfaces its own fetch errors */
    }
  }

  async function loadPir() {
    const dot = $("wl-pir-dot");
    const text = $("wl-pir-text");
    const sub = $("wl-pir-sub");
    const card = $("wl-motion-card");
    let p;
    try {
      const res = await fetch(apiUrl("/api/pir"), { cache: "no-store" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      p = await res.json();
    } catch (_err) {
      dot.className = "wl-pir-dot offline";
      text.textContent = "unavailable";
      text.className = "wl-pir-text-offline";
      sub.textContent = "";
      card.classList.remove("detected");
      pirDetected = false;
      return;
    }

    if (p.server_ts != null) lastServerTs = p.server_ts;
    const offline = !p.available || !p.service_active;
    const detected = !!p.any_detected && !offline;

    dot.className = "wl-pir-dot " + (offline ? "offline" : detected ? "detected" : "clear");
    card.classList.toggle("detected", detected);
    if (offline) {
      text.textContent = p.available ? "service down" : "no sensor";
      text.className = "wl-pir-text-offline";
    } else {
      text.textContent = detected ? "Motion" : "Clear";
      text.className = detected ? "wl-pir-text-detected" : "";
    }

    // Per-pin state, now with the sensor type (pir / mmwave).
    const pins = Array.isArray(p.pins) ? p.pins : [];
    const pinStr = pins
      .map((pin) => `${pin.sensor || "gpio" + pin.gpio}(${pin.gpio}) ${pin.detected ? "●" : "○"}`)
      .join("   ");
    let lastStr = "";
    if (p.last_event_ts != null) {
      const ref = lastServerTs != null ? lastServerTs : p.server_ts;
      if (ref != null) {
        lastStr = `last ${fmtAgeSec(ref - p.last_event_ts)}` +
          (p.last_event_pin != null ? ` (${p.last_event_pin})` : "");
      }
    }
    sub.textContent = [pinStr, lastStr].filter(Boolean).join("   ·   ");

    // Clear→detected edge: a trigger just landed → pull events shortly.
    if (detected && !pirDetected) setTimeout(() => loadEvents(true), 1200);
    pirDetected = detected;
  }

  // Live ambient lux + day/night mode (orchestrator metering loop, ~20–60s).
  async function loadLight() {
    const el = $("wl-lux");
    const sub = $("wl-lux-sub");
    let p;
    try {
      const res = await fetch(apiUrl("/api/light"), { cache: "no-store" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      p = await res.json();
    } catch (_err) {
      el.textContent = "—";
      el.className = "stat-value wl-lux";
      sub.textContent = "";
      return;
    }
    if (p.server_ts != null) lastServerTs = p.server_ts;
    if (!p.available || p.lux == null) {
      el.textContent = "warming up";
      el.className = "stat-value wl-lux";
      sub.textContent = "";
      return;
    }
    const night = p.mode === "night";
    const stale = p.age_s != null && p.age_s > 180; // expect 0–60s; flag if stuck
    el.innerHTML =
      `${fmtLux(p.lux)} <span class="wl-lux-unit">lux</span>` +
      `<span class="wl-chip ${night ? "night" : "day"} wl-lux-mode">${night ? "🌙 night" : "☀ day"}</span>`;
    el.className = "stat-value wl-lux" + (stale ? " stale" : "");
    // Where the current lux sits on the day↔night band + server-relative freshness.
    const bits = [];
    if (p.night_enter_lux != null && p.day_return_lux != null)
      bits.push(`night<${p.night_enter_lux} / day>${p.day_return_lux}`);
    if (p.age_s != null) bits.push(fmtAgeSec(p.age_s));
    sub.textContent = bits.join("   ·   ");
  }

  // Status-strip "Events · 24h": count in the last 24h + most-recent age. Ages
  // use the server clock (lastServerTs), never the browser clock.
  function updateEventsStat(events) {
    if (!Array.isArray(events)) return;
    const el = $("wl-events-stat");
    const sub = $("wl-events-stat-sub");
    const ref = lastServerTs;
    let count = events.length;
    let latest = null;
    if (ref != null) {
      count = 0;
      events.forEach((e) => {
        if (e.start_time == null) return;
        if (ref - e.start_time <= 86400) count++;
        if (latest == null || e.start_time > latest) latest = e.start_time;
      });
    }
    el.textContent = String(count);
    sub.textContent =
      latest != null && ref != null ? "last " + fmtAgeSec(ref - latest) : events.length ? "" : "none yet";
  }

  async function loadEventsStat() {
    try {
      const res = await fetch(apiUrl("/api/events?type=pir&limit=200"), { cache: "no-store" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      updateEventsStat(await res.json());
    } catch (_err) {
      /* best-effort, like the rest of the status strip */
    }
  }

  // ---- tabs controller -------------------------------------------------------

  const TABS = {
    snapshots: { load: loadSnapshots, poll: true },
    events: { load: () => loadEvents(false), poll: true },
    recordings: { load: loadRecordings, poll: true },
    stacks: { load: loadStacks, poll: false },
    live: { load: activateLive, leave: teardownLive, poll: false },
    settings: { load: loadSettings, poll: false },
  };

  function activateTab(name, fromHash) {
    if (!TABS[name]) name = "snapshots";
    if (name === activeTab && fromHash) return;
    const prev = TABS[activeTab];
    if (prev && prev.leave) prev.leave();

    activeTab = name;
    document.querySelectorAll(".wl-tabpanel").forEach((p) => {
      p.hidden = p.id !== "tab-" + name;
    });
    $("wl-tabs").querySelectorAll(".vbtn").forEach((b) => {
      b.classList.toggle("active", b.dataset.tab === name);
      b.setAttribute("aria-selected", b.dataset.tab === name ? "true" : "false");
    });
    if (location.hash.slice(1) !== name) history.replaceState(null, "", "#" + name);

    setMsg("");
    TABS[name].load();
    startContentPolling();
  }

  function startContentPolling() {
    stopContentPolling();
    const tab = TABS[activeTab];
    if (tab && tab.poll && $("wl-auto").checked && !document.hidden) {
      contentTimer = setInterval(() => TABS[activeTab].load(), CONTENT_POLL_MS);
    }
  }
  function stopContentPolling() {
    if (contentTimer) clearInterval(contentTimer);
    contentTimer = null;
  }

  function startAmbientPolling() {
    stopAmbientPolling();
    if ($("wl-auto").checked && !document.hidden) {
      pirTimer = setInterval(loadPir, PIR_POLL_MS);
      statusTimer = setInterval(() => { loadStatus(); loadLight(); loadEventsStat(); }, STATUS_POLL_MS);
    }
  }
  function stopAmbientPolling() {
    if (pirTimer) clearInterval(pirTimer);
    if (statusTimer) clearInterval(statusTimer);
    pirTimer = statusTimer = null;
  }

  function onAutoOrVisibilityChange() {
    startAmbientPolling();
    startContentPolling();
  }

  // ---- Snapshots tab ---------------------------------------------------------

  function buildStreamFilter() {
    const seg = $("wl-streamseg");
    seg.replaceChildren();
    const mk = (val, label, active) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "vbtn" + (active ? " active" : "");
      b.dataset.stream = val;
      b.textContent = label;
      return b;
    };
    seg.append(mk("all", "Both", snapStream === "all"));
    streams.forEach((s) => seg.append(mk(s, CAM_LABELS[s] || s, snapStream === s)));
  }

  async function loadSnapshots() {
    const q = new URLSearchParams({ limit: String(SNAP_LIMIT) });
    if (snapStream !== "all") q.set("stream", snapStream);
    let data;
    try {
      const res = await fetch(apiUrl("/api/snapshots?" + q.toString()), { cache: "no-store" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      data = await res.json();
    } catch (err) {
      if (activeTab === "snapshots") setMsg("Snapshot fetch failed: " + err.message, "err");
      return;
    }
    const items = Array.isArray(data) ? data : [];
    snapGroups = groupByEvent(items);
    $("wl-snap-count").textContent =
      `${items.length} frames · ${snapGroups.length} events` +
      (snapStream === "all" ? "" : " · " + streamLabel(snapStream));
    renderSnapshots();
    if (msg().classList.contains("err")) setMsg("");
  }

  // Group adjacent same-(stream,event) frames; legacy items with no event are
  // singletons. Newest-first order is preserved (API sorts by ts desc).
  function groupByEvent(items) {
    const groups = [];
    const byKey = new Map();
    items.forEach((s) => {
      const key = s.event ? s.stream + "|" + s.event : "_" + groups.length;
      let g = byKey.get(key);
      if (!g) {
        g = { key, stream: s.stream, event: s.event, night: s.night, frames: [] };
        byKey.set(key, g);
        groups.push(g);
      }
      g.frames.push(s);
    });
    return groups;
  }

  function renderSnapshots() {
    const grid = $("wl-snap-grid");
    grid.replaceChildren();
    $("wl-snap-empty").hidden = snapGroups.length > 0;
    const frag = document.createDocumentFragment();
    snapGroups.forEach((g, gi) => frag.append(buildSnapTile(g, gi)));
    grid.append(frag);
  }

  function buildSnapTile(g, gi) {
    const lead = g.frames[0];
    const tile = document.createElement("div");
    tile.className = "wl-tile";
    tile.tabIndex = 0;
    const open = () => openLightbox(groupLbList(g), 0);
    tile.addEventListener("click", open);
    tile.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
    });

    const fig = document.createElement("div");
    fig.className = "wl-clipfig";
    const img = document.createElement("img");
    img.className = "wl-thumb";
    img.loading = "lazy";
    img.src = mediaUrl(lead.url);
    img.alt = `${lead.stream} ${lead.time_iso}`;
    fig.append(img);
    if (g.frames.length > 1) {
      const badge = document.createElement("span");
      badge.className = "wl-countbadge";
      badge.textContent = "×" + g.frames.length;
      fig.append(badge);
    }
    tile.append(fig);

    const meta = document.createElement("div");
    meta.className = "wl-meta";
    meta.innerHTML =
      `<div class="wl-meta-row"><span class="wl-time">${timeShort(lead.time_iso)}</span>` +
      `<span class="wl-chip ${g.night ? "night" : "day"}">${g.night ? "🌙 night" : "☀ day"}</span></div>` +
      `<div class="wl-meta-row"><span class="wl-chip cam">${streamLabel(lead.stream)}</span>` +
      `<span class="wl-exp">${lead.exposure || ""} · g${lead.gain || "?"}${lead.raw_url ? " · RAW" : ""}</span></div>`;
    tile.append(meta);
    return tile;
  }

  // Lightbox list for one snapshot group; night groups carry a stack action.
  function groupLbList(g) {
    const stack = g.night && g.event ? { stream: g.stream, event: g.event } : null;
    return g.frames.map((s, k) => ({
      src: mediaUrl(s.url),
      alt: `${s.stream} ${s.time_iso}`,
      stack,
      cap:
        `<span>${camLabel(s.stream)}</span>` +
        `<span>${s.time_iso}</span>` +
        `<span>frame ${k + 1} / ${g.frames.length}</span>` +
        `<span>${s.night ? "🌙 night" : "☀ day"} · ${s.exposure || "?"} · gain ${s.gain || "?"}</span>` +
        (s.raw_url ? `<a href="${mediaUrl(s.raw_url)}" download>⬇ DNG</a>` : ""),
    }));
  }

  // ---- Events tab (PIR clips) ------------------------------------------------

  // `silent` = fetched in the background (e.g. PIR edge); don't surface errors.
  async function loadEvents(silent) {
    let evts;
    try {
      const q = new URLSearchParams({ type: "pir", limit: String(EVENTS_LIMIT) });
      const res = await fetch(apiUrl("/api/events?" + q.toString()), { cache: "no-store" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      evts = await res.json();
    } catch (err) {
      if (!silent && activeTab === "events") setMsg("Events fetch failed: " + err.message, "err");
      return;
    }
    clipEvents = Array.isArray(evts) ? evts : [];
    updateEventsStat(clipEvents); // keep the status-strip card fresh while browsing
    const sig = clipEvents.map((e) => e.id).join(",");
    const changed = sig !== lastClipsSig;
    lastClipsSig = sig;
    $("wl-events-count").textContent = clipEvents.length ? `${clipEvents.length} events` : "";
    if (changed && (activeTab === "events" || !silent)) renderEvents();
    if (!silent && msg().classList.contains("err")) setMsg("");
  }

  function renderEvents() {
    const grid = $("wl-events-grid");
    grid.replaceChildren();
    $("wl-events-empty").hidden = clipEvents.length > 0;
    const frag = document.createDocumentFragment();
    clipEvents.forEach((e) => frag.append(buildClipTile(e)));
    grid.append(frag);
  }

  function parseEventData(e) {
    try { return e && e.data ? JSON.parse(e.data) : {}; }
    catch (_err) { return {}; }
  }

  function buildClipTile(e) {
    const d = parseEventData(e);
    const mode = d.mode || (e.thumbnail && e.thumbnail.includes("/snap/cam2/") ? "day" : "");
    const stills = Array.isArray(d.stills) && d.stills.length ? d.stills : e.thumbnail ? [e.thumbnail] : [];
    const cam = streamFromStill(stills[0] || e.thumbnail, mode);

    const tile = document.createElement("div");
    tile.className = "wl-tile wl-cliptile";
    tile.tabIndex = 0;
    const open = () => openEventModal(e, d, stills, cam, mode);
    tile.addEventListener("click", open);
    tile.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); open(); }
    });

    const fig = document.createElement("div");
    fig.className = "wl-clipfig";
    const posterSrc = e.thumbnail || stills[0];
    if (posterSrc) {
      const img = document.createElement("img");
      img.className = "wl-thumb";
      img.loading = "lazy";
      img.src = mediaUrl(posterSrc);
      img.alt = mode || "event";
      fig.append(img);
    } else {
      const ph = document.createElement("div");
      ph.className = "wl-thumb placeholder";
      ph.textContent = "◇";
      fig.append(ph);
    }
    const hasVideo = !!(d.burst_video || (e.has_clip && e.clip_path));
    const play = document.createElement("span");
    play.className = "wl-clipplay";
    play.textContent = hasVideo ? "▶" : "🖼";
    fig.append(play);
    tile.append(fig);

    const meta = document.createElement("div");
    meta.className = "wl-meta";
    const stillsTxt = stills.length ? `${stills.length} still${stills.length === 1 ? "" : "s"}` : "";
    meta.innerHTML =
      `<div class="wl-meta-row"><span class="wl-time">${fmtEventTime(e.start_time)}</span>` +
      `<span class="wl-chip ${mode === "night" ? "night" : "day"}">${mode === "night" ? "🌙 night" : "☀ day"}</span></div>` +
      `<div class="wl-meta-row"><span class="wl-chip cam">${streamLabel(cam)}</span>` +
      `<span class="wl-exp">${stillsTxt}</span></div>`;
    tile.append(meta);
    return tile;
  }

  // ---- event detail overlay --------------------------------------------------

  function openEventModal(e, d, stills, cam, mode) {
    const badge = mode === "night" ? "🌙 night" : "☀ day";
    $("wl-eb-title").textContent = `${badge} · ${camLabel(cam)} · ${fmtEventTime(e.start_time)}`;
    buildEventDetail($("wl-eb-body"), e, d, stills, cam, mode);
    $("wl-eventbox").hidden = false;
  }
  function closeEventModal() {
    $("wl-eventbox").hidden = true;
    $("wl-eb-body").replaceChildren(); // tears down any playing media
  }

  function buildEventDetail(detail, e, d, stills, cam, mode) {
    detail.replaceChildren();
    const hasClip = !!(e.has_clip && e.clip_path);

    if (d.burst_video) detail.append(buildVideoPlayer(d.burst_video, false, true));
    else if (hasClip) detail.append(buildVideoPlayer(e.clip_path));

    if (d.burst_video && hasClip)
      detail.append(secondaryPlayback("▶ Play clip with sound", () => buildVideoPlayer(e.clip_path)));
    if (d.audio)
      detail.append(secondaryPlayback("🔊 Play audio", () => buildAudioPlayer(d.audio)));

    if (mode === "night" && d.metering) detail.append(buildMetering(d));

    // Night events can be recovered (stacked). Derive the event id from a still.
    const event = eventIdFrom(stills[0] || e.thumbnail);
    if (mode === "night" && cam && event) detail.append(buildStackAction(cam, event));

    if (!d.burst_video && !hasClip) {
      const note = document.createElement("p");
      note.className = "wl-evt-note";
      note.textContent = "No video for this event.";
      detail.append(note);
    }
    detail.append(buildStillsGallery(e, d, stills, cam, mode));
  }

  function buildStackAction(stream, event) {
    const wrap = document.createElement("div");
    wrap.className = "wl-evt-stackbar";
    const sel = document.createElement("select");
    sel.className = "wl-stack-method-inline";
    [["mean", "mean"], ["median", "median"], ["max", "max"]].forEach(([v, t]) => {
      const o = document.createElement("option");
      o.value = v; o.textContent = t; sel.append(o);
    });
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "wl-btn wl-btn-accent";
    btn.textContent = "✨ Recover / Stack";
    btn.addEventListener("click", () =>
      requestStack({ stream, event, method: sel.value, trigger: btn }));
    const hint = document.createElement("span");
    hint.className = "wl-hint";
    hint.textContent = "low-light recovery · ~1 min for raw";
    wrap.append(btn, sel, hint);
    return wrap;
  }

  function secondaryPlayback(label, makePlayer) {
    const wrap = document.createElement("div");
    wrap.className = "wl-evt-secondary";
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "wl-btn wl-evt-secbtn";
    btn.textContent = label;
    btn.addEventListener("click", () => btn.replaceWith(makePlayer()), { once: true });
    wrap.append(btn);
    return wrap;
  }

  function buildStillsGallery(e, d, stills, cam, mode) {
    const frag = document.createDocumentFragment();
    if (!stills.length) return frag;
    const pre = mode === "night" && d.pre_frames > 0 ? d.pre_frames : 0;
    if (pre) frag.append(mediaLabel(`Stills · first ${pre} are pre-trigger`));
    const gal = document.createElement("div");
    gal.className = "wl-evt-stills";
    const lb = stills.map((u, k) => ({
      src: mediaUrl(u),
      alt: `${mode || "event"} still ${k + 1}/${stills.length}`,
      cap:
        `<span>${camLabel(cam)}</span>` +
        `<span>frame ${k + 1} / ${stills.length}${k < pre ? " · pre-trigger" : ""}</span>` +
        `<span>${fmtEventTime(e.start_time)}</span>`,
    }));
    stills.forEach((u, k) => {
      const img = document.createElement("img");
      img.className = "wl-evt-still" + (k < pre ? " pre" : "");
      img.loading = "lazy";
      img.src = mediaUrl(u);
      img.alt = `still ${k + 1}`;
      img.addEventListener("click", () => openLightbox(lb, k));
      gal.append(img);
    });
    frag.append(gal);
    return frag;
  }

  function mediaLabel(text) {
    const el = document.createElement("div");
    el.className = "wl-evt-medialabel";
    el.textContent = text;
    return el;
  }
  function buildAudioPlayer(path) {
    const a = document.createElement("audio");
    a.className = "wl-evt-audio";
    a.controls = true;
    a.preload = "metadata";
    a.src = clipUrl(path);
    return a;
  }

  // Event clips carry rotation metadata (browser shows them upright → no .rot);
  // recording segments don't (browser shows sideways → caller passes rotate=true).
  function buildVideoPlayer(path, rotate, autoplay) {
    const wrap = document.createElement("div");
    wrap.className = "wl-evt-clipbox";
    const vwrap = document.createElement("div");
    vwrap.className = "wl-evt-video-wrap" + (rotate ? " rot" : "");
    const v = document.createElement("video");
    v.className = "wl-evt-video";
    v.controls = true;
    v.preload = "metadata";
    v.playsInline = true;
    if (autoplay) { v.muted = true; v.autoplay = true; v.loop = true; }
    v.src = clipUrl(path);
    vwrap.append(v);
    wrap.append(vwrap);
    const a = document.createElement("a");
    a.className = "wl-evt-clip";
    a.href = clipUrl(path);
    a.target = "_blank";
    a.rel = "noopener";
    a.textContent = "open raw video ↗";
    wrap.append(a);
    return wrap;
  }

  function buildMetering(d) {
    const box = document.createElement("div");
    box.className = "wl-evt-metering";
    const m = d.metering || {};
    const rows = [
      ["lux", m.lux != null ? Number(m.lux).toFixed(2) : null],
      ["exposure", m.exposure_us != null ? `${(m.exposure_us / 1000).toFixed(0)} ms` : null],
      ["gain", m.gain != null ? `${m.gain}×` : null],
      ["source", m.source || null],
      ["cal k", d.cal_k != null ? Number(d.cal_k).toFixed(2) : null],
    ].filter(([, v]) => v != null);
    if (!rows.length) { box.textContent = "No metering data."; return box; }
    rows.forEach(([k, v]) => {
      const cell = document.createElement("div");
      cell.className = "wl-evt-meter-cell";
      cell.innerHTML = `<span class="wl-evt-meter-k">${k}</span><span class="wl-evt-meter-v">${v}</span>`;
      box.append(cell);
    });
    return box;
  }

  // ---- Recordings tab --------------------------------------------------------

  function recStream() { return recStreams[0] || "cam"; }

  async function loadRecordings() {
    const countEl = $("wl-rec-count");
    let segs;
    try {
      const res = await fetch(
        apiUrl(`/api/segments?stream=${recStream()}&limit=${REC_LIMIT}`), { cache: "no-store" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      segs = await res.json();
    } catch (err) {
      countEl.textContent = "fetch failed";
      return;
    }
    segs = Array.isArray(segs) ? segs : [];
    countEl.textContent = segs.length ? `${segs.length} shown · ${streamLabel(recStream())}` : "";
    $("wl-rec-empty").hidden = segs.length > 0;
    const sig = segs.map((s) => s.url).join(",");
    if (sig === lastRecsSig) return;
    lastRecsSig = sig;
    renderRecordings(segs);
  }

  function renderRecordings(segs) {
    const list = $("wl-rec-list");
    list.replaceChildren();
    const frag = document.createDocumentFragment();
    segs.forEach((seg) => frag.append(buildRecordingRow(seg)));
    list.append(frag);
  }

  function buildRecordingRow(seg) {
    const li = document.createElement("li");
    li.className = "wl-evt";
    const row = document.createElement("div");
    row.className = "wl-evt-row";
    row.tabIndex = 0;
    row.setAttribute("role", "button");
    row.setAttribute("aria-expanded", "false");

    const img = document.createElement("img");
    img.className = "wl-evt-thumb";
    img.loading = "lazy";
    img.alt = "recording poster";
    img.src = posterUrl(seg.url);
    img.addEventListener("error", () => {
      const ph = document.createElement("div");
      ph.className = "wl-evt-thumb placeholder";
      ph.textContent = "◇";
      img.replaceWith(ph);
    });
    row.append(img);

    const main = document.createElement("div");
    main.className = "wl-evt-main";
    const top = document.createElement("div");
    top.className = "wl-evt-top";
    const label = document.createElement("span");
    label.className = "wl-evt-label";
    label.textContent = "Recording";
    const time = document.createElement("span");
    time.className = "wl-evt-time";
    time.textContent = fmtEventTime(seg.start_time);
    top.append(label, time);
    const meta = document.createElement("div");
    meta.className = "wl-evt-meta";
    const bits = [];
    if (seg.duration != null) bits.push(`${Number(seg.duration).toFixed(0)}s`);
    if (seg.size != null) bits.push(fmtBytes(seg.size));
    meta.textContent = bits.join("  ·  ");
    main.append(top, meta);
    row.append(main);

    const tail = document.createElement("div");
    tail.className = "wl-evt-tail";
    const tag = document.createElement("span");
    tag.className = "wl-evt-cliptag";
    tag.textContent = "▶ play";
    const caret = document.createElement("span");
    caret.className = "wl-evt-caret";
    caret.textContent = "▾";
    tail.append(tag, caret);
    row.append(tail);

    const detail = document.createElement("div");
    detail.className = "wl-evt-detail";
    detail.hidden = true;
    let built = false;
    const toggle = () => {
      const open = li.classList.toggle("open");
      row.setAttribute("aria-expanded", open ? "true" : "false");
      detail.hidden = !open;
      if (open) {
        expandedRecs.add(seg.url);
        if (!built) { detail.append(buildVideoPlayer(seg.url, false)); built = true; }
      } else {
        expandedRecs.delete(seg.url);
      }
    };
    row.addEventListener("click", toggle);
    row.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); toggle(); }
    });
    li.append(row, detail);
    if (expandedRecs.has(seg.url)) toggle();
    return li;
  }

  // ---- Stacks tab ------------------------------------------------------------

  async function loadStacks() {
    let all = [];
    try {
      const results = await Promise.all(
        streams.map((s) =>
          fetch(apiUrl(`/api/stacks?stream=${encodeURIComponent(s)}`), { cache: "no-store" })
            .then((r) => (r.ok ? r.json() : []))
            .catch(() => [])));
      all = results.flat().filter(Boolean);
    } catch (_err) {
      if (activeTab === "stacks") setMsg("Stacks fetch failed.", "err");
      return;
    }
    all.sort((a, b) => (b.created_iso || "").localeCompare(a.created_iso || ""));
    $("wl-stacks-count").textContent = all.length ? `${all.length} stacks` : "";
    const sig = all.map((r) => r.url).join(",");
    if (sig === lastStacksSig && activeTab === "stacks") return;
    lastStacksSig = sig;
    renderStacks(all);
  }

  function renderStacks(stacks) {
    const grid = $("wl-stacks-grid");
    grid.replaceChildren();
    $("wl-stacks-empty").hidden = stacks.length > 0;
    const lb = stacks.map((r) => ({
      src: stackedUrl(r.url),
      alt: `stack ${r.event} ${r.method}`,
      cap:
        `<span>${camLabel(r.stream)}</span><span>${r.event}</span>` +
        `<span>${r.method} · ${r.source} · ${r.frames} frames</span>` +
        `<span>${r.width}×${r.height} · mean ${r.mean}</span>`,
    }));
    const frag = document.createDocumentFragment();
    stacks.forEach((r, i) => frag.append(buildStackCard(r, i, lb)));
    grid.append(frag);
  }

  function buildStackCard(r, i, lb) {
    const tile = document.createElement("div");
    tile.className = "wl-tile";
    tile.tabIndex = 0;
    const open = () => openLightbox(lb, i);
    tile.addEventListener("click", (e) => {
      if (e.target.closest(".wl-stack-redo")) return;
      open();
    });
    tile.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
    });

    const img = document.createElement("img");
    img.className = "wl-thumb";
    img.loading = "lazy";
    img.src = stackedUrl(r.url);
    img.alt = `stack ${r.event}`;
    tile.append(img);

    const meta = document.createElement("div");
    meta.className = "wl-meta";
    meta.innerHTML =
      `<div class="wl-meta-row"><span class="wl-time">${r.event}</span>` +
      `<span class="wl-chip ${r.source === "raw" ? "night" : "cam"}">${r.source}</span></div>` +
      `<div class="wl-meta-row"><span class="wl-chip cam">${streamLabel(r.stream)} · ${r.method}</span>` +
      `<span class="wl-exp">${r.frames}f · ${r.width}×${r.height}</span></div>`;
    const redo = document.createElement("button");
    redo.type = "button";
    redo.className = "wl-btn wl-stack-redo";
    redo.textContent = "↻ re-stack";
    redo.addEventListener("click", (e) => {
      e.stopPropagation();
      requestStack({ stream: r.stream, event: r.event, method: r.method, force: true, trigger: redo });
    });
    meta.append(redo);
    tile.append(meta);
    return tile;
  }

  // Synchronous on the Pi (~1 min for raw) — no client-side abort timeout; show a
  // spinner on the triggering control and clear messaging.
  async function requestStack({ stream, event, method = "mean", force = false, trigger = null }) {
    if (trigger) { trigger.disabled = true; trigger.dataset.label = trigger.textContent; trigger.textContent = "Stacking…"; }
    setMsg(`Stacking ${event} (${method})… may take ~1 min for raw.`);
    try {
      const res = await fetch(apiUrl("/api/stack"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ stream, event, method, force }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        const detail = body && body.detail ? body.detail : "HTTP " + res.status;
        const hint = /40[13]/.test(String(res.status)) ? " (token not injected at proxy?)" : "";
        throw new Error(detail + hint);
      }
      setMsg(`Stacked ${event}: ${body.frames || "?"} frames, ${body.method} (${body.source}).`, "ok");
      lastStacksSig = ""; // force re-render
      await loadStacks();
      if (activeTab !== "stacks") setMsg(`Stacked ${event} — see the Stacks tab.`, "ok");
    } catch (err) {
      setMsg("Stack failed: " + err.message, "err");
    } finally {
      if (trigger) { trigger.disabled = false; trigger.textContent = trigger.dataset.label || "✨ Recover / Stack"; }
    }
  }

  function buildStackStreamSelect() {
    const sel = $("wl-stack-stream");
    sel.replaceChildren();
    streams.forEach((s) => {
      const o = document.createElement("option");
      o.value = s; o.textContent = CAM_LABELS[s] || s;
      sel.append(o);
    });
  }

  function onStackFormSubmit(ev) {
    ev.preventDefault();
    const stream = $("wl-stack-stream").value;
    const event = $("wl-stack-event").value.trim();
    const method = $("wl-stack-method").value;
    const force = $("wl-stack-force").checked;
    if (!/^\d{8}_\d{6}$/.test(event)) {
      setMsg("Event must be YYYYMMDD_HHMMSS.", "err");
      return;
    }
    requestStack({ stream, event, method, force, trigger: ev.submitter });
  }

  // ---- Live tab --------------------------------------------------------------

  function activateLive() {
    loadStreaming();
  }

  function buildLiveGrid(streaming) {
    const grid = $("wl-live-grid");
    const empty = $("wl-live-empty");
    if (!streaming) {
      grid.replaceChildren();
      liveBuilt = false;
      empty.hidden = false;
      return;
    }
    empty.hidden = true;
    if (liveBuilt) return; // don't rebuild live iframes every poll
    if (/^https?:\/\//i.test(GO2RTC) && location.protocol === "https:") {
      // Mixed content would be blocked — surface it instead of silent blank frames.
      grid.replaceChildren();
      const p = document.createElement("p");
      p.className = "wl-empty";
      p.textContent = "Live base must be a same-origin path over HTTPS — check WILDLIFE_GO2RTC_BASE.";
      grid.append(p);
      return;
    }
    grid.replaceChildren();
    const frag = document.createDocumentFragment();
    streams.forEach((s) => {
      const card = document.createElement("div");
      card.className = "wl-live-card";
      const h = document.createElement("h3");
      h.textContent = CAM_LABELS[s] || s;
      const ifr = document.createElement("iframe");
      ifr.src = go2rtcUrl(s);
      ifr.allow = "autoplay; fullscreen";
      ifr.loading = "lazy";
      card.append(h, ifr);
      frag.append(card);
    });
    grid.append(frag);
    liveBuilt = true;
  }

  function teardownLive() {
    // Drop iframe srcs so the Pi doesn't hold stale WebRTC peers.
    $("wl-live-grid").querySelectorAll("iframe").forEach((f) => (f.src = "about:blank"));
    liveBuilt = false;
  }

  async function loadStreaming() {
    let sm;
    try {
      const res = await fetch(apiUrl("/api/streaming"), { cache: "no-store" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      sm = await res.json();
    } catch (err) {
      setMsg("Streaming status failed: " + err.message, "err");
      return;
    }
    const tgl = $("wl-live-toggle");
    if (tgl && document.activeElement !== tgl) tgl.checked = !!sm.streaming;
    $("wl-live-hint").textContent = sm.streaming
      ? "Live is on — the snapshot timer is paused."
      : "Enabling live pauses the snapshot timer (camera contention).";
    buildLiveGrid(!!sm.streaming);
  }

  async function toggleStreaming(ev) {
    const enabled = ev.target.checked;
    setMsg(enabled ? "Starting live…" : "Stopping live…");
    try {
      const res = await fetch(apiUrl("/api/streaming"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      });
      if (!res.ok) {
        const hint = /40[13]/.test(String(res.status)) ? " (token not injected at proxy?)" : "";
        throw new Error("HTTP " + res.status + hint);
      }
      setMsg(enabled ? "Live started." : "Live stopped.", "ok");
      liveBuilt = false;
      await loadStreaming();
      loadStatus();
    } catch (err) {
      setMsg("Toggle failed: " + err.message, "err");
      ev.target.checked = !enabled; // revert the switch
    }
  }

  // ---- Settings tab (data-driven) --------------------------------------------

  // Declarative field spec → controls + helper text. `convert: "us_ms"` stores in
  // µs but shows a friendlier ms slider. `advanced` fields tuck into a per-section
  // Advanced disclosure. range = slider w/ live readout; toggle = on/off switch.
  const SETTINGS_SPEC = [
    { title: "Mode switching", blurb: "Day/night hysteresis — keep day-return above night-enter to avoid flapping.", fields: [
      { key: "night_enter_lux", label: "Night enter", control: "range", min: 0, max: 20, step: 0.5, unit: "lux", help: "Below this measured lux, switch into night mode." },
      { key: "day_return_lux", label: "Day return", control: "range", min: 0, max: 20, step: 0.5, unit: "lux", help: "Above this lux, switch back to day mode." },
      { key: "lux_day", label: "Day reference", control: "range", min: 0, max: 20, step: 0.5, unit: "lux", help: "Reference lux for day metering." },
      { key: "mode_min_dwell_s", label: "Min mode dwell", control: "number", min: 0, max: 3600, step: 5, unit: "s", help: "Minimum time in a mode before it may switch (anti-flap)." },
    ]},
    { title: "Metering", blurb: "How often the light meter samples.", fields: [
      { key: "meter_interval_s", label: "Meter interval", control: "number", min: 1, max: 3600, step: 1, unit: "s", help: "Seconds between light reads in steady state." },
      { key: "meter_interval_fast_s", label: "Fast interval", control: "number", min: 1, max: 600, step: 1, unit: "s", help: "Faster cadence right after a mode change." },
      { key: "night_cal_k", label: "Night cal k", control: "range", min: 0, max: 5, step: 0.05, advanced: true, help: "Seeds cam2 meter → IMX415 exposure; AGC self-corrects from there." },
    ]},
    { title: "Night exposure / AGC", blurb: "AGC maxes the shutter first (clean light), then fills with gain (noisier light).", fields: [
      { key: "night_shutter_us", label: "Start shutter", control: "range", min: 50, max: 1000, step: 10, unit: "ms", convert: "us_ms", help: "Initial night shutter. Higher = brighter, but more motion blur." },
      { key: "night_gain", label: "Start gain", control: "range", min: 1, max: 64, step: 1, unit: "×", help: "Initial night analog gain." },
      { key: "night_max_exposure_us", label: "Shutter ceiling", control: "range", min: 50, max: 1000, step: 10, unit: "ms", convert: "us_ms", help: "Max shutter. 1000ms ≈ 1 fps, 200ms ≈ 5 fps. Higher = more clean light." },
      { key: "night_gain_ceiling", label: "Gain ceiling", control: "range", min: 1, max: 64, step: 1, unit: "×", help: "Max analog gain. Higher = more light, more noise." },
      { key: "night_target_luma", label: "Target brightness", control: "range", min: 0, max: 255, step: 1, help: "AGC target frame brightness (0–255)." },
      { key: "night_agc_damp", label: "AGC damping", control: "range", min: 0, max: 1, step: 0.05, advanced: true, help: "AGC response strength; lower if frames pulse/hunt." },
      { key: "night_agc_up_max", label: "Up-ramp (shutter)", control: "range", min: 1.1, max: 8.0, step: 0.1, advanced: true, help: "Max brighten step below the ceiling — how fast the shutter lengthens." },
      { key: "night_agc_up_gain", label: "Up-ramp (gain)", control: "range", min: 1.05, max: 2.0, step: 0.05, advanced: true, help: "Max brighten step in the high-gain region — lower eases into high gain." },
    ]},
    { title: "Night burst", blurb: "More frames stack better but take longer.", fields: [
      { key: "night_burst_frames", label: "Burst frames", control: "number", min: 1, max: 30, step: 1, help: "Frames captured per night event." },
      { key: "night_burst_max_s", label: "Burst max", control: "number", min: 1, max: 60, step: 1, unit: "s", help: "Cap on a night burst's duration." },
      { key: "night_pre_frames", label: "Pre-trigger frames", control: "number", min: 0, max: 10, step: 1, help: "Frames kept from just before the trigger." },
      { key: "night_save_raw", label: "Save night raw (DNG)", control: "toggle", help: "Save raw DNGs for night events — needed for high-quality stacking (large files)." },
    ]},
    { title: "Day capture", fields: [
      { key: "day_burst_count", label: "Day burst", control: "number", min: 1, max: 30, step: 1, help: "Frames captured per day event." },
      { key: "day_freeze_us", label: "Day shutter", control: "number", min: 100, max: 100000, step: 100, unit: "µs", help: "Day shutter — lower freezes motion (less blur)." },
    ]},
    { title: "Clip / roll", fields: [
      { key: "preroll_s", label: "Pre-roll", control: "number", min: 0, max: 30, step: 1, unit: "s", help: "Footage kept before an event." },
      { key: "postroll_s", label: "Post-roll", control: "number", min: 0, max: 30, step: 1, unit: "s", help: "Footage kept after an event." },
      { key: "event_clip_rotate", label: "Clip rotation", control: "select", advanced: true, options: [[-180, "-180°"], [-90, "-90°"], [0, "0°"], [90, "90°"], [180, "180°"]], help: "⚠ Rotation applied to event clips — changing this affects orientation." },
    ]},
    { title: "Audio", fields: [
      { key: "audio_gain_db", label: "Mic gain", control: "range", min: 0, max: 30, step: 1, unit: "dB", help: "Microphone gain for event audio." },
      { key: "night_audio_s", label: "Night audio", control: "number", min: 0, max: 30, step: 1, unit: "s", help: "Seconds of audio captured on a night event." },
    ]},
    { title: "Video (burst encode)", fields: [
      { key: "burst_video_fps", label: "Burst FPS", control: "number", min: 1, max: 30, step: 1, unit: "fps", help: "Frame rate of the encoded burst video." },
      { key: "burst_video_max_edge", label: "Max edge", control: "number", min: 320, max: 7680, step: 1, unit: "px", help: "Longest edge of the burst video (downscale cap)." },
    ]},
    { title: "Image", fields: [
      { key: "jpeg_quality", label: "JPEG quality", control: "range", min: 1, max: 100, step: 1, help: "JPEG quality for snapshots." },
      { key: "save_raw", label: "Save day raw (DNG)", control: "toggle", help: "Save raw DNGs for day/global snapshots (large files)." },
      { key: "orientation", label: "Sensor orientation", control: "select", advanced: true, options: [[1, "1"], [2, "2"], [3, "3"], [4, "4"], [5, "5"], [6, "6"], [7, "7"], [8, "8"]], help: "⚠ EXIF orientation code (1–8) — affects how frames are rotated." },
    ]},
  ];

  const SPEC_BY_KEY = {};
  SETTINGS_SPEC.forEach((sec) => sec.fields.forEach((f) => (SPEC_BY_KEY[f.key] = f)));

  let settingsBuilt = false;

  const toDisplay = (f, v) => (v == null ? v : f.convert === "us_ms" ? Math.round(v / 1000) : v);
  const toStored = (f, v) => (f.convert === "us_ms" ? Math.round(v * 1000) : v);

  function renderField(f) {
    const wrap = document.createElement("div");
    wrap.className = "wl-field wl-field-" + f.control;

    const labelRow = document.createElement("div");
    labelRow.className = "wl-field-label";
    const name = document.createElement("span");
    name.className = "wl-field-name";
    name.textContent = f.label;
    labelRow.append(name);

    const id = "set-" + f.key;

    if (f.control === "range") {
      const out = document.createElement("span");
      out.className = "wl-field-val";
      out.id = id + "-out";
      labelRow.append(out);
      wrap.append(labelRow);
      const input = document.createElement("input");
      input.type = "range";
      input.id = id;
      input.min = f.min; input.max = f.max; input.step = f.step;
      input.addEventListener("input", () => (out.textContent = `${input.value}${f.unit ? " " + f.unit : ""}`));
      wrap.append(input);
    } else if (f.control === "toggle") {
      const sw = document.createElement("label");
      sw.className = "wl-switch wl-field-toggle";
      const input = document.createElement("input");
      input.type = "checkbox";
      input.id = id;
      const track = document.createElement("span");
      track.className = "wl-switch-track";
      const thumb = document.createElement("span");
      thumb.className = "wl-switch-thumb";
      track.append(thumb);
      sw.append(input, track);
      labelRow.append(sw);
      wrap.append(labelRow);
    } else if (f.control === "select") {
      wrap.append(labelRow);
      const sel = document.createElement("select");
      sel.id = id;
      (f.options || []).forEach(([v, t]) => {
        const o = document.createElement("option");
        o.value = v; o.textContent = t;
        sel.append(o);
      });
      wrap.append(sel);
    } else {
      // number
      wrap.append(labelRow);
      const row = document.createElement("div");
      row.className = "wl-num-row";
      const input = document.createElement("input");
      input.type = "number";
      input.id = id;
      if (f.min != null) input.min = f.min;
      if (f.max != null) input.max = f.max;
      if (f.step != null) input.step = f.step;
      row.append(input);
      if (f.unit) {
        const u = document.createElement("span");
        u.className = "wl-num-unit";
        u.textContent = f.unit;
        row.append(u);
      }
      wrap.append(row);
    }

    if (f.help) {
      const help = document.createElement("small");
      help.className = "wl-help";
      help.textContent = f.help;
      wrap.append(help);
    }
    return wrap;
  }

  function renderSettingsForm() {
    const body = $("wl-settings-body");
    body.replaceChildren();
    SETTINGS_SPEC.forEach((sec) => {
      const section = document.createElement("div");
      section.className = "wl-set-section";
      const h = document.createElement("h3");
      h.className = "wl-set-h";
      h.textContent = sec.title;
      section.append(h);
      if (sec.blurb) {
        const b = document.createElement("p");
        b.className = "wl-set-blurb";
        b.textContent = sec.blurb;
        section.append(b);
      }
      const grid = document.createElement("div");
      grid.className = "wl-set-grid";
      const adv = sec.fields.filter((f) => f.advanced);
      sec.fields.filter((f) => !f.advanced).forEach((f) => grid.append(renderField(f)));
      section.append(grid);
      if (adv.length) {
        const det = document.createElement("details");
        det.className = "wl-adv";
        const sum = document.createElement("summary");
        sum.textContent = "Advanced";
        det.append(sum);
        const agrid = document.createElement("div");
        agrid.className = "wl-set-grid";
        adv.forEach((f) => agrid.append(renderField(f)));
        det.append(agrid);
        section.append(det);
      }
      body.append(section);
    });
    settingsBuilt = true;
  }

  function applySettingValues(s) {
    SETTINGS_SPEC.forEach((sec) => sec.fields.forEach((f) => {
      const el = $("set-" + f.key);
      if (!el || s[f.key] == null) return;
      if (f.control === "toggle") {
        el.checked = !!s[f.key];
      } else if (f.control === "range") {
        el.value = toDisplay(f, s[f.key]);
        const out = $("set-" + f.key + "-out");
        if (out) out.textContent = `${el.value}${f.unit ? " " + f.unit : ""}`;
      } else {
        el.value = toDisplay(f, s[f.key]);
      }
    }));
  }

  async function loadSettings() {
    if (!settingsBuilt) renderSettingsForm();
    try {
      const res = await fetch(apiUrl("/api/settings"), { cache: "no-store" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      applySettingValues(await res.json());
      if (msg().classList.contains("err")) setMsg("");
    } catch (err) {
      setMsg("Could not load settings: " + err.message, "err");
    }
  }

  async function saveSettings(ev) {
    ev.preventDefault();
    const payload = {};
    SETTINGS_SPEC.forEach((sec) => sec.fields.forEach((f) => {
      const el = $("set-" + f.key);
      if (!el) return;
      if (f.control === "toggle") {
        payload[f.key] = el.checked;
      } else if (el.value !== "") {
        payload[f.key] = toStored(f, Number(el.value));
      }
    }));
    setMsg("Saving settings…");
    try {
      const res = await fetch(apiUrl("/api/settings"), {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        const hint = /40[13]/.test(String(res.status)) ? " (token not injected at proxy?)" : "";
        throw new Error("HTTP " + res.status + hint);
      }
      applySettingValues(await res.json());
      setMsg("Settings saved — applies next capture.", "ok");
    } catch (err) {
      setMsg("Save failed: " + err.message, "err");
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
      if (!res.ok || body.ok === false) throw new Error(body.error || "HTTP " + res.status);
      setMsg("Capture fired — refreshing…", "ok");
      setTimeout(() => {
        if (activeTab === "snapshots") loadSnapshots();
        loadStatus();
        setMsg("");
      }, 3500);
    } catch (err) {
      const hint = /40[13]/.test(err.message) ? " (token not injected at proxy?)" : "";
      setMsg("Capture failed: " + err.message + hint, "err");
    } finally {
      btn.disabled = false;
    }
  }

  function refreshActive() {
    TABS[activeTab].load();
    loadStatus();
    loadPir();
    loadLight();
    loadEventsStat();
  }

  // ---- bootstrap & wiring ----------------------------------------------------

  async function bootstrap() {
    try {
      const cfg = await fetch(apiUrl("/api/config"), { cache: "no-store" }).then((r) => r.json());
      if (cfg && Array.isArray(cfg.streams) && cfg.streams.length) streams = cfg.streams;
    } catch (_err) { /* keep defaults */ }
    try {
      const cams = await fetch(apiUrl("/api/cameras"), { cache: "no-store" }).then((r) => r.json());
      if (Array.isArray(cams)) {
        const rec = cams.filter((c) => (c.segments || 0) > 0).map((c) => c.stream);
        if (rec.length) recStreams = rec;
      }
    } catch (_err) { /* keep defaults */ }
    buildStreamFilter();
    buildStackStreamSelect();
    renderSettingsForm();
  }

  function init() {
    // Tabs
    $("wl-tabs").addEventListener("click", (e) => {
      const btn = e.target.closest(".vbtn");
      if (btn) activateTab(btn.dataset.tab);
    });
    // Snapshot camera filter
    $("wl-streamseg").addEventListener("click", (e) => {
      const btn = e.target.closest(".vbtn");
      if (!btn) return;
      snapStream = btn.dataset.stream;
      $("wl-streamseg").querySelectorAll(".vbtn").forEach((b) => b.classList.toggle("active", b === btn));
      loadSnapshots();
    });
    // Global controls
    $("wl-refresh").addEventListener("click", refreshActive);
    $("wl-capture").addEventListener("click", captureNow);
    $("wl-auto").addEventListener("change", onAutoOrVisibilityChange);
    // Stacks
    $("wl-stack-form").addEventListener("submit", onStackFormSubmit);
    // Live
    $("wl-live-toggle").addEventListener("change", toggleStreaming);
    // Settings
    $("wl-settings-form").addEventListener("submit", saveSettings);
    $("wl-settings-reload").addEventListener("click", loadSettings);
    // Event overlay
    $("wl-eb-close").addEventListener("click", closeEventModal);
    $("wl-eventbox").addEventListener("click", (e) => { if (e.target.id === "wl-eventbox") closeEventModal(); });
    // Lightbox
    $("wl-lb-close").addEventListener("click", closeLightbox);
    $("wl-lb-prev").addEventListener("click", () => stepLightbox(-1));
    $("wl-lb-next").addEventListener("click", () => stepLightbox(1));
    $("wl-lightbox").addEventListener("click", (e) => { if (e.target.id === "wl-lightbox") closeLightbox(); });
    document.addEventListener("keydown", (e) => {
      if (!$("wl-lightbox").hidden) {
        if (e.key === "Escape") closeLightbox();
        else if (e.key === "ArrowLeft") stepLightbox(-1);
        else if (e.key === "ArrowRight") stepLightbox(1);
        return;
      }
      if (!$("wl-eventbox").hidden && e.key === "Escape") closeEventModal();
    });
    document.addEventListener("visibilitychange", onAutoOrVisibilityChange);

    bootstrap().then(() => {
      const initial = location.hash.slice(1);
      activeTab = TABS[initial] ? initial : "snapshots";
      activateTab(activeTab, true);
      loadPir();
      loadStatus();
      loadLight();
      loadEventsStat();
      startAmbientPolling();
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
