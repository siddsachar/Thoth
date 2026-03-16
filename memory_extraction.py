"""Background memory extraction — scans past conversations for personal facts.

Runs at app startup and periodically (every ~6 hours) to catch memories
the agent missed during live conversation.  Uses the user's current LLM
model to extract personal facts, then deduplicates against existing
memories before saving.

Stores the last extraction timestamp so it only processes new/updated
threads since the previous run.
"""

from __future__ import annotations

import json
import logging
import pathlib
import os
import threading
from datetime import datetime

logger = logging.getLogger(__name__)

# ── Persistence ──────────────────────────────────────────────────────────────
_DATA_DIR = pathlib.Path(
    os.environ.get("THOTH_DATA_DIR", pathlib.Path.home() / ".thoth")
)
_DATA_DIR.mkdir(parents=True, exist_ok=True)
_STATE_FILE = _DATA_DIR / "memory_extraction_state.json"

_INTERVAL_S = 6 * 3600  # 6 hours

# Thread IDs to exclude from background extraction (e.g. currently active
# conversations).  Updated by the UI layer via ``set_active_thread``.
_active_threads: set[str] = set()
_active_lock = threading.Lock()


def set_active_thread(thread_id: str | None, previous_id: str | None = None) -> None:
    """Tell the extractor which thread is currently active.

    Call this whenever the user switches threads.  *previous_id* (if given)
    is removed from the exclusion set so it becomes eligible for future
    extraction runs.
    """
    with _active_lock:
        if previous_id and previous_id in _active_threads:
            _active_threads.discard(previous_id)
        if thread_id:
            _active_threads.add(thread_id)


def _load_state() -> dict:
    if _STATE_FILE.exists():
        try:
            return json.loads(_STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_state(state: dict) -> None:
    _STATE_FILE.write_text(json.dumps(state, indent=2))


from prompts import EXTRACTION_PROMPT


# ── Core extraction logic ────────────────────────────────────────────────────

def _get_thread_messages(thread_id: str) -> list[dict]:
    """Load messages from a thread via the LangGraph checkpointer."""
    try:
        from agent import get_agent_graph
        from threads import checkpointer  # noqa: F811

        config = {"configurable": {"thread_id": thread_id}}
        agent = get_agent_graph()
        state = agent.get_state(config)
        if not state or not state.values:
            return []
        messages = state.values.get("messages", [])
        result = []
        for m in messages:
            role = "user" if m.type == "human" else ("assistant" if m.type == "ai" else None)
            content = getattr(m, "content", "") or ""
            if role and content.strip():
                result.append({"role": role, "content": content[:2000]})
        return result
    except Exception as exc:
        logger.debug("Could not load thread %s: %s", thread_id, exc)
        return []


def _format_conversation(messages: list[dict]) -> str:
    """Format messages into a readable conversation string."""
    lines = []
    for m in messages:
        prefix = "User" if m["role"] == "user" else "Assistant"
        lines.append(f"{prefix}: {m['content']}")
    return "\n".join(lines)


def _extract_from_conversation(conversation_text: str) -> list[dict]:
    """Call the LLM to extract personal facts from a conversation."""
    import re
    try:
        from models import get_current_model
        import ollama

        prompt = EXTRACTION_PROMPT.format(conversation=conversation_text)
        response = ollama.chat(
            model=get_current_model(),
            messages=[{"role": "user", "content": prompt}],
            options={"temperature": 0.1, "num_ctx": 4096},
        )
        raw = response["message"]["content"].strip()

        # Strip <think>...</think> blocks from reasoning models
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
        raw = re.sub(r"</?think>", "", raw).strip()

        # Try to find JSON array in the response
        # Look for [...] pattern
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        if not match:
            return []
        data = json.loads(match.group())
        if not isinstance(data, list):
            return []
        # Validate each entry
        valid = []
        for entry in data:
            if (
                isinstance(entry, dict)
                and entry.get("category")
                and entry.get("subject")
                and entry.get("content")
            ):
                valid.append(entry)
        return valid
    except Exception as exc:
        logger.warning("Memory extraction LLM call failed: %s", exc)
        return []


def _dedup_and_save(extracted: list[dict]) -> int:
    """Save extracted memories, deduplicating against existing ones.

    Uses ``find_by_subject(category=None, ...)`` — a deterministic SQL
    lookup by normalised subject across **all** categories.  This avoids
    duplicates when the extraction LLM classifies a fact into a different
    category than the live tool did (e.g. ``event/dad`` vs ``person/Dad``).

    Returns the number of new/updated memories.
    """
    from memory import save_memory, find_by_subject, update_memory, VALID_CATEGORIES

    saved_count = 0
    for entry in extracted:
        category = entry["category"].lower().strip()
        if category not in VALID_CATEGORIES:
            continue
        subject = entry["subject"].strip()
        content = entry["content"].strip()
        if not subject or not content:
            continue

        # Check for existing memory with same subject (any category)
        existing = find_by_subject(None, subject)

        if existing:
            # Memory about this subject already exists — only update if
            # the extracted content is richer than what we have.
            if len(content) > len(existing.get("content", "")):
                try:
                    update_memory(existing["id"], content, source="extraction")
                    saved_count += 1
                    logger.info(
                        "Updated memory %s (%s) via extraction",
                        existing["id"], subject,
                    )
                except Exception as exc:
                    logger.debug("Failed to update memory: %s", exc)
            # else: existing content is already richer, skip
        else:
            # No match — save as new
            try:
                save_memory(category, subject, content, source="extraction")
                saved_count += 1
                logger.info("Auto-saved memory: [%s] %s", category, subject)
            except Exception as exc:
                logger.debug("Failed to save memory: %s", exc)

    return saved_count


# ── Public API ───────────────────────────────────────────────────────────────

def run_extraction(on_status=None, exclude_thread_ids: set[str] | None = None) -> int:
    """Scan threads updated since last extraction and extract memories.

    Parameters
    ----------
    on_status : callable, optional
        Called with status strings for UI feedback, e.g. ``on_status("Processing 3 threads…")``.
    exclude_thread_ids : set[str], optional
        Thread IDs to skip (e.g. the currently active conversation) to
        avoid racing with live tool calls.

    Returns
    -------
    int
        Number of new/updated memories saved.
    """
    from threads import _list_threads

    state = _load_state()
    last_run = state.get("last_extraction", "2000-01-01T00:00:00")
    exclude = exclude_thread_ids or set()

    threads = _list_threads()
    if not threads:
        if on_status:
            on_status("No conversations to process")
        state["last_extraction"] = datetime.now().isoformat()
        _save_state(state)
        return 0

    # Find threads updated since last extraction, excluding active ones
    new_threads = []
    for tid, name, created, updated in threads:
        if tid in exclude:
            continue
        if updated and updated > last_run:
            new_threads.append((tid, name))

    if not new_threads:
        if on_status:
            on_status("No new conversations since last extraction")
        state["last_extraction"] = datetime.now().isoformat()
        _save_state(state)
        return 0

    if on_status:
        on_status(f"Scanning {len(new_threads)} conversation(s) for memories…")

    total_saved = 0
    for tid, name in new_threads:
        messages = _get_thread_messages(tid)
        # Only process threads with user messages
        user_msgs = [m for m in messages if m["role"] == "user"]
        if not user_msgs:
            continue

        # Build conversation text (cap at ~6000 chars to fit in context)
        conv_text = _format_conversation(messages)
        if len(conv_text) > 6000:
            conv_text = conv_text[:6000] + "\n[... truncated]"

        if on_status:
            on_status(f"Extracting memories from: {name}")

        extracted = _extract_from_conversation(conv_text)
        if extracted:
            count = _dedup_and_save(extracted)
            total_saved += count
            logger.info("Thread '%s': extracted %d, saved %d", name, len(extracted), count)

    state["last_extraction"] = datetime.now().isoformat()
    _save_state(state)

    if on_status:
        if total_saved:
            on_status(f"Extracted {total_saved} new memory(s)")
        else:
            on_status("No new memories found")

    return total_saved


# ── Background timer ─────────────────────────────────────────────────────────

_timer_thread: threading.Thread | None = None
_timer_stop = threading.Event()


def start_periodic_extraction() -> None:
    """Start a daemon thread that runs extraction every 6 hours."""
    global _timer_thread
    if _timer_thread is not None and _timer_thread.is_alive():
        return

    _timer_stop.clear()

    def _loop():
        while not _timer_stop.wait(timeout=_INTERVAL_S):
            logger.info("Periodic memory extraction starting…")
            try:
                with _active_lock:
                    exclude = set(_active_threads)
                count = run_extraction(exclude_thread_ids=exclude)
                logger.info("Periodic extraction complete: %d memories", count)
            except Exception as exc:
                logger.warning("Periodic extraction failed: %s", exc)

    _timer_thread = threading.Thread(target=_loop, daemon=True, name="thoth-mem-extract")
    _timer_thread.start()
    logger.info("Periodic memory extraction scheduled every %d hours", _INTERVAL_S // 3600)


def stop_periodic_extraction() -> None:
    """Signal the periodic extraction thread to stop."""
    _timer_stop.set()
