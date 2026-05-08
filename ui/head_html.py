"""Thoth UI — ``<head>`` HTML injection (CSS + JS).

Call ``inject_head_html()`` once per page load to add highlight.js,
vis-network, and custom Thoth styles/scripts.
"""

from __future__ import annotations

from nicegui import ui

HEAD_HTML = """\
<link rel="stylesheet"
      href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/atom-one-dark.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>
<script src="/static/vis-network.min.js"></script>
<script src="/static/mermaid.min.js"></script>
<script>mermaid.initialize({startOnLoad: false, theme: 'dark', securityLevel: 'strict'});</script>
<script>
(function() {
  if (window.__thothClientErrorReporterInstalled) return;
  window.__thothClientErrorReporterInstalled = true;
  function report(kind, payload) {
    try {
      var body = Object.assign({
        kind: kind,
        url: window.location.href,
        userAgent: navigator.userAgent,
        ts: new Date().toISOString()
      }, payload || {});
      fetch('/api/client-error', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body),
        keepalive: true
      }).catch(function() {});
    } catch (err) {}
  }
  window.thothReportClientEvent = report;
  window.addEventListener('error', function(event) {
    report('error', {
      message: event.message || '',
      source: event.filename || '',
      line: event.lineno || 0,
      column: event.colno || 0,
      stack: event.error && event.error.stack ? String(event.error.stack) : ''
    });
  });
  window.addEventListener('unhandledrejection', function(event) {
    var reason = event.reason;
    report('unhandledrejection', {
      message: reason && reason.message ? String(reason.message) : String(reason || ''),
      stack: reason && reason.stack ? String(reason.stack) : ''
    });
  });
  var lastConnectionReport = 0;
  function reportConnectionState(message) {
    var now = Date.now();
    if (now - lastConnectionReport < 30000) return;
    lastConnectionReport = now;
    report('connection_state', {message: message || 'connection state changed'});
  }
  window.addEventListener('offline', function() { reportConnectionState('browser offline'); });
  window.addEventListener('online', function() { reportConnectionState('browser online'); });
  var lastActivityReport = 0;
  function reportActivity(eventName) {
    var now = Date.now();
    if (now - lastActivityReport < 60000) return;
    lastActivityReport = now;
    report('activity', {message: 'user activity', event: eventName || ''});
  }
  ['keydown', 'pointerdown', 'input', 'wheel'].forEach(function(name) {
    window.addEventListener(name, function() { reportActivity(name); }, {passive: true, capture: true});
  });
  new MutationObserver(function() {
    try {
      var body = document.body;
      if (!body) return;
      var text = body.innerText || '';
      if (text.indexOf('trying to connect') !== -1 || text.indexOf('Disconnected') !== -1) {
        reportConnectionState('NiceGUI client reconnecting');
      }
    } catch (err) {}
  }).observe(document.documentElement, {childList: true, subtree: true, characterData: true});
})();
</script>
<script>
(function() {
  var _mermaidTimer = null;
  new MutationObserver(function() {
    var nodes = document.querySelectorAll('pre.mermaid');
    if (nodes.length > 0) {
      clearTimeout(_mermaidTimer);
      _mermaidTimer = setTimeout(function() {
        mermaid.run({nodes: document.querySelectorAll('pre.mermaid'), suppressErrors: true});
      }, 150);
    }
  }).observe(document.documentElement, {childList: true, subtree: true});
})();
</script>
<style>
    html, body { overflow: hidden !important; height: 100vh; }
    .nicegui-content { overflow: hidden !important; }
    /* Chat messages must never produce a horizontal scroll bar — on
       narrow windows / small panes the content wraps instead. Long
       unbreakable tokens (URLs, CJK, code) break anywhere. */
    .thoth-msg pre,
    .thoth-msg-body pre {
        white-space: pre-wrap;
        word-break: break-word;
        overflow-wrap: anywhere;
        overflow-x: hidden;
        max-width: 100%;
    }
    .thoth-msg code,
    .thoth-msg-body code {
        white-space: pre-wrap;
        overflow-wrap: anywhere;
        word-break: break-word;
    }
    .thoth-msg a { color: #64b5f6; overflow-wrap: anywhere; word-break: break-word; }
    .thoth-msg a:hover { text-decoration: underline; }
    /* Tables inside messages: scroll within a container rather than
       stretch the outer chat column. */
    .thoth-msg-body table {
        display: block;
        max-width: 100%;
        overflow-x: auto;
    }
    .thoth-msg-body img,
    .thoth-msg-body video,
    .thoth-msg-body iframe {
        max-width: 100%;
        height: auto;
    }
    /* Designer-pane chat bubbles use a different class but need the
       same horizontal-scroll protection on narrow panes. */
    .thoth-designer-bubble,
    .thoth-designer-bubble * {
        min-width: 0;
        max-width: 100%;
    }
    .thoth-designer-bubble pre,
    .thoth-designer-bubble code {
        white-space: pre-wrap;
        word-break: break-word;
        overflow-wrap: anywhere;
        overflow-x: hidden;
    }
    .thoth-designer-bubble table {
        display: block;
        max-width: 100%;
        overflow-x: auto;
    }
    .thoth-msg-row {
        display: flex;
        gap: 0.75rem;
        padding: 0.75rem 0.5rem;
        width: 100%;
        border-radius: 8px;
    }
    .thoth-msg-row-user {
        background: rgba(255, 255, 255, 0.04);
    }
    .thoth-avatar {
        width: 36px;
        height: 36px;
        min-width: 36px;
        border-radius: 50%;
        display: flex;
        align-items: center;
        justify-content: center;
        font-size: 1.1rem;
        margin-top: 2px;
    }
    .thoth-avatar-user { background: #1976d2; color: white; }
    .thoth-avatar-bot { background: #37474f; color: gold !important; }
    .thoth-avatar img {
        width: 100%; height: 100%;
        object-fit: cover;
        border-radius: 50%;
    }
    .thoth-msg-header {
        display: flex !important;
        align-items: baseline;
        gap: 0.5rem;
    }
    .thoth-msg-name {
        font-weight: 600;
        font-size: 0.9rem;
        color: #e0e0e0;
    }
    /* Bot name = gold */
    .thoth-msg-row:not(.thoth-msg-row-user) .thoth-msg-name {
        color: gold !important;
    }
    .thoth-msg-stamp {
        font-size: 0.7rem;
        color: #888;
        margin-left: 0.5rem;
    }
    .thoth-msg-body {
        flex: 1;
        min-width: 0;
        overflow: hidden;
        /* Never allow a horizontal scroll bar inside a message bubble. */
        overflow-wrap: anywhere;
        word-break: break-word;
        /* Override Quasar QScrollArea's user-select: none */
        -webkit-user-select: text;
        user-select: text;
        cursor: default;
    }
    .thoth-msg-body .thoth-msg,
    .thoth-msg-body p,
    .thoth-msg-body li,
    .thoth-msg-body td,
    .thoth-msg-body th,
    .thoth-msg-body span:not(.thoth-msg-name):not(.thoth-msg-stamp) {
        cursor: text;
    }
    .thoth-msg-body .nicegui-code pre {
        white-space: pre-wrap;
        word-break: break-all;
    }
    .thoth-typing .dots span {
        animation: tblink 1.4s infinite both;
    }
    .thoth-typing .dots span:nth-child(2) { animation-delay: 0.2s; }
    .thoth-typing .dots span:nth-child(3) { animation-delay: 0.4s; }
    @keyframes tblink {
        0%, 80%, 100% { opacity: 0; }
        40% { opacity: 1; }
    }
    @keyframes thoth-spin { to { transform: rotate(360deg); } }
    .thoth-spin { animation: thoth-spin 1s linear infinite; }
    .mermaid-rendered { background: rgba(255,255,255,0.03); border-radius: 8px; padding: 12px; margin: 8px 0; overflow-x: auto; }
</style>
<script>
// Open all external (http/https) links in the system browser.
// In native mode (pywebview) this routes through the Python js_api
// so the OS default browser opens instead of navigating in-app.
// In a regular browser session it falls back to window.open().
document.addEventListener('click', function(e) {
    var a = e.target.closest('a[href]');
    if (!a) return;
    var href = a.href || '';
    if (!/^https?:/i.test(href)) return;
    e.preventDefault();
    e.stopPropagation();
    if (window.pywebview && window.pywebview.api && window.pywebview.api.open_url) {
        window.pywebview.api.open_url(href);
    } else {
        window.open(href, '_blank', 'noopener');
    }
});

window.__thothManagedWindows = window.__thothManagedWindows || {};

window.thothOpenManagedWindow = async function(options) {
    options = options || {};
    var rawUrl = options.url || '';
    if (!rawUrl) return false;

    var name = options.name || '_blank';
    var title = options.title || 'Thoth';
    var width = Number(options.width || 1600);
    var height = Number(options.height || 900);
    var href = new URL(rawUrl, window.location.origin).href;

    if (window.pywebview && window.pywebview.api && window.pywebview.api.open_window) {
        try {
            return !!(await window.pywebview.api.open_window(name, href, title, width, height));
        } catch (err) {
            console.warn('thothOpenManagedWindow failed via pywebview bridge', err);
            return false;
        }
    }

    try {
        var existing = window.__thothManagedWindows[name];
        if (existing && !existing.closed) {
            existing.location.href = href;
            if (existing.focus) existing.focus();
            return true;
        }
    } catch (err) {
        console.warn('thothOpenManagedWindow could not reuse existing browser window', err);
    }

    var features = [
        'popup=yes',
        'resizable=yes',
        'scrollbars=yes',
        'width=' + width,
        'height=' + height,
    ].join(',');
    var opened = window.open(href, name, features);
    if (!opened) return false;
    window.__thothManagedWindows[name] = opened;
    try {
        if (opened.focus) opened.focus();
    } catch (err) {
        console.warn('thothOpenManagedWindow could not focus browser window', err);
    }
    return true;
};

window.thothCloseManagedWindow = async function(name) {
    if (!name) return false;

    if (window.pywebview && window.pywebview.api && window.pywebview.api.close_window) {
        try {
            return !!(await window.pywebview.api.close_window(name));
        } catch (err) {
            console.warn('thothCloseManagedWindow failed via pywebview bridge', err);
            return false;
        }
    }

    try {
        var existing = window.__thothManagedWindows[name];
        if (existing && !existing.closed) {
            existing.close();
        }
        delete window.__thothManagedWindows[name];
        return true;
    } catch (err) {
        console.warn('thothCloseManagedWindow could not close browser window', err);
        return false;
    }
};

// ── pywebview-only right-click context menu ─────────────────────
(function() {
    if (!window.pywebview && !navigator.userAgent.includes('pywebview')) {
        // Normal browser — let native context menu work
        // Re-check after a short delay (pywebview may inject late)
        setTimeout(function() { if (!window.pywebview) return; initThothCtx(); }, 1500);
    } else {
        initThothCtx();
    }
    function initThothCtx() {
        var menu = document.createElement('div');
        menu.id = 'thoth-ctx-menu';
        menu.style.cssText = 'display:none; position:fixed; z-index:99999; '
            + 'background:#1e1e2e; border:1px solid #444; border-radius:6px; '
            + 'padding:4px 0; min-width:140px; box-shadow:0 4px 16px rgba(0,0,0,0.5); '
            + 'font-family:sans-serif; font-size:0.85rem; color:#ddd;';
        var items = [
            {label:'Cut', icon:'\u2702', cmd:'cut', needsSel:true, needsEdit:true},
            {label:'Copy', icon:'\u2398', cmd:'copy', needsSel:true},
            {label:'Paste', icon:'\u2399', cmd:'paste', needsEdit:true},
            {sep:true},
            {label:'Select All', icon:'\u2610', cmd:'selectAll'}
        ];
        items.forEach(function(it) {
            if (it.sep) {
                var hr = document.createElement('div');
                hr.style.cssText = 'height:1px; background:#444; margin:4px 0;';
                menu.appendChild(hr); return;
            }
            var btn = document.createElement('div');
            btn.textContent = it.icon + '  ' + it.label;
            btn.dataset.cmd = it.cmd;
            btn.dataset.needsSel = it.needsSel ? '1' : '';
            btn.dataset.needsEdit = it.needsEdit ? '1' : '';
            btn.style.cssText = 'padding:6px 16px; cursor:pointer; white-space:nowrap;';
            btn.onmouseenter = function() { btn.style.background = '#333'; };
            btn.onmouseleave = function() { btn.style.background = 'none'; };
            btn.onmousedown = function(e) {
                e.preventDefault();
                var cmd = btn.dataset.cmd;
                if (cmd === 'paste') {
                    function _doInsert(t) {
                        // Re-focus right before insertion — focus drifts during async clipboard read
                        var el = _ctxTarget || document.activeElement;
                        if (!el) return;
                        el.focus();
                        if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') {
                            if (!document.execCommand('insertText', false, t)) {
                                // execCommand failed — manipulate value directly
                                var s = el.selectionStart || 0, end = el.selectionEnd || 0;
                                el.value = el.value.substring(0, s) + t + el.value.substring(end);
                                el.selectionStart = el.selectionEnd = s + t.length;
                                el.dispatchEvent(new Event('input', {bubbles: true}));
                            }
                        } else if (el.isContentEditable) {
                            document.execCommand('insertText', false, t);
                        }
                    }
                    // Prefer pywebview bridge (works reliably on macOS WKWebView)
                    if (window.pywebview && window.pywebview.api && window.pywebview.api.get_clipboard) {
                        window.pywebview.api.get_clipboard().then(function(t) {
                            if (t != null) { _doInsert(t); }
                            else {
                                navigator.clipboard.readText().then(_doInsert).catch(function() {
                                    document.execCommand('paste');
                                });
                            }
                        }).catch(function() {
                            navigator.clipboard.readText().then(_doInsert).catch(function() {
                                document.execCommand('paste');
                            });
                        });
                    } else {
                        navigator.clipboard.readText().then(_doInsert).catch(function() {
                            document.execCommand('paste');
                        });
                    }
                } else {
                    document.execCommand(cmd);
                }
                menu.style.display = 'none';
            };
            menu.appendChild(btn);
        });
        document.body.appendChild(menu);

        var _ctxTarget = null;

        document.addEventListener('contextmenu', function(e) {
            e.preventDefault();
            // Use the actual right-clicked element — on macOS WKWebView,
            // right-click does NOT move document.activeElement.
            _ctxTarget = e.target.closest('input, textarea, [contenteditable]') || document.activeElement;
            var sel = window.getSelection().toString();
            var el = e.target.closest('input, textarea, [contenteditable]');
            menu.querySelectorAll('[data-cmd]').forEach(function(b) {
                var show = true;
                if (b.dataset.needsSel && !sel) show = false;
                if (b.dataset.needsEdit && !el) show = false;
                b.style.display = show ? 'block' : 'none';
            });
            menu.style.left = Math.min(e.clientX, window.innerWidth - 160) + 'px';
            menu.style.top = Math.min(e.clientY, window.innerHeight - 200) + 'px';
            menu.style.display = 'block';
        });
        document.addEventListener('click', function() { menu.style.display = 'none'; });
        document.addEventListener('keydown', function(e) { if (e.key === 'Escape') menu.style.display = 'none'; });
    }
})();
</script>
"""


def inject_head_html() -> None:
    """Add the Thoth head HTML (CSS + JS) to the current page."""
    ui.add_head_html(HEAD_HTML)
