"""Plugins Settings tab — card grid for installed plugins.

Called from ``ui/settings.py`` as ``_build_plugins_tab()``.
"""

from __future__ import annotations

import logging
from typing import Callable

from nicegui import ui

logger = logging.getLogger(__name__)


def build_plugins_tab(
    *,
    on_browse_marketplace: Callable | None = None,
) -> None:
    """Build the Plugins tab content inside the settings dialog."""
    from plugins import state as plugin_state
    from plugins import registry as plugin_registry
    from plugins import loader as plugin_loader

    ui.label("🔌 Plugins").classes("text-h6")
    ui.label(
        "Manage installed plugins. Browse the marketplace to discover new ones."
    ).classes("text-grey-6 text-sm")
    ui.separator().classes("q-my-md")

    # ── Top action bar ───────────────────────────────────────────────────
    with ui.row().classes("w-full justify-end q-mb-md gap-2"):
        if on_browse_marketplace:
            ui.button(
                "Browse Marketplace", icon="shopping_cart",
                on_click=on_browse_marketplace,
            ).props("color=primary outline")
        ui.button(
            "Reload Plugins", icon="refresh",
            on_click=lambda: _reload_and_refresh(),
        ).props("outline")

    # ── Plugin cards container ───────────────────────────────────────────
    cards_container = ui.column().classes("w-full gap-2")

    def _refresh_cards():
        cards_container.clear()
        manifests = plugin_registry.get_loaded_manifests()
        def _is_custom_tool_manifest(manifest) -> bool:
            tags = set(getattr(manifest, "tags", []) or [])
            return bool({"custom-tool", "tool-capsule"} & tags)

        capsule_manifests = [
            manifest for manifest in manifests
            if _is_custom_tool_manifest(manifest)
        ]
        regular_manifests = [
            manifest for manifest in manifests
            if not _is_custom_tool_manifest(manifest)
        ]

        if not manifests:
            with cards_container:
                with ui.column().classes("w-full items-center q-pa-lg"):
                    ui.icon("extension_off", size="64px").classes("text-grey-4")
                    ui.label("No plugins installed").classes(
                        "text-grey-5 text-h6 q-mt-sm"
                    )
                    ui.label(
                        "Browse the marketplace to discover and install plugins."
                    ).classes("text-grey-5 text-sm")
                    if on_browse_marketplace:
                        ui.button(
                            "Browse Marketplace", icon="shopping_cart",
                            on_click=on_browse_marketplace,
                        ).props("color=primary q-mt-md")
            return

        with cards_container:
            for manifest in regular_manifests:
                _build_plugin_card(manifest, _refresh_cards)
            if capsule_manifests:
                _build_tool_capsules_section(capsule_manifests, _refresh_cards)

    def _build_plugin_card(manifest, refresh_fn):
        plugin_id = manifest.id
        enabled = plugin_state.is_plugin_enabled(plugin_id)
        tools = plugin_registry.get_plugin_tools(plugin_id)
        skills = plugin_registry.get_plugin_skills(plugin_id)

        # Check for missing required API keys
        missing_keys = _get_missing_keys(manifest)

        with ui.card().classes("w-full q-pa-sm"):
            with ui.row().classes("w-full items-center no-wrap"):
                ui.switch(
                    "",
                    value=enabled,
                    on_change=lambda e, pid=plugin_id: _toggle_plugin(
                        pid, e.value, refresh_fn
                    ),
                )
                ui.label(f"{manifest.icon} {manifest.name}").classes(
                    "text-body1 text-weight-medium"
                )
                ui.space()
                ui.label(f"v{manifest.version}").classes("text-grey-5 text-sm")

            # Description
            ui.label(manifest.description).classes(
                "text-grey-6 text-sm q-pl-lg"
            )

            # Stats + warnings row
            with ui.row().classes("q-pl-lg q-mt-xs gap-2 items-center"):
                if tools:
                    ui.badge(
                        f"🔧 {len(tools)} tool{'s' if len(tools) != 1 else ''}",
                        color="blue-grey",
                    ).props("outline")
                if skills:
                    ui.badge(
                        f"📜 {len(skills)} skill{'s' if len(skills) != 1 else ''}",
                        color="teal",
                    ).props("outline")

                if missing_keys:
                    ui.badge(
                        f"⚠️ {len(missing_keys)} missing key{'s' if len(missing_keys) != 1 else ''}",
                        color="warning",
                    ).props("outline").tooltip(
                        "Missing: " + ", ".join(missing_keys)
                    )

                ui.space()

                # Configure button
                ui.button(
                    "Configure", icon="settings",
                    on_click=lambda _, m=manifest: _open_config(m, refresh_fn),
                ).props("flat dense size=sm")

    def _toggle_plugin(plugin_id: str, enabled: bool, refresh_fn):
        from agent import clear_agent_cache

        plugin_state.set_plugin_enabled(plugin_id, enabled)
        clear_agent_cache()
        label = "enabled" if enabled else "disabled"
        ui.notify(f"Plugin {plugin_id} {label}", type="info")
        refresh_fn()

    def _open_config(manifest, refresh_fn):
        from plugins.ui_plugin_dialog import open_plugin_dialog

        def _after_uninstall(plugin_id):
            _uninstall_plugin(plugin_id, refresh_fn)

        open_plugin_dialog(
            manifest,
            on_change=refresh_fn,
            on_uninstall=_after_uninstall,
        )

    def _uninstall_plugin(plugin_id: str, refresh_fn):
        """Uninstall a plugin — remove files, state, and registry entries."""
        from plugins import registry as reg

        with ui.dialog() as confirm_dlg, ui.card():
            ui.label(f"Uninstall plugin '{plugin_id}'?").classes("text-body1")
            ui.label(
                "This will remove the plugin files and all saved settings."
            ).classes("text-grey-6 text-sm")
            with ui.row().classes("w-full justify-end gap-2 q-mt-md"):
                ui.button("Cancel", on_click=confirm_dlg.close).props("flat")

                def _do_uninstall():
                    try:
                        import shutil
                        plugin_dir = plugin_loader.PLUGINS_DIR / plugin_id
                        if plugin_dir.exists():
                            shutil.rmtree(plugin_dir)
                        reg.unregister_plugin(plugin_id)
                        plugin_state.remove_plugin_state(plugin_id)
                        ui.notify(
                            f"✅ Plugin '{plugin_id}' uninstalled",
                            type="positive",
                        )
                    except Exception as exc:
                        logger.error("Uninstall error: %s", exc, exc_info=True)
                        ui.notify(
                            f"Error uninstalling: {exc}", type="negative"
                        )
                    confirm_dlg.close()
                    refresh_fn()

                ui.button(
                    "Uninstall", on_click=_do_uninstall
                ).props("color=negative")
        confirm_dlg.open()

    def _build_tool_capsules_section(capsule_manifests, refresh_fn):
        from developer import tool_capsules

        ui.separator().classes("q-my-md")
        with ui.row().classes("items-center gap-2"):
            ui.icon("extension").classes("text-blue-4")
            ui.label("Custom Tools").classes("text-body1 text-weight-bold")
            ui.badge(f"{len(capsule_manifests)} promoted", color="blue-grey").props("outline")
        ui.label(
            "Developer-created Custom Tools promoted into Thoth's normal plugin tool surface."
        ).classes("text-grey-6 text-sm q-mb-sm")

        capsules_by_plugin = {
            capsule.promoted_plugin_id: capsule
            for capsule in tool_capsules.list_promoted_capsules()
        }
        for manifest in capsule_manifests:
            plugin_id = manifest.id
            capsule = capsules_by_plugin.get(plugin_id)
            enabled = plugin_state.is_plugin_enabled(plugin_id)
            tools = plugin_registry.get_plugin_tools(plugin_id)

            with ui.card().classes("w-full q-pa-sm"):
                with ui.row().classes("w-full items-center no-wrap gap-2"):
                    ui.switch(
                        "",
                        value=enabled,
                        on_change=lambda e, pid=plugin_id: _toggle_plugin(
                            pid, e.value, refresh_fn
                        ),
                    )
                    ui.icon("extension").classes("text-blue-4")
                    ui.label(manifest.name).classes("text-body1 text-weight-medium")
                    ui.space()
                    ui.badge(f"{len(tools)} tool{'s' if len(tools) != 1 else ''}", color="blue-grey").props("outline")
                    ui.button(
                        "Remove", icon="delete",
                        on_click=lambda _, cid=(capsule.id if capsule else ""): _remove_capsule_tool(cid, refresh_fn),
                    ).props("flat dense color=negative")
                ui.label(manifest.description).classes("text-grey-6 text-sm q-pl-lg")
                if capsule:
                    ui.label(f"Source: {capsule.source_url}").classes("text-grey-6 text-xs q-pl-lg")
                    ui.label(f"Path: {capsule.installed_path}").classes("text-grey-6 text-xs q-pl-lg")

    def _remove_capsule_tool(capsule_id: str, refresh_fn):
        if not capsule_id:
            ui.notify("Custom Tool metadata was not found", type="warning")
            return

        with ui.dialog() as confirm_dlg, ui.card():
            ui.label("Remove Custom Tool from chat tools?").classes("text-body1")
            ui.label(
                "This removes the plugin-style tool from Thoth. The source folder is not deleted."
            ).classes("text-grey-6 text-sm")
            with ui.row().classes("w-full justify-end gap-2 q-mt-md"):
                ui.button("Cancel", on_click=confirm_dlg.close).props("flat")

                def _do_remove():
                    try:
                        from developer.tool_capsules import remove_promoted_capsule_tool
                        from agent import clear_agent_cache

                        remove_promoted_capsule_tool(capsule_id)
                        clear_agent_cache()
                        ui.notify("Custom Tool removed from plugin tools", type="positive")
                    except Exception as exc:
                        logger.error("Custom Tool removal error: %s", exc, exc_info=True)
                        ui.notify(f"Error removing Custom Tool: {exc}", type="negative")
                    confirm_dlg.close()
                    refresh_fn()

                ui.button("Remove", on_click=_do_remove).props("color=negative")
        confirm_dlg.open()

    async def _reload_and_refresh():
        import asyncio
        from plugins import loader
        from plugins import registry as reg
        from agent import clear_agent_cache

        # Clear current registry
        for m in list(reg.get_loaded_manifests()):
            reg.unregister_plugin(m.id)

        # Reload
        results = await asyncio.to_thread(loader.load_plugins)
        # Clear agent cache so new plugin tools are picked up
        clear_agent_cache()
        loaded = sum(1 for r in results if r.success)
        failed = sum(1 for r in results if not r.success)
        msg = f"Loaded {loaded} plugin{'s' if loaded != 1 else ''}"
        if failed:
            msg += f", {failed} failed"
        ui.notify(msg, type="positive" if not failed else "warning")
        _refresh_cards()

    _refresh_cards()


# ── Helpers ──────────────────────────────────────────────────────────────────
def _get_missing_keys(manifest) -> list[str]:
    """Return list of required API key names that are not set."""
    from plugins import state as plugin_state

    missing = []
    api_keys = manifest.settings.get("api_keys", {})
    for key_name, key_info in api_keys.items():
        if key_info.get("required", False):
            val = plugin_state.get_plugin_secret(manifest.id, key_name)
            if not val:
                missing.append(key_info.get("label", key_name))
    return missing
