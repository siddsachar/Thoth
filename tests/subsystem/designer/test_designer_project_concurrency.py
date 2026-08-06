from __future__ import annotations

import pytest

from row_bot.designer.state import DesignerPage, DesignerProject


pytestmark = pytest.mark.subsystem


def _isolate_storage(tmp_path, monkeypatch):
    from row_bot.designer import storage

    root = tmp_path / "designer"
    monkeypatch.setattr(storage, "DESIGNER_DIR", root)
    monkeypatch.setattr(storage, "PROJECTS_DIR", root / "projects")
    monkeypatch.setattr(storage, "REFERENCES_DIR", root / "references")
    monkeypatch.setattr(storage, "ASSETS_DIR", root / "assets")
    storage._PROJECT_SAVE_LOCKS.clear()
    return storage


def test_stale_designer_copy_cannot_overwrite_newer_project(tmp_path, monkeypatch):
    storage = _isolate_storage(tmp_path, monkeypatch)
    original = DesignerProject(
        id="project-a",
        name="Initial",
        pages=[DesignerPage(title="Page", html="<h1>Initial</h1>")],
    )
    storage.save_project(original)
    first = storage.load_project(original.id)
    stale = storage.load_project(original.id)
    assert first is not None and stale is not None

    first.pages[0].html = "<h1>Newer edit</h1>"
    storage.save_project(first)
    stale.pages[0].html = "<h1>Stale overwrite</h1>"

    with pytest.raises(storage.StaleDesignerProjectError, match="Reload"):
        storage.save_project(stale)

    persisted = storage.load_project(original.id)
    assert persisted is not None
    assert persisted.pages[0].html == "<h1>Newer edit</h1>"


def test_background_thread_uses_bound_project_not_visible_project(
    tmp_path,
    monkeypatch,
):
    storage = _isolate_storage(tmp_path, monkeypatch)
    from row_bot.designer import session
    import row_bot.agent as agent
    import row_bot.threads as threads

    session._active_projects_by_key.clear()
    session._undo_stacks_by_key.clear()
    session._ui_active_key = None
    bound = DesignerProject(id="bound-project", name="Bound")
    visible = DesignerProject(id="visible-project", name="Visible")
    storage.save_project(bound)
    storage.save_project(visible)
    session.set_active_project(visible)
    monkeypatch.setattr(agent, "get_current_thread_id", lambda: "bound-thread")
    monkeypatch.setattr(
        threads,
        "_get_thread_project_id",
        lambda thread_id: "bound-project" if thread_id == "bound-thread" else "",
    )

    active = session.get_active_project()

    assert active is not None
    assert active.id == "bound-project"
    assert session.get_ui_active_project().id == "visible-project"


def test_unbound_agent_thread_never_falls_back_to_visible_project(
    tmp_path,
    monkeypatch,
):
    storage = _isolate_storage(tmp_path, monkeypatch)
    from row_bot.designer import session
    import row_bot.agent as agent
    import row_bot.threads as threads

    session._active_projects_by_key.clear()
    session._undo_stacks_by_key.clear()
    session._ui_active_key = None
    visible = DesignerProject(id="visible-project", name="Visible")
    storage.save_project(visible)
    session.set_active_project(visible)
    monkeypatch.setattr(agent, "get_current_thread_id", lambda: "ordinary-chat-thread")
    monkeypatch.setattr(threads, "_get_thread_project_id", lambda _thread_id: "")

    assert session.get_active_project() is None
    assert session.get_ui_active_project().id == "visible-project"
