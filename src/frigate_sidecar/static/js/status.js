// Status dashboard refresh: re-render the page every ~10s. The payload is
// cheap and the page is server-rendered, so the simplest correct refresh is a
// full reload of the main content via fetch + document.write-free swap --
// we just re-request the page and replace <main>.
(function () {
  var INTERVAL_MS = 10000;
  async function tick() {
    try {
      var resp = await fetch(location.pathname + location.search, {
        headers: { Accept: "text/html" },
      });
      if (!resp.ok) return;
      var text = await resp.text();
      var doc = new DOMParser().parseFromString(text, "text/html");
      var fresh = doc.getElementById("status-root");
      var current = document.getElementById("status-root");
      if (fresh && current) current.replaceWith(fresh);
    } catch (e) {
      /* transient -- try again next tick */
    }
  }
  setInterval(tick, INTERVAL_MS);
})();
