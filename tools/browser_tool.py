"""Browser tool — shared visible browser automation via Playwright.

Provides the agent with browser automation sub-tools that open a *real*
Chromium window the user can see and interact with.  The browser uses a
**persistent profile** so cookies, logins, and localStorage survive
between sessions.

Design
------
* **Shared visible browser** — ``headless=False``, so the user can see what
  the agent is doing and intervene (e.g. type passwords, solve CAPTCHAs).
* **Persistent profile** — ``launch_persistent_context()`` stores state in
  ``~/.thoth/browser_profile/`` so sites stay logged-in across restarts.
* **Accessibility-tree snapshots** — after every action the tool takes a
  DOM snapshot and assigns numbered references ([1], [2], …) to
  interactive elements so the LLM can click/type by number.
* **Background workflow blocking** — browser actions are blocked when
  running inside a background workflow.
* **Channel detection** — prefers installed Chrome, then Edge (Windows),
  then falls back to Playwright's bundled Chromium.

Sub-tools (7)
-------------
``browser_navigate``   — go to a URL
``browser_click``      — click element by ref number
``browser_type``       — type text into element by ref number
``browser_scroll``     — scroll page up/down
``browser_snapshot``   — take a fresh accessibility snapshot
``browser_back``       — go back one page
``browser_tab``        — manage tabs (list / switch / new / close)
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import pathlib
import platform
import queue
import re
import signal
import subprocess
import threading
import time
from datetime import datetime
from typing import Any, Optional

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from tools.base import BaseTool
from tools import registry

logger = logging.getLogger(__name__)

# ── Data directory ───────────────────────────────────────────────────────────
DATA_DIR = pathlib.Path(
    os.environ.get("THOTH_DATA_DIR", pathlib.Path.home() / ".thoth")
)
DATA_DIR.mkdir(parents=True, exist_ok=True)
_PROFILE_DIR = DATA_DIR / "browser_profile"
_HISTORY_PATH = DATA_DIR / "browser_history.json"

_IS_WINDOWS = platform.system() == "Windows"

# ── Constants ────────────────────────────────────────────────────────────────
MAX_SNAPSHOT_CHARS = 25_000  # Truncate snapshot text returned to LLM
MAX_SNAPSHOT_ELEMENTS = 100  # Soft cap on interactive elements returned to LLM
_VIEWPORT = {"width": 1280, "height": 900}


# ═════════════════════════════════════════════════════════════════════════════
# BROWSER CHANNEL DETECTION
# ═════════════════════════════════════════════════════════════════════════════

def _detect_channel() -> str | None:
    """Return the best available browser channel, or *None* for bundled Chromium.

    Prefers Chrome (cross-platform), then Edge (Windows only).
    """
    # Channel names that Playwright recognises
    candidates = ["chrome"]
    if _IS_WINDOWS:
        candidates.append("msedge")

    try:
        from playwright.sync_api import sync_playwright
        for ch in candidates:
            try:
                pw = sync_playwright().start()
                browser = pw.chromium.launch(channel=ch, headless=True)
                browser.close()
                pw.stop()
                logger.info("Detected browser channel: %s", ch)
                return ch
            except Exception:
                try:
                    pw.stop()
                except Exception:
                    pass
    except Exception:
        pass
    logger.info("No installed browser detected — will use bundled Chromium")
    return None


# Cache the detection result
_cached_channel: str | None = None
_channel_detected: bool = False


def _get_channel() -> str | None:
    """Return cached channel (detect on first call)."""
    global _cached_channel, _channel_detected
    if not _channel_detected:
        _cached_channel = _detect_channel()
        _channel_detected = True
    return _cached_channel


# ═════════════════════════════════════════════════════════════════════════════
# ACCESSIBILITY SNAPSHOT (numbered refs)
# ═════════════════════════════════════════════════════════════════════════════

_SNAPSHOT_JS = r"""
() => {
    const MAX_ELEMENTS = """ + str(MAX_SNAPSHOT_ELEMENTS) + r""";
    const interactiveSelectors = [
        'a[href]', 'button', 'input', 'textarea', 'select',
        '[role="button"]', '[role="link"]', '[role="tab"]',
        '[role="menuitem"]', '[role="checkbox"]', '[role="radio"]',
        '[role="combobox"]', '[role="textbox"]', '[role="searchbox"]',
        '[contenteditable="true"]', 'summary',
    ];

    const selector = interactiveSelectors.join(', ');
    const elements = document.querySelectorAll(selector);
    const refs = [];
    let refNum = 1;
    let skipped = 0;

    // ── Smart filter: track duplicate link labels ───────────────────
    // First pass: count how many links share each normalised label
    const linkLabelCounts = {};
    const linkLabelSeen = {};  // how many of each label we've emitted
    for (const el of elements) {
        const tag = el.tagName.toLowerCase();
        if (tag !== 'a') continue;
        const lbl = (
            el.getAttribute('aria-label') ||
            el.getAttribute('title') ||
            el.innerText ||
            ''
        ).trim().toLowerCase();
        if (lbl) linkLabelCounts[lbl] = (linkLabelCounts[lbl] || 0) + 1;
    }

    // ── Second pass: build refs with filtering ──────────────────────
    for (const el of elements) {
        // Skip hidden / zero-size elements
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 && rect.height === 0) continue;
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden') continue;

        const tag = el.tagName.toLowerCase();
        const role = el.getAttribute('role') || '';
        const type = el.getAttribute('type') || '';
        const ariaLabel = (el.getAttribute('aria-label') || '').trim();
        let label = (
            ariaLabel ||
            el.getAttribute('title') ||
            el.getAttribute('placeholder') ||
            el.innerText ||
            el.getAttribute('alt') ||
            el.getAttribute('name') ||
            ''
        ).trim().substring(0, 80);

        const isLink = (tag === 'a' || role === 'link');
        const isFormControl = (tag === 'input' || tag === 'textarea' || tag === 'select');
        const isButton = (tag === 'button' || role === 'button');

        // ── Heuristic filters (links only — never skip form controls or buttons) ──
        if (isLink && !isFormControl && !isButton) {
            // 1) Skip links with empty / whitespace-only labels
            if (!label) { skipped++; continue; }

            // 2) Skip links with very short text (≤2 chars) unless they
            //    have a meaningful aria-label (≥4 chars)
            if (label.length <= 2 && ariaLabel.length < 4) { skipped++; continue; }

            // 3) Duplicate label dedup: if 4+ links share the same label,
            //    keep only the first 2 occurrences
            const normLabel = label.toLowerCase();
            if ((linkLabelCounts[normLabel] || 0) >= 4) {
                linkLabelSeen[normLabel] = (linkLabelSeen[normLabel] || 0) + 1;
                if (linkLabelSeen[normLabel] > 2) { skipped++; continue; }
            }
        }

        // ── Soft cap ────────────────────────────────────────────────
        if (refNum > MAX_ELEMENTS) { skipped++; continue; }

        const href = el.getAttribute('href') || '';
        const value = el.value !== undefined ? String(el.value).substring(0, 40) : '';

        // Store ref number as a data attribute for later retrieval
        el.setAttribute('data-thoth-ref', String(refNum));

        let desc = `[${refNum}]`;
        if (tag === 'a') desc += ` link "${label}"` + (href ? ` → ${href.substring(0, 100)}` : '');
        else if (tag === 'button' || role === 'button') desc += ` button "${label}"`;
        else if (tag === 'input') {
            desc += ` input[${type || 'text'}]`;
            if (label) desc += ` "${label}"`;
            if (value) desc += ` value="${value}"`;
        }
        else if (tag === 'textarea') {
            desc += ` textarea`;
            if (label) desc += ` "${label}"`;
            if (value) desc += ` value="${value.substring(0, 40)}"`;
        }
        else if (tag === 'select') {
            desc += ` select "${label}"`;
            if (value) desc += ` value="${value}"`;
        }
        else desc += ` ${tag}${role ? '[role=' + role + ']' : ''} "${label}"`;

        refs.push(desc);
        refNum++;
    }

    return {
        url: location.href,
        title: document.title,
        refs: refs,
        refCount: refNum - 1,
        skipped: skipped,
    };
}
"""


def _take_snapshot(page) -> dict:
    """Execute the snapshot JS on *page* and return the result dict."""
    try:
        return page.evaluate(_SNAPSHOT_JS)
    except Exception as exc:
        logger.warning("Snapshot failed: %s", exc)
        return {"url": page.url, "title": "", "refs": [], "refCount": 0, "skipped": 0}


def _format_snapshot(snap: dict) -> str:
    """Format a snapshot dict into a text block for the LLM."""
    skipped = snap.get("skipped", 0)
    ref_count = snap.get("refCount", 0)
    header = f"Interactive elements ({ref_count})"
    if skipped:
        header += f" — {skipped} low-value elements filtered"
    lines = [
        f"URL: {snap.get('url', '')}",
        f"Title: {snap.get('title', '')}",
        f"{header}:",
    ]
    for ref_line in snap.get("refs", []):
        lines.append(f"  {ref_line}")
    text = "\n".join(lines)
    if len(text) > MAX_SNAPSHOT_CHARS:
        text = text[:MAX_SNAPSHOT_CHARS] + "\n\n… (snapshot truncated)"
    return text


def _click_ref(page, ref: int) -> str:
    """Click the element with the given ref number (retries once on stale DOM)."""
    for attempt in range(2):
        el = page.query_selector(f'[data-thoth-ref="{ref}"]')
        if not el:
            if attempt == 0:
                page.wait_for_timeout(1500)
                continue
            return f"Error: element ref [{ref}] not found. Take a new snapshot to refresh refs."
        try:
            el.scroll_into_view_if_needed(timeout=3000)
            el.click(timeout=5000)
            page.wait_for_load_state("load", timeout=10000)
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            return "Clicked successfully."
        except Exception as exc:
            if attempt == 0 and ("not attached" in str(exc).lower() or "detached" in str(exc).lower()):
                page.wait_for_timeout(1500)
                continue
            return f"Click failed: {exc}"
    return f"Error: element ref [{ref}] could not be resolved after retry."


def _type_ref(page, ref: int, text: str, submit: bool = False) -> str:
    """Type text into the element with the given ref number (retries once on stale DOM)."""
    for attempt in range(2):
        el = page.query_selector(f'[data-thoth-ref="{ref}"]')
        if not el:
            if attempt == 0:
                page.wait_for_timeout(1500)
                continue
            return f"Error: element ref [{ref}] not found. Take a new snapshot to refresh refs."
        try:
            el.scroll_into_view_if_needed(timeout=3000)
            el.click(timeout=3000)
            el.fill(text)
            if submit:
                el.press("Enter")
                page.wait_for_load_state("load", timeout=15000)
                try:
                    page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
            return "Typed successfully."
        except Exception as exc:
            if attempt == 0 and ("not attached" in str(exc).lower() or "detached" in str(exc).lower()):
                page.wait_for_timeout(1500)
                continue
            return f"Type failed: {exc}"
    return f"Error: element ref [{ref}] could not be resolved after retry."


# ═════════════════════════════════════════════════════════════════════════════
# BROWSER SESSION — one per thread
# ═════════════════════════════════════════════════════════════════════════════

class BrowserSession:
    """Wraps a Playwright persistent browser context (one per thread).

    The browser window is visible (``headless=False``) and uses a shared
    profile directory so cookies/logins persist.

    **Threading model**: Playwright's sync API is bound to the OS thread
    that called ``sync_playwright().start()``.  Since the agent dispatches
    each tool call from a *different* daemon thread, we run a dedicated
    long-lived "Playwright thread" and marshal every operation onto it
    via a work queue.  This avoids the dreaded "cannot switch to a
    different thread" error.
    """

    def __init__(self):
        self._pw = None          # Playwright instance (owned by _pw_thread)
        self._context = None     # BrowserContext  (owned by _pw_thread)
        self._launched = False
        self._closed = False
        self._active_page = None  # Explicitly tracked active page
        self._browser_pid: int | None = None  # PID of the browser process
        self._launch_error: Exception | None = None  # Set if _pw_loop fails

        # Dedicated Playwright thread + work queue
        self._work_q: queue.Queue = queue.Queue()
        self._pw_thread: threading.Thread | None = None
        self._ready = threading.Event()   # set once PW is running

    # ── Internal: run callables on the Playwright thread ─────────────

    def _pw_loop(self) -> None:
        """Event loop running on the dedicated Playwright thread."""
        from playwright.sync_api import sync_playwright

        self._launch_error = None

        try:
            _PROFILE_DIR.mkdir(parents=True, exist_ok=True)
            channel = _get_channel()

            self._pw = sync_playwright().start()

            launch_kwargs: dict[str, Any] = {
                "user_data_dir": str(_PROFILE_DIR),
                "headless": False,
                "viewport": _VIEWPORT,
                "bypass_csp": True,
                "args": [
                    "--disable-blink-features=AutomationControlled",
                ],
            }
            if channel:
                launch_kwargs["channel"] = channel

            self._context = self._pw.chromium.launch_persistent_context(**launch_kwargs)
            self._launched = True

            # Capture browser PID for targeted cleanup on crash
            try:
                self._browser_pid = self._context.browser.process.pid
            except Exception:
                self._browser_pid = None

            logger.info("Browser session launched (channel=%s, pid=%s)",
                        channel or "chromium", self._browser_pid)
            self._ready.set()
        except Exception as exc:
            logger.error("Browser launch failed: %s", exc)
            self._launch_error = exc
            self._ready.set()  # unblock _run_on_pw_thread immediately
            # Clean up partial state
            try:
                if self._pw:
                    self._pw.stop()
            except Exception:
                pass
            self._pw = None
            self._context = None
            return

        # Process work items until a None sentinel arrives
        while True:
            item = self._work_q.get()
            if item is None:
                break  # shutdown sentinel
            fn, future = item
            try:
                result = fn()
                future.set_result(result)
            except Exception as exc:
                future.set_exception(exc)

        # Teardown (still on the PW thread)
        try:
            if self._context:
                self._context.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._context = None
        self._pw = None
        self._launched = False
        self._browser_pid = None

    def _kill_orphaned_browser(self) -> None:
        """Kill only the browser process Playwright launched (PID-scoped).

        Also removes the Chromium profile lock files so the next launch
        can claim the profile directory.
        """
        pid = self._browser_pid
        if pid:
            try:
                if _IS_WINDOWS:
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(pid)],
                        capture_output=True, timeout=10,
                    )
                else:
                    os.kill(pid, signal.SIGKILL)
                logger.info("Killed orphaned browser process (pid=%s)", pid)
            except (ProcessLookupError, OSError, subprocess.TimeoutExpired):
                logger.debug("Orphaned browser pid %s already dead", pid)
            self._browser_pid = None

        # Remove Chromium profile lock files so re-launch can acquire them
        for lock_name in ("SingletonLock", "lockfile"):
            lock_path = _PROFILE_DIR / lock_name
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _run_on_pw_thread(self, fn):
        """Submit *fn* to the Playwright thread and block until it returns."""
        if self._closed:
            raise RuntimeError("BrowserSession is closed")

        # Start (or restart) the PW thread — up to _MAX_RETRIES recovery attempts
        _MAX_RETRIES = 2
        if self._pw_thread is None or not self._pw_thread.is_alive():
            was_previous_crash = (self._pw_thread is not None
                                  and not self._launched)
            for attempt in range(_MAX_RETRIES + 1):
                if attempt > 0 or was_previous_crash:
                    logger.warning(
                        "Browser recovery attempt %d/%d — killing orphan & cleaning locks",
                        attempt + (1 if not was_previous_crash else 0),
                        _MAX_RETRIES,
                    )
                    self._kill_orphaned_browser()
                    time.sleep(2)

                self._ready.clear()
                self._launch_error = None
                self._work_q = queue.Queue()  # fresh queue
                self._pw_thread = threading.Thread(
                    target=self._pw_loop, daemon=True, name="thoth-pw"
                )
                self._pw_thread.start()
                self._ready.wait(timeout=60)

                if self._launched:
                    break  # success

                # Launch failed — check if we have retries left
                err = self._launch_error
                if attempt < _MAX_RETRIES:
                    logger.warning("Browser launch failed (attempt %d): %s",
                                   attempt + 1, err)
                    continue

                # Out of retries — raise with actual error
                raise RuntimeError(
                    f"Browser failed to launch after {_MAX_RETRIES + 1} attempts: {err}"
                )

        future: concurrent.futures.Future = concurrent.futures.Future()
        self._work_q.put((fn, future))
        return future.result(timeout=120)

    # ── Lifecycle ────────────────────────────────────────────────────────

    @property
    def page(self):
        """Return the explicitly-tracked active page.

        Falls back to the last page in the context if the tracked page is
        closed or was never set.  MUST be called from the PW thread
        (i.e. inside a lambda passed to ``_run_on_pw_thread``).
        """
        # Prefer the explicitly tracked page if still valid
        if self._active_page is not None:
            try:
                if not self._active_page.is_closed():
                    return self._active_page
            except Exception:
                pass
            self._active_page = None

        pages = self._context.pages
        if not pages:
            pg = self._context.new_page()
            self._active_page = pg
            return pg
        self._active_page = pages[-1]
        return pages[-1]

    def close(self) -> None:
        """Shut down the browser and Playwright thread."""
        self._closed = True
        try:
            self._work_q.put(None)  # sentinel to exit _pw_loop
        except Exception:
            pass
        if self._pw_thread and self._pw_thread.is_alive():
            self._pw_thread.join(timeout=10)

    # ── Actions (called from any thread) ─────────────────────────────

    def navigate(self, url: str) -> str:
        """Navigate to *url* and return snapshot."""
        def _do():
            page = self.page
            try:
                page.goto(url, wait_until="load", timeout=30000)
                # Best-effort wait for network to settle (catches JS redirects)
                try:
                    page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
            except Exception as exc:
                return f"Navigation failed: {exc}"
            snap = _take_snapshot(page)
            return _format_snapshot(snap)
        return self._run_on_pw_thread(_do)

    def click(self, ref: int) -> str:
        """Click element by ref and return snapshot."""
        def _do():
            page = self.page
            result = _click_ref(page, ref)
            snap = _take_snapshot(page)
            return f"{result}\n\n{_format_snapshot(snap)}"
        return self._run_on_pw_thread(_do)

    def type_text(self, ref: int, text: str, submit: bool = False) -> str:
        """Type into element by ref and return snapshot."""
        def _do():
            page = self.page
            result = _type_ref(page, ref, text, submit)
            snap = _take_snapshot(page)
            return f"{result}\n\n{_format_snapshot(snap)}"
        return self._run_on_pw_thread(_do)

    def scroll(self, direction: str = "down", amount: int = 3) -> str:
        """Scroll the page and return snapshot."""
        def _do():
            page = self.page
            delta = amount * 400
            if direction == "up":
                delta = -delta
            try:
                page.mouse.wheel(0, delta)
                page.wait_for_timeout(500)
            except Exception as exc:
                return f"Scroll failed: {exc}"
            snap = _take_snapshot(page)
            return _format_snapshot(snap)
        return self._run_on_pw_thread(_do)

    def snapshot(self) -> str:
        """Take a fresh snapshot of the current page."""
        def _do():
            page = self.page
            snap = _take_snapshot(page)
            return _format_snapshot(snap)
        return self._run_on_pw_thread(_do)

    def go_back(self) -> str:
        """Go back one page and return snapshot."""
        def _do():
            page = self.page
            try:
                page.go_back(wait_until="load", timeout=10000)
                try:
                    page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
            except Exception as exc:
                return f"Back navigation failed: {exc}"
            snap = _take_snapshot(page)
            return _format_snapshot(snap)
        return self._run_on_pw_thread(_do)

    def tab_action(self, action: str = "list", tab_id: int | None = None,
                   url: str | None = None) -> str:
        """Manage tabs: list, switch, new, close."""
        def _do():
            pages = self._context.pages

            if action == "list":
                lines = [f"Open tabs ({len(pages)}):"]
                for i, pg in enumerate(pages):
                    marker = " ← active" if pg == self.page else ""
                    lines.append(f"  [{i}] {pg.url} — {pg.title()}{marker}")
                return "\n".join(lines)

            elif action == "switch":
                if tab_id is None or tab_id < 0 or tab_id >= len(pages):
                    return f"Invalid tab_id. Use 0–{len(pages) - 1}."
                self._active_page = pages[tab_id]
                pages[tab_id].bring_to_front()
                snap = _take_snapshot(pages[tab_id])
                return f"Switched to tab [{tab_id}].\n\n{_format_snapshot(snap)}"

            elif action == "new":
                new_page = self._context.new_page()
                self._active_page = new_page
                new_page.bring_to_front()
                if url:
                    try:
                        new_page.goto(url, wait_until="load", timeout=30000)
                        try:
                            new_page.wait_for_load_state("networkidle", timeout=5000)
                        except Exception:
                            pass
                    except Exception as exc:
                        return f"New tab opened but navigation failed: {exc}"
                snap = _take_snapshot(new_page)
                return f"Opened new tab [{len(self._context.pages) - 1}].\n\n{_format_snapshot(snap)}"

            elif action == "close":
                if tab_id is None or tab_id < 0 or tab_id >= len(pages):
                    return f"Invalid tab_id. Use 0–{len(pages) - 1}."
                if len(pages) <= 1:
                    return "Cannot close the last tab."
                closed_page = pages[tab_id]
                if self._active_page == closed_page:
                    self._active_page = None
                closed_page.close()
                remaining = self._context.pages
                active = self.page  # resolves to a valid page
                snap = _take_snapshot(active)
                return f"Closed tab [{tab_id}]. {len(remaining)} tab(s) remaining.\n\n{_format_snapshot(snap)}"

            else:
                return f"Unknown tab action: {action}. Use list/switch/new/close."
        return self._run_on_pw_thread(_do)

    def take_screenshot(self) -> bytes | None:
        """Take a screenshot (PNG bytes) of the current page."""
        if not self._launched or self._closed:
            return None
        try:
            def _do():
                page = self.page
                return page.screenshot(type="png")
            return self._run_on_pw_thread(_do)
        except Exception:
            return None


# ═════════════════════════════════════════════════════════════════════════════
# BROWSER SESSION MANAGER — one session per thread
# ═════════════════════════════════════════════════════════════════════════════

class BrowserSessionManager:
    """Manages a **single shared** :class:`BrowserSession` for all threads.

    Only one Chromium instance can use a persistent profile directory at a
    time.  Rather than per-thread sessions (which would fight over the
    profile lock), every thread shares the same browser window.
    """

    def __init__(self):
        self._shared_session: BrowserSession | None = None
        self._lock = threading.Lock()

    # Kept for backward compat with UI code that checks membership
    @property
    def _sessions(self) -> dict[str, BrowserSession]:
        """Legacy shim — returns a dict-like view for ``in`` checks."""
        if self._shared_session is not None:
            return {"__shared__": self._shared_session}
        return {}

    def has_active_session(self) -> bool:
        """Return True if a browser session has been created."""
        return self._shared_session is not None

    def get_session(self, thread_id: str = "") -> BrowserSession:
        """Return the shared browser session (created on first call)."""
        with self._lock:
            if self._shared_session is None:
                self._shared_session = BrowserSession()
            return self._shared_session

    def kill_session(self, thread_id: str) -> None:
        """No-op — the shared browser stays open until app exit."""
        pass

    def kill_all(self) -> None:
        """Shut down the shared browser (called on app exit)."""
        with self._lock:
            session = self._shared_session
            self._shared_session = None
        if session:
            session.close()


_session_manager = BrowserSessionManager()


def get_session_manager() -> BrowserSessionManager:
    """Return the global browser session manager (for cleanup from UI code)."""
    return _session_manager


# ═════════════════════════════════════════════════════════════════════════════
# BROWSER HISTORY PERSISTENCE
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
        logger.warning("Failed to save browser history", exc_info=True)


def get_browser_history(thread_id: str) -> list[dict]:
    """Get browser history entries for a thread."""
    return _load_history().get(thread_id, [])


def append_browser_history(thread_id: str, entry: dict) -> None:
    """Append a browser action entry to history for a thread."""
    history = _load_history()
    history.setdefault(thread_id, []).append(entry)
    _save_history(history)


def clear_browser_history(thread_id: str) -> None:
    """Clear browser history for a thread."""
    history = _load_history()
    if thread_id in history:
        del history[thread_id]
        _save_history(history)


# ═════════════════════════════════════════════════════════════════════════════
# HELPER — background workflow guard
# ═════════════════════════════════════════════════════════════════════════════

def _block_if_background(action_name: str) -> str | None:
    """Return an error string if running in a background workflow, else None."""
    try:
        from agent import is_background_workflow
        if is_background_workflow():
            return (
                f"⚠️ BLOCKED: browser_{action_name} cannot run in a "
                "background workflow. Browser automation requires a visible "
                "window and user presence. Do NOT retry this tool. "
                "Inform the user that this action was skipped."
            )
    except ImportError:
        pass
    return None


def _get_thread_id() -> str:
    """Get the current thread ID from the agent context."""
    try:
        from agent import _current_thread_id_var
        return _current_thread_id_var.get() or "default"
    except ImportError:
        return "default"


# ═════════════════════════════════════════════════════════════════════════════
# PYDANTIC INPUT SCHEMAS
# ═════════════════════════════════════════════════════════════════════════════

class _NavigateInput(BaseModel):
    url: str = Field(description="The URL to navigate to (must start with http:// or https://)")

class _ClickInput(BaseModel):
    ref: int = Field(description="The reference number [N] of the element to click, from the last snapshot")

class _TypeInput(BaseModel):
    ref: int = Field(description="The reference number [N] of the input element to type into")
    text: str = Field(description="The text to type into the element")
    submit: bool = Field(default=False, description="Press Enter after typing (e.g. to submit a search)")

class _ScrollInput(BaseModel):
    direction: str = Field(default="down", description="Scroll direction: 'up' or 'down'")
    amount: int = Field(default=3, description="Number of scroll steps (1 = ~400px)")

class _TabInput(BaseModel):
    action: str = Field(default="list", description="Tab action: 'list', 'switch', 'new', or 'close'")
    tab_id: Optional[int] = Field(default=None, description="Tab index for switch/close actions")
    url: Optional[str] = Field(default=None, description="URL to open in a new tab (only for action='new')")


# ═════════════════════════════════════════════════════════════════════════════
# BROWSER TOOL
# ═════════════════════════════════════════════════════════════════════════════

class BrowserTool(BaseTool):

    @property
    def name(self) -> str:
        return "browser"

    @property
    def display_name(self) -> str:
        return "🌐 Browser"

    @property
    def description(self) -> str:
        return (
            "Automate a real browser window the user can see. "
            "Navigate websites, click buttons, fill forms, read page content. "
            "Uses a persistent profile so logins and cookies are preserved."
        )

    @property
    def enabled_by_default(self) -> bool:
        return False

    @property
    def config_schema(self) -> dict[str, dict]:
        return {}

    @property
    def destructive_tool_names(self) -> set[str]:
        return set()

    def as_langchain_tools(self) -> list:
        """Return 7 browser sub-tools for the agent."""

        # ── Navigate ─────────────────────────────────────────────────────

        def browser_navigate(url: str) -> str:
            """Navigate the CURRENT browser tab to a URL (replaces the current page).

            Use this to open a website in the active tab.  If the user wants a
            NEW tab instead, use browser_tab(action='new', url=...).
            The browser window is visible — the user can see what you're doing.
            After navigation, a snapshot of all clickable/typeable elements is
            returned with numbered references.

            Args:
                url: The URL to navigate to (must start with http:// or https://)
            """
            blocked = _block_if_background("navigate")
            if blocked:
                return blocked

            # Security: reject javascript: URLs
            if url.strip().lower().startswith("javascript:"):
                return "Error: javascript: URLs are not allowed for security reasons."
            if not url.strip().lower().startswith(("http://", "https://")):
                url = "https://" + url

            thread_id = _get_thread_id()
            session = _session_manager.get_session(thread_id)
            result = session.navigate(url)

            # Persist to history
            append_browser_history(thread_id, {
                "action": "navigate",
                "url": url,
                "timestamp": datetime.now().isoformat(),
            })
            return result

        # ── Click ────────────────────────────────────────────────────────

        def browser_click(ref: int) -> str:
            """Click an interactive element by its reference number from the snapshot.

            After the last browser_navigate or browser_snapshot call, each
            interactive element has a numbered reference like [1], [2], etc.
            Pass that number here to click it.  A new snapshot is returned
            after clicking.

            Args:
                ref: The reference number [N] of the element to click
            """
            blocked = _block_if_background("click")
            if blocked:
                return blocked

            thread_id = _get_thread_id()
            session = _session_manager.get_session(thread_id)
            result = session.click(ref)

            append_browser_history(thread_id, {
                "action": "click",
                "ref": ref,
                "timestamp": datetime.now().isoformat(),
            })
            return result

        # ── Type ─────────────────────────────────────────────────────────

        def browser_type(ref: int, text: str, submit: bool = False) -> str:
            """Type text into an input field identified by its reference number.

            After typing, a new snapshot is returned.  Set submit=True to
            press Enter after typing (e.g. to submit a search form).

            Args:
                ref: The reference number [N] of the input element
                text: The text to type
                submit: Whether to press Enter after typing (default: False)
            """
            blocked = _block_if_background("type")
            if blocked:
                return blocked

            thread_id = _get_thread_id()
            session = _session_manager.get_session(thread_id)
            result = session.type_text(ref, text, submit)

            append_browser_history(thread_id, {
                "action": "type",
                "ref": ref,
                "text": text,
                "submit": submit,
                "timestamp": datetime.now().isoformat(),
            })
            return result

        # ── Scroll ───────────────────────────────────────────────────────

        def browser_scroll(direction: str = "down", amount: int = 3) -> str:
            """Scroll the page up or down and return a fresh snapshot.

            Args:
                direction: 'up' or 'down' (default: 'down')
                amount: Number of scroll steps, each ~400px (default: 3)
            """
            blocked = _block_if_background("scroll")
            if blocked:
                return blocked

            thread_id = _get_thread_id()
            session = _session_manager.get_session(thread_id)
            result = session.scroll(direction, amount)

            append_browser_history(thread_id, {
                "action": "scroll",
                "direction": direction,
                "amount": amount,
                "timestamp": datetime.now().isoformat(),
            })
            return result

        # ── Snapshot ─────────────────────────────────────────────────────

        def browser_snapshot() -> str:
            """Take a fresh snapshot of the current page's interactive elements.

            Returns the page URL, title, and a numbered list of all clickable,
            typeable, and interactive elements.  Use this after the user
            interacts with the browser manually, or to refresh stale refs.
            """
            blocked = _block_if_background("snapshot")
            if blocked:
                return blocked

            thread_id = _get_thread_id()
            session = _session_manager.get_session(thread_id)
            return session.snapshot()

        # ── Back ─────────────────────────────────────────────────────────

        def browser_back() -> str:
            """Go back to the previous page (like pressing the Back button).

            Returns a fresh snapshot of the page after going back.
            """
            blocked = _block_if_background("back")
            if blocked:
                return blocked

            thread_id = _get_thread_id()
            session = _session_manager.get_session(thread_id)
            result = session.go_back()

            append_browser_history(thread_id, {
                "action": "back",
                "timestamp": datetime.now().isoformat(),
            })
            return result

        # ── Tab ──────────────────────────────────────────────────────────

        def browser_tab(action: str = "list", tab_id: int | None = None,
                        url: str | None = None) -> str:
            """Manage browser tabs: list, switch, open new, or close.

            Use this tool — NOT browser_navigate — when the user wants a new tab.

            Actions:
            - 'list': show all open tabs with their indices
            - 'switch': switch to tab by tab_id
            - 'new': open a new tab (optionally with a URL). Use this when the
              user says "open … in a new tab".
            - 'close': close tab by tab_id

            Args:
                action: One of 'list', 'switch', 'new', 'close'
                tab_id: Tab index (required for 'switch' and 'close')
                url: URL to open in a new tab (only for action='new')
            """
            blocked = _block_if_background("tab")
            if blocked:
                return blocked

            # Validate URL for new tab
            if action == "new" and url:
                if url.strip().lower().startswith("javascript:"):
                    return "Error: javascript: URLs are not allowed for security reasons."
                if not url.strip().lower().startswith(("http://", "https://")):
                    url = "https://" + url

            thread_id = _get_thread_id()
            session = _session_manager.get_session(thread_id)
            result = session.tab_action(action, tab_id, url)

            append_browser_history(thread_id, {
                "action": f"tab_{action}",
                "tab_id": tab_id,
                "url": url,
                "timestamp": datetime.now().isoformat(),
            })
            return result

        # ── Build StructuredTool list ────────────────────────────────────

        return [
            StructuredTool.from_function(
                func=browser_navigate,
                name="browser_navigate",
                description=(
                    "Navigate the browser to a URL. Opens a visible browser "
                    "window and returns a snapshot of all interactive elements "
                    "with numbered references. The user can see the browser."
                ),
                args_schema=_NavigateInput,
            ),
            StructuredTool.from_function(
                func=browser_click,
                name="browser_click",
                description=(
                    "Click an interactive element by its reference number "
                    "from the last browser snapshot. Returns a new snapshot."
                ),
                args_schema=_ClickInput,
            ),
            StructuredTool.from_function(
                func=browser_type,
                name="browser_type",
                description=(
                    "Type text into an input field by its reference number. "
                    "Set submit=True to press Enter after typing. "
                    "Returns a new snapshot."
                ),
                args_schema=_TypeInput,
            ),
            StructuredTool.from_function(
                func=browser_scroll,
                name="browser_scroll",
                description=(
                    "Scroll the page up or down. Returns a fresh snapshot "
                    "of interactive elements after scrolling."
                ),
                args_schema=_ScrollInput,
            ),
            StructuredTool.from_function(
                func=browser_snapshot,
                name="browser_snapshot",
                description=(
                    "Take a fresh snapshot of the current page's interactive "
                    "elements. Use after manual user interaction or when "
                    "refs may be stale."
                ),
            ),
            StructuredTool.from_function(
                func=browser_back,
                name="browser_back",
                description=(
                    "Go back to the previous page in browser history. "
                    "Returns a fresh snapshot."
                ),
            ),
            StructuredTool.from_function(
                func=browser_tab,
                name="browser_tab",
                description=(
                    "Manage browser tabs: list all tabs, switch to a tab, "
                    "open a new tab (optionally with URL), or close a tab."
                ),
                args_schema=_TabInput,
            ),
        ]


registry.register(BrowserTool())
