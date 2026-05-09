"""Background document extraction — extracts knowledge graph entities from uploaded files.

When a document is uploaded via Settings → Documents, it is first
chunk-indexed into FAISS (instant retrieval) and then queued here for
LLM-based entity extraction.  The pipeline (map-reduce):

1. Load the full document text via ``documents.load_document_text()``
2. Split into overlapping windows (~6 000 chars each)
3. MAP — summarize each window into 3-5 sentences
4. REDUCE — combine all summaries into one 300-600 word article
5. EXTRACT — pull core entities + relations from the reduced summary
6. Save one media entity (the document) + extracted entities/relations
7. Rebuild FAISS index + wiki vault once at the end

Progress is exposed via ``get_extraction_status()`` for the status bar,
and results are logged to the activity panel via ``notifications.notify()``.
"""

from __future__ import annotations

import json
import logging
import pathlib
import re
import shutil
import threading
from collections import defaultdict
from typing import Callable

logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────
_WINDOW_SIZE = 6000      # chars per extraction window
_WINDOW_OVERLAP = 500    # char overlap between windows

# ── Module-level state for UI polling ────────────────────────────────────────
_state_lock = threading.Lock()
_active_extraction: dict | None = None
# {"file": "report.pdf", "progress": 5, "total": 23, "entities": 8, "status": "running"}

_extraction_queue: list[tuple[str, str]] = []  # [(file_path, display_name), ...]
_queue_lock = threading.Lock()
_stop_event = threading.Event()
_worker_thread: threading.Thread | None = None


# ═════════════════════════════════════════════════════════════════════════════
# Public API
# ═════════════════════════════════════════════════════════════════════════════

def get_extraction_status() -> dict | None:
    """Return current document extraction status for the status bar.

    Returns ``None`` when idle.
    """
    with _state_lock:
        return dict(_active_extraction) if _active_extraction else None


def get_queue_length() -> int:
    """Return the number of documents waiting in the extraction queue."""
    with _queue_lock:
        return len(_extraction_queue)


def stop_extraction() -> bool:
    """Signal the current extraction to stop. Returns True if one was running."""
    with _state_lock:
        if _active_extraction is None:
            return False
    _stop_event.set()
    return True


def queue_extraction(file_path: str, display_name: str) -> None:
    """Add a document to the extraction queue.

    If no worker is running, starts the background thread.
    """
    with _queue_lock:
        _extraction_queue.append((file_path, display_name))
    _ensure_worker()


# ═════════════════════════════════════════════════════════════════════════════
# Internal: window splitting
# ═════════════════════════════════════════════════════════════════════════════

def _split_into_windows(
    text: str,
    window_size: int = _WINDOW_SIZE,
    overlap: int = _WINDOW_OVERLAP,
) -> list[str]:
    """Split *text* into overlapping windows of approximately *window_size* chars."""
    if len(text) <= window_size:
        return [text]
    windows: list[str] = []
    start = 0
    while start < len(text):
        end = start + window_size
        windows.append(text[start:end])
        start = end - overlap
    return windows


# ═════════════════════════════════════════════════════════════════════════════
# Internal: LLM helpers
# ═════════════════════════════════════════════════════════════════════════════

def _llm_call(prompt: str) -> str:
    """Invoke the current LLM with a single human message and return clean text."""
    from models import get_current_model, get_llm_for
    from langchain_core.messages import HumanMessage

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
    return raw


# ── MAP step: summarize one window ──────────────────────────────────────────

def _map_summarize_window(
    text: str,
    title: str,
    section_num: int,
    total_sections: int,
) -> str:
    """Summarize a single window into 3-5 sentences via DOC_MAP_PROMPT."""
    try:
        from prompts import DOC_MAP_PROMPT

        prompt = DOC_MAP_PROMPT.format(
            document_title=title,
            section_number=section_num,
            total_sections=total_sections,
            document_text=text,
        )
        return _llm_call(prompt)
    except Exception as exc:
        logger.warning("MAP summarize failed for section %d: %s", section_num, exc)
        return ""


# ── REDUCE step: combine all summaries into one article ─────────────────────

def _reduce_summaries(title: str, summaries: list[str]) -> str:
    """Combine section summaries into one coherent document article."""
    try:
        from prompts import DOC_REDUCE_PROMPT

        joined = "\n\n".join(
            f"[Section {i}] {s}" for i, s in enumerate(summaries, 1) if s.strip()
        )
        prompt = DOC_REDUCE_PROMPT.format(
            document_title=title,
            section_summaries=joined,
        )
        return _llm_call(prompt)
    except Exception as exc:
        logger.warning("REDUCE failed for %s: %s", title, exc)
        # Fallback: concatenate summaries
        return " ".join(s for s in summaries if s.strip())


# ── EXTRACT step: pull entities from the reduced summary ────────────────────

def _extract_from_summary(title: str, summary: str) -> list[dict]:
    """Extract core entities + relations from the document summary."""
    try:
        from prompts import DOC_EXTRACT_PROMPT

        prompt = DOC_EXTRACT_PROMPT.format(
            document_title=title,
            document_summary=summary,
        )
        raw = _llm_call(prompt)

        # Extract JSON array from response (bracket-counting
        # handles nested arrays correctly, unlike greedy regex).
        import knowledge_graph as _kg_parse
        _json_str = _kg_parse.extract_json_block(raw, "[")
        if not _json_str:
            return []
        data = json.loads(_json_str)
        if not isinstance(data, list):
            return []

        valid: list[dict] = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            if entry.get("category") and entry.get("subject") and entry.get("content"):
                valid.append(entry)
            elif entry.get("relation_type") and entry.get("source_subject") and entry.get("target_subject"):
                valid.append(entry)
        return valid
    except Exception as exc:
        logger.warning("EXTRACT failed for %s: %s", title, exc)
        return []


# ═════════════════════════════════════════════════════════════════════════════
# Internal: cross-window dedup (kept for backward compat, used as safety net)
# ═════════════════════════════════════════════════════════════════════════════

def _cross_window_dedup(all_extracted: list[dict]) -> list[dict]:
    """Merge entities with the same subject across multiple windows.

    Relations are passed through unchanged.
    """
    entities_by_subject: dict[str, dict] = {}
    relations: list[dict] = []

    for entry in all_extracted:
        if entry.get("relation_type"):
            relations.append(entry)
            continue

        subject = (entry.get("subject") or "").strip()
        if not subject:
            continue
        key = subject.lower()

        if key in entities_by_subject:
            existing = entities_by_subject[key]
            # Merge content
            old_content = existing.get("content", "")
            new_content = entry.get("content", "")
            if new_content.lower() not in old_content.lower():
                existing["content"] = f"{old_content}. {new_content}".replace(". . ", ". ")
            # Merge aliases
            old_aliases = existing.get("aliases", "") or ""
            new_aliases = entry.get("aliases", "") or ""
            if isinstance(new_aliases, list):
                new_aliases = ", ".join(str(a) for a in new_aliases)
            if new_aliases:
                old_set = {a.strip().lower() for a in old_aliases.split(",") if a.strip()}
                for alias in new_aliases.split(","):
                    alias = alias.strip()
                    if alias and alias.lower() not in old_set:
                        old_aliases = f"{old_aliases}, {alias}" if old_aliases else alias
                        old_set.add(alias.lower())
                existing["aliases"] = old_aliases
        else:
            entities_by_subject[key] = dict(entry)

    return list(entities_by_subject.values()) + relations


# ═════════════════════════════════════════════════════════════════════════════
# Internal: core extraction pipeline
# ═════════════════════════════════════════════════════════════════════════════

def _copy_to_vault_raw(file_path: str, display_name: str) -> None:
    """Copy the original file to vault/raw/ if wiki vault is enabled."""
    try:
        import wiki_vault
        if wiki_vault.is_enabled():
            raw_dir = wiki_vault.get_vault_path() / "raw"
            raw_dir.mkdir(parents=True, exist_ok=True)
            dest = raw_dir / display_name
            if not dest.exists():
                shutil.copy2(file_path, dest)
                logger.info("Copied %s to vault/raw/", display_name)
    except Exception as exc:
        logger.debug("Failed to copy to vault/raw/: %s", exc)


def extract_from_document(
    file_path: str,
    display_name: str,
    on_status: Callable[[str], None] | None = None,
    stop_event: threading.Event | None = None,
) -> dict:
    """Map-reduce extraction from an uploaded document.

    Pipeline:
      1. Load full text
      2. Copy to vault/raw/
      3. Split into windows
      4. MAP: summarize each window (3-5 sentences)
      5. REDUCE: combine summaries into one article
      6. Create media entity (the document itself) with the article
      7. EXTRACT: pull core entities + relations from the article
      8. Save entities + relations via _dedup_and_save
      9. Link all extracted entities → document hub via extracted_from
     10. Rebuild FAISS index + wiki vault

    Returns dict with:
      - entities_saved: int
      - document_title: str
      - status: "completed" | "stopped" | "error"
      - error: str | None
    """
    from documents import load_document_text

    title = pathlib.Path(display_name).stem
    result = {
        "entities_saved": 0,
        "document_title": title,
        "status": "error",
        "error": None,
    }

    try:
        # 1. Load full text
        if on_status:
            on_status(f"Loading {display_name}…")
        full_text, _ = load_document_text(file_path)

        # 2. Copy original to vault/raw/
        _copy_to_vault_raw(file_path, display_name)

        # 3. Split into windows
        windows = _split_into_windows(full_text)
        total = len(windows)
        if on_status:
            on_status(f"Summarizing {display_name} ({total} section(s))…")

        # 4. MAP — summarize each window
        summaries: list[str] = []
        for i, window in enumerate(windows, 1):
            if stop_event and stop_event.is_set():
                result["status"] = "stopped"
                break

            if on_status:
                on_status(
                    f"{display_name}: summarizing section {i}/{total}"
                )

            # Update module-level state for status bar polling
            with _state_lock:
                if _active_extraction:
                    _active_extraction["progress"] = i
                    _active_extraction["total"] = total
                    _active_extraction["entities"] = 0
                    _active_extraction["phase"] = "map"

            summary = _map_summarize_window(window, title, i, total)
            if summary:
                summaries.append(summary)

        if result["status"] == "stopped":
            return result

        if not summaries:
            logger.warning("No summaries produced for %s", display_name)
            result["status"] = "completed"
            return result

        # 5. REDUCE — combine into one article
        if on_status:
            on_status(f"{display_name}: compiling article…")
        with _state_lock:
            if _active_extraction:
                _active_extraction["phase"] = "reduce"

        article = _reduce_summaries(title, summaries)

        if not article or len(article.strip()) < 50:
            logger.warning("Reduce produced empty article for %s", display_name)
            result["status"] = "completed"
            return result

        # 6. Create the document media entity (hub) — dedup if re-uploading
        import knowledge_graph as kg
        from memory import find_by_subject, update_memory
        source_label = f"document:{display_name}"

        kg._skip_reindex = True

        # Check if a hub entity already exists (re-upload scenario)
        existing_hub = find_by_subject("media", title)
        if existing_hub:
            # Update description with the new article
            try:
                update_memory(existing_hub["id"], article, source=source_label)
                hub_entity = existing_hub
                logger.info("Updated existing document hub: %s", title)
            except Exception:
                hub_entity = existing_hub
        else:
            hub_entity = kg.save_entity(
                entity_type="media",
                subject=title,
                description=article,
                source=source_label,
            )
        hub_id = hub_entity["id"] if hub_entity else None
        saved_count = 1 if hub_entity else 0

        # Link User → uploaded → document hub
        if hub_id:
            user_id = kg._ensure_user_entity()
            if user_id:
                kg.add_relation(
                    user_id, hub_id, "uploaded",
                    source=source_label,
                )

        # 7. EXTRACT — pull core entities from the article
        if on_status:
            on_status(f"{display_name}: extracting key entities…")
        with _state_lock:
            if _active_extraction:
                _active_extraction["phase"] = "extract"

        extracted = _extract_from_summary(title, article)

        # ── Post-extraction quality gates ────────────────────────────
        if extracted:
            _DOC_ENTITY_CAP = 12
            _MIN_DESC_LEN = 30

            # Separate entities and relations
            entities = [e for e in extracted if e.get("category") and not e.get("relation_type")]
            relations = [e for e in extracted if e.get("relation_type")]

            # Filter entities with thin descriptions
            entities = [
                e for e in entities
                if len((e.get("content") or "").strip()) >= _MIN_DESC_LEN
            ]

            # Cap entity count — keep highest confidence first, then first-seen order
            if len(entities) > _DOC_ENTITY_CAP:
                logger.info(
                    "Document entity cap: %d entities → keeping top %d for %s",
                    len(entities), _DOC_ENTITY_CAP, display_name,
                )
                entities = entities[:_DOC_ENTITY_CAP]

            extracted = entities + relations

        # 8. Save entities + relations via _dedup_and_save
        if extracted:
            from memory_extraction import _dedup_and_save

            deduped = _cross_window_dedup(extracted)
            batch_saved = _dedup_and_save(deduped, source=source_label)
            saved_count += batch_saved

            # 9. Link extracted entities → document hub
            if hub_id:
                conn = kg._get_conn()
                rows = conn.execute(
                    "SELECT id FROM entities WHERE source = ? AND id != ?",
                    (source_label, hub_id),
                ).fetchall()
                conn.close()

                for row in rows:
                    try:
                        kg.add_relation(
                            row[0], hub_id, "extracted_from",
                            source=source_label,
                        )
                    except Exception:
                        pass

        result["entities_saved"] = saved_count

        # 10. Rebuild indices
        try:
            kg._skip_reindex = False
            kg.rebuild_index()
        except Exception as exc:
            logger.debug("Post-document rebuild_index failed: %s", exc)

        # Wiki vault rebuild
        try:
            import wiki_vault
            if wiki_vault.is_enabled():
                wiki_vault.rebuild_vault()
                logger.info("Post-document wiki vault rebuild complete")
        except Exception as exc:
            logger.debug("Post-document wiki rebuild skipped: %s", exc)

        if result["status"] != "stopped":
            result["status"] = "completed"

    except Exception as exc:
        logger.error("Document extraction failed for %s: %s", display_name, exc)
        result["status"] = "error"
        result["error"] = str(exc)
    finally:
        try:
            from documents import release_document_embedding_resources

            release_document_embedding_resources("document extraction complete")
        except Exception:
            logger.debug("Document embedding resource release failed", exc_info=True)

    return result


# ═════════════════════════════════════════════════════════════════════════════
# Internal: background worker
# ═════════════════════════════════════════════════════════════════════════════

def _ensure_worker() -> None:
    """Start the background extraction worker thread if not already running."""
    global _worker_thread
    if _worker_thread is not None and _worker_thread.is_alive():
        return
    _worker_thread = threading.Thread(
        target=_worker_loop, daemon=True, name="thoth-doc-extract",
    )
    _worker_thread.start()


def _worker_loop() -> None:
    """Process the extraction queue one document at a time."""
    from notifications import notify

    while True:
        with _queue_lock:
            if not _extraction_queue:
                break
            file_path, display_name = _extraction_queue.pop(0)

        _stop_event.clear()

        with _state_lock:
            global _active_extraction
            _active_extraction = {
                "file": display_name,
                "progress": 0,
                "total": 0,
                "entities": 0,
                "status": "running",
            }

        try:
            result = extract_from_document(
                file_path, display_name,
                stop_event=_stop_event,
            )

            # Notify via activity panel
            status = result["status"]
            saved = result["entities_saved"]

            if status == "completed":
                notify(
                    "Document Extraction",
                    f"📄 Extracted {saved} entities from {display_name}",
                    icon="📄",
                )
                logger.info(
                    "Document extraction complete: %s → %d entities",
                    display_name, saved,
                )
            elif status == "stopped":
                notify(
                    "Document Extraction",
                    f"⏹ Extraction stopped for {display_name} ({saved} entities saved)",
                    icon="⏹",
                )
                logger.info(
                    "Document extraction stopped: %s → %d entities",
                    display_name, saved,
                )
            elif status == "error":
                notify(
                    "Document Extraction",
                    f"❌ Extraction failed for {display_name}: {result.get('error', 'unknown')}",
                    icon="❌",
                    toast_type="negative",
                )
                logger.error(
                    "Document extraction failed: %s — %s",
                    display_name, result.get("error"),
                )
        except Exception as exc:
            notify(
                "Document Extraction",
                f"❌ Extraction failed for {display_name}: {exc}",
                icon="❌",
                toast_type="negative",
            )
            logger.error("Document extraction crashed: %s — %s", display_name, exc)
        finally:
            with _state_lock:
                _active_extraction = None

            # Clean up staging file
            try:
                p = pathlib.Path(file_path)
                if p.exists() and "doc_staging" in str(p):
                    p.unlink()
            except Exception:
                pass

    # Worker done — clear thread reference
    global _worker_thread
    _worker_thread = None
