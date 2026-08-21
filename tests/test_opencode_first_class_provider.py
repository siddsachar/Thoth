import copy
import importlib
import json
import sys
from pathlib import Path
from types import ModuleType

import httpx
import pytest

import row_bot.models as models
import row_bot.providers.runtime as runtime
import row_bot.providers.config as provider_config
from row_bot.providers import opencode as opencode_provider
from row_bot.cancellation import CancellationScope, use_cancellation_scope
from row_bot.providers.auth_store import get_provider_secret, provider_secret_status
from row_bot.providers.catalog import PROVIDER_DEFINITIONS, classify_model_capabilities, infer_provider_id, legacy_cache_to_model_infos
from row_bot.providers.model_catalog import build_model_catalog_rows
from row_bot.providers.models import AuthMethod, TransportMode
from row_bot.providers.opencode import (
    OpenCodeUnsupportedRouteError,
    list_opencode_model_infos,
    opencode_known_route,
    opencode_failure_diagnostics,
    opencode_model_route,
    opencode_model_transport,
    opencode_route_diagnostics,
)
from row_bot.providers.transports.anthropic_cancellable import CancellableChatAnthropic


class _CloseCountingStream(httpx.SyncByteStream):
    def __init__(self) -> None:
        self.close_calls = 0

    def __iter__(self):
        return iter(())

    def close(self) -> None:
        self.close_calls += 1


def _assert_sync_client_registered_with_cancellation_scope(client: httpx.Client) -> None:
    stream = _CloseCountingStream()
    response = httpx.Response(200, request=httpx.Request("GET", "https://example.test"), stream=stream)
    scope = CancellationScope()

    with use_cancellation_scope(scope):
        for hook in client.event_hooks["response"]:
            hook(response)

    assert stream.close_calls == 0
    scope.cancel("test")
    assert stream.close_calls == 1
from row_bot.providers.readiness import evaluate_agent_readiness, evaluate_runtime_readiness
from row_bot.providers.resolution import resolve_provider_config
from row_bot.providers.selection import ModelSelectionError, add_quick_choice_for_model, canonicalize_model_selection, list_quick_choices, model_choice_value


EXISTING_PROVIDER_IDS = (
    "ollama",
    "ollama_cloud",
    "openai",
    "codex",
    "openrouter",
    "anthropic",
    "google",
    "xai",
    "minimax",
)


def _native_opencode_registry() -> dict:
    return {
        "opencode": {
            "id": "opencode",
            "name": "OpenCode Zen",
            "npm": "@ai-sdk/openai-compatible",
            "models": {
                "x-preview-f-free": {
                    "id": "x-preview-f-free",
                    "name": "Ox Alpha Free",
                    "provider": {"npm": "@ai-sdk/openai-compatible"},
                    "tool_call": True,
                    "reasoning": True,
                    "limit": {"context": 200_000, "output": 64_000},
                    "modalities": {"input": ["text", "image"], "output": ["text"]},
                },
                "gemini-3-flash": {
                    "id": "gemini-3-flash",
                    "name": "Gemini 3 Flash",
                    "provider": {"npm": "@ai-sdk/google"},
                    "tool_call": True,
                    "reasoning": True,
                    "limit": {"context": 1_048_576, "output": 65_536},
                    "modalities": {"input": ["text", "image"], "output": ["text"]},
                },
                "registry-only": {
                    "id": "registry-only",
                    "name": "Registry Only",
                    "provider": {"npm": "@ai-sdk/openai-compatible"},
                    "tool_call": True,
                    "limit": {"context": 32_768, "output": 4_096},
                    "modalities": {"input": ["text"], "output": ["text"]},
                },
                "gpt-5.5": {
                    "id": "gpt-5.5",
                    "name": "GPT 5.5",
                    "provider": {"npm": "@ai-sdk/openai"},
                },
                "gpt-5.6": {
                    "id": "gpt-5.6",
                    "name": "GPT 5.6",
                    "provider": {"npm": "@ai-sdk/openai"},
                },
                "claude-sonnet-4-5": {
                    "id": "claude-sonnet-4-5",
                    "name": "Claude Sonnet 4.5",
                    "provider": {"npm": "@ai-sdk/anthropic"},
                },
                "qwen3.6-plus": {
                    "id": "qwen3.6-plus",
                    "name": "Qwen3.6 Plus",
                    "provider": {"npm": "@ai-sdk/anthropic"},
                },
            },
        },
        "opencode-go": {
            "id": "opencode-go",
            "name": "OpenCode Go",
            "npm": "@ai-sdk/openai-compatible",
            "models": {
                "gemini-3-flash": {
                    "id": "gemini-3-flash",
                    "name": "Gemini 3 Flash",
                    "provider": {"npm": "@ai-sdk/google"},
                    "tool_call": True,
                    "reasoning": True,
                    "limit": {"context": 1_048_576, "output": 65_536},
                    "modalities": {"input": ["text", "image"], "output": ["text"]},
                },
                "qwen3.6-plus": {
                    "id": "qwen3.6-plus",
                    "name": "Qwen3.6 Plus",
                    "provider": {"npm": "@ai-sdk/anthropic"},
                },
                "minimax-m2.7": {
                    "id": "minimax-m2.7",
                    "name": "MiniMax M2.7",
                    "provider": {"npm": "@ai-sdk/anthropic"},
                },
            },
        },
    }


def test_phase1_existing_provider_definitions_baseline_unchanged():
    definitions = {provider_id: copy.deepcopy(PROVIDER_DEFINITIONS[provider_id]) for provider_id in EXISTING_PROVIDER_IDS}

    assert definitions["openai"].default_transport == TransportMode.OPENAI_CHAT
    assert definitions["codex"].default_transport == TransportMode.OPENAI_RESPONSES
    assert definitions["openrouter"].base_url == "https://openrouter.ai/api/v1"
    assert definitions["anthropic"].default_transport == TransportMode.ANTHROPIC_MESSAGES
    assert definitions["google"].default_transport == TransportMode.GOOGLE_GENAI
    assert definitions["xai"].base_url == "https://api.x.ai/v1"
    assert definitions["minimax"].base_url == "https://api.minimax.io/anthropic"
    assert set(definitions) == set(EXISTING_PROVIDER_IDS)


def test_phase1_opencode_provider_definitions_exist():
    zen = PROVIDER_DEFINITIONS["opencode_zen"]
    go = PROVIDER_DEFINITIONS["opencode_go"]

    assert zen.display_name == "OpenCode Zen"
    assert zen.auth_methods == (AuthMethod.API_KEY,)
    assert zen.default_transport == TransportMode.OPENAI_CHAT
    assert zen.base_url == "https://opencode.ai/zen/v1"
    assert go.display_name == "OpenCode Go"
    assert go.auth_methods == (AuthMethod.API_KEY,)
    assert go.default_transport == TransportMode.OPENAI_CHAT
    assert go.base_url == "https://opencode.ai/zen/go/v1"


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        ("gpt-4o", "openai"),
        ("gpt-5.5", "openai"),
        ("claude-sonnet-4-5", "anthropic"),
        ("gemini-2.5-pro", "google"),
        ("grok-4", "xai"),
        ("MiniMax-M2.7", "minimax"),
        ("anthropic/claude-sonnet-4", "openrouter"),
    ],
)
def test_phase0_existing_infer_provider_id_baseline(model_id, expected):
    assert infer_provider_id(model_id) == expected


def test_phase0_cloud_cache_is_bare_keyed_and_can_be_overwritten(monkeypatch):
    monkeypatch.setattr(models, "_sync_custom_model_cache", lambda: None)
    original = dict(models._cloud_model_cache)
    models._cloud_model_cache.clear()
    try:
        models._cloud_model_cache["glm-5.1"] = {"provider": "openrouter", "label": "Routed GLM", "ctx": 131072}
        models._cloud_model_cache["glm-5.1"] = {"provider": "custom_openai_lab", "label": "Lab GLM", "ctx": 65536}

        assert models._cloud_model_cache["glm-5.1"]["provider"] == "custom_openai_lab"
        assert models.get_cloud_provider("glm-5.1") == "custom_openai_lab"
    finally:
        models._cloud_model_cache.clear()
        models._cloud_model_cache.update(original)


def test_phase0_single_provider_can_require_multiple_transports():
    chat = classify_model_capabilities("openai", "gpt-4o")
    responses = classify_model_capabilities("openai", "gpt-5.5")

    assert chat["transport"] == TransportMode.OPENAI_CHAT
    assert responses["transport"] == TransportMode.OPENAI_RESPONSES


def test_phase0_existing_configured_provider_listing_excludes_opencode(monkeypatch):
    monkeypatch.setattr(runtime, "is_provider_available", lambda provider_id: provider_id in {"openai", "minimax"})
    monkeypatch.setattr(runtime, "provider_status", lambda provider_id: {"configured": False})
    monkeypatch.setattr("row_bot.providers.custom.list_custom_endpoints", lambda: [])

    assert runtime.list_configured_provider_ids() == ["openai", "minimax"]


def test_phase1_opencode_api_key_env_vars_are_scoped(monkeypatch):
    monkeypatch.setenv("OPENCODE_ZEN_API_KEY", "zen-env-secret")
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "go-env-secret")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    assert get_provider_secret("opencode_zen") == "zen-env-secret"
    assert provider_secret_status("opencode_zen")["source"] == "environment"
    assert get_provider_secret("opencode_go") == "go-env-secret"
    assert provider_secret_status("opencode_go")["source"] == "environment"
    assert get_provider_secret("openai") != "zen-env-secret"
    assert get_provider_secret("openai") != "go-env-secret"


def test_phase1_configured_provider_listing_includes_only_keyed_opencode(monkeypatch):
    available = {"opencode_go"}
    monkeypatch.setattr(runtime, "is_provider_available", lambda provider_id: provider_id in available)
    monkeypatch.setattr(runtime, "provider_status", lambda provider_id: {"configured": False})
    monkeypatch.setattr("row_bot.providers.custom.list_custom_endpoints", lambda: [])

    assert runtime.list_configured_provider_ids() == ["opencode_go"]


def test_phase2_provider_qualified_cache_rows_are_distinct_from_bare_provider_rows(monkeypatch):
    monkeypatch.setattr("row_bot.providers.model_catalog._provider_status_by_id", lambda: {
        "openrouter": {"configured": True},
        "opencode_go": {"configured": True},
    })

    rows = build_model_catalog_rows(
        cloud_cache={
            "glm-5.1": {"provider": "openrouter", "label": "GLM via OpenRouter", "ctx": 131072},
            "model:opencode_go:glm-5.1": {"provider": "opencode_go", "label": "GLM via OpenCode Go", "ctx": 131072},
        },
        ollama_rows=[],
        quick_choices=[],
    )

    by_ref = {row.selection_ref: row for row in rows}
    assert by_ref["model:openrouter:glm-5.1"].provider_id == "openrouter"
    assert by_ref["model:opencode_go:glm-5.1"].provider_id == "opencode_go"
    assert by_ref["model:opencode_go:glm-5.1"].model_id == "glm-5.1"


def test_phase2_opencode_minimax_id_does_not_impersonate_direct_minimax(monkeypatch):
    monkeypatch.setattr("row_bot.providers.model_catalog._provider_status_by_id", lambda: {
        "minimax": {"configured": True},
        "opencode_zen": {"configured": True},
    })

    rows = build_model_catalog_rows(
        cloud_cache={
            "MiniMax-M2.7": {"provider": "minimax", "label": "MiniMax Direct", "ctx": 204800},
            "model:opencode_zen:minimax-m2.7": {
                "provider": "opencode_zen",
                "label": "MiniMax via OpenCode Zen",
                "ctx": 204800,
            },
        },
        ollama_rows=[],
        quick_choices=[],
    )

    by_ref = {row.selection_ref: row for row in rows}
    assert by_ref["model:minimax:MiniMax-M2.7"].provider_id == "minimax"
    assert by_ref["model:opencode_zen:minimax-m2.7"].provider_id == "opencode_zen"


def test_phase2_legacy_cache_conversion_reads_provider_qualified_keys():
    infos = legacy_cache_to_model_infos({
        "model:opencode_go:glm-5.1": {
            "provider": "opencode_go",
            "label": "GLM via OpenCode Go",
            "ctx": 131072,
        }
    })

    assert len(infos) == 1
    assert infos[0].provider_id == "opencode_go"
    assert infos[0].model_id == "glm-5.1"
    assert infos[0].selection_ref == "model:opencode_go:glm-5.1"


def test_phase2_quick_choices_keep_same_opencode_model_id_unambiguous(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr("row_bot.api_keys.get_cloud_config", lambda: {"starred_models": []})

    add_quick_choice_for_model(
        "glm-5.1",
        provider_id="openrouter",
        display_name="GLM via OpenRouter",
    )
    add_quick_choice_for_model(
        "glm-5.1",
        provider_id="opencode_go",
        display_name="GLM via OpenCode Go",
    )

    assert {choice["id"] for choice in list_quick_choices("")} == {
        "model:openrouter:glm-5.1",
        "model:opencode_go:glm-5.1",
    }
    with pytest.raises(ModelSelectionError, match="Ambiguous model selection 'glm-5.1'"):
        canonicalize_model_selection("glm-5.1", "workflow")


def test_phase3_opencode_static_routes_map_transports():
    assert opencode_model_transport("opencode_zen", "nemotron-3-super-free") == TransportMode.OPENAI_CHAT
    assert opencode_model_transport("opencode_zen", "deepseek-v4-flash-free") == TransportMode.OPENAI_CHAT
    assert opencode_model_transport("opencode_zen", "gpt-5.5") == TransportMode.OPENAI_RESPONSES
    assert opencode_model_transport("opencode_zen", "gpt-5.6") == TransportMode.OPENAI_RESPONSES
    assert opencode_model_transport("opencode_zen", "claude-sonnet-4-5") == TransportMode.ANTHROPIC_MESSAGES
    assert opencode_model_transport("opencode_zen", "qwen3.6-plus") == TransportMode.ANTHROPIC_MESSAGES
    assert opencode_model_transport("opencode_go", "glm-5.1") == TransportMode.OPENAI_CHAT
    assert opencode_model_transport("opencode_go", "mimo-v2.5-pro") == TransportMode.OPENAI_CHAT
    assert opencode_model_transport("opencode_go", "minimax-m2.7") == TransportMode.ANTHROPIC_MESSAGES
    assert opencode_model_transport("opencode_go", "qwen3.6-plus") == TransportMode.ANTHROPIC_MESSAGES


@pytest.mark.parametrize(
    ("package", "expected"),
    [
        ("@ai-sdk/openai-compatible", TransportMode.OPENAI_CHAT),
        ("@ai-sdk/openai", TransportMode.OPENAI_RESPONSES),
        ("@ai-sdk/anthropic", TransportMode.ANTHROPIC_MESSAGES),
        ("@ai-sdk/google", TransportMode.GOOGLE_GENAI),
        ("@ai-sdk/unknown", None),
        ("", None),
    ],
)
def test_native_opencode_protocol_table_fails_closed(package, expected):
    assert opencode_provider.native_opencode_transport(package) == expected


def test_native_opencode_registry_builds_ox_and_gemini_routes():
    registry = opencode_provider.parse_opencode_registry_provider(
        _native_opencode_registry(),
        "opencode_zen",
    )

    ox = opencode_provider.opencode_native_model_route(
        "opencode_zen",
        {"id": "x-preview-f-free"},
        registry,
    )
    gemini = opencode_provider.opencode_native_model_route(
        "opencode_zen",
        {"id": "gemini-3-flash"},
        registry,
    )

    assert ox is not None
    assert ox.display_name == "Ox Alpha Free"
    assert ox.transport == TransportMode.OPENAI_CHAT
    assert ox.context_window == 200_000
    assert ox.tool_calling is True
    assert ox.reasoning is True
    assert ox.input_modalities == frozenset({"text", "image"})
    assert gemini is not None
    assert gemini.transport == TransportMode.GOOGLE_GENAI
    assert gemini.context_window == 1_048_576


def test_native_opencode_go_rollout_skew_uses_supported_provider_default():
    registry = opencode_provider.parse_opencode_registry_provider(
        _native_opencode_registry(),
        "opencode_go",
    )

    route = opencode_provider.opencode_native_model_route(
        "opencode_go",
        {"id": "x-preview-f-free", "name": "Ox Alpha Free", "context_length": 200_000},
        registry,
    )

    assert route is not None
    assert route.model_id == "x-preview-f-free"
    assert route.transport == TransportMode.OPENAI_CHAT
    assert route.display_name == "Ox Alpha Free"


def test_native_opencode_unknown_model_protocol_is_not_guessed():
    payload = _native_opencode_registry()
    payload["opencode"]["models"]["mystery"] = {
        "id": "mystery",
        "name": "Mystery",
        "provider": {"npm": "@ai-sdk/not-supported"},
    }
    registry = opencode_provider.parse_opencode_registry_provider(payload, "opencode_zen")

    assert opencode_provider.opencode_native_model_route(
        "opencode_zen",
        {"id": "mystery"},
        registry,
    ) is None


def test_followup_opencode_stale_models_are_known_but_not_supported():
    route = opencode_known_route("opencode_zen", "deepseek-v3.2")

    assert route is not None
    assert route.unsupported_reason
    with pytest.raises(OpenCodeUnsupportedRouteError, match="deepseek-v3.2"):
        opencode_model_route("opencode_zen", "deepseek-v3.2")


def test_phase3_opencode_gemini_routes_are_deferred():
    route = opencode_known_route("opencode_zen", "gemini-2.5-pro")

    assert route is not None
    assert route.transport == TransportMode.GOOGLE_GENAI
    with pytest.raises(OpenCodeUnsupportedRouteError, match="OpenCode Gemini routes are deferred"):
        opencode_model_route("opencode_zen", "gemini-2.5-pro")


def test_phase3_opencode_model_infos_use_canonical_refs_and_task_metadata():
    infos = {info.selection_ref: info for info in list_opencode_model_infos("opencode_zen")}

    assert infos["model:opencode_zen:nemotron-3-super-free"].transport == TransportMode.OPENAI_CHAT
    assert infos["model:opencode_zen:gpt-5.5"].transport == TransportMode.OPENAI_RESPONSES
    assert infos["model:opencode_zen:gpt-5.5"].tasks == frozenset({"responses"})
    assert infos["model:opencode_zen:claude-sonnet-4-5"].transport == TransportMode.ANTHROPIC_MESSAGES


def test_followup_opencode_zen_marks_models_dev_image_input_routes_as_vision():
    infos = {info.selection_ref: info for info in list_opencode_model_infos("opencode_zen")}

    assert "image" in infos["model:opencode_zen:gpt-5.5"].input_modalities
    assert "vision" in infos["model:opencode_zen:gpt-5.5"].capabilities
    assert "image" in infos["model:opencode_zen:claude-sonnet-4-5"].input_modalities
    assert "vision" in infos["model:opencode_zen:claude-sonnet-4-5"].capabilities
    assert "image" in infos["model:opencode_zen:qwen3.6-plus"].input_modalities
    assert "vision" in infos["model:opencode_zen:qwen3.6-plus"].capabilities
    assert "image" in infos["model:opencode_zen:kimi-k2.5"].input_modalities
    assert "vision" in infos["model:opencode_zen:kimi-k2.5"].capabilities
    assert "image" in infos["model:opencode_zen:grok-build-0.1"].input_modalities
    assert "image" in infos["model:opencode_zen:mimo-v2.5-free"].input_modalities
    assert "image" not in infos["model:opencode_zen:minimax-m2.7"].input_modalities
    assert "model:opencode_zen:gemini-3.5-flash" not in infos


def test_followup_opencode_zen_vision_refs_appear_after_discovery_refresh_without_metadata(monkeypatch):
    original = dict(models._cloud_model_cache)
    models._cloud_model_cache.clear()

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": [
                    {"id": "gpt-5.5"},
                    {"id": "claude-sonnet-4-5"},
                    {"id": "qwen3.6-plus"},
                    {"id": "kimi-k2.5"},
                ]
            }

    monkeypatch.setattr("row_bot.providers.auth_store.get_provider_secret", lambda provider_id: "opencode-key")
    monkeypatch.setattr("httpx.get", lambda *args, **kwargs: _Resp())
    try:
        models._fetch_opencode_models("opencode_zen", registry_payload=_native_opencode_registry())
        vision_models = set(models.list_cloud_vision_models())

        assert "model:opencode_zen:gpt-5.5" in vision_models
        assert "model:opencode_zen:claude-sonnet-4-5" in vision_models
        assert "model:opencode_zen:qwen3.6-plus" in vision_models
        assert "model:opencode_zen:kimi-k2.5" in vision_models
    finally:
        models._cloud_model_cache.clear()
        models._cloud_model_cache.update(original)


def test_followup_opencode_go_marks_models_dev_image_input_routes_as_vision():
    infos = {
        info.selection_ref: info
        for info in list_opencode_model_infos(
            "opencode_go",
            model_ids=[
                "mimo-v2-omni",
                "mimo-v2.5",
                "mimo-v2.5-pro",
                "qwen3.7-max",
                "qwen3.6-plus",
                "minimax-m2.7",
                "kimi-k2.6",
            ],
        )
    }

    assert "image" in infos["model:opencode_go:mimo-v2-omni"].input_modalities
    assert "vision" in infos["model:opencode_go:mimo-v2-omni"].capabilities
    assert "image" in infos["model:opencode_go:mimo-v2.5"].input_modalities
    assert "image" not in infos["model:opencode_go:mimo-v2.5-pro"].input_modalities
    assert "image" not in infos["model:opencode_go:qwen3.7-max"].input_modalities
    assert "image" in infos["model:opencode_go:qwen3.6-plus"].input_modalities
    assert "vision" in infos["model:opencode_go:qwen3.6-plus"].capabilities
    assert "image" not in infos["model:opencode_go:minimax-m2.7"].input_modalities
    assert "image" in infos["model:opencode_go:kimi-k2.6"].input_modalities


def test_followup_opencode_go_vision_ref_appears_after_discovery_refresh_without_metadata(monkeypatch):
    original = dict(models._cloud_model_cache)
    models._cloud_model_cache.clear()

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": [
                    {"id": "mimo-v2-omni"},
                    {"id": "mimo-v2.5-pro"},
                    {"id": "qwen3.6-plus"},
                    {"id": "minimax-m2.7"},
                ]
            }

    monkeypatch.setattr("row_bot.providers.auth_store.get_provider_secret", lambda provider_id: "opencode-key")
    monkeypatch.setattr("httpx.get", lambda *args, **kwargs: _Resp())
    try:
        models._fetch_opencode_models("opencode_go", registry_payload=_native_opencode_registry())
        vision_models = set(models.list_cloud_vision_models())

        assert "model:opencode_go:mimo-v2-omni" in vision_models
        assert "model:opencode_go:mimo-v2.5-pro" not in vision_models
        assert "model:opencode_go:qwen3.6-plus" in vision_models
        assert "model:opencode_go:minimax-m2.7" not in vision_models
    finally:
        models._cloud_model_cache.clear()
        models._cloud_model_cache.update(original)


def test_followup_opencode_live_input_modalities_override_static_vision_metadata(monkeypatch):
    original = dict(models._cloud_model_cache)
    models._cloud_model_cache.clear()

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": [
                    {"id": "kimi-k2.5", "modalities": {"input": ["text", "image", "video"]}},
                    {"id": "qwen3.6-plus", "modalities": {"input": ["text"]}},
                    {"id": "mimo-v2-pro", "modalities": {"input": ["text"]}, "attachment": True},
                ]
            }

    monkeypatch.setattr("row_bot.providers.auth_store.get_provider_secret", lambda provider_id: "opencode-key")
    monkeypatch.setattr("httpx.get", lambda *args, **kwargs: _Resp())
    try:
        models._fetch_opencode_models("opencode_go", registry_payload=_native_opencode_registry())
        vision_models = set(models.list_cloud_vision_models())

        assert "model:opencode_go:kimi-k2.5" in vision_models
        assert "model:opencode_go:qwen3.6-plus" not in vision_models
        assert "model:opencode_go:mimo-v2-pro" not in vision_models
    finally:
        models._cloud_model_cache.clear()
        models._cloud_model_cache.update(original)


def test_phase3_opencode_models_appear_in_catalog_with_canonical_refs(monkeypatch):
    monkeypatch.setattr("row_bot.providers.model_catalog._provider_status_by_id", lambda: {
        "opencode_zen": {"configured": True},
        "opencode_go": {"configured": True},
    })

    rows = build_model_catalog_rows(cloud_cache={}, ollama_rows=[], quick_choices=[])
    refs = {row.selection_ref for row in rows}

    assert "model:opencode_zen:nemotron-3-super-free" in refs
    assert "model:opencode_go:glm-5.1" in refs
    assert "model:opencode_go:mimo-v2.5-pro" in refs
    assert "model:opencode_zen:gemini-2.5-pro" not in refs


def test_dynamic_opencode_catalog_rows_are_not_overwritten_or_expanded_by_static_rows(monkeypatch):
    monkeypatch.setattr("row_bot.providers.model_catalog._provider_status_by_id", lambda: {
        "opencode_zen": {"configured": True},
    })
    dynamic_ref = "model:opencode_zen:x-preview-f-free"
    rows = build_model_catalog_rows(
        cloud_cache={
            dynamic_ref: {
                "provider": "opencode_zen",
                "label": "Ox Alpha Free (Live)",
                "ctx": 200_000,
                "transport": "openai_chat",
                "source": "opencode_live_catalog",
                "capabilities_snapshot": {
                    "transport": "openai_chat",
                    "tasks": ["chat"],
                    "capabilities": ["chat", "reasoning", "streaming", "text", "tool_calling", "vision"],
                    "input_modalities": ["image", "text"],
                    "output_modalities": ["text"],
                    "tool_calling": True,
                    "streaming": True,
                    "endpoint_compatibility": ["openai_chat"],
                },
            }
        },
        ollama_rows=[],
        quick_choices=[],
    )
    by_ref = {row.selection_ref: row for row in rows}

    assert by_ref[dynamic_ref].display_name == "Ox Alpha Free (Live)"
    assert by_ref[dynamic_ref].context_window == 200_000
    assert "vision" in by_ref[dynamic_ref].capabilities_snapshot["capabilities"]
    assert "model:opencode_zen:gpt-5.5" not in by_ref
    assert "model:opencode_zen:nemotron-3-super-free" not in by_ref


def test_phase3_fetch_opencode_models_uses_provider_qualified_cache(monkeypatch):
    original = dict(models._cloud_model_cache)
    models._cloud_model_cache.clear()
    captured = {}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": [
                    {"id": "glm-5.1"},
                    {"id": "mimo-v2.5-pro"},
                    {"id": "qwen3.6-plus"},
                    {"id": "hy3-preview"},
                ]
            }

    def _fake_get(*args, **kwargs):
        captured["headers"] = dict(kwargs.get("headers") or {})
        return _Resp()

    monkeypatch.setattr("row_bot.providers.auth_store.get_provider_secret", lambda provider_id: "opencode-key")
    monkeypatch.setattr("httpx.get", _fake_get)
    try:
        count = models._fetch_opencode_models("opencode_go", registry_payload=_native_opencode_registry())

        assert count >= 3
        assert captured["headers"]["Authorization"] == "Bearer opencode-key"
        assert "glm-5.1" not in models._cloud_model_cache
        assert models._cloud_model_cache["model:opencode_go:glm-5.1"]["provider"] == "opencode_go"
        assert models._cloud_model_cache["model:opencode_go:mimo-v2.5-pro"]["provider"] == "opencode_go"
        assert models._cloud_model_cache["model:opencode_go:qwen3.6-plus"]["transport"] == "anthropic_messages"
        assert models._cloud_model_cache["model:opencode_go:hy3-preview"]["transport"] == "openai_chat"
    finally:
        models._cloud_model_cache.clear()
        models._cloud_model_cache.update(original)


def test_native_opencode_discovery_intersects_live_availability_with_registry(monkeypatch):
    original = dict(models._cloud_model_cache)
    models._cloud_model_cache.clear()

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "x-preview-f-free"}]}

    monkeypatch.setattr("row_bot.providers.auth_store.get_provider_secret", lambda provider_id: "zen-key")
    monkeypatch.setattr("httpx.get", lambda *args, **kwargs: _Resp())
    try:
        count = models._fetch_opencode_models(
            "opencode_zen",
            registry_payload=_native_opencode_registry(),
        )

        assert count == 1
        assert set(models._cloud_model_cache) == {"model:opencode_zen:x-preview-f-free"}
        cached = models._cloud_model_cache["model:opencode_zen:x-preview-f-free"]
        assert cached["label"] == "Ox Alpha Free"
        assert cached["ctx"] == 200_000
        assert cached["transport"] == "openai_chat"
        assert cached["capabilities_snapshot"]["reasoning"] is None
        assert "reasoning" in cached["capabilities_snapshot"]["capabilities"]
        assert "image" in cached["capabilities_snapshot"]["input_modalities"]
        assert "model:opencode_zen:gemini-3-flash" not in models._cloud_model_cache
        assert "model:opencode_zen:registry-only" not in models._cloud_model_cache
    finally:
        models._cloud_model_cache.clear()
        models._cloud_model_cache.update(original)


def test_native_opencode_discovery_isolated_by_gateway(monkeypatch):
    original = dict(models._cloud_model_cache)
    models._cloud_model_cache.clear()

    class _Resp:
        def __init__(self, model_id):
            self.model_id = model_id

        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": self.model_id}]}

    def _fake_get(url, **kwargs):
        if "/go/" in url:
            return _Resp("gemini-3-flash")
        return _Resp("x-preview-f-free")

    monkeypatch.setattr("row_bot.providers.auth_store.get_provider_secret", lambda provider_id: f"{provider_id}-key")
    monkeypatch.setattr("httpx.get", _fake_get)
    try:
        models._fetch_opencode_models("opencode_zen", registry_payload=_native_opencode_registry())
        models._fetch_opencode_models("opencode_go", registry_payload=_native_opencode_registry())

        assert "model:opencode_zen:x-preview-f-free" in models._cloud_model_cache
        assert "model:opencode_go:x-preview-f-free" not in models._cloud_model_cache
        assert "model:opencode_go:gemini-3-flash" in models._cloud_model_cache
        assert "model:opencode_zen:gemini-3-flash" not in models._cloud_model_cache
    finally:
        models._cloud_model_cache.clear()
        models._cloud_model_cache.update(original)


def test_native_opencode_gateway_failure_preserves_last_known_good(monkeypatch):
    original = dict(models._cloud_model_cache)
    models._cloud_model_cache.clear()
    cached_ref = "model:opencode_zen:x-preview-f-free"
    cached = {
        "provider": "opencode_zen",
        "label": "Ox Alpha Free",
        "ctx": 200_000,
        "transport": "openai_chat",
        "source": "opencode_live_catalog",
    }
    models._cloud_model_cache[cached_ref] = dict(cached)
    monkeypatch.setattr("row_bot.providers.auth_store.get_provider_secret", lambda provider_id: "zen-key")
    monkeypatch.setattr("httpx.get", lambda *args, **kwargs: (_ for _ in ()).throw(httpx.ConnectError("offline")))
    try:
        assert models._fetch_opencode_models(
            "opencode_zen",
            registry_payload=_native_opencode_registry(),
        ) == 0
        assert models._cloud_model_cache[cached_ref] == cached
    finally:
        models._cloud_model_cache.clear()
        models._cloud_model_cache.update(original)


def test_native_opencode_registry_failure_preserves_last_known_good(monkeypatch):
    original = dict(models._cloud_model_cache)
    models._cloud_model_cache.clear()
    cached_ref = "model:opencode_go:x-preview-f-free"
    cached = {
        "provider": "opencode_go",
        "label": "Ox Alpha Free",
        "ctx": 200_000,
        "transport": "openai_chat",
        "source": "opencode_live_catalog",
    }
    models._cloud_model_cache[cached_ref] = dict(cached)

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "x-preview-f-free"}]}

    monkeypatch.setattr("row_bot.providers.auth_store.get_provider_secret", lambda provider_id: "go-key")
    monkeypatch.setattr("httpx.get", lambda *args, **kwargs: _Resp())
    try:
        assert models._fetch_opencode_models("opencode_go", registry_payload=None) == 0
        assert models._cloud_model_cache[cached_ref] == cached
    finally:
        models._cloud_model_cache.clear()
        models._cloud_model_cache.update(original)


def test_native_opencode_cold_gateway_failure_uses_static_fallback(monkeypatch):
    original = dict(models._cloud_model_cache)
    models._cloud_model_cache.clear()
    monkeypatch.setattr("row_bot.providers.auth_store.get_provider_secret", lambda provider_id: "zen-key")
    monkeypatch.setattr("httpx.get", lambda *args, **kwargs: (_ for _ in ()).throw(httpx.ConnectError("offline")))
    try:
        count = models._fetch_opencode_models("opencode_zen")

        assert count > 0
        cached = models._cloud_model_cache["model:opencode_zen:gpt-5.5"]
        assert cached["transport"] == "openai_responses"
        assert cached["source"] == "opencode_static_fallback"
    finally:
        models._cloud_model_cache.clear()
        models._cloud_model_cache.update(original)


def test_native_opencode_valid_empty_gateway_catalog_clears_stale_rows(monkeypatch):
    original = dict(models._cloud_model_cache)
    models._cloud_model_cache.clear()
    models._cloud_model_cache["model:opencode_zen:x-preview-f-free"] = {
        "provider": "opencode_zen",
        "label": "Ox Alpha Free",
        "ctx": 200_000,
        "transport": "openai_chat",
        "source": "opencode_live_catalog",
    }

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": []}

    monkeypatch.setattr("row_bot.providers.auth_store.get_provider_secret", lambda provider_id: "zen-key")
    monkeypatch.setattr("httpx.get", lambda *args, **kwargs: _Resp())
    try:
        assert models._fetch_opencode_models(
            "opencode_zen",
            registry_payload=_native_opencode_registry(),
        ) == 0
        assert not models._provider_cloud_cache_entries("opencode_zen")
    finally:
        models._cloud_model_cache.clear()
        models._cloud_model_cache.update(original)


def test_native_opencode_full_refresh_coalesces_models_dev_fetch(monkeypatch):
    original = dict(models._cloud_model_cache)
    models._cloud_model_cache.clear()
    calls: list[str] = []

    class _Resp:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    def _fake_get(url, **kwargs):
        calls.append(url)
        if url == opencode_provider.OPENCODE_MODELS_DEV_URL:
            return _Resp(_native_opencode_registry())
        if "/go/" in url:
            return _Resp({"data": [{"id": "gemini-3-flash"}]})
        return _Resp({"data": [{"id": "x-preview-f-free"}]})

    monkeypatch.setattr(models, "REFRESHABLE_CLOUD_PROVIDER_IDS", ("opencode_zen", "opencode_go"))
    monkeypatch.setattr(models, "fetch_context_catalog", lambda: None)
    monkeypatch.setattr(models, "_save_cloud_cache", lambda: None)
    monkeypatch.setattr("row_bot.providers.auth_store.get_provider_secret", lambda provider_id: f"{provider_id}-key")
    monkeypatch.setattr("httpx.get", _fake_get)
    try:
        results = models.refresh_cloud_models_detailed()

        assert results["opencode_zen"].status == "live"
        assert results["opencode_go"].status == "live"
        assert calls.count(opencode_provider.OPENCODE_MODELS_DEV_URL) == 1
    finally:
        models._cloud_model_cache.clear()
        models._cloud_model_cache.update(original)


def test_native_opencode_unknown_protocol_diagnostic_is_sanitized(caplog, monkeypatch):
    original = dict(models._cloud_model_cache)
    models._cloud_model_cache.clear()
    payload = _native_opencode_registry()
    payload["opencode"]["models"]["mystery"] = {
        "id": "mystery",
        "provider": {"npm": "@ai-sdk/not-supported"},
        "private_debug": "raw-sensitive-response-content",
    }

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "mystery", "debug": "raw-sensitive-response-content"}]}

    monkeypatch.setattr("row_bot.providers.auth_store.get_provider_secret", lambda provider_id: "secret-do-not-log")
    monkeypatch.setattr("httpx.get", lambda *args, **kwargs: _Resp())
    try:
        with caplog.at_level("WARNING", logger="row_bot.models"):
            assert models._fetch_opencode_models("opencode_zen", registry_payload=payload) == 0

        logged = caplog.text
        assert "opencode_zen/mystery" in logged
        assert "@ai-sdk/not-supported" in logged
        assert "secret-do-not-log" not in logged
        assert "raw-sensitive-response-content" not in logged
    finally:
        models._cloud_model_cache.clear()
        models._cloud_model_cache.update(original)


def test_native_opencode_cache_route_survives_persistence_without_name_classification(tmp_path, monkeypatch):
    original = dict(models._cloud_model_cache)
    models._cloud_model_cache.clear()
    monkeypatch.setattr(models, "_CLOUD_CACHE_PATH", tmp_path / "cloud_models_cache.json")
    monkeypatch.setattr(models, "_DATA_DIR", tmp_path)
    models._cloud_model_cache["model:opencode_zen:x-preview-f-free"] = {
        "provider": "opencode_zen",
        "label": "Ox Alpha Free",
        "ctx": 200_000,
        "transport": "openai_chat",
        "source": "opencode_live_catalog",
        "capabilities_snapshot": {
            "transport": "openai_chat",
            "tasks": ["chat"],
            "capabilities": ["chat", "reasoning", "streaming", "text", "tool_calling"],
            "input_modalities": ["image", "text"],
            "output_modalities": ["text"],
            "tool_calling": True,
            "streaming": True,
            "endpoint_compatibility": ["openai_chat"],
        },
    }
    try:
        models._save_cloud_cache()
        models._cloud_model_cache.clear()
        models._cloud_model_cache.update(models._load_cloud_cache())

        route = opencode_model_route("opencode_zen", "x-preview-f-free")
        resolved = resolve_provider_config("model:opencode_zen:x-preview-f-free")

        assert route.transport == TransportMode.OPENAI_CHAT
        assert route.display_name == "Ox Alpha Free"
        assert route.input_modalities == frozenset({"text", "image"})
        assert resolved.transport == TransportMode.OPENAI_CHAT
    finally:
        models._cloud_model_cache.clear()
        models._cloud_model_cache.update(original)


def test_old_opencode_cache_row_without_route_uses_legacy_static_fallback(monkeypatch):
    original = dict(models._cloud_model_cache)
    models._cloud_model_cache.clear()
    models._cloud_model_cache["model:opencode_zen:gpt-5.5"] = {
        "provider": "opencode_zen",
        "label": "GPT 5.5",
        "ctx": 400_000,
    }
    try:
        assert opencode_model_route("opencode_zen", "gpt-5.5").transport == TransportMode.OPENAI_RESPONSES
    finally:
        models._cloud_model_cache.clear()
        models._cloud_model_cache.update(original)


def test_malformed_cached_opencode_route_fails_closed(monkeypatch):
    original = dict(models._cloud_model_cache)
    models._cloud_model_cache.clear()
    models._cloud_model_cache["model:opencode_zen:x-preview-f-free"] = {
        "provider": "opencode_zen",
        "label": "Ox Alpha Free",
        "ctx": 200_000,
        "transport": "invented_transport",
        "source": "opencode_live_catalog",
    }
    try:
        with pytest.raises(OpenCodeUnsupportedRouteError, match="unsupported cached transport"):
            opencode_model_route("opencode_zen", "x-preview-f-free")
    finally:
        models._cloud_model_cache.clear()
        models._cloud_model_cache.update(original)


def test_native_opencode_refresh_to_catalog_resolution_and_runtime_end_to_end(monkeypatch):
    original = dict(models._cloud_model_cache)
    models._cloud_model_cache.clear()

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "x-preview-f-free"}, {"id": "gemini-3-flash"}]}

    fake_google_module = ModuleType("langchain_google_genai")

    class _FakeChatGoogleGenerativeAI:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    fake_google_module.ChatGoogleGenerativeAI = _FakeChatGoogleGenerativeAI
    monkeypatch.setitem(sys.modules, "langchain_google_genai", fake_google_module)
    monkeypatch.setattr("httpx.get", lambda *args, **kwargs: _Resp())
    monkeypatch.setattr("row_bot.providers.auth_store.get_provider_secret", lambda provider_id: "zen-key")
    monkeypatch.setattr(runtime, "get_provider_secret", lambda provider_id, credential_name="api_key": "zen-key")
    monkeypatch.setattr("row_bot.providers.model_catalog._provider_status_by_id", lambda: {
        "opencode_zen": {"configured": True},
    })
    monkeypatch.setattr("row_bot.providers.readiness.provider_status", lambda provider_id: {"configured": True})
    try:
        assert models._fetch_opencode_models(
            "opencode_zen",
            registry_payload=_native_opencode_registry(),
        ) == 2

        rows = build_model_catalog_rows(
            cloud_cache=dict(models._cloud_model_cache),
            ollama_rows=[],
            quick_choices=[],
        )
        by_ref = {row.selection_ref: row for row in rows}
        ox_ref = "model:opencode_zen:x-preview-f-free"
        gemini_ref = "model:opencode_zen:gemini-3-flash"

        assert {
            ref
            for ref, row in by_ref.items()
            if row.provider_id == "opencode_zen"
        } == {ox_ref, gemini_ref}
        assert resolve_provider_config(ox_ref).transport == TransportMode.OPENAI_CHAT
        assert resolve_provider_config(gemini_ref).transport == TransportMode.GOOGLE_GENAI

        ox_model = runtime.create_chat_model(ox_ref)
        gemini_model = runtime.create_chat_model(gemini_ref)

        assert ox_model.model_name == "x-preview-f-free"
        assert ox_model.api_key == "zen-key"
        assert ox_model.base_url == "https://opencode.ai/zen/v1"
        assert gemini_model.kwargs["model"] == "gemini-3-flash"
        assert gemini_model.kwargs["google_api_key"] == "zen-key"
        assert gemini_model.kwargs["base_url"] == "https://opencode.ai/zen"
        assert gemini_model.kwargs["api_version"] == "v1"
        assert (
            f"{gemini_model.kwargs['base_url']}/{gemini_model.kwargs['api_version']}"
            f"/models/{gemini_model.kwargs['model']}:generateContent"
        ) == "https://opencode.ai/zen/v1/models/gemini-3-flash:generateContent"
        assert evaluate_agent_readiness(gemini_ref, context_window_override=1_048_576).ready is True
    finally:
        models._cloud_model_cache.clear()
        models._cloud_model_cache.update(original)


def test_native_opencode_provider_default_handles_live_registry_rollout_skew(monkeypatch):
    original = dict(models._cloud_model_cache)
    models._cloud_model_cache.clear()

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "deepseek-v4-flash-free"}, {"id": "gpt-5.6"}, {"id": "deepseek-v3.2"}]}

    monkeypatch.setattr("row_bot.providers.auth_store.get_provider_secret", lambda provider_id: "opencode-key")
    monkeypatch.setattr("httpx.get", lambda *args, **kwargs: _Resp())
    try:
        models._fetch_opencode_models("opencode_zen", registry_payload=_native_opencode_registry())

        assert "model:opencode_zen:deepseek-v4-flash-free" in models._cloud_model_cache
        assert models._cloud_model_cache["model:opencode_zen:gpt-5.6"]["transport"] == "openai_responses"
        assert models._cloud_model_cache["model:opencode_zen:deepseek-v3.2"]["transport"] == "openai_chat"
    finally:
        models._cloud_model_cache.clear()
        models._cloud_model_cache.update(original)


def test_phase4_resolution_uses_opencode_model_level_transport():
    assert resolve_provider_config("model:opencode_zen:nemotron-3-super-free").transport == TransportMode.OPENAI_CHAT
    assert resolve_provider_config("model:opencode_zen:gpt-5.5").transport == TransportMode.OPENAI_RESPONSES
    assert resolve_provider_config("model:opencode_zen:claude-sonnet-4-5").transport == TransportMode.ANTHROPIC_MESSAGES
    assert resolve_provider_config("model:opencode_go:minimax-m2.7").transport == TransportMode.ANTHROPIC_MESSAGES
    assert resolve_provider_config("model:opencode_go:qwen3.6-plus").transport == TransportMode.ANTHROPIC_MESSAGES


def test_phase4_opencode_chat_runtime_keeps_v1_base_url(monkeypatch):
    monkeypatch.setattr(runtime, "get_provider_secret", lambda provider_id, credential_name="api_key": "opencode-key")

    model = runtime.create_chat_model("model:opencode_go:glm-5.1")

    assert model.model_name == "glm-5.1"
    assert model.api_key == "opencode-key"
    assert model.base_url == "https://opencode.ai/zen/go/v1"
    assert model.endpoint["provider_id"] == "opencode_go"


def test_phase4_opencode_responses_runtime_keeps_v1_base_url(monkeypatch):
    fake_module = ModuleType("langchain_openai")

    class _FakeChatOpenAI:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    fake_module.ChatOpenAI = _FakeChatOpenAI
    monkeypatch.setitem(sys.modules, "langchain_openai", fake_module)
    monkeypatch.setattr(runtime, "get_provider_secret", lambda provider_id, credential_name="api_key": "zen-key")

    model = runtime.create_chat_model("model:opencode_zen:gpt-5.5")

    assert model.kwargs["model"] == "gpt-5.5"
    assert model.kwargs["api_key"] == "zen-key"
    assert model.kwargs["base_url"] == "https://opencode.ai/zen/v1"
    assert model.kwargs["use_responses_api"] is True
    assert model.kwargs["output_version"] == "responses/v1"
    assert isinstance(model.kwargs["http_client"], httpx.Client)


def test_phase4_opencode_anthropic_runtime_strips_v1(monkeypatch):
    monkeypatch.setattr(runtime, "get_provider_secret", lambda provider_id, credential_name="api_key": "go-key")

    model = runtime.create_chat_model("model:opencode_go:minimax-m2.7")

    assert isinstance(model, CancellableChatAnthropic)
    assert model.model == "minimax-m2.7"
    assert model.anthropic_api_key.get_secret_value() == "go-key"
    assert model.anthropic_api_url == "https://opencode.ai/zen/go"
    assert "http_client" not in model.model_kwargs
    _assert_sync_client_registered_with_cancellation_scope(model._client._client)


def test_phase4_opencode_gemini_runtime_is_blocked(monkeypatch):
    monkeypatch.setattr(runtime, "get_provider_secret", lambda provider_id, credential_name="api_key": "zen-key")

    with pytest.raises(OpenCodeUnsupportedRouteError, match="OpenCode Gemini routes are deferred"):
        runtime.create_chat_model("model:opencode_zen:gemini-2.5-pro")


def test_phase4_missing_opencode_key_uses_opencode_specific_error(monkeypatch):
    monkeypatch.setattr(runtime, "get_provider_secret", lambda provider_id, credential_name="api_key": "")

    with pytest.raises(ValueError, match="OpenCode Zen API key not configured"):
        runtime.create_chat_model("model:opencode_zen:nemotron-3-super-free")


def test_phase5_supported_opencode_chat_model_is_selectable_and_agent_ready(monkeypatch):
    monkeypatch.setattr("row_bot.providers.readiness.provider_status", lambda provider_id: {"configured": True})

    result = evaluate_runtime_readiness(
        "model:opencode_go:glm-5.1",
        context_window_override=131072,
    )

    assert result.chat.ready is True
    assert result.agent.ready is True
    assert result.selected_mode == "agent"
    assert result.agent.tool_calling is True
    assert result.agent.tool_calling_source == "trusted_provider"


def test_phase5_opencode_responses_model_is_agent_ready(monkeypatch):
    monkeypatch.setattr("row_bot.providers.readiness.provider_status", lambda provider_id: {"configured": True})

    result = evaluate_agent_readiness(
        "model:opencode_zen:gpt-5.5",
        context_window_override=400000,
    )

    assert result.ready is True
    assert result.transport == TransportMode.OPENAI_RESPONSES


def test_phase5_opencode_gemini_route_is_blocked_with_deferred_reason(monkeypatch):
    monkeypatch.setattr("row_bot.providers.readiness.provider_status", lambda provider_id: {"configured": True})

    result = evaluate_runtime_readiness(
        "model:opencode_zen:gemini-2.5-pro",
        context_window_override=1048576,
    )

    assert result.selected_mode == "blocked"
    assert any("OpenCode Gemini routes are deferred" in error for error in result.chat.errors)
    assert any("OpenCode Gemini routes are deferred" in error for error in result.agent.errors)


def test_phase6_default_and_quick_choice_values_use_opencode_canonical_refs(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr("row_bot.api_keys.get_cloud_config", lambda: {"starred_models": []})

    default_ref = model_choice_value("nemotron-3-super-free", provider_id="opencode_zen")
    add_quick_choice_for_model(
        "nemotron-3-super-free",
        provider_id="opencode_zen",
        display_name="Nemotron via OpenCode Zen",
    )

    assert default_ref == "model:opencode_zen:nemotron-3-super-free"
    stored = provider_config.load_provider_config()["quick_choices"][0]
    assert stored["id"] == "model:opencode_zen:nemotron-3-super-free"
    assert list_quick_choices("chat")[0]["id"] == "model:opencode_zen:nemotron-3-super-free"


def test_phase6_workflow_model_overrides_store_opencode_refs(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr("row_bot.api_keys.get_cloud_config", lambda: {"starred_models": []})
    import row_bot.tasks as tasks

    tasks = importlib.reload(tasks)

    task_id = tasks.create_task(
        "OpenCode workflow",
        prompts=["say hi"],
        model_override="model:opencode_go:glm-5.1",
        steps=[{"type": "prompt", "prompt": "step hi", "model_override": "model:opencode_go:mimo-v2.5-pro"}],
    )
    tasks.update_task(task_id, model_override="model:opencode_zen:nemotron-3-super-free")

    task = tasks.get_task(task_id)
    assert task["model_override"] == "model:opencode_zen:nemotron-3-super-free"
    assert task["steps"][0]["model_override"] == "model:opencode_go:mimo-v2.5-pro"


def test_phase6_channel_model_override_preserves_opencode_ref(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "threads"))
    import row_bot.threads as threads

    threads = importlib.reload(threads)
    ref = "model:opencode_go:glm-5.1"
    threads._save_thread_meta("tg_opencode", "Telegram OpenCode")
    threads._set_thread_model_override("tg_opencode", ref)

    assert threads._get_thread_model_override("tg_opencode") == ref


def test_phase6_ambiguous_bare_opencode_model_fails_clearly(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr("row_bot.api_keys.get_cloud_config", lambda: {"starred_models": []})
    add_quick_choice_for_model("glm-5.1", provider_id="opencode_go", display_name="OpenCode GLM")
    add_quick_choice_for_model("glm-5.1", provider_id="openrouter", display_name="OpenRouter GLM")

    with pytest.raises(ModelSelectionError, match="Use one of: model:opencode_go:glm-5.1, model:openrouter:glm-5.1|Use one of: model:openrouter:glm-5.1, model:opencode_go:glm-5.1"):
        canonicalize_model_selection("glm-5.1", "workflow")


def test_phase7_opencode_route_diagnostics_include_provider_model_and_transport():
    diagnostics = opencode_route_diagnostics("opencode_go", "glm-5.1")

    assert diagnostics["provider_id"] == "opencode_go"
    assert diagnostics["model_id"] == "glm-5.1"
    assert diagnostics["selection_ref"] == "model:opencode_go:glm-5.1"
    assert diagnostics["base_url"] == "https://opencode.ai/zen/go/v1"
    assert diagnostics["anthropic_base_url"] == "https://opencode.ai/zen/go"
    assert diagnostics["transport"] == "openai_chat"


def test_phase7_opencode_failure_diagnostics_are_scoped_to_opencode():
    diagnostics = opencode_failure_diagnostics(
        "opencode_zen",
        "nemotron-3-super-free",
        RuntimeError("401 unauthorized"),
    )

    assert diagnostics["provider_id"] == "opencode_zen"
    assert diagnostics["model_id"] == "nemotron-3-super-free"
    assert "OpenCode Zen authentication failed" in diagnostics["hint"]
    assert "401 unauthorized" in diagnostics["error"]


def test_followup_opencode_model_not_supported_diagnostic_is_not_key_only():
    diagnostics = opencode_failure_diagnostics(
        "opencode_zen",
        "deepseek-v3.2",
        RuntimeError("HTTP 401. Details: Model deepseek-v3.2 is not supported"),
    )

    assert "model is not supported" in diagnostics["hint"].lower()
    assert "API key" not in diagnostics["hint"]


def test_phase7_opencode_404_diagnostic_mentions_route_without_fallback():
    diagnostics = opencode_failure_diagnostics(
        "opencode_go",
        "glm-5.1",
        RuntimeError("404 not found"),
    )

    assert "stale or mapped to the wrong transport" in diagnostics["hint"]
    assert diagnostics["provider_id"] == "opencode_go"


def test_followup_opencode_model_level_anthropic_detection(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    import row_bot.agent as agent

    assert agent._provider_uses_anthropic_messages("opencode_zen", "claude-sonnet-4-5") is True
    assert agent._provider_uses_anthropic_messages("opencode_go", "qwen3.6-plus") is True
    assert agent._provider_uses_anthropic_messages("opencode_go", "glm-5.1") is False


def test_followup_openrouter_nested_input_modalities_enable_vision():
    result = classify_model_capabilities(
        "openrouter",
        "moonshotai/kimi-k2.5",
        {"architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]}},
    )

    assert "image" in result["input_modalities"]
    assert "vision" in result["capabilities"]


def test_ux_provider_rows_expose_opencode_keyring_controls():
    from row_bot.providers.auth_store import PROVIDER_API_KEY_ENV
    from row_bot.ui.provider_settings import _api_key_provider_action_state, _api_key_provider_ids, _api_key_provider_ui

    assert PROVIDER_API_KEY_ENV["opencode_zen"] == "OPENCODE_ZEN_API_KEY"
    assert PROVIDER_API_KEY_ENV["opencode_go"] == "OPENCODE_GO_API_KEY"
    assert "opencode_zen" in _api_key_provider_ids()
    assert "opencode_go" in _api_key_provider_ids()
    assert _api_key_provider_action_state({"provider_id": "opencode_zen"})["can_manage_api_key"] is True
    assert _api_key_provider_action_state({"provider_id": "opencode_go"})["can_manage_api_key"] is True
    assert _api_key_provider_ui("opencode_zen").validator_name == ""
    assert _api_key_provider_ui("opencode_go").validator_name == ""


def test_ux_setup_wizard_exposes_and_saves_opencode_keys():
    source = Path("src/row_bot/ui/setup_wizard.py").read_text(encoding="utf-8")

    assert "OpenCode Zen API Key (optional)" in source
    assert "OpenCode Go API Key (optional)" in source
    assert '("opencode_zen", opencode_zen_val)' in source
    assert '("opencode_go", opencode_go_val)' in source
    assert 'set_key("OPENCODE_ZEN_API_KEY", opencode_zen_val)' in source
    assert 'set_key("OPENCODE_GO_API_KEY", opencode_go_val)' in source


def test_ux_setup_wizard_option_preserves_opencode_ref_without_noisy_label():
    from row_bot.ui.setup_wizard import cloud_model_setup_option

    option = cloud_model_setup_option(
        "model:opencode_go:glm-5.1",
        {"provider": "opencode_go", "label": "GLM 5.1"},
        emoji_lookup=lambda value: "OG",
    )

    assert option["value"] == "model:opencode_go:glm-5.1"
    assert option["provider_id"] == "opencode_go"
    assert option["model_id"] == "glm-5.1"
    assert option["label"] == "OG GLM 5.1"
    assert "model:opencode_go" not in option["label"]


def test_ux_provider_status_counts_opencode_provider_qualified_cache(monkeypatch):
    from row_bot.providers.status import provider_status_cards

    original = dict(models._cloud_model_cache)
    models._cloud_model_cache.clear()
    try:
        monkeypatch.setattr("row_bot.models._sync_custom_model_cache", lambda: None)
        monkeypatch.setattr("row_bot.providers.status.provider_status", lambda provider_id: {"configured": provider_id == "opencode_go"})
        models._cloud_model_cache["model:opencode_go:glm-5.1"] = {
            "provider": "opencode_go",
            "label": "GLM 5.1",
            "ctx": 131072,
            "capabilities_snapshot": {"tasks": ["chat"]},
        }

        card = next(card for card in provider_status_cards() if card["provider_id"] == "opencode_go")

        assert card["configured"] is True
        assert card["group"] == "API Providers"
        assert card["model_count"] == 1
        assert card["chat_count"] == 1
    finally:
        models._cloud_model_cache.clear()
        models._cloud_model_cache.update(original)


class _FakeKeyring:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, account: str) -> str | None:
        return self.values.get((service, account))

    def set_password(self, service: str, account: str, value: str) -> None:
        self.values[(service, account)] = value

    def delete_password(self, service: str, account: str) -> None:
        self.values.pop((service, account), None)


def test_ux_opencode_api_keys_use_existing_keyring_backed_store(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path))
    for env_var in ("OPENCODE_ZEN_API_KEY", "OPENCODE_GO_API_KEY"):
        monkeypatch.delenv(env_var, raising=False)

    import row_bot.api_keys as api_keys
    import row_bot.secret_store as secret_store

    secret_store = importlib.reload(secret_store)
    api_keys = importlib.reload(api_keys)
    backend = _FakeKeyring()
    secret_store._set_backend_for_tests(backend)
    try:
        api_keys.set_key("OPENCODE_ZEN_API_KEY", "zen-secret-1234")
        api_keys.set_key("OPENCODE_GO_API_KEY", "go-secret-5678")

        metadata = json.loads(Path(api_keys.KEYS_PATH).read_text(encoding="utf-8"))
        encoded = json.dumps(metadata)

        assert api_keys.get_key("OPENCODE_ZEN_API_KEY") == "zen-secret-1234"
        assert api_keys.get_key("OPENCODE_GO_API_KEY") == "go-secret-5678"
        assert metadata["keys"]["OPENCODE_ZEN_API_KEY"]["fingerprint"] == "****1234"
        assert metadata["keys"]["OPENCODE_GO_API_KEY"]["fingerprint"] == "****5678"
        assert "zen-secret-1234" not in encoded
        assert "go-secret-5678" not in encoded
    finally:
        secret_store._set_backend_for_tests(None)
