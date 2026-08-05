from __future__ import annotations

import logging
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_settings_lifecycle_uses_generation_and_local_errors() -> None:
    src = _read("src/row_bot/ui/settings.py")
    assert "LoadGeneration" in src
    assert "settings.open.shell" in src
    assert "settings.tab.load." in src
    assert "settings.tab.render." in src
    assert "Could not load {name} settings" in src
    assert "settings_child_modal_open" in src


def test_knowledge_browse_is_bounded_and_lazy() -> None:
    src = _read("src/row_bot/ui/settings.py")
    assert "list_memory_summaries" in src
    assert '_browse_page = {"limit": 25}' in src
    assert "Load more" in src
    assert "defer_ui(_run, delay=0.3)" in src
    assert "Open to load recent recall decisions" in src
    assert "Open to load memory changes" in src
    assert "Check vault sync" in src


def test_recall_trace_labels_resolve_subjects_bounded() -> None:
    settings_src = _read("src/row_bot/ui/settings.py")
    kg_src = _read("src/row_bot/knowledge_graph.py")
    mem_src = _read("src/row_bot/memory.py")
    assert "def _trace_subjects" in settings_src
    assert "list_memory_subjects" in settings_src
    assert "def _trace_memory_label" in settings_src
    assert "subjects.get(mid)" in settings_src
    assert "list_entity_subjects" in kg_src
    assert "WHERE id IN" in kg_src
    assert "[:100]" in kg_src
    assert "def list_memory_subjects" in mem_src


def test_memory_change_log_uses_friendly_bounded_labels() -> None:
    settings_src = _read("src/row_bot/ui/settings.py")
    assert "def _journal_subjects" in settings_src
    assert "list_memory_subjects" in settings_src
    assert "def _journal_action_label" in settings_src
    assert '"user_modified": "User edit"' in settings_src
    assert "def _journal_status_label" in settings_src
    assert 'return f"status: {new_status}"' in settings_src
    assert '"high_authority_update": "Manual edit saved as authoritative"' in settings_src
    assert "old_status') or '?'" not in settings_src


def test_entity_editor_defers_heavy_sections() -> None:
    src = _read("src/row_bot/ui/entity_editor.py")
    assert "entity_editor.open.start" in src
    assert "entity_editor.render.core" in src
    assert "Open to load audit and provenance" in src
    assert "Open to load relations" in src
    assert "kg.list_entities(limit=500)" not in src
    assert "kg.list_entity_summaries(limit=50" in src
    assert "entity_editor.peer_search" in src


def test_entity_editor_save_runs_off_ui_loop() -> None:
    src = _read("src/row_bot/ui/entity_editor.py")
    assert "async def _save" in src
    assert "await run.io_bound(_commit_save)" in src
    assert "entity_editor.save.commit" in src
    assert "save_button.disable()" in src
    assert "defer_ui(_notify_saved, delay=0.05)" in src


def test_knowledge_refresh_after_save_is_staged() -> None:
    src = _read("src/row_bot/ui/settings.py")
    assert "settings.knowledge.after_save.data" in src
    assert 'context="settings knowledge refresh after entity save"' in src
    assert "await run.io_bound(_load_memory_rows)" in src
    assert "delay=0.01" in src
    assert "delay=0.06" in src
    assert "delay=0.12" in src


def test_shared_data_summary_api_exists() -> None:
    kg_src = _read("src/row_bot/knowledge_graph.py")
    mem_src = _read("src/row_bot/memory.py")
    assert "def list_entity_summaries" in kg_src
    assert "substr(description, 1, ?)" in kg_src
    assert "def list_memory_summaries" in mem_src


def test_graph_chat_and_streaming_are_instrumented() -> None:
    graph_src = _read("src/row_bot/ui/graph_panel.py")
    chat_src = _read("src/row_bot/ui/chat.py")
    streaming_src = _read("src/row_bot/ui/streaming.py")
    assert "kg.graph_to_vis_json(max_nodes=250)" in graph_src
    assert "graph_panel.render" in graph_src
    assert "editButtonHtml" in graph_src
    assert "button type=" in graph_src
    assert "data-eid=" in graph_src
    assert 'href=\\"#\\" data-eid=' not in graph_src
    assert "event.stopPropagation()" in graph_src
    assert "chat.transcript.render" in chat_src
    assert "streaming.consume_generation" in streaming_src


def test_fast_oversized_payload_logs_payload_warning_not_slow(monkeypatch, caplog) -> None:
    import row_bot.stability as stability
    import row_bot.ui.performance as perf

    snapshots: list[str] = []
    monkeypatch.setattr(stability, "log_performance_snapshot", lambda label: snapshots.append(label))

    with caplog.at_level(logging.WARNING, logger="row_bot.ui.performance"):
        perf.log_ui_perf(
            "graph_panel.render",
            11.2,
            threshold_ms=1000.0,
            payload_chars=356_930,
        )

    messages = [record.message for record in caplog.records]
    assert any("ui perf payload large: graph_panel.render" in message for message in messages)
    assert not any("ui perf slow: graph_panel.render.payload" in message for message in messages)
    assert not any("threshold=0.0ms" in message for message in messages)
    assert snapshots == []


def test_slow_render_still_logs_slow_warning(monkeypatch, caplog) -> None:
    import row_bot.stability as stability
    import row_bot.ui.performance as perf

    snapshots: list[str] = []
    monkeypatch.setattr(stability, "log_performance_snapshot", lambda label: snapshots.append(label))

    with caplog.at_level(logging.WARNING, logger="row_bot.ui.performance"):
        perf.log_ui_perf("graph_panel.render", 1500.0, threshold_ms=1000.0, payload_chars=10)

    messages = [record.message for record in caplog.records]
    assert any("ui perf slow: graph_panel.render" in message for message in messages)
    assert snapshots == ["ui-slow:graph_panel.render"]


def test_fast_component_count_warning_is_not_slow(caplog) -> None:
    import row_bot.ui.performance as perf

    with caplog.at_level(logging.WARNING, logger="row_bot.ui.performance"):
        perf.log_ui_perf("settings.render", 12.0, threshold_ms=1000.0, components=500)

    messages = [record.message for record in caplog.records]
    assert any("ui perf component count high: settings.render" in message for message in messages)
    assert not any("ui perf slow: settings.render.components" in message for message in messages)


def test_transcript_rendering_is_bounded_and_generation_safe() -> None:
    chat_src = _read("src/row_bot/ui/chat.py")
    app_src = _read("src/row_bot/app.py")
    state_src = _read("src/row_bot/ui/state.py")
    transcript_src = _read("src/row_bot/ui/transcript.py")
    render_src = _read("src/row_bot/ui/render.py")
    head_src = _read("src/row_bot/ui/head_html.py")

    assert "choose_transcript_window" in chat_src
    assert "Load earlier messages" in chat_src
    assert "TRANSCRIPT_CHUNK_TARGET_MS" in chat_src
    assert "p.transcript_generation != _render_generation" in chat_src
    assert "transcript_rendered_keys" in state_src
    assert "def message_key" in transcript_src
    assert "LARGE_TRANSCRIPT_THRESHOLD = 80" in transcript_src
    assert "TRANSCRIPT_WINDOW_SIZE = 60" in transcript_src

    assert "chat.transcript.sync" in app_src
    assert "append_tail" in app_src
    assert "p.chat_container.clear()" in app_src
    assert "rendered_window_matches" in app_src
    assert "LONG_MARKDOWN_PREVIEW_THRESHOLD = 16_000" in render_src
    assert "Show full message" in render_src
    assert "_render_text_with_embeds_now(text)" in render_src
    assert "def _render_mermaid_with_save" in render_src
    assert "Save diagram as PNG" in render_src
    assert "canvas.toDataURL('image/png')" in render_src
    assert "maxSide = 4096" in render_src
    assert "minExportWidth = 1800" in render_src
    assert "intrinsicWidth" in render_src
    assert "foreignObject" in render_src
    assert "mermaid-export-label" in render_src
    assert "wrapLabelText" in render_src
    assert "#252525 !important" in render_src
    assert "timeout=20" in render_src
    assert "Save diagram as PNG (up to 4K)" in render_src
    assert ".mermaid-rendered svg" in head_src
    assert "htmlLabels: false" in head_src
    assert "rowBotNormalizeMermaidDiagrams" in head_src
    assert "min-width: 900px" in head_src
    assert "rowBotHighlightCodeBlocks" in head_src
    assert "MutationObserver" in head_src
    assert 'pre code:not([data-highlighted="yes"])' in head_src
    assert "row-bot-live-stream" in head_src
    assert "row-bot-live-stream" in _read("src/row_bot/ui/streaming.py")
    assert "window.rowBotHighlightCodeBlocks" in render_src


def test_chat_autoscroll_observer_rejects_non_node_targets() -> None:
    chat_src = _read("src/row_bot/ui/chat.py")
    components_src = _read("src/row_bot/ui/chat_components.py")
    head_src = _read("src/row_bot/ui/head_html.py")

    # Studio rebuilds can invalidate the Quasar scroll container between
    # lookup and observer setup. The embedded browser guard is an intentional
    # source-level contract because this JavaScript runs outside Python.
    guard = "if (!c || !(c instanceof Node)) return;"
    assert guard in chat_src
    assert guard in components_src
    assert ").observe(document.documentElement" not in head_src
    assert head_src.count("ObserverRoot instanceof Node") == 3


def test_detached_finalize_marks_live_render_state_without_repaint() -> None:
    app_src = _read("src/row_bot/app.py")
    streaming_src = _read("src/row_bot/ui/streaming.py")

    assert "mark_chat_message_rendered" in streaming_src
    assert "cb.mark_chat_message_rendered(a_msg)" in streaming_src
    assert "not gen.detached" in streaming_src
    assert "def _mark_chat_message_rendered" in app_src
    assert "cb.mark_chat_message_rendered = _mark_chat_message_rendered" in app_src


def test_token_counter_is_debounced_and_generation_safe() -> None:
    app_src = _read("src/row_bot/app.py")

    assert '"scheduled_key": None' in app_src
    assert '"generation": 0' in app_src
    assert "async def _schedule_token_counter_async" in app_src
    assert "await asyncio.sleep(0.75)" in app_src
    assert 'context="token counter refresh"' in app_src
    assert "generation != _token_counter_state" in app_src


def test_ui_performance_harness_profiles_real_transcripts() -> None:
    harness_src = _read("scripts/ui_performance_harness.py")

    assert "--profile-transcript" in harness_src
    assert "--profile-blank-thread" in harness_src
    assert "load_thread_messages(thread_id)" in harness_src
    assert "choose_transcript_window" in harness_src
    assert '"transcript_load_ms": 1000' in harness_src
    assert '"transcript_initial_rows": 60' in harness_src


def test_blank_thread_shell_defers_heavy_chat_work() -> None:
    chat_src = _read("src/row_bot/ui/chat.py")
    picker_src = _read("src/row_bot/ui/chat_components.py")
    app_src = _read("src/row_bot/app.py")
    sidebar_src = _read("src/row_bot/ui/sidebar.py")
    skills_src = _read("src/row_bot/skills.py")
    state_src = _read("src/row_bot/ui/state.py")

    assert "chat.shell.render" in chat_src
    assert "chat.header.render" in chat_src
    assert "chat.composer.render" in chat_src
    assert "chat.model_surface.resolve" in chat_src
    assert "await run.io_bound(_resolve_model_surface)" in chat_src
    assert "await run.io_bound(_skills_mod.load_skills)" in chat_src
    assert "_skills_mod.load_skills()" not in chat_src
    assert "get_enabled_manual_skills_snapshot" in chat_src
    assert "chat_upload_js_installed" in chat_src
    assert "window._rowBotUploadHooksInstalled" in chat_src

    assert "chat.model_picker.options" in picker_src
    assert "Loading pinned models..." in picker_src
    assert "Pinned models unavailable" in picker_src
    assert "await run.io_bound(" in picker_src
    assert "generation_getter() != shell_generation" in picker_src

    assert 'def _rebuild_main(immediate: bool = False, reason: str = "unspecified")' in app_src
    assert "app.main.rebuild.skeleton" in app_src
    assert "app.main.rebuild.hydrate" in app_src
    assert "app.main.rebuild.immediate" in app_src
    assert 'rebuild_main(immediate=True, reason="new_thread")' in sidebar_src

    assert "def skills_loaded" in skills_src
    assert "def get_enabled_manual_skills_snapshot" in skills_src
    assert "chat_shell_generation" in state_src
