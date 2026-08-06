"""Embedding provider factory and resource management."""

from __future__ import annotations

import gc
import importlib.util
import logging
import pathlib
import sys
import threading
import time
from typing import Any

from row_bot.api_keys import get_key
from row_bot.embedding_config import CLOUD_MODELS, LOCAL_MODELS, active_embedding_metadata, get_embedding_config
from langchain_core.embeddings import Embeddings
from row_bot.stability import log_performance_snapshot

logger = logging.getLogger(__name__)

_provider_lock = threading.Lock()
_provider = None
_provider_key: tuple[Any, ...] | None = None

RECALL_EMBEDDING_WAIT_SECONDS = 30.0

_load_state_lock = threading.Lock()
_load_generation = 0
_load_key: tuple[Any, ...] | None = None
_load_status = "idle"
_load_started_at = 0.0
_load_deadline = 0.0
_load_error_code = ""
_load_error_detail = ""
_load_event = threading.Event()
_load_thread: threading.Thread | None = None

_BASE_LOCAL_PACKAGES = ("sentence_transformers", "langchain_huggingface")
_LOCAL_MODEL_DOWNLOAD_IGNORE_PATTERNS = (
    "gguf/*",
    "onnx/*",
    "openvino/*",
)


class LocalEmbeddingUnavailable(RuntimeError):
    """A display-safe reason why local semantic recall is unavailable."""

    def __init__(self, code: str, detail: str):
        self.code = str(code or "local_model_failed")
        self.detail = str(detail or "The local embedding model could not be loaded.")
        super().__init__(self.detail)


class _DimensionAdapter(Embeddings):
    """Trim embedding vectors when a provider supports Matryoshka-like dimensions."""

    def __init__(self, inner: Any, dimension: int | None):
        self.inner = inner
        self.dimension = int(dimension) if dimension else None

    def embed_query(self, text: str) -> list[float]:
        return self._trim(self.inner.embed_query(text))

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._trim(vec) for vec in self.inner.embed_documents(texts)]

    def _trim(self, vector: list[float]) -> list[float]:
        if not self.dimension:
            return vector
        return list(vector)[: self.dimension]


def _embedding_key(cfg: dict[str, Any]) -> tuple[Any, ...]:
    meta = active_embedding_metadata(cfg)
    return (
        cfg.get("provider"),
        cfg.get("local_model"),
        cfg.get("cloud_model"),
        meta.get("dimension"),
    )


def _classify_local_load_error(exc: Exception) -> tuple[str, str]:
    detail = str(exc or "").strip()
    lowered = detail.lower()
    exception_name = type(exc).__name__.lower()
    missing_markers = (
        "local_files_only",
        "local cache",
        "cached files",
        "couldn't find the requested files",
        "cannot find the requested files",
        "not found in the cached files",
        "not found locally",
        "cached snapshot",
        "outgoing traffic has been disabled",
    )
    if "localentrynotfound" in exception_name or any(marker in lowered for marker in missing_markers):
        return (
            "local_model_missing",
            "The selected local embedding model is not available in the local cache.",
        )
    return (
        "local_model_failed",
        "The cached local embedding model could not be loaded.",
    )


def _begin_local_load(key: tuple[Any, ...], *, force: bool = False) -> tuple[int, bool]:
    global _load_generation, _load_key, _load_status
    global _load_started_at, _load_deadline, _load_error_code, _load_error_detail
    global _load_event
    with _load_state_lock:
        if not force and _provider is not None and _provider_key == key:
            _load_key = key
            _load_status = "ready"
            _load_error_code = ""
            _load_error_detail = ""
            _load_event.set()
            return _load_generation, False
        if not force and _load_key == key and _load_status in {"loading", "missing", "failed"}:
            return _load_generation, False
        if not force and _load_key == key and _load_status == "ready":
            return _load_generation, False
        _load_generation += 1
        _load_key = key
        _load_status = "loading"
        _load_started_at = time.monotonic()
        _load_deadline = _load_started_at + RECALL_EMBEDDING_WAIT_SECONDS
        _load_error_code = ""
        _load_error_detail = ""
        _load_event = threading.Event()
        return _load_generation, True


def _finish_local_load(
    key: tuple[Any, ...],
    generation: int,
    *,
    error: Exception | None = None,
) -> None:
    global _load_status, _load_error_code, _load_error_detail
    with _load_state_lock:
        if _load_key != key or _load_generation != generation:
            return
        if error is None:
            _load_status = "ready"
            _load_error_code = ""
            _load_error_detail = ""
        else:
            _load_error_code, _load_error_detail = _classify_local_load_error(error)
            _load_status = "missing" if _load_error_code == "local_model_missing" else "failed"
        _load_event.set()


def _get_or_build_provider(
    cfg: dict[str, Any],
    key: tuple[Any, ...],
    *,
    generation: int | None = None,
) -> Any:
    global _provider, _provider_key
    if _provider is not None and _provider_key == key:
        return _provider
    with _provider_lock:
        if _provider is not None and _provider_key == key:
            return _provider
        built = _build_provider(cfg)
        if generation is not None:
            with _load_state_lock:
                if _load_key != key or _load_generation != generation:
                    raise LocalEmbeddingUnavailable(
                        "local_model_failed",
                        "The local embedding configuration changed while the model was loading.",
                    )
        _provider = built
        _provider_key = key
        return _provider


def get_embedding_provider() -> Any:
    """Return the active embedding object, blocking for explicit embedding work."""
    cfg = get_embedding_config()
    ensure_embedding_runtime_available(cfg)
    key = _embedding_key(cfg)
    if _provider is not None and _provider_key == key:
        return _provider
    if cfg.get("provider") == "cloud":
        return _get_or_build_provider(cfg, key)

    generation, _ = _begin_local_load(key)
    try:
        provider = _get_or_build_provider(cfg, key, generation=generation)
    except Exception as exc:
        _finish_local_load(key, generation, error=exc)
        raise
    _finish_local_load(key, generation)
    return provider


def _local_load_worker(cfg: dict[str, Any], key: tuple[Any, ...], generation: int) -> None:
    try:
        ensure_embedding_runtime_available(cfg)
        _get_or_build_provider(cfg, key, generation=generation)
    except Exception as exc:
        logger.warning("Local embedding model load failed: %s", exc)
        _finish_local_load(key, generation, error=exc)
        return
    _finish_local_load(key, generation)


def start_local_embedding_load(*, force: bool = False) -> dict[str, Any]:
    """Start one background, cache-only load for the selected local model."""
    global _load_thread
    cfg = get_embedding_config()
    if cfg.get("provider") != "local":
        return get_local_embedding_status()
    key = _embedding_key(cfg)
    generation, should_start = _begin_local_load(key, force=force)
    if should_start:
        thread = threading.Thread(
            target=_local_load_worker,
            args=(dict(cfg), key, generation),
            name="row-bot-local-embedding-load",
            daemon=True,
        )
        with _load_state_lock:
            if _load_key == key and _load_generation == generation:
                _load_thread = thread
        thread.start()
    return get_local_embedding_status(probe_cache=False)


def get_embedding_provider_for_recall() -> Any:
    """Return the provider after the shared first-load grace, or fail quickly."""
    cfg = get_embedding_config()
    if cfg.get("provider") != "local":
        return get_embedding_provider()
    key = _embedding_key(cfg)
    if _provider is not None and _provider_key == key:
        return _provider

    start_local_embedding_load()
    with _load_state_lock:
        status = _load_status if _load_key == key else "idle"
        event = _load_event
        deadline = _load_deadline
        error_code = _load_error_code
        error_detail = _load_error_detail

    if status in {"missing", "failed"}:
        raise LocalEmbeddingUnavailable(error_code, error_detail)
    remaining = max(0.0, deadline - time.monotonic())
    if status == "loading" and remaining > 0:
        event.wait(remaining)

    if _provider is not None and _provider_key == key:
        return _provider
    with _load_state_lock:
        status = _load_status if _load_key == key else "idle"
        error_code = _load_error_code
        error_detail = _load_error_detail
    if status in {"missing", "failed"}:
        raise LocalEmbeddingUnavailable(error_code, error_detail)
    raise LocalEmbeddingUnavailable(
        "local_model_timeout",
        f"The local embedding model did not become ready within {int(RECALL_EMBEDDING_WAIT_SECONDS)} seconds.",
    )


def _require_cached_snapshot(model_key: str) -> pathlib.Path:
    """Resolve a model to a local path without allowing network access."""
    from huggingface_hub import snapshot_download

    return pathlib.Path(
        snapshot_download(
            repo_id=str(LOCAL_MODELS[model_key]["model"]),
            local_files_only=True,
        )
    )


def _cached_snapshot(model_key: str) -> pathlib.Path | None:
    try:
        return _require_cached_snapshot(model_key)
    except Exception:
        return None


def get_local_embedding_status(*, probe_cache: bool = True) -> dict[str, Any]:
    """Return display-safe state for the selected local embedding model."""
    cfg = get_embedding_config()
    model_key = str(cfg.get("local_model") or "mxbai-large-v1")
    model_def = LOCAL_MODELS[model_key]
    key = _embedding_key(cfg)
    if cfg.get("provider") != "local":
        return {
            "state": "inactive",
            "model_key": model_key,
            "label": str(model_def["label"]),
            "detail": "Cloud embeddings are selected.",
        }
    if _provider is not None and _provider_key == key:
        state = "ready"
        detail = "Loaded locally."
    else:
        with _load_state_lock:
            state = _load_status if _load_key == key else "idle"
            detail = _load_error_detail if _load_key == key else ""
        if state == "idle" and probe_cache:
            state = "cached" if _cached_snapshot(model_key) is not None else "missing"
            detail = "Available in the local cache." if state == "cached" else "Not available in the local cache."
    return {
        "state": state,
        "model_key": model_key,
        "label": str(model_def["label"]),
        "detail": detail,
    }


def retry_local_embedding_load() -> dict[str, Any]:
    """Start a fresh cache-only load attempt for the selected local model."""
    release_embedding_resources("local embedding retry", collect=False)
    return start_local_embedding_load(force=True)


def download_local_embedding_model(model_key: str, *, repair: bool = False) -> pathlib.Path:
    """Explicitly download or repair a selected model in the Hugging Face cache."""
    if model_key not in LOCAL_MODELS:
        raise ValueError(f"Unknown local embedding model: {model_key}")
    if not repair:
        cached = _cached_snapshot(model_key)
        if cached is not None:
            return cached
    from huggingface_hub import snapshot_download

    path = pathlib.Path(
        snapshot_download(
            repo_id=str(LOCAL_MODELS[model_key]["model"]),
            local_files_only=False,
            force_download=bool(repair),
            ignore_patterns=list(_LOCAL_MODEL_DOWNLOAD_IGNORE_PATTERNS),
        )
    )
    cfg = get_embedding_config()
    if cfg.get("provider") == "local" and cfg.get("local_model") == model_key:
        retry_local_embedding_load()
    return path


def release_embedding_resources(reason: str = "manual", *, collect: bool = True) -> None:
    """Clear cached embedding provider resources and ask native backends to release memory."""
    global _provider, _provider_key, _load_generation, _load_key, _load_status
    global _load_started_at, _load_deadline, _load_error_code, _load_error_detail
    global _load_event, _load_thread
    if _provider is None and not collect:
        with _load_state_lock:
            if _load_status == "idle":
                return
    logger.info("perf: releasing embedding resources (%s)", reason)
    log_performance_snapshot("embedding-release-before")
    _provider = None
    _provider_key = None
    with _load_state_lock:
        _load_generation += 1
        _load_event.set()
        _load_key = None
        _load_status = "idle"
        _load_started_at = 0.0
        _load_deadline = 0.0
        _load_error_code = ""
        _load_error_detail = ""
        _load_event = threading.Event()
        _load_thread = None
    if collect:
        gc.collect()
        try:
            import torch

            if hasattr(torch, "cuda") and torch.cuda.is_available():
                torch.cuda.empty_cache()
            if hasattr(torch, "mps") and hasattr(torch.mps, "empty_cache"):
                torch.mps.empty_cache()
        except Exception:
            logger.debug("Torch cache release unavailable", exc_info=True)
        gc.collect()
    log_performance_snapshot("embedding-release-after")


def _build_provider(cfg: dict[str, Any]) -> Any:
    if cfg.get("provider") == "cloud":
        return _build_cloud_provider(cfg)
    return _build_local_provider(cfg)


def ensure_embedding_runtime_available(cfg: dict[str, Any] | None = None) -> None:
    """Raise a clear error if the configured embedding runtime cannot start."""
    cfg = cfg or get_embedding_config()
    if cfg.get("provider") == "cloud":
        model_key = str(cfg.get("cloud_model") or "openai:text-embedding-3-small")
        model_def = CLOUD_MODELS[model_key]
        if not get_key(str(model_def["api_key"])):
            raise RuntimeError(f"{model_def['api_key']} is required for {model_def['label']}")
        return

    model_key = str(cfg.get("local_model") or "qwen3-0.6b")
    model_def = LOCAL_MODELS[model_key]
    required = [*_BASE_LOCAL_PACKAGES, *model_def.get("required_packages", [])]
    missing = [pkg for pkg in required if importlib.util.find_spec(str(pkg)) is None]
    if missing:
        package_list = ", ".join(missing)
        raise RuntimeError(
            f"{model_def['label']} cannot start because Python package(s) are missing: {package_list}. "
            f"Active Python: {sys.executable}. "
            "This packaged runtime is incomplete; reinstall Row-Bot from a build that passes runtime dependency verification."
        )


def _build_local_provider(cfg: dict[str, Any]) -> Any:
    model_key = str(cfg.get("local_model") or "qwen3-0.6b")
    model_def = LOCAL_MODELS[model_key]
    model_path = _require_cached_snapshot(model_key)
    dimension = int(active_embedding_metadata(cfg)["dimension"])
    ensure_embedding_runtime_available(cfg)
    model_kwargs = {"device": "cpu", "local_files_only": True}
    if model_def.get("trust_remote_code"):
        model_kwargs["trust_remote_code"] = True

    import io as _io
    import os as _os
    import sys as _sys

    _os.environ["TQDM_DISABLE"] = "1"
    old_stderr = _sys.stderr
    _sys.stderr = _io.StringIO()
    try:
        from langchain_huggingface import HuggingFaceEmbeddings

        logger.info("Loading local embedding model %s", model_def["model"])
        provider = HuggingFaceEmbeddings(
            model_name=str(model_path),
            model_kwargs=model_kwargs,
            encode_kwargs={"batch_size": int(cfg.get("batch_size") or 32)},
        )
    finally:
        _sys.stderr = old_stderr
        _os.environ.pop("TQDM_DISABLE", None)
    logger.info("Local embedding model loaded: %s", model_def["model"])
    if dimension == int(model_def["dimension"]):
        return provider
    return _DimensionAdapter(provider, dimension)


def _build_cloud_provider(cfg: dict[str, Any]) -> Any:
    model_key = str(cfg.get("cloud_model") or "openai:text-embedding-3-small")
    model_def = CLOUD_MODELS[model_key]
    dimension = int(active_embedding_metadata(cfg)["dimension"])
    api_key = get_key(str(model_def["api_key"]))
    if not api_key:
        raise RuntimeError(f"{model_def['api_key']} is required for {model_def['label']}")
    provider_name = str(model_def["provider"])
    model = str(model_def["model"])
    logger.info("Using cloud embedding provider %s model=%s dimension=%d", provider_name, model, dimension)
    if provider_name == "openai":
        from langchain_openai import OpenAIEmbeddings

        return OpenAIEmbeddings(model=model, dimensions=dimension, api_key=api_key)
    if provider_name == "google":
        from langchain_google_genai import GoogleGenerativeAIEmbeddings

        provider = GoogleGenerativeAIEmbeddings(model=model, google_api_key=api_key)
        default_dim = int(model_def["dimension"])
        if dimension == default_dim:
            return provider
        return _DimensionAdapter(provider, dimension)
    raise RuntimeError(f"Unsupported embedding provider: {provider_name}")
