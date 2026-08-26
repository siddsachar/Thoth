from __future__ import annotations

import json
from pathlib import Path

from row_bot.prompts import _AGENT_GUIDELINES, AGENT_BG_OVERRIDE
from row_bot.tools.browser_tool import BrowserTool
from row_bot.tools.computer_use_tool import ComputerUseTool


def test_prompt_prefers_structured_then_browser_then_computer() -> None:
    assert "structured Row-Bot tool or plugin" in _AGENT_GUIDELINES
    assert "use Browser for ordinary website navigation" in _AGENT_GUIDELINES
    assert "use computer_use for native desktop apps" in _AGENT_GUIDELINES
    assert "do not silently switch interaction engines" in _AGENT_GUIDELINES
    for workflow_marker in (
        "replace_text",
        "capture_after",
        "visual_question",
        "key_sequence",
        "at most three reversible mutations",
        "action_dispatched=true",
        "free-form Vision prose",
    ):
        assert workflow_marker not in _AGENT_GUIDELINES
    assert "Computer Use is unavailable in background tasks" in AGENT_BG_OVERRIDE


def test_existing_browser_guidance_uses_computer_while_ordinary_navigation_stays_managed() -> None:
    computer_description = ComputerUseTool().description
    browser_description = BrowserTool().description

    assert "ordinary website navigation" in _AGENT_GUIDELINES
    assert "separate managed browser" in _AGENT_GUIDELINES
    assert "already-open native browser windows" in computer_description
    assert "service policy is authoritative" in computer_description
    assert "Navigate websites" in browser_description


def test_computer_static_guidance_is_tool_bound_deduplicated_and_within_budget() -> None:
    from row_bot import skills

    skills.load_skills()
    guide = skills.get_skill("computer_use_guide")
    assert guide is not None
    assert guide.tools == ["computer_use"]
    assert guide.name not in {skill.name for skill in skills.get_manual_skills()}
    assert len(guide.instructions.split()) <= 300
    assert sum(
        1 for line in guide.instructions.splitlines() if line.lstrip().startswith("-")
    ) <= 10

    description = ComputerUseTool().description
    routing_line = next(
        line
        for line in _AGENT_GUIDELINES.splitlines()
        if "Interaction preference order" in line
    )
    assert len(description.split()) <= 120
    assert len(_AGENT_GUIDELINES.split()) <= 2_100
    assert len((routing_line + " " + description + " " + guide.instructions).split()) <= 550
    assert description not in guide.instructions
    assert routing_line not in guide.instructions


def test_optional_playwright_mcp_remains_an_external_surface() -> None:
    catalog = json.loads(
        Path("src/row_bot/mcp_client/recommended_servers.json").read_text(encoding="utf-8")
    )
    playwright = next(server for server in catalog if server.get("name") == "Playwright MCP")
    assert playwright["install"]["command"] == "npx"
    assert "@playwright/mcp" in playwright["install"]["args"]
    assert "external" not in BrowserTool().description.casefold()  # first-party direct backend only
    assert "direct Python Playwright" in BrowserTool().description
