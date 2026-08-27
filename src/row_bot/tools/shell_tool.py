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

Shell history is persisted to ``~/.row-bot/shell_history.json`` so that it
survives app restarts (the subprocess session itself does not survive).
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import threading
import time
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool
from row_bot.data_paths import get_row_bot_data_dir
from row_bot.process_cancellation import run_cancellable_subprocess
from row_bot.tools.base import BaseTool
from row_bot.tools import registry
from row_bot.tools.approval_gate import gate_action

logger = logging.getLogger(__name__)

# ── Data directory for shell history ─────────────────────────────────────────
DATA_DIR = get_row_bot_data_dir()
DATA_DIR.mkdir(parents=True, exist_ok=True)
_HISTORY_PATH = DATA_DIR / "shell_history.json"


# ═════════════════════════════════════════════════════════════════════════════
# SAFETY CLASSIFICATION
# ═════════════════════════════════════════════════════════════════════════════

# Patterns that are ALWAYS blocked (catastrophic / irreversible)
_BLOCKED_PATTERNS: list[re.Pattern] = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"\brm\s+(-[a-z]*f[a-z]*\s+)?/(\s*$|\*)",      # rm -rf / AND rm -rf /*
        r"\brm\s+-[a-z]*r[a-z]*\s+/(etc|var|usr|home|boot|sys|proc)\b",  # rm system dirs
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
        r"\|\s*(ba)?sh\b",                               # pipe to shell
        r"\|\s*python[23]?\s+(-c\b|-)",                  # pipe to python -c / -
        r"\bInvoke-Expression\b|\biex\b",                # PowerShell code exec
        r"\bchmod\s+(-R\s+)?000\s+/",                    # chmod 000 /
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
    # NOTE: curl/wget intentionally excluded — prompt injection vectors
    # Env
    "env", "printenv", "$env:",
]

# Operators that make any command potentially dangerous — if present,
# skip safe-prefix matching and classify as needs_approval.
# Catches: echo "x" > file, ls; rm -rf /, cat f | bash, $(cmd), `cmd`
_UNSAFE_OPERATORS = re.compile(
    r";|&&|\|\||[|]|>>?(?!/dev/null)"   # chain, pipe, redirect (except >/dev/null)
    r"|\$\("                             # command substitution
    r"|`"                                # backtick substitution
)


def _strip_quoted(text: str) -> str:
    """Remove single- and double-quoted strings from *text*.

    Returns the command with all quoted segments replaced by a
    placeholder so that operators inside quotes (e.g. ``echo "a > b"``)
    do not trigger the unsafe-operator check.  This is a lightweight
    approach that covers the common case without a full shell parser.
    """
    result: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in ('"', "'"):
            # Skip to matching close quote
            quote = ch
            i += 1
            while i < n and text[i] != quote:
                if text[i] == '\\' and quote == '"' and i + 1 < n:
                    i += 1  # skip escaped char in double quotes
                i += 1
            i += 1  # skip closing quote
            result.append("_Q_")  # placeholder
        else:
            result.append(ch)
            i += 1
    return "".join(result)


def _append_process_note(output: str, note: str) -> str:
    text = str(output or "").rstrip()
    return f"{text}\n{note}" if text else note


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

    # Multi-line commands: classify each line, take the most severe.
    if '\n' in cmd:
        severities = {"blocked": 2, "needs_approval": 1, "safe": 0}
        worst = "safe"
        for line in cmd.split('\n'):
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            line_class = classify_command(line, extra_blocked)
            if severities.get(line_class, 0) > severities.get(worst, 0):
                worst = line_class
            if worst == "blocked":
                return "blocked"
        return worst

    # Check built-in blocked patterns
    for pattern in _BLOCKED_PATTERNS:
        if pattern.search(cmd):
            return "blocked"

    # Check user-configured blocked patterns
    if extra_blocked:
        for pattern in extra_blocked:
            if pattern.search(cmd):
                return "blocked"

    # Disqualify from "safe" if the command contains shell operators that
    # can chain, redirect, or substitute — these make any prefix unsafe.
    # e.g. "echo hello > /etc/passwd" or "ls; rm -rf /home"
    # Strip quoted strings first so operators inside quotes don't trigger.
    if _UNSAFE_OPERATORS.search(_strip_quoted(cmd)):
        return "needs_approval"

    # Check safe prefixes (only reached for simple, single commands)
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
                    # PowerShell starts fresh for every call, so clear its
                    # process-local error list and native status before the
                    # submitted command. Preserve a native exit code and fail
                    # if the command added any PowerShell error record, even
                    # when a later statement happened to succeed.
                    wrapped = (
                        "$global:Error.Clear()\n"
                        "$global:LASTEXITCODE = 0\n"
                        "$__row_bot_terminated = $false\n"
                        "try {\n"
                        f"& {{\n{command}\n}}\n"
                        "$__row_bot_ok = $?\n"
                        "} catch {\n"
                        "$__row_bot_ok = $false\n"
                        "$__row_bot_terminated = $true\n"
                        "[Console]::Error.WriteLine([string]$_)\n"
                        "}\n"
                        "$__row_bot_native_exit = $LASTEXITCODE\n"
                        "$__row_bot_error_count = $Error.Count\n"
                        "[Console]::Error.WriteLine("
                        "'___ROW_BOT_CWD___' + (Get-Location).Path)\n"
                        "if ($__row_bot_native_exit -ne 0) { exit $__row_bot_native_exit }\n"
                        "if ($__row_bot_terminated -or -not $__row_bot_ok -or "
                        "$__row_bot_error_count -gt 0) { exit 1 }"
                    )
                    proc = run_cancellable_subprocess(
                        ["powershell", "-NoProfile", "-NoLogo", "-Command", wrapped],
                        cwd=self.cwd,
                        timeout=timeout,
                        text=True,
                        encoding="utf-8", errors="replace",
                    )
                else:
                    # Unix: same idea — cwd marker goes to stderr
                    wrapped = (
                        f"{command}\n"
                        f'echo "___ROW_BOT_CWD___$(pwd)" >&2'
                    )
                    shell = os.environ.get("SHELL", "/bin/bash")
                    proc = run_cancellable_subprocess(
                        [shell, "-c", wrapped],
                        cwd=self.cwd,
                        timeout=timeout,
                        text=True,
                        encoding="utf-8", errors="replace",
                    )

                if proc.timed_out:
                    return {
                        "command": command,
                        "output": f"Command timed out after {timeout}s",
                        "exit_code": -1,
                        "cwd": self.cwd,
                        "duration": timeout,
                    }
                stdout = proc.stdout or ""
                stderr = proc.stderr or ""
                if proc.cancelled:
                    stderr = _append_process_note(stderr, "Command stopped by user.")

                # stdout is pure command output — no marker to strip
                actual_output = stdout.rstrip()

                # Extract new cwd from stderr (last occurrence in case
                # the command itself printed the marker string)
                marker = "___ROW_BOT_CWD___"
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

    def _get_developer_workspace_root(
        self,
        thread_id: str = "",
        developer_workspace_id: str = "",
    ) -> str | None:
        """Infer the active Developer workspace for a thread, if one exists."""
        clean_thread_id = str(thread_id or "").strip()
        clean_workspace_id = str(developer_workspace_id or "").strip()
        if not clean_workspace_id and (
            not clean_thread_id or clean_thread_id == "default"
        ):
            return None
        try:
            from row_bot.developer.storage import get_workspace
            from row_bot.threads import _get_thread_developer_workspace

            workspace_id = clean_workspace_id or _get_thread_developer_workspace(
                clean_thread_id
            )
            workspace = get_workspace(workspace_id) if workspace_id else None
        except Exception:
            logger.debug("Could not resolve Developer workspace for shell cwd", exc_info=True)
            return None
        if workspace and workspace.path and os.path.isdir(workspace.path):
            return workspace.path
        return None

    def _get_workspace_root(
        self,
        thread_id: str = "",
        developer_workspace_id: str = "",
    ) -> str | None:
        """Resolve shell cwd, preferring Developer workspaces for Developer threads."""
        developer_root = self._get_developer_workspace_root(
            thread_id,
            developer_workspace_id,
        )
        if developer_root:
            return developer_root
        fs_tool = registry.get_tool("filesystem")
        if fs_tool:
            root = fs_tool.get_config("workspace_root", "")
            if root and os.path.isdir(root):
                return root
        return None

    def _get_runtime_configurable(
        self,
        runtime_config: RunnableConfig | None,
    ) -> Mapping[str, Any]:
        """Return LangChain runtime ``configurable`` values, if available."""
        if isinstance(runtime_config, Mapping):
            configurable = runtime_config.get("configurable")
            if isinstance(configurable, Mapping):
                return configurable
        return {}

    def _get_shell_context_ids(
        self,
        runtime_config: RunnableConfig | None = None,
    ) -> tuple[str, str]:
        """Resolve the current thread and Developer workspace for shell execution."""
        configurable = self._get_runtime_configurable(runtime_config)
        thread_id = str(configurable.get("thread_id") or "").strip()
        developer_workspace_id = str(
            configurable.get("developer_workspace_id") or ""
        ).strip()

        if not thread_id or not developer_workspace_id:
            try:
                from row_bot.developer import tool_context

                thread_id = thread_id or str(tool_context.get_thread_id() or "").strip()
                developer_workspace_id = developer_workspace_id or str(
                    tool_context.get_workspace_id() or ""
                ).strip()
                if thread_id and not developer_workspace_id:
                    developer_workspace_id = str(
                        tool_context.infer_workspace_id_from_thread(thread_id) or ""
                    ).strip()
            except Exception:
                logger.debug("Could not resolve Developer shell context", exc_info=True)

        if not thread_id:
            try:
                from row_bot.agent import _current_thread_id_var

                thread_id = _current_thread_id_var.get() or ""
            except ImportError:
                thread_id = ""

        if not thread_id:
            thread_id = "default"
        return thread_id, developer_workspace_id

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

    def execute(
        self,
        command: str,
        runtime_config: RunnableConfig | None = None,
        approval_reason: str = "",
    ) -> str:
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
            # Determine execution context for background-audit compatibility.
            # gate_action() below handles block / approve / allow_all.
            try:
                from row_bot.agent import is_background_workflow

                _is_bg = is_background_workflow()
            except Exception:
                _is_bg = False
            logger.debug("Shell command approval required; background=%s", _is_bg)
            blocked = gate_action(
                {
                    "tool": "run_command",
                    "label": "Run shell command",
                    "description": f"Run shell command: {command}",
                    "approval_reason": approval_reason,
                    "args": {"command": command},
                },
                blocked_message=(
                    f"BLOCKED: This command requires approval and cannot run "
                    f"in Block approval mode: {command}"
                ),
                cancelled_message="Command cancelled by user.",
            )
            if blocked:
                return blocked

        # ── Execute ──────────────────────────────────────────────────────
        thread_id, developer_workspace_id = self._get_shell_context_ids(runtime_config)

        # Always use subprocess — reliable, cross-platform, no PTY contention
        working_dir = self._get_workspace_root(thread_id, developer_workspace_id)
        session_key = (
            thread_id
            if thread_id and thread_id != "default"
            else f"developer:{developer_workspace_id}" if developer_workspace_id else "default"
        )
        session = _session_manager.get_session(session_key, working_dir)
        result = session.run_command(command)

        # Persist to history
        result["timestamp"] = datetime.now().isoformat()
        append_shell_history(session_key, result)

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
        """Return ``run_command`` and ``read_terminal`` tools."""
        tool_instance = self

        def run_command(
            command: str,
            approval_reason: str = "",
            config: RunnableConfig = None,
        ) -> str:
            """Execute a shell command on the user's computer.

            Commands run in a persistent session — cd, environment changes,
            etc. persist across calls within the same conversation.
            Safe commands (ls, pwd, git status, …) run automatically;
            other commands require user approval.

            Args:
                command: The shell command to execute
                        (e.g. 'ls -la', 'pip install requests',
                         'git status').
                approval_reason: One short user-facing sentence explaining
                        why this command is needed if approval is required.
            """
            try:
                return tool_instance.execute(
                    command,
                    runtime_config=config,
                    approval_reason=approval_reason,
                )
            except Exception as exc:
                from langgraph.errors import GraphInterrupt
                if isinstance(exc, GraphInterrupt):
                    raise
                logger.error("Shell tool error: %s", exc, exc_info=True)
                return f"Error executing command: {exc}"

        def read_terminal(lines: int = 50) -> str:
            """Read recent output from the user's interactive terminal.

            This reads the scrollback buffer of the user's xterm.js
            terminal — both user-typed commands and replayed agent
            command results.  Use when the user says "look at my
            terminal" or "what does the terminal show".

            Args:
                lines: Number of recent lines to return (default 50,
                       max 500).  Increase for verbose output.
            """
            try:
                from row_bot.terminal_bridge import TerminalBridge
                bridge = TerminalBridge.get_instance()
                output = bridge.read_output(lines=lines)
                if not output.strip():
                    return "(Terminal is empty — no recent output)"
                return output
            except Exception as exc:
                logger.debug("read_terminal error: %s", exc)
                return f"Could not read terminal: {exc}"

        return [
            StructuredTool.from_function(
                func=run_command,
                name="run_command",
                description=(
                    "Execute a shell command on the user's computer. "
                    "Commands run in a persistent session — cd persists "
                    "across calls. Safe commands (ls, pwd, git status, etc.) "
                    "run automatically; other commands require user approval. "
                    "When approval may be needed, include approval_reason as "
                    "one short sentence explaining why the command is needed. "
                    "Returns the command output and exit code."
                ),
            ),
            StructuredTool.from_function(
                func=read_terminal,
                name="read_terminal",
                description=(
                    "Read recent output from the user's terminal. Returns "
                    "the last N lines of plain text visible in the terminal. "
                    "Use when the user references terminal content or you "
                    "need to inspect what a previous command produced. "
                    "Default 50 lines, max 500."
                ),
            ),
        ]


registry.register(ShellTool())
