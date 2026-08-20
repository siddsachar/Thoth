from __future__ import annotations

import sqlite3

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage

from row_bot.providers.models import ModelInfo, TransportMode
from row_bot.providers.reasoning import (
    ReasoningCapabilities,
    ReasoningRequestPlan,
    ReasoningSelection,
    reasoning_choices,
    validate_reasoning_selection,
)
from row_bot.providers.transports.reasoning_fallback import (
    ReasoningFallbackChatModel,
    should_retry_reasoning_error,
)


def test_reasoning_metadata_round_trips_through_model_snapshot() -> None:
    caps = ReasoningCapabilities(
        supported_efforts=("low", "high"),
        mandatory=True,
        source="test_catalog",
        request_style="openrouter",
    )
    info = ModelInfo(
        provider_id="openrouter",
        model_id="vendor/exact-model",
        display_name="Exact model",
        context_window=8192,
        transport=TransportMode.OPENAI_CHAT,
        reasoning=caps.to_json(),
    )

    assert info.capability_snapshot()["reasoning"] == caps.to_json()


def test_reasoning_choices_are_exact_and_mandatory_models_omit_off() -> None:
    caps = ReasoningCapabilities(
        supported_efforts=("high", "low"),
        mandatory=True,
        can_disable=True,
        request_style="openai",
    )

    assert [choice.label for choice in reasoning_choices(caps)] == ["Provider default", "Low", "High"]
    with pytest.raises(ValueError, match="cannot be disabled"):
        validate_reasoning_selection(ReasoningSelection(kind="off"), caps)


def test_reasoning_request_plan_fingerprint_isolates_selections() -> None:
    model = "model:openai:gpt-5"
    default = ReasoningRequestPlan(model)
    low = ReasoningRequestPlan(model, ReasoningSelection(kind="effort", effort="low"))
    high = ReasoningRequestPlan(model, ReasoningSelection(kind="effort", effort="high"))

    assert default.fingerprint == "reasoning:provider-default"
    assert len({default.fingerprint, low.fingerprint, high.fingerprint}) == 3


def test_llm_cache_isolated_by_reasoning_fingerprint(monkeypatch) -> None:
    import row_bot.models as models
    import row_bot.providers.runtime as runtime

    caps = ReasoningCapabilities(supported_efforts=("low", "high"), request_style="openai")
    low = ReasoningRequestPlan(
        "model:openai:gpt-5",
        ReasoningSelection(kind="effort", effort="low"),
        caps,
    )
    high = ReasoningRequestPlan(
        "model:openai:gpt-5",
        ReasoningSelection(kind="effort", effort="high"),
        caps,
    )
    created = []
    monkeypatch.setattr(models, "get_cloud_provider", lambda _model: "openai")
    monkeypatch.setattr(models, "get_cloud_model_context", lambda _model: 128_000)
    monkeypatch.setattr(models, "_runtime_model_name", lambda _model: "gpt-5")
    monkeypatch.setattr(
        runtime,
        "create_chat_model",
        lambda model, provider, reasoning_plan=None: created.append(reasoning_plan.fingerprint) or object(),
    )
    models._override_llm_cache.clear()

    low_first = models._get_cloud_llm("model:openai:gpt-5", reasoning_plan=low)
    low_second = models._get_cloud_llm("model:openai:gpt-5", reasoning_plan=low)
    high_model = models._get_cloud_llm("model:openai:gpt-5", reasoning_plan=high)

    assert low_first is low_second
    assert high_model is not low_first
    assert created == [low.fingerprint, high.fingerprint]


def test_thread_reasoning_persistence_is_per_thread_and_exact_model(tmp_path, monkeypatch) -> None:
    import row_bot.threads as threads

    db_path = tmp_path / "threads.db"
    monkeypatch.setattr(threads, "DB_PATH", str(db_path))
    threads._init_thread_db(raise_on_error=True)
    threads._save_thread_meta("thread-a", "A")
    threads._save_thread_meta("thread-b", "B")

    threads.set_thread_reasoning_selection(
        "thread-a", "model:openai:gpt-5", {"kind": "effort", "effort": "low"}
    )
    threads.set_thread_reasoning_selection(
        "thread-a", "model:anthropic:claude-sonnet-4-6", {"kind": "effort", "effort": "high"}
    )

    assert threads.get_thread_reasoning_selection("thread-a", "model:openai:gpt-5") == {
        "kind": "effort",
        "effort": "low",
    }
    assert threads.get_thread_reasoning_selection(
        "thread-a", "model:anthropic:claude-sonnet-4-6"
    ) == {"kind": "effort", "effort": "high"}
    assert threads.get_thread_reasoning_selection("thread-b", "model:openai:gpt-5") is None


def test_legacy_thread_database_migrates_to_empty_reasoning_map(tmp_path, monkeypatch) -> None:
    import row_bot.threads as threads

    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE thread_meta (thread_id TEXT PRIMARY KEY, name TEXT, created_at TEXT, updated_at TEXT)"
        )
        conn.execute("INSERT INTO thread_meta VALUES ('legacy', 'Legacy', '', '')")
        conn.commit()
    monkeypatch.setattr(threads, "DB_PATH", str(db_path))

    threads._init_thread_db(raise_on_error=True)

    assert threads.get_thread_reasoning_selections("legacy") == {}


def test_combined_chat_controls_save_model_approval_profile_and_reasoning_atomically(tmp_path, monkeypatch) -> None:
    import row_bot.threads as threads

    db_path = tmp_path / "controls.db"
    monkeypatch.setattr(threads, "DB_PATH", str(db_path))
    threads._init_thread_db(raise_on_error=True)
    threads._save_thread_meta("mobile", "Mobile")

    threads.set_thread_chat_controls(
        "mobile",
        model_override="model:openai:gpt-5",
        approval_mode="approve",
        profile_id_or_slug="",
        reasoning_model_ref="model:openai:gpt-5",
        reasoning_selection={"kind": "effort", "effort": "high"},
    )

    assert threads._get_thread_model_override("mobile") == "model:openai:gpt-5"
    assert threads._get_thread_approval_mode("mobile") == "approve"
    assert threads.get_thread_reasoning_selection("mobile", "model:openai:gpt-5") == {
        "kind": "effort",
        "effort": "high",
    }


def test_reasoning_command_is_strict_and_persists_for_active_exact_model(tmp_path, monkeypatch) -> None:
    import row_bot.threads as threads
    from row_bot.providers.reasoning import apply_reasoning_command

    db_path = tmp_path / "commands.db"
    monkeypatch.setattr(threads, "DB_PATH", str(db_path))
    threads._init_thread_db(raise_on_error=True)
    threads._save_thread_meta("chat", "Chat")
    model = "model:openai:gpt-5"

    assert "Valid choices" in apply_reasoning_command("chat", model, "")
    assert apply_reasoning_command("chat", model, "high") == f"Reasoning for {model}: High."
    assert threads.get_thread_reasoning_selection("chat", model) == {
        "kind": "effort",
        "effort": "high",
    }
    assert "not supported" in apply_reasoning_command("chat", model, "xhigh")
    assert threads.get_thread_reasoning_selection("chat", model)["effort"] == "high"
    assert apply_reasoning_command("chat", model, "default") == (
        f"Reasoning for {model}: Provider default."
    )
    assert threads.get_thread_reasoning_selection("chat", model) is None


@pytest.mark.parametrize(
    ("provider", "selection", "expected"),
    [
        ("openrouter", ReasoningSelection(kind="effort", effort="high"), {"reasoning": {"effort": "high"}}),
        ("openrouter", ReasoningSelection(kind="off"), {"reasoning": {"enabled": False}}),
        ("openrouter", ReasoningSelection(kind="budget", budget=4096), {"reasoning": {"max_tokens": 4096}}),
        ("openai", ReasoningSelection(kind="effort", effort="low"), {"reasoning_effort": "low"}),
        ("xai", ReasoningSelection(kind="effort", effort="high"), {"reasoning_effort": "high"}),
        ("google", ReasoningSelection(kind="budget", budget=2048), {"thinking_budget": 2048}),
        ("ollama", ReasoningSelection(kind="effort", effort="medium"), {"reasoning": "medium"}),
    ],
)
def test_transport_constructor_mapping_is_provider_native(provider, selection, expected) -> None:
    from row_bot.providers.runtime import _reasoning_constructor_kwargs

    caps = ReasoningCapabilities(
        supported_efforts=(selection.effort,) if selection.effort else (),
        can_disable=selection.kind == "off",
        supports_budget=selection.kind == "budget",
        budget_max=8192,
        request_style=provider,
    )
    plan = ReasoningRequestPlan(f"model:{provider}:exact", selection, caps)

    assert _reasoning_constructor_kwargs(plan, provider=provider) == expected


def test_anthropic_effort_enables_only_exact_adaptive_thinking() -> None:
    from row_bot.providers.runtime import _reasoning_constructor_kwargs

    caps = ReasoningCapabilities(
        supported_efforts=("high",),
        thinking_mode="adaptive",
        request_style="anthropic",
    )
    plan = ReasoningRequestPlan(
        "model:anthropic:claude-sonnet-4-6",
        ReasoningSelection(kind="effort", effort="high"),
        caps,
    )

    assert _reasoning_constructor_kwargs(plan, provider="anthropic") == {
        "effort": "high",
        "thinking": {"type": "adaptive"},
    }


class _ReasoningRequestError(RuntimeError):
    status_code = 400


class _FakeRunnable:
    def __init__(self, events=None, error=None):
        self.events = list(events or [])
        self.error = error
        self.calls = 0

    def stream(self, messages, **kwargs):
        self.calls += 1
        for event in self.events:
            yield event
        if self.error:
            raise self.error

    async def astream(self, messages, **kwargs):
        for event in self.stream(messages, **kwargs):
            yield event

    def invoke(self, messages, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return AIMessage(content="fallback")

    async def ainvoke(self, messages, **kwargs):
        return self.invoke(messages, **kwargs)

    def bind_tools(self, tools, **kwargs):
        return self


def _fallback_model(primary, provider_default) -> ReasoningFallbackChatModel:
    caps = ReasoningCapabilities(supported_efforts=("low",), request_style="openai")
    return ReasoningFallbackChatModel(
        primary=primary,
        provider_default=provider_default,
        plan=ReasoningRequestPlan(
            "model:openai:gpt-5",
            ReasoningSelection(kind="effort", effort="low"),
            caps,
        ),
        provider_id="openai",
    )


def test_fallback_retries_once_before_any_output(monkeypatch, caplog) -> None:
    import row_bot.providers.transports.reasoning_fallback as fallback_module

    primary = _FakeRunnable(error=_ReasoningRequestError("HTTP 400: reasoning effort is unsupported"))
    provider_default = _FakeRunnable(events=[AIMessageChunk(content="fallback")])
    resets = []
    monkeypatch.setattr(fallback_module, "_active_thread_id", lambda: "thread-a")
    monkeypatch.setattr("row_bot.threads.set_thread_reasoning_selection", lambda *args: resets.append(args))

    chunks = list(_fallback_model(primary, provider_default)._stream([HumanMessage(content="private prompt")]))

    assert "".join(str(chunk.message.content) for chunk in chunks) == "fallback"
    assert primary.calls == provider_default.calls == 1
    assert resets == [("thread-a", "model:openai:gpt-5", None)]
    assert "private prompt" not in caplog.text
    assert "Bearer" not in caplog.text


def test_fallback_never_retries_after_any_stream_output() -> None:
    primary = _FakeRunnable(
        events=[AIMessageChunk(content="partial")],
        error=_ReasoningRequestError("HTTP 400: reasoning effort is unsupported"),
    )
    provider_default = _FakeRunnable(events=[AIMessageChunk(content="duplicate")])

    with pytest.raises(_ReasoningRequestError):
        list(_fallback_model(primary, provider_default)._stream([HumanMessage(content="prompt")]))

    assert provider_default.calls == 0


def test_fallback_treats_empty_metadata_chunk_as_output() -> None:
    primary = _FakeRunnable(
        events=[AIMessageChunk(content="", response_metadata={"usage": {"input_tokens": 1}})],
        error=_ReasoningRequestError("HTTP 422: thinking budget is unsupported"),
    )
    provider_default = _FakeRunnable(events=[AIMessageChunk(content="duplicate")])

    with pytest.raises(_ReasoningRequestError):
        list(_fallback_model(primary, provider_default)._stream([HumanMessage(content="prompt")]))

    assert provider_default.calls == 0


def test_fallback_failure_does_not_recurse(monkeypatch) -> None:
    import row_bot.providers.transports.reasoning_fallback as fallback_module

    primary = _FakeRunnable(error=_ReasoningRequestError("HTTP 400: reasoning effort is unsupported"))
    provider_default = _FakeRunnable(error=RuntimeError("HTTP 503: unavailable"))
    resets = []
    monkeypatch.setattr(fallback_module, "_active_thread_id", lambda: "thread-a")
    monkeypatch.setattr("row_bot.threads.set_thread_reasoning_selection", lambda *args: resets.append(args))

    with pytest.raises(RuntimeError, match="503"):
        list(_fallback_model(primary, provider_default)._stream([HumanMessage(content="prompt")]))

    assert primary.calls == provider_default.calls == 1
    assert resets == []


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("HTTP 400: invalid tools"),
        RuntimeError("HTTP 401: reasoning unauthorized"),
        RuntimeError("HTTP 429: reasoning rate limit"),
        RuntimeError("HTTP 500: reasoning backend failure"),
        TimeoutError("reasoning timed out"),
    ],
)
def test_fallback_classifier_rejects_broad_or_non_validation_errors(error) -> None:
    assert should_retry_reasoning_error(error) is False
