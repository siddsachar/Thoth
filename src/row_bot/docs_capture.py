"""Safe capture support for real public documentation screenshots.

This module is intentionally small and boring: it can freeze time, reduce
motion, seed safe demo state, and disable side-effect-heavy startup paths. It
must never render replacement screenshot UI.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from row_bot.data_paths import get_row_bot_data_dir


DOCS_CAPTURE_ENV = "ROW_BOT_DOCS_CAPTURE"
DOCS_FIXED_NOW_ENV = "ROW_BOT_DOCS_FIXED_NOW"
DOCS_DISABLE_NETWORK_ENV = "ROW_BOT_DOCS_DISABLE_NETWORK"
DOCS_DISABLE_AUTOSTART_ENV = "ROW_BOT_DOCS_DISABLE_AUTOSTART"
DOCS_REDUCE_MOTION_ENV = "ROW_BOT_DOCS_REDUCE_MOTION"
DOCS_FAKE_PROVIDERS_ENV = "ROW_BOT_DOCS_FAKE_PROVIDERS"
DOCS_REAL_DATA_ENV = "ROW_BOT_DOCS_REAL_DATA"
DOCS_DEMO_STATE_FILE = "docs_real_ui_demo_state.json"
DEMO_THREAD_ID = "docs-demo-chat"

SCENARIOS = {
    "first-run",
    "configured",
    "chat",
    "workflows",
    "designer",
    "developer",
    "knowledge",
    "settings",
    "channels",
    "voice",
    "mcp",
    "plugins",
    "mobile",
    "full",
}

ALLOWED_EMAIL_DOMAINS = {"example.com", "example.test"}
SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"ghp_[A-Za-z0-9_]{16,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"AIza[0-9A-Za-z_-]{20,}"),
    re.compile(r"BEGIN (?:RSA |OPENSSH |EC |)PRIVATE KEY"),
    re.compile(r"C:\\Users\\", re.IGNORECASE),
    re.compile(r"/Users/[^/\s]+"),
]


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def is_docs_capture() -> bool:
    return _truthy(os.environ.get(DOCS_CAPTURE_ENV))


def is_docs_real_data_capture() -> bool:
    """Return whether this already-opted-in docs process uses the real profile."""

    return is_docs_capture() and _truthy(os.environ.get(DOCS_REAL_DATA_ENV))


def docs_capture_fixed_now() -> datetime:
    raw = os.environ.get(DOCS_FIXED_NOW_ENV) or "2026-06-18T09:00:00Z"
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return datetime(2026, 6, 18, 9, 0, tzinfo=timezone.utc)


def docs_capture_disable_network() -> bool:
    return is_docs_capture() and _truthy(os.environ.get(DOCS_DISABLE_NETWORK_ENV, "1"))


def docs_capture_disable_autostart() -> bool:
    return is_docs_capture() and _truthy(os.environ.get(DOCS_DISABLE_AUTOSTART_ENV, "1"))


def docs_capture_fake_provider_status() -> bool:
    return is_docs_capture() and _truthy(os.environ.get(DOCS_FAKE_PROVIDERS_ENV, "1"))


def docs_capture_provider_cards() -> list[dict[str, Any]]:
    """Return display-only provider states for isolated documentation capture."""
    if not docs_capture_fake_provider_status():
        return []
    return [
        {
            "provider_id": "ollama",
            "display_name": "Ollama Local",
            "icon": "OL",
            "group": "Local",
            "configured": True,
            "runtime_enabled": True,
            "source": "local_daemon",
            "model_count": 3,
            "chat_count": 3,
            "media_count": 0,
            "risk_label": "local_private",
        },
        {
            "provider_id": "codex",
            "display_name": "ChatGPT / Codex",
            "icon": "C",
            "group": "Subscription Accounts",
            "configured": False,
            "runtime_enabled": False,
            "source": "",
            "model_count": 3,
            "chat_count": 3,
            "media_count": 0,
            "risk_label": "subscription",
        },
        {
            "provider_id": "claude_subscription",
            "display_name": "Claude Subscription",
            "icon": "CS",
            "group": "Subscription Accounts",
            "configured": False,
            "runtime_enabled": False,
            "source": "",
            "model_count": 4,
            "chat_count": 4,
            "media_count": 0,
            "risk_label": "subscription",
        },
        {
            "provider_id": "xai_oauth",
            "display_name": "xAI Grok",
            "icon": "X",
            "group": "Subscription Accounts",
            "configured": False,
            "runtime_enabled": False,
            "source": "",
            "oauth_client_id_configured": True,
            "oauth_client_id_source": "default",
            "model_count": 4,
            "chat_count": 3,
            "media_count": 1,
            "risk_label": "subscription",
        },
        {
            "provider_id": "openai",
            "display_name": "OpenAI API",
            "icon": "AI",
            "group": "API Providers",
            "configured": False,
            "runtime_enabled": False,
            "source": "",
            "model_count": 8,
            "chat_count": 6,
            "media_count": 2,
            "risk_label": "api_key",
        },
        {
            "provider_id": "anthropic",
            "display_name": "Anthropic API",
            "icon": "AC",
            "group": "API Providers",
            "configured": False,
            "runtime_enabled": False,
            "source": "",
            "model_count": 5,
            "chat_count": 5,
            "media_count": 0,
            "risk_label": "api_key",
        },
        {
            "provider_id": "google",
            "display_name": "Google AI API",
            "icon": "G",
            "group": "API Providers",
            "configured": False,
            "runtime_enabled": False,
            "source": "",
            "model_count": 7,
            "chat_count": 5,
            "media_count": 2,
            "risk_label": "api_key",
        },
    ]


def docs_capture_model_choices() -> list[dict[str, str]]:
    """Return inert, display-only Quick Choices for screenshot capture."""
    if not docs_capture_fake_provider_status():
        return []
    return [
        {
            "value": "model:ollama:llama3.1:8b",
            "label": "Ollama Local · llama3.1:8b",
        },
        {
            "value": "model:custom:demo-chat",
            "label": "Custom local endpoint · demo-chat",
        },
    ]


def docs_capture_reduce_motion_css() -> str:
    if not (is_docs_capture() and _truthy(os.environ.get(DOCS_REDUCE_MOTION_ENV, "1"))):
        return ""
    return """
<style>
html[data-row-bot-docs-capture="1"] *, html[data-row-bot-docs-capture="1"] *::before, html[data-row-bot-docs-capture="1"] *::after {
  animation-duration: 0.001ms !important;
  animation-iteration-count: 1 !important;
  scroll-behavior: auto !important;
  transition-duration: 0.001ms !important;
}
</style>
""".strip()


def docs_capture_bootstrap_html() -> str:
    if not is_docs_capture():
        return ""
    return """
<script>
(() => {
  if (window.__rowBotDocsCaptureInstalled) return;
  window.__rowBotDocsCaptureInstalled = true;
  document.documentElement.setAttribute('data-row-bot-docs-capture', '1');
  const mark = () => {
    if (document.body) document.body.setAttribute('data-docs-id', 'app-shell');
    document.querySelectorAll('input[type="password"], [autocomplete*="token"], [autocomplete*="password"]').forEach((el) => {
      el.setAttribute('data-sensitive', 'true');
    });
  };
  mark();
  new MutationObserver(mark).observe(document.documentElement, {childList: true, subtree: true});
})();
</script>
""".strip()


def docs_capture_demo_state_path(data_dir: Path | None = None) -> Path:
    root = data_dir or get_row_bot_data_dir()
    return root / DOCS_DEMO_STATE_FILE


def load_docs_capture_demo_state(data_dir: Path | None = None) -> dict[str, Any]:
    path = docs_capture_demo_state_path(data_dir)
    if not path.exists():
        return default_docs_capture_demo_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default_docs_capture_demo_state()
    return data if isinstance(data, dict) else default_docs_capture_demo_state()


def write_docs_capture_demo_state(data_dir: Path, scenario: str = "full") -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    data = default_docs_capture_demo_state()
    data["scenario"] = scenario
    path = docs_capture_demo_state_path(data_dir)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def default_docs_capture_demo_state() -> dict[str, Any]:
    now = docs_capture_fixed_now().isoformat().replace("+00:00", "Z")
    return {
        "scenario": "full",
        "generated_at": now,
        "workspace": "%ROW_BOT_DATA_DIR%/docs-demo-workspace",
        "account_email": "demo.operator@example.com",
        "thread_id": DEMO_THREAD_ID,
        "thread_name": "Demo launch checklist",
        "model": "llama3.1:8b",
        "messages": [
            {
                "role": "user",
                "content": "Summarize the launch checklist for the demo workspace.",
            },
            {
                "role": "assistant",
                "content": (
                    "I found the project brief, checked two indexed documents, "
                    "and drafted a five-step checklist. The remaining action is "
                    "waiting for approval before writing a summary file."
                ),
                "tool_results": [
                    {"name": "filesystem.search", "content": "Found launch-checklist.md in the demo workspace."},
                    {"name": "documents.search", "content": "Matched Launch brief.pdf and Support FAQ.md."},
                ],
            },
        ],
        "threads": [
            {"id": DEMO_THREAD_ID, "name": "Demo launch checklist", "kind": "chat"},
            {"id": "docs-demo-research", "name": "Research digest", "kind": "chat"},
            {"id": "docs-demo-workflow", "name": "Morning brief run", "kind": "workflow"},
        ],
        "profiles": [
            {
                "id": "docs-profile-research",
                "slug": "demo_research_guide",
                "display_name": "Research Guide",
                "description": "Reviews local sources and returns a concise, cited summary.",
                "capability": "read_only",
            },
            {
                "id": "docs-profile-project",
                "slug": "demo_project_coordinator",
                "display_name": "Project Coordinator",
                "description": "Breaks a safe internal project into reviewable steps.",
                "capability": "orchestrator",
            },
        ],
        "goal": {
            "id": "docs-goal-launch",
            "thread_id": DEMO_THREAD_ID,
            "objective": "Prepare and verify the fictional launch checklist",
            "status": "active",
            "turns_used": 3,
            "max_turns": 8,
            "progress": ["Indexed the launch brief", "Drafted the five-step checklist"],
            "blockers": ["Approval required before writing the summary file"],
        },
        "agents": [
            {
                "id": "docs-agent-parent",
                "thread_id": DEMO_THREAD_ID,
                "display_name": "Launch coordinator",
                "kind": "subagent",
                "status": "running",
                "summary": "Coordinating two safe review tasks.",
            },
            {
                "id": "docs-agent-child-complete",
                "parent_run_id": "docs-agent-parent",
                "thread_id": "docs-agent-source-review",
                "display_name": "Source reviewer",
                "kind": "subagent",
                "status": "completed",
                "summary": "Checked the two local demo documents.",
            },
            {
                "id": "docs-agent-child-running",
                "parent_run_id": "docs-agent-parent",
                "thread_id": "docs-agent-checklist",
                "display_name": "Checklist editor",
                "kind": "subagent",
                "status": "running",
                "summary": "Preparing a reviewable checklist draft.",
            },
        ],
        "workflows": [
            {"id": "docs-workflow-brief", "name": "Morning Brief", "status": "Paused", "next": "Weekdays 08:30"},
            {"id": "docs-workflow-approval", "name": "Launch Summary", "status": "Needs approval", "next": "Manual"},
            {"id": "docs-workflow-research", "name": "Research Digest", "status": "Ready", "next": "Fridays 16:00"},
        ],
        "workflow_runs": [
            {"id": "docs-run-complete", "workflow": "Morning Brief", "status": "completed", "steps": "3/3"},
            {"id": "docs-run-failed", "workflow": "Research Digest", "status": "failed", "steps": "1/2", "message": "Demo source unavailable; safe to retry."},
        ],
        "documents": [
            {"title": "Launch brief.pdf", "status": "Indexed"},
            {"title": "Support FAQ.md", "status": "Indexed"},
        ],
        "knowledge": {
            "entities": [
                {"id": "docs-entity-launch", "subject": "Demo Launch", "type": "project", "description": "A fictional product launch used only for documentation."},
                {"id": "docs-entity-checklist", "subject": "Launch Checklist", "type": "fact", "description": "Five review steps derived from the demo brief."},
                {"id": "docs-entity-review", "subject": "Safety Review", "type": "concept", "description": "Approval is required before consequential writes."},
                {"id": "docs-entity-support", "subject": "Support FAQ", "type": "fact", "description": "Answers for a fictional support team."},
            ],
            "relations": [
                ["docs-entity-launch", "docs-entity-checklist", "uses"],
                ["docs-entity-checklist", "docs-entity-review", "builds_on"],
                ["docs-entity-support", "docs-entity-launch", "part_of"],
            ],
            "wiki_pages": ["Demo Launch", "Launch Checklist", "Safety Review"],
        },
        "developer": {
            "workspace_id": "",
            "name": "demo-release-notes",
            "branch": "docs/demo-checklist",
            "todo": "Add a recovery note to the fictional launch checklist.",
            "test_output": "3 passed in 0.04s",
        },
        "designer": {
            "project_id": "docs-designer-project",
            "name": "Community Workshop Deck",
            "template": "Clean presentation",
            "export": "PDF and PowerPoint ready",
        },
        "providers": [
            {"name": "Ollama Local", "status": "Ready", "model": "llama3.1:8b"},
            {"name": "Custom local endpoint", "status": "Ready", "model": "demo-chat"},
            {"name": "OpenAI API", "status": "Not connected", "model": "gpt-4.1-mini"},
            {"name": "ChatGPT / Codex", "status": "Available to connect", "model": "subscription"},
            {"name": "xAI Grok", "status": "Available to connect", "model": "OAuth or API"},
        ],
        "channels": [
            {"name": "Telegram", "status": "Configured, stopped"},
            {"name": "WhatsApp", "status": "Configured, stopped"},
            {"name": "Slack", "status": "Configured, stopped"},
            {"name": "Discord", "status": "Configured, stopped"},
            {"name": "SMS", "status": "Not configured"},
        ],
        "mcp": [
            {"name": "GitHub MCP Server", "status": "Configured, disabled"},
            {"name": "Playwright MCP", "status": "Configured, disabled"},
        ],
        "plugins": [
            {"name": "Demo CRM Lookup", "status": "Installed, disabled"},
            {"name": "Invoice Helper", "status": "Install review required"},
        ],
        "integrations": [
            {"name": "GitHub", "status": "Not connected"},
            {"name": "Google Gmail and Calendar", "status": "Not connected"},
            {"name": "X", "status": "Not connected"},
        ],
        "mobile": {
            "device_name": "Demo Android Phone",
            "status": "Paired",
            "access_mode": "Trusted LAN",
            "events": ["Paired", "Access granted", "Session refreshed"],
        },
    }


def docs_capture_query_params(client: Any) -> dict[str, str]:
    try:
        params = getattr(getattr(client, "request", None), "query_params", {})
        return {str(key): str(value) for key, value in dict(params).items()}
    except Exception:
        return {}


def configure_docs_capture_state(
    state: Any,
    query: dict[str, str],
    *,
    load_messages: Callable[[str], list[dict[str, Any]]] | None = None,
) -> dict[str, str]:
    """Apply capture-only navigation state to the real app state object."""
    if not is_docs_capture():
        return {}
    intent = {
        "surface": query.get("docs_surface", ""),
        "home_tab": query.get("home_tab", ""),
        "settings_tab": query.get("settings_tab", ""),
        "dialog": query.get("dialog", ""),
        "mobile_view": query.get("mobile_view", ""),
    }
    real_data = is_docs_real_data_capture()
    demo = {} if real_data else load_docs_capture_demo_state()
    state.active_designer_project = None
    state.active_developer_workspace_id = None
    if intent["mobile_view"]:
        state.mobile_view = intent["mobile_view"].strip().title()
    if intent["surface"] == "designer-editor":
        from row_bot.designer.storage import list_projects, load_project

        project_id = str((demo.get("designer") or {}).get("project_id") or "")
        if real_data:
            projects = list_projects()
            project_id = str(projects[0].get("id") or "") if projects else ""
        state.active_designer_project = load_project(project_id)
        state.thread_id = "docs-designer-thread"
        state.thread_name = (
            str(getattr(state.active_designer_project, "name", "") or "Designer project")
            if real_data
            else "Community Workshop Deck"
        )
        state.messages = []
        return intent
    if intent["surface"] == "developer-workspace":
        workspace_id = str((demo.get("developer") or {}).get("workspace_id") or "")
        if real_data:
            from row_bot.developer.storage import list_workspaces

            workspaces = list_workspaces()
            workspace_id = str(workspaces[0].id) if workspaces else ""
        state.active_developer_workspace_id = workspace_id
        state.thread_id = "docs-developer-thread"
        state.thread_name = "Developer workspace" if real_data else "Demo release notes"
        state.messages = []
        return intent
    if intent["home_tab"]:
        state.thread_id = None
        state.thread_name = None
        state.messages = []
        state.preferred_home_tab = intent["home_tab"]
        return intent
    if intent["settings_tab"] or intent["dialog"] in {
        "setup-center",
        "skills-hub",
        "plugin-marketplace",
        "mcp-add-server",
        "mcp-marketplace",
    }:
        if intent["settings_tab"] == "Plugins":
            # Normal startup loads plugins in a later background phase. The
            # capture startup is intentionally abbreviated, so load only the
            # inert plugin already seeded into the isolated demo directory.
            from row_bot.plugins.loader import load_plugins

            load_plugins()
        state.thread_id = None
        state.thread_name = None
        state.messages = []
        state.preferred_home_tab = "Workflows"
        return intent
    if intent["surface"].startswith("chat") or query.get("thread_id"):
        thread_id = query.get("thread_id") or str(demo.get("thread_id") or DEMO_THREAD_ID)
        thread_name = str(demo.get("thread_name") or "Demo thread")
        if real_data and (not thread_id or thread_id == DEMO_THREAD_ID):
            from row_bot.threads import _list_threads

            rows = _list_threads(include_details=True)
            row = next(
                (
                    item
                    for item in rows
                    if str(item[6] or "chat").casefold() in {"", "chat"}
                ),
                None,
            )
            if row is not None:
                thread_id = str(row[0])
                thread_name = str(row[1] or "Conversation")
        state.thread_id = thread_id
        state.thread_name = thread_name
        state.thread_model_override = str(demo.get("model") or "")
        loaded = load_messages(thread_id) if load_messages else []
        state.messages = loaded or list(demo.get("messages") or [])
        if intent["mobile_view"] and thread_id:
            state.mobile_chat_mode = "thread"
        return intent
    state.thread_id = None
    state.thread_name = None
    state.messages = []
    state.preferred_home_tab = intent["home_tab"] or "Workflows"
    return intent


def scan_demo_data_safety(data_dir: Path) -> list[str]:
    errors: list[str] = []
    data_dir = data_dir.resolve()
    payload_parts: list[str] = []
    for path in sorted(data_dir.rglob("*")):
        if not path.is_file():
            continue
        try:
            resolved = path.resolve()
        except Exception:
            errors.append(f"Could not resolve demo path: {path}")
            continue
        if data_dir not in resolved.parents and resolved != data_dir:
            errors.append(f"Demo file escaped data dir: {resolved}")
        if path.stat().st_size > 2_000_000:
            continue
        try:
            payload_parts.append(path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
    payload = "\n".join(payload_parts)
    for pattern in SECRET_PATTERNS:
        if pattern.search(payload):
            errors.append(f"Demo data contains blocked pattern: {pattern.pattern}")
    for email in re.findall(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})", payload):
        if email.lower() not in ALLOWED_EMAIL_DOMAINS:
            errors.append(f"Demo data contains non-example email domain: {email}")
    return errors
