from __future__ import annotations

import inspect
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from row_bot.approval_policy import DEFAULT_APPROVAL_MODE

log = logging.getLogger(__name__)


def approval_mode_for_config(config: dict) -> str:
    configurable = config.get("configurable") or {}
    thread_id = str(configurable.get("thread_id") or "")
    if not thread_id:
        return DEFAULT_APPROVAL_MODE
    try:
        from row_bot.threads import _get_thread_approval_mode

        return _get_thread_approval_mode(thread_id)
    except Exception:
        return DEFAULT_APPROVAL_MODE


@dataclass(frozen=True)
class ChannelGoalStart:
    """Prepared channel Goal start that should enter the normal agent path."""

    goal: dict[str, Any]
    prompt: str
    objective: str


@dataclass(frozen=True)
class ChannelGoalRunResult:
    """Result from a channel Goal run/continuation loop."""

    turns: int
    status: str
    reason: str
    interrupt_data: Any | None = None


def prepare_channel_goal_start(text: str, thread_id: str | None) -> ChannelGoalStart | None:
    """Start a channel ``/goal <objective>`` and return the initial agent prompt.

    Goal control commands return ``None`` so existing command dispatch keeps
    handling ``/goal``, ``/goal status``, ``/goal pause``, and friends.
    """

    message = str(text or "").strip()
    parts = message.split(maxsplit=1)
    if not parts or parts[0].lower() != "/goal" or not thread_id:
        return None
    arg = parts[1] if len(parts) > 1 else ""
    from row_bot import goals

    if not goals.is_goal_start_argument(arg):
        return None
    goal = goals.start_goal(str(thread_id), arg)
    return ChannelGoalStart(
        goal=goal,
        prompt=goals.build_initial_goal_prompt(goal),
        objective=str(goal.get("objective") or arg),
    )


def format_goal_started_ack(goal_start: ChannelGoalStart) -> str:
    """Return the immediate channel acknowledgement for a started Goal."""

    objective = " ".join(str(goal_start.objective or "Goal").split())
    if len(objective) > 220:
        objective = objective[:217].rstrip() + "..."
    return (
        f"Goal started: {objective}\n\n"
        "I'm working on it now. This may take a bit; I'll send progress or the final result here, "
        "and I'll ask for approval if a step needs it."
    )


def _extract_agent_result(result: Any) -> tuple[str, Any | None]:
    if isinstance(result, tuple):
        answer = str(result[0] or "") if result else ""
        interrupt_data = result[1] if len(result) > 1 else None
        return answer, interrupt_data
    return str(result or ""), None


def _model_override_from_config(config: dict | None) -> str:
    configurable = (config or {}).get("configurable") or {}
    return str(configurable.get("model_override") or "")


def thread_id_from_config(config: dict | None) -> str:
    configurable = (config or {}).get("configurable") or {}
    return str(configurable.get("thread_id") or "").strip()


def prepare_channel_turn_config(config: dict, user_text: str) -> dict:
    """Attach durable parent-generation identity to a channel turn."""

    prepared = {
        **dict(config or {}),
        "configurable": dict((config or {}).get("configurable") or {}),
    }
    configurable = prepared["configurable"]
    thread_id = str(configurable.get("thread_id") or "")
    configurable.setdefault(
        "generation_id",
        f"{thread_id}:channel:{uuid.uuid4().hex[:12]}",
    )
    configurable.setdefault("root_objective", str(user_text or "").strip())
    configurable.setdefault("runtime_surface", "channel")
    configurable.setdefault("runtime_mode", "auto")
    return prepared


def finalize_channel_orchestration(
    config: dict,
    assistant_text: str,
    enabled_tool_names: list[str],
) -> bool:
    """Suspend required channel work and let durable delivery own ack/final."""

    configurable = config.get("configurable") or {}
    thread_id = str(configurable.get("thread_id") or "")
    generation_id = str(configurable.get("generation_id") or "")
    if not thread_id or not generation_id:
        return False
    from row_bot.agent_orchestrator import (
        finalize_parent_generation,
        get_generation_orchestration,
    )

    orchestration = get_generation_orchestration(thread_id, generation_id)
    if not orchestration or int(orchestration.get("required_total") or 0) <= 0:
        return False
    if int(orchestration.get("orchestration_version") or 0) >= 2:
        # The shared agent completion guard already persisted the real parent
        # output and moved the turn to waiting. Channels deliver that output
        # normally; they do not replace it with adapter-authored narration.
        return False
    text = str(assistant_text or "")
    if text.strip():
        from row_bot.threads import remove_latest_checkpoint_ai_message

        remove_latest_checkpoint_ai_message(thread_id, text)
    return finalize_parent_generation(
        str(orchestration["id"]),
        continuation_state={
            "config": config,
            "enabled_tool_names": list(enabled_tool_names),
            "provisional_text": text,
            "channel_turn": True,
        },
        delivery_context={
            "runtime_surface": "channel",
            "runtime_channel": str(configurable.get("runtime_channel") or ""),
            "plugin_id": str(configurable.get("plugin_id") or ""),
        },
    )


def orchestration_suspended_final() -> str:
    from row_bot.channels.streaming import ORCHESTRATION_SUSPENDED_FINAL

    return ORCHESTRATION_SUSPENDED_FINAL


def channel_turn_is_suspended(answer: Any) -> bool:
    return str(answer or "").strip() == orchestration_suspended_final()


def channel_turn_waiting_on_parent(config: dict | None) -> bool:
    """Return whether the unified parent is awaiting child/event work.

    Version 2 parents keep and deliver their real model-authored progress.  The
    channel Goal loop still needs to pause at that point instead of evaluating
    the progress text as a completed Goal turn.
    """

    configurable = (config or {}).get("configurable") or {}
    thread_id = str(configurable.get("thread_id") or "")
    generation_id = str(configurable.get("generation_id") or "")
    if not thread_id or not generation_id:
        return False
    try:
        from row_bot.agent_orchestrator import get_generation_orchestration

        orchestration = get_generation_orchestration(thread_id, generation_id)
    except Exception:
        return False
    return bool(
        orchestration
        and int(orchestration.get("orchestration_version") or 0) >= 2
        and str(orchestration.get("parent_state") or "")
        in {"waiting", "runnable", "running"}
    )


def _has_assistant_after_latest_human(messages: list[object]) -> bool:
    start = 0
    for idx, message in enumerate(messages):
        if str(getattr(message, "type", "") or "") == "human":
            start = idx + 1
    return any(
        str(getattr(message, "type", "") or "") == "ai"
        for message in messages[start:]
    )


def _channel_delivery_metadata(delivery: Any | None) -> dict[str, Any]:
    if delivery is None:
        return {}
    metadata: dict[str, Any] = {}
    for key in ("delivered", "streamed", "finalized", "fallback_sent", "transport"):
        if hasattr(delivery, key):
            metadata[key] = getattr(delivery, key)
    error = str(getattr(delivery, "error", "") or "").strip()
    if error:
        metadata["error"] = error[:500]
    return metadata


def persist_channel_assistant_message(
    config: dict | None,
    assistant_text: str,
    *,
    channel_name: str,
    delivery: Any | None = None,
) -> bool:
    """Append a channel-delivered assistant answer when LangGraph did not.

    Provider stream disconnects can leave the channel with a partial/error final
    answer while the latest checkpoint still contains only the human message.
    This helper repairs that UI-visible gap without duplicating normal graph
    completions or tool/approval assistant turns.
    """

    thread_id = thread_id_from_config(config)
    text = str(assistant_text or "").strip()
    if not thread_id or not text or channel_turn_is_suspended(text):
        return False
    try:
        from langchain_core.messages import AIMessage
        from row_bot.threads import append_checkpoint_messages, get_latest_checkpoint_messages

        messages = get_latest_checkpoint_messages(thread_id)
        if _has_assistant_after_latest_human(messages):
            return False
        metadata = {
            "channel_checkpoint_fallback": True,
            "channel": str(channel_name or "channel"),
            "channel_delivery": _channel_delivery_metadata(delivery),
        }
        appended = bool(
            append_checkpoint_messages(
                thread_id,
                [AIMessage(content=text, additional_kwargs={"row_bot_ui": metadata})],
            )
        )
        if appended:
            _touch_thread(thread_id)
        return appended
    except Exception:
        log.debug("Could not persist channel assistant message to checkpoint", exc_info=True)
        return False


def resolve_goal_approval_for_config(config: dict | None, approved: bool) -> bool:
    """Update a waiting channel Goal before resuming an approval interrupt."""

    thread_id = thread_id_from_config(config)
    if not thread_id:
        return False
    try:
        from row_bot import goals

        goal = goals.get_current_goal(thread_id, include_terminal=True)
        if not goal or str(goal.get("status") or "") != "waiting_approval":
            return False
        if approved:
            goals.resume_goal(thread_id)
        else:
            goals.block_goal(thread_id, reason="Approval was denied by the user.")
        return True
    except Exception:
        return False


def _turn_id(channel_name: str, thread_id: str) -> str:
    return f"channel:{channel_name}:{thread_id}:{uuid.uuid4().hex[:12]}"


def _after_goal_turn(
    *,
    channel_name: str,
    thread_id: str,
    config: dict | None,
    assistant_text: str,
    interrupt_data: Any | None,
):
    from row_bot import goals

    decision = goals.after_turn(
        thread_id=thread_id,
        turn_id=_turn_id(channel_name, thread_id),
        assistant_text=assistant_text,
        model_override=_model_override_from_config(config),
        pending_approval=bool(interrupt_data),
    )
    _touch_thread(thread_id)
    return decision


def _touch_thread(thread_id: str) -> None:
    try:
        from row_bot.threads import touch_thread

        touch_thread(str(thread_id))
    except Exception:
        pass


def _is_terminal_goal_status(status: str) -> bool:
    return str(status or "").lower() in {"completed", "blocked", "failed", "cancelled", "timed_out"}


def finalize_channel_goal_terminal_notification(
    *,
    thread_id: str,
    goal: Mapping[str, Any] | None = None,
) -> bool:
    """Emit a compact channel goal terminal notification after answer delivery."""

    clean_thread_id = thread_id_from_config({"configurable": {"thread_id": thread_id}})
    if not clean_thread_id:
        return False
    _touch_thread(clean_thread_id)
    try:
        from row_bot import goals

        current = dict(goal or goals.get_current_goal(clean_thread_id, include_terminal=True) or {})
        if not current or not _is_terminal_goal_status(str(current.get("status") or "")):
            return False
        run_id = str(current.get("active_run_id") or "")
        if not run_id:
            return False
        from row_bot.channels.thread_notifications import notify_agent_run_terminal

        delivered = notify_agent_run_terminal(run_id)
        _touch_thread(clean_thread_id)
        return delivered
    except Exception:
        return False


def _finalize_terminal_goal_decision(thread_id: str, decision: Any) -> None:
    if not _is_terminal_goal_status(str(getattr(decision, "status", "") or "")):
        return
    goal = getattr(decision, "goal", None)
    if isinstance(goal, Mapping):
        finalize_channel_goal_terminal_notification(thread_id=thread_id, goal=goal)


def run_channel_goal_sync(
    *,
    channel_name: str,
    thread_id: str,
    config: dict,
    first_prompt: str,
    run_turn: Callable[[str, dict], Any],
    send_text: Callable[[str], Any],
    max_continuations: int = 20,
) -> ChannelGoalRunResult:
    """Run a channel Goal start plus internal continuations synchronously."""

    prompt = first_prompt
    turns = 0
    status = "active"
    reason = ""
    interrupt_data: Any | None = None
    while prompt and turns < max(1, int(max_continuations or 20)):
        turns += 1
        result = run_turn(prompt, config)
        answer, interrupt_data = _extract_agent_result(result)
        if channel_turn_is_suspended(answer):
            return ChannelGoalRunResult(
                turns=turns,
                status="waiting_children",
                reason="Required child Agents are still running.",
            )
        if answer:
            send_text(answer)
        if channel_turn_waiting_on_parent(config):
            return ChannelGoalRunResult(
                turns=turns,
                status="waiting_children",
                reason="The original parent is waiting for child Agent events.",
            )
        decision = _after_goal_turn(
            channel_name=channel_name,
            thread_id=thread_id,
            config=config,
            assistant_text=answer,
            interrupt_data=interrupt_data,
        )
        status = decision.status or status
        reason = decision.reason or reason
        if interrupt_data or not decision.should_continue or not decision.continuation_prompt:
            _finalize_terminal_goal_decision(thread_id, decision)
            return ChannelGoalRunResult(
                turns=turns,
                status=status,
                reason=reason,
                interrupt_data=interrupt_data,
            )
        prompt = decision.continuation_prompt
    return ChannelGoalRunResult(turns=turns, status=status, reason=reason)


def continue_channel_goal_after_turn_sync(
    *,
    channel_name: str,
    thread_id: str,
    config: dict,
    assistant_text: str,
    interrupt_data: Any | None,
    run_turn: Callable[[str, dict], Any],
    send_text: Callable[[str], Any],
    max_continuations: int = 20,
) -> ChannelGoalRunResult:
    """Evaluate a just-finished channel turn and continue the Goal if needed."""

    if channel_turn_waiting_on_parent(config):
        return ChannelGoalRunResult(
            turns=0,
            status="waiting_children",
            reason="The original parent is waiting for child Agent events.",
        )
    decision = _after_goal_turn(
        channel_name=channel_name,
        thread_id=thread_id,
        config=config,
        assistant_text=assistant_text,
        interrupt_data=interrupt_data,
    )
    if interrupt_data or not decision.should_continue or not decision.continuation_prompt:
        _finalize_terminal_goal_decision(thread_id, decision)
        return ChannelGoalRunResult(
            turns=0,
            status=decision.status,
            reason=decision.reason,
            interrupt_data=interrupt_data,
        )
    return run_channel_goal_sync(
        channel_name=channel_name,
        thread_id=thread_id,
        config=config,
        first_prompt=decision.continuation_prompt,
        run_turn=run_turn,
        send_text=send_text,
        max_continuations=max_continuations,
    )


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def run_channel_goal_async(
    *,
    channel_name: str,
    thread_id: str,
    config: dict,
    first_prompt: str,
    run_turn: Callable[[str, dict], Awaitable[Any] | Any],
    send_text: Callable[[str], Awaitable[Any] | Any],
    max_continuations: int = 20,
) -> ChannelGoalRunResult:
    """Run a channel Goal start plus internal continuations asynchronously."""

    prompt = first_prompt
    turns = 0
    status = "active"
    reason = ""
    interrupt_data: Any | None = None
    while prompt and turns < max(1, int(max_continuations or 20)):
        turns += 1
        result = await _maybe_await(run_turn(prompt, config))
        answer, interrupt_data = _extract_agent_result(result)
        if channel_turn_is_suspended(answer):
            return ChannelGoalRunResult(
                turns=turns,
                status="waiting_children",
                reason="Required child Agents are still running.",
            )
        if answer:
            await _maybe_await(send_text(answer))
        if channel_turn_waiting_on_parent(config):
            return ChannelGoalRunResult(
                turns=turns,
                status="waiting_children",
                reason="The original parent is waiting for child Agent events.",
            )
        decision = _after_goal_turn(
            channel_name=channel_name,
            thread_id=thread_id,
            config=config,
            assistant_text=answer,
            interrupt_data=interrupt_data,
        )
        status = decision.status or status
        reason = decision.reason or reason
        if interrupt_data or not decision.should_continue or not decision.continuation_prompt:
            _finalize_terminal_goal_decision(thread_id, decision)
            return ChannelGoalRunResult(
                turns=turns,
                status=status,
                reason=reason,
                interrupt_data=interrupt_data,
            )
        prompt = decision.continuation_prompt
    return ChannelGoalRunResult(turns=turns, status=status, reason=reason)


async def continue_channel_goal_after_turn_async(
    *,
    channel_name: str,
    thread_id: str,
    config: dict,
    assistant_text: str,
    interrupt_data: Any | None,
    run_turn: Callable[[str, dict], Awaitable[Any] | Any],
    send_text: Callable[[str], Awaitable[Any] | Any],
    max_continuations: int = 20,
) -> ChannelGoalRunResult:
    """Evaluate a just-finished async channel turn and continue the Goal."""

    if channel_turn_waiting_on_parent(config):
        return ChannelGoalRunResult(
            turns=0,
            status="waiting_children",
            reason="The original parent is waiting for child Agent events.",
        )
    decision = _after_goal_turn(
        channel_name=channel_name,
        thread_id=thread_id,
        config=config,
        assistant_text=assistant_text,
        interrupt_data=interrupt_data,
    )
    if interrupt_data or not decision.should_continue or not decision.continuation_prompt:
        _finalize_terminal_goal_decision(thread_id, decision)
        return ChannelGoalRunResult(
            turns=0,
            status=decision.status,
            reason=decision.reason,
            interrupt_data=interrupt_data,
        )
    return await run_channel_goal_async(
        channel_name=channel_name,
        thread_id=thread_id,
        config=config,
        first_prompt=decision.continuation_prompt,
        run_turn=run_turn,
        send_text=send_text,
        max_continuations=max_continuations,
    )
