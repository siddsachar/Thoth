from __future__ import annotations

import asyncio
from typing import Any

import pytest

from row_bot.plugins.api import (
    ChannelAttachment,
    ChannelInboundMessage,
    ChannelOutboundCallbacks,
)


pytestmark = pytest.mark.subsystem


class CallbackRecorder:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.streams: list[tuple[str, Any, str]] = []
        self.approvals: list[tuple[Any, dict]] = []

    def callbacks(self) -> ChannelOutboundCallbacks:
        return ChannelOutboundCallbacks(
            send_text=self.texts.append,
            start_stream=self.start_stream,
            update_stream=self.update_stream,
            finish_stream=self.finish_stream,
            send_approval_request=self.send_approval_request,
        )

    def start_stream(self, placeholder: str) -> str:
        self.streams.append(("start", "", placeholder))
        return "stream-1"

    def update_stream(self, handle: Any, text: str) -> None:
        self.streams.append(("update", handle, text))

    def finish_stream(self, handle: Any, text: str) -> None:
        self.streams.append(("finish", handle, text))

    def send_approval_request(self, interrupt_data: Any, config: dict) -> str:
        self.approvals.append((interrupt_data, config))
        return "approval-ref"


def _patch_common_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    from row_bot.channels import runtime as channel_runtime
    from row_bot.plugins import channel_runtime as plugin_runtime
    from row_bot.tools import registry as tool_registry

    async def no_goal_continuation(**_kwargs: Any):
        return channel_runtime.ChannelGoalRunResult(
            turns=0,
            status="idle",
            reason="",
        )

    monkeypatch.setattr(plugin_runtime, "_ensure_thread_meta", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(tool_registry, "get_enabled_tools", lambda: [])
    monkeypatch.setattr(
        channel_runtime,
        "continue_channel_goal_after_turn_async",
        no_goal_continuation,
    )


def _message(text: str = "hello") -> ChannelInboundMessage:
    return ChannelInboundMessage(
        channel_name="teams",
        external_conversation_id="conversation-1",
        sender_id="user-1",
        text=text,
    )


def test_plugin_channel_message_dispatches_to_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_common_runtime(monkeypatch)
    import row_bot.agent as agent
    from row_bot.plugins.channel_runtime import handle_plugin_channel_message

    calls: list[tuple[str, list[str], dict]] = []

    def fake_stream_agent(user_input: str, enabled: list[str], config: dict):
        calls.append((user_input, enabled, config))
        yield ("token", "Agent answer")
        yield ("done", "Agent answer")

    monkeypatch.setattr(agent, "stream_agent", fake_stream_agent)
    monkeypatch.setattr(agent, "RECURSION_LIMIT_CHAT", 25)
    recorder = CallbackRecorder()

    result = asyncio.run(
        handle_plugin_channel_message(
            plugin_id="teams-plugin",
            message=_message("hello Row-Bot"),
            callbacks=ChannelOutboundCallbacks(send_text=recorder.texts.append),
            channel=None,
            enabled_tool_names=["search"],
            stream=False,
        )
    )

    assert result.thread_id == "teams_conversation-1"
    assert result.answer == "Agent answer"
    assert result.handled is True
    assert recorder.texts == ["Agent answer"]
    assert calls[0][0] == "hello Row-Bot"
    assert calls[0][1] == ["search"]
    configurable = calls[0][2]["configurable"]
    assert configurable["runtime_surface"] == "channel"
    assert configurable["runtime_mode"] == "auto"
    assert configurable["runtime_channel"] == "teams"


def test_plugin_channel_delivers_unified_parent_progress_without_adapter_synthesis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_common_runtime(monkeypatch)
    import row_bot.agent as agent
    from row_bot.channels import runtime as channel_runtime
    from row_bot.plugins.channel_runtime import handle_plugin_channel_message

    def fake_stream_agent(_user_input: str, _enabled: list[str], _config: dict):
        yield ("token", "I delegated two checks and will continue when they return.")
        yield ("done", "I delegated two checks and will continue when they return.")
        yield (
            "orchestration_waiting",
            {"orchestration_id": "orchestration-1", "output_kind": "progress"},
        )

    monkeypatch.setattr(agent, "stream_agent", fake_stream_agent)
    monkeypatch.setattr(
        channel_runtime,
        "finalize_channel_orchestration",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("plugin adapters must not synthesize or suppress v2 output")
        ),
    )
    recorder = CallbackRecorder()

    result = asyncio.run(
        handle_plugin_channel_message(
            plugin_id="teams-plugin",
            message=_message("delegate these checks"),
            callbacks=ChannelOutboundCallbacks(send_text=recorder.texts.append),
            channel=None,
            enabled_tool_names=["agents"],
            stream=False,
        )
    )

    assert result.answer == "I delegated two checks and will continue when they return."
    assert recorder.texts == [result.answer]


def test_plugin_channel_slash_command_does_not_run_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_common_runtime(monkeypatch)
    import row_bot.agent as agent
    from row_bot.plugins.channel_runtime import handle_plugin_channel_message

    def fail_stream_agent(*_args: Any, **_kwargs: Any):
        raise AssertionError("slash commands should not run the agent")

    monkeypatch.setattr(agent, "stream_agent", fail_stream_agent)
    texts: list[str] = []

    result = asyncio.run(
        handle_plugin_channel_message(
            plugin_id="teams-plugin",
            message=_message("/help"),
            callbacks=ChannelOutboundCallbacks(send_text=texts.append),
            channel=None,
            enabled_tool_names=[],
            stream=False,
        )
    )

    assert result.command is True
    assert result.handled is True
    assert texts and "Available Commands" in texts[0]


def test_plugin_channel_attachment_prompt_uses_media_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_common_runtime(monkeypatch)
    import row_bot.agent as agent
    from row_bot.channels import media
    from row_bot.plugins.channel_runtime import handle_plugin_channel_message

    seen_prompts: list[str] = []
    monkeypatch.setattr(media, "analyze_image", lambda data, question: "a whiteboard sketch")

    def fake_stream_agent(user_input: str, _enabled: list[str], _config: dict):
        seen_prompts.append(user_input)
        yield ("token", "noted")
        yield ("done", "noted")

    monkeypatch.setattr(agent, "stream_agent", fake_stream_agent)
    texts: list[str] = []
    message = _message("what is this?")
    message.attachments.append(
        ChannelAttachment(
            filename="board.png",
            content_type="image/png",
            kind="image",
            data=b"fake image bytes",
        )
    )

    result = asyncio.run(
        handle_plugin_channel_message(
            plugin_id="teams-plugin",
            message=message,
            callbacks=ChannelOutboundCallbacks(send_text=texts.append),
            channel=None,
            enabled_tool_names=[],
            stream=False,
        )
    )

    assert result.error == ""
    assert "Image attachment 'board.png' analysis" in seen_prompts[0]
    assert "a whiteboard sketch" in seen_prompts[0]
    assert seen_prompts[0].endswith("what is this?")


def test_plugin_channel_streaming_callbacks_receive_updates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_common_runtime(monkeypatch)
    import row_bot.agent as agent
    from row_bot.plugins.channel_runtime import handle_plugin_channel_message

    def fake_stream_agent(_user_input: str, _enabled: list[str], _config: dict):
        yield ("token", "A streaming answer")
        yield ("done", "A streaming answer")

    monkeypatch.setattr(agent, "stream_agent", fake_stream_agent)
    recorder = CallbackRecorder()

    result = asyncio.run(
        handle_plugin_channel_message(
            plugin_id="teams-plugin",
            message=_message("stream please"),
            callbacks=recorder.callbacks(),
            channel=None,
            enabled_tool_names=[],
            stream=True,
        )
    )

    assert result.answer == "A streaming answer"
    assert [item[0] for item in recorder.streams] == ["start", "update", "finish"]
    assert recorder.texts == []


def test_plugin_channel_stream_finish_failure_falls_back_to_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_common_runtime(monkeypatch)
    import row_bot.agent as agent
    from row_bot.plugins.channel_runtime import handle_plugin_channel_message

    def fake_stream_agent(_user_input: str, _enabled: list[str], _config: dict):
        yield ("token", "final answer")
        yield ("done", "final answer")

    def finish_raises(_handle: Any, _text: str) -> None:
        raise RuntimeError("edit failed")

    monkeypatch.setattr(agent, "stream_agent", fake_stream_agent)
    texts: list[str] = []
    streams: list[tuple[str, Any, str]] = []
    callbacks = ChannelOutboundCallbacks(
        send_text=texts.append,
        start_stream=lambda text: streams.append(("start", "", text)) or "stream",
        update_stream=lambda handle, text: streams.append(("update", handle, text)),
        finish_stream=finish_raises,
    )

    result = asyncio.run(
        handle_plugin_channel_message(
            plugin_id="teams-plugin",
            message=_message("stream please"),
            callbacks=callbacks,
            channel=None,
            enabled_tool_names=[],
            stream=True,
        )
    )

    assert result.answer == "final answer"
    assert texts == ["final answer"]


def test_plugin_channel_streaming_send_text_only_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_common_runtime(monkeypatch)
    import row_bot.agent as agent
    from row_bot.plugins.channel_runtime import handle_plugin_channel_message

    def fake_stream_agent(_user_input: str, _enabled: list[str], _config: dict):
        yield ("token", "plain final")
        yield ("done", "plain final")

    monkeypatch.setattr(agent, "stream_agent", fake_stream_agent)
    texts: list[str] = []

    result = asyncio.run(
        handle_plugin_channel_message(
            plugin_id="teams-plugin",
            message=_message("stream please"),
            callbacks=ChannelOutboundCallbacks(send_text=texts.append),
            channel=None,
            enabled_tool_names=[],
            stream=True,
        )
    )

    assert result.answer == "plain final"
    assert texts == ["plain final"]


def test_plugin_channel_goal_uses_stream_callbacks_without_duplicate_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_common_runtime(monkeypatch)
    import row_bot.agent as agent
    from row_bot.channels import runtime as channel_runtime
    from row_bot.plugins.channel_runtime import handle_plugin_channel_message

    monkeypatch.setattr(
        channel_runtime,
        "prepare_channel_goal_start",
        lambda _text, _thread_id: channel_runtime.ChannelGoalStart(
            goal={"id": "goal-1"},
            prompt="goal prompt",
            objective="goal objective",
        ),
    )

    async def fake_goal_loop(**kwargs: Any):
        answer, interrupt = await kwargs["run_turn"](kwargs["first_prompt"], kwargs["config"])
        await kwargs["send_text"](answer)
        return channel_runtime.ChannelGoalRunResult(
            turns=1,
            status="completed",
            reason="done",
            interrupt_data=interrupt,
        )

    monkeypatch.setattr(channel_runtime, "run_channel_goal_async", fake_goal_loop)

    def fake_stream_agent(_user_input: str, _enabled: list[str], _config: dict):
        yield ("token", "goal streamed")
        yield ("done", "goal streamed")

    monkeypatch.setattr(agent, "stream_agent", fake_stream_agent)
    recorder = CallbackRecorder()

    result = asyncio.run(
        handle_plugin_channel_message(
            plugin_id="teams-plugin",
            message=_message("/goal finish it"),
            callbacks=recorder.callbacks(),
            channel=None,
            enabled_tool_names=[],
            stream=True,
        )
    )

    assert result.handled is True
    assert result.command is True
    assert [item[0] for item in recorder.streams] == ["start", "update", "finish"]
    assert recorder.texts == [channel_runtime.format_goal_started_ack(
        channel_runtime.ChannelGoalStart(
            goal={"id": "goal-1"},
            prompt="goal prompt",
            objective="goal objective",
        )
    )]


def test_plugin_channel_interrupt_uses_approval_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_common_runtime(monkeypatch)
    import row_bot.agent as agent
    from row_bot.plugins.channel_runtime import handle_plugin_channel_message

    interrupt = [{"__interrupt_id": "i1", "tool": "shell", "description": "Run command"}]

    def fake_stream_agent(_user_input: str, _enabled: list[str], _config: dict):
        yield ("interrupt", interrupt)

    monkeypatch.setattr(agent, "stream_agent", fake_stream_agent)
    recorder = CallbackRecorder()

    result = asyncio.run(
        handle_plugin_channel_message(
            plugin_id="teams-plugin",
            message=_message("needs approval"),
            callbacks=recorder.callbacks(),
            channel=None,
            enabled_tool_names=[],
            stream=False,
        )
    )

    assert result.interrupted is True
    assert result.interrupt_data == interrupt
    assert recorder.approvals[0][0] == interrupt
    assert recorder.texts == []


def test_plugin_channel_approval_resume_calls_agent_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_common_runtime(monkeypatch)
    import row_bot.agent as agent
    from row_bot.plugins.channel_runtime import handle_plugin_channel_approval

    calls: list[tuple[list[str], dict, bool, list[str] | None]] = []

    def fake_resume_agent(
        enabled: list[str],
        config: dict,
        approved: bool,
        *,
        interrupt_ids: list[str] | None = None,
    ):
        calls.append((enabled, config, approved, interrupt_ids))
        yield ("token", "resumed")
        yield ("done", "resumed")

    monkeypatch.setattr(agent, "resume_stream_agent", fake_resume_agent)
    texts: list[str] = []

    result = asyncio.run(
        handle_plugin_channel_approval(
            plugin_id="teams-plugin",
            channel_name="teams",
            thread_id="teams_conversation-1",
            approved=True,
            callbacks=ChannelOutboundCallbacks(send_text=texts.append),
            interrupt_ids=["i1"],
            source="teams",
        )
    )

    assert result.answer == "resumed"
    assert texts == ["resumed"]
    assert calls[0][2] is True
    assert calls[0][3] == ["i1"]
    assert calls[0][1]["configurable"]["runtime_surface"] == "approval"


def test_plugin_channel_approval_resume_uses_stream_callbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_common_runtime(monkeypatch)
    import row_bot.agent as agent
    from row_bot.plugins.channel_runtime import handle_plugin_channel_approval

    def fake_resume_agent(
        _enabled: list[str],
        _config: dict,
        _approved: bool,
        *,
        interrupt_ids: list[str] | None = None,
    ):
        assert interrupt_ids == ["i1"]
        yield ("token", "resumed")
        yield ("done", "resumed")

    monkeypatch.setattr(agent, "resume_stream_agent", fake_resume_agent)
    recorder = CallbackRecorder()

    result = asyncio.run(
        handle_plugin_channel_approval(
            plugin_id="teams-plugin",
            channel_name="teams",
            thread_id="teams_conversation-1",
            approved=True,
            callbacks=recorder.callbacks(),
            interrupt_ids=["i1"],
            source="teams",
        )
    )

    assert result.answer == "resumed"
    assert [item[0] for item in recorder.streams] == ["start", "update", "finish"]
    assert recorder.texts == []


def test_process_channel_attachment_rejects_url_only() -> None:
    from row_bot.plugins.channel_runtime import process_plugin_channel_attachment

    result = process_plugin_channel_attachment(
        ChannelAttachment(filename="remote.txt", url="https://example.invalid/file.txt")
    )

    assert "URL-only attachments" in result.error
