"""Enforce the initial headless import and public type-annotation boundaries.

This scoped ratchet does not pretend to type-check the untyped legacy graph.
DTO value/schema conformance is tested by the protocol contract suite.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SCOPES = ("application", "runtime", "projection", "api")
FORBIDDEN_IMPORTS = ("nicegui", "webview", "row_bot.ui", "row_bot.app",
                     "row_bot.developer.ui", "row_bot.designer.editor")


@dataclass(frozen=True)
class Finding:
    line: int
    code: str
    message: str


def inspect_source(source: str) -> list[Finding]:
    """Report concrete layer violations and unannotated public boundary methods."""
    tree = ast.parse(source)
    findings = []
    for node in ast.walk(tree):
        imports = []
        if isinstance(node, ast.Import):
            imports = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            imports = [node.module or ""]
            imports.extend(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute)):
            name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr
            if name in {"__import__", "import_module"} and node.args and isinstance(node.args[0], ast.Constant):
                imports = [str(node.args[0].value)]
        for module in imports:
            if any(module == forbidden or module.startswith(forbidden + ".") for forbidden in FORBIDDEN_IMPORTS):
                findings.append(Finding(node.lineno, "CP001", "Headless boundary imports presentation code"))
                break
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name.startswith("_"):
            continue
        arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
        arguments.extend(arg for arg in (node.args.vararg, node.args.kwarg) if arg is not None)
        missing = [arg.arg for arg in arguments if arg.arg not in {"self", "cls"} and arg.annotation is None]
        if missing or node.returns is None:
            findings.append(Finding(node.lineno, "CP002", f"Public boundary {node.name} needs parameter/return annotations"))
    return findings


def boundary_paths() -> list[Path]:
    paths = [path for scope in SCOPES for path in (ROOT / "src" / "row_bot" / scope).rglob("*.py")]
    paths.extend(ROOT / "src" / "row_bot" / path for path in (
        "conversation_resources.py", "file_context.py", "message_projection.py", "developer/inspector_snapshot.py",
    ))
    return sorted(path for path in paths if path.is_file())


def main() -> int:
    count = 0
    paths = boundary_paths()
    for path in paths:
        for finding in inspect_source(path.read_text(encoding="utf-8")):
            count += 1
            print(f"{path.relative_to(ROOT).as_posix()}:{finding.line}: {finding.code} {finding.message}")
    print(f"Client platform boundary ratchet: {len(paths)} files, {count} violations")
    return int(count > 0)


if __name__ == "__main__":
    sys.exit(main())
