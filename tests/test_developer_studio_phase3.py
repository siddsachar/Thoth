from __future__ import annotations

import importlib
import subprocess
import sys


def _fresh_modules(tmp_path, monkeypatch):
    monkeypatch.setenv("THOTH_DATA_DIR", str(tmp_path / "data"))
    for name in ["threads", "developer.storage", "developer.agent_context"]:
        sys.modules.pop(name, None)
    import developer.storage as storage
    import developer.agent_context as agent_context

    return importlib.reload(storage), importlib.reload(agent_context)


def test_developer_agent_context_is_compact_and_policy_aware(tmp_path, monkeypatch):
    storage, agent_context = _fresh_modules(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("print('hello')\n", encoding="utf-8")
    (repo / "node_modules").mkdir()
    (repo / "src").mkdir()
    workspace = storage.add_or_update_local_workspace(str(repo))
    storage.set_workspace_approval_mode(workspace.id, "auto_edit")

    context = agent_context.build_developer_agent_context(workspace.id)

    assert "[Developer Studio context]" in context
    assert f"Path: {repo.resolve()}" in context
    assert "Approval mode: auto_edit" in context
    assert "explicit destination" in context
    assert "- app.py" in context
    assert "- src/" in context
    assert "node_modules" not in context
    assert "print('hello')" not in context


def test_developer_agent_context_reports_git_state(tmp_path, monkeypatch):
    storage, agent_context = _fresh_modules(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo), check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test User"], check=True)
    (repo / "README.md").write_text("# Test\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True, text=True)
    (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    workspace = storage.add_or_update_local_workspace(str(repo))

    context = agent_context.build_developer_agent_context(workspace.id)

    assert "Git branch:" in context
    assert "Git dirty: yes" in context


def test_developer_identity_questions_answer_from_context(tmp_path, monkeypatch):
    storage, agent_context = _fresh_modules(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo), check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "-b", "feature/test"], check=True, capture_output=True, text=True)
    workspace = storage.add_or_update_local_workspace(str(repo))

    answer = agent_context.maybe_answer_workspace_identity(
        workspace.id,
        "What repo am I in and what branch is active?",
    )

    assert answer is not None
    assert "repo" in answer
    assert f"Path: `{repo.resolve()}`" in answer
    assert "Active branch: `feature/test`" in answer
    assert agent_context.maybe_answer_workspace_identity(workspace.id, "Review the code") is None
