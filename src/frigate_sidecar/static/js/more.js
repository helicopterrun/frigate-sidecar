(function () {
  var btn = document.getElementById('more-btn');
  var sheet = document.getElementById('more-sheet');
  var backdrop = document.getElementById('more-backdrop');
  if (!btn || !sheet) return;

  function toggle() {
    var open = sheet.classList.toggle('open');
    backdrop.classList.toggle('open', open);
    btn.setAttribute('aria-expanded', open);
  }

  btn.addEventListener('click', toggle);
  backdrop.addEventListener('click', toggle);
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && sheet.classList.contains('open')) toggle();
  });
})();
