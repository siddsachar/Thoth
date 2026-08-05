"""Deterministic subsystem tests for the ArXiv tool.

Tests cover:
- Tool identity and registration contracts
- Result formatting (title, authors, date, abstract, links)
- Empty result handling
- Error propagation from the arxiv client
- Author truncation (>5 authors)
- Version suffix stripping from HTML URLs
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest


pytestmark = pytest.mark.subsystem


# ── Fake arxiv objects ───────────────────────────────────────────────────────


@dataclass
class _FakeAuthor:
    """Minimal stand-in for arxiv.Result.Author."""

    name: str


@dataclass
class _FakeResult:
    """Minimal stand-in for arxiv.Result."""

    title: str
    summary: str
    published: datetime
    primary_category: str
    pdf_url: str
    entry_id: str
    authors: list[_FakeAuthor] = field(default_factory=list)
    _short_id: str = ""

    def get_short_id(self) -> str:
        return self._short_id


class _FakeClient:
    """Mimics arxiv.Client.results() returning pre-built results."""

    def __init__(self, results: list[_FakeResult]) -> None:
        self._results = results

    def results(self, search) -> list[_FakeResult]:
        return self._results


class _ErrorClient:
    """Raises an exception when results() is called."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def results(self, search):
        raise self._exc


# ── Identity & registration ─────────────────────────────────────────────────


def test_arxiv_tool_name_and_display_name(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.arxiv_tool import ArxivTool

    tool = ArxivTool()
    assert tool.name == "arxiv"
    assert tool.display_name == "📚 Arxiv"


def test_arxiv_tool_enabled_by_default(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.arxiv_tool import ArxivTool

    assert ArxivTool().enabled_by_default is True


def test_arxiv_tool_requires_no_api_keys(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.arxiv_tool import ArxivTool

    assert ArxivTool().required_api_keys == {}


def test_arxiv_tool_is_registered(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools import registry

    import row_bot.tools.arxiv_tool  # noqa: F401

    tool = registry.get_tool("arxiv")
    assert tool is not None
    assert tool.name == "arxiv"


# ── Successful retrieval ─────────────────────────────────────────────────────


def _make_fake_result(
    title: str = "Attention Is All You Need",
    summary: str = "We propose a new architecture called the Transformer.",
    authors: list[str] | None = None,
    published: datetime | None = None,
    short_id: str = "1706.03762v7",
    category: str = "cs.CL",
) -> _FakeResult:
    if authors is None:
        authors = ["Ashish Vaswani", "Noam Shazeer", "Niki Parmar"]
    if published is None:
        published = datetime(2017, 6, 12)
    return _FakeResult(
        title=title,
        summary=summary,
        published=published,
        primary_category=category,
        pdf_url=f"https://arxiv.org/pdf/{short_id}",
        entry_id=f"http://arxiv.org/abs/{short_id}",
        authors=[_FakeAuthor(name=a) for a in authors],
        _short_id=short_id,
    )


def test_execute_formats_single_result_correctly(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.arxiv_tool import ArxivTool

    fake_result = _make_fake_result()
    tool = ArxivTool()

    with patch("arxiv.Client", return_value=_FakeClient([fake_result])), \
         patch("arxiv.Search"):
        result = tool.execute("transformer architecture")

    assert "[1] Attention Is All You Need" in result
    assert "Authors: Ashish Vaswani, Noam Shazeer, Niki Parmar" in result
    assert "Published: 2017-06-12" in result
    assert "Categories: cs.CL" in result
    assert "We propose a new architecture" in result
    assert "https://arxiv.org/html/1706.03762" in result
    assert "SOURCE_URL:" in result
    assert "Tip: To read a paper's full text" in result


def test_execute_formats_multiple_results(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.arxiv_tool import ArxivTool

    results = [
        _make_fake_result(title="Paper A", short_id="2301.00001v1"),
        _make_fake_result(title="Paper B", short_id="2301.00002v2"),
        _make_fake_result(title="Paper C", short_id="2301.00003v1"),
    ]
    tool = ArxivTool()

    with patch("arxiv.Client", return_value=_FakeClient(results)), \
         patch("arxiv.Search"):
        result = tool.execute("deep learning")

    assert "[1] Paper A" in result
    assert "[2] Paper B" in result
    assert "[3] Paper C" in result
    # Results separated by ---
    assert "---" in result


# ── HTML URL version stripping ───────────────────────────────────────────────


def test_version_suffix_stripped_from_html_url(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.arxiv_tool import ArxivTool

    fake = _make_fake_result(short_id="2401.12345v3")
    tool = ArxivTool()

    with patch("arxiv.Client", return_value=_FakeClient([fake])), \
         patch("arxiv.Search"):
        result = tool.execute("test")

    # The HTML URL should NOT have the version suffix
    assert "https://arxiv.org/html/2401.12345" in result
    # It should NOT contain v3 in the HTML URL
    assert "https://arxiv.org/html/2401.12345v3" not in result


def test_no_version_suffix_handled_gracefully(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.arxiv_tool import ArxivTool

    fake = _make_fake_result(short_id="2401.12345")
    tool = ArxivTool()

    with patch("arxiv.Client", return_value=_FakeClient([fake])), \
         patch("arxiv.Search"):
        result = tool.execute("test")

    assert "https://arxiv.org/html/2401.12345" in result


# ── Author truncation ────────────────────────────────────────────────────────


def test_more_than_five_authors_shows_et_al(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.arxiv_tool import ArxivTool

    many_authors = [f"Author {i}" for i in range(1, 9)]  # 8 authors
    fake = _make_fake_result(authors=many_authors)
    tool = ArxivTool()

    with patch("arxiv.Client", return_value=_FakeClient([fake])), \
         patch("arxiv.Search"):
        result = tool.execute("test")

    # First 5 authors listed
    assert "Author 1" in result
    assert "Author 5" in result
    # 6th author not listed directly
    assert "Author 6" not in result.split("et al.")[0]
    # et al. with count
    assert "et al." in result
    assert "8 authors" in result


def test_five_or_fewer_authors_no_truncation(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.arxiv_tool import ArxivTool

    three_authors = ["Alice", "Bob", "Charlie"]
    fake = _make_fake_result(authors=three_authors)
    tool = ArxivTool()

    with patch("arxiv.Client", return_value=_FakeClient([fake])), \
         patch("arxiv.Search"):
        result = tool.execute("test")

    assert "Alice, Bob, Charlie" in result
    assert "et al." not in result


# ── Empty results & errors ───────────────────────────────────────────────────


def test_execute_returns_message_on_empty_results(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.arxiv_tool import ArxivTool

    tool = ArxivTool()

    with patch("arxiv.Client", return_value=_FakeClient([])), \
         patch("arxiv.Search"):
        result = tool.execute("xyznonexistenttopic12345")

    assert "No arXiv papers found" in result


def test_execute_returns_error_on_client_failure(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.arxiv_tool import ArxivTool

    tool = ArxivTool()

    with patch("arxiv.Client", return_value=_ErrorClient(ConnectionError("timeout"))), \
         patch("arxiv.Search"):
        result = tool.execute("test query")

    assert "arXiv search error" in result
    assert "timeout" in result


# ── Description contract ─────────────────────────────────────────────────────


def test_description_mentions_query_syntax(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.arxiv_tool import ArxivTool

    desc = ArxivTool().description
    assert "ti:" in desc
    assert "au:" in desc
    assert "cat:" in desc


# ── LangChain tool wrapper ──────────────────────────────────────────────────


def test_as_langchain_tool_returns_structured_tool(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / ".row-bot"))
    from row_bot.tools.arxiv_tool import ArxivTool

    tool = ArxivTool()
    lc_tools = tool.as_langchain_tools()

    assert len(lc_tools) == 1
    assert lc_tools[0].name == "arxiv"
    assert len(lc_tools[0].description) > 0
