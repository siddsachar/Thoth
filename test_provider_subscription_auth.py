import json
import base64

import providers.config as provider_config
from providers.auth_store import get_provider_secret, set_provider_secret
from providers.codex import (
    CODEX_OAUTH_CLIENT_ID,
    CodexDeviceAuthorization,
    CodexDeviceFlow,
    CodexTokenSet,
    codex_token_metadata,
    disconnect_codex_metadata,
    discover_codex_credentials,
    exchange_codex_device_authorization,
    external_reference_metadata,
    fallback_codex_model_infos,
    fetch_codex_model_infos,
    list_codex_model_infos,
    poll_codex_device_authorization,
    refresh_codex_token,
    save_external_reference,
    save_codex_oauth_tokens,
    seed_recommended_codex_quick_choices,
    start_codex_device_flow,
    summarize_auth_json,
)
from providers.models import AuthMethod, TransportMode
from providers.selection import add_quick_choice_for_model, list_quick_model_ids
from secret_store import _set_backend_for_tests


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
    def __init__(self, status_code=200, payload=None, text="", body=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text or body
        self.body = body

    def json(self):
        return self._payload

    def iter_lines(self):
        for line in self.body.splitlines():
            yield line


class _HttpClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError("No fake response queued")
        return self.responses.pop(0)

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if not self.responses:
            raise AssertionError("No fake response queued")
        return self.responses.pop(0)

    def close(self):
        self.closed = True


def _jwt(claims):
    def b64(data):
        return base64.urlsafe_b64encode(json.dumps(data).encode("utf-8")).rstrip(b"=").decode("ascii")

    return f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64(claims)}.sig"


def _sse(*events):
    chunks = []
    for event in events:
        chunks.append(f"event: {event['type']}\n")
        chunks.append(f"data: {json.dumps(event)}\n\n")
    return "".join(chunks)


def _assistant_message(text):
    return {
        "type": "response.output_item.done",
        "item": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        },
    }


def _completed(response_id="resp_1"):
    return {
        "type": "response.completed",
        "response": {
            "id": response_id,
            "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
        },
    }


def _save_runtime_tokens():
    access_token = _jwt({
        "exp": 1893456000,
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "acct-runtime",
            "chatgpt_plan_type": "plus",
        },
    })
    save_codex_oauth_tokens(CodexTokenSet(
        access_token=access_token,
        refresh_token="refresh-token",
        id_token="id-token",
    ))
    return access_token


def test_codex_auth_shape_never_returns_secret_values(tmp_path):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(json.dumps({
        "access_token": "codex-access-token-secret",
        "refresh_token": "codex-refresh-token-secret",
        "account": {"email": "person@example.test"},
        "expires_at": 1893456000,
    }), encoding="utf-8")

    summary = summarize_auth_json(auth_path)
    encoded = json.dumps(summary)

    assert summary["exists"] is True
    assert summary["key_names"] == ["access_token", "account", "expires_at", "refresh_token"]
    assert summary["key_types"]["account"] == "object"
    assert summary["key_types"]["expires_at"] == "int"
    assert summary["sensitive_key_names"] == ["access_token", "refresh_token"]
    assert "codex-access-token-secret" not in encoded
    assert "codex-refresh-token-secret" not in encoded
    assert "person@example.test" not in encoded


def test_codex_external_discovery_reports_shape_only(tmp_path):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(json.dumps({"id_token": "id-token-secret", "mode": "chatgpt"}), encoding="utf-8")

    discovered = discover_codex_credentials(auth_path=auth_path, binary="definitely-missing-codex")
    encoded = json.dumps(discovered)

    assert discovered["provider_id"] == "codex"
    assert discovered["source"] == "codex_cli"
    assert discovered["exists"] is True
    assert discovered["cli_installed"] is False
    assert discovered["auth_key_names"] == ["id_token", "mode"]
    assert discovered["auth_sensitive_key_names"] == ["id_token"]
    assert "id-token-secret" not in encoded


def test_external_credentials_includes_codex_metadata_without_values(tmp_path, monkeypatch):
    import providers.external_credentials as external_credentials

    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text(json.dumps({"access_token": "secret-value"}), encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    entries = external_credentials.discover_external_credentials()
    codex = next(entry for entry in entries if entry["provider_id"] == "codex")
    encoded = json.dumps(codex)

    assert codex["exists"] is True
    assert codex["auth_key_names"] == ["access_token"]
    assert "secret-value" not in encoded


def test_codex_provider_definition_and_status_group(tmp_path, monkeypatch):
    from providers.catalog import get_provider_definition
    from providers.status import provider_status_cards

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    definition = get_provider_definition("codex")

    assert definition is not None
    assert definition.display_name == "ChatGPT / Codex"
    assert definition.risk_label == "subscription"
    assert definition.default_transport == TransportMode.OPENAI_RESPONSES
    assert AuthMethod.EXTERNAL_CLI in definition.auth_methods
    assert AuthMethod.OAUTH_DEVICE in definition.auth_methods

    monkeypatch.setattr("providers.codex.discover_codex_credentials", lambda: {
        "label": "~/.codex/auth.json",
        "path_hash": "abc123",
        "exists": False,
        "cli_installed": False,
    })
    card = next(card for card in provider_status_cards() if card["provider_id"] == "codex")
    assert card["group"] == "Subscription Accounts"
    assert card["external_reference_exists"] is False
    assert card["runtime_enabled"] is False


def test_codex_provider_ui_action_state_for_detected_and_configured_login():
    from ui.provider_settings import _codex_action_state, _source_label

    detected = {
        "provider_id": "codex",
        "configured": False,
        "source": "external_cli_detected",
        "external_reference_exists": True,
        "runtime_enabled": False,
    }
    configured = {
        "provider_id": "codex",
        "configured": True,
        "source": "external_cli",
        "external_reference_exists": True,
        "runtime_enabled": False,
    }
    oauth = {
        "provider_id": "codex",
        "configured": True,
        "source": "oauth_device",
        "external_reference_exists": False,
        "runtime_enabled": False,
    }

    assert _codex_action_state(detected) == {
        "can_connect": True,
        "can_reference": True,
        "can_disconnect": False,
        "runtime_enabled": False,
    }
    assert _codex_action_state(configured) == {
        "can_connect": True,
        "can_reference": False,
        "can_disconnect": True,
        "runtime_enabled": False,
    }
    assert _codex_action_state(oauth) == {
        "can_connect": False,
        "can_reference": False,
        "can_disconnect": True,
        "runtime_enabled": False,
    }
    assert _source_label("oauth_device") == "Signed in with ChatGPT"


def test_codex_auth_file_detection_is_not_implicit_configuration(tmp_path, monkeypatch):
    import providers.runtime as runtime

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr("providers.codex.discover_codex_credentials", lambda: {
        "label": "~/.codex/auth.json",
        "path_hash": "abc123",
        "exists": True,
        "cli_installed": True,
    })
    _set_backend_for_tests(_MemoryKeyring())
    try:
        status = runtime.provider_status("codex")

        assert status["configured"] is False
        assert status["source"] == "external_cli_detected"
        assert status["external_reference_exists"] is True
        assert status["runtime_enabled"] is False
    finally:
        _set_backend_for_tests(None)


def test_codex_device_flow_start_and_poll_request_shapes():
    client = _HttpClient([
        _Response(200, {"device_auth_id": "dev-123", "user_code": "CODE-123", "interval": "0"}),
        _Response(404, {"error": "authorization_pending"}),
        _Response(200, {
            "authorization_code": "auth-code",
            "code_verifier": "verifier",
            "code_challenge": "challenge",
        }),
    ])

    flow = start_codex_device_flow(http_client=client, issuer="https://auth.openai.test")
    pending = poll_codex_device_authorization(flow, http_client=client)
    authorization = poll_codex_device_authorization(flow, http_client=client)

    assert flow.verification_uri == "https://auth.openai.test/codex/device"
    assert flow.user_code == "CODE-123"
    assert flow.device_auth_id == "dev-123"
    assert flow.interval_seconds == 0
    assert flow.prompt.user_code == "CODE-123"
    assert pending is None
    assert authorization == CodexDeviceAuthorization(
        authorization_code="auth-code",
        code_verifier="verifier",
        code_challenge="challenge",
        issuer="https://auth.openai.test",
        client_id=CODEX_OAUTH_CLIENT_ID,
    )
    assert client.calls[0] == (
        "https://auth.openai.test/api/accounts/deviceauth/usercode",
        {"json": {"client_id": CODEX_OAUTH_CLIENT_ID}, "headers": {"Content-Type": "application/json"}},
    )
    assert client.calls[1] == (
        "https://auth.openai.test/api/accounts/deviceauth/token",
        {"json": {"device_auth_id": "dev-123", "user_code": "CODE-123"}, "headers": {"Content-Type": "application/json"}},
    )


def test_codex_device_exchange_refresh_and_metadata_are_secret_safe():
    access_token = _jwt({
        "exp": 1893456000,
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "acct-123",
            "chatgpt_plan_type": "plus",
        },
    })
    exchanged = _HttpClient([_Response(200, {
        "id_token": "id-token-secret",
        "access_token": access_token,
        "refresh_token": "refresh-token-secret",
    })])

    token_set = exchange_codex_device_authorization(
        CodexDeviceAuthorization(
            authorization_code="auth-code",
            code_verifier="verifier",
            issuer="https://auth.openai.test",
        ),
        http_client=exchanged,
    )
    refreshed = refresh_codex_token(
        "refresh-token-secret",
        http_client=_HttpClient([_Response(200, {"access_token": access_token})]),
    )

    metadata = codex_token_metadata(access_token)
    encoded_metadata = json.dumps(metadata)

    assert token_set.access_token == access_token
    assert token_set.refresh_token == "refresh-token-secret"
    assert token_set.account_id == "acct-123"
    assert token_set.plan_type == "plus"
    assert token_set.expires_at.startswith("2030-01-01T00:00:00")
    assert refreshed.refresh_token == "refresh-token-secret"
    assert metadata["account_id_hash"] == "****-123"
    assert "refresh-token-secret" not in encoded_metadata
    assert exchanged.calls[0] == (
        "https://auth.openai.test/oauth/token",
        {
            "data": {
                "grant_type": "authorization_code",
                "code": "auth-code",
                "redirect_uri": "https://auth.openai.test/deviceauth/callback",
                "client_id": CODEX_OAUTH_CLIENT_ID,
                "code_verifier": "verifier",
            },
            "headers": {"Content-Type": "application/x-www-form-urlencoded"},
        },
    )


def test_codex_oauth_token_save_uses_keyring_and_oauth_device_status(tmp_path, monkeypatch):
    import providers.runtime as runtime

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr("providers.codex.discover_codex_credentials", lambda: {
        "label": "~/.codex/auth.json",
        "path_hash": "abc123",
        "exists": False,
        "cli_installed": False,
    })
    backend = _MemoryKeyring()
    _set_backend_for_tests(backend)
    access_token = _jwt({
        "exp": 1893456000,
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "acct-123",
            "chatgpt_plan_type": "pro",
        },
    })
    try:
        saved = save_codex_oauth_tokens(CodexTokenSet(
            access_token=access_token,
            refresh_token="refresh-token-secret",
            id_token="id-token-secret",
        ))
        status = runtime.provider_status("codex")
        encoded_saved = json.dumps(saved)

        assert saved["configured"] is True
        assert saved["auth_method"] == AuthMethod.OAUTH_DEVICE.value
        assert saved["source"] == AuthMethod.OAUTH_DEVICE.value
        assert saved["account_id_hash"] == "****-123"
        assert saved["plan_type"] == "pro"
        assert status["configured"] is True
        assert status["source"] == AuthMethod.OAUTH_DEVICE.value
        assert status["auth_method"] == AuthMethod.OAUTH_DEVICE.value
        assert status["account_id_hash"] == "****-123"
        assert status["plan_type"] == "pro"
        assert get_provider_secret("codex", "access_token") == access_token
        assert get_provider_secret("codex", "refresh_token") == "refresh-token-secret"
        assert "refresh-token-secret" not in encoded_saved
        assert access_token not in encoded_saved
    finally:
        _set_backend_for_tests(None)


def test_codex_explicit_external_reference_configures_status(tmp_path, monkeypatch):
    import providers.runtime as runtime

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(json.dumps({"access_token": "secret-value"}), encoding="utf-8")
    reference = external_reference_metadata(auth_path)
    provider_config.save_provider_config({"providers": {"codex": reference}})
    monkeypatch.setattr("providers.codex.discover_codex_credentials", lambda: {
        "label": "~/.codex/auth.json",
        "path_hash": "abc123",
        "exists": False,
        "cli_installed": False,
    })

    status = runtime.provider_status("codex")

    assert status["configured"] is True
    assert status["source"] == "external_cli"
    assert status["external_reference_label"] == str(auth_path)
    assert status["runtime_enabled"] is False


def test_codex_keyring_token_configures_status_without_leaking_value(tmp_path, monkeypatch):
    import providers.runtime as runtime

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr("providers.codex.discover_codex_credentials", lambda: {
        "label": "~/.codex/auth.json",
        "path_hash": "abc123",
        "exists": False,
        "cli_installed": False,
    })
    backend = _MemoryKeyring()
    _set_backend_for_tests(backend)
    try:
        set_provider_secret("codex", "access_token", "codex-access-token-secret", auth_method=AuthMethod.OAUTH_DEVICE)

        status = runtime.provider_status("codex")
        configured = runtime.list_configured_provider_ids()
        encoded = json.dumps(status)

        assert status["configured"] is True
        assert status["source"] == "keyring"
        assert status["fingerprint"] == "****cret"
        assert "codex" in configured
        assert "codex-access-token-secret" not in encoded
    finally:
        _set_backend_for_tests(None)


def test_codex_runtime_is_not_accidentally_routed_to_api_key_provider(tmp_path, monkeypatch):
    import providers.runtime as runtime

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    _set_backend_for_tests(_MemoryKeyring())
    try:
        try:
            runtime.create_chat_model("gpt-5.4", provider_id="codex")
        except ValueError as exc:
            assert "in-app ChatGPT login" in str(exc)
        else:
            raise AssertionError("Expected Codex runtime to require in-app OAuth tokens")
    finally:
        _set_backend_for_tests(None)


def test_codex_save_external_reference_persists_metadata_only(tmp_path, monkeypatch):
    import providers.runtime as runtime

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(json.dumps({"access_token": "secret-value", "mode": "chatgpt"}), encoding="utf-8")

    saved = save_external_reference(auth_path)
    status = runtime.provider_status("codex")
    encoded = json.dumps(saved)

    assert saved["configured"] is True
    assert saved["auth_method"] == AuthMethod.EXTERNAL_CLI.value
    assert saved["source"] == "external_cli"
    assert saved["external_reference_label"] == str(auth_path)
    assert saved["auth_key_names"] == ["access_token", "mode"]
    assert status["configured"] is True
    assert status["source"] == "external_cli"
    assert "secret-value" not in encoded


def test_codex_save_missing_external_reference_is_not_configured(tmp_path, monkeypatch):
    import providers.runtime as runtime

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    missing_path = tmp_path / "missing-auth.json"
    _set_backend_for_tests(_MemoryKeyring())
    try:
        saved = save_external_reference(missing_path)
        status = runtime.provider_status("codex")

        assert saved["configured"] is False
        assert saved["health"] == "missing_auth"
        assert status["configured"] is False
        assert status["source"] == "external_cli"
    finally:
        _set_backend_for_tests(None)


def test_codex_disconnect_removes_thoth_metadata_and_owned_tokens(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    backend = _MemoryKeyring()
    _set_backend_for_tests(backend)
    try:
        auth_path = tmp_path / "auth.json"
        auth_path.write_text(json.dumps({"access_token": "external-secret"}), encoding="utf-8")
        save_external_reference(auth_path)
        set_provider_secret("codex", "access_token", "thoth-owned-token", auth_method=AuthMethod.OAUTH_DEVICE)

        disconnect_codex_metadata()

        assert "codex" not in provider_config.load_provider_config()["providers"]
        assert get_provider_secret("codex", "access_token") == ""
        assert auth_path.exists()
    finally:
        _set_backend_for_tests(None)


def test_codex_live_model_catalog_filters_hidden_and_preserves_metadata():
    client = _HttpClient([_Response(200, {
        "models": [
            {
                "slug": "gpt-5.5",
                "display_name": "GPT-5.5",
                "context_window": 400000,
                "input_modalities": ["text", "image"],
                "supported_reasoning_efforts": ["low", "medium", "high"],
            },
            {"slug": "codex-auto-review", "visibility": "internal"},
            {"slug": "codex-hidden-review", "visibility": "hide"},
            {"slug": "hidden-model", "hidden": True},
            {"slug": "unsupported-model", "supported_in_api": False},
            {"slug": "gpt-5.4-mini", "displayName": "GPT-5.4 Mini"},
        ],
    })])

    infos = fetch_codex_model_infos(access_token="access-token", account_id="acct-123", http_client=client)
    by_id = {info.model_id: info for info in infos}

    assert set(by_id) == {"gpt-5.5", "gpt-5.4-mini"}
    assert by_id["gpt-5.5"].display_name == "GPT-5.5"
    assert by_id["gpt-5.5"].context_window == 400000
    assert "image" in by_id["gpt-5.5"].input_modalities
    assert "vision" in by_id["gpt-5.5"].capabilities
    assert "reasoning" in by_id["gpt-5.5"].capabilities
    assert by_id["gpt-5.5"].source_confidence == "live_chatgpt_codex_catalog"
    assert client.calls[0][0].endswith("/backend-api/codex/models?client_version=1.0.0")
    assert client.calls[0][1]["headers"]["Authorization"] == "Bearer access-token"
    assert client.calls[0][1]["headers"]["ChatGPT-Account-ID"] == "acct-123"


def test_codex_fallback_model_infos_use_documented_subscription_catalog(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")

    infos = fallback_codex_model_infos()
    by_id = {info.model_id: info for info in infos}

    assert set(by_id) == {"gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex", "gpt-5.3-codex-spark", "gpt-5.2"}
    assert "codex-auto-review" not in by_id
    assert all(info.provider_id == "codex" for info in infos)
    assert by_id["gpt-5.5"].selection_ref == "model:codex:gpt-5.5"
    assert by_id["gpt-5.2"].selection_ref == "model:codex:gpt-5.2"
    assert "image" in by_id["gpt-5.5"].input_modalities
    assert "vision" in by_id["gpt-5.5"].capabilities
    assert all(info.transport == TransportMode.OPENAI_RESPONSES for info in infos)
    assert all(info.risk_label == "subscription" for info in infos)
    assert all(info.capability_snapshot()["tasks"] == ["responses"] for info in infos)


def test_codex_model_infos_cache_live_catalog_and_fall_back_to_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    _set_backend_for_tests(_MemoryKeyring())
    access_token = _jwt({
        "exp": 1893456000,
        "https://api.openai.com/auth": {"chatgpt_account_id": "acct-123"},
    })
    try:
        save_codex_oauth_tokens(CodexTokenSet(access_token=access_token, refresh_token="refresh-token", id_token="id-token"))
        live_client = _HttpClient([_Response(200, {
            "models": [{
                "slug": "gpt-live",
                "display_name": "GPT Live",
                "supported_reasoning_efforts": ["medium"],
            }],
        })])

        live_infos = list_codex_model_infos(force_refresh=True, http_client=live_client)
        cached_infos = list_codex_model_infos()
    finally:
        _set_backend_for_tests(None)

    assert [info.model_id for info in live_infos] == ["gpt-live"]
    assert [info.model_id for info in cached_infos] == ["gpt-live"]
    assert "reasoning" in cached_infos[0].capabilities


def test_codex_quick_choice_seed_only_adds_recommended_model(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr("providers.runtime.provider_status", lambda provider_id: {
        "configured": True,
        "runtime_enabled": True,
    })

    quick = seed_recommended_codex_quick_choices(max_choices=5)
    codex_refs = [
        choice.get("id")
        for choice in quick
        if isinstance(choice, dict) and choice.get("provider_id") == "codex"
    ]

    assert codex_refs == ["model:codex:gpt-5.5"]


def test_codex_quick_choice_seed_requires_configured_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    _set_backend_for_tests(_MemoryKeyring())
    try:
        assert seed_recommended_codex_quick_choices() == []

        auth_path = tmp_path / "auth.json"
        auth_path.write_text(json.dumps({"access_token": "external-secret"}), encoding="utf-8")
        save_external_reference(auth_path)
        quick = seed_recommended_codex_quick_choices()
    finally:
        _set_backend_for_tests(None)

    assert quick == []


def test_codex_quick_choices_are_hidden_until_runtime_enabled(tmp_path, monkeypatch):
    import api_keys

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr(api_keys, "get_cloud_config", lambda: {"starred_models": []})
    monkeypatch.setattr("providers.runtime.provider_status", lambda provider_id: {
        "configured": True,
        "runtime_enabled": False,
    })

    add_quick_choice_for_model(
        "gpt-5.4",
        provider_id="codex",
        display_name="GPT-5.4 (Codex)",
        capabilities_snapshot=list_codex_model_infos()[0].capability_snapshot(),
    )

    assert list_quick_model_ids("chat") == []


def test_codex_runtime_builds_official_responses_request_shape(tmp_path, monkeypatch):
    from langchain_core.messages import HumanMessage, SystemMessage
    from providers.transports.codex_responses import ChatCodexResponses

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    _set_backend_for_tests(_MemoryKeyring())
    access_token = _save_runtime_tokens()
    client = _HttpClient([_Response(body=_sse(_assistant_message("Hello from Codex"), _completed()))])
    model = ChatCodexResponses(
        model_name="gpt-5.4",
        base_url="https://chatgpt.test/backend-api/codex",
        session_id="session-123",
        installation_id="install-123",
        http_client=client,
    )
    try:
        result = model.invoke([SystemMessage(content="Be brief."), HumanMessage(content="hello")])
    finally:
        _set_backend_for_tests(None)

    assert result.content == "Hello from Codex"
    assert client.calls[0][0] == "https://chatgpt.test/backend-api/codex/responses"
    headers = client.calls[0][1]["headers"]
    body = client.calls[0][1]["json"]
    assert headers["Authorization"] == f"Bearer {access_token}"
    assert headers["ChatGPT-Account-ID"] == "acct-runtime"
    assert headers["originator"] == "codex_cli_rs"
    assert headers["session_id"] == "session-123"
    assert headers["Accept"] == "text/event-stream"
    assert body["model"] == "gpt-5.4"
    assert body["instructions"] == "Be brief."
    assert body["input"] == [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello"}]}]
    assert body["stream"] is True
    assert body["store"] is False
    assert body["prompt_cache_key"] == "session-123"
    assert body["client_metadata"] == {"x-codex-installation-id": "install-123"}


def test_codex_runtime_streams_text_deltas(tmp_path, monkeypatch):
    from langchain_core.messages import HumanMessage
    from providers.transports.codex_responses import ChatCodexResponses

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    _set_backend_for_tests(_MemoryKeyring())
    _save_runtime_tokens()
    client = _HttpClient([_Response(body=_sse(
        {"type": "response.output_text.delta", "delta": "Hel"},
        {"type": "response.output_text.delta", "delta": "lo"},
        _completed(),
    ))])
    model = ChatCodexResponses(model_name="gpt-5.4", http_client=client)
    try:
        chunks = list(model._stream([HumanMessage(content="hello")]))
    finally:
        _set_backend_for_tests(None)

    assert [chunk.message.content for chunk in chunks] == ["Hel", "lo", ""]
    assert chunks[-1].message.chunk_position == "last"


def test_codex_runtime_streams_final_assistant_message_without_deltas(tmp_path, monkeypatch):
    from langchain_core.messages import HumanMessage
    from providers.transports.codex_responses import ChatCodexResponses

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    _set_backend_for_tests(_MemoryKeyring())
    _save_runtime_tokens()
    client = _HttpClient([_Response(body=_sse(_assistant_message("Final text"), _completed()))])
    model = ChatCodexResponses(model_name="gpt-5.4", http_client=client)
    try:
        chunks = list(model._stream([HumanMessage(content="hello")]))
    finally:
        _set_backend_for_tests(None)

    assert [chunk.message.content for chunk in chunks] == ["Final text", ""]
    assert chunks[-1].message.chunk_position == "last"


def test_codex_runtime_streams_function_call_chunks(tmp_path, monkeypatch):
    from langchain_core.messages import HumanMessage
    from providers.transports.codex_responses import ChatCodexResponses

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    _set_backend_for_tests(_MemoryKeyring())
    _save_runtime_tokens()
    client = _HttpClient([_Response(body=_sse(
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "name": "lookup_order",
                "call_id": "call-1",
                "arguments": "{\"order_id\":\"42\"}",
            },
        },
        _completed(),
    ))])
    model = ChatCodexResponses(model_name="gpt-5.4", http_client=client)
    try:
        chunks = list(model._stream([HumanMessage(content="lookup")]))
    finally:
        _set_backend_for_tests(None)

    tool_chunks = [chunk.message for chunk in chunks if chunk.message.tool_call_chunks]
    assert len(tool_chunks) == 1
    assert tool_chunks[0].content == ""
    assert tool_chunks[0].tool_call_chunks == [{
        "name": "lookup_order",
        "args": "{\"order_id\":\"42\"}",
        "id": "call-1",
        "index": 0,
        "type": "tool_call_chunk",
    }]
    assert tool_chunks[0].tool_calls == [{"name": "lookup_order", "args": {"order_id": "42"}, "id": "call-1", "type": "tool_call"}]
    assert chunks[-1].message.chunk_position == "last"


def test_codex_runtime_maps_function_call_items(tmp_path, monkeypatch):
    from langchain_core.messages import HumanMessage
    from providers.transports.codex_responses import ChatCodexResponses

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    _set_backend_for_tests(_MemoryKeyring())
    _save_runtime_tokens()
    client = _HttpClient([_Response(body=_sse(
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "name": "lookup_order",
                "call_id": "call-1",
                "arguments": "{\"order_id\":\"42\"}",
            },
        },
        _completed(),
    ))])
    model = ChatCodexResponses(model_name="gpt-5.4", http_client=client)
    try:
        result = model.invoke([HumanMessage(content="lookup")])
    finally:
        _set_backend_for_tests(None)

    assert result.tool_calls == [{"name": "lookup_order", "args": {"order_id": "42"}, "id": "call-1", "type": "tool_call"}]


def test_codex_runtime_replays_tool_calls_before_tool_outputs(tmp_path, monkeypatch):
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from providers.transports.codex_responses import ChatCodexResponses

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    _set_backend_for_tests(_MemoryKeyring())
    _save_runtime_tokens()
    client = _HttpClient([_Response(body=_sse(_assistant_message("done"), _completed()))])
    model = ChatCodexResponses(model_name="gpt-5.4", http_client=client)
    messages = [
        HumanMessage(content="look this up"),
        AIMessage(content="", tool_calls=[{
            "name": "lookup_order",
            "args": {"order_id": "42"},
            "id": "call_48651121",
            "type": "tool_call",
        }]),
        ToolMessage(content="Order 42 shipped.", name="lookup_order", tool_call_id="call_48651121"),
    ]
    try:
        result = model.invoke(messages)
    finally:
        _set_backend_for_tests(None)

    assert result.content == "done"
    body = client.calls[0][1]["json"]
    assert body["input"] == [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "look this up"}]},
        {
            "type": "function_call",
            "call_id": "call_48651121",
            "name": "lookup_order",
            "arguments": "{\"order_id\":\"42\"}",
        },
        {"type": "function_call_output", "call_id": "call_48651121", "output": "Order 42 shipped."},
    ]


def test_codex_runtime_refreshes_once_after_401(tmp_path, monkeypatch):
    from langchain_core.messages import HumanMessage
    import providers.transports.codex_responses as codex_transport
    from providers.transports.codex_responses import ChatCodexResponses

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    _set_backend_for_tests(_MemoryKeyring())
    old_access_token = _save_runtime_tokens()
    new_access_token = _jwt({
        "exp": 1893457000,
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "acct-runtime",
            "chatgpt_plan_type": "plus",
        },
    })
    monkeypatch.setattr(
        codex_transport.codex_auth,
        "refresh_codex_token",
        lambda refresh_token: CodexTokenSet(access_token=new_access_token, refresh_token=refresh_token, id_token="id-token"),
    )
    client = _HttpClient([
        _Response(401, text="expired"),
        _Response(body=_sse(_assistant_message("after refresh"), _completed())),
    ])
    model = ChatCodexResponses(model_name="gpt-5.4", http_client=client)
    try:
        result = model.invoke([HumanMessage(content="hello")])
    finally:
        _set_backend_for_tests(None)

    assert result.content == "after refresh"
    assert client.calls[0][1]["headers"]["Authorization"] == f"Bearer {old_access_token}"
    assert client.calls[1][1]["headers"]["Authorization"] == f"Bearer {new_access_token}"


def test_runtime_status_and_factory_enable_codex_for_oauth_tokens(tmp_path, monkeypatch):
    import providers.runtime as runtime
    from providers.transports.codex_responses import ChatCodexResponses

    monkeypatch.setattr(provider_config, "CONFIG_PATH", tmp_path / "providers.json")
    monkeypatch.setattr("providers.codex.discover_codex_credentials", lambda: {
        "label": "~/.codex/auth.json",
        "path_hash": "abc123",
        "exists": False,
        "cli_installed": False,
    })
    _set_backend_for_tests(_MemoryKeyring())
    try:
        _save_runtime_tokens()
        status = runtime.provider_status("codex")
        model = runtime.create_chat_model("gpt-5.4", provider_id="codex")
    finally:
        _set_backend_for_tests(None)

    assert status["configured"] is True
    assert status["runtime_enabled"] is True
    assert isinstance(model, ChatCodexResponses)
    assert model.model_name == "gpt-5.4"
