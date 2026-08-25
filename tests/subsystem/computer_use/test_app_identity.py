from __future__ import annotations

import pytest

from row_bot.computer_use.service import ComputerUseError, ComputerUseService, LeaseOwner


OWNER = LeaseOwner("browser-thread", "browser-generation", "browser-task")


def _window(
    window_id: int,
    app_name: str,
    title: str,
    *,
    pid: int | None = None,
) -> dict:
    return {
        "window_id": window_id,
        "pid": pid or window_id + 1000,
        "app_name": app_name,
        "title": title,
        "bounds": {"x": 0, "y": 0, "width": 1200, "height": 800},
        "is_on_screen": True,
    }


def test_microsoft_edge_display_name_selects_only_title_scoped_msedge_window(
    service,
    fake_transport,
) -> None:
    private_title = "Private mail - secret@example.test"
    fake_transport.scenario.windows = (
        _window(1, "msedge.exe", "YouTube - Microsoft Edge"),
        _window(2, "msedge.exe", private_title),
        _window(3, "notepad.exe", "YouTube notes"),
    )
    service.acquire(OWNER, validate_context=False)

    rows = service.list_windows(OWNER, app="Microsoft Edge", window_hint="YouTube")

    assert len(rows) == 1
    assert rows[0]["app"] == "msedge.exe"
    assert "YouTube" not in repr(rows)
    assert private_title not in repr(rows)


def test_duplicate_native_browser_rows_are_deduplicated_in_stable_order(
    service,
    fake_transport,
) -> None:
    first = _window(1, "msedge.exe", "First - Edge")
    second = _window(2, "msedge.exe", "Second - Edge")
    fake_transport.scenario.windows = (first, dict(first), second)
    service.acquire(OWNER, validate_context=False)

    rows = service.list_windows(OWNER, app="Edge")

    assert [row["candidate"] for row in rows] == [
        "matching msedge.exe window 1",
        "matching msedge.exe window 2",
    ]


@pytest.mark.parametrize(
    ("requested", "driver_name"),
    [
        ("Microsoft Edge", "msedge.exe"),
        ("Edge", "msedge.exe"),
        ("Google Chrome", "chrome.exe"),
        ("Chrome", "chrome.exe"),
        ("Mozilla Firefox", "firefox.exe"),
        ("Firefox", "firefox.exe"),
        ("Brave Browser", "brave.exe"),
        ("Brave", "brave.exe"),
        ("Safari", "Safari.app"),
    ],
)
def test_explicit_browser_alias_groups_match_only_their_canonical_driver(
    service,
    fake_transport,
    requested: str,
    driver_name: str,
) -> None:
    fake_transport.scenario.windows = (
        _window(1, driver_name, "Target"),
        _window(2, "unrelated.exe", "Target"),
    )
    service.acquire(OWNER, validate_context=False)

    rows = service.list_windows(OWNER, app=requested)

    assert len(rows) == 1
    assert rows[0]["app"] == driver_name


def test_unknown_or_partial_app_name_is_not_fuzzily_resolved(service, fake_transport) -> None:
    fake_transport.scenario.windows = (
        _window(1, "chrome.exe", "Target"),
        _window(2, "msedge.exe", "Target"),
    )
    service.acquire(OWNER, validate_context=False)

    assert service.list_windows(OWNER, app="Browser") == []
    assert service.list_windows(OWNER, app="Chrom") == []


def test_launch_resolves_edge_once_and_keeps_friendly_approval_copy(
    fake_client,
    fake_transport,
) -> None:
    approvals: list[dict] = []
    fake_transport.scenario.apps = (
        {"name": "msedge.exe", "running": True},
        {"name": "msedge.exe", "running": True},
    )
    service = ComputerUseService(
        client_factory=lambda: fake_client,
        approval_callback=lambda payload: approvals.append(payload) or True,
    )
    service.acquire(OWNER, validate_context=False)

    windows = service.launch_app("Microsoft Edge", OWNER)

    assert windows[0]["app"] == "msedge.exe"
    launch_args = next(args for name, args in fake_transport.calls if name == "launch_app")
    assert launch_args["name"] == "msedge.exe"
    assert len(approvals) == 1
    assert approvals[0]["app"] == "msedge.exe"
    assert "Microsoft Edge" in approvals[0]["label"]


def test_known_browser_executable_remains_resolvable_when_inventory_scan_fails(
    fake_client,
    fake_transport,
) -> None:
    fake_transport.scenario.list_apps_error_code = "uwp_scan_failed"
    service = ComputerUseService(
        client_factory=lambda: fake_client,
        approval_callback=lambda _payload: True,
    )
    service.acquire(OWNER, validate_context=False)

    service.launch_app("Microsoft Edge", OWNER)

    launch_args = next(args for name, args in fake_transport.calls if name == "launch_app")
    assert launch_args["name"] == "msedge.exe"


def test_unknown_launch_name_is_not_guessed_from_inventory(fake_client, fake_transport) -> None:
    fake_transport.scenario.apps = (
        {"name": "chrome.exe", "running": True},
        {"name": "msedge.exe", "running": True},
    )
    service = ComputerUseService(
        client_factory=lambda: fake_client,
        approval_callback=lambda _payload: True,
    )
    service.acquire(OWNER, validate_context=False)

    with pytest.raises(ComputerUseError, match="resolve"):
        service.launch_app("Browser", OWNER)

    assert "launch_app" not in [name for name, _args in fake_transport.calls]


@pytest.mark.parametrize("protected_name", ["Row.Bot.exe", "ROW BOT", "cua_driver.exe"])
def test_protected_controller_names_remain_blocked_after_normalization(
    service,
    fake_transport,
    protected_name: str,
) -> None:
    service.acquire(OWNER, validate_context=False)

    with pytest.raises(ComputerUseError) as exc_info:
        service.list_windows(OWNER, app=protected_name)

    assert exc_info.value.code == "hard_blocked"
    assert "list_windows" not in [name for name, _args in fake_transport.calls]


def test_driver_launch_error_is_structured_and_never_retried(fake_client, fake_transport) -> None:
    fake_transport.scenario.apps = ({"name": "msedge.exe", "running": True},)
    fake_transport.scenario.launch_error_code = "driver_unavailable"
    service = ComputerUseService(
        client_factory=lambda: fake_client,
        approval_callback=lambda _payload: True,
    )
    service.acquire(OWNER, validate_context=False)

    with pytest.raises(ComputerUseError) as exc_info:
        service.launch_app("Microsoft Edge", OWNER)

    assert exc_info.value.code == "driver_unavailable"
    names = [name for name, _args in fake_transport.calls]
    assert names.count("launch_app") == 1
    assert "list_windows" not in names
    assert "get_window_state" not in names
