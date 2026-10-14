/* Fix176: public pages (trainer / client / participant links, login).
 *
 * 1. Any form: the first click shows a spinner on its button and further
 *    clicks are ignored, so a slow page doesn't lead to double submissions.
 * 2. A form with files chosen: uploads with a progress screen ("Uploading 2
 *    files... 45%"), then "Upload complete, finishing up", then shows the
 *    page the server answers with - so it's obvious the step finished.
 *
 * Runs after the page's own submit handlers (it listens on document), and
 * leaves alone anything they cancelled (empty signature, "Remove this file?"
 * answered No), forms that open in a new tab, and forms marked .js-no-busy.
 */
(function () {
  function busyLabel(form, btn, hasFiles) {
    return (btn && btn.dataset.busyText) || (hasFiles ? 'Uploading…' : 'Please wait…');
  }

  function lockButtons(form, label, submitter) {
    var buttons = form.querySelectorAll('button[type="submit"], button:not([type]), input[type="submit"]');
    var main = submitter || buttons[0];
    // Disable after this tick: disabling during the submit event would drop
    // the clicked button's name/value (e.g. Confirm vs Reject) from the post.
    setTimeout(function () {
      buttons.forEach(function (b) { b.disabled = true; });
      if (main && main.tagName === 'BUTTON') {
        main.innerHTML = '<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>' + label;
      }
    }, 0);
  }

  function unlock(form) {
    delete form.dataset.sent;
    form.querySelectorAll('button, input[type="submit"]').forEach(function (b) {
      b.disabled = false;
      if (b.dataset.originalHtml) { b.innerHTML = b.dataset.originalHtml; }
    });
  }

  function fmtMB(bytes) { return (bytes / 1048576).toFixed(bytes < 10485760 ? 1 : 0); }

  function overlay() {
    var el = document.createElement('div');
    el.id = 'uploadOverlay';
    el.setAttribute('role', 'status');
    el.style.cssText = 'position:fixed;inset:0;z-index:2000;background:rgba(15,23,42,.55);display:flex;'
      + 'align-items:center;justify-content:center;padding:16px;';
    el.innerHTML =
      '<div class="card shadow p-4 text-center" style="max-width:420px;width:100%;border-radius:16px;">'
      + '<div class="mb-2" id="upIcon"><span class="spinner-border text-success" role="status" aria-hidden="true"></span></div>'
      + '<h5 class="mb-1" id="upTitle">Uploading…</h5>'
      + '<p class="small text-muted mb-3" id="upText">Please keep this page open.</p>'
      + '<div class="progress" style="height:10px;" id="upBarWrap">'
      + '<div class="progress-bar bg-success" id="upBar" style="width:0%"></div></div>'
      + '<div class="small text-muted mt-2" id="upDetail"></div>'
      + '</div>';
    document.body.appendChild(el);
    return {
      el: el,
      set: function (title, text, pct, detail) {
        if (title !== null) el.querySelector('#upTitle').textContent = title;
        if (text !== null) el.querySelector('#upText').textContent = text;
        if (pct !== null) {
          el.querySelector('#upBar').style.width = pct + '%';
          el.querySelector('#upBar').classList.toggle('progress-bar-striped', pct >= 100);
          el.querySelector('#upBar').classList.toggle('progress-bar-animated', pct >= 100);
        }
        if (detail !== null) el.querySelector('#upDetail').textContent = detail;
      },
      fail: function (message) {
        el.querySelector('#upIcon').innerHTML = '<i class="bi bi-exclamation-triangle-fill text-danger" style="font-size:2rem;"></i>';
        el.querySelector('#upTitle').textContent = "Upload didn't go through";
        el.querySelector('#upText').textContent = message;
        el.querySelector('#upBarWrap').style.display = 'none';
        el.querySelector('#upDetail').innerHTML = '<button type="button" class="btn btn-sm btn-outline-secondary mt-2" id="upClose">OK, try again</button>';
      }
    };
  }

  function uploadWithProgress(form, submitter, files) {
    var total = files.reduce(function (n, f) { return n + f.size; }, 0);
    var count = files.length;
    var ov = overlay();
    var noun = count === 1 ? '1 file' : count + ' files';
    ov.set('Uploading ' + noun + '…', 'Please keep this page open until it says done.', 0, '0 of ' + fmtMB(total) + ' MB');

    var data;
    try { data = new FormData(form, submitter || undefined); }
    catch (err) {
      data = new FormData(form);
      if (submitter && submitter.name) data.append(submitter.name, submitter.value);
    }

    var xhr = new XMLHttpRequest();
    xhr.open((form.getAttribute('method') || 'post').toUpperCase(), form.action || window.location.href);
    xhr.upload.onprogress = function (e) {
      if (!e.lengthComputable) return;
      var pct = Math.min(99, Math.round(e.loaded / e.total * 100));
      ov.set(null, null, pct, pct + '% · ' + fmtMB(e.loaded) + ' of ' + fmtMB(e.total) + ' MB');
    };
    xhr.upload.onload = function () {
      ov.set('Upload complete', 'Finishing up… please keep this page open a moment longer.', 100, 'Almost done');
    };
    xhr.onload = function () {
      if (xhr.status === 413) {
        ov.fail("That's too much to upload in one go. Try fewer files at a time, or smaller photos.");
      } else if (xhr.status >= 500) {
        ov.fail('Something went wrong on our side. Please try again in a moment.');
      } else {
        // Show the page the server answered with (the success page, or the
        // form again with its message), as a normal page load would.
        // Only take the new address when the server redirected; a page
        // rendered straight from the POST keeps the form's address, so a
        // refresh doesn't land on a POST-only URL.
        var action = new URL(form.action || window.location.href, window.location.href).href;
        if (xhr.responseURL && xhr.responseURL !== action) {
          try { history.replaceState(null, '', xhr.responseURL); } catch (e) { /* ignore */ }
        }
        document.open();
        document.write(xhr.responseText);
        document.close();
        window.scrollTo(0, 0);
        return;
      }
      wireClose(ov, form);
    };
    xhr.onerror = xhr.ontimeout = function () {
      ov.fail('Check your internet connection and try again. Nothing was lost; your files just need sending again.');
      wireClose(ov, form);
    };
    xhr.send(data);
  }

  function wireClose(ov, form) {
    var btn = ov.el.querySelector('#upClose');
    if (btn) btn.addEventListener('click', function () { ov.el.remove(); unlock(form); });
  }

  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!(form instanceof HTMLFormElement) || form.classList.contains('js-no-busy')) return;
    if (e.defaultPrevented) return;  // the page's own check stopped it
    var submitter = e.submitter || null;
    var target = (submitter && submitter.getAttribute('formtarget')) || form.getAttribute('target');
    if (target && target !== '_self') return;
    if (form.dataset.sent) { e.preventDefault(); return; }
    form.dataset.sent = '1';

    var files = [];
    form.querySelectorAll('input[type="file"]').forEach(function (inp) {
      Array.prototype.forEach.call(inp.files || [], function (f) { files.push(f); });
    });
    var btn = submitter || form.querySelector('button[type="submit"], button:not([type])');
    form.querySelectorAll('button').forEach(function (b) { b.dataset.originalHtml = b.innerHTML; });
    lockButtons(form, busyLabel(form, btn, files.length > 0), btn);

    if (files.length && window.XMLHttpRequest && window.FormData) {
      e.preventDefault();
      uploadWithProgress(form, submitter, files);
    }
  });

  // Back button returning to a page with a locked button: reload it fresh.
  window.addEventListener('pageshow', function (e) { if (e.persisted) window.location.reload(); });
})();
