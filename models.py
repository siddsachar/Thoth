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
    # ── Qwen family ──────────────────────────────────────────────────────
    "qwen3:8b", "qwen3:14b", "qwen3:30b", "qwen3:32b", "qwen3:235b",
    "qwen3.5:9b", "qwen3.5:27b", "qwen3.5:35b", "qwen3.5:122b",
    "qwen3-coder:30b",
    # ── Llama family ─────────────────────────────────────────────────────
    "llama3.1:8b", "llama3.1:70b", "llama3.1:405b",
    "llama3.3:70b",
    "llama3-groq-tool-use:8b", "llama3-groq-tool-use:70b",
    # ── Mistral family ───────────────────────────────────────────────────
    "mistral:7b",
    "mistral-nemo:12b",
    "mistral-small:22b", "mistral-small:24b",
    "mistral-small3.1:24b",
    "mistral-small3.2:24b",
    "mistral-large:123b",
    "mixtral:8x7b", "mixtral:8x22b",
    "magistral:24b",
    "ministral-3:8b", "ministral-3:14b",
    # ── Other tool-capable models ────────────────────────────────────────
    "rnj-1:8b",
    "glm-4.7-flash:30b",
    "nemotron-3-nano:30b",
    "nemotron:70b",
    "devstral-small-2:24b",
    "devstral-2:123b",
    "olmo-3.1:32b",
    "lfm2:24b",
    "gpt-oss:20b", "gpt-oss:120b",
    "firefunction-v2:70b",
]

# Set of all model *family* prefixes known to support Ollama tool calling.
# Used to flag downloaded models NOT in this set with a ⚠️ warning.
_TOOL_COMPATIBLE_FAMILIES: set[str] = {
    m.split(":")[0] for m in POPULAR_MODELS
}

_current_model = _saved.get("model", DEFAULT_MODEL)
_num_ctx = _saved.get("context_size", DEFAULT_CONTEXT_SIZE)
_llm_instance = None
_model_max_ctx_cache: dict[str, int | None] = {}  # model_name → max context


def get_llm() -> ChatOllama:
    """Return the current LLM instance, creating one if needed."""
    global _llm_instance
    if _llm_instance is None:
        _llm_instance = ChatOllama(model=_current_model, num_ctx=_num_ctx)
    return _llm_instance


def get_model_max_context(model_name: str | None = None) -> int | None:
    """Query Ollama for the model's native max context length.

    Returns the context_length from model metadata, or *None* if it
    cannot be determined.  Results are cached per model name.
    """
    name = model_name or _current_model
    if name in _model_max_ctx_cache:
        return _model_max_ctx_cache[name]
    try:
        info = ollama.show(name)
        mi = info.modelinfo or {}
        arch = mi.get("general.architecture", "")
        ctx = mi.get(f"{arch}.context_length") if arch else None
        _model_max_ctx_cache[name] = int(ctx) if ctx is not None else None
    except Exception:
        _model_max_ctx_cache[name] = None
    return _model_max_ctx_cache[name]


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
    """Return the *effective* context size — the minimum of the user's
    setting and the model's native max context length.

    This is the value that trimming and the token counter should use.
    """
    model_max = get_model_max_context()
    if model_max is not None:
        return min(_num_ctx, model_max)
    return _num_ctx


def get_user_context_size() -> int:
    """Return the raw user-selected context size (before model capping)."""
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


def is_tool_compatible(model_name: str) -> bool:
    """Check whether a model family is in the known tool-compatible set."""
    family = model_name.split(":")[0]
    return family in _TOOL_COMPATIBLE_FAMILIES


def check_tool_support(model_name: str) -> bool:
    """Send a minimal tool-call request to verify the model supports tools.

    Returns True if the model accepts tools, False if it rejects them (400).
    """
    try:
        ollama.chat(
            model=model_name,
            messages=[{"role": "user", "content": "hi"}],
            tools=[{
                "type": "function",
                "function": {
                    "name": "_ping",
                    "description": "test",
                    "parameters": {"type": "object", "properties": {}},
                },
            }],
        )
        return True
    except Exception as exc:
        if "does not support tools" in str(exc) or "400" in str(exc):
            return False
        return True  # Network or other error — don't block


def pull_model(model_name: str):
    """Download a model from Ollama. Yields progress dicts when streamed."""
    return ollama.pull(model_name, stream=True)