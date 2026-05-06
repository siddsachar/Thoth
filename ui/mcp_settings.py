"""Settings UI for external MCP servers."""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from nicegui import run, ui

from ui.timer_utils import defer_ui

from mcp_client import config as mcp_config
from mcp_client.conflicts import conflicts_for_entry, conflicts_for_server, requires_manual_tool_selection, unique_server_name
from mcp_client.marketplace import MarketplaceEntry, MarketplaceSearchResult, entry_to_server_config, search_marketplace_with_status
from mcp_client.requirements import RuntimeRequirement, check_server_requirements, install_managed_runtime, requirements_for_install
from mcp_client.runtime import get_status_summary, probe_server, refresh_server, discover_enabled_servers

_OPEN_SERVER_EXPANSIONS: set[str] = set()


def _parse_json_mapping(raw: str, label: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} must be valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{label} must be a JSON object")
    return data


def _split_args(raw: str) -> list[str]:
    return [part for part in re.split(r"\s+", (raw or "").strip()) if part]


def _server_from_form(name: str, transport: str, command: str, args: str, url: str,
                      env: str, headers: str, timeout: Any, output_limit: Any) -> dict[str, Any]:
    if not (name or "").strip():
        raise ValueError("Server name is required")
    cfg = {
        "name": (name or "").strip(),
        "enabled": False,
        "transport": transport,
        "command": (command or "").strip(),
        "args": _split_args(args),
        "url": (url or "").strip(),
        "env": _parse_json_mapping(env, "Environment"),
        "headers": _parse_json_mapping(headers, "Headers"),
        "connect_timeout": float(timeout or 30),
        "tool_timeout": float(timeout or 120),
        "output_limit": int(output_limit or 24000),
    }
    if cfg["transport"] == "stdio" and not cfg["command"]:
        raise ValueError("stdio servers require a command")
    if cfg["transport"] != "stdio" and not cfg["url"]:
        raise ValueError("remote MCP servers require a URL")
    return cfg


def _apply_probe_defaults(server_cfg: dict[str, Any], probe: dict[str, Any]) -> dict[str, Any]:
    enabled: dict[str, bool] = {}
    approvals: list[str] = []
    catalog: dict[str, dict[str, Any]] = {}
    manual_select = requires_manual_tool_selection(str(server_cfg.get("name") or ""), server_cfg)
    for tool in probe.get("tools") or []:
        tool_name = tool.get("name")
        if not tool_name:
            continue
        destructive = bool(tool.get("destructive"))
        enabled[tool_name] = False if manual_select else not destructive
        if destructive:
            approvals.append(tool_name)
        catalog[tool_name] = {
            "name": tool_name,
            "description": str(tool.get("description") or ""),
            "destructive": destructive,
            "requires_approval": bool(tool.get("requires_approval") or destructive),
            "input_schema": tool.get("input_schema") or {},
        }
    server_cfg.setdefault("tools", {})["enabled"] = enabled
    server_cfg.setdefault("tools", {})["require_approval"] = approvals
    server_cfg.setdefault("tools", {})["catalog"] = catalog
    return server_cfg


def _import_payload(raw_text: str) -> int:
    data = json.loads(raw_text)
    if not isinstance(data, dict):
        raise ValueError("Import must be a JSON object")
    servers = data.get("mcpServers") or data.get("servers") or data
    if not isinstance(servers, dict):
        raise ValueError("No server map found in import")
    count = 0
    for name, server in servers.items():
        if not isinstance(server, dict):
            continue
        cfg = dict(server)
        cfg["enabled"] = False
        if "transport" not in cfg:
            cfg["transport"] = "streamable_http" if cfg.get("url") else "stdio"
        mcp_config.upsert_server(str(name), cfg)
        count += 1
    return count


def _source_badges(source: dict[str, Any]) -> list[tuple[str, str]]:
    badges: list[tuple[str, str]] = []
    category = str(source.get("category") or "").strip()
    trust = str(source.get("trust_tier") or source.get("classification") or "").strip()
    risk = str(source.get("risk_level") or "").strip().lower()
    if category:
        badges.append((category, "blue"))
    if trust:
        badges.append((trust.replace("_", " "), "green" if "official" in trust else "grey"))
    if risk:
        badges.append((f"{risk} risk", "red" if risk == "high" else "orange" if risk == "medium" else "green"))
    if source.get("not_verified_by_thoth"):
        badges.append(("not audited by Thoth", "grey"))
    return badges


def _configured_tool_rows(server_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    tools_cfg = server_cfg.get("tools", {}) if isinstance(server_cfg.get("tools"), dict) else {}
    enabled_map = dict(tools_cfg.get("enabled") or {})
    approvals = set(tools_cfg.get("require_approval") or [])
    catalog = tools_cfg.get("catalog") if isinstance(tools_cfg.get("catalog"), dict) else {}
    names = set(enabled_map) | approvals | set(catalog) | set(tools_cfg.get("include") or [])
    names -= set(tools_cfg.get("exclude") or [])
    rows: list[dict[str, Any]] = []
    for name in sorted(names):
        saved = catalog.get(name) if isinstance(catalog.get(name), dict) else {}
        destructive = bool(saved.get("destructive") or name in approvals)
        rows.append({
            "name": name,
            "enabled": bool(enabled_map.get(name, False)),
            "description": str(saved.get("description") or "No description available from the last test."),
            "destructive": destructive,
            "requires_approval": bool(saved.get("requires_approval") or destructive or name in approvals),
            "input_schema": saved.get("input_schema") or {},
            "configured_only": True,
        })
    return rows


def _schema_summary(schema: dict[str, Any] | None) -> str:
    if not isinstance(schema, dict):
        return ""
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        return ""
    required = set(schema.get("required") or [])
    parts: list[str] = []
    for name, spec in list(properties.items())[:6]:
        if not isinstance(spec, dict):
            parts.append(str(name))
            continue
        type_name = spec.get("type")
        if isinstance(type_name, list):
            type_name = next((item for item in type_name if item != "null"), type_name[0] if type_name else "value")
        label = f"{name}: {type_name or 'value'}"
        if name in required:
            label += "*"
        parts.append(label)
    suffix = "..." if len(properties) > 6 else ""
    return "Inputs: " + ", ".join(parts) + suffix


def _display_tool_rows(server_cfg: dict[str, Any], catalog_tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = {tool["name"]: tool for tool in _configured_tool_rows(server_cfg) if tool.get("name")}
    for tool in catalog_tools or []:
        name = tool.get("name")
        if not name:
            continue
        merged = dict(rows.get(name, {}))
        merged.update(tool)
        merged["configured_only"] = False
        rows[name] = merged
    return [rows[name] for name in sorted(rows)]


def _has_pending_enabled_servers(servers: dict[str, dict[str, Any]], statuses: dict[str, dict[str, Any]]) -> bool:
    for server_name, server_cfg in servers.items():
        if not server_cfg.get("enabled"):
            continue
        status = str(statuses.get(server_name, {}).get("status", "not_started"))
        if status in {"connecting", "not_started"}:
            return True
    return False


def build_mcp_settings_tab(reopen: Callable[[str], None] | None = None) -> None:
    ui.label("External MCP Tools").classes("text-h6")
    ui.label("Connect external Model Context Protocol servers without letting one bad server affect Thoth.").classes("text-grey-6 text-sm")

    def _refresh():
        if reopen:
            reopen("MCP")

    def _refresh_soon(delay: float = 1.0):
        if reopen:
            defer_ui(lambda: reopen("MCP"), delay=delay)

    cfg = mcp_config.get_config()
    status = get_status_summary()

    with ui.row().classes("items-center gap-4 q-mt-md"):
        def _toggle_global(e):
            mcp_config.set_global_enabled(bool(e.value))
            if e.value:
                discover_enabled_servers()
                _refresh_soon()
            ui.notify("MCP enabled" if e.value else "MCP disabled", type="positive")
            _refresh()

        ui.switch("Enable MCP", value=bool(cfg.get("enabled")), on_change=_toggle_global).tooltip("Global kill switch for all external MCP tools")
        sdk_badge = "SDK ready" if status.get("sdk_available") else "SDK missing"
        ui.badge(sdk_badge, color="green" if status.get("sdk_available") else "orange")
        ui.badge(f"{status.get('connected_server_count', 0)} connected", color="blue")
        ui.badge(f"{status.get('enabled_tool_count', 0)} enabled tools", color="purple")

    with ui.row().classes("q-mt-md gap-2"):
        ui.button("Add Server", icon="add", on_click=lambda: _open_server_dialog(_refresh)).props("unelevated")
        ui.button("Import Config", icon="upload_file", on_click=lambda: _open_import_dialog(_refresh)).props("flat")
        ui.button("Browse MCP Servers", icon="travel_explore", on_click=lambda: _open_marketplace_dialog(_refresh)).props("flat")
        ui.button("Diagnostics", icon="bug_report", on_click=_open_diagnostics_dialog).props("flat")

    ui.separator().classes("q-my-md")

    servers = cfg.get("servers", {})
    if not servers:
        ui.label("No MCP servers configured yet.").classes("text-grey-6 text-sm")
        return

    status_servers = status.get("servers", {})
    catalog = status.get("tools", {})
    for server_name, server_cfg in sorted(servers.items()):
        server_status = status_servers.get(server_name, {})
        tools = _display_tool_rows(server_cfg, catalog.get(server_name, []))
        _render_server_row(server_name, server_cfg, server_status, tools, _refresh, _refresh_soon)
    if cfg.get("enabled") and _has_pending_enabled_servers(servers, status_servers):
        _refresh_soon()


def _render_server_row(server_name: str, server_cfg: dict[str, Any], server_status: dict[str, Any],
                   tools: list[dict[str, Any]], refresh: Callable[[], None], refresh_soon: Callable[[], None]) -> None:
    default_open = _server_expansion_default_open(server_name)
    with ui.expansion(value=default_open).classes("w-full q-mb-sm") as exp:
        exp.on_value_change(lambda e, name=server_name: _set_server_expansion_open(name, bool(e.value)))
        with exp.add_slot("header"):
            with ui.row().classes("items-center justify-between w-full no-wrap"):
                with ui.row().classes("items-center gap-2"):
                    def _toggle_server(e, name=server_name):
                        mcp_config.set_server_enabled(name, bool(e.value))
                        if e.value:
                            discover_enabled_servers()
                            refresh_soon()
                        else:
                            from mcp_client.runtime import stop_server
                            stop_server(name)
                        refresh()

                    ui.checkbox(value=bool(server_cfg.get("enabled")), on_change=_toggle_server)
                    ui.label(server_name).classes("font-medium")
                    ui.badge(str(server_cfg.get("transport", "stdio")), color="grey")
                    st = str(server_status.get("status", "disabled"))
                    color = "green" if st == "connected" else "orange" if st in {"connecting", "not_started"} else "grey" if st in {"disabled", "global_disabled"} else "red"
                    ui.badge(st, color=color)
                    enabled_tool_count = sum(1 for tool in tools if tool.get("enabled"))
                    ui.label(f"{enabled_tool_count}/{len(tools)} tools").classes("text-caption text-grey-6")
                with ui.row().classes("items-center gap-1"):
                    async def _test(name=server_name, cfg=server_cfg):
                        note = ui.notification("Testing MCP server...", type="ongoing", spinner=True, timeout=None)
                        try:
                            result = await run.io_bound(probe_server, name, cfg)
                            note.dismiss()
                            if result.get("ok"):
                                updated = _apply_probe_defaults(dict(cfg), result)
                                mcp_config.upsert_server(name, updated)
                                if requires_manual_tool_selection(name, updated):
                                    ui.notify(f"Found {result.get('tool_count', 0)} tools. Review overlap/risk before enabling.", type="warning")
                                else:
                                    ui.notify(f"Found {result.get('tool_count', 0)} tools", type="positive")
                            else:
                                ui.notify(result.get("error", "MCP test failed"), type="negative")
                        except Exception as exc:
                            note.dismiss()
                            ui.notify(f"MCP test failed: {exc}", type="negative")
                        refresh()

                    ui.button(icon="science", on_click=_test).props("flat round size=sm").tooltip("Test connection")
                    ui.button(icon="refresh", on_click=lambda n=server_name: (refresh_server(n), refresh(), refresh_soon())).props("flat round size=sm").tooltip("Refresh")
                    ui.button(icon="edit", on_click=lambda n=server_name, c=server_cfg: _open_server_dialog(refresh, n, c)).props("flat round size=sm").tooltip("Edit")
                    ui.button(icon="delete", on_click=lambda n=server_name: _delete_server(n, refresh)).props("flat round size=sm color=negative").tooltip("Delete")

        last_error = server_status.get("last_error")
        if last_error:
            ui.label(str(last_error)).classes("text-negative text-caption")
        source = server_cfg.get("source") if isinstance(server_cfg.get("source"), dict) else {}
        if source:
            with ui.row().classes("items-center gap-1 q-mb-xs"):
                for label, color in _source_badges(source):
                    ui.badge(label, color=color)
        for conflict in conflicts_for_server(server_name, server_cfg):
            ui.label(conflict.message).classes("text-caption text-red" if conflict.severity == "high" else "text-caption text-orange-8")
        _render_requirement_rows(server_name, server_cfg, refresh, refresh_soon)
        if tools:
            ui.label("Tools").classes("text-sm font-bold q-mt-sm")
            for tool in tools:
                with ui.row().classes("items-center gap-2 w-full"):
                    ui.checkbox(
                        value=bool(tool.get("enabled")),
                        on_change=lambda e, s=server_name, t=tool.get("name"): (_set_server_expansion_open(s, True), mcp_config.set_tool_enabled(s, t, bool(e.value)), refresh()),
                    )
                    ui.label(tool.get("name", "tool")).classes("font-medium")
                    if tool.get("destructive"):
                        ui.badge("approval", color="red")
                    if tool.get("destructive"):
                        locked_approval = ui.row().classes("items-center gap-1 text-caption text-red")
                        locked_approval.tooltip("Destructive MCP tools always interrupt for approval when enabled.")
                        with locked_approval:
                            ui.icon("lock", size="xs")
                            ui.label("Approval always required")
                    else:
                        ui.checkbox(
                            "Require approval",
                            value=bool(tool.get("requires_approval")),
                            on_change=lambda e, s=server_name, t=tool.get("name"): (_set_server_expansion_open(s, True), mcp_config.set_tool_requires_approval(s, t, bool(e.value)), refresh()),
                        ).classes("text-caption")
                    description = tool.get("description", "")
                    if tool.get("configured_only") and not tool.get("input_schema"):
                        description = f"{description} Refresh to load the live schema."
                    ui.label(description).classes("text-caption text-grey-6")
                    schema_text = _schema_summary(tool.get("input_schema"))
                    if schema_text:
                        ui.label(schema_text).classes("text-caption text-grey-7")
        else:
            ui.label("Test or refresh this server to discover tools.").classes("text-grey-6 text-sm")
        with ui.expansion("Advanced"):
            tools_cfg = server_cfg.get("tools", {}) if isinstance(server_cfg.get("tools"), dict) else {}
            with ui.row().classes("items-center gap-4"):
                ui.checkbox(
                    "Expose resources as utility tools",
                    value=bool(tools_cfg.get("resources_enabled")),
                    on_change=lambda e, s=server_name: (_set_server_expansion_open(s, True), mcp_config.set_server_utility_enabled(s, "resources_enabled", bool(e.value)), refresh()),
                )
                ui.checkbox(
                    "Expose prompts as utility tools",
                    value=bool(tools_cfg.get("prompts_enabled")),
                    on_change=lambda e, s=server_name: (_set_server_expansion_open(s, True), mcp_config.set_server_utility_enabled(s, "prompts_enabled", bool(e.value)), refresh()),
                )
            ui.markdown(
                "```json\n" + json.dumps(mcp_config.normalize_server_config(server_name, server_cfg), indent=2) + "\n```",
                extras=["fenced-code-blocks"],
            )


def _set_server_expansion_open(server_name: str, open_: bool) -> None:
    if open_:
        _OPEN_SERVER_EXPANSIONS.add(server_name)
    else:
        _OPEN_SERVER_EXPANSIONS.discard(server_name)


def _server_expansion_default_open(server_name: str) -> bool:
    return server_name in _OPEN_SERVER_EXPANSIONS


def _requirement_label(requirement: RuntimeRequirement) -> str:
    commands = ", ".join(requirement.commands)
    return f"Requires {requirement.label}" + (f" ({commands})" if commands else "")


def _render_requirement_rows(server_name: str, server_cfg: dict[str, Any], refresh: Callable[[], None], refresh_soon: Callable[[], None]) -> None:
    checks = check_server_requirements(server_cfg)
    if not checks:
        return
    ui.label("Requirements").classes("text-sm font-bold q-mt-sm")
    for check in checks:
        requirement = check.requirement
        with ui.row().classes("items-center gap-2 w-full"):
            ui.icon("check_circle" if check.available else "error", color="positive" if check.available else "warning")
            ui.label(_requirement_label(requirement)).classes("font-medium")
            ui.badge("available" if check.available else "missing", color="green" if check.available else "orange")
            if requirement.managed:
                ui.badge("Thoth can install", color="blue")
            else:
                ui.badge("manual setup", color="grey")
            if check.source == "managed":
                ui.badge("managed", color="purple")
            if not check.available and check.installable:
                async def _install(runtime_id=requirement.id, label=requirement.label, enabled=bool(server_cfg.get("enabled"))):
                    note = ui.notification(f"Installing {label} for Thoth...", type="ongoing", spinner=True, timeout=None)
                    try:
                        result = await run.io_bound(install_managed_runtime, runtime_id)
                        note.dismiss()
                        if result.ok:
                            ui.notify(result.message, type="positive")
                            if enabled:
                                refresh_server(server_name)
                                refresh_soon()
                        else:
                            ui.notify(result.message, type="negative")
                    except Exception as exc:
                        note.dismiss()
                        ui.notify(f"Install failed: {exc}", type="negative")
                    refresh()

                ui.button("Install in Thoth", icon="download", on_click=_install).props("flat dense")
            elif not check.available and requirement.setup_url:
                ui.link("Setup", requirement.setup_url, new_tab=True).classes("text-caption")
        if not check.available:
            ui.label(check.message).classes("text-caption text-grey-7")


def _delete_server(name: str, refresh: Callable[[], None]) -> None:
    mcp_config.delete_server(name)
    from mcp_client.runtime import stop_server
    stop_server(name)
    ui.notify(f"Deleted {name}", type="positive")
    refresh()


def _open_server_dialog(refresh: Callable[[], None], name: str | None = None, server_cfg: dict[str, Any] | None = None) -> None:
    cfg = server_cfg or {}
    with ui.dialog() as dialog, ui.card().classes("w-[720px] max-w-full"):
        ui.label("Edit MCP Server" if name else "Add MCP Server").classes("text-h6")
        name_in = ui.input("Name", value=name or cfg.get("name", "")).classes("w-full")
        transport = ui.select(
            {"stdio": "stdio", "streamable_http": "Streamable HTTP", "sse": "SSE"},
            value=cfg.get("transport", "stdio"),
            label="Transport",
        ).classes("w-full")
        command = ui.input("Command", value=cfg.get("command", "")).classes("w-full")
        args = ui.input("Arguments", value=" ".join(cfg.get("args") or [])).classes("w-full")
        url = ui.input("URL", value=cfg.get("url", "")).classes("w-full")
        env = ui.textarea("Environment JSON", value=json.dumps(cfg.get("env") or {}, indent=2)).classes("w-full")
        headers = ui.textarea("Headers JSON", value=json.dumps(cfg.get("headers") or {}, indent=2)).classes("w-full")
        with ui.row().classes("gap-2"):
            timeout = ui.number("Timeout seconds", value=cfg.get("tool_timeout", 120), min=1).classes("w-48")
            output_limit = ui.number("Output cap characters", value=cfg.get("output_limit", 24000), min=1000).classes("w-56")

        async def _save(test_first: bool = False):
            try:
                server_name = str(name_in.value or "").strip()
                next_cfg = _server_from_form(server_name, transport.value, command.value, args.value, url.value, env.value, headers.value, timeout.value, output_limit.value)
                if test_first:
                    note = ui.notification("Testing MCP server...", type="ongoing", spinner=True, timeout=None)
                    result = await run.io_bound(probe_server, server_name, next_cfg)
                    note.dismiss()
                    if not result.get("ok"):
                        ui.notify(result.get("error", "MCP test failed"), type="negative")
                        return
                    next_cfg = _apply_probe_defaults(next_cfg, result)
                    ui.notify(f"Saved disabled with {result.get('tool_count', 0)} discovered tools", type="positive")
                else:
                    ui.notify("Saved disabled. Test before enabling.", type="warning")
                if name and name != server_name:
                    mcp_config.delete_server(name)
                mcp_config.upsert_server(server_name, next_cfg)
                dialog.close()
                refresh()
            except Exception as exc:
                ui.notify(str(exc), type="negative")

        with ui.row().classes("justify-end w-full"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Save Disabled", on_click=lambda: _save(False)).props("flat")
            ui.button("Test & Save", icon="science", on_click=lambda: _save(True)).props("unelevated")
    dialog.open()


def _open_import_dialog(refresh: Callable[[], None]) -> None:
    with ui.dialog() as dialog, ui.card().classes("w-[760px] max-w-full"):
        ui.label("Import MCP Config").classes("text-h6")
        ui.label("Imported servers are saved disabled until tested.").classes("text-caption text-grey-6")
        raw = ui.textarea("JSON", placeholder='{"mcpServers": {"server-name": {"command": "npx", "args": ["-y", "..."]}}}').classes("w-full").props("rows=14")
        def _import():
            try:
                count = _import_payload(raw.value or "")
                ui.notify(f"Imported {count} server(s), saved disabled until tested", type="positive")
                dialog.close()
                refresh()
            except Exception as exc:
                ui.notify(str(exc), type="negative")
        with ui.row().classes("justify-end w-full"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Import Config", icon="upload_file", on_click=_import).props("unelevated")
    dialog.open()


def _open_marketplace_dialog(refresh: Callable[[], None]) -> None:
    with ui.dialog() as dialog, ui.card().classes("w-[900px] max-w-full"):
        ui.label("Connect MCP Tools").classes("text-h6")
        ui.label("Recommended starters and directory results are imported disabled until tested. Thoth labels overlap/risk, but does not audit third-party servers.").classes("text-caption text-grey-6")

        async def _search():
            results_col.clear()
            with results_col:
                ui.spinner(size="lg")
            try:
                result = await run.io_bound(search_marketplace_with_status, query.value or "")
                entries = result.entries
                status_label.text = _format_marketplace_status(result)
            except Exception as exc:
                entries = []
                status_label.text = "Directory search unavailable."
                ui.notify(f"Directory unavailable: {exc}", type="warning")
            results_col.clear()
            with results_col:
                if not entries:
                    ui.label("No matching servers found.").classes("text-grey-6 text-sm")
                for entry in entries:
                    _render_marketplace_entry(entry, dialog, refresh)

        with ui.row().classes("w-full items-end gap-2 no-wrap"):
            query = ui.input("Search", placeholder="github, playwright, context7, sentry, database...").classes("col")
            query.on("keydown.enter.exact.prevent", _search)
            ui.button("Search Directories", icon="search", on_click=_search).props("unelevated")
        status_label = ui.label("").classes("text-caption text-grey-6")
        results_col = ui.column().classes("w-full gap-2")
        with ui.row().classes("justify-end w-full"):
            ui.button("Close", on_click=dialog.close).props("flat")
        dialog.on("show", lambda _: _search())
    dialog.open()


def _render_marketplace_entry(entry: MarketplaceEntry, dialog, refresh: Callable[[], None]) -> None:
    with ui.card().classes("w-full"):
        with ui.row().classes("items-start justify-between w-full"):
            with ui.column().classes("gap-1"):
                with ui.row().classes("items-center gap-2"):
                    ui.label(entry.name).classes("font-medium")
                    ui.badge(entry.source, color="blue")
                    if entry.recommended:
                        ui.badge("recommended starter", color="purple")
                    if entry.classification:
                        ui.badge(entry.classification, color="green" if "official" in entry.classification else "grey")
                    if entry.category:
                        ui.badge(entry.category, color="blue")
                    if entry.risk_level:
                        risk_color = "red" if entry.risk_level == "high" else "orange" if entry.risk_level == "medium" else "green"
                        ui.badge(f"{entry.risk_level} risk", color=risk_color)
                    if entry.requires_auth:
                        ui.badge("auth", color="orange")
                    if entry.transport:
                        ui.badge(entry.transport, color="grey")
                    for requirement in requirements_for_install(entry.install):
                        ui.badge(f"requires {requirement.label}", color="blue" if requirement.managed else "grey")
                ui.label(entry.description or "No description provided.").classes("text-caption text-grey-6")
                for conflict in conflicts_for_entry(entry):
                    ui.label(conflict.message).classes("text-caption text-red" if conflict.severity == "high" else "text-caption text-orange-8")
                if entry.notes:
                    ui.label(" ".join(entry.notes[:2])).classes("text-caption text-grey-7")
                if entry.url:
                    ui.link(entry.url, entry.url, new_tab=True).classes("text-caption")

            def _import_entry(e=entry):
                cfg = entry_to_server_config(e)
                base_name = re.sub(r"[^a-zA-Z0-9_-]+", "-", e.name.strip().lower()).strip("-") or e.id
                safe_name = unique_server_name(base_name, set(mcp_config.get_servers()))
                mcp_config.upsert_server(safe_name, cfg)
                ui.notify(f"Imported {e.name}, saved disabled until tested", type="positive")
                dialog.close()
                refresh()

            ui.button("Import", icon="add", on_click=_import_entry).props("flat")


def _open_diagnostics_dialog() -> None:
    with ui.dialog() as dialog, ui.card().classes("w-[900px] max-w-full"):
        ui.label("MCP Diagnostics").classes("text-h6")
        payload = {
            "config": mcp_config.masked_config(),
            "status": get_status_summary(),
        }
        ui.markdown("```json\n" + json.dumps(payload, indent=2, default=str) + "\n```", extras=["fenced-code-blocks"]).classes("w-full")
        with ui.row().classes("justify-end w-full"):
            ui.button("Close", on_click=dialog.close).props("flat")
    dialog.open()


def _format_marketplace_status(result: MarketplaceSearchResult) -> str:
    count = len(result.entries)
    if count == 0:
        return "No matching servers found."
    source_labels = {
        "official": "Official Registry",
        "pulsemcp": "PulseMCP",
        "smithery": "Smithery",
        "glama": "Glama",
        "curated": "Curated Starter Catalog",
    }
    sources = ", ".join(
        f"{source_labels.get(source, source)} ({source_count})"
        for source, source_count in sorted(result.source_counts.items())
    ) or "directory results"
    result_word = "result" if count == 1 else "results"
    if result.mode == "live":
        return f"Showing {count} live {result_word} from {sources}."
    if result.mode == "cache":
        return f"Showing {count} cached {result_word} from {sources}."
    return f"Showing {count} curated starter {result_word}."