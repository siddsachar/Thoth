from __future__ import annotations

import json
import subprocess
from typing import Mapping, Sequence

import pytest

from scripts import smoke_docker_server as smoke


pytestmark = [pytest.mark.subsystem, pytest.mark.installer]


class FakeResourceRunner:
    def __init__(
        self,
        *,
        containers: Mapping[str, str] | None = None,
        volumes: Mapping[str, str] | None = None,
    ) -> None:
        self.containers = dict(containers or {})
        self.volumes = dict(volumes or {})
        self.calls: list[tuple[str, ...]] = []

    def run(
        self,
        args: Sequence[str],
        *,
        check: bool = True,
        timeout: float | None = None,
        sensitive_output: bool = False,
        input_text: str | None = None,
    ) -> smoke.CommandResult:
        del timeout, sensitive_output, input_text
        command = tuple(args)
        self.calls.append(command)
        resource = command[1] if len(command) > 1 else ""
        resources = self.containers if resource in {"container", "logs"} else self.volumes

        if resource in {"container", "volume"} and command[2] == "inspect":
            name = command[-1]
            if name not in resources:
                return smoke.CommandResult(command, 1, stderr="not found")
            if "--format" in command:
                return smoke.CommandResult(command, 0, stdout=resources[name] + "\n")
            return smoke.CommandResult(command, 0, stdout="{}\n")
        if resource == "logs":
            name = command[-1]
            return smoke.CommandResult(
                command,
                0 if name in self.containers else 1,
                stdout="ordinary server log\n",
            )
        if resource == "container" and command[2] == "rm":
            name = command[-1]
            self.containers.pop(name, None)
            return smoke.CommandResult(command, 0, stdout=name + "\n")
        if resource == "volume" and command[2] == "rm":
            name = command[-1]
            self.volumes.pop(name, None)
            return smoke.CommandResult(command, 0, stdout=name + "\n")
        result = smoke.CommandResult(command, 0)
        if check and result.returncode:
            raise AssertionError("unexpected fake command failure")
        return result


class AlwaysUnavailableHttp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def send(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json_body: Mapping[str, object] | None = None,
        timeout: float | None = None,
    ) -> smoke.HttpResult:
        del headers, json_body, timeout
        self.calls.append((method, url))
        return smoke.HttpResult(503, (), b"not ready")

    def cookie_values(self) -> tuple[str, ...]:
        return ()


class SequenceHttp:
    def __init__(self, results):
        self.results = list(results)
        self.calls: list[tuple[str, str, float | None]] = []

    def send(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json_body: Mapping[str, object] | None = None,
        timeout: float | None = None,
    ) -> smoke.HttpResult:
        del headers, json_body
        self.calls.append((method, url, timeout))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def cookie_values(self) -> tuple[str, ...]:
        return ()


def test_container_command_matches_compose_security_and_never_builds_or_pulls() -> None:
    command = smoke.container_run_args(
        image="row-bot:test",
        container_name="row-bot-smoke-deadbeef",
        volume_name="row-bot-smoke-data-deadbeef",
        ownership="deadbeef",
        secrets_directory="C:/isolated/row-bot-smoke-secrets",
    )

    assert command[:3] == ("docker", "run", "--detach")
    assert command[-1] == "row-bot:test"
    assert "build" not in command
    assert "pull" not in command
    for expected in (
        "10001:10001",
        "--read-only",
        "/tmp:rw,noexec,nosuid,nodev,size=256m",
        "256m",
        "ALL",
        "no-new-privileges:true",
        "127.0.0.1::8080",
        "ROW_BOT_CONTAINERIZED=1",
        "ROW_BOT_DATA_DIR=/data",
        "ROW_BOT_DEPLOYMENT_MODE=server",
        "type=bind,src=C:/isolated/row-bot-smoke-secrets,dst=/run/secrets,readonly",
        "45",
    ):
        assert expected in command
    assert not any("docker.sock" in part for part in command)
    assert not any("privileged" in part for part in command)


@pytest.mark.parametrize(
    ("output", "port"),
    (("127.0.0.1:49152\n", 49152), ("127.0.0.1:1", 1)),
)
def test_random_loopback_port_parsing(output: str, port: int) -> None:
    assert smoke.parse_docker_port(output) == port


@pytest.mark.parametrize(
    "output",
    ("", "0.0.0.0:49152", "127.0.0.1:0", "127.0.0.1:1\n127.0.0.1:2"),
)
def test_random_loopback_port_parsing_rejects_ambiguous_or_public_bindings(
    output: str,
) -> None:
    with pytest.raises(smoke.SmokeError):
        smoke.parse_docker_port(output)


def test_subprocess_adapter_uses_no_shell_and_redacts_sensitive_failures(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured.update(kwargs)
        return subprocess.CompletedProcess(args, 7, "raw-token-value", "")

    monkeypatch.setattr(smoke.subprocess, "run", fake_run)

    with pytest.raises(smoke.SmokeError, match="output redacted") as failure:
        smoke.SubprocessRunner().run(
            ("docker", "exec", "owned", "invite"),
            sensitive_output=True,
        )

    assert "raw-token-value" not in str(failure.value)
    assert captured["args"] == ["docker", "exec", "owned", "invite"]
    assert captured["shell"] is False
    assert captured["check"] is False


def test_generated_name_collision_is_refused_without_removal() -> None:
    suffix = "deadbeef"
    name = f"row-bot-smoke-{suffix}"
    runner = FakeResourceRunner(containers={name: "someone-else"})
    subject = smoke.DockerServerSmoke(
        image="row-bot:test",
        runner=runner,
        suffix=suffix,
    )

    with pytest.raises(smoke.SmokeError, match="already exists"):
        subject.run()

    assert not any("rm" in command for command in runner.calls)
    assert runner.containers == {name: "someone-else"}


def test_cleanup_removes_only_exact_owned_container_and_volume() -> None:
    suffix = "deadbeef"
    container = f"row-bot-smoke-{suffix}"
    volume = f"row-bot-smoke-data-{suffix}"
    runner = FakeResourceRunner(
        containers={container: suffix, "unrelated": "different"},
        volumes={volume: suffix, "unrelated-data": "different"},
    )
    subject = smoke.DockerServerSmoke(
        image="row-bot:test",
        runner=runner,
        suffix=suffix,
    )

    subject.cleanup()

    assert runner.containers == {"unrelated": "different"}
    assert runner.volumes == {"unrelated-data": "different"}
    assert ("docker", "container", "rm", "--force", container) in runner.calls
    assert ("docker", "volume", "rm", volume) in runner.calls


def test_cleanup_refuses_an_exact_name_with_the_wrong_ownership_label() -> None:
    suffix = "deadbeef"
    container = f"row-bot-smoke-{suffix}"
    runner = FakeResourceRunner(containers={container: "different-owner"})
    subject = smoke.DockerServerSmoke(
        image="row-bot:test",
        runner=runner,
        suffix=suffix,
    )

    with pytest.raises(smoke.SmokeError, match="unowned"):
        subject.cleanup()

    assert container in runner.containers
    assert not any(command[1:3] == ("container", "rm") for command in runner.calls)


def test_readiness_timeout_uses_fake_http_and_clock_without_docker() -> None:
    http = AlwaysUnavailableHttp()
    ticks = iter((0.0, 0.0, 0.5, 1.0, 1.5))
    sleeps: list[float] = []
    subject = smoke.DockerServerSmoke(
        image="row-bot:test",
        runner=FakeResourceRunner(),
        http=http,
        suffix="deadbeef",
        startup_timeout=1.0,
        monotonic=lambda: next(ticks),
        sleep=sleeps.append,
    )

    with pytest.raises(smoke.SmokeError, match="startup timeout"):
        subject._wait_ready("http://127.0.0.1:49152")

    assert http.calls == [
        ("GET", "http://127.0.0.1:49152/healthz"),
        ("GET", "http://127.0.0.1:49152/readyz"),
        ("GET", "http://127.0.0.1:49152/healthz"),
        ("GET", "http://127.0.0.1:49152/readyz"),
    ]
    assert sleeps == [0.5, 0.5]


def test_readiness_requires_two_consecutive_healthy_samples() -> None:
    http = SequenceHttp([smoke.HttpResult(200, (), b"ok") for _ in range(4)])
    ticks = iter((0.0, 0.0, 0.5))
    sleeps: list[float] = []
    subject = smoke.DockerServerSmoke(
        image="row-bot:test",
        runner=FakeResourceRunner(),
        http=http,
        suffix="deadbeef",
        monotonic=lambda: next(ticks),
        sleep=sleeps.append,
    )

    subject._wait_ready("http://127.0.0.1:49152")

    assert [call[:2] for call in http.calls] == [
        ("GET", "http://127.0.0.1:49152/healthz"),
        ("GET", "http://127.0.0.1:49152/readyz"),
        ("GET", "http://127.0.0.1:49152/healthz"),
        ("GET", "http://127.0.0.1:49152/readyz"),
    ]
    assert sleeps == [0.5]


def test_transient_get_is_retried_with_functional_timeout() -> None:
    http = SequenceHttp([
        smoke.SmokeError("connection reset"),
        smoke.HttpResult(200, (), b"ok"),
    ])
    ticks = iter((0.0, 0.25))
    sleeps: list[float] = []
    subject = smoke.DockerServerSmoke(
        image="row-bot:test",
        request_timeout=17.0,
        runner=FakeResourceRunner(),
        http=http,
        suffix="deadbeef",
        monotonic=lambda: next(ticks),
        sleep=sleeps.append,
    )

    result = subject._send_http(
        "GET",
        "http://127.0.0.1:49152",
        "/api/access/session",
        stage="session check",
        retry_transient=True,
    )

    assert result.status == 200
    assert http.calls == [
        ("GET", "http://127.0.0.1:49152/api/access/session", 17.0),
        ("GET", "http://127.0.0.1:49152/api/access/session", 17.0),
    ]
    assert sleeps == [0.5]


def test_post_transport_failure_is_not_replayed() -> None:
    http = SequenceHttp([smoke.SmokeError("connection reset")])
    subject = smoke.DockerServerSmoke(
        image="row-bot:test",
        runner=FakeResourceRunner(),
        http=http,
        suffix="deadbeef",
    )

    with pytest.raises(smoke.SmokeError, match="invitation claim failed"):
        subject._send_http(
            "POST",
            "http://127.0.0.1:49152",
            "/api/access/invitations/claim",
            stage="invitation claim",
        )

    assert len(http.calls) == 1


def test_transport_failure_reports_method_path_and_exception(monkeypatch) -> None:
    transport = smoke.UrllibHttpTransport(default_timeout=19.0)
    captured: dict[str, float] = {}

    def fail(_request, *, timeout):
        captured["timeout"] = timeout
        raise smoke.error.URLError("runner connection failed")

    monkeypatch.setattr(transport._opener, "open", fail)

    with pytest.raises(
        smoke.SmokeError,
        match=r"HTTP GET /api/access/session did not complete \(URLError\)",
    ):
        transport.send("GET", "http://127.0.0.1:49152/api/access/session?ignored=1")

    assert captured["timeout"] == 19.0


def test_failure_diagnostics_are_bounded_and_redact_smoke_secrets(capsys) -> None:
    suffix = "deadbeef"
    container = f"row-bot-smoke-{suffix}"

    class DiagnosticRunner(FakeResourceRunner):
        def run(self, args, **kwargs):
            command = tuple(args)
            if command[:3] == ("docker", "container", "inspect") and "--format" not in command:
                payload = [{
                    "State": {
                        "Status": "restarting",
                        "Running": True,
                        "Restarting": True,
                        "OOMKilled": False,
                        "Dead": False,
                        "ExitCode": 1,
                    },
                    "RestartCount": 2,
                }]
                return smoke.CommandResult(command, 0, stdout=json.dumps(payload))
            if command[1] == "logs":
                return smoke.CommandResult(
                    command,
                    0,
                    stdout="ordinary log\nsecret=smoke-owned-secret\n",
                )
            return super().run(command, **kwargs)

    runner = DiagnosticRunner(containers={container: suffix})
    subject = smoke.DockerServerSmoke(
        image="row-bot:test",
        runner=runner,
        suffix=suffix,
    )
    subject._secrets.append("smoke-owned-secret")

    subject._emit_failure_diagnostics()

    stderr = capsys.readouterr().err
    assert '"RestartCount":2' in stderr
    assert '"Status":"restarting"' in stderr
    assert "ordinary log" in stderr
    assert "<redacted>" in stderr
    assert "smoke-owned-secret" not in stderr


def test_secret_detection_never_echoes_the_secret() -> None:
    with pytest.raises(smoke.SmokeError) as failure:
        smoke.assert_secrets_absent("prefix super-secret suffix", ("super-secret",))

    assert "super-secret" not in str(failure.value)
