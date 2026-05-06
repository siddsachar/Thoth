import providers.config as provider_config
from providers.capabilities import model_supports_surface
from providers.custom import (
    custom_model_cache_entries,
    custom_provider_id,
    delete_custom_endpoint,
    get_custom_endpoint,
    list_custom_provider_definitions,
    model_infos_from_openai_compatible_catalog,
    refresh_custom_endpoint_models,
    save_custom_endpoint,
)


def test_custom_endpoint_catalog_parses_vllm_models(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    endpoint = {
        "id": "local-vllm",
        "name": "Local vLLM",
        "base_url": "http://127.0.0.1:8000/v1",
        "auth_required": False,
        "execution_location": "local",
    }
    payload = {
        "data": [
            {"id": "meta-llama/Llama-3.1-8B-Instruct", "context_length": 131072},
            {"id": "text-embedding-3-large"},
        ]
    }

    infos = model_infos_from_openai_compatible_catalog(endpoint, payload)

    assert [info.model_id for info in infos] == ["meta-llama/Llama-3.1-8B-Instruct", "text-embedding-3-large"]
    assert infos[0].provider_id == custom_provider_id("local-vllm")
    assert model_supports_surface(infos[0], "chat") is True
    assert model_supports_surface(infos[1], "chat") is False
    assert model_supports_surface(infos[1], "embeddings") is True


def test_custom_endpoint_catalog_infers_lmstudio_vision_models(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    endpoint = {
        "id": "lmstudio",
        "name": "LM Studio",
        "base_url": "http://127.0.0.1:1234/v1",
        "auth_required": False,
        "execution_location": "local",
    }
    payload = {"data": [{"id": "llava-phi3:3.8b"}, {"id": "google/gemma-3-4b-it"}, {"id": "qwen2.5-vl-7b-instruct"}]}

    infos = model_infos_from_openai_compatible_catalog(endpoint, payload)

    assert [info.model_id for info in infos] == ["llava-phi3:3.8b", "google/gemma-3-4b-it", "qwen2.5-vl-7b-instruct"]
    assert all(model_supports_surface(info, "vision") for info in infos)


def test_custom_endpoint_catalog_applies_manual_vision_capabilities(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    endpoint = {
        "id": "manual-vision",
        "name": "Manual Vision",
        "base_url": "http://127.0.0.1:1234/v1",
        "auth_required": False,
        "manual_capabilities": {"vision": True},
    }
    payload = {"data": [{"id": "local-model-with-sparse-metadata"}]}

    infos = model_infos_from_openai_compatible_catalog(endpoint, payload)

    assert model_supports_surface(infos[0], "vision") is True


def test_custom_endpoint_definitions_include_saved_endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")

    save_custom_endpoint({
        "id": "llama-cpp",
        "name": "llama.cpp",
        "base_url": "http://127.0.0.1:8080/v1/",
        "auth_required": False,
        "execution_location": "local",
    })

    definitions = list_custom_provider_definitions()

    assert definitions[0].id == custom_provider_id("llama-cpp")
    assert definitions[0].base_url == "http://127.0.0.1:8080/v1"
    assert definitions[0].risk_label == "local_private"


def test_custom_endpoint_delete_removes_config_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    save_custom_endpoint({"id": "localai", "base_url": "http://127.0.0.1:8080/v1", "auth_required": False})

    delete_custom_endpoint("localai")

    assert get_custom_endpoint("localai") is None


def test_custom_endpoint_refresh_persists_model_cache_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    save_custom_endpoint({"id": "dummy", "base_url": "http://127.0.0.1:8000/v1", "auth_required": False})

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "thoth-dummy-chat", "context_length": 8192}]}

    import httpx
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: _Response())

    infos = refresh_custom_endpoint_models("dummy")
    entries = custom_model_cache_entries()

    assert [info.model_id for info in infos] == ["thoth-dummy-chat"]
    assert entries["thoth-dummy-chat"]["provider"] == custom_provider_id("dummy")
    assert entries["thoth-dummy-chat"]["capabilities_snapshot"]["tasks"] == ["chat"]