"""Small, fail-closed policy table for Computer Use."""

from __future__ import annotations

import re
import unicodedata
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


_BLOCKED_APP = frozenset(
    {
        "terminal",
        "windowsterminal",
        "powershell",
        "windowspowershell",
        "pwsh",
        "commandprompt",
        "cmd",
        "console",
        "conhost",
        "shell",
        "repl",
        "gnometerminal",
        "konsole",
        "xterm",
        "iterm",
        "iterm2",
        "alacritty",
        "kitty",
        "wezterm",
        "hyper",
        "passwordmanager",
        "1password",
        "bitwarden",
        "keepass",
        "keepassxc",
        "keychainaccess",
        "credentialmanager",
        "lockscreen",
        "loginwindow",
        "securedesktop",
        "securitysettings",
        "windowssecurity",
    }
)
_PYTHON_HOST_APP = re.compile(r"(?:^|[\\/])pythonw?(?:\.exe)?$", re.IGNORECASE)
_PROTECTED_SURFACE = re.compile(
    r"^\s*(?:user account control|secure desktop|login window|lock screen|"
    r"windows security|security\s*(?:&|and)\s*privacy|"
    r"privacy\s*(?:&|and)\s*security|accessibility permission|"
    r"screen recording permission|authentication required|elevation required)\s*$",
    re.IGNORECASE,
)
_HANDOFF = re.compile(
    r"(?<!\w)(?:password|passcode|recovery\s+code|payment\s+card|credit\s+card|"
    r"bank\s+credential|one[- ]?time(?:\s+(?:password|code))?|otp|2fa|mfa|"
    r"captcha|biometric|passkey|uac|user\s+account\s+control|"
    r"accessibility\s+permission|screen\s+recording\s+permission|tcc|"
    r"legal\s+acceptance)(?!\w)",
    re.IGNORECASE,
)
_CONSEQUENTIAL = re.compile(
    r"(?<!\w)(?:send|post|submit|publish|confirm|purchase|pay|transfer|order|book|trade|"
    r"delete|remove|empty\s+trash|overwrite|upload|download|share|invite|grant|revoke|"
    r"permission|install|execute|run|account|security|privacy|network|medical|"
    r"financial|export|transmit|save|quit|exit|sign\s+out|log\s+out|"
    r"close\s+without\s+saving)(?!\w)",
    re.IGNORECASE,
)
_SECURE_ROLE = re.compile(r"(?:password|secure|credential|otp|captcha)", re.IGNORECASE)
_CONSEQUENTIAL_TARGET_ACTIONS = frozenset(
    {
        "click",
        "double_click",
        "right_click",
        "type",
        "replace_text",
        "key",
        "key_sequence",
        "menu",
    }
)
_DANGEROUS_KEYS = frozenset({
    "win", "windows", "meta", "super", "ctrl+alt+delete", "command+space",
    "cmd+space", "alt+f4", "cmd+q", "control+command+q",
})


def is_consequential_label(value: object) -> bool:
    return bool(
        _CONSEQUENTIAL.search(unicodedata.normalize("NFKC", str(value or "")))
    )


def _canonical_app_identity(value: object) -> str:
    """Return one exact app/process/bundle identity for protected-class checks."""

    text = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    text = text.replace("\\", "/").rsplit("/", 1)[-1]
    had_app_suffix = False
    for suffix in (".exe", ".app"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            had_app_suffix = True
            break
    if not had_app_suffix and "." in text and " " not in text:
        text = text.rsplit(".", 1)[-1]
    return "".join(character for character in text if character.isalnum())


def _normalized_identity_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(character for character in text if character.isalnum())


def _is_protected_surface(window_title: object) -> bool:
    title = unicodedata.normalize("NFKC", str(window_title or ""))
    return bool(_PROTECTED_SURFACE.fullmatch(title))


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
    target = " ".join((role, label, expected_effect, destination)).strip()
    app_identity = _canonical_app_identity(app_name)
    row_bot_controller = bool(
        app_identity in {"rowbot", "cuadriver"}
        or (
            _PYTHON_HOST_APP.search(str(app_name or "").strip())
            and "rowbot" in _normalized_identity_text(window_title)
        )
    )
    if row_bot_controller or app_identity in _BLOCKED_APP or _is_protected_surface(window_title):
        return PolicyDecision(PolicyOutcome.BLOCKED, "This app or protected surface is not available to Computer Use.", False)
    if _SECURE_ROLE.search(role) or _HANDOFF.search(
        " ".join((label, expected_effect))
    ):
        return PolicyDecision(PolicyOutcome.HANDOFF, "Sensitive credentials or a protected system surface require user takeover.", False)
    normalized_keys = keys.strip().lower().replace(" ", "")
    if action == "key" and normalized_keys in {item.replace(" ", "") for item in _DANGEROUS_KEYS}:
        return PolicyDecision(PolicyOutcome.BLOCKED, "System or security key chords are blocked.", False)
    if action == "key_sequence" and "calculator" not in app_identity:
        return PolicyDecision(
            PolicyOutcome.BLOCKED,
            "The bounded key sequence is available only for a semantic Calculator target.",
            False,
        )
    if action in {"list_apps", "list_windows", "capture", "wait", "stop"}:
        return PolicyDecision(PolicyOutcome.OBSERVATION, "Read-only observation or local lifecycle action.")
    if foreground:
        return PolicyDecision(PolicyOutcome.CONSEQUENTIAL, "Foreground takeover always requires confirmation.")
    if action in _CONSEQUENTIAL_TARGET_ACTIONS and is_consequential_label(target):
        return PolicyDecision(PolicyOutcome.CONSEQUENTIAL, "The target may create an external or hard-to-reverse effect.", False)
    if coordinate_only and action in {"click", "double_click", "right_click", "key"}:
        return PolicyDecision(PolicyOutcome.CONSEQUENTIAL, "An ambiguous coordinate action requires point-of-risk confirmation.")
    if action == "key" and normalized_keys in {"enter", "return"}:
        return PolicyDecision(PolicyOutcome.CONSEQUENTIAL, "Enter may submit the active form or dialog.", False)
    if action in {"launch_app", "focus", "click", "double_click", "right_click", "type", "replace_text", "key", "key_sequence", "scroll", "drag", "menu"}:
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
