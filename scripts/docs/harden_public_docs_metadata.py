"""Harden public docs metadata for the complete user guide pass."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[2]
META = ROOT / "docs-content" / "metadata"


def load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def dump(path: Path, data: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=False), encoding="utf-8")


def harden_screenshots() -> None:
    path = META / "screenshots.yml"
    data = load(path)
    screenshots = data.setdefault("screenshots", {})
    for shot_id, shot in screenshots.items():
        if not isinstance(shot, dict):
            continue
        shot["id"] = shot_id
        shot.setdefault("page", (shot.get("docs_pages") or [""])[0])
        shot.setdefault("purpose", shot.get("title") or shot_id.replace("-", " ").title())
        shot.setdefault("selector", shot.get("capture_selector") or shot.get("wait_for") or "")
        shot.setdefault("source", "real-data-dir")
        shot.setdefault("review_status", "needs-review")
        shot.setdefault("public_asset", shot.get("status") == "required")
        if shot.get("status") == "deferred":
            shot["public_asset"] = False
        if shot_id == "first-launch-setup-wizard":
            shot["source"] = "isolated-first-launch"
        if shot_id == "app-shell-overview":
            shot["title"] = "Row-Bot Interface Overview"
            shot["purpose"] = "Row-Bot Interface overview"
            shot["alt"] = "Row-Bot main interface with sidebar, Home tabs, Activity Center, Buddy, Settings, and terminal."
            shot["expected_text"] = ["Workflows", "Conversations"]
        if shot_id == "chat-main":
            shot["alt"] = "Row-Bot chat view with conversation history, tool results, and composer controls."
        if shot_id == "chat-tool-trace":
            shot["reason"] = "Tool-result rendering is present in chat history but lacks a dedicated stable trace selector for focused capture."
            shot["follow_up"] = "Add selectors in row_bot.ui.tool_trace and capture the real rendered trace."
        if shot_id == "designer-editor":
            shot["reason"] = "Designer editor capture needs a safe Designer project record and editor-route state before it can be repeated without side effects."
            shot["follow_up"] = "Seed or select a review-safe Designer project through storage APIs and capture the real editor."
        deferred_pages = {
            "chat-model-picker": "/docs/chat/model-picker",
            "chat-tool-trace": "/docs/chat/tools-approvals-and-terminal",
            "chat-approval": "/docs/chat/tools-approvals-and-terminal",
            "designer-editor": "/docs/designer/",
            "developer-workspace": "/docs/developer/",
            "plugin-marketplace": "/docs/integrations/plugins",
            "mcp-add-server": "/docs/integrations/mcp",
        }
        if shot_id in deferred_pages:
            shot["page"] = deferred_pages[shot_id]
    dump(path, data)


def harden_surfaces() -> None:
    path = META / "ui_surfaces.yml"
    data = load(path)
    surfaces = data.setdefault("surfaces", {})
    app_shell = surfaces.get("app_shell")
    if isinstance(app_shell, dict):
        app_shell["title"] = "Row-Bot Interface"
        app_shell["description"] = (
            "Main Row-Bot interface with sidebar, thread list, Home tabs, "
            "Activity Center, Buddy, Settings, and terminal panel."
        )
    dump(path, data)


def harden_guides() -> None:
    path = META / "how_to_guides.yml"
    data = load(path)
    guides = data.setdefault("guides", {})
    updates = {
        "create-workflow": {"route": "/docs/guides/workflows"},
        "use-designer": {"route": "/docs/designer/"},
        "use-developer": {"route": "/docs/developer/"},
        "configure-models": {"route": "/docs/configuration/models-and-providers"},
        "use-chat": {"route": "/docs/chat/"},
        "use-skills-plugins-mcp": {"route": "/docs/skills/"},
        "configure-channels-voice": {"route": "/docs/integrations/channels"},
        "review-knowledge": {
            "title": "Review Knowledge",
            "route": "/docs/knowledge/",
            "sources": ["src/row_bot/knowledge_graph.py", "src/row_bot/ui/graph_panel.py"],
        },
        "review-monitor": {
            "title": "Use Monitor",
            "route": "/docs/monitor/",
            "sources": ["src/row_bot/ui/home.py", "src/row_bot/startup_diagnostics.py"],
        },
    }
    for key, values in updates.items():
        guides.setdefault(key, {}).update(values)
    dump(path, data)


def harden_routes() -> None:
    path = META / "docs_routes.yml"
    data = load(path)
    routes = data.setdefault("routes", {})
    additions = {
        "/docs/app-shell/agent-profiles": {"file": "docs-site/docs/app-shell/agent-profiles.mdx", "owner": "app-shell"},
        "/docs/guides/workflows": {"file": "docs-site/docs/guides/workflows.mdx", "owner": "workflows"},
        "/docs/designer/": {"file": "docs-site/docs/designer/index.mdx", "owner": "designer"},
        "/docs/developer/": {"file": "docs-site/docs/developer/index.mdx", "owner": "developer"},
        "/docs/knowledge/": {"file": "docs-site/docs/knowledge/index.mdx", "owner": "knowledge"},
        "/docs/monitor/": {"file": "docs-site/docs/monitor/index.mdx", "owner": "monitor"},
        "/docs/settings/providers": {"file": "docs-site/docs/settings/providers.mdx", "owner": "settings"},
        "/docs/settings/models": {"file": "docs-site/docs/settings/models.mdx", "owner": "settings"},
        "/docs/settings/documents": {"file": "docs-site/docs/settings/documents.mdx", "owner": "settings"},
        "/docs/settings/tools": {"file": "docs-site/docs/settings/tools.mdx", "owner": "settings"},
        "/docs/settings/skills": {"file": "docs-site/docs/settings/skills.mdx", "owner": "settings"},
        "/docs/settings/system": {"file": "docs-site/docs/settings/system.mdx", "owner": "settings"},
        "/docs/settings/accounts": {"file": "docs-site/docs/settings/accounts.mdx", "owner": "settings"},
        "/docs/settings/utilities": {"file": "docs-site/docs/settings/utilities.mdx", "owner": "settings"},
        "/docs/settings/tracker": {"file": "docs-site/docs/settings/tracker.mdx", "owner": "settings"},
        "/docs/settings/knowledge": {"file": "docs-site/docs/settings/knowledge.mdx", "owner": "settings"},
        "/docs/settings/buddy": {"file": "docs-site/docs/settings/buddy.mdx", "owner": "settings"},
        "/docs/settings/voice": {"file": "docs-site/docs/settings/voice.mdx", "owner": "settings"},
        "/docs/settings/channels": {"file": "docs-site/docs/settings/channels.mdx", "owner": "settings"},
        "/docs/settings/mcp": {"file": "docs-site/docs/settings/mcp.mdx", "owner": "settings"},
        "/docs/settings/plugins": {"file": "docs-site/docs/settings/plugins.mdx", "owner": "settings"},
        "/docs/settings/preferences": {"file": "docs-site/docs/settings/preferences.mdx", "owner": "settings"},
    }
    routes.update(additions)
    dump(path, data)


def harden_settings() -> None:
    path = META / "settings.yml"
    data = load(path)
    tabs = data.setdefault("tabs", {})
    buddy = tabs.get("Buddy")
    if isinstance(buddy, dict):
        buddy["description"] = "Configure companion behavior, look, overlay mode, custom looks, and motion."
    dump(path, data)


def main() -> int:
    harden_screenshots()
    harden_surfaces()
    harden_guides()
    harden_routes()
    harden_settings()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
