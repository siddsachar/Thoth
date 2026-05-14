import importlib
import json
import uuid
from pathlib import Path


def _case_dir():
    path = Path(".tmp") / "pytest-embedding-provider-config" / f"case-{uuid.uuid4().hex}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _reload_embedding_config(monkeypatch, data_dir):
    monkeypatch.setenv("THOTH_DATA_DIR", str(data_dir))
    import embedding_config

    return importlib.reload(embedding_config)


def test_embedding_config_defaults_and_index_metadata(monkeypatch):
    data_dir = _case_dir()
    embedding_config = _reload_embedding_config(monkeypatch, data_dir)

    cfg = embedding_config.get_embedding_config()
    assert cfg["provider"] == "local"
    assert cfg["local_model"] == "mxbai-large-v1"

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
    requirements = Path("requirements.txt").read_text(encoding="utf-8")

    import embedding_config

    assert "sentence-transformers" in requirements
    assert "langchain-huggingface" in requirements
    assert "einops" in requirements
    assert embedding_config.LOCAL_MODELS["nomic-v1.5"]["required_packages"] == ["einops"]


def test_packaged_builds_verify_embedding_runtime_imports():
    verifier = Path("scripts/verify_runtime_dependencies.py").read_text(encoding="utf-8")
    windows_build = Path("installer/build_installer.ps1").read_text(encoding="utf-8")
    mac_build = Path("installer/build_mac_app.sh").read_text(encoding="utf-8")
    linux_build = Path("installer/build_linux_app.sh").read_text(encoding="utf-8")
    legacy_deps = Path("installer/install_deps.bat").read_text(encoding="utf-8")
    windows_installer = Path("installer/thoth_setup.iss").read_text(encoding="utf-8")

    assert '"embeddings"' in verifier
    assert '"sentence_transformers"' in verifier
    assert '"langchain_huggingface"' in verifier
    assert "verify_runtime_dependencies.py" in windows_build
    assert "verify_runtime_dependencies.py" in windows_installer
    assert "build\\python\\Lib\\site-packages\\sentence_transformers\\__init__.py" in windows_installer
    assert "build\\python\\Lib\\site-packages\\langchain_huggingface\\__init__.py" in windows_installer
    assert "build\\python\\Lib\\site-packages\\transformers\\__init__.py" in windows_installer
    assert "build\\python\\Lib\\site-packages\\torch\\__init__.py" in windows_installer
    assert "verify_runtime_dependencies.py\" embeddings" in mac_build
    assert "verify_runtime_dependencies.py\" embeddings" in linux_build
    assert "Assembled app runtime dependencies verified" in mac_build
    assert "Assembled Linux runtime dependencies verified" in linux_build
    assert "THOTH_INSTALL_ROOT=\"$RESOURCES\"" in mac_build
    assert "verify_runtime_dependencies.py\" embeddings" in legacy_deps


def test_startup_diagnostics_reports_required_embedding_packages(monkeypatch):
    import startup_diagnostics

    def _missing_transformers(name):
        if name == "transformers":
            return None
        return object()

    monkeypatch.setattr(startup_diagnostics.importlib.util, "find_spec", _missing_transformers)

    missing = startup_diagnostics.preflight_required_runtime_packages()

    assert missing == {"embeddings": ["transformers"]}


def test_local_embedding_preflight_reports_missing_base_packages(monkeypatch):
    import embedding_providers

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

    from embedding_providers import _DimensionAdapter

    class FakeProvider(Embeddings):
        def embed_query(self, text):
            return [1.0, 2.0, 3.0]

        def embed_documents(self, texts):
            return [[1.0, 2.0, 3.0] for _ in texts]

    adapter = _DimensionAdapter(FakeProvider(), 2)

    assert isinstance(adapter, Embeddings)
    assert adapter.embed_query("hello") == [1.0, 2.0]
    assert adapter.embed_documents(["a", "b"]) == [[1.0, 2.0], [1.0, 2.0]]


def test_markdown_loader_uses_builtin_encoding_fallback():
    import documents

    path = _case_dir() / "notes.md"
    path.write_bytes("# Cafe notes\n\nSmart quote: \x93hello\x94".encode("latin-1"))

    loader = documents.DocumentLoader.supported_file_types[".md"](str(path))
    pages = loader.load()

    assert pages
    assert "Cafe notes" in pages[0].page_content
    assert "autodetect_encoding" not in Path("documents.py").read_text(encoding="utf-8")


def test_embedding_overhaul_source_contracts_are_wired():
    root = Path(".")
    documents_src = (root / "documents.py").read_text(encoding="utf-8")
    extraction_src = (root / "document_extraction.py").read_text(encoding="utf-8")
    memory_src = (root / "memory_extraction.py").read_text(encoding="utf-8")
    settings_src = (root / "ui" / "settings.py").read_text(encoding="utf-8")
    installer_src = (root / "installer" / "thoth_setup.iss").read_text(encoding="utf-8")

    assert "get_embedding_provider()" in documents_src
    assert "index_metadata_matches(VECTOR_STORE_DIR)" in documents_src
    assert "write_index_metadata(VECTOR_STORE_DIR)" in documents_src
    assert "rebuild_vector_store_from_vault" in documents_src
    assert "VECTOR_STORE_DIR.with_name" in documents_src
    assert 'release_document_embedding_resources("document extraction complete")' in extraction_src
    assert 'release_embedding_resources("memory extraction complete")' in memory_src
    assert "Cloud embeddings send document chunks and memory text" in settings_src
    assert "Embedding engine" in settings_src
    assert 'logger.error("Document vector rebuild failed", exc_info=True)' in settings_src
    assert 'logger.error("Memory vector rebuild failed", exc_info=True)' in settings_src
    assert 'logger.error("Document upload/index failed for %s", name, exc_info=True)' in settings_src
    assert "doc_upload.reset()" in settings_src
    assert "embedding_config.py" in installer_src
    assert "embedding_providers.py" in installer_src
