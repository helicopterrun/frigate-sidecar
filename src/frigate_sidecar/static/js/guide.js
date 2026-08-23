// Guide interactivity: live stat fill, walkthrough checklist persistence,
// and the index topic filter. Stats come from one JSON fetch so topic HTML
// stays a pure markdown render (guide.py).

(function () {
  // --- Live stats: fill every {{stat:...}} placeholder on the page.
  var statSpans = document.querySelectorAll('.guide-stat');
  if (statSpans.length) {
    SC.fetchJson('/guide/stats.json').then(function (data) {
      statSpans.forEach(function (span) {
        var v = data.stats[span.dataset.stat];
        if (v !== undefined) span.textContent = v;
      });
    }).catch(function () { /* placeholders keep their dash */ });
  }

  // --- Walkthrough checklists: progress per topic in localStorage.
  var page = document.querySelector('.guide-page');
  var slug = page ? page.dataset.slug : null;

  function storeKey(walkIndex) {
    return 'sidecar.guide.' + slug + '.' + walkIndex;
  }

  document.querySelectorAll('.walkthrough').forEach(function (walk) {
    if (!slug) return;
    var key = storeKey(walk.dataset.walkthrough);
    var boxes = walk.querySelectorAll('input[type="checkbox"]');
    var progress = walk.querySelector('.walkthrough-progress');

    var saved = [];
    try { saved = JSON.parse(localStorage.getItem(key) || '[]'); } catch (e) {}
    boxes.forEach(function (box) {
      if (saved.indexOf(Number(box.dataset.step)) !== -1) box.checked = true;
    });

    function update(persist) {
      var checked = [];
      boxes.forEach(function (box) {
        if (box.checked) checked.push(Number(box.dataset.step));
      });
      progress.textContent = checked.length + '/' + boxes.length + ' done';
      if (persist) {
        try { localStorage.setItem(key, JSON.stringify(checked)); } catch (e) {}
      }
    }
    update(false);

    boxes.forEach(function (box) {
      box.addEventListener('change', function () { update(true); });
    });
    walk.querySelector('.walkthrough-reset').addEventListener('click', function () {
      boxes.forEach(function (box) { box.checked = false; });
      update(true);
      SC.toast('walkthrough reset');
    });
  });

  // --- Index filter: hide topics whose title doesn't match; hide emptied
  // sections so the list stays scannable.
  var filter = document.getElementById('guide-filter');
  if (filter) {
    filter.addEventListener('input', function () {
      var q = filter.value.trim().toLowerCase();
      document.querySelectorAll('.guide-section').forEach(function (section) {
        var any = false;
        section.querySelectorAll('li[data-title]').forEach(function (li) {
          var hit = !q || li.dataset.title.indexOf(q) !== -1;
          li.style.display = hit ? '' : 'none';
          if (hit) any = true;
        });
        section.style.display = any ? '' : 'none';
      });
    });
  }
})();
