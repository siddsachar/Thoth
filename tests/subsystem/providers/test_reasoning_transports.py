from __future__ import annotations

import sys
from types import ModuleType

from langchain_core.messages import HumanMessage

from row_bot.providers.reasoning import ReasoningCapabilities, ReasoningRequestPlan, ReasoningSelection
from row_bot.providers.transports.claude_subscription_messages import ChatClaudeSubscriptionMessages
from row_bot.providers.transports.codex_responses import ChatCodexResponses
from row_bot.providers.transports.ollama_cloud import ChatOllamaCloud
from row_bot.providers.transports.openai_compatible import ChatOpenAICompatible
from row_bot.providers.transports.xai_oauth_responses import ChatXAIOAuthResponses
from row_bot.providers.transports.reasoning_fallback import ReasoningFallbackChatModel


def _plan(model_ref: str, *, style: str, kind: str = "effort", effort: str = "high", budget: int = 0):
    selection = ReasoningSelection(kind=kind, effort=effort, budget=budget)
    caps = ReasoningCapabilities(
        supported_efforts=(effort,) if kind == "effort" else (),
        can_disable=kind == "off",
        supports_budget=kind == "budget",
        budget_max=max(budget, 1),
        thinking_mode="adaptive" if style == "anthropic" and kind == "effort" else "none",
        request_style=style,
    )
    return ReasoningRequestPlan(model_ref, selection, caps)


def test_codex_subscription_maps_effort_to_responses_reasoning() -> None:
    model = ChatCodexResponses(
        model_name="gpt-5.5",
        reasoning_plan=_plan("model:codex:gpt-5.5", style="openai_responses", effort="xhigh"),
    )

    body = model._request_body([HumanMessage(content="hello")])

    assert body["reasoning"] == {"effort": "xhigh"}


def test_xai_oauth_maps_effort_to_responses_reasoning() -> None:
    model = ChatXAIOAuthResponses(
        model_name="grok-4.6",
        reasoning_plan=_plan("model:xai_oauth:grok-4.6", style="xai_responses"),
    )

    body = model._request_body([HumanMessage(content="hello")])

    assert body["reasoning"] == {"effort": "high"}


def test_claude_subscription_maps_effort_to_output_config_and_adaptive_thinking() -> None:
    model = ChatClaudeSubscriptionMessages(
        model_name="claude-sonnet-4-6",
        reasoning_plan=_plan("model:claude_subscription:claude-sonnet-4-6", style="anthropic"),
    )

    request = model._request_kwargs([HumanMessage(content="hello")])

    assert request["output_config"] == {"effort": "high"}
    assert request["thinking"] == {"type": "adaptive"}


def test_ollama_cloud_maps_exact_effort_to_native_think() -> None:
    model = ChatOllamaCloud(
        model_name="gpt-oss:20b-cloud",
        api_key="test-key",
        reasoning_plan=_plan("model:ollama_cloud:gpt-oss:20b-cloud", style="ollama", effort="medium"),
    )

    body = model._request_body([HumanMessage(content="hello")], stream=True)

    assert body["think"] == "medium"


def test_openai_compatible_uses_only_proven_request_style() -> None:
    openrouter = ChatOpenAICompatible(
        model_name="vendor/exact",
        base_url="https://example.test/v1",
        reasoning_plan=_plan("model:openrouter:vendor/exact", style="openrouter"),
    )
    openai_chat = ChatOpenAICompatible(
        model_name="exact",
        base_url="https://example.test/v1",
        reasoning_plan=_plan("model:custom_openai_exact:exact", style="openai_chat", effort="low"),
    )

    assert openrouter._request_body([HumanMessage(content="hello")], stream=False)["reasoning"] == {
        "effort": "high"
    }
    assert openai_chat._request_body([HumanMessage(content="hello")], stream=False)["reasoning_effort"] == "low"


def test_provider_default_preserves_each_custom_transport_baseline() -> None:
    messages = [HumanMessage(content="hello")]

    assert ChatXAIOAuthResponses(model_name="grok-4")._request_body(messages).get("reasoning") is None
    claude = ChatClaudeSubscriptionMessages(model_name="claude-sonnet-4-6")._request_kwargs(messages)
    assert "output_config" not in claude
    assert "thinking" not in claude
    assert "think" not in ChatOllamaCloud(model_name="gpt-oss:20b", api_key="key")._request_body(
        messages, stream=True
    )


def test_runtime_builds_dynamic_and_unchanged_default_openai_models(monkeypatch) -> None:
    import row_bot.providers.runtime as runtime

    fake_module = ModuleType("langchain_openai")

    class FakeChatOpenAI:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    fake_module.ChatOpenAI = FakeChatOpenAI
    monkeypatch.setitem(sys.modules, "langchain_openai", fake_module)
    monkeypatch.setattr(runtime, "get_provider_secret", lambda _provider: "test-key")
    plan = _plan("model:openai:gpt-5", style="openai", effort="high")

    model = runtime.create_chat_model("gpt-5", "openai", reasoning_plan=plan)

    assert isinstance(model, ReasoningFallbackChatModel)
    assert model.primary.kwargs["reasoning_effort"] == "high"
    assert "reasoning_effort" not in model.provider_default.kwargs
    assert "reasoning" not in model.provider_default.kwargs
