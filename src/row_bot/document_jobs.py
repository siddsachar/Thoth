"""Durable, FIFO coordination for the bounded document-ingestion pipeline."""

from __future__ import annotations

import contextlib
import logging
import os
import pathlib
import re
import shutil
import sqlite3
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable, Iterator

from row_bot.data_paths import get_row_bot_data_dir

logger = logging.getLogger(__name__)

UPLOAD_CHUNK_BYTES = 1024 * 1024
MAX_UPLOAD_BYTES = 256 * 1024 * 1024
MIN_STAGING_FREE_BYTES = 2 * 1024 * 1024 * 1024
EMBEDDING_BATCH_SIZE = 32
INDEX_SEGMENT_CHUNKS = 2_000
REDUCTION_GROUP_SIZE = 8
LEASE_SECONDS = 90

JOB_STATUSES = {
    "staging",
    "queued",
    "indexing",
    "searchable",
    "extracting",
    "completed",
    "failed",
    "cancelled",
    "skipped_duplicate",
}
JOB_STAGES = {
    "upload",
    "parse",
    "embed",
    "index_commit",
    "knowledge_map",
    "knowledge_reduce",
    "knowledge_commit",
    "finalize",
}
BATCH_STATUSES = {
    "staging",
    "queued",
    "running",
    "paused",
    "completed",
    "completed_with_errors",
    "cancelled",
}
TERMINAL_JOB_STATUSES = {"completed", "failed", "cancelled", "skipped_duplicate"}
ACTIVE_JOB_STATUSES = {"indexing", "extracting"}

_JOB_TRANSITIONS = {
    "staging": {"queued", "failed", "cancelled", "skipped_duplicate"},
    "queued": {"indexing", "failed", "cancelled"},
    "indexing": {"queued", "searchable", "failed", "cancelled"},
    "searchable": {"extracting", "completed", "failed", "cancelled"},
    "extracting": {"searchable", "completed", "failed", "cancelled"},
    "completed": set(),
    "failed": {"queued"},
    "cancelled": set(),
    "skipped_duplicate": set(),
}

_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_UNSAFE_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class DocumentJobError(RuntimeError):
    """Base class for durable ingestion errors."""


class InvalidJobTransition(DocumentJobError):
    """Raised when a caller requests an illegal state transition."""


class DocumentCancelled(DocumentJobError):
    """Raised at a pipeline cancellation checkpoint."""


@dataclass(frozen=True)
class DocumentBatch:
    id: str
    created_at: str
    updated_at: str
    status: str
    pause_requested: bool
    cancel_requested: bool


@dataclass(frozen=True)
class DocumentJob:
    id: str
    batch_id: str
    sequence: int
    original_name: str
    stored_name: str
    staged_path: str
    content_sha256: str
    size_bytes: int
    extension: str
    status: str
    stage: str
    index_progress_current: int
    index_progress_total: int
    extraction_progress_current: int
    extraction_progress_total: int
    attempt: int
    cancel_requested: bool
    error_code: str
    error_message: str
    created_at: str
    updated_at: str
    started_at: str
    searchable_at: str
    completed_at: str


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def sanitize_original_name(original_name: str) -> str:
    """Return a cross-platform, basename-only upload name."""
    normalized = unicodedata.normalize("NFC", str(original_name or ""))
    normalized = normalized.replace("\\", "/").split("/")[-1].strip()
    normalized = _UNSAFE_FILENAME.sub("_", normalized)
    normalized = re.sub(r"_+", "_", normalized).strip(" ._")
    if not normalized:
        normalized = "document"
    path = pathlib.Path(normalized)
    stem = path.stem[:120].rstrip(" .") or "document"
    suffix = path.suffix[:20].lower()
    if stem.upper() in _WINDOWS_RESERVED_NAMES:
        stem = f"_{stem}"
    return f"{stem}{suffix}"


def collision_safe_stored_name(original_name: str, job_id: str) -> str:
    safe = pathlib.Path(sanitize_original_name(original_name))
    suffix = safe.suffix.lower()
    stem = safe.stem[:96] or "document"
    return f"{stem}-{job_id[:10]}{suffix}"


def _row_job(row: sqlite3.Row) -> DocumentJob:
    values = dict(row)
    values["cancel_requested"] = bool(values["cancel_requested"])
    return DocumentJob(**values)


def _row_batch(row: sqlite3.Row) -> DocumentBatch:
    values = dict(row)
    values["pause_requested"] = bool(values["pause_requested"])
    values["cancel_requested"] = bool(values["cancel_requested"])
    return DocumentBatch(**values)


class DocumentJobService:
    """Own queue persistence, validated transitions, recovery, and controls."""

    def __init__(
        self,
        data_dir: str | pathlib.Path | None = None,
        *,
        now: Callable[[], str] = utc_now,
        monotonic: Callable[[], float] = time.time,
    ) -> None:
        self.data_dir = pathlib.Path(data_dir or get_row_bot_data_dir())
        self.root = self.data_dir / "document_ingestion"
        self.staging_root = self.root / "staging"
        self.work_root = self.root / "work"
        self.completed_root = self.root / "completed"
        self.db_path = self.root / "jobs.db"
        self._now = now
        self._monotonic = monotonic
        self._write_lock = threading.RLock()
        self.root.mkdir(parents=True, exist_ok=True)
        self.staging_root.mkdir(parents=True, exist_ok=True)
        self.work_root.mkdir(parents=True, exist_ok=True)
        self.completed_root.mkdir(parents=True, exist_ok=True)
        self._initialize_or_recover_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _initialize_or_recover_schema(self) -> None:
        try:
            self._initialize_schema()
        except sqlite3.DatabaseError as exc:
            corrupt = self.db_path.with_name(
                f"jobs.corrupt-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}.db"
            )
            logger.error("Recovering corrupt document-ingestion DB to %s: %s", corrupt, exc)
            if self.db_path.exists():
                os.replace(self.db_path, corrupt)
            for suffix in ("-wal", "-shm"):
                sidecar = pathlib.Path(f"{self.db_path}{suffix}")
                with contextlib.suppress(FileNotFoundError):
                    sidecar.unlink()
            self._initialize_schema()

    def _initialize_schema(self) -> None:
        with contextlib.closing(self._connect()) as conn, conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS document_batches (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    pause_requested INTEGER NOT NULL DEFAULT 0,
                    cancel_requested INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS document_jobs (
                    id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    original_name TEXT NOT NULL,
                    stored_name TEXT NOT NULL,
                    staged_path TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    extension TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    index_progress_current INTEGER NOT NULL DEFAULT 0,
                    index_progress_total INTEGER NOT NULL DEFAULT 0,
                    extraction_progress_current INTEGER NOT NULL DEFAULT 0,
                    extraction_progress_total INTEGER NOT NULL DEFAULT 0,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT NOT NULL DEFAULT '',
                    searchable_at TEXT NOT NULL DEFAULT '',
                    completed_at TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(batch_id) REFERENCES document_batches(id) ON DELETE CASCADE
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_document_jobs_batch_sequence
                    ON document_jobs(batch_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_document_jobs_status_fifo
                    ON document_jobs(status, sequence, created_at);

                CREATE TABLE IF NOT EXISTS document_records (
                    document_id TEXT PRIMARY KEY,
                    original_name TEXT NOT NULL,
                    stored_name TEXT NOT NULL,
                    staged_path TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    searchable_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_document_records_hash
                    ON document_records(content_sha256);

                CREATE TABLE IF NOT EXISTS document_worker_leases (
                    name TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    heartbeat_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS document_map_summaries (
                    job_id TEXT NOT NULL,
                    window_index INTEGER NOT NULL,
                    summary TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(job_id, window_index),
                    FOREIGN KEY(job_id) REFERENCES document_jobs(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS document_reduce_summaries (
                    job_id TEXT NOT NULL,
                    level INTEGER NOT NULL,
                    group_index INTEGER NOT NULL,
                    summary TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(job_id, level, group_index),
                    FOREIGN KEY(job_id) REFERENCES document_jobs(id) ON DELETE CASCADE
                );
                """
            )
            conn.execute("PRAGMA user_version=1")
            integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
            if integrity != "ok":
                raise sqlite3.DatabaseError(f"document ingestion integrity check failed: {integrity}")

    def create_batch(self) -> str:
        batch_id = uuid.uuid4().hex
        now = self._now()
        with contextlib.closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO document_batches
                    (id, created_at, updated_at, status, pause_requested, cancel_requested)
                VALUES (?, ?, ?, 'staging', 0, 0)
                """,
                (batch_id, now, now),
            )
        return batch_id

    def create_staging_job(
        self,
        batch_id: str,
        sequence: int,
        original_name: str,
    ) -> DocumentJob:
        job_id = uuid.uuid4().hex
        safe_name = sanitize_original_name(original_name)
        stored_name = collision_safe_stored_name(safe_name, job_id)
        staged_path = self.staging_root / job_id / stored_name
        now = self._now()
        with contextlib.closing(self._connect()) as conn, conn:
            batch = conn.execute(
                "SELECT status FROM document_batches WHERE id=?", (batch_id,)
            ).fetchone()
            if batch is None or batch["status"] != "staging":
                raise DocumentJobError("Uploads can only be added while a batch is staging")
            conn.execute(
                """
                INSERT INTO document_jobs (
                    id, batch_id, sequence, original_name, stored_name, staged_path,
                    content_sha256, size_bytes, extension, status, stage,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, '', 0, ?, 'staging', 'upload', ?, ?)
                """,
                (
                    job_id,
                    batch_id,
                    int(sequence),
                    str(original_name or safe_name),
                    stored_name,
                    str(staged_path),
                    pathlib.Path(safe_name).suffix.lower(),
                    now,
                    now,
                ),
            )
        return self.get_job(job_id)

    def complete_staging(
        self,
        job_id: str,
        content_sha256: str,
        size_bytes: int,
        staged_path: str | pathlib.Path,
    ) -> DocumentJob:
        now = self._now()
        duplicate = False
        with self._write_lock, contextlib.closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status FROM document_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            if row["status"] != "staging":
                raise InvalidJobTransition(f"{row['status']} -> queued")
            duplicate = conn.execute(
                "SELECT 1 FROM document_records WHERE content_sha256=? LIMIT 1",
                (content_sha256,),
            ).fetchone() is not None
            status = "skipped_duplicate" if duplicate else "queued"
            completed_at = now if duplicate else ""
            conn.execute(
                """
                UPDATE document_jobs
                SET content_sha256=?, size_bytes=?, staged_path=?, status=?,
                    updated_at=?, completed_at=?, error_code='', error_message=''
                WHERE id=?
                """,
                (
                    content_sha256,
                    int(size_bytes),
                    str(staged_path),
                    status,
                    now,
                    completed_at,
                    job_id,
                ),
            )
            conn.commit()
        if duplicate:
            with contextlib.suppress(FileNotFoundError):
                pathlib.Path(staged_path).unlink()
        return self.get_job(job_id)

    def fail_staging(self, job_id: str, code: str, message: str) -> DocumentJob:
        return self.transition_job(
            job_id,
            "failed",
            stage="upload",
            error_code=code,
            error_message=message,
        )

    def finish_batch_staging(self, batch_id: str) -> DocumentBatch:
        with self._write_lock, contextlib.closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            batch = conn.execute(
                "SELECT * FROM document_batches WHERE id=?", (batch_id,)
            ).fetchone()
            if batch is None:
                raise KeyError(batch_id)
            if batch["status"] != "staging":
                conn.commit()
                return _row_batch(batch)
            rows = conn.execute(
                "SELECT status FROM document_jobs WHERE batch_id=?", (batch_id,)
            ).fetchall()
            statuses = {row["status"] for row in rows}
            if "staging" in statuses:
                raise DocumentJobError("Cannot queue a batch with unfinished uploads")
            status = self._terminal_batch_status(statuses) if not (statuses - TERMINAL_JOB_STATUSES) else "queued"
            now = self._now()
            conn.execute(
                "UPDATE document_batches SET status=?, updated_at=? WHERE id=?",
                (status, now, batch_id),
            )
            conn.commit()
        _wake_supervisor()
        return self.get_batch(batch_id)

    def get_job(self, job_id: str) -> DocumentJob:
        with contextlib.closing(self._connect()) as conn, conn:
            row = conn.execute("SELECT * FROM document_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return _row_job(row)

    def get_batch(self, batch_id: str) -> DocumentBatch:
        with contextlib.closing(self._connect()) as conn, conn:
            row = conn.execute(
                "SELECT * FROM document_batches WHERE id=?", (batch_id,)
            ).fetchone()
        if row is None:
            raise KeyError(batch_id)
        return _row_batch(row)

    def list_jobs(self, batch_id: str | None = None) -> list[DocumentJob]:
        query = "SELECT * FROM document_jobs"
        params: tuple[object, ...] = ()
        if batch_id is not None:
            query += " WHERE batch_id=?"
            params = (batch_id,)
        query += " ORDER BY created_at, batch_id, sequence"
        with contextlib.closing(self._connect()) as conn, conn:
            rows = conn.execute(query, params).fetchall()
        return [_row_job(row) for row in rows]

    def list_batches(self, *, include_finished: bool = True) -> list[DocumentBatch]:
        query = "SELECT * FROM document_batches"
        if not include_finished:
            query += " WHERE status NOT IN ('completed','completed_with_errors','cancelled')"
        query += " ORDER BY created_at, id"
        with contextlib.closing(self._connect()) as conn, conn:
            rows = conn.execute(query).fetchall()
        return [_row_batch(row) for row in rows]

    def list_document_records(self) -> list[dict[str, object]]:
        with contextlib.closing(self._connect()) as conn, conn:
            rows = conn.execute(
                "SELECT * FROM document_records ORDER BY searchable_at, document_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def remove_document_record(self, document_id: str) -> bool:
        """Remove one durable searchable-record row by document ID."""
        with contextlib.closing(self._connect()) as conn, conn:
            cur = conn.execute(
                "DELETE FROM document_records WHERE document_id=?",
                (document_id,),
            )
        return bool(cur.rowcount)

    def clear_document_records(self) -> int:
        """Clear searchable-record metadata after indexes are retired."""
        with contextlib.closing(self._connect()) as conn, conn:
            count = int(conn.execute("SELECT COUNT(*) FROM document_records").fetchone()[0])
            conn.execute("DELETE FROM document_records")
        return count

    def cancel_all_batches(self) -> int:
        """Persist cancellation for all unfinished batches."""
        with contextlib.closing(self._connect()) as conn, conn:
            rows = conn.execute(
                """
                SELECT id FROM document_batches
                WHERE status NOT IN ('completed','completed_with_errors','cancelled')
                """
            ).fetchall()
        for row in rows:
            self.cancel_batch(str(row["id"]))
        return len(rows)

    def retire_document_source(self, document_id: str) -> pathlib.Path | None:
        """Move one retained source to a recoverable retired directory."""
        with contextlib.closing(self._connect()) as conn, conn:
            row = conn.execute(
                "SELECT staged_path FROM document_records WHERE document_id=?",
                (document_id,),
            ).fetchone()
        if row is None:
            return None
        source = pathlib.Path(str(row["staged_path"]))
        if not source.exists():
            return None
        retired = self.root / "retired" / document_id / source.name
        retired.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, retired)
        return retired

    def retire_all_document_sources(self) -> int:
        """Recoverably retire every retained source before a clear-all."""
        records = self.list_document_records()
        retired = 0
        for record in records:
            if self.retire_document_source(str(record["document_id"])) is not None:
                retired += 1
        return retired

    def health(self) -> dict[str, object]:
        """Return read-only database and interrupted-work health details."""
        db_ok = False
        error = ""
        known_ids: set[str] = set()
        missing_sources = 0
        try:
            with contextlib.closing(self._connect()) as conn, conn:
                db_ok = conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
                rows = conn.execute(
                    "SELECT id, status, staged_path FROM document_jobs"
                ).fetchall()
            known_ids = {str(row["id"]) for row in rows}
            missing_sources = sum(
                not pathlib.Path(str(row["staged_path"])).is_file()
                for row in rows
                if row["status"] in {
                    "staging",
                    "queued",
                    "indexing",
                    "searchable",
                    "extracting",
                }
            )
        except Exception as exc:
            error = str(exc)
        staging_orphans = sum(
            child.is_dir() and child.name not in known_ids
            for child in self.staging_root.iterdir()
        )
        work_orphans = sum(
            child.is_dir()
            and child.name not in known_ids
            and not child.name.startswith("rebuild-")
            for child in self.work_root.iterdir()
        )
        return {
            "db_ok": db_ok,
            "error": error,
            "missing_sources": int(missing_sources),
            "staging_orphans": int(staging_orphans),
            "work_orphans": int(work_orphans),
        }

    def transition_job(
        self,
        job_id: str,
        new_status: str,
        *,
        stage: str | None = None,
        error_code: str = "",
        error_message: str = "",
    ) -> DocumentJob:
        if new_status not in JOB_STATUSES:
            raise InvalidJobTransition(f"Unknown status: {new_status}")
        if stage is not None and stage not in JOB_STAGES:
            raise DocumentJobError(f"Unknown document stage: {stage}")
        with self._write_lock, contextlib.closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM document_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            current = row["status"]
            if new_status != current and new_status not in _JOB_TRANSITIONS[current]:
                raise InvalidJobTransition(f"{current} -> {new_status}")
            now = self._now()
            started_at = row["started_at"]
            searchable_at = row["searchable_at"]
            completed_at = row["completed_at"]
            if new_status in ACTIVE_JOB_STATUSES and not started_at:
                started_at = now
            if new_status == "searchable" and not searchable_at:
                searchable_at = now
            if new_status in TERMINAL_JOB_STATUSES:
                completed_at = now
            conn.execute(
                """
                UPDATE document_jobs
                SET status=?, stage=COALESCE(?, stage), updated_at=?,
                    started_at=?, searchable_at=?, completed_at=?,
                    error_code=?, error_message=?
                WHERE id=?
                """,
                (
                    new_status,
                    stage,
                    now,
                    started_at,
                    searchable_at,
                    completed_at,
                    error_code,
                    error_message,
                    job_id,
                ),
            )
            conn.commit()
        return self.get_job(job_id)

    def update_progress(
        self,
        job_id: str,
        *,
        stage: str,
        current: int,
        total: int = 0,
    ) -> None:
        if stage not in JOB_STAGES:
            raise DocumentJobError(f"Unknown document stage: {stage}")
        prefix = "index" if stage in {"parse", "embed", "index_commit"} else "extraction"
        with contextlib.closing(self._connect()) as conn, conn:
            conn.execute(
                f"""
                UPDATE document_jobs
                SET stage=?, {prefix}_progress_current=?, {prefix}_progress_total=?,
                    updated_at=?
                WHERE id=?
                """,
                (stage, max(0, int(current)), max(0, int(total)), self._now(), job_id),
            )

    def mark_searchable(self, job_id: str) -> DocumentJob:
        job = self.transition_job(job_id, "searchable", stage="index_commit")
        with contextlib.closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO document_records (
                    document_id, original_name, stored_name, staged_path,
                    content_sha256, size_bytes, searchable_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, '')
                ON CONFLICT(document_id) DO UPDATE SET
                    original_name=excluded.original_name,
                    stored_name=excluded.stored_name,
                    staged_path=excluded.staged_path,
                    content_sha256=excluded.content_sha256,
                    size_bytes=excluded.size_bytes,
                    searchable_at=excluded.searchable_at
                """,
                (
                    job.id,
                    job.original_name,
                    job.stored_name,
                    job.staged_path,
                    job.content_sha256,
                    job.size_bytes,
                    job.searchable_at,
                ),
            )
        return job

    def mark_completed(self, job_id: str) -> DocumentJob:
        job = self.transition_job(job_id, "completed", stage="finalize")
        source = pathlib.Path(job.staged_path)
        completed_path = self.completed_root / job.id / job.stored_name
        if source.is_file() and source != completed_path:
            completed_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, completed_path)
            with contextlib.suppress(OSError):
                source.parent.rmdir()
            with contextlib.closing(self._connect()) as conn, conn:
                conn.execute(
                    "UPDATE document_jobs SET staged_path=? WHERE id=?",
                    (str(completed_path), job_id),
                )
            job = self.get_job(job_id)
        with contextlib.closing(self._connect()) as conn, conn:
            conn.execute(
                """
                UPDATE document_records
                SET completed_at=?, staged_path=?
                WHERE document_id=?
                """,
                (job.completed_at, job.staged_path, job_id),
            )
        return job

    def mark_failed(
        self,
        job_id: str,
        code: str,
        message: str,
        *,
        stage: str | None = None,
    ) -> DocumentJob:
        return self.transition_job(
            job_id,
            "failed",
            stage=stage,
            error_code=code,
            error_message=message[:2000],
        )

    def pause_batch(self, batch_id: str, paused: bool = True) -> DocumentBatch:
        with contextlib.closing(self._connect()) as conn, conn:
            row = conn.execute(
                "SELECT status FROM document_batches WHERE id=?", (batch_id,)
            ).fetchone()
            if row is None:
                raise KeyError(batch_id)
            if row["status"] in {"completed", "completed_with_errors", "cancelled"}:
                return self.get_batch(batch_id)
            status = "paused" if paused else "queued"
            conn.execute(
                """
                UPDATE document_batches
                SET pause_requested=?, status=?, updated_at=?
                WHERE id=?
                """,
                (int(paused), status, self._now(), batch_id),
            )
        if not paused:
            _wake_supervisor()
        return self.get_batch(batch_id)

    def cancel_job(self, job_id: str) -> DocumentJob:
        with self._write_lock, contextlib.closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status FROM document_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            status = row["status"]
            now = self._now()
            immediate = status in {"staging", "queued", "searchable"}
            conn.execute(
                """
                UPDATE document_jobs
                SET cancel_requested=1, status=?, updated_at=?, completed_at=?
                WHERE id=?
                """,
                ("cancelled" if immediate else status, now, now if immediate else "", job_id),
            )
            conn.commit()
        _wake_supervisor()
        return self.get_job(job_id)

    def cancel_batch(self, batch_id: str) -> DocumentBatch:
        now = self._now()
        with self._write_lock, contextlib.closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE document_batches
                SET cancel_requested=1, pause_requested=0, status='cancelled', updated_at=?
                WHERE id=?
                """,
                (now, batch_id),
            )
            conn.execute(
                """
                UPDATE document_jobs
                SET cancel_requested=1,
                    status=CASE
                        WHEN status IN ('staging','queued','searchable') THEN 'cancelled'
                        ELSE status
                    END,
                    completed_at=CASE
                        WHEN status IN ('staging','queued','searchable') THEN ?
                        ELSE completed_at
                    END,
                    updated_at=?
                WHERE batch_id=? AND status NOT IN ('completed','failed','cancelled','skipped_duplicate')
                """,
                (now, now, batch_id),
            )
            conn.commit()
        _wake_supervisor()
        return self.get_batch(batch_id)

    def should_cancel(self, job_id: str) -> bool:
        with contextlib.closing(self._connect()) as conn, conn:
            row = conn.execute(
                """
                SELECT j.cancel_requested AS job_cancel, b.cancel_requested AS batch_cancel
                FROM document_jobs j
                JOIN document_batches b ON b.id=j.batch_id
                WHERE j.id=?
                """,
                (job_id,),
            ).fetchone()
        return row is None or bool(row["job_cancel"] or row["batch_cancel"])

    def raise_if_cancelled(self, job_id: str) -> None:
        if self.should_cancel(job_id):
            raise DocumentCancelled(f"Document {job_id} was cancelled")

    def retry_failed(self, job_id: str) -> DocumentJob:
        job = self.get_job(job_id)
        if job.status != "failed":
            raise InvalidJobTransition(f"{job.status} -> queued")
        if not pathlib.Path(job.staged_path).is_file():
            raise DocumentJobError("The staged source is missing; upload the document again.")
        with contextlib.closing(self._connect()) as conn, conn:
            searchable_record = conn.execute(
                "SELECT 1 FROM document_records WHERE document_id=?",
                (job_id,),
            ).fetchone()
        retry_status = "searchable" if searchable_record else "queued"
        retry_stage = "knowledge_map" if searchable_record else "parse"
        if not searchable_record:
            work_dir = self.work_root / job.id
            self._remove_owned_tree(work_dir, self.work_root)
        with contextlib.closing(self._connect()) as conn, conn:
            conn.execute(
                f"""
                UPDATE document_jobs
                SET status='{retry_status}', stage='{retry_stage}', cancel_requested=0,
                    index_progress_current=0, index_progress_total=0,
                    error_code='', error_message='', completed_at='', updated_at=?
                WHERE id=?
                """,
                (self._now(), job_id),
            )
            conn.execute(
                """
                UPDATE document_batches
                SET status='queued', cancel_requested=0, pause_requested=0, updated_at=?
                WHERE id=?
                """,
                (self._now(), job.batch_id),
            )
        _wake_supervisor()
        return self.get_job(job_id)

    def clear_finished(self) -> int:
        with self._write_lock, contextlib.closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT id FROM document_batches
                WHERE status IN ('completed','completed_with_errors','cancelled')
                  AND NOT EXISTS (
                      SELECT 1 FROM document_jobs
                      WHERE batch_id=document_batches.id
                        AND status IN ('staging','queued','indexing','searchable','extracting')
                  )
                """
            ).fetchall()
            batch_ids = [row["id"] for row in rows]
            job_rows = []
            for batch_id in batch_ids:
                job_rows.extend(
                    conn.execute(
                        "SELECT id, status FROM document_jobs WHERE batch_id=?",
                        (batch_id,),
                    ).fetchall()
                )
                conn.execute("DELETE FROM document_batches WHERE id=?", (batch_id,))
            conn.commit()
        for row in job_rows:
            self._remove_owned_tree(self.work_root / row["id"], self.work_root)
            self._remove_owned_tree(self.staging_root / row["id"], self.staging_root)
        return len(batch_ids)

    def claim_next(self, owner: str, *, lease_seconds: int = LEASE_SECONDS) -> DocumentJob | None:
        now_epoch = self._monotonic()
        skipped_duplicate_paths: list[pathlib.Path] = []
        with self._write_lock, contextlib.closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            lease = conn.execute(
                "SELECT owner, expires_at FROM document_worker_leases WHERE name='coordinator'"
            ).fetchone()
            if lease and lease["owner"] != owner and float(lease["expires_at"]) > now_epoch:
                conn.rollback()
                return None
            conn.execute(
                """
                INSERT INTO document_worker_leases(name, owner, heartbeat_at, expires_at)
                VALUES ('coordinator', ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    owner=excluded.owner,
                    heartbeat_at=excluded.heartbeat_at,
                    expires_at=excluded.expires_at
                """,
                (owner, now_epoch, now_epoch + lease_seconds),
            )
            active = conn.execute(
                """
                SELECT 1 FROM document_jobs
                WHERE status IN ('indexing','extracting')
                LIMIT 1
                """
            ).fetchone()
            if active is not None:
                conn.commit()
                return None
            batches = conn.execute(
                """
                SELECT * FROM document_batches
                WHERE status IN ('queued','running')
                  AND pause_requested=0 AND cancel_requested=0
                ORDER BY created_at, id
                """
            ).fetchall()
            for batch in batches:
                while True:
                    queued = conn.execute(
                        """
                        SELECT * FROM document_jobs
                        WHERE batch_id=? AND status='queued' AND cancel_requested=0
                        ORDER BY sequence, created_at
                        LIMIT 1
                        """,
                        (batch["id"],),
                    ).fetchone()
                    if queued is None:
                        break
                    duplicate = conn.execute(
                        """
                        SELECT 1 FROM document_records
                        WHERE content_sha256=? LIMIT 1
                        """,
                        (queued["content_sha256"],),
                    ).fetchone()
                    if duplicate is None:
                        break
                    now = self._now()
                    conn.execute(
                        """
                        UPDATE document_jobs
                        SET status='skipped_duplicate', stage='finalize',
                            completed_at=?, updated_at=?
                        WHERE id=?
                        """,
                        (now, now, queued["id"]),
                    )
                    skipped_duplicate_paths.append(
                        pathlib.Path(str(queued["staged_path"]))
                    )
                candidate = queued
                new_status = "indexing"
                stage = "parse"
                if candidate is None:
                    candidate = conn.execute(
                        """
                        SELECT * FROM document_jobs
                        WHERE batch_id=? AND status='searchable' AND cancel_requested=0
                        ORDER BY sequence, created_at
                        LIMIT 1
                        """,
                        (batch["id"],),
                    ).fetchone()
                    new_status = "extracting"
                    stage = "knowledge_map"
                if candidate is None:
                    self._finalize_batch_in_connection(conn, batch["id"])
                    continue
                now = self._now()
                conn.execute(
                    """
                    UPDATE document_jobs
                    SET status=?, stage=?, attempt=attempt+1,
                        started_at=CASE WHEN started_at='' THEN ? ELSE started_at END,
                        updated_at=?
                    WHERE id=?
                    """,
                    (new_status, stage, now, now, candidate["id"]),
                )
                conn.execute(
                    "UPDATE document_batches SET status='running', updated_at=? WHERE id=?",
                    (now, batch["id"]),
                )
                claimed = conn.execute(
                    "SELECT * FROM document_jobs WHERE id=?", (candidate["id"],)
                ).fetchone()
                conn.commit()
                for path in skipped_duplicate_paths:
                    with contextlib.suppress(FileNotFoundError):
                        path.unlink()
                return _row_job(claimed)
            conn.commit()
        for path in skipped_duplicate_paths:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
        return None

    def heartbeat(self, owner: str, *, lease_seconds: int = LEASE_SECONDS) -> bool:
        now = self._monotonic()
        with contextlib.closing(self._connect()) as conn, conn:
            cur = conn.execute(
                """
                UPDATE document_worker_leases
                SET heartbeat_at=?, expires_at=?
                WHERE name='coordinator' AND owner=?
                """,
                (now, now + lease_seconds, owner),
            )
        return bool(cur.rowcount)

    def release_lease(self, owner: str) -> None:
        with contextlib.closing(self._connect()) as conn, conn:
            conn.execute(
                "DELETE FROM document_worker_leases WHERE name='coordinator' AND owner=?",
                (owner,),
            )

    def recover_unfinished(self) -> dict[str, int]:
        recovered_indexing = 0
        recovered_extracting = 0
        missing = 0
        with self._write_lock, contextlib.closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM document_worker_leases WHERE expires_at<=?",
                (self._monotonic(),),
            )
            rows = conn.execute(
                """
                SELECT * FROM document_jobs
                WHERE status IN ('queued','indexing','searchable','extracting')
                """
            ).fetchall()
            for row in rows:
                source_exists = pathlib.Path(row["staged_path"]).is_file()
                if not source_exists:
                    conn.execute(
                        """
                        UPDATE document_jobs
                        SET status='failed', error_code='missing_staged_source',
                            error_message=?, completed_at=?, updated_at=?
                        WHERE id=?
                        """,
                        (
                            "The staged source is missing. Remove this entry and upload the document again.",
                            self._now(),
                            self._now(),
                            row["id"],
                        ),
                    )
                    missing += 1
                    continue
                if row["status"] == "indexing":
                    conn.execute(
                        "UPDATE document_jobs SET status='queued', stage='parse', updated_at=? WHERE id=?",
                        (self._now(), row["id"]),
                    )
                    recovered_indexing += 1
                elif row["status"] == "extracting":
                    conn.execute(
                        """
                        UPDATE document_jobs
                        SET status='searchable', stage='knowledge_map', updated_at=?
                        WHERE id=?
                        """,
                        (self._now(), row["id"]),
                    )
                    recovered_extracting += 1
            conn.commit()
        for row in rows:
            if row["status"] == "indexing":
                self._remove_owned_tree(self.work_root / row["id"] / "index", self.work_root)
        removed_temps = 0
        for temp in self.staging_root.glob("*/*.uploading"):
            with contextlib.suppress(FileNotFoundError):
                temp.unlink()
                removed_temps += 1
        known_ids = {job.id for job in self.list_jobs()}
        recovered_orphans = 0
        orphan_root = self.root / "recovery_orphans"
        for owner, label in (
            (self.staging_root, "staging"),
            (self.work_root, "work"),
        ):
            for child in owner.iterdir():
                if (
                    not child.is_dir()
                    or child.name in known_ids
                    or child.name.startswith("rebuild-")
                ):
                    continue
                destination = orphan_root / (
                    f"{label}-{child.name}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%f')}"
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(child, destination)
                recovered_orphans += 1
        return {
            "indexing_restarted": recovered_indexing,
            "extraction_resumed": recovered_extracting,
            "missing_sources_failed": missing,
            "upload_temps_removed": removed_temps,
            "orphan_directories_retired": recovered_orphans,
        }

    def active_summary(self) -> dict[str, object]:
        with contextlib.closing(self._connect()) as conn, conn:
            active = conn.execute(
                """
                SELECT j.*, b.pause_requested AS batch_paused
                FROM document_jobs j
                JOIN document_batches b ON b.id=j.batch_id
                WHERE j.status IN ('indexing','extracting')
                ORDER BY j.started_at, j.sequence
                LIMIT 1
                """
            ).fetchone()
            remaining = conn.execute(
                """
                SELECT COUNT(*) FROM document_jobs
                WHERE status IN ('staging','queued','indexing','searchable','extracting')
                """
            ).fetchone()[0]
            paused = conn.execute(
                "SELECT COUNT(*) FROM document_batches WHERE pause_requested=1"
            ).fetchone()[0]
        return {
            "active": dict(active) if active else None,
            "remaining": int(remaining),
            "paused": bool(paused),
        }

    def store_map_summary(self, job_id: str, window_index: int, summary: str) -> None:
        with contextlib.closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO document_map_summaries
                    (job_id, window_index, summary, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (job_id, int(window_index), summary, self._now()),
            )

    def iter_map_summaries(self, job_id: str) -> Iterator[tuple[int, str]]:
        with contextlib.closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                """
                SELECT window_index, summary FROM document_map_summaries
                WHERE job_id=? ORDER BY window_index
                """,
                (job_id,),
            )
            while True:
                rows = cursor.fetchmany(32)
                if not rows:
                    break
                for row in rows:
                    yield int(row["window_index"]), str(row["summary"])

    def last_map_window(self, job_id: str) -> int:
        with contextlib.closing(self._connect()) as conn, conn:
            value = conn.execute(
                "SELECT MAX(window_index) FROM document_map_summaries WHERE job_id=?",
                (job_id,),
            ).fetchone()[0]
        return int(value) if value is not None else -1

    def store_reduce_summary(
        self,
        job_id: str,
        level: int,
        group_index: int,
        summary: str,
    ) -> None:
        with contextlib.closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO document_reduce_summaries
                    (job_id, level, group_index, summary, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (job_id, int(level), int(group_index), summary, self._now()),
            )

    def iter_reduce_summaries(self, job_id: str, level: int) -> Iterator[tuple[int, str]]:
        with contextlib.closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                """
                SELECT group_index, summary FROM document_reduce_summaries
                WHERE job_id=? AND level=? ORDER BY group_index
                """,
                (job_id, int(level)),
            )
            while True:
                rows = cursor.fetchmany(32)
                if not rows:
                    break
                for row in rows:
                    yield int(row["group_index"]), str(row["summary"])

    def count_reduce_summaries(self, job_id: str, level: int) -> int:
        with contextlib.closing(self._connect()) as conn, conn:
            return int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM document_reduce_summaries
                    WHERE job_id=? AND level=?
                    """,
                    (job_id, int(level)),
                ).fetchone()[0]
            )

    def finalizable_batches(self) -> list[str]:
        with contextlib.closing(self._connect()) as conn, conn:
            rows = conn.execute(
                """
                SELECT b.id
                FROM document_batches b
                WHERE b.status IN ('queued','running')
                  AND NOT EXISTS (
                      SELECT 1 FROM document_jobs j
                      WHERE j.batch_id=b.id
                        AND j.status IN ('staging','queued','indexing','searchable','extracting')
                  )
                ORDER BY b.created_at, b.id
                """
            ).fetchall()
        return [str(row["id"]) for row in rows]

    def finalize_batch(self, batch_id: str) -> DocumentBatch:
        with self._write_lock, contextlib.closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            self._finalize_batch_in_connection(conn, batch_id)
            conn.commit()
        return self.get_batch(batch_id)

    def _finalize_batch_in_connection(self, conn: sqlite3.Connection, batch_id: str) -> None:
        statuses = {
            row["status"]
            for row in conn.execute(
                "SELECT status FROM document_jobs WHERE batch_id=?", (batch_id,)
            ).fetchall()
        }
        if statuses - TERMINAL_JOB_STATUSES:
            return
        status = self._terminal_batch_status(statuses)
        conn.execute(
            "UPDATE document_batches SET status=?, updated_at=? WHERE id=?",
            (status, self._now(), batch_id),
        )

    @staticmethod
    def _terminal_batch_status(statuses: set[str]) -> str:
        if statuses and statuses <= {"cancelled"}:
            return "cancelled"
        if "failed" in statuses or "cancelled" in statuses:
            return "completed_with_errors"
        return "completed"

    @staticmethod
    def _remove_owned_tree(path: pathlib.Path, owner: pathlib.Path) -> None:
        path = path.resolve()
        owner = owner.resolve()
        if path == owner or owner not in path.parents:
            raise DocumentJobError(f"Refusing cleanup outside {owner}")
        if path.exists():
            shutil.rmtree(path)


class _SupervisorProcessLock:
    def __init__(self, path: pathlib.Path) -> None:
        self.path = path
        self.handle = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        self.handle.seek(0)
        if self.handle.read(1) == b"":
            self.handle.seek(0)
            self.handle.write(b"0")
            self.handle.flush()
        try:
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self.handle.close()
            self.handle = None
            return False

    def release(self) -> None:
        if self.handle is None:
            return
        with contextlib.suppress(OSError):
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()
        self.handle = None


class DocumentSupervisor:
    """Single-flight background coordinator for sequential heavy operations."""

    def __init__(self, service: DocumentJobService) -> None:
        self.service = service
        self.owner = f"{os.getpid()}-{uuid.uuid4().hex}"
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._process_lock = _SupervisorProcessLock(service.root / "supervisor.lock")

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> bool:
        if self.running:
            return False
        if not self._process_lock.acquire():
            logger.info("Document supervisor already owns the process lock")
            return False
        recovery = self.service.recover_unfinished()
        if any(recovery.values()):
            logger.info("Recovered unfinished document ingestion: %s", recovery)
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="row-bot-document-ingestion",
        )
        self._thread.start()
        return True

    def wake(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                job = self.service.claim_next(self.owner)
                if job is None:
                    self._finalize_ready_batches()
                    self._wake.wait(30)
                    self._wake.clear()
                    continue
                try:
                    if job.status == "indexing":
                        from row_bot.documents import index_document_job

                        index_document_job(job, self.service)
                        self.service.raise_if_cancelled(job.id)
                        self.service.mark_searchable(job.id)
                    elif job.status == "extracting":
                        from row_bot.document_extraction import extract_document_job

                        extract_document_job(job, self.service)
                        self.service.raise_if_cancelled(job.id)
                        self.service.mark_completed(job.id)
                except DocumentCancelled:
                    current = self.service.get_job(job.id)
                    if current.status in ACTIVE_JOB_STATUSES:
                        self.service.transition_job(
                            job.id,
                            "cancelled",
                            stage=current.stage,
                            error_code="cancelled",
                            error_message="Cancelled by the user.",
                        )
                except Exception as exc:
                    logger.exception("Document ingestion failed for %s", job.original_name)
                    current = self.service.get_job(job.id)
                    if current.status in ACTIVE_JOB_STATUSES:
                        self.service.mark_failed(
                            job.id,
                            f"{current.stage}_failed",
                            str(exc),
                            stage=current.stage,
                        )
                finally:
                    self.service.heartbeat(self.owner)
                    self._finalize_ready_batches()
        finally:
            self.service.release_lease(self.owner)
            self._process_lock.release()

    def _finalize_ready_batches(self) -> None:
        for batch_id in self.service.finalizable_batches():
            try:
                _finalize_shared_knowledge_indexes()
            except Exception:
                logger.warning(
                    "Document batch final consistency refresh failed for %s",
                    batch_id,
                    exc_info=True,
                )
            batch = self.service.finalize_batch(batch_id)
            _notify_batch_complete(self.service, batch)


def _finalize_shared_knowledge_indexes() -> None:
    import row_bot.knowledge_graph as kg

    kg._skip_reindex = False
    kg.rebuild_index()
    try:
        import row_bot.wiki_vault as wiki_vault

        if wiki_vault.is_enabled():
            wiki_vault.rebuild_vault()
    except Exception:
        logger.debug("Document batch wiki consistency refresh skipped", exc_info=True)


def _notify_batch_complete(service: DocumentJobService, batch: DocumentBatch) -> None:
    try:
        from row_bot.notifications import notify

        jobs = service.list_jobs(batch.id)
        completed = sum(job.status == "completed" for job in jobs)
        failed = sum(job.status in {"failed", "cancelled"} for job in jobs)
        duplicates = sum(job.status == "skipped_duplicate" for job in jobs)
        notify(
            "Document Ingestion",
            f"{completed} complete, {failed} failed or cancelled, {duplicates} duplicate skipped",
            icon="📄",
        )
    except Exception:
        logger.debug("Document batch completion notification skipped", exc_info=True)


_supervisor_lock = threading.Lock()
_supervisor: DocumentSupervisor | None = None


def ensure_document_supervisor(
    service: DocumentJobService | None = None,
) -> DocumentSupervisor:
    """Return the one process-local supervisor, starting it when necessary."""
    global _supervisor
    with _supervisor_lock:
        if _supervisor is None:
            _supervisor = DocumentSupervisor(service or DocumentJobService())
            _supervisor.start()
        elif not _supervisor.running:
            _supervisor.start()
        return _supervisor


def _wake_supervisor() -> None:
    supervisor = _supervisor
    if supervisor is not None:
        supervisor.wake()


def get_document_job_service() -> DocumentJobService:
    """Create a lightweight service facade over the active data directory."""
    return DocumentJobService()
