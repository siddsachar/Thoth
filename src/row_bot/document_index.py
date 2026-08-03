"""Transactional, per-document FAISS shards with legacy-read compatibility."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import pathlib
import shutil
import threading
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime
from typing import Any

from langchain_core.documents import Document

from row_bot.data_paths import get_row_bot_data_dir
from row_bot.document_jobs import INDEX_SEGMENT_CHUNKS, DocumentCancelled
from row_bot.embedding_config import (
    active_embedding_metadata,
    index_metadata_matches,
)

logger = logging.getLogger(__name__)

DATA_DIR = get_row_bot_data_dir()
DOCUMENT_INDEX_DIR = DATA_DIR / "document_index"
LEGACY_VECTOR_STORE_DIR = DATA_DIR / "vector_store"
CORPUS_MANIFEST_NAME = "manifest.json"
DOCUMENTS_DIR_NAME = "documents"
MANIFEST_VERSION = 1
_manifest_lock = threading.RLock()


def _empty_manifest() -> dict[str, Any]:
    return {"version": MANIFEST_VERSION, "documents": []}


def _read_json(path: pathlib.Path, default: Any) -> Any:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError):
        return default


def _atomic_write_json(
    path: pathlib.Path,
    value: Any,
    *,
    replace: Callable[[str | os.PathLike[str], str | os.PathLike[str]], None] = os.replace,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    try:
        replace(temp, path)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            temp.unlink()
        raise


def read_corpus_manifest(index_root: pathlib.Path = DOCUMENT_INDEX_DIR) -> dict[str, Any]:
    manifest = _read_json(index_root / CORPUS_MANIFEST_NAME, _empty_manifest())
    if not isinstance(manifest, dict) or not isinstance(manifest.get("documents"), list):
        return _empty_manifest()
    return manifest


def initialize_index(index_root: pathlib.Path = DOCUMENT_INDEX_DIR) -> None:
    (index_root / DOCUMENTS_DIR_NAME).mkdir(parents=True, exist_ok=True)
    manifest_path = index_root / CORPUS_MANIFEST_NAME
    if not manifest_path.exists():
        _atomic_write_json(manifest_path, _empty_manifest())


def _safe_remove_tree(path: pathlib.Path, owner: pathlib.Path) -> None:
    resolved = path.resolve()
    resolved_owner = owner.resolve()
    if resolved == resolved_owner or resolved_owner not in resolved.parents:
        raise ValueError(f"Refusing cleanup outside {resolved_owner}")
    if resolved.exists():
        shutil.rmtree(resolved)


def _segment_parts(
    batches: Iterable[list[Document]],
    *,
    segment_chunks: int,
) -> Iterator[tuple[list[Document], bool]]:
    """Yield pieces plus a flag indicating that a segment is full."""
    count = 0
    for batch in batches:
        offset = 0
        while offset < len(batch):
            room = segment_chunks - count
            part = batch[offset : offset + room]
            offset += len(part)
            count += len(part)
            full = count >= segment_chunks
            yield part, full
            if full:
                count = 0


def build_unpublished_document(
    *,
    document_id: str,
    original_name: str,
    stored_name: str,
    content_sha256: str,
    chunks: Iterable[Document],
    work_document_dir: pathlib.Path,
    embedding: Any,
    check_cancelled: Callable[[], None] | None = None,
    progress: Callable[[int], None] | None = None,
    embedding_metadata: dict[str, Any] | None = None,
    segment_chunks: int = INDEX_SEGMENT_CHUNKS,
) -> dict[str, Any]:
    """Build fixed-size FAISS segments under an unpublished work directory."""
    from langchain_community.vectorstores import FAISS
    from row_bot.documents import iter_chunk_batches

    if work_document_dir.exists():
        _safe_remove_tree(work_document_dir, work_document_dir.parent)
    work_document_dir.mkdir(parents=True, exist_ok=False)

    segment_number = 0
    segment_count = 0
    total_chunks = 0
    segment_store = None
    segments: list[dict[str, Any]] = []

    def save_segment() -> None:
        nonlocal segment_number, segment_count, segment_store
        if segment_store is None or segment_count == 0:
            return
        if check_cancelled:
            check_cancelled()
        name = f"segment-{segment_number:04d}"
        segment_path = work_document_dir / name
        segment_store.save_local(str(segment_path))
        segments.append({"name": name, "chunk_count": segment_count})
        segment_number += 1
        segment_count = 0
        segment_store = None

    try:
        batches = iter_chunk_batches(chunks)
        for part, segment_full in _segment_parts(batches, segment_chunks=segment_chunks):
            if check_cancelled:
                check_cancelled()
            if not part:
                continue
            if segment_store is None:
                segment_store = FAISS.from_documents(part, embedding=embedding)
            else:
                segment_store.add_documents(part)
            segment_count += len(part)
            total_chunks += len(part)
            if progress:
                progress(total_chunks)
            if segment_full:
                save_segment()
        save_segment()
        if not total_chunks:
            raise ValueError("No valid text content found in the document.")
        manifest = {
            "version": MANIFEST_VERSION,
            "document_id": document_id,
            "original_name": original_name,
            "stored_name": stored_name,
            "content_sha256": content_sha256,
            "embedding": embedding_metadata or active_embedding_metadata(),
            "created_at": datetime.now(UTC).isoformat(),
            "chunk_count": total_chunks,
            "segment_count": len(segments),
            "segments": segments,
            "complete": True,
        }
        _atomic_write_json(work_document_dir / "manifest.json", manifest)
        return manifest
    except Exception:
        _safe_remove_tree(work_document_dir, work_document_dir.parent)
        raise


def publish_document(
    work_document_dir: pathlib.Path,
    document_manifest: dict[str, Any],
    *,
    index_root: pathlib.Path = DOCUMENT_INDEX_DIR,
    replace: Callable[[str | os.PathLike[str], str | os.PathLike[str]], None] = os.replace,
) -> None:
    """Publish one complete document, then atomically expose it in the corpus."""
    with _manifest_lock:
        initialize_index(index_root)
        document_id = str(document_manifest["document_id"])
        live_documents = index_root / DOCUMENTS_DIR_NAME
        live_dir = live_documents / document_id
        if live_dir.exists():
            retired = index_root / "retired" / (
                f"{document_id}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}"
            )
            retired.parent.mkdir(parents=True, exist_ok=True)
            os.replace(live_dir, retired)
        os.replace(work_document_dir, live_dir)

        manifest_path = index_root / CORPUS_MANIFEST_NAME
        corpus = read_corpus_manifest(index_root)
        entries = [
            entry
            for entry in corpus["documents"]
            if str(entry.get("document_id")) != document_id
        ]
        entries.append(
            {
                "document_id": document_id,
                "original_name": document_manifest["original_name"],
                "stored_name": document_manifest["stored_name"],
                "content_sha256": document_manifest["content_sha256"],
                "created_at": document_manifest["created_at"],
            }
        )
        entries.sort(key=lambda item: (str(item.get("created_at", "")), str(item["document_id"])))
        _atomic_write_json(
            manifest_path,
            {"version": MANIFEST_VERSION, "documents": entries},
            replace=replace,
        )


def _embedding_matches(stored: Any, active: dict[str, Any]) -> bool:
    if not isinstance(stored, dict):
        return False
    keys = ("provider", "model", "dimension")
    return all(stored.get(key) == active.get(key) for key in keys)


def _candidate_key(
    score: float,
    document: Document,
    *,
    ordinal: int,
) -> tuple[float, str, str, int, int]:
    metadata = document.metadata or {}
    return (
        float(score),
        str(metadata.get("source") or ""),
        str(metadata.get("document_id") or ""),
        int(metadata.get("chunk_index") or ordinal),
        ordinal,
    )


class DocumentIndexRetriever:
    def __init__(self, facade: "DocumentVectorStoreFacade", k: int) -> None:
        self.facade = facade
        self.k = max(1, int(k))

    def invoke(self, query: str, *_args: Any, **_kwargs: Any) -> list[Document]:
        return self.facade.similarity_search(query, k=self.k)

    def get_relevant_documents(self, query: str) -> list[Document]:
        return self.invoke(query)


class DocumentVectorStoreFacade:
    """Search sharded documents and the compatible legacy index sequentially."""

    def __init__(
        self,
        *,
        index_root: pathlib.Path = DOCUMENT_INDEX_DIR,
        legacy_root: pathlib.Path = LEGACY_VECTOR_STORE_DIR,
        embedding_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.index_root = pathlib.Path(index_root)
        self.legacy_root = pathlib.Path(legacy_root)
        self.embedding_factory = embedding_factory

    def as_retriever(self, *, search_kwargs: dict[str, Any] | None = None, **_kwargs: Any) -> DocumentIndexRetriever:
        kwargs = search_kwargs or {}
        return DocumentIndexRetriever(self, int(kwargs.get("k", 4)))

    def similarity_search(self, query: str, k: int = 4) -> list[Document]:
        return [document for _score, _key, document in self._search(query, k)]

    def similarity_search_with_score(
        self,
        query: str,
        k: int = 4,
    ) -> list[tuple[Document, float]]:
        return [(document, score) for score, _key, document in self._search(query, k)]

    def _search(
        self,
        query: str,
        k: int,
    ) -> list[tuple[float, tuple[float, str, str, int, int], Document]]:
        from langchain_community.vectorstores import FAISS
        from row_bot.documents import get_embedding_model_for_recall

        k = max(1, int(k))
        embedding = (
            self.embedding_factory()
            if self.embedding_factory is not None
            else get_embedding_model_for_recall()
        )
        active = active_embedding_metadata()
        corpus = read_corpus_manifest(self.index_root)
        visible_entries: list[dict[str, Any]] = []
        for entry in corpus["documents"]:
            if isinstance(entry, dict) and entry.get("document_id"):
                visible_entries.append(entry)
        name_counts: dict[str, int] = {}
        for entry in visible_entries:
            name = str(entry.get("original_name") or "")
            name_counts[name] = name_counts.get(name, 0) + 1

        candidates: list[tuple[float, tuple[float, str, str, int, int], Document]] = []

        def retain_candidate(
            candidate: tuple[float, tuple[float, str, str, int, int], Document],
        ) -> None:
            candidates.append(candidate)
            candidates.sort(key=lambda item: item[1])
            del candidates[k:]

        ordinal = 0
        for entry in visible_entries:
            document_id = str(entry["document_id"])
            document_dir = self.index_root / DOCUMENTS_DIR_NAME / document_id
            document_manifest = _read_json(document_dir / "manifest.json", {})
            if (
                not isinstance(document_manifest, dict)
                or not document_manifest.get("complete")
                or not _embedding_matches(document_manifest.get("embedding"), active)
            ):
                continue
            original_name = str(document_manifest.get("original_name") or entry.get("original_name") or "")
            source = original_name
            if name_counts.get(original_name, 0) > 1:
                ingested = str(document_manifest.get("created_at") or "")[:10]
                disambiguator = (
                    f"{ingested} · {document_id[:8]}"
                    if ingested
                    else document_id[:8]
                )
                source = f"{original_name} ({disambiguator})"
            for segment_number, segment in enumerate(document_manifest.get("segments") or []):
                segment_name = str(segment.get("name") or "")
                if not segment_name:
                    continue
                try:
                    store = FAISS.load_local(
                        str(document_dir / segment_name),
                        embeddings=embedding,
                        allow_dangerous_deserialization=True,
                    )
                    hits = store.similarity_search_with_score(query, k=k)
                except Exception:
                    logger.warning(
                        "Skipping unreadable document segment %s/%s",
                        document_id,
                        segment_name,
                        exc_info=True,
                    )
                    continue
                for chunk_number, (document, raw_score) in enumerate(hits):
                    metadata = dict(document.metadata or {})
                    metadata.update(
                        {
                            "source": source,
                            "original_name": original_name,
                            "document_id": document_id,
                            "segment": segment_number,
                            "chunk_index": metadata.get("chunk_index", chunk_number),
                        }
                    )
                    document.metadata = metadata
                    key = _candidate_key(float(raw_score), document, ordinal=ordinal)
                    retain_candidate((float(raw_score), key, document))
                    ordinal += 1
                del store

        if self.legacy_root.exists() and index_metadata_matches(self.legacy_root, active):
            try:
                tombstone_path = self.index_root / "legacy_tombstones.json"
                tombstones_value = _read_json(tombstone_path, [])
                tombstones = {
                    str(item)
                    for item in tombstones_value
                    if isinstance(tombstones_value, list)
                }
                legacy = FAISS.load_local(
                    str(self.legacy_root),
                    embeddings=embedding,
                    allow_dangerous_deserialization=True,
                )
                for document, raw_score in legacy.similarity_search_with_score(query, k=k):
                    metadata = dict(document.metadata or {})
                    if str(metadata.get("source") or "") in tombstones:
                        continue
                    metadata.setdefault("legacy", True)
                    metadata.setdefault("document_id", "")
                    document.metadata = metadata
                    key = _candidate_key(float(raw_score), document, ordinal=ordinal)
                    retain_candidate((float(raw_score), key, document))
                    ordinal += 1
                del legacy
            except Exception:
                logger.warning("Compatible legacy document index could not be queried", exc_info=True)

        return candidates

    def clear_cache(self) -> None:
        """The facade intentionally retains no loaded FAISS segments."""


def remove_document_shard(
    document_id: str,
    *,
    index_root: pathlib.Path = DOCUMENT_INDEX_DIR,
) -> bool:
    with _manifest_lock:
        manifest = read_corpus_manifest(index_root)
        entries = manifest["documents"]
        kept = [entry for entry in entries if str(entry.get("document_id")) != document_id]
        if len(kept) == len(entries):
            return False
        _atomic_write_json(
            index_root / CORPUS_MANIFEST_NAME,
            {"version": MANIFEST_VERSION, "documents": kept},
        )
        live = index_root / DOCUMENTS_DIR_NAME / document_id
        if live.exists():
            retired = index_root / "retired" / (
                f"{document_id}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}"
            )
            retired.parent.mkdir(parents=True, exist_ok=True)
            os.replace(live, retired)
        return True


def reset_sharded_index(
    *,
    index_root: pathlib.Path = DOCUMENT_INDEX_DIR,
) -> pathlib.Path | None:
    with _manifest_lock:
        retired: pathlib.Path | None = None
        if index_root.exists():
            retired = index_root.with_name(
                f"{index_root.name}.retired-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}"
            )
            os.replace(index_root, retired)
        initialize_index(index_root)
        return retired


def index_health(
    *,
    index_root: pathlib.Path = DOCUMENT_INDEX_DIR,
    legacy_root: pathlib.Path = LEGACY_VECTOR_STORE_DIR,
) -> dict[str, Any]:
    active = active_embedding_metadata()
    corpus_path = index_root / CORPUS_MANIFEST_NAME
    corpus = read_corpus_manifest(index_root)
    stale = 0
    readable = 0
    partial = 0
    for entry in corpus["documents"]:
        document_dir = index_root / DOCUMENTS_DIR_NAME / str(entry.get("document_id") or "")
        manifest = _read_json(document_dir / "manifest.json", {})
        if not manifest or not manifest.get("complete"):
            partial += 1
        elif not _embedding_matches(manifest.get("embedding"), active):
            stale += 1
        else:
            readable += 1
    visible_ids = {
        str(entry.get("document_id") or "")
        for entry in corpus["documents"]
        if isinstance(entry, dict)
    }
    documents_root = index_root / DOCUMENTS_DIR_NAME
    orphan_documents = (
        sum(
            child.is_dir() and child.name not in visible_ids
            for child in documents_root.iterdir()
        )
        if documents_root.exists()
        else 0
    )
    legacy_exists = legacy_root.exists()
    legacy_compatible = legacy_exists and index_metadata_matches(legacy_root, active)
    return {
        "exists": corpus_path.exists() or legacy_exists,
        "sharded_documents": len(corpus["documents"]),
        "readable_documents": readable,
        "stale_documents": stale,
        "partial_documents": partial,
        "orphan_documents": int(orphan_documents),
        "legacy_exists": legacy_exists,
        "legacy_compatible": legacy_compatible,
        "stale": bool(stale or (legacy_exists and not legacy_compatible)),
        "active": active,
    }
