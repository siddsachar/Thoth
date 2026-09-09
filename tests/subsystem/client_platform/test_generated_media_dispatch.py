"""Exact per-tool media association through the production ToolNode wrapper."""
from __future__ import annotations

import base64
import threading

from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.prebuilt import ToolNode
from langgraph.graph import StateGraph, MessagesState, START, END

from tests.contracts.client_platform.test_headless_lifecycle import platform  # noqa: F401


def tool_graph(tools, wrapper):
    graph = StateGraph(MessagesState)
    graph.add_node("tools", ToolNode(tools, wrap_tool_call=wrapper))
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    return graph.compile()


def test_parallel_real_tool_dispatch_binds_generated_bytes_to_exact_tool(platform):
    from row_bot.agent import _wrap_platform_tool_call
    from row_bot.application.attachment_context import prepared_attachments
    from row_bot.application.attachments import read_attachment
    from row_bot.tools.image_gen_tool import _set_pending_image
    barrier = threading.Barrier(2)
    @tool
    def synthetic_image(label: str) -> str:
        """Generate an isolated synthetic fixture image."""
        _set_pending_image(base64.b64encode(b"\x89PNG\r\n\x1a\n" + label.encode()).decode())
        barrier.wait(timeout=10)
        return "Synthetic image complete"
    calls = [{"id": f"call-{label}", "name": "synthetic_image", "args": {"label": label}, "type": "tool_call"}
             for label in ("a", "b")]
    node = tool_graph([synthetic_image], _wrap_platform_tool_call)
    with prepared_attachments("conversation-a", []):
        result = node.invoke({"messages": [AIMessage(content="", tool_calls=calls, id="issuing-native")]})
    references = []
    for message in result["messages"][1:]:
        assert message.tool_call_id in {"call-a", "call-b"}
        outcome = message.additional_kwargs["platform_media"][0]
        assert outcome["type"] == "media.available"
        reference = outcome["payload"]["media_ref"]
        references.append(reference)
        _, data = read_attachment(reference)
        assert data.endswith(message.tool_call_id.removeprefix("call-").encode())
    assert len(set(references)) == 2


def test_generated_media_error_is_bound_without_failing_tool_result(platform):
    from row_bot.agent import _wrap_platform_tool_call
    from row_bot.application.attachment_context import prepared_attachments
    from row_bot.tools.image_gen_tool import _set_pending_image
    @tool
    def synthetic_invalid_image() -> str:
        """Produce invalid synthetic media without external effects."""
        _set_pending_image("invalid base64")
        return "Synthetic operation complete"
    node = tool_graph([synthetic_invalid_image], _wrap_platform_tool_call)
    with prepared_attachments("conversation-a", []):
        result = node.invoke({"messages": [AIMessage(content="", id="issuer", tool_calls=[
            {"id": "invalid-call", "name": "synthetic_invalid_image", "args": {}, "type": "tool_call"}])]})
    message = result["messages"][1]
    assert message.content == "Synthetic operation complete"
    assert message.tool_call_id == "invalid-call"
    assert message.additional_kwargs["platform_media"] == [{"type": "media.error", "payload": {"code": "media_unavailable"}}]
