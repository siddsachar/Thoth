"""Gmail tool — search, read, and draft emails via the Gmail API."""

from __future__ import annotations

import logging
import os
import pathlib

from tools.base import BaseTool
from tools import registry

logger = logging.getLogger(__name__)

# Credential / token files live in the Thoth data directory
_DATA_DIR = pathlib.Path(
    os.environ.get("THOTH_DATA_DIR", pathlib.Path.home() / ".thoth")
)
_GMAIL_DIR = _DATA_DIR / "gmail"
_GMAIL_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_CREDENTIALS_PATH = str(_GMAIL_DIR / "credentials.json")
DEFAULT_TOKEN_PATH = str(_GMAIL_DIR / "token.json")

# Gmail operations — grouped by risk level
_READ_OPS = ["search_gmail", "get_gmail_message", "get_gmail_thread"]
_COMPOSE_OPS = ["create_gmail_draft"]
_SEND_OPS = ["send_gmail_message"]
ALL_OPERATIONS = _READ_OPS + _COMPOSE_OPS + _SEND_OPS
DEFAULT_OPERATIONS = _READ_OPS + _COMPOSE_OPS  # send disabled by default

# Full access scope
GMAIL_SCOPES = ["https://mail.google.com/"]


class GmailTool(BaseTool):

    @property
    def name(self) -> str:
        return "gmail"

    @property
    def display_name(self) -> str:
        return "📧 Gmail"

    @property
    def description(self) -> str:
        return (
            "Search, read, draft, and send emails via Gmail. "
            "Use this when the user asks about emails, wants to search "
            "their inbox, read messages, draft, or send emails."
        )

    @property
    def destructive_tool_names(self) -> set[str]:
        return {"send_gmail_message"}

    @property
    def enabled_by_default(self) -> bool:
        return False  # Must set up OAuth credentials first

    @property
    def required_api_keys(self) -> dict[str, str]:
        return {}

    @property
    def config_schema(self) -> dict[str, dict]:
        return {
            "credentials_path": {
                "label": "credentials.json path",
                "type": "text",
                "default": DEFAULT_CREDENTIALS_PATH,
            },
            "selected_operations": {
                "label": "Allowed operations",
                "type": "multicheck",
                "default": DEFAULT_OPERATIONS,
                "options": ALL_OPERATIONS,
            },
        }

    # ── Auth helpers ─────────────────────────────────────────────────────────
    def _get_credentials_path(self) -> str:
        return self.get_config("credentials_path", DEFAULT_CREDENTIALS_PATH)

    def _get_token_path(self) -> str:
        return DEFAULT_TOKEN_PATH

    def has_credentials_file(self) -> bool:
        return os.path.isfile(self._get_credentials_path())

    def is_authenticated(self) -> bool:
        return os.path.isfile(self._get_token_path())

    def authenticate(self):
        """Run the OAuth consent flow (opens browser).  Must be called
        when ``credentials.json`` exists but ``token.json`` does not."""
        from langchain_google_community.gmail.utils import get_gmail_credentials

        get_gmail_credentials(
            token_file=self._get_token_path(),
            scopes=GMAIL_SCOPES,
            client_sercret_file=self._get_credentials_path(),
        )

    def _build_api_resource(self):
        from langchain_google_community.gmail.utils import (
            build_resource_service,
            get_gmail_credentials,
        )

        credentials = get_gmail_credentials(
            token_file=self._get_token_path(),
            scopes=GMAIL_SCOPES,
            client_sercret_file=self._get_credentials_path(),
        )
        return build_resource_service(credentials=credentials)

    # ── Build toolkit tools ──────────────────────────────────────────────────
    def _get_selected_operations(self) -> list[str]:
        ops = self.get_config("selected_operations", DEFAULT_OPERATIONS)
        return [op for op in ops if op in ALL_OPERATIONS]

    def as_langchain_tools(self) -> list:
        """Return the selected Gmail tools using stored OAuth credentials."""
        if not self.has_credentials_file():
            return []
        if not self.is_authenticated():
            return []

        try:
            api_resource = self._build_api_resource()
        except Exception:
            logger.warning("Gmail API resource build failed (OAuth issue?)", exc_info=True)
            return []

        from langchain_google_community import GmailToolkit

        toolkit = GmailToolkit(api_resource=api_resource)
        all_tools = toolkit.get_tools()

        selected = self._get_selected_operations()
        # Filter to selected operations only
        tools = [t for t in all_tools if t.name in selected]

        # Wrap each tool so empty results return an explicit message
        # instead of an empty string (which causes the LLM to hallucinate).
        return [_wrap_gmail_tool_empty_guard(t) for t in tools]

    def execute(self, query: str) -> str:
        return "Use the individual Gmail operations instead."


# ── Empty-result guard ───────────────────────────────────────────────────

_EMPTY_MESSAGES: dict[str, str] = {
    "search_gmail": (
        "No emails were found matching that query. "
        "The inbox search returned zero results."
    ),
    "get_gmail_message": "No message content was returned.",
    "get_gmail_thread": "No thread content was returned.",
}
_DEFAULT_EMPTY_MSG = "The Gmail tool returned no results."


def _wrap_gmail_tool_empty_guard(tool):
    """Wrap a LangChain Gmail tool so that empty / blank results are
    replaced with an explicit 'no results' message.  This prevents the
    LLM from hallucinating fake emails when the API returns nothing."""
    from langchain_core.tools import StructuredTool

    original_func = tool.func if hasattr(tool, "func") else None
    if original_func is None:
        return tool

    empty_msg = _EMPTY_MESSAGES.get(tool.name, _DEFAULT_EMPTY_MSG)

    def _guarded(*args, **kwargs):
        result = original_func(*args, **kwargs)
        # Treat None, empty string, empty list, or whitespace-only as empty
        if not result or (isinstance(result, str) and not result.strip()):
            return empty_msg
        # Also catch list-like results that stringified to '[]'
        if isinstance(result, str) and result.strip() in ("[]", "[]\n", ""):
            return empty_msg
        return result

    return StructuredTool.from_function(
        func=_guarded,
        name=tool.name,
        description=tool.description,
    )


registry.register(GmailTool())
