from __future__ import annotations

import importlib
import subprocess
import sys


def _fresh_modules(tmp_path, monkeypatch):
    monkeypatch.setenv("THOTH_DATA_DIR", str(tmp_path / "data"))
    for name in ["threads", "developer.storage", "developer.git", "developer.sandbox"]:
        sys.modules.pop(name, None)
    import threads
    import developer.storage as storage
    import developer.git as dev_git
    import developer.sandbox as sandbox

    return (
        importlib.reload(threads),
        importlib.reload(storage),
        importlib.reload(dev_git),
        importlib.reload(sandbox),
    )


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _init_repo(repo):
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo), check=True, capture_output=True, text=True)
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("# Test\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")


def test_developer_approval_policy_modes(tmp_path, monkeypatch):
    _threads, _storage, _dev_git, sandbox = _fresh_modules(tmp_path, monkeypatch)

    assert sandbox.decide_action("read_only", "read").decision == "allow"
    assert sandbox.decide_action("read_only", "edit").decision == "block"
    assert sandbox.decide_action("ask", "edit").decision == "ask"
    assert sandbox.decide_action("auto_edit", "edit").decision == "allow"
    assert sandbox.decide_action("auto_edit", "git_push").decision == "ask"
    assert sandbox.decide_action("agent_run", "start_server").decision == "allow"
    assert sandbox.action_needs_explicit_user_intent("git_push") is True


def test_developer_workspace_approval_mode_persists(tmp_path, monkeypatch):
    _threads, storage, _dev_git, _sandbox = _fresh_modules(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()

    workspace = storage.add_or_update_local_workspace(str(repo))
    updated = storage.set_workspace_approval_mode(workspace.id, "auto_edit")

    assert updated.approval_mode == "auto_edit"
    assert storage.get_workspace(workspace.id).approval_mode == "auto_edit"


def test_developer_git_status_and_branch_creation(tmp_path, monkeypatch):
    _threads, _storage, dev_git, _sandbox = _fresh_modules(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    _init_repo(repo)

    status = dev_git.get_git_status(str(repo))
    assert status.is_git is True
    assert status.dirty is False

    (repo / "change.txt").write_text("dirty\n", encoding="utf-8")
    dirty = dev_git.get_git_status(str(repo))
    assert dirty.dirty is True

    branch_status = dev_git.create_branch(str(repo), "feat/example branch")
    assert branch_status.branch == "feat/example-branch"


def test_developer_worktree_requires_explicit_parent(tmp_path, monkeypatch):
    _threads, _storage, dev_git, _sandbox = _fresh_modules(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    _init_repo(repo)

    try:
        dev_git.create_worktree(str(repo), str(tmp_path / "missing"), "feat/sandbox")
    except ValueError as exc:
        assert "parent folder" in str(exc)
    else:
        raise AssertionError("create_worktree must require an explicit existing parent")

    parent = tmp_path / "worktrees"
    parent.mkdir()
    target = dev_git.create_worktree(str(repo), str(parent), "feat/sandbox")
    assert target.exists()
    assert target.parent == parent

