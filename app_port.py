"""Shared app-port helpers for the Thoth web UI."""

from __future__ import annotations

import os
from collections.abc import Mapping

DEFAULT_APP_PORT = 8080
THOTH_PORT_ENV = "THOTH_PORT"
THOTH_HOST_ENV = "THOTH_HOST"


def parse_app_port(value: object, default: int = DEFAULT_APP_PORT) -> int:
    """Return a valid TCP port parsed from *value*, or *default*."""
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    if 1 <= port <= 65535:
        return port
    return default


def get_app_port(
    default: int = DEFAULT_APP_PORT,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Return the active Thoth app port for this process."""
    env = os.environ if environ is None else environ
    return parse_app_port(env.get(THOTH_PORT_ENV), default=default)


def app_base_url(host: str = "127.0.0.1", *, port: int | None = None) -> str:
    """Return the local base URL for the active app port."""
    resolved_port = get_app_port() if port is None else parse_app_port(port)
    return f"http://{host}:{resolved_port}"
