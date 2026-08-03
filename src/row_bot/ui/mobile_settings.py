"""Mobile-safe settings sections for compact owner presentation."""

from __future__ import annotations

import logging
from typing import Any, Callable

from nicegui import run, ui

from row_bot.ui.timer_utils import defer_ui

logger = logging.getLogger(__name__)

_MOBILE_SETTINGS_ADAPTER_CSS = """
<style>
.row-bot-mobile-settings-section {
    width: 100%;
    display: flex;
    flex-direction: column;
    gap: 10px;
}
.row-bot-mobile-settings-card {
    width: 100%;
    box-sizing: border-box;
    border: 1px solid rgba(148, 163, 184, 0.18);
    border-radius: 8px;
    background: rgba(15, 23, 42, 0.34);
    padding: 10px;
}
.row-bot-mobile-settings-metrics {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px;
}
.row-bot-mobile-settings-metric {
    min-width: 0;
    border: 1px solid rgba(148, 163, 184, 0.18);
    border-radius: 8px;
    background: rgba(148, 163, 184, 0.08);
    padding: 8px;
}
.row-bot-mobile-provider-card {
    width: 100%;
    box-sizing: border-box;
    border-bottom: 1px solid rgba(148, 163, 184, 0.14);
    padding: 10px 0;
}
.row-bot-mobile-provider-card:last-child {
    border-bottom: 0;
}
.row-bot-mobile-provider-badges {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
}
.row-bot-mobile-provider-badges .q-badge {
    white-space: normal;
    overflow-wrap: anywhere;
}
.row-bot-mobile-desktop-only {
    width: 100%;
    box-sizing: border-box;
    display: flex;
    align-items: flex-start;
    gap: 8px;
    border: 1px solid rgba(88, 166, 255, 0.28);
    border-radius: 8px;
    background: rgba(88, 166, 255, 0.08);
    padding: 10px;
}
.row-bot-mobile-settings-toolbar {
    width: 100%;
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(112px, 0.42fr);
    gap: 8px;
}
.row-bot-mobile-settings-row {
    width: 100%;
    box-sizing: border-box;
    border-bottom: 1px solid rgba(148, 163, 184, 0.14);
    padding: 9px 0;
}
.row-bot-mobile-settings-row:last-child {
    border-bottom: 0;
}
.row-bot-mobile-settings-text {
    min-width: 0;
    overflow-wrap: anywhere;
}
.row-bot-mobile-settings-text .text-grey-6 {
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
}
.row-bot-mobile-settings-actions {
    flex: 0 0 auto;
}
</style>
"""


def _ensure_mobile_settings_adapter_css() -> None:
    ui.html(_MOBILE_SETTINGS_ADAPTER_CSS, sanitize=False)


def _source_label(source: str) -> str:
    labels = {
        "environment": "Environment",
        "keyring": "Keyring",
        "session": "Session",
        "legacy_plaintext": "Legacy plaintext",
        "api_keys": "Saved API key",
        "external_cli": "External CLI",
        "external_cli_detected": "External CLI detected",
        "oauth_device": "ChatGPT sign-in",
        "oauth_pkce": "Row-Bot OAuth",
        "no_auth": "No key required",
        "local_daemon": "Local daemon",
        "not_running": "Not running",
    }
    return labels.get(str(source or ""), str(source or "Connected"))


def _status_style(card: dict[str, Any]) -> tuple[str, str, str]:
    configured = bool(card.get("configured"))
    source = str(card.get("source") or "")
    provider_id = str(card.get("provider_id") or "")
    if provider_id in {"codex", "claude_subscription", "xai_oauth"} and configured and not card.get("runtime_enabled"):
        return "#f59e0b", "Reconnect", "warning"
    if configured:
        if source == "external_cli":
            return "#38bdf8", "Referenced", "info"
        return "#22c55e", "Connected", "positive"
    if source == "external_cli_detected":
        return "#38bdf8", "Detected", "info"
    if source == "not_running":
        return "#f59e0b", "Not running", "warning"
    return "#71717a", "Not connected", "grey"


def _metadata_parts(card: dict[str, Any]) -> list[str]:
    parts: list[str] = []
    source = str(card.get("source") or "")
    if source:
        parts.append(_source_label(source))
    plan_type = str(card.get("plan_type") or "")
    if plan_type:
        parts.append(f"{plan_type} plan")
    model_count = card.get("model_count")
    if model_count is not None:
        parts.append(f"{model_count} models")
    elif card.get("configured") or card.get("runtime_enabled"):
        parts.append("catalog count unknown")
    chat_count = int(card.get("chat_count") or 0)
    media_count = int(card.get("media_count") or 0)
    if chat_count:
        parts.append(f"{chat_count} chat")
    if media_count:
        parts.append(f"{media_count} media")
    return parts


def _metric(label: str, value: int | str) -> None:
    with ui.element("div").classes("row-bot-mobile-settings-metric"):
        ui.label(str(value)).classes("text-weight-bold")
        ui.label(label).classes("text-grey-6 text-xs")


def _desktop_only_notice(title: str, body: str) -> None:
    with ui.element("div").classes("row-bot-mobile-desktop-only"):
        ui.icon("desktop_windows").classes("text-primary q-mt-xs")
        with ui.column().classes("gap-1").style("min-width: 0;"):
            ui.label(title).classes("text-sm text-weight-medium")
            ui.label(body).classes("text-grey-6 text-xs")


def build_mobile_providers_settings(*, on_change: Callable[[], object] | None = None) -> None:
    """Render provider settings in a phone-safe stacked layout."""

    from row_bot.providers.status import provider_status_cards
    from row_bot.ui.provider_settings import _open_provider_api_key_dialog

    _ensure_mobile_settings_adapter_css()
    ui.label("Providers").classes("text-h6")
    ui.label("Connection status and safe credential summaries. Secret values are never shown.").classes(
        "text-grey-6 text-sm"
    )
    container = ui.column().classes("row-bot-mobile-settings-section")

    def _reload() -> None:
        defer_ui(_load)

    def _open_key(card: dict[str, Any]) -> None:
        _open_provider_api_key_dialog(card, on_change=_reload)

    def _render_provider(card: dict[str, Any]) -> None:
        dot_color, status_label, badge_color = _status_style(card)
        metadata = _metadata_parts(card)
        fingerprint = str(card.get("fingerprint") or "")
        account_hash = str(card.get("account_id_hash") or card.get("user_hash") or "")
        client_fingerprint = str(card.get("oauth_client_id_fingerprint") or "")
        with ui.element("div").classes("row-bot-mobile-provider-card"):
            with ui.row().classes("w-full items-start gap-2 no-wrap"):
                ui.label(str(card.get("icon") or "AI")).classes("text-lg").style("width: 24px; text-align: center;")
                with ui.column().classes("gap-1").style("min-width: 0; flex: 1;"):
                    with ui.row().classes("w-full items-center gap-2 no-wrap"):
                        ui.element("span").style(
                            f"width: 8px; height: 8px; border-radius: 999px; background: {dot_color}; flex: 0 0 auto;"
                        )
                        ui.label(str(card.get("display_name") or card.get("provider_id") or "Provider")).classes(
                            "text-sm text-weight-medium ellipsis"
                        )
                    ui.label(" - ".join(metadata) if metadata else "Add credentials to enable this provider").classes(
                        "text-grey-6 text-xs"
                    ).style("overflow-wrap: anywhere;")
                    with ui.element("div").classes("row-bot-mobile-provider-badges"):
                        ui.badge(status_label, color=badge_color).props("outline dense")
                        ui.badge(str(card.get("risk_label") or "provider"), color="grey").props("outline dense")
                        if fingerprint:
                            ui.badge(fingerprint, color="blue-grey").props("outline dense").tooltip(
                                "Credential fingerprint"
                            )
                        if account_hash:
                            ui.badge(account_hash, color="blue-grey").props("outline dense").tooltip(
                                "Account fingerprint"
                            )
                        if client_fingerprint:
                            ui.badge(client_fingerprint, color="blue-grey").props("outline dense").tooltip(
                                "OAuth client fingerprint"
                            )
                with ui.button(icon="more_vert").props("flat dense round"):
                    with ui.menu().classes("q-pa-xs"):
                        auth_methods = {str(item) for item in (card.get("auth_methods") or [])}
                        if "api_key" in auth_methods:
                            ui.button(
                                "Manage API key",
                                icon="key",
                                on_click=lambda _, c=card: _open_key(c),
                            ).props("flat dense no-caps")
                        ui.button(
                            "Refresh status",
                            icon="refresh",
                            on_click=_reload,
                        ).props("flat dense no-caps")
                        if "api_key" not in auth_methods:
                            ui.label("Sign-in and runtime tests are desktop settings actions.").classes(
                                "text-grey-6 text-xs q-pa-sm"
                            )

    def _render(cards: list[dict[str, Any]]) -> None:
        container.clear()
        with container:
            connected = sum(1 for card in cards if card.get("configured"))
            local = sum(1 for card in cards if card.get("group") == "Local")
            api = sum(1 for card in cards if card.get("group") == "API Providers")
            subscription = sum(1 for card in cards if card.get("group") == "Subscription Accounts")
            with ui.element("div").classes("row-bot-mobile-settings-metrics"):
                _metric("connected", connected)
                _metric("local", local)
                _metric("API", api)
                _metric("subscription", subscription)
            for group_name in ("Local", "Subscription Accounts", "API Providers", "Custom Endpoints"):
                group_cards = [card for card in cards if card.get("group") == group_name]
                if not group_cards:
                    continue
                with ui.element("div").classes("row-bot-mobile-settings-card"):
                    ui.label(group_name).classes("text-grey-5 text-xs text-uppercase q-mb-xs")
                    for card in group_cards:
                        _render_provider(card)

    async def _load() -> None:
        container.clear()
        with container:
            with ui.row().classes("items-center gap-2 text-grey-6 text-sm"):
                ui.spinner(size="sm")
                ui.label("Checking provider status...")
        try:
            cards = await run.io_bound(provider_status_cards)
        except Exception as exc:
            logger.warning("Mobile provider status failed: %s", exc, exc_info=True)
            container.clear()
            with container:
                ui.label(f"Could not load provider status: {exc}").classes("text-warning text-sm")
                ui.button("Retry", icon="refresh", on_click=_reload).props("flat dense no-caps")
            return
        _render(cards)

    defer_ui(_load)


def _skill_source_label(skill: Any, hub_records: dict[str, Any]) -> str:
    source = str(getattr(skill, "source", "") or "")
    if source == "bundled":
        return "Bundled"
    if getattr(skill, "name", "") in hub_records:
        return "Public"
    if source == "user":
        return "Custom"
    return source.title() or "Skill"


def build_mobile_skills_settings() -> None:
    """Render mobile-safe skill management without opening Skills Hub."""

    _ensure_mobile_settings_adapter_css()
    ui.label("Skills").classes("text-h6")
    ui.label("Installed skill availability and pinned defaults.").classes("text-grey-6 text-sm")
    try:
        import row_bot.skills as skills_mod
        from row_bot.skills_hub.provenance import load_records

        if not skills_mod.skills_loaded():
            skills_mod.load_skills()
        hub_records = load_records()
    except Exception as exc:
        logger.warning("Mobile skills summary failed: %s", exc, exc_info=True)
        ui.label(f"Could not load skills: {exc}").classes("text-warning text-sm")
        return

    _desktop_only_notice(
        "Skills Hub is desktop-only in Mobile V1",
        "Browse, install, and create skills from desktop. Mobile keeps local enable, disable, and pin controls.",
    )

    with ui.element("div").classes("row-bot-mobile-settings-toolbar"):
        search_input = ui.input(placeholder="Search skills").props("outlined dense clearable")
        filter_select = ui.select(
            ["All", "Enabled", "Pinned", "Custom", "Public"],
            value="All",
            label="Filter",
        ).props("outlined dense")

    metrics_col = ui.element("div").classes("row-bot-mobile-settings-metrics")
    skills_card = ui.element("div").classes("row-bot-mobile-settings-card")

    def _render() -> None:
        all_skills = skills_mod.get_manual_skills()
        enabled = [skill for skill in all_skills if skills_mod.is_enabled(skill.name)]
        pinned = [skill for skill in all_skills if skills_mod.is_pinned(skill.name)]
        query = str(search_input.value or "").strip().lower()
        filter_value = str(filter_select.value or "All")

        def _matches(skill: Any) -> bool:
            source = _skill_source_label(skill, hub_records)
            haystack = " ".join(
                [
                    str(getattr(skill, "name", "")),
                    str(getattr(skill, "display_name", "")),
                    str(getattr(skill, "description", "")),
                    " ".join(str(tag) for tag in (getattr(skill, "tags", []) or [])),
                    source,
                ]
            ).lower()
            if query and query not in haystack:
                return False
            if filter_value == "Enabled":
                return skills_mod.is_enabled(skill.name)
            if filter_value == "Pinned":
                return skills_mod.is_pinned(skill.name)
            if filter_value == "Custom":
                return str(getattr(skill, "source", "") or "") == "user"
            if filter_value == "Public":
                return skill.name in hub_records
            return True

        visible = [skill for skill in all_skills if _matches(skill)]
        visible.sort(key=lambda skill: str(getattr(skill, "display_name", "") or skill.name).lower())

        metrics_col.clear()
        with metrics_col:
            _metric("available", len(all_skills))
            _metric("enabled", len(enabled))
            _metric("pinned", len(pinned))
            _metric("custom", sum(1 for skill in all_skills if getattr(skill, "source", "") == "user"))

        skills_card.clear()
        with skills_card:
            ui.label("Installed skills").classes("text-subtitle2")
            if not visible:
                ui.label("No skills match the current filter.").classes("text-grey-6 text-sm")
                return
            for skill in visible[:80]:
                name = str(skill.name)
                is_enabled = skills_mod.is_enabled(name)
                is_pinned = skills_mod.is_pinned(name)
                source = _skill_source_label(skill, hub_records)

                def _set_enabled(e: Any, skill_name: str = name) -> None:
                    skills_mod.set_enabled(skill_name, bool(e.value))
                    _render()

                def _toggle_pin(skill_name: str = name) -> None:
                    try:
                        skills_mod.set_pinned(skill_name, not skills_mod.is_pinned(skill_name))
                    except Exception as exc:
                        logger.warning("Could not update mobile skill pin: %s", exc, exc_info=True)
                        ui.notify(str(exc), type="warning")
                    _render()

                with ui.element("div").classes("row-bot-mobile-settings-row"):
                    with ui.row().classes("w-full items-start gap-2 no-wrap"):
                        ui.switch("", value=is_enabled, on_change=_set_enabled).props("dense")
                        ui.button(
                            icon="push_pin",
                            on_click=lambda _=None, n=name: _toggle_pin(n),
                        ).props(
                            "flat dense round size=sm "
                            f"color={'primary' if is_pinned else 'grey'}"
                        ).tooltip("Pinned skills start active in new chats and workflows.")
                        with ui.column().classes("row-bot-mobile-settings-text gap-1").style("flex: 1;"):
                            with ui.row().classes("w-full items-center gap-1 no-wrap"):
                                ui.label(
                                    f"{getattr(skill, 'icon', '') or '-'} "
                                    f"{getattr(skill, 'display_name', '') or name}"
                                ).classes("text-sm text-weight-medium ellipsis")
                            desc = str(getattr(skill, "description", "") or "No description.")
                            ui.label(desc).classes("text-grey-6 text-xs")
                            with ui.element("div").classes("row-bot-mobile-provider-badges"):
                                ui.badge("enabled" if is_enabled else "disabled", color="green" if is_enabled else "grey").props(
                                    "outline dense"
                                )
                                if is_pinned:
                                    ui.badge("pinned", color="primary").props("outline dense")
                                ui.badge(source, color="blue-grey").props("outline dense")
            if len(visible) > 80:
                ui.label(f"{len(visible) - 80} more skills match. Narrow the search to manage them.").classes(
                    "text-grey-6 text-xs"
                )

    search_input.on("update:model-value", lambda _: _render())
    filter_select.on("update:model-value", lambda _: _render())
    _render()


def build_mobile_plugins_settings() -> None:
    """Render mobile-safe installed plugin management without opening the marketplace."""

    _ensure_mobile_settings_adapter_css()
    ui.label("Plugins").classes("text-h6")
    ui.label("Installed plugin status and enablement.").classes("text-grey-6 text-sm")
    try:
        from row_bot.plugins import loader as plugin_loader
        from row_bot.plugins import registry as plugin_registry
        from row_bot.plugins import state as plugin_state
        from row_bot.plugins.ui_settings import (
            _can_enable_plugin,
            _get_missing_secrets,
            _get_missing_settings,
            _manifest_counts,
            _plugin_status,
        )

        manifests = sorted(plugin_registry.get_loaded_manifests(), key=lambda item: item.name.lower())
        summary = plugin_loader.get_load_summary()
    except Exception as exc:
        logger.warning("Mobile plugins summary failed: %s", exc, exc_info=True)
        ui.label(f"Could not load plugins: {exc}").classes("text-warning text-sm")
        return

    _desktop_only_notice(
        "Plugin Marketplace is desktop-only in Mobile V1",
        "Browse, install, configure, test, update, and uninstall plugins from desktop. Mobile keeps installed plugin enable and disable controls.",
    )

    with ui.element("div").classes("row-bot-mobile-settings-toolbar"):
        search_input = ui.input(placeholder="Search plugins").props("outlined dense clearable")
        filter_select = ui.select(
            ["All", "Enabled", "Disabled", "Setup needed"],
            value="All",
            label="Filter",
        ).props("outlined dense")

    metrics_col = ui.element("div").classes("row-bot-mobile-settings-metrics")
    plugins_card = ui.element("div").classes("row-bot-mobile-settings-card")

    def _toggle_plugin(manifest: Any, enabled: bool) -> None:
        if enabled:
            ok, reason = _can_enable_plugin(manifest)
            if not ok:
                ui.notify(reason, type="warning")
                return
        plugin_state.set_plugin_enabled(manifest.id, enabled)
        try:
            plugin_loader.refresh_plugin_runtime(f"mobile plugin {'enable' if enabled else 'disable'}")
        except Exception:
            logger.debug("Could not refresh plugin runtime after mobile plugin toggle", exc_info=True)
        ui.notify(f"Plugin {manifest.id} {'enabled' if enabled else 'disabled'}", type="info")
        _render()

    def _render() -> None:
        enabled_count = sum(1 for manifest in manifests if plugin_state.is_plugin_enabled(manifest.id))
        query = str(search_input.value or "").strip().lower()
        filter_value = str(filter_select.value or "All")

        def _matches(manifest: Any) -> bool:
            enabled = plugin_state.is_plugin_enabled(manifest.id)
            missing_setup = bool(_get_missing_settings(manifest) or _get_missing_secrets(manifest))
            haystack = " ".join(
                [
                    str(getattr(manifest, "id", "")),
                    str(getattr(manifest, "name", "")),
                    str(getattr(manifest, "description", "")),
                ]
            ).lower()
            if query and query not in haystack:
                return False
            if filter_value == "Enabled":
                return enabled
            if filter_value == "Disabled":
                return not enabled
            if filter_value == "Setup needed":
                return missing_setup
            return True

        visible = [manifest for manifest in manifests if _matches(manifest)]
        metrics_col.clear()
        with metrics_col:
            _metric("installed", len(manifests))
            _metric("enabled", enabled_count)
            _metric("loaded", int(summary.get("loaded", 0) or 0))
            _metric("failed", int(summary.get("failed", 0) or 0))

        plugins_card.clear()
        with plugins_card:
            ui.label("Installed plugins").classes("text-subtitle2")
            if not visible:
                ui.label("No plugins match the current filter.").classes("text-grey-6 text-sm")
                return
            for manifest in visible[:80]:
                enabled = plugin_state.is_plugin_enabled(manifest.id)
                status_label, status_color = _plugin_status(manifest, enabled=enabled)
                missing_setup = _get_missing_settings(manifest) + _get_missing_secrets(manifest)
                counts = _manifest_counts(manifest)
                with ui.element("div").classes("row-bot-mobile-settings-row"):
                    with ui.row().classes("w-full items-start gap-2 no-wrap"):
                        ui.icon(getattr(manifest, "icon", "extension") or "extension").classes("text-primary q-mt-xs")
                        with ui.column().classes("row-bot-mobile-settings-text gap-1").style("flex: 1;"):
                            ui.label(getattr(manifest, "name", manifest.id)).classes("text-sm text-weight-medium")
                            ui.label(getattr(manifest, "description", "") or manifest.id).classes(
                                "text-grey-6 text-xs"
                            )
                            with ui.element("div").classes("row-bot-mobile-provider-badges"):
                                ui.badge(status_label, color=status_color).props("outline dense")
                                ui.badge(f"v{getattr(manifest, 'version', '')}", color="blue-grey").props("outline dense")
                                for label, count in (
                                    ("tools", counts["native_tools"]),
                                    ("MCP", counts["mcp_servers"]),
                                    ("channels", counts["channels"]),
                                    ("skills", counts["skills"]),
                                ):
                                    if count:
                                        ui.badge(f"{count} {label}", color="blue-grey").props("outline dense")
                            if missing_setup:
                                ui.label("Setup needed: " + ", ".join(missing_setup)).classes("text-warning text-xs")
                        button_label = "Disable" if enabled else "Enable"
                        ui.button(
                            button_label,
                            icon="toggle_on" if enabled else "toggle_off",
                            on_click=lambda _=None, m=manifest, value=not enabled: _toggle_plugin(m, value),
                        ).props(
                            ("flat dense no-caps color=negative")
                            if enabled
                            else "flat dense no-caps color=primary"
                        ).classes("row-bot-mobile-settings-actions")
            if len(visible) > 80:
                ui.label(f"{len(visible) - 80} more plugins match. Narrow the search to manage them.").classes(
                    "text-grey-6 text-xs"
                )

    search_input.on("update:model-value", lambda _: _render())
    filter_select.on("update:model-value", lambda _: _render())
    _render()
