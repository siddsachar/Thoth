"""URL / Webpage Reader tool — fetch and extract text from any URL."""

from __future__ import annotations

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from tools.base import BaseTool
from tools import registry


class _ReadURLInput(BaseModel):
    url: str = Field(description="The full URL to read (must start with http:// or https://)")


_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"


def _read_url(url: str) -> str:
    """Fetch a webpage and return its text content."""
    import os
    import re
    from langchain_community.document_loaders import WebBaseLoader

    # Ensure USER_AGENT is set so WebBaseLoader doesn't warn
    os.environ.setdefault("USER_AGENT", _USER_AGENT)

    try:
        loader = WebBaseLoader(
            web_paths=[url],
            requests_kwargs={"timeout": 15},
        )
        docs = loader.load()
    except Exception as exc:
        return f"Failed to fetch URL: {exc}"

    if not docs:
        return "No content could be extracted from the URL."

    text = "\n\n".join(doc.page_content for doc in docs)

    # Collapse excessive whitespace / blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = text.strip()

    if not text:
        return "The page was fetched but no readable text content was found."

    # Truncate very long pages to avoid overwhelming the LLM context
    MAX_CHARS = 12_000
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n\n… [content truncated]"

    source = docs[0].metadata.get("source", url)
    return f"SOURCE_URL: {source}\n\n{text}"


class URLReaderTool(BaseTool):

    @property
    def name(self) -> str:
        return "url_reader"

    @property
    def display_name(self) -> str:
        return "🌐 URL Reader"

    @property
    def description(self) -> str:
        return (
            "Fetch and read the text content of any webpage or URL. "
            "Use this when the user provides a link or asks about the "
            "content of a specific webpage."
        )

    @property
    def enabled_by_default(self) -> bool:
        return True

    @property
    def required_api_keys(self) -> dict[str, str]:
        return {}

    def as_langchain_tools(self) -> list:
        return [
            StructuredTool.from_function(
                func=_read_url,
                name="read_url",
                description=(
                    "Fetch a webpage and extract its text content. "
                    "Input must be a full URL starting with http:// or https://. "
                    "Returns the page's readable text. Use this whenever a user "
                    "shares a link or asks about a specific webpage."
                ),
                args_schema=_ReadURLInput,
            )
        ]

    def execute(self, query: str) -> str:
        return _read_url(query)


registry.register(URLReaderTool())
