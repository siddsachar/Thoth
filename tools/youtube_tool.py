"""YouTube tools — search videos and fetch transcripts."""

from __future__ import annotations

import json
import re

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from tools.base import BaseTool
from tools import registry


# ── Schemas ──────────────────────────────────────────────────────────────────

class _YouTubeTranscriptInput(BaseModel):
    url: str = Field(
        description=(
            "A YouTube video URL (e.g. https://www.youtube.com/watch?v=VIDEO_ID "
            "or https://youtu.be/VIDEO_ID) or just the video ID."
        )
    )
    language: str = Field(
        default="en",
        description="Language code for the transcript (e.g. 'en', 'es', 'fr'). Defaults to English.",
    )


class _YouTubeSearchInput(BaseModel):
    query: str = Field(
        description="Search query to find YouTube videos (e.g. 'python async programming')."
    )
    max_results: int = Field(
        default=5,
        description="Maximum number of video results to return (1-10). Defaults to 5.",
    )


# ── Tool functions ───────────────────────────────────────────────────────────

def _get_transcript(url: str, language: str = "en") -> str:
    """Fetch the transcript of a YouTube video."""
    from langchain_community.document_loaders import YoutubeLoader

    # Extract video ID if a full URL was given
    video_id = url.strip()
    patterns = [
        r"(?:v=|/v/|youtu\.be/)([a-zA-Z0-9_-]{11})",
        r"^([a-zA-Z0-9_-]{11})$",
    ]
    for pat in patterns:
        m = re.search(pat, video_id)
        if m:
            video_id = m.group(1)
            break

    try:
        loader = YoutubeLoader(
            video_id=video_id,
            language=[language, "en"],
            add_video_info=False,
        )
        docs = loader.load()
    except Exception as exc:
        return f"Failed to fetch YouTube transcript: {exc}"

    if not docs:
        return "No transcript available for this video. The video may not have captions."

    text = docs[0].page_content.strip()
    if not text:
        return "The transcript was empty. The video may not have captions."

    MAX_CHARS = 15_000
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n\n… [transcript truncated]"

    source_url = f"https://www.youtube.com/watch?v={video_id}"
    return f"SOURCE_URL: {source_url}\n\n{text}"


def _search_youtube(query: str, max_results: int = 5) -> str:
    """Search YouTube for videos matching a query."""
    from youtube_search import YoutubeSearch

    max_results = max(1, min(max_results, 10))

    try:
        results = YoutubeSearch(query, max_results=max_results).to_dict()
    except Exception as exc:
        return f"YouTube search failed: {exc}"

    if not results:
        return "No YouTube videos found for that query."

    videos = []
    for r in results:
        video_url = "https://www.youtube.com" + r.get("url_suffix", "")
        # Strip tracking params for cleaner URLs
        video_url = re.sub(r"&pp=[^&]*", "", video_url)
        videos.append({
            "title": r.get("title", ""),
            "url": video_url,
            "channel": r.get("channel", ""),
            "duration": r.get("duration", ""),
            "views": r.get("views", ""),
            "publish_time": r.get("publish_time", ""),
        })

    return json.dumps(videos, indent=2)


# ── Tool class ───────────────────────────────────────────────────────────────

class YouTubeTool(BaseTool):

    @property
    def name(self) -> str:
        return "youtube"

    @property
    def display_name(self) -> str:
        return "▶️ YouTube"

    @property
    def description(self) -> str:
        return (
            "Search YouTube for videos and fetch video transcripts. "
            "Use this when the user wants to find videos on a topic or "
            "asks about the content of a specific YouTube video."
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
                func=_search_youtube,
                name="youtube_search",
                description=(
                    "Search YouTube for videos matching a query. Returns a list of "
                    "videos with title, URL, channel, duration, views, and publish time. "
                    "Use this when the user wants to find YouTube videos on a topic. "
                    "To get the content of a specific video, use youtube_transcript."
                ),
                args_schema=_YouTubeSearchInput,
            ),
            StructuredTool.from_function(
                func=_get_transcript,
                name="youtube_transcript",
                description=(
                    "Fetch the transcript (captions/subtitles) of a YouTube video. "
                    "Input a YouTube URL or video ID. Returns the full transcript text. "
                    "Use this when a user shares a YouTube link or asks to summarize "
                    "a specific video. Pair with youtube_search to find then read videos."
                ),
                args_schema=_YouTubeTranscriptInput,
            ),
        ]

    def execute(self, query: str) -> str:
        return _search_youtube(query)


registry.register(YouTubeTool())
