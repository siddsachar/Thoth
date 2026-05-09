"""Embedding provider configuration for Thoth knowledge and documents."""

from __future__ import annotations

import json
import logging
import os
import pathlib
from copy import deepcopy
from typing import Any

logger = logging.getLogger(__name__)

DATA_DIR = pathlib.Path(os.environ.get("THOTH_DATA_DIR", pathlib.Path.home() / ".thoth"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

CONFIG_PATH = DATA_DIR / "embedding_config.json"

LOCAL_MODELS: dict[str, dict[str, Any]] = {
    "qwen3-0.6b": {
        "label": "Qwen3 0.6B",
        "model": "Qwen/Qwen3-Embedding-0.6B",
        "description": "Quality multilingual local embeddings. Runtime download.",
        "dimension": 1024,
        "context": 32768,
        "trust_remote_code": False,
    },
    "nomic-v1.5": {
        "label": "Nomic Embed Text v1.5",
        "model": "nomic-ai/nomic-embed-text-v1.5",
        "description": "Fast local embeddings with lower memory pressure. Runtime download.",
        "dimension": 768,
        "context": 8192,
        "trust_remote_code": True,
        "required_packages": ["einops"],
    },
    "mxbai-large-v1": {
        "label": "Mixedbread Embed Large v1",
        "model": "mixedbread-ai/mxbai-embed-large-v1",
        "description": "Strong English retrieval local embeddings. Runtime download.",
        "dimension": 1024,
        "context": 512,
        "trust_remote_code": False,
    },
}

CLOUD_MODELS: dict[str, dict[str, Any]] = {
    "openai:text-embedding-3-small": {
        "label": "OpenAI text-embedding-3-small",
        "provider": "openai",
        "model": "text-embedding-3-small",
        "dimension": 1536,
        "dimension_options": [256, 512, 1024, 1536],
        "api_key": "OPENAI_API_KEY",
    },
    "openai:text-embedding-3-large": {
        "label": "OpenAI text-embedding-3-large",
        "provider": "openai",
        "model": "text-embedding-3-large",
        "dimension": 3072,
        "dimension_options": [256, 512, 1024, 1536, 3072],
        "api_key": "OPENAI_API_KEY",
    },
    "google:gemini-embedding-001": {
        "label": "Google Gemini Embedding",
        "provider": "google",
        "model": "models/gemini-embedding-001",
        "dimension": 3072,
        "dimension_options": [768, 1536, 3072],
        "api_key": "GOOGLE_API_KEY",
    },
}

DEFAULT_CONFIG: dict[str, Any] = {
    "provider": "local",
    "local_model": "mxbai-large-v1",
    "cloud_model": "openai:text-embedding-3-small",
    "dimension": None,
    "batch_size": 32,
    "auto_unload": True,
}


def get_embedding_config() -> dict[str, Any]:
    """Return embedding config with defaults filled."""
    cfg = deepcopy(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            stored = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                cfg.update(stored)
        except (OSError, json.JSONDecodeError):
            logger.warning("Failed to load embedding config from %s", CONFIG_PATH, exc_info=True)
    cfg["provider"] = cfg.get("provider") if cfg.get("provider") in {"local", "cloud"} else "local"
    if cfg.get("local_model") not in LOCAL_MODELS:
        cfg["local_model"] = DEFAULT_CONFIG["local_model"]
    if cfg.get("cloud_model") not in CLOUD_MODELS:
        cfg["cloud_model"] = DEFAULT_CONFIG["cloud_model"]
    try:
        cfg["batch_size"] = max(1, min(256, int(cfg.get("batch_size") or DEFAULT_CONFIG["batch_size"])))
    except (TypeError, ValueError):
        cfg["batch_size"] = DEFAULT_CONFIG["batch_size"]
    if cfg.get("dimension") in ("", 0, "0"):
        cfg["dimension"] = None
    return cfg


def save_embedding_config(updates: dict[str, Any]) -> dict[str, Any]:
    """Persist embedding config updates and return the normalized config."""
    cfg = get_embedding_config()
    cfg.update(updates)
    cfg["provider"] = cfg.get("provider") if cfg.get("provider") in {"local", "cloud"} else "local"
    if cfg.get("local_model") not in LOCAL_MODELS:
        cfg["local_model"] = DEFAULT_CONFIG["local_model"]
    if cfg.get("cloud_model") not in CLOUD_MODELS:
        cfg["cloud_model"] = DEFAULT_CONFIG["cloud_model"]
    try:
        cfg["batch_size"] = max(1, min(256, int(cfg.get("batch_size") or DEFAULT_CONFIG["batch_size"])))
    except (TypeError, ValueError):
        cfg["batch_size"] = DEFAULT_CONFIG["batch_size"]
    if cfg.get("dimension") in ("", 0, "0"):
        cfg["dimension"] = None
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return cfg


def active_embedding_metadata(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return stable metadata that identifies compatible FAISS indexes."""
    cfg = config or get_embedding_config()
    if cfg.get("provider") == "cloud":
        model_def = CLOUD_MODELS[str(cfg["cloud_model"])]
        provider = str(model_def["provider"])
        model_id = str(model_def["model"])
        dimension = _normalized_dimension(cfg.get("dimension"), int(model_def["dimension"]))
    else:
        model_def = LOCAL_MODELS[str(cfg["local_model"])]
        provider = "local"
        model_id = str(model_def["model"])
        dimension = _normalized_dimension(cfg.get("dimension"), int(model_def["dimension"]))
    return {
        "version": 1,
        "provider": provider,
        "model": model_id,
        "dimension": dimension,
        "normalize": True,
    }


def metadata_path(vector_dir: pathlib.Path) -> pathlib.Path:
    return vector_dir / "embedding_metadata.json"


def read_index_metadata(vector_dir: pathlib.Path) -> dict[str, Any] | None:
    path = metadata_path(vector_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        logger.warning("Failed to read embedding metadata from %s", path, exc_info=True)
        return None


def write_index_metadata(vector_dir: pathlib.Path, metadata: dict[str, Any] | None = None) -> None:
    vector_dir.mkdir(parents=True, exist_ok=True)
    metadata_path(vector_dir).write_text(
        json.dumps(metadata or active_embedding_metadata(), indent=2),
        encoding="utf-8",
    )


def index_metadata_matches(vector_dir: pathlib.Path, metadata: dict[str, Any] | None = None) -> bool:
    current = metadata or active_embedding_metadata()
    stored = read_index_metadata(vector_dir)
    if not stored:
        return False
    return all(stored.get(key) == current.get(key) for key in ("provider", "model", "dimension", "normalize"))


def describe_active_embedding(config: dict[str, Any] | None = None) -> str:
    cfg = config or get_embedding_config()
    if cfg.get("provider") == "cloud":
        model_def = CLOUD_MODELS[str(cfg["cloud_model"])]
        return f"{model_def['label']} (cloud)"
    model_def = LOCAL_MODELS[str(cfg["local_model"])]
    return f"{model_def['label']} (local)"


def _normalized_dimension(value: Any, default: int) -> int:
    try:
        dimension = int(value or default)
    except (TypeError, ValueError):
        return default
    if dimension <= 0:
        return default
    return min(dimension, default)
