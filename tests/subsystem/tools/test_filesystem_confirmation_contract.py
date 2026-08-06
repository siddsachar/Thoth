from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.subsystem


def test_filesystem_guide_preserves_conversational_save_confirmation() -> None:
    guide = " ".join(
        Path("tool_guides/filesystem_guide/SKILL.md")
        .read_text(encoding="utf-8")
        .lower()
        .split()
    )

    assert "not approval-gated" in guide
    assert "normal conversation" in guide
    assert "zero write/copy/export calls" in guide
    assert "exactly one normal" in guide
    assert "do not create an approval request" in guide
    assert "move and delete" in guide


def test_explicit_save_confirmation_survives_restart_then_writes_once(
    tmp_path,
    monkeypatch,
) -> None:
    data_dir = tmp_path / "data"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(data_dir))
    for name in ("row_bot.tasks", "row_bot.threads"):
        sys.modules.pop(name, None)

    from langchain_core.messages import AIMessage, HumanMessage
    import row_bot.tasks as tasks
    import row_bot.threads as threads

    threads.append_checkpoint_messages(
        "confirmation-thread",
        [
            HumanMessage(
                content="Prepare the checklist, but ask me before saving it."
            ),
            AIMessage(
                content="The checklist is ready. Shall I save it now?"
            ),
        ],
    )
    target = workspace / "packing-list.txt"
    assert not target.exists()
    assert tasks.get_pending_approvals(parent_thread_id="confirmation-thread") == []

    # A fresh module/checkpointer observes the pending conversational turn.
    threads = importlib.reload(threads)
    restored = threads.get_latest_checkpoint_messages("confirmation-thread")
    assert [str(message.content) for message in restored[-2:]] == [
        "Prepare the checklist, but ask me before saving it.",
        "The checklist is ready. Shall I save it now?",
    ]

    from row_bot.tools.filesystem_tool import FileSystemTool

    filesystem = FileSystemTool()
    monkeypatch.setattr(filesystem, "_get_workspace_root", lambda: str(workspace))
    monkeypatch.setattr(filesystem, "_get_selected_operations", lambda: ["write_file"])
    [write_tool] = [
        tool
        for tool in filesystem.as_langchain_tools()
        if tool.name == "workspace_write_file"
    ]
    calls = 0

    def save_after_yes() -> str:
        nonlocal calls
        calls += 1
        return str(
            write_tool.invoke(
                {
                    "file_path": "packing-list.txt",
                    "text": "Passport\nWarm coat\nChargers\n",
                    "append": False,
                }
            )
        )

    threads.append_checkpoint_messages(
        "confirmation-thread", [HumanMessage(content="Yes, save it.")]
    )
    result = save_after_yes()

    assert calls == 1
    assert "packing-list.txt" in result
    assert target.read_text(encoding="utf-8") == (
        "Passport\nWarm coat\nChargers\n"
    )
    assert tasks.get_pending_approvals(parent_thread_id="confirmation-thread") == []
    assert filesystem.destructive_tool_names == {
        "workspace_move_file",
        "workspace_file_delete",
    }


def test_ordinary_workspace_save_is_immediate_and_ungated(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    from row_bot.tools.filesystem_tool import FileSystemTool

    filesystem = FileSystemTool()
    monkeypatch.setattr(filesystem, "_get_workspace_root", lambda: str(workspace))
    monkeypatch.setattr(filesystem, "_get_selected_operations", lambda: ["write_file"])
    [write_tool] = [
        tool
        for tool in filesystem.as_langchain_tools()
        if tool.name == "workspace_write_file"
    ]

    write_tool.invoke(
        {
            "file_path": "direct-save.txt",
            "text": "Saved immediately.\n",
            "append": False,
        }
    )

    assert (workspace / "direct-save.txt").read_text(encoding="utf-8") == (
        "Saved immediately.\n"
    )
