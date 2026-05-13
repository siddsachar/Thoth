from __future__ import annotations

import pathlib
import re
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class GitStatus:
    path: str
    is_git: bool = False
    branch: str = ""
    remote: str = ""
    dirty: bool = False
    ahead_behind: str = ""
    error: str = ""


def _run_git(path: pathlib.Path, args: list[str], *, timeout: int = 10) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.stdout.strip()


def get_git_status(path: str) -> GitStatus:
    folder = pathlib.Path(path).expanduser()
    if not folder.exists():
        return GitStatus(path=str(folder), error="Workspace folder does not exist.")
    try:
        inside = _run_git(folder, ["rev-parse", "--is-inside-work-tree"])
        if inside.lower() != "true":
            return GitStatus(path=str(folder), is_git=False)
        branch = _run_git(folder, ["branch", "--show-current"])
        remote = ""
        try:
            remote = _run_git(folder, ["remote", "get-url", "origin"])
        except Exception:
            remote = ""
        dirty = bool(_run_git(folder, ["status", "--porcelain"]))
        ahead_behind = ""
        try:
            ahead_behind = _run_git(folder, ["status", "-sb"]).splitlines()[0]
        except Exception:
            ahead_behind = ""
        return GitStatus(
            path=str(folder),
            is_git=True,
            branch=branch,
            remote=remote,
            dirty=dirty,
            ahead_behind=ahead_behind,
        )
    except Exception as exc:
        return GitStatus(path=str(folder), error=str(exc))


def sanitize_branch_name(name: str) -> str:
    text = str(name or "").strip().replace("\\", "/")
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"[^A-Za-z0-9._/-]+", "-", text)
    text = re.sub(r"/+", "/", text).strip("/.-")
    if not text:
        return "feature"
    if text.endswith(".lock"):
        text = text[:-5]
    return text[:120]


def suggest_feature_branch(base: str = "developer") -> str:
    return f"feat/{sanitize_branch_name(base)}"


def create_branch(path: str, branch_name: str) -> GitStatus:
    folder = pathlib.Path(path).expanduser()
    branch = sanitize_branch_name(branch_name)
    if not branch:
        raise ValueError("Branch name is required.")
    _run_git(folder, ["checkout", "-b", branch])
    return get_git_status(str(folder))


def switch_branch(path: str, branch_name: str) -> GitStatus:
    folder = pathlib.Path(path).expanduser()
    branch = sanitize_branch_name(branch_name)
    if not branch:
        raise ValueError("Branch name is required.")
    _run_git(folder, ["switch", branch])
    return get_git_status(str(folder))


def commit_changes(path: str, message: str, paths: list[str] | None = None) -> GitStatus:
    folder = pathlib.Path(path).expanduser()
    msg = str(message or "").strip()
    if not msg:
        raise ValueError("Commit message is required.")
    add_paths = paths or ["."]
    _run_git(folder, ["add", "--", *add_paths])
    _run_git(folder, ["commit", "-m", msg], timeout=60)
    return get_git_status(str(folder))


def fast_forward_merge(path: str, branch_name: str) -> GitStatus:
    folder = pathlib.Path(path).expanduser()
    branch = sanitize_branch_name(branch_name)
    if not branch:
        raise ValueError("Branch name is required.")
    _run_git(folder, ["merge", "--ff-only", branch], timeout=60)
    return get_git_status(str(folder))


def create_worktree(path: str, parent_folder: str, branch_name: str) -> pathlib.Path:
    source = pathlib.Path(path).expanduser().resolve()
    parent = pathlib.Path(parent_folder).expanduser().resolve()
    if not parent.exists() or not parent.is_dir():
        raise ValueError(f"Worktree parent folder does not exist: {parent_folder}")
    branch = sanitize_branch_name(branch_name)
    if not branch:
        raise ValueError("Branch name is required.")
    target = parent / branch.replace("/", "-")
    if target.exists():
        raise FileExistsError(f"Worktree target already exists: {target}")
    _run_git(source, ["worktree", "add", "-b", branch, str(target)])
    return target
