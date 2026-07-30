"""Generate deterministic MDX reference pages from public docs inventory."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.docs.collect_inventory import build_inventory  # noqa: E402
from scripts.docs.schemas import clean_public_text  # noqa: E402


GENERATED_DIR = ROOT / "docs-site" / "docs" / "reference" / "generated"
TEMPLATE = ROOT / "scripts" / "docs" / "templates" / "reference_page.mdx"


PAGE_DEFS: list[tuple[str, str, str, str, Callable[[dict[str, Any]], str]]] = []


def _escape(value: Any) -> str:
    text = clean_public_text(value)
    return (
        text.replace("|", "\\|")
        .replace("{", "&#123;")
        .replace("}", "&#125;")
        .replace("\n", " ")
        .strip()
    )


def _link(value: str) -> str:
    value = str(value or "")
    if not value:
        return ""
    if value.startswith("/docs/") or value.startswith("http"):
        return f"[{value}]({value})"
    return f"`{_escape(value)}`"


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return "_No records found._"
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(_escape(value) for value in row) + " |")
    return "\n".join(lines)


def _record_table(records: list[dict[str, Any]], columns: list[tuple[str, str]]) -> str:
    rows = []
    for record in records:
        rows.append([_format_cell(record, key) for key, _label in columns])
    return _table([label for _key, label in columns], rows)


def _format_cell(record: dict[str, Any], key: str) -> str:
    value = record.get(key, "")
    if isinstance(value, list):
        return ", ".join(str(item) for item in value if str(item))
    if isinstance(value, dict):
        return str({item_key: value[item_key] for item_key in sorted(value)})
    if key.endswith("route"):
        return _link(str(value))
    if key in {"source", "guide"} and value:
        return f"`{value}`"
    return str(value or "")


def _section(title: str, body: str) -> str:
    return f"## {title}\n\n{body}".strip()


def _tools(inv: dict[str, Any]) -> str:
    return _section(
        "Tools",
        _record_table(
            inv.get("tools", []),
            [
                ("id", "ID"),
                ("title", "Tool"),
                ("description", "Description"),
                ("approval", "Approval classification"),
                ("guide", "Guide"),
                ("source", "Source"),
            ],
        ),
    )


def _providers(inv: dict[str, Any]) -> str:
    return _section(
        "Providers",
        _record_table(
            inv.get("providers", []),
            [
                ("id", "ID"),
                ("title", "Provider"),
                ("description", "Description"),
                ("route", "Route"),
                ("auth_methods", "Auth"),
                ("transport", "Transport"),
                ("risk_label", "Risk label"),
            ],
        ),
    )


def _settings(inv: dict[str, Any]) -> str:
    return _section(
        "Settings Tabs",
        _record_table(
            inv.get("settings", []),
            [
                ("title", "Tab"),
                ("description", "Description"),
                ("docs_route", "Docs route"),
                ("screenshot_id", "Screenshot"),
            ],
        ),
    )


def _settings_controls(inv: dict[str, Any]) -> str:
    rows = inv.get("settings_controls", [])
    sections = []
    for tab in [row.get("title") for row in inv.get("settings", [])]:
        tab_rows = [row for row in rows if row.get("tab") == tab]
        sections.append(
            _section(
                f"{tab} Controls",
                _record_table(
                    tab_rows,
                    [
                        ("label", "Control"),
                        ("control", "Type"),
                        ("default", "Default"),
                        ("allowed_values", "Allowed values"),
                        ("effect", "Effect"),
                        ("dependencies", "Dependencies"),
                        ("restart", "Restart"),
                        ("security", "Security"),
                        ("source", "Source"),
                    ],
                ),
            )
        )
    return "\n\n".join(sections)


def _home_tabs(inv: dict[str, Any]) -> str:
    return _section(
        "Home Tabs",
        _record_table(
            inv.get("home_tabs", []),
            [
                ("title", "Tab"),
                ("docs_route", "Docs route"),
                ("screenshot_id", "Screenshot"),
                ("builder", "Builder"),
                ("source", "Source"),
            ],
        ),
    )


def _channels(inv: dict[str, Any]) -> str:
    return _section(
        "Channels",
        _record_table(
            inv.get("channels", []),
            [
                ("id", "ID"),
                ("title", "Channel"),
                ("description", "Description"),
                ("configured_by", "Configuration"),
                ("capabilities", "Capabilities"),
            ],
        ),
    )


def _skills(inv: dict[str, Any]) -> str:
    return "\n\n".join(
        [
            _section(
                "Bundled Skills",
                _record_table(
                    [row for row in inv.get("skills", []) if row.get("kind") == "bundled_skills"],
                    [("id", "ID"), ("title", "Skill"), ("description", "Description"), ("source", "Source")],
                ),
            ),
            _section(
                "Tool Guides",
                _record_table(
                    [row for row in inv.get("skills", []) if row.get("kind") == "tool_guides"],
                    [("id", "ID"), ("title", "Guide"), ("description", "Description"), ("source", "Source")],
                ),
            ),
        ]
    )


def _mcp(inv: dict[str, Any]) -> str:
    return _section(
        "Recommended MCP Servers",
        _record_table(
            inv.get("mcp", []),
            [
                ("id", "ID"),
                ("title", "Server"),
                ("description", "Description"),
                ("transport", "Transport"),
                ("command", "Command"),
                ("overlaps_native", "Native overlap"),
            ],
        ),
    )


def _plugins(inv: dict[str, Any]) -> str:
    return _section(
        "Plugin Surfaces",
        _record_table(
            inv.get("plugins", []),
            [
                ("id", "ID"),
                ("title", "Area"),
                ("description", "Description"),
                ("required_fields", "Required fields"),
                ("fields", "Known fields"),
            ],
        ),
    )


def _data_storage(inv: dict[str, Any]) -> str:
    return _section(
        "Data Paths",
        _record_table(
            inv.get("data_paths", []),
            [
                ("id", "ID"),
                ("title", "Path"),
                ("description", "Description"),
                ("environment_override", "Override"),
                ("source", "Source"),
            ],
        ),
    )


def _safety(inv: dict[str, Any]) -> str:
    return _section(
        "Approval Decisions",
        _record_table(
            inv.get("safety", []),
            [
                ("id", "ID"),
                ("title", "Label"),
                ("decision", "Decision"),
                ("description", "Description"),
                ("source", "Source"),
            ],
        ),
    )


def _environment(inv: dict[str, Any]) -> str:
    return _section(
        "Environment And Config",
        _record_table(
            inv.get("environment", []),
            [("variable", "Variable"), ("description", "Purpose"), ("source", "Source")],
        ),
    )


def _cli(inv: dict[str, Any]) -> str:
    return _section(
        "Command-line Options",
        _record_table(
            inv.get("cli_options", []),
            [
                ("command", "Command"),
                ("option", "Option"),
                ("description", "Purpose"),
                ("default", "Default"),
                ("source", "Source"),
            ],
        ),
    )


def _screenshots(inv: dict[str, Any]) -> str:
    screenshots = inv.get("metadata", {}).get("screenshots", {}).get("screenshots", {})
    rows = []
    if isinstance(screenshots, dict):
        for screenshot_id, shot in sorted(screenshots.items()):
            if not isinstance(shot, dict):
                continue
            rows.append(
                {
                    "id": screenshot_id,
                    "title": shot.get("title", ""),
                    "status": shot.get("status", ""),
                    "review_status": shot.get("review_status", ""),
                    "source": shot.get("source", ""),
                    "output": shot.get("output", ""),
                    "alt": shot.get("alt", ""),
                }
            )
    return _section(
        "Screenshot Manifest",
        _record_table(
            rows,
            [
                ("id", "ID"),
                ("title", "Title"),
                ("status", "Status"),
                ("review_status", "Review"),
                ("source", "Source"),
                ("output", "Output"),
                ("alt", "Alt text"),
            ],
        ),
    )


PAGE_DEFS = [
    ("index", "Reference Tables", "Compact lookup tables for Row-Bot features and settings.", "scripts/docs/generate_mdx.py", lambda inv: _generated_index()),
    ("tools", "Tools", "Generated reference for Row-Bot tools and tool guides.", "scripts/docs/collect_inventory.py", _tools),
    ("providers", "Providers", "Generated reference for model providers and provider risk labels.", "scripts/docs/collect_inventory.py", _providers),
    ("settings", "Settings", "Generated reference for settings tabs.", "docs-content/metadata/settings.yml", _settings),
    ("settings-controls", "Settings Controls", "Generated control-level settings reference with defaults, effects, dependencies, restart notes, and security notes.", "scripts/docs/collect_inventory.py", _settings_controls),
    ("home-tabs", "Home Tabs", "Generated reference for Home tab coverage.", "docs-content/metadata/home_tabs.yml", _home_tabs),
    ("channels", "Channels", "Generated reference for messaging channels.", "scripts/docs/collect_inventory.py", _channels),
    ("skills", "Skills", "Generated reference for bundled skills and tool guides.", "scripts/docs/collect_inventory.py", _skills),
    ("mcp", "MCP", "Generated reference for recommended MCP servers.", "src/row_bot/mcp_client/recommended_servers.json", _mcp),
    ("plugins", "Plugins", "Generated reference for plugin manifest behavior.", "src/row_bot/plugins/manifest.py", _plugins),
    ("data-storage", "Data Storage", "Generated reference for local data paths and storage.", "src/row_bot/data_paths.py", _data_storage),
    ("safety-approvals", "Safety And Approvals", "Generated reference for approval policy and safety modes.", "src/row_bot/approval_policy.py", _safety),
    ("environment-and-config", "Environment And Config", "Generated reference for important environment variables.", "src/row_bot/brand.py", _environment),
    ("cli", "CLI", "Generated reference for launcher, access, server, and plugin command-line options.", "src/row_bot/launcher.py, src/row_bot/access/cli.py", _cli),
    ("screenshots", "Screenshots", "Generated reference for automated screenshot coverage.", "docs-content/metadata/screenshots.yml", _screenshots),
]


def _generated_index() -> str:
    rows = []
    for slug, title, description, _source, _builder in PAGE_DEFS:
        if slug == "index":
            continue
        rows.append([f"[{title}](/docs/reference/generated/{slug})", description])
    return _section("Generated Pages", _table(["Page", "Description"], rows))


def inventory_hash(inventory: dict[str, Any]) -> str:
    payload = json.dumps(inventory, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def render_pages(inventory: dict[str, Any]) -> dict[Path, str]:
    template = TEMPLATE.read_text(encoding="utf-8")
    digest = inventory_hash(inventory)
    pages: dict[Path, str] = {}
    for slug, title, description, source, builder in PAGE_DEFS:
        body = builder(inventory)
        text = template.format(
            title=title,
            description=description,
            source=source,
            inventory_hash=digest,
            body=body,
        ).rstrip() + "\n"
        pages[GENERATED_DIR / f"{slug}.mdx"] = text
    return pages


def load_inventory(path: Path | None) -> dict[str, Any]:
    if path is None:
        return build_inventory()
    inventory_file = path / "inventory.json" if path.is_dir() else path
    return json.loads(inventory_file.read_text(encoding="utf-8"))


def write_pages(pages: dict[Path, str]) -> None:
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    for path, text in pages.items():
        path.write_text(text, encoding="utf-8")


def check_pages(pages: dict[Path, str]) -> list[str]:
    errors: list[str] = []
    for path, expected in pages.items():
        if not path.exists():
            errors.append(f"Missing generated page: {path.relative_to(ROOT)}")
            continue
        actual = path.read_text(encoding="utf-8")
        if actual != expected:
            errors.append(f"Stale generated page: {path.relative_to(ROOT)}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate generated public docs MDX pages")
    parser.add_argument("--inventory", default=None, help="Inventory directory or inventory.json")
    parser.add_argument("--check", action="store_true", help="Fail if generated pages are stale")
    args = parser.parse_args()

    inventory = load_inventory(Path(args.inventory).resolve() if args.inventory else None)
    pages = render_pages(inventory)
    if args.check:
        errors = check_pages(pages)
        if errors:
            for error in errors:
                print(f"ERROR: {error}")
            return 1
        print("Generated MDX pages are current")
        return 0
    write_pages(pages)
    print(f"Wrote {len(pages)} generated MDX pages to {GENERATED_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
