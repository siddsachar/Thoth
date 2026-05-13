from __future__ import annotations

import pathlib
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class ChangedFile:
    path: str
    status: str
    additions: int = 0
    deletions: int = 0


@dataclass(frozen=True)
class DiffStats:
    files: int = 0
    additions: int = 0
    deletions: int = 0


def _git(path: pathlib.Path, args: list[str], *, timeout: int = 10) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.stdout


def list_changed_files(workspace_path: str) -> list[ChangedFile]:
    folder = pathlib.Path(workspace_path).expanduser()
    try:
        output = _git(folder, ["status", "--porcelain", "-uall"])
    except Exception:
        return []
    stats = _workspace_numstat(folder)
    rows: list[ChangedFile] = []
    for line in output.splitlines():
        if not line:
            continue
        status = line[:2].strip() or "modified"
        file_path = line[3:].strip()
        if " -> " in file_path:
            file_path = file_path.split(" -> ", 1)[1]
        additions, deletions = stats.get(file_path, (0, 0))
        if additions == 0 and deletions == 0 and status == "??":
            additions = _count_text_lines_if_safe(folder, file_path)
        rows.append(ChangedFile(path=file_path, status=status, additions=additions, deletions=deletions))
    return rows


def get_file_diff(workspace_path: str, file_path: str, *, limit: int = 12_000) -> str:
    folder = pathlib.Path(workspace_path).expanduser()
    clean_file = str(file_path or "").strip()
    if not clean_file:
        return ""
    try:
        diff = _git(folder, ["diff", "--", clean_file], timeout=20)
        if not diff:
            diff = _git(folder, ["diff", "--cached", "--", clean_file], timeout=20)
    except Exception:
        return ""
    if not diff and _is_untracked(folder, clean_file):
        target = (folder / clean_file).resolve()
        try:
            target.relative_to(folder.resolve())
        except ValueError:
            return ""
        if target.is_file() and _looks_text(target):
            try:
                text = target.read_text(encoding="utf-8", errors="replace")
            except Exception:
                return ""
            body = "\n".join(f"+{line}" for line in text.splitlines())
            diff = (
                f"diff --git a/{clean_file} b/{clean_file}\n"
                "new file mode 100644\n"
                "--- /dev/null\n"
                f"+++ b/{clean_file}\n"
                f"@@ -0,0 +1,{len(text.splitlines())} @@\n"
                f"{body}\n"
            )
    if len(diff) > limit:
        return diff[:limit] + "\n...[diff truncated]"
    return diff


def get_file_diff_stats(workspace_path: str, file_path: str) -> tuple[int, int]:
    folder = pathlib.Path(workspace_path).expanduser()
    clean_file = str(file_path or "").strip()
    if not clean_file:
        return 0, 0
    try:
        output = _git(folder, ["diff", "--numstat", "--", clean_file], timeout=20)
        if not output:
            output = _git(folder, ["diff", "--cached", "--numstat", "--", clean_file], timeout=20)
    except Exception:
        output = ""
    additions = 0
    deletions = 0
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        add_raw, del_raw = parts[0], parts[1]
        additions += int(add_raw) if add_raw.isdigit() else 0
        deletions += int(del_raw) if del_raw.isdigit() else 0
    if additions == 0 and deletions == 0 and _is_untracked(folder, clean_file):
        additions = _count_text_lines_if_safe(folder, clean_file)
    return additions, deletions


def get_workspace_diff_stats(workspace_path: str) -> DiffStats:
    changed = list_changed_files(workspace_path)
    return DiffStats(
        files=len(changed),
        additions=sum(item.additions for item in changed),
        deletions=sum(item.deletions for item in changed),
    )


def list_workspace_files(workspace_path: str, *, limit: int = 120) -> list[str]:
    folder = pathlib.Path(workspace_path).expanduser().resolve()
    skip = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", "dist", "build"}
    rows: list[str] = []
    if not folder.is_dir():
        return rows
    for path in sorted(folder.rglob("*"), key=lambda item: item.relative_to(folder).as_posix().lower()):
        rel_parts = set(path.relative_to(folder).parts)
        if rel_parts & skip:
            continue
        if path.is_file():
            rows.append(path.relative_to(folder).as_posix())
            if len(rows) >= limit:
                break
    return rows


def read_file_preview(workspace_path: str, file_path: str, *, max_chars: int = 20_000) -> str:
    folder = pathlib.Path(workspace_path).expanduser().resolve()
    clean = str(file_path or "").strip().replace("\\", "/")
    if not clean:
        return ""
    target = (folder / clean).resolve()
    try:
        target.relative_to(folder)
    except ValueError as exc:
        raise ValueError(f"Path escapes workspace: {file_path}") from exc
    if not target.is_file():
        return "File not found."
    if not _looks_text(target):
        return "Binary or unsupported file preview."
    text = target.read_text(encoding="utf-8", errors="replace")
    if len(text) > max_chars:
        return text[:max_chars] + "\n...[file truncated]"
    return text


def _workspace_numstat(folder: pathlib.Path) -> dict[str, tuple[int, int]]:
    stats: dict[str, tuple[int, int]] = {}
    for args in (["diff", "--numstat"], ["diff", "--cached", "--numstat"]):
        try:
            output = _git(folder, args, timeout=20)
        except Exception:
            continue
        for line in output.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            add_raw, del_raw, path = parts[0], parts[1], parts[2]
            additions = int(add_raw) if add_raw.isdigit() else 0
            deletions = int(del_raw) if del_raw.isdigit() else 0
            prev_additions, prev_deletions = stats.get(path, (0, 0))
            stats[path] = (prev_additions + additions, prev_deletions + deletions)
    return stats


def _count_text_lines_if_safe(folder: pathlib.Path, file_path: str) -> int:
    target = (folder / file_path).resolve()
    try:
        target.relative_to(folder.resolve())
    except ValueError:
        return 0
    if not target.is_file() or not _looks_text(target):
        return 0
    try:
        return len(target.read_text(encoding="utf-8", errors="replace").splitlines())
    except Exception:
        return 0


def _is_untracked(folder: pathlib.Path, file_path: str) -> bool:
    try:
        output = _git(folder, ["status", "--porcelain", "-uall", "--", file_path], timeout=10)
    except Exception:
        return False
    return any(line.startswith("?? ") for line in output.splitlines())


def _looks_text(path: pathlib.Path, *, sniff_bytes: int = 4096) -> bool:
    try:
        data = path.read_bytes()[:sniff_bytes]
    except Exception:
        return False
    return b"\0" not in data
