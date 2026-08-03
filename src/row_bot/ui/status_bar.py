"""Row-Bot UI: status bar with avatar, health pills, and diagnosis button.

Replaces the old logo section on the home screen.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import time
from typing import Callable

from row_bot.brand import APP_BRAND_ACCENT, APP_BRAND_ACCENT_RGB, APP_DISPLAY_NAME
from row_bot.data_paths import get_row_bot_data_dir
from nicegui import run, ui
from row_bot.ui.timer_utils import safe_timer

from row_bot.ui.status_checks import (
    CheckResult,
    order_status_results,
    run_all_checks,
    run_heavy_checks,
    run_light_checks,
)
from row_bot.ui.performance import log_ui_perf

logger = logging.getLogger(__name__)

_DATA_DIR = get_row_bot_data_dir()
_USER_CONFIG_PATH = _DATA_DIR / "user_config.json"
_BUDDY_AVATAR_URL_CACHE: dict[str, str] = {}

# ═════════════════════════════════════════════════════════════════════════════
# AVATAR CONFIG
# ═════════════════════════════════════════════════════════════════════════════

_DEFAULT_EMOJI = "🤖"
_DEFAULT_COLOR = APP_BRAND_ACCENT

_AVATAR_EMOJIS = [
    "🤖", "🧠", "🦊", "🐱", "🦉", "🐙", "🎭", "👾", "🌀",
    "💎", "🔮", "🪐", "⚡", "🌊", "🐉", "🦋", "🍀", "🎯", "🏔️",
    "🌸", "🦁", "🐺", "🐝", "🦅", "🎵", "🔥", "❄️", "☀️", "🌙",
]

_RING_COLORS = [
    APP_BRAND_ACCENT, "#4caf50", "#2196f3", "#e91e63", "#9c27b0",
    "#ff5722", "#00bcd4", "#ff9800", "#8bc34a", "#607d8b",
    "#f44336", "#3f51b5", "#009688", "#cddc39", "#795548",
]

_STATUS_ICON_MAP = {
    "Ollama": "dns",
    "Model": "memory",
    "Cloud API": "cloud",
    "Tunnel": "settings_ethernet",
    "Gmail OAuth": "email",
    "Calendar OAuth": "event",
    "X OAuth": "alternate_email",
    "GitHub": "code",
    "Workflows": "flash_on",
    "Knowledge": "device_hub",
    "Dream Cycle": "brightness_3",
    "TTS": "volume_up",
    "Wiki Vault": "description",
    "Logging": "assignment",
    "Disk": "save",
    "Threads DB": "storage",
    "FAISS Index": "bubble_chart",
    "Documents": "folder",
    "Search": "search",
    "Skills": "extension",
    "Tracker": "playlist_add_check",
    "Buddy": "android",
    "MCP": "power",
    "Plugins": "widgets",
    "Network": "wifi",
    "Tools": "build",
}


def _load_avatar_config() -> dict:
    """Load avatar preferences from user_config.json."""
    try:
        if _USER_CONFIG_PATH.exists():
            data = json.loads(_USER_CONFIG_PATH.read_text(encoding="utf-8"))
            return data.get("avatar", {})
    except (json.JSONDecodeError, OSError):
        pass
    return {}


# ── Public helpers for chat avatar ───────────────────────────────────────

def get_bot_avatar_emoji() -> str:
    """Return the configured avatar emoji."""
    return _load_avatar_config().get("emoji", _DEFAULT_EMOJI)


def get_bot_avatar_html() -> str:
    """Return the inner HTML for the bot's chat avatar.

    If the user has a generated image avatar, returns an ``<img>`` tag.
    If the user explicitly picked a non-default emoji, returns that emoji.
    Otherwise returns the selected Buddy pack preview.
    """
    cfg = _load_avatar_config()
    if cfg.get("mode") == "image" and cfg.get("image"):
        b64 = cfg["image"]
        return f'<img src="data:image/png;base64,{b64}" alt="avatar" />'
    emoji = cfg.get("emoji")
    if emoji and emoji != _DEFAULT_EMOJI:
        return str(emoji)
    buddy_url = _get_buddy_avatar_url()
    if buddy_url:
        return f'<img src="{html.escape(buddy_url, quote=True)}" alt="Buddy avatar" />'
    return '<span class="material-icons" aria-hidden="true">smart_toy</span>'


def _get_buddy_avatar_url() -> str:
    try:
        from row_bot.buddy.assets import load_buddy_pack, static_url_for_path
        from row_bot.buddy.config import get_buddy_config

        pack_id = str(get_buddy_config().get("pack_id") or "glyph")
        cached = _BUDDY_AVATAR_URL_CACHE.get(pack_id)
        if cached:
            return cached
        pack = load_buddy_pack(pack_id)
        if pack.preview_path and pack.preview_path.exists():
            url = static_url_for_path(pack.preview_path)
            if url:
                _BUDDY_AVATAR_URL_CACHE[pack_id] = url
                return url
    except Exception:
        logger.debug("Could not resolve Buddy avatar preview", exc_info=True)
    return ""


def _save_avatar_config(avatar: dict) -> None:
    """Save avatar preferences to user_config.json."""
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        config = {}
        if _USER_CONFIG_PATH.exists():
            try:
                config = json.loads(_USER_CONFIG_PATH.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        config["avatar"] = avatar
        _USER_CONFIG_PATH.write_text(
            json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except OSError as exc:
        logger.warning("Failed to save avatar config: %s", exc)


# ═════════════════════════════════════════════════════════════════════════════
# STATUS CACHE (module-level, avoids re-running heavy checks on every render)
# ═════════════════════════════════════════════════════════════════════════════

_status_cache: dict[str, CheckResult] = {}
_cache_time: float = 0.0
_CACHE_TTL = 300.0  # 5 minutes for heavy checks
_status_refresh_task: asyncio.Task[list[CheckResult]] | None = None
_status_refresh_task_id: int = 0
_heavy_refresh_task: asyncio.Task[list[CheckResult]] | None = None
_heavy_refresh_task_id: int = 0
_refresh_generation = 0


def _next_refresh_id() -> int:
    global _refresh_generation
    _refresh_generation += 1
    return _refresh_generation


def _status_cache_stale() -> bool:
    return not _status_cache or time.time() - _cache_time > _CACHE_TTL


def _placeholder_results() -> list[CheckResult]:
    return [CheckResult("System", "inactive", "Checking status...")]


def _docs_capture_results() -> list[CheckResult] | None:
    """Return inert display-only status for public documentation capture."""
    from row_bot.docs_capture import is_docs_capture

    if not is_docs_capture():
        return None
    return [
        CheckResult("Model", "ok", "Demo provider ready", settings_tab="Providers"),
        CheckResult("Workflows", "ok", "2 configured", settings_tab=""),
        CheckResult("Knowledge", "ok", "Demo workspace indexed", settings_tab="Knowledge"),
        CheckResult("Documents", "ok", "3 demo documents", settings_tab="Documents"),
        CheckResult("Skills", "ok", "2 demo skills", settings_tab="Skills"),
        CheckResult("Plugins", "ok", "1 demo plugin", settings_tab="Plugins"),
        CheckResult("MCP", "inactive", "Demo server disabled", settings_tab="MCP"),
        CheckResult("Network", "inactive", "Disabled for documentation capture"),
    ]


def _get_render_cached_results() -> list[CheckResult]:
    """Return current status pills without running synchronous health checks."""
    started = time.perf_counter()
    results = order_status_results(list(_status_cache.values())) if _status_cache else _placeholder_results()
    log_ui_perf(
        "home.status_bar.cached",
        (time.perf_counter() - started) * 1000.0,
        threshold_ms=50.0,
        cached=bool(_status_cache),
        stale=_status_cache_stale(),
        results=len(results),
    )
    return results


def _get_cached_results() -> list[CheckResult]:
    """Return cached check results without running health checks."""
    return _get_render_cached_results()


def _force_refresh() -> list[CheckResult]:
    """Force-refresh all checks (bypasses cache)."""
    return _force_refresh_with_id(refresh_id=_next_refresh_id())


def _replace_status_cache(results: list[CheckResult]) -> list[CheckResult]:
    global _status_cache, _cache_time
    ordered = order_status_results(results)
    _status_cache = {r.name: r for r in ordered}
    _cache_time = time.time()
    return ordered


def _merge_status_cache(results: list[CheckResult], *, mark_fresh: bool = True) -> list[CheckResult]:
    global _status_cache, _cache_time
    merged = dict(_status_cache)
    for result in results:
        merged[result.name] = result
    ordered = order_status_results(list(merged.values()))
    _status_cache = {r.name: r for r in ordered}
    if mark_fresh:
        _cache_time = time.time()
    return ordered


def _force_refresh_with_id(*, refresh_id: int) -> list[CheckResult]:
    started = time.perf_counter()
    all_results = run_all_checks()
    ordered = _replace_status_cache(all_results)
    log_ui_perf(
        "home.status_bar.aggregate",
        (time.perf_counter() - started) * 1000.0,
        threshold_ms=500.0,
        refresh_id=refresh_id,
        results=len(ordered),
        cached=False,
        stale=False,
        mode="full",
    )
    log_ui_perf(
        "home.status_bar.force_refresh",
        0.0,
        threshold_ms=999999.0,
        refresh_id=refresh_id,
        results=len(ordered),
    )
    return ordered


def _refresh_light_results(*, refresh_id: int) -> list[CheckResult]:
    started = time.perf_counter()
    light_results = run_light_checks()
    merged = _merge_status_cache(light_results, mark_fresh=False)
    log_ui_perf(
        "home.status_bar.light_refresh",
        (time.perf_counter() - started) * 1000.0,
        threshold_ms=500.0,
        refresh_id=refresh_id,
        results=len(light_results),
        cache_results=len(merged),
        cached=False,
        stale=True,
    )
    return merged


def _refresh_heavy_results(*, refresh_id: int) -> list[CheckResult]:
    started = time.perf_counter()
    heavy_results = run_heavy_checks(live_ollama_probe=False)
    merged = _merge_status_cache(heavy_results)
    log_ui_perf(
        "home.status_bar.heavy_refresh",
        (time.perf_counter() - started) * 1000.0,
        threshold_ms=1500.0,
        refresh_id=refresh_id,
        results=len(heavy_results),
        cache_results=len(merged),
        cached=False,
        stale=False,
    )
    return merged


async def _coalesced_force_refresh() -> list[CheckResult]:
    """Run one shared full status refresh for concurrent Home renders."""
    global _status_refresh_task, _status_refresh_task_id
    refresh_id = _next_refresh_id()
    if _status_refresh_task is None or _status_refresh_task.done():
        _status_refresh_task_id = refresh_id
        _status_refresh_task = asyncio.create_task(
            run.io_bound(lambda: _force_refresh_with_id(refresh_id=_status_refresh_task_id))
        )
    else:
        refresh_id = _status_refresh_task_id
        log_ui_perf(
            "home.status_bar.async_refresh",
            0.0,
            threshold_ms=999999.0,
            refresh_id=refresh_id,
            coalesced=True,
            mode="full",
        )
    return await asyncio.shield(_status_refresh_task)


async def _coalesced_heavy_refresh(*, refresh_id: int | None = None) -> list[CheckResult]:
    """Run one shared heavy status refresh for concurrent Home renders."""
    global _heavy_refresh_task, _heavy_refresh_task_id
    if refresh_id is None:
        refresh_id = _next_refresh_id()
    if _heavy_refresh_task is None or _heavy_refresh_task.done():
        _heavy_refresh_task_id = refresh_id
        _heavy_refresh_task = asyncio.create_task(
            run.io_bound(lambda: _refresh_heavy_results(refresh_id=_heavy_refresh_task_id))
        )
    else:
        refresh_id = _heavy_refresh_task_id
        log_ui_perf(
            "home.status_bar.async_refresh",
            0.0,
            threshold_ms=999999.0,
            refresh_id=refresh_id,
            coalesced=True,
            mode="heavy",
        )
    return await asyncio.shield(_heavy_refresh_task)


# ═════════════════════════════════════════════════════════════════════════════
# STATUS BAR UI
# ═════════════════════════════════════════════════════════════════════════════

# CSS for the status panel (avatar removed — now in sidebar)
_AVATAR_CSS = """
<style>
@keyframes row-bot-wave-scroll {
    0%   { transform: translateX(0) translateY(-50%); }
    100% { transform: translateX(-50%) translateY(-50%); }
}
.row-bot-status-panel {
    background: rgba(30, 30, 30, 0.7);
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 14px;
    padding: 0.55rem 0.9rem;
    margin: 0.4rem 0.3rem 0.15rem 0.3rem;
    position: relative;
    overflow: hidden;
}
.row-bot-status-panel > * { position: relative; z-index: 1; }
.row-bot-status-panel::before {
    content: "";
    position: absolute;
    top: 50%; left: 0;
    width: 200%; height: 140%;
    z-index: 0;
    opacity: 0.18;
    pointer-events: none;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 600 100' preserveAspectRatio='none'%3E%3Cpath d='M0,50 L40,50 L50,50 L55,48 L60,55 L65,10 L70,90 L75,30 L80,50 L100,50 L140,50 L150,50 L155,48 L160,55 L165,10 L170,90 L175,30 L180,50 L200,50 L240,50 L250,50 L255,48 L260,55 L265,10 L270,90 L275,30 L280,50 L300,50 L340,50 L350,50 L355,48 L360,55 L365,10 L370,90 L375,30 L380,50 L400,50 L440,50 L450,50 L455,48 L460,55 L465,10 L470,90 L475,30 L480,50 L500,50 L540,50 L550,50 L555,48 L560,55 L565,10 L570,90 L575,30 L580,50 L600,50' fill='none' stroke='%234caf50' stroke-width='1' stroke-linejoin='round'/%3E%3C/svg%3E");
    background-repeat: repeat-x;
    background-size: 50% 100%;
    animation: row-bot-wave-scroll 14s linear infinite;
}
.status-pills-row {
    display: flex; flex-wrap: wrap; gap: 5px; align-items: center;
    justify-content: center;
}
.status-pill {
    display: inline-flex; align-items: center; justify-content: center; gap: 5px;
    min-width: 34px;
    height: 30px;
    padding: 0 8px;
    border-radius: 999px;
    font-size: 0.8rem;
    border: 1px solid rgba(255,255,255,0.1);
    cursor: pointer;
    transition: background 0.2s, border-color 0.2s, box-shadow 0.2s, transform 0.2s;
    white-space: nowrap;
    position: relative;
    background: rgba(255, 255, 255, 0.035);
    color: rgba(255, 255, 255, 0.86);
}
.status-pill:hover {
    background: rgba(255,255,255,0.1);
    transform: translateY(-1px);
}
.status-pill .dot {
    width: 9px; height: 9px;
    border-radius: 50%;
    display: inline-block;
    flex-shrink: 0;
}
.status-pill.status-icon-pill {
    width: 34px;
    padding: 0;
}
.status-pill.status-icon-pill .material-icons {
    font-size: 17px;
    line-height: 1;
}
.status-pill.status-icon-pill .dot {
    width: 7px;
    height: 7px;
    position: absolute;
    right: 5px;
    bottom: 5px;
    border: 1px solid rgba(20, 20, 20, 0.95);
}
.status-pill.status-warn {
    border-color: rgba(255, 167, 38, 0.65);
    box-shadow: 0 0 11px rgba(255, 167, 38, 0.30);
    color: #FFCC80;
}
.status-pill.status-error {
    border-color: rgba(239, 83, 80, 0.75);
    box-shadow: 0 0 13px rgba(239, 83, 80, 0.34);
    color: #EF9A9A;
}
.status-pill.status-inactive {
    color: rgba(255, 255, 255, 0.45);
}
.status-alert-badge {
    position: absolute;
    top: -5px;
    right: -5px;
    min-width: 13px;
    height: 13px;
    padding: 0 3px;
    border-radius: 999px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    font-size: 10px;
    line-height: 13px;
    font-weight: 800;
    color: #171717;
    background: #FFA726;
    border: 1px solid rgba(20, 20, 20, 0.9);
    box-shadow: 0 0 7px rgba(255, 167, 38, 0.55);
}
.status-pill.status-error .status-alert-badge {
    color: #fff;
    background: #EF5350;
    box-shadow: 0 0 8px rgba(239, 83, 80, 0.65);
}
.row-bot-buddy-hatch-progress {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    border: 1px solid #AB47BC;
    border-radius: 12px;
    padding: 3px 11px;
    font-size: 0.75rem;
    color: #CE93D8;
    background: rgba(171, 71, 188, 0.10);
    animation: pulse-border 2s infinite;
}
.row-bot-buddy-hatch-progress.done {
    border-color: #66bb6a;
    color: #a5d6a7;
    background: rgba(102, 187, 106, 0.10);
    animation: none;
}
.row-bot-buddy-hatch-progress.warn {
    border-color: #FFA726;
    color: #FFCC80;
    background: rgba(255, 167, 38, 0.10);
    animation: none;
}
.row-bot-buddy-hatch-progress.error {
    border-color: #ef5350;
    color: #ef9a9a;
    background: rgba(239, 83, 80, 0.10);
    animation: none;
}
.status-pill.inactive { opacity: 0.4; font-size: 0.75rem; }
.row-bot-gear-btn {
    width: 44px; height: 44px;
    border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    cursor: pointer;
    border: 1px solid rgba(160, 200, 220, 0.30);
    background: rgba(160, 200, 220, 0.08);
    color: #b0cfe0;
    font-size: 1.3rem;
    transition: background 0.2s, transform 0.2s, box-shadow 0.3s;
    flex-shrink: 0;
    box-shadow: 0 0 6px rgba(160, 200, 220, 0.20);
}
.row-bot-gear-btn:hover {
    background: rgba(160, 200, 220, 0.18);
    transform: scale(1.08);
    box-shadow: 0 0 12px rgba(160, 200, 220, 0.35);
}
.row-bot-diag-btn {
    width: 44px; height: 44px;
    border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    cursor: pointer;
    border: 1px solid rgba(__ROW_BOT_BRAND_ACCENT_RGB__, 0.32);
    background: rgba(__ROW_BOT_BRAND_ACCENT_RGB__, 0.10);
    color: __ROW_BOT_BRAND_ACCENT__;
    font-size: 1.3rem;
    transition: background 0.2s, transform 0.2s;
    flex-shrink: 0;
}
.row-bot-diag-btn:hover {
    background: rgba(__ROW_BOT_BRAND_ACCENT_RGB__, 0.20);
    transform: scale(1.08);
}
@keyframes row-bot-diag-spin {
    0%   { transform: rotate(0deg); }
    100% { transform: rotate(360deg); }
}
.row-bot-diag-spinning .material-icons {
    animation: row-bot-diag-spin 0.8s linear infinite;
}
@keyframes pulse-border {
    0%   { border-color: #FFA726; }
    50%  { border-color: #FF7043; }
    100% { border-color: #FFA726; }
}
</style>
"""

_AVATAR_CSS = _AVATAR_CSS.replace("__ROW_BOT_BRAND_ACCENT__", APP_BRAND_ACCENT).replace(
    "__ROW_BOT_BRAND_ACCENT_RGB__",
    APP_BRAND_ACCENT_RGB,
)



def build_status_bar(
    open_settings: Callable[[str], None],
) -> None:
    """Build the status bar that replaces the old logo section."""

    ui.html(_AVATAR_CSS, sanitize=False)

    # Paint cached status immediately; full checks refresh asynchronously.
    # Documentation capture uses a deterministic, display-only fixture so the
    # screenshot harness never probes providers, accounts, or the network.
    capture_results = _docs_capture_results()
    results = capture_results or _get_render_cached_results()
    result_map = {r.name: r for r in results}

    with ui.element("div").classes("row-bot-status-panel w-full"):
      with ui.row().classes("w-full items-center no-wrap gap-3").style(
          "min-height: 50px;"
      ):

        # ── LEFT: Settings gear icon ─────────────────────────────
        _gear_el = ui.html(
            '<div class="row-bot-gear-btn" title="Settings">'
            '<span class="material-icons" style="font-size:1.3rem;">settings</span>'
            '</div>',
            sanitize=False,
        ).on("click", lambda: open_settings(""))

        # ── CENTER: Status icons ─────────────────────────────────
        pills_container = ui.column().classes("flex-grow gap-1 items-center").style(
            "min-width: 0;"
        )

        def _render_pills(container, result_map: dict[str, CheckResult]):
            # Channel pills are now shown in the sidebar monitor
            _channel_names = set()
            try:
                from row_bot.channels.registry import all_channels
                _channel_names = {ch.display_name for ch in all_channels()}
            except Exception:
                pass

            container.clear()
            with container:
                items = [r for r in result_map.values() if r.name not in _channel_names]
                with ui.element("div").classes("status-pills-row"):
                    for r in items:
                        inactive_cls = " inactive" if r.status == "inactive" else ""
                        status_cls = f" status-{r.status}"
                        icon_name = _STATUS_ICON_MAP.get(r.name, "radio_button_checked")
                        alert_html = (
                            '<span class="status-alert-badge">!</span>'
                            if r.status in {"warn", "error"} else ""
                        )

                        def _pill_click(tab=r.settings_tab, nm=r.name):
                            if tab:
                                open_settings(tab)

                        pill_html = (
                            f'<span class="material-icons" aria-hidden="true">{icon_name}</span>'
                            f'<span class="dot" style="background:{r.dot_color};"></span>'
                            f'{alert_html}'
                        )
                        pill = ui.html(
                            f'<span class="status-pill status-icon-pill{status_cls}{inactive_cls}" '
                            f'aria-label="{r.name}: {r.status_label} - {r.detail}">'
                            f'{pill_html}</span>',
                            sanitize=False,
                        ).tooltip(f"{r.name}: {r.status_label} - {r.detail}")

                        if r.settings_tab:
                            pill.on("click", _pill_click)

        _render_pills(pills_container, result_map)

        async def _refresh_pills_if_needed() -> None:
            if not _status_cache_stale():
                return
            started = time.perf_counter()
            refresh_id = _next_refresh_id()
            try:
                refreshed = await run.io_bound(lambda: _refresh_light_results(refresh_id=refresh_id))
                _render_pills(pills_container, {r.name: r for r in refreshed})
                log_ui_perf(
                    "home.status_bar.async_refresh",
                    (time.perf_counter() - started) * 1000.0,
                    threshold_ms=500.0,
                    refresh_id=refresh_id,
                    mode="light",
                    coalesced=False,
                    results=len(refreshed),
                )
            except Exception:
                logger.debug("Status bar light refresh failed", exc_info=True)

            async def _refresh_heavy_pills() -> None:
                heavy_started = time.perf_counter()
                try:
                    heavy_refreshed = await _coalesced_heavy_refresh(refresh_id=refresh_id)
                    _render_pills(pills_container, {r.name: r for r in heavy_refreshed})
                    log_ui_perf(
                        "home.status_bar.async_refresh",
                        (time.perf_counter() - heavy_started) * 1000.0,
                        threshold_ms=999999.0,
                        refresh_id=refresh_id,
                        mode="heavy",
                        results=len(heavy_refreshed),
                    )
                except Exception:
                    logger.debug("Status bar heavy refresh failed", exc_info=True)

            asyncio.create_task(_refresh_heavy_pills())

        if capture_results is None:
            asyncio.create_task(_refresh_pills_if_needed())

        # ── RIGHT: Diagnosis button ───────────────────────────────
        _diagnosis_running = False

        async def _run_diagnosis():
            """Force-refresh and show full diagnosis dialog."""
            nonlocal _diagnosis_running
            if _diagnosis_running:
                return
            _diagnosis_running = True
            # Show spinner while checks run
            diag_btn_el.classes(add='row-bot-diag-spinning')
            await asyncio.sleep(0.05)  # let UI update
            try:
                diag_results = await _coalesced_force_refresh()
            except Exception:
                logger.debug("Status diagnosis refresh failed; using cached results", exc_info=True)
                diag_results = _get_render_cached_results()
            finally:
                diag_btn_el.classes(remove='row-bot-diag-spinning')
                _diagnosis_running = False
            elapsed = max(
                r.checked_at for r in diag_results
            ) - min(r.checked_at for r in diag_results) if diag_results else 0

            with ui.dialog() as dlg, ui.card().style(
                "min-width: 420px; max-width: 520px;"
            ):
                with ui.row().classes("w-full items-center justify-between"):
                    ui.label("🔍 System Diagnosis").classes("text-subtitle1 font-bold")
                    ui.button(icon="close", on_click=dlg.close).props("flat dense round size=sm")
                ui.separator()

                for r in diag_results:
                    with ui.expansion(
                        text=r.name,
                        icon=r.icon,
                    ).classes("w-full").props("dense") as exp:
                        exp.style(f"border-left: 3px solid {r.dot_color};")
                        with ui.row().classes("w-full items-center no-wrap gap-2"):
                            ui.html(
                                f'<span class="dot" style="background:{r.dot_color}; '
                                f'width:8px; height:8px; border-radius:50%; display:inline-block;"></span>',
                                sanitize=False,
                            )
                            ui.label(r.name).classes("font-bold")
                            ui.space()
                            ui.label(r.status_label).style(f"color: {r.dot_color};").classes("text-sm")
                        ui.label(r.detail).classes("text-xs text-grey-6 q-ml-md")

                ui.separator()
                with ui.row().classes("w-full items-center justify-between"):
                    ui.label(f"Checked {len(diag_results)} services").classes("text-xs text-grey-6")

                    def _copy_report():
                        lines = [f"{APP_DISPLAY_NAME} System Diagnosis", "=" * 40]
                        for r in diag_results:
                            icon = {"ok": "✅", "warn": "⚠️", "error": "❌", "inactive": "⬜"}.get(r.status, "?")
                            lines.append(f"{icon} {r.name}: {r.status_label} — {r.detail}")
                        report = "\n".join(lines)
                        ui.run_javascript(
                            f'navigator.clipboard.writeText({json.dumps(report)})'
                        )
                        ui.notify("Report copied to clipboard", type="positive")

                    with ui.row().classes("gap-2"):
                        ui.button("📋 Copy Report", on_click=_copy_report).props(
                            "flat dense no-caps"
                        )
                        ui.button("Close", on_click=dlg.close).props("dense")

            dlg.open()
            # Also refresh the pills behind the dialog
            new_map = {r.name: r for r in diag_results}
            _render_pills(pills_container, new_map)

        diag_btn_el = ui.html(
            '<div class="row-bot-diag-btn" title="Run system diagnosis">'
            '<span class="material-icons" style="font-size:1.3rem;">health_and_safety</span>'
            '</div>',
            sanitize=False,
        ).on("click", lambda: _run_diagnosis())

      # ── DOCUMENT INGESTION pill (below status row) ──────────────
      extraction_pill = ui.html("", sanitize=False)
      extraction_pill.set_visibility(False)
      extraction_pill.style("text-align: center; margin-top: 4px;")

      def _poll_extraction_status() -> None:
          """Timer callback — show the durable active document and stage."""
          try:
              from row_bot.document_jobs import DocumentJobService
          except ImportError:
              return
          summary = DocumentJobService().active_summary()
          active = summary.get("active")
          remaining = int(summary.get("remaining") or 0)
          paused = bool(summary.get("paused"))
          if not active and not (paused and remaining):
              extraction_pill.set_visibility(False)
              return
          active = active or {}
          fname = html.escape(str(active.get("original_name") or "Document queue"))
          stage = str(active.get("stage") or "paused")
          stage_label = {
              "upload": "uploading",
              "parse": "parsing",
              "embed": "embedding",
              "index_commit": "publishing search index",
              "knowledge_map": "extracting knowledge",
              "knowledge_reduce": "compiling knowledge",
              "knowledge_commit": "saving knowledge",
              "finalize": "finalizing",
              "paused": "paused",
          }.get(stage, stage.replace("_", " "))
          if active.get("status") == "indexing":
              progress = int(active.get("index_progress_current") or 0)
              total = int(active.get("index_progress_total") or 0)
          else:
              progress = int(active.get("extraction_progress_current") or 0)
              total = int(active.get("extraction_progress_total") or 0)
          progress_text = f" · {progress}/{total}" if total else ""
          paused_text = " · paused" if paused else ""
          cancel_html = (
              '<span id="document-ingestion-cancel-btn" style="cursor:pointer; margin-left:4px;" '
              'title="Cancel active document">⏹ Cancel active</span>'
              if active else ""
          )
          extraction_pill.set_content(
              f'<span style="display:inline-flex; align-items:center; gap:6px; '
              f'border:1px solid #FFA726; border-radius:12px; padding:2px 10px; '
              f'font-size:0.75rem; color:#FFA726; animation:pulse-border 2s infinite;">'
              f'📄 {fname} · {stage_label}{progress_text} · {remaining} remaining{paused_text}'
              f'{cancel_html}'
              f'</span>'
          )
          extraction_pill.set_visibility(True)

      safe_timer(2.0, _poll_extraction_status)

      # ── BUDDY HATCH PROGRESS pill ─────────────────────────────
      buddy_hatch_pill = ui.html("", sanitize=False)
      buddy_hatch_pill.set_visibility(False)
      buddy_hatch_pill.style("text-align: center; margin-top: 4px;")

      def _poll_buddy_hatch_status() -> None:
          try:
              from row_bot.buddy.hatch import get_hatch_generation_status
          except Exception:
              buddy_hatch_pill.set_visibility(False)
              return
          status = get_hatch_generation_status()
          if not status:
              buddy_hatch_pill.set_visibility(False)
              return
          state = str(status.get("status") or "")
          if state not in {"queued", "running", "completed", "partial", "failed"}:
              buddy_hatch_pill.set_visibility(False)
              return
          finished_at = float(status.get("finished_at") or 0.0)
          if finished_at and time.time() - finished_at > 90:
              buddy_hatch_pill.set_visibility(False)
              return
          completed = int(status.get("completed_clips") or 0)
          total = int(status.get("total_clips") or 0)
          pct = int(completed / total * 100) if total else 0
          bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
          message = str(status.get("message") or "Buddy generation")
          css_state = {"completed": " done", "partial": " warn", "failed": " error"}.get(state, "")
          clip_text = f" · {bar} {completed}/{total}" if total and state in {"queued", "running"} else ""
          buddy_hatch_pill.set_content(
              f'<span class="row-bot-buddy-hatch-progress{css_state}" title="Buddy Hatch generation status">'
              f'<span class="material-icons" style="font-size:14px;">auto_fix_high</span>'
              f'{message}{clip_text}'
              f'</span>'
          )
          buddy_hatch_pill.set_visibility(True)

      safe_timer(2.0, _poll_buddy_hatch_status)

      # The only status-bar action targets the active document.
      extraction_pill.on("click", lambda: _handle_extraction_stop())

      def _handle_extraction_stop():
          try:
              from row_bot.document_jobs import DocumentJobService

              service = DocumentJobService()
              active = service.active_summary().get("active")
              if isinstance(active, dict) and active.get("id"):
                  service.cancel_job(str(active["id"]))
                  ui.notify("Stopping the active document…", type="info")
          except Exception:
              logger.debug("Could not cancel active document ingestion", exc_info=True)

      # ── UPDATE-AVAILABLE pill ────────────────────────────────
      update_pill = ui.html("", sanitize=False)
      update_pill.set_visibility(False)
      update_pill.style("text-align: center; margin-top: 4px; cursor: pointer;")

      def _refresh_update_pill() -> None:
          try:
              import row_bot.updater as updater # noqa: WPS433 (local import keeps startup light)
              info = updater.get_update_state().available
          except Exception:
              return
          if info is None:
              update_pill.set_visibility(False)
              return
          update_pill.set_content(
              f'<span class="status-pill" style="border:1px solid #66bb6a; '
              f'color:#66bb6a; background:rgba(102,187,106,0.08);" '
              f'title="New version available — click to view">'
              f'<span class="dot" style="background:#66bb6a;"></span>'
              f'⬆ v{info.version}'
              f'</span>'
          )
          update_pill.set_visibility(True)

      def _open_update_dialog() -> None:
          try:
              from row_bot.ui.update_dialog import show_update_dialog
              show_update_dialog()
          except Exception as exc:
              logger.debug("Failed to open update dialog: %s", exc)

      update_pill.on("click", lambda: _open_update_dialog())
      _refresh_update_pill()
      safe_timer(60.0, _refresh_update_pill)
