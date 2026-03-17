import threading

from models import get_llm, get_context_size, get_current_model
from api_keys import apply_keys
from prompts import AGENT_SYSTEM_PROMPT, SUMMARIZE_PROMPT
from langchain_classic.retrievers import ContextualCompressionRetriever
from langchain_classic.retrievers.document_compressors import LLMChainExtractor
from langchain_core.messages import trim_messages, ToolMessage, AIMessage
from langgraph.types import interrupt, Command
from threads import pick_or_create_thread, checkpointer
import logging

logger = logging.getLogger(__name__)

apply_keys()

# ── Contextual compression: extract only query-relevant content per doc ──────
_compressor = None

def _get_compressor():
    """Lazy-init compressor so it always uses the current LLM."""
    global _compressor
    _compressor = LLMChainExtractor.from_llm(get_llm())
    return _compressor

def _compressed(base_retriever):
    """Wrap any retriever with contextual compression.  Public so tool
    modules can call ``from agent import _compressed``."""
    return ContextualCompressionRetriever(
        base_compressor=_get_compressor(),
        base_retriever=base_retriever,
    )

# ── Import tools package (triggers auto-registration of all tools) ───────────
import tools  # noqa: E402 — must come after _compressed is defined
from tools import registry as tool_registry


# ═════════════════════════════════════════════════════════════════════════════
# ReAct Agent — LLM decides which tools to call
# ═════════════════════════════════════════════════════════════════════════════
from langgraph.prebuilt import create_react_agent
from datetime import datetime as _datetime


# ── Pre-model hook: trim messages to fit context window ──────────────────────
_CHARS_PER_TOKEN = 4  # conservative approximation for budget math


def _pre_model_trim(state: dict) -> dict:
    """Trim conversation history to ~70% of the context window before each
    LLM call, and inject the current date/time so it is always accurate.

    Uses ``llm_input_messages`` so the full history stays intact in the
    checkpointer — only the LLM sees the trimmed version."""
    max_tokens = int(get_context_size() * 0.85)

    # ── Proportionally shrink oversized ToolMessages ─────────────────
    # Without this, trim_messages (strategy="last") may drop ALL context
    # when a single huge ToolMessage — or the sum of several — exceeds
    # the token budget.  We leave ~35 % for system prompt, human/AI
    # messages, and generation headroom.
    messages = list(state["messages"])
    tool_budget_chars = int(max_tokens * 0.65) * _CHARS_PER_TOKEN

    tool_indices = [
        i for i, m in enumerate(messages)
        if m.type == "tool" and len(getattr(m, "content", "") or "") > 0
    ]
    if tool_indices:
        total_tool_chars = sum(
            len(messages[i].content or "") for i in tool_indices
        )
        if total_tool_chars > tool_budget_chars:
            for i in tool_indices:
                m = messages[i]
                content = m.content or ""
                # Each tool gets a share proportional to its original size
                share = len(content) / total_tool_chars
                cap = max(2_000, int(tool_budget_chars * share))
                if len(content) > cap:
                    messages[i] = ToolMessage(
                        content=(
                            content[:cap]
                            + f"\n\n[Truncated to fit context – first "
                              f"{cap:,} of {len(content):,} chars shown]"
                        ),
                        name=m.name,
                        tool_call_id=m.tool_call_id,
                    )

    # ── Apply cached context summary (if available) ──────────────────
    # If a summary was produced by _do_summarize, replace the older
    # messages with a single SystemMessage so the LLM sees a compact
    # version.  The full history remains in the checkpoint.
    _thread_id = _current_thread_id_var.get() or None
    if _thread_id and _thread_id in _summary_cache:
        from langchain_core.messages import SystemMessage as _SM
        cached = _summary_cache[_thread_id]
        _split = cached["msg_count"]
        if 0 < _split < len(messages):
            # Keep the system prompt (position 0) if present
            _sys = [messages[0]] if messages and messages[0].type == "system" else []
            _summary_msg = _SM(
                content=(
                    "[Conversation Summary — the following condenses earlier "
                    "messages that are no longer shown in full]\n"
                    + cached["summary"]
                    + "\n[End of summary — recent messages follow]"
                )
            )
            messages = _sys + [_summary_msg] + messages[_split:]

    trimmed = trim_messages(
        messages,
        max_tokens=max_tokens,
        token_counter="approximate",
        strategy="last",
        start_on="human",
        include_system=True,
        allow_partial=False,
    )

    # Inject current date & time right after the system message so the
    # model always has an up-to-date reference.  This runs on every LLM
    # call, so even after days of uptime the date stays correct.
    from langchain_core.messages import SystemMessage
    now = _datetime.now()
    time_msg = SystemMessage(
        content=(
            f"Current date and time: {now.strftime('%A, %B %d, %Y at %I:%M %p')}."
        )
    )
    # Insert after the first system message (the main prompt)
    insert_idx = 1  # default: after position 0
    for i, m in enumerate(trimmed):
        if isinstance(m, SystemMessage):
            insert_idx = i + 1
            break
    trimmed.insert(insert_idx, time_msg)

    # ── Auto-recall: inject relevant memories before the last user msg ───
    # Embed the latest human message and pull the top-5 most relevant
    # memories from the FAISS index.  This ensures the model always has
    # personal context without needing to call search_memory explicitly.
    try:
        last_human_text = None
        last_human_idx = None
        for i in range(len(trimmed) - 1, -1, -1):
            if trimmed[i].type == "human":
                last_human_text = trimmed[i].content
                last_human_idx = i
                break

        if last_human_text and last_human_idx is not None:
            from memory import semantic_search as _mem_search, count_memories

            if count_memories() > 0:
                # Use first 500 chars of user message for embedding
                query = last_human_text[:500] if isinstance(last_human_text, str) else str(last_human_text)[:500]
                memories = _mem_search(query, top_k=5, threshold=0.35)
                if memories:
                    lines = []
                    for m in memories:
                        lines.append(
                            f"- [id={m['id']}] [{m['category']}] {m['subject']}: {m['content']}"
                            + (f" (tags: {m['tags']})" if m.get("tags") else "")
                        )
                    recall_msg = SystemMessage(
                        content=(
                            "You KNOW the following facts about this user "
                            "(from your long-term memory):\n"
                            + "\n".join(lines)
                            + "\n\nTreat these as things you already know. "
                            "Use them to answer the user's question directly — "
                            "do NOT say you don't know or search for this info. "
                            "Do not mention that these were recalled from memory. "
                            "If you need to update or delete one of these, use its ID."
                        )
                    )
                    trimmed.insert(last_human_idx, recall_msg)
    except Exception as exc:
        logger.debug("Auto-recall failed (non-fatal): %s", exc)

    return {"llm_input_messages": trimmed}

# Cache compiled agent graphs keyed by frozenset of enabled tool names
_agent_cache: dict[frozenset[str], object] = {}

# Thread-local flag — background workflows skip destructive tools
import threading as _threading
import contextvars as _contextvars
_tlocal = _threading.local()

# ContextVar for current_thread_id — unlike threading.local, this
# propagates to sync executor threads used by LangGraph for tools.
_current_thread_id_var: _contextvars.ContextVar[str] = _contextvars.ContextVar(
    "current_thread_id", default=""
)


def is_background_workflow() -> bool:
    """Return True if code is running inside a background workflow.

    Used by self-gating tools (e.g. shell) to block destructive
    operations without the generic interrupt wrapper."""
    return getattr(_tlocal, 'background_workflow', False)

# ── Context summarization ────────────────────────────────────────────────────
_SUMMARY_THRESHOLD = 0.80   # trigger summarization at 80 % of context window
_PROTECTED_TURNS = 5         # keep the last N human messages (+ their replies) intact
_summary_cache: dict[str, dict] = {}  # thread_id → {"summary": str, "msg_count": int}

def _should_summarize(agent, config: dict, user_input: str) -> bool:
    """Return True if the *effective* context (accounting for any cached
    summary) plus the new user input would exceed the summarization
    threshold and there are enough messages to make summarization
    worthwhile.
    """
    max_tokens = get_context_size()
    threshold = int(max_tokens * _SUMMARY_THRESHOLD)
    try:
        state = agent.get_state(config)
        if not state or not state.values:
            return False
        msgs = state.values.get("messages", [])
        if not msgs:
            return False

        # Need at least PROTECTED_TURNS + 1 human messages to have
        # something to summarize
        human_count = sum(1 for m in msgs if m.type == "human")
        if human_count <= _PROTECTED_TURNS:
            return False

        # Compute *effective* size — if a summary cache exists, use
        # summary size + messages-after-split instead of the full raw
        # checkpoint.  This prevents re-triggering every turn after the
        # first summarization.
        thread_id = (config.get("configurable") or {}).get("thread_id", "")
        cached = _summary_cache.get(thread_id) if thread_id else None

        if cached and 0 < cached["msg_count"] < len(msgs):
            old_split = cached["msg_count"]
            # Effective = system prompt + summary text + messages after split
            sys_chars = len(getattr(msgs[0], "content", "") or "") if msgs[0].type == "system" else 0
            summary_chars = len(cached["summary"]) + 120  # framing overhead
            recent_chars = sum(
                len(getattr(m, "content", "") or "") for m in msgs[old_split:]
            )
            total_chars = sys_chars + summary_chars + recent_chars
            total_chars += len(user_input)
            estimated_tokens = total_chars // _CHARS_PER_TOKEN
            if estimated_tokens <= threshold:
                return False

            # Over threshold — but only re-summarize if the gap between
            # the old split and the new split is substantial enough to
            # justify another LLM call.  Otherwise the protected window
            # itself is large (e.g. huge tool results) and re-summarizing
            # won't materially help.
            human_indices = [i for i, m in enumerate(msgs) if m.type == "human"]
            new_split = human_indices[-_PROTECTED_TURNS] if len(human_indices) > _PROTECTED_TURNS else old_split
            gap_chars = sum(
                len(getattr(m, "content", "") or "") for m in msgs[old_split:new_split]
            )
            _MIN_GAP_CHARS = 2000  # don't waste an LLM call for trivial gaps
            return gap_chars >= _MIN_GAP_CHARS
        else:
            total_chars = sum(len(getattr(m, "content", "") or "") for m in msgs)

        total_chars += len(user_input)
        estimated_tokens = total_chars // _CHARS_PER_TOKEN
        return estimated_tokens > threshold
    except Exception:
        logger.debug("_should_summarize check failed", exc_info=True)
        return False


def _do_summarize(agent, config: dict) -> None:
    """Summarize older messages and cache the result for the thread.

    The summary replaces the older portion of messages inside
    ``_pre_model_trim`` — the checkpoint is NOT modified, so the full
    conversation is always available in the UI and in the raw state.
    """
    thread_id = (config.get("configurable") or {}).get("thread_id", "")
    try:
        state = agent.get_state(config)
        if not state or not state.values:
            return
        msgs = state.values.get("messages", [])
        if not msgs:
            return

        # Find split point — protect the last N human messages
        human_indices = [i for i, m in enumerate(msgs) if m.type == "human"]
        if len(human_indices) <= _PROTECTED_TURNS:
            return
        split_idx = human_indices[-_PROTECTED_TURNS]

        # Collect messages to summarize.
        # On first summarization: all messages from start to split_idx.
        # On rolling re-summarization: only the GAP (old_split → new split)
        # since everything before old_split is already in the cached summary.
        first_content = 1 if msgs and msgs[0].type == "system" else 0
        existing_summary = _summary_cache.get(thread_id, {}).get("summary", "")
        old_split = _summary_cache.get(thread_id, {}).get("msg_count", 0)

        if existing_summary and 0 < old_split < split_idx:
            # Rolling: only feed the gap (already-summarized portion is in
            # existing_summary, not re-sent as raw messages).
            old_msgs = msgs[old_split:split_idx]
        else:
            # First time: everything from after system prompt to split.
            old_msgs = msgs[first_content:split_idx]

        if not old_msgs:
            return

        # Build a text representation for the summarizer
        parts: list[str] = []
        if existing_summary:
            parts.append(f"[Previous summary of even earlier messages]:\n{existing_summary}\n")

        for m in old_msgs:
            role = m.type.upper()
            content = getattr(m, "content", "") or ""
            if not content:
                continue
            # Cap individual messages so the summarizer prompt stays manageable
            if len(content) > 3000:
                content = content[:3000] + " …[truncated]"
            # Skip tool messages verbatim — just note the tool name + short excerpt
            if m.type == "tool":
                name = getattr(m, "name", "tool")
                content = f"[Tool result from {name}]: {content[:600]}"
            parts.append(f"{role}: {content}")

        conversation_text = "\n".join(parts)

        # Call the LLM to produce a summary
        llm = get_llm()
        summary_response = llm.invoke([
            {"role": "system", "content": SUMMARIZE_PROMPT},
            {"role": "human", "content": conversation_text},
        ])

        summary_text = (summary_response.content or "").strip()
        # Strip <think>…</think> blocks from thinking / reasoning models
        summary_text = _re.sub(r"<think>.*?</think>", "", summary_text, flags=_re.DOTALL)
        summary_text = _re.sub(r"</?think>", "", summary_text).strip()

        if summary_text:
            _summary_cache[thread_id] = {
                "summary": summary_text,
                "msg_count": split_idx,
            }
            logger.info(
                "Context summarized for thread %s — %d messages condensed "
                "(%d chars → %d chars)",
                thread_id, split_idx - first_content,
                len(conversation_text), len(summary_text),
            )
    except Exception:
        logger.warning("Context summarization failed (non-fatal)", exc_info=True)


def clear_summary_cache(thread_id: str | None = None) -> None:
    """Clear cached summaries — for a specific thread, or all threads."""
    if thread_id:
        _summary_cache.pop(thread_id, None)
    else:
        _summary_cache.clear()


# Human-readable labels for destructive tool operations
_DESTRUCTIVE_LABELS: dict[str, str] = {
    "workspace_file_delete": "Delete file",
    "workspace_move_file": "Move / rename file",
    "delete_calendar_event": "Delete calendar event",
    "move_calendar_event": "Move calendar event",
    "send_gmail_message": "Send email",
    "delete_memory": "Delete memory",
    "tracker_delete": "Delete tracker / entry",
}


def _wrap_with_interrupt_gate(tool) -> None:
    """Mutate a LangChain tool in-place so that calling it triggers a
    LangGraph ``interrupt()`` before the real function runs.  The graph
    pauses, the UI shows a confirmation prompt, and the tool only executes
    if the user approves."""
    label = _DESTRUCTIVE_LABELS.get(tool.name, tool.name)

    if hasattr(tool, "func") and tool.func is not None:
        _orig = tool.func

        def _gated(*args, _fn=_orig, _label=label, _tname=tool.name, **kwargs):
            args_str = ", ".join(
                f"{k}={v!r}" for k, v in kwargs.items()
            )
            if args:
                args_str = repr(args[0]) if len(args) == 1 else repr(args)
                if kwargs:
                    args_str += ", " + ", ".join(f"{k}={v!r}" for k, v in kwargs.items())
            if getattr(_tlocal, 'background_workflow', False):
                return (f"⚠️ BLOCKED: '{_label}' requires user confirmation "
                        "and cannot run in a background workflow. "
                        "Do NOT retry this tool. Inform the user that this "
                        "action was skipped and move on.")
            approval = interrupt({
                "tool": _tname,
                "label": _label,
                "description": f"{_label}: {args_str}",
                "args": kwargs or (args[0] if args else {}),
            })
            if not approval:
                return "Action cancelled by user."
            return _fn(*args, **kwargs)

        tool.func = _gated
    else:
        _orig = tool._run

        def _gated_run(*args, _fn=_orig, _label=label, _tname=tool.name, **kwargs):
            args_str = ", ".join(f"{k}={v!r}" for k, v in kwargs.items())
            if getattr(_tlocal, 'background_workflow', False):
                return (f"⚠️ BLOCKED: '{_label}' requires user confirmation "
                        "and cannot run in a background workflow. "
                        "Do NOT retry this tool. Inform the user that this "
                        "action was skipped and move on.")
            approval = interrupt({
                "tool": _tname,
                "label": _label,
                "description": f"{_label}: {args_str}",
                "args": kwargs or (args[0] if args else {}),
            })
            if not approval:
                return "Action cancelled by user."
            return _fn(*args, **kwargs)

        tool._run = _gated_run


def clear_agent_cache():
    """Clear the cached agent graphs so tools are rebuilt on next call."""
    _agent_cache.clear()
    _TOOL_DISPLAY_NAMES.clear()


def get_token_usage(config: dict | None = None) -> tuple[int, int]:
    """Return ``(used_tokens, max_tokens)`` for the current thread.

    Runs the same ``trim_messages`` logic as ``_pre_model_trim`` so the
    counter reflects what the LLM *actually* sees, not the full history.
    Returns ``(0, max_tokens)`` when there is no active thread.
    """
    max_tokens = get_context_size()
    if config is None:
        return 0, max_tokens
    try:
        agent = get_agent_graph()
        state = agent.get_state(config)
        if not state or not state.values:
            return 0, max_tokens
        msgs = state.values.get("messages", [])
        if not msgs:
            return 0, max_tokens

        # Account for cached summary — mirrors _pre_model_trim logic
        thread_id = (config.get("configurable") or {}).get("thread_id", "")
        if thread_id and thread_id in _summary_cache:
            cached = _summary_cache[thread_id]
            split = cached["msg_count"]
            if 0 < split < len(msgs):
                sys_msg = [msgs[0]] if msgs and msgs[0].type == "system" else []
                summary_chars = len(cached["summary"]) + 120  # overhead
                recent_chars = sum(
                    len(getattr(m, "content", "") or "")
                    for m in msgs[split:]
                )
                total_chars = summary_chars + recent_chars
                if sys_msg:
                    total_chars += len(getattr(sys_msg[0], "content", "") or "")
                used = total_chars // _CHARS_PER_TOKEN
                return used, max_tokens

        # Mirror _pre_model_trim: trim, then count what remains
        budget = int(max_tokens * 0.85)
        trimmed = trim_messages(
            msgs,
            max_tokens=budget,
            token_counter="approximate",
            strategy="last",
            start_on="human",
            include_system=True,
            allow_partial=False,
        )
        total_chars = sum(len(getattr(m, "content", "") or "") for m in trimmed)
        used = total_chars // _CHARS_PER_TOKEN
        return used, max_tokens
    except Exception:
        logger.debug("Token usage estimation failed", exc_info=True)
        return 0, max_tokens


def get_agent_graph(enabled_tool_names: list[str] | None = None):
    """Build (or return cached) a ReAct agent graph for the given set of
    enabled tools.  The agent is rebuilt only when the tool set changes."""
    if enabled_tool_names is None:
        enabled_tool_names = [t.name for t in tool_registry.get_enabled_tools()]

    is_background = getattr(_tlocal, 'background_workflow', False)
    cache_key = frozenset(enabled_tool_names) | frozenset({f"ctx:{get_context_size()}", f"model:{get_current_model()}", f"bg:{is_background}"})

    if cache_key not in _agent_cache:
        # Collect LangChain tool wrappers for enabled tools
        lc_tools = []
        destructive_names: set[str] = set()
        for name in enabled_tool_names:
            tool_obj = tool_registry.get_tool(name)
            if tool_obj is not None:
                lc_tools.extend(tool_obj.as_langchain_tools())
                destructive_names.update(tool_obj.destructive_tool_names)

        if is_background:
            # Background workflows: remove destructive tools entirely so the
            # LLM can't call them (prevents infinite retry loops).
            lc_tools = [t for t in lc_tools if t.name not in destructive_names]
        else:
            # Interactive sessions: gate destructive tools with interrupt() —
            # the graph will pause, yield an "interrupt" event, and wait for
            # user approval before actually executing the tool.
            for t in lc_tools:
                if t.name in destructive_names:
                    _wrap_with_interrupt_gate(t)

        # Wrap every tool so exceptions are returned to the LLM as error
        # messages instead of crashing the stream.  LangChain's built-in
        # handle_tool_error only catches ToolException; external toolkit
        # tools (e.g. Calendar) may raise plain Exception.
        # NOTE: GraphInterrupt must NOT be caught — it's used by LangGraph
        # to implement the interrupt/resume flow.
        from langgraph.errors import GraphInterrupt

        for t in lc_tools:
            if hasattr(t, "func") and t.func is not None:
                # StructuredTool / Tool created via from_function
                _orig_func = t.func
                def _safe_func(*args, _fn=_orig_func, **kwargs):
                    try:
                        return _fn(*args, **kwargs)
                    except GraphInterrupt:
                        raise  # Must propagate for interrupt/resume flow
                    except Exception as exc:
                        logger.error("Tool %s raised an error: %s", _fn.__name__ if hasattr(_fn, '__name__') else '?', exc, exc_info=True)
                        return f"Tool error: {exc}"
                t.func = _safe_func
            else:
                # Toolkit tools that override _run directly
                _orig_run = t._run
                def _safe_run(*args, _fn=_orig_run, **kwargs):
                    try:
                        return _fn(*args, **kwargs)
                    except GraphInterrupt:
                        raise
                    except Exception as exc:
                        logger.error("Tool _run raised an error: %s", exc, exc_info=True)
                        return f"Tool error: {exc}"
                t._run = _safe_run

        if not lc_tools:
            # Agent without tools is pointless — fall back to plain LLM
            lc_tools = []

        agent = create_react_agent(
            model=get_llm(),
            tools=lc_tools,
            prompt=AGENT_SYSTEM_PROMPT,
            pre_model_hook=_pre_model_trim,
            checkpointer=checkpointer,
            name="thoth_agent",
        )
        _agent_cache[cache_key] = agent

    return _agent_cache[cache_key]


def invoke_agent(user_input: str, enabled_tool_names: list[str], config: dict) -> str:
    """Invoke the ReAct agent and return the final answer text."""
    agent = get_agent_graph(enabled_tool_names)

    # Set thread-local so _pre_model_trim can find the summary cache
    _current_thread_id_var.set(
        (config.get("configurable") or {}).get("thread_id", "")
    )

    # Summarize if context is above threshold
    if _should_summarize(agent, config, user_input):
        _do_summarize(agent, config)

    result = agent.invoke(
        {"messages": [("human", user_input)]},
        config=config,
    )
    # The agent returns messages; the last AI message is the answer
    messages = result.get("messages", [])
    for msg in reversed(messages):
        if hasattr(msg, "type") and msg.type == "ai" and msg.content:
            return msg.content
    return "I wasn't able to generate a response."


import re as _re

# Map tool func names (search_xxx) back to display names
_TOOL_DISPLAY_NAMES: dict[str, str] = {}


def _resolve_tool_display_name(func_name: str) -> str:
    """Convert tool function name to display name using the registry.
    For multi-tool entries (e.g. filesystem), map sub-tool names back
    to the parent tool's display name."""
    if not _TOOL_DISPLAY_NAMES:
        for t in tool_registry.get_all_tools():
            _TOOL_DISPLAY_NAMES[t.name] = t.display_name
            # Also map sub-tool names for tools that return multiple
            try:
                for lc_tool in t.as_langchain_tools():
                    if lc_tool.name != t.name:
                        _TOOL_DISPLAY_NAMES[lc_tool.name] = t.display_name
            except Exception:
                pass  # tool not configured yet — sub-names added on rebuild
    return _TOOL_DISPLAY_NAMES.get(func_name, func_name)


def stream_agent(user_input: str, enabled_tool_names: list[str], config: dict,
                  *, stop_event: threading.Event | None = None):
    """Stream the agent response as structured events.

    Yields tuples of ``(event_type, payload)`` where *event_type* is one of:

    * ``"tool_call"``   – payload = tool display name (str)
    * ``"tool_done"``   – payload = tool display name (str)
    * ``"thinking"``    – payload = ``None`` (model is reasoning)
    * ``"token"``       – payload = token text (str)
    * ``"interrupt"``   – payload = interrupt data dict (graph is paused)
    * ``"summarizing"`` – payload = ``None`` (condensing older context)
    * ``"done"``        – payload = full answer text (str)
    """
    agent = get_agent_graph(enabled_tool_names)

    # Set thread-local so _pre_model_trim can find the summary cache
    _current_thread_id_var.set(
        (config.get("configurable") or {}).get("thread_id", "")
    )

    # ── Context summarization (runs before the main agent stream) ────
    if _should_summarize(agent, config, user_input):
        yield ("summarizing", None)
        _do_summarize(agent, config)

    yield from _stream_graph(agent, {"messages": [("human", user_input)]}, config,
                             stop_event=stop_event)


def repair_orphaned_tool_calls(enabled_tool_names: list[str] | None = None, config: dict | None = None) -> None:
    """Patch the checkpoint so every AIMessage tool_call has a ToolMessage.

    Called after stop-generation to prevent
    ``INVALID_CHAT_HISTORY`` errors on the next query.
    """
    if config is None:
        return
    try:
        agent = get_agent_graph(enabled_tool_names)
        state = agent.get_state(config)
        if not state or not state.values:
            return
        msgs = state.values.get("messages", [])
        if not msgs:
            return

        # Collect IDs of existing ToolMessages
        answered = {m.tool_call_id for m in msgs if m.type == "tool"}

        # Find orphaned tool_calls in AIMessages
        patches: list[ToolMessage] = []
        for m in msgs:
            for tc in getattr(m, "tool_calls", []):
                if tc.get("id") and tc["id"] not in answered:
                    patches.append(ToolMessage(
                        content="[Cancelled by user]",
                        name=tc["name"],
                        tool_call_id=tc["id"],
                    ))

        if patches:
            agent.update_state(config, {"messages": patches})
        # Always add a visible stop marker so the conversation reloads correctly
        agent.update_state(config, {"messages": [
            AIMessage(content="\u23f9\ufe0f *[Stopped]*")
        ]})
    except Exception:
        logger.debug("repair_orphaned_tool_calls failed", exc_info=True)


def resume_stream_agent(enabled_tool_names: list[str], config: dict, approved: bool,
                        *, stop_event: threading.Event | None = None):
    """Resume an interrupted agent graph after user approval/denial.

    Yields the same ``(event_type, payload)`` tuples as ``stream_agent``.
    """
    agent = get_agent_graph(enabled_tool_names)
    yield from _stream_graph(agent, Command(resume=approved), config,
                             stop_event=stop_event)


def _stream_graph(agent, input_data, config: dict,
                  *, stop_event: threading.Event | None = None):
    """Shared streaming logic for both initial invocation and resume."""
    full_answer = []
    thinking_signalled = False
    _seen_tool_calls: set[str] = set()

    try:
        stream_iter = agent.stream(
            input_data,
            config=config,
            stream_mode=["messages", "updates"],
        )
    except Exception as exc:
        if "does not support tools" in str(exc) or "status code: 400" in str(exc):
            yield ("error", f"{get_current_model()} does not support tool calling. "
                   "Please switch to a compatible model in Settings → Models.")
        else:
            yield ("error", str(exc))
        return

    try:
      for event in stream_iter:
        # ── Stop-button cancellation ─────────────────────────────────────
        if stop_event and stop_event.is_set():
            break

        mode, data = event

        # ── updates: tool call / tool result events ──────────────────────────
        if mode == "updates":
            if not isinstance(data, dict):
                continue
            for node, ndata in data.items():
                if not isinstance(ndata, dict):
                    continue
                for m in ndata.get("messages", []):
                    # Tool call initiated by the agent
                    tc_list = getattr(m, "tool_calls", [])
                    if tc_list:
                        for tc in tc_list:
                            tc_id = tc.get("id", tc["name"])
                            if tc_id not in _seen_tool_calls:
                                _seen_tool_calls.add(tc_id)
                                yield ("tool_call", _resolve_tool_display_name(tc["name"]))
                    # Tool result returned
                    if m.type == "tool":
                        yield ("tool_done", {
                            "name": _resolve_tool_display_name(m.name),
                            "raw_name": m.name,
                            "content": getattr(m, "content", ""),
                        })

        # ── messages: token-level streaming ──────────────────────────────────
        elif mode == "messages":
            msg, meta = data

            # Only process AI message chunks from the agent node
            # (skip tool results, human msgs, and tools-node broadcasts)
            class_name = type(msg).__name__
            if class_name != "AIMessageChunk":
                continue
            if meta.get("langgraph_node") != "agent":
                continue

            # Skip chunks that are part of a tool-call decision
            if getattr(msg, "tool_calls", []) or getattr(msg, "tool_call_chunks", []):
                continue

            content = msg.content
            if not content:
                # Empty content = thinking phase for reasoning models
                if not thinking_signalled:
                    thinking_signalled = True
                    yield ("thinking", None)
                continue

            # Strip <think>…</think> blocks from thinking models
            cleaned = _re.sub(r"<think>.*?</think>", "", content, flags=_re.DOTALL)
            cleaned = _re.sub(r"</?think>", "", cleaned)

            if cleaned:
                thinking_signalled = False
                full_answer.append(cleaned)
                yield ("token", cleaned)
    except Exception as exc:
        if "does not support tools" in str(exc) or "status code: 400" in str(exc):
            yield ("error", f"{get_current_model()} does not support tool calling. "
                   "Please switch to a compatible model in Settings → Models.")
        else:
            yield ("error", str(exc))
        return

    # Check if the graph paused due to an interrupt (destructive tool gate)
    state = agent.get_state(config)
    if state and state.next:
        # Graph has pending nodes — look for interrupt data
        for task in state.tasks:
            if hasattr(task, "interrupts") and task.interrupts:
                yield ("interrupt", task.interrupts[0].value)
                return

    yield ("done", "".join(full_answer))

if __name__ == "__main__":
    config = pick_or_create_thread()
    print("Type your questions below. Type 'quit' to exit, 'switch' to change threads.\n")
    while True:
        user_input = input("You: ").strip()
        if not user_input:
            continue
        if user_input.lower() == "quit":
            break
        if user_input.lower() == "switch":
            config = pick_or_create_thread()
            continue

        enabled = [t.name for t in tool_registry.get_enabled_tools()]
        answer = invoke_agent(user_input, enabled, config)
        print(f"\nAssistant: {answer}\n")

