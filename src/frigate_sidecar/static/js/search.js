(function () {
  var input = document.querySelector('.search-input');
  var drop = document.getElementById('search-drop');
  if (!input || !drop) return;

  var timer = null;

  function search(q) {
    if (!q || q.length < 2) { drop.hidden = true; return; }
    fetch('/v1/events/search?limit=8&q=' + encodeURIComponent(q))
      .then(function (r) { return r.json(); })
      .then(function (events) { render(events, q); })
      .catch(function () { drop.hidden = true; });
  }

  function render(events, q) {
    if (!events.length) {
      drop.innerHTML = '<div class="search-empty">No results for "' + escHtml(q) + '"</div>';
      drop.hidden = false;
      return;
    }
    var html = '';
    events.forEach(function (ev) {
      var t = new Date(ev.start_time * 1000);
      var when = t.toLocaleDateString(undefined, {month:'short', day:'numeric'})
        + ' ' + t.toLocaleTimeString(undefined, {hour:'2-digit', minute:'2-digit'});
      var thumb = ev.has_snapshot
        ? '<img src="/api/events/' + ev.id + '/snapshot.jpg?quality=50&h=40" alt="" class="search-thumb">'
        : '';
      html += '<a href="/triage/' + ev.id + '" class="search-result">'
        + thumb
        + '<span class="search-meta">'
        + '<span class="search-label">' + escHtml(ev.label) + '</span>'
        + '<span class="search-camera">' + escHtml(ev.camera) + ' · ' + when + '</span>'
        + '</span></a>';
    });
    drop.innerHTML = html;
    drop.hidden = false;
  }

  function escHtml(s) {
    var d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
  }

  input.addEventListener('input', function () {
    clearTimeout(timer);
    var q = input.value.trim();
    timer = setTimeout(function () { search(q); }, 300);
  });

  input.addEventListener('focus', function () {
    if (input.value.trim().length >= 2) search(input.value.trim());
  });

  document.addEventListener('click', function (e) {
    if (!e.target.closest('.search-form')) drop.hidden = true;
  });

  input.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') { drop.hidden = true; input.blur(); }
  });
})();
