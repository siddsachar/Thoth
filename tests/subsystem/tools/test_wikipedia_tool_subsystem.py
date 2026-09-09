"""Deterministic subsystem tests for the Wikipedia tool.

Tests cover:
- Tool identity and registration contracts
- HTTPS API URL enforcement
- Graceful error handling on retriever failures
- Output format with sources
- Empty result handling
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest


pytestmark = pytest.mark.subsystem


# ── Helpers ──────────────────────────────────────────────────────────────────


@dataclass
class _FakeDocument:
    """Minimal stand-in for langchain_core.documents.Document."""

    page_content: str
    metadata: dict


class _FakeRetriever:
    """Returns canned documents for any query."""

    def __init__(self, docs: list[_FakeDocument]) -> None:
        self._docs = docs

    def invoke(self, query: str) -> list[_FakeDocument]:
        return self._docs


class _EmptyRetriever(_FakeRetriever):
    """Always returns no results."""

    def __init__(self) -> None:
        super().__init__([])


class _ErrorRetriever:
    """Raises a configurable exception on every invoke."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def invoke(self, query: str):
        raise self._exc


# ── Identity & registration ─────────────────────────────────────────────────


def test_wikipedia_tool_name_and_display_name(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.wikipedia_tool import WikipediaTool

    tool = WikipediaTool()
    assert tool.name == "wikipedia"
    assert tool.display_name == "🌐 Wikipedia"


def test_wikipedia_tool_enabled_by_default(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.wikipedia_tool import WikipediaTool

    assert WikipediaTool().enabled_by_default is True


def test_wikipedia_tool_requires_no_api_keys(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.wikipedia_tool import WikipediaTool

    assert WikipediaTool().required_api_keys == {}


def test_wikipedia_tool_is_registered(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools import registry

    # Import the module to trigger registration
    import row_bot.tools.wikipedia_tool  # noqa: F401

    tool = registry.get_tool("wikipedia")
    assert tool is not None
    assert tool.name == "wikipedia"


# ── HTTPS enforcement ────────────────────────────────────────────────────────


def test_configure_wikipedia_client_upgrades_http_to_https(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    import wikipedia.wikipedia as wiki_impl
    from row_bot.tools.wikipedia_tool import _configure_wikipedia_client

    original = wiki_impl.API_URL
    try:
        wiki_impl.API_URL = "http://en.wikipedia.org/w/api.php"
        _configure_wikipedia_client()
        assert wiki_impl.API_URL == "https://en.wikipedia.org/w/api.php"
    finally:
        wiki_impl.API_URL = original


def test_configure_wikipedia_client_leaves_https_unchanged(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    import wikipedia.wikipedia as wiki_impl
    from row_bot.tools.wikipedia_tool import _configure_wikipedia_client

    original = wiki_impl.API_URL
    try:
        wiki_impl.API_URL = "https://en.wikipedia.org/w/api.php"
        _configure_wikipedia_client()
        assert wiki_impl.API_URL == "https://en.wikipedia.org/w/api.php"
    finally:
        wiki_impl.API_URL = original


# ── Error handling ───────────────────────────────────────────────────────────


def test_execute_returns_recovery_message_on_json_error(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.wikipedia_tool import WikipediaTool

    tool = WikipediaTool()
    monkeypatch.setattr(
        tool,
        "get_retriever",
        lambda **kwargs: _ErrorRetriever(
            json.JSONDecodeError("Expecting value", "", 0)
        ),
    )

    result = tool.execute("nonexistent topic")

    assert "temporarily unavailable" in result
    assert "Do not retry the Wikipedia tool" in result
    assert "answer from general knowledge" in result


def test_execute_returns_recovery_message_on_connection_error(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.wikipedia_tool import WikipediaTool

    tool = WikipediaTool()
    monkeypatch.setattr(
        tool,
        "get_retriever",
        lambda **kwargs: _ErrorRetriever(ConnectionError("network unreachable")),
    )

    result = tool.execute("test query")

    assert "temporarily unavailable" in result
    assert "ConnectionError" in result


def test_execute_returns_recovery_on_generic_exception(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.wikipedia_tool import WikipediaTool

    tool = WikipediaTool()
    monkeypatch.setattr(
        tool,
        "get_retriever",
        lambda **kwargs: _ErrorRetriever(RuntimeError("unexpected")),
    )

    result = tool.execute("test query")

    assert "temporarily unavailable" in result
    assert "RuntimeError" in result


# ── Successful retrieval ─────────────────────────────────────────────────────


def test_execute_formats_results_with_sources(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.wikipedia_tool import WikipediaTool

    tool = WikipediaTool()
    docs = [
        _FakeDocument(
            page_content="Python is a programming language.",
            metadata={"source": "https://en.wikipedia.org/wiki/Python"},
        ),
        _FakeDocument(
            page_content="Guido van Rossum created Python.",
            metadata={"source": "https://en.wikipedia.org/wiki/Guido_van_Rossum"},
        ),
    ]
    monkeypatch.setattr(
        tool, "get_retriever", lambda **kwargs: _FakeRetriever(docs)
    )

    result = tool.execute("Python programming")

    assert "[Result 1]" in result
    assert "[Result 2]" in result
    assert "Python is a programming language." in result
    assert "Guido van Rossum created Python." in result
    assert "SOURCE_URL: https://en.wikipedia.org/wiki/Python" in result
    assert "SOURCE_URL: https://en.wikipedia.org/wiki/Guido_van_Rossum" in result


def test_execute_returns_no_results_message_on_empty(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.wikipedia_tool import WikipediaTool

    tool = WikipediaTool()
    monkeypatch.setattr(
        tool, "get_retriever", lambda **kwargs: _EmptyRetriever()
    )

    result = tool.execute("xyznonexistenttopic12345")

    assert "No results found" in result


# ── Description contract ─────────────────────────────────────────────────────


def test_description_guides_agent_on_broad_vs_specific_queries(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.wikipedia_tool import WikipediaTool

    desc = WikipediaTool().description.lower()

    assert "encyclopedia" in desc or "encyclopedic" in desc
    assert "broad" in desc
    assert "sources" in desc or "source" in desc


# ── LangChain tool wrapper ──────────────────────────────────────────────────


def test_as_langchain_tool_returns_structured_tool(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.wikipedia_tool import WikipediaTool

    tool = WikipediaTool()
    lc_tools = tool.as_langchain_tools()

    assert len(lc_tools) == 1
    assert lc_tools[0].name == "wikipedia"
    assert len(lc_tools[0].description) > 0
