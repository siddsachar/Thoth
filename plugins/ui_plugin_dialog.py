"""Per-plugin configuration dialog.

Shows plugin details, API keys, settings, tools/skills,
and actions (update, uninstall).
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from nicegui import ui

logger = logging.getLogger(__name__)


def open_plugin_dialog(
    manifest: Any,
    *,
    on_change: Callable | None = None,
    on_uninstall: Callable | None = None,
) -> None:
    """Open a dialog for configuring a single plugin."""
    from plugins import state as plugin_state
    from plugins import registry as plugin_registry

    plugin_id = manifest.id

    with ui.dialog().props("persistent") as dlg, ui.card().classes(
        "w-full"
    ).style("min-width: 550px; max-width: 700px;"):

        # ── Header ───────────────────────────────────────────────────
        with ui.row().classes("w-full items-center no-wrap"):
            ui.label(f"{manifest.icon} {manifest.name}").classes(
                "text-h6 text-weight-bold"
            )
            ui.label(f"v{manifest.version}").classes("text-grey-6 text-sm")
            ui.space()
            ui.button(icon="close", on_click=dlg.close).props("flat round dense")

        author_parts = [f"by {manifest.author.name}"]
        if manifest.license:
            author_parts.append(manifest.license)
        ui.label("  •  ".join(author_parts)).classes("text-grey-6 text-sm")

        ui.separator().classes("q-my-sm")

        # ── Description ──────────────────────────────────────────────
        if manifest.description:
            ui.label("Description").classes("text-subtitle2 text-weight-medium")
            ui.label(manifest.description).classes("text-grey-7 text-sm q-mb-sm")

        # ── API Keys ─────────────────────────────────────────────────
        api_keys_spec = manifest.settings.get("api_keys", {})
        if api_keys_spec:
            ui.label("API Keys").classes("text-subtitle2 text-weight-medium q-mt-sm")
            key_inputs: dict[str, ui.input] = {}
            for key_name, key_info in api_keys_spec.items():
                label = key_info.get("label", key_name)
                placeholder = key_info.get("placeholder", "")
                required = key_info.get("required", False)
                configured = bool(plugin_state.get_plugin_secret(plugin_id, key_name))
                suffix = " *" if required else ""
                with ui.row().classes("w-full items-center gap-2 no-wrap"):
                    inp = ui.input(
                        f"{label}{suffix}",
                        value="",
                        placeholder=placeholder or ("Configured" if configured else ""),
                        password=True,
                        password_toggle_button=True,
                    ).classes("col")
                    if configured:
                        ui.button(
                            icon="delete",
                            on_click=lambda key=key_name: plugin_state.delete_plugin_secret(plugin_id, key),
                        ).props("flat round dense color=negative").tooltip("Clear saved API key")
                key_inputs[key_name] = inp

        # ── Config Settings ──────────────────────────────────────────
        config_spec = manifest.settings.get("config", {})
        config_inputs: dict[str, Any] = {}
        if config_spec:
            ui.label("Settings").classes("text-subtitle2 text-weight-medium q-mt-sm")
            for cfg_name, cfg_info in config_spec.items():
                label = cfg_info.get("label", cfg_name)
                cfg_type = cfg_info.get("type", "text")
                default = cfg_info.get("default", "")
                current = plugin_state.get_plugin_config(plugin_id, cfg_name, default)

                if cfg_type == "select":
                    options = cfg_info.get("options", [])
                    sel = ui.select(
                        label=label, options=options, value=current
                    ).classes("w-full")
                    config_inputs[cfg_name] = sel
                elif cfg_type == "number":
                    num = ui.number(
                        label=label, value=current,
                        min=cfg_info.get("min"), max=cfg_info.get("max"),
                    ).classes("w-full")
                    config_inputs[cfg_name] = num
                elif cfg_type == "boolean":
                    sw = ui.switch(label, value=bool(current))
                    config_inputs[cfg_name] = sw
                else:
                    txt = ui.input(label=label, value=str(current)).classes("w-full")
                    config_inputs[cfg_name] = txt

        # ── Tools ────────────────────────────────────────────────────
        tools = plugin_registry.get_plugin_tools(plugin_id)
        if tools:
            ui.label("Tools").classes("text-subtitle2 text-weight-medium q-mt-sm")
            for tool in tools:
                with ui.row().classes("items-center gap-2"):
                    ui.icon("build", size="xs").classes("text-grey-6")
                    ui.label(f"{tool.display_name}").classes("text-body2")
                    ui.label(f"— {tool.description}").classes(
                        "text-grey-6 text-sm"
                    ) if tool.description else None

        # ── Skills ───────────────────────────────────────────────────
        skills = plugin_registry.get_plugin_skills(plugin_id)
        if skills:
            ui.label("Skills").classes("text-subtitle2 text-weight-medium q-mt-sm")
            for skill in skills:
                with ui.row().classes("items-center gap-2"):
                    ui.icon("auto_fix_high", size="xs").classes("text-grey-6")
                    display = skill.get("display_name", skill.get("name", "?"))
                    desc = skill.get("description", "")
                    ui.label(display).classes("text-body2")
                    if desc:
                        ui.label(f"— {desc}").classes("text-grey-6 text-sm")

        ui.separator().classes("q-my-sm")

        # ── Action buttons ───────────────────────────────────────────
        with ui.row().classes("w-full justify-between items-center"):
            def _do_uninstall():
                dlg.close()
                if on_uninstall:
                    on_uninstall(plugin_id)

            ui.button(
                "Uninstall", icon="delete",
                on_click=_do_uninstall,
            ).props("flat color=negative size=sm")

            with ui.row().classes("gap-2"):
                ui.button("Cancel", on_click=dlg.close).props("flat")

                def _save():
                    # Save API keys
                    if api_keys_spec:
                        for key_name in api_keys_spec:
                            val = key_inputs[key_name].value or ""
                            if val:
                                plugin_state.set_plugin_secret(plugin_id, key_name, val)
                    # Save config
                    if config_spec:
                        for cfg_name, widget in config_inputs.items():
                            plugin_state.set_plugin_config(
                                plugin_id, cfg_name, widget.value
                            )

                    ui.notify(f"✅ {manifest.name} settings saved", type="positive")
                    dlg.close()
                    if on_change:
                        on_change()

                ui.button("Save", icon="save", on_click=_save).props("color=primary")

    dlg.open()
