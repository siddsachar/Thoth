from __future__ import annotations

import pytest

from tests.fixtures.developer import fake_change_set, fake_pending_change, fake_workspace


pytestmark = pytest.mark.subsystem


def test_sandbox_import_requires_approval_before_host_patch(tmp_path, monkeypatch) -> None:
    from row_bot.developer.sandbox import ApprovalDecision
    from row_bot.tools import developer_tool

    workspace = fake_workspace(tmp_path)
    pending = fake_pending_change(workspace.id)
    change_set = fake_change_set(workspace.id)
    calls: list[bool] = []
    marked: list[str] = []

    def fake_apply_patch_to_workspace(*_args, confirmed: bool, **_kwargs):
        calls.append(confirmed)
        return change_set, ApprovalDecision("allow", "confirmed")

    monkeypatch.setattr(developer_tool, "_active_workspace", lambda: (workspace, tmp_path / "workspace"))
    monkeypatch.setattr(developer_tool, "_active_approval_mode", lambda: "approve")
    monkeypatch.setattr(developer_tool, "get_thread_id", lambda: "thread-1")
    monkeypatch.setattr(developer_tool, "get_pending_change", lambda change_id: pending if change_id == pending.id else None)
    monkeypatch.setattr(developer_tool, "mark_pending_change_imported", lambda change_id: marked.append(change_id))
    monkeypatch.setattr(developer_tool, "interrupt", lambda _payload: True)
    monkeypatch.setattr(developer_tool.developer_edits, "apply_patch_to_workspace", fake_apply_patch_to_workspace)

    result = developer_tool._import_sandbox_changes(pending.id, "Import test patch")

    assert calls == [True]
    assert marked == [pending.id]
    assert "Imported sandbox change pending-1 as change set change-1" in result
    assert "- update app.py" in result


def test_sandbox_import_cancel_does_not_apply_or_mark_pending(tmp_path, monkeypatch) -> None:
    from row_bot.developer.sandbox import ApprovalDecision
    from row_bot.tools import developer_tool

    workspace = fake_workspace(tmp_path)
    pending = fake_pending_change(workspace.id)
    calls: list[bool] = []
    marked: list[str] = []

    def fake_apply_patch_to_workspace(*_args, confirmed: bool, **_kwargs):
        calls.append(confirmed)
        return None, ApprovalDecision("ask", "approval required")

    monkeypatch.setattr(developer_tool, "_active_workspace", lambda: (workspace, tmp_path / "workspace"))
    monkeypatch.setattr(developer_tool, "_active_approval_mode", lambda: "approve")
    monkeypatch.setattr(developer_tool, "get_thread_id", lambda: "thread-1")
    monkeypatch.setattr(developer_tool, "get_pending_change", lambda change_id: pending if change_id == pending.id else None)
    monkeypatch.setattr(developer_tool, "mark_pending_change_imported", lambda change_id: marked.append(change_id))
    monkeypatch.setattr(developer_tool, "interrupt", lambda _payload: False)
    monkeypatch.setattr(developer_tool.developer_edits, "apply_patch_to_workspace", fake_apply_patch_to_workspace)

    result = developer_tool._import_sandbox_changes(pending.id, "Import test patch")

    assert result == "Sandbox import cancelled by user."
    assert calls == []
    assert marked == []


def test_sandbox_import_rejects_pending_change_for_other_workspace(tmp_path, monkeypatch) -> None:
    from row_bot.tools import developer_tool

    workspace = fake_workspace(tmp_path)
    pending = fake_pending_change("other-workspace")
    monkeypatch.setattr(developer_tool, "_active_workspace", lambda: (workspace, tmp_path / "workspace"))
    monkeypatch.setattr(developer_tool, "get_pending_change", lambda _change_id: pending)

    with pytest.raises(ValueError, match="not found"):
        developer_tool._import_sandbox_changes(pending.id)
