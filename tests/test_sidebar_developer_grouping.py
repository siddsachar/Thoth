from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_sidebar_groups_developer_threads_under_expandable_workspace_rows():
    source = (ROOT / "src" / "row_bot" / "ui" / "sidebar.py").read_text(encoding="utf-8")
    list_block = source.split("def _rebuild_thread_list()", 1)[1].split(
        "if len(threads) > SIDEBAR_MAX_THREADS:",
        1,
    )[0]

    assert "_SIDEBAR_DEV_EXPANDED" in source
    assert "_ensure_sidebar_dev_state_loaded" in source
    assert "_persist_sidebar_dev_expanded" in source
    assert "def _sidebar_display_items" in list_block
    assert 'filter_key not in {"all", "code"}' in list_block
    assert "top_workspace_id" in list_block
    assert "not _SIDEBAR_DEV_EXPANDED_HAS_SAVED_STATE" in list_block
    assert "_SIDEBAR_DEV_EXPANDED.add(top_workspace_id)" in list_block
    assert 'items.append(("developer_group", group, cat, False))' in list_block
    assert "project_ws = row[12]" in list_block
    assert "if project_ws in _SIDEBAR_DEV_EXPANDED" in list_block
    assert '"folder_open" if is_expanded else "folder"' in list_block
    assert "def _toggle_workspace" in list_block
    assert "developer-thread-child" in list_block


def test_sidebar_developer_grouping_preserves_thread_action_menu_on_children():
    source = (ROOT / "src" / "row_bot" / "ui" / "sidebar.py").read_text(encoding="utf-8")
    list_block = source.split("def _rebuild_thread_list()", 1)[1].split(
        "if len(threads) > SIDEBAR_MAX_THREADS:",
        1,
    )[0]

    assert "show_rename_thread_dialog" in list_block
    assert "_render_action_menu_item(" in list_block
    assert 'label="Rename"' in list_block
    assert 'icon="edit"' in list_block
    assert 'label="Delete"' in list_block
    assert 'icon="delete"' in list_block
    assert "is_developer_child" in list_block


def test_all_conversations_modal_reuses_developer_workspace_grouping():
    source = (ROOT / "src" / "row_bot" / "ui" / "sidebar.py").read_text(encoding="utf-8")
    modal_block = source.split("def _show_all():", 1)[1].split(
        "def _do_bulk_delete",
        1,
    )[0]

    assert "_sidebar_display_items(" in modal_block
    assert "_filtered" in modal_block
    assert "_MODAL_FILTER" in modal_block
    assert "sort_rows=False" in modal_block
    assert 'if item_kind == "developer_group"' in modal_block
    assert '"folder_open" if is_expanded else "folder"' in modal_block
    assert "def _toggle_workspace" in modal_block
    assert "developer-thread-child" in modal_block
    assert "_render_pin_toggle_button(" in modal_block
    assert "show_rename_thread_dialog" in modal_block


def test_all_conversations_modal_folder_toggle_does_not_rebuild_sidebar():
    source = (ROOT / "src" / "row_bot" / "ui" / "sidebar.py").read_text(encoding="utf-8")
    modal_block = source.split("def _show_all():", 1)[1].split(
        "caption_bits = [",
        1,
    )[0]
    toggle_block = modal_block.split("def _toggle_workspace", 1)[1]

    assert "_rebuild_dialog_list()" in toggle_block
    assert "_rebuild_thread_list_ref[0]()" not in toggle_block
    assert "_persist_sidebar_dev_expanded()" in toggle_block


def test_sidebar_thread_filters_are_icon_descriptors_not_wrapping_labels():
    source = (ROOT / "src" / "row_bot" / "ui" / "sidebar.py").read_text(encoding="utf-8")

    assert "THREAD_FILTER_DESCRIPTORS" in source
    for icon in ("forum", "chat_bubble_outline", "brush", "code", "task_alt", "badge"):
        assert f'"icon": "{icon}"' in source
    assert "_render_filter_button" in source
    assert "aria-label" in source
    assert "row-bot-thread-filter-icon" in source
    assert "no-caps no-wrap" in source
    assert "width: 46px" in source
    assert "height: 30px" in source
    assert "flex-wrap: nowrap" in source
    assert 'f"{label} {n}" if n else label' not in source
    assert 'style("flex-wrap: wrap;")' not in source


def test_all_conversations_modal_uses_visible_filter_chips():
    source = (ROOT / "src" / "row_bot" / "ui" / "sidebar.py").read_text(encoding="utf-8")
    modal_block = source.split("def _show_all():", 1)[1].split(
        "list_container = ui.column()",
        1,
    )[0]

    assert "_render_modal_filter_button" in source
    assert "row-bot-thread-filter-modal" in source
    assert "_render_modal_filter_button(" in modal_block
    assert "_render_filter_button(" not in modal_block
    assert 'f"{label} {count}" if count else label' in source
    assert "overflow: visible" in modal_block


def test_sidebar_developer_expanded_state_persists(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path))
    sys.modules.pop("ui.sidebar", None)
    import row_bot.ui.sidebar as sidebar

    sidebar = importlib.reload(sidebar)
    state_file = tmp_path / "sidebar_state.json"
    state_file.write_text(json.dumps({"other_sidebar_state": "keep"}), encoding="utf-8")

    sidebar._ensure_sidebar_dev_state_loaded()

    assert sidebar._SIDEBAR_DEV_EXPANDED == set()
    assert sidebar._SIDEBAR_DEV_EXPANDED_HAS_SAVED_STATE is False

    sidebar._SIDEBAR_DEV_EXPANDED = {"dev_a", "dev_b"}
    sidebar._SIDEBAR_DEV_EXPANDED_LOADED = True

    sidebar._persist_sidebar_dev_expanded()

    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["other_sidebar_state"] == "keep"
    assert saved["developer_workspace_expanded"] == ["dev_a", "dev_b"]

    sidebar._SIDEBAR_DEV_EXPANDED = set()
    sidebar._SIDEBAR_DEV_EXPANDED_LOADED = False
    sidebar._SIDEBAR_DEV_EXPANDED_HAS_SAVED_STATE = False
    sidebar._ensure_sidebar_dev_state_loaded()

    assert sidebar._SIDEBAR_DEV_EXPANDED == {"dev_a", "dev_b"}
    assert sidebar._SIDEBAR_DEV_EXPANDED_HAS_SAVED_STATE is True
