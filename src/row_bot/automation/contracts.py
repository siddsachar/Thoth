"""Small immutable vocabulary shared by Browser and Computer Use.

This module deliberately contains no service, lease, page, process, transport,
or persistence ownership.  Those remain private to each automation engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class AutomationSurface(str, Enum):
    BROWSER = "browser"
    COMPUTER = "computer"


_BACKEND_EFFECTS = frozenset(
    {"confirmed", "partial", "unverifiable", "suspected_noop", "refused"}
)
_VISUAL_CHANGES = frozenset({"changed", "unchanged", "unknown"})
_SAFE_ERROR_CODES = frozenset(
    {
        "approval_denied",
        "browser_unavailable",
        "cancelled",
        "computer_failed",
        "driver_failed",
        "driver_unavailable",
        "hard_blocked",
        "handoff_required",
        "invalid_input",
        "lease_busy",
        "navigation_failed",
        "no_progress",
        "runtime_mismatch",
        "stale_observation",
        "target_mismatch",
        "transient_backend_failure",
        "unsupported_capability",
    }
)


@dataclass(frozen=True)
class ObservationStatus:
    """Non-content provenance for one bounded ephemeral observation."""

    revision: int
    backend_declared_count: int | None = None
    backend_received_count: int = 0
    backend_filtered_count: int | None = None
    locally_validated_count: int = 0
    projected_count: int = 0
    locally_filtered_count: int = 0
    backend_limited: bool | None = None
    backend_sparse: bool = False
    local_limit_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "revision",
            "backend_received_count",
            "locally_validated_count",
            "projected_count",
            "locally_filtered_count",
        ):
            if int(getattr(self, name)) < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.backend_declared_count is not None and self.backend_declared_count < 0:
            raise ValueError("backend_declared_count must be non-negative")
        if self.backend_filtered_count is not None and self.backend_filtered_count < 0:
            raise ValueError("backend_filtered_count must be non-negative")
        object.__setattr__(
            self,
            "local_limit_reasons",
            tuple(sorted({str(reason) for reason in self.local_limit_reasons if str(reason)})),
        )

    @property
    def provenance(self) -> str:
        local_limited = bool(self.local_limit_reasons)
        if self.backend_sparse:
            return "sparse"
        if self.backend_limited is True and local_limited:
            return "both"
        if self.backend_limited is True:
            return "driver"
        if local_limited:
            return "row_bot"
        if self.backend_limited is False:
            return "complete"
        return "unknown"

    @property
    def truncated(self) -> bool:
        """Compatibility summary for older trace consumers."""

        return self.backend_limited is True or bool(self.local_limit_reasons)

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "backend_declared_count": self.backend_declared_count,
            "backend_received_count": self.backend_received_count,
            "backend_filtered_count": self.backend_filtered_count,
            "locally_validated_count": self.locally_validated_count,
            "projected_count": self.projected_count,
            "locally_filtered_count": self.locally_filtered_count,
            "backend_limited": self.backend_limited,
            "backend_sparse": self.backend_sparse,
            "local_limit_reasons": list(self.local_limit_reasons),
            "provenance": self.provenance,
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class ActionReceipt:
    """Truthful cross-engine action facts with compatibility properties."""

    surface: AutomationSurface
    target_id: str
    action_family: str
    revision: int
    dispatched: bool = True
    completed: bool = True
    backend_effect: str = "unverifiable"
    delivery: str = ""
    route: str = ""
    visual_change: str = "unknown"
    verified_outcome: bool | None = None
    verified_scope: str = ""
    cause: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "surface", AutomationSurface(self.surface))
        effect = str(self.backend_effect or "unverifiable").casefold()
        object.__setattr__(
            self,
            "backend_effect",
            effect if effect in _BACKEND_EFFECTS else "unverifiable",
        )
        visual = str(self.visual_change or "unknown").casefold()
        object.__setattr__(
            self,
            "visual_change",
            visual if visual in _VISUAL_CHANGES else "unknown",
        )
        object.__setattr__(self, "cause", str(self.cause or "")[:120])
        scope = str(self.verified_scope or "").casefold()
        object.__setattr__(
            self,
            "verified_scope",
            scope if scope in {"delivery", "exact_value", "exact_state"} else "",
        )

    @property
    def action(self) -> str:
        return self.action_family

    @property
    def target_revision(self) -> int:
        return self.revision

    @property
    def action_dispatched(self) -> bool:
        return self.dispatched

    @property
    def action_completed(self) -> bool:
        return self.completed

    @property
    def driver_effect(self) -> str:
        return self.backend_effect

    @property
    def delivery_mode(self) -> str:
        return self.delivery

    @property
    def effect_verified(self) -> bool:
        return self.verified_outcome is True

    @property
    def effect(self) -> str:
        return self.visual_change if self.visual_change != "unknown" else self.backend_effect

    def to_dict(self, *, compatibility: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "surface": self.surface.value,
            "target_id": self.target_id,
            "action_family": self.action_family,
            "revision": self.revision,
            "dispatched": self.dispatched,
            "completed": self.completed,
            "backend_effect": self.backend_effect,
            "delivery": self.delivery,
            "route": self.route,
            "visual_change": self.visual_change,
            "verified_outcome": self.verified_outcome,
            "cause": self.cause,
        }
        if compatibility:
            payload.update(
                {
                    "action": self.action,
                    "target_revision": self.target_revision,
                    "action_dispatched": self.action_dispatched,
                    "action_completed": self.action_completed,
                    "driver_effect": self.driver_effect,
                    "delivery_mode": self.delivery_mode,
                    "effect_verified": self.effect_verified,
                    "effect": self.effect,
                }
            )
        return payload


@dataclass(frozen=True)
class AutomationError(Exception):
    code: str
    retryable: bool
    action_family: str
    remediation: str

    def __post_init__(self) -> None:
        code = str(self.code or "computer_failed")
        object.__setattr__(self, "code", code if code in _SAFE_ERROR_CODES else "computer_failed")
        object.__setattr__(self, "action_family", str(self.action_family or "automation")[:64])
        object.__setattr__(self, "remediation", str(self.remediation or "Stop or take over.")[:240])
        Exception.__init__(self, self.remediation)


@dataclass(frozen=True)
class ActivitySnapshot:
    surface: AutomationSurface
    active: bool
    paused: bool
    thread_id: str
    state: str
    target: str
    last_action: str
    revision: int
    has_thumbnail: bool = False
    preview_shielded: bool = False
    generation_id: str = ""

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        surface: AutomationSurface | str | None = None,
    ) -> "ActivitySnapshot":
        selected = AutomationSurface(surface or value.get("surface") or value.get("engine"))
        target = value.get("target") or value.get("app") or value.get("site") or ""
        return cls(
            surface=selected,
            active=bool(value.get("active")),
            paused=bool(value.get("paused")),
            thread_id=str(value.get("thread_id") or ""),
            state=str(value.get("state") or "idle"),
            target=str(target)[:160],
            last_action=str(value.get("last_action") or "")[:160],
            revision=max(0, int(value.get("revision") or 0)),
            has_thumbnail=bool(value.get("has_thumbnail")),
            preview_shielded=bool(value.get("preview_shielded")),
            generation_id=str(value.get("generation_id") or ""),
        )

    @property
    def state_label(self) -> str:
        return activity_state_label(self.state)


def activity_state_label(state: object) -> str:
    return {
        "acquiring": "Starting",
        "acting": "Acting",
        "observing": "Observing",
        "verifying": "Verifying",
        "waiting_approval": "Waiting for approval",
        "waiting_user": "Waiting for you",
        "resuming": "Resuming",
        "needs_attention": "Needs attention",
        "failed": "Needs attention",
        "stopping": "Stopping",
    }.get(str(state or "").casefold(), "Ready")


def classify_no_progress(
    *,
    backend_effect: object,
    visual_change: object,
    verified_outcome: bool | None,
) -> str:
    """Classify evidence without turning one unknown result into an error."""

    effect = str(backend_effect or "unverifiable").casefold()
    visual = str(visual_change or "unknown").casefold()
    if verified_outcome is True or effect in {"confirmed", "partial"} or visual == "changed":
        return "progress"
    if verified_outcome is False and (effect == "suspected_noop" or visual == "unchanged"):
        return "no_progress"
    return "unknown"


def sanitize_automation_error(
    surface: AutomationSurface | str,
    action_family: object,
    error: BaseException | str,
) -> AutomationError:
    """Reduce backend failures to reviewed codes without returning raw text."""

    selected = AutomationSurface(surface)
    known = str(getattr(error, "code", "") or "").casefold()
    message = str(error or "").casefold()
    if known in _SAFE_ERROR_CODES:
        code = known
    elif any(marker in message for marker in ("stale", "detached", "not attached")):
        code = "stale_observation"
    elif any(marker in message for marker in ("cancel", "stopped by user")):
        code = "cancelled"
    elif any(marker in message for marker in ("executable", "runtime", "browser type")):
        code = "runtime_mismatch"
    elif any(marker in message for marker in ("timeout", "temporarily unavailable")):
        code = "transient_backend_failure"
    else:
        code = "browser_unavailable" if selected is AutomationSurface.BROWSER else "driver_failed"
    remediation = {
        "stale_observation": "Observe the same surface again before retrying the action.",
        "cancelled": "The action was stopped; start a new action only if still requested.",
        "runtime_mismatch": "Repair the reviewed managed runtime before retrying.",
        "transient_backend_failure": "Retry once using the same surface, then stop or take over.",
        "browser_unavailable": "Repair Browser Automation or take over the managed browser.",
        "driver_failed": "Retry once if safe, then stop or take over Computer Use.",
    }.get(code, "Stop or take over before trying a different action.")
    retryable = code in {"stale_observation", "transient_backend_failure"}
    return AutomationError(code, retryable, str(action_family or "automation"), remediation)
