from __future__ import annotations

import pytest

from row_bot.computer_use.service import ComputerUseError, LeaseOwner, Target
from pathlib import Path
import os


OWNER = LeaseOwner("privacy-thread", "privacy-generation", "privacy-task")


def test_screenshot_is_ephemeral_and_absent_from_model_text_and_status(service) -> None:
    service.acquire(OWNER, validate_context=False)
    target_id = service.list_windows(OWNER, app="Calculator")[0]["target_id"]
    observation = service.capture(target_id, OWNER)
    assert observation.screenshot
    assert "base64" not in observation.model_text().lower()
    assert "screenshot" not in str(service.status_snapshot()).lower()
    assert service.ephemeral_screenshot() == observation.screenshot
    service.stop()
    assert service.ephemeral_screenshot() is None
    durable = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in Path(os.environ["ROW_BOT_DATA_DIR"]).rglob("*")
        if path.is_file()
    )
    assert "iVBOR" not in durable


def test_typed_value_is_absent_from_service_state_model_output_and_fake_history(service, fake_transport) -> None:
    secret = "never-persist-this-secret"
    service.acquire(OWNER, validate_context=False)
    target_id = service.list_windows(OWNER, app="Calculator")[0]["target_id"]
    service.capture(target_id, OWNER)
    result = service.act("type", target_id, OWNER, text=secret)
    assert secret not in repr(result)
    assert secret not in str(service.status_snapshot())
    assert secret not in repr(fake_transport.calls)


def test_replaced_value_is_absent_from_model_status_receipt_logs_and_fake_history(
    service,
    fake_transport,
    caplog,
) -> None:
    import logging

    secret = "never-expose-this-replacement"
    fake_transport.scenario.semantic_elements = (
        {
            "role": "GridCell",
            "label": "Selected item",
            "value": "prior private value",
            "enabled": True,
            "selected": True,
        },
    )
    service.acquire(OWNER, validate_context=False)
    target_id = service.list_windows(OWNER, app="Calculator")[0]["target_id"]
    observation = service.capture(target_id, OWNER)
    signature = (
        "replace_text",
        target_id,
        True,
        len(secret),
    )

    with caplog.at_level(logging.INFO, logger="row_bot.computer_use.service"):
        service.begin_tool_call(signature)
        result = service.act(
            "replace_text",
            target_id,
            OWNER,
            element_token=observation.elements[0].token,
            text=secret,
        )
        service.end_tool_call(
            signature,
            action_family="replace_text",
            driver_effect=result.driver_effect,
            effect_verified=result.effect_verified,
        )

    rendered = "\n".join(record.message for record in caplog.records)
    assert secret not in repr(result)
    assert secret not in observation.model_text()
    assert secret not in str(service.status_snapshot())
    assert secret not in repr(fake_transport.calls)
    assert secret not in rendered
    assert "prior private value" not in observation.model_text()
    assert "replace_text (value hidden)" in str(service.status_snapshot())


def test_driver_interpolated_replacement_is_absent_from_public_error_and_history(
    service,
    fake_transport,
) -> None:
    secret = "private-driver-interpolated-value"
    fake_transport.scenario.semantic_elements = (
        {"role": "Edit", "label": "Document field", "enabled": True},
    )
    service.acquire(OWNER, validate_context=False)
    target_id = service.list_windows(OWNER, app="Calculator")[0]["target_id"]
    observation = service.capture(target_id, OWNER)
    fake_transport.scenario.action_error_code = "unsupported"
    fake_transport.scenario.action_error_message = f"set_value rejected {secret!r}"

    with pytest.raises(ComputerUseError) as failed:
        service.act(
            "replace_text",
            target_id,
            OWNER,
            element_token=observation.elements[0].token,
            text=secret,
        )

    assert secret not in str(failed.value)
    assert secret not in repr(fake_transport.calls)
    assert secret not in str(service.status_snapshot())


def test_post_stop_has_no_replay_or_completion_state_and_keeps_receipt_truthful(
    service,
    fake_transport,
) -> None:
    secret = "generation-private-complete-value"
    fake_transport.scenario.semantic_elements = (
        {
            "role": "Edit",
            "label": "Document field",
            "value": "old private value",
            "enabled": True,
        },
    )
    fake_transport.scenario.set_value_updates_document = False
    fake_transport.scenario.delivery_profile = "catalyst_value_unavailable"
    service.acquire(OWNER, validate_context=False)
    target_id = service.list_windows(OWNER, app="Calculator")[0]["target_id"]
    observation = service.capture(target_id, OWNER)

    uncertain = service.act(
        "replace_text",
        target_id,
        OWNER,
        element_token=observation.elements[0].token,
        text=secret,
    )
    service.stop()

    assert uncertain.effect_verified is False
    assert not hasattr(service, "_pending_mutation")
    assert not hasattr(service, "_completion_ledger")
    assert secret not in repr(service.status_snapshot())
    assert "old private value" not in repr(service.status_snapshot())


def test_window_discovery_requires_scope_before_calling_the_driver(service, fake_transport) -> None:
    service.acquire(OWNER, validate_context=False)

    with pytest.raises(ComputerUseError, match="requires an app name"):
        service.list_windows(OWNER)

    assert "list_windows" not in [name for name, _args in fake_transport.calls]


def test_window_discovery_returns_only_private_scoped_candidates(service, fake_transport) -> None:
    private_title = "Private inbox - secret@example.test"
    fake_transport.scenario.windows = (
        {
            "window_id": 1,
            "pid": 1,
            "app_name": "python.exe",
            "title": "Row-Bot",
            "bounds": {"x": 0, "y": 0, "width": 800, "height": 600},
            "is_on_screen": True,
        },
        {
            "window_id": 2,
            "pid": 2,
            "app_name": "Notepad",
            "title": "TARGET A - Notepad",
            "bounds": {"x": 0, "y": 0, "width": 800, "height": 600},
            "is_on_screen": True,
        },
        {
            "window_id": 3,
            "pid": 3,
            "app_name": "Edge",
            "title": private_title,
            "bounds": {"x": 0, "y": 0, "width": 800, "height": 600},
            "is_on_screen": True,
        },
    )
    service.acquire(OWNER, validate_context=False)

    rows = service.list_windows(OWNER, app="Notepad", window_hint="TARGET A")

    assert len(rows) == 1
    rendered = repr(rows)
    assert rows[0]["app"] == "Notepad"
    assert "TARGET A" not in rendered
    assert private_title not in rendered
    assert "Row-Bot" not in rendered
    durable = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in Path(os.environ["ROW_BOT_DATA_DIR"]).rglob("*")
        if path.is_file()
    )
    assert private_title not in durable


def test_controller_surfaces_cannot_be_discovered_or_launched(service, fake_transport) -> None:
    service.acquire(OWNER, validate_context=False)

    with pytest.raises(ComputerUseError, match="cannot be targeted") as discovery_error:
        service.list_windows(OWNER, app="python.exe", window_hint="Row-Bot")
    assert discovery_error.value.code == "hard_blocked"
    assert discovery_error.value.retryable is False
    with pytest.raises(ComputerUseError, match="cannot be targeted"):
        service.launch_app("Row-Bot", OWNER)
    service._targets["protected-test-target"] = Target(
        target_id="protected-test-target",
        pid=99,
        window_id=100,
        app_name="python.exe",
        window_title="Row-Bot",
        bounds=(0, 0, 800, 600),
    )
    with pytest.raises(ComputerUseError, match="cannot be targeted") as capture_error:
        service.capture("protected-test-target", OWNER)
    assert capture_error.value.code == "hard_blocked"

    names = [name for name, _args in fake_transport.calls]
    assert "list_windows" not in names
    assert "launch_app" not in names
    assert "get_window_state" not in names
