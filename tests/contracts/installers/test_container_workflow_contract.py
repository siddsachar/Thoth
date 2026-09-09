from __future__ import annotations

from pathlib import Path

import pytest
import yaml


pytestmark = [pytest.mark.contract, pytest.mark.installer]

WORKFLOW = Path(".github/workflows/container.yml")


def _source() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _workflow() -> dict[str, object]:
    return yaml.load(_source(), Loader=yaml.BaseLoader)


def test_container_workflow_has_only_review_and_release_triggers() -> None:
    workflow = _workflow()
    triggers = workflow["on"]

    assert set(triggers) == {"pull_request", "workflow_dispatch", "release"}
    assert triggers["release"] == {"types": ["published"]}
    assert "push" not in triggers
    paths = set(triggers["pull_request"]["paths"])
    assert {
        ".github/workflows/container.yml",
        ".dockerignore",
        "deploy/docker/**",
        "pyproject.toml",
        "uv.lock",
        "requirements.txt",
        "app.py",
        "static/**",
        "sounds/**",
        "bundled_skills/**",
        "tool_guides/**",
        "src/**",
        "frontend/**",
        "contracts/client-platform/**",
        "scripts/client_build.py",
        "scripts/verify_runtime_dependencies.py",
        "scripts/smoke_docker_server.py",
        "tests/contracts/installers/test_container_workflow_contract.py",
        "tests/contracts/installers/test_remote_access_deployment_contract.py",
    } <= paths


def test_native_matrices_use_official_amd64_and_arm64_runners() -> None:
    jobs = _workflow()["jobs"]
    expected = [
        {
            "runner": "ubuntu-24.04",
            "platform": "linux/amd64",
            "architecture": "amd64",
        },
        {
            "runner": "ubuntu-24.04-arm",
            "platform": "linux/arm64",
            "architecture": "arm64",
        },
    ]

    assert jobs["verify-image"]["strategy"]["matrix"]["include"] == expected
    assert jobs["release-image"]["strategy"]["matrix"]["include"] == expected
    assert "qemu" not in _source().lower()


def test_verification_build_loads_and_smokes_without_registry_write() -> None:
    workflow = _workflow()
    job = workflow["jobs"]["verify-image"]
    steps = job["steps"]
    build = next(step for step in steps if step.get("name") == "Build native verification image")
    smoke = next(step for step in steps if step.get("name") == "Smoke native verification image")

    assert workflow["permissions"] == {"contents": "read"}
    assert "permissions" not in job
    assert job["if"] == "${{ github.event_name != 'release' }}"
    assert build["uses"] == "docker/build-push-action@v6"
    assert build["with"]["load"] == "true"
    assert build["with"]["push"] == "false"
    assert build["with"]["provenance"] == "false"
    assert build["with"]["sbom"] == "false"
    assert "scripts/smoke_docker_server.py" in smoke["run"]
    assert not any("docker/login-action" in step.get("uses", "") for step in steps)
    assert not any("docker push" in step.get("run", "") for step in steps)


def test_release_job_validates_identity_and_smokes_before_login_and_push() -> None:
    workflow = _workflow()
    job = workflow["jobs"]["release-image"]
    steps = job["steps"]
    source = _source()
    names = [step.get("name", step.get("uses", "")) for step in steps]

    assert job["if"] == "${{ github.event_name == 'release' }}"
    assert job["permissions"] == {"contents": "read", "packages": "write"}
    assert names.index("Build native release image") < names.index(
        "Smoke native release image"
    )
    assert names.index("Smoke native release image") < names.index(
        "Log in to GHCR after smoke"
    )
    assert names.index("Log in to GHCR after smoke") < names.index(
        "Push architecture-specific temporary tag"
    )
    assert 'version="${RELEASE_TAG#v}"' in source
    assert '"$version" == v*' in source
    assert "src/row_bot/version.py" in source
    assert "git rev-list -n 1" in source
    assert "git rev-parse HEAD" in source
    assert "ROW_BOT_VERSION=${{ steps.release.outputs.version }}" in source
    assert "ROW_BOT_SOURCE_REVISION=${{ steps.release.outputs.revision }}" in source
    assert "password: ${{ secrets.GITHUB_TOKEN }}" in source
    assert "PAT" not in source


def test_release_build_is_loaded_before_push_and_disables_implicit_attestations() -> None:
    steps = _workflow()["jobs"]["release-image"]["steps"]
    build = next(step for step in steps if step.get("name") == "Build native release image")

    assert build["with"]["load"] == "true"
    assert build["with"]["push"] == "false"
    assert build["with"]["provenance"] == "false"
    assert build["with"]["sbom"] == "false"
    lowered = _source().lower()
    for unsupported in ("cosign", "slsa", "attestation", "id-token: write"):
        assert unsupported not in lowered


def test_manifest_waits_for_all_native_images_and_latest_is_stable_only() -> None:
    job = _workflow()["jobs"]["release-manifest"]
    source = _source()
    steps = job["steps"]
    release = next(step for step in steps if step.get("name") == "Publish release manifest")
    latest = next(step for step in steps if step.get("name") == "Publish stable latest manifest")

    assert job["needs"] == "release-image"
    assert job["if"] == "${{ github.event_name == 'release' }}"
    assert job["permissions"] == {"contents": "read", "packages": "write"}
    assert "-amd64" in release["run"]
    assert "-arm64" in release["run"]
    assert latest["if"] == "${{ github.event.release.prerelease == false }}"
    assert "${IMAGE_NAME}:latest" in latest["run"]
    assert "GITHUB_STEP_SUMMARY" in source
    for phrase in (
        "Version:",
        "Source revision:",
        "Manifest digest:",
        "linux/amd64 digest:",
        "linux/arm64 digest:",
    ):
        assert phrase in source
