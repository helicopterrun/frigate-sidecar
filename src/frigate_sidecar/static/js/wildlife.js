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
  const PIR_POLL_MS = 3000; // PIR is live state — poll fast, but pause when hidden
  const EVENTS_LIMIT = 50; // PIR events to pull per refresh
  const REC_LIMIT = 60; // recording segments to pull (≈10 min at 10s each)
  const REC_STREAM = "cam"; // only `cam` records continuous segments; cam2 has none
  // Stills mode expects newest_snapshot_age_s ≈ 60–90s; flag past 5 min.
  // Freshness is judged with the SERVER's age field, never the browser clock —
  // the Pi and this web server can have different clocks/timezones (per API.md).
  const STALE_AGE_S = 5 * 60;

  const STREAM_LABELS = {
    cam: "IMX415",
    cam2: "IMX708",
  };
  // Events don't carry a camera id (their `stream` is the PIR pin, e.g.
  // "GPIO17"). The real camera is implied by mode: day bursts come from cam2
  // (IMX708 autofocus), night from cam (IMX415 low-light). We still derive it
  // from the still path when possible — see streamFromStill().
  const CAM_LABELS = {
    cam: "IMX415 · low-light",
    cam2: "IMX708 · autofocus",
  };

  const $ = (id) => document.getElementById(id);
  const grid = $("wl-grid");
  const empty = $("wl-empty");
  const msg = $("wl-msg");

  let stream = "all"; // 'all' | 'cam' | 'cam2'
  let items = []; // last rendered snapshot list (for the grid)
  let lbList = []; // generic lightbox source: [{ src, alt, cap }] — snapshots OR event stills
  let lbIndex = -1;
  const expandedEvents = new Set(); // event ids whose detail panel is open (survives re-render)
  let lastEventsSig = ""; // id-signature of the last rendered event set; skip re-render if unchanged
  const expandedRecs = new Set(); // recording ids (seg url) whose player is open
  let lastRecsSig = ""; // signature of the last rendered recording set
  let pollTimer = null;
  let pirTimer = null;
  let pirDetected = false; // last-seen any_detected, for clear→detected edge
  // Most recent SERVER wall-clock (epoch s) from /api/pir or /api/stats. Event
  // ages are computed against this, never the browser clock (per API.md).
  let lastServerTs = null;

  // ---- helpers ---------------------------------------------------------------

  const apiUrl = (path) => API.replace(/\/$/, "") + path;
  // Snapshot `url`/`raw_url` and event still URLs are relative to the API
  // origin → prefix the base.
  const mediaUrl = (rel) => API.replace(/\/$/, "") + rel;
  // Event clips are streamed through the SIDECAR (route /wildlife/media/...),
  // NOT the wildlife API base: that path is independent of the ?api override and
  // of whether NPM forwards /wildlifecam/media to the Pi (it doesn't reliably).
  // clip_path is like "/media/_events/cam/evt_x.mp4".
  const clipUrl = (clipPath) =>
    clipPath ? "/wildlife/media/" + clipPath.replace(/^\/+media\/+/, "") : "";
  // Recording poster: the sidecar extracts + caches the segment's OWN first
  // frame (ffmpeg) — always the correct camera/frame. seg.url is "/media/…".
  const posterUrl = (segPath) =>
    segPath ? "/wildlife/poster/" + segPath.replace(/^\/+media\/+/, "") : "";

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
      tile.addEventListener("click", () => openLightbox(snapshotLbList(), i));
      tile.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          openLightbox(snapshotLbList(), i);
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

  // The lightbox pages through a generic list of { src, alt, cap(HTML) } so it
  // serves both the snapshot grid and an event's still burst.
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
    $("wl-lb-cap").innerHTML = it.cap || "";
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

  // Lightbox source for the snapshot grid (preserves the original caption).
  function snapshotLbList() {
    return items.map((s) => ({
      src: mediaUrl(s.url),
      alt: `${s.stream} ${s.time_iso}`,
      cap:
        `<span>${STREAM_LABELS[s.stream] || s.stream}</span>` +
        `<span>${s.time_iso}</span>` +
        `<span>${s.night ? "🌙 night" : "☀ day"} · ${s.exposure || "?"} · gain ${s.gain || "?"}</span>` +
        (s.raw_url
          ? `<a href="${mediaUrl(s.raw_url)}" download>⬇ DNG (${fmtBytes(s.size)} jpg)</a>`
          : `<span>no raw</span>`),
    }));
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
        if (st.server_ts != null) lastServerTs = st.server_ts;
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

  // ---- live motion (PIR) -----------------------------------------------------

  // Poll /api/pir for the live sensor state. Shape (per wildlife-cam app.py):
  //   { service_active, available, pins:[{gpio,level,state,detected}],
  //     any_detected, last_event_ts, last_event_pin, server_ts }
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
      // Best-effort, like the status strip — show offline, don't spam wl-msg.
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

    // Sub-line: per-pin state + last-trigger age (server-relative).
    const pins = Array.isArray(p.pins) ? p.pins : [];
    const pinStr = pins
      .map((pin) => `gpio${pin.gpio} ${pin.detected ? "●" : "○"}`)
      .join("  ");
    let lastStr = "";
    if (p.last_event_ts != null) {
      const ref = lastServerTs != null ? lastServerTs : p.server_ts;
      if (ref != null) {
        const ago = fmtAgeSec(ref - p.last_event_ts);
        lastStr = `last ${ago}` + (p.last_event_pin != null ? ` (gpio${p.last_event_pin})` : "");
      }
    }
    sub.textContent = [pinStr, lastStr].filter(Boolean).join("  ·  ");

    // Clear→detected edge: a trigger just landed → pull events soon (give the
    // upstream a moment to write the row before we re-fetch).
    if (detected && !pirDetected) {
      setTimeout(loadEvents, 1200);
    }
    pirDetected = detected;
  }

  // ---- motion events ---------------------------------------------------------

  // GET /api/events?type=pir → newest-first list. Item shape (wildlife-cam
  // docs/API.md): { id, type, stream, start_time, end_time, score, label,
  // thumbnail, clip_path, has_clip, data }. Most fields are optional/forward-
  // looking, so render defensively.
  async function loadEvents() {
    const countEl = $("wl-events-count");
    let evts;
    try {
      const q = new URLSearchParams({ type: "pir", limit: String(EVENTS_LIMIT) });
      const res = await fetch(apiUrl("/api/events?" + q.toString()), { cache: "no-store" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      evts = await res.json();
    } catch (err) {
      countEl.textContent = "fetch failed";
      return;
    }
    evts = Array.isArray(evts) ? evts : [];
    countEl.textContent = evts.length ? `${evts.length} shown` : "";
    $("wl-events-empty").hidden = evts.length > 0;

    // Skip the DOM rebuild when the event set is unchanged — otherwise the 45s
    // poll would collapse an open row and restart a playing clip. The ordered id
    // list is a sufficient signature (rows are append-only upstream).
    const sig = evts.map((e) => e.id).join(",");
    if (sig === lastEventsSig) return;
    lastEventsSig = sig;
    renderEvents(evts);
  }

  function renderEvents(evts) {
    const list = $("wl-events-list");
    list.replaceChildren();
    const frag = document.createDocumentFragment();
    evts.forEach((e) => frag.append(buildEventRow(e)));
    list.append(frag);
  }

  // `data` is a JSON STRING carrying the real payload (mode, stills, lux, and —
  // at night — a metering block). The top-level `stream` is the PIR pin
  // ("GPIO17") and `score` is always null, so neither is rendered; everything
  // meaningful comes from parsed `data`.
  function parseEventData(e) {
    try {
      return e && e.data ? JSON.parse(e.data) : {};
    } catch (_err) {
      return {};
    }
  }

  // Stills live at /snap/<cam>/… — pull the cam segment. Falls back to the mode
  // convention (day→cam2 / night→cam) when the path is unexpected.
  function streamFromStill(url, mode) {
    const m = /\/snap\/([^/]+)\//.exec(url || "");
    if (m) return m[1];
    return mode === "night" ? "cam" : mode === "day" ? "cam2" : null;
  }

  function buildEventRow(e) {
    const d = parseEventData(e);
    const mode = d.mode || (e.thumbnail && e.thumbnail.includes("/snap/cam2/") ? "day" : "");
    const stills =
      Array.isArray(d.stills) && d.stills.length
        ? d.stills
        : e.thumbnail
        ? [e.thumbnail]
        : [];
    const cam = streamFromStill(stills[0] || e.thumbnail, mode);

    const li = document.createElement("li");
    li.className = "wl-evt";

    // ---- collapsed header row (click / Enter to expand) ----
    const row = document.createElement("div");
    row.className = "wl-evt-row";
    row.tabIndex = 0;
    row.setAttribute("role", "button");
    row.setAttribute("aria-expanded", "false");

    const thumbSrc = e.thumbnail || stills[0];
    if (thumbSrc) {
      const img = document.createElement("img");
      img.className = "wl-evt-thumb";
      img.loading = "lazy";
      img.src = mediaUrl(thumbSrc);
      img.alt = mode || e.type || "event";
      row.append(img);
    } else {
      const ph = document.createElement("div");
      ph.className = "wl-evt-thumb placeholder";
      ph.textContent = "◇";
      row.append(ph);
    }

    const main = document.createElement("div");
    main.className = "wl-evt-main";

    const top = document.createElement("div");
    top.className = "wl-evt-top";
    if (mode) {
      const badge = document.createElement("span");
      badge.className = "wl-evt-mode " + (mode === "night" ? "night" : "day");
      badge.textContent = mode === "night" ? "🌙 night" : "☀ day";
      top.append(badge);
    }
    if (cam) {
      const camEl = document.createElement("span");
      camEl.className = "wl-evt-label";
      camEl.textContent = CAM_LABELS[cam] || cam;
      top.append(camEl);
    }
    const time = document.createElement("span");
    time.className = "wl-evt-time";
    time.textContent = fmtEventTime(e.start_time);
    top.append(time);

    const meta = document.createElement("div");
    meta.className = "wl-evt-meta";
    const bits = [];
    if (mode === "night" && d.metering) {
      const ms = meteringSummary(d.metering);
      if (ms) bits.push(ms);
    } else if (d.lux != null) {
      bits.push(`${Number(d.lux).toFixed(0)} lux`);
    }
    if (stills.length) bits.push(`${stills.length} still${stills.length === 1 ? "" : "s"}`);
    const dur = eventDuration(e);
    if (dur) bits.push(dur);
    meta.textContent = bits.join("  ·  ");

    main.append(top, meta);
    row.append(main);

    const tail = document.createElement("div");
    tail.className = "wl-evt-tail";
    if (e.has_clip && e.clip_path) {
      const tag = document.createElement("span");
      tag.className = "wl-evt-cliptag";
      tag.textContent = "▶ clip";
      tail.append(tag);
    }
    const caret = document.createElement("span");
    caret.className = "wl-evt-caret";
    caret.textContent = "▾";
    tail.append(caret);
    row.append(tail);

    // ---- detail panel (built lazily on first expand) ----
    const detail = document.createElement("div");
    detail.className = "wl-evt-detail";
    detail.hidden = true;

    let built = false;
    const toggle = () => {
      const open = li.classList.toggle("open");
      row.setAttribute("aria-expanded", open ? "true" : "false");
      detail.hidden = !open;
      if (open) {
        expandedEvents.add(e.id);
        if (!built) {
          buildEventDetail(detail, e, d, stills, cam, mode);
          built = true;
        }
      } else {
        expandedEvents.delete(e.id);
      }
    };
    row.addEventListener("click", toggle);
    row.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        toggle();
      }
    });

    li.append(row, detail);

    // Restore the open state across a poll-driven re-render.
    if (expandedEvents.has(e.id)) toggle();

    return li;
  }

  function buildEventDetail(detail, e, d, stills, cam, mode) {
    detail.replaceChildren();

    // Still burst → click any frame to page through it in the lightbox.
    if (stills.length) {
      const gal = document.createElement("div");
      gal.className = "wl-evt-stills";
      const lb = stills.map((u, k) => ({
        src: mediaUrl(u),
        alt: `${mode || "event"} still ${k + 1}/${stills.length}`,
        cap:
          `<span>${CAM_LABELS[cam] || cam || "event"}</span>` +
          `<span>frame ${k + 1} / ${stills.length}</span>` +
          `<span>${fmtEventTime(e.start_time)}</span>`,
      }));
      stills.forEach((u, k) => {
        const img = document.createElement("img");
        img.className = "wl-evt-still";
        img.loading = "lazy";
        img.src = mediaUrl(u);
        img.alt = `still ${k + 1}`;
        img.addEventListener("click", () => openLightbox(lb, k));
        gal.append(img);
      });
      detail.append(gal);
    }

    // Day events carry a prerolled clip; night events have none — show metering.
    if (mode === "night") {
      detail.append(buildMetering(d));
    } else if (e.has_clip && e.clip_path) {
      detail.append(buildVideoPlayer(e.clip_path));
    } else {
      const note = document.createElement("p");
      note.className = "wl-evt-note";
      note.textContent = "No clip for this event.";
      detail.append(note);
    }
  }

  // Both event clips and recording segments come from `cam` and are recorded
  // sideways with no rotation metadata (known backend limitation — the H.264
  // can't be losslessly rotated). We rotate the <video> 90° clockwise in CSS
  // (`.wl-evt-video`); the poster route applies the matching ffmpeg transpose=1.
  // `path` is a "/media/…" path (clip_path or seg.url).
  function buildVideoPlayer(path) {
    const wrap = document.createElement("div");
    wrap.className = "wl-evt-clipbox";

    const vwrap = document.createElement("div");
    vwrap.className = "wl-evt-video-wrap";
    const v = document.createElement("video");
    v.className = "wl-evt-video";
    v.controls = true;
    v.preload = "metadata";
    v.playsInline = true;
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

  // Night metering block: { lux, exposure_us, gain, source } + top-level cal_k.
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
    if (!rows.length) {
      box.textContent = "No metering data.";
      return box;
    }
    rows.forEach(([k, v]) => {
      const cell = document.createElement("div");
      cell.className = "wl-evt-meter-cell";
      cell.innerHTML =
        `<span class="wl-evt-meter-k">${k}</span><span class="wl-evt-meter-v">${v}</span>`;
      box.append(cell);
    });
    return box;
  }

  function meteringSummary(m) {
    const bits = [];
    if (m.lux != null) bits.push(`${Number(m.lux).toFixed(1)} lux`);
    if (m.exposure_us != null) bits.push(`${(m.exposure_us / 1000).toFixed(0)} ms`);
    if (m.gain != null) bits.push(`gain ${m.gain}`);
    return bits.join(" · ");
  }

  // Event start_time is epoch seconds. Show local time + a server-relative age
  // (uses lastServerTs from /api/pir|/api/stats, never the browser clock).
  function fmtEventTime(ts) {
    if (ts == null) return "";
    const local = new Date(ts * 1000).toLocaleString([], {
      month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit",
    });
    if (lastServerTs != null) return `${local} · ${fmtAgeSec(lastServerTs - ts)}`;
    return local;
  }

  function eventDuration(e) {
    if (e.end_time == null || e.start_time == null) return "";
    const d = e.end_time - e.start_time;
    if (!(d > 0)) return "";
    return d < 60 ? `${d.toFixed(1)}s` : `${Math.round(d / 60)}m`;
  }

  // ---- recordings (continuous segments) --------------------------------------

  // Recordings are 10s rolling segments from `cam` only (cam2 has none). Each
  // tile's poster is the segment's OWN first frame, extracted + cached server-
  // side (GET /wildlife/poster/...) — always the correct camera/frame. Clicking
  // a row plays the real segment via the /wildlife/media proxy.
  async function loadRecordings() {
    const countEl = $("wl-rec-count");
    let segs;
    try {
      const res = await fetch(
        apiUrl(`/api/segments?stream=${REC_STREAM}&limit=${REC_LIMIT}`),
        { cache: "no-store" },
      );
      if (!res.ok) throw new Error("HTTP " + res.status);
      segs = await res.json();
    } catch (err) {
      countEl.textContent = "fetch failed";
      return;
    }
    segs = Array.isArray(segs) ? segs : [];
    countEl.textContent = segs.length ? `${segs.length} shown` : "";
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

    // Poster = the segment's own extracted first frame; if it isn't available
    // (extraction failed), swap in a placeholder tile.
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
        if (!built) {
          detail.append(buildVideoPlayer(seg.url));
          built = true;
        }
      } else {
        expandedRecs.delete(seg.url);
      }
    };
    row.addEventListener("click", toggle);
    row.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        toggle();
      }
    });

    li.append(row, detail);
    if (expandedRecs.has(seg.url)) toggle();
    return li;
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

  // Mirrors the Pi's GET/PUT /api/settings keys. PUT merges, so the form only
  // sends fields present here. `orientation` is intentionally omitted — it's
  // tied to the still/clip rotation handling and shouldn't be a casual knob.
  const SETTING_FIELDS = [
    // Mode switching (day/night hysteresis)
    ["night_enter_lux", "number"],
    ["day_return_lux", "number"],
    ["lux_day", "number"],
    ["mode_min_dwell_s", "number"],
    // Metering
    ["meter_interval_s", "number"],
    ["meter_interval_fast_s", "number"],
    ["night_cal_k", "number"],
    // Night exposure
    ["night_shutter_us", "number"],
    ["night_gain", "number"],
    ["night_max_exposure_us", "number"],
    ["night_gain_ceiling", "number"],
    // Night burst
    ["night_burst_frames", "number"],
    ["night_burst_max_s", "number"],
    // Day capture
    ["day_burst_count", "number"],
    ["day_freeze_us", "number"],
    // Clip pre/post-roll
    ["preroll_s", "number"],
    ["postroll_s", "number"],
    // Image
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
    loadPir();
    loadEvents();
    loadRecordings();
  }

  function startPolling() {
    stopPolling();
    if ($("wl-auto").checked && !document.hidden) {
      pollTimer = setInterval(refreshAll, POLL_MS);
      // PIR is live state — poll it on its own faster cadence.
      pirTimer = setInterval(loadPir, PIR_POLL_MS);
    }
  }
  function stopPolling() {
    if (pollTimer) clearInterval(pollTimer);
    if (pirTimer) clearInterval(pirTimer);
    pollTimer = pirTimer = null;
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
