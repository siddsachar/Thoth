"""Thoth — NiceGUI Frontend
========================

Alternative UI powered by NiceGUI (FastAPI + Vue.js / Quasar).

Run:   python app_nicegui.py   →   http://localhost:8080

The Streamlit frontend (app.py) remains fully functional.
Both frontends share the same backend modules and data directory.
"""

from __future__ import annotations

import asyncio
import base64 as _b64
import io
import json
import logging
import os
import pathlib
import queue
import re
import subprocess
import sys
import threading
import uuid
from datetime import datetime
from typing import Optional

# ── Configure root logger so all module loggers emit to stderr ──────────────
# stderr is captured by launcher.py and written to ~/.thoth/thoth_app.log
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stderr,
)
# Silence extremely noisy third-party loggers
for _noisy in ("httpx", "httpcore", "urllib3", "asyncio", "multipart",
               "watchfiles", "nicegui", "uvicorn.error", "uvicorn.access",
               "sentence_transformers", "transformers", "huggingface_hub",
               "googleapiclient", "googleapiclient.discovery_cache",
               "primp", "ddgs", "ddgs.ddgs"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

# Suppress noisy OpenCV DSHOW warnings (C-level stderr output)
os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
# Prevent "USER_AGENT environment variable not set" warning from langchain
os.environ.setdefault("USER_AGENT", "Thoth/3.0")

logger = logging.getLogger(__name__)

# Ensure our own directory is on sys.path (needed for embedded Python where
# the ._pth file may not include the app directory).
_app_dir = os.path.dirname(os.path.abspath(__file__))
if _app_dir not in sys.path:
    sys.path.insert(0, _app_dir)

from nicegui import ui, app, run, events

# ── Messaging channels ──────────────────────────────────────────────────────
from channels.telegram import start_bot as _tg_start_bot, stop_bot as _tg_stop_bot
from channels.email import (
    start_polling as _email_start,
    stop_polling as _email_stop,
    get_poll_interval as _email_poll_interval,
    set_poll_interval as _email_set_poll_interval,
)
from channels import config as _ch_config

# ── Backend imports (shared with Streamlit frontend) ─────────────────────────
from threads import _list_threads, _save_thread_meta, _delete_thread
from agent import (
    get_agent_graph, stream_agent, resume_stream_agent,
    clear_agent_cache, repair_orphaned_tool_calls, get_token_usage,
    clear_summary_cache,
)
from documents import (
    load_processed_files, load_and_vectorize_document,
    reset_vector_store, DocumentLoader,
)
from models import (
    get_current_model, set_model, list_all_models, list_local_models,
    is_model_local, is_tool_compatible, check_tool_support, pull_model,
    get_context_size, get_user_context_size, set_context_size, get_model_max_context,
    DEFAULT_MODEL, DEFAULT_CONTEXT_SIZE, CONTEXT_SIZE_OPTIONS, CONTEXT_SIZE_LABELS,
)
from api_keys import get_key, set_key, apply_keys
from tools import registry as tool_registry
from voice import get_voice_service, get_available_whisper_sizes
from tts import TTSService, VOICE_CATALOG
from vision import VisionService, POPULAR_VISION_MODELS, list_cameras
from tools.vision_tool import set_vision_service
from memory_extraction import run_extraction, start_periodic_extraction, set_active_thread
from workflows import (
    list_workflows, create_workflow, update_workflow,
    delete_workflow, duplicate_workflow,
    run_workflow_background, get_running_workflows, get_run_history,
    seed_default_workflows, start_workflow_scheduler,
)
from notifications import drain_toasts
import memory as memory_db

# ═════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═════════════════════════════════════════════════════════════════════════════

_WELCOME_MESSAGE = """\
👋 **Welcome to Thoth — your private AI assistant.**

Everything runs locally on your machine. Your conversations, memories, and files never leave your computer.

---

🤖 **Agent** — I autonomously pick from 19 tools to answer your questions — search the web, read files, send emails, check your calendar, and more.

🧠 **Memory** — I remember things you tell me across conversations and learn from past chats automatically.

🎤 **Voice** — Toggle the mic to talk hands-free. I can speak back too — all processed locally, never sent to the cloud.

👁️ **Vision** — I can see your webcam or screen and answer questions about what's there.

⚡ **Workflows** — Build multi-step automations that run on a schedule — daily briefings, email digests, research summaries.

📬 **Channels** — Connect Telegram or Email so I can respond to messages even when the app window is closed.

---

⚙️ Head to **Settings** to configure tools and explore options. Just type or speak — I'll figure out which tools to use.
"""

_EXAMPLE_PROMPTS = [
    "What's the weather in New York?",
    "Summarize the latest AI research papers",
    "What do you remember about me?",
    "Read and summarize report.pdf in my workspace",
    "Send an email to Mom saying I'll be home at 5",
    "What am I looking at? (with camera)",
]

_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
_DATA_EXTENSIONS = {".csv", ".tsv", ".xlsx", ".xls", ".json", ".jsonl"}
_TEXT_EXTENSIONS = {
    ".txt", ".md", ".py", ".js", ".ts", ".html", ".css", ".xml", ".yaml",
    ".yml", ".toml", ".ini", ".cfg", ".log", ".sh", ".bat", ".ps1", ".sql",
    ".r", ".java", ".c", ".cpp", ".h", ".cs", ".go", ".rs", ".rb", ".php",
    ".swift", ".kt", ".lua", ".pl",
}
_CHARS_PER_TOKEN = 4  # conservative approximation

def _file_budget() -> int:
    """Dynamic char budget for attached files: 35 % of the model's context window.

    For 32K context →  ~28K chars (7K tokens)
    For 128K context → ~114K chars (28K tokens)
    Falls back to 40K chars if context size is unavailable.
    """
    try:
        ctx = get_context_size()
    except Exception:
        ctx = 32_768
    return int(ctx * 0.35 * _CHARS_PER_TOKEN)

_YT_URL_PATTERN = re.compile(
    r"https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/)([a-zA-Z0-9_-]{11})(?:[^\s)\]]*)"
)

_SIDEBAR_MAX_THREADS = 8
_MAX_STREAM_SENTENCES = 3
_SENTENCE_SPLIT = re.compile(r'(?<=[.!?])\s+')

_ICON_OPTIONS = [
    "⚡", "📊", "📧", "📝", "🔍", "🗂️", "📰", "🧹", "💡", "🔔",
    "📅", "🌐", "🤖", "📋", "🛠️", "🎯", "📈", "🔄", "💬", "🧪",
]

_ALLOWED_UPLOAD_SUFFIXES = sorted(
    ext.lstrip(".") for ext in _IMAGE_EXTENSIONS | _TEXT_EXTENSIONS | _DATA_EXTENSIONS | {".pdf"}
)


# ═════════════════════════════════════════════════════════════════════════════
# APP CONFIG PERSISTENCE
# ═════════════════════════════════════════════════════════════════════════════

_APP_CONFIG_DIR = pathlib.Path(
    os.environ.get("THOTH_DATA_DIR", pathlib.Path.home() / ".thoth")
)
_APP_CONFIG_PATH = _APP_CONFIG_DIR / "app_config.json"


def _load_app_config() -> dict:
    if _APP_CONFIG_PATH.exists():
        try:
            return json.loads(_APP_CONFIG_PATH.read_text())
        except Exception:
            logger.warning("Failed to load app config from %s", _APP_CONFIG_PATH, exc_info=True)
            return {}
    return {}


def _save_app_config(cfg: dict) -> None:
    _APP_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _APP_CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def _is_first_run() -> bool:
    return not _load_app_config().get("onboarding_seen", False)


def _mark_onboarding_seen() -> None:
    cfg = _load_app_config()
    cfg["onboarding_seen"] = True
    _save_app_config(cfg)


def _is_setup_complete() -> bool:
    """Check whether the first-launch setup wizard has been completed."""
    return _load_app_config().get("setup_complete", False)


def _mark_setup_complete() -> None:
    cfg = _load_app_config()
    cfg["setup_complete"] = True
    _save_app_config(cfg)


# ═════════════════════════════════════════════════════════════════════════════
# SHARED APPLICATION STATE (module-level singleton)
# ═════════════════════════════════════════════════════════════════════════════

class AppState:
    """Shared backend state — lives for the lifetime of the server process."""

    def __init__(self) -> None:
        self.thread_id: str | None = None
        self.thread_name: str | None = None
        self.messages: list[dict] = []
        self.current_model: str = get_current_model()
        self.context_size: int = get_user_context_size()
        self.is_generating: bool = False
        self.stop_event: threading.Event = threading.Event()
        self.pending_interrupt: dict | None = None
        self.show_onboarding: bool = _is_first_run()
        self.voice_enabled: bool = False
        self.voice_service = get_voice_service()
        self.tts_service = TTSService()
        self.vision_service = VisionService()
        self.tts_service.voice_service = self.voice_service
        set_vision_service(self.vision_service)
        self.attached_data_cache: dict[str, bytes] = {}


state = AppState()

# ── Startup gate ─────────────────────────────────────────────────────────
_startup_ready = False
_startup_status = "Starting…"
_startup_warnings: list[str] = []  # toast messages queued during startup


# ═════════════════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ═════════════════════════════════════════════════════════════════════════════

def _strip_file_context(content: str) -> str:
    """Replace verbose file-context blocks with compact badges for display.

    The agent_input stores ``[Attached PDF: report.pdf, 30 pages]\n<full text>``
    followed by ``\n\n<user text>``.  For display we collapse each block to
    just the header line (e.g. ``📎 report.pdf``).
    """
    if "[Attached " not in content:
        return content
    parts = content.split("\n\n")
    badges: list[str] = []
    user_parts: list[str] = []
    for part in parts:
        if part.startswith("[Attached "):
            header = part.split("\n", 1)[0]
            after_colon = header.split(": ", 1)[1] if ": " in header else header
            fname = after_colon.split(",")[0].split("]")[0].strip()
            badges.append(f"📎 {fname}")
        elif part.startswith(("[Trimmed ", "[Truncated ", "--- Page ")):
            continue  # leftover file-context artifacts
        elif part.lstrip().startswith(("[Trimmed ", "[Truncated ")):
            continue
        else:
            user_parts.append(part)
    result_parts: list[str] = []
    if badges:
        result_parts.append(", ".join(badges))
    if user_parts:
        result_parts.append("\n\n".join(user_parts))
    return "\n\n".join(result_parts) if result_parts else content


def load_thread_messages(thread_id: str) -> list[dict]:
    """Rebuild the message list from the LangGraph checkpoint."""
    config = {"configurable": {"thread_id": thread_id}}
    try:
        agent = get_agent_graph()
        snapshot = agent.get_state(config)
        if snapshot and snapshot.values and "messages" in snapshot.values:
            msgs: list[dict] = []
            pending_tool_results: list[dict] = []
            pending_charts: list[str] = []
            for m in snapshot.values["messages"]:
                if m.type == "tool":
                    tool_name = getattr(m, "name", "") or "tool"
                    tool_content = m.content if isinstance(m.content, str) else str(m.content)

                    # Extract chart JSON from __CHART__: markers
                    if tool_content and tool_content.startswith("__CHART__:"):
                        marker_end = tool_content.find("\n\n", 10)
                        if marker_end == -1:
                            fig_json = tool_content[10:]
                            display_text = "Chart created"
                        else:
                            fig_json = tool_content[10:marker_end]
                            display_text = tool_content[marker_end + 2:]
                        pending_charts.append(fig_json)
                        tool_content = display_text

                    pending_tool_results.append({
                        "name": tool_name,
                        "content": tool_content,
                    })
                elif m.type == "human" and m.content:
                    pending_tool_results.clear()
                    pending_charts.clear()
                    # Check for user-attached images (base64 in multimodal content)
                    user_images: list[str] = []
                    if isinstance(m.content, list):
                        text_parts = []
                        for part in m.content:
                            if isinstance(part, dict):
                                if part.get("type") == "text":
                                    text_parts.append(part["text"])
                                elif part.get("type") == "image_url":
                                    url = part.get("image_url", {}).get("url", "")
                                    if url.startswith("data:image"):
                                        b64 = url.split(",", 1)[1] if "," in url else ""
                                        if b64:
                                            user_images.append(b64)
                        content = "\n".join(text_parts)
                    else:
                        content = m.content
                    msg_dict: dict = {"role": "user", "content": _strip_file_context(content)}
                    if user_images:
                        msg_dict["images"] = user_images
                    msgs.append(msg_dict)
                elif m.type == "ai" and m.content:
                    msg_dict = {"role": "assistant", "content": m.content}
                    if pending_tool_results:
                        msg_dict["tool_results"] = list(pending_tool_results)
                        pending_tool_results = []
                    if pending_charts:
                        msg_dict["charts"] = list(pending_charts)
                        pending_charts = []
                    msgs.append(msg_dict)
            return msgs
    except Exception:
        pass
    return []


def _process_attached_files(
    files: list[dict],
    vision_svc: VisionService | None,
) -> tuple[str, list[str], list[str]]:
    """Process uploaded files and return (context_text, image_b64_list, warnings).

    *files* is a list of ``{"name": str, "data": bytes}`` dicts.
    """
    budget = _file_budget()
    context_parts: list[str] = []
    images_b64: list[str] = []
    warnings: list[str] = []

    for f in files:
        name = f["name"]
        data = f["data"]
        suffix = pathlib.Path(name).suffix.lower()

        if suffix in _IMAGE_EXTENSIONS:
            b64 = _b64.b64encode(data).decode("ascii")
            images_b64.append(b64)
            if vision_svc and vision_svc.enabled:
                description = vision_svc.analyze(
                    data, f"Describe this image in detail. The filename is '{name}'."
                )
                context_parts.append(f"[Attached image: {name}]\n{description}")
            else:
                context_parts.append(f"[Attached image: {name} — vision is disabled, cannot analyze]")

        elif suffix == ".pdf":
            try:
                from pypdf import PdfReader
                reader = PdfReader(io.BytesIO(data))
                pages = []
                for i, page in enumerate(reader.pages):
                    text = page.extract_text() or ""
                    if text.strip():
                        pages.append(f"--- Page {i+1} ---\n{text}")
                    if sum(len(p) for p in pages) > budget:
                        pages.append(f"[Truncated — {len(reader.pages)} pages total, showing first {i+1}]")
                        warnings.append(f"📎 {name}: truncated — {len(reader.pages)} pages total, only first {i+1} shown")
                        break
                content = "\n".join(pages) if pages else "(No extractable text found)"
                context_parts.append(f"[Attached PDF: {name}, {len(reader.pages)} pages]\n{content}")
            except Exception as exc:
                context_parts.append(f"[Attached PDF: {name} — failed to extract text: {exc}]")

        elif suffix in _DATA_EXTENSIONS:
            try:
                from data_reader import read_data_file
                buf = io.BytesIO(data)
                summary = read_data_file(buf, name=name, max_chars=budget)
                context_parts.append(f"[Attached data file: {name}]\n{summary}")
                state.attached_data_cache[name] = data
            except Exception as exc:
                context_parts.append(f"[Attached data file: {name} — failed to parse: {exc}]")

        elif suffix in _TEXT_EXTENSIONS:
            try:
                text = data.decode("utf-8", errors="replace")
                if len(text) > budget:
                    warnings.append(f"📎 {name}: truncated — showing first {budget:,} of {len(text):,} chars")
                    text = text[:budget] + f"\n[Truncated — {len(data)} bytes total]"
                context_parts.append(f"[Attached file: {name}]\n{text}")
            except Exception as exc:
                context_parts.append(f"[Attached file: {name} — failed to read: {exc}]")
        else:
            context_parts.append(f"[Attached file: {name} — unsupported file type '{suffix}']")

    # ── Total-budget cap: proportionally shrink if combined text > budget ──
    total_chars = sum(len(p) for p in context_parts)
    if total_chars > budget and len(context_parts) > 0:
        for idx, part in enumerate(context_parts):
            share = len(part) / total_chars
            cap = max(2_000, int(budget * share))
            if len(part) > cap:
                warnings.append(f"📎 Trimmed to fit context — showing first {cap:,} of {len(part):,} chars")
                context_parts[idx] = (
                    part[:cap]
                    + f"\n[Trimmed to fit — showing first {cap:,} of {len(part):,} chars]"
                )

    return "\n\n".join(context_parts), images_b64, warnings


# ── Export helpers ───────────────────────────────────────────────────────────

def _export_as_markdown(thread_name: str, messages: list[dict]) -> str:
    lines = [f"# {thread_name}\n"]
    lines.append(f"*Exported from Thoth on {datetime.now().strftime('%Y-%m-%d %H:%M')}*\n")
    lines.append("---\n")
    for msg in messages:
        role = "🧑 User" if msg["role"] == "user" else "𓁟 Thoth"
        lines.append(f"### {role}\n")
        lines.append(msg["content"] + "\n")
    return "\n".join(lines)


def _export_as_text(thread_name: str, messages: list[dict]) -> str:
    lines = [thread_name]
    lines.append(f"Exported from Thoth on {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("=" * 60)
    lines.append("")
    for msg in messages:
        role = "User" if msg["role"] == "user" else "Thoth"
        lines.append(f"[{role}]")
        lines.append(msg["content"])
        lines.append("")
    return "\n".join(lines)


def _export_as_pdf(thread_name: str, messages: list[dict]) -> bytes:
    """Convert messages to a PDF document. Returns PDF bytes."""
    from fpdf import FPDF

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()

    def _safe(text: str) -> str:
        return text.encode("latin-1", errors="replace").decode("latin-1")

    # Title
    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 12, _safe(thread_name), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(128, 128, 128)
    pdf.cell(0, 6, f"Exported from Thoth on {datetime.now().strftime('%Y-%m-%d %H:%M')}",
             new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(4)
    pdf.set_draw_color(200, 200, 200)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(6)

    for msg in messages:
        role = "User" if msg["role"] == "user" else "Thoth"
        pdf.set_font("Helvetica", "B", 11)
        if msg["role"] == "user":
            pdf.set_text_color(50, 100, 200)
        else:
            pdf.set_text_color(200, 160, 0)
        pdf.cell(0, 8, role, new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)

        pdf.set_font("Helvetica", "", 10)
        safe_text = _safe(msg["content"])
        pdf.multi_cell(0, 5, safe_text)
        pdf.ln(4)

    return bytes(pdf.output())


# ── Cross-platform browse helpers (run in executor to avoid blocking) ────────

def _pick_folder_native(title: str, initial_dir: str) -> str | None:
    """Platform-native folder picker (no tkinter dependency on macOS/Linux)."""
    if sys.platform == "darwin":
        script = f'POSIX path of (choose folder with prompt "{title}"'
        if initial_dir and os.path.isdir(initial_dir):
            script += f' default location POSIX file "{initial_dir}"'
        script += ')'
        try:
            r = subprocess.run(["osascript", "-e", script],
                               capture_output=True, text=True, timeout=120)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip().rstrip("/")
        except Exception:
            pass
        return None

    if sys.platform.startswith("linux"):
        for cmd in (
            ["zenity", "--file-selection", "--directory", f"--title={title}"],
            ["kdialog", "--getexistingdirectory", initial_dir or ".",
             "--title", title],
        ):
            try:
                r = subprocess.run(cmd, capture_output=True, text=True,
                                   timeout=120)
                if r.returncode == 0 and r.stdout.strip():
                    return r.stdout.strip()
            except FileNotFoundError:
                continue
            except Exception:
                pass

    # Windows / fallback: tkinter
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk(); root.withdraw(); root.attributes("-topmost", True)
        result = filedialog.askdirectory(title=title,
                                         initialdir=initial_dir or None)
        root.destroy()
        return result or None
    except ImportError:
        return None


def _pick_file_native(
    title: str, initial_dir: str, filetypes: list[tuple[str, str]] | None,
) -> str | None:
    """Platform-native file picker (no tkinter dependency on macOS/Linux)."""
    if sys.platform == "darwin":
        script = f'POSIX path of (choose file with prompt "{title}"'
        if initial_dir and os.path.isdir(initial_dir):
            script += f' default location POSIX file "{initial_dir}"'
        if filetypes:
            exts = []
            for _, pattern in filetypes:
                for part in pattern.split(";"):
                    ext = part.strip().lstrip("*.").lower()
                    if ext:
                        exts.append(f'"{ext}"')
            if exts:
                script += f' of type {{{", ".join(exts)}}}'
        script += ')'
        try:
            r = subprocess.run(["osascript", "-e", script],
                               capture_output=True, text=True, timeout=120)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except Exception:
            pass
        return None

    if sys.platform.startswith("linux"):
        filt = ""
        if filetypes:
            filt = " ".join(p for _, p in filetypes)
        for cmd in (
            ["zenity", "--file-selection", f"--title={title}"]
            + ([f"--file-filter={filt}"] if filt else []),
            ["kdialog", "--getopenfilename", initial_dir or ".",
             filt or "*", "--title", title],
        ):
            try:
                r = subprocess.run(cmd, capture_output=True, text=True,
                                   timeout=120)
                if r.returncode == 0 and r.stdout.strip():
                    return r.stdout.strip()
            except FileNotFoundError:
                continue
            except Exception:
                pass

    # Windows / fallback: tkinter
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk(); root.withdraw(); root.attributes("-topmost", True)
        result = filedialog.askopenfilename(
            title=title, initialdir=initial_dir or None,
            filetypes=filetypes or [],
        )
        root.destroy()
        return result or None
    except ImportError:
        return None


async def _browse_folder(title: str = "Select folder",
                         initial_dir: str = "") -> str | None:
    return await asyncio.to_thread(_pick_folder_native, title, initial_dir)


async def _browse_file(
    title: str = "Select file",
    initial_dir: str = "",
    filetypes: list[tuple[str, str]] | None = None,
) -> str | None:
    return await asyncio.to_thread(_pick_file_native, title, initial_dir,
                                   filetypes)


# ═════════════════════════════════════════════════════════════════════════════
# MAIN PAGE
# ═════════════════════════════════════════════════════════════════════════════

@ui.page("/")
async def index():
    # ── Theme ────────────────────────────────────────────────────────────
    ui.dark_mode(True)

    # ── Startup splash (non-blocking: polls global status, redirects when ready)
    if not _startup_ready:
        with ui.column().classes("absolute-center items-center gap-4"):
            ui.label("𓁟").style("font-size: 4rem; color: gold;")
            ui.label("Thoth").style("font-size: 1.6rem; font-weight: 700; letter-spacing: 0.1em; color: gold;")
            status_label = ui.label(_startup_status).classes("text-grey-5 text-sm")
            ui.spinner("dots", size="1.5rem", color="grey-6")

        def _poll_ready():
            status_label.text = _startup_status
            if _startup_ready:
                ui.navigate.to("/")

        ui.timer(0.3, _poll_ready)
        return  # page is complete; timer keeps polling

    # ── Show startup warnings (channel auto-start failures, etc.) ────────
    if _startup_warnings:
        for msg in _startup_warnings:
            ui.notify(msg, type="warning", timeout=8000, close_button=True)
        _startup_warnings.clear()

    ui.add_head_html("""
    <link rel="stylesheet"
          href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/atom-one-dark.min.css">
    <script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>
    <style>
        .thoth-msg pre { overflow-x: auto; max-width: 100%; }
        .thoth-msg a { color: #64b5f6; }
        .thoth-msg a:hover { text-decoration: underline; }
        .thoth-msg-row {
            display: flex;
            gap: 0.75rem;
            padding: 0.75rem 0.5rem;
            width: 100%;
            border-radius: 8px;
        }
        .thoth-msg-row-user {
            background: rgba(255, 255, 255, 0.04);
        }
        .thoth-avatar {
            width: 36px;
            height: 36px;
            min-width: 36px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 1.1rem;
            margin-top: 2px;
        }
        .thoth-avatar-user { background: #1976d2; color: white; }
        .thoth-avatar-bot { background: #37474f; color: gold !important; }
        .thoth-msg-header {
            display: flex !important;
            align-items: baseline;
            gap: 0.5rem;
        }
        .thoth-msg-name {
            font-weight: 600;
            font-size: 0.9rem;
            color: #e0e0e0;
        }
        /* Bot name = gold */
        .thoth-msg-row:not(.thoth-msg-row-user) .thoth-msg-name {
            color: gold !important;
        }
        .thoth-msg-stamp {
            font-size: 0.7rem;
            color: #888;
            margin-left: 0.5rem;
        }
        .thoth-msg-body {
            flex: 1;
            min-width: 0;
            overflow: hidden;
            /* Override Quasar QScrollArea's user-select: none */
            -webkit-user-select: text;
            user-select: text;
            cursor: default;
        }
        .thoth-msg-body .thoth-msg,
        .thoth-msg-body p,
        .thoth-msg-body li,
        .thoth-msg-body td,
        .thoth-msg-body th,
        .thoth-msg-body span:not(.thoth-msg-name):not(.thoth-msg-stamp) {
            cursor: text;
        }
        .thoth-msg-body .nicegui-code pre {
            white-space: pre-wrap;
            word-break: break-all;
        }
        .thoth-typing .dots span {
            animation: tblink 1.4s infinite both;
        }
        .thoth-typing .dots span:nth-child(2) { animation-delay: 0.2s; }
        .thoth-typing .dots span:nth-child(3) { animation-delay: 0.4s; }
        @keyframes tblink {
            0%, 80%, 100% { opacity: 0; }
            40% { opacity: 1; }
        }
    </style>
    <script>
    // Make all links in chat messages open in a new tab
    document.addEventListener('click', function(e) {
        const a = e.target.closest('.thoth-msg a, .thoth-msg-body a');
        if (a && a.href && !a.href.startsWith('javascript:')) {
            a.target = '_blank';
            a.rel = 'noopener noreferrer';
        }
    });
    </script>
    """)

    # ── Per-page UI references ───────────────────────────────────────────
    class P:
        """Per-client page element references."""
        main_col: ui.column = None          # type: ignore[assignment]
        chat_scroll: ui.scroll_area = None  # type: ignore[assignment]
        chat_container: ui.column = None    # type: ignore[assignment]
        thread_container: ui.column = None  # type: ignore[assignment]
        token_label: ui.label = None        # type: ignore[assignment]
        token_bar: ui.linear_progress = None  # type: ignore[assignment]
        voice_status_label: ui.label = None  # type: ignore[assignment]
        stop_btn: ui.button = None          # type: ignore[assignment]
        voice_switch: ui.switch = None      # type: ignore[assignment]
        pending_files: list[dict] = []
        file_chips_row: ui.row = None       # type: ignore[assignment]
        chat_input: ui.input = None         # type: ignore[assignment]
        chat_header_label: ui.label = None  # type: ignore[assignment]
        settings_dlg: ui.dialog = None      # type: ignore[assignment]
        export_dlg: ui.dialog = None        # type: ignore[assignment]
        interrupt_dlg: ui.dialog = None     # type: ignore[assignment]

    p = P()
    p.pending_files = []

    # ── Health check ─────────────────────────────────────────────────────
    def _run_health_check() -> tuple[bool, str]:
        try:
            import ollama as _oll
            _oll.list()
        except Exception:
            return False, "Cannot connect to Ollama. Make sure it is running (`ollama serve`)."
        current = get_current_model()
        if not is_model_local(current):
            return False, f"Model {current} is not downloaded. Open Settings → Models to download it."
        return True, ""

    ok, err = await run.io_bound(_run_health_check)
    # Only show the "model not downloaded" notification if setup wizard was already completed
    if not ok and _is_setup_complete():
        ui.notify(err, type="negative", timeout=0, close_button=True)

    # ── First-launch setup wizard ────────────────────────────────────────
    if not _is_setup_complete():
        from models import POPULAR_MODELS, DEFAULT_MODEL
        from vision import DEFAULT_VISION_MODEL

        setup_dlg = ui.dialog().props("persistent maximized transition-show=fade transition-hide=fade")

        with setup_dlg:
            with ui.card().classes("w-full max-w-2xl mx-auto q-pa-lg"):
                # ── Header ───────────────────────────────────────────────
                ui.html(
                    '<div style="text-align:center;">'
                    '<h1 style="color: gold; margin-bottom: 0;">𓁟 Welcome to Thoth</h1>'
                    '</div>',
                    sanitize=False,
                )
                ui.label(
                    "Let's get you set up. This will only take a minute."
                ).classes("text-center text-grey-6")

                ui.separator()

                # ── Brain Model ──────────────────────────────────────────
                ui.label("🧠 Brain Model").classes("text-h6")
                ui.label(
                    "The main reasoning model that powers conversations and tool use. "
                    "14B+ recommended for best accuracy."
                ).classes("text-grey-6 text-sm")

                local_now = await run.io_bound(list_local_models)
                setup_all_models = list_all_models()
                brain_default = state.current_model

                setup_brain_opts = {
                    m: f"{'✅' if m in local_now else '⬇️'}  {m}"
                    for m in setup_all_models
                }
                setup_brain_select = ui.select(
                    label="Brain model",
                    options=setup_brain_opts,
                    value=brain_default,
                ).classes("w-full")

                brain_status = ui.label("").classes("text-sm")
                brain_status.visible = False
                brain_done = {"value": brain_default in local_now}

                setup_brain_dl = ui.button(f"⬇️ Download {brain_default}").props("color=primary")
                setup_brain_dl.visible = brain_default not in local_now
                if brain_default in local_now:
                    brain_status.text = f"✅ {brain_default} is ready"
                    brain_status.visible = True

                async def _setup_dl_brain():
                    sel = setup_brain_select.value
                    if is_model_local(sel):
                        brain_status.text = f"✅ {sel} is already downloaded"
                        brain_status.visible = True
                        brain_done["value"] = True
                        setup_brain_dl.visible = False
                        _update_finish()
                        return
                    setup_brain_dl.disable()
                    brain_status.text = f"⏳ Downloading {sel}… this may take a few minutes"
                    brain_status.visible = True
                    n = ui.notification(f"Downloading {sel}…", type="ongoing", spinner=True, timeout=None)
                    await run.io_bound(lambda: list(pull_model(sel)))
                    n.dismiss()
                    brain_status.text = f"✅ {sel} downloaded successfully!"
                    setup_brain_dl.visible = False
                    setup_brain_dl.enable()
                    brain_done["value"] = True
                    set_model(sel)
                    state.current_model = sel
                    clear_agent_cache()
                    _update_finish()

                setup_brain_dl.on_click(_setup_dl_brain)

                def _on_setup_brain_change(e):
                    sel = e.value
                    setup_brain_dl.text = f"⬇️ Download {sel}"
                    already = is_model_local(sel)
                    setup_brain_dl.visible = not already
                    brain_done["value"] = already
                    if already:
                        brain_status.text = f"✅ {sel} is ready"
                        brain_status.visible = True
                        set_model(sel)
                        state.current_model = sel
                        clear_agent_cache()
                    else:
                        brain_status.visible = False
                    _update_finish()

                setup_brain_select.on_value_change(_on_setup_brain_change)

                ui.separator()

                # ── Vision Model ─────────────────────────────────────────
                ui.label("👁️ Vision Model").classes("text-h6")
                ui.label(
                    "Used for camera and screen capture analysis. "
                    "Optional — you can skip this and download it later."
                ).classes("text-grey-6 text-sm")

                vsvc = state.vision_service
                setup_vision_opts = {
                    m: f"{'✅' if m in local_now else '⬇️'}  {m}"
                    for m in sorted(set(POPULAR_VISION_MODELS + ([vsvc.model] if vsvc.model not in POPULAR_VISION_MODELS else [])))
                }
                setup_vision_select = ui.select(
                    label="Vision model",
                    options=setup_vision_opts,
                    value=vsvc.model,
                ).classes("w-full")

                vision_status = ui.label("").classes("text-sm")
                vision_status.visible = False

                setup_vision_dl = ui.button(f"⬇️ Download {vsvc.model}").props("color=primary outline")
                setup_vision_dl.visible = vsvc.model not in local_now
                if vsvc.model in local_now:
                    vision_status.text = f"✅ {vsvc.model} is ready"
                    vision_status.visible = True

                async def _setup_dl_vision():
                    sel = setup_vision_select.value
                    if is_model_local(sel):
                        vision_status.text = f"✅ {sel} is already downloaded"
                        vision_status.visible = True
                        setup_vision_dl.visible = False
                        return
                    setup_vision_dl.disable()
                    vision_status.text = f"⏳ Downloading {sel}… this may take a few minutes"
                    vision_status.visible = True
                    n = ui.notification(f"Downloading {sel}…", type="ongoing", spinner=True, timeout=None)
                    await run.io_bound(lambda: list(pull_model(sel)))
                    n.dismiss()
                    vision_status.text = f"✅ {sel} downloaded successfully!"
                    setup_vision_dl.visible = False
                    setup_vision_dl.enable()
                    vsvc.model = sel

                setup_vision_dl.on_click(_setup_dl_vision)

                def _on_setup_vision_change(e):
                    sel = e.value
                    setup_vision_dl.text = f"⬇️ Download {sel}"
                    already = is_model_local(sel)
                    setup_vision_dl.visible = not already
                    if already:
                        vision_status.text = f"✅ {sel} is ready"
                        vision_status.visible = True
                        vsvc.model = sel
                    else:
                        vision_status.visible = False

                setup_vision_select.on_value_change(_on_setup_vision_change)

                ui.separator()

                # ── Recommended Setup ────────────────────────────────────
                ui.label("📋 Recommended Setup").classes("text-h6")
                ui.label(
                    "After completing this wizard, head to Settings to get the most out of Thoth:"
                ).classes("text-grey-6 text-sm")

                tips = [
                    ("🔑", "API Keys", "Settings → Tools to add API keys for web search, weather, Wolfram Alpha, and more. DuckDuckGo search works without a key."),
                    ("📧", "Gmail", "Settings → Tools → Gmail to connect your Google account for reading and sending email."),
                    ("📅", "Calendar", "Settings → Tools → Calendar to connect Google Calendar for checking events and scheduling."),
                    ("📄", "Documents", "Drop PDFs, text files, or URLs into the sidebar to give Thoth context about your work."),
                    ("🎙️", "Voice", "Settings → Voice to enable hands-free voice input and spoken responses."),
                    ("📡", "Channels", "Settings → Channels to connect Telegram or Email so Thoth can respond to messages when the app is closed."),
                ]
                for icon, title, desc in tips:
                    with ui.row().classes("items-start gap-2 q-py-xs"):
                        ui.label(icon).classes("text-lg")
                        with ui.column().classes("gap-0"):
                            ui.label(title).classes("font-bold text-sm")
                            ui.label(desc).classes("text-grey-6 text-xs")

                ui.separator()

                # ── Finish ───────────────────────────────────────────────
                finish_btn = ui.button("Get Started →").props("color=primary size=lg").classes("w-full")

                def _update_finish():
                    finish_btn.set_enabled(brain_done["value"])

                _update_finish()

                async def _finish_setup():
                    _mark_setup_complete()
                    setup_dlg.close()
                    # If brain model was downloaded, suppress the health-check notification
                    if brain_done["value"]:
                        _rebuild_main()

                finish_btn.on_click(_finish_setup)

        setup_dlg.open()

    # ══════════════════════════════════════════════════════════════════════
    # RENDER HELPERS
    # ══════════════════════════════════════════════════════════════════════

    def _render_text_with_embeds(text: str) -> None:
        """Render markdown text with inline YouTube video embeds."""
        if not text:
            return
        seen_yt: set[str] = set()
        last_end = 0
        parts: list[tuple[str, str]] = []
        for match in _YT_URL_PATTERN.finditer(text):
            vid_id = match.group(1)
            parts.append(("text", text[last_end:match.end()]))
            if vid_id not in seen_yt:
                seen_yt.add(vid_id)
                parts.append(("video", vid_id))
            last_end = match.end()
        if last_end < len(text):
            parts.append(("text", text[last_end:]))
        if not parts:
            ui.markdown(text, extras=['code-friendly', 'fenced-code-blocks', 'tables']).classes("thoth-msg w-full")
        else:
            for kind, value in parts:
                if kind == "text" and value.strip():
                    ui.markdown(value, extras=['code-friendly', 'fenced-code-blocks', 'tables']).classes("thoth-msg w-full")
                elif kind == "video":
                    ui.html(
                        f'<iframe width="280" height="158" '
                        f'src="https://www.youtube.com/embed/{value}" '
                        f'frameborder="0" allowfullscreen '
                        f'style="border-radius:8px;"></iframe>',
                        sanitize=False,
                    )

    def _render_message_content(msg: dict) -> None:
        """Render a single message's content inside the current parent element."""
        role = msg.get("role", "assistant")

        # Tool results
        tool_results = msg.get("tool_results")
        if tool_results:
            for tr in tool_results:
                with ui.expansion(f"✅ {tr['name']}", icon="check_circle").classes("w-full"):
                    content = tr.get("content", "")
                    if len(content) > 5_000:
                        content = content[:5_000] + "\n\n… (truncated)"
                    if content:
                        ui.code(content).classes("w-full text-xs")

        # Images (live) or placeholder (reloaded thread)
        images = msg.get("images")
        if images:
            caption = "📎 Attached" if role == "user" else "📷 Captured"
            for b64 in images:
                ui.image(f"data:image/jpeg;base64,{b64}").classes("w-80 rounded")
                ui.label(caption).classes("text-xs text-grey-6")
        elif tool_results and any(
            tr.get("name") in ("analyze_image", "👁️ Vision") for tr in tool_results
        ):
            with ui.row().classes("items-center gap-2").style(
                "padding: 0.5rem 0.75rem; border-radius: 8px; "
                "background: rgba(255,255,255,0.04);"
            ):
                ui.icon("image", size="sm").style("color: #888;")
                ui.label("Image not available — captures are transient to save space").style(
                    "font-size: 0.8rem; color: #888; font-style: italic;"
                )

        # Charts (Plotly)
        charts = msg.get("charts")
        if charts:
            try:
                import plotly.io as _pio
                for fig_json in charts:
                    fig = _pio.from_json(fig_json)
                    ui.plotly(fig).classes("w-full")
            except Exception:
                pass

        # Main text with inline YouTube embeds
        text = msg.get("content", "")
        if text:
            _render_text_with_embeds(text)

        # Trigger highlight.js on new code blocks
        try:
            ui.run_javascript("document.querySelectorAll('pre code').forEach(el => hljs.highlightElement(el));")
        except RuntimeError:
            pass

    def _add_chat_message(msg: dict) -> None:
        """Append a rendered chat message to the chat container."""
        if p.chat_container is None:
            return
        is_user = msg["role"] == "user"
        avatar_cls = "thoth-avatar thoth-avatar-user" if is_user else "thoth-avatar thoth-avatar-bot"
        avatar_content = "👤" if is_user else "𓁟"
        name = "You" if is_user else "Thoth"
        stamp = msg.get("timestamp", datetime.now().strftime("%H:%M"))
        with p.chat_container:
            row_cls = "thoth-msg-row thoth-msg-row-user" if is_user else "thoth-msg-row"
            with ui.element("div").classes(row_cls):
                ui.html(f'<div class="{avatar_cls}">{avatar_content}</div>', sanitize=False)
                with ui.column().classes("thoth-msg-body gap-1"):
                    ui.html(
                        f'<div class="thoth-msg-header">'
                        f'<span class="thoth-msg-name">{name}</span>'
                        f'<span class="thoth-msg-stamp">{stamp}</span>'
                        f'</div>',
                        sanitize=False,
                    )
                    _render_message_content(msg)

    # ── Terminal entry renderer ──────────────────────────────────────────

    def _add_terminal_entry(entry: dict) -> None:
        """Render a single shell command + output in the terminal panel."""
        if p.terminal_container is None:
            return
        cmd = entry.get("command", "")
        output = entry.get("output", "")
        exit_code = entry.get("exit_code", 0)
        duration = entry.get("duration", 0)
        cwd = entry.get("cwd", "")

        with p.terminal_container:
            # Prompt line
            cwd_short = os.path.basename(cwd) if cwd else "~"
            color = "#4ec9b0" if exit_code == 0 else "#f44747"
            ui.html(
                f'<div style="font-family:monospace; font-size:0.8rem; color:#569cd6;">'
                f'<span style="color:#888;">{cwd_short}</span> '
                f'<span style="color:#dcdcaa;">$</span> {cmd}</div>',
                sanitize=False,
            )
            # Output
            if output:
                ui.html(
                    f'<pre style="font-family:monospace; font-size:0.75rem; '
                    f'color:#d4d4d4; margin:0; padding:2px 0; white-space:pre-wrap; '
                    f'word-break:break-all; max-height:200px; overflow-y:auto;">'
                    f'{output}</pre>',
                    sanitize=False,
                )
            # Exit code badge
            ui.html(
                f'<div style="font-size:0.65rem; color:{color}; margin-bottom:4px;">'
                f'exit {exit_code} · {duration}s</div>',
                sanitize=False,
            )

    # ══════════════════════════════════════════════════════════════════════
    # STREAMING
    # ══════════════════════════════════════════════════════════════════════

    async def _send_message(text: str, voice_mode: bool = False) -> None:
        """Send a message and stream the agent response."""
        if not text.strip() or state.is_generating:
            return

        # Ensure a thread exists
        if state.thread_id is None:
            tid = uuid.uuid4().hex[:12]
            name = text[:50]
            _save_thread_meta(tid, name)
            state.thread_id = tid
            state.thread_name = name
            state.messages = []
            state.show_onboarding = False
            _rebuild_main()
            _rebuild_thread_list()

        state.is_generating = True
        state.stop_event = threading.Event()   # fresh event per generation
        if p.stop_btn:
            p.stop_btn.enable()

        # ── Process attached files ───────────────────────────────────────
        file_context = ""
        user_images: list[str] = []
        file_warnings: list[str] = []
        if p.pending_files:
            file_context, user_images, file_warnings = await run.io_bound(
                _process_attached_files, list(p.pending_files), state.vision_service
            )
            p.pending_files.clear()
            if p.file_chips_row:
                p.file_chips_row.clear()
            for fw in file_warnings:
                ui.notify(fw, type="warning", position="top", close_button=True, timeout=8000)

        # ── Build agent input ────────────────────────────────────────────
        agent_input = text
        if file_context:
            agent_input = f"{file_context}\n\n{text}" if text else file_context

        display_content = text
        if file_context and not text:
            display_content = "[attached files]"

        # ── Append user message ──────────────────────────────────────────
        user_msg: dict = {"role": "user", "content": display_content}
        if user_images:
            user_msg["images"] = user_images
        state.messages.append(user_msg)
        _add_chat_message(user_msg)

        # Auto-name thread
        if state.thread_name and (state.thread_name.startswith("Thread ") or state.thread_name.startswith("💻 Thread ")):
            state.thread_name = f"💻 {display_content[:50]}"
            _save_thread_meta(state.thread_id, state.thread_name)
            _rebuild_thread_list()
            if p.chat_header_label:
                p.chat_header_label.set_text(f"💬 {state.thread_name}")
        else:
            _save_thread_meta(state.thread_id, state.thread_name)

        # ── Prepare assistant message placeholder ────────────────────────
        thinking_label = None
        assistant_md = None
        tool_col = None

        with p.chat_container:
            with ui.element("div").classes("thoth-msg-row"):
                ui.html('<div class="thoth-avatar thoth-avatar-bot">𓁟</div>', sanitize=False)
                with ui.column().classes("thoth-msg-body gap-1") as _wrapper:
                    ui.html(
                        '<div class="thoth-msg-header">'
                        '<span class="thoth-msg-name">Thoth</span>'
                        f'<span class="thoth-msg-stamp">{datetime.now().strftime("%H:%M")}</span>'
                        '</div>',
                        sanitize=False,
                    )
                    tool_col = ui.column().classes("w-full gap-1")
                    thinking_label = ui.html(
                        '<span class="thoth-typing" style="font-size:0.9rem; opacity:0.6;">'
                        'Thoth is thinking<span class="dots">'
                        '<span>.</span><span>.</span><span>.</span></span></span>',
                        sanitize=False,
                    )
                    assistant_md = ui.markdown("", extras=['code-friendly', 'fenced-code-blocks', 'tables']).classes("thoth-msg w-full")
                    assistant_md.set_visibility(False)

        if p.chat_scroll:
            p.chat_scroll.scroll_to(percent=1.0)

        # ── Build config ─────────────────────────────────────────────────
        config = {
            "configurable": {"thread_id": state.thread_id},
            "recursion_limit": 25,
        }
        enabled_tools = [t.name for t in tool_registry.get_enabled_tools()]

        if voice_mode:
            agent_input = (
                "[Voice input — the user is speaking to you via microphone "
                "and your response will be read aloud. Keep responses concise "
                "and conversational.]\n\n" + agent_input
            )

        # ── Stream in background thread via queue ────────────────────────
        q: queue.Queue = queue.Queue()
        stop_event = state.stop_event     # capture ref for this generation

        def _sync_stream():
            try:
                for ev in stream_agent(agent_input, enabled_tools, config,
                                       stop_event=stop_event):
                    if stop_event.is_set():
                        break
                    q.put(ev)
            except Exception as exc:
                if not stop_event.is_set():
                    q.put(("error", str(exc)))
            finally:
                if stop_event.is_set():
                    try:
                        repair_orphaned_tool_calls(enabled_tools, config)
                    except Exception:
                        pass
                q.put(None)          # always signal consumer we're done

        threading.Thread(target=_sync_stream, daemon=True).start()

        # ── Process events ───────────────────────────────────────────────
        accumulated = ""
        tool_results: list[dict] = []
        chart_data: list[str] = []
        captured_images: list[str] = []
        interrupt_data = None
        tts_buffer = ""
        tts_in_code = False
        tts_spoken = 0
        tts_active = voice_mode and state.tts_service.enabled
        first_content = False
        thinking_text = ""       # accumulated thinking tokens
        thinking_md = None        # live markdown element for streaming thinking
        thinking_collapsed = False  # whether thinking has been collapsed already

        _stopped_shown = False
        _drain_deadline = 0.0

        try:  # outer try — guarantees is_generating=False no matter what
         while True:
            if state.stop_event.is_set() and not _stopped_shown:
                # Show visual feedback immediately
                _stopped_shown = True
                _drain_deadline = asyncio.get_event_loop().time() + 30
                try:
                    if thinking_label:
                        thinking_label.delete()
                        thinking_label = None
                    if thinking_md:
                        thinking_md.delete()
                        thinking_md = None
                    if assistant_md:
                        assistant_md.set_visibility(True)
                        accumulated += "\n\n\u23f9\ufe0f *[Stopped]*"
                        assistant_md.set_content(accumulated)
                except Exception:
                    pass
                if tts_active:
                    state.tts_service.stop()
                # Don't break — drain queue until producer sends None

            # Timeout: if draining too long, give up
            if _stopped_shown and asyncio.get_event_loop().time() > _drain_deadline:
                break

            try:
                event = q.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.05)
                continue

            if event is None:
                break

            # After stop, keep draining but ignore events
            if _stopped_shown:
                continue

            event_type, payload = event

            _break_loop = False

            # Remove thinking indicator on first real content
            if not first_content and event_type in ("token", "done"):
                first_content = True
                if thinking_label:
                    thinking_label.delete()
                    thinking_label = None
                # Collapse streamed thinking into an expandable section
                if thinking_text and not thinking_collapsed:
                    thinking_collapsed = True
                    if thinking_md:
                        thinking_md.delete()
                        thinking_md = None
                    try:
                        if tool_col:
                            with tool_col:
                                with ui.expansion(
                                    "💭 Thinking", icon="psychology"
                                ).classes("w-full"):
                                    ui.code(
                                        thinking_text.strip()[:8_000]
                                    ).classes("w-full text-xs")
                    except Exception:
                        logger.error("Error rendering thinking collapse", exc_info=True)
                if assistant_md:
                    assistant_md.set_visibility(True)

            if event_type == "error":
                if thinking_label:
                    thinking_label.delete()
                if assistant_md:
                    assistant_md.set_visibility(True)
                    assistant_md.set_content(f"⚠️ An error occurred: {payload}")
                try:
                    repair_orphaned_tool_calls(enabled_tools, config)
                except Exception:
                    pass
                _break_loop = True

            elif event_type == "tool_call":
                if tool_col:
                    with tool_col:
                        _pending_exp = ui.expansion(
                            f"🔄 {payload}…", icon="hourglass_empty"
                        ).classes("w-full")
                        _pending_exp._thoth_tool_name = payload  # stash for matching

            elif event_type == "tool_done":
                tool_name = payload["name"] if isinstance(payload, dict) else payload
                tool_content = payload.get("content", "") if isinstance(payload, dict) else ""

                # Chart detection
                if tool_content and tool_content.startswith("__CHART__:"):
                    marker_end = tool_content.find("\n\n", 10)
                    if marker_end == -1:
                        fig_json = tool_content[10:]
                        display_text = "Chart created"
                    else:
                        fig_json = tool_content[10:marker_end]
                        display_text = tool_content[marker_end + 2:]
                    chart_data.append(fig_json)
                    try:
                        import plotly.io as _pio
                        fig = _pio.from_json(fig_json)
                        with tool_col:
                            ui.plotly(fig).classes("w-full")
                    except Exception:
                        pass
                    tool_content = display_text

                # Update the pending expansion or create a new one
                if tool_col:
                    # Find matching pending expansion
                    matched_exp = None
                    for child in tool_col:
                        if hasattr(child, '_thoth_tool_name'):
                            matched_exp = child
                    if matched_exp:
                        matched_exp._props["icon"] = "check_circle"
                        matched_exp._text = f"✅ {tool_name}"
                        matched_exp.update()
                        if tool_content:
                            display = tool_content[:5_000]
                            if len(tool_content) > 5_000:
                                display += "\n\n… (truncated)"
                            with matched_exp:
                                ui.code(display).classes("w-full text-xs")
                        del matched_exp._thoth_tool_name
                    else:
                        with tool_col:
                            with ui.expansion(f"✅ {tool_name}", icon="check_circle").classes("w-full"):
                                if tool_content:
                                    display = tool_content[:5_000]
                                    if len(tool_content) > 5_000:
                                        display += "\n\n… (truncated)"
                                    ui.code(display).classes("w-full text-xs")

                tool_results.append({"name": tool_name, "content": tool_content})

                # Shell command → render in terminal panel
                raw_tool_name = payload.get("raw_name", "") if isinstance(payload, dict) else ""
                if raw_tool_name == "run_command" and p.terminal_container is not None:
                    # Parse the output to build a history-style entry
                    _lines = (tool_content or "").split("\n")
                    _cmd_line = _lines[0][2:] if _lines and _lines[0].startswith("$ ") else ""
                    _info_line = _lines[-1] if _lines else ""
                    _e_code = 0
                    _dur = 0.0
                    _cwd = ""
                    import re as _re_term
                    _info_m = _re_term.search(
                        r"Exit code:\s*(-?\d+)\s*\|\s*Duration:\s*([\d.]+)s\s*\|\s*cwd:\s*(.*)",
                        _info_line,
                    )
                    if _info_m:
                        _e_code = int(_info_m.group(1))
                        _dur = float(_info_m.group(2))
                        _cwd = _info_m.group(3).strip()
                    _output_lines = _lines[1:-1] if len(_lines) > 2 else []
                    _add_terminal_entry({
                        "command": _cmd_line,
                        "output": "\n".join(_output_lines),
                        "exit_code": _e_code,
                        "duration": _dur,
                        "cwd": _cwd,
                    })
                    # Auto-show terminal panel on first shell result
                    if not getattr(p, "terminal_visible", False):
                        p.terminal_visible = True
                        if p.terminal_panel is not None:
                            p.terminal_panel.set_visibility(True)
                        if hasattr(p, "terminal_chevron") and p.terminal_chevron:
                            p.terminal_chevron.props("icon=expand_less")
                    if p.terminal_scroll:
                        p.terminal_scroll.scroll_to(percent=1.0)

                # Vision capture
                if tool_name in ("👁️ Vision", "analyze_image"):
                    vsvc = state.vision_service
                    if vsvc and vsvc.last_capture:
                        b64_img = _b64.b64encode(vsvc.last_capture).decode("ascii")
                        captured_images.append(b64_img)
                        with tool_col:
                            ui.image(f"data:image/jpeg;base64,{b64_img}").classes("w-80 rounded")
                        vsvc.last_capture = None

                # Browser screenshot thumbnail
                if raw_tool_name.startswith("browser_"):
                    try:
                        from tools.browser_tool import get_session_manager as _get_bsm
                        _bsm = _get_bsm()
                        if _bsm.has_active_session():
                            _bs = _bsm.get_session()
                            _screenshot_bytes = _bs.take_screenshot()
                            if _screenshot_bytes:
                                _b64_ss = _b64.b64encode(_screenshot_bytes).decode("ascii")
                                with tool_col:
                                    ui.image(f"data:image/png;base64,{_b64_ss}").classes(
                                        "w-80 rounded"
                                    ).style("border: 1px solid #333; margin-top: 4px;")
                    except Exception:
                        pass  # Screenshot is non-critical

            elif event_type == "summarizing":
                # Replace the thinking spinner with a summarization indicator
                if thinking_label:
                    thinking_label.delete()
                    thinking_label = None
                with _wrapper:
                    thinking_label = ui.html(
                        '<span class="thoth-typing" style="font-size:0.9rem; opacity:0.6;">'
                        '📝 Summarizing conversation history<span class="dots">'
                        '<span>.</span><span>.</span><span>.</span></span></span>',
                        sanitize=False,
                    )

            elif event_type == "thinking":
                pass  # spinner already visible

            elif event_type == "thinking_token":
                thinking_text += payload
                # Replace spinner with live streaming thinking text
                if thinking_label:
                    thinking_label.delete()
                    thinking_label = None
                if thinking_md is None:
                    with _wrapper:
                        thinking_md = ui.markdown(
                            "", extras=["code-friendly", "fenced-code-blocks"]
                        ).classes("thoth-msg w-full").style(
                            "opacity: 0.55; font-size: 0.88rem; font-style: italic;"
                        )
                thinking_md.set_content(thinking_text)
                if p.chat_scroll:
                    p.chat_scroll.scroll_to(percent=1.0)

            elif event_type == "token":
                accumulated += payload
                if assistant_md:
                    assistant_md.set_content(accumulated)
                if p.chat_scroll:
                    p.chat_scroll.scroll_to(percent=1.0)

                # Streaming TTS
                if tts_active:
                    if "```" in payload:
                        tts_in_code = not tts_in_code
                    if not tts_in_code:
                        tts_buffer += payload
                        sentences = _SENTENCE_SPLIT.split(tts_buffer)
                        if len(sentences) > 1:
                            for s in sentences[:-1]:
                                if tts_spoken >= _MAX_STREAM_SENTENCES:
                                    break
                                state.tts_service.speak_streaming(s)
                                tts_spoken += 1
                                if tts_spoken >= _MAX_STREAM_SENTENCES:
                                    state.tts_service.flush_streaming(
                                        "The full response is shown in the app."
                                    )
                                    tts_active = False
                            tts_buffer = sentences[-1]

            elif event_type == "interrupt":
                interrupt_data = payload
                _break_loop = True

            elif event_type == "done":
                accumulated = payload
                if thinking_label:
                    thinking_label.delete()
                    thinking_label = None
                # Collapse any leftover thinking (e.g. model only produced thinking)
                if thinking_text and not thinking_collapsed:
                    thinking_collapsed = True
                    if thinking_md:
                        thinking_md.delete()
                        thinking_md = None
                    try:
                        if tool_col:
                            with tool_col:
                                with ui.expansion(
                                    "💭 Thinking", icon="psychology"
                                ).classes("w-full"):
                                    ui.code(
                                        thinking_text.strip()[:8_000]
                                    ).classes("w-full text-xs")
                    except Exception:
                        logger.error("Error rendering thinking collapse (done)", exc_info=True)
                elif thinking_md:
                    thinking_md.delete()
                    thinking_md = None
                if assistant_md:
                    assistant_md.set_visibility(True)
                    assistant_md.set_content(accumulated)

            if _break_loop:
                break

        # ── Finalise ─────────────────────────────────────────────────────
         try:
            if tts_active:
                state.tts_service.flush_streaming(tts_buffer)

            # Replace plain markdown with YouTube-aware rendering if needed
            if accumulated and _YT_URL_PATTERN.search(accumulated):
                if assistant_md:
                    assistant_md.delete()
                    assistant_md = None
                with _wrapper:
                    _render_text_with_embeds(accumulated)

            # Highlight code blocks
            try:
                ui.run_javascript(
                    "document.querySelectorAll('pre code').forEach(el => hljs.highlightElement(el));"
                )
            except RuntimeError:
                pass

            # Store assistant message
            a_msg: dict = {"role": "assistant", "content": accumulated}
            if tool_results:
                a_msg["tool_results"] = tool_results
            if chart_data:
                a_msg["charts"] = chart_data
            if captured_images:
                a_msg["images"] = captured_images
            state.messages.append(a_msg)
         except Exception:
            logger.error("Error in post-stream finalization", exc_info=True)
        finally:
            state.is_generating = False
            if p.stop_btn:
                p.stop_btn.props('icon=stop')
                p.stop_btn.disable()

            # Resume mic
            if state.voice_enabled and not (state.tts_service and state.tts_service.enabled):
                state.voice_service.unmute()

        if p.chat_scroll:
            p.chat_scroll.scroll_to(percent=1.0)

        # Handle interrupt
        if interrupt_data:
            state.pending_interrupt = interrupt_data
            _show_interrupt(interrupt_data)

        _update_token_counter()

    # ── Resume after interrupt ───────────────────────────────────────────

    async def _resume_after_interrupt(approved: bool) -> None:
        state.pending_interrupt = None
        state.is_generating = True
        state.stop_event = threading.Event()   # fresh event per generation
        if p.stop_btn:
            p.stop_btn.enable()

        config = {
            "configurable": {"thread_id": state.thread_id},
            "recursion_limit": 25,
        }
        enabled_tools = [t.name for t in tool_registry.get_enabled_tools()]

        q: queue.Queue = queue.Queue()
        stop_event = state.stop_event     # capture ref for this generation

        def _sync():
            try:
                for ev in resume_stream_agent(enabled_tools, config, approved,
                                              stop_event=stop_event):
                    if stop_event.is_set():
                        break
                    q.put(ev)
            except Exception as exc:
                if not stop_event.is_set():
                    q.put(("error", str(exc)))
            finally:
                if stop_event.is_set():
                    try:
                        repair_orphaned_tool_calls(enabled_tools, config)
                    except Exception:
                        pass
                q.put(None)          # always signal consumer we're done

        threading.Thread(target=_sync, daemon=True).start()

        # Create placeholder
        assistant_md = None
        tool_col = None
        with p.chat_container:
            with ui.element("div").classes("thoth-msg-row"):
                ui.html('<div class="thoth-avatar thoth-avatar-bot">𓁟</div>', sanitize=False)
                with ui.column().classes("thoth-msg-body gap-1") as _wrapper:
                    ui.html(
                        '<div class="thoth-msg-header">'
                        '<span class="thoth-msg-name">Thoth</span>'
                        f'<span class="thoth-msg-stamp">{datetime.now().strftime("%H:%M")}</span>'
                        '</div>',
                        sanitize=False,
                    )
                    tool_col = ui.column().classes("w-full gap-1")
                    assistant_md = ui.markdown("", extras=['code-friendly', 'fenced-code-blocks', 'tables']).classes("thoth-msg w-full")

        accumulated = ""
        tool_results: list[dict] = []
        chart_data: list[str] = []

        _stopped_shown = False
        _drain_deadline = 0.0

        try:  # outer try — guarantees is_generating=False
         while True:
            if state.stop_event.is_set() and not _stopped_shown:
                _stopped_shown = True
                _drain_deadline = asyncio.get_event_loop().time() + 30
                if assistant_md:
                    accumulated += "\n\n\u23f9\ufe0f *[Stopped]*"
                    assistant_md.set_content(accumulated)

            if _stopped_shown and asyncio.get_event_loop().time() > _drain_deadline:
                break

            try:
                event = q.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.05)
                continue
            if event is None:
                break

            if _stopped_shown:
                continue

            et, pl = event
            if et == "token":
                accumulated += pl
                if assistant_md:
                    assistant_md.set_content(accumulated)
            elif et == "tool_call" and tool_col:
                with tool_col:
                    _pexp = ui.expansion(f"🔄 {pl}…", icon="hourglass_empty").classes("w-full")
                    _pexp._thoth_tool_name = pl
            elif et == "tool_done" and tool_col:
                tn = pl["name"] if isinstance(pl, dict) else pl
                tc = pl.get("content", "") if isinstance(pl, dict) else ""
                matched = None
                for child in tool_col:
                    if hasattr(child, '_thoth_tool_name'):
                        matched = child
                if matched:
                    matched._props["icon"] = "check_circle"
                    matched._text = f"✅ {tn}"
                    matched.update()
                    if tc:
                        display = tc[:5_000] + ("\n\n… (truncated)" if len(tc) > 5_000 else "")
                        with matched:
                            ui.code(display).classes("w-full text-xs")
                    del matched._thoth_tool_name
                else:
                    with tool_col:
                        with ui.expansion(f"✅ {tn}", icon="check_circle").classes("w-full"):
                            if tc:
                                display = tc[:5_000] + ("\n\n… (truncated)" if len(tc) > 5_000 else "")
                                ui.code(display).classes("w-full text-xs")
                tool_results.append({"name": tn, "content": tc})

                # Shell command → render in terminal panel
                _raw = pl.get("raw_name", "") if isinstance(pl, dict) else ""
                if _raw == "run_command" and p.terminal_container is not None:
                    _lines = (tc or "").split("\n")
                    _cmd_line = _lines[0][2:] if _lines and _lines[0].startswith("$ ") else ""
                    _info_line = _lines[-1] if _lines else ""
                    _e_code = 0
                    _dur = 0.0
                    _cwd = ""
                    import re as _re_term
                    _info_m = _re_term.search(
                        r"Exit code:\s*(-?\d+)\s*\|\s*Duration:\s*([\d.]+)s\s*\|\s*cwd:\s*(.*)",
                        _info_line,
                    )
                    if _info_m:
                        _e_code = int(_info_m.group(1))
                        _dur = float(_info_m.group(2))
                        _cwd = _info_m.group(3).strip()
                    _output_lines = _lines[1:-1] if len(_lines) > 2 else []
                    _add_terminal_entry({
                        "command": _cmd_line,
                        "output": "\n".join(_output_lines),
                        "exit_code": _e_code,
                        "duration": _dur,
                        "cwd": _cwd,
                    })
                    if not getattr(p, "terminal_visible", False):
                        p.terminal_visible = True
                        if p.terminal_panel is not None:
                            p.terminal_panel.set_visibility(True)
                        if hasattr(p, "terminal_chevron") and p.terminal_chevron:
                            p.terminal_chevron.props("icon=expand_less")
                    if p.terminal_scroll:
                        p.terminal_scroll.scroll_to(percent=1.0)

            elif et == "done":
                accumulated = pl
                if assistant_md:
                    assistant_md.set_content(accumulated)
            elif et == "interrupt":
                state.pending_interrupt = pl
                _show_interrupt(pl)
                break
            elif et == "error":
                if assistant_md:
                    assistant_md.set_content(f"⚠️ {pl}")
                break

         # Replace plain markdown with YouTube-aware rendering if needed
         try:
            if accumulated and _YT_URL_PATTERN.search(accumulated):
                if assistant_md:
                    assistant_md.delete()
                    assistant_md = None
                with _wrapper:
                    _render_text_with_embeds(accumulated)

            a_msg: dict = {"role": "assistant", "content": accumulated}
            if tool_results:
                a_msg["tool_results"] = tool_results
            if chart_data:
                a_msg["charts"] = chart_data
            state.messages.append(a_msg)
         except Exception:
            logger.error("Error in resume finalization", exc_info=True)
        finally:
            state.is_generating = False
            if p.stop_btn:
                p.stop_btn.props('icon=stop')
                p.stop_btn.disable()
        if p.chat_scroll:
            p.chat_scroll.scroll_to(percent=1.0)

    # ══════════════════════════════════════════════════════════════════════
    # INTERRUPT DIALOG
    # ══════════════════════════════════════════════════════════════════════

    p.interrupt_dlg = ui.dialog().props("persistent")

    def _show_interrupt(data: dict) -> None:
        p.interrupt_dlg.clear()
        desc = data.get("description", "The agent needs your approval.")
        with p.interrupt_dlg, ui.card().classes("q-pa-none").style(
            "width: 520px; max-width: 90vw; border-radius: 16px; overflow: hidden;"
            "background: #1a1a2e; border: 1px solid #2a2a4a;"
        ):
            # ── Header ──
            with ui.row().classes("w-full items-center q-pa-md").style(
                "background: linear-gradient(135deg, #2d1b00 0%, #1a1a2e 100%);"
                "border-bottom: 1px solid #3d2e00;"
            ):
                ui.icon("warning_amber", size="28px", color="amber")
                ui.label("Confirmation Required").style(
                    "font-size: 1.15rem; font-weight: 700; color: #f0c040; margin-left: 8px;"
                )
            # ── Body ──
            with ui.column().classes("w-full q-pa-lg"):
                ui.label("The agent wants to perform the following action:").style(
                    "font-size: 0.85rem; color: #8888aa; margin-bottom: 8px;"
                )
                with ui.element("div").style(
                    "background: #12121e; border: 1px solid #2a2a4a; border-radius: 10px;"
                    "padding: 14px 16px; max-height: 260px; overflow-y: auto;"
                    "font-size: 0.9rem; color: #d0d0e0; line-height: 1.6;"
                    "word-wrap: break-word; white-space: pre-wrap;"
                ):
                    ui.markdown(desc, extras=['code-friendly', 'fenced-code-blocks', 'tables'])
            # ── Footer ──
            with ui.row().classes("w-full justify-end q-pa-md gap-3").style(
                "border-top: 1px solid #2a2a4a;"
            ):
                ui.button("Deny", on_click=lambda: (_close_interrupt(False))).props(
                    "flat no-caps"
                ).style(
                    "color: #ff6b6b; font-weight: 600; font-size: 0.9rem;"
                    "padding: 8px 24px; border-radius: 8px;"
                )
                ui.button("Approve", on_click=lambda: (_close_interrupt(True))).props(
                    "unelevated no-caps"
                ).style(
                    "background: #2d8a4e; color: white; font-weight: 600;"
                    "font-size: 0.9rem; padding: 8px 28px; border-radius: 8px;"
                )
        p.interrupt_dlg.open()

    def _close_interrupt(approved: bool) -> None:
        p.interrupt_dlg.close()
        asyncio.create_task(_resume_after_interrupt(approved))

    # ══════════════════════════════════════════════════════════════════════
    # EXPORT DIALOG
    # ══════════════════════════════════════════════════════════════════════

    p.export_dlg = ui.dialog()

    def _save_export(data: bytes, filename: str) -> None:
        """Deliver an export file to the user.

        In native mode (pywebview) blob downloads are silently ignored on
        macOS WebKit, so we write directly to ~/Downloads and notify.
        In browser mode we use the normal ``ui.download()`` API.
        """
        if "--native" in sys.argv:
            dl_dir = pathlib.Path.home() / "Downloads"
            dl_dir.mkdir(parents=True, exist_ok=True)
            dest = dl_dir / filename
            # Avoid overwriting — append (1), (2), … if needed
            counter = 1
            while dest.exists():
                stem = pathlib.Path(filename).stem
                suffix = pathlib.Path(filename).suffix
                dest = dl_dir / f"{stem} ({counter}){suffix}"
                counter += 1
            dest.write_bytes(data)
            ui.notify(f"Saved to {dest}", type="positive")
        else:
            ui.download(data, filename)

    def _open_export() -> None:
        if not state.messages:
            ui.notify("Nothing to export.", type="warning")
            return
        name = state.thread_name or "conversation"
        msgs = state.messages
        p.export_dlg.clear()
        with p.export_dlg, ui.card().classes("w-96"):
            ui.label("📤 Export Conversation").classes("text-h6")
            ui.separator()
            with ui.column().classes("w-full gap-2"):
                def dl_md():
                    try:
                        data = _export_as_markdown(name, msgs).encode("utf-8")
                        fname = f"{name}.md"
                        p.export_dlg.close()
                        _save_export(data, fname)
                    except Exception as exc:
                        logger.exception("Export markdown failed")
                        ui.notify(f"Export failed: {exc}", type="negative")

                def dl_txt():
                    try:
                        data = _export_as_text(name, msgs).encode("utf-8")
                        fname = f"{name}.txt"
                        p.export_dlg.close()
                        _save_export(data, fname)
                    except Exception as exc:
                        logger.exception("Export text failed")
                        ui.notify(f"Export failed: {exc}", type="negative")

                def dl_pdf():
                    try:
                        data = _export_as_pdf(name, msgs)
                        fname = f"{name}.pdf"
                        p.export_dlg.close()
                        _save_export(data, fname)
                    except ImportError:
                        ui.notify("PDF export requires `fpdf2`. Run: pip install fpdf2", type="negative")
                    except Exception as exc:
                        logger.exception("Export PDF failed")
                        ui.notify(f"PDF export failed: {exc}", type="negative")

                ui.button("📄 Markdown", on_click=dl_md).classes("w-full")
                ui.button("📃 Plain text", on_click=dl_txt).classes("w-full")
                ui.button("📕 PDF", on_click=dl_pdf).classes("w-full")
            ui.separator()
            ui.button("Close", on_click=p.export_dlg.close).props("flat").classes("w-full")
        p.export_dlg.open()

    # ══════════════════════════════════════════════════════════════════════
    # SETTINGS DIALOG
    # ══════════════════════════════════════════════════════════════════════

    p.settings_dlg = ui.dialog().props("maximized transition-show=fade transition-hide=fade")

    def _open_settings() -> None:
        p.settings_dlg.clear()
        with p.settings_dlg:
            with ui.card().classes("w-full h-full no-shadow").style(
                "max-width: 64rem; margin: 0 auto;"
            ):
                # ── Header ───────────────────────────────────────────────
                with ui.row().classes("w-full items-center justify-between px-4 pt-3 pb-1"):
                    with ui.row().classes("items-center gap-2"):
                        ui.icon("settings", size="sm")
                        ui.label("Settings").classes("text-h5")
                    ui.button(icon="close", on_click=p.settings_dlg.close).props(
                        "flat round size=sm"
                    )

                ui.separator()

                # ── Left tabs + right content ────────────────────────────
                with ui.splitter(value=18).classes("w-full flex-grow").props(
                    "disable"
                ).style("height: calc(100vh - 100px);") as splitter:
                    with splitter.before:
                        with ui.tabs().props("vertical").classes(
                            "w-full h-full"
                        ) as tabs:
                            tab_models = ui.tab("Models", icon="smart_toy")
                            tab_mem = ui.tab("Memory", icon="psychology")
                            tab_voice = ui.tab("Voice", icon="mic")
                            tab_wf = ui.tab("Workflows", icon="bolt")
                            tab_fs = ui.tab("System", icon="terminal")
                            tab_tracker = ui.tab("Tracker", icon="checklist")
                            tab_docs = ui.tab("Documents", icon="description")
                            tab_tools = ui.tab("Search", icon="search")
                            tab_gmail = ui.tab("Gmail", icon="email")
                            tab_cal = ui.tab("Calendar", icon="event")
                            tab_channels = ui.tab("Channels", icon="forum")
                            tab_utils = ui.tab("Utilities", icon="build")

                    with splitter.after:
                        with ui.tab_panels(tabs, value=tab_models).classes(
                            "w-full h-full"
                        ) as panels:
                            with ui.tab_panel(tab_docs).classes("px-6 py-4"):
                                _build_documents_tab()
                            with ui.tab_panel(tab_models).classes("px-6 py-4"):
                                _build_models_tab()
                            with ui.tab_panel(tab_tools).classes("px-6 py-4"):
                                _build_tools_tab()
                            with ui.tab_panel(tab_fs).classes("px-6 py-4"):
                                _build_system_access_tab()
                            with ui.tab_panel(tab_gmail).classes("px-6 py-4"):
                                _build_gmail_tab()
                            with ui.tab_panel(tab_cal).classes("px-6 py-4"):
                                _build_calendar_tab()
                            with ui.tab_panel(tab_utils).classes("px-6 py-4"):
                                _build_utilities_tab()
                            with ui.tab_panel(tab_tracker).classes("px-6 py-4"):
                                _build_tracker_tab()
                            with ui.tab_panel(tab_mem).classes("px-6 py-4"):
                                _build_memory_tab()
                            with ui.tab_panel(tab_wf).classes("px-6 py-4"):
                                _build_workflows_tab()
                            with ui.tab_panel(tab_voice).classes("px-6 py-4"):
                                _build_voice_tab()
                            with ui.tab_panel(tab_channels).classes("px-6 py-4"):
                                _build_channels_tab()

        p.settings_dlg.open()

    # ── Settings tab builders ────────────────────────────────────────────

    def _build_documents_tab() -> None:
        ui.label("📄 Local Documents").classes("text-h6")
        ui.label(
            "Upload your own files (PDF, TXT, DOCX, etc.) to build a local knowledge base. "
            "Documents are chunked, vectorized, and stored in a local FAISS database "
            "for fast semantic search. When enabled, the agent will search these "
            "documents to answer questions about your personal content."
        ).classes("text-grey-6 text-sm")

        async def _handle_doc_upload(e: events.UploadEventArguments):
            name = e.file.name
            data = await e.file.read()
            with tempfile.NamedTemporaryFile(delete=False, suffix=pathlib.Path(name).suffix) as tmp:
                tmp.write(data)
                tmp_path = tmp.name
            try:
                await run.io_bound(load_and_vectorize_document, tmp_path, True, name)
                ui.notify(f"✅ {name} indexed", type="positive")
            except Exception as exc:
                ui.notify(f"Failed: {exc}", type="negative")
            finally:
                os.unlink(tmp_path)

        import tempfile
        ui.upload(
            label="Upload documents (PDF, DOCX, TXT)",
            on_upload=_handle_doc_upload,
            auto_upload=True,
            multiple=True,
        ).classes("w-full")

        ui.separator()
        processed = load_processed_files()
        if processed:
            ui.label(f"📚 {len(processed)} indexed document(s)").classes("font-bold")
            for f in sorted(processed):
                ui.label(f"  • {f}").classes("text-sm")
        else:
            ui.label("No documents indexed yet.").classes("text-grey-6")

        ui.separator()

        def _clear_docs():
            reset_vector_store()
            ui.notify("🗑️ Vector store cleared.", type="info")
            p.settings_dlg.close()
            _open_settings()

        ui.button("🗑️ Clear all documents", on_click=_clear_docs).props("flat color=negative")

    def _build_models_tab() -> None:
        ui.label("🤖 Models").classes("text-h6")
        ui.label(
            "Thoth uses two models: a Brain model for reasoning, tool use, "
            "and conversation, and a Vision model for camera-based image "
            "analysis. Both are served locally through Ollama. "
            "Models marked ✅ are already downloaded; ⬇️ models need to be "
            "downloaded first using the Download button."
        ).classes("text-grey-6 text-sm")
        ui.separator()
        ui.label("🧠 Brain Model").classes("text-h6")
        ui.label(
            "The main reasoning model that powers Thoth's conversations and "
            "tool use. Recommended: 14B+ for best accuracy. "
            "Minimum: 8B — smaller models may struggle with complex tasks."
        ).classes("text-grey-6 text-sm")

        all_models = list_all_models()
        local = list_local_models()
        current = state.current_model
        if current not in all_models:
            all_models = sorted(set(all_models + [current]))

        def _model_label(m):
            dl = '✅' if m in local else '⬇️'
            warn = '' if is_tool_compatible(m) else '  ⚠️ may not support tools'
            return f"{dl}  {m}{warn}"

        model_opts = {m: _model_label(m) for m in all_models}

        model_select = ui.select(
            label="Select model",
            options=model_opts,
            value=current,
        ).classes("w-full")

        # Download button — visible when selected model is not yet downloaded
        brain_dl_btn = ui.button(f"⬇️ Download {current}").props("color=primary outline")
        brain_dl_btn.visible = current not in local

        async def _download_brain(e=None):
            sel = model_select.value
            if is_model_local(sel):
                ui.notify(f"✅ {sel} is already downloaded.", type="info")
                brain_dl_btn.visible = False
                return
            brain_dl_btn.disable()
            n = ui.notification(f"Downloading {sel}…", type="ongoing", spinner=True, timeout=None)
            await run.io_bound(lambda: list(pull_model(sel)))
            n.dismiss()
            ui.notify(f"✅ {sel} ready!", type="positive")
            brain_dl_btn.visible = False
            brain_dl_btn.enable()
            # Refresh dropdown labels
            refreshed_local = list_local_models()
            model_select.options = {m: f"{'✅' if m in refreshed_local else '⬇️'}  {m}" + ('' if is_tool_compatible(m) else '  ⚠️ may not support tools') for m in all_models}
            model_select.update()
            # Apply the newly downloaded model
            set_model(sel)
            state.current_model = sel
            clear_agent_cache()

        brain_dl_btn.on_click(_download_brain)

        async def _on_model_change(e):
            sel = e.value
            if sel == state.current_model:
                return
            prev = state.current_model
            # Update download button
            brain_dl_btn.text = f"⬇️ Download {sel}"
            brain_dl_btn.visible = not is_model_local(sel)
            # If the model isn't downloaded yet, don't switch — wait for the Download button
            if not is_model_local(sel):
                return
            # Validate tool support for unknown families
            if not is_tool_compatible(sel):
                ui.notify(f"Checking tool support for {sel}…", type="info")
                ok = await run.io_bound(lambda: check_tool_support(sel))
                if not ok:
                    ui.notify(
                        f"⚠️ {sel} does not support tool calling. "
                        f"Reverting to {prev}.",
                        type="negative", close_button=True, timeout=10000,
                    )
                    model_select.value = prev
                    return
            set_model(sel)
            state.current_model = sel
            clear_agent_cache()
            # Notify if new model caps the selected context size
            model_max = await run.io_bound(lambda: get_model_max_context(sel))
            user_val = get_user_context_size()
            if model_max is not None and user_val > model_max:
                max_lbl = CONTEXT_SIZE_LABELS.get(model_max, f"{model_max:,}")
                usr_lbl = CONTEXT_SIZE_LABELS.get(user_val, f"{user_val:,}")
                ui.notify(
                    f"Context capped: {sel} max is {max_lbl} (you selected {usr_lbl}). "
                    f"Trimming will use {max_lbl}.",
                    type="warning", close_button=True, timeout=8000,
                )
            # Refresh context cap note since model max may differ
            if _ctx_note_updater[0]:
                _ctx_note_updater[0]()

        model_select.on_value_change(_on_model_change)
        _ctx_note_updater = [None]  # filled after _update_ctx_note is defined

        ui.separator()

        # Context window
        ctx_opts = {v: CONTEXT_SIZE_LABELS.get(v, str(v)) for v in CONTEXT_SIZE_OPTIONS}

        ctx_note = ui.label("").classes("text-xs text-warning")
        ctx_note.visible = False

        def _update_ctx_note():
            """Show a note if the effective context is capped by the model."""
            model_max = get_model_max_context()
            user_val = get_user_context_size()
            if model_max is not None and user_val > model_max:
                max_label = CONTEXT_SIZE_LABELS.get(model_max, f"{model_max:,}")
                ctx_note.text = f"ℹ️ Model max is {max_label} — trimming will use {max_label}"
                ctx_note.visible = True
            else:
                ctx_note.visible = False

        def _on_ctx_change(e):
            set_context_size(e.value)
            state.context_size = e.value
            clear_agent_cache()
            _update_ctx_note()
            # Notify if selection exceeds model max
            model_max = get_model_max_context()
            if model_max is not None and e.value > model_max:
                max_lbl = CONTEXT_SIZE_LABELS.get(model_max, f"{model_max:,}")
                usr_lbl = CONTEXT_SIZE_LABELS.get(e.value, f"{e.value:,}")
                ui.notify(
                    f"Context capped: model max is {max_lbl} (you selected {usr_lbl}). "
                    f"Trimming will use {max_lbl}.",
                    type="warning", close_button=True, timeout=8000,
                )

        ui.select(
            label="Context window size",
            options=ctx_opts,
            value=state.context_size,
            on_change=_on_ctx_change,
        ).classes("w-full").tooltip("How many tokens the model can process at once. If this exceeds the model's native max, trimming will use the model's actual limit. Default: 32K.")

        _update_ctx_note()
        _ctx_note_updater[0] = _update_ctx_note

        ui.separator()
        ui.label("👁️ Vision Model").classes("text-h6")
        ui.label(
            "The model used for camera and screen capture analysis — reading text, "
            "identifying objects, capturing screenshots, and answering visual questions. "
            "Runs as a separate lightweight model alongside the brain."
        ).classes("text-grey-6 text-sm")

        vsvc = state.vision_service
        all_vision = sorted(set(POPULAR_VISION_MODELS + ([vsvc.model] if vsvc.model not in POPULAR_VISION_MODELS else [])))
        vision_opts = {m: f"{'✅' if m in local else '⬇️'}  {m}" for m in all_vision}

        vision_select = ui.select(options=vision_opts, value=vsvc.model).classes("w-full")

        # Download button for vision model
        vision_dl_btn = ui.button(f"⬇️ Download {vsvc.model}").props("color=primary outline")
        vision_dl_btn.visible = vsvc.model not in local

        async def _download_vision(e=None):
            sel = vision_select.value
            if is_model_local(sel):
                ui.notify(f"✅ {sel} is already downloaded.", type="info")
                vision_dl_btn.visible = False
                return
            vision_dl_btn.disable()
            n = ui.notification(f"Downloading {sel}…", type="ongoing", spinner=True, timeout=None)
            await run.io_bound(lambda: list(pull_model(sel)))
            n.dismiss()
            ui.notify(f"✅ {sel} ready!", type="positive")
            vision_dl_btn.visible = False
            vision_dl_btn.enable()
            refreshed_local = list_local_models()
            vision_select.options = {m: f"{'✅' if m in refreshed_local else '⬇️'}  {m}" for m in all_vision}
            vision_select.update()
            # Apply the newly downloaded model
            vsvc.model = sel
            clear_agent_cache()

        vision_dl_btn.on_click(_download_vision)

        async def _on_vision_change(e):
            sel = e.value
            vision_dl_btn.text = f"⬇️ Download {sel}"
            vision_dl_btn.visible = not is_model_local(sel)
            if sel != vsvc.model:
                # If the model isn't downloaded yet, don't switch — wait for the Download button
                if not is_model_local(sel):
                    return
                vsvc.model = sel
                clear_agent_cache()

        vision_select.on_value_change(_on_vision_change)

        # Camera
        cameras = list_cameras()
        if cameras:
            cam_opts = {i: f"Camera {i}" for i in cameras}
            ui.select(label="Camera", options=cam_opts, value=vsvc.camera_index,
                      on_change=lambda e: setattr(vsvc, "camera_index", e.value)).classes("w-full")
        else:
            ui.label("No cameras detected.").classes("text-grey-6 text-sm")

        ui.switch("Enable vision", value=vsvc.enabled,
                  on_change=lambda e: setattr(vsvc, "enabled", e.value)
        ).tooltip("Allow the agent to capture images from your webcam.")

    def _build_tools_tab() -> None:
        ui.label("🔍 Search & Knowledge Tools").classes("text-h6")
        ui.label(
            "Enable or disable search and knowledge tools. "
            "Each tool gives the agent a different way to look up information — "
            "web search, Wikipedia, arXiv, YouTube, etc. "
            "Tools that require an API key will show a key input when enabled. "
            "Disabling unused tools improves accuracy by reducing the number of "
            "choices the model has to reason about."
        ).classes("text-grey-6 text-sm")
        ui.separator()

        skip_tools = {
            "filesystem", "shell", "gmail", "documents", "calendar", "timer",
            "url_reader", "calculator", "weather", "vision", "chart",
            "system_info", "conversation_search", "memory", "tracker",
            "browser",
        }
        for tool in tool_registry.get_all_tools():
            if tool.name in skip_tools:
                continue
            _build_tool_toggle(tool)
            ui.separator()

    def _build_tool_toggle(tool) -> None:
        """Render a toggle + API key inputs + config for one tool."""
        ui.switch(
            tool.display_name,
            value=tool_registry.is_enabled(tool.name),
            on_change=lambda e, n=tool.name: tool_registry.set_enabled(n, e.value),
        ).tooltip(tool.description)

        # Setup instructions
        if tool.name == "web_search":
            with ui.expansion("📋 Tavily Setup Instructions"):
                ui.markdown(
                    "1. Go to [app.tavily.com](https://app.tavily.com/) and sign up.\n"
                    "2. Create an API key (Development = 1,000 free searches/month).\n"
                    "3. Paste the key below.",
                    extras=['code-friendly', 'fenced-code-blocks', 'tables'],
                )
        elif tool.name == "wolfram_alpha":
            with ui.expansion("📋 Wolfram Alpha Setup Instructions"):
                ui.markdown(
                    "1. Go to [developer.wolframalpha.com](https://developer.wolframalpha.com/) and sign up.\n"
                    "2. Click **Get an AppID** and create an app.\n"
                    "3. Paste the AppID below.",
                    extras=['code-friendly', 'fenced-code-blocks', 'tables'],
                )

        # API keys
        if tool.required_api_keys:
            for label, env_var in tool.required_api_keys.items():
                current_val = get_key(env_var)
                ui.input(
                    label, value=current_val, password=True, password_toggle_button=True,
                    on_change=lambda e, ev=env_var: set_key(ev, e.value),
                ).classes("w-full")

        # Config schema
        schema = tool.config_schema
        if schema:
            for cfg_key, spec in schema.items():
                cfg_type = spec.get("type", "text")
                cfg_label = spec.get("label", cfg_key)
                cfg_default = spec.get("default")
                current_cfg = tool.get_config(cfg_key, cfg_default)
                if cfg_type == "text":
                    ui.input(
                        cfg_label, value=current_cfg or "",
                        on_change=lambda e, t=tool, k=cfg_key: t.set_config(k, e.value),
                    ).classes("w-full")
                elif cfg_type == "multicheck":
                    options = spec.get("options", [])
                    current_list = current_cfg if isinstance(current_cfg, list) else (cfg_default or [])
                    ui.label(cfg_label).classes("text-sm font-bold mt-2")
                    for opt in options:
                        ui.checkbox(
                            opt, value=opt in current_list,
                            on_change=lambda e, t=tool, k=cfg_key, o=opt, cl=current_list: (
                                cl.append(o) if e.value and o not in cl else (cl.remove(o) if not e.value and o in cl else None),
                                t.set_config(k, list(cl)),
                            ),
                        )

    def _build_ops_checkboxes(groups, current_ops, tool, cfg_key="selected_operations"):
        """Render grouped operation checkboxes."""
        ui.label("Allowed operations").classes("text-sm font-bold mt-2")
        selected = list(current_ops)

        def _toggle(op, val):
            if val and op not in selected:
                selected.append(op)
            elif not val and op in selected:
                selected.remove(op)
            tool.set_config(cfg_key, list(selected))

        with ui.row().classes("w-full gap-8"):
            for header, ops in groups:
                with ui.column():
                    ui.label(header).classes("font-bold text-sm")
                    for op in ops:
                        ui.checkbox(op, value=op in current_ops,
                                    on_change=lambda e, o=op: _toggle(o, e.value))

    def _build_system_access_tab() -> None:
        from tools.filesystem_tool import _SAFE_OPS, _WRITE_OPS, _DESTRUCTIVE_OPS

        ui.label("🖥️ System Access").classes("text-h6")
        ui.label(
            "Give Thoth access to your local system — shell commands and "
            "file operations. Destructive actions always require your "
            "explicit approval before execution."
        ).classes("text-grey-6 text-sm")

        fs_tool = tool_registry.get_tool("filesystem")
        if not fs_tool:
            ui.label("Filesystem tool not found.").classes("text-negative")
            return

        # ── Shared workspace folder ──────────────────────────────────────
        ui.separator()
        ui.label("📂 Workspace Folder").classes("text-subtitle1 font-bold")
        ui.label(
            "The Filesystem tool is sandboxed to this folder and cannot access "
            "anything outside it. The Shell tool uses this as its starting "
            "directory but can navigate elsewhere."
        ).classes("text-grey-6 text-xs")

        fs_root_default = fs_tool.config_schema.get("workspace_root", {}).get("default", "")
        current_root = fs_tool.get_config("workspace_root", fs_root_default)
        root_input = ui.input(
            "Workspace folder", value=current_root or "",
            on_change=lambda e: fs_tool.set_config("workspace_root", e.value),
        ).classes("w-full")

        async def _browse_ws():
            folder = await _browse_folder("Select Workspace folder", current_root)
            if folder:
                root_input.value = folder
                fs_tool.set_config("workspace_root", folder)

        ui.button("Browse…", on_click=_browse_ws).props("flat dense")

        if current_root and not os.path.isdir(current_root):
            ui.label(f"⚠️ Folder not found: {current_root}").classes("text-warning text-sm")

        # ── Shell Access ─────────────────────────────────────────────────
        ui.separator()
        ui.label("🖥️ Shell Access").classes("text-subtitle1 font-bold")
        ui.label(
            "Run shell commands directly on your system "
            "(PowerShell on Windows, bash on macOS/Linux). "
            "Read-only commands like ls, pwd, and git status run "
            "automatically. Anything that modifies files, installs "
            "software, or changes system state requires your "
            "approval first. Commands time out after 120\u202fs."
        ).classes("text-grey-6 text-xs")

        shell_tool = tool_registry.get_tool("shell")
        if shell_tool:
            ui.switch(
                "Enable Shell tool",
                value=tool_registry.is_enabled("shell"),
                on_change=lambda e: tool_registry.set_enabled("shell", e.value),
            ).tooltip(shell_tool.description)

            shell_blocked = shell_tool.get_config("blocked_commands", "")
            ui.input(
                "Additional blocked patterns (comma-separated)",
                value=shell_blocked or "",
                on_change=lambda e: shell_tool.set_config("blocked_commands", e.value),
            ).classes("w-full").tooltip(
                "Commands matching these patterns will always be refused. "
                "Separate multiple patterns with commas."
            )
        else:
            ui.label("Shell tool not found.").classes("text-grey-6 text-sm")

        # ── Browser Automation ───────────────────────────────────────────
        ui.separator()
        ui.label("🌐 Browser Automation").classes("text-subtitle1 font-bold")
        ui.label(
            "Open a real browser window that you and the agent share. "
            "The agent can navigate, click, fill forms, and read pages. "
            "You can jump in to type passwords or solve CAPTCHAs. "
            "Uses a persistent profile so logins survive between sessions."
        ).classes("text-grey-6 text-xs")
        ui.label(
            "\u26A0\uFE0F Browser automation relies on page structure, which varies "
            "across sites \u2014 the agent may occasionally misclick or miss "
            "elements. Review important actions before relying on the result."
        ).classes("text-amber-6 text-xs mt-1")

        browser_tool = tool_registry.get_tool("browser")
        if browser_tool:
            ui.switch(
                "Enable Browser tool",
                value=tool_registry.is_enabled("browser"),
                on_change=lambda e: tool_registry.set_enabled("browser", e.value),
            ).tooltip(browser_tool.description)
        else:
            ui.label("Browser tool not found.").classes("text-grey-6 text-sm")

        # ── File Operations ──────────────────────────────────────────────
        ui.separator()
        ui.label("📁 File Operations").classes("text-subtitle1 font-bold")
        ui.label(
            "Read, write, search, copy, move, and delete files within "
            "the workspace folder above. "
            "Structured data (CSV, Excel, JSON) is parsed with pandas."
        ).classes("text-grey-6 text-xs")

        ui.switch(
            "Enable Filesystem tool",
            value=tool_registry.is_enabled("filesystem"),
            on_change=lambda e: tool_registry.set_enabled("filesystem", e.value),
        ).tooltip(fs_tool.description)

        ops_default = fs_tool.config_schema.get("selected_operations", {}).get("default", [])
        current_ops = fs_tool.get_config("selected_operations", ops_default)
        if not isinstance(current_ops, list):
            current_ops = ops_default
        _build_ops_checkboxes(
            [("Read-only", _SAFE_OPS), ("Write", _WRITE_OPS), ("⚠️ Destructive", _DESTRUCTIVE_OPS)],
            current_ops, fs_tool,
        )

    def _build_gmail_tab() -> None:
        gmail_tool = tool_registry.get_tool("gmail")
        if not gmail_tool:
            ui.label("Gmail tool not found.").classes("text-negative")
            return

        ui.label("📧 Gmail Integration").classes("text-h6")
        ui.label(
            "Connect Thoth to your Gmail account to search emails, read messages, "
            "view threads, create drafts, and send emails — all through natural language. "
            "Sending emails requires explicit confirmation and is disabled by default. "
            "Your OAuth credentials are stored locally and never leave your machine. "
            "Uses the same Google Cloud credentials as Calendar."
        ).classes("text-grey-6 text-sm")

        ui.switch(
            "Enable Gmail tool",
            value=tool_registry.is_enabled("gmail"),
            on_change=lambda e: tool_registry.set_enabled("gmail", e.value),
        ).tooltip(gmail_tool.description)

        with ui.expansion("📋 Setup Instructions"):
            ui.markdown(
                "1. Go to [Google Cloud Console](https://console.cloud.google.com) → New Project\n"
                "2. Enable **Gmail API** in APIs & Services → Library\n"
                "3. Create OAuth client ID (Desktop app) in Credentials\n"
                "4. Add your account as test user if using External OAuth\n"
                "5. Download credentials.json and point path below to it\n"
                "6. Click Authenticate — one-time browser login",
                extras=['code-friendly', 'fenced-code-blocks', 'tables'],
            )

        ui.separator()

        creds_default = gmail_tool.config_schema.get("credentials_path", {}).get("default", "")
        current_creds = gmail_tool.get_config("credentials_path", creds_default)
        creds_input = ui.input(
            "credentials.json path", value=current_creds or "",
            on_change=lambda e: gmail_tool.set_config("credentials_path", e.value),
        ).classes("w-full")

        async def _browse_creds():
            path = await _browse_file(
                "Select credentials.json",
                os.path.dirname(current_creds) if current_creds else "",
                [("JSON files", "*.json")],
            )
            if path:
                creds_input.value = path
                gmail_tool.set_config("credentials_path", path)

        ui.button("Browse…", on_click=_browse_creds).props("flat dense")
        ui.separator()

        if gmail_tool.has_credentials_file():
            if gmail_tool.is_authenticated():
                ui.label("✅ Authenticated with Gmail").classes("text-positive")

                async def _reauth_gmail():
                    """Delete stale token and redo the OAuth flow."""
                    try:
                        token_path = gmail_tool._get_token_path()
                        if os.path.isfile(token_path):
                            os.remove(token_path)
                        await run.io_bound(gmail_tool.authenticate)
                        clear_agent_cache()  # rebuild agent with fresh Gmail tools
                        ui.notify("✅ Gmail re-authenticated!", type="positive")
                        p.settings_dlg.close()
                        _open_settings()
                    except Exception as e:
                        ui.notify(f"Auth failed: {e}", type="negative")

                ui.button(
                    "🔄 Re-authenticate", on_click=_reauth_gmail,
                ).props("flat dense").tooltip(
                    "Use this if your Gmail token has expired or been revoked"
                )
                ui.label(
                    "Google OAuth tokens expire after ~7 days if your Cloud project is in Testing mode. "
                    "To avoid this, publish your OAuth app in Google Cloud Console (Internal or External). "
                    "If your token expires, click Re-authenticate above."
                ).classes("text-grey-6 text-xs mt-1")
            else:
                ui.label("🔑 Credentials found but not authenticated.").classes("text-warning")

                async def _auth_gmail():
                    try:
                        await run.io_bound(gmail_tool.authenticate)
                        clear_agent_cache()  # rebuild agent with Gmail tools
                        ui.notify("✅ Gmail authenticated!", type="positive")
                        p.settings_dlg.close()
                        _open_settings()
                    except Exception as e:
                        ui.notify(f"Auth failed: {e}", type="negative")

                ui.button("Authenticate…", on_click=_auth_gmail)
        else:
            ui.label("Point path above to your credentials.json").classes("text-grey-6 text-sm")

        ui.separator()
        from tools.gmail_tool import _READ_OPS, _COMPOSE_OPS, _SEND_OPS
        ops_default = gmail_tool.config_schema.get("selected_operations", {}).get("default", [])
        current_ops = gmail_tool.get_config("selected_operations", ops_default)
        if not isinstance(current_ops, list):
            current_ops = ops_default
        _build_ops_checkboxes(
            [("Read", _READ_OPS), ("Compose", _COMPOSE_OPS), ("⚠️ Send", _SEND_OPS)],
            current_ops, gmail_tool,
        )

    def _build_calendar_tab() -> None:
        cal_tool = tool_registry.get_tool("calendar")
        if not cal_tool:
            ui.label("Calendar tool not found.").classes("text-negative")
            return

        ui.label("📅 Google Calendar").classes("text-h6")
        ui.label(
            "Connect Thoth to Google Calendar to search events, create reminders, "
            "and manage your schedule through natural language. "
            "Destructive actions (move, delete events) require explicit confirmation "
            "and are disabled by default. "
            "Your OAuth credentials are stored locally and never leave your machine. "
            "Uses the same Google Cloud credentials as Gmail."
        ).classes("text-grey-6 text-sm")

        ui.switch(
            "Enable Calendar tool",
            value=tool_registry.is_enabled("calendar"),
            on_change=lambda e: tool_registry.set_enabled("calendar", e.value),
        ).tooltip(cal_tool.description)

        with ui.expansion("📋 Setup Instructions"):
            ui.markdown(
                "Uses the same Google Cloud credentials as Gmail.\n\n"
                "1. Enable **Google Calendar API** in your project\n"
                "2. Add your account as a test user if using External OAuth\n"
                "3. Point credentials path below to the same credentials.json\n"
                "4. Click Authenticate — one-time browser login",
                extras=['code-friendly', 'fenced-code-blocks', 'tables'],
            )

        ui.separator()

        cal_creds_default = cal_tool.config_schema.get("credentials_path", {}).get("default", "")
        current_cal_creds = cal_tool.get_config("credentials_path", cal_creds_default)
        cal_creds_input = ui.input(
            "credentials.json path", value=current_cal_creds or "",
            on_change=lambda e: cal_tool.set_config("credentials_path", e.value),
        ).classes("w-full")

        async def _browse_cal_creds():
            path = await _browse_file(
                "Select credentials.json",
                os.path.dirname(current_cal_creds) if current_cal_creds else "",
                [("JSON files", "*.json")],
            )
            if path:
                cal_creds_input.value = path
                cal_tool.set_config("credentials_path", path)

        ui.button("Browse…", on_click=_browse_cal_creds).props("flat dense")
        ui.separator()

        if cal_tool.has_credentials_file():
            if cal_tool.is_authenticated():
                ui.label("✅ Authenticated with Calendar").classes("text-positive")

                async def _reauth_cal():
                    """Delete stale token and redo the OAuth flow."""
                    try:
                        token_path = cal_tool._get_token_path()
                        if os.path.isfile(token_path):
                            os.remove(token_path)
                        await run.io_bound(cal_tool.authenticate)
                        clear_agent_cache()
                        ui.notify("✅ Calendar re-authenticated!", type="positive")
                        p.settings_dlg.close()
                        _open_settings()
                    except Exception as e:
                        ui.notify(f"Auth failed: {e}", type="negative")

                ui.button(
                    "🔄 Re-authenticate", on_click=_reauth_cal,
                ).props("flat dense").tooltip(
                    "Use this if your Calendar token has expired or been revoked"
                )
                ui.label(
                    "Google OAuth tokens expire after ~7 days if your Cloud project is in Testing mode. "
                    "To avoid this, publish your OAuth app in Google Cloud Console (Internal or External). "
                    "If your token expires, click Re-authenticate above."
                ).classes("text-grey-6 text-xs mt-1")
            else:
                ui.label("🔑 Credentials found but not authenticated.").classes("text-warning")

                async def _auth_cal():
                    try:
                        await run.io_bound(cal_tool.authenticate)
                        clear_agent_cache()
                        ui.notify("✅ Calendar authenticated!", type="positive")
                        p.settings_dlg.close()
                        _open_settings()
                    except Exception as e:
                        ui.notify(f"Auth failed: {e}", type="negative")

                ui.button("Authenticate…", on_click=_auth_cal)
        else:
            ui.label("Point path above to your credentials.json").classes("text-grey-6 text-sm")

        ui.separator()
        from tools.calendar_tool import (
            _READ_OPS as CAL_READ_OPS,
            _WRITE_OPS as CAL_WRITE_OPS,
            _DESTRUCTIVE_OPS as CAL_DESTRUCTIVE_OPS,
        )
        cal_ops_default = cal_tool.config_schema.get("selected_operations", {}).get("default", [])
        current_cal_ops = cal_tool.get_config("selected_operations", cal_ops_default)
        if not isinstance(current_cal_ops, list):
            current_cal_ops = cal_ops_default
        _build_ops_checkboxes(
            [("Read", CAL_READ_OPS), ("Write", CAL_WRITE_OPS), ("⚠️ Destructive", CAL_DESTRUCTIVE_OPS)],
            current_cal_ops, cal_tool,
        )

    def _build_utilities_tab() -> None:
        ui.label("🔧 Utility Tools").classes("text-h6")
        ui.label(
            "Lightweight productivity tools that extend Thoth's capabilities beyond "
            "search and knowledge retrieval — things like setting reminders, "
            "reading web pages, and other everyday tasks."
        ).classes("text-grey-6 text-sm")
        ui.separator()
        util_names = ["timer", "url_reader", "calculator", "weather", "chart", "system_info", "conversation_search"]
        for uname in util_names:
            utool = tool_registry.get_tool(uname)
            if utool is None:
                continue
            ui.switch(
                utool.display_name,
                value=tool_registry.is_enabled(uname),
                on_change=lambda e, n=uname: tool_registry.set_enabled(n, e.value),
            ).tooltip(utool.description)
            ui.separator()

    def _build_tracker_tab() -> None:
        from tools.tracker_tool import _get_db, _get_all_trackers, _DB_PATH

        ui.label("\U0001f4cb Habit & Health Tracker").classes("text-h6")
        ui.label(
            "Track recurring activities, habits, symptoms, medications, and health events. "
            "Log entries via chat, view history, compute streaks and adherence, "
            "and analyse trends over time. All data is stored locally in a "
            "SQLite database and never sent to any cloud service."
        ).classes("text-grey-6 text-sm")

        tracker_tool = tool_registry.get_tool("tracker")
        if not tracker_tool:
            ui.label("Tracker tool not found.").classes("text-negative")
            return

        ui.switch(
            "Enable Habit Tracker",
            value=tool_registry.is_enabled("tracker"),
            on_change=lambda e: tool_registry.set_enabled("tracker", e.value),
        ).tooltip(tracker_tool.description)

        ui.separator()

        # Show active trackers from the database
        try:
            conn = _get_db()
            trackers = _get_all_trackers(conn)
            total_entries = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
            conn.close()
        except Exception:
            trackers = []
            total_entries = 0

        ui.label(f"Active trackers: {len(trackers)}  \u00b7  Total entries: {total_entries}").classes("font-bold")

        if trackers:
            tracker_container = ui.column().classes("w-full")

            def _refresh_trackers():
                tracker_container.clear()
                try:
                    c = _get_db()
                    tlist = _get_all_trackers(c)
                    with tracker_container:
                        if not tlist:
                            ui.label("No trackers yet. Start tracking by chatting with Thoth!").classes("text-grey-6")
                        else:
                            for t in tlist:
                                entry_count = c.execute(
                                    "SELECT COUNT(*) FROM entries WHERE tracker_id = ?",
                                    (t["id"],),
                                ).fetchone()[0]
                                last_entry = c.execute(
                                    "SELECT timestamp FROM entries WHERE tracker_id = ? ORDER BY timestamp DESC LIMIT 1",
                                    (t["id"],),
                                ).fetchone()
                                last_str = last_entry[0][:10] if last_entry else "never"
                                type_badge = t["type"]
                                if t.get("unit"):
                                    type_badge += f" ({t['unit']})"
                                with ui.row().classes("w-full items-center gap-2"):
                                    ui.label(f"\u25cf {t['name']}").classes("font-bold")
                                    ui.badge(type_badge).props("outline")
                                    ui.label(f"{entry_count} entries \u00b7 last: {last_str}").classes(
                                        "text-xs text-grey-6"
                                    )
                                ui.separator()
                    c.close()
                except Exception as exc:
                    with tracker_container:
                        ui.label(f"Error loading trackers: {exc}").classes("text-negative")

            _refresh_trackers()

            ui.separator()

            async def _delete_all_tracker_data():
                confirm = await ui.run_javascript(
                    "confirm('Delete ALL tracker data? This cannot be undone.')"
                )
                if confirm:
                    try:
                        import os
                        c = _get_db()
                        c.execute("DELETE FROM entries")
                        c.execute("DELETE FROM trackers")
                        c.commit()
                        c.close()
                        ui.notify("All tracker data deleted.", type="info")
                        _refresh_trackers()
                    except Exception as exc:
                        ui.notify(f"Error: {exc}", type="negative")

            ui.button(
                "\U0001f5d1\ufe0f Delete All Tracker Data",
                on_click=_delete_all_tracker_data,
            ).props("flat dense color=negative")
        else:
            ui.label("No trackers yet. Start tracking by chatting with Thoth!").classes("text-grey-6 mt-2")

    def _build_memory_tab() -> None:
        ui.label("🧠 Memory").classes("text-h6")
        ui.label(
            "Thoth can remember personal details you share across conversations — "
            "names, birthdays, preferences, important facts, and more. Memories "
            "are stored locally and never sent to any cloud service. "
            "The agent saves memories automatically when you share something "
            "worth remembering. Memories are searchable via semantic similarity, "
            "so the agent can find relevant memories even when you don't use "
            "the exact same words. You can also ask it to recall, update, or "
            "forget specific memories in chat."
        ).classes("text-grey-6 text-sm")

        mem_tool = tool_registry.get_tool("memory")
        if mem_tool:
            ui.switch(
                "Enable Memory",
                value=tool_registry.is_enabled("memory"),
                on_change=lambda e: tool_registry.set_enabled("memory", e.value),
            ).tooltip("When enabled, the agent can save and recall long-term memories.")

        ui.separator()
        total = memory_db.count_memories()
        ui.label(f"Stored memories: {total}").classes("font-bold")

        if total > 0:
            cat_options = ["All"] + sorted(memory_db.VALID_CATEGORIES)
            cat_sel = ui.select(label="Filter by category", options=cat_options, value="All").classes("w-full")
            search_input = ui.input("Search memories", placeholder="Type a keyword…").classes("w-full")
            mem_container = ui.column().classes("w-full")

            def _refresh_memories():
                mem_container.clear()
                cat = None if cat_sel.value == "All" else cat_sel.value
                q = search_input.value
                if q:
                    memories = memory_db.search_memories(q, category=cat)
                else:
                    memories = memory_db.list_memories(category=cat)
                with mem_container:
                    if not memories:
                        ui.label("No matching memories.").classes("text-grey-6")
                    else:
                        for mem in memories:
                            with ui.expansion(f"**{mem['subject']}** — _{mem['category']}_").classes("w-full"):
                                ui.markdown(mem["content"], extras=['code-friendly', 'fenced-code-blocks', 'tables'])
                                tags = mem.get("tags", "")
                                if tags:
                                    ui.label(f"Tags: {tags}").classes("text-xs text-grey-6")
                                ui.label(
                                    f"ID: {mem['id']} · Created: {mem['created_at'][:16]} · Updated: {mem['updated_at'][:16]}"
                                ).classes("text-xs text-grey-6")

                                def _del_mem(mid=mem["id"]):
                                    memory_db.delete_memory(mid)
                                    ui.notify("Memory deleted.", type="info")
                                    _refresh_memories()

                                ui.button("🗑️ Delete", on_click=_del_mem).props("flat dense color=negative")

            cat_sel.on("update:model-value", lambda _: _refresh_memories())
            search_input.on("update:model-value", lambda _: _refresh_memories())
            _refresh_memories()

            ui.separator()

            def _delete_all_memories():
                memory_db.delete_all_memories()
                ui.notify("All memories deleted.", type="info")
                p.settings_dlg.close()
                _open_settings()

            with ui.row().classes("w-full"):
                ui.button("🗑️ Delete all memories", on_click=_delete_all_memories).props("flat color=negative")

    def _build_workflows_tab() -> None:
        ui.label("⚡ Workflows").classes("text-h6")
        ui.label(
            "Create reusable prompt workflows — multi-step sequences that run in a fresh "
            "conversation thread. Each step sees the output of the previous one, so you can "
            "chain research → summarise → action. "
            "Workflows always run in the background — results appear as a conversation "
            "in the sidebar and trigger a desktop notification when complete. "
            "Destructive operations (file delete, send email, etc.) are automatically excluded "
            "from background workflow runs. "
            "Template variables: {{date}}, {{day}}, {{time}}, {{month}}, {{year}} — "
            "replaced at runtime. Set a workflow to run on a daily or weekly schedule."
        ).classes("text-grey-6 text-sm")

        wf_container = ui.column().classes("w-full")

        def _refresh_wf():
            wf_container.clear()
            wf_list = list_workflows()
            with wf_container:
                if not wf_list:
                    ui.label("No workflows yet.").classes("text-grey-6")
                for wf in wf_list:
                    _build_single_workflow(wf, _refresh_wf)

        def _create_new():
            create_workflow(name="New Workflow", prompts=[""], description="", icon="⚡")
            _refresh_wf()

        ui.button("＋ New Workflow", on_click=_create_new).classes("w-full")
        _refresh_wf()

    def _build_single_workflow(wf: dict, refresh_fn) -> None:
        """Build the editor for a single workflow inside an expansion."""
        with ui.expansion(f"{wf['icon']} {wf['name']}").classes("w-full"):
            name_input = ui.input("Name", value=wf["name"]).classes("w-full")
            # Ensure the workflow's current icon is in the options list
            _wf_icon_opts = list(_ICON_OPTIONS)
            if wf["icon"] not in _wf_icon_opts:
                _wf_icon_opts.insert(0, wf["icon"])
            icon_sel = ui.select(label="Icon", options=_wf_icon_opts, value=wf["icon"]).classes("w-32")
            desc_input = ui.input("Description", value=wf.get("description") or "").classes("w-full")

            ui.label("Prompts (executed in order)").classes("font-bold text-sm mt-2")
            prompts_data = list(wf["prompts"])
            prompt_inputs: list[ui.textarea] = []
            prompt_container = ui.column().classes("w-full")

            def _rebuild_prompts():
                # Save current values
                for i, ta in enumerate(prompt_inputs):
                    if i < len(prompts_data):
                        prompts_data[i] = ta.value
                prompt_container.clear()
                prompt_inputs.clear()
                with prompt_container:
                    for i, p_text in enumerate(prompts_data):
                        with ui.row().classes("w-full items-start gap-1"):
                            ta = ui.textarea(f"Step {i+1}", value=p_text).classes("flex-grow")
                            prompt_inputs.append(ta)
                            if len(prompts_data) > 1:
                                def _remove(idx=i):
                                    for j, _ta in enumerate(prompt_inputs):
                                        if j < len(prompts_data):
                                            prompts_data[j] = _ta.value
                                    prompts_data.pop(idx)
                                    _rebuild_prompts()
                                ui.button(icon="close", on_click=_remove).props("flat dense round")

                    def _add():
                        for j, _ta in enumerate(prompt_inputs):
                            if j < len(prompts_data):
                                prompts_data[j] = _ta.value
                        prompts_data.append("")
                        _rebuild_prompts()

                    ui.button("＋ Add step", on_click=_add).props("flat dense")

            _rebuild_prompts()

            # Schedule
            sched_options = ["Manual only", "Daily", "Weekly"]
            current_sched = wf.get("schedule") or ""
            if current_sched.startswith("daily"):
                sched_idx = "Daily"
            elif current_sched.startswith("weekly"):
                sched_idx = "Weekly"
            else:
                sched_idx = "Manual only"

            # Parse existing time / day from schedule string
            _sched_time = "08:00"
            _sched_day = "mon"
            if current_sched.startswith("daily:"):
                _sched_time = current_sched.split(":", 1)[1]  # "08:00"
            elif current_sched.startswith("weekly:"):
                parts = current_sched.split(":")
                if len(parts) >= 3:
                    _sched_day = parts[1]
                    _sched_time = f"{parts[2]}:{parts[3]}" if len(parts) >= 4 else "08:00"

            day_options = {
                "mon": "Monday",
                "tue": "Tuesday",
                "wed": "Wednesday",
                "thu": "Thursday",
                "fri": "Friday",
                "sat": "Saturday",
                "sun": "Sunday",
            }

            with ui.row().classes("items-center gap-2"):
                sched_sel = ui.select(label="Schedule", options=sched_options, value=sched_idx).classes("w-48")

                sched_time_input = ui.input(label="Time", value=_sched_time).classes("w-28").props('mask="##:##" placeholder="HH:MM"')
                sched_time_input.visible = sched_idx in ("Daily", "Weekly")

                sched_day_sel = ui.select(label="Day", options=day_options, value=_sched_day).classes("w-36")
                sched_day_sel.visible = sched_idx == "Weekly"

            def _on_sched_change(e):
                sched_time_input.visible = e.value in ("Daily", "Weekly")
                sched_day_sel.visible = e.value == "Weekly"

            sched_sel.on_value_change(_on_sched_change)

            # Last run
            if wf.get("last_run"):
                try:
                    lr = datetime.fromisoformat(wf["last_run"])
                    ui.label(f"Last run: {lr.strftime('%b %d, %Y at %I:%M %p')}").classes("text-xs text-grey-6")
                except (ValueError, TypeError):
                    pass

            # Action buttons
            with ui.row().classes("w-full gap-2 mt-2"):
                def _save():
                    for j, _ta in enumerate(prompt_inputs):
                        if j < len(prompts_data):
                            prompts_data[j] = _ta.value
                    updates = {}
                    if name_input.value != wf["name"]:
                        updates["name"] = name_input.value
                    if icon_sel.value != wf["icon"]:
                        updates["icon"] = icon_sel.value
                    if desc_input.value != (wf.get("description") or ""):
                        updates["description"] = desc_input.value
                    clean_prompts = [p for p in prompts_data if p.strip()]
                    if clean_prompts != wf["prompts"]:
                        updates["prompts"] = clean_prompts

                    # Schedule
                    sv = sched_sel.value
                    final_schedule = None
                    if sv == "Daily":
                        t = sched_time_input.value.strip() or "08:00"
                        final_schedule = f"daily:{t}"
                    elif sv == "Weekly":
                        t = sched_time_input.value.strip() or "08:00"
                        d = sched_day_sel.value or "mon"
                        final_schedule = f"weekly:{d}:{t}"
                    if final_schedule != wf.get("schedule"):
                        updates["schedule"] = final_schedule

                    if updates:
                        update_workflow(wf["id"], **updates)
                        ui.notify("💾 Saved", type="positive")
                        refresh_fn()
                    else:
                        ui.notify("No changes.", type="info")

                ui.button("💾 Save", on_click=_save)

                def _dup():
                    duplicate_workflow(wf["id"])
                    refresh_fn()

                ui.button("📋 Duplicate", on_click=_dup).props("flat")

                def _del():
                    delete_workflow(wf["id"])
                    refresh_fn()

                ui.button("🗑️ Delete", on_click=_del).props("flat color=negative")

            # Run history
            runs = get_run_history(wf["id"], limit=3)
            if runs:
                with ui.expansion("📜 Recent runs"):
                    for r in runs:
                        icon = "✅" if r["status"] == "completed" else ("🔄" if r["status"] == "running" else "❌")
                        started = datetime.fromisoformat(r["started_at"]).strftime("%b %d, %I:%M %p")
                        ui.label(f"{icon} {started} — {r['steps_done']}/{r['steps_total']} steps").classes("text-xs")

    def _build_voice_tab() -> None:
        ui.label("🎤 Voice Input").classes("text-h6")
        ui.label(
            "Talk to Thoth hands-free using voice input. "
            "Toggle 🎤 Voice in the chat area to start listening. "
            "Thoth continuously listens and transcribes your speech "
            "using a local Whisper model. The mic is automatically muted while "
            "Thoth speaks and resumes when it finishes. "
            "Everything runs locally — no audio is sent to the cloud. "
            "Requires a working microphone connected to this computer."
        ).classes("text-grey-6 text-sm")

        voice_svc = state.voice_service

        whisper_sizes = get_available_whisper_sizes()
        whisper_labels = {
            "tiny": "Tiny (~39 MB, fastest)", "base": "Base (~74 MB, balanced)",
            "small": "Small (~244 MB, accurate)", "medium": "Medium (~769 MB, best accuracy)",
        }
        whisper_opts = {s: whisper_labels.get(s, s) for s in whisper_sizes}
        ui.select(
            label="Whisper model size", options=whisper_opts,
            value=voice_svc.whisper_size,
            on_change=lambda e: setattr(voice_svc, "whisper_size", e.value),
        ).classes("w-full").tooltip("Larger models are more accurate but use more RAM and are slower. Downloaded on first use.")

        ui.separator()

        # ── TTS ──────────────────────────────────────────────────────────
        ui.label("🔊 Text-to-Speech").classes("text-h6")
        ui.label(
            "Enable text-to-speech to hear Thoth read responses aloud. "
            "When paired with voice input, this creates a fully hands-free experience. "
            "Short responses are read in full; longer responses are summarized aloud "
            "with the full text shown in the app. "
            "Uses Kokoro TTS — a fast, high-quality neural speech engine. "
            "Everything runs locally, no audio is sent to the cloud. "
            "Click Install Kokoro TTS below to download the model "
            "and voice pack (~200 MB total)."
        ).classes("text-grey-6 text-sm")

        tts = state.tts_service

        if not tts.is_installed():
            async def _install_kokoro():
                ui.notify("Downloading Kokoro TTS model & voices…", type="ongoing", timeout=0)
                await run.io_bound(tts.download_model)
                ui.notify("✅ Kokoro TTS installed!", type="positive")
                p.settings_dlg.close()
                _open_settings()

            ui.button("⬇️ Install Kokoro TTS", on_click=_install_kokoro).classes("w-full")
        else:
            ui.switch("Enable text-to-speech", value=tts.enabled,
                      on_change=lambda e: setattr(tts, "enabled", e.value))

            # Voice selector — all Kokoro voices are bundled
            voice_opts = {v: VOICE_CATALOG.get(v, v) for v in tts.get_installed_voices()}
            if voice_opts:
                ui.select(label="Voice", options=voice_opts, value=tts.voice,
                          on_change=lambda e: setattr(tts, "voice", e.value)).classes("w-full")

            ui.label("Speech speed").classes("text-sm")
            ui.slider(
                min=0.5, max=2.0, step=0.1, value=tts.speed,
                on_change=lambda e: setattr(tts, "speed", e.value),
            ).classes("w-full")

            ui.switch("Auto-speak voice responses", value=tts.auto_speak,
                      on_change=lambda e: setattr(tts, "auto_speak", e.value)
            ).tooltip("Automatically read responses aloud when using voice input.")

            def _test():
                tts.speak_now("Hello! I'm Thoth, your knowledgeable personal agent. How can I help you today?")

            ui.button("🔊 Test voice", on_click=_test).props("flat")

    # ── Channels tab ─────────────────────────────────────────────────────

    def _build_channels_tab() -> None:
        from api_keys import get_key, set_key
        from channels.telegram import is_configured as tg_configured, is_running as tg_running

        ui.label("📱 Messaging Channels").classes("text-h6")
        ui.label(
            "Connect Thoth to external messaging platforms so you can chat with "
            "your personal agent from your phone — even when the browser isn't open. "
            "Each channel gets its own conversation thread."
        ).classes("text-grey-6 text-sm")

        ui.separator()

        # ── Telegram ────────────────────────────────────────────────────
        ui.label("Telegram Bot").classes("text-h6")
        ui.label(
            "Chat with Thoth from Telegram using a personal bot. "
            "Uses long polling — no public URL or tunnel required. "
            "Only the authorised Telegram user ID can interact with the bot."
        ).classes("text-grey-6 text-sm")

        with ui.expansion("📖 Setup Guide", icon="help_outline").classes("w-full"):
            ui.markdown(
                "### Quick Setup\n"
                "1. Open Telegram and message [@BotFather](https://t.me/BotFather)\n"
                "2. Send `/newbot`, follow the prompts, and copy the **Bot Token**\n"
                "3. To find your **User ID**, message [@userinfobot](https://t.me/userinfobot) "
                "— it will reply with your numeric ID\n"
                "4. Paste both values below and click **Save**\n"
                "5. Click **▶️ Start Bot** to begin polling\n"
                "6. Open your bot in Telegram and send `/start`\n\n"
                "That's it! The bot runs locally — no cloud server or tunnel needed.\n\n"
                "**Commands available in Telegram:**\n"
                "- `/help` — Show available commands\n"
                "- `/newthread` — Start a fresh conversation\n"
                "- `/tools` — List enabled tools\n"
                "- `/status` — Check bot status\n\n"
                "**Security:** Only the user ID you enter below can interact with the bot. "
                "Anyone else who finds your bot will be rejected.",
                extras=['code-friendly', 'fenced-code-blocks', 'tables'],
            ).classes("text-sm")

        ui.separator()

        # Credential fields
        tg_token = get_key("TELEGRAM_BOT_TOKEN")
        tg_user_id = get_key("TELEGRAM_USER_ID")

        token_input = ui.input(
            label="Bot Token",
            value=tg_token,
            password=True,
            password_toggle_button=True,
        ).classes("w-full").tooltip("From @BotFather after creating your bot")

        user_id_input = ui.input(
            label="Your Telegram User ID",
            value=tg_user_id,
        ).classes("w-full").tooltip("Numeric ID from @userinfobot — only this user can interact with the bot")

        # Status indicator
        status_container = ui.row().classes("items-center gap-2 mt-2")
        _update_tg_status(status_container)

        def _save_tg_creds():
            set_key("TELEGRAM_BOT_TOKEN", token_input.value.strip())
            set_key("TELEGRAM_USER_ID", user_id_input.value.strip())
            _update_tg_status(status_container)
            ui.notify("Telegram credentials saved", type="positive")

        ui.button("💾 Save", on_click=_save_tg_creds).classes("mt-2")

        ui.separator()

        # Start / Stop controls
        ui.label("Bot Control").classes("text-subtitle2 mt-2")

        async def _start_tg():
            if not tg_configured():
                ui.notify("Please save your credentials first", type="warning")
                return
            try:
                ok = await _tg_start_bot()
                if ok:
                    _ch_config.set("telegram", "auto_start", True)
                    ui.notify("✅ Telegram bot started!", type="positive")
                else:
                    ui.notify("⚠️ Could not start — check credentials", type="warning")
            except Exception as exc:
                ui.notify(f"Error starting bot: {exc}", type="negative")
            _update_tg_status(status_container)

        async def _stop_tg():
            try:
                await _tg_stop_bot()
                _ch_config.set("telegram", "auto_start", False)
                ui.notify("Telegram bot stopped", type="info")
            except Exception as exc:
                ui.notify(f"Error stopping bot: {exc}", type="negative")
            _update_tg_status(status_container)

        with ui.row().classes("gap-2"):
            ui.button("▶️ Start Bot", on_click=_start_tg).props("color=positive")
            ui.button("⏹️ Stop Bot", on_click=_stop_tg).props("color=negative flat")

        ui.separator().classes("mt-6")

        # ── Email ────────────────────────────────────────────────────
        from channels.email import is_configured as email_configured, is_running as email_running

        ui.label("📧 Email Channel").classes("text-h6")
        ui.label(
            "Send an email to yourself with [Thoth] in the subject line to run a query. "
            "Replies arrive in the same email thread. Requires Gmail OAuth (set up via the Gmail tool)."
        ).classes("text-grey-6 text-sm")

        with ui.expansion("📖 Setup Guide", icon="help_outline").classes("w-full"):
            ui.markdown(
                "### Quick Setup\n"
                "1. First, enable the **Gmail** tool in the Tools tab and complete OAuth sign-in\n"
                "2. Come back here and click **▶️ Start Polling**\n"
                "3. Send yourself an email with `[Thoth]` in the subject line\n"
                "4. Write your query in the email body\n"
                "5. Thoth will reply in the same email thread\n\n"
                "**How it works:**\n"
                "- Polls Gmail every N seconds for unread emails with `[Thoth]` in the subject\n"
                "- Only processes emails **from your own address** (security filter)\n"
                "- Each email subject gets its own agent conversation thread\n"
                "- Tool approval requests are sent as email replies — just reply APPROVE or DENY\n\n"
                "**Tip:** You can adjust the poll interval below. Lower = faster responses but more API calls.",
                extras=['code-friendly', 'fenced-code-blocks', 'tables'],
            ).classes("text-sm")

        ui.separator()

        # Email status
        email_status_container = ui.row().classes("items-center gap-2 mt-2")
        _update_email_status(email_status_container)

        # Poll interval slider
        current_interval = _email_poll_interval()
        interval_label = ui.label(f"Poll interval: {current_interval}s").classes("text-sm mt-2")

        def _on_interval_change(e):
            val = int(e.value)
            _email_set_poll_interval(val)
            interval_label.text = f"Poll interval: {val}s"

        ui.slider(
            min=10, max=300, step=10, value=current_interval,
            on_change=_on_interval_change
        ).classes("w-full").tooltip("How often to check for new emails (seconds)")

        ui.separator()

        # Start / Stop controls
        ui.label("Email Control").classes("text-subtitle2 mt-2")

        async def _start_email():
            if not email_configured():
                ui.notify(
                    "Gmail OAuth not set up — enable the Gmail tool first and complete sign-in",
                    type="warning",
                )
                return
            try:
                ok = await _email_start()
                if ok:
                    _ch_config.set("email", "auto_start", True)
                    ui.notify("✅ Email polling started!", type="positive")
                else:
                    ui.notify("⚠️ Could not start — check Gmail OAuth", type="warning")
            except Exception as exc:
                ui.notify(f"Error starting email channel: {exc}", type="negative")
            _update_email_status(email_status_container)

        async def _stop_email():
            try:
                await _email_stop()
                _ch_config.set("email", "auto_start", False)
                ui.notify("Email polling stopped", type="info")
            except Exception as exc:
                ui.notify(f"Error stopping email channel: {exc}", type="negative")
            _update_email_status(email_status_container)

        with ui.row().classes("gap-2"):
            ui.button("▶️ Start Polling", on_click=_start_email).props("color=positive")
            ui.button("⏹️ Stop Polling", on_click=_stop_email).props("color=negative flat")

    def _update_tg_status(container):
        """Update the Telegram status indicator."""
        from channels.telegram import is_configured as tg_configured, is_running as tg_running
        container.clear()
        with container:
            if tg_running():
                ui.icon("check_circle", color="green").classes("text-lg")
                ui.label("Bot running — polling for messages").classes("text-green text-sm")
            elif tg_configured():
                ui.icon("pause_circle", color="blue").classes("text-lg")
                ui.label("Configured — click Start to begin").classes("text-blue text-sm")
            else:
                ui.icon("warning", color="orange").classes("text-lg")
                ui.label("Not configured").classes("text-orange text-sm")

    def _update_email_status(container):
        """Update the Email channel status indicator."""
        from channels.email import (
            is_configured as email_configured,
            is_running as email_running,
            get_last_error as email_last_error,
        )
        container.clear()
        with container:
            last_err = email_last_error()
            if last_err:
                ui.icon("error", color="red").classes("text-lg")
                ui.label(last_err).classes("text-red text-sm")
            elif email_running():
                ui.icon("check_circle", color="green").classes("text-lg")
                ui.label("Polling — checking for [Thoth] emails").classes("text-green text-sm")
            elif email_configured():
                ui.icon("pause_circle", color="blue").classes("text-lg")
                ui.label("Gmail OAuth ready — click Start to begin").classes("text-blue text-sm")
            else:
                ui.icon("warning", color="orange").classes("text-lg")
                ui.label("Gmail OAuth not set up — enable the Gmail tool first").classes("text-orange text-sm")

    # ══════════════════════════════════════════════════════════════════════
    # SIDEBAR
    # ══════════════════════════════════════════════════════════════════════

    with ui.left_drawer(value=True, fixed=True).style("width: 280px") as drawer:
        # Logo
        ui.html('<h2 style="margin: 0; color: gold;">𓁟 Thoth</h2>', sanitize=False)
        ui.label("Personal AI Sovereignty").classes("text-xs text-grey-6")
        ui.separator()

        # Home + New buttons
        with ui.row().classes("w-full gap-2"):
            def _go_home():
                prev = state.thread_id
                state.thread_id = None
                state.thread_name = None
                state.messages = []
                set_active_thread(None, previous_id=prev)
                _rebuild_main()
                _rebuild_thread_list()

            ui.button("🏠 Home", on_click=_go_home).classes("flex-grow").props("flat")

            def _new_thread():
                tid = uuid.uuid4().hex[:12]
                name = f"💻 Thread {datetime.now().strftime('%b %d, %H:%M')}"
                _save_thread_meta(tid, name)
                prev = state.thread_id
                state.thread_id = tid
                state.thread_name = name
                state.messages = []
                set_active_thread(tid, previous_id=prev)
                _rebuild_main()
                _rebuild_thread_list()

            ui.button("＋ New", on_click=_new_thread).classes("flex-grow").props("color=primary")

        ui.label("Conversations").classes("text-subtitle2 mt-2")
        p.thread_container = ui.column().classes("w-full gap-0")

        # Spacer pushes bottom section down
        ui.space()

        # Token counter
        p.token_label = ui.label("Context: 0K / 32K (0%)").classes("text-xs text-grey-6")
        p.token_bar = ui.linear_progress(value=0, show_value=False).style("height: 6px;")

        # Settings + Help
        with ui.row().classes("w-full gap-2"):
            ui.button("⚙️ Settings", on_click=_open_settings).classes("flex-grow")

            def _show_help():
                state.show_onboarding = True
                _rebuild_main()

            ui.button("👋", on_click=_show_help).props("flat dense")

    # ── Thread list builder ──────────────────────────────────────────────

    def _rebuild_thread_list() -> None:
        if p.thread_container is None:
            return
        p.thread_container.clear()
        threads = _list_threads()
        running_tids = get_running_workflows()

        def _fmt_ts(iso_str: str) -> str:
            """Format ISO timestamp to short readable form, e.g. 'Mar 09, 5:08 PM'."""
            try:
                dt = datetime.fromisoformat(iso_str)
                # %#I on Windows, %-I on Linux — fall back gracefully
                try:
                    return dt.strftime("%b %d, %#I:%M %p")
                except ValueError:
                    return dt.strftime("%b %d, %-I:%M %p")
            except Exception:
                return iso_str[:16] if iso_str else ""

        with p.thread_container:
            if not threads:
                ui.label("No conversations yet.").classes("text-grey-6 text-sm q-px-sm")
                return

            visible = threads[:_SIDEBAR_MAX_THREADS]
            for tid, name, created, updated in visible:
                name = name or ""
                is_active = tid == state.thread_id
                is_running = tid in running_tids

                def _select(t=tid, n=name):
                    prev = state.thread_id
                    state.thread_id = t
                    state.thread_name = n
                    state.messages = load_thread_messages(t)
                    set_active_thread(t, previous_id=prev)
                    _rebuild_main()
                    _rebuild_thread_list()

                def _delete(t=tid):
                    _delete_thread(t)
                    clear_summary_cache(t)
                    # Clean up shell session + history
                    from tools.shell_tool import get_session_manager, clear_shell_history
                    get_session_manager().kill_session(t)
                    clear_shell_history(t)
                    # Clean up browser session + history
                    from tools.browser_tool import (
                        get_session_manager as get_browser_session_manager,
                        clear_browser_history,
                    )
                    get_browser_session_manager().kill_session(t)
                    clear_browser_history(t)
                    set_active_thread(None, previous_id=t)
                    if state.thread_id == t:
                        state.thread_id = None
                        state.thread_name = None
                        state.messages = []
                        _rebuild_main()
                    _rebuild_thread_list()

                # Each thread item: a clickable row with name + delete icon
                with ui.item(on_click=_select).classes("w-full rounded").props(
                    "clickable" + (" active" if is_active else "")
                ).style("min-height: 40px; padding: 4px 8px;"):
                    with ui.item_section().props("avatar").style("min-width: 28px;"):
                        if is_running:
                            _thr_icon = "hourglass_top"
                        elif name.startswith("✈️"):
                            _thr_icon = "send"  # paper plane
                        elif name.startswith("📧"):
                            _thr_icon = "email"
                        elif name.startswith("⚡"):
                            _thr_icon = "electric_bolt"
                        else:
                            _thr_icon = "computer"  # web app
                        ui.icon(_thr_icon, size="xs").classes(
                            "text-primary" if is_active else "text-grey-6"
                        )
                    with ui.item_section():
                        ui.item_label(name).classes("ellipsis").style(
                            "font-size: 0.85rem;" + ("font-weight: 600;" if is_active else "")
                        )
                        if updated:
                            ui.item_label(_fmt_ts(updated)).props("caption").classes("text-grey-7").style(
                                "font-size: 0.7rem;"
                            )
                    with ui.item_section().props("side"):
                        ui.button(
                            icon="delete_outline", on_click=lambda e, t=tid: (_delete(t), e.sender.parent_slot.parent.update())
                        ).props("flat dense round size=xs color=grey-6").on(
                            "click", js_handler="(e) => e.stopPropagation()"
                        )

            if len(threads) > _SIDEBAR_MAX_THREADS:
                def _show_all():
                    with ui.dialog() as dlg, ui.card().classes("w-96"):
                        ui.label("All Conversations").classes("text-h6")
                        with ui.list().props("bordered separator").classes("w-full"):
                            for tid, name, created, updated in threads:
                                def _sel(t=tid, n=name):
                                    state.thread_id = t
                                    state.thread_name = n
                                    state.messages = load_thread_messages(t)
                                    dlg.close()
                                    _rebuild_main()
                                    _rebuild_thread_list()

                                def _del(t=tid):
                                    _delete_thread(t)
                                    clear_summary_cache(t)
                                    # Clean up shell session + history
                                    from tools.shell_tool import get_session_manager, clear_shell_history
                                    get_session_manager().kill_session(t)
                                    clear_shell_history(t)
                                    # Clean up browser session + history
                                    from tools.browser_tool import (
                                        get_session_manager as get_browser_session_manager,
                                        clear_browser_history,
                                    )
                                    get_browser_session_manager().kill_session(t)
                                    clear_browser_history(t)
                                    if state.thread_id == t:
                                        state.thread_id = None
                                        state.messages = []
                                    dlg.close()
                                    _rebuild_main()
                                    _rebuild_thread_list()

                                with ui.item(on_click=_sel).props("clickable"):
                                    with ui.item_section().props("avatar").style("min-width: 28px;"):
                                        ui.icon("chat_bubble_outline", size="xs")
                                    with ui.item_section():
                                        ui.item_label(name)
                                        if updated:
                                            ui.item_label(_fmt_ts(updated)).props("caption")
                                    with ui.item_section().props("side"):
                                        ui.button(
                                            icon="delete_outline", on_click=lambda e, t=tid: _del(t),
                                        ).props("flat dense round size=xs color=grey-6").on(
                                            "click", js_handler="(e) => e.stopPropagation()"
                                        )
                        ui.separator()
                        with ui.row().classes("w-full gap-2"):
                            def _delete_all():
                                for t, *_ in threads:
                                    _delete_thread(t)
                                clear_summary_cache()  # clear all summaries
                                # Clean up all shell sessions + history
                                from tools.shell_tool import get_session_manager, clear_shell_history
                                for t, *_ in threads:
                                    get_session_manager().kill_session(t)
                                    clear_shell_history(t)
                                state.thread_id = None
                                state.thread_name = None
                                state.messages = []
                                dlg.close()
                                _rebuild_main()
                                _rebuild_thread_list()

                            ui.button("Delete all", icon="delete_sweep", on_click=_delete_all).props(
                                "flat color=negative"
                            ).classes("flex-grow")
                            ui.button("Close", on_click=dlg.close).props("flat").classes("flex-grow")
                    dlg.open()

                ui.button(
                    f"Show all ({len(threads)})", on_click=_show_all
                ).classes("w-full q-mt-xs").props("flat dense size=sm")

    _rebuild_thread_list()

    # ══════════════════════════════════════════════════════════════════════
    # MAIN CONTENT AREA
    # ══════════════════════════════════════════════════════════════════════

    p.main_col = ui.column().classes("w-full max-w-5xl mx-auto px-4 no-wrap").style(
        "height: calc(100vh - 16px); overflow: hidden;"
    )

    def _rebuild_main() -> None:
        if p.main_col is None:
            return
        p.main_col.clear()
        with p.main_col:
            if state.thread_id is None:
                _build_home()
            else:
                _build_chat()

    # ── Home screen ──────────────────────────────────────────────────────

    def _build_home() -> None:
        with ui.scroll_area().classes("w-full flex-grow"):
            # Title
            ui.html(
                '<div style="text-align:center; padding-top:2rem;">'
                '<h1 style="color: gold;">𓁟 Thoth</h1></div>',
                sanitize=False,
            )

            if state.show_onboarding:
                with ui.card().classes("w-full"):
                    with ui.row().classes("w-full justify-between items-center"):
                        ui.label("")  # spacer
                        def _dismiss_help():
                            state.show_onboarding = False
                            _mark_onboarding_seen()
                            _rebuild_main()
                        ui.button(icon="close", on_click=_dismiss_help).props("flat dense round size=sm")
                    ui.markdown(_WELCOME_MESSAGE, extras=['code-friendly', 'fenced-code-blocks', 'tables'])
                    ui.separator()
                    ui.label("💡 Try asking me something:").classes("font-bold")
                    with ui.row().classes("w-full flex-wrap gap-2"):
                        for prompt in _EXAMPLE_PROMPTS:
                            def _try(p=prompt):
                                state.show_onboarding = False
                                _mark_onboarding_seen()
                                asyncio.create_task(_send_message(p))

                            ui.button(prompt, on_click=_try).props("flat dense outline").style(
                                "text-transform: none;"
                            )
                if _is_first_run():
                    _mark_onboarding_seen()
            else:
                ui.html(
                    '<p style="text-align:center; font-size:1.1rem; opacity:0.6;">'
                    'Select a conversation from the sidebar or start a new one.</p>',
                    sanitize=False,
                )

            # Workflow tiles
            home_workflows = list_workflows()
            if home_workflows:
                ui.separator()
                ui.label("⚡ Workflows").classes("text-h5")
                ui.label("Create and manage workflows in ⚙️ Settings → ⚡ Workflows").classes("text-xs text-grey-6")
                with ui.row().classes("w-full flex-wrap gap-4"):
                    for wf in home_workflows:
                        with ui.card().classes("w-48"):
                            ui.label(wf["icon"]).classes("text-h3 text-center w-full")
                            ui.label(wf["name"]).classes("font-bold text-center w-full")
                            if wf.get("description"):
                                ui.label(wf["description"]).classes("text-xs text-grey-6 text-center w-full")
                            step_label = f"{len(wf['prompts'])} step{'s' if len(wf['prompts']) != 1 else ''}"
                            if wf.get("last_run"):
                                try:
                                    lr = datetime.fromisoformat(wf["last_run"])
                                    step_label += f" · Last: {lr.strftime('%b %d')}"
                                except (ValueError, TypeError):
                                    pass
                            sched = wf.get("schedule") or ""
                            if sched.startswith("daily"):
                                step_label += " · 📅 Daily"
                            elif sched.startswith("weekly"):
                                step_label += " · 📅 Weekly"
                            ui.label(step_label).classes("text-xs text-grey-6 text-center w-full")

                            def _run_wf(w=wf):
                                wf_tid = uuid.uuid4().hex[:12]
                                wf_name = f"⚡ {w['name']} — {datetime.now().strftime('%b %d, %I:%M %p')}"
                                _save_thread_meta(wf_tid, wf_name)
                                bg_tools = [t.name for t in tool_registry.get_enabled_tools()]
                                run_workflow_background(w["id"], wf_tid, bg_tools, start_step=0, notification=True)
                                ui.notify(f"⚡ {w['name']} started — you'll be notified when done.", type="positive")
                                _rebuild_thread_list()

                            ui.button("▶ Run", on_click=_run_wf).classes("w-full").props("color=primary")

        # Home screen has no input — add a simple prompt input
        with ui.row().classes("w-full items-end gap-2 shrink-0 py-2"):
            home_input = ui.input(placeholder="Ask anything to start a conversation…").classes(
                "flex-grow"
            ).props('outlined dense')

            async def _home_send():
                text = home_input.value
                if text and text.strip():
                    home_input.value = ""
                    await _send_message(text)

            home_input.on("keydown.enter", _home_send)
            ui.button(icon="send", on_click=_home_send).props("color=primary round")

    # ── Chat screen ──────────────────────────────────────────────────────

    def _build_chat() -> None:
        # Header
        running_wfs = get_running_workflows()
        bg = running_wfs.get(state.thread_id)

        with ui.row().classes("w-full items-center shrink-0"):
            if bg:
                ui.html(
                    f"<h3>⚡ {bg['name']} "
                    f"<span style='font-size:0.8rem; opacity:0.7;'>"
                    f"Running — Step {bg['step']+1}/{bg['total']}</span></h3>",
                    sanitize=False,
                )
            else:
                p.chat_header_label = ui.label(f"💬 {state.thread_name}").classes("text-h5 flex-grow")
            if state.messages:
                ui.button(icon="download", on_click=_open_export).props("flat round").tooltip("Export")

        # Scrollable message area
        p.chat_scroll = ui.scroll_area().classes("w-full flex-grow")
        with p.chat_scroll:
            p.chat_container = ui.column().classes("w-full gap-2")

        # Render existing messages
        for msg in state.messages:
            _add_chat_message(msg)

        # Onboarding (triggered by 👋 button)
        if state.show_onboarding:
            with p.chat_container:
                with ui.element("div").classes("thoth-msg-row"):
                    ui.html('<div class="thoth-avatar thoth-avatar-bot">𓁟</div>', sanitize=False)
                    with ui.column().classes("thoth-msg-body gap-1"):
                        ui.html(
                            '<div class="thoth-msg-header">'
                            '<span class="thoth-msg-name">Thoth</span>'
                            '</div>',
                            sanitize=False,
                        )
                        ui.markdown(_WELCOME_MESSAGE, extras=['code-friendly', 'fenced-code-blocks', 'tables'])
                        with ui.row().classes("flex-wrap gap-2"):
                            for prompt in _EXAMPLE_PROMPTS:
                                def _try_inline(pr=prompt):
                                    state.show_onboarding = False
                                    asyncio.create_task(_send_message(pr))

                                ui.button(prompt, on_click=_try_inline).props("flat dense outline").style(
                                    "text-transform:none;"
                                )

                        def _dismiss():
                            state.show_onboarding = False
                            _rebuild_main()

                        ui.button("✕ Dismiss", on_click=_dismiss).props("flat dense")

        # Interrupt UI
        if state.pending_interrupt:
            _show_interrupt(state.pending_interrupt)

        # Scroll to bottom
        if p.chat_scroll:
            p.chat_scroll.scroll_to(percent=1.0)

        # ── Terminal toggle bar + panel ───────────────────────────────
        p.terminal_visible = False
        p.terminal_toggle_bar = None

        if tool_registry.is_enabled("shell"):
            def _toggle_terminal():
                p.terminal_visible = not getattr(p, "terminal_visible", False)
                if p.terminal_panel is not None:
                    p.terminal_panel.set_visibility(p.terminal_visible)
                    if p.terminal_visible and p.terminal_scroll:
                        p.terminal_scroll.scroll_to(percent=1.0)
                # Rotate chevron icon
                if p.terminal_toggle_bar is not None:
                    _chevron = "expand_less" if p.terminal_visible else "expand_more"
                    p.terminal_chevron.props(f"icon={_chevron}")

            p.terminal_toggle_bar = ui.row().classes(
                "w-full items-center px-3 cursor-pointer"
            ).style(
                "height: 28px; background: #1a1a2e; "
                "border-top: 1px solid #333; gap: 6px;"
            )
            p.terminal_toggle_bar.on("click", lambda: _toggle_terminal())

            def _clear_terminal():
                from tools.shell_tool import clear_shell_history
                if state.thread_id:
                    clear_shell_history(state.thread_id)
                if p.terminal_container:
                    p.terminal_container.clear()

            with p.terminal_toggle_bar:
                ui.icon("terminal").classes("text-grey-5").style("font-size: 14px;")
                ui.label("Terminal").classes("text-xs font-bold text-grey-5 flex-grow")
                ui.button(icon="delete_sweep", on_click=_clear_terminal).props(
                    "flat round dense size=xs"
                ).classes("text-grey-5").tooltip("Clear terminal history")
                p.terminal_chevron = ui.button(icon="expand_more").props(
                    "flat round dense size=xs"
                ).classes("text-grey-5")
                p.terminal_chevron.on("click.stop", lambda: _toggle_terminal())

        p.terminal_panel = ui.column().classes("w-full shrink-0").style(
            "max-height: 250px;"
        )
        p.terminal_panel.set_visibility(False)
        p.terminal_scroll = None
        p.terminal_container = None

        with p.terminal_panel:
            p.terminal_scroll = ui.scroll_area().classes("w-full flex-grow").style(
                "max-height: 230px; background: #0d1117;"
            )
            with p.terminal_scroll:
                p.terminal_container = ui.column().classes("w-full gap-0 px-2 py-1")

        # Render existing shell history for this thread
        if state.thread_id:
            from tools.shell_tool import get_shell_history
            _history = get_shell_history(state.thread_id)
            for entry in _history:
                _add_terminal_entry(entry)
            # Auto-show terminal panel if there is history
            if _history and p.terminal_panel is not None:
                p.terminal_visible = True
                p.terminal_panel.set_visibility(True)
                if hasattr(p, "terminal_chevron") and p.terminal_chevron:
                    p.terminal_chevron.props("icon=expand_less")
            if p.terminal_scroll:
                p.terminal_scroll.scroll_to(percent=1.0)

        # ── File chips (shown above input when files attached) ────────────
        p.file_chips_row = ui.row().classes("w-full flex-wrap gap-1")

        async def _on_upload(e: events.UploadEventArguments):
            data = await e.file.read()
            name = e.file.name
            p.pending_files.append({"name": name, "data": data})
            with p.file_chips_row:
                idx = len(p.pending_files) - 1
                def _remove(i=idx, badge=None):
                    if i < len(p.pending_files):
                        p.pending_files.pop(i)
                    if badge:
                        badge.delete()
                b = ui.badge(f"📎 {name} ✕", color="grey-8").props("outline")
                b.on("click", lambda b=b, i=idx: _remove(i, b))
                b.style("cursor: pointer;")

        # Hidden upload element triggered by attach button & drag-and-drop
        _hidden_upload = ui.upload(
            on_upload=_on_upload,
            auto_upload=True,
            multiple=True,
        ).classes("hidden")

        # Enable drag-and-drop anywhere on the page → forward to QUploader
        ui.run_javascript(f'''
            (() => {{
                const uid = {_hidden_upload.id};
                const body = document.body;
                let overlay = null;

                function showOverlay() {{
                    if (overlay) return;
                    overlay = document.createElement("div");
                    overlay.style.cssText = "position:fixed;inset:0;z-index:9999;" +
                        "background:rgba(30,136,229,0.15);border:3px dashed #1e88e5;" +
                        "display:flex;align-items:center;justify-content:center;" +
                        "pointer-events:none;";
                    overlay.innerHTML = '<div style="color:#1e88e5;font-size:1.5rem;font-weight:600;">Drop files here</div>';
                    document.body.appendChild(overlay);
                }}

                function hideOverlay() {{
                    if (overlay) {{ overlay.remove(); overlay = null; }}
                }}

                body.addEventListener("dragover", (e) => {{ e.preventDefault(); showOverlay(); }});
                body.addEventListener("dragleave", (e) => {{
                    if (e.relatedTarget === null || !body.contains(e.relatedTarget)) hideOverlay();
                }});
                body.addEventListener("drop", (e) => {{
                    e.preventDefault();
                    hideOverlay();
                    const files = e.dataTransfer?.files;
                    if (!files || files.length === 0) return;
                    const vue = getElement(uid);
                    if (vue && vue.$refs.qRef) {{
                        vue.$refs.qRef.addFiles(files);
                    }}
                }});
            }})();
        ''')

        # ── Chat input + attach + send + stop ────────────────────────────
        with ui.row().classes("w-full items-end gap-2 shrink-0"):
            ui.button(icon="attach_file", on_click=lambda: ui.run_javascript(
                f"document.getElementById('c{_hidden_upload.id}').querySelector('input[type=file]').click()"
            )).props("flat round dense").tooltip("Attach files")

            p.chat_input = ui.input(
                placeholder="Ask anything…",
            ).classes("flex-grow").props("outlined dense")

            async def _on_send():
                text = p.chat_input.value
                if text and text.strip():
                    p.chat_input.value = ""
                    await _send_message(text)
                elif p.pending_files:
                    p.chat_input.value = ""
                    await _send_message("")

            p.chat_input.on("keydown.enter", _on_send)
            ui.button(icon="send", on_click=_on_send).props("color=primary round")

            def _on_stop():
                state.stop_event.set()             # signal producer + consumer
                tts = state.tts_service
                if tts and tts.enabled:
                    tts.stop()
                    if state.voice_service and state.voice_service.is_running:
                        state.voice_service.unmute()
                # Don't set is_generating=False here — consumer does it
                # after producer confirms cleanup is done (sends None)
                if p.stop_btn:
                    p.stop_btn.props('icon=hourglass_top')

            p.stop_btn = ui.button(icon="stop", on_click=_on_stop).props(
                "round"
            ).tooltip("Stop generation")
            if not state.is_generating:
                p.stop_btn.disable()

        # ── Voice bar ────────────────────────────────────────────────────
        with ui.row().classes("w-full items-center shrink-0 gap-2 py-1"):
            def _toggle_voice(e):
                state.voice_enabled = e.value
                if e.value:
                    state.voice_service.start()
                else:
                    state.voice_service.stop()

            p.voice_switch = ui.switch("🎤 Voice", value=state.voice_enabled, on_change=_toggle_voice)
            p.voice_status_label = ui.label("").classes("text-xs text-grey-6")

    # ── Initial render ───────────────────────────────────────────────────
    _rebuild_main()

    # ══════════════════════════════════════════════════════════════════════
    # TIMERS (page-level — survive rebuilds)
    # ══════════════════════════════════════════════════════════════════════

    def _poll_notifications() -> None:
        for t in drain_toasts():
            ui.notify(t["message"], type="positive", position="top-right", timeout=5000)
            _rebuild_thread_list()

    def _poll_voice() -> None:
        if not state.voice_enabled:
            if p.voice_status_label:
                p.voice_status_label.text = ""
            return

        svc = state.voice_service
        new_status = svc.get_status()
        st = svc.state
        if p.voice_status_label:
            if st == "listening":
                p.voice_status_label.text = "🔴 Listening — speak now…"
            elif st == "transcribing":
                p.voice_status_label.text = "⏳ Processing…"
            elif st == "muted":
                tts = state.tts_service
                if tts and not tts.is_speaking:
                    svc.unmute()
                    p.voice_status_label.text = "🔴 Listening — speak now…"
                else:
                    p.voice_status_label.text = "🔇 Speaking…"
            elif st == "stopped":
                p.voice_status_label.text = f"⚫ {new_status or 'Stopped'}"

        text = svc.get_transcription()
        if text:
            # Stop TTS if speaking
            if state.tts_service and state.tts_service.enabled:
                state.tts_service.stop()
            # Auto-name thread
            if state.thread_name and state.thread_name.startswith("Thread "):
                state.thread_name = text[:50]
                _save_thread_meta(state.thread_id, state.thread_name)
                _rebuild_thread_list()
            asyncio.create_task(_send_message(text, voice_mode=True))

    def _update_token_counter() -> None:
        config = {"configurable": {"thread_id": state.thread_id}} if state.thread_id else None
        used, max_tokens = get_token_usage(config)
        pct = min(used / max_tokens, 1.0) if max_tokens else 0.0
        used_label = f"{used / 1_000:.1f}K" if max_tokens >= 1_000 else str(used)
        max_label = f"{max_tokens / 1_000:.0f}K" if max_tokens >= 1_000 else str(max_tokens)
        if p.token_label:
            p.token_label.text = f"Context: {used_label} / {max_label} ({pct:.0%})"
        if p.token_bar:
            p.token_bar.value = pct

    ui.timer(1.0, _poll_notifications)
    ui.timer(0.3, _poll_voice)
    ui.timer(5.0, _update_token_counter)

    # Initial token count
    _update_token_counter()


# ═════════════════════════════════════════════════════════════════════════════
# SHUTDOWN — clean up browser & shell sessions so no orphans survive
# ═════════════════════════════════════════════════════════════════════════════

@app.on_shutdown
async def on_shutdown():
    """Gracefully close Thoth-managed browser and shell sessions.

    • BrowserSessionManager.kill_all() sends a sentinel to the Playwright
      work queue → _pw_loop calls context.close() → only closes the
      Playwright-managed browser, never the user's own browser windows.
    • ShellSessionManager.kill_all() just clears the session dict
      (subprocesses are already cleaned up by Python GC / __del__).
    """
    print("[shutdown] Cleaning up sessions…")
    try:
        from tools.browser_tool import get_session_manager as _get_bsm
        _get_bsm().kill_all()
        print("[shutdown] Browser session closed")
    except Exception as exc:
        print(f"[shutdown] Browser cleanup error: {exc}")
    try:
        from tools.shell_tool import get_session_manager as _get_ssm
        _get_ssm().kill_all()
        print("[shutdown] Shell sessions closed")
    except Exception as exc:
        print(f"[shutdown] Shell cleanup error: {exc}")
    print("[shutdown] Done")


# ═════════════════════════════════════════════════════════════════════════════
# STARTUP
# ═════════════════════════════════════════════════════════════════════════════

@app.on_startup
async def on_startup():
    global _startup_ready, _startup_status

    def _set(msg):
        global _startup_status
        _startup_status = msg
        print(f"[startup] {msg}")

    _set("🔑 Applying API keys…")
    await asyncio.to_thread(apply_keys)

    _set("🧠 Extracting memories…")
    def _extract():
        def _on_status(m):
            global _startup_status
            _startup_status = f"🧠 {m}"
            print(f"[startup]   {m}")
        return run_extraction(on_status=_on_status)
    count = await asyncio.to_thread(_extract)
    print(f"[startup] Memory extraction done — {count} new memory(s)")

    _set("🔄 Starting periodic extraction…")
    await asyncio.to_thread(start_periodic_extraction)

    _set("⚡ Loading workflows…")
    await asyncio.to_thread(lambda: (seed_default_workflows(), start_workflow_scheduler()))

    # ── Auto-start channels ──────────────────────────────────────────────────
    _set("📡 Starting channels…")
    if _ch_config.get("telegram", "auto_start", False):
        try:
            ok = await _tg_start_bot()
            if ok:
                print("[startup] ✅ Telegram bot auto-started")
            else:
                _startup_warnings.append("⚠️ Telegram bot failed to auto-start — check Settings → Channels")
                print("[startup] ⚠️ Telegram bot auto-start failed")
        except Exception as exc:
            _startup_warnings.append(f"⚠️ Telegram bot failed to auto-start: {exc}")
            print(f"[startup] ⚠️ Telegram auto-start error: {exc}")

    if _ch_config.get("email", "auto_start", False):
        try:
            ok = await _email_start()
            if ok:
                print("[startup] ✅ Email polling auto-started")
            else:
                _startup_warnings.append("⚠️ Email polling failed to auto-start — check Settings → Channels")
                print("[startup] ⚠️ Email polling auto-start failed")
        except Exception as exc:
            _startup_warnings.append(f"⚠️ Email polling failed to auto-start: {exc}")
            print(f"[startup] ⚠️ Email auto-start error: {exc}")

    _set("✅ Ready")
    _startup_ready = True


# ═════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═════════════════════════════════════════════════════════════════════════════

if __name__ in {"__main__", "__mp_main__"}:
    # --native  → open in a native OS window (pywebview) instead of browser
    # --show   → open a browser tab (for development / fallback)
    _native = "--native" in sys.argv
    _show   = "--show" in sys.argv and not _native

    ui.run(
        title="Thoth",
        port=8080,
        dark=True,
        favicon="𓁟",
        reload=False,
        show=_show,
        native=_native,
        window_size=(1280, 900) if _native else None,
    )
