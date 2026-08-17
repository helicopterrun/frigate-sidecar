// Theme picker: mirrors the Elsinore app's five palettes. The <html>
// data-theme attribute is applied inline in base.html's <head> (before CSS
// paints) to avoid a flash of the default theme; this file only wires the
// picker.
(function () {
  var KEY = "sidecar.theme";

  // Keep the browser-chrome color (iOS Safari toolbar / status bar) in step
  // with the active theme's --surface.
  var SURFACES = {
    "": "#1C1D24",
    "kronborg-signal": "#0A2845",
    "moss-terracotta": "#27362A",
    "oresund-harbor": "#11324D",
    "midnight-fjord": "#0C2335",
  };
  function syncThemeColor() {
    var meta = document.querySelector('meta[name="theme-color"]');
    if (!meta) return;
    var theme = document.documentElement.getAttribute("data-theme") || "";
    meta.setAttribute("content", SURFACES[theme] || SURFACES[""]);
  }
  syncThemeColor();

  var picker = document.getElementById("theme-picker");
  if (!picker) return;
  // Elsinore became the default (value "") on 2026-08-15; migrate the old
  // explicit selection so the picker doesn't show a stale unknown value.
  if (localStorage.getItem(KEY) === "elsinore") {
    localStorage.removeItem(KEY);
    document.documentElement.removeAttribute("data-theme");
  }
  picker.value = localStorage.getItem(KEY) || "";
  picker.addEventListener("change", function () {
    if (picker.value) {
      localStorage.setItem(KEY, picker.value);
      document.documentElement.setAttribute("data-theme", picker.value);
    } else {
      localStorage.removeItem(KEY);
      document.documentElement.removeAttribute("data-theme");
    }
    syncThemeColor();
  });
})();
