"""Dream Cycle — nightly background knowledge refinement.

Runs during a configurable quiet window (default 1–5 AM local time) when
the system is idle.  Performs three safe, non-destructive operations:

1. **Duplicate merge** — entities with ≥0.93 semantic similarity AND same
   type are auto-merged.
2. **Description enrichment** — thin entities (<80 chars) that appear in
   multiple conversations get richer descriptions.
3. **Relationship inference** — entity pairs that co-occur in the same
   conversation but have no edge are evaluated for a connection.

All changes are tagged with ``source="dream_*"`` for traceability and
logged to a persistent dream journal (``~/.thoth/dream_journal.json``).

Architecture mirrors ``memory_extraction.py``: daemon thread, direct LLM
calls (no agent overhead), conservative thresholds, batch-capped.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────

_DATA_DIR = pathlib.Path(
    os.environ.get("THOTH_DATA_DIR", pathlib.Path.home() / ".thoth")
)
_DATA_DIR.mkdir(parents=True, exist_ok=True)

_CONFIG_FILE = _DATA_DIR / "dream_config.json"
_JOURNAL_FILE = _DATA_DIR / "dream_journal.json"
_REJECTION_CACHE_FILE = _DATA_DIR / "dream_rejections.json"

# Defaults
_DEFAULT_CONFIG = {
    "enabled": True,
    "window_start": 1,      # 1 AM local time
    "window_end": 5,         # 5 AM local time
    "merge_threshold": 0.93,
    "enrich_min_chars": 80,
    "infer_confidence": 0.80,
    "min_entities": 20,
    "batch_size": 50,
}

_JOURNAL_MAX_ENTRIES = 100
_CHECK_INTERVAL_S = 30 * 60  # Check every 30 minutes


def _load_config() -> dict:
    """Load dream cycle config, falling back to defaults."""
    cfg = dict(_DEFAULT_CONFIG)
    if _CONFIG_FILE.exists():
        try:
            stored = json.loads(_CONFIG_FILE.read_text())
            cfg.update(stored)
        except Exception:
            pass
    return cfg


def _save_config(cfg: dict) -> None:
    _CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


def get_config() -> dict:
    """Public accessor for UI."""
    return _load_config()


def set_enabled(enabled: bool) -> None:
    cfg = _load_config()
    cfg["enabled"] = enabled
    _save_config(cfg)


def set_window(start: int, end: int) -> None:
    cfg = _load_config()
    cfg["window_start"] = start
    cfg["window_end"] = end
    _save_config(cfg)


def is_enabled() -> bool:
    return _load_config().get("enabled", True)


# ── Dream journal ────────────────────────────────────────────────────────────

def _load_journal() -> list[dict]:
    if _JOURNAL_FILE.exists():
        try:
            return json.loads(_JOURNAL_FILE.read_text())
        except Exception:
            pass
    return []


def _save_journal(entries: list[dict]) -> None:
    # Keep only the most recent entries
    entries = entries[-_JOURNAL_MAX_ENTRIES:]
    _JOURNAL_FILE.write_text(json.dumps(entries, indent=2))


def _append_journal(entry: dict) -> None:
    journal = _load_journal()
    journal.append(entry)
    _save_journal(journal)


def get_journal(limit: int = 10) -> list[dict]:
    """Return the most recent dream journal entries."""
    entries = _load_journal()
    return entries[-limit:]


def get_dream_status() -> dict:
    """Return dream cycle status for Activity panel."""
    cfg = _load_config()
    journal = _load_journal()
    last = journal[-1] if journal else None
    return {
        "enabled": cfg.get("enabled", True),
        "window": f"{cfg.get('window_start', 1)}:00 – {cfg.get('window_end', 5)}:00",
        "last_run": last.get("timestamp") if last else None,
        "last_summary": last.get("summary") if last else None,
    }


# ── Rejection cache ─────────────────────────────────────────────────────────

def _rejection_cache_ttl_days() -> int:
    """Return rejection cache TTL scaled by graph size.

    Small graphs cycle through pairs faster so stale rejections should
    expire sooner, giving the LLM another chance as context evolves.
    """
    try:
        import knowledge_graph as _kg
        count = _kg.count_entities()
    except Exception:
        count = 0
    if count < 200:
        return 3
    if count < 500:
        return 5
    return 7

def _load_rejection_cache() -> dict[str, str]:
    """Load pair rejection cache: {pair_key: iso_timestamp}."""
    if _REJECTION_CACHE_FILE.exists():
        try:
            return json.loads(_REJECTION_CACHE_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_rejection_cache(cache: dict[str, str]) -> None:
    _REJECTION_CACHE_FILE.write_text(json.dumps(cache, indent=2))


def _record_rejection(entity_a_id: str, entity_b_id: str) -> None:
    """Record that a pair was rejected by the LLM."""
    cache = _load_rejection_cache()
    pair_key = "|".join(sorted([entity_a_id, entity_b_id]))
    cache[pair_key] = datetime.now(timezone.utc).isoformat()
    # Prune expired entries while we're here
    ttl = _rejection_cache_ttl_days()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=ttl)).isoformat()
    cache = {k: v for k, v in cache.items() if v > cutoff}
    _save_rejection_cache(cache)


def _is_pair_recently_rejected(entity_a_id: str, entity_b_id: str) -> bool:
    """Check if a pair was rejected within the cache window."""
    cache = _load_rejection_cache()
    pair_key = "|".join(sorted([entity_a_id, entity_b_id]))
    ts = cache.get(pair_key)
    if not ts:
        return False
    ttl = _rejection_cache_ttl_days()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=ttl)).isoformat()
    return ts > cutoff


# ── Idle detection ───────────────────────────────────────────────────────────

def _is_idle() -> bool:
    """Check that heavyweight dream work will not compete with the user."""
    try:
        from memory_extraction import is_app_idle
        return is_app_idle()
    except Exception:
        return True  # If we can't check, assume idle


def _in_dream_window() -> bool:
    """Check if current local time is within the dream window."""
    cfg = _load_config()
    hour = datetime.now().hour
    start = cfg.get("window_start", 1)
    end = cfg.get("window_end", 5)
    if start <= end:
        return start <= hour < end
    else:
        # Wraps midnight, e.g. 23:00 - 03:00
        return hour >= start or hour < end


def _already_ran_today() -> bool:
    """Check if a dream cycle already completed today."""
    journal = _load_journal()
    if not journal:
        return False
    last = journal[-1]
    try:
        last_dt = datetime.fromisoformat(last["timestamp"])
        return last_dt.date() == datetime.now().date()
    except (KeyError, ValueError):
        return False


def _is_ollama_busy() -> bool:
    """Check if Ollama is currently processing a request.

    Queries the ``/api/ps`` endpoint.  Returns True if any model is
    currently loaded with active requests, meaning a user-facing task
    is in progress and we should not compete for GPU.
    """
    try:
        import urllib.request
        from models import _ollama_base_url
        req = urllib.request.Request(f"{_ollama_base_url()}/api/ps", method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            import json as _json
            data = _json.loads(resp.read())
            models = data.get("models", [])
            # A model is truly busy only when it has active slots in use.
            # A model that is merely loaded/cached (size_vram > 0 but
            # num_requests == 0) should not block the dream cycle.
            return any(
                m.get("num_requests", m.get("size_vram", 0)) > 0
                for m in models
            )
    except Exception:
        # Can't reach Ollama — not busy (or using cloud model)
        return False


def _should_dream() -> bool:
    """All conditions met for a dream cycle?"""
    if not is_enabled():
        return False
    if _already_ran_today():
        return False
    if not _in_dream_window():
        return False
    if not _is_idle():
        return False
    if _is_ollama_busy():
        logger.info("Dream cycle deferred — Ollama is busy processing a request")
        return False
    # Never run while any agent generation is in flight — a long dream
    # cycle competes with live requests for LLM bandwidth and can
    # starve the UI.
    try:
        from ui.state import _active_generations
        if _active_generations:
            logger.info(
                "Dream cycle deferred — %d active generation(s) in flight",
                len(_active_generations),
            )
            return False
    except Exception:
        # ui.state import failures should not prevent dreaming
        pass
    return True


# ── LLM helper ───────────────────────────────────────────────────────────────

def _llm_call(prompt: str) -> str:
    """Make a direct LLM call. Returns raw response text."""
    from models import get_current_model, get_llm_for
    from langchain_core.messages import HumanMessage

    llm = get_llm_for(get_current_model())
    response = llm.invoke([HumanMessage(content=prompt)])
    raw = response.content or ""
    if isinstance(raw, list):
        parts = []
        for block in raw:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        raw = "\n".join(parts)
    if not isinstance(raw, str):
        raw = str(raw) if raw else ""
    # Strip <think>...</think> blocks from reasoning models
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
    raw = re.sub(r"</?think>", "", raw).strip()
    return raw


# ── OP1: Duplicate merge ────────────────────────────────────────────────────

def _find_merge_candidates(batch: list[dict], threshold: float) -> list[tuple[dict, dict, float]]:
    """Find pairs of entities with high semantic similarity and same type.

    Returns list of (entity_a, entity_b, score) tuples, deduplicated so
    each entity appears at most once (highest-scoring pair wins).
    """
    import knowledge_graph as kg

    seen_ids: set[str] = set()
    candidates: list[tuple[dict, dict, float]] = []

    for entity in batch:
        eid = entity["id"]
        if eid in seen_ids:
            continue
        etype = entity.get("entity_type", "")
        search_text = kg._entity_text(entity)

        try:
            hits = kg.semantic_search(search_text, top_k=5, threshold=threshold)
        except Exception:
            continue

        for hit in hits:
            hid = hit["id"]
            if hid == eid or hid in seen_ids:
                continue
            if hit.get("entity_type", "") != etype:
                continue
            # Skip the "User" entity — never merge it
            if entity.get("subject", "").strip().lower() == "user":
                continue
            if hit.get("subject", "").strip().lower() == "user":
                continue

            score = hit.get("score", 0)

            # Subject-name guard: if normalized subjects differ
            # significantly, require much higher similarity to merge.
            subj_a = entity.get("subject", "").strip().lower()
            subj_b = hit.get("subject", "").strip().lower()
            if subj_a != subj_b:
                # Check if one is a substring of the other (e.g. "Bob" / "Bob Smith")
                is_substring = subj_a in subj_b or subj_b in subj_a
                if not is_substring:
                    # Different names — require 0.98+ or skip
                    if score < 0.98:
                        continue

            candidates.append((entity, hit, score))
            seen_ids.add(eid)
            seen_ids.add(hid)
            break  # One merge per entity per cycle

    return candidates


def _merge_entities(entity_a: dict, entity_b: dict) -> dict | None:
    """Merge two entities, keeping the older one. Returns merge log entry."""
    import knowledge_graph as kg
    from prompts import DREAM_MERGE_PROMPT

    # Determine survivor (older by created_at)
    a_created = entity_a.get("created_at", "")
    b_created = entity_b.get("created_at", "")
    if a_created <= b_created:
        survivor, duplicate = entity_a, entity_b
    else:
        survivor, duplicate = entity_b, entity_a

    # LLM: synthesize best description from both
    prompt = DREAM_MERGE_PROMPT.format(
        entity_type=survivor.get("entity_type", ""),
        subject_a=survivor.get("subject", ""),
        description_a=survivor.get("description", ""),
        subject_b=duplicate.get("subject", ""),
        description_b=duplicate.get("description", ""),
    )

    try:
        merged_desc = _llm_call(prompt).strip()
        if not merged_desc or len(merged_desc) < 10:
            return None
    except Exception as exc:
        logger.warning("Dream merge LLM call failed: %s", exc)
        return None

    # Union aliases
    a_aliases = set(a.strip() for a in (survivor.get("aliases", "") or "").split(",") if a.strip())
    b_aliases = set(a.strip() for a in (duplicate.get("aliases", "") or "").split(",") if a.strip())
    # Add the duplicate's subject as an alias if different
    dup_subj = duplicate.get("subject", "").strip()
    surv_subj = survivor.get("subject", "").strip()
    if dup_subj.lower() != surv_subj.lower():
        b_aliases.add(dup_subj)
    merged_aliases = ", ".join(sorted(a_aliases | b_aliases))

    # Re-point all relations from duplicate to survivor
    try:
        dup_rels = kg.get_relations(duplicate["id"], direction="both")
        for rel in dup_rels:
            src = rel["source_id"]
            tgt = rel["target_id"]
            rtype = rel["relation_type"]
            # Determine new endpoints
            new_src = survivor["id"] if src == duplicate["id"] else src
            new_tgt = survivor["id"] if tgt == duplicate["id"] else tgt
            # Skip self-loops
            if new_src == new_tgt:
                continue
            # Add the relation to survivor (ignores duplicates via UNIQUE index)
            kg.add_relation(
                new_src, new_tgt, rtype,
                source="dream_merge",
                confidence=rel.get("confidence", 0.8),
            )
    except Exception as exc:
        logger.debug("Re-pointing relations failed: %s", exc)

    # Update survivor with merged description + aliases
    try:
        kg.update_entity(
            survivor["id"],
            merged_desc,
            aliases=merged_aliases if merged_aliases else None,
        )
    except Exception as exc:
        logger.warning("Dream merge update failed: %s", exc)
        return None

    # Delete the duplicate
    try:
        deleted = kg.delete_entity(duplicate["id"])
        if not deleted:
            logger.warning(
                "Dream merge: delete_entity(%s) returned False — zombie entity",
                duplicate["id"],
            )
            return None
    except Exception as exc:
        logger.debug("Dream merge delete failed: %s", exc)
        return None

    return {
        "survivor_id": survivor["id"],
        "survivor_subject": surv_subj,
        "duplicate_id": duplicate["id"],
        "duplicate_subject": dup_subj,
        "merged_description": merged_desc[:200],
        "aliases": merged_aliases,
    }


# ── OP2: Description enrichment ─────────────────────────────────────────────

def _find_thin_entities(batch: list[dict], min_chars: int) -> list[dict]:
    """Find entities with descriptions shorter than min_chars."""
    return [e for e in batch if len(e.get("description", "") or "") < min_chars]


def _find_conversation_mentions(subject: str, aliases: str = "") -> list[str]:
    """Search conversations for sentence-level mentions of an entity.

    Instead of extracting raw character windows (which mix facts about
    different entities), this splits conversation text into sentences and
    only returns sentences that actually mention the target entity's name
    or aliases.  Returns up to 3 excerpts (one per conversation).
    """
    from threads import _list_threads
    from memory_extraction import _get_thread_messages, _format_conversation

    names = {subject.lower()}
    for alias in (aliases or "").split(","):
        alias = alias.strip()
        if alias:
            names.add(alias.lower())

    threads = _list_threads()
    if not threads:
        return []

    excerpts = []
    for tid, name, created, updated, *rest in threads:
        if len(excerpts) >= 3:
            break
        try:
            messages = _get_thread_messages(tid)
            if not messages:
                continue
            conv_text = _format_conversation(messages)
            conv_lower = conv_text.lower()
            if not any(n in conv_lower for n in names):
                continue

            # Split into sentences and keep only those mentioning the entity
            relevant = _extract_relevant_sentences(conv_text, names)
            if relevant:
                excerpts.append(relevant)
        except Exception:
            continue

    return excerpts


def _extract_relevant_sentences(text: str, names: set[str], max_chars: int = 500) -> str:
    """Extract sentences from *text* that mention any name in *names*.

    Splits on sentence boundaries (`.!?` followed by whitespace or newline)
    and on conversation turn boundaries (`User:` / `Assistant:`).  Returns
    only the sentences that contain at least one of the target names,
    concatenated and capped at *max_chars*.
    """
    # Split on sentence-ending punctuation or conversation turn markers
    parts = re.split(r'(?<=[.!?])\s+|(?=\b(?:User|Assistant):)', text)
    kept: list[str] = []
    total = 0
    for part in parts:
        part = part.strip()
        if not part:
            continue
        part_lower = part.lower()
        if any(n in part_lower for n in names):
            kept.append(part)
            total += len(part)
            if total >= max_chars:
                break
    return " ".join(kept)


def _collect_other_subjects(entity_id: str | None, all_entities: list[dict] | None = None) -> set[str]:
    """Build a set of lowercased subject names for all entities EXCEPT *entity_id*.

    Used by the post-enrichment validator to detect cross-entity fact-bleed.
    """
    if all_entities is None:
        import knowledge_graph as kg
        all_entities = kg.list_entities(limit=100_000)

    others: set[str] = set()
    for e in all_entities:
        if entity_id and e["id"] == entity_id:
            continue
        subj = (e.get("subject", "") or "").strip().lower()
        if subj and len(subj) >= 3 and subj != "user":
            others.add(subj)
        for alias in (e.get("aliases", "") or "").split(","):
            alias = alias.strip().lower()
            if alias and len(alias) >= 3 and alias != "user":
                others.add(alias)
    return others


def _prepare_enrichment_inputs(
    all_entities: list[dict],
    merges: list[dict],
    min_chars: int,
) -> tuple[list[dict], set[str]]:
    """Prepare Phase 2 enrichment candidates and validation guardrails."""
    merged_ids = {
        item.get("duplicate_id")
        for item in merges
        if item.get("duplicate_id")
    }
    surviving_entities = [
        entity for entity in all_entities
        if entity["id"] not in merged_ids
    ]
    thin_entities = _find_thin_entities(surviving_entities, min_chars)
    thin_entities.sort(key=lambda entity: entity.get("updated_at", ""))
    other_subjects = _collect_other_subjects(None, surviving_entities)
    return thin_entities[:20], other_subjects


def _validate_enrichment(
    enriched: str,
    entity: dict,
    other_subjects: set[str],
) -> bool:
    """Return True if *enriched* passes cross-entity contamination check.

    Scans the enriched description for mentions of other known entity
    subjects.  Subjects that already appear in the entity's known
    relationships are allowed (e.g. "Diana works with Bob" is fine if
    Diana→Bob is an existing relation).  Everything else is contamination.
    """
    import knowledge_graph as kg

    enriched_lower = enriched.lower()
    entity_subject = (entity.get("subject", "") or "").strip().lower()

    # Collect peer subjects from existing relationships — these are allowed
    allowed: set[str] = set()
    try:
        rels = kg.get_relations(entity["id"], direction="both")
        for r in rels:
            peer = (r.get("peer_subject", "") or "").strip().lower()
            if peer:
                allowed.add(peer)
    except Exception:
        pass

    for subj in other_subjects:
        if subj == entity_subject:
            continue
        if subj in allowed:
            continue
        # Use word-boundary check to avoid false positives on substrings
        if re.search(r'\b' + re.escape(subj) + r'\b', enriched_lower):
            logger.info(
                "Dream enrich REJECTED for '%s': mentions unrelated entity '%s'",
                entity.get("subject", ""), subj,
            )
            return False
    return True


def _enrich_entity(
    entity: dict,
    excerpts: list[str],
    other_subjects: set[str] | None = None,
) -> dict | None:
    """Enrich an entity's description using conversation context."""
    import knowledge_graph as kg
    from prompts import DREAM_ENRICH_PROMPT

    # Build relationship context so the LLM knows entity boundaries
    rel_lines = []
    try:
        rels = kg.get_relations(entity["id"])
        for r in rels[:10]:
            arrow = "→" if r["direction"] == "outgoing" else "←"
            rel_lines.append(f"  {arrow} {r['relation_type']} → {r['peer_subject']}")
    except Exception:
        pass
    relationships_text = "\n".join(rel_lines) if rel_lines else "(none known)"

    context = "\n---\n".join(excerpts[:3])
    prompt = DREAM_ENRICH_PROMPT.format(
        entity_type=entity.get("entity_type", ""),
        subject=entity.get("subject", ""),
        current_description=entity.get("description", ""),
        relationships=relationships_text,
        conversation_excerpts=context,
    )

    try:
        enriched = _llm_call(prompt).strip()
        if not enriched or len(enriched) < 10:
            return None
    except Exception as exc:
        logger.warning("Dream enrich LLM call failed: %s", exc)
        return None

    # Safety: new description must be at least as long as the old one
    old_desc = entity.get("description", "") or ""
    if len(enriched) < len(old_desc):
        return None

    # Identity check: reject if the LLM returned the exact same text
    if enriched.strip() == old_desc.strip():
        logger.info(
            "Dream enrich skipped identity (no change): '%s' (%d chars)",
            entity.get("subject", ""), len(old_desc),
        )
        return None

    # Layer 2: Cross-entity contamination check (deterministic)
    if other_subjects is not None:
        if not _validate_enrichment(enriched, entity, other_subjects):
            return None

    # Layer 3: Fact-grounding verification — reject sentences that
    # introduce facts not mentioned in any excerpt or the old description.
    # Split new content into sentences and check each one has evidence.
    import re as _re_enrich
    old_lower = old_desc.lower()
    excerpts_lower = " ".join(excerpts).lower()
    new_sentences = _re_enrich.split(r"(?<=[.!?])\s+", enriched)
    ungrounded = []
    for sentence in new_sentences:
        s = sentence.strip().lower()
        if not s or len(s) < 15:
            continue  # Skip short fragments
        # Check if key words from this sentence appear in sources
        # Extract meaningful words (>3 chars, not stopwords)
        words = [w for w in _re_enrich.findall(r"\b\w{4,}\b", s)
                 if w not in {"this", "that", "with", "from", "have", "been",
                              "also", "they", "their", "about", "which", "when",
                              "where", "what", "into", "more", "some", "other",
                              "than", "very", "each", "most"}]
        if not words:
            continue
        # At least 40% of meaningful words must appear in sources
        in_sources = sum(1 for w in words if w in old_lower or w in excerpts_lower)
        ratio = in_sources / len(words) if words else 0
        if ratio < 0.4:
            ungrounded.append(sentence.strip()[:80])

    if ungrounded:
        logger.warning(
            "Dream enrich rejected for '%s': %d ungrounded sentence(s): %s",
            entity.get("subject", ""), len(ungrounded),
            "; ".join(ungrounded[:3]),
        )
        return None

    try:
        kg.update_entity(entity["id"], enriched)
    except Exception as exc:
        logger.warning("Dream enrich update failed: %s", exc)
        return None

    return {
        "entity_id": entity["id"],
        "subject": entity.get("subject", ""),
        "old_length": len(old_desc),
        "new_length": len(enriched),
        "old_description": old_desc[:200],
        "new_description": enriched[:200],
    }


# ── OP3: Relationship inference ──────────────────────────────────────────────

def _find_cooccurring_pairs(batch: list[dict]) -> list[tuple[dict, dict, str, int]]:
    """Find entity pairs that co-occur in conversations but have no edge.

    Returns list of (entity_a, entity_b, conversation_excerpt, co_occurrence_count)
    tuples.  Limited to 1 pair per entity to keep batch size manageable.
    Requires word-boundary matching and counts co-occurrences across
    conversations to produce a quality signal.
    """
    import re as _re
    import knowledge_graph as kg
    from threads import _list_threads
    from memory_extraction import _get_thread_messages, _format_conversation

    threads = _list_threads()
    if not threads:
        return []

    # Build entity lookup by subject/aliases (lowercased) with word-boundary
    # regex patterns to avoid substring false positives (e.g. "Bob" matching
    # "Bobsled").
    entity_names: dict[str, dict] = {}
    entity_patterns: dict[str, _re.Pattern] = {}
    for e in batch:
        subj = (e.get("subject", "") or "").strip().lower()
        if subj and subj != "user":
            entity_names[subj] = e
            entity_patterns[subj] = _re.compile(r"\b" + _re.escape(subj) + r"\b", _re.IGNORECASE)
        for alias in (e.get("aliases", "") or "").split(","):
            alias = alias.strip().lower()
            if alias and alias != "user":
                entity_names[alias] = e
                entity_patterns[alias] = _re.compile(r"\b" + _re.escape(alias) + r"\b", _re.IGNORECASE)

    # First pass: count co-occurrences across ALL conversations
    pair_conversations: dict[tuple[str, str], list[str]] = {}  # pair_key → [excerpts]

    for tid, name, created, updated, *rest in threads:
        try:
            messages = _get_thread_messages(tid)
            if not messages:
                continue
            conv_text = _format_conversation(messages)
        except Exception:
            continue

        # Find which entities from our batch appear (with word boundaries)
        found_in_conv: list[dict] = []
        seen_entity_ids: set[str] = set()
        for name_str, pattern in entity_patterns.items():
            entity = entity_names[name_str]
            if entity["id"] not in seen_entity_ids and pattern.search(conv_text):
                found_in_conv.append(entity)
                seen_entity_ids.add(entity["id"])

        # Record all pairs in this conversation
        for i, ea in enumerate(found_in_conv):
            for eb in found_in_conv[i + 1:]:
                if ea["id"] == eb["id"]:
                    continue
                pair_key = tuple(sorted([ea["id"], eb["id"]]))

                # Extract excerpt around first entity mention
                excerpt = conv_text[:500]
                for name_str, pattern in entity_patterns.items():
                    if entity_names[name_str]["id"] == ea["id"]:
                        m = pattern.search(conv_text)
                        if m:
                            idx = m.start()
                            start = max(0, idx - 200)
                            end = min(len(conv_text), idx + 500)
                            excerpt = conv_text[start:end]
                            break

                if pair_key not in pair_conversations:
                    pair_conversations[pair_key] = []
                pair_conversations[pair_key].append(excerpt)

    # Second pass: filter, rank, and select pairs
    from collections import Counter as _Counter

    _VAGUE_EDGE_TYPES = {"related_to", "associated_with"}
    _HUB_CAP = 3  # max appearances per entity across selected pairs

    pairs: list[tuple[dict, dict, str, int]] = []
    seen_pairs: set[tuple[str, str]] = set()
    entity_use_count: _Counter = _Counter()

    # Build entity lookup by ID for quick access
    entity_by_id: dict[str, dict] = {e["id"]: e for e in batch}

    # Sort by co-occurrence count descending (most evidence first)
    ranked_pairs = sorted(pair_conversations.items(), key=lambda x: len(x[1]), reverse=True)

    for pair_key, excerpts in ranked_pairs:
        if len(pairs) >= 25:
            break

        ea_id, eb_id = pair_key

        # Hub diversity cap: skip if either entity already used 3 times
        if entity_use_count[ea_id] >= _HUB_CAP or entity_use_count[eb_id] >= _HUB_CAP:
            continue

        ea = entity_by_id.get(ea_id)
        eb = entity_by_id.get(eb_id)
        if not ea or not eb:
            continue

        # Skip pairs recently rejected by LLM
        if _is_pair_recently_rejected(ea_id, eb_id):
            continue

        # Pre-flight merge check: if A's description mentions B's subject
        # (or vice versa) AND they share the same entity_type, these are
        # likely duplicates — skip inference.  Cross-type mentions are
        # normal (e.g. a project description mentioning a skill).
        _desc_a = (ea.get("description", "") or "").lower()
        _desc_b = (eb.get("description", "") or "").lower()
        _subj_a = (ea.get("subject", "") or "").strip().lower()
        _subj_b = (eb.get("subject", "") or "").strip().lower()
        _same_type = ea.get("entity_type") == eb.get("entity_type")
        if _same_type and _subj_b and len(_subj_b) > 2 and _re.search(r"\b" + _re.escape(_subj_b) + r"\b", _desc_a):
            logger.info(
                "Dream skip infer (probable duplicate): '%s' description mentions '%s'",
                ea.get("subject", ""), eb.get("subject", ""),
            )
            continue
        if _same_type and _subj_a and len(_subj_a) > 2 and _re.search(r"\b" + _re.escape(_subj_a) + r"\b", _desc_b):
            logger.info(
                "Dream skip infer (probable duplicate): '%s' description mentions '%s'",
                eb.get("subject", ""), ea.get("subject", ""),
            )
            continue

        # Check if they already have a meaningful (non-vague) relation
        try:
            existing = kg.get_relations(ea["id"], direction="both")
            has_meaningful_edge = any(
                r.get("peer_id") == eb["id"]
                and r.get("relation_type", "related_to") not in _VAGUE_EDGE_TYPES
                for r in existing
            )
            if has_meaningful_edge:
                continue
        except Exception:
            continue

        # Multi-excerpt evidence: join up to 3 best excerpts for rich context
        co_count = len(excerpts)
        sorted_excerpts = sorted(excerpts, key=len, reverse=True)
        if co_count >= 3:
            evidence = "\n\n---\n\n".join(sorted_excerpts[:3])
        else:
            evidence = sorted_excerpts[0]

        pairs.append((ea, eb, evidence, co_count))
        seen_pairs.add(pair_key)
        entity_use_count[ea_id] += 1
        entity_use_count[eb_id] += 1

    return pairs


def _infer_relation(entity_a: dict, entity_b: dict, excerpt: str,
                    confidence: float = 0.7, co_occurrence_count: int = 1) -> dict | None:
    """Ask the LLM if two entities are related, given conversation evidence.

    The LLM returns confidence, evidence quote, and directionality.
    Vague relation types are rejected.  Confidence below 0.75 is rejected.
    """
    import knowledge_graph as kg
    from prompts import DREAM_INFER_PROMPT

    # Banned vague relation types that add noise to the graph
    _BANNED_TYPES = {
        "related_to", "associated_with", "connected_to", "linked_to",
        "has_relation", "involves", "correlates_with",
    }

    prompt = DREAM_INFER_PROMPT.format(
        type_a=entity_a.get("entity_type", ""),
        subject_a=entity_a.get("subject", ""),
        description_a=entity_a.get("description", ""),
        type_b=entity_b.get("entity_type", ""),
        subject_b=entity_b.get("subject", ""),
        description_b=entity_b.get("description", ""),
        conversation_excerpt=excerpt[:1000],
        co_occurrence_count=co_occurrence_count,
    )

    try:
        raw = _llm_call(prompt)
    except Exception as exc:
        logger.warning("Dream infer LLM call failed: %s", exc)
        return None

    # Parse JSON response
    try:
        import knowledge_graph as _kg_parse
        _json_str = _kg_parse.extract_json_block(raw, "{")
        if not _json_str:
            return None
        result = json.loads(_json_str)
        if not result.get("has_relation"):
            return None
        rel_type = result.get("relation_type", "").strip().lower().replace(" ", "_")
        if not rel_type:
            return None
    except (json.JSONDecodeError, AttributeError):
        return None

    # ── Post-filter: reject vague relation types ─────────────────────
    if rel_type in _BANNED_TYPES:
        logger.info(
            "Dream infer rejected vague type '%s' for %s → %s",
            rel_type, entity_a.get("subject", ""), entity_b.get("subject", ""),
        )
        return None

    # ── Tautology guard: reject if one subject is a substring of the other ──
    _subj_a_low = (entity_a.get("subject", "") or "").strip().lower()
    _subj_b_low = (entity_b.get("subject", "") or "").strip().lower()
    if _subj_a_low and _subj_b_low and len(_subj_a_low) > 2 and len(_subj_b_low) > 2:
        if _subj_a_low in _subj_b_low or _subj_b_low in _subj_a_low:
            logger.info(
                "Dream infer rejected tautology: '%s' ↔ '%s' (substring match)",
                entity_a.get("subject", ""), entity_b.get("subject", ""),
            )
            return None

    # ── Dynamic confidence from LLM ──────────────────────────────────
    llm_confidence = result.get("confidence")
    if isinstance(llm_confidence, (int, float)):
        final_confidence = max(0.0, min(1.0, float(llm_confidence)))
    else:
        final_confidence = confidence  # Fallback to config default

    # Reject low-confidence inferences
    if final_confidence < 0.80:
        logger.info(
            "Dream infer rejected low confidence (%.2f) for %s → %s",
            final_confidence, entity_a.get("subject", ""), entity_b.get("subject", ""),
        )
        return None

    # ── Directionality from LLM ──────────────────────────────────────
    llm_source = (result.get("source", "") or "").strip().lower()
    llm_target = (result.get("target", "") or "").strip().lower()
    subj_a = (entity_a.get("subject", "") or "").strip().lower()
    subj_b = (entity_b.get("subject", "") or "").strip().lower()

    # If LLM says source=B and target=A, swap the direction
    if llm_source == subj_b and llm_target == subj_a:
        source_entity, target_entity = entity_b, entity_a
    else:
        # Default: entity_a → entity_b (or LLM confirmed A → B)
        source_entity, target_entity = entity_a, entity_b

    # ── Evidence tracking ────────────────────────────────────────────
    evidence = (result.get("evidence", "") or "").strip()
    rel_properties = {}
    if evidence:
        rel_properties["evidence"] = evidence[:500]
    rel_properties["co_occurrences"] = co_occurrence_count

    # Add the relation
    try:
        rel = kg.add_relation(
            source_entity["id"], target_entity["id"], rel_type,
            source="dream_infer",
            confidence=final_confidence,
            properties=rel_properties,
        )
        if not rel:
            return None
    except Exception as exc:
        logger.debug("Dream infer add_relation failed: %s", exc)
        return None

    return {
        "source_id": source_entity["id"],
        "source_subject": source_entity.get("subject", ""),
        "target_id": target_entity["id"],
        "target_subject": target_entity.get("subject", ""),
        "relation_type": rel_type,
        "confidence": final_confidence,
        "evidence": evidence,
        "co_occurrences": co_occurrence_count,
    }


# ── OP5: Insights analysis ──────────────────────────────────────────────────

def _collect_system_snapshot() -> str:
    """Gather a text snapshot of recent system activity for insights analysis."""
    sections: list[str] = []

    # 1. Recent logs (warnings and errors only)
    try:
        from logging_config import read_recent_logs
        logs = read_recent_logs(100)
        error_logs = [
            entry for entry in logs
            if entry.get("level", "").upper() in ("WARNING", "ERROR", "CRITICAL")
        ]
        if error_logs:
            lines = []
            for entry in error_logs[:30]:
                ts = entry.get("ts", "?")
                lvl = entry.get("level", "?")
                msg = entry.get("msg", "")[:200]
                tool = entry.get("tool", "")
                tool_label = f" [{tool}]" if tool else ""
                lines.append(f"  [{ts}]{tool_label} {lvl}: {msg}")
            sections.append("RECENT WARNINGS/ERRORS (last 7 days):\n" + "\n".join(lines))
            tool_counts: dict[str, int] = {}
            for entry in error_logs:
                tool = (entry.get("tool", "") or "").strip()
                if not tool:
                    continue
                tool_counts[tool] = tool_counts.get(tool, 0) + 1
            if tool_counts:
                count_lines = [
                    f"  {tool}: {count}"
                    for tool, count in sorted(tool_counts.items(), key=lambda item: (-item[1], item[0]))
                ]
                sections.append("TOOL ERROR COUNTS:\n" + "\n".join(count_lines))
            else:
                sections.append("TOOL ERROR COUNTS: None")
        else:
            sections.append("RECENT WARNINGS/ERRORS: None")
            sections.append("TOOL ERROR COUNTS: None")
    except Exception:
        sections.append("RECENT WARNINGS/ERRORS: (unavailable)")
        sections.append("TOOL ERROR COUNTS: (unavailable)")

    # 2. Knowledge graph stats
    try:
        import knowledge_graph as kg
        entity_count = kg.count_entities()
        relation_count = kg.count_relations()
        sections.append(
            f"KNOWLEDGE GRAPH: {entity_count} entities, {relation_count} relations"
        )
    except Exception:
        sections.append("KNOWLEDGE GRAPH: (unavailable)")

    # 3. Document library
    try:
        from documents import load_processed_files
        processed_files = load_processed_files()
        sections.append(f"DOCUMENT LIBRARY: {len(processed_files)} indexed files")
    except Exception:
        sections.append("DOCUMENT LIBRARY: (unavailable)")

    # 4. Task history
    try:
        from tasks import list_tasks
        tasks = list_tasks()
        if tasks:
            lines = []
            for t in tasks[:15]:
                name = t.get("name", "?")
                enabled = t.get("enabled", False)
                last_run = t.get("last_run") or "never"
                last_status = t.get("last_status")
                if not last_status:
                    last_status = "never_run" if last_run == "never" else "no_history"
                lines.append(f"  {name}: enabled={enabled}, last_run={last_run}, status={last_status}")
            sections.append("TASKS:\n" + "\n".join(lines))
        else:
            sections.append("TASKS: None configured")
    except Exception:
        sections.append("TASKS: (unavailable)")

    # 5. Active model and provider runtime
    try:
        from models import get_current_model, get_context_size, get_cloud_provider, is_model_local
        from providers.selection import provider_display_label

        model = get_current_model()
        local = is_model_local(model)
        provider_id = "local" if local else (get_cloud_provider(model) or "provider")
        provider_label = provider_display_label(provider_id)
        sections.append(
            f"MODEL: {model}; provider={provider_label}; type={'local' if local else 'provider'}; "
            f"context={get_context_size(model)}"
        )
    except Exception:
        sections.append("MODEL: (unavailable)")

    # 6. Provider connections and Quick Choices
    try:
        from providers.status import provider_status_cards
        from providers.selection import list_quick_choices

        cards = provider_status_cards()
        configured = [card for card in cards if card.get("configured")]
        lines = [
            f"  {card.get('display_name')}: source={card.get('source') or 'unknown'}, "
            f"runtime_enabled={card.get('runtime_enabled')}, models={card.get('model_count') or 0}"
            for card in configured[:12]
        ]
        quick_count = len([choice for choice in list_quick_choices("status_tool") if choice.get("kind") == "model"])
        if lines:
            sections.append(
                f"PROVIDERS: {len(configured)} configured, {quick_count} status-tool Quick Choice(s)\n"
                + "\n".join(lines)
            )
        else:
            sections.append(f"PROVIDERS: none configured, {quick_count} status-tool Quick Choice(s)")
    except Exception:
        sections.append("PROVIDERS: (unavailable)")

    # 7. Media model defaults
    try:
        from tools import registry as tool_registry
        from tools.image_gen_tool import _get_configured_selection as _image_selection
        from tools.video_gen_tool import _get_configured_selection as _video_selection

        sections.append(
            "MEDIA MODELS: "
            f"image={_image_selection()} (tool={'enabled' if tool_registry.is_enabled('image_gen') else 'disabled'}), "
            f"video={_video_selection()} (tool={'enabled' if tool_registry.is_enabled('video_gen') else 'disabled'})"
        )
    except Exception:
        sections.append("MEDIA MODELS: (unavailable)")

    # 8. Skills
    try:
        from skills import get_manual_skill_statuses
        skill_statuses = get_manual_skill_statuses()
        enabled = [skill for skill, is_enabled in skill_statuses if is_enabled]
        skill_names = [skill.display_name or skill.name for skill, _ in skill_statuses]
        sections.append(
            f"SKILLS: {len(enabled)} enabled / {len(skill_statuses)} total — "
            + ", ".join(skill_names[:20])
        )
    except Exception:
        sections.append("SKILLS: (unavailable)")

    # 9. Channels
    try:
        from channels.registry import configured_channels, running_channels
        configured = configured_channels()
        running = running_channels()
        if configured:
            lines = [f"  {ch.label}: running={ch in running}" for ch in configured]
            sections.append("CHANNELS:\n" + "\n".join(lines))
        else:
            sections.append("CHANNELS: None configured")
    except Exception:
        sections.append("CHANNELS: (unavailable)")

    # 10. Last dream cycle
    try:
        journal = _load_journal()
        if journal:
            last = journal[-1]
            sections.append(
                f"LAST DREAM CYCLE: {last.get('timestamp', '?')} — {last.get('summary', '?')}"
            )
        else:
            sections.append("LAST DREAM CYCLE: Never run")
    except Exception:
        sections.append("LAST DREAM CYCLE: (unavailable)")

    # 11. Existing active insights (so LLM avoids duplicates)
    try:
        from insights import get_active_insights
        active = get_active_insights()
        if active:
            lines = [f"  [{i['category']}] {i['title']}" for i in active[:10]]
            sections.append("EXISTING ACTIVE INSIGHTS:\n" + "\n".join(lines))
    except Exception:
        pass

    return "\n\n".join(sections)


def _run_insights_phase(cycle_id: str, on_status=None) -> dict:
    """Phase 5: Analyze system state and generate insights.

    Returns a dict with 'insights_added' and 'insights_merged' counts.
    """
    from prompts import DREAM_INSIGHTS_PROMPT
    import insights

    result = {"insights_added": 0, "insights_merged": 0, "errors": []}

    def _status(msg: str):
        logger.info("Dream [%s]: %s", cycle_id, msg)
        if on_status:
            on_status(msg)

    _status("Phase 5: Analyzing system for insights…")

    # Collect data
    try:
        snapshot = _collect_system_snapshot()
    except Exception as exc:
        result["errors"].append(f"Snapshot collection failed: {exc}")
        return result

    # Get assistant name for prompt
    try:
        from identity import get_assistant_name
        name = get_assistant_name()
    except Exception:
        name = "Thoth"

    prompt = DREAM_INSIGHTS_PROMPT.format(
        assistant_name=name,
        snapshot=snapshot,
    )

    # LLM call
    try:
        raw = _llm_call(prompt)
    except Exception as exc:
        result["errors"].append(f"Insights LLM call failed: {exc}")
        return result

    # Parse JSON array
    try:
        # Try to extract JSON array from response
        import knowledge_graph as _kg_parse
        json_str = _kg_parse.extract_json_block(raw, "[")
        if not json_str:
            # Fallback: try the whole response
            json_str = raw.strip()
        items = json.loads(json_str)
        if not isinstance(items, list):
            items = []
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning("Dream insights: failed to parse LLM response: %s", exc)
        result["errors"].append(f"JSON parse error: {exc}")
        return result

    # Store each insight
    for item in items[:5]:  # Cap at 5 per cycle
        try:
            added = insights.add_insight(
                category=item.get("category", "system_health"),
                severity=item.get("severity", "info"),
                title=item.get("title", "Untitled insight")[:100],
                body=item.get("body", ""),
                evidence=item.get("evidence", []),
                suggestion=item.get("suggestion", ""),
                auto_fixable=item.get("auto_fixable", False),
                confidence=float(item.get("confidence", 0.5)),
                source_cycle=cycle_id,
                skill_draft=item.get("skill_draft"),
            )
            if added:
                if added.get("_merged"):
                    result["insights_merged"] += 1
                else:
                    result["insights_added"] += 1
                _status(f"Insight: [{added['category']}] {added['title']}")
        except Exception as exc:
            result["errors"].append(f"Insight store error: {exc}")

    # Maintenance
    try:
        pruned = insights.auto_prune()
        if pruned:
            _status(f"Auto-pruned {pruned} stale insight(s)")
    except Exception:
        pass

    insights.set_last_analysis()
    _status(f"Insights phase: {result['insights_added']} new, {result['insights_merged']} merged")
    return result


# ── Main dream cycle ────────────────────────────────────────────────────────

def run_dream_cycle(on_status=None) -> dict:
    """Execute one dream cycle. Returns a summary dict.

    Parameters
    ----------
    on_status : callable, optional
        Called with status strings for UI/logging feedback.
    """
    import knowledge_graph as kg

    cycle_id = uuid.uuid4().hex[:8]
    start_time = datetime.now(timezone.utc)
    cfg = _load_config()

    summary = {
        "cycle_id": cycle_id,
        "timestamp": start_time.isoformat(),
        "merges": [],
        "enrichments": [],
        "inferred_relations": [],
        "errors": [],
        "summary": "",
        "duration_s": 0,
    }

    def _status(msg: str):
        logger.info("Dream [%s]: %s", cycle_id, msg)
        if on_status:
            on_status(msg)

    # Check minimum entity count
    entity_count = kg.count_entities()
    if entity_count < cfg.get("min_entities", 20):
        _status(f"Skipped — only {entity_count} entities (min: {cfg.get('min_entities', 20)})")
        summary["summary"] = f"Skipped — {entity_count} entities below minimum"
        summary["duration_s"] = (datetime.now(timezone.utc) - start_time).total_seconds()
        _append_journal(summary)
        return summary

    # Select batch
    batch_size = cfg.get("batch_size", 50)
    all_entities = kg.list_entities(limit=100_000)
    # Rotate through entities using a stored offset so each cycle
    # processes a different slice instead of always the same oldest 50
    all_entities.sort(key=lambda e: e.get("updated_at", ""))
    batch_offset = cfg.get("_batch_offset", 0)
    if batch_offset >= len(all_entities):
        batch_offset = 0
    end = batch_offset + batch_size
    if end <= len(all_entities):
        batch = all_entities[batch_offset:end]
    else:
        # Wrap around
        batch = all_entities[batch_offset:] + all_entities[:end - len(all_entities)]
    # Advance offset for next cycle (half-overlap for continuity)
    next_offset = (batch_offset + batch_size // 2) % max(len(all_entities), 1)
    cfg["_batch_offset"] = next_offset
    _save_config(cfg)

    _status(f"Starting dream cycle — {len(batch)} entities (of {entity_count})")

    # Suppress per-entity FAISS rebuilds during batch operations
    kg._skip_reindex = True

    try:
        # ── OP1: Duplicate merge ─────────────────────────────────────
        _status("Phase 1: Scanning for duplicates…")
        merge_threshold = cfg.get("merge_threshold", 0.93)
        # Need FAISS index for similarity search — rebuild if needed
        kg._skip_reindex = False
        candidates = _find_merge_candidates(batch, merge_threshold)
        kg._skip_reindex = True

        for entity_a, entity_b, score in candidates:
            try:
                result = _merge_entities(entity_a, entity_b)
                if result:
                    result["score"] = round(score, 4)
                    summary["merges"].append(result)
                    _status(
                        f"Merged: '{result['duplicate_subject']}' → "
                        f"'{result['survivor_subject']}' ({score:.2f})"
                    )
            except Exception as exc:
                summary["errors"].append(f"Merge error: {exc}")

        # ── OP2: Description enrichment ──────────────────────────────
        _status("Phase 2: Enriching thin descriptions…")
        min_chars = cfg.get("enrich_min_chars", 80)
        enrichment_candidates, other_subjects = _prepare_enrichment_inputs(
            all_entities,
            summary["merges"],
            min_chars,
        )

        for entity in enrichment_candidates:

            excerpts = _find_conversation_mentions(
                entity.get("subject", ""),
                entity.get("aliases", ""),
            )
            if len(excerpts) < 2:
                continue  # Need 2+ conversations as evidence

            try:
                result = _enrich_entity(entity, excerpts, other_subjects)
                if result:
                    summary["enrichments"].append(result)
                    _status(
                        f"Enriched: '{result['subject']}' "
                        f"({result['old_length']} → {result['new_length']} chars)"
                    )
            except Exception as exc:
                summary["errors"].append(f"Enrich error: {exc}")

        # ── OP3: Confidence decay on stale inferences ────────────────
        _status("Phase 3: Decaying stale inferences…")
        _DECAY_AFTER_DAYS = 90
        _DECAY_FACTOR = 0.9       # reduce by 10% each cycle
        _DECAY_DELETE_BELOW = 0.3  # remove if confidence drops below this
        summary["decayed"] = []
        summary["pruned"] = []
        _decay_conn = None
        try:
            _decay_conn = kg._get_conn()
            cutoff_date = (datetime.now(timezone.utc) - timedelta(days=_DECAY_AFTER_DAYS)).isoformat()
            stale_rows = _decay_conn.execute(
                "SELECT id, source_id, target_id, relation_type, confidence, updated_at "
                "FROM relations WHERE source = 'dream_infer' AND updated_at < ?",
                (cutoff_date,),
            ).fetchall()
            for row in stale_rows:
                old_conf = row["confidence"]
                new_conf = round(old_conf * _DECAY_FACTOR, 4)
                if new_conf < _DECAY_DELETE_BELOW:
                    # Prune: too low to be useful
                    kg.delete_relation(row["id"])
                    summary["pruned"].append({
                        "relation_id": row["id"],
                        "relation_type": row["relation_type"],
                        "old_confidence": old_conf,
                    })
                    _status(f"Pruned stale inference: {row['relation_type']} (conf {old_conf:.2f})")
                else:
                    _decay_conn.execute(
                        "UPDATE relations SET confidence = ?, updated_at = ? WHERE id = ?",
                        (new_conf, datetime.now(timezone.utc).isoformat(), row["id"]),
                    )
                    summary["decayed"].append({
                        "relation_id": row["id"],
                        "relation_type": row["relation_type"],
                        "old_confidence": old_conf,
                        "new_confidence": new_conf,
                    })
            _decay_conn.commit()
            if summary["decayed"] or summary["pruned"]:
                _status(
                    f"Decayed {len(summary['decayed'])} stale inference(s), "
                    f"pruned {len(summary['pruned'])}"
                )
        except Exception as exc:
            summary["errors"].append(f"Decay error: {exc}")
        finally:
            if _decay_conn:
                _decay_conn.close()

        # ── OP4: Relationship inference ──────────────────────────────
        _status("Phase 4: Inferring relationships…")
        infer_confidence = cfg.get("infer_confidence", 0.7)
        pairs = _find_cooccurring_pairs(batch)

        for entity_a, entity_b, excerpt, co_count in pairs[:15]:  # Cap inferences per cycle
            try:
                result = _infer_relation(
                    entity_a, entity_b, excerpt,
                    confidence=infer_confidence,
                    co_occurrence_count=co_count,
                )
                if result:
                    summary["inferred_relations"].append(result)
                    _status(
                        f"Inferred: '{result['source_subject']}' "
                        f"--[{result['relation_type']}]--> "
                        f"'{result['target_subject']}' "
                        f"(conf={result['confidence']:.2f}, "
                        f"co_occ={result.get('co_occurrences', '?')})"
                    )
                else:
                    # Cache rejection so we don't re-evaluate this pair
                    _record_rejection(entity_a["id"], entity_b["id"])
            except Exception as exc:
                summary["errors"].append(f"Infer error: {exc}")

        # ── OP5: Insights analysis ───────────────────────────────────
        try:
            insights_result = _run_insights_phase(cycle_id, on_status=on_status)
            summary["insights"] = insights_result
            if insights_result.get("errors"):
                summary["errors"].extend(insights_result["errors"])
        except Exception as exc:
            summary["errors"].append(f"Insights error: {exc}")

    finally:
        # Restore normal indexing and rebuild once
        kg._skip_reindex = False
        try:
            kg.rebuild_index()
        except Exception as exc:
            summary["errors"].append(f"FAISS rebuild error: {exc}")
            logger.warning("Post-dream FAISS rebuild failed: %s", exc)

        # Rebuild wiki vault if enabled
        try:
            import wiki_vault
            if wiki_vault.is_enabled():
                wiki_vault.rebuild_vault()
        except Exception:
            pass

    # Compose summary text
    m = len(summary["merges"])
    e = len(summary["enrichments"])
    r = len(summary["inferred_relations"])
    d = len(summary.get("decayed", []))
    p = len(summary.get("pruned", []))
    ins = summary.get("insights", {})
    i_add = ins.get("insights_added", 0)
    i_merge = ins.get("insights_merged", 0)
    errs = len(summary["errors"])
    duration = (datetime.now(timezone.utc) - start_time).total_seconds()
    summary["duration_s"] = round(duration, 1)
    parts = [f"{m} merge(s), {e} enrichment(s), {r} inference(s)"]
    if d or p:
        parts.append(f"{d} decayed, {p} pruned")
    if i_add or i_merge:
        parts.append(f"{i_add} insight(s), {i_merge} merged")
    if errs:
        parts.append(f"{errs} error(s)")
    parts.append(f"in {duration:.0f}s")
    summary["summary"] = ", ".join(parts)

    _status(f"Dream cycle complete — {summary['summary']}")
    _append_journal(summary)
    return summary


# ── Background daemon ────────────────────────────────────────────────────────

_dream_thread: threading.Thread | None = None
_dream_stop = threading.Event()


def start_dream_loop() -> None:
    """Start the daemon thread that checks for dream conditions."""
    global _dream_thread
    if _dream_thread is not None and _dream_thread.is_alive():
        return

    _dream_stop.clear()

    def _loop():
        while not _dream_stop.wait(timeout=_CHECK_INTERVAL_S):
            if not _should_dream():
                continue
            logger.info("Dream conditions met — starting dream cycle…")
            try:
                result = run_dream_cycle()
                logger.info("Dream cycle complete: %s", result.get("summary", ""))
            except Exception as exc:
                logger.warning("Dream cycle failed: %s", exc)

    _dream_thread = threading.Thread(target=_loop, daemon=True, name="thoth-dream-cycle")
    _dream_thread.start()
    logger.info(
        "Dream cycle daemon started — window %d:00–%d:00, checks every %d min",
        _load_config().get("window_start", 1),
        _load_config().get("window_end", 5),
        _CHECK_INTERVAL_S // 60,
    )


def stop_dream_loop() -> None:
    """Signal the dream loop thread to stop."""
    _dream_stop.set()
