from types import SimpleNamespace


def test_openai_fetch_keeps_new_image_generation_model(monkeypatch):
    import row_bot.models as models
    import row_bot.api_keys as api_keys

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": [
                    {"id": "gpt-image-2"},
                    {"id": "text-embedding-3-large"},
                ]
            }

    fake_httpx = SimpleNamespace(get=lambda *args, **kwargs: _Response())
    monkeypatch.setitem(__import__("sys").modules, "httpx", fake_httpx)
    monkeypatch.setattr(api_keys, "get_key", lambda key: "sk-test" if key == "OPENAI_API_KEY" else "")
    models._cloud_model_cache.clear()

    count = models.fetch_cloud_models("openai")

    assert count == 1
    assert "gpt-image-2" in models._cloud_model_cache
    snapshot = models._cloud_model_cache["gpt-image-2"]["capabilities_snapshot"]
    assert snapshot["tasks"] == ["image_generation"]
    assert snapshot["output_modalities"] == ["image"]
    assert "text-embedding-3-large" not in models._cloud_model_cache


def test_openai_fetch_accepts_new_gpt5_family_models_without_an_allowlist(monkeypatch):
    import row_bot.api_keys as api_keys
    import row_bot.models as models

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": [
                    {"id": "gpt-5.6-sol"},
                    {"id": "gpt-5.6-terra"},
                    {"id": "gpt-5.6-luna"},
                ]
            }

    fake_httpx = SimpleNamespace(get=lambda *args, **kwargs: _Response())
    monkeypatch.setitem(__import__("sys").modules, "httpx", fake_httpx)
    monkeypatch.setattr(api_keys, "get_key", lambda key: "sk-test" if key == "OPENAI_API_KEY" else "")
    models._cloud_model_cache.clear()

    count = models.fetch_cloud_models("openai")

    assert count == 3
    assert {model_id for model_id in models._cloud_model_cache if model_id.startswith("gpt-5.6-")} == {
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
    }
    assert all(
        models._cloud_model_cache[model_id]["transport"] == "openai_responses"
        for model_id in ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna")
    )


def test_dynamic_provider_image_model_appears_in_image_options(monkeypatch):
    import row_bot.api_keys as api_keys
    import row_bot.models as models
    from row_bot.providers.catalog import model_info_from_metadata, model_info_to_cache_entry
    from row_bot.tools.image_gen_tool import get_available_image_models

    monkeypatch.setattr(api_keys, "get_key", lambda key: "sk-test" if key == "OPENAI_API_KEY" else "")
    info = model_info_from_metadata("openai", "gpt-image-2", display_name="GPT Image 2")
    models._cloud_model_cache.clear()
    models._cloud_model_cache["gpt-image-2"] = model_info_to_cache_entry(info)

    options = get_available_image_models()

    assert "openai/gpt-image-2" in options
    assert "GPT Image 2" in options["openai/gpt-image-2"]
    assert options["openai/gpt-image-2"] == "⬡  GPT Image 2  (OpenAI)"


def test_dynamic_provider_video_model_appears_in_video_options(monkeypatch):
    import row_bot.api_keys as api_keys
    import row_bot.models as models
    from row_bot.providers.catalog import model_info_from_metadata, model_info_to_cache_entry
    from row_bot.tools.video_gen_tool import get_available_video_models

    monkeypatch.setattr(api_keys, "get_key", lambda key: "test" if key == "GOOGLE_API_KEY" else "")
    info = model_info_from_metadata("google", "veo-4.0-generate-preview", display_name="Veo 4")
    models._cloud_model_cache.clear()
    models._cloud_model_cache["veo-4.0-generate-preview"] = model_info_to_cache_entry(info)

    options = get_available_video_models()

    assert "google/veo-4.0-generate-preview" in options
    assert "Veo 4" in options["google/veo-4.0-generate-preview"]
    assert options["google/veo-4.0-generate-preview"] == "💎  Veo 4  (Google)"


def test_google_nano_banana_catalog_entry_appears_in_image_options(monkeypatch):
    import row_bot.api_keys as api_keys
    import row_bot.models as models
    from row_bot.providers.catalog import model_info_from_metadata, model_info_to_cache_entry
    from row_bot.tools.image_gen_tool import get_available_image_models

    monkeypatch.setattr(api_keys, "get_key", lambda key: "test" if key == "GOOGLE_API_KEY" else "")
    info = model_info_from_metadata("google", "gemini-3.1-flash-image-preview", display_name="Nano Banana 2")
    models._cloud_model_cache.clear()
    models._cloud_model_cache["gemini-3.1-flash-image-preview"] = model_info_to_cache_entry(info)

    options = get_available_image_models()

    assert "google/gemini-3.1-flash-image-preview" in options
    assert options["google/gemini-3.1-flash-image-preview"] == "💎  Nano Banana 2  (Google)"


def test_xai_imagine_catalog_entry_appears_in_image_options(monkeypatch):
    import row_bot.api_keys as api_keys
    import row_bot.models as models
    from row_bot.providers.catalog import model_info_from_metadata, model_info_to_cache_entry
    from row_bot.tools.image_gen_tool import get_available_image_models

    monkeypatch.setattr(api_keys, "get_key", lambda key: "test" if key == "XAI_API_KEY" else "")
    info = model_info_from_metadata("xai", "grok-imagine-image", display_name="Grok Imagine")
    models._cloud_model_cache.clear()
    models._cloud_model_cache["grok-imagine-image"] = model_info_to_cache_entry(info)

    options = get_available_image_models()

    assert "xai/grok-imagine-image" in options
    assert "xai/grok-imagine-image-quality" in options
    assert "Grok Imagine Quality" in options["xai/grok-imagine-image-quality"]
    assert options["xai/grok-imagine-image"] == "𝕏  Grok Imagine  (xAI)"


def test_xai_oauth_media_options_require_oauth_runtime(monkeypatch):
    import row_bot.api_keys as api_keys
    import row_bot.models as models
    import row_bot.providers.xai_oauth as xai_oauth
    from row_bot.tools.image_gen_tool import get_available_image_models
    from row_bot.tools.video_gen_tool import get_available_video_models

    monkeypatch.setattr(api_keys, "get_key", lambda key: "")
    monkeypatch.setattr(xai_oauth, "xai_oauth_runtime_available", lambda *, refresh_if_needed=False: False)
    models._cloud_model_cache.clear()

    assert "xai_oauth/grok-imagine-image" not in get_available_image_models()
    assert "xai_oauth/grok-imagine-video" not in get_available_video_models()

    monkeypatch.setattr(xai_oauth, "xai_oauth_runtime_available", lambda *, refresh_if_needed=False: True)

    image_options = get_available_image_models()
    video_options = get_available_video_models()

    assert "xai_oauth/grok-imagine-image" in image_options
    assert "xai_oauth/grok-imagine-image-quality" in image_options
    assert image_options["xai_oauth/grok-imagine-image"] == "X  Grok Imagine  (xAI Grok)"
    assert "xai_oauth/grok-imagine-video" in video_options
    assert video_options["xai_oauth/grok-imagine-video"] == "X  Grok Imagine Video  (xAI Grok)"
    assert "xai/grok-imagine-image" not in image_options
    assert "xai/grok-imagine-video" not in video_options


def test_xai_api_key_media_options_stay_separate_from_oauth(monkeypatch):
    import row_bot.api_keys as api_keys
    import row_bot.models as models
    import row_bot.providers.xai_oauth as xai_oauth
    from row_bot.tools.image_gen_tool import get_available_image_models

    monkeypatch.setattr(api_keys, "get_key", lambda key: "xai-api-key" if key == "XAI_API_KEY" else "")
    monkeypatch.setattr(xai_oauth, "xai_oauth_runtime_available", lambda *, refresh_if_needed=False: False)
    models._cloud_model_cache.clear()

    options = get_available_image_models()

    assert "xai/grok-imagine-image" in options
    assert "xai_oauth/grok-imagine-image" not in options


def test_curated_media_catalog_does_not_overwrite_live_media_row():
    from row_bot.providers.catalog import model_info_from_metadata, model_info_to_cache_entry
    from row_bot.providers.model_catalog import build_model_catalog_rows

    info = model_info_from_metadata(
        "openai",
        "gpt-image-1",
        display_name="GPT Image 1 Live",
        context_window=256000,
        source="provider_catalog",
    )

    rows = build_model_catalog_rows(
        cloud_cache={"gpt-image-1": model_info_to_cache_entry(info)},
        quick_choices=[],
    )
    row = next(row for row in rows if row.selection_ref == "model:openai:gpt-image-1")

    assert row.context_window == 256000
    assert row.source == "provider_catalog"
    assert row.supports("image")


def test_generation_parameters_reach_catalog_rows_and_media_quick_choices(tmp_path, monkeypatch):
    import row_bot.api_keys as api_keys
    import row_bot.models as models
    import row_bot.providers.config as provider_config
    from row_bot.providers.catalog import model_info_from_metadata, model_info_to_cache_entry
    from row_bot.providers.model_catalog import build_model_catalog_rows
    from row_bot.providers.selection import seed_configured_media_quick_choices
    from row_bot.tools import registry

    generation_parameters = {
        "options": {"quality": ["low", "medium"], "resolution": ["1k", "2k"]},
        "defaults": {"quality": "medium", "resolution": "1k"},
        "valid_combinations": [
            {"quality": "low", "resolution": "1k"},
            {"quality": "medium", "resolution": "1k"},
            {"quality": "medium", "resolution": "2k"},
        ],
    }
    info = model_info_from_metadata(
        "xai",
        "future-renderer",
        {"generation_parameters": generation_parameters},
        display_name="Future Renderer",
    )
    cache_entry = model_info_to_cache_entry(info)
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(api_keys, "get_cloud_config", lambda: {"starred_models": []})
    monkeypatch.setattr(api_keys, "get_key", lambda key: "test" if key == "XAI_API_KEY" else "")
    monkeypatch.setitem(models._cloud_model_cache, "model:xai:future-renderer", cache_entry)
    registry.set_tool_config("image_gen", "model", "xai/future-renderer")

    rows = build_model_catalog_rows(
        cloud_cache={"model:xai:future-renderer": cache_entry},
        quick_choices=[],
    )
    quick = seed_configured_media_quick_choices()

    row = next(row for row in rows if row.selection_ref == "model:xai:future-renderer")
    choice = next(choice for choice in quick if choice["id"] == "model:xai:future-renderer")
    assert row.capabilities_snapshot["generation_parameters"] == generation_parameters
    assert choice["capabilities_snapshot"]["generation_parameters"] == generation_parameters


def test_grouped_quick_choices_seed_current_media_tool_defaults(tmp_path, monkeypatch):
    import row_bot.api_keys as api_keys
    import row_bot.models as models
    import row_bot.providers.config as provider_config
    from row_bot.providers.catalog import model_info_from_metadata, model_info_to_cache_entry
    from row_bot.providers.selection import grouped_quick_choices
    from row_bot.tools import registry

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(api_keys, "get_cloud_config", lambda: {"starred_models": []})
    monkeypatch.setattr(api_keys, "get_key", lambda key: "test")
    models._cloud_model_cache.clear()
    image_info = model_info_from_metadata("openai", "gpt-image-2", display_name="GPT Image 2")
    video_info = model_info_from_metadata("google", "veo-4.0-generate-preview", display_name="Veo 4")
    models._cloud_model_cache["gpt-image-2"] = model_info_to_cache_entry(image_info)
    models._cloud_model_cache["veo-4.0-generate-preview"] = model_info_to_cache_entry(video_info)
    registry.set_tool_config("image_gen", "model", "openai/gpt-image-2")
    registry.set_tool_config("video_gen", "model", "google/veo-4.0-generate-preview")

    groups = {group["id"]: group["choices"] for group in grouped_quick_choices(include_inactive=False)}

    assert [choice["model_id"] for choice in groups["image"]] == ["gpt-image-2"]
    assert [choice["model_id"] for choice in groups["video"]] == ["veo-4.0-generate-preview"]
    assert groups["chat"] == []


def test_seed_configured_media_quick_choices_supports_xai_oauth(tmp_path, monkeypatch):
    import row_bot.api_keys as api_keys
    import row_bot.providers.config as provider_config
    import row_bot.providers.xai_oauth as xai_oauth
    from row_bot.providers.selection import seed_configured_media_quick_choices
    from row_bot.tools import registry

    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(api_keys, "get_cloud_config", lambda: {"starred_models": []})
    monkeypatch.setattr(xai_oauth, "xai_oauth_runtime_available", lambda *, refresh_if_needed=False: True)
    registry.set_tool_config("image_gen", "model", "xai_oauth/grok-imagine-image")
    registry.set_tool_config("video_gen", "model", "xai_oauth/grok-imagine-video")

    quick = seed_configured_media_quick_choices()
    by_id = {choice["id"]: choice for choice in quick}

    assert by_id["model:xai_oauth:grok-imagine-image"]["provider_id"] == "xai_oauth"
    assert by_id["model:xai_oauth:grok-imagine-image"]["visibility"] == ["image"]
    assert by_id["model:xai_oauth:grok-imagine-video"]["provider_id"] == "xai_oauth"
    assert by_id["model:xai_oauth:grok-imagine-video"]["visibility"] == ["video"]
