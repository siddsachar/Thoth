from __future__ import annotations

import importlib
import json
import pathlib

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings

import pytest


def test_chunk_batches_never_exceed_reviewed_bound(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    import row_bot.documents as documents

    documents = importlib.reload(documents)
    chunks = (Document(page_content=str(i)) for i in range(101))
    batches = list(documents.iter_chunk_batches(chunks))

    assert sum(map(len, batches)) == 101
    assert max(map(len, batches)) <= 32


def test_page_iterator_prefers_lazy_loader(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    import row_bot.documents as documents

    documents = importlib.reload(documents)
    source = tmp_path / "notes.fake"
    source.write_text("unused", encoding="utf-8")
    calls = []

    class Loader:
        def __init__(self, _path):
            pass

        def lazy_load(self):
            calls.append("lazy")
            yield Document(page_content="one")
            yield Document(page_content="two")

        def load(self):
            raise AssertionError("eager load must not be used when lazy_load exists")

    monkeypatch.setitem(documents.DocumentLoader.supported_file_types, ".fake", Loader)

    assert [page.page_content for page in documents.iter_document_pages(source)] == [
        "one",
        "two",
    ]
    assert calls == ["lazy"]


def test_page_iterator_adapts_eager_loader_and_rejects_empty_or_unsupported(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    import row_bot.documents as documents

    documents = importlib.reload(documents)
    source = tmp_path / "notes.fake"
    source.write_text("unused", encoding="utf-8")

    class EagerLoader:
        def __init__(self, _path):
            pass

        def load(self):
            return [
                Document(page_content=""),
                Document(page_content="bounded fallback"),
            ]

    monkeypatch.setitem(
        documents.DocumentLoader.supported_file_types,
        ".fake",
        EagerLoader,
    )

    assert [page.page_content for page in documents.iter_document_pages(source)] == [
        "bounded fallback"
    ]
    empty = tmp_path / "empty.txt"
    empty.write_bytes(b"")
    assert list(documents.iter_document_chunks(empty)) == []
    with pytest.raises(ValueError, match="Unsupported file type"):
        list(documents.iter_document_pages(tmp_path / "notes.unsupported"))


def test_text_loader_reads_fixed_pages_and_resource_release_clears_cache(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    import row_bot.documents as documents

    documents = importlib.reload(documents)
    monkeypatch.setattr(documents, "TEXT_PAGE_CHARS", 8)
    source = tmp_path / "notes.txt"
    source.write_text("0123456789abcdefghi", encoding="utf-8")
    pages = list(documents.iter_document_pages(source))

    assert [len(page.page_content) for page in pages] == [8, 8, 3]

    class Facade:
        cleared = False

        def clear_cache(self):
            self.cleared = True

    facade = Facade()
    released = []
    monkeypatch.setattr(documents, "_vector_store", facade)
    monkeypatch.setattr(documents, "get_embedding_config", lambda: {"auto_unload": True})
    monkeypatch.setattr(
        documents,
        "release_embedding_resources",
        lambda reason: released.append(reason),
    )

    documents.release_document_embedding_resources("bounded-test")

    assert facade.cleared is True
    assert documents._vector_store is None
    assert released == ["bounded-test"]


class FakeEmbeddings(Embeddings):
    def __init__(self):
        self.batch_sizes = []

    def embed_documents(self, texts):
        self.batch_sizes.append(len(texts))
        return [[float(len(text)), float(index % 7), 1.0] for index, text in enumerate(texts)]

    def embed_query(self, text):
        return [float(len(text)), 0.0, 1.0]


def test_segment_rollover_and_embedding_batches_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    import row_bot.document_index as document_index

    document_index = importlib.reload(document_index)
    embedding = FakeEmbeddings()
    work = tmp_path / "work" / "doc"
    chunks = (
        Document(page_content=f"chunk-{index}", metadata={"chunk_index": index})
        for index in range(2001)
    )

    manifest = document_index.build_unpublished_document(
        document_id="doc-1",
        original_name="notes.txt",
        stored_name="notes-doc1.txt",
        content_sha256="a" * 64,
        chunks=chunks,
        work_document_dir=work,
        embedding=embedding,
        embedding_metadata={"provider": "fake", "model": "fake", "dimension": 3},
    )

    assert [segment["chunk_count"] for segment in manifest["segments"]] == [2000, 1]
    assert max(embedding.batch_sizes) <= 32


def test_cancellation_during_build_removes_unpublished_work(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    import row_bot.document_index as document_index

    document_index = importlib.reload(document_index)
    calls = 0

    def cancel():
        nonlocal calls
        calls += 1
        if calls >= 2:
            from row_bot.document_jobs import DocumentCancelled

            raise DocumentCancelled("stop")

    work = tmp_path / "work" / "doc"
    with pytest.raises(Exception, match="stop"):
        document_index.build_unpublished_document(
            document_id="doc-1",
            original_name="notes.txt",
            stored_name="notes-doc1.txt",
            content_sha256="a" * 64,
            chunks=(Document(page_content=str(i)) for i in range(100)),
            work_document_dir=work,
            embedding=FakeEmbeddings(),
            check_cancelled=cancel,
            embedding_metadata={"provider": "fake", "model": "fake", "dimension": 3},
        )

    assert not work.exists()


def test_manifest_replace_failure_keeps_old_corpus_visible(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    import row_bot.document_index as document_index

    document_index = importlib.reload(document_index)
    root = tmp_path / "index"
    document_index.initialize_index(root)
    old = {
        "version": 1,
        "documents": [{"document_id": "old", "created_at": "1"}],
    }
    document_index._atomic_write_json(root / "manifest.json", old)
    work = tmp_path / "work" / "new"
    work.mkdir(parents=True)
    (work / "manifest.json").write_text("{}", encoding="utf-8")
    manifest = {
        "document_id": "new",
        "original_name": "new.txt",
        "stored_name": "new-new.txt",
        "content_sha256": "b" * 64,
        "created_at": "2",
    }

    with pytest.raises(OSError):
        document_index.publish_document(
            work,
            manifest,
            index_root=root,
            replace=lambda _src, _dst: (_ for _ in ()).throw(OSError("replace failed")),
        )

    assert document_index.read_corpus_manifest(root) == old


def _write_fake_document_manifest(root, document_id, name, embedding, segments):
    root.mkdir(parents=True, exist_ok=True)
    corpus_path = root / "manifest.json"
    corpus = json.loads(corpus_path.read_text()) if corpus_path.exists() else {
        "version": 1,
        "documents": [],
    }
    corpus["documents"].append(
        {
            "document_id": document_id,
            "original_name": name,
            "stored_name": name,
            "content_sha256": document_id * 8,
            "created_at": f"2026-01-0{len(corpus['documents']) + 1}T00:00:00",
        }
    )
    corpus_path.write_text(json.dumps(corpus), encoding="utf-8")
    doc_dir = root / "documents" / document_id
    doc_dir.mkdir(parents=True)
    (doc_dir / "manifest.json").write_text(
        json.dumps(
            {
                "complete": True,
                "document_id": document_id,
                "original_name": name,
                "stored_name": name,
                "embedding": embedding,
                "created_at": "2026-01-01T00:00:00",
                "segments": [{"name": segment} for segment in segments],
            }
        ),
        encoding="utf-8",
    )
    for segment in segments:
        (doc_dir / segment).mkdir()


def test_deterministic_top_k_stale_exclusion_and_legacy_merge(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    import langchain_community.vectorstores
    import row_bot.document_index as document_index

    document_index = importlib.reload(document_index)
    active = {"provider": "fake", "model": "v1", "dimension": 3}
    root = tmp_path / "index"
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    _write_fake_document_manifest(root, "a", "same.txt", active, ["segment-0000"])
    _write_fake_document_manifest(root, "b", "same.txt", active, ["segment-0000"])
    _write_fake_document_manifest(
        root,
        "stale",
        "stale.txt",
        {"provider": "fake", "model": "old", "dimension": 3},
        ["segment-0000"],
    )

    class Store:
        def __init__(self, path):
            self.path = str(path)

        def similarity_search_with_score(self, _query, k):
            del k
            if pathlib.Path(self.path).parent.name == "stale":
                raise AssertionError("stale segment must not load")
            if self.path == str(legacy):
                return [(Document(page_content="legacy", metadata={"source": "legacy.txt"}), 0.2)]
            document_id = pathlib.Path(self.path).parent.name
            return [
                (
                    Document(page_content=document_id, metadata={"chunk_index": 0}),
                    0.1,
                )
            ]

    monkeypatch.setattr(document_index, "active_embedding_metadata", lambda: active)
    monkeypatch.setattr(document_index, "index_metadata_matches", lambda *_args: True)
    monkeypatch.setattr(
        langchain_community.vectorstores.FAISS,
        "load_local",
        lambda path, **_kwargs: Store(path),
    )

    facade = document_index.DocumentVectorStoreFacade(
        index_root=root,
        legacy_root=legacy,
        embedding_factory=lambda: object(),
    )
    results = facade.similarity_search("query", k=3)

    assert [result.page_content for result in results] == ["a", "b", "legacy"]
    assert all(result.metadata["source"].startswith("same.txt (") for result in results[:2])


def test_remove_same_name_document_targets_id_only(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    import row_bot.document_index as document_index

    document_index = importlib.reload(document_index)
    root = tmp_path / "index"
    _write_fake_document_manifest(root, "a", "same.txt", {}, [])
    _write_fake_document_manifest(root, "b", "same.txt", {}, [])

    assert document_index.remove_document_shard("a", index_root=root) is True

    remaining = document_index.read_corpus_manifest(root)["documents"]
    assert [row["document_id"] for row in remaining] == ["b"]
    assert (root / "documents" / "b").exists()
    assert not (root / "documents" / "a").exists()
    assert list((root / "retired").glob("a-*"))


def test_bounded_rebuild_from_vault_uses_sharded_atomic_output(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(data))
    import row_bot.documents as documents
    import row_bot.wiki_vault as wiki_vault

    documents = importlib.reload(documents)
    wiki_vault = importlib.reload(wiki_vault)
    vault = tmp_path / "vault"
    wiki_vault.set_vault_path(str(vault))
    wiki_vault.set_enabled(True)
    raw = vault / "raw"
    (raw / "one.txt").write_text("one " * 1000, encoding="utf-8")
    (raw / "two.txt").write_text("two " * 1000, encoding="utf-8")
    fake = FakeEmbeddings()
    monkeypatch.setattr(documents, "get_embedding_model", lambda: fake)

    assert documents.rebuild_vector_store_from_vault() == 2

    manifest = json.loads((documents.DOCUMENT_INDEX_DIR / "manifest.json").read_text())
    assert len(manifest["documents"]) == 2
    assert max(fake.batch_sizes) <= 32


def test_index_health_covers_new_compatible_and_stale_legacy_states(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    import row_bot.document_index as document_index

    document_index = importlib.reload(document_index)
    root = tmp_path / "index"
    legacy = tmp_path / "legacy"

    fresh = document_index.index_health(index_root=root, legacy_root=legacy)
    assert fresh["exists"] is False
    assert fresh["sharded_documents"] == 0

    legacy.mkdir()
    monkeypatch.setattr(document_index, "index_metadata_matches", lambda *_args: True)
    compatible = document_index.index_health(index_root=root, legacy_root=legacy)
    assert compatible["legacy_compatible"] is True
    assert compatible["stale"] is False

    monkeypatch.setattr(document_index, "index_metadata_matches", lambda *_args: False)
    stale = document_index.index_health(index_root=root, legacy_root=legacy)
    assert stale["legacy_compatible"] is False
    assert stale["stale"] is True
