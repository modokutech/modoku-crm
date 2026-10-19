/* Fix204: per-day training times.
 *
 * One time for every day stays the default. For a class (or quotation line)
 * running over more than one day, a "Different time on some days?" link opens
 * one row per day (date, start, end), pre-filled with the main time, so a
 * 1.5-day course can be day 1 9am-5pm, day 2 9am-1pm. Dates can be changed and
 * days added, for the rare class that isn't back to back (10 & 17 Nov).
 *
 * The rows live in a hidden input as JSON [{date, start, end}]; the server
 * takes the first/last date and the first day's time from it as well.
 *
 * DayTimes.attach(box, {start, end, timeStart, timeEnd, hidden, onChange})
 *   box: the element to render the link + rows into
 *   start/end/timeStart/timeEnd: the main date and time inputs
 *   hidden: the hidden input holding the JSON
 */
(function () {
  function iso(d) { return d.toISOString().slice(0, 10); }
  function addDays(isoDate, n) {
    const d = new Date(isoDate + 'T00:00:00Z');
    d.setUTCDate(d.getUTCDate() + n);
    return iso(d);
  }
  function label(isoDate) {
    const d = new Date(isoDate + 'T00:00:00Z');
    return d.toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short', timeZone: 'UTC' });
  }
  function read(hidden) {
    try { const v = JSON.parse(hidden.value || '[]'); return Array.isArray(v) ? v : []; } catch (e) { return []; }
  }

  function attach(box, o) {
    box.classList.add('day-times');
    box.innerHTML =
      '<a href="#" class="dt-open small">Different time on some days?</a>' +
      '<div class="dt-panel border rounded p-2 mt-1" style="display:none">' +
      '  <div class="dt-rows"></div>' +
      '  <div class="d-flex flex-wrap gap-2 mt-1 small">' +
      '    <a href="#" class="dt-add">+ Add a day</a>' +
      '    <a href="#" class="dt-close text-muted">Use the same time every day</a>' +
      '  </div>' +
      '</div>';
    const link = box.querySelector('.dt-open');
    const panel = box.querySelector('.dt-panel');
    const rowsBox = box.querySelector('.dt-rows');

    function rowHtml(day, i) {
      if (o.compact) {  // narrow spots (a quotation line's Time column)
        return '<div class="dt-row mb-2">' +
          '<div class="d-flex align-items-center gap-1 mb-1">' +
          '<span class="small text-muted text-nowrap">Day ' + (i + 1) + '</span>' +
          '<input type="date" class="form-control form-control-sm dt-date" value="' + (day.date || '') + '">' +
          '<button type="button" class="btn btn-sm btn-link text-danger p-0 dt-remove" title="Remove this day">✕</button>' +
          '</div>' +
          '<div class="d-flex gap-1">' +
          '<input type="time" class="form-control form-control-sm dt-start" value="' + (day.start || '') + '">' +
          '<input type="time" class="form-control form-control-sm dt-end" value="' + (day.end || '') + '">' +
          '</div><span class="dt-label d-none"></span></div>';
      }
      return '<div class="dt-row d-flex flex-wrap align-items-center gap-1 mb-1">' +
        '<span class="small text-muted" style="width:52px">Day ' + (i + 1) + '</span>' +
        '<input type="date" class="form-control form-control-sm dt-date" style="width:150px" value="' + (day.date || '') + '">' +
        '<input type="time" class="form-control form-control-sm dt-start" style="width:120px" value="' + (day.start || '') + '">' +
        '<span class="small">to</span>' +
        '<input type="time" class="form-control form-control-sm dt-end" style="width:120px" value="' + (day.end || '') + '">' +
        '<span class="small text-muted dt-label"></span>' +
        '<button type="button" class="btn btn-sm btn-link text-danger p-0 dt-remove" title="Remove this day">✕</button>' +
        '</div>';
    }
    function collect() {
      return Array.from(rowsBox.querySelectorAll('.dt-row')).map(function (r) {
        return { date: r.querySelector('.dt-date').value, start: r.querySelector('.dt-start').value, end: r.querySelector('.dt-end').value };
      }).filter(function (d) { return d.date; });
    }
    function render(days) {
      rowsBox.innerHTML = days.map(rowHtml).join('');
      rowsBox.querySelectorAll('.dt-row').forEach(function (r) {
        const v = r.querySelector('.dt-date').value;
        r.querySelector('.dt-label').textContent = v ? label(v) : '';
      });
    }
    // Rows for start..end, keeping times already set for a date.
    function fromRange() {
      const s = o.start.value, e = o.end.value || o.start.value;
      if (!s) return [];
      const known = {};
      collect().forEach(function (d) { known[d.date] = d; });
      const days = [];
      for (let d = s, n = 0; d <= e && n < 62; d = addDays(d, 1), n++) {
        days.push(known[d] || { date: d, start: o.timeStart.value, end: o.timeEnd.value });
      }
      return days;
    }
    function isOpen() { return panel.style.display !== 'none'; }
    // Write the hidden JSON and keep the main fields in step (first/last
    // date, first day's time).
    function save() {
      if (!isOpen()) { o.hidden.value = ''; refreshLink(); if (o.onChange) o.onChange(); return; }
      const days = collect().sort(function (a, b) { return a.date < b.date ? -1 : 1; });
      o.hidden.value = days.length > 1 ? JSON.stringify(days) : '';
      if (days.length) {
        o.start.value = days[0].date;
        o.end.value = days.length > 1 ? days[days.length - 1].date : '';
        o.timeStart.value = days[0].start;
        o.timeEnd.value = days[0].end;
      }
      rowsBox.querySelectorAll('.dt-row').forEach(function (r) {
        const v = r.querySelector('.dt-date').value;
        r.querySelector('.dt-label').textContent = v ? label(v) : '';
      });
      if (o.onChange) o.onChange();
    }
    function refreshLink() {
      const multi = o.start.value && o.end.value && o.end.value > o.start.value;
      link.style.display = (!isOpen() && multi) ? '' : 'none';
    }
    function open(days) {
      panel.style.display = '';
      render(days && days.length ? days : fromRange());
      [o.timeStart, o.timeEnd].forEach(function (el) { el.readOnly = true; el.classList.add('bg-light'); });
      save();
      refreshLink();
    }
    function close() {
      panel.style.display = 'none';
      rowsBox.innerHTML = '';
      [o.timeStart, o.timeEnd].forEach(function (el) { el.readOnly = false; el.classList.remove('bg-light'); });
      save();
    }

    link.addEventListener('click', function (e) { e.preventDefault(); open(); });
    box.querySelector('.dt-close').addEventListener('click', function (e) { e.preventDefault(); close(); });
    box.querySelector('.dt-add').addEventListener('click', function (e) {
      e.preventDefault();
      const days = collect();
      const last = days.length ? days[days.length - 1] : { date: o.start.value, start: o.timeStart.value, end: o.timeEnd.value };
      days.push({ date: last.date ? addDays(last.date, 1) : '', start: last.start, end: last.end });
      render(days);
      save();
    });
    rowsBox.addEventListener('click', function (e) {
      if (!e.target.classList.contains('dt-remove')) return;
      e.target.closest('.dt-row').remove();
      if (collect().length < 2) close(); else { render(collect()); save(); }
    });
    rowsBox.addEventListener('change', save);
    // Changing the main dates while the rows are open rebuilds them
    // (keeping the times already set for a date).
    [o.start, o.end].forEach(function (el) {
      el.addEventListener('change', function () {
        if (isOpen()) {
          if (!(o.end.value && o.end.value > o.start.value)) { close(); return; }
          render(fromRange());
          save();
        }
        refreshLink();
      });
    });

    const saved = read(o.hidden);
    if (saved.length > 1) open(saved); else refreshLink();
    return { open: open, close: close, refresh: refreshLink, isOpen: isOpen };
  }

  window.DayTimes = { attach: attach };
})();
