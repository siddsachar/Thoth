from __future__ import annotations

from pathlib import Path

import yaml

from scripts.probe_macos_cua import classify_diagnostics


WORKFLOW = Path(".github/workflows/macos-cua-probe.yml")


def test_healthy_diagnostics_stop_before_calculator_as_accepted() -> None:
    status, accepted = classify_diagnostics(
        "degraded",
        {"schema_version": "1", "overall": "ok", "checks": []},
    )

    assert (status, accepted) == ("diagnostics_passed", True)


def test_only_recognized_macos_permission_failures_are_accepted() -> None:
    status, accepted = classify_diagnostics(
        "permission_missing",
        {
            "schema_version": "1",
            "overall": "failed",
            "checks": [
                {"name": "tcc_accessibility", "status": "fail"},
                {"name": "screen_capture_permission", "status": "fail"},
            ],
        },
    )

    assert (status, accepted) == ("permission_pending", True)


def test_mixed_or_malformed_diagnostic_failures_are_rejected() -> None:
    mixed = classify_diagnostics(
        "permission_missing",
        {
            "schema_version": "1",
            "overall": "failed",
            "checks": [
                {"name": "tcc_accessibility", "status": "fail"},
                {"name": "mcp_transport", "status": "fail"},
            ],
        },
    )
    malformed = classify_diagnostics("degraded", {"overall": "ok", "checks": []})

    assert mixed == ("failed", False)
    assert malformed == ("failed", False)


def test_probe_workflow_is_manual_opt_in_and_separate_from_ci() -> None:
    workflow = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)

    assert set(workflow["on"]) == {"workflow_dispatch"}
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    assert inputs["accept_cua_notice"]["default"] == "false"
    assert inputs["runner"]["options"] == ["macos-15", "macos-15-intel"]
    assert workflow["permissions"] == {"contents": "read"}
    job = workflow["jobs"]["probe"]
    assert job["runs-on"] == "${{ inputs.runner }}"
    assert "env" not in job
    probe = next(
        step
        for step in job["steps"]
        if "scripts/probe_macos_cua.py" in step.get("run", "")
    )
    assert "$RUNNER_TEMP/row-bot-cua-probe/data" in probe["run"]
    upload = next(
        step
        for step in job["steps"]
        if step.get("uses") == "actions/upload-artifact@v7"
    )
    assert upload["with"]["path"] == "macos-cua-probe-report/report.json"
