from __future__ import annotations

import json
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace

import pytest

from tests.subsystem.plugins.conftest import write_plugin

pytestmark = pytest.mark.subsystem


@pytest.mark.parametrize("mode", ["conflict", "transitive", "timeout", "failure", "missing", "malformed"])
def test_dependency_plan_fails_closed_before_install(monkeypatch, mode):
    from row_bot.plugins import sandbox
    monkeypatch.setattr(sandbox, "_get_core_requirements", lambda: {"host_package": "1.0"})
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        assert "--dry-run" in argv
        assert "host-package==1.0" in Path(argv[argv.index("--constraint") + 1]).read_text()
        if mode == "timeout":
            raise subprocess.TimeoutExpired(argv, 60)
        if mode == "failure":
            return SimpleNamespace(returncode=1)
        report = Path(argv[argv.index("--report") + 1])
        if mode != "missing":
            data = {"version": "1", "install": [{"metadata": {
                "name": "host-package" if mode == "conflict" else "addon", "version": "2.0",
                "requires_dist": ["host-package>=2"] if mode == "transitive" else []}}]}
            report.write_text("invalid" if mode == "malformed" else json.dumps(data))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(sandbox.subprocess, "run", run)
    ok, _ = sandbox.install_dependencies(["addon"])
    assert not ok and len(calls) == 1


def test_dependency_install_reuses_verified_complete_constraints(monkeypatch):
    from row_bot.plugins import sandbox
    monkeypatch.setattr(sandbox, "_get_core_requirements", lambda: {"host_package": "1.0"})
    calls = []
    def run(argv, **kwargs):
        pins = Path(argv[argv.index("--constraint") + 1]).read_text()
        calls.append(pins)
        if "--dry-run" in argv:
            Path(argv[argv.index("--report") + 1]).write_text(json.dumps({"version": "1", "install": [
                {"metadata": {"name": "addon", "version": "2.0", "requires_dist": ["host-package==1.0"]}}]}))
        else:
            assert "--no-deps" in argv and "addon==2.0" in argv
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(sandbox.subprocess, "run", run)
    assert sandbox.install_dependencies(["addon"])[0]
    assert len(calls) == 2 and "addon==2.0" in calls[1] and "host-package==1.0" in calls[1]


def test_late_api_registration_and_webhook_are_revoked(plugin_modules, tmp_path):
    from row_bot.plugins.api import PluginAPI
    plugin_modules["state"].set_plugin_enabled("fixture", True)
    api = PluginAPI("fixture", tmp_path, plugin_modules["state"], staged=True)
    api.register_webhook_route("fixture", lambda request: None)
    with pytest.raises(RuntimeError, match="not published"):
        api._check_dispatch()
    assert not plugin_modules["webhooks"]._webhooks
    api._revoke()
    with pytest.raises(RuntimeError):
        api.register_skill({"name": "late"})
    with pytest.raises(RuntimeError):
        api.register_webhook_route("late", lambda request: None)
    assert not api._registered_webhooks


def test_disable_then_reenable_does_not_restore_registration_epoch(plugin_modules, tmp_path):
    from row_bot.plugins.api import PluginAPI
    state, loader = plugin_modules["state"], plugin_modules["loader"]
    state.set_plugin_enabled("fixture", True)
    old = PluginAPI("fixture", tmp_path, state, staged=True)
    loader._registrations["fixture"] = old
    state.set_plugin_enabled("fixture", False)
    state.set_plugin_enabled("fixture", True)
    with pytest.raises(RuntimeError):
        old.register_skill({"name": "late"})
    replacement = PluginAPI("fixture", tmp_path, state, staged=True)
    loader._registrations["fixture"] = replacement
    loader._cleanup_plugin_runtime("fixture", expected_api=old)
    assert loader._registrations["fixture"] is replacement
    replacement.register_skill({"name": "current"})


def test_replacement_cannot_publish_between_revocation_and_contribution_cleanup(plugin_modules, tmp_path, monkeypatch):
    from row_bot.plugins.api import PluginAPI
    state, loader, registry = (plugin_modules[k] for k in ("state", "loader", "registry"))
    state.set_plugin_enabled("fixture", True)
    old = PluginAPI("fixture", tmp_path, state, staged=True)
    loader._registrations["fixture"] = old
    observed = []
    original = registry.unregister_plugin
    def cleanup(plugin_id):
        def replacement_probe():
            acquired = loader._registration_lock.acquire(blocking=False)
            observed.append(acquired)
            if acquired:
                loader._registration_lock.release()
        worker = threading.Thread(target=replacement_probe)
        worker.start()
        worker.join(5)
        assert not worker.is_alive()
        original(plugin_id)
    monkeypatch.setattr(registry, "unregister_plugin", cleanup)
    loader._cleanup_plugin_runtime("fixture", expected_api=old)
    assert observed == [False]
    assert old._registration_revoked and "fixture" not in loader._registrations


def test_bound_plugin_tool_revoked_by_disable_and_reload(plugin_modules, tmp_path):
    plugin = write_plugin(tmp_path)
    loader, registry, state = (plugin_modules[k] for k in ("loader", "registry", "state"))
    state.set_plugin_enabled("sample-plugin", True)
    assert loader._load_single_plugin(plugin).success
    bound = registry.get_langchain_tools(refresh_mcp=False)[0]
    assert bound.invoke({"query": "fixture"}) == "sample:fixture"
    state.set_plugin_enabled("sample-plugin", False)
    with pytest.raises(RuntimeError, match="revoked"):
        bound.invoke({"query": "fixture"})
    state.set_plugin_enabled("sample-plugin", True)
    loader._cleanup_plugin_runtime("sample-plugin")
    assert loader._load_single_plugin(plugin).success
    with pytest.raises(RuntimeError, match="revoked"):
        bound.invoke({"query": "fixture"})


def test_registration_thread_after_timeout_cannot_publish(plugin_modules, tmp_path, monkeypatch):
    import sys
    from row_bot.plugins.api import PluginAPI
    state, loader, webhooks = (plugin_modules[k] for k in ("state", "loader", "webhooks"))
    state.set_plugin_enabled("fixture", True)
    api = PluginAPI("fixture", tmp_path, state, staged=True)
    started, release, ended = threading.Event(), threading.Event(), threading.Event()
    def late(api):
        started.set()
        assert release.wait(5)
        try:
            api.register_webhook_route("late", lambda request: None)
        finally:
            ended.set()
    monkeypatch.setitem(sys.modules, "fixture_registration_barrier", SimpleNamespace(late=late))
    (tmp_path / "plugin_main.py").write_text("from fixture_registration_barrier import late\ndef register(api): late(api)\n")
    real_thread = threading.Thread
    owned = []
    def injected_thread(*args, **kwargs):
        worker = real_thread(*args, **kwargs)
        original_join = worker.join
        owned.append((worker, original_join))
        worker.join = lambda timeout=None: started.wait(5)
        return worker
    monkeypatch.setattr(loader.threading, "Thread", injected_thread)
    try:
        with pytest.raises(TimeoutError):
            loader._call_register_with_timeout(tmp_path, api)
    finally:
        release.set()
        for worker, join in owned:
            join(5)
            assert not worker.is_alive()
    assert ended.is_set()
    assert not webhooks._webhooks
    assert api._registration_revoked
