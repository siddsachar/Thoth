import contextvars
import json
import logging
import os
import pathlib
import threading

try:
    import ollama as _ollama_mod
except ImportError:
    _ollama_mod = None  # type: ignore[assignment]

try:
    from langchain_ollama import ChatOllama
except ImportError:
    ChatOllama = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

# ── Cloud provider URLs ─────────────────────────────────────────────────────
OPENAI_BASE_URL = "https://api.openai.com/v1"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1"
GOOGLE_GENAI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
XAI_BASE_URL = "https://api.x.ai/v1"

# ── Context-size heuristics (prefix-match, checked top-to-bottom) ───────────
# Used when the provider API doesn't expose context_length (e.g. OpenAI) and
# the model isn't in the OpenRouter cross-reference cache.  More-specific
# prefixes must come before shorter ones.  Covers OpenAI, Anthropic & Gemini.
_CONTEXT_HEURISTICS: list[tuple[str, int]] = [
    # ── OpenAI ────────────────────────────────────────────────────────
    ("gpt-4.1",       1_048_576),   # gpt-4.1 / mini / nano  — 1M
    ("gpt-4.5",       1_048_576),   # gpt-4.5 family          — 1M
    ("gpt-5",         1_048_576),   # gpt-5 / 5.4 etc         — 1M
    ("gpt-4o",          128_000),   # gpt-4o / 4o-mini        — 128K
    ("gpt-4-turbo",     128_000),   # gpt-4-turbo             — 128K
    ("gpt-4",             8_192),   # base gpt-4 (legacy)     — 8K
    ("gpt-3.5",          16_385),   # gpt-3.5-turbo           — 16K
    ("o1",              200_000),   # o1 / o1-mini / o1-pro   — 200K
    ("o3",              200_000),   # o3 / o3-mini / o3-pro   — 200K
    ("o4",              200_000),   # o4-mini etc              — 200K
    ("chatgpt-",        128_000),   # chatgpt- aliases        — 128K
    # ── Anthropic ─────────────────────────────────────────────────────
    ("claude-opus-4",  1_000_000),  # Opus 4.x                — 1M
    ("claude-sonnet-4",1_000_000),  # Sonnet 4.x              — 1M
    ("claude-haiku-4",   200_000),  # Haiku 4.x               — 200K
    ("claude-3-5",       200_000),  # Claude 3.5 family       — 200K
    ("claude-3",         200_000),  # Claude 3 family         — 200K
    ("claude-2",         100_000),  # Claude 2.x (legacy)     — 100K
    ("claude",           200_000),  # Catch-all Claude        — 200K
    # ── Google Gemini ─────────────────────────────────────────────────
    ("gemini-3",       1_048_576),  # Gemini 3.x              — 1M
    ("gemini-2.5",     1_048_576),  # Gemini 2.5 Flash/Pro    — 1M
    ("gemini-2.0",     1_048_576),  # Gemini 2.0              — 1M
    ("gemini-1.5-pro", 2_097_152),  # Gemini 1.5 Pro          — 2M
    ("gemini-1.5",     1_048_576),  # Gemini 1.5 Flash        — 1M
    ("gemini-1.0",        32_768),  # Legacy Gemini 1.0       — 32K
    ("gemini",         1_048_576),  # Catch-all Gemini        — 1M
    # ── xAI (Grok) ─────────────────────────────────────────────────────
    ("grok-4",         2_000_000),  # Grok 4 / 4.20           — 2M
    ("grok-3",           131_072),  # Grok 3 & 3-mini         — 131K
    ("grok-2",           131_072),  # Grok 2 family           — 131K
    ("grok",             131_072),  # Catch-all Grok          — 131K
]

_CLOUD_CONTEXT_FALLBACK = 256_000   # safe default for totally unknown models


def _estimate_context_heuristic(model_name: str) -> int:
    """Guess context size from the model name using prefix heuristics.

    Strips any ``provider/`` prefix (e.g. ``openai/gpt-4o`` → ``gpt-4o``)
    before matching.  Returns ``_CLOUD_CONTEXT_FALLBACK`` if nothing matches.
    """
    bare = _runtime_model_name(model_name).split("/")[-1]          # strip provider/ slug
    for prefix, ctx in _CONTEXT_HEURISTICS:
        if bare.startswith(prefix):
            return ctx
    return _CLOUD_CONTEXT_FALLBACK


def _parse_provider_model_ref(model_name: str | None) -> tuple[str, str] | None:
    try:
        from providers.selection import parse_model_ref

        return parse_model_ref(model_name)
    except Exception:
        return None


def _runtime_model_name(model_name: str | None) -> str:
    raw = str(model_name or "")
    parsed = _parse_provider_model_ref(raw)
    return parsed[1] if parsed else raw

# Prefixes considered chat-capable when filtering OpenAI /v1/models
_OPENAI_CHAT_PREFIXES = ("gpt-", "o1", "o3", "o4", "chatgpt-")
# Substrings that indicate a non-chat model — skip these
_OPENAI_SKIP_SUBSTRINGS = ("dall-e", "whisper", "tts", "embedding", "davinci",
                           "babbage", "moderation", "realtime", "audio",
                           "transcri", "search")

# ── Dynamic cloud model cache ───────────────────────────────────────────────
_cloud_model_cache: dict[str, dict] = {}   # model_id → {label, ctx, provider}
_cloud_cache_lock = threading.Lock()

# ── Context catalog (keyless OpenRouter public data) ────────────────────────
_context_catalog: dict[str, int] = {}      # model_id → context_length
_context_catalog_lock = threading.Lock()

# ── Trending Ollama models (fetched from ollama.com) ────────────────────────
_trending_ollama_cache: list[str] = []
_trending_fetched: bool = False

DEFAULT_MODEL = "qwen3:14b"
DEFAULT_CONTEXT_SIZE = 32768

CONTEXT_SIZE_OPTIONS = [16384, 32768, 65536, 131072, 262144]
CONTEXT_SIZE_LABELS = {16384: "16K", 32768: "32K",
                       65536: "64K", 131072: "128K", 262144: "256K"}

# Cloud-model context options (user-selectable cap — reduces cost / rate-limit pressure)
DEFAULT_CLOUD_CONTEXT_SIZE = 131072   # 128K — safe default for most API tiers
CLOUD_CONTEXT_SIZE_OPTIONS = [32768, 65536, 131072, 262144, 524288, 1048576]
CLOUD_CONTEXT_SIZE_LABELS = {
    32768: "32K", 65536: "64K", 131072: "128K",
    262144: "256K", 524288: "512K", 1048576: "1M",
}

# ── Persistent settings file ────────────────────────────────────────────────
_DATA_DIR = pathlib.Path(os.environ.get("THOTH_DATA_DIR", pathlib.Path.home() / ".thoth"))
_SETTINGS_PATH = _DATA_DIR / "model_settings.json"
_CLOUD_CACHE_PATH = _DATA_DIR / "cloud_models_cache.json"
_CONTEXT_CATALOG_PATH = _DATA_DIR / "context_catalog_cache.json"


def _load_settings() -> dict:
    """Load persisted model settings, or return defaults."""
    try:
        if _SETTINGS_PATH.exists():
            return json.loads(_SETTINGS_PATH.read_text())
    except Exception:
        logger.warning("Failed to load model settings from %s", _SETTINGS_PATH, exc_info=True)
    return {}


def _save_settings(settings: dict):
    """Persist model settings to disk."""
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _SETTINGS_PATH.write_text(json.dumps(settings, indent=2))


def _load_cloud_cache() -> dict:
    """Load persisted cloud model cache from disk."""
    try:
        if _CLOUD_CACHE_PATH.exists():
            data = json.loads(_CLOUD_CACHE_PATH.read_text())
            if isinstance(data, dict):
                return data
    except Exception:
        logger.warning("Failed to load cloud cache from %s", _CLOUD_CACHE_PATH, exc_info=True)
    return {}


def _save_cloud_cache():
    """Persist current cloud model cache to disk."""
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        with _cloud_cache_lock:
            _CLOUD_CACHE_PATH.write_text(json.dumps(_cloud_model_cache))
    except Exception:
        logger.warning("Failed to save cloud cache to %s", _CLOUD_CACHE_PATH, exc_info=True)


def _load_context_catalog() -> dict[str, int]:
    """Load persisted context catalog from disk."""
    try:
        if _CONTEXT_CATALOG_PATH.exists():
            data = json.loads(_CONTEXT_CATALOG_PATH.read_text())
            if isinstance(data, dict):
                return data
    except Exception:
        logger.warning("Failed to load context catalog from %s",
                       _CONTEXT_CATALOG_PATH, exc_info=True)
    return {}


def _save_context_catalog():
    """Persist current context catalog to disk."""
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        with _context_catalog_lock:
            _CONTEXT_CATALOG_PATH.write_text(json.dumps(_context_catalog))
    except Exception:
        logger.warning("Failed to save context catalog to %s",
                       _CONTEXT_CATALOG_PATH, exc_info=True)


# Initialise from saved settings (fall back to defaults for first run)
_saved = _load_settings()

# Load persisted cloud cache so is_cloud_model() works before refresh
_cloud_model_cache.update(_load_cloud_cache())

# Load persisted context catalog so context resolution works before live fetch
_context_catalog.update(_load_context_catalog())

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
# Clamp legacy values below the new minimum to the smallest option
if _num_ctx < CONTEXT_SIZE_OPTIONS[0]:
    _num_ctx = CONTEXT_SIZE_OPTIONS[0]
_cloud_num_ctx = _saved.get("cloud_context_size", DEFAULT_CLOUD_CONTEXT_SIZE)
if _cloud_num_ctx < CLOUD_CONTEXT_SIZE_OPTIONS[0]:
    _cloud_num_ctx = CLOUD_CONTEXT_SIZE_OPTIONS[0]
_llm_instance = None
_model_max_ctx_cache: dict[str, int | None] = {}  # model_name → max context


def get_llm():
    """Return the current LLM instance, creating one if needed.

    If the default model is a cloud model, returns the appropriate
    ``ChatOpenAI`` instance.  Otherwise returns ``ChatOllama``.
    """
    global _llm_instance
    if _llm_instance is None:
        if is_cloud_model(_current_model):
            _llm_instance = _get_cloud_llm(_current_model)
        else:
            runtime_model = _runtime_model_name(_current_model)
            logger.info("Creating LLM instance: model=%s, num_ctx=%s", runtime_model, _num_ctx)
            _llm_instance = ChatOllama(model=runtime_model, num_ctx=_num_ctx, reasoning=True)
    return _llm_instance


_override_llm_cache: dict[tuple[str, int], object] = {}  # model → ChatOllama or ChatOpenAI


def get_llm_for(model_name: str, num_ctx: int | None = None):
    """Return an LLM for a specific model (not the global singleton).

    For local (Ollama) models, returns a ``ChatOllama``.
    For cloud (OpenRouter) models, returns a ``ChatOpenAI`` pointed at
    the OpenRouter API.  Results are cached per (model, ctx) pair.
    """
    if is_cloud_model(model_name):
        return _get_cloud_llm(model_name)

    runtime_model = _runtime_model_name(model_name)
    if num_ctx is None:
        model_max = get_model_max_context(runtime_model)
        if model_max is not None:
            num_ctx = min(model_max, _num_ctx)
        else:
            num_ctx = _num_ctx
    key = (runtime_model, num_ctx)
    if key not in _override_llm_cache:
        logger.info("Creating override LLM: model=%s, num_ctx=%s", runtime_model, num_ctx)
        _override_llm_cache[key] = ChatOllama(model=runtime_model, num_ctx=num_ctx, reasoning=True)
    return _override_llm_cache[key]


def get_model_max_context(model_name: str | None = None) -> int | None:
    """Query Ollama for the model's native max context length.

    For cloud models, returns the hardcoded context size from the catalog.
    Returns the context_length from model metadata, or *None* if it
    cannot be determined.  Results are cached per model name.
    """
    raw_name = model_name or _current_model
    if is_cloud_model(raw_name):
        return get_cloud_model_context(raw_name)
    name = _runtime_model_name(raw_name)
    if name in _model_max_ctx_cache:
        return _model_max_ctx_cache[name]
    if not _ollama_mod:
        _model_max_ctx_cache[name] = None
        return None
    try:
        info = _ollama_mod.show(name)
        mi = info.modelinfo or {}
        arch = mi.get("general.architecture", "")
        ctx = mi.get(f"{arch}.context_length") if arch else None
        _model_max_ctx_cache[name] = int(ctx) if ctx is not None else None
    except Exception:
        logger.debug("Could not query max context for model %s", name, exc_info=True)
        _model_max_ctx_cache[name] = None
    return _model_max_ctx_cache[name]


def set_model(model_name: str):
    """Switch the active model.  Accepts both local Ollama and cloud model IDs.

    For local models: unloads the previous model from Ollama's VRAM.
    For cloud models: just updates the setting (no Ollama interaction).
    """
    global _current_model, _llm_instance
    if model_name != _current_model:
        logger.info("Switching model: %s → %s", _current_model, model_name)
        # Unload previous local model from Ollama memory
        if not is_cloud_model(_current_model) and _ollama_mod:
            try:
                _ollama_mod.generate(model=_runtime_model_name(_current_model), prompt="", keep_alive=0)
            except Exception:
                logger.debug("Could not unload previous model %s", _current_model, exc_info=True)
    _current_model = model_name
    if is_cloud_model(model_name):
        _llm_instance = _get_cloud_llm(model_name)
    else:
        _llm_instance = ChatOllama(model=_runtime_model_name(model_name), num_ctx=_num_ctx, reasoning=True)
    _save_settings({"model": _current_model, "context_size": _num_ctx,
                    "cloud_context_size": _cloud_num_ctx})


# Thread-local model override — allows agent.py to propagate the per-thread
# cloud model override so that get_context_size() (and everything downstream:
# get_tool_budget, _keep_browser_snapshots, tool budgets) automatically uses
# the correct context window without every caller needing an explicit argument.
_active_model_override: contextvars.ContextVar[str] = contextvars.ContextVar(
    "active_model_override", default=""
)


def set_active_model_override(name: str) -> None:
    """Set the thread-local model override (called by agent.py before execution)."""
    _active_model_override.set(name)


def get_context_size(model_name: str | None = None) -> int:
    """Return the *effective* context size for the given (or current) model.

    - **Cloud models** use ``min(user_cloud_cap, model_native_max)``.
      The user-configurable cap reduces cost and rate-limit pressure.
    - **Local (Ollama) models** use ``min(user_setting, model_native_max)``
      because ``num_ctx`` directly controls VRAM usage.

    Resolution order for the model name:
    1. Explicit *model_name* argument.
    2. Thread-local ``_active_model_override`` (set by agent.py).
    3. Global ``_current_model``.
    """
    name = model_name or _active_model_override.get() or _current_model
    model_max = get_model_max_context(name)
    if is_cloud_model(name):
        # Cloud: cap at user-selected limit (reduces cost / rate-limit pressure)
        native = model_max if model_max is not None else _estimate_context_heuristic(name)
        return min(_cloud_num_ctx, native)
    # Local: respect user's VRAM-controlling setting
    if model_max is not None:
        return min(_num_ctx, model_max)
    return _num_ctx


def get_tool_budget(fraction: float, *,
                    floor: int = 10_000, ceiling: int = 200_000) -> int:
    """Dynamic char budget for a tool result, scaled to the model's context.

    Returns a *character* limit suitable for string slicing.
    Assumes ~3 chars per token for the tokens-to-chars conversion.
    Clamped between *floor* and *ceiling* to prevent extremes.
    """
    ctx = get_context_size()
    return min(ceiling, max(floor, int(ctx * fraction * 3)))


def get_user_context_size() -> int:
    """Return the raw user-selected context size (before model capping)."""
    return _num_ctx


def get_cloud_context_size() -> int:
    """Return the raw user-selected cloud context cap."""
    return _cloud_num_ctx


def set_cloud_context_size(size: int):
    """Change the cloud context cap and recreate the LLM instance."""
    global _cloud_num_ctx, _llm_instance
    logger.info("Cloud context size changed: %s → %s", _cloud_num_ctx, size)
    _cloud_num_ctx = size
    if is_cloud_model(_current_model):
        _llm_instance = _get_cloud_llm(_current_model)
    _save_settings({"model": _current_model, "context_size": _num_ctx,
                    "cloud_context_size": _cloud_num_ctx})


def set_context_size(size: int):
    """Change the context window size and recreate the LLM instance."""
    global _num_ctx, _llm_instance
    logger.info("Context size changed: %s → %s", _num_ctx, size)
    _num_ctx = size
    if is_cloud_model(_current_model):
        _llm_instance = _get_cloud_llm(_current_model)
    else:
        _llm_instance = ChatOllama(model=_runtime_model_name(_current_model), num_ctx=_num_ctx, reasoning=True)
    _save_settings({"model": _current_model, "context_size": _num_ctx,
                    "cloud_context_size": _cloud_num_ctx})


def get_current_model() -> str:
    return _current_model


def _ollama_host_port() -> tuple[str, int]:
    """Return the TCP host/port configured for the local Ollama daemon."""
    from urllib.parse import urlparse

    raw = (os.environ.get("OLLAMA_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    parsed = urlparse(raw if "://" in raw else f"//{raw}")
    host = parsed.hostname or raw
    try:
        port = parsed.port or 11434
    except ValueError:
        port = 11434
    return host, port


def _ollama_reachable(timeout: float = 1.0) -> bool:
    """Fast TCP probe to check if the Ollama server is listening."""
    import socket
    host, port = _ollama_host_port()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, TimeoutError):
        return False


def list_local_models() -> list[str]:
    """Return names of models already downloaded in Ollama."""
    if not _ollama_mod or not _ollama_reachable():
        return []
    try:
        response = _ollama_mod.list()
        return sorted({m.model for m in response.models})
    except Exception:
        logger.debug("Could not list local Ollama models", exc_info=True)
        return []


def list_all_models() -> list[str]:
    """Return a combined, sorted list of local + popular + trending models."""
    local = list_local_models()
    return sorted(set(local + POPULAR_MODELS + _trending_ollama_cache))


def get_trending_models() -> list[str]:
    """Return the set of trending model names (for icon display)."""
    return list(_trending_ollama_cache)


def fetch_trending_ollama_models() -> list[str]:
    """Fetch trending models from ollama.com/api/tags.

    Returns a list of model name strings.  Safe to call from any thread.
    Results are cached in-memory for the session.
    """
    global _trending_ollama_cache, _trending_fetched
    if _trending_fetched:
        return _trending_ollama_cache
    try:
        import httpx
        resp = httpx.get("https://ollama.com/api/tags", timeout=10)
        resp.raise_for_status()
        data = resp.json().get("models", [])
        names = [m.get("name", "") for m in data if m.get("name")]
        _trending_ollama_cache = names
        _trending_fetched = True
        logger.info("Fetched %d trending Ollama models", len(names))
        return names
    except Exception as exc:
        logger.debug("Could not fetch trending Ollama models: %s", exc)
        _trending_fetched = True  # don't retry on failure
        return []


def is_model_local(model_name: str) -> bool:
    """Check whether a model is already downloaded."""
    parsed = _parse_provider_model_ref(model_name)
    if parsed and parsed[0] not in {"local", "ollama"}:
        return False
    runtime_model = _runtime_model_name(model_name)
    local = list_local_models()
    return any(
        runtime_model == m
        or f"{runtime_model}:latest" == m
        or runtime_model == m.split(":")[0]
        for m in local
    )


def _looks_like_cloud_model(model_name: str) -> bool:
    """Heuristic: return True if *model_name* looks like a cloud model.

    Only used as a last-resort fallback when the persisted cache is empty
    AND the in-memory cache hasn't been populated yet.
    """
    if "/" in model_name:                       # OpenRouter format: provider/model
        return True
    if any(model_name.startswith(p) for p in _OPENAI_CHAT_PREFIXES):
        # Exclude known Ollama models that happen to share a prefix
        family = model_name.split(":")[0]
        if family in _TOOL_COMPATIBLE_FAMILIES:
            return False
        return True
    return False


def _infer_cloud_provider(model_name: str) -> str | None:
    """Infer a cloud provider from a model ID when the cache is unavailable."""
    parsed = _parse_provider_model_ref(model_name)
    if parsed:
        provider_id, _model_id = parsed
        return None if provider_id in {"local", "ollama"} else provider_id
    model_name = _runtime_model_name(model_name)
    _sync_custom_model_cache()
    if model_name in _cloud_model_cache:
        return _cloud_model_cache[model_name]["provider"]
    if "/" in model_name:
        return "openrouter"

    bare_name = model_name.split("/")[-1]
    if any(bare_name.startswith(prefix) for prefix in _OPENAI_CHAT_PREFIXES):
        family = bare_name.split(":")[0]
        if family not in _TOOL_COMPATIBLE_FAMILIES:
            return "openai"
    if bare_name.startswith("claude"):
        return "anthropic"
    if bare_name.startswith("gemini"):
        return "google"
    if bare_name.startswith("grok"):
        return "xai"
    return None


def is_cloud_model(model_name: str) -> bool:
    """Return True if *model_name* is a known cloud model.

    Uses the persisted cache (loaded from disk at startup), then falls back
    to provider-prefix inference so a saved cloud default survives cache or
    key outages.
    """
    return _infer_cloud_provider(model_name) is not None


def get_cloud_provider(model_name: str) -> str | None:
    """Return ``'openai'``, ``'openrouter'``, ``'anthropic'``, ``'google'``, or ``None``."""
    return _infer_cloud_provider(model_name)


# ── Provider emoji mapping ───────────────────────────────────────────────────
_PROVIDER_EMOJI: dict[str | None, str] = {
    "openai": "⬡",
    "codex": "C",
    "openrouter": "🌐",
    "anthropic": "🔶",
    "google": "💎",
    "xai": "𝕏",
    None: "☁️",  # fallback for unknown cloud
}


def get_provider_emoji(model_name: str) -> str:
    """Return a provider-specific emoji for a model.

    Local models get 🖥️, cloud models get a provider-specific icon.
    """
    if not is_cloud_model(model_name):
        return "🖥️"
    prov = get_cloud_provider(model_name)
    return _PROVIDER_EMOJI.get(prov, _PROVIDER_EMOJI[None])


def is_cloud_available() -> bool:
    """Return True if any cloud API key is configured."""
    from providers.runtime import list_configured_provider_ids
    return bool(list_configured_provider_ids())


def is_openai_available() -> bool:
    """Return True if an OpenAI API key is configured."""
    from api_keys import get_key
    return bool(get_key("OPENAI_API_KEY"))


def is_openrouter_available() -> bool:
    """Return True if an OpenRouter API key is configured."""
    from api_keys import get_key
    return bool(get_key("OPENROUTER_API_KEY"))


def is_anthropic_available() -> bool:
    """Return True if an Anthropic API key is configured."""
    from api_keys import get_key
    return bool(get_key("ANTHROPIC_API_KEY"))


def is_google_available() -> bool:
    """Return True if a Google AI API key is configured."""
    from api_keys import get_key
    return bool(get_key("GOOGLE_API_KEY"))


def is_xai_available() -> bool:
    """Return True if an xAI API key is configured."""
    from api_keys import get_key
    return bool(get_key("XAI_API_KEY"))


def list_cloud_models(provider: str | None = None) -> list[str]:
    """Return cached cloud model IDs, optionally filtered by provider."""
    _sync_custom_model_cache()
    if provider:
        return [m for m, info in _cloud_model_cache.items() if info["provider"] == provider]
    return list(_cloud_model_cache.keys())


def _sync_custom_model_cache() -> None:
    try:
        from providers.custom import custom_model_cache_entries
        entries = custom_model_cache_entries()
    except Exception:
        return
    if not entries:
        return
    with _cloud_cache_lock:
        _cloud_model_cache.update(entries)


def list_starred_cloud_models() -> list[str]:
    """Return cloud models the user has starred (for the thread picker)."""
    from providers.selection import list_quick_model_ids, migrate_legacy_starred_models
    from api_keys import get_cloud_config
    _sync_custom_model_cache()
    migrate_legacy_starred_models(cloud_models=_cloud_model_cache.keys())
    quick_models = [m for m in list_quick_model_ids("chat") if m in _cloud_model_cache]
    if quick_models:
        return quick_models
    starred = set(get_cloud_config().get("starred_models", []))
    return [m for m in _cloud_model_cache if m in starred]


def star_cloud_model(model_id: str) -> None:
    """Add a model to the starred list."""
    from api_keys import get_cloud_config, set_cloud_config
    from providers.selection import add_quick_choice_for_model
    starred = list(get_cloud_config().get("starred_models", []))
    if model_id not in starred:
        starred.append(model_id)
        set_cloud_config("starred_models", starred)
    add_quick_choice_for_model(model_id, source="legacy_starred_cloud")


def unstar_cloud_model(model_id: str) -> None:
    """Remove a model from the starred list."""
    from api_keys import get_cloud_config, set_cloud_config
    from providers.selection import remove_quick_choice_for_model
    starred = list(get_cloud_config().get("starred_models", []))
    if model_id in starred:
        starred.remove(model_id)
        set_cloud_config("starred_models", starred)
    remove_quick_choice_for_model(model_id)


def get_cloud_model_context(model_name: str) -> int:
    """Return the context window size for a cloud model.

    Resolution order:
    1. Cached value (from OpenRouter API or previous fetch).
    2. Context catalog (public OpenRouter data, no key needed).
    3. Prefix-based heuristic covering OpenAI / Anthropic / Gemini.
    4. Safe fallback (256K).
    """
    parsed = _parse_provider_model_ref(model_name)
    provider_id = parsed[0] if parsed else None
    runtime_model = _runtime_model_name(model_name)
    info = _cloud_model_cache.get(runtime_model)
    if info and (not provider_id or info.get("provider") == provider_id):
        return info["ctx"]
    if provider_id == "codex":
        try:
            from providers.codex import list_codex_model_infos

            for model_info in list_codex_model_infos():
                if model_info.model_id == runtime_model and model_info.context_window:
                    return int(model_info.context_window)
        except Exception:
            pass
    return _catalog_or_heuristic(runtime_model)


def list_cloud_vision_models() -> list[str]:
    """Return cloud model IDs that support vision / image input."""
    from providers.capabilities import snapshot_supports_surface
    return [
        m for m, info in _cloud_model_cache.items()
        if info.get("vision")
        or (isinstance(info.get("capabilities_snapshot"), dict) and bool(info.get("capabilities_snapshot"))
            and snapshot_supports_surface(info.get("capabilities_snapshot"), "vision"))
    ]


def is_cloud_vision_model(model_name: str) -> bool:
    """Return True if *model_name* is a cloud model with vision support."""
    runtime_model = _runtime_model_name(model_name)
    info = _cloud_model_cache.get(runtime_model)
    if not info:
        return False
    from providers.capabilities import snapshot_supports_surface
    snapshot = info.get("capabilities_snapshot")
    return bool(
        info.get("vision")
        or (isinstance(snapshot, dict) and bool(snapshot) and snapshot_supports_surface(snapshot, "vision"))
    )


def _get_cloud_llm(model_name: str):
    """Return a cached LLM for a cloud model.

    OpenAI-direct models use ``ChatOpenAI``.  OpenRouter models use
    ``ChatOpenRouter`` which correctly surfaces ``reasoning_content``
    in ``additional_kwargs`` for reasoning models.
    """
    from providers.runtime import create_chat_model

    provider = get_cloud_provider(model_name)
    runtime_model = _runtime_model_name(model_name)
    ctx = get_cloud_model_context(model_name)
    key = (f"{provider or 'openrouter'}:{runtime_model}", ctx)
    if key in _override_llm_cache:
        return _override_llm_cache[key]

    logger.info("Creating cloud LLM: model=%s via %s", runtime_model, provider or "openrouter")

    _override_llm_cache[key] = create_chat_model(runtime_model, provider)
    return _override_llm_cache[key]


# ── Dynamic model fetching ──────────────────────────────────────────────────


def fetch_context_catalog() -> int:
    """Fetch the public OpenRouter model catalog (no API key needed).

    Populates ``_context_catalog`` with ``model_id → context_length``
    for every model OpenRouter lists.  Returns the number of entries.
    Safe to call from background threads.
    """
    import httpx

    try:
        resp = httpx.get(f"{OPENROUTER_BASE_URL}/models", timeout=15)
        resp.raise_for_status()
        data = resp.json().get("data", [])
    except Exception as exc:
        logger.warning("Failed to fetch context catalog: %s", exc)
        return 0

    count = 0
    with _context_catalog_lock:
        _context_catalog.clear()
        for m in data:
            mid = m.get("id", "")
            ctx = m.get("context_length")
            if mid and ctx and isinstance(ctx, int) and ctx > 0:
                _context_catalog[mid] = ctx
                count += 1
    _save_context_catalog()
    logger.info("Context catalog: %d models indexed", count)
    return count


def validate_openrouter_key(api_key: str) -> bool:
    """Validate an OpenRouter API key by hitting the auth endpoint.

    Returns True if the key is accepted, False otherwise.
    """
    import httpx

    try:
        resp = httpx.get(
            f"{OPENROUTER_BASE_URL}/auth/key",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        return resp.status_code == 200
    except Exception:
        return False


def validate_anthropic_key(api_key: str) -> bool:
    """Validate an Anthropic API key by listing models.

    Returns True if the key is accepted, False otherwise.
    """
    import httpx

    try:
        resp = httpx.get(
            f"{ANTHROPIC_BASE_URL}/models",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            params={"limit": 1},
            timeout=10,
        )
        return resp.status_code == 200
    except Exception:
        return False


def validate_google_key(api_key: str) -> bool:
    """Validate a Google AI API key by listing models.

    Returns True if the key is accepted, False otherwise.
    """
    import httpx

    try:
        resp = httpx.get(
            f"{GOOGLE_GENAI_BASE_URL}/models",
            params={"key": api_key, "pageSize": 1},
            timeout=10,
        )
        return resp.status_code == 200
    except Exception:
        return False


def validate_xai_key(api_key: str) -> bool:
    """Validate an xAI API key by attempting to list models.

    Returns True if the key is accepted, False otherwise.
    Uses the same ``_fetch_xai_models`` path to validate: if at least one
    model can be fetched, the key is valid.
    """
    import httpx

    try:
        resp = httpx.get(
            f"{XAI_BASE_URL}/language-models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        if resp.status_code == 200:
            return True
        logger.warning("xAI key validation: %d — %s", resp.status_code, resp.text[:200])
        return False
    except Exception as exc:
        logger.warning("xAI key validation error: %s", exc)
        return False


def _catalog_or_heuristic(model_id: str) -> int:
    """Resolve context size for any model via catalog → heuristic → fallback.

    Checks ``_context_catalog`` first (public OpenRouter data, covers all
    providers).  Falls back to prefix heuristic, then ``_CLOUD_CONTEXT_FALLBACK``.
    """
    # Try exact match in catalog (e.g. "openai/gpt-4o")
    with _context_catalog_lock:
        cat_val = _context_catalog.get(f"openai/{model_id}")
        if cat_val:
            return cat_val
        # Also try bare ID for OpenRouter-style keys
        cat_val = _context_catalog.get(model_id)
        if cat_val:
            return cat_val
    return _estimate_context_heuristic(model_id)


# Anthropic model ID substrings to skip (non-chat or internal models)
_ANTHROPIC_SKIP_SUBSTRINGS = ("embed", "tokenizer")

# Google model substrings to skip
_GOOGLE_SKIP_SUBSTRINGS = ("embed", "aqa", "tts")


def fetch_cloud_models(provider: str) -> int:
    """Fetch available models from *provider*.

    Supported providers: ``'openai'``, ``'openrouter'``, ``'anthropic'``,
    ``'google'``.  Populates ``_cloud_model_cache``.  Returns the number
    of models found.  Safe to call from background threads.
    """
    import httpx
    from api_keys import get_key
    from providers.capabilities import model_supports_surface
    from providers.catalog import model_info_from_metadata, model_info_to_cache_entry

    if provider == "openai":
        api_key = get_key("OPENAI_API_KEY")
        if not api_key:
            return 0
        url = f"{OPENAI_BASE_URL}/models"
        headers = {"Authorization": f"Bearer {api_key}"}
    elif provider == "openrouter":
        api_key = get_key("OPENROUTER_API_KEY")
        if not api_key:
            return 0
        url = f"{OPENROUTER_BASE_URL}/models"
        headers = {"Authorization": f"Bearer {api_key}"}
    elif provider == "anthropic":
        api_key = get_key("ANTHROPIC_API_KEY")
        if not api_key:
            return 0
        return _fetch_anthropic_models(api_key)
    elif provider == "google":
        api_key = get_key("GOOGLE_API_KEY")
        if not api_key:
            return 0
        return _fetch_google_models(api_key)
    elif provider == "xai":
        api_key = get_key("XAI_API_KEY")
        if not api_key:
            return 0
        return _fetch_xai_models(api_key)
    else:
        return 0

    try:
        resp = httpx.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json().get("data", [])
    except Exception as exc:
        logger.warning("Failed to fetch %s models: %s", provider, exc)
        return 0

    count = 0
    with _cloud_cache_lock:
        if provider == "openai":
            for m in data:
                mid = m.get("id", "")
                model_info = model_info_from_metadata(
                    "openai", mid, m,
                    display_name=mid,
                    context_window=_catalog_or_heuristic(mid),
                )
                if not any(model_supports_surface(model_info, surface) for surface in ("chat", "image", "video")):
                    continue
                entry = model_info_to_cache_entry(model_info)
                _cloud_model_cache[mid] = entry
                count += 1
        else:  # openrouter
            for m in data:
                mid = m.get("id", "")
                name = m.get("name", mid)
                ctx = m.get("context_length", 128_000)
                # Only include models with a '/' (provider/model format)
                if "/" not in mid:
                    continue
                model_info = model_info_from_metadata(
                    "openrouter", mid, m,
                    display_name=name,
                    context_window=ctx,
                )
                if not any(model_supports_surface(model_info, surface) for surface in ("chat", "image", "video")):
                    continue
                _cloud_model_cache[mid] = model_info_to_cache_entry(model_info)
                count += 1
    logger.info("Fetched %d %s models", count, provider)
    return count


def _fetch_anthropic_models(api_key: str) -> int:
    """Fetch models from the Anthropic ``/v1/models`` endpoint.

    Handles pagination via ``after_id``.  Uses ``max_input_tokens`` for
    context size when available, falling back to ``_catalog_or_heuristic``.
    """
    import httpx
    from providers.catalog import model_info_from_metadata, model_info_to_cache_entry

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    count = 0
    after_id: str | None = None

    try:
        while True:
            params: dict[str, str | int] = {"limit": 100}
            if after_id:
                params["after_id"] = after_id
            resp = httpx.get(
                f"{ANTHROPIC_BASE_URL}/models",
                headers=headers, params=params, timeout=15,
            )
            resp.raise_for_status()
            body = resp.json()
            models = body.get("data", [])

            with _cloud_cache_lock:
                for m in models:
                    mid = m.get("id", "")
                    if not mid:
                        continue
                    if any(s in mid for s in _ANTHROPIC_SKIP_SUBSTRINGS):
                        continue
                    display = m.get("display_name", mid)
                    # Use API context size if available; fall back to catalog/heuristic
                    api_ctx = m.get("max_input_tokens", 0)
                    ctx = api_ctx if api_ctx and api_ctx > 0 else _catalog_or_heuristic(mid)
                    # Vision from capabilities
                    caps = m.get("capabilities", {})
                    has_vision = bool(
                        caps.get("image_input", {}).get("supported")
                    )
                    metadata = dict(m)
                    metadata["vision"] = has_vision
                    _cloud_model_cache[mid] = model_info_to_cache_entry(model_info_from_metadata(
                        "anthropic", mid, metadata,
                        display_name=display,
                        context_window=ctx,
                    ))
                    count += 1

            if not body.get("has_more"):
                break
            after_id = body.get("last_id")
            if not after_id:
                break
    except Exception as exc:
        logger.warning("Failed to fetch anthropic models: %s", exc)

    logger.info("Fetched %d anthropic models", count)
    return count


def _fetch_google_models(api_key: str) -> int:
    """Fetch models from the Google Generative AI ``models.list`` endpoint.

    Includes chat, image, and video-capable models. Media generation models are
    often exposed through non-chat APIs, so they must stay in the provider cache
    for the image/video tools even when they are hidden from chat pickers.
    Uses ``inputTokenLimit`` for context size.
    """
    import httpx
    from providers.capabilities import model_supports_surface
    from providers.catalog import model_info_from_metadata, model_info_to_cache_entry

    count = 0
    page_token: str | None = None

    try:
        while True:
            params: dict[str, str | int] = {"key": api_key, "pageSize": 100}
            if page_token:
                params["pageToken"] = page_token
            resp = httpx.get(
                f"{GOOGLE_GENAI_BASE_URL}/models",
                params=params, timeout=15,
            )
            resp.raise_for_status()
            body = resp.json()
            models = body.get("models", [])

            with _cloud_cache_lock:
                for m in models:
                    name = m.get("name", "")        # e.g. "models/gemini-2.5-flash"
                    mid = name.removeprefix("models/")  # → "gemini-2.5-flash"
                    if not mid:
                        continue
                    methods = m.get("supportedGenerationMethods", [])
                    if any(s in mid for s in _GOOGLE_SKIP_SUBSTRINGS):
                        continue
                    display = m.get("displayName", mid)
                    ctx = m.get("inputTokenLimit", 0)
                    if not ctx or ctx <= 0:
                        ctx = _catalog_or_heuristic(mid)
                    metadata = dict(m)
                    metadata["supportedGenerationMethods"] = methods
                    metadata["vision"] = "generateContent" in methods
                    model_info = model_info_from_metadata("google", mid, metadata, display_name=display, context_window=ctx)
                    if not any(model_supports_surface(model_info, surface) for surface in ("chat", "image", "video")):
                        continue
                    _cloud_model_cache[mid] = model_info_to_cache_entry(model_info)
                    count += 1

            page_token = body.get("nextPageToken")
            if not page_token:
                break
    except Exception as exc:
        logger.warning("Failed to fetch google models: %s", exc)

    logger.info("Fetched %d google models", count)
    return count


# Substrings that mark non-chat xAI models to drop from the provider cache.
_XAI_SKIP_SUBSTRINGS: tuple[str, ...] = ()


def _fetch_xai_models(api_key: str) -> int:
    """Fetch language models from the xAI ``/v1/language-models`` endpoint.

    Uses ``input_modalities`` for vision detection.  Context size is resolved
    via the OpenRouter catalog / prefix heuristic (the xAI API does not expose
    context window sizes).
    """
    import httpx
    from providers.catalog import model_info_from_metadata, model_info_to_cache_entry

    try:
        resp = httpx.get(
            f"{XAI_BASE_URL}/language-models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15,
        )
        resp.raise_for_status()
        body = resp.json()
        models = body.get("models", [])
    except Exception as exc:
        logger.warning("Failed to fetch xai models: %s", exc)
        return 0

    count = 0
    with _cloud_cache_lock:
        for m in models:
            mid = m.get("id", "")
            if not mid:
                continue
            if any(s in mid for s in _XAI_SKIP_SUBSTRINGS):
                continue
            display = mid  # xAI doesn't return a display name
            ctx = _catalog_or_heuristic(mid)
            has_vision = "image" in m.get("input_modalities", [])
            metadata = dict(m)
            metadata["vision"] = has_vision
            _cloud_model_cache[mid] = model_info_to_cache_entry(model_info_from_metadata(
                "xai", mid, metadata,
                display_name=display,
                context_window=ctx,
            ))
            count += 1

    logger.info("Fetched %d xai models", count)
    return count


def refresh_cloud_models() -> int:
    """Clear cache and re-fetch from all configured providers.

    Also refreshes the context catalog (keyless) for accurate context sizes.
    If the current default model is temporarily absent from the refreshed
    cache, keep the user's saved choice instead of rewriting it to a local
    fallback.
    """
    global _current_model, _llm_instance
    # Remember current model before clearing
    _prev_model = _current_model
    _was_cloud = is_cloud_model(_prev_model)

    # Fetch context catalog first so OpenAI models get accurate sizes
    fetch_context_catalog()
    with _cloud_cache_lock:
        _cloud_model_cache.clear()
    total = 0
    total += fetch_cloud_models("openai")
    total += fetch_cloud_models("openrouter")
    total += fetch_cloud_models("anthropic")
    total += fetch_cloud_models("google")
    total += fetch_cloud_models("xai")
    _save_cloud_cache()

    # Do not rewrite the user's default just because a provider refresh missed
    # it. Keyring migrations, offline starts, and provider list gaps can all be
    # temporary, and clobbering model_settings.json makes new threads surprise
    # the user with the first local Ollama model.
    if _was_cloud and _prev_model not in _cloud_model_cache:
        logger.warning(
            "Default cloud model '%s' was not returned by refresh; preserving saved default.",
            _prev_model,
        )
        _llm_instance = None  # lazy-recreate on next get_llm()

    return total


def is_tool_compatible(model_name: str) -> bool:
    """Check whether a model is in the known tool-compatible set.

    All cloud models (fetched dynamically) support tool calling."""
    if is_cloud_model(model_name):
        info = _cloud_model_cache.get(model_name, {})
        snapshot = info.get("capabilities_snapshot") if isinstance(info, dict) else {}
        if isinstance(snapshot, dict) and snapshot:
            return snapshot.get("tool_calling") is not False
        return True
    try:
        from providers.ollama import is_ollama_tool_capable
        return is_ollama_tool_capable(model_name)
    except Exception:
        pass
    family = model_name.split(":")[0]
    return family in _TOOL_COMPATIBLE_FAMILIES


def check_tool_support(model_name: str) -> bool:
    """Send a minimal tool-call request to verify the model supports tools.

    Returns True if the model accepts tools, False if it rejects them (400).
    """
    if not _ollama_mod:
        return False
    try:
        _ollama_mod.chat(
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
        logger.debug("Tool support check for %s failed: %s", model_name, exc)
        return True  # Network or other error — don't block


def pull_model(model_name: str):
    """Download a model from Ollama. Yields progress dicts when streamed."""
    if not _ollama_mod:
        raise RuntimeError("Ollama is not installed")
    return _ollama_mod.pull(model_name, stream=True)