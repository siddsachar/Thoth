from __future__ import annotations

import importlib
import pathlib
import sys


def _fresh_modules(tmp_path, monkeypatch):
    monkeypatch.setenv("THOTH_DATA_DIR", str(tmp_path))
    for name in [
        "threads",
        "developer.storage",
    ]:
        sys.modules.pop(name, None)
    import threads
    import developer.storage as storage

    return importlib.reload(threads), importlib.reload(storage)


def test_developer_workspace_storage_keeps_user_path(tmp_path, monkeypatch):
    _threads, storage = _fresh_modules(tmp_path, monkeypatch)
    repo = tmp_path / "already-cloned"
    repo.mkdir()

    workspace = storage.add_or_update_local_workspace(str(repo))

    assert workspace.path == str(repo.resolve())
    assert not pathlib.Path(workspace.path).is_relative_to(storage.DEVELOPER_DIR)
    assert storage.list_workspaces()[0].id == workspace.id


def test_developer_workspace_rejects_empty_path(tmp_path, monkeypatch):
    _threads, storage = _fresh_modules(tmp_path, monkeypatch)

    try:
        storage.add_or_update_local_workspace("")
    except ValueError as exc:
        assert "choose" in str(exc).lower() or "folder" in str(exc).lower()
    else:
        raise AssertionError("empty workspace path should not resolve to the current working directory")


def test_developer_workspace_remove_is_metadata_only(tmp_path, monkeypatch):
    threads, storage = _fresh_modules(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    workspace = storage.add_or_update_local_workspace(str(repo))
    thread_id = storage.ensure_workspace_thread(workspace.id)

    removed = storage.remove_workspace(workspace.id)

    assert removed.id == workspace.id
    assert repo.exists()
    assert storage.list_workspaces() == []
    assert storage.list_workspaces(include_hidden=True)[0].hidden is True
    assert threads._get_thread_type(thread_id) == "code"
    assert threads._get_thread_developer_workspace(thread_id) == workspace.id

    restored = storage.add_or_update_local_workspace(str(repo))
    assert restored.default_thread_id == thread_id
    assert restored.hidden is False


def test_developer_clone_requires_explicit_destination(tmp_path, monkeypatch):
    _threads, storage = _fresh_modules(tmp_path, monkeypatch)

    assert storage.suggested_clone_name("https://github.com/example/project.git") == "project"
    try:
        storage.clone_repository("https://github.com/example/project.git", str(tmp_path / "missing"))
    except ValueError as exc:
        assert "destination" in str(exc).lower() or "folder" in str(exc).lower()
    else:
        raise AssertionError("clone_repository must require an existing explicit destination folder")


def test_code_thread_metadata_and_classification(tmp_path, monkeypatch):
    threads, storage = _fresh_modules(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    workspace = storage.add_or_update_local_workspace(str(repo))

    thread_id = storage.ensure_workspace_thread(workspace.id)
    rows = threads._list_threads(include_details=True)

    assert rows[0][0] == thread_id
    assert rows[0][5] == ""  # keep existing project_id index stable for Designer tests
    assert rows[0][6] == "code"
    assert rows[0][7] == workspace.id
    assert threads._get_thread_type(thread_id) == "code"
    assert threads._get_thread_developer_workspace(thread_id) == workspace.id
    assert threads.classify_thread("", thread_id, set(), "code", workspace.id) == "code"
