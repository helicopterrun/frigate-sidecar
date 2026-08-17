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

  function start() { if (!timer) timer = setInterval(tick, INTERVAL_MS); }
  function stop() { clearInterval(timer); timer = null; }
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) stop();
    else { tick(); start(); }
  });
  start();
})();
