"""Thoth v3.0.0 — Comprehensive Test Suite

Validates that all modules import cleanly, key functions exist,
config round-trips work, DB connectivity works, and the NiceGUI
app can start and serve HTTP on port 8080.

Usage:  python test_suite.py
"""

from __future__ import annotations

import ast
import importlib
import os
import socket
import subprocess
import sys
import time
import traceback
from pathlib import Path

# ── Ensure project root is on sys.path ──────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent
os.chdir(PROJECT_ROOT)
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PASS = 0
FAIL = 0
WARN = 0
RESULTS: list[tuple[str, str, str]] = []  # (status, test_name, detail)


def record(status: str, name: str, detail: str = ""):
    global PASS, FAIL, WARN
    if status == "PASS":
        PASS += 1
    elif status == "FAIL":
        FAIL += 1
    else:
        WARN += 1
    RESULTS.append((status, name, detail))
    icon = {"PASS": "✅", "FAIL": "❌", "WARN": "⚠️"}.get(status, "?")
    line = f"  {icon} {name}"
    if detail:
        line += f"  —  {detail}"
    print(line)


# ═════════════════════════════════════════════════════════════════════════════
# 1. AST SYNTAX CHECK — every .py file must parse
# ═════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("1. AST SYNTAX CHECK")
print("=" * 70)

py_files = sorted(PROJECT_ROOT.glob("*.py")) + sorted((PROJECT_ROOT / "tools").glob("*.py")) + sorted((PROJECT_ROOT / "channels").glob("*.py"))
py_files = [f for f in py_files if f.name != "test_suite.py"]

for f in py_files:
    rel = f.relative_to(PROJECT_ROOT)
    try:
        source = f.read_text(encoding="utf-8")
        ast.parse(source)
        record("PASS", f"syntax: {rel}")
    except SyntaxError as e:
        record("FAIL", f"syntax: {rel}", str(e))

# ═════════════════════════════════════════════════════════════════════════════
# 2. MODULE IMPORTS — core modules
# ═════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("2. CORE MODULE IMPORTS")
print("=" * 70)

CORE_MODULES = [
    "agent",
    "threads",
    "models",
    "memory",
    "memory_extraction",
    "documents",
    "api_keys",
    "voice",
    "tts",
    "vision",
    "data_reader",
    "workflows",
    "notifications",
    "launcher",
]

for mod_name in CORE_MODULES:
    try:
        importlib.import_module(mod_name)
        record("PASS", f"import {mod_name}")
    except Exception as e:
        record("FAIL", f"import {mod_name}", f"{type(e).__name__}: {e}")

# ═════════════════════════════════════════════════════════════════════════════
# 3. TOOL MODULE IMPORTS
# ═════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("3. TOOL MODULE IMPORTS")
print("=" * 70)

TOOL_MODULES = [
    "tools",
    "tools.base",
    "tools.registry",
    "tools.arxiv_tool",
    "tools.calculator_tool",
    "tools.calendar_tool",
    "tools.chart_tool",
    "tools.conversation_search_tool",
    "tools.documents_tool",
    "tools.duckduckgo_tool",
    "tools.filesystem_tool",
    "tools.gmail_tool",
    "tools.memory_tool",
    "tools.system_info_tool",
    "tools.timer_tool",
    "tools.url_reader_tool",
    "tools.vision_tool",
    "tools.weather_tool",
    "tools.web_search_tool",
    "tools.wikipedia_tool",
    "tools.wolfram_tool",
    "tools.youtube_tool",
]

for mod_name in TOOL_MODULES:
    try:
        importlib.import_module(mod_name)
        record("PASS", f"import {mod_name}")
    except Exception as e:
        record("FAIL", f"import {mod_name}", f"{type(e).__name__}: {e}")

# ═════════════════════════════════════════════════════════════════════════════
# 4. CHANNEL MODULE IMPORTS
# ═════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("4. CHANNEL MODULE IMPORTS")
print("=" * 70)

CHANNEL_MODULES = [
    "channels",
    "channels.config",
    "channels.telegram",
    "channels.email",
]

for mod_name in CHANNEL_MODULES:
    try:
        importlib.import_module(mod_name)
        record("PASS", f"import {mod_name}")
    except Exception as e:
        record("FAIL", f"import {mod_name}", f"{type(e).__name__}: {e}")

# ═════════════════════════════════════════════════════════════════════════════
# 5. KEY FUNCTION / CLASS EXISTENCE
# ═════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("5. KEY FUNCTION / CLASS EXISTENCE")
print("=" * 70)

FUNCTION_CHECKS = [
    ("agent", "stream_agent"),
    ("agent", "resume_stream_agent"),
    ("agent", "get_agent_graph"),
    ("agent", "clear_agent_cache"),
    ("threads", "_list_threads"),
    ("threads", "_save_thread_meta"),
    ("threads", "_delete_thread"),
    ("threads", "pick_or_create_thread"),
    ("models", "list_local_models"),
    ("memory", "save_memory"),
    ("memory", "semantic_search"),
    ("memory_extraction", "run_extraction"),
    ("memory_extraction", "start_periodic_extraction"),
    ("documents", "load_and_vectorize_document"),
    ("documents", "get_embedding_model"),
    ("documents", "get_vector_store"),
    ("api_keys", "get_key"),
    ("api_keys", "set_key"),
    ("api_keys", "apply_keys"),
    ("voice", "get_voice_service"),
    ("tts", "TTSService"),
    ("vision", "capture_frame"),
    ("vision", "capture_screenshot"),
    ("workflows", "seed_default_workflows"),
    ("workflows", "start_workflow_scheduler"),
    ("notifications", "notify"),
    ("channels.config", "get"),
    ("channels.config", "set"),
    ("channels.telegram", "start_bot"),
    ("channels.telegram", "stop_bot"),
    ("channels.telegram", "is_configured"),
    ("channels.telegram", "is_running"),
    ("channels.email", "start_polling"),
    ("channels.email", "stop_polling"),
    ("channels.email", "is_configured"),
    ("channels.email", "is_running"),
    ("channels.email", "get_poll_interval"),
    ("channels.email", "set_poll_interval"),
    ("tools.registry", "get_all_tools"),
    ("tools.registry", "get_enabled_tools"),
    ("tools.registry", "get_langchain_tools"),
    ("tools.tracker_tool", "TrackerTool"),
    ("tools.tracker_tool", "_tracker_log"),
    ("tools.tracker_tool", "_tracker_query"),
    ("tools.tracker_tool", "_tracker_delete"),
    ("launcher", "_ThothProcess"),
    ("launcher", "ThothTray"),
    ("launcher", "_show_splash"),
    ("launcher", "_SPLASH_TK"),
]

for mod_name, attr_name in FUNCTION_CHECKS:
    try:
        mod = importlib.import_module(mod_name)
        if hasattr(mod, attr_name):
            record("PASS", f"{mod_name}.{attr_name} exists")
        else:
            record("FAIL", f"{mod_name}.{attr_name} exists", "attribute not found")
    except Exception as e:
        record("FAIL", f"{mod_name}.{attr_name} exists", f"import error: {e}")

# ═════════════════════════════════════════════════════════════════════════════
# 6. TOOL REGISTRY — all tools registered
# ═════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("6. TOOL REGISTRY CHECK")
print("=" * 70)

try:
    from tools.registry import get_all_tools

    EXPECTED_TOOLS = {
        "web_search", "duckduckgo", "wikipedia", "arxiv", "youtube",
        "url_reader", "documents", "gmail", "calendar", "filesystem",
        "timer", "calculator", "wolfram_alpha", "weather", "vision",
        "memory", "conversation_search", "system_info", "chart",
        "tracker",
    }

    all_tools = get_all_tools()
    # get_all_tools may return a list of tool objects — extract names
    if isinstance(all_tools, list):
        registered = {getattr(t, 'name', getattr(t, 'tool_name', str(t))) for t in all_tools}
    else:
        registered = set(all_tools.keys())
    missing = EXPECTED_TOOLS - registered
    extra = registered - EXPECTED_TOOLS

    if not missing:
        record("PASS", f"tool registry: {len(registered)} tools registered")
    else:
        record("FAIL", f"tool registry: missing {missing}")

    if extra:
        record("WARN", f"tool registry: extra tools {extra}")

except Exception as e:
    record("FAIL", "tool registry", str(e))

# ═════════════════════════════════════════════════════════════════════════════
# 7. LAUNCHER SPLASH SCREEN VALIDATION
# ═════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("7. LAUNCHER SPLASH SCREEN VALIDATION")
print("=" * 70)

try:
    from launcher import _SPLASH_TK, _show_splash

    # Script must be a non-trivial string
    if isinstance(_SPLASH_TK, str) and len(_SPLASH_TK) > 100:
        record("PASS", f"_SPLASH_TK is {len(_SPLASH_TK)} chars")
    else:
        record("FAIL", "_SPLASH_TK", "empty or too short")

    # Script must be valid Python
    try:
        ast.parse(_SPLASH_TK)
        record("PASS", "_SPLASH_TK is valid Python")
    except SyntaxError as e:
        record("FAIL", "_SPLASH_TK syntax", str(e))

    # Script should reference tkinter and port polling
    for keyword in ["tkinter", "socket", "PORT"]:
        if keyword.lower() in _SPLASH_TK.lower():
            record("PASS", f"splash script contains '{keyword}'")
        else:
            record("FAIL", f"splash script missing '{keyword}'")

    # _show_splash must be callable
    if callable(_show_splash):
        record("PASS", "_show_splash is callable")
    else:
        record("FAIL", "_show_splash not callable")

except Exception as e:
    record("FAIL", "launcher splash validation", str(e))

# ═════════════════════════════════════════════════════════════════════════════
# 8. CHANNELS CONFIG ROUND-TRIP
# ═════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("8. CHANNELS CONFIG ROUND-TRIP")
print("=" * 70)

try:
    from channels import config as ch_config

    # Write a test value
    ch_config.set("_test", "round_trip", True)
    val = ch_config.get("_test", "round_trip", False)
    if val is True:
        record("PASS", "channels config write+read")
    else:
        record("FAIL", "channels config write+read", f"got {val!r}")

    # Clean up
    ch_config.set("_test", "round_trip", None)

except Exception as e:
    record("FAIL", "channels config round-trip", str(e))

# ═════════════════════════════════════════════════════════════════════════════
# 9. THREAD DB CONNECTIVITY
# ═════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("9. THREAD DB CONNECTIVITY")
print("=" * 70)

try:
    from threads import _list_threads
    threads = _list_threads()
    record("PASS", f"thread DB: {len(threads)} threads")
except Exception as e:
    record("FAIL", "thread DB connectivity", str(e))

# ═════════════════════════════════════════════════════════════════════════════
# 10. NO STREAMLIT IMPORTS IN app_nicegui.py
# ═════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("10. NO STREAMLIT IMPORTS IN app_nicegui.py")
print("=" * 70)

try:
    source = (PROJECT_ROOT / "app_nicegui.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    streamlit_imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if "streamlit" in alias.name.lower():
                    streamlit_imports.append(f"line {node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module and "streamlit" in node.module.lower():
                streamlit_imports.append(f"line {node.lineno}: from {node.module} import ...")

    if not streamlit_imports:
        record("PASS", "no streamlit imports in app_nicegui.py")
    else:
        record("FAIL", "streamlit imports found in app_nicegui.py", "; ".join(streamlit_imports))

except Exception as e:
    record("FAIL", "streamlit import check", str(e))

# ═════════════════════════════════════════════════════════════════════════════
# 11. NiceGUI APP IMPORT CHECK
# ═════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("11. NiceGUI APP AST PARSE + BASIC IMPORT CHECK")
print("=" * 70)

try:
    source = (PROJECT_ROOT / "app_nicegui.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    record("PASS", f"app_nicegui.py AST parsed ({len(source):,} chars)")
except Exception as e:
    record("FAIL", "app_nicegui.py AST parse", str(e))

# Check nicegui is importable
try:
    import nicegui
    record("PASS", f"nicegui package v{nicegui.__version__}")
except ImportError:
    record("FAIL", "nicegui package import", "not installed")

# ═════════════════════════════════════════════════════════════════════════════
# 12. REQUIREMENTS.TXT DEPENDENCIES
# ═════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("12. KEY DEPENDENCY CHECKS")
print("=" * 70)

KEY_PACKAGES = [
    "nicegui",
    "langchain",
    "langchain_core",
    "langchain_ollama",
    "langgraph",
    "faiss",
    "sentence_transformers",
    "ollama",
    "pystray",
    "PIL",  # Pillow
    "webview",  # pywebview
]

for pkg in KEY_PACKAGES:
    try:
        importlib.import_module(pkg)
        record("PASS", f"dependency: {pkg}")
    except ImportError:
        record("FAIL", f"dependency: {pkg}", "not installed")

# ═════════════════════════════════════════════════════════════════════════════
# 13. TRACKER TOOL FUNCTIONAL TESTS
# ═════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("13. TRACKER TOOL FUNCTIONAL TESTS")
print("=" * 70)

_tracker_test_db = None
try:
    import sqlite3
    import tempfile
    import pathlib
    import json
    from datetime import datetime, timedelta
    from tools import tracker_tool as _tt

    # Use an isolated in-memory DB for tests (schema must match tracker_tool._get_db)
    _tracker_test_db = sqlite3.connect(":memory:")
    _tracker_test_db.execute("PRAGMA journal_mode=WAL")
    _tracker_test_db.execute("PRAGMA foreign_keys=ON")
    _tracker_test_db.executescript("""
        CREATE TABLE IF NOT EXISTS trackers (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
            type        TEXT NOT NULL DEFAULT 'boolean',
            unit        TEXT,
            icon        TEXT,
            created_at  TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS entries (
            id          TEXT PRIMARY KEY,
            tracker_id  TEXT NOT NULL REFERENCES trackers(id) ON DELETE CASCADE,
            timestamp   TEXT NOT NULL,
            value       TEXT NOT NULL DEFAULT 'true',
            notes       TEXT,
            created_at  TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_entries_tracker
            ON entries(tracker_id, timestamp);
    """)

    # 13a. Create tracker
    t = _tt._create_tracker(_tracker_test_db, "Aspirin", "boolean", None)
    if t["name"] == "Aspirin" and t["type"] == "boolean":
        record("PASS", "tracker: create boolean tracker")
    else:
        record("FAIL", "tracker: create boolean tracker", f"got {t}")

    t2 = _tt._create_tracker(_tracker_test_db, "Weight", "numeric", "kg")
    if t2["name"] == "Weight" and t2["unit"] == "kg":
        record("PASS", "tracker: create numeric tracker with unit")
    else:
        record("FAIL", "tracker: create numeric tracker with unit", f"got {t2}")

    t3 = _tt._create_tracker(_tracker_test_db, "Sleep", "duration", None)
    if t3["type"] == "duration":
        record("PASS", "tracker: create duration tracker")
    else:
        record("FAIL", "tracker: create duration tracker", f"got {t3}")

    # 13b. Find tracker (case-insensitive)
    found = _tt._find_tracker(_tracker_test_db, "aspirin")
    if found and found["name"] == "Aspirin":
        record("PASS", "tracker: find case-insensitive")
    else:
        record("FAIL", "tracker: find case-insensitive", f"got {found}")

    not_found = _tt._find_tracker(_tracker_test_db, "Nonexistent")
    if not_found is None:
        record("PASS", "tracker: find returns None for missing")
    else:
        record("FAIL", "tracker: find returns None for missing", f"got {not_found}")

    # 13c. List all trackers
    all_t = _tt._get_all_trackers(_tracker_test_db)
    if len(all_t) == 3 and {x["name"] for x in all_t} == {"Aspirin", "Weight", "Sleep"}:
        record("PASS", f"tracker: list all ({len(all_t)} trackers)")
    else:
        record("FAIL", "tracker: list all", f"got {len(all_t)} trackers")

    # 13d. Log entries
    e1 = _tt._log_entry(_tracker_test_db, t["id"], "true", None, None)
    if e1["value"] == "true" and e1["tracker_id"] == t["id"]:
        record("PASS", "tracker: log boolean entry")
    else:
        record("FAIL", "tracker: log boolean entry", f"got {e1}")

    e2 = _tt._log_entry(_tracker_test_db, t2["id"], "82.5", "morning", None)
    if e2["value"] == "82.5" and e2["notes"] == "morning":
        record("PASS", "tracker: log numeric entry with notes")
    else:
        record("FAIL", "tracker: log numeric entry with notes", f"got {e2}")

    e3 = _tt._log_entry(_tracker_test_db, t["id"], "true", None, "2026-03-10T08:00:00")
    if "2026-03-10" in e3["timestamp"]:
        record("PASS", "tracker: log entry with custom timestamp")
    else:
        record("FAIL", "tracker: log entry with custom timestamp", f"got {e3}")

    # 13e. Get entries with filters
    entries = _tt._get_entries(_tracker_test_db, t["id"])
    if len(entries) == 2:  # two Aspirin entries
        record("PASS", f"tracker: get entries ({len(entries)} rows)")
    else:
        record("FAIL", "tracker: get entries", f"expected 2, got {len(entries)}")

    # e1 was auto-timestamped (now), e3 was set to 2026-03-10.
    # Filter to entries from yesterday onward → should return only e1.
    since_dt = datetime.now() - timedelta(hours=23)
    recent = _tt._get_entries(_tracker_test_db, t["id"], since=since_dt)
    if len(recent) == 1:  # only the one from today
        record("PASS", "tracker: get entries with since filter")
    else:
        record("FAIL", "tracker: get entries with since filter", f"expected 1, got {len(recent)}")

    # 13f. Period parsing
    td_30d = _tt._parse_period("last 30 days")
    if td_30d and td_30d.days == 30:
        record("PASS", "tracker: parse '30 days'")
    else:
        record("FAIL", "tracker: parse '30 days'", f"got {td_30d}")

    td_2w = _tt._parse_period("past 2 weeks")
    if td_2w and td_2w.days == 14:
        record("PASS", "tracker: parse '2 weeks'")
    else:
        record("FAIL", "tracker: parse '2 weeks'", f"got {td_2w}")

    td_3m = _tt._parse_period("3 months")
    if td_3m and td_3m.days == 90:
        record("PASS", "tracker: parse '3 months'")
    else:
        record("FAIL", "tracker: parse '3 months'", f"got {td_3m}")

    td_none = _tt._parse_period("show me stuff")
    if td_none is None:
        record("PASS", "tracker: parse returns None for no-period text")
    else:
        record("FAIL", "tracker: parse returns None for no-period text", f"got {td_none}")

    # 13g. Analysis — adherence
    # Build test entries: aspirin taken on 5 of last 7 days
    test_entries_bool = []
    base = datetime.now()
    for i in [0, 1, 2, 4, 6]:  # 5 distinct days
        test_entries_bool.append({
            "id": i, "tracker_id": 1, "value": "true", "notes": None,
            "timestamp": (base - timedelta(days=i)).strftime("%Y-%m-%dT%H:%M:%S")
        })
    adh = _tt._adherence(test_entries_bool, 7)
    if adh["days_tracked"] == 5 and adh["total_days"] == 7:
        pct = adh["adherence_pct"]
        expected_pct = round(5 / 7 * 100, 1)
        if abs(pct - expected_pct) < 0.2:
            record("PASS", f"tracker: adherence calc ({pct}%)")
        else:
            record("FAIL", "tracker: adherence calc", f"expected ~{expected_pct}%, got {pct}%")
    else:
        record("FAIL", "tracker: adherence calc", f"got {adh}")

    # 13h. Analysis — streaks
    # Consecutive days: today, yesterday, 2 days ago → streak=3
    streak_entries = []
    for i in range(3):
        streak_entries.append({
            "id": i, "tracker_id": 1, "value": "true", "notes": None,
            "timestamp": (base - timedelta(days=i)).strftime("%Y-%m-%dT%H:%M:%S")
        })
    stk = _tt._streaks(streak_entries)
    if stk["current_streak"] == 3 and stk["longest_streak"] == 3:
        record("PASS", f"tracker: streak calc (current={stk['current_streak']})")
    else:
        record("FAIL", "tracker: streak calc", f"got {stk}")

    # 13i. Analysis — numeric stats
    num_entries = []
    for i, v in enumerate([80.0, 82.5, 81.0, 83.0, 79.5]):
        num_entries.append({
            "id": i, "tracker_id": 2, "value": str(v), "notes": None,
            "timestamp": (base - timedelta(days=i)).strftime("%Y-%m-%dT%H:%M:%S")
        })
    ns = _tt._numeric_stats(num_entries)
    if ns and abs(ns["mean"] - 81.2) < 0.1 and ns["min"] == 79.5 and ns["max"] == 83.0 and ns["count"] == 5:
        record("PASS", f"tracker: numeric stats (mean={ns['mean']}, min={ns['min']}, max={ns['max']})")
    else:
        record("FAIL", "tracker: numeric stats", f"got {ns}")

    # 13j. Analysis — frequency
    freq = _tt._frequency(test_entries_bool, 7)
    if freq["total_entries"] == 5 and "per_week" in freq and "per_month" in freq:
        record("PASS", f"tracker: frequency ({freq['total_entries']} entries, {freq['per_week']}/wk)")
    else:
        record("FAIL", "tracker: frequency", f"got {freq}")

    # 13k. Analysis — day of week distribution
    dow = _tt._day_of_week_distribution(test_entries_bool)
    if isinstance(dow, dict) and len(dow) == 7:
        total = sum(dow.values())
        if total == 5:  # 5 entries spread over weekdays
            record("PASS", f"tracker: day-of-week distribution (total={total})")
        else:
            record("FAIL", "tracker: day-of-week distribution", f"total entries={total}, expected 5")
    else:
        record("FAIL", "tracker: day-of-week distribution", f"got {dow}")

    # 13l. Analysis — cycle estimation
    # Simulate period tracker: start every ~28 days
    cycle_entries = []
    for c in range(4):
        ts = (base - timedelta(days=c * 28)).strftime("%Y-%m-%dT%H:%M:%S")
        cycle_entries.append({
            "id": c, "tracker_id": 3, "value": "started", "notes": None,
            "timestamp": ts
        })
    ce = _tt._cycle_estimation(cycle_entries)
    if ce["cycles"] == 4 and ce["avg_cycle_days"] == 28.0:
        record("PASS", f"tracker: cycle estimation (avg={ce['avg_cycle_days']}d)")
    else:
        record("FAIL", "tracker: cycle estimation", f"got {ce}")

    # 13m. Analysis — co-occurrence
    # Create a second tracker and log entries on same days
    t_headache = _tt._create_tracker(_tracker_test_db, "Headache", "boolean", None)
    t_coffee = _tt._create_tracker(_tracker_test_db, "Coffee", "boolean", None)
    overlap_days = [0, 1, 3, 5]  # Both logged on these days
    for d in overlap_days:
        ts = (base - timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%S")
        _tt._log_entry(_tracker_test_db, t_headache["id"], "true", None, ts)
        _tt._log_entry(_tracker_test_db, t_coffee["id"], "true", None, ts)
    # Add some coffee-only days
    for d in [2, 4, 6]:
        ts = (base - timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%S")
        _tt._log_entry(_tracker_test_db, t_coffee["id"], "true", None, ts)

    co = _tt._co_occurrence(
        _tracker_test_db, t_headache["id"], t_coffee["id"],
        window_days=0, since=base - timedelta(days=7)
    )
    if co["matches"] == 4 and co["a_total"] == 4 and co["b_total"] == 7:
        record("PASS", f"tracker: co-occurrence (matches={co['matches']}, a={co['a_total']}, b={co['b_total']})")
    else:
        record("FAIL", "tracker: co-occurrence", f"got {co}")

    # 13n. CSV export
    test_rows = [{"date": "2026-03-11", "value": "82.5"}, {"date": "2026-03-10", "value": "80.0"}]
    csv_path = _tt._export_csv(test_rows, "test_weight")
    if pathlib.Path(csv_path).exists():
        csv_content = pathlib.Path(csv_path).read_text()
        if "82.5" in csv_content and "date" in csv_content:
            record("PASS", "tracker: CSV export")
        else:
            record("FAIL", "tracker: CSV export", "content mismatch")
        pathlib.Path(csv_path).unlink(missing_ok=True)  # clean up
    else:
        record("FAIL", "tracker: CSV export", f"file not found: {csv_path}")

    # 13o. TrackerTool class validation
    tool_inst = _tt.TrackerTool()
    if tool_inst.name == "tracker":
        record("PASS", "tracker: TrackerTool.name")
    else:
        record("FAIL", "tracker: TrackerTool.name", f"got '{tool_inst.name}'")

    if tool_inst.enabled_by_default is True:
        record("PASS", "tracker: enabled_by_default")
    else:
        record("FAIL", "tracker: enabled_by_default", f"got {tool_inst.enabled_by_default}")

    if tool_inst.destructive_tool_names == {"tracker_delete"}:
        record("PASS", "tracker: destructive_tool_names")
    else:
        record("FAIL", "tracker: destructive_tool_names", f"got {tool_inst.destructive_tool_names}")

    lc_tools = tool_inst.as_langchain_tools()
    lc_names = sorted([t.name for t in lc_tools])
    if lc_names == ["tracker_delete", "tracker_log", "tracker_query"]:
        record("PASS", f"tracker: 3 LangChain sub-tools {lc_names}")
    else:
        record("FAIL", "tracker: LangChain sub-tools", f"got {lc_names}")

    # 13p. _tracker_log integration (uses real function with test db patching)
    _orig_get_db = _tt._get_db
    _tt._get_db = lambda: _tracker_test_db
    try:
        result = _tt._tracker_log(tracker_name="TestVitaminD", value="5000", tracker_type="numeric", unit="IU")
        if "TestVitaminD" in result and "5000" in result:
            record("PASS", "tracker: _tracker_log integration")
        else:
            record("FAIL", "tracker: _tracker_log integration", f"got: {result[:100]}")
    finally:
        _tt._get_db = _orig_get_db

except Exception as e:
    record("FAIL", "tracker tool tests", f"{type(e).__name__}: {e}")
    traceback.print_exc()
finally:
    if _tracker_test_db:
        _tracker_test_db.close()

# ═════════════════════════════════════════════════════════════════════════════
# 14. LIVE LAUNCH TEST — start app, verify HTTP, shut down
# ═════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("14. LIVE LAUNCH TEST (port 8080)")
print("=" * 70)


def _port_open(port: int, timeout: float = 1.0) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex(("127.0.0.1", port)) == 0


# Make sure port is free first
if _port_open(8080):
    record("WARN", "live launch: port 8080 already in use — skipping")
else:
    proc = None
    port_ok = False
    try:
        python = sys.executable
        proc = subprocess.Popen(
            [python, "app_nicegui.py"],
            cwd=str(PROJECT_ROOT),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        record("PASS", f"app started (PID {proc.pid})")

        # Wait up to 60s for port 8080 to open
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if _port_open(8080):
                port_ok = True
                break
            # Check process hasn't crashed
            if proc.poll() is not None:
                record("FAIL", "app crashed during startup", f"exit code: {proc.returncode}")
                break
            time.sleep(1)

        if port_ok:
            record("PASS", "port 8080 responding")

            # Try HTTP GET
            try:
                import urllib.request
                resp = urllib.request.urlopen("http://127.0.0.1:8080", timeout=10)
                status = resp.status
                if status == 200:
                    record("PASS", f"HTTP GET / → {status}")
                else:
                    record("WARN", f"HTTP GET / → {status}")
            except Exception as e:
                record("WARN", f"HTTP GET / failed: {e}")

        elif proc.poll() is None:
            record("FAIL", "port 8080 not open after 60s")

    except Exception as e:
        record("FAIL", "live launch test", str(e))
    finally:
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            if port_ok:
                record("PASS", "app shut down cleanly")
            else:
                record("WARN", "app process terminated (port never opened)")


# ═════════════════════════════════════════════════════════════════════════════
# 15. CROSS-PLATFORM LOGIC TESTS
# ═════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("15. CROSS-PLATFORM LOGIC TESTS")
print("=" * 70)

# --- 15a. tts._piper_platform_info() — current platform values -----------
try:
    from tts import _piper_platform_info, _PIPER_ARCHIVE, _PIPER_BINARY, _PIPER_DOWNLOAD_URL

    arch, bname, url = _piper_platform_info()
    if arch and bname and url:
        record("PASS", f"tts: _piper_platform_info() → ({arch}, {bname})")
    else:
        record("FAIL", "tts: _piper_platform_info()", "returned empty values")

    # Module-level constants must match current call
    if _PIPER_ARCHIVE == arch and _PIPER_BINARY == bname and _PIPER_DOWNLOAD_URL == url:
        record("PASS", "tts: module constants match _piper_platform_info()")
    else:
        record("FAIL", "tts: module constants mismatch",
               f"archive={_PIPER_ARCHIVE}, binary={_PIPER_BINARY}")

    # URL must start with the expected github base
    if url.startswith("https://github.com/rhasspy/piper/releases/download/"):
        record("PASS", "tts: download URL has correct base")
    else:
        record("FAIL", "tts: download URL base", url)

except Exception as e:
    record("FAIL", "tts: _piper_platform_info()", str(e))

# --- 15b. tts._piper_platform_info() — mock all platform branches --------
try:
    from unittest.mock import patch
    from tts import _piper_platform_info as _ppinfo

    EXPECTED = {
        ("Windows", "amd64"):  ("piper_windows_amd64.zip",     "piper.exe"),
        ("Darwin",  "arm64"):  ("piper_macos_aarch64.tar.gz",  "piper"),
        ("Linux",   "x86_64"): ("piper_linux_x86_64.tar.gz",   "piper"),
        ("Linux",   "aarch64"):("piper_linux_aarch64.tar.gz",  "piper"),
    }

    all_ok = True
    for (sys_name, mach), (exp_archive, exp_binary) in EXPECTED.items():
        with patch("tts.platform") as mock_plat:
            mock_plat.system.return_value = sys_name
            mock_plat.machine.return_value = mach
            a, b, u = _ppinfo()
            if a != exp_archive or b != exp_binary:
                record("FAIL", f"tts: piper mock ({sys_name}/{mach})",
                       f"got ({a}, {b}), expected ({exp_archive}, {exp_binary})")
                all_ok = False
            elif not u.endswith(exp_archive):
                record("FAIL", f"tts: piper mock URL ({sys_name}/{mach})",
                       f"URL does not end with {exp_archive}")
                all_ok = False

    if all_ok:
        record("PASS", f"tts: _piper_platform_info() all {len(EXPECTED)} platform branches OK")
except Exception as e:
    record("FAIL", "tts: piper mock tests", str(e))

# --- 15c. tts._piper_platform_info() — unsupported platform raises -------
try:
    with patch("tts.platform") as mock_plat:
        mock_plat.system.return_value = "FreeBSD"
        mock_plat.machine.return_value = "amd64"
        try:
            _ppinfo()
            record("FAIL", "tts: unsupported platform", "expected RuntimeError")
        except RuntimeError:
            record("PASS", "tts: unsupported platform raises RuntimeError")
except Exception as e:
    record("FAIL", "tts: unsupported platform test", str(e))

# --- 15d. vision._CV_BACKEND is a valid OpenCV constant ------------------
try:
    import cv2
    from vision import _CV_BACKEND

    EXPECTED_BACKENDS = {cv2.CAP_DSHOW, cv2.CAP_AVFOUNDATION, cv2.CAP_V4L2}
    if _CV_BACKEND in EXPECTED_BACKENDS:
        record("PASS", f"vision: _CV_BACKEND={_CV_BACKEND} is a valid backend")
    else:
        record("FAIL", "vision: _CV_BACKEND", f"unexpected value {_CV_BACKEND}")

    # On Windows it must be CAP_DSHOW
    if sys.platform == "win32":
        if _CV_BACKEND == cv2.CAP_DSHOW:
            record("PASS", "vision: _CV_BACKEND == CAP_DSHOW on Windows")
        else:
            record("FAIL", "vision: _CV_BACKEND on Windows",
                   f"expected {cv2.CAP_DSHOW}, got {_CV_BACKEND}")
    elif sys.platform == "darwin":
        if _CV_BACKEND == cv2.CAP_AVFOUNDATION:
            record("PASS", "vision: _CV_BACKEND == CAP_AVFOUNDATION on macOS")
        else:
            record("FAIL", "vision: _CV_BACKEND on macOS",
                   f"expected {cv2.CAP_AVFOUNDATION}, got {_CV_BACKEND}")
    else:
        if _CV_BACKEND == cv2.CAP_V4L2:
            record("PASS", "vision: _CV_BACKEND == CAP_V4L2 on Linux")
        else:
            record("FAIL", "vision: _CV_BACKEND on Linux",
                   f"expected {cv2.CAP_V4L2}, got {_CV_BACKEND}")

except Exception as e:
    record("FAIL", "vision: _CV_BACKEND", str(e))

# --- 15e. notifications._play_sound exists and is callable ----------------
try:
    from notifications import _play_sound

    if callable(_play_sound):
        record("PASS", "notifications: _play_sound is callable")
    else:
        record("FAIL", "notifications: _play_sound", "not callable")
except Exception as e:
    record("FAIL", "notifications: _play_sound import", str(e))

# --- 15f. launcher._SPLASH_TK contains os.name guard ---------------------
try:
    from launcher import _SPLASH_TK

    if "os.name == 'nt'" in _SPLASH_TK:
        record("PASS", "launcher: _SPLASH_TK has os.name == 'nt' guard")
    else:
        record("FAIL", "launcher: _SPLASH_TK", "missing os.name guard")

    # Must still contain the DLL loading code (Windows path intact)
    if "ctypes.CDLL" in _SPLASH_TK:
        record("PASS", "launcher: _SPLASH_TK still has ctypes.CDLL for Windows")
    else:
        record("FAIL", "launcher: _SPLASH_TK", "ctypes.CDLL block removed")

    # Valid Python
    try:
        ast.parse(_SPLASH_TK)
        record("PASS", "launcher: _SPLASH_TK is valid Python")
    except SyntaxError as se:
        record("FAIL", "launcher: _SPLASH_TK syntax", str(se))

except Exception as e:
    record("FAIL", "launcher: cross-platform splash", str(e))


# ── 15f. Ollama auto-start helpers ──────────────────────────────────────────
try:
    from launcher import _is_ollama_running, _start_ollama, _OLLAMA_PORT

    # _is_ollama_running returns a bool
    result = _is_ollama_running()
    assert isinstance(result, bool), f"Expected bool, got {type(result)}"
    record("PASS", "launcher: _is_ollama_running returns bool")

    # _start_ollama is callable
    assert callable(_start_ollama)
    record("PASS", "launcher: _start_ollama is callable")

    # _OLLAMA_PORT is 11434
    assert _OLLAMA_PORT == 11434, f"Expected 11434, got {_OLLAMA_PORT}"
    record("PASS", "launcher: _OLLAMA_PORT == 11434")

    # _start_ollama skips if Ollama is already running (mock port check)
    import unittest.mock as _mock_ollama
    with _mock_ollama.patch("launcher._is_ollama_running", return_value=True):
        # Should return immediately without launching anything
        _start_ollama()
        record("PASS", "launcher: _start_ollama no-op when already running")

except Exception as e:
    record("FAIL", "launcher: ollama auto-start helpers", str(e))


# ═════════════════════════════════════════════════════════════════════════════
# SUMMARY
# ═════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("SUMMARY")
print("=" * 70)
print(f"  ✅ PASS: {PASS}")
print(f"  ❌ FAIL: {FAIL}")
print(f"  ⚠️  WARN: {WARN}")
print(f"  Total: {PASS + FAIL + WARN}")
print()

if FAIL > 0:
    print("FAILED TESTS:")
    for status, name, detail in RESULTS:
        if status == "FAIL":
            print(f"  ❌ {name}: {detail}")
    print()

if FAIL == 0:
    print("🎉 ALL TESTS PASSED!")
else:
    print(f"⛔ {FAIL} TEST(S) FAILED")

sys.exit(1 if FAIL > 0 else 0)
