import importlib
import contextvars
import pathlib
import sqlite3
import sys
import types

import pytest

import row_bot.api_keys as api_keys
import row_bot.providers.config as provider_config
from row_bot.providers.selection import (
    add_quick_choice_for_model,
    canonicalize_model_selection,
    ModelSelectionError,
    model_selection_diagnostics,
    resolve_selection,
)


def _isolated_provider_config(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(api_keys, "get_cloud_config", lambda: {"starred_models": []})
    provider_config.save_provider_config({
        "custom_endpoints": [{
            "id": "lmstudio",
            "name": "LM Studio",
            "base_url": "http://127.0.0.1:1234/v1",
            "auth_required": False,
            "models": [{"model_id": "qwen3.6-35b-a3b"}],
        }],
    })


def _isolated_tasks_module(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    import row_bot.tasks as tasks
    import row_bot.threads as threads

    monkeypatch.setattr(threads, "DB_PATH", str(tmp_path / "data" / "threads.db"))
    return importlib.reload(tasks)


def test_phase0_diagnostics_reports_full_custom_provider_ref(tmp_path, monkeypatch):
    _isolated_provider_config(tmp_path, monkeypatch)
    ref = "model:custom_openai_lmstudio:qwen3.6-35b-a3b"

    diagnostics = model_selection_diagnostics(
        ref,
        runtime_surface="workflow",
        runtime_mode="agent",
        tools_bound=True,
    )

    assert diagnostics["raw_stored_model_override"] == ref
    assert diagnostics["selection_ref"] == ref
    assert diagnostics["provider_id"] == "custom_openai_lmstudio"
    assert diagnostics["runtime_model"] == "qwen3.6-35b-a3b"
    assert diagnostics["runtime_surface"] == "workflow"
    assert diagnostics["runtime_mode"] == "agent"
    assert diagnostics["tools_bound"] is True


def test_phase0_full_workflow_override_resolves_to_custom_provider(tmp_path, monkeypatch):
    _isolated_provider_config(tmp_path, monkeypatch)

    resolved = resolve_selection("model:custom_openai_lmstudio:qwen3.6-35b-a3b")

    assert resolved is not None
    assert resolved.ref == "model:custom_openai_lmstudio:qwen3.6-35b-a3b"
    assert resolved.provider_id == "custom_openai_lmstudio"
    assert resolved.model_id == "qwen3.6-35b-a3b"


def test_phase0_bare_custom_looking_model_currently_resolves_to_ollama(tmp_path, monkeypatch):
    _isolated_provider_config(tmp_path, monkeypatch)

    resolved = resolve_selection("qwen3.6-35b-a3b")

    assert resolved is not None
    assert resolved.ref == "model:ollama:qwen3.6-35b-a3b"
    assert resolved.provider_id == "ollama"


def test_phase3_telegram_model_command_stores_canonical_ref():
    source = pathlib.Path("src/row_bot/channels/telegram.py").read_text(encoding="utf-8")

    assert "canonicalize_model_selection(model_id, \"channels\")" in source
    assert 'config["configurable"]["model_override"] = canonical.ref' in source
    assert "_set_thread_model_override(tid, canonical.ref)" in source
    assert 'config["configurable"]["model_override"] = model_id' not in source
    assert "visible_choices = choices[:12]" in source


def test_phase3_telegram_thread_reload_keeps_full_model_ref(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "threads"))
    import row_bot.threads as threads

    threads = importlib.reload(threads)
    ref = "model:custom_openai_lmstudio:qwen3.6-35b-a3b"
    threads._save_thread_meta("tg_123_abc", "Telegram 123")
    threads._set_thread_model_override("tg_123_abc", ref)

    listed = threads._list_threads()
    row = next(row for row in listed if row[0] == "tg_123_abc")

    assert row[4] == ref


def test_telegram_refresh_thread_model_override_syncs_cached_config(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "threads"))
    import row_bot.threads as threads
    from row_bot.channels.telegram import _refresh_thread_model_override

    threads = importlib.reload(threads)
    thread_id = "tg_123_abc"
    threads._save_thread_meta(thread_id, "Telegram 123")
    threads._set_thread_model_override(thread_id, "model:openai:gpt-4o-mini")

    refreshed = _refresh_thread_model_override({"configurable": {"thread_id": thread_id}})

    assert refreshed["configurable"]["model_override"] == "model:openai:gpt-4o-mini"

    threads._set_thread_model_override(thread_id, "")
    refreshed = _refresh_thread_model_override({
        "configurable": {
            "thread_id": thread_id,
            "model_override": "model:openai:gpt-4o-mini",
        }
    })

    assert "model_override" not in refreshed["configurable"]


def test_telegram_message_flow_refreshes_cached_model_override_before_agent_run():
    source = pathlib.Path("src/row_bot/channels/telegram.py").read_text(encoding="utf-8")
    message_flow = source.split("async def _run_agent_for_message", 1)[1].split(
        "executor_future = loop.run_in_executor", 1
    )[0]

    assert "config = _refresh_thread_model_override(config)" in message_flow
    assert 'context.chat_data["thread_config"] = config' in message_flow


def test_telegram_approval_resume_refreshes_cached_model_override_before_agent_run():
    source = pathlib.Path("src/row_bot/channels/telegram.py").read_text(encoding="utf-8")
    callback_flow = source.split('approved = query.data == "interrupt_approve"', 1)[1].split(
        "interrupt_ids = _extract_interrupt_ids", 1
    )[0]

    assert 'config = _refresh_thread_model_override(pending["config"])' in callback_flow
    assert 'context.chat_data["thread_config"] = config' in callback_flow


def test_phase0_workflow_delivery_status_is_separate_from_routing(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path))
    import row_bot.tasks as tasks

    tasks = importlib.reload(tasks)
    task = {"name": "Delivery Only", "delivery_channel": "", "channels": []}

    assert tasks._deliver_to_channels(task, "done") == ("", "")


def test_phase1_full_custom_ref_stays_full_ref(tmp_path, monkeypatch):
    _isolated_provider_config(tmp_path, monkeypatch)

    canonical = canonicalize_model_selection(
        "model:custom_openai_lmstudio:qwen3.6-35b-a3b",
        "workflow",
    )

    assert canonical.ref == "model:custom_openai_lmstudio:qwen3.6-35b-a3b"
    assert canonical.provider_id == "custom_openai_lmstudio"
    assert canonical.model_id == "qwen3.6-35b-a3b"


def test_phase1_quick_choice_display_canonicalizes_to_full_ref(tmp_path, monkeypatch):
    _isolated_provider_config(tmp_path, monkeypatch)
    add_quick_choice_for_model(
        "qwen3.6-35b-a3b",
        provider_id="custom_openai_lmstudio",
        display_name="Qwen Local",
    )

    canonical = canonicalize_model_selection("Qwen Local", "workflow")

    assert canonical.ref == "model:custom_openai_lmstudio:qwen3.6-35b-a3b"
    assert canonical.source == "quick_choice"


def test_phase1_unique_custom_bare_model_canonicalizes_to_full_ref(tmp_path, monkeypatch):
    _isolated_provider_config(tmp_path, monkeypatch)

    canonical = canonicalize_model_selection("qwen3.6-35b-a3b", "workflow")

    assert canonical.ref == "model:custom_openai_lmstudio:qwen3.6-35b-a3b"
    assert canonical.source == "custom_endpoint_model"


def test_phase1_ambiguous_bare_model_fails_clearly(tmp_path, monkeypatch):
    _isolated_provider_config(tmp_path, monkeypatch)
    cfg = provider_config.load_provider_config()
    cfg["custom_endpoints"].append({
        "id": "llamacpp",
        "name": "llama.cpp",
        "base_url": "http://127.0.0.1:8080/v1",
        "auth_required": False,
        "models": [{"model_id": "qwen3.6-35b-a3b"}],
    })
    provider_config.save_provider_config(cfg)

    with pytest.raises(ModelSelectionError, match="Ambiguous model selection"):
        canonicalize_model_selection("qwen3.6-35b-a3b", "workflow")


def test_phase1_unknown_workflow_bare_model_fails_clearly(tmp_path, monkeypatch):
    _isolated_provider_config(tmp_path, monkeypatch)

    with pytest.raises(ModelSelectionError, match="Cannot infer a provider"):
        canonicalize_model_selection("totally-unknown-local-name", "workflow")


def test_phase2_create_task_stores_full_custom_ref(tmp_path, monkeypatch):
    _isolated_provider_config(tmp_path, monkeypatch)
    tasks = _isolated_tasks_module(tmp_path, monkeypatch)

    task_id = tasks.create_task(
        "Full ref workflow",
        prompts=["say hi"],
        model_override="model:custom_openai_lmstudio:qwen3.6-35b-a3b",
    )

    assert tasks.get_task(task_id)["model_override"] == "model:custom_openai_lmstudio:qwen3.6-35b-a3b"


def test_phase2_create_task_canonicalizes_unique_custom_bare_model(tmp_path, monkeypatch):
    _isolated_provider_config(tmp_path, monkeypatch)
    tasks = _isolated_tasks_module(tmp_path, monkeypatch)

    task_id = tasks.create_task(
        "Bare custom workflow",
        prompts=["say hi"],
        model_override="qwen3.6-35b-a3b",
    )

    assert tasks.get_task(task_id)["model_override"] == "model:custom_openai_lmstudio:qwen3.6-35b-a3b"


def test_phase2_update_task_canonicalizes_model_override(tmp_path, monkeypatch):
    _isolated_provider_config(tmp_path, monkeypatch)
    tasks = _isolated_tasks_module(tmp_path, monkeypatch)
    task_id = tasks.create_task("Update workflow", prompts=["say hi"])

    tasks.update_task(task_id, model_override="qwen3.6-35b-a3b")

    assert tasks.get_task(task_id)["model_override"] == "model:custom_openai_lmstudio:qwen3.6-35b-a3b"


def test_phase2_step_level_model_override_canonicalizes(tmp_path, monkeypatch):
    _isolated_provider_config(tmp_path, monkeypatch)
    tasks = _isolated_tasks_module(tmp_path, monkeypatch)

    task_id = tasks.create_task(
        "Step model workflow",
        steps=[{"type": "prompt", "prompt": "say hi", "model_override": "qwen3.6-35b-a3b"}],
    )

    assert tasks.get_task(task_id)["steps"][0]["model_override"] == "model:custom_openai_lmstudio:qwen3.6-35b-a3b"


def test_phase2_ambiguous_bare_workflow_model_fails_on_save(tmp_path, monkeypatch):
    _isolated_provider_config(tmp_path, monkeypatch)
    cfg = provider_config.load_provider_config()
    cfg["custom_endpoints"].append({
        "id": "llamacpp",
        "name": "llama.cpp",
        "base_url": "http://127.0.0.1:8080/v1",
        "auth_required": False,
        "models": [{"model_id": "qwen3.6-35b-a3b"}],
    })
    provider_config.save_provider_config(cfg)
    tasks = _isolated_tasks_module(tmp_path, monkeypatch)

    with pytest.raises(ModelSelectionError, match="Ambiguous model selection"):
        tasks.create_task(
            "Ambiguous workflow",
            prompts=["say hi"],
            model_override="qwen3.6-35b-a3b",
        )


def test_phase4_explicit_workflow_model_failure_does_not_pop_override(tmp_path, monkeypatch):
    _isolated_provider_config(tmp_path, monkeypatch)
    tasks = _isolated_tasks_module(tmp_path, monkeypatch)
    ref = "model:custom_openai_lmstudio:qwen3.6-35b-a3b"
    message = tasks._workflow_model_failure_message(ref, RuntimeError("model failed to load"))

    assert "not retrying with the default provider" in message
    assert f"selected_ref={ref}" in message
    assert "provider_id=custom_openai_lmstudio" in message
    assert "runtime_model=qwen3.6-35b-a3b" in message

    source = pathlib.Path("src/row_bot/tasks.py").read_text(encoding="utf-8")
    assert '.pop("model_override")' not in source
    assert ".pop('model_override')" not in source


def test_phase4_failed_custom_workflow_does_not_retry_default_provider(tmp_path, monkeypatch):
    _isolated_provider_config(tmp_path, monkeypatch)
    tasks = _isolated_tasks_module(tmp_path, monkeypatch)
    ref = "model:custom_openai_lmstudio:qwen3.6-35b-a3b"
    task_id = tasks.create_task(
        "No fallback workflow",
        prompts=["say hi"],
        model_override=ref,
    )
    thread_id = tasks._prepare_task_thread(tasks.get_task(task_id))
    calls = []

    class _Var:
        def set(self, value):
            return None

    fake_agent = types.SimpleNamespace(
        TaskStoppedError=type("TaskStoppedError", (Exception,), {}),
        RECURSION_LIMIT_TASK=50,
        _background_workflow_var=_Var(),
        _approval_mode_var=_Var(),
        _persistent_thread_var=_Var(),
        repair_orphaned_tool_calls=lambda *_args, **_kwargs: None,
    )

    def _invoke_agent(_prompt, _tools, config, stop_event=None):
        from row_bot.conversation_resources import current_execution_context
        assert current_execution_context().conversation_id == thread_id
        calls.append((config.get("configurable") or {}).get("model_override"))
        raise RuntimeError("model failed to load")

    fake_agent.invoke_agent = _invoke_agent
    monkeypatch.setitem(sys.modules, "agent", fake_agent)
    monkeypatch.setitem(sys.modules, "row_bot.agent", fake_agent)

    class _ImmediateThread:
        def __init__(self, target, *args, **kwargs):
            self._target = target

        def start(self):
            contextvars.Context().run(self._target)

    monkeypatch.setattr(tasks.threading, "Thread", _ImmediateThread)

    tasks.run_task_background(task_id, thread_id, [], notification=False)
    runs = tasks.get_recent_runs(1)

    assert calls == [ref]
    assert runs[0]["status"] == "failed"
    assert ref in runs[0]["status_message"]
    assert "not retrying with the default provider" in runs[0]["status_message"]


def test_phase5_delivery_failure_is_separate_from_execution_success(tmp_path, monkeypatch):
    _isolated_provider_config(tmp_path, monkeypatch)
    tasks = _isolated_tasks_module(tmp_path, monkeypatch)
    task_id = tasks.create_task(
        "Delivery failure workflow",
        prompts=["say hi"],
        channels=["slack"],
    )
    thread_id = tasks._prepare_task_thread(tasks.get_task(task_id))
    calls = []

    def _invoke_agent(*_args, **_kwargs):
        from row_bot.conversation_resources import current_execution_context
        assert current_execution_context().conversation_id == thread_id
        calls.append("execution")
        return "execution ok"

    class _Var:
        def set(self, value):
            return None

    fake_agent = types.SimpleNamespace(
        TaskStoppedError=type("TaskStoppedError", (Exception,), {}),
        RECURSION_LIMIT_TASK=50,
        _background_workflow_var=_Var(),
        _approval_mode_var=_Var(),
        _persistent_thread_var=_Var(),
        invoke_agent=_invoke_agent,
        repair_orphaned_tool_calls=lambda *_args, **_kwargs: None,
    )
    monkeypatch.setitem(sys.modules, "agent", fake_agent)
    monkeypatch.setitem(sys.modules, "row_bot.agent", fake_agent)

    class _ImmediateThread:
        def __init__(self, target, *args, **kwargs):
            self._target = target

        def start(self):
            contextvars.Context().run(self._target)

    class _FailingChannel:
        name = "slack"
        display_name = "Slack"

        def get_default_target(self):
            return "chat"

        def send_message(self, target, text):
            assert "execution ok" in text
            calls.append("delivery")
            raise RuntimeError("telegram unavailable")

    monkeypatch.setattr(tasks.threading, "Thread", _ImmediateThread)
    monkeypatch.setattr(tasks, "get_task_channels", lambda _task: [_FailingChannel()])

    tasks.run_task_background(task_id, thread_id, [], notification=False)
    run = tasks.get_recent_runs(1)[0]

    assert run["status"] == "completed_delivery_failed"
    assert calls == ["execution", "delivery"]
    assert "Slack: telegram unavailable" in run["status_message"]
    assert tasks._workflow_final_status_for_delivery("delivery_failed") == "completed_delivery_failed"


def test_phase6_shared_model_command_is_provider_aware_and_read_only(tmp_path, monkeypatch):
    _isolated_provider_config(tmp_path, monkeypatch)
    add_quick_choice_for_model(
        "qwen3.6-35b-a3b",
        provider_id="custom_openai_lmstudio",
        display_name="Qwen Local",
    )
    import row_bot.channels.commands as commands

    response = commands.cmd_model("Slack", "Qwen Local")

    assert "model:custom_openai_lmstudio:qwen3.6-35b-a3b" in response
    assert "cannot persist per-conversation model overrides" in response


def test_phase6_shared_model_command_ambiguous_bare_fails_clearly(tmp_path, monkeypatch):
    _isolated_provider_config(tmp_path, monkeypatch)
    cfg = provider_config.load_provider_config()
    cfg["custom_endpoints"].append({
        "id": "llamacpp",
        "name": "llama.cpp",
        "base_url": "http://127.0.0.1:8080/v1",
        "auth_required": False,
        "models": [{"model_id": "qwen3.6-35b-a3b"}],
    })
    provider_config.save_provider_config(cfg)
    import row_bot.channels.commands as commands

    response = commands.cmd_model("Discord", "qwen3.6-35b-a3b")

    assert "Ambiguous model selection" in response
    assert "model:custom_openai_lmstudio:qwen3.6-35b-a3b" in response
    assert "model:custom_openai_llamacpp:qwen3.6-35b-a3b" in response


def test_phase7_telegram_message_runtime_uses_channel_auto():
    source = pathlib.Path("src/row_bot/channels/telegram.py").read_text(encoding="utf-8")

    assert "def build_channel_runtime_config" in source
    assert 'build_channel_runtime_config(config, "message")' in source
    run_body = source.split("def _run_agent_sync", 1)[1].split("def _resume_agent_sync", 1)[0]
    assert '"runtime_surface": "approval"' not in run_body
    assert '"runtime_mode": "agent"' not in run_body


def test_phase7_telegram_approval_resume_uses_approval_agent():
    source = pathlib.Path("src/row_bot/channels/telegram.py").read_text(encoding="utf-8")
    resume_body = source.split("def _resume_agent_sync", 1)[1].split("async def _send_html", 1)[0]

    assert 'build_channel_runtime_config(config, "approval")' in resume_body
    helper_body = source.split("def build_channel_runtime_config", 1)[1].split("def _run_agent_sync", 1)[0]
    assert 'runtime_surface = "approval"' in helper_body
    assert 'runtime_mode = "agent"' in helper_body


@pytest.mark.parametrize(
    "path",
    [
        "src/row_bot/channels/slack.py",
        "src/row_bot/channels/discord_channel.py",
        "src/row_bot/channels/whatsapp.py",
        "src/row_bot/channels/sms.py",
    ],
)
def test_phase8_other_channel_message_runtime_uses_channel_auto(path):
    source = pathlib.Path(path).read_text(encoding="utf-8")

    assert "def build_channel_runtime_config" in source
    assert 'build_channel_runtime_config(config, "message")' in source
    run_body = source.split("def _run_agent_sync", 1)[1].split("def _resume_agent_sync", 1)[0]
    assert '"runtime_surface": "approval"' not in run_body
    assert '"runtime_mode": "agent"' not in run_body


@pytest.mark.parametrize(
    "path",
    [
        "src/row_bot/channels/slack.py",
        "src/row_bot/channels/discord_channel.py",
        "src/row_bot/channels/whatsapp.py",
        "src/row_bot/channels/sms.py",
    ],
)
def test_phase8_other_channel_approval_runtime_stays_agent(path):
    source = pathlib.Path(path).read_text(encoding="utf-8")
    helper_body = source.split("def build_channel_runtime_config", 1)[1].split("def _run_agent_sync", 1)[0]
    resume_body = source.split("def _resume_agent_sync", 1)[1]

    assert 'runtime_surface = "approval"' in helper_body
    assert 'runtime_mode = "agent"' in helper_body
    assert 'build_channel_runtime_config(config, "approval")' in resume_body


def test_phase9_legacy_model_diagnostics_are_read_only(tmp_path, monkeypatch):
    _isolated_provider_config(tmp_path, monkeypatch)
    tasks = _isolated_tasks_module(tmp_path, monkeypatch)
    task_id = tasks.create_task("Legacy task", prompts=["hi"])
    db_path = pathlib.Path(tmp_path / "data" / "tasks.db")
    legacy_steps = '[{"id":"step_1","type":"prompt","prompt":"hi","model_override":"missing-model"}]'
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE tasks SET model_override = ?, steps = ? WHERE id = ?",
            ("qwen3.6-35b-a3b", legacy_steps, task_id),
        )
        conn.commit()

    import row_bot.threads as threads

    threads = importlib.reload(threads)
    threads._save_thread_meta("tg_legacy", "Telegram legacy")
    threads._set_thread_model_override("tg_legacy", "qwen3.6-35b-a3b")

    diagnostics = tasks.diagnose_legacy_model_overrides()

    assert {
        (item["scope"], item["raw_value"], item["status"])
        for item in diagnostics
    } >= {
        ("task", "qwen3.6-35b-a3b", "canonicalizable"),
        ("task_step", "missing-model", "unknown"),
        ("thread", "qwen3.6-35b-a3b", "canonicalizable"),
    }
    assert tasks.get_task(task_id)["model_override"] == "qwen3.6-35b-a3b"
    assert threads._get_thread_model_override("tg_legacy") == "qwen3.6-35b-a3b"
