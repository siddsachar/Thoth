from __future__ import annotations

import json
import sys

import pytest


pytestmark = [pytest.mark.subsystem, pytest.mark.installer]


def test_smoke_app_skips_live_launch_when_port_is_already_in_use(monkeypatch, tmp_path) -> None:
    import scripts.smoke_app as smoke_app

    monkeypatch.setattr(smoke_app, "_port_open", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        smoke_app.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("Popen should not be called when port is already open"),
    )

    result = smoke_app.run_app_smoke(cwd=tmp_path, port=8123, timeout=0.1)

    assert result.ok is True
    assert result.messages == [("WARN", "port 8123 already in use; skipping live launch")]


def test_smoke_app_main_parses_command_and_returns_status(monkeypatch, capsys) -> None:
    import scripts.smoke_app as smoke_app

    captured = {}

    def fake_run_app_smoke(**kwargs):
        captured.update(kwargs)
        result = smoke_app.SmokeResult(ok=True, port=kwargs["port"])
        result.add("PASS", "fake smoke")
        return result

    monkeypatch.setattr(smoke_app, "run_app_smoke", fake_run_app_smoke)
    monkeypatch.setattr(
        sys,
        "argv",
        ["smoke_app.py", "--port", "8124", "--timeout", "3", "--cwd", ".", "--no-root-check", "--", "python", "app.py"],
    )

    assert smoke_app.main() == 0
    assert captured["port"] == 8124
    assert captured["timeout"] == 3
    assert captured["check_root"] is False
    assert captured["command"] == ["python", "app.py"]
    assert "[PASS] fake smoke" in capsys.readouterr().out


def test_smoke_app_uses_ephemeral_launcher_secret_for_guarded_probes(
    monkeypatch, tmp_path
) -> None:
    import scripts.smoke_app as smoke_app

    captured: dict[str, object] = {"requests": []}

    class FakeProcess:
        pid = 12345
        returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 0

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

    class FakeResponse:
        status = 200

        def __init__(self, body: bytes):
            self._body = body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit=None):
            return self._body

    def fake_popen(*_args, **kwargs):
        captured["env"] = kwargs["env"]
        return FakeProcess()

    def fake_urlopen(request, timeout=None):
        captured["requests"].append(request)
        url = request.full_url if hasattr(request, "full_url") else str(request)
        if url.endswith("/api/launcher-ping"):
            return FakeResponse(b'{"app":"row-bot"}')
        if url.endswith("/api/startup-state"):
            return FakeResponse(json.dumps({"ready": True}).encode())
        return FakeResponse(b"")

    monkeypatch.setattr(smoke_app, "_port_open", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(smoke_app.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(smoke_app.urllib.request, "urlopen", fake_urlopen)

    result = smoke_app.run_app_smoke(
        command=["python", "app.py"],
        cwd=tmp_path,
        port=8125,
        timeout=1,
        wait_startup_ready=True,
        data_dir=tmp_path / "data",
    )

    env = captured["env"]
    assert isinstance(env, dict)
    secret = env[smoke_app.LAUNCH_SECRET_ENV]
    assert isinstance(secret, str)
    assert len(secret) >= 32
    requests = captured["requests"]
    guarded = [
        request
        for request in requests
        if hasattr(request, "full_url")
        and request.full_url.endswith(("/api/launcher-ping", "/api/startup-state"))
    ]
    assert len(guarded) == 2
    assert all(
        request.get_header("Authorization") == f"Bearer {secret}" for request in guarded
    )
    assert result.ok is True
    assert secret not in repr(result.messages)
