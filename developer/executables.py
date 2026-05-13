from __future__ import annotations

import os
import shutil
from pathlib import Path


def resolve_executable(name: str, *, windows_candidates: list[str] | None = None) -> str:
    """Resolve a CLI from PATH, then common Windows installer locations.

    GUI-launched apps on Windows often inherit an old PATH from before a CLI
    installer updated the user/system environment. Checking the standard
    install folders keeps Developer Studio from reporting installed CLIs as
    missing until the next OS/session restart.
    """

    resolved = shutil.which(name)
    if resolved:
        return resolved
    if os.name != "nt":
        return ""

    candidates = list(windows_candidates or [])
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    candidate_roots = {
        "%ProgramFiles%": program_files,
        "%ProgramFiles(x86)%": program_files_x86,
        "%LOCALAPPDATA%": local_app_data,
    }

    for raw in candidates:
        expanded = raw
        for token, value in candidate_roots.items():
            expanded = expanded.replace(token, value)
        path = Path(expanded)
        if path.exists() and path.is_file():
            return str(path)
    return ""


def resolve_docker() -> str:
    return resolve_executable(
        "docker",
        windows_candidates=[
            r"%ProgramFiles%\Docker\Docker\resources\bin\docker.exe",
            r"%ProgramFiles%\Docker\Docker\resources\bin\docker",
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
    )
