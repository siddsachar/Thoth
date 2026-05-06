from providers.capabilities import model_supports_surface
from providers.catalog import classify_model_capabilities, get_provider_definition, infer_provider_id, legacy_cache_to_model_infos, model_info_to_cache_entry
from providers.model_catalog import CatalogModelRow, build_model_catalog_rows, rows_for_surface
from providers.models import ModelInfo, TransportMode
from providers.ollama import (
    extract_ollama_library_family_ids,
    extract_ollama_library_model_ids,
    ollama_catalog_rows,
    ollama_model_info,
    ollama_provider_catalog_model_ids,
    preferred_ollama_tag_models,
)


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


def test_minimax_model_ids_infer_to_minimax_provider():
    for model_id in (
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


def test_cache_entry_includes_capability_snapshot():
    info = legacy_cache_to_model_infos({
        "gpt-4o": {"label": "GPT-4o", "ctx": 128000, "provider": "openai", "vision": True},
    })[0]

    entry = model_info_to_cache_entry(info)

    assert entry["capabilities_snapshot"]["tasks"] == ["chat"]
    assert "image" in entry["capabilities_snapshot"]["input_modalities"]
    assert entry["transport"] == "openai_chat"


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


def test_model_catalog_splits_media_from_chat_and_preserves_pins(monkeypatch):
    import providers.model_catalog as catalog_view

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


def test_model_catalog_ui_filters_and_bounds_large_provider_groups():
    from ui.model_catalog import CATALOG_PROVIDER_ROW_LIMIT, _filter_catalog_rows, _visible_provider_rows

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


def test_model_catalog_includes_downloadable_ollama_rows(monkeypatch):
    import providers.model_catalog as catalog_view

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

    assert len(rows) == 1
    assert rows[0].provider_id == "ollama"
    assert rows[0].model_id == "qwen3.6:27b"
    assert rows[0].downloadable is True
    assert rows[0].runtime_ready is False
    assert rows[0].status_reason.startswith("Download")


def test_legacy_model_facade_uses_ollama_tool_capability_catalog():
    from models import is_tool_compatible

    assert is_tool_compatible("qwen3.6:27b") is True
    assert is_tool_compatible("qwen3.6:35b-a3b") is True


def test_ollama_embedding_model_is_not_chat_surface():
    info = ollama_model_info("nomic-embed-text:latest", installed=True)

    assert model_supports_surface(info, "chat") is False
    assert model_supports_surface(info, "embeddings") is True


def test_ollama_catalog_rows_merge_installed_and_recommended():
    rows = ollama_catalog_rows(
        ["qwen3:14b", "gemma3:4b", "llava-phi3:3.8b"],
        ["qwen3:14b", "mistral:7b", "mistral:7b", "moondream:latest", "phi4:14b"],
        context_windows={"qwen3:14b": 32768},
    )

    by_id = {row["model_id"]: row for row in rows}
    assert list(by_id) == ["gemma3:4b", "llava-phi3:3.8b", "qwen3:14b", "mistral:7b", "moondream:latest"]
    assert by_id["qwen3:14b"]["installed"] is True
    assert by_id["qwen3:14b"]["context_window"] == 32768
    assert by_id["gemma3:4b"]["installed"] is True
    assert "image" in by_id["gemma3:4b"]["capabilities_snapshot"]["input_modalities"]
    assert "image" in by_id["llava-phi3:3.8b"]["capabilities_snapshot"]["input_modalities"]
    assert by_id["mistral:7b"]["installed"] is False
    assert by_id["mistral:7b"]["capabilities_snapshot"]["tasks"] == ["chat"]
    assert by_id["moondream:latest"]["recommended"] is True
    assert "image" in by_id["moondream:latest"]["capabilities_snapshot"]["input_modalities"]


def test_preferred_ollama_tag_models_filters_variant_noise():
    tags = [
        "qwen3.6:latest",
        "qwen3.6:27b",
        "qwen3.6:35b",
        "qwen3.6:27b-coding-mxfp8",
        "qwen3.6:27b-cloud",
        "qwen3.6:27b-q4_K_M",
        "qwen3.6:35b-a3b",
        "qwen3.6:35b-a3b-q8_0",
        "gemma3:4b",
    ]

    assert preferred_ollama_tag_models(tags) == ["qwen3.6:27b", "qwen3.6:35b", "qwen3.6:35b-a3b", "gemma3:4b"]


def test_ollama_provider_catalog_model_ids_keeps_tool_and_vision_choices():
    ids = ollama_provider_catalog_model_ids(
        installed_models=["qwen3.6:27b", "gemma3:4b"],
        curated_models=["mistral:7b", "llava-phi3:3.8b", "phi4:14b"],
        library_families=["qwen3.6", "granite4.1", "gemma4", "moondream"],
        family_tag_models=["qwen3.6:27b", "qwen3.6:27b-q4_K_M", "qwen3.6:35b"],
    )

    assert ids == ["qwen3.6:27b", "gemma3:4b", "mistral:7b", "llava-phi3:3.8b", "moondream", "qwen3.6:35b"]


def test_extract_ollama_library_model_ids_includes_small_qwen_tags():
    html = '''
        <a href="/library/qwen3:latest">qwen3:latest</a>
        <a href="https://ollama.com/library/qwen3:0.6b">qwen3:0.6b</a>
        <a href="/library/qwen3:1.7b">qwen3:1.7b</a>
        <a href="/library/qwen3:4b-instruct-2507-q4_K_M">qwen3:4b-instruct-2507-q4_K_M</a>
        <a href="/library/qwen3:0.6b">duplicate</a>
    '''

    assert extract_ollama_library_model_ids(html, "qwen3") == [
        "qwen3:latest",
        "qwen3:0.6b",
        "qwen3:1.7b",
        "qwen3:4b-instruct-2507-q4_K_M",
    ]


def test_extract_ollama_library_family_ids_includes_latest_qwen_family():
    html = '''
        <a href="/library/qwen3.6">qwen3.6</a>
        <a href="https://ollama.com/library/granite4.1">granite4.1</a>
        <a href="/library/qwen3.6">duplicate</a>
        <a href="/library/qwen3:0.6b">tag page should not be a family row</a>
    '''

    assert extract_ollama_library_family_ids(html) == ["qwen3.6", "granite4.1"]