from __future__ import annotations

import os
import shutil
from pathlib import Path


def _is_windows() -> bool:
    return os.name == "nt"


def resolve_executable(
    name: str,
    *,
    windows_candidates: list[str] | None = None,
    posix_candidates: list[str] | None = None,
) -> str:
    """Resolve a CLI from PATH, then common GUI-launch install locations.

    GUI-launched apps on Windows often inherit an old PATH from before a CLI
    installer updated the user/system environment. Checking the standard
    install folders keeps Developer Studio from reporting installed CLIs as
    missing until the next OS/session restart.

    macOS apps launched from Finder have the same problem: LaunchServices gives
    them a small PATH which often excludes Homebrew and app-bundled CLI paths.
    """

    resolved = shutil.which(name)
    if resolved:
        return resolved

    windows = _is_windows()
    if windows:
        candidates = list(windows_candidates or [])
        candidate_roots = {
            "%ProgramFiles%": os.environ.get("ProgramFiles", r"C:\Program Files"),
            "%ProgramFiles(x86)%": os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            "%LOCALAPPDATA%": os.environ.get("LOCALAPPDATA", ""),
        }
    else:
        candidates = list(posix_candidates or [])
        candidate_roots = {
            "$HOME": os.path.expanduser("~"),
            "~": os.path.expanduser("~"),
        }

    for raw in candidates:
        expanded = raw
        for token, value in candidate_roots.items():
            expanded = expanded.replace(token, value)
        path = Path(expanded)
        if path.exists() and path.is_file():
            return str(path) if windows else expanded
    return ""


def resolve_docker() -> str:
    return resolve_executable(
        "docker",
        windows_candidates=[
            r"%ProgramFiles%\Docker\Docker\resources\bin\docker.exe",
            r"%ProgramFiles%\Docker\Docker\resources\bin\docker",
        ],
        posix_candidates=[
            "/opt/homebrew/bin/docker",
            "/usr/local/bin/docker",
            "/Applications/Docker.app/Contents/Resources/bin/docker",
            "/Applications/Docker.app/Contents/Resources/bin/com.docker.cli",
        ],
    )


def resolve_podman() -> str:
    return resolve_executable(
        "podman",
        windows_candidates=[
            r"%ProgramFiles%\RedHat\Podman\podman.exe",
            r"%ProgramFiles%\RedHat\Podman\podman",
            r"%LOCALAPPDATA%\Programs\podman\podman.exe",
        ],
        posix_candidates=[
            "/opt/homebrew/bin/podman",
            "/usr/local/bin/podman",
            "/usr/bin/podman",
            "/Applications/Podman Desktop.app/Contents/Resources/resources/podman/bin/podman",
        ],
    )


def resolve_github_cli() -> str:
    return resolve_executable(
        "gh",
        windows_candidates=[
            r"%ProgramFiles%\GitHub CLI\gh.exe",
            r"%ProgramFiles(x86)%\GitHub CLI\gh.exe",
            r"%LOCALAPPDATA%\Programs\GitHub CLI\gh.exe",
        ],
        posix_candidates=[
            "/opt/homebrew/bin/gh",
            "/usr/local/bin/gh",
        ],
    )
