"""Thoth tools package — import this module to discover all tools.

Each tool module auto-registers itself with the registry on import.
To add a new tool, create a new ``*_tool.py`` module in this package
that subclasses ``BaseTool`` and calls ``registry.register()``.
"""

from tools import registry  # noqa: F401 — make registry accessible as tools.registry

# Import every tool module so they self-register.
# When you add a new tool, add its import here.
from tools import documents_tool   # noqa: F401
from tools import wikipedia_tool   # noqa: F401
from tools import arxiv_tool       # noqa: F401
from tools import web_search_tool  # noqa: F401
from tools import duckduckgo_tool  # noqa: F401
from tools import filesystem_tool  # noqa: F401
from tools import gmail_tool       # noqa: F401
from tools import calendar_tool   # noqa: F401
from tools import url_reader_tool # noqa: F401
from tools import youtube_tool    # noqa: F401
from tools import timer_tool      # noqa: F401
from tools import calculator_tool  # noqa: F401
from tools import wolfram_tool     # noqa: F401
from tools import weather_tool     # noqa: F401
from tools import vision_tool      # noqa: F401
from tools import memory_tool      # noqa: F401
from tools import conversation_search_tool  # noqa: F401
from tools import system_info_tool  # noqa: F401
from tools import chart_tool        # noqa: F401
from tools import tracker_tool      # noqa: F401
from tools import shell_tool        # noqa: F401
try:                                # browser_tool is still in development
    from tools import browser_tool  # noqa: F401
except ImportError:
    pass
