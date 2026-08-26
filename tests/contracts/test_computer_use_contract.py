from __future__ import annotations

import json

from row_bot.computer_use.client import ALLOWED_CUA_TOOLS, FORBIDDEN_TOOL_FAMILIES, MODEL_ACTION_TO_CUA
from row_bot.computer_use.service import ComputerUseError
from row_bot.tools.computer_use_tool import ComputerUseInput, ComputerUseTool, _computer_error_payload
from row_bot.providers.models import TransportMode
from row_bot.providers.tool_schema import apply_tool_schema_compatibility


def test_model_tool_is_one_flat_provider_neutral_schema() -> None:
    tools = ComputerUseTool().as_langchain_tools()
    assert [tool.name for tool in tools] == ["computer_use"]
    schema = ComputerUseInput.model_json_schema()
    assert schema["type"] == "object"
    assert schema["required"] == ["action"]
    assert "$defs" not in schema
    assert all(spec.get("type") in {"string", "integer", "boolean"} for spec in schema["properties"].values())
    assert "text" in schema["properties"]
    assert schema["properties"]["capture_after"]["type"] == "boolean"
    assert "key_sequence" in schema["properties"]["action"]["description"]
    assert "comma-separated" in schema["properties"]["keys"]["description"]
    assert "7,*,8,=" in schema["properties"]["keys"]["description"]
    assert "compact" in schema["properties"]["keys"]["description"].lower()
    assert "current caret" in schema["properties"]["text"]["description"]
    assert "complete value" in schema["properties"]["element_token"]["description"]
    assert "Before the first coordinate-only visual action" in schema["properties"]["visual_question"]["description"]
    assert "semantic actions deliberately skip Vision" in schema["properties"]["visual_question"]["description"]
    assert "visual_question" in ComputerUseTool().description
    assert "never guess coordinates" in ComputerUseTool().description


def test_key_sequence_driver_failure_is_not_misreported_as_invalid_input() -> None:
    payload = json.loads(
        _computer_error_payload(
            "key_sequence",
            ComputerUseError("The operation completed successfully. (0x00000000)"),
        )
    )

    assert payload["error_code"] == "driver_failed"
    assert payload["display_summary"] == "Computer action failed safely."


def test_beta_action_map_and_internal_allowlist_exclude_maintenance() -> None:
    assert set(MODEL_ACTION_TO_CUA) == {
        "list_apps", "list_windows", "launch_app", "capture", "focus",
        "click", "double_click", "right_click", "type", "key", "scroll", "drag",
    }
    assert ALLOWED_CUA_TOOLS.isdisjoint(FORBIDDEN_TOOL_FAMILIES)
    assert "set_config" in ALLOWED_CUA_TOOLS
    assert "check_for_update" not in ALLOWED_CUA_TOOLS
    assert "start_recording" not in ALLOWED_CUA_TOOLS


def test_computer_use_is_off_by_default() -> None:
    assert ComputerUseTool().enabled_by_default is False
    assert ComputerUseTool().destructive_tool_names == set()


def test_flat_schema_survives_every_provider_transport_policy() -> None:
    tool = ComputerUseTool().as_langchain_tools()[0]
    for transport in TransportMode:
        result = apply_tool_schema_compatibility([tool], transport, explicitly_requested_names=["computer_use"])
        assert [item.name for item in result.tools] == ["computer_use"]
        assert result.rejected_tool_names == ()


def test_computer_tool_errors_are_structured_and_privacy_safe(tmp_path, monkeypatch) -> None:
    from types import SimpleNamespace

    from row_bot.computer_use.client import CuaClient
    from row_bot.computer_use.readiness import ReadinessCode, acknowledge_disclosure
    from row_bot.computer_use.service import ComputerUseService, LeaseOwner
    from row_bot.tools import computer_use_tool
    from tests.fixtures.fake_cua import FakeCuaTransport

    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path))
    acknowledge_disclosure()
    transport = FakeCuaTransport()
    client = CuaClient("fake.exe", session_id="errors", transport_factory=lambda *_args: transport)
    service = ComputerUseService(client_factory=lambda: client, approval_callback=lambda _payload: True)
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

    payload = json.loads(tool.invoke({"action": "capture"}))

    assert payload["ok"] is False
    assert payload["error"] is True
    assert payload["error_code"] == "invalid_input"
    assert payload["retryable"] is False
    assert "target_id" not in payload["display_summary"]
    assert "arguments" not in payload


def test_direct_self_target_block_is_terminal_before_cua_starts(tmp_path, monkeypatch) -> None:
    from types import SimpleNamespace

    from row_bot.computer_use.client import CuaClient
    from row_bot.computer_use.readiness import ReadinessCode, acknowledge_disclosure
    from row_bot.computer_use.service import ComputerUseService, LeaseOwner
    from row_bot.tools import computer_use_tool
    from tests.fixtures.fake_cua import FakeCuaTransport

    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path))
    acknowledge_disclosure()
    transport = FakeCuaTransport()
    client = CuaClient(
        "fake.exe",
        session_id="protected",
        transport_factory=lambda *_args: transport,
    )
    service = ComputerUseService(client_factory=lambda: client)
    owner = LeaseOwner("thread", "generation", "task")
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

    payload = json.loads(
        ComputerUseTool().as_langchain_tools()[0].invoke({
            "action": "list_windows",
            "app": "Row-Bot",
            "window_hint": "Row-Bot",
        })
    )

    assert payload["error_code"] == "hard_blocked"
    assert payload["terminal"] is True
    assert payload["retryable"] is False
    assert transport.opened is False
    assert transport.calls == []
