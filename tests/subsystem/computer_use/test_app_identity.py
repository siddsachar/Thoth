from __future__ import annotations

import pytest

from row_bot.computer_use import service as service_module
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
        _window(3, "CalculatorApp.exe", "Calculator"),
    )
    service.acquire(OWNER, validate_context=False)

    assert service.list_windows(OWNER, app="Browser") == []
    assert service.list_windows(OWNER, app="Chrom") == []
    assert service.list_windows(OWNER, app="Calc") == []


@pytest.mark.parametrize(
    ("requested", "driver_name"),
    [
        ("Calculator", "Windows Calculator"),
        ("Calculator", "CalculatorApp.exe"),
        ("Windows Calculator", "CalculatorApp.exe"),
    ],
)
def test_explicit_calculator_identity_group_matches_inventory_and_window_names(
    service,
    fake_transport,
    requested: str,
    driver_name: str,
) -> None:
    fake_transport.scenario.windows = (
        _window(1, driver_name, "Calculator"),
        _window(2, "unrelated.exe", "Calculator notes"),
    )
    service.acquire(OWNER, validate_context=False)

    rows = service.list_windows(OWNER, app=requested)

    assert len(rows) == 1
    assert rows[0]["app"] == driver_name


def test_calculator_window_discovery_accepts_only_the_exact_packaged_host_title(
    service,
    fake_transport,
) -> None:
    fake_transport.scenario.windows = (
        _window(1, "ApplicationFrameHost.exe", "Calculator"),
        _window(2, "ApplicationFrameHost.exe", "Calculator notes"),
        _window(3, "unrelated.exe", "Calculator"),
    )
    service.acquire(OWNER, validate_context=False)

    rows = service.list_windows(OWNER, app="Calculator")

    assert len(rows) == 1
    target = service._target(rows[0]["target_id"])
    assert (target.app_name, target.window_title) == (
        "ApplicationFrameHost.exe",
        "Calculator",
    )


def test_packaged_host_discovery_is_exact_for_any_reviewed_app_name(
    service,
    fake_transport,
) -> None:
    fake_transport.scenario.windows = (
        _window(1, "ApplicationFrameHost.exe", "Photos"),
        _window(2, "ApplicationFrameHost.exe", "Photos notes"),
        _window(3, "unrelated.exe", "Photos"),
    )
    service.acquire(OWNER, validate_context=False)

    rows = service.list_windows(OWNER, app="Photos")

    assert len(rows) == 1
    target = service._target(rows[0]["target_id"])
    assert (target.app_name, target.window_title) == (
        "ApplicationFrameHost.exe",
        "Photos",
    )


def test_calculator_launch_resolves_windows_inventory_name_under_local_ui_grant(
    fake_client,
    fake_transport,
    monkeypatch,
) -> None:
    monkeypatch.setattr(service_module.platform, "system", lambda: "Windows")
    approvals: list[dict] = []
    fake_transport.scenario.apps = (
        {
            "name": "Windows Calculator",
            "bundle_id": "Microsoft.WindowsCalculator_8wekyb3d8bbwe",
            "kind": "uwp",
            "running": False,
        },
    )
    service = ComputerUseService(
        client_factory=lambda: fake_client,
        approval_callback=lambda payload: approvals.append(payload) or True,
    )
    service.acquire(OWNER, validate_context=False)
    service.grant_app_permission_for_local_ui(OWNER, "Calculator")

    windows = service.launch_app("Calculator", OWNER)

    launch_args = next(args for name, args in fake_transport.calls if name == "launch_app")
    assert launch_args["name"] == "calc.exe"
    assert windows[0]["app"] == "Calculator"
    assert [name for name, _args in fake_transport.calls].count("list_windows") == 3
    assert approvals == []


def test_packaged_calculator_launch_never_trusts_an_unidentified_launch_row(
    fake_client,
    fake_transport,
    monkeypatch,
) -> None:
    monkeypatch.setattr(service_module.platform, "system", lambda: "Windows")
    fake_transport.scenario.apps = (
        {
            "name": "Windows Calculator",
            "bundle_id": "Microsoft.WindowsCalculator_8wekyb3d8bbwe",
            "kind": "uwp",
            "running": False,
        },
    )
    fake_transport.scenario.windows = (
        _window(1, "unrelated.exe", "Calculator notes"),
    )
    service = ComputerUseService(
        client_factory=lambda: fake_client,
        approval_callback=lambda _payload: True,
    )
    service.PACKAGED_LAUNCH_STABILITY_TIMEOUT_SECONDS = 0.0
    service.acquire(OWNER, validate_context=False)

    with pytest.raises(ComputerUseError) as exc_info:
        service.launch_app("Calculator", OWNER)

    assert exc_info.value.code == "target_gone"
    assert [name for name, _args in fake_transport.calls].count("launch_app") == 1
    assert [name for name, _args in fake_transport.calls].count("list_windows") == 1
    assert "get_window_state" not in [name for name, _args in fake_transport.calls]


def test_packaged_calculator_launch_rebinds_only_after_replacement_is_stable(
    fake_client,
    fake_transport,
    monkeypatch,
) -> None:
    monkeypatch.setattr(service_module.platform, "system", lambda: "Windows")
    fake_transport.scenario.apps = (
        {
            "name": "Windows Calculator",
            "bundle_id": "Microsoft.WindowsCalculator_8wekyb3d8bbwe",
            "kind": "uwp",
            "running": False,
        },
    )
    transient = _window(1, "CalculatorApp.exe", "Calculator", pid=111)
    replacement = _window(2, "CalculatorApp.exe", "Calculator", pid=222)
    fake_transport.scenario.window_snapshots = (
        (transient,),
        (replacement,),
        (replacement,),
        (replacement,),
    )
    fake_transport.scenario.launch_pid = 222
    fake_transport.scenario.launch_window_id = 2
    service = ComputerUseService(
        client_factory=lambda: fake_client,
        approval_callback=lambda _payload: True,
    )
    service.PACKAGED_LAUNCH_POLL_INTERVAL_SECONDS = 0.0
    service.acquire(OWNER, validate_context=False)
    service.grant_app_permission_for_local_ui(OWNER, "Calculator")

    windows = service.launch_app("Calculator", OWNER)

    target = service._target(windows[0]["target_id"])
    assert (target.pid, target.window_id) == (222, 2)
    assert [name for name, _args in fake_transport.calls].count("list_windows") == 4


def test_packaged_calculator_launch_binds_exact_package_window_under_shared_host(
    fake_client,
    fake_transport,
    monkeypatch,
) -> None:
    monkeypatch.setattr(service_module.platform, "system", lambda: "Windows")
    fake_transport.scenario.apps = (
        {
            "name": "Windows Calculator",
            "kind": "uwp",
            "bundle_id": "Microsoft.WindowsCalculator_8wekyb3d8bbwe",
            "running": False,
        },
    )
    trusted = _window(7, "ApplicationFrameHost.exe", "Calculator", pid=777)
    decoy = _window(8, "ApplicationFrameHost.exe", "Calculator", pid=777)
    fake_transport.scenario.window_snapshots = (
        (trusted, decoy),
        (trusted, decoy),
        (trusted, decoy),
    )
    fake_transport.scenario.launch_pid = 777
    fake_transport.scenario.launch_window_id = 7
    service = ComputerUseService(
        client_factory=lambda: fake_client,
        approval_callback=lambda _payload: True,
    )
    service.PACKAGED_LAUNCH_POLL_INTERVAL_SECONDS = 0.0
    service.acquire(OWNER, validate_context=False)
    service.grant_app_permission_for_local_ui(OWNER, "Calculator")

    windows = service.launch_app("Calculator", OWNER)

    assert len(windows) == 1
    target = service._target(windows[0]["target_id"])
    assert (target.app_name, target.pid, target.window_id) == (
        "Calculator",
        777,
        7,
    )


def test_packaged_calculator_launch_rejects_untrusted_package_identity(
    fake_client,
    fake_transport,
    monkeypatch,
) -> None:
    monkeypatch.setattr(service_module.platform, "system", lambda: "Windows")
    fake_transport.scenario.apps = (
        {
            "name": "Windows Calculator",
            "kind": "uwp",
            "bundle_id": "Microsoft.WindowsCalculator_8wekyb3d8bbwe",
            "running": False,
        },
    )
    fake_transport.scenario.launch_bundle_id = "Unrelated.Package_123!App"
    service = ComputerUseService(
        client_factory=lambda: fake_client,
        approval_callback=lambda _payload: True,
    )
    service.acquire(OWNER, validate_context=False)
    service.grant_app_permission_for_local_ui(OWNER, "Calculator")

    with pytest.raises(ComputerUseError) as exc_info:
        service.launch_app("Calculator", OWNER)

    assert exc_info.value.code == "target_gone"
    assert "get_window_state" not in [name for name, _args in fake_transport.calls]


def test_any_reviewed_packaged_app_uses_package_proven_shared_host_binding(
    fake_client,
    fake_transport,
) -> None:
    fake_transport.scenario.apps = (
        {
            "name": "Contoso Notes",
            "kind": "uwp",
            "bundle_id": "Contoso.Notes_abcd1234",
            "running": False,
        },
    )
    trusted = _window(
        17,
        "ContosoNotes.exe",
        "Quarterly plan - Contoso Notes",
        pid=1717,
    )
    fake_transport.scenario.window_snapshots = (
        (trusted,),
        (trusted,),
        (trusted,),
    )
    fake_transport.scenario.launch_pid = 1717
    fake_transport.scenario.launch_window_id = 17
    fake_transport.scenario.launch_bundle_id = "Contoso.Notes_abcd1234!App"
    service = ComputerUseService(
        client_factory=lambda: fake_client,
        approval_callback=lambda _payload: True,
    )
    service.PACKAGED_LAUNCH_POLL_INTERVAL_SECONDS = 0.0
    service.acquire(OWNER, validate_context=False)
    service.grant_app_permission_for_local_ui(OWNER, "Contoso Notes")

    windows = service.launch_app("Contoso Notes", OWNER)

    assert len(windows) == 1
    target = service._target(windows[0]["target_id"])
    assert (target.app_name, target.pid, target.window_id) == (
        "Contoso Notes",
        1717,
        17,
    )


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
