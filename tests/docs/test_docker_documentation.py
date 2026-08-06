from __future__ import annotations

from pathlib import Path

import yaml


PUBLIC_GUIDE = Path("docs-site/docs/operations/docker.mdx")
REMOTE_GUIDE = Path("docs-site/docs/operations/remote-access.mdx")
FIRST_LAUNCH_GUIDE = Path("docs-site/docs/getting-started/first-launch.mdx")
DOCUMENTS_GUIDE = Path("docs-site/docs/settings/documents.mdx")
OPERATOR_GUIDE = Path("deploy/docker/README.md")
ROOT_README = Path("README.md")
SIDEBAR = Path("docs-site/sidebars.ts")
GUIDE_METADATA = Path("docs-content/metadata/how_to_guides.yml")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_public_docker_guide_is_discoverable_from_docs_and_readmes() -> None:
    assert PUBLIC_GUIDE.is_file()
    assert "'operations/docker'" in _read(SIDEBAR)
    assert "/docs/operations/docker" in _read(REMOTE_GUIDE)
    assert "https://row-bot.ai/docs/operations/docker" in _read(ROOT_README)
    assert "https://row-bot.ai/docs/operations/docker" in _read(OPERATOR_GUIDE)

    metadata = yaml.safe_load(_read(GUIDE_METADATA))["guides"]["operate-docker-vps"]
    assert metadata["route"] == "/docs/operations/docker"
    assert {
        "deploy/docker/**",
        "src/row_bot/access/",
        "src/row_bot/runtime_paths.py",
        "src/row_bot/secret_store.py",
        "src/row_bot/developer/",
        "scripts/smoke_docker_server.py",
    } <= set(metadata["sources"])


def test_public_guide_has_pull_first_start_health_and_invitation_commands() -> None:
    source = _read(PUBLIC_GUIDE)

    for phrase in (
        "ROW_BOT_VERSION=X.Y.Z",
        "raw.githubusercontent.com/siddsachar/row-bot/v${ROW_BOT_VERSION}",
        "ghcr.io/siddsachar/row-bot:${ROW_BOT_VERSION}",
        'docker buildx imagetools inspect "${ROW_BOT_IMAGE}"',
        "docker compose -f compose.yaml up -d",
        "http://127.0.0.1:8080/healthz",
        "http://127.0.0.1:8080/readyz",
        "row-bot access invite --layout desktop",
        "--origin http://127.0.0.1:8080",
        "ghcr.io/siddsachar/row-bot@sha256:RELEASE_MANIFEST_DIGEST",
    ):
        assert phrase in source
    first_start = source.index("## First Start And Owner Invitation")
    source_build = source.index("## Build From Source")
    assert first_start < source_build
    assert "--build" not in source[first_start:source_build]


def test_public_guide_covers_lifecycle_backup_restore_and_deliberate_removal() -> None:
    source = _read(PUBLIC_GUIDE)

    for phrase in (
        "docker compose -f compose.yaml stop row-bot",
        "docker compose -f compose.yaml start row-bot",
        "--force-recreate",
        "docker compose -f compose.yaml cp row-bot:/data/.",
        "--project-name row-bot-restore",
        "docker compose -f compose.yaml pull row-bot",
        "matching pre-upgrade data backup",
        "docker compose -f compose.yaml down",
        "docker compose -f compose.yaml down --volumes",
        "not recoverable unless you have a tested backup",
    ):
        assert phrase in source


def test_public_guide_covers_secrets_source_build_and_vps_topology() -> None:
    source = _read(PUBLIC_GUIDE)

    for phrase in (
        "compose.secrets.yaml.example",
        "ROW_BOT_SECRETS_HOST_DIR=/srv/row-bot/secrets",
        "ROW_BOT_SECRET_STORE_KEY",
        "openssl rand -hex 32",
        "/data/secure-secrets",
        "survive a process restart or container replacement",
        "invalid or changed key fails closed",
        "/run/secrets:ro",
        "compose.build.yaml",
        "compose.vps.yaml",
        "Docker Compose 2.24.4",
        "ROW_BOT_PUBLIC_URL='https://row-bot.example.com'",
        "ROW_BOT_ALLOWED_HOSTS='row-bot.example.com'",
        "ROW_BOT_TRUSTED_PROXY_CIDRS=127.0.0.1/32",
        "inbound TCP 80/443",
        "do not expose 8080",
        "WebSocket remains connected",
        "streamed response",
        "Reboot the VPS",
    ):
        assert phrase in source
    assert source.index("## Before First Start: Preserve Account Credentials") < source.index(
        "## First Start And Owner Invitation"
    )


def test_public_guide_covers_embedding_setup_and_multiple_instances() -> None:
    source = _read(PUBLIC_GUIDE)

    for phrase in (
        "Mixedbread Embed Large v1",
        "checked-by-default 675 MB download",
        "Settings → Documents",
        'Screenshot id="settings-documents"',
        "## Multiple Isolated Instances",
        "--project-name row-bot-main",
        "ROW_BOT_HOST_PORT=8081",
        "its own data volume and external secret directory",
    ):
        assert phrase in source


def test_embedding_download_is_documented_for_every_install_type() -> None:
    first_launch = _read(FIRST_LAUNCH_GUIDE)
    documents = _read(DOCUMENTS_GUIDE)

    for phrase in (
        "desktop, source, and official Docker install",
        "Mixedbread Embed Large v1",
        "checked-by-default 675 MB download",
        "Download model",
        "Retry local load",
        "Repair local model",
        "bounded lexical and graph fallback",
        'Screenshot id="settings-documents"',
    ):
        assert phrase in first_launch
    for phrase in (
        "separate from the chat model",
        "Download model",
        "Retry local load",
        "Repair local model",
        "cloud embedding provider is opt-in",
    ):
        assert phrase in documents


def test_operator_guide_separates_container_verification_from_publication() -> None:
    source = _read(OPERATOR_GUIDE)

    for phrase in (
        "Pull requests that touch container inputs",
        "workflow_dispatch",
        "does not publish",
        "Publishing a GitHub Release",
        "Stable releases also update",
        "confirm the GHCR package is public",
        "docker logout ghcr.io",
        "--platform linux/amd64",
        "--platform linux/arm64",
        "manifest digest",
    ):
        assert phrase in source
    assert "ghcr.io/siddsachar/row-bot:4.5.0" not in source


def test_public_guide_separates_host_tailscale_and_session_renewal() -> None:
    source = _read(PUBLIC_GUIDE)

    for phrase in (
        "Tailscale runs on the Linux host, not in the Row-Bot container",
        "tailscale serve --bg http://127.0.0.1:8080",
        "neither contains nor controls the host Tailscale CLI",
        "lasts up to 30 days",
        "every 12 hours",
        "final seven days",
        "Temporary 12-hour sessions",
        "migrated legacy sessions never renew",
        "inactive trusted browser can still expire after 30 days",
    ):
        assert phrase in source


def test_public_guide_documents_all_three_developer_execution_cases() -> None:
    source = _read(PUBLIC_GUIDE)

    for phrase in (
        "Row-Bot installed on a host",
        "The official Row-Bot application container",
        "Developer Docker Sandbox is unavailable and fails closed",
        "must not mount the host Docker socket",
        "An approved Custom Tool inside the application container",
        "deliberately uses Local mode inside that same container",
        "not a silent fallback from a requested Docker workspace",
        "cache growth depends on what the owner installs and uses",
    ):
        assert phrase in source


def test_public_guide_does_not_make_unimplemented_release_or_service_claims() -> None:
    source = _read(PUBLIC_GUIDE).lower()

    for prohibited_claim in (
        "supports multiple users",
        "highly available",
        "automatically backs up",
        "automatically updates",
        "signed container image",
        "includes an sbom",
        "slsa provenance",
        "certified vulnerability-free",
    ):
        assert prohibited_claim not in source


def test_remote_guide_states_active_renewal_without_duplicating_runbook() -> None:
    source = _read(REMOTE_GUIDE)

    assert "every 12 hours" in source
    assert "final seven days" in source
    assert "inactive trusted browser can still expire after 30 days" in source
    assert "Temporary 12-hour sessions" in source
    assert "[Docker And VPS Operations](/docs/operations/docker)" in source
