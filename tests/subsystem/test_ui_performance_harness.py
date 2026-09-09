from __future__ import annotations

import os
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from scripts import ui_performance_harness as harness


pytestmark = pytest.mark.subsystem


def test_process_roles_use_exact_pids_and_do_not_invent_server_memory(monkeypatch):
    seen = []

    def process(pid):
        seen.append(pid)
        return SimpleNamespace(memory_info=lambda: SimpleNamespace(rss=pid * 1024 * 1024))

    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(Process=process))
    samples = [harness.process_sample("probe", 11), harness.process_sample("server", 22),
               harness.process_sample("browser", 33), harness.process_sample("server", None)]
    assert seen == [11, 22, 33]
    assert [sample["rss_mb"] for sample in samples] == [11, 22, 33, None]
    assert samples[-1]["status"] == "unavailable"
    result = harness.CheckResult("helper", 0, True)
    assert result.pid == os.getpid()
    assert result.process_role == "probe"


def test_http_timing_is_labeled_as_reachability_not_interactive_render(monkeypatch):
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, count):
            return b"fixture"

    monkeypatch.setattr(harness, "urlopen", lambda *args, **kwargs: Response())
    result = harness._fetch("fixture", "http://127.0.0.1/", 1)
    assert result.ok
    assert result.measurement == "http_reachability"
    assert result.process_role == "probe"


def test_canonical_read_only_data_path_and_sqlite_mode(tmp_path, monkeypatch):
    missing = tmp_path / "absent"
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(missing))
    monkeypatch.setenv("ROW_BOT_HOME", str(tmp_path / "wrong-legacy-env"))
    assert harness._row_bot_home() == missing
    assert not missing.exists()
    path = tmp_path / "threads.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE fixture(value TEXT)")
    with harness._read_metadata(path) as connection:
        assert connection.execute("SELECT * FROM fixture").fetchall() == []
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("INSERT INTO fixture VALUES ('no write')")
