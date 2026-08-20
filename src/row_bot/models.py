import contextvars
from dataclasses import dataclass
from datetime import datetime, timezone
import ipaddress
import json
import logging
import os
import threading
from urllib.parse import urlparse
import urllib.error
import urllib.request

from row_bot.data_paths import get_row_bot_data_dir

try:
    import ollama as _ollama_mod
except ImportError:
    _ollama_mod = None  # type: ignore[assignment]

ChatOllama = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

# ── Cloud provider URLs ─────────────────────────────────────────────────────
OPENAI_BASE_URL = "https://api.openai.com/v1"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
REQUESTY_BASE_URL = "https://router.requesty.ai/v1"
ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1"
GOOGLE_GENAI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
XAI_BASE_URL = "https://api.x.ai/v1"
MINIMAX_ANTHROPIC_BASE_URL = "https://api.minimax.io/anthropic"
OLLAMA_CLOUD_BASE_URL = "https://ollama.com"
ATLASCLOUD_BASE_URL = "https://api.atlascloud.ai/v1"

# Provider-qualified maintained capacities. These are deliberately narrow:
# an unknown model must not inherit a plausible-looking generic capacity.
_EXACT_CONTEXT_LIMITS: dict[str, tuple[tuple[str, int], ...]] = {
    "openai": (
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
    ),
    "anthropic": (
    ("claude-opus-4",  1_000_000),  # Opus 4.x                — 1M
    ("claude-sonnet-4",1_000_000),  # Sonnet 4.x              — 1M
    ("claude-haiku-4",   200_000),  # Haiku 4.x               — 200K
    ("claude-3-5",       200_000),  # Claude 3.5 family       — 200K
    ("claude-3",         200_000),  # Claude 3 family         — 200K
    ("claude-2",         100_000),  # Claude 2.x (legacy)     — 100K
    ("claude",           200_000),  # Catch-all Claude        — 200K
    ),
    "google": (
    ("gemini-3",       1_048_576),  # Gemini 3.x              — 1M
    ("gemini-2.5",     1_048_576),  # Gemini 2.5 Flash/Pro    — 1M
    ("gemini-2.0",     1_048_576),  # Gemini 2.0              — 1M
    ("gemini-1.5-pro", 2_097_152),  # Gemini 1.5 Pro          — 2M
    ("gemini-1.5",     1_048_576),  # Gemini 1.5 Flash        — 1M
    ("gemini-1.0",        32_768),  # Legacy Gemini 1.0       — 32K
    ("gemini",         1_048_576),  # Catch-all Gemini        — 1M
    ),
    "xai": (
    ("grok-4",         2_000_000),  # Grok 4 / 4.20           — 2M
    ("grok-3",           131_072),  # Grok 3 & 3-mini         — 131K
    ("grok-2",           131_072),  # Grok 2 family           — 131K
    ("grok",             131_072),  # Catch-all Grok          — 131K
    ),
    "minimax": (
    ("minimax-m3",     1_000_000),  # MiniMax M3 Anthropic-compatible model
    ("minimax-m2",       204_800),  # MiniMax M2.x Anthropic-compatible models
    ),
}


def _maintained_context_limit(provider_id: str, model_name: str) -> int:
    """Return an exact maintained provider-qualified capacity, or zero."""
    bare = _runtime_model_name(model_name).split("/")[-1].lower()  # strip provider/ slug
    aliases = {
        "codex": "openai",
        "claude_subscription": "anthropic",
        "xai_oauth": "xai",
    }
    provider = aliases.get(str(provider_id or "").strip(), str(provider_id or "").strip())
    for prefix, ctx in _EXACT_CONTEXT_LIMITS.get(provider, ()):
        if bare.startswith(prefix):
            return ctx
    return 0


def _parse_provider_model_ref(model_name: str | None) -> tuple[str, str] | None:
    try:
        from row_bot.providers.selection import parse_model_ref

        return parse_model_ref(model_name)
    except Exception:
        return None


def _runtime_model_name(model_name: str | None) -> str:
    raw = str(model_name or "")
    parsed = _parse_provider_model_ref(raw)
    return parsed[1] if parsed else raw


def _ollama_runtime_model_name(model_name: str | None) -> str:
    """Resolve local Ollama family aliases to an installed daemon tag.

    Ollama accepts and displays model families such as ``llama3`` in some UI
    paths, but the daemon inventory commonly exposes only ``llama3:latest``.
    Keep explicit tags untouched, and only expand a bare family when the local
    daemon has one unambiguous installed match.
    """
    runtime_model = _runtime_model_name(model_name).strip()
    if not runtime_model or ":" in runtime_model:
        return runtime_model
    try:
        local_models = list_local_models()
    except Exception:
        return runtime_model
    if runtime_model in local_models:
        return runtime_model
    latest = f"{runtime_model}:latest"
    if latest in local_models:
        return latest
    family_matches = [
        name for name in local_models
        if name.split(":", 1)[0] == runtime_model
    ]
    return family_matches[0] if len(family_matches) == 1 else runtime_model


def _provider_qualified_cloud_cache_key(provider_id: str | None, model_id: str | None) -> str:
    provider = str(provider_id or "").strip()
    model = str(model_id or "").strip()
    return f"model:{provider}:{model}" if provider and model else ""


def _cloud_cache_entry_for(model_name: str | None, provider_id: str | None = None) -> dict | None:
    parsed = _parse_provider_model_ref(model_name)
    runtime_model = _runtime_model_name(model_name)
    provider = provider_id or (parsed[0] if parsed else None)
    qualified_key = _provider_qualified_cloud_cache_key(provider, runtime_model)
    if qualified_key:
        info = _cloud_model_cache.get(qualified_key)
        if isinstance(info, dict):
            cached_provider = str(info.get("provider") or "")
            if not cached_provider or cached_provider == provider:
                return info
    info = _cloud_model_cache.get(runtime_model)
    if isinstance(info, dict):
        cached_provider = str(info.get("provider") or "")
        if not provider or not cached_provider or cached_provider == provider:
            return info
    return None

# Prefixes considered chat-capable when filtering OpenAI /v1/models
_OPENAI_CHAT_PREFIXES = ("gpt-", "o1", "o3", "o4", "chatgpt-")
# Substrings that indicate a non-chat model — skip these
_OPENAI_SKIP_SUBSTRINGS = ("dall-e", "whisper", "tts", "embedding", "davinci",
                           "babbage", "moderation", "realtime", "audio",
                           "transcri", "search")

# ── Dynamic cloud model cache ───────────────────────────────────────────────
_cloud_model_cache: dict[str, dict] = {}   # model_id → {label, ctx, provider}
_cloud_cache_lock = threading.Lock()

REFRESHABLE_CLOUD_PROVIDER_IDS: tuple[str, ...] = (
    "openai",
    "codex",
    "ollama_cloud",
    "openrouter",
    "requesty",
    "anthropic",
    "google",
    "xai",
    "xai_oauth",
    "minimax",
    "opencode_zen",
    "opencode_go",
    "atlascloud",
    "claude_subscription",
)

_SUBSCRIPTION_CATALOG_PROVIDER_IDS = frozenset({"codex", "claude_subscription", "xai_oauth"})


@dataclass(frozen=True)
class ProviderCatalogRefreshResult:
    provider_id: str
    status: str
    effective_count: int
    update_count: int = 0
    live_count: int = 0
    preserved_count: int = 0
    source: str = ""
    message: str = ""

    def status_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "count": self.effective_count,
            "live_count": self.live_count,
            "preserved_count": self.preserved_count,
            "source": self.source,
            "message": self.message,
        }


def _provider_cloud_cache_entries(provider_id: str) -> dict[str, dict]:
    provider = str(provider_id or "").strip()
    with _cloud_cache_lock:
        return {
            str(model_id): dict(info)
            for model_id, info in _cloud_model_cache.items()
            if isinstance(info, dict) and _cloud_cache_entry_belongs_to_provider(str(model_id), info, provider)
        }


def _cloud_cache_entry_belongs_to_provider(model_id: str, info: dict, provider_id: str) -> bool:
    cached_provider = str(info.get("provider") or "")
    if cached_provider:
        return cached_provider == provider_id
    if provider_id != "minimax":
        return False
    key = str(model_id or "")
    if key.startswith("model:minimax:"):
        return True
    return _runtime_model_name(key).split("/")[-1].lower().startswith("minimax")


def _replace_provider_cloud_cache_entries(provider_id: str, entries: dict[str, dict]) -> int:
    provider = str(provider_id or "").strip()
    staged = {
        str(model_id): dict(info)
        for model_id, info in entries.items()
        if model_id and isinstance(info, dict) and str(info.get("provider") or "") == provider
    }
    if not staged:
        return 0
    with _cloud_cache_lock:
        stale_keys = [
            model_id
            for model_id, info in _cloud_model_cache.items()
            if isinstance(info, dict) and _cloud_cache_entry_belongs_to_provider(str(model_id), info, provider)
        ]
        for model_id in stale_keys:
            _cloud_model_cache.pop(model_id, None)
        _cloud_model_cache.update(staged)
    return len(staged)


def _provider_catalog_cache_stamp(provider_id: str) -> str:
    if provider_id not in _SUBSCRIPTION_CATALOG_PROVIDER_IDS:
        return ""
    try:
        from row_bot.providers.config import load_provider_config

        cache = load_provider_config().get("providers", {}).get(provider_id, {}).get("catalog_cache")
        return str(cache.get("fetched_at") or "") if isinstance(cache, dict) else ""
    except Exception:
        return ""

# ── Context catalog (keyless OpenRouter public data) ────────────────────────
_context_catalog: dict[str, int] = {}      # model_id → context_length
_context_catalog_lock = threading.Lock()

# ── Deprecated Ollama discovery cache (kept as inert compatibility state) ───
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
def _coerce_context_size(
    value,
    default: int,
    *,
    allowed: list[int] | tuple[int, ...] | None = None,
    minimum: int | None = None,
) -> int:
    """Return a numeric context size from persisted/UI values."""
    fallback = int(default or 0)
    try:
        if isinstance(value, bool):
            raise ValueError
        if isinstance(value, (int, float)):
            parsed = int(value)
        else:
            text = str(value or "").strip().lower().replace(",", "").replace("_", "")
            multiplier = 1
            if text.endswith("k"):
                multiplier = 1_000
                text = text[:-1]
            elif text.endswith("m"):
                multiplier = 1_000_000
                text = text[:-1]
            parsed = int(float(text) * multiplier)
    except (TypeError, ValueError):
        parsed = fallback
    if allowed:
        allowed_ints = sorted(int(item) for item in allowed)
        if parsed in allowed_ints:
            return parsed
        if parsed < allowed_ints[0]:
            return allowed_ints[0]
        return min(allowed_ints, key=lambda item: abs(item - parsed))
    if minimum is not None and parsed < int(minimum):
        return int(minimum)
    return parsed if parsed > 0 else fallback


_DATA_DIR = get_row_bot_data_dir()
_SETTINGS_PATH = _DATA_DIR / "model_settings.json"
_CLOUD_CACHE_PATH = _DATA_DIR / "cloud_models_cache.json"
_CONTEXT_CATALOG_PATH = _DATA_DIR / "context_catalog_cache.json"
_CONTEXT_POLICY_VERSION = 2


def _valid_context_value(value, *, allowed: list[int], default: int) -> int:
    return _coerce_context_size(value, default, allowed=allowed)


def _migrate_context_settings(settings: dict | None) -> tuple[dict, bool]:
    """Return merge-preserving v2 context settings and whether they changed.

    Historical 32K local and 128K cloud defaults are intentionally interpreted
    as Auto because old files cannot distinguish a default from an explicit
    selection. Non-default values remain explicit.
    """
    original = dict(settings or {}) if isinstance(settings, dict) else {}
    migrated = dict(original)
    try:
        version = int(migrated.get("context_policy_version") or 1)
    except (TypeError, ValueError):
        version = 1

    old_local = _valid_context_value(
        migrated.get("context_size", DEFAULT_CONTEXT_SIZE),
        allowed=CONTEXT_SIZE_OPTIONS,
        default=DEFAULT_CONTEXT_SIZE,
    )
    local_mode = str(migrated.get("local_context_mode") or "").strip().lower()
    if version < _CONTEXT_POLICY_VERSION or local_mode not in {"auto", "fixed"}:
        local_mode = "auto" if old_local == DEFAULT_CONTEXT_SIZE else "fixed"
    migrated["local_context_mode"] = local_mode
    migrated["context_size"] = old_local

    override = migrated.get("cloud_context_override")
    if version < _CONTEXT_POLICY_VERSION or "cloud_context_override" not in migrated:
        old_cloud = _valid_context_value(
            migrated.get("cloud_context_size", DEFAULT_CLOUD_CONTEXT_SIZE),
            allowed=CLOUD_CONTEXT_SIZE_OPTIONS,
            default=DEFAULT_CLOUD_CONTEXT_SIZE,
        )
        override = None if old_cloud == DEFAULT_CLOUD_CONTEXT_SIZE else old_cloud
    elif override is not None:
        override = _valid_context_value(
            override,
            allowed=CLOUD_CONTEXT_SIZE_OPTIONS,
            default=DEFAULT_CLOUD_CONTEXT_SIZE,
        )
    migrated["cloud_context_override"] = override
    migrated["context_policy_version"] = _CONTEXT_POLICY_VERSION
    return migrated, migrated != original


def _load_settings() -> dict:
    """Load persisted model settings, or return defaults."""
    raw: dict = {}
    try:
        if _SETTINGS_PATH.exists():
            loaded = json.loads(_SETTINGS_PATH.read_text())
            raw = loaded if isinstance(loaded, dict) else {}
    except Exception:
        logger.warning("Failed to load model settings from %s", _SETTINGS_PATH, exc_info=True)
    migrated, changed = _migrate_context_settings(raw)
    if changed and _SETTINGS_PATH.exists():
        try:
            _DATA_DIR.mkdir(parents=True, exist_ok=True)
            _SETTINGS_PATH.write_text(json.dumps(migrated, indent=2))
        except Exception:
            logger.warning("Failed to persist model settings migration", exc_info=True)
    return migrated


def _save_settings(settings: dict):
    """Merge and persist model settings without dropping unrelated keys."""
    existing: dict = {}
    try:
        if _SETTINGS_PATH.exists():
            loaded = json.loads(_SETTINGS_PATH.read_text())
            existing = loaded if isinstance(loaded, dict) else {}
    except Exception:
        logger.warning("Failed to merge existing model settings", exc_info=True)
    existing, _changed = _migrate_context_settings(existing)
    existing.update(dict(settings or {}))
    merged, _changed = _migrate_context_settings(existing)
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _SETTINGS_PATH.write_text(json.dumps(merged, indent=2))


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

try:
    from row_bot.providers.selection import model_choice_value as _canonical_model_choice_value

    _current_model = _canonical_model_choice_value(_saved.get("model", DEFAULT_MODEL))
except Exception:
    _current_model = _saved.get("model", DEFAULT_MODEL)
_num_ctx = _coerce_context_size(
    _saved.get("context_size", DEFAULT_CONTEXT_SIZE),
    DEFAULT_CONTEXT_SIZE,
    allowed=CONTEXT_SIZE_OPTIONS,
)
_local_context_mode = str(_saved.get("local_context_mode") or "auto")
_cloud_num_ctx = _coerce_context_size(
    _saved.get("cloud_context_override") or DEFAULT_CLOUD_CONTEXT_SIZE,
    DEFAULT_CLOUD_CONTEXT_SIZE,
    allowed=CLOUD_CONTEXT_SIZE_OPTIONS,
)
_cloud_context_override = _saved.get("cloud_context_override")
if _cloud_context_override is not None:
    _cloud_context_override = _coerce_context_size(
        _cloud_context_override,
        DEFAULT_CLOUD_CONTEXT_SIZE,
        allowed=CLOUD_CONTEXT_SIZE_OPTIONS,
    )
_llm_instance = None
_model_max_ctx_cache: dict[str, int | None] = {}  # model_name → max context
_observed_local_context: dict[str, int] = {}


@dataclass(frozen=True)
class ContextPolicy:
    model_ref: str
    provider_id: str
    runtime_model: str
    native_max: int | None
    user_cap: int
    effective_context: int
    policy_kind: str
    cap_source: str
    request_application: str
    native_limit_tokens: int | None = None
    requested_limit_tokens: int | None = None
    effective_limit_tokens: int | None = None
    limit_kind: str = "unknown"
    usable_input_tokens: int | None = None
    compact_at_tokens: int | None = None
    capacity_source: str = "unknown"
    capacity_state: str = "unavailable"
    observed_limit_tokens: int | None = None
    warning: str = ""


def _context_settings_payload(**extra) -> dict:
    payload = {
        "context_policy_version": _CONTEXT_POLICY_VERSION,
        "local_context_mode": _local_context_mode,
        "context_size": _num_ctx,
        "cloud_context_override": _cloud_context_override,
        "cloud_context_size": _cloud_num_ctx,
    }
    payload.update(extra)
    return payload


def _normalize_ollama_client_host(host: str) -> str:
    normalized = (host or "127.0.0.1").strip()
    if normalized == "0.0.0.0":
        return "127.0.0.1"
    if normalized == "::":
        return "::1"
    return normalized


def _format_ollama_base_url(host: str, port: int, scheme: str = "http") -> str:
    formatted_host = host
    try:
        if isinstance(ipaddress.ip_address(host), ipaddress.IPv6Address):
            formatted_host = f"[{host}]"
    except ValueError:
        pass
    return f"{scheme or 'http'}://{formatted_host}:{port}"


def _ollama_endpoint_parts() -> tuple[str, int, str]:
    raw = (os.environ.get("OLLAMA_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    parsed = urlparse(raw if "://" in raw else f"//{raw}")
    host = _normalize_ollama_client_host(parsed.hostname or raw)
    try:
        port = parsed.port or 11434
    except ValueError:
        port = 11434
    return host, port, parsed.scheme or "http"


def _ollama_base_url() -> str:
    """Return a client-safe Ollama base URL derived from OLLAMA_HOST."""
    host, port, scheme = _ollama_endpoint_parts()
    return _format_ollama_base_url(host, port, scheme)


def _ollama_client():
    if not _ollama_mod:
        return None
    client_factory = getattr(_ollama_mod, "Client", None)
    if callable(client_factory):
        return client_factory(host=_ollama_base_url())
    return _ollama_mod


def _ollama_http_json(path: str, payload: dict | None = None, *, timeout: float = 4.0) -> dict:
    url = f"{_ollama_base_url().rstrip('/')}/{str(path or '').lstrip('/')}"
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if payload is not None else "GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    parsed = json.loads(raw.decode("utf-8") or "{}")
    return parsed if isinstance(parsed, dict) else {}


def _chat_ollama(model: str, **kwargs):
    global ChatOllama
    if ChatOllama is None:
        try:
            from langchain_ollama import ChatOllama as _ChatOllama
        except ImportError as exc:
            raise RuntimeError("langchain-ollama is not installed") from exc
        ChatOllama = _ChatOllama
    if "reasoning" not in kwargs:
        try:
            from row_bot.providers.ollama import is_ollama_reasoning_model

            if is_ollama_reasoning_model(model):
                kwargs["reasoning"] = True
        except Exception:
            pass
    return ChatOllama(model=model, base_url=_ollama_base_url(), **kwargs)


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
            runtime_model = _ollama_runtime_model_name(_current_model)
            num_ctx = _local_num_ctx_for(_current_model)
            logger.info("Creating LLM instance: model=%s, num_ctx=%s", runtime_model, num_ctx)
            _llm_instance = _chat_ollama(model=runtime_model, num_ctx=num_ctx)
    return _llm_instance


_override_llm_cache: dict[tuple[str, int, str], object] = {}


def clear_llm_cache() -> None:
    """Drop cached chat model clients so provider credential changes take effect."""
    global _llm_instance
    _llm_instance = None
    _override_llm_cache.clear()


def _local_num_ctx_for(model_name: str | None) -> int:
    """Return the Ollama ``num_ctx`` after applying native model caps."""
    model_max = get_model_max_context(model_name)
    requested = DEFAULT_CONTEXT_SIZE if _local_context_mode == "auto" else _num_ctx
    user_ctx = _coerce_context_size(requested, DEFAULT_CONTEXT_SIZE, minimum=CONTEXT_SIZE_OPTIONS[0])
    model_ctx = _coerce_context_size(model_max, 0) if model_max else None
    runtime_model = _ollama_runtime_model_name(model_name)
    observed = _observed_local_context.get(runtime_model)
    candidates = [user_ctx]
    if model_ctx and model_ctx > 0:
        candidates.append(model_ctx)
    if observed and observed > 0:
        candidates.append(observed)
    return min(candidates)


def get_llm_for(model_name: str, num_ctx: int | None = None, *, reasoning_plan=None):
    """Return an LLM for a specific model (not the global singleton).

    For local (Ollama) models, returns a ``ChatOllama``.
    For cloud (OpenRouter) models, returns a ``ChatOpenAI`` pointed at
    the OpenRouter API.  Results are cached per (model, ctx) pair.
    """
    if is_cloud_model(model_name):
        return _get_cloud_llm(model_name, reasoning_plan=reasoning_plan)

    runtime_model = _ollama_runtime_model_name(model_name)
    if num_ctx is None:
        num_ctx = _local_num_ctx_for(model_name)
    fingerprint = getattr(reasoning_plan, "fingerprint", "reasoning:provider-default")
    key = (runtime_model, num_ctx, fingerprint)
    if key not in _override_llm_cache:
        logger.info("Creating override LLM: model=%s, num_ctx=%s", runtime_model, num_ctx)
        kwargs = {}
        if reasoning_plan is not None and not reasoning_plan.is_default:
            from row_bot.providers.runtime import _reasoning_constructor_kwargs

            kwargs.update(_reasoning_constructor_kwargs(reasoning_plan, provider="ollama"))
        _override_llm_cache[key] = _chat_ollama(model=runtime_model, num_ctx=num_ctx, **kwargs)
    return _override_llm_cache[key]


def get_model_max_context(model_name: str | None = None) -> int | None:
    """Query Ollama for the model's native max context length.

    For cloud models, returns the hardcoded context size from the catalog.
    Returns the context_length from model metadata, or *None* if it
    cannot be determined.  Results are cached per model name.
    """
    raw_name = model_name or _current_model
    resolved = _resolved_context_identity(raw_name)
    if resolved and resolved.provider_id.startswith("custom_openai_"):
        endpoint = resolved.endpoint or {}
        models = endpoint.get("models") if isinstance(endpoint, dict) else []
        if isinstance(models, list):
            for item in models:
                if not isinstance(item, dict):
                    continue
                item_model = str(item.get("model_id") or item.get("id") or "")
                if item_model != resolved.runtime_model:
                    continue
                ctx = _coerce_context_size(item.get("context_window") or item.get("ctx"), 0)
                return ctx if ctx > 0 else None
        probe = endpoint.get("last_probe") if isinstance(endpoint.get("last_probe"), dict) else {}
        ctx = _coerce_context_size(probe.get("context_window"), 0)
        return ctx if ctx > 0 else None
    if is_cloud_model(raw_name):
        try:
            cloud_context = int(get_cloud_model_context(raw_name))
        except (TypeError, ValueError):
            return None
        return cloud_context if cloud_context > 0 else None
    name = _ollama_runtime_model_name(raw_name)
    if name in _model_max_ctx_cache:
        return _model_max_ctx_cache[name]
    client = _ollama_client()
    metadata: dict | None = None
    try:
        if client:
            info = client.show(name)
            metadata = getattr(info, "modelinfo", None) or getattr(info, "model_info", None) or {}
    except Exception:
        logger.debug("Could not query max context for model %s", name, exc_info=True)
    if metadata is None:
        try:
            info = _ollama_http_json("/api/show", {"model": name})
            metadata = info.get("model_info") or info.get("modelinfo") or {}
        except Exception:
            logger.debug("Could not query max context over Ollama HTTP for model %s", name, exc_info=True)
            metadata = {}
    arch = str(metadata.get("general.architecture") or "")
    ctx = metadata.get(f"{arch}.context_length") if arch else None
    if ctx is None:
        for key, value in metadata.items():
            if str(key).endswith(".context_length"):
                ctx = value
                break
    try:
        _model_max_ctx_cache[name] = int(ctx) if ctx is not None else None
    except (TypeError, ValueError):
        _model_max_ctx_cache[name] = None
    cached_context = _model_max_ctx_cache[name]
    return cached_context if cached_context is not None and cached_context > 0 else None


def set_model(model_name: str):
    """Switch the active model.  Accepts both local Ollama and cloud model IDs.

    For local models: unloads the previous model from Ollama's VRAM.
    For cloud models: just updates the setting (no Ollama interaction).
    """
    global _current_model, _llm_instance
    if model_name != _current_model:
        logger.info("Switching model: %s → %s", _current_model, model_name)
        # Unload previous local model from Ollama memory
        client = _ollama_client()
        if not is_cloud_model(_current_model) and client:
            try:
                client.generate(model=_ollama_runtime_model_name(_current_model), prompt="", keep_alive=0)
            except Exception:
                logger.debug("Could not unload previous model %s", _current_model, exc_info=True)
    _current_model = model_name
    if is_cloud_model(model_name):
        _llm_instance = _get_cloud_llm(model_name)
    else:
        _llm_instance = _chat_ollama(
            model=_ollama_runtime_model_name(model_name),
            num_ctx=_local_num_ctx_for(model_name),
        )
    _save_settings(_context_settings_payload(model=_current_model))


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


def _resolved_context_identity(model_name: str):
    try:
        from row_bot.providers.resolution import resolve_provider_config

        return resolve_provider_config(model_name, allow_legacy_local=True)
    except Exception:
        return None


def get_context_policy(model_name: str | None = None) -> ContextPolicy:
    """Return the resolved context policy for the given (or active) model."""
    name = model_name or _active_model_override.get() or _current_model
    resolved = _resolved_context_identity(name)
    provider_id = resolved.provider_id if resolved else (get_cloud_provider(name) or "ollama")
    runtime_model = resolved.runtime_model if resolved else _runtime_model_name(name)
    model_ref_value = resolved.selection_ref if resolved else name
    remote_policy = bool(resolved and resolved.execution_location != "local") or is_cloud_model(name)
    if resolved and resolved.execution_location == "local" and provider_id.startswith("custom_openai_"):
        remote_policy = False

    model_max = get_model_max_context(name)
    model_max_int = _coerce_context_size(model_max, 0) if model_max else 0
    native_max = model_max_int if model_max_int > 0 else None
    cache_entry = _cloud_cache_entry_for(name, provider_id) if remote_policy else None
    capacity_source = "provider_metadata" if native_max else "unknown"
    endpoint = resolved.endpoint if resolved and isinstance(resolved.endpoint, dict) else {}
    if native_max is None and provider_id.startswith("custom_openai_"):
        profile_default = _coerce_context_size(endpoint.get("unknown_context_fallback"), 0)
        if profile_default > 0:
            native_max = profile_default
            capacity_source = "profile_default"
    if native_max and cache_entry:
        capacity_source = str(cache_entry.get("source") or "provider_catalog")
    elif native_max and _maintained_context_limit(provider_id, runtime_model) == native_max:
        capacity_source = "maintained_table"

    explicit_endpoint_override = _coerce_context_size(
        endpoint.get("context_override"),
        0,
    )
    if remote_policy:
        requested_limit = (
            explicit_endpoint_override
            if explicit_endpoint_override > 0
            else int(_cloud_context_override)
            if _cloud_context_override is not None
            else None
        )
    else:
        requested_limit = _coerce_context_size(
            DEFAULT_CONTEXT_SIZE if _local_context_mode == "auto" else _num_ctx,
            DEFAULT_CONTEXT_SIZE,
            minimum=CONTEXT_SIZE_OPTIONS[0],
        )

    observed_limit = None
    if provider_id == "ollama" and not remote_policy:
        observed_limit = _observed_local_context.get(_ollama_runtime_model_name(name))

    candidates = [
        int(value)
        for value in (requested_limit, native_max, observed_limit)
        if value is not None and int(value) > 0
    ]
    if remote_policy and requested_limit is None and native_max is None:
        # Keep the persisted setting on Auto, but give unknown remote models a
        # conservative, explicitly disclosed application limit.  This is trim
        # and compaction authority only; it is never presented as provider
        # metadata or a user-selected override.
        effective_limit = DEFAULT_CLOUD_CONTEXT_SIZE
        capacity_source = "app_fallback"
    else:
        effective_limit = min(candidates) if candidates else None

    if requested_limit is not None and (
        explicit_endpoint_override > 0 or (remote_policy and _cloud_context_override is not None)
    ):
        capacity_source = "advanced_override"
    elif observed_limit is not None and effective_limit == observed_limit:
        capacity_source = "observed_allocation"
    elif not remote_policy and effective_limit is not None and native_max is None:
        capacity_source = "requested_local_allocation"

    limit_kind = "input_only" if provider_id in {"anthropic", "claude_subscription", "google"} else "combined"
    if cache_entry and str(cache_entry.get("limit_kind") or "") in {"input_only", "combined"}:
        limit_kind = str(cache_entry["limit_kind"])
    usable_input = None
    compact_at = None
    if effective_limit is not None and effective_limit > 0:
        usable_input = int(effective_limit * (0.95 if limit_kind == "input_only" else 0.85))
        compact_at = int(effective_limit * 0.75)
    capacity_state = "ready" if usable_input and compact_at else "unavailable"
    warning = ""
    if observed_limit and requested_limit and observed_limit < requested_limit:
        warning = (
            f"The loaded local model is using {observed_limit:,} context tokens, "
            f"below the requested {requested_limit:,}."
        )

    effective_legacy = int(effective_limit or 0)
    user_cap = int(requested_limit or 0)
    if provider_id == "ollama" and not remote_policy:
        request_application = "ollama_num_ctx"
    elif provider_id.startswith("custom_openai_"):
        context_param = str(endpoint.get("context_param_name") or "")
        if endpoint.get("supports_runtime_context_override") and context_param:
            request_application = f"request_param:{context_param}"
        else:
            request_application = "trim_only"
    else:
        request_application = "trim_only"

    return ContextPolicy(
        model_ref=model_ref_value,
        provider_id=provider_id,
        runtime_model=runtime_model,
        native_max=int(native_max) if native_max else None,
        user_cap=user_cap,
        effective_context=effective_legacy,
        policy_kind="provider" if remote_policy else "local",
        cap_source=capacity_source,
        request_application=request_application,
        native_limit_tokens=int(native_max) if native_max else None,
        requested_limit_tokens=int(requested_limit) if requested_limit else None,
        effective_limit_tokens=int(effective_limit) if effective_limit else None,
        limit_kind=limit_kind if effective_limit else "unknown",
        usable_input_tokens=usable_input,
        compact_at_tokens=compact_at,
        capacity_source=capacity_source,
        capacity_state=capacity_state,
        observed_limit_tokens=int(observed_limit) if observed_limit else None,
        warning=warning,
    )


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
    policy = get_context_policy(model_name)
    if policy.effective_limit_tokens:
        return policy.effective_limit_tokens
    # Deprecated compatibility integer for unrelated legacy budgets. Main
    # request readiness/preparation uses the nullable safe policy fields.
    return DEFAULT_CLOUD_CONTEXT_SIZE if policy.policy_kind == "provider" else DEFAULT_CONTEXT_SIZE


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
    requested = DEFAULT_CONTEXT_SIZE if _local_context_mode == "auto" else _num_ctx
    return _coerce_context_size(requested, DEFAULT_CONTEXT_SIZE, minimum=CONTEXT_SIZE_OPTIONS[0])


def get_local_context_mode() -> str:
    return _local_context_mode


def get_cloud_context_size() -> int:
    """Deprecated compatibility getter for the legacy cloud cap."""
    return _coerce_context_size(
        _cloud_context_override or DEFAULT_CLOUD_CONTEXT_SIZE,
        DEFAULT_CLOUD_CONTEXT_SIZE,
        minimum=CLOUD_CONTEXT_SIZE_OPTIONS[0],
    )


def get_cloud_context_override() -> int | None:
    return int(_cloud_context_override) if _cloud_context_override is not None else None


def set_cloud_context_size(size: int):
    """Deprecated compatibility setter which creates an advanced override."""
    global _cloud_context_override, _cloud_num_ctx, _llm_instance
    coerced = _coerce_context_size(size, DEFAULT_CLOUD_CONTEXT_SIZE, allowed=CLOUD_CONTEXT_SIZE_OPTIONS)
    logger.info("Cloud context override changed: %s → %s", _cloud_context_override, coerced)
    _cloud_context_override = coerced
    _cloud_num_ctx = coerced
    _override_llm_cache.clear()
    if is_cloud_model(_current_model):
        # Context policy changes must remain saveable while a provider is
        # disconnected.  Recreate the transport lazily on the next request.
        _llm_instance = None
    _save_settings(_context_settings_payload(model=_current_model))


def clear_cloud_context_override() -> None:
    """Restore automatic provider capacity selection."""
    global _cloud_context_override, _cloud_num_ctx, _llm_instance
    _cloud_context_override = None
    _cloud_num_ctx = DEFAULT_CLOUD_CONTEXT_SIZE
    _override_llm_cache.clear()
    if is_cloud_model(_current_model):
        # Clearing an override is a local policy edit, not a provider call.
        _llm_instance = None
    _save_settings(_context_settings_payload(model=_current_model))


def set_context_size(size: int):
    """Change the local context window and invalidate the active LLM."""
    global _local_context_mode, _num_ctx, _llm_instance
    coerced = _coerce_context_size(size, DEFAULT_CONTEXT_SIZE, allowed=CONTEXT_SIZE_OPTIONS)
    logger.info("Context size changed: %s → %s", _num_ctx, coerced)
    _local_context_mode = "fixed"
    _num_ctx = coerced
    _override_llm_cache.clear()
    if is_cloud_model(_current_model):
        _llm_instance = None
    else:
        _llm_instance = _chat_ollama(
            model=_ollama_runtime_model_name(_current_model),
            num_ctx=_local_num_ctx_for(_current_model),
        )
    _save_settings(_context_settings_payload(model=_current_model))


def set_context_size_auto() -> None:
    """Use Row-Bot's deterministic 32K local request, capped when observed."""
    global _local_context_mode, _num_ctx, _llm_instance
    _local_context_mode = "auto"
    _num_ctx = DEFAULT_CONTEXT_SIZE
    _override_llm_cache.clear()
    if not is_cloud_model(_current_model):
        _llm_instance = _chat_ollama(
            model=_ollama_runtime_model_name(_current_model),
            num_ctx=_local_num_ctx_for(_current_model),
        )
    _save_settings(_context_settings_payload(model=_current_model))


def record_observed_local_context(model_name: str, context_length: int | None) -> None:
    """Record an already-observed Ollama allocation without performing I/O."""
    runtime_model = _ollama_runtime_model_name(model_name)
    try:
        observed = int(context_length or 0)
    except (TypeError, ValueError):
        observed = 0
    if runtime_model and observed > 0:
        _observed_local_context[runtime_model] = observed


def refresh_observed_local_context_after_load(model_name: str) -> int | None:
    """Read Ollama's already-loaded allocation; never call this for UI repaint."""
    runtime_model = _ollama_runtime_model_name(model_name)
    try:
        payload = _ollama_http_json("/api/ps", timeout=2.0)
    except Exception:
        logger.debug("Could not read Ollama active allocation", exc_info=True)
        return None
    for item in payload.get("models", []) if isinstance(payload, dict) else []:
        if not isinstance(item, dict):
            continue
        loaded_name = str(item.get("name") or item.get("model") or "")
        if loaded_name not in {runtime_model, model_name}:
            continue
        try:
            observed = int(item.get("context_length") or 0)
        except (TypeError, ValueError):
            observed = 0
        if observed > 0:
            record_observed_local_context(runtime_model, observed)
            return observed
    return None


def get_current_model() -> str:
    _reset_current_model_if_missing_custom_provider()
    return _current_model


def _ollama_host_port() -> tuple[str, int]:
    """Return the TCP host/port configured for the local Ollama daemon."""
    host, port, _scheme = _ollama_endpoint_parts()
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
    """Return names of models currently exposed by the Ollama daemon."""
    if not _ollama_reachable():
        return []
    names: set[str] = set()
    client = _ollama_client()
    try:
        if client:
            response = client.list()
            for model in getattr(response, "models", []) or []:
                name = getattr(model, "model", None) or getattr(model, "name", None)
                if isinstance(model, dict):
                    name = model.get("model") or model.get("name") or name
                if name:
                    names.add(str(name))
    except Exception:
        logger.debug("Could not list local Ollama models", exc_info=True)
    if not names:
        try:
            response = _ollama_http_json("/api/tags")
            for model in response.get("models", []) or []:
                if not isinstance(model, dict):
                    continue
                name = model.get("model") or model.get("name")
                if name:
                    names.add(str(name))
        except Exception:
            logger.debug("Could not list local Ollama models over HTTP", exc_info=True)
    return sorted(names)


def list_all_models() -> list[str]:
    """Return models currently exposed by the Ollama daemon."""
    return sorted(set(list_local_models()))


def get_trending_models() -> list[str]:
    """Return no public Ollama discovery rows; Row-Bot manages daemon models only."""
    return []


def fetch_trending_ollama_models() -> list[str]:
    """Deprecated compatibility shim.

    Row-Bot no longer fetches public Ollama model listings or offers downloads.
    Local model management lives in Ollama; this app only reads daemon-exposed
    tags.
    """
    global _trending_ollama_cache, _trending_fetched
    _trending_ollama_cache = []
    _trending_fetched = True
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
    if model_name.split("/")[-1].lower().startswith("minimax"):
        return True
    return False


def _infer_cloud_provider(model_name: str) -> str | None:
    """Infer a cloud provider from a model ID when the cache is unavailable."""
    parsed = _parse_provider_model_ref(model_name)
    if parsed:
        provider_id, _model_id = parsed
        if provider_id == "ollama":
            try:
                from row_bot.providers.ollama import is_ollama_cloud_offload_model
                return "ollama" if is_ollama_cloud_offload_model(_model_id) else None
            except Exception:
                return None
        return None if provider_id == "local" else provider_id
    model_name = _runtime_model_name(model_name)
    try:
        from row_bot.providers.ollama import is_ollama_cloud_offload_model
        if is_ollama_cloud_offload_model(model_name):
            return "ollama"
    except Exception:
        pass
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
    if bare_name.lower().startswith("minimax"):
        return "minimax"
    return None


def is_cloud_model(model_name: str) -> bool:
    """Return True if *model_name* is a known cloud model.

    Uses the persisted cache (loaded from disk at startup), then falls back
    to provider-prefix inference so a saved cloud default survives cache or
    key outages.
    """
    return _infer_cloud_provider(model_name) is not None


def get_cloud_provider(model_name: str) -> str | None:
    """Return the cloud provider id for a model, or ``None`` for local models."""
    return _infer_cloud_provider(model_name)


# ── Provider emoji mapping ───────────────────────────────────────────────────
_PROVIDER_EMOJI: dict[str | None, str] = {
    "openai": "⬡",
    "ollama": "☁️",
    "ollama_cloud": "☁️",
    "codex": "C",
    "claude_subscription": "C",
    "xai_oauth": "X",
    "opencode_zen": "OZ",
    "opencode_go": "OG",
    "atlascloud": "AC",
    "openrouter": "🌐",
    "requesty": "🛰️",
    "anthropic": "🔶",
    "google": "💎",
    "xai": "𝕏",
    "minimax": "M",
    None: "☁️",  # fallback for unknown cloud
}

def get_provider_emoji(model_name: str) -> str:
    """Return a provider-specific emoji for a model.

    Local models get a desktop icon, cloud models get a provider-specific icon.
    """
    if not is_cloud_model(model_name):
        return "🖥️"
    prov = get_cloud_provider(model_name)
    return _PROVIDER_EMOJI.get(prov, _PROVIDER_EMOJI[None])


def is_cloud_available() -> bool:
    """Return True if any cloud API key is configured."""
    from row_bot.providers.runtime import list_configured_provider_ids
    return bool(list_configured_provider_ids())


def is_openai_available() -> bool:
    """Return True if an OpenAI API key is configured."""
    from row_bot.api_keys import get_key
    return bool(get_key("OPENAI_API_KEY"))


def is_openrouter_available() -> bool:
    """Return True if an OpenRouter API key is configured."""
    from row_bot.api_keys import get_key
    return bool(get_key("OPENROUTER_API_KEY"))


def is_requesty_available() -> bool:
    """Return True if a Requesty API key is configured."""
    from row_bot.api_keys import get_key
    return bool(get_key("REQUESTY_API_KEY"))


def is_ollama_cloud_available() -> bool:
    """Return True if an Ollama Cloud API key is configured."""
    from row_bot.api_keys import get_key
    return bool(get_key("OLLAMA_API_KEY"))


def is_anthropic_available() -> bool:
    """Return True if an Anthropic API key is configured."""
    from row_bot.api_keys import get_key
    return bool(get_key("ANTHROPIC_API_KEY"))


def is_google_available() -> bool:
    """Return True if a Google AI API key is configured."""
    from row_bot.api_keys import get_key
    return bool(get_key("GOOGLE_API_KEY"))


def is_xai_available() -> bool:
    """Return True if an xAI API key is configured."""
    from row_bot.api_keys import get_key
    return bool(get_key("XAI_API_KEY"))


def is_minimax_available() -> bool:
    """Return True if a MiniMax API key is configured."""
    from row_bot.api_keys import get_key
    return bool(get_key("MINIMAX_API_KEY"))


def is_atlascloud_available() -> bool:
    """Return True if an Atlas Cloud API key is configured."""
    from row_bot.api_keys import get_key
    return bool(get_key("ATLASCLOUD_API_KEY"))


def list_cloud_models(provider: str | None = None) -> list[str]:
    """Return cached cloud model IDs, optionally filtered by provider."""
    _sync_custom_model_cache()
    if provider:
        models = [m for m, info in _cloud_model_cache.items() if info["provider"] == provider]
        if provider == "codex":
            try:
                from row_bot.providers.codex import list_codex_model_infos_for_status
                seen = set(models)
                for model_info in list_codex_model_infos_for_status():
                    if model_info.model_id not in seen:
                        models.append(model_info.model_id)
                        seen.add(model_info.model_id)
            except Exception:
                pass
        if provider == "claude_subscription":
            try:
                from row_bot.providers.claude_subscription import list_claude_subscription_model_infos_for_status
                seen = set(models)
                for model_info in list_claude_subscription_model_infos_for_status():
                    if model_info.model_id not in seen:
                        models.append(model_info.model_id)
                        seen.add(model_info.model_id)
            except Exception:
                pass
        if provider == "xai_oauth":
            try:
                from row_bot.providers.xai_oauth import list_xai_oauth_model_infos_for_status
                seen = set(models)
                for model_info in list_xai_oauth_model_infos_for_status():
                    if model_info.model_id not in seen:
                        models.append(model_info.model_id)
                        seen.add(model_info.model_id)
            except Exception:
                pass
        return models
    return list(_cloud_model_cache.keys())


def _sync_custom_model_cache() -> None:
    try:
        from row_bot.providers.custom import custom_model_cache_entries
        entries = custom_model_cache_entries()
    except Exception:
        return
    with _cloud_cache_lock:
        for model_id, info in list(_cloud_model_cache.items()):
            if isinstance(info, dict) and str(info.get("provider") or "").startswith("custom_openai_"):
                replacement = entries.get(model_id)
                if not replacement or replacement.get("provider") != info.get("provider"):
                    _cloud_model_cache.pop(model_id, None)
        _cloud_model_cache.update(entries)


def reset_current_model_if_removed(
    provider_id: str,
    *,
    removed_model_ids: set[str] | None = None,
) -> bool:
    """Reset the saved Brain default if it points at a removed provider/model."""
    global _current_model, _llm_instance
    provider_id = str(provider_id or "").strip()
    if not provider_id:
        return False
    try:
        from row_bot.providers.selection import list_quick_choices, model_choice_value, parse_model_ref
    except Exception:
        return False
    parsed = parse_model_ref(_current_model)
    if not parsed:
        return False
    current_provider, current_model = parsed
    removed = {str(model_id) for model_id in (removed_model_ids or set()) if str(model_id)}
    if current_provider != provider_id:
        return False
    if removed and current_model not in removed:
        return False
    fallback = ""
    try:
        for choice in list_quick_choices("chat"):
            if choice.get("kind") != "model" or choice.get("active") is False:
                continue
            choice_provider = str(choice.get("provider_id") or "")
            choice_model = str(choice.get("model_id") or "")
            if choice_provider == provider_id and (not removed or choice_model in removed):
                continue
            fallback = model_choice_value(choice_model, provider_id=choice_provider)
            if fallback:
                break
    except Exception:
        fallback = ""
    if not fallback:
        fallback = model_choice_value(DEFAULT_MODEL, provider_id="ollama")
    _current_model = fallback
    _llm_instance = None
    _override_llm_cache.clear()
    _save_settings(_context_settings_payload(model=_current_model))
    return True


def _reset_current_model_if_missing_custom_provider() -> None:
    try:
        from row_bot.providers.custom import get_custom_endpoint, is_custom_openai_provider
        from row_bot.providers.selection import parse_model_ref
    except Exception:
        return
    parsed = parse_model_ref(_current_model)
    if not parsed:
        return
    provider_id, _model_id = parsed
    if is_custom_openai_provider(provider_id) and not get_custom_endpoint(provider_id):
        reset_current_model_if_removed(provider_id)


def list_starred_cloud_models() -> list[str]:
    """Return cloud models the user has starred (for the thread picker)."""
    from row_bot.providers.selection import list_quick_model_ids, migrate_legacy_starred_models
    from row_bot.api_keys import get_cloud_config
    _sync_custom_model_cache()
    migrate_legacy_starred_models(cloud_models=_cloud_model_cache.keys())
    quick_models = [m for m in list_quick_model_ids("chat") if m in _cloud_model_cache]
    if quick_models:
        return quick_models
    starred = set(get_cloud_config().get("starred_models", []))
    return [m for m in _cloud_model_cache if m in starred]


def star_cloud_model(model_id: str) -> None:
    """Add a model to the starred list."""
    from row_bot.api_keys import get_cloud_config, set_cloud_config
    from row_bot.providers.selection import add_quick_choice_for_model
    starred = list(get_cloud_config().get("starred_models", []))
    if model_id not in starred:
        starred.append(model_id)
        set_cloud_config("starred_models", starred)
    add_quick_choice_for_model(model_id, source="legacy_starred_cloud")


def unstar_cloud_model(model_id: str) -> None:
    """Remove a model from the starred list."""
    from row_bot.api_keys import get_cloud_config, set_cloud_config
    from row_bot.providers.selection import remove_quick_choice_for_model
    starred = list(get_cloud_config().get("starred_models", []))
    if model_id in starred:
        starred.remove(model_id)
        set_cloud_config("starred_models", starred)
    remove_quick_choice_for_model(model_id)


def get_cloud_model_context(model_name: str) -> int:
    """Return the context window size for a cloud model.

    Resolution order:
    1. Exact provider-qualified cached value.
    2. Provider-native status metadata.
    3. Provider-qualified catalog or narrowly maintained exact model table.
    Unknown capacity returns zero; there is no generic cloud fallback.
    """
    parsed = _parse_provider_model_ref(model_name)
    provider_id = parsed[0] if parsed else None
    runtime_model = _runtime_model_name(model_name)
    _sync_custom_model_cache()
    info = _cloud_cache_entry_for(model_name, provider_id)
    if info and (not provider_id or info.get("provider") == provider_id):
        cached_ctx = int(info.get("ctx") or 0)
        if cached_ctx > 0:
            return cached_ctx
    if provider_id == "codex":
        try:
            from row_bot.providers.codex import list_codex_model_infos_for_status

            for model_info in list_codex_model_infos_for_status():
                if model_info.model_id == runtime_model and model_info.context_window:
                    return int(model_info.context_window)
        except Exception:
            pass
    if provider_id == "claude_subscription":
        try:
            from row_bot.providers.claude_subscription import list_claude_subscription_model_infos_for_status

            for model_info in list_claude_subscription_model_infos_for_status():
                if model_info.model_id == runtime_model and model_info.context_window:
                    return int(model_info.context_window)
        except Exception:
            pass
    if provider_id == "xai_oauth":
        try:
            from row_bot.providers.xai_oauth import list_xai_oauth_model_infos_for_status

            for model_info in list_xai_oauth_model_infos_for_status():
                if model_info.model_id == runtime_model and model_info.context_window:
                    return int(model_info.context_window)
        except Exception:
            pass
    return _catalog_or_heuristic(provider_id or "", runtime_model)


def list_cloud_vision_models() -> list[str]:
    """Return cloud model IDs that support vision / image input."""
    from row_bot.providers.capabilities import snapshot_supports_surface
    vision_models = [
        m for m, info in _cloud_model_cache.items()
        if info.get("vision")
        or (isinstance(info.get("capabilities_snapshot"), dict) and bool(info.get("capabilities_snapshot"))
            and snapshot_supports_surface(info.get("capabilities_snapshot"), "vision"))
    ]
    try:
        from row_bot.providers.codex import list_codex_model_infos_for_status
        seen = set(vision_models)
        for model_info in list_codex_model_infos_for_status():
            if model_info.model_id not in seen and snapshot_supports_surface(model_info.capability_snapshot(), "vision"):
                vision_models.append(model_info.model_id)
                seen.add(model_info.model_id)
    except Exception:
        pass
    try:
        from row_bot.providers.claude_subscription import list_claude_subscription_model_infos_for_status
        seen = set(vision_models)
        for model_info in list_claude_subscription_model_infos_for_status():
            if model_info.model_id not in seen and snapshot_supports_surface(model_info.capability_snapshot(), "vision"):
                vision_models.append(model_info.model_id)
                seen.add(model_info.model_id)
    except Exception:
        pass
    try:
        from row_bot.providers.xai_oauth import list_xai_oauth_model_infos_for_status
        seen = set(vision_models)
        for model_info in list_xai_oauth_model_infos_for_status():
            ref = model_info.selection_ref
            if ref not in seen and snapshot_supports_surface(model_info.capability_snapshot(), "vision"):
                vision_models.append(ref)
                seen.add(ref)
    except Exception:
        pass
    return vision_models


def is_cloud_vision_model(model_name: str) -> bool:
    """Return True if *model_name* is a cloud model with vision support."""
    parsed = _parse_provider_model_ref(model_name)
    runtime_model = _runtime_model_name(model_name)
    if parsed and parsed[0] == "codex":
        try:
            from row_bot.providers.capabilities import snapshot_supports_surface
            from row_bot.providers.codex import list_codex_model_infos_for_status
            return any(
                model_info.model_id == runtime_model
                and snapshot_supports_surface(model_info.capability_snapshot(), "vision")
                for model_info in list_codex_model_infos_for_status()
            )
        except Exception:
            return False
    if parsed and parsed[0] == "claude_subscription":
        try:
            from row_bot.providers.capabilities import snapshot_supports_surface
            from row_bot.providers.claude_subscription import list_claude_subscription_model_infos_for_status
            return any(
                model_info.model_id == runtime_model
                and snapshot_supports_surface(model_info.capability_snapshot(), "vision")
                for model_info in list_claude_subscription_model_infos_for_status()
            )
        except Exception:
            return False
    if parsed and parsed[0] == "xai_oauth":
        try:
            from row_bot.providers.capabilities import snapshot_supports_surface
            from row_bot.providers.xai_oauth import list_xai_oauth_model_infos_for_status
            return any(
                model_info.model_id == runtime_model
                and snapshot_supports_surface(model_info.capability_snapshot(), "vision")
                for model_info in list_xai_oauth_model_infos_for_status()
            )
        except Exception:
            return False
    info = _cloud_cache_entry_for(model_name, parsed[0] if parsed else None)
    if not info:
        return False
    from row_bot.providers.capabilities import snapshot_supports_surface
    snapshot = info.get("capabilities_snapshot")
    return bool(
        info.get("vision")
        or (isinstance(snapshot, dict) and bool(snapshot) and snapshot_supports_surface(snapshot, "vision"))
    )


def _get_cloud_llm(model_name: str, *, reasoning_plan=None):
    """Return a cached LLM for a cloud model.

    OpenAI-direct models use ``ChatOpenAI``.  OpenRouter models use
    ``ChatOpenRouter`` which correctly surfaces ``reasoning_content``
    in ``additional_kwargs`` for reasoning models.
    """
    from row_bot.providers.runtime import create_chat_model

    provider = get_cloud_provider(model_name)
    runtime_model = _runtime_model_name(model_name)
    ctx = get_cloud_model_context(model_name)
    fingerprint = getattr(reasoning_plan, "fingerprint", "reasoning:provider-default")
    key = (f"{provider or 'openrouter'}:{runtime_model}", ctx, fingerprint)
    if key in _override_llm_cache:
        return _override_llm_cache[key]

    logger.info("Creating provider LLM: model=%s via %s", runtime_model, provider or "openrouter")

    _override_llm_cache[key] = create_chat_model(runtime_model, provider, reasoning_plan=reasoning_plan)
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


def validate_requesty_key(api_key: str) -> bool:
    """Validate a Requesty API key by listing OpenAI-compatible models."""
    import httpx
    from row_bot.providers.requesty import requesty_models_url

    try:
        resp = httpx.get(
            requesty_models_url(),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning("Requesty key validation: %d - %s", resp.status_code, resp.text[:200])
            return False
        try:
            body_json = resp.json()
        except Exception:
            body_json = {}
        return isinstance(body_json.get("data"), list)
    except Exception as exc:
        logger.warning("Requesty key validation error: %s", exc)
        return False


def validate_ollama_cloud_key(api_key: str) -> bool:
    """Validate an Ollama Cloud API key with a tiny authenticated chat probe."""
    import httpx
    from row_bot.providers.transports.ollama_cloud import normalize_ollama_cloud_api_key

    clean_key = normalize_ollama_cloud_api_key(api_key)
    if not clean_key:
        return False

    try:
        probe_model = "gpt-oss:20b"
        resp = httpx.post(
            f"{OLLAMA_CLOUD_BASE_URL}/api/chat",
            headers={"Authorization": f"Bearer {clean_key}"},
            json={
                "model": probe_model,
                "messages": [{"role": "user", "content": "ok"}],
                "stream": False,
                "options": {"num_predict": 1},
            },
            timeout=20,
        )
        if resp.status_code == 200:
            return True
        logger.warning("Ollama Cloud key validation: %d - %s", resp.status_code, resp.text[:200])
        return False
    except Exception as exc:
        logger.warning("Ollama Cloud key validation error: %s", exc)
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


def validate_minimax_key(api_key: str) -> bool:
    """Validate a MiniMax API key with the Anthropic-compatible models API.

    MiniMax reports a
    valid key with no available balance as ``insufficient balance (1008)``;
    treat that as accepted credentials so the UI does not mislabel the key as
    invalid.
    """
    import httpx

    try:
        resp = httpx.get(
            f"{MINIMAX_ANTHROPIC_BASE_URL}/v1/models",
            headers={"x-api-key": api_key},
            params={"limit": 1},
            timeout=15,
        )
        if resp.status_code == 200:
            try:
                body_json = resp.json()
            except Exception:
                body_json = {}
            return isinstance(body_json.get("data"), list)
        body = resp.text[:500]
        lower_body = body.lower()
        if "insufficient balance" in lower_body or "1008" in lower_body:
            logger.warning(
                "MiniMax key validation accepted credentials but account is not billable: %d — %s",
                resp.status_code,
                resp.text[:200],
            )
            return True
        logger.warning("MiniMax key validation: %d — %s", resp.status_code, resp.text[:200])
        return False
    except Exception as exc:
        logger.warning("MiniMax key validation error: %s", exc)
        return False


def validate_atlascloud_key(api_key: str) -> bool:
    """Validate an Atlas Cloud API key by listing OpenAI-compatible models."""
    import httpx
    from row_bot.providers.atlascloud import atlascloud_models_url

    try:
        resp = httpx.get(
            atlascloud_models_url(),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        if resp.status_code == 200:
            try:
                body_json = resp.json()
            except Exception:
                body_json = {}
            return isinstance(body_json.get("data"), list)
        logger.warning("Atlas Cloud key validation: %d - %s", resp.status_code, resp.text[:200])
        return False
    except Exception as exc:
        logger.warning("Atlas Cloud key validation error: %s", exc)
        return False


def _catalog_or_heuristic(provider_id: str, model_id: str) -> int:
    """Resolve a cached or maintained provider-qualified context capacity."""
    provider = str(provider_id or "").strip()
    runtime_model = _runtime_model_name(model_id).strip()
    with _context_catalog_lock:
        cat_val = _context_catalog.get(f"{provider}/{runtime_model}")
        if cat_val:
            return cat_val
        # OpenRouter runtime IDs already contain their upstream provider slug,
        # so this remains provider-qualified rather than a bare cross-provider key.
        if provider == "openrouter" and "/" in runtime_model:
            cat_val = _context_catalog.get(runtime_model)
            if cat_val:
                return cat_val
    return _maintained_context_limit(provider, runtime_model)


# Anthropic model ID substrings to skip (non-chat or internal models)
_ANTHROPIC_SKIP_SUBSTRINGS = ("embed", "tokenizer")

# Google model substrings to skip
_GOOGLE_SKIP_SUBSTRINGS = ("embed", "aqa", "tts")


def _fetch_ollama_cloud_models(api_key: str) -> int:
    """Fetch models from Ollama Cloud's native ``/api/tags`` endpoint."""
    import httpx
    from row_bot.providers.capabilities import model_supports_surface
    from row_bot.providers.catalog import model_info_from_metadata, model_info_to_cache_entry
    from row_bot.providers.transports.ollama_cloud import normalize_ollama_cloud_api_key

    clean_key = normalize_ollama_cloud_api_key(api_key)

    try:
        resp = httpx.get(
            f"{OLLAMA_CLOUD_BASE_URL}/api/tags",
            headers={"Authorization": f"Bearer {clean_key}"} if clean_key else {},
            timeout=15,
        )
        resp.raise_for_status()
        models = resp.json().get("models", [])
        if not isinstance(models, list):
            logger.warning("Ollama Cloud model fetch returned invalid models payload")
            return 0
        if len(models) > 500:
            logger.warning("Ollama Cloud model fetch returned %d entries; limiting to first 500", len(models))
            models = models[:500]
    except Exception as exc:
        logger.warning("Failed to fetch ollama_cloud models: %s", exc)
        return 0

    entries: dict[str, dict] = {}
    for m in models:
        if not isinstance(m, dict):
            continue
        mid = str(m.get("name") or m.get("model") or "").strip()
        if not mid:
            continue
        metadata = dict(m)
        details = m.get("details")
        if isinstance(details, dict):
            metadata.update({f"details_{key}": value for key, value in details.items()})
        ctx = int(m.get("context_length") or m.get("context_window") or 0)
        if ctx <= 0:
            ctx = _catalog_or_heuristic("ollama_cloud", mid)
        model_info = model_info_from_metadata(
            "ollama_cloud",
            mid,
            metadata,
            display_name=mid,
            context_window=ctx,
            source="ollama_cloud_catalog",
        )
        if not model_supports_surface(model_info, "chat"):
            continue
        entries[mid] = model_info_to_cache_entry(model_info)
    count = _replace_provider_cloud_cache_entries("ollama_cloud", entries)
    logger.info("Fetched %d ollama_cloud models", count)
    return count


def _fetch_cloud_models(provider: str) -> int:
    """Fetch available models from *provider*.

    Supported providers include OpenAI, ChatGPT/Codex, Ollama Cloud,
    OpenRouter, Requesty, Anthropic, Google, xAI, xAI OAuth, MiniMax,
    OpenCode, Atlas Cloud, and Claude Subscription. Populates
    ``_cloud_model_cache`` and returns the number of models found. Safe to
    call from background threads.
    """
    import httpx
    from row_bot.providers.auth_store import get_provider_secret
    from row_bot.providers.capabilities import model_supports_surface
    from row_bot.providers.catalog import model_info_from_metadata, model_info_to_cache_entry

    if provider == "openai":
        api_key = get_provider_secret(provider)
        if not api_key:
            return 0
        url = f"{OPENAI_BASE_URL}/models"
        headers = {"Authorization": f"Bearer {api_key}"}
    elif provider == "ollama_cloud":
        api_key = get_provider_secret(provider)
        if not api_key:
            return 0
        return _fetch_ollama_cloud_models(api_key)
    elif provider == "openrouter":
        api_key = get_provider_secret(provider)
        if not api_key:
            return 0
        url = f"{OPENROUTER_BASE_URL}/models"
        headers = {"Authorization": f"Bearer {api_key}"}
    elif provider == "requesty":
        api_key = get_provider_secret(provider)
        if not api_key:
            return 0
        return _fetch_requesty_models(api_key)
    elif provider == "anthropic":
        api_key = get_provider_secret(provider)
        if not api_key:
            return 0
        return _fetch_anthropic_models(api_key)
    elif provider == "google":
        api_key = get_provider_secret(provider)
        if not api_key:
            return 0
        return _fetch_google_models(api_key)
    elif provider == "xai":
        api_key = get_provider_secret(provider)
        if not api_key:
            return 0
        return _fetch_xai_models(api_key)
    elif provider == "xai_oauth":
        return _fetch_xai_oauth_models()
    elif provider == "minimax":
        api_key = get_provider_secret(provider)
        if not api_key:
            return 0
        return _fetch_minimax_models(api_key)
    elif provider in {"opencode_zen", "opencode_go"}:
        api_key = get_provider_secret(provider)
        if not api_key:
            return 0
        return _fetch_opencode_models(provider)
    elif provider == "atlascloud":
        api_key = get_provider_secret(provider)
        if not api_key:
            return 0
        return _fetch_atlascloud_models(api_key)
    elif provider == "codex":
        return _fetch_codex_models()
    elif provider == "claude_subscription":
        return _fetch_claude_subscription_models()
    else:
        return 0

    try:
        resp = httpx.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json().get("data", [])
    except Exception as exc:
        logger.warning("Failed to fetch %s models: %s", provider, exc)
        return 0

    entries: dict[str, dict] = {}
    if provider == "openai":
        for m in data:
            if not isinstance(m, dict):
                continue
            mid = str(m.get("id") or "").strip()
            if not mid:
                continue
            model_info = model_info_from_metadata(
                "openai", mid, m,
                display_name=mid,
                context_window=_catalog_or_heuristic("openai", mid),
            )
            if not any(model_supports_surface(model_info, surface) for surface in ("chat", "image", "video")):
                continue
            entries[mid] = model_info_to_cache_entry(model_info)
    else:  # openrouter
        for m in data:
            if not isinstance(m, dict):
                continue
            mid = str(m.get("id") or "").strip()
            name = str(m.get("name") or mid)
            ctx = int(m.get("context_length") or 128_000)
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
            entries[mid] = model_info_to_cache_entry(model_info)
    count = _replace_provider_cloud_cache_entries(provider, entries)
    logger.info("Fetched %d %s models", count, provider)
    return count


def fetch_cloud_models(provider: str) -> int:
    """Refresh one provider and return its compatibility update count."""
    return refresh_cloud_provider_models(provider).update_count


def _fetch_anthropic_models(api_key: str) -> int:
    """Fetch models from the Anthropic ``/v1/models`` endpoint.

    Handles pagination via ``after_id``.  Uses ``max_input_tokens`` for
    context size when available, falling back to ``_catalog_or_heuristic``.
    """
    import httpx
    from row_bot.providers.catalog import model_info_from_metadata, model_info_to_cache_entry

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    after_id: str | None = None
    fetched: list[dict] = []

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
            if not isinstance(models, list):
                logger.warning("Anthropic model fetch returned malformed data payload")
                return 0
            fetched.extend(item for item in models if isinstance(item, dict))

            if not body.get("has_more"):
                break
            after_id = str(body.get("last_id") or "")
            if not after_id:
                logger.warning("Anthropic model fetch indicated more pages without last_id")
                return 0
    except Exception as exc:
        logger.warning("Failed to fetch anthropic models: %s", exc)
        return 0

    entries: dict[str, dict] = {}
    for m in fetched:
        mid = str(m.get("id") or "").strip()
        if not mid or any(s in mid for s in _ANTHROPIC_SKIP_SUBSTRINGS):
            continue
        display = str(m.get("display_name") or mid)
        api_ctx = m.get("max_input_tokens", 0)
        ctx = api_ctx if api_ctx and api_ctx > 0 else _catalog_or_heuristic("anthropic", mid)
        caps = m.get("capabilities", {})
        has_vision = bool(
            isinstance(caps, dict)
            and isinstance(caps.get("image_input"), dict)
            and caps.get("image_input", {}).get("supported")
        )
        metadata = dict(m)
        metadata["vision"] = has_vision
        entries[mid] = model_info_to_cache_entry(model_info_from_metadata(
            "anthropic", mid, metadata,
            display_name=display,
            context_window=ctx,
        ))
    count = _replace_provider_cloud_cache_entries("anthropic", entries)
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
    from row_bot.providers.capabilities import model_supports_surface
    from row_bot.providers.catalog import model_info_from_metadata, model_info_to_cache_entry

    page_token: str | None = None
    fetched: list[dict] = []

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
            if not isinstance(models, list):
                logger.warning("Google model fetch returned malformed data payload")
                return 0
            fetched.extend(item for item in models if isinstance(item, dict))

            page_token = body.get("nextPageToken")
            if not page_token:
                break
    except Exception as exc:
        logger.warning("Failed to fetch google models: %s", exc)
        return 0

    entries: dict[str, dict] = {}
    for m in fetched:
        name = str(m.get("name") or "")        # e.g. "models/gemini-2.5-flash"
        mid = name.removeprefix("models/")  # → "gemini-2.5-flash"
        if not mid or any(s in mid for s in _GOOGLE_SKIP_SUBSTRINGS):
            continue
        methods = m.get("supportedGenerationMethods", [])
        methods = methods if isinstance(methods, list) else []
        display = str(m.get("displayName") or mid)
        ctx = m.get("inputTokenLimit", 0)
        if not ctx or ctx <= 0:
            ctx = _catalog_or_heuristic("google", mid)
        metadata = dict(m)
        metadata["supportedGenerationMethods"] = methods
        metadata["vision"] = "generateContent" in methods
        model_info = model_info_from_metadata("google", mid, metadata, display_name=display, context_window=ctx)
        if not any(model_supports_surface(model_info, surface) for surface in ("chat", "image", "video")):
            continue
        entries[mid] = model_info_to_cache_entry(model_info)
    count = _replace_provider_cloud_cache_entries("google", entries)
    logger.info("Fetched %d google models", count)
    return count


# Substrings that mark non-chat xAI models to drop from the provider cache.
_XAI_SKIP_SUBSTRINGS: tuple[str, ...] = ()

_MINIMAX_FALLBACK_MODELS: tuple[tuple[str, int], ...] = (
    ("MiniMax-M3", 1_000_000),
    ("MiniMax-M2.7", 204_800),
    ("MiniMax-M2.7-highspeed", 204_800),
    ("MiniMax-M2.5", 204_800),
    ("MiniMax-M2.5-highspeed", 204_800),
    ("MiniMax-M2.1", 204_800),
    ("MiniMax-M2.1-highspeed", 204_800),
    ("MiniMax-M2", 204_800),
)
_MINIMAX_FALLBACK_CONTEXTS = dict(_MINIMAX_FALLBACK_MODELS)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _minimax_metadata_int(metadata: dict, *keys: str) -> int:
    for key in keys:
        value = metadata.get(key)
        if value is None:
            continue
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return 0


def _minimax_context_window(model_id: str, metadata: dict | None = None) -> int:
    metadata = metadata or {}
    api_context = _minimax_metadata_int(
        metadata,
        "max_input_tokens",
        "input_token_limit",
        "inputTokenLimit",
        "context_window",
        "contextWindow",
        "context_length",
        "contextLength",
    )
    if api_context:
        return api_context
    if model_id in _MINIMAX_FALLBACK_CONTEXTS:
        return _MINIMAX_FALLBACK_CONTEXTS[model_id]
    return _catalog_or_heuristic("minimax", model_id)


def _minimax_metadata_modalities(metadata: dict, *keys: str) -> set[str]:
    modalities: set[str] = set()
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, (list, tuple, set, frozenset)):
            continue
        for item in value:
            text = str(item or "").strip().lower()
            if text in {"text", "image", "video", "audio"}:
                modalities.add(text)
    return modalities


def _minimax_input_modalities(model_id: str, metadata: dict | None = None) -> set[str]:
    metadata = metadata or {}
    inputs = {"text"}
    inputs.update(_minimax_metadata_modalities(metadata, "input_modalities", "input", "modalities"))
    architecture = metadata.get("architecture")
    if isinstance(architecture, dict):
        inputs.update(_minimax_metadata_modalities(architecture, "input_modalities", "input", "modalities"))
    capabilities = metadata.get("capabilities")
    if isinstance(capabilities, dict):
        for key in ("image_input", "vision", "image"):
            value = capabilities.get(key)
            if value is True or (isinstance(value, dict) and value.get("supported")):
                inputs.add("image")
        for key in ("video_input", "video"):
            value = capabilities.get(key)
            if value is True or (isinstance(value, dict) and value.get("supported")):
                inputs.add("video")
    if metadata.get("vision") is True:
        inputs.add("image")
    if model_id.lower().startswith("minimax-m3"):
        inputs.update({"image", "video"})
    return inputs


def _minimax_output_modalities(metadata: dict | None = None) -> set[str]:
    metadata = metadata or {}
    outputs = _minimax_metadata_modalities(metadata, "output_modalities", "output")
    return outputs or {"text"}


def _minimax_bool(metadata: dict, key: str, default: bool) -> bool:
    value = metadata.get(key)
    return value if isinstance(value, bool) else default


def _minimax_model_info(
    model_id: str,
    metadata: dict | None = None,
    *,
    display_name: str | None = None,
    source: str = "minimax_live_catalog",
    source_confidence: str = "live_minimax_model_list",
    last_verified_at: str = "",
):
    from row_bot.providers.models import ModelInfo, ModelModality, ModelTask, TransportMode

    metadata = dict(metadata or {})
    inputs = _minimax_input_modalities(model_id, metadata)
    outputs = _minimax_output_modalities(metadata)
    tool_calling = _minimax_bool(metadata, "tool_calling", True)
    streaming = _minimax_bool(metadata, "streaming", True)
    capabilities = {"text", "chat"}
    if ModelModality.IMAGE.value in inputs:
        capabilities.add("vision")
    if ModelModality.VIDEO.value in inputs:
        capabilities.add("video_input")
    if tool_calling:
        capabilities.add("tool_calling")
    if streaming:
        capabilities.add("streaming")
    if model_id.lower().startswith("minimax-m"):
        capabilities.add("thinking")
    return ModelInfo(
        provider_id="minimax",
        model_id=model_id,
        display_name=display_name or str(metadata.get("display_name") or model_id),
        context_window=_minimax_context_window(model_id, metadata),
        transport=TransportMode.ANTHROPIC_MESSAGES,
        capabilities=frozenset(capabilities),
        input_modalities=frozenset(inputs),
        output_modalities=frozenset(outputs),
        tasks=frozenset({ModelTask.CHAT.value}),
        tool_calling=tool_calling,
        streaming=streaming,
        endpoint_compatibility=frozenset({TransportMode.ANTHROPIC_MESSAGES}),
        source_confidence=source_confidence,
        last_verified_at=last_verified_at,
        source=source,
    )


def _minimax_fallback_model_infos():
    return [
        _minimax_model_info(
            model_id,
            {"max_input_tokens": context_window},
            source="minimax_static_fallback",
            source_confidence="documented_minimax_fallback",
        )
        for model_id, context_window in _MINIMAX_FALLBACK_MODELS
    ]


def _is_minimax_cache_entry(model_id: str, info: dict) -> bool:
    provider = str(info.get("provider") or "")
    if provider:
        return provider == "minimax"
    key = str(model_id or "")
    if key.startswith("model:minimax:"):
        return True
    return _runtime_model_name(key).split("/")[-1].lower().startswith("minimax")


def _fetch_xai_models(api_key: str) -> int:
    """Fetch xAI models and populate API-key provider cache rows.

    xAI exposes different slices of metadata from ``/models`` and
    ``/language-models``. Merge both so new chat rows and Grok Imagine media
    rows appear automatically when the provider lists them.
    """
    import httpx
    from row_bot.providers.catalog import model_info_from_metadata, model_info_to_cache_entry
    from row_bot.providers.capabilities import model_supports_surface
    from row_bot.providers.models import TransportMode
    from row_bot.providers.xai_catalog import (
        is_hidden_xai_model,
        merge_xai_curated_chat_extras,
        merged_xai_model_entries,
        xai_model_id_from_item,
    )

    pages: list[list] = []
    errors: list[str] = []
    for path in ("/models", "/language-models"):
        try:
            resp = httpx.get(
                f"{XAI_BASE_URL}{path}",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=15,
            )
            if int(getattr(resp, "status_code", 0) or 0) == 404:
                continue
            resp.raise_for_status()
            body = resp.json()
            entries = body.get("data") or body.get("models")
            if isinstance(entries, list):
                pages.append(entries)
        except Exception as exc:
            errors.append(f"{path}: {exc}")
            continue
    if not pages:
        logger.warning("Failed to fetch xai models: %s", "; ".join(errors) if errors else "no model list returned")
        return 0

    model_infos = []
    verified_at = _utc_now_iso()
    seen: set[str] = set()
    for m in merged_xai_model_entries(pages):
        mid = xai_model_id_from_item(m)
        if not mid or mid in seen:
            continue
        seen.add(mid)
        if any(s in mid for s in _XAI_SKIP_SUBSTRINGS) or is_hidden_xai_model(m, mid):
            continue
        display = str(m.get("display_name") or m.get("displayName") or m.get("label") or mid)
        try:
            ctx = int(m.get("context_window") or m.get("contextWindow") or m.get("context_length") or m.get("contextLength") or 0)
        except (TypeError, ValueError):
            ctx = 0
        if ctx <= 0:
            ctx = _catalog_or_heuristic("xai", mid)
        metadata = dict(m)
        modalities = metadata.get("input_modalities") or metadata.get("inputModalities") or metadata.get("modalities")
        if isinstance(modalities, str):
            modalities = [modalities]
        metadata["input_modalities"] = modalities if isinstance(modalities, list) else metadata.get("input_modalities", [])
        metadata["vision"] = any(str(item).lower() == "image" for item in metadata.get("input_modalities", []))
        model_info = model_info_from_metadata(
            "xai", mid, metadata,
            display_name=display,
            context_window=ctx,
        )
        if not any(model_supports_surface(model_info, surface) for surface in ("chat", "image", "video")):
            continue
        model_infos.append(model_info)

    model_infos = merge_xai_curated_chat_extras(
        model_infos,
        "xai",
        transport=TransportMode.OPENAI_CHAT,
        source="row_bot_xai_curated_catalog",
        risk_label="api_key",
        verified_at=verified_at,
    )
    if not model_infos:
        logger.warning("xai model fetch returned no usable model rows; preserving previous cache")
        return 0

    entries = {
        model_info.model_id: model_info_to_cache_entry(model_info)
        for model_info in model_infos
    }
    count = _replace_provider_cloud_cache_entries("xai", entries)
    logger.info("Fetched %d xai models", count)
    return count


def _fetch_requesty_models(api_key: str) -> int:
    """Fetch models from Requesty's OpenAI-compatible ``/v1/models`` endpoint.

    Requesty uses slash-style upstream model IDs such as ``openai/gpt-4o-mini``.
    Cache entries are provider-qualified so they cannot collide with OpenRouter
    rows or fallback provider inference.
    """
    import httpx
    from row_bot.providers.capabilities import model_supports_surface
    from row_bot.providers.catalog import model_info_to_cache_entry
    from row_bot.providers.requesty import requesty_model_info_from_metadata, requesty_models_url

    existing_requesty_rows = _provider_cloud_cache_entries("requesty")

    try:
        resp = httpx.get(
            requesty_models_url(),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15,
        )
        resp.raise_for_status()
        raw_data = resp.json().get("data", [])
        if not isinstance(raw_data, list):
            logger.warning("Requesty model fetch returned malformed data payload")
            return 0
        data = [item for item in raw_data if isinstance(item, dict)]
    except Exception as exc:
        if existing_requesty_rows:
            logger.warning("Failed to fetch requesty models: %s; preserving previous Requesty cache", exc)
            return 0
        logger.warning("Failed to fetch requesty models: %s", exc)
        return 0

    verified_at = _utc_now_iso()
    model_infos = []
    seen: set[str] = set()
    for metadata in data:
        model_id = str(metadata.get("id") or "").strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        model_info = requesty_model_info_from_metadata(
            model_id,
            metadata,
            display_name=str(metadata.get("name") or metadata.get("display_name") or metadata.get("displayName") or model_id),
            context_window=_catalog_or_heuristic("requesty", model_id),
            last_verified_at=verified_at,
        )
        if not model_info:
            continue
        if not any(model_supports_surface(model_info, surface) for surface in ("chat", "vision")):
            continue
        model_infos.append(model_info)

    if not model_infos:
        logger.warning("Requesty model fetch returned no usable chat model rows; preserving previous cache")
        return 0

    entries = {
        model_info.selection_ref: model_info_to_cache_entry(model_info)
        for model_info in model_infos
    }
    count = _replace_provider_cloud_cache_entries("requesty", entries)
    logger.info("Fetched %d requesty models", count)
    return count


def _fetch_atlascloud_models(api_key: str) -> int:
    """Fetch models from the Atlas Cloud OpenAI-compatible ``/v1/models`` endpoint.

    Atlas Cloud exposes an OpenAI-compatible catalog, but phase 1 only admits
    LLM chat/vision rows. Cache entries are provider-qualified because Atlas
    IDs commonly use the same slash form as OpenRouter model IDs.
    """
    import httpx
    from row_bot.providers.atlascloud import (
        atlascloud_model_info_from_metadata,
        atlascloud_models_url,
        list_atlascloud_fallback_model_infos,
    )
    from row_bot.providers.capabilities import model_supports_surface
    from row_bot.providers.catalog import model_info_to_cache_entry

    existing_atlas_rows = _provider_cloud_cache_entries("atlascloud")

    source = "live"
    data: list[dict] = []
    try:
        resp = httpx.get(
            atlascloud_models_url(),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15,
        )
        resp.raise_for_status()
        raw_data = resp.json().get("data", [])
        if not isinstance(raw_data, list):
            logger.warning("Atlas Cloud model fetch returned malformed data payload")
            return 0
        data = [item for item in raw_data if isinstance(item, dict)]
    except Exception as exc:
        if existing_atlas_rows:
            logger.warning("Failed to fetch atlascloud models: %s; preserving previous Atlas cache", exc)
            return 0
        logger.warning("Failed to fetch atlascloud models: %s; using static fallback", exc)
        source = "fallback"

    verified_at = _utc_now_iso()
    model_infos = []
    if source == "live":
        seen: set[str] = set()
        for m in data:
            mid = str(m.get("id") or "").strip()
            if not mid or mid in seen:
                continue
            seen.add(mid)
            ctx = m.get("context_length") or m.get("context_window") or _catalog_or_heuristic("atlascloud", mid)
            model_info = atlascloud_model_info_from_metadata(
                mid,
                m,
                display_name=str(m.get("name") or m.get("display_name") or m.get("displayName") or mid),
                context_window=ctx,
                source="atlascloud_live_catalog",
                source_confidence="live_atlascloud_model_list",
                last_verified_at=verified_at,
            )
            if not model_info or not model_supports_surface(model_info, "chat"):
                continue
            model_infos.append(model_info)
    if not model_infos:
        if source == "live" and data:
            if existing_atlas_rows:
                logger.warning("Atlas Cloud live catalog returned no usable chat models; preserving previous cache")
                return 0
            logger.warning("Atlas Cloud live catalog returned no usable chat models; using static fallback")
            model_infos = list_atlascloud_fallback_model_infos()
            source = "fallback"
        else:
            model_infos = list_atlascloud_fallback_model_infos()
            source = "fallback"
    if not model_infos:
        return 0

    if source == "fallback" and existing_atlas_rows:
        logger.warning("Atlas Cloud fallback skipped because previous Atlas cache exists")
        return 0

    entries: dict[str, dict] = {}
    for model_info in model_infos:
        entry = model_info_to_cache_entry(model_info)
        entry["source"] = "atlascloud_live_catalog" if source == "live" else "atlascloud_static_fallback"
        entries[model_info.selection_ref] = entry
    count = _replace_provider_cloud_cache_entries("atlascloud", entries)
    logger.info("Fetched %d atlascloud models", count)
    return count


def _fetch_minimax_models(api_key: str) -> int:
    """Populate MiniMax Anthropic-compatible models from the live provider catalog."""
    if not api_key:
        return 0
    import httpx
    from row_bot.providers.catalog import model_info_to_cache_entry

    fetched: list[dict] = []
    after_id: str | None = None
    verified_at = _utc_now_iso()
    try:
        while True:
            params: dict[str, str | int] = {"limit": 100}
            if after_id:
                params["after_id"] = after_id
            resp = httpx.get(
                f"{MINIMAX_ANTHROPIC_BASE_URL}/v1/models",
                headers={"x-api-key": api_key},
                params=params,
                timeout=15,
            )
            resp.raise_for_status()
            body = resp.json()
            page = body.get("data")
            if not isinstance(page, list):
                logger.warning("MiniMax model fetch returned malformed data payload")
                return 0
            fetched.extend(item for item in page if isinstance(item, dict))
            if not body.get("has_more"):
                break
            after_id = str(body.get("last_id") or "")
            if not after_id:
                logger.warning("MiniMax model fetch indicated more pages without last_id")
                return 0
    except Exception as exc:
        logger.warning("Failed to fetch minimax models: %s", exc)
        return 0

    if not fetched:
        logger.warning("MiniMax model fetch returned no models; preserving previous cache")
        return 0

    model_infos = []
    seen: set[str] = set()
    for item in fetched:
        model_id = str(item.get("id") or "").strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        display_name = str(item.get("display_name") or item.get("name") or model_id)
        model_infos.append(_minimax_model_info(
            model_id,
            item,
            display_name=display_name,
            source="minimax_live_catalog",
            source_confidence="live_minimax_model_list",
            last_verified_at=verified_at,
        ))

    if not model_infos:
        logger.warning("MiniMax model fetch contained no usable model ids; preserving previous cache")
        return 0

    entries = {
        model_info.model_id: model_info_to_cache_entry(model_info)
        for model_info in model_infos
    }
    count = _replace_provider_cloud_cache_entries("minimax", entries)
    logger.info("Fetched %d minimax models", count)
    return count


def _fetch_opencode_models(provider_id: str) -> int:
    """Populate OpenCode models from live discovery using provider-qualified keys."""
    import httpx

    from row_bot.providers.auth_store import get_provider_secret
    from row_bot.providers.catalog import model_info_to_cache_entry
    from row_bot.providers.opencode import list_opencode_model_infos, opencode_models_url

    def _string_list(value: object) -> list[str] | None:
        if not isinstance(value, list):
            return None
        return [str(item).strip().lower() for item in value if str(item).strip()]

    def _opencode_input_modalities(item: dict) -> list[str] | None:
        modalities = item.get("modalities")
        if isinstance(modalities, dict):
            values = _string_list(modalities.get("input"))
            if values is not None:
                return values
        for key in ("input_modalities", "inputModalities"):
            values = _string_list(item.get(key))
            if values is not None:
                return values
        architecture = item.get("architecture")
        if isinstance(architecture, dict):
            values = _string_list(architecture.get("input_modalities"))
            if values is not None:
                return values
        return None

    existing_rows = _provider_cloud_cache_entries(provider_id)
    api_key = get_provider_secret(provider_id)
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    model_ids: list[str] | None = None
    image_input_model_ids: set[str] | None = None
    source = "live"
    try:
        resp = httpx.get(opencode_models_url(provider_id), headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        model_ids = []
        discovered_image_inputs: set[str] = set()
        saw_input_metadata = False
        for item in data:
            if not isinstance(item, dict):
                continue
            model_id = str(item.get("id") or "").strip()
            if not model_id:
                continue
            model_ids.append(model_id)
            input_modalities = _opencode_input_modalities(item)
            if input_modalities is not None:
                saw_input_metadata = True
                if "image" in input_modalities:
                    discovered_image_inputs.add(model_id)
        if saw_input_metadata:
            image_input_model_ids = discovered_image_inputs
    except Exception as exc:
        if existing_rows:
            logger.warning("Failed to fetch %s models: %s; preserving previous OpenCode cache", provider_id, exc)
            return 0
        logger.warning("Failed to fetch %s models: %s; using static OpenCode fallback", provider_id, exc)
        source = "fallback"

    infos = list_opencode_model_infos(
        provider_id,
        model_ids=model_ids,
        image_input_model_ids=image_input_model_ids,
    )
    entries: dict[str, dict] = {}
    for model_info in infos:
        entry = model_info_to_cache_entry(model_info)
        entry["source"] = "opencode_live_catalog" if source == "live" else "opencode_static_fallback"
        entries[model_info.selection_ref] = entry
    count = _replace_provider_cloud_cache_entries(provider_id, entries)
    logger.info("Fetched %d %s models", count, provider_id)
    return count


def _fetch_codex_models() -> int:
    """Populate ChatGPT/Codex models from Row-Bot-owned OAuth discovery."""
    from row_bot.providers.catalog import model_info_to_cache_entry
    from row_bot.providers.codex import list_codex_model_infos

    try:
        infos = list_codex_model_infos(force_refresh=True)
    except Exception as exc:
        logger.warning("Failed to fetch codex models: %s", exc)
        return 0
    if not infos:
        return 0
    entries = {
        model_info.selection_ref: model_info_to_cache_entry(model_info)
        for model_info in infos
    }
    count = _replace_provider_cloud_cache_entries("codex", entries)
    logger.info("Fetched %d codex models", count)
    return count


def _fetch_claude_subscription_models() -> int:
    """Populate Claude Subscription models from Row-Bot-owned OAuth catalog."""
    from row_bot.providers.catalog import model_info_to_cache_entry
    from row_bot.providers.claude_subscription import list_claude_subscription_model_infos

    try:
        infos = list_claude_subscription_model_infos(force_refresh=True)
    except Exception as exc:
        logger.warning("Failed to fetch claude_subscription models: %s", exc)
        return 0
    if not infos:
        return 0
    entries = {
        model_info.selection_ref: model_info_to_cache_entry(model_info)
        for model_info in infos
    }
    count = _replace_provider_cloud_cache_entries("claude_subscription", entries)
    logger.info("Fetched %d claude_subscription models", count)
    return count


def _fetch_xai_oauth_models() -> int:
    """Populate xAI OAuth models from Row-Bot-owned OAuth catalog."""
    from row_bot.providers.catalog import model_info_to_cache_entry
    from row_bot.providers.xai_oauth import (
        list_xai_oauth_model_infos,
        run_xai_oauth_vision_probe,
        xai_oauth_vision_probe_needed,
    )

    try:
        infos = list_xai_oauth_model_infos(force_refresh=True)
    except Exception as exc:
        logger.warning("Failed to fetch xai_oauth models: %s", exc)
        return 0
    if not infos:
        return 0
    try:
        if xai_oauth_vision_probe_needed():
            logger.info("Running xai_oauth vision probe during model refresh")
            probe = run_xai_oauth_vision_probe()
            confirmed = probe.get("confirmed_model_ids") if isinstance(probe, dict) else []
            failed = probe.get("failed_model_ids") if isinstance(probe, dict) else []
            logger.info(
                "Completed xai_oauth vision probe during model refresh: confirmed=%d failed=%d",
                len(confirmed) if isinstance(confirmed, list) else 0,
                len(failed) if isinstance(failed, list) else 0,
            )
            infos = list_xai_oauth_model_infos()
    except Exception as exc:
        logger.warning("Failed to probe xai_oauth vision during model refresh: %s", exc)
    entries = {
        model_info.selection_ref: model_info_to_cache_entry(model_info)
        for model_info in infos
    }
    count = _replace_provider_cloud_cache_entries("xai_oauth", entries)
    logger.info("Fetched %d xai_oauth models", count)
    return count


def refresh_cloud_provider_models(provider_id: str) -> ProviderCatalogRefreshResult:
    """Refresh one provider while retaining its last-known-good catalog."""
    provider = str(provider_id or "").strip()
    before_stamp = _provider_catalog_cache_stamp(provider)
    try:
        update_count = _fetch_cloud_models(provider)
    except Exception as exc:
        logger.warning("Failed to refresh %s model catalog: %s", provider, exc)
        update_count = 0
    after_entries = _provider_cloud_cache_entries(provider)
    after_stamp = _provider_catalog_cache_stamp(provider)

    sources = {
        str(info.get("source") or "")
        for info in after_entries.values()
        if isinstance(info, dict)
    }
    uses_fallback = bool(sources) and all("fallback" in source for source in sources)
    subscription_cache_reused = (
        provider in _SUBSCRIPTION_CATALOG_PROVIDER_IDS
        and bool(before_stamp)
        and before_stamp == after_stamp
        and bool(after_entries)
    )
    if uses_fallback:
        status = "fallback"
        source = "static_fallback"
    elif update_count > 0 and not subscription_cache_reused:
        status = "live"
        source = "provider_catalog"
    elif after_entries:
        status = "cached"
        source = "last_known_good"
    else:
        status = "empty"
        source = ""

    effective_count = len(after_entries)
    preserved_count = effective_count if status == "cached" else 0
    live_count = update_count if status == "live" else 0
    message = ""
    if status == "cached":
        message = f"No live catalog update; kept {effective_count} cached model(s)."
    elif status == "fallback":
        message = f"Using {effective_count} documented fallback model(s)."
    elif status == "empty":
        message = "No usable models were discovered."

    return ProviderCatalogRefreshResult(
        provider_id=provider,
        status=status,
        effective_count=effective_count,
        update_count=update_count,
        live_count=live_count,
        preserved_count=preserved_count,
        source=source,
        message=message,
    )


def refresh_cloud_models_detailed() -> dict[str, ProviderCatalogRefreshResult]:
    """Refresh every registered remote catalog and return provider outcomes.

    Also refreshes the context catalog (keyless) for accurate context sizes.
    If the current default model is temporarily absent from the refreshed
    cache, keep the user's saved choice instead of rewriting it to a local
    fallback.
    """
    global _current_model, _llm_instance
    # Remember the current model so a refresh gap cannot rewrite its default.
    _prev_model = _current_model
    _was_cloud = is_cloud_model(_prev_model)

    # Fetch context catalog first so OpenAI models get accurate sizes
    fetch_context_catalog()
    results = {
        provider_id: refresh_cloud_provider_models(provider_id)
        for provider_id in REFRESHABLE_CLOUD_PROVIDER_IDS
    }
    _save_cloud_cache()

    # Do not rewrite the user's default just because a provider refresh missed
    # it. Keyring migrations, offline starts, and provider list gaps can all be
    # temporary, and clobbering model_settings.json makes new threads surprise
    # the user with the first local Ollama model.
    if _was_cloud and not _cloud_model_available_after_refresh(_prev_model):
        logger.warning(
            "Default cloud model '%s' was not returned by refresh; preserving saved default.",
            _prev_model,
        )
        _llm_instance = None  # lazy-recreate on next get_llm()

    return results


def refresh_cloud_models() -> int:
    """Refresh all remote provider catalogs and return the update count."""
    return sum(result.update_count for result in refresh_cloud_models_detailed().values())


def _cloud_model_available_after_refresh(model_name: str) -> bool:
    """Return whether a saved cloud default is known after provider refresh.

    Direct API providers store bare runtime IDs in ``_cloud_model_cache`` while
    newer selectors may save provider-qualified refs such as
    ``model:codex:gpt-5.6-sol``. Subscription providers also keep their own
    provider catalog cache, so check that cache before warning about a missing
    default when the shared refresh cache is unavailable.
    """
    runtime_model = _runtime_model_name(model_name)
    if model_name in _cloud_model_cache or runtime_model in _cloud_model_cache:
        return True

    parsed = _parse_provider_model_ref(model_name)
    if not parsed:
        return False
    provider_id, parsed_model = parsed
    if _provider_qualified_cloud_cache_key(provider_id, parsed_model) in _cloud_model_cache:
        return True
    if parsed_model in _cloud_model_cache:
        info = _cloud_model_cache.get(parsed_model, {})
        return not provider_id or info.get("provider") == provider_id
    if provider_id == "codex":
        try:
            from row_bot.providers.codex import list_codex_model_infos_for_status

            return any(
                model_info.model_id == parsed_model
                for model_info in list_codex_model_infos_for_status()
            )
        except Exception:
            logger.debug("Could not validate Codex default model after refresh", exc_info=True)
    if provider_id == "claude_subscription":
        try:
            from row_bot.providers.claude_subscription import list_claude_subscription_model_infos_for_status

            return any(
                model_info.model_id == parsed_model
                for model_info in list_claude_subscription_model_infos_for_status()
            )
        except Exception:
            logger.debug("Could not validate Claude Subscription default model after refresh", exc_info=True)
    if provider_id == "xai_oauth":
        try:
            from row_bot.providers.xai_oauth import list_xai_oauth_model_infos_for_status

            return any(
                model_info.model_id == parsed_model
                for model_info in list_xai_oauth_model_infos_for_status()
            )
        except Exception:
            logger.debug("Could not validate xAI OAuth default model after refresh", exc_info=True)
    return False


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
        from row_bot.providers.ollama import is_ollama_tool_capable
        return is_ollama_tool_capable(model_name)
    except Exception:
        pass
    family = model_name.split(":")[0]
    return family in _TOOL_COMPATIBLE_FAMILIES


def check_tool_support(model_name: str) -> bool:
    """Send a minimal tool-call request to verify the model supports tools.

    Returns True if the model accepts tools, False if it rejects them (400).
    """
    client = _ollama_client()
    if not client:
        return False
    try:
        client.chat(
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
