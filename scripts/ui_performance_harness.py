"""Optional local UI performance harness for a running Row-Bot instance.

This intentionally stays outside the default test suite. It records coarse
HTTP reachability timings and process RSS when pointed at an already running
app, and leaves browser-level scenario automation to manual QA or Playwright
where available.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.request import urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
for _path in (PROJECT_ROOT, PROJECT_ROOT / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))


@dataclass
class CheckResult:
    name: str
    elapsed_ms: float
    ok: bool
    detail: str = ""
    rss_mb: float | None = None
    measurement: str = "probe_helper"
    process_role: str = "probe"
    pid: int = field(default_factory=os.getpid)


def process_sample(role: str, pid: int | None) -> dict[str, object]:
    """Attribute a point sample to an explicit process, never infer server RSS."""
    sample: dict[str, object] = {"role": role, "pid": pid, "rss_mb": None, "status": "unavailable"}
    if pid is None:
        return sample
    try:
        import psutil

        sample["rss_mb"] = psutil.Process(pid).memory_info().rss / (1024 * 1024)
        sample["status"] = "observed"
    except Exception:
        pass
    return sample


def _rss_mb() -> float | None:
    try:
        import psutil

        return psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)
    except Exception:
        return None


def _row_bot_home() -> Path:
    from row_bot.data_paths import get_row_bot_data_dir

    return get_row_bot_data_dir(create=False)


@contextmanager
def _read_metadata(db_path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        yield connection
    finally:
        connection.close()


def _fetch(name: str, url: str, timeout: float) -> CheckResult:
    before = _rss_mb()
    started = time.perf_counter()
    try:
        with urlopen(url, timeout=timeout) as response:
            body = response.read(2048)
        elapsed = (time.perf_counter() - started) * 1000.0
        after = _rss_mb()
        return CheckResult(
            name=name,
            elapsed_ms=elapsed,
            ok=200 <= response.status < 500,
            detail=f"status={response.status} bytes_sampled={len(body)}",
            rss_mb=None if before is None or after is None else after - before,
            measurement="http_reachability",
        )
    except Exception as exc:
        elapsed = (time.perf_counter() - started) * 1000.0
        return CheckResult(name=name, elapsed_ms=elapsed, ok=False, detail=type(exc).__name__,
                           rss_mb=None, measurement="http_reachability")


def _resolve_transcript_thread(selector: str) -> tuple[str, str]:
    db_path = _row_bot_home() / "threads.db"
    if not db_path.exists():
        raise FileNotFoundError(f"threads.db not found at {db_path}")
    if selector and selector != "latest":
        with _read_metadata(db_path) as conn:
            row = conn.execute(
                "SELECT thread_id, COALESCE(name, '') FROM thread_meta WHERE thread_id = ?",
                (selector,),
            ).fetchone()
        return (selector, row[1] if row else "")
    with _read_metadata(db_path) as conn:
        row = conn.execute(
            """
            SELECT thread_id, COALESCE(name, '')
            FROM thread_meta
            ORDER BY COALESCE(updated_at, created_at, '') DESC
            LIMIT 1
            """
        ).fetchone()
    if not row:
        raise RuntimeError("No thread metadata found")
    return str(row[0]), str(row[1] or "")


def _profile_transcript(selector: str) -> CheckResult:
    from row_bot.ui.helpers import load_thread_messages
    from row_bot.ui.transcript import TRANSCRIPT_WINDOW_SIZE, choose_transcript_window, message_keys

    thread_id, _name = _resolve_transcript_thread(selector)
    before = _rss_mb()
    started = time.perf_counter()
    messages = load_thread_messages(thread_id)
    elapsed = (time.perf_counter() - started) * 1000.0
    after = _rss_mb()
    window = choose_transcript_window(len(messages), window_size=TRANSCRIPT_WINDOW_SIZE)
    keys = message_keys(messages[window.start:window.end], start=window.start)
    ok = elapsed <= 1_000.0 and window.visible_count <= TRANSCRIPT_WINDOW_SIZE
    detail = (
        f"rows={len(messages)} "
        f"window={window.start}:{window.end} visible={window.visible_count} "
        f"keys={len(keys)}"
    )
    return CheckResult(
        name="transcript.latest_profile",
        elapsed_ms=elapsed,
        ok=ok,
        detail=detail,
        rss_mb=None if before is None or after is None else after - before,
    )


def _resolve_latest_blank_thread() -> tuple[str, str]:
    db_path = _row_bot_home() / "threads.db"
    if not db_path.exists():
        raise FileNotFoundError(f"threads.db not found at {db_path}")
    from row_bot.ui.helpers import load_thread_messages

    with _read_metadata(db_path) as conn:
        rows = conn.execute(
            """
            SELECT thread_id, COALESCE(name, '')
            FROM thread_meta
            ORDER BY COALESCE(updated_at, created_at, '') DESC
            LIMIT 100
            """
        ).fetchall()
    for thread_id, name in rows:
        if not load_thread_messages(str(thread_id)):
            return str(thread_id), str(name or "")
    raise RuntimeError("No recent blank thread metadata found")


def _profile_blank_thread() -> CheckResult:
    from row_bot.ui.helpers import load_thread_messages
    from row_bot.ui.transcript import TRANSCRIPT_WINDOW_SIZE, choose_transcript_window, message_keys

    thread_id, _name = _resolve_latest_blank_thread()
    before = _rss_mb()
    started = time.perf_counter()
    messages = load_thread_messages(thread_id)
    elapsed = (time.perf_counter() - started) * 1000.0
    after = _rss_mb()
    window = choose_transcript_window(len(messages), window_size=TRANSCRIPT_WINDOW_SIZE)
    keys = message_keys(messages[window.start:window.end], start=window.start)
    ok = elapsed <= 250.0 and len(messages) == 0 and window.visible_count == 0 and not keys
    detail = (
        f"rows={len(messages)} "
        f"window={window.start}:{window.end} visible={window.visible_count} "
        f"keys={len(keys)}"
    )
    return CheckResult(
        name="transcript.blank_thread_profile",
        elapsed_ms=elapsed,
        ok=ok,
        detail=detail,
        rss_mb=None if before is None or after is None else after - before,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--launch", action="store_true", help="Launch a temporary app process before checking HTTP.")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--output", default="")
    parser.add_argument("--server-pid", type=int, help="Explicit running server PID for a separate RSS sample.")
    parser.add_argument("--browser-pid", type=int, action="append", default=[],
                        help="Explicit browser/renderer PID; repeat to record each independently.")
    parser.add_argument(
        "--profile-transcript",
        default="",
        metavar="THREAD_ID|latest",
        help="Profile real transcript loading/windowing from the local Row-Bot data store.",
    )
    parser.add_argument(
        "--profile-blank-thread",
        action="store_true",
        help="Profile the latest local blank thread without creating new user data.",
    )
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    results: list[CheckResult] = []
    launch_messages: list[dict[str, str]] = []
    if args.launch:
        try:
            from pathlib import Path
            from scripts.smoke_app import run_app_smoke

            started = time.perf_counter()
            smoke = run_app_smoke(cwd=Path.cwd(), port=args.port, timeout=max(args.timeout, 60.0))
            elapsed = (time.perf_counter() - started) * 1000.0
            launch_messages = [{"status": status, "message": message} for status, message in smoke.messages]
            results.append(
                CheckResult(
                    name="app.launch_smoke",
                    elapsed_ms=elapsed,
                    ok=smoke.ok,
                    detail=f"port={smoke.port}",
                    rss_mb=None,
                    measurement="owned_child_http_readiness",
                )
            )
        except Exception as exc:
            results.append(
                CheckResult(
                    name="app.launch_smoke",
                    elapsed_ms=0.0,
                    ok=False,
                    detail=str(exc),
                    rss_mb=None,
                )
            )
    else:
        results.append(_fetch("app.root", f"{base}/", args.timeout))
    if args.profile_transcript:
        try:
            results.append(_profile_transcript(args.profile_transcript))
        except Exception as exc:
            results.append(
                CheckResult(
                    name="transcript.latest_profile",
                    elapsed_ms=0.0,
                    ok=False,
                    detail=str(exc),
                    rss_mb=None,
                )
            )
    if args.profile_blank_thread:
        try:
            results.append(_profile_blank_thread())
        except Exception as exc:
            results.append(
                CheckResult(
                    name="transcript.blank_thread_profile",
                    elapsed_ms=0.0,
                    ok=False,
                    detail=str(exc),
                    rss_mb=None,
                )
            )
    payload = {
        "base_url": base,
        "process_samples": [process_sample("probe", os.getpid()),
                            process_sample("server", args.server_pid),
                            *(process_sample("browser", pid) for pid in args.browser_pid)],
        "measurement_notes": [
            "CheckResult.rss_mb is the probe-process RSS delta, not server or browser memory.",
            "HTTP reachability/readiness does not measure browser paint or interactive usability.",
            "Process samples are explicit-PID point observations; summed RSS is not unique memory.",
        ],
        "results": [asdict(result) for result in results],
        "launch_messages": launch_messages,
        "budgets": {
            "settings_shell_ms": 300,
            "knowledge_initial_ms": 500,
            "knowledge_search_ms": 500,
            "editor_core_ms": 500,
            "transcript_load_ms": 1000,
            "blank_thread_load_ms": 250,
            "transcript_initial_rows": 60,
            "rss_delta_mb": 250,
        },
        "manual_scenarios": [
            "Settings > Knowledge > search a disposable fixture > Edit > Save",
            "Settings > Models",
            "Knowledge Map graph details and edit",
            "Large transcript with streaming finalization",
        ],
    }
    text = json.dumps(payload, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text)
    print(text)
    return 0 if all(result.ok for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
