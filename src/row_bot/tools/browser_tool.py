"""Thin tool schemas and registry for Row-Bot's managed Python Playwright backend."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from row_bot.browser import BrowserSession, BrowserSessionManager
from row_bot.browser.history import (
    append_browser_history as _append_history,
    clear_browser_history as _clear_history,
    get_browser_history as _get_history,
)
from row_bot.browser.history import HISTORY_PATH as _DEFAULT_HISTORY_PATH
from row_bot.browser.policy import (
    consequential_browser_target as _consequential_browser_target,
    history_url as _history_url,
    navigation_policy as _navigation_policy,
)
from row_bot.browser.service import BrowserWorkItem as _BrowserWorkItem
from row_bot.browser.service import browser_runs_headless as _browser_runs_headless
from row_bot.tools import registry
from row_bot.tools.base import BaseTool


_HISTORY_PATH = _DEFAULT_HISTORY_PATH


def _build_snapshot_js(max_elements: int) -> str:
    """Compatibility source contract: values stay hidden and no DOM refs are written."""

    return f"""
() => ({{
  max_elements: {max(0, int(max_elements))},
  value_length: true,
  collector: 'exact ephemeral element handles; no mutable DOM reference attributes'
}})
"""


def get_browser_history(thread_id: str) -> list[dict]:
    return _get_history(thread_id, path=_HISTORY_PATH)


def append_browser_history(thread_id: str, entry: dict) -> None:
    _append_history(thread_id, entry, path=_HISTORY_PATH)


def clear_browser_history(thread_id: str) -> None:
    _clear_history(thread_id, path=_HISTORY_PATH)


def _get_thread_id() -> str:
    try:
        from row_bot.agent import _current_thread_id_var

        return _current_thread_id_var.get() or "default"
    except ImportError:
        return "default"


class _NavigateInput(BaseModel):
    url: str = Field(description="The HTTP(S) URL to open in Row-Bot's managed browser")


class _ClickInput(BaseModel):
    ref: str = Field(description="Opaque target token from the current managed-browser observation")


class _TypeInput(BaseModel):
    ref: str = Field(description="Opaque input token from the current managed-browser observation")
    text: str = Field(description="Text to enter; the value is never returned or persisted")
    submit: bool = Field(default=False, description="Press Enter after typing")


class _ScrollInput(BaseModel):
    direction: str = Field(default="down", description="Scroll direction: up or down")
    amount: int = Field(default=3, ge=1, le=20, description="Number of approximately 400-pixel steps")


class _TabInput(BaseModel):
    action: str = Field(default="list", description="Tab action: list, switch, new, or close")
    tab_id: Optional[int] = Field(default=None, description="Owned tab index for switch/close")
    url: Optional[str] = Field(default=None, description="Optional HTTP(S) URL for a new owned tab")


_session_manager = BrowserSessionManager()


def get_session_manager() -> BrowserSessionManager:
    return _session_manager


def _normalise_url(url: str) -> tuple[str | None, str]:
    selected = str(url or "").strip()
    if selected.casefold().startswith("javascript:"):
        return None, "Error: javascript: URLs are not allowed."
    if not selected.casefold().startswith(("http://", "https://")):
        selected = "https://" + selected
    return selected, ""


class BrowserTool(BaseTool):
    @property
    def name(self) -> str:
        return "browser"

    @property
    def display_name(self) -> str:
        return "🌐 Browser"

    @property
    def description(self) -> str:
        return (
            "Navigate websites and automate ordinary web pages in Row-Bot's owned browser profile using direct Python Playwright. "
            "Use Computer Use instead for an already-open browser, browser chrome, native dialogs, or OS windows."
        )

    @property
    def enabled_by_default(self) -> bool:
        return True

    @property
    def config_schema(self) -> dict[str, dict]:
        return {}

    @property
    def destructive_tool_names(self) -> set[str]:
        return set()

    def as_langchain_tools(self) -> list:
        def browser_navigate(url: str) -> str:
            """Open an ordinary website in Row-Bot's managed browser and return one bounded observation."""

            selected, error = _normalise_url(url)
            if selected is None:
                return error
            thread_id = _get_thread_id()
            session = _session_manager.get_session(thread_id)
            policy, reason = _navigation_policy(selected, session.current_url(thread_id))
            if policy == "block":
                return f"BLOCKED: {reason}"
            if policy == "ask":
                from row_bot.tools.approval_gate import gate_action

                session.mark_waiting_approval(thread_id, "Approve website navigation")
                blocked = gate_action({
                    "tool": "browser_navigate", "label": "Managed Browser navigation",
                    "action": "navigate", "origin_and_path": _history_url(selected), "reason": reason,
                })
                if blocked:
                    session.end_activity(thread_id)
                    return blocked
            result = session.navigate(selected, thread_id)
            append_browser_history(thread_id, {
                "action": "navigate", "url": _history_url(selected), "timestamp": datetime.now().isoformat(),
            })
            return result

        def browser_click(ref: str) -> str:
            """Click one exact opaque target from the current managed-browser observation."""

            thread_id = _get_thread_id()
            session = _session_manager.get_session(thread_id)
            metadata = session.describe_ref(ref, thread_id)
            if not metadata:
                return "ERROR [stale_observation]: Observe the same managed page again before retrying."
            consequence = _consequential_browser_target(metadata)
            if consequence:
                from row_bot.tools.approval_gate import gate_action

                session.mark_waiting_approval(thread_id, "Approve page action")
                blocked = gate_action({
                    "tool": "browser_click", "label": "Managed Browser consequential action",
                    "action": "click", "target": str(metadata.get("label") or "control")[:160],
                    "reason": consequence,
                })
                if blocked:
                    session.end_activity(thread_id)
                    return blocked
                return "ERROR [stale_observation]: Approval invalidated the page snapshot; observe and re-approve the exact target."
            result = session.click(ref, thread_id)
            append_browser_history(thread_id, {
                "action": "click", "target_token_revision": str(ref).split("_", 1)[0],
                "timestamp": datetime.now().isoformat(),
            })
            return result

        def browser_type(ref: str, text: str, submit: bool = False) -> str:
            """Enter hidden text into one exact opaque input; optionally submit it."""

            thread_id = _get_thread_id()
            session = _session_manager.get_session(thread_id)
            metadata = session.describe_ref(ref, thread_id)
            if not metadata:
                return "ERROR [stale_observation]: Observe the same managed page again before retrying."
            consequence = _consequential_browser_target(metadata, submit=submit)
            if consequence:
                from row_bot.tools.approval_gate import gate_action

                session.mark_waiting_approval(thread_id, "Approve form action")
                blocked = gate_action({
                    "tool": "browser_type", "label": "Managed Browser form action",
                    "action": "type_and_submit" if submit else "type",
                    "target": str(metadata.get("label") or "field")[:160], "reason": consequence,
                    "data_summary": f"Text entry ({len(text)} characters; value hidden)",
                })
                if blocked:
                    session.end_activity(thread_id)
                    return blocked
                return "ERROR [stale_observation]: Approval invalidated the page snapshot; observe and re-approve the exact target."
            result = session.type_text(ref, text, submit, thread_id)
            append_browser_history(thread_id, {
                "action": "type", "target_token_revision": str(ref).split("_", 1)[0],
                "text_length": len(text), "submit": submit, "timestamp": datetime.now().isoformat(),
            })
            return result

        def browser_scroll(direction: str = "down", amount: int = 3) -> str:
            """Scroll the current owned page and return one fresh semantic observation."""

            thread_id = _get_thread_id()
            result = _session_manager.get_session(thread_id).scroll(direction, amount, thread_id)
            append_browser_history(thread_id, {
                "action": "scroll", "direction": direction, "amount": amount,
                "timestamp": datetime.now().isoformat(),
            })
            return result

        def browser_snapshot() -> str:
            """Replace the current ephemeral observation and return new opaque target tokens."""

            thread_id = _get_thread_id()
            return _session_manager.get_session(thread_id).snapshot(thread_id)

        def browser_back() -> str:
            """Navigate the owned page back and return one fresh observation."""

            thread_id = _get_thread_id()
            result = _session_manager.get_session(thread_id).go_back(thread_id)
            append_browser_history(thread_id, {"action": "back", "timestamp": datetime.now().isoformat()})
            return result

        def browser_tab(action: str = "list", tab_id: int | None = None, url: str | None = None) -> str:
            """List, switch, open, or close only tabs owned by this task."""

            if action == "new" and url:
                selected, error = _normalise_url(url)
                if selected is None:
                    return error
                url = selected
            thread_id = _get_thread_id()
            session = _session_manager.get_session(thread_id)
            if action == "new" and url:
                policy, reason = _navigation_policy(url, session.current_url(thread_id))
                if policy == "block":
                    return f"BLOCKED: {reason}"
                if policy == "ask":
                    from row_bot.tools.approval_gate import gate_action

                    session.mark_waiting_approval(thread_id, "Approve new task tab")
                    blocked = gate_action({
                        "tool": "browser_tab", "label": "Managed Browser new-tab navigation",
                        "action": "new_tab", "origin_and_path": _history_url(url), "reason": reason,
                    })
                    if blocked:
                        session.end_activity(thread_id)
                        return blocked
            result = session.tab_action(action, tab_id, url, thread_id)
            append_browser_history(thread_id, {
                "action": f"tab_{action}", "tab_id": tab_id,
                "url": _history_url(url) if url else None, "timestamp": datetime.now().isoformat(),
            })
            return result

        return [
            StructuredTool.from_function(
                func=browser_navigate, name="browser_navigate",
                description="Open an ordinary website in Row-Bot's owned managed browser profile; returns opaque exact target tokens.",
                args_schema=_NavigateInput,
            ),
            StructuredTool.from_function(
                func=browser_click, name="browser_click",
                description="Click an exact opaque target from the current managed-browser observation.",
                args_schema=_ClickInput,
            ),
            StructuredTool.from_function(
                func=browser_type, name="browser_type",
                description="Enter hidden text into an exact current managed-browser input target.",
                args_schema=_TypeInput,
            ),
            StructuredTool.from_function(
                func=browser_scroll, name="browser_scroll",
                description="Scroll the current managed page and return one semantic observation.",
                args_schema=_ScrollInput,
            ),
            StructuredTool.from_function(
                func=browser_snapshot, name="browser_snapshot",
                description="Refresh the managed page observation; old opaque target tokens become stale.",
            ),
            StructuredTool.from_function(
                func=browser_back, name="browser_back",
                description="Navigate the current owned managed-browser page back.",
            ),
            StructuredTool.from_function(
                func=browser_tab, name="browser_tab",
                description="Manage only this task's tabs in Row-Bot's owned browser profile.",
                args_schema=_TabInput,
            ),
        ]


registry.register(BrowserTool())
