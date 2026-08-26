from __future__ import annotations

import concurrent.futures
import json
import logging
import threading
from types import SimpleNamespace

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.prebuilt import create_react_agent

from row_bot.computer_use.client import CuaClient
from row_bot.computer_use.service import ComputerUseError, ComputerUseService, LeaseOwner
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


def test_codex_computer_graph_keeps_provider_parallel_batches_enabled() -> None:
    from row_bot.providers.transports.codex_responses import ChatCodexResponses

    binding_kwargs: list[dict] = []

    class RecordingCodexResponses(ChatCodexResponses):
        def bind_tools(self, tools, **kwargs):
            binding_kwargs.append(dict(kwargs))
            return super().bind_tools(tools, **kwargs)

    computer_tool = ComputerUseTool().as_langchain_tools()[0]
    model = RecordingCodexResponses(model_name="gpt-5.6-sol")

    create_react_agent(model=model, tools=[computer_tool])

    assert binding_kwargs == [{}]


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


def test_exact_replacement_is_atomic_token_bound_and_honors_explicit_vision_once(
    tmp_path,
    monkeypatch,
) -> None:
    scenario = FakeScenario(
        apps=({"name": "Form Studio", "running": True, "active": True},),
        windows=(
            {
                "window_id": 611,
                "pid": 2611,
                "app_name": "Form Studio",
                "title": "Untitled form",
                "bounds": {"x": 0, "y": 0, "width": 900, "height": 700},
                "is_on_screen": True,
            },
        ),
        capture_pid=2611,
        capture_window_id=611,
        semantic_elements=(
            {
                "role": "GridCell",
                "label": "Selected item",
                "enabled": True,
                "selected": True,
            },
        ),
        rotate_element_tokens=True,
    )
    _service, transport, vision, tool = _native_browser_tool(
        tmp_path,
        monkeypatch,
        scenario,
    )
    captured = json.loads(tool.invoke({"action": "capture", "app": "Form Studio"}))
    target_id = captured["fresh_observation"].split("Target ID: ", 1)[1].splitlines()[0]
    token = captured["fresh_observation"].split("token=", 1)[1].split(" ", 1)[0]
    replacement = "private complete value"

    replaced = json.loads(
        tool.invoke(
            {
                "action": "replace_text",
                "target_id": target_id,
                "element_token": token,
                "text": replacement,
                "capture_after": True,
                "visual_question": "Run the one explicitly requested advisory visual check.",
            }
        )
    )

    set_value_calls = [args for name, args in transport.calls if name == "set_value"]
    assert len(set_value_calls) == 1
    assert set_value_calls[0]["element_token"] == token
    assert set_value_calls[0]["value"] == f"<redacted:{len(replacement)} chars>"
    assert replaced["action_dispatched"] is True
    assert replaced["effect_verified"] is True
    assert replaced["action_completed"] is True
    assert replaced["outcome"] == "verified"
    assert replaced["semantic_postcondition"] == "matched"
    assert replaced["verified_scope"] == "exact_value"
    assert "computer_use_completion_blocked" not in replaced
    assert len(vision.calls) == 1
    assert all(name != "press_key" for name, _args in transport.calls)


def test_unverified_mutation_does_not_override_agent_final_status(
    tmp_path,
    monkeypatch,
) -> None:
    scenario = FakeScenario(
        apps=({"name": "Document App", "running": True, "active": True},),
        windows=(
            {
                "window_id": 615,
                "pid": 2615,
                "app_name": "Document App",
                "title": "Untitled document",
                "bounds": {"x": 0, "y": 0, "width": 900, "height": 700},
                "is_on_screen": True,
            },
        ),
        capture_pid=2615,
        capture_window_id=615,
        semantic_elements=(
            {"role": "Edit", "label": "Document body", "enabled": True},
        ),
        set_value_updates_document=False,
        delivery_profile="catalyst_value_unavailable",
    )
    service, _transport, _vision, tool = _native_browser_tool(
        tmp_path,
        monkeypatch,
        scenario,
    )
    captured = json.loads(tool.invoke({"action": "capture", "app": "Document App"}))
    target_id = captured["fresh_observation"].split("Target ID: ", 1)[1].splitlines()[0]
    token = captured["fresh_observation"].split("token=", 1)[1].split(" ", 1)[0]

    uncertain = json.loads(
        tool.invoke(
            {
                "action": "replace_text",
                "target_id": target_id,
                "element_token": token,
                "text": "private requested value",
                "capture_after": True,
            }
        )
    )
    assert uncertain["action_dispatched"] is True
    assert uncertain["effect_verified"] is False
    assert "computer_use_completion_blocked" not in uncertain
    assert not hasattr(service, "computer_use_completion_blocked")


def test_advisory_vision_and_stop_do_not_create_generation_completion_state(
    tmp_path,
    monkeypatch,
) -> None:
    scenario = FakeScenario(
        apps=({"name": "Document Surface", "running": True, "active": True},),
        windows=(
            {
                "window_id": 616,
                "pid": 2616,
                "app_name": "Document Surface",
                "title": "Untitled surface",
                "bounds": {"x": 0, "y": 0, "width": 900, "height": 700},
                "is_on_screen": True,
            },
        ),
        capture_pid=2616,
        capture_window_id=616,
        semantic_elements=(
            {
                "role": "Edit",
                "label": "Document item",
                "enabled": True,
                "selected": False,
            },
        ),
        set_value_updates_document=False,
        delivery_profile="catalyst_value_unavailable",
    )
    service, transport, vision, tool = _native_browser_tool(
        tmp_path,
        monkeypatch,
        scenario,
    )
    captured = json.loads(tool.invoke({"action": "capture", "app": "Document Surface"}))
    target_id = captured["fresh_observation"].split("Target ID: ", 1)[1].splitlines()[0]
    token = captured["fresh_observation"].split("token=", 1)[1].split(" ", 1)[0]

    uncertain = json.loads(
        tool.invoke(
            {
                "action": "replace_text",
                "target_id": target_id,
                "element_token": token,
                "text": "private complete value",
                "capture_after": True,
                "visual_question": "Describe the visible item after the mutation.",
            }
        )
    )
    stopped = tool.invoke({"action": "stop"})
    assert uncertain["action_dispatched"] is True
    assert uncertain["effect_verified"] is False
    assert len(vision.calls) == 1
    assert "stopped" in stopped.casefold()
    assert [name for name, _args in transport.calls].count("set_value") == 1
    assert "computer_use_completion_blocked" not in uncertain
    assert not hasattr(service, "computer_use_completion_blocked")


def test_tokenless_tabular_type_payload_is_one_literal_caret_action(
    tmp_path,
    monkeypatch,
) -> None:
    scenario = FakeScenario(
        apps=({"name": "Document App", "running": True, "active": True},),
        windows=(
            {
                "window_id": 612,
                "pid": 2612,
                "app_name": "Document App",
                "title": "Untitled document",
                "bounds": {"x": 0, "y": 0, "width": 900, "height": 700},
                "is_on_screen": True,
            },
        ),
        capture_pid=2612,
        capture_window_id=612,
        semantic_elements=(
            {"role": "Edit", "label": "Document body", "enabled": True},
        ),
    )
    service, transport, vision, tool = _native_browser_tool(
        tmp_path,
        monkeypatch,
        scenario,
    )
    captured = json.loads(tool.invoke({"action": "capture", "app": "Document App"}))
    target_id = captured["fresh_observation"].split("Target ID: ", 1)[1].splitlines()[0]
    calls_before = len(transport.calls)

    delivered = json.loads(
        tool.invoke(
            {
                "action": "type",
                "target_id": target_id,
                "text": "first\tsecond\nthird\tfourth",
            }
        )
    )

    mutation_calls = transport.calls[calls_before:]
    assert delivered["action_dispatched"] is True
    assert [name for name, _args in mutation_calls] == ["type_text"]
    assert mutation_calls[0][1]["text"] == "<redacted:25 chars>"
    assert "element_token" not in mutation_calls[0][1]
    assert all(
        name not in {"bring_to_front", "click", "get_window_state", "set_value"}
        for name, _args in mutation_calls
    )
    assert all(
        marker not in json.dumps(delivered).casefold()
        for marker in ("structured_layout", "clipboard", "shell")
    )
    assert service.current_observation(target_id) is not None
    assert vision.calls == []


def test_one_model_batch_of_three_computer_calls_allows_only_one_capture(
    tmp_path,
    monkeypatch,
    caplog,
) -> None:
    scenario = FakeScenario(
        apps=({"name": "Generic App", "running": True, "active": True},),
        windows=(
            {
                "window_id": 619,
                "pid": 2619,
                "app_name": "Generic App",
                "title": "Generic document",
                "bounds": {"x": 0, "y": 0, "width": 900, "height": 700},
                "is_on_screen": True,
            },
        ),
        capture_pid=2619,
        capture_window_id=619,
        semantic_elements=({"role": "Button", "label": "Current action"},),
        rotate_element_tokens=True,
    )
    service, transport, vision, tool = _native_browser_tool(
        tmp_path,
        monkeypatch,
        scenario,
    )
    initial = json.loads(tool.invoke({"action": "capture", "app": "Generic App"}))
    target_id = initial["fresh_observation"].split("Target ID: ", 1)[1].splitlines()[0]
    generation_before = service.current_observation(target_id).generation
    caplog.clear()

    capture_entered = threading.Event()
    release_capture = threading.Event()
    overlap_rejected = threading.Event()
    rejected_count = 0
    rejected_lock = threading.Lock()
    original_call_raw = transport.call_raw
    original_begin_tool_call = service.begin_tool_call

    def blocking_call_raw(name, arguments=None):
        if name == "get_window_state":
            capture_entered.set()
            assert release_capture.wait(timeout=5)
        return original_call_raw(name, arguments)

    def tracked_begin_tool_call(signature):
        nonlocal rejected_count
        try:
            return original_begin_tool_call(signature)
        except ComputerUseError as exc:
            if exc.code == "parallel_calls_not_supported":
                with rejected_lock:
                    rejected_count += 1
                    if rejected_count == 2:
                        overlap_rejected.set()
            raise

    monkeypatch.setattr(transport, "call_raw", blocking_call_raw)
    monkeypatch.setattr(service, "begin_tool_call", tracked_begin_tool_call)

    class ToolBatchModel(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            del tools, kwargs
            return self

    calls = [
        {
            "name": "computer_use",
            "args": {"action": "capture", "target_id": target_id},
            "id": f"capture-{index}",
            "type": "tool_call",
        }
        for index in range(3)
    ]
    graph = create_react_agent(
        model=ToolBatchModel(
            responses=[AIMessage(content="", tool_calls=calls), AIMessage(content="done")]
        ),
        tools=[tool],
        version="v2",
    )

    with caplog.at_level(logging.INFO, logger="row_bot.computer_use.service"):
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                graph.invoke,
                {"messages": [HumanMessage(content="Capture the current target three times.")]},
            )
            assert capture_entered.wait(timeout=5)
            assert overlap_rejected.wait(timeout=5)
            release_capture.set()
            result = future.result(timeout=10)

    payloads = [
        json.loads(message.content)
        for message in result["messages"]
        if isinstance(message, ToolMessage)
    ]
    assert sum(
        payload.get("error_code") == "parallel_calls_not_supported"
        for payload in payloads
    ) == 2
    assert sum("fresh_observation" in payload for payload in payloads) == 1
    assert [name for name, _args in transport.calls].count("get_window_state") == 2
    assert service.current_observation(target_id).generation == generation_before + 1
    assert vision.calls == []
    receipts = [
        record.message
        for record in caplog.records
        if record.message.startswith("computer_use.action_receipt ")
    ]
    assert len(receipts) == 1
    assert "driver_calls=1" in receipts[0]
    assert "capture_calls=1" in receipts[0]
    assert "vision_calls=0" in receipts[0]


def test_unverified_action_payload_allows_later_work_without_replay(
    tmp_path,
    monkeypatch,
) -> None:
    scenario = FakeScenario(
        apps=({"name": "Document App", "running": True, "active": True},),
        windows=(
            {
                "window_id": 613,
                "pid": 2613,
                "app_name": "Document App",
                "title": "Untitled document",
                "bounds": {"x": 0, "y": 0, "width": 900, "height": 700},
                "is_on_screen": True,
            },
        ),
        capture_pid=2613,
        capture_window_id=613,
        effect="unverifiable",
    )
    _service, transport, vision, tool = _native_browser_tool(
        tmp_path,
        monkeypatch,
        scenario,
    )
    captured = json.loads(tool.invoke({"action": "capture", "app": "Document App"}))
    target_id = captured["fresh_observation"].split("Target ID: ", 1)[1].splitlines()[0]

    result = json.loads(
        tool.invoke(
            {
                "action": "type",
                "target_id": target_id,
                "text": "one literal insertion",
            }
        )
    )

    assert result["action_dispatched"] is True
    assert result["effect_verified"] is False
    assert "must not be replayed" in result["next_action"]
    assert result["evidence"] == {
        "dispatch": "dispatched",
        "native_state": "unknown",
        "exact_postcondition": "not_verified",
        "verified_scope": "",
    }
    assert [name for name, _args in transport.calls].count("type_text") == 1
    assert vision.calls == []


def test_reversible_click_payload_allows_one_current_evidence_alternative_route(
    tmp_path,
    monkeypatch,
) -> None:
    unchanged_controls = (
        {"role": "Button", "label": "Play", "selected": False},
    )
    scenario = FakeScenario(
        apps=({"name": "Media App", "running": True, "active": True},),
        windows=(
            {
                "window_id": 614,
                "pid": 2614,
                "app_name": "Media App",
                "title": "Media",
                "bounds": {"x": 0, "y": 0, "width": 900, "height": 700},
                "is_on_screen": True,
            },
        ),
        capture_pid=2614,
        capture_window_id=614,
        effect="unverifiable",
        rotate_element_tokens=True,
        semantic_snapshots=(unchanged_controls, unchanged_controls),
    )
    _service, transport, vision, tool = _native_browser_tool(
        tmp_path,
        monkeypatch,
        scenario,
    )
    captured = json.loads(tool.invoke({"action": "capture", "app": "Media App"}))
    observation = captured["fresh_observation"]
    target_id = observation.split("Target ID: ", 1)[1].splitlines()[0]
    token = observation.split("token=", 1)[1].split(" ", 1)[0]

    result = json.loads(
        tool.invoke(
            {
                "action": "click",
                "target_id": target_id,
                "element_token": token,
                "capture_after": True,
            }
        )
    )

    assert result["action_dispatched"] is True
    assert result["native_change"] == "unchanged"
    assert result["effect_verified"] is False
    assert "one alternative exact route" in result["next_action"].casefold()
    assert "current evidence" in result["next_action"].casefold()
    assert [name for name, _args in transport.calls].count("click") == 1
    assert vision.calls == []


def test_approval_interrupt_logs_pending_then_one_completed_action(
    tmp_path,
    monkeypatch,
    caplog,
) -> None:
    import logging

    private_label = "Send private material"
    scenario = FakeScenario(
        apps=({"name": "Form Studio", "running": True, "active": True},),
        windows=(
            {
                "window_id": 614,
                "pid": 2614,
                "app_name": "Form Studio",
                "title": "Private form title",
                "bounds": {"x": 0, "y": 0, "width": 900, "height": 700},
                "is_on_screen": True,
            },
        ),
        capture_pid=2614,
        capture_window_id=614,
        semantic_elements=(
            {"role": "Button", "label": private_label, "enabled": True},
        ),
    )
    service, transport, _vision, tool = _native_browser_tool(
        tmp_path,
        monkeypatch,
        scenario,
    )
    monkeypatch.setattr(
        "row_bot.tools.approval_gate.current_approval_mode",
        lambda: "approve",
    )
    captured = json.loads(tool.invoke({"action": "capture", "app": "Form Studio"}))
    target_id = captured["fresh_observation"].split("Target ID: ", 1)[1].splitlines()[0]
    token = captured["fresh_observation"].split("token=", 1)[1].split(" ", 1)[0]
    caplog.clear()

    class GraphInterrupt(RuntimeError):
        pass

    service._approval = lambda _payload: (_ for _ in ()).throw(GraphInterrupt("pending"))
    with caplog.at_level(logging.INFO, logger="row_bot.computer_use.service"):
        with pytest.raises(GraphInterrupt):
            tool.invoke(
                {
                    "action": "click",
                    "target_id": target_id,
                    "element_token": token,
                }
            )
        service._approval = lambda _payload: True
        completed = json.loads(
            tool.invoke(
                {
                    "action": "click",
                    "target_id": target_id,
                    "element_token": token,
                }
            )
        )

    pending = [
        record.message
        for record in caplog.records
        if record.message.startswith("computer_use.action_pending ")
    ]
    receipts = [
        record.message
        for record in caplog.records
        if record.message.startswith("computer_use.action_receipt ")
    ]
    assert len(pending) == 1
    assert "status=approval_pending" in pending[0]
    assert "success=" not in pending[0]
    assert len(receipts) == 1
    assert "success=true" in receipts[0]
    assert [name for name, _args in transport.calls].count("click") == 1
    assert completed["action_completed"] is False
    assert completed["driver_effect"] == "confirmed"
    assert completed["effect_verified"] is False
    assert completed["action_outcome"] == "delivered_unverified"
    assert completed["verified_scope"] == ""
    assert completed["evidence"]["exact_postcondition"] == "not_verified"
    diagnostics = "\n".join(pending + receipts)
    assert private_label not in diagnostics
    assert token not in diagnostics
    assert "Private form title" not in diagnostics


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
        "app": "msedge.exe",
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
    assert "msedge.exe" in approvals[0]["label"]
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


def test_generic_semantic_control_flow_uses_no_vision_until_explicitly_requested(
    tmp_path,
    monkeypatch,
) -> None:
    scenario = FakeScenario(
        apps=({"name": "Generic Client", "running": True, "active": True},),
        windows=(
            {
                "window_id": 620,
                "pid": 2620,
                "app_name": "Generic Client",
                "title": "Generic content",
                "bounds": {"x": 0, "y": 0, "width": 900, "height": 700},
                "is_on_screen": True,
            },
        ),
        capture_pid=2620,
        capture_window_id=620,
        semantic_elements=(
            {"role": "ComboBox", "label": "Search", "value": ""},
            {"role": "DataItem", "label": "Result"},
            {"role": "Button", "label": "Queue items"},
            {"role": "Button", "label": "Next item"},
            {"role": "Button", "label": "Pause item"},
        ),
    )
    service, transport, vision, tool = _native_browser_tool(
        tmp_path,
        monkeypatch,
        scenario,
    )

    captured = json.loads(tool.invoke({"action": "capture", "app": "Generic Client"}))
    observation = captured["fresh_observation"]
    target_id = observation.split("Target ID: ", 1)[1].splitlines()[0]
    tokens = {
        line.split('label="', 1)[1].split('"', 1)[0]: line.split("token=", 1)[1].split(" ", 1)[0]
        for line in observation.splitlines()
        if line.startswith("- token=")
    }
    tool.invoke(
        {
            "action": "replace_text",
            "target_id": target_id,
            "element_token": tokens["Search"],
            "text": "bounded query",
        }
    )
    for label in ("Result", "Queue items", "Next item", "Pause item"):
        tool.invoke(
            {
                "action": "click",
                "target_id": target_id,
                "element_token": tokens[label],
            }
        )
    routine_refresh = json.loads(
        tool.invoke({"action": "capture", "target_id": target_id})
    )

    assert "fresh_observation" in routine_refresh
    assert vision.calls == []
    assert service.performance_snapshot()["vision_calls"] == 0

    visual = json.loads(
        tool.invoke(
            {
                "action": "capture",
                "target_id": target_id,
                "visual_question": "Describe the one pixel-only detail.",
            }
        )
    )
    assert "vision_evidence" in visual
    assert vision.calls == ["Describe the one pixel-only detail."]
    assert [name for name, _args in transport.calls].count("get_window_state") == 3


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
            assert "delivery_mode" not in arguments


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
    assert enter["action_completed"] is False
    assert enter["effect_verified"] is False
    assert "error_code" not in enter
    assert "vision_evidence" in final
    names = [name for name, _args in transport.calls]
    assert names.count("bring_to_front") == 1
    assert names.count("type_text") == 1
    assert names.count("press_key") == 3
    assert names.count("get_window_state") == 2
    assert transport.effective_keys == []
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
