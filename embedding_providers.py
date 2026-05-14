"""Embedding provider factory and resource management."""

from __future__ import annotations

import gc
import importlib.util
import logging
import sys
import threading
from typing import Any

from api_keys import get_key
from embedding_config import CLOUD_MODELS, LOCAL_MODELS, active_embedding_metadata, get_embedding_config
from langchain_core.embeddings import Embeddings
from stability import log_performance_snapshot

logger = logging.getLogger(__name__)

_provider_lock = threading.Lock()
_provider = None
_provider_key: tuple[Any, ...] | None = None

_BASE_LOCAL_PACKAGES = ("sentence_transformers", "langchain_huggingface")


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


def get_embedding_provider() -> Any:
    """Return the active embedding object with LangChain-compatible methods."""
    global _provider, _provider_key
    cfg = get_embedding_config()
    ensure_embedding_runtime_available(cfg)
    meta = active_embedding_metadata(cfg)
    key = (
        cfg.get("provider"),
        cfg.get("local_model"),
        cfg.get("cloud_model"),
        meta.get("dimension"),
    )
    if _provider is not None and _provider_key == key:
        return _provider
    with _provider_lock:
        if _provider is not None and _provider_key == key:
            return _provider
        release_embedding_resources("provider switch", collect=False)
        _provider = _build_provider(cfg)
        _provider_key = key
        return _provider


def release_embedding_resources(reason: str = "manual", *, collect: bool = True) -> None:
    """Clear cached embedding provider resources and ask native backends to release memory."""
    global _provider, _provider_key
    if _provider is None and not collect:
        return
    logger.info("perf: releasing embedding resources (%s)", reason)
    log_performance_snapshot("embedding-release-before")
    _provider = None
    _provider_key = None
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
            "This packaged runtime is incomplete; reinstall Thoth from a build that passes runtime dependency verification."
        )


def _build_local_provider(cfg: dict[str, Any]) -> Any:
    model_key = str(cfg.get("local_model") or "qwen3-0.6b")
    model_def = LOCAL_MODELS[model_key]
    dimension = int(active_embedding_metadata(cfg)["dimension"])
    ensure_embedding_runtime_available(cfg)
    model_kwargs = {"device": "cpu"}
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
            model_name=str(model_def["model"]),
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
