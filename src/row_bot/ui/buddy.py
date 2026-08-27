"""NiceGUI surfaces for Buddy."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import pathlib
import time
import uuid

from row_bot.brand import APP_DISPLAY_NAME
from nicegui import run, ui

from row_bot.buddy.assets import delete_generated_buddy_pack, list_buddy_packs, load_buddy_pack, static_url_for_path
from row_bot.buddy.brain import get_buddy_snapshot
from row_bot.buddy.config import (
    get_buddy_config,
    get_buddy_placement_state,
    save_buddy_config,
    set_buddy_config,
)
from row_bot.buddy.events import BuddyEventType, emit_buddy_event
from row_bot.buddy.overlay import (
    BuddyPlacement,
    OverlayTurnTarget,
    build_thread_snapshot,
)
from row_bot.buddy.hatch import (
    activate_hatch_art,
    activate_hatch_motion,
    activate_hatch_motion_pack,
    get_hatch_generation_status,
    mark_hatch_generation_status_seen,
    start_hatch_generation_job,
    use_hatch_still_only,
)
from row_bot.ui.confirm import confirm_destructive

logger = logging.getLogger(__name__)

_BUDDY_HEAD = """
<script>
(() => {
    if (window.location && window.location.pathname === '/buddy-overlay') {
        document.documentElement.classList.add('row-bot-buddy-overlay-html');
        document.documentElement.style.background = 'transparent';
        document.documentElement.style.backgroundColor = 'transparent';
    }
})();
</script>
<script src="/static/buddy/runtime/buddy.js?v=row-bot-buddy-v7"></script>
<style>
html.row-bot-buddy-overlay-html,
html.row-bot-buddy-overlay-html body,
html.row-bot-buddy-overlay-html #app,
html.row-bot-buddy-overlay-html .nicegui-layout,
html.row-bot-buddy-overlay-html .q-layout,
html.row-bot-buddy-overlay-html .q-page-container,
html.row-bot-buddy-overlay-html .q-page,
html.row-bot-buddy-overlay-html .nicegui-content {
    background: transparent !important;
    background-color: transparent !important;
}
html.row-bot-buddy-overlay-html,
html.row-bot-buddy-overlay-html body {
    margin: 0 !important;
    overflow: hidden !important;
}
.row-bot-buddy-wrap {
  --buddy-energy: 60;
  --buddy-focus: 20;
  --buddy-alert: 0;
  width: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
}
.row-bot-buddy-stage {
  position: relative;
    width: 132px;
    height: 132px;
  border-radius: 8px;
  overflow: hidden;
    background: transparent;
    border: 1px solid transparent;
}
.row-bot-buddy-stage canvas {
  width: 100%;
  height: 100%;
  display: block;
}
.row-bot-buddy-stage::after {
  content: '';
  position: absolute;
  inset: 8px;
  border-radius: 999px;
    border: 1px solid rgba(77, 184, 171, calc(var(--buddy-focus) / 260));
  box-shadow: 0 0 calc(var(--buddy-alert) * 0.18px) rgba(247, 118, 87, 0.55);
    transition: border-color 280ms ease, box-shadow 280ms ease, opacity 280ms ease;
  pointer-events: none;
}
.row-bot-buddy-wrap[data-surface="sidebar"] {
    gap: 8px;
}
.row-bot-buddy-sidebar-action {
    position: relative;
    width: 158px;
    height: 158px;
    display: grid;
    place-items: center;
    border-radius: 999px;
    cursor: default;
    outline: none;
}
.row-bot-buddy-sidebar-action::before {
    content: '';
    position: absolute;
    inset: 5px;
    border-radius: 999px;
    border: 1px dashed rgba(77, 184, 171, 0.28);
    box-shadow: inset 0 0 24px rgba(77, 184, 171, 0.08), 0 0 18px rgba(228, 194, 94, 0.10);
    opacity: 0;
    transform: scale(0.96);
    transition: opacity 180ms ease, transform 180ms ease, border-color 180ms ease;
    pointer-events: none;
}
.row-bot-buddy-sidebar-action.row-bot-buddy-dock-empty::before {
    opacity: 1;
    transform: scale(1);
    animation: row-bot-buddy-sidebar-ring 3.2s ease-in-out infinite;
}
.row-bot-buddy-sidebar-action.row-bot-buddy-dock-hover::before {
    border-color: rgba(228, 194, 94, 0.58);
    box-shadow: inset 0 0 24px rgba(228, 194, 94, 0.08), 0 0 20px rgba(228, 194, 94, 0.22);
}
.row-bot-buddy-sidebar-action:focus-visible .row-bot-buddy-stage {
    box-shadow: 0 0 0 2px rgba(228, 194, 94, 0.54), 0 10px 28px rgba(0, 0, 0, 0.24);
}
.row-bot-buddy-in-app {
    position: relative;
    z-index: 2;
    width: 140px;
    cursor: grab;
    touch-action: none;
    user-select: none;
}
.row-bot-buddy-in-app.row-bot-buddy-dragging {
    cursor: grabbing;
}
.row-bot-buddy-in-app.row-bot-buddy-drag-preview {
    position: fixed;
    right: 18px;
    bottom: 18px;
    width: 158px;
    z-index: 10000;
    pointer-events: auto;
}
.row-bot-buddy-wrap[data-surface="sidebar"] .row-bot-buddy-stage {
    width: 132px;
    height: 132px;
    border-radius: 999px;
    border: 1px solid rgba(77, 184, 171, 0.34);
    box-shadow: 0 0 0 1px rgba(228, 194, 94, 0.10), 0 10px 28px rgba(0, 0, 0, 0.24);
}
.row-bot-buddy-wrap[data-surface="sidebar"] .row-bot-buddy-stage::after {
    inset: 5px;
    border: 1px solid rgba(77, 184, 171, calc(0.20 + var(--buddy-focus) / 260));
    box-shadow:
        0 0 calc(8px + var(--buddy-focus) * 0.12px) rgba(77, 184, 171, 0.24),
        0 0 calc(var(--buddy-alert) * 0.22px) rgba(247, 118, 87, 0.62);
    animation: row-bot-buddy-sidebar-ring 2.8s ease-in-out infinite;
}
.row-bot-buddy-wrap[data-surface="sidebar"][data-animation^="celebrate"] .row-bot-buddy-stage::after,
.row-bot-buddy-wrap[data-surface="sidebar"][data-mood="proud"] .row-bot-buddy-stage::after {
    border-color: rgba(228, 194, 94, 0.58);
    box-shadow: 0 0 22px rgba(228, 194, 94, 0.35);
}
.row-bot-buddy-wrap[data-surface="sidebar"][data-mood="concerned"] .row-bot-buddy-stage::after,
.row-bot-buddy-wrap[data-surface="sidebar"][data-animation="worry"] .row-bot-buddy-stage::after {
    border-color: rgba(247, 118, 87, 0.56);
    box-shadow: 0 0 22px rgba(247, 118, 87, 0.36);
}
.row-bot-buddy-wrap[data-surface="sidebar"][data-animation="tap_glass"] .row-bot-buddy-stage::after,
.row-bot-buddy-wrap[data-surface="sidebar"][data-animation="pause"] .row-bot-buddy-stage::after {
    border-color: rgba(228, 194, 94, 0.46);
    box-shadow:
        0 0 14px rgba(228, 194, 94, 0.20),
        0 0 18px rgba(77, 184, 171, 0.14);
    animation-duration: 4.2s;
}
.row-bot-buddy-pack-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(132px, 1fr));
    gap: 10px;
    width: 100%;
}
.row-bot-buddy-pack-card.q-btn {
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
.row-bot-buddy-pack-card .q-btn__content {
    width: 100%;
    height: 100%;
    display: flex;
    flex-direction: column;
    align-items: stretch;
    justify-content: flex-start;
    gap: 7px;
    flex-wrap: nowrap;
}
.row-bot-buddy-pack-card:hover {
    border-color: rgba(79, 163, 255, 0.58);
    background: linear-gradient(180deg, rgba(24, 36, 54, 0.94), rgba(12, 18, 28, 0.76));
    transform: translateY(-1px);
}
.row-bot-buddy-pack-card-selected.q-btn {
    border-color: rgba(79, 163, 255, 0.92);
    box-shadow: 0 0 0 1px rgba(79, 163, 255, 0.34), 0 10px 24px rgba(0, 0, 0, 0.22);
    background: linear-gradient(180deg, rgba(24, 52, 81, 0.96), rgba(11, 27, 42, 0.82));
}
.row-bot-buddy-pack-preview {
    width: 100%;
    height: 104px;
    flex: 0 0 104px;
    border-radius: 8px;
    overflow: hidden;
    background: #070a0e;
    border: 1px solid rgba(148, 163, 184, 0.16);
}
.row-bot-buddy-pack-preview .q-img__image {
    object-fit: contain !important;
}
.row-bot-buddy-pack-preview-empty {
    display: flex;
    align-items: center;
    justify-content: center;
    border: 1px dashed rgba(148, 163, 184, 0.28);
    color: rgba(148, 163, 184, 0.76);
}
.row-bot-buddy-pack-title {
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
.row-bot-buddy-pack-meta {
    width: 100%;
    min-width: 0;
    color: #9fb0c3;
    font-size: 10px;
    line-height: 1.2;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.row-bot-buddy-pack-card-selected .row-bot-buddy-pack-title {
    color: #8fc7ff;
}
@keyframes row-bot-buddy-sidebar-ring {
    0%, 100% { opacity: 0.72; transform: scale(0.985); }
    50% { opacity: 1; transform: scale(1.015); }
}
.row-bot-buddy-fallback {
  position: absolute;
  inset: 0;
  display: grid;
  place-items: center;
  color: #e4c25e;
  font-size: 42px;
  opacity: 0;
  transition: opacity 0.18s ease;
}
.row-bot-buddy-wrap.buddy-unavailable .row-bot-buddy-fallback { opacity: 1; }
.row-bot-buddy-status {
    display: none;
  max-width: 168px;
  color: #8f9baa;
  font-size: 10px;
  line-height: 1.2;
  text-align: center;
}
.row-bot-buddy-wrap[data-surface="sidebar"] .row-bot-buddy-status {
        display: block;
        max-width: 198px;
        min-height: 13px;
        color: #a7b3c2;
}
.row-bot-buddy-in-app.row-bot-buddy-drag-preview .row-bot-buddy-stage {
    width: 154px;
    height: 154px;
    filter: drop-shadow(0 16px 24px rgba(0, 0, 0, 0.34));
    border: 0;
}
.row-bot-buddy-in-app.row-bot-buddy-drag-preview .row-bot-buddy-stage::after {
    display: none;
}
.row-bot-buddy-in-app.row-bot-buddy-drag-preview .row-bot-buddy-wrap,
.row-bot-buddy-overlay-page .row-bot-buddy-wrap {
    position: relative;
}
.row-bot-buddy-overlay-page .row-bot-buddy-wrap {
    width: 100%;
    height: 100%;
    justify-content: center;
    gap: 4px;
}
.row-bot-buddy-in-app.row-bot-buddy-drag-preview .row-bot-buddy-status,
.row-bot-buddy-overlay-page .row-bot-buddy-status {
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
}
.row-bot-buddy-wrap[data-bubble-verbosity="quiet"][data-surface="desktop"] .row-bot-buddy-status:empty {
    display: none;
}
.row-bot-buddy-overlay-page {
    position: fixed;
    inset: 0;
    box-sizing: border-box;
    width: 100vw;
    height: 100vh;
    overflow: hidden;
    padding: 8px;
    display: flex;
    flex-direction: column;
    align-items: stretch;
    gap: 6px;
    color: #e7edf5;
    background: linear-gradient(145deg, #0b1119, #111b26);
    border: 1px solid rgba(92, 167, 183, 0.34);
    border-radius: 0;
    box-shadow: 0 18px 42px rgba(0, 0, 0, 0.42);
}
html.row-bot-buddy-overlay-html,
body.row-bot-buddy-overlay-body,
body.row-bot-buddy-overlay-body .nicegui-layout,
body.row-bot-buddy-overlay-body .q-layout,
body.row-bot-buddy-overlay-body .q-page-container,
body.row-bot-buddy-overlay-body .q-page,
body.row-bot-buddy-overlay-body .nicegui-content {
    background: transparent !important;
    background-color: transparent !important;
}
body.row-bot-buddy-overlay-body {
    margin: 0 !important;
    overflow: hidden !important;
}
.row-bot-buddy-overlay-page .row-bot-buddy-stage {
    width: min(68vw, 176px);
    height: min(68vw, 176px);
}
.row-bot-buddy-overlay-page .row-bot-buddy-status {
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
.row-bot-buddy-overlay-header {
    flex: 0 0 auto;
    min-height: 44px;
    display: flex;
    align-items: center;
    gap: 6px;
    cursor: grab;
    user-select: none;
}
.row-bot-buddy-overlay-header:active { cursor: grabbing; }
.row-bot-buddy-overlay-avatar {
    width: 44px;
    min-width: 44px;
    height: 44px;
    overflow: hidden;
    pointer-events: none;
}
.row-bot-buddy-overlay-avatar .row-bot-buddy-stage {
    width: 44px;
    height: 44px;
    border-radius: 999px;
}
.row-bot-buddy-overlay-avatar .row-bot-buddy-status { display: none; }
.row-bot-buddy-overlay-meta { min-width: 0; flex: 1; gap: 1px; }
.row-bot-buddy-overlay-title {
    max-width: 240px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-size: 12px;
    font-weight: 700;
}
.row-bot-buddy-overlay-subtitle {
    max-width: 240px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: #9fb0c3;
    font-size: 10px;
}
.row-bot-buddy-overlay-no-drag,
.row-bot-buddy-overlay-no-drag * { cursor: default; }
.row-bot-buddy-overlay-body {
    flex: 1 1 auto;
    min-height: 0;
    align-items: stretch;
}
.row-bot-buddy-overlay-response {
    flex: 1 1 auto;
    min-height: 0;
    max-height: none;
    overflow-y: auto;
    padding: 7px 9px;
    border-radius: 9px;
    background: rgba(3, 7, 12, 0.56);
    border: 1px solid rgba(148, 163, 184, 0.16);
    color: #d9e2ec;
    font-size: 10px;
    line-height: 1.25;
    white-space: pre-wrap;
    word-break: break-word;
}
.row-bot-buddy-overlay-approval {
    flex: 0 0 auto;
    min-height: 34px;
    padding: 3px 5px;
    border-radius: 9px;
    background: rgba(78, 54, 4, 0.34);
    border: 1px solid rgba(228, 194, 94, 0.34);
}
.row-bot-buddy-overlay-approval-summary {
    min-width: 0;
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: #e8e1cc;
    font-size: 10px;
    line-height: 1.2;
}
.row-bot-buddy-overlay-approval-actions { flex-shrink: 0; }
.row-bot-buddy-overlay-approval-actions .q-btn {
    min-height: 25px;
    padding: 0 5px;
    font-size: 10px;
}
.row-bot-buddy-overlay-composer {
    flex: 0 0 auto;
    min-height: 31px;
    border-radius: 10px;
    background: rgba(3, 7, 12, 0.68);
    border: 1px solid rgba(92, 167, 183, 0.26);
}
.row-bot-buddy-overlay-composer textarea {
    max-height: 42px !important;
    overflow-y: auto !important;
    font-size: 11px !important;
    line-height: 1.25 !important;
}
.row-bot-buddy-overlay-actions {
    flex: 0 0 auto;
    min-height: 26px;
    padding-top: 1px;
    border-top: 1px solid rgba(148, 163, 184, 0.14);
}
.row-bot-buddy-overlay-action.q-btn {
    color: #b8c8d9;
    transition: color 120ms ease, background-color 120ms ease;
}
.row-bot-buddy-overlay-action.q-btn:hover {
    color: #f0f5fa;
    background: rgba(92, 167, 183, 0.14);
}
.row-bot-buddy-overlay-action.q-btn:active {
    color: #ffffff;
    background: rgba(92, 167, 183, 0.22);
}
.row-bot-buddy-overlay-action.q-btn:focus-visible {
    outline: 2px solid #e4c25e;
    outline-offset: 1px;
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
    safe_concept = _clean_hatch_concept(concept)
    personality_id = personality if personality in _PERSONALITY_OPTIONS else "warm_mystical"
    hint = _PERSONALITY_GENERATION_HINTS.get(personality_id, _PERSONALITY_GENERATION_HINTS["warm_mystical"])
    parts = [
        safe_concept,
        f"Personality style: {_PERSONALITY_OPTIONS[personality_id]} - {hint}.",
    ]
    safe_notes = (notes or "").strip()
    if safe_notes:
        parts.append(f"User style notes: {safe_notes}")
    return "\n\n".join(parts)


def _clean_hatch_concept(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return f"A cute tiny mystical coding familiar for {APP_DISPLAY_NAME}"
    for marker in ("Personality style:", "User style notes:"):
        if text.startswith(marker):
            return f"A cute tiny mystical coding familiar for {APP_DISPLAY_NAME}"
        marker_index = text.find(f"\n{marker}")
        if marker_index >= 0:
            text = text[:marker_index].strip()
    return text or f"A cute tiny mystical coding familiar for {APP_DISPLAY_NAME}"


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
    render_fit = "cover"
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
    rendered_motion_url = "" if motion_pack_json != "{}" else motion_url
    element_id = f"buddy-{surface}-{uuid.uuid4().hex[:10]}"
    unavailable = "Loading motion pack" if motion_pack_json != "{}" else ("Loading motion" if motion_url else ("Loading companion" if preview_url else "Generate a companion look to activate animation"))
    return f"""
    <div id="{element_id}" class="row-bot-buddy-wrap" data-row-bot-buddy data-surface="{html.escape(surface)}" data-personality="{html.escape(personality)}" data-bubble-verbosity="{html.escape(bubble_verbosity)}" data-preview="{html.escape(preview_url)}" data-motion="{html.escape(rendered_motion_url)}" data-motion-pack="{html.escape(motion_pack_json, quote=True)}" data-generated-fit="{html.escape(render_fit)}">
      <div class="row-bot-buddy-stage">
        <canvas id="{element_id}-canvas" width="220" height="220" aria-label="Companion animation"></canvas>
        <div class="row-bot-buddy-fallback" aria-hidden="true">*</div>
      </div>
      <div class="row-bot-buddy-status buddy-status">{html.escape(unavailable)}</div>
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
        "desktop": _surface_html("desktop"),
    }
    code = f"""
        (() => {{
            const replacements = {json.dumps(surface_html)};
            document.querySelectorAll('[data-row-bot-buddy]').forEach((element) => {{
                const surface = element.dataset.surface;
                const html = replacements[surface];
                if (!html) return;
                const wrapper = document.createElement('div');
                wrapper.innerHTML = html.trim();
                const next = wrapper.firstElementChild;
                if (next) element.replaceWith(next);
            }});
            setTimeout(() => window.RowBotBuddy && window.RowBotBuddy.initAll(), 80);
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
    placement = get_buddy_placement_state()
    snapshot.update(
        {
            "placement": placement.placement.value,
            "visible": placement.visible,
            "collapsed": placement.collapsed,
        }
    )
    code = f"""
        (() => {{
            const snapshot = {json.dumps(snapshot)};
            if (!window.__ROW_BOT_BUDDY_HOLD_SNAPSHOT && window.RowBotBuddy) {{
                window.RowBotBuddy.setState(snapshot);
            }}
            const docked = snapshot.placement === 'docked' && snapshot.visible;
            if (docked && window.RowBotBuddyDock) window.RowBotBuddyDock.resetAll();
            document.querySelectorAll('[data-buddy-sidebar-shell], [data-buddy-in-app-shell]').forEach((element) => {{
                element.style.display = docked ? '' : 'none';
            }});
        }})();
    """
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
    ui.run_javascript("setTimeout(() => window.RowBotBuddy && window.RowBotBuddy.initAll(), 100);")
    return root


def _emit_buddy_hi() -> None:
    emit_buddy_event(BuddyEventType.NOTIFICATION, source="buddy.ui", payload={"label": "Hi"})


def _apply_buddy_surface_settings(cfg: dict) -> None:
    placement = get_buddy_placement_state()
    in_app_enabled = bool(cfg.get("enabled", True)) and placement.visible and placement.placement is BuddyPlacement.DOCKED
    bubble_verbosity = str(cfg.get("bubble_verbosity") or "normal")
    personality = str(cfg.get("personality") or "warm_mystical")
    ui.run_javascript(
        f"""
        (() => {{
            const inAppVisible = {json.dumps(in_app_enabled)};
            const bubbleVerbosity = {json.dumps(bubble_verbosity)};
            const personality = {json.dumps(personality)};
            document.querySelectorAll('[data-buddy-sidebar-shell]').forEach((element) => {{
                element.style.display = inAppVisible ? '' : 'none';
            }});
            document.querySelectorAll('[data-buddy-in-app-shell]').forEach((element) => {{
                element.style.display = inAppVisible ? '' : 'none';
            }});
            if (inAppVisible && window.RowBotBuddyDock) window.RowBotBuddyDock.resetAll();
            document.querySelectorAll('[data-row-bot-buddy]').forEach((element) => {{
                element.dataset.bubbleVerbosity = bubbleVerbosity;
                element.dataset.personality = personality;
            }});
        }})();
        """
    )
    build_in_app_buddy()


def build_sidebar_buddy(state, p, open_settings=None) -> None:
    cfg = get_buddy_config()
    placement = get_buddy_placement_state()
    visible = bool(cfg.get("enabled", True)) and placement.visible and placement.placement is BuddyPlacement.DOCKED
    dock_id = f"buddy-sidebar-dock-{uuid.uuid4().hex[:10]}"
    in_app_id = f"buddy-in-app-shell-{uuid.uuid4().hex[:10]}"

    def _handle_click() -> None:
        _emit_buddy_hi()
        if open_settings:
            open_settings("Buddy")

    with ui.column().classes("w-full items-center gap-1 q-mb-sm"):
        with ui.element("div").classes("row-bot-buddy-sidebar-action") as shell:
            shell._props["id"] = dock_id
            shell._props["data-buddy-sidebar-shell"] = "1"
            if not visible:
                shell.style("display: none;")
            with ui.element("div").classes("row-bot-buddy-in-app row-bot-buddy-docked") as buddy_shell:
                buddy_shell._props["id"] = in_app_id
                buddy_shell._props["data-buddy-in-app-shell"] = "1"
                buddy_shell._props["role"] = "button"
                buddy_shell._props["tabindex"] = "0"
                buddy_shell.on("buddy-click", _handle_click)
                p.sidebar_avatar = build_buddy_surface("sidebar")
        if visible and not bool(cfg.get("tear_off_hint_dismissed", False)):
            with ui.row().classes("items-center no-wrap gap-1 q-px-xs") as tear_off_hint:
                ui.label("Drag Buddy onto your desktop").classes("text-caption text-grey-6")

                def _dismiss_tear_off_hint() -> None:
                    set_buddy_config("tear_off_hint_dismissed", True)
                    tear_off_hint.delete()

                ui.button(icon="close", on_click=_dismiss_tear_off_hint).props(
                    "flat round dense size=xs aria-label='Dismiss Buddy tip'"
                )
    ui.run_javascript(f"setTimeout(() => {{ {_install_in_app_buddy_drag_js(in_app_id, dock_id)} }}, 100);")


def build_in_app_buddy(*, recreate: bool = False) -> None:
    ui.run_javascript(
        """
        (() => {
            document.querySelectorAll('[data-buddy-in-app-shell]').forEach((element) => {
                const dock = element.dataset.buddyDockId || (element.closest('[data-buddy-sidebar-shell]') || {}).id || '';
                if (dock && window.RowBotBuddyDock) window.RowBotBuddyDock.install(element.id, dock);
            });
            window.RowBotBuddy && window.RowBotBuddy.initAll();
        })();
        """
    )

def _install_in_app_buddy_drag_js(element_id: str, dock_id: str) -> str:
    return f"""
        const element = document.getElementById({json.dumps(element_id)});
        const dock = document.getElementById({json.dumps(dock_id)});
        if (!window.RowBotBuddyDock) {{
            window.RowBotBuddyDock = {{
                resetAll() {{
                    document.querySelectorAll('[data-buddy-in-app-shell]').forEach((node) => {{
                        if (node.__rowBotBuddyDockHome) node.__rowBotBuddyDockHome();
                    }});
                }},
                install(elementId, dockId) {{
                    const target = document.getElementById(elementId);
                    const targetDock = document.getElementById(dockId);
                    if (!target || !targetDock || target.dataset.buddyDragInstalled === '1') return;
                    target.dataset.buddyDockId = dockId;
                    target.dataset.buddyDragInstalled = '1';
                    let gesture = null;
                    const dockHome = () => {{
                        target.classList.add('row-bot-buddy-docked');
                        target.classList.remove('row-bot-buddy-drag-preview', 'row-bot-buddy-dragging');
                        target.style.display = '';
                        target.style.left = '';
                        target.style.top = '';
                        target.style.right = '';
                        target.style.bottom = '';
                        targetDock.classList.remove('row-bot-buddy-dock-empty', 'row-bot-buddy-dock-hover');
                        if (target.parentElement !== targetDock) targetDock.appendChild(target);
                    }};
                    const beginPreview = (rect, active) => {{
                        target.style.left = rect.left + 'px';
                        target.style.top = rect.top + 'px';
                        target.style.right = 'auto';
                        target.style.bottom = 'auto';
                        target.classList.add('row-bot-buddy-drag-preview');
                        target.classList.remove('row-bot-buddy-docked');
                        targetDock.classList.add('row-bot-buddy-dock-empty');
                        if (target.parentElement !== document.body) document.body.appendChild(target);
                        try {{ target.setPointerCapture(active.pointerId); }} catch (error) {{}}
                    }};
                    target.__rowBotBuddyDockHome = () => {{
                        const ownsProjection = gesture && (
                            gesture.phase === 'pressed'
                            || gesture.phase === 'previewing'
                            || gesture.phase === 'committing'
                        );
                        if (!ownsProjection) dockHome();
                    }};
                    const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
                    const containsPoint = (rect, event) => Boolean(
                        rect
                        && event.clientX >= rect.left
                        && event.clientX <= rect.right
                        && event.clientY >= rect.top
                        && event.clientY <= rect.bottom
                    );
                    const isOverDock = (active, event) => (
                        containsPoint(active.dockRect, event)
                        || containsPoint(targetDock.getBoundingClientRect(), event)
                    );
                    const releaseCapture = (active) => {{
                        if (!active || !target.hasPointerCapture) return;
                        try {{
                            if (target.hasPointerCapture(active.pointerId)) target.releasePointerCapture(active.pointerId);
                        }} catch (error) {{}}
                    }};
                    const finishGesture = (active, phase, restoreHome = false) => {{
                        if (!active) return;
                        active.phase = phase;
                        releaseCapture(active);
                        target.classList.remove('row-bot-buddy-dragging');
                        if (restoreHome) dockHome();
                        if (gesture === active) gesture = null;
                    }};
                    const notify = (message, type) => {{
                        if (window.Quasar && window.Quasar.Notify) {{
                            window.Quasar.Notify.create({{message, type: type || 'info', timeout: 3000}});
                        }}
                    }};
                    const cancelGesture = (active) => {{
                        if (!active || active.phase === 'committing' || active.phase === 'completed' || active.phase === 'cancelled') return;
                        finishGesture(active, 'cancelled', active.phase === 'previewing');
                    }};
                    const commitTearOff = (event, active = gesture) => {{
                        if (!active || active.phase !== 'previewing') return;
                        active.phase = 'committing';
                        active.screenX = Number.isFinite(event.screenX) ? event.screenX : active.screenX;
                        active.screenY = Number.isFinite(event.screenY) ? event.screenY : active.screenY;
                        const api = window.pywebview && window.pywebview.api ? window.pywebview.api : null;
                        if (!api || !api.tear_off_buddy) {{
                            finishGesture(active, 'cancelled', true);
                            notify('Drag-to-desktop is available in the native Row-Bot window', 'warning');
                            return;
                        }}
                        const port = Number(window.location.port || 8080);
                        Promise.resolve(api.tear_off_buddy(active.screenX, active.screenY, port)).then((ok) => {{
                            if (!ok) {{
                                finishGesture(active, 'cancelled', true);
                                notify('Buddy could not be torn off', 'negative');
                                return;
                            }}
                            active.phase = 'completed';
                            releaseCapture(active);
                            target.classList.remove('row-bot-buddy-dragging');
                            target.style.display = 'none';
                            targetDock.classList.add('row-bot-buddy-dock-empty');
                            if (gesture === active) gesture = null;
                        }}).catch(() => {{
                            finishGesture(active, 'cancelled', true);
                            notify('Buddy could not be torn off', 'negative');
                        }});
                    }};
                    target.addEventListener('pointerdown', (event) => {{
                        if (event.button !== 0 || gesture) return;
                        const rect = target.getBoundingClientRect();
                        gesture = {{
                            phase: 'pressed',
                            moved: false,
                            pointerId: event.pointerId,
                            startX: event.clientX,
                            startY: event.clientY,
                            screenX: event.screenX,
                            screenY: event.screenY,
                            offsetX: event.clientX - rect.left,
                            offsetY: event.clientY - rect.top,
                            dockRect: null,
                        }};
                        target.classList.add('row-bot-buddy-dragging');
                        try {{ target.setPointerCapture(event.pointerId); }} catch (error) {{}}
                        event.preventDefault();
                    }});
                    target.addEventListener('pointermove', (event) => {{
                        const active = gesture;
                        if (!active || active.pointerId !== event.pointerId || active.phase === 'committing') return;
                        active.screenX = event.screenX;
                        active.screenY = event.screenY;
                        const rect = target.getBoundingClientRect();
                        if (active.phase === 'pressed' && Math.hypot(event.clientX - active.startX, event.clientY - active.startY) > 6) {{
                            active.moved = true;
                            active.dockRect = targetDock.getBoundingClientRect();
                            active.phase = 'previewing';
                            beginPreview(rect, active);
                        }}
                        if (active.phase !== 'previewing') return;
                        const width = target.offsetWidth;
                        const height = target.offsetHeight;
                        target.style.left = clamp(event.clientX - active.offsetX, 8, window.innerWidth - width - 8) + 'px';
                        target.style.top = clamp(event.clientY - active.offsetY, 8, window.innerHeight - height - 8) + 'px';
                        target.style.right = 'auto';
                        target.style.bottom = 'auto';
                        const atClientEdge = event.clientX <= 1 || event.clientY <= 1 || event.clientX >= window.innerWidth - 1 || event.clientY >= window.innerHeight - 1;
                        if (atClientEdge && !isOverDock(active, event)) commitTearOff(event, active);
                    }});
                    const pointerUp = (event) => {{
                        const active = gesture;
                        if (!active || active.pointerId !== event.pointerId || active.phase === 'committing') return;
                        if (active.phase === 'pressed') {{
                            finishGesture(active, 'completed');
                            target.dispatchEvent(new CustomEvent('buddy-click', {{ bubbles: true }}));
                            return;
                        }}
                        if (active.phase !== 'previewing') return;
                        if (isOverDock(active, event)) cancelGesture(active);
                        else commitTearOff(event, active);
                    }};
                    target.addEventListener('pointerup', pointerUp);
                    target.addEventListener('pointercancel', (event) => {{
                        if (gesture && gesture.pointerId === event.pointerId) cancelGesture(gesture);
                    }});
                    target.addEventListener('lostpointercapture', (event) => {{
                        const active = gesture;
                        if (!active || active.pointerId !== event.pointerId || active.phase === 'committing') return;
                        try {{
                            if (target.hasPointerCapture && target.hasPointerCapture(event.pointerId)) return;
                        }} catch (error) {{}}
                        cancelGesture(active);
                    }});
                    target.addEventListener('keydown', (event) => {{
                        if (event.key !== 'Enter' && event.key !== ' ') return;
                        event.preventDefault();
                        target.dispatchEvent(new CustomEvent('buddy-click', {{ bubbles: true }}));
                    }});
                }},
            }};
        }}
        if (!element || element.dataset.buddyDragInstalled === '1') return;
        if (dock) window.RowBotBuddyDock.install({json.dumps(element_id)}, {json.dumps(dock_id)});
    """


def build_buddy_overlay_page(state) -> None:
    from row_bot.threads import delete_thread_draft, load_thread_draft, save_thread_draft
    from row_bot.ui.state import P, _active_generations
    from row_bot.ui.streaming import Callbacks, request_generation_stop, resume_after_interrupt, send_message

    inject_buddy_head()
    client = ui.context.client
    p = P()
    p.pending_files = []
    cb = Callbacks()

    def _noop(*_args, **_kwargs):
        return None

    for callback_name in cb.__slots__:
        setattr(cb, callback_name, _noop)
    p.streaming_callbacks = cb

    async def _native_call(expression: str, fallback: str = "false"):
        try:
            return await client.run_javascript(
                f"""
                (async () => {{
                    const api = window.pywebview && window.pywebview.api ? window.pywebview.api : null;
                    if (!api) return {fallback};
                    return await ({expression});
                }})()
                """
            )
        except Exception:
            return None

    async def _open_full_thread() -> None:
        result = await _native_call("api.show_main_window ? api.show_main_window() : false")
        if not result:
            await client.run_javascript("window.open('/', '_blank', 'noopener');")

    async def _dock() -> None:
        await _native_call("api.dock_buddy ? api.dock_buddy() : false")

    async def _hide() -> None:
        await _native_call("api.hide_buddy_window ? api.hide_buddy_window(true) : false")

    ui.run_javascript(
        """
        (() => {
            document.documentElement.style.overflow = 'hidden';
            document.documentElement.classList.add('row-bot-buddy-overlay-html');
            document.documentElement.style.background = 'transparent';
            document.body.style.overflow = 'hidden';
            document.body.style.margin = '0';
            document.body.style.background = 'transparent';
            document.body.classList.add('row-bot-buddy-overlay-body');
            const startedAt = performance.now();
            let attempts = 0;
            const revealOverlay = () => {
                const api = window.pywebview && window.pywebview.api ? window.pywebview.api : null;
                const elapsed = performance.now() - startedAt;
                const root = document.querySelector('.row-bot-buddy-overlay-page [data-row-bot-buddy]');
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
    with ui.element("div").classes("row-bot-buddy-overlay-page"):
        with ui.element("div").classes("row-bot-buddy-overlay-header pywebview-drag-region"):
            with ui.element("div").classes("row-bot-buddy-overlay-avatar"):
                build_buddy_surface("desktop")
            with ui.column().classes("row-bot-buddy-overlay-meta"):
                thread_label = ui.label("New chat").classes("row-bot-buddy-overlay-title")
                context_label = ui.label("Chat").classes("row-bot-buddy-overlay-subtitle")

        with ui.column().classes("row-bot-buddy-overlay-body w-full gap-1"):
            response_label = ui.label("Ready").classes("row-bot-buddy-overlay-response w-full")
            response_label._props["role"] = "status"
            response_label._props["aria-live"] = "polite"

            with ui.row().classes(
                "row-bot-buddy-overlay-approval w-full items-center no-wrap gap-1"
            ) as approval_box:
                approval_summary = ui.label("Approval required").classes(
                    "row-bot-buddy-overlay-approval-summary"
                )
                with ui.row().classes(
                    "row-bot-buddy-overlay-approval-actions items-center no-wrap gap-0"
                ):
                    deny_button = ui.button("Deny").props("flat dense no-caps size=sm color=negative")
                    review_button = ui.button("Details").props("flat dense no-caps size=sm")
                    approve_button = ui.button("Approve").props("flat dense no-caps size=sm color=positive")
            approval_box.set_visibility(False)

            with ui.row().classes("row-bot-buddy-overlay-composer w-full items-end no-wrap gap-1 q-px-xs"):
                composer = ui.textarea(placeholder="Message this thread…").classes("w-full").props(
                    "dense borderless autogrow rows=1 input-style='padding: 4px 3px;' aria-label='Buddy message'"
                )
                send_button = ui.button(icon="send").props("flat round dense size=sm aria-label='Send'")
                stop_button = ui.button(icon="stop").props("flat round dense size=sm color=negative aria-label='Stop'")
                stop_button.set_visibility(False)

        with ui.row().classes(
            "row-bot-buddy-overlay-actions row-bot-buddy-overlay-no-drag "
            "w-full items-center justify-center no-wrap gap-1"
        ):
            ui.button(icon="open_in_new", on_click=_open_full_thread).props(
                "flat round dense size=xs aria-label='Open full thread'"
            ).classes("row-bot-buddy-overlay-action").tooltip("Open full thread")
            ui.button(icon="dock", on_click=_dock).props(
                "flat round dense size=xs aria-label='Dock Buddy'"
            ).classes("row-bot-buddy-overlay-action").tooltip("Dock Buddy")
            ui.button(icon="visibility_off", on_click=_hide).props(
                "flat round dense size=xs aria-label='Hide Buddy'"
            ).classes("row-bot-buddy-overlay-action").tooltip("Hide Buddy")

        with ui.column().style("display:none") as hidden_chat:
            p.chat_container = hidden_chat
    p.chat_input = composer
    p.chat_header_label = thread_label

    draft_state = {"thread_id": "", "loaded_text": "", "source": ""}

    def _load_selected_draft(thread_id: str, *, force: bool = False) -> None:
        if not thread_id:
            if force:
                composer.value = ""
                composer.update()
            draft_state.update({"thread_id": "", "loaded_text": "", "source": ""})
            return
        draft = load_thread_draft(thread_id) or {}
        text = str(draft.get("text") or "")
        current = str(composer.value or "")
        can_replace = force or current == draft_state["loaded_text"] or not current
        if can_replace and current != text:
            composer.value = text
            composer.update()
        draft_state.update(
            {
                "thread_id": thread_id,
                "loaded_text": text if can_replace else current,
                "source": str(draft.get("source") or ""),
            }
        )

    def _on_draft_change(event) -> None:
        text = str(event.args if isinstance(event.args, str) else composer.value or "")
        thread_id = str(getattr(state, "thread_id", "") or "")
        if thread_id:
            save_thread_draft(thread_id, text, source="buddy_overlay")
        draft_state.update({"thread_id": thread_id, "loaded_text": text, "source": "buddy_overlay"})

    composer.on("update:model-value", _on_draft_change)

    async def _restore_foreground() -> None:
        await _native_call("api.restore_foreground_target ? api.restore_foreground_target() : false")

    async def _send() -> None:
        text = str(composer.value or "")
        if not text.strip():
            return
        target = OverlayTurnTarget.capture(state)
        captured_thread_id = target.thread_id
        await _restore_foreground()
        try:
            await send_message(text, state=state, p=p, cb=cb, turn_target=target)
        except Exception as exc:
            logger.exception("Buddy overlay send failed")
            response_label.set_text(str(exc) or "The message could not be sent. Open Row-Bot for details.")
            return
        accepted_thread_id = captured_thread_id or str(getattr(state, "thread_id", "") or "")
        accepted = accepted_thread_id in _active_generations or any(
            isinstance(message, dict)
            and message.get("role") == "user"
            and str(message.get("content") or "") == text
            for message in (getattr(state, "messages", None) or [])
        )
        if accepted:
            composer.value = ""
            composer.update()
            delete_thread_draft(accepted_thread_id)
            draft_state.update(
                {"thread_id": accepted_thread_id, "loaded_text": "", "source": "buddy_overlay"}
            )

    def _stop() -> None:
        thread_id = str(getattr(state, "thread_id", "") or "")
        request_generation_stop(thread_id, state=state, p=p, reason="buddy_overlay")

    async def _settle_approval(approved: bool) -> None:
        thread_id = str(getattr(state, "thread_id", "") or "")
        generation_id = str(getattr(state, "pending_interrupt_generation_id", "") or "")
        if not thread_id or not generation_id.startswith(f"{thread_id}:"):
            await _open_full_thread()
            return
        await resume_after_interrupt(approved, state=state, p=p, cb=cb)

    send_button.on("click", _send)
    stop_button.on("click", _stop)
    approve_button.on("click", lambda: asyncio.create_task(_settle_approval(True)))
    deny_button.on("click", lambda: asyncio.create_task(_settle_approval(False)))
    review_button.on("click", _open_full_thread)
    composer.on(
        "keydown.enter",
        _send,
        js_handler="""(event) => {
            if (event.shiftKey) return;
            event.preventDefault();
            emit();
        }""",
    )

    projection_state = {
        "key": None,
        "last_idle_poll": 0.0,
        "thread_id": "",
        "foreground_poll": 0.0,
        "error_by_thread": {},
        "surface": "Chat",
        "target_app": "",
    }

    def _render_context_label() -> None:
        surface = str(projection_state["surface"] or "Chat")
        target_app = str(projection_state["target_app"] or "")
        context_label.set_text(
            f"{surface} · Target: {target_app}" if target_app else surface
        )

    async def _poll_projection() -> None:
        now = time.monotonic()
        selected_id = str(getattr(state, "thread_id", "") or "")
        generation = _active_generations.get(selected_id)
        streaming = generation is not None and str(getattr(generation, "status", "")) in {"streaming", "interrupted"}
        if not streaming and now - projection_state["last_idle_poll"] < 0.6:
            return
        projection_state["last_idle_poll"] = now
        buddy_status = str(get_buddy_snapshot().get("message") or "")
        snapshot = build_thread_snapshot(state, _active_generations, buddy_status=buddy_status)
        if snapshot.error:
            projection_state["error_by_thread"][snapshot.thread_id] = snapshot.error
        elif streaming:
            projection_state["error_by_thread"].pop(snapshot.thread_id, None)
        cached_error = str(projection_state["error_by_thread"].get(snapshot.thread_id) or "")
        if projection_state["thread_id"] != snapshot.thread_id:
            previous_id = str(projection_state["thread_id"] or "")
            if previous_id and str(composer.value or ""):
                save_thread_draft(previous_id, str(composer.value or ""), source="buddy_overlay")
            projection_state["thread_id"] = snapshot.thread_id
            _load_selected_draft(snapshot.thread_id, force=True)
        elif not str(composer.value or ""):
            _load_selected_draft(snapshot.thread_id)
        effective_key = (snapshot.key, cached_error)
        if effective_key != projection_state["key"]:
            projection_state["key"] = effective_key
            thread_label.set_text(snapshot.thread_name)
            projection_state["surface"] = (
                {"normal_chat": "Chat", "developer": "Developer", "designer": "Designer"}.get(
                    snapshot.runtime_surface.value,
                    "Chat",
                )
            )
            _render_context_label()
            display_text = snapshot.error or cached_error or snapshot.response_text or snapshot.progress_text or "Ready"
            response_label.set_text(display_text)
            send_button.set_visibility(not snapshot.generating)
            stop_button.set_visibility(snapshot.can_stop)
            approval_box.set_visibility(snapshot.approval.required)
            if snapshot.approval.required:
                summary = snapshot.approval.description or "Approval required"
                if snapshot.approval.reason:
                    summary = f"{summary} · {snapshot.approval.reason}"
                approval_summary.set_text(summary)
                approve_button.set_visibility(snapshot.approval.simple)
                deny_button.set_visibility(snapshot.approval.simple)
                approve_button.set_text("Approve all" if snapshot.approval.count > 1 else "Approve")
                review_button.set_text("Details" if snapshot.approval.simple else "Review")
            await client.run_javascript(
                f"""
                (() => {{
                    const element = document.getElementById('c{response_label.id}');
                    if (!element) return;
                    if (!element.dataset.buddyScrollInstalled) {{
                        element.dataset.buddyScrollInstalled = '1';
                        element.dataset.buddyAtBottom = '1';
                        element.addEventListener('scroll', () => {{
                            element.dataset.buddyAtBottom = String(element.scrollHeight - element.scrollTop - element.clientHeight < 8 ? 1 : 0);
                        }});
                    }}
                    if (element.dataset.buddyAtBottom !== '0') element.scrollTop = element.scrollHeight;
                }})();
                """
            )
        if now - projection_state["foreground_poll"] >= 0.75:
            projection_state["foreground_poll"] = now
            target = await _native_call("api.get_foreground_target ? api.get_foreground_target() : null", "null")
            app_name = str(target.get("app_name") or "") if isinstance(target, dict) else ""
            if projection_state["target_app"] != app_name:
                projection_state["target_app"] = app_name
                _render_context_label()

    ui.timer(0.15, _poll_projection)
    _load_selected_draft(str(getattr(state, "thread_id", "") or ""), force=True)


def build_buddy_settings_tab(_reopen=None) -> None:
    from row_bot.identity import sanitize_personality, _PERSONALITY_MAX_LEN

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

    buddy_enabled = bool(cfg.get("enabled", True))
    placement = get_buddy_placement_state()
    has_visible_buddy = buddy_enabled and placement.visible

    ui.label("Buddy").classes("text-h6 q-mb-xs")
    with ui.row().classes("items-center justify-between w-full q-mb-sm"):
        ui.label("Companion behavior, look, and generated motion.").classes("text-grey-6 text-sm")
        with ui.row().classes("items-center gap-1"):
            ui.badge("enabled" if has_visible_buddy else "paused", color="green" if has_visible_buddy else "grey").props("outline dense")
            ui.badge(pack_status_text, color="cyan" if active_pack.status == "available" else "orange").props("outline dense")

    presence_actions, presence_body = _section("Visibility", "widgets", "Buddy is docked here or torn off onto the desktop.")
    with presence_body:
        with ui.row().classes("items-center gap-2 no-wrap w-full q-py-xs"):
            ui.icon("visibility", size="sm").classes("text-grey-6")
            with ui.column().classes("gap-0").style("min-width: 0; flex: 1;"):
                ui.label("Show Buddy").classes("text-sm text-weight-medium")
                ui.label("Drag Buddy away from the sidebar to tear it off. Use Dock Buddy in the overlay to return.").classes("text-grey-6 text-xs")
            visibility = ui.switch("Show Buddy", value=has_visible_buddy).props("dense")

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
                    card.classes(add="row-bot-buddy-pack-card-selected")
                else:
                    card.classes(remove="row-bot-buddy-pack-card-selected")
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
        with ui.element("div").classes("row-bot-buddy-pack-grid"):
            for pack in packs:
                is_selected = pack.id == selected_pack_id["value"]
                preview_url = _pack_preview_url(pack)
                status_text = "Ready" if pack.status == "available" else (pack.message or "Needs attention")
                pack_label = _display_pack_name(pack.name)
                with ui.button(on_click=lambda pack_id=pack.id: _select_pack(pack_id)).props("flat no-caps padding=none").classes(
                    "row-bot-buddy-pack-card" + (" row-bot-buddy-pack-card-selected" if is_selected else "")
                ) as pack_card:
                    pack_cards[pack.id] = pack_card
                    if preview_url:
                        ui.image(preview_url).classes("row-bot-buddy-pack-preview")
                    else:
                        with ui.element("div").classes("row-bot-buddy-pack-preview row-bot-buddy-pack-preview-empty"):
                            ui.icon("auto_awesome", size="md")
                    with ui.column().classes("gap-0 w-full"):
                        ui.label(pack_label).classes("row-bot-buddy-pack-title")
                        ui.label(f"{len(pack.motion_clips or {})} clips · {status_text}").classes("row-bot-buddy-pack-meta")
        if not packs:
            ui.label("No packs found. Generate one or refresh after installing assets.").classes("text-grey-6 text-xs")

    generation_actions, generation_body = _section("Generate Look", "auto_fix_high", f"Describe the Buddy. {APP_DISPLAY_NAME} handles sizing and motion automatically.")
    with generation_actions:
        ui.badge("background", color="purple").props("outline dense")
    with generation_body:
        prompt = ui.textarea(
            label="Concept",
            value=_clean_hatch_concept(str(cfg.get("hatch_prompt") or cfg.get("hatch_generation_prompt") or f"A cute tiny mystical coding familiar for {APP_DISPLAY_NAME}")),
        ).classes("w-full").props("outlined autogrow")
        buddy_description = ui.textarea(
            label="Style notes (optional)",
            value=str(cfg.get("personality_description") or ""),
        ).props(f"maxlength={_PERSONALITY_MAX_LEN} counter outlined autogrow").classes("w-full")
        hatch_status = ui.label("").classes("text-grey-6 text-xs")
        with ui.row().classes("items-start gap-3 w-full"):
            hatch_preview = ui.image().classes("w-40 rounded-borders").style("display: none;")
            hatch_motion = ui.html("", sanitize=False).classes("q-mt-xs")

    def _save() -> None:
        buddy_notes = sanitize_personality(str(buddy_description.value or ""))
        visible = bool(visibility.value)
        latest_cfg = get_buddy_config()
        concept_prompt = _clean_hatch_concept(str(prompt.value or ""))
        if concept_prompt != str(prompt.value or "").strip():
            prompt.set_value(concept_prompt)
        latest_cfg.update({
            "enabled": visible,
            "visible": visible,
            "pack_id": str(selected_pack_id.get("value") or "glyph"),
            "personality": str(buddy_personality.value or "warm_mystical"),
            "personality_description": buddy_notes,
            "bubble_verbosity": str(buddy_bubbles.value or "normal"),
            "hatch_prompt": concept_prompt,
            "hatch_generation_prompt": _compose_hatch_prompt(concept_prompt, str(buddy_personality.value or "warm_mystical"), buddy_notes),
        })
        if pack_selection_touched["value"]:
            _clear_hatch_media_overrides(latest_cfg)
        save_buddy_config(latest_cfg)
        cfg.clear()
        cfg.update(latest_cfg)
        if placement.placement is BuddyPlacement.DESKTOP:
            native_method = "show_buddy_window" if visible else "hide_buddy_window"
            ui.run_javascript(
                f"""
                (() => {{
                    const api = window.pywebview && window.pywebview.api ? window.pywebview.api : null;
                    if (api && api.{native_method}) Promise.resolve(api.{native_method}(true)).catch(() => {{}});
                }})();
                """
            )
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

    def _set_hatch_job_status_text(status: dict) -> None:
        if not status:
            return
        state = str(status.get("status") or "")
        message = str(status.get("message") or "")
        completed = int(status.get("completed_clips") or 0)
        total = int(status.get("total_clips") or 0)
        if state in {"queued", "running"}:
            suffix = f" ({completed}/{total} clips)" if total else ""
            hatch_status.set_text((message or "Generating Buddy in the background") + suffix)
        elif state == "completed":
            hatch_status.set_text(message or "Buddy generation complete")
        elif state == "partial":
            hatch_status.set_text(message or "Buddy still is ready; motion needs attention")
        elif state == "failed":
            hatch_status.set_text(message or "Buddy generation failed")

    async def _hatch() -> None:
        buddy_notes = sanitize_personality(str(buddy_description.value or ""))
        if buddy_notes != str(buddy_description.value or ""):
            buddy_description.set_value(buddy_notes)
            ui.notify("Some companion personality text was removed", type="warning")
            return
        concept_prompt = _clean_hatch_concept(str(prompt.value or ""))
        if concept_prompt != str(prompt.value or "").strip():
            prompt.set_value(concept_prompt)
        composed_prompt = _compose_hatch_prompt(concept_prompt, str(buddy_personality.value or "warm_mystical"), buddy_notes)
        hatch_status.set_text("Starting Buddy generation in the background...")
        hatch_button.props(add="loading")
        try:
            latest_cfg = get_buddy_config()
            latest_cfg.update({
                "hatch_prompt": concept_prompt,
                "hatch_generation_prompt": composed_prompt,
                "personality": str(buddy_personality.value or "warm_mystical"),
                "personality_description": buddy_notes,
                "bubble_verbosity": str(buddy_bubbles.value or "normal"),
            })
            save_buddy_config(latest_cfg)
            job = await run.io_bound(
                start_hatch_generation_job,
                composed_prompt,
                pack_id=str(selected_pack_id.get("value") or "glyph"),
                mode="full",
                display_prompt=concept_prompt,
                reuse_existing=False,
            )
            _set_hatch_job_status_text(job)
            emit_buddy_event(BuddyEventType.NOTIFICATION, source="buddy.hatch", payload={"label": "Buddy generation started"})
            ui.notify("Buddy generation started in the background", type="info")
        except Exception as exc:
            hatch_status.set_text("Buddy generation could not start")
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
        concept_prompt = _clean_hatch_concept(str(prompt.value or latest_cfg.get("hatch_prompt") or latest_cfg.get("hatch_generation_prompt") or ""))
        if concept_prompt != str(prompt.value or "").strip():
            prompt.set_value(concept_prompt)
        composed_prompt = _compose_hatch_prompt(
            concept_prompt,
            str(buddy_personality.value or "warm_mystical"),
            buddy_notes,
        )
        hatch_status.set_text("Generating motion pack for current Buddy art...")
        retry_motion_button.props(add="loading")
        try:
            latest_cfg = get_buddy_config()
            latest_cfg.update({
                "hatch_prompt": concept_prompt,
                "hatch_generation_prompt": composed_prompt,
                "personality": str(buddy_personality.value or "warm_mystical"),
                "personality_description": buddy_notes,
                "bubble_verbosity": str(buddy_bubbles.value or "normal"),
            })
            save_buddy_config(latest_cfg)
            job = await run.io_bound(
                start_hatch_generation_job,
                composed_prompt,
                pack_id=target_pack_id,
                mode="motion",
                preview_path=preview_path,
                display_prompt=concept_prompt,
                reuse_existing=False,
            )
            _set_hatch_job_status_text(job)
            emit_buddy_event(BuddyEventType.NOTIFICATION, source="buddy.hatch", payload={"label": "Buddy motion generation started"})
            ui.notify("Buddy motion regeneration started in the background", type="info")
        except Exception as exc:
            hatch_status.set_text("Buddy motion generation could not start")
            ui.notify(str(exc), type="negative")
        finally:
            retry_motion_button.props(remove="loading")

    seen_hatch_job = {"id": "", "terminal": False}

    def _poll_hatch_job_status() -> None:
        status = get_hatch_generation_status()
        job_id = str(status.get("id") or "")
        if not job_id:
            return
        state = str(status.get("status") or "")
        if job_id != seen_hatch_job["id"]:
            seen_hatch_job["id"] = job_id
            seen_hatch_job["terminal"] = False
        _set_hatch_job_status_text(status)
        if state in {"completed", "partial", "failed"} and not seen_hatch_job["terminal"]:
            seen_hatch_job["terminal"] = True
            latest_cfg = get_buddy_config()
            pack_id = str(status.get("pack_id") or latest_cfg.get("pack_id") or selected_pack_id.get("value") or "glyph")
            selected_pack_id["value"] = pack_id
            _refresh_existing_buddy_surfaces()
            _apply_buddy_surface_settings(latest_cfg)
            if _reopen and state in {"completed", "partial"} and not status.get("settings_refresh_seen"):
                mark_hatch_generation_status_seen(job_id)
                _reopen("Buddy")

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
                prompt=_clean_hatch_concept(str(latest_cfg.get("hatch_prompt") or prompt.value or latest_cfg.get("hatch_generation_prompt") or "")),
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
    ui.timer(2.0, _poll_hatch_job_status)
