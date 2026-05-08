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
import time
from datetime import datetime

logger = logging.getLogger(__name__)

# ── Persistence ──────────────────────────────────────────────────────────────
_DATA_DIR = pathlib.Path(
    os.environ.get("THOTH_DATA_DIR", pathlib.Path.home() / ".thoth")
)
_DATA_DIR.mkdir(parents=True, exist_ok=True)
_STATE_FILE = _DATA_DIR / "memory_extraction_state.json"
_JOURNAL_FILE = _DATA_DIR / "extraction_journal.json"
_JOURNAL_MAX_ENTRIES = 100

_INTERVAL_S = 2 * 3600  # 2 hours
_IDLE_DELAY_S = 5 * 60

# Thread IDs to exclude from background extraction (e.g. currently active
# conversations).  Updated by the UI layer via ``set_active_thread``.
_active_threads: set[str] = set()
_active_lock = threading.Lock()
_activity_lock = threading.Lock()
_last_activity_ts = time.monotonic()
_idle_once_thread: threading.Thread | None = None


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
    mark_user_activity("thread switch")


def mark_user_activity(reason: str = "user") -> None:
    """Record foreground activity so heavy extraction can wait for idle."""
    global _last_activity_ts
    with _activity_lock:
        _last_activity_ts = time.monotonic()
    logger.debug("Memory extraction idle timer reset: %s", reason)


def idle_seconds() -> float:
    with _activity_lock:
        return max(0.0, time.monotonic() - _last_activity_ts)


def is_app_idle(min_idle_s: float = _IDLE_DELAY_S) -> bool:
    """Return True when heavyweight background memory work may run."""
    if idle_seconds() < min_idle_s:
        return False
    try:
        from ui.state import _active_generations
        if _active_generations:
            return False
    except Exception:
        pass
    try:
        from document_extraction import get_extraction_status
        status = get_extraction_status()
        if status and status.get("status") == "running":
            return False
    except Exception:
        pass
    try:
        from tasks import get_running_tasks
        if get_running_tasks():
            return False
    except Exception:
        pass
    return True


def _load_state() -> dict:
    if _STATE_FILE.exists():
        try:
            return json.loads(_STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def get_extraction_status() -> dict:
    """Return extraction status info for the Activity panel."""
    st = _load_state()
    return {
        "last_extraction": st.get("last_extraction"),
        "interval_hours": _INTERVAL_S / 3600,
        "threads_scanned": st.get("threads_scanned", 0),
        "entities_saved": st.get("entities_saved", 0),
        "islands_repaired": st.get("islands_repaired", 0),
    }


def _save_state(state: dict) -> None:
    _STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Extraction journal ───────────────────────────────────────────────────────

def _load_extraction_journal() -> list[dict]:
    if _JOURNAL_FILE.exists():
        try:
            return json.loads(_JOURNAL_FILE.read_text())
        except Exception:
            pass
    return []


def _save_extraction_journal(journal: list[dict]) -> None:
    _JOURNAL_FILE.write_text(json.dumps(journal, indent=2))


def _append_extraction_journal(entry: dict) -> None:
    journal = _load_extraction_journal()
    journal.append(entry)
    if len(journal) > _JOURNAL_MAX_ENTRIES:
        journal = journal[-_JOURNAL_MAX_ENTRIES:]
    _save_extraction_journal(journal)


def get_extraction_journal(limit: int = 10) -> list[dict]:
    """Return the most recent extraction journal entries."""
    journal = _load_extraction_journal()
    return journal[-limit:] if limit else journal


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
            if isinstance(content, list):
                text_parts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif isinstance(block, str):
                        text_parts.append(block)
                content = "\n".join(text_parts)
            if not isinstance(content, str):
                content = str(content) if content else ""
            if role and content.strip():
                result.append({"role": role, "content": content[:2000]})
        return result
    except Exception as exc:
        logger.debug("Could not load thread %s: %s", thread_id, exc)
        return []


_ASSISTANT_TRUNCATE = 200  # chars — enough context, not enough to extract from


def _format_conversation(messages: list[dict]) -> str:
    """Format messages into a readable conversation string.

    User messages are included in full (they contain the facts we want).
    Assistant messages are truncated to ``_ASSISTANT_TRUNCATE`` chars to
    prevent the LLM from extracting facts from its own output — search
    results, file listings, generated stories, workflow reports, etc.
    """
    lines = []
    for m in messages:
        if m["role"] == "user":
            lines.append(f"User: {m['content']}")
        else:
            text = m["content"]
            if len(text) > _ASSISTANT_TRUNCATE:
                text = text[:_ASSISTANT_TRUNCATE] + " [...]"
            lines.append(f"Assistant: {text}")
    return "\n".join(lines)


def _extract_from_conversation(conversation_text: str) -> list[dict]:
    """Call the LLM to extract personal facts from a conversation."""
    import re
    try:
        from models import get_current_model, get_llm_for
        from langchain_core.messages import HumanMessage

        prompt = EXTRACTION_PROMPT.format(conversation=conversation_text)
        llm = get_llm_for(get_current_model())
        response = llm.invoke([HumanMessage(content=prompt)])
        raw = response.content or ""
        if isinstance(raw, list):
            text_parts = []
            for block in raw:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif isinstance(block, str):
                    text_parts.append(block)
            raw = "\n".join(text_parts)
        if not isinstance(raw, str):
            raw = str(raw) if raw else ""
        raw = raw.strip()

        # Strip <think>...</think> blocks from reasoning models
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
        raw = re.sub(r"</?think>", "", raw).strip()

        # Try to find JSON array in the response (bracket-counting
        # handles nested arrays correctly, unlike greedy regex).
        import knowledge_graph as _kg_parse
        _json_str = _kg_parse.extract_json_block(raw, "[")
        if not _json_str:
            return []
        data = json.loads(_json_str)
        if not isinstance(data, list):
            return []
        # Validate each entry
        valid = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            # Entity object: has category + subject + content
            if (
                entry.get("category")
                and entry.get("subject")
                and entry.get("content")
            ):
                valid.append(entry)
            # Relation object: has relation_type + source_subject + target_subject
            elif (
                entry.get("relation_type")
                and entry.get("source_subject")
                and entry.get("target_subject")
            ):
                valid.append(entry)
        return valid
    except Exception as exc:
        logger.warning("Memory extraction LLM call failed: %s", exc)
        return []


def _dedup_and_save(extracted: list[dict], source: str = "extraction") -> int:
    """Save extracted memories and relations, deduplicating against existing ones.

    Uses ``find_by_subject(category=None, ...)`` — a deterministic SQL
    lookup by normalised subject across **all** categories.  This avoids
    duplicates when the extraction LLM classifies a fact into a different
    category than the live tool did (e.g. ``event/dad`` vs ``person/Dad``).

    Also processes extracted ``relations`` — connecting entities that
    the LLM identified as related.

    Parameters
    ----------
    source : str
        Value for the ``source`` field on saved entities/relations.
        Defaults to ``"extraction"`` for conversation extraction.
        Document extraction passes ``"document:<filename>"``.

    Returns the number of new/updated memories + relations.
    """
    from memory import save_memory, find_by_subject, update_memory, VALID_CATEGORIES
    import knowledge_graph as kg

    # Suppress per-entity rebuild_index() — we do one rebuild at the end.
    kg._skip_reindex = True

    saved_count = 0
    try:

        # ── Pass 1: save/update entities and build a subject→id map ──────
        subject_to_id: dict[str, str] = {}

        # Pre-populate the map with the "User" entity if it exists
        user_entity = find_by_subject(None, "User")
        if user_entity:
            subject_to_id[kg._normalize_subject("User")] = user_entity["id"]

        for entry in extracted:
            category = entry.get("category", "").lower().strip()
            if category not in VALID_CATEGORIES:
                continue
            subject = entry["subject"].strip()
            content = entry["content"].strip()
            if not subject or not content:
                continue

            # Extract optional aliases from the LLM output (may be str or list)
            raw_aliases = entry.get("aliases", "")
            if isinstance(raw_aliases, list):
                raw_aliases = ", ".join(str(a) for a in raw_aliases)
            new_aliases = (raw_aliases or "").strip()

            # Check for existing memory with same subject (any category)
            existing = find_by_subject(None, subject)

            # FAISS semantic fallback — catches synonyms (e.g. "Father" vs "Dad")
            # Use higher threshold when source classes differ to avoid merging
            # impersonal document content with personal conversation entities.
            if not existing:
                try:
                    _hits = kg.semantic_search(
                        f"{subject}: {content}", top_k=1, threshold=0.80,
                    )
                    if _hits:
                        hit = _hits[0]
                        hit_source = (hit.get("source") or "").strip()
                        # Cross-source check: document vs non-document
                        src_is_doc = source.startswith("document:")
                        hit_is_doc = hit_source.startswith("document:")
                        if src_is_doc != hit_is_doc:
                            # Require tighter threshold for cross-source merges
                            score = hit.get("score", 0)
                            if score < 0.90:
                                logger.info(
                                    "Cross-source merge skipped (%.2f < 0.90): '%s' vs '%s' (src=%s, hit=%s)",
                                    score, subject, hit.get("subject", "?"), source, hit_source,
                                )
                                hit = None
                        if hit:
                            existing = hit
                except Exception:
                    pass

            if existing:
                subject_to_id[kg._normalize_subject(subject)] = existing["id"]

                # Merge aliases if the LLM provided new ones
                update_kwargs: dict = {}
                if new_aliases:
                    old_aliases = existing.get("aliases", "") or ""
                    old_set = {a.strip().lower() for a in old_aliases.split(",") if a.strip()}
                    new_set = {a.strip() for a in new_aliases.split(",") if a.strip()}
                    to_add = [a for a in new_set if a.lower() not in old_set]
                    if to_add:
                        merged = (old_aliases + ", " + ", ".join(to_add)).strip(", ")
                        update_kwargs["aliases"] = merged
                        # Also register each new alias in the subject→id map
                        for alias in to_add:
                            subject_to_id[kg._normalize_subject(alias)] = existing["id"]

                # Memory about this subject already exists — merge content if
                # the extraction produced genuinely new information.
                old_content = existing.get("content", "").strip()
                if content.lower() in old_content.lower():
                    # Extracted content already captured — nothing new
                    merged_content = old_content
                    content_changed = False
                elif old_content.lower() in content.lower():
                    # Extracted content is a superset — replace
                    merged_content = content
                    content_changed = True
                else:
                    # Both have unique info — check for contradiction before
                    # merging.  Reuse the same LLM-based check that the live
                    # memory tool uses to prevent conflicting facts.
                    try:
                        from tools.memory_tool import _check_contradiction
                        conflict = _check_contradiction(old_content, content, subject)
                        if conflict:
                            logger.warning(
                                "Extraction contradiction for '%s': %s — skipping merge",
                                subject, conflict,
                            )
                            merged_content = old_content
                            content_changed = False
                        else:
                            merged_content = f"{old_content}. {content}".replace(". . ", ". ")
                            content_changed = True
                    except Exception as exc:
                        logger.warning(
                            "Extraction contradiction check failed for '%s': %s — keeping existing content",
                            subject, exc,
                        )
                        merged_content = old_content
                        content_changed = False

                if content_changed or update_kwargs:
                    try:
                        update_memory(
                            existing["id"],
                            merged_content,
                            **update_kwargs,
                        )
                        saved_count += 1
                        logger.info(
                            "Updated memory %s (%s) via extraction",
                            existing["id"], subject,
                        )
                    except Exception as exc:
                        logger.debug("Failed to update memory: %s", exc)
                # else: existing content is already richer and no alias update needed
            else:
                # No match — save as new
                try:
                    result = save_memory(
                        category, subject, content,
                        tags="", source=source,
                    )
                    subject_to_id[kg._normalize_subject(subject)] = result["id"]

                    # If we created a new entity with aliases, update it
                    if new_aliases:
                        try:
                            update_memory(result["id"], content, aliases=new_aliases, source=source)
                            for alias in new_aliases.split(","):
                                alias = alias.strip()
                                if alias:
                                    subject_to_id[kg._normalize_subject(alias)] = result["id"]
                        except Exception:
                            pass

                    saved_count += 1
                    logger.info("Auto-saved memory: [%s] %s", category, subject)
                except Exception as exc:
                    logger.debug("Failed to save memory: %s", exc)

        # ── Pass 2: save extracted relations ─────────────────────────────
        relations = [e for e in extracted if e.get("relation_type")]
        _EXTRACTION_BANNED_TYPES = {
            "related_to", "associated_with", "connected_to", "linked_to",
            "has_relation", "involves", "correlates_with",
        }

        for rel in relations:
            src_subj = kg._normalize_subject(rel.get("source_subject", "").strip())
            tgt_subj = kg._normalize_subject(rel.get("target_subject", "").strip())
            rel_type = rel.get("relation_type", "").strip()
            rel_confidence = rel.get("confidence", 0.8)
            if not src_subj or not tgt_subj or not rel_type:
                continue

            # Pre-normalize relation type before any checks
            rel_type = kg.normalize_relation_type(rel_type)

            # Reject vague relation types that add noise to the graph
            if rel_type in _EXTRACTION_BANNED_TYPES:
                logger.info(
                    "Extraction skipped vague type '%s': %s --[%s]--> %s",
                    rel_type, rel.get("source_subject", "?"),
                    rel_type, rel.get("target_subject", "?"),
                )
                continue

            # Reject low-confidence relations (<0.80)
            if isinstance(rel_confidence, (int, float)) and rel_confidence < 0.80:
                logger.info(
                    "Extraction skipped low-confidence relation (%.2f): %s --[%s]--> %s",
                    rel_confidence, rel.get("source_subject", "?"),
                    rel_type, rel.get("target_subject", "?"),
                )
                continue

            # Resolve subjects to entity IDs
            src_id = subject_to_id.get(src_subj)
            tgt_id = subject_to_id.get(tgt_subj)

            # Try database lookup if not in our local map
            if not src_id:
                found = find_by_subject(None, rel.get("source_subject", "").strip())
                if found:
                    src_id = found["id"]
            if not tgt_id:
                found = find_by_subject(None, rel.get("target_subject", "").strip())
                if found:
                    tgt_id = found["id"]

            # FAISS semantic fallback — high threshold to avoid false matches.
            # Catches name variants the LLM uses that don't match subjects or aliases
            # (e.g. "Father" when entity stored as "Dad").
            if not src_id:
                try:
                    _hits = kg.semantic_search(
                        rel.get("source_subject", "").strip(),
                        top_k=1, threshold=0.80,
                    )
                    if _hits:
                        src_id = _hits[0]["id"]
                except Exception:
                    pass
            if not tgt_id:
                try:
                    _hits = kg.semantic_search(
                        rel.get("target_subject", "").strip(),
                        top_k=1, threshold=0.80,
                    )
                    if _hits:
                        tgt_id = _hits[0]["id"]
                except Exception:
                    pass

            if src_id and tgt_id:
                try:
                    result = kg.add_relation(
                        src_id, tgt_id, rel_type,
                        source=source,
                        confidence=rel_confidence if isinstance(rel_confidence, (int, float)) else 0.8,
                    )
                    if result:
                        saved_count += 1
                        logger.info(
                            "Auto-linked: %s --[%s]--> %s",
                            rel.get("source_subject", "?"), rel_type,
                            rel.get("target_subject", "?"),
                        )
                except Exception as exc:
                    logger.debug("Failed to save relation: %s", exc)

    finally:
        kg._skip_reindex = False

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
    # and background workflow threads (⚡ prefix) which contain only
    # AI-generated content with no user-stated facts.
    new_threads = []
    for tid, name, created, updated, *rest in threads:
        if tid in exclude:
            continue
        if name and name.startswith("⚡"):
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
    islands_repaired = 0
    journal_entry = {
        "timestamp": datetime.now().isoformat(),
        "threads_scanned": len(new_threads),
        "thread_details": [],
        "entities_saved": 0,
        "contradictions_blocked": 0,
        "low_confidence_skipped": 0,
        "errors": [],
    }

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
            journal_entry["thread_details"].append({
                "thread": name or tid,
                "extracted": len(extracted),
                "saved": count,
            })
        else:
            journal_entry["thread_details"].append({
                "thread": name or tid,
                "extracted": 0,
                "saved": 0,
            })

    # Single FAISS rebuild after ALL threads processed (not per-thread).
    # Always reset _skip_reindex — _dedup_and_save's try/finally handles
    # its own reset, but ensure the flag is clean even if no threads matched.
    try:
        import knowledge_graph as kg
        kg._skip_reindex = False
        if total_saved:
            kg.rebuild_index()
    except Exception as exc:
        logger.debug("Post-extraction rebuild_index failed: %s", exc)

    # Single wiki vault rebuild after ALL threads processed (not per-entity)
    if total_saved:
        try:
            import wiki_vault
            if wiki_vault.is_enabled():
                wiki_vault.rebuild_vault()
                logger.info("Post-extraction wiki vault rebuild complete")
        except Exception as exc:
            logger.debug("Post-extraction wiki rebuild skipped: %s", exc)

    state["last_extraction"] = datetime.now().isoformat()
    state["threads_scanned"] = len(new_threads)
    state["entities_saved"] = total_saved
    _save_state(state)

    # Append journal entry
    journal_entry["entities_saved"] = total_saved
    journal_entry["islands_repaired"] = islands_repaired
    journal_entry["summary"] = (
        f"{len(new_threads)} thread(s) scanned, {total_saved} saved, "
        f"{islands_repaired} island(s) repaired"
    )
    _append_extraction_journal(journal_entry)
    logger.info("Memory extraction complete: %s", journal_entry["summary"])

    if on_status:
        if total_saved:
            on_status(f"Extracted {total_saved} new memory(s)")
        else:
            on_status("No new memories found")

    return total_saved


def run_extraction_if_idle(on_status=None, exclude_thread_ids: set[str] | None = None) -> int:
    """Run extraction only when the foreground app is idle."""
    if not is_app_idle():
        logger.info("Memory extraction deferred; app is active (idle %.0fs)", idle_seconds())
        return 0
    return run_extraction(on_status=on_status, exclude_thread_ids=exclude_thread_ids)


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
                count = run_extraction_if_idle(exclude_thread_ids=exclude)
                logger.info("Periodic extraction complete: %d memories", count)
            except Exception as exc:
                logger.warning("Periodic extraction failed: %s", exc)

    _timer_thread = threading.Thread(target=_loop, daemon=True, name="thoth-mem-extract")
    _timer_thread.start()
    logger.info("Periodic memory extraction scheduled every %d hours", _INTERVAL_S // 3600)


def schedule_idle_extraction(delay_s: float = _IDLE_DELAY_S) -> None:
    """Schedule one best-effort idle extraction after startup."""
    global _idle_once_thread
    if _idle_once_thread is not None and _idle_once_thread.is_alive():
        return

    def _run() -> None:
        if _timer_stop.wait(timeout=delay_s):
            return
        try:
            with _active_lock:
                exclude = set(_active_threads)
            count = run_extraction_if_idle(exclude_thread_ids=exclude)
            if count:
                logger.info("Startup idle extraction complete: %d memories", count)
        except Exception as exc:
            logger.warning("Startup idle extraction failed: %s", exc)

    _idle_once_thread = threading.Thread(target=_run, daemon=True, name="thoth-mem-idle-once")
    _idle_once_thread.start()
    logger.info("Startup memory extraction deferred until idle")


def stop_periodic_extraction() -> None:
    """Signal the periodic extraction thread to stop."""
    _timer_stop.set()
