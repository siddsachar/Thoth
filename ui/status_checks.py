"""Thoth UI — system health checks for the status bar.

Each check returns a ``CheckResult`` with a uniform interface.
All checks are read-only and non-destructive.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)

_DATA_DIR = pathlib.Path(
    os.environ.get("THOTH_DATA_DIR", pathlib.Path.home() / ".thoth")
)


# ═════════════════════════════════════════════════════════════════════════════
# CHECK RESULT
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class CheckResult:
    """Uniform result from a single health check."""

    name: str
    status: str  # "ok" | "warn" | "error" | "inactive"
    detail: str = ""
    checked_at: float = field(default_factory=time.time)
    settings_tab: str = ""  # which settings tab to open on click

    @property
    def dot_color(self) -> str:
        return {
            "ok": "#4caf50",      # green
            "warn": "#ff9800",    # amber
            "error": "#f44336",   # red
            "inactive": "#666",   # grey
        }.get(self.status, "#666")

    @property
    def icon(self) -> str:
        return {
            "ok": "check_circle",
            "warn": "warning",
            "error": "error",
            "inactive": "radio_button_unchecked",
        }.get(self.status, "help")

    @property
    def status_label(self) -> str:
        return {
            "ok": "Healthy",
            "warn": "Warning",
            "error": "Error",
            "inactive": "Not configured",
        }.get(self.status, "Unknown")


# ═════════════════════════════════════════════════════════════════════════════
# INDIVIDUAL CHECKS
# ═════════════════════════════════════════════════════════════════════════════

def check_ollama() -> CheckResult:
    """Check if Ollama server is reachable."""
    try:
        from models import _ollama_reachable
        if _ollama_reachable(timeout=1.0):
            return CheckResult("Ollama", "ok", "Server reachable", settings_tab="Models")
        return CheckResult("Ollama", "error", "Server unreachable", settings_tab="Models")
    except Exception as exc:
        return CheckResult("Ollama", "error", str(exc), settings_tab="Models")


def check_active_model() -> CheckResult:
    """Check if the configured model is available."""
    try:
        from models import get_current_model
        model = get_current_model()
        if model:
            return CheckResult("Model", "ok", model, settings_tab="Models")
        return CheckResult("Model", "warn", "No model selected", settings_tab="Models")
    except Exception as exc:
        return CheckResult("Model", "error", str(exc), settings_tab="Models")


def check_cloud_api() -> CheckResult:
    """Check if cloud API keys are configured."""
    try:
        from models import is_cloud_available
        if is_cloud_available():
            return CheckResult("Cloud API", "ok", "Keys configured", settings_tab="Cloud")
        return CheckResult("Cloud API", "inactive", "No API keys", settings_tab="Cloud")
    except Exception as exc:
        return CheckResult("Cloud API", "error", str(exc), settings_tab="Cloud")


def check_telegram() -> CheckResult:
    """Check Telegram bot status."""
    try:
        from channels.telegram import is_configured, is_running
        if not is_configured():
            return CheckResult("Telegram", "inactive", "Not configured", settings_tab="Channels")
        if is_running():
            return CheckResult("Telegram", "ok", "Running", settings_tab="Channels")
        return CheckResult("Telegram", "warn", "Stopped", settings_tab="Channels")
    except Exception as exc:
        return CheckResult("Telegram", "error", str(exc), settings_tab="Channels")


def check_channels() -> list[CheckResult]:
    """Dynamic health checks for all registered channels."""
    results = []
    try:
        from channels.registry import all_channels
        for ch in all_channels():
            try:
                if not ch.is_configured():
                    results.append(CheckResult(ch.display_name, "inactive",
                                               "Not configured", settings_tab="Channels"))
                elif ch.is_running():
                    results.append(CheckResult(ch.display_name, "ok",
                                               "Running", settings_tab="Channels"))
                else:
                    results.append(CheckResult(ch.display_name, "warn",
                                               "Stopped", settings_tab="Channels"))
            except Exception as exc:
                results.append(CheckResult(ch.display_name, "error",
                                           str(exc), settings_tab="Channels"))
    except Exception:
        pass
    return results


def check_tunnel() -> CheckResult:
    """Health check for the tunnel subsystem."""
    try:
        from tunnel import tunnel_manager
        if not tunnel_manager.is_available():
            return CheckResult("Tunnel", "inactive", "Not configured",
                               settings_tab="Channels")
        active = tunnel_manager.active_tunnels()
        if active:
            urls = ", ".join(f"{p}\u2192{u}" for p, u in active.items())
            return CheckResult("Tunnel", "ok",
                               f"{len(active)} active: {urls}",
                               settings_tab="Channels")
        return CheckResult("Tunnel", "inactive", "Ready (no active tunnels)",
                           settings_tab="Channels")
    except Exception as exc:
        return CheckResult("Tunnel", "error", str(exc),
                           settings_tab="Channels")


def check_gmail_oauth() -> CheckResult:
    """Check Gmail OAuth token health."""
    try:
        from tools import registry
        if not registry.is_enabled("gmail"):
            return CheckResult("Gmail OAuth", "inactive", "Tool disabled", settings_tab="Gmail")
        tool = registry.get_tool("gmail")
        if tool is None or not tool.is_authenticated():
            return CheckResult("Gmail OAuth", "inactive", "Not authenticated", settings_tab="Gmail")
        status, detail = tool.check_token_health()
        if status in ("valid", "refreshed"):
            label = "Valid" if status == "valid" else "Refreshed"
            return CheckResult("Gmail OAuth", "ok", label, settings_tab="Gmail")
        if status == "expired":
            return CheckResult("Gmail OAuth", "warn", "Token expired", settings_tab="Gmail")
        return CheckResult("Gmail OAuth", "error", detail, settings_tab="Gmail")
    except Exception as exc:
        return CheckResult("Gmail OAuth", "error", str(exc), settings_tab="Gmail")


def check_calendar_oauth() -> CheckResult:
    """Check Calendar OAuth token health."""
    try:
        from tools import registry
        if not registry.is_enabled("calendar"):
            return CheckResult("Calendar OAuth", "inactive", "Tool disabled", settings_tab="Calendar")
        tool = registry.get_tool("calendar")
        if tool is None or not tool.is_authenticated():
            return CheckResult("Calendar OAuth", "inactive", "Not authenticated", settings_tab="Calendar")
        status, detail = tool.check_token_health()
        if status in ("valid", "refreshed"):
            label = "Valid" if status == "valid" else "Refreshed"
            return CheckResult("Calendar OAuth", "ok", label, settings_tab="Calendar")
        if status == "expired":
            return CheckResult("Calendar OAuth", "warn", "Token expired", settings_tab="Calendar")
        return CheckResult("Calendar OAuth", "error", detail, settings_tab="Calendar")
    except Exception as exc:
        return CheckResult("Calendar OAuth", "error", str(exc), settings_tab="Calendar")


def check_x_oauth() -> CheckResult:
    """Check X (Twitter) OAuth token health."""
    try:
        from tools import registry
        if not registry.is_enabled("x"):
            return CheckResult("X OAuth", "inactive", "Tool disabled", settings_tab="Accounts")
        tool = registry.get_tool("x")
        if tool is None or not tool.is_authenticated():
            return CheckResult("X OAuth", "inactive", "Not authenticated", settings_tab="Accounts")
        status, detail = tool.check_token_health()
        if status in ("valid", "refreshed"):
            label = "Valid" if status == "valid" else "Refreshed"
            return CheckResult("X OAuth", "ok", label, settings_tab="Accounts")
        if status == "expired":
            return CheckResult("X OAuth", "warn", "Token expired", settings_tab="Accounts")
        return CheckResult("X OAuth", "error", detail, settings_tab="Accounts")
    except Exception as exc:
        return CheckResult("X OAuth", "error", str(exc), settings_tab="Accounts")


def check_task_scheduler() -> CheckResult:
    """Check APScheduler health."""
    try:
        from tasks import _scheduler
        if _scheduler is None:
            return CheckResult("Scheduler", "warn", "Not started")
        jobs = _scheduler.get_jobs()
        return CheckResult("Scheduler", "ok", f"{len(jobs)} job{'s' if len(jobs) != 1 else ''}")
    except Exception as exc:
        return CheckResult("Scheduler", "error", str(exc))


def check_memory_extraction() -> CheckResult:
    """Check memory extraction pipeline status."""
    try:
        from memory_extraction import get_extraction_status
        status = get_extraction_status()
        last = status.get("last_extraction")
        interval = int(status.get("interval_hours", 6))
        if last:
            try:
                dt = datetime.fromisoformat(last)
                age_h = (datetime.now() - dt).total_seconds() / 3600
                if age_h < interval * 2:
                    return CheckResult(
                        "Knowledge", "ok",
                        f"Last: {dt.strftime('%b %d, %I:%M %p')}",
                        settings_tab="Knowledge",
                    )
                return CheckResult(
                    "Knowledge", "warn",
                    f"Overdue — last: {dt.strftime('%b %d')}",
                    settings_tab="Knowledge",
                )
            except (ValueError, TypeError):
                return CheckResult("Knowledge", "ok", f"Last: {last}", settings_tab="Knowledge")
        return CheckResult("Knowledge", "ok", "Not yet run", settings_tab="Knowledge")
    except Exception as exc:
        return CheckResult("Knowledge", "error", str(exc), settings_tab="Knowledge")


def check_disk_space() -> CheckResult:
    """Check free disk space on the data directory drive."""
    try:
        import shutil
        usage = shutil.disk_usage(str(_DATA_DIR))
        free_gb = usage.free / (1024 ** 3)
        pct_used = (usage.used / usage.total) * 100
        if pct_used > 95:
            return CheckResult("Disk", "error", f"{free_gb:.1f} GB free ({pct_used:.0f}% used)", settings_tab="System")
        if pct_used > 85:
            return CheckResult("Disk", "warn", f"{free_gb:.1f} GB free ({pct_used:.0f}% used)", settings_tab="System")
        return CheckResult("Disk", "ok", f"{free_gb:.1f} GB free", settings_tab="System")
    except Exception as exc:
        return CheckResult("Disk", "error", str(exc), settings_tab="System")


def check_threads_db() -> CheckResult:
    """Quick SQLite integrity check on threads.db."""
    db_path = _DATA_DIR / "threads.db"
    try:
        if not db_path.exists():
            return CheckResult("Threads DB", "ok", "No database yet")
        conn = sqlite3.connect(str(db_path), timeout=2)
        conn.execute("SELECT 1 FROM thread_meta LIMIT 1")
        count = conn.execute("SELECT COUNT(*) FROM thread_meta").fetchone()[0]
        conn.close()
        return CheckResult("Threads DB", "ok", f"{count} thread{'s' if count != 1 else ''}")
    except Exception as exc:
        return CheckResult("Threads DB", "error", str(exc))


def check_faiss_index() -> CheckResult:
    """Check if the FAISS memory vector index exists and is readable."""
    vector_dir = _DATA_DIR / "memory_vectors"
    try:
        index_file = vector_dir / "index.faiss"
        map_file = vector_dir / "id_map.json"
        if not vector_dir.exists():
            return CheckResult("FAISS Index", "ok", "Not yet created")
        if not index_file.exists():
            return CheckResult("FAISS Index", "ok", "Empty index")
        size_kb = index_file.stat().st_size / 1024
        entities = 0
        if map_file.exists():
            try:
                data = json.loads(map_file.read_text())
                entities = len(data) if isinstance(data, list) else 0
            except (json.JSONDecodeError, OSError):
                pass
        return CheckResult(
            "FAISS Index", "ok",
            f"{entities} vectors · {size_kb:.0f} KB",
        )
    except Exception as exc:
        return CheckResult("FAISS Index", "error", str(exc))


def check_dream_cycle() -> CheckResult:
    """Check Dream Cycle status — enabled, last run time."""
    try:
        from dream_cycle import get_dream_status
        status = get_dream_status()
        if not status.get("enabled"):
            return CheckResult("Dream Cycle", "inactive", "Disabled", settings_tab="Knowledge")
        last = status.get("last_run")
        if last:
            try:
                dt = datetime.fromisoformat(last)
                summary = status.get("last_summary", "")
                return CheckResult(
                    "Dream Cycle", "ok",
                    f"{dt.strftime('%b %d, %I:%M %p')} — {summary}",
                    settings_tab="Knowledge",
                )
            except (ValueError, TypeError):
                return CheckResult("Dream Cycle", "ok", f"Last: {last}", settings_tab="Knowledge")
        return CheckResult("Dream Cycle", "ok", "Enabled — not yet run", settings_tab="Knowledge")
    except Exception as exc:
        return CheckResult("Dream Cycle", "error", str(exc), settings_tab="Knowledge")


def check_tts() -> CheckResult:
    """Check TTS engine status — installed and enabled."""
    try:
        from tts import TTSService
        svc = TTSService()
        if not svc._enabled:
            return CheckResult("TTS", "inactive", "Disabled", settings_tab="Voice")
        if not svc.is_installed():
            return CheckResult("TTS", "warn", "Model not installed", settings_tab="Voice")
        voice = svc._voice or "default"
        return CheckResult("TTS", "ok", f"Kokoro · {voice}", settings_tab="Voice")
    except Exception as exc:
        return CheckResult("TTS", "error", str(exc), settings_tab="Voice")


def check_wiki_vault() -> CheckResult:
    """Check Wiki Vault status — enabled, article count, and sync state."""
    try:
        from wiki_vault import is_enabled, get_vault_stats, check_vault_sync
        if not is_enabled():
            return CheckResult("Wiki Vault", "inactive", "Disabled", settings_tab="Knowledge")
        stats = get_vault_stats()
        articles = stats.get("articles", 0)
        # Check for out-of-sync vault files
        try:
            out_of_sync = check_vault_sync()
        except Exception:
            out_of_sync = []
        if out_of_sync:
            count = len(out_of_sync)
            return CheckResult(
                "Wiki Vault", "warn",
                f"{articles} article{'s' if articles != 1 else ''} · {count} edited in vault",
                settings_tab="Knowledge",
            )
        return CheckResult(
            "Wiki Vault", "ok",
            f"{articles} article{'s' if articles != 1 else ''}",
            settings_tab="Knowledge",
        )
    except Exception as exc:
        return CheckResult("Wiki Vault", "error", str(exc), settings_tab="Knowledge")


def check_document_store() -> CheckResult:
    """Check if the document vector store directory exists."""
    store_dir = _DATA_DIR / "vector_store"
    try:
        if not store_dir.exists():
            return CheckResult("Documents", "ok", "No documents indexed", settings_tab="Documents")
        files = list(store_dir.iterdir())
        if files:
            return CheckResult("Documents", "ok", f"{len(files)} index file{'s' if len(files) != 1 else ''}", settings_tab="Documents")
        return CheckResult("Documents", "ok", "Empty store", settings_tab="Documents")
    except Exception as exc:
        return CheckResult("Documents", "error", str(exc), settings_tab="Documents")


def check_network() -> CheckResult:
    """Quick outbound connectivity check."""
    try:
        import socket as _sock
        s = _sock.create_connection(("1.1.1.1", 53), timeout=2)
        s.close()
        return CheckResult("Network", "ok", "Connected")
    except OSError:
        return CheckResult("Network", "warn", "No internet")
    except Exception as exc:
        return CheckResult("Network", "error", str(exc))


def check_logging() -> CheckResult:
    """Check that file logging is active and today's log file exists."""
    try:
        from logging_config import get_log_stats
        stats = get_log_stats()
        if stats.get("today_file"):
            size = stats.get("today_size_kb", 0)
            return CheckResult(
                "Logging", "ok",
                f"Active — {size:.0f} KB today, {stats['total_files']} file(s)",
                settings_tab="System",
            )
        return CheckResult("Logging", "warn", "No log file for today",
                           settings_tab="System")
    except Exception as exc:
        return CheckResult("Logging", "error", str(exc), settings_tab="System")


def check_tools() -> CheckResult:
    """Check how many tools are enabled."""
    try:
        from tools import registry as _tool_reg
        n_enabled = len(_tool_reg.get_enabled_tools())
        n_total = len(_tool_reg.get_all_tools())
        if n_enabled:
            return CheckResult("Tools", "ok", f"{n_enabled} / {n_total} enabled", settings_tab="Tools")
        return CheckResult("Tools", "error", f"0 / {n_total} enabled", settings_tab="Tools")
    except Exception as exc:
        return CheckResult("Tools", "error", str(exc), settings_tab="Tools")


# ═════════════════════════════════════════════════════════════════════════════
# CHECK REGISTRY
# ═════════════════════════════════════════════════════════════════════════════

# Ordered list of all checks — determines display order in status bar
ALL_CHECKS = [
    check_ollama,
    check_active_model,
    check_cloud_api,
    check_channels,
    check_tunnel,
    check_gmail_oauth,
    check_calendar_oauth,
    check_x_oauth,
    check_task_scheduler,
    check_memory_extraction,
    check_dream_cycle,
    check_tts,
    check_wiki_vault,
    check_logging,
    check_disk_space,
    check_threads_db,
    check_faiss_index,
    check_document_store,
    check_network,
    check_tools,
]

# Lightweight checks (just reading Python booleans — near zero cost)
LIGHT_CHECKS = [
    check_active_model,
    check_channels,
    check_tunnel,
    check_task_scheduler,
    check_tts,
    check_tools,
]

# Heavier checks (I/O, network, OAuth token probing)
HEAVY_CHECKS = [
    check_ollama,
    check_cloud_api,
    check_gmail_oauth,
    check_calendar_oauth,
    check_x_oauth,
    check_memory_extraction,
    check_dream_cycle,
    check_wiki_vault,
    check_logging,
    check_disk_space,
    check_threads_db,
    check_faiss_index,
    check_document_store,
    check_network,
]


def run_all_checks() -> list[CheckResult]:
    """Run every registered check and return results."""
    results = []
    for fn in ALL_CHECKS:
        try:
            result = fn()
            if isinstance(result, list):
                results.extend(result)
            else:
                results.append(result)
        except Exception as exc:
            results.append(CheckResult(fn.__name__, "error", str(exc)))
    return results


def run_light_checks() -> list[CheckResult]:
    """Run only lightweight (instant) checks."""
    results = []
    for fn in LIGHT_CHECKS:
        try:
            result = fn()
            if isinstance(result, list):
                results.extend(result)
            else:
                results.append(result)
        except Exception as exc:
            results.append(CheckResult(fn.__name__, "error", str(exc)))
    return results
