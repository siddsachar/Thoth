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
    assert "call stop immediately and never add another capture" in _AGENT_GUIDELINES
    assert "before the first coordinate-only action" in _AGENT_GUIDELINES
    assert "capture once with visual_question" in _AGENT_GUIDELINES
    assert "list_apps active metadata" in _AGENT_GUIDELINES
    assert "never guess, launch aliases, switch to managed Browser" in _AGENT_GUIDELINES
    assert "at most three reversible mutations" in _AGENT_GUIDELINES
    assert "do not repeatedly focus it" in _AGENT_GUIDELINES
    assert "A single unchanged or unknown visual comparison" in _AGENT_GUIDELINES
    assert "Computer Use is unavailable in background tasks" in AGENT_BG_OVERRIDE


def test_existing_browser_guidance_uses_computer_while_ordinary_navigation_stays_managed() -> None:
    computer_description = ComputerUseTool().description
    browser_description = BrowserTool().description

    assert "'this browser'" in _AGENT_GUIDELINES
    assert "'the browser below'" in _AGENT_GUIDELINES
    assert "normally begin with one computer_use capture using app" in _AGENT_GUIDELINES
    assert "Reserve list_windows for ambiguity" in _AGENT_GUIDELINES
    assert "do not call launch_app merely to focus it" in _AGENT_GUIDELINES
    assert "ordinary website navigation" in _AGENT_GUIDELINES
    assert "separate managed browser" in _AGENT_GUIDELINES
    assert "personal browser profile or CDP endpoint" in _AGENT_GUIDELINES
    assert "already-open native browser windows" in computer_description
    assert "silently switching to the managed Browser" in computer_description
    assert "Navigate websites" in browser_description


def test_optional_playwright_mcp_remains_an_external_surface() -> None:
    catalog = json.loads(
        Path("src/row_bot/mcp_client/recommended_servers.json").read_text(encoding="utf-8")
    )
    playwright = next(server for server in catalog if server.get("name") == "Playwright MCP")
    assert playwright["install"]["command"] == "npx"
    assert "@playwright/mcp" in playwright["install"]["args"]
    assert "external" not in BrowserTool().description.casefold()  # first-party direct backend only
    assert "direct Python Playwright" in BrowserTool().description
