from types import SimpleNamespace


def test_openai_fetch_keeps_new_image_generation_model(monkeypatch):
    import models
    import api_keys

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


def test_dynamic_provider_image_model_appears_in_image_options(monkeypatch):
    import api_keys
    import models
    from providers.catalog import model_info_from_metadata, model_info_to_cache_entry
    from tools.image_gen_tool import get_available_image_models

    monkeypatch.setattr(api_keys, "get_key", lambda key: "sk-test" if key == "OPENAI_API_KEY" else "")
    info = model_info_from_metadata("openai", "gpt-image-2", display_name="GPT Image 2")
    models._cloud_model_cache.clear()
    models._cloud_model_cache["gpt-image-2"] = model_info_to_cache_entry(info)

    options = get_available_image_models()

    assert "openai/gpt-image-2" in options
    assert "GPT Image 2" in options["openai/gpt-image-2"]
    assert options["openai/gpt-image-2"] == "⬡  GPT Image 2  (OpenAI)"


def test_dynamic_provider_video_model_appears_in_video_options(monkeypatch):
    import api_keys
    import models
    from providers.catalog import model_info_from_metadata, model_info_to_cache_entry
    from tools.video_gen_tool import get_available_video_models

    monkeypatch.setattr(api_keys, "get_key", lambda key: "test" if key == "GOOGLE_API_KEY" else "")
    info = model_info_from_metadata("google", "veo-4.0-generate-preview", display_name="Veo 4")
    models._cloud_model_cache.clear()
    models._cloud_model_cache["veo-4.0-generate-preview"] = model_info_to_cache_entry(info)

    options = get_available_video_models()

    assert "google/veo-4.0-generate-preview" in options
    assert "Veo 4" in options["google/veo-4.0-generate-preview"]
    assert options["google/veo-4.0-generate-preview"] == "💎  Veo 4  (Google)"


def test_google_nano_banana_catalog_entry_appears_in_image_options(monkeypatch):
    import api_keys
    import models
    from providers.catalog import model_info_from_metadata, model_info_to_cache_entry
    from tools.image_gen_tool import get_available_image_models

    monkeypatch.setattr(api_keys, "get_key", lambda key: "test" if key == "GOOGLE_API_KEY" else "")
    info = model_info_from_metadata("google", "gemini-3.1-flash-image-preview", display_name="Nano Banana 2")
    models._cloud_model_cache.clear()
    models._cloud_model_cache["gemini-3.1-flash-image-preview"] = model_info_to_cache_entry(info)

    options = get_available_image_models()

    assert "google/gemini-3.1-flash-image-preview" in options
    assert options["google/gemini-3.1-flash-image-preview"] == "💎  Nano Banana 2  (Google)"


def test_xai_imagine_catalog_entry_appears_in_image_options(monkeypatch):
    import api_keys
    import models
    from providers.catalog import model_info_from_metadata, model_info_to_cache_entry
    from tools.image_gen_tool import get_available_image_models

    monkeypatch.setattr(api_keys, "get_key", lambda key: "test" if key == "XAI_API_KEY" else "")
    info = model_info_from_metadata("xai", "grok-imagine-image", display_name="Grok Imagine")
    models._cloud_model_cache.clear()
    models._cloud_model_cache["grok-imagine-image"] = model_info_to_cache_entry(info)

    options = get_available_image_models()

    assert "xai/grok-imagine-image" in options
    assert options["xai/grok-imagine-image"] == "𝕏  Grok Imagine  (xAI)"


def test_grouped_quick_choices_seed_current_media_tool_defaults(tmp_path, monkeypatch):
    import api_keys
    import models
    import providers.config as provider_config
    from providers.catalog import model_info_from_metadata, model_info_to_cache_entry
    from providers.selection import grouped_quick_choices
    from tools import registry

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
