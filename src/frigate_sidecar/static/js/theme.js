// Theme picker: mirrors the Elsinore app's five palettes. The <html>
// data-theme attribute is applied inline in base.html's <head> (before CSS
// paints) to avoid a flash of the default theme; this file only wires the
// picker.
(function () {
  var KEY = "sidecar.theme";
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
  });
})();
