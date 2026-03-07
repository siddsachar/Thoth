import streamlit as st
import uuid
import pathlib
import tempfile
import os
import json as _json_mod
import base64 as _b64
from datetime import datetime

# ── Page config (must be first Streamlit command) ────────────────────────────
st.set_page_config(
    page_title="Thoth",
    page_icon="𓁟",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Heavy imports behind a loading indicator ─────────────────────────────────
with st.spinner("Loading models. Please Wait…"):
    from threads import _list_threads, _save_thread_meta, _delete_thread, checkpointer, DB_PATH
    from agent import invoke_agent, get_agent_graph, stream_agent, resume_stream_agent, clear_agent_cache, repair_orphaned_tool_calls, get_token_usage
    from documents import (
        load_processed_files,
        load_and_vectorize_document,
        reset_vector_store,
        DocumentLoader,
    )
    from models import (
        get_current_model,
        set_model,
        list_all_models,
        list_local_models,
        is_model_local,
        pull_model,
        get_context_size,
        set_context_size,
        DEFAULT_MODEL,
        DEFAULT_CONTEXT_SIZE,
        CONTEXT_SIZE_OPTIONS,
        CONTEXT_SIZE_LABELS,
    )
    from api_keys import get_key, set_key, apply_keys
    from tools import registry as tool_registry
    from voice import VoiceService, get_voice_service, get_available_wake_models, get_available_whisper_sizes
    from tts import TTSService, get_voice_catalog, VOICE_CATALOG
    from vision import VisionService, POPULAR_VISION_MODELS, DEFAULT_VISION_MODEL, list_cameras
    from tools.vision_tool import set_vision_service
    apply_keys()


# ── Startup health check ────────────────────────────────────────────────────
def _startup_health_check():
    """Verify Ollama is reachable and the selected model is available.
    Blocks the app with a clear error screen if something is wrong."""
    import ollama as _ollama_check

    # 1. Check Ollama connectivity
    try:
        _ollama_check.list()
    except Exception:
        st.error(
            "**Cannot connect to Ollama.**\n\n"
            "Thoth requires [Ollama](https://ollama.com/) to be running.\n\n"
            "**To fix this:**\n"
            "1. Open a terminal and run `ollama serve`\n"
            "2. Or start the Ollama desktop app\n"
            "3. Then refresh this page",
            icon="🚫",
        )
        if st.button("🔄 Retry", use_container_width=True, type="primary"):
            st.rerun()
        st.stop()

    # 2. Check if the selected model is downloaded
    current = get_current_model()
    if not is_model_local(current):
        st.warning(
            f"**Model `{current}` is not downloaded.**\n\n"
            f"Download it now to get started. You can change models later in Settings.",
            icon="⚠️",
        )
        if st.button(f"⬇️ Download {current}", use_container_width=True, type="primary"):
            progress = st.progress(0, text=f"Downloading {current}…")
            try:
                for update in pull_model(current):
                    if hasattr(update, "completed") and hasattr(update, "total") and update.total:
                        pct = update.completed / update.total
                        progress.progress(pct, text=f"Downloading {current}… {pct:.0%}")
                progress.progress(1.0, text="Download complete!")
                st.rerun()
            except Exception as e:
                st.error(f"Download failed: {e}")
        st.stop()

_startup_health_check()


# ── App config persistence (first-run flag, etc.) ──────────────────────────
_APP_CONFIG_DIR = pathlib.Path(os.environ.get("THOTH_DATA_DIR", pathlib.Path.home() / ".thoth"))
_APP_CONFIG_PATH = _APP_CONFIG_DIR / "app_config.json"


def _load_app_config() -> dict:
    if _APP_CONFIG_PATH.exists():
        try:
            return _json_mod.loads(_APP_CONFIG_PATH.read_text())
        except Exception:
            return {}
    return {}


def _save_app_config(cfg: dict) -> None:
    _APP_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _APP_CONFIG_PATH.write_text(_json_mod.dumps(cfg, indent=2))


def _is_first_run() -> bool:
    return not _load_app_config().get("onboarding_seen", False)


def _mark_onboarding_seen() -> None:
    cfg = _load_app_config()
    cfg["onboarding_seen"] = True
    _save_app_config(cfg)


# ── Onboarding / welcome message ──────────────────────────────────────────
_WELCOME_MESSAGE = """\
👋 **Welcome to Thoth — your personal AI assistant!**

Here's what I can do:

---

🔍 **Search & Knowledge**
Web search, Wikipedia, Arxiv papers, YouTube videos & transcripts, URL reading, and your uploaded documents.

📧 **Productivity**
Read & send Gmail, manage Google Calendar events, read & write files on your computer, and set desktop notification timers.

🧮 **Computation & Analysis**
Math calculations, Wolfram Alpha queries, weather forecasts, CSV/Excel/JSON analysis, and camera or screen capture via vision.

🧠 **Memory & History**
I remember important things you tell me across conversations. I can also search your past conversations by keyword.

---

⚙️ **Getting started:**
- Head to **⚙️ Settings** (bottom of the sidebar) to configure tools like Gmail, Calendar, and your filesystem workspace folder.
- **Attach files** by clicking the 📎 icon in the chat input — I can read PDFs, spreadsheets, JSON, images, and more.
- **Voice mode**: Click the 🎙️ mic button above the chat input or say the wake word to talk to me.
- **Stop generation** anytime with the ⏹ button.

---

💡 **Try asking me something:**
"""

_EXAMPLE_PROMPTS = [
    "What's the weather in New York?",
    "Summarize the latest AI news",
    "Set a timer for 10 minutes",
    "What do you remember about me?",
    "Read the file report.pdf in my workspace",
    "What's the derivative of x³ + 2x?",
]


# ── DRY helpers: tkinter browse, model download, ops checkboxes ────────────────
def _browse_folder(title: str = "Select folder", initial_dir: str | None = None) -> str | None:
    """Open a native folder-picker dialog and return the selected path (or None)."""
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    folder = filedialog.askdirectory(title=title, initialdir=initial_dir or None)
    root.destroy()
    return folder or None


def _browse_file(
    title: str = "Select file",
    initial_dir: str | None = None,
    filetypes: list[tuple[str, str]] | None = None,
) -> str | None:
    """Open a native file-picker dialog and return the selected path (or None)."""
    import tkinter as tk
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    path = filedialog.askopenfilename(
        title=title,
        initialdir=initial_dir or None,
        filetypes=filetypes or [],
    )
    root.destroy()
    return path or None


def _pull_model_with_progress(model_name: str) -> None:
    """Download an Ollama model with a Streamlit status indicator."""
    with st.status(f"Downloading **{model_name}**…", expanded=True) as status:
        for progress in pull_model(model_name):
            total = int(progress.get("total", 0) if progress.get("total") else 0)
            completed = int(progress.get("completed", 0) if progress.get("completed") else 0)
            msg = progress.get("status", "")
            if total:
                pct = int(completed / total * 100)
                status.update(label=f"Downloading {model_name}: {pct}%")
            else:
                status.update(label=f"{model_name}: {msg}")
        status.update(label=f"✅ {model_name} ready!", state="complete")


def _render_ops_checkboxes(
    columns: list[tuple[str, list[str]]],
    current_ops: list[str],
    key_prefix: str,
) -> list[str]:
    """Render a 3-column grid of operation checkboxes.

    *columns* is a list of ``(header, ops_list)`` tuples.
    Returns the list of selected operations.
    """
    st.caption("Allowed operations")
    cols = st.columns(len(columns))
    selected: list[str] = []
    for col, (header, ops) in zip(cols, columns):
        with col:
            st.markdown(f"**{header}**")
            for op in ops:
                if st.checkbox(op, value=op in current_ops, key=f"cfg_{key_prefix}_ops_{op}"):
                    selected.append(op)
    return selected


# ── Ensure default model is available ────────────────────────────────────────
if not is_model_local(DEFAULT_MODEL):
    _pull_model_with_progress(DEFAULT_MODEL)

# ── Custom CSS ───────────────────────────────────────────────────────────────
st.markdown(
    """
    <style>
    /* Hide Streamlit's built-in header (deploy button / hamburger menu) */
    header[data-testid="stHeader"] { display: none !important; }

    /* Tighten top padding */
    .block-container { padding-top: 1.5rem; padding-bottom: 7rem; }

    /* Pin chat input to bottom */
    div[data-testid="stChatInput"] {
        position: fixed;
        bottom: 2.5rem;
        width: calc(100% - 28rem);
        z-index: 100;
    }

    /* Stop button pinned to the right of chat input */
    .st-key-stop_btn {
        position: fixed;
        bottom: 2.6rem;
        right: 1.5rem;
        z-index: 101;
        width: auto;
    }
    .st-key-stop_btn button {
        border-radius: 50%;
        width: 2.8rem;
        height: 2.8rem;
        padding: 0;
        font-size: 1.3rem;
        line-height: 1;
        display: flex;
        align-items: center;
        justify-content: center;
    }

    /* Export button styling */
    .st-key-export_btn button {
        min-height: 2.2rem;
        margin-top: 0.3rem;
    }

    /* Thread buttons */
    div[data-testid="stSidebar"] .stButton > button {
        width: 100%;
        text-align: left;
        border-radius: 8px;
    }

    /* Voice bar pinned just above chat input */
    .st-key-voice_bar {
        position: fixed;
        bottom: 0.25rem;
        z-index: 101;
        width: calc(100% - 25rem);
        background: var(--background-color, #0e1117);
        padding: 0.15rem 0;
    }
    .st-key-voice_bar [data-testid="stHorizontalBlock"] {
        align-items: center;
        gap: 0.5rem;
    }

    /* Typing indicator animation */
    .thoth-typing {
        color: #888;
        font-size: 0.95rem;
        font-style: italic;
    }
    .thoth-typing .dots span {
        animation: blink 1.4s infinite both;
    }
    .thoth-typing .dots span:nth-child(2) { animation-delay: 0.2s; }
    .thoth-typing .dots span:nth-child(3) { animation-delay: 0.4s; }
    @keyframes blink {
        0%, 80%, 100% { opacity: 0; }
        40% { opacity: 1; }
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# ── Auto-scroll helper ───────────────────────────────────────────────────────
import streamlit.components.v1 as components

def _scroll_to_bottom():
    """Inject JS to scroll the chat area to the bottom."""
    components.html(
        """
        <script>
            function scrollChatToBottom() {
                let el = window.frameElement;
                while (el) {
                    el = el.parentElement;
                    if (el && el.scrollHeight > el.clientHeight + 10) {
                        el.scrollTop = el.scrollHeight;
                    }
                }
            }
            scrollChatToBottom();
            setTimeout(scrollChatToBottom, 200);
            setTimeout(scrollChatToBottom, 800);
        </script>
        """,
        height=1,
    )


# ── Helper: render message with embedded YouTube videos ─────────────────────
import re as _re_mod

_YT_URL_PATTERN = _re_mod.compile(
    r"https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/)([a-zA-Z0-9_-]{11})(?:[^\s)\]]*)"
)

# Matches fenced code blocks: ```lang\n...\n```
_CODE_FENCE_PATTERN = _re_mod.compile(
    r"```(\w*)\n(.*?)```", _re_mod.DOTALL
)

def _yt_embed_html(video_id: str) -> str:
    """Return a small iframe embed for a YouTube video."""
    return (
        f'<iframe width="280" height="158" '
        f'src="https://www.youtube.com/embed/{video_id}" '
        f'frameborder="0" allowfullscreen '
        f'style="border-radius:8px;"></iframe>'
    )


def _render_text_segment(text: str) -> None:
    """Render a text segment, splitting out fenced code blocks into
    ``st.code()`` widgets (which have a built-in copy button and syntax
    highlighting) while rendering surrounding prose as markdown."""
    last = 0
    for m in _CODE_FENCE_PATTERN.finditer(text):
        # Render any prose before this code block
        before = text[last:m.start()].strip()
        if before:
            st.markdown(before)
        lang = m.group(1) or None
        code = m.group(2).rstrip("\n")
        st.code(code, language=lang)
        last = m.end()
    # Render remaining prose after the last code block
    after = text[last:].strip()
    if after:
        st.markdown(after)


def _render_message(content: str, images: list[str] | None = None, tool_results: list[dict] | None = None, role: str = "assistant", charts: list[str] | None = None):
    """Render a chat message with syntax-highlighted code blocks (with copy
    button), inline YouTube embeds, captured images, charts, and tool results."""
    # Show tool results as expandable sections
    if tool_results:
        for tr in tool_results:
            with st.expander(f"✅ {tr['name']}", expanded=False):
                if tr.get("content"):
                    display = tr["content"]
                    if len(display) > 5000:
                        display = display[:5000] + "\n\n… (truncated)"
                    st.code(display)
            if tr.get("content") and "[Truncated" in tr["content"]:
                import re as _re_mod
                _trunc_match = _re_mod.search(r'\[Truncated[^\]]*\]', tr["content"])
                _trunc_msg = _trunc_match.group(0)[1:-1] if _trunc_match else "File was too large to read in full"
                st.warning(_trunc_msg, icon="⚠️")

    # Show captured images / attached images
    if images:
        caption = "📎 Attached image" if role == "user" else "📷 Captured image"
        for b64_img in images:
            img_bytes = _b64.b64decode(b64_img)
            st.image(img_bytes, caption=caption, width=320)

    # Show inline charts (Plotly)
    if charts:
        try:
            import plotly.io as _pio
            for fig_json in charts:
                fig = _pio.from_json(fig_json)
                st.plotly_chart(fig, use_container_width=True)
        except Exception:
            pass

    # Split content at each YouTube URL and insert an embed after each one
    seen = set()
    last_end = 0
    parts = []
    for match in _YT_URL_PATTERN.finditer(content):
        video_id = match.group(1)
        # Include text up to and including the URL
        parts.append(("text", content[last_end:match.end()]))
        if video_id not in seen:
            seen.add(video_id)
            parts.append(("video", video_id))
        last_end = match.end()
    # Remaining text after last URL
    if last_end < len(content):
        parts.append(("text", content[last_end:]))

    if not parts:
        _render_text_segment(content)
        return

    for kind, value in parts:
        if kind == "text" and value.strip():
            _render_text_segment(value)
        elif kind == "video":
            st.markdown(_yt_embed_html(value), unsafe_allow_html=True)


# ── Helper: export conversation ─────────────────────────────────────────────
def _export_as_markdown(thread_name: str, messages: list[dict]) -> str:
    """Convert messages to a Markdown document."""
    lines = [f"# {thread_name}\n"]
    lines.append(f"*Exported from Thoth on {datetime.now().strftime('%Y-%m-%d %H:%M')}*\n")
    lines.append("---\n")
    for msg in messages:
        role = "🧑 User" if msg["role"] == "user" else "𓁟 Thoth"
        lines.append(f"### {role}\n")
        lines.append(msg["content"] + "\n")
    return "\n".join(lines)


def _export_as_text(thread_name: str, messages: list[dict]) -> str:
    """Convert messages to plain text."""
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

    # Title
    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 12, thread_name, new_x="LMARGIN", new_y="NEXT")
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
        # Role header
        pdf.set_font("Helvetica", "B", 11)
        if msg["role"] == "user":
            pdf.set_text_color(50, 100, 200)
        else:
            pdf.set_text_color(200, 160, 0)
        pdf.cell(0, 8, role, new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)

        # Message body — encode to latin-1 safe text for FPDF built-in fonts
        pdf.set_font("Helvetica", "", 10)
        text = msg["content"]
        # Replace characters that can't be encoded in latin-1
        safe_text = text.encode("latin-1", errors="replace").decode("latin-1")
        pdf.multi_cell(0, 5, safe_text)
        pdf.ln(4)

    return bytes(pdf.output())


@st.dialog("📤 Export Conversation", width="small")
def _export_dialog():
    """Dialog for exporting the current conversation."""
    messages = st.session_state.messages
    thread_name = st.session_state.thread_name or "Conversation"

    if not messages:
        st.warning("No messages to export.")
        return

    st.markdown(f"Export **{thread_name}** ({len(messages)} messages)")

    fmt = st.radio(
        "Format",
        ["Markdown (.md)", "Plain text (.txt)", "PDF (.pdf)"],
        horizontal=True,
        label_visibility="collapsed",
    )

    # Sanitize filename
    safe_name = _re_mod.sub(r'[^\w\s-]', '', thread_name).strip().replace(' ', '_')[:50]
    if not safe_name:
        safe_name = "conversation"

    if fmt == "Markdown (.md)":
        data = _export_as_markdown(thread_name, messages)
        st.download_button(
            "⬇️ Download Markdown",
            data=data,
            file_name=f"{safe_name}.md",
            mime="text/markdown",
            use_container_width=True,
        )
    elif fmt == "Plain text (.txt)":
        data = _export_as_text(thread_name, messages)
        st.download_button(
            "⬇️ Download Text",
            data=data,
            file_name=f"{safe_name}.txt",
            mime="text/plain",
            use_container_width=True,
        )
    elif fmt == "PDF (.pdf)":
        try:
            data = _export_as_pdf(thread_name, messages)
            st.download_button(
                "⬇️ Download PDF",
                data=data,
                file_name=f"{safe_name}.pdf",
                mime="application/pdf",
                use_container_width=True,
            )
        except ImportError:
            st.error("PDF export requires the `fpdf2` package.\n\n"
                    "Run: `pip install fpdf2`")
        except Exception as exc:
            st.error(f"PDF generation failed: {exc}")


# ── Helper: load chat history from checkpoint ───────────────────────────────
def load_thread_messages(thread_id: str) -> list[dict]:
    """Return list of {'role': ..., 'content': ..., 'tool_results': [...]} from the checkpointer."""
    config = {"configurable": {"thread_id": thread_id}}
    try:
        agent = get_agent_graph()
        snapshot = agent.get_state(config)
        if snapshot and snapshot.values and "messages" in snapshot.values:
            msgs = []
            pending_tool_results: list[dict] = []
            for m in snapshot.values["messages"]:
                if m.type == "tool":
                    # Collect tool results to attach to the next AI message
                    pending_tool_results.append({
                        "name": getattr(m, "name", "") or "tool",
                        "content": m.content if isinstance(m.content, str) else str(m.content),
                    })
                elif m.type == "human" and m.content:
                    pending_tool_results.clear()  # shouldn't happen, but reset
                    msgs.append({"role": "user", "content": m.content})
                elif m.type == "ai" and m.content:
                    msg_dict = {"role": "assistant", "content": m.content}
                    if pending_tool_results:
                        msg_dict["tool_results"] = pending_tool_results
                        pending_tool_results = []
                    msgs.append(msg_dict)
            return msgs
    except Exception:
        pass
    return []


# ── Helper: process attached files from chat input ──────────────────────────
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
_DATA_EXTENSIONS = {".csv", ".tsv", ".xlsx", ".xls", ".json", ".jsonl"}
_TEXT_EXTENSIONS = {".txt", ".md", ".py", ".js", ".ts", ".html",
                    ".css", ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg",
                    ".log", ".sh", ".bat", ".ps1", ".sql", ".r", ".java", ".c",
                    ".cpp", ".h", ".cs", ".go", ".rs", ".rb", ".php", ".swift",
                    ".kt", ".lua", ".pl"}
_MAX_TEXT_CHARS = 24000  # cap extracted text to avoid blowing context


def _process_attached_files(files) -> tuple[str, list[str]]:
    """Process uploaded files and return (context_text, image_b64_list).

    * Images → analysed via vision model, base64 stored for inline display
    * PDFs → text extracted via pypdf
    * CSV/Excel/JSON → parsed with pandas into schema + stats + preview
    * Text files → content read directly
    """
    context_parts: list[str] = []
    images_b64: list[str] = []

    for f in files:
        name = f.name
        suffix = pathlib.Path(name).suffix.lower()
        data = f.read()
        f.seek(0)  # reset for potential re-read

        if suffix in _IMAGE_EXTENSIONS:
            # Store base64 for inline display
            b64 = _b64.b64encode(data).decode("ascii")
            images_b64.append(b64)

            # Analyse via vision model
            vsvc = st.session_state.get("vision_service_obj")
            if vsvc and vsvc.enabled:
                description = vsvc.analyze(data, f"Describe this image in detail. The filename is '{name}'.")
                context_parts.append(f"[Attached image: {name}]\n{description}")
            else:
                context_parts.append(f"[Attached image: {name} — vision is disabled, cannot analyze]")

        elif suffix == ".pdf":
            try:
                from pypdf import PdfReader
                import io
                reader = PdfReader(io.BytesIO(data))
                pages = []
                for i, page in enumerate(reader.pages):
                    text = page.extract_text() or ""
                    if text.strip():
                        pages.append(f"--- Page {i+1} ---\n{text}")
                    if sum(len(p) for p in pages) > _MAX_TEXT_CHARS:
                        pages.append(f"[Truncated — {len(reader.pages)} pages total, showing first {i+1}]")
                        break
                content = "\n".join(pages) if pages else "(No extractable text found)"
                context_parts.append(f"[Attached PDF: {name}, {len(reader.pages)} pages]\n{content}")
            except Exception as exc:
                context_parts.append(f"[Attached PDF: {name} — failed to extract text: {exc}]")

        elif suffix in _DATA_EXTENSIONS:
            try:
                import io as _io
                from data_reader import read_data_file
                buf = _io.BytesIO(data)
                summary = read_data_file(buf, name=name, max_chars=_MAX_TEXT_CHARS)
                context_parts.append(f"[Attached data file: {name}]\n{summary}")
                # Cache raw bytes so the chart tool can re-read the file
                if "_attached_data_cache" not in st.session_state:
                    st.session_state["_attached_data_cache"] = {}
                st.session_state["_attached_data_cache"][name] = data
            except Exception as exc:
                context_parts.append(f"[Attached data file: {name} — failed to parse: {exc}]")

        elif suffix in _TEXT_EXTENSIONS:
            try:
                text = data.decode("utf-8", errors="replace")
                if len(text) > _MAX_TEXT_CHARS:
                    text = text[:_MAX_TEXT_CHARS] + f"\n[Truncated — {len(data)} bytes total]"
                context_parts.append(f"[Attached file: {name}]\n{text}")
            except Exception as exc:
                context_parts.append(f"[Attached file: {name} — failed to read: {exc}]")

        else:
            context_parts.append(f"[Attached file: {name} — unsupported file type '{suffix}']")

    return ("\n\n".join(context_parts), images_b64)


# ── Helper: run streaming loop and collect answer ───────────────────────────
_MAX_STREAM_SENTENCES = 3
_SENTENCE_SPLIT = _re_mod.compile(r'(?<=[.!?])\s+')

def _stream_events(event_generator, voice_mode: bool = False):
    """Run the streaming event loop inside a ``st.chat_message("assistant")``
    context.  Returns ``(answer_text, interrupt_data_or_None, captured_images, tool_results, chart_data)``.
    If an interrupt is returned the graph is paused awaiting user confirmation.
    ``captured_images`` is a list of base64-encoded JPEG strings from the vision
    tool (if any).  ``chart_data`` is a list of Plotly figure JSON strings."""
    status_container = st.container()
    answer_placeholder = st.empty()
    answer_tokens: list[str] = []
    active_statuses: dict[str, object] = {}
    final_answer = ""
    thinking_placeholder = None
    captured_images: list[str] = []
    tool_results: list[dict] = []
    chart_data: list[str] = []  # Plotly figure JSON strings

    # Streaming TTS: accumulate text, emit complete sentences
    _tts_buffer = ""
    _tts_in_code_block = False
    _tts_sentences_spoken = 0
    _tts_active = voice_mode and hasattr(st.session_state, 'tts_service') and st.session_state.tts_service.enabled
    if _tts_active:
        st.session_state.tts_service.stop()  # clear any previous playback

    # Show typing indicator until first meaningful event
    typing_indicator = answer_placeholder.markdown(
        '<span class="thoth-typing">Thoth is thinking<span class="dots">'
        '<span>.</span><span>.</span><span>.</span></span></span>',
        unsafe_allow_html=True,
    )
    first_event_received = False

    for event_type, payload in event_generator:
        # ── Stop requested by user ──────────────────────────────────────
        if st.session_state.stop_requested:
            if _tts_active:
                st.session_state.tts_service.stop()
            partial = "".join(answer_tokens) or final_answer
            if partial:
                answer_placeholder.markdown(partial)
            return (partial or "", None, captured_images, tool_results, chart_data)

        # Clear typing indicator on first meaningful event
        if not first_event_received:
            first_event_received = True
            answer_placeholder.empty()

        if event_type == "tool_call":
            s = status_container.status(f"Searching {payload}…", expanded=False)
            active_statuses[payload] = s

            # Announce tool usage aloud in voice mode
            if voice_mode:
                tts = st.session_state.tts_service
                if tts.enabled:
                    phrase = payload.replace("_", " ")
                    tts.speak_now(f"Ok, let me use the {phrase} tool.")

        elif event_type == "tool_done":
            tool_name = payload["name"] if isinstance(payload, dict) else payload
            tool_content = payload.get("content", "") if isinstance(payload, dict) else ""
            s = active_statuses.get(tool_name)

            # Detect chart marker in tool output and render inline
            _chart_marker_prefix = "__CHART__:"
            if tool_content and tool_content.startswith(_chart_marker_prefix):
                # Extract Plotly JSON — marker format: __CHART__:{json}\n\nChart created: ...
                marker_end = tool_content.find("\n\n", len(_chart_marker_prefix))
                if marker_end == -1:
                    fig_json = tool_content[len(_chart_marker_prefix):]
                    display_text = "Chart created"
                else:
                    fig_json = tool_content[len(_chart_marker_prefix):marker_end]
                    display_text = tool_content[marker_end + 2:]
                chart_data.append(fig_json)
                # Render the chart live during streaming
                try:
                    import plotly.io as _pio
                    fig = _pio.from_json(fig_json)
                    status_container.plotly_chart(fig, use_container_width=True)
                except Exception:
                    pass
                # Store clean text (without JSON blob) in tool_results
                tool_content = display_text
                if s:
                    s.update(label=f"✅ {tool_name}", state="complete")
                    s.markdown(f"📊 {display_text}")
            else:
                if s:
                    s.update(label=f"✅ {tool_name}", state="complete")
                    if tool_content:
                        # Truncate very long results for display
                        display = tool_content if len(tool_content) <= 5000 else tool_content[:5000] + "\n\n… (truncated)"
                        s.code(display)
                if tool_content and "[Truncated" in tool_content:
                    import re as _re_mod
                    _trunc_match = _re_mod.search(r'\[Truncated[^\]]*\]', tool_content)
                    _trunc_msg = _trunc_match.group(0)[1:-1] if _trunc_match else "File was too large to read in full"
                    status_container.warning(_trunc_msg, icon="⚠️")

            tool_results.append({"name": tool_name, "content": tool_content})

            # Show captured image inline when the vision tool completes
            if tool_name in ("👁️ Vision", "analyze_image"):
                vsvc = st.session_state.get("vision_service_obj")
                if vsvc and vsvc.last_capture:
                    b64_img = _b64.b64encode(vsvc.last_capture).decode("ascii")
                    captured_images.append(b64_img)
                    status_container.image(
                        vsvc.last_capture,
                        caption="📷 Captured from camera",
                        width=320,
                    )
                    vsvc.last_capture = None  # consume

        elif event_type == "thinking":
            if thinking_placeholder is None:
                thinking_placeholder = status_container.status(
                    "Working…", expanded=False
                )

        elif event_type == "token":
            if thinking_placeholder is not None:
                thinking_placeholder.update(
                    label="💭 Thought complete", state="complete"
                )
                thinking_placeholder = None
            answer_tokens.append(payload)
            answer_placeholder.markdown("".join(answer_tokens))

            # Streaming TTS: detect and speak complete sentences
            if _tts_active:
                # Track code blocks to skip them
                if "```" in payload:
                    _tts_in_code_block = not _tts_in_code_block
                if not _tts_in_code_block:
                    _tts_buffer += payload
                    sentences = _SENTENCE_SPLIT.split(_tts_buffer)
                    if len(sentences) > 1:
                        # All but last are complete — speak them
                        for s in sentences[:-1]:
                            if _tts_sentences_spoken >= _MAX_STREAM_SENTENCES:
                                break
                            st.session_state.tts_service.speak_streaming(s)
                            _tts_sentences_spoken += 1
                            if _tts_sentences_spoken >= _MAX_STREAM_SENTENCES:
                                st.session_state.tts_service.flush_streaming(
                                    "The full response is shown in the app."
                                )
                                _tts_active = False
                        _tts_buffer = sentences[-1]  # keep incomplete tail

        elif event_type == "interrupt":
            if thinking_placeholder is not None:
                thinking_placeholder.update(
                    label="💭 Thought complete", state="complete"
                )
            if _tts_active:
                st.session_state.tts_service.flush_streaming(_tts_buffer)
            return ("".join(answer_tokens), payload, captured_images, tool_results, chart_data)

        elif event_type == "done":
            final_answer = payload

    # Ensure final render
    if not answer_tokens and final_answer:
        answer_placeholder.markdown(final_answer)
    if thinking_placeholder is not None:
        thinking_placeholder.update(
            label="💭 Thought complete", state="complete"
        )

    answer = final_answer or "".join(answer_tokens)

    # Flush remaining TTS buffer
    if _tts_active:
        st.session_state.tts_service.flush_streaming(_tts_buffer)

    # Embed YouTube videos inline
    seen_ids = set()
    for yt_match in _YT_URL_PATTERN.finditer(answer):
        vid_id = yt_match.group(1)
        if vid_id not in seen_ids:
            seen_ids.add(vid_id)
            st.markdown(_yt_embed_html(vid_id), unsafe_allow_html=True)

    return (answer, None, captured_images, tool_results, chart_data)


# ── Session state defaults ──────────────────────────────────────────────────
if "thread_id" not in st.session_state:
    st.session_state.thread_id = None
    st.session_state.thread_name = None

if "messages" not in st.session_state:
    st.session_state.messages = []

if "uploader_key" not in st.session_state:
    st.session_state.uploader_key = 0

if "current_model" not in st.session_state:
    st.session_state.current_model = get_current_model()

if "context_size" not in st.session_state:
    st.session_state.context_size = get_context_size()

if "pending_interrupt" not in st.session_state:
    st.session_state.pending_interrupt = None

if "voice_service" not in st.session_state:
    st.session_state.voice_service = get_voice_service()

if "voice_enabled" not in st.session_state:
    st.session_state.voice_enabled = False

if "voice_status" not in st.session_state:
    st.session_state.voice_status = ""

if "voice_text" not in st.session_state:
    st.session_state.voice_text = None

if "tts_service" not in st.session_state:
    st.session_state.tts_service = TTSService()

# Wire voice service into TTS for mic gating
if st.session_state.tts_service.voice_service is None:
    st.session_state.tts_service.voice_service = st.session_state.voice_service

if "is_generating" not in st.session_state:
    st.session_state.is_generating = False

if "stop_requested" not in st.session_state:
    st.session_state.stop_requested = False

if "pending_agent_input" not in st.session_state:
    st.session_state.pending_agent_input = None

if "show_onboarding" not in st.session_state:
    st.session_state.show_onboarding = _is_first_run()

if "vision_service_obj" not in st.session_state:
    st.session_state.vision_service_obj = VisionService()
    set_vision_service(st.session_state.vision_service_obj)

# Always sync module-level model state from session state (survives reruns & refreshes)
if get_current_model() != st.session_state.current_model:
    set_model(st.session_state.current_model)

if get_context_size() != st.session_state.context_size:
    set_context_size(st.session_state.context_size)


# ═════════════════════════════════════════════════════════════════════════════
# SETTINGS DIALOG
# ═════════════════════════════════════════════════════════════════════════════
@st.dialog("⚙️ Settings", width="large")
def settings_dialog():
    tab_models, tab_tools, tab_docs, tab_fs, tab_gmail, tab_cal, tab_utils, tab_memory, tab_prefs = st.tabs(
        ["🤖 Models", "🔍 Search", "📄 Local Documents", "📁 Filesystem", "📧 Gmail", "📅 Calendar", "🔧 Utilities", "🧠 Memory", "🎛️ Preferences"]
    )

    # ── Documents tab ────────────────────────────────────────────────────────────
    with tab_docs:
        st.info(
            "Upload your own files (PDF, TXT, DOCX, etc.) to build a local knowledge base. "
            "Documents are chunked, vectorized, and stored in a local FAISS database "
            "for fast semantic search. When enabled, the agent will search these "
            "documents to answer questions about your personal content.",
            icon="📄",
        )
        docs_tool = tool_registry.get_tool("documents")
        if docs_tool:
            docs_enabled = st.toggle(
                docs_tool.display_name,
                value=tool_registry.is_enabled("documents"),
                key="toggle_documents",
                help=docs_tool.description,
            )
            if docs_enabled != tool_registry.is_enabled("documents"):
                tool_registry.set_enabled("documents", docs_enabled)

        st.divider()

        # Upload
        supported_exts = list(DocumentLoader.supported_file_types.keys())
        uploaded_files = st.file_uploader(
            "Upload documents",
            type=[ext.lstrip(".") for ext in supported_exts],
            accept_multiple_files=True,
            label_visibility="collapsed",
            key=f"uploader_{st.session_state.uploader_key}",
        )

        if uploaded_files:
            for uf in uploaded_files:
                suffix = pathlib.Path(uf.name).suffix
                with tempfile.NamedTemporaryFile(
                    delete=False, suffix=suffix, dir="."
                ) as tmp:
                    tmp.write(uf.getbuffer())
                    tmp_path = tmp.name

                try:
                    with st.spinner(f"Processing {uf.name}\u2026"):
                        load_and_vectorize_document(
                            tmp_path, skip_if_processed=False, display_name=uf.name
                        )
                    st.success(f"\u2714 {uf.name}")
                except Exception as exc:
                    st.error(f"Failed to process {uf.name}: {exc}")
                finally:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
            st.session_state.uploader_key += 1
            st.rerun()

        st.divider()

        # Indexed documents
        st.markdown("**Indexed documents**")
        processed = load_processed_files()
        if processed:
            for fp in sorted(processed):
                name = pathlib.Path(fp).name
                st.markdown(f"- 📎 {name}")
            if st.button("\U0001f5d1 Clear all documents", use_container_width=True):
                reset_vector_store()
                st.session_state.uploader_key += 1
                st.rerun()
        else:
            st.caption("No documents indexed yet.")

    # ── Models tab ───────────────────────────────────────────────────────────
    with tab_models:
        st.info(
            "Thoth uses two models: a **Brain** model for reasoning, tool use, "
            "and conversation, and a **Vision** model for camera-based image "
            "analysis. Both are served locally through Ollama.\n\n"
            "Models marked ✅ are already downloaded; ⬇️ models will be "
            "pulled automatically when selected.",
            icon="🤖",
        )

        st.subheader("🧠 Brain Model")
        st.caption(
            "The main reasoning model that powers Thoth's conversations and "
            "tool use. **Recommended: 14B+** for best accuracy. "
            "**Minimum: 8B** — smaller models may struggle with complex tasks."
        )
        all_models = list_all_models()
        local_models = list_local_models()
        current = st.session_state.current_model
        if current not in all_models:
            all_models = sorted(set(all_models + [current]))
        idx = all_models.index(current) if current in all_models else 0

        selected_model = st.selectbox(
            "Select model",
            options=all_models,
            index=idx,
            format_func=lambda m: f"{'✅' if m in local_models else '⬇️'}  {m}",
            label_visibility="collapsed",
        )

        st.divider()

        # Context window size slider
        ctx_idx = CONTEXT_SIZE_OPTIONS.index(st.session_state.context_size) \
            if st.session_state.context_size in CONTEXT_SIZE_OPTIONS else \
            CONTEXT_SIZE_OPTIONS.index(DEFAULT_CONTEXT_SIZE)

        selected_ctx = st.select_slider(
            "Context window size",
            options=CONTEXT_SIZE_OPTIONS,
            value=CONTEXT_SIZE_OPTIONS[ctx_idx],
            format_func=lambda v: CONTEXT_SIZE_LABELS.get(v, str(v)),
            help="How many tokens the model can process at once. "
                 "Larger values let the model remember more conversation history "
                 "but use more VRAM. Default: 32K.",
        )

        st.divider()

        # ── Vision model settings ────────────────────────────────────────
        st.subheader("👁️ Vision Model")
        st.caption(
            "The model used for camera and screen capture analysis — reading text, "
            "identifying objects, capturing screenshots, and answering visual questions. "
            "Runs as a separate lightweight model alongside the brain."
        )

        vsvc = st.session_state.vision_service_obj
        local_models = list_local_models()

        # Combine popular vision models with any already-local models
        all_vision = sorted(set(POPULAR_VISION_MODELS + (
            [vsvc.model] if vsvc.model not in POPULAR_VISION_MODELS else []
        )))
        v_idx = all_vision.index(vsvc.model) if vsvc.model in all_vision else 0

        selected_vision = st.selectbox(
            "Vision model",
            options=all_vision,
            index=v_idx,
            format_func=lambda m: f"{'✅' if m in local_models else '⬇️'}  {m}",
            key="vision_model_select",
            label_visibility="collapsed",
        )

        # Camera selector
        cameras = list_cameras()
        if cameras:
            cam_labels = {i: f"Camera {i}" for i in cameras}
            cam_idx = cameras.index(vsvc.camera_index) if vsvc.camera_index in cameras else 0
            selected_cam = st.selectbox(
                "Camera",
                options=cameras,
                index=cam_idx,
                format_func=lambda i: cam_labels.get(i, f"Camera {i}"),
                key="vision_cam_select",
            )
            if selected_cam != vsvc.camera_index:
                vsvc.camera_index = selected_cam
        else:
            st.caption("No cameras detected.")

        vision_enabled = st.toggle(
            "Enable vision",
            value=vsvc.enabled,
            key="vision_enabled_toggle",
            help="Allow the agent to capture images from your webcam.",
        )
        if vision_enabled != vsvc.enabled:
            vsvc.enabled = vision_enabled

    # ── Tools tab (excludes filesystem — it has its own tab) ─────────────────
    with tab_tools:
        st.info(
            "Enable or disable search and knowledge tools. "
            "Each tool gives the agent a different way to look up information — "
            "web search, Wikipedia, arXiv, YouTube, etc. "
            "Tools that require an API key will show a key input when enabled.\n\n"
            "Disabling unused tools improves accuracy by reducing the number of "
            "choices the model has to reason about.",
            icon="🔍",
        )
        for tool in tool_registry.get_all_tools():
            if tool.name in ("filesystem", "gmail", "documents", "calendar", "timer", "url_reader", "calculator", "weather", "vision"):
                continue

            enabled = st.toggle(
                tool.display_name,
                value=tool_registry.is_enabled(tool.name),
                key=f"toggle_{tool.name}",
                help=tool.description,
            )
            if enabled != tool_registry.is_enabled(tool.name):
                tool_registry.set_enabled(tool.name, enabled)

            # Tavily — collapsible setup instructions
            if tool.name == "web_search":
                with st.expander("📋 Setup Instructions"):
                    st.markdown(
                        """
**Step 1 — Create an Account**

1. Go to [app.tavily.com](https://app.tavily.com/) and sign up or sign in.

**Step 2 — Create an API Key**

2. Click the **+** button to create a new API key.
3. Enter a **Key Name** — a unique name to identify this key (e.g. *Thoth*).
4. Select **Key Type** — choose **Development** (1,000 free searches/month) or **Production** if you have a paid account.
5. Click **Create**.
6. Copy the API key from the overview page and paste it into the field below.
                        """.strip()
                    )

            # Wolfram Alpha — collapsible setup instructions
            if tool.name == "wolfram_alpha":
                with st.expander("📋 Setup Instructions"):
                    st.markdown(
                        """
**Step 1 — Create a Wolfram ID**

1. Go to the [Wolfram Alpha Developer Portal](https://developer.wolframalpha.com/) and sign up for a free Wolfram ID (or sign in if you already have one).

**Step 2 — Get an App ID**

2. Once signed in, click the **Get an AppID** button to start creating your app.
3. Give it a name (e.g. *Thoth*), a short description, and select an app type.
4. Copy the generated **AppID** and paste it into the field below.
                        """.strip()
                    )

            # Show API key inputs inline beneath the tool that needs them
            if tool.required_api_keys:
                with st.container():
                    for label, env_var in tool.required_api_keys.items():
                        current_val = get_key(env_var)
                        new_val = st.text_input(
                            label,
                            value=current_val,
                            type="password",
                            key=f"apikey_{env_var}",
                            label_visibility="visible",
                        )
                        if new_val != current_val:
                            set_key(env_var, new_val)

            # Show tool-specific config widgets from config_schema
            schema = tool.config_schema
            if schema:
                with st.container():
                    for cfg_key, spec in schema.items():
                        cfg_type = spec.get("type", "text")
                        cfg_label = spec.get("label", cfg_key)
                        cfg_default = spec.get("default")
                        current_cfg = tool.get_config(cfg_key, cfg_default)

                        if cfg_type == "text":
                            new_cfg = st.text_input(
                                cfg_label,
                                value=current_cfg or "",
                                key=f"cfg_{tool.name}_{cfg_key}",
                            )
                            if new_cfg != (current_cfg or ""):
                                tool.set_config(cfg_key, new_cfg)

                        elif cfg_type == "multicheck":
                            options = spec.get("options", [])
                            current_list = current_cfg if isinstance(current_cfg, list) else (cfg_default or [])
                            st.caption(cfg_label)
                            new_list = []
                            for opt in options:
                                checked = st.checkbox(
                                    opt,
                                    value=opt in current_list,
                                    key=f"cfg_{tool.name}_{cfg_key}_{opt}",
                                )
                                if checked:
                                    new_list.append(opt)
                            if new_list != current_list:
                                tool.set_config(cfg_key, new_list)

            st.divider()

    # ── Utilities tab ────────────────────────────────────────────────────────
    with tab_utils:
        st.info(
            "Lightweight productivity tools that extend Thoth's capabilities beyond "
            "search and knowledge retrieval — things like setting reminders, "
            "reading web pages, and other everyday tasks. ",
            icon="🔧",
        )
        _utility_tools = ["timer", "url_reader", "calculator", "weather"]
        for _uname in _utility_tools:
            _utool = tool_registry.get_tool(_uname)
            if _utool is None:
                continue
            _u_enabled = st.toggle(
                _utool.display_name,
                value=tool_registry.is_enabled(_uname),
                key=f"toggle_{_uname}",
                help=_utool.description,
            )
            if _u_enabled != tool_registry.is_enabled(_uname):
                tool_registry.set_enabled(_uname, _u_enabled)
            st.divider()

    # ── Filesystem tab ───────────────────────────────────────────────────────
    with tab_fs:
        st.info(
            "Give Thoth access to read, write, and manage files on your computer. "
            "Operations include listing directories, reading files (PDF, CSV, Excel, "
            "JSON, JSONL, TSV, and plain text), writing, copying, moving, and deleting "
            "files.\n\n"
            "**📊 Structured data:** CSV, Excel (.xlsx/.xls), JSON, JSONL, and TSV "
            "files are parsed with pandas — Thoth sees column schema, statistics, "
            "and a preview of the data. For Excel files with multiple sheets, Thoth "
            "can target a specific sheet.\n\n"
            "**🔒 Safety first:** Destructive actions (move, delete) require explicit "
            "confirmation — Thoth will always ask before proceeding. "
            "These operations are disabled by default; enable them in the "
            "operations below.\n\n"
            "Thoth can **only** access the workspace folder you select below — "
            "it has no access to any other files or directories on your system.",
            icon="📁",
        )
        fs_tool = tool_registry.get_tool("filesystem")
        if fs_tool:
            fs_enabled = st.toggle(
                "Enable Filesystem tool",
                value=tool_registry.is_enabled("filesystem"),
                key="toggle_filesystem",
                help=fs_tool.description,
            )
            if fs_enabled != tool_registry.is_enabled("filesystem"):
                tool_registry.set_enabled("filesystem", fs_enabled)

            st.divider()

            # Workspace folder with browse button
            fs_root_default = fs_tool.config_schema.get("workspace_root", {}).get("default", "")
            current_root = fs_tool.get_config("workspace_root", fs_root_default)

            col_path, col_browse = st.columns([4, 1])
            with col_path:
                new_root = st.text_input(
                    "Workspace folder",
                    value=current_root or "",
                    key="cfg_filesystem_workspace_root",
                )
            with col_browse:
                st.markdown("<br>", unsafe_allow_html=True)
                if st.button("Browse…", key="browse_filesystem_workspace_root"):
                    folder = _browse_folder("Select Workspace folder", current_root)
                    if folder:
                        new_root = folder
            if new_root != (current_root or ""):
                fs_tool.set_config("workspace_root", new_root)
                st.rerun()

            # Validate workspace path
            if current_root and not os.path.isdir(current_root):
                st.warning(f"⚠️ Folder not found: {current_root}")

            st.divider()

            # Allowed operations checkboxes
            ops_default = fs_tool.config_schema.get("selected_operations", {}).get("default", [])
            current_ops = fs_tool.get_config("selected_operations", ops_default)
            if not isinstance(current_ops, list):
                current_ops = ops_default

            from tools.filesystem_tool import ALL_OPERATIONS, _SAFE_OPS, _WRITE_OPS, _DESTRUCTIVE_OPS

            new_ops = _render_ops_checkboxes(
                [("Read-only", _SAFE_OPS), ("Write", _WRITE_OPS), ("⚠️ Destructive", _DESTRUCTIVE_OPS)],
                current_ops, "filesystem",
            )
            if new_ops != current_ops:
                fs_tool.set_config("selected_operations", new_ops)

    # ── Gmail tab ────────────────────────────────────────────────────────────
    with tab_gmail:
        gmail_tool = tool_registry.get_tool("gmail")
        if gmail_tool:
            st.info(
                "Connect Thoth to your Gmail account to search emails, read messages, "
                "view threads, create drafts, and send emails — all through natural language.\n\n"
                "**🔒 Safety first:** Sending emails requires explicit confirmation — "
                "Thoth will always ask before sending. Send is disabled by default; "
                "enable it in the operations below. "
                "Your OAuth credentials are stored locally and never leave your machine.\n\n"
                "Uses the same Google Cloud credentials as Calendar. "
                "If you've already set up Calendar, just click Authenticate below "
                "to grant Gmail access.",
                icon="📧",
            )
            gmail_enabled = st.toggle(
                "Enable Gmail tool",
                value=tool_registry.is_enabled("gmail"),
                key="toggle_gmail",
                help=gmail_tool.description,
            )
            if gmail_enabled != tool_registry.is_enabled("gmail"):
                tool_registry.set_enabled("gmail", gmail_enabled)

            with st.expander("📋 Setup Instructions"):
                st.markdown(
                    """
**Step 1 — Create a Google Cloud Project**

1. Go to [Google Cloud Console](https://console.cloud.google.com) and sign in with the Google account you want to connect.
2. Click the project dropdown (top left) → **New Project**. Name it something like *Thoth Gmail* and create it.
3. In your new project go to **APIs & Services → Library**, search for **Gmail API** and click **Enable**.

**Step 2 — Set Up OAuth Credentials**

4. Go to **APIs & Services → OAuth consent screen**. Choose **External** (or **Internal** if you have Workspace). Fill in app name, support email, and developer email. Save and continue through scopes.
5. Go to **Client → Create Client → OAuth client ID**. Choose **Desktop app**, name it *Thoth*, and create.
6. If you chose **External** (not Google Workspace), add your Gmail account as a test user: **APIs & Services → OAuth consent screen → Audience → Test users**.
7. Click the **download** button (JSON) on your new credential and save the file.

> ⚠️ Keep this file secure — it grants access to your Gmail. Don't commit it to git or share it publicly.

8. Use the **Browse…** button below to select the downloaded file (or copy it to the path shown).
9. Click **Authenticate…** to open the OAuth screen → select your Google account → accept the warning about the unverified app → OK.

> Google shows this warning for apps that haven't gone through their verification process. Since this is your own private integration, it's safe — you're authorizing your own app to access your own email.

10. Authentication is a **one-time** activity. You should then see *✅ Authenticated with Gmail*.
                    """.strip()
                )

            st.divider()

            # Credentials path with browse button
            creds_default = gmail_tool.config_schema.get("credentials_path", {}).get("default", "")
            current_creds = gmail_tool.get_config("credentials_path", creds_default)

            col_creds, col_creds_browse = st.columns([4, 1])
            with col_creds:
                new_creds = st.text_input(
                    "credentials.json path",
                    value=current_creds or "",
                    key="cfg_gmail_credentials_path",
                )
            with col_creds_browse:
                st.markdown("<br>", unsafe_allow_html=True)
                if st.button("Browse…", key="browse_gmail_credentials"):
                    creds_file = _browse_file(
                        "Select credentials.json",
                        os.path.dirname(current_creds),
                        [("JSON files", "*.json"), ("All files", "*.*")],
                    )
                    if creds_file:
                        new_creds = creds_file
            if new_creds != (current_creds or ""):
                gmail_tool.set_config("credentials_path", new_creds)
                st.rerun()

            # Auth status
            st.divider()
            if gmail_tool.has_credentials_file():
                if gmail_tool.is_authenticated():
                    st.success("✅ Authenticated with Gmail")
                else:
                    st.warning("🔑 credentials.json found but not yet authenticated.")
                    if st.button("Authenticate…", key="btn_gmail_auth"):
                        try:
                            gmail_tool.authenticate()
                            st.success("✅ Authenticated successfully!")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Authentication failed: {e}")
            else:
                st.info(
                    "Place your Google OAuth **credentials.json** at the path above "
                    "to enable Gmail integration."
                )

            # Allowed operations
            st.divider()
            from tools.gmail_tool import ALL_OPERATIONS, _READ_OPS, _COMPOSE_OPS, _SEND_OPS

            gmail_ops_default = gmail_tool.config_schema.get("selected_operations", {}).get("default", [])
            current_gmail_ops = gmail_tool.get_config("selected_operations", gmail_ops_default)
            if not isinstance(current_gmail_ops, list):
                current_gmail_ops = gmail_ops_default

            new_gmail_ops = _render_ops_checkboxes(
                [("Read", _READ_OPS), ("Compose", _COMPOSE_OPS), ("⚠️ Send", _SEND_OPS)],
                current_gmail_ops, "gmail",
            )
            if new_gmail_ops != current_gmail_ops:
                gmail_tool.set_config("selected_operations", new_gmail_ops)

    # ── Calendar tab ───────────────────────────────────────────────────────────
    with tab_cal:
        cal_tool = tool_registry.get_tool("calendar")
        if cal_tool:
            st.info(
                "Connect Thoth to Google Calendar to search events, create reminders, "
                "and manage your schedule through natural language.\n\n"
                "**🔒 Safety first:** Destructive actions (move, delete events) require "
                "explicit confirmation — Thoth will always ask before proceeding. "
                "These operations are disabled by default; enable them in the "
                "operations below. "
                "Your OAuth credentials are stored locally and never leave your machine.\n\n"
                "Uses the same Google Cloud credentials as Gmail. "
                "If you've already set up Gmail, just click Authenticate below "
                "to grant calendar access.",
                icon="📅",
            )
            cal_enabled = st.toggle(
                "Enable Google Calendar tool",
                value=tool_registry.is_enabled("calendar"),
                key="toggle_calendar",
                help=cal_tool.description,
            )
            if cal_enabled != tool_registry.is_enabled("calendar"):
                tool_registry.set_enabled("calendar", cal_enabled)

            with st.expander("📋 Setup Instructions"):
                st.markdown(
                    """
If you already set up Gmail, you can **skip to step 3** below — the same \
credentials.json works for Calendar.

**First-time setup** (if you haven’t configured Gmail yet):

1. Go to [Google Cloud Console](https://console.cloud.google.com) and create a project (or reuse your existing one).
2. Enable the **Google Calendar API** in **APIs & Services → Library**.
3. Ensure you have an **OAuth client ID** (Desktop app) — if you set one up for Gmail, it’s the same one.
4. If using External OAuth, make sure your account is listed as a **test user** in **OAuth consent screen → Audience → Test users**.
5. Point the credentials path below to your **credentials.json** file.
6. Click **Authenticate…** — a browser window will open for the Calendar consent screen.
7. Authentication is a **one-time** activity. You should then see *✅ Authenticated with Calendar*.
                    """.strip()
                )

            st.divider()

            # Credentials path with browse button
            cal_creds_default = cal_tool.config_schema.get("credentials_path", {}).get("default", "")
            current_cal_creds = cal_tool.get_config("credentials_path", cal_creds_default)

            col_cal_creds, col_cal_browse = st.columns([4, 1])
            with col_cal_creds:
                new_cal_creds = st.text_input(
                    "credentials.json path",
                    value=current_cal_creds or "",
                    key="cfg_calendar_credentials_path",
                )
            with col_cal_browse:
                st.markdown("<br>", unsafe_allow_html=True)
                if st.button("Browse…", key="browse_calendar_credentials"):
                    cal_creds_file = _browse_file(
                        "Select credentials.json",
                        os.path.dirname(current_cal_creds),
                        [("JSON files", "*.json"), ("All files", "*.*")],
                    )
                    if cal_creds_file:
                        new_cal_creds = cal_creds_file
            if new_cal_creds != (current_cal_creds or ""):
                cal_tool.set_config("credentials_path", new_cal_creds)
                st.rerun()

            # Auth status
            st.divider()
            if cal_tool.has_credentials_file():
                if cal_tool.is_authenticated():
                    st.success("✅ Authenticated with Calendar")
                else:
                    st.warning("🔑 credentials.json found but not yet authenticated.")
                    if st.button("Authenticate…", key="btn_calendar_auth"):
                        try:
                            cal_tool.authenticate()
                            st.success("✅ Authenticated successfully!")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Authentication failed: {e}")
            else:
                st.info(
                    "Point the path above to your Google OAuth **credentials.json** "
                    "to enable Calendar integration."
                )

            # Allowed operations
            st.divider()
            from tools.calendar_tool import (
                ALL_OPERATIONS as CAL_ALL_OPS,
                _READ_OPS as CAL_READ_OPS,
                _WRITE_OPS as CAL_WRITE_OPS,
                _DESTRUCTIVE_OPS as CAL_DESTRUCTIVE_OPS,
            )

            cal_ops_default = cal_tool.config_schema.get("selected_operations", {}).get("default", [])
            current_cal_ops = cal_tool.get_config("selected_operations", cal_ops_default)
            if not isinstance(current_cal_ops, list):
                current_cal_ops = cal_ops_default

            new_cal_ops = _render_ops_checkboxes(
                [("Read", CAL_READ_OPS), ("Write", CAL_WRITE_OPS), ("⚠️ Destructive", CAL_DESTRUCTIVE_OPS)],
                current_cal_ops, "calendar",
            )
            if new_cal_ops != current_cal_ops:
                cal_tool.set_config("selected_operations", new_cal_ops)

    # ── Memory tab ──────────────────────────────────────────────────────────
    with tab_memory:
        import memory as memory_db

        st.info(
            "Thoth can remember personal details you share across conversations — "
            "names, birthdays, preferences, important facts, and more. Memories "
            "are stored locally and never sent to any cloud service.\n\n"
            "The agent saves memories automatically when you share something "
            "worth remembering. You can also ask it to recall, update, or "
            "forget specific memories in chat.",
            icon="🧠",
        )

        mem_tool = tool_registry.get_tool("memory")
        if mem_tool:
            mem_enabled = tool_registry.is_enabled("memory")
            new_mem_enabled = st.toggle(
                "Enable Memory",
                value=mem_enabled,
                key="memory_tool_toggle",
                help="When enabled, the agent can save and recall long-term memories.",
            )
            if new_mem_enabled != mem_enabled:
                tool_registry.set_enabled("memory", new_mem_enabled)

        st.divider()

        total = memory_db.count_memories()
        st.markdown(f"**Stored memories:** {total}")

        if total > 0:
            # Category filter
            _cat_options = ["All"] + sorted(memory_db.VALID_CATEGORIES)
            selected_cat = st.selectbox(
                "Filter by category",
                options=_cat_options,
                key="memory_cat_filter",
            )
            cat_filter = None if selected_cat == "All" else selected_cat

            # Search box
            search_q = st.text_input(
                "Search memories",
                placeholder="Type a keyword…",
                key="memory_search_input",
            )

            if search_q:
                memories = memory_db.search_memories(search_q, category=cat_filter)
            else:
                memories = memory_db.list_memories(category=cat_filter)

            if not memories:
                st.caption("No matching memories.")
            else:
                for mem in memories:
                    with st.expander(
                        f"**{mem['subject']}** — _{mem['category']}_",
                    ):
                        st.markdown(mem["content"])
                        _tag_str = mem.get("tags", "")
                        if _tag_str:
                            st.caption(f"Tags: {_tag_str}")
                        st.caption(
                            f"ID: `{mem['id']}` · "
                            f"Created: {mem['created_at'][:16]} · "
                            f"Updated: {mem['updated_at'][:16]}"
                        )
                        if st.button(
                            "🗑️ Delete",
                            key=f"mem_del_{mem['id']}",
                        ):
                            memory_db.delete_memory(mem["id"])
                            st.rerun()

            # Delete all memories
            st.divider()
            with st.popover("🗑️ Delete all memories", use_container_width=True):
                st.warning("⚠️ This will permanently delete **all** stored memories. This cannot be undone.")
                if st.button(
                    "Yes, delete all memories",
                    use_container_width=True,
                    type="primary",
                    key="confirm_delete_all_memories_btn",
                ):
                    memory_db.delete_all_memories()
                    st.rerun()

    # ── Preferences tab ─────────────────────────────────────────────────────
    with tab_prefs:
        st.subheader("🎤 Voice Input")
        st.info(
            "Enable voice input to talk to Thoth using a wake word. "
            "When enabled, Thoth listens for a wake word (e.g. \"Hey Jarvis\") "
            "and then transcribes your speech using a local Whisper model. "
            "Everything runs locally — no audio is sent to the cloud.\n\n"
            "**Requirements:** A working microphone connected to this computer.",
            icon="🎤",
        )

        voice_svc = st.session_state.voice_service

        # Wake word model selector
        available_wake = get_available_wake_models()
        if available_wake:
            wake_labels = {m: m.replace("_", " ").replace("v0.1", "").strip().title() for m in available_wake}
            current_wake = voice_svc.wake_model
            if current_wake not in available_wake and available_wake:
                current_wake = available_wake[0]
            selected_wake = st.selectbox(
                "Wake word",
                options=available_wake,
                index=available_wake.index(current_wake) if current_wake in available_wake else 0,
                format_func=lambda m: wake_labels.get(m, m),
                help="The phrase you say to activate voice input.",
            )
            if selected_wake != voice_svc.wake_model:
                voice_svc.wake_model = selected_wake
        else:
            st.warning("No wake-word models found. They will be downloaded automatically when voice is first enabled.")

        # Whisper model size
        whisper_sizes = get_available_whisper_sizes()
        whisper_labels = {"tiny": "Tiny (~39 MB, fastest)", "base": "Base (~74 MB, balanced)", "small": "Small (~244 MB, best accuracy)"}
        current_whisper = voice_svc.whisper_size
        selected_whisper = st.selectbox(
            "Whisper model size",
            options=whisper_sizes,
            index=whisper_sizes.index(current_whisper) if current_whisper in whisper_sizes else 1,
            format_func=lambda s: whisper_labels.get(s, s),
            help="Larger models are more accurate but use more RAM and are slower. Downloaded on first use.",
        )
        if selected_whisper != voice_svc.whisper_size:
            voice_svc.whisper_size = selected_whisper

        # Wake threshold slider
        new_threshold = st.slider(
            "Wake word sensitivity",
            min_value=0.1,
            max_value=0.95,
            value=voice_svc.wake_threshold,
            step=0.05,
            help="Lower = more sensitive (may trigger on background noise). Higher = stricter (may miss soft speech).",
        )
        if new_threshold != voice_svc.wake_threshold:
            voice_svc.wake_threshold = new_threshold

        st.divider()

        # ── Text-to-Speech ────────────────────────────────────────────────
        st.subheader("🔊 Text-to-Speech")
        st.info(
            "Enable text-to-speech to hear Thoth read responses aloud. "
            "When paired with voice input, this creates a fully hands-free experience. "
            "Short responses are read in full; longer responses are summarized aloud "
            "with the full text shown in the app.\n\n"
            "Uses Piper TTS — a fast, high-quality neural speech engine. "
            "Everything runs locally, no audio is sent to the cloud.\n\n"
            "**Setup:** Click *Install Piper TTS* below to download the speech engine "
            "and a default voice (~50 MB total).",
            icon="🔊",
        )

        tts = st.session_state.tts_service

        if not tts.is_piper_installed():
            if st.button("⬇️ Install Piper TTS", use_container_width=True, key="btn_install_piper"):
                with st.status("Downloading Piper TTS engine…", expanded=True) as dl_status:
                    progress_bar = st.progress(0.0)
                    tts.download_piper(progress=lambda p: progress_bar.progress(p))
                    dl_status.update(label="Downloading default voice…")
                    progress_bar.progress(0.0)
                    tts.download_voice(progress=lambda p: progress_bar.progress(p))
                    dl_status.update(label="✅ Piper TTS installed!", state="complete")
                st.rerun()
        else:
            # Enable toggle
            tts_enabled = st.toggle(
                "Enable text-to-speech",
                value=tts.enabled,
                key="tts_enabled_toggle",
            )
            if tts_enabled != tts.enabled:
                tts.enabled = tts_enabled

            # Voice selector
            installed_voices = tts.get_installed_voices()
            if installed_voices:
                current_tts_voice = tts.voice
                if current_tts_voice not in installed_voices:
                    current_tts_voice = installed_voices[0]
                selected_tts_voice = st.selectbox(
                    "Voice",
                    options=installed_voices,
                    index=installed_voices.index(current_tts_voice) if current_tts_voice in installed_voices else 0,
                    format_func=lambda v: VOICE_CATALOG.get(v, v),
                    key="tts_voice_select",
                )
                if selected_tts_voice != tts.voice:
                    tts.voice = selected_tts_voice
            else:
                st.caption("No voices installed yet.")

            # Download additional voices
            not_installed = [v for v in VOICE_CATALOG if v not in installed_voices]
            if not_installed:
                with st.expander("⬇️ Download additional voices"):
                    for vid in not_installed:
                        col_vname, col_vdl = st.columns([3, 1])
                        with col_vname:
                            st.caption(VOICE_CATALOG[vid])
                        with col_vdl:
                            if st.button("Download", key=f"dl_voice_{vid}"):
                                with st.spinner(f"Downloading {VOICE_CATALOG[vid]}…"):
                                    tts.download_voice(vid)
                                st.rerun()

            # Speed slider
            new_tts_speed = st.slider(
                "Speech speed",
                min_value=0.5,
                max_value=2.0,
                value=tts.speed,
                step=0.1,
                format="%.1fx",
                key="tts_speed_slider",
            )
            if new_tts_speed != tts.speed:
                tts.speed = new_tts_speed

            # Auto-speak toggle
            tts_auto = st.toggle(
                "Auto-speak voice responses",
                value=tts.auto_speak,
                help="Automatically read responses aloud when using voice input.",
                key="tts_auto_speak_toggle",
            )
            if tts_auto != tts.auto_speak:
                tts.auto_speak = tts_auto

            # Test button
            if st.button("🔊 Test voice", key="btn_test_tts"):
                tts.speak_now(
                    "Hello! I'm Thoth, your knowledgeable personal agent. "
                    "How can I help you today?"
                )

    # ── Handle model switch inside dialog ────────────────────────────────────
    if selected_model and selected_model != st.session_state.current_model:
        if not is_model_local(selected_model):
            _pull_model_with_progress(selected_model)
        set_model(selected_model)
        st.session_state.current_model = selected_model
        clear_agent_cache()
        st.rerun()

    # ── Handle context size change ───────────────────────────────────────────
    if selected_ctx != st.session_state.context_size:
        set_context_size(selected_ctx)
        st.session_state.context_size = selected_ctx
        clear_agent_cache()
        st.rerun()

    # ── Handle vision model switch ───────────────────────────────────────────
    if selected_vision and selected_vision != vsvc.model:
        if not is_model_local(selected_vision):
            _pull_model_with_progress(selected_vision)
        vsvc.model = selected_vision
        clear_agent_cache()
        st.rerun()


# ═════════════════════════════════════════════════════════════════════════════
# LEFT SIDEBAR – Thread Manager
# ═════════════════════════════════════════════════════════════════════════════
with st.sidebar:
    st.markdown('<h1 style="color: #FFD700;">𓁟 Thoth</h1>', unsafe_allow_html=True)
    st.caption("God of Wisdom, Writing, and Knowledge\nYour Knowledgeable Personal Agent")
    st.divider()

    # New thread button
    if st.button("＋  New conversation", use_container_width=True, type="primary"):
        tid = uuid.uuid4().hex[:12]
        name = f"Thread {datetime.now().strftime('%b %d, %H:%M')}"
        _save_thread_meta(tid, name)
        st.session_state.thread_id = tid
        st.session_state.thread_name = name
        st.session_state.messages = []
        st.rerun()

    st.markdown("#### Conversations")

    threads = _list_threads()
    if not threads:
        st.info("No conversations yet.")

    def _render_thread_row(tid: str, name: str, key_prefix: str) -> None:
        """Render a single thread row with select + delete buttons."""
        is_active = tid == st.session_state.thread_id
        col_t, col_d = st.columns([5, 1])
        with col_t:
            label = f"{'\u25b8 ' if is_active else ''}{name}"
            if st.button(
                label,
                key=f"{key_prefix}_{tid}",
                use_container_width=True,
                type="secondary" if not is_active else "primary",
            ):
                st.session_state.thread_id = tid
                st.session_state.thread_name = name
                st.session_state.messages = load_thread_messages(tid)
                st.rerun()
        with col_d:
            if st.button("\U0001f5d1", key=f"{key_prefix}_del_{tid}", help=f"Delete {name}"):
                _delete_thread(tid)
                if st.session_state.thread_id == tid:
                    st.session_state.thread_id = None
                    st.session_state.thread_name = None
                    st.session_state.messages = []
                st.rerun()

    _SIDEBAR_MAX_THREADS = 5
    visible_threads = threads[:_SIDEBAR_MAX_THREADS]
    has_more = len(threads) > _SIDEBAR_MAX_THREADS

    for tid, name, created, updated in visible_threads:
        _render_thread_row(tid, name, "thread")

    if has_more:
        @st.dialog("📋 All Conversations")
        def _all_threads_dialog():
            for tid, name, created, updated in threads:
                _render_thread_row(tid, name, "allthread")

            # ── Delete all conversations (popover avoids dialog-closing rerun) ─
            st.divider()
            with st.popover("🗑️ Delete all conversations", use_container_width=True):
                st.warning("⚠️ This will permanently delete **all** conversations. This cannot be undone.")
                if st.button("Yes, delete all", use_container_width=True, type="primary", key="confirm_delete_all_btn"):
                    for tid, _name, _c, _u in threads:
                        _delete_thread(tid)
                    st.session_state.thread_id = None
                    st.session_state.thread_name = None
                    st.session_state.messages = []
                    st.rerun()

        if st.button(f"Show all ({len(threads)})", use_container_width=True):
            _all_threads_dialog()

    # ── Buttons pinned to bottom of sidebar ───────────────────────────────────
    st.markdown(
        """<div style="position: fixed; bottom: 1rem; width: inherit; z-index: 200;">""",
        unsafe_allow_html=True,
    )

    # ── Token usage counter ───────────────────────────────────────────────────
    _cfg = {"configurable": {"thread_id": st.session_state.thread_id}} if st.session_state.get("thread_id") else None
    _used, _max = get_token_usage(_cfg)
    _pct = min(_used / _max, 1.0) if _max else 0.0
    if _max >= 1_000:
        _used_label = f"{_used / 1_000:.1f}K"
        _max_label = f"{_max / 1_000:.0f}K"
    else:
        _used_label = str(_used)
        _max_label = str(_max)
    st.markdown(
        f'<p style="margin:0 0 2px 0; font-size:0.78rem; color:#B8A04A;">'
        f'Context: {_used_label} / {_max_label} tokens ({_pct:.0%})</p>',
        unsafe_allow_html=True,
    )
    st.markdown(
        f'<div style="background:#333; border-radius:4px; height:6px; margin-bottom:8px;">'
        f'<div style="background:#DAA520; width:{_pct:.0%}; height:100%; border-radius:4px;"></div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    _btn_col_settings, _btn_col_help = st.columns([5, 1])
    with _btn_col_settings:
        if st.button("⚙️ Settings", use_container_width=True):
            settings_dialog()
    with _btn_col_help:
        if st.button("?", key="help_btn", help="Show what Thoth can do"):
            st.session_state.show_onboarding = True
    st.markdown("</div>", unsafe_allow_html=True)


# ═════════════════════════════════════════════════════════════════════════════
# MAIN AREA – Chat
# ═════════════════════════════════════════════════════════════════════════════

if st.session_state.thread_id is None:
    # ── Empty state: show onboarding or simple prompt ────────────────────
    if st.session_state.show_onboarding:
        st.markdown(
            '<div style="text-align:center; padding-top:2rem;">'
            '<h1 style="color: #FFD700;">𓁟 Thoth</h1></div>',
            unsafe_allow_html=True,
        )
        with st.chat_message("assistant"):
            st.markdown(_WELCOME_MESSAGE)
            cols = st.columns(3)
            for i, prompt in enumerate(_EXAMPLE_PROMPTS):
                with cols[i % 3]:
                    if st.button(prompt, key=f"onb_try_{i}", use_container_width=True):
                        # Create a new thread and send this prompt
                        _onb_tid = uuid.uuid4().hex[:12]
                        _onb_name = prompt[:50]
                        _save_thread_meta(_onb_tid, _onb_name)
                        st.session_state.thread_id = _onb_tid
                        st.session_state.thread_name = _onb_name
                        st.session_state.messages = [{"role": "user", "content": prompt}]
                        st.session_state.pending_agent_input = {"input": prompt, "voice_mode": False}
                        st.session_state.is_generating = True
                        st.session_state.show_onboarding = False
                        _mark_onboarding_seen()
                        st.rerun()
        # Mark as seen after first display
        if _is_first_run():
            _mark_onboarding_seen()
    else:
        st.markdown(
            """
            <div style='text-align:center; padding-top: 8rem;'>
                <h1 style="color: #FFD700;">𓁟 Thoth</h1>
                <p style='font-size:1.15rem; color: #888;'>
                    Select a conversation from the sidebar or start a new one.
                </p>
            </div>
            """,
            unsafe_allow_html=True,
        )
else:
    _hdr_col, _export_col = st.columns([10, 1])
    with _hdr_col:
        st.markdown(f"### 💬 {st.session_state.thread_name}")
    with _export_col:
        if st.session_state.messages:
            if st.button("📤", key="export_btn", help="Export conversation"):
                _export_dialog()

    # ── Consume voice transcription (add to messages, defer agent call) ──
    _voice_pending = None
    if st.session_state.voice_enabled and st.session_state.voice_text:
        # Stop any in-progress TTS so the assistant doesn't talk over the user
        if hasattr(st.session_state, 'tts_service') and st.session_state.tts_service.enabled:
            st.session_state.tts_service.stop()
            st.session_state.tts_service.speak_now("Ok. Working.")
        _voice_pending = st.session_state.voice_text
        st.session_state.voice_text = None  # consume it

        # Append user message so it renders in the loop below
        st.session_state.messages.append(
            {"role": "user", "content": _voice_pending}
        )
        # Auto-name thread from first user message
        if st.session_state.thread_name and st.session_state.thread_name.startswith("Thread "):
            new_name = _voice_pending[:50]
            st.session_state.thread_name = new_name
            _save_thread_meta(st.session_state.thread_id, new_name)

    # ── Handle stop (run was interrupted by stop button click) ────────
    if st.session_state.stop_requested:
        _tts = st.session_state.get("tts_service")
        if _tts and _tts.enabled:
            _tts.stop()
            # User pressed stop → let them speak next (enter follow-up)
            _vsvc = st.session_state.get("voice_service")
            if _vsvc and _vsvc.is_running:
                _vsvc.enter_follow_up()
        if st.session_state.is_generating:
            # Generation was in progress — clear pending input
            st.session_state.pending_agent_input = None
            # Repair checkpoint: add dummy ToolMessages for orphaned tool_calls
            if st.session_state.thread_id:
                _stop_config = {"configurable": {"thread_id": st.session_state.thread_id}}
                _stop_tools = [t.name for t in tool_registry.get_enabled_tools()]
                repair_orphaned_tool_calls(_stop_tools, _stop_config)
        st.session_state.stop_requested = False
        st.session_state.is_generating = False

    # Render existing messages
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            _render_message(msg["content"], images=msg.get("images"), tool_results=msg.get("tool_results"), role=msg["role"], charts=msg.get("charts"))

    # ── In-thread onboarding (triggered by ? button) ────────────────────
    if st.session_state.show_onboarding:
        with st.chat_message("assistant"):
            st.markdown(_WELCOME_MESSAGE)
            _onb_cols = st.columns(3)
            for _oi, _op in enumerate(_EXAMPLE_PROMPTS):
                with _onb_cols[_oi % 3]:
                    if st.button(_op, key=f"onb_inline_{_oi}", use_container_width=True):
                        st.session_state.messages.append({"role": "user", "content": _op})
                        st.session_state.pending_agent_input = {"input": _op, "voice_mode": False}
                        st.session_state.is_generating = True
                        st.session_state.show_onboarding = False
                        if st.session_state.thread_name and st.session_state.thread_name.startswith("Thread "):
                            _save_thread_meta(st.session_state.thread_id, _op[:50])
                            st.session_state.thread_name = _op[:50]
                        st.rerun()
            if st.button("✕ Dismiss", key="dismiss_onboarding"):
                st.session_state.show_onboarding = False
                st.rerun()

    # Auto-scroll chat to bottom after messages render
    _scroll_to_bottom()

    # ── Pending interrupt: show confirmation UI ──────────────────────────
    if st.session_state.pending_interrupt is not None:
        interrupt_data = st.session_state.pending_interrupt
        st.warning(
            f"**Confirmation required**\n\n{interrupt_data['description']}",
            icon="⚠️",
        )
        col_a, col_d, _ = st.columns([1, 1, 4])
        with col_a:
            approved = st.button(
                "✅ Approve", type="primary",
                use_container_width=True, key="interrupt_approve",
            )
        with col_d:
            denied = st.button(
                "❌ Deny",
                use_container_width=True, key="interrupt_deny",
            )

        if approved or denied:
            st.session_state.pending_interrupt = None
            st.session_state.pending_agent_input = {
                "resume": True, "approved": approved,
            }
            st.session_state.is_generating = True
            st.rerun()

    # ── Defer voice input for processing after stop button renders ────
    if _voice_pending:
        _save_thread_meta(
            st.session_state.thread_id, st.session_state.thread_name
        )
        st.session_state.pending_agent_input = {
            "input": _voice_pending, "voice_mode": True,
        }
        st.session_state.is_generating = True
        st.rerun()

    # ── Voice controls (pinned above chat input) ────────────────────────
    voice_svc = st.session_state.voice_service
    with st.container(key="voice_bar"):
        vcol_toggle, vcol_status = st.columns([1, 4])
        with vcol_toggle:
            voice_on = st.toggle(
                "🎤 Voice",
                value=st.session_state.voice_enabled,
                key="voice_toggle_main",
            )
            if voice_on != st.session_state.voice_enabled:
                st.session_state.voice_enabled = voice_on
                if voice_on:
                    voice_svc.start()
                else:
                    voice_svc.stop()
                st.rerun()
        with vcol_status:
            if st.session_state.voice_enabled:
                @st.fragment(run_every=0.25)
                def _voice_poll():
                    svc = st.session_state.voice_service
                    # Send heartbeat so voice knows the browser tab is open
                    svc.update_heartbeat()
                    # Drain status queue
                    new_status = svc.get_status()
                    if new_status:
                        st.session_state.voice_status = new_status
                    # Render live status
                    state = svc.state
                    if state == "sleeping":
                        st.caption(f"💤 {st.session_state.voice_status or 'Waiting for wake word…'}")
                    elif state == "listening":
                        st.caption("🔴 Listening — speak now…")
                    elif state == "transcribing":
                        st.caption("⏳ Transcribing…")
                    elif state == "muted":
                        st.caption("🔇 Speaking…")
                    elif state == "follow_up":
                        st.caption("👂 Listening (no wake word needed)…")
                    elif state == "stopped":
                        st.caption(f"⚫ {st.session_state.voice_status or 'Stopped'}")
                        if st.session_state.voice_enabled:
                            svc.start()
                    # Check for completed transcription
                    text = svc.get_transcription()
                    if text:
                        st.session_state.voice_text = text
                        st.rerun(scope="app")

                _voice_poll()

    # ── Normal chat input ────────────────────────────────────────────────
    _FILE_TYPES = sorted(
        [ext.lstrip(".") for ext in _IMAGE_EXTENSIONS | _TEXT_EXTENSIONS | _DATA_EXTENSIONS | {".pdf"}]
    )
    # ── Stop button (always visible, greyed out when idle) ────────────
    def _on_stop_click():
        st.session_state.stop_requested = True

    with st.container(key="stop_btn"):
        _tts_svc = st.session_state.get("tts_service")
        _stop_enabled = st.session_state.is_generating or (
            _tts_svc is not None and _tts_svc.is_speaking
        )
        st.button(
            "\u23f9",
            on_click=_on_stop_click,
            disabled=not _stop_enabled,
            help="Stop generation",
        )

    # ── Process deferred agent input (runs AFTER stop button is rendered) ─
    _pending = st.session_state.pending_agent_input
    if _pending and not st.session_state.stop_requested:
        st.session_state.pending_agent_input = None
        config = {
            "configurable": {"thread_id": st.session_state.thread_id},
            "recursion_limit": 25,
        }
        enabled_tools = [t.name for t in tool_registry.get_enabled_tools()]
        voice_mode = _pending.get("voice_mode", False)

        with st.chat_message("assistant"):
            _scroll_to_bottom()
            try:
                if _pending.get("resume"):
                    answer, interrupt_data, cap_imgs, tool_res, charts = _stream_events(
                        resume_stream_agent(
                            enabled_tools, config, _pending["approved"]
                        )
                    )
                else:
                    answer, interrupt_data, cap_imgs, tool_res, charts = _stream_events(
                        stream_agent(_pending["input"], enabled_tools, config),
                        voice_mode=voice_mode,
                    )
            except Exception as _agent_exc:
                # Catch GraphRecursionError and any other streaming crashes.
                # Repair the checkpoint so the thread is not permanently corrupted.
                _exc_name = type(_agent_exc).__name__
                if "RecursionError" in _exc_name or "Recursion" in str(_agent_exc):
                    answer = ("⚠️ I got stuck in a tool loop and had to stop. "
                              "Please try rephrasing your request or starting a new thread.")
                else:
                    answer = f"⚠️ An error occurred: {_agent_exc}"
                interrupt_data = None
                cap_imgs = []
                tool_res = []
                charts = []
                st.markdown(answer)
                # Repair orphaned tool calls so the thread stays usable
                try:
                    repair_orphaned_tool_calls(enabled_tools, config)
                except Exception:
                    pass

        st.session_state.is_generating = False
        st.session_state.stop_requested = False

        if interrupt_data:
            st.session_state.pending_interrupt = interrupt_data

        msg = {"role": "assistant", "content": answer}
        if cap_imgs:
            msg["images"] = cap_imgs
        if tool_res:
            msg["tool_results"] = tool_res
        if charts:
            msg["charts"] = charts
        st.session_state.messages.append(msg)

        st.rerun()

    if chat_value := st.chat_input(
        "Ask anything or attach files…",
        accept_file="multiple",
        file_type=_FILE_TYPES,
    ):
        # Unpack: chat_value is a ChatInputValue with .text and .files
        user_input = chat_value.text if hasattr(chat_value, "text") else str(chat_value)
        attached_files = getattr(chat_value, "files", []) or []

        # Process attached files
        file_context = ""
        user_images: list[str] = []
        if attached_files:
            file_context, user_images = _process_attached_files(attached_files)

        # Build the message sent to the agent (text + file context)
        agent_input = user_input
        if file_context:
            agent_input = f"{file_context}\n\n{user_input}" if user_input else file_context

        # Build user message for display (just show the typed text + image thumbs)
        display_content = user_input
        if attached_files and not user_input:
            display_content = ", ".join(f"📎 {f.name}" for f in attached_files)
        elif attached_files:
            file_badges = ", ".join(f"📎 {f.name}" for f in attached_files)
            display_content = f"{file_badges}\n\n{user_input}"

        user_msg = {"role": "user", "content": display_content}
        if user_images:
            user_msg["images"] = user_images
        st.session_state.messages.append(user_msg)

        # Auto-name thread from first user message
        if st.session_state.thread_name and st.session_state.thread_name.startswith("Thread "):
            new_name = display_content[:50]
            st.session_state.thread_name = new_name
            _save_thread_meta(st.session_state.thread_id, new_name)
        else:
            _save_thread_meta(
                st.session_state.thread_id, st.session_state.thread_name
            )
        st.session_state.pending_agent_input = {
            "input": agent_input, "voice_mode": False,
        }
        st.session_state.is_generating = True
        st.rerun()