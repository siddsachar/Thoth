import base64
import json
import socket
import threading
import urllib.error
import urllib.request
import zlib
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import row_bot.providers.config as provider_config
from row_bot.providers.auth_store import get_provider_secret
from row_bot.providers.models import AuthMethod, ModelInfo, ModelModality, ModelTask, TransportMode
from row_bot.providers.xai_catalog import XAI_COMPOSER_MODEL_ID
from row_bot.providers.xai_oauth import (
    DEFAULT_XAI_OAUTH_CLIENT_ID,
    DEFAULT_XAI_OAUTH_SCOPES,
    XAI_OAUTH_BASE_URL,
    XAI_OAUTH_VISION_PROBE_VERSION,
    XAIOAuthTokenSet,
    XAIOAuthError,
    authorization_from_xai_oauth_callback,
    check_xai_oauth_token_health,
    clear_xai_oauth_client_id_override,
    disconnect_xai_oauth_metadata,
    exchange_xai_oauth_authorization,
    fetch_xai_oauth_model_infos,
    list_xai_oauth_model_infos,
    normalize_xai_oauth_provider_id,
    refresh_xai_oauth_token,
    run_xai_oauth_runtime_probe,
    run_xai_oauth_vision_probe,
    save_xai_oauth_client_id,
    save_xai_oauth_tokens,
    seed_recommended_xai_oauth_quick_choices,
    start_xai_oauth_flow,
    wait_for_xai_oauth_loopback_authorization,
    xai_oauth_base_url,
    xai_oauth_client_id_status,
    xai_oauth_configured_client_id,
    xai_oauth_default_client_id,
    xai_oauth_saved_client_id_override,
    xai_oauth_vision_probe_needed,
)
from row_bot.secret_store import _set_backend_for_tests


ROOT = Path(__file__).resolve().parents[1]


class _MemoryKeyring:
    def __init__(self):
        self.values = {}

    def get_password(self, service, account):
        return self.values.get((service, account))

    def set_password(self, service, account, value):
        self.values[(service, account)] = value

    def delete_password(self, service, account):
        self.values.pop((service, account), None)


class _Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text or (json.dumps(self._payload) if payload is not None else "")

    def json(self):
        return self._payload


class _HttpClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if not self.responses:
            raise AssertionError("No fake response queued")
        return self.responses.pop(0)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if not self.responses:
            raise AssertionError("No fake response queued")
        return self.responses.pop(0)

    def close(self):
        self.closed = True


def _jwt(claims):
    def b64(data):
        return base64.urlsafe_b64encode(json.dumps(data).encode("utf-8")).rstrip(b"=").decode("ascii")

    return f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64(claims)}.sig"


def _valid_token(user_id="user-123", account_id="acct-123"):
    return _jwt({
        "exp": 1893456000,
        "sub": user_id,
        "account_id": account_id,
        "email": "person@example.test",
        "scope": "openid profile offline_access",
    })


def _discovery_payload():
    return {
        "issuer": "https://auth.x.ai",
        "authorization_endpoint": "https://auth.x.ai/oauth/authorize",
        "token_endpoint": "https://auth.x.ai/oauth/token",
    }


def _free_loopback_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_xai_oauth_provider_definition_and_routing_are_separate():
    from row_bot.providers.catalog import classify_model_capabilities, get_provider_definition, infer_provider_id
    from row_bot.providers.resolution import resolve_provider_config
    from row_bot.providers.selection import model_choice_value

    oauth_definition = get_provider_definition("xai_oauth")
    api_key_definition = get_provider_definition("xai")

    assert oauth_definition is not None
    assert oauth_definition.display_name == "xAI Grok"
    assert oauth_definition.default_transport == TransportMode.OPENAI_RESPONSES
    assert AuthMethod.OAUTH_PKCE in oauth_definition.auth_methods
    assert oauth_definition.risk_label == "subscription"
    assert api_key_definition is not None
    assert api_key_definition.display_name == "xAI API"
    assert api_key_definition.default_transport == TransportMode.OPENAI_CHAT
    assert api_key_definition.auth_methods == (AuthMethod.API_KEY,)
    assert infer_provider_id("grok-4") == "xai"
    assert normalize_xai_oauth_provider_id("grok-oauth") == "xai_oauth"
    assert model_choice_value("grok-4", provider_id="xai-oauth") == "model:xai_oauth:grok-4"

    resolved = resolve_provider_config("model:grok-oauth:grok-4")
    classified = classify_model_capabilities("xai_oauth", "grok-4", {"input_modalities": ["text", "image"]})

    assert resolved.provider_id == "xai_oauth"
    assert resolved.runtime_model == "grok-4"
    assert resolved.transport == TransportMode.OPENAI_RESPONSES
    assert classified["tasks"] == {ModelTask.RESPONSES.value}
    assert classified["transport"] == TransportMode.OPENAI_RESPONSES
    assert "image" in classified["input_modalities"]


def test_xai_oauth_start_exchange_refresh_shapes(monkeypatch):
    for env_var in (
        "ROW_BOT_XAI_OAUTH_CLIENT_ID",
        "ROW_BOT_XAI_OAUTH_SCOPES",
        "ROW_BOT_XAI_OAUTH_REDIRECT_PORT",
    ):
        monkeypatch.delenv(env_var, raising=False)
    access_token = _valid_token()
    client_id = "client-123"
    discovery_client = _HttpClient([_Response(200, _discovery_payload())])

    flow = start_xai_oauth_flow(http_client=discovery_client, client_id=client_id)
    parsed = urlparse(flow.authorization_url)
    query = parse_qs(parsed.query)
    authorization = authorization_from_xai_oauth_callback(
        flow,
        f"http://127.0.0.1:56121/callback?code=code-123&state={flow.state}",
    )
    exchange_client = _HttpClient([_Response(200, {
        "access_token": access_token,
        "refresh_token": "refresh-secret",
        "id_token": "id-secret",
        "expires_in": 3600,
    })])
    token_set = exchange_xai_oauth_authorization(authorization, http_client=exchange_client)
    refresh_client = _HttpClient([_Response(200, {"access_token": access_token})])
    refreshed = refresh_xai_oauth_token(
        "refresh-secret",
        http_client=refresh_client,
        token_endpoint=flow.token_endpoint,
        client_id=client_id,
    )

    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == "https://auth.x.ai/oauth/authorize"
    assert flow.token_endpoint == "https://auth.x.ai/oauth/token"
    assert flow.client_id == client_id
    assert flow.scopes == DEFAULT_XAI_OAUTH_SCOPES
    assert query["response_type"] == ["code"]
    assert query["client_id"] == [client_id]
    assert query["scope"] == [" ".join(DEFAULT_XAI_OAUTH_SCOPES)]
    assert query["state"] == [flow.state]
    assert query["nonce"] == [flow.nonce]
    assert query["code_challenge"] == [flow.code_challenge]
    assert query["code_challenge_method"] == ["S256"]
    assert query["plan"] == ["generic"]
    assert query["referrer"] == ["row-bot"]
    assert authorization.authorization_code == "code-123"
    assert authorization.code_challenge == flow.code_challenge
    assert authorization.code_challenge_method == "S256"
    assert token_set.access_token == access_token
    assert token_set.refresh_token == "refresh-secret"
    assert token_set.user_id == "user-123"
    assert refreshed.refresh_token == "refresh-secret"

    assert discovery_client.calls[0][0:2] == ("GET", "https://auth.x.ai/.well-known/openid-configuration")
    assert exchange_client.calls[0][1] == "https://auth.x.ai/oauth/token"
    exchange_kwargs = exchange_client.calls[0][2]
    assert exchange_kwargs["data"]["grant_type"] == "authorization_code"
    assert exchange_kwargs["data"]["client_id"] == client_id
    assert exchange_kwargs["data"]["code_verifier"] == flow.code_verifier
    assert exchange_kwargs["data"]["code_challenge"] == flow.code_challenge
    assert exchange_kwargs["data"]["code_challenge_method"] == "S256"
    assert exchange_kwargs["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    assert exchange_kwargs["headers"]["User-Agent"].startswith("Row-Bot/")
    refresh_kwargs = refresh_client.calls[0][2]
    assert refresh_kwargs["data"] == {
        "grant_type": "refresh_token",
        "refresh_token": "refresh-secret",
        "client_id": client_id,
    }


def test_xai_oauth_loopback_callback_success(monkeypatch):
    for env_var in (
        "ROW_BOT_XAI_OAUTH_CLIENT_ID",
        "ROW_BOT_XAI_OAUTH_SCOPES",
        "ROW_BOT_XAI_OAUTH_REDIRECT_PORT",
    ):
        monkeypatch.delenv(env_var, raising=False)
    port = _free_loopback_port()
    flow = start_xai_oauth_flow(
        http_client=_HttpClient([_Response(200, _discovery_payload())]),
        client_id="client-123",
        redirect_uri=f"http://127.0.0.1:{port}/callback",
    )
    opened = threading.Event()
    result = {}

    def _browser_open(url):
        result["opened_url"] = url
        opened.set()
        return True

    thread = threading.Thread(
        target=lambda: result.setdefault(
            "authorization",
            wait_for_xai_oauth_loopback_authorization(
                flow,
                browser_open=_browser_open,
                timeout_seconds=3,
            ),
        )
    )
    thread.start()
    assert opened.wait(2)
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/callback?code=code-123&state={flow.state}", timeout=3) as response:
        body = response.read().decode("utf-8")
    thread.join(3)

    assert not thread.is_alive()
    assert result["opened_url"] == flow.authorization_url
    assert result["authorization"].authorization_code == "code-123"
    assert result["authorization"].client_id == "client-123"
    assert "xAI Grok connected" in body


def test_xai_oauth_loopback_rejects_missing_callback_code(monkeypatch):
    monkeypatch.delenv("ROW_BOT_XAI_OAUTH_CLIENT_ID", raising=False)
    port = _free_loopback_port()
    flow = start_xai_oauth_flow(
        http_client=_HttpClient([_Response(200, _discovery_payload())]),
        client_id="client-123",
        redirect_uri=f"http://127.0.0.1:{port}/callback",
    )
    opened = threading.Event()
    result = {}

    def _run():
        try:
            wait_for_xai_oauth_loopback_authorization(
                flow,
                browser_open=lambda url: opened.set() or True,
                timeout_seconds=3,
            )
        except Exception as exc:
            result["error"] = exc

    thread = threading.Thread(target=_run)
    thread.start()
    assert opened.wait(2)
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/callback", timeout=3)
    except urllib.error.HTTPError as exc:
        assert exc.code == 400
    thread.join(3)

    assert not thread.is_alive()
    assert isinstance(result["error"], XAIOAuthError)
    assert "did not include a code or error" in str(result["error"])


def test_xai_oauth_loopback_reports_occupied_port(monkeypatch):
    monkeypatch.delenv("ROW_BOT_XAI_OAUTH_CLIENT_ID", raising=False)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = int(sock.getsockname()[1])
        flow = start_xai_oauth_flow(
            http_client=_HttpClient([_Response(200, _discovery_payload())]),
            client_id="client-123",
            redirect_uri=f"http://127.0.0.1:{port}/callback",
        )
        try:
            wait_for_xai_oauth_loopback_authorization(flow, open_browser=False, timeout_seconds=0.1)
        except XAIOAuthError as exc:
            assert exc.kind == "loopback_port_unavailable"
            assert str(port) in str(exc)
        else:
            raise AssertionError("Expected occupied xAI OAuth callback port to fail")


def test_xai_oauth_loopback_can_be_cancelled(monkeypatch):
    monkeypatch.delenv("ROW_BOT_XAI_OAUTH_CLIENT_ID", raising=False)
    port = _free_loopback_port()
    flow = start_xai_oauth_flow(
        http_client=_HttpClient([_Response(200, _discovery_payload())]),
        client_id="client-123",
        redirect_uri=f"http://127.0.0.1:{port}/callback",
    )
    ready = threading.Event()
    cancel = threading.Event()
    result = {}

    def _run():
        try:
            wait_for_xai_oauth_loopback_authorization(
                flow,
                open_browser=False,
                ready_callback=ready.set,
                cancel_event=cancel,
                timeout_seconds=3,
            )
        except Exception as exc:
            result["error"] = exc

    thread = threading.Thread(target=_run)
    thread.start()
    assert ready.wait(2)
    cancel.set()
    thread.join(2)

    assert not thread.is_alive()
    assert isinstance(result["error"], XAIOAuthError)
    assert result["error"].kind == "loopback_cancelled"


def test_xai_oauth_flow_uses_default_client_id_without_env_or_saved_config(tmp_path, monkeypatch):
    for env_var in (
        "ROW_BOT_XAI_OAUTH_CLIENT_ID",
        "ROW_BOT_XAI_OAUTH_SCOPES",
        "ROW_BOT_XAI_OAUTH_REDIRECT_PORT",
    ):
        monkeypatch.delenv(env_var, raising=False)
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    client = _HttpClient([_Response(200, _discovery_payload())])

    flow = start_xai_oauth_flow(http_client=client)
    query = parse_qs(urlparse(flow.authorization_url).query)
    status = xai_oauth_client_id_status()

    assert DEFAULT_XAI_OAUTH_CLIENT_ID
    assert len(DEFAULT_XAI_OAUTH_CLIENT_ID) == 36
    assert DEFAULT_XAI_OAUTH_CLIENT_ID.endswith("9264a828")
    assert flow.client_id == DEFAULT_XAI_OAUTH_CLIENT_ID
    assert query["client_id"] == [DEFAULT_XAI_OAUTH_CLIENT_ID]
    assert status["configured"] is True
    assert status["source"] == "default"
    assert status["default_configured"] is True
    assert xai_oauth_default_client_id() == DEFAULT_XAI_OAUTH_CLIENT_ID
    assert xai_oauth_saved_client_id_override() == ""
    assert client.calls[0][0] == "GET"


def test_xai_oauth_client_id_can_be_saved_and_used_for_flow(tmp_path, monkeypatch):
    import row_bot.providers.xai_oauth as xai_oauth_module

    for env_var in (
        "ROW_BOT_XAI_OAUTH_CLIENT_ID",
        "ROW_BOT_XAI_OAUTH_SCOPES",
        "ROW_BOT_XAI_OAUTH_REDIRECT_PORT",
    ):
        monkeypatch.delenv(env_var, raising=False)
    monkeypatch.setattr(xai_oauth_module, "DEFAULT_XAI_OAUTH_CLIENT_ID", "shared-default-client")
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    saved = save_xai_oauth_client_id("client-123")
    discovery_client = _HttpClient([_Response(200, _discovery_payload())])

    flow = start_xai_oauth_flow(http_client=discovery_client)
    status = xai_oauth_client_id_status()
    parsed = urlparse(flow.authorization_url)
    query = parse_qs(parsed.query)

    assert saved["oauth_client_id_configured"] is True
    assert saved["configured"] is False
    assert status["configured"] is True
    assert status["source"] == "override"
    assert status["fingerprint"]
    assert xai_oauth_saved_client_id_override() == "client-123"
    assert xai_oauth_configured_client_id() == "client-123"
    assert flow.client_id == "client-123"
    assert query["client_id"] == ["client-123"]

    cleared = clear_xai_oauth_client_id_override()
    status_after_clear = xai_oauth_client_id_status()

    assert "oauth_client_id" not in cleared
    assert xai_oauth_saved_client_id_override() == ""
    assert xai_oauth_configured_client_id() == "shared-default-client"
    assert status_after_clear["source"] == "default"


def test_xai_oauth_token_save_status_and_no_secret_leak(tmp_path, monkeypatch):
    import row_bot.providers.runtime as runtime

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    backend = _MemoryKeyring()
    _set_backend_for_tests(backend)
    access_token = _valid_token()
    try:
        provider_config.save_provider_config({
            "providers": {
                "xai_oauth": {
                    "last_runtime_probe": {"ok": False, "errors": ["old failure"]},
                },
            },
        })
        saved = save_xai_oauth_tokens(XAIOAuthTokenSet(
            access_token=access_token,
            refresh_token="refresh-secret",
            id_token="id-secret",
        ))
        status = runtime.provider_status("xai_oauth")
        configured = runtime.list_configured_provider_ids()
        encoded = json.dumps(saved)

        assert saved["configured"] is True
        assert saved["auth_method"] == AuthMethod.OAUTH_PKCE.value
        assert saved["source"] == AuthMethod.OAUTH_PKCE.value
        assert saved["user_hash"] == "****-123"
        assert saved["account_id_hash"] == "****-123"
        assert saved["email_hash"] == "****test"
        assert "last_runtime_probe" not in saved
        assert status["runtime_enabled"] is True
        assert status["token_health"] == "valid"
        assert "xai_oauth" in configured
        assert get_provider_secret("xai_oauth", "access_token") == access_token
        assert get_provider_secret("xai_oauth", "refresh_token") == "refresh-secret"
        assert access_token not in encoded
        assert "refresh-secret" not in encoded
    finally:
        _set_backend_for_tests(None)


def test_xai_oauth_token_save_records_default_client_id_without_saving_override(tmp_path, monkeypatch):
    import row_bot.providers.xai_oauth as xai_oauth_module

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(xai_oauth_module, "DEFAULT_XAI_OAUTH_CLIENT_ID", "shared-default-client")
    monkeypatch.delenv("ROW_BOT_XAI_OAUTH_CLIENT_ID", raising=False)
    backend = _MemoryKeyring()
    _set_backend_for_tests(backend)
    try:
        saved = save_xai_oauth_tokens(XAIOAuthTokenSet(
            access_token=_valid_token(),
            refresh_token="refresh-secret",
            id_token="id-secret",
        ))
        status = xai_oauth_client_id_status()

        assert saved["oauth_client_id_configured"] is True
        assert saved["oauth_client_id_source"] == "default"
        assert "oauth_client_id" not in saved
        assert status["configured"] is True
        assert status["source"] == "default"
        assert xai_oauth_saved_client_id_override() == ""
        assert xai_oauth_configured_client_id() == "shared-default-client"
    finally:
        _set_backend_for_tests(None)


def test_xai_oauth_stale_environment_metadata_does_not_block_default(tmp_path, monkeypatch):
    import row_bot.providers.xai_oauth as xai_oauth_module

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(xai_oauth_module, "DEFAULT_XAI_OAUTH_CLIENT_ID", "shared-default-client")
    monkeypatch.delenv("ROW_BOT_XAI_OAUTH_CLIENT_ID", raising=False)
    provider_config.save_provider_config({
        "providers": {
            "xai_oauth": {
                "provider_id": "xai_oauth",
                "auth_method": AuthMethod.OAUTH_PKCE.value,
                "oauth_client_id_configured": True,
                "oauth_client_id_source": "environment",
                "oauth_client_id_fingerprint": "****stale",
            },
        },
    })

    status = xai_oauth_client_id_status()

    assert status["configured"] is True
    assert status["source"] == "default"
    assert xai_oauth_saved_client_id_override() == ""
    assert xai_oauth_configured_client_id() == "shared-default-client"


def test_xai_oauth_status_requires_stored_credentials(tmp_path, monkeypatch):
    import row_bot.providers.runtime as runtime

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.delenv("ROW_BOT_XAI_OAUTH_CLIENT_ID", raising=False)
    backend = _MemoryKeyring()
    _set_backend_for_tests(backend)
    try:
        provider_config.save_provider_config({
            "providers": {
                "xai_oauth": {
                    "provider_id": "xai_oauth",
                    "auth_method": AuthMethod.OAUTH_PKCE.value,
                    "configured": True,
                    "source": AuthMethod.OAUTH_PKCE.value,
                    "fingerprint": "****stale",
                },
            },
        })
        status = runtime.provider_status("xai_oauth")

        assert status["configured"] is False
        assert status["runtime_enabled"] is False
        assert status["token_health"] == "missing"
        assert "xai_oauth" not in runtime.list_configured_provider_ids()
    finally:
        _set_backend_for_tests(None)


def test_xai_oauth_expired_refresh_reconnect_clears_oauth_only(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    backend = _MemoryKeyring()
    _set_backend_for_tests(backend)
    expired_token = _jwt({"exp": 946684800, "sub": "old-user"})
    try:
        save_xai_oauth_client_id("client-123")
        save_xai_oauth_tokens(XAIOAuthTokenSet(
            access_token=expired_token,
            refresh_token="refresh-secret",
        ))
        health = check_xai_oauth_token_health(
            http_client=_HttpClient([_Response(400, {"error": "invalid_grant"})]),
        )
        cfg = provider_config.load_provider_config()["providers"]["xai_oauth"]

        assert health.status == "expired"
        assert health.runnable is False
        assert cfg["configured"] is False
        assert cfg["auth_method"] == AuthMethod.OAUTH_PKCE.value
        assert cfg["oauth_client_id"] == "client-123"
        assert cfg["oauth_client_id_source"] == "override"
        assert xai_oauth_configured_client_id() == "client-123"
        assert get_provider_secret("xai_oauth", "access_token") == ""
        assert get_provider_secret("xai_oauth", "refresh_token") == ""
        assert get_provider_secret("xai", "api_key") == ""
    finally:
        _set_backend_for_tests(None)


def test_xai_oauth_unsafe_base_url_is_rejected_and_reset(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    provider_config.save_provider_config({
        "providers": {
            "xai_oauth": {
                "base_url": "http://not-xai.example/v1",
            },
        },
    })

    base_url = xai_oauth_base_url()
    cfg = provider_config.load_provider_config()["providers"]["xai_oauth"]

    assert base_url == XAI_OAUTH_BASE_URL
    assert cfg["base_url"] == XAI_OAUTH_BASE_URL
    assert "HTTPS xAI-owned" in cfg["base_url_warning"]


def test_xai_oauth_model_catalog_live_discovery_and_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    _set_backend_for_tests(_MemoryKeyring())
    client = _HttpClient([_Response(200, {
        "data": [
            {
                "id": "grok-4",
                "display_name": "Grok 4",
                "context_window": 2000000,
                "input_modalities": ["text"],
            },
            {
                "id": "grok-4-vision",
                "display_name": "Grok 4 Vision",
                "capabilities": {"image_input": {"supported": True}},
            },
            {"id": "grok-imagine-image", "display_name": "Grok Imagine"},
            {"id": "grok-internal", "hidden": True},
        ],
    })])
    try:
        save_xai_oauth_tokens(XAIOAuthTokenSet(access_token=_valid_token(), refresh_token="refresh-secret"))
        infos = fetch_xai_oauth_model_infos(access_token="access-token", http_client=client)
        cached = list_xai_oauth_model_infos(force_refresh=True, http_client=_HttpClient([_Response(200, {
            "data": [{"id": "grok-live", "display_name": "Grok Live"}],
        })]))
        cached_again = list_xai_oauth_model_infos()
    finally:
        _set_backend_for_tests(None)

    assert client.calls[0][1] == "https://api.x.ai/v1/models"
    assert client.calls[0][2]["headers"]["Authorization"] == "Bearer access-token"
    assert {info.model_id for info in infos} == {"grok-4", "grok-4-vision", "grok-imagine-image", XAI_COMPOSER_MODEL_ID}
    vision = next(info for info in infos if info.model_id == "grok-4-vision")
    media = next(info for info in infos if info.model_id == "grok-imagine-image")
    composer = next(info for info in infos if info.model_id == XAI_COMPOSER_MODEL_ID)
    assert vision.provider_id == "xai_oauth"
    assert vision.transport == TransportMode.OPENAI_RESPONSES
    assert vision.tasks == frozenset({ModelTask.RESPONSES.value})
    assert "image" in vision.input_modalities
    assert vision.selection_ref == "model:xai_oauth:grok-4-vision"
    assert media.provider_id == "xai_oauth"
    assert media.tasks == frozenset({ModelTask.IMAGE_GENERATION.value})
    assert media.output_modalities == frozenset({"image"})
    assert media.tool_calling is False
    assert media.selection_ref == "model:xai_oauth:grok-imagine-image"
    assert composer.provider_id == "xai_oauth"
    assert composer.transport == TransportMode.OPENAI_RESPONSES
    assert composer.tasks == frozenset({ModelTask.RESPONSES.value})
    assert "image" not in composer.input_modalities
    assert composer.selection_ref == f"model:xai_oauth:{XAI_COMPOSER_MODEL_ID}"
    assert [info.model_id for info in cached] == ["grok-live", XAI_COMPOSER_MODEL_ID]
    assert [info.model_id for info in cached_again] == ["grok-live", XAI_COMPOSER_MODEL_ID]


def test_xai_api_key_discovery_merges_model_endpoints_and_curated_composer(monkeypatch):
    import sys
    from types import SimpleNamespace

    import row_bot.models as model_registry

    class _XAIResponse:
        def __init__(self, payload):
            self.status_code = 200
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    calls: list[str] = []

    def _get(url, **kwargs):
        calls.append(url)
        if url.endswith("/models"):
            return _XAIResponse({"data": [
                {"id": "grok-4.3", "display_name": "Grok 4.3"},
                {"id": "grok-imagine-image-quality", "display_name": "Grok Imagine Quality"},
                {"id": "grok-imagine-video", "display_name": "Grok Imagine Video"},
            ]})
        if url.endswith("/language-models"):
            return _XAIResponse({"models": [
                {"id": "grok-4.3", "input_modalities": ["text", "image"]},
            ]})
        if url.endswith("/image-generation-models"):
            return _XAIResponse({"models": [{
                "id": "future-renderer",
                "display_name": "Future Renderer",
                "pricing": [
                    {"quality": "low", "resolution": "1k", "price_per_image": 4},
                    {"quality": "medium", "resolution": "1k", "price_per_image": 6},
                    {"quality": "medium", "resolution": "2k", "price_per_image": 8},
                ],
            }]})
        raise AssertionError(url)

    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(get=_get))
    with model_registry._cloud_cache_lock:
        original_cache = dict(model_registry._cloud_model_cache)
        model_registry._cloud_model_cache.clear()
    try:
        count = model_registry._fetch_xai_models("xai-key")
        with model_registry._cloud_cache_lock:
            cached = dict(model_registry._cloud_model_cache)
    finally:
        with model_registry._cloud_cache_lock:
            model_registry._cloud_model_cache.clear()
            model_registry._cloud_model_cache.update(original_cache)

    assert calls == [
        "https://api.x.ai/v1/models",
        "https://api.x.ai/v1/language-models",
        "https://api.x.ai/v1/image-generation-models",
    ]
    assert count == 5
    assert {"grok-4.3", "grok-imagine-image-quality", "grok-imagine-video", "future-renderer", XAI_COMPOSER_MODEL_ID} <= set(cached)
    assert cached["grok-4.3"]["vision"] is True
    assert cached[XAI_COMPOSER_MODEL_ID]["provider"] == "xai"
    assert cached[XAI_COMPOSER_MODEL_ID]["transport"] == "openai_chat"
    assert cached[XAI_COMPOSER_MODEL_ID]["capabilities_snapshot"]["tasks"] == ["chat"]
    assert cached["grok-imagine-image-quality"]["capabilities_snapshot"]["tasks"] == ["image_generation"]
    assert cached["grok-imagine-image-quality"]["capabilities_snapshot"]["output_modalities"] == ["image"]
    assert cached["grok-imagine-video"]["capabilities_snapshot"]["tasks"] == ["video_generation"]
    assert cached["future-renderer"]["capabilities_snapshot"]["generation_parameters"]["valid_combinations"][-1] == {
        "quality": "medium",
        "resolution": "2k",
    }


def test_xai_oauth_image_metadata_merges_for_unknown_model_and_survives_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    _set_backend_for_tests(_MemoryKeyring())
    client = _HttpClient([
        _Response(200, {"data": [{"id": "future-renderer", "display_name": "Basic Row"}]}),
        _Response(200, {"models": []}),
        _Response(200, {"models": [{
            "id": "future-renderer",
            "display_name": "Future Renderer",
            "input_modalities": ["text", "image"],
            "output_modalities": ["image"],
            "pricing": [
                {"quality": "low", "resolution": "1k", "price_per_image": 4},
                {"quality": "low", "resolution": "2k", "price_per_image": 6},
                {"quality": "medium", "resolution": "1k", "price_per_image": 6},
                {"quality": "medium", "resolution": "2k", "price_per_image": 8},
            ],
        }]}),
    ])
    try:
        save_xai_oauth_tokens(XAIOAuthTokenSet(access_token=_valid_token(), refresh_token="refresh-secret"))
        live = list_xai_oauth_model_infos(force_refresh=True, http_client=client)
        cached = list_xai_oauth_model_infos()
    finally:
        _set_backend_for_tests(None)

    assert [call[1] for call in client.calls] == [
        "https://api.x.ai/v1/models",
        "https://api.x.ai/v1/language-models",
        "https://api.x.ai/v1/image-generation-models",
    ]
    live_info = next(info for info in live if info.model_id == "future-renderer")
    cached_info = next(info for info in cached if info.model_id == "future-renderer")
    assert live_info.display_name == "Future Renderer"
    assert live_info.tasks == frozenset({ModelTask.IMAGE_GENERATION.value})
    assert live_info.generation_parameters["defaults"] == {"quality": "medium", "resolution": "1k"}
    assert cached_info.generation_parameters == live_info.generation_parameters


def test_xai_oauth_model_discovery_accepts_new_model_ids_without_static_allowlist(tmp_path, monkeypatch):
    from row_bot.providers.model_catalog import build_model_catalog_rows

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(
        "row_bot.providers.model_catalog._provider_status_by_id",
        lambda: {"xai_oauth": {"configured": True, "runtime_enabled": True}},
    )
    _set_backend_for_tests(_MemoryKeyring())
    try:
        save_xai_oauth_tokens(XAIOAuthTokenSet(access_token=_valid_token(), refresh_token="refresh-secret"))
        infos = list_xai_oauth_model_infos(force_refresh=True, http_client=_HttpClient([_Response(200, {
            "data": [{
                "id": "grok-new-future-2027",
                "display_name": "Grok New Future",
                "input_modalities": ["text"],
                "capabilities": ["tool_calling"],
            }],
        })]))
        rows = build_model_catalog_rows(cloud_cache={}, ollama_rows=[], defaults={}, quick_choices=[])
    finally:
        _set_backend_for_tests(None)

    assert "model:xai_oauth:grok-new-future-2027" in [info.selection_ref for info in infos]
    assert f"model:xai_oauth:{XAI_COMPOSER_MODEL_ID}" in [info.selection_ref for info in infos]
    row = next(row for row in rows if row.selection_ref == "model:xai_oauth:grok-new-future-2027")
    assert row.provider_id == "xai_oauth"
    assert "chat" in row.categories
    assert row.capabilities_snapshot["transport"] == "openai_responses"


def test_xai_oauth_capability_refresh_updates_and_removes_vision(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    _set_backend_for_tests(_MemoryKeyring())
    try:
        save_xai_oauth_tokens(XAIOAuthTokenSet(access_token=_valid_token(), refresh_token="refresh-secret"))
        first = list_xai_oauth_model_infos(force_refresh=True, http_client=_HttpClient([_Response(200, {
            "data": [{
                "id": "grok-drift",
                "display_name": "Grok Drift",
                "input_modalities": ["text"],
            }],
        })]))
        second = list_xai_oauth_model_infos(force_refresh=True, http_client=_HttpClient([_Response(200, {
            "data": [{
                "id": "grok-drift",
                "display_name": "Grok Drift",
                "input_modalities": ["text", "image"],
            }],
        })]))
        third = list_xai_oauth_model_infos(force_refresh=True, http_client=_HttpClient([_Response(200, {
            "data": [{
                "id": "grok-drift",
                "display_name": "Grok Drift",
                "input_modalities": ["text"],
            }],
        })]))
        cached = list_xai_oauth_model_infos()
    finally:
        _set_backend_for_tests(None)

    assert "image" not in first[0].input_modalities
    assert "vision" not in first[0].capabilities
    assert "image" in second[0].input_modalities
    assert "vision" in second[0].capabilities
    assert "image" not in third[0].input_modalities
    assert "vision" not in third[0].capabilities
    assert "image" not in cached[0].input_modalities
    assert "vision" not in cached[0].capabilities


def test_xai_oauth_probe_image_data_url_is_valid_png():
    import row_bot.providers.xai_oauth as xai_oauth

    prefix = "data:image/png;base64,"
    data_url = xai_oauth._probe_image_data_url()
    assert data_url.startswith(prefix)
    data = base64.b64decode(data_url[len(prefix):], validate=True)
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    assert width >= 8
    assert height >= 8
    assert width * height >= 512

    offset = 8
    chunks: list[bytes] = []
    while offset < len(data):
        assert offset + 12 <= len(data)
        length = int.from_bytes(data[offset:offset + 4], "big")
        chunk_type = data[offset + 4:offset + 8]
        chunk_data = data[offset + 8:offset + 8 + length]
        chunk_crc = int.from_bytes(data[offset + 8 + length:offset + 12 + length], "big")
        assert offset + 12 + length <= len(data)
        assert zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF == chunk_crc
        chunks.append(chunk_type)
        offset += 12 + length

    assert offset == len(data)
    assert chunks[0] == b"IHDR"
    assert b"IDAT" in chunks
    assert chunks[-1] == b"IEND"


def test_xai_oauth_vision_probe_updates_cached_capability_snapshot(tmp_path, monkeypatch):
    from langchain_core.messages import AIMessage

    class _VisionModel:
        def __init__(self, content="image"):
            self.content = content
            self.messages = []

        def invoke(self, messages):
            self.messages.extend(messages)
            return AIMessage(content=self.content)

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    _set_backend_for_tests(_MemoryKeyring())
    try:
        save_xai_oauth_tokens(XAIOAuthTokenSet(access_token=_valid_token(), refresh_token="refresh-secret"))
        list_xai_oauth_model_infos(force_refresh=True, http_client=_HttpClient([_Response(200, {
            "data": [{
                "id": "grok-probe",
                "display_name": "Grok Probe",
                "input_modalities": ["text"],
            }],
        })]))
        success_model = _VisionModel("image")
        success = run_xai_oauth_vision_probe("grok-probe", chat_model=success_model)
        confirmed = list_xai_oauth_model_infos()[0]
        failure = run_xai_oauth_vision_probe("grok-probe", chat_model=_VisionModel("text only"))
        failed = list_xai_oauth_model_infos()[0]
        cfg = provider_config.load_provider_config()["providers"]["xai_oauth"]
    finally:
        _set_backend_for_tests(None)

    assert success["ok"] is True
    assert success["probe_version"] == XAI_OAUTH_VISION_PROBE_VERSION
    assert success["status"] == "confirmed"
    assert success_model.messages
    content = success_model.messages[0].content
    assert isinstance(content, list)
    assert any(isinstance(item, dict) and item.get("type") == "image_url" for item in content)
    assert "image" in confirmed.input_modalities
    assert "vision" in confirmed.capabilities
    assert failure["ok"] is False
    assert failure["status"] == "failed"
    assert "image" not in failed.input_modalities
    assert "vision" not in failed.capabilities
    assert cfg["last_vision_probe"]["model_id"] == "grok-probe"
    assert cfg["catalog_cache"]["models"][0]["vision_probe_status"] == "failed"


def test_xai_oauth_background_vision_probe_tries_discovered_models_and_survives_refresh(tmp_path, monkeypatch):
    from langchain_core.messages import AIMessage
    import row_bot.providers.transports.xai_oauth_responses as transport_module

    calls: list[str] = []

    class _VisionModel:
        def __init__(self, model_name, timeout=0):
            self.model_name = model_name

        def invoke(self, messages):
            calls.append(self.model_name)
            if self.model_name in {"grok-second", "grok-third"}:
                return AIMessage(content="image")
            return AIMessage(content="text only")

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(transport_module, "ChatXAIOAuthResponses", _VisionModel)
    _set_backend_for_tests(_MemoryKeyring())
    try:
        save_xai_oauth_tokens(XAIOAuthTokenSet(access_token=_valid_token(), refresh_token="refresh-secret"))
        list_xai_oauth_model_infos(force_refresh=True, http_client=_HttpClient([_Response(200, {
            "data": [
                {"id": "grok-first", "display_name": "Grok First", "input_modalities": ["text"]},
                {"id": "grok-second", "display_name": "Grok Second", "input_modalities": ["text"]},
                {"id": "grok-third", "display_name": "Grok Third", "input_modalities": ["text"]},
            ],
        })]))
        result = run_xai_oauth_vision_probe()
        confirmed = {info.model_id: info for info in list_xai_oauth_model_infos()}
        refreshed = list_xai_oauth_model_infos(force_refresh=True, http_client=_HttpClient([_Response(200, {
            "data": [
                {"id": "grok-first", "display_name": "Grok First", "input_modalities": ["text"]},
                {"id": "grok-second", "display_name": "Grok Second", "input_modalities": ["text"]},
                {"id": "grok-third", "display_name": "Grok Third", "input_modalities": ["text"]},
            ],
        })]))
    finally:
        _set_backend_for_tests(None)

    assert calls == ["grok-first", "grok-second", "grok-third", XAI_COMPOSER_MODEL_ID]
    assert result["ok"] is True
    assert result["probe_version"] == XAI_OAUTH_VISION_PROBE_VERSION
    assert result["model_id"] == "grok-second"
    assert result["confirmed_model_ids"] == ["grok-second", "grok-third"]
    assert result["failed_model_ids"] == ["grok-first", XAI_COMPOSER_MODEL_ID]
    assert "image" not in confirmed["grok-first"].input_modalities
    assert "image" in confirmed["grok-second"].input_modalities
    assert "vision" in confirmed["grok-second"].capabilities
    assert "image" in confirmed["grok-third"].input_modalities
    assert "vision" in confirmed["grok-third"].capabilities
    refreshed_by_id = {info.model_id: info for info in refreshed}
    assert "image" in refreshed_by_id["grok-second"].input_modalities
    assert "vision" in refreshed_by_id["grok-second"].capabilities
    assert "image" in refreshed_by_id["grok-third"].input_modalities
    assert "vision" in refreshed_by_id["grok-third"].capabilities


def test_xai_oauth_vision_probe_needed_tracks_version_and_new_models(tmp_path, monkeypatch):
    from langchain_core.messages import AIMessage
    import row_bot.providers.transports.xai_oauth_responses as transport_module

    class _VisionModel:
        def __init__(self, model_name="", timeout=0):
            self.model_name = model_name

        def invoke(self, messages):
            return AIMessage(content="image")

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(transport_module, "ChatXAIOAuthResponses", _VisionModel)
    _set_backend_for_tests(_MemoryKeyring())
    try:
        save_xai_oauth_tokens(XAIOAuthTokenSet(access_token=_valid_token(), refresh_token="refresh-secret"))
        list_xai_oauth_model_infos(force_refresh=True, http_client=_HttpClient([_Response(200, {
            "data": [{"id": "grok-one", "display_name": "Grok One", "input_modalities": ["text"]}],
        })]))
        needs_initial_probe = xai_oauth_vision_probe_needed()
        run_xai_oauth_vision_probe()
        needs_after_current_probe = xai_oauth_vision_probe_needed()
        list_xai_oauth_model_infos(force_refresh=True, http_client=_HttpClient([_Response(200, {
            "data": [
                {"id": "grok-one", "display_name": "Grok One", "input_modalities": ["text"]},
                {"id": "grok-two", "display_name": "Grok Two", "input_modalities": ["text"]},
            ],
        })]))
        needs_after_new_model = xai_oauth_vision_probe_needed()
    finally:
        _set_backend_for_tests(None)

    assert needs_initial_probe is True
    assert needs_after_current_probe is False
    assert needs_after_new_model is True


def test_xai_oauth_model_refresh_runs_stale_vision_probe(monkeypatch):
    import row_bot.models as model_registry
    import row_bot.providers.xai_oauth as xai_oauth_module

    text_info = ModelInfo(
        provider_id="xai_oauth",
        model_id="grok-refresh",
        display_name="Grok Refresh",
        context_window=1024,
        transport=TransportMode.OPENAI_RESPONSES,
        capabilities=frozenset({"text", "chat", "streaming", "tool_calling"}),
        input_modalities=frozenset({ModelModality.TEXT.value}),
        output_modalities=frozenset({ModelModality.TEXT.value}),
        tasks=frozenset({ModelTask.RESPONSES.value}),
        tool_calling=True,
        streaming=True,
        endpoint_compatibility=frozenset({TransportMode.OPENAI_RESPONSES}),
        billing_label="subscription",
    )
    vision_info = ModelInfo(
        provider_id="xai_oauth",
        model_id="grok-refresh",
        display_name="Grok Refresh",
        context_window=1024,
        transport=TransportMode.OPENAI_RESPONSES,
        capabilities=frozenset({"text", "chat", "streaming", "tool_calling", "vision"}),
        input_modalities=frozenset({ModelModality.TEXT.value, ModelModality.IMAGE.value}),
        output_modalities=frozenset({ModelModality.TEXT.value}),
        tasks=frozenset({ModelTask.RESPONSES.value}),
        tool_calling=True,
        streaming=True,
        endpoint_compatibility=frozenset({TransportMode.OPENAI_RESPONSES}),
        billing_label="subscription",
    )
    probe_ran = {"value": False}

    def _fake_list_xai_oauth_model_infos(*, force_refresh=False, http_client=None):
        return [vision_info if probe_ran["value"] else text_info]

    def _fake_run_xai_oauth_vision_probe():
        probe_ran["value"] = True
        return {
            "ok": True,
            "confirmed_model_ids": ["grok-refresh"],
            "failed_model_ids": [],
        }

    monkeypatch.setattr(xai_oauth_module, "list_xai_oauth_model_infos", _fake_list_xai_oauth_model_infos)
    monkeypatch.setattr(xai_oauth_module, "xai_oauth_vision_probe_needed", lambda: not probe_ran["value"])
    monkeypatch.setattr(xai_oauth_module, "run_xai_oauth_vision_probe", _fake_run_xai_oauth_vision_probe)

    with model_registry._cloud_cache_lock:
        original_cache = dict(model_registry._cloud_model_cache)
        model_registry._cloud_model_cache.clear()
    try:
        count = model_registry._fetch_xai_oauth_models()
        with model_registry._cloud_cache_lock:
            cached = dict(model_registry._cloud_model_cache)
    finally:
        with model_registry._cloud_cache_lock:
            model_registry._cloud_model_cache.clear()
            model_registry._cloud_model_cache.update(original_cache)

    assert count == 1
    assert probe_ran["value"] is True
    entry = cached["model:xai_oauth:grok-refresh"]
    assert entry["vision"] is True
    assert "image" in entry["capabilities_snapshot"]["input_modalities"]


def test_xai_oauth_discovery_failure_uses_last_successful_cache_without_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    _set_backend_for_tests(_MemoryKeyring())
    try:
        save_xai_oauth_tokens(XAIOAuthTokenSet(access_token=_valid_token(), refresh_token="refresh-secret"))
        list_xai_oauth_model_infos(force_refresh=True, http_client=_HttpClient([_Response(200, {
            "data": [{"id": "grok-cached", "display_name": "Grok Cached"}],
        })]))
        fallback = list_xai_oauth_model_infos(force_refresh=True, http_client=_HttpClient([
            _Response(503, {"error": {"message": "temporary outage"}}),
        ]))
        cfg = provider_config.load_provider_config()["providers"]["xai_oauth"]
    finally:
        _set_backend_for_tests(None)

    assert [info.selection_ref for info in fallback] == [
        "model:xai_oauth:grok-cached",
        f"model:xai_oauth:{XAI_COMPOSER_MODEL_ID}",
    ]
    assert cfg["model_count_status"] == "unavailable"
    assert cfg["model_count_source"] == "live_xai_oauth_catalog"
    assert "temporary outage" in cfg["last_error"]


def test_xai_oauth_disappeared_selected_model_preserves_provider_ref(tmp_path, monkeypatch):
    from row_bot.providers.model_catalog import build_model_catalog_rows

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(
        "row_bot.providers.model_catalog._provider_status_by_id",
        lambda: {"xai_oauth": {"configured": True, "runtime_enabled": True}},
    )
    _set_backend_for_tests(_MemoryKeyring())
    try:
        save_xai_oauth_tokens(XAIOAuthTokenSet(access_token=_valid_token(), refresh_token="refresh-secret"))
        list_xai_oauth_model_infos(force_refresh=True, http_client=_HttpClient([_Response(200, {
            "data": [{"id": "grok-current", "display_name": "Grok Current"}],
        })]))
        rows = build_model_catalog_rows(
            cloud_cache={},
            ollama_rows=[],
            defaults={"chat": "model:xai_oauth:grok-vanished"},
            quick_choices=[],
        )
    finally:
        _set_backend_for_tests(None)

    vanished = next(row for row in rows if row.selection_ref == "model:xai_oauth:grok-vanished")
    assert vanished.provider_id == "xai_oauth"
    assert vanished.model_id == "grok-vanished"
    assert vanished.default_surfaces == ("chat",)
    assert vanished.selection_ref == "model:xai_oauth:grok-vanished"
    assert any(row.selection_ref == "model:xai_oauth:grok-current" for row in rows)
    assert not any(
        row.provider_id == "xai" and row.model_id == "grok-vanished"
        for row in rows
    )
    assert not any(
        row.provider_id == "xai" and "chat" in row.default_surfaces
        for row in rows
    )


def test_xai_oauth_runtime_factory_and_probe(tmp_path, monkeypatch):
    import row_bot.providers.runtime as runtime
    from langchain_core.messages import AIMessage, ToolMessage
    from row_bot.providers.transports.xai_oauth_responses import ChatXAIOAuthResponses

    class _ProbeModel:
        def __init__(self, *, bound=False):
            self.bound = bound

        def bind_tools(self, tools, tool_choice=None):
            assert [getattr(tool, "name", "") for tool in tools] == ["calculate"]
            assert tool_choice == "calculate"
            return _ProbeModel(bound=True)

        def invoke(self, messages):
            if self.bound:
                return AIMessage(content="", tool_calls=[{
                    "name": "calculate",
                    "args": {"expression": "1 + 1"},
                    "id": "call_probe",
                    "type": "tool_call",
                }])
            if any(isinstance(message, ToolMessage) for message in messages):
                return AIMessage(content="done")
            return AIMessage(content="row-bot-xai-smoke-ok")

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    _set_backend_for_tests(_MemoryKeyring())
    try:
        try:
            runtime.create_chat_model("grok-4", provider_id="xai_oauth")
        except ValueError as exc:
            assert "OAuth token" in str(exc)
        else:
            raise AssertionError("Expected xAI OAuth runtime to require OAuth tokens")

        save_xai_oauth_tokens(XAIOAuthTokenSet(access_token=_valid_token(), refresh_token="refresh-secret"))
        model = runtime.create_chat_model("grok-4", provider_id="xai_oauth")
        probe = run_xai_oauth_runtime_probe("grok-4", chat_model=_ProbeModel())
        provider_entry = provider_config.load_provider_config()["providers"]["xai_oauth"]

        assert isinstance(model, ChatXAIOAuthResponses)
        assert model.model_name == "grok-4"
        assert model.base_url == "https://api.x.ai/v1"
        assert probe["ok"] is True
        assert probe["chat_ok"] is True
        assert probe["tool_calling"] is True
        assert probe["tool_round_trip"] is True
        assert provider_entry["last_runtime_probe"]["ok"] is True
        assert provider_entry["last_error"] == ""
    finally:
        _set_backend_for_tests(None)


def test_xai_oauth_quick_choices_are_hidden_until_runtime_enabled(tmp_path, monkeypatch):
    import row_bot.api_keys as api_keys
    from row_bot.providers.selection import add_quick_choice_for_model, list_quick_model_ids

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(api_keys, "get_cloud_config", lambda: {"starred_models": []})
    monkeypatch.setattr("row_bot.providers.runtime.provider_status", lambda provider_id: {
        "configured": True,
        "runtime_enabled": False,
    })

    add_quick_choice_for_model(
        "grok-4",
        provider_id="xai_oauth",
        display_name="Grok 4",
        capabilities_snapshot={
            "tasks": ["responses"],
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "tool_calling": True,
            "streaming": True,
            "transport": "openai_responses",
        },
    )

    assert list_quick_model_ids("chat") == []


def test_xai_oauth_quick_choice_seed_requires_runtime_enabled(tmp_path, monkeypatch):
    from row_bot.providers.models import ModelInfo

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(
        "row_bot.providers.runtime.provider_status",
        lambda provider_id: {"configured": False, "runtime_enabled": False},
    )
    assert seed_recommended_xai_oauth_quick_choices() == []

    monkeypatch.setattr(
        "row_bot.providers.runtime.provider_status",
        lambda provider_id: {"configured": True, "runtime_enabled": True},
    )
    monkeypatch.setattr(
        "row_bot.providers.xai_oauth.list_xai_oauth_model_infos",
        lambda: [
            ModelInfo(
                provider_id="xai_oauth",
                model_id="grok-4",
                display_name="Grok 4",
                context_window=2_000_000,
                transport=TransportMode.OPENAI_RESPONSES,
                tasks=frozenset({"responses"}),
                capabilities=frozenset({"text", "chat", "tool_calling", "streaming"}),
                input_modalities=frozenset({"text"}),
                output_modalities=frozenset({"text"}),
                tool_calling=True,
                streaming=True,
                endpoint_compatibility=frozenset({TransportMode.OPENAI_RESPONSES}),
            )
        ],
    )

    quick = seed_recommended_xai_oauth_quick_choices()

    assert [choice["id"] for choice in quick] == ["model:xai_oauth:grok-4"]
    assert quick[0]["provider_id"] == "xai_oauth"


def test_xai_oauth_status_settings_and_wizard_hooks_are_present(tmp_path, monkeypatch):
    from row_bot.providers.status import provider_status_cards
    from row_bot.ui.provider_settings import _xai_oauth_action_state
    import row_bot.providers.xai_oauth as xai_oauth_module

    monkeypatch.delenv("ROW_BOT_XAI_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.setattr(xai_oauth_module, "DEFAULT_XAI_OAUTH_CLIENT_ID", "shared-default-client")
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    provider_config.save_provider_config({
        "providers": {
            "xai_oauth": {
                "provider_id": "xai_oauth",
                "auth_method": AuthMethod.OAUTH_PKCE.value,
                "last_vision_probe": {
                    "model_id": "grok-probe",
                    "ok": True,
                    "status": "confirmed",
                },
            },
        },
    })
    cards = provider_status_cards()
    xai_oauth_card = next(card for card in cards if card["provider_id"] == "xai_oauth")
    xai_api_card = next(card for card in cards if card["provider_id"] == "xai")
    provider_settings = (ROOT / "src" / "row_bot" / "ui" / "provider_settings.py").read_text(encoding="utf-8")
    setup_wizard = (ROOT / "src" / "row_bot" / "ui" / "setup_wizard.py").read_text(encoding="utf-8")
    x_tool = (ROOT / "src" / "row_bot" / "tools" / "x_tool.py").read_text(encoding="utf-8")

    assert xai_oauth_card["display_name"] == "xAI Grok"
    assert xai_oauth_card["group"] == "Subscription Accounts"
    assert xai_api_card["display_name"] == "xAI API"
    assert xai_api_card["group"] == "API Providers"
    assert xai_oauth_card["oauth_client_id_configured"] is True
    assert xai_oauth_card["oauth_client_id_source"] == "default"
    assert "Row-Bot default" in xai_oauth_card["oauth_client_id_detail"]
    assert xai_oauth_card["last_vision_probe"]["model_id"] == "grok-probe"
    assert xai_oauth_card["last_vision_probe"]["ok"] is True
    assert "_configure_xai_oauth_client_id_dialog" in provider_settings
    assert "save_xai_oauth_client_id" in provider_settings
    assert "clear_xai_oauth_client_id_override" in provider_settings
    assert "Using Row-Bot default OAuth client ID" in provider_settings
    assert "Using saved OAuth client ID override" in provider_settings
    assert "Configure xAI OAuth client ID before connecting" not in provider_settings
    assert "OAuth client ID override" in provider_settings
    assert "Reset to default" in provider_settings
    assert "_connect_xai_oauth_login" in provider_settings
    assert "wait_for_xai_oauth_loopback_authorization" in provider_settings
    assert "open_browser=False" in provider_settings
    assert "cancel_event=listener_cancel" in provider_settings
    assert "window.open" in provider_settings
    assert "Open xAI Login" in provider_settings
    assert "If the page did not open automatically" in provider_settings
    assert "Callback URL or authorization code" in provider_settings
    assert "Paste the xAI callback URL or authorization code" in provider_settings
    assert "Connect with pasted code" in provider_settings
    assert "run_xai_oauth_runtime_probe" in provider_settings
    assert "run_xai_oauth_vision_probe" in provider_settings
    assert "_queue_xai_oauth_vision_probe_if_needed" in provider_settings
    assert "image_search" not in provider_settings
    assert "Test xAI Grok vision" not in provider_settings
    assert "vision ok" not in provider_settings
    assert "vision failed" not in provider_settings
    assert "disconnect_xai_oauth_metadata" in provider_settings
    assert "xAI account fingerprint" in provider_settings
    assert "Use xAI Grok" in setup_wizard
    assert "setup_xai_oauth_client_id" in setup_wizard
    assert "ROW_BOT_XAI_OAUTH_CLIENT_ID_ENV" not in setup_wizard
    assert "xAI OAuth Client ID override (optional)" in setup_wizard
    assert "setup_xai_key" in setup_wizard
    assert "validate_xai_key" in setup_wizard
    assert "wait_for_xai_oauth_loopback_authorization" in setup_wizard
    assert "open_browser=False" in setup_wizard
    assert "cancel_event=listener_cancel" in setup_wizard
    assert "window.open" in setup_wizard
    assert "Open xAI Login" in setup_wizard
    assert "If the page did not open automatically" in setup_wizard
    assert "Callback URL or authorization code" in setup_wizard
    assert "Paste the xAI callback URL or authorization code" in setup_wizard
    assert "Connect with pasted code" in setup_wizard
    assert "xai_oauth_runtime_available" in setup_wizard
    assert "seed_recommended_xai_oauth_quick_choices" in setup_wizard
    assert "xai_oauth" not in x_tool
    assert _xai_oauth_action_state({
        "provider_id": "xai_oauth",
        "configured": False,
        "runtime_enabled": False,
        "oauth_client_id_configured": True,
        "token_health": "missing",
    })["can_connect"] is True
    assert _xai_oauth_action_state({
        "provider_id": "xai_oauth",
        "configured": True,
        "runtime_enabled": False,
        "oauth_client_id_configured": True,
        "source": AuthMethod.OAUTH_PKCE.value,
        "token_health": "expired",
    })["can_connect"] is True


def test_xai_oauth_disconnect_removes_oauth_metadata_only(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    _set_backend_for_tests(_MemoryKeyring())
    try:
        save_xai_oauth_tokens(XAIOAuthTokenSet(
            access_token=_valid_token(),
            refresh_token="refresh-secret",
        ))
        disconnect_xai_oauth_metadata()

        assert "xai_oauth" not in provider_config.load_provider_config().get("providers", {})
        assert get_provider_secret("xai_oauth", "access_token") == ""
        assert get_provider_secret("xai_oauth", "refresh_token") == ""
    finally:
        _set_backend_for_tests(None)
