"""NiceGUI surfaces for Buddy."""

from __future__ import annotations

import html
import json
import base64
import pathlib
import uuid

from nicegui import run, ui

from buddy.assets import delete_generated_buddy_pack, list_buddy_packs, load_buddy_pack, static_url_for_path
from buddy.brain import get_buddy_snapshot
from buddy.config import get_buddy_config, save_buddy_config, set_buddy_config
from buddy.events import BuddyEventType, emit_buddy_event
from buddy.hatch import (
    activate_hatch_art,
    activate_hatch_motion,
    activate_hatch_motion_pack,
    create_hatch_draft,
    generate_hatch_buddy,
    generate_hatch_motion_pack,
    motion_clip_specs,
    use_hatch_still_only,
)
from ui.confirm import confirm_destructive

_BUDDY_HEAD = """
<script>
(() => {
    if (window.location && window.location.pathname === '/buddy-overlay') {
        document.documentElement.classList.add('thoth-buddy-overlay-html');
        document.documentElement.style.background = 'transparent';
        document.documentElement.style.backgroundColor = 'transparent';
    }
})();
</script>
<script src="/static/buddy/runtime/buddy.js?v=buddy-polish-v4"></script>
<style>
html.thoth-buddy-overlay-html,
html.thoth-buddy-overlay-html body,
html.thoth-buddy-overlay-html #app,
html.thoth-buddy-overlay-html .nicegui-layout,
html.thoth-buddy-overlay-html .q-layout,
html.thoth-buddy-overlay-html .q-page-container,
html.thoth-buddy-overlay-html .q-page,
html.thoth-buddy-overlay-html .nicegui-content {
    background: transparent !important;
    background-color: transparent !important;
}
html.thoth-buddy-overlay-html,
html.thoth-buddy-overlay-html body {
    margin: 0 !important;
    overflow: hidden !important;
}
.thoth-buddy-wrap {
  --buddy-energy: 60;
  --buddy-focus: 20;
  --buddy-alert: 0;
  width: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
}
.thoth-buddy-stage {
  position: relative;
    width: 132px;
    height: 132px;
  border-radius: 8px;
  overflow: hidden;
    background: transparent;
    border: 1px solid transparent;
}
.thoth-buddy-stage canvas {
  width: 100%;
  height: 100%;
  display: block;
}
.thoth-buddy-stage::after {
  content: '';
  position: absolute;
  inset: 8px;
  border-radius: 999px;
    border: 1px solid rgba(77, 184, 171, calc(var(--buddy-focus) / 260));
  box-shadow: 0 0 calc(var(--buddy-alert) * 0.18px) rgba(247, 118, 87, 0.55);
    transition: border-color 280ms ease, box-shadow 280ms ease, opacity 280ms ease;
  pointer-events: none;
}
.thoth-buddy-wrap[data-surface="sidebar"] {
    gap: 8px;
}
.thoth-buddy-sidebar-action {
    position: relative;
    width: 214px;
    height: 214px;
    display: grid;
    place-items: center;
    border-radius: 999px;
    cursor: default;
    outline: none;
}
.thoth-buddy-sidebar-action::before {
    content: '';
    position: absolute;
    inset: 8px;
    border-radius: 999px;
    border: 1px dashed rgba(77, 184, 171, 0.28);
    box-shadow: inset 0 0 24px rgba(77, 184, 171, 0.08), 0 0 18px rgba(228, 194, 94, 0.10);
    opacity: 0;
    transform: scale(0.96);
    transition: opacity 180ms ease, transform 180ms ease, border-color 180ms ease;
    pointer-events: none;
}
.thoth-buddy-sidebar-action.thoth-buddy-dock-empty::before {
    opacity: 1;
    transform: scale(1);
    animation: thoth-buddy-sidebar-ring 3.2s ease-in-out infinite;
}
.thoth-buddy-sidebar-action.thoth-buddy-dock-hover::before {
    border-color: rgba(228, 194, 94, 0.58);
    box-shadow: inset 0 0 24px rgba(228, 194, 94, 0.08), 0 0 20px rgba(228, 194, 94, 0.22);
}
.thoth-buddy-sidebar-action:focus-visible .thoth-buddy-stage {
    box-shadow: 0 0 0 2px rgba(228, 194, 94, 0.54), 0 10px 28px rgba(0, 0, 0, 0.24);
}
.thoth-buddy-in-app {
    position: relative;
    z-index: 2;
    width: 198px;
    cursor: grab;
    touch-action: none;
    user-select: none;
}
.thoth-buddy-in-app.thoth-buddy-dragging {
    cursor: grabbing;
}
.thoth-buddy-in-app.thoth-buddy-undocked {
    position: fixed;
    right: 18px;
    bottom: 18px;
    width: 158px;
    z-index: 10000;
    pointer-events: auto;
}
.thoth-buddy-wrap[data-surface="sidebar"] .thoth-buddy-stage {
    width: 198px;
    height: 198px;
    border-radius: 999px;
    border: 1px solid rgba(77, 184, 171, 0.34);
    box-shadow: 0 0 0 1px rgba(228, 194, 94, 0.10), 0 10px 28px rgba(0, 0, 0, 0.24);
}
.thoth-buddy-wrap[data-surface="sidebar"] .thoth-buddy-stage::after {
    inset: 5px;
    border: 1px solid rgba(77, 184, 171, calc(0.20 + var(--buddy-focus) / 260));
    box-shadow:
        0 0 calc(8px + var(--buddy-focus) * 0.12px) rgba(77, 184, 171, 0.24),
        0 0 calc(var(--buddy-alert) * 0.22px) rgba(247, 118, 87, 0.62);
    animation: thoth-buddy-sidebar-ring 2.8s ease-in-out infinite;
}
.thoth-buddy-wrap[data-surface="sidebar"][data-animation^="celebrate"] .thoth-buddy-stage::after,
.thoth-buddy-wrap[data-surface="sidebar"][data-mood="proud"] .thoth-buddy-stage::after {
    border-color: rgba(228, 194, 94, 0.58);
    box-shadow: 0 0 22px rgba(228, 194, 94, 0.35);
}
.thoth-buddy-wrap[data-surface="sidebar"][data-mood="concerned"] .thoth-buddy-stage::after,
.thoth-buddy-wrap[data-surface="sidebar"][data-animation="worry"] .thoth-buddy-stage::after {
    border-color: rgba(247, 118, 87, 0.56);
    box-shadow: 0 0 22px rgba(247, 118, 87, 0.36);
}
.thoth-buddy-wrap[data-surface="sidebar"][data-animation="tap_glass"] .thoth-buddy-stage::after,
.thoth-buddy-wrap[data-surface="sidebar"][data-animation="pause"] .thoth-buddy-stage::after {
    border-color: rgba(228, 194, 94, 0.46);
    box-shadow:
        0 0 14px rgba(228, 194, 94, 0.20),
        0 0 18px rgba(77, 184, 171, 0.14);
    animation-duration: 4.2s;
}
.thoth-buddy-pack-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(132px, 1fr));
    gap: 10px;
    width: 100%;
}
.thoth-buddy-pack-card.q-btn {
    width: 100%;
    height: 174px;
    min-height: 174px;
    padding: 8px !important;
    border: 1px solid rgba(148, 163, 184, 0.22);
    border-radius: 8px;
    background: linear-gradient(180deg, rgba(18, 26, 38, 0.84), rgba(10, 15, 23, 0.68));
    color: inherit;
    overflow: hidden;
    text-align: left;
    transition: border-color 160ms ease, background 160ms ease, box-shadow 160ms ease, transform 160ms ease;
}
.thoth-buddy-pack-card .q-btn__content {
    width: 100%;
    height: 100%;
    display: flex;
    flex-direction: column;
    align-items: stretch;
    justify-content: flex-start;
    gap: 7px;
    flex-wrap: nowrap;
}
.thoth-buddy-pack-card:hover {
    border-color: rgba(79, 163, 255, 0.58);
    background: linear-gradient(180deg, rgba(24, 36, 54, 0.94), rgba(12, 18, 28, 0.76));
    transform: translateY(-1px);
}
.thoth-buddy-pack-card-selected.q-btn {
    border-color: rgba(79, 163, 255, 0.92);
    box-shadow: 0 0 0 1px rgba(79, 163, 255, 0.34), 0 10px 24px rgba(0, 0, 0, 0.22);
    background: linear-gradient(180deg, rgba(24, 52, 81, 0.96), rgba(11, 27, 42, 0.82));
}
.thoth-buddy-pack-preview {
    width: 100%;
    height: 104px;
    flex: 0 0 104px;
    border-radius: 8px;
    overflow: hidden;
    background: #070a0e;
    border: 1px solid rgba(148, 163, 184, 0.16);
}
.thoth-buddy-pack-preview .q-img__image {
    object-fit: contain !important;
}
.thoth-buddy-pack-preview-empty {
    display: flex;
    align-items: center;
    justify-content: center;
    border: 1px dashed rgba(148, 163, 184, 0.28);
    color: rgba(148, 163, 184, 0.76);
}
.thoth-buddy-pack-title {
    width: 100%;
    min-width: 0;
    color: #dce7f7;
    font-size: 12px;
    font-weight: 700;
    line-height: 1.15;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.thoth-buddy-pack-meta {
    width: 100%;
    min-width: 0;
    color: #9fb0c3;
    font-size: 10px;
    line-height: 1.2;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.thoth-buddy-pack-card-selected .thoth-buddy-pack-title {
    color: #8fc7ff;
}
@keyframes thoth-buddy-sidebar-ring {
    0%, 100% { opacity: 0.72; transform: scale(0.985); }
    50% { opacity: 1; transform: scale(1.015); }
}
.thoth-buddy-fallback {
  position: absolute;
  inset: 0;
  display: grid;
  place-items: center;
  color: #e4c25e;
  font-size: 42px;
  opacity: 0;
  transition: opacity 0.18s ease;
}
.thoth-buddy-wrap.buddy-unavailable .thoth-buddy-fallback { opacity: 1; }
.thoth-buddy-status {
    display: none;
  max-width: 168px;
  color: #8f9baa;
  font-size: 10px;
  line-height: 1.2;
  text-align: center;
}
.thoth-buddy-wrap[data-surface="sidebar"] .thoth-buddy-status {
        display: block;
        max-width: 198px;
        min-height: 13px;
        color: #a7b3c2;
}
.thoth-buddy-in-app.thoth-buddy-undocked .thoth-buddy-stage {
    width: 154px;
    height: 154px;
    filter: drop-shadow(0 16px 24px rgba(0, 0, 0, 0.34));
    border: 0;
}
.thoth-buddy-in-app.thoth-buddy-undocked .thoth-buddy-stage::after {
    display: none;
}
.thoth-buddy-in-app.thoth-buddy-undocked .thoth-buddy-wrap,
.thoth-buddy-overlay-page .thoth-buddy-wrap {
    position: relative;
}
.thoth-buddy-overlay-page .thoth-buddy-wrap {
    width: 100%;
    height: 100%;
    justify-content: center;
    gap: 4px;
}
.thoth-buddy-in-app.thoth-buddy-undocked .thoth-buddy-status,
.thoth-buddy-overlay-page .thoth-buddy-status {
    display: block;
    max-width: 172px;
    min-height: 18px;
    margin-top: -4px;
    padding: 6px 8px;
    color: #d7dee8;
    background: rgba(9, 13, 18, 0.78);
    border: 1px solid rgba(77, 184, 171, 0.26);
    border-radius: 8px;
    box-shadow: 0 10px 22px rgba(0, 0, 0, 0.24);
    backdrop-filter: blur(8px);
}
.thoth-buddy-wrap[data-bubble-verbosity="quiet"][data-surface="floating"] .thoth-buddy-status,
.thoth-buddy-wrap[data-bubble-verbosity="quiet"][data-surface="desktop"] .thoth-buddy-status:empty {
    display: none;
}
.thoth-buddy-overlay-page {
    position: fixed;
    inset: 0;
  width: 100vw;
  height: 100vh;
  overflow: hidden;
  display: grid;
  place-items: center;
    background: transparent;
}
html.thoth-buddy-overlay-html,
body.thoth-buddy-overlay-body,
body.thoth-buddy-overlay-body .nicegui-layout,
body.thoth-buddy-overlay-body .q-layout,
body.thoth-buddy-overlay-body .q-page-container,
body.thoth-buddy-overlay-body .q-page,
body.thoth-buddy-overlay-body .nicegui-content {
    background: transparent !important;
    background-color: transparent !important;
}
body.thoth-buddy-overlay-body {
    margin: 0 !important;
    overflow: hidden !important;
}
.thoth-buddy-overlay-page .thoth-buddy-stage {
    width: min(68vw, 176px);
    height: min(68vw, 176px);
}
.thoth-buddy-overlay-page .thoth-buddy-status {
        max-width: min(88vw, 218px);
        min-height: 24px;
        max-height: 58px;
        overflow: hidden;
        text-overflow: ellipsis;
        font-size: 11px;
        line-height: 1.25;
        background: rgba(9, 13, 18, 0.86);
        border-color: rgba(228, 194, 94, 0.34);
}
</style>
"""

_injected_head_clients: set[str] = set()
_buddy_pump_clients: set[str] = set()

_PERSONALITY_OPTIONS = {
    "warm_mystical": "Warm mystical",
    "calm_focus": "Calm focus",
    "playful_helper": "Playful helper",
    "quiet_guardian": "Quiet guardian",
    "curious_scholar": "Curious scholar",
}

_PERSONALITY_GENERATION_HINTS = {
    "warm_mystical": "gentle, luminous, encouraging, and a little mysterious",
    "calm_focus": "minimal, steady, precise, and designed for deep work",
    "playful_helper": "bright, expressive, nimble, and visibly helpful without feeling noisy",
    "quiet_guardian": "protective, quiet, observant, and reassuring",
    "curious_scholar": "bookish, inquisitive, analytical, and warmly attentive",
}

_BUBBLE_OPTIONS = {
    "quiet": "Quiet",
    "normal": "Normal",
    "chatty": "Chatty",
}


def _compose_hatch_prompt(concept: str, personality: str, notes: str) -> str:
    safe_concept = (concept or "A cute tiny mystical coding familiar for Thoth").strip()
    personality_id = personality if personality in _PERSONALITY_OPTIONS else "warm_mystical"
    hint = _PERSONALITY_GENERATION_HINTS.get(personality_id, _PERSONALITY_GENERATION_HINTS["warm_mystical"])
    parts = [
        safe_concept,
        f"Personality style: {_PERSONALITY_OPTIONS[personality_id]} - {hint}.",
    ]
    safe_notes = (notes or "").strip()
    if safe_notes:
        parts.append(f"Generation guidance from the user: {safe_notes}")
    parts.append("Keep the character readable as a compact desktop companion across idle, thinking, working, approval, success, and error motion states.")
    return "\n\n".join(parts)

def inject_buddy_head() -> None:
    client = getattr(ui.context, "client", None)
    client_id = str(getattr(client, "id", "")) if client is not None else ""
    if client_id:
        if client_id in _injected_head_clients:
            return
        _injected_head_clients.add(client_id)
    ui.add_head_html(_BUDDY_HEAD)


def _motion_pack_payload(manifest_path: str) -> dict:
    if not manifest_path:
        return {}
    manifest_file = pathlib.Path(manifest_path).expanduser()
    if not manifest_file.exists() or not manifest_file.is_file():
        return {}
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except Exception:
        return {}
    clips = manifest.get("clips") if isinstance(manifest.get("clips"), dict) else {}
    payload_clips: dict[str, dict[str, str]] = {}
    for clip_id, entry in clips.items():
        if not isinstance(entry, dict):
            continue
        clip_path = (manifest_file.parent / str(entry.get("path") or f"{clip_id}.mp4")).resolve()
        if not clip_path.exists():
            continue
        payload_clips[str(clip_id)] = {
            "src": static_url_for_path(clip_path),
            "label": str(entry.get("label") or clip_id).title(),
        }
    if not payload_clips:
        return {}
    animation_map = manifest.get("animation_map") if isinstance(manifest.get("animation_map"), dict) else {}
    return {
        "defaultClip": str(manifest.get("default_clip") or "idle"),
        "animationMap": {str(k): str(v) for k, v in animation_map.items()},
        "clips": payload_clips,
    }


def _surface_html(surface: str) -> str:
    cfg = get_buddy_config()
    bubble_verbosity = str(cfg.get("bubble_verbosity") or "normal")
    personality = str(cfg.get("personality") or "warm_mystical")
    active_preview = str(cfg.get("active_hatch_preview") or "")
    preview_path = active_preview if active_preview and pathlib.Path(active_preview).expanduser().exists() else ""
    if not preview_path and cfg.get("latest_hatch_preview"):
        try:
            preview_path = str(activate_hatch_art(str(cfg.get("latest_hatch_preview") or "")))
        except Exception:
            preview_path = str(cfg.get("latest_hatch_preview") or "")
    preview_url = static_url_for_path(preview_path) if preview_path else ""
    active_motion = str(cfg.get("active_hatch_motion") or "")
    motion_path = active_motion if active_motion and pathlib.Path(active_motion).expanduser().exists() else ""
    if not motion_path and cfg.get("latest_hatch_motion"):
        try:
            motion_path = str(activate_hatch_motion(str(cfg.get("latest_hatch_motion") or "")))
        except Exception:
            motion_path = str(cfg.get("latest_hatch_motion") or "")
    motion_url = static_url_for_path(motion_path) if motion_path else ""
    active_motion_pack = str(cfg.get("active_hatch_motion_pack") or "")
    motion_pack_path = active_motion_pack if active_motion_pack and pathlib.Path(active_motion_pack).expanduser().exists() else ""
    if not motion_pack_path and cfg.get("latest_hatch_motion_pack"):
        try:
            motion_pack_path = str(activate_hatch_motion_pack(str(cfg.get("latest_hatch_motion_pack") or "")))
        except Exception:
            motion_pack_path = str(cfg.get("latest_hatch_motion_pack") or "")
    if not preview_path and not motion_pack_path:
        pack = load_buddy_pack(str(cfg.get("pack_id") or "glyph"))
        if pack.runtime in {"generated_motion_pack", "generated_still"} and pack.status == "available":
            preview_path = str(pack.preview_path)
            preview_url = static_url_for_path(preview_path)
            if pack.runtime == "generated_motion_pack" and not motion_path and pack.default_clip in pack.motion_clips:
                motion_path = str(pack.motion_clips[pack.default_clip])
                motion_url = static_url_for_path(motion_path)
                motion_pack_path = str(pack.motion_pack_path)
    motion_pack_json = json.dumps(_motion_pack_payload(motion_pack_path), separators=(",", ":")) if motion_pack_path else "{}"
    element_id = f"buddy-{surface}-{uuid.uuid4().hex[:10]}"
    unavailable = "Loading motion pack" if motion_pack_json != "{}" else ("Loading motion" if motion_url else ("Loading companion" if preview_url else "Generate a companion look to activate animation"))
    return f"""
    <div id="{element_id}" class="thoth-buddy-wrap" data-thoth-buddy data-surface="{html.escape(surface)}" data-personality="{html.escape(personality)}" data-bubble-verbosity="{html.escape(bubble_verbosity)}" data-preview="{html.escape(preview_url)}" data-motion="{html.escape(motion_url)}" data-motion-pack="{html.escape(motion_pack_json, quote=True)}">
      <div class="thoth-buddy-stage">
        <canvas id="{element_id}-canvas" width="220" height="220" aria-label="Companion animation"></canvas>
        <div class="thoth-buddy-fallback" aria-hidden="true">*</div>
      </div>
      <div class="thoth-buddy-status buddy-status">{html.escape(unavailable)}</div>
    </div>
    """


def _clear_hatch_media_overrides(cfg: dict) -> None:
    for key in (
        "active_hatch_preview",
        "active_hatch_motion",
        "active_hatch_motion_pack",
        "active_hatch_motion_clips",
        "latest_hatch_preview",
        "latest_hatch_motion",
        "latest_hatch_motion_pack",
        "latest_hatch_motion_error",
    ):
        cfg.pop(key, None)


def _refresh_existing_buddy_surfaces() -> None:
    surface_html = {
        "sidebar": _surface_html("sidebar"),
        "floating": _surface_html("floating"),
        "desktop": _surface_html("desktop"),
    }
    code = f"""
        (() => {{
            const replacements = {json.dumps(surface_html)};
            document.querySelectorAll('[data-thoth-buddy]').forEach((element) => {{
                const surface = element.dataset.surface;
                const html = replacements[surface];
                if (!html) return;
                const wrapper = document.createElement('div');
                wrapper.innerHTML = html.trim();
                const next = wrapper.firstElementChild;
                if (next) element.replaceWith(next);
            }});
            setTimeout(() => window.ThothBuddy && window.ThothBuddy.initAll(), 80);
        }})();
        """
    delivered = False
    try:
        current_client = ui.context.client
        instances = getattr(current_client.__class__, "instances", {})
    except Exception:
        instances = {}
    for client in list(getattr(instances, "values", lambda: [])()):
        if not _client_is_live(client):
            continue
        try:
            client.run_javascript(code)
            delivered = True
        except RuntimeError as exc:
            if "deleted" not in str(exc).lower():
                raise
    if not delivered:
        ui.run_javascript(code)


def _client_is_live(client) -> bool:
    client_id = getattr(client, "id", "")
    instances = getattr(client.__class__, "instances", {})
    return bool(client_id) and not getattr(client, "_deleted", False) and client_id in instances


def _push_snapshot(client=None) -> None:
    if client is not None and not _client_is_live(client):
        return
    snapshot = get_buddy_snapshot()
    code = (
        f"if (!window.__THOTH_BUDDY_HOLD_SNAPSHOT && window.ThothBuddy) "
        f"window.ThothBuddy.setState({json.dumps(snapshot)});"
    )
    try:
        if client is not None:
            client.run_javascript(code)
        else:
            ui.run_javascript(code)
    except RuntimeError as exc:
        if "deleted" not in str(exc).lower():
            raise


def _ensure_buddy_client_runtime() -> None:
    try:
        client = ui.context.client
    except Exception:
        return
    client_id = getattr(client, "id", "")
    if not client_id or client_id in _buddy_pump_clients:
        return
    _buddy_pump_clients.add(client_id)
    with client:
        pump_timer = ui.timer(0.6, lambda: _push_snapshot(client))

    def _cleanup() -> None:
        _buddy_pump_clients.discard(client_id)
        try:
            pump_timer.cancel(with_current_invocation=True)
        except Exception:
            try:
                pump_timer.deactivate()
            except Exception:
                pass

    try:
        client.on_disconnect(_cleanup)
    except Exception:
        pass


def build_buddy_surface(surface: str = "sidebar"):
    inject_buddy_head()
    _ensure_buddy_client_runtime()
    root = ui.html(_surface_html(surface), sanitize=False)
    ui.run_javascript("setTimeout(() => window.ThothBuddy && window.ThothBuddy.initAll(), 100);")
    return root


def _emit_buddy_hi() -> None:
    emit_buddy_event(BuddyEventType.NOTIFICATION, source="buddy.ui", payload={"label": "Hi"})


def _apply_buddy_surface_settings(cfg: dict) -> None:
    enabled = bool(cfg.get("enabled", True))
    in_app_enabled = enabled and bool(cfg.get("sidebar_enabled", True))
    desktop_enabled = enabled and bool(cfg.get("desktop_enabled", False))
    bubble_verbosity = str(cfg.get("bubble_verbosity") or "normal")
    personality = str(cfg.get("personality") or "warm_mystical")
    ui.run_javascript(
        f"""
        (() => {{
            const inAppVisible = {json.dumps(in_app_enabled)};
            const bubbleVerbosity = {json.dumps(bubble_verbosity)};
            const personality = {json.dumps(personality)};
            const notify = (message, type) => {{
                if (window.Quasar && window.Quasar.Notify) {{
                    window.Quasar.Notify.create({{ message, type: type || 'info', timeout: 2600 }});
                }}
            }};
            document.querySelectorAll('[data-buddy-sidebar-shell]').forEach((element) => {{
                element.style.display = inAppVisible ? '' : 'none';
            }});
            document.querySelectorAll('[data-buddy-in-app-shell]').forEach((element) => {{
                element.style.display = inAppVisible ? '' : 'none';
            }});
            if (!inAppVisible && window.ThothBuddyDock) window.ThothBuddyDock.resetAll();
            document.querySelectorAll('[data-thoth-buddy]').forEach((element) => {{
                element.dataset.bubbleVerbosity = bubbleVerbosity;
                element.dataset.personality = personality;
            }});
            const api = window.pywebview && window.pywebview.api ? window.pywebview.api : null;
            if (api) {{
                if ({json.dumps(desktop_enabled)} && api.open_buddy_window) {{
                    Promise.resolve(api.open_buddy_window(Number(window.location.port || 8080), 260, 260)).then((ok) => {{
                        if (!ok) notify('Desktop overlay is available only in the native window', 'warning');
                    }}).catch((error) => notify('Desktop overlay could not open: ' + (error && error.message ? error.message : String(error || 'unknown error')), 'negative'));
                }} else if (api.close_buddy_window) {{
                    Promise.resolve(api.close_buddy_window(false)).catch(() => {{}});
                }}
            }} else if ({json.dumps(desktop_enabled)}) {{
                notify('Desktop overlay needs the pywebview native window', 'warning');
            }}
        }})();
        """
    )
    build_in_app_buddy()
    _install_desktop_overlay_focus_sync(cfg)


def _install_desktop_overlay_focus_sync(cfg: dict) -> None:
    enabled = bool(cfg.get("enabled", True)) and bool(cfg.get("desktop_enabled", False))
    ui.run_javascript(
        f"""
        (() => {{
            const api = window.pywebview && window.pywebview.api ? window.pywebview.api : null;
            window.__THOTH_BUDDY_DESKTOP_ENABLED = {json.dumps(enabled)};
            if (!api) return;
            const port = Number(window.location.port || 8080);
            const hideForFocus = () => {{
                if (!window.__THOTH_BUDDY_DESKTOP_ENABLED || !api.hide_buddy_window) return;
                Promise.resolve(api.hide_buddy_window(false)).catch(() => {{}});
            }};
            const showForBlur = () => {{
                if (!window.__THOTH_BUDDY_DESKTOP_ENABLED || !api.show_buddy_window) return;
                Promise.resolve(api.show_buddy_window(false, port, 260, 260)).catch(() => {{}});
            }};
            if (!window.__THOTH_BUDDY_DESKTOP_FOCUS_SYNC) {{
                window.__THOTH_BUDDY_DESKTOP_FOCUS_SYNC = true;
                window.addEventListener('focus', hideForFocus);
                window.addEventListener('blur', showForBlur);
                document.addEventListener('visibilitychange', () => {{
                    if (document.visibilityState === 'visible') hideForFocus();
                    else showForBlur();
                }});
            }}
            if (window.__THOTH_BUDDY_DESKTOP_ENABLED && document.hasFocus()) hideForFocus();
        }})();
        """
    )


def build_sidebar_buddy(state, p, open_settings=None) -> None:
    cfg = get_buddy_config()
    visible = bool(cfg.get("enabled", True)) and bool(cfg.get("sidebar_enabled", True))
    dock_id = f"buddy-sidebar-dock-{uuid.uuid4().hex[:10]}"
    in_app_id = f"buddy-in-app-shell-{uuid.uuid4().hex[:10]}"

    def _handle_click() -> None:
        _emit_buddy_hi()
        if open_settings:
            open_settings("Buddy")

    with ui.column().classes("w-full items-center gap-1 q-mb-sm"):
        with ui.element("div").classes("thoth-buddy-sidebar-action") as shell:
            shell._props["id"] = dock_id
            shell._props["data-buddy-sidebar-shell"] = "1"
            if not visible:
                shell.style("display: none;")
            with ui.element("div").classes("thoth-buddy-in-app thoth-buddy-docked") as buddy_shell:
                buddy_shell._props["id"] = in_app_id
                buddy_shell._props["data-buddy-in-app-shell"] = "1"
                buddy_shell._props["role"] = "button"
                buddy_shell._props["tabindex"] = "0"
                buddy_shell.on("buddy-click", _handle_click)
                p.sidebar_avatar = build_buddy_surface("sidebar")
    ui.run_javascript(f"setTimeout(() => {{ {_install_in_app_buddy_drag_js(in_app_id, dock_id)} }}, 100);")


def build_in_app_buddy(*, recreate: bool = False) -> None:
    cfg = get_buddy_config()
    _install_desktop_overlay_focus_sync(cfg)
    ui.run_javascript(
        """
        (() => {
            document.querySelectorAll('[data-buddy-in-app-shell]').forEach((element) => {
                const dock = element.dataset.buddyDockId || (element.closest('[data-buddy-sidebar-shell]') || {}).id || '';
                if (dock && window.ThothBuddyDock) window.ThothBuddyDock.install(element.id, dock);
            });
            window.ThothBuddy && window.ThothBuddy.initAll();
        })();
        """
    )


def build_floating_buddy(*, recreate: bool = False) -> None:
    build_in_app_buddy(recreate=recreate)


def _install_in_app_buddy_drag_js(element_id: str, dock_id: str) -> str:
    return f"""
        const element = document.getElementById({json.dumps(element_id)});
        const dock = document.getElementById({json.dumps(dock_id)});
        if (!window.ThothBuddyDock) {{
            window.ThothBuddyDock = {{
                resetAll() {{
                    document.querySelectorAll('[data-buddy-in-app-shell]').forEach((node) => {{
                        if (node.__thothBuddyDockHome) node.__thothBuddyDockHome();
                    }});
                }},
                install(elementId, dockId) {{
                    const target = document.getElementById(elementId);
                    const targetDock = document.getElementById(dockId);
                    if (!target || !targetDock || target.dataset.buddyDragInstalled === '1') return;
                    target.dataset.buddyDockId = dockId;
                    target.dataset.buddyDragInstalled = '1';
                    const root = () => target.querySelector('[data-thoth-buddy]');
                    const setSurface = (surface) => {{
                        const buddyRoot = root();
                        if (buddyRoot) buddyRoot.dataset.surface = surface;
                    }};
                    const dockHome = () => {{
                        target.classList.add('thoth-buddy-docked');
                        target.classList.remove('thoth-buddy-undocked', 'thoth-buddy-dragging');
                        target.style.left = '';
                        target.style.top = '';
                        target.style.right = '';
                        target.style.bottom = '';
                        targetDock.classList.remove('thoth-buddy-dock-empty', 'thoth-buddy-dock-hover');
                        if (target.parentElement !== targetDock) targetDock.appendChild(target);
                        setSurface('sidebar');
                    }};
                    const undock = (rect) => {{
                        target.style.left = rect.left + 'px';
                        target.style.top = rect.top + 'px';
                        target.style.right = 'auto';
                        target.style.bottom = 'auto';
                        target.classList.add('thoth-buddy-undocked');
                        target.classList.remove('thoth-buddy-docked');
                        targetDock.classList.add('thoth-buddy-dock-empty');
                        if (target.parentElement !== document.body) document.body.appendChild(target);
                        setSurface('floating');
                    }};
                    target.__thothBuddyDockHome = dockHome;
                    let drag = null;
                    let moved = false;
                    const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
                    const isNearDock = () => {{
                        const buddyRect = target.getBoundingClientRect();
                        const dockRect = targetDock.getBoundingClientRect();
                        const buddyX = buddyRect.left + buddyRect.width / 2;
                        const buddyY = buddyRect.top + buddyRect.height / 2;
                        const dockX = dockRect.left + dockRect.width / 2;
                        const dockY = dockRect.top + dockRect.height / 2;
                        const distance = Math.hypot(buddyX - dockX, buddyY - dockY);
                        return distance < Math.max(92, dockRect.width * 0.58);
                    }};
                    const updateDockHover = () => {{
                        if (!target.classList.contains('thoth-buddy-undocked')) return;
                        targetDock.classList.toggle('thoth-buddy-dock-hover', isNearDock());
                    }};
                    target.addEventListener('pointerdown', (event) => {{
                        if (event.button !== 0) return;
                        const rect = target.getBoundingClientRect();
                        drag = {{ pointerId: event.pointerId, startX: event.clientX, startY: event.clientY, offsetX: event.clientX - rect.left, offsetY: event.clientY - rect.top }};
                        moved = false;
                        target.classList.add('thoth-buddy-dragging');
                        try {{ target.setPointerCapture(event.pointerId); }} catch (error) {{}}
                        event.preventDefault();
                    }});
                    target.addEventListener('pointermove', (event) => {{
                        if (!drag || drag.pointerId !== event.pointerId) return;
                        const rect = target.getBoundingClientRect();
                        if (Math.abs(event.clientX - drag.startX) > 4 || Math.abs(event.clientY - drag.startY) > 4) {{
                            moved = true;
                            if (!target.classList.contains('thoth-buddy-undocked')) undock(rect);
                        }}
                        if (!moved) return;
                        const width = target.offsetWidth;
                        const height = target.offsetHeight;
                        target.style.left = clamp(event.clientX - drag.offsetX, 8, window.innerWidth - width - 8) + 'px';
                        target.style.top = clamp(event.clientY - drag.offsetY, 8, window.innerHeight - height - 8) + 'px';
                        target.style.right = 'auto';
                        target.style.bottom = 'auto';
                        updateDockHover();
                    }});
                    const finish = (event) => {{
                        if (!drag || drag.pointerId !== event.pointerId) return;
                        const wasClick = !moved;
                        drag = null;
                        target.classList.remove('thoth-buddy-dragging');
                        if (!wasClick && target.classList.contains('thoth-buddy-undocked') && isNearDock()) dockHome();
                        else targetDock.classList.remove('thoth-buddy-dock-hover');
                        if (wasClick) target.dispatchEvent(new CustomEvent('buddy-click', {{ bubbles: true }}));
                    }};
                    target.addEventListener('pointerup', finish);
                    target.addEventListener('pointercancel', finish);
                    target.addEventListener('keydown', (event) => {{
                        if (event.key !== 'Enter' && event.key !== ' ') return;
                        event.preventDefault();
                        target.dispatchEvent(new CustomEvent('buddy-click', {{ bubbles: true }}));
                    }});
                }},
            }};
        }}
        if (!element || element.dataset.buddyDragInstalled === '1') return;
        if (dock) window.ThothBuddyDock.install({json.dumps(element_id)}, {json.dumps(dock_id)});
    """


def build_buddy_overlay_page() -> None:
    inject_buddy_head()
    ui.run_javascript(
        """
        (() => {
            document.documentElement.style.overflow = 'hidden';
            document.documentElement.classList.add('thoth-buddy-overlay-html');
            document.documentElement.style.background = 'transparent';
            document.body.style.overflow = 'hidden';
            document.body.style.margin = '0';
            document.body.style.background = 'transparent';
            document.body.classList.add('thoth-buddy-overlay-body');
            const startedAt = performance.now();
            let attempts = 0;
            const revealOverlay = () => {
                const api = window.pywebview && window.pywebview.api ? window.pywebview.api : null;
                const elapsed = performance.now() - startedAt;
                const root = document.querySelector('.thoth-buddy-overlay-page [data-thoth-buddy]');
                const rootReady = root && (root.classList.contains('buddy-ready') || root.classList.contains('buddy-generated'));
                if (api && root && (rootReady || elapsed >= 700)) {
                    if (api.mark_buddy_window_ready) {
                        Promise.resolve(api.mark_buddy_window_ready()).catch(() => {});
                    } else if (api.show_buddy_window) {
                        Promise.resolve(api.show_buddy_window(false)).catch(() => {});
                    }
                    return;
                }
                attempts += 1;
                if (attempts < 40) setTimeout(revealOverlay, 50);
            };
            requestAnimationFrame(() => requestAnimationFrame(revealOverlay));
        })();
        """,
        timeout=1,
    )
    emit_buddy_event(BuddyEventType.APP_READY, source="buddy.overlay", payload={"label": "Overlay ready"})
    with ui.element("div").classes("thoth-buddy-overlay-page"):
        build_buddy_surface("desktop")


def build_buddy_settings_tab(_reopen=None) -> None:
    from identity import sanitize_personality, _PERSONALITY_MAX_LEN

    cfg = get_buddy_config()
    packs = list_buddy_packs()
    packs_by_id = {pack.id: pack for pack in packs}
    selected_pack_id = {"value": str(cfg.get("pack_id") or "glyph")}
    pack_selection_touched = {"value": False}
    if selected_pack_id["value"] not in packs_by_id and packs:
        selected_pack_id["value"] = packs[0].id

    def _display_pack_name(name: str | None) -> str:
        value = str(name or "").strip()
        if value.lower().startswith("buddy "):
            value = value[6:].strip()
        return value or "Default"

    active_pack = packs_by_id.get(selected_pack_id["value"]) or load_buddy_pack(selected_pack_id["value"])
    active_pack_label = _display_pack_name(active_pack.name)
    motion_count = len(active_pack.motion_clips or {})
    pack_status_text = "Motion pack ready" if active_pack.status == "available" else (active_pack.message or "Pack needs attention")
    overlay_available = "Native pywebview only"

    def _section(title: str, icon: str, subtitle: str):
        with ui.column().classes("w-full gap-2 q-pa-sm rounded-borders q-mb-sm").style(
            "border: 1px solid rgba(148, 163, 184, 0.18); "
            "background: rgba(15, 23, 42, 0.10);"
        ):
            with ui.row().classes("items-center justify-between w-full no-wrap"):
                with ui.row().classes("items-center gap-2 no-wrap"):
                    ui.icon(icon, size="sm").classes("text-primary")
                    with ui.column().classes("gap-0"):
                        ui.label(title).classes("text-subtitle2")
                        ui.label(subtitle).classes("text-grey-6 text-xs")
                header_actions = ui.row().classes("items-center gap-1 no-wrap")
            body = ui.column().classes("w-full gap-2")
        return header_actions, body

    def _surface_switch_row(icon: str, title: str, subtitle: str, label: str, value: bool, *, divider: bool = True):
        border = "border-bottom: 1px solid rgba(148, 163, 184, 0.10);" if divider else ""
        with ui.row().classes("items-center gap-2 no-wrap w-full q-py-xs").style(border):
            ui.icon(icon, size="sm").classes("text-grey-6")
            with ui.column().classes("gap-0").style("min-width: 0; flex: 1;"):
                ui.label(title).classes("text-sm text-weight-medium")
                ui.label(subtitle).classes("text-grey-6 text-xs")
            return ui.switch(label, value=value).props("dense")

    async def _desktop_call(open_requested: bool) -> None:
        script = f"""
        (async () => {{
            const api = window.pywebview && window.pywebview.api ? window.pywebview.api : null;
            if (!api) return 'unavailable';
            try {{
                if ({json.dumps(open_requested)}) {{
                    if (!api.open_buddy_window) return 'unavailable';
                    return await api.open_buddy_window(Number(window.location.port || 8080), 260, 260) ? 'opened' : 'failed';
                }}
                if (!api.close_buddy_window) return 'unavailable';
                return await api.close_buddy_window(true) ? 'closed' : 'not-open';
            }} catch (error) {{
                return 'failed: ' + (error && error.message ? error.message : String(error || 'unknown error'));
            }}
        }})()
        """
        result = await ui.run_javascript(script, timeout=5)
        if result == "opened":
            ui.notify("Desktop overlay opened", type="positive")
        elif result == "closed":
            ui.notify("Desktop overlay closed", type="info")
        elif result == "not-open":
            ui.notify("Desktop overlay was not open", type="info")
        elif result == "unavailable":
            ui.notify("Desktop overlay needs the pywebview native window", type="warning")
        elif isinstance(result, str) and result.startswith("failed: "):
            ui.notify(f"Desktop overlay could not be opened: {result.removeprefix('failed: ')}", type="negative")
        else:
            ui.notify("Desktop overlay could not be opened", type="negative")

    async def _open_desktop_overlay() -> None:
        await _desktop_call(True)

    async def _close_desktop_overlay() -> None:
        await _desktop_call(False)

    buddy_enabled = bool(cfg.get("enabled", True))
    in_app_initial = buddy_enabled and (bool(cfg.get("sidebar_enabled", True)) or bool(cfg.get("floating_enabled", False)))
    desktop_initial = buddy_enabled and bool(cfg.get("desktop_enabled", False))
    has_visible_buddy = in_app_initial or desktop_initial

    ui.label("Buddy").classes("text-h6 q-mb-xs")
    with ui.row().classes("items-center justify-between w-full q-mb-sm"):
        ui.label("Companion behavior, look, and generated motion.").classes("text-grey-6 text-sm")
        with ui.row().classes("items-center gap-1"):
            ui.badge("enabled" if has_visible_buddy else "paused", color="green" if has_visible_buddy else "grey").props("outline dense")
            ui.badge(pack_status_text, color="cyan" if active_pack.status == "available" else "orange").props("outline dense")

    presence_actions, presence_body = _section("Where it appears", "widgets", "Choose the in-app surface and optional desktop overlay.")
    with presence_actions:
        ui.badge(overlay_available, color="blue-grey").props("outline dense")
    with presence_body:
        in_app = _surface_switch_row(
            "radio_button_checked",
            "In app",
            "Starts in the sidebar dock and can be dragged into the workspace.",
            "In app",
            in_app_initial,
        )
        with ui.row().classes("items-center gap-2 no-wrap w-full q-py-xs"):
            ui.icon("picture_in_picture_alt", size="sm").classes("text-grey-6")
            with ui.column().classes("gap-0").style("min-width: 0; flex: 1;"):
                ui.label("Desktop overlay").classes("text-sm text-weight-medium")
                ui.label("A separate transparent pywebview window.").classes("text-grey-6 text-xs")
            desktop = ui.switch("Desktop overlay", value=desktop_initial).props("dense")
            ui.button(icon="open_in_new", on_click=_open_desktop_overlay).props("flat dense round size=sm color=primary").tooltip("Open overlay now")
            ui.button(icon="close", on_click=_close_desktop_overlay).props("flat dense round size=sm").tooltip("Close overlay")

    behavior_actions, behavior_body = _section("Behavior", "tune", "Bubble tone and runtime personality for status text.")
    with behavior_actions:
        ui.badge("runtime", color="blue-grey").props("outline dense")
    with behavior_body:
        with ui.row().classes("items-end gap-2 w-full"):
            buddy_personality = ui.select(
                _PERSONALITY_OPTIONS,
                value=str(cfg.get("personality") or "warm_mystical"),
                label="Companion personality",
            ).classes("col-grow").props("dense outlined")
            buddy_bubbles = ui.select(
                _BUBBLE_OPTIONS,
                value=str(cfg.get("bubble_verbosity") or "normal"),
                label="Bubble style",
            ).classes("min-w-[180px]").props("dense outlined")
        ui.label("Quiet hides bubbles, Normal mirrors the current state, Chatty rewrites short state labels in the selected personality.").classes("text-grey-6 text-xs")

    look_actions, look_body = _section("Look & Motion", "auto_awesome", "Select the active pack used everywhere.")
    with look_actions:
        selected_clip_badge = ui.badge(f"{motion_count} clips", color="cyan" if motion_count else "grey").props("outline dense")
    with look_body:
        pack_cards = {}
        selected_pack_summary = ui.label(f"Selected: {active_pack_label}. {pack_status_text}.").classes("text-grey-6 text-xs")

        def _pack_preview_url(pack) -> str:
            return static_url_for_path(pack.preview_path) if pack.preview_path and pack.preview_path.exists() else ""

        def _selected_generated_pack_preview(latest_cfg: dict | None = None) -> tuple[str, str]:
            current_cfg = latest_cfg or get_buddy_config()
            pack_id = str(selected_pack_id.get("value") or current_cfg.get("pack_id") or "")
            if pack_id.startswith("hatch-"):
                try:
                    pack = load_buddy_pack(pack_id)
                    if pack.runtime in {"generated_motion_pack", "generated_still"} and pack.preview_path.exists():
                        return pack_id, str(pack.preview_path)
                except Exception:
                    pass
            preview_path = str(current_cfg.get("latest_hatch_preview") or current_cfg.get("active_hatch_preview") or "")
            if preview_path and pathlib.Path(preview_path).expanduser().exists():
                return pathlib.Path(preview_path).expanduser().resolve().parent.name, preview_path
            return pack_id, ""

        def _select_pack(pack_id: str) -> None:
            pack = packs_by_id.get(pack_id)
            if not pack or pack.status != "available":
                ui.notify(pack.message if pack else "Pack is unavailable", type="warning")
                return
            pack_label = _display_pack_name(pack.name)
            selected_pack_id["value"] = pack_id
            pack_selection_touched["value"] = True
            for current_id, card in pack_cards.items():
                if current_id == pack_id:
                    card.classes(add="thoth-buddy-pack-card-selected")
                else:
                    card.classes(remove="thoth-buddy-pack-card-selected")
            selected_status = "Motion pack ready" if pack.status == "available" else (pack.message or "Pack needs attention")
            selected_pack_summary.set_text(f"Selected: {pack_label}. {selected_status}.")
            selected_motion_count = len(pack.motion_clips or {})
            selected_clip_badge.set_text(f"{selected_motion_count} clips")
            latest_cfg = get_buddy_config()
            latest_cfg["pack_id"] = pack_id
            _clear_hatch_media_overrides(latest_cfg)
            save_buddy_config(latest_cfg)
            emit_buddy_event(BuddyEventType.APP_READY, source="buddy.settings", payload={"label": f"{pack_label} selected"})
            _refresh_existing_buddy_surfaces()
            _apply_buddy_surface_settings(latest_cfg)
            ui.notify(f"{pack_label} selected", type="positive")

        def _delete_selected_generated_pack() -> None:
            pack_id = str(selected_pack_id.get("value") or get_buddy_config().get("pack_id") or "")
            try:
                pack = load_buddy_pack(pack_id)
            except Exception:
                pack = None
            if not pack_id.startswith("hatch-") or not pack or pack.runtime not in {"generated_motion_pack", "generated_still"}:
                ui.notify("Delete is available for generated Hatch looks", type="warning")
                return
            pack_label = _display_pack_name(pack.name)

            def _confirm_delete() -> None:
                try:
                    deleted_pack_id = delete_generated_buddy_pack(pack_id)
                except Exception as exc:
                    ui.notify(str(exc), type="negative")
                    return
                remaining_packs = [candidate for candidate in list_buddy_packs() if candidate.id != deleted_pack_id and candidate.status == "available"]
                fallback_pack_id = "glyph" if any(candidate.id == "glyph" for candidate in remaining_packs) else (remaining_packs[0].id if remaining_packs else "glyph")
                latest_cfg = get_buddy_config()
                latest_cfg["pack_id"] = fallback_pack_id
                _clear_hatch_media_overrides(latest_cfg)
                save_buddy_config(latest_cfg)
                selected_pack_id["value"] = fallback_pack_id
                hatch_motion.set_content("")
                hatch_status.set_text(f"Deleted generated Buddy look {pack_label}")
                emit_buddy_event(BuddyEventType.APP_READY, source="buddy.settings", payload={"label": "Generated Buddy look deleted"})
                _refresh_existing_buddy_surfaces()
                _apply_buddy_surface_settings(latest_cfg)
                ui.notify(f"Deleted {pack_label}", type="positive")
                if _reopen:
                    _reopen("Buddy")

            confirm_destructive(
                f"Delete {pack_label}?",
                "This removes the generated look from the Buddy picker. Bundled looks are not affected.",
                confirm_label="Delete generated look",
                on_confirm=_confirm_delete,
            )

        with ui.row().classes("items-center justify-between w-full"):
            ui.label("Look").classes("text-sm text-weight-medium")
            with ui.row().classes("items-center gap-1 no-wrap"):
                ui.button(icon="delete", on_click=_delete_selected_generated_pack).props("flat dense round size=sm color=negative").tooltip("Delete generated look")
                ui.button(icon="refresh", on_click=lambda: _reopen("Buddy") if _reopen else None).props("flat dense round size=sm").tooltip("Refresh packs")
        with ui.element("div").classes("thoth-buddy-pack-grid"):
            for pack in packs:
                is_selected = pack.id == selected_pack_id["value"]
                preview_url = _pack_preview_url(pack)
                status_text = "Ready" if pack.status == "available" else (pack.message or "Needs attention")
                pack_label = _display_pack_name(pack.name)
                with ui.button(on_click=lambda pack_id=pack.id: _select_pack(pack_id)).props("flat no-caps padding=none").classes(
                    "thoth-buddy-pack-card" + (" thoth-buddy-pack-card-selected" if is_selected else "")
                ) as pack_card:
                    pack_cards[pack.id] = pack_card
                    if preview_url:
                        ui.image(preview_url).classes("thoth-buddy-pack-preview")
                    else:
                        with ui.element("div").classes("thoth-buddy-pack-preview thoth-buddy-pack-preview-empty"):
                            ui.icon("auto_awesome", size="md")
                    with ui.column().classes("gap-0 w-full"):
                        ui.label(pack_label).classes("thoth-buddy-pack-title")
                        ui.label(f"{len(pack.motion_clips or {})} clips · {status_text}").classes("thoth-buddy-pack-meta")
        if not packs:
            ui.label("No packs found. Generate one or refresh after installing assets.").classes("text-grey-6 text-xs")

    generation_actions, generation_body = _section("Generate Look", "auto_fix_high", "Concept and generation guidance used when creating the full motion pack.")
    with generation_actions:
        ui.badge("prompt-generated", color="purple").props("outline dense")
    with generation_body:
        prompt = ui.textarea(
            label="Concept",
            value=str(cfg.get("hatch_prompt", "A cute tiny mystical coding familiar for Thoth")),
        ).classes("w-full").props("outlined autogrow")
        buddy_description = ui.textarea(
            label="Generation guidance and personality notes",
            value=str(cfg.get("personality_description") or ""),
        ).props(f"maxlength={_PERSONALITY_MAX_LEN} counter outlined autogrow").classes("w-full")
        hatch_status = ui.label("").classes("text-grey-6 text-xs")
        with ui.row().classes("items-start gap-3 w-full"):
            hatch_preview = ui.image().classes("w-40 rounded-borders").style("display: none;")
            hatch_motion = ui.html("", sanitize=False).classes("q-mt-xs")

    def _save() -> None:
        buddy_notes = sanitize_personality(str(buddy_description.value or ""))
        in_app_enabled = bool(in_app.value)
        desktop_enabled = bool(desktop.value)
        latest_cfg = get_buddy_config()
        latest_cfg.update({
            "enabled": in_app_enabled or desktop_enabled,
            "sidebar_enabled": in_app_enabled,
            "floating_enabled": False,
            "desktop_enabled": desktop_enabled,
            "pack_id": str(selected_pack_id.get("value") or "glyph"),
            "personality": str(buddy_personality.value or "warm_mystical"),
            "personality_description": buddy_notes,
            "bubble_verbosity": str(buddy_bubbles.value or "normal"),
            "hatch_prompt": str(prompt.value or ""),
            "hatch_generation_prompt": _compose_hatch_prompt(str(prompt.value or ""), str(buddy_personality.value or "warm_mystical"), buddy_notes),
        })
        if pack_selection_touched["value"]:
            _clear_hatch_media_overrides(latest_cfg)
        save_buddy_config(latest_cfg)
        cfg.clear()
        cfg.update(latest_cfg)
        if buddy_notes != str(buddy_description.value or ""):
            buddy_description.set_value(buddy_notes)
            ui.notify("Some companion personality text was removed", type="warning")
            return
        emit_buddy_event(BuddyEventType.APP_READY, source="buddy.settings", payload={"label": "Buddy settings saved"})
        _refresh_existing_buddy_surfaces()
        _apply_buddy_surface_settings(latest_cfg)
        ui.notify("Buddy settings saved", type="positive")
        if _reopen:
            _reopen("Buddy")

    async def _hatch() -> None:
        buddy_notes = sanitize_personality(str(buddy_description.value or ""))
        if buddy_notes != str(buddy_description.value or ""):
            buddy_description.set_value(buddy_notes)
            ui.notify("Some companion personality text was removed", type="warning")
            return
        concept_prompt = str(prompt.value or "")
        composed_prompt = _compose_hatch_prompt(concept_prompt, str(buddy_personality.value or "warm_mystical"), buddy_notes)
        hatch_status.set_text("Generating Buddy art and motion pack...")
        hatch_button.props(add="loading")
        try:
            draft = await run.io_bound(
                generate_hatch_buddy,
                composed_prompt,
                pack_id=str(selected_pack_id.get("value") or "glyph"),
            )
            latest_cfg = get_buddy_config()
            latest_cfg.update({
                "hatch_prompt": concept_prompt,
                "hatch_generation_prompt": composed_prompt,
                "personality": str(buddy_personality.value or "warm_mystical"),
                "personality_description": buddy_notes,
                "bubble_verbosity": str(buddy_bubbles.value or "normal"),
                "pack_id": str(draft.pack_id or selected_pack_id.get("value") or "glyph"),
            })
            selected_pack_id["value"] = str(draft.pack_id or selected_pack_id.get("value") or "glyph")
            save_buddy_config(latest_cfg)
            preview_path = str(draft.preview_path or "")
            if preview_path:
                with open(preview_path, "rb") as preview_file:
                    preview_b64 = base64.b64encode(preview_file.read()).decode("ascii")
                hatch_preview.set_source(f"data:image/png;base64,{preview_b64}")
                hatch_preview.style("display: block;")
            if draft.motion_clips:
                labels = {spec.id: spec.label for spec in motion_clip_specs()}
                videos = []
                for clip_id, clip_path in draft.motion_clips.items():
                    motion_url = static_url_for_path(clip_path)
                    label = html.escape(labels.get(clip_id, clip_id.title()))
                    videos.append(
                        '<div style="display:flex;flex-direction:column;gap:4px;width:118px;">'
                        f'<video src="{html.escape(motion_url)}" autoplay loop muted playsinline controls '
                        'style="width:118px;height:118px;object-fit:cover;border-radius:8px;background:#070a0e;"></video>'
                        f'<span style="font-size:10px;color:#8f9baa;text-align:center;">{label}</span>'
                        '</div>'
                    )
                hatch_motion.set_content(
                    '<div style="display:flex;flex-wrap:wrap;gap:8px;max-width:390px;">'
                    + "".join(videos)
                    + '</div>'
                )
            elif draft.active_motion_path:
                motion_url = static_url_for_path(draft.active_motion_path)
                hatch_motion.set_content(
                    f'<video src="{html.escape(motion_url)}" autoplay loop muted playsinline controls '
                    'style="width:160px;max-width:100%;border-radius:8px;background:#070a0e;"></video>'
                )
            if draft.status == "motion_pack_generated":
                hatch_status.set_text(f"Generated live Buddy art and motion pack {draft.id}")
                emit_buddy_event(BuddyEventType.NOTIFICATION, source="buddy.hatch", payload={"label": "Buddy motion pack generated"})
                ui.notify("Buddy motion pack generated", type="positive")
            else:
                hatch_status.set_text(f"Generated companion art, but motion pack failed for {draft.id}")
                emit_buddy_event(BuddyEventType.NOTIFICATION, source="buddy.hatch", payload={"label": "Buddy art generated"})
                ui.notify("Buddy art generated; motion pack needs video provider setup", type="warning")
        except Exception as exc:
            draft = create_hatch_draft(composed_prompt, pack_id=str(selected_pack_id.get("value") or "glyph"))
            latest_cfg = get_buddy_config()
            latest_cfg["hatch_prompt"] = concept_prompt
            latest_cfg["hatch_generation_prompt"] = composed_prompt
            save_buddy_config(latest_cfg)
            hatch_status.set_text(f"Buddy art generation failed. Saved draft {draft.id}")
            ui.notify(str(exc), type="negative")
        finally:
            hatch_button.props(remove="loading")

    async def _retry_motion() -> None:
        buddy_notes = sanitize_personality(str(buddy_description.value or ""))
        if buddy_notes != str(buddy_description.value or ""):
            buddy_description.set_value(buddy_notes)
            ui.notify("Some companion personality text was removed", type="warning")
            return
        latest_cfg = get_buddy_config()
        target_pack_id, preview_path = _selected_generated_pack_preview(latest_cfg)
        if not preview_path or not pathlib.Path(preview_path).expanduser().exists():
            ui.notify("Select or generate a Hatch look before retrying motion", type="warning")
            return
        concept_prompt = str(prompt.value or latest_cfg.get("hatch_prompt") or "")
        composed_prompt = str(latest_cfg.get("hatch_generation_prompt") or "") or _compose_hatch_prompt(
            concept_prompt,
            str(buddy_personality.value or "warm_mystical"),
            buddy_notes,
        )
        hatch_status.set_text("Generating motion pack for current Buddy art...")
        retry_motion_button.props(add="loading")
        try:
            draft = await run.io_bound(
                generate_hatch_motion_pack,
                composed_prompt,
                preview_path,
                pack_id=target_pack_id,
                reuse_existing=False,
            )
            labels = {spec.id: spec.label for spec in motion_clip_specs()}
            videos = []
            for clip_id, clip_path in draft.motion_clips.items():
                motion_url = static_url_for_path(clip_path)
                label = html.escape(labels.get(clip_id, clip_id.title()))
                videos.append(
                    '<div style="display:flex;flex-direction:column;gap:4px;width:118px;">'
                    f'<video src="{html.escape(motion_url)}" autoplay loop muted playsinline controls '
                    'style="width:118px;height:118px;object-fit:cover;border-radius:8px;background:#070a0e;"></video>'
                    f'<span style="font-size:10px;color:#8f9baa;text-align:center;">{label}</span>'
                    '</div>'
                )
            if videos:
                hatch_motion.set_content(
                    '<div style="display:flex;flex-wrap:wrap;gap:8px;max-width:390px;">'
                    + "".join(videos)
                    + '</div>'
                )
            latest_cfg = get_buddy_config()
            latest_cfg.update({
                "hatch_prompt": concept_prompt,
                "hatch_generation_prompt": composed_prompt,
                "personality": str(buddy_personality.value or "warm_mystical"),
                "personality_description": buddy_notes,
                "bubble_verbosity": str(buddy_bubbles.value or "normal"),
                "pack_id": str(draft.pack_id or latest_cfg.get("pack_id") or "glyph"),
            })
            selected_pack_id["value"] = str(draft.pack_id or latest_cfg.get("pack_id") or "glyph")
            save_buddy_config(latest_cfg)
            hatch_status.set_text(f"Generated motion pack for current Buddy art {draft.id}")
            emit_buddy_event(BuddyEventType.NOTIFICATION, source="buddy.hatch", payload={"label": "Buddy motion pack generated"})
            _refresh_existing_buddy_surfaces()
            _apply_buddy_surface_settings(latest_cfg)
            ui.notify("Buddy motion pack generated", type="positive")
        except Exception as exc:
            hatch_status.set_text("Buddy motion generation failed")
            ui.notify(str(exc), type="negative")
        finally:
            retry_motion_button.props(remove="loading")

    def _use_still_only() -> None:
        latest_cfg = get_buddy_config()
        pack_id = str(latest_cfg.get("pack_id") or selected_pack_id.get("value") or "")
        preview_path = str(latest_cfg.get("latest_hatch_preview") or latest_cfg.get("active_hatch_preview") or "")
        if not preview_path:
            try:
                pack = load_buddy_pack(pack_id)
                if pack.id.startswith("hatch-") and pack.preview_path.exists():
                    preview_path = str(pack.preview_path)
            except Exception:
                preview_path = ""
        if not pack_id.startswith("hatch-") or not preview_path:
            ui.notify("Still-only mode is available for generated Hatch looks", type="warning")
            return
        try:
            still_pack_id = use_hatch_still_only(
                pack_id,
                preview_path,
                prompt=str(latest_cfg.get("hatch_generation_prompt") or latest_cfg.get("hatch_prompt") or prompt.value or ""),
            )
        except Exception as exc:
            ui.notify(str(exc), type="negative")
            return
        latest_cfg["pack_id"] = still_pack_id
        for key in (
            "active_hatch_motion",
            "active_hatch_motion_pack",
            "active_hatch_motion_clips",
            "latest_hatch_motion",
            "latest_hatch_motion_pack",
            "latest_hatch_motion_error",
        ):
            latest_cfg.pop(key, None)
        save_buddy_config(latest_cfg)
        selected_pack_id["value"] = still_pack_id
        hatch_motion.set_content("")
        hatch_status.set_text("Using current Buddy art as a still image")
        _refresh_existing_buddy_surfaces()
        _apply_buddy_surface_settings(latest_cfg)
        ui.notify("Using still image only", type="positive")

    with ui.row().classes("items-center justify-end gap-2 w-full q-mt-md"):
        ui.button("Save", icon="save", on_click=_save).props("unelevated no-caps color=primary")
        ui.button("Use still only", icon="image", on_click=_use_still_only).props("outline no-caps")
        retry_motion_button = ui.button("Retry motion", icon="movie", on_click=_retry_motion).props("outline no-caps")
        hatch_button = ui.button("Generate full Buddy", icon="auto_fix_high", on_click=_hatch).props("outline no-caps")


def set_floating_enabled(enabled: bool) -> dict:
    return set_buddy_config("floating_enabled", bool(enabled))
