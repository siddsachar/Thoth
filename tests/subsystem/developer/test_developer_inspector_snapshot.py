from __future__ import annotations

import time
import asyncio
from dataclasses import replace

import pytest

from tests.fixtures.developer import fake_pending_change, fake_workspace


pytestmark = pytest.mark.subsystem


def test_inspector_snapshot_fingerprint_includes_sandbox_pending_changes(tmp_path) -> None:
    from row_bot.developer.change_ledger import ChangeSet
    from row_bot.developer.devcontainer import DevcontainerInfo
    from row_bot.developer.inspector_snapshot import InspectorSnapshot, _fingerprint_snapshot
    from row_bot.developer.review import DiffStats
    from row_bot.developer.sandbox_runtime import SandboxProbe, SandboxStatus

    workspace = fake_workspace(tmp_path, execution_mode="docker", sandbox_network="ask")
    pending = fake_pending_change(workspace.id)
    base = InspectorSnapshot(
        workspace_id=workspace.id,
        thread_id="thread-1",
        version=0,
        created_at=time.time(),
        workspace=workspace,
        git_summary={"branch": "main"},
        todos=[],
        changed_files=[],
        diff_stats=DiffStats(files=0, additions=0, deletions=0),
        agent_changes=[],
        command_specs=[],
        devcontainer=DevcontainerInfo(present=False),
        sandbox_probe=SandboxProbe(available=True, binary="docker", version="test", message="ok"),
        sandbox_status=SandboxStatus(available=True, backend="docker", running=True, container_name="row-bot-test", image=workspace.sandbox_image),
        sandbox_pending_changes=[pending],
    )
    changed = InspectorSnapshot(
        **{
            **base.__dict__,
            "sandbox_pending_changes": [],
            "agent_changes": [
                ChangeSet(
                    id="change-1",
                    workspace_id=workspace.id,
                    thread_id="thread-1",
                    created_at=time.time(),
                    summary="change",
                    files=[],
                )
            ],
        }
    )

    assert _fingerprint_snapshot(base) != _fingerprint_snapshot(changed)


def _snapshot(tmp_path):
    from row_bot.developer.devcontainer import DevcontainerInfo
    from row_bot.developer.inspector_snapshot import InspectorSnapshot
    from row_bot.developer.sandbox_runtime import SandboxProbe

    workspace = fake_workspace(tmp_path)
    return InspectorSnapshot(
        workspace_id=workspace.id, thread_id="fixture-thread", version=0,
        created_at=0, workspace=workspace, git_summary={}, todos=[],
        changed_files=[], diff_stats=None, agent_changes=[], command_specs=[],
        devcontainer=DevcontainerInfo(present=False), sandbox_probe=SandboxProbe(False),
        sandbox_status=None, sandbox_pending_changes=[],
    )


def test_headless_refresh_coalesces_and_keeps_unchanged_version(tmp_path, monkeypatch):
    from row_bot.developer import inspector_snapshot as owner

    snapshot = _snapshot(tmp_path)
    monkeypatch.setattr(owner, "_snapshots", {})
    monkeypatch.setattr(owner, "_states", {})

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def collect(fn, *args):
            calls.append(args)
            if len(calls) == 1:
                entered.set()
                await release.wait()
            return snapshot

        monkeypatch.setattr(owner.asyncio, "to_thread", collect)
        owner.request_snapshot_refresh(snapshot.workspace_id, snapshot.thread_id, debounce=0)
        await asyncio.wait_for(entered.wait(), 2)
        task = owner._states[(snapshot.workspace_id, snapshot.thread_id)].task
        for _ in range(7):
            owner.request_snapshot_refresh(snapshot.workspace_id, snapshot.thread_id, debounce=0)
        release.set()
        await asyncio.wait_for(task, 2)
        assert len(calls) == 2
        first = owner.get_snapshot(snapshot.workspace_id, snapshot.thread_id)
        owner._store_snapshot(replace(snapshot, created_at=99))
        assert owner.get_snapshot(snapshot.workspace_id, snapshot.thread_id).version == first.version
        await owner.shutdown_snapshot_refreshes()

    asyncio.run(scenario())


def test_clear_invalidates_inflight_collection_even_if_cancellation_is_delayed(tmp_path, monkeypatch):
    from row_bot.developer import inspector_snapshot as owner

    snapshot = _snapshot(tmp_path)
    monkeypatch.setattr(owner, "_snapshots", {})
    monkeypatch.setattr(owner, "_states", {})

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def collect(fn, *args):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                # A filesystem worker may finish after its waiter was cancelled.
                await release.wait()
            return snapshot

        monkeypatch.setattr(owner.asyncio, "to_thread", collect)
        owner.request_snapshot_refresh(snapshot.workspace_id, snapshot.thread_id, debounce=0)
        await asyncio.wait_for(entered.wait(), 2)
        task = owner._states[(snapshot.workspace_id, snapshot.thread_id)].task
        assert owner.clear_thread_snapshots(snapshot.thread_id) == 1
        release.set()
        await asyncio.wait_for(task, 2)
        assert owner.get_snapshot(snapshot.workspace_id, snapshot.thread_id) is None
        assert not owner._states
        await owner.shutdown_snapshot_refreshes()

    asyncio.run(scenario())


def test_refresh_failure_preserves_cache_and_allows_retry(tmp_path, monkeypatch):
    from row_bot.developer import inspector_snapshot as owner

    snapshot = _snapshot(tmp_path)
    monkeypatch.setattr(owner, "_snapshots", {})
    monkeypatch.setattr(owner, "_states", {})
    owner._store_snapshot(snapshot)
    original = owner.get_snapshot(snapshot.workspace_id, snapshot.thread_id)

    async def scenario():
        async def failure(fn, *args):
            raise OSError("fixture unavailable")

        monkeypatch.setattr(owner.asyncio, "to_thread", failure)
        owner.request_snapshot_refresh(snapshot.workspace_id, snapshot.thread_id, debounce=0)
        await owner._states[(snapshot.workspace_id, snapshot.thread_id)].task
        assert owner.get_snapshot(snapshot.workspace_id, snapshot.thread_id) is original

        async def success(fn, *args):
            return replace(snapshot, git_summary={"branch": "fixture"})

        monkeypatch.setattr(owner.asyncio, "to_thread", success)
        owner.request_snapshot_refresh(snapshot.workspace_id, snapshot.thread_id, debounce=0)
        await owner._states[(snapshot.workspace_id, snapshot.thread_id)].task
        assert owner.get_snapshot(snapshot.workspace_id, snapshot.thread_id).version > original.version
        await owner.shutdown_snapshot_refreshes()
        await owner.shutdown_snapshot_refreshes()

    asyncio.run(scenario())
