from __future__ import annotations

import importlib
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]


def _fresh_threads(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    for name in ("threads", "row_bot.threads"):
        sys.modules.pop(name, None)
    import row_bot.threads as threads

    return importlib.reload(threads)


def _fresh_developer_modules(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    for name in (
        "row_bot.threads",
        "row_bot.developer.storage",
        "row_bot.developer.worktrees",
        "row_bot.tasks",
    ):
        sys.modules.pop(name, None)
    import row_bot.threads as threads
    import row_bot.developer.storage as storage

    return importlib.reload(threads), importlib.reload(storage)


def _detail_row(
    thread_id: str,
    updated_at: str,
    *,
    pinned_at: str = "",
    thread_type: str = "",
    developer_workspace_id: str = "",
    project_workspace_id: str = "",
) -> tuple:
    return (
        thread_id,
        thread_id,
        "2026-06-03T09:00:00",
        updated_at,
        "",
        "",
        thread_type,
        developer_workspace_id,
        "",
        "",
        "",
        "",
        project_workspace_id,
        pinned_at,
    )


def test_thread_pin_metadata_migrates_and_preserves_updated_at(tmp_path, monkeypatch):
    threads = _fresh_threads(tmp_path, monkeypatch)
    thread_id = threads.create_thread("Thread Jun 03, 20:45")

    with sqlite3.connect(threads.DB_PATH) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(thread_meta)").fetchall()}
        conn.execute(
            "UPDATE thread_meta SET updated_at = '2026-06-03T10:00:00' WHERE thread_id = ?",
            (thread_id,),
        )
        conn.commit()
    assert "pinned_at" in columns

    pinned_at = threads.pin_thread(thread_id)

    assert pinned_at
    assert threads.is_thread_pinned(thread_id) is True
    row = next(item for item in threads._list_threads(include_details=True) if item[0] == thread_id)
    assert row[12] == ""
    assert row[13] == pinned_at
    with sqlite3.connect(threads.DB_PATH) as conn:
        stored_updated_at = conn.execute(
            "SELECT updated_at FROM thread_meta WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()[0]
    assert stored_updated_at == "2026-06-03T10:00:00"

    threads.unpin_thread(thread_id)

    assert threads.is_thread_pinned(thread_id) is False
    row = next(item for item in threads._list_threads(include_details=True) if item[0] == thread_id)
    assert row[13] == ""
    with sqlite3.connect(threads.DB_PATH) as conn:
        stored_updated_at = conn.execute(
            "SELECT updated_at FROM thread_meta WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()[0]
    assert stored_updated_at == "2026-06-03T10:00:00"


def test_thread_pin_helpers_reject_missing_or_empty_thread_ids(tmp_path, monkeypatch):
    threads = _fresh_threads(tmp_path, monkeypatch)

    for thread_id in ("", "missing"):
        try:
            threads.pin_thread(thread_id)
        except ValueError as exc:
            assert "thread" in str(exc).lower()
        else:
            raise AssertionError(f"pinning {thread_id!r} should fail")


def test_developer_workspace_thread_rows_append_pin_state(tmp_path, monkeypatch):
    threads = _fresh_threads(tmp_path, monkeypatch)
    thread_id = threads.create_thread(
        "Developer thread",
        thread_type="code",
        developer_workspace_id="workspace_1",
        project_workspace_id="workspace_1",
    )

    pinned_at = threads.pin_thread(thread_id)
    row = threads.list_developer_workspace_threads("workspace_1")[0]

    assert row[10] == "workspace_1"
    assert row[11] == pinned_at


def test_sidebar_display_order_remains_recency_based_for_current_running_and_pinned():
    from row_bot.ui import sidebar

    rows = [
        (_detail_row("newer", "2026-06-03T10:05:00"), "chat"),
        (_detail_row("pinned", "2026-06-03T10:00:00", pinned_at="2026-06-03T11:00:00"), "chat"),
        (_detail_row("active", "2026-06-03T09:00:00"), "chat"),
        (_detail_row("running", "2026-06-03T08:00:00"), "chat"),
    ]

    ordered = sidebar._sort_classified_thread_rows(
        rows,
        active_thread_id="active",
        running_thread_ids={"running"},
    )

    assert [row[0][0] for row in ordered] == ["newer", "pinned", "active", "running"]


def test_all_conversations_sort_surfaces_pinned_before_recent_threads():
    from row_bot.ui import sidebar

    rows = [
        (_detail_row("newer_1", "2026-06-03T10:05:00"), "chat"),
        (_detail_row("newer_2", "2026-06-03T10:04:00"), "chat"),
        (_detail_row("old_pinned", "2026-06-03T09:00:00", pinned_at="2026-06-03T11:00:00"), "chat"),
    ]

    ordered = sidebar._sort_classified_thread_rows_pinned_first(rows)

    assert [row[0][0] for row in ordered] == ["old_pinned", "newer_1", "newer_2"]


def test_sidebar_visible_sections_keep_pinned_and_recent_rows_with_many_pins():
    from row_bot.ui import sidebar
    from row_bot.ui.constants import SIDEBAR_MAX_THREADS

    rows = [
        (_detail_row("latest_unpinned", "2026-06-03T12:00:00"), "chat"),
        *[
            (
                _detail_row(
                    f"old_pinned_{index}",
                    f"2026-06-03T09:{index:02d}:00",
                    pinned_at=f"2026-06-03T11:{index:02d}:00",
                ),
                "chat",
            )
            for index in range(SIDEBAR_MAX_THREADS)
        ],
    ]

    pinned_rows, recent_rows = sidebar._sidebar_visible_classified_sections(rows)
    pinned_ids = [row[0][0] for row in pinned_rows]
    recent_ids = [row[0][0] for row in recent_rows]

    assert len(pinned_rows) == 5
    assert len(pinned_rows) + len(recent_rows) <= SIDEBAR_MAX_THREADS
    assert pinned_ids[0] == "old_pinned_9"
    assert recent_ids[0] == "latest_unpinned"


def test_sidebar_filter_applies_to_pinned_and_recent_sections():
    from row_bot.ui import sidebar

    rows = [
        (_detail_row("pinned_chat", "2026-06-03T10:05:00", pinned_at="2026-06-03T11:05:00"), "chat"),
        (
            _detail_row(
                "pinned_code",
                "2026-06-03T10:04:00",
                pinned_at="2026-06-03T11:04:00",
                thread_type="code",
            ),
            "code",
        ),
        (_detail_row("recent_chat", "2026-06-03T10:03:00"), "chat"),
    ]

    filtered_rows = sidebar._filter_classified_thread_rows(rows, "chat")
    pinned_rows, recent_rows = sidebar._sidebar_visible_classified_sections(filtered_rows)

    assert [row[0][0] for row in pinned_rows] == ["pinned_chat"]
    assert [row[0][0] for row in recent_rows] == ["recent_chat"]


def test_sidebar_and_all_conversations_pin_affordances_are_wired():
    source = (ROOT / "src" / "row_bot" / "ui" / "sidebar.py").read_text(encoding="utf-8")
    modal_block = source.split("def _show_all():", 1)[1].split(
        "def _do_bulk_delete",
        1,
    )[0]

    assert "apply_thread_pin" in source
    assert "_render_pin_toggle_button(" in source
    assert "_render_pin_menu_item(" in source
    assert "_render_action_menu_item(" in source
    assert 'label="Unpin" if is_pinned else "Pin"' in source
    assert 'icon="push_pin"' in source
    assert 'label="Rename"' in source
    assert 'icon="edit"' in source
    assert 'label="Delete"' in source
    assert 'icon="delete"' in source
    assert 'js_handler="(e) => e.stopPropagation()"' in source
    assert "_pin_modal" in modal_block
    assert 'dlg.on("hide", _refresh_after_modal_hide)' in modal_block
    assert "_classify_visible_thread_rows(" in modal_block
    assert "if not bulk.active:" in modal_block
    assert "_filter_classified_thread_rows(" in modal_block
    assert "_sort_classified_thread_rows_pinned_first(_filtered)" in modal_block
    pin_modal_block = modal_block.split("def _pin_modal", 1)[1].split("item_classes =", 1)[0]
    assert "_rebuild_dialog_list()" in pin_modal_block
    assert "_rebuild_thread_list_ref[0]()" not in pin_modal_block


def test_sidebar_places_interactive_thread_pin_toggle_before_action_menu():
    source = (ROOT / "src" / "row_bot" / "ui" / "sidebar.py").read_text(encoding="utf-8")
    list_block = source.split("def _rebuild_thread_list()", 1)[1].split(
        "if len(threads) > SIDEBAR_MAX_THREADS:",
        1,
    )[0]
    sidebar_thread_block = list_block.split("with ui.item(on_click=_select)", 1)[1]
    thread_main_block, thread_side_block = sidebar_thread_block.split(
        'with ui.item_section().props("side")',
        1,
    )

    assert '"folder_open" if is_expanded else "folder"' in list_block
    assert "row-bot-thread-row" in list_block
    assert "_render_pin_toggle_button(" not in thread_main_block
    assert "_render_pin_toggle_button(" in thread_side_block
    assert "is_pinned=is_pinned" in thread_side_block
    assert "pinned=not is_pinned" in thread_side_block
    assert thread_side_block.index("_render_pin_toggle_button(") < thread_side_block.index(
        'ui.button(icon="more_vert")'
    )
    assert "row-bot-pin-toggle-unpinned" in source
    assert "row-bot-thread-row:hover .row-bot-pin-toggle-unpinned" in source
    assert "row-bot-thread-row:focus-within .row-bot-pin-toggle-unpinned" in source
    assert "_thr_icon" not in thread_main_block
    assert 'ui.icon("computer"' not in thread_main_block
    assert 'ui.icon("cloud"' not in thread_main_block
    assert 'ui.icon("code"' not in thread_main_block
    assert "_render_pinned_thread_icon" not in source


def test_sidebar_places_active_thread_spinner_left_of_thread_title():
    source = (ROOT / "src" / "row_bot" / "ui" / "sidebar.py").read_text(encoding="utf-8")
    list_block = source.split("def _rebuild_thread_list()", 1)[1].split(
        "if len(threads) > SIDEBAR_MAX_THREADS:",
        1,
    )[0]
    sidebar_thread_block = list_block.split("with ui.item(on_click=_select)", 1)[1]
    thread_main_block, thread_side_block = sidebar_thread_block.split(
        'with ui.item_section().props("side")',
        1,
    )

    assert "_thread_has_active_generation(tid)" in list_block
    assert "is_running = tid in running_tids" in list_block
    assert "row-bot-thread-activity-slot" in source
    assert "row-bot-thread-activity-spinner" in source
    assert 'ui.spinner("oval", size="xs", color="primary")' in source
    assert "_render_thread_activity_indicator(activity_label)" in thread_main_block
    assert thread_main_block.index("_render_thread_activity_indicator(activity_label)") < thread_main_block.index(
        "ui.item_label(name)"
    )
    assert "_render_pin_toggle_button(" not in thread_main_block
    assert "_render_pin_toggle_button(" in thread_side_block


def test_sidebar_active_generation_indicator_only_tracks_streaming_status():
    from row_bot.ui import sidebar
    from row_bot.ui.state import _active_generations

    previous = dict(_active_generations)
    try:
        _active_generations.clear()
        _active_generations["streaming"] = SimpleNamespace(status="streaming")
        _active_generations["done"] = SimpleNamespace(status="done")
        _active_generations["stopped"] = SimpleNamespace(status="stopped")

        assert sidebar._thread_has_active_generation("streaming") is True
        assert sidebar._thread_has_active_generation("done") is False
        assert sidebar._thread_has_active_generation("stopped") is False
        assert sidebar._thread_has_active_generation("missing") is False
    finally:
        _active_generations.clear()
        _active_generations.update(previous)


def test_sidebar_developer_groups_keep_pinned_children_inside_project_folders():
    source = (ROOT / "src" / "row_bot" / "ui" / "sidebar.py").read_text(encoding="utf-8")
    list_block = source.split("def _rebuild_thread_list()", 1)[1].split(
        "if len(threads) > SIDEBAR_MAX_THREADS:",
        1,
    )[0]

    assert "_sort_classified_thread_rows(" in list_block
    assert 'display_items.append(("section_header", "Pinned", "", False))' in list_block
    assert 'display_items.append(("section_header", "Recent", "", False))' in list_block
    assert "project_ws = row[12]" in list_block
    assert '"pinned_count": 0' in list_block
    assert 'caption_bits.append(f"{pinned_count} pinned")' in list_block
    assert "Contains pinned threads" not in list_block
    assert 'items.append(("developer_group", group, cat, False))' in list_block
    assert "developer-thread-child" in list_block


def test_developer_latest_thread_remains_recency_based_when_older_thread_is_pinned(tmp_path, monkeypatch):
    threads, storage = _fresh_developer_modules(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    workspace = storage.add_or_update_local_workspace(str(repo))
    older = storage.ensure_workspace_thread(workspace.id)
    newer = storage.create_workspace_thread(workspace.id, name="Thread Jun 03, 20:45")

    with sqlite3.connect(threads.DB_PATH) as conn:
        conn.execute(
            "UPDATE thread_meta SET updated_at = '2026-06-03T10:00:00' WHERE thread_id = ?",
            (older,),
        )
        conn.execute(
            "UPDATE thread_meta SET updated_at = '2026-06-03T10:00:01' WHERE thread_id = ?",
            (newer,),
        )
        conn.commit()
    pinned_at = threads.pin_thread(older)

    rows = storage.list_workspace_threads(workspace.id)

    assert storage.latest_workspace_thread(workspace.id) == newer
    assert [row[0] for row in rows[:2]] == [newer, older]
    assert next(row for row in rows if row[0] == older)[11] == pinned_at


def test_developer_thread_switcher_display_order_uses_current_then_pinned_then_recent():
    from row_bot.developer import ui as developer_ui

    rows = [
        ("newer", "Newer", "", "2026-06-03T10:05:00", "", "", "code", "ws", "", "", "ws", ""),
        ("pinned", "Pinned", "", "2026-06-03T10:00:00", "", "", "code", "ws", "", "", "ws", "2026-06-03T11:00:00"),
        ("current", "Current", "", "2026-06-03T09:00:00", "", "", "code", "ws", "", "", "ws", ""),
    ]

    ordered = developer_ui._sort_developer_thread_rows(rows, current_thread_id="current")

    assert [row[0] for row in ordered] == ["current", "pinned", "newer"]
