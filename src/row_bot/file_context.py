"""Headless attachment ingestion shared by client services and the legacy UI.

Only explicit send processing writes attachment bytes or invokes a supplied
vision service. Importing this module does not initialize presentation state.
"""
from __future__ import annotations

import base64 as _b64
import io
import logging
import pathlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from row_bot.vision import VisionService

logger = logging.getLogger(__name__)

ATTACHMENT_CONTEXT_START = "<row_bot_attachment_context>"
ATTACHMENT_CONTEXT_END = "</row_bot_attachment_context>"

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
DATA_EXTENSIONS = {".csv", ".tsv", ".xlsx", ".xls", ".json", ".jsonl"}
TEXT_EXTENSIONS = {
    ".txt", ".md", ".py", ".js", ".ts", ".html", ".css", ".xml", ".yaml",
    ".yml", ".toml", ".ini", ".cfg", ".log", ".sh", ".bat", ".ps1", ".sql",
    ".r", ".java", ".c", ".cpp", ".h", ".cs", ".go", ".rs", ".rb", ".php",
    ".swift", ".kt", ".lua", ".pl",
}
CHARS_PER_TOKEN_APPROX = 3  # used only for file-size char budgets


def file_budget(model_name: str | None = None) -> int:
    """Dynamic char budget for attached files: 35 % of the model's context window.

    For 32K context →  ~28K chars (7K tokens)
    For 128K context → ~114K chars (28K tokens)
    Falls back to 40K chars if context size is unavailable.
    """
    from row_bot.models import get_context_size

    try:
        ctx = get_context_size(model_name)
    except Exception:
        ctx = 32_768
    return int(ctx * 0.35 * CHARS_PER_TOKEN_APPROX)

def wrap_attachment_context(context: str) -> str:
    """Mark attachment context so transcript reload can strip it cleanly."""
    context = str(context or "").strip()
    if not context:
        return ""
    return f"{ATTACHMENT_CONTEXT_START}\n{context}\n{ATTACHMENT_CONTEXT_END}"

def materialize_chat_attachments(files: list[dict]) -> list[dict]:
    """Persist chat attachments and copy them into the workspace.

    Each file dict is updated with ``workspace_path`` when copying succeeds.
    """
    if not files:
        return []

    from row_bot.channels.media import copy_to_workspace, save_inbound_file

    manifest: list[dict] = []
    for f in files:
        name = str(f.get("name") or "attachment")
        data = f.get("data", b"")
        if not isinstance(data, (bytes, bytearray)):
            manifest.append({"name": name, "workspace_path": None, "error": "missing bytes"})
            continue
        try:
            saved = save_inbound_file(bytes(data), name)
            ws_rel = copy_to_workspace(saved, workspace_filename=name)
            if ws_rel:
                f["workspace_path"] = ws_rel
            f["saved_path"] = str(saved)
            manifest.append({
                "name": name,
                "saved_path": str(saved),
                "workspace_path": ws_rel,
                "size": len(data),
                "suffix": pathlib.Path(name).suffix.lower(),
            })
        except Exception as exc:
            logger.warning("Failed to materialize chat attachment %s: %s", name, exc)
            manifest.append({"name": name, "workspace_path": None, "error": str(exc)})
    return manifest

def _workspace_hint(f: dict) -> str:
    ws_path = str(f.get("workspace_path") or "").replace("\\", "/").strip()
    if not ws_path:
        return ""
    return (
        f"Workspace path: {ws_path}\n"
        "For full file access, call workspace_read_file with this exact "
        "workspace-relative path."
    )

def _with_workspace_hint(header: str, body: str, f: dict) -> str:
    hint = _workspace_hint(f)
    if hint:
        return f"{header}\n{hint}\n{body}".rstrip()
    return f"{header}\n{body}".rstrip()

def _vision_analysis_failed(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return True
    lowered = value.lower()
    failure_prefixes = (
        "vision analysis failed",
        "vision is disabled",
        "failed to capture",
        "failed to read image",
        "image file not found",
        "image file is empty",
        "could not access the camera",
        "ollama is not installed",
    )
    return lowered.startswith(failure_prefixes)

def process_attached_files(
    files: list[dict],
    vision_svc: VisionService | None,
    attached_data_cache: dict[str, bytes],
    model_name: str | None = None,
) -> tuple[str, list[str], list[str]]:
    """Process uploaded files and return (context_text, image_b64_list, warnings).

    *files* is a list of ``{"name": str, "data": bytes}`` dicts.
    """
    budget = file_budget(model_name)
    context_parts: list[str] = []
    images_b64: list[str] = []
    warnings: list[str] = []

    for f in files:
        name = f["name"]
        data = f["data"]
        suffix = pathlib.Path(name).suffix.lower()

        if suffix in IMAGE_EXTENSIONS:
            b64 = _b64.b64encode(data).decode("ascii")
            images_b64.append(b64)
            if vision_svc and vision_svc.enabled:
                description = vision_svc.analyze(
                    data, f"Describe this image in detail. The filename is '{name}'."
                )
                if _vision_analysis_failed(description):
                    context_parts.append(_with_workspace_hint(
                        f"[Attached image: {name} - vision analysis failed]",
                        description,
                        f,
                    ))
                else:
                    context_parts.append(
                        _with_workspace_hint(
                            f"[Attached image: {name} — ALREADY ANALYZED, do NOT call analyze_image]",
                            description,
                            f,
                        )
                    )
            else:
                context_parts.append(_with_workspace_hint(
                    f"[Attached image: {name} — vision is disabled, cannot analyze]",
                    "",
                    f,
                ))

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
                context_parts.append(_with_workspace_hint(
                    f"[Attached PDF: {name}, {len(reader.pages)} pages]",
                    content,
                    f,
                ))
            except Exception as exc:
                context_parts.append(_with_workspace_hint(
                    f"[Attached PDF: {name} — failed to extract text: {exc}]",
                    "",
                    f,
                ))

        elif suffix in DATA_EXTENSIONS:
            try:
                from row_bot.data_reader import read_data_file
                buf = io.BytesIO(data)
                summary = read_data_file(buf, name=name, max_chars=budget)
                context_parts.append(_with_workspace_hint(
                    f"[Attached data file: {name}]",
                    summary,
                    f,
                ))
                attached_data_cache[name] = data
            except Exception as exc:
                context_parts.append(_with_workspace_hint(
                    f"[Attached data file: {name} — failed to parse: {exc}]",
                    "",
                    f,
                ))

        elif suffix in TEXT_EXTENSIONS:
            try:
                text = data.decode("utf-8", errors="replace")
                if len(text) > budget:
                    warnings.append(f"📎 {name}: truncated — showing first {budget:,} of {len(text):,} chars")
                    text = text[:budget] + f"\n[Truncated — {len(data)} bytes total]"
                context_parts.append(_with_workspace_hint(
                    f"[Attached file: {name}]",
                    text,
                    f,
                ))
            except Exception as exc:
                context_parts.append(_with_workspace_hint(
                    f"[Attached file: {name} — failed to read: {exc}]",
                    "",
                    f,
                ))
        else:
            context_parts.append(_with_workspace_hint(
                f"[Attached file: {name} — unsupported file type '{suffix}']",
                "",
                f,
            ))

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
