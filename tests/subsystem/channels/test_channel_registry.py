from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from tests.fixtures.channels import FakeChannel


pytestmark = pytest.mark.subsystem


@pytest.fixture(autouse=True)
def reset_channel_registry():
    from row_bot.channels import registry

    registry._reset()
    yield
    registry._reset()


def test_registry_delivers_only_to_running_channels() -> None:
    from row_bot.channels import registry

    channel = FakeChannel(display_name="Registry Fake")
    registry.register(channel)

    assert registry.deliver("fake", "target", "before start")[0] == "delivery_failed"

    asyncio.run(channel.start())
    status, detail = registry.deliver("fake", "target", "after start")

    assert status == "delivered"
    assert "Registry Fake" in detail
    assert channel.messages[-1].text == "after start"
    assert registry.running_channels() == [channel]
    assert registry.configured_channels() == [channel]


def test_registry_tracks_channel_sources_and_unregisters_plugin_channels() -> None:
    from row_bot.channels import registry

    core = FakeChannel(name="fake")
    plugin = FakeChannel(name="matrix", display_name="Matrix Plugin")

    registry.register(core)
    registry.register(
        plugin,
        source=registry.ChannelSource(kind="plugin", plugin_id="matrix-plugin", label="Matrix"),
    )

    assert registry.get_source("fake") == registry.ChannelSource()
    assert registry.get_source("matrix").kind == "plugin"
    assert registry.get_source("matrix").plugin_id == "matrix-plugin"
    assert registry.allows_custom_ui("fake") is True
    assert registry.allows_custom_ui("matrix") is False

    with pytest.raises(ValueError, match="already registered"):
        registry.register(FakeChannel(name="matrix"))

    registry.unregister_plugin_channels("matrix-plugin")

    assert registry.get("matrix") is None
    assert registry.get("fake") is core


def test_register_and_unregister_invalidate_only_an_already_loaded_agent(monkeypatch) -> None:
    from row_bot.channels import registry

    calls: list[str] = []
    monkeypatch.setattr(
        registry,
        "sys",
        SimpleNamespace(modules={"row_bot.agent": SimpleNamespace(clear_agent_cache=lambda: calls.append("clear"))}),
    )

    registry.register(FakeChannel(name="cached"))
    registry.unregister("cached")
    registry.unregister("missing")

    assert calls == ["clear", "clear"]

    monkeypatch.setattr(registry, "sys", SimpleNamespace(modules={}))
    registry.register(FakeChannel(name="no-agent"))
    assert calls == ["clear", "clear"]


def test_generated_channel_send_tools_are_destructive_names() -> None:
    from row_bot.channels.tool_factory import channel_tool_names, destructive_channel_tool_names

    channel = FakeChannel(name="fake")

    assert channel_tool_names(channel) == [
        "send_fake_message",
        "send_fake_photo",
        "send_fake_document",
    ]
    assert destructive_channel_tool_names(channel) == {
        "send_fake_message",
        "send_fake_photo",
        "send_fake_document",
    }


def test_registry_validation_reports_unknown_or_incomplete_delivery() -> None:
    from row_bot.channels import registry

    with pytest.raises(ValueError, match="delivery_target"):
        registry.validate_delivery(None, "target")

    with pytest.raises(ValueError, match="delivery_target is empty"):
        registry.validate_delivery("fake", None)

    with pytest.raises(ValueError, match="Unknown delivery channel"):
        registry.validate_delivery("missing", "target")


def test_channel_auth_store_uses_channel_namespace(monkeypatch) -> None:
    from row_bot.channels import auth_store

    secrets: dict[tuple[str | None, str], str] = {}

    monkeypatch.setattr(auth_store.api_keys, "get_key", lambda _name: "")
    monkeypatch.setattr(auth_store.api_keys, "key_status", lambda _name: {"configured": False})
    monkeypatch.setattr(auth_store.secret_store, "fingerprint", lambda value: f"fp:{value[-4:]}")
    monkeypatch.setattr(auth_store.secret_store, "get_secret", lambda name, namespace=None: secrets.get((namespace, name), ""))
    monkeypatch.setattr(auth_store.secret_store, "set_secret", lambda name, value, namespace=None: secrets.__setitem__((namespace, name), value))
    monkeypatch.setattr(auth_store.secret_store, "delete_secret", lambda name, namespace=None: secrets.pop((namespace, name), None))
    monkeypatch.delenv("FAKE_TOKEN", raising=False)

    auth_store.set_channel_secret("fake", "FAKE_TOKEN", "secret-value")

    assert secrets[("channels:fake", "FAKE_TOKEN")] == "secret-value"
    assert auth_store.get_channel_secret("fake", "FAKE_TOKEN") == "secret-value"
    assert auth_store.channel_secret_status("fake", "FAKE_TOKEN") == {
        "configured": True,
        "source": "channel keyring",
        "fingerprint": "fp:alue",
    }

    auth_store.delete_channel_secret("fake", "FAKE_TOKEN")
    assert auth_store.get_channel_secret("fake", "FAKE_TOKEN") == ""
