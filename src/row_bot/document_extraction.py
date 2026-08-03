"""Bounded, checkpointed knowledge extraction for durable document jobs."""

from __future__ import annotations

import json
import logging
import pathlib
import re
import shutil
import threading
from collections.abc import Callable, Iterable, Iterator
from types import SimpleNamespace
from typing import Any

from langchain_core.documents import Document

from row_bot.document_jobs import (
    REDUCTION_GROUP_SIZE,
    DocumentCancelled,
    DocumentJob,
    DocumentJobService,
)

logger = logging.getLogger(__name__)

_WINDOW_SIZE = 6_000
_WINDOW_OVERLAP = 500
_state_lock = threading.Lock()
_active_extraction: dict[str, Any] | None = None


def get_extraction_status() -> dict[str, Any] | None:
    """Compatibility status view backed by the durable job service."""
    try:
        summary = DocumentJobService().active_summary()
        active = summary.get("active")
        if isinstance(active, dict) and active.get("status") == "extracting":
            return {
                "file": active.get("original_name", ""),
                "progress": active.get("extraction_progress_current", 0),
                "total": active.get("extraction_progress_total", 0),
                "entities": 0,
                "phase": active.get("stage", "knowledge_map"),
                "job_id": active.get("id", ""),
            }
    except Exception:
        logger.debug("Durable extraction status unavailable", exc_info=True)
    with _state_lock:
        return dict(_active_extraction) if _active_extraction else None


def get_queue_length() -> int:
    try:
        return int(DocumentJobService().active_summary()["remaining"])
    except Exception:
        return 0


def stop_extraction() -> bool:
    try:
        active = DocumentJobService().active_summary().get("active")
        if isinstance(active, dict) and active.get("status") == "extracting":
            DocumentJobService().cancel_job(str(active["id"]))
            return True
    except Exception:
        logger.debug("Could not cancel durable extraction", exc_info=True)
    return False


def iter_extraction_windows(
    pages: Iterable[Document],
    *,
    window_size: int = _WINDOW_SIZE,
    overlap: int = _WINDOW_OVERLAP,
) -> Iterator[str]:
    """Yield rolling windows while retaining at most one bounded buffer."""
    if window_size <= 0 or overlap < 0 or overlap >= window_size:
        raise ValueError("Invalid extraction window bounds")
    buffer = ""
    for page in pages:
        content = getattr(page, "page_content", "")
        if not isinstance(content, str) or not content:
            continue
        offset = 0
        while offset < len(content):
            room = window_size - len(buffer)
            take = min(room, len(content) - offset)
            buffer += content[offset : offset + take]
            offset += take
            if len(buffer) == window_size:
                yield buffer
                buffer = buffer[-overlap:] if overlap else ""
    if buffer:
        yield buffer


def _split_into_windows(
    text: str,
    window_size: int = _WINDOW_SIZE,
    overlap: int = _WINDOW_OVERLAP,
) -> list[str]:
    """Legacy test/helper API; durable extraction uses the iterator above."""
    return list(
        iter_extraction_windows(
            [Document(page_content=text)],
            window_size=window_size,
            overlap=overlap,
        )
    )


def _llm_call(prompt: str) -> str:
    from langchain_core.messages import HumanMessage
    from row_bot.models import get_current_model, get_llm_for

    llm = get_llm_for(get_current_model())
    response = llm.invoke([HumanMessage(content=prompt)])
    raw = response.content or ""
    if isinstance(raw, list):
        parts: list[str] = []
        for block in raw:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif isinstance(block, str):
                parts.append(block)
        raw = "\n".join(parts)
    if not isinstance(raw, str):
        raw = str(raw) if raw else ""
    raw = re.sub(r"<think>.*?</think>", "", raw.strip(), flags=re.DOTALL)
    return re.sub(r"</?think>", "", raw).strip()


def _map_summarize_window(
    text: str,
    title: str,
    section_num: int,
    total_sections: int,
) -> str:
    from row_bot.prompts import DOC_MAP_PROMPT

    prompt = DOC_MAP_PROMPT.format(
        document_title=title,
        section_number=section_num,
        total_sections=total_sections or "unknown",
        document_text=text,
    )
    return _llm_call(prompt)


def _reduce_summaries(title: str, summaries: list[str]) -> str:
    """Reduce one fixed, small group of summaries."""
    from row_bot.prompts import DOC_REDUCE_PROMPT

    joined = "\n\n".join(
        f"[Section {index}] {summary}"
        for index, summary in enumerate(summaries, 1)
        if summary.strip()
    )
    if not joined:
        return ""
    return _llm_call(
        DOC_REDUCE_PROMPT.format(
            document_title=title,
            section_summaries=joined,
        )
    )


def _extract_from_summary(title: str, summary: str) -> list[dict[str, Any]]:
    try:
        from row_bot.prompts import DOC_EXTRACT_PROMPT
        import row_bot.knowledge_graph as kg

        raw = _llm_call(
            DOC_EXTRACT_PROMPT.format(
                document_title=title,
                document_summary=summary,
            )
        )
        json_text = kg.extract_json_block(raw, "[")
        data = json.loads(json_text) if json_text else []
        if not isinstance(data, list):
            return []
        return [
            entry
            for entry in data
            if isinstance(entry, dict)
            and (
                (
                    entry.get("category")
                    and entry.get("subject")
                    and entry.get("content")
                )
                or (
                    entry.get("relation_type")
                    and entry.get("source_subject")
                    and entry.get("target_subject")
                )
            )
        ]
    except Exception as exc:
        logger.warning("Document entity extraction failed for %s: %s", title, exc)
        return []


def _cross_window_dedup(all_extracted: list[dict]) -> list[dict]:
    entities_by_subject: dict[str, dict] = {}
    relations: list[dict] = []
    for entry in all_extracted:
        if entry.get("relation_type"):
            relations.append(entry)
            continue
        subject = str(entry.get("subject") or "").strip()
        if not subject:
            continue
        key = subject.casefold()
        if key not in entities_by_subject:
            entities_by_subject[key] = dict(entry)
            continue
        existing = entities_by_subject[key]
        old_content = str(existing.get("content") or "")
        new_content = str(entry.get("content") or "")
        if new_content and new_content.casefold() not in old_content.casefold():
            existing["content"] = f"{old_content}. {new_content}".replace(". . ", ". ")
    return list(entities_by_subject.values()) + relations


def _copy_to_vault_raw(
    file_path: str,
    stored_name: str,
    *,
    original_name: str | None = None,
    document_id: str = "",
) -> pathlib.Path | None:
    """Copy by collision-safe stored name and retain original display metadata."""
    try:
        import row_bot.wiki_vault as wiki_vault

        if not wiki_vault.is_enabled():
            return None
        raw_dir = wiki_vault.get_vault_path() / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        destination = raw_dir / stored_name
        if not destination.exists():
            shutil.copy2(file_path, destination)
        metadata_dir = raw_dir / ".metadata"
        metadata_dir.mkdir(parents=True, exist_ok=True)
        metadata_path = metadata_dir / f"{stored_name}.json"
        metadata_path.write_text(
            json.dumps(
                {
                    "document_id": document_id,
                    "original_name": original_name or stored_name,
                    "stored_name": stored_name,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return destination
    except Exception:
        logger.debug("Document vault raw copy skipped", exc_info=True)
        return None


def _fixed_groups(
    values: Iterable[tuple[int, str]],
    group_size: int = REDUCTION_GROUP_SIZE,
) -> Iterator[list[str]]:
    group: list[str] = []
    for _index, value in values:
        if value.strip():
            group.append(value)
        if len(group) >= group_size:
            yield group
            group = []
    if group:
        yield group


def _one_summary(values: Iterable[tuple[int, str]]) -> str:
    for _index, value in values:
        if value.strip():
            return value
    return ""


def _hierarchical_reduce(
    job: DocumentJob,
    service: Any,
    title: str,
) -> str:
    map_count = sum(1 for _ in service.iter_map_summaries(job.id))
    if map_count == 0:
        return ""
    if map_count == 1:
        if service.count_reduce_summaries(job.id, 1) == 0:
            service.raise_if_cancelled(job.id)
            reduced = _reduce_summaries(
                title,
                [_one_summary(service.iter_map_summaries(job.id))],
            )
            service.raise_if_cancelled(job.id)
            service.store_reduce_summary(job.id, 1, 0, reduced)
        return _one_summary(service.iter_reduce_summaries(job.id, 1))

    source_level = 0
    source_count = map_count
    while source_count > 1:
        target_level = source_level + 1
        completed_groups = service.count_reduce_summaries(job.id, target_level)
        source_values = (
            service.iter_map_summaries(job.id)
            if source_level == 0
            else service.iter_reduce_summaries(job.id, source_level)
        )
        for group_index, group in enumerate(_fixed_groups(source_values)):
            if group_index < completed_groups:
                continue
            service.raise_if_cancelled(job.id)
            reduced = _reduce_summaries(title, group)
            service.raise_if_cancelled(job.id)
            service.store_reduce_summary(job.id, target_level, group_index, reduced)
            service.update_progress(
                job.id,
                stage="knowledge_reduce",
                current=group_index + 1,
                total=0,
            )
        source_count = service.count_reduce_summaries(job.id, target_level)
        if source_count == 0:
            return ""
        source_level = target_level
    return _one_summary(service.iter_reduce_summaries(job.id, source_level))


def _find_document_hub_by_source(source_label: str) -> dict | None:
    import row_bot.knowledge_graph as kg

    conn = kg._get_conn()
    try:
        row = conn.execute(
            """
            SELECT id FROM entities
            WHERE entity_type='media' AND source=?
            ORDER BY created_at LIMIT 1
            """,
            (source_label,),
        ).fetchone()
    finally:
        conn.close()
    return kg.get_entity(row[0]) if row else None


def _commit_knowledge(
    job: DocumentJob,
    article: str,
    *,
    window_count: int,
    summary_count: int,
) -> int:
    import row_bot.knowledge_graph as kg
    import row_bot.memory_evolution as memory_evolution
    from row_bot.memory import update_memory

    title = pathlib.Path(job.original_name).stem
    source_label = str(getattr(job, "source_label", "") or f"document:{job.id}")
    context = {
        "kind": "document",
        "document_id": job.id,
        "display_name": job.original_name,
        "stored_name": job.stored_name,
        "document_title": title,
        "window_count": window_count,
        "summary_count": summary_count,
        "actor": "document_extraction",
    }
    properties = memory_evolution.merge_properties(
        {},
        {
            "status": "active",
            "memory_tier": "resource",
            "source_context": context,
            "evidence_count": summary_count,
        },
        source=source_label,
        entity_type="media",
        actor="document_extraction",
        source_context=context,
    )
    kg._skip_reindex = True
    hub = _find_document_hub_by_source(source_label)
    if hub:
        update_memory(
            hub["id"],
            article,
            source=source_label,
            properties=properties,
        )
        hub = kg.get_entity(hub["id"])
    else:
        hub = kg.save_entity(
            "media",
            title,
            article,
            source=source_label,
            properties=properties,
        )
    saved = 1 if hub else 0
    if hub:
        user_id = kg._ensure_user_entity()
        if user_id:
            kg.add_relation(user_id, hub["id"], "uploaded", source=source_label)

    extracted = _extract_from_summary(title, article)
    entities = [
        entry
        for entry in extracted
        if entry.get("category")
        and not entry.get("relation_type")
        and len(str(entry.get("content") or "").strip()) >= 30
    ][:12]
    relations = [entry for entry in extracted if entry.get("relation_type")]
    if entities or relations:
        from row_bot.memory_extraction import _dedup_and_save

        saved += _dedup_and_save(
            _cross_window_dedup(entities + relations),
            source=source_label,
            source_context=context,
        )
        if hub:
            conn = kg._get_conn()
            try:
                rows = conn.execute(
                    "SELECT id FROM entities WHERE source=? AND id!=?",
                    (source_label, hub["id"]),
                ).fetchall()
            finally:
                conn.close()
            for row in rows:
                try:
                    kg.add_relation(
                        row[0],
                        hub["id"],
                        "extracted_from",
                        source=source_label,
                    )
                except Exception:
                    logger.debug("Document extracted_from relation skipped", exc_info=True)
    return saved


def extract_document_job(job: DocumentJob, service: Any) -> dict[str, Any]:
    """Resume bounded map/reduce extraction for one already-searchable job."""
    from row_bot.documents import (
        iter_document_pages,
        release_document_embedding_resources,
    )

    title = pathlib.Path(job.original_name).stem
    result = {
        "entities_saved": 0,
        "document_title": title,
        "status": "error",
        "error": None,
    }
    try:
        service.raise_if_cancelled(job.id)
        _copy_to_vault_raw(
            job.staged_path,
            job.stored_name,
            original_name=job.original_name,
            document_id=job.id,
        )
        completed_window = service.last_map_window(job.id)
        window_count = 0
        windows = iter_extraction_windows(iter_document_pages(job.staged_path))
        for window_index, window in enumerate(windows):
            window_count = window_index + 1
            if window_index <= completed_window:
                continue
            service.raise_if_cancelled(job.id)
            summary = _map_summarize_window(
                window,
                title,
                window_index + 1,
                0,
            )
            service.raise_if_cancelled(job.id)
            service.store_map_summary(job.id, window_index, summary)
            service.update_progress(
                job.id,
                stage="knowledge_map",
                current=window_index + 1,
                total=0,
            )
        service.update_progress(
            job.id,
            stage="knowledge_map",
            current=window_count,
            total=window_count,
        )
        service.raise_if_cancelled(job.id)
        article = _hierarchical_reduce(job, service, title)
        if not article or len(article.strip()) < 50:
            logger.warning("Knowledge reduction produced no substantial article for %s", job.original_name)
            result["status"] = "completed"
            return result
        service.raise_if_cancelled(job.id)
        summary_count = sum(
            1 for _index, summary in service.iter_map_summaries(job.id) if summary.strip()
        )
        service.update_progress(
            job.id,
            stage="knowledge_commit",
            current=0,
            total=1,
        )
        result["entities_saved"] = _commit_knowledge(
            job,
            article,
            window_count=window_count,
            summary_count=summary_count,
        )
        service.raise_if_cancelled(job.id)
        service.update_progress(
            job.id,
            stage="knowledge_commit",
            current=1,
            total=1,
        )
        result["status"] = "completed"
        return result
    except DocumentCancelled:
        result["status"] = "stopped"
        raise
    except Exception as exc:
        result["error"] = str(exc)
        logger.error("Document extraction failed for %s: %s", job.original_name, exc)
        raise
    finally:
        release_document_embedding_resources("document extraction complete")


class _CompatibilityCheckpoint:
    def __init__(self, stop_event: threading.Event | None = None) -> None:
        self.stop_event = stop_event
        self.maps: dict[int, str] = {}
        self.reductions: dict[tuple[int, int], str] = {}

    def raise_if_cancelled(self, _job_id: str) -> None:
        if self.stop_event and self.stop_event.is_set():
            raise DocumentCancelled("Compatibility extraction stopped")

    def last_map_window(self, _job_id: str) -> int:
        return max(self.maps, default=-1)

    def store_map_summary(self, _job_id: str, index: int, summary: str) -> None:
        self.maps.setdefault(index, summary)

    def iter_map_summaries(self, _job_id: str):
        yield from sorted(self.maps.items())

    def count_reduce_summaries(self, _job_id: str, level: int) -> int:
        return sum(key[0] == level for key in self.reductions)

    def store_reduce_summary(
        self, _job_id: str, level: int, group_index: int, summary: str
    ) -> None:
        self.reductions.setdefault((level, group_index), summary)

    def iter_reduce_summaries(self, _job_id: str, level: int):
        for (stored_level, group_index), summary in sorted(self.reductions.items()):
            if stored_level == level:
                yield group_index, summary

    def update_progress(self, _job_id: str, *, stage: str, current: int, total: int = 0) -> None:
        with _state_lock:
            if _active_extraction is not None:
                _active_extraction.update(
                    {
                        "phase": stage,
                        "progress": current,
                        "total": total,
                    }
                )


def extract_from_document(
    file_path: str,
    display_name: str,
    on_status: Callable[[str], None] | None = None,
    stop_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Compatibility wrapper for direct callers outside the durable queue."""
    del on_status
    job_id = f"compat-{pathlib.Path(file_path).stat().st_mtime_ns:x}"
    job = SimpleNamespace(
        id=job_id,
        original_name=display_name,
        stored_name=f"{pathlib.Path(display_name).stem}-{job_id[-8:]}{pathlib.Path(display_name).suffix}",
        staged_path=str(file_path),
        source_label=f"document:{display_name}",
    )
    checkpoint = _CompatibilityCheckpoint(stop_event)
    with _state_lock:
        global _active_extraction
        _active_extraction = {
            "file": display_name,
            "progress": 0,
            "total": 0,
            "entities": 0,
            "phase": "knowledge_map",
        }
    try:
        return extract_document_job(job, checkpoint)
    except DocumentCancelled:
        return {
            "entities_saved": 0,
            "document_title": pathlib.Path(display_name).stem,
            "status": "stopped",
            "error": None,
        }
    except Exception as exc:
        return {
            "entities_saved": 0,
            "document_title": pathlib.Path(display_name).stem,
            "status": "error",
            "error": str(exc),
        }
    finally:
        with _state_lock:
            _active_extraction = None


def queue_extraction(file_path: str, display_name: str) -> None:
    """Deprecated compatibility path; durable uploads use the supervisor."""
    thread = threading.Thread(
        target=extract_from_document,
        args=(file_path, display_name),
        daemon=True,
        name="row-bot-document-extraction-compat",
    )
    thread.start()
