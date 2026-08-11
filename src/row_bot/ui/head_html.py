"""Row-Bot UI — ``<head>`` HTML injection (CSS + JS).

Call ``inject_head_html()`` once per page load to add highlight.js,
vis-network, and custom Row-Bot styles/scripts.
"""

from __future__ import annotations

from row_bot.brand import APP_BRAND_ACCENT, APP_BRAND_ACCENT_RGB
from nicegui import ui

HEAD_HTML = """\
<link rel="manifest" href="/mobile/manifest.webmanifest">
<meta name="theme-color" content="__ROW_BOT_BRAND_ACCENT__">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Row-Bot">
<link rel="stylesheet"
      href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/atom-one-dark.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>
<script src="/static/vis-network.min.js"></script>
<script src="/static/mermaid.min.js"></script>
<script>
(function() {
  if (!('serviceWorker' in navigator)) return;
  if (location.protocol === 'file:') return;
  window.addEventListener('load', function() {
    navigator.serviceWorker.register('/mobile/service-worker.js', {scope: '/'})
      .catch(function() {});
  });
})();
</script>
<script>
mermaid.initialize({
  startOnLoad: false,
  theme: 'dark',
  securityLevel: 'strict',
  flowchart: {htmlLabels: false},
  state: {htmlLabels: false}
});
</script>
<script>
(function() {
  if (window.__rowBotClientErrorReporterInstalled) return;
  window.__rowBotClientErrorReporterInstalled = true;
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
  window.rowBotReportClientEvent = report;
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
  var connectionObserverRoot = document.documentElement;
  if (connectionObserverRoot instanceof Node) {
    new MutationObserver(function() {
      try {
        var body = document.body;
        if (!body) return;
        var text = body.innerText || '';
        if (text.indexOf('trying to connect') !== -1 || text.indexOf('Disconnected') !== -1) {
          reportConnectionState('NiceGUI client reconnecting');
        }
      } catch (err) {}
    }).observe(connectionObserverRoot, {childList: true, subtree: true, characterData: true});
  }
})();
</script>
<script>
(function() {
  if (window.__rowBotCodeHighlighterInstalled) return;
  window.__rowBotCodeHighlighterInstalled = true;
  var _highlightTimer = null;
  function highlightCodeBlocks() {
    if (typeof hljs === 'undefined') return;
    document.querySelectorAll('pre code:not([data-highlighted="yes"])').forEach(function(el) {
      if (el.closest('.row-bot-live-stream')) return;
      if (el.children.length) return;
      try { hljs.highlightElement(el); } catch (err) {}
    });
  }
  window.rowBotHighlightCodeBlocks = function() {
    clearTimeout(_highlightTimer);
    _highlightTimer = setTimeout(function() {
      requestAnimationFrame(highlightCodeBlocks);
    }, 80);
  };
  var highlightObserverRoot = document.documentElement;
  if (highlightObserverRoot instanceof Node) {
    new MutationObserver(function() {
      window.rowBotHighlightCodeBlocks();
    }).observe(highlightObserverRoot, {childList: true, subtree: true});
  }
  window.addEventListener('load', window.rowBotHighlightCodeBlocks);
  window.rowBotHighlightCodeBlocks();
})();
</script>
<script>
(function() {
  var _mermaidTimer = null;
  window.rowBotNormalizeMermaidDiagrams = function(root) {
    root = root || document;
    Array.from(root.querySelectorAll('.mermaid-rendered svg')).forEach(function(svg) {
      try {
        svg.style.overflow = 'visible';
        svg.setAttribute('preserveAspectRatio', 'xMinYMin meet');
        var box = svg.getBBox ? svg.getBBox() : null;
        if (box && box.width > 0 && box.height > 0) {
          var pad = 18;
          var x = Math.floor(box.x - pad);
          var y = Math.floor(box.y - pad);
          var w = Math.ceil(box.width + pad * 2);
          var h = Math.ceil(box.height + pad * 2);
          svg.setAttribute('viewBox', [x, y, w, h].join(' '));
          svg.dataset.rowBotIntrinsicWidth = String(w);
          svg.dataset.rowBotIntrinsicHeight = String(h);
        }
      } catch (err) {}
    });
  };
  window.rowBotRenderMermaidDiagrams = function(root) {
    if (typeof mermaid === 'undefined') return;
    root = root || document;
    var nodes = Array.from(root.querySelectorAll('pre.mermaid')).filter(function(node) {
      return !node.closest('.row-bot-live-stream');
    });
    if (!nodes.length) return;
    return Promise.resolve(mermaid.run({nodes: nodes, suppressErrors: true})).then(function() {
      requestAnimationFrame(function() { window.rowBotNormalizeMermaidDiagrams(root); });
    }).catch(function() {});
  };
  var mermaidObserverRoot = document.documentElement;
  if (mermaidObserverRoot instanceof Node) {
    new MutationObserver(function() {
      var nodes = Array.from(document.querySelectorAll('pre.mermaid')).filter(function(node) {
        return !node.closest('.row-bot-live-stream');
      });
      if (nodes.length > 0) {
        clearTimeout(_mermaidTimer);
        _mermaidTimer = setTimeout(function() {
          window.rowBotRenderMermaidDiagrams(document);
        }, 150);
      }
    }).observe(mermaidObserverRoot, {childList: true, subtree: true});
  }
})();
</script>
<style>
    html, body { overflow: hidden !important; height: 100vh; }
    .nicegui-content { overflow: hidden !important; }
    /* Chat messages must never produce a horizontal scroll bar — on
       narrow windows / small panes the content wraps instead. Long
       unbreakable tokens (URLs, CJK, code) break anywhere. */
    .row-bot-msg pre,
    .row-bot-msg-body pre {
        white-space: pre-wrap;
        word-break: break-word;
        overflow-wrap: anywhere;
        overflow-x: hidden;
        max-width: 100%;
    }
    .row-bot-msg code,
    .row-bot-msg-body code {
        white-space: pre-wrap;
        overflow-wrap: anywhere;
        word-break: break-word;
    }
    .row-bot-msg a { color: #64b5f6; overflow-wrap: anywhere; word-break: break-word; }
    .row-bot-msg a:hover { text-decoration: underline; }
    /* Tables inside messages: scroll within a container rather than
       stretch the outer chat column. */
    .row-bot-msg-body table {
        display: block;
        max-width: 100%;
        overflow-x: auto;
    }
    .row-bot-msg-body img,
    .row-bot-msg-body video,
    .row-bot-msg-body iframe {
        max-width: 100%;
        height: auto;
    }
    /* Designer-pane chat bubbles use a different class but need the
       same horizontal-scroll protection on narrow panes. */
    .row-bot-designer-bubble,
    .row-bot-designer-bubble * {
        min-width: 0;
        max-width: 100%;
    }
    .row-bot-designer-bubble pre,
    .row-bot-designer-bubble code {
        white-space: pre-wrap;
        word-break: break-word;
        overflow-wrap: anywhere;
        overflow-x: hidden;
    }
    .row-bot-designer-bubble table {
        display: block;
        max-width: 100%;
        overflow-x: auto;
    }
    .row-bot-msg-row {
        display: flex;
        gap: 0.75rem;
        padding: 0.75rem 0.5rem;
        width: 100%;
        border-radius: 8px;
    }
    .row-bot-msg-row-user {
        background: rgba(255, 255, 255, 0.04);
    }
    .row-bot-avatar {
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
    .row-bot-avatar-user { background: #1976d2; color: white; }
    .row-bot-avatar-bot { background: #37474f; color: __ROW_BOT_BRAND_ACCENT__ !important; }
    .row-bot-avatar img {
        width: 100%; height: 100%;
        object-fit: cover;
        border-radius: 50%;
    }
    .row-bot-msg-header {
        display: flex !important;
        align-items: baseline;
        gap: 0.5rem;
    }
    .row-bot-msg-name {
        font-weight: 600;
        font-size: 0.9rem;
        color: #e0e0e0;
    }
    /* Bot name = brand accent */
    .row-bot-msg-row:not(.row-bot-msg-row-user) .row-bot-msg-name {
        color: __ROW_BOT_BRAND_ACCENT__ !important;
    }
    .row-bot-msg-stamp {
        font-size: 0.7rem;
        color: #888;
        margin-left: 0.5rem;
    }
    .row-bot-msg-body {
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
    .row-bot-msg-body .row-bot-msg,
    .row-bot-msg-body p,
    .row-bot-msg-body li,
    .row-bot-msg-body td,
    .row-bot-msg-body th,
    .row-bot-msg-body span:not(.row-bot-msg-name):not(.row-bot-msg-stamp) {
        cursor: text;
    }
    .row-bot-msg-body .nicegui-code pre {
        white-space: pre-wrap;
        word-break: break-all;
    }
    /* Tool traces are secondary transcript metadata. Keep their collapsed
       controls close to the compact progressive-skill marker without
       muting the result content revealed inside each expansion. */
    .row-bot-tool-trace > .q-expansion-item__container > .q-item,
    .row-bot-tool-trace-item > .q-expansion-item__container > .q-item {
        min-height: 30px;
        padding: 2px 4px;
        color: #9e9e9e;
    }
    .row-bot-tool-trace > .q-expansion-item__container > .q-item .q-item__label,
    .row-bot-tool-trace-item > .q-expansion-item__container > .q-item .q-item__label {
        font-size: 0.75rem;
        font-weight: 400;
        line-height: 1.1rem;
    }
    .row-bot-tool-trace > .q-expansion-item__container > .q-item .q-icon,
    .row-bot-tool-trace-item > .q-expansion-item__container > .q-item .q-icon {
        color: #9e9e9e;
        font-size: 15px;
    }
    .row-bot-tool-trace > .q-expansion-item__container > .q-item .q-item__section--avatar,
    .row-bot-tool-trace-item > .q-expansion-item__container > .q-item .q-item__section--avatar {
        min-width: 24px;
        padding-right: 4px;
    }
    .row-bot-tool-trace > .q-expansion-item__container > .q-item .q-item__section--side,
    .row-bot-tool-trace-item > .q-expansion-item__container > .q-item .q-item__section--side {
        padding-left: 4px;
    }
    .row-bot-agent-run-list {
        align-items: center;
        flex-wrap: wrap;
        gap: 0.25rem;
    }
    .row-bot-agent-run-stud.q-btn {
        flex: 0 1 auto;
        width: auto;
        min-width: 0;
        max-width: min(100%, 300px);
        min-height: 30px;
        padding: 3px 8px;
        border: 1px solid rgba(148, 163, 184, 0.18);
        border-radius: 999px;
        background: rgba(148, 163, 184, 0.055);
        color: #d8dee9;
        box-shadow: none;
        transition: none !important;
    }
    .row-bot-agent-run-stud.q-btn .q-btn__content {
        width: 100%;
        min-width: 0;
        flex-wrap: nowrap;
        gap: 0.375rem;
    }
    .row-bot-agent-run-stud.q-btn .q-focus-helper {
        transition: none !important;
    }
    .row-bot-agent-run-stud.q-btn:hover {
        border-color: rgba(__ROW_BOT_BRAND_ACCENT_RGB__, 0.34);
        background: rgba(__ROW_BOT_BRAND_ACCENT_RGB__, 0.09);
    }
    .row-bot-agent-run-stud.q-btn:focus-visible {
        outline: 2px solid rgba(__ROW_BOT_BRAND_ACCENT_RGB__, 0.78);
        outline-offset: 2px;
    }
    .row-bot-agent-run-name {
        min-width: 0;
        max-width: 240px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }
    .row-bot-agent-run-status-dot {
        flex: 0 0 7px;
        width: 7px;
        height: 7px;
        border-radius: 50%;
        box-shadow: 0 0 0 1px rgba(255, 255, 255, 0.2);
    }
    .row-bot-typing .dots span {
        animation: tblink 1.4s infinite both;
    }
    .row-bot-typing .dots span:nth-child(2) { animation-delay: 0.2s; }
    .row-bot-typing .dots span:nth-child(3) { animation-delay: 0.4s; }
    @keyframes tblink {
        0%, 80%, 100% { opacity: 0; }
        40% { opacity: 1; }
    }
    @keyframes row-bot-spin { to { transform: rotate(360deg); } }
    .row-bot-spin { animation: row-bot-spin 1s linear infinite; }
    .mermaid-rendered {
        width: 100%;
        max-width: 100%;
        background: rgba(255,255,255,0.03);
        border-radius: 8px;
        padding: 16px;
        margin: 8px 0;
        overflow-x: auto;
    }
    .mermaid-rendered svg {
        display: block;
        width: 100%;
        min-width: 900px;
        max-width: none !important;
        height: auto;
        margin: 0;
        overflow: visible;
    }
    @media (max-width: 960px) {
        .mermaid-rendered svg {
            min-width: 680px;
        }
    }
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

window.__rowBotManagedWindows = window.__rowBotManagedWindows || {};

window.rowBotOpenManagedWindow = async function(options) {
    options = options || {};
    var rawUrl = options.url || '';
    if (!rawUrl) return false;

    var name = options.name || '_blank';
    var title = options.title || 'Row-Bot';
    var width = Number(options.width || 1600);
    var height = Number(options.height || 900);
    var href = new URL(rawUrl, window.location.origin).href;

    if (window.pywebview && window.pywebview.api && window.pywebview.api.open_window) {
        try {
            return !!(await window.pywebview.api.open_window(name, href, title, width, height));
        } catch (err) {
            console.warn('rowBotOpenManagedWindow failed via pywebview bridge', err);
            return false;
        }
    }

    try {
        var existing = window.__rowBotManagedWindows[name];
        if (existing && !existing.closed) {
            existing.location.href = href;
            if (existing.focus) existing.focus();
            return true;
        }
    } catch (err) {
        console.warn('rowBotOpenManagedWindow could not reuse existing browser window', err);
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
    window.__rowBotManagedWindows[name] = opened;
    try {
        if (opened.focus) opened.focus();
    } catch (err) {
        console.warn('rowBotOpenManagedWindow could not focus browser window', err);
    }
    return true;
};

window.rowBotCloseManagedWindow = async function(name) {
    if (!name) return false;

    if (window.pywebview && window.pywebview.api && window.pywebview.api.close_window) {
        try {
            return !!(await window.pywebview.api.close_window(name));
        } catch (err) {
            console.warn('rowBotCloseManagedWindow failed via pywebview bridge', err);
            return false;
        }
    }

    try {
        var existing = window.__rowBotManagedWindows[name];
        if (existing && !existing.closed) {
            existing.close();
        }
        delete window.__rowBotManagedWindows[name];
        return true;
    } catch (err) {
        console.warn('rowBotCloseManagedWindow could not close browser window', err);
        return false;
    }
};

// ── pywebview-only right-click context menu ─────────────────────
(function() {
    if (!window.pywebview && !navigator.userAgent.includes('pywebview')) {
        // Normal browser — let native context menu work
        // Re-check after a short delay (pywebview may inject late)
        setTimeout(function() { if (!window.pywebview) return; initRowBotCtx(); }, 1500);
    } else {
        initRowBotCtx();
    }
    function initRowBotCtx() {
        if (!document.body) {
            document.addEventListener('DOMContentLoaded', initRowBotCtx, {once:true});
            return;
        }
        if (document.getElementById('row-bot-ctx-menu')) return;
        var menu = document.createElement('div');
        menu.id = 'row-bot-ctx-menu';
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
HEAD_HTML = HEAD_HTML.replace("__ROW_BOT_BRAND_ACCENT__", APP_BRAND_ACCENT).replace(
    "__ROW_BOT_BRAND_ACCENT_RGB__",
    APP_BRAND_ACCENT_RGB,
)


def inject_head_html() -> None:
    """Add the Row-Bot head HTML (CSS + JS) to the current page."""
    ui.add_head_html(HEAD_HTML)
