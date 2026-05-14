import threading
import time

from providers.model_catalog_cache import CatalogCacheSnapshot


def test_model_catalog_cache_round_trip(tmp_path, monkeypatch):
    import providers.model_catalog_cache as cache

    path = tmp_path / "model_catalog_cache.json"
    monkeypatch.setattr(cache, "CATALOG_CACHE_PATH", path)
    snapshot = CatalogCacheSnapshot(
        version=cache.CACHE_VERSION,
        generated_at=123.0,
        cloud_cache={"gpt-test": {"provider": "openai", "label": "GPT Test"}},
        ollama_rows=[{"model_id": "qwen3:14b", "installed": True}],
        provider_status={"openai": {"status": "ok", "count": 1}},
        warnings=("xAI rate limited",),
        reason="test",
    )

    cache.write_model_catalog_cache(snapshot)
    loaded = cache.read_model_catalog_cache()

    assert loaded.cloud_cache == snapshot.cloud_cache
    assert loaded.ollama_rows == snapshot.ollama_rows
    assert loaded.provider_status == snapshot.provider_status
    assert loaded.warnings == snapshot.warnings
    assert loaded.reason == "test"


def test_model_catalog_refresh_preserves_last_good_when_empty(tmp_path, monkeypatch):
    import providers.model_catalog_cache as cache

    path = tmp_path / "model_catalog_cache.json"
    monkeypatch.setattr(cache, "CATALOG_CACHE_PATH", path)
    previous = CatalogCacheSnapshot(
        version=cache.CACHE_VERSION,
        generated_at=time.time() - 1000,
        cloud_cache={"gpt-test": {"provider": "openai", "label": "GPT Test"}},
        ollama_rows=[],
        provider_status={"openai": {"status": "ok", "count": 1}},
        warnings=(),
        reason="previous",
    )
    cache.write_model_catalog_cache(previous)
    monkeypatch.setattr(cache, "_refresh_cloud_cache", lambda provider_id=None: ({}, {"cloud": {"status": "ok", "count": 0}}))
    monkeypatch.setattr(cache, "_refresh_ollama_rows", lambda: [])

    refreshed = cache.refresh_model_catalog_cache(reason="test", force=True)

    assert refreshed.cloud_cache == previous.cloud_cache
    assert cache.read_model_catalog_cache().cloud_cache == previous.cloud_cache


def test_background_model_catalog_refresh_coalesces(monkeypatch):
    import providers.model_catalog_cache as cache

    started = threading.Event()
    release = threading.Event()

    def _slow_refresh(**kwargs):
        started.set()
        release.wait(timeout=5)
        return cache.empty_catalog_cache()

    monkeypatch.setattr(cache, "refresh_model_catalog_cache", _slow_refresh)
    assert cache.start_model_catalog_refresh_background(reason="test", force=True) is True
    assert started.wait(timeout=2)
    assert cache.start_model_catalog_refresh_background(reason="second", force=True) is False
    release.set()
    deadline = time.time() + 5
    while cache.is_model_catalog_refresh_running() and time.time() < deadline:
        time.sleep(0.05)
    assert cache.is_model_catalog_refresh_running() is False


def test_settings_models_tab_is_cache_first():
    src = open("ui/settings.py", encoding="utf-8").read()

    assert "Load model settings" not in src
    assert "load_ollama_catalog_rows" not in src
    assert "build_cached_model_catalog_rows" in src
    assert "build_lazy_model_catalog_section" in src
    assert "Open only when you need to browse or pin models" in src
    assert "Model catalog refreshed: {rows} models" in src
    assert "start_model_catalog_refresh_background" in src


def test_model_catalog_keeps_saved_minimax_default_visible():
    from providers.model_catalog import build_model_catalog_rows

    rows = build_model_catalog_rows(
        cloud_cache={},
        ollama_rows=[],
        defaults={"chat": "model:minimax:MiniMax-M2.7"},
    )

    minimax = [row for row in rows if row.selection_ref == "model:minimax:MiniMax-M2.7"]
    assert minimax
    assert minimax[0].supports("chat")
    assert "chat" in minimax[0].default_surfaces
