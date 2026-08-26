from __future__ import annotations

import threading
import urllib.error
import urllib.request

import pytest

from row_bot.access.launcher_control import (
    LAUNCHER_CONTROL_NONCE_HEADER,
    LAUNCHER_CONTROL_PORT_ENV,
    LAUNCH_SECRET_ENV,
    LauncherControlServer,
    LauncherControlStatus,
    _is_loopback,
    request_launcher_restart,
)


def _post(
    port: int,
    secret: str,
    nonce: str,
    path: str = "/v1/restart-child",
) -> int:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=b"",
        method="POST",
        headers={
            "Authorization": f"Bearer {secret}",
            LAUNCHER_CONTROL_NONCE_HEADER: nonce,
        },
    )
    with urllib.request.urlopen(request, timeout=2) as response:
        return int(response.status)


def test_launcher_control_accepts_one_authenticated_restart() -> None:
    restarted = threading.Event()
    server = LauncherControlServer(restarted.set, secret="s" * 43)
    port = server.start()
    try:
        result = request_launcher_restart(
            environ={
                LAUNCH_SECRET_ENV: server.secret,
                LAUNCHER_CONTROL_PORT_ENV: str(port),
            }
        )
        assert result.status is LauncherControlStatus.ACCEPTED
        assert result.restart_required is False
        assert restarted.wait(2)
    finally:
        server.stop()


def test_launcher_control_rejects_bad_secret_and_replay() -> None:
    called = threading.Event()
    server = LauncherControlServer(called.set, secret="v" * 43)
    port = server.start()
    nonce = "n" * 24
    try:
        with pytest.raises(urllib.error.HTTPError) as bad:
            _post(port, "wrong" * 12, nonce)
        assert bad.value.code == 403
        assert _post(port, server.secret, nonce) == 202
        with pytest.raises(urllib.error.HTTPError) as replay:
            _post(port, server.secret, nonce)
        assert replay.value.code == 409
        assert called.wait(2)
    finally:
        server.stop()


def test_launcher_control_accepts_authenticated_launcher_shutdown_once() -> None:
    restarted = threading.Event()
    shutdown = threading.Event()
    server = LauncherControlServer(
        restarted.set,
        shutdown_launcher=shutdown.set,
        secret="k" * 43,
    )
    port = server.start()
    nonce = "s" * 24
    try:
        assert (
            _post(port, server.secret, nonce, "/v1/shutdown-launcher") == 202
        )
        assert shutdown.wait(2)
        assert not restarted.is_set()
        with pytest.raises(urllib.error.HTTPError) as replay:
            _post(port, server.secret, nonce, "/v1/shutdown-launcher")
        assert replay.value.code == 409
    finally:
        server.stop()


def test_launcher_control_hides_shutdown_route_without_callback() -> None:
    server = LauncherControlServer(lambda: None, secret="h" * 43)
    port = server.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as missing:
            _post(port, server.secret, "m" * 24, "/v1/shutdown-launcher")
        assert missing.value.code == 404
    finally:
        server.stop()


def test_launcher_control_missing_or_dead_channel_requires_restart() -> None:
    missing = request_launcher_restart(environ={})
    assert missing.status is LauncherControlStatus.UNAVAILABLE
    assert missing.restart_required is True

    dead = request_launcher_restart(
        environ={
            LAUNCH_SECRET_ENV: "x" * 43,
            LAUNCHER_CONTROL_PORT_ENV: "1",
        },
        timeout=0.1,
    )
    assert dead.status is LauncherControlStatus.ERROR
    assert dead.restart_required is True


def test_launcher_control_environment_is_ephemeral_and_server_stops() -> None:
    server = LauncherControlServer(lambda: None, secret="z" * 43)
    port = server.start()
    assert server.child_environment() == {
        LAUNCH_SECRET_ENV: "z" * 43,
        LAUNCHER_CONTROL_PORT_ENV: str(port),
    }
    server.stop()
    assert server.port is None
    with pytest.raises(OSError):
        _post(port, "z" * 43, "q" * 24)


def test_launcher_control_loopback_check_is_exact() -> None:
    assert _is_loopback("127.0.0.1")
    assert _is_loopback("::1")
    assert _is_loopback("::ffff:127.0.0.1")
    assert not _is_loopback("192.168.1.8")
    assert not _is_loopback("localhost")
