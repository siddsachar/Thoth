from row_bot.ui.bulk_select import BulkSelect
from row_bot.ui.sidebar import selectable_thread_ids_for_filter


def _row(thread_id: str) -> tuple:
    return (thread_id, thread_id, "", "")


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
    assert selectable_thread_ids_for_filter(classified, "agents") == ["agent-child"]

