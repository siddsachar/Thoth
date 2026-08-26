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


def test_canonical_edge_identity_selects_only_title_scoped_window(
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

    rows = service.list_windows(OWNER, app="msedge.exe", window_hint="YouTube")

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

    rows = service.list_windows(OWNER, app="msedge.exe")

    assert [row["candidate"] for row in rows] == [
        "matching msedge.exe window 1",
        "matching msedge.exe window 2",
    ]


def test_repeated_window_discovery_reuses_lease_target_id_until_identity_changes(
    service,
    fake_transport,
) -> None:
    original = _window(41, "Generic App", "Document", pid=4101)
    fake_transport.scenario.windows = (original,)
    service.acquire(OWNER, validate_context=False)

    first = service.list_windows(OWNER, app="Generic App")[0]["target_id"]
    repeated = service.list_windows(OWNER, app="Generic App")[0]["target_id"]

    assert repeated == first

    fake_transport.scenario.windows = (
        _window(42, "Generic App", "Document", pid=4101),
    )
    changed_window = service.list_windows(OWNER, app="Generic App")[0]["target_id"]
    assert changed_window != first

    fake_transport.scenario.windows = (
        _window(42, "Generic App", "Document", pid=4102),
    )
    changed_pid = service.list_windows(OWNER, app="Generic App")[0]["target_id"]
    assert changed_pid not in {first, changed_window}

    service.stop()
    service.acquire(OWNER, validate_context=False)
    next_lease = service.list_windows(OWNER, app="Generic App")[0]["target_id"]
    assert next_lease != changed_pid


@pytest.mark.parametrize(
    ("requested", "driver_name"),
    [
        ("Google Chrome", "chrome.exe"),
        ("Mozilla Firefox", "firefox.exe"),
        ("Brave Browser", "brave.exe"),
    ],
)
def test_unique_friendly_name_with_one_extra_word_resolves_driver_identity(
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


@pytest.mark.parametrize("requested", ["Microsoft Edge", "Edge"])
def test_names_without_a_shared_exact_word_are_not_guessed(
    service,
    fake_transport,
    requested: str,
) -> None:
    fake_transport.scenario.windows = (_window(1, "msedge.exe", "Target"),)
    service.acquire(OWNER, validate_context=False)

    assert service.list_windows(OWNER, app=requested) == []


@pytest.mark.parametrize(
    ("requested", "driver_name"),
    [
        ("Chrome", "chrome.exe"),
        ("Firefox", "firefox.exe"),
        ("Brave", "brave.exe"),
        ("Safari", "Safari.app"),
    ],
)
def test_exact_identity_allows_only_platform_executable_or_bundle_suffix(
    service,
    fake_transport,
    requested: str,
    driver_name: str,
) -> None:
    fake_transport.scenario.windows = (_window(1, driver_name, "Target"),)
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


def test_app_scoped_capture_zero_matches_returns_bounded_running_exact_candidates_without_pixels(
    service,
    fake_transport,
) -> None:
    private_title = "Private document title"
    fake_transport.scenario.windows = (
        _window(1, "grid-editor.exe", private_title),
    )
    candidate_names = (
        "grid-editor.exe",
        "Document App.app",
        "org.example.Editor",
        *(f"candidate-{index}.exe" for index in range(9)),
    )
    fake_transport.scenario.apps = tuple(
        {
            "name": name,
            "running": True,
            "active": index == 3,
        }
        for index, name in enumerate(candidate_names)
    ) + (
        {"name": "not-running.exe", "running": False, "active": False},
    )

    with pytest.raises(ComputerUseError) as missing:
        service.capture(owner=OWNER, app="Grid Editing App")

    assert missing.value.code == "target_gone"
    assert len(missing.value.candidates) == 8
    assert missing.value.candidates[3] == {
        "name": "candidate-0.exe",
        "running": True,
        "active": True,
    }
    assert all(row["running"] is True for row in missing.value.candidates)
    assert "not-running.exe" not in repr(missing.value.candidates)
    assert private_title not in repr(missing.value.candidates)
    assert [name for name, _args in fake_transport.calls].count("list_windows") == 0
    assert [name for name, _args in fake_transport.calls].count("list_apps") == 1
    assert "launch_app" not in [name for name, _args in fake_transport.calls]
    assert "get_window_state" not in [name for name, _args in fake_transport.calls]


def test_app_scoped_capture_multiple_matches_returns_opaque_ambiguity_without_pixels(
    service,
    fake_transport,
) -> None:
    private_titles = ("Private workbook A", "Private workbook B")
    fake_transport.scenario.windows = (
        _window(1, "Notepad", private_titles[0], pid=2001),
        _window(2, "Notepad", private_titles[1], pid=2001),
    )
    fake_transport.scenario.apps = (
        {"name": "Notepad", "pid": 2001, "running": True},
    )

    with pytest.raises(ComputerUseError) as ambiguous:
        service.capture(owner=OWNER, app="Notepad")

    assert ambiguous.value.code == "ambiguous_target"
    assert len(ambiguous.value.candidates) == 2
    assert all(str(row["target_id"]).startswith("target_") for row in ambiguous.value.candidates)
    assert all(title not in repr(ambiguous.value.candidates) for title in private_titles)
    assert "get_window_state" not in [name for name, _args in fake_transport.calls]


def test_app_scoped_capture_rejects_exact_name_window_owned_by_another_pid(
    service,
    fake_transport,
) -> None:
    fake_transport.scenario.apps = (
        {"name": "Native Editor.app", "pid": 3101, "running": True},
    )
    fake_transport.scenario.windows = (
        _window(41, "Native Editor.app", "Open", pid=4101),
    )

    with pytest.raises(ComputerUseError) as mismatch:
        service.capture(owner=OWNER, app="Native Editor.app")

    assert mismatch.value.code == "target_gone"
    assert "get_window_state" not in [name for name, _args in fake_transport.calls]


def test_protected_app_scoped_capture_is_blocked_before_driver_start(
    service,
    fake_transport,
) -> None:
    with pytest.raises(ComputerUseError) as blocked:
        service.capture(owner=OWNER, app="python.exe", window_hint="Row-Bot")

    assert blocked.value.code == "hard_blocked"
    assert fake_transport.opened is False
    assert fake_transport.calls == []


@pytest.mark.parametrize(
    ("requested", "driver_name"),
    [
        ("Calculator", "CalculatorApp.exe"),
        ("Windows Calculator", "CalculatorApp.exe"),
    ],
)
def test_noncanonical_calculator_names_do_not_cross_select(
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

    assert rows == []


def test_unique_friendly_name_with_one_vendor_word_selects_visible_window(
    service,
    fake_transport,
) -> None:
    fake_transport.scenario.windows = (
        _window(1, "Windows Calculator", "Calculator"),
        _window(2, "unrelated.exe", "Calculator notes"),
    )
    service.acquire(OWNER, validate_context=False)

    rows = service.list_windows(OWNER, app="Calculator")

    assert len(rows) == 1
    assert rows[0]["app"] == "Windows Calculator"


def test_window_discovery_does_not_infer_app_from_packaged_host_title(
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

    assert rows == []


def test_window_discovery_does_not_infer_any_app_from_shared_host_title(
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

    assert rows == []


def test_packaged_launch_uses_exact_inventory_name_under_local_ui_grant(
    fake_client,
    fake_transport,
) -> None:
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
    service.grant_app_permission_for_local_ui(OWNER, "Windows Calculator")

    windows = service.launch_app("Windows Calculator", OWNER)

    launch_args = next(args for name, args in fake_transport.calls if name == "launch_app")
    assert launch_args["name"] == "Windows Calculator"
    assert windows[0]["app"] == "Windows Calculator"
    assert [name for name, _args in fake_transport.calls].count("list_windows") == 3
    assert approvals == []


def test_packaged_calculator_launch_never_trusts_an_unidentified_launch_row(
    fake_client,
    fake_transport,
) -> None:
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
        service.launch_app("Windows Calculator", OWNER)

    assert exc_info.value.code == "target_gone"
    assert [name for name, _args in fake_transport.calls].count("launch_app") == 1
    assert [name for name, _args in fake_transport.calls].count("list_windows") == 1
    assert "get_window_state" not in [name for name, _args in fake_transport.calls]


def test_packaged_calculator_launch_rebinds_only_after_replacement_is_stable(
    fake_client,
    fake_transport,
) -> None:
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
    service.grant_app_permission_for_local_ui(OWNER, "Windows Calculator")

    windows = service.launch_app("Windows Calculator", OWNER)

    target = service._target(windows[0]["target_id"])
    assert (target.pid, target.window_id) == (222, 2)
    assert [name for name, _args in fake_transport.calls].count("list_windows") == 4


def test_packaged_calculator_launch_binds_exact_package_window_under_shared_host(
    fake_client,
    fake_transport,
) -> None:
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
    service.grant_app_permission_for_local_ui(OWNER, "Windows Calculator")

    windows = service.launch_app("Windows Calculator", OWNER)

    assert len(windows) == 1
    target = service._target(windows[0]["target_id"])
    assert (target.app_name, target.pid, target.window_id) == (
        "Windows Calculator",
        777,
        7,
    )


def test_packaged_calculator_launch_rejects_untrusted_package_identity(
    fake_client,
    fake_transport,
) -> None:
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
    service.grant_app_permission_for_local_ui(OWNER, "Windows Calculator")

    with pytest.raises(ComputerUseError) as exc_info:
        service.launch_app("Windows Calculator", OWNER)

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


def test_classic_office_launch_stabilizes_from_start_surface_to_workbook(
    fake_client,
    fake_transport,
) -> None:
    fake_transport.scenario.apps = (
        {"name": "EXCEL.EXE", "running": False, "active": False},
    )
    transient = _window(1, "EXCEL.EXE", "Excel", pid=900)
    workbook = _window(2, "EXCEL.EXE", "Book1 - Excel", pid=900)
    fake_transport.scenario.window_snapshots = (
        (transient,),
        (workbook,),
        (workbook,),
        (workbook,),
    )
    fake_transport.scenario.launch_pid = 900
    fake_transport.scenario.launch_window_id = 1
    service = ComputerUseService(
        client_factory=lambda: fake_client,
        approval_callback=lambda _payload: True,
    )
    service.PACKAGED_LAUNCH_POLL_INTERVAL_SECONDS = 0.0
    service.acquire(OWNER, validate_context=False)

    windows = service.launch_app("EXCEL.EXE", OWNER)

    target = service._target(windows[0]["target_id"])
    assert (target.app_name, target.pid, target.window_id) == ("EXCEL.EXE", 900, 2)
    assert service.current_observation(target.target_id) is not None


def test_classic_launch_never_binds_unrelated_exact_app_decoy(
    fake_client,
    fake_transport,
) -> None:
    fake_transport.scenario.apps = ({"name": "EXCEL.EXE", "running": False},)
    workbook = _window(2, "EXCEL.EXE", "Book1 - Excel", pid=900)
    decoy = _window(3, "EXCEL.EXE", "Existing workbook - Excel", pid=901)
    fake_transport.scenario.window_snapshots = (
        (workbook, decoy),
        (workbook, decoy),
        (workbook, decoy),
    )
    fake_transport.scenario.launch_pid = 900
    fake_transport.scenario.launch_window_id = 2
    service = ComputerUseService(
        client_factory=lambda: fake_client,
        approval_callback=lambda _payload: True,
    )
    service.PACKAGED_LAUNCH_POLL_INTERVAL_SECONDS = 0.0
    service.acquire(OWNER, validate_context=False)

    windows = service.launch_app("EXCEL.EXE", OWNER)

    target = service._target(windows[0]["target_id"])
    assert (target.pid, target.window_id) == (900, 2)


def test_classic_launch_rejects_fuzzy_same_pid_fallback_row(
    fake_client,
    fake_transport,
) -> None:
    fake_transport.scenario.apps = ({"name": "EXCEL.EXE", "running": False},)
    fake_transport.scenario.windows = (
        _window(2, "Microsoft Excel", "Book1 - Excel", pid=900),
    )
    fake_transport.scenario.launch_pid = 900
    fake_transport.scenario.launch_window_id = 2
    service = ComputerUseService(
        client_factory=lambda: fake_client,
        approval_callback=lambda _payload: True,
    )
    service.PACKAGED_LAUNCH_STABILITY_TIMEOUT_SECONDS = 0.0
    service.acquire(OWNER, validate_context=False)

    with pytest.raises(ComputerUseError) as rejected:
        service.launch_app("EXCEL.EXE", OWNER)

    assert rejected.value.code == "target_gone"
    assert "get_window_state" not in [name for name, _args in fake_transport.calls]


def test_classic_launch_stabilization_wait_is_cancellable(
    fake_client,
    fake_transport,
    monkeypatch,
) -> None:
    fake_transport.scenario.apps = ({"name": "EXCEL.EXE", "running": False},)
    fake_transport.scenario.windows = ()
    fake_transport.scenario.launch_pid = 900
    fake_transport.scenario.launch_window_id = 2
    service = ComputerUseService(
        client_factory=lambda: fake_client,
        approval_callback=lambda _payload: True,
    )
    service.acquire(OWNER, validate_context=False)

    def cancel_during_wait(_timeout: float) -> bool:
        service._cancel.set()
        return True

    monkeypatch.setattr(service._cancel, "wait", cancel_during_wait)

    with pytest.raises(Exception) as cancelled:
        service.launch_app("EXCEL.EXE", OWNER)

    assert type(cancelled.value).__name__ == "CancelledError"
    assert "get_window_state" not in [name for name, _args in fake_transport.calls]


def test_launch_uses_exact_edge_identity_for_approval_and_driver(
    fake_client,
    fake_transport,
) -> None:
    approvals: list[dict] = []
    fake_transport.scenario.apps = (
        {"name": "msedge.exe", "running": True},
        {"name": "msedge.exe", "running": True},
    )
    fake_transport.scenario.windows = (
        _window(101, "msedge.exe", "Microsoft Edge", pid=4242),
    )
    service = ComputerUseService(
        client_factory=lambda: fake_client,
        approval_callback=lambda payload: approvals.append(payload) or True,
    )
    service.acquire(OWNER, validate_context=False)

    windows = service.launch_app("msedge.exe", OWNER)

    assert windows[0]["app"] == "msedge.exe"
    launch_args = next(args for name, args in fake_transport.calls if name == "launch_app")
    assert launch_args["name"] == "msedge.exe"
    assert len(approvals) == 1
    assert approvals[0]["app"] == "msedge.exe"
    assert "msedge.exe" in approvals[0]["label"]


def test_inventory_failure_does_not_trigger_a_browser_alias_fallback(
    fake_client,
    fake_transport,
) -> None:
    fake_transport.scenario.list_apps_error_code = "uwp_scan_failed"
    fake_transport.scenario.windows = (
        _window(101, "msedge.exe", "Microsoft Edge", pid=4242),
    )
    service = ComputerUseService(
        client_factory=lambda: fake_client,
        approval_callback=lambda _payload: True,
    )
    service.acquire(OWNER, validate_context=False)

    with pytest.raises(ComputerUseError):
        service.launch_app("msedge.exe", OWNER)

    assert "launch_app" not in [name for name, _args in fake_transport.calls]


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
        service.launch_app("msedge.exe", OWNER)

    assert exc_info.value.code == "driver_unavailable"
    names = [name for name, _args in fake_transport.calls]
    assert names.count("launch_app") == 1
    assert "list_windows" not in names
    assert "get_window_state" not in names
