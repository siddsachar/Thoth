from pathlib import Path
from types import SimpleNamespace

from row_bot.ui.bulk_select import BulkSelect, _bind_bulk_selection_checkbox
from row_bot.ui import sidebar
from row_bot.ui.sidebar import selectable_thread_ids_for_filter


ROOT = Path(__file__).resolve().parents[1]


class _FakeCheckbox:
    def __init__(self) -> None:
        self._value_handler = None
        self.stops_click_propagation = False

    def on_value_change(self, handler) -> None:
        self._value_handler = handler

    def on(self, event: str, *, js_handler: str) -> None:
        assert event == "click"
        self.stops_click_propagation = "stopPropagation" in js_handler

    def emit_value(self, value: bool) -> None:
        assert self._value_handler is not None
        self._value_handler(SimpleNamespace(value=value))


def _row(thread_id: str) -> tuple:
    return (thread_id, thread_id, "", "")


def _detail_row(
    thread_id: str,
    *,
    project_id: str = "",
    thread_type: str = "",
    developer_workspace_id: str = "",
) -> tuple:
    return (
        thread_id,
        thread_id,
        "",
        "",
        "",
        project_id,
        thread_type,
        developer_workspace_id,
    )


def test_checkbox_uncheck_updates_count_and_destructive_target() -> None:
    bulk = BulkSelect()
    bulk.set_mode(True)
    first = _FakeCheckbox()
    second = _FakeCheckbox()
    _bind_bulk_selection_checkbox(first, bulk, "first")
    _bind_bulk_selection_checkbox(second, bulk, "second")

    first.emit_value(True)
    second.emit_value(True)
    assert bulk.count == 2
    assert bulk.selected == {"first", "second"}

    first.emit_value(False)
    assert bulk.count == 1
    assert sorted(bulk.selected) == ["second"]
    assert first.stops_click_propagation is True
    assert second.stops_click_propagation is True


def test_all_bulk_checkbox_surfaces_use_typed_value_change_binding() -> None:
    paths = (
        "src/row_bot/ui/sidebar.py",
        "src/row_bot/ui/home.py",
        "src/row_bot/designer/home_tab.py",
        "src/row_bot/ui/settings.py",
    )
    for relative_path in paths:
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "_bind_bulk_selection_checkbox" in source
        assert "bool(e.args)" not in source


def test_select_many_and_deselect_many_preserve_other_filter_selections() -> None:
    bulk = BulkSelect()
    bulk.set_mode(True)
    classified = [
        (_row("chat-1"), "chat"),
        (_row("code-1"), "code"),
        (_row("code-2"), "code"),
        (_row("agent-1"), "agents"),
    ]

    bulk.select_many(selectable_thread_ids_for_filter(classified, "chat"))
    bulk.select_many(selectable_thread_ids_for_filter(classified, "code"))

    assert bulk.selected == {"chat-1", "code-1", "code-2"}
    bulk.deselect_many(selectable_thread_ids_for_filter(classified, "code"))
    assert bulk.selected == {"chat-1"}


def test_selectable_filter_ids_exclude_agents_from_all_but_include_collapsed_code_rows() -> None:
    classified = [
        (_row("chat-1"), "chat"),
        (_row("code-collapsed-1"), "code"),
        (_row("code-collapsed-2"), "code"),
        (_row("agent-child"), "agents"),
    ]

    assert selectable_thread_ids_for_filter(classified, "all") == [
        "chat-1",
        "code-collapsed-1",
        "code-collapsed-2",
    ]
    assert selectable_thread_ids_for_filter(classified, "code") == [
        "code-collapsed-1",
        "code-collapsed-2",
    ]
    assert selectable_thread_ids_for_filter(classified, "agents") == []


def test_visible_conversation_dataset_reconciles_counts_and_excludes_agent_children() -> None:
    rows = [
        _detail_row("chat-1"),
        _detail_row("design-1", project_id="project-1"),
        _detail_row("code-parent", thread_type="code"),
        _detail_row("code-collapsed", developer_workspace_id="workspace-1"),
        _detail_row("workflow-1"),
        _detail_row("agent-marked", thread_type="agent_child"),
        _detail_row("agent-legacy"),
    ]
    classified = sidebar._classify_visible_thread_rows(
        rows,
        workflow_thread_ids={"workflow-1"},
        agent_runs=[
            {
                "kind": "subagent",
                "thread_id": "agent-legacy",
                "parent_thread_id": "chat-1",
            }
        ],
    )
    counts = sidebar._visible_conversation_counts(classified)

    assert [row[0] for row, _category in classified] == [
        "chat-1",
        "design-1",
        "code-parent",
        "code-collapsed",
        "workflow-1",
    ]
    assert counts == {
        "all": 5,
        "chat": 1,
        "designer": 1,
        "code": 2,
        "workflow": 1,
    }
    assert counts["all"] == sum(
        counts[key] for key in ("chat", "designer", "code", "workflow")
    )
    assert selectable_thread_ids_for_filter(classified, "all") == [
        "chat-1",
        "design-1",
        "code-parent",
        "code-collapsed",
        "workflow-1",
    ]


def test_reported_screenshot_arithmetic_uses_175_visible_parents_not_329_rows() -> None:
    chat_rows = [_detail_row(f"chat-{index}") for index in range(144)]
    design_rows = [
        _detail_row(f"design-{index}", project_id=f"project-{index}")
        for index in range(4)
    ]
    code_rows = [
        _detail_row(f"code-{index}", thread_type="code")
        for index in range(5)
    ]
    workflow_ids = {f"workflow-{index}" for index in range(22)}
    workflow_rows = [_detail_row(thread_id) for thread_id in sorted(workflow_ids)]
    child_rows = [
        _detail_row(f"agent-child-{index}", thread_type="agent_child")
        for index in range(154)
    ]
    raw_rows = chat_rows + design_rows + code_rows + workflow_rows + child_rows

    classified = sidebar._classify_visible_thread_rows(
        raw_rows,
        workflow_thread_ids=workflow_ids,
    )
    counts = sidebar._visible_conversation_counts(classified)

    assert len(raw_rows) == 329
    assert counts == {
        "all": 175,
        "chat": 144,
        "designer": 4,
        "code": 5,
        "workflow": 22,
    }
    assert len(selectable_thread_ids_for_filter(classified, "all")) == 175


def test_removed_agents_filter_normalizes_to_all_and_cannot_select_children() -> None:
    assert sidebar._normalize_conversation_filter("agents") == "all"
    assert sidebar._normalize_conversation_filter("unknown") == "all"
    assert sidebar._normalize_conversation_filter("code") == "code"
    assert [item["key"] for item in sidebar.THREAD_FILTER_DESCRIPTORS] == [
        "all",
        "chat",
        "designer",
        "code",
        "workflow",
    ]

    classified = [
        (_row("chat-1"), "chat"),
        (_row("agent-child"), "agents"),
    ]
    bulk = BulkSelect()
    bulk.set_mode(True)
    bulk.select_many(selectable_thread_ids_for_filter(classified, "all"))
    assert bulk.selected == {"chat-1"}
    bulk.deselect_many(selectable_thread_ids_for_filter(classified, "all"))
    assert bulk.selected == set()
    assert selectable_thread_ids_for_filter(classified, "agents") == []
