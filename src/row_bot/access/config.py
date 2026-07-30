"""Configuration primitives for Row-Bot request access decisions.

This module deliberately does not inspect the listen address when selecting a
deployment mode.  Reachability and authentication are separate concerns.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from ipaddress import IPv4Network, IPv6Network, ip_address, ip_network
import os
from typing import Iterable, Mapping
from urllib.parse import urlsplit

IPAddressNetwork = IPv4Network | IPv6Network

DEFAULT_ALLOWED_HOSTS = ("localhost", "127.0.0.1", "[::1]")
_DEFAULT_PORTS = {"http": 80, "https": 443}


class AccessConfigError(ValueError):
    """Raised when access configuration cannot be interpreted safely."""


class DeploymentMode(StrEnum):
    """The explicit application deployment boundary."""

    DESKTOP = "desktop"
    SERVER = "server"

    @classmethod
    def parse(cls, value: object) -> DeploymentMode:
        text = str(value or "").strip().lower()
        try:
            return cls(text)
        except ValueError as exc:
            raise AccessConfigError(
                "deployment mode must be explicitly set to 'desktop' or 'server'"
            ) from exc


class UntrustedForwardedAction(StrEnum):
    """How forwarding metadata received from an untrusted transport is handled."""

    REJECT = "reject"
    IGNORE = "ignore"


def _normalized_hostname(value: str) -> str:
    text = str(value or "").strip().rstrip(".")
    if not text or any(char.isspace() for char in text):
        raise AccessConfigError("host is empty or contains whitespace")
    if any(char in text for char in ("/", "\\", "@", "#", "?")):
        raise AccessConfigError("host contains an invalid delimiter")
    try:
        parsed_ip = ip_address(text)
    except ValueError:
        try:
            return text.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise AccessConfigError("host is not valid IDNA") from exc
    if parsed_ip.version == 6:
        return f"[{parsed_ip.compressed}]"
    return parsed_ip.compressed


def canonical_host(value: object, *, allow_port: bool = True) -> str:
    """Return a canonical host or host:port authority.

    IPv6 authorities must be bracketed when a port is present.  A bare IPv6
    address is accepted and normalized to brackets.
    """

    text = str(value or "").strip()
    if not text or "," in text:
        raise AccessConfigError("host is empty or ambiguous")

    host_text = text
    port: int | None = None
    if text.startswith("["):
        closing = text.find("]")
        if closing < 0:
            raise AccessConfigError("IPv6 host has no closing bracket")
        host_text = text[1:closing]
        remainder = text[closing + 1 :]
        if remainder:
            if not remainder.startswith(":") or not remainder[1:].isdigit():
                raise AccessConfigError("host port is malformed")
            port = int(remainder[1:])
    elif text.count(":") == 1:
        possible_host, possible_port = text.rsplit(":", 1)
        if possible_port.isdigit():
            host_text = possible_host
            port = int(possible_port)
    elif text.count(":") > 1:
        # A bare IPv6 literal without a port.
        host_text = text

    if port is not None:
        if not allow_port:
            raise AccessConfigError("host port is not allowed here")
        if not 1 <= port <= 65535:
            raise AccessConfigError("host port is outside 1..65535")

    host = _normalized_hostname(host_text)
    return f"{host}:{port}" if port is not None else host


def host_without_port(authority: object) -> str:
    canonical = canonical_host(authority)
    if canonical.startswith("["):
        return canonical[: canonical.find("]") + 1]
    if canonical.count(":") == 1:
        return canonical.rsplit(":", 1)[0]
    return canonical


def canonical_origin(value: object) -> str:
    """Normalize an HTTP(S) origin, rejecting paths and credentials."""

    text = str(value or "").strip()
    if not text:
        raise AccessConfigError("origin is required")
    try:
        parsed = urlsplit(text)
    except ValueError as exc:
        raise AccessConfigError("origin is malformed") from exc
    scheme = parsed.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        raise AccessConfigError("origin scheme must be http or https")
    if not parsed.netloc or parsed.username is not None or parsed.password is not None:
        raise AccessConfigError("origin must contain a host and no credentials")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise AccessConfigError("origin must not contain a path, query, or fragment")
    try:
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise AccessConfigError("origin port is malformed") from exc
    if not host:
        raise AccessConfigError("origin host is missing")
    authority = canonical_host(host, allow_port=False)
    if port is not None and port != _DEFAULT_PORTS[scheme]:
        authority = f"{authority}:{port}"
    return f"{scheme}://{authority}"


def parse_trusted_proxy_cidrs(values: Iterable[object]) -> tuple[IPAddressNetwork, ...]:
    """Parse trusted proxy networks, rejecting every malformed entry."""

    networks: list[IPAddressNetwork] = []
    for raw in values:
        text = str(raw or "").strip()
        if not text:
            raise AccessConfigError("trusted proxy CIDR cannot be empty")
        try:
            network = ip_network(text, strict=False)
        except ValueError as exc:
            raise AccessConfigError(f"invalid trusted proxy CIDR: {text!r}") from exc
        if network not in networks:
            networks.append(network)
    return tuple(networks)


def _split_csv(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    return tuple(part.strip() for part in str(value).split(",") if part.strip())


@dataclass(frozen=True, slots=True)
class AccessConfig:
    """Validated request-security configuration."""

    deployment_mode: DeploymentMode = DeploymentMode.DESKTOP
    trusted_proxy_cidrs: tuple[IPAddressNetwork, ...] = ()
    allowed_hosts: tuple[str, ...] = DEFAULT_ALLOWED_HOSTS
    public_origins: tuple[str, ...] = ()
    untrusted_forwarded_action: UntrustedForwardedAction = (
        UntrustedForwardedAction.REJECT
    )

    @classmethod
    def build(
        cls,
        *,
        deployment_mode: DeploymentMode | str = DeploymentMode.DESKTOP,
        trusted_proxy_cidrs: Iterable[object] = (),
        allowed_hosts: Iterable[object] = DEFAULT_ALLOWED_HOSTS,
        public_origins: Iterable[object] = (),
        untrusted_forwarded_action: UntrustedForwardedAction | str = (
            UntrustedForwardedAction.REJECT
        ),
    ) -> AccessConfig:
        mode = (
            deployment_mode
            if isinstance(deployment_mode, DeploymentMode)
            else DeploymentMode.parse(deployment_mode)
        )
        try:
            forwarded_action = UntrustedForwardedAction(
                str(untrusted_forwarded_action).strip().lower()
            )
        except ValueError as exc:
            raise AccessConfigError(
                "untrusted forwarded action must be 'reject' or 'ignore'"
            ) from exc

        normalized_hosts: list[str] = []
        for raw in allowed_hosts:
            host = canonical_host(raw)
            if host == "*":
                raise AccessConfigError("wildcard allowed hosts are not supported")
            if host not in normalized_hosts:
                normalized_hosts.append(host)
        if not normalized_hosts:
            raise AccessConfigError("at least one allowed host is required")

        normalized_origins: list[str] = []
        for raw in public_origins:
            origin = canonical_origin(raw)
            if origin not in normalized_origins:
                normalized_origins.append(origin)

        return cls(
            deployment_mode=mode,
            trusted_proxy_cidrs=parse_trusted_proxy_cidrs(trusted_proxy_cidrs),
            allowed_hosts=tuple(normalized_hosts),
            public_origins=tuple(normalized_origins),
            untrusted_forwarded_action=forwarded_action,
        )

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> AccessConfig:
        """Build configuration from explicit environment values.

        The deployment mode defaults to the backwards-compatible desktop mode;
        it is never derived from ``ROW_BOT_HOST`` or another listen setting.
        """

        env = os.environ if environ is None else environ
        public_origins = _split_csv(
            env.get("ROW_BOT_PUBLIC_ORIGINS") or env.get("ROW_BOT_PUBLIC_URL")
        )
        allowed_hosts = list(_split_csv(env.get("ROW_BOT_ALLOWED_HOSTS")))
        if not allowed_hosts:
            allowed_hosts.extend(DEFAULT_ALLOWED_HOSTS)
            for raw_origin in public_origins:
                try:
                    origin_host = urlsplit(canonical_origin(raw_origin)).netloc
                except AccessConfigError:
                    continue
                normalized = host_without_port(origin_host)
                if normalized not in allowed_hosts:
                    allowed_hosts.append(normalized)
        return cls.build(
            deployment_mode=env.get("ROW_BOT_DEPLOYMENT_MODE", "desktop"),
            trusted_proxy_cidrs=_split_csv(
                env.get("ROW_BOT_TRUSTED_PROXY_CIDRS")
                or env.get("ROW_BOT_TRUSTED_PROXIES")
            ),
            allowed_hosts=allowed_hosts,
            public_origins=public_origins,
            untrusted_forwarded_action=env.get(
                "ROW_BOT_UNTRUSTED_FORWARDED_ACTION",
                UntrustedForwardedAction.REJECT.value,
            ),
        )

    def host_allowed(self, authority: object) -> bool:
        """Return whether an exact host rule accepts the request authority.

        A configured host without a port accepts that hostname on any port.
        A host with a port accepts only that authority.
        """

        try:
            normalized = canonical_host(authority)
        except AccessConfigError:
            return False
        host = host_without_port(normalized)
        return normalized in self.allowed_hosts or host in self.allowed_hosts

    def origin_allowed(self, origin: object) -> bool:
        try:
            normalized = canonical_origin(origin)
        except AccessConfigError:
            return False
        if self.public_origins:
            return normalized in self.public_origins
        return self.host_allowed(urlsplit(normalized).netloc)
