"""Narrow contracts shared by the independent automation engines."""

from row_bot.automation.contracts import (
    ActionReceipt,
    ActivitySnapshot,
    AutomationError,
    AutomationSurface,
    ObservationStatus,
    classify_no_progress,
    sanitize_automation_error,
)

__all__ = [
    "ActionReceipt",
    "ActivitySnapshot",
    "AutomationError",
    "AutomationSurface",
    "ObservationStatus",
    "classify_no_progress",
    "sanitize_automation_error",
]
