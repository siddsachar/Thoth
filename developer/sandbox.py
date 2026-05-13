from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from developer.state import ApprovalMode


DeveloperAction = Literal[
    "read",
    "edit",
    "run_safe_command",
    "run_install",
    "run_network",
    "start_server",
    "delete",
    "git_branch",
    "git_worktree",
    "git_commit",
    "git_push",
    "git_pr",
]

PolicyDecision = Literal["allow", "ask", "block"]


@dataclass(frozen=True)
class ApprovalDecision:
    decision: PolicyDecision
    reason: str

    @property
    def allowed(self) -> bool:
        return self.decision == "allow"

    @property
    def requires_approval(self) -> bool:
        return self.decision == "ask"


_ACTION_LABELS = {
    "read": "read workspace files",
    "edit": "edit workspace files",
    "run_safe_command": "run a local command",
    "run_install": "install dependencies",
    "run_network": "use the network",
    "start_server": "start a local server",
    "delete": "delete files",
    "git_branch": "create or switch branches",
    "git_worktree": "create a Git worktree",
    "git_commit": "create a commit",
    "git_push": "push to a remote",
    "git_pr": "open a pull request",
}


def decide_action(mode: ApprovalMode, action: DeveloperAction) -> ApprovalDecision:
    """Return the policy decision for a Developer action.

    This is intentionally conservative.  The UI can still perform explicit
    user-triggered actions, but agent-driven actions must pass through this
    function so the same policy holds everywhere.
    """
    label = _ACTION_LABELS[action]

    if mode == "read_only":
        if action == "read":
            return ApprovalDecision("allow", f"Read Only allows {label}.")
        return ApprovalDecision("block", f"Read Only blocks attempts to {label}.")

    if mode == "ask":
        if action == "read" or action == "run_safe_command":
            return ApprovalDecision("allow", f"Ask Before Changes allows {label}.")
        return ApprovalDecision("ask", f"Ask Before Changes requires approval to {label}.")

    if mode == "auto_edit":
        if action in {"read", "edit", "run_safe_command", "git_branch", "git_worktree"}:
            return ApprovalDecision("allow", f"Auto Edit allows {label} inside the workspace.")
        return ApprovalDecision("ask", f"Auto Edit requires approval to {label}.")

    if mode == "agent_run":
        if action in {
            "read",
            "edit",
            "run_safe_command",
            "start_server",
            "git_branch",
            "git_worktree",
        }:
            return ApprovalDecision("allow", f"Agent Run allows {label} inside the workspace.")
        return ApprovalDecision("ask", f"Agent Run requires approval to {label}.")

    return ApprovalDecision("ask", f"Unknown mode requires approval to {label}.")


def action_needs_explicit_user_intent(action: DeveloperAction) -> bool:
    """Actions that should never be hidden behind a generic approval."""
    return action in {"delete", "run_install", "run_network", "git_commit", "git_push", "git_pr"}
