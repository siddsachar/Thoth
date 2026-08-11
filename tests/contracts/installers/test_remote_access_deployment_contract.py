from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml


pytestmark = [pytest.mark.contract, pytest.mark.installer]

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCKERFILE = REPO_ROOT / "deploy" / "docker" / "Dockerfile"
COMPOSE_FILE = REPO_ROOT / "deploy" / "docker" / "compose.yaml"
COMPOSE_BUILD_FILE = REPO_ROOT / "deploy" / "docker" / "compose.build.yaml"
COMPOSE_VPS_FILE = REPO_ROOT / "deploy" / "docker" / "compose.vps.yaml"
COMPOSE_SECRETS_FILE = (
    REPO_ROOT / "deploy" / "docker" / "compose.secrets.yaml.example"
)
DOCKER_README = REPO_ROOT / "deploy" / "docker" / "README.md"
CADDYFILE = REPO_ROOT / "deploy" / "reverse-proxy" / "Caddyfile.example"
SYSTEMD_UNIT = REPO_ROOT / "deploy" / "systemd" / "row-bot.service.example"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class _ComposeLoader(yaml.SafeLoader):
    pass


def _construct_reset(loader: _ComposeLoader, node: yaml.Node) -> object:
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return loader.construct_scalar(node)


_ComposeLoader.add_constructor("!reset", _construct_reset)


def _load_compose(path: Path) -> dict[str, object]:
    return yaml.load(_read(path), Loader=_ComposeLoader)


def test_deployment_artifacts_exist() -> None:
    for path in (
        DOCKERFILE,
        COMPOSE_FILE,
        COMPOSE_BUILD_FILE,
        COMPOSE_VPS_FILE,
        COMPOSE_SECRETS_FILE,
        DOCKER_README,
        CADDYFILE,
        SYSTEMD_UNIT,
        DOCKERIGNORE,
    ):
        assert path.is_file(), path


def test_dockerfile_uses_locked_python_313_build_and_non_root_runtime() -> None:
    source = _read(DOCKERFILE)

    assert "python:3.13." in source
    assert "ghcr.io/astral-sh/uv:" in source
    assert "node:24.4.1-bookworm-slim" in source
    assert ":latest" not in source
    assert "COPY pyproject.toml uv.lock README.md LICENSE ./" in source
    sync_commands = re.findall(r"uv sync [^\n]+", source)
    assert len(sync_commands) == 2
    assert all("--frozen" in command for command in sync_commands)
    assert all("--no-dev" in command for command in sync_commands)
    assert all("--no-editable" in command for command in sync_commands)
    assert all("--extra all" in command for command in sync_commands)
    assert "USER 10001:10001" in source
    assert "ROW_BOT_APP_ROOT=/opt/row-bot/app" in source
    assert "COPY static /opt/row-bot/app/static" in source
    assert 'ROW_BOT_DATA_DIR=/data' in source
    assert 'ROW_BOT_DEPLOYMENT_MODE=server' in source
    assert 'ROW_BOT_CONTAINERIZED=1' in source
    assert 'VOLUME ["/data"]' in source
    assert "EXPOSE 8080" in source
    assert "/healthz" in source
    assert 'ENTRYPOINT ["row-bot"]' in source
    assert 'CMD ["serve"]' in source


def test_dockerfile_has_release_identity_labels_and_build_arguments() -> None:
    source = _read(DOCKERFILE)

    assert "ARG ROW_BOT_VERSION=0.0.0" in source
    assert "ARG ROW_BOT_SOURCE_REVISION=unknown" in source
    assert (
        'org.opencontainers.image.source="https://github.com/siddsachar/row-bot"'
        in source
    )
    assert 'org.opencontainers.image.version="${ROW_BOT_VERSION}"' in source
    assert 'org.opencontainers.image.revision="${ROW_BOT_SOURCE_REVISION}"' in source
    assert 'org.opencontainers.image.title="Row-Bot authenticated server"' in source
    assert "authenticated long-running server mode" in source


def test_dockerfile_bundles_complete_server_runtimes_and_verifies_them() -> None:
    source = _read(DOCKERFILE)

    assert "COPY --from=uv /uv /uvx /usr/local/bin/" in source
    assert "COPY --from=node /usr/local /usr/local" in source
    assert "python -m playwright install chromium" in source
    assert "python -m playwright install-deps chromium" in source
    assert "ln -s \"${chromium_path}\" /usr/local/bin/google-chrome" in source
    assert (
        "COPY --from=builder /opt/row-bot/playwright-browsers "
        "/opt/row-bot/playwright-browsers"
    ) in source
    for package in (
        "ffmpeg",
        "fonts-noto-core",
        "fonts-noto-color-emoji",
        "libgl1",
        "libglib2.0-0",
        "libportaudio2",
    ):
        assert package in source
    assert (
        "python /opt/row-bot/app/scripts/verify_runtime_dependencies.py all"
        in source
    )
    assert "playwright_chromium_revision=" in source
    assert "sha256sum uv.lock" in source


def test_dockerfile_routes_browser_models_and_managed_runtimes_correctly() -> None:
    source = _read(DOCKERFILE)

    expected_environment = {
        "ROW_BOT_BROWSER_HEADLESS": "1",
        "PLAYWRIGHT_BROWSERS_PATH": "/opt/row-bot/playwright-browsers",
        "XDG_CACHE_HOME": "/data/cache",
        "HF_HOME": "/data/cache/huggingface",
        "TORCH_HOME": "/data/cache/torch",
        "SENTENCE_TRANSFORMERS_HOME": "/data/cache/sentence-transformers",
        "UV_CACHE_DIR": "/data/cache/uv",
        "TMPDIR": "/data/tmp",
        "ROW_BOT_SECRETS_DIR": "/run/secrets",
    }
    for name, value in expected_environment.items():
        assert f"{name}={value}" in source
    assert "/data/runtimes" in source
    assert "playwright-browsers /data" not in source


def test_compose_defaults_to_loopback_and_isolated_persistent_state() -> None:
    compose = _load_compose(COMPOSE_FILE)
    service = compose["services"]["row-bot"]
    secret_init = compose["services"]["secret-store-init"]

    assert "build" not in service
    assert service["image"] == "${ROW_BOT_IMAGE:-ghcr.io/siddsachar/row-bot:latest}"
    assert service["user"] == "10001:10001"
    assert service["restart"] == "unless-stopped"
    assert service["read_only"] is True
    assert service["cap_drop"] == ["ALL"]
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert service["ports"] == [
        "${ROW_BOT_BIND_ADDRESS:-127.0.0.1}:${ROW_BOT_HOST_PORT:-8080}:8080"
    ]
    assert "row_bot_data:/data" in service["volumes"]
    secret_mount = next(
        mount
        for mount in service["volumes"]
        if isinstance(mount, dict) and mount.get("target") == "/run/secrets"
    )
    assert secret_mount == {
        "type": "volume",
        "source": "row_bot_secrets",
        "target": "/run/secrets",
        "read_only": True,
        "volume": {"nocopy": True},
    }
    assert {"row_bot_data", "row_bot_secrets"} <= set(compose["volumes"])
    assert service["depends_on"] == {
        "secret-store-init": {"condition": "service_completed_successfully"}
    }
    assert secret_init["image"] == service["image"]
    assert secret_init["user"] == "0:0"
    assert secret_init["network_mode"] == "none"
    assert secret_init["read_only"] is True
    assert secret_init["restart"] == "no"
    assert secret_init["cap_drop"] == ["ALL"]
    assert secret_init["cap_add"] == ["CHOWN"]
    assert secret_init["security_opt"] == ["no-new-privileges:true"]
    assert secret_init["volumes"] == [
        {
            "type": "volume",
            "source": "row_bot_secrets",
            "target": "/run/secrets",
            "volume": {"nocopy": True},
        }
    ]
    assert "initialize_persistent_server_secret_store" in " ".join(
        secret_init["command"]
    )
    assert service["environment"] == {
        "ROW_BOT_DATA_DIR": "/data",
        "ROW_BOT_DEPLOYMENT_MODE": "server",
        "ROW_BOT_HOST": "0.0.0.0",
        "ROW_BOT_PORT": "8080",
        "ROW_BOT_BROWSER_HEADLESS": "1",
        "PLAYWRIGHT_BROWSERS_PATH": "/opt/row-bot/playwright-browsers",
        "XDG_CACHE_HOME": "/data/cache",
        "HF_HOME": "/data/cache/huggingface",
        "TORCH_HOME": "/data/cache/torch",
        "SENTENCE_TRANSFORMERS_HOME": "/data/cache/sentence-transformers",
        "UV_CACHE_DIR": "/data/cache/uv",
        "TMPDIR": "/data/tmp",
        "ROW_BOT_SECRETS_DIR": "/run/secrets",
    }
    assert service["shm_size"] == "256m"
    assert service["tmpfs"] == ["/tmp:rw,noexec,nosuid,nodev,size=256m"]
    assert service["logging"] == {
        "driver": "json-file",
        "options": {"max-size": "10m", "max-file": "5"},
    }
    assert "/healthz" in " ".join(service["healthcheck"]["test"])
    assert "container_name" not in service


def test_compose_does_not_weaken_container_or_mount_host_resources() -> None:
    compose = _load_compose(COMPOSE_FILE)
    service = compose["services"]["row-bot"]
    serialized = _read(COMPOSE_FILE).lower()

    for key in ("privileged", "ipc", "pid", "devices"):
        assert key not in service
    assert service.get("network_mode") != "host"
    assert "/var/run/docker.sock" not in serialized
    assert "/dev/snd" not in serialized
    assert "/dev/video" not in serialized
    assert "/tmp/.x11-unix" not in serialized
    assert "tailscale" not in serialized
    assert "funnel" not in serialized


def test_compose_does_not_embed_credentials_or_invitation_material() -> None:
    compose = _load_compose(COMPOSE_FILE)
    service = compose["services"]["row-bot"]
    environment = service.get("environment", {})

    assert "env_file" not in service
    assert "secrets" not in service
    sensitive_keys = {
        key
        for key in environment
        if re.search(r"(?:TOKEN|PASSWORD|API_KEY|INVIT)", key, re.IGNORECASE)
    }
    assert not sensitive_keys
    assert environment["ROW_BOT_SECRETS_DIR"] == "/run/secrets"
    assert "ROW_BOT_SECRET_STORE_KEY" not in environment
    assert "ROW_BOT_SECRET_STORE_KEY" not in _read(COMPOSE_FILE)


def test_source_build_override_changes_only_build_source_and_image_name() -> None:
    compose = _load_compose(COMPOSE_BUILD_FILE)
    assert set(compose) == {"services"}
    assert set(compose["services"]) == {"secret-store-init", "row-bot"}
    assert compose["services"]["secret-store-init"] == {"image": "row-bot:source-build"}
    assert compose["services"]["row-bot"] == {
        "build": {
            "context": "../..",
            "dockerfile": "deploy/docker/Dockerfile",
        },
        "image": "row-bot:source-build",
    }


def test_vps_override_uses_exact_host_loopback_proxy_contract() -> None:
    source = _read(COMPOSE_VPS_FILE)
    compose = _load_compose(COMPOSE_VPS_FILE)
    service = compose["services"]["row-bot"]

    assert "Linux-only" in source
    assert "Compose 2.24.4" in source
    assert service["network_mode"] == "host"
    assert service["ports"] == []
    assert "ports: !reset []" in source
    assert service["environment"] == {
        "ROW_BOT_HOST": "127.0.0.1",
        "ROW_BOT_PUBLIC_URL": (
            "${ROW_BOT_PUBLIC_URL:?Set ROW_BOT_PUBLIC_URL to the exact public "
            "HTTPS origin}"
        ),
        "ROW_BOT_ALLOWED_HOSTS": (
            "${ROW_BOT_ALLOWED_HOSTS:?Set ROW_BOT_ALLOWED_HOSTS to the exact "
            "public host}"
        ),
        "ROW_BOT_TRUSTED_PROXY_CIDRS": "127.0.0.1/32",
    }


def test_secret_override_mounts_only_the_required_read_only_directory() -> None:
    compose = _load_compose(COMPOSE_SECRETS_FILE)
    service = compose["services"]["row-bot"]

    assert set(service) == {"volumes"}
    assert service["volumes"] == [
        "${ROW_BOT_SECRETS_HOST_DIR:?Set ROW_BOT_SECRETS_HOST_DIR to an "
        "absolute private host directory}:/run/secrets:ro"
    ]
    assert not re.search(
        r"(?:TOKEN|PASSWORD|API_KEY|INVIT|OPENAI|ANTHROPIC)",
        _read(COMPOSE_SECRETS_FILE),
        re.IGNORECASE,
    )


@pytest.mark.parametrize(
    "path",
    (COMPOSE_FILE, COMPOSE_BUILD_FILE, COMPOSE_VPS_FILE, COMPOSE_SECRETS_FILE),
)
def test_compose_surfaces_never_request_privilege_or_host_devices(path: Path) -> None:
    source = _read(path).lower()
    compose = _load_compose(path)
    service = compose["services"]["row-bot"]

    for key in ("privileged", "ipc", "pid", "devices"):
        assert key not in service
    assert "/var/run/docker.sock" not in source
    assert "/dev/snd" not in source
    assert "/dev/video" not in source
    assert "/tmp/.x11-unix" not in source
    assert "tailscale" not in source
    assert "funnel" not in source


def test_dockerignore_excludes_local_state_and_secret_file_patterns() -> None:
    patterns = set(_read(DOCKERIGNORE).splitlines())

    assert {".git", ".venv", ".local", ".tmp", ".testtmp", "tests"} <= patterns
    assert {"*.db", "*.db-*", ".env", ".env.*", "*.pem", "*.key"} <= patterns
    assert "**/.row-bot" in patterns


def test_docker_guide_documents_bootstrap_isolation_and_recovery() -> None:
    source = _read(DOCKER_README)

    assert (
        "docker compose -f deploy/docker/compose.yaml exec row-bot \\\n"
        "  row-bot access invite --layout desktop"
    ) in source
    assert (
        "docker compose -f deploy/docker/compose.yaml exec row-bot \\\n"
        "  row-bot access invite --layout compact"
    ) in source
    assert "Both layouts represent the same owner" in source
    assert "ROW_BOT_BIND_ADDRESS=192.168.1.20" in source
    assert "--project-name row-bot-main" in source
    assert "--project-name row-bot-lab" in source
    assert "ROW_BOT_HOST_PORT=8081" in source
    assert "instance identity" in source
    assert "cookie names and sessions" in source
    assert "Back up, restore, and upgrade" in source
    assert "single-owner, multi-device" in source
    assert re.search(
        r"not\s+a multi-user or hostile multi-tenant isolation boundary",
        source,
    )


def test_docker_guide_documents_complete_image_and_explicit_actions() -> None:
    source = _read(DOCKER_README)
    normalized = re.sub(r"\s+", " ", source)

    for phrase in (
        "complete supported Row-Bot server feature set",
        "no Python extra installation is required",
        "Browser-local voice",
        "secure HTTPS context",
        "OpenAI Realtime voice",
        "CPU-only baseline",
        "/data/cache/huggingface",
        "/data/runtimes",
        "uv`/`uvx",
        "node`/`npm`/`npx",
        "general runtimes, not preinstalled MCP servers",
        "installed but unconfigured",
        "ROW_BOT_SECRETS_DIR",
        "/run/secrets:ro",
        "read-only in Settings",
        "does **not**",
        "No physical host microphone",
        "native desktop Computer Use",
        "GPU acceleration",
    ):
        assert phrase in normalized


def test_caddy_example_is_a_dedicated_origin_with_explicit_proxy_contract() -> None:
    source = _read(CADDYFILE)

    assert re.search(r"(?m)^row-bot\.example\.com \{$", source)
    assert "ROW_BOT_PUBLIC_URL=https://row-bot.example.com" in source
    assert "ROW_BOT_ALLOWED_HOSTS=row-bot.example.com" in source
    assert "ROW_BOT_TRUSTED_PROXY_CIDRS=127.0.0.1/32" in source
    assert "reverse_proxy 127.0.0.1:8080" in source
    assert "header_up -Forwarded" in source
    assert "header_up -X-Real-IP" in source
    assert "header_up X-Forwarded-Host {host}" in source
    assert "header_up X-Forwarded-Proto {scheme}" in source
    assert "header_up X-Forwarded-For {remote_host}" in source
    assert "header_up X-Forwarded-Port {server_port}" in source
    assert "WebSockets automatically" in source
    assert "flush_interval -1" in source
    assert "stream_close_delay 5m" in source
    assert "health_uri /readyz" in source
    assert "/row-bot {" not in source


def test_systemd_example_is_persistent_restartable_and_hardened() -> None:
    source = _read(SYSTEMD_UNIT)

    assert "User=row-bot" in source
    assert "Group=row-bot" in source
    assert "StateDirectory=row-bot" in source
    assert "StateDirectoryMode=0700" in source
    assert "Environment=ROW_BOT_DATA_DIR=/var/lib/row-bot" in source
    assert "Environment=ROW_BOT_DEPLOYMENT_MODE=server" in source
    assert "Environment=ROW_BOT_HOST=127.0.0.1" in source
    assert "ExecStart=/opt/row-bot/.venv/bin/row-bot serve" in source
    assert "Restart=on-failure" in source
    assert "NoNewPrivileges=true" in source
    assert "ProtectSystem=strict" in source
    assert "CapabilityBoundingSet=" in source
    assert "multi-user or hostile multi-tenant" not in source.lower()
