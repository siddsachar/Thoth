import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import launcher
import service


def test_read_pid_returns_none_for_missing_file(tmp_path: Path) -> None:
    assert service.read_pid(tmp_path / "missing.pid") is None


def test_read_pid_returns_none_for_garbage(tmp_path: Path) -> None:
    pid_path = tmp_path / "service.pid"
    pid_path.write_text("not-a-number\n")
    assert service.read_pid(pid_path) is None


def test_write_and_remove_pid_round_trip(tmp_path: Path) -> None:
    pid_path = tmp_path / "service.pid"
    service.write_pid(pid_path, 4242)
    assert pid_path.read_text().strip() == "4242"
    assert service.read_pid(pid_path) == 4242

    service.remove_pid(pid_path)
    assert not pid_path.exists()
    # remove_pid is idempotent.
    service.remove_pid(pid_path)


def test_is_alive_for_self_and_invalid() -> None:
    assert service.is_alive(os.getpid()) is True
    assert service.is_alive(0) is False
    # PID 1 is always init on Linux; treat as live (or PermissionError → live).
    assert service.is_alive(1) is True


def test_status_message_states(tmp_path: Path) -> None:
    pid_path = tmp_path / "service.pid"

    assert service.status_message(pid_path) == "Thoth service: stopped."

    service.write_pid(pid_path, os.getpid())
    assert "running" in service.status_message(pid_path)

    # Stale PID — write a number that is extremely unlikely to be alive.
    service.write_pid(pid_path, 999_999)
    msg = service.status_message(pid_path)
    assert "stopped" in msg and "stale" in msg


def test_stop_service_when_not_running(tmp_path: Path) -> None:
    pid_path = tmp_path / "service.pid"
    assert "not running" in service.stop_service(pid_path).lower()


def test_stop_service_clears_stale_pid(tmp_path: Path) -> None:
    pid_path = tmp_path / "service.pid"
    service.write_pid(pid_path, 999_999)
    msg = service.stop_service(pid_path)
    assert "stale" in msg.lower() or "not running" in msg.lower()
    assert not pid_path.exists()


@pytest.mark.skipif(os.name != "posix", reason="signal-based stop is POSIX-only")
def test_stop_service_terminates_child(tmp_path: Path) -> None:
    pid_path = tmp_path / "service.pid"
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    try:
        service.write_pid(pid_path, proc.pid)
        msg = service.stop_service(pid_path, timeout=5.0)
        proc.wait(timeout=5)
        assert proc.returncode is not None
        assert "stopped" in msg.lower() or "force-killed" in msg.lower()
        assert not pid_path.exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_install_systemd_unit_writes_expected_file(tmp_path: Path) -> None:
    unit_path = tmp_path / "thoth.service"
    written = service.install_systemd_unit(launch_cmd="/opt/thoth/bin/thoth", unit_path=unit_path)
    assert written == unit_path
    contents = unit_path.read_text()
    assert "ExecStart=/opt/thoth/bin/thoth --server --no-tray --no-open --no-splash" in contents
    assert "Type=simple" in contents
    assert "[Install]" in contents


def test_default_paths_use_thoth_data_dir(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("THOTH_DATA_DIR", str(tmp_path))
    assert service.default_pid_path() == tmp_path / "service.pid"
    assert service.default_log_path() == tmp_path / "service.log"


def test_main_service_status_prints_and_returns(monkeypatch, capsys, tmp_path: Path) -> None:
    monkeypatch.setenv("THOTH_DATA_DIR", str(tmp_path))
    launcher.main(["--service-status"])
    out = capsys.readouterr().out
    assert "Thoth service" in out


def test_main_service_stop_when_not_running(monkeypatch, capsys, tmp_path: Path) -> None:
    monkeypatch.setenv("THOTH_DATA_DIR", str(tmp_path))
    launcher.main(["--service-stop"])
    out = capsys.readouterr().out
    assert "not running" in out.lower()


def test_main_install_systemd_writes_unit(monkeypatch, capsys, tmp_path: Path) -> None:
    fake_unit = tmp_path / "thoth.service"

    def fake_install(launch_cmd=None, unit_path=None):
        fake_unit.write_text("UNIT")
        return fake_unit

    monkeypatch.setattr(service, "install_systemd_unit", fake_install)
    launcher.main(["--install-systemd-service"])
    out = capsys.readouterr().out
    assert str(fake_unit) in out
    assert "systemctl --user" in out


def test_arg_parser_accepts_service_flags() -> None:
    parser = launcher._build_arg_parser()
    args = parser.parse_args(["--run-as-service"])
    assert args.run_as_service is True
    assert args.service_stop is False
