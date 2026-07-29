from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import pytest

from row_bot.access.config import AccessConfig, AccessConfigError
from row_bot.access.request_context import RequestContextError, RequestContextResolver
from row_bot.access.runtime_policy import (
    RuntimeAccessPolicy,
    canonical_managed_https_origin,
)

MANAGED_ORIGIN = "https://managed-example.ngrok-free.app"
OPERATOR_ORIGIN = "https://operator.example"


def _scope(
    *,
    client: str,
    host: str = "managed-example.ngrok-free.app",
) -> dict:
    return {
        "type": "http",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "query_string": b"",
        "headers": [
            (b"host", host.encode("ascii")),
            (b"x-forwarded-for", b"198.51.100.25"),
            (b"x-forwarded-host", host.encode("ascii")),
            (b"x-forwarded-proto", b"https"),
        ],
        "client": (client, 51000),
    }


def _base_config() -> AccessConfig:
    return AccessConfig.build(
        deployment_mode="server",
        trusted_proxy_cidrs=("10.0.0.2/32",),
        allowed_hosts=("localhost", "operator.example"),
        public_origins=(OPERATOR_ORIGIN,),
    )


def test_register_and_unregister_exact_managed_origin_preserves_base() -> None:
    base = _base_config()
    policy = RuntimeAccessPolicy(base)

    origin = policy.register_managed_origin(
        "HTTPS://Managed-Example.NGROK-Free.App:443/"
    )
    registered = policy.snapshot()

    assert origin == MANAGED_ORIGIN
    assert registered.managed_origins == (MANAGED_ORIGIN,)
    managed_config = registered.config_for_scope(_scope(client="127.0.0.1"))
    assert managed_config.host_allowed("managed-example.ngrok-free.app")
    assert managed_config.origin_allowed(MANAGED_ORIGIN)
    assert managed_config.origin_allowed(OPERATOR_ORIGIN) is False
    assert registered.base_config.origin_allowed(OPERATOR_ORIGIN)
    assert base.allowed_hosts == ("localhost", "operator.example")
    assert base.public_origins == (OPERATOR_ORIGIN,)
    assert {str(network) for network in managed_config.trusted_proxy_cidrs} == {
        "127.0.0.1/32",
        "::1/128",
    }

    assert policy.register_managed_origin(MANAGED_ORIGIN) == MANAGED_ORIGIN
    assert policy.snapshot().managed_origins == (MANAGED_ORIGIN,)
    assert policy.unregister_managed_origin(MANAGED_ORIGIN) is True
    assert policy.unregister_managed_origin(MANAGED_ORIGIN) is False
    assert policy.snapshot().base_config is base


@pytest.mark.parametrize(
    "value",
    [
        "http://managed-example.ngrok-free.app",
        "https://user@managed-example.ngrok-free.app",
        "https://managed-example.ngrok-free.app/path",
        "https://managed-example.ngrok-free.app?query=1",
        "https://managed-example.ngrok-free.app#fragment",
        "https://*.ngrok-free.app",
        "https://",
        "not a URL",
        "",
    ],
)
def test_managed_origin_rejects_non_https_or_non_origin_urls(value: str) -> None:
    with pytest.raises(AccessConfigError):
        canonical_managed_https_origin(value)


@pytest.mark.parametrize("client", ["127.0.0.1", "::1", "::ffff:127.0.0.1"])
def test_managed_proxy_forwarding_is_trusted_only_from_loopback(client: str) -> None:
    policy = RuntimeAccessPolicy(_base_config())
    policy.register_managed_origin(MANAGED_ORIGIN)

    scope = _scope(client=client)
    provenance = RequestContextResolver(
        policy.snapshot().config_for_scope(scope)
    ).resolve_provenance(scope)

    assert provenance.trusted_proxy is True
    assert provenance.origin == MANAGED_ORIGIN
    assert provenance.effective_client == "198.51.100.25"


@pytest.mark.parametrize(
    "client",
    ["192.168.1.20", "172.17.0.1", "10.0.0.2", "100.64.0.8"],
)
def test_managed_proxy_forwarding_rejects_other_peer_ranges(client: str) -> None:
    policy = RuntimeAccessPolicy(_base_config())
    policy.register_managed_origin(MANAGED_ORIGIN)

    with pytest.raises(RequestContextError, match="untrusted_forwarding_headers"):
        scope = _scope(client=client)
        RequestContextResolver(
            policy.snapshot().config_for_scope(scope)
        ).resolve_provenance(scope)


def test_managed_registration_does_not_expand_operator_managed_config() -> None:
    base = _base_config()
    policy = RuntimeAccessPolicy(base)
    policy.register_managed_origin(MANAGED_ORIGIN)
    snapshot = policy.snapshot()
    managed_config = snapshot.config_for_scope(_scope(client="127.0.0.1"))
    operator_scope = _scope(
        client="127.0.0.1",
        host="operator.example",
    )
    operator_config = snapshot.config_for_scope(operator_scope)

    assert managed_config.host_allowed("unknown.ngrok-free.app") is False
    assert managed_config.origin_allowed("https://unknown.ngrok-free.app") is False
    assert managed_config.host_allowed("operator.example") is False
    assert operator_config is base
    with pytest.raises(RequestContextError, match="untrusted_forwarding_headers"):
        RequestContextResolver(operator_config).resolve_provenance(operator_scope)

    policy.clear_managed_origins()

    assert policy.snapshot().base_config is base


def test_concurrent_registration_and_snapshot_views_remain_coherent() -> None:
    policy = RuntimeAccessPolicy(_base_config())

    def exercise(index: int) -> None:
        origin = f"https://managed-{index}.ngrok-free.app"
        policy.register_managed_origin(origin)
        snapshot = policy.snapshot()
        assert snapshot.managed_origins
        for active_origin in snapshot.managed_origins:
            authority = urlsplit(active_origin).netloc
            scope = _scope(client="127.0.0.1", host=authority)
            config = snapshot.config_for_scope(scope)
            loopbacks = {
                str(network) for network in config.trusted_proxy_cidrs
            }
            assert loopbacks == {"127.0.0.1/32", "::1/128"}
            assert config.host_allowed(authority)
            assert config.origin_allowed(active_origin)
        policy.unregister_managed_origin(origin)
        final = policy.snapshot()
        if not final.managed_origins:
            assert final.base_config is policy.base_config

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(exercise, range(128)))
