import pathlib

import api_keys
import providers.config as provider_config
from providers.selection import (
    add_quick_choice_for_model,
    deactivate_quick_choice,
    deactivate_quick_choice_for_error,
    grouped_quick_choices,
    list_model_choice_options,
    list_quick_model_ids,
    list_quick_choices,
    model_choice_options_map,
    model_choice_value,
    migrate_legacy_starred_models,
    provider_display_label,
    remove_quick_choice_for_model,
    resolve_selection,
)


def test_legacy_starred_models_migrate_to_quick_choices(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(api_keys, "get_cloud_config", lambda: {"starred_models": ["gpt-4o", "qwen3:14b"]})

    quick = migrate_legacy_starred_models(cloud_models=["gpt-4o"])

    assert [choice["model_id"] for choice in quick] == ["gpt-4o"]
    assert list_quick_model_ids("chat") == ["gpt-4o"]


def test_quick_choice_add_remove_and_resolution(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(api_keys, "get_cloud_config", lambda: {"starred_models": []})

    add_quick_choice_for_model("claude-sonnet-4-5", display_name="Claude Work")

    resolved = resolve_selection("Claude Work")
    assert resolved is not None
    assert resolved.provider_id == "anthropic"
    assert resolved.model_id == "claude-sonnet-4-5"

    remove_quick_choice_for_model("claude-sonnet-4-5")
    assert list_quick_model_ids("chat") == []


def test_route_refs_resolve_but_are_not_runtime_active(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(api_keys, "get_cloud_config", lambda: {"starred_models": []})

    resolved = resolve_selection("route:balanced")

    assert resolved is not None
    assert resolved.kind == "route"
    assert resolved.active is False


def test_quick_choices_filter_by_capability_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(api_keys, "get_cloud_config", lambda: {"starred_models": []})

    add_quick_choice_for_model(
        "text-embedding-3-large",
        provider_id="openai",
        capabilities_snapshot={
            "tasks": ["embedding"],
            "input_modalities": ["text"],
            "output_modalities": ["text"],
        },
    )

    assert list_quick_choices("chat") == []
    assert [choice["model_id"] for choice in list_quick_choices("embeddings")] == ["text-embedding-3-large"]
    stored = provider_config.load_provider_config()["quick_choices"][0]
    assert stored["inactive_surfaces"]["chat"].startswith("Capability metadata")


def test_surface_inactive_quick_choice_stays_available_for_other_surfaces(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(api_keys, "get_cloud_config", lambda: {"starred_models": []})

    add_quick_choice_for_model("gpt-4o", provider_id="openai", display_name="GPT Work")
    assert deactivate_quick_choice(model_id="gpt-4o", provider_id="openai", surface="chat", reason="Chat probe failed") is True

    assert list_quick_choices("chat") == []
    visible_elsewhere = list_quick_choices("")
    assert [choice["display_name"] for choice in visible_elsewhere] == ["GPT Work"]


def test_globally_inactive_quick_choice_resolves_with_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(api_keys, "get_cloud_config", lambda: {"starred_models": []})

    add_quick_choice_for_model("gpt-4o", provider_id="openai", display_name="GPT Work")
    assert deactivate_quick_choice(model_id="gpt-4o", provider_id="openai", reason="Model removed by provider") is True

    assert list_quick_choices("") == []
    inactive = list_quick_choices("", include_inactive=True)
    assert inactive[0]["active"] is False
    assert inactive[0]["inactive_reason"] == "Model removed by provider"
    resolved = resolve_selection("GPT Work")
    assert resolved is not None
    assert resolved.active is False
    assert resolved.reason == "Model removed by provider"


def test_repinning_quick_choice_reactivates_it(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(api_keys, "get_cloud_config", lambda: {"starred_models": []})

    add_quick_choice_for_model("gpt-4o", provider_id="openai")
    deactivate_quick_choice(model_id="gpt-4o", provider_id="openai", reason="Temporary failure")
    add_quick_choice_for_model("gpt-4o", provider_id="openai")

    assert list_quick_choices("chat")[0]["active"] is True


def test_surface_specific_pin_visibility_keeps_media_out_of_chat(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(api_keys, "get_cloud_config", lambda: {"starred_models": []})

    add_quick_choice_for_model(
        "gpt-image-1",
        provider_id="openai",
        surface="image",
        capabilities_snapshot={
            "tasks": ["image_generation"],
            "input_modalities": ["text"],
            "output_modalities": ["image"],
        },
    )

    assert list_quick_model_ids("chat") == []
    assert list_quick_model_ids("image") == ["gpt-image-1"]
    stored = provider_config.load_provider_config()["quick_choices"][0]
    assert stored["visibility"] == ["image"]


def test_deactivate_quick_choice_for_error_uses_normalized_next_action(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(api_keys, "get_cloud_config", lambda: {"starred_models": []})

    add_quick_choice_for_model("gpt-image-1", provider_id="openai")
    assert deactivate_quick_choice_for_error(
        model_id="gpt-image-1",
        provider_id="openai",
        surface="chat",
        error=ValueError("model is not a chat model"),
    ) is True

    inactive = list_quick_choices("chat", include_inactive=True)
    assert inactive[0]["active"] is False
    assert inactive[0]["inactive_reason"] == "Choose a model whose capability badges match this surface."


def test_quick_choice_remove_supports_custom_provider_id(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(api_keys, "get_cloud_config", lambda: {"starred_models": []})

    provider_id = "custom_openai_dummy"
    add_quick_choice_for_model("thoth-dummy-chat", provider_id=provider_id)
    remove_quick_choice_for_model("thoth-dummy-chat", provider_id=provider_id)

    assert list_quick_choices("") == []


def test_quick_choices_keep_same_model_id_for_different_providers(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(api_keys, "get_cloud_config", lambda: {"starred_models": []})

    add_quick_choice_for_model("shared-model", provider_id="openrouter", display_name="Routed Shared")
    add_quick_choice_for_model("shared-model", provider_id="custom_openai_lab", display_name="Lab Shared")

    choices = list_quick_choices("")

    assert {choice["id"] for choice in choices} == {
        "model:openrouter:shared-model",
        "model:custom_openai_lab:shared-model",
    }
    assert {choice["display_name"] for choice in choices} == {"Routed Shared", "Lab Shared"}


def test_model_choice_options_disambiguate_same_model_id_by_provider(tmp_path, monkeypatch):
    import providers.runtime as provider_runtime

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(api_keys, "get_cloud_config", lambda: {"starred_models": []})
    monkeypatch.setattr(
        provider_runtime,
        "provider_status",
        lambda provider_id: {"runtime_enabled": True} if provider_id == "codex" else {},
    )

    snapshot = {"tasks": ["chat"], "input_modalities": ["text"], "output_modalities": ["text"]}
    add_quick_choice_for_model("gpt-5.5", provider_id="openai", display_name="GPT-5.5", capabilities_snapshot=snapshot)
    add_quick_choice_for_model("gpt-5.5", provider_id="codex", display_name="GPT-5.5", capabilities_snapshot=snapshot)

    options = list_model_choice_options("chat")
    option_map = model_choice_options_map("chat")

    assert [option["value"] for option in options] == [
        "model:openai:gpt-5.5",
        "model:codex:gpt-5.5",
    ]
    assert "OpenAI API" in option_map["model:openai:gpt-5.5"]
    assert "ChatGPT / Codex" in option_map["model:codex:gpt-5.5"]
    assert model_choice_value("gpt-5.5") == "model:openai:gpt-5.5"

    resolved = resolve_selection("model:codex:gpt-5.5")
    assert resolved is not None
    assert resolved.provider_id == "codex"
    assert resolved.model_id == "gpt-5.5"


def test_codex_vision_quick_choice_survives_capability_refresh(tmp_path, monkeypatch):
    import providers.runtime as provider_runtime
    from providers.codex import fallback_codex_model_infos

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(api_keys, "get_cloud_config", lambda: {"starred_models": []})
    monkeypatch.setattr(
        provider_runtime,
        "provider_status",
        lambda provider_id: {"runtime_enabled": True} if provider_id == "codex" else {},
    )

    model_info = next(info for info in fallback_codex_model_infos() if info.model_id == "gpt-5.5")
    add_quick_choice_for_model(
        "gpt-5.5",
        provider_id="codex",
        display_name="GPT-5.5",
        capabilities_snapshot=model_info.capability_snapshot(),
        surface="vision",
    )

    options = list_model_choice_options("vision")
    stored = provider_config.load_provider_config()["quick_choices"][0]

    assert options[0]["value"] == "model:codex:gpt-5.5"
    assert "ChatGPT / Codex" in options[0]["label"]
    assert "image" in stored["capabilities_snapshot"]["input_modalities"]


def test_provider_display_label_uses_dynamic_provider_metadata():
    assert provider_display_label("codex") == "ChatGPT / Codex"
    assert provider_display_label("custom_openai_local_vllm") == "Custom Local Vllm"


def test_grouped_quick_choices_separates_surface_models_and_routes(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(api_keys, "get_cloud_config", lambda: {"starred_models": []})

    add_quick_choice_for_model(
        "gpt-4o",
        provider_id="openai",
        capabilities_snapshot={
            "tasks": ["chat"],
            "input_modalities": ["text", "image"],
            "output_modalities": ["text"],
        },
    )
    add_quick_choice_for_model(
        "gpt-image-1",
        provider_id="openai",
        capabilities_snapshot={
            "tasks": ["image_generation"],
            "input_modalities": ["text"],
            "output_modalities": ["image"],
        },
    )

    groups = {group["id"]: group["choices"] for group in grouped_quick_choices(include_inactive=True, include_media_defaults=False)}

    assert [choice["model_id"] for choice in groups["chat"]] == ["gpt-4o", "gpt-image-1"]
    assert groups["chat"][1]["active"] is False
    assert [choice["model_id"] for choice in groups["vision"]] == ["gpt-4o"]
    assert [choice["model_id"] for choice in groups["image"]] == ["gpt-image-1"]
    assert "routes" not in groups

    route_groups = {group["id"]: group["choices"] for group in grouped_quick_choices(include_inactive=True, include_routes=True, include_media_defaults=False)}
    assert {choice["route_id"] for choice in route_groups["routes"]} >= {"balanced", "private"}
    assert all(choice["active"] is False for choice in route_groups["routes"])

    visible_groups = {group["id"]: group["choices"] for group in grouped_quick_choices(include_inactive=False, include_media_defaults=False)}
    assert [choice["model_id"] for choice in visible_groups["chat"]] == ["gpt-4o"]
    assert [choice["model_id"] for choice in visible_groups["image"]] == ["gpt-image-1"]


def test_grouped_quick_choices_refreshes_stale_capability_snapshots(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(api_keys, "get_cloud_config", lambda: {"starred_models": []})

    add_quick_choice_for_model(
        "qwen3.6:27b",
        provider_id="ollama",
        capabilities_snapshot={
            "tasks": ["chat"],
            "input_modalities": ["text", "image"],
            "output_modalities": ["text"],
        },
    )
    add_quick_choice_for_model("gpt-5.4", provider_id="openai", capabilities_snapshot={"tasks": ["chat"]})

    groups = {group["id"]: group["choices"] for group in grouped_quick_choices(include_inactive=True, include_media_defaults=False)}

    assert [choice["model_id"] for choice in groups["vision"]] == ["gpt-5.4"]
    stored = {choice["model_id"]: choice for choice in provider_config.load_provider_config()["quick_choices"]}
    assert "image" not in stored["qwen3.6:27b"]["capabilities_snapshot"]["input_modalities"]
    assert "image" in stored["gpt-5.4"]["capabilities_snapshot"]["input_modalities"]


def test_models_tab_copy_explains_catalog_pinning_before_picker():
    source = pathlib.Path("ui/settings.py").read_text(encoding="utf-8")

    assert "Pin models in the catalog below before looking for them here." in source
    assert "No pinned Vision choices yet. Pin Vision models in the catalog below." in source