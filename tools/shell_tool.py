"""Shell tool — execute shell commands with a persistent working directory.

Provides the agent with a ``run_command`` tool that executes shell commands
on the user's machine.  Working directory is tracked across calls within
the same thread (conversation).  The workspace folder is shared with the
Filesystem tool (configured once in Settings → System Access).

Safety
------
* **Blocked commands** — patterns that match catastrophic/irreversible
  operations (e.g. ``rm -rf /``, ``format C:``) are refused outright.
* **Safe commands** — read-only / informational commands (``ls``, ``pwd``,
  ``git status``, …) execute automatically without user approval.
* **Everything else** — triggers a LangGraph ``interrupt()`` so the user
  can approve or deny before execution.

Shell history is persisted to ``~/.thoth/shell_history.json`` so that it
survives app restarts (the subprocess session itself does not survive).
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import platform
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime
from typing import Any

from langchain_core.tools import StructuredTool
from langgraph.types import interrupt

from tools.base import BaseTool
from tools import registry

logger = logging.getLogger(__name__)

# ── Data directory for shell history ─────────────────────────────────────────
DATA_DIR = pathlib.Path(
    os.environ.get("THOTH_DATA_DIR", pathlib.Path.home() / ".thoth")
)
DATA_DIR.mkdir(parents=True, exist_ok=True)
_HISTORY_PATH = DATA_DIR / "shell_history.json"


# ═════════════════════════════════════════════════════════════════════════════
# SAFETY CLASSIFICATION
# ═════════════════════════════════════════════════════════════════════════════

# Patterns that are ALWAYS blocked (catastrophic / irreversible)
_BLOCKED_PATTERNS: list[re.Pattern] = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"\brm\s+(-[a-z]*f[a-z]*\s+)?/\s*$",          # rm -rf /
        r"\brm\s+-[a-z]*f[a-z]*\s+~/?$",               # rm -rf ~
        r"\bmkfs\b",                                     # format filesystem
        r"\bdd\s+.*of=/dev/",                            # dd to device
        r"\bformat\s+[a-z]:",                            # Windows format drive
        r">\s*/dev/sd[a-z]",                             # overwrite disk
        r"\b(shutdown|reboot|halt|poweroff)\b",          # system power
        r"\breg\s+delete\b.*\\\\HKLM",                  # Windows registry delete
        r"Remove-Item\s+.*-Recurse.*\s+[A-Z]:\\$",      # PowerShell rm C:\
        r"del\s+/[sq]\s+[A-Z]:\\",                       # Windows del /s C:\
        r":(){ :\|:& };:",                               # fork bomb
    ]
]

# Command prefixes considered safe (auto-execute, no approval needed)
_SAFE_PREFIXES: list[str] = [
    # Navigation & listing
    "ls", "dir", "pwd", "cd", "pushd", "popd", "tree",
    "Get-Location", "Set-Location", "Get-ChildItem",
    # Reading
    "cat", "head", "tail", "less", "more", "type",
    "Get-Content", "Select-String",
    "wc", "file", "stat", "du", "df",
    # Info
    "echo", "date", "whoami", "hostname", "uname",
    "which", "where", "command -v", "Get-Command",
    "Write-Output", "Write-Host",
    # Search
    "find", "grep", "rg", "fd", "ag",
    # Version / package info
    "python --version", "python3 --version", "node --version",
    "npm --version", "pip --version", "pip list", "pip show",
    "pip freeze", "conda list", "conda info",
    # Git (read-only)
    "git status", "git log", "git diff", "git branch",
    "git remote", "git show", "git tag", "git stash list",
    # System info
    "systeminfo", "ver", "lsb_release",
    "top -l 1", "ps", "tasklist",
    "ipconfig", "ifconfig", "ip addr",
    "ping", "nslookup", "dig", "traceroute", "tracert",
    "curl", "wget",
    # Env
    "env", "printenv", "$env:",
]


def classify_command(command: str, extra_blocked: list[re.Pattern] | None = None) -> str:
    """Classify a command as ``'safe'``, ``'needs_approval'``, or ``'blocked'``.

    Parameters
    ----------
    command : str
        The shell command to classify.
    extra_blocked : list[re.Pattern] | None
        Additional user-configured blocked patterns.

    Returns
    -------
    str
        One of ``'safe'``, ``'needs_approval'``, ``'blocked'``.
    """
    cmd = command.strip()

    # Check built-in blocked patterns
    for pattern in _BLOCKED_PATTERNS:
        if pattern.search(cmd):
            return "blocked"

    # Check user-configured blocked patterns
    if extra_blocked:
        for pattern in extra_blocked:
            if pattern.search(cmd):
                return "blocked"

    # Check safe prefixes
    cmd_lower = cmd.lower()
    for prefix in _SAFE_PREFIXES:
        if cmd_lower.startswith(prefix.lower()):
            return "safe"

    return "needs_approval"


# ═════════════════════════════════════════════════════════════════════════════
# SHELL SESSION — tracks working directory across commands
# ═════════════════════════════════════════════════════════════════════════════

_IS_WINDOWS = platform.system() == "Windows"


class ShellSession:
    """A logical shell session that tracks cwd across commands.

    Each ``run_command`` call spawns a short-lived subprocess (reliable),
    but the working directory is carried forward between calls so ``cd``
    works as expected.
    """

    def __init__(self, working_dir: str | None = None):
        self.cwd: str = working_dir or os.path.expanduser("~")
        self._lock = threading.Lock()

    def run_command(self, command: str, timeout: int = 120) -> dict:
        """Execute *command* and return a result dict.

        Returns
        -------
        dict
            ``{command, output, exit_code, cwd, duration}``
        """
        start = time.time()

        with self._lock:
            try:
                if _IS_WINDOWS:
                    # PowerShell: run command, then write cwd marker to
                    # stderr so it doesn't interfere with pipeline-buffered
                    # stdout (pipeline output can appear after Write-Host).
                    # Capture $? immediately after the user command (before
                    # any other statement resets it), then emit cwd marker,
                    # then exit 1 if the command failed.  This ensures
                    # cmdlet errors (which don't set $LASTEXITCODE) still
                    # surface as a non-zero process exit code.
                    wrapped = (
                        f"{command}\n"
                        f"$_ok = $?\n"
                        f"[Console]::Error.WriteLine("
                        f"'___THOTH_CWD___' + (Get-Location).Path)\n"
                        f"if (-not $_ok) {{ exit 1 }}"
                    )
                    proc = subprocess.run(
                        ["powershell", "-NoProfile", "-NoLogo", "-Command", wrapped],
                        capture_output=True, text=True,
                        cwd=self.cwd, timeout=timeout,
                        encoding="utf-8", errors="replace",
                    )
                else:
                    # Unix: same idea — cwd marker goes to stderr
                    wrapped = (
                        f"{command}\n"
                        f'echo "___THOTH_CWD___$(pwd)" >&2'
                    )
                    shell = os.environ.get("SHELL", "/bin/bash")
                    proc = subprocess.run(
                        [shell, "-c", wrapped],
                        capture_output=True, text=True,
                        cwd=self.cwd, timeout=timeout,
                        encoding="utf-8", errors="replace",
                    )

                stdout = proc.stdout or ""
                stderr = proc.stderr or ""

                # stdout is pure command output — no marker to strip
                actual_output = stdout.rstrip()

                # Extract new cwd from stderr (last occurrence in case
                # the command itself printed the marker string)
                marker = "___THOTH_CWD___"
                if marker in stderr:
                    marker_pos = stderr.rfind(marker)
                    cwd_part = stderr[marker_pos + len(marker):]
                    new_cwd = cwd_part.strip().split("\n")[0].strip()
                    if new_cwd and os.path.isdir(new_cwd):
                        self.cwd = new_cwd
                    # Remove the marker line from stderr so it doesn't
                    # leak into the output shown to the user / LLM
                    stderr = stderr[:marker_pos].rstrip()

                # Combine stdout and stderr
                output = actual_output
                if stderr.strip():
                    if output:
                        output += "\n" + stderr.rstrip()
                    else:
                        output = stderr.rstrip()

                duration = round(time.time() - start, 2)
                return {
                    "command": command,
                    "output": output,
                    "exit_code": proc.returncode,
                    "cwd": self.cwd,
                    "duration": duration,
                }

            except subprocess.TimeoutExpired:
                return {
                    "command": command,
                    "output": f"Command timed out after {timeout}s",
                    "exit_code": -1,
                    "cwd": self.cwd,
                    "duration": timeout,
                }
            except Exception as exc:
                duration = round(time.time() - start, 2)
                return {
                    "command": command,
                    "output": f"Error: {exc}",
                    "exit_code": -1,
                    "cwd": self.cwd,
                    "duration": duration,
                }


class ShellSessionManager:
    """One :class:`ShellSession` per ``thread_id``."""

    def __init__(self):
        self._sessions: dict[str, ShellSession] = {}
        self._lock = threading.Lock()

    def get_session(self, thread_id: str, working_dir: str | None = None) -> ShellSession:
        with self._lock:
            if thread_id not in self._sessions:
                self._sessions[thread_id] = ShellSession(working_dir)
            return self._sessions[thread_id]

    def kill_session(self, thread_id: str) -> None:
        with self._lock:
            self._sessions.pop(thread_id, None)

    def kill_all(self) -> None:
        with self._lock:
            self._sessions.clear()


# Global session manager
_session_manager = ShellSessionManager()


def get_session_manager() -> ShellSessionManager:
    """Return the global session manager (for cleanup from UI code)."""
    return _session_manager


# ═════════════════════════════════════════════════════════════════════════════
# SHELL HISTORY PERSISTENCE
# ═════════════════════════════════════════════════════════════════════════════

def _load_history() -> dict[str, list[dict]]:
    if _HISTORY_PATH.exists():
        try:
            return json.loads(_HISTORY_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_history(history: dict[str, list[dict]]) -> None:
    try:
        _HISTORY_PATH.write_text(
            json.dumps(history, default=str), encoding="utf-8"
        )
    except OSError:
        logger.warning("Failed to save shell history", exc_info=True)


def get_shell_history(thread_id: str) -> list[dict]:
    """Get shell history for a thread."""
    return _load_history().get(thread_id, [])


def append_shell_history(thread_id: str, entry: dict) -> None:
    """Append a command entry to shell history for a thread."""
    history = _load_history()
    history.setdefault(thread_id, []).append(entry)
    _save_history(history)


def clear_shell_history(thread_id: str) -> None:
    """Clear shell history for a thread."""
    history = _load_history()
    if thread_id in history:
        del history[thread_id]
        _save_history(history)


# ═════════════════════════════════════════════════════════════════════════════
# SHELL TOOL
# ═════════════════════════════════════════════════════════════════════════════

class ShellTool(BaseTool):

    @property
    def name(self) -> str:
        return "shell"

    @property
    def display_name(self) -> str:
        return "🖥️ Shell"

    @property
    def description(self) -> str:
        return (
            "Execute shell commands on the user's computer. "
            "Use this when the user asks to run terminal commands, install packages, "
            "check system information, run scripts, manage processes, or perform "
            "any task that requires command-line access. "
            "Commands run in a persistent session — cd persists across calls "
            "within the same conversation."
        )

    @property
    def enabled_by_default(self) -> bool:
        return True

    @property
    def config_schema(self) -> dict[str, dict]:
        return {
            "blocked_commands": {
                "label": "Additional blocked patterns (comma-separated)",
                "type": "text",
                "default": "",
            },
        }

    @property
    def destructive_tool_names(self) -> set[str]:
        # Empty — the tool self-gates via interrupt() based on per-command
        # safety classification (safe commands auto-run, others prompt).
        return set()

    def _get_workspace_root(self) -> str | None:
        """Read workspace root from the Filesystem tool's config (shared)."""
        fs_tool = registry.get_tool("filesystem")
        if fs_tool:
            root = fs_tool.get_config("workspace_root", "")
            if root and os.path.isdir(root):
                return root
        return None

    def _get_extra_blocked(self) -> list[re.Pattern]:
        """Parse user-configured additional blocked patterns."""
        raw = self.get_config("blocked_commands", "")
        if not raw:
            return []
        patterns = []
        for part in raw.split(","):
            part = part.strip()
            if part:
                try:
                    patterns.append(re.compile(re.escape(part), re.IGNORECASE))
                except re.error:
                    pass
        return patterns

    def execute(self, command: str) -> str:
        """Run a shell command.  Self-gates with ``interrupt()`` for
        non-safe commands."""
        command = command.strip()
        if not command:
            return "No command provided."

        # ── Safety classification ────────────────────────────────────────
        extra = self._get_extra_blocked()
        classification = classify_command(command, extra)

        if classification == "blocked":
            return (
                f"🚫 BLOCKED: This command matches a blocked pattern and "
                f"cannot be executed: {command}"
            )

        if classification == "needs_approval":
            # In background workflows, check task-scoped command allowlist
            _is_bg = False
            try:
                from agent import is_background_workflow, _task_allowed_commands_var
                _is_bg = is_background_workflow()
            except ImportError:
                pass

            if _is_bg:
                allowed = _task_allowed_commands_var.get() or []
                cmd_lower = command.strip().lower()
                if not any(cmd_lower.startswith(prefix.lower())
                           for prefix in allowed):
                    if allowed:
                        return (
                            f"⚠️ BLOCKED: Command '{command}' is not in this "
                            f"task's allowed commands list. The task owner can "
                            f"add it in the task editor under "
                            f"'🔒 Background permissions'.\n"
                            f"Currently allowed prefixes: {', '.join(allowed)}\n"
                            f"Do NOT retry this tool."
                        )
                    return (
                        f"⚠️ BLOCKED: Command '{command}' requires approval "
                        f"and this task has no allowed commands configured. "
                        f"The task owner can configure allowed command "
                        f"prefixes in the task editor under "
                        f"'🔒 Background permissions'.\n"
                        f"Do NOT retry this tool."
                    )
                # Command matches an allowed prefix — skip interrupt, proceed
            else:
                # Interactive session — gate with interrupt
                approval = interrupt({
                    "tool": "run_command",
                    "label": "Run shell command",
                    "description": f"Run shell command: {command}",
                    "args": {"command": command},
                })
                if not approval:
                    return "Command cancelled by user."

        # ── Execute ──────────────────────────────────────────────────────
        try:
            from agent import _current_thread_id_var
            thread_id = _current_thread_id_var.get() or "default"
        except ImportError:
            thread_id = "default"

        working_dir = self._get_workspace_root()
        session = _session_manager.get_session(thread_id, working_dir)
        result = session.run_command(command)

        # Persist to history
        result["timestamp"] = datetime.now().isoformat()
        append_shell_history(thread_id, result)

        # Format for LLM
        output = result["output"]
        exit_code = result["exit_code"]
        duration = result["duration"]
        cwd = result["cwd"]

        parts = [f"$ {command}"]
        if output:
            parts.append(output)
        parts.append(f"\n[Exit code: {exit_code} | Duration: {duration}s | cwd: {cwd}]")

        return "\n".join(parts)

    def as_langchain_tools(self) -> list:
        """Return the ``run_command`` tool."""
        tool_instance = self

        def run_command(command: str) -> str:
            """Execute a shell command on the user's computer.

            Commands run in a persistent session — cd, environment changes,
            etc. persist across calls within the same conversation.
            Safe commands (ls, pwd, git status, …) run automatically;
            other commands require user approval.

            Args:
                command: The shell command to execute
                        (e.g. 'ls -la', 'pip install requests',
                         'git status').
            """
            try:
                return tool_instance.execute(command)
            except Exception as exc:
                from langgraph.errors import GraphInterrupt
                if isinstance(exc, GraphInterrupt):
                    raise
                logger.error("Shell tool error: %s", exc, exc_info=True)
                return f"Error executing command: {exc}"

        return [
            StructuredTool.from_function(
                func=run_command,
                name="run_command",
                description=(
                    "Execute a shell command on the user's computer. "
                    "Commands run in a persistent session — cd persists "
                    "across calls. Safe commands (ls, pwd, git status, etc.) "
                    "run automatically; other commands require user approval. "
                    "Returns the command output and exit code."
                ),
            )
        ]


registry.register(ShellTool())
