"""Pure checkpoint message conversion shared by Chat Only and legacy rendering.

This is the supported existing message shape adapter. Protocol snapshots remain
owned by ConversationProjection; importing this converter loads no UI state.
"""
from __future__ import annotations

from row_bot.file_context import ATTACHMENT_CONTEXT_START, ATTACHMENT_CONTEXT_END


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
