from __future__ import annotations

import json

from row_bot.access.diagnostics import (
    DiagnosticStatus,
    DoctorContext,
    redact_diagnostic_value,
    run_access_doctor,
)


def _context(tmp_path, **overrides) -> DoctorContext:
    values = {
        "deployment_mode": "server",
        "listen_host": "127.0.0.1",
        "port": 8080,
        "public_url": None,
        "allowed_hosts": ("localhost",),
        "trusted_proxy_cidrs": (),
        "data_dir": tmp_path,
        "access_db_path": tmp_path / "mobile.db",
    }
    values.update(overrides)
    return DoctorContext(**values)


def test_doctor_reports_safe_local_server_without_creating_database(tmp_path) -> None:
    report = run_access_doctor(_context(tmp_path))

    assert report.ok is True
    assert not (tmp_path / "mobile.db").exists()
    checks = {check.id: check for check in report.checks}
    assert checks["deployment_mode"].status is DiagnosticStatus.PASS
    assert checks["access_database"].status is DiagnosticStatus.INFO
    assert checks["owner_recovery"].status is DiagnosticStatus.PASS


def test_doctor_flags_public_bind_without_origin_and_multiple_workers(tmp_path) -> None:
    report = run_access_doctor(
        _context(
            tmp_path,
            listen_host="0.0.0.0",
            workers=2,
            ephemeral_data=True,
        )
    )
    checks = {check.id: check for check in report.checks}

    assert report.ok is False
    assert checks["public_binding"].status is DiagnosticStatus.ERROR
    assert checks["workers"].status is DiagnosticStatus.ERROR
    assert checks["persistent_data"].status is DiagnosticStatus.ERROR


def test_doctor_warns_for_lan_http_and_tailscale_conflict(tmp_path) -> None:
    report = run_access_doctor(
        _context(
            tmp_path,
            listen_host="192.168.1.4",
            public_url="http://192.168.1.4:8080",
            allowed_hosts=("192.168.1.4",),
            tailscale_state="conflicting",
            active_route_status="error",
        )
    )
    checks = {check.id: check for check in report.checks}

    assert report.ok is True
    assert checks["insecure_http"].status is DiagnosticStatus.WARNING
    assert checks["tailscale"].status is DiagnosticStatus.WARNING
    assert checks["route"].status is DiagnosticStatus.WARNING


def test_doctor_rejects_malformed_proxy_and_database(tmp_path) -> None:
    (tmp_path / "mobile.db").write_bytes(b"not a sqlite database")
    report = run_access_doctor(
        _context(
            tmp_path,
            trusted_proxy_cidrs=("not-a-cidr",),
        )
    )
    checks = {check.id: check for check in report.checks}

    assert checks["trusted_proxies"].status is DiagnosticStatus.ERROR
    assert checks["access_database"].status is DiagnosticStatus.ERROR


def test_diagnostic_redaction_removes_nested_credentials() -> None:
    secret = "do-not-print-this"
    value = {
        "safe": "visible",
        "token": secret,
        "nested": {
            "authorization_header": secret,
            "cookie": secret,
        },
    }

    serialized = json.dumps(redact_diagnostic_value(value))

    assert secret not in serialized
    assert "visible" in serialized
    assert serialized.count("[redacted]") == 3
