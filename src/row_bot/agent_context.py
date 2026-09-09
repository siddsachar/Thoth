"""UI-neutral context packing helpers for Agent runs."""

from __future__ import annotations

from typing import Any, Iterable, Mapping


_CONTEXT_MODES = {"auto", "focused", "recent", "full", "empty", "resume"}


class AgentContextError(ValueError):
    """Raised when an Agent context mode or packet is invalid."""


def normalize_context_mode(mode: str | None, *, fallback: str = "focused") -> str:
    value = str(mode or "").strip().lower()
    if not value:
        value = fallback
    if value not in _CONTEXT_MODES:
        raise AgentContextError(f"Invalid Agent context mode: {mode}")
    return value


def profile_context_mode(profile_snapshot: Mapping[str, Any] | None) -> str:
    profile = dict(profile_snapshot or {})
    context_policy = profile.get("context_policy_json") or {}
    if not isinstance(context_policy, dict):
        return "focused"
    mode = normalize_context_mode(
        context_policy.get("default_context_mode"),
        fallback="focused",
    )
    return "focused" if mode == "auto" else mode


def profile_context_budget(profile_snapshot: Mapping[str, Any] | None) -> int:
    profile = dict(profile_snapshot or {})
    context_policy = profile.get("context_policy_json") or {}
    if not isinstance(context_policy, dict):
        return 4000
    try:
        value = int(context_policy.get("max_context_tokens") or 0)
    except (TypeError, ValueError):
        value = 0
    return value if value > 0 else 4000


def select_context_mode(
    requested_mode: str | None,
    profile_snapshot: Mapping[str, Any] | None,
) -> str:
    requested = normalize_context_mode(
        requested_mode,
        fallback=profile_context_mode(profile_snapshot),
    )
    if requested == "auto":
        return profile_context_mode(profile_snapshot)
    return requested


def estimate_tokens(text: str) -> int:
    return max(1, (len(str(text or "")) + 3) // 4)


def message_to_text(message: Any) -> str:
    """Convert common checkpoint message shapes into compact transcript text."""
    role = ""
    content: Any = ""
    if isinstance(message, dict):
        role = str(message.get("role") or message.get("type") or "")
        content = message.get("content", "")
    elif isinstance(message, (tuple, list)) and len(message) >= 2:
        role = str(message[0] or "")
        content = message[1]
    else:
        role = str(getattr(message, "type", "") or getattr(message, "role", "") or "")
        content = getattr(message, "content", "")
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content") or ""
                if text:
                    parts.append(str(text))
            elif item:
                parts.append(str(item))
        content = "\n".join(parts)
    text = " ".join(str(content or "").split())
    if not text:
        return ""
    role = {
        "human": "User",
        "user": "User",
        "ai": "Assistant",
        "assistant": "Assistant",
        "system": "System",
        "tool": "Tool",
    }.get(role.lower(), role.title() if role else "Message")
    return f"{role}: {text}"


def transcript_to_text(messages: Iterable[Any], *, limit: int | None = None) -> str:
    items = [line for msg in messages for line in [message_to_text(msg)] if line]
    if limit is not None and limit >= 0:
        items = items[-limit:]
    return "\n".join(items)


def load_parent_context(parent_thread_id: str, *, recent_limit: int = 8) -> dict[str, Any]:
    if not parent_thread_id:
        return {"summary": "", "recent": "", "full": "", "message_count": 0}
    try:
        from row_bot.threads import get_latest_checkpoint_messages, load_validated_summary_state

        messages = get_latest_checkpoint_messages(parent_thread_id)
        summary_payload = (
            load_validated_summary_state(parent_thread_id, "agent", messages=messages)
            or load_validated_summary_state(parent_thread_id, "chat_only", messages=messages)
            or {}
        )
    except Exception:
        messages = []
        summary_payload = {}
    summary_text = str(summary_payload.get("summary") or "").strip()
    summary = (
        '<HISTORICAL_CONTEXT untrusted="true">\n'
        + summary_text
        + "\n</HISTORICAL_CONTEXT>"
        if summary_text
        else ""
    )
    return {
        "summary": summary,
        "recent": transcript_to_text(messages, limit=recent_limit),
        "full": transcript_to_text(messages),
        "message_count": len(messages),
    }


def _profile_header(profile: Mapping[str, Any]) -> list[str]:
    display_name = str(profile.get("display_name") or profile.get("slug") or "Agent")
    instructions = str(profile.get("instructions") or "").strip()
    handoff = str(profile.get("handoff_contract") or "").strip()
    parts = [f"AGENT PROFILE: {display_name}"]
    if instructions:
        parts.extend(["", "PROFILE INSTRUCTIONS:", instructions])
    if handoff:
        parts.extend(["", "HANDOFF CONTRACT:", handoff])
    return parts


def build_child_agent_prompt(
    *,
    objective: str,
    profile_snapshot: Mapping[str, Any],
    context: str = "",
    context_mode: str = "",
    parent_thread_id: str = "",
    parent_run_id: str = "",
    model_override: str = "",
) -> dict[str, str]:
    """Build the focused prompt packet for a child Agent run.

    V1 starts with a deliberately compact packet: profile instructions,
    mission, optional caller-supplied context, and the expected handoff shape.
    Transcript-derived `recent`/`full` modes are layered in the later context
    packer phase.
    """
    profile = dict(profile_snapshot or {})
    requested_mode = normalize_context_mode(context_mode, fallback="auto")
    mode = select_context_mode(context_mode, profile)
    if requested_mode == "auto":
        parent_preview = load_parent_context(parent_thread_id, recent_limit=2)
        mode = "focused" if context else "recent" if parent_preview["message_count"] else profile_context_mode(profile)
    objective = str(objective or "").strip()
    context = str(context or "").strip()
    parent_context = load_parent_context(parent_thread_id)
    fallback = ""
    context_sections: list[tuple[str, str]] = []
    if mode == "focused":
        if context:
            context_sections.append(("CONTEXT PACKET", context))
    elif mode == "empty":
        context_sections.append(("CONTEXT MODE", "empty: no parent transcript or caller context was included."))
    elif mode == "recent":
        if parent_context["summary"]:
            context_sections.append(("PARENT SUMMARY", parent_context["summary"]))
        if parent_context["recent"]:
            context_sections.append(("RECENT PARENT TURNS", parent_context["recent"]))
        if context:
            context_sections.append(("CALLER CONTEXT", context))
    elif mode == "full":
        full_text = parent_context["full"]
        if context:
            full_text = (full_text + "\n\nCALLER CONTEXT:\n" + context).strip()
        budget = profile_context_budget(profile)
        if estimate_tokens(full_text) > budget:
            fallback = "full_to_recent_summary"
            mode = "recent"
            if parent_context["summary"]:
                context_sections.append(("PARENT SUMMARY", parent_context["summary"]))
            if parent_context["recent"]:
                context_sections.append(("RECENT PARENT TURNS", parent_context["recent"]))
            if context:
                context_sections.append(("CALLER CONTEXT", context))
        elif full_text:
            context_sections.append(("FULL PARENT TRANSCRIPT", full_text))
    elif mode == "resume":
        if context:
            context_sections.append(("RESUME CONTEXT", context))
        context_sections.append(("CONTEXT MODE", "resume: continue an existing child thread with the supplied instruction."))

    parts = [
        *_profile_header(profile),
        "",
        "MISSION:",
        objective,
    ]
    model_ref = str(model_override or "").strip()
    runtime_lines = [
        "The parent selected this child Agent's runtime model before start.",
        (
            f"Runtime model override: {model_ref}"
            if model_ref
            else "Runtime model override: inherited from the parent/default runtime."
        ),
        "Do not call row_bot_update_setting(setting='model') to satisfy your own runtime model request. "
        "Do not use setting='thread_model' or setting='default_model' for that either.",
    ]
    parts.extend(["", "RUNTIME MODEL:", "\n".join(runtime_lines)])
    for title, body in context_sections:
        if body:
            parts.extend(["", f"{title}:", body])
    if fallback:
        parts.extend(["", "CONTEXT FALLBACK:", fallback])
    if parent_thread_id or parent_run_id:
        refs = []
        if parent_thread_id:
            refs.append(f"parent_thread_id={parent_thread_id}")
        if parent_run_id:
            refs.append(f"parent_run_id={parent_run_id}")
        parts.extend(["", "PARENT REFS:", ", ".join(refs)])
    parts.extend([
        "",
        "Return a concise result for the parent agent. Include evidence, files or commands inspected, risks, and next steps when relevant.",
    ])
    prompt = "\n".join(parts).strip()
    budget = profile_context_budget(profile)
    if estimate_tokens(prompt) > budget:
        # Context sections are atomic; removing a complete historical packet
        # cannot leave a tool call separated from its result. Mission, profile,
        # runtime restrictions and handoff remain mandatory.
        for title, body in reversed(context_sections):
            if estimate_tokens(prompt) <= budget:
                break
            prompt = prompt.replace(f"\n{title}:\n{body}", "", 1)
        if estimate_tokens(prompt) > budget:
            raise AgentContextError("The mandatory child packet exceeds its configured context allowance.")
    return {
        "mode": mode,
        "prompt": prompt,
        "summary": (
            f"{profile.get('display_name') or profile.get('slug') or 'Agent'} using {mode} context"
            + (f" fallback={fallback}" if fallback else "")
            + (" with supplied context" if context and mode != "empty" else "")
        ),
        "fallback": fallback,
        "message_count": str(parent_context["message_count"]),
        "estimated_tokens": str(estimate_tokens(prompt)),
    }
