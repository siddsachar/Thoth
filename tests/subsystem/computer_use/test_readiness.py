from __future__ import annotations

from types import SimpleNamespace

from row_bot.computer_use import readiness as readiness_module
from row_bot.computer_use.readiness import DISCLOSURE_TEXT, ReadinessCode, acknowledge_disclosure, cancel_disclosure, configure_system_cua, disclosure_acknowledged, readiness, verify_system_cua
from row_bot.mcp_client import requirements


def test_disclosure_acknowledgement_is_local_and_versioned(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path))
    cancel_disclosure()
    assert disclosure_acknowledged() is False
    assert readiness(enabled=True).code is ReadinessCode.DISCLOSURE_REQUIRED
    acknowledge_disclosure()
    assert disclosure_acknowledged() is True


def test_old_notice_acknowledgement_requires_consent_again(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path))
    (tmp_path / "computer_use_settings.json").write_text(
        '{"acknowledged_notice_version": 1}',
        encoding="utf-8",
    )
    assert disclosure_acknowledged() is False
    assert readiness(enabled=True).code is ReadinessCode.DISCLOSURE_REQUIRED


def test_notice_describes_expanded_0200_telemetry_and_exclusions() -> None:
    lowered = DISCLOSURE_TEXT.casefold()
    for expected in (
        "process-session",
        "tool/operation",
        "duration bucket",
        "aggregate session",
        "window/desktop modality",
        "permission-gate",
        "tool arguments or results",
        "accessibility trees",
        "raw errors",
        "not row-bot telemetry",
    ):
        assert expected in lowered


def test_linux_is_unavailable_without_import_or_process_failure(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path))
    acknowledge_disclosure()
    monkeypatch.setattr(readiness_module.platform, "system", lambda: "Linux")
    assert readiness(enabled=True).code is ReadinessCode.UNSUPPORTED


def test_system_override_version_check_cannot_run_before_disclosure(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path))
    binary = tmp_path / "cua-driver.exe"
    binary.write_bytes(b"fake")
    configure_system_cua(str(binary), enabled=True)
    cancel_disclosure()
    called = []
    monkeypatch.setattr(readiness_module.subprocess, "run", lambda *_args, **_kwargs: called.append(True))
    try:
        verify_system_cua()
    except PermissionError:
        pass
    else:
        raise AssertionError("disclosure was not enforced")
    assert called == []


def test_verified_system_override_must_match_exact_reviewed_version(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(readiness_module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(readiness_module.platform, "machine", lambda: "AMD64")
    binary = tmp_path / "cua-driver.exe"
    binary.write_bytes(b"fake")
    configure_system_cua(str(binary), enabled=True)
    acknowledge_disclosure()
    class _Completed:
        returncode = 0
        stdout = "cua-driver 0.20.0"
        stderr = ""
    monkeypatch.setattr(readiness_module.subprocess, "run", lambda *_args, **_kwargs: _Completed())
    assert verify_system_cua().code is ReadinessCode.READY
    assert readiness(enabled=True).code is ReadinessCode.DEGRADED


def test_successful_system_diagnostics_do_not_require_calculator_test(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(readiness_module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(readiness_module.platform, "machine", lambda: "AMD64")
    binary = tmp_path / "cua-driver.exe"
    binary.write_bytes(b"fake")
    configure_system_cua(str(binary), enabled=True)
    acknowledge_disclosure()

    class _Completed:
        returncode = 0
        stdout = "cua-driver 0.20.0"
        stderr = ""

    monkeypatch.setattr(readiness_module.subprocess, "run", lambda *_args, **_kwargs: _Completed())
    verify_system_cua()
    readiness_module._set_readiness_marker("doctor_ok", True)

    state = readiness(enabled=True)
    settings = readiness_module._read_json(tmp_path / "computer_use_settings.json")
    assert state.code is ReadinessCode.READY
    assert "system_cua_observation_ok" not in settings


def test_successful_managed_diagnostics_do_not_require_calculator_test(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(readiness_module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(readiness_module.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(requirements, "RUNTIMES_DIR", tmp_path / "runtimes")
    acknowledge_disclosure()
    asset = readiness_module.selected_asset()
    assert asset is not None
    version_root = requirements.RUNTIMES_DIR / "cua-driver" / "0.20.0"
    executable = version_root / "cua-driver.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"reviewed")
    requirements._write_manifest(
        "cua-driver",
        {
            "installed": True,
            "version": "0.20.0",
            "archive_sha256": asset["sha256"],
            "root": str(version_root),
            "executable_path": str(executable),
            "source": "reviewed-pinned-archive",
        },
    )

    class _HealthyDoctor:
        def __init__(self, _executable, **_kwargs):
            pass

        def start(self):
            return None

        def call_internal(self, _name):
            return SimpleNamespace(
                structured={
                    "schema_version": "1",
                    "overall": "ok",
                    "checks": [],
                }
            )

        def close(self):
            return None

    import row_bot.computer_use.client as client_module

    monkeypatch.setattr(client_module, "CuaClient", _HealthyDoctor)
    result = readiness_module.run_cua_diagnostics()
    stored = requirements._read_manifest("cua-driver")

    assert result.code is ReadinessCode.READY
    assert readiness(enabled=True).code is ReadinessCode.READY
    assert stored["doctor_ok"] is True
    assert "observation_ok" not in stored
    assert "Calculator" not in result.message


def test_failed_managed_doctor_rolls_back_to_retained_known_good(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(readiness_module.platform, "system", lambda: "Windows")
    monkeypatch.setattr(readiness_module.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(requirements, "RUNTIMES_DIR", tmp_path / "runtimes")
    acknowledge_disclosure()
    asset = readiness_module.selected_asset()
    assert asset is not None
    runtime_root = requirements.RUNTIMES_DIR / "cua-driver"
    old_root = runtime_root / "0.7.1"
    new_root = runtime_root / "0.20.0"
    old_root.mkdir(parents=True)
    new_root.mkdir()
    old_executable = old_root / "cua-driver.exe"
    new_executable = new_root / "cua-driver.exe"
    old_executable.write_bytes(b"old")
    new_executable.write_bytes(b"new")
    requirements._write_manifest("cua-driver", {
        "installed": True,
        "version": "0.20.0",
        "archive_sha256": asset["sha256"],
        "root": str(new_root),
        "executable_path": str(new_executable),
        "previous_manifest": {
            "installed": True,
            "version": "0.7.1",
            "archive_sha256": "old",
            "root": str(old_root),
            "executable_path": str(old_executable),
            "doctor_ok": True,
        },
    })

    class _FailingDoctor:
        def __init__(self, _executable, **_kwargs):
            pass

        def start(self):
            return None

        def call_internal(self, _name):
            return SimpleNamespace(structured={"schema_version": "1", "overall": "failed", "checks": []})

        def close(self):
            return None

    import row_bot.computer_use.client as client_module

    monkeypatch.setattr(client_module, "CuaClient", _FailingDoctor)
    result = readiness_module.run_cua_diagnostics()
    assert result.code is ReadinessCode.FAILED
    assert requirements._read_manifest("cua-driver")["version"] == "0.7.1"
    assert old_root.exists()
    assert not new_root.exists()


def test_macos_legacy_flattened_bundle_requires_repair_before_diagnostics(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(readiness_module.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(readiness_module.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(requirements, "RUNTIMES_DIR", tmp_path / "runtimes")
    acknowledge_disclosure()
    asset = readiness_module.selected_asset()
    assert asset is not None
    version_root = requirements.RUNTIMES_DIR / "cua-driver" / "0.20.0"
    executable = version_root / "Contents" / "MacOS" / "cua-driver"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"reviewed")
    requirements._write_manifest(
        "cua-driver",
        {
            "installed": True,
            "version": "0.20.0",
            "archive_sha256": asset["sha256"],
            "root": str(version_root),
            "executable_path": str(executable),
            "source": "reviewed-pinned-archive",
        },
    )

    state = readiness(enabled=True)

    assert state.code is ReadinessCode.DEGRADED
    assert state.executable == str(executable)
    assert "repair" in state.remediation.casefold()
    assert "reinstall" in state.remediation.casefold()


def test_macos_cua_install_requests_app_bundle_preservation(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(readiness_module.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(readiness_module.platform, "machine", lambda: "arm64")
    acknowledge_disclosure()
    captured = {}

    def _install(runtime_id, **kwargs):
        captured["runtime_id"] = runtime_id
        captured.update(kwargs)
        return SimpleNamespace(ok=True, message="installed")

    monkeypatch.setattr(requirements, "install_pinned_archive_runtime", _install)

    result = readiness_module.install_cua_runtime()

    assert result.ok is True
    assert captured["runtime_id"] == "cua-driver"
    assert captured["preserve_top_level_directory"] is True


def test_macos_permission_diagnostics_do_not_expose_upstream_internal_hints(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(readiness_module.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(readiness_module.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(requirements, "RUNTIMES_DIR", tmp_path / "runtimes")
    acknowledge_disclosure()
    asset = readiness_module.selected_asset()
    assert asset is not None
    version_root = requirements.RUNTIMES_DIR / "cua-driver" / "0.20.0"
    executable = version_root / "CuaDriver.app" / "Contents" / "MacOS" / "cua-driver"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"reviewed")
    requirements._write_manifest(
        "cua-driver",
        {
            "installed": True,
            "version": "0.20.0",
            "archive_sha256": asset["sha256"],
            "root": str(version_root),
            "executable_path": str(executable),
            "source": "reviewed-pinned-archive",
            "preserve_top_level_directory": True,
        },
    )

    class _PermissionDoctor:
        def __init__(self, _executable, **_kwargs):
            pass

        def start(self):
            return None

        def call_internal(self, _name):
            return SimpleNamespace(
                structured={
                    "schema_version": "1",
                    "overall": "failed",
                    "checks": [
                        {
                            "name": "tcc_accessibility",
                            "status": "fail",
                            "hint": (
                                "Grant Accessibility to CuaDriver.app; see bundle_identity "
                                "and restart via cua-driver mcp."
                            ),
                        }
                    ],
                }
            )

        def close(self):
            return None

    import row_bot.computer_use.client as client_module

    monkeypatch.setattr(client_module, "CuaClient", _PermissionDoctor)

    result = readiness_module.run_cua_diagnostics()

    assert result.code is ReadinessCode.PERMISSION_MISSING
    assert "Row-Bot" in result.remediation
    assert "CuaDriver" not in result.remediation
    assert "bundle_identity" not in result.remediation
    assert "cua-driver mcp" not in result.remediation
    assert result.details is not None
