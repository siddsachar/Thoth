"""Small, fail-closed policy table for Computer Use."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any


class PolicyOutcome(str, Enum):
    OBSERVATION = "observation"
    ROUTINE = "routine"
    CONSEQUENTIAL = "consequential"
    HANDOFF = "handoff"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class PolicyDecision:
    outcome: PolicyOutcome
    reason: str
    reversible: bool = True


_BLOCKED_APP = re.compile(
    r"(?:row[- ]?bot|terminal|powershell|command prompt|cmd\.exe|console|shell|repl|"
    r"password manager|1password|bitwarden|keepass|keychain access|credential manager|"
    r"lock screen|login window|secure desktop|security settings)",
    re.IGNORECASE,
)
_HANDOFF = re.compile(
    r"(?:password|passcode|recovery code|payment card|credit card|bank credential|"
    r"one[- ]?time|otp|2fa|mfa|captcha|biometric|passkey|uac|user account control|"
    r"accessibility permission|screen recording permission|tcc|legal acceptance)",
    re.IGNORECASE,
)
_CONSEQUENTIAL = re.compile(
    r"(?:send|post|submit|publish|confirm|purchase|pay|transfer|order|book|trade|"
    r"delete|remove|empty trash|overwrite|upload|download|share|invite|grant|revoke|"
    r"permission|install|execute|run|account|security|privacy|network|medical|"
    r"financial|export|transmit|save|quit|exit|sign out|log out|close without saving)",
    re.IGNORECASE,
)
_SECURE_ROLE = re.compile(r"(?:password|secure|credential|otp|captcha)", re.IGNORECASE)
_DANGEROUS_KEYS = frozenset({
    "win", "windows", "meta", "super", "ctrl+alt+delete", "command+space",
    "cmd+space", "alt+f4", "cmd+q", "control+command+q",
})


def is_consequential_label(value: object) -> bool:
    return bool(_CONSEQUENTIAL.search(str(value or "")))


def classify_action(
    action: str,
    *,
    app_name: str = "",
    window_title: str = "",
    role: str = "",
    label: str = "",
    expected_effect: str = "",
    destination: str = "",
    coordinate_only: bool = False,
    foreground: bool = False,
    keys: str = "",
) -> PolicyDecision:
    action = str(action or "").strip().lower()
    surface = " ".join((app_name, window_title)).strip()
    target = " ".join((role, label, expected_effect, destination)).strip()
    if _BLOCKED_APP.search(surface):
        return PolicyDecision(PolicyOutcome.BLOCKED, "This app or protected surface is not available to Computer Use.", False)
    if _HANDOFF.search(" ".join((surface, target))):
        return PolicyDecision(PolicyOutcome.HANDOFF, "Sensitive credentials or a protected system surface require user takeover.", False)
    if action == "type" and (_SECURE_ROLE.search(role) or _HANDOFF.search(label)):
        return PolicyDecision(PolicyOutcome.HANDOFF, "Secure fields must be completed by the user.", False)
    normalized_keys = keys.strip().lower().replace(" ", "")
    if action == "key" and normalized_keys in {item.replace(" ", "") for item in _DANGEROUS_KEYS}:
        return PolicyDecision(PolicyOutcome.BLOCKED, "System or security key chords are blocked.", False)
    if action == "key_sequence" and "calculator" not in surface.casefold():
        return PolicyDecision(
            PolicyOutcome.BLOCKED,
            "The bounded key sequence is available only for a semantic Calculator target.",
            False,
        )
    if action in {"list_apps", "list_windows", "capture", "wait", "stop"}:
        return PolicyDecision(PolicyOutcome.OBSERVATION, "Read-only observation or local lifecycle action.")
    if foreground:
        return PolicyDecision(PolicyOutcome.CONSEQUENTIAL, "Foreground takeover always requires confirmation.")
    if is_consequential_label(target):
        return PolicyDecision(PolicyOutcome.CONSEQUENTIAL, "The target may create an external or hard-to-reverse effect.", False)
    if coordinate_only and action in {"click", "double_click", "right_click", "key"}:
        return PolicyDecision(PolicyOutcome.CONSEQUENTIAL, "An ambiguous coordinate action requires point-of-risk confirmation.")
    if action == "key" and normalized_keys in {"enter", "return"}:
        return PolicyDecision(PolicyOutcome.CONSEQUENTIAL, "Enter may submit the active form or dialog.", False)
    if action in {"launch_app", "focus", "click", "double_click", "right_click", "type", "key", "key_sequence", "scroll", "drag", "menu"}:
        return PolicyDecision(PolicyOutcome.ROUTINE, "Routine action inside the approved task-scoped target.")
    return PolicyDecision(PolicyOutcome.BLOCKED, "Unknown Computer action fails closed.", False)


def approval_payload(
    action: str,
    *,
    app_name: str,
    window_title: str,
    target_label: str,
    expected_effect: str,
    reversible: bool,
    typed_text: str | None = None,
    preview_ref: str = "",
) -> dict[str, Any]:
    """Build the serializable approval shape without secrets or screenshots."""

    payload: dict[str, Any] = {
        "tool": "computer_use",
        "label": f"Computer · {app_name or 'target'}",
        "action": str(action),
        "app": str(app_name)[:128],
        "window": str(window_title)[:160],
        "target": str(target_label)[:160],
        "expected_effect": str(expected_effect)[:240],
        "reversible": bool(reversible),
        "data_summary": (
            f"Text entry ({len(typed_text)} characters; value hidden)"
            if typed_text is not None else "No typed value included"
        ),
        "choices": ["Allow once", "Take over", "Deny"],
        "always_confirm": True,
    }
    if preview_ref:
        payload["ephemeral_preview_ref"] = str(preview_ref)
    return payload
