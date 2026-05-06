from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any

import secret_store

from providers.models import AuthMethod, ModelInfo, ModelModality, ModelTask, ProviderHealth, TransportMode
from providers.oauth import DeviceCodePrompt, OAuthToken, expiry_from_seconds

CODEX_PROVIDER_ID = "codex"
CODEX_HOME_ENV = "CODEX_HOME"
DEFAULT_CODEX_HOME = pathlib.Path("~/.codex")
AUTH_FILENAME = "auth.json"
CODEX_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_OAUTH_ISSUER = "https://auth.openai.com"
CODEX_OAUTH_TOKEN_URL = f"{CODEX_OAUTH_ISSUER}/oauth/token"
CODEX_MODELS_URL = "https://chatgpt.com/backend-api/codex/models?client_version=1.0.0"
CODEX_OAUTH_TIMEOUT_SECONDS = 15 * 60
SENSITIVE_KEY_PARTS = (
    "access",
    "api_key",
    "authorization",
    "credential",
    "id_token",
    "password",
    "refresh",
    "secret",
    "session",
    "token",
)


@dataclass(frozen=True)
class CodexDeviceFlow:
    verification_uri: str
    user_code: str
    device_auth_id: str
    expires_at: str
    interval_seconds: int = 5
    issuer: str = CODEX_OAUTH_ISSUER
    client_id: str = CODEX_OAUTH_CLIENT_ID

    @property
    def prompt(self) -> DeviceCodePrompt:
        return DeviceCodePrompt(
            verification_uri=self.verification_uri,
            user_code=self.user_code,
            expires_at=self.expires_at,
            interval_seconds=self.interval_seconds,
        )


@dataclass(frozen=True)
class CodexDeviceAuthorization:
    authorization_code: str
    code_verifier: str
    code_challenge: str = ""
    issuer: str = CODEX_OAUTH_ISSUER
    client_id: str = CODEX_OAUTH_CLIENT_ID


@dataclass(frozen=True)
class CodexTokenSet:
    access_token: str
    refresh_token: str = ""
    id_token: str = ""
    expires_at: str = ""
    account_id: str = ""
    plan_type: str = ""

    def oauth_token(self) -> OAuthToken:
        return OAuthToken(
            access_token=self.access_token,
            refresh_token=self.refresh_token,
            expires_at=self.expires_at,
        )

FALLBACK_CODEX_MODELS = [
    {
        "id": "gpt-5.5",
        "display_name": "GPT-5.5 (ChatGPT)",
        "source_confidence": "documented_chatgpt_codex_model",
        "recommended": True,
    },
    {
        "id": "gpt-5.4",
        "display_name": "GPT-5.4 (ChatGPT)",
        "source_confidence": "documented_chatgpt_codex_model",
        "recommended": False,
    },
    {
        "id": "gpt-5.4-mini",
        "display_name": "GPT-5.4 Mini (ChatGPT)",
        "source_confidence": "documented_chatgpt_codex_model",
        "recommended": False,
    },
    {
        "id": "gpt-5.3-codex",
        "display_name": "GPT-5.3 Codex (ChatGPT)",
        "source_confidence": "documented_chatgpt_codex_model",
        "recommended": False,
    },
    {
        "id": "gpt-5.3-codex-spark",
        "display_name": "GPT-5.3 Codex Spark (ChatGPT Pro)",
        "source_confidence": "documented_chatgpt_pro_preview_model",
        "recommended": False,
    },
    {
        "id": "gpt-5.2",
        "display_name": "GPT-5.2 (ChatGPT)",
        "source_confidence": "documented_chatgpt_codex_alternative",
        "recommended": False,
    },
]
CURATED_CODEX_MODELS = FALLBACK_CODEX_MODELS


def codex_home() -> pathlib.Path:
    return pathlib.Path(os.environ.get(CODEX_HOME_ENV) or DEFAULT_CODEX_HOME).expanduser()


def codex_auth_path(home: pathlib.Path | str | None = None) -> pathlib.Path:
    root = pathlib.Path(home).expanduser() if home is not None else codex_home()
    return root / AUTH_FILENAME


def path_hash(path: pathlib.Path | str) -> str:
    expanded = pathlib.Path(path).expanduser()
    return hashlib.sha256(str(expanded).encode("utf-8")).hexdigest()[:12]


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


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _safe_error_body(response: Any) -> str:
    try:
        text = str(getattr(response, "text", "") or "")
    except Exception:
        text = ""
    return text[:300]


def _json_response(response: Any) -> dict[str, Any]:
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Codex auth response was not a JSON object.")
    return payload


def _new_http_client(timeout: float = 30.0) -> Any:
    import httpx

    return httpx.Client(timeout=timeout)


def _is_pytest_running() -> bool:
    return "PYTEST_CURRENT_TEST" in os.environ


def _clean_model_id(value: Any) -> str:
    return str(value or "").strip()


def _is_hidden_codex_model(item: dict[str, Any]) -> bool:
    if item.get("hidden") is True or item.get("is_hidden") is True:
        return True
    if item.get("supported_in_api") is False:
        return True
    visibility = str(item.get("visibility") or item.get("status") or "").strip().lower()
    return visibility in {"hide", "hidden", "internal", "disabled", "unavailable"}


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


def _codex_model_info(
    model_id: str,
    *,
    display_name: str | None = None,
    context_window: int = 0,
    input_modalities: set[str] | None = None,
    source_confidence: str = "documented_chatgpt_codex_model",
    source: str = "codex_fallback_catalog",
    tool_calling: bool | None = None,
    streaming: bool | None = True,
) -> ModelInfo:
    inputs = set(input_modalities or {ModelModality.TEXT.value})
    if not inputs:
        inputs = {ModelModality.TEXT.value}
    capabilities = {"text", "chat"}
    if streaming:
        capabilities.add("streaming")
    if ModelModality.IMAGE.value in inputs:
        capabilities.add("vision")
    if tool_calling:
        capabilities.add("tool_calling")
    return ModelInfo(
        provider_id=CODEX_PROVIDER_ID,
        model_id=model_id,
        display_name=display_name or model_id,
        context_window=int(context_window or 0),
        transport=TransportMode.OPENAI_RESPONSES,
        capabilities=frozenset(capabilities),
        input_modalities=frozenset(inputs),
        output_modalities=frozenset({ModelModality.TEXT.value}),
        tasks=frozenset({ModelTask.RESPONSES.value}),
        tool_calling=tool_calling,
        streaming=streaming,
        endpoint_compatibility=frozenset({TransportMode.OPENAI_RESPONSES}),
        source_confidence=source_confidence,
        risk_label="subscription",
        source=source,
    )


def _codex_model_info_with_capabilities(model_info: ModelInfo, capabilities: set[str]) -> ModelInfo:
    merged = set(model_info.capabilities) | {str(item) for item in capabilities if str(item)}
    return replace(model_info, capabilities=frozenset(merged))


def _codex_model_info_from_live_item(item: dict[str, Any]) -> ModelInfo | None:
    if not isinstance(item, dict) or _is_hidden_codex_model(item):
        return None
    model_id = _clean_model_id(item.get("slug") or item.get("id") or item.get("model"))
    if not model_id:
        return None
    display_name = _clean_model_id(
        item.get("display_name")
        or item.get("displayName")
        or item.get("name")
        or model_id
    )
    context_window = item.get("context_window") or item.get("contextWindow") or item.get("contextTokens") or 0
    try:
        context_window = int(context_window or 0)
    except (TypeError, ValueError):
        context_window = 0
    input_modalities = _normalize_modalities(
        item.get("input_modalities")
        or item.get("inputModalities")
        or item.get("modalities")
    ) or {ModelModality.TEXT.value}
    reasoning_efforts = item.get("supported_reasoning_efforts") or item.get("supportedReasoningEfforts") or []
    tool_calling = item.get("tool_calling") or item.get("supports_tools") or item.get("supportsTools")
    if tool_calling is not None:
        tool_calling = bool(tool_calling)
    info = _codex_model_info(
        model_id,
        display_name=display_name,
        context_window=context_window,
        input_modalities=input_modalities,
        source_confidence="live_chatgpt_codex_catalog",
        source="codex_live_catalog",
        tool_calling=tool_calling,
        streaming=item.get("streaming") if isinstance(item.get("streaming"), bool) else True,
    )
    if isinstance(reasoning_efforts, (list, tuple, set)) and reasoning_efforts:
        return _codex_model_info_with_capabilities(info, {"reasoning"})
    return info


def _codex_model_cache_row(model_info: ModelInfo) -> dict[str, Any]:
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
    }


def _codex_model_info_from_cache_row(row: dict[str, Any]) -> ModelInfo | None:
    model_id = _clean_model_id(row.get("id") or row.get("model_id"))
    if not model_id:
        return None
    info = _codex_model_info(
        model_id,
        display_name=_clean_model_id(row.get("display_name") or model_id),
        context_window=int(row.get("context_window") or 0),
        input_modalities=_normalize_modalities(row.get("input_modalities")) or {ModelModality.TEXT.value},
        source_confidence=str(row.get("source_confidence") or "cached_chatgpt_codex_catalog"),
        source=str(row.get("source") or "codex_cached_catalog"),
        tool_calling=row.get("tool_calling") if isinstance(row.get("tool_calling"), bool) else None,
        streaming=row.get("streaming") if isinstance(row.get("streaming"), bool) else True,
    )
    capabilities = _normalize_modalities([])
    raw_capabilities = row.get("capabilities")
    if isinstance(raw_capabilities, str):
        capabilities = {raw_capabilities}
    elif isinstance(raw_capabilities, (list, tuple, set, frozenset)):
        capabilities = {str(item) for item in raw_capabilities if str(item)}
    return _codex_model_info_with_capabilities(info, capabilities) if capabilities else info


def _decode_jwt_claims(token: str) -> dict[str, Any]:
    if not isinstance(token, str) or not token.strip():
        return {}
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    try:
        import base64

        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        return claims if isinstance(claims, dict) else {}
    except Exception:
        return {}


def codex_token_metadata(access_token: str = "", id_token: str = "") -> dict[str, Any]:
    claims = _decode_jwt_claims(access_token) or _decode_jwt_claims(id_token)
    auth_claims = claims.get("https://api.openai.com/auth")
    if not isinstance(auth_claims, dict):
        auth_claims = {}
    account_id = auth_claims.get("chatgpt_account_id")
    plan_type = auth_claims.get("chatgpt_plan_type")
    exp = claims.get("exp")
    expires_at = ""
    if isinstance(exp, (int, float)):
        try:
            expires_at = datetime.fromtimestamp(float(exp), timezone.utc).isoformat()
        except Exception:
            expires_at = ""
    return {
        "account_id": account_id if isinstance(account_id, str) else "",
        "account_id_hash": secret_store.fingerprint(account_id) if isinstance(account_id, str) and account_id else "",
        "plan_type": plan_type if isinstance(plan_type, str) else "",
        "expires_at": expires_at,
    }


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


def codex_runtime_credentials(*, refresh_if_needed: bool = True, http_client: Any | None = None) -> CodexTokenSet:
    """Return Thoth-owned Codex OAuth credentials for direct runtime use.

    This intentionally ignores metadata-only external CLI references. Runtime
    callers need actual bearer/account values, which Thoth only treats as
    available when they are stored in its provider keyring namespace.
    """
    from providers.auth_store import get_provider_secret
    from providers.config import load_provider_config

    access_token = get_provider_secret(CODEX_PROVIDER_ID, "access_token")
    refresh_token = get_provider_secret(CODEX_PROVIDER_ID, "refresh_token")
    id_token = get_provider_secret(CODEX_PROVIDER_ID, "id_token")
    account_id = get_provider_secret(CODEX_PROVIDER_ID, "account")
    provider_cfg = load_provider_config().get("providers", {}).get(CODEX_PROVIDER_ID, {})
    if provider_cfg.get("source") != AuthMethod.OAUTH_DEVICE.value or provider_cfg.get("auth_method") != AuthMethod.OAUTH_DEVICE.value:
        return CodexTokenSet(access_token="")
    expires_at = str(provider_cfg.get("expires_at") or "")
    plan_type = str(provider_cfg.get("plan_type") or "")

    metadata = codex_token_metadata(access_token, id_token)
    if not account_id:
        account_id = str(metadata.get("account_id") or "")
    if not expires_at:
        expires_at = str(metadata.get("expires_at") or "")
    if not plan_type:
        plan_type = str(metadata.get("plan_type") or "")

    if refresh_if_needed and access_token and refresh_token and _expires_soon(expires_at):
        refreshed = refresh_codex_token(refresh_token, http_client=http_client)
        saved = save_codex_oauth_tokens(refreshed)
        access_token = refreshed.access_token
        refresh_token = refreshed.refresh_token or refresh_token
        id_token = refreshed.id_token or id_token
        account_id = refreshed.account_id or account_id
        expires_at = refreshed.expires_at or str(saved.get("expires_at") or expires_at)
        plan_type = refreshed.plan_type or str(saved.get("plan_type") or plan_type)

    return CodexTokenSet(
        access_token=access_token,
        refresh_token=refresh_token,
        id_token=id_token,
        expires_at=expires_at,
        account_id=account_id,
        plan_type=plan_type,
    )


def codex_runtime_available() -> bool:
    try:
        credentials = codex_runtime_credentials(refresh_if_needed=False)
    except Exception:
        return False
    return bool(credentials.access_token and credentials.account_id)


def fetch_codex_model_infos(
    *,
    access_token: str | None = None,
    account_id: str | None = None,
    http_client: Any | None = None,
) -> list[ModelInfo]:
    token = str(access_token or "").strip()
    if not token:
        credentials = codex_runtime_credentials(refresh_if_needed=True)
        token = credentials.access_token
        account_id = account_id or credentials.account_id
    if not token:
        return []
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    if account_id:
        headers["ChatGPT-Account-ID"] = str(account_id)
    client = http_client or _new_http_client(timeout=10.0)
    owns_client = http_client is None
    try:
        response = client.get(CODEX_MODELS_URL, headers=headers, timeout=10.0)
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
    entries = payload.get("models") or payload.get("data") or []
    if not isinstance(entries, list):
        return []
    infos: list[ModelInfo] = []
    seen: set[str] = set()
    for item in entries:
        if not isinstance(item, dict):
            continue
        info = _codex_model_info_from_live_item(item)
        if not info or info.model_id in seen:
            continue
        seen.add(info.model_id)
        infos.append(info)
    return infos


def _save_codex_catalog_cache(infos: list[ModelInfo]) -> None:
    if not infos:
        return
    from providers.config import update_provider_config

    def _update(cfg: dict[str, Any]) -> None:
        entry = cfg.setdefault("providers", {}).setdefault(CODEX_PROVIDER_ID, {})
        entry["catalog_cache"] = {
            "fetched_at": _utcnow().isoformat(),
            "source": "live_chatgpt_codex_catalog",
            "models": [_codex_model_cache_row(info) for info in infos],
        }

    update_provider_config(_update)


def _load_codex_catalog_cache() -> list[ModelInfo]:
    from providers.config import load_provider_config

    cache = load_provider_config().get("providers", {}).get(CODEX_PROVIDER_ID, {}).get("catalog_cache")
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
        info = _codex_model_info_from_cache_row(row)
        if not info or info.model_id in seen:
            continue
        seen.add(info.model_id)
        infos.append(info)
    return infos


def fallback_codex_model_infos() -> list[ModelInfo]:
    return [
        _codex_model_info(
            str(model["id"]),
            display_name=str(model["display_name"]),
            input_modalities=set(model.get("input_modalities") or ({ModelModality.TEXT.value, ModelModality.IMAGE.value} if str(model["id"]).startswith("gpt-5") else {ModelModality.TEXT.value})),
            source_confidence=str(model.get("source_confidence") or "documented_chatgpt_codex_model"),
            source="codex_documented_fallback_catalog",
            tool_calling=None,
            streaming=True,
        )
        for model in FALLBACK_CODEX_MODELS
    ]


def summarize_auth_json(path: pathlib.Path | str | None = None) -> dict[str, Any]:
    """Return display-safe metadata about a Codex auth cache.

    The Codex docs state that file-based credentials live in ``auth.json`` and
    must be treated like a password. This function never returns values from the
    file; it only returns top-level key names and broad JSON types.
    """
    target = pathlib.Path(path).expanduser() if path is not None else codex_auth_path()
    summary: dict[str, Any] = {
        "label": str(path or pathlib.Path("~/.codex") / AUTH_FILENAME),
        "path_hash": path_hash(target),
        "exists": target.exists(),
        "key_names": [],
        "key_types": {},
        "sensitive_key_names": [],
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
    return summary


def codex_cli_info(binary: str = "codex") -> dict[str, Any]:
    source = shutil.which(binary) or ""
    return {
        "installed": bool(source),
        "source": source,
    }


def discover_codex_credentials(
    *,
    auth_path: pathlib.Path | str | None = None,
    binary: str = "codex",
) -> dict[str, Any]:
    auth_summary = summarize_auth_json(auth_path)
    cli = codex_cli_info(binary)
    return {
        "provider_id": CODEX_PROVIDER_ID,
        "source": "codex_cli",
        "label": auth_summary["label"],
        "path_hash": auth_summary["path_hash"],
        "exists": auth_summary["exists"],
        "cli_installed": cli["installed"],
        "cli_source": cli["source"],
        "auth_key_names": auth_summary["key_names"],
        "auth_key_types": auth_summary["key_types"],
        "auth_sensitive_key_names": auth_summary["sensitive_key_names"],
        "auth_error": auth_summary["error"],
    }


def start_codex_device_flow(
    *,
    http_client: Any | None = None,
    issuer: str = CODEX_OAUTH_ISSUER,
    client_id: str = CODEX_OAUTH_CLIENT_ID,
) -> CodexDeviceFlow:
    """Start OpenAI's Codex device-code flow without persisting secrets."""
    normalized_issuer = str(issuer or CODEX_OAUTH_ISSUER).rstrip("/")
    client = http_client or _new_http_client()
    owns_client = http_client is None
    try:
        response = client.post(
            f"{normalized_issuer}/api/accounts/deviceauth/usercode",
            json={"client_id": client_id},
            headers={"Content-Type": "application/json"},
        )
    finally:
        if owns_client:
            client.close()

    status_code = int(getattr(response, "status_code", 0) or 0)
    if status_code < 200 or status_code >= 300:
        raise RuntimeError(f"Codex device-code request failed with HTTP {status_code}: {_safe_error_body(response)}")
    payload = _json_response(response)
    user_code = str(payload.get("user_code") or payload.get("usercode") or "").strip()
    device_auth_id = str(payload.get("device_auth_id") or "").strip()
    if not user_code or not device_auth_id:
        raise RuntimeError("Codex device-code response was missing user_code or device_auth_id.")
    try:
        interval_seconds = int(str(payload.get("interval") or "5").strip())
    except ValueError:
        interval_seconds = 5
    return CodexDeviceFlow(
        verification_uri=f"{normalized_issuer}/codex/device",
        user_code=user_code,
        device_auth_id=device_auth_id,
        interval_seconds=max(0, interval_seconds),
        expires_at=expiry_from_seconds(CODEX_OAUTH_TIMEOUT_SECONDS),
        issuer=normalized_issuer,
        client_id=client_id,
    )


def poll_codex_device_authorization(
    flow: CodexDeviceFlow,
    *,
    http_client: Any | None = None,
) -> CodexDeviceAuthorization | None:
    """Poll once for a Codex authorization code. Returns None while pending."""
    client = http_client or _new_http_client()
    owns_client = http_client is None
    try:
        response = client.post(
            f"{flow.issuer.rstrip('/')}/api/accounts/deviceauth/token",
            json={"device_auth_id": flow.device_auth_id, "user_code": flow.user_code},
            headers={"Content-Type": "application/json"},
        )
    finally:
        if owns_client:
            client.close()

    status_code = int(getattr(response, "status_code", 0) or 0)
    if status_code in {403, 404}:
        return None
    if status_code < 200 or status_code >= 300:
        raise RuntimeError(f"Codex device authorization failed with HTTP {status_code}: {_safe_error_body(response)}")
    payload = _json_response(response)
    authorization_code = str(payload.get("authorization_code") or "").strip()
    code_verifier = str(payload.get("code_verifier") or "").strip()
    code_challenge = str(payload.get("code_challenge") or "").strip()
    if not authorization_code or not code_verifier:
        raise RuntimeError("Codex device authorization response was missing authorization_code or code_verifier.")
    return CodexDeviceAuthorization(
        authorization_code=authorization_code,
        code_verifier=code_verifier,
        code_challenge=code_challenge,
        issuer=flow.issuer,
        client_id=flow.client_id,
    )


def wait_for_codex_device_authorization(
    flow: CodexDeviceFlow,
    *,
    http_client: Any | None = None,
    timeout_seconds: int = CODEX_OAUTH_TIMEOUT_SECONDS,
    sleep: Any = time.sleep,
) -> CodexDeviceAuthorization:
    deadline = time.monotonic() + max(0, timeout_seconds)
    while True:
        authorization = poll_codex_device_authorization(flow, http_client=http_client)
        if authorization is not None:
            return authorization
        if time.monotonic() >= deadline:
            raise TimeoutError("Codex device authorization timed out.")
        sleep_for = min(max(0, flow.interval_seconds), max(0, deadline - time.monotonic()))
        sleep(sleep_for)


def _token_set_from_payload(payload: dict[str, Any]) -> CodexTokenSet:
    access_token = str(payload.get("access_token") or "").strip()
    if not access_token:
        raise RuntimeError("Codex token response did not include an access_token.")
    refresh_token = str(payload.get("refresh_token") or "").strip()
    id_token = str(payload.get("id_token") or "").strip()
    metadata = codex_token_metadata(access_token, id_token)
    expires_at = metadata.get("expires_at") or ""
    expires_in = payload.get("expires_in")
    if not expires_at and isinstance(expires_in, (int, float)):
        expires_at = (_utcnow() + timedelta(seconds=max(0, float(expires_in)))).isoformat()
    return CodexTokenSet(
        access_token=access_token,
        refresh_token=refresh_token,
        id_token=id_token,
        expires_at=expires_at,
        account_id=str(metadata.get("account_id") or ""),
        plan_type=str(metadata.get("plan_type") or ""),
    )


def exchange_codex_device_authorization(
    authorization: CodexDeviceAuthorization,
    *,
    http_client: Any | None = None,
) -> CodexTokenSet:
    client = http_client or _new_http_client()
    owns_client = http_client is None
    try:
        response = client.post(
            f"{authorization.issuer.rstrip('/')}/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": authorization.authorization_code,
                "redirect_uri": f"{authorization.issuer.rstrip('/')}/deviceauth/callback",
                "client_id": authorization.client_id,
                "code_verifier": authorization.code_verifier,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    finally:
        if owns_client:
            client.close()

    status_code = int(getattr(response, "status_code", 0) or 0)
    if status_code < 200 or status_code >= 300:
        raise RuntimeError(f"Codex token exchange failed with HTTP {status_code}: {_safe_error_body(response)}")
    return _token_set_from_payload(_json_response(response))


def refresh_codex_token(refresh_token: str, *, http_client: Any | None = None) -> CodexTokenSet:
    if not str(refresh_token or "").strip():
        raise RuntimeError("Codex refresh token is missing.")
    client = http_client or _new_http_client()
    owns_client = http_client is None
    try:
        response = client.post(
            CODEX_OAUTH_TOKEN_URL,
            json={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": CODEX_OAUTH_CLIENT_ID,
            },
            headers={"Content-Type": "application/json"},
        )
    finally:
        if owns_client:
            client.close()

    status_code = int(getattr(response, "status_code", 0) or 0)
    if status_code < 200 or status_code >= 300:
        raise RuntimeError(f"Codex token refresh failed with HTTP {status_code}: {_safe_error_body(response)}")
    payload = _json_response(response)
    if not payload.get("refresh_token"):
        payload["refresh_token"] = refresh_token
    return _token_set_from_payload(payload)


def save_codex_oauth_tokens(token_set: CodexTokenSet) -> dict[str, Any]:
    """Persist Thoth-owned Codex OAuth tokens in keyring and metadata in providers.json."""
    from providers.auth_store import set_provider_secret
    from providers.config import update_provider_config

    set_provider_secret(
        CODEX_PROVIDER_ID,
        "access_token",
        token_set.access_token,
        source=AuthMethod.OAUTH_DEVICE.value,
        auth_method=AuthMethod.OAUTH_DEVICE,
    )
    if token_set.refresh_token:
        set_provider_secret(
            CODEX_PROVIDER_ID,
            "refresh_token",
            token_set.refresh_token,
            source=AuthMethod.OAUTH_DEVICE.value,
            auth_method=AuthMethod.OAUTH_DEVICE,
        )
    if token_set.id_token:
        set_provider_secret(
            CODEX_PROVIDER_ID,
            "id_token",
            token_set.id_token,
            source=AuthMethod.OAUTH_DEVICE.value,
            auth_method=AuthMethod.OAUTH_DEVICE,
        )
    if token_set.account_id:
        set_provider_secret(
            CODEX_PROVIDER_ID,
            "account",
            token_set.account_id,
            source=AuthMethod.OAUTH_DEVICE.value,
            auth_method=AuthMethod.OAUTH_DEVICE,
        )

    token_metadata = codex_token_metadata(token_set.access_token, token_set.id_token)
    fingerprint = secret_store.fingerprint(token_set.access_token)
    now = _utcnow().isoformat()

    def _update(cfg: dict[str, Any]) -> None:
        entry = cfg.setdefault("providers", {}).setdefault(CODEX_PROVIDER_ID, {})
        entry.update({
            "provider_id": CODEX_PROVIDER_ID,
            "auth_method": AuthMethod.OAUTH_DEVICE.value,
            "configured": True,
            "health": ProviderHealth.CONNECTED.value,
            "source": AuthMethod.OAUTH_DEVICE.value,
            "fingerprint": fingerprint,
            "expires_at": token_set.expires_at or token_metadata.get("expires_at") or "",
            "account_id_hash": token_metadata.get("account_id_hash") or "",
            "plan_type": token_set.plan_type or token_metadata.get("plan_type") or "",
            "updated_at": now,
            "last_error": "",
            "external_reference_exists": False,
        })

    cfg = update_provider_config(_update)
    return dict(cfg.get("providers", {}).get(CODEX_PROVIDER_ID, {}))


def external_reference_metadata(path: pathlib.Path | str | None = None) -> dict[str, Any]:
    discovered = discover_codex_credentials(auth_path=path)
    return {
        "provider_id": CODEX_PROVIDER_ID,
        "source": "external_cli",
        "external_reference_label": discovered["label"],
        "external_reference_path_hash": discovered["path_hash"],
        "external_reference_exists": bool(discovered["exists"]),
        "auth_key_names": list(discovered.get("auth_key_names") or []),
        "auth_sensitive_key_names": list(discovered.get("auth_sensitive_key_names") or []),
    }


def save_external_reference(path: pathlib.Path | str | None = None) -> dict[str, Any]:
    """Persist an explicit reference to an existing Codex auth cache.

    This records only metadata. It never copies token values from the external
    file and never edits Codex CLI-managed files.
    """
    from providers.config import update_provider_config

    metadata = external_reference_metadata(path)

    def _update(cfg: dict[str, Any]) -> None:
        entry = cfg.setdefault("providers", {}).setdefault(CODEX_PROVIDER_ID, {})
        entry.update(metadata)
        entry.update({
            "provider_id": CODEX_PROVIDER_ID,
            "auth_method": AuthMethod.EXTERNAL_CLI.value,
            "configured": bool(metadata["external_reference_exists"]),
            "health": ProviderHealth.CONNECTED.value if metadata["external_reference_exists"] else ProviderHealth.MISSING_AUTH.value,
            "source": "external_cli",
            "fingerprint": "",
            "last_error": "" if metadata["external_reference_exists"] else "Codex auth cache was not found.",
        })

    cfg = update_provider_config(_update)
    return dict(cfg.get("providers", {}).get(CODEX_PROVIDER_ID, {}))


def disconnect_codex_metadata(*, remove_thoth_tokens: bool = True) -> None:
    """Remove Thoth-owned Codex metadata and optional Thoth-owned token secrets."""
    from providers.auth_store import delete_provider_secret
    from providers.config import update_provider_config

    if remove_thoth_tokens:
        for credential_name in ("access_token", "refresh_token", "id_token", "account"):
            delete_provider_secret(CODEX_PROVIDER_ID, credential_name)

    def _update(cfg: dict[str, Any]) -> None:
        cfg.setdefault("providers", {}).pop(CODEX_PROVIDER_ID, None)

    update_provider_config(_update)


def list_codex_model_infos(*, force_refresh: bool = False, http_client: Any | None = None) -> list[ModelInfo]:
    if force_refresh or (http_client is not None) or not _is_pytest_running():
        try:
            live_infos = fetch_codex_model_infos(http_client=http_client)
        except Exception:
            live_infos = []
        if live_infos:
            _save_codex_catalog_cache(live_infos)
            return live_infos

    cached_infos = _load_codex_catalog_cache()
    if cached_infos:
        return cached_infos
    return fallback_codex_model_infos()


def seed_recommended_codex_quick_choices(*, max_choices: int = 1) -> list[dict[str, Any]]:
    from providers.runtime import provider_status
    from providers.selection import add_quick_choice_for_model
    from providers.config import load_provider_config

    status = provider_status(CODEX_PROVIDER_ID)
    if not status.get("configured") or not status.get("runtime_enabled"):
        return load_provider_config().get("quick_choices", [])
    recommended_ids = {
        str(model["id"])
        for model in FALLBACK_CODEX_MODELS
        if model.get("recommended")
    }
    candidates = [
        model_info for model_info in list_codex_model_infos()
        if not recommended_ids or model_info.model_id in recommended_ids
    ]
    for model_info in candidates[:max(0, max_choices)]:
        add_quick_choice_for_model(
            model_info.model_id,
            provider_id=CODEX_PROVIDER_ID,
            display_name=model_info.display_name,
            source="codex_recommended",
            capabilities_snapshot=model_info.capability_snapshot(),
        )
    return load_provider_config().get("quick_choices", [])
