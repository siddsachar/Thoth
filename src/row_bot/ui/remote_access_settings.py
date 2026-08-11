"""Remote Access settings UI for owner devices."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import inspect
import json
from typing import Any

from nicegui import run, ui

from row_bot.access.access_routes import (
    AccessRouteConfigStore,
    AccessRouteInventory,
    AccessRouteKind,
    ListenMode,
    ListenModeChange,
    RestartChild,
    apply_listen_mode,
    build_route_inventory,
)
from row_bot.access.models import SessionLifetime
from row_bot.access.service import AccessService, CreatedInvitation
from row_bot.ui.access_context import current_access_context, require_ui_owner

Authorize = Callable[[], Any]
RouteInventoryProvider = Callable[[], AccessRouteInventory]
TailscaleStatusSink = Callable[[object | None], None]

LAN_RESTART_NOTICE = (
    "Applying this change restarts Row-Bot. If no launcher is available, "
    "restart it manually."
)
TAILSCALE_ENABLE_RESTART_NOTICE = (
    "Enabling this route restarts Row-Bot after the route is verified. If no "
    "launcher is available, restart it manually."
)
TAILSCALE_DISABLE_RESTART_NOTICE = (
    "Disabling this route restarts Row-Bot after the route removal is verified. "
    "If no launcher is available, restart it manually."
)


class StaleInvitationRouteError(ValueError):
    """Raised when a selected route is no longer eligible."""


@dataclass(slots=True)
class RemoteAccessActions:
    """Capability-guarded handlers used by the NiceGUI controls."""

    service: AccessService
    route_store: AccessRouteConfigStore
    port: int = 8080
    restart_child: RestartChild | None = None
    tailscale_controller: object | None = None
    route_inventory_provider: RouteInventoryProvider | None = None
    tailscale_status_sink: TailscaleStatusSink | None = None
    authorizer: Authorize = require_ui_owner
    _controller_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def _require_admin(self) -> None:
        self.authorizer()

    def _publish_tailscale_status(self, status: object | None) -> None:
        if self.tailscale_status_sink:
            self.tailscale_status_sink(status)

    def create_invitation(
        self,
        *,
        layout: str,
        route_id: str,
        lifetime: str,
    ) -> CreatedInvitation:
        self._require_admin()
        if layout not in {"desktop", "compact"}:
            raise ValueError("layout must be desktop or compact")
        normalized_lifetime = SessionLifetime(lifetime)
        inventory = self.refresh_route_inventory()
        route = inventory.resolve_invitation_route(route_id)
        if route is None:
            raise StaleInvitationRouteError(
                "The selected connection route changed. Refresh and try again."
            )
        return self.service.create_invitation(
            intended_origin=route.origin,
            session_lifetime=normalized_lifetime,
            next_path="/?mobile=1" if layout == "compact" else "/",
            created_by="settings_owner",
            access_route="settings",
        )

    def create_custom_invitation(
        self,
        *,
        layout: str,
        origin: str,
        lifetime: str,
    ) -> CreatedInvitation:
        self._require_admin()
        if layout not in {"desktop", "compact"}:
            raise ValueError("layout must be desktop or compact")
        normalized_lifetime = SessionLifetime(lifetime)
        return self.service.create_invitation(
            intended_origin=origin,
            session_lifetime=normalized_lifetime,
            next_path="/?mobile=1" if layout == "compact" else "/",
            created_by="settings_owner",
            access_route="settings_custom",
        )

    def refresh_route_inventory(self) -> AccessRouteInventory:
        provider = self.route_inventory_provider
        if provider is None:
            return build_route_inventory(
                port=self.port,
                config=self.route_store.load_or_default(),
            )
        inventory = provider()
        if not isinstance(inventory, AccessRouteInventory):
            raise TypeError("route inventory provider returned an invalid value")
        return inventory

    def revoke_device(self, device_id: str) -> bool:
        self._require_admin()
        return self.service.revoke_device(device_id)

    def revoke_session(self, session_id: str) -> bool:
        self._require_admin()
        return self.service.revoke_session(session_id)

    async def set_lan_enabled(self, enabled: bool) -> ListenModeChange:
        self._require_admin()
        return await apply_listen_mode(
            self.route_store,
            ListenMode.LOCAL_NETWORK if enabled else ListenMode.LOCAL_ONLY,
            restart_child=self.restart_child,
        )

    async def _restart_after_tailscale_change(self, result: Any) -> Any:
        """Restart after a verified mutation so startup proxy policy is current."""
        if not bool(getattr(result, "success", False)):
            return result
        if self.restart_child is None:
            return replace(
                result,
                restart_required=True,
                restart_reason="launcher_unavailable",
            )
        try:
            restart = self.restart_child()
            if inspect.isawaitable(restart):
                restart = await asyncio.wait_for(restart, timeout=10.0)
            restarted = restart is not False
        except TimeoutError:
            return replace(
                result,
                restart_required=True,
                restart_reason="restart_timeout",
            )
        except Exception:
            return replace(
                result,
                restart_required=True,
                restart_reason="restart_failed",
            )
        return replace(
            result,
            restarted=restarted,
            restart_required=not restarted,
            restart_reason="restarted" if restarted else "restart_declined",
        )

    async def detect_tailscale(self) -> Any:
        """Run injected detection only after an explicit owner action."""
        self._require_admin()
        if self.tailscale_controller is None:
            self._publish_tailscale_status(None)
            return None
        detect = getattr(self.tailscale_controller, "detect", None)
        if not callable(detect):
            self._publish_tailscale_status(None)
            return None
        try:
            result = (
                await detect(port=self.port)
                if inspect.iscoroutinefunction(detect)
                else await run.io_bound(detect, port=self.port)
            )
        except Exception:
            self._publish_tailscale_status(None)
            raise
        self._publish_tailscale_status(result)
        return result

    async def plan_tailscale(self) -> Any:
        """Ask the injected controller for a non-mutating setup plan."""
        self._require_admin()
        if self.tailscale_controller is None:
            self._publish_tailscale_status(None)
            return None
        plan = getattr(self.tailscale_controller, "plan", None)
        if not callable(plan):
            self._publish_tailscale_status(None)
            return None
        try:
            result = (
                await plan(port=self.port)
                if inspect.iscoroutinefunction(plan)
                else await run.io_bound(plan, port=self.port)
            )
        except Exception:
            self._publish_tailscale_status(None)
            raise
        self._publish_tailscale_status(getattr(result, "status", None))
        return result

    async def apply_tailscale(self, plan: object) -> Any:
        """Apply an already-reviewed plan after an explicit owner action."""
        self._require_admin()
        if self.tailscale_controller is None:
            self._publish_tailscale_status(None)
            return None
        apply = getattr(self.tailscale_controller, "apply", None)
        if not callable(apply):
            self._publish_tailscale_status(None)
            return None
        if self._controller_lock.locked():
            raise RuntimeError("A Tailscale operation is already pending.")
        self._publish_tailscale_status(None)
        async with self._controller_lock:
            result = (
                await apply(plan)
                if inspect.iscoroutinefunction(apply)
                else await run.io_bound(apply, plan)
            )
        self._publish_tailscale_status(getattr(result, "status", None))
        outcome = await self._restart_after_tailscale_change(result)
        if bool(getattr(outcome, "restarted", False)):
            self._publish_tailscale_status(None)
        return outcome

    async def disable_tailscale(self) -> Any:
        """Remove only the unchanged route proven to be owned by Row-Bot."""
        self._require_admin()
        if self.tailscale_controller is None:
            self._publish_tailscale_status(None)
            return None
        disable = getattr(self.tailscale_controller, "disable_owned", None)
        if not callable(disable):
            self._publish_tailscale_status(None)
            return None
        if self._controller_lock.locked():
            raise RuntimeError("A Tailscale operation is already pending.")
        self._publish_tailscale_status(None)
        async with self._controller_lock:
            result = (
                await disable()
                if inspect.iscoroutinefunction(disable)
                else await run.io_bound(disable)
            )
        self._publish_tailscale_status(getattr(result, "status", None))
        outcome = await self._restart_after_tailscale_change(result)
        if bool(getattr(outcome, "restarted", False)):
            self._publish_tailscale_status(None)
        return outcome


def build_remote_access_settings_section(
    *,
    settings_section: Callable[..., Any],
    service: AccessService | None = None,
    route_store: AccessRouteConfigStore | None = None,
    route_inventory: AccessRouteInventory | None = None,
    route_inventory_provider: RouteInventoryProvider | None = None,
    tailscale_status_sink: TailscaleStatusSink | None = None,
    tailscale_status: object | None = None,
    tailscale_verified_at: datetime | None = None,
    restart_child: RestartChild | None = None,
    tailscale_controller: object | None = None,
    authorizer: Authorize = require_ui_owner,
    port: int = 8080,
    status_dot: Callable[[str, str, str | None], None] | None = None,
    metric_chip: Callable[[str, Any, str | None, str], None] | None = None,
) -> None:
    """Render Settings → System → Remote Access without network probes."""
    selected_service = service or AccessService()
    selected_store = route_store or AccessRouteConfigStore()
    route_config = selected_store.load_or_default()
    inventory = route_inventory or build_route_inventory(
        port=port,
        config=route_config,
    )
    selected_inventory_provider = route_inventory_provider or (lambda: inventory)
    actions = RemoteAccessActions(
        service=selected_service,
        route_store=selected_store,
        port=port,
        restart_child=restart_child,
        tailscale_controller=tailscale_controller,
        route_inventory_provider=selected_inventory_provider,
        tailscale_status_sink=tailscale_status_sink,
        authorizer=authorizer,
    )

    devices = selected_service.list_devices(include_revoked=True)
    sessions = selected_service.list_sessions(include_revoked=True)
    active_devices = [device for device in devices if device.revoked_at is None]

    with settings_section(
        "Remote Access",
        "Connect your other computers, phones, and tablets securely.",
        icon="devices",
        tone="warning",
        docs_id="remote-access-settings",
    ):
        with ui.row().classes("items-center gap-2 w-full"):
            if status_dot:
                status_dot(
                    "Protected",
                    "ok",
                    "Every non-local device needs a revocable Row-Bot session.",
                )
            if metric_chip:
                metric_chip(
                    "connected devices",
                    len(active_devices),
                    "devices",
                    "primary",
                )
        ui.label(
            "Remote Access is off by default. Your normal desktop localhost "
            "experience stays unchanged."
        ).classes("text-grey-6 text-xs")
        ui.label(
            "Every authenticated browser is the same Row-Bot owner. Device and "
            "layout details do not reduce permissions."
        ).classes("text-grey-6 text-xs")
        _current_session_card()

        invitation_dialog, invitation_content, refresh_invitation_routes = (
            _invitation_dialog(
                actions=actions,
                inventory=inventory,
            )
        )

        def open_invitation_dialog() -> None:
            try:
                refresh_invitation_routes()
            except Exception:
                ui.notify(
                    "Connection routes could not be refreshed. Try again.",
                    type="warning",
                )
                return
            invitation_dialog.open()

        with ui.row().classes("items-center gap-2 w-full"):
            ui.button(
                "Invite a device",
                icon="qr_code_2",
                on_click=open_invitation_dialog,
            ).props("unelevated no-caps color=primary")

        _tailscale_card(
            actions,
            inventory,
            refresh_invitation_routes,
            initial_status=tailscale_status,
            initial_verified_at=tailscale_verified_at,
        )
        _lan_card(actions, route_config.lan_enabled, refresh_invitation_routes)
        _devices_card(actions, devices, sessions)
        _advanced_card(inventory, port=port)

        # Retain the reference for NiceGUI's element lifecycle.
        invitation_content


def _current_session_card() -> None:
    context = current_access_context()
    if context is None or context.session_id is None:
        return
    with ui.card().classes("w-full q-pa-sm"):
        ui.label("This browser session").classes("text-subtitle2")
        ui.label(
            "Signing out revokes this browser session without affecting other devices."
        ).classes("text-grey-6 text-xs")
        ui.button(
            "Sign out this browser",
            icon="logout",
            on_click=lambda: ui.run_javascript(
                "fetch('/api/access/logout',{method:'POST',headers:{"
                "'Content-Type':'application/json'},body:'{}'}).finally("
                "()=>window.location.replace('/connect'))"
            ),
        ).props("flat dense no-caps color=negative")


def _invitation_dialog(
    *,
    actions: RemoteAccessActions,
    inventory: AccessRouteInventory,
) -> tuple[Any, Any, Callable[[], None]]:
    with (
        ui.dialog() as dialog,
        ui.card()
        .classes("w-[720px] max-w-full q-pa-md")
        .props('data-docs-id="remote-access-invitation"'),
    ):
        content = ui.column().classes("w-full gap-3")

    with content:
        with ui.row().classes("items-start justify-between w-full no-wrap"):
            with ui.column().classes("gap-0"):
                ui.label("Invite a device").classes("text-h6")
                ui.label("Choose the layout for this device.").classes(
                    "text-grey-6 text-sm"
                )
                ui.label(
                    "Every connected device has full owner access to this Row-Bot."
                ).classes("text-grey-6 text-sm")
            ui.button(icon="close", on_click=dialog.close).props("flat dense round")

        layout = ui.radio(
            {
                "desktop": "Computer — Desktop layout",
                "compact": "Phone or tablet — Compact layout",
            },
            value="desktop",
        ).props("inline")
        lifetime = ui.radio(
            {
                "trusted": "Trusted device — 30 days",
                "temporary": "Temporary access — 12 hours",
            },
            value="trusted",
        ).props("inline")
        initial_route = inventory.preferred_invitation_route()
        route_select = (
            ui.select(
                _route_select_options(inventory),
                value=initial_route.id if initial_route else None,
                label="Connection route",
            )
            .props(
                'options-dense behavior="menu" popup-content-class="'
                'row-bot-route-options" aria-label="Connection route"'
            )
            .classes("w-full min-w-0")
            .style("min-width: 0;")
        )
        route_select.add_slot(
            "option",
            r"""
            <q-item v-bind="props.itemProps">
              <q-item-section>
                <q-item-label class="ellipsis">{{ props.opt.label }}</q-item-label>
              </q-item-section>
            </q-item>
            """,
        )
        route_help = ui.label().classes("text-grey-6 text-xs")
        ui.add_css(
            ".row-bot-route-options { max-height: min(50vh, 22rem); "
            "overflow-y: auto; max-width: calc(100vw - 2rem); }"
        )
        ui.label(
            "The invitation expires after 10 minutes and works once. "
            "Opening or previewing it does not connect the device; the "
            "recipient must press Connect."
        ).classes("text-grey-6 text-xs")
        result = ui.column().classes("w-full gap-2")

        active_inventory = inventory

        def refresh_route_help() -> None:
            text, warning = _route_help_state(active_inventory, route_select.value)
            route_help.text = text
            route_help.classes(
                replace=f"{'text-warning' if warning else 'text-grey-6'} text-xs"
            )
            route_help.update()

        route_select.on("update:model-value", lambda _: refresh_route_help())

        def refresh_routes(
            next_inventory: AccessRouteInventory | None = None,
        ) -> None:
            nonlocal active_inventory
            refreshed = next_inventory or actions.refresh_route_inventory()
            active_inventory = refreshed
            previous_route_id = str(route_select.value or "")
            route_select.options = _route_select_options(refreshed)
            route_ids = {route.id for route in refreshed.invitation_routes}
            if previous_route_id in route_ids:
                route_select.value = previous_route_id
            else:
                preferred = refreshed.preferred_invitation_route()
                route_select.value = preferred.id if preferred else None
            route_select.update()
            refresh_route_help()

        def create() -> None:
            result.clear()
            selected_route_id = str(route_select.value or "")
            if not selected_route_id:
                ui.notify(
                    "Choose a reachable connection route before creating an invitation.",
                    type="warning",
                )
                return
            try:
                created = actions.create_invitation(
                    layout=str(layout.value),
                    route_id=selected_route_id,
                    lifetime=str(lifetime.value),
                )
            except PermissionError:
                ui.notify("Owner access is required.", type="negative")
                return
            except StaleInvitationRouteError as exc:
                refresh_routes()
                ui.notify(str(exc), type="warning")
                return
            _render_created_invitation(
                result,
                created,
                layout=str(layout.value),
            )

        ui.button(
            "Create invitation",
            icon="link",
            on_click=create,
        ).props("unelevated no-caps color=primary")

        with ui.expansion(
            "Use another configured address",
            icon="travel_explore",
            value=False,
        ).classes("w-full"):
            custom_origin = (
                ui.input(
                    label="Browser-facing origin",
                    placeholder="https://row-bot.example.com",
                )
                .props("dense outlined")
                .classes("w-full")
            )
            ui.label(
                "This only creates an invitation for an address you have already "
                "configured. It does not configure or verify Cloudflare, DNS, TLS, "
                "proxy trust, firewall rules, or Row-Bot's listen address."
            ).classes("text-grey-6 text-xs")

            def create_custom() -> None:
                result.clear()
                origin = str(custom_origin.value or "")
                if not origin.strip():
                    ui.notify(
                        "Enter a browser-facing origin before creating an invitation.",
                        type="warning",
                    )
                    return
                try:
                    created = actions.create_custom_invitation(
                        layout=str(layout.value),
                        origin=origin,
                        lifetime=str(lifetime.value),
                    )
                except PermissionError:
                    ui.notify("Owner access is required.", type="negative")
                    return
                except ValueError:
                    ui.notify(
                        "Enter an exact HTTP or HTTPS origin, such as "
                        "https://row-bot.example.com.",
                        type="warning",
                    )
                    return
                _render_created_invitation(
                    result,
                    created,
                    layout=str(layout.value),
                )

            ui.button(
                "Create for configured address",
                icon="link",
                on_click=create_custom,
            ).props("unelevated no-caps color=primary")

    refresh_routes(inventory)
    return dialog, content, refresh_routes


def _route_select_options(inventory: AccessRouteInventory) -> dict[str, str]:
    """Return NiceGUI's value-to-label mapping for stable route IDs."""
    return {route.id: route.label for route in inventory.invitation_routes}


def _route_help_state(
    inventory: AccessRouteInventory,
    route_id: object,
) -> tuple[str, bool]:
    """Return selector help text and whether it should use warning styling."""
    if not inventory.invitation_routes:
        return (
            "No cross-device route is ready. Check Tailscale, enable Local network, "
            "or configure an HTTPS address.",
            False,
        )
    route = inventory.resolve_invitation_route(route_id)
    if route is None:
        return "Choose a connection route.", False
    if route.warning:
        return route.warning, True
    return "", False


def _render_created_invitation(
    container: Any,
    created: CreatedInvitation,
    *,
    layout: str,
) -> None:
    invitation_url = created.invitation_url()
    with container:
        ui.separator()
        ui.label(
            "Computer — Desktop layout"
            if layout == "desktop"
            else "Phone or tablet — Compact layout"
        ).classes("text-subtitle2 text-weight-medium")
        try:
            from row_bot.designer.qr_utils import generate_qr_png_b64

            qr_image = generate_qr_png_b64(invitation_url, box_size=8)
        except Exception:
            qr_image = ""
        if qr_image:
            ui.image(qr_image).style(
                "width: 240px; height: 240px; image-rendering: pixelated;"
            )
        else:
            ui.label(
                "QR generation is unavailable. Copy the invitation link instead."
            ).classes("text-warning text-sm")
        ui.button(
            "Copy invitation link",
            icon="content_copy",
            on_click=lambda: _copy_to_clipboard(invitation_url),
        ).props("flat dense no-caps color=primary")
        ui.label(invitation_url).classes("text-primary text-xs").style(
            "word-break: break-all;"
        )
        ui.label(f"Expires: {created.invitation.expires_at.isoformat()}").classes(
            "text-grey-6 text-xs"
        )
        ui.label(
            "This one-time link creates a normal revocable session only after "
            "the recipient presses Connect. Session credentials never appear "
            "in the URL."
        ).classes("text-grey-6 text-xs")


def _tailscale_card(
    actions: RemoteAccessActions,
    inventory: AccessRouteInventory,
    refresh_invitation_routes: Callable[[], None],
    *,
    initial_status: object | None = None,
    initial_verified_at: datetime | None = None,
) -> None:
    with ui.card().classes("w-full q-pa-md"):
        with ui.row().classes("items-center gap-2"):
            ui.icon("vpn_lock")
            ui.label("Tailscale — Recommended").classes(
                "text-subtitle2 text-weight-medium"
            )
        ui.label(
            "Private HTTPS access through your tailnet. Row-Bot never installs "
            "Tailscale, signs you in, enables Funnel, or resets other routes."
        ).classes("text-grey-6 text-xs")
        state_area = ui.column().classes("w-full gap-2")

        def render_status(
            state: Any | None,
            *,
            verified_at: datetime | None = None,
        ) -> None:
            state_area.clear()
            with state_area:
                if state is None:
                    ui.badge("Not checked", color="grey").props("outline")
                    return
                raw_state = (
                    state.get("state") or state.get("status")
                    if isinstance(state, dict)
                    else getattr(state, "state", state)
                )
                state_text = str(getattr(raw_state, "value", raw_state) or "unknown")
                color = (
                    "positive"
                    if state_text == "active_owned"
                    else "warning"
                    if state_text
                    in {
                        "consent_required",
                        "route_conflict",
                        "active_unowned",
                        "funnel_active",
                        "outcome_unverified",
                    }
                    else "grey"
                )
                ui.badge(state_text.replace("_", " ").title(), color=color).props(
                    "outline"
                )
                detail_value = (
                    state.get("detail", "")
                    if isinstance(state, dict)
                    else getattr(state, "detail", "")
                )
                detail = str(detail_value or "")
                if detail:
                    ui.label(detail).classes("text-grey-6 text-xs")
                if verified_at is None:
                    verified_label = (
                        "Verified by an explicit check in this Settings session."
                    )
                else:
                    normalized_verified_at = (
                        verified_at.replace(tzinfo=timezone.utc)
                        if verified_at.tzinfo is None
                        else verified_at.astimezone(timezone.utc)
                    )
                    verified_label = (
                        "Last verified by explicit check: "
                        f"{normalized_verified_at.strftime('%Y-%m-%d %H:%M:%S')} UTC. "
                        "Cached for this running Row-Bot process; refresh to recheck."
                    )
                ui.label(verified_label).classes("text-grey-6 text-xs")
                serve_url_value = (
                    state.get("serve_url", "")
                    if isinstance(state, dict)
                    else getattr(state, "serve_url", "")
                )
                serve_url = str(serve_url_value or "")
                if serve_url:
                    _copy_value_row("Private origin at last verification", serve_url)
                consent_url_value = (
                    state.get("consent_url", "")
                    if isinstance(state, dict)
                    else getattr(state, "consent_url", "")
                )
                consent_url = str(consent_url_value or "")
                if consent_url:
                    ui.link("Review Tailscale consent", consent_url, new_tab=True)

        render_status(initial_status, verified_at=initial_verified_at)

        async def check_status() -> None:
            try:
                state = await actions.detect_tailscale()
            except PermissionError:
                ui.notify("Owner access is required.", type="negative")
                return
            except Exception:
                render_status(None)
                refresh_invitation_routes()
                ui.notify(
                    "Tailscale status could not be verified.",
                    type="warning",
                )
                return
            ui.notify(
                "Tailscale status refreshed."
                if state is not None
                else "Tailscale controller is unavailable.",
                type="info" if state is not None else "warning",
            )
            render_status(state, verified_at=datetime.now(timezone.utc))
            refresh_invitation_routes()

        async def review_plan() -> None:
            try:
                plan = await actions.plan_tailscale()
            except PermissionError:
                ui.notify("Owner access is required.", type="negative")
                return
            except Exception:
                render_status(None)
                refresh_invitation_routes()
                ui.notify(
                    "Tailscale status could not be verified.",
                    type="warning",
                )
                return
            if plan is None:
                ui.notify("Tailscale controller is unavailable.", type="warning")
                return
            render_status(
                getattr(plan, "status", None),
                verified_at=datetime.now(timezone.utc),
            )
            with state_area:
                render_state = getattr(plan, "status", None)
                raw_state = getattr(render_state, "state", "")
                state_text = str(getattr(raw_state, "value", raw_state) or "")
                ui.label(str(getattr(plan, "description", "") or "")).classes(
                    "text-grey-6 text-xs"
                )
                if bool(getattr(plan, "can_apply", False)):
                    ui.label(
                        "Tailscale is third-party software. The command below "
                        "changes its local Serve configuration and may contact "
                        "Tailscale's service."
                    ).classes("text-warning text-xs")
                    with ui.row().classes("items-center gap-3"):
                        ui.link(
                            "Tailscale privacy policy",
                            "https://tailscale.com/privacy-policy",
                            new_tab=True,
                        )
                        ui.link(
                            "Tailscale terms",
                            "https://tailscale.com/terms",
                            new_tab=True,
                        )
                    ui.label(TAILSCALE_ENABLE_RESTART_NOTICE).classes(
                        "text-warning text-xs"
                    )
                    consent = ui.checkbox("I reviewed this exact private Serve change.")

                    async def apply_plan() -> None:
                        if not consent.value:
                            ui.notify(
                                "Review and confirm the third-party change first.",
                                type="warning",
                            )
                            return
                        outcome = await actions.apply_tailscale(plan)
                        render_status(
                            getattr(outcome, "status", None),
                            verified_at=datetime.now(timezone.utc),
                        )
                        refresh_invitation_routes()
                        success = bool(getattr(outcome, "success", False))
                        if success and bool(getattr(outcome, "restarted", False)):
                            message = (
                                "Private Tailscale route enabled. "
                                "Row-Bot is restarting."
                            )
                        elif success and bool(
                            getattr(outcome, "restart_required", False)
                        ):
                            message = (
                                "Private Tailscale route enabled. "
                                "Restart Row-Bot to activate it."
                            )
                        elif success:
                            message = "Private Tailscale route enabled."
                        else:
                            message = str(
                                getattr(outcome, "error", "")
                                or "Tailscale setup did not complete."
                            )
                        ui.notify(
                            message,
                            type=(
                                "warning"
                                if bool(getattr(outcome, "restart_required", False))
                                else "positive"
                                if success
                                else "negative"
                            ),
                        )

                    ui.button(
                        "Enable private Serve route",
                        icon="vpn_lock",
                        on_click=apply_plan,
                    ).props("unelevated no-caps color=primary")
                elif state_text == "active_owned":
                    ui.label(TAILSCALE_DISABLE_RESTART_NOTICE).classes(
                        "text-warning text-xs"
                    )

                    async def disable_owned() -> None:
                        outcome = await actions.disable_tailscale()
                        render_status(
                            getattr(outcome, "status", None),
                            verified_at=datetime.now(timezone.utc),
                        )
                        refresh_invitation_routes()
                        success = bool(getattr(outcome, "success", False))
                        if success and bool(getattr(outcome, "restarted", False)):
                            message = (
                                "Owned Tailscale route disabled. Row-Bot is restarting."
                            )
                        elif success and bool(
                            getattr(outcome, "restart_required", False)
                        ):
                            message = (
                                "Owned Tailscale route disabled. "
                                "Restart Row-Bot to finish the change."
                            )
                        elif success:
                            message = "Owned Tailscale route disabled."
                        else:
                            message = str(
                                getattr(outcome, "error", "")
                                or "The route was not changed."
                            )
                        ui.notify(
                            message,
                            type=(
                                "warning"
                                if bool(getattr(outcome, "restart_required", False))
                                else "positive"
                                if success
                                else "warning"
                            ),
                        )

                    ui.button(
                        "Disable Row-Bot route",
                        icon="link_off",
                        on_click=disable_owned,
                    ).props("flat dense no-caps color=negative")

        ui.button(
            "Check Tailscale status",
            icon="refresh",
            on_click=check_status,
        ).props("flat dense no-caps")
        ui.button(
            "Review private route",
            icon="fact_check",
            on_click=review_plan,
        ).props("flat dense no-caps color=primary")


def _lan_card(
    actions: RemoteAccessActions,
    enabled: bool,
    refresh_invitation_routes: Callable[[], None],
) -> None:
    with ui.card().classes("w-full q-pa-md"):
        with ui.row().classes("items-center gap-2"):
            ui.icon("lan")
            ui.label("Local network").classes("text-subtitle2 text-weight-medium")
        ui.label(
            "Devices on this network can see the Row-Bot connection page, but "
            "cannot use the app without a valid session. LAN HTTP is unencrypted."
        ).classes("text-grey-6 text-xs")
        outcome = ui.label("Enabled" if enabled else "Off — localhost only").classes(
            "text-grey-6 text-xs"
        )
        ui.label(LAN_RESTART_NOTICE).classes("text-warning text-xs")

        async def change(event: Any) -> None:
            try:
                result = await actions.set_lan_enabled(bool(event.value))
            except PermissionError:
                ui.notify("Owner access is required.", type="negative")
                return
            if result.restarted:
                message = "Row-Bot restarted with the new listen setting."
            elif result.restart_required:
                message = "Restart Row-Bot to apply this change."
            else:
                message = "Listen setting is already active."
            outcome.text = message
            outcome.update()
            refresh_invitation_routes()
            ui.notify(
                message,
                type="warning" if result.restart_required else "positive",
            )

        ui.switch("Allow local-network connections", value=enabled, on_change=change)


def _devices_card(
    actions: RemoteAccessActions,
    devices: list[Any],
    sessions: list[Any],
) -> None:
    sessions_by_device: dict[str, list[Any]] = {}
    for session in sessions:
        sessions_by_device.setdefault(session.device_id, []).append(session)

    with ui.expansion("Connected devices and sessions", icon="devices").classes(
        "w-full"
    ):
        if not devices:
            ui.label("No connected devices yet.").classes("text-grey-6 text-xs")
            return
        for device in devices:
            with ui.card().classes("w-full q-pa-sm"):
                with ui.row().classes("items-start justify-between w-full no-wrap"):
                    with ui.column().classes("gap-0"):
                        ui.label(device.display_name).classes(
                            "text-sm text-weight-medium"
                        )
                        ui.label(
                            "Owner device · "
                            f"{'revoked' if device.revoked_at else 'active'}"
                        ).classes("text-grey-6 text-xs")

                    def revoke_device(device_id: str = device.id) -> None:
                        try:
                            revoked = actions.revoke_device(device_id)
                        except PermissionError:
                            ui.notify("Owner access is required.", type="negative")
                            return
                        ui.notify(
                            "Device revoked."
                            if revoked
                            else "Device was already revoked.",
                            type="positive" if revoked else "info",
                        )

                    if device.revoked_at is None:
                        ui.button(
                            icon="block",
                            on_click=revoke_device,
                        ).props("flat dense round color=negative").tooltip(
                            "Revoke device and all sessions"
                        )
                for session in sessions_by_device.get(device.id, []):
                    with ui.row().classes("items-center gap-2 w-full"):
                        ui.label(
                            f"Session {session.id[:8]}… · "
                            f"expires {session.expires_at.isoformat()}"
                        ).classes("text-grey-6 text-xs")

                        def revoke_session(session_id: str = session.id) -> None:
                            try:
                                revoked = actions.revoke_session(session_id)
                            except PermissionError:
                                ui.notify("Owner access is required.", type="negative")
                                return
                            ui.notify(
                                "Session revoked."
                                if revoked
                                else "Session was already revoked.",
                                type="positive" if revoked else "info",
                            )

                        if session.revoked_at is None:
                            ui.button(
                                icon="logout",
                                on_click=revoke_session,
                            ).props("flat dense round").tooltip("Revoke session")


def _advanced_card(inventory: AccessRouteInventory, *, port: int) -> None:
    with ui.expansion("Advanced", icon="tune").classes("w-full"):
        ui.label(
            "Expert connection details. Raw hosts, proxies, and public origins "
            "are configured through the CLI or deployment configuration."
        ).classes("text-grey-6 text-xs")
        ui.label(f"Application port: {port}").classes("text-grey-6 text-xs")
        for route in inventory.routes:
            with ui.card().classes("w-full q-pa-sm"):
                ui.label(route.label).classes("text-sm text-weight-medium")
                ui.label(route.detail).classes("text-grey-6 text-xs")
                if route.origin:
                    _copy_value_row("Origin", route.origin)
                if route.warning:
                    ui.label(route.warning).classes("text-warning text-xs")

        ngrok = inventory.by_kind(AccessRouteKind.NGROK)
        if not ngrok:
            with ui.card().classes("w-full q-pa-sm"):
                ui.label("ngrok — Public tunnel (Advanced)").classes(
                    "text-sm text-weight-medium"
                )
                ui.label(
                    "ngrok is managed separately. Row-Bot authentication is "
                    "still required on its public endpoint."
                ).classes("text-grey-6 text-xs")


def _copy_value_row(label: str, value: str) -> None:
    with ui.row().classes("items-center gap-2 w-full no-wrap"):
        with ui.column().classes("gap-0").style("min-width: 0; flex: 1;"):
            ui.label(label).classes("text-grey-6 text-xs")
            ui.label(value).classes("text-primary text-xs").style(
                "word-break: break-all;"
            )
        ui.button(
            icon="content_copy",
            on_click=lambda: _copy_to_clipboard(value),
        ).props("flat dense round size=sm").tooltip("Copy")


def _copy_to_clipboard(value: str) -> None:
    safe_value = json.dumps(value)
    ui.run_javascript(f"navigator.clipboard.writeText({safe_value})")
    ui.notify("Copied", type="info")


__all__ = [
    "RemoteAccessActions",
    "RouteInventoryProvider",
    "StaleInvitationRouteError",
    "build_remote_access_settings_section",
]
