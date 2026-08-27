from pathlib import Path

from row_bot.providers.capabilities import model_supports_surface
from row_bot.providers.catalog import classify_model_capabilities, get_provider_definition, infer_provider_id, legacy_cache_to_model_infos, model_info_to_cache_entry
from row_bot.providers.model_catalog import CatalogModelRow, build_model_catalog_rows, rows_for_surface
from row_bot.providers.models import ModelInfo, TransportMode
from row_bot.providers.ollama import (
    is_ollama_cloud_offload_model,
    is_ollama_chat_candidate,
    ollama_catalog_rows,
    ollama_model_info,
)


ROOT = Path(__file__).resolve().parents[1]


def test_provider_catalog_infers_existing_api_key_providers():
    assert infer_provider_id("gpt-5") == "openai"
    assert infer_provider_id("claude-sonnet-4-5") == "anthropic"
    assert infer_provider_id("gemini-2.5-pro") == "google"
    assert infer_provider_id("grok-4-1-fast-reasoning") == "xai"
    assert infer_provider_id("anthropic/claude-sonnet-4") == "openrouter"


def test_minimax_provider_definition_and_model_inference():
    definition = get_provider_definition("minimax")
    assert definition is not None
    assert definition.display_name == "MiniMax API"
    assert definition.default_transport == TransportMode.ANTHROPIC_MESSAGES
    assert definition.base_url == "https://api.minimax.io/anthropic"
    assert definition.auth_methods[0].value == "api_key"


def test_atlascloud_provider_definition_and_capabilities():
    from row_bot.providers.resolution import resolve_provider_config

    definition = get_provider_definition("atlascloud")
    assert definition is not None
    assert definition.display_name == "Atlas Cloud"
    assert definition.default_transport == TransportMode.OPENAI_CHAT
    assert definition.base_url == "https://api.atlascloud.ai/v1"
    assert definition.risk_label == "cloud_provider"
    assert definition.auth_methods[0].value == "api_key"

    classified = classify_model_capabilities("atlascloud", "deepseek-ai/DeepSeek-V3-0324")
    assert "chat" in classified["tasks"]
    assert classified["transport"] == TransportMode.OPENAI_CHAT
    assert classified["tool_calling"] is None

    resolved = resolve_provider_config(
        "model:atlascloud:deepseek-ai/DeepSeek-V3-0324",
        allow_legacy_local=False,
    )
    assert resolved.provider_id == "atlascloud"
    assert resolved.model_id == "deepseek-ai/DeepSeek-V3-0324"
    assert resolved.transport == TransportMode.OPENAI_CHAT
    assert resolved.base_url == "https://api.atlascloud.ai/v1"
    assert resolved.risk_label == "cloud_provider"
    assert resolved.execution_location == "remote"


def test_settings_models_tab_does_not_auto_load_heavy_work():
    source = (ROOT / "src" / "row_bot" / "ui" / "settings.py").read_text(encoding="utf-8")

    assert "Load model settings" not in source
    assert "Preparing model settings" not in source
    assert "\n        defer_ui(_load)" not in source
    assert "start_model_catalog_refresh_background" in source


def test_settings_models_uses_cached_catalog_and_defers_camera_probe():
    source = (ROOT / "src" / "row_bot" / "ui" / "settings.py").read_text(encoding="utf-8")
    render_section = source.split("def _render_models_tab_content", 1)[1].split("def _collect_models_tab_data", 1)[0]
    collect_section = source.split("def _collect_models_tab_data", 1)[1].split("def _build_models_tab", 1)[0]

    assert "build_cached_model_catalog_rows" in render_section
    assert "load_ollama_catalog_rows" not in render_section
    assert "build_model_catalog_rows" not in collect_section
    assert "fetch_trending_ollama_models" not in collect_section
    assert "pull_model" not in source
    assert "on_download" not in (ROOT / "src" / "row_bot" / "ui" / "model_catalog.py").read_text(encoding="utf-8")
    assert "cameras = list_cameras()" not in render_section
    assert "await run.io_bound(list_cameras)" in render_section


def test_setup_wizard_does_not_manage_ollama_downloads():
    source = (ROOT / "src" / "row_bot" / "ui" / "setup_wizard.py").read_text(encoding="utf-8")

    assert "pull_model" not in source
    assert "list_all_models" not in source
    assert "POPULAR_MODELS" not in source
    assert "POPULAR_VISION_MODELS" not in source
    assert "setup_brain_dl" not in source
    assert "setup_vision_dl" not in source


def test_ollama_provider_public_catalog_discovery_removed():
    source = (ROOT / "src" / "row_bot" / "providers" / "ollama.py").read_text(encoding="utf-8")

    assert "fetch_ollama_library" not in source
    assert "ollama_provider_catalog_model_ids" not in source
    assert "preferred_ollama_tag_models" not in source
    assert "OLLAMA_LIBRARY_URL" not in source


def test_settings_models_initial_render_is_snapshot_only():
    source = (ROOT / "src" / "row_bot" / "ui" / "settings.py").read_text(encoding="utf-8")
    render_section = source.split("def _render_models_tab_content", 1)[1].split("def _collect_models_tab_data", 1)[0]
    initial_build = render_section.split('ui.label("Models")', 1)[0]

    assert "_ollama_reachable()" not in initial_build
    assert "list_local_models()" not in initial_build
    assert "get_context_policy(" not in initial_build
    assert "get_model_max_context(" not in initial_build
    assert "get_available_image_models()" not in initial_build
    assert "get_available_video_models()" not in initial_build
    assert 'snapshot.get("context_policy")' in initial_build
    assert 'snapshot.get("image_options")' in initial_build
    assert 'snapshot.get("video_options")' in initial_build


def test_model_catalog_pin_refresh_is_async_safe():
    catalog_source = (ROOT / "src" / "row_bot" / "ui" / "model_catalog.py").read_text(encoding="utf-8")
    settings_source = (ROOT / "src" / "row_bot" / "ui" / "settings.py").read_text(encoding="utf-8")

    assert "async def _run_catalog_callback" in catalog_source
    assert "await _run_catalog_callback(on_change)" in catalog_source
    assert "await _run_catalog_callback(on_set_default, surface, row)" in catalog_source
    assert "async def _refresh_top_picker_options" in settings_source
    assert "await run.io_bound(_collect_top_picker_options)" in settings_source
    assert "if inspect.isawaitable(result):" in settings_source


def test_minimax_model_ids_infer_to_minimax_provider():
    for model_id in (
        "MiniMax-M3",
        "MiniMax-M2.7",
        "MiniMax-M2.7-highspeed",
        "MiniMax-M2.5",
        "MiniMax-M2.5-highspeed",
        "MiniMax-M2.1",
        "MiniMax-M2.1-highspeed",
        "MiniMax-M2",
    ):
        assert infer_provider_id(model_id) == "minimax"


def test_minimax_model_capabilities_classified_as_chat():
    classified = classify_model_capabilities("minimax", "MiniMax-M2.7")
    assert "chat" in classified["tasks"]
    assert classified["transport"] == TransportMode.ANTHROPIC_MESSAGES
    assert "text" in classified["input_modalities"]
    assert "text" in classified["output_modalities"]
    assert classified["tool_calling"] is True
    assert classified["streaming"] is True


def test_minimax_m3_capabilities_classified_as_vision_chat_not_video_generation():
    from row_bot.providers.capabilities import snapshot_supports_surface
    from row_bot.providers.models import ModelInfo

    classified = classify_model_capabilities("minimax", "MiniMax-M3")
    info = ModelInfo(
        provider_id="minimax",
        model_id="MiniMax-M3",
        display_name="MiniMax-M3",
        context_window=1_000_000,
        transport=classified["transport"],
        capabilities=frozenset(classified["capabilities"]),
        input_modalities=frozenset(classified["input_modalities"]),
        output_modalities=frozenset(classified["output_modalities"]),
        tasks=frozenset(classified["tasks"]),
        tool_calling=classified["tool_calling"],
        streaming=classified["streaming"],
        endpoint_compatibility=frozenset(classified["endpoint_compatibility"]),
    )
    snapshot = info.capability_snapshot()

    assert classified["transport"] == TransportMode.ANTHROPIC_MESSAGES
    assert "chat" in classified["tasks"]
    assert "image" in classified["input_modalities"]
    assert "video" in classified["input_modalities"]
    assert "text" in classified["output_modalities"]
    assert classified["tool_calling"] is True
    assert classified["streaming"] is True
    assert snapshot_supports_surface(snapshot, "chat") is True
    assert snapshot_supports_surface(snapshot, "vision") is True
    assert snapshot_supports_surface(snapshot, "video") is False


def test_legacy_cache_to_model_infos_preserves_context_and_capabilities():
    infos = legacy_cache_to_model_infos({
        "gpt-4o": {"label": "GPT-4o", "ctx": 128000, "provider": "openai", "vision": True},
    })

    assert len(infos) == 1
    assert infos[0].provider_id == "openai"
    assert infos[0].context_window == 128000
    assert "vision" in infos[0].capabilities
    assert infos[0].selection_ref == "model:openai:gpt-4o"


def test_openai_responses_only_models_are_chat_surface_compatible():
    classified = classify_model_capabilities("openai", "gpt-5.5-pro")

    assert classified["transport"] == TransportMode.OPENAI_RESPONSES
    assert TransportMode.OPENAI_RESPONSES in classified["endpoint_compatibility"]
    assert "responses" in classified["tasks"]


def test_non_chat_models_are_excluded_from_chat_surface():
    classified = classify_model_capabilities("openai", "text-embedding-3-large")
    info = ModelInfo(
        provider_id="openai",
        model_id="text-embedding-3-large",
        display_name="Embedding",
        context_window=8192,
        transport=classified["transport"],
        capabilities=frozenset(classified["capabilities"]),
        input_modalities=frozenset(classified["input_modalities"]),
        output_modalities=frozenset(classified["output_modalities"]),
        tasks=frozenset(classified["tasks"]),
        tool_calling=classified["tool_calling"],
        streaming=classified["streaming"],
        endpoint_compatibility=frozenset(classified["endpoint_compatibility"]),
    )

    assert model_supports_surface(info, "chat") is False
    assert model_supports_surface(info, "embeddings") is True


def test_voice_models_are_voice_surface_not_chat():
    voice_model_ids = [
        "whisper-1",
        "gpt-4o-transcribe",
        "tts-1",
        "gpt-realtime",
        "gpt-4o-audio-preview",
    ]

    for model_id in voice_model_ids:
        classified = classify_model_capabilities("openai", model_id)
        info = ModelInfo(
            provider_id="openai",
            model_id=model_id,
            display_name=model_id,
            context_window=0,
            transport=classified["transport"],
            capabilities=frozenset(classified["capabilities"]),
            input_modalities=frozenset(classified["input_modalities"]),
            output_modalities=frozenset(classified["output_modalities"]),
            tasks=frozenset(classified["tasks"]),
            tool_calling=classified["tool_calling"],
            streaming=classified["streaming"],
            endpoint_compatibility=frozenset(classified["endpoint_compatibility"]),
        )

        assert model_supports_surface(info, "voice") is True
        assert model_supports_surface(info, "audio") is True
        assert model_supports_surface(info, "chat") is False


def test_cache_entry_includes_capability_snapshot():
    info = legacy_cache_to_model_infos({
        "gpt-4o": {"label": "GPT-4o", "ctx": 128000, "provider": "openai", "vision": True},
    })[0]

    entry = model_info_to_cache_entry(info)

    assert entry["capabilities_snapshot"]["tasks"] == ["chat"]
    assert "image" in entry["capabilities_snapshot"]["input_modalities"]
    assert entry["transport"] == "openai_chat"


def test_xai_pricing_matrix_becomes_canonical_generation_metadata():
    from row_bot.providers.capabilities import normalize_snapshot
    from row_bot.providers.catalog import model_info_from_metadata
    from row_bot.providers.xai_catalog import xai_generation_parameters_from_item

    generation_parameters = xai_generation_parameters_from_item({
        "pricing": [
            {"quality": "low", "resolution": "1k", "price_per_image": 4},
            {"quality": "low", "resolution": "2k", "price_per_image": 6},
            {"quality": "medium", "resolution": "1k", "price_per_image": 6},
            {"quality": "medium", "resolution": "2k", "price_per_image": 8},
        ],
    })
    info = model_info_from_metadata(
        "xai",
        "future-renderer",
        {"generation_parameters": generation_parameters},
        display_name="Future Renderer",
    )
    snapshot = info.capability_snapshot()
    normalized = normalize_snapshot(snapshot)

    assert generation_parameters == {
        "options": {"quality": ["low", "medium"], "resolution": ["1k", "2k"]},
        "defaults": {"quality": "medium", "resolution": "1k"},
        "valid_combinations": [
            {"quality": "low", "resolution": "1k"},
            {"quality": "low", "resolution": "2k"},
            {"quality": "medium", "resolution": "1k"},
            {"quality": "medium", "resolution": "2k"},
        ],
    }
    assert info.tasks == frozenset({"image_generation"})
    assert snapshot["generation_parameters"] == generation_parameters
    assert normalized["generation_parameters"] == generation_parameters


def test_openrouter_supported_parameters_mark_tools_supported():
    classified = classify_model_capabilities(
        "openrouter",
        "qwen/qwen3.7-max",
        {"supported_parameters": ["tools", "tool_choice"]},
    )

    assert classified["tool_calling"] is True
    assert "tool_calling" in classified["capabilities"]


def test_ollama_provider_definition_and_model_capabilities():
    definition = get_provider_definition("ollama")
    info = ollama_model_info("qwen3:14b", installed=True, context_window=32768)
    vision_info = ollama_model_info("llava-phi3:3.8b", installed=True)

    assert definition is not None
    assert definition.default_transport == TransportMode.OLLAMA_CHAT
    assert definition.risk_label == "local_private"
    assert info.provider_id == "ollama"
    assert info.transport == TransportMode.OLLAMA_CHAT
    assert "chat" in info.tasks
    assert info.tool_calling is True
    assert model_supports_surface(info, "chat") is True
    assert model_supports_surface(info, "vision") is False
    assert vision_info.tool_calling is False
    assert model_supports_surface(vision_info, "vision") is True


def test_ollama_native_capabilities_mark_unknown_family_as_tool_capable():
    info = ollama_model_info(
        "qwen3.8:27b",
        installed=True,
        metadata={"capabilities": ["completion", "vision", "tools", "thinking"]},
    )

    assert info.tool_calling is True
    assert "tool_calling" in info.capabilities


def test_ollama_native_capabilities_override_tool_capable_family_fallback():
    info = ollama_model_info(
        "qwen3:14b",
        installed=True,
        metadata={"capabilities": ["completion", "thinking"]},
    )

    assert info.tool_calling is False
    assert "tool_calling" not in info.capabilities


def test_ollama_explicit_tool_calling_boolean_precedes_native_capabilities():
    explicitly_disabled = ollama_model_info(
        "qwen3:14b",
        metadata={"tool_calling": False, "capabilities": ["completion", "tools"]},
    )
    explicitly_enabled = ollama_model_info(
        "future-family:latest",
        metadata={"tool_calling": True, "capabilities": ["completion"]},
    )

    assert explicitly_disabled.tool_calling is False
    assert explicitly_enabled.tool_calling is True


def test_ollama_catalog_uses_daemon_reported_vision_capabilities_for_unknown_family():
    model_id = "qwen3.6:35b-a3b-mtp-q4_K_M"

    rows = ollama_catalog_rows(
        [model_id],
        [],
        metadata_by_model={
            model_id: {
                "capabilities": ["completion", "vision"],
                "input_modalities": ["image", "text"],
            },
        },
    )

    assert rows[0]["model_id"] == model_id
    assert "vision" in rows[0]["capabilities_snapshot"]["capabilities"]
    assert "image" in rows[0]["capabilities_snapshot"]["input_modalities"]


def test_ollama_cloud_provider_definition_and_capabilities():
    definition = get_provider_definition("ollama_cloud")
    classified = classify_model_capabilities("ollama_cloud", "gpt-oss:120b-cloud")
    vision = classify_model_capabilities("ollama_cloud", "gemma4:31b-cloud", {"capabilities": ["completion", "vision"]})

    assert definition is not None
    assert definition.display_name == "Ollama Cloud"
    assert definition.default_transport == TransportMode.OLLAMA_CLOUD_CHAT
    assert definition.base_url == "https://ollama.com"
    assert definition.risk_label == "cloud_provider"
    assert "chat" in classified["tasks"]
    assert classified["transport"] == TransportMode.OLLAMA_CLOUD_CHAT
    assert "vision" in vision["capabilities"]
    assert "image" in vision["input_modalities"]


def test_ollama_cloud_offload_models_keep_local_provider_but_cloud_risk():
    info = ollama_model_info("gpt-oss:120b-cloud", installed=True)

    assert is_ollama_cloud_offload_model("gpt-oss:120b-cloud") is True
    assert is_ollama_cloud_offload_model("qwen3:14b") is False
    assert info.provider_id == "ollama"
    assert info.transport == TransportMode.OLLAMA_CHAT
    assert info.risk_label == "cloud_provider"


def test_ollama_cloud_offload_model_can_be_ready_without_local_list(monkeypatch):
    import row_bot.providers.model_catalog as catalog_view

    monkeypatch.setattr(catalog_view, "_provider_status_by_id", lambda: {"ollama": {"configured": True, "source": "local_daemon"}})
    monkeypatch.setattr(catalog_view, "_custom_model_infos", lambda: [])
    monkeypatch.setattr(catalog_view, "_codex_model_infos", lambda: [])
    monkeypatch.setattr(catalog_view, "_curated_media_entries", lambda surface: {})

    rows = build_model_catalog_rows(
        cloud_cache={},
        ollama_rows=ollama_catalog_rows(
            [],
            ["gemma4:31b-cloud"],
            ready_cloud_offload={"gemma4:31b-cloud"},
            metadata_by_model={
                "gemma4:31b-cloud": {
                    "context_window": 262144,
                    "capabilities": ["completion", "vision"],
                },
            },
        ),
    )

    row = rows[0]
    assert row.model_id == "gemma4:31b-cloud"
    assert row.provider_id == "ollama"
    assert row.installed is True
    assert row.runtime_ready is True
    assert row.downloadable is False
    assert row.risk_label == "cloud_provider"
    assert row.availability == "cloud_offload_ready"
    assert row.context_window == 262144
    assert "vision" in row.categories


def test_direct_and_routed_multimodal_chat_models_support_vision_surface():
    direct = classify_model_capabilities("openai", "gpt-5.4")
    routed_google = classify_model_capabilities("openrouter", "google/gemini-2.0-flash-001")
    routed_anthropic = classify_model_capabilities("openrouter", "anthropic/claude-opus-4.6")

    assert "image" in direct["input_modalities"]
    assert "image" in routed_google["input_modalities"]
    assert "image" in routed_anthropic["input_modalities"]


def test_google_nano_banana_models_are_image_generation_surface():
    classified = classify_model_capabilities("google", "gemini-3.1-flash-image-preview")

    assert classified["tasks"] == {"image_generation", "image_edit"}
    assert "image" in classified["input_modalities"]
    assert classified["output_modalities"] == {"image"}


def test_xai_imagine_image_model_is_image_generation_surface():
    classified = classify_model_capabilities("xai", "grok-imagine-image")

    assert classified["tasks"] == {"image_generation"}
    assert classified["output_modalities"] == {"image"}


def test_xai_imagine_quality_model_is_image_generation_surface():
    classified = classify_model_capabilities("xai", "grok-imagine-image-quality")

    assert classified["tasks"] == {"image_generation"}
    assert classified["output_modalities"] == {"image"}


def test_xai_oauth_imagine_models_are_media_surfaces():
    image = classify_model_capabilities("xai_oauth", "grok-imagine-image")
    video = classify_model_capabilities("xai_oauth", "grok-imagine-video")

    assert image["tasks"] == {"image_generation"}
    assert image["output_modalities"] == {"image"}
    assert image["tool_calling"] is False
    assert image["streaming"] is False
    assert video["tasks"] == {"video_generation"}
    assert video["output_modalities"] == {"video"}
    assert video["tool_calling"] is False
    assert video["streaming"] is False


def test_model_catalog_splits_media_from_chat_and_preserves_pins(monkeypatch):
    import row_bot.providers.model_catalog as catalog_view

    monkeypatch.setattr(catalog_view, "_provider_status_by_id", lambda: {})
    monkeypatch.setattr(catalog_view, "_custom_model_infos", lambda: [])
    monkeypatch.setattr(catalog_view, "_codex_model_infos", lambda: [])
    monkeypatch.setattr(catalog_view, "_curated_media_entries", lambda surface: {})

    rows = build_model_catalog_rows(
        cloud_cache={
            "gpt-4o": {"label": "GPT-4o", "ctx": 128000, "provider": "openai", "vision": True},
            "gpt-image-1": {"label": "GPT Image 1", "ctx": 0, "provider": "openai", "vision": False},
            "veo-3.1-generate-preview": {"label": "Veo 3.1", "ctx": 0, "provider": "google", "vision": False},
        },
        ollama_rows=[],
        defaults={"chat": "gpt-4o", "image": "openai/gpt-image-1"},
        quick_choices=[
            {
                "id": "model:openai:gpt-4o",
                "kind": "model",
                "provider_id": "openai",
                "model_id": "gpt-4o",
                "visibility": ["chat", "workflow", "channels", "designer", "status_tool"],
                "capabilities_snapshot": {
                    "tasks": ["chat"],
                    "input_modalities": ["text", "image"],
                    "output_modalities": ["text"],
                },
            },
            {
                "id": "model:openai:gpt-image-1",
                "kind": "model",
                "provider_id": "openai",
                "model_id": "gpt-image-1",
                "visibility": ["image"],
                "capabilities_snapshot": {
                    "tasks": ["image_generation"],
                    "input_modalities": ["text"],
                    "output_modalities": ["image"],
                },
            },
        ],
    )

    chat_ids = {row.model_id for row in rows_for_surface(rows, "chat")}
    image_ids = {row.model_id for row in rows_for_surface(rows, "image")}
    video_ids = {row.model_id for row in rows_for_surface(rows, "video")}
    by_id = {row.model_id: row for row in rows}

    assert "gpt-4o" in chat_ids
    assert "gpt-image-1" not in chat_ids
    assert "veo-3.1-generate-preview" not in chat_ids
    assert "gpt-image-1" in image_ids
    assert "veo-3.1-generate-preview" in video_ids
    assert "chat" in by_id["gpt-4o"].pinned_surfaces
    assert "image" in by_id["gpt-image-1"].pinned_surfaces
    assert "chat" in by_id["gpt-4o"].default_surfaces
    assert "image" in by_id["gpt-image-1"].default_surfaces


def test_model_catalog_surfaces_connected_xai_oauth_media_as_media_only(monkeypatch):
    import row_bot.providers.model_catalog as catalog_view

    monkeypatch.setattr(
        catalog_view,
        "_provider_status_by_id",
        lambda: {"xai_oauth": {"configured": True, "runtime_enabled": True}},
    )
    monkeypatch.setattr(catalog_view, "_custom_model_infos", lambda: [])
    monkeypatch.setattr(catalog_view, "_codex_model_infos", lambda: [])
    monkeypatch.setattr(catalog_view, "_opencode_model_infos", lambda provider_status=None: [])
    monkeypatch.setattr(catalog_view, "_claude_subscription_model_infos", lambda: [])
    monkeypatch.setattr(catalog_view, "_xai_oauth_model_infos", lambda: [])

    rows = build_model_catalog_rows(cloud_cache={}, ollama_rows=[], quick_choices=[])
    oauth_rows = [row for row in rows if row.provider_id == "xai_oauth"]
    by_model = {row.model_id: row for row in oauth_rows}

    assert by_model["grok-imagine-image"].supports("image")
    assert not by_model["grok-imagine-image"].supports("chat")
    assert by_model["grok-imagine-video"].supports("video")
    assert not by_model["grok-imagine-video"].supports("chat")
    assert by_model["grok-imagine-image"].runtime_ready is True
    assert by_model["grok-imagine-video"].runtime_ready is True


def test_model_catalog_lists_voice_models_on_voice_surface(monkeypatch):
    import row_bot.providers.model_catalog as catalog_view

    monkeypatch.setattr(catalog_view, "_provider_status_by_id", lambda: {"openai": {"configured": True}})
    monkeypatch.setattr(catalog_view, "_custom_model_infos", lambda: [])
    monkeypatch.setattr(catalog_view, "_codex_model_infos", lambda: [])
    monkeypatch.setattr(catalog_view, "_curated_media_entries", lambda surface: {})

    rows = build_model_catalog_rows(
        cloud_cache={
            "whisper-1": {"label": "Whisper", "ctx": 0, "provider": "openai"},
            "gpt-4o-audio-preview": {"label": "Audio Preview", "ctx": 128000, "provider": "openai"},
            "gpt-4o": {"label": "GPT-4o", "ctx": 128000, "provider": "openai"},
        },
        ollama_rows=[],
    )

    voice_ids = {row.model_id for row in rows_for_surface(rows, "voice")}
    chat_ids = {row.model_id for row in rows_for_surface(rows, "chat")}

    assert {"whisper-1", "gpt-4o-audio-preview"} <= voice_ids
    assert "gpt-4o" not in voice_ids
    assert "whisper-1" not in chat_ids
    assert "gpt-4o-audio-preview" not in chat_ids


def test_model_catalog_keeps_agent_incompatible_models_visible_as_chat_only(monkeypatch):
    import row_bot.providers.model_catalog as catalog_view

    monkeypatch.setattr(catalog_view, "_provider_status_by_id", lambda: {"openrouter": {"configured": True}})
    monkeypatch.setattr(catalog_view, "_custom_model_infos", lambda: [])
    monkeypatch.setattr(catalog_view, "_codex_model_infos", lambda: [])
    monkeypatch.setattr(catalog_view, "_curated_media_entries", lambda surface: {})

    rows = build_model_catalog_rows(
        cloud_cache={
            "vendor/chat-no-tools": {
                "label": "No Tools",
                "ctx": 128_000,
                "provider": "openrouter",
                "capabilities_snapshot": {
                    "tasks": ["chat"],
                    "input_modalities": ["text"],
                    "output_modalities": ["text"],
                    "tool_calling": None,
                    "transport": "openai_chat",
                },
            },
        },
        ollama_rows=[],
    )

    assert len(rows) == 1
    assert rows[0].supports("chat") is True
    assert rows[0].runtime_ready is True
    assert rows[0].runtime_mode == "chat_only"
    assert "Chat Only" in rows[0].status_reason


def test_custom_catalog_uses_matching_probe_without_promoting_sibling(tmp_path, monkeypatch):
    import row_bot.providers.config as provider_config
    import row_bot.providers.model_catalog as catalog_view
    from row_bot.providers.custom import custom_provider_id, save_custom_endpoint

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    provider_id = custom_provider_id("llama-live")
    save_custom_endpoint({
        "id": "llama-live",
        "name": "llama.cpp Live",
        "profile": "llama_cpp",
        "base_url": "http://127.0.0.1:1235/v1",
        "execution_location": "local",
        "auth_required": False,
        "models": [
            {"model_id": "probed-model", "display_name": "Probed", "context_window": 65_536},
            {"model_id": "unprobed-model", "display_name": "Unprobed", "context_window": 65_536},
        ],
        "last_probe": {
            "model_id": "probed-model",
            "classification": "agent_ready",
            "chat_ok": True,
            "tool_calling": True,
            "tool_round_trip": True,
        },
    })
    monkeypatch.setattr(
        catalog_view,
        "_provider_status_by_id",
        lambda: {provider_id: {"configured": True, "runtime_enabled": True}},
    )
    monkeypatch.setattr(catalog_view, "_codex_model_infos", lambda: [])
    monkeypatch.setattr(catalog_view, "_curated_media_entries", lambda surface: {})

    rows = build_model_catalog_rows(cloud_cache={}, ollama_rows=[], quick_choices=[])
    custom_rows = {row.model_id: row for row in rows if row.provider_id == provider_id}

    assert custom_rows["probed-model"].runtime_mode == "agent"
    assert custom_rows["probed-model"].status_reason == ""
    assert custom_rows["unprobed-model"].runtime_mode == "chat_only"


def test_custom_catalog_preserves_chat_only_and_failed_probe_results(tmp_path, monkeypatch):
    import row_bot.providers.config as provider_config
    import row_bot.providers.model_catalog as catalog_view
    from row_bot.providers.custom import custom_provider_id, save_custom_endpoint

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    provider_id = custom_provider_id("custom-live")
    monkeypatch.setattr(
        catalog_view,
        "_provider_status_by_id",
        lambda: {provider_id: {"configured": True, "runtime_enabled": True}},
    )
    monkeypatch.setattr(catalog_view, "_codex_model_infos", lambda: [])
    monkeypatch.setattr(catalog_view, "_curated_media_entries", lambda surface: {})

    def _row_for_probe(probe):
        save_custom_endpoint({
            "id": "custom-live",
            "base_url": "http://127.0.0.1:1235/v1",
            "auth_required": False,
            "models": [{"model_id": "model-a", "context_window": 65_536}],
            "last_probe": {"model_id": "model-a", **probe},
        })
        return next(
            row
            for row in build_model_catalog_rows(cloud_cache={}, ollama_rows=[], quick_choices=[])
            if row.provider_id == provider_id
        )

    chat_only = _row_for_probe({"classification": "chat_only", "chat_ok": True, "tool_calling": False})
    failed = _row_for_probe({"classification": "unavailable", "chat_ok": False})

    assert chat_only.runtime_ready is True
    assert chat_only.runtime_mode == "chat_only"
    assert "tool round trip" in chat_only.status_reason
    assert failed.runtime_ready is False
    assert failed.runtime_mode == "blocked"
    assert "did not verify chat" in failed.status_reason


def test_custom_catalog_probe_cannot_bypass_agent_context_floor(tmp_path, monkeypatch):
    import row_bot.providers.config as provider_config
    import row_bot.providers.model_catalog as catalog_view
    from row_bot.providers.custom import custom_provider_id, save_custom_endpoint

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    provider_id = custom_provider_id("small-context")
    save_custom_endpoint({
        "id": "small-context",
        "base_url": "http://127.0.0.1:1235/v1",
        "auth_required": False,
        "models": [{"model_id": "model-a", "context_window": 20_000}],
        "last_probe": {
            "model_id": "model-a",
            "classification": "agent_ready",
            "chat_ok": True,
            "tool_calling": True,
            "tool_round_trip": True,
        },
    })
    monkeypatch.setattr(
        catalog_view,
        "_provider_status_by_id",
        lambda: {provider_id: {"configured": True, "runtime_enabled": True}},
    )
    monkeypatch.setattr(catalog_view, "_codex_model_infos", lambda: [])
    monkeypatch.setattr(catalog_view, "_curated_media_entries", lambda surface: {})

    row = next(
        row
        for row in build_model_catalog_rows(cloud_cache={}, ollama_rows=[], quick_choices=[])
        if row.provider_id == provider_id
    )

    assert row.runtime_ready is True
    assert row.runtime_mode == "chat_only"
    assert "Chat Only" in row.status_reason


def test_openrouter_cached_tool_metadata_makes_agent_ready(monkeypatch):
    import row_bot.providers.model_catalog as catalog_view

    monkeypatch.setattr(catalog_view, "_provider_status_by_id", lambda: {"openrouter": {"configured": True}})
    monkeypatch.setattr(catalog_view, "_custom_model_infos", lambda: [])
    monkeypatch.setattr(catalog_view, "_codex_model_infos", lambda: [])
    monkeypatch.setattr(catalog_view, "_curated_media_entries", lambda surface: {})

    rows = build_model_catalog_rows(
        cloud_cache={
            "vendor/chat-tools": {
                "label": "Tools",
                "ctx": 128_000,
                "provider": "openrouter",
                "capabilities_snapshot": {
                    "tasks": ["chat"],
                    "input_modalities": ["text"],
                    "output_modalities": ["text"],
                    "tool_calling": True,
                    "streaming": True,
                    "transport": "openai_chat",
                    "endpoint_compatibility": ["openai_chat"],
                },
            },
        },
        ollama_rows=[],
    )

    assert len(rows) == 1
    assert rows[0].capabilities_snapshot["tool_calling"] is True
    assert rows[0].runtime_ready is True
    assert rows[0].status_reason == ""


def test_model_catalog_does_not_resurrect_deleted_custom_default(tmp_path, monkeypatch):
    import row_bot.providers.config as provider_config
    import row_bot.providers.model_catalog as catalog_view

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(catalog_view, "_provider_status_by_id", lambda: {})
    monkeypatch.setattr(catalog_view, "_custom_model_infos", lambda: [])
    monkeypatch.setattr(catalog_view, "_codex_model_infos", lambda: [])
    monkeypatch.setattr(catalog_view, "_curated_media_entries", lambda surface: {})

    rows = build_model_catalog_rows(
        cloud_cache={},
        ollama_rows=[],
        defaults={"chat": "model:custom_openai_deleted:ghost-chat"},
        quick_choices=[],
    )

    assert rows == []


def test_openrouter_cached_no_tool_metadata_is_chat_only(monkeypatch):
    import row_bot.providers.model_catalog as catalog_view

    monkeypatch.setattr(catalog_view, "_provider_status_by_id", lambda: {"openrouter": {"configured": True}})
    monkeypatch.setattr(catalog_view, "_custom_model_infos", lambda: [])
    monkeypatch.setattr(catalog_view, "_codex_model_infos", lambda: [])
    monkeypatch.setattr(catalog_view, "_curated_media_entries", lambda surface: {})

    rows = build_model_catalog_rows(
        cloud_cache={
            "vendor/chat-no-tools": {
                "label": "No Tools",
                "ctx": 128_000,
                "provider": "openrouter",
                "capabilities_snapshot": {
                    "tasks": ["chat"],
                    "input_modalities": ["text"],
                    "output_modalities": ["text"],
                    "tool_calling": False,
                    "streaming": True,
                    "transport": "openai_chat",
                    "endpoint_compatibility": ["openai_chat"],
                },
            },
        },
        ollama_rows=[],
    )

    assert len(rows) == 1
    assert rows[0].capabilities_snapshot["tool_calling"] is False
    assert rows[0].runtime_ready is True
    assert rows[0].runtime_mode == "chat_only"
    assert "Chat Only" in rows[0].status_reason


def test_model_catalog_ui_filters_and_bounds_large_provider_groups():
    from row_bot.ui.model_catalog import CATALOG_PROVIDER_ROW_LIMIT, _filter_catalog_rows, _visible_provider_rows

    rows = [
        CatalogModelRow(
            provider_id="openai",
            provider_display_name="OpenAI API",
            model_id=f"gpt-large-{idx}",
            display_name=f"GPT Large {idx}",
            categories=("chat",),
        )
        for idx in range(CATALOG_PROVIDER_ROW_LIMIT + 5)
    ]
    rows.append(
        CatalogModelRow(
            provider_id="google",
            provider_display_name="Google AI",
            model_id="gemini-vision-special",
            display_name="Gemini Vision Special",
            categories=("vision",),
        )
    )

    openai_chat_rows = _filter_catalog_rows(rows, surface="chat", provider="openai")
    special_vision_rows = _filter_catalog_rows(rows, surface="vision", query="special")

    assert len(openai_chat_rows) == CATALOG_PROVIDER_ROW_LIMIT + 5
    assert len(_visible_provider_rows(openai_chat_rows, CATALOG_PROVIDER_ROW_LIMIT)) == CATALOG_PROVIDER_ROW_LIMIT
    assert _visible_provider_rows(openai_chat_rows, -1) == []
    assert [row.model_id for row in special_vision_rows] == ["gemini-vision-special"]


def test_model_catalog_excludes_non_daemon_ollama_rows(monkeypatch):
    import row_bot.providers.model_catalog as catalog_view

    monkeypatch.setattr(catalog_view, "_provider_status_by_id", lambda: {"ollama": {"configured": True, "source": "local_daemon"}})
    monkeypatch.setattr(catalog_view, "_custom_model_infos", lambda: [])
    monkeypatch.setattr(catalog_view, "_codex_model_infos", lambda: [])
    monkeypatch.setattr(catalog_view, "_curated_media_entries", lambda surface: {})

    rows = build_model_catalog_rows(
        cloud_cache={},
        ollama_rows=ollama_catalog_rows([], ["qwen3.6:27b"]),
        defaults={},
        quick_choices=[],
    )

    assert rows == []


def test_legacy_model_facade_uses_ollama_tool_capability_catalog():
    from row_bot.models import is_tool_compatible

    assert is_tool_compatible("qwen3.6:27b") is True
    assert is_tool_compatible("qwen3.6:35b-a3b") is True


def test_local_ollama_discovery_uses_http_fallback(monkeypatch):
    import row_bot.models as models

    monkeypatch.setattr(models, "_ollama_reachable", lambda: True)
    monkeypatch.setattr(models, "_ollama_client", lambda: None)
    monkeypatch.setattr(
        models,
        "_ollama_http_json",
        lambda path, payload=None, **kwargs: {
            "models": [{"name": "vendor/non-tool-chat:14b"}]
        } if path == "/api/tags" else {},
    )

    assert models.list_local_models() == ["vendor/non-tool-chat:14b"]


def test_local_ollama_runtime_expands_unique_family_alias(monkeypatch):
    import row_bot.models as models

    monkeypatch.setattr(models, "list_local_models", lambda: ["llama3:latest"])

    assert models._ollama_runtime_model_name("model:ollama:llama3") == "llama3:latest"


def test_local_ollama_runtime_keeps_ambiguous_family_alias(monkeypatch):
    import row_bot.models as models

    monkeypatch.setattr(
        models,
        "list_local_models",
        lambda: ["llama3:8b", "llama3:70b"],
    )

    assert models._ollama_runtime_model_name("model:ollama:llama3") == "llama3"


def test_local_ollama_context_uses_http_show_fallback(monkeypatch):
    import row_bot.models as models

    models._model_max_ctx_cache.clear()
    monkeypatch.setattr(models, "is_cloud_model", lambda model_name: False)
    monkeypatch.setattr(models, "_ollama_client", lambda: None)
    monkeypatch.setattr(
        models,
        "_ollama_http_json",
        lambda path, payload=None, **kwargs: {
            "model_info": {
                "general.architecture": "llama",
                "llama.context_length": 131072,
            }
        } if path == "/api/show" else {},
    )

    assert models.get_model_max_context("model:ollama:vendor/non-tool-chat:14b") == 131072


def test_ollama_embedding_model_is_not_chat_surface():
    info = ollama_model_info("nomic-embed-text:latest", installed=True)

    assert model_supports_surface(info, "chat") is False
    assert model_supports_surface(info, "embeddings") is True


def test_ollama_catalog_includes_installed_unknown_chat_models(monkeypatch):
    import row_bot.providers.model_catalog as catalog_view

    monkeypatch.setattr(catalog_view, "_provider_status_by_id", lambda: {"ollama": {"configured": True, "source": "local_daemon"}})
    monkeypatch.setattr(catalog_view, "_custom_model_infos", lambda: [])
    monkeypatch.setattr(catalog_view, "_codex_model_infos", lambda: [])
    monkeypatch.setattr(catalog_view, "_curated_media_entries", lambda surface: {})

    assert is_ollama_chat_candidate("gemma4:e4b") is True
    rows = build_model_catalog_rows(
        cloud_cache={},
        ollama_rows=ollama_catalog_rows(["gemma4:e4b", "nomic-embed-text:latest"], []),
        defaults={},
        quick_choices=[],
    )

    by_id = {row.model_id: row for row in rows}
    assert "gemma4:e4b" in by_id
    assert "nomic-embed-text:latest" not in by_id
    assert "gemma4:e4b" in {row.model_id for row in rows_for_surface(rows, "chat")}
    assert "gemma4:e4b" not in {row.model_id for row in rows_for_surface(rows, "vision")}
    assert by_id["gemma4:e4b"].installed is True
    assert by_id["gemma4:e4b"].downloadable is False


def test_ollama_catalog_rows_are_daemon_only():
    rows = ollama_catalog_rows(
        ["qwen3:14b", "gemma3:4b", "llava-phi3:3.8b"],
        ["qwen3:14b", "mistral:7b", "mistral:7b", "moondream:latest", "phi4:14b"],
        context_windows={"qwen3:14b": 32768},
    )

    by_id = {row["model_id"]: row for row in rows}
    assert list(by_id) == ["gemma3:4b", "llava-phi3:3.8b", "qwen3:14b"]
    assert by_id["qwen3:14b"]["installed"] is True
    assert by_id["qwen3:14b"]["context_window"] == 32768
    assert by_id["gemma3:4b"]["installed"] is True
    assert "image" in by_id["gemma3:4b"]["capabilities_snapshot"]["input_modalities"]
    assert "image" in by_id["llava-phi3:3.8b"]["capabilities_snapshot"]["input_modalities"]
    assert all(row["downloadable"] is False for row in rows)
    assert all(row["recommended"] is False for row in rows)
