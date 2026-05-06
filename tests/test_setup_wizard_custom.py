from __future__ import annotations

from providers.models import ModelInfo, TransportMode
from ui.setup_wizard import build_custom_endpoint_setup_payload, custom_endpoint_model_options


def test_custom_endpoint_setup_payload_uses_api_key_auth() -> None:
    payload = build_custom_endpoint_setup_payload("http://127.0.0.1:8000/v1/", " sk-local ")

    assert payload["id"] == "127.0.0.1:8000"
    assert payload["name"] == "Self-hosted (127.0.0.1:8000)"
    assert payload["base_url"] == "http://127.0.0.1:8000/v1"
    assert payload["api_key"] == "sk-local"
    assert payload["auth_required"] is True
    assert payload["execution_location"] == "local"
    assert payload["transport"] == "openai_chat"


def test_custom_endpoint_setup_payload_treats_empty_key_as_no_auth() -> None:
    payload = build_custom_endpoint_setup_payload("https://models.example.com/v1", "")

    assert payload["id"] == "models.example.com"
    assert payload["api_key"] == ""
    assert payload["auth_required"] is False
    assert payload["execution_location"] == "remote"


def test_custom_endpoint_model_options_use_provider_model_refs() -> None:
    info = ModelInfo(
        provider_id="custom_openai_localai",
        model_id="thoth-dummy-chat",
        display_name="Thoth Dummy Chat",
        context_window=4096,
        transport=TransportMode.OPENAI_CHAT,
    )

    assert custom_endpoint_model_options([info]) == {
        "model:custom_openai_localai:thoth-dummy-chat": "↔ Thoth Dummy Chat"
    }
