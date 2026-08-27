from __future__ import annotations

import sys
import types
from dataclasses import replace

import pytest

from tests.fixtures.providers import FakeChatModel, fake_model_info


pytestmark = pytest.mark.contract


def test_fake_chat_model_contract_is_deterministic() -> None:
    model = FakeChatModel(model="contract-model", responses=["first", "second"], stream_chunks=["a", "b"])

    assert model.invoke("hello").content == "first"
    assert model.invoke([("user", "hello again")]).content == "second"
    assert [chunk.content for chunk in model.stream("stream please")] == ["a", "b"]
    assert model.bind_tools([{"name": "fake_tool"}]) is model
    assert model.bound_tools == [{"name": "fake_tool"}]
    assert len(model.invocations) == 3


def test_model_info_cache_round_trips_capability_snapshot() -> None:
    from row_bot.providers.catalog import model_info_from_legacy, model_info_to_cache_entry

    generation_parameters = {
        "options": {"quality": ["low", "medium"], "resolution": ["1k", "2k"]},
        "defaults": {"quality": "medium", "resolution": "1k"},
        "valid_combinations": [
            {"quality": "low", "resolution": "1k"},
            {"quality": "medium", "resolution": "2k"},
        ],
    }
    info = replace(
        fake_model_info(provider_id="openai", model_id="gpt-4o"),
        generation_parameters=generation_parameters,
    )
    entry = model_info_to_cache_entry(info)
    restored = model_info_from_legacy("model:openai:gpt-4o", entry)

    assert restored is not None
    assert restored.selection_ref == "model:openai:gpt-4o"
    assert restored.context_window == 8192
    assert restored.tool_calling is True
    assert restored.streaming is True
    assert "image" in restored.input_modalities
    assert entry["capabilities_snapshot"]["generation_parameters"] == generation_parameters
    assert restored.generation_parameters == generation_parameters


def test_custom_fake_provider_runtime_uses_fake_transport(tmp_path, monkeypatch) -> None:
    from row_bot.providers import config as provider_config
    from row_bot.providers import custom, runtime
    from row_bot.providers.custom import custom_provider_id

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(custom, "delete_provider_secret", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime, "provider_secret_status", lambda *_args, **_kwargs: {"configured": False, "source": "", "fingerprint": ""})

    provider_id = custom_provider_id("contract-fake")
    custom.save_custom_endpoint(
        {
            "id": "contract-fake",
            "name": "Contract Fake",
            "base_url": "http://127.0.0.1:65535/v1",
            "auth_required": False,
            "models": [
                {
                    "id": "fake-chat",
                    "model_id": "fake-chat",
                    "display_name": "Fake Chat",
                    "context_window": 4096,
                    "capabilities_snapshot": fake_model_info(provider_id=provider_id).capability_snapshot(),
                }
            ],
        }
    )

    fake_transport = types.ModuleType("row_bot.providers.transports.openai_compatible")
    fake_transport.ChatOpenAICompatible = FakeChatModel
    monkeypatch.setitem(sys.modules, "row_bot.providers.transports.openai_compatible", fake_transport)

    status = runtime.provider_status(provider_id)
    chat_model = runtime.create_chat_model("fake-chat", provider_id=provider_id)

    assert status["configured"] is True
    assert status["source"] == "no_auth"
    assert isinstance(chat_model, FakeChatModel)
    assert chat_model.model == "fake-chat"
    assert chat_model.kwargs["base_url"] == "http://127.0.0.1:65535/v1"


def test_provider_error_contract_normalizes_common_failures() -> None:
    from row_bot.providers.errors import ProviderErrorKind, normalize_provider_error

    assert normalize_provider_error(ValueError("401 unauthorized API key")).kind == ProviderErrorKind.AUTHENTICATION
    assert normalize_provider_error(RuntimeError("quota exceeded")).kind == ProviderErrorKind.QUOTA_EXHAUSTED
    assert normalize_provider_error(RuntimeError("tools are not supported")).kind == ProviderErrorKind.UNSUPPORTED_CAPABILITY


def test_every_builtin_provider_has_refresh_ownership_or_an_explicit_exemption() -> None:
    from row_bot.models import REFRESHABLE_CLOUD_PROVIDER_IDS
    from row_bot.providers.catalog import list_provider_definitions

    provider_ids = {definition.id for definition in list_provider_definitions()}
    explicit_exemptions = {"ollama"}

    assert set(REFRESHABLE_CLOUD_PROVIDER_IDS).isdisjoint(explicit_exemptions)
    assert set(REFRESHABLE_CLOUD_PROVIDER_IDS) | explicit_exemptions == provider_ids
