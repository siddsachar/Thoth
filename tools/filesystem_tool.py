"""Filesystem tool — sandboxed file operations within a user-configured workspace."""

from __future__ import annotations

from tools.base import BaseTool
from tools import registry

# Operations the user can enable, grouped by risk level
_SAFE_OPS = ["read_file", "list_directory", "file_search"]
_WRITE_OPS = ["write_file", "copy_file"]
_DESTRUCTIVE_OPS = ["move_file", "file_delete"]
ALL_OPERATIONS = _SAFE_OPS + _WRITE_OPS + _DESTRUCTIVE_OPS

# Default: safe + write operations enabled
DEFAULT_OPERATIONS = _SAFE_OPS + _WRITE_OPS


class FileSystemTool(BaseTool):

    @property
    def name(self) -> str:
        return "filesystem"

    @property
    def display_name(self) -> str:
        return "📁 Filesystem"

    @property
    def description(self) -> str:
        return (
            "Read, write, search, copy, move, and delete files within a "
            "sandboxed workspace folder. Use this when the user asks to "
            "create files, read local files, organise folders, save notes, "
            "or manage files on disk."
        )

    @property
    def enabled_by_default(self) -> bool:
        return False  # Must configure workspace first

    @property
    def required_api_keys(self) -> dict[str, str]:
        return {}

    @property
    def config_schema(self) -> dict[str, dict]:
        return {
            "workspace_root": {
                "label": "Workspace folder",
                "type": "folder",
                "default": "",
            },
            "selected_operations": {
                "label": "Allowed operations",
                "type": "multicheck",
                "default": DEFAULT_OPERATIONS,
                "options": ALL_OPERATIONS,
            },
        }

    @property
    def destructive_tool_names(self) -> set[str]:
        return {"move_file", "file_delete"}

    # ── Build the toolkit tools ──────────────────────────────────────────────
    def _get_workspace_root(self) -> str:
        root = self.get_config("workspace_root", "")
        if not root:
            raise ValueError(
                "Filesystem workspace folder is not configured. "
                "Set it in Settings → Tools → 📁 Filesystem."
            )
        return root

    def _get_selected_operations(self) -> list[str]:
        ops = self.get_config("selected_operations", DEFAULT_OPERATIONS)
        return [op for op in ops if op in ALL_OPERATIONS]

    def as_langchain_tools(self) -> list:
        """Return the selected FileManagementToolkit tools, sandboxed to
        the configured workspace root.  The default ``read_file`` tool is
        replaced with a custom version that can also read PDF files."""
        import os
        from langchain_community.agent_toolkits import FileManagementToolkit

        root = self._get_workspace_root()
        if not os.path.isdir(root):
            # Return nothing if workspace doesn't exist yet
            return []

        selected = self._get_selected_operations()
        if not selected:
            return []

        toolkit = FileManagementToolkit(
            root_dir=root,
            selected_tools=selected,
        )
        tools = toolkit.get_tools()

        # Replace the default read_file with our PDF-aware version
        if "read_file" in selected:
            tools = [t for t in tools if t.name != "read_file"]
            tools.append(_make_pdf_aware_read_tool(root))

        return tools

    def execute(self, query: str) -> str:
        # Not used — as_langchain_tools() provides individual tools directly
        return "Use the individual file operations instead."


_MAX_READ_CHARS = 80_000  # ~24K tokens — main agent trims further via _pre_model_trim


def _make_pdf_aware_read_tool(root_dir: str):
    """Create a ``read_file`` StructuredTool that can read both text and PDF
    files.  Paths are resolved relative to *root_dir* and validated to stay
    within the sandbox.  Output is capped at ``_MAX_READ_CHARS`` to prevent
    a single tool result from blowing up the agent context window."""
    import os
    from pathlib import Path
    from langchain_core.tools import StructuredTool

    def _cap(text: str, label: str = "") -> str:
        """Truncate *text* to _MAX_READ_CHARS with a notice if trimmed."""
        if len(text) <= _MAX_READ_CHARS:
            return text
        suffix = (
            f"\n\n[Truncated — showing first {_MAX_READ_CHARS:,} characters"
            f"{label}. File is {len(text):,} characters total.]"
        )
        return text[:_MAX_READ_CHARS] + suffix

    def read_file(file_path: str) -> str:
        """Read the contents of a file. For PDF files, extracts all text
        from every page. For CSV/Excel/JSON files, returns schema, stats,
        and a preview. The file_path is relative to the workspace root.
        For Excel files, append '::SheetName' to read a specific sheet
        (e.g. 'data.xlsx::Sheet2')."""
        # Parse optional sheet specifier for Excel files
        sheet_name = ""
        if "::" in file_path:
            file_path, sheet_name = file_path.rsplit("::", 1)

        resolved = Path(root_dir) / file_path
        resolved = resolved.resolve()

        # Sandbox check — must stay within root
        if not str(resolved).startswith(str(Path(root_dir).resolve())):
            return f"Error: path '{file_path}' is outside the workspace."

        if not resolved.exists():
            return f"Error: file not found: {file_path}"

        # ── Structured data files (CSV, Excel, JSON) ────────────────────
        from data_reader import is_data_file, read_data_file
        if is_data_file(resolved.name):
            return read_data_file(resolved, sheet=sheet_name,
                                  max_chars=_MAX_READ_CHARS)

        if resolved.suffix.lower() == ".pdf":
            try:
                from pypdf import PdfReader
                reader = PdfReader(str(resolved))
                total_pages = len(reader.pages)
                pages = []
                for i, page in enumerate(reader.pages, 1):
                    text = page.extract_text() or ""
                    if text.strip():
                        pages.append(f"--- Page {i} ---\n{text}")
                if not pages:
                    return f"PDF file '{file_path}' contains no extractable text."
                full = "\n\n".join(pages)
                return _cap(full, f" of {total_pages} pages")
            except Exception as exc:
                return f"Error reading PDF '{file_path}': {exc}"
        else:
            try:
                text = resolved.read_text(encoding="utf-8", errors="replace")
                return _cap(text)
            except Exception as exc:
                return f"Error reading '{file_path}': {exc}"

    return StructuredTool.from_function(
        func=read_file,
        name="read_file",
        description=(
            "Read the contents of a file (including PDF, CSV, Excel, and JSON files). "
            "For CSV/Excel/JSON, returns column schema, statistics, and a preview. "
            "For Excel, append '::SheetName' to the path to read a specific sheet. "
            "The file_path should be relative to the workspace root."
        ),
    )


registry.register(FileSystemTool())
