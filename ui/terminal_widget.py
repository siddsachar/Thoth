"""Persistent global terminal widget for NiceGUI.

Uses NiceGUI's built-in ``ui.xterm()`` element (which wraps xterm.js)
connected to the PTY via ``TerminalBridge``.

The terminal is a **first-class global resource** — it is built once at
page init (outside the per-thread rebuild zone) and persists across
thread switches.  The xterm.js scrollback is never lost.

Provides ``build_terminal_panel()`` which creates a resizable bottom
panel containing a live interactive terminal.  The panel is placed
inline inside the main content column so it naturally pushes chat
content upward when expanded.
"""

from __future__ import annotations

import asyncio
import logging

from nicegui import ui

from ui.timer_utils import safe_timer

logger = logging.getLogger(__name__)


# ── Terminal widget builder ──────────────────────────────────────────────────

def build_terminal_panel(p, state, tool_registry) -> None:
    """Build the persistent terminal toggle bar and panel.

    Must be called inside the outer content column (``_outer``) so the
    terminal participates in normal document flow — expanding the panel
    pushes the chat area up rather than overlaying it.

    Parameters
    ----------
    p : P
        Per-client element holder.
    state : AppState
        Application state.
    tool_registry : module
        The tools.registry module (for ``is_enabled`` check).
    """
    p.terminal_visible = False
    p.terminal_toggle_bar = None
    p._terminal_initialized = False
    p._xterm_element = None
    p.terminal_panel = None

    if not tool_registry.is_enabled("shell"):
        return

    _panel_id = f"thoth-term-panel-{id(p)}"
    _drag_id = f"thoth-term-drag-{id(p)}"

    # ── Toggle callback ──────────────────────────────────────────────
    def _toggle_terminal():
        p.terminal_visible = not p.terminal_visible
        _chevron = "expand_more" if p.terminal_visible else "expand_less"
        if p.terminal_chevron:
            p.terminal_chevron.props(f"icon={_chevron}")
        if p.terminal_panel is not None:
            p.terminal_panel.set_visibility(p.terminal_visible)
        if hasattr(p, '_drag_handle') and p._drag_handle is not None:
            p._drag_handle.set_visibility(p.terminal_visible)
        if p.terminal_visible:
            if not p._terminal_initialized:
                p._terminal_initialized = True
                _wire_pty(p)
                ui.run_javascript(p._drag_resize_js)
            if p._xterm_element is not None:
                p._xterm_element.fit()

    # ── Terminal container (border + glow) ──────────────────────────
    _term_container = ui.column().classes("w-full shrink-0 no-wrap").style(
        "gap: 0; border: 1px solid #3a4a5c; border-radius: 8px;"
        " overflow: hidden;"
        " box-shadow: 0 0 12px rgba(88, 166, 255, 0.15), 0 0 4px rgba(88, 166, 255, 0.1);"
        " margin-top: 4px;"
    )

    with _term_container:

        # Drag handle (above toggle bar — only visible when expanded)
        drag_handle = ui.element('div').style(
            'height: 3px; cursor: ns-resize; background: #30363d;'
            ' width: 100%; flex-shrink: 0; transition: background 0.15s;'
        )
        drag_handle._props['id'] = _drag_id
        drag_handle.on('mouseenter', lambda: drag_handle.style('background: #58a6ff;'))
        drag_handle.on('mouseleave', lambda: drag_handle.style('background: #30363d;'))
        drag_handle.set_visibility(False)
        p._drag_handle = drag_handle

        # ── Toggle bar (always visible) ──────────────────────────────
        p.terminal_toggle_bar = ui.row().classes(
            "w-full items-center px-3 cursor-pointer"
        ).style(
            "height: 28px; background: #1a1a2e;"
            " gap: 6px; flex-shrink: 0;"
        )
        p.terminal_toggle_bar.on("click", lambda: _toggle_terminal())

    with p.terminal_toggle_bar:
        ui.icon("terminal").classes("text-grey-5").style(
            "font-size: 14px;"
        )

        p._pty_status_dot = ui.icon("circle").classes(
            "text-grey-6"
        ).style("font-size: 8px;")

        ui.label("Terminal").classes(
            "text-xs font-bold text-grey-5 flex-grow"
        )

        def _clear_terminal():
            if p._xterm_element is not None:
                p._xterm_element.run_terminal_method('clear')

        ui.button(
            icon="delete_sweep", on_click=_clear_terminal
        ).props("flat round dense size=xs").classes(
            "text-grey-5"
        ).tooltip("Clear terminal")

        p.terminal_chevron = ui.button(icon="expand_less").props(
            "flat round dense size=xs"
        ).classes("text-grey-5")
        p.terminal_chevron.on(
            "click.stop", lambda: _toggle_terminal()
        )

    # ── Terminal panel (hidden by default) — inside _term_container for glow
    with _term_container:
        p.terminal_panel = ui.column().classes("w-full shrink-0").style(
            "height: 280px; background: #0d1117; gap: 0;"
        )
        p.terminal_panel._props['id'] = _panel_id
        p.terminal_panel.set_visibility(False)

    with p.terminal_panel:
        # xterm.js terminal
        terminal = ui.xterm({
            'cursorBlink': True,
            'cursorStyle': 'bar',
            'fontSize': 13,
            'fontFamily': "'Cascadia Code', 'JetBrains Mono', 'Fira Code', 'Consolas', monospace",
            'theme': {
                'background': '#0d1117',
                'foreground': '#d4d4d4',
                'cursor': '#569cd6',
                'selectionBackground': 'rgba(86, 156, 214, 0.3)',
                'black': '#1e1e1e',
                'red': '#f44747',
                'green': '#4ec9b0',
                'yellow': '#dcdcaa',
                'blue': '#569cd6',
                'magenta': '#c586c0',
                'cyan': '#9cdcfe',
                'white': '#d4d4d4',
                'brightBlack': '#808080',
                'brightRed': '#f44747',
                'brightGreen': '#4ec9b0',
                'brightYellow': '#dcdcaa',
                'brightBlue': '#569cd6',
                'brightMagenta': '#c586c0',
                'brightCyan': '#9cdcfe',
                'brightWhite': '#ffffff',
            },
            'scrollback': 10000,
            'allowProposedApi': True,
        }).classes("w-full").style(
            "background: #0d1117; flex: 1 1 0; min-height: 0;"
        )

        p._xterm_element = terminal
        ui.element('q-resize-observer').on('resize', terminal.fit)

    # Drag-resize JS (deferred to first show via _toggle_terminal)
    p._drag_resize_js = f'''
    (function() {{
        const dragHandle = document.getElementById('{_drag_id}');
        const panelEl = document.getElementById('{_panel_id}');
        if (!dragHandle || !panelEl) return;
        dragHandle.addEventListener('mousedown', function(e) {{
            const startY = e.clientY;
            const startHeight = panelEl.offsetHeight;
            e.preventDefault();
            e.stopPropagation();
            document.body.style.userSelect = 'none';
            function onMove(ev) {{
                const delta = startY - ev.clientY;
                const h = Math.max(100, Math.min(
                    window.innerHeight * 0.6, startHeight + delta));
                panelEl.style.height = h + 'px';
            }}
            function onUp() {{
                document.removeEventListener('mousemove', onMove);
                document.removeEventListener('mouseup', onUp);
                document.body.style.userSelect = '';
                window.dispatchEvent(new Event('resize'));
            }}
            document.addEventListener('mousemove', onMove);
            document.addEventListener('mouseup', onUp);
        }});
    }})();
    '''

    # ── PTY status pill updater ──────────────────────────────────────
    def _update_status_pill():
        try:
            from terminal_bridge import TerminalBridge
            if TerminalBridge.has_instance():
                bridge = TerminalBridge.get_instance()
                st = bridge.status
                if st == "running":
                    p._pty_status_dot.classes(replace="text-green-5")
                elif st == "restarting":
                    p._pty_status_dot.classes(replace="text-yellow-5")
                else:
                    p._pty_status_dot.classes(replace="text-red-5")

                # Auto-restart if crashed
                if not bridge.is_running and bridge._status == "running":
                    bridge._status = "stopped"
                    p._pty_status_dot.classes(replace="text-red-5")
                    logger.warning("PTY process died — attempting restart")
                    asyncio.ensure_future(bridge.restart())
            else:
                p._pty_status_dot.classes(replace="text-grey-6")
        except Exception:
            pass

    safe_timer(5.0, _update_status_pill)


def _wire_pty(p) -> None:
    """Wire the xterm.js element to the PTY bridge.

    Called once on first terminal panel open.  Starts the PTY lazily
    so the initial shell prompt flows through the registered callback
    and appears in xterm.js immediately.
    """
    from terminal_bridge import TerminalBridge

    bridge = TerminalBridge.get_instance()
    terminal = p._xterm_element
    if terminal is None:
        return

    # ── Wire xterm events to the bridge ──────────────────────────────
    terminal.on_data(lambda e: bridge.on_input(e.data))
    terminal.on('resize', lambda e: bridge.on_resize(
        e.args.get('cols', 120) if isinstance(e.args, dict) else 120,
        e.args.get('rows', 30) if isinstance(e.args, dict) else 30,
    ))

    # ── Output streaming: PTY → xterm (batched at 30ms) ──────────────
    _output_queue: list[str] = []

    def _on_pty_output(data: str) -> None:
        _output_queue.append(data)

    bridge.register_output_callback(_on_pty_output)

    def _flush_output() -> None:
        if not _output_queue:
            return
        batch = "".join(_output_queue)
        _output_queue.clear()
        terminal.write(batch)

    _output_timer = ui.timer(0.03, _flush_output)

    # ── Start PTY now (callback is already registered) ───────────────
    if not bridge.is_running:
        cwd = None
        try:
            from tools import registry
            fs_tool = registry.get_tool("filesystem")
            if fs_tool:
                root = fs_tool.get_config("workspace_root", "")
                if root:
                    cwd = root
        except Exception:
            pass
        bridge.start(cols=120, rows=30, cwd=cwd)
        try:
            loop = asyncio.get_running_loop()
            if bridge._reader_task is None or bridge._reader_task.done():
                bridge._reader_task = loop.create_task(bridge._reader_loop())
        except RuntimeError:
            pass

    # ── Cleanup on disconnect ────────────────────────────────────────
    def _on_disconnect():
        try:
            bridge.unregister_output_callback(_on_pty_output)
        except Exception:
            pass
        try:
            _output_timer.deactivate()
        except Exception:
            pass

    try:
        ui.context.client.on_disconnect(_on_disconnect)
    except Exception:
        pass
