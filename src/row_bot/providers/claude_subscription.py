from __future__ import annotations

import base64
import hashlib
import json
import os
import pathlib
import secrets
import shutil
import subprocess
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import row_bot.secret_store as secret_store

from row_bot.providers.models import AuthMethod, ModelInfo, ModelModality, ModelTask, ProviderHealth, TransportMode
from row_bot.providers.oauth import OAuthToken, expiry_from_seconds

CLAUDE_SUBSCRIPTION_PROVIDER_ID = "claude_subscription"
CLAUDE_HOME_ENV = "CLAUDE_HOME"
CLAUDE_CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"
DEFAULT_CLAUDE_HOME = pathlib.Path.home() / ".claude"
CLAUDE_CREDENTIALS_FILE = ".credentials.json"
CLAUDE_LEGACY_CREDENTIALS_FILE = pathlib.Path.home() / ".claude.json"
CLAUDE_CODE_BINARY_NAMES = ("claude", "claude.exe")
CLAUDE_SUBSCRIPTION_API_ROOT_URL = "https://api.anthropic.com"
CLAUDE_SUBSCRIPTION_API_BASE_URL = f"{CLAUDE_SUBSCRIPTION_API_ROOT_URL}/v1"
CLAUDE_SUBSCRIPTION_MESSAGES_URL = f"{CLAUDE_SUBSCRIPTION_API_BASE_URL}/messages"
CLAUDE_SUBSCRIPTION_MODELS_URL = f"{CLAUDE_SUBSCRIPTION_API_BASE_URL}/models"
CLAUDE_SUBSCRIPTION_OAUTH_TIMEOUT_SECONDS = 15 * 60

ROW_BOT_CLAUDE_CLIENT_ID_ENV = "ROW_BOT_CLAUDE_SUBSCRIPTION_CLIENT_ID"
ROW_BOT_CLAUDE_AUTHORIZE_URL_ENV = "ROW_BOT_CLAUDE_SUBSCRIPTION_AUTHORIZE_URL"
ROW_BOT_CLAUDE_TOKEN_URL_ENV = "ROW_BOT_CLAUDE_SUBSCRIPTION_TOKEN_URL"
ROW_BOT_CLAUDE_REDIRECT_URI_ENV = "ROW_BOT_CLAUDE_SUBSCRIPTION_REDIRECT_URI"
ROW_BOT_CLAUDE_SCOPES_ENV = "ROW_BOT_CLAUDE_SUBSCRIPTION_SCOPES"
DEFAULT_CLAUDE_OAUTH_AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
DEFAULT_CLAUDE_OAUTH_TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
DEFAULT_CLAUDE_OAUTH_REFRESH_TOKEN_URLS = (
    "https://platform.claude.com/v1/oauth/token",
    DEFAULT_CLAUDE_OAUTH_TOKEN_URL,
)
DEFAULT_CLAUDE_OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
DEFAULT_CLAUDE_OAUTH_REDIRECT_URI = "https://console.anthropic.com/oauth/code/callback"
DEFAULT_CLAUDE_OAUTH_SCOPES = ("org:create_api_key", "user:profile", "user:inference")
CLAUDE_SUBSCRIPTION_OAUTH_USER_AGENT_VERSION_FALLBACK = "2.1.74"
CLAUDE_SUBSCRIPTION_OAUTH_USER_AGENT_TEMPLATE = "claude-cli/{version} (external, cli)"
CLAUDE_SUBSCRIPTION_CLAUDE_CODE_SYSTEM_PREFIX = "You are Claude Code, Anthropic's official CLI for Claude."
CLAUDE_SUBSCRIPTION_MCP_TOOL_PREFIX = "mcp_"
CLAUDE_SUBSCRIPTION_COMMON_BETAS = (
    "interleaved-thinking-2025-05-14",
    "fine-grained-tool-streaming-2025-05-14",
)
CLAUDE_SUBSCRIPTION_OAUTH_ONLY_BETAS = ("claude-code-20250219", "oauth-2025-04-20")
CLAUDE_SUBSCRIPTION_OAUTH_BETAS = CLAUDE_SUBSCRIPTION_COMMON_BETAS + CLAUDE_SUBSCRIPTION_OAUTH_ONLY_BETAS

_claude_subscription_cli_version_cache: str | None = None

SENSITIVE_KEY_PARTS = (
    "access",
    "api_key",
    "authorization",
    "bearer",
    "credential",
    "id_token",
    "password",
    "refresh",
    "secret",
    "session",
    "setup",
    "token",
)


@dataclass(frozen=True)
class ClaudeSubscriptionOAuthFlow:
    authorization_url: str
    code_verifier: str
    code_challenge: str
    state: str
    redirect_uri: str
    expires_at: str
    authorize_url: str
    token_url: str
    client_id: str
    scopes: tuple[str, ...] = DEFAULT_CLAUDE_OAUTH_SCOPES


@dataclass(frozen=True)
class ClaudeSubscriptionAuthorization:
    authorization_code: str
    code_verifier: str
    redirect_uri: str
    token_url: str
    client_id: str
    state: str = ""


@dataclass(frozen=True)
class ClaudeSubscriptionTokenSet:
    access_token: str
    refresh_token: str = ""
    id_token: str = ""
    expires_at: str = ""
    user_id: str = ""
    account_id: str = ""
    plan_type: str = ""
    scopes: tuple[str, ...] = ()

    def oauth_token(self) -> OAuthToken:
        return OAuthToken(
            access_token=self.access_token,
            refresh_token=self.refresh_token,
            expires_at=self.expires_at,
            scopes=self.scopes,
        )


@dataclass(frozen=True)
class ClaudeSubscriptionTokenHealth:
    status: str
    detail: str
    credentials: ClaudeSubscriptionTokenSet = ClaudeSubscriptionTokenSet(access_token="")

    @property
    def runnable(self) -> bool:
        return self.status in {"valid", "refreshed"} and bool(self.credentials.access_token)


@dataclass(frozen=True)
class ClaudeSubscriptionCredentialSummary:
    cli_installed: bool = False
    cli_version: str = ""
    cli_source: str = ""
    auth_status: str = ""
    credential_file_exists: bool = False
    credential_source: str = ""
    credential_path_hash: str = ""
    legacy_credential_file_exists: bool = False
    legacy_credential_path_hash: str = ""
    expires_at: str = ""
    user_hash: str = ""
    account_id_hash: str = ""
    metadata_only: bool = True

    def to_json(self) -> dict[str, Any]:
        return {
            "cli_installed": self.cli_installed,
            "cli_version": self.cli_version,
            "cli_source": self.cli_source,
            "auth_status": self.auth_status,
            "credential_file_exists": self.credential_file_exists,
            "credential_source": self.credential_source,
            "credential_path_hash": self.credential_path_hash,
            "legacy_credential_file_exists": self.legacy_credential_file_exists,
            "legacy_credential_path_hash": self.legacy_credential_path_hash,
            "expires_at": self.expires_at,
            "user_hash": self.user_hash,
            "account_id_hash": self.account_id_hash,
            "metadata_only": self.metadata_only,
        }


@dataclass(frozen=True)
class ClaudeSubscriptionModelCatalog:
    models: tuple[ModelInfo, ...]
    source: str = "fallback"
    fetched_at: str = ""


FALLBACK_CLAUDE_SUBSCRIPTION_MODELS = [
    {
        "id": "claude-sonnet-4-6",
        "display_name": "Claude Sonnet 4.6",
        "context_window": 1_000_000,
        "input_modalities": ("text", "image"),
        "source_confidence": "documented_claude_model",
        "recommended": True,
    },
    {
        "id": "claude-opus-4-8",
        "display_name": "Claude Opus 4.8",
        "context_window": 1_000_000,
        "input_modalities": ("text", "image"),
        "source_confidence": "documented_claude_model",
        "recommended": False,
    },
    {
        "id": "claude-haiku-4-5-20251001",
        "display_name": "Claude Haiku 4.5",
        "context_window": 200_000,
        "input_modalities": ("text", "image"),
        "source_confidence": "documented_claude_model",
        "recommended": False,
    },
    {
        "id": "claude-fable-5",
        "display_name": "Claude Fable 5",
        "context_window": 1_000_000,
        "input_modalities": ("text", "image"),
        "source_confidence": "documented_claude_model",
        "recommended": False,
    },
]


def claude_home() -> pathlib.Path:
    configured = os.environ.get(CLAUDE_CONFIG_DIR_ENV) or os.environ.get(CLAUDE_HOME_ENV)
    return pathlib.Path(configured).expanduser() if configured else DEFAULT_CLAUDE_HOME


def claude_credentials_path(home: pathlib.Path | str | None = None) -> pathlib.Path:
    root = pathlib.Path(home).expanduser() if home is not None else claude_home()
    return root / CLAUDE_CREDENTIALS_FILE


def claude_legacy_credentials_path() -> pathlib.Path:
    return pathlib.Path(CLAUDE_LEGACY_CREDENTIALS_FILE).expanduser()


def path_hash(path: pathlib.Path | str) -> str:
    expanded = pathlib.Path(path).expanduser()
    return hashlib.sha256(str(expanded).encode("utf-8")).hexdigest()[:12]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _value_type(value: Any) -> str:
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if value is None:
        return "null"
    return type(value).__name__


def _looks_sensitive(key: str) -> bool:
    normalized = str(key or "").lower().replace("-", "_")
    return any(part in normalized for part in SENSITIVE_KEY_PARTS)


def _redact_text(value: str, *, limit: int = 300) -> str:
    text = str(value or "")
    for marker in ("access_token", "refresh_token", "id_token", "setup_token", "Authorization", "Bearer"):
        text = text.replace(marker, "[redacted]")
    return text[:limit]


def _safe_error_body(response: Any) -> str:
    try:
        text = str(getattr(response, "text", "") or "")
    except Exception:
        text = ""
    return _redact_text(text)


def _json_response(response: Any) -> dict[str, Any]:
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Claude Subscription auth response was not a JSON object.")
    return payload


def _new_http_client(timeout: float = 30.0) -> Any:
    import httpx

    return httpx.Client(timeout=timeout)


def _is_pytest_running() -> bool:
    return "PYTEST_CURRENT_TEST" in os.environ


def _parse_expires_at(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _expires_soon(expires_at: str, *, skew_seconds: int = 120) -> bool:
    parsed = _parse_expires_at(expires_at)
    if parsed is None:
        return False
    return parsed <= _utcnow() + timedelta(seconds=max(0, skew_seconds))


def _decode_jwt_claims(token: str) -> dict[str, Any]:
    if not isinstance(token, str) or not token.strip():
        return {}
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        return claims if isinstance(claims, dict) else {}
    except Exception:
        return {}


def _string_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(part for part in value.replace(",", " ").split() if part)
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(str(item) for item in value if str(item))
    return ()


def claude_subscription_token_metadata(access_token: str = "", id_token: str = "") -> dict[str, Any]:
    claims = _decode_jwt_claims(access_token) or _decode_jwt_claims(id_token)
    user_id = claims.get("sub") or claims.get("user_id") or claims.get("userId")
    account_id = claims.get("account_id") or claims.get("accountId") or claims.get("organization_id")
    plan_type = claims.get("plan_type") or claims.get("planType") or claims.get("tier")
    exp = claims.get("exp")
    expires_at = ""
    if isinstance(exp, (int, float)):
        try:
            expires_at = datetime.fromtimestamp(float(exp), timezone.utc).isoformat()
        except Exception:
            expires_at = ""
    scopes = _string_tuple(claims.get("scope") or claims.get("scp") or claims.get("scopes"))
    return {
        "user_id": user_id if isinstance(user_id, str) else "",
        "user_hash": secret_store.fingerprint(user_id) if isinstance(user_id, str) and user_id else "",
        "account_id": account_id if isinstance(account_id, str) else "",
        "account_id_hash": secret_store.fingerprint(account_id) if isinstance(account_id, str) and account_id else "",
        "plan_type": plan_type if isinstance(plan_type, str) else "",
        "expires_at": expires_at,
        "scopes": scopes,
    }


def _token_set_from_payload(payload: dict[str, Any], *, fallback_refresh_token: str = "") -> ClaudeSubscriptionTokenSet:
    access_token = str(payload.get("access_token") or payload.get("token") or "").strip()
    if not access_token:
        raise RuntimeError("Claude Subscription token response did not include an access_token.")
    refresh_token = str(payload.get("refresh_token") or fallback_refresh_token or "").strip()
    id_token = str(payload.get("id_token") or "").strip()
    metadata = claude_subscription_token_metadata(access_token, id_token)
    expires_at = str(metadata.get("expires_at") or "")
    expires_in = payload.get("expires_in")
    if not expires_at and isinstance(expires_in, (int, float)):
        expires_at = (_utcnow() + timedelta(seconds=max(0, float(expires_in)))).isoformat()
    scopes = _string_tuple(payload.get("scope") or payload.get("scopes")) or tuple(metadata.get("scopes") or ())
    return ClaudeSubscriptionTokenSet(
        access_token=access_token,
        refresh_token=refresh_token,
        id_token=id_token,
        expires_at=expires_at,
        user_id=str(metadata.get("user_id") or ""),
        account_id=str(metadata.get("account_id") or ""),
        plan_type=str(metadata.get("plan_type") or ""),
        scopes=scopes,
    )


def claude_subscription_runtime_credentials(
    *,
    refresh_if_needed: bool = True,
    http_client: Any | None = None,
) -> ClaudeSubscriptionTokenSet:
    from row_bot.providers.auth_store import get_provider_secret
    from row_bot.providers.config import load_provider_config

    access_token = get_provider_secret(CLAUDE_SUBSCRIPTION_PROVIDER_ID, "access_token")
    refresh_token = get_provider_secret(CLAUDE_SUBSCRIPTION_PROVIDER_ID, "refresh_token")
    id_token = get_provider_secret(CLAUDE_SUBSCRIPTION_PROVIDER_ID, "id_token")
    provider_cfg = load_provider_config().get("providers", {}).get(CLAUDE_SUBSCRIPTION_PROVIDER_ID, {})
    if provider_cfg.get("auth_method") != AuthMethod.OAUTH_PKCE.value:
        return ClaudeSubscriptionTokenSet(access_token="")
    expires_at = str(provider_cfg.get("expires_at") or "")
    user_id = get_provider_secret(CLAUDE_SUBSCRIPTION_PROVIDER_ID, "user_id")
    account_id = get_provider_secret(CLAUDE_SUBSCRIPTION_PROVIDER_ID, "account")
    plan_type = str(provider_cfg.get("plan_type") or "")
    scopes = _string_tuple(provider_cfg.get("scopes"))

    metadata = claude_subscription_token_metadata(access_token, id_token)
    if not user_id:
        user_id = str(metadata.get("user_id") or "")
    if not account_id:
        account_id = str(metadata.get("account_id") or "")
    if not expires_at:
        expires_at = str(metadata.get("expires_at") or "")
    if not plan_type:
        plan_type = str(metadata.get("plan_type") or "")
    if not scopes:
        scopes = tuple(metadata.get("scopes") or ())

    if refresh_if_needed and refresh_token and (not access_token or _expires_soon(expires_at)):
        refreshed = refresh_claude_subscription_token(refresh_token, http_client=http_client)
        saved = save_claude_subscription_oauth_tokens(refreshed)
        access_token = refreshed.access_token
        refresh_token = refreshed.refresh_token or refresh_token
        id_token = refreshed.id_token or id_token
        user_id = refreshed.user_id or user_id
        account_id = refreshed.account_id or account_id
        expires_at = refreshed.expires_at or str(saved.get("expires_at") or expires_at)
        plan_type = refreshed.plan_type or str(saved.get("plan_type") or plan_type)
        scopes = refreshed.scopes or scopes

    return ClaudeSubscriptionTokenSet(
        access_token=access_token,
        refresh_token=refresh_token,
        id_token=id_token,
        expires_at=expires_at,
        user_id=user_id,
        account_id=account_id,
        plan_type=plan_type,
        scopes=scopes,
    )


def check_claude_subscription_token_health(
    *,
    refresh_if_needed: bool = True,
    http_client: Any | None = None,
) -> ClaudeSubscriptionTokenHealth:
    try:
        credentials = claude_subscription_runtime_credentials(refresh_if_needed=False)
    except Exception as exc:
        return ClaudeSubscriptionTokenHealth("error", f"Could not read Claude Subscription credentials: {exc}")

    if not credentials.access_token and not credentials.refresh_token:
        return ClaudeSubscriptionTokenHealth(
            "missing",
            "Claude Subscription needs to be connected in Settings -> Providers before subscription models can run.",
            credentials,
        )

    should_refresh = bool(
        refresh_if_needed
        and credentials.refresh_token
        and (not credentials.access_token or _expires_soon(credentials.expires_at))
    )
    if should_refresh:
        try:
            refreshed = refresh_claude_subscription_token(credentials.refresh_token, http_client=http_client)
            saved = save_claude_subscription_oauth_tokens(refreshed)
            credentials = ClaudeSubscriptionTokenSet(
                access_token=refreshed.access_token,
                refresh_token=refreshed.refresh_token or credentials.refresh_token,
                id_token=refreshed.id_token or credentials.id_token,
                expires_at=refreshed.expires_at or str(saved.get("expires_at") or credentials.expires_at),
                user_id=refreshed.user_id or credentials.user_id,
                account_id=refreshed.account_id or credentials.account_id,
                plan_type=refreshed.plan_type or str(saved.get("plan_type") or credentials.plan_type),
                scopes=refreshed.scopes or credentials.scopes,
            )
            return ClaudeSubscriptionTokenHealth("refreshed", "Claude Subscription token refreshed successfully.", credentials)
        except Exception as exc:
            text = _redact_text(str(exc))
            lowered = text.lower()
            if any(marker in lowered for marker in ("invalid_grant", "revoked", "expired")):
                return ClaudeSubscriptionTokenHealth(
                    "expired",
                    "Claude Subscription sign-in expired or was revoked. Reconnect Claude Subscription in Settings -> Providers.",
                    credentials,
                )
            return ClaudeSubscriptionTokenHealth("error", f"Claude Subscription token refresh failed: {text}", credentials)

    if not credentials.access_token:
        return ClaudeSubscriptionTokenHealth(
            "missing",
            "Claude Subscription access token is missing and no refresh was possible. Reconnect Claude Subscription in Settings -> Providers.",
            credentials,
        )
    if _expires_soon(credentials.expires_at):
        return ClaudeSubscriptionTokenHealth(
            "expired",
            "Claude Subscription token is expired and no refresh token is available. Reconnect Claude Subscription in Settings -> Providers.",
            credentials,
        )
    return ClaudeSubscriptionTokenHealth("valid", "Claude Subscription token is valid.", credentials)


def claude_subscription_runtime_available() -> bool:
    try:
        health = check_claude_subscription_token_health(refresh_if_needed=False)
    except Exception:
        return False
    return health.runnable


def claude_subscription_reconnect_message(detail: str = "") -> str:
    suffix = f" {detail}" if detail else ""
    return (
        "Claude Subscription needs to be reconnected before using this model. "
        "Open Settings -> Providers -> Claude Subscription, reconnect, then try again."
        f"{suffix}"
    )


def claude_subscription_runtime_block_message(*, refresh_if_needed: bool = True) -> str | None:
    health = check_claude_subscription_token_health(refresh_if_needed=refresh_if_needed)
    if health.runnable:
        return None
    return claude_subscription_reconnect_message(health.detail)


def _pkce_verifier() -> str:
    return secrets.token_urlsafe(64)[:96]


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _configured_oauth_value(explicit: str | None, env_var: str, label: str, *, default: str = "") -> str:
    value = str(explicit or os.environ.get(env_var) or default or "").strip()
    if not value:
        raise RuntimeError(
            f"Claude Subscription OAuth {label} is not configured. "
            "Row-Bot needs an OAuth client before it can start an in-app Claude login."
        )
    return value


def _configured_scopes(explicit: tuple[str, ...] | list[str] | str | None = None) -> tuple[str, ...]:
    scopes = _string_tuple(explicit)
    if scopes:
        return scopes
    env_scopes = _string_tuple(os.environ.get(ROW_BOT_CLAUDE_SCOPES_ENV, ""))
    return env_scopes or DEFAULT_CLAUDE_OAUTH_SCOPES


def _claude_cli_version_token(raw_version: str) -> str:
    version = str(raw_version or "").strip().split()
    if version and version[0] and version[0][0].isdigit():
        return version[0]
    return ""


def claude_subscription_cli_version() -> str:
    global _claude_subscription_cli_version_cache
    if _claude_subscription_cli_version_cache is not None:
        return _claude_subscription_cli_version_cache
    info = claude_cli_info()
    version = _claude_cli_version_token(str(info.get("version") or ""))
    _claude_subscription_cli_version_cache = version or CLAUDE_SUBSCRIPTION_OAUTH_USER_AGENT_VERSION_FALLBACK
    return _claude_subscription_cli_version_cache


def claude_subscription_oauth_user_agent() -> str:
    return CLAUDE_SUBSCRIPTION_OAUTH_USER_AGENT_TEMPLATE.format(version=claude_subscription_cli_version())


def claude_subscription_oauth_betas() -> str:
    return ",".join(CLAUDE_SUBSCRIPTION_OAUTH_BETAS)


def claude_subscription_oauth_headers(*, accept: str = "application/json") -> dict[str, str]:
    headers = {
        "anthropic-beta": claude_subscription_oauth_betas(),
        "user-agent": claude_subscription_oauth_user_agent(),
        "x-app": "cli",
    }
    if accept:
        headers["Accept"] = accept
    return headers


def claude_subscription_sdk_base_url(base_url: str = CLAUDE_SUBSCRIPTION_API_ROOT_URL) -> str:
    value = str(base_url or CLAUDE_SUBSCRIPTION_API_ROOT_URL).strip().rstrip("/")
    if value.endswith("/v1"):
        value = value[:-3].rstrip("/")
    return value or CLAUDE_SUBSCRIPTION_API_ROOT_URL


def claude_subscription_sdk_client(
    access_token: str,
    *,
    base_url: str = CLAUDE_SUBSCRIPTION_API_ROOT_URL,
    timeout: float = 120.0,
    client_factory: Any | None = None,
) -> Any:
    token = str(access_token or "").strip()
    if not token:
        raise RuntimeError("Claude Subscription access token is missing. Connect Claude Subscription in Settings -> Providers.")
    factory = client_factory
    if factory is None:
        import anthropic

        factory = anthropic.Anthropic
    from httpx import Timeout

    kwargs: dict[str, Any] = {
        "auth_token": token,
        "default_headers": claude_subscription_oauth_headers(accept=""),
        "timeout": Timeout(timeout=float(timeout), connect=10.0),
    }
    if base_url:
        kwargs["base_url"] = claude_subscription_sdk_base_url(base_url)
    return factory(**kwargs)


def claude_subscription_compat_system(system: str | list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [{"type": "text", "text": CLAUDE_SUBSCRIPTION_CLAUDE_CODE_SYSTEM_PREFIX}]
    if isinstance(system, str):
        if system:
            blocks.append({"type": "text", "text": system})
        return blocks
    if isinstance(system, list):
        for block in system:
            if isinstance(block, dict):
                blocks.append(dict(block))
            elif block:
                blocks.append({"type": "text", "text": str(block)})
    return blocks


def claude_subscription_wire_tool_name(name: str) -> str:
    value = str(name or "").strip()
    if not value or value.startswith(CLAUDE_SUBSCRIPTION_MCP_TOOL_PREFIX):
        return value
    return CLAUDE_SUBSCRIPTION_MCP_TOOL_PREFIX + value


def _known_row_bot_tool(name: str) -> bool:
    if not name:
        return False
    try:
        from row_bot.tools import registry as tool_registry

        if tool_registry.get_tool(name) is not None:
            return True
        for tool in tool_registry.get_all_tools():
            for langchain_tool in tool.as_langchain_tools():
                if str(getattr(langchain_tool, "name", "") or "") == name:
                    return True
    except Exception:
        pass
    try:
        from row_bot.plugins import registry as plugin_registry

        if name in plugin_registry.get_plugin_tool_names():
            return True
        if name in plugin_registry.get_enabled_plugin_tool_names():
            return True
    except Exception:
        pass
    return False


def claude_subscription_runtime_tool_name(name: str) -> str:
    value = str(name or "").strip()
    prefix = CLAUDE_SUBSCRIPTION_MCP_TOOL_PREFIX
    if not value.startswith(prefix):
        return value
    stripped = value[len(prefix):]
    if stripped and _known_row_bot_tool(stripped) and not _known_row_bot_tool(value):
        return stripped
    return value


def _authorization_code_and_state(raw_code: str, expected_state: str = "") -> tuple[str, str]:
    raw = str(raw_code or "").strip()
    if not raw:
        raise RuntimeError("Claude Subscription authorization code is missing.")
    code, callback_state = raw, ""
    if "#" in raw:
        code, callback_state = raw.split("#", 1)
    code = code.strip()
    callback_state = callback_state.strip()
    if not code:
        raise RuntimeError("Claude Subscription authorization code is missing.")
    expected = str(expected_state or "").strip()
    if callback_state and expected and callback_state != expected:
        raise RuntimeError("Claude Subscription OAuth state did not match. Start a fresh Claude login and try again.")
    return code, callback_state or expected


def _refresh_token_urls(explicit: str | None) -> tuple[str, ...]:
    if explicit:
        return (_configured_oauth_value(explicit, ROW_BOT_CLAUDE_TOKEN_URL_ENV, "token URL"),)
    env_url = str(os.environ.get(ROW_BOT_CLAUDE_TOKEN_URL_ENV) or "").strip()
    if env_url:
        return (env_url,)
    return DEFAULT_CLAUDE_OAUTH_REFRESH_TOKEN_URLS


def start_claude_subscription_oauth_flow(
    *,
    authorize_url: str | None = None,
    token_url: str | None = None,
    client_id: str | None = None,
    redirect_uri: str | None = None,
    scopes: tuple[str, ...] | list[str] | str | None = None,
) -> ClaudeSubscriptionOAuthFlow:
    normalized_authorize_url = _configured_oauth_value(
        authorize_url,
        ROW_BOT_CLAUDE_AUTHORIZE_URL_ENV,
        "authorization URL",
        default=DEFAULT_CLAUDE_OAUTH_AUTHORIZE_URL,
    )
    normalized_token_url = _configured_oauth_value(
        token_url,
        ROW_BOT_CLAUDE_TOKEN_URL_ENV,
        "token URL",
        default=DEFAULT_CLAUDE_OAUTH_TOKEN_URL,
    )
    normalized_client_id = _configured_oauth_value(
        client_id,
        ROW_BOT_CLAUDE_CLIENT_ID_ENV,
        "client id",
        default=DEFAULT_CLAUDE_OAUTH_CLIENT_ID,
    )
    normalized_redirect_uri = str(
        redirect_uri
        or os.environ.get(ROW_BOT_CLAUDE_REDIRECT_URI_ENV)
        or DEFAULT_CLAUDE_OAUTH_REDIRECT_URI
    ).strip()
    scope_tuple = _configured_scopes(scopes)
    verifier = _pkce_verifier()
    challenge = _pkce_challenge(verifier)
    state = secrets.token_urlsafe(24)
    query = urlencode({
        "code": "true",
        "response_type": "code",
        "client_id": normalized_client_id,
        "redirect_uri": normalized_redirect_uri,
        "scope": " ".join(scope_tuple),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    separator = "&" if "?" in normalized_authorize_url else "?"
    return ClaudeSubscriptionOAuthFlow(
        authorization_url=f"{normalized_authorize_url}{separator}{query}",
        code_verifier=verifier,
        code_challenge=challenge,
        state=state,
        redirect_uri=normalized_redirect_uri,
        expires_at=expiry_from_seconds(CLAUDE_SUBSCRIPTION_OAUTH_TIMEOUT_SECONDS),
        authorize_url=normalized_authorize_url,
        token_url=normalized_token_url,
        client_id=normalized_client_id,
        scopes=scope_tuple,
    )


def exchange_claude_subscription_authorization(
    authorization: ClaudeSubscriptionAuthorization,
    *,
    http_client: Any | None = None,
) -> ClaudeSubscriptionTokenSet:
    code, state = _authorization_code_and_state(authorization.authorization_code, authorization.state)
    payload = {
        "grant_type": "authorization_code",
        "client_id": authorization.client_id,
        "code": code,
        "state": state,
        "redirect_uri": authorization.redirect_uri,
        "code_verifier": authorization.code_verifier,
    }
    client = http_client or _new_http_client()
    owns_client = http_client is None
    try:
        response = client.post(
            authorization.token_url,
            json=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": claude_subscription_oauth_user_agent(),
            },
        )
    finally:
        if owns_client:
            client.close()

    status_code = int(getattr(response, "status_code", 0) or 0)
    if status_code < 200 or status_code >= 300:
        raise RuntimeError(f"Claude Subscription token exchange failed with HTTP {status_code}: {_safe_error_body(response)}")
    return _token_set_from_payload(_json_response(response))


def refresh_claude_subscription_token(
    refresh_token: str,
    *,
    token_url: str | None = None,
    client_id: str | None = None,
    http_client: Any | None = None,
) -> ClaudeSubscriptionTokenSet:
    if not str(refresh_token or "").strip():
        raise RuntimeError("Claude Subscription refresh token is missing.")
    normalized_token_urls = _refresh_token_urls(token_url)
    normalized_client_id = _configured_oauth_value(
        client_id,
        ROW_BOT_CLAUDE_CLIENT_ID_ENV,
        "client id",
        default=DEFAULT_CLAUDE_OAUTH_CLIENT_ID,
    )
    client = http_client or _new_http_client()
    owns_client = http_client is None
    last_response: Any | None = None
    last_exc: Exception | None = None
    try:
        for normalized_token_url in normalized_token_urls:
            try:
                response = client.post(
                    normalized_token_url,
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                        "client_id": normalized_client_id,
                    },
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "User-Agent": claude_subscription_oauth_user_agent(),
                    },
                )
            except Exception as exc:
                last_exc = exc
                continue
            last_response = response
            status_code = int(getattr(response, "status_code", 0) or 0)
            if 200 <= status_code < 300:
                return _token_set_from_payload(_json_response(response), fallback_refresh_token=refresh_token)
    finally:
        if owns_client:
            client.close()

    if last_response is not None:
        status_code = int(getattr(last_response, "status_code", 0) or 0)
        raise RuntimeError(f"Claude Subscription token refresh failed with HTTP {status_code}: {_safe_error_body(last_response)}")
    if last_exc is not None:
        raise RuntimeError(f"Claude Subscription token refresh failed: {_redact_text(str(last_exc))}")
    raise RuntimeError("Claude Subscription token refresh failed.")


def import_claude_subscription_setup_token(
    token: str,
    *,
    expires_at: str = "",
    plan_type: str = "",
) -> dict[str, Any]:
    """Explicitly import a user-provided Claude setup-token into Row-Bot storage."""
    access_token = str(token or "").strip()
    if not access_token:
        raise ValueError("Claude setup token is empty.")
    metadata = claude_subscription_token_metadata(access_token)
    return save_claude_subscription_oauth_tokens(ClaudeSubscriptionTokenSet(
        access_token=access_token,
        expires_at=expires_at or str(metadata.get("expires_at") or ""),
        user_id=str(metadata.get("user_id") or ""),
        account_id=str(metadata.get("account_id") or ""),
        plan_type=plan_type or str(metadata.get("plan_type") or ""),
        scopes=tuple(metadata.get("scopes") or ()),
    ))


def save_claude_subscription_oauth_tokens(token_set: ClaudeSubscriptionTokenSet) -> dict[str, Any]:
    from row_bot.providers.auth_store import set_provider_secret
    from row_bot.providers.config import update_provider_config

    set_provider_secret(
        CLAUDE_SUBSCRIPTION_PROVIDER_ID,
        "access_token",
        token_set.access_token,
        source=AuthMethod.OAUTH_PKCE.value,
        auth_method=AuthMethod.OAUTH_PKCE,
    )
    if token_set.refresh_token:
        set_provider_secret(
            CLAUDE_SUBSCRIPTION_PROVIDER_ID,
            "refresh_token",
            token_set.refresh_token,
            source=AuthMethod.OAUTH_PKCE.value,
            auth_method=AuthMethod.OAUTH_PKCE,
        )
    if token_set.id_token:
        set_provider_secret(
            CLAUDE_SUBSCRIPTION_PROVIDER_ID,
            "id_token",
            token_set.id_token,
            source=AuthMethod.OAUTH_PKCE.value,
            auth_method=AuthMethod.OAUTH_PKCE,
        )
    if token_set.user_id:
        set_provider_secret(
            CLAUDE_SUBSCRIPTION_PROVIDER_ID,
            "user_id",
            token_set.user_id,
            source=AuthMethod.OAUTH_PKCE.value,
            auth_method=AuthMethod.OAUTH_PKCE,
        )
    if token_set.account_id:
        set_provider_secret(
            CLAUDE_SUBSCRIPTION_PROVIDER_ID,
            "account",
            token_set.account_id,
            source=AuthMethod.OAUTH_PKCE.value,
            auth_method=AuthMethod.OAUTH_PKCE,
        )

    token_metadata = claude_subscription_token_metadata(token_set.access_token, token_set.id_token)
    fingerprint = secret_store.fingerprint(token_set.access_token)
    now = _utcnow().isoformat()

    def _update(cfg: dict[str, Any]) -> None:
        entry = cfg.setdefault("providers", {}).setdefault(CLAUDE_SUBSCRIPTION_PROVIDER_ID, {})
        entry.update({
            "provider_id": CLAUDE_SUBSCRIPTION_PROVIDER_ID,
            "auth_method": AuthMethod.OAUTH_PKCE.value,
            "configured": True,
            "health": ProviderHealth.CONNECTED.value,
            "source": AuthMethod.OAUTH_PKCE.value,
            "fingerprint": fingerprint,
            "expires_at": token_set.expires_at or token_metadata.get("expires_at") or "",
            "user_hash": secret_store.fingerprint(token_set.user_id) if token_set.user_id else token_metadata.get("user_hash") or "",
            "account_id_hash": secret_store.fingerprint(token_set.account_id) if token_set.account_id else token_metadata.get("account_id_hash") or "",
            "plan_type": token_set.plan_type or token_metadata.get("plan_type") or "",
            "scopes": list(token_set.scopes or token_metadata.get("scopes") or ()),
            "updated_at": now,
            "last_error": "",
            "external_reference_exists": False,
            "external_reference_metadata_only": True,
        })
        entry.pop("last_runtime_probe", None)

    cfg = update_provider_config(_update)
    return dict(cfg.get("providers", {}).get(CLAUDE_SUBSCRIPTION_PROVIDER_ID, {}))


def disconnect_claude_subscription_metadata(*, remove_row_bot_tokens: bool = True) -> None:
    from row_bot.providers.auth_store import delete_provider_secret
    from row_bot.providers.config import update_provider_config

    if remove_row_bot_tokens:
        for credential_name in ("access_token", "refresh_token", "id_token", "user_id", "account"):
            delete_provider_secret(CLAUDE_SUBSCRIPTION_PROVIDER_ID, credential_name)

    def _update(cfg: dict[str, Any]) -> None:
        cfg.setdefault("providers", {}).pop(CLAUDE_SUBSCRIPTION_PROVIDER_ID, None)

    update_provider_config(_update)


def summarize_claude_credentials_json(path: pathlib.Path | str | None = None) -> dict[str, Any]:
    target = pathlib.Path(path).expanduser() if path is not None else claude_credentials_path()
    summary: dict[str, Any] = {
        "label": str(path or pathlib.Path("~/.claude") / CLAUDE_CREDENTIALS_FILE),
        "path_hash": path_hash(target),
        "exists": target.exists(),
        "key_names": [],
        "key_types": {},
        "sensitive_key_names": [],
        "expires_at": "",
        "user_hash": "",
        "account_id_hash": "",
        "error": "",
    }
    if not target.exists():
        return summary
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        summary["error"] = "invalid_json"
        return summary
    if not isinstance(payload, dict):
        summary["error"] = "not_object"
        return summary
    key_names = sorted(str(key) for key in payload.keys())
    summary["key_names"] = key_names
    summary["key_types"] = {str(key): _value_type(value) for key, value in payload.items()}
    summary["sensitive_key_names"] = [key for key in key_names if _looks_sensitive(key)]
    summary.update(_safe_credential_metadata(payload))
    return summary


def _safe_credential_metadata(payload: dict[str, Any]) -> dict[str, str]:
    metadata = {"expires_at": "", "user_hash": "", "account_id_hash": ""}
    for key in ("expires_at", "expiresAt", "expiration", "expiry"):
        value = payload.get(key)
        if isinstance(value, (int, float)):
            try:
                metadata["expires_at"] = datetime.fromtimestamp(float(value), timezone.utc).isoformat()
                break
            except Exception:
                pass
        elif isinstance(value, str) and _parse_expires_at(value):
            metadata["expires_at"] = _parse_expires_at(value).isoformat() if _parse_expires_at(value) else ""
            break
    for key in ("user_id", "userId", "account", "email"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            metadata["user_hash"] = secret_store.fingerprint(value)
            break
        if isinstance(value, dict):
            for subkey in ("id", "email", "user_id"):
                subvalue = value.get(subkey)
                if isinstance(subvalue, str) and subvalue:
                    metadata["user_hash"] = secret_store.fingerprint(subvalue)
                    break
    for key in ("account_id", "accountId", "organization_id", "org_id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            metadata["account_id_hash"] = secret_store.fingerprint(value)
            break
    return metadata


def _first_claude_binary() -> str:
    for binary in CLAUDE_CODE_BINARY_NAMES:
        found = shutil.which(binary)
        if found:
            return found
    return ""


def claude_cli_info(binary: str | None = None) -> dict[str, Any]:
    source = shutil.which(binary) if binary else _first_claude_binary()
    version = ""
    if source:
        try:
            proc = subprocess.run([source, "--version"], capture_output=True, text=True, timeout=5, check=False)
            if proc.returncode == 0:
                version = _redact_text((proc.stdout or proc.stderr).strip(), limit=120)
        except Exception:
            version = ""
    return {
        "installed": bool(source),
        "source": source or "",
        "version": version,
    }


def claude_auth_status(binary: str | None = None) -> str:
    source = shutil.which(binary) if binary else _first_claude_binary()
    if not source:
        return ""
    try:
        proc = subprocess.run([source, "auth", "status"], capture_output=True, text=True, timeout=10, check=False)
    except Exception:
        return ""
    text = (proc.stdout or proc.stderr or "").strip()
    if proc.returncode != 0 and not text:
        return "unknown"
    return _redact_text(text, limit=160)


def discover_claude_subscription_credentials(
    *,
    credentials_path: pathlib.Path | str | None = None,
    legacy_credentials_path: pathlib.Path | str | None = None,
    binary: str | None = None,
) -> dict[str, Any]:
    primary = summarize_claude_credentials_json(credentials_path)
    legacy = summarize_claude_credentials_json(legacy_credentials_path or claude_legacy_credentials_path())
    cli = claude_cli_info(binary)
    status = claude_auth_status(binary) if cli["installed"] else ""
    summary = ClaudeSubscriptionCredentialSummary(
        cli_installed=bool(cli["installed"]),
        cli_version=str(cli.get("version") or ""),
        cli_source=str(cli.get("source") or ""),
        auth_status=status,
        credential_file_exists=bool(primary["exists"]),
        credential_source=str(primary["label"]),
        credential_path_hash=str(primary["path_hash"]),
        legacy_credential_file_exists=bool(legacy["exists"]),
        legacy_credential_path_hash=str(legacy["path_hash"]),
        expires_at=str(primary.get("expires_at") or legacy.get("expires_at") or ""),
        user_hash=str(primary.get("user_hash") or legacy.get("user_hash") or ""),
        account_id_hash=str(primary.get("account_id_hash") or legacy.get("account_id_hash") or ""),
        metadata_only=True,
    )
    result = summary.to_json()
    result.update({
        "provider_id": CLAUDE_SUBSCRIPTION_PROVIDER_ID,
        "source": "claude_code",
        "label": primary["label"],
        "path_hash": primary["path_hash"],
        "exists": bool(primary["exists"] or legacy["exists"]),
        "auth_key_names": primary["key_names"],
        "auth_key_types": primary["key_types"],
        "auth_sensitive_key_names": primary["sensitive_key_names"],
        "auth_error": primary["error"],
        "legacy_label": legacy["label"],
        "legacy_auth_key_names": legacy["key_names"],
        "legacy_auth_sensitive_key_names": legacy["sensitive_key_names"],
        "legacy_auth_error": legacy["error"],
        "external_reference_metadata_only": True,
    })
    return result


def external_reference_metadata(path: pathlib.Path | str | None = None) -> dict[str, Any]:
    discovered = discover_claude_subscription_credentials(credentials_path=path)
    return {
        "provider_id": CLAUDE_SUBSCRIPTION_PROVIDER_ID,
        "source": "external_cli",
        "external_reference_label": discovered["label"],
        "external_reference_path_hash": discovered["path_hash"],
        "external_reference_exists": bool(discovered["exists"]),
        "external_reference_source": "claude_code",
        "external_reference_metadata_only": True,
        "cli_installed": bool(discovered.get("cli_installed")),
        "cli_version": str(discovered.get("cli_version") or ""),
        "auth_status": str(discovered.get("auth_status") or ""),
        "auth_key_names": list(discovered.get("auth_key_names") or []),
        "auth_sensitive_key_names": list(discovered.get("auth_sensitive_key_names") or []),
        "user_hash": str(discovered.get("user_hash") or ""),
        "account_id_hash": str(discovered.get("account_id_hash") or ""),
        "expires_at": str(discovered.get("expires_at") or ""),
    }


def save_external_reference(path: pathlib.Path | str | None = None) -> dict[str, Any]:
    from row_bot.providers.config import update_provider_config

    metadata = external_reference_metadata(path)

    def _update(cfg: dict[str, Any]) -> None:
        entry = cfg.setdefault("providers", {}).setdefault(CLAUDE_SUBSCRIPTION_PROVIDER_ID, {})
        entry.update(metadata)
        entry.update({
            "provider_id": CLAUDE_SUBSCRIPTION_PROVIDER_ID,
            "auth_method": AuthMethod.EXTERNAL_CLI.value,
            "configured": bool(metadata["external_reference_exists"]),
            "health": ProviderHealth.CONNECTED.value if metadata["external_reference_exists"] else ProviderHealth.MISSING_AUTH.value,
            "source": "external_cli",
            "fingerprint": "",
            "last_error": "" if metadata["external_reference_exists"] else "Claude Code credentials were not found.",
        })

    cfg = update_provider_config(_update)
    return dict(cfg.get("providers", {}).get(CLAUDE_SUBSCRIPTION_PROVIDER_ID, {}))


def save_claude_subscription_runtime_probe(probe: dict[str, Any]) -> dict[str, Any]:
    from row_bot.providers.config import update_provider_config

    safe_probe = dict(probe or {})
    safe_probe["provider_id"] = CLAUDE_SUBSCRIPTION_PROVIDER_ID
    safe_probe["runtime"] = "native_oauth_messages"
    safe_probe["probed_at"] = str(safe_probe.get("probed_at") or _utcnow().isoformat())
    errors = [
        _redact_text(str(error), limit=220)
        for error in safe_probe.get("errors", [])
        if str(error or "").strip()
    ]
    safe_probe["errors"] = errors[:5]

    def _update(cfg: dict[str, Any]) -> None:
        entry = cfg.setdefault("providers", {}).setdefault(CLAUDE_SUBSCRIPTION_PROVIDER_ID, {})
        entry["last_runtime_probe"] = dict(safe_probe)
        entry["last_error"] = "" if safe_probe.get("ok") else "; ".join(errors[:2])

    cfg = update_provider_config(_update)
    return dict(cfg.get("providers", {}).get(CLAUDE_SUBSCRIPTION_PROVIDER_ID, {}).get("last_runtime_probe", {}))


def _probe_text_content(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    return str(content or "")


def _probe_calculate_tool() -> Any:
    from langchain_core.tools import StructuredTool

    def _calculate(expression: str) -> str:
        return "1 + 1 = 2" if str(expression or "").strip() else "2"

    return StructuredTool.from_function(
        func=_calculate,
        name="calculate",
        description="Evaluate a mathematical expression.",
    )


def run_claude_subscription_runtime_probe(
    model_name: str = "claude-sonnet-4-6",
    *,
    chat_model: Any | None = None,
) -> dict[str, Any]:
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    result: dict[str, Any] = {
        "provider_id": CLAUDE_SUBSCRIPTION_PROVIDER_ID,
        "runtime": "native_oauth_messages",
        "model": str(model_name or "claude-sonnet-4-6"),
        "ok": False,
        "chat_ok": False,
        "tool_calling": None,
        "tool_round_trip": None,
        "streaming_tool_calling": None,
        "errors": [],
        "probed_at": _utcnow().isoformat(),
    }
    try:
        model = chat_model
        if model is None:
            from row_bot.providers.transports.claude_subscription_messages import ChatClaudeSubscriptionMessages

            model = ChatClaudeSubscriptionMessages(model_name=result["model"], max_tokens=96)

        expected = "row-bot-claude-smoke-ok"
        text_response = model.invoke([HumanMessage(content=f"Reply with exactly this text and nothing else: {expected}")])
        text = _probe_text_content(text_response).strip().strip("`").strip()
        result["chat_ok"] = expected in text
        if not result["chat_ok"]:
            result["errors"].append(f"chat: unexpected response {text[:80] or '<empty>'}")

        tool_model = model.bind_tools([_probe_calculate_tool()], tool_choice="calculate")
        tool_prompt = "Use the calculate tool for the expression 1 + 1. Do not answer in text."
        tool_response = tool_model.invoke([HumanMessage(content=tool_prompt)])
        tool_calls = [
            dict(call)
            for call in (getattr(tool_response, "tool_calls", None) or [])
            if isinstance(call, dict)
        ]
        calculate_call = next((call for call in tool_calls if call.get("name") == "calculate"), None)
        result["tool_calling"] = calculate_call is not None
        if calculate_call is None:
            names = ", ".join(str(call.get("name") or "") for call in tool_calls if call.get("name"))
            result["errors"].append(f"tools: expected calculate tool call, got {names or 'none'}")
        else:
            call_id = str(calculate_call.get("id") or "call_row_bot_claude_probe")
            replay_response = model.invoke([
                HumanMessage(content=tool_prompt),
                AIMessage(content="", tool_calls=[{
                    "name": "calculate",
                    "args": dict(calculate_call.get("args") or {"expression": "1 + 1"}),
                    "id": call_id,
                    "type": "tool_call",
                }]),
                ToolMessage(content="1 + 1 = 2", name="calculate", tool_call_id=call_id),
            ])
            result["tool_round_trip"] = replay_response is not None
    except Exception as exc:
        result["errors"].append(_redact_text(str(exc), limit=220))
        if result["tool_calling"] is None:
            result["tool_calling"] = False
        if result["tool_round_trip"] is None:
            result["tool_round_trip"] = False

    result["ok"] = (
        result.get("chat_ok") is True
        and result.get("tool_calling") is True
        and result.get("tool_round_trip") is True
    )
    return save_claude_subscription_runtime_probe(result)


def _normalize_modalities(value: Any) -> set[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple, set, frozenset)):
        values = list(value)
    else:
        values = []
    normalized: set[str] = set()
    for item in values:
        text = str(item or "").strip().lower()
        if text in {"text", "image", "audio", "video"}:
            normalized.add(text)
    return normalized


def _claude_subscription_model_info(
    model_id: str,
    *,
    display_name: str | None = None,
    context_window: int = 0,
    input_modalities: set[str] | None = None,
    source_confidence: str = "documented_claude_subscription_model",
    source: str = "claude_subscription_fallback_catalog",
    tool_calling: bool | None = True,
    streaming: bool | None = True,
) -> ModelInfo:
    from row_bot.providers.reasoning import reasoning_metadata_for_catalog

    inputs = set(input_modalities or {ModelModality.TEXT.value})
    if not inputs:
        inputs = {ModelModality.TEXT.value}
    capabilities = {"text", "chat"}
    if streaming:
        capabilities.add("streaming")
    if tool_calling:
        capabilities.add("tool_calling")
    if ModelModality.IMAGE.value in inputs:
        capabilities.add("vision")
    return ModelInfo(
        provider_id=CLAUDE_SUBSCRIPTION_PROVIDER_ID,
        model_id=model_id,
        display_name=display_name or model_id,
        context_window=int(context_window or 0),
        transport=TransportMode.ANTHROPIC_MESSAGES,
        capabilities=frozenset(capabilities),
        input_modalities=frozenset(inputs),
        output_modalities=frozenset({ModelModality.TEXT.value}),
        tasks=frozenset({ModelTask.CHAT.value}),
        tool_calling=tool_calling,
        streaming=streaming,
        endpoint_compatibility=frozenset({TransportMode.ANTHROPIC_MESSAGES}),
        source_confidence=source_confidence,
        risk_label="subscription",
        source=source,
        reasoning=reasoning_metadata_for_catalog(CLAUDE_SUBSCRIPTION_PROVIDER_ID, model_id, {}),
    )


def _claude_subscription_model_info_with_capabilities(model_info: ModelInfo, capabilities: set[str]) -> ModelInfo:
    merged = set(model_info.capabilities) | {str(item) for item in capabilities if str(item)}
    return replace(model_info, capabilities=frozenset(merged))


def _model_info_from_live_item(item: dict[str, Any], *, verified_at: str) -> ModelInfo | None:
    if not isinstance(item, dict):
        return None
    model_id = str(item.get("id") or item.get("model") or "").strip()
    if not model_id:
        return None
    display_name = str(item.get("display_name") or item.get("displayName") or item.get("name") or model_id)
    context_window = item.get("max_input_tokens") or item.get("context_window") or item.get("contextWindow") or 0
    try:
        context_window = int(context_window or 0)
    except (TypeError, ValueError):
        context_window = 0
    input_modalities = _normalize_modalities(
        item.get("input_modalities")
        or item.get("inputModalities")
        or item.get("modalities")
    ) or {ModelModality.TEXT.value}
    capabilities = item.get("capabilities") if isinstance(item.get("capabilities"), dict) else {}
    if isinstance(capabilities, dict):
        for key in ("image_input", "vision", "image"):
            value = capabilities.get(key)
            if value is True or (isinstance(value, dict) and value.get("supported")):
                input_modalities.add(ModelModality.IMAGE.value)
    tool_calling = item.get("tool_calling") if isinstance(item.get("tool_calling"), bool) else True
    streaming = item.get("streaming") if isinstance(item.get("streaming"), bool) else True
    info = _claude_subscription_model_info(
        model_id,
        display_name=display_name,
        context_window=context_window,
        input_modalities=input_modalities,
        source_confidence="live_claude_subscription_catalog",
        source="claude_subscription_live_catalog",
        tool_calling=tool_calling,
        streaming=streaming,
    )
    extra_capabilities: set[str] = set()
    if item.get("thinking") or item.get("extended_thinking"):
        extra_capabilities.add("thinking")
    if verified_at:
        info = replace(info, last_verified_at=verified_at)
    return _claude_subscription_model_info_with_capabilities(info, extra_capabilities) if extra_capabilities else info


def _model_cache_row(model_info: ModelInfo) -> dict[str, Any]:
    return {
        "id": model_info.model_id,
        "display_name": model_info.display_name,
        "context_window": model_info.context_window,
        "input_modalities": sorted(model_info.input_modalities),
        "capabilities": sorted(model_info.capabilities),
        "tool_calling": model_info.tool_calling,
        "streaming": model_info.streaming,
        "source_confidence": model_info.source_confidence,
        "source": model_info.source,
        "last_verified_at": model_info.last_verified_at,
        "reasoning": dict(model_info.reasoning) if model_info.reasoning else None,
    }


def _model_info_from_cache_row(row: dict[str, Any]) -> ModelInfo | None:
    model_id = str(row.get("id") or row.get("model_id") or "").strip()
    if not model_id:
        return None
    info = _claude_subscription_model_info(
        model_id,
        display_name=str(row.get("display_name") or model_id),
        context_window=int(row.get("context_window") or 0),
        input_modalities=_normalize_modalities(row.get("input_modalities")) or {ModelModality.TEXT.value},
        source_confidence=str(row.get("source_confidence") or "cached_claude_subscription_catalog"),
        source=str(row.get("source") or "claude_subscription_cached_catalog"),
        tool_calling=row.get("tool_calling") if isinstance(row.get("tool_calling"), bool) else True,
        streaming=row.get("streaming") if isinstance(row.get("streaming"), bool) else True,
    )
    if row.get("last_verified_at"):
        info = replace(info, last_verified_at=str(row.get("last_verified_at")))
    if isinstance(row.get("reasoning"), dict):
        info = replace(info, reasoning=dict(row["reasoning"]))
    raw_capabilities = row.get("capabilities")
    capabilities: set[str] = set()
    if isinstance(raw_capabilities, str):
        capabilities = {raw_capabilities}
    elif isinstance(raw_capabilities, (list, tuple, set, frozenset)):
        capabilities = {str(item) for item in raw_capabilities if str(item)}
    return _claude_subscription_model_info_with_capabilities(info, capabilities) if capabilities else info


def fetch_claude_subscription_model_infos(
    *,
    access_token: str | None = None,
    http_client: Any | None = None,
) -> list[ModelInfo]:
    token = str(access_token or "").strip()
    if not token:
        credentials = claude_subscription_runtime_credentials(refresh_if_needed=True)
        token = credentials.access_token
    if not token:
        return []
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": claude_subscription_oauth_betas(),
        "User-Agent": claude_subscription_oauth_user_agent(),
        "x-app": "cli",
    }
    client = http_client or _new_http_client(timeout=10.0)
    owns_client = http_client is None
    try:
        response = client.get(CLAUDE_SUBSCRIPTION_MODELS_URL, headers=headers, timeout=10.0)
    finally:
        if owns_client:
            client.close()
    status_code = int(getattr(response, "status_code", 0) or 0)
    if status_code < 200 or status_code >= 300:
        return []
    try:
        payload = response.json()
    except Exception:
        return []
    if not isinstance(payload, dict):
        return []
    entries = payload.get("data") or payload.get("models") or []
    if not isinstance(entries, list):
        return []
    verified_at = _utcnow().replace(microsecond=0).isoformat().replace("+00:00", "Z")
    infos: list[ModelInfo] = []
    seen: set[str] = set()
    for item in entries:
        if not isinstance(item, dict):
            continue
        info = _model_info_from_live_item(item, verified_at=verified_at)
        if not info or info.model_id in seen:
            continue
        seen.add(info.model_id)
        infos.append(info)
    return infos


def _save_catalog_cache(infos: list[ModelInfo]) -> None:
    if not infos:
        return
    from row_bot.providers.config import update_provider_config

    def _update(cfg: dict[str, Any]) -> None:
        entry = cfg.setdefault("providers", {}).setdefault(CLAUDE_SUBSCRIPTION_PROVIDER_ID, {})
        entry["catalog_cache"] = {
            "fetched_at": _utcnow().isoformat(),
            "source": "live_claude_subscription_catalog",
            "models": [_model_cache_row(info) for info in infos],
        }

    update_provider_config(_update)


def _load_catalog_cache() -> list[ModelInfo]:
    from row_bot.providers.config import load_provider_config

    cache = load_provider_config().get("providers", {}).get(CLAUDE_SUBSCRIPTION_PROVIDER_ID, {}).get("catalog_cache")
    if not isinstance(cache, dict):
        return []
    rows = cache.get("models")
    if not isinstance(rows, list):
        return []
    infos: list[ModelInfo] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        info = _model_info_from_cache_row(row)
        if not info or info.model_id in seen:
            continue
        seen.add(info.model_id)
        infos.append(info)
    return infos


def fallback_claude_subscription_model_infos() -> list[ModelInfo]:
    return [
        _claude_subscription_model_info(
            str(model["id"]),
            display_name=str(model["display_name"]),
            context_window=int(model.get("context_window") or 0),
            input_modalities=_normalize_modalities(model.get("input_modalities")) or {ModelModality.TEXT.value},
            source_confidence=str(model.get("source_confidence") or "documented_claude_subscription_model"),
            source="claude_subscription_documented_fallback_catalog",
            tool_calling=True,
            streaming=True,
        )
        for model in FALLBACK_CLAUDE_SUBSCRIPTION_MODELS
    ]


def list_claude_subscription_model_infos(
    *,
    force_refresh: bool = False,
    http_client: Any | None = None,
) -> list[ModelInfo]:
    if force_refresh or (http_client is not None):
        try:
            live_infos = fetch_claude_subscription_model_infos(http_client=http_client)
        except Exception:
            live_infos = []
        if live_infos:
            _save_catalog_cache(live_infos)
            return live_infos

    cached_infos = _load_catalog_cache()
    if cached_infos:
        return cached_infos
    return fallback_claude_subscription_model_infos()


def list_claude_subscription_model_infos_for_status() -> list[ModelInfo]:
    """Return Claude subscription catalog metadata for status cards without live discovery."""
    cached_infos = _load_catalog_cache()
    if cached_infos:
        return cached_infos
    return fallback_claude_subscription_model_infos()


def seed_recommended_claude_subscription_quick_choices(*, max_choices: int = 1) -> list[dict[str, Any]]:
    from row_bot.providers.config import load_provider_config
    from row_bot.providers.runtime import provider_status
    from row_bot.providers.selection import add_quick_choice_for_model

    status = provider_status(CLAUDE_SUBSCRIPTION_PROVIDER_ID)
    if not status.get("configured") or not status.get("runtime_enabled"):
        return load_provider_config().get("quick_choices", [])
    recommended_ids = {
        str(model["id"])
        for model in FALLBACK_CLAUDE_SUBSCRIPTION_MODELS
        if model.get("recommended")
    }
    infos = list_claude_subscription_model_infos()
    if not infos:
        infos = list_claude_subscription_model_infos(force_refresh=True)
    candidates = [info for info in infos if info.model_id in recommended_ids] or infos
    for model_info in candidates[:max(0, max_choices)]:
        add_quick_choice_for_model(
            model_info.model_id,
            provider_id=CLAUDE_SUBSCRIPTION_PROVIDER_ID,
            display_name=model_info.display_name,
            source="claude_subscription_recommended",
            capabilities_snapshot=model_info.capability_snapshot(),
        )
    return load_provider_config().get("quick_choices", [])
