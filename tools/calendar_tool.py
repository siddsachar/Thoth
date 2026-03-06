"""Google Calendar tool — view, create, and manage calendar events."""

from __future__ import annotations

import json
import os
import pathlib

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field
from typing import Optional

from tools.base import BaseTool
from tools import registry

# Credential / token files live in the Thoth data directory
_DATA_DIR = pathlib.Path(
    os.environ.get("THOTH_DATA_DIR", pathlib.Path.home() / ".thoth")
)
_CALENDAR_DIR = _DATA_DIR / "calendar"
_CALENDAR_DIR.mkdir(parents=True, exist_ok=True)

# Re-use the same Google OAuth credentials.json as Gmail (same Cloud project),
# but keep a separate token file because the scopes differ.
_GMAIL_DIR = _DATA_DIR / "gmail"
DEFAULT_CREDENTIALS_PATH = str(_GMAIL_DIR / "credentials.json")
DEFAULT_TOKEN_PATH = str(_CALENDAR_DIR / "token.json")

# Calendar operations — tiered by risk
# Note: get_calendars_info is excluded because our simplified search_events
# wrapper fetches calendar info internally, and exposing both confuses the LLM.
_READ_OPS = ["get_current_datetime", "search_events"]
_WRITE_OPS = ["create_calendar_event", "update_calendar_event"]
_DESTRUCTIVE_OPS = ["move_calendar_event", "delete_calendar_event"]
ALL_OPERATIONS = _READ_OPS + _WRITE_OPS + _DESTRUCTIVE_OPS
DEFAULT_OPERATIONS = _READ_OPS + _WRITE_OPS  # Safe default — no delete/move

CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar"]


class CalendarTool(BaseTool):

    @property
    def name(self) -> str:
        return "calendar"

    @property
    def display_name(self) -> str:
        return "📅 Google Calendar"

    @property
    def description(self) -> str:
        return (
            "View, search, create, and manage Google Calendar events. "
            "Use this when the user asks about their schedule, upcoming events, "
            "wants to create or modify calendar entries, or check availability."
        )

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

    @property
    def destructive_tool_names(self) -> set[str]:
        return {"move_calendar_event", "delete_calendar_event"}

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
        from langchain_google_community.calendar.utils import get_google_credentials

        get_google_credentials(
            scopes=CALENDAR_SCOPES,
            token_file=self._get_token_path(),
            client_secrets_file=self._get_credentials_path(),
        )

    def _build_api_resource(self):
        from langchain_google_community.calendar.utils import (
            build_calendar_service,
            get_google_credentials,
        )

        credentials = get_google_credentials(
            scopes=CALENDAR_SCOPES,
            token_file=self._get_token_path(),
            client_secrets_file=self._get_credentials_path(),
        )
        return build_calendar_service(credentials=credentials)

    # ── Build toolkit tools ──────────────────────────────────────────────────
    def _get_selected_operations(self) -> list[str]:
        ops = self.get_config("selected_operations", DEFAULT_OPERATIONS)
        return [op for op in ops if op in ALL_OPERATIONS]

    def as_langchain_tools(self) -> list:
        """Return the selected Calendar tools using stored OAuth credentials."""
        if not self.has_credentials_file():
            return []
        if not self.is_authenticated():
            return []

        try:
            api_resource = self._build_api_resource()
        except Exception:
            return []

        from langchain_google_community.calendar.toolkit import CalendarToolkit

        toolkit = CalendarToolkit(api_resource=api_resource)
        all_tools = toolkit.get_tools()

        selected = self._get_selected_operations()
        tools = [t for t in all_tools if t.name in selected]

        # Replace the original search_events with a simplified wrapper
        # that auto-fetches calendars_info internally so the LLM only
        # needs to provide date range and optional query.
        if "search_events" in selected:
            tools = [t for t in tools if t.name != "search_events"]
            tools.append(_make_simple_search_tool(api_resource))

        return tools

    def execute(self, query: str) -> str:
        return "Use the individual Calendar operations instead."


# ── Simplified search_events wrapper ─────────────────────────────────────────
class _SearchEventsInput(BaseModel):
    min_datetime: str = Field(
        description="Start datetime in 'YYYY-MM-DD HH:MM:SS' format."
    )
    max_datetime: str = Field(
        description="End datetime in 'YYYY-MM-DD HH:MM:SS' format."
    )
    max_results: int = Field(
        default=10, description="Maximum number of events to return."
    )
    query: Optional[str] = Field(
        default=None,
        description="Optional free-text search term to filter events.",
    )


def _make_simple_search_tool(api_resource):
    """Build a search_events tool that auto-fetches calendars info."""
    from zoneinfo import ZoneInfo

    def _search_events(
        min_datetime: str,
        max_datetime: str,
        max_results: int = 10,
        query: Optional[str] = None,
    ) -> str:
        # Auto-fetch calendars info
        calendars = api_resource.calendarList().list().execute()
        cal_data = []
        for item in calendars.get("items", []):
            cal_data.append({
                "id": item["id"],
                "summary": item["summary"],
                "timeZone": item["timeZone"],
            })

        from datetime import datetime as dt

        all_events = []
        for cal in cal_data:
            tz = ZoneInfo(cal["timeZone"]) if cal.get("timeZone") else None
            time_min = dt.strptime(min_datetime, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz).isoformat()
            time_max = dt.strptime(max_datetime, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz).isoformat()

            events_result = (
                api_resource.events()
                .list(
                    calendarId=cal["id"],
                    timeMin=time_min,
                    timeMax=time_max,
                    maxResults=max_results,
                    singleEvents=True,
                    orderBy="startTime",
                    q=query,
                )
                .execute()
            )
            for ev in events_result.get("items", []):
                start = ev.get("start", {}).get("dateTime", ev.get("start", {}).get("date", ""))
                end = ev.get("end", {}).get("dateTime", ev.get("end", {}).get("date", ""))
                all_events.append({
                    "event_id": ev.get("id", ""),
                    "calendar_id": cal["id"],
                    "summary": ev.get("summary", "(no title)"),
                    "start": start,
                    "end": end,
                    "location": ev.get("location", ""),
                    "description": ev.get("description", ""),
                    "calendar": cal["summary"],
                })

        if not all_events:
            return "No events found in the specified time range."
        return json.dumps(all_events, indent=2)

    return StructuredTool.from_function(
        func=_search_events,
        name="search_events",
        description=(
            "Search for events in Google Calendar. Provide a date/time range "
            "in 'YYYY-MM-DD HH:MM:SS' format. Optionally filter by a text query. "
            "Calendars are fetched automatically — no need to call get_calendars_info first."
        ),
        args_schema=_SearchEventsInput,
    )


registry.register(CalendarTool())
