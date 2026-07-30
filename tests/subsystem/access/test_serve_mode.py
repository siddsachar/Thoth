from __future__ import annotations

import argparse
import ast
from pathlib import Path

import pytest

from row_bot.access.cli import (
    build_remote_access_parser,
    legacy_serve_requested,
    legacy_serve_warning,
    resolve_serve_options,
    serve_startup_lines,
)
from row_bot.access.config import AccessConfig, DeploymentMode
from row_bot.access.request_context import (
    AuthenticationKind,
    RequestContextResolver,
)
from row_bot.runtime_paths import app_path, app_root


def _serve_args(*arguments: str) -> argparse.Namespace:
    return build_remote_access_parser().parse_args(("serve", *arguments))


def test_startup_does_not_automatically_fetch_network_model_catalogs() -> None:
    source = Path("src/row_bot/app.py").read_text(encoding="utf-8")
    module = ast.parse(source)
    startup = next(
        node
        for node in module.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_run_startup_sequence"
    )
    names = {
        node.id
        for node in ast.walk(startup)
        if isinstance(node, ast.Name)
    }

    assert "fetch_context_catalog" not in names
    assert "schedule_model_catalog_refresh_jobs" not in names


def test_agent_graph_prewarm_treats_unconfigured_provider_as_a_skip() -> None:
    source = Path("src/row_bot/app.py").read_text(encoding="utf-8")
    module = ast.parse(source)
    prewarm = next(
        node
        for node in module.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_prewarm_agent_graph_background"
    )
    handled = {
        handler.type.id
        for node in ast.walk(prewarm)
        if isinstance(node, ast.Try)
        for handler in node.handlers
        if isinstance(handler.type, ast.Name)
    }

    assert "AgentCompatibilityError" in handled


def test_server_mode_skips_agent_graph_provider_prewarm() -> None:
    source = Path("src/row_bot/app.py").read_text(encoding="utf-8")
    module = ast.parse(source)
    schedule = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_schedule_agent_graph_prewarm"
    )
    function_source = ast.unparse(schedule)

    assert "ROW_BOT_DEPLOYMENT_MODE" in function_source
    assert "server_mode" in function_source
    assert function_source.index("server_mode") < function_source.index(
        "_schedule_background_task"
    )


def test_serve_defaults_are_safe_and_headless(tmp_path) -> None:
    options = resolve_serve_options(
        _serve_args(),
        environ={"ROW_BOT_DATA_DIR": str(tmp_path)},
        durable={},
    )

    assert options.deployment_mode is DeploymentMode.SERVER
    assert options.host == "127.0.0.1"
    assert options.port == 8080
    assert options.public_url is None
    assert options.data_dir == tmp_path
    assert options.auto_start_ollama is False
    assert options.workers == 1
    assert options.open_browser is False
    assert options.tray is False
    assert options.splash is False
    environment = options.to_environment()
    assert environment["ROW_BOT_DEPLOYMENT_MODE"] == "server"
    assert environment["ROW_BOT_AUTO_START_OLLAMA"] == "0"
    assert environment["ROW_BOT_NO_OPEN"] == "1"
    assert environment["ROW_BOT_DISABLE_TRAY"] == "1"
    assert environment["ROW_BOT_DISABLE_SPLASH"] == "1"


def test_serve_cli_precedes_environment_then_durable_config(tmp_path) -> None:
    options = resolve_serve_options(
        _serve_args(
            "--host",
            "127.0.0.3",
            "--port",
            "9003",
            "--public-url",
            "https://cli.example",
            "--allowed-host",
            "cli.example",
            "--trusted-proxy",
            "127.0.0.0/8",
            "--data-dir",
            str(tmp_path / "cli"),
            "--auto-start-ollama",
        ),
        environ={
            "ROW_BOT_HOST": "127.0.0.2",
            "ROW_BOT_PORT": "9002",
            "ROW_BOT_PUBLIC_URL": "https://env.example",
            "ROW_BOT_ALLOWED_HOSTS": "env.example",
            "ROW_BOT_TRUSTED_PROXY_CIDRS": "10.0.0.0/8",
            "ROW_BOT_DATA_DIR": str(tmp_path / "env"),
            "ROW_BOT_AUTO_START_OLLAMA": "0",
        },
        durable={
            "host": "127.0.0.1",
            "port": 9001,
            "public_url": "https://durable.example",
            "allowed_hosts": ("durable.example",),
            "trusted_proxy_cidrs": ("192.168.0.0/16",),
            "data_dir": str(tmp_path / "durable"),
            "auto_start_ollama": False,
        },
    )

    assert options.host == "127.0.0.3"
    assert options.port == 9003
    assert options.public_url == "https://cli.example"
    assert options.allowed_hosts == ("cli.example",)
    assert options.trusted_proxy_cidrs == ("127.0.0.0/8",)
    assert options.data_dir == tmp_path / "cli"
    assert options.auto_start_ollama is True


def test_environment_precedes_durable_config(tmp_path) -> None:
    options = resolve_serve_options(
        _serve_args(),
        environ={
            "ROW_BOT_HOST": "127.0.0.2",
            "ROW_BOT_PORT": "9002",
            "ROW_BOT_DATA_DIR": str(tmp_path / "env"),
        },
        durable={
            "host": "127.0.0.1",
            "port": 9001,
            "data_dir": str(tmp_path / "durable"),
        },
    )

    assert options.host == "127.0.0.2"
    assert options.port == 9002
    assert options.data_dir == tmp_path / "env"


@pytest.mark.parametrize("workers", ["0", "2", "4"])
def test_serve_rejects_multiple_or_zero_workers(workers, tmp_path) -> None:
    with pytest.raises(ValueError, match="exactly one worker"):
        resolve_serve_options(
            _serve_args("--workers", workers),
            environ={"ROW_BOT_DATA_DIR": str(tmp_path)},
        )


def test_server_loopback_requires_auth_while_desktop_loopback_is_owner() -> None:
    scope = {
        "type": "http",
        "scheme": "http",
        "method": "GET",
        "path": "/",
        "headers": [(b"host", b"localhost:8080")],
        "client": ("127.0.0.1", 50100),
    }
    server = RequestContextResolver(
        AccessConfig.build(
            deployment_mode="server",
            allowed_hosts=("localhost",),
        )
    ).resolve(scope)
    desktop = RequestContextResolver(
        AccessConfig.build(
            deployment_mode="desktop",
            allowed_hosts=("localhost",),
        )
    ).resolve(scope)

    assert server.authentication_kind is AuthenticationKind.UNAUTHENTICATED
    assert desktop.authentication_kind is AuthenticationKind.LOCAL_OWNER


def test_startup_guidance_contains_recovery_but_no_invitation_secret(tmp_path) -> None:
    options = resolve_serve_options(
        _serve_args("--public-url", "https://row-bot.example"),
        environ={"ROW_BOT_DATA_DIR": str(tmp_path)},
    )

    output = "\n".join(serve_startup_lines(options))

    assert "row-bot access invite --layout desktop" in output
    assert "Health: /healthz" in output
    assert "rbi_" not in output
    assert "token" not in output.lower()
    assert "password" not in output.lower()


def test_legacy_server_flags_have_explicit_deprecation_contract(tmp_path) -> None:
    legacy_args = argparse.Namespace(
        server=True,
        no_open=True,
        host=None,
        port=None,
        public_url=None,
        allowed_hosts=None,
        trusted_proxy_cidrs=None,
        data_dir=str(tmp_path),
        auto_start_ollama=None,
        workers=None,
    )

    assert legacy_serve_requested(legacy_args) is True
    warning = legacy_serve_warning()
    assert "--server --no-open" in warning
    assert "row-bot serve" in warning
    options = resolve_serve_options(
        legacy_args,
        environ={},
        legacy_compatibility=True,
    )
    assert options.legacy_compatibility is True
    assert options.open_browser is False


def test_installed_runtime_payload_root_can_be_explicit(
    tmp_path,
    monkeypatch,
) -> None:
    payload = tmp_path / "runtime payload"
    monkeypatch.setenv("ROW_BOT_APP_ROOT", str(payload))

    assert app_root() == payload.resolve()
    assert app_path("app.py") == payload.resolve() / "app.py"
