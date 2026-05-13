from __future__ import annotations

import importlib
import sys


def _fresh_modules(tmp_path, monkeypatch):
    monkeypatch.setenv("THOTH_DATA_DIR", str(tmp_path / "data"))
    sys.modules.pop("developer.devcontainer", None)
    import developer.devcontainer as devcontainer

    return importlib.reload(devcontainer)


def test_devcontainer_detection_absent(tmp_path, monkeypatch):
    devcontainer = _fresh_modules(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()

    info = devcontainer.detect_devcontainer(str(repo))

    assert info.present is False
    assert "No devcontainer" in info.message


def test_devcontainer_detection_reads_basic_config(tmp_path, monkeypatch):
    devcontainer = _fresh_modules(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    config_dir = repo / ".devcontainer"
    config_dir.mkdir(parents=True)
    (config_dir / "devcontainer.json").write_text(
        '{"name": "Thoth Dev", "image": "mcr.microsoft.com/devcontainers/python:1-3.12"}',
        encoding="utf-8",
    )

    info = devcontainer.detect_devcontainer(str(repo))

    assert info.present is True
    assert info.name == "Thoth Dev"
    assert info.image.startswith("mcr.microsoft.com")


def test_devcontainer_docker_detection_handles_missing_binary(tmp_path, monkeypatch):
    devcontainer = _fresh_modules(tmp_path, monkeypatch)
    monkeypatch.setattr(devcontainer, "resolve_docker", lambda: "")

    assert devcontainer.detect_docker() is False
