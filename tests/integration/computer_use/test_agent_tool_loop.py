from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from row_bot.computer_use.client import CuaClient
from row_bot.computer_use.service import ComputerUseService, LeaseOwner
from row_bot.tools import computer_use_tool
from row_bot.tools.computer_use_tool import ComputerUseTool
from tests.fixtures.fake_cua import (
    SANITIZED_NATIVE_BROWSER_APPS,
    SANITIZED_NATIVE_BROWSER_WINDOWS,
    FakeCuaTransport,
    FakeScenario,
)


class _CountingVision:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def analyze(self, _image: bytes, question: str) -> str:
        self.calls.append(question)
        return "The requested visible state is described by this sanitized fake."


def _native_browser_tool(tmp_path, monkeypatch, scenario: FakeScenario):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path))
    from row_bot.computer_use.readiness import ReadinessCode, acknowledge_disclosure

    acknowledge_disclosure()
    transport = FakeCuaTransport(scenario)
    client = CuaClient(
        "fake.exe",
        session_id="native-performance",
        transport_factory=lambda *_args: transport,
    )
    vision = _CountingVision()
    service = ComputerUseService(
        client_factory=lambda: client,
        approval_callback=lambda _payload: True,
        vision_service=vision,
    )
    owner = LeaseOwner("performance-thread", "performance-generation", "performance-task")
    service.acquire(owner, validate_context=False)
    monkeypatch.setattr("row_bot.computer_use.service.current_owner", lambda: owner)
    monkeypatch.setattr(computer_use_tool, "get_computer_use_service", lambda: service)
    monkeypatch.setattr(
        "row_bot.computer_use.readiness.readiness",
        lambda **_kwargs: SimpleNamespace(
            code=ReadinessCode.READY,
            message="ready",
            remediation="",
        ),
    )
    monkeypatch.setattr(
        "row_bot.tools.approval_gate.current_approval_mode",
        lambda: "allow_all",
    )
    return service, transport, vision, ComputerUseTool().as_langchain_tools()[0]


def test_single_tool_runs_discovery_capture_and_verified_action(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path))
    from row_bot.computer_use.readiness import acknowledge_disclosure
    acknowledge_disclosure()
    transport = FakeCuaTransport(FakeScenario(calculator_semantics=True))
    client = CuaClient("fake.exe", session_id="integration", transport_factory=lambda *_args: transport)
    service = ComputerUseService(client_factory=lambda: client, approval_callback=lambda _payload: True)
    owner = LeaseOwner("thread", "generation", "task")
    service.acquire(owner, validate_context=False)
    monkeypatch.setattr("row_bot.computer_use.service.current_owner", lambda: owner)
    monkeypatch.setattr(computer_use_tool, "get_computer_use_service", lambda: service)
    from row_bot.computer_use.readiness import ReadinessCode
    monkeypatch.setattr("row_bot.computer_use.readiness.readiness", lambda **_kwargs: SimpleNamespace(code=ReadinessCode.READY, message="ready", remediation=""))
    tools = ComputerUseTool().as_langchain_tools()
    tool = tools[0]

    apps = json.loads(tool.invoke({"action": "list_apps"}))
    windows = json.loads(tool.invoke({"action": "list_windows", "app": "Calculator"}))
    target = windows["windows"][0]["target_id"]
    captured = json.loads(tool.invoke({"action": "capture", "target_id": target}))
    token = captured["fresh_observation"].split("token=", 1)[1].split(" ", 1)[0]
    verified = json.loads(
        tool.invoke({
            "action": "click",
            "target_id": target,
            "element_token": token,
            "capture_after": True,
        })
    )

    assert apps["apps"][0]["name"] == "Calculator"
    assert "Calculator" in verified["fresh_observation"]
    assert verified["capture_is_fresh"] is True
    assert [name for name, _args in transport.calls].count("click") == 1
    assert transport.calls[-1][0] == "get_window_state"


def test_existing_edge_window_tool_loop_preserves_scope_approval_and_target(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path))
    from row_bot.computer_use.readiness import ReadinessCode, acknowledge_disclosure

    acknowledge_disclosure()
    scenario = FakeScenario(
        apps=({"name": "msedge.exe", "running": True},),
        capture_pid=2501,
        capture_window_id=501,
        windows=(
            {
                "window_id": 501,
                "pid": 2501,
                "app_name": "msedge.exe",
                "title": "YouTube - Microsoft Edge",
                "bounds": {"x": 0, "y": 0, "width": 1280, "height": 720},
                "is_on_screen": True,
            },
            {
                "window_id": 502,
                "pid": 2501,
                "app_name": "msedge.exe",
                "title": "Private mail - Microsoft Edge",
                "bounds": {"x": 0, "y": 0, "width": 1280, "height": 720},
                "is_on_screen": True,
            },
        ),
    )
    transport = FakeCuaTransport(scenario)
    client = CuaClient(
        "fake.exe",
        session_id="native-browser",
        transport_factory=lambda *_args: transport,
    )
    approvals: list[dict] = []
    service = ComputerUseService(
        client_factory=lambda: client,
        approval_callback=lambda payload: approvals.append(payload) or True,
    )
    owner = LeaseOwner("thread", "generation", "task")
    service.acquire(owner, validate_context=False)
    monkeypatch.setattr("row_bot.computer_use.service.current_owner", lambda: owner)
    monkeypatch.setattr(computer_use_tool, "get_computer_use_service", lambda: service)
    monkeypatch.setattr(
        "row_bot.computer_use.readiness.readiness",
        lambda **_kwargs: SimpleNamespace(
            code=ReadinessCode.READY,
            message="ready",
            remediation="",
        ),
    )
    tool = ComputerUseTool().as_langchain_tools()[0]

    listed = json.loads(tool.invoke({
        "action": "list_windows",
        "app": "Microsoft Edge",
        "window_hint": "YouTube",
    }))
    assert len(listed["windows"]) == 1
    target_id = listed["windows"][0]["target_id"]
    captured = json.loads(tool.invoke({"action": "capture", "target_id": target_id}))
    scrolled = json.loads(tool.invoke({
        "action": "scroll",
        "target_id": target_id,
        "direction": "down",
        "amount": 240,
    }))

    assert f"Target ID: {target_id}" in captured["fresh_observation"]
    assert scrolled["target_id"] == target_id
    assert len(approvals) == 1
    assert approvals[0]["app"] == "msedge.exe"
    assert "Microsoft Edge" in approvals[0]["label"]
    names = [name for name, _args in transport.calls]
    assert names.count("list_windows") == 1
    assert names.count("get_window_state") == 1
    assert names.count("scroll") == 1
    assert "launch_app" not in names


def test_calculator_fast_path_needs_only_three_model_tool_calls(tmp_path, monkeypatch) -> None:
    natural_prompt = (
        "Use Computer Use only. Open Calculator, calculate 7 × 8, verify that the "
        "display is 56 with a fresh capture, then stop. Do not use Browser, Shell, "
        "clipboard, or filesystem tools."
    )
    assert "key_sequence" not in natural_prompt
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path))
    from row_bot.computer_use.readiness import ReadinessCode, acknowledge_disclosure

    acknowledge_disclosure()
    transport = FakeCuaTransport(FakeScenario(calculator_semantics=True))
    client = CuaClient(
        "fake.exe",
        session_id="fast-path",
        transport_factory=lambda *_args: transport,
    )
    service = ComputerUseService(
        client_factory=lambda: client,
        approval_callback=lambda _payload: True,
    )
    owner = LeaseOwner("thread", "generation", "task")
    service.acquire(owner, validate_context=False)
    monkeypatch.setattr("row_bot.computer_use.service.current_owner", lambda: owner)
    monkeypatch.setattr(computer_use_tool, "get_computer_use_service", lambda: service)
    monkeypatch.setattr(
        "row_bot.computer_use.readiness.readiness",
        lambda **_kwargs: SimpleNamespace(
            code=ReadinessCode.READY,
            message="ready",
            remediation="",
        ),
    )
    tool = ComputerUseTool().as_langchain_tools()[0]
    model_calls = []

    launch_args = {"action": "launch_app", "app": "Calculator"}
    model_calls.append(launch_args)
    launched = json.loads(tool.invoke(launch_args))
    assert launched["capture_required"] is False
    assert "Computer" in launched["fresh_observation"]
    target_id = launched["windows"][0]["target_id"]

    sequence_args = {
        "action": "key_sequence",
        "target_id": target_id,
        "keys": "7,*,8,=",
    }
    model_calls.append(sequence_args)
    verified = json.loads(tool.invoke(sequence_args))
    assert "Display 56" in verified["fresh_observation"]
    assert verified["capture_is_fresh"] is True
    assert "call stop now" in verified["next_action"]
    assert "do not capture again" in verified["next_action"]

    stop_args = {"action": "stop"}
    model_calls.append(stop_args)
    assert "stopped" in tool.invoke(stop_args).lower()

    assert len(model_calls) == 3
    assert [call["action"] for call in model_calls] == [
        "launch_app",
        "key_sequence",
        "stop",
    ]
    assert [name for name, _args in transport.calls].count("get_window_state") == 2
    assert [name for name, _args in transport.calls].count("list_windows") == 3


def test_generic_notepad_wait_after_approval_surface_uses_existing_lease(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path))
    from row_bot.computer_use import service as service_module
    from row_bot.computer_use.readiness import ReadinessCode, acknowledge_disclosure

    acknowledge_disclosure()
    transport = FakeCuaTransport()
    client = CuaClient(
        "fake.exe",
        session_id="generic-wait",
        transport_factory=lambda *_args: transport,
    )
    service = ComputerUseService(
        client_factory=lambda: client,
        approval_callback=lambda _payload: True,
    )
    owner = LeaseOwner("thread", "generation", "task")
    service.acquire(owner, validate_context=False)
    monkeypatch.setattr("row_bot.computer_use.service.current_owner", lambda: owner)
    monkeypatch.setattr(computer_use_tool, "get_computer_use_service", lambda: service)
    monkeypatch.setattr(
        "row_bot.computer_use.readiness.readiness",
        lambda **_kwargs: SimpleNamespace(
            code=ReadinessCode.READY,
            message="ready",
            remediation="",
        ),
    )
    clock = [10.0]
    monkeypatch.setattr(service_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        service_module.time,
        "sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )
    tool = ComputerUseTool().as_langchain_tools()[0]

    windows = json.loads(tool.invoke({"action": "list_windows", "app": "Notepad"}))
    target_id = windows["windows"][0]["target_id"]
    json.loads(tool.invoke({"action": "capture", "target_id": target_id}))
    calls_before = len(transport.calls)

    waited = json.loads(
        tool.invoke({"action": "wait", "target_id": target_id, "amount": 8_000})
    )

    assert waited["capture_is_fresh"] is True
    assert "Notepad" in waited["fresh_observation"]
    assert waited["display_summary"] == (
        "Waited on the selected target and captured a fresh observation."
    )
    assert [name for name, _args in transport.calls[calls_before:]] == [
        "get_window_state"
    ]


def test_native_foreground_discovery_uses_active_metadata_without_vision(
    tmp_path,
    monkeypatch,
) -> None:
    scenario = FakeScenario(
        apps=SANITIZED_NATIVE_BROWSER_APPS,
        windows=SANITIZED_NATIVE_BROWSER_WINDOWS,
    )
    _service, transport, vision, tool = _native_browser_tool(
        tmp_path,
        monkeypatch,
        scenario,
    )

    inventory = json.loads(tool.invoke({"action": "list_apps"}))
    foreground_app = inventory["foreground"]["app"]
    windows = json.loads(tool.invoke({
        "action": "list_windows",
        "app": foreground_app,
        "window_hint": "Example media",
    }))

    assert inventory["foreground"] == {"status": "known", "app": "msedge.exe"}
    assert inventory["foreground_unknown"] is False
    assert len(windows["windows"]) == 1
    assert vision.calls == []
    names = [name for name, _args in transport.calls]
    assert names.count("list_apps") == 1
    assert names.count("list_windows") == 1
    assert "launch_app" not in names
    assert "get_window_state" not in names


@pytest.mark.parametrize(
    "apps",
    [
        (
            {"name": "msedge.exe", "running": True, "active": False},
            {"name": "Notepad", "running": True, "active": False},
        ),
        (
            {"name": "msedge.exe", "running": True, "active": True},
            {"name": "Notepad", "running": True, "active": True},
        ),
    ],
)
def test_missing_or_ambiguous_native_foreground_is_explicitly_unknown(
    tmp_path,
    monkeypatch,
    apps,
) -> None:
    _service, transport, vision, tool = _native_browser_tool(
        tmp_path,
        monkeypatch,
        FakeScenario(apps=apps),
    )

    inventory = json.loads(tool.invoke({"action": "list_apps"}))

    assert inventory["foreground"] == {"status": "foreground_unknown", "app": ""}
    assert inventory["foreground_unknown"] is True
    assert "user-provided app/title hint or Take over" in inventory["next_action"]
    assert vision.calls == []
    assert [name for name, _args in transport.calls].count("list_apps") == 1
    assert "list_windows" not in [name for name, _args in transport.calls]


def test_three_action_capability_flow_meets_computer_and_vision_budgets(
    tmp_path,
    monkeypatch,
) -> None:
    scenario = FakeScenario(
        apps=SANITIZED_NATIVE_BROWSER_APPS,
        windows=SANITIZED_NATIVE_BROWSER_WINDOWS,
        capture_pid=2501,
        capture_window_id=501,
    )
    service, transport, vision, tool = _native_browser_tool(
        tmp_path,
        monkeypatch,
        scenario,
    )
    computer_calls: list[dict] = []

    def invoke(payload: dict):
        computer_calls.append(payload)
        return json.loads(tool.invoke(payload)) if payload["action"] != "stop" else tool.invoke(payload)

    inventory = invoke({"action": "list_apps"})
    windows = invoke({
        "action": "list_windows",
        "app": inventory["foreground"]["app"],
        "window_hint": "Example media",
    })
    target_id = windows["windows"][0]["target_id"]
    invoke({
        "action": "capture",
        "target_id": target_id,
        "visual_question": "Identify a safe reversible test surface.",
    })
    invoke({"action": "focus", "target_id": target_id})
    invoke({"action": "type", "target_id": target_id, "text": "sanitized test"})
    invoke({"action": "key", "target_id": target_id, "keys": "tab"})
    invoke({"action": "scroll", "target_id": target_id, "direction": "down", "amount": 3})
    final = invoke({
        "action": "capture",
        "target_id": target_id,
        "visual_question": "Describe the final visible test state.",
    })
    invoke({"action": "stop"})

    assert len(computer_calls) == 9
    assert len(vision.calls) == 2
    assert service.performance_snapshot()["captures"] == 2
    assert "vision_evidence" in final
    names = [name for name, _args in transport.calls]
    assert names.count("bring_to_front") == 1
    assert names.count("get_window_state") == 2
    assert names.count("type_text") == 1
    assert names.count("press_key") == 1
    assert names.count("scroll") == 1
    for name, arguments in transport.calls:
        if name in {"type_text", "press_key", "scroll"}:
            assert arguments["delivery_mode"] == "foreground"


def test_native_browser_search_play_pause_flow_meets_budgets_without_blind_retry(
    tmp_path,
    monkeypatch,
) -> None:
    scenario = FakeScenario(
        apps=SANITIZED_NATIVE_BROWSER_APPS,
        windows=SANITIZED_NATIVE_BROWSER_WINDOWS,
        capture_pid=2501,
        capture_window_id=501,
        accepted_background_noop_tools=frozenset({"press_key"}),
    )
    service, transport, vision, tool = _native_browser_tool(
        tmp_path,
        monkeypatch,
        scenario,
    )
    computer_calls: list[dict] = []

    def invoke(payload: dict):
        computer_calls.append(payload)
        return json.loads(tool.invoke(payload)) if payload["action"] != "stop" else tool.invoke(payload)

    inventory = invoke({"action": "list_apps"})
    windows = invoke({
        "action": "list_windows",
        "app": inventory["foreground"]["app"],
        "window_hint": "Example media",
    })
    target_id = windows["windows"][0]["target_id"]
    invoke({
        "action": "capture",
        "target_id": target_id,
        "visual_question": "Locate the current native browser content state.",
    })
    invoke({"action": "focus", "target_id": target_id})
    invoke({"action": "type", "target_id": target_id, "text": "sanitized query"})
    enter = invoke({"action": "key", "target_id": target_id, "keys": "enter"})
    invoke({"action": "key", "target_id": target_id, "keys": "space"})
    invoke({"action": "key", "target_id": target_id, "keys": "space"})
    final = invoke({
        "action": "capture",
        "target_id": target_id,
        "visual_question": "Is the final requested native browser state visible?",
    })
    invoke({"action": "stop"})

    assert len(computer_calls) == 10 <= 12
    assert len(vision.calls) == 2 <= 3
    assert service.performance_snapshot()["captures"] == 2
    assert enter["action_dispatched"] is True
    assert enter["action_completed"] is True
    assert "error_code" not in enter
    assert "vision_evidence" in final
    names = [name for name, _args in transport.calls]
    assert names.count("bring_to_front") == 1
    assert names.count("type_text") == 1
    assert names.count("press_key") == 3
    assert names.count("get_window_state") == 2
    assert transport.effective_keys == ["enter", "space", "space"]
    assert "launch_app" not in names


def test_structured_native_browser_error_stays_in_computer_without_fallback(
    tmp_path,
    monkeypatch,
) -> None:
    scenario = FakeScenario(
        apps=SANITIZED_NATIVE_BROWSER_APPS,
        windows=SANITIZED_NATIVE_BROWSER_WINDOWS,
        capture_pid=2501,
        capture_window_id=501,
    )
    _service, transport, vision, tool = _native_browser_tool(
        tmp_path,
        monkeypatch,
        scenario,
    )
    inventory = json.loads(tool.invoke({"action": "list_apps"}))
    windows = json.loads(tool.invoke({
        "action": "list_windows",
        "app": inventory["foreground"]["app"],
        "window_hint": "Example media",
    }))
    target_id = windows["windows"][0]["target_id"]
    json.loads(tool.invoke({"action": "capture", "target_id": target_id}))
    scenario.action_error_code = "focus_failed"

    failed = json.loads(tool.invoke({"action": "focus", "target_id": target_id}))

    assert failed["ok"] is False
    assert failed["error"] is True
    assert failed["error_code"] == "driver_failed"
    assert vision.calls == []
    names = [name for name, _args in transport.calls]
    assert names.count("bring_to_front") == 1
    assert "launch_app" not in names
    assert all(not name.startswith("browser_") for name in names)
