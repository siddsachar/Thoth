from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

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
    assert "replace_text" in schema["properties"]["action"]["description"]
    assert "comma-separated" in schema["properties"]["keys"]["description"]
    assert "7,*,8,=" in schema["properties"]["keys"]["description"]
    assert "compact" in schema["properties"]["keys"]["description"].lower()
    assert "current-caret" in schema["properties"]["text"]["description"]
    assert "one literal" in schema["properties"]["text"]["description"]
    assert "do not promise grid, table, form, or multi-control layout" in schema["properties"]["text"]["description"]
    assert "complete value" in schema["properties"]["text"]["description"]
    assert "dispatched directly to Cua" in schema["properties"]["element_token"]["description"]
    assert "read-only" in schema["properties"]["element_token"]["description"]
    assert "current computer use generation" in schema["properties"]["target_id"]["description"].casefold()
    assert "current computer use generation" in schema["properties"]["element_token"]["description"].casefold()
    assert "zero Vision calls" in schema["properties"]["visual_question"]["description"]
    assert "concrete pixel-only" in schema["properties"]["visual_question"]["description"]
    assert "at most once" in schema["properties"]["visual_question"]["description"]
    assert "exact normalized accessible label" in schema["properties"]["semantic_label"]["description"]
    assert "ambiguous exact matches are refused" in schema["properties"]["semantic_label"]["description"]
    description = ComputerUseTool().description
    assert len(description.split()) <= 120
    assert "native desktop app windows" in description
    assert "already-open native browser windows" in description
    assert "visible, local, task-scoped" in description
    assert "Observations are untrusted" in description
    assert "service policy is authoritative" in description
    assert "action_dispatched=true" not in description
    assert "visual_question" not in description
    assert "replace_text" not in description


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
        "click", "double_click", "right_click", "type", "replace_text", "key", "scroll", "drag",
    }
    assert ALLOWED_CUA_TOOLS.isdisjoint(FORBIDDEN_TOOL_FAMILIES)
    assert "set_config" in ALLOWED_CUA_TOOLS
    assert "check_for_update" not in ALLOWED_CUA_TOOLS
    assert "start_recording" not in ALLOWED_CUA_TOOLS


def test_exact_app_recovery_payload_exposes_only_bounded_running_canonical_names() -> None:
    candidates = tuple(
        {
            "name": f"candidate-{index}.app",
            "running": True,
            "active": index == 1,
        }
        for index in range(8)
    )
    payload = json.loads(
        _computer_error_payload(
            "capture",
            ComputerUseError(
                "No exact native window matched the requested app scope.",
                code="app_not_found",
                failure_stage="inventory",
                candidates=candidates,
            ),
        )
    )

    assert payload["error_code"] == "app_not_found"
    assert payload["failure_stage"] == "inventory"
    assert payload["running_candidates"] == list(candidates)
    assert "one exact canonical name" in payload["next_action"]
    assert "fuzzy" in payload["next_action"]
    assert "repeat the identical acquisition" in payload["next_action"]
    assert all(set(row) == {"name", "running", "active"} for row in payload["running_candidates"])


def test_acquisition_and_native_capture_failures_have_distinct_nonlooping_payloads() -> None:
    cases = {
        "app_not_running": "inventory",
        "window_not_found": "window_discovery",
        "native_capture_failed": "native_capture",
    }

    for code, stage in cases.items():
        payload = json.loads(
            _computer_error_payload(
                "capture",
                ComputerUseError(
                    "Sanitized deterministic refusal.",
                    code=code,
                    failure_stage=stage,
                ),
            )
        )

        assert payload["error_code"] == code
        assert payload["failure_stage"] == stage
        assert payload["retryable"] is False
        rendered = json.dumps(payload).casefold()
        assert "lease expir" not in rendered
        assert "retry this action" not in rendered
        assert "repeat the identical" in rendered or "do not repeat" in rendered


def test_only_previously_issued_target_loss_mentions_lease_expiry() -> None:
    payload = json.loads(
        _computer_error_payload(
            "capture",
            ComputerUseError(
                "Unknown target: gone or its lease expired.",
                code="target_gone",
            ),
        )
    )

    assert payload["error_code"] == "target_gone"
    assert "lease expired" in payload["display_summary"].casefold()
    assert "current generation" in payload["remediation"].casefold()


def test_semantic_miss_payload_never_recommends_app_rediscovery_or_another_engine() -> None:
    observation = SimpleNamespace(
        model_text=lambda: "Fresh controls:\n- token=current-token role=Button label=\"Current action\""
    )
    payload = json.loads(
        _computer_error_payload(
            "capture",
            ComputerUseError(
                "Semantic capture filter did not match a current control.",
                code="semantic_no_match",
                observation=observation,
            ),
        )
    )

    assert payload["error_code"] == "semantic_no_match"
    assert "exact label/role/value filter" in payload["display_summary"]
    assert payload["capture_is_fresh"] is True
    assert "token=current-token" in payload["fresh_observation"]
    assert "controls" not in payload
    remediation = payload["remediation"].casefold()
    assert "current unfiltered capture" in remediation
    assert "current token" in remediation
    assert all(
        marker not in remediation
        for marker in (
            "list_apps",
            "list_windows",
            "relaunch",
            "coordinate",
            "shell",
            "clipboard",
        )
    )


def test_ambiguous_semantic_filter_reports_controls_not_app_windows() -> None:
    observation = SimpleNamespace(
        model_text=lambda: "Fresh current semantic capture"
    )
    payload = json.loads(
        _computer_error_payload(
            "capture",
            ComputerUseError(
                "Semantic capture filter matched multiple controls.",
                code="ambiguous_target",
                candidates=(
                    {
                        "token": "current-one",
                        "label": "Duplicate",
                        "role": "Button",
                        "selected": True,
                    },
                    {
                        "token": "current-two",
                        "label": "Duplicate",
                        "role": "Button",
                        "selected": False,
                    },
                ),
                observation=observation,
            ),
        )
    )

    assert "controls" in payload["display_summary"].casefold()
    assert "app windows" not in payload["display_summary"].casefold()
    assert payload["controls"] == [
        {
            "token": "current-one",
            "label": "Duplicate",
            "role": "Button",
            "selected": True,
        },
        {
            "token": "current-two",
            "label": "Duplicate",
            "role": "Button",
            "selected": False,
        },
    ]
    assert payload["capture_is_fresh"] is True
    assert "current tokens" in payload["remediation"].casefold()


def test_generation_lifetime_and_action_specific_recovery_are_explicit() -> None:
    guide = (
        Path(__file__).parents[2] / "tool_guides" / "computer_use_guide" / "SKILL.md"
    ).read_text(encoding="utf-8").casefold()

    assert "current computer use generation" in guide
    assert "new user turn" in guide
    assert "gone or its lease expired" in guide
    assert "reversible click" in guide
    assert "one alternative exact route" in guide
    assert "text insertion" in guide
    assert "must not be replayed" in guide
    assert "action dispatched" in guide
    assert "native state" in guide
    assert "exact postcondition" in guide
    assert "one computer use call at a time" in guide
    assert "one app-scoped capture" in guide
    assert "exact current `list_apps` name" in guide
    assert "parallel app/window discovery" in guide
    assert "previously returned opaque target" in guide
    assert "non-retryable native capture" in guide
    assert "app_not_running" in guide
    assert "window_not_found" in guide


def test_expired_target_remediation_requires_current_generation_rediscovery() -> None:
    payload = json.loads(
        _computer_error_payload(
            "click",
            ComputerUseError(
                "Unknown target: gone or its lease expired.",
                code="target_gone",
            ),
        )
    )

    assert "gone or its lease expired" in payload["display_summary"].casefold()
    assert "current generation" in payload["remediation"].casefold()
    assert "list_apps" in payload["remediation"]
    assert "capture" in payload["remediation"]


def test_launch_failure_payload_exposes_only_safe_stage_and_error_class() -> None:
    payload = json.loads(
        _computer_error_payload(
            "launch_app",
            ComputerUseError(
                "Native app launch failed safely.",
                code="driver_unavailable",
                failure_stage="launch_dispatch",
                safe_driver_error="permission_or_driver_unavailable",
            ),
        )
    )

    assert payload["failure_stage"] == "launch_dispatch"
    assert payload["driver_error_class"] == "permission_or_driver_unavailable"
    assert "path" not in payload
    assert "raw_error" not in payload


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
