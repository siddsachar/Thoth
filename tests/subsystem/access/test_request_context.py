from __future__ import annotations

import pytest

from row_bot.access.config import (
    AccessConfig,
    AccessConfigError,
    DeploymentMode,
    UntrustedForwardedAction,
    canonical_origin,
)
from row_bot.access.request_context import (
    AuthenticationKind,
    PresentationMode,
    RequestContextError,
    RequestContextResolver,
    SessionIdentity,
    request_origin_matches,
    safe_relative_next,
)


def _scope(
    *,
    client: str = "127.0.0.1",
    host: str = "localhost:8080",
    scheme: str = "http",
    headers: list[tuple[bytes, bytes]] | None = None,
    query: bytes = b"",
    scope_type: str = "http",
) -> dict:
    all_headers = [(b"host", host.encode("ascii"))]
    all_headers.extend(headers or [])
    return {
        "type": scope_type,
        "method": "GET",
        "scheme": scheme,
        "path": "/",
        "query_string": query,
        "headers": all_headers,
        "client": (client, 51000),
    }


def _config(
    *,
    mode: str = "desktop",
    trusted: tuple[str, ...] = (),
    hosts: tuple[str, ...] = ("localhost", "127.0.0.1", "[::1]"),
    origins: tuple[str, ...] = (),
    action: str = "reject",
) -> AccessConfig:
    return AccessConfig.build(
        deployment_mode=mode,
        trusted_proxy_cidrs=trusted,
        allowed_hosts=hosts,
        public_origins=origins,
        untrusted_forwarded_action=action,
    )


@pytest.mark.parametrize(
    "client",
    ["localhost", "127.0.0.1", "::1", "::ffff:127.0.0.1"],
)
def test_desktop_direct_loopback_is_implicit_owner(client: str) -> None:
    resolver = RequestContextResolver(_config())

    context = resolver.resolve(_scope(client=client))

    assert context.authentication_kind is AuthenticationKind.LOCAL_OWNER
    assert context.authenticated


def test_server_loopback_requires_a_session_and_mode_is_not_inferred_from_bind() -> (
    None
):
    config = AccessConfig.from_env(
        {
            "ROW_BOT_DEPLOYMENT_MODE": "server",
            "ROW_BOT_HOST": "127.0.0.1",
        }
    )
    context = RequestContextResolver(config).resolve(_scope())

    assert config.deployment_mode is DeploymentMode.SERVER
    assert context.authentication_kind is AuthenticationKind.UNAUTHENTICATED


@pytest.mark.parametrize("client", ["192.168.1.20", "172.17.0.1", "10.0.2.2"])
def test_lan_and_docker_like_peers_never_become_owner(client: str) -> None:
    resolver = RequestContextResolver(_config())

    context = resolver.resolve(_scope(client=client))

    assert context.authentication_kind is AuthenticationKind.UNAUTHENTICATED
    assert not context.authenticated


@pytest.mark.parametrize("client", ["", "not-an-ip", "127.0.0.1:5000", "[::1"])
def test_malformed_transport_peer_fails_closed(client: str) -> None:
    resolver = RequestContextResolver(_config())

    with pytest.raises(RequestContextError, match="invalid_transport_peer"):
        resolver.resolve(_scope(client=client))


def test_untrusted_forwarding_headers_reject_by_default() -> None:
    resolver = RequestContextResolver(_config())

    with pytest.raises(RequestContextError, match="untrusted_forwarding_headers"):
        resolver.resolve(
            _scope(
                headers=[
                    (b"x-forwarded-for", b"127.0.0.1"),
                    (b"x-forwarded-proto", b"https"),
                ],
                client="192.168.1.20",
            )
        )


def test_untrusted_forwarding_can_be_ignored_without_elevation() -> None:
    resolver = RequestContextResolver(
        _config(action=UntrustedForwardedAction.IGNORE.value)
    )

    context = resolver.resolve(
        _scope(
            client="192.168.1.20",
            headers=[
                (b"x-forwarded-for", b"127.0.0.1"),
                (b"x-forwarded-proto", b"https"),
            ],
        )
    )

    assert context.authentication_kind is AuthenticationKind.UNAUTHENTICATED
    assert context.effective_client == "192.168.1.20"
    assert context.scheme == "http"
    assert context.trusted_proxy is False


def test_trusted_proxy_walks_chain_from_edge_and_uses_public_origin() -> None:
    config = _config(
        mode="server",
        trusted=("10.0.0.0/8",),
        hosts=("rowbot.example.com",),
        origins=("https://rowbot.example.com",),
    )
    resolver = RequestContextResolver(config)

    context = resolver.resolve(
        _scope(
            client="10.0.0.2",
            host="rowbot.example.com",
            headers=[
                (
                    b"forwarded",
                    b"for=198.51.100.99, for=203.0.113.7;proto=https;host=rowbot.example.com",
                )
            ],
        ),
        session=SessionIdentity(
            device_id="device",
            session_id="session",
        ),
    )

    # The attacker-controlled left-most entry is not trusted after the actual
    # untrusted client at the proxy edge is reached.
    assert context.effective_client == "203.0.113.7"
    assert context.proxy_chain == ("10.0.0.2", "203.0.113.7")
    assert context.origin == "https://rowbot.example.com"
    assert context.authentication_kind is AuthenticationKind.SESSION


def test_trusted_x_forwarded_chain_normalizes_ipv4_mapped_ipv6() -> None:
    resolver = RequestContextResolver(
        _config(
            trusted=("127.0.0.0/8",),
            hosts=("rowbot.example.com",),
            origins=("https://rowbot.example.com",),
        )
    )

    provenance = resolver.resolve_provenance(
        _scope(
            client="::ffff:127.0.0.1",
            host="rowbot.example.com",
            headers=[
                (b"x-forwarded-for", b"::ffff:192.0.2.40"),
                (b"x-forwarded-proto", b"https"),
                (b"x-forwarded-host", b"rowbot.example.com"),
            ],
        )
    )

    assert provenance.transport_peer == "127.0.0.1"
    assert provenance.effective_client == "192.0.2.40"
    assert provenance.direct_loopback is False


@pytest.mark.parametrize(
    "headers,code",
    [
        (
            [(b"forwarded", b"for=unknown;proto=https;host=rowbot.example.com")],
            "malformed_forwarded_for",
        ),
        (
            [
                (b"forwarded", b"for=192.0.2.1"),
                (b"x-forwarded-for", b"192.0.2.1"),
            ],
            "ambiguous_forwarding_model",
        ),
        ([(b"x-real-ip", b"192.0.2.1")], "unsupported_forwarding_headers"),
        (
            [
                (b"x-forwarded-for", b"192.0.2.1"),
                (b"x-forwarded-proto", b"javascript"),
            ],
            "invalid_forwarded_proto",
        ),
    ],
)
def test_malformed_or_ambiguous_trusted_proxy_metadata_fails_closed(
    headers: list[tuple[bytes, bytes]],
    code: str,
) -> None:
    resolver = RequestContextResolver(
        _config(
            trusted=("10.0.0.0/8",),
            hosts=("rowbot.example.com",),
            origins=("https://rowbot.example.com",),
        )
    )

    with pytest.raises(RequestContextError, match=code):
        resolver.resolve(
            _scope(client="10.0.0.2", host="rowbot.example.com", headers=headers)
        )


def test_forwarded_proto_cannot_confuse_configured_public_origin() -> None:
    resolver = RequestContextResolver(
        _config(
            trusted=("10.0.0.0/8",),
            hosts=("rowbot.example.com",),
            origins=("https://rowbot.example.com",),
        )
    )

    with pytest.raises(RequestContextError, match="unexpected_origin"):
        resolver.resolve(
            _scope(
                client="10.0.0.2",
                host="rowbot.example.com",
                headers=[
                    (b"x-forwarded-for", b"192.0.2.1"),
                    (b"x-forwarded-proto", b"http"),
                    (b"x-forwarded-host", b"rowbot.example.com"),
                ],
            )
        )


def test_unexpected_host_is_rejected_before_authorization() -> None:
    resolver = RequestContextResolver(_config())

    with pytest.raises(RequestContextError, match="unexpected_host"):
        resolver.resolve(_scope(host="attacker.example"))


def test_same_origin_and_presentation_are_independent_of_authentication() -> None:
    resolver = RequestContextResolver(_config())
    owner = resolver.resolve(
        _scope(
            query=b"mobile=1",
            headers=[(b"origin", b"http://localhost:8080")],
        ),
    )
    remote_owner = resolver.resolve(
        _scope(client="192.168.1.20", query=b"mobile=0"),
        session=SessionIdentity(
            device_id="device",
            session_id="session",
        ),
    )

    assert owner.presentation is PresentationMode.COMPACT
    assert owner.authenticated
    assert request_origin_matches(
        owner, _scope(headers=[(b"origin", b"http://localhost:8080")])
    )
    assert remote_owner.presentation is PresentationMode.DESKTOP
    assert remote_owner.authenticated


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/", "/"),
        ("/settings?tab=system", "/settings?tab=system"),
        ("settings", "/"),
        ("//attacker.example/path", "/"),
        ("https://attacker.example/", "/"),
        ("/%2f%2fattacker.example", "/"),
        ("/a/../admin", "/"),
        ("/safe\r\nLocation: https://attacker.example", "/"),
    ],
)
def test_safe_relative_next_blocks_open_redirects(value: str, expected: str) -> None:
    assert safe_relative_next(value) == expected


def test_access_configuration_rejects_malformed_values() -> None:
    with pytest.raises(AccessConfigError):
        AccessConfig.build(deployment_mode="container")
    with pytest.raises(AccessConfigError):
        AccessConfig.build(trusted_proxy_cidrs=("not-a-cidr",))
    with pytest.raises(AccessConfigError):
        AccessConfig.build(allowed_hosts=("*",))
    with pytest.raises(AccessConfigError):
        canonical_origin("https://rowbot.example.com/path")
