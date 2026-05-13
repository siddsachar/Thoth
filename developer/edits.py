from __future__ import annotations

import pathlib
import re
import subprocess

from developer import change_ledger
from developer.change_ledger import ChangeSet, FileChange
from developer.sandbox import ApprovalDecision, decide_action
from developer.state import ApprovalMode
from developer.storage import get_workspace


_DIFF_PATH_RE = re.compile(r"^(?:diff --git a/(.+?) b/(.+)|--- (?:a/)?(.+)|\+\+\+ (?:b/)?(.+))$")


def _workspace_root(workspace_id: str) -> pathlib.Path:
    workspace = get_workspace(workspace_id)
    if workspace is None:
        raise ValueError("No active Developer workspace.")
    root = pathlib.Path(workspace.path).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Workspace folder does not exist: {root}")
    return root


def _validate_relative_path(root: pathlib.Path, rel_path: str) -> pathlib.Path:
    clean = (rel_path or "").strip().replace("\\", "/")
    if not clean or clean == "/dev/null":
        raise ValueError("Empty patch path.")
    if clean.startswith(("a/", "b/")):
        clean = clean[2:]
    if clean.startswith("/") or clean.startswith("../") or "/../" in clean:
        raise ValueError(f"Patch path escapes workspace: {rel_path}")
    target = (root / clean).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Patch path escapes workspace: {rel_path}") from exc
    return target


def paths_from_patch(root: pathlib.Path, patch: str) -> list[str]:
    paths: list[str] = []
    for line in (patch or "").splitlines():
        match = _DIFF_PATH_RE.match(line.strip())
        if not match:
            continue
        for group in match.groups():
            if not group or group == "/dev/null":
                continue
            clean = group.strip()
            if "\t" in clean:
                clean = clean.split("\t", 1)[0]
            if clean not in paths:
                _validate_relative_path(root, clean)
                paths.append(clean[2:] if clean.startswith(("a/", "b/")) else clean)
    if not paths:
        raise ValueError("Patch did not include any workspace file paths.")
    return paths


def preview_patch(workspace_id: str, patch: str) -> str:
    root = _workspace_root(workspace_id)
    paths = paths_from_patch(root, patch)
    return "Patch looks structurally valid for:\n" + "\n".join(f"- {path}" for path in paths)


def apply_patch_to_workspace(
    *,
    workspace_id: str,
    thread_id: str,
    patch: str,
    approval_mode: ApprovalMode,
    summary: str = "",
    confirmed: bool = False,
) -> tuple[ChangeSet | None, ApprovalDecision]:
    decision = decide_action(approval_mode, "edit")
    if decision.decision == "block":
        return None, decision
    if decision.requires_approval and not confirmed:
        return None, decision

    root = _workspace_root(workspace_id)
    paths = paths_from_patch(root, patch)
    before: dict[str, str | None] = {}
    for path in paths:
        target = _validate_relative_path(root, path)
        before[path] = target.read_text(encoding="utf-8", errors="replace") if target.exists() else None

    check = subprocess.run(
        ["git", "apply", "--check", "--whitespace=nowarn", "-"],
        cwd=str(root),
        input=patch,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if check.returncode != 0:
        raise ValueError((check.stderr or check.stdout or "Patch did not apply.").strip())

    applied = subprocess.run(
        ["git", "apply", "--whitespace=nowarn", "-"],
        cwd=str(root),
        input=patch,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if applied.returncode != 0:
        raise ValueError((applied.stderr or applied.stdout or "Patch failed while applying.").strip())

    files: list[FileChange] = []
    for path in paths:
        target = _validate_relative_path(root, path)
        after = target.read_text(encoding="utf-8", errors="replace") if target.exists() else None
        before_text = before[path]
        if before_text is None and after is not None:
            action = "create"
        elif before_text is not None and after is None:
            action = "delete"
        else:
            action = "update"
        files.append(
            FileChange(
                path=path,
                action=action,
                before_hash=change_ledger.text_hash(before_text),
                after_hash=change_ledger.text_hash(after),
                before_text=before_text,
                patch=patch,
            )
        )
    change_set = change_ledger.record_change_set(
        workspace_id=workspace_id,
        thread_id=thread_id,
        summary=summary or "Developer patch",
        files=files,
    )
    return change_set, decision


def write_file_to_workspace(
    *,
    workspace_id: str,
    thread_id: str,
    path: str,
    content: str,
    approval_mode: ApprovalMode,
    summary: str = "",
    confirmed: bool = False,
) -> tuple[ChangeSet | None, ApprovalDecision]:
    decision = decide_action(approval_mode, "edit")
    if decision.decision == "block":
        return None, decision
    if decision.requires_approval and not confirmed:
        return None, decision

    root = _workspace_root(workspace_id)
    target = _validate_relative_path(root, path)
    before_text = target.read_text(encoding="utf-8", errors="replace") if target.exists() else None
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    after_text = target.read_text(encoding="utf-8", errors="replace")
    action = "create" if before_text is None else "update"
    change_set = change_ledger.record_change_set(
        workspace_id=workspace_id,
        thread_id=thread_id,
        summary=summary or f"Write {path}",
        files=[
            FileChange(
                path=path.strip().replace("\\", "/"),
                action=action,
                before_hash=change_ledger.text_hash(before_text),
                after_hash=change_ledger.text_hash(after_text),
                before_text=before_text,
            )
        ],
    )
    return change_set, decision


def revert_change_set(workspace_id: str, change_set_id: str) -> str:
    root = _workspace_root(workspace_id)
    matches = [
        item for item in change_ledger.list_change_sets(workspace_id=workspace_id, include_reverted=False)
        if item.id == change_set_id
    ]
    if not matches:
        raise ValueError(f"Agent change set not found or already reverted: {change_set_id}")
    change_set = matches[0]
    for file_change in change_set.files:
        target = _validate_relative_path(root, file_change.path)
        current = target.read_text(encoding="utf-8", errors="replace") if target.exists() else None
        if change_ledger.text_hash(current) != file_change.after_hash:
            raise ValueError(
                f"Refusing to revert {file_change.path}: file changed after the agent edit."
            )
    for file_change in change_set.files:
        target = _validate_relative_path(root, file_change.path)
        if file_change.before_text is None:
            target.unlink(missing_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(file_change.before_text, encoding="utf-8")
    change_ledger.mark_reverted(change_set.id)
    return f"Reverted {len(change_set.files)} file(s) from change set {change_set.id}."
