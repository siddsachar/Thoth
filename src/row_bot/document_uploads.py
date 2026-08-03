"""Streaming, disk-first upload staging for durable document jobs."""

from __future__ import annotations

import hashlib
import inspect
import os
import pathlib
import shutil
from collections.abc import Awaitable, Callable
from typing import Any

from row_bot.document_jobs import (
    MAX_UPLOAD_BYTES,
    MIN_STAGING_FREE_BYTES,
    UPLOAD_CHUNK_BYTES,
    DocumentJob,
    DocumentJobError,
    DocumentJobService,
)


class UploadRejected(DocumentJobError):
    """A per-file staging rejection safe to display to the user."""


def _default_disk_free(path: pathlib.Path) -> int:
    return int(shutil.disk_usage(str(path)).free)


async def _read_bounded(stream: Any, size: int) -> bytes:
    read = getattr(stream, "read", None)
    if not callable(read):
        raise UploadRejected("This upload source does not support streaming reads.")
    try:
        value = read(size)
    except TypeError as exc:
        raise UploadRejected(
            "This upload source cannot be streamed safely; the file was not accepted."
        ) from exc
    if inspect.isawaitable(value):
        value = await value
    if value is None:
        return b""
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise UploadRejected("The upload source returned invalid binary data.")
    data = bytes(value)
    if len(data) > size:
        raise UploadRejected("The upload source exceeded the bounded read size.")
    return data


async def _iter_bounded_chunks(stream: Any, size: int):
    iterate = getattr(stream, "iterate", None)
    if callable(iterate):
        try:
            iterator = iterate(chunk_size=size)
        except TypeError as exc:
            raise UploadRejected(
                "This upload source cannot provide bounded streaming chunks."
            ) from exc
        async for value in iterator:
            if not isinstance(value, (bytes, bytearray, memoryview)):
                raise UploadRejected("The upload source returned invalid binary data.")
            data = bytes(value)
            if len(data) > size:
                raise UploadRejected("The upload source exceeded the bounded read size.")
            if data:
                yield data
        return
    while True:
        data = await _read_bounded(stream, size)
        if not data:
            return
        yield data


async def stage_upload(
    service: DocumentJobService,
    batch_id: str,
    sequence: int,
    original_name: str,
    stream: Any,
    *,
    declared_size: int | None = None,
    disk_free: Callable[[pathlib.Path], int] = _default_disk_free,
    max_bytes: int = MAX_UPLOAD_BYTES,
    reserve_bytes: int = MIN_STAGING_FREE_BYTES,
    chunk_bytes: int = UPLOAD_CHUNK_BYTES,
) -> DocumentJob:
    """Stream one upload into its collision-safe job directory.

    Backend size and disk checks remain authoritative even when the client
    supplies a size hint.
    """
    chunk_bytes = min(max(1, int(chunk_bytes)), UPLOAD_CHUNK_BYTES)
    max_bytes = int(max_bytes)
    reserve_bytes = int(reserve_bytes)
    if declared_size is not None and int(declared_size) > max_bytes:
        raise UploadRejected("The file exceeds the 256 MiB upload limit.")

    job = service.create_staging_job(batch_id, sequence, original_name)
    final_path = pathlib.Path(job.staged_path)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = final_path.with_name(f".{final_path.name}.uploading")
    digest = hashlib.sha256()
    size = 0

    try:
        available = int(disk_free(final_path.parent))
        expected = max(0, int(declared_size or 0))
        if available - expected < reserve_bytes:
            raise UploadRejected(
                "Not enough free disk space to stage this file while keeping the 2 GiB safety reserve."
            )

        with temp_path.open("xb") as output:
            async for data in _iter_bounded_chunks(stream, chunk_bytes):
                next_size = size + len(data)
                if next_size > max_bytes:
                    raise UploadRejected("The file exceeds the 256 MiB upload limit.")
                if int(disk_free(final_path.parent)) - len(data) < reserve_bytes:
                    raise UploadRejected(
                        "Staging stopped because the 2 GiB free-space safety reserve would be crossed."
                    )
                output.write(data)
                digest.update(data)
                size = next_size
            if size == 0:
                raise UploadRejected("The uploaded file is empty.")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_path, final_path)
        return service.complete_staging(job.id, digest.hexdigest(), size, final_path)
    except Exception as exc:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        if service.get_job(job.id).status == "staging":
            code = "upload_rejected" if isinstance(exc, UploadRejected) else "upload_failed"
            service.fail_staging(job.id, code, str(exc))
        raise
