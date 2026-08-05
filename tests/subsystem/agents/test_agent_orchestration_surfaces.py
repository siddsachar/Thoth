from __future__ import annotations

import asyncio
import importlib
import sys

import pytest


pytestmark = pytest.mark.subsystem


def _fresh_modules(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(data_dir))
    for name in (
        "row_bot.tasks",
        "row_bot.agent_profiles",
        "row_bot.agent_settings",
        "row_bot.agent_runs",
        "row_bot.agent_orchestrator",
    ):
        sys.modules.pop(name, None)
    import row_bot.tasks as tasks
    import row_bot.agent_runs as agent_runs
    import row_bot.agent_orchestrator as orchestrator

    importlib.reload(tasks)
    agent_runs = importlib.reload(agent_runs)
    orchestrator = importlib.reload(orchestrator)
    return agent_runs, orchestrator


def test_acknowledgement_and_final_delivery_are_idempotent_without_polling(
    tmp_path,
    monkeypatch,
):
    agent_runs, orchestrator = _fresh_modules(tmp_path, monkeypatch)
    orchestration = orchestrator.create_or_get_orchestration(
        parent_thread_id="channel-thread",
        parent_generation_id="channel-generation",
        root_objective="Research two independent facts",
        model_ref="provider:model",
        approval_mode="approve",
        runtime_surface="channel",
    )
    child = agent_runs.create_agent_run(
        run_id="child",
        status="running",
        parent_thread_id="channel-thread",
        prompt="Research fact",
        model_override="provider:model",
    )
    orchestrator.register_member(orchestration["id"], child["id"], required=True)
    deliveries: list[tuple[str, str, str]] = []
    orchestrator.set_test_executors(
        synthesis=lambda _row, _prompt: "One final response",
        delivery=lambda _row, kind, text, key: deliveries.append((kind, text, key)) or True,
    )

    for _ in range(2):
        assert orchestrator.finalize_parent_generation(
            orchestration["id"],
            continuation_state={"config": {"configurable": {}}, "enabled_tool_names": []},
        )
    agent_runs.finish_agent_run(child["id"], "completed", summary="Fact")
    orchestrator.handle_run_terminal(child["id"])
    orchestrator.wait_for_synthesis(orchestration["id"])
    orchestrator.retry_pending_deliveries()

    assert [kind for kind, _text, _key in deliveries] == [
        "acknowledgement",
        "final",
    ]
    assert len({key for _kind, _text, key in deliveries}) == 2


def test_local_chat_delivery_is_complete_without_a_channel_binding(
    tmp_path,
    monkeypatch,
):
    agent_runs, orchestrator = _fresh_modules(tmp_path, monkeypatch)
    sys.modules.pop("row_bot.threads", None)
    import row_bot.threads as threads

    threads = importlib.reload(threads)
    parent_thread_id = threads.create_thread("Local orchestration")
    orchestration = orchestrator.create_or_get_orchestration(
        parent_thread_id=parent_thread_id,
        parent_generation_id="local-generation",
        root_objective="Return one local answer.",
        model_ref="provider:model",
        approval_mode="approve",
        runtime_surface="normal_chat",
    )
    child = agent_runs.create_agent_run(
        run_id="local-child",
        status="running",
        parent_thread_id=parent_thread_id,
        prompt="Produce evidence",
        model_override="provider:model",
    )
    orchestrator.register_member(orchestration["id"], child["id"], required=True)
    orchestrator.set_test_executors(synthesis=lambda *_args: "One local final")

    assert orchestrator.finalize_parent_generation(
        orchestration["id"],
        continuation_state={"config": {"configurable": {}}, "enabled_tool_names": []},
    )
    agent_runs.finish_agent_run(child["id"], "completed", summary="Evidence")
    orchestrator.wait_for_synthesis(orchestration["id"])

    messages = orchestrator.list_messages(orchestration["id"])
    assert [(row["kind"], row["delivery_status"]) for row in messages] == [
        ("acknowledgement", "delivered"),
        ("final", "delivered"),
    ]
    assert orchestrator.retry_pending_deliveries() == 0


def test_default_synthesis_uses_a_disposable_thread(monkeypatch, tmp_path):
    _agent_runs, orchestrator = _fresh_modules(tmp_path, monkeypatch)
    orchestration = orchestrator.create_or_get_orchestration(
        parent_thread_id="visible-parent",
        parent_generation_id="visible-generation",
        root_objective="Return one consolidated answer.",
        model_ref="model:codex:gpt-5.6-sol",
        approval_mode="approve",
        runtime_surface="normal_chat",
    )
    orchestration["continuation_state_json"] = {
        "config": {
            "configurable": {
                "thread_id": "visible-parent",
                "generation_id": "visible-generation",
            }
        },
        "enabled_tool_names": ["agents", "web_search"],
    }
    seen = {}
    cleaned = []

    def fake_invoke(prompt, enabled_tools, config):
        seen["prompt"] = prompt
        seen["enabled_tools"] = enabled_tools
        seen["config"] = config
        return "Consolidated final"

    monkeypatch.setattr("row_bot.agent.invoke_agent", fake_invoke)
    monkeypatch.setattr(
        "row_bot.threads.delete_threads",
        lambda thread_ids: cleaned.extend(thread_ids) or (len(thread_ids), []),
    )

    result = orchestrator._default_synthesis_executor(
        orchestration,
        "Internal synthesis packet",
    )

    synthesis_thread_id = seen["config"]["configurable"]["thread_id"]
    assert result == "Consolidated final"
    assert synthesis_thread_id == f"orchestration-synthesis:{orchestration['id']}"
    assert synthesis_thread_id != "visible-parent"
    assert seen["config"]["configurable"]["generation_id"].endswith(":synthesis")
    assert seen["enabled_tools"] == ["web_search"]
    assert cleaned == [synthesis_thread_id]


def test_group_message_stop_and_result_packet_keep_worktree_reference(
    tmp_path,
    monkeypatch,
):
    agent_runs, orchestrator = _fresh_modules(tmp_path, monkeypatch)
    orchestration = orchestrator.create_or_get_orchestration(
        parent_thread_id="parent",
        parent_generation_id="generation",
        root_objective="Implement and review safely",
        model_ref="provider:model",
        approval_mode="block",
        runtime_surface="normal_chat",
    )
    child = agent_runs.create_agent_run(
        run_id="writer",
        status="queued",
        parent_thread_id="parent",
        prompt="Implement in worktree",
        workspace_mode="worktree",
        workspace_path="D:/disposable/worktree",
        model_override="provider:model",
    )
    orchestrator.register_member(orchestration["id"], child["id"], required=True)

    assert orchestrator.message_orchestration(
        orchestration["id"],
        "Also run the focused test.",
    ) == 1
    assert orchestrator.list_messages(orchestration["id"], kinds=["steering"])[0][
        "delivery_status"
    ] == "pending"
    stopped: list[str] = []
    monkeypatch.setattr(
        "row_bot.agent_runner.stop_agent_run",
        lambda run_id: stopped.append(run_id) or agent_runs.get_agent_run(run_id),
    )
    overview = orchestrator.stop_orchestration(orchestration["id"])
    assert overview["status"] == "stopped"
    assert stopped == ["writer"]


def test_channel_runtime_owns_acknowledgement_and_final_after_suspension(
    tmp_path,
    monkeypatch,
):
    agent_runs, orchestrator = _fresh_modules(tmp_path, monkeypatch)
    from row_bot.channels import runtime as channel_runtime

    config = channel_runtime.prepare_channel_turn_config(
        {"configurable": {"thread_id": "channel-parent", "runtime_channel": "fake"}},
        "Compare two sources.",
    )
    configurable = config["configurable"]
    orchestration = orchestrator.create_or_get_orchestration(
        parent_thread_id="channel-parent",
        parent_generation_id=configurable["generation_id"],
        root_objective=configurable["root_objective"],
        model_ref="provider:model",
        approval_mode="approve",
        runtime_surface="channel",
    )
    child = agent_runs.create_agent_run(
        run_id="channel-child",
        status="running",
        parent_thread_id="channel-parent",
        prompt="Check source one",
        model_override="provider:model",
    )
    orchestrator.register_member(orchestration["id"], child["id"], required=True)
    deliveries: list[tuple[str, str]] = []
    orchestrator.set_test_executors(
        synthesis=lambda _row, _prompt: "Channel consolidated final",
        delivery=lambda _row, kind, text, _key: deliveries.append((kind, text)) or True,
    )

    assert channel_runtime.finalize_channel_orchestration(
        config,
        "Provisional channel draft",
        ["agents"],
    )
    agent_runs.finish_agent_run(child["id"], "completed", summary="Evidence")
    orchestrator.wait_for_synthesis(orchestration["id"])

    assert deliveries == [
        ("acknowledgement", "I'm working on this with 1 agent."),
        ("final", "Channel consolidated final"),
    ]


def test_channel_stream_sentinel_cleans_preview_without_sending_a_draft():
    from row_bot.channels.streaming import (
        ORCHESTRATION_SUSPENDED_FINAL,
        ChannelStreamConfig,
        ChannelStreamConsumer,
    )

    class Transport:
        def __init__(self):
            self.operations = []

        async def send_typing(self):
            return None

        async def start(self, text):
            self.operations.append(("start", text))
            return "preview"

        async def update(self, handle, text, *, final=False):
            self.operations.append(("update", text, final))

        async def send_final(self, text):
            self.operations.append(("send_final", text))
            return ["final"]

        async def cleanup_preview(self, handle):
            self.operations.append(("cleanup", handle))

        def split_text(self, text):
            return [text] if text else []

    async def run_case():
        transport = Transport()
        consumer = ChannelStreamConsumer(
            transport,
            ChannelStreamConfig(
                channel="fake",
                transport_mode="edit",
                update_interval_s=0,
                min_update_chars=1,
                typing_interval_s=None,
                cursor="...",
                max_message_units=1000,
                sparse_progress=False,
            ),
        )

        async def events():
            yield "token", "provisional"

        result = await consumer.consume_events(
            events(),
            final_text=ORCHESTRATION_SUSPENDED_FINAL,
        )
        return result, transport.operations

    result, operations = asyncio.run(run_case())
    assert result.delivered is True
    assert result.final_text == ""
    assert ("cleanup", "preview") in operations
    assert not any(operation[0] == "send_final" for operation in operations)


def test_failed_delivery_retries_once_without_duplicate_success(
    tmp_path,
    monkeypatch,
):
    agent_runs, orchestrator = _fresh_modules(tmp_path, monkeypatch)
    orchestration = orchestrator.create_or_get_orchestration(
        parent_thread_id="delivery-parent",
        parent_generation_id="delivery-generation",
        root_objective="Deliver exactly once.",
        model_ref="provider:model",
        approval_mode="block",
        runtime_surface="channel",
    )
    child = agent_runs.create_agent_run(
        run_id="delivery-child",
        status="running",
        parent_thread_id="delivery-parent",
        prompt="Finish",
        model_override="provider:model",
    )
    orchestrator.register_member(orchestration["id"], child["id"], required=True)
    attempts: list[tuple[str, str]] = []

    def delivery(_row, kind, _text, key):
        attempts.append((kind, key))
        return len([item for item in attempts if item[0] == kind]) > 1

    orchestrator.set_test_executors(
        synthesis=lambda *_args: "Final after delivery retry",
        delivery=delivery,
    )
    orchestrator.finalize_parent_generation(
        orchestration["id"],
        continuation_state={"config": {"configurable": {}}, "enabled_tool_names": []},
    )
    agent_runs.finish_agent_run(child["id"], "completed", summary="Done")
    orchestrator.wait_for_synthesis(orchestration["id"])

    assert orchestrator.retry_pending_deliveries() == 2
    assert orchestrator.retry_pending_deliveries() == 0
    assert [kind for kind, _key in attempts] == [
        "acknowledgement",
        "final",
        "acknowledgement",
        "final",
    ]
    assert len({key for _kind, key in attempts}) == 2


def test_group_stop_wins_synthesis_race(tmp_path, monkeypatch):
    agent_runs, orchestrator = _fresh_modules(tmp_path, monkeypatch)
    orchestration = orchestrator.create_or_get_orchestration(
        parent_thread_id="stop-parent",
        parent_generation_id="stop-generation",
        root_objective="Stop before synthesis.",
        model_ref="provider:model",
        approval_mode="block",
        runtime_surface="normal_chat",
    )
    child = agent_runs.create_agent_run(
        run_id="stop-child",
        status="running",
        parent_thread_id="stop-parent",
        prompt="Long work",
        model_override="provider:model",
    )
    orchestrator.register_member(orchestration["id"], child["id"], required=True)
    calls: list[str] = []
    orchestrator.set_test_executors(
        synthesis=lambda *_args: calls.append("synthesis") or "Unexpected",
        delivery=lambda *_args: True,
    )
    orchestrator.finalize_parent_generation(
        orchestration["id"],
        continuation_state={"config": {"configurable": {}}, "enabled_tool_names": []},
    )
    monkeypatch.setattr(
        "row_bot.agent_runner.stop_agent_run",
        lambda run_id: agent_runs.finish_agent_run(
            run_id,
            "stopped",
            status_message="Stop requested",
        ),
    )
    result = orchestrator.stop_orchestration(orchestration["id"])
    orchestrator.wait_for_synthesis(orchestration["id"])

    assert result["status"] == "stopped"
    assert calls == []


def test_ui_and_voice_source_contract_has_durable_refresh_without_cutoff():
    from pathlib import Path

    streaming_source = Path("src/row_bot/ui/streaming.py").read_text(encoding="utf-8")
    app_source = Path("src/row_bot/app.py").read_text(encoding="utf-8")

    assert "gen.tts_active = False" in streaming_source
    assert "voice_output.speak_final(speakable.text)" in streaming_source
    assert "_orchestration_acknowledgement" not in streaming_source
    assert "remove_latest_checkpoint_ai_message" not in streaming_source
    assert "get_generation_orchestration" in streaming_source
    assert "retry_pending_deliveries" in app_source
    assert "speak_orchestration_final" in app_source
    assert "voice_final" in app_source
    assert '"parent_progress"' in app_source
    assert '"parent_final"' in app_source
    assert "240" not in "\n".join(
        line
        for line in app_source.splitlines()
        if "orchestration" in line.lower() or "agent" in line.lower()
    )


def test_detached_voice_final_uses_saved_transport():
    from row_bot.voice.output_controller import speak_orchestration_final

    class FakeTTS:
        enabled = True

        def __init__(self):
            self.finals = []

        def flush_streaming(self, text):
            self.finals.append(text)

    tts = FakeTTS()
    assert speak_orchestration_final(
        {"voice_mode": True, "voice_transport": "normal"},
        "Consolidated final",
        tts_service=tts,
        realtime_speaker=None,
        now=lambda: 0.0,
    )
    assert tts.finals == ["Consolidated final"]
    assert not speak_orchestration_final(
        {"voice_mode": False, "voice_transport": "normal"},
        "Silent",
        tts_service=tts,
        realtime_speaker=None,
        now=lambda: 0.0,
    )


def test_agent_entrypoints_route_later_input_without_starting_another_graph(
    monkeypatch,
):
    import row_bot.agent as agent

    monkeypatch.setattr(
        agent,
        "_route_waiting_parent_input",
        lambda _text, _config: {"id": "orchestration-1"},
    )
    monkeypatch.setattr(
        agent,
        "_invoke_agent_graph",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a second graph must not start for parent steering")
        ),
    )
    config = {
        "configurable": {
            "thread_id": "thread-1",
            "generation_id": "later-generation",
        }
    }

    assert agent.invoke_agent("Use this too", ["agents"], config) == {
        "type": "orchestration_waiting",
        "orchestration_id": "orchestration-1",
    }
    assert list(agent.stream_agent("Use this too", ["agents"], config)) == [
        (
            "orchestration_waiting",
            {
                "orchestration_id": "orchestration-1",
                "text": "",
                "output_kind": "steering",
            },
        )
    ]
