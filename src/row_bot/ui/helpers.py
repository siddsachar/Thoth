"""Row-Bot UI helpers (config, file processing, native pickers, exports).

All functions are pure or side-effect-light.  They operate on the
parameters they receive — no hidden globals except the on-disk config.
"""

from __future__ import annotations

import asyncio
import base64 as _b64
import hashlib
import io
import json
import logging
import os
import pathlib
import subprocess
import sys
import time
from datetime import datetime
from typing import Any

from row_bot.brand import APP_DISPLAY_NAME, APP_NATIVE_ENV
from row_bot.data_paths import get_row_bot_data_dir
from row_bot.ui.constants import (
    IMAGE_EXTENSIONS,
    DATA_EXTENSIONS,
    TEXT_EXTENSIONS,
    CHARS_PER_TOKEN_APPROX,
)

from row_bot.models import get_context_size
from row_bot.vision import VisionService

logger = logging.getLogger(__name__)

ATTACHMENT_CONTEXT_START = "<row_bot_attachment_context>"
ATTACHMENT_CONTEXT_END = "</row_bot_attachment_context>"


# ═════════════════════════════════════════════════════════════════════════════
# APP CONFIG PERSISTENCE
# ═════════════════════════════════════════════════════════════════════════════

_APP_CONFIG_DIR = get_row_bot_data_dir()
_APP_CONFIG_PATH = _APP_CONFIG_DIR / "app_config.json"


def load_app_config() -> dict:
    if _APP_CONFIG_PATH.exists():
        try:
            return json.loads(_APP_CONFIG_PATH.read_text())
        except Exception:
            logger.warning("Failed to load app config from %s", _APP_CONFIG_PATH, exc_info=True)
            return {}
    return {}


def save_app_config(cfg: dict) -> None:
    _APP_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _APP_CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def is_first_run() -> bool:
    return not load_app_config().get("onboarding_seen", False)


def mark_onboarding_seen() -> None:
    cfg = load_app_config()
    cfg["onboarding_seen"] = True
    save_app_config(cfg)


def is_setup_complete() -> bool:
    """Check whether the first-launch setup wizard has been completed."""
    return load_app_config().get("setup_complete", False)


def mark_setup_complete() -> None:
    cfg = load_app_config()
    cfg["setup_complete"] = True
    save_app_config(cfg)


# ═════════════════════════════════════════════════════════════════════════════
# FILE PROCESSING
# ═════════════════════════════════════════════════════════════════════════════

def file_budget(model_name: str | None = None) -> int:
    """Dynamic char budget for attached files: 35 % of the model's context window.

    For 32K context →  ~28K chars (7K tokens)
    For 128K context → ~114K chars (28K tokens)
    Falls back to 40K chars if context size is unavailable.
    """
    try:
        ctx = get_context_size(model_name)
    except Exception:
        ctx = 32_768
    return int(ctx * 0.35 * CHARS_PER_TOKEN_APPROX)


def strip_file_context(content: str) -> str:
    """Replace verbose file-context blocks with compact badges for display."""
    if ATTACHMENT_CONTEXT_START in content and ATTACHMENT_CONTEXT_END in content:
        before, rest = content.split(ATTACHMENT_CONTEXT_START, 1)
        hidden, after = rest.split(ATTACHMENT_CONTEXT_END, 1)
        badges: list[str] = []
        for line in hidden.splitlines():
            if not line.startswith("[Attached "):
                continue
            header = line.split("\n", 1)[0]
            bracket_end = header.find("]")
            scoped = header[: bracket_end + 1] if bracket_end != -1 else header
            after_colon = scoped.split(": ", 1)[1] if ": " in scoped else scoped
            fname = after_colon.split("]")[0].strip()
            if "ALREADY ANALYZED" in fname:
                fname = fname.split("ALREADY ANALYZED", 1)[0].strip()
            for marker in (" — ", " - ", ","):
                if marker in fname:
                    fname = fname.split(marker, 1)[0].strip()
            badges.append(f"\U0001f4ce {fname}")
        visible = "\n\n".join(part.strip() for part in (before, after) if part.strip())
        if badges:
            return "\n\n".join(part for part in (", ".join(badges), visible) if part)
        return visible

    if "[Attached " not in content:
        return content
    parts = content.split("\n\n")
    badges: list[str] = []
    user_parts: list[str] = []
    for part in parts:
        if part.startswith("[Attached "):
            header = part.split("\n", 1)[0]
            bracket_end = header.find("]")
            scoped = header[: bracket_end + 1] if bracket_end != -1 else header
            after_colon = scoped.split(": ", 1)[1] if ": " in scoped else scoped
            fname = after_colon.split("]")[0].strip()
            # Drop analysis metadata so badges show only the filename.
            if "ALREADY ANALYZED" in fname:
                fname = fname.split("ALREADY ANALYZED", 1)[0].strip()
            for marker in (" — ", " - ", ","):
                if marker in fname:
                    fname = fname.split(marker, 1)[0].strip()
            badges.append(f"📎 {fname}")
        elif part.startswith(("[Trimmed ", "[Truncated ", "--- Page ")):
            continue
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


def _message_signature(msg: dict) -> str:
    """Stable signature used to reattach persisted images on reload."""
    role = str(msg.get("role", ""))
    content = msg.get("content", "")
    if isinstance(content, list):
        content = "\n".join(str(x) for x in content)
    elif not isinstance(content, str):
        content = str(content)
    raw = f"{role}\n{content}".encode("utf-8", errors="replace")
    return hashlib.sha1(raw).hexdigest()


def attach_thinking_to_message(msg: dict, thinking_text: str | None) -> dict:
    """Attach streamed reasoning text to a UI message when present."""

    thinking = str(thinking_text or "").strip()
    if thinking:
        msg["thinking"] = thinking
    return msg


def persist_thread_media_state(thread_id: str | None, messages: list[dict]) -> None:
    """Persist message media payloads as files on disk with a v2 sidecar."""
    if not thread_id:
        return
    try:
        from row_bot.threads import save_thread_media, save_media_file, _next_media_filename
        from row_bot.utils.media import is_image_filename, image_ext_from_b64

        entries: list[dict] = []
        for idx, msg in enumerate(messages):
            media_items: list[dict] = []

            # Images
            images = msg.get("images")
            persist_flags = msg.get("_media_persist_flags", [])
            if isinstance(images, list) and images:
                for img_idx, img in enumerate(images):
                    if isinstance(img, str) and img:
                        # Per-image persist flag if available, else message-level fallback
                        persist = (persist_flags[img_idx]
                                   if img_idx < len(persist_flags)
                                   else msg.get("_media_persist", False))
                        if is_image_filename(img):
                            # Already a filename stored on disk
                            media_items.append({"type": "image", "path": img, "persist": persist})
                        else:
                            # It's a base64 string — save to disk
                            ext = image_ext_from_b64(img)
                            prefix = "gen" if persist else "cap"
                            fname = _next_media_filename(thread_id, prefix, ext)
                            raw = _b64.b64decode(img)
                            save_media_file(thread_id, fname, raw)
                            media_items.append({"type": "image", "path": fname, "persist": persist})
                            # Replace base64 in message with filename for future calls
                            images[img_idx] = fname

            # Videos (already saved as files — just record in sidecar)
            videos = msg.get("videos")
            if isinstance(videos, list) and videos:
                for vid in videos:
                    if isinstance(vid, dict) and vid.get("filename"):
                        media_items.append({
                            "type": "video",
                            "path": vid["filename"],
                            "persist": True,
                        })
                    elif isinstance(vid, str) and vid:
                        media_items.append({
                            "type": "video",
                            "path": vid,
                            "persist": True,
                        })

            if not media_items:
                continue
            entries.append({
                "idx": idx,
                "role": str(msg.get("role", "")),
                "sig": _message_signature(msg),
                "media": media_items,
            })
        save_thread_media(thread_id, {"version": 2, "entries": entries})
    except Exception:
        logger.debug("Failed to persist thread media state", exc_info=True)


def _img_ext_from_b64(b64: str) -> str:
    """Deprecated — prefer ``utils.media.image_ext_from_b64``.  Kept as a
    thin alias for in-repo compatibility."""
    from row_bot.utils.media import image_ext_from_b64
    return image_ext_from_b64(b64)


def _hydrate_thread_media(thread_id: str, messages: list[dict]) -> list[dict]:
    """Merge persisted media files back into checkpoint-rebuilt messages."""
    try:
        from row_bot.threads import load_thread_media, load_media_file

        payload = load_thread_media(thread_id)
        if not payload:
            return messages
        entries = payload.get("entries", [])
        if not isinstance(entries, list) or not entries:
            return messages

        used_indices: set[int] = set()
        by_sig: dict[tuple[str, str], list[int]] = {}
        for i, msg in enumerate(messages):
            key = (str(msg.get("role", "")), _message_signature(msg))
            by_sig.setdefault(key, []).append(i)

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            role = str(entry.get("role", ""))
            sig = str(entry.get("sig", ""))
            media = entry.get("media")
            if not isinstance(media, list) or not media:
                continue

            target_idx = None
            idx = entry.get("idx")
            if isinstance(idx, int) and 0 <= idx < len(messages):
                m = messages[idx]
                if str(m.get("role", "")) == role and _message_signature(m) == sig:
                    target_idx = idx

            if target_idx is None:
                for cand in by_sig.get((role, sig), []):
                    if cand not in used_indices:
                        target_idx = cand
                        break

            if target_idx is None and isinstance(idx, int) and 0 <= idx < len(messages):
                m = messages[idx]
                if str(m.get("role", "")) == role and idx not in used_indices:
                    target_idx = idx

            if target_idx is None:
                continue

            # Load image files from disk and attach as filenames
            img_filenames: list[str] = []
            vid_entries: list[dict] = []
            has_persist = False
            for item in media:
                if item.get("type") == "image":
                    fname = item.get("path", "")
                    if fname and load_media_file(thread_id, fname) is not None:
                        img_filenames.append(fname)
                    if item.get("persist"):
                        has_persist = True
                elif item.get("type") == "video":
                    fname = item.get("path", "")
                    if fname and load_media_file(thread_id, fname) is not None:
                        vid_entries.append({"filename": fname})
                    has_persist = True

            if img_filenames:
                messages[target_idx]["images"] = img_filenames
                if has_persist:
                    messages[target_idx]["_media_persist"] = True
            if vid_entries:
                messages[target_idx]["videos"] = vid_entries
            used_indices.add(target_idx)

    except Exception:
        logger.debug("Failed to hydrate thread media for reload", exc_info=True)

    return messages


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


def langchain_messages_to_ui_messages(messages: list) -> list[dict]:
    """Convert raw LangChain checkpoint messages into Row-Bot UI messages."""
    import re as _re

    def _restore_row_bot_ui_metadata(msg_dict: dict, m: object) -> dict:
        checkpoint_message_id = str(getattr(m, "id", "") or "").strip()
        if checkpoint_message_id:
            msg_dict["checkpoint_message_id"] = checkpoint_message_id
        ak = getattr(m, "additional_kwargs", None) or {}
        metadata = ak.get("row_bot_ui") if isinstance(ak, dict) else None
        if not isinstance(metadata, dict):
            return msg_dict
        for key in (
            "timestamp",
            "turn_boundary",
            "agent_run_ids",
            "agent_run_refresh_key",
            "agent_lifecycle",
            "agent_completion_for",
            "agent_approval_for",
            "approval_request_id",
            "approval_resume_token",
            "approval_status",
            "channel_notification_key",
            "orchestration_id",
            "orchestration_message_kind",
            "goal_completion_for",
            "goal_run_id",
            "goal_status",
        ):
            if key in metadata:
                msg_dict[key] = metadata[key]
        return msg_dict

    msgs: list[dict] = []
    pending_tool_results: list[dict] = []
    pending_charts: list[str] = []
    pending_tool_invoke_names: dict[str, str] = {}
    for m in messages:
        m_type = getattr(m, "type", "")
        if m_type == "human":
            pending_tool_invoke_names.clear()
        if m_type == "tool":
            tool_name = getattr(m, "name", "") or "tool"
            if tool_name == "tool_invoke":
                tool_call_id = getattr(m, "tool_call_id", "")
                normalized_call_id = (
                    tool_call_id.strip()
                    if isinstance(tool_call_id, str)
                    else ""
                )
                if normalized_call_id:
                    tool_name = pending_tool_invoke_names.pop(
                        normalized_call_id,
                        tool_name,
                    )
            content_value = getattr(m, "content", "")
            tool_content = content_value if isinstance(content_value, str) else str(content_value)
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
            pending_tool_results.append({"name": tool_name, "content": tool_content})
        elif m_type == "human" and getattr(m, "content", None):
            pending_tool_results.clear()
            pending_charts.clear()
            user_images: list[str] = []
            content_value = getattr(m, "content", "")
            if isinstance(content_value, list):
                text_parts = []
                for part in content_value:
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
                content = content_value
            msg_dict: dict = {"role": "user", "content": strip_file_context(str(content or ""))}
            if user_images:
                msg_dict["images"] = user_images
            _restore_row_bot_ui_metadata(msg_dict, m)
            msgs.append(msg_dict)
        elif m_type == "ai":
            ai_kwargs = getattr(m, "additional_kwargs", None) or {}
            ui_metadata = (
                ai_kwargs.get("row_bot_ui")
                if isinstance(ai_kwargs, dict)
                else None
            )
            if isinstance(ui_metadata, dict) and ui_metadata.get("hidden"):
                pending_tool_results.clear()
                pending_charts.clear()
                pending_tool_invoke_names.clear()
                continue
            for tool_call in getattr(m, "tool_calls", []) or []:
                if not isinstance(tool_call, dict) or tool_call.get("name") != "tool_invoke":
                    continue
                call_id = tool_call.get("id")
                args = tool_call.get("args")
                underlying_name = args.get("name") if isinstance(args, dict) else None
                if not isinstance(call_id, str) or not call_id.strip():
                    continue
                if not isinstance(underlying_name, str):
                    continue
                normalized_name = _re.sub(r"\s+", " ", underlying_name).strip()[:180]
                if normalized_name:
                    pending_tool_invoke_names[call_id.strip()] = normalized_name
            ai_content = getattr(m, "content", "") or ""
            if isinstance(ai_content, list):
                text_parts = []
                for block in ai_content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif isinstance(block, str):
                        text_parts.append(block)
                ai_content = "\n".join(text_parts)
            if not isinstance(ai_content, str):
                ai_content = str(ai_content) if ai_content else ""
            if not ai_content.strip():
                ak = ai_kwargs
                if ak.get("reasoning_content") and not getattr(m, "tool_calls", []):
                    continue
                if pending_tool_results and not getattr(m, "tool_calls", []):
                    msg_dict = {"role": "assistant", "content": "", "tool_results": list(pending_tool_results)}
                    if pending_charts:
                        msg_dict["charts"] = list(pending_charts)
                        pending_charts = []
                    pending_tool_results = []
                    msgs.append(msg_dict)
                continue
            thinking = ""
            ak = ai_kwargs
            if ak.get("reasoning_content"):
                thinking = ak["reasoning_content"]
            think_parts = _re.findall(r"<think>(.*?)</think>", ai_content, flags=_re.DOTALL)
            if think_parts:
                thinking = (thinking + "\n" + "\n".join(think_parts)).strip()
                ai_content = _re.sub(r"<think>.*?</think>", "", ai_content, flags=_re.DOTALL).strip()
            if not ai_content:
                continue
            msg_dict = {"role": "assistant", "content": ai_content}
            _restore_row_bot_ui_metadata(msg_dict, m)
            if thinking:
                msg_dict["thinking"] = thinking
            if pending_tool_results:
                msg_dict["tool_results"] = list(pending_tool_results)
                pending_tool_results = []
            if pending_charts:
                msg_dict["charts"] = list(pending_charts)
                pending_charts = []
            msgs.append(msg_dict)
    if pending_tool_results:
        msgs.append({"role": "assistant", "content": "", "tool_results": list(pending_tool_results)})
    return msgs


def load_thread_messages(thread_id: str) -> list[dict]:
    """Rebuild the message list from checkpoint storage without agent startup."""
    started = time.perf_counter()
    try:
        from row_bot.threads import get_latest_checkpoint_messages, list_thread_events, merge_thread_events

        read_t0 = time.perf_counter()
        raw_messages = get_latest_checkpoint_messages(thread_id)
        read_elapsed = time.perf_counter() - read_t0
        convert_t0 = time.perf_counter()
        msgs = langchain_messages_to_ui_messages(raw_messages)
        convert_elapsed = time.perf_counter() - convert_t0
        media_t0 = time.perf_counter()
        hydrated = _hydrate_thread_media(thread_id, msgs)
        hydrated = merge_thread_events(hydrated, list_thread_events(thread_id))
        media_elapsed = time.perf_counter() - media_t0
        logger.info(
            "perf: load_thread_messages total=%.3fs checkpoint=%.3fs convert=%.3fs media=%.3fs thread=%s raw=%d ui=%d",
            time.perf_counter() - started,
            read_elapsed,
            convert_elapsed,
            media_elapsed,
            str(thread_id)[:8],
            len(raw_messages),
            len(hydrated),
        )
        return hydrated
    except Exception:
        logger.debug("Failed to load thread messages for %s", thread_id, exc_info=True)
        return []

    """Rebuild the message list from the LangGraph checkpoint."""
    import re as _re
    # Legacy body below is intentionally unreachable; transcript loading now
    # uses direct checkpoint reads above.

    config = {"configurable": {"thread_id": thread_id}}
    try:
        agent = None
        snapshot = None
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
                    msg_dict: dict = {"role": "user", "content": strip_file_context(content)}
                    if user_images:
                        msg_dict["images"] = user_images
                    msgs.append(msg_dict)
                elif m.type == "ai":
                    ai_content = m.content or ""
                    if isinstance(ai_content, list):
                        text_parts = []
                        for block in ai_content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                text_parts.append(block.get("text", ""))
                            elif isinstance(block, str):
                                text_parts.append(block)
                        ai_content = "\n".join(text_parts)
                    if not isinstance(ai_content, str):
                        ai_content = str(ai_content) if ai_content else ""

                    # Empty-content AI message: if this is a terminal
                    # response (no tool_calls) and there are pending tool
                    # results, flush them cleanly — the model simply
                    # produced no text after the tool ran (e.g. image gen).
                    if not ai_content.strip():
                        if pending_tool_results and not getattr(m, "tool_calls", []):
                            msg_dict = {
                                "role": "assistant",
                                "content": "",
                                "tool_results": list(pending_tool_results),
                            }
                            if pending_charts:
                                msg_dict["charts"] = list(pending_charts)
                                pending_charts = []
                            pending_tool_results = []
                            msgs.append(msg_dict)
                        continue

                    # ── Recover thinking / reasoning content ──────────
                    thinking = ""
                    ak = getattr(m, "additional_kwargs", None) or {}
                    if ak.get("reasoning_content"):
                        thinking = ak["reasoning_content"]
                    # Some models embed <think>…</think> in content
                    think_parts = _re.findall(
                        r"<think>(.*?)</think>", ai_content, flags=_re.DOTALL
                    )
                    if think_parts:
                        thinking = (thinking + "\n" + "\n".join(think_parts)).strip()
                        ai_content = _re.sub(
                            r"<think>.*?</think>", "", ai_content, flags=_re.DOTALL
                        ).strip()
                    if not ai_content:
                        continue

                    msg_dict = {"role": "assistant", "content": ai_content}
                    if thinking:
                        msg_dict["thinking"] = thinking
                    if pending_tool_results:
                        msg_dict["tool_results"] = list(pending_tool_results)
                        pending_tool_results = []
                    if pending_charts:
                        msg_dict["charts"] = list(pending_charts)
                        pending_charts = []
                    msgs.append(msg_dict)

            # Flush orphaned tool results that were never attached to a
            # final AI message (e.g. recursion limit hit mid-tool-loop).
            if pending_tool_results:
                msgs.append({
                    "role": "assistant",
                    "content": "",
                    "tool_results": list(pending_tool_results),
                })
            return _hydrate_thread_media(thread_id, msgs)
    except Exception:
        pass
    return []


def persist_detached_thread_media(
    thread_id: str,
    assistant_content: str,
    *,
    images: list[str] | None = None,
    image_persist_flags: list[bool] | None = None,
    videos: list[dict | str] | None = None,
) -> bool:
    """Persist detached-run media onto the matching checkpoint-backed assistant message."""
    pending_images = list(images or [])
    pending_videos = list(videos or [])
    if not thread_id or (not pending_images and not pending_videos):
        return False

    messages = load_thread_messages(thread_id)
    if not messages:
        return False

    assistant_text = assistant_content or ""
    assistant_sig = _message_signature({"role": "assistant", "content": assistant_text})
    target_idx: int | None = None

    for idx in range(len(messages) - 1, -1, -1):
        msg = messages[idx]
        if str(msg.get("role", "")) != "assistant":
            continue
        if _message_signature(msg) == assistant_sig:
            target_idx = idx
            break

    if target_idx is None and not assistant_text.strip():
        for idx in range(len(messages) - 1, -1, -1):
            if str(messages[idx].get("role", "")) == "assistant":
                target_idx = idx
                break

    if target_idx is None:
        return False

    target = messages[target_idx]

    if pending_images:
        existing_images = list(target.get("images", []))
        target["images"] = existing_images + pending_images

        existing_flags = list(target.get("_media_persist_flags", []))
        if len(existing_flags) < len(existing_images):
            existing_flags.extend(
                [bool(target.get("_media_persist", False))]
                * (len(existing_images) - len(existing_flags))
            )

        new_flags = list(image_persist_flags or [])
        if len(new_flags) < len(pending_images):
            new_flags.extend([False] * (len(pending_images) - len(new_flags)))

        target["_media_persist_flags"] = existing_flags + new_flags[: len(pending_images)]
        if any(target["_media_persist_flags"]):
            target["_media_persist"] = True

    if pending_videos:
        target["videos"] = list(target.get("videos", [])) + pending_videos

    persist_thread_media_state(thread_id, messages)
    return True


# ═════════════════════════════════════════════════════════════════════════════
# EXPORT HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def export_as_markdown(thread_name: str, messages: list[dict]) -> str:
    lines = [f"# {thread_name}\n"]
    lines.append(f"*Exported from {APP_DISPLAY_NAME} on {datetime.now().strftime('%Y-%m-%d %H:%M')}*\n")
    lines.append("---\n")
    for msg in messages:
        role = "User" if msg["role"] == "user" else APP_DISPLAY_NAME
        lines.append(f"### {role}\n")
        lines.append(msg["content"] + "\n")
    return "\n".join(lines)


def export_as_text(thread_name: str, messages: list[dict]) -> str:
    lines = [thread_name]
    lines.append(f"Exported from {APP_DISPLAY_NAME} on {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("=" * 60)
    lines.append("")
    for msg in messages:
        role = "User" if msg["role"] == "user" else APP_DISPLAY_NAME
        lines.append(f"[{role}]")
        lines.append(msg["content"])
        lines.append("")
    return "\n".join(lines)


def export_as_pdf(thread_name: str, messages: list[dict],
                  thread_id: str | None = None) -> bytes:
    """Convert messages to a professional PDF document. Returns PDF bytes.

    Uses Playwright (headless Chromium) for full Unicode/emoji support,
    embedded images, charts, and styled markdown rendering.
    Falls back to basic fpdf2 text export if Playwright is unavailable.

    ``thread_id`` is required to resolve filename-based image entries
    (the post-D1 storage format).  When omitted, only base64 image
    entries can be embedded; filename entries are skipped.
    """
    try:
        return _render_pdf_playwright(thread_name, messages, thread_id)
    except Exception as exc:
        logger.warning("Playwright PDF failed (%s) — falling back to fpdf2", exc)
        return _render_pdf_fpdf2(thread_name, messages)


def _build_conversation_html(thread_name: str, messages: list[dict],
                             thread_id: str | None = None) -> str:
    """Build a complete styled HTML document from conversation messages."""
    import markdown2

    escaped_title = (
        thread_name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    # CSS for professional PDF styling
    css = """
    @page { size: A4; margin: 20mm 18mm 20mm 18mm; }
    body {
        font-family: -apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
        font-size: 11pt; line-height: 1.5; color: #222; margin: 0; padding: 0;
    }
    .pdf-header {
        border-bottom: 2px solid #bbb; padding-bottom: 8px; margin-bottom: 16px;
    }
    .pdf-header h1 { font-size: 18pt; margin: 0 0 4px 0; color: #222; }
    .pdf-header .stamp { font-size: 9pt; color: #888; }
    .msg { margin-bottom: 14px; page-break-inside: auto; break-inside: auto; }
    .msg-role {
        font-weight: 700; font-size: 11pt; margin-bottom: 2px;
    }
    .msg-role { page-break-after: avoid; break-after: avoid; }
    .msg-role.user { color: #1565C0; }
    .msg-role.assistant { color: #C8A000; }
    .msg-body { font-size: 10.5pt; color: #333; }
    .msg-body p { margin: 4px 0; }
    .msg-body pre {
        background: #f5f5f5; border: 1px solid #ddd; border-radius: 4px;
        padding: 8px 10px; font-size: 9pt; overflow-x: auto;
        white-space: pre-wrap; word-break: break-all;
        font-family: 'Consolas', 'Courier New', monospace;
    }
    .msg-body code {
        background: #f0f0f0; padding: 1px 4px; border-radius: 3px;
        font-size: 9.5pt; font-family: 'Consolas', 'Courier New', monospace;
    }
    .msg-body pre code { background: none; padding: 0; }
    .msg-body table {
        border-collapse: collapse; margin: 8px 0; font-size: 10pt;
    }
    .msg-body th, .msg-body td {
        border: 1px solid #ccc; padding: 4px 8px; text-align: left;
    }
    .msg-body th { background: #f0f0f0; font-weight: 600; }
    .msg-body blockquote {
        border-left: 3px solid #ccc; margin: 8px 0; padding: 4px 12px;
        color: #555;
    }
    .msg-body img { max-width: 100%; height: auto; border-radius: 4px; margin: 6px 0; }
    .tool-block {
        background: #f8f8f8; border: 1px solid #e0e0e0; border-radius: 4px;
        padding: 6px 10px; margin: 6px 0; font-size: 9pt; color: #555;
    }
    .tool-block summary { font-weight: 600; cursor: pointer; color: #444; }
    .chart-img { max-width: 100%; height: auto; margin: 8px 0; }
    .msg-body pre, .msg-body table, .tool-block, .chart-img {
        page-break-inside: avoid; break-inside: avoid;
    }
    """

    parts = [
        "<!DOCTYPE html><html><head><meta charset='utf-8'>",
        f"<style>{css}</style></head><body>",
        f'<div class="pdf-header"><h1>{escaped_title}</h1>',
        f'<span class="stamp">Exported from {APP_DISPLAY_NAME} on {stamp}</span></div>',
    ]

    for msg in messages:
        role = msg.get("role", "assistant")
        role_label = "User" if role == "user" else APP_DISPLAY_NAME
        role_cls = "user" if role == "user" else "assistant"

        parts.append('<div class="msg">')
        parts.append(f'<div class="msg-role {role_cls}">{role_label}</div>')

        # Tool results (collapsed details)
        tool_results = msg.get("tool_results")
        if tool_results:
            from row_bot.ui.tool_trace import (
                display_tool_content,
                group_tool_results,
                is_skill_load_noop_result,
                parse_skill_load_result,
            )

            generic_results = []
            seen_skill_ids: set[str] = set()
            for result in tool_results:
                payload = parse_skill_load_result(result) if isinstance(result, dict) else None
                if payload:
                    if payload["skill_id"] in seen_skill_ids:
                        continue
                    seen_skill_ids.add(payload["skill_id"])
                    safe_name = (
                        payload["display_name"].replace("&", "&amp;")
                        .replace("<", "&lt;")
                        .replace(">", "&gt;")
                    )
                    parts.append(f'<div class="skill-use">Using {safe_name}</div>')
                elif isinstance(result, dict) and is_skill_load_noop_result(result):
                    continue
                else:
                    generic_results.append(result)

            for group in group_tool_results(generic_results):
                parts.append(
                    f'<details class="tool-block"><summary>✅ {group.label}</summary>'
                )
                for idx, tr in enumerate(group.results, start=1):
                    name = f"#{idx}" if group.count > 1 else group.name
                    content = display_tool_content(tr.get("content", ""), limit=3000)
                    safe_content = (
                        content.replace("&", "&amp;")
                        .replace("<", "&lt;")
                        .replace(">", "&gt;")
                    )
                    parts.append(f"<strong>{name}</strong><pre>{safe_content}</pre>")
                parts.append("</details>")

        # Images — may be raw base64 or a filename persisted to the
        # per-thread media directory.  (See D1 refactor: once a thread
        # reloads from checkpoint, entries are filenames.)
        images = msg.get("images")
        if images:
            from row_bot.utils.media import (
                is_image_filename,
                image_mime_from_bytes,
                image_ext_from_b64,
            )
            from row_bot.threads import load_media_file
            for entry in images:
                if not isinstance(entry, str) or not entry:
                    continue
                img_bytes: bytes | None = None
                mime = "image/jpeg"
                if is_image_filename(entry):
                    if not thread_id:
                        # Cannot resolve filename without thread context
                        continue
                    img_bytes = load_media_file(thread_id, entry)
                    if img_bytes is None:
                        continue
                    mime = image_mime_from_bytes(img_bytes)
                    b64 = _b64.b64encode(img_bytes).decode("ascii")
                else:
                    # Treat as raw base64
                    b64 = entry
                    mime = f"image/{image_ext_from_b64(b64).replace('jpg', 'jpeg')}"
                parts.append(
                    f'<img src="data:{mime};base64,{b64}" '
                    f'style="max-width:400px;" />'
                )

        # Charts (Plotly JSON → PNG via kaleido)
        charts = msg.get("charts")
        if charts:
            for fig_json in charts:
                try:
                    import plotly.io as pio
                    fig = pio.from_json(fig_json)
                    png_bytes = fig.to_image(
                        format="png", width=800, height=450, scale=2
                    )
                    chart_b64 = _b64.b64encode(png_bytes).decode("ascii")
                    parts.append(
                        f'<img class="chart-img" '
                        f'src="data:image/png;base64,{chart_b64}" />'
                    )
                except Exception:
                    parts.append(
                        '<div class="tool-block">[Chart could not be rendered]</div>'
                    )

        # Main message text → HTML via markdown2
        text = msg.get("content", "")
        if isinstance(text, list):
            text = " ".join(str(t) for t in text)
        if text:
            html_content = markdown2.markdown(
                text,
                extras=["fenced-code-blocks", "tables", "code-friendly"],
            )
            parts.append(f'<div class="msg-body">{html_content}</div>')

        parts.append("</div>")

    parts.append("</body></html>")
    return "\n".join(parts)


def _render_pdf_playwright(thread_name: str, messages: list[dict],
                           thread_id: str | None = None) -> bytes:
    """Render conversation to PDF via headless Chromium (Playwright).

    Uses a completely separate headless browser instance — does NOT
    interfere with the visible browser used by BrowserTool.
    """
    import concurrent.futures
    from playwright.sync_api import sync_playwright

    html = _build_conversation_html(thread_name, messages, thread_id)

    def _render_in_worker() -> bytes:
        pw = sync_playwright().start()
        try:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content(html, wait_until="networkidle")
            pdf_bytes = page.pdf(
                format="A4",
                margin={
                    "top": "20mm",
                    "right": "18mm",
                    "bottom": "20mm",
                    "left": "18mm",
                },
                print_background=True,
            )
            page.close()
            browser.close()
            return pdf_bytes
        finally:
            pw.stop()

    # Playwright sync API must run outside NiceGUI's asyncio event-loop thread.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_render_in_worker).result()


def _render_pdf_fpdf2(thread_name: str, messages: list[dict]) -> bytes:
    """Fallback: basic text-only PDF via fpdf2 (no images/emoji)."""
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
    pdf.cell(0, 6, f"Exported from {APP_DISPLAY_NAME} on {datetime.now().strftime('%Y-%m-%d %H:%M')}",
             new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(4)
    pdf.set_draw_color(200, 200, 200)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(6)

    for msg in messages:
        role = "User" if msg["role"] == "user" else APP_DISPLAY_NAME
        pdf.set_font("Helvetica", "B", 11)
        if msg["role"] == "user":
            pdf.set_text_color(50, 100, 200)
        else:
            pdf.set_text_color(200, 160, 0)
        pdf.cell(0, 8, role, new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)

        pdf.set_font("Helvetica", "", 10)
        safe_text = _safe(msg.get("content", ""))
        pdf.multi_cell(0, 5, safe_text)
        pdf.ln(4)

    return bytes(pdf.output())


# ═════════════════════════════════════════════════════════════════════════════
# CROSS-PLATFORM NATIVE FILE PICKERS
# ═════════════════════════════════════════════════════════════════════════════

def _is_pywebview_native_window() -> bool:
    return os.environ.get(APP_NATIVE_ENV) == "1"


async def _pick_with_pywebview(
    method: str,
    title: str,
    initial_dir: str,
    filetypes: list[tuple[str, str]] | None = None,
) -> str | None:
    if not _is_pywebview_native_window():
        return None
    try:
        from nicegui import ui

        script = f"""
        (async () => {{
            const api = window.pywebview && window.pywebview.api ? window.pywebview.api : null;
            if (!api || !api.{method}) return null;
            try {{
                return await api.{method}(
                    {json.dumps(title)},
                    {json.dumps(initial_dir or "")},
                    {json.dumps(filetypes or [])}
                );
            }} catch (err) {{
                console.warn('Row-Bot native file dialog failed', err);
                return null;
            }}
        }})()
        """
        result = await ui.run_javascript(script, timeout=180)
    except Exception:
        logger.debug("pywebview native file dialog unavailable", exc_info=True)
        return None
    if isinstance(result, (list, tuple)):
        result = result[0] if result else None
    if isinstance(result, str) and result.strip():
        return result.strip()
    return None


async def _pick_file_pywebview(
    title: str,
    initial_dir: str,
    filetypes: list[tuple[str, str]] | None,
) -> str | None:
    return await _pick_with_pywebview("choose_file", title, initial_dir, filetypes)


async def _pick_folder_pywebview(title: str, initial_dir: str) -> str | None:
    return await _pick_with_pywebview("choose_folder", title, initial_dir)


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
            uti_map = {"json": "public.json", "txt": "public.plain-text",
                       "csv": "public.comma-separated-values-text",
                       "pdf": "com.adobe.pdf", "png": "public.png",
                       "jpg": "public.jpeg", "jpeg": "public.jpeg"}
            for _, pattern in filetypes:
                for part in pattern.replace(";", " ").split():
                    ext = part.strip().lstrip("*.").lower()
                    if ext:
                        exts.append(f'"{ext}"')
                        uti = uti_map.get(ext)
                        if uti:
                            exts.append(f'"{uti}"')
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


async def browse_folder(title: str = "Select folder",
                        initial_dir: str = "") -> str | None:
    path = await _pick_folder_pywebview(title, initial_dir)
    if path:
        return path
    return await asyncio.to_thread(_pick_folder_native, title, initial_dir)


async def browse_file(
    title: str = "Select file",
    initial_dir: str = "",
    filetypes: list[tuple[str, str]] | None = None,
) -> str | None:
    path = await _pick_file_pywebview(title, initial_dir, filetypes)
    if path:
        return path
    return await asyncio.to_thread(_pick_file_native, title, initial_dir,
                                   filetypes)
