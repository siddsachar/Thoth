from __future__ import annotations

import hashlib
import importlib
import pathlib
import threading
from types import SimpleNamespace

from langchain_core.documents import Document

import pytest


def test_rolling_windows_preserve_order_and_overlap(monkeypatch):
    import row_bot.document_extraction as extraction

    extraction = importlib.reload(extraction)
    pages = [
        Document(page_content="abcdefgh"),
        Document(page_content="ijklmnop"),
    ]

    windows = list(
        extraction.iter_extraction_windows(
            iter(pages),
            window_size=10,
            overlap=3,
        )
    )

    assert windows == ["abcdefghij", "hijklmnop"]


def _extracting_job(tmp_path, monkeypatch, name="notes.txt", content=b"notes"):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    import row_bot.document_jobs as document_jobs
    import row_bot.document_extraction as extraction

    jobs = importlib.reload(document_jobs)
    extraction = importlib.reload(extraction)
    service = jobs.DocumentJobService(tmp_path / "data")
    batch = service.create_batch()
    job = service.create_staging_job(batch, 0, name)
    path = pathlib.Path(job.staged_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    job = service.complete_staging(
        job.id,
        hashlib.sha256(content).hexdigest(),
        len(content),
        path,
    )
    service.finish_batch_staging(batch)
    service.transition_job(job.id, "indexing", stage="parse")
    service.mark_searchable(job.id)
    service.transition_job(job.id, "extracting", stage="knowledge_map")
    return jobs, extraction, service, service.get_job(job.id)


def _patch_extraction_side_effects(extraction, monkeypatch):
    import row_bot.documents as documents

    monkeypatch.setattr(extraction, "_copy_to_vault_raw", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(extraction, "_commit_knowledge", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(documents, "release_document_embedding_resources", lambda *_args: None)
    monkeypatch.setattr(documents, "iter_document_pages", lambda _path: iter(()))


def test_persisted_map_result_resumes_at_next_window(tmp_path, monkeypatch):
    _jobs, extraction, service, job = _extracting_job(tmp_path, monkeypatch)
    _patch_extraction_side_effects(extraction, monkeypatch)
    windows = ["window zero", "window one", "window two"]
    monkeypatch.setattr(extraction, "iter_extraction_windows", lambda _pages: iter(windows))
    calls = []
    service.store_map_summary(job.id, 0, "already summarized")
    monkeypatch.setattr(
        extraction,
        "_map_summarize_window",
        lambda text, *_args: calls.append(text) or f"summary for {text}",
    )
    monkeypatch.setattr(
        extraction,
        "_reduce_summaries",
        lambda _title, summaries: (
            "A sufficiently detailed compiled article for deterministic resume testing. "
            + " ".join(summaries)
        ),
    )

    result = extraction.extract_document_job(job, service)

    assert result["status"] == "completed"
    assert calls == ["window one", "window two"]
    assert service.last_map_window(job.id) == 2


def test_hierarchical_reduction_resumes_and_never_exceeds_group_bound(
    tmp_path,
    monkeypatch,
):
    _jobs, extraction, service, job = _extracting_job(tmp_path, monkeypatch)
    for index in range(17):
        service.store_map_summary(job.id, index, f"summary {index}")
    service.store_reduce_summary(job.id, 1, 0, "existing first group")
    group_sizes = []

    def reduce_group(_title, summaries):
        group_sizes.append(len(summaries))
        return "Reduced article content long enough for another hierarchy level. " + " ".join(summaries)

    monkeypatch.setattr(extraction, "_reduce_summaries", reduce_group)

    article = extraction._hierarchical_reduce(job, service, "Notes")
    first_call_count = len(group_sizes)
    resumed = extraction._hierarchical_reduce(job, service, "Notes")

    assert article == resumed
    assert first_call_count > 0
    assert len(group_sizes) == first_call_count
    assert max(group_sizes) <= 8
    assert service.count_reduce_summaries(job.id, 1) == 3
    assert service.count_reduce_summaries(job.id, 2) == 1


def test_cancellation_between_provider_calls_prevents_more_calls(tmp_path, monkeypatch):
    jobs, extraction, service, job = _extracting_job(tmp_path, monkeypatch)
    _patch_extraction_side_effects(extraction, monkeypatch)
    monkeypatch.setattr(
        extraction,
        "iter_extraction_windows",
        lambda _pages: iter(("one", "two", "three")),
    )
    calls = []

    def map_call(text, *_args):
        calls.append(text)
        service.cancel_job(job.id)
        return "summary"

    monkeypatch.setattr(extraction, "_map_summarize_window", map_call)

    with pytest.raises(jobs.DocumentCancelled):
        extraction.extract_document_job(job, service)

    assert calls == ["one"]


def test_provider_failure_isolated_and_next_job_remains_claimable(tmp_path, monkeypatch):
    jobs, extraction, service, first = _extracting_job(tmp_path, monkeypatch)
    _patch_extraction_side_effects(extraction, monkeypatch)
    monkeypatch.setattr(extraction, "iter_extraction_windows", lambda _pages: iter(("one",)))
    monkeypatch.setattr(
        extraction,
        "_map_summarize_window",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("provider failed")),
    )
    second_batch = service.create_batch()
    second = service.create_staging_job(second_batch, 0, "second.txt")
    second_path = pathlib.Path(second.staged_path)
    second_path.parent.mkdir(parents=True)
    second_path.write_text("second", encoding="utf-8")
    second = service.complete_staging(
        second.id,
        hashlib.sha256(b"second").hexdigest(),
        6,
        second_path,
    )
    service.finish_batch_staging(second_batch)

    with pytest.raises(RuntimeError, match="provider failed"):
        extraction.extract_document_job(first, service)
    service.mark_failed(first.id, "knowledge_map_failed", "provider failed", stage="knowledge_map")
    service.finalize_batch(first.batch_id)

    assert service.claim_next("owner").id == second.id


def test_wiki_raw_copy_uses_stored_name_and_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    import row_bot.document_extraction as extraction
    import row_bot.wiki_vault as wiki_vault

    extraction = importlib.reload(extraction)
    wiki_vault = importlib.reload(wiki_vault)
    wiki_vault.set_vault_path(str(tmp_path / "vault"))
    wiki_vault.set_enabled(True)
    source = tmp_path / "source.txt"
    source.write_text("content", encoding="utf-8")

    copied = extraction._copy_to_vault_raw(
        str(source),
        "source-a1b2c3.txt",
        original_name="source.txt",
        document_id="doc-1",
    )

    assert copied == tmp_path / "vault" / "raw" / "source-a1b2c3.txt"
    metadata = (tmp_path / "vault" / "raw" / ".metadata" / "source-a1b2c3.txt.json").read_text(
        encoding="utf-8"
    )
    assert '"document_id": "doc-1"' in metadata
    assert '"original_name": "source.txt"' in metadata


def test_graph_and_wiki_finalization_occurs_once_per_batch(tmp_path, monkeypatch):
    jobs, extraction, service, job = _extracting_job(tmp_path, monkeypatch)
    service.mark_completed(job.id)
    calls = []
    monkeypatch.setattr(jobs, "_finalize_shared_knowledge_indexes", lambda: calls.append("refresh"))
    monkeypatch.setattr(jobs, "_notify_batch_complete", lambda *_args: None)
    supervisor = jobs.DocumentSupervisor(service)

    supervisor._finalize_ready_batches()
    supervisor._finalize_ready_batches()

    assert calls == ["refresh"]
    assert service.get_batch(job.batch_id).status == "completed"


def test_document_hub_commit_is_idempotent_by_document_id(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    import row_bot.knowledge_graph as knowledge_graph
    import row_bot.memory as memory
    import row_bot.memory_evolution as memory_evolution
    import row_bot.document_extraction as extraction

    kg = importlib.reload(knowledge_graph)
    importlib.reload(memory)
    importlib.reload(memory_evolution)
    extraction = importlib.reload(extraction)
    kg._skip_reindex = True
    monkeypatch.setattr(extraction, "_extract_from_summary", lambda *_args: [])
    job = SimpleNamespace(
        id="document-id",
        original_name="same.txt",
        stored_name="same-document.txt",
    )
    article = "A sufficiently detailed article for an idempotent document knowledge hub."

    extraction._commit_knowledge(job, article, window_count=1, summary_count=1)
    extraction._commit_knowledge(job, article, window_count=1, summary_count=1)

    conn = kg._get_conn()
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM entities WHERE source='document:document-id' AND entity_type='media'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 1
