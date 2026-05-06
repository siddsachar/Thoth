from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_buddy_ui_surfaces_are_wired():
    app_src = _read("app.py")
    sidebar_src = _read("ui/sidebar.py")
    settings_src = _read("ui/settings.py")

    assert '@ui.page("/buddy-overlay")' in app_src
    assert "build_in_app_buddy" in app_src
    assert "build_sidebar_buddy" in sidebar_src
    assert 'ui.tab("Buddy"' in settings_src
    assert "build_buddy_settings_tab" in settings_src
    assert 'app.add_static_files("/_buddy"' in app_src
    assert "health_result = await run.io_bound(_run_health_check)" in app_src
    assert "Startup health check returned invalid result" in app_src
    assert "ok, err = health_result" in app_src


def test_buddy_events_are_emitted_from_runtime_sources():
    streaming_src = _read("ui/streaming.py")
    tasks_src = _read("tasks.py")
    notifications_src = _read("notifications.py")
    brain_src = _read("buddy/brain.py")

    for marker in [
        "GENERATION_STARTED",
        "TOOL_STARTED",
        "APPROVAL_NEEDED",
        "APPROVAL_APPROVED",
        "APPROVAL_DENIED",
        "GENERATION_DONE",
        "GENERATION_ERROR",
    ]:
        assert marker in streaming_src or marker in tasks_src
    assert "WORKFLOW_STARTED" in tasks_src
    assert "WORKFLOW_STEP" in tasks_src
    assert "WORKFLOW_DONE" in tasks_src
    assert "APPROVAL_TIMED_OUT" in tasks_src
    assert 'BuddyEventType.APPROVAL_DENIED: ("approval", "workflow")' in brain_src
    assert "BuddyEventType.NOTIFICATION" in notifications_src


def test_buddy_desktop_and_packaging_hooks_exist():
    launcher_src = _read("launcher.py")
    inno_src = _read("installer/thoth_setup.iss")
    mac_src = _read("installer/build_mac_app.sh")
    linux_src = _read("installer/build_linux_app.sh")

    assert "open_buddy_window" in launcher_src
    assert "mark_buddy_window_ready" in launcher_src
    assert "close_buddy_window" in launcher_src
    assert "show_buddy_window" in launcher_src
    assert "hide_buddy_window" in launcher_src
    assert "minimize_buddy_window" in launcher_src
    assert "Show Buddy" in launcher_src
    assert "Hide Buddy" in launcher_src
    assert "_send_window_command" in launcher_src
    assert "ThreadingHTTPServer" in launcher_src
    assert 'path.startswith("/buddy/show")' in launcher_src
    assert 'path.startswith("/buddy/hide")' in launcher_src
    assert "_buddy_overlay_url" in launcher_src
    assert '"transparent": True' in launcher_src
    assert '"background_color": "#000000"' in launcher_src
    assert '"background_color": "#00000000"' not in launcher_src
    assert "fallback_kwargs.pop(\"background_color\", None)" in launcher_src
    assert "minimal_kwargs" in launcher_src
    assert "plain_kwargs" in launcher_src
    assert "Buddy overlay create_window failed" in launcher_src
    assert "webview.create_window(\"Buddy\", url, width=width, height=height)" in launcher_src
    assert '"shadow": False' in launcher_src
    assert '"focus": False' in launcher_src
    assert '"hidden": True' in launcher_src
    assert '"easy_drag", "hidden"' in launcher_src
    assert "_BUDDY_WINDOW_READY = False" in launcher_src
    assert "if not _BUDDY_WINDOW_READY:" in launcher_src
    assert "_BUDDY_MANUALLY_HIDDEN" in launcher_src
    assert "cache_bust=False" in launcher_src
    assert "buddy_refresh" in launcher_src
    assert "_WINDOW_SCRIPT = r'''\nimport sys\nimport time" in launcher_src
    assert "url = _buddy_overlay_url(buddy_port)" in launcher_src
    assert "refresh_url = _buddy_overlay_url(buddy_port, cache_bust=True)" in launcher_src
    assert "existing.load_url(refresh_url)" in launcher_src
    assert '"url": url' in launcher_src
    assert "from buddy.desktop import open_buddy_overlay" not in launcher_src
    assert "..\\buddy\\*.py" in inno_src
    assert " migration buddy" in mac_src
    assert " migration buddy" in linux_src


def test_buddy_runtime_supports_generated_art_as_primary_path():
    runtime_src = _read("static/buddy/runtime/buddy.js")
    buddy_ui_src = _read("ui/buddy.py")
    manifest_src = _read("static/buddy/builtins/glyph/manifest.json")

    assert "initGeneratedRoot" in runtime_src
    assert "drawGeneratedBuddy" in runtime_src
    assert "drawCoverSource" in runtime_src
    assert "drawTransparentSource" in runtime_src
    assert "sampleBackgroundColor" in runtime_src
    assert "isVideoBackgroundPixel" in runtime_src
    assert "seedBackgroundCorners" in runtime_src
    assert "BACKGROUND_COLOR_DISTANCE_THRESHOLD = 24" in runtime_src
    assert "BACKGROUND_LUMA_DELTA_THRESHOLD = 15" in runtime_src
    assert "BACKGROUND_SEED_RATIO = 0.05" in runtime_src
    assert "distance < BACKGROUND_COLOR_DISTANCE_THRESHOLD" in runtime_src
    assert "Generated motion ready" in runtime_src
    assert "Generated motion pack ready" in runtime_src
    assert "clipForSnapshot" in runtime_src
    assert "data-motion-pack" in buddy_ui_src
    assert "data-personality" in buddy_ui_src
    assert "Generate a companion look in Settings to activate animation" in runtime_src
    assert "data-preview" in buddy_ui_src
    assert "data-motion" in buddy_ui_src
    assert "data-riv" not in buddy_ui_src
    assert "@rive-app/canvas" not in buddy_ui_src
    assert "background: transparent" in buddy_ui_src
    assert '"runtime": "generated_motion_pack"' in manifest_src
    assert '"preview": "preview.png"' in manifest_src
    assert '"path": "motions/idle.mp4"' in manifest_src


def test_buddy_hatch_prompts_request_keyable_motion_assets():
    hatch_src = _read("buddy/hatch.py")

    assert "flat solid keyable" in hatch_src
    assert "18 percent empty margin" in hatch_src
    assert "frame edge" in hatch_src
    assert "rim light or outline" in hatch_src
    assert "for attempt in range(2)" in hatch_src
    assert "reused existing clip" in hatch_src
    assert "reuse_existing" in hatch_src
    assert "_is_rate_limited_generation_result" in hatch_src
    assert "THOTH_BUDDY_GOOGLE_VIDEO_SPACING_SECONDS" in hatch_src
    assert "The user concept is the source of truth" in hatch_src
    assert "do not force ancient, mystical, ink, gold, teal, glyph, or Thoth-like motifs" in hatch_src
    assert "Thoth-inspired" not in hatch_src
    assert "teal-gold magical glow" not in hatch_src


def test_buddy_settings_keeps_rive_import_out_of_normal_ux():
    buddy_ui_src = _read("ui/buddy.py")
    settings_src = _read("ui/settings.py")

    assert "Install Buddy .riv" not in buddy_ui_src
    assert "accept='.riv'" not in buddy_ui_src
    assert "Open Preferences" not in buddy_ui_src
    assert "Generate full Buddy" in buddy_ui_src
    assert "generate_hatch_buddy" in buddy_ui_src
    assert "generate_hatch_motion_pack" in buddy_ui_src
    assert "Retry motion" in buddy_ui_src
    assert 'ui.tab("Preferences"' in settings_src
    assert "Save Buddy preferences" not in settings_src
    assert "Companion personality" in buddy_ui_src
    assert "Generation guidance and personality notes" in buddy_ui_src
    assert "_compose_hatch_prompt" in buddy_ui_src
    assert "hatch_generation_prompt" in buddy_ui_src
    assert "bubble_verbosity" in buddy_ui_src
    assert "Buddy name" not in buddy_ui_src
    assert "buddy_name_input" not in buddy_ui_src
    assert '"display_name": buddy_name' not in buddy_ui_src


def test_buddy_settings_does_not_render_loose_pack_labels():
    buddy_ui_src = _read("ui/buddy.py")

    assert 'for pack in packs:\n            ui.label(pack.name)' not in buddy_ui_src
    assert 'with ui.element("div").classes("thoth-buddy-pack-grid")' in buddy_ui_src


def test_buddy_settings_uses_visual_pack_picker():
    buddy_ui_src = _read("ui/buddy.py")

    assert "thoth-buddy-pack-grid" in buddy_ui_src
    assert "thoth-buddy-pack-card" in buddy_ui_src
    assert "thoth-buddy-pack-card-selected" in buddy_ui_src
    assert "thoth-buddy-pack-preview" in buddy_ui_src
    assert "static_url_for_path(pack.preview_path)" in buddy_ui_src
    assert "selected_pack_id" in buddy_ui_src
    assert "pack_selection_touched" in buddy_ui_src
    assert "thoth-buddy-pack-title" in buddy_ui_src
    assert "thoth-buddy-pack-meta" in buddy_ui_src
    assert "_clear_hatch_media_overrides" in buddy_ui_src
    assert '"active_hatch_preview"' in buddy_ui_src
    assert '"active_hatch_motion_pack"' in buddy_ui_src
    assert '"latest_hatch_preview"' in buddy_ui_src
    assert '"latest_hatch_motion_pack"' in buddy_ui_src
    assert "_refresh_existing_buddy_surfaces" in buddy_ui_src
    assert '"desktop": _surface_html("desktop")' in buddy_ui_src
    assert "client.run_javascript(code)" in buddy_ui_src
    assert "element.replaceWith(next)" in buddy_ui_src
    assert 'latest_cfg["pack_id"] = pack_id' in buddy_ui_src
    assert "0.20),\n.thoth-buddy-pack-grid" not in buddy_ui_src
    assert "pack_select = ui.select" not in buddy_ui_src


def test_buddy_settings_save_preserves_latest_hatch_media():
    buddy_ui_src = _read("ui/buddy.py")
    save_section = buddy_ui_src.split("def _save()", 1)[1].split("async def _hatch()", 1)[0]

    assert "latest_cfg = get_buddy_config()" in save_section
    assert "latest_cfg.update({" in save_section
    assert "_clear_hatch_media_overrides(latest_cfg)" in save_section
    assert "save_buddy_config(latest_cfg)" in save_section
    assert "_apply_buddy_surface_settings(latest_cfg)" in save_section


def test_buddy_settings_can_retry_motion_for_existing_hatch_art():
    buddy_ui_src = _read("ui/buddy.py")
    retry_section = buddy_ui_src.split("async def _retry_motion()", 1)[1].split("with ui.row().classes", 1)[0]

    assert 'latest_cfg.get("latest_hatch_preview") or latest_cfg.get("active_hatch_preview")' in retry_section
    assert "generate_hatch_motion_pack" in retry_section
    assert "reuse_existing=False" in retry_section
    assert "_refresh_existing_buddy_surfaces()" in retry_section
    assert "Buddy motion pack generated" in retry_section


def test_buddy_settings_visibility_controls_are_not_redundant():
    buddy_ui_src = _read("ui/buddy.py")

    assert '_section("Visibility"' not in buddy_ui_src
    assert '_section("Where it appears"' in buddy_ui_src
    assert '_section("Surfaces"' not in buddy_ui_src
    assert 'ui.switch("Enable Buddy"' not in buddy_ui_src
    assert '"In-app Buddy"' not in buddy_ui_src
    assert '"In app"' in buddy_ui_src
    assert "in_app_initial" in buddy_ui_src
    assert "desktop_initial" in buddy_ui_src
    assert '"enabled": in_app_enabled or desktop_enabled' in buddy_ui_src
    assert '"sidebar_enabled": in_app_enabled' in buddy_ui_src
    assert '"desktop_enabled": desktop_enabled' in buddy_ui_src
    assert '"floating_enabled": False' in buddy_ui_src


def test_buddy_settings_strips_redundant_pack_prefixes():
    buddy_ui_src = _read("ui/buddy.py")

    assert "def _display_pack_name" in buddy_ui_src
    assert 'value.lower().startswith("buddy ")' in buddy_ui_src
    assert "ui.label(pack_label).classes(\"thoth-buddy-pack-title\")" in buddy_ui_src
    assert "Selected: {pack_label}" in buddy_ui_src
    assert "{pack_label} selected" in buddy_ui_src
    assert 'ui.label("Buddy look")' not in buddy_ui_src
    assert 'label="Buddy concept"' not in buddy_ui_src


def test_buddy_surface_sizing_and_docked_drag_are_targeted():
    buddy_ui_src = _read("ui/buddy.py")
    runtime_src = _read("static/buddy/runtime/buddy.js")

    assert '.thoth-buddy-wrap[data-surface="sidebar"] .thoth-buddy-stage' in buddy_ui_src
    assert "width: 198px" in buddy_ui_src
    assert "thoth-buddy-sidebar-ring" in buddy_ui_src
    assert "data-buddy-display-name" not in buddy_ui_src
    assert "data-display-name" not in buddy_ui_src
    assert "sidebar_avatar_label = ui.label" not in buddy_ui_src
    assert "root.dataset.displayName" not in runtime_src
    assert "buddy-label" not in runtime_src
    assert ".thoth-buddy-in-app.thoth-buddy-undocked .thoth-buddy-stage::after" in buddy_ui_src
    assert "display: none" in buddy_ui_src
    assert "buddyDragInstalled" in buddy_ui_src
    assert "ThothBuddyDock" in buddy_ui_src
    assert "thoth-buddy-dock-empty" in buddy_ui_src
    assert "thoth-buddy-dock-hover" in buddy_ui_src
    assert "thoth-buddy-docked" in buddy_ui_src
    assert "thoth-buddy-undocked" in buddy_ui_src
    assert "document.body.appendChild(target)" in buddy_ui_src
    assert "targetDock.appendChild(target)" in buddy_ui_src
    assert "setSurface('floating')" in buddy_ui_src
    assert "setSurface('sidebar')" in buddy_ui_src
    assert "resetAll()" in buddy_ui_src
    assert "setPointerCapture" in buddy_ui_src
    assert "CustomEvent('buddy-click'" in buddy_ui_src
    assert "moved = true" in buddy_ui_src
    assert "_ensure_buddy_client_runtime" in buddy_ui_src
    assert "_buddy_pump_clients" in buddy_ui_src
    assert "build_in_app_buddy()" in buddy_ui_src
    assert "_thoth_buddy_floating_shell" not in buddy_ui_src
    assert "data-buddy-floating-shell" not in buddy_ui_src
    assert "ui.element(\"div\").classes(\"thoth-buddy-floating\")" not in buddy_ui_src
    assert "ui.timer(0.6, lambda: _push_snapshot(client))" in buddy_ui_src
    assert "ui.timer(0.1, lambda: _install_floating_drag" not in buddy_ui_src


def test_buddy_sidebar_click_replaces_toolbar_buttons():
    buddy_ui_src = _read("ui/buddy.py")

    assert "thoth-buddy-sidebar-action" in buddy_ui_src
    assert "data-buddy-sidebar-shell" in buddy_ui_src
    assert "data-buddy-in-app-shell" in buddy_ui_src
    assert "open_settings(\"Buddy\")" in buddy_ui_src
    assert "_emit_buddy_hi" in buddy_ui_src
    assert "thoth-buddy-toolbar" not in buddy_ui_src
    assert "icon=\"favorite\"" not in buddy_ui_src


def test_buddy_status_bubbles_and_hot_apply_are_wired():
    buddy_ui_src = _read("ui/buddy.py")
    runtime_src = _read("static/buddy/runtime/buddy.js")

    assert "data-bubble-verbosity" in buddy_ui_src
    assert '.thoth-buddy-wrap[data-surface="sidebar"] .thoth-buddy-status' in buddy_ui_src
    assert ".thoth-buddy-in-app.thoth-buddy-undocked .thoth-buddy-status" in buddy_ui_src
    assert ".thoth-buddy-overlay-page .thoth-buddy-status" in buddy_ui_src
    assert "width: min(68vw, 176px)" in buddy_ui_src
    assert "max-height: 58px" in buddy_ui_src
    assert "generatedStatus(root, snapshot)" in runtime_src
    assert "snapshot.message || snapshot.label" in runtime_src
    assert "PERSONALITY_STATUS" in runtime_src
    assert "verbosity === 'chatty'" in runtime_src
    assert "surface === 'desktop' && kind !== 'idle'" in runtime_src
    assert "DESKTOP_STATUS_HOLD_MS = 5500" in runtime_src
    assert "statusHoldText" in runtime_src
    assert "statusHoldUntil" in runtime_src
    assert "def _client_is_live(client)" in buddy_ui_src
    assert "not getattr(client, \"_deleted\", False)" in buddy_ui_src
    assert "lambda: _push_snapshot(client)" in buddy_ui_src
    assert "pump_timer.cancel(with_current_invocation=True)" in buddy_ui_src
    assert "_apply_buddy_surface_settings" in buddy_ui_src
    assert "window.pywebview.api" in buddy_ui_src
    assert "open_buddy_window" in buddy_ui_src
    assert "show_buddy_window" in buddy_ui_src
    assert "hide_buddy_window" in buddy_ui_src
    assert "minimize_buddy_window" not in buddy_ui_src
    assert "close_buddy_window" in buddy_ui_src
    assert "document.querySelectorAll('[data-buddy-in-app-shell]')" in buddy_ui_src
    assert "__THOTH_BUDDY_DESKTOP_FOCUS_SYNC" in buddy_ui_src
    assert "api.hide_buddy_window(false)" in buddy_ui_src
    assert "api.show_buddy_window(false" in buddy_ui_src
    assert "thoth-buddy-overlay-controls" not in buddy_ui_src
    assert "_hide_desktop_overlay" not in buddy_ui_src
    assert "_show_desktop_overlay" not in buddy_ui_src
    assert "thoth-buddy-overlay-html" in buddy_ui_src
    assert "thoth-buddy-overlay-body" in buddy_ui_src
    assert "window.location.pathname === '/buddy-overlay'" in buddy_ui_src
    assert "document.documentElement.classList.add('thoth-buddy-overlay-html')" in buddy_ui_src
    assert "revealOverlay" in buddy_ui_src
    assert "api.mark_buddy_window_ready" in buddy_ui_src
    assert "elapsed >= 700" in buddy_ui_src
    assert "requestAnimationFrame(() => requestAnimationFrame(revealOverlay))" in buddy_ui_src
    assert "html.thoth-buddy-overlay-html body" in buddy_ui_src
    assert "html.thoth-buddy-overlay-html .q-layout" in buddy_ui_src
    assert ".nicegui-content" in buddy_ui_src


def test_buddy_idle_video_uses_quiet_replay_cadence():
    runtime_src = _read("static/buddy/runtime/buddy.js")

    assert "IDLE_REPLAY_DELAY_MS = 60000" in runtime_src
    assert "shouldUseIdleCadence" in runtime_src
    assert "animation === 'idle_breathe'" in runtime_src
    assert "video.loop = false" in runtime_src
    assert "video.autoplay = false" in runtime_src
    assert "idleStillUntil = nowMs() + IDLE_REPLAY_DELAY_MS" in runtime_src
    assert "idleStill ? state.image" in runtime_src
    assert "LOOP_RESTART_PADDING_SECONDS = 0.08" in runtime_src
    assert "restartVideoSmoothly" in runtime_src
    assert "smoothLoopIfNeeded" in runtime_src


def test_buddy_approval_motion_is_softened_and_state_changes_crossfade():
    runtime_src = _read("static/buddy/runtime/buddy.js")
    buddy_ui_src = _read("ui/buddy.py")

    assert "CLIP_CROSSFADE_MS = 280" in runtime_src
    assert "drawTransitionedSource" in runtime_src
    assert "transitionFromSource" in runtime_src
    assert "progress * progress * (3 - 2 * progress)" in runtime_src
    assert "state.activeClip === 'approval'" in runtime_src
    assert "return 0.72" in runtime_src
    assert "isApproval ? Math.sin(phase * 2.1) * 0.7" in runtime_src
    assert "isApproval ? 'rgba(228, 194, 94, 0.28)'" in runtime_src
    assert "buddy-polish-v4" in buddy_ui_src
    assert 'data-animation="tap_glass"' in buddy_ui_src
    assert "animation-duration: 4.2s" in buddy_ui_src
    assert "animation === 'tap_glass' || animation === 'pause'" not in runtime_src
    assert "if (animation === 'pause') return 0.82" in runtime_src
