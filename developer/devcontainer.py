from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from developer.executables import resolve_docker

@dataclass(frozen=True)
class DevcontainerInfo:
    present: bool
    path: str = ""
    name: str = ""
    image: str = ""
    dockerfile: str = ""
    docker_available: bool = False
    message: str = ""


def detect_docker(timeout: int = 3) -> bool:
    docker = resolve_docker()
    if not docker:
        return False
    try:
        proc = subprocess.run(
            [docker, "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception:
        return False
    return proc.returncode == 0


def detect_devcontainer(workspace_path: str, *, check_docker: bool = False) -> DevcontainerInfo:
    root = Path(workspace_path).expanduser().resolve()
    config = root / ".devcontainer" / "devcontainer.json"
    docker_available = detect_docker() if check_docker else False
    if not config.exists():
        return DevcontainerInfo(
            present=False,
            docker_available=docker_available,
            message="No devcontainer configuration found.",
        )

    name = ""
    image = ""
    dockerfile = ""
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            name = str(data.get("name") or "")
            image = str(data.get("image") or "")
            dockerfile = str(data.get("dockerFile") or data.get("dockerfile") or "")
    except Exception:
        return DevcontainerInfo(
            present=True,
            path=str(config),
            docker_available=docker_available,
            message="Devcontainer configuration exists but could not be parsed.",
        )

    return DevcontainerInfo(
        present=True,
        path=str(config),
        name=name,
        image=image,
        dockerfile=dockerfile,
        docker_available=docker_available,
        message="Devcontainer configuration detected.",
    )
