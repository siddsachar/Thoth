from __future__ import annotations

import sys
import types

import pytest

from row_bot.access.config import AccessConfig
from row_bot.access.runtime_policy import RuntimeAccessPolicy
from row_bot.tunnel import TunnelError, TunnelManager, TunnelProvider


class FakeTunnelProvider(TunnelProvider):
    def __init__(self, urls: dict[int, str] | None = None) -> None:
        self.urls = dict(urls or {})
        self.active: dict[int, str] = {}
        self.start_calls: list[tuple[int, str]] = []
        self.stop_calls: list[int] = []
        self.stop_all_calls = 0

    def start(self, port: int, label: str = "") -> str:
        self.start_calls.append((port, label))
        url = self.active.get(port) or self.urls.get(
            port, f"https://managed-{port}.ngrok-free.app"
        )
        self.active[port] = url
        return url

    def stop(self, port: int) -> None:
        self.stop_calls.append(port)
        self.active.pop(port, None)

    def stop_all(self) -> None:
        self.stop_all_calls += 1
        self.active.clear()

    def get_url(self, port: int) -> str | None:
        return self.active.get(port)

    def is_available(self) -> bool:
        return True

    def active_tunnels(self) -> dict[int, str]:
        return dict(self.active)


def _manager(
    provider: FakeTunnelProvider,
) -> tuple[TunnelManager, RuntimeAccessPolicy]:
    policy = RuntimeAccessPolicy(
        AccessConfig.build(deployment_mode="server", allowed_hosts=("localhost",))
    )
    manager = TunnelManager(managed_origin_registrar=policy)
    manager.set_provider(provider)
    return manager, policy


def test_ngrok_authtoken_uses_saved_row_bot_key(monkeypatch):
    import row_bot.api_keys as api_keys
    import row_bot.tunnel as tunnel

    monkeypatch.delenv("NGROK_AUTHTOKEN", raising=False)
    monkeypatch.setattr(api_keys, "get_key", lambda name: " saved-token " if name == "NGROK_AUTHTOKEN" else "")

    assert tunnel._ngrok_authtoken() == "saved-token"


def test_ngrok_authtoken_falls_back_to_environment_when_key_store_fails(monkeypatch):
    import row_bot.api_keys as api_keys
    import row_bot.tunnel as tunnel

    monkeypatch.setenv("NGROK_AUTHTOKEN", "env-token")

    def _raise(_name: str) -> str:
        raise RuntimeError("key store unavailable")

    monkeypatch.setattr(api_keys, "get_key", _raise)

    assert tunnel._ngrok_authtoken() == "env-token"


def test_ngrok_configuration_reports_unreadable_saved_key(monkeypatch):
    import row_bot.api_keys as api_keys
    import row_bot.tunnel as tunnel

    monkeypatch.setitem(sys.modules, "pyngrok", types.SimpleNamespace())
    monkeypatch.delenv("NGROK_AUTHTOKEN", raising=False)
    monkeypatch.setattr(api_keys, "get_key", lambda name: "")
    monkeypatch.setattr(api_keys, "key_status", lambda name: {"configured": True})

    status, detail = tunnel.ngrok_configuration_status()

    assert status == "error"
    assert "keyring secret is unreadable" in detail


def test_managed_tunnel_registers_before_exposure_and_unregisters_on_stop() -> None:
    provider = FakeTunnelProvider()
    manager, policy = _manager(provider)

    origin = manager.start_tunnel(8080, label="main_app")

    assert origin == "https://managed-8080.ngrok-free.app"
    assert manager.get_url(8080) == origin
    assert manager.active_tunnels() == {8080: origin}
    assert policy.snapshot().managed_origins == (origin,)
    assert manager.start_tunnel(8080, label="duplicate") == origin
    assert provider.start_calls == [(8080, "main_app")]

    manager.stop_tunnel(8080)

    assert provider.stop_calls == [8080]
    assert manager.get_url(8080) is None
    assert policy.snapshot().managed_origins == ()


def test_stop_all_unregisters_every_owned_origin_only() -> None:
    provider = FakeTunnelProvider()
    operator_origin = "https://operator.example"
    policy = RuntimeAccessPolicy(
        AccessConfig.build(
            deployment_mode="server",
            allowed_hosts=("localhost", "operator.example"),
            public_origins=(operator_origin,),
        )
    )
    manager = TunnelManager(managed_origin_registrar=policy)
    manager.set_provider(provider)
    first = manager.start_tunnel(8080)
    second = manager.start_tunnel(9090)

    manager.stop_all()

    assert provider.stop_all_calls == 1
    assert manager.active_tunnels() == {}
    assert policy.snapshot().managed_origins == ()
    assert policy.snapshot().base_config.public_origins == (operator_origin,)
    assert first not in policy.snapshot().managed_origins
    assert second not in policy.snapshot().managed_origins


@pytest.mark.parametrize(
    "url",
    [
        "http://managed.ngrok-free.app",
        "https://user@managed.ngrok-free.app",
        "https://managed.ngrok-free.app/path",
        "https://managed.ngrok-free.app?query=1",
        "https://managed.ngrok-free.app#fragment",
        "https://*.ngrok-free.app",
        "malformed",
    ],
)
def test_invalid_provider_url_rolls_back_without_exposure(url: str) -> None:
    provider = FakeTunnelProvider({8080: url})
    manager, policy = _manager(provider)

    with pytest.raises(TunnelError, match="rejected by the access policy"):
        manager.start_tunnel(8080)

    assert provider.stop_calls == [8080]
    assert provider.active == {}
    assert manager.get_url(8080) is None
    assert policy.snapshot().managed_origins == ()


def test_registration_failure_closes_new_tunnel_and_fails_closed() -> None:
    class FailingRegistrar:
        def register_managed_origin(self, _url: object) -> str:
            raise RuntimeError("injected registration failure")

        def unregister_managed_origin(self, _url: object) -> bool:
            return False

    provider = FakeTunnelProvider()
    manager = TunnelManager(managed_origin_registrar=FailingRegistrar())
    manager.set_provider(provider)

    with pytest.raises(TunnelError, match="rejected by the access policy"):
        manager.start_tunnel(8080)

    assert provider.stop_calls == [8080]
    assert manager.get_url(8080) is None


def test_missing_runtime_policy_refuses_to_start_provider() -> None:
    provider = FakeTunnelProvider()
    manager = TunnelManager()
    manager.set_provider(provider)

    with pytest.raises(TunnelError, match="access policy is unavailable"):
        manager.start_tunnel(8080)

    assert provider.start_calls == []
