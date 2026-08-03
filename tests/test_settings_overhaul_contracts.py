from __future__ import annotations

import ast
from pathlib import Path


SETTINGS = Path("src/row_bot/ui/settings.py")


def _function_source(name: str) -> str:
    source = SETTINGS.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"{name} not found")


def test_settings_sections_moved_to_requested_tabs():
    system_src = _function_source("_build_system_access_tab")
    prefs_src = _function_source("_build_preferences_tab")
    knowledge_src = _function_source("_build_knowledge_tab")
    channels_src = _function_source("_build_channels_tab")

    assert "_build_window_mode_section()" in prefs_src
    assert "_build_window_mode_section()" not in system_src
    assert "_build_dream_cycle_section()" in prefs_src
    assert "Dream Cycle" not in knowledge_src
    assert "_build_tunnel_settings_section()" in system_src
    assert "_build_tunnel_settings_section()" not in channels_src
    assert "Tunnel credentials are in System" in channels_src


def test_settings_polish_helpers_are_local_and_used():
    settings_src = SETTINGS.read_text(encoding="utf-8")

    for helper in (
        "_settings_header",
        "_settings_section",
        "_metric_chip",
        "_status_dot",
    ):
        assert f"def {helper}" in settings_src

    for tab_name in (
        "_build_documents_tab",
        "_build_tools_tab",
        "_build_system_access_tab",
        "_build_utilities_tab",
        "_build_tracker_tab",
        "_build_knowledge_tab",
        "_build_voice_tab",
        "_build_channels_tab",
        "_build_preferences_tab",
        "_build_skills_tab",
    ):
        assert "_settings_header(" in _function_source(tab_name)


def test_skills_tab_uses_compact_management_surface():
    skills_src = _function_source("_build_skills_tab")

    assert "_settings_header(" in skills_src
    assert "_settings_section(" in skills_src
    assert "_metric_chip(" in skills_src
    assert '"Search skills"' in skills_src
    assert '"Filter"' in skills_src
    assert '"Sort"' in skills_src
    assert '"More actions"' in skills_src
    assert "ui.menu_item" in skills_src
    assert '"Pinned skills start active in new chats, tasks, Designer, and Developer."' in skills_src
    assert 'with ui.card().classes("w-full q-pa-sm")' not in skills_src
    assert '"Available",\n                                value=skills_mod.is_enabled' not in skills_src


def test_status_and_home_links_follow_new_information_architecture():
    status_src = Path("src/row_bot/ui/status_checks.py").read_text(encoding="utf-8")
    home_src = Path("src/row_bot/ui/home.py").read_text(encoding="utf-8")
    tunnel_src = status_src.split("def check_tunnel", 1)[1].split("def check_gmail_oauth", 1)[0]
    dream_src = status_src.split("def check_dream_cycle", 1)[1].split("def check_tts", 1)[0]

    assert 'CheckResult("Tunnel"' in status_src
    assert 'settings_tab="System"' in tunnel_src
    assert 'CheckResult("Dream Cycle"' in status_src
    assert 'settings_tab="Preferences"' in dream_src
    assert "Settings \u2192 Preferences" in home_src
    assert "Settings \u2192 Knowledge" not in home_src


def test_settings_shell_is_providers_first_and_single_panel():
    settings_src = SETTINGS.read_text(encoding="utf-8")
    app_src = Path("src/row_bot/app.py").read_text(encoding="utf-8")
    open_src = _function_source("open_settings")
    build_models_src = _function_source("_build_models_tab")

    assert 'initial_tab: str = "Providers"' in settings_src
    assert 'def _open_settings(initial_tab: str = "Providers")' in app_src
    assert "with ui.tabs(value=_initial_name)" in open_src
    assert open_src.index('tab_cloud = ui.tab("Providers"') < open_src.index('tab_models = ui.tab("Models"')
    assert open_src.index('(tab_cloud, "Providers", _build_cloud_tab)') < open_src.index('(tab_models, "Models", _build_models_tab)')
    assert "ui.tab_panels" not in open_src
    assert "ui.tab_panel" not in open_src
    assert "content_panel = ui.column()" in open_src
    assert "_load_generation" in open_src
    assert "data = await run.io_bound(_collect_models_tab_data)" in build_models_src
    assert 'safe_ui_task(_load_models, context="models settings load")' in build_models_src
    render_models_src = _function_source("_render_models_tab_content")
    assert "_safe_model_select_state" in settings_src
    assert "_model_options_map" in settings_src
    assert "build_lazy_model_catalog_section" in render_models_src
    assert "build_model_catalog_section(" not in render_models_src
    assert "Current Brain default is unavailable" in render_models_src
    assert "Current Image default is unavailable" in render_models_src
    assert "Current Video default is unavailable" in render_models_src


def test_models_tab_uses_collected_current_model_snapshot():
    collect_src = _function_source("_collect_models_tab_data")
    render_src = _function_source("_render_models_tab_content")

    assert '"current_model": current_model' in collect_src
    assert 'snapshot.get("current_model")' in render_src
    assert "state.current_model = current" in render_src


def test_settings_shell_preserves_all_registered_tabs():
    open_src = _function_source("open_settings")

    for tab_name in (
        "Providers",
        "Models",
        "Documents",
        "Search",
        "Skills",
        "System",
        "Accounts",
        "Utilities",
        "Tracker",
        "Knowledge",
        "Buddy",
        "Voice",
        "Channels",
        "MCP",
        "Plugins",
        "Preferences",
    ):
        assert f'"{tab_name}"' in open_src


def test_voice_tab_owns_voice_models_not_credentials():
    voice_src = _function_source("_build_voice_tab")

    for section in (
        "Talk",
        "Dictation",
        "Realtime Talk Voice",
        "Normal Read-Aloud",
        "Voice Models",
        "Provider Voice Models",
        "Diagnostics",
    ):
        assert section in voice_src

    assert "_load_voice_model_rows" in SETTINGS.read_text(encoding="utf-8")
    assert 'list_quick_choices("voice"' in SETTINGS.read_text(encoding="utf-8")
    assert 'on_click=lambda: _reopen("Providers")' in voice_src
    assert "Provider credentials stay in Providers" in voice_src
    assert "does not call the LLM until Send" in voice_src
    assert "OpenAI Realtime sends live microphone audio" in voice_src
    assert "Dictation is STT-only" in voice_src
    assert "ongoing provider cost" in voice_src
    assert "build_voice_provider_catalog" in voice_src
    assert "provider_options_for_capability" in voice_src
    assert "model_options_for_capability" in voice_src


def test_voice_tab_uses_dependent_provider_selectors_and_realtime_controls():
    voice_src = _function_source("_build_voice_tab")

    assert "def _set_talk_provider" in voice_src
    assert "talk_model=selected_or_default_model(voice_catalog, \"talk\", provider_id, \"\")" in voice_src
    assert "def _set_dictation_provider" in voice_src
    assert "dictation_model=selected_or_default_model(voice_catalog, \"dictation\", provider_id, \"\")" in voice_src
    assert "def _set_speech_output_provider" in voice_src
    assert "speech_output_model=selected_or_default_model(voice_catalog, \"speech_output\", provider_id, \"\")" in voice_src
    assert 'if talk_provider_value == "openai_realtime"' in voice_src
    assert "REALTIME_VOICE_OPTIONS" in voice_src
    assert "realtime_speaking_rate" not in voice_src
    assert "Speaking speed" not in voice_src
    assert "Fallback to local Talk if Realtime is unavailable" in voice_src
    assert "Normal Read-Aloud is local text chat playback" in voice_src


def test_voice_diagnostics_include_turn_latency_summary():
    voice_src = _function_source("_build_voice_tab")

    assert "realtime_latency_summary_ms" in voice_src
    assert "Turn timing:" in voice_src


def test_document_ingestion_ui_exposes_only_agreed_controls_and_milestones():
    settings_src = SETTINGS.read_text(encoding="utf-8")
    documents_src = _function_source("_build_documents_tab")
    upload_src = _function_source("_handle_doc_upload_batch")
    status_src = Path("src/row_bot/ui/status_bar.py").read_text(encoding="utf-8")

    for label in (
        "Queued",
        "Indexing",
        "Searchable",
        "Extracting",
        "Complete",
        "Failed",
        "Cancelled",
        "Duplicate skipped",
    ):
        assert f'"{label}"' in settings_src
    for control in (
        '"Pause"',
        '"Resume"',
        '"Cancel remaining"',
        '"Clear finished"',
        '"Retry {job.original_name}"',
        '"Cancel {job.original_name}"',
    ):
        assert control in documents_src
    for forbidden in (
        '"Batch size"',
        '"Shard size"',
        '"Lease duration"',
        '"Disk reserve"',
        '"Parser settings"',
    ):
        assert forbidden not in documents_src

    assert "stage_upload(" in upload_src
    assert "upload.size()" in upload_src
    assert ".read()" not in upload_src
    assert "document_job_service.finish_batch_staging" in upload_src
    assert "Accepted {accepted} document" in upload_src
    assert "was not accepted" in upload_src
    assert "document_job_service.pause_batch" in documents_src
    assert "document_job_service.cancel_job" in documents_src
    assert "document_job_service.cancel_batch" in documents_src
    assert "document_job_service.retry_failed" in documents_src
    assert "document_job_service.clear_finished" in documents_src
    assert "UPDATE document_jobs" not in documents_src
    assert "Supported files: PDF, DOC, DOCX, TXT, MD, HTML, HTM, and EPUB." in documents_src
    assert "text-overflow:ellipsis" in documents_src
    assert "flex-shrink:0" in documents_src

    assert "DocumentJobService().active_summary()" in status_src
    assert "Cancel active document" in status_src
    assert "service.cancel_job" in status_src
    assert "Cancel remaining" not in status_src


def test_document_embedding_status_panel_keeps_dark_mode_contrast():
    documents_src = _function_source("_build_documents_tab")
    status_panel_src = documents_src.split("local_status_panel =", 1)[1].split(
        "def _sync_embedding_controls", 1
    )[0]

    assert "bg-grey-2" not in status_panel_src
    assert "background: rgba(148, 163, 184, 0.08);" in status_panel_src
    assert "border: 1px solid rgba(148, 163, 184, 0.24);" in status_panel_src
