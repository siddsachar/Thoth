"""Wolfram Alpha tool — advanced computation, unit conversion, and knowledge queries.

Uses the Wolfram Full Results API v2 directly (JSON mode) instead of the
``wolframalpha`` Python package, which has a known bug that asserts an exact
Content-Type header (``text/xml;charset=utf-8``) that the API no longer returns.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import requests
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from tools.base import BaseTool
from tools import registry

logger = logging.getLogger(__name__)

_API_URL = "http://api.wolframalpha.com/v2/query"


# ---------------------------------------------------------------------------
# Helper: call Wolfram Full Results API and return a concise text answer
# ---------------------------------------------------------------------------

def _wolfram_query(query: str, app_id: str, timeout: int = 30) -> str:
    """Query Wolfram Alpha Full Results API (JSON) and return a text summary."""
    params: dict[str, Any] = {
        "appid": app_id,
        "input": query,
        "output": "json",
    }
    resp = requests.get(_API_URL, params=params, timeout=timeout)
    resp.raise_for_status()

    data = resp.json().get("queryresult", {})

    if data.get("error"):
        err = data["error"]
        return f"Wolfram Alpha error: {err.get('msg', err)}"

    if not data.get("success"):
        # Provide didyoumeans suggestions if available
        tips = data.get("didyoumeans")
        if tips:
            suggestions = tips if isinstance(tips, list) else [tips]
            names = ", ".join(
                t.get("val", "") if isinstance(t, dict) else str(t)
                for t in suggestions
            )
            return (
                f"Wolfram Alpha could not interpret: {query}\n"
                f"Did you mean: {names}"
            )
        return f"Wolfram Alpha returned no results for: {query}"

    # Collect pod plaintext entries
    lines: list[str] = []
    for pod in data.get("pods", []):
        title = pod.get("title", "")
        for sub in pod.get("subpods", []):
            text = (sub.get("plaintext") or "").strip()
            if text:
                lines.append(f"{title}: {text}")

    return "\n".join(lines) if lines else f"Wolfram Alpha returned no text for: {query}"


# ---------------------------------------------------------------------------
# Tool class
# ---------------------------------------------------------------------------

class WolframAlphaTool(BaseTool):

    @property
    def name(self) -> str:
        return "wolfram_alpha"

    @property
    def display_name(self) -> str:
        return "🔢 Wolfram Alpha"

    @property
    def description(self) -> str:
        return (
            "Query Wolfram Alpha for advanced computation, unit/currency "
            "conversion, symbolic math (equations, derivatives, integrals), "
            "scientific data, nutrition facts, and more. Use this when a "
            "question goes beyond basic arithmetic."
        )

    @property
    def enabled_by_default(self) -> bool:
        return False  # Requires API key

    @property
    def required_api_keys(self) -> dict[str, str]:
        return {"Wolfram Alpha App ID": "WOLFRAM_ALPHA_APPID"}

    def as_langchain_tools(self) -> list:
        app_id = os.environ.get("WOLFRAM_ALPHA_APPID", "")
        if not app_id:
            return []

        class _WolframInput(BaseModel):
            query: str = Field(
                description=(
                    "A natural-language or mathematical query for Wolfram Alpha. "
                    "Examples: 'solve x^2 - 4x + 3 = 0', '150 USD to EUR', "
                    "'calories in 200g chicken breast', 'distance from NYC to London', "
                    "'derivative of sin(x)*e^x', 'molecular weight of caffeine'."
                )
            )

        def _run(query: str) -> str:
            try:
                return _wolfram_query(query, app_id)
            except requests.RequestException as exc:
                logger.warning("Wolfram Alpha request failed: %s", exc)
                return f"Wolfram Alpha error: {exc}"
            except Exception as exc:
                logger.warning("Wolfram Alpha unexpected error: %s", exc)
                return f"Wolfram Alpha error: {exc}"

        return [
            StructuredTool.from_function(
                func=_run,
                name="wolfram_alpha",
                description=(
                    "Query Wolfram Alpha for advanced computation, unit/currency "
                    "conversion, symbolic math (equations, calculus, algebra), "
                    "scientific data, chemistry, physics constants, nutrition, "
                    "date calculations, and more. Accepts natural language queries."
                ),
                args_schema=_WolframInput,
            )
        ]

    def execute(self, query: str) -> str:
        tools = self.as_langchain_tools()
        if not tools:
            return "Wolfram Alpha App ID not configured."
        return tools[0].invoke({"query": query})


registry.register(WolframAlphaTool())
