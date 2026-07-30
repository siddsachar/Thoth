"""Authenticated loopback control channel between app and owning launcher."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from enum import StrEnum
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
import json
import os
import re
import secrets
import threading
import time
from typing import Callable, Mapping
import urllib.error
import urllib.request

LAUNCH_SECRET_ENV = "ROW_BOT_LAUNCH_SECRET"
LAUNCHER_CONTROL_PORT_ENV = "ROW_BOT_LAUNCHER_CONTROL_PORT"
LAUNCHER_CONTROL_NONCE_HEADER = "X-Row-Bot-Control-Nonce"
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{22,128}$")
_MAX_REPLAY_NONCES = 256
_NONCE_TTL_SECONDS = 300.0


class LauncherControlStatus(StrEnum):
    ACCEPTED = "accepted"
    UNAVAILABLE = "unavailable"
    REJECTED = "rejected"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class LauncherControlResult:
    status: LauncherControlStatus
    restart_required: bool

    @property
    def accepted(self) -> bool:
        return self.status is LauncherControlStatus.ACCEPTED


def _is_loopback(value: object) -> bool:
    try:
        return ip_address(str(value or "")).is_loopback
    except ValueError:
        return False


class LauncherControlServer:
    """Small launcher-owned server exposing only a guarded restart operation."""

    def __init__(
        self,
        restart_child: Callable[[], None],
        *,
        secret: str | None = None,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.restart_child = restart_child
        self.secret = secret or secrets.token_urlsafe(32)
        self._now = now
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._used_nonces: OrderedDict[str, float] = OrderedDict()
        self._nonce_lock = threading.Lock()

    @property
    def port(self) -> int | None:
        if self._httpd is None:
            return None
        return int(self._httpd.server_address[1])

    def child_environment(self) -> dict[str, str]:
        port = self.port
        if port is None:
            return {}
        return {
            LAUNCH_SECRET_ENV: self.secret,
            LAUNCHER_CONTROL_PORT_ENV: str(port),
        }

    def _consume_nonce(self, nonce: str) -> bool:
        if not _NONCE_RE.fullmatch(nonce):
            return False
        now = self._now()
        cutoff = now - _NONCE_TTL_SECONDS
        with self._nonce_lock:
            while self._used_nonces:
                _oldest, created = next(iter(self._used_nonces.items()))
                if created > cutoff:
                    break
                self._used_nonces.popitem(last=False)
            if nonce in self._used_nonces:
                return False
            if len(self._used_nonces) >= _MAX_REPLAY_NONCES:
                self._used_nonces.popitem(last=False)
            self._used_nonces[nonce] = now
        return True

    def start(self) -> int:
        if self._httpd is not None:
            assert self.port is not None
            return self.port

        controller = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "RowBotLauncherControl/1"
            sys_version = ""

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def _respond(self, status: int, payload: Mapping[str, object]) -> None:
                body = json.dumps(dict(payload), separators=(",", ":")).encode(
                    "utf-8"
                )
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:  # noqa: N802
                if not _is_loopback(self.client_address[0]):
                    self._respond(403, {"ok": False, "error": "loopback_required"})
                    return
                if self.path != "/v1/restart-child":
                    self._respond(404, {"ok": False, "error": "not_found"})
                    return
                authorization = str(self.headers.get("Authorization") or "")
                expected = f"Bearer {controller.secret}"
                if not hmac.compare_digest(authorization, expected):
                    self._respond(403, {"ok": False, "error": "invalid_control"})
                    return
                nonce = str(
                    self.headers.get(LAUNCHER_CONTROL_NONCE_HEADER) or ""
                ).strip()
                if not controller._consume_nonce(nonce):
                    self._respond(409, {"ok": False, "error": "replay_rejected"})
                    return
                try:
                    content_length = int(self.headers.get("Content-Length") or "0")
                except ValueError:
                    self._respond(400, {"ok": False, "error": "invalid_request"})
                    return
                if content_length not in {0}:
                    self._respond(400, {"ok": False, "error": "body_not_allowed"})
                    return
                self._respond(202, {"ok": True, "accepted": True})
                threading.Thread(
                    target=controller.restart_child,
                    daemon=True,
                    name="row-bot-child-restart",
                ).start()

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            daemon=True,
            name="row-bot-launcher-control",
        )
        self._thread.start()
        assert self.port is not None
        return self.port

    def stop(self) -> None:
        server, thread = self._httpd, self._thread
        self._httpd = None
        self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)


def request_launcher_restart(
    *,
    environ: Mapping[str, str] | None = None,
    timeout: float = 3.0,
) -> LauncherControlResult:
    """Ask the owning launcher to restart its child, or request manual restart."""
    env = os.environ if environ is None else environ
    secret = str(env.get(LAUNCH_SECRET_ENV) or "")
    raw_port = str(env.get(LAUNCHER_CONTROL_PORT_ENV) or "")
    if len(secret) < 32:
        return LauncherControlResult(LauncherControlStatus.UNAVAILABLE, True)
    try:
        port = int(raw_port)
    except ValueError:
        return LauncherControlResult(LauncherControlStatus.UNAVAILABLE, True)
    if not 1 <= port <= 65535:
        return LauncherControlResult(LauncherControlStatus.UNAVAILABLE, True)
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/restart-child",
        data=b"",
        method="POST",
        headers={
            "Authorization": f"Bearer {secret}",
            LAUNCHER_CONTROL_NONCE_HEADER: secrets.token_urlsafe(24),
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=max(0.1, min(timeout, 10.0))) as response:
            accepted = int(getattr(response, "status", 0)) == 202
    except urllib.error.HTTPError:
        return LauncherControlResult(LauncherControlStatus.REJECTED, True)
    except (OSError, urllib.error.URLError, TimeoutError):
        return LauncherControlResult(LauncherControlStatus.ERROR, True)
    return LauncherControlResult(
        LauncherControlStatus.ACCEPTED if accepted else LauncherControlStatus.ERROR,
        not accepted,
    )


__all__ = [
    "LAUNCHER_CONTROL_NONCE_HEADER",
    "LAUNCHER_CONTROL_PORT_ENV",
    "LAUNCH_SECRET_ENV",
    "LauncherControlResult",
    "LauncherControlServer",
    "LauncherControlStatus",
    "request_launcher_restart",
]
