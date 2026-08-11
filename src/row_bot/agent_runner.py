"""Background runner for chat-spawned child Agents."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Mapping, Sequence

from row_bot.approval_policy import DEFAULT_APPROVAL_MODE, normalize_approval_mode

logger = logging.getLogger(__name__)


_ACTIVE_LOCK = threading.RLock()
_ACTIVE_AGENT_RUNS: dict[str, dict[str, Any]] = {}
_DISPATCH_CONDITION = threading.Condition(threading.RLock())
_DISPATCH_QUEUE: list[tuple[str, str]] = []
_DISPATCH_ACTIVE: dict[str, str] = {}


def notify_agent_runtime_settings_changed() -> None:
    """Wake queued children so a safe local capacity increase applies live."""

    with _DISPATCH_CONDITION:
        _DISPATCH_CONDITION.notify_all()


def _dispatch_counts(parent_key: str) -> tuple[int, int]:
    global_active = len(_DISPATCH_ACTIVE)
    parent_active = sum(1 for value in _DISPATCH_ACTIVE.values() if value == parent_key)
    return parent_active, global_active


def _acquire_child_capacity(
    run_id: str,
    parent_key: str,
    stop_event: threading.Event,
) -> bool:
    from row_bot.agent_settings import load_agent_runtime_settings
    from row_bot.agent_runs import update_agent_status

    ticket = (str(run_id), str(parent_key or "top-level"))
    with _DISPATCH_CONDITION:
        if ticket not in _DISPATCH_QUEUE:
            _DISPATCH_QUEUE.append(ticket)
        queued_status_published = False
        while not stop_event.is_set():
            settings = load_agent_runtime_settings()
            parent_active, global_active = _dispatch_counts(ticket[1])
            # Preserve FIFO within each parent while allowing another parent
            # to use free global capacity when the queue head's parent is full.
            eligible_ticket = next(
                (
                    queued
                    for queued in _DISPATCH_QUEUE
                    if _dispatch_counts(queued[1])[0]
                    < settings.max_concurrent_children
                ),
                None,
            )
            if (
                eligible_ticket == ticket
                and parent_active < settings.max_concurrent_children
                and global_active < settings.max_active_children_global
            ):
                _DISPATCH_QUEUE.remove(ticket)
                _DISPATCH_ACTIVE[ticket[0]] = ticket[1]
                return True
            if not queued_status_published:
                update_agent_status(run_id, "queued", "Queued for Agent capacity")
                queued_status_published = True
            _DISPATCH_CONDITION.wait(timeout=0.1)
        if ticket in _DISPATCH_QUEUE:
            _DISPATCH_QUEUE.remove(ticket)
        _DISPATCH_CONDITION.notify_all()
        return False


def _release_child_capacity(run_id: str) -> None:
    with _DISPATCH_CONDITION:
        _DISPATCH_ACTIVE.pop(str(run_id), None)
        _DISPATCH_QUEUE[:] = [ticket for ticket in _DISPATCH_QUEUE if ticket[0] != str(run_id)]
        _DISPATCH_CONDITION.notify_all()


def child_dispatch_state() -> dict[str, Any]:
    """Return safe read-only capacity state for tests and the Agents drawer."""

    from row_bot.agent_settings import load_agent_runtime_settings

    settings = load_agent_runtime_settings()
    with _DISPATCH_CONDITION:
        return {
            "queued": len(_DISPATCH_QUEUE),
            "active": len(_DISPATCH_ACTIVE),
            "max_active": settings.max_active_children_global,
            "max_per_parent": settings.max_concurrent_children,
        }


def _arm_child_timeout(run_id: str, stop_event: threading.Event) -> threading.Timer | None:
    from row_bot.agent_runs import get_agent_run

    run = get_agent_run(run_id) or {}
    snapshot = dict(run.get("settings_snapshot_json") or {})
    timeout_seconds = max(0.0, float(snapshot.get("child_timeout_seconds") or 0))
    if timeout_seconds <= 0:
        return None
    remaining = max(0.0, timeout_seconds - float(run.get("active_seconds") or 0))

    def expire() -> None:
        with _ACTIVE_LOCK:
            entry = _ACTIVE_AGENT_RUNS.get(run_id)
            if entry is not None:
                entry["timed_out"] = True
        stop_event.set()

    if remaining <= 0:
        expire()
        return None
    timer = threading.Timer(remaining, expire)
    timer.daemon = True
    timer.start()
    return timer


def _child_timed_out(run_id: str) -> bool:
    with _ACTIVE_LOCK:
        return bool((_ACTIVE_AGENT_RUNS.get(run_id) or {}).get("timed_out"))

_RUNTIME_ERROR_TEXT_PREFIXES = (
    "API error:",
    "API quota exceeded",
    "Authentication failed",
    "Billing limit reached",
    "Context too long",
    "I got stuck in a tool loop",
    "Rate limit reached",
    "Request timed out",
    "The AI provider",
)
_RUNTIME_ERROR_TEXT_FRAGMENTS = (
    " does not support tool calling",
)

_READ_ONLY_DEFAULT_DENY_TOOLS = {
    "calendar",
    "custom_tool_builder",
    "designer",
    "gmail",
    "goal",
    "image_gen",
    "row_bot_updater",
    "task",
    "tracker",
    "video_gen",
    "x",
}


class AgentRunnerError(ValueError):
    """Raised when a child Agent cannot be created or started."""


def _short_title(text: str, *, limit: int = 64) -> str:
    title = " ".join(str(text or "").strip().split())
    if not title:
        return "Agent"
    if len(title) <= limit:
        return title
    return title[: limit - 1].rstrip() + "..."


def _strip_leading_symbols(text: str) -> str:
    cleaned = str(text or "").strip()
    while cleaned and not cleaned[0].isalnum():
        cleaned = cleaned[1:].lstrip()
    return cleaned


def _is_runtime_error_text(text: str) -> bool:
    cleaned = _strip_leading_symbols(text)
    if not cleaned:
        return False
    if cleaned.startswith(_RUNTIME_ERROR_TEXT_PREFIXES):
        return True
    return any(fragment in cleaned for fragment in _RUNTIME_ERROR_TEXT_FRAGMENTS)


def _enabled_tool_names(enabled_tool_names: Sequence[str] | None) -> list[str]:
    if enabled_tool_names is not None:
        return [str(name) for name in enabled_tool_names if str(name or "").strip()]
    from row_bot.tools import registry as tool_registry

    return [tool.name for tool in tool_registry.get_enabled_tools()]


def _profile_tool_allowlist(profile_snapshot: Mapping[str, Any]) -> list[str]:
    tool_policy = profile_snapshot.get("tool_policy_json") or {}
    if not isinstance(tool_policy, dict):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in tool_policy.get("allow_tools") or []:
        text = str(item or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _filter_child_tools(
    enabled_tool_names: Sequence[str],
    profile_snapshot: Mapping[str, Any],
) -> list[str]:
    """Apply profile tool restrictions and deny recursive delegation by default."""
    tool_policy = profile_snapshot.get("tool_policy_json") or {}
    if not isinstance(tool_policy, dict):
        tool_policy = {}
    requested = [str(name) for name in enabled_tool_names if str(name or "").strip()]
    allow = {str(name) for name in tool_policy.get("allow_tools") or [] if str(name or "").strip()}
    deny: set[str] = set()
    capability = str(tool_policy.get("capability") or "read_only")
    if capability == "read_only" and not allow:
        deny.update(_READ_ONLY_DEFAULT_DENY_TOOLS)
    if not tool_policy.get("allow_delegation"):
        deny.add("agents")
    filtered = [name for name in requested if name not in deny]
    if allow:
        mcp_allowed = "mcp" in allow or any(name.startswith("mcp_") for name in allow)
        filtered = [
            name
            for name in filtered
            if name in allow or (name == "mcp" and mcp_allowed)
        ]
    return filtered


def _profile_child_skills(
    _parent_skills_override: Sequence[str] | None,
    profile_snapshot: Mapping[str, Any],
) -> list[str]:
    skill_policy = profile_snapshot.get("skill_policy_json") or {}
    if not isinstance(skill_policy, dict):
        skill_policy = {}
    base = [
        str(name)
        for name in skill_policy.get("skills_override") or []
        if str(name or "").strip()
    ]
    deny = {
        str(name)
        for name in skill_policy.get("deny_skills") or []
        if str(name or "").strip()
    }
    return [name for name in base if name not in deny]


def _profile_requires_write_lock(profile_snapshot: Mapping[str, Any]) -> bool:
    tool_policy = profile_snapshot.get("tool_policy_json") or {}
    workspace_policy = profile_snapshot.get("workspace_policy_json") or {}
    if not isinstance(tool_policy, dict):
        tool_policy = {}
    if not isinstance(workspace_policy, dict):
        workspace_policy = {}
    capability = str(tool_policy.get("capability") or "read_only")
    return capability in {"write_capable", "orchestrator"} or bool(
        workspace_policy.get("write_lock_required")
    )


def _profile_workspace_mode(profile_snapshot: Mapping[str, Any]) -> str:
    workspace_policy = profile_snapshot.get("workspace_policy_json") or {}
    if not isinstance(workspace_policy, dict):
        return "auto"
    return str(workspace_policy.get("workspace_mode_default") or "auto")


def _normalize_workspace_mode(value: str, *, default: str = "auto") -> str:
    raw = str(value or "").strip().lower().replace("-", "_")
    if not raw:
        raw = default
    if raw not in {"auto", "read_only", "single_writer", "worktree"}:
        raise AgentRunnerError(f"Unknown child Agent workspace mode: {value}")
    return raw


def _writer_lock_key(
    *,
    developer_workspace_id: str = "",
    parent_thread_id: str = "",
    child_thread_id: str = "",
) -> str:
    if developer_workspace_id:
        return f"developer:{developer_workspace_id}"
    if parent_thread_id:
        return f"thread:{parent_thread_id}"
    return f"thread:{child_thread_id or 'default'}"


def _developer_workspace_supports_auto_worktree(workspace_id: str) -> bool:
    """Return whether an automatic child worktree can use this workspace."""

    if not str(workspace_id or "").strip():
        return False
    try:
        from row_bot.developer.storage import get_workspace, is_git_repository_root

        workspace = get_workspace(workspace_id)
        return bool(workspace and is_git_repository_root(workspace.path))
    except Exception:
        return False


def _parent_thread_defaults(parent_thread_id: str) -> dict[str, Any]:
    if not parent_thread_id:
        return {
            "approval_mode": DEFAULT_APPROVAL_MODE,
            "model_override": "",
            "developer_workspace_id": "",
            "project_workspace_id": "",
            "designer_project_id": "",
            "skills_override": None,
        }
    from row_bot.threads import (
        _get_thread_approval_mode,
        _get_thread_developer_workspace,
        _get_thread_model_override,
        _get_thread_project_id,
        _get_thread_project_workspace,
        get_thread_skills_override,
    )

    return {
        "approval_mode": _get_thread_approval_mode(parent_thread_id),
        "model_override": _get_thread_model_override(parent_thread_id),
        "developer_workspace_id": _get_thread_developer_workspace(parent_thread_id),
        "project_workspace_id": _get_thread_project_workspace(parent_thread_id),
        "designer_project_id": _get_thread_project_id(parent_thread_id),
        "skills_override": get_thread_skills_override(parent_thread_id),
    }


def _invoke_agent(
    prompt: str,
    enabled_tool_names: list[str],
    config: dict[str, Any],
    *,
    stop_event: threading.Event,
) -> str | dict:
    from row_bot.agent import invoke_agent

    return invoke_agent(prompt, enabled_tool_names, config, stop_event=stop_event)


def _resume_invoke_agent(
    enabled_tool_names: list[str],
    config: dict[str, Any],
    approved: bool,
    *,
    interrupt_ids: list[str] | None = None,
    stop_event: threading.Event,
) -> str | dict:
    from row_bot.agent import resume_invoke_agent

    return resume_invoke_agent(
        enabled_tool_names,
        config,
        approved,
        interrupt_ids=interrupt_ids,
        stop_event=stop_event,
    )


def _is_task_stopped(exc: BaseException) -> bool:
    return exc.__class__.__name__ == "TaskStoppedError"


def _build_child_config(
    *,
    run_id: str,
    child_thread_id: str,
    approval_mode: str,
    model_override: str = "",
    developer_workspace_id: str = "",
    designer_project_id: str = "",
    parent_thread_id: str = "",
    parent_run_id: str = "",
    profile_snapshot: Mapping[str, Any],
    tool_allowlist: Sequence[str] | None = None,
) -> dict[str, Any]:
    configurable = {
        "thread_id": child_thread_id,
        "runtime_surface": "agent_child",
        "runtime_mode": "agent",
        "approval_mode": approval_mode,
        "agent_profile_id": str(profile_snapshot.get("id") or ""),
        "agent_profile_snapshot": dict(profile_snapshot),
        "parent_thread_id": parent_thread_id,
        "parent_run_id": parent_run_id,
        "agent_run_id": run_id,
    }
    if tool_allowlist:
        configurable["tool_allowlist"] = [
            str(name)
            for name in tool_allowlist
            if str(name or "").strip()
        ]
    if model_override:
        configurable["model_override"] = model_override
    if developer_workspace_id:
        configurable["developer_workspace_id"] = developer_workspace_id
    if designer_project_id:
        configurable["designer_project_id"] = designer_project_id
    return {"configurable": configurable}


def spawn_agent_run(
    objective: str,
    *,
    parent_thread_id: str = "",
    parent_run_id: str = "",
    parent_message_id: str = "",
    profile: str = "",
    agent_profile_id: str = "",
    display_name: str = "",
    context: str = "",
    context_mode: str = "",
    enabled_tool_names: Sequence[str] | None = None,
    model_override: str = "",
    approval_mode: str = "",
    developer_workspace_id: str = "",
    workspace_mode: str = "",
    use_worktree: bool = False,
    orchestration_id: str = "",
    orchestration_required: bool = True,
    orchestration_dependencies: Sequence[str] | None = None,
    orchestration_attempt: int = 1,
    retry_of_run_id: str = "",
    wait: bool = False,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Create and start a single child Agent run."""
    objective = str(objective or "").strip()
    if not objective:
        raise AgentRunnerError("Child Agent objective cannot be empty.")

    parent_defaults = _parent_thread_defaults(parent_thread_id)
    parent_approval = normalize_approval_mode(
        approval_mode or parent_defaults["approval_mode"],
        DEFAULT_APPROVAL_MODE,
    )
    profile_ref = str(agent_profile_id or profile or "worker").strip()
    from row_bot.agent_profiles import resolve_profile_for_run

    resolved_profile = resolve_profile_for_run(
        profile_ref,
        parent_approval_mode=parent_approval,
    )
    profile_snapshot = resolved_profile["profile_snapshot"]
    effective_approval = normalize_approval_mode(
        resolved_profile["effective_approval_mode"],
        parent_approval,
    )
    model = str(model_override or parent_defaults["model_override"] or "")
    parent_developer_workspace_id = str(
        developer_workspace_id or parent_defaults["developer_workspace_id"] or ""
    )
    profile_workspace_mode = _normalize_workspace_mode(_profile_workspace_mode(profile_snapshot))
    effective_workspace_mode = _normalize_workspace_mode(
        workspace_mode,
        default=profile_workspace_mode,
    )
    if use_worktree:
        effective_workspace_mode = "worktree"
    requires_write_lock = _profile_requires_write_lock(profile_snapshot)
    if (
        orchestration_id
        and requires_write_lock
        and parent_developer_workspace_id
        and effective_workspace_mode != "worktree"
        and _developer_workspace_supports_auto_worktree(parent_developer_workspace_id)
    ):
        # Parallel automatic writers must not share a Git checkout. Ordinary
        # folders retain the profile's single-writer lock instead.
        effective_workspace_mode = "worktree"

    from row_bot.agent_context import build_child_agent_prompt
    from row_bot.agent_runs import (
        append_agent_event,
        create_agent_run,
        create_agent_run_edge,
        get_agent_run,
    )
    from row_bot.agent_settings import load_agent_runtime_settings
    from row_bot.threads import create_thread, set_thread_skills_override

    run_id = uuid.uuid4().hex[:12]
    parent_run = get_agent_run(parent_run_id) if parent_run_id else None
    if parent_run_id and not parent_run:
        raise AgentRunnerError("The parent Agent Run does not exist.")
    depth = int((parent_run or {}).get("depth") or 0) + 1
    root_run_id = (
        str((parent_run or {}).get("root_run_id") or (parent_run or {}).get("id") or "")
        or run_id
    )
    runtime_settings = load_agent_runtime_settings()
    if depth > runtime_settings.max_spawn_depth:
        raise AgentRunnerError(
            f"Nested Agent depth {depth} exceeds the configured maximum of "
            f"{runtime_settings.max_spawn_depth}."
        )
    effective_developer_workspace_id = parent_developer_workspace_id
    workspace_path = ""
    worktree_allocation: dict[str, Any] | None = None
    if effective_workspace_mode == "worktree":
        if not parent_developer_workspace_id:
            raise AgentRunnerError(
                "Worktree requires a git-backed Developer workspace. Choose a repo before starting this child Agent."
            )
        try:
            from row_bot.developer.worktrees import allocate_agent_worktree

            worktree_allocation = allocate_agent_worktree(
                run_id,
                parent_developer_workspace_id,
                objective=objective,
                parent_thread_id=parent_thread_id,
            )
        except Exception as exc:
            raise AgentRunnerError(str(exc)) from exc
        if str(worktree_allocation.get("status") or "") != "active":
            raise AgentRunnerError(
                str(worktree_allocation.get("error") or "Failed to create Worktree.")
            )
        effective_developer_workspace_id = str(
            worktree_allocation.get("worktree_workspace_id") or ""
        )
        workspace_path = str(worktree_allocation.get("worktree_path") or "")
        if not effective_developer_workspace_id or not workspace_path:
            raise AgentRunnerError("Worktree did not return a usable workspace.")

    packet = build_child_agent_prompt(
        objective=objective,
        profile_snapshot=profile_snapshot,
        context=context,
        context_mode=context_mode,
        parent_thread_id=parent_thread_id,
        parent_run_id=parent_run_id,
        model_override=model,
    )
    child_display = display_name or f"{profile_snapshot.get('display_name', 'Agent')}: {_short_title(objective, limit=42)}"
    child_thread_id = create_thread(
        child_display,
        thread_type="agent_child",
        project_id=str(parent_defaults.get("designer_project_id") or ""),
        developer_workspace_id=effective_developer_workspace_id,
        project_workspace_id=str(
            (worktree_allocation or {}).get("project_workspace_id")
            or parent_defaults.get("project_workspace_id")
            or parent_developer_workspace_id
        ),
        approval_mode=effective_approval,
        model_override=model,
        agent_profile_id=str(profile_snapshot.get("id") or ""),
        agent_profile_slug=str(profile_snapshot.get("slug") or ""),
        seed_default_skills=False,
    )
    child_skills = _profile_child_skills(parent_defaults["skills_override"], profile_snapshot)
    set_thread_skills_override(child_thread_id, child_skills)

    tool_allowlist = _profile_tool_allowlist(profile_snapshot)
    child_tools = _filter_child_tools(
        _enabled_tool_names(enabled_tool_names),
        profile_snapshot,
    )
    write_lock_key = (
        _writer_lock_key(
            developer_workspace_id=effective_developer_workspace_id,
            parent_thread_id=parent_thread_id,
            child_thread_id=child_thread_id,
        )
        if requires_write_lock
        else ""
    )
    config = _build_child_config(
        run_id=run_id,
        child_thread_id=child_thread_id,
        approval_mode=effective_approval,
        model_override=model,
        developer_workspace_id=effective_developer_workspace_id,
        designer_project_id=str(parent_defaults.get("designer_project_id") or ""),
        parent_thread_id=parent_thread_id,
        parent_run_id=parent_run_id,
        profile_snapshot=profile_snapshot,
        tool_allowlist=tool_allowlist,
    )
    if tool_allowlist:
        try:
            from row_bot.agent_tool_catalog import count_tool_ids_by_source

            logger.info(
                "child Agent tool allow-list active: profile=%s selected=%d counts=%s",
                profile_snapshot.get("slug") or profile_snapshot.get("id") or "",
                len(tool_allowlist),
                count_tool_ids_by_source(tool_allowlist),
            )
        except Exception:
            logger.info(
                "child Agent tool allow-list active: profile=%s selected=%d",
                profile_snapshot.get("slug") or profile_snapshot.get("id") or "",
                len(tool_allowlist),
            )
    run = create_agent_run(
        run_id=run_id,
        kind="subagent",
        status="queued",
        parent_run_id=parent_run_id,
        root_run_id=root_run_id,
        parent_thread_id=parent_thread_id,
        parent_message_id=parent_message_id,
        thread_id=child_thread_id,
        depth=depth,
        profile_id=str(profile_snapshot.get("id") or ""),
        profile_snapshot_json=profile_snapshot,
        display_name=child_display,
        prompt=objective,
        context_mode=packet["mode"],
        context_summary=packet["summary"],
        model_override=model,
        tools_override=tool_allowlist if tool_allowlist else child_tools,
        skills_override=child_skills,
        approval_mode=effective_approval,
        workspace_id=effective_developer_workspace_id,
        workspace_path=workspace_path,
        workspace_mode=effective_workspace_mode,
        write_lock_key=write_lock_key,
    )
    if parent_run_id:
        create_agent_run_edge(parent_run_id, run_id, "spawned_by_tool")
    if worktree_allocation:
        append_agent_event(
            run_id,
            "workspace.worktree_allocated",
            {
                "parent_workspace_id": parent_developer_workspace_id,
                "workspace_id": effective_developer_workspace_id,
                "workspace_path": workspace_path,
                "branch_name": worktree_allocation.get("branch_name", ""),
                "seeded_from_current_changes": bool(
                    (worktree_allocation.get("metadata_json") or {}).get("seeded_from_current_changes")
                ),
            },
            visibility="user_visible",
        )
    append_agent_event(
        run_id,
        "context.packed",
        {
            "mode": packet["mode"],
            "fallback": packet.get("fallback", ""),
            "message_count": packet.get("message_count", "0"),
            "estimated_tokens": packet.get("estimated_tokens", "0"),
        },
        visibility="internal",
    )
    if orchestration_id:
        from row_bot.agent_orchestrator import register_member

        register_member(
            orchestration_id,
            run_id,
            required=orchestration_required,
            dependency_run_ids=orchestration_dependencies,
            attempt=orchestration_attempt,
            retry_of_run_id=retry_of_run_id,
        )

    stop_event = threading.Event()
    thread = threading.Thread(
        target=_run_agent_thread,
        args=(
            run_id,
            packet["prompt"],
            child_tools,
            config,
            stop_event,
            requires_write_lock,
            write_lock_key,
        ),
        daemon=True,
        name=f"agent-run-{run_id}",
    )
    with _ACTIVE_LOCK:
        _ACTIVE_AGENT_RUNS[run_id] = {
            "thread": thread,
            "stop_event": stop_event,
            "started_at": datetime.now().isoformat(),
        }
    thread.start()
    if wait:
        thread.join(timeout=timeout)
        return wait_for_agent_run(run_id, timeout=0)
    return run


def _interrupt_message(display_name: str, interrupts: list[dict[str, Any]]) -> str:
    try:
        from row_bot.approval_messages import compact_message, normalize_interrupts

        payload = normalize_interrupts(interrupts, source_label=display_name or "Child Agent")
        message = compact_message(payload)
        if message:
            return message
    except Exception:
        logger.debug("Could not build compact child approval message", exc_info=True)
    details: list[str] = []
    for intr in interrupts:
        tool = str(intr.get("tool") or "tool")
        desc = str(intr.get("description") or "").strip()
        details.append(desc or f"Tool '{tool}' needs approval")
    suffix = "; ".join(details) if details else "Approval is required to continue."
    return f"{display_name or 'Child Agent'} needs approval: {suffix}"


def _pause_agent_for_approval(
    run_id: str,
    result: dict[str, Any],
    config: dict[str, Any],
    enabled_tool_names: list[str],
) -> None:
    from row_bot.agent_runs import get_agent_run, save_agent_resume_state
    from row_bot.channels.thread_notifications import notify_agent_run_approval
    from row_bot.tasks import create_approval_request
    from row_bot.approval_messages import compact_message, normalize_interrupts

    run = get_agent_run(run_id) or {}
    interrupts = result.get("interrupts") or []
    if not isinstance(interrupts, list):
        interrupts = []
    configurable = config.get("configurable") or {}
    source_label = str(run.get("display_name") or "Child Agent")
    parent_thread_id = str(run.get("parent_thread_id") or "")
    approval_payload = normalize_interrupts(
        interrupts,
        source_label=source_label,
        agent_run_id=run_id,
        parent_thread_id=parent_thread_id,
    )
    message = compact_message(approval_payload) or _interrupt_message(source_label, interrupts)
    resume_state = {
        "config": config,
        "enabled_tool_names": enabled_tool_names,
        "tool_allowlist": list(configurable.get("tool_allowlist") or []),
        "interrupts": interrupts,
        "approval_payload": approval_payload,
        "external_discovery_active": any(
            bool(item.get("external_discovery_active"))
            for item in interrupts
            if isinstance(item, dict)
        ),
    }
    resume_token, approval_id = create_approval_request(
        run_id=run_id,
        task_id="",
        step_id="agent_interrupt",
        message=message,
        agent_run_id=run_id,
        resume_kind="agent_run",
        source_label=source_label,
        source_thread_id=str(configurable.get("thread_id") or run.get("thread_id") or ""),
        parent_thread_id=parent_thread_id,
        approval_payload_json=approval_payload,
    )
    resume_state["resume_token"] = resume_token
    resume_state["approval_id"] = approval_id
    save_agent_resume_state(
        run_id,
        resume_state,
        status="waiting_approval",
        status_message="Waiting for approval",
    )
    notify_agent_run_approval(approval_id)


def _run_agent_thread(
    run_id: str,
    prompt: str,
    enabled_tool_names: list[str],
    config: dict[str, Any],
    stop_event: threading.Event,
    requires_write_lock: bool = False,
    write_lock_key: str = "",
) -> None:
    from row_bot.agent_runs import (
        acquire_agent_write_lock,
        append_agent_event,
        finish_agent_run,
        get_agent_parent_messages,
        get_agent_run,
        release_agent_write_lock,
        start_agent_run,
        update_agent_status,
    )

    lock_acquired = False
    capacity_acquired = False
    timeout_timer: threading.Timer | None = None
    try:
        try:
            from row_bot.agent_orchestrator import wait_for_dependencies

            if not wait_for_dependencies(run_id, stop_event):
                finish_agent_run(
                    run_id,
                    "stopped",
                    status_message="Stop requested while waiting for dependencies",
                )
                return
        except ImportError:
            pass
        configurable = config.get("configurable") or {}
        parent_key = str(
            configurable.get("parent_run_id")
            or configurable.get("parent_thread_id")
            or "top-level"
        )
        capacity_acquired = _acquire_child_capacity(run_id, parent_key, stop_event)
        if not capacity_acquired:
            finish_agent_run(run_id, "stopped", status_message="Stop requested while queued")
            return
        if requires_write_lock:
            update_agent_status(run_id, "queued", "Queued for writer lock")
            while not stop_event.is_set():
                if acquire_agent_write_lock(
                    write_lock_key,
                    run_id,
                    thread_id=(config.get("configurable") or {}).get("thread_id", ""),
                    workspace_id=(config.get("configurable") or {}).get("developer_workspace_id", ""),
                    metadata_json={"runtime_surface": "agent_child"},
                ):
                    lock_acquired = True
                    break
                time.sleep(0.05)
            if stop_event.is_set() and not lock_acquired:
                finish_agent_run(run_id, "stopped", status_message="Stop requested")
                return
        start_agent_run(run_id)
        timeout_timer = _arm_child_timeout(run_id, stop_event)
        append_agent_event(
            run_id,
            "turn.started",
            {"thread_id": (config.get("configurable") or {}).get("thread_id", "")},
        )
        parent_messages = get_agent_parent_messages(run_id)
        if parent_messages:
            joined = "\n".join(f"- {message}" for message in parent_messages[-5:])
            prompt = f"{prompt}\n\n[Parent follow-up before start]\n{joined}"
            append_agent_event(
                run_id,
                "parent.messages.applied",
                {"count": len(parent_messages)},
                visibility="internal",
            )
            try:
                from row_bot.agent_orchestrator import mark_steering_delivered

                mark_steering_delivered(run_id, parent_messages)
            except Exception:
                logger.debug("Could not acknowledge queued Agent guidance", exc_info=True)
        applied_parent_message_count = len(parent_messages)
        result = _invoke_agent(
            prompt,
            enabled_tool_names,
            config,
            stop_event=stop_event,
        )
        if not stop_event.is_set() and not (
            isinstance(result, dict)
            and result.get("type") in {"interrupt", "error", "terminal"}
        ):
            latest_parent_messages = get_agent_parent_messages(run_id)
            follow_ups = latest_parent_messages[applied_parent_message_count:]
            if follow_ups:
                follow_up_prompt = (
                    "[Parent guidance received at the next safe boundary]\n"
                    + "\n".join(f"- {message}" for message in follow_ups[-5:])
                    + "\n\nUpdate or verify your result in light of this guidance."
                )
                append_agent_event(
                    run_id,
                    "parent.messages.applied",
                    {"count": len(follow_ups), "boundary": "post_turn"},
                    visibility="internal",
                )
                try:
                    from row_bot.agent_orchestrator import mark_steering_delivered

                    mark_steering_delivered(run_id, follow_ups)
                except Exception:
                    logger.debug("Could not acknowledge Agent guidance", exc_info=True)
                result = _invoke_agent(
                    follow_up_prompt,
                    enabled_tool_names,
                    config,
                    stop_event=stop_event,
                )
        if _child_timed_out(run_id):
            finish_agent_run(
                run_id,
                "timed_out",
                status_message="Child Agent active-time limit reached",
                terminal_reason="timeout",
            )
            return
        if stop_event.is_set() or (get_agent_run(run_id) or {}).get("stop_requested"):
            finish_agent_run(run_id, "stopped", status_message="Stop requested")
            return
        if isinstance(result, dict) and result.get("type") == "interrupt":
            _pause_agent_for_approval(run_id, result, config, enabled_tool_names)
            return
        if isinstance(result, dict) and result.get("type") == "error":
            message = str(result.get("error") or result.get("message") or "Agent resume failed.")
            finish_agent_run(run_id, "failed", error=message, status_message=message)
            return
        if isinstance(result, dict) and result.get("type") == "terminal":
            message = str(result.get("message") or "Agent stopped incomplete.")
            terminal_reason = str(result.get("terminal_reason") or "")
            finish_agent_run(
                run_id,
                "blocked",
                summary=message,
                result_json={"response": message, "complete": False},
                error=message,
                status_message=message,
                terminal_reason=terminal_reason,
            )
            return
        text = str(result or "")
        if _is_runtime_error_text(text):
            finish_agent_run(
                run_id,
                "failed",
                summary=text,
                result_json={"response": text},
                error=text,
                status_message=text,
            )
            return
        append_agent_event(run_id, "turn.completed", {"length": len(text)})
        finish_agent_run(
            run_id,
            "completed",
            summary=text,
            result_json={"response": text},
        )
    except BaseException as exc:
        if _child_timed_out(run_id):
            finish_agent_run(
                run_id,
                "timed_out",
                error=str(exc),
                status_message="Child Agent active-time limit reached",
                terminal_reason="timeout",
            )
        elif _is_task_stopped(exc) or stop_event.is_set():
            finish_agent_run(run_id, "stopped", status_message="Stop requested")
        else:
            finish_agent_run(
                run_id,
                "failed",
                error=str(exc),
                status_message=str(exc),
            )
    finally:
        if timeout_timer is not None:
            timeout_timer.cancel()
        if lock_acquired:
            release_agent_write_lock(run_id=run_id)
        if capacity_acquired:
            _release_child_capacity(run_id)
        _notify_child_agent_waiters(run_id)
        with _ACTIVE_LOCK:
            _ACTIVE_AGENT_RUNS.pop(run_id, None)


def resume_agent_run(
    run_id: str,
    *,
    resume_token: str = "",
    approved: bool = True,
) -> dict[str, Any] | None:
    """Resume or stop an interrupted child Agent after approval response."""
    from row_bot.agent_runs import append_agent_event, finish_agent_run, get_agent_run

    run = get_agent_run(run_id)
    if not run:
        return None
    try:
        from row_bot.agent_orchestrator import (
            get_member_for_run,
            get_orchestration,
            record_thread_event,
        )

        member = get_member_for_run(run_id)
        orchestration = (
            get_orchestration(str(member.get("orchestration_id") or ""))
            if member
            else None
        )
        if orchestration and int(orchestration.get("orchestration_version") or 0) >= 2:
            approval_id = str(
                (run.get("resume_state_json") or {}).get("approval_id") or "approval"
            )
            record_thread_event(
                str(orchestration["id"]),
                kind="child_approval_resolved",
                content=(
                    f"Approval for child {run_id} was "
                    f"{'approved' if approved else 'denied'}."
                ),
                run_id=run_id,
                source_event_id=(
                    f"run:{run_id}:approval:{approval_id}:"
                    f"{'approved' if approved else 'denied'}"
                ),
                payload={
                    "run_id": run_id,
                    "approved": bool(approved),
                    "approval_id": approval_id,
                },
            )
    except Exception:
        logger.debug("Could not publish child approval resolution", exc_info=True)
    if not approved:
        append_agent_event(
            run_id,
            "approval.resolved",
            {"approved": False, "resume_token": resume_token},
        )
        run = finish_agent_run(
            run_id,
            "stopped",
            status_message="Approval denied by user",
        )
        _notify_child_agent_waiters(run_id)
        return run
    resume_state = run.get("resume_state_json") or {}
    config = resume_state.get("config")
    enabled_tool_names = resume_state.get("enabled_tool_names")
    if not isinstance(config, dict) or not isinstance(enabled_tool_names, list):
        return finish_agent_run(
            run_id,
            "failed",
            error="Missing Agent resume state",
            status_message="Missing Agent resume state",
        )
    config = dict(config)
    configurable = dict(config.get("configurable") or {})
    configurable["external_discovery_active"] = bool(
        resume_state.get("external_discovery_active")
    )
    config["configurable"] = configurable
    interrupt_ids: list[str] = []
    seen_interrupt_ids: set[str] = set()
    for intr in resume_state.get("interrupts", []):
        if not isinstance(intr, dict):
            continue
        raw_id = intr.get("__interrupt_id") or intr.get("id")
        if not raw_id:
            continue
        interrupt_id = str(raw_id)
        if interrupt_id in seen_interrupt_ids:
            continue
        seen_interrupt_ids.add(interrupt_id)
        interrupt_ids.append(interrupt_id)
    append_agent_event(
        run_id,
        "approval.resolved",
        {"approved": True, "resume_token": resume_token},
    )
    stop_event = threading.Event()
    thread = threading.Thread(
        target=_resume_agent_thread,
        args=(run_id, enabled_tool_names, config, interrupt_ids, stop_event),
        daemon=True,
        name=f"agent-resume-{run_id}",
    )
    with _ACTIVE_LOCK:
        _ACTIVE_AGENT_RUNS[run_id] = {
            "thread": thread,
            "stop_event": stop_event,
            "started_at": datetime.now().isoformat(),
        }
    thread.start()
    return get_agent_run(run_id)


def _resume_agent_thread(
    run_id: str,
    enabled_tool_names: list[str],
    config: dict[str, Any],
    interrupt_ids: list[str],
    stop_event: threading.Event,
) -> None:
    from row_bot.agent_runs import (
        acquire_agent_write_lock,
        append_agent_event,
        finish_agent_run,
        get_agent_run,
        release_agent_write_lock,
        start_agent_run,
        update_agent_status,
    )

    lock_acquired = False
    capacity_acquired = False
    timeout_timer: threading.Timer | None = None
    try:
        run = get_agent_run(run_id) or {}
        parent_key = str(run.get("parent_run_id") or run.get("parent_thread_id") or "top-level")
        capacity_acquired = _acquire_child_capacity(run_id, parent_key, stop_event)
        if not capacity_acquired:
            finish_agent_run(run_id, "stopped", status_message="Stop requested while queued")
            return
        write_lock_key = str(run.get("write_lock_key") or "")
        if write_lock_key:
            update_agent_status(run_id, "queued", "Queued for writer lock")
            while not stop_event.is_set():
                if acquire_agent_write_lock(
                    write_lock_key,
                    run_id,
                    thread_id=str(run.get("thread_id") or ""),
                    workspace_id=str(run.get("workspace_id") or ""),
                    metadata_json={"runtime_surface": "agent_child_resume"},
                ):
                    lock_acquired = True
                    break
                time.sleep(0.05)
            if stop_event.is_set() and not lock_acquired:
                finish_agent_run(run_id, "stopped", status_message="Stop requested")
                return
        start_agent_run(run_id)
        timeout_timer = _arm_child_timeout(run_id, stop_event)
        result = _resume_invoke_agent(
            enabled_tool_names,
            config,
            True,
            interrupt_ids=interrupt_ids or None,
            stop_event=stop_event,
        )
        if not stop_event.is_set() and not (
            isinstance(result, dict)
            and result.get("type") in {"interrupt", "error", "terminal"}
        ):
            try:
                from row_bot.agent_orchestrator import (
                    mark_steering_delivered,
                    pending_steering_for_run,
                )

                follow_ups = pending_steering_for_run(run_id)
                if follow_ups:
                    result = _invoke_agent(
                        "[Parent guidance received after approval]\n"
                        + "\n".join(f"- {message}" for message in follow_ups[-5:])
                        + "\n\nUpdate or verify your result in light of this guidance.",
                        enabled_tool_names,
                        config,
                        stop_event=stop_event,
                    )
                    mark_steering_delivered(run_id, follow_ups)
            except Exception:
                logger.debug("Could not apply Agent guidance after approval", exc_info=True)
        if _child_timed_out(run_id):
            finish_agent_run(
                run_id,
                "timed_out",
                status_message="Child Agent active-time limit reached",
                terminal_reason="timeout",
            )
            return
        if stop_event.is_set() or (get_agent_run(run_id) or {}).get("stop_requested"):
            finish_agent_run(run_id, "stopped", status_message="Stop requested")
            return
        if isinstance(result, dict) and result.get("type") == "interrupt":
            _pause_agent_for_approval(run_id, result, config, enabled_tool_names)
            return
        if isinstance(result, dict) and result.get("type") == "error":
            message = str(result.get("error") or result.get("message") or "Agent resume failed.")
            finish_agent_run(run_id, "failed", error=message, status_message=message)
            return
        if isinstance(result, dict) and result.get("type") == "terminal":
            message = str(result.get("message") or "Agent stopped incomplete.")
            terminal_reason = str(result.get("terminal_reason") or "")
            finish_agent_run(
                run_id,
                "blocked",
                summary=message,
                result_json={"response": message, "resumed": True, "complete": False},
                error=message,
                status_message=message,
                terminal_reason=terminal_reason,
            )
            return
        text = str(result or "")
        if _is_runtime_error_text(text):
            finish_agent_run(
                run_id,
                "failed",
                summary=text,
                result_json={"response": text, "resumed": True},
                error=text,
                status_message=text,
            )
            return
        append_agent_event(run_id, "turn.completed", {"length": len(text), "resumed": True})
        finish_agent_run(
            run_id,
            "completed",
            summary=text,
            result_json={"response": text, "resumed": True},
        )
    except BaseException as exc:
        if _child_timed_out(run_id):
            finish_agent_run(
                run_id,
                "timed_out",
                error=str(exc),
                status_message="Child Agent active-time limit reached",
                terminal_reason="timeout",
            )
        elif _is_task_stopped(exc) or stop_event.is_set():
            finish_agent_run(run_id, "stopped", status_message="Stop requested")
        else:
            finish_agent_run(
                run_id,
                "failed",
                error=str(exc),
                status_message=str(exc),
            )
    finally:
        if timeout_timer is not None:
            timeout_timer.cancel()
        if lock_acquired:
            release_agent_write_lock(run_id=run_id)
        if capacity_acquired:
            _release_child_capacity(run_id)
        _notify_child_agent_waiters(run_id)
        with _ACTIVE_LOCK:
            _ACTIVE_AGENT_RUNS.pop(run_id, None)


def wait_for_agent_run(run_id: str, timeout: float | None = None) -> dict[str, Any] | None:
    """Wait for a live child Agent thread, then return its durable row."""
    deadline = time.monotonic() + timeout if timeout is not None else None
    while True:
        with _ACTIVE_LOCK:
            entry = _ACTIVE_AGENT_RUNS.get(run_id)
        if not entry:
            from row_bot.agent_runs import get_agent_run

            return get_agent_run(run_id)
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        thread = entry["thread"]
        thread.join(timeout=remaining)
        if not thread.is_alive():
            continue
        if deadline is not None and time.monotonic() >= deadline:
            from row_bot.agent_runs import get_agent_run

            return get_agent_run(run_id)


def _agent_run_terminal_statuses() -> set[str]:
    try:
        from row_bot.agent_runs import TERMINAL_STATUSES

        return set(TERMINAL_STATUSES)
    except Exception:
        return {
            "completed",
            "completed_delivery_failed",
            "failed",
            "stopped",
            "blocked",
            "timed_out",
            "cancelled",
        }


def agent_run_is_terminal(run: Mapping[str, Any] | None) -> bool:
    if not run:
        return True
    return str(run.get("status") or "") in _agent_run_terminal_statuses()


def wait_for_agent_run_terminal(
    run_id: str,
    timeout: float | None = None,
    *,
    poll_interval: float = 0.25,
) -> dict[str, Any] | None:
    """Wait until an Agent Run reaches a durable terminal status."""
    return wait_for_agent_run_terminal_or_status(
        run_id,
        timeout=timeout,
        poll_interval=poll_interval,
    )


def wait_for_agent_run_terminal_or_status(
    run_id: str,
    timeout: float | None = None,
    *,
    statuses: set[str] | frozenset[str] | None = None,
    poll_interval: float = 0.25,
) -> dict[str, Any] | None:
    """Wait until an Agent Run is terminal or reaches one of *statuses*."""
    from row_bot.agent_runs import get_agent_run

    awaited_statuses = {str(status) for status in (statuses or set())}
    deadline = time.monotonic() + timeout if timeout is not None else None
    sleep_step = max(0.01, float(poll_interval or 0.25))
    while True:
        row = get_agent_run(run_id)
        row_status = str((row or {}).get("status") or "")
        if agent_run_is_terminal(row) or row_status in awaited_statuses:
            return row
        if deadline is not None and time.monotonic() >= deadline:
            return row

        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        wait_for = sleep_step if remaining is None else min(sleep_step, remaining)
        with _ACTIVE_LOCK:
            entry = _ACTIVE_AGENT_RUNS.get(run_id)
        if entry:
            entry["thread"].join(timeout=wait_for)
        else:
            time.sleep(wait_for)


def stop_agent_run(run_id: str) -> dict[str, Any] | None:
    """Request stop for a live child Agent and update durable state."""
    with _ACTIVE_LOCK:
        entry = _ACTIVE_AGENT_RUNS.get(run_id)
        if entry:
            entry["stop_event"].set()
    from row_bot.agent_runs import stop_agent_run as _stop_agent_run

    run = _stop_agent_run(run_id)
    _notify_child_agent_waiters(run_id)
    return run


def _notify_child_agent_waiters(run_id: str) -> None:
    try:
        from row_bot.tasks import resume_workflows_waiting_for_child_agent

        resume_workflows_waiting_for_child_agent(run_id)
    except Exception:
        logger.exception(
            "Failed to notify workflows waiting for child Agent %s",
            run_id,
        )


def list_active_agent_run_ids() -> list[str]:
    with _ACTIVE_LOCK:
        return sorted(_ACTIVE_AGENT_RUNS)
