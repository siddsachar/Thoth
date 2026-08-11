from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest

from row_bot.access.access_routes import (
    AccessRouteConfig,
    AccessRouteConfigStore,
    AccessRouteKind,
    ListenMode,
    build_route_inventory,
)
from row_bot.access.config import AccessConfig
from row_bot.access.models import SessionLifetime
from row_bot.access.runtime_policy import RuntimeAccessPolicy
from row_bot.access.service import AccessService
from row_bot.access.store import AccessStore
from row_bot.access.tailscale import (
    OWNERSHIP_SCHEMA_VERSION,
    TailscaleOperationResult,
    TailscaleOwnership,
    TailscaleOwnershipStore,
    TailscaleRoute,
    TailscaleState,
    TailscaleStatus,
    TailscaleStatusCache,
)
from row_bot.tunnel import TunnelManager, TunnelProvider
from row_bot.ui.remote_access_settings import (
    LAN_RESTART_NOTICE,
    RemoteAccessActions,
    StaleInvitationRouteError,
    TAILSCALE_DISABLE_RESTART_NOTICE,
    TAILSCALE_ENABLE_RESTART_NOTICE,
    build_remote_access_settings_section,
)


ORIGIN = "https://row-bot.example"
TAILSCALE_ORIGIN = "https://row-bot.example-tailnet.ts.net"
TAILSCALE_TARGET = "http://127.0.0.1:8080"
TAILSCALE_FINGERPRINT = "a" * 64


def _ready_inventory():
    return build_route_inventory(
        port=8080,
        reverse_proxy_origins=(ORIGIN,),
    )


def _owner_authorizer() -> None:
    return None


def _unauthenticated_authorizer() -> None:
    raise PermissionError("authentication_required")


def _owned_tailscale_status() -> tuple[TailscaleStatus, TailscaleOwnership]:
    route = TailscaleRoute(
        origin=TAILSCALE_ORIGIN,
        path="/",
        target=TAILSCALE_TARGET,
        endpoint="row-bot.example-tailnet.ts.net:443",
    )
    status = TailscaleStatus(
        state=TailscaleState.ACTIVE_OWNED,
        binary="/fake/tailscale",
        backend_state="Running",
        dns_name="row-bot.example-tailnet.ts.net",
        serve_url=TAILSCALE_ORIGIN,
        routes=(route,),
        config_fingerprint=TAILSCALE_FINGERPRINT,
        config_complete=True,
        detail="Row-Bot's private Tailscale route is active.",
    )
    ownership = TailscaleOwnership(
        schema_version=OWNERSHIP_SCHEMA_VERSION,
        config_fingerprint=TAILSCALE_FINGERPRINT,
        origin=TAILSCALE_ORIGIN,
        target=TAILSCALE_TARGET,
        path="/",
        https_port=443,
    )
    return status, ownership


def test_owner_actions_create_both_layouts_and_lifetimes(tmp_path) -> None:
    service = AccessService(AccessStore(tmp_path / "mobile.db"))
    actions = RemoteAccessActions(
        service=service,
        route_store=AccessRouteConfigStore(tmp_path / "routes.json"),
        route_inventory_provider=_ready_inventory,
        authorizer=_owner_authorizer,
    )
    route_id = _ready_inventory().invitation_routes[0].id

    desktop = actions.create_invitation(
        layout="desktop",
        route_id=route_id,
        lifetime="trusted",
    )
    compact = actions.create_invitation(
        layout="compact",
        route_id=route_id,
        lifetime="temporary",
    )

    assert desktop.invitation.session_lifetime is SessionLifetime.TRUSTED
    assert desktop.invitation.intended_origin == ORIGIN
    assert desktop.next_path == "/"
    assert compact.invitation.session_lifetime is SessionLifetime.TEMPORARY
    assert compact.next_path == "/?mobile=1"


def test_owner_actions_create_custom_origin_for_both_layouts_and_lifetimes(
    tmp_path,
) -> None:
    service = AccessService(AccessStore(tmp_path / "mobile.db"))
    actions = RemoteAccessActions(
        service=service,
        route_store=AccessRouteConfigStore(tmp_path / "routes.json"),
        authorizer=_owner_authorizer,
    )

    desktop = actions.create_custom_invitation(
        layout="desktop",
        origin=ORIGIN,
        lifetime="trusted",
    )
    compact = actions.create_custom_invitation(
        layout="compact",
        origin="https://tablet.row-bot.example:8443",
        lifetime="temporary",
    )

    assert desktop.invitation.session_lifetime is SessionLifetime.TRUSTED
    assert desktop.invitation.intended_origin == ORIGIN
    assert desktop.invitation.created_by == "settings_owner"
    assert desktop.invitation.access_route == "settings_custom"
    assert desktop.next_path == "/"
    assert compact.invitation.session_lifetime is SessionLifetime.TEMPORARY
    assert compact.invitation.intended_origin == ("https://tablet.row-bot.example:8443")
    assert compact.invitation.created_by == "settings_owner"
    assert compact.invitation.access_route == "settings_custom"
    assert compact.next_path == "/?mobile=1"


def test_custom_origin_canonicalization_is_delegated_to_access_service(
    tmp_path,
) -> None:
    service = AccessService(AccessStore(tmp_path / "mobile.db"))
    actions = RemoteAccessActions(
        service=service,
        route_store=AccessRouteConfigStore(tmp_path / "routes.json"),
        authorizer=_owner_authorizer,
    )

    created = actions.create_custom_invitation(
        layout="desktop",
        origin=" HTTPS://ROW-BOT.EXAMPLE:443/ ",
        lifetime="trusted",
    )

    assert created.invitation.intended_origin == ORIGIN
    assert created.invitation_url().startswith(f"{ORIGIN}/connect?invitation=")


@pytest.mark.parametrize(
    "origin",
    [
        "ftp://row-bot.example",
        "https://owner:secret@row-bot.example",
        "https://row-bot.example/path",
        "https://row-bot.example?query=1",
        "https://row-bot.example#fragment",
        "https://row-bot.example:not-a-port",
    ],
)
def test_invalid_custom_origins_create_no_invitation(tmp_path, origin: str) -> None:
    service = AccessService(AccessStore(tmp_path / "mobile.db"))
    actions = RemoteAccessActions(
        service=service,
        route_store=AccessRouteConfigStore(tmp_path / "routes.json"),
        authorizer=_owner_authorizer,
    )

    with pytest.raises(ValueError):
        actions.create_custom_invitation(
            layout="desktop",
            origin=origin,
            lifetime="trusted",
        )

    assert service.list_invitations() == []


@pytest.mark.parametrize(
    ("layout", "lifetime"),
    [
        ("wide", "trusted"),
        ("desktop", "forever"),
        ("desktop", "migrated"),
    ],
)
def test_invalid_custom_layouts_and_lifetimes_create_no_invitation(
    tmp_path,
    layout: str,
    lifetime: str,
) -> None:
    service = AccessService(AccessStore(tmp_path / "mobile.db"))
    actions = RemoteAccessActions(
        service=service,
        route_store=AccessRouteConfigStore(tmp_path / "routes.json"),
        authorizer=_owner_authorizer,
    )

    with pytest.raises(ValueError):
        actions.create_custom_invitation(
            layout=layout,
            origin=ORIGIN,
            lifetime=lifetime,
        )

    assert service.list_invitations() == []


def test_unauthenticated_custom_origin_cannot_create_invitation(tmp_path) -> None:
    service = AccessService(AccessStore(tmp_path / "mobile.db"))
    actions = RemoteAccessActions(
        service=service,
        route_store=AccessRouteConfigStore(tmp_path / "routes.json"),
        authorizer=_unauthenticated_authorizer,
    )

    with pytest.raises(PermissionError, match="authentication_required"):
        actions.create_custom_invitation(
            layout="desktop",
            origin=ORIGIN,
            lifetime="trusted",
        )

    assert service.list_invitations() == []


def test_unauthenticated_context_cannot_mutate_access_state(tmp_path) -> None:
    route_store = AccessRouteConfigStore(tmp_path / "routes.json")
    service = AccessService(AccessStore(tmp_path / "mobile.db"))
    actions = RemoteAccessActions(
        service=service,
        route_store=route_store,
        authorizer=_unauthenticated_authorizer,
    )

    with pytest.raises(PermissionError, match="authentication_required"):
        actions.create_invitation(
            layout="desktop",
            route_id="missing",
            lifetime="trusted",
        )
    with pytest.raises(PermissionError, match="authentication_required"):
        asyncio.run(actions.set_lan_enabled(True))
    with pytest.raises(PermissionError, match="authentication_required"):
        actions.revoke_device("device")

    assert not route_store.path.exists()
    assert service.list_invitations() == []


def test_device_and_session_revoke_controls_use_service(tmp_path) -> None:
    service = AccessService(AccessStore(tmp_path / "mobile.db"))
    actions = RemoteAccessActions(
        service=service,
        route_store=AccessRouteConfigStore(tmp_path / "routes.json"),
        route_inventory_provider=_ready_inventory,
        authorizer=_owner_authorizer,
    )
    created = actions.create_invitation(
        layout="desktop",
        route_id=_ready_inventory().invitation_routes[0].id,
        lifetime="trusted",
    )
    claim = service.claim_invitation(
        created.token,
        intended_origin=ORIGIN,
        display_name="Laptop",
    )

    assert actions.revoke_session(claim.session.id) is True
    assert service.validate_session(claim.session_token) is None


def test_lan_action_uses_injected_restart_and_persists(tmp_path) -> None:
    restarts = 0

    def restart() -> bool:
        nonlocal restarts
        restarts += 1
        return True

    store = AccessRouteConfigStore(tmp_path / "routes.json")
    actions = RemoteAccessActions(
        service=AccessService(AccessStore(tmp_path / "mobile.db")),
        route_store=store,
        restart_child=restart,
        authorizer=_owner_authorizer,
    )

    result = asyncio.run(actions.set_lan_enabled(True))

    assert result.restarted is True
    assert restarts == 1
    assert store.load().listen_mode is ListenMode.LOCAL_NETWORK


def test_tailscale_controller_is_only_called_by_explicit_guarded_actions(
    tmp_path,
) -> None:
    class FakeTailscale:
        def __init__(self) -> None:
            self.detect_calls = 0
            self.plan_calls = 0
            self.apply_calls = 0

        def detect(self, *, port: int):
            self.detect_calls += 1
            return {"state": "ready", "port": port}

        def plan(self, *, port: int):
            self.plan_calls += 1
            return {"operation": "enable", "port": port}

        def apply(self, plan):
            self.apply_calls += 1
            return plan

    controller = FakeTailscale()
    actions = RemoteAccessActions(
        service=AccessService(AccessStore(tmp_path / "mobile.db")),
        route_store=AccessRouteConfigStore(tmp_path / "routes.json"),
        tailscale_controller=controller,
        authorizer=_owner_authorizer,
    )

    assert controller.detect_calls == 0
    assert controller.plan_calls == 0
    assert controller.apply_calls == 0
    assert asyncio.run(actions.detect_tailscale()) == {"state": "ready", "port": 8080}
    plan = asyncio.run(actions.plan_tailscale())
    assert plan == {"operation": "enable", "port": 8080}
    assert asyncio.run(actions.apply_tailscale(plan)) == plan
    assert (controller.detect_calls, controller.plan_calls, controller.apply_calls) == (
        1,
        1,
        1,
    )


def test_explicit_tailscale_refresh_replaces_cache_and_failure_invalidates(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    statuses = [
        TailscaleStatus(state=TailscaleState.READY, detail="ready"),
        TailscaleStatus(
            state=TailscaleState.CLI_NOT_FOUND,
            detail="unavailable",
        ),
    ]
    cache = TailscaleStatusCache()
    instance_key = str(tmp_path / "data" / "tailscale_serve_ownership.json")

    class FakeTailscale:
        def detect(self, *, port: int):
            assert port == 8080
            if statuses:
                return statuses.pop(0)
            raise RuntimeError("verification failed")

    def remember(status: object | None) -> None:
        cache.remember(
            instance_key=instance_key,
            port=8080,
            status=status,
            ownership=None,
        )

    actions = RemoteAccessActions(
        service=AccessService(AccessStore(tmp_path / "mobile.db")),
        route_store=AccessRouteConfigStore(tmp_path / "routes.json"),
        tailscale_controller=FakeTailscale(),
        tailscale_status_sink=remember,
        authorizer=_owner_authorizer,
    )

    first = asyncio.run(actions.detect_tailscale())
    first_snapshot = cache.get(
        instance_key=instance_key,
        port=8080,
        ownership=None,
    )
    second = asyncio.run(actions.detect_tailscale())
    second_snapshot = cache.get(
        instance_key=instance_key,
        port=8080,
        ownership=None,
    )

    assert first.state is TailscaleState.READY
    assert first_snapshot is not None
    assert first_snapshot.status is first
    assert second.state is TailscaleState.CLI_NOT_FOUND
    assert second_snapshot is not None
    assert second_snapshot.status is second

    with pytest.raises(RuntimeError, match="verification failed"):
        asyncio.run(actions.detect_tailscale())

    assert (
        cache.get(
            instance_key=instance_key,
            port=8080,
            ownership=None,
        )
        is None
    )


def test_enable_disable_and_restart_transitions_update_cached_status(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    active, ownership = _owned_tailscale_status()
    inactive = TailscaleStatus(state=TailscaleState.READY, detail="ready")
    ownership_store = TailscaleOwnershipStore()
    ownership_store.save(ownership)
    cache = TailscaleStatusCache()
    instance_key = str(ownership_store.path.resolve())

    class FakeTailscale:
        def apply(self, _plan):
            ownership_store.save(ownership)
            return TailscaleOperationResult(success=True, status=active)

        def disable_owned(self):
            ownership_store.clear()
            return TailscaleOperationResult(success=True, status=inactive)

    def remember(status: object | None) -> None:
        cache.remember(
            instance_key=instance_key,
            port=8080,
            status=status,
            ownership=ownership_store.load(),
        )

    actions = RemoteAccessActions(
        service=AccessService(AccessStore(tmp_path / "mobile.db")),
        route_store=AccessRouteConfigStore(tmp_path / "routes.json"),
        tailscale_controller=FakeTailscale(),
        tailscale_status_sink=remember,
        authorizer=_owner_authorizer,
    )

    applied = asyncio.run(actions.apply_tailscale(object()))
    applied_snapshot = cache.get(
        instance_key=instance_key,
        port=8080,
        ownership=ownership_store.load(),
    )
    disabled = asyncio.run(actions.disable_tailscale())
    disabled_snapshot = cache.get(
        instance_key=instance_key,
        port=8080,
        ownership=ownership_store.load(),
    )

    assert applied.restart_required is True
    assert applied_snapshot is not None
    assert applied_snapshot.status.state is TailscaleState.ACTIVE_OWNED
    assert disabled.restart_required is True
    assert disabled_snapshot is not None
    assert disabled_snapshot.status.state is TailscaleState.READY

    restarting_actions = RemoteAccessActions(
        service=actions.service,
        route_store=actions.route_store,
        tailscale_controller=FakeTailscale(),
        tailscale_status_sink=remember,
        restart_child=lambda: True,
        authorizer=_owner_authorizer,
    )
    restarted = asyncio.run(restarting_actions.apply_tailscale(object()))

    assert restarted.restarted is True
    assert (
        cache.get(
            instance_key=instance_key,
            port=8080,
            ownership=ownership_store.load(),
        )
        is None
    )


def test_successful_tailscale_changes_restart_to_refresh_proxy_policy(tmp_path) -> None:
    restarts = 0
    active = TailscaleStatus(state=TailscaleState.ACTIVE_OWNED)
    inactive = TailscaleStatus(state=TailscaleState.READY)

    class FakeTailscale:
        def apply(self, _plan):
            return TailscaleOperationResult(success=True, status=active)

        def disable_owned(self):
            return TailscaleOperationResult(success=True, status=inactive)

    def restart() -> bool:
        nonlocal restarts
        restarts += 1
        return True

    actions = RemoteAccessActions(
        service=AccessService(AccessStore(tmp_path / "mobile.db")),
        route_store=AccessRouteConfigStore(tmp_path / "routes.json"),
        tailscale_controller=FakeTailscale(),
        restart_child=restart,
        authorizer=_owner_authorizer,
    )

    applied = asyncio.run(actions.apply_tailscale(object()))
    disabled = asyncio.run(actions.disable_tailscale())

    assert applied.restarted is True
    assert applied.restart_required is False
    assert disabled.restarted is True
    assert disabled.restart_required is False
    assert restarts == 2


def test_failed_tailscale_change_does_not_restart(tmp_path) -> None:
    restarts = 0

    class FakeTailscale:
        def apply(self, _plan):
            return TailscaleOperationResult(
                success=False,
                status=TailscaleStatus(state=TailscaleState.ERROR),
                error="not changed",
            )

    def restart() -> bool:
        nonlocal restarts
        restarts += 1
        return True

    actions = RemoteAccessActions(
        service=AccessService(AccessStore(tmp_path / "mobile.db")),
        route_store=AccessRouteConfigStore(tmp_path / "routes.json"),
        tailscale_controller=FakeTailscale(),
        restart_child=restart,
        authorizer=_owner_authorizer,
    )

    result = asyncio.run(actions.apply_tailscale(object()))

    assert result.success is False
    assert result.restart_required is False
    assert restarts == 0


def test_tailscale_change_requests_manual_restart_without_launcher(tmp_path) -> None:
    class FakeTailscale:
        def apply(self, _plan):
            return TailscaleOperationResult(
                success=True,
                status=TailscaleStatus(state=TailscaleState.ACTIVE_OWNED),
            )

    actions = RemoteAccessActions(
        service=AccessService(AccessStore(tmp_path / "mobile.db")),
        route_store=AccessRouteConfigStore(tmp_path / "routes.json"),
        tailscale_controller=FakeTailscale(),
        authorizer=_owner_authorizer,
    )

    result = asyncio.run(actions.apply_tailscale(object()))

    assert result.restarted is False
    assert result.restart_required is True
    assert result.restart_reason == "launcher_unavailable"


def test_managed_ngrok_inventory_tracks_registration_and_revalidates_invites(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    managed_origin = "https://managed-phone.ngrok-free.app"
    operator_origin = "https://operator-phone.ngrok-free.app"

    class FakeProvider(TunnelProvider):
        def __init__(self) -> None:
            self.active: dict[int, str] = {}

        def start(self, port: int, label: str = "") -> str:  # noqa: ARG002
            self.active[port] = managed_origin
            return managed_origin

        def stop(self, port: int) -> None:
            self.active.pop(port, None)

        def stop_all(self) -> None:
            self.active.clear()

        def get_url(self, port: int) -> str | None:
            return self.active.get(port)

        def is_available(self) -> bool:
            return True

        def active_tunnels(self) -> dict[int, str]:
            return dict(self.active)

    provider = FakeProvider()
    policy = RuntimeAccessPolicy(
        AccessConfig.build(
            deployment_mode="server",
            allowed_hosts=("localhost", "operator-phone.ngrok-free.app"),
            public_origins=(operator_origin,),
        )
    )
    manager = TunnelManager(managed_origin_registrar=policy)
    manager.set_provider(provider)

    def inventory():
        return build_route_inventory(
            port=8080,
            ngrok_url=manager.get_url(8080),
            reverse_proxy_origins=(operator_origin,),
        )

    provider.active[8080] = operator_origin
    operator_inventory = inventory()
    assert operator_inventory.by_kind(AccessRouteKind.NGROK) == ()
    assert operator_inventory.by_kind(AccessRouteKind.REVERSE_PROXY)[0].origin == (
        operator_origin
    )
    provider.active.clear()

    assert manager.start_tunnel(8080, label="main_app") == managed_origin
    active_inventory = inventory()
    managed_route = active_inventory.by_kind(AccessRouteKind.NGROK)[0]
    assert managed_route.origin == managed_origin
    assert managed_route in active_inventory.invitation_routes

    actions = RemoteAccessActions(
        service=AccessService(AccessStore(tmp_path / "mobile.db")),
        route_store=AccessRouteConfigStore(tmp_path / "routes.json"),
        route_inventory_provider=inventory,
        authorizer=_owner_authorizer,
    )
    created = actions.create_invitation(
        layout="desktop",
        route_id=managed_route.id,
        lifetime="trusted",
    )
    assert created.invitation.intended_origin == managed_origin

    manager.stop_tunnel(8080)
    assert inventory().by_kind(AccessRouteKind.NGROK) == ()
    with pytest.raises(StaleInvitationRouteError, match="changed"):
        actions.create_invitation(
            layout="desktop",
            route_id=managed_route.id,
            lifetime="trusted",
        )


def test_settings_render_contract_is_remote_first_and_network_silent() -> None:
    source = inspect.getsource(build_remote_access_settings_section)
    module_source = inspect.getsource(
        __import__(
            "row_bot.ui.remote_access_settings",
            fromlist=["build_remote_access_settings_section"],
        )
    )

    assert '"Remote Access"' in source
    assert '"Invite a device"' in source
    assert "_tailscale_card(" in source
    assert (
        "_lan_card(actions, route_config.lan_enabled, refresh_invitation_routes)"
        in source
    )
    assert "_advanced_card(inventory, port=port)" in source
    assert "detect_tailscale(" not in source
    assert "tunnel_manager" not in module_source
    assert "get_url(" not in module_source
    assert "socket." not in module_source
    assert "requests." not in module_source


def test_settings_provider_uses_process_cache_and_registered_manager_url_only() -> None:
    settings_source = Path("src/row_bot/ui/settings.py").read_text(encoding="utf-8")
    provider_source = settings_source.split(
        "def _route_inventory_provider():",
        1,
    )[1].split("_access_inventory = _route_inventory_provider()", 1)[0]

    assert "_tailscale_status_state" not in settings_source
    assert "_process_tailscale_status_cache()" in settings_source
    assert "_tailscale_status_snapshot()" in provider_source
    assert "ngrok_url=tunnel_manager.get_url(_access_port)" in provider_source
    assert "._provider" not in provider_source
    assert "start_tunnel(" not in provider_source
    assert "detect(" not in provider_source


def test_settings_copy_covers_layouts_owner_access_qr_one_time_and_ngrok() -> None:
    source = inspect.getsource(
        __import__(
            "row_bot.ui.remote_access_settings", fromlist=["RemoteAccessActions"]
        )
    )

    assert "Computer — Desktop layout" in source
    assert "Phone or tablet — Compact layout" in source
    assert "Every connected device has full owner access" in source
    assert "Trusted device — 30 days" in source
    assert "Temporary access — 12 hours" in source
    assert "expires after 10 minutes and works once" in source
    assert "must press Connect" in source
    assert "generate_qr_png_b64" in source
    assert "Copy invitation link" in source
    assert "ngrok — Public tunnel (Advanced)" in source
    assert "Row-Bot authentication is " in source
    assert "still required on its public endpoint" in source


def test_restart_notices_are_rendered_before_lan_and_tailscale_actions() -> None:
    module = __import__(
        "row_bot.ui.remote_access_settings",
        fromlist=["RemoteAccessActions"],
    )
    lan_source = inspect.getsource(module._lan_card)
    tailscale_source = inspect.getsource(module._tailscale_card)

    assert LAN_RESTART_NOTICE == (
        "Applying this change restarts Row-Bot. If no launcher is available, "
        "restart it manually."
    )
    assert TAILSCALE_ENABLE_RESTART_NOTICE == (
        "Enabling this route restarts Row-Bot after the route is verified. If no "
        "launcher is available, restart it manually."
    )
    assert TAILSCALE_DISABLE_RESTART_NOTICE == (
        "Disabling this route restarts Row-Bot after the route removal is "
        "verified. If no launcher is available, restart it manually."
    )
    assert lan_source.index("ui.label(LAN_RESTART_NOTICE)") < lan_source.index(
        'ui.switch("Allow local-network connections"'
    )
    assert tailscale_source.index(
        "ui.label(TAILSCALE_ENABLE_RESTART_NOTICE)"
    ) < tailscale_source.index('"Enable private Serve route"')
    assert tailscale_source.index(
        "ui.label(TAILSCALE_DISABLE_RESTART_NOTICE)"
    ) < tailscale_source.index('"Disable Row-Bot route"')


def test_invitation_selector_uses_stable_ids_and_bounded_live_options() -> None:
    module = __import__(
        "row_bot.ui.remote_access_settings",
        fromlist=["RemoteAccessActions"],
    )
    dialog_source = inspect.getsource(module._invitation_dialog)
    build_source = inspect.getsource(module.build_remote_access_settings_section)

    assert "_route_select_options(inventory)" in dialog_source
    assert "route.id" in inspect.getsource(module._route_select_options)
    assert "selected_route_id" in dialog_source
    assert "actions.refresh_route_inventory()" in dialog_source
    assert "max-height: min(50vh, 22rem)" in dialog_source
    assert "overflow-y: auto" in dialog_source
    assert 'aria-label="Connection route"' in dialog_source
    assert "route_inventory_provider()" not in build_source
    assert "def open_invitation_dialog()" in build_source
    assert build_source.index("refresh_invitation_routes()") < build_source.index(
        "invitation_dialog.open()"
    )
    assert "on_click=open_invitation_dialog" in build_source
    assert "inventory = self.refresh_route_inventory()" in inspect.getsource(
        module.RemoteAccessActions.create_invitation
    )
    inventory = _ready_inventory()
    assert module._route_select_options(inventory) == {
        route.id: route.label for route in inventory.invitation_routes
    }


def test_invitation_selector_surfaces_the_selected_route_warning() -> None:
    module = __import__(
        "row_bot.ui.remote_access_settings",
        fromlist=["RemoteAccessActions"],
    )
    inventory = build_route_inventory(
        port=8080,
        config=AccessRouteConfig(listen_mode=ListenMode.LOCAL_NETWORK),
        lan_addresses=("8.8.8.8",),
        reverse_proxy_origins=(ORIGIN,),
    )
    lan_route = inventory.by_kind(AccessRouteKind.LAN)[0]
    https_route = inventory.by_kind(AccessRouteKind.REVERSE_PROXY)[0]

    assert module._route_help_state(inventory, None) == (
        "Choose a connection route.",
        False,
    )
    lan_text, lan_warning = module._route_help_state(inventory, lan_route.id)
    assert "outside private IP ranges" in lan_text
    assert lan_warning is True
    assert module._route_help_state(inventory, https_route.id) == ("", False)
    assert module._route_help_state(build_route_inventory(port=8080), None) == (
        "No cross-device route is ready. Check Tailscale, enable Local network, "
        "or configure an HTTPS address.",
        False,
    )

    dialog_source = inspect.getsource(module._invitation_dialog)
    assert 'route_select.on("update:model-value"' in dialog_source
    assert "refresh_route_help()" in dialog_source


def test_custom_origin_ui_is_collapsed_separate_and_disclosed() -> None:
    module = __import__(
        "row_bot.ui.remote_access_settings",
        fromlist=["RemoteAccessActions"],
    )
    dialog_source = inspect.getsource(module._invitation_dialog)
    route_handler = dialog_source.split("def create() -> None:", 1)[1].split(
        'ui.button(\n            "Create invitation"',
        1,
    )[0]
    custom_handler = dialog_source.split("def create_custom() -> None:", 1)[1].split(
        'ui.button(\n                "Create for configured address"',
        1,
    )[0]

    assert '"Use another configured address"' in dialog_source
    assert "value=False" in dialog_source
    assert 'label="Browser-facing origin"' in dialog_source
    assert 'placeholder="https://row-bot.example.com"' in dialog_source
    assert '"Create invitation"' in dialog_source
    assert '"Create for configured address"' in dialog_source
    assert "actions.create_invitation(" in route_handler
    assert "route_id=selected_route_id" in route_handler
    assert "actions.create_custom_invitation(" not in route_handler
    assert "actions.create_custom_invitation(" in custom_handler
    assert "actions.create_invitation(" not in custom_handler
    assert custom_handler.index("if not origin.strip()") < custom_handler.index(
        "actions.create_custom_invitation("
    )
    for boundary in (
        "Cloudflare",
        "DNS",
        "TLS",
        "proxy trust",
        "firewall rules",
        "Row-Bot's listen address",
    ):
        assert boundary in dialog_source
    assert "only creates an invitation" in dialog_source
    assert "does not configure or verify" in dialog_source


def test_route_inventory_can_be_injected_without_detection() -> None:
    inventory = build_route_inventory(port=9090)

    assert inventory.preferred_invitation_origin() is None


def test_invitation_resolves_route_again_and_rejects_stale_selection(tmp_path) -> None:
    inventories = [_ready_inventory(), build_route_inventory(port=8080)]
    calls = 0

    def current_inventory():
        nonlocal calls
        inventory = inventories[min(calls, len(inventories) - 1)]
        calls += 1
        return inventory

    actions = RemoteAccessActions(
        service=AccessService(AccessStore(tmp_path / "mobile.db")),
        route_store=AccessRouteConfigStore(tmp_path / "routes.json"),
        route_inventory_provider=current_inventory,
        authorizer=_owner_authorizer,
    )
    selected_id = current_inventory().invitation_routes[0].id

    with pytest.raises(StaleInvitationRouteError, match="changed"):
        actions.create_invitation(
            layout="desktop",
            route_id=selected_id,
            lifetime="trusted",
        )

    assert actions.service.list_invitations() == []


def test_cached_tailscale_copy_is_visibly_last_verified() -> None:
    module = __import__(
        "row_bot.ui.remote_access_settings",
        fromlist=["RemoteAccessActions"],
    )
    tailscale_source = inspect.getsource(module._tailscale_card)

    assert "Last verified by explicit check:" in tailscale_source
    assert "Cached for this running Row-Bot process; refresh to recheck." in (
        tailscale_source
    )
    assert "Private origin at last verification" in tailscale_source
