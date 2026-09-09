from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from threading import Barrier

import pytest

pytestmark = pytest.mark.subsystem


@pytest.fixture
def resources(tmp_path, monkeypatch):
    from row_bot import conversation_resources as service, threads
    from row_bot.designer import storage as artifacts
    from row_bot.designer.state import DesignerProject
    from row_bot.developer import storage as workspaces
    from row_bot.developer.state import DeveloperWorkspace

    monkeypatch.setattr(threads, "DB_PATH", str(tmp_path / "threads.db"))
    threads._ensure_thread_db()
    with sqlite3.connect(threads.DB_PATH) as conn:
        conn.execute("INSERT INTO thread_meta(thread_id, name) VALUES ('conversation', 'Fixture')")
        conn.execute("INSERT INTO thread_meta(thread_id, name) VALUES ('child', 'Child')")
    monkeypatch.setattr(workspaces, "DEVELOPER_DIR", tmp_path / "developer")
    monkeypatch.setattr(workspaces, "WORKSPACES_PATH", tmp_path / "developer" / "workspaces.json")
    monkeypatch.setattr(artifacts, "DESIGNER_DIR", tmp_path / "designer")
    monkeypatch.setattr(artifacts, "PROJECTS_DIR", tmp_path / "designer" / "projects")
    monkeypatch.setattr(artifacts, "ASSETS_DIR", tmp_path / "designer" / "assets")
    monkeypatch.setattr(artifacts, "REFERENCES_DIR", tmp_path / "designer" / "references")
    workspaces.save_workspace(DeveloperWorkspace(id="workspace", name="Workspace", path=str(tmp_path)))
    artifacts.save_project(DesignerProject(id="artifact", name="Artifact"))
    artifacts.save_project(DesignerProject(id="artifact-other", name="Other"))
    return service


def test_workspace_and_artifact_share_revision_and_survive_reopen(resources):
    workspace = resources.bind("conversation", "workspace", "workspace", expected_revision=0)
    result = resources.bind("conversation", "artifact", "artifact", expected_revision=1)
    assert result.revision == result.bindings_revision == "2"
    assert {item.kind for item in result.bindings} == {"workspace", "artifact"}
    assert result.bindings[0] == workspace.bindings[0]
    assert resources.list_bindings("conversation") == result
    assert all(resources.describe(item).available for item in result.bindings)
    assert "path" not in json.dumps([asdict(resources.describe(item)) for item in result.bindings])


def test_same_revision_concurrent_bind_accepts_one(resources):
    barrier = Barrier(2)

    def bind(kind, resource_id):
        barrier.wait()
        try:
            return resources.bind("conversation", kind, resource_id, expected_revision=0)
        except resources.ResourceError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(bind, "workspace", "workspace")
        b = pool.submit(bind, "artifact", "artifact")
        results = [a.result(), b.result()]
    assert results.count("revision_conflict") == 1
    assert len(resources.list_bindings("conversation").bindings) == 1


def test_child_inherits_exact_parent_bindings_and_rejects_stale_parent(resources):
    resources.bind("conversation", "workspace", "workspace", expected_revision=0)
    parent = resources.bind("conversation", "artifact", "artifact", expected_revision=1)
    with pytest.raises(resources.ResourceError, match="revision_conflict"):
        resources.inherit_bindings("conversation", "child", expected_parent_revision=1,
                                   expected_child_revision=0)
    child = resources.inherit_bindings("conversation", "child", expected_parent_revision=2,
                                       expected_child_revision=0)
    assert [(item.kind, item.resource_id) for item in child.bindings] == [
        (item.kind, item.resource_id) for item in parent.bindings]
    assert not {item.binding_id for item in child.bindings} & {item.binding_id for item in parent.bindings}


def test_unbind_preserves_domain_data_and_revokes_captured_context(resources):
    result = resources.bind("conversation", "artifact", "artifact", expected_revision=0)
    binding = result.bindings[0]
    with resources.execution_context("conversation") as context:
        assert context.resolve("artifact") == binding
        resources.unbind("conversation", binding.binding_id, expected_revision=1)
        with pytest.raises(resources.ResourceError, match="resource_binding_revoked"):
            context.resolve("artifact")
    assert resources.describe(binding).available
    assert resources.list_bindings("conversation").bindings == ()


def test_child_inheritance_preserves_allocated_worktree_and_all_artifacts(resources, tmp_path):
    from row_bot import threads
    from row_bot.developer.state import DeveloperWorkspace
    from row_bot.developer.storage import save_workspace

    save_workspace(DeveloperWorkspace(id="worktree", name="Child worktree", path=str(tmp_path)))
    resources.bind("conversation", "workspace", "workspace", role="primary", expected_revision=0)
    resources.bind("conversation", "artifact", "artifact", role="primary", expected_revision=1)
    resources.bind("conversation", "artifact", "artifact-other", role="reference", expected_revision=2)
    with sqlite3.connect(threads.DB_PATH) as conn:
        conn.execute("UPDATE thread_meta SET developer_workspace_id='worktree', "
                     "project_workspace_id='workspace', project_id='artifact' WHERE thread_id='child'")
    original_workspace = next(item for item in resources.list_bindings("child").bindings if item.kind == "workspace")
    child = resources.inherit_bindings("conversation", "child", expected_parent_revision=3,
                                       expected_child_revision=0, preserve_child_workspace=True)
    assert {item.resource_id for item in child.bindings} == {"worktree", "artifact", "artifact-other"}
    assert original_workspace in child.bindings
    assert threads._get_thread_project_workspace("child") == "workspace"
    with resources.execution_context("child") as context:
        assert context.resolve("workspace").resource_id == "worktree"
        assert context.resolve("artifact").resource_id == "artifact"


def test_ambiguous_artifacts_require_explicit_binding_identity(resources):
    result = resources.bind("conversation", "artifact", "artifact", expected_revision=0)
    result = resources.bind("conversation", "artifact", "artifact-other", expected_revision=1)
    with resources.execution_context("conversation") as context:
        with pytest.raises(resources.ResourceError, match="resource_ambiguous"):
            context.resolve("artifact")
    selected = result.bindings[1]
    with resources.execution_context("conversation", binding_ids=(selected.binding_id,)) as context:
        assert context.resolve("artifact") == selected
    assert resources.current_execution_context() is None


def test_captured_empty_context_never_uses_visible_studio(resources, monkeypatch):
    from row_bot.designer import session, storage
    from row_bot.developer import tool_context

    monkeypatch.setattr(session, "_active_projects_by_key", {})
    monkeypatch.setattr(session, "_undo_stacks_by_key", {})
    monkeypatch.setattr(session, "_ui_active_key", "visible")
    session._active_projects_by_key["visible"] = storage.load_project("artifact")
    tokens = tool_context.set_context(workspace_id="visible-unrelated", thread_id="conversation")
    try:
        with resources.execution_context("conversation"):
            assert session.get_active_project() is None
            assert tool_context.get_workspace_id() == ""
        assert session.get_ui_active_project().id == "artifact"
    finally:
        tool_context.reset_context(tokens)


def test_bound_artifact_overrides_visible_studio(resources, monkeypatch):
    from row_bot.designer import session, storage

    monkeypatch.setattr(session, "_active_projects_by_key", {})
    monkeypatch.setattr(session, "_undo_stacks_by_key", {})
    monkeypatch.setattr(session, "_ui_active_key", "visible")
    session._active_projects_by_key["visible"] = storage.load_project("artifact-other")
    resources.bind("conversation", "artifact", "artifact", expected_revision=0)
    with resources.execution_context("conversation"):
        assert session.get_active_project().id == "artifact"
    assert session.get_ui_active_project().id == "artifact-other"


@pytest.mark.parametrize("bound", [False, True])
def test_actual_child_worker_resolves_its_resources_without_visible_studio(resources, monkeypatch, tmp_path, bound):
    from row_bot import agent_profiles, agent_runner, tasks
    from row_bot.designer import session, storage
    from row_bot.developer import tool_context
    from row_bot.runtime.executions import GenerationRuntimeRegistry

    # This actual child producer also consumes profiles/runs in tasks.db. Keep
    # that owner private, including its independent schema-ready cache.
    monkeypatch.setattr(tasks, "_DB_PATH", str(tmp_path / "tasks.db"))
    monkeypatch.setattr(agent_profiles, "_SCHEMA_READY", False)
    monkeypatch.setattr(agent_runner, "generation_registry", GenerationRuntimeRegistry())
    monkeypatch.setattr(session, "_active_projects_by_key", {})
    monkeypatch.setattr(session, "_undo_stacks_by_key", {})
    monkeypatch.setattr(session, "_ui_active_key", "visible")
    session._active_projects_by_key["visible"] = storage.load_project("artifact-other")
    if bound:
        resources.bind("conversation", "workspace", "workspace", expected_revision=0)
        resources.bind("conversation", "artifact", "artifact", expected_revision=1)
    observed = []
    def invoke(prompt, tools, config, *, stop_event):
        context = resources.current_execution_context()
        assert context is not None
        assert context.conversation_id == config["configurable"]["thread_id"]
        project = session.get_active_project()
        observed.append((project.id if project else None, tool_context.get_workspace_id()))
        return "Synthetic resource inspection complete"
    monkeypatch.setattr(agent_runner, "_invoke_agent", invoke)
    run = agent_runner.spawn_agent_run("Inspect synthetic resources", parent_thread_id="conversation",
        profile="quality_reviewer", enabled_tool_names=[], wait=True, timeout=10)
    assert run["status"] == "completed"
    assert observed == ([("artifact", "workspace")] if bound else [(None, "")])
    assert session.get_ui_active_project().id == "artifact-other"
    assert resources.current_execution_context() is None


def test_legacy_relationships_have_stable_ids_without_query_migration(resources):
    from row_bot import threads

    with sqlite3.connect(threads.DB_PATH) as conn:
        conn.execute("UPDATE thread_meta SET developer_workspace_id='workspace', project_id='artifact' "
                     "WHERE thread_id='conversation'")
    first = resources.list_bindings("conversation")
    assert first == resources.list_bindings("conversation")
    assert {item.kind for item in first.bindings} == {"workspace", "artifact"}
    with sqlite3.connect(threads.DB_PATH) as conn:
        assert conn.execute("SELECT resource_bindings_json FROM thread_meta WHERE thread_id='conversation'").fetchone()[0] == ""
    workspace = next(item for item in first.bindings if item.kind == "workspace")
    remaining = resources.unbind("conversation", workspace.binding_id, expected_revision=0)
    assert remaining.bindings == tuple(item for item in first.bindings if item.kind != "workspace")
    assert threads._get_thread_developer_workspace("conversation") == ""
    assert threads._get_thread_project_workspace("conversation") == ""


@pytest.mark.parametrize("identifier", ["../artifact", "..", "a/b", "a\\b", "C:artifact", "a\x00b"])
def test_resource_path_inputs_are_not_opaque_ids(resources, identifier):
    with pytest.raises(resources.ResourceError, match="invalid_resource"):
        resources.bind("conversation", "artifact", identifier, expected_revision=0)


def test_resource_source_revision_and_unavailability(resources):
    from row_bot.designer import storage

    with pytest.raises(resources.ResourceError, match="resource_revision_conflict"):
        resources.bind("conversation", "artifact", "artifact", expected_revision=0,
                       expected_resource_revision="stale")
    result = resources.bind("conversation", "artifact", "artifact", expected_revision=0)
    # Remove only this fixture's exact file; no resource deletion/migration routine.
    (storage.PROJECTS_DIR / "artifact.json").unlink()
    assert resources.list_bindings("conversation") == result
    assert not resources.describe(result.bindings[0]).available
    with resources.execution_context("conversation") as context:
        with pytest.raises(resources.ResourceError, match="resource_unavailable"):
            context.resolve("artifact")


def test_resource_queries_do_not_create_domain_directories(tmp_path, monkeypatch):
    from row_bot.designer import storage as artifacts
    from row_bot.developer import storage as workspaces

    missing = tmp_path / "absent"
    monkeypatch.setattr(workspaces, "DEVELOPER_DIR", missing / "developer")
    monkeypatch.setattr(workspaces, "WORKSPACES_PATH", missing / "developer" / "workspaces.json")
    monkeypatch.setattr(artifacts, "PROJECTS_DIR", missing / "designer" / "projects")
    assert workspaces.get_workspace("missing") is None
    assert artifacts.get_project_metadata("missing") is None
    assert not missing.exists()
