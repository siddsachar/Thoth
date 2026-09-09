"""Collect public-docs inventory from Row-Bot source files.

The inventory intentionally avoids importing the NiceGUI app. It scans stable
source locations and emits deterministic JSON that generated docs can consume.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.docs.schemas import (  # noqa: E402
    DocsPageRecord,
    public_route_for_doc,
    repo_path,
    slugify,
    to_jsonable,
    write_json,
)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(_read_text(path)) or {}
    return data if isinstance(data, dict) else {}


def _frontmatter(path: Path) -> dict[str, Any]:
    text = _read_text(path)
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    data: dict[str, Any] = {}
    for line in text[3:end].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        data[key.strip()] = value.strip().strip('"').strip("'")
    return data


def _parse_ast(path: Path) -> ast.Module | None:
    try:
        return ast.parse(_read_text(path), filename=str(path))
    except SyntaxError:
        return None


def _first_docstring_summary(path: Path) -> str:
    tree = _parse_ast(path)
    if tree is None:
        return ""
    doc = ast.get_docstring(tree) or ""
    summary = " ".join(doc.strip().split())
    return summary.split(". ")[0].strip()


def _literal_value(node: ast.AST) -> Any:
    if isinstance(node, ast.Dict):
        data: dict[Any, Any] = {}
        for key_node, value_node in zip(node.keys, node.values):
            if key_node is None:
                continue
            key = _literal_value(key_node)
            data[key] = _literal_value(value_node)
        return data
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return [_literal_value(item) for item in node.elts]
    try:
        return ast.literal_eval(node)
    except Exception:
        if isinstance(node, ast.Call):
            return _literal_call(node)
        if isinstance(node, ast.Attribute):
            return node.attr
        if isinstance(node, ast.Name):
            return node.id
    return None


def _literal_call(node: ast.Call) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for kw in node.keywords:
        if kw.arg:
            data[kw.arg] = _literal_value(kw.value)
    return data


def _assigned_dict(path: Path, name: str) -> dict[str, Any]:
    tree = _parse_ast(path)
    if tree is None:
        return {}
    for node in tree.body:
        value_node: ast.AST | None = None
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            value_node = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
            value_node = node.value
        if value_node is not None:
            value = _literal_value(value_node)
            return value if isinstance(value, dict) else {}
    return {}


def _class_names(path: Path) -> list[str]:
    tree = _parse_ast(path)
    if tree is None:
        return []
    return sorted(node.name for node in tree.body if isinstance(node, ast.ClassDef))


def _skill_frontmatter(path: Path) -> dict[str, Any]:
    data = _frontmatter(path)
    text = _read_text(path)
    if "description" not in data:
        for line in text.splitlines():
            if line.lower().startswith("description:"):
                data["description"] = line.split(":", 1)[1].strip()
                break
    if "name" not in data:
        match = re.search(r"^#\s+(.+)$", text, flags=re.MULTILINE)
        if match:
            data["name"] = match.group(1).strip()
    return data


def collect_tools() -> list[dict[str, Any]]:
    tools_dir = ROOT / "src" / "row_bot" / "tools"
    guide_by_id = {
        path.parent.name.removesuffix("_guide"): path
        for path in (ROOT / "tool_guides").glob("*/SKILL.md")
    }
    tools: list[dict[str, Any]] = []
    for path in sorted(tools_dir.glob("*_tool.py")):
        tool_id = path.stem.removesuffix("_tool")
        guide = guide_by_id.get(tool_id)
        guide_meta = _skill_frontmatter(guide) if guide else {}
        tools.append(
            {
                "id": tool_id,
                "title": guide_meta.get("name") or tool_id.replace("_", " ").title(),
                "description": guide_meta.get("description") or _first_docstring_summary(path),
                "source": repo_path(ROOT, path),
                "guide": repo_path(ROOT, guide) if guide else "",
                "classes": _class_names(path),
                "approval": _tool_approval_summary(path),
            }
        )
    return tools


def _tool_approval_summary(path: Path) -> str:
    tree = _parse_ast(path)
    if tree is None:
        return "Operation-dependent"
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name != "destructive_tool_names":
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                names.add(child.value)
    if names:
        return "Approval-gated: " + ", ".join(sorted(names))
    if "approval" in _read_text(path).casefold():
        return "Operation-dependent approval"
    return "No tool-wide destructive classification"


def collect_providers() -> list[dict[str, Any]]:
    catalog = _assigned_dict(ROOT / "src" / "row_bot" / "providers" / "catalog.py", "PROVIDER_DEFINITIONS")
    providers: list[dict[str, Any]] = []
    for provider_id, raw in sorted(catalog.items()):
        data = raw if isinstance(raw, dict) else {}
        auth_methods = data.get("auth_methods") or []
        if isinstance(auth_methods, tuple):
            auth_methods = list(auth_methods)
        providers.append(
            {
                "id": provider_id,
                "title": data.get("display_name") or provider_id.replace("_", " ").title(),
                "description": _provider_description(provider_id, data),
                "source": "src/row_bot/providers/catalog.py",
                "auth_methods": [str(item) for item in auth_methods],
                "transport": str(data.get("default_transport") or ""),
                "base_url": str(data.get("base_url") or ""),
                "risk_label": str(data.get("risk_label") or "api_key"),
                "route": _provider_route(provider_id, data),
                "experimental": bool(data.get("experimental")),
            }
        )
    return providers


def _provider_route(provider_id: str, data: dict[str, Any]) -> str:
    if provider_id == "ollama":
        return "Local"
    if str(data.get("risk_label") or "") == "subscription":
        return "Subscription"
    if provider_id == "custom":
        return "Custom endpoint"
    return "API"


def _provider_description(provider_id: str, data: dict[str, Any]) -> str:
    risk = str(data.get("risk_label") or "")
    if provider_id == "ollama":
        return "Local Ollama models running on this machine."
    if risk == "subscription":
        return "Subscription-backed provider path using local sign-in or an external CLI."
    if risk == "third_party_router":
        return "Third-party model router provider configured with an API key."
    if data.get("base_url"):
        return "Hosted model provider configured with an API key."
    return "Model provider supported by Row-Bot."


def collect_settings() -> list[dict[str, Any]]:
    settings = _load_yaml(ROOT / "docs-content" / "metadata" / "settings.yml").get("tabs", {})
    rows: list[dict[str, Any]] = []
    if isinstance(settings, dict):
        for name, meta in settings.items():
            meta = meta if isinstance(meta, dict) else {}
            rows.append(
                {
                    "id": slugify(name),
                    "title": name,
                    "description": str(meta.get("description") or ""),
                    "docs_route": str(meta.get("docs_route") or ""),
                    "screenshot_id": str(meta.get("screenshot_id") or ""),
                    "source": "docs-content/metadata/settings.yml",
                }
            )
    return rows


_SETTING_CONTROL_TYPES = {
    "button",
    "checkbox",
    "input",
    "number",
    "radio",
    "select",
    "slider",
    "switch",
    "textarea",
    "toggle",
}


def _call_name(node: ast.Call) -> tuple[str, str]:
    func = node.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return func.value.id, func.attr
    return "", ""


def _call_keyword(node: ast.Call, name: str) -> ast.AST | None:
    for keyword in node.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _control_label(node: ast.Call, control_type: str) -> str:
    label_node = _call_keyword(node, "label")
    if label_node is None and node.args and control_type != "slider":
        label_node = node.args[0]
    value = (
        _literal_value(label_node)
        if isinstance(label_node, (ast.Constant, ast.JoinedStr))
        else None
    )
    if isinstance(value, str) and value.strip():
        return " ".join(value.split())
    placeholder = _literal_value(_call_keyword(node, "placeholder"))
    if isinstance(placeholder, str) and placeholder.strip():
        return " ".join(placeholder.split())
    return ""


def _control_value(node: ast.Call, keyword: str) -> Any:
    value_node = _call_keyword(node, keyword)
    if value_node is None:
        return ""
    value = _literal_value(value_node)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value if value is not None else ""
    if isinstance(value, (list, dict)):
        return value
    return "Configured value"


def collect_settings_controls() -> list[dict[str, Any]]:
    tabs = _load_yaml(ROOT / "docs-content" / "metadata" / "settings_tabs.yml").get("tabs", {})
    builder_to_tab = {
        str(meta.get("builder")): str(tab)
        for tab, meta in (tabs.items() if isinstance(tabs, dict) else [])
        if isinstance(meta, dict) and meta.get("builder")
    }
    docs_routes = {
        row["title"]: row["docs_route"] for row in collect_settings()
    }
    files = [
        (ROOT / "src" / "row_bot" / "ui" / "settings.py", ""),
        (ROOT / "src" / "row_bot" / "ui" / "provider_settings.py", "Providers"),
        (ROOT / "src" / "row_bot" / "ui" / "buddy.py", "Buddy"),
        (ROOT / "src" / "row_bot" / "ui" / "mcp_settings.py", "MCP"),
        (ROOT / "src" / "row_bot" / "plugins" / "ui_settings.py", "Plugins"),
        (ROOT / "src" / "row_bot" / "ui" / "remote_access_settings.py", "System"),
        (ROOT / "src" / "row_bot" / "ui" / "computer_use.py", "System"),
        (ROOT / "src" / "row_bot" / "ui" / "update_dialog.py", "Preferences"),
    ]
    raw_rows: list[dict[str, Any]] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self, path: Path, fixed_tab: str) -> None:
            self.path = path
            self.fixed_tab = fixed_tab
            self.functions: list[str] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.functions.append(node.name)
            self.generic_visit(node)
            self.functions.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node: ast.Call) -> None:
            owner, control_type = _call_name(node)
            if owner == "ui" and control_type in _SETTING_CONTROL_TYPES:
                tab = self.fixed_tab or next(
                    (builder_to_tab[name] for name in reversed(self.functions) if name in builder_to_tab),
                    "",
                )
                if not tab and any(
                    name in {
                        "_build_github_account_panel",
                        "_build_google_account_panel",
                        "_build_x_account_panel",
                    }
                    for name in self.functions
                ):
                    tab = "Accounts"
                label = _control_label(node, control_type)
                if tab and label:
                    raw_rows.append(
                        {
                            "tab": tab,
                            "label": label,
                            "control": control_type,
                            "default": _control_value(node, "value"),
                            "allowed_values": _control_value(node, "options"),
                            "source": f"{repo_path(ROOT, self.path)}:{node.lineno}",
                            "line": node.lineno,
                        }
                    )
            self.generic_visit(node)

    for path, fixed_tab in files:
        tree = _parse_ast(path)
        if tree is not None:
            Visitor(path, fixed_tab).visit(tree)

    dynamic_controls = {
        "Models": ["Default chat model", "Quick Choices", "Refresh model catalog"],
        "Tools": [
            "Auto-select external tools (recommended)",
            "Load all external tools",
            "Web search",
            "DuckDuckGo",
            "Wolfram Alpha",
            "arXiv",
            "Wikipedia",
            "YouTube",
        ],
        "Accounts": [
            "Reconnect GitHub CLI",
            "Refresh GitHub CLI authorisation",
            "Use anonymous GitHub access for public sources",
            "Clear saved GitHub token",
            "Connect Google account",
            "Enable X tool",
        ],
        "Utilities": [
            "Tasks",
            "Timer",
            "URL reader",
            "Calculator",
            "Weather",
            "Charts",
            "System information",
            "Conversation search",
            "Custom Tool builder",
        ],
    }
    for channel in collect_channels():
        for field in channel.get("configured_by", []):
            dynamic_controls.setdefault("Channels", []).append(f"{channel['title']}: {field}")
        dynamic_controls.setdefault("Channels", []).extend(
            [f"{channel['title']}: Start or stop", f"{channel['title']}: Test connection"]
        )
    for tab, labels in dynamic_controls.items():
        for index, label in enumerate(labels, start=1):
            raw_rows.append(
                {
                    "tab": tab,
                    "label": label,
                    "control": "dynamic",
                    "default": "Configured locally",
                    "allowed_values": "Shown inline",
                    "source": "runtime registry",
                    "line": index,
                }
            )

    security_by_tab = {
        "Providers": "Credentials are stored through the configured secret store; values are not shown in this reference.",
        "Accounts": "Connecting an account opens an external authorisation flow.",
        "Channels": "Starting an adapter can receive or deliver real messages; review targets first.",
        "MCP": "External servers can expose consequential tools; keep new servers disabled until tested.",
        "Plugins": "Review source, permissions, and provided capabilities before enabling.",
        "System": "Filesystem, shell, browser, network, and mobile-access controls can widen local access.",
    }
    dependency_by_tab = {
        "Voice": "Voice extras, provider credentials, and OS audio permissions may be required.",
        "Buddy": "Desktop overlay behaviour depends on native-window support.",
        "Channels": "The matching channel extra and third-party account configuration are required.",
        "MCP": "The MCP extra and a compatible external server are required.",
        "Plugins": "Plugin-provided dependencies remain disabled until reviewed and installed.",
    }
    counts: dict[str, int] = {}
    rows: list[dict[str, Any]] = []
    for row in sorted(raw_rows, key=lambda item: (item["tab"], item["source"], item["line"])):
        base = f"{slugify(row['tab'])}-{slugify(row['label'])}-{row['control']}"
        counts[base] = counts.get(base, 0) + 1
        row["id"] = base if counts[base] == 1 else f"{base}-{counts[base]}"
        row["effect"] = f"Changes {row['label']} in {row['tab']} settings."
        row["dependencies"] = dependency_by_tab.get(row["tab"], "No optional dependency is indicated by the control itself.")
        row["restart"] = "Follow any inline restart or reconnect prompt shown after changing the value."
        row["security"] = security_by_tab.get(row["tab"], "Stored locally unless the surrounding feature explicitly uses an external service.")
        row["docs_route"] = docs_routes.get(row["tab"], "")
        rows.append(row)
    return rows


def _cli_rows_for_path(
    path: Path,
    *,
    command_by_receiver: dict[str, str] | None = None,
    command_by_function: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    tree = _parse_ast(path)
    if tree is None:
        return []
    parser_commands: dict[str, str] = {"parser": "row-bot"}
    parser_commands.update(command_by_receiver or {})
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        if not (isinstance(func, ast.Attribute) and func.attr == "add_parser" and node.value.args):
            continue
        command = _literal_value(node.value.args[0])
        if isinstance(command, str):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    parser_commands.setdefault(target.id, f"row-bot plugin {command}")
    rows: list[dict[str, Any]] = []
    function_commands = command_by_function or {}
    for parent in ast.walk(tree):
        if not isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        function_command = function_commands.get(parent.name)
        for node in parent.body:
            for candidate in ast.walk(node):
                if not isinstance(candidate, ast.Call) or not isinstance(candidate.func, ast.Attribute) or candidate.func.attr != "add_argument":
                    continue
                receiver = candidate.func.value.id if isinstance(candidate.func.value, ast.Name) else "parser"
                options = [value for value in (_literal_value(arg) for arg in candidate.args) if isinstance(value, str)]
                if not options:
                    continue
                description = str(_control_value(candidate, "help") or "").replace("NiceGUI server", "Row-Bot local server")
                if description == "SUPPRESS":
                    continue
                rows.append(
                    {
                        "id": slugify(
                            "-".join(
                                (
                                    function_command or parser_commands.get(receiver, "row-bot"),
                                    *options,
                                )
                            )
                        ),
                        "command": (function_command or parser_commands.get(receiver, "row-bot")),
                        "option": ", ".join(options),
                        "description": description,
                        "default": _control_value(candidate, "default"),
                        "source": f"{repo_path(ROOT, path)}:{candidate.lineno}",
                    }
                )
    return rows


def collect_cli_options() -> list[dict[str, Any]]:
    rows = _cli_rows_for_path(ROOT / "src" / "row_bot" / "launcher.py")
    access_path = ROOT / "src" / "row_bot" / "access" / "cli.py"
    rows.extend(
        _cli_rows_for_path(
            access_path,
            command_by_receiver={
                "parser": "row-bot access",
                "invite": "row-bot access invite",
                "lifetime": "row-bot access invite",
                "list_parser": "row-bot access list",
                "revoke": "row-bot access revoke",
                "revoke_all": "row-bot access revoke-all",
                "doctor": "row-bot access doctor",
            },
            command_by_function={
                "configure_serve_parser": "row-bot serve",
                "_add_access_common": "row-bot access subcommands",
            },
        )
    )
    access_commands = (
        "row-bot access invite",
        "row-bot access list",
        "row-bot access revoke",
        "row-bot access revoke-all",
        "row-bot access doctor",
    )
    expanded_rows: list[dict[str, Any]] = []
    for row in rows:
        if row["command"] != "row-bot access subcommands":
            expanded_rows.append(row)
            continue
        for command in access_commands:
            expanded = dict(row)
            expanded["command"] = command
            expanded["id"] = slugify(f"{command}-{row['option']}")
            expanded_rows.append(expanded)
    unique_rows = {(row["command"], row["option"], row["source"]): row for row in expanded_rows}
    return sorted(
        unique_rows.values(),
        key=lambda row: (row["command"], row["option"], row["source"]),
    )


def collect_environment() -> list[dict[str, Any]]:
    variables = {
        "ROW_BOT_DATA_DIR": "Override the local Row-Bot data directory for this process.",
        "ROW_BOT_WORKSPACE": "Override the default workspace used by file-oriented tools.",
        "ROW_BOT_HOST": "Choose the interface bound by the local application server.",
        "ROW_BOT_PORT": "Choose the preferred local application port.",
        "ROW_BOT_DEPLOYMENT_MODE": "Choose desktop or authenticated server request policy.",
        "ROW_BOT_PUBLIC_URL": "Set the canonical browser-facing origin for server mode.",
        "ROW_BOT_PUBLIC_ORIGINS": "Set one or more allowed browser-facing origins.",
        "ROW_BOT_ALLOWED_HOSTS": "Allow exact HTTP Host authorities accepted by the access gate.",
        "ROW_BOT_TRUSTED_PROXY_CIDRS": "Trust forwarding headers only from exact connecting proxy networks.",
        "ROW_BOT_UNTRUSTED_FORWARDED_ACTION": "Reject or ignore forwarding headers received from an untrusted peer.",
        "ROW_BOT_WORKERS": "Set the server worker count; Row-Bot requires exactly one.",
        "ROW_BOT_EPHEMERAL_DATA": "Tell access diagnostics that server data is intentionally ephemeral.",
        "ROW_BOT_SECRETS_DIR": "Read allowlisted externally managed secrets from individual files.",
        "ROW_BOT_BROWSER_HEADLESS": "Run bundled browser automation without an interactive display.",
        "ROW_BOT_NATIVE": "Select native-window behaviour for advanced launch scenarios.",
        "ROW_BOT_AUTO_START_OLLAMA": "Allow or suppress launcher attempts to start Ollama automatically.",
        "ROW_BOT_STARTUP_TIMEOUT": "Set the launcher startup timeout in seconds.",
        "ROW_BOT_WEBVIEW_STORAGE_PATH": "Override native webview storage for advanced troubleshooting.",
        "ROW_BOT_INSTALL_ROOT": "Identify the installed application root for updater and repair flows.",
        "ROW_BOT_PLUGIN_INDEX_URL": "Override the public plugin marketplace index URL.",
        "ROW_BOT_PLUGIN_REPO_URL": "Override the plugin repository used by marketplace installs.",
        "ROW_BOT_XAI_OAUTH_CLIENT_ID": "Override the xAI OAuth client identifier.",
        "ROW_BOT_XAI_OAUTH_REDIRECT_PORT": "Override the local xAI OAuth callback port.",
        "ROW_BOT_XAI_OAUTH_SCOPES": "Override the requested xAI OAuth scopes.",
        "ROW_BOT_REALTIME_INSTRUCTIONS": "Override additional realtime voice session instructions.",
    }
    source_roots = [ROOT / "src" / "row_bot"]
    source_by_variable: dict[str, list[str]] = {name: [] for name in variables}
    for source_root in source_roots:
        for path in sorted(source_root.rglob("*.py")):
            text = _read_text(path)
            for name in variables:
                # Keep canonical *_ENV aliases, but not longer identifiers such
                # as the native bridge global __ROW_BOT_NATIVE_CLIENT__.
                if name in text and re.search(rf"\b{re.escape(name)}(?:_ENV)?\b", text):
                    source_by_variable[name].append(repo_path(ROOT, path))
    return [
        {
            "id": slugify(name),
            "variable": name,
            "description": description,
            "source": ", ".join(source_by_variable[name]),
        }
        for name, description in variables.items()
    ]


def collect_home_tabs() -> list[dict[str, Any]]:
    tabs = _load_yaml(ROOT / "docs-content" / "metadata" / "home_tabs.yml").get("tabs", {})
    rows: list[dict[str, Any]] = []
    if isinstance(tabs, dict):
        for name, meta in tabs.items():
            meta = meta if isinstance(meta, dict) else {}
            rows.append(
                {
                    "id": slugify(name),
                    "title": name,
                    "docs_route": str(meta.get("docs_route") or ""),
                    "screenshot_id": str(meta.get("screenshot_id") or ""),
                    "source": str(meta.get("source") or "src/row_bot/ui/home.py"),
                    "builder": str(meta.get("builder") or "build_home"),
                }
            )
    return rows


def collect_channels() -> list[dict[str, Any]]:
    channels_dir = ROOT / "src" / "row_bot" / "channels"
    skip = {
        "__init__",
        "auth",
        "auth_store",
        "agent_output",
        "approval",
        "base",
        "commands",
        "config",
        "media",
        "media_capture",
        "registry",
        "runtime",
        "streaming",
        "thread_notifications",
        "thread_repair",
        "tool_factory",
    }
    rows: list[dict[str, Any]] = []
    for path in sorted(channels_dir.glob("*.py")):
        if path.stem in skip or path.stem.startswith("_"):
            continue
        text = _read_text(path)
        if "Channel" not in text:
            continue
        display = _regex_return_for_property(text, "display_name") or path.stem.replace("_", " ").title()
        name = _regex_return_for_property(text, "name") or path.stem
        rows.append(
            {
                "id": slugify(name),
                "title": display,
                "description": _first_docstring_summary(path) or f"{display} messaging channel.",
                "source": repo_path(ROOT, path),
                "configured_by": _channel_config_fields(text),
                "capabilities": _channel_capabilities(text),
            }
        )
    return rows


def _regex_return_for_property(text: str, prop: str) -> str:
    pattern = rf"def\s+{re.escape(prop)}\(self\).*?return\s+['\"]([^'\"]+)['\"]"
    match = re.search(pattern, text, flags=re.DOTALL)
    return match.group(1) if match else ""


def _channel_config_fields(text: str) -> list[str]:
    labels = re.findall(r"ConfigField\([^)]*label\s*=\s*['\"]([^'\"]+)['\"]", text, flags=re.DOTALL)
    return sorted(set(labels))


def _channel_capabilities(text: str) -> list[str]:
    match = re.search(r"ChannelCapabilities\((.*?)\)", text, flags=re.DOTALL)
    if not match:
        return []
    return sorted(re.findall(r"([a-z_]+)\s*=\s*True", match.group(1)))


def collect_skills(root_name: str) -> list[dict[str, Any]]:
    skills_root = ROOT / root_name
    skills: list[dict[str, Any]] = []
    if not skills_root.exists():
        return skills
    for path in sorted(skills_root.glob("*/SKILL.md")):
        meta = _skill_frontmatter(path)
        skills.append(
            {
                "id": path.parent.name,
                "title": meta.get("display_name") or meta.get("name") or path.parent.name.replace("_", " ").title(),
                "description": meta.get("description", ""),
                "kind": root_name,
                "source": repo_path(ROOT, path),
            }
        )
    return skills


def collect_mcp() -> list[dict[str, Any]]:
    path = ROOT / "src" / "row_bot" / "mcp_client" / "recommended_servers.json"
    try:
        raw = json.loads(_read_text(path))
    except Exception:
        raw = []
    rows: list[dict[str, Any]] = []
    for entry in raw if isinstance(raw, list) else raw.get("servers", []):
        if not isinstance(entry, dict):
            continue
        install = entry.get("install") if isinstance(entry.get("install"), dict) else {}
        rows.append(
            {
                "id": str(entry.get("id") or slugify(entry.get("name") or "mcp")),
                "title": str(entry.get("name") or entry.get("id") or "MCP server"),
                "description": str(entry.get("description") or ""),
                "source": repo_path(ROOT, path),
                "category": str(entry.get("category") or ""),
                "transport": str(install.get("transport") or ""),
                "command": str(install.get("command") or ""),
                "overlaps_native": entry.get("overlaps_native") or [],
            }
        )
    return rows


def collect_plugins() -> list[dict[str, Any]]:
    manifest = ROOT / "src" / "row_bot" / "plugins" / "manifest.py"
    text = _read_text(manifest)
    fields = re.findall(r"^\s{4}([a-zA-Z_][a-zA-Z0-9_]*)\s*:", text, flags=re.MULTILINE)
    return [
        {
            "id": "plugin-manifest",
            "title": "Plugin manifest",
            "description": "Schema and validation behavior for plugin.json files.",
            "source": repo_path(ROOT, manifest),
            "required_fields": [
                "id",
                "name",
                "version",
                "min_row_bot_version",
                "author",
                "description",
            ],
            "fields": fields,
            "id_pattern": "lowercase letters, numbers, and hyphens; 2-64 characters",
            "version_pattern": "semver x.y.z",
        },
        {
            "id": "custom-tools",
            "title": "Custom Tools",
            "description": "Reviewed Developer Studio tools can be promoted into the plugin-style tool surface.",
            "source": "src/row_bot/plugins/ui_settings.py",
            "required_fields": [],
            "fields": ["name", "description", "tools", "source_url", "installed_path"],
        },
    ]


def collect_data_paths() -> list[dict[str, Any]]:
    path = ROOT / "src" / "row_bot" / "data_paths.py"
    labels = {
        "data_dir": "Root data directory",
        "tasks_db": "Workflow/task database",
        "memory_db": "Memory database",
        "threads_db": "Thread metadata and checkpoints",
        "logs_dir": "Application logs",
    }
    return [
        {
            "id": key,
            "title": title,
            "description": "Resolved under ROW_BOT_DATA_DIR when set, otherwise the default Row-Bot user data directory.",
            "source": repo_path(ROOT, path),
            "environment_override": "ROW_BOT_DATA_DIR",
        }
        for key, title in labels.items()
    ]


def collect_safety() -> list[dict[str, Any]]:
    approval_path = ROOT / "src" / "row_bot" / "approval_policy.py"
    labels = _assigned_dict(approval_path, "APPROVAL_MODE_LABELS")
    rows = []
    for mode, label in sorted(labels.items()):
        rows.append(
            {
                "id": str(mode),
                "title": str(label),
                "description": _approval_description(str(mode)),
                "source": repo_path(ROOT, approval_path),
                "decision": "block" if mode == "block" else "allow" if mode == "allow_all" else "ask",
            }
        )
    rows.append(
        {
            "id": "mcp-destructive-tools",
            "title": "MCP destructive tool classification",
            "description": "External MCP tools that look destructive require approval by default.",
            "source": "src/row_bot/mcp_client/safety.py",
            "decision": "ask",
        }
    )
    return rows


def _approval_description(mode: str) -> str:
    if mode == "block":
        return "Read-only or blocked mode for actions that would change external state."
    if mode == "allow_all":
        return "Automatically allows actions in trusted contexts."
    return "Prompts the user before a sensitive action continues."


def collect_docs_pages() -> list[dict[str, Any]]:
    docs_root = ROOT / "docs-site" / "docs"
    pages: list[dict[str, Any]] = []
    if not docs_root.exists():
        return pages
    paths = sorted(docs_root.rglob("*.md")) + sorted(docs_root.rglob("*.mdx"))
    for path in paths:
        rel = path.relative_to(docs_root)
        meta = _frontmatter(path)
        title = meta.get("title") or path.stem.replace("-", " ").title()
        pages.append(
            to_jsonable(
                DocsPageRecord(
                    id=slugify(str(rel.with_suffix(""))),
                    source=repo_path(ROOT, path),
                    path=str(rel).replace("\\", "/"),
                    route=public_route_for_doc(path, docs_root),
                    title=str(title),
                    description=str(meta.get("description") or ""),
                )
            )
        )
    return pages


def collect_version() -> dict[str, str]:
    version_file = ROOT / "src" / "row_bot" / "version.py"
    text = _read_text(version_file) if version_file.exists() else ""
    match = re.search(r"__version__\s*=\s*['\"]([^'\"]+)['\"]", text)
    return {"version": match.group(1) if match else "unknown"}


def collect_metadata() -> dict[str, Any]:
    return {
        "ui_surfaces": _load_yaml(ROOT / "docs-content" / "metadata" / "ui_surfaces.yml"),
        "settings": _load_yaml(ROOT / "docs-content" / "metadata" / "settings.yml"),
        "settings_tabs": _load_yaml(ROOT / "docs-content" / "metadata" / "settings_tabs.yml"),
        "home_tabs": _load_yaml(ROOT / "docs-content" / "metadata" / "home_tabs.yml"),
        "dialogs": _load_yaml(ROOT / "docs-content" / "metadata" / "dialogs.yml"),
        "screenshots": _load_yaml(ROOT / "docs-content" / "metadata" / "screenshots.yml"),
        "how_to_guides": _load_yaml(ROOT / "docs-content" / "metadata" / "how_to_guides.yml"),
    }


def build_inventory() -> dict[str, Any]:
    bundled_skills = collect_skills("bundled_skills")
    tool_guides = collect_skills("tool_guides")
    return {
        "version": collect_version(),
        "tools": collect_tools(),
        "providers": collect_providers(),
        "settings": collect_settings(),
        "settings_controls": collect_settings_controls(),
        "cli_options": collect_cli_options(),
        "environment": collect_environment(),
        "home_tabs": collect_home_tabs(),
        "channels": collect_channels(),
        "skills": bundled_skills + tool_guides,
        "mcp": collect_mcp(),
        "plugins": collect_plugins(),
        "data_paths": collect_data_paths(),
        "safety": collect_safety(),
        "docs_pages": collect_docs_pages(),
        "metadata": collect_metadata(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect Row-Bot public docs inventory")
    parser.add_argument("--out", default="docs-build/inventory", help="Output directory")
    args = parser.parse_args()

    out_dir = (ROOT / args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    inventory = build_inventory()
    write_json(out_dir / "inventory.json", inventory)
    for key, value in inventory.items():
        write_json(out_dir / f"{key}.json", value)
    print(f"Wrote public docs inventory to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
