"""Gmail tool — search, read, and draft emails via the Gmail API."""

from __future__ import annotations

import os
import pathlib

from tools.base import BaseTool
from tools import registry

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
            return []

        from langchain_google_community import GmailToolkit

        toolkit = GmailToolkit(api_resource=api_resource)
        all_tools = toolkit.get_tools()

        selected = self._get_selected_operations()
        # Filter to selected operations only
        return [t for t in all_tools if t.name in selected]

    def execute(self, query: str) -> str:
        return "Use the individual Gmail operations instead."


registry.register(GmailTool())
