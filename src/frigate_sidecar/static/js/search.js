// Header event search. There are two instances of the form -- one in the
// desktop header, one in the phone "More" sheet (the header is display:none
// under 720px) -- so this binds every .search-form rather than the first
// .search-input on the page. Each form owns the .search-drop inside it.
(function () {
  var forms = document.querySelectorAll('.search-form');
  if (!forms.length) return;

  function escHtml(s) {
    var d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
  }

  function render(drop, events, q) {
    if (!events.length) {
      drop.innerHTML = '<div class="search-empty">No results for "' + escHtml(q) + '"</div>';
      drop.hidden = false;
      return;
    }
    var html = '';
    events.forEach(function (ev) {
      var t = new Date(ev.start_time * 1000);
      var when = t.toLocaleDateString(undefined, {month: 'short', day: 'numeric'})
        + ' ' + t.toLocaleTimeString(undefined, {hour: '2-digit', minute: '2-digit'});
      var thumb = ev.has_snapshot
        ? '<img src="/api/events/' + encodeURIComponent(ev.id)
          + '/snapshot.jpg?quality=50&h=40" alt="" class="search-thumb">'
        : '';
      // The event detail page is /event/{id}. /triage/{id} is not a route, and
      // the proxy catch-all would answer it with Frigate's own SPA at HTTP 200
      // rather than a 404 -- a dead link that looks like a working one.
      html += '<a href="/event/' + encodeURIComponent(ev.id) + '" class="search-result">'
        + thumb
        + '<span class="search-meta">'
        + '<span class="search-label">' + escHtml(ev.label) + '</span>'
        + '<span class="search-camera">' + escHtml(ev.camera) + ' · ' + when + '</span>'
        + '</span></a>';
    });
    drop.innerHTML = html;
    drop.hidden = false;
  }

  function bind(form) {
    var input = form.querySelector('.search-input');
    var drop = form.querySelector('.search-drop');
    if (!input || !drop) return;

    var timer = null;

    function search(q) {
      if (!q || q.length < 2) { drop.hidden = true; return; }
      fetch('/v1/events/search?limit=8&q=' + encodeURIComponent(q))
        .then(function (r) { return r.json(); })
        .then(function (events) { render(drop, events, q); })
        .catch(function () { drop.hidden = true; });
    }

    input.addEventListener('input', function () {
      clearTimeout(timer);
      var q = input.value.trim();
      timer = setTimeout(function () { search(q); }, 300);
    });

    input.addEventListener('focus', function () {
      if (input.value.trim().length >= 2) search(input.value.trim());
    });

    input.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') { drop.hidden = true; input.blur(); }
    });

    // Any click outside *this* form closes its own dropdown.
    document.addEventListener('click', function (e) {
      if (!form.contains(e.target)) drop.hidden = true;
    });
  }

  Array.prototype.forEach.call(forms, bind);
})();
