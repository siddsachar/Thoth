"""Run the authenticated server smoke flow against an existing Docker image.

This script is intentionally dependency-free. It never builds or pulls an image and
owns only one cryptographically named container and volume, both protected by an
ownership label that is rechecked before removal.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import time
from typing import Callable, Mapping, Protocol, Sequence
from urllib import error, request
from urllib.parse import parse_qs, urlsplit


SMOKE_LABEL = "io.row-bot.docker-server-smoke"
KIND_LABEL = "io.row-bot.docker-server-smoke.kind"
DEFAULT_STARTUP_TIMEOUT = 120.0
STOP_GRACE_SECONDS = 45


class SmokeError(RuntimeError):
    """Raised when an isolated Docker smoke assertion fails."""


@dataclass(frozen=True, slots=True)
class CommandResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""


class Runner(Protocol):
    def run(
        self,
        args: Sequence[str],
        *,
        check: bool = True,
        timeout: float | None = None,
        sensitive_output: bool = False,
        input_text: str | None = None,
    ) -> CommandResult: ...


class SubprocessRunner:
    """Execute explicit Docker argument lists without a command shell."""

    def run(
        self,
        args: Sequence[str],
        *,
        check: bool = True,
        timeout: float | None = None,
        sensitive_output: bool = False,
        input_text: str | None = None,
    ) -> CommandResult:
        command = tuple(str(part) for part in args)
        completed = subprocess.run(
            list(command),
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            input=input_text,
        )
        result = CommandResult(
            args=command,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
        if check and result.returncode != 0:
            detail = "output redacted" if sensitive_output else _safe_error(result)
            raise SmokeError(
                f"Docker command failed with exit {result.returncode}: {detail}"
            )
        return result


def _safe_error(result: CommandResult) -> str:
    detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
    return detail[:500]


@dataclass(frozen=True, slots=True)
class HttpResult:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def header(self, name: str) -> str:
        target = name.casefold()
        return next(
            (value for key, value in self.headers if key.casefold() == target),
            "",
        )

    def all_headers(self, name: str) -> tuple[str, ...]:
        target = name.casefold()
        return tuple(value for key, value in self.headers if key.casefold() == target)

    def json(self) -> object:
        try:
            return json.loads(self.text)
        except json.JSONDecodeError as exc:
            raise SmokeError("Server returned invalid JSON during Docker smoke") from exc


class HttpTransport(Protocol):
    def send(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json_body: Mapping[str, object] | None = None,
    ) -> HttpResult: ...

    def cookie_values(self) -> tuple[str, ...]: ...


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


class UrllibHttpTransport:
    """Small cookie-aware HTTP adapter with redirects exposed to assertions."""

    def __init__(self) -> None:
        self._cookies = http.cookiejar.CookieJar()
        self._opener = request.build_opener(
            request.HTTPCookieProcessor(self._cookies),
            _NoRedirect(),
        )

    def send(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json_body: Mapping[str, object] | None = None,
    ) -> HttpResult:
        data = None
        request_headers = dict(headers or {})
        if json_body is not None:
            data = json.dumps(json_body, separators=(",", ":")).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
            request_headers.setdefault("Accept", "application/json")
        outbound = request.Request(
            url,
            data=data,
            headers=request_headers,
            method=method.upper(),
        )
        try:
            response = self._opener.open(outbound, timeout=5)
        except error.HTTPError as exc:
            response = exc
        except (error.URLError, TimeoutError, OSError) as exc:
            raise SmokeError("Docker server HTTP request did not complete") from exc
        with response:
            return HttpResult(
                status=int(response.status),
                headers=tuple(response.headers.items()),
                body=response.read(),
            )

    def cookie_values(self) -> tuple[str, ...]:
        return tuple(cookie.value for cookie in self._cookies)


def container_run_args(
    *,
    image: str,
    container_name: str,
    volume_name: str,
    ownership: str,
    secrets_directory: str,
) -> tuple[str, ...]:
    """Return the exact isolated run command used for every recreation."""

    environment = (
        "ROW_BOT_CONTAINERIZED=1",
        "ROW_BOT_DATA_DIR=/data",
        "ROW_BOT_DEPLOYMENT_MODE=server",
        "ROW_BOT_HOST=0.0.0.0",
        "ROW_BOT_PORT=8080",
        "ROW_BOT_BROWSER_HEADLESS=1",
        "PLAYWRIGHT_BROWSERS_PATH=/opt/row-bot/playwright-browsers",
        "XDG_CACHE_HOME=/data/cache",
        "HF_HOME=/data/cache/huggingface",
        "TORCH_HOME=/data/cache/torch",
        "SENTENCE_TRANSFORMERS_HOME=/data/cache/sentence-transformers",
        "UV_CACHE_DIR=/data/cache/uv",
        "TMPDIR=/data/tmp",
        "ROW_BOT_SECRETS_DIR=/run/secrets",
    )
    command = [
        "docker",
        "run",
        "--detach",
        "--name",
        container_name,
        "--label",
        f"{SMOKE_LABEL}={ownership}",
        "--label",
        f"{KIND_LABEL}=container",
        "--init",
        "--restart",
        "unless-stopped",
        "--user",
        "10001:10001",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=256m",
        "--shm-size",
        "256m",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--publish",
        "127.0.0.1::8080",
        "--mount",
        f"type=volume,src={volume_name},dst=/data",
        "--mount",
        f"type=bind,src={secrets_directory},dst=/run/secrets,readonly",
        "--stop-timeout",
        str(STOP_GRACE_SECONDS),
    ]
    for item in environment:
        command.extend(("--env", item))
    command.append(image)
    return tuple(command)


def parse_docker_port(output: str) -> int:
    """Parse one random IPv4 loopback mapping reported by ``docker port``."""

    matches = re.findall(r"(?m)^127\.0\.0\.1:(\d+)\s*$", output.strip())
    if len(matches) != 1:
        raise SmokeError("Docker did not report one IPv4 loopback port mapping")
    port = int(matches[0])
    if not 1 <= port <= 65535:
        raise SmokeError("Docker reported an invalid loopback port")
    return port


def assert_secrets_absent(text: str, secret_values: Sequence[str]) -> None:
    """Reject credential disclosure without echoing the credential in an error."""

    if any(value and value in text for value in secret_values):
        raise SmokeError("A raw invitation or session credential appeared in output")


class DockerServerSmoke:
    def __init__(
        self,
        *,
        image: str,
        startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
        runner: Runner | None = None,
        http: HttpTransport | None = None,
        suffix: str | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        selected_image = image.strip()
        if not selected_image:
            raise ValueError("--image must not be empty")
        if startup_timeout <= 0:
            raise ValueError("--startup-timeout must be positive")
        self.image = selected_image
        self.startup_timeout = startup_timeout
        self.runner = runner or SubprocessRunner()
        self.http = http or UrllibHttpTransport()
        self.suffix = suffix or secrets.token_hex(8)
        if not re.fullmatch(r"[a-z0-9]{8,64}", self.suffix):
            raise ValueError("smoke suffix must be 8-64 lowercase letters or digits")
        self.container_name = f"row-bot-smoke-{self.suffix}"
        self.volume_name = f"row-bot-smoke-data-{self.suffix}"
        self._monotonic = monotonic
        self._sleep = sleep
        self._logs: list[str] = []
        self._secrets: list[str] = []
        self._secrets_directory = ""

    def _inspect_exists(self, resource: str, name: str) -> bool:
        result = self.runner.run(
            ("docker", resource, "inspect", name),
            check=False,
        )
        return result.returncode == 0

    def _label(self, resource: str, name: str, label: str) -> str:
        template = (
            f'{{{{ index .Config.Labels "{label}" }}}}'
            if resource == "container"
            else f'{{{{ index .Labels "{label}" }}}}'
        )
        result = self.runner.run(
            ("docker", resource, "inspect", "--format", template, name)
        )
        return result.stdout.strip()

    def _require_owned(self, resource: str, name: str) -> None:
        if self._label(resource, name, SMOKE_LABEL) != self.suffix:
            raise SmokeError(f"Refusing to remove an unowned Docker {resource}")

    def _reject_collisions(self) -> None:
        if self._inspect_exists("container", self.container_name):
            raise SmokeError("Generated Docker container name already exists")
        if self._inspect_exists("volume", self.volume_name):
            raise SmokeError("Generated Docker volume name already exists")

    def _create_volume(self) -> None:
        result = self.runner.run(
            (
                "docker",
                "volume",
                "create",
                "--label",
                f"{SMOKE_LABEL}={self.suffix}",
                "--label",
                f"{KIND_LABEL}=volume",
                self.volume_name,
            )
        )
        if result.stdout.strip() != self.volume_name:
            raise SmokeError("Docker did not create the exact requested volume")

    def _create_container(self) -> None:
        if not self._secrets_directory:
            raise SmokeError("Docker smoke secret directory is not initialized")
        self.runner.run(
            container_run_args(
                image=self.image,
                container_name=self.container_name,
                volume_name=self.volume_name,
                ownership=self.suffix,
                secrets_directory=self._secrets_directory,
            )
        )

    def _prepare_secret_directory(self, directory: str) -> None:
        path = Path(directory)
        os.chmod(path, 0o755)
        master_key = secrets.token_hex(32)
        key_file = path / "ROW_BOT_SECRET_STORE_KEY"
        key_file.write_text(master_key + "\n", encoding="ascii")
        os.chmod(key_file, 0o444)
        self._secrets.append(master_key)
        self._secrets_directory = str(path.resolve())

    def _capture_logs(self) -> None:
        if not self._inspect_exists("container", self.container_name):
            return
        result = self.runner.run(
            ("docker", "logs", self.container_name),
            check=False,
        )
        self._logs.extend((result.stdout, result.stderr))

    def _remove_container(self, *, force: bool = False) -> None:
        if not self._inspect_exists("container", self.container_name):
            return
        self._require_owned("container", self.container_name)
        command = ["docker", "container", "rm"]
        if force:
            command.append("--force")
        command.append(self.container_name)
        self.runner.run(tuple(command))

    def cleanup(self) -> None:
        """Remove only exact owned resources after rechecking their labels."""

        self._capture_logs()
        self._remove_container(force=True)
        if self._inspect_exists("volume", self.volume_name):
            self._require_owned("volume", self.volume_name)
            self.runner.run(("docker", "volume", "rm", self.volume_name))

    def _origin(self) -> str:
        result = self.runner.run(
            ("docker", "port", self.container_name, "8080/tcp")
        )
        return f"http://127.0.0.1:{parse_docker_port(result.stdout)}"

    def _wait_ready(self, origin: str) -> None:
        deadline = self._monotonic() + self.startup_timeout
        last_statuses: tuple[int | None, int | None] = (None, None)
        while self._monotonic() < deadline:
            statuses: list[int | None] = []
            for path in ("/healthz", "/readyz"):
                try:
                    statuses.append(self.http.send("GET", origin + path).status)
                except SmokeError:
                    statuses.append(None)
            last_statuses = (statuses[0], statuses[1])
            if last_statuses == (200, 200):
                return
            self._sleep(0.5)
        raise SmokeError(
            "Docker server did not become healthy and ready before the startup timeout "
            f"(last statuses: {last_statuses})"
        )

    def _exec_runtime_contract(self) -> None:
        probe = """
import os
from pathlib import Path
assert (os.getuid(), os.getgid()) == (10001, 10001)
Path('/data/.row-bot-docker-smoke').write_text('ok', encoding='utf-8')
try:
    Path('/opt/row-bot/.row-bot-docker-smoke').write_text('must fail')
except OSError:
    pass
else:
    raise AssertionError('/opt unexpectedly writable')
""".strip()
        self.runner.run(
            ("docker", "exec", self.container_name, "python", "-c", probe)
        )

    def _exec_json(self, *args: str, sensitive_output: bool = False) -> object:
        result = self.runner.run(
            ("docker", "exec", self.container_name, *args),
            sensitive_output=sensitive_output,
        )
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise SmokeError("Container command returned invalid JSON") from exc

    def _assert_session(self, origin: str, expected_session_id: str) -> None:
        response = self.http.send("GET", origin + "/api/access/session")
        if response.status != 200:
            raise SmokeError("Authenticated session endpoint did not return HTTP 200")
        assert_secrets_absent(response.text, self._secrets)
        payload = response.json()
        if not isinstance(payload, dict) or not payload.get("authenticated"):
            raise SmokeError("Persisted owner session is not authenticated")
        if payload.get("session_id") != expected_session_id:
            raise SmokeError("Authenticated session identity changed across recreation")
        root = self.http.send("GET", origin + "/")
        if root.status != 200:
            raise SmokeError("Authenticated owner UI did not return HTTP 200")
        assert_secrets_absent(root.text, self._secrets)

    def _stop_start(self, origin: str, session_id: str) -> str:
        self.runner.run(
            ("docker", "stop", "--time", str(STOP_GRACE_SECONDS), self.container_name),
            timeout=STOP_GRACE_SECONDS + 10,
        )
        self.runner.run(("docker", "start", self.container_name))
        restarted_origin = self._origin()
        self._wait_ready(restarted_origin)
        self._assert_session(restarted_origin, session_id)
        return restarted_origin

    def _recreate(self, session_id: str) -> str:
        self.runner.run(
            ("docker", "stop", "--time", str(STOP_GRACE_SECONDS), self.container_name),
            timeout=STOP_GRACE_SECONDS + 10,
        )
        self._capture_logs()
        self._remove_container()
        self._create_container()
        origin = self._origin()
        self._wait_ready(origin)
        self._assert_session(origin, session_id)
        return origin

    def _move_expiry_into_renewal_window(self, session_id: str) -> None:
        statement = """
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
connection = sqlite3.connect('/data/mobile.db')
expiry = (datetime.now(timezone.utc) + timedelta(days=6)).isoformat()
cursor = connection.execute(
    'UPDATE access_sessions SET expires_at = ? WHERE id = ? AND revoked_at IS NULL',
    (expiry, sys.argv[1]),
)
connection.commit()
assert cursor.rowcount == 1
""".strip()
        self.runner.run(
            (
                "docker",
                "exec",
                self.container_name,
                "python",
                "-c",
                statement,
                session_id,
            )
        )

    def _write_persistent_provider_secret(self) -> tuple[str, str]:
        value = "fake-smoke-oauth-" + secrets.token_urlsafe(24)
        self._secrets.append(value)
        statement = """
import sys
from row_bot.providers.auth_store import set_provider_secret
value = sys.stdin.read()
assert value
set_provider_secret('codex', 'refresh_token', value, source='oauth_device')
""".strip()
        self.runner.run(
            (
                "docker",
                "exec",
                "--interactive",
                self.container_name,
                "python",
                "-c",
                statement,
            ),
            sensitive_output=True,
            input_text=value,
        )
        return value, hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _assert_persistent_provider_secret(self, expected_digest: str) -> None:
        statement = """
import hashlib
import json
from row_bot.providers.auth_store import get_provider_secret
value = get_provider_secret('codex', 'refresh_token')
print(json.dumps({'configured': bool(value), 'digest': hashlib.sha256(value.encode('utf-8')).hexdigest() if value else ''}))
""".strip()
        payload = self._exec_json("python", "-c", statement)
        if not isinstance(payload, dict) or payload.get("configured") is not True:
            raise SmokeError("Encrypted provider secret did not survive container recreation")
        if payload.get("digest") != expected_digest:
            raise SmokeError("Encrypted provider secret changed across container recreation")

    def _refresh(self, origin: str, session_id: str) -> None:
        self._move_expiry_into_renewal_window(session_id)
        response = self.http.send(
            "POST",
            origin + "/api/access/session/refresh",
            headers={"Origin": origin, "Accept": "application/json"},
            json_body={},
        )
        if response.status != 200:
            raise SmokeError("Trusted-session refresh did not return HTTP 200")
        assert_secrets_absent(response.text, self._secrets)
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("renewed") is not True:
            raise SmokeError("Trusted session was not renewed inside its renewal window")
        if payload.get("lifetime") != "trusted" or not payload.get("expires_at"):
            raise SmokeError("Trusted-session refresh response is incomplete")
        if not response.all_headers("Set-Cookie"):
            raise SmokeError("Trusted-session refresh did not renew the browser cookie")

    def _verify_no_log_leak(self) -> None:
        self._capture_logs()
        assert_secrets_absent("\n".join(self._logs), self._secrets)

    def _verify_graceful_stop(self) -> None:
        started = self._monotonic()
        self.runner.run(
            ("docker", "stop", "--time", str(STOP_GRACE_SECONDS), self.container_name),
            timeout=STOP_GRACE_SECONDS + 10,
        )
        elapsed = self._monotonic() - started
        if elapsed > STOP_GRACE_SECONDS + 5:
            raise SmokeError("Container exceeded the configured graceful-stop window")
        self._capture_logs()
        assert_secrets_absent("\n".join(self._logs), self._secrets)

    def run(self) -> None:
        self._reject_collisions()
        with tempfile.TemporaryDirectory(prefix=f"row-bot-smoke-secrets-{self.suffix}-") as secret_directory:
            self._prepare_secret_directory(secret_directory)
            try:
                self._create_volume()
                self._create_container()
                origin = self._origin()
                self._exec_runtime_contract()
                self._wait_ready(origin)

                neutral = self.http.send("GET", origin + "/")
                if neutral.status != 303 or neutral.header("Location") != "/connect?next=%2F":
                    raise SmokeError("Unauthenticated root did not use the neutral connection flow")

                doctor = self._exec_json(
                    "row-bot",
                    "access",
                    "doctor",
                    "--json",
                    "--host",
                    "127.0.0.1",
                )
                if not isinstance(doctor, dict) or doctor.get("ok") is not True:
                    raise SmokeError("Access doctor did not return a successful JSON report")

                invitation_payload = self._exec_json(
                    "row-bot",
                    "access",
                    "invite",
                    "--json",
                    "--layout",
                    "desktop",
                    "--origin",
                    origin,
                    sensitive_output=True,
                )
                if not isinstance(invitation_payload, dict):
                    raise SmokeError("Invitation command did not return a JSON object")
                invitation = invitation_payload.get("invitation")
                if not isinstance(invitation, dict):
                    raise SmokeError("Invitation command returned an invalid payload")
                invitation_url = str(invitation.get("invitation_url") or "")
                invitation_values = parse_qs(urlsplit(invitation_url).query).get(
                    "invitation", []
                )
                if len(invitation_values) != 1:
                    raise SmokeError("Invitation command did not return exactly one token")
                invitation_token = invitation_values[0]
                self._secrets.append(invitation_token)

                claim = self.http.send(
                    "POST",
                    origin + "/api/access/invitations/claim",
                    headers={"Origin": origin, "Accept": "application/json"},
                    json_body={
                        "invitation": invitation_token,
                        "display_name": "Docker smoke browser",
                    },
                )
                if claim.status != 200:
                    raise SmokeError("Invitation claim did not return HTTP 200")
                assert_secrets_absent(claim.text, self._secrets)
                claim_payload = claim.json()
                if not isinstance(claim_payload, dict) or not claim_payload.get(
                    "authenticated"
                ):
                    raise SmokeError("Invitation claim did not authenticate the owner")
                session = claim_payload.get("session")
                if not isinstance(session, dict) or not session.get("id"):
                    raise SmokeError("Invitation claim omitted the public session identity")
                session_id = str(session["id"])
                cookies = self.http.cookie_values()
                if len(cookies) != 1:
                    raise SmokeError("Invitation claim did not issue exactly one session cookie")
                self._secrets.append(cookies[0])
                assert_secrets_absent(claim.text, self._secrets)

                self._assert_session(origin, session_id)
                _secret, secret_digest = self._write_persistent_provider_secret()
                origin = self._stop_start(origin, session_id)
                origin = self._recreate(session_id)
                self._assert_persistent_provider_secret(secret_digest)
                self._refresh(origin, session_id)
                origin = self._recreate(session_id)
                self._assert_persistent_provider_secret(secret_digest)
                self._verify_no_log_leak()
                self._verify_graceful_stop()
            finally:
                self.cleanup()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Smoke an already-built Row-Bot authenticated server image.",
    )
    parser.add_argument("--image", required=True, help="Existing local image reference")
    parser.add_argument(
        "--startup-timeout",
        type=float,
        default=DEFAULT_STARTUP_TIMEOUT,
        help=f"Seconds to wait for health and readiness (default: {DEFAULT_STARTUP_TIMEOUT:g})",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        DockerServerSmoke(
            image=args.image,
            startup_timeout=args.startup_timeout,
        ).run()
    except (SmokeError, subprocess.SubprocessError, OSError, ValueError) as exc:
        print(f"docker server smoke failed: {exc}", file=sys.stderr)
        return 1
    print(
        "docker server smoke passed: authentication, renewal, encrypted provider "
        "persistence, isolation, and graceful stop verified"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
