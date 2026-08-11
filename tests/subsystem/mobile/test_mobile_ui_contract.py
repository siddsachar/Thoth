from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace


def test_mobile_request_detection_is_explicit() -> None:
    from row_bot.ui.mobile import _mobile_view, is_mobile_client

    class Request:
        query_params = {"mobile": "1"}

    class DesktopRequest:
        query_params = {}

    class Client:
        request = Request()

    class DesktopClient:
        request = DesktopRequest()

    assert is_mobile_client(Client()) is True
    assert is_mobile_client(DesktopClient()) is False
    state = SimpleNamespace()
    assert _mobile_view(state) == "Chat"
    assert state.mobile_view == "Chat"


def test_access_context_controls_compact_shell_not_cookie_or_peer() -> None:
    from row_bot.access.config import AccessConfig
    from row_bot.access.request_context import (
        RequestContextResolver,
        SessionIdentity,
    )
    from row_bot.ui.mobile import is_mobile_client

    resolver = RequestContextResolver(AccessConfig.build(deployment_mode="desktop"))
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "query_string": b"",
        "scheme": "http",
        "client": ("192.168.1.20", 51000),
        "headers": [(b"host", b"localhost:8080")],
    }
    compact = resolver.resolve(
        {**scope, "query_string": b"mobile=1"},
        session=SessionIdentity(
            device_id="device",
            session_id="session",
        ),
    )
    owner = resolver.resolve(
        {**scope, "client": ("127.0.0.1", 51000)},
    )
    compact_request = SimpleNamespace(
        query_params={},
        state=SimpleNamespace(row_bot_access_context=compact),
    )
    owner_request = SimpleNamespace(
        query_params={},
        state=SimpleNamespace(row_bot_access_context=owner),
    )

    assert is_mobile_client(SimpleNamespace(request=compact_request)) is True
    assert is_mobile_client(SimpleNamespace(request=owner_request)) is False


def test_mobile_workflow_approval_uses_mobile_source(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path))
    from row_bot import tasks
    from row_bot.ui.mobile import respond_to_mobile_workflow_approval

    calls: list[dict] = []

    def fake_respond(resume_token: str, approved: bool, note: str = "", source: str = "web") -> bool:
        calls.append(
            {
                "resume_token": resume_token,
                "approved": approved,
                "note": note,
                "source": source,
            }
        )
        return True

    monkeypatch.setattr(tasks, "respond_to_approval", fake_respond)

    assert respond_to_mobile_workflow_approval("resume-token", True, note="from phone") is True
    assert calls == [
        {
            "resume_token": "resume-token",
            "approved": True,
            "note": "from phone",
            "source": "mobile",
        }
    ]


def test_mobile_workflow_approval_click_runs_off_ui_loop() -> None:
    mobile_src = Path("src/row_bot/ui/mobile.py").read_text(encoding="utf-8")

    assert "from nicegui import run as nicegui_run, ui" in mobile_src
    assert "def respond_to_mobile_approval(" in mobile_src
    assert "def respond_to_mobile_workflow_approval(" in mobile_src
    assert "async def respond_to_mobile_workflow_approval_async(" in mobile_src
    assert "await nicegui_run.io_bound(respond_to_mobile_approval" in mobile_src
    assert "mobile_workflow_approval_busy" in mobile_src
    assert "mobile_workflow_approval_sending" in mobile_src


def test_app_routes_mobile_shell_without_desktop_chrome() -> None:
    app_src = Path("src/row_bot/app.py").read_text(encoding="utf-8")
    mobile_src = Path("src/row_bot/ui/mobile.py").read_text(encoding="utf-8")

    assert "access_context_from_client(ui.context.client)" in app_src
    assert '_access_context.presentation.value == "compact"' in app_src
    assert "build_mobile_shell(" in app_src
    assert "mobile=_mobile_client" in app_src
    assert "_companion_client" not in app_src
    assert "has_capability" not in app_src
    assert "if not _mobile_client:" in app_src
    assert "build_command_center(" in app_src
    assert "build_terminal_panel(p, state, _tool_registry)" in app_src
    assert "build_chat_view=" not in app_src
    assert "add_chat_message=" in app_src
    assert "Developer Studio\" if state.active_developer_workspace_id else \"Designer Studio" in mobile_src
    assert "source=\"mobile\"" in mobile_src
    assert "(\"Chat\", \"chat\")" in mobile_src
    assert "open_settings(\"Providers\")" in mobile_src
    assert 'if tab == "Settings":' in mobile_src
    assert "def _build_settings(" not in mobile_src


def test_mobile_shell_css_is_full_bleed() -> None:
    mobile_src = Path("src/row_bot/ui/mobile.py").read_text(encoding="utf-8")

    assert "body:has(.row-bot-mobile-root)" in mobile_src
    assert ".row-bot-main-shell.row-bot-mobile-root" in mobile_src
    assert "position: fixed;" in mobile_src
    assert "inset: 0;" in mobile_src
    assert "width: 100vw !important;" in mobile_src
    assert "border-radius: 0 !important;" in mobile_src


def test_mobile_active_chat_hides_global_header_and_bottom_nav() -> None:
    mobile_src = Path("src/row_bot/ui/mobile.py").read_text(encoding="utf-8")
    header_section = mobile_src.split("def _build_mobile_header(", 1)[1].split("def _build_mobile_nav(", 1)[0]

    assert "active_chat_detail" in mobile_src
    assert "mobile_chat_mode(state) == \"thread\"" in mobile_src
    assert "if not active_chat_detail:" in mobile_src
    assert "_build_mobile_header(state, open_settings=open_settings)" in mobile_src
    assert "_build_mobile_nav(state, rebuild_main=rebuild_main, open_settings=open_settings)" in mobile_src
    assert "open_settings(\"Providers\")" not in header_section
    assert "width: 24px; height: 24px" in header_section
    assert "text-subtitle2 ellipsis" in header_section


def test_mobile_reuses_shared_skill_load_transcript_renderer() -> None:
    app_src = Path("src/row_bot/app.py").read_text(encoding="utf-8")
    mobile_src = Path("src/row_bot/ui/mobile.py").read_text(encoding="utf-8")
    render_src = Path("src/row_bot/ui/render.py").read_text(encoding="utf-8")

    assert "add_chat_message=(" in app_src
    assert "add_chat_message" in mobile_src
    assert "render_skill_load_stub" in render_src
    assert "skill_load" not in mobile_src
