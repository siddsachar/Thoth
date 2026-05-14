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
        from models import _ollama_reachable, get_current_model, is_cloud_model
        if _ollama_reachable(timeout=1.0):
            return CheckResult("Ollama", "ok", "Server reachable", settings_tab="Models")
        model = get_current_model()
        if model and not is_cloud_model(model):
            return CheckResult("Ollama", "warn", "Local model server unreachable", settings_tab="Models")
        return CheckResult("Ollama", "inactive", "Not in use", settings_tab="Models")
    except Exception as exc:
        return CheckResult("Ollama", "warn", str(exc), settings_tab="Models")


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
            return CheckResult("Cloud API", "ok", "Keys configured", settings_tab="Providers")
        return CheckResult("Cloud API", "inactive", "No API keys", settings_tab="Providers")
    except Exception as exc:
        return CheckResult("Cloud API", "error", str(exc), settings_tab="Providers")


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
                               settings_tab="System")
        active = tunnel_manager.active_tunnels()
        if active:
            urls = ", ".join(f"{p}\u2192{u}" for p, u in active.items())
            return CheckResult("Tunnel", "ok",
                               f"{len(active)} active: {urls}",
                               settings_tab="System")
        return CheckResult("Tunnel", "inactive", "Ready (no active tunnels)",
                           settings_tab="System")
    except Exception as exc:
        return CheckResult("Tunnel", "error", str(exc),
                           settings_tab="System")


def check_gmail_oauth() -> CheckResult:
    """Check Gmail OAuth token health."""
    try:
        from tools import registry
        if not registry.is_enabled("gmail"):
            return CheckResult("Gmail OAuth", "inactive", "Tool disabled", settings_tab="Accounts")
        tool = registry.get_tool("gmail")
        if tool is None or not tool.is_authenticated():
            return CheckResult("Gmail OAuth", "inactive", "Not authenticated", settings_tab="Accounts")
        status, detail = tool.check_token_health()
        if status in ("valid", "refreshed"):
            label = "Valid" if status == "valid" else "Refreshed"
            return CheckResult("Gmail OAuth", "ok", label, settings_tab="Accounts")
        if status == "expired":
            return CheckResult("Gmail OAuth", "warn", "Token expired", settings_tab="Accounts")
        return CheckResult("Gmail OAuth", "error", detail, settings_tab="Accounts")
    except Exception as exc:
        return CheckResult("Gmail OAuth", "error", str(exc), settings_tab="Accounts")


def check_calendar_oauth() -> CheckResult:
    """Check Calendar OAuth token health."""
    try:
        from tools import registry
        if not registry.is_enabled("calendar"):
            return CheckResult("Calendar OAuth", "inactive", "Tool disabled", settings_tab="Accounts")
        tool = registry.get_tool("calendar")
        if tool is None or not tool.is_authenticated():
            return CheckResult("Calendar OAuth", "inactive", "Not authenticated", settings_tab="Accounts")
        status, detail = tool.check_token_health()
        if status in ("valid", "refreshed"):
            label = "Valid" if status == "valid" else "Refreshed"
            return CheckResult("Calendar OAuth", "ok", label, settings_tab="Accounts")
        if status == "expired":
            return CheckResult("Calendar OAuth", "warn", "Token expired", settings_tab="Accounts")
        return CheckResult("Calendar OAuth", "error", detail, settings_tab="Accounts")
    except Exception as exc:
        return CheckResult("Calendar OAuth", "error", str(exc), settings_tab="Accounts")


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
    """Check workflow scheduler, active runs, and pending approvals."""
    try:
        from tasks import _scheduler, get_pending_approvals, get_running_tasks
        running = len(get_running_tasks())
        pending = len(get_pending_approvals())
        if _scheduler is None:
            detail = "Scheduler not started"
            if running or pending:
                detail += f" · {running} running · {pending} approval waiting"
            return CheckResult("Workflows", "warn", detail)
        jobs = _scheduler.get_jobs()
        parts = [
            f"{len(jobs)} scheduled",
            f"{running} running",
            f"{pending} approval{'s' if pending != 1 else ''} waiting",
        ]
        return CheckResult("Workflows", "warn" if pending else "ok", " · ".join(parts))
    except Exception as exc:
        return CheckResult("Workflows", "error", str(exc))


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
            return CheckResult("Threads DB", "ok", "No database yet", settings_tab="System")
        conn = sqlite3.connect(str(db_path), timeout=2)
        conn.execute("SELECT 1 FROM thread_meta LIMIT 1")
        count = conn.execute("SELECT COUNT(*) FROM thread_meta").fetchone()[0]
        conn.close()
        return CheckResult("Threads DB", "ok", f"{count} thread{'s' if count != 1 else ''}", settings_tab="System")
    except Exception as exc:
        return CheckResult("Threads DB", "error", str(exc), settings_tab="System")


def check_faiss_index() -> CheckResult:
    """Check if the FAISS memory vector index exists and is readable."""
    vector_dir = _DATA_DIR / "memory_vectors"
    try:
        index_file = vector_dir / "index.faiss"
        map_file = vector_dir / "id_map.json"
        if not vector_dir.exists():
            return CheckResult("FAISS Index", "ok", "Not yet created", settings_tab="Knowledge")
        if not index_file.exists():
            return CheckResult("FAISS Index", "ok", "Empty index", settings_tab="Knowledge")
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
            settings_tab="Knowledge",
        )
    except Exception as exc:
        return CheckResult("FAISS Index", "error", str(exc), settings_tab="Knowledge")


def check_dream_cycle() -> CheckResult:
    """Check Dream Cycle status — enabled, last run time."""
    try:
        from dream_cycle import get_dream_status
        status = get_dream_status()
        if not status.get("enabled"):
            return CheckResult("Dream Cycle", "inactive", "Disabled", settings_tab="Preferences")
        last = status.get("last_run")
        if last:
            try:
                dt = datetime.fromisoformat(last)
                summary = status.get("last_summary", "")
                return CheckResult(
                    "Dream Cycle", "ok",
                    f"{dt.strftime('%b %d, %I:%M %p')} — {summary}",
                    settings_tab="Preferences",
                )
            except (ValueError, TypeError):
                return CheckResult("Dream Cycle", "ok", f"Last: {last}", settings_tab="Preferences")
        return CheckResult("Dream Cycle", "ok", "Enabled — not yet run", settings_tab="Preferences")
    except Exception as exc:
        return CheckResult("Dream Cycle", "error", str(exc), settings_tab="Preferences")


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
    """Check indexed document count using the Settings source of truth."""
    try:
        from documents import document_vector_status, load_processed_files
        processed = load_processed_files()
        doc_count = len(processed)
        vector_status = document_vector_status()
        stale = bool(vector_status.get("stale"))
        exists = bool(vector_status.get("exists"))
        label = f"{doc_count} doc{'s' if doc_count != 1 else ''} indexed"
        if stale:
            return CheckResult("Documents", "warn", f"{label} · rebuild recommended", settings_tab="Documents")
        if doc_count:
            return CheckResult("Documents", "ok", label, settings_tab="Documents")
        if exists:
            return CheckResult("Documents", "ok", "Index ready · no docs", settings_tab="Documents")
        return CheckResult("Documents", "ok", "No documents indexed", settings_tab="Documents")
    except Exception as exc:
        return CheckResult("Documents", "error", str(exc), settings_tab="Documents")


def check_network() -> CheckResult:
    """Quick outbound connectivity check."""
    try:
        import socket as _sock
        s = _sock.create_connection(("1.1.1.1", 53), timeout=2)
        s.close()
        return CheckResult("Network", "ok", "Connected", settings_tab="System")
    except OSError:
        return CheckResult("Network", "warn", "No internet", settings_tab="System")
    except Exception as exc:
        return CheckResult("Network", "error", str(exc), settings_tab="System")


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


def check_search_tools() -> CheckResult:
    """Check search/research tool availability."""
    try:
        from tools import registry as _tool_reg
        search_tools = {
            "web_search", "duckduckgo", "wolfram_alpha", "arxiv",
            "wikipedia", "youtube",
        }
        available = [tool for tool in _tool_reg.get_all_tools() if tool.name in search_tools]
        enabled = [tool for tool in available if _tool_reg.is_enabled(tool.name)]
        total = len(available)
        if not total:
            return CheckResult("Search", "inactive", "No search tools", settings_tab="Search")
        status = "ok" if enabled else "warn"
        return CheckResult("Search", status, f"{len(enabled)} / {total} enabled", settings_tab="Search")
    except Exception as exc:
        return CheckResult("Search", "error", str(exc), settings_tab="Search")


def check_tools() -> CheckResult:
    """Check how many tools are enabled."""
    try:
        from tools import registry as _tool_reg
        n_enabled = len(_tool_reg.get_enabled_tools())
        n_total = len(_tool_reg.get_all_tools())
        if n_enabled:
            return CheckResult("Tools", "ok", f"{n_enabled} / {n_total} enabled", settings_tab="Utilities")
        return CheckResult("Tools", "error", f"0 / {n_total} enabled", settings_tab="Utilities")
    except Exception as exc:
        return CheckResult("Tools", "error", str(exc), settings_tab="Utilities")


def check_skills() -> CheckResult:
    """Check manually configurable skills."""
    try:
        import skills as skills_mod
        if not skills_mod.get_all_skills():
            skills_mod.load_skills()
        manual = skills_mod.get_manual_skills()
        enabled = [sk for sk in manual if skills_mod.is_enabled(sk.name)]
        if not manual:
            return CheckResult("Skills", "inactive", "No manual skills", settings_tab="Skills")
        return CheckResult("Skills", "ok" if enabled else "inactive", f"{len(enabled)} / {len(manual)} enabled", settings_tab="Skills")
    except Exception as exc:
        return CheckResult("Skills", "error", str(exc), settings_tab="Skills")


def check_tracker() -> CheckResult:
    """Check habit/activity tracker state."""
    try:
        from tools import registry as _tool_reg
        enabled = _tool_reg.is_enabled("tracker")
        from tools.tracker_tool import _get_db, _get_all_trackers
        conn = _get_db()
        try:
            trackers = _get_all_trackers(conn)
            total_entries = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        finally:
            conn.close()
        status = "ok" if enabled else "inactive"
        detail = f"{len(trackers)} tracker{'s' if len(trackers) != 1 else ''} · {total_entries} entries"
        if not enabled:
            detail = f"Disabled · {detail}"
        return CheckResult("Tracker", status, detail, settings_tab="Tracker")
    except Exception as exc:
        return CheckResult("Tracker", "error", str(exc), settings_tab="Tracker")


def check_buddy() -> CheckResult:
    """Check Buddy companion configuration."""
    try:
        from buddy.config import get_buddy_config
        cfg = get_buddy_config()
        if not cfg.get("enabled", True):
            return CheckResult("Buddy", "inactive", "Disabled", settings_tab="Buddy")
        mode = str(cfg.get("mode") or "sidebar").replace("_", " ")
        pack = str(cfg.get("pack_id") or "default")
        return CheckResult("Buddy", "ok", f"{mode} · {pack}", settings_tab="Buddy")
    except Exception as exc:
        return CheckResult("Buddy", "error", str(exc), settings_tab="Buddy")


def check_mcp() -> CheckResult:
    """Check external MCP server/tool status."""
    try:
        from mcp_client.runtime import get_status_summary
        status = get_status_summary()
        if not status.get("sdk_available"):
            return CheckResult("MCP", "warn", "SDK missing", settings_tab="MCP")
        server_count = int(status.get("server_count") or 0)
        enabled_servers = int(status.get("enabled_server_count") or 0)
        connected = int(status.get("connected_server_count") or 0)
        enabled_tools = int(status.get("enabled_tool_count") or 0)
        if not status.get("enabled"):
            return CheckResult("MCP", "inactive", f"Disabled · {server_count} servers", settings_tab="MCP")
        if enabled_servers and connected < enabled_servers:
            return CheckResult("MCP", "warn", f"{connected}/{enabled_servers} connected · {enabled_tools} tools", settings_tab="MCP")
        detail = f"{connected} connected · {enabled_tools} tools"
        if not server_count:
            detail = "No servers configured"
        return CheckResult("MCP", "ok", detail, settings_tab="MCP")
    except Exception as exc:
        return CheckResult("MCP", "error", str(exc), settings_tab="MCP")


def check_plugins() -> CheckResult:
    """Check installed plugin load state."""
    try:
        from plugins import loader as plugin_loader
        from plugins import registry as plugin_registry
        from plugins import state as plugin_state
        summary = plugin_loader.get_load_summary()
        manifests = plugin_registry.get_loaded_manifests()
        total = len(manifests) or int(summary.get("total") or 0)
        enabled = sum(1 for manifest in manifests if plugin_state.is_plugin_enabled(manifest.id))
        failed = int(summary.get("failed") or 0)
        if failed:
            return CheckResult("Plugins", "warn", f"{enabled} enabled · {failed} failed", settings_tab="Plugins")
        if not total:
            return CheckResult("Plugins", "inactive", "No plugins installed", settings_tab="Plugins")
        return CheckResult("Plugins", "ok", f"{enabled} / {total} enabled", settings_tab="Plugins")
    except Exception as exc:
        return CheckResult("Plugins", "error", str(exc), settings_tab="Plugins")


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
    check_search_tools,
    check_skills,
    check_tracker,
    check_buddy,
    check_mcp,
    check_plugins,
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
    check_search_tools,
    check_buddy,
    check_mcp,
    check_plugins,
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
    check_skills,
    check_tracker,
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
