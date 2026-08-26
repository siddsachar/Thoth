from __future__ import annotations

import pytest
import base64

from row_bot.computer_use.client import CuaClient, build_cua_environment, parse_cua_result
from row_bot.computer_use.readiness import cancel_disclosure
from tests.fixtures.fake_cua import FakeCuaTransport, FakeScenario
from row_bot.mcp_client.results import RawCallContent, RawCallResult


def test_client_starts_tagged_lifecycle_without_arbitrary_config(fake_client, fake_transport) -> None:
    fake_client.start()
    assert fake_transport.calls[:1] == [
        ("start_session", {"session": "row-bot-test-session"}),
    ]
    assert all(name != "set_config" for name, _args in fake_transport.calls)


def test_environment_disables_update_check_but_does_not_override_telemetry(monkeypatch) -> None:
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    env = build_cua_environment("session", {"HOME": "/home/test", "SECRET": "no", "CUA_DRIVER_RS_TELEMETRY_ENABLED": "0"})
    assert env["CUA_DRIVER_RS_UPDATE_CHECK"] == "0"
    assert env["CUA_DRIVER_EMBEDDED"] == "1"
    assert "CUA_DRIVER_RS_TELEMETRY_ENABLED" not in env
    assert "SECRET" not in env


def test_no_process_opens_before_disclosure(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path))
    cancel_disclosure()
    transport = FakeCuaTransport()
    client = CuaClient("fake.exe", transport_factory=lambda *_args: transport)
    with pytest.raises(PermissionError, match="disclosure"):
        client.start()
    assert transport.opened is False
    assert transport.calls == []


def test_capture_parses_text_image_and_capped_semantics(fake_client) -> None:
    response = fake_client.call_action("capture", {"pid": 4242, "window_id": 101})
    assert response.image_bytes and response.image_bytes.startswith(b"\x89PNG")
    assert (response.image_width, response.image_height) == (1, 1)
    assert [element.token for element in response.elements] == ["g1-element-0", "g1-element-1", "g1-element-2"]


def test_replace_text_maps_to_set_value_with_value_not_type_text(fake_client, fake_transport) -> None:
    replacement = "private exact replacement"

    response = fake_client.call_action(
        "replace_text",
        {
            "pid": 4242,
            "window_id": 101,
            "element_token": "fresh-exact-token",
            "value": replacement,
        },
    )

    assert response.is_error is False
    assert fake_transport.calls[-1] == (
        "set_value",
        {
            "pid": 4242,
            "window_id": 101,
            "element_token": "fresh-exact-token",
            "value": f"<redacted:{len(replacement)} chars>",
            "session": "row-bot-test-session",
        },
    )
    assert all(name != "type_text" for name, _args in fake_transport.calls)


def test_text_mutation_driver_free_text_is_normalized_at_private_boundary(
    fake_client,
    fake_transport,
) -> None:
    replacement = "private value interpolated by driver"
    fake_transport.scenario.action_error_code = "unsupported"
    fake_transport.scenario.action_error_message = f"could not set {replacement!r}"

    response = fake_client.call_action(
        "replace_text",
        {
            "pid": 4242,
            "window_id": 101,
            "element_token": "fresh-exact-token",
            "value": replacement,
        },
    )

    assert response.is_error is True
    assert response.error_code == "unsupported"
    assert replacement not in response.text
    assert replacement not in repr(response.structured)


def test_malformed_image_fails_closed(fake_transport) -> None:
    fake_transport.scenario = FakeScenario(malformed_image=True)
    with pytest.raises(ValueError, match="base64"):
        parse_cua_result(fake_transport.call_raw("get_window_state", {"pid": 1, "window_id": 2}))


def test_top_level_background_unavailable_error_code_is_preserved() -> None:
    result = RawCallResult(
        (RawCallContent(kind="text", text="background unavailable"),),
        {"error": True, "error_code": "background_unavailable"},
        True,
    )

    response = parse_cua_result(result)

    assert response.is_error is True
    assert response.error_code == "background_unavailable"


def test_oversized_tree_is_deterministically_capped(fake_transport) -> None:
    fake_transport.scenario = FakeScenario(oversized_tree=True)
    response = parse_cua_result(fake_transport.call_raw("get_window_state", {}))
    assert response.truncated is True
    assert len(response.elements) <= 2_000
    assert all(element.depth <= 25 for element in response.elements)
    assert "depth_limit" in response.local_limit_reasons


def test_tagged_observation_envelope_retains_two_thousand_depth_25_elements(fake_transport) -> None:
    fake_transport.scenario = FakeScenario(
        semantic_elements=tuple(
            {
                "role": "Button",
                "label": f"Synthetic action {index}",
                "depth": 25 if index == 1_999 else 1,
                "in_web_content": index == 1_999,
            }
            for index in range(2_000)
        ),
        driver_limited=False,
    )

    response = parse_cua_result(fake_transport.call_raw("get_window_state", {}))

    assert len(response.elements) == 2_000
    assert response.elements[-1].depth == 25
    assert response.backend_received_count == 2_000
    assert response.backend_declared_count == 2_000
    assert response.backend_limited is False
    assert response.local_limit_reasons == ()


def test_observation_validation_reports_driver_and_row_bot_limits_separately(fake_transport) -> None:
    fake_transport.scenario = FakeScenario(
        semantic_elements=(
            {"role": "Button", "label": "Valid", "depth": 1},
            {"role": "Button", "label": "Too deep", "depth": 26},
        ),
        driver_declared_count=2_000,
        driver_limited=True,
    )

    response = parse_cua_result(fake_transport.call_raw("get_window_state", {}))

    assert response.backend_limited is True
    assert response.backend_declared_count == 2_000
    assert response.backend_received_count == 2
    assert response.locally_filtered_count == 1
    assert response.local_limit_reasons == ("depth_limit",)


def test_nonfinite_geometry_duplicate_identity_and_invalid_parent_are_dropped(fake_transport) -> None:
    raw = fake_transport.call_raw("get_window_state", {})
    structured = dict(raw.structured_content)
    structured["elements"] = [
        {
            "element_index": 1,
            "element_token": "snapshot:1",
            "role": "Button",
            "label": "Valid",
            "frame": {"x": 0, "y": 0, "w": 10, "h": 10},
            "depth": 1,
        },
        {
            "element_index": 1,
            "element_token": "snapshot:1",
            "role": "Button",
            "label": "Duplicate",
            "frame": {"x": 0, "y": 0, "w": 10, "h": 10},
            "depth": 1,
        },
        {
            "element_index": 2,
            "element_token": "snapshot:2",
            "role": "Button",
            "label": "Infinite",
            "frame": {"x": float("inf"), "y": 0, "w": 10, "h": 10},
            "depth": 2,
            "parent_index": 1,
        },
        {
            "element_index": 3,
            "element_token": "snapshot:3",
            "role": "Button",
            "label": "Orphan",
            "frame": {"x": 0, "y": 0, "w": 10, "h": 10},
            "depth": 2,
            "parent_index": 99,
        },
    ]
    parsed = parse_cua_result(
        RawCallResult(raw.content, structured, raw.is_error)
    )
    assert [element.label for element in parsed.elements] == ["Valid"]
    assert set(parsed.local_limit_reasons) == {
        "invalid_geometry",
        "invalid_identity",
        "invalid_topology",
    }


def test_tagged_start_session_owns_window_scope_without_set_config(fake_transport, tmp_path, monkeypatch) -> None:
    from row_bot.computer_use.readiness import acknowledge_disclosure

    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path))
    acknowledge_disclosure()
    client = CuaClient(
        "fake.exe",
        session_id="tagged-session",
        contract_version="0.20.0",
        transport_factory=lambda *_args: fake_transport,
    )
    client.start()
    assert fake_transport.calls[0] == (
        "start_session",
        {"session": "tagged-session"},
    )
    assert all(name != "set_config" for name, _args in fake_transport.calls)


def test_tagged_contract_rejects_arbitrary_config_even_when_legacy_fixture_allows_it(
    fake_transport,
    tmp_path,
    monkeypatch,
) -> None:
    from row_bot.computer_use.readiness import acknowledge_disclosure

    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path))
    acknowledge_disclosure()
    client = CuaClient(
        "fake-cua-driver.exe",
        session_id="tagged-no-config",
        contract_version="0.20.0",
        capabilities=frozenset({"verify_state", "invoke_menu", "recording"}),
        transport_factory=lambda _exe, _session, _env: fake_transport,
    )
    client.start()
    with pytest.raises(PermissionError, match="configuration mutation"):
        client.call_internal("set_config", {"capture_scope": "desktop"})
    with pytest.raises(PermissionError):
        client.call_reviewed_driver_tool("start_recording")


def test_new_service_only_tools_remain_capability_gated(fake_transport, tmp_path, monkeypatch) -> None:
    from row_bot.computer_use.readiness import acknowledge_disclosure

    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path))
    acknowledge_disclosure()
    unavailable = CuaClient(
        "fake.exe",
        transport_factory=lambda *_args: fake_transport,
    )
    with pytest.raises(PermissionError):
        unavailable.call_reviewed_driver_tool("verify_state", {})

    available = CuaClient(
        "fake.exe",
        capabilities=frozenset({"verify_state"}),
        transport_factory=lambda *_args: fake_transport,
    )
    response = available.call_reviewed_driver_tool("verify_state", {"expect": []})
    assert response.is_error is False
    assert response.structured["status"] == "satisfied"


def test_forbidden_driver_tool_cannot_be_called(fake_client) -> None:
    with pytest.raises(PermissionError):
        fake_client._call("start_recording", {"output_dir": "secret"})


def test_disconnect_is_not_retried_by_client_or_service(fake_client, fake_transport) -> None:
    from row_bot.computer_use.service import ComputerUseError, ComputerUseService, LeaseOwner

    service = ComputerUseService(client_factory=lambda: fake_client, approval_callback=lambda _payload: True)
    owner = LeaseOwner("disconnect", "generation", "task")
    service.acquire(owner, validate_context=False)
    fake_transport.scenario.disconnect = True
    with pytest.raises(ComputerUseError, match="disconnected"):
        service.list_apps(owner)
    assert service.status_snapshot()["active"] is False
    assert service.status_snapshot()["state"] == "failed"


def test_image_mime_magic_mismatch_and_decoded_size_fail_closed() -> None:
    mismatch = RawCallResult((RawCallContent(kind="image", data=base64.b64encode(b"not a png").decode(), mime_type="image/png"),), {})
    with pytest.raises(ValueError, match="magic"):
        parse_cua_result(mismatch)
    oversized = RawCallResult((RawCallContent(kind="image", data=base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * (8 * 1024 * 1024)).decode(), mime_type="image/png"),), {})
    with pytest.raises(ValueError, match="8 MiB"):
        parse_cua_result(oversized)
