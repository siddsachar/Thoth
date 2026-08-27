from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from row_bot.automation.contracts import (
    ActionReceipt,
    ActivitySnapshot,
    AutomationSurface,
    ObservationStatus,
    classify_no_progress,
    sanitize_automation_error,
)


@pytest.mark.parametrize("surface", list(AutomationSurface))
def test_shared_receipt_is_immutable_and_preserves_computer_compatibility(surface) -> None:
    receipt = ActionReceipt(
        surface=surface,
        target_id="synthetic-target",
        action_family="click",
        revision=7,
        backend_effect="confirmed",
        delivery="foreground",
        route="accessibility",
        visual_change="changed",
        verified_outcome=True,
    )
    payload = receipt.to_dict()
    assert payload["surface"] == surface.value
    assert payload["action"] == "click"
    assert payload["target_revision"] == 7
    assert payload["action_dispatched"] is True
    assert payload["driver_effect"] == "confirmed"
    assert payload["effect_verified"] is True
    with pytest.raises(FrozenInstanceError):
        receipt.route = "pixels"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("backend_limited", "sparse", "reasons", "expected"),
    [
        (False, False, (), "complete"),
        (True, False, (), "driver"),
        (False, False, ("byte_limit",), "row_bot"),
        (True, False, ("depth_limit",), "both"),
        (None, True, (), "sparse"),
        (None, False, (), "unknown"),
    ],
)
def test_observation_provenance_keeps_backend_and_local_limits_distinct(
    backend_limited, sparse, reasons, expected
) -> None:
    status = ObservationStatus(
        revision=3,
        backend_declared_count=12,
        backend_received_count=10,
        locally_validated_count=9,
        projected_count=4,
        backend_limited=backend_limited,
        backend_sparse=sparse,
        local_limit_reasons=reasons,
    )
    assert status.provenance == expected


def test_unknown_result_is_not_no_progress_but_verified_unchanged_is() -> None:
    assert classify_no_progress(
        backend_effect="unverifiable", visual_change="unknown", verified_outcome=False
    ) == "unknown"
    assert classify_no_progress(
        backend_effect="suspected_noop", visual_change="unchanged", verified_outcome=False
    ) == "no_progress"


def test_error_sanitization_never_returns_raw_backend_details() -> None:
    error = sanitize_automation_error(
        AutomationSurface.BROWSER,
        "click",
        RuntimeError("selector=#secret detached at C:\\private\\profile"),
    )
    assert error.code == "stale_observation"
    assert "secret" not in error.remediation
    assert "private" not in error.remediation


def test_activity_projection_accepts_existing_engine_snapshots() -> None:
    snapshot = ActivitySnapshot.from_mapping(
        {
            "engine": "computer",
            "active": True,
            "thread_id": "task-thread",
            "state": "waiting_user",
            "app": "Synthetic Editor",
            "revision": 4,
        }
    )
    assert snapshot.surface is AutomationSurface.COMPUTER
    assert snapshot.target == "Synthetic Editor"
    assert snapshot.state_label == "Waiting for you"
