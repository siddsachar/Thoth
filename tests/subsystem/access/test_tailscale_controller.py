from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
from typing import Sequence

import pytest

from row_bot.access.config import (
    AccessConfig,
    DeploymentMode,
    UntrustedForwardedAction,
)
from row_bot.access.access_routes import AccessRouteKind, build_route_inventory
from row_bot.access.tailscale import (
    CommandResult,
    MUTATION_TIMEOUT_SECONDS,
    OWNERSHIP_SCHEMA_VERSION,
    READ_ONLY_TIMEOUT_SECONDS,
    TailscaleOperationResult,
    TailscaleOwnership,
    TailscaleOwnershipStore,
    TailscalePlanAction,
    TailscaleServeController,
    TailscaleState,
    TailscaleStatusCache,
    augment_access_config_for_owned_tailscale,
    parse_serve_status_json,
    redact_command_detail,
)


pytestmark = pytest.mark.subsystem

WINDOWS_BINARY = r"C:\Program Files\Tailscale\tailscale.exe"
POSIX_BINARY = "/usr/local/bin/tailscale"
OTHER_BINARY = "/opt/tailscale/bin/tailscale"
DNS_NAME = "row-bot.example-tailnet.ts.net"
OTHER_DNS_NAME = "changed.example-tailnet.ts.net"
ORIGIN = f"https://{DNS_NAME}"
TARGET = "http://127.0.0.1:8080"
ENABLE = (
    POSIX_BINARY,
    "serve",
    "--bg",
    "--yes",
    "--https=443",
    TARGET,
)


def _node_status(*, backend: str = "Running", dns_name: str = DNS_NAME) -> str:
    return json.dumps(
        {
            "BackendState": backend,
            "Self": {
                "DNSName": f"{dns_name}.",
                "TailscaleIPs": ["100.64.0.10"],
            },
        }
    )


def _empty_serve(*, nonce: bool = False) -> str:
    value: dict[str, object] = {"TCP": {}, "Web": {}, "AllowFunnel": {}}
    if nonce:
        value["FutureFalseValue"] = False
    return json.dumps(value)


def _serve_config(
    *,
    target: str = TARGET,
    dns_name: str = DNS_NAME,
    path: str = "/",
    funnel: bool = False,
    extra: bool = False,
) -> str:
    handlers: dict[str, object] = {path: {"Proxy": target}}
    if extra:
        handlers["/other"] = {"Proxy": "http://127.0.0.1:9999"}
    return json.dumps(
        {
            "TCP": {"443": {"HTTPS": True}},
            "Web": {f"{dns_name}:443": {"Handlers": handlers}},
            "AllowFunnel": {f"{dns_name}:443": funnel},
        }
    )


class FakeRunner:
    def __init__(
        self,
        responses: Sequence[tuple[Sequence[str] | None, CommandResult | BaseException]],
    ) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[tuple[str, ...], float]] = []

    def __call__(self, argv: Sequence[str], timeout: float) -> CommandResult:
        command = tuple(argv)
        self.calls.append((command, timeout))
        if not self._responses:
            raise AssertionError(f"unexpected Tailscale command: {command!r}")
        expected, response = self._responses.pop(0)
        if expected is not None:
            assert command == tuple(expected)
        if isinstance(response, BaseException):
            raise response
        return response

    def assert_finished(self) -> None:
        assert self._responses == []


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class WhichSequence:
    def __init__(self, *values: str | None) -> None:
        self._values = list(values)

    def __call__(self, name: str) -> str | None:
        assert name == "tailscale"
        if len(self._values) > 1:
            return self._values.pop(0)
        return self._values[0]


def _controller(
    tmp_path: Path,
    runner: FakeRunner,
    *,
    binary: str = POSIX_BINARY,
    which: WhichSequence | None = None,
    clock: FakeClock | None = None,
) -> TailscaleServeController:
    fake_clock = clock or FakeClock()
    return TailscaleServeController(
        runner=runner,
        which=which or (lambda name: binary if name == "tailscale" else None),
        read_timeout=2.5,
        mutation_timeout=29.0,
        reconciliation_timeout=1.0,
        clock=fake_clock,
        sleeper=fake_clock.sleep,
        backoff=(0.5,),
        ownership_path=tmp_path / "tailscale-ownership.json",
    )


def _ready_responses(
    binary: str = POSIX_BINARY,
    *,
    dns_name: str = DNS_NAME,
    serve: str | None = None,
) -> list[tuple[Sequence[str], CommandResult]]:
    return [
        (
            (binary, "status", "--json"),
            CommandResult(0, stdout=_node_status(dns_name=dns_name)),
        ),
        (
            (binary, "serve", "status", "--json"),
            CommandResult(0, stdout=serve or _empty_serve()),
        ),
    ]


def _active_responses(
    *,
    binary: str = POSIX_BINARY,
    dns_name: str = DNS_NAME,
    config: str | None = None,
) -> list[tuple[Sequence[str], CommandResult]]:
    return [
        (
            (binary, "status", "--json"),
            CommandResult(0, stdout=_node_status(dns_name=dns_name)),
        ),
        (
            (binary, "serve", "status", "--json"),
            CommandResult(
                0,
                stdout=config or _serve_config(dns_name=dns_name),
            ),
        ),
    ]


def _capability_responses(
    binary: str = POSIX_BINARY,
    *,
    supported: bool = True,
) -> list[tuple[Sequence[str], CommandResult]]:
    help_text = (
        "USAGE\n  tailscale serve [flags] <target>\n"
        "FLAGS\n  --bg  --yes  --https=<port>\n"
        if supported
        else "USAGE\n  tailscale serve <target>\nFLAGS\n  --bg\n"
    )
    return [
        (
            (binary, "version", "--json"),
            CommandResult(
                0,
                stdout=json.dumps(
                    {
                        "short": "1.98.9",
                        "long": "1.98.9-tabcdef",
                    }
                ),
            ),
        ),
        (
            (binary, "serve", "--help"),
            CommandResult(0, stdout=help_text),
        ),
    ]


def _plan_responses(
    binary: str = POSIX_BINARY,
) -> list[tuple[Sequence[str], CommandResult]]:
    return [*_ready_responses(binary), *_capability_responses(binary)]


def _ownership(config: str | None = None) -> TailscaleOwnership:
    parsed = parse_serve_status_json(config or _serve_config(), dns_name=DNS_NAME)
    return TailscaleOwnership(
        schema_version=OWNERSHIP_SCHEMA_VERSION,
        config_fingerprint=parsed.fingerprint,
        origin=ORIGIN,
        target=TARGET,
        path="/",
        https_port=443,
    )


def _mutations(runner: FakeRunner) -> list[tuple[str, ...]]:
    return [
        command
        for command, _timeout in runner.calls
        if len(command) >= 2
        and command[1] == "serve"
        and "status" not in command
        and "--help" not in command
    ]


def test_owned_tailscale_policy_augments_exact_matching_app_port() -> None:
    config = AccessConfig.build(
        deployment_mode=DeploymentMode.SERVER,
        trusted_proxy_cidrs=("10.0.0.0/8",),
        allowed_hosts=("localhost", "existing.example.com"),
        public_origins=("https://existing.example.com",),
        untrusted_forwarded_action=UntrustedForwardedAction.IGNORE,
    )

    augmented = augment_access_config_for_owned_tailscale(
        config,
        ownership=_ownership(),
        app_port=8080,
    )

    assert tuple(str(network) for network in augmented.trusted_proxy_cidrs) == (
        "10.0.0.0/8",
        "127.0.0.1/32",
        "::1/128",
    )
    assert augmented.allowed_hosts == (
        "localhost",
        "existing.example.com",
        DNS_NAME,
    )
    assert augmented.public_origins == (
        "https://existing.example.com",
        ORIGIN,
    )
    assert augmented.deployment_mode is config.deployment_mode
    assert augmented.untrusted_forwarded_action is config.untrusted_forwarded_action


def test_owned_tailscale_policy_deduplicates_existing_exact_values() -> None:
    config = AccessConfig.build(
        trusted_proxy_cidrs=("127.0.0.1/32", "::1/128"),
        allowed_hosts=(DNS_NAME,),
        public_origins=(ORIGIN,),
    )

    augmented = augment_access_config_for_owned_tailscale(
        config,
        ownership=_ownership(),
        app_port=8080,
    )

    assert augmented == config


@pytest.mark.parametrize(
    "ownership,app_port",
    [
        (None, 8080),
        ({"schema_version": OWNERSHIP_SCHEMA_VERSION}, 8080),
        (
            {
                **_ownership().to_dict(),
                "origin": "https://public.example.com",
            },
            8080,
        ),
        (
            {
                **_ownership().to_dict(),
                "target": "http://192.0.2.10:8080",
            },
            8080,
        ),
        (_ownership(), 9000),
        (_ownership(), 0),
        (_ownership(), True),
    ],
    ids=[
        "missing",
        "invalid-record",
        "non-tailnet-origin",
        "non-loopback-target",
        "mismatched-port",
        "invalid-port",
        "boolean-port",
    ],
)
def test_owned_tailscale_policy_returns_same_config_when_not_exactly_valid(
    ownership: TailscaleOwnership | dict[str, object] | None,
    app_port: int,
) -> None:
    config = AccessConfig.build(
        trusted_proxy_cidrs=("10.0.0.0/8",),
        allowed_hosts=("existing.example.com",),
        public_origins=("https://existing.example.com",),
    )

    unchanged = augment_access_config_for_owned_tailscale(
        config,
        ownership=ownership,
        app_port=app_port,
    )

    assert unchanged is config


def test_detect_reports_cli_not_found_without_running_a_command(tmp_path: Path) -> None:
    runner = FakeRunner([])
    controller = TailscaleServeController(
        runner=runner,
        which=lambda _name: None,
        ownership_path=tmp_path / "ownership.json",
    )

    status = controller.detect(port=8080)

    assert status.state is TailscaleState.CLI_NOT_FOUND
    assert not status.installed
    assert runner.calls == []


def test_detect_reports_signed_out_and_daemon_timeout_safely(tmp_path: Path) -> None:
    signed_out = FakeRunner(
        [
            (
                (POSIX_BINARY, "status", "--json"),
                CommandResult(0, stdout=_node_status(backend="NeedsLogin")),
            )
        ]
    )
    assert (
        _controller(tmp_path, signed_out).detect(port=8080).state
        is TailscaleState.SIGNED_OUT
    )

    timed_out = FakeRunner(
        [
            (
                (POSIX_BINARY, "status", "--json"),
                subprocess.TimeoutExpired(
                    (POSIX_BINARY, "status", "--json"),
                    2.5,
                    output="private status output",
                    stderr="https://private.example-tailnet.ts.net/debug",
                ),
            )
        ]
    )
    status = _controller(tmp_path, timed_out).detect(port=8080)
    assert status.state is TailscaleState.DAEMON_UNAVAILABLE
    assert status.detail == "Tailscale command timed out."
    assert timed_out.calls[0][1] == 2.5


def test_plan_probes_1989_capabilities_and_is_an_immutable_exact_plan(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(_plan_responses())
    controller = _controller(tmp_path, runner)

    plan = controller.plan(port=8080)

    assert plan.action is TailscalePlanAction.ENABLE
    assert plan.command == ENABLE
    assert plan.binary == POSIX_BINARY
    assert plan.dns_name == DNS_NAME
    assert plan.target == TARGET
    assert plan.port == 8080
    assert plan.https_port == 443
    assert plan.cli_version.startswith("1.98.9")
    assert plan.baseline_fingerprint == plan.status.config_fingerprint
    assert all(timeout == 2.5 for _command, timeout in runner.calls)
    assert _mutations(runner) == []
    runner.assert_finished()


def test_plan_refuses_cli_without_required_serve_flags(tmp_path: Path) -> None:
    runner = FakeRunner([*_ready_responses(), *_capability_responses(supported=False)])

    plan = _controller(tmp_path, runner).plan(port=8080)

    assert plan.action is TailscalePlanAction.UPGRADE_REQUIRED
    assert plan.status.state is TailscaleState.UNSUPPORTED_CLI
    assert plan.command == ()
    assert _mutations(runner) == []


def test_plan_refuses_incomplete_text_only_baseline(tmp_path: Path) -> None:
    runner = FakeRunner(
        [
            _ready_responses()[0],
            (
                (POSIX_BINARY, "serve", "status", "--json"),
                CommandResult(0, stdout="{not-json"),
            ),
            (
                (POSIX_BINARY, "serve", "status"),
                CommandResult(0, stdout="No Serve config"),
            ),
        ]
    )

    plan = _controller(tmp_path, runner).plan(port=8080)

    assert plan.action is TailscalePlanAction.UPGRADE_REQUIRED
    assert plan.command == ()


def test_detect_consent_conflict_funnel_and_owned_states(tmp_path: Path) -> None:
    consent = "https://login.tailscale.com/f/serve?node=private-value"
    consent_runner = FakeRunner(
        [
            _ready_responses()[0],
            (
                (POSIX_BINARY, "serve", "status", "--json"),
                CommandResult(1, stderr=f"To enable Serve, visit {consent}"),
            ),
        ]
    )
    consent_status = _controller(tmp_path, consent_runner).detect(port=8080)
    assert consent_status.state is TailscaleState.CONSENT_REQUIRED
    assert consent_status.consent_url == consent
    assert "https://" not in consent_status.detail

    conflict_runner = FakeRunner(
        _active_responses(config=_serve_config(target="http://127.0.0.1:9000"))
    )
    assert (
        _controller(tmp_path, conflict_runner).detect(port=8080).state
        is TailscaleState.ROUTE_CONFLICT
    )

    funnel_runner = FakeRunner(_active_responses(config=_serve_config(funnel=True)))
    assert (
        _controller(tmp_path, funnel_runner).detect(port=8080).state
        is TailscaleState.FUNNEL_ACTIVE
    )

    path = tmp_path / "owned.json"
    TailscaleOwnershipStore(path).save(_ownership())
    owned_runner = FakeRunner(_active_responses())
    owned = TailscaleServeController(
        runner=owned_runner,
        which=lambda _name: POSIX_BINARY,
        ownership_path=path,
    ).detect(port=8080)
    assert owned.state is TailscaleState.ACTIVE_OWNED
    assert owned.owned


def test_verified_status_survives_two_settings_constructions_without_new_probe(
    tmp_path: Path,
    monkeypatch,
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(data_dir))
    ownership_store = TailscaleOwnershipStore()
    ownership_store.save(_ownership())
    runner = FakeRunner(_active_responses())
    controller = TailscaleServeController(
        runner=runner,
        which=lambda _name: POSIX_BINARY,
    )
    verified_at = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)
    cache = TailscaleStatusCache(clock=lambda: verified_at)
    instance_key = str(ownership_store.path.resolve())

    status = controller.detect(port=8080)
    cache.remember(
        instance_key=instance_key,
        port=8080,
        status=status,
        ownership=ownership_store.load(),
    )
    probe_calls = list(runner.calls)

    def construct_settings_inventory():
        snapshot = cache.get(
            instance_key=instance_key,
            port=8080,
            ownership=ownership_store.load(),
        )
        return (
            snapshot,
            build_route_inventory(
                port=8080,
                tailscale_state=snapshot.status if snapshot else None,
            ),
        )

    first_snapshot, first_inventory = construct_settings_inventory()
    second_snapshot, second_inventory = construct_settings_inventory()

    assert first_snapshot is second_snapshot
    assert second_snapshot is not None
    assert second_snapshot.verified_at == verified_at
    assert first_inventory.by_kind(AccessRouteKind.TAILSCALE)[0].available is True
    assert second_inventory.preferred_invitation_origin() == ORIGIN
    assert runner.calls == probe_calls

    ownership_store.clear()

    invalidated_snapshot, invalidated_inventory = construct_settings_inventory()
    assert invalidated_snapshot is None
    assert invalidated_inventory.by_kind(AccessRouteKind.TAILSCALE) == ()
    assert runner.calls == probe_calls


def test_status_cache_rejects_malformed_active_status(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    ownership_store = TailscaleOwnershipStore()
    ownership_store.save(_ownership())
    runner = FakeRunner(_active_responses())
    controller = TailscaleServeController(
        runner=runner,
        which=lambda _name: POSIX_BINARY,
    )
    status = controller.detect(port=8080)
    cache = TailscaleStatusCache()
    instance_key = str(ownership_store.path.resolve())

    remembered = cache.remember(
        instance_key=instance_key,
        port=8080,
        status=replace(status, serve_url="https://malformed.example"),
        ownership=ownership_store.load(),
    )

    assert remembered is None
    assert (
        cache.get(
            instance_key=instance_key,
            port=8080,
            ownership=ownership_store.load(),
        )
        is None
    )


def test_json_and_text_status_variants_remain_read_only(tmp_path: Path) -> None:
    text_serve = (
        f"Available on your tailnet:\n{ORIGIN}/\n"
        f"|-- proxy {TARGET}\nServe started and running in background.\n"
    )
    runner = FakeRunner(
        [
            (
                (POSIX_BINARY, "status", "--json"),
                CommandResult(0, stdout="{not-json"),
            ),
            (
                (POSIX_BINARY, "status"),
                CommandResult(
                    0,
                    stdout="100.64.0.10 row-bot owner@ linux active",
                ),
            ),
            (
                (POSIX_BINARY, "serve", "status", "--json"),
                CommandResult(0, stdout="{not-json"),
            ),
            (
                (POSIX_BINARY, "serve", "status"),
                CommandResult(0, stdout=text_serve),
            ),
        ]
    )

    status = _controller(tmp_path, runner).detect(port=8080)

    assert status.state is TailscaleState.ACTIVE_UNOWNED
    assert status.serve_url == ORIGIN
    assert not status.config_complete
    assert _mutations(runner) == []
    runner.assert_finished()


def test_timeout_partial_output_exposes_consent_without_generic_url(
    tmp_path: Path,
) -> None:
    consent = "https://login.tailscale.com/f/serve?node=private-value"
    timeout = subprocess.TimeoutExpired(
        ENABLE,
        29.0,
        output=("x" * 70_000).encode(),
        stderr=f"Complete HTTPS consent at {consent}".encode(),
    )
    # Consent must be within the bounded stderr even when stdout is oversized.
    runner = FakeRunner(
        [
            *_plan_responses(),
            *_ready_responses(),
            (ENABLE, timeout),
            *_ready_responses(),
        ]
    )
    controller = _controller(tmp_path, runner)
    plan = controller.plan(port=8080)

    result = controller.apply(plan)

    assert not result.success
    assert result.status.state is TailscaleState.CONSENT_REQUIRED
    assert result.status.consent_url == consent
    assert "https://" not in result.error
    assert _mutations(runner) == [ENABLE]
    assert not (tmp_path / "tailscale-ownership.json").exists()
    runner.assert_finished()


def test_timeout_with_no_route_is_one_failed_mutation_and_bounded_backoff(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    runner = FakeRunner(
        [
            *_plan_responses(),
            *_ready_responses(),
            (ENABLE, subprocess.TimeoutExpired(ENABLE, 29.0)),
            *_ready_responses(),
            *_ready_responses(),
            *_ready_responses(),
        ]
    )
    controller = _controller(tmp_path, runner, clock=clock)
    plan = controller.plan(port=8080)

    result = controller.apply(plan)

    assert not result.success
    assert result.status.state is TailscaleState.READY
    assert _mutations(runner) == [ENABLE]
    assert clock.sleeps == [0.5, 0.5]
    assert max(clock.sleeps) <= 0.5
    assert not (tmp_path / "tailscale-ownership.json").exists()
    assert dict(runner.calls)[ENABLE] == 29.0
    runner.assert_finished()


@pytest.mark.parametrize(
    "mutation",
    [
        subprocess.TimeoutExpired(ENABLE, 29.0),
        CommandResult(1, stderr="the process exited late"),
        CommandResult(0),
    ],
    ids=["timeout", "nonzero", "zero"],
)
def test_delayed_exact_route_is_authoritative_over_process_exit(
    tmp_path: Path,
    mutation: CommandResult | BaseException,
) -> None:
    clock = FakeClock()
    path = tmp_path / "tailscale-ownership.json"
    runner = FakeRunner(
        [
            *_plan_responses(),
            *_ready_responses(),
            (ENABLE, mutation),
            *_ready_responses(),
            *_active_responses(),
        ]
    )
    controller = _controller(tmp_path, runner, clock=clock)
    plan = controller.plan(port=8080)

    result = controller.apply(plan)

    assert result.success
    assert result.status.state is TailscaleState.ACTIVE_OWNED
    assert result.ownership == TailscaleOwnershipStore(path).load()
    assert _mutations(runner) == [ENABLE]
    assert clock.sleeps == [0.5]
    runner.assert_finished()


@pytest.mark.parametrize(
    "unsafe_config, expected_state",
    [
        (_serve_config(funnel=True), TailscaleState.FUNNEL_ACTIVE),
        (
            _serve_config(target="http://127.0.0.1:9000"),
            TailscaleState.ROUTE_CONFLICT,
        ),
        (_serve_config(extra=True), TailscaleState.ACTIVE_UNOWNED),
    ],
    ids=["funnel", "conflict", "multiple-routes"],
)
def test_timeout_followed_by_unsafe_state_fails_without_cleanup(
    tmp_path: Path,
    unsafe_config: str,
    expected_state: TailscaleState,
) -> None:
    runner = FakeRunner(
        [
            *_plan_responses(),
            *_ready_responses(),
            (ENABLE, subprocess.TimeoutExpired(ENABLE, 29.0)),
            *_active_responses(config=unsafe_config),
        ]
    )
    controller = _controller(tmp_path, runner)
    plan = controller.plan(port=8080)

    result = controller.apply(plan)

    assert not result.success
    assert result.status.state is expected_state
    assert _mutations(runner) == [ENABLE]
    assert "cleanup" in result.error
    assert not (tmp_path / "tailscale-ownership.json").exists()
    runner.assert_finished()


def test_status_unavailable_through_deadline_is_explicitly_unverified(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    status_timeout = subprocess.TimeoutExpired(
        (POSIX_BINARY, "status", "--json"),
        2.5,
    )
    runner = FakeRunner(
        [
            *_plan_responses(),
            *_ready_responses(),
            (ENABLE, subprocess.TimeoutExpired(ENABLE, 29.0)),
            ((POSIX_BINARY, "status", "--json"), status_timeout),
            ((POSIX_BINARY, "status", "--json"), status_timeout),
            ((POSIX_BINARY, "status", "--json"), status_timeout),
        ]
    )
    controller = _controller(tmp_path, runner, clock=clock)
    plan = controller.plan(port=8080)

    result = controller.apply(plan)

    assert not result.success
    assert result.status.state is TailscaleState.OUTCOME_UNVERIFIED
    assert "could not be verified" in result.error
    assert _mutations(runner) == [ENABLE]
    assert clock.sleeps == [0.5, 0.5]
    runner.assert_finished()


@pytest.mark.parametrize(
    "field,value",
    [
        ("port", 9000),
        ("target", "http://127.0.0.1:9000"),
        ("binary", OTHER_BINARY),
        ("dns_name", OTHER_DNS_NAME),
        ("baseline_fingerprint", "0" * 64),
        ("https_port", 8443),
        (
            "command",
            (
                POSIX_BINARY,
                "serve",
                "--bg",
                "--yes",
                TARGET,
            ),
        ),
    ],
)
def test_apply_refuses_an_altered_immutable_plan_before_revalidation(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    runner = FakeRunner(_plan_responses())
    controller = _controller(tmp_path, runner)
    plan = controller.plan(port=8080)

    result = controller.apply(replace(plan, **{field: value}))

    assert not result.success
    assert "altered" in result.error
    assert _mutations(runner) == []
    runner.assert_finished()


@pytest.mark.parametrize(
    "change",
    ["binary", "dns", "readiness", "fingerprint"],
)
def test_apply_revalidates_identity_readiness_and_fingerprint_before_mutation(
    tmp_path: Path,
    change: str,
) -> None:
    which = WhichSequence(POSIX_BINARY, OTHER_BINARY) if change == "binary" else None
    if change == "binary":
        changed = _ready_responses(OTHER_BINARY)
    elif change == "dns":
        changed = _ready_responses(dns_name=OTHER_DNS_NAME)
    elif change == "readiness":
        changed = _active_responses(
            config=_serve_config(target="http://127.0.0.1:9000")
        )
    else:
        changed = _ready_responses(serve=_empty_serve(nonce=True))
    runner = FakeRunner([*_plan_responses(), *changed])
    controller = _controller(tmp_path, runner, which=which)
    plan = controller.plan(port=8080)

    result = controller.apply(plan)

    assert not result.success
    assert "changed after planning" in result.error
    assert _mutations(runner) == []
    runner.assert_finished()


def test_windows_binary_with_spaces_is_one_argv_element(tmp_path: Path) -> None:
    expected_enable = (
        WINDOWS_BINARY,
        "serve",
        "--bg",
        "--yes",
        "--https=443",
        TARGET,
    )
    runner = FakeRunner(_plan_responses(WINDOWS_BINARY))

    plan = _controller(
        tmp_path,
        runner,
        binary=WINDOWS_BINARY,
    ).plan(port=8080)

    assert plan.command == expected_enable
    assert all(call[0][0] == WINDOWS_BINARY for call in runner.calls)


@pytest.mark.parametrize(
    "mutation",
    [
        subprocess.TimeoutExpired(
            (POSIX_BINARY, "serve", "--https=443", "off"),
            29.0,
        ),
        CommandResult(1, stderr="late exit"),
    ],
    ids=["timeout", "nonzero"],
)
def test_disable_verified_absence_is_success_and_clears_ownership(
    tmp_path: Path,
    mutation: CommandResult | BaseException,
) -> None:
    path = tmp_path / "tailscale-ownership.json"
    TailscaleOwnershipStore(path).save(_ownership())
    off = (POSIX_BINARY, "serve", "--https=443", "off")
    runner = FakeRunner(
        [
            *_active_responses(),
            (off, mutation),
            *_ready_responses(),
        ]
    )
    controller = _controller(tmp_path, runner)

    result = controller.disable_owned()

    assert result.success
    assert result.status.state is TailscaleState.READY
    assert not path.exists()
    assert _mutations(runner) == [off]
    assert dict(runner.calls)[off] == 29.0
    runner.assert_finished()


@pytest.mark.parametrize("outcome", ["unchanged", "unknown"])
def test_disable_unchanged_or_unknown_retains_ownership_and_never_retries(
    tmp_path: Path,
    outcome: str,
) -> None:
    clock = FakeClock()
    path = tmp_path / "tailscale-ownership.json"
    ownership = _ownership()
    TailscaleOwnershipStore(path).save(ownership)
    off = (POSIX_BINARY, "serve", "--https=443", "off")
    responses: list[tuple[Sequence[str] | None, CommandResult | BaseException]] = [
        *_active_responses(),
        (off, subprocess.TimeoutExpired(off, 29.0)),
    ]
    if outcome == "unchanged":
        responses.extend(
            [
                *_active_responses(),
                *_active_responses(),
                *_active_responses(),
            ]
        )
    else:
        status_timeout = subprocess.TimeoutExpired(
            (POSIX_BINARY, "status", "--json"),
            2.5,
        )
        responses.extend(
            [
                ((POSIX_BINARY, "status", "--json"), status_timeout),
                ((POSIX_BINARY, "status", "--json"), status_timeout),
                ((POSIX_BINARY, "status", "--json"), status_timeout),
            ]
        )
    runner = FakeRunner(responses)
    controller = _controller(tmp_path, runner, clock=clock)

    result = controller.disable_owned()

    assert not result.success
    assert path.exists()
    assert TailscaleOwnershipStore(path).load() == ownership
    assert _mutations(runner) == [off]
    assert clock.sleeps == [0.5, 0.5]
    if outcome == "unknown":
        assert result.status.state is TailscaleState.OUTCOME_UNVERIFIED
    else:
        assert result.status.state is TailscaleState.ACTIVE_OWNED
    runner.assert_finished()


def test_disable_refuses_changed_or_missing_ownership_without_mutation(
    tmp_path: Path,
) -> None:
    missing_runner = FakeRunner([])
    missing = _controller(tmp_path, missing_runner).disable_owned()
    assert not missing.success
    assert _mutations(missing_runner) == []

    path = tmp_path / "tailscale-ownership.json"
    TailscaleOwnershipStore(path).save(_ownership())
    changed_runner = FakeRunner(_active_responses(config=_serve_config(extra=True)))
    changed = _controller(tmp_path, changed_runner).disable_owned()
    assert not changed.success
    assert path.exists()
    assert _mutations(changed_runner) == []


def test_mutation_timeouts_are_separate_named_defaults() -> None:
    assert READ_ONLY_TIMEOUT_SECONDS < MUTATION_TIMEOUT_SECONDS
    assert 3.0 <= READ_ONLY_TIMEOUT_SECONDS <= 5.0
    assert MUTATION_TIMEOUT_SECONDS == 30.0


def test_redaction_bounds_output_and_removes_every_private_url() -> None:
    detail = redact_command_detail(
        "failure at https://private.example-tailnet.ts.net/secret " * 500
    )

    assert "https://" not in detail
    assert "private.example" not in detail
    assert len(detail) <= 500


@pytest.mark.parametrize("port", [0, 65536, -1, True])
def test_invalid_ports_are_rejected_before_any_command(
    tmp_path: Path,
    port: int,
) -> None:
    runner = FakeRunner([])
    controller = _controller(tmp_path, runner)

    with pytest.raises(ValueError, match="1..65535"):
        controller.plan(port=port)

    assert runner.calls == []


def test_status_public_dict_never_contains_private_controller_metadata(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(_active_responses())

    public = _controller(tmp_path, runner).detect(port=8080).to_public_dict()

    assert public["serve_url"] == ORIGIN
    assert "binary" not in public
    assert "config_fingerprint" not in public
    assert "config_complete" not in public
    assert "routes" not in public


def test_operation_result_type_remains_stable() -> None:
    result = TailscaleOperationResult(
        success=False,
        status=replace(
            TailscaleServeController(
                runner=FakeRunner([]),
                which=lambda _name: None,
            ).detect(port=8080),
            detail="offline",
        ),
        error="offline",
    )

    assert not result.success
    assert result.status.state is TailscaleState.CLI_NOT_FOUND
