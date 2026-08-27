"""Managed Browser backend.

This package owns Row-Bot's direct Python Playwright integration.  It does not
own, attach to, or share sessions with native Computer Use or external MCP
browser servers.
"""

from row_bot.browser.service import BrowserSession, BrowserSessionManager, ManagedBrowserService

__all__ = ["BrowserSession", "BrowserSessionManager", "ManagedBrowserService"]
