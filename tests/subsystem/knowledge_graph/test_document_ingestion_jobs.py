from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib
import pathlib
import sqlite3

import pytest


def _service(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    import row_bot.document_jobs as document_jobs

    jobs = importlib.reload(document_jobs)
    return jobs, jobs.DocumentJobService(tmp_path / "data")


def test_queue_is_durable_and_fifo(tmp_path, monkeypatch):
    jobs, service = _service(tmp_path, monkeypatch)
    batch = service.create_batch()
    first = service.create_staging_job(batch, 0, "first.txt")
    second = service.create_staging_job(batch, 1, "second.txt")
    service.complete_staging(first.id, "a" * 64, 1, first.staged_path)
    service.complete_staging(second.id, "b" * 64, 1, second.staged_path)
    service.finish_batch_staging(batch)

    reloaded = jobs.DocumentJobService(tmp_path / "data")
    assert [job.original_name for job in reloaded.list_jobs(batch)] == [
        "first.txt",
        "second.txt",
    ]


def test_upload_stream_is_bounded_and_hashes_incrementally(tmp_path, monkeypatch):
    jobs, service = _service(tmp_path, monkeypatch)
    from row_bot.document_uploads import stage_upload

    class Stream:
        def __init__(self):
            self.calls = []
            self.parts = iter((b"abc", b"def", b""))

        async def read(self, size):
            self.calls.append(size)
            return next(self.parts)

    stream = Stream()
    batch = service.create_batch()
    job = asyncio.run(
        stage_upload(
            service,
            batch,
            0,
            "notes.txt",
            stream,
            disk_free=lambda _path: jobs.MIN_STAGING_FREE_BYTES + 1024,
        )
    )

    assert stream.calls
    assert max(stream.calls) <= jobs.UPLOAD_CHUNK_BYTES
    assert job.content_sha256 == hashlib.sha256(b"abcdef").hexdigest()


def test_illegal_job_transition_is_rejected(tmp_path, monkeypatch):
    jobs, service = _service(tmp_path, monkeypatch)
    batch = service.create_batch()
    job = service.create_staging_job(batch, 0, "notes.txt")

    with pytest.raises(jobs.InvalidJobTransition):
        service.transition_job(job.id, "completed")


def _queue_local_file(service, batch, sequence, name, content):
    job = service.create_staging_job(batch, sequence, name)
    path = pathlib.Path(job.staged_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return service.complete_staging(
        job.id,
        hashlib.sha256(content).hexdigest(),
        len(content),
        path,
    )


def test_partial_and_corrupt_schema_recover(tmp_path, monkeypatch):
    jobs, _service_instance = _service(tmp_path, monkeypatch)
    partial_dir = tmp_path / "partial"
    db_dir = partial_dir / "document_ingestion"
    db_dir.mkdir(parents=True)
    with contextlib.closing(sqlite3.connect(db_dir / "jobs.db")) as conn:
        conn.execute("CREATE TABLE document_jobs (id TEXT PRIMARY KEY)")
        conn.commit()

    partial = jobs.DocumentJobService(partial_dir)
    assert partial.create_batch()
    assert list(db_dir.glob("jobs.corrupt-*.db"))

    corrupt_dir = tmp_path / "corrupt"
    corrupt_db_dir = corrupt_dir / "document_ingestion"
    corrupt_db_dir.mkdir(parents=True)
    (corrupt_db_dir / "jobs.db").write_bytes(b"not sqlite")

    corrupt = jobs.DocumentJobService(corrupt_dir)
    assert corrupt.create_batch()
    assert list(corrupt_db_dir.glob("jobs.corrupt-*.db"))


def test_fifo_claims_index_all_documents_before_extraction(tmp_path, monkeypatch):
    _jobs, service = _service(tmp_path, monkeypatch)
    first_batch = service.create_batch()
    first = _queue_local_file(service, first_batch, 0, "one.txt", b"one")
    second = _queue_local_file(service, first_batch, 1, "two.txt", b"two")
    service.finish_batch_staging(first_batch)
    later_batch = service.create_batch()
    later = _queue_local_file(service, later_batch, 0, "three.txt", b"three")
    service.finish_batch_staging(later_batch)

    claimed = service.claim_next("owner")
    assert claimed.id == first.id
    service.mark_searchable(first.id)
    claimed = service.claim_next("owner")
    assert claimed.id == second.id
    service.mark_searchable(second.id)
    claimed = service.claim_next("owner")
    assert claimed.id == first.id
    assert claimed.status == "extracting"
    service.mark_completed(first.id)
    claimed = service.claim_next("owner")
    assert claimed.id == second.id
    service.mark_completed(second.id)
    claimed = service.claim_next("owner")
    assert claimed.id == later.id


def test_single_flight_lease_and_expiry(tmp_path, monkeypatch):
    jobs, _ = _service(tmp_path, monkeypatch)
    clock = [100.0]
    service = jobs.DocumentJobService(
        tmp_path / "lease-data",
        monotonic=lambda: clock[0],
    )
    batch = service.create_batch()
    queued = _queue_local_file(service, batch, 0, "one.txt", b"one")
    service.finish_batch_staging(batch)

    assert service.claim_next("owner-a", lease_seconds=5).id == queued.id
    assert service.claim_next("owner-a", lease_seconds=5) is None
    service.transition_job(queued.id, "queued", stage="parse")
    assert service.claim_next("owner-b", lease_seconds=5) is None
    clock[0] = 106.0
    assert service.claim_next("owner-b", lease_seconds=5).id == queued.id


def test_pause_resume_and_cancellation_persist(tmp_path, monkeypatch):
    jobs, service = _service(tmp_path, monkeypatch)
    batch = service.create_batch()
    first = _queue_local_file(service, batch, 0, "one.txt", b"one")
    second = _queue_local_file(service, batch, 1, "two.txt", b"two")
    service.finish_batch_staging(batch)

    service.pause_batch(batch, True)
    reloaded = jobs.DocumentJobService(tmp_path / "data")
    assert reloaded.get_batch(batch).pause_requested is True
    assert reloaded.claim_next("owner") is None
    reloaded.pause_batch(batch, False)
    active = reloaded.claim_next("owner")
    assert active.id == first.id
    assert reloaded.cancel_job(active.id).cancel_requested is True
    assert reloaded.cancel_job(second.id).status == "cancelled"
    assert reloaded.cancel_batch(batch).status == "cancelled"


def test_retry_failed_resets_work_and_requeues_batch(tmp_path, monkeypatch):
    _jobs, service = _service(tmp_path, monkeypatch)
    batch = service.create_batch()
    job = _queue_local_file(service, batch, 0, "one.txt", b"one")
    service.finish_batch_staging(batch)
    service.transition_job(job.id, "indexing", stage="parse")
    work = service.work_root / job.id / "index"
    work.mkdir(parents=True)
    (work / "partial").write_text("partial", encoding="utf-8")
    service.mark_failed(job.id, "parser", "failed", stage="parse")
    service.finalize_batch(batch)

    retried = service.retry_failed(job.id)

    assert retried.status == "queued"
    assert retried.error_code == ""
    assert not work.exists()
    assert service.get_batch(batch).status == "queued"


def test_clear_finished_preserves_active_batches_and_dedup_records(tmp_path, monkeypatch):
    _jobs, service = _service(tmp_path, monkeypatch)
    finished_batch = service.create_batch()
    finished = _queue_local_file(service, finished_batch, 0, "done.txt", b"done")
    service.finish_batch_staging(finished_batch)
    service.transition_job(finished.id, "indexing", stage="parse")
    service.mark_searchable(finished.id)
    service.transition_job(finished.id, "extracting", stage="knowledge_map")
    service.mark_completed(finished.id)
    service.finalize_batch(finished_batch)

    active_batch = service.create_batch()
    active = _queue_local_file(service, active_batch, 0, "active.txt", b"active")
    service.finish_batch_staging(active_batch)

    assert service.clear_finished() == 1
    assert service.get_job(active.id).status == "queued"
    assert service.list_document_records()[0]["document_id"] == finished.id


def test_recovery_restarts_index_resumes_extraction_and_fails_missing(tmp_path, monkeypatch):
    _jobs, service = _service(tmp_path, monkeypatch)
    batch = service.create_batch()
    indexing = _queue_local_file(service, batch, 0, "index.txt", b"index")
    extracting = _queue_local_file(service, batch, 1, "extract.txt", b"extract")
    missing = _queue_local_file(service, batch, 2, "missing.txt", b"missing")
    service.finish_batch_staging(batch)
    service.transition_job(indexing.id, "indexing", stage="embed")
    service.transition_job(extracting.id, "indexing", stage="index_commit")
    service.mark_searchable(extracting.id)
    service.transition_job(extracting.id, "extracting", stage="knowledge_map")
    service.store_map_summary(extracting.id, 0, "saved")
    pathlib.Path(missing.staged_path).unlink()
    partial = service.work_root / indexing.id / "index"
    partial.mkdir(parents=True)
    (partial / "partial").write_text("x", encoding="utf-8")

    result = service.recover_unfinished()

    assert result == {
        "indexing_restarted": 1,
        "extraction_resumed": 1,
        "missing_sources_failed": 1,
        "upload_temps_removed": 0,
        "orphan_directories_retired": 0,
    }
    assert service.get_job(indexing.id).status == "queued"
    assert not partial.exists()
    assert service.get_job(extracting.id).status == "searchable"
    assert service.last_map_window(extracting.id) == 0
    assert service.get_job(missing.id).error_code == "missing_staged_source"


def test_duplicate_content_skips_but_same_name_different_content_coexists(
    tmp_path,
    monkeypatch,
):
    _jobs, service = _service(tmp_path, monkeypatch)
    first_batch = service.create_batch()
    first = _queue_local_file(service, first_batch, 0, "same.txt", b"alpha")
    service.finish_batch_staging(first_batch)
    service.transition_job(first.id, "indexing", stage="parse")
    service.mark_searchable(first.id)

    second_batch = service.create_batch()
    duplicate = _queue_local_file(service, second_batch, 0, "copy.txt", b"alpha")
    different = _queue_local_file(service, second_batch, 1, "same.txt", b"beta")

    assert duplicate.status == "skipped_duplicate"
    assert different.status == "queued"
    assert first.stored_name != different.stored_name


def test_duplicate_selected_in_same_batch_is_skipped_before_second_index(
    tmp_path,
    monkeypatch,
):
    _jobs, service = _service(tmp_path, monkeypatch)
    batch = service.create_batch()
    first = _queue_local_file(service, batch, 0, "first.txt", b"identical")
    second = _queue_local_file(service, batch, 1, "second.txt", b"identical")
    service.finish_batch_staging(batch)

    assert service.claim_next("owner").id == first.id
    service.mark_searchable(first.id)
    next_job = service.claim_next("owner")

    assert service.get_job(second.id).status == "skipped_duplicate"
    assert next_job.id == first.id
    assert next_job.status == "extracting"


@pytest.mark.parametrize(
    ("name", "expected_fragment"),
    [
        ("../../escape.txt", "escape-"),
        ("CON.txt", "_CON-"),
        ("café?.txt", "café_-"),
    ],
)
def test_filename_sanitization_is_contained(
    tmp_path,
    monkeypatch,
    name,
    expected_fragment,
):
    _jobs, service = _service(tmp_path, monkeypatch)
    batch = service.create_batch()
    job = service.create_staging_job(batch, 0, name)

    assert expected_fragment in job.stored_name
    assert service.staging_root.resolve() in pathlib.Path(job.staged_path).resolve().parents


def test_upload_exact_limit_accepted_and_limit_plus_one_rejected(tmp_path, monkeypatch):
    jobs, service = _service(tmp_path, monkeypatch)
    from row_bot.document_uploads import UploadRejected, stage_upload

    class SizedStream:
        def __init__(self, sizes):
            self.sizes = iter(sizes)

        async def read(self, requested):
            size = next(self.sizes, 0)
            assert size <= requested
            return b"x" * size

    batch = service.create_batch()
    accepted = asyncio.run(
        stage_upload(
            service,
            batch,
            0,
            "limit.txt",
            SizedStream([4, 4, 0]),
            max_bytes=8,
            chunk_bytes=4,
            reserve_bytes=10,
            disk_free=lambda _path: 100,
        )
    )
    assert accepted.size_bytes == 8

    with pytest.raises(UploadRejected):
        asyncio.run(
            stage_upload(
                service,
                batch,
                1,
                "too-large.txt",
                SizedStream([4, 4, 1, 0]),
                max_bytes=8,
                chunk_bytes=4,
                reserve_bytes=10,
                disk_free=lambda _path: 100,
            )
        )


def test_upload_reserve_and_interruption_remove_only_own_temp(tmp_path, monkeypatch):
    jobs, service = _service(tmp_path, monkeypatch)
    from row_bot.document_uploads import UploadRejected, stage_upload

    class Interrupted:
        def __init__(self):
            self.calls = 0

        async def read(self, _requested):
            self.calls += 1
            if self.calls == 1:
                return b"abc"
            raise OSError("interrupted")

    batch = service.create_batch()
    sentinel = service.staging_root / "sentinel"
    sentinel.mkdir(parents=True)
    (sentinel / "keep").write_text("keep", encoding="utf-8")
    with pytest.raises(UploadRejected):
        asyncio.run(
            stage_upload(
                service,
                batch,
                0,
                "reserve.txt",
                Interrupted(),
                reserve_bytes=100,
                disk_free=lambda _path: 99,
            )
        )
    with pytest.raises(OSError):
        asyncio.run(
            stage_upload(
                service,
                batch,
                1,
                "interrupt.txt",
                Interrupted(),
                reserve_bytes=10,
                disk_free=lambda _path: 100,
            )
        )

    assert (sentinel / "keep").read_text(encoding="utf-8") == "keep"
    assert not list(service.staging_root.rglob("*.uploading"))


def test_repeated_ui_initialization_starts_one_supervisor(tmp_path, monkeypatch):
    jobs, service = _service(tmp_path, monkeypatch)
    starts = []

    class FakeSupervisor:
        def __init__(self, selected_service):
            self.service = selected_service
            self.running = False

        def start(self):
            starts.append(self.service)
            self.running = True
            return True

    monkeypatch.setattr(jobs, "DocumentSupervisor", FakeSupervisor)
    monkeypatch.setattr(jobs, "_supervisor", None)

    first = jobs.ensure_document_supervisor(service)
    second = jobs.ensure_document_supervisor(service)

    assert first is second
    assert starts == [service]


def test_restart_loaded_jobs_keep_user_visible_status(tmp_path, monkeypatch):
    jobs, service = _service(tmp_path, monkeypatch)
    from row_bot.ui.settings import document_job_status_label

    batch = service.create_batch()
    job = _queue_local_file(service, batch, 0, "queued.txt", b"queued")
    service.finish_batch_staging(batch)
    reloaded = jobs.DocumentJobService(tmp_path / "data")

    assert reloaded.get_job(job.id).status == "queued"
    assert document_job_status_label(reloaded.get_job(job.id).status) == "Queued"


def test_recovery_retires_orphans_and_health_reports_clean_state(tmp_path, monkeypatch):
    _jobs, service = _service(tmp_path, monkeypatch)
    orphan_staging = service.staging_root / "unknown"
    orphan_work = service.work_root / "unknown"
    orphan_staging.mkdir(parents=True)
    orphan_work.mkdir(parents=True)
    (orphan_staging / ".source.uploading").write_text("partial", encoding="utf-8")
    (orphan_work / "partial").write_text("partial", encoding="utf-8")

    result = service.recover_unfinished()
    health = service.health()

    assert result["upload_temps_removed"] == 1
    assert result["orphan_directories_retired"] == 2
    assert health["db_ok"] is True
    assert health["staging_orphans"] == 0
    assert health["work_orphans"] == 0
    assert len(list((service.root / "recovery_orphans").iterdir())) == 2
