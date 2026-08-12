from __future__ import annotations

from pathlib import Path


def test_atlascloud_live_fetch_uses_provider_qualified_keys_and_preserves_openrouter(monkeypatch):
    import httpx
    import row_bot.models as models
    import row_bot.providers.auth_store as auth_store

    old_cache = dict(models._cloud_model_cache)

    class _Response:
        status_code = 200
        text = "{}"

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": [
                    {
                        "id": "qwen/qwen3-32b",
                        "name": "Qwen3 32B",
                        "context_length": 131_072,
                        "supported_features": ["json_mode", "structured_outputs", "tools"],
                    },
                    {"id": "Qwen/Qwen3-VL-235B-A22B-Instruct", "context_length": 131_072},
                ]
            }

    monkeypatch.setattr(auth_store, "get_provider_secret", lambda provider_id: "atlas-key" if provider_id == "atlascloud" else "")
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: _Response())
    try:
        models._cloud_model_cache.clear()
        models._cloud_model_cache["qwen/qwen3-32b"] = {
            "provider": "openrouter",
            "label": "OpenRouter Qwen3 32B",
            "ctx": 1_000_000,
        }

        count = models.fetch_cloud_models("atlascloud")

        assert count == 2
        assert models._cloud_model_cache["qwen/qwen3-32b"]["provider"] == "openrouter"
        assert models._cloud_model_cache["model:atlascloud:qwen/qwen3-32b"]["provider"] == "atlascloud"
        assert (
            models._cloud_model_cache["model:atlascloud:qwen/qwen3-32b"]["capabilities_snapshot"]["tool_calling"]
            is True
        )
        assert models.get_cloud_provider("qwen/qwen3-32b") == "openrouter"
        assert models.get_cloud_provider("model:atlascloud:qwen/qwen3-32b") == "atlascloud"
        assert "model:atlascloud:Qwen/Qwen3-VL-235B-A22B-Instruct" in models.list_cloud_vision_models()
    finally:
        models._cloud_model_cache.clear()
        models._cloud_model_cache.update(old_cache)


def test_atlascloud_runtime_accepts_provider_ref(monkeypatch):
    import row_bot.providers.runtime as runtime

    monkeypatch.setattr(runtime, "get_provider_secret", lambda provider_id: "atlas-key")

    model = runtime.create_chat_model("model:atlascloud:Qwen/Qwen3-VL-235B-A22B-Instruct")

    assert type(model).__name__ == "ChatOpenAICompatible"
    assert model.model_name == "Qwen/Qwen3-VL-235B-A22B-Instruct"
    assert model.base_url == "https://api.atlascloud.ai/v1"
    assert model.endpoint["provider_id"] == "atlascloud"
    assert model.endpoint["profile"] == "atlascloud"


def test_validate_atlascloud_key_lists_models(monkeypatch):
    import httpx
    import row_bot.models as models

    captured = {}

    class _Response:
        status_code = 200
        text = "{}"

        def json(self):
            return {"data": []}

    def _fake_get(url, **kwargs):
        captured["url"] = url
        captured["headers"] = dict(kwargs.get("headers") or {})
        return _Response()

    monkeypatch.setattr(httpx, "get", _fake_get)

    assert models.validate_atlascloud_key("atlas-key") is True
    assert captured["url"] == "https://api.atlascloud.ai/v1/models"
    assert captured["headers"]["Authorization"] == "Bearer atlas-key"


def test_atlascloud_fetch_filters_media_generation_rows(monkeypatch):
    import httpx
    import row_bot.models as models
    import row_bot.providers.auth_store as auth_store
    from row_bot.providers.capabilities import snapshot_supports_surface

    old_cache = dict(models._cloud_model_cache)

    class _Response:
        status_code = 200
        text = "{}"

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": [
                    {"id": "deepseek-v3", "context_length": 128_000},
                    {"id": "nano-banana-2/text-to-image", "name": "Nano Banana 2 Text-to-Image"},
                    {"id": "kling-v2.0/text-to-video", "name": "Kling Text-to-Video"},
                    {"id": "audio/tts", "name": "Text to Speech"},
                ]
            }

    monkeypatch.setattr(auth_store, "get_provider_secret", lambda provider_id: "atlas-key" if provider_id == "atlascloud" else "")
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: _Response())
    try:
        models._cloud_model_cache.clear()
        count = models.fetch_cloud_models("atlascloud")

        assert count == 1
        assert "model:atlascloud:deepseek-v3" in models._cloud_model_cache
        assert all("nano-banana" not in key for key in models._cloud_model_cache)
        assert all("kling" not in key for key in models._cloud_model_cache)
        snapshot = models._cloud_model_cache["model:atlascloud:deepseek-v3"]["capabilities_snapshot"]
        assert snapshot_supports_surface(snapshot, "chat") is True
        assert snapshot_supports_surface(snapshot, "image") is False
        assert snapshot_supports_surface(snapshot, "video") is False
        assert "model:atlascloud:deepseek-v3" not in models.list_cloud_vision_models()
    finally:
        models._cloud_model_cache.clear()
        models._cloud_model_cache.update(old_cache)


def test_atlascloud_model_catalog_rows_are_canonical_and_surface_scoped(monkeypatch):
    from row_bot.providers.model_catalog import build_model_catalog_rows, rows_for_surface

    monkeypatch.setattr("row_bot.providers.model_catalog._provider_status_by_id", lambda: {
        "atlascloud": {"configured": True},
    })
    cloud_cache = {
        "model:atlascloud:deepseek-v3": {
            "provider": "atlascloud",
            "label": "DeepSeek V3",
            "ctx": 128_000,
            "transport": "openai_chat",
            "capabilities_snapshot": {
                "tasks": ["chat"],
                "input_modalities": ["text"],
                "output_modalities": ["text"],
                "tool_calling": None,
                "streaming": True,
                "transport": "openai_chat",
                "endpoint_compatibility": ["openai_chat"],
            },
        },
        "model:atlascloud:Qwen/Qwen3-VL-235B-A22B-Instruct": {
            "provider": "atlascloud",
            "label": "Qwen3 VL",
            "ctx": 131_072,
            "transport": "openai_chat",
            "capabilities_snapshot": {
                "tasks": ["chat"],
                "input_modalities": ["image", "text"],
                "output_modalities": ["text"],
                "tool_calling": None,
                "streaming": True,
                "transport": "openai_chat",
                "endpoint_compatibility": ["openai_chat"],
            },
        },
    }

    rows = build_model_catalog_rows(cloud_cache=cloud_cache, ollama_rows=[], quick_choices=[])
    refs = {row.selection_ref for row in rows}
    vision_refs = {row.selection_ref for row in rows_for_surface(rows, "vision")}
    image_refs = {row.selection_ref for row in rows_for_surface(rows, "image")}
    video_refs = {row.selection_ref for row in rows_for_surface(rows, "video")}

    assert "model:atlascloud:deepseek-v3" in refs
    assert "model:atlascloud:Qwen/Qwen3-VL-235B-A22B-Instruct" in refs
    assert "model:atlascloud:deepseek-v3" not in vision_refs
    assert "model:atlascloud:Qwen/Qwen3-VL-235B-A22B-Instruct" in vision_refs
    assert not any(ref.startswith("model:atlascloud:") for ref in image_refs)
    assert not any(ref.startswith("model:atlascloud:") for ref in video_refs)


def test_refresh_cloud_models_preserves_atlas_rows_when_fetch_fails(monkeypatch):
    import row_bot.models as models

    old_cache = dict(models._cloud_model_cache)
    old_current = models._current_model
    calls: list[str] = []

    def _fake_fetch(provider: str) -> int:
        calls.append(provider)
        return 0

    monkeypatch.setattr(models, "fetch_context_catalog", lambda: 0)
    monkeypatch.setattr(models, "_fetch_cloud_models", _fake_fetch)
    monkeypatch.setattr(models, "_save_cloud_cache", lambda: None)
    try:
        models._cloud_model_cache.clear()
        models._cloud_model_cache["model:atlascloud:deepseek-v3"] = {
            "provider": "atlascloud",
            "label": "DeepSeek V3",
            "ctx": 128_000,
        }
        models._current_model = "model:atlascloud:deepseek-v3"

        models.refresh_cloud_models()

        assert "atlascloud" in calls
        assert models._cloud_model_cache["model:atlascloud:deepseek-v3"]["provider"] == "atlascloud"
    finally:
        models._current_model = old_current
        models._cloud_model_cache.clear()
        models._cloud_model_cache.update(old_cache)


def test_atlascloud_media_rows_do_not_leak_to_media_options(monkeypatch):
    import row_bot.api_keys as api_keys
    from row_bot.providers.media import IMAGE_PROVIDER_META, VIDEO_PROVIDER_META, media_model_options

    monkeypatch.setattr(api_keys, "get_key", lambda key: "")
    assert "atlascloud" not in IMAGE_PROVIDER_META
    assert "atlascloud" not in VIDEO_PROVIDER_META

    cloud_cache = {
        "model:atlascloud:nano-banana-2/text-to-image": {
            "provider": "atlascloud",
            "label": "Nano Banana 2",
            "capabilities_snapshot": {
                "tasks": ["image_generation"],
                "input_modalities": ["text"],
                "output_modalities": ["image"],
                "transport": "openai_chat",
            },
        },
        "model:atlascloud:kling-v2.0/text-to-video": {
            "provider": "atlascloud",
            "label": "Kling",
            "capabilities_snapshot": {
                "tasks": ["video_generation"],
                "input_modalities": ["text"],
                "output_modalities": ["video"],
                "transport": "openai_chat",
            },
        },
    }

    assert media_model_options("image", cloud_cache) == {}
    assert media_model_options("video", cloud_cache) == {}


def test_atlascloud_openrouter_style_tool_metadata_is_classified():
    from row_bot.providers.catalog import classify_model_capabilities

    live_schema = classify_model_capabilities(
        "atlascloud",
        "qwen/qwen3-32b",
        {"supported_features": ["json_mode", "structured_outputs", "tools"]},
    )
    live_schema_no_tools = classify_model_capabilities(
        "atlascloud",
        "deepseek-v3",
        {"supported_features": ["json_mode", "reasoning"]},
    )
    live_schema_no_tools_overrides_direct_boolean = classify_model_capabilities(
        "atlascloud",
        "deepseek-v3",
        {"supported_features": ["json_mode", "reasoning"], "tool_calling": True},
    )
    nested_live_schema = classify_model_capabilities(
        "atlascloud",
        "qwen/qwen3-32b",
        {"metadata": {"supported_features": ["tools"]}},
    )
    direct_boolean_fallback = classify_model_capabilities(
        "atlascloud",
        "qwen/qwen3-32b",
        {"tool_calling": True},
    )
    snake_case = classify_model_capabilities(
        "atlascloud",
        "qwen/qwen3-32b",
        {"supported_parameters": ["temperature", "tools", "tool_choice"]},
    )
    camel_case = classify_model_capabilities(
        "atlascloud",
        "qwen/qwen3-32b",
        {"supportedParameters": ["temperature", "tool-choice"]},
    )
    parameter_map = classify_model_capabilities(
        "atlascloud",
        "qwen/qwen3-32b",
        {"parameters": {"temperature": {}, "tools": {"supported": True}}},
    )
    no_tools = classify_model_capabilities(
        "atlascloud",
        "qwen/qwen3-32b",
        {"supported_parameters": ["temperature", "max_tokens"]},
    )
    upstream_qwen = classify_model_capabilities("atlascloud", "qwen/qwen3-32b", {})
    missing_unknown = classify_model_capabilities("atlascloud", "unknown/model", {})

    assert live_schema["tool_calling"] is True
    assert live_schema_no_tools["tool_calling"] is False
    assert live_schema_no_tools_overrides_direct_boolean["tool_calling"] is False
    assert nested_live_schema["tool_calling"] is True
    assert direct_boolean_fallback["tool_calling"] is True
    assert snake_case["tool_calling"] is True
    assert camel_case["tool_calling"] is True
    assert parameter_map["tool_calling"] is True
    assert no_tools["tool_calling"] is False
    assert upstream_qwen["tool_calling"] is True
    assert missing_unknown["tool_calling"] is None


def test_atlascloud_sparse_routed_upstream_models_infer_tools_and_vision():
    from row_bot.providers.capabilities import snapshot_supports_surface
    from row_bot.providers.catalog import classify_model_capabilities

    sparse_metadata = {
        "input_modalities": ["text"],
        "output_modalities": ["text"],
    }
    for model_id in (
        "openai/gpt-4o",
        "openai/gpt-5.1",
        "google/gemini-2.5-flash",
        "google/gemini-2.5-pro",
        "anthropic/claude-sonnet-4-20250514",
    ):
        classified = classify_model_capabilities("atlascloud", model_id, sparse_metadata)

        assert classified["tool_calling"] is True
        assert "image" in classified["input_modalities"]
        assert snapshot_supports_surface(classified, "chat") is True
        assert snapshot_supports_surface(classified, "vision") is True
        assert snapshot_supports_surface(classified, "image") is False
        assert snapshot_supports_surface(classified, "video") is False


def test_atlascloud_upstream_capabilities_keep_explicit_negatives_and_known_vision_negatives():
    from row_bot.providers.capabilities import snapshot_supports_surface
    from row_bot.providers.catalog import classify_model_capabilities

    explicit_no_tools = classify_model_capabilities(
        "atlascloud",
        "openai/gpt-4o",
        {
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "supported_features": ["json_mode", "reasoning"],
        },
    )
    o3_mini = classify_model_capabilities(
        "atlascloud",
        "openai/o3-mini",
        {
            "input_modalities": ["text"],
            "output_modalities": ["text"],
        },
    )
    unknown = classify_model_capabilities(
        "atlascloud",
        "unknown/frontier-chat",
        {
            "input_modalities": ["text"],
            "output_modalities": ["text"],
        },
    )

    assert explicit_no_tools["tool_calling"] is False
    assert "image" in explicit_no_tools["input_modalities"]
    assert snapshot_supports_surface(explicit_no_tools, "vision") is True

    assert o3_mini["tool_calling"] is True
    assert "image" not in o3_mini["input_modalities"]
    assert snapshot_supports_surface(o3_mini, "vision") is False

    assert unknown["tool_calling"] is None
    assert "image" not in unknown["input_modalities"]
    assert snapshot_supports_surface(unknown, "vision") is False


def test_atlascloud_readiness_routes_unknown_tools_to_chat_only(monkeypatch):
    import row_bot.models as models
    from row_bot.providers.models import TransportMode
    from row_bot.providers.readiness import evaluate_runtime_readiness
    from row_bot.providers.resolution import resolve_provider_config

    monkeypatch.setattr(models, "get_context_policy", lambda value: models.ContextPolicy(
        model_ref="model:atlascloud:deepseek-v3",
        provider_id="atlascloud",
        runtime_model="deepseek-v3",
        native_max=128_000,
        user_cap=128_000,
        effective_context=128_000,
        policy_kind="provider",
        cap_source="provider_metadata",
        request_application="trim_only",
    ))

    result = evaluate_runtime_readiness(
        resolve_provider_config("model:atlascloud:deepseek-v3"),
        capability_snapshot={
            "tasks": ["chat"],
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "tool_calling": None,
            "streaming": True,
            "transport": TransportMode.OPENAI_CHAT.value,
            "endpoint_compatibility": [TransportMode.OPENAI_CHAT.value],
        },
        status={"configured": True},
    )

    assert result.agent.ready is False
    assert result.chat.ready is True
    assert result.selected_mode == "chat_only"
    assert any("Atlas Cloud structured tool support is unknown" in error for error in result.agent.errors)


def test_atlascloud_metadata_tool_support_makes_agent_ready(monkeypatch):
    import row_bot.models as models
    from row_bot.providers.models import TransportMode
    from row_bot.providers.readiness import evaluate_runtime_readiness
    from row_bot.providers.resolution import resolve_provider_config

    monkeypatch.setattr(models, "get_context_policy", lambda value: models.ContextPolicy(
        model_ref="model:atlascloud:qwen/qwen3-32b",
        provider_id="atlascloud",
        runtime_model="qwen/qwen3-32b",
        native_max=128_000,
        user_cap=128_000,
        effective_context=128_000,
        policy_kind="provider",
        cap_source="provider_metadata",
        request_application="trim_only",
    ))

    result = evaluate_runtime_readiness(
        resolve_provider_config("model:atlascloud:qwen/qwen3-32b"),
        capability_snapshot={
            "tasks": ["chat"],
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "tool_calling": True,
            "streaming": True,
            "transport": TransportMode.OPENAI_CHAT.value,
            "endpoint_compatibility": [TransportMode.OPENAI_CHAT.value],
            "source_confidence": "live_atlascloud_model_list",
        },
        status={"configured": True},
    )

    assert result.selected_mode == "agent"
    assert result.agent.ready is True
    assert result.agent.tool_calling is True
    assert result.agent.tool_round_trip is True
    assert result.agent.tool_calling_source == "atlascloud_metadata"
    assert result.agent.capability_source == "catalog"


def test_atlascloud_upstream_tool_support_makes_sparse_big_three_agent_ready(monkeypatch):
    import row_bot.models as models
    from row_bot.providers.catalog import classify_model_capabilities
    from row_bot.providers.readiness import evaluate_runtime_readiness
    from row_bot.providers.resolution import resolve_provider_config

    monkeypatch.setattr(models, "get_context_policy", lambda value: models.ContextPolicy(
        model_ref="model:atlascloud:openai/gpt-4o",
        provider_id="atlascloud",
        runtime_model="openai/gpt-4o",
        native_max=128_000,
        user_cap=128_000,
        effective_context=128_000,
        policy_kind="provider",
        cap_source="provider_metadata",
        request_application="trim_only",
    ))

    result = evaluate_runtime_readiness(
        resolve_provider_config("model:atlascloud:openai/gpt-4o"),
        capability_snapshot=classify_model_capabilities(
            "atlascloud",
            "openai/gpt-4o",
            {
                "input_modalities": ["text"],
                "output_modalities": ["text"],
            },
        ),
        status={"configured": True},
    )

    assert result.selected_mode == "agent"
    assert result.agent.ready is True
    assert result.agent.tool_calling is True
    assert result.agent.tool_round_trip is True
    assert result.agent.tool_calling_source == "atlascloud_metadata"


def test_atlascloud_upstream_vision_snapshots_work_with_vision_compatibility(monkeypatch):
    import row_bot.models as models
    from row_bot.providers.atlascloud import atlascloud_model_info_from_metadata
    from row_bot.providers.catalog import model_info_to_cache_entry
    from row_bot.vision import vision_model_compatibility

    old_cache = dict(models._cloud_model_cache)
    try:
        models._cloud_model_cache.clear()
        vision_info = atlascloud_model_info_from_metadata(
            "openai/gpt-4o",
            {
                "input_modalities": ["text"],
                "output_modalities": ["text"],
                "context_length": 128_000,
            },
        )
        text_only_info = atlascloud_model_info_from_metadata(
            "openai/o3-mini",
            {
                "input_modalities": ["text"],
                "output_modalities": ["text"],
                "context_length": 128_000,
            },
        )
        assert vision_info is not None
        assert text_only_info is not None
        models._cloud_model_cache[vision_info.selection_ref] = model_info_to_cache_entry(vision_info)
        models._cloud_model_cache[text_only_info.selection_ref] = model_info_to_cache_entry(text_only_info)

        assert vision_model_compatibility("model:atlascloud:openai/gpt-4o")["usable"] is True
        o3_compatibility = vision_model_compatibility("model:atlascloud:openai/o3-mini")
        assert o3_compatibility["usable"] is False
        assert o3_compatibility["explicit"] is True
    finally:
        models._cloud_model_cache.clear()
        models._cloud_model_cache.update(old_cache)


def test_atlascloud_stale_probe_for_other_model_does_not_override_metadata(monkeypatch):
    import row_bot.models as models
    from row_bot.providers.models import TransportMode
    from row_bot.providers.readiness import evaluate_agent_readiness
    from row_bot.providers.resolution import resolve_provider_config

    monkeypatch.setattr(models, "get_context_policy", lambda value: models.ContextPolicy(
        model_ref="model:atlascloud:qwen/qwen3-32b",
        provider_id="atlascloud",
        runtime_model="qwen/qwen3-32b",
        native_max=128_000,
        user_cap=128_000,
        effective_context=128_000,
        policy_kind="provider",
        cap_source="provider_metadata",
        request_application="trim_only",
    ))

    result = evaluate_agent_readiness(
        resolve_provider_config("model:atlascloud:qwen/qwen3-32b"),
        capability_snapshot={
            "tasks": ["chat"],
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "tool_calling": True,
            "streaming": True,
            "transport": TransportMode.OPENAI_CHAT.value,
            "endpoint_compatibility": [TransportMode.OPENAI_CHAT.value],
        },
        status={
            "configured": True,
            "last_runtime_probe": {
                "ok": False,
                "tool_calling": False,
                "tool_round_trip": False,
                "model_id": "deepseek-v3",
                "errors": ["model did not return a structured tool call"],
            },
        },
    )

    assert result.ready is True
    assert result.tool_calling_source == "atlascloud_metadata"


def test_atlascloud_runtime_probe_can_promote_agent_readiness(monkeypatch):
    import row_bot.models as models
    from row_bot.providers.models import TransportMode
    from row_bot.providers.readiness import evaluate_agent_readiness
    from row_bot.providers.resolution import resolve_provider_config

    monkeypatch.setattr(models, "get_context_policy", lambda value: models.ContextPolicy(
        model_ref="model:atlascloud:deepseek-v3",
        provider_id="atlascloud",
        runtime_model="deepseek-v3",
        native_max=128_000,
        user_cap=128_000,
        effective_context=128_000,
        policy_kind="provider",
        cap_source="provider_metadata",
        request_application="trim_only",
    ))

    result = evaluate_agent_readiness(
        resolve_provider_config("model:atlascloud:deepseek-v3"),
        capability_snapshot={
            "tasks": ["chat"],
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "tool_calling": None,
            "streaming": True,
            "transport": TransportMode.OPENAI_CHAT.value,
            "endpoint_compatibility": [TransportMode.OPENAI_CHAT.value],
        },
        status={
            "configured": True,
            "last_runtime_probe": {
                "ok": True,
                "tool_calling": True,
                "tool_round_trip": True,
                "streaming_tool_calling": False,
            },
        },
    )

    assert result.ready is True
    assert result.tool_calling is True
    assert result.tool_round_trip is True
    assert result.tool_calling_source == "runtime_probe"
    assert result.capability_source == "runtime_probe"


def test_atlascloud_runtime_probe_map_can_promote_specific_model(monkeypatch):
    import row_bot.models as models
    from row_bot.providers.models import TransportMode
    from row_bot.providers.readiness import evaluate_agent_readiness
    from row_bot.providers.resolution import resolve_provider_config

    monkeypatch.setattr(models, "get_context_policy", lambda value: models.ContextPolicy(
        model_ref="model:atlascloud:qwen/qwen3-32b",
        provider_id="atlascloud",
        runtime_model="qwen/qwen3-32b",
        native_max=128_000,
        user_cap=128_000,
        effective_context=128_000,
        policy_kind="provider",
        cap_source="provider_metadata",
        request_application="trim_only",
    ))

    result = evaluate_agent_readiness(
        resolve_provider_config("model:atlascloud:qwen/qwen3-32b"),
        capability_snapshot={
            "tasks": ["chat"],
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "tool_calling": None,
            "streaming": True,
            "transport": TransportMode.OPENAI_CHAT.value,
            "endpoint_compatibility": [TransportMode.OPENAI_CHAT.value],
        },
        status={
            "configured": True,
            "last_runtime_probe": {
                "ok": False,
                "tool_calling": False,
                "tool_round_trip": False,
                "model_id": "deepseek-v3",
                "errors": ["other model failed"],
            },
            "runtime_probes": {
                "qwen/qwen3-32b": {
                    "ok": True,
                    "tool_calling": True,
                    "tool_round_trip": True,
                    "streaming_tool_calling": False,
                    "model_id": "qwen/qwen3-32b",
                    "errors": [],
                },
            },
        },
    )

    assert result.ready is True
    assert result.tool_calling_source == "runtime_probe"
    assert result.tool_round_trip is True


def test_atlascloud_runtime_probe_persists_to_provider_status(tmp_path, monkeypatch):
    import row_bot.providers.config as provider_config
    import row_bot.providers.runtime as runtime
    from row_bot.providers.atlascloud import save_atlascloud_runtime_probe

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(runtime, "provider_secret_status", lambda provider_id, credential_name="api_key": {
        "configured": True,
        "source": "environment",
        "fingerprint": "fp",
    })

    saved = save_atlascloud_runtime_probe({
        "ok": True,
        "chat_ok": True,
        "tool_calling": True,
        "tool_round_trip": True,
        "streaming_tool_calling": False,
        "model_id": "deepseek-v3",
        "errors": [],
    })
    status = runtime.provider_status("atlascloud")

    assert saved["ok"] is True
    assert saved["runtime"] == "openai_compatible_chat"
    assert status["configured"] is True
    assert status["last_runtime_probe"]["ok"] is True
    assert status["last_runtime_probe"]["tool_round_trip"] is True
    assert status["runtime_probes"]["deepseek-v3"]["ok"] is True
    assert status["last_error"] == ""


def test_atlascloud_model_catalog_has_no_provider_specific_probe_action():
    source = Path("src/row_bot/ui/model_catalog.py").read_text(encoding="utf-8")
    settings_source = Path("src/row_bot/ui/settings.py").read_text(encoding="utf-8")

    assert "on_probe_agent" not in source
    assert "Verify Atlas tool support" not in source
    assert "fact_check" not in source
    assert "run_atlascloud_runtime_probe" not in settings_source


def test_atlascloud_setup_wizard_is_wired_as_cloud_provider():
    source = Path("src/row_bot/ui/setup_wizard.py").read_text(encoding="utf-8")

    assert "validate_atlascloud_key" in source
    assert "setup_atlascloud_key" in source
    assert '("atlascloud", atlascloud_val)' in source
    assert 'set_key("ATLASCLOUD_API_KEY", atlascloud_val)' in source
