/* Markdown editor widget (md1) — toolbar, shortcuts, live preview, refs.
 *
 * Attaches to any textarea carrying data-md-editor (see tokamak/pages.py
 * creation forms). No build step, no deps: plain DOM + fetch. The widget
 * talks to two read-only endpoints (tokamak/md_views.py):
 *   POST /markdown/preview/  — render the current text server-side so the
 *        preview shows exactly what will be stored-rendered later, refs and
 *        all. CSRF token is taken from the cookie every page already sets.
 *   GET  /markdown/suggest/  — name autocomplete while typing "#", "ticket:"
 *        or "project:"; Enter/Tab accepts the highlighted suggestion.
 */
(function () {
  'use strict';

  var PREVIEW_URL = '/markdown/preview/';
  var SUGGEST_URL = '/markdown/suggest/';
  var DEBOUNCE_MS = 300;   // preview waits this long after last keystroke
  var MAX_CHARS = 64000;   // mirror tokamak.markdown.MAX_INPUT_CHARS clamp

  /* ---------- CSRF helper ------------------------------------------------ */
  function csrfToken() {
    var m = document.cookie.match(/(?:^|;\s*)csrfmiddlewaretoken=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : '';
  }

  /* ---------- editing primitives (undo-friendly where possible) --------- */
  function applyEdit(ta, before, after, sel) {
    // Wrap the selection, or insert a placeholder and select it instead.
    var start = ta.selectionStart, end = ta.selectionEnd;
    var hadSel = start !== end;
    var inner = ta.value.slice(start, end);
    if (!hadSel && !inner && sel) inner = sel;
    var text = before + inner + after;
    insertText(ta, text);
    // Re-highlight the inner part so typing replaces a placeholder.
    ta.selectionStart = start + before.length;
    ta.selectionEnd = start + before.length + inner.length;
    ta.dispatchEvent(new Event('input', { bubbles: true }));
  }

  function insertText(ta, text) {
    // execCommand keeps native undo working in most browsers; fall back to
    // direct value splicing (undo degrades, edit still lands).
    ta.focus();
    try {
      if (document.execCommand('insertText', false, text)) return;
    } catch (err) { /* fall through */ }
    var s = ta.selectionStart, e = ta.selectionEnd;
    ta.value = ta.value.slice(0, s) + text + ta.value.slice(e);
    ta.selectionStart = ta.selectionEnd = s + text.length;
  }

  function prefixLines(ta, marker) {
    var s = ta.selectionStart, e = ta.selectionEnd;
    var startLine = ta.value.lastIndexOf('\n', s - 1) + 1;
    var lines = ta.value.slice(startLine, e).split('\n');
    var allOn = true, i;
    for (i = 0; i < lines.length; i++) {
      if (!lines[i].startsWith(marker)) { allOn = false; break; }
    }
    var out = lines.map(function (ln) {
      if (allOn) return ln.slice(marker.length);
      return marker + ln;
    }).join('\n');
    ta.setRangeText(out, startLine, e, 'end');
    // reselect the affected block so the user can keep tweaking lines
    ta.selectionStart = startLine;
    ta.selectionEnd = startLine + out.length;
    ta.dispatchEvent(new Event('input', { bubbles: true }));
  }

  function wrapWord(ta, marker) {
    var s = ta.selectionStart, e = ta.selectionEnd;
    var inner = ta.value.slice(s, e);
    if (s !== e &&
        ta.value.slice(Math.max(0, s - marker.length), s) === marker &&
        ta.value.slice(e, e + marker.length) === marker) {
      // already wrapped: unwrap instead (toggle semantics)
      ta.setRangeText(inner, s - marker.length, e + marker.length, 'preserve');
      ta.selectionStart = s - marker.length;
      ta.selectionEnd = e - marker.length;
    } else {
      applyEdit(ta, marker, marker, 'text');
    }
    ta.dispatchEvent(new Event('input', { bubbles: true }));
  }

  /* ---------- toolbar definition ---------------------------------------- */
  var BUTTONS = [
    { glyph: '𝐁', title: 'Bold — Ctrl/Cmd+B', key: 'b',
      run: function (ta) { wrapWord(ta, '**'); } },
    { glyph: '𝐼', title: 'Italic — Ctrl/Cmd+I', key: 'i',
      run: function (ta) { wrapWord(ta, '_'); } },
    { glyph: '</>', title: 'Inline code — Ctrl/Cmd+E', key: 'e',
      run: function (ta) { wrapWord(ta, '`'); } },
    { glyph: '🔗', title: 'Link — Ctrl/Cmd+K', key: 'k',
      run: function (ta) { applyEdit(ta, '[', '](https://)', 'label'); } },
    { glyph: 'H', title: 'Heading',
      run: function (ta) { prefixLines(ta, '## '); } },
    { glyph: '“', title: 'Quote',
      run: function (ta) { prefixLines(ta, '> '); } },
    { glyph: '•', title: 'Bulleted list',
      run: function (ta) { prefixLines(ta, '- '); } },
    { glyph: '1.', title: 'Numbered list (each line)',
      run: function (ta) { numberLines(ta); } },
    { glyph: '☑', title: 'Task list item',
      run: function (ta) { prefixLines(ta, '- [ ] '); } },
    { glyph: '#', title: 'Reference a ticket (#id)',
      run: function (ta) { insertAtCaret(ta, '#'); } },
    { glyph: '¶', title: 'Project reference',
      run: function (ta) { insertAtCaret(ta, 'project:'); } },
  ];

  function numberLines(ta) {
    var s = ta.selectionStart, e = ta.selectionEnd;
    var startLine = ta.value.lastIndexOf('\n', s - 1) + 1;
    var lines = ta.value.slice(startLine, e).split('\n');
    var out = lines.map(function (ln, idx) {
      return ln.trim() ? (idx + 1) + '. ' + ln.replace(/^\d+\.\s*/, '') : ln;
    }).join('\n');
    ta.setRangeText(out, startLine, e, 'end');
    ta.selectionStart = startLine;
    ta.selectionEnd = startLine + out.length;
    ta.dispatchEvent(new Event('input', { bubbles: true }));
  }

  function insertAtCaret(ta, text) {
    insertText(ta, text);
    ta.dispatchEvent(new Event('input', { bubbles: true }));
  }

  /* ---------- live preview ---------------------------------------------- */
  function makePreview(wrap, ta) {
    var box = document.createElement('div');
    box.className = 'md-preview';
    box.innerHTML =
      '<span class="md-preview-label">Preview</span>' +
      '<div class="md-body"><p class="md-empty">Nothing to preview yet.</p></div>';
    wrap.appendChild(box);
    var body = box.querySelector('.md-body');

    var timer = null;
    function update() {
      var text = ta.value.slice(0, MAX_CHARS);
      if (!text.trim()) {
        body.innerHTML = '<p class="md-empty">Nothing to preview yet.</p>';
        return;
      }
      var form = new FormData();
      form.append('text', text);
      fetch(PREVIEW_URL, {
        method: 'POST',
        body: form,
        headers: { 'X-CSRFToken': csrfToken() },
        credentials: 'same-origin',
      })
        .then(function (r) { return r.json(); })
        .then(function (data) { body.innerHTML = data.html; })
        .catch(function () { /* keep the last good preview */ });
    }
    function schedule() {
      clearTimeout(timer);
      timer = setTimeout(update, DEBOUNCE_MS);
    }
    ta.addEventListener('input', schedule);
    return { update: update };
  }

  /* ---------- reference autocomplete ------------------------------------ */
  var TOKEN_RE = /(?:(#)|(ticket|tkt|proj|project):)([\w.-]*)$/i;

  function makeSuggest(wrap, ta) {
    var box = document.createElement('div');
    box.className = 'md-suggest';
    box.hidden = true;
    wrap.appendChild(box);
    var state = { items: [], active: -1, tokenStart: -1, tokenEnd: -1 };

    function close() {
      box.hidden = true;
      state.items = [];
      state.active = -1;
    }

    function commit(item) {
      var s = state.tokenStart, e = state.tokenEnd;
      if (s < 0) return;
      ta.setRangeText(item.ref + ' ', s, e, 'end');
      ta.dispatchEvent(new Event('input', { bubbles: true }));
      close();
      ta.focus();
    }

    function render() {
      box.innerHTML = '';
      state.items.forEach(function (item, idx) {
        var row = document.createElement('div');
        row.className = 'md-suggestion' + (idx === state.active ? ' active' : '');
        row.innerHTML =
          '<b>' + escapeHtml(item.label) + '</b>' +
          '<span class="ref">' + item.ref + '</span>';
        row.addEventListener('mousedown', function (ev) {
          ev.preventDefault();
          commit(item);
        });
        box.appendChild(row);
      });
      box.hidden = state.items.length === 0;
    }

    var seq = 0;
    function maybeOpen() {
      var before = ta.value.slice(0, ta.selectionStart);
      var m = TOKEN_RE.exec(before);
      if (!m) { close(); return; }
      var isHash = m[1] !== undefined;
      var kind = isHash ? 'ticket' : 'project';
      var q = m[3];
      if (q.length === 0) { close(); return; }   // "#" or "project:" alone
      state.tokenStart = before.length - m[0].length;
      state.tokenEnd = ta.selectionStart;
      var mine = ++seq;
      fetch(SUGGEST_URL + '?kind=' + kind + '&q=' + encodeURIComponent(q), {
        headers: { 'X-CSRFToken': csrfToken() },
        credentials: 'same-origin',
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (mine !== seq) return;   // a later keystroke outranked this one
          state.items = data.items;
          state.active = state.items.length ? 0 : -1;
          render();
        })
        .catch(close);
    }

    ta.addEventListener('keyup', function (ev) {
      if (nav(ev, false)) return;
      maybeOpen();
    });
    ta.addEventListener('keydown', function (ev) {
      if (box.hidden) return;
      if (nav(ev, true)) return;
      if (ev.key === 'Escape') { close(); }
      else if (ev.key === 'Enter' || ev.key === 'Tab') {
        if (state.active >= 0 && state.items[state.active]) {
          ev.preventDefault();
          commit(state.items[state.active]);
        } else {
          close();
        }
      }
    });
    ta.addEventListener('blur', function () {
      setTimeout(close, 120);   // let mousedown land first
    });

    function nav(ev, whenOpen) {
      if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
        if (!whenOpen || box.hidden || !state.items.length) return false;
        ev.preventDefault();
        var d = ev.key === 'ArrowDown' ? 1 : -1;
        state.active =
          (state.active + d + state.items.length) % state.items.length;
        render();
        return true;
      }
      return false;
    }

    function escapeHtml(s) {
      var d = document.createElement('div');
      d.textContent = s;
      return d.innerHTML;
    }
  }

  /* ---------- attach ----------------------------------------------------- */
  function attach(ta) {
    if (ta.__mdEditor) return;
    ta.__mdEditor = true;

    var wrap = document.createElement('div');
    wrap.className = 'md-wrap';
    ta.parentNode.insertBefore(wrap, ta);
    wrap.appendChild(ta);

    var bar = document.createElement('div');
    bar.className = 'md-toolbar';
    BUTTONS.forEach(function (btn) {
      var b = document.createElement('button');
      b.type = 'button';   // never submit the form by accident
      b.textContent = btn.glyph;
      b.title = btn.title;
      b.setAttribute('aria-label', btn.title);
      b.addEventListener('mousedown', function (ev) { ev.preventDefault(); });
      b.addEventListener('click', function () { btn.run(ta); });
      bar.appendChild(b);
    });
    wrap.insertBefore(bar, ta);

    ta.addEventListener('keydown', function (ev) {
      if (!(ev.ctrlKey || ev.metaKey) || ev.altKey) return;
      var key = ev.key ? ev.key.toLowerCase() : '';
      if (key === 'b') { ev.preventDefault(); wrapWord(ta, '**'); }
      else if (key === 'i') { ev.preventDefault(); wrapWord(ta, '_'); }
      else if (key === 'e') { ev.preventDefault(); wrapWord(ta, '`'); }
      else if (key === 'k') {
        ev.preventDefault();
        applyEdit(ta, '[', '](https://)', 'label');
      }
    });

    makePreview(wrap, ta);
    makeSuggest(wrap, ta);
  }

  function init() {
    var nodes = document.querySelectorAll('textarea[data-md-editor]');
    Array.prototype.forEach.call(nodes, attach);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
  window.tokamakMarkdown = { attach: attach, init: init };
})();
