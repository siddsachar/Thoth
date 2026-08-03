"""Canonical ASGI request provenance and access context resolution."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from ipaddress import IPv4Address, IPv6Address, ip_address
import posixpath
import re
from typing import Iterable, Mapping
from urllib.parse import parse_qs, quote, unquote, urlsplit, urlunsplit

from row_bot.access.config import (
    AccessConfig,
    AccessConfigError,
    DeploymentMode,
    UntrustedForwardedAction,
    canonical_host,
    canonical_origin,
    host_without_port,
)

IPAddress = IPv4Address | IPv6Address

ACCESS_CONTEXT_SCOPE_KEY = "row_bot.access_context"
PROVENANCE_SCOPE_KEY = "row_bot.request_provenance"

SUPPORTED_FORWARDED_HEADERS = frozenset(
    {
        "forwarded",
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-port",
        "x-forwarded-proto",
    }
)
RECOGNIZED_FORWARDING_HEADERS = frozenset(
    set(SUPPORTED_FORWARDED_HEADERS)
    | {
        "x-forwarded",
        "x-forwarded-server",
        "x-original-forwarded-for",
        "x-real-ip",
        "x-client-ip",
        "x-cluster-client-ip",
        "cf-connecting-ip",
        "true-client-ip",
        "fly-client-ip",
    }
)
_UNSAFE_NEXT_CHARACTERS = frozenset({"\r", "\n", "\x00", "\\"})
_FORWARDED_TOKEN_RE = re.compile(r"^[A-Za-z][A-Za-z0-9!#$%&'*+\-.^_`|~]*$")


class RequestContextError(ValueError):
    """A privacy-safe request classification failure."""

    def __init__(self, code: str, *, status_code: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


class AuthenticationKind(StrEnum):
    LOCAL_OWNER = "local_owner"
    SESSION = "session"
    UNAUTHENTICATED = "unauthenticated"


class PresentationMode(StrEnum):
    DESKTOP = "desktop"
    COMPACT = "compact"


@dataclass(frozen=True, slots=True)
class SessionIdentity:
    """Authentication result supplied by the access-session service."""

    device_id: str
    session_id: str


@dataclass(frozen=True, slots=True)
class RequestProvenance:
    """Network and browser-facing metadata after trusted-proxy validation."""

    transport_peer: str
    effective_client: str
    scheme: str
    host: str
    origin: str
    forwarding_headers: tuple[str, ...]
    trusted_proxy: bool
    trusted_proxy_peer: str | None
    proxy_chain: tuple[str, ...]
    direct_loopback: bool


@dataclass(frozen=True, slots=True)
class AccessContext:
    """One immutable authentication and presentation context per request."""

    deployment_mode: DeploymentMode
    authentication_kind: AuthenticationKind
    transport_peer: str
    effective_client: str
    forwarding_headers: tuple[str, ...]
    trusted_proxy: bool
    trusted_proxy_peer: str | None
    proxy_chain: tuple[str, ...]
    scheme: str
    host: str
    origin: str
    presentation: PresentationMode
    direct_loopback: bool = False
    device_id: str | None = None
    session_id: str | None = None

    @property
    def authenticated(self) -> bool:
        return self.authentication_kind is not AuthenticationKind.UNAUTHENTICATED

    @property
    def is_local_owner(self) -> bool:
        return self.authentication_kind is AuthenticationKind.LOCAL_OWNER

def _headers(scope: Mapping[str, object]) -> dict[str, list[str]]:
    headers: dict[str, list[str]] = {}
    for raw_name, raw_value in scope.get("headers", []) or []:
        name = bytes(raw_name).decode("latin-1", errors="ignore").lower()
        value = bytes(raw_value).decode("latin-1", errors="ignore")
        headers.setdefault(name, []).append(value)
    return headers


def _single_header(
    headers: Mapping[str, list[str]],
    name: str,
    *,
    required: bool = False,
) -> str:
    values = headers.get(name, [])
    if not values:
        if required:
            raise RequestContextError(f"missing_{name.replace('-', '_')}")
        return ""
    if len(values) != 1:
        raise RequestContextError(f"ambiguous_{name.replace('-', '_')}")
    return values[0].strip()


def normalize_ip(value: object, *, allow_localhost: bool = True) -> IPAddress | None:
    """Parse a peer IP, collapsing IPv4-mapped IPv6 to IPv4.

    Malformed values return ``None`` so callers can fail closed without
    including attacker-controlled input in diagnostics.
    """

    text = str(value or "").strip()
    if allow_localhost and text.lower().rstrip(".") == "localhost":
        return IPv4Address("127.0.0.1")
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    # ASGI peer hosts may contain an IPv6 zone identifier.  It is not part of
    # address identity for loopback or CIDR membership.
    if "%" in text:
        text, _separator, zone = text.partition("%")
        if not text or not zone:
            return None
    try:
        parsed = ip_address(text)
    except ValueError:
        return None
    if isinstance(parsed, IPv6Address) and parsed.ipv4_mapped is not None:
        return parsed.ipv4_mapped
    return parsed


def _ip_text(address: IPAddress) -> str:
    return address.compressed


def _is_trusted_proxy(address: IPAddress, config: AccessConfig) -> bool:
    for network in config.trusted_proxy_cidrs:
        if address.version == network.version and address in network:
            return True
    return False


def _split_quoted(value: str, separator: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    quoted = False
    escaped = False
    for char in value:
        if escaped:
            current.append(char)
            escaped = False
        elif quoted and char == "\\":
            escaped = True
        elif char == '"':
            quoted = not quoted
            current.append(char)
        elif char == separator and not quoted:
            parts.append("".join(current).strip())
            current.clear()
        else:
            current.append(char)
    if quoted or escaped:
        raise RequestContextError("malformed_forwarded_header")
    parts.append("".join(current).strip())
    if any(not part for part in parts):
        raise RequestContextError("malformed_forwarded_header")
    return parts


def _unquote_forwarded(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        return text[1:-1].replace(r"\\", "\\").replace(r"\"", '"')
    if '"' in text:
        raise RequestContextError("malformed_forwarded_header")
    return text


def _forwarded_for_ip(value: str) -> IPAddress:
    text = _unquote_forwarded(value)
    if not text or text.lower() == "unknown" or text.startswith("_"):
        raise RequestContextError("malformed_forwarded_for")
    host = text
    if text.startswith("["):
        closing = text.find("]")
        if closing < 0:
            raise RequestContextError("malformed_forwarded_for")
        host = text[1:closing]
        remainder = text[closing + 1 :]
        if remainder and (not remainder.startswith(":") or not remainder[1:].isdigit()):
            raise RequestContextError("malformed_forwarded_for")
    elif text.count(":") == 1:
        possible_host, possible_port = text.rsplit(":", 1)
        if possible_port.isdigit():
            host = possible_host
    address = normalize_ip(host, allow_localhost=False)
    if address is None:
        raise RequestContextError("malformed_forwarded_for")
    return address


@dataclass(frozen=True, slots=True)
class _ForwardedValues:
    chain: tuple[IPAddress, ...]
    scheme: str | None = None
    host: str | None = None


def _parse_forwarded(value: str) -> _ForwardedValues:
    elements: list[dict[str, str]] = []
    for raw_element in _split_quoted(value, ","):
        parameters: dict[str, str] = {}
        for raw_parameter in _split_quoted(raw_element, ";"):
            if "=" not in raw_parameter:
                raise RequestContextError("malformed_forwarded_header")
            raw_name, raw_value = raw_parameter.split("=", 1)
            name = raw_name.strip().lower()
            if not _FORWARDED_TOKEN_RE.fullmatch(name) or name in parameters:
                raise RequestContextError("malformed_forwarded_header")
            parameters[name] = _unquote_forwarded(raw_value)
        if "for" not in parameters:
            raise RequestContextError("malformed_forwarded_header")
        elements.append(parameters)
    if not elements:
        raise RequestContextError("malformed_forwarded_header")
    edge = elements[-1]
    scheme = edge.get("proto")
    if scheme is not None:
        scheme = scheme.lower()
        if scheme not in {"http", "https"}:
            raise RequestContextError("invalid_forwarded_proto")
    host = edge.get("host")
    if host is not None:
        try:
            host = canonical_host(host)
        except AccessConfigError as exc:
            raise RequestContextError("invalid_forwarded_host") from exc
    return _ForwardedValues(
        chain=tuple(_forwarded_for_ip(element["for"]) for element in elements),
        scheme=scheme,
        host=host,
    )


def _comma_values(value: str, *, code: str) -> list[str]:
    values = [part.strip() for part in value.split(",")]
    if not values or any(not part for part in values):
        raise RequestContextError(code)
    return values


def _parse_x_forwarded(headers: Mapping[str, list[str]]) -> _ForwardedValues:
    raw_for = _single_header(headers, "x-forwarded-for", required=True)
    chain = tuple(
        _forwarded_for_ip(value)
        for value in _comma_values(raw_for, code="malformed_x_forwarded_for")
    )

    scheme: str | None = None
    raw_proto = _single_header(headers, "x-forwarded-proto")
    if raw_proto:
        scheme = _comma_values(raw_proto, code="malformed_x_forwarded_proto")[
            -1
        ].lower()
        if scheme not in {"http", "https"}:
            raise RequestContextError("invalid_forwarded_proto")

    host: str | None = None
    raw_host = _single_header(headers, "x-forwarded-host")
    if raw_host:
        try:
            host = canonical_host(
                _comma_values(raw_host, code="malformed_x_forwarded_host")[-1]
            )
        except AccessConfigError as exc:
            raise RequestContextError("invalid_forwarded_host") from exc

    raw_port = _single_header(headers, "x-forwarded-port")
    if raw_port:
        port_text = _comma_values(raw_port, code="malformed_x_forwarded_port")[-1]
        if not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
            raise RequestContextError("invalid_forwarded_port")
        if host is None:
            raise RequestContextError("forwarded_port_without_host")
        if host == host_without_port(host):
            host = f"{host}:{int(port_text)}"
        elif not host.endswith(f":{int(port_text)}"):
            raise RequestContextError("conflicting_forwarded_port")
    return _ForwardedValues(chain=chain, scheme=scheme, host=host)


def _effective_client(
    transport: IPAddress,
    forwarded_chain: Iterable[IPAddress],
    config: AccessConfig,
) -> tuple[IPAddress, tuple[str, ...]]:
    """Walk from the trusted edge and stop at the first untrusted address."""

    current = transport
    walked: list[str] = [_ip_text(transport)]
    for candidate in reversed(tuple(forwarded_chain)):
        if not _is_trusted_proxy(current, config):
            break
        current = candidate
        walked.append(_ip_text(candidate))
    return current, tuple(walked)


def _scope_scheme(scope: Mapping[str, object]) -> str:
    scheme = str(scope.get("scheme") or "http").strip().lower()
    if scheme == "ws":
        return "http"
    if scheme == "wss":
        return "https"
    if scheme not in {"http", "https"}:
        raise RequestContextError("invalid_transport_scheme")
    return scheme


class RequestContextResolver:
    """Resolve transport/proxy/host/origin data exactly once."""

    def __init__(self, config: AccessConfig) -> None:
        self.config = config

    def resolve_provenance(self, scope: Mapping[str, object]) -> RequestProvenance:
        headers = _headers(scope)
        client = scope.get("client") or ("", 0)
        try:
            raw_transport = client[0]  # type: ignore[index]
        except (IndexError, TypeError):
            raw_transport = ""
        transport = normalize_ip(raw_transport)
        if transport is None:
            raise RequestContextError("invalid_transport_peer")
        transport_text = _ip_text(transport)

        forwarding_headers = tuple(
            sorted(name for name in headers if name in RECOGNIZED_FORWARDING_HEADERS)
        )
        trusted_transport = _is_trusted_proxy(transport, self.config)
        if forwarding_headers and not trusted_transport:
            if (
                self.config.untrusted_forwarded_action
                is UntrustedForwardedAction.REJECT
            ):
                raise RequestContextError("untrusted_forwarding_headers")
            forwarded = None
        elif forwarding_headers:
            unsupported = set(forwarding_headers) - SUPPORTED_FORWARDED_HEADERS
            if unsupported:
                raise RequestContextError("unsupported_forwarding_headers")
            has_standard = "forwarded" in forwarding_headers
            has_x_forwarded = any(
                name.startswith("x-forwarded-") for name in forwarding_headers
            )
            if has_standard and has_x_forwarded:
                raise RequestContextError("ambiguous_forwarding_model")
            if has_standard:
                forwarded = _parse_forwarded(
                    _single_header(headers, "forwarded", required=True)
                )
            else:
                forwarded = _parse_x_forwarded(headers)
        else:
            forwarded = None

        raw_host = _single_header(headers, "host", required=True)
        try:
            direct_host = canonical_host(raw_host)
        except AccessConfigError as exc:
            raise RequestContextError("invalid_host", status_code=400) from exc
        scheme = _scope_scheme(scope)
        host = direct_host
        effective = transport
        proxy_chain: tuple[str, ...] = ()
        if forwarded is not None:
            effective, proxy_chain = _effective_client(
                transport, forwarded.chain, self.config
            )
            scheme = forwarded.scheme or scheme
            host = forwarded.host or direct_host

        if not self.config.host_allowed(host):
            raise RequestContextError("unexpected_host", status_code=400)
        try:
            origin = canonical_origin(f"{scheme}://{host}")
        except AccessConfigError as exc:
            raise RequestContextError("invalid_origin", status_code=400) from exc
        if forwarded is not None and not self.config.origin_allowed(origin):
            raise RequestContextError("unexpected_origin", status_code=400)

        direct_loopback = (
            transport.is_loopback and not forwarding_headers and forwarded is None
        )
        return RequestProvenance(
            transport_peer=transport_text,
            effective_client=_ip_text(effective),
            scheme=scheme,
            host=host,
            origin=origin,
            forwarding_headers=forwarding_headers,
            trusted_proxy=forwarded is not None,
            trusted_proxy_peer=transport_text if forwarded is not None else None,
            proxy_chain=proxy_chain,
            direct_loopback=direct_loopback,
        )

    def resolve(
        self,
        scope: Mapping[str, object],
        *,
        session: SessionIdentity | None = None,
    ) -> AccessContext:
        provenance = self.resolve_provenance(scope)
        if (
            self.config.deployment_mode is DeploymentMode.DESKTOP
            and provenance.direct_loopback
        ):
            kind = AuthenticationKind.LOCAL_OWNER
            device_id = None
            session_id = None
        elif session is not None:
            kind = AuthenticationKind.SESSION
            device_id = session.device_id
            session_id = session.session_id
        else:
            kind = AuthenticationKind.UNAUTHENTICATED
            device_id = None
            session_id = None

        presentation = requested_presentation(scope)
        return AccessContext(
            deployment_mode=self.config.deployment_mode,
            authentication_kind=kind,
            transport_peer=provenance.transport_peer,
            effective_client=provenance.effective_client,
            forwarding_headers=provenance.forwarding_headers,
            trusted_proxy=provenance.trusted_proxy,
            trusted_proxy_peer=provenance.trusted_proxy_peer,
            proxy_chain=provenance.proxy_chain,
            scheme=provenance.scheme,
            host=provenance.host,
            origin=provenance.origin,
            presentation=presentation,
            direct_loopback=provenance.direct_loopback,
            device_id=device_id,
            session_id=session_id,
        )


def requested_presentation(
    scope: Mapping[str, object],
) -> PresentationMode:
    """Resolve presentation without allowing it to change authorization."""

    query = bytes(scope.get("query_string") or b"").decode("latin-1", errors="ignore")
    values = parse_qs(query, keep_blank_values=True)
    requested = str((values.get("mobile") or values.get("m") or [""])[0]).lower()
    if requested in {"1", "true", "yes", "compact"}:
        return PresentationMode.COMPACT
    return PresentationMode.DESKTOP


def request_origin_matches(context: AccessContext, scope: Mapping[str, object]) -> bool:
    """Return whether the browser Origin exactly matches the resolved origin."""

    headers = _headers(scope)
    try:
        raw_origin = _single_header(headers, "origin", required=True)
        origin = canonical_origin(raw_origin)
    except (AccessConfigError, RequestContextError):
        return False
    return origin == context.origin


def safe_relative_next(value: object, *, default: str = "/") -> str:
    """Normalize an application-relative redirect target or return ``default``."""

    text = str(value or "").strip()
    if not text or any(char in text for char in _UNSAFE_NEXT_CHARACTERS):
        return default
    try:
        parsed = urlsplit(text)
    except ValueError:
        return default
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
        return default
    try:
        decoded_path = unquote(parsed.path, errors="strict")
    except (UnicodeDecodeError, ValueError):
        return default
    if (
        decoded_path.startswith("//")
        or "\\" in decoded_path
        or any(segment == ".." for segment in decoded_path.split("/"))
    ):
        return default
    normalized_path = posixpath.normpath(decoded_path)
    if not normalized_path.startswith("/"):
        return default
    if decoded_path.endswith("/") and not normalized_path.endswith("/"):
        normalized_path += "/"
    encoded_path = quote(normalized_path, safe="/:@-._~!$&'()*+,;=")
    return urlunsplit(("", "", encoded_path, parsed.query, ""))
