import asyncio
import queue


def test_stream_graph_preserves_text_on_tool_call_chunks():
    from agent import _stream_graph

    class AIMessageChunk:
        def __init__(self, content="", *, tool_calls=None, tool_call_chunks=None):
            self.content = content
            self.response_metadata = {}
            self.additional_kwargs = {}
            self.tool_calls = tool_calls or []
            self.tool_call_chunks = tool_call_chunks or []

    class FakeToolCallMessage:
        type = "ai"
        content = ""
        tool_calls = [{"id": "call_search", "name": "web_search", "args": {"query": "test"}}]

    class FakeToolMessage:
        type = "tool"
        name = "web_search"
        content = "result"

    class FakeState:
        next = None
        tasks = []
        values = {"messages": []}

    class FakeAgent:
        def stream(self, input_data, config, stream_mode):
            yield ("messages", (AIMessageChunk("I'll"), {"langgraph_node": "agent"}))
            yield (
                "messages",
                (
                    AIMessageChunk(
                        " check that now.",
                        tool_call_chunks=[{"name": "web_search", "args": "{}"}],
                    ),
                    {"langgraph_node": "agent"},
                ),
            )
            yield ("updates", {"agent": {"messages": [FakeToolCallMessage()]}})
            yield ("updates", {"tools": {"messages": [FakeToolMessage()]}})
            yield ("messages", (AIMessageChunk(" The result is ready."), {"langgraph_node": "agent"}))
            yield ("updates", {})

        def get_state(self, config):
            return FakeState()

    events = list(_stream_graph(FakeAgent(), {}, {"configurable": {"thread_id": "test-channel-stream"}}))
    token_text = "".join(payload for event_type, payload in events if event_type == "token")
    done_text = [payload for event_type, payload in events if event_type == "done"][-1]

    assert "I'll check that now." in token_text
    assert "I'll check that now." in done_text
    assert "The result is ready." in done_text


def test_telegram_stream_consumer_returns_none_when_final_edit_fails():
    from channels.telegram import _tg_edit_consumer

    class FakeSentMessage:
        def __init__(self):
            self.edits = []

        async def edit_text(self, text, **kwargs):
            self.edits.append(text)
            if "complete" in text:
                raise RuntimeError("simulated final edit failure")

    async def run_case():
        events = queue.Queue()
        events.put(("token", "first"))
        events.put(("token", " complete"))
        events.put(None)
        sent = FakeSentMessage()
        result = await _tg_edit_consumer(None, sent, events, asyncio.get_running_loop())
        return result, sent.edits

    result, edits = asyncio.run(run_case())

    assert result is None
    assert edits[0] == "first"
    assert any("complete" in edit for edit in edits)
