from types import SimpleNamespace


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


def test_orchestration_suspend_removes_the_full_provisional_turn(monkeypatch):
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
