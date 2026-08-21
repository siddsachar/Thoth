import row_bot.providers.config as provider_config
from row_bot.providers.custom import custom_provider_id, save_custom_endpoint
from row_bot.providers.models import TransportMode
from row_bot.providers.ollama import ollama_model_info
from row_bot.providers.readiness import (
    AGENT_MODE_MIN_CONTEXT,
    CHAT_ONLY_MIN_CONTEXT,
    evaluate_agent_readiness,
    evaluate_chat_readiness,
    evaluate_runtime_readiness,
)
from row_bot.providers.resolution import resolve_provider_config
from types import SimpleNamespace


def test_known_cloud_model_passes_readiness_without_live_probe(monkeypatch):
    import row_bot.providers.readiness as readiness
    import row_bot.models as models

    monkeypatch.setattr(readiness, "provider_status", lambda provider_id: {"configured": True})
    monkeypatch.setattr(models, "get_context_policy", lambda value: models.ContextPolicy(
        model_ref="model:openai:gpt-4o",
        provider_id="openai",
        runtime_model="gpt-4o",
        native_max=128_000,
        user_cap=128_000,
        effective_context=128_000,
        policy_kind="provider",
        cap_source="provider_metadata",
        request_application="trim_only",
    ))

    result = evaluate_agent_readiness("model:openai:gpt-4o")

    assert result.ready is True
    assert result.required_context == AGENT_MODE_MIN_CONTEXT
    assert result.tool_calling is True
    assert result.tool_round_trip is True
    assert result.capability_source in {"trusted_provider", "catalog"}


def test_unknown_auto_capacity_blocks_agent_and_chat_with_all_remedies(monkeypatch):
    import row_bot.providers.readiness as readiness
    import row_bot.models as models

    monkeypatch.setattr(readiness, "provider_status", lambda provider_id, **kwargs: {"configured": True})
    monkeypatch.setattr(models, "get_context_policy", lambda value: models.ContextPolicy(
        model_ref="model:openai:opaque-model",
        provider_id="openai",
        runtime_model="opaque-model",
        native_max=None,
        user_cap=0,
        effective_context=0,
        policy_kind="provider",
        cap_source="unknown",
        request_application="trim_only",
        native_limit_tokens=None,
        requested_limit_tokens=None,
        effective_limit_tokens=None,
        capacity_state="unavailable",
    ))

    agent_result = evaluate_agent_readiness("model:openai:opaque-model")
    chat_result = evaluate_chat_readiness("model:openai:opaque-model")

    for result in (agent_result, chat_result):
        assert result.ready is False
        message = result.user_message()
        assert "Refresh the provider catalog" in message
        assert "Advanced context override" in message
        assert "known-capacity model" in message


def test_unknown_native_explicit_override_is_ready(monkeypatch):
    import row_bot.providers.readiness as readiness
    import row_bot.models as models

    monkeypatch.setattr(readiness, "provider_status", lambda provider_id, **kwargs: {"configured": True})
    monkeypatch.setattr(models, "get_context_policy", lambda value: models.ContextPolicy(
        model_ref="model:openai:opaque-model",
        provider_id="openai",
        runtime_model="opaque-model",
        native_max=None,
        user_cap=262_144,
        effective_context=262_144,
        policy_kind="provider",
        cap_source="advanced_override",
        request_application="trim_only",
        native_limit_tokens=None,
        requested_limit_tokens=262_144,
        effective_limit_tokens=262_144,
        capacity_source="advanced_override",
        capacity_state="ready",
    ))

    assert evaluate_agent_readiness("model:openai:opaque-model").ready is True
    assert evaluate_chat_readiness("model:openai:opaque-model").ready is True


def test_claude_subscription_passes_agent_readiness_after_native_tool_transport(monkeypatch):
    import row_bot.providers.readiness as readiness
    import row_bot.models as models
    from row_bot.providers.claude_subscription import fallback_claude_subscription_model_infos

    model_info = next(info for info in fallback_claude_subscription_model_infos() if info.model_id == "claude-sonnet-4-6")
    monkeypatch.setattr(readiness, "provider_status", lambda provider_id: {"configured": True, "runtime_enabled": True})
    monkeypatch.setattr(models, "get_context_policy", lambda value: models.ContextPolicy(
        model_ref="model:claude_subscription:claude-sonnet-4-6",
        provider_id="claude_subscription",
        runtime_model="claude-sonnet-4-6",
        native_max=1_000_000,
        user_cap=128_000,
        effective_context=128_000,
        policy_kind="provider",
        cap_source="provider_metadata",
        request_application="trim_only",
    ))

    result = evaluate_agent_readiness(
        "model:claude_subscription:claude-sonnet-4-6",
        capability_snapshot=model_info.capability_snapshot(),
    )

    assert result.ready is True
    assert result.tool_calling is True
    assert result.tool_round_trip is True
    assert result.tool_calling_source == "trusted_provider"


def test_claude_subscription_failed_runtime_probe_blocks_agent_readiness(monkeypatch):
    import row_bot.models as models
    from row_bot.providers.claude_subscription import fallback_claude_subscription_model_infos

    model_info = next(info for info in fallback_claude_subscription_model_infos() if info.model_id == "claude-sonnet-4-6")
    monkeypatch.setattr(models, "get_context_policy", lambda value: models.ContextPolicy(
        model_ref="model:claude_subscription:claude-sonnet-4-6",
        provider_id="claude_subscription",
        runtime_model="claude-sonnet-4-6",
        native_max=1_000_000,
        user_cap=128_000,
        effective_context=128_000,
        policy_kind="provider",
        cap_source="provider_metadata",
        request_application="trim_only",
    ))

    result = evaluate_runtime_readiness(
        "model:claude_subscription:claude-sonnet-4-6",
        capability_snapshot=model_info.capability_snapshot(),
        status={
            "configured": True,
            "runtime_enabled": True,
            "last_runtime_probe": {
                "ok": False,
                "chat_ok": False,
                "tool_calling": False,
                "tool_round_trip": False,
                "errors": ["Claude subscription rate/usage limit reached"],
            },
        },
    )

    assert result.agent.ready is False
    assert result.chat.ready is False
    assert result.selected_mode == "blocked"
    assert any("runtime probe failed" in error for error in result.agent.errors)


def test_context_below_32k_blocks_agent_mode(monkeypatch):
    import row_bot.providers.readiness as readiness
    import row_bot.models as models

    monkeypatch.setattr(readiness, "provider_status", lambda provider_id: {"configured": True})
    monkeypatch.setattr(models, "get_context_policy", lambda value: models.ContextPolicy(
        model_ref="model:openai:gpt-4",
        provider_id="openai",
        runtime_model="gpt-4",
        native_max=8_192,
        user_cap=128_000,
        effective_context=8_192,
        policy_kind="provider",
        cap_source="provider_metadata",
        request_application="trim_only",
    ))

    result = evaluate_agent_readiness("model:openai:gpt-4")

    assert result.ready is False
    assert any("at least 32,000" in error for error in result.errors)


def test_context_above_16k_below_32k_can_be_chat_only(monkeypatch):
    import row_bot.providers.readiness as readiness
    import row_bot.models as models

    monkeypatch.setattr(readiness, "provider_status", lambda provider_id: {"configured": True})
    monkeypatch.setattr(models, "get_context_policy", lambda value: models.ContextPolicy(
        model_ref="model:openai:gpt-3.5-turbo",
        provider_id="openai",
        runtime_model="gpt-3.5-turbo",
        native_max=CHAT_ONLY_MIN_CONTEXT,
        user_cap=128_000,
        effective_context=CHAT_ONLY_MIN_CONTEXT,
        policy_kind="provider",
        cap_source="provider_metadata",
        request_application="trim_only",
    ))

    result = evaluate_runtime_readiness("model:openai:gpt-3.5-turbo")

    assert result.agent.ready is False
    assert result.chat.ready is True
    assert result.selected_mode == "chat_only"


def test_readiness_coerces_string_context_override(monkeypatch):
    import row_bot.providers.readiness as readiness

    monkeypatch.setattr(readiness, "provider_status", lambda provider_id: {"configured": True})

    result = evaluate_agent_readiness(
        "model:openai:gpt-4o",
        context_window_override="32768",
    )

    assert result.ready is True
    assert result.context_window == 32_768
    assert isinstance(result.context_window, int)


def test_openrouter_missing_tool_metadata_fails_closed(monkeypatch):
    import row_bot.providers.readiness as readiness
    import row_bot.models as models

    monkeypatch.setattr(readiness, "provider_status", lambda provider_id: {"configured": True})
    monkeypatch.setattr(models, "get_context_policy", lambda value: models.ContextPolicy(
        model_ref="model:openrouter:unknown/vendor",
        provider_id="openrouter",
        runtime_model="unknown/vendor",
        native_max=128_000,
        user_cap=128_000,
        effective_context=128_000,
        policy_kind="provider",
        cap_source="provider_metadata",
        request_application="trim_only",
    ))

    result = evaluate_agent_readiness(
        resolve_provider_config("model:openrouter:unknown/vendor"),
        capability_snapshot={
            "tasks": ["chat"],
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "tool_calling": None,
            "transport": TransportMode.OPENAI_CHAT.value,
        },
    )

    assert result.ready is False
    assert result.tool_calling is None
    assert any("OpenRouter tool metadata" in error for error in result.errors)


def test_openrouter_missing_tool_metadata_can_be_chat_only(monkeypatch):
    import row_bot.providers.readiness as readiness
    import row_bot.models as models

    monkeypatch.setattr(readiness, "provider_status", lambda provider_id: {"configured": True})
    monkeypatch.setattr(models, "get_context_policy", lambda value: models.ContextPolicy(
        model_ref="model:openrouter:unknown/vendor",
        provider_id="openrouter",
        runtime_model="unknown/vendor",
        native_max=128_000,
        user_cap=128_000,
        effective_context=128_000,
        policy_kind="provider",
        cap_source="provider_metadata",
        request_application="trim_only",
    ))

    result = evaluate_runtime_readiness(
        resolve_provider_config("model:openrouter:unknown/vendor"),
        capability_snapshot={
            "tasks": ["chat"],
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "tool_calling": None,
            "transport": TransportMode.OPENAI_CHAT.value,
        },
    )

    assert result.agent.ready is False
    assert result.chat.ready is True
    assert result.selected_mode == "chat_only"


def test_openrouter_cached_tool_metadata_is_used_for_runtime_readiness(monkeypatch):
    import row_bot.providers.readiness as readiness
    import row_bot.models as models

    model_id = "qwen/qwen3.7-max"
    monkeypatch.setattr(readiness, "provider_status", lambda provider_id: {"configured": True})
    monkeypatch.setattr(models, "get_context_policy", lambda value: models.ContextPolicy(
        model_ref=f"model:openrouter:{model_id}",
        provider_id="openrouter",
        runtime_model=model_id,
        native_max=1_000_000,
        user_cap=1_000_000,
        effective_context=1_000_000,
        policy_kind="provider",
        cap_source="provider_metadata",
        request_application="trim_only",
    ))
    monkeypatch.setitem(models._cloud_model_cache, model_id, {
        "label": "Qwen: Qwen3.7 Max",
        "ctx": 1_000_000,
        "provider": "openrouter",
        "capabilities_snapshot": {
            "tasks": ["chat"],
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "tool_calling": True,
            "streaming": True,
            "transport": TransportMode.OPENAI_CHAT.value,
            "endpoint_compatibility": [TransportMode.OPENAI_CHAT.value],
        },
    })

    result = evaluate_runtime_readiness(resolve_provider_config(f"model:openrouter:{model_id}"))

    assert result.selected_mode == "agent"
    assert result.agent.ready is True
    assert result.agent.tool_calling is True
    assert result.agent.tool_calling_source == "openrouter_metadata"


def test_ollama_probe_can_promote_catalog_unknown_model_to_agent(monkeypatch):
    import row_bot.providers.ollama as ollama
    import row_bot.providers.readiness as readiness
    import row_bot.models as models

    monkeypatch.setattr(readiness, "provider_status", lambda provider_id: {"configured": True})
    monkeypatch.setattr(models, "get_context_policy", lambda value: models.ContextPolicy(
        model_ref="model:ollama:gemma3:4b",
        provider_id="ollama",
        runtime_model="gemma3:4b",
        native_max=131_072,
        user_cap=65_536,
        effective_context=65_536,
        policy_kind="local",
        cap_source="provider_metadata",
        request_application="ollama_num_ctx",
    ))
    monkeypatch.setattr(ollama, "probe_ollama_tool_round_trip", lambda model_id: {
        "ok": True,
        "tool_calling": True,
        "tool_round_trip": True,
    })

    result = evaluate_agent_readiness("model:ollama:gemma3:4b", probe_ollama_tools=True)

    assert result.ready is True
    assert result.tool_calling_source == "ollama_probe"
    assert result.capability_source == "probe"


def test_ollama_native_tool_metadata_is_agent_ready_without_probe(monkeypatch):
    import row_bot.providers.ollama as ollama

    model_info = ollama_model_info(
        "qwen3.8:27b",
        installed=True,
        context_window=65_536,
        metadata={"capabilities": ["completion", "vision", "tools", "thinking"]},
    )
    monkeypatch.setattr(
        ollama,
        "probe_ollama_tool_round_trip",
        lambda model_id: (_ for _ in ()).throw(AssertionError("unexpected live Ollama probe")),
    )

    result = evaluate_runtime_readiness(
        "model:ollama:qwen3.8:27b",
        capability_snapshot=model_info.capability_snapshot(),
        status={"configured": True},
        context_window_override=65_536,
    )

    assert result.agent.ready is True
    assert result.agent.tool_calling is True
    assert result.agent.tool_calling_source == "ollama_catalog_hint"
    assert result.selected_mode == "agent"


def test_ollama_readiness_rechecks_observed_loaded_context(monkeypatch):
    import row_bot.providers.ollama as ollama
    import row_bot.providers.readiness as readiness
    import row_bot.models as models

    observed = {"loaded": False}
    monkeypatch.setattr(readiness, "provider_status", lambda provider_id: {"configured": True})

    def context_policy(_value):
        effective = 24_576 if observed["loaded"] else 32_768
        return models.ContextPolicy(
            model_ref="model:ollama:gemma3:4b",
            provider_id="ollama",
            runtime_model="gemma3:4b",
            native_max=131_072,
            user_cap=32_768,
            effective_context=effective,
            policy_kind="local",
            cap_source="observed_allocation" if observed["loaded"] else "provider_metadata",
            request_application="ollama_num_ctx",
            effective_limit_tokens=effective,
            warning="Loaded allocation is below 32K." if observed["loaded"] else "",
        )

    monkeypatch.setattr(models, "get_context_policy", context_policy)
    monkeypatch.setattr(
        models,
        "refresh_observed_local_context_after_load",
        lambda model_id: observed.update(loaded=True) or 24_576,
    )
    monkeypatch.setattr(ollama, "probe_ollama_tool_round_trip", lambda model_id: {
        "ok": True,
        "tool_calling": True,
        "tool_round_trip": True,
    })

    result = evaluate_agent_readiness(
        "model:ollama:gemma3:4b",
        probe_ollama_tools=True,
    )

    assert result.ready is False
    assert result.context_window == 24_576
    assert any("requires at least 32,000" in error for error in result.errors)
    assert "Loaded allocation is below 32K." in result.warnings


def test_ollama_probe_failure_routes_to_chat_only(monkeypatch):
    import row_bot.providers.ollama as ollama
    import row_bot.providers.readiness as readiness
    import row_bot.models as models

    monkeypatch.setattr(readiness, "provider_status", lambda provider_id: {"configured": True})
    monkeypatch.setattr(models, "get_context_policy", lambda value: models.ContextPolicy(
        model_ref="model:ollama:gemma3:4b",
        provider_id="ollama",
        runtime_model="gemma3:4b",
        native_max=131_072,
        user_cap=65_536,
        effective_context=65_536,
        policy_kind="local",
        cap_source="provider_metadata",
        request_application="ollama_num_ctx",
    ))
    monkeypatch.setattr(ollama, "probe_ollama_tool_round_trip", lambda model_id: {
        "ok": False,
        "tool_calling": False,
        "tool_round_trip": False,
        "error": "model did not emit a tool call",
    })

    result = evaluate_runtime_readiness("model:ollama:gemma3:4b", probe_ollama_tools=True)

    assert result.agent.ready is False
    assert result.chat.ready is True
    assert result.selected_mode == "chat_only"
    assert any("tool probe" in error.lower() for error in result.agent.errors)


def test_ollama_probe_timeout_blocks_instead_of_chat_only(monkeypatch):
    import row_bot.providers.ollama as ollama
    import row_bot.providers.readiness as readiness
    import row_bot.models as models

    monkeypatch.setattr(readiness, "provider_status", lambda provider_id: {"configured": True})
    monkeypatch.setattr(models, "get_context_policy", lambda value: models.ContextPolicy(
        model_ref="model:ollama:qwen3.6:27b",
        provider_id="ollama",
        runtime_model="qwen3.6:27b",
        native_max=131_072,
        user_cap=65_536,
        effective_context=65_536,
        policy_kind="local",
        cap_source="provider_metadata",
        request_application="ollama_num_ctx",
    ))
    monkeypatch.setattr(ollama, "probe_ollama_tool_round_trip", lambda model_id: {
        "ok": False,
        "tool_calling": False,
        "tool_round_trip": False,
        "error": "timed out",
    })

    result = evaluate_runtime_readiness("model:ollama:qwen3.6:27b", probe_ollama_tools=True)

    assert result.agent.ready is False
    assert result.chat.ready is True
    assert result.selected_mode == "blocked"
    assert "timed out" in result.selection_reason.lower()


def test_ollama_tool_probe_requires_tool_call_and_round_trip(monkeypatch):
    import sys
    import row_bot.models as models
    import row_bot.providers.ollama as ollama

    calls = []

    class Response:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload
            self.text = str(payload)

        def json(self):
            return self._payload

    def post(url, json, timeout):
        calls.append(json)
        if len(calls) == 1:
            return Response(200, {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"function": {"name": "row_bot_probe", "arguments": {"value": "ok"}}}],
                }
            })
        return Response(200, {"message": {"role": "assistant", "content": "ok"}})

    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(post=post))
    monkeypatch.setattr(models, "_ollama_base_url", lambda: "http://127.0.0.1:11434")
    ollama._tool_probe_cache.clear()

    result = ollama.probe_ollama_tool_round_trip("gemma3:4b", force=True)

    assert result["ok"] is True
    assert result["tool_calling"] is True
    assert result["tool_round_trip"] is True
    assert calls[0]["tools"][0]["function"]["name"] == "row_bot_probe"
    assert calls[1]["messages"][1]["tool_calls"]
    assert calls[1]["messages"][2]["role"] == "tool"


def test_custom_endpoint_requires_tool_probe_even_with_manual_capability(tmp_path, monkeypatch):
    import row_bot.providers.readiness as readiness
    import row_bot.models as models

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(readiness, "provider_status", lambda provider_id: {"configured": True})
    provider_id = custom_provider_id("lab")
    save_custom_endpoint({
        "id": "lab",
        "base_url": "http://127.0.0.1:8000/v1",
        "auth_required": False,
        "manual_capabilities": {"tool_calling": True, "context_window": 65_536},
        "models": [{
            "id": "local-chat",
            "model_id": "local-chat",
            "context_window": 65_536,
            "capabilities_snapshot": {
                "tasks": ["chat"],
                "input_modalities": ["text"],
                "output_modalities": ["text"],
                "tool_calling": True,
            },
        }],
    })
    monkeypatch.setattr(models, "get_context_policy", lambda value: models.ContextPolicy(
        model_ref=f"model:{provider_id}:local-chat",
        provider_id=provider_id,
        runtime_model="local-chat",
        native_max=65_536,
        user_cap=65_536,
        effective_context=65_536,
        policy_kind="local",
        cap_source="provider_metadata",
        request_application="trim_only",
    ))

    result = evaluate_agent_readiness(f"model:{provider_id}:local-chat")

    assert result.ready is False
    assert any("probe" in error.lower() for error in result.errors)


def test_custom_endpoint_successful_tool_round_trip_probe_passes(tmp_path, monkeypatch):
    import row_bot.providers.readiness as readiness
    import row_bot.models as models

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(readiness, "provider_status", lambda provider_id: {"configured": True})
    provider_id = custom_provider_id("lab")
    save_custom_endpoint({
        "id": "lab",
        "base_url": "http://127.0.0.1:8000/v1",
        "auth_required": False,
        "last_probe": {
            "ok": True,
            "tool_calling": True,
            "tool_round_trip": True,
            "context_window": 65_536,
        },
        "models": [{
            "id": "local-chat",
            "model_id": "local-chat",
            "context_window": 65_536,
            "capabilities_snapshot": {
                "tasks": ["chat"],
                "input_modalities": ["text"],
                "output_modalities": ["text"],
                "tool_calling": None,
            },
        }],
    })
    monkeypatch.setattr(models, "get_context_policy", lambda value: models.ContextPolicy(
        model_ref=f"model:{provider_id}:local-chat",
        provider_id=provider_id,
        runtime_model="local-chat",
        native_max=65_536,
        user_cap=65_536,
        effective_context=65_536,
        policy_kind="local",
        cap_source="provider_metadata",
        request_application="trim_only",
    ))

    result = evaluate_agent_readiness(f"model:{provider_id}:local-chat")

    assert result.ready is True
    assert result.tool_calling is True
    assert result.tool_round_trip is True
    assert result.streaming_tool_calling is None
    assert result.tool_calling_source == "probe"
    assert any("non-stream fallback" in warning for warning in result.warnings)


def test_custom_endpoint_probe_for_different_model_does_not_grant_agent_readiness(tmp_path, monkeypatch):
    import row_bot.providers.readiness as readiness
    import row_bot.models as models

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(readiness, "provider_status", lambda provider_id: {"configured": True})
    provider_id = custom_provider_id("lab")
    save_custom_endpoint({
        "id": "lab",
        "base_url": "http://127.0.0.1:8000/v1",
        "auth_required": False,
        "last_probe": {
            "model_id": "other-chat",
            "ok": True,
            "chat_ok": True,
            "tool_calling": True,
            "tool_round_trip": True,
        },
        "models": [
            {"model_id": "local-chat", "context_window": 65_536},
            {"model_id": "other-chat", "context_window": 65_536},
        ],
    })
    monkeypatch.setattr(models, "get_context_policy", lambda value: models.ContextPolicy(
        model_ref=f"model:{provider_id}:local-chat",
        provider_id=provider_id,
        runtime_model="local-chat",
        native_max=65_536,
        user_cap=65_536,
        effective_context=65_536,
        policy_kind="local",
        cap_source="provider_metadata",
        request_application="trim_only",
    ))

    result = evaluate_agent_readiness(f"model:{provider_id}:local-chat")

    assert result.ready is False
    assert result.tool_calling is None
    assert result.tool_calling_source == "missing_probe"
    assert any("endpoint probe" in error for error in result.errors)


def test_custom_endpoint_streamed_tool_probe_is_exposed_in_readiness(tmp_path, monkeypatch):
    import row_bot.providers.readiness as readiness
    import row_bot.models as models

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(readiness, "provider_status", lambda provider_id: {"configured": True})
    provider_id = custom_provider_id("lab")
    save_custom_endpoint({
        "id": "lab",
        "base_url": "http://127.0.0.1:8000/v1",
        "auth_required": False,
        "last_probe": {
            "ok": True,
            "tool_calling": True,
            "tool_round_trip": True,
            "streaming_ok": True,
            "streaming_tool_calling": True,
            "context_window": 65_536,
        },
        "models": [{
            "id": "local-chat",
            "model_id": "local-chat",
            "context_window": 65_536,
            "capabilities_snapshot": {
                "tasks": ["chat"],
                "input_modalities": ["text"],
                "output_modalities": ["text"],
                "tool_calling": None,
            },
        }],
    })
    monkeypatch.setattr(models, "get_context_policy", lambda value: models.ContextPolicy(
        model_ref=f"model:{provider_id}:local-chat",
        provider_id=provider_id,
        runtime_model="local-chat",
        native_max=65_536,
        user_cap=65_536,
        effective_context=65_536,
        policy_kind="local",
        cap_source="provider_metadata",
        request_application="trim_only",
    ))

    result = evaluate_agent_readiness(f"model:{provider_id}:local-chat")

    assert result.ready is True
    assert result.streaming is True
    assert result.streaming_tool_calling is True
    assert not any("non-stream fallback" in warning for warning in result.warnings)


def test_custom_endpoint_chat_probe_without_tools_is_chat_only(tmp_path, monkeypatch):
    import row_bot.providers.readiness as readiness
    import row_bot.models as models

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(readiness, "provider_status", lambda provider_id: {"configured": True})
    provider_id = custom_provider_id("lab")
    save_custom_endpoint({
        "id": "lab",
        "base_url": "http://127.0.0.1:8000/v1",
        "auth_required": False,
        "last_probe": {
            "ok": False,
            "agent_ok": False,
            "chat_only_ok": True,
            "classification": "chat_only",
            "chat_ok": True,
            "tool_calling": False,
            "tool_round_trip": None,
            "context_window": 65_536,
        },
        "models": [{
            "id": "local-chat",
            "model_id": "local-chat",
            "context_window": 65_536,
            "capabilities_snapshot": {
                "tasks": ["chat"],
                "input_modalities": ["text"],
                "output_modalities": ["text"],
                "tool_calling": None,
            },
        }],
    })
    monkeypatch.setattr(models, "get_context_policy", lambda value: models.ContextPolicy(
        model_ref=f"model:{provider_id}:local-chat",
        provider_id=provider_id,
        runtime_model="local-chat",
        native_max=65_536,
        user_cap=65_536,
        effective_context=65_536,
        policy_kind="local",
        cap_source="provider_metadata",
        request_application="trim_only",
    ))

    result = evaluate_runtime_readiness(f"model:{provider_id}:local-chat")

    assert result.agent.ready is False
    assert result.chat.ready is True
    assert result.selected_mode == "chat_only"
