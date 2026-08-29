// Search-box type-ahead: suggests creators and genres as you type.
(function () {
  var input = document.getElementById('q');
  var box = document.getElementById('suggest');
  if (!input || !box) return;
  var timer, items = [], cursor = -1;

  function close() { box.hidden = true; items = []; cursor = -1; }

  function render(list, term) {
    if (!list.length) return close();
    box.innerHTML = list.map(function (s) {
      var href = (s.type === 'creator' ? '/?creator=' : '/?genre=') + encodeURIComponent(s.label);
      return '<a href="' + href + '"><span><span class="k">' + s.type + '</span> ' +
             s.label.replace(/</g, '&lt;') + '</span><span class="k">' + s.count + '</span></a>';
    }).join('');
    items = Array.prototype.slice.call(box.querySelectorAll('a'));
    cursor = -1;
    box.hidden = false;
  }

  input.addEventListener('input', function () {
    var term = input.value.trim();
    clearTimeout(timer);
    if (term.length < 2) return close();
    // Debounced so a fast typist doesn't fire a request per keystroke.
    timer = setTimeout(function () {
      fetch('/api/suggest?q=' + encodeURIComponent(term))
        .then(function (r) { return r.json(); })
        .then(function (l) { render(l, term); })
        .catch(close);
    }, 140);
  });

  input.addEventListener('keydown', function (e) {
    if (box.hidden || !items.length) return;
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      if (cursor >= 0) items[cursor].classList.remove('on');
      cursor = (cursor + (e.key === 'ArrowDown' ? 1 : items.length - 1)) % items.length;
      items[cursor].classList.add('on');
    } else if (e.key === 'Enter' && cursor >= 0) {
      e.preventDefault();
      window.location = items[cursor].getAttribute('href');
    } else if (e.key === 'Escape') {
      close();
    }
  });

  document.addEventListener('click', function (e) {
    if (!box.contains(e.target) && e.target !== input) close();
  });
})();

// Find-in-transcript: filters the timecoded lines down to matches.
(function () {
  var find = document.getElementById('tfind');
  var wrap = document.getElementById('tlines');
  if (!find || !wrap) return;
  var lines = Array.prototype.slice.call(wrap.querySelectorAll('.tline'));
  lines.forEach(function (l) { l.dataset.text = l.textContent.toLowerCase(); });

  find.addEventListener('input', function () {
    var term = find.value.trim().toLowerCase();
    lines.forEach(function (l) {
      l.classList.toggle('hide', !!term && l.dataset.text.indexOf(term) === -1);
    });
  });
})();

// Client-side filter for the creators table.
(function () {
  var find = document.getElementById('filterlist');
  var table = document.getElementById('creatortbl');
  if (!find || !table) return;
  var rows = Array.prototype.slice.call(table.querySelectorAll('tbody tr'));
  rows.forEach(function (r) { r.dataset.text = r.textContent.toLowerCase(); });

  find.addEventListener('input', function () {
    var term = find.value.trim().toLowerCase();
    rows.forEach(function (r) {
      r.style.display = (!term || r.dataset.text.indexOf(term) !== -1) ? '' : 'none';
    });
  });
})();

// "/" focuses search from anywhere, as long as you're not already typing.
document.addEventListener('keydown', function (e) {
  if (e.key !== '/' || /input|textarea/i.test(e.target.tagName)) return;
  var q = document.getElementById('q');
  if (q) { e.preventDefault(); q.focus(); q.select(); }
});
