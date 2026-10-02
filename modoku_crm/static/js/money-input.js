/* Fix112: thousands separators in amount fields, as you type.
 *
 * Any <input data-money> shows 1700 as 1,700, 170000 as 170,000 (up to two
 * decimals), keeping the cursor where it was. Leaving the field tidies it
 * (10500.0 -> 10,500; 10500.5 -> 10,500.50).
 *
 * Quotation / invoice amounts are numbers on the server, so on submit the
 * commas are stripped again (data-money). JD14's fee fields are text printed
 * straight onto the form, so they keep their commas (data-money="keep").
 *
 * Page scripts read an amount with moneyValue(input) instead of parseFloat,
 * and call moneyFormat(input) after setting .value themselves.
 */
(function () {
  function shape(raw) {
    var s = String(raw == null ? '' : raw).replace(/[^\d.]/g, '');
    var dot = s.indexOf('.');
    if (dot !== -1) s = s.slice(0, dot + 1) + s.slice(dot + 1).replace(/\./g, '');
    var parts = s.split('.');
    var whole = parts[0].replace(/^0+(?=\d)/, '');
    if (whole === '' && parts.length > 1) whole = '0';
    whole = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    return parts.length > 1 ? whole + '.' + parts[1].slice(0, 2) : whole;
  }

  function tidy(raw) {
    var n = parseFloat(String(raw).replace(/,/g, ''));
    if (isNaN(n)) return '';
    return n.toLocaleString('en-US', {
      minimumFractionDigits: Number.isInteger(n) ? 0 : 2,
      maximumFractionDigits: 2,
    });
  }

  function moneyValue(input) {
    return parseFloat(String(input.value).replace(/,/g, '')) || 0;
  }

  function moneyFormat(input) {
    input.value = tidy(input.value);
  }

  function onInput(input) {
    var before = input.value;
    var caret = input.selectionStart == null ? before.length : input.selectionStart;
    // Keep the caret after the same number of digits/dots it was after.
    var kept = before.slice(0, caret).replace(/[^\d.]/g, '').length;
    var after = shape(before);
    if (after === before) return;
    input.value = after;
    var pos = 0, seen = 0;
    while (pos < after.length && seen < kept) {
      if (/[\d.]/.test(after[pos])) seen++;
      pos++;
    }
    try { input.setSelectionRange(pos, pos); } catch (e) { /* not focusable */ }
  }

  function prepare(input) {
    if (input.type === 'number') input.type = 'text';
    input.setAttribute('inputmode', 'decimal');
    input.setAttribute('autocomplete', 'off');
    if (input.value !== '') moneyFormat(input);
  }

  // Capture phase: runs before a page's own "input" handlers, so their
  // totals see the formatted value (and read it through moneyValue).
  document.addEventListener('input', function (e) {
    if (e.target.matches && e.target.matches('input[data-money]')) onInput(e.target);
  }, true);
  document.addEventListener('blur', function (e) {
    if (e.target.matches && e.target.matches('input[data-money]') && e.target.value !== '') moneyFormat(e.target);
  }, true);
  document.addEventListener('submit', function (e) {
    e.target.querySelectorAll('input[data-money]').forEach(function (input) {
      if (input.dataset.money !== 'keep') input.value = String(input.value).replace(/,/g, '');
    });
  }, true);

  function init(root) {
    (root || document).querySelectorAll('input[data-money]').forEach(prepare);
  }
  // Now, for the fields already on the page (this script is loaded after
  // the form), and again once the page has loaded, for any added later.
  init();
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { init(); });
  }

  window.moneyValue = moneyValue;
  window.moneyFormat = moneyFormat;
  window.moneyInit = init;
})();
