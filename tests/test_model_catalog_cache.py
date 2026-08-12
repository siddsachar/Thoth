import threading
import time

from row_bot.providers.model_catalog_cache import CatalogCacheSnapshot


def test_model_catalog_cache_round_trip(tmp_path, monkeypatch):
    import row_bot.providers.model_catalog_cache as cache

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
    import row_bot.api_keys as api_keys
    import row_bot.providers.config as provider_config
    import row_bot.providers.model_catalog_cache as cache

    path = tmp_path / "model_catalog_cache.json"
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(api_keys, "get_cloud_config", lambda: {"starred_models": []})
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


def test_minimax_provider_refresh_does_not_preclear_on_failed_fetch(monkeypatch):
    import row_bot.models as models
    import row_bot.providers.model_catalog_cache as cache

    old_cache = dict(models._cloud_model_cache)
    monkeypatch.setattr(models, "fetch_context_catalog", lambda: 0)
    monkeypatch.setattr(models, "_fetch_cloud_models", lambda provider_id: 0)
    monkeypatch.setattr(models, "_save_cloud_cache", lambda: None)
    try:
        models._cloud_model_cache.clear()
        models._cloud_model_cache["MiniMax-M2.7"] = {
            "provider": "minimax",
            "label": "MiniMax-M2.7",
            "ctx": 204_800,
        }

        cloud_cache, status = cache._refresh_cloud_cache(provider_id="minimax")

        assert "MiniMax-M2.7" in cloud_cache
        assert cloud_cache["MiniMax-M2.7"]["provider"] == "minimax"
        assert status == {
            "minimax": {
                "status": "cached",
                "count": 1,
                "live_count": 0,
                "preserved_count": 1,
                "source": "last_known_good",
                "message": "No live catalog update; kept 1 cached model(s).",
            }
        }
    finally:
        models._cloud_model_cache.clear()
        models._cloud_model_cache.update(old_cache)


def test_model_catalog_refresh_prunes_stale_custom_quick_choices(tmp_path, monkeypatch):
    import row_bot.api_keys as api_keys
    import row_bot.providers.config as provider_config
    import row_bot.providers.model_catalog_cache as cache
    from row_bot.providers.selection import add_quick_choice_for_model

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(api_keys, "get_cloud_config", lambda: {"starred_models": []})
    monkeypatch.setattr(cache, "CATALOG_CACHE_PATH", tmp_path / "model_catalog_cache.json")
    monkeypatch.setattr(cache, "_refresh_cloud_cache", lambda provider_id=None: (
        {"gpt-test": {"provider": "openai", "label": "GPT Test"}},
        {"openai": {"status": "ok", "count": 1}},
    ))
    monkeypatch.setattr(cache, "_refresh_ollama_rows", lambda: [])
    add_quick_choice_for_model("ghost-model", provider_id="custom_openai_deleted")

    refreshed = cache.refresh_model_catalog_cache(reason="test", force=True)

    assert refreshed.total_rows == 1
    assert provider_config.load_provider_config()["quick_choices"] == []


def test_background_model_catalog_refresh_coalesces(monkeypatch):
    import row_bot.providers.model_catalog_cache as cache

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
    src = open("src/row_bot/ui/settings.py", encoding="utf-8").read()

    assert "Load model settings" not in src
    assert "load_ollama_catalog_rows" not in src
    assert "build_cached_model_catalog_rows" in src
    assert "build_lazy_model_catalog_section" in src
    assert "Open only when you need to browse or pin models" in src
    assert "Model catalog refreshed: {rows} models" in src
    assert "start_model_catalog_refresh_background" in src


def test_model_catalog_keeps_saved_minimax_default_visible():
    from row_bot.providers.model_catalog import build_model_catalog_rows

    rows = build_model_catalog_rows(
        cloud_cache={},
        ollama_rows=[],
        defaults={"chat": "model:minimax:MiniMax-M2.7"},
    )

    minimax = [row for row in rows if row.selection_ref == "model:minimax:MiniMax-M2.7"]
    assert minimax
    assert minimax[0].supports("chat")
    assert "chat" in minimax[0].default_surfaces


def test_cached_catalog_rows_do_not_call_live_subscription_catalogs(tmp_path, monkeypatch):
    import row_bot.providers.config as provider_config
    import row_bot.providers.model_catalog as model_catalog
    import row_bot.providers.model_catalog_cache as cache
    import row_bot.providers.xai_oauth as xai_oauth

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(cache, "CATALOG_CACHE_PATH", tmp_path / "model_catalog_cache.json")
    monkeypatch.setattr(
        model_catalog,
        "_provider_status_by_id",
        lambda: {"xai_oauth": {"configured": True, "runtime_enabled": True}},
    )
    provider_config.save_provider_config({
        "providers": {
            "xai_oauth": {
                "catalog_cache": {
                    "models": [{
                        "id": "grok-4.3",
                        "display_name": "grok-4.3",
                        "context_window": 1_000_000,
                        "capabilities": ["chat", "streaming", "text"],
                        "input_modalities": ["text"],
                        "output_modalities": ["text"],
                        "tasks": ["responses"],
                        "tool_calling": True,
                        "streaming": True,
                        "transport": "openai_responses",
                    }],
                },
            },
        },
    })

    def _boom(*args, **kwargs):
        raise AssertionError("cached catalog rendering must not call live subscription catalogs")

    monkeypatch.setattr(xai_oauth, "list_xai_oauth_model_infos", _boom)

    rows = cache.build_cached_model_catalog_rows(
        snapshot=cache.CatalogCacheSnapshot(
            version=cache.CACHE_VERSION,
            generated_at=123.0,
            cloud_cache={},
            ollama_rows=[],
            provider_status={},
            warnings=(),
            reason="test",
        ),
        quick_choices=[],
    )

    assert "model:xai_oauth:grok-4.3" in {row.selection_ref for row in rows}


def test_codex_provider_refresh_discovers_live_models(tmp_path, monkeypatch):
    import row_bot.models as models
    import row_bot.providers.codex as codex
    import row_bot.providers.config as provider_config
    import row_bot.providers.model_catalog_cache as cache

    old_cache = dict(models._cloud_model_cache)
    live_infos = [
        codex._codex_model_info_from_live_item({
            "slug": model_id,
            "display_name": model_id,
            "input_modalities": ["text", "image"],
            "supported_reasoning_efforts": ["medium"],
        })
        for model_id in ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna")
    ]
    assert all(live_infos)
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(models, "fetch_context_catalog", lambda: 0)
    monkeypatch.setattr(models, "_save_cloud_cache", lambda: None)
    monkeypatch.setattr(codex, "list_codex_model_infos", lambda force_refresh=False: list(live_infos))
    try:
        models._cloud_model_cache.clear()

        cloud_cache, status = cache._refresh_cloud_cache(provider_id="codex")

        assert {
            key for key, info in cloud_cache.items()
            if info.get("provider") == "codex"
        } == {
            "model:codex:gpt-5.6-sol",
            "model:codex:gpt-5.6-terra",
            "model:codex:gpt-5.6-luna",
        }
        assert status["codex"]["status"] == "live"
        assert status["codex"]["count"] == 3
    finally:
        models._cloud_model_cache.clear()
        models._cloud_model_cache.update(old_cache)


def test_global_refresh_uses_the_registered_provider_order(monkeypatch):
    import row_bot.models as models

    old_cache = dict(models._cloud_model_cache)
    calls: list[str] = []
    monkeypatch.setattr(models, "fetch_context_catalog", lambda: 0)
    monkeypatch.setattr(models, "_save_cloud_cache", lambda: None)
    monkeypatch.setattr(models, "_fetch_cloud_models", lambda provider_id: calls.append(provider_id) or 0)
    monkeypatch.setattr(models, "_cloud_model_available_after_refresh", lambda _model: True)
    try:
        models.refresh_cloud_models_detailed()
    finally:
        models._cloud_model_cache.clear()
        models._cloud_model_cache.update(old_cache)

    assert calls == list(models.REFRESHABLE_CLOUD_PROVIDER_IDS)
    assert "codex" in calls


def test_failed_openai_refresh_preserves_last_known_good_rows(monkeypatch):
    import httpx

    import row_bot.models as models
    import row_bot.providers.auth_store as auth_store

    old_cache = dict(models._cloud_model_cache)
    monkeypatch.setattr(auth_store, "get_provider_secret", lambda provider_id: "test-key" if provider_id == "openai" else "")
    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")))
    try:
        models._cloud_model_cache.clear()
        models._cloud_model_cache["gpt-existing"] = {
            "provider": "openai",
            "label": "GPT Existing",
        }

        result = models.refresh_cloud_provider_models("openai")

        assert result.status == "cached"
        assert result.effective_count == 1
        assert models._cloud_model_cache["gpt-existing"]["provider"] == "openai"
    finally:
        models._cloud_model_cache.clear()
        models._cloud_model_cache.update(old_cache)


def test_successful_openai_refresh_replaces_only_openai_rows(monkeypatch):
    import httpx

    import row_bot.models as models
    import row_bot.providers.auth_store as auth_store

    old_cache = dict(models._cloud_model_cache)
    monkeypatch.setattr(auth_store, "get_provider_secret", lambda provider_id: "test-key" if provider_id == "openai" else "")
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *args, **kwargs: _PagedCatalogResponse({"data": [{"id": "gpt-new"}]}),
    )
    try:
        models._cloud_model_cache.clear()
        models._cloud_model_cache.update({
            "gpt-retired": {"provider": "openai", "label": "GPT Retired"},
            "claude-existing": {"provider": "anthropic", "label": "Claude Existing"},
        })

        result = models.refresh_cloud_provider_models("openai")

        assert result.status == "live"
        assert result.effective_count == 1
        assert "gpt-new" in models._cloud_model_cache
        assert "gpt-retired" not in models._cloud_model_cache
        assert "claude-existing" in models._cloud_model_cache
    finally:
        models._cloud_model_cache.clear()
        models._cloud_model_cache.update(old_cache)


class _PagedCatalogResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_anthropic_second_page_failure_does_not_commit_partial_catalog(monkeypatch):
    import httpx

    import row_bot.models as models

    old_cache = dict(models._cloud_model_cache)
    responses = iter([
        _PagedCatalogResponse({
            "data": [{"id": "claude-new"}],
            "has_more": True,
            "last_id": "claude-new",
        }),
        RuntimeError("page two failed"),
    ])

    def _get(*args, **kwargs):
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(httpx, "get", _get)
    try:
        models._cloud_model_cache.clear()
        models._cloud_model_cache["claude-existing"] = {
            "provider": "anthropic",
            "label": "Claude Existing",
        }

        assert models._fetch_anthropic_models("test-key") == 0
        assert "claude-existing" in models._cloud_model_cache
        assert "claude-new" not in models._cloud_model_cache
    finally:
        models._cloud_model_cache.clear()
        models._cloud_model_cache.update(old_cache)


def test_google_second_page_failure_does_not_commit_partial_catalog(monkeypatch):
    import httpx

    import row_bot.models as models

    old_cache = dict(models._cloud_model_cache)
    responses = iter([
        _PagedCatalogResponse({
            "models": [{"name": "models/gemini-new", "supportedGenerationMethods": ["generateContent"]}],
            "nextPageToken": "next-page",
        }),
        RuntimeError("page two failed"),
    ])

    def _get(*args, **kwargs):
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(httpx, "get", _get)
    try:
        models._cloud_model_cache.clear()
        models._cloud_model_cache["gemini-existing"] = {
            "provider": "google",
            "label": "Gemini Existing",
        }

        assert models._fetch_google_models("test-key") == 0
        assert "gemini-existing" in models._cloud_model_cache
        assert "gemini-new" not in models._cloud_model_cache
    finally:
        models._cloud_model_cache.clear()
        models._cloud_model_cache.update(old_cache)
