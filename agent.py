from models import get_llm, get_context_size, get_current_model
from api_keys import apply_keys
from langchain_classic.retrievers import ContextualCompressionRetriever
from langchain_classic.retrievers.document_compressors import LLMChainExtractor
from langchain_core.messages import trim_messages, ToolMessage
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
    max_tokens = int(get_context_size() * 0.8)

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
                            f"- [{m['category']}] {m['subject']}: {m['content']}"
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
                            "Do not mention that these were recalled from memory."
                        )
                    )
                    trimmed.insert(last_human_idx, recall_msg)
    except Exception as exc:
        logger.debug("Auto-recall failed (non-fatal): %s", exc)

    return {"llm_input_messages": trimmed}

AGENT_SYSTEM_PROMPT = (
    "You are Thoth, a knowledgeable personal assistant with access to tools.\n\n"
    "TOOL USE GUIDELINES:\n"
    "- ALWAYS use your tools to look up information before answering factual questions.\n"
    "- For anything time-sensitive (news, weather, prices, scores, releases, events,\n"
    "  current status, 'latest', 'recent', 'today', 'this week', etc.) you MUST\n"
    "  search the web — do NOT rely on your training data for these.\n"
    "- For facts that can change over time (populations, leaders, rankings, statistics,\n"
    "  laws, versions, availability) prefer searching over internal knowledge.\n"
    "- You may call multiple tools or the same tool multiple times with different queries.\n"
    "- Only use internal knowledge for well-established, timeless facts (math, definitions,\n"
    "  historical events with fixed dates, etc.).\n"
    "- When researching a topic, consider using the youtube_search tool to find\n"
    "  relevant videos. Only include video links that the tool actually returned.\n"
    "- When the user asks to set a reminder, timer, or alarm, decide which tool to use:\n"
    "  * For quick timers (minutes to a few hours): use the set_timer tool. It shows\n"
    "    a desktop notification when the time is up. Say 'remind me in 5 minutes' etc.\n"
    "  * For day-level reminders or scheduled events: use create_calendar_event.\n"
    "    Google Calendar handles notifications across devices.\n"
    "  Use get_current_datetime to determine the current time when needed.\n"
    "- When the user asks about weather or forecasts, use the weather tools\n"
    "  (get_current_weather, get_weather_forecast). They provide precise data\n"
    "  from Open-Meteo and are faster than web search for weather queries.\n"
    "- You have DIRECT ACCESS to the user's webcam and screen through the\n"
    "  analyze_image tool. You CAN see — this is not hypothetical. When the user\n"
    "  says anything like 'what do you see', 'look at this', 'can you see me',\n"
    "  'what's in front of me', 'describe what you see', or any variation asking\n"
    "  you to look or see, IMMEDIATELY call analyze_image — do NOT ask for\n"
    "  clarification, do NOT say you can't see, do NOT ask them to describe it.\n"
    "  Just call the tool. Use source='camera' by default. Use source='screen'\n"
    "  when they mention screen, monitor, display, or desktop.\n"
    "  Pass the user's question as the argument (or 'Describe everything you see'\n"
    "  if the question is vague like 'what do you see').\n"
    "- When the user asks a math or calculation question, choose the right tool:\n"
    "  * For basic arithmetic, powers, roots, trig, logarithms, factorials, and\n"
    "    combinatorics: use the calculate tool. It is fast, offline, and free.\n"
    "  * For unit/currency conversion, symbolic math (solving equations, derivatives,\n"
    "    integrals), scientific data, chemistry, physics constants, nutrition,\n"
    "    date calculations, or anything beyond basic math: use wolfram_alpha.\n"
    "  * If you are unsure, try the calculate tool first. If it fails or the query\n"
    "    requires natural-language understanding, fall back to wolfram_alpha.\n\n"
    "HABIT / ACTIVITY TRACKING:\n"
    "- You have a habit tracker for logging recurring activities: medications,\n"
    "  symptoms, habits, health events (periods, headaches, exercise, mood, etc.).\n"
    "- When a user mentions something that matches an existing tracker — e.g.\n"
    "  'I have a headache' when Headache is tracked — ask: 'Want me to log that?'\n"
    "  before logging.  Never log silently.\n"
    "- Use tracker_log to record entries, tracker_query for history/stats/trends.\n"
    "- tracker_query exports CSV files that you can pass to create_chart for\n"
    "  visualisations (bar charts of frequency, line charts of values over time).\n\n"
    "DATA VISUALISATION:\n"
    "- When you analyse tabular data (CSV, Excel, JSON) and the results would be\n"
    "  clearer as a chart, use the create_chart tool to render an interactive\n"
    "  Plotly chart inline.  Supported types: bar, horizontal_bar, line, scatter,\n"
    "  pie, donut, histogram, box, area, heatmap.\n"
    "- Common triggers: user asks to 'plot', 'chart', 'graph', 'visualise', or\n"
    "  when comparing categories, showing trends over time, or displaying\n"
    "  distributions.  You may also proactively suggest a chart when it adds value.\n"
    "- Pass the data_source (file path or attachment filename), chart_type,\n"
    "  and column names. The tool auto-picks columns if you omit x/y.\n\n"
    "MEMORY GUIDELINES:\n"
    "- You have a long-term memory system. Use it to remember important personal\n"
    "  information the user shares: names, birthdays, relationships, preferences,\n"
    "  facts, upcoming events, places, and projects.\n"
    "- When the user tells you something worth remembering (e.g. 'My mom's name is\n"
    "  Sarah', 'I prefer dark mode', 'My project deadline is June 1'), save it\n"
    "  using save_memory with an appropriate category.\n"
    "- IMPORTANT: If the user casually mentions personal information (moving,\n"
    "  birthdays, names, preferences, pets, relationships) alongside another\n"
    "  request, you MUST save that info AND handle their request. Do both.\n"
    "- Relevant memories are automatically recalled and shown to you before each\n"
    "  response.  Use them to answer directly — do not say 'I don't know' when\n"
    "  the information is in your recalled memories.  If you need a deeper or\n"
    "  more focused search, use search_memory.\n"
    "- Categories: person (people and relationships), preference (likes/dislikes/\n"
    "  settings), fact (general knowledge about the user), event (dates/deadlines/\n"
    "  appointments), place (locations/addresses), project (work/hobby projects).\n"
    "- Do NOT save trivial or transient information (e.g. 'search for X', 'what\n"
    "  time is it'). Only save things with long-term personal value.\n"
    "- Do NOT save information that is being tracked by the tracker tool.\n"
    "  If you already called tracker_log for something (medications, symptoms,\n"
    "  exercise, periods, mood, sleep), do NOT also save_memory for it.\n"
    "- When saving, briefly confirm what you remembered to the user.\n\n"
    "CONVERSATION HISTORY SEARCH:\n"
    "- When the user asks about something discussed in a previous conversation\n"
    "  (e.g. 'What did I ask about taxes?', 'When did we talk about Python?',\n"
    "  'Find where I mentioned that recipe'), use search_conversations.\n"
    "- When the user asks to see their saved threads or chat history, use\n"
    "  list_conversations.\n\n"
    "SOURCE CITATION RULES:\n"
    "- Each tool result contains a SOURCE_URL line with the exact link or file path.\n"
    "- You MUST cite the EXACT SOURCE_URL value from the tool output.\n"
    "- Format citations as: (Source: <exact SOURCE_URL value>)\n"
    "- NEVER paraphrase, shorten, or summarize source URLs. Copy them verbatim.\n"
    "- NEVER invent, fabricate, or guess URLs. Only include URLs that appear\n"
    "  verbatim in tool results. If a tool did not return a URL, do NOT make one up.\n"
    "- Do NOT generate arxiv.org, youtube.com, or any other links from memory.\n"
    "  Only use links that tools explicitly returned to you.\n"
    "- If you use your internal knowledge, cite as (Source: Internal Knowledge).\n"
    "- If you don't know the answer, say you don't know."
)

# Cache compiled agent graphs keyed by frozenset of enabled tool names
_agent_cache: dict[frozenset[str], object] = {}

# Thread-local flag — background workflows skip destructive tools
import threading as _threading
_tlocal = _threading.local()

# Human-readable labels for destructive tool operations
_DESTRUCTIVE_LABELS: dict[str, str] = {
    "file_delete": "Delete file",
    "move_file": "Move / rename file",
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
        # Mirror _pre_model_trim: trim, then count what remains
        budget = int(max_tokens * 0.8)
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
            for lc_tool in t.as_langchain_tools():
                if lc_tool.name != t.name:
                    _TOOL_DISPLAY_NAMES[lc_tool.name] = t.display_name
    return _TOOL_DISPLAY_NAMES.get(func_name, func_name)


def stream_agent(user_input: str, enabled_tool_names: list[str], config: dict):
    """Stream the agent response as structured events.

    Yields tuples of ``(event_type, payload)`` where *event_type* is one of:

    * ``"tool_call"``   – payload = tool display name (str)
    * ``"tool_done"``   – payload = tool display name (str)
    * ``"thinking"``    – payload = ``None`` (model is reasoning)
    * ``"token"``       – payload = token text (str)
    * ``"interrupt"``   – payload = interrupt data dict (graph is paused)
    * ``"done"``        – payload = full answer text (str)
    """
    agent = get_agent_graph(enabled_tool_names)
    yield from _stream_graph(agent, {"messages": [("human", user_input)]}, config)


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
    except Exception:
        logger.debug("repair_orphaned_tool_calls failed", exc_info=True)


def resume_stream_agent(enabled_tool_names: list[str], config: dict, approved: bool):
    """Resume an interrupted agent graph after user approval/denial.

    Yields the same ``(event_type, payload)`` tuples as ``stream_agent``.
    """
    agent = get_agent_graph(enabled_tool_names)
    yield from _stream_graph(agent, Command(resume=approved), config)


def _stream_graph(agent, input_data, config: dict):
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

