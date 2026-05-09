"""Thoth UI — constants, patterns and extension sets.

Pure data — no side-effects on import.
"""

from __future__ import annotations

import re

# ═════════════════════════════════════════════════════════════════════════════
# WELCOME / EXAMPLES
# ═════════════════════════════════════════════════════════════════════════════

_WELCOME_BODY = """\

---

🤖 **Agent workspace** — Chat, reason, browse, read files, use tools, and work across your local app state.

🧠 **Knowledge** — Build memory from conversations, upload documents, search your knowledge graph, and choose local or cloud embeddings.

⚡ **Workflows** — Run manual or scheduled background agents. Starter workflows are disabled until you review and enable them.

🎨 **Designer Studio** — Create decks, documents, landing pages, app mockups, and storyboards from briefs or templates.

🌐 **Browser & tools** — Use visible browser automation, search, files, shell, Gmail, Calendar, MCP servers, plugins, and skills when configured.

🎤 **Voice & vision** — Talk hands-free, hear spoken replies, and ask questions about your camera or screen.

📬 **Channels** — Connect Telegram, WhatsApp, Discord, Slack, or SMS. Workflow run status always remains available in the web app.

---

⚙️ Use **Settings** or the sidebar hello button to finish setup anytime. Just type what you want done — I'll pick the useful tools.
"""


def welcome_message(cloud: bool = False) -> str:
    if cloud:
        header = (
            "👋 **Welcome to Thoth — your personal AI workspace.**\n\n"
            "Your selected model runs in the cloud, while Thoth stores your "
            "conversations, memory, documents, workflows, and settings locally."
        )
    else:
        header = (
            "👋 **Welcome to Thoth — your private AI workspace.**\n\n"
            "Your selected model runs locally. Conversations, memory, documents, "
            "workflows, and settings stay on this machine unless you connect external services."
        )
    return header + _WELCOME_BODY


EXAMPLE_PROMPTS = [
    "Summarize my latest documents and suggest next actions",
    "Create a disabled workflow for a weekly research briefing",
    "Draft a landing page in Designer Studio for a new product",
    "What do you remember about my current projects?",
    "Research the latest AI agent trends and cite sources",
    "Check my upcoming calendar and prepare a daily plan",
]

# ═════════════════════════════════════════════════════════════════════════════
# FILE / UPLOAD EXTENSIONS
# ═════════════════════════════════════════════════════════════════════════════

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
DATA_EXTENSIONS = {".csv", ".tsv", ".xlsx", ".xls", ".json", ".jsonl"}
TEXT_EXTENSIONS = {
    ".txt", ".md", ".py", ".js", ".ts", ".html", ".css", ".xml", ".yaml",
    ".yml", ".toml", ".ini", ".cfg", ".log", ".sh", ".bat", ".ps1", ".sql",
    ".r", ".java", ".c", ".cpp", ".h", ".cs", ".go", ".rs", ".rb", ".php",
    ".swift", ".kt", ".lua", ".pl",
}
CHARS_PER_TOKEN_APPROX = 3  # used only for file-size char budgets

ALLOWED_UPLOAD_SUFFIXES = sorted(
    ext.lstrip(".") for ext in IMAGE_EXTENSIONS | TEXT_EXTENSIONS | DATA_EXTENSIONS | {".pdf"}
)

# ═════════════════════════════════════════════════════════════════════════════
# REGEX PATTERNS
# ═════════════════════════════════════════════════════════════════════════════

YT_URL_PATTERN = re.compile(
    r"https?://(?:www\.)?(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/)([a-zA-Z0-9_-]{11})(?:[^\s)\]]*)"
)

SENTENCE_SPLIT = re.compile(r'(?<=[.!?])\s+')

# ═════════════════════════════════════════════════════════════════════════════
# UI CONSTANTS
# ═════════════════════════════════════════════════════════════════════════════

SIDEBAR_MAX_THREADS = 10
MAX_STREAM_SENTENCES = 3

ICON_OPTIONS = [
    "⚡", "📊", "📧", "📝", "🔍", "🗂️", "📰", "🧹", "💡", "🔔",
    "📅", "🌐", "🤖", "📋", "🛠️", "🎯", "📈", "🔄", "💬", "🧪",
]
