"""Bounded document parsing and compatibility access to document retrieval."""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import json
import logging
import os
import pathlib
import shutil
import threading
import uuid
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from typing import Any

from langchain_core.documents import Document

from row_bot.data_paths import get_row_bot_data_dir
from row_bot.document_jobs import EMBEDDING_BATCH_SIZE, DocumentJob, DocumentJobService
from row_bot.embedding_config import (
    active_embedding_metadata,
    describe_active_embedding,
    get_embedding_config,
    index_metadata_matches,
    read_index_metadata,
)
from row_bot.embedding_providers import (
    ensure_embedding_runtime_available,
    get_embedding_provider,
    get_embedding_provider_for_recall,
    release_embedding_resources,
)

logger = logging.getLogger(__name__)

DATA_DIR = get_row_bot_data_dir()
DATA_DIR.mkdir(parents=True, exist_ok=True)
PROCESSED_FILES_PATH = DATA_DIR / "processed_files.json"
VECTOR_STORE_DIR = DATA_DIR / "vector_store"  # read-only legacy compatibility
DOCUMENT_INDEX_DIR = DATA_DIR / "document_index"

CHUNK_SIZE = 1_500
CHUNK_OVERLAP = 150
TEXT_PAGE_CHARS = 64 * 1_024
TEXT_ENCODING_SAMPLE_BYTES = 64 * 1_024

_processed_files_lock = threading.RLock()
_embedding_lock = threading.Lock()
_vector_store = None


def load_processed_files() -> set[str]:
    """Return legacy names plus display names from durable document records."""
    processed: set[str] = set()
    if PROCESSED_FILES_PATH.exists():
        try:
            value = json.loads(PROCESSED_FILES_PATH.read_text(encoding="utf-8"))
            if isinstance(value, list):
                processed.update(str(item) for item in value)
        except (json.JSONDecodeError, OSError, TypeError):
            logger.warning("Ignoring unreadable legacy processed-files metadata")
    try:
        service = DocumentJobService(DATA_DIR)
        processed.update(
            str(record["original_name"]) for record in service.list_document_records()
        )
    except Exception:
        logger.debug("Durable document records unavailable", exc_info=True)
    return processed


def save_processed_file(file_path: str) -> None:
    """Atomically add a legacy processed-file marker."""
    with _processed_files_lock:
        processed = load_processed_files()
        processed.add(str(file_path))
        temp = PROCESSED_FILES_PATH.with_name(f".{PROCESSED_FILES_PATH.name}.tmp")
        temp.write_text(
            json.dumps(sorted(processed), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(temp, PROCESSED_FILES_PATH)


def is_file_processed(file_path: str) -> bool:
    return str(file_path) in load_processed_files()


def clear_processed_files() -> None:
    with contextlib.suppress(FileNotFoundError):
        PROCESSED_FILES_PATH.unlink()


def _text_encoding(path: pathlib.Path) -> str:
    with path.open("rb") as handle:
        sample = handle.read(TEXT_ENCODING_SAMPLE_BYTES)
    if sample.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return "cp1252"
    return "utf-8"


class _BoundedTextLoader:
    """Small compatibility loader whose lazy path never reads the whole file."""

    def __init__(self, path: str) -> None:
        self.path = pathlib.Path(path)

    def lazy_load(self) -> Iterator[Document]:
        encoding = _text_encoding(self.path)
        with self.path.open(
            "r",
            encoding=encoding,
            errors="replace",
            newline=None,
        ) as handle:
            page = 0
            while True:
                content = handle.read(TEXT_PAGE_CHARS)
                if not content:
                    return
                yield Document(
                    page_content=content,
                    metadata={"source": str(self.path), "page": page},
                )
                page += 1

    def load(self) -> list[Document]:
        """Retain the upstream loader API for legacy direct callers."""
        return list(self.lazy_load())


def _upstream_loader(module_name: str, class_name: str):
    def build(path: str):
        try:
            module = importlib.import_module(module_name)
            loader_class = getattr(module, class_name)
        except Exception as exc:
            raise RuntimeError(
                f"{class_name} is unavailable; install its optional parser dependencies."
            ) from exc
        return loader_class(path)

    return build


class DocumentLoader:
    supported_file_types = {
        ".pdf": _upstream_loader(
            "langchain_community.document_loaders.pdf",
            "PyPDFLoader",
        ),
        ".docx": _upstream_loader(
            "langchain_community.document_loaders.word_document",
            "UnstructuredWordDocumentLoader",
        ),
        ".doc": _upstream_loader(
            "langchain_community.document_loaders.word_document",
            "UnstructuredWordDocumentLoader",
        ),
        ".txt": _BoundedTextLoader,
        ".md": _BoundedTextLoader,
        ".html": _upstream_loader(
            "langchain_community.document_loaders.html_bs",
            "BSHTMLLoader",
        ),
        ".htm": _upstream_loader(
            "langchain_community.document_loaders.html_bs",
            "BSHTMLLoader",
        ),
        ".epub": _upstream_loader(
            "langchain_community.document_loaders.epub",
            "UnstructuredEPubLoader",
        ),
    }


def iter_document_pages(path: str | pathlib.Path) -> Iterator[Document]:
    """Yield non-empty pages, preferring an upstream loader's lazy API."""
    source = pathlib.Path(path)
    extension = source.suffix.lower()
    loader_class = DocumentLoader.supported_file_types.get(extension)
    if loader_class is None:
        raise ValueError(f"Unsupported file type: {extension}")
    loader = loader_class(str(source))
    lazy_load = getattr(loader, "lazy_load", None)
    pages = lazy_load() if callable(lazy_load) else iter(loader.load())
    for page in pages:
        content = getattr(page, "page_content", None)
        if not isinstance(content, str) or not content.strip():
            continue
        clean = content.encode("utf-8", errors="surrogatepass").decode(
            "utf-8", errors="replace"
        )
        yield Document(
            page_content=clean,
            metadata=dict(getattr(page, "metadata", {}) or {}),
        )


def _iter_page_chunks(
    text: str,
    *,
    chunk_size: int = CHUNK_SIZE,
    overlap: int = CHUNK_OVERLAP,
) -> Iterator[str]:
    if chunk_size <= 0 or overlap < 0 or overlap >= chunk_size:
        raise ValueError("Invalid document chunk bounds")
    start = 0
    length = len(text)
    while start < length:
        end = min(length, start + chunk_size)
        chunk = text[start:end]
        if chunk.strip():
            yield chunk
        if end >= length:
            break
        start = end - overlap


def iter_document_chunks(
    path: str | pathlib.Path,
    metadata: dict[str, Any] | None = None,
    *,
    check_cancelled=None,
) -> Iterator[Document]:
    """Split and yield one bounded chunk at a time without page/chunk lists."""
    base_metadata = dict(metadata or {})
    chunk_index = 0
    for page_index, page in enumerate(iter_document_pages(path)):
        if check_cancelled:
            check_cancelled()
        page_metadata = dict(page.metadata or {})
        page_metadata.update(base_metadata)
        page_metadata.setdefault("page", page_index)
        for content in _iter_page_chunks(page.page_content):
            if check_cancelled:
                check_cancelled()
            chunk_metadata = dict(page_metadata)
            chunk_metadata["chunk_index"] = chunk_index
            yield Document(page_content=content, metadata=chunk_metadata)
            chunk_index += 1


def iter_chunk_batches(
    chunks: Iterable[Document],
    batch_size: int = EMBEDDING_BATCH_SIZE,
) -> Iterator[list[Document]]:
    """Yield fixed embedding batches; the reviewed maximum is always 32."""
    bounded_size = min(max(1, int(batch_size)), EMBEDDING_BATCH_SIZE)
    batch: list[Document] = []
    for chunk in chunks:
        batch.append(chunk)
        if len(batch) >= bounded_size:
            yield batch
            batch = []
    if batch:
        yield batch


def get_embedding_model():
    with _embedding_lock:
        ensure_embedding_runtime_available()
        return get_embedding_provider()


def get_embedding_model_for_recall():
    return get_embedding_provider_for_recall()


def get_vector_store():
    """Return the shard/legacy compatibility facade used by document search."""
    global _vector_store
    if _vector_store is None:
        from row_bot.document_index import DocumentVectorStoreFacade

        _vector_store = DocumentVectorStoreFacade(
            index_root=DOCUMENT_INDEX_DIR,
            legacy_root=VECTOR_STORE_DIR,
        )
    return _vector_store


def index_document_job(job: DocumentJob, service: DocumentJobService) -> None:
    """Build and transactionally publish one durable job's document shards."""
    from row_bot.document_index import build_unpublished_document, publish_document

    service.raise_if_cancelled(job.id)
    service.update_progress(job.id, stage="parse", current=0, total=0)
    metadata = {
        "source": job.original_name,
        "original_name": job.original_name,
        "stored_name": job.stored_name,
        "document_id": job.id,
        "content_sha256": job.content_sha256,
    }
    chunks = iter_document_chunks(
        job.staged_path,
        metadata,
        check_cancelled=lambda: service.raise_if_cancelled(job.id),
    )
    work_document_dir = service.work_root / job.id / "index" / "document"
    manifest = build_unpublished_document(
        document_id=job.id,
        original_name=job.original_name,
        stored_name=job.stored_name,
        content_sha256=job.content_sha256,
        chunks=chunks,
        work_document_dir=work_document_dir,
        embedding=get_embedding_model(),
        check_cancelled=lambda: service.raise_if_cancelled(job.id),
        progress=lambda current: service.update_progress(
            job.id,
            stage="embed",
            current=current,
            total=0,
        ),
        embedding_metadata=active_embedding_metadata(),
    )
    service.raise_if_cancelled(job.id)
    service.update_progress(
        job.id,
        stage="index_commit",
        current=int(manifest["chunk_count"]),
        total=int(manifest["chunk_count"]),
    )
    publish_document(
        work_document_dir,
        manifest,
        index_root=DOCUMENT_INDEX_DIR,
    )


def _copy_existing_file_into_job(
    service: DocumentJobService,
    job: DocumentJob,
    source: pathlib.Path,
) -> DocumentJob:
    final = pathlib.Path(job.staged_path)
    final.parent.mkdir(parents=True, exist_ok=True)
    temp = final.with_name(f".{final.name}.copying")
    digest = hashlib.sha256()
    size = 0
    try:
        with source.open("rb") as reader, temp.open("xb") as writer:
            while True:
                data = reader.read(1024 * 1024)
                if not data:
                    break
                writer.write(data)
                digest.update(data)
                size += len(data)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temp, final)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            temp.unlink()
        raise
    return service.complete_staging(job.id, digest.hexdigest(), size, final)


def load_and_vectorize_document(
    file_path: str,
    skip_if_processed: bool = True,
    display_name: str | None = None,
) -> None:
    """Bounded synchronous compatibility entry point for older callers."""
    record_name = display_name or file_path
    if skip_if_processed and is_file_processed(record_name):
        logger.info("Skipping already processed file: %s", record_name)
        return
    service = DocumentJobService(DATA_DIR)
    batch_id = service.create_batch()
    job = service.create_staging_job(batch_id, 0, record_name)
    try:
        job = _copy_existing_file_into_job(service, job, pathlib.Path(file_path))
        service.finish_batch_staging(batch_id)
        if job.status == "skipped_duplicate":
            return
        service.transition_job(job.id, "indexing", stage="parse")
        index_document_job(service.get_job(job.id), service)
        service.mark_searchable(job.id)
        save_processed_file(record_name)
    except Exception as exc:
        current = service.get_job(job.id)
        if current.status not in {"failed", "cancelled", "skipped_duplicate"}:
            service.mark_failed(job.id, "compatibility_index_failed", str(exc), stage=current.stage)
        raise


def load_document_text(file_path: str) -> tuple[str, str]:
    """Legacy compatibility helper; the durable extraction path does not use it."""
    parts = (page.page_content for page in iter_document_pages(file_path))
    full_text = "\n\n".join(parts)
    if not full_text:
        raise ValueError(f"No text content found in: {file_path}")
    return full_text, pathlib.Path(file_path).stem


def document_vector_status() -> dict[str, Any]:
    from row_bot.document_index import index_health

    health = index_health(
        index_root=DOCUMENT_INDEX_DIR,
        legacy_root=VECTOR_STORE_DIR,
    )
    health.update(
        {
            "stored": read_index_metadata(VECTOR_STORE_DIR),
            "active_label": describe_active_embedding(),
        }
    )
    return health


def release_document_embedding_resources(
    reason: str = "document work complete",
) -> None:
    global _vector_store
    if _vector_store is not None:
        with contextlib.suppress(Exception):
            _vector_store.clear_cache()
    if reason != "embedding settings changed" and not get_embedding_config().get(
        "auto_unload", False
    ):
        return
    _vector_store = None
    release_embedding_resources(reason)


def _recoverable_retire(path: pathlib.Path, label: str) -> pathlib.Path | None:
    if not path.exists():
        return None
    retired = path.with_name(
        f"{path.name}.{label}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}"
    )
    os.replace(path, retired)
    return retired


def reset_vector_store() -> None:
    """Retire document indexes recoverably and initialize an empty shard set."""
    global _vector_store
    from row_bot.document_index import reset_sharded_index

    service = DocumentJobService(DATA_DIR)
    service.cancel_all_batches()
    service.retire_all_document_sources()
    service.clear_document_records()
    clear_processed_files()
    reset_sharded_index(index_root=DOCUMENT_INDEX_DIR)
    _recoverable_retire(VECTOR_STORE_DIR, "retired")
    _vector_store = None


def remove_document(document_id: str) -> bool:
    """Remove one durable document by ID, or tombstone one legacy display name."""
    from row_bot.document_index import remove_document_shard

    service = DocumentJobService(DATA_DIR)
    records = {
        str(record["document_id"]): record
        for record in service.list_document_records()
    }
    if document_id in records:
        record = records[document_id]
        removed = remove_document_shard(document_id, index_root=DOCUMENT_INDEX_DIR)
        service.retire_document_source(document_id)
        service.remove_document_record(document_id)
        try:
            import row_bot.wiki_vault as wiki_vault

            raw = wiki_vault.get_vault_path() / "raw" / str(record["stored_name"])
            with contextlib.suppress(FileNotFoundError):
                raw.unlink()
        except Exception:
            logger.debug("Document raw-copy cleanup skipped", exc_info=True)
        return removed

    # Legacy stores remain read-only. A source tombstone excludes a removed
    # legacy name without mutating or partially rewriting the FAISS files.
    tombstones = DATA_DIR / "document_index" / "legacy_tombstones.json"
    values = _read_name_list(tombstones)
    values.add(document_id)
    tombstones.parent.mkdir(parents=True, exist_ok=True)
    temp = tombstones.with_name(f".{tombstones.name}.tmp")
    temp.write_text(json.dumps(sorted(values), indent=2), encoding="utf-8")
    os.replace(temp, tombstones)
    processed = load_processed_files()
    if document_id in processed:
        processed.discard(document_id)
        PROCESSED_FILES_PATH.write_text(
            json.dumps(sorted(processed), indent=2), encoding="utf-8"
        )
        return True
    return False


def _read_name_list(path: pathlib.Path) -> set[str]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return {str(item) for item in value} if isinstance(value, list) else set()
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError):
        return set()


def _hash_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            data = handle.read(1024 * 1024)
            if not data:
                break
            digest.update(data)
    return digest.hexdigest()


def rebuild_vector_store_from_vault() -> int:
    """Boundedly rebuild all vault raw copies into an atomic shard directory."""
    from row_bot.document_index import (
        build_unpublished_document,
        initialize_index,
        publish_document,
    )

    try:
        import row_bot.wiki_vault as wiki_vault

        raw_dir = wiki_vault.get_vault_path() / "raw"
    except Exception:
        raw_dir = DATA_DIR / "vault" / "raw"
    if not raw_dir.exists():
        raise FileNotFoundError("No vault/raw document copies were found to rebuild from.")

    rebuild_id = uuid.uuid4().hex
    temp_root = DOCUMENT_INDEX_DIR.with_name(f"{DOCUMENT_INDEX_DIR.name}.rebuild-{rebuild_id}")
    temp_work = DATA_DIR / "document_ingestion" / "work" / f"rebuild-{rebuild_id}"
    initialize_index(temp_root)
    indexed = 0
    try:
        for path in sorted(raw_dir.iterdir(), key=lambda item: item.name.casefold()):
            if not path.is_file() or path.suffix.lower() not in DocumentLoader.supported_file_types:
                continue
            content_hash = _hash_file(path)
            document_id = uuid.uuid5(uuid.NAMESPACE_URL, f"row-bot:{path.name}:{content_hash}").hex
            original_name = path.name
            metadata_path = raw_dir / ".metadata" / f"{path.name}.json"
            metadata = {}
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError):
                pass
            original_name = str(metadata.get("original_name") or original_name)
            chunks = iter_document_chunks(
                path,
                {
                    "source": original_name,
                    "original_name": original_name,
                    "stored_name": path.name,
                    "document_id": document_id,
                    "content_sha256": content_hash,
                },
            )
            work_document_dir = temp_work / document_id
            try:
                manifest = build_unpublished_document(
                    document_id=document_id,
                    original_name=original_name,
                    stored_name=path.name,
                    content_sha256=content_hash,
                    chunks=chunks,
                    work_document_dir=work_document_dir,
                    embedding=get_embedding_model(),
                    embedding_metadata=active_embedding_metadata(),
                )
            except ValueError:
                logger.warning("No valid text content found in vault copy: %s", path)
                continue
            publish_document(work_document_dir, manifest, index_root=temp_root)
            indexed += 1
        if indexed == 0:
            raise ValueError("No valid text content was found in vault/raw documents.")

        backup = None
        if DOCUMENT_INDEX_DIR.exists():
            backup = DOCUMENT_INDEX_DIR.with_name(
                f"{DOCUMENT_INDEX_DIR.name}.rebuild-backup-{rebuild_id}"
            )
            os.replace(DOCUMENT_INDEX_DIR, backup)
        try:
            os.replace(temp_root, DOCUMENT_INDEX_DIR)
        except Exception:
            if backup is not None and backup.exists() and not DOCUMENT_INDEX_DIR.exists():
                os.replace(backup, DOCUMENT_INDEX_DIR)
            raise
        _recoverable_retire(VECTOR_STORE_DIR, "legacy-backup")
        clear_processed_files()
        global _vector_store
        _vector_store = None
        return indexed
    finally:
        if temp_root.exists():
            shutil.rmtree(temp_root)
        if temp_work.exists():
            shutil.rmtree(temp_work)
        release_document_embedding_resources("document vector rebuild")
