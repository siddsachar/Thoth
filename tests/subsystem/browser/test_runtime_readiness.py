from __future__ import annotations

import inspect

from row_bot.tools.browser_tool import BrowserSession, _browser_runs_headless


def test_browser_startup_has_no_hidden_install_or_csp_bypass() -> None:
    source = inspect.getsource(BrowserSession)
    assert "playwright\", \"install" not in source
    assert "bypass_csp" not in source
    assert "Managed Chromium is not installed" in source
    assert "networkidle" not in source
    assert "wait_for_timeout" not in source
    assert "time.sleep" not in source


def test_server_browser_headless_flag_is_explicit(monkeypatch) -> None:
    monkeypatch.delenv("ROW_BOT_BROWSER_HEADLESS", raising=False)
    assert _browser_runs_headless() is False
    for value in ("1", "true", "YES", "on"):
        monkeypatch.setenv("ROW_BOT_BROWSER_HEADLESS", value)
        assert _browser_runs_headless() is True
    monkeypatch.setenv("ROW_BOT_BROWSER_HEADLESS", "0")
    assert _browser_runs_headless() is False
