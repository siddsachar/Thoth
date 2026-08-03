import importlib
import json
import threading
import time
import tomllib
import uuid
from pathlib import Path

import pytest


def _case_dir():
    path = Path(".tmp") / "pytest-embedding-provider-config" / f"case-{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _reload_embedding_config(monkeypatch, data_dir):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(data_dir))
    import row_bot.embedding_config as embedding_config

    return importlib.reload(embedding_config)


def _pyproject():
    return tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))


def _dependency_names(entries):
    return {
        entry.split(";", 1)[0]
        .split("[", 1)[0]
        .split(">=", 1)[0]
        .split("==", 1)[0]
        .split("<", 1)[0]
        .strip()
        .lower()
        for entry in entries
    }


def test_embedding_config_defaults_and_index_metadata(monkeypatch):
    data_dir = _case_dir()
    embedding_config = _reload_embedding_config(monkeypatch, data_dir)

    cfg = embedding_config.get_embedding_config()
    assert cfg["provider"] == "local"
    assert cfg["local_model"] == "mxbai-large-v1"
    assert cfg["auto_unload"] is False

    active = embedding_config.active_embedding_metadata(cfg)
    assert active["provider"] == "local"
    assert active["model"] == "mixedbread-ai/mxbai-embed-large-v1"
    assert active["dimension"] == 1024

    vector_dir = data_dir / "vector_store"
    assert not embedding_config.index_metadata_matches(vector_dir, active)

    embedding_config.write_index_metadata(vector_dir, active)
    assert embedding_config.index_metadata_matches(vector_dir, active)

    saved = embedding_config.save_embedding_config(
        {
            "provider": "cloud",
            "cloud_model": "openai:text-embedding-3-small",
            "dimension": 512,
            "batch_size": 999,
        }
    )
    assert saved["batch_size"] == 256
    assert embedding_config.active_embedding_metadata(saved)["dimension"] == 512
    assert not embedding_config.index_metadata_matches(vector_dir)


def test_embedding_config_recovers_from_invalid_values(monkeypatch):
    data_dir = _case_dir()
    embedding_config = _reload_embedding_config(monkeypatch, data_dir)
    embedding_config.CONFIG_PATH.write_text(
        json.dumps(
            {
                "provider": "unknown",
                "local_model": "missing",
                "cloud_model": "missing",
                "dimension": "0",
                "batch_size": "not-a-number",
            }
        ),
        encoding="utf-8",
    )

    cfg = embedding_config.get_embedding_config()

    assert cfg["provider"] == "local"
    assert cfg["local_model"] == "mxbai-large-v1"
    assert cfg["cloud_model"] == "openai:text-embedding-3-small"
    assert cfg["dimension"] is None
    assert cfg["batch_size"] == 32


def test_nomic_dependency_is_explicit():
    optional = _pyproject()["project"]["optional-dependencies"]
    local_embedding_deps = _dependency_names(optional["local-embeddings"])
    requirements = Path("requirements.txt").read_text(encoding="utf-8")

    import row_bot.embedding_config as embedding_config

    assert "sentence-transformers" in local_embedding_deps
    assert "langchain-huggingface" in local_embedding_deps
    assert "einops" in local_embedding_deps
    assert "# This file is generated from pyproject.toml and uv.lock." in requirements
    assert "sentence-transformers==" in requirements
    assert "langchain-huggingface==" in requirements
    assert embedding_config.LOCAL_MODELS["nomic-v1.5"]["required_packages"] == ["einops"]


def test_packaged_builds_verify_required_runtime_imports():
    verifier = Path("scripts/verify_runtime_dependencies.py").read_text(encoding="utf-8")
    windows_build = Path("installer/build_installer.ps1").read_text(encoding="utf-8")
    mac_build = Path("installer/build_mac_app.sh").read_text(encoding="utf-8")
    linux_build = Path("installer/build_linux_app.sh").read_text(encoding="utf-8")
    legacy_deps = Path("installer/install_deps.bat").read_text(encoding="utf-8")
    windows_installer = Path("installer/row_bot_setup.iss").read_text(encoding="utf-8")

    for group in (
        "core",
        "voice",
        "designer",
        "browser",
        "channels",
        "mcp",
        "developer",
        "local-embeddings",
        "media",
        "all",
        "embeddings",
        "providers",
        "tools",
        "youtube",
    ):
        assert f'"{group}"' in verifier
    assert '"sentence_transformers"' in verifier
    assert '"langchain_huggingface"' in verifier
    assert '"httpx"' in verifier
    assert '"google.genai"' in verifier
    assert '"youtube_transcript_api"' in verifier
    assert "verify_runtime_dependencies.py" in windows_build
    assert "verify_runtime_dependencies.py" in windows_installer
    assert "build\\python\\Lib\\site-packages\\sentence_transformers\\__init__.py" in windows_installer
    assert "build\\python\\Lib\\site-packages\\langchain_huggingface\\__init__.py" in windows_installer
    assert "build\\python\\Lib\\site-packages\\transformers\\__init__.py" in windows_installer
    assert "build\\python\\Lib\\site-packages\\torch\\__init__.py" in windows_installer
    assert "build\\python\\Lib\\site-packages\\httpx\\__init__.py" in windows_installer
    assert "build\\python\\Lib\\site-packages\\keyring\\__init__.py" in windows_installer
    assert "build\\python\\Lib\\site-packages\\langchain_ollama\\__init__.py" in windows_installer
    assert "build\\python\\Lib\\site-packages\\langchain_openai\\__init__.py" in windows_installer
    assert "build\\python\\Lib\\site-packages\\langchain_anthropic\\__init__.py" in windows_installer
    assert "build\\python\\Lib\\site-packages\\langchain_google_genai\\__init__.py" in windows_installer
    assert "build\\python\\Lib\\site-packages\\langchain_openrouter\\__init__.py" in windows_installer
    assert "build\\python\\Lib\\site-packages\\langchain_xai\\__init__.py" in windows_installer
    assert "build\\python\\Lib\\site-packages\\google\\genai\\__init__.py" in windows_installer
    assert "build\\python\\Lib\\site-packages\\openai\\__init__.py" in windows_installer
    assert "build\\python\\Lib\\site-packages\\mcp\\__init__.py" in windows_installer
    assert "build\\python\\Lib\\site-packages\\youtube_search\\__init__.py" in windows_installer
    assert "build\\python\\Lib\\site-packages\\youtube_transcript_api\\__init__.py" in windows_installer
    assert "verify_runtime_dependencies.py\"" in mac_build
    assert "verify_runtime_dependencies.py\"" in linux_build
    assert "verify_runtime_dependencies.py\" all" in mac_build
    assert "verify_runtime_dependencies.py\" all" in linux_build
    assert "verify_runtime_dependencies.py\" embeddings" not in mac_build
    assert "verify_runtime_dependencies.py\" embeddings" not in linux_build
    assert "Assembled app runtime dependencies verified" in mac_build
    assert "Assembled Linux runtime dependencies verified" in linux_build
    assert "ROW_BOT_INSTALL_ROOT=\"$RESOURCES\"" in mac_build
    unsafe_tests_cleanup = "find \"$PYTHON_PREFIX/lib\" -type d -name 'tests'"
    assert unsafe_tests_cleanup not in mac_build
    assert unsafe_tests_cleanup not in linux_build
    assert "verify_runtime_dependencies.py\" embeddings" not in legacy_deps
    assert "verify_runtime_dependencies.py\" all >>" in legacy_deps


def test_startup_diagnostics_reports_required_embedding_packages(monkeypatch):
    import row_bot.startup_diagnostics as startup_diagnostics

    def _missing_transformers(name):
        if name == "transformers":
            return None
        return object()

    monkeypatch.setattr(startup_diagnostics.importlib.util, "find_spec", _missing_transformers)

    missing = startup_diagnostics.preflight_required_runtime_packages()

    assert missing == {"embeddings": ["transformers"]}


def test_local_embedding_preflight_reports_missing_base_packages(monkeypatch):
    import row_bot.embedding_providers as embedding_providers

    def _missing_base_package(name):
        if name == "sentence_transformers":
            return None
        return object()

    monkeypatch.setattr(embedding_providers.importlib.util, "find_spec", _missing_base_package)

    try:
        embedding_providers.ensure_embedding_runtime_available(
            {"provider": "local", "local_model": "mxbai-large-v1"}
        )
    except RuntimeError as exc:
        assert "sentence_transformers" in str(exc)
        assert "Active Python:" in str(exc)
    else:
        raise AssertionError("missing sentence_transformers should fail preflight")


def test_dimension_adapter_trims_query_and_document_vectors():
    from langchain_core.embeddings import Embeddings

    from row_bot.embedding_providers import _DimensionAdapter

    class FakeProvider(Embeddings):
        def embed_query(self, text):
            return [1.0, 2.0, 3.0]

        def embed_documents(self, texts):
            return [[1.0, 2.0, 3.0] for _ in texts]

    adapter = _DimensionAdapter(FakeProvider(), 2)

    assert isinstance(adapter, Embeddings)
    assert adapter.embed_query("hello") == [1.0, 2.0]
    assert adapter.embed_documents(["a", "b"]) == [[1.0, 2.0], [1.0, 2.0]]


def test_local_embedding_provider_is_strictly_cache_only(monkeypatch, tmp_path):
    import langchain_huggingface
    import row_bot.embedding_providers as embedding_providers

    captured = {}

    class FakeHuggingFaceEmbeddings:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(
        langchain_huggingface,
        "HuggingFaceEmbeddings",
        FakeHuggingFaceEmbeddings,
    )
    monkeypatch.setattr(
        embedding_providers,
        "ensure_embedding_runtime_available",
        lambda _cfg=None: None,
    )
    monkeypatch.setattr(
        embedding_providers,
        "_require_cached_snapshot",
        lambda _model_key: tmp_path,
    )

    embedding_providers._build_local_provider(
        {
            "provider": "local",
            "local_model": "mxbai-large-v1",
            "cloud_model": "openai:text-embedding-3-small",
            "dimension": None,
            "batch_size": 16,
        }
    )

    assert captured["model_kwargs"]["local_files_only"] is True
    assert captured["model_kwargs"]["device"] == "cpu"
    assert captured["encode_kwargs"]["batch_size"] == 16
    assert captured["model_name"] == str(tmp_path)


def test_concurrent_recall_callers_share_one_normal_load(monkeypatch):
    import row_bot.embedding_providers as embedding_providers

    embedding_providers.release_embedding_resources("test reset", collect=False)
    cfg = {
        "provider": "local",
        "local_model": "mxbai-large-v1",
        "cloud_model": "openai:text-embedding-3-small",
        "dimension": None,
        "batch_size": 16,
    }
    provider = object()
    build_started = threading.Event()
    allow_build = threading.Event()
    build_count = 0
    build_count_lock = threading.Lock()

    monkeypatch.setattr(embedding_providers, "get_embedding_config", lambda: dict(cfg))
    monkeypatch.setattr(
        embedding_providers,
        "ensure_embedding_runtime_available",
        lambda _cfg=None: None,
    )
    monkeypatch.setattr(embedding_providers, "RECALL_EMBEDDING_WAIT_SECONDS", 1.0)

    def _slow_build(_cfg, key, *, generation=None):
        nonlocal build_count
        with build_count_lock:
            build_count += 1
        build_started.set()
        assert allow_build.wait(1.0)
        embedding_providers._provider = provider
        embedding_providers._provider_key = key
        return provider

    monkeypatch.setattr(embedding_providers, "_get_or_build_provider", _slow_build)
    results = []
    errors = []

    def _recall():
        try:
            results.append(embedding_providers.get_embedding_provider_for_recall())
        except Exception as exc:  # pragma: no cover - assertion below reports it
            errors.append(exc)

    first = threading.Thread(target=_recall)
    second = threading.Thread(target=_recall)
    first.start()
    assert build_started.wait(1.0)
    second.start()
    allow_build.set()
    first.join(1.0)
    second.join(1.0)

    assert errors == []
    assert results == [provider, provider]
    assert build_count == 1
    embedding_providers.release_embedding_resources("test cleanup", collect=False)


def test_missing_cached_model_fails_fast_after_first_attempt(monkeypatch):
    import row_bot.embedding_providers as embedding_providers

    embedding_providers.release_embedding_resources("test reset", collect=False)
    cfg = {
        "provider": "local",
        "local_model": "mxbai-large-v1",
        "cloud_model": "openai:text-embedding-3-small",
        "dimension": None,
    }
    attempts = 0
    monkeypatch.setattr(embedding_providers, "get_embedding_config", lambda: dict(cfg))
    monkeypatch.setattr(
        embedding_providers,
        "ensure_embedding_runtime_available",
        lambda _cfg=None: None,
    )

    def _missing(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise OSError("Model not found in the local cache")

    monkeypatch.setattr(embedding_providers, "_get_or_build_provider", _missing)

    with pytest.raises(embedding_providers.LocalEmbeddingUnavailable) as first:
        embedding_providers.get_embedding_provider_for_recall()
    started = time.perf_counter()
    with pytest.raises(embedding_providers.LocalEmbeddingUnavailable) as second:
        embedding_providers.get_embedding_provider_for_recall()

    assert first.value.code == "local_model_missing"
    assert second.value.code == "local_model_missing"
    assert attempts == 1
    assert time.perf_counter() - started < 0.2
    embedding_providers.release_embedding_resources("test cleanup", collect=False)


def test_recall_waits_one_shared_grace_then_recovers_when_load_finishes(monkeypatch):
    import row_bot.embedding_providers as embedding_providers

    embedding_providers.release_embedding_resources("test reset", collect=False)
    cfg = {
        "provider": "local",
        "local_model": "mxbai-large-v1",
        "cloud_model": "openai:text-embedding-3-small",
        "dimension": None,
    }
    provider = object()
    build_started = threading.Event()
    allow_build = threading.Event()
    attempts = 0
    monkeypatch.setattr(embedding_providers, "get_embedding_config", lambda: dict(cfg))
    monkeypatch.setattr(
        embedding_providers,
        "ensure_embedding_runtime_available",
        lambda _cfg=None: None,
    )
    monkeypatch.setattr(embedding_providers, "RECALL_EMBEDDING_WAIT_SECONDS", 0.03)

    def _slow_build(_cfg, key, *, generation=None):
        nonlocal attempts
        attempts += 1
        build_started.set()
        assert allow_build.wait(1.0)
        embedding_providers._provider = provider
        embedding_providers._provider_key = key
        return provider

    monkeypatch.setattr(embedding_providers, "_get_or_build_provider", _slow_build)
    started = time.perf_counter()
    with pytest.raises(embedding_providers.LocalEmbeddingUnavailable) as first:
        embedding_providers.get_embedding_provider_for_recall()
    first_elapsed = time.perf_counter() - started
    second_started = time.perf_counter()
    with pytest.raises(embedding_providers.LocalEmbeddingUnavailable) as second:
        embedding_providers.get_embedding_provider_for_recall()
    second_elapsed = time.perf_counter() - second_started

    assert build_started.is_set()
    assert first.value.code == "local_model_timeout"
    assert second.value.code == "local_model_timeout"
    assert first_elapsed > 0.005
    assert second_elapsed < first_elapsed
    assert attempts == 1

    allow_build.set()
    load_thread = embedding_providers._load_thread
    assert load_thread is not None
    load_thread.join(1.0)
    assert embedding_providers.get_embedding_provider_for_recall() is provider
    embedding_providers.release_embedding_resources("test cleanup", collect=False)


def test_download_is_explicit_and_status_probe_is_cache_only(monkeypatch, tmp_path):
    import huggingface_hub
    import row_bot.embedding_providers as embedding_providers

    calls = []

    def _snapshot_download(**kwargs):
        calls.append(kwargs)
        return str(tmp_path)

    monkeypatch.setattr(huggingface_hub, "snapshot_download", _snapshot_download)
    monkeypatch.setattr(
        embedding_providers,
        "get_embedding_config",
        lambda: {
            "provider": "cloud",
            "local_model": "mxbai-large-v1",
            "cloud_model": "openai:text-embedding-3-small",
            "dimension": None,
        },
    )

    assert embedding_providers._cached_snapshot("mxbai-large-v1") == tmp_path
    embedding_providers.download_local_embedding_model("mxbai-large-v1", repair=True)

    assert calls[0]["local_files_only"] is True
    assert calls[1]["local_files_only"] is False
    assert calls[1]["force_download"] is True


def test_markdown_loader_uses_builtin_encoding_fallback():
    import row_bot.documents as documents

    path = _case_dir() / "notes.md"
    path.write_bytes("# Cafe notes\n\nSmart quote: \x93hello\x94".encode("latin-1"))

    loader = documents.DocumentLoader.supported_file_types[".md"](str(path))
    pages = loader.load()

    assert pages
    assert "Cafe notes" in pages[0].page_content
    assert "autodetect_encoding" not in Path("src/row_bot/documents.py").read_text(encoding="utf-8")


def test_embedding_overhaul_source_contracts_are_wired():
    root = Path(".")
    documents_src = (root / "src" / "row_bot" / "documents.py").read_text(encoding="utf-8")
    extraction_src = (root / "src" / "row_bot" / "document_extraction.py").read_text(encoding="utf-8")
    memory_src = (root / "src" / "row_bot" / "memory_extraction.py").read_text(encoding="utf-8")
    settings_src = (root / "src" / "row_bot" / "ui" / "settings.py").read_text(encoding="utf-8")
    installer_src = (root / "installer" / "row_bot_setup.iss").read_text(encoding="utf-8")

    assert "get_embedding_provider()" in documents_src
    assert "DocumentVectorStoreFacade" in documents_src
    assert "legacy compatibility" in documents_src
    assert "rebuild_vector_store_from_vault" in documents_src
    assert "iter_document_chunks" in documents_src
    assert "iter_chunk_batches" in documents_src
    assert 'release_document_embedding_resources("document extraction complete")' in extraction_src
    assert 'release_embedding_resources("memory extraction complete")' in memory_src
    assert "Cloud embeddings send document chunks and memory text" in settings_src
    assert "Embedding engine" in settings_src
    assert 'logger.error("Document vector rebuild failed", exc_info=True)' in settings_src
    assert 'logger.error("Memory vector rebuild failed", exc_info=True)' in settings_src
    assert '"Document upload staging rejected for %s"' in settings_src
    assert "doc_upload.reset()" in settings_src
    assert "on_multi_upload=_handle_doc_upload_batch" in settings_src
    assert "embedding_config.py" in installer_src
    assert "embedding_providers.py" in installer_src
