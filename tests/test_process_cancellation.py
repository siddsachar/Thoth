from __future__ import annotations

import subprocess
import threading

from row_bot.cancellation import CancellationScope, use_cancellation_scope
from row_bot.process_cancellation import run_cancellable_subprocess


class _FakeProcess:
    pid = 1234

    def __init__(self) -> None:
        self.started = threading.Event()
        self.terminated = threading.Event()
        self.returncode = None

    def communicate(self, timeout=None):
        self.started.set()
        if not self.terminated.wait(timeout=2):
            raise AssertionError("fake process was not terminated")
        self.returncode = -15
        return "partial stdout", "partial stderr"

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15
        self.terminated.set()

    def wait(self, timeout=None):
        if not self.terminated.wait(timeout=timeout):
            import subprocess

            raise subprocess.TimeoutExpired(["fake"], timeout=timeout)
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9
        self.terminated.set()


class _ExitedDirectProcess:
    pid = 2345
    returncode = 0

    def __init__(self, stdout, stderr) -> None:
        stdout.write(b"direct stdout")
        stderr.write(b"direct stderr")
        self.communicate_calls: list[float | None] = []

    def communicate(self, timeout=None):
        self.communicate_calls.append(timeout)
        return None, None

    def poll(self):
        return self.returncode


class _ExitedAtTimeoutProcess:
    pid = 3456
    returncode = 0

    def __init__(self) -> None:
        self.communicate_calls: list[float | None] = []

    def communicate(self, timeout=None):
        self.communicate_calls.append(timeout)
        if timeout is None:
            raise AssertionError("subprocess cleanup must never communicate without a timeout")
        raise subprocess.TimeoutExpired(["fake"], timeout=timeout)

    def poll(self):
        return self.returncode


def test_run_cancellable_subprocess_terminates_process_when_scope_is_cancelled(monkeypatch) -> None:
    import row_bot.process_cancellation as process_cancellation

    fake = _FakeProcess()
    monkeypatch.setattr(process_cancellation.subprocess, "Popen", lambda *_args, **_kwargs: fake)
    scope = CancellationScope()
    result_holder = []

    def run() -> None:
        with use_cancellation_scope(scope):
            result_holder.append(
                run_cancellable_subprocess(["fake"], cwd=".", timeout=30)
            )

    worker = threading.Thread(target=run)
    worker.start()
    assert fake.started.wait(timeout=1)

    scope.cancel("test")
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert fake.terminated.is_set() is True
    assert result_holder[0].cancelled is True
    assert result_holder[0].returncode == 130
    assert result_holder[0].stdout == "partial stdout"
    assert result_holder[0].stderr == "partial stderr"


def test_run_cancellable_subprocess_does_not_wait_for_descendant_pipe_eof(
    monkeypatch,
) -> None:
    import row_bot.process_cancellation as process_cancellation

    captured = {}

    def fake_popen(*_args, **kwargs):
        captured.update(kwargs)
        return _ExitedDirectProcess(kwargs["stdout"], kwargs["stderr"])

    monkeypatch.setattr(process_cancellation.subprocess, "Popen", fake_popen)

    result = run_cancellable_subprocess(["fake"], cwd=".", timeout=30)

    assert captured["stdout"] is not subprocess.PIPE
    assert captured["stderr"] is not subprocess.PIPE
    assert result.returncode == 0
    assert result.stdout == "direct stdout"
    assert result.stderr == "direct stderr"


def test_run_cancellable_subprocess_timeout_has_no_unbounded_second_drain(
    monkeypatch,
) -> None:
    import row_bot.process_cancellation as process_cancellation

    fake = _ExitedAtTimeoutProcess()
    monkeypatch.setattr(
        process_cancellation.subprocess,
        "Popen",
        lambda *_args, **_kwargs: fake,
    )

    result = run_cancellable_subprocess(["fake"], cwd=".", timeout=7)

    assert result.timed_out is True
    assert result.returncode == 124
    assert fake.communicate_calls == [7]
