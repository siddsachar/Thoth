import sys
from types import SimpleNamespace


def _is_local_model(*installed):
    installed_set = set(installed)
    return lambda model: model in installed_set


def test_thoth_status_normalizes_dynamic_image_model_label(monkeypatch):
    import tools.image_gen_tool as image_gen_tool
    from tools.thoth_status_tool import _normalize_provider_model_value

    monkeypatch.setattr(image_gen_tool, "get_available_image_models", lambda: {
        "openai/gpt-image-2": "⬡  GPT Image 2  (OpenAI)",
        "google/gemini-3-pro-image-preview": "💎  Nano Banana Pro  (Google)",
    })

    assert _normalize_provider_model_value("image_gen_model", "GPT Image 2") == "openai/gpt-image-2"
    assert _normalize_provider_model_value("image_gen_model", "gpt-image-2") == "openai/gpt-image-2"
    assert _normalize_provider_model_value("image_gen_model", "Nano Banana Pro") == "google/gemini-3-pro-image-preview"


def test_thoth_status_normalizes_dynamic_video_model_label(monkeypatch):
    import tools.video_gen_tool as video_gen_tool
    from tools.thoth_status_tool import _normalize_provider_model_value

    monkeypatch.setattr(video_gen_tool, "get_available_video_models", lambda: {
        "google/veo-3.1-generate-preview": "💎  Veo 3.1  (Google)",
        "xai/grok-imagine-video": "𝕏  Grok Imagine Video  (xAI)",
    })

    assert _normalize_provider_model_value("video_gen_model", "Veo 3.1") == "google/veo-3.1-generate-preview"
    assert _normalize_provider_model_value("video_gen_model", "grok-imagine-video") == "xai/grok-imagine-video"


def test_thoth_status_leaves_ambiguous_media_bare_id_unchanged(monkeypatch):
    import tools.image_gen_tool as image_gen_tool
    from tools.thoth_status_tool import _normalize_provider_model_value

    monkeypatch.setattr(image_gen_tool, "get_available_image_models", lambda: {
        "openai/shared-image": "⬡  Shared Image  (OpenAI)",
        "custom_openai_lab/shared-image": "↔  Shared Image  (Lab)",
    })

    assert _normalize_provider_model_value("image_gen_model", "shared-image") == "shared-image"


def test_thoth_status_media_update_seeds_quick_choices(monkeypatch):
    import langgraph.types
    import providers.selection as provider_selection
    import tools.image_gen_tool as image_gen_tool
    import tools.registry as tool_registry
    from tools.thoth_status_tool import _update_setting

    calls = []
    monkeypatch.setattr(langgraph.types, "interrupt", lambda payload: True)
    monkeypatch.setattr(image_gen_tool, "get_available_image_models", lambda: {
        "openai/gpt-image-2": "⬡  GPT Image 2  (OpenAI)",
    })
    monkeypatch.setattr(tool_registry, "set_tool_config", lambda tool, key, value: calls.append((tool, key, value)))
    monkeypatch.setattr(provider_selection, "seed_configured_media_quick_choices", lambda: calls.append(("seed", "media", "quick_choices")))

    result = _update_setting("image_gen_model", "GPT Image 2")

    assert result == "Image generation model set to: openai/gpt-image-2"
    assert ("image_gen", "model", "openai/gpt-image-2") in calls
    assert ("seed", "media", "quick_choices") in calls


def test_thoth_status_rejects_unknown_provider_chat_model(tmp_path, monkeypatch):
    import api_keys
    import models
    import providers.config as provider_config
    from tools.thoth_status_tool import _resolve_model_update_value

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(api_keys, "get_cloud_config", lambda: {"starred_models": []})
    monkeypatch.setattr(models, "is_model_local", _is_local_model())
    monkeypatch.setattr(models, "list_cloud_models", lambda provider=None: [])
    monkeypatch.setattr(models, "list_cloud_vision_models", lambda: [])

    model_value, error = _resolve_model_update_value("gpt-99-fictional", surface="chat")

    assert model_value is None
    assert "not in the current catalog" in str(error)


def test_thoth_status_allows_installed_unknown_local_chat_model(tmp_path, monkeypatch):
    import api_keys
    import models
    import providers.config as provider_config
    from tools.thoth_status_tool import _resolve_model_update_value

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(api_keys, "get_cloud_config", lambda: {"starred_models": []})
    monkeypatch.setattr(models, "is_model_local", _is_local_model("gemma4:e4b"))
    monkeypatch.setattr(models, "list_cloud_models", lambda provider=None: [])
    monkeypatch.setattr(models, "list_cloud_vision_models", lambda: [])

    model_value, error = _resolve_model_update_value("gemma4:e4b", surface="chat")

    assert error is None
    assert model_value == "gemma4:e4b"


def test_thoth_status_rejects_local_model_without_vision_metadata(tmp_path, monkeypatch):
    import api_keys
    import models
    import providers.config as provider_config
    from tools.thoth_status_tool import _resolve_model_update_value

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(api_keys, "get_cloud_config", lambda: {"starred_models": []})
    monkeypatch.setattr(models, "is_model_local", _is_local_model("gemma4:e4b"))
    monkeypatch.setattr(models, "list_cloud_models", lambda provider=None: [])
    monkeypatch.setattr(models, "list_cloud_vision_models", lambda: [])

    model_value, error = _resolve_model_update_value("gemma4:e4b", surface="vision")

    assert model_value is None
    assert "does not have Vision capability metadata" in str(error)


def test_thoth_status_vision_model_update_persists_valid_model(tmp_path, monkeypatch):
    import api_keys
    import langgraph.types
    import models
    import providers.config as provider_config
    import tools.vision_tool as vision_tool
    from tools.thoth_status_tool import _update_setting

    calls = []
    vision_service = SimpleNamespace(model="moondream:latest")
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(api_keys, "get_cloud_config", lambda: {"starred_models": []})
    monkeypatch.setattr(langgraph.types, "interrupt", lambda payload: True)
    monkeypatch.setattr(models, "is_model_local", _is_local_model("gemma3:4b"))
    monkeypatch.setattr(models, "list_cloud_models", lambda provider=None: [])
    monkeypatch.setattr(models, "list_cloud_vision_models", lambda: [])
    monkeypatch.setattr(vision_tool, "_get_vision_service", lambda: vision_service)
    monkeypatch.setitem(sys.modules, "agent", SimpleNamespace(clear_agent_cache=lambda: calls.append("clear")))

    result = _update_setting("vision_model", "gemma3:4b")

    assert result == "Vision model changed to: gemma3:4b"
    assert vision_service.model == "gemma3:4b"
    assert calls == ["clear"]


def test_thoth_status_allows_codex_vision_quick_choice(tmp_path, monkeypatch):
    import api_keys
    import models
    import providers.config as provider_config
    import providers.runtime as provider_runtime
    from providers.codex import fallback_codex_model_infos
    from providers.selection import add_quick_choice_for_model
    from tools.thoth_status_tool import _resolve_model_update_value

    model_info = next(info for info in fallback_codex_model_infos() if info.model_id == "gpt-5.5")
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(api_keys, "get_cloud_config", lambda: {"starred_models": []})
    monkeypatch.setattr(models, "is_model_local", _is_local_model())
    monkeypatch.setattr(provider_runtime, "provider_status", lambda provider_id: {"runtime_enabled": True})
    add_quick_choice_for_model(
        "gpt-5.5",
        provider_id="codex",
        display_name="GPT-5.5",
        capabilities_snapshot=model_info.capability_snapshot(),
        surface="vision",
    )

    model_value, error = _resolve_model_update_value("model:codex:gpt-5.5", surface="vision")

    assert error is None
    assert model_value == "gpt-5.5"
