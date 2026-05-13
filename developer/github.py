from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from developer.executables import resolve_github_cli
from developer.sandbox import ApprovalDecision, decide_action
from developer.state import ApprovalMode


@dataclass(frozen=True)
class GhStatus:
    installed: bool
    authenticated: bool
    version: str = ""
    user: str = ""
    message: str = ""
    path: str = ""


@dataclass(frozen=True)
class GhResult:
    ok: bool
    ran: bool
    stdout: str = ""
    stderr: str = ""
    returncode: int | None = None
    url: str = ""
    decision: ApprovalDecision | None = None


@dataclass(frozen=True)
class PrPreview:
    title: str
    body: str
    branch: str = ""
    changed_files: int = 0


def _run(args: list[str], *, cwd: str | None = None, timeout: int = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def get_gh_status(timeout: int = 6) -> GhStatus:
    """Return local GitHub CLI status without requiring Thoth-specific auth."""
    gh_path = resolve_github_cli()
    if not gh_path:
        return GhStatus(False, False, message="GitHub CLI is not installed.")

    try:
        version_proc = _run([gh_path, "--version"], timeout=timeout)
    except Exception as exc:
        return GhStatus(True, False, message=f"Unable to run gh: {exc}", path=gh_path)

    version_line = (version_proc.stdout or version_proc.stderr).splitlines()
    version = version_line[0].strip() if version_line else "gh"

    try:
        auth_proc = _run([gh_path, "auth", "status", "-h", "github.com"], timeout=timeout)
    except Exception as exc:
        return GhStatus(True, False, version=version, message=f"Unable to check gh auth: {exc}", path=gh_path)

    auth_output = f"{auth_proc.stdout}\n{auth_proc.stderr}"
    authenticated = auth_proc.returncode == 0
    user = ""
    for line in auth_output.splitlines():
        stripped = line.strip()
        if "Logged in to github.com account" in stripped:
            user = stripped.rsplit(" ", 1)[-1].strip("()")
            break

    message = "Authenticated with GitHub CLI." if authenticated else "Run `gh auth login` to connect GitHub."
    return GhStatus(True, authenticated, version=version, user=user, message=message, path=gh_path)


def suggest_pull_request_text(workspace_path: str, *, max_files: int = 20) -> PrPreview:
    root = Path(workspace_path).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Workspace path does not exist: {root}")
    branch_proc = _run(["git", "branch", "--show-current"], cwd=str(root), timeout=8)
    branch = (branch_proc.stdout or "").strip()
    status_proc = _run(["git", "status", "--porcelain"], cwd=str(root), timeout=8)
    changed = []
    for line in (status_proc.stdout or "").splitlines():
        if not line.strip():
            continue
        changed.append(line[3:].strip() or line.strip())
    title_seed = branch.replace("feat/", "").replace("fix/", "").replace("-", " ").strip()
    title = title_seed[:1].upper() + title_seed[1:] if title_seed else "Developer changes"
    shown = changed[:max_files]
    body_lines = [
        "## Summary",
        "",
        "- Update implementation via Thoth Developer Studio.",
        "",
        "## Changed files",
        "",
    ]
    if shown:
        body_lines.extend(f"- `{path}`" for path in shown)
        if len(changed) > len(shown):
            body_lines.append(f"- ...and {len(changed) - len(shown)} more")
    else:
        body_lines.append("- No local file changes detected yet.")
    body_lines.extend(["", "## Tests", "", "- Not run yet."])
    return PrPreview(title=title, body="\n".join(body_lines), branch=branch, changed_files=len(changed))


def push_current_branch(
    workspace_path: str,
    approval_mode: ApprovalMode,
    *,
    confirmed: bool = False,
    timeout: int = 120,
) -> GhResult:
    """Push HEAD to origin after passing Developer approval policy."""
    decision = decide_action(approval_mode, "git_push")
    if decision.requires_approval and confirmed:
        decision = ApprovalDecision("allow", "User explicitly approved this push.")
    if not decision.allowed:
        return GhResult(False, False, stderr=decision.reason, decision=decision)

    root = Path(workspace_path).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Workspace path does not exist: {root}")

    proc = _run(["git", "push", "-u", "origin", "HEAD"], cwd=str(root), timeout=timeout)
    return GhResult(
        ok=proc.returncode == 0,
        ran=True,
        stdout=proc.stdout[-20000:],
        stderr=proc.stderr[-20000:],
        returncode=proc.returncode,
        decision=decision,
    )


def create_pull_request(
    workspace_path: str,
    approval_mode: ApprovalMode,
    *,
    title: str = "",
    body: str = "",
    draft: bool = False,
    confirmed: bool = False,
    timeout: int = 120,
) -> GhResult:
    """Create a pull request through gh CLI after passing approval policy."""
    decision = decide_action(approval_mode, "git_pr")
    if decision.requires_approval and confirmed:
        decision = ApprovalDecision("allow", "User explicitly approved pull request creation.")
    if not decision.allowed:
        return GhResult(False, False, stderr=decision.reason, decision=decision)

    gh_path = resolve_github_cli()
    if not gh_path:
        return GhResult(False, False, stderr="GitHub CLI is not installed.", decision=decision)

    root = Path(workspace_path).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Workspace path does not exist: {root}")

    args = [gh_path, "pr", "create"]
    if title:
        args.extend(["--title", title])
    if body:
        args.extend(["--body", body])
    if not title and not body:
        args.append("--fill")
    if draft:
        args.append("--draft")

    proc = _run(args, cwd=str(root), timeout=timeout)
    output = f"{proc.stdout}\n{proc.stderr}"
    url = ""
    for token in output.split():
        if token.startswith("https://github.com/") and "/pull/" in token:
            url = token.strip()
            break

    return GhResult(
        ok=proc.returncode == 0,
        ran=True,
        stdout=proc.stdout[-20000:],
        stderr=proc.stderr[-20000:],
        returncode=proc.returncode,
        url=url,
        decision=decision,
    )
