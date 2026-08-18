// Status dashboard refresh: poll /status.json every ~10s and patch the
// [data-k] cells in place — no HTML re-render, so focus/scroll survive.
// Paused while the tab is hidden.
(function () {
  var INTERVAL_MS = 10000;
  var timer = null;

  function fmtBytes(n) {
    if (n === null || n === undefined) return "—";
    var units = ["B", "KB", "MB", "GB", "TB"];
    var size = n;
    for (var i = 0; i < units.length; i++) {
      if (size < 1024 || i === units.length - 1) {
        return (i <= 1 ? Math.round(size) : size.toFixed(1)) + " " + units[i];
      }
      size /= 1024;
    }
  }

  function setCell(k, text, cls) {
    var n = document.querySelector('[data-k="' + k + '"]');
    if (!n) return;
    n.textContent = text;
    if (cls !== undefined) n.className = "stat-value cell-class " + cls;
  }

  function lagHtml(lag) {
    if (lag === null || lag === undefined) return "—";
    if (lag < 120) return '<span class="cell-class ok">' + Math.round(lag) + "s</span>";
    if (lag < 600) return '<span class="cell-class warn">' + Math.round(lag) + "s</span>";
    return '<span class="cell-class noise">' + Math.round(lag / 60) + "m</span>";
  }

  async function tick() {
    var s;
    try {
      s = await SC.fetchJson("/status.json");
    } catch (e) {
      return; // transient — try again next tick
    }
    setCell("frigate", s.frigate.reachable ? s.frigate.version : "unreachable",
      s.frigate.reachable ? "ok" : "noise");
    setCell("scrub", s.scrub.enabled ? "on" : "off", s.scrub.enabled ? "ok" : "muted");
    setCell("push", s.push.enabled ? "on" : "off", s.push.enabled ? "ok" : "muted");
    setCell("devices", String(s.push.device_count));
    setCell("last-cycle", s.scrub.last_cycle_s_ago !== null && s.scrub.last_cycle_s_ago !== undefined
      ? s.scrub.last_cycle_s_ago + "s ago" : "—");
    if (s.scrub.sheet_count !== undefined) setCell("sheets", String(s.scrub.sheet_count));
    setCell("scrub-cache", fmtBytes(s.sizes.scrub_cache));
    setCell("mqtt", s.push.mqtt_connected ? "live" : "stale", s.push.mqtt_connected ? "ok" : "noise");
    setCell("frigate-online", s.push.frigate_online ? "online" : "offline",
      s.push.frigate_online ? "ok" : "noise");
    setCell("last-traffic", s.push.last_event_s_ago !== null && s.push.last_event_s_ago !== undefined
      ? s.push.last_event_s_ago + "s ago" : "—");
    setCell("sidecar-db", fmtBytes(s.sizes.sidecar_db));
    setCell("frigate-db", fmtBytes(s.sizes.frigate_db));
    (s.scrub.cameras || []).forEach(function (c) {
      var cell = document.querySelector('#scrub-cams td[data-cam="' + c.camera + '"]');
      if (cell) cell.innerHTML = lagHtml(c.lag_s);
    });
  }

  // --- Camera snapshots + recent Frigate events (30s cadence) -------------
  var MEDIA_INTERVAL_MS = 30000;
  var mediaTimer = null;

  function refreshSnaps() {
    var imgs = document.querySelectorAll("[data-cam-snap]");
    var t = Date.now();
    for (var i = 0; i < imgs.length; i++) {
      var cam = imgs[i].getAttribute("data-cam-snap");
      imgs[i].src = "/api/" + encodeURIComponent(cam) + "/latest.jpg?h=270&t=" + t;
    }
  }

  function relTime(epoch) {
    var s = Math.max(0, Math.round(Date.now() / 1000 - epoch));
    if (s < 90) return s + "s ago";
    if (s < 5400) return Math.round(s / 60) + "m ago";
    if (s < 172800) return Math.round(s / 3600) + "h ago";
    return Math.round(s / 86400) + "d ago";
  }

  async function refreshEvents() {
    var strip = document.getElementById("event-strip");
    if (!strip) return;
    var events;
    try {
      events = await SC.fetchJson("/api/events?limit=8");
    } catch (e) {
      return; // transient — keep whatever is showing
    }
    if (!Array.isArray(events)) return;
    strip.textContent = "";
    if (!events.length) {
      var none = document.createElement("span");
      none.className = "help";
      none.textContent = "no recent events";
      strip.appendChild(none);
      return;
    }
    events.forEach(function (ev) {
      var a = document.createElement("a");
      a.className = "event-card";
      a.href = "/live/" + encodeURIComponent(ev.camera);
      a.title = "Open " + ev.camera + " live stream";
      var img = document.createElement("img");
      img.src = "/api/events/" + encodeURIComponent(ev.id) + "/thumbnail.jpg";
      img.alt = ev.label + " on " + ev.camera;
      img.loading = "lazy";
      var meta = document.createElement("span");
      meta.className = "event-meta";
      var label = ev.sub_label || ev.label || "?";
      var live = !ev.end_time;
      meta.textContent = label + " · " + ev.camera + " · " +
        (live ? "live now" : relTime(ev.start_time));
      if (live) a.className += " live";
      a.appendChild(img);
      a.appendChild(meta);
      strip.appendChild(a);
    });
  }

  function mediaTick() { refreshSnaps(); refreshEvents(); }

  function start() {
    if (!timer) timer = setInterval(tick, INTERVAL_MS);
    if (!mediaTimer) mediaTimer = setInterval(mediaTick, MEDIA_INTERVAL_MS);
  }
  function stop() {
    clearInterval(timer); timer = null;
    clearInterval(mediaTimer); mediaTimer = null;
  }
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) stop();
    else { tick(); mediaTick(); start(); }
  });
  start();
  refreshEvents();
})();
