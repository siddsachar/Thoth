import json
import os
import pathlib

import ollama
from langchain_ollama import ChatOllama

DEFAULT_MODEL = "qwen3:14b"
DEFAULT_CONTEXT_SIZE = 32768

CONTEXT_SIZE_OPTIONS = [4096, 8192, 16384, 32768, 65536, 131072, 262144]
CONTEXT_SIZE_LABELS = {4096: "4K", 8192: "8K", 16384: "16K", 32768: "32K",
                       65536: "64K", 131072: "128K", 262144: "256K"}

# ── Persistent settings file ────────────────────────────────────────────────
_DATA_DIR = pathlib.Path(os.environ.get("THOTH_DATA_DIR", pathlib.Path.home() / ".thoth"))
_SETTINGS_PATH = _DATA_DIR / "model_settings.json"


def _load_settings() -> dict:
    """Load persisted model settings, or return defaults."""
    try:
        if _SETTINGS_PATH.exists():
            return json.loads(_SETTINGS_PATH.read_text())
    except Exception:
        pass
    return {}


def _save_settings(settings: dict):
    """Persist model settings to disk."""
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _SETTINGS_PATH.write_text(json.dumps(settings, indent=2))


# Initialise from saved settings (fall back to defaults for first run)
_saved = _load_settings()

POPULAR_MODELS = [
    "llama3.1:8b", "llama3.1:70b",
    "llama3.3:70b",
    "qwen3:8b", "qwen3:14b", "qwen3:30b",
    "qwen2.5:7b", "qwen2.5:14b", "qwen2.5:32b", "qwen2.5:72b",
    "gemma3:12b", "gemma3:27b",
    "gemma2:9b", "gemma2:27b",
    "mistral:7b",
    "mixtral:8x7b",
    "phi4:14b",
    "deepseek-r1:7b", "deepseek-r1:8b",
    "deepseek-r1:14b", "deepseek-r1:32b", "deepseek-r1:70b",
]

_current_model = _saved.get("model", DEFAULT_MODEL)
_num_ctx = _saved.get("context_size", DEFAULT_CONTEXT_SIZE)
_llm_instance = None


def get_llm() -> ChatOllama:
    """Return the current LLM instance, creating one if needed."""
    global _llm_instance
    if _llm_instance is None:
        _llm_instance = ChatOllama(model=_current_model, num_ctx=_num_ctx)
    return _llm_instance


def set_model(model_name: str):
    """Switch the active model (call after ensuring it's downloaded).
    Unloads the previous model from Ollama's VRAM before loading the new one."""
    global _current_model, _llm_instance
    # Unload the old model from Ollama memory
    if model_name != _current_model:
        try:
            ollama.generate(model=_current_model, prompt="", keep_alive=0)
        except Exception:
            pass  # best-effort; old model may already be unloaded
    _current_model = model_name
    _llm_instance = ChatOllama(model=model_name, num_ctx=_num_ctx)
    _save_settings({"model": _current_model, "context_size": _num_ctx})


def get_context_size() -> int:
    """Return the current context window size in tokens."""
    return _num_ctx


def set_context_size(size: int):
    """Change the context window size and recreate the LLM instance."""
    global _num_ctx, _llm_instance
    _num_ctx = size
    _llm_instance = ChatOllama(model=_current_model, num_ctx=_num_ctx)
    _save_settings({"model": _current_model, "context_size": _num_ctx})


def get_current_model() -> str:
    return _current_model


def list_local_models() -> list[str]:
    """Return names of models already downloaded in Ollama."""
    try:
        response = ollama.list()
        return sorted({m.model for m in response.models})
    except Exception:
        return []


def list_all_models() -> list[str]:
    """Return a combined, sorted list of local + popular models."""
    local = list_local_models()
    return sorted(set(local + POPULAR_MODELS))


def is_model_local(model_name: str) -> bool:
    """Check whether a model is already downloaded."""
    local = list_local_models()
    return any(
        model_name == m
        or f"{model_name}:latest" == m
        or model_name == m.split(":")[0]
        for m in local
    )


def pull_model(model_name: str):
    """Download a model from Ollama. Yields progress dicts when streamed."""
    return ollama.pull(model_name, stream=True)