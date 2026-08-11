from types import SimpleNamespace


def test_suffix_pruning_offsets_for_large_transcript_prefix_control():
    from row_bot.ui.transcript import transcript_message_child_bounds

    assert transcript_message_child_bounds(60, 60, 58) == (58, 60)
    assert transcript_message_child_bounds(61, 60, 58) == (59, 61)


def test_langchain_messages_to_ui_messages_preserves_visible_shapes():
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from row_bot.ui.helpers import langchain_messages_to_ui_messages

    messages = [
        HumanMessage(content=[
            {"type": "text", "text": "hello"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc123"}},
        ]),
        AIMessage(content="", tool_calls=[{"id": "call_1", "name": "chart", "args": {}}]),
        ToolMessage(
            content='__CHART__:{"data":[]}\n\nChart created',
            name="chart",
            tool_call_id="call_1",
        ),
        AIMessage(
            content="<think>hidden work</think>Here is the chart.",
            additional_kwargs={"reasoning_content": "provider reasoning"},
        ),
    ]

    ui_messages = langchain_messages_to_ui_messages(messages)

    assert ui_messages == [
        {"role": "user", "content": "hello", "images": ["abc123"]},
        {
            "role": "assistant",
            "content": "Here is the chart.",
            "thinking": "provider reasoning\nhidden work",
            "tool_results": [{"name": "chart", "content": "Chart created"}],
            "charts": ['{"data":[]}'],
        },
    ]


def test_checkpoint_tool_invoke_results_recover_distinct_underlying_names_for_ui_and_export():
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from row_bot.ui.helpers import _build_conversation_html, langchain_messages_to_ui_messages
    from row_bot.ui.tool_trace import group_tool_results

    targets = [
        "hacker_news_top_stories",
        "mcp_context7_resolve_library_id",
        "custom_tool_read_catalog",
        "channel_read_history",
    ]
    messages = [HumanMessage(content="Read these sources")]
    messages.append(AIMessage(content="", tool_calls=[
        {
            "id": f"call-{index}",
            "name": "tool_invoke",
            "args": {
                "name": target,
                "arguments": {"token": f"secret-{index}", "nested": {"private": True}},
            },
            "type": "tool_call",
        }
        for index, target in enumerate(targets)
    ]))
    messages.extend(
        ToolMessage(content=f"result-{index}", name="tool_invoke", tool_call_id=f"call-{index}")
        for index in range(len(targets))
    )
    messages.append(AIMessage(content="Done."))

    ui_messages = langchain_messages_to_ui_messages(messages)
    results = ui_messages[-1]["tool_results"]
    html = _build_conversation_html("Recovered tools", ui_messages)

    assert [result["name"] for result in results] == targets
    assert [group.name for group in group_tool_results(results)] == targets
    for target in targets:
        assert target in html
    assert "secret-" not in str(ui_messages)
    assert "private" not in str(ui_messages)


def test_checkpoint_tool_invoke_identity_falls_back_safely_and_is_consumed_once():
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from row_bot.ui.helpers import langchain_messages_to_ui_messages

    messages = [
        HumanMessage(content="first"),
        AIMessage(content="", tool_calls=[{
            "id": "shared-call",
            "name": "tool_invoke",
            "args": {"name": "  first_target\n  lookup  ", "arguments": {"secret": "hidden"}},
            "type": "tool_call",
        }]),
        ToolMessage(content="first result", name="tool_invoke", tool_call_id="shared-call"),
        AIMessage(content="", tool_calls=[{
            "id": "shared-call",
            "name": "tool_invoke",
            "args": {"name": "second_target", "arguments": {}},
            "type": "tool_call",
        }]),
        ToolMessage(content="second result", name="tool_invoke", tool_call_id="shared-call"),
        AIMessage(content="done"),
        HumanMessage(content="new boundary"),
        AIMessage(content="", tool_calls=[{
            "id": "stale-call",
            "name": "tool_invoke",
            "args": {"name": "must_not_cross_turns", "arguments": {}},
            "type": "tool_call",
        }]),
        HumanMessage(content="clear it"),
        ToolMessage(content="stale result", name="tool_invoke", tool_call_id="stale-call"),
        ToolMessage(content="missing id", name="tool_invoke", tool_call_id=""),
        AIMessage(content="after boundary"),
    ]

    ui_messages = langchain_messages_to_ui_messages(messages)
    first_results = ui_messages[1]["tool_results"]
    final_results = ui_messages[-1]["tool_results"]

    assert [result["name"] for result in first_results] == [
        "first_target lookup",
        "second_target",
    ]
    assert [result["name"] for result in final_results] == ["tool_invoke", "tool_invoke"]
    assert "hidden" not in str(ui_messages)
    assert "must_not_cross_turns" not in str(ui_messages)


def test_checkpoint_tool_identity_rejects_malformed_metadata_and_preserves_direct_tools():
    from langchain_core.messages import AIMessage, ToolMessage
    from row_bot.ui.helpers import langchain_messages_to_ui_messages

    malformed_ai = SimpleNamespace(
        type="ai",
        content="",
        additional_kwargs={},
        tool_calls=[
            {"id": "bad-name", "name": "tool_invoke", "args": {"name": {"nested": "value"}}},
            {"id": "", "name": "tool_invoke", "args": {"name": "missing_call_id"}},
        ],
    )
    ui_messages = langchain_messages_to_ui_messages([
        malformed_ai,
        ToolMessage(content="bad name", name="tool_invoke", tool_call_id="bad-name"),
        ToolMessage(content="bad id", name="tool_invoke", tool_call_id=""),
        AIMessage(content="", tool_calls=[{
            "id": "direct-call",
            "name": "workspace_read_file",
            "args": {"path": "notes.txt"},
            "type": "tool_call",
        }]),
        ToolMessage(
            content="direct result",
            name="workspace_read_file",
            tool_call_id="direct-call",
        ),
        AIMessage(content="done"),
    ])

    assert [result["name"] for result in ui_messages[-1]["tool_results"]] == [
        "tool_invoke",
        "tool_invoke",
        "workspace_read_file",
    ]


def test_langchain_messages_to_ui_messages_does_not_surface_reasoning_only_planning_after_vision_tool():
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from row_bot.ui.helpers import langchain_messages_to_ui_messages

    messages = [
        HumanMessage(content="what do you see?"),
        AIMessage(content="", tool_calls=[{"id": "call_1", "name": "analyze_image", "args": {"source": "screen"}}]),
        ToolMessage(content="The screenshot shows a settings window.", name="analyze_image", tool_call_id="call_1"),
        AIMessage(content="", additional_kwargs={"reasoning_content": "The tool returned detailed information. I should summarize it for the user."}),
        AIMessage(content="The screenshot shows a settings window."),
    ]

    ui_messages = langchain_messages_to_ui_messages(messages)

    assert ui_messages[-1]["content"] == "The screenshot shows a settings window."
    assert "I should summarize" not in str(ui_messages)
    assert ui_messages[-1]["tool_results"] == [{"name": "analyze_image", "content": "The screenshot shows a settings window."}]


def test_process_attached_files_does_not_mark_failed_vision_as_analyzed():
    from row_bot.ui.helpers import process_attached_files

    class _Vision:
        enabled = True

        def analyze(self, data, question):
            return "Vision analysis failed: image input unsupported"

    context, images, warnings = process_attached_files(
        [{"name": "photo.png", "data": b"not-really-an-image"}],
        _Vision(),
        {},
        model_name="qwen",
    )

    assert images
    assert warnings == []
    assert "vision analysis failed" in context
    assert "ALREADY ANALYZED" not in context
    assert "do NOT call analyze_image" not in context


def test_load_thread_messages_does_not_import_or_call_agent_graph(monkeypatch):
    from langchain_core.messages import AIMessage, HumanMessage
    import sys
    import row_bot.threads as threads
    import row_bot.ui.helpers as helpers

    def _boom(*args, **kwargs):
        raise AssertionError("get_agent_graph should not be used for transcript loading")

    fake_agent = SimpleNamespace(get_agent_graph=_boom)
    monkeypatch.setitem(sys.modules, "agent", fake_agent)
    monkeypatch.setitem(sys.modules, "row_bot.agent", fake_agent)
    monkeypatch.setattr(threads, "get_latest_checkpoint_messages", lambda thread_id: [
        HumanMessage(content="question"),
        AIMessage(content="answer"),
    ])

    assert helpers.load_thread_messages("thread-1") == [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
    ]


def test_get_latest_checkpoint_messages_reads_checkpointer_without_graph(monkeypatch):
    import row_bot.threads as threads

    raw_messages = [object()]

    class FakeCheckpointer:
        def get_tuple(self, config):
            assert config["configurable"]["thread_id"] == "thread-2"
            return SimpleNamespace(checkpoint={"channel_values": {"messages": raw_messages}})

    monkeypatch.setattr(threads, "checkpointer", FakeCheckpointer())

    assert threads.get_latest_checkpoint_messages("thread-2") == raw_messages


def test_get_token_usage_reads_checkpoint_without_agent_graph(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from langchain_core.messages import HumanMessage
    import row_bot.agent as agent
    import row_bot.threads as threads

    def _boom(*args, **kwargs):
        raise AssertionError("get_agent_graph should not be used for token usage")

    monkeypatch.setattr(agent, "get_agent_graph", _boom)
    monkeypatch.setattr(threads, "get_latest_checkpoint_messages", lambda thread_id: [HumanMessage(content="hello")])

    used, max_tokens = agent.get_token_usage({"configurable": {"thread_id": "thread-token"}}, model_override="model:ollama:qwen3:14b")

    assert used > 0
    assert max_tokens > 0


def test_append_checkpoint_messages_uses_checkpointer_string_versions(monkeypatch):
    from langchain_core.messages import HumanMessage
    import row_bot.threads as threads

    writes = {}

    class FakeCheckpointer:
        def get_tuple(self, config):
            return SimpleNamespace(
                config={"configurable": {"thread_id": "thread-3", "checkpoint_ns": "", "checkpoint_id": "parent"}},
                checkpoint={
                    "channel_values": {"messages": []},
                    "channel_versions": {"messages": "00000000000000000000000000000001.0000000000000000"},
                    "versions_seen": {},
                },
            )

        def get_next_version(self, current, channel):
            assert current == "00000000000000000000000000000001.0000000000000000"
            return "00000000000000000000000000000002.0000000000000000"

        def put(self, config, checkpoint, metadata, new_versions):
            writes["checkpoint"] = checkpoint
            writes["metadata"] = metadata
            writes["new_versions"] = new_versions
            return config

    monkeypatch.setattr(threads, "checkpointer", FakeCheckpointer())

    assert threads.append_checkpoint_messages("thread-3", [HumanMessage(content="hello")]) is True
    assert writes["checkpoint"]["channel_versions"]["messages"] == "00000000000000000000000000000002.0000000000000000"
    assert writes["new_versions"]["messages"] == "00000000000000000000000000000002.0000000000000000"
    assert isinstance(writes["checkpoint"]["channel_versions"]["messages"], str)


def test_append_checkpoint_messages_repairs_legacy_int_versions(monkeypatch):
    from langchain_core.messages import HumanMessage
    import row_bot.threads as threads

    writes = {}

    class FakeCheckpointer:
        def get_tuple(self, config):
            return SimpleNamespace(
                config={"configurable": {"thread_id": "thread-4", "checkpoint_ns": "", "checkpoint_id": "parent"}},
                checkpoint={
                    "channel_values": {"messages": []},
                    "channel_versions": {"messages": 2, "other": "3"},
                    "versions_seen": {"agent": {"messages": 1}},
                },
            )

        def get_next_version(self, current, channel):
            assert current == "00000000000000000000000000000002.0000000000000000"
            return "00000000000000000000000000000003.0000000000000000"

        def put(self, config, checkpoint, metadata, new_versions):
            writes["checkpoint"] = checkpoint
            writes["new_versions"] = new_versions
            return config

    monkeypatch.setattr(threads, "checkpointer", FakeCheckpointer())

    assert threads.append_checkpoint_messages("thread-4", [HumanMessage(content="hello")]) is True
    assert writes["checkpoint"]["channel_versions"]["messages"] == "00000000000000000000000000000003.0000000000000000"
    assert writes["checkpoint"]["channel_versions"]["other"] == "00000000000000000000000000000003.0000000000000000"
    assert writes["checkpoint"]["versions_seen"]["agent"]["messages"] == "00000000000000000000000000000001.0000000000000000"


def test_orchestration_suspend_cleans_model_checkpoint_without_ui_state(monkeypatch):
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    import row_bot.threads as threads

    previous_user = HumanMessage(content="Earlier question")
    previous_answer = AIMessage(content="Earlier answer")
    current_user = HumanMessage(content="Compare three vendors")
    draft = AIMessage(
        content="I will delegate this.",
        tool_calls=[{"name": "agents", "args": {}, "id": "call-1"}],
    )
    tool_result = ToolMessage(
        content='{"message":"Child Agent started."}',
        tool_call_id="call-1",
        name="agents",
    )
    provisional = AIMessage(content="The agents are queued.")
    trailing_empty = AIMessage(content="")
    writes = {}

    class FakeCheckpointer:
        def get_tuple(self, config):
            return SimpleNamespace(
                config={
                    "configurable": {
                        "thread_id": "thread-orchestration",
                        "checkpoint_ns": "",
                        "checkpoint_id": "parent",
                    }
                },
                checkpoint={
                    "channel_values": {
                        "messages": [
                            previous_user,
                            previous_answer,
                            current_user,
                            draft,
                            tool_result,
                            provisional,
                            trailing_empty,
                        ]
                    },
                    "channel_versions": {
                        "messages": "00000000000000000000000000000005.0000000000000000"
                    },
                    "versions_seen": {},
                },
            )

        def get_next_version(self, current, channel):
            assert current == "00000000000000000000000000000005.0000000000000000"
            return "00000000000000000000000000000006.0000000000000000"

        def put(self, config, checkpoint, metadata, new_versions):
            writes["checkpoint"] = checkpoint
            writes["metadata"] = metadata
            writes["new_versions"] = new_versions
            return config

    monkeypatch.setattr(threads, "checkpointer", FakeCheckpointer())

    assert threads.remove_latest_checkpoint_ai_message(
        "thread-orchestration",
        "I will delegate this.The agents are queued.",
    )
    assert writes["checkpoint"]["channel_values"]["messages"] == [
        previous_user,
        previous_answer,
        current_user,
    ]
    assert writes["metadata"]["writes"]["messages"] == -4


def test_durable_orchestration_merge_preserves_unrelated_messages_and_queue():
    from row_bot.ui.transcript import upsert_durable_transcript_message

    old_answer = {"role": "assistant", "content": "old answer"}
    acknowledgement = {
        "role": "assistant",
        "content": "I'm working on this with 1 agent.",
        "orchestration_id": "orch-1",
        "orchestration_message_kind": "acknowledgement",
    }
    queued = {
        "role": "user",
        "content": "queued follow-up",
        "queued_control": {"id": "queued-1", "status": "queued_parent_turn"},
    }
    messages = [
        {"role": "user", "content": "old question"},
        old_answer,
        {"role": "user", "content": "delegate this"},
        acknowledgement,
        queued,
    ]
    final = {
        "role": "assistant",
        "content": "Consolidated final",
        "orchestration_id": "orch-1",
        "orchestration_message_kind": "final",
        "channel_notification_key": "orchestration:orch-1:final",
    }

    changed, index = upsert_durable_transcript_message(messages, final)

    assert changed is True
    assert index == 4
    assert messages[1] is old_answer
    assert messages[3] is acknowledgement
    assert messages[4] is final
    assert messages[5] is queued

    duplicate = dict(final)
    duplicate["content"] = "Consolidated final"
    changed, index = upsert_durable_transcript_message(messages, duplicate)

    assert changed is False
    assert index == 4
    assert len(messages) == 6


def test_durable_orchestration_merge_upgrades_matching_plain_checkpoint_row():
    from row_bot.ui.transcript import upsert_durable_transcript_message

    messages = [
        {"role": "user", "content": "delegate this"},
        {"role": "assistant", "content": "Consolidated final"},
    ]
    incoming = {
        "role": "assistant",
        "content": "Consolidated final",
        "orchestration_id": "orch-1",
        "orchestration_message_kind": "final",
        "channel_notification_key": "orchestration:orch-1:parent:3",
    }

    changed, index = upsert_durable_transcript_message(messages, incoming)

    assert changed is True
    assert index == 1
    assert len(messages) == 2
    assert messages[1] == incoming


def test_match_durable_orchestration_outputs_restores_missing_checkpoint_metadata():
    from row_bot.ui.transcript import match_durable_orchestration_outputs

    loaded_messages = [
        {"role": "user", "content": "delegate this"},
        {"role": "assistant", "content": "Parent progress"},
        {"role": "assistant", "content": "Consolidated final"},
    ]
    durable_outputs = [
        {
            "id": "orchestration:orch-1:parent:2",
            "kind": "parent_progress",
            "content": "Parent progress",
        },
        {
            "id": "orchestration:orch-1:parent:3",
            "kind": "parent_final",
            "content": "Consolidated final",
        },
    ]

    matched = match_durable_orchestration_outputs(
        loaded_messages,
        durable_outputs,
        orchestration_id="orch-1",
    )

    assert [message["content"] for message in matched] == [
        "Parent progress",
        "Consolidated final",
    ]
    assert [message["orchestration_message_kind"] for message in matched] == [
        "progress",
        "final",
    ]
    assert [message["channel_notification_key"] for message in matched] == [
        "orchestration:orch-1:parent:2",
        "orchestration:orch-1:parent:3",
    ]


def test_langchain_messages_to_ui_messages_preserves_checkpoint_message_ids():
    from langchain_core.messages import AIMessage, HumanMessage
    from row_bot.ui.helpers import langchain_messages_to_ui_messages

    ui_messages = langchain_messages_to_ui_messages(
        [
            HumanMessage(content="question", id="human-checkpoint-id"),
            AIMessage(content="answer", id="ai-checkpoint-id"),
        ]
    )

    assert ui_messages[0]["checkpoint_message_id"] == "human-checkpoint-id"
    assert ui_messages[1]["checkpoint_message_id"] == "ai-checkpoint-id"


def test_get_latest_checkpoint_revision_prefers_checkpoint_config_identity(monkeypatch):
    import row_bot.threads as threads

    class FakeCheckpointer:
        def get_tuple(self, config):
            assert config["configurable"]["thread_id"] == "thread-revision"
            return SimpleNamespace(
                config={"configurable": {"checkpoint_id": "checkpoint-42"}},
                checkpoint={
                    "id": "fallback-id",
                    "channel_versions": {"messages": "version-7"},
                },
            )

    monkeypatch.setattr(threads, "checkpointer", FakeCheckpointer())

    assert threads.get_latest_checkpoint_revision("thread-revision") == "checkpoint-42"


def test_match_durable_orchestration_outputs_uses_checkpoint_id_before_duplicate_text():
    from row_bot.ui.transcript import match_durable_orchestration_outputs

    loaded_messages = [
        {
            "role": "assistant",
            "content": "Same progress text",
            "checkpoint_message_id": "older",
        },
        {
            "role": "assistant",
            "content": "Same progress text",
            "checkpoint_message_id": "current",
        },
    ]
    durable_outputs = [
        {
            "id": "orchestration:orch-1:parent_progress:2",
            "kind": "parent_progress",
            "content": "Same progress text",
            "payload_json": {"checkpoint_message_id": "current"},
        }
    ]

    matched = match_durable_orchestration_outputs(
        loaded_messages,
        durable_outputs,
        orchestration_id="orch-1",
    )

    assert matched == [
        {
            **loaded_messages[1],
            "orchestration_id": "orch-1",
            "orchestration_message_kind": "progress",
            "channel_notification_key": "orchestration:orch-1:parent_progress:2",
        }
    ]


def test_agent_refresh_key_tracks_checkpoint_revision_source_contract():
    from pathlib import Path

    source = Path("src/row_bot/app.py").read_text(encoding="utf-8")
    poll = source.split("def _current_agent_run_refresh_key", 1)[1].split(
        "def _reload_completed_orchestration_transcript", 1
    )[0]
    reload_block = source.split(
        "def _reload_completed_orchestration_transcript", 1
    )[1].split("def _current_child_agent_run_ids", 1)[0]

    assert "get_latest_checkpoint_revision(tid)" in poll
    assert "get_latest_checkpoint_revision(tid)" in reload_block


def test_orchestration_transcript_refresh_is_retryable_across_live_generation():
    from pathlib import Path

    source = Path("src/row_bot/app.py").read_text(encoding="utf-8")
    poll = source.split("def _poll_agent_card_refresh", 1)[1].split(
        "def _poll_notifications",
        1,
    )[0]
    reload_block = source.split(
        "def _reload_completed_orchestration_transcript",
        1,
    )[1].split("def _current_child_agent_run_ids", 1)[0]

    assert 'return "processed_no_change"' in reload_block
    assert 'return "processed_changed"' in reload_block
    assert 'return "retry"' in reload_block
    assert "if latest_output and not orchestration_messages:" in reload_block
    assert reload_block.index("for incoming_output in orchestration_messages:") < (
        reload_block.index('_last_orchestration_transcript["key"] = refresh_key')
    )
    assert '_last_agent_run_refresh.update({"thread_id": tid, **keys})' not in poll
    live_return = poll.index("if live_generation:")
    transcript_ack = poll.index(
        '_last_agent_run_refresh["transcript"] = keys["transcript"]'
    )
    assert live_return < transcript_ack
    assert 'if reload_result != "retry":' in poll


def test_transient_durable_output_match_miss_retries_then_merges_exactly_once():
    from row_bot.ui.transcript import (
        match_durable_orchestration_outputs,
        upsert_durable_transcript_message,
    )

    output = {
        "id": "orchestration:orch-retry:parent_final:1",
        "kind": "parent_final",
        "content": "Durable fallback final",
    }
    active: list[dict] = []

    first_match = match_durable_orchestration_outputs(
        [],
        [output],
        orchestration_id="orch-retry",
    )
    assert first_match == []
    assert active == []

    loaded = [{"role": "assistant", "content": "Durable fallback final"}]
    second_match = match_durable_orchestration_outputs(
        loaded,
        [output],
        orchestration_id="orch-retry",
    )
    assert len(second_match) == 1
    assert upsert_durable_transcript_message(active, second_match[0]) == (True, 0)
    assert upsert_durable_transcript_message(active, second_match[0]) == (False, 0)
    assert len(active) == 1


def test_checkpoint_parent_approval_upserts_live_by_stable_approval_key():
    from row_bot.ui.streaming import _merge_checkpoint_approval_messages

    active = [
        {"role": "user", "content": "Continue with the protected action."},
    ]
    checkpoint = [
        {"role": "assistant", "content": "Visible parent progress"},
        {
            "role": "assistant",
            "content": "Parent Agent needs approval.",
            "approval_request_id": "approval-parent-1",
            "approval_resume_token": "resume-parent-1",
            "approval_status": "pending",
            "channel_notification_key": "agent_approval:approval-parent-1",
            "orchestration_id": "orch-1",
            "orchestration_message_kind": "parent_approval",
        },
    ]

    assert _merge_checkpoint_approval_messages(active, checkpoint) is True
    assert [
        message.get("approval_request_id")
        for message in active
        if message.get("approval_request_id")
    ] == ["approval-parent-1"]
    assert "Visible parent progress" not in [message.get("content") for message in active]
    assert _merge_checkpoint_approval_messages(active, checkpoint) is False

    assert _merge_checkpoint_approval_messages(
        active,
        [checkpoint[1]],
        approval_statuses={"approval-parent-1": "denied"},
    ) is True
    approval_message = next(
        message for message in active if message.get("approval_request_id")
    )
    assert approval_message["approval_status"] == "denied"
    assert approval_message["approval_resume_token"] == ""
