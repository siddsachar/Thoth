from __future__ import annotations

import pytest

from row_bot.computer_use.readiness import CuaReadiness, ReadinessCode
from row_bot.ui import computer_use as computer_use_ui
from row_bot.ui.computer_use import (
    computer_use_permission_recovery,
    computer_use_settings_view,
    open_macos_privacy_settings,
)


@pytest.mark.parametrize(
    ("state", "title", "primary"),
    [
        (CuaReadiness(ReadinessCode.DISABLED, "internal"), "Computer Use is off", ""),
        (
            CuaReadiness(ReadinessCode.DISCLOSURE_REQUIRED, "internal"),
            "Review required",
            "",
        ),
        (
            CuaReadiness(ReadinessCode.NOT_INSTALLED, "internal"),
            "Set up Computer Use",
            "Install Computer Use",
        ),
        (
            CuaReadiness(
                ReadinessCode.DEGRADED,
                "internal",
                executable="cua.exe",
                remediation="Run diagnostics.",
            ),
            "Finish setting up Computer Use",
            "Check setup",
        ),
        (
            CuaReadiness(
                ReadinessCode.DEGRADED,
                "internal",
                executable="cua.exe",
                remediation="Test with Calculator.",
            ),
            "Finish setting up Computer Use",
            "Test Computer Use",
        ),
        (
            CuaReadiness(ReadinessCode.READY, "internal", executable="cua.exe"),
            "Computer Use is ready",
            "Test Computer Use",
        ),
        (
            CuaReadiness(ReadinessCode.UNSUPPORTED, "internal"),
            "Computer Use is unavailable",
            "",
        ),
    ],
)
def test_settings_view_uses_plain_language_and_contextual_primary_action(
    state: CuaReadiness,
    title: str,
    primary: str,
) -> None:
    view = computer_use_settings_view(state)
    assert view.title == title
    assert view.primary_action == primary
    assert "Cua" not in view.title
    assert "SHA" not in view.detail
    assert "Driver" not in view.detail


@pytest.mark.parametrize(
    ("operation", "title"),
    [
        ("installing", "Installing Computer Use…"),
        ("checking", "Checking Computer Use…"),
        ("testing", "Testing Computer Use…"),
    ],
)
def test_settings_view_has_explicit_busy_states(operation: str, title: str) -> None:
    view = computer_use_settings_view(
        CuaReadiness(ReadinessCode.READY, "internal", executable="cua.exe"),
        operation=operation,
    )
    assert view.title == title
    assert view.primary_action == ""


def test_ready_state_hides_install_and_keeps_manage_recovery() -> None:
    view = computer_use_settings_view(
        CuaReadiness(ReadinessCode.READY, "internal", executable="cua.exe")
    )
    assert not view.needs_install
    assert view.show_manage
    assert view.allow_test
    assert view.allow_check


def test_macos_permission_failure_has_a_guided_recheck_state() -> None:
    view = computer_use_settings_view(
        CuaReadiness(
            ReadinessCode.PERMISSION_MISSING,
            "internal",
            executable="/managed/CuaDriver.app/Contents/MacOS/cua-driver",
        ),
        system="Darwin",
    )
    assert view.title == "Allow macOS access"
    assert view.primary_action == "Recheck"
    assert "Row-Bot" in view.detail


def test_macos_bundle_repair_is_a_direct_primary_action() -> None:
    view = computer_use_settings_view(
        CuaReadiness(
            ReadinessCode.DEGRADED,
            "internal",
            executable="/managed/0.7.1/Contents/MacOS/cua-driver",
            remediation="Reinstall Computer Use to repair its macOS helper.",
        )
    )
    assert view.primary_action == "Repair Computer Use"
    assert view.needs_install is True
    assert "macOS runtime inside Row-Bot" in view.detail


def test_macos_permission_recovery_is_specific_and_hides_driver_internals() -> None:
    state = CuaReadiness(
        ReadinessCode.PERMISSION_MISSING,
        "Cua Driver diagnostics need attention.",
        executable="/managed/CuaDriver.app/Contents/MacOS/cua-driver",
        remediation=(
            "If the process bundle is not com.trycua.driver (see bundle_identity), "
            "restart via `cua-driver mcp`."
        ),
        details={
            "checks": [
                {
                    "name": "tcc_accessibility",
                    "status": "fail",
                    "hint": "Grant Accessibility to CuaDriver.app.",
                },
                {
                    "name": "screen_capture_capability",
                    "status": "fail",
                    "hint": "Grant screen capture to the responsible process.",
                },
                {
                    "name": "bundle_identity",
                    "status": "fail",
                    "hint": "Internal bundle identity detail.",
                },
            ]
        },
    )

    recovery = computer_use_permission_recovery(state, system="Darwin")

    assert recovery is not None
    assert recovery.missing_accessibility is True
    assert recovery.missing_screen_recording is True
    visible_text = " ".join((recovery.title, recovery.detail, *recovery.steps))
    assert "Row-Bot" in visible_text
    assert "System Settings" in visible_text
    for internal in ("CuaDriver", "com.trycua.driver", "bundle_identity", "cua-driver mcp"):
        assert internal not in visible_text


def test_permission_recovery_is_not_shown_for_non_permission_failures() -> None:
    state = CuaReadiness(ReadinessCode.FAILED, "internal")
    assert computer_use_permission_recovery(state, system="Darwin") is None


@pytest.mark.parametrize(
    ("permission", "fragment"),
    [
        ("accessibility", "Privacy_Accessibility"),
        ("screen_recording", "Privacy_ScreenCapture"),
    ],
)
def test_macos_permission_buttons_open_fixed_system_settings_deep_links(
    permission: str,
    fragment: str,
    monkeypatch,
) -> None:
    calls = []
    monkeypatch.setattr(
        computer_use_ui.subprocess,
        "Popen",
        lambda args, **kwargs: calls.append((args, kwargs)),
    )

    open_macos_privacy_settings(permission)

    assert calls[0][0][0] == "open"
    assert fragment in calls[0][0][1]
    assert "shell" not in calls[0][1]
