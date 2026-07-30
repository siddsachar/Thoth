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
  ``~/.row-bot/browser_profile/`` so sites stay logged-in across restarts.
* **Per-thread tab isolation** — each agent thread (interactive chat or
  background task) gets its own tab within the single browser window.
* **Accessibility-tree snapshots** — after every action the tool takes a
  DOM snapshot and assigns numbered references ([1], [2], …) to
  interactive elements so the LLM can click/type by number.
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
import ipaddress
import logging
import os
import pathlib
import platform
import queue
import re
import signal
import subprocess
import tempfile
from urllib.parse import parse_qsl, urlparse, urlunparse
import threading
import time
from datetime import datetime
from typing import Any, Optional
from dataclasses import dataclass, field

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from row_bot.cancellation import current_cancellation_scope
from row_bot.data_paths import get_row_bot_data_dir
from row_bot.tools.base import BaseTool
from row_bot.tools import registry

logger = logging.getLogger(__name__)

# ── Data directory ───────────────────────────────────────────────────────────
DATA_DIR = get_row_bot_data_dir()
DATA_DIR.mkdir(parents=True, exist_ok=True)
_PROFILE_DIR = DATA_DIR / "browser_profile"
_HISTORY_PATH = DATA_DIR / "browser_history.json"
_history_lock = threading.RLock()

_IS_WINDOWS = platform.system() == "Windows"

# ── Constants ────────────────────────────────────────────────────────────────
def _snapshot_char_budget() -> int:
    from row_bot.models import get_tool_budget
    return get_tool_budget(0.20, floor=15_000, ceiling=150_000)

def _snapshot_element_cap() -> int:
    from row_bot.models import get_context_size
    return min(500, max(80, get_context_size() // 400))

_VIEWPORT = {"width": 1280, "height": 900}


def _browser_runs_headless() -> bool:
    raw = str(os.environ.get("ROW_BOT_BROWSER_HEADLESS") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


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

def _build_snapshot_js(max_elements: int) -> str:
    return r"""
() => {
    const MAX_ELEMENTS = """ + str(max_elements) + r""";
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
        const valueLength = el.value !== undefined ? String(el.value).length : 0;

        // Store ref number as a data attribute for later retrieval
        el.setAttribute('data-row-bot-ref', String(refNum));

        let desc = `[${refNum}]`;
        if (tag === 'a') desc += ` link "${label}"` + (href ? ` → ${href.substring(0, 100)}` : '');
        else if (tag === 'button' || role === 'button') desc += ` button "${label}"`;
        else if (tag === 'input') {
            desc += ` input[${type || 'text'}]`;
            if (label) desc += ` "${label}"`;
            if (valueLength) desc += ` value_length=${valueLength}`;
        }
        else if (tag === 'textarea') {
            desc += ` textarea`;
            if (label) desc += ` "${label}"`;
            if (valueLength) desc += ` value_length=${valueLength}`;
        }
        else if (tag === 'select') {
            desc += ` select "${label}"`;
            if (valueLength) desc += ` value_present=true`;
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
        js = _build_snapshot_js(_snapshot_element_cap())
        return page.evaluate(js)
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
    budget = _snapshot_char_budget()
    if len(text) > budget:
        text = text[:budget] + "\n\n… (snapshot truncated)"
    return text


def _click_ref(page, ref: int) -> str:
    """Click the element with the given ref number (retries once on stale DOM)."""
    for attempt in range(2):
        el = page.query_selector(f'[data-row-bot-ref="{ref}"]')
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
        el = page.query_selector(f'[data-row-bot-ref="{ref}"]')
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


# ── Prompt‑injection defence: URL exfiltration check ─────────────────────
_B64_SEGMENT_RE = re.compile(r"[A-Za-z0-9+/=]{100,}")


def _check_exfiltration_url(url: str) -> str:
    """Soft check for data‑exfiltration via URL query parameters.

    Returns a warning string if the URL looks suspicious, empty string
    otherwise.  Does NOT block navigation — only appends a warning.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return ""
    qs = parsed.query + (parsed.fragment or "")
    if len(qs) > 500:
        return (
            "(⚠ Security warning: this URL has an unusually long query string "
            f"({len(qs)} chars) which may be an attempt to exfiltrate data. "
            "Proceed with caution.)"
        )
    if _B64_SEGMENT_RE.search(qs):
        return (
            "(⚠ Security warning: this URL contains a large base64‑like "
            "segment in its query parameters, which may be an attempt to "
            "exfiltrate data via URL encoding. Proceed with caution.)"
        )
    return ""


def _history_url(url: str) -> str:
    """Persist origin/path only; query and fragment may contain secrets."""

    try:
        parsed = urlparse(url)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
    except Exception:
        return "[invalid URL]"


def _navigation_policy(url: str, current_url: str = "") -> tuple[str, str]:
    """Return (block|ask|allow, reason) for a proposed navigation."""

    try:
        parsed = urlparse(url)
    except Exception:
        return "block", "Malformed URL."
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return "block", "Only explicit HTTP(S) origins are allowed."
    if parsed.username or parsed.password:
        return "block", "Credentials in URLs are not allowed."
    warning = _check_exfiltration_url(url)
    if warning:
        return "block", warning.strip("()")
    sensitive_names = re.compile(r"(?:token|secret|password|passwd|api[_-]?key|auth|session|otp|code)", re.IGNORECASE)
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if sensitive_names.search(key) and value:
            return "ask", f"URL query parameter '{key}' may contain sensitive data."
    host = parsed.hostname.strip("[]")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if host.casefold() == "localhost" or (address and (address.is_private or address.is_loopback or address.is_link_local)):
        return "ask", "Navigation targets a local or private-network origin."
    if current_url:
        current = urlparse(current_url)
        current_origin = (current.scheme.lower(), (current.hostname or "").lower(), current.port)
        proposed_origin = (parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port)
        if current.hostname and current_origin != proposed_origin and parsed.query:
            return "ask", f"Cross-origin navigation with query data: {current_origin[1]} → {proposed_origin[1]}."
    return "allow", ""


def _consequential_browser_target(metadata: dict[str, Any], *, submit: bool = False) -> str:
    from row_bot.computer_use.policy import is_consequential_label

    label = " ".join(str(metadata.get(key) or "") for key in ("label", "text", "type", "role", "href", "form_action"))
    if submit:
        return "Submitting a form may create an external side effect."
    if str(metadata.get("type") or "").lower() == "file":
        return "File upload controls require approval."
    if metadata.get("download"):
        return "Downloads require approval."
    if is_consequential_label(label):
        return f"The target '{str(metadata.get('label') or metadata.get('text') or 'control')[:120]}' may create an external side effect."
    return ""


@dataclass
class _BrowserWorkItem:
    fn: Any
    future: concurrent.futures.Future
    scope: Any = None
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _cancelled: bool = False
    _dispatched: bool = False

    def cancel(self) -> None:
        with self._lock:
            if not self._dispatched:
                self._cancelled = True
                self.future.cancel()

    def begin_dispatch(self) -> bool:
        with self._lock:
            if self._cancelled or self.future.cancelled() or (self.scope is not None and self.scope.is_cancelled()):
                self._cancelled = True
                self.future.cancel()
                return False
            self._dispatched = True
            return True


# ═════════════════════════════════════════════════════════════════════════════
# BROWSER SESSION — one per thread
# ═════════════════════════════════════════════════════════════════════════════

class BrowserSession:
    """Wraps a Playwright persistent browser context shared by all threads.

    The browser window is visible (``headless=False``) and uses a shared
    profile directory so cookies/logins persist.

    **Per-thread tab isolation**: Each agent thread (interactive chat or
    background task) gets its own tab within the single browser window.
    Operations target only the calling thread's tab, preventing cross-thread
    page flipping.  Tabs are auto-created on first use and cleaned up when
    a thread is deleted or a task run finishes.

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
        self._page_owners: dict[Any, str] = {}
        self._thread_pages: dict[str, Any] = {}  # thread_id → Page
        self._thread_pages_last_used: dict[str, float] = {}  # thread_id → monotonic ts
        self._browser_pid: int | None = None  # PID of the browser process
        self._launch_error: Exception | None = None  # Set if _pw_loop fails

        # Dedicated Playwright thread + work queue
        self._work_q: queue.Queue = queue.Queue()
        self._pw_thread: threading.Thread | None = None
        self._ready = threading.Event()   # set once PW is running
        self._activity_lock = threading.RLock()
        self._activity_by_thread: dict[str, dict[str, Any]] = {}
        self._activity_revision = 0
        self._activity_listeners: list[Any] = []
        self._preview_by_thread: dict[str, bytes] = {}
        self._preview_shielded: set[str] = set()

    # ── Local live-control state (never captures or drives the page) ──

    def add_activity_listener(self, callback: Any) -> Any:
        with self._activity_lock:
            self._activity_listeners.append(callback)
        return lambda: self._remove_activity_listener(callback)

    def _remove_activity_listener(self, callback: Any) -> None:
        with self._activity_lock:
            if callback in self._activity_listeners:
                self._activity_listeners.remove(callback)

    @staticmethod
    def _site_label(url: str) -> str:
        parsed = urlparse(str(url or ""))
        return str(parsed.hostname or "New tab")[:120]

    def _publish_activity(
        self,
        thread_id: str,
        *,
        state: str,
        action: str = "",
        page: Any = None,
        active: bool = True,
    ) -> None:
        """Publish metadata already available on the Playwright thread."""

        thread_id = str(thread_id or "default")
        with self._activity_lock:
            previous = dict(self._activity_by_thread.get(thread_id) or {})
            url = str(previous.get("url") or "")
            title = str(previous.get("title") or "")
            if page is not None:
                try:
                    url = str(page.url or "")
                except Exception:
                    pass
                try:
                    title = str(page.title() or "")[:160]
                except Exception:
                    pass
            if url and url != str(previous.get("url") or ""):
                self._preview_by_thread.pop(thread_id, None)
                self._preview_shielded.discard(thread_id)
            self._activity_revision += 1
            snapshot = {
                "engine": "browser",
                "active": bool(active),
                "paused": state == "waiting_user",
                "thread_id": thread_id,
                "state": str(state),
                "target": title or self._site_label(url),
                "site": self._site_label(url),
                "url": url,
                "last_action": str(action or previous.get("last_action") or "")[:160],
                "has_thumbnail": thread_id in self._preview_by_thread,
                "preview_shielded": thread_id in self._preview_shielded,
                "revision": self._activity_revision,
            }
            if active:
                self._activity_by_thread[thread_id] = snapshot
            else:
                self._activity_by_thread.pop(thread_id, None)
            listeners = list(self._activity_listeners)
        for callback in listeners:
            try:
                callback(dict(snapshot))
            except Exception:
                logger.debug("Browser live-control listener failed", exc_info=True)

    def status_snapshot(self, thread_id: str) -> dict[str, Any]:
        with self._activity_lock:
            snapshot = self._activity_by_thread.get(str(thread_id or "default"))
            if snapshot:
                return dict(snapshot)
            return {
                "engine": "browser",
                "active": False,
                "paused": False,
                "thread_id": str(thread_id or "default"),
                "state": "idle",
                "target": "",
                "site": "",
                "url": "",
                "last_action": "",
                "has_thumbnail": False,
                "preview_shielded": False,
                "revision": self._activity_revision,
            }

    def end_activity(self, thread_id: str, *, preserve_takeover: bool = False) -> None:
        snapshot = self.status_snapshot(thread_id)
        if preserve_takeover and snapshot.get("state") == "waiting_user":
            return
        with self._activity_lock:
            self._preview_by_thread.pop(str(thread_id or "default"), None)
            self._preview_shielded.discard(str(thread_id or "default"))
        self._publish_activity(thread_id, state="idle", active=False)

    def ephemeral_screenshot(self, thread_id: str) -> bytes | None:
        """Return the latest in-memory task-tab frame without capturing."""

        with self._activity_lock:
            return self._preview_by_thread.get(str(thread_id or "default"))

    def _set_preview_frame(
        self,
        thread_id: str,
        image: bytes | None,
        *,
        shielded: bool,
    ) -> None:
        thread_id = str(thread_id or "default")
        with self._activity_lock:
            if image:
                self._preview_by_thread[thread_id] = bytes(image)
            else:
                self._preview_by_thread.pop(thread_id, None)
            if shielded:
                self._preview_shielded.add(thread_id)
            else:
                self._preview_shielded.discard(thread_id)
            snapshot = dict(self._activity_by_thread.get(thread_id) or {})
        if snapshot.get("active"):
            self._publish_activity(
                thread_id,
                state=str(snapshot.get("state") or "observing"),
                action=str(snapshot.get("last_action") or ""),
            )

    def mark_waiting_approval(self, thread_id: str, action: str) -> None:
        self._publish_activity(
            thread_id,
            state="waiting_approval",
            action=action,
        )

    def _run_activity(self, thread_id: str, action: str, fn: Any) -> Any:
        self._publish_activity(thread_id, state="acting", action=action)
        try:
            return self._run_on_pw_thread(fn)
        except BaseException:
            self._publish_activity(thread_id, state="needs_attention", action=action)
            raise

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
                "headless": _browser_runs_headless(),
                "viewport": _VIEWPORT,
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
            }
            if channel:
                launch_kwargs["channel"] = channel
            else:
                from row_bot.mcp_client.requirements import playwright_browser_executable_path

                executable_path = playwright_browser_executable_path()
                if not executable_path:
                    raise RuntimeError(
                        "Playwright Chromium is not installed. Use Settings > System > Browser Automation > Install browser runtime."
                    )
                launch_kwargs["executable_path"] = executable_path

            self._context = self._pw.chromium.launch_persistent_context(**launch_kwargs)
            self._launched = True

            # Capture browser PID for targeted cleanup on crash
            try:
                self._browser_pid = self._context.browser.process.pid
            except Exception:
                self._browser_pid = None

            # Detect external browser close (user closes the window)
            def _on_close():
                logger.warning("Browser closed externally — marking session dead")
                self._launched = False
                activity_threads = list(self._activity_by_thread)
                self._thread_pages.clear()
                self._page_owners.clear()
                for activity_thread in activity_threads:
                    self.end_activity(activity_thread)
                # Push a sentinel so the work-queue loop exits cleanly
                try:
                    self._work_q.put(None)
                except Exception:
                    pass

            try:
                self._context.browser.on("disconnected", _on_close)
            except Exception:
                pass

            logger.info("Browser session launched (channel=%s, pid=%s)",
                        channel or "chromium", self._browser_pid)
            self._ready.set()
        except Exception as exc:
            msg = str(exc).lower()
            if "executable doesn't exist" in msg or "not installed" in msg:
                logger.warning("Chromium runtime missing; explicit installation is required")
            else:
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
            work = item
            if not work.begin_dispatch():
                continue
            try:
                result = work.fn()
                if not work.future.cancelled():
                    work.future.set_result(result)
            except Exception as exc:
                if not work.future.cancelled():
                    work.future.set_exception(exc)

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

        # Fail any remaining queued work items so callers don't hang
        while True:
            try:
                item = self._work_q.get_nowait()
            except queue.Empty:
                break
            if item is None:
                continue
            if not item.future.done():
                item.future.set_exception(RuntimeError("Browser session ended"))

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
        scope = current_cancellation_scope()
        if scope is not None and scope.is_cancelled():
            return "Browser action stopped by user."

        # Start (or restart) the PW thread — up to _MAX_RETRIES recovery attempts
        _MAX_RETRIES = 2
        if self._pw_thread is None or not self._pw_thread.is_alive():
            was_previous_crash = (self._pw_thread is not None
                                  and not self._launched)
            self._thread_pages.clear()  # stale after crash/restart
            self._page_owners.clear()
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
                    target=self._pw_loop, daemon=True, name="row-bot-pw"
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
        work = _BrowserWorkItem(fn=fn, future=future, scope=scope)
        self._work_q.put(work)
        unregister = None
        if scope is not None:
            unregister = scope.register(work.cancel, "browser_work.cancel")
        try:
            deadline = time.monotonic() + 120.0
            while True:
                if scope is not None and scope.is_cancelled():
                    work.cancel()
                    return "Browser action stopped by user."
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise concurrent.futures.TimeoutError()
                try:
                    return future.result(timeout=min(0.1, remaining))
                except concurrent.futures.TimeoutError:
                    continue
        except Exception as exc:
            # If browser was closed externally, the _on_close handler sets
            # _launched=False and pushes a sentinel.  Detect this and retry
            # once — the top of this method will see the dead PW thread and
            # enter the recovery path.
            _msg = str(exc).lower()
            if ("has been closed" in _msg or "target page" in _msg
                    or "browser has been closed" in _msg):
                logger.warning("Browser closed mid-operation — will restart")
                self._launched = False
                self._closed = False  # allow re-launch
                self._thread_pages.clear()
                # Wait for PW thread to exit (sentinel was already pushed)
                if self._pw_thread and self._pw_thread.is_alive():
                    self._pw_thread.join(timeout=10)
                return self._run_on_pw_thread(fn)  # retry once
            raise
        finally:
            if unregister is not None:
                unregister()

    # ── Lifecycle ────────────────────────────────────────────────────────

    _BLANK_URLS = frozenset({"", "about:blank", "chrome://newtab/",
                              "edge://newtab/"})

    def _get_page_for_thread(self, thread_id: str):
        """Return the page owned by *thread_id*, creating one if needed.

        MUST be called from the PW thread (i.e. inside a lambda passed to
        ``_run_on_pw_thread``).  On first call for a thread, reuses the
        initial blank tab if unowned, otherwise opens a new tab.  Only
        blank tabs are eligible for claiming — pages that already have
        content belong to another context and should not be reused.

        Does NOT call ``bring_to_front()`` — callers that need the tab
        visible (e.g. ``navigate``) should do that explicitly.
        """
        # Check if thread already has a live page
        page = self._thread_pages.get(thread_id)
        if page is not None:
            try:
                if not page.is_closed():
                    self._thread_pages_last_used[thread_id] = time.monotonic()
                    return page
            except Exception:
                pass
            # Page was closed externally — remove stale entry
            self._thread_pages.pop(thread_id, None)
            self._thread_pages_last_used.pop(thread_id, None)

        # Claim an unowned *blank* page if available
        owned_pages = set(self._page_owners)
        for p in self._context.pages:
            try:
                if (p not in owned_pages and not p.is_closed()
                        and p.url in self._BLANK_URLS):
                    self._thread_pages[thread_id] = p
                    self._page_owners[p] = thread_id
                    self._thread_pages_last_used[thread_id] = time.monotonic()
                    return p
            except Exception:
                continue

        # No blank unowned pages — open a new tab
        new_page = self._context.new_page()
        self._thread_pages[thread_id] = new_page
        self._page_owners[new_page] = thread_id
        self._thread_pages_last_used[thread_id] = time.monotonic()
        return new_page

    @property
    def page(self):
        """Return a page for the 'default' thread (backward compat).

        Used by ``take_screenshot`` and any legacy code that doesn't pass
        a thread_id.  MUST be called from the PW thread.
        """
        return self._get_page_for_thread("default")

    def get_page_for_screenshot(self, thread_id: str | None = None):
        """Return an existing page for screenshots — never creates a tab.

        Prefers the page owned by *thread_id* if given, otherwise returns
        the most recently created owned page, or the first open page.
        Returns ``None`` only if no pages exist at all.
        MUST be called from the PW thread.
        """
        if thread_id:
            page = self._thread_pages.get(thread_id)
            if page and not page.is_closed():
                return page
            return None
        # Fall back to any owned page (most recently added)
        for pg in reversed(list(self._thread_pages.values())):
            try:
                if not pg.is_closed():
                    return pg
            except Exception:
                continue
        # Last resort — any open page in the context
        for pg in self._context.pages:
            try:
                if not pg.is_closed():
                    return pg
            except Exception:
                continue
        return None

    def release_thread(self, thread_id: str) -> None:
        """Close the tab owned by *thread_id* (if any).

        Safe to call from any thread — the close runs on the PW thread.
        Does nothing if the thread has no tab or if it's the last tab
        (Playwright requires at least one page in the context).
        """
        if not self._launched or self._closed:
            self.end_activity(thread_id)
            return
        try:
            def _do():
                pages = [page for page, owner in list(self._page_owners.items()) if owner == thread_id]
                self._thread_pages.pop(thread_id, None)
                self._thread_pages_last_used.pop(thread_id, None)
                for page in pages:
                    self._page_owners.pop(page, None)
                    try:
                        if not page.is_closed() and len(self._context.pages) > 1:
                            page.close()
                    except Exception:
                        pass
            self._run_on_pw_thread(_do)
        except Exception:
            # Browser may be crashed / closed — just drop the mapping
            self._thread_pages.pop(thread_id, None)
            self._thread_pages_last_used.pop(thread_id, None)
            for page, owner in list(self._page_owners.items()):
                if owner == thread_id:
                    self._page_owners.pop(page, None)
        finally:
            self.end_activity(thread_id)

    def evict_idle(self, ttl_seconds: float = 600.0) -> int:
        """Close tabs untouched for longer than *ttl_seconds*.

        Tabs belonging to threads in ``ui.state._active_generations`` are
        always preserved — a running generation may take the screenshot
        minutes after its last navigate.  Returns the number of tabs
        closed.  Safe to call from any thread.
        """
        if not self._launched or self._closed:
            return 0
        try:
            from row_bot.ui.state import _active_generations
            active = set(_active_generations.keys())
        except Exception:
            active = set()
        cutoff = time.monotonic() - ttl_seconds
        to_close: list[str] = []
        for tid, last in list(self._thread_pages_last_used.items()):
            if tid in active:
                continue
            if last < cutoff:
                to_close.append(tid)
        for tid in to_close:
            self.release_thread(tid)
        return len(to_close)

    def close(self) -> None:
        """Shut down the browser and Playwright thread."""
        self._closed = True
        activity_threads = list(self._activity_by_thread)
        self._thread_pages.clear()
        self._page_owners.clear()
        self._thread_pages_last_used.clear()
        for activity_thread in activity_threads:
            self.end_activity(activity_thread)
        try:
            self._work_q.put(None)  # sentinel to exit _pw_loop
        except Exception:
            pass
        if self._pw_thread and self._pw_thread.is_alive():
            self._pw_thread.join(timeout=10)

    # ── Actions (called from any thread) ─────────────────────────────

    def current_url(self, thread_id: str = "default") -> str:
        """Return the calling thread's active URL without creating a tab."""

        if not self._launched or self._closed:
            return ""
        def _do():
            page = self._thread_pages.get(thread_id)
            return page.url if page is not None and not page.is_closed() else ""
        return self._run_on_pw_thread(_do)

    def bring_to_front(self, thread_id: str = "default") -> bool:
        """Bring only the task-owned tab forward without creating a tab."""

        if not self._launched or self._closed:
            return False

        def _do() -> bool:
            page = self._thread_pages.get(thread_id)
            if page is None or page.is_closed():
                return False
            page.bring_to_front()
            self._publish_activity(
                thread_id,
                state="waiting_user",
                action="You took over this tab",
                page=page,
            )
            return True

        return bool(self._run_on_pw_thread(_do))

    def take_over(self, thread_id: str = "default") -> bool:
        """Foreground the task-owned tab and mark automation as user-owned."""

        return self.bring_to_front(thread_id)

    def describe_ref(self, ref: int, thread_id: str = "default") -> dict[str, Any]:
        """Return minimal untrusted metadata used only to increase gating."""

        def _do():
            page = self._get_page_for_thread(thread_id)
            element = page.query_selector(f'[data-row-bot-ref="{ref}"]')
            if element is None:
                return {}
            return element.evaluate("""
                el => ({
                    tag: (el.tagName || '').toLowerCase(),
                    type: el.getAttribute('type') || '',
                    role: el.getAttribute('role') || '',
                    label: el.getAttribute('aria-label') || el.getAttribute('title') || el.getAttribute('placeholder') || '',
                    text: (el.innerText || '').trim().slice(0, 160),
                    href: el.getAttribute('href') || '',
                    download: el.hasAttribute('download'),
                    form_action: el.form ? (el.form.getAttribute('action') || '') : '',
                })
            """)
        return dict(self._run_on_pw_thread(_do) or {})

    def navigate(self, url: str, thread_id: str = "default") -> str:
        """Navigate to *url* and return snapshot."""
        def _do():
            page = self._get_page_for_thread(thread_id)
            page.bring_to_front()
            try:
                page.goto(url, wait_until="load", timeout=30000)
                try:
                    page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
            except Exception as exc:
                self._publish_activity(
                    thread_id,
                    state="needs_attention",
                    action="Open website",
                    page=page,
                )
                return f"Navigation failed: {exc}"
            snap = _take_snapshot(page)
            self._publish_activity(
                thread_id,
                state="observing",
                action="Opened website",
                page=page,
            )
            return _format_snapshot(snap)
        return self._run_activity(thread_id, "Open website", _do)

    def click(self, ref: int, thread_id: str = "default") -> str:
        """Click element by ref and return snapshot."""
        def _do():
            page = self._get_page_for_thread(thread_id)
            result = _click_ref(page, ref)
            snap = _take_snapshot(page)
            self._publish_activity(
                thread_id,
                state="observing",
                action="Clicked a page control",
                page=page,
            )
            return f"{result}\n\n{_format_snapshot(snap)}"
        return self._run_activity(thread_id, "Click page control", _do)

    def type_text(self, ref: int, text: str, submit: bool = False,
                  thread_id: str = "default") -> str:
        """Type into element by ref and return snapshot."""
        def _do():
            page = self._get_page_for_thread(thread_id)
            result = _type_ref(page, ref, text, submit)
            snap = _take_snapshot(page)
            self._publish_activity(
                thread_id,
                state="observing",
                action="Entered text (value hidden)",
                page=page,
            )
            return f"{result}\n\n{_format_snapshot(snap)}"
        return self._run_activity(thread_id, "Enter text (value hidden)", _do)

    def scroll(self, direction: str = "down", amount: int = 3,
               thread_id: str = "default") -> str:
        """Scroll the page and return snapshot."""
        def _do():
            page = self._get_page_for_thread(thread_id)
            delta = amount * 400
            if direction == "up":
                delta = -delta
            try:
                page.mouse.wheel(0, delta)
                page.wait_for_timeout(500)
            except Exception as exc:
                self._publish_activity(
                    thread_id,
                    state="needs_attention",
                    action="Scroll page",
                    page=page,
                )
                return f"Scroll failed: {exc}"
            snap = _take_snapshot(page)
            self._publish_activity(
                thread_id,
                state="observing",
                action="Scrolled page",
                page=page,
            )
            return _format_snapshot(snap)
        return self._run_activity(thread_id, "Scroll page", _do)

    def snapshot(self, thread_id: str = "default") -> str:
        """Take a fresh snapshot of the current page."""
        def _do():
            page = self._get_page_for_thread(thread_id)
            snap = _take_snapshot(page)
            self._publish_activity(
                thread_id,
                state="observing",
                action="Checked current page",
                page=page,
            )
            return _format_snapshot(snap)
        return self._run_activity(thread_id, "Check current page", _do)

    def go_back(self, thread_id: str = "default") -> str:
        """Go back one page and return snapshot."""
        def _do():
            page = self._get_page_for_thread(thread_id)
            try:
                page.go_back(wait_until="load", timeout=10000)
                try:
                    page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
            except Exception as exc:
                self._publish_activity(
                    thread_id,
                    state="needs_attention",
                    action="Go back",
                    page=page,
                )
                return f"Back navigation failed: {exc}"
            snap = _take_snapshot(page)
            self._publish_activity(
                thread_id,
                state="observing",
                action="Went back",
                page=page,
            )
            return _format_snapshot(snap)
        return self._run_activity(thread_id, "Go back", _do)

    def tab_action(self, action: str = "list", tab_id: int | None = None,
                   url: str | None = None, thread_id: str = "default") -> str:
        """Manage tabs: list, switch, new, close."""
        def _do():
            my_page = self._get_page_for_thread(thread_id)
            pages = [page for page, owner in self._page_owners.items() if owner == thread_id and not page.is_closed()]

            if action == "list":
                lines = [f"Open tabs ({len(pages)}):"]
                for i, pg in enumerate(pages):
                    marker = " ← active" if pg == my_page else ""
                    lines.append(f"  [{i}] {pg.url} — {pg.title()}{marker}")
                self._publish_activity(
                    thread_id,
                    state="observing",
                    action="Listed task tabs",
                    page=my_page,
                )
                return "\n".join(lines)

            elif action == "switch":
                if tab_id is None or tab_id < 0 or tab_id >= len(pages):
                    return f"Invalid tab_id. Use 0–{len(pages) - 1}."
                self._thread_pages[thread_id] = pages[tab_id]
                pages[tab_id].bring_to_front()
                snap = _take_snapshot(pages[tab_id])
                self._publish_activity(
                    thread_id,
                    state="observing",
                    action="Switched task tab",
                    page=pages[tab_id],
                )
                return f"Switched to tab [{tab_id}].\n\n{_format_snapshot(snap)}"

            elif action == "new":
                new_page = self._context.new_page()
                self._thread_pages[thread_id] = new_page
                self._page_owners[new_page] = thread_id
                new_page.bring_to_front()
                if url:
                    try:
                        new_page.goto(url, wait_until="load", timeout=30000)
                        try:
                            new_page.wait_for_load_state("networkidle", timeout=5000)
                        except Exception:
                            pass
                    except Exception as exc:
                        self._publish_activity(
                            thread_id,
                            state="needs_attention",
                            action="Open task tab",
                            page=new_page,
                        )
                        return f"New tab opened but navigation failed: {exc}"
                snap = _take_snapshot(new_page)
                self._publish_activity(
                    thread_id,
                    state="observing",
                    action="Opened task tab",
                    page=new_page,
                )
                return f"Opened new tab [{len(pages)}].\n\n{_format_snapshot(snap)}"

            elif action == "close":
                if tab_id is None or tab_id < 0 or tab_id >= len(pages):
                    return f"Invalid tab_id. Use 0–{len(pages) - 1}."
                if len(pages) <= 1:
                    return "Cannot close the last tab."
                closed_page = pages[tab_id]
                self._page_owners.pop(closed_page, None)
                if self._thread_pages.get(thread_id) == closed_page:
                    self._thread_pages.pop(thread_id, None)
                closed_page.close()
                remaining = [page for page, owner in self._page_owners.items() if owner == thread_id and not page.is_closed()]
                # Re-resolve calling thread's page
                active = self._get_page_for_thread(thread_id)
                snap = _take_snapshot(active)
                self._publish_activity(
                    thread_id,
                    state="observing",
                    action="Closed task tab",
                    page=active,
                )
                return f"Closed tab [{tab_id}]. {len(remaining)} tab(s) remaining.\n\n{_format_snapshot(snap)}"

            else:
                return f"Unknown tab action: {action}. Use list/switch/new/close."
        return self._run_activity(thread_id, f"Tab action: {action}", _do)

    def take_screenshot(self, thread_id: str | None = None) -> bytes | None:
        """Take a screenshot (PNG bytes) of the thread's page.

        If *thread_id* is given, screenshots that thread's tab.
        Otherwise falls back to the most recently used tab.
        Never creates a new tab.
        """
        if not self._launched or self._closed:
            return None
        try:
            def _do():
                page = self.get_page_for_screenshot(thread_id)
                if page is None:
                    return None, True
                try:
                    protected = page.query_selector(
                        'input[type="password"], input[autocomplete="one-time-code"], '
                        'input[autocomplete="cc-number"], input[autocomplete="cc-csc"]'
                    )
                    if protected is not None:
                        return None, True
                except Exception:
                    # A page we cannot inspect safely is not previewable.
                    return None, True
                return page.screenshot(type="png"), False
            image, shielded = self._run_on_pw_thread(_do)
            if thread_id is not None:
                self._set_preview_frame(thread_id, image, shielded=bool(shielded))
            return image
        except Exception:
            if thread_id is not None:
                self._set_preview_frame(thread_id, None, shielded=True)
            return None


# ═════════════════════════════════════════════════════════════════════════════
# BROWSER SESSION MANAGER — one session per thread
# ═════════════════════════════════════════════════════════════════════════════

class BrowserSessionManager:
    """Manages a **single shared** :class:`BrowserSession` for all threads.

    Only one Chromium instance can use a persistent profile directory at a
    time.  Rather than per-thread sessions (which would fight over the
    profile lock), every thread shares the same browser window.  Each
    thread gets its own tab within that window for isolation.
    """

    def __init__(self):
        self._shared_session: BrowserSession | None = None
        self._lock = threading.Lock()
        self._activity_listeners: list[Any] = []

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
                for callback in self._activity_listeners:
                    self._shared_session.add_activity_listener(callback)
            return self._shared_session

    def add_activity_listener(self, callback: Any) -> Any:
        """Listen for local metadata changes without creating a browser."""

        with self._lock:
            self._activity_listeners.append(callback)
            session = self._shared_session
            if session is not None:
                session.add_activity_listener(callback)

        def _remove() -> None:
            with self._lock:
                if callback in self._activity_listeners:
                    self._activity_listeners.remove(callback)
                current = self._shared_session
            if current is not None:
                current._remove_activity_listener(callback)

        return _remove

    def status_snapshot(self, thread_id: str) -> dict[str, Any]:
        with self._lock:
            session = self._shared_session
        if session is None:
            return {
                "engine": "browser",
                "active": False,
                "paused": False,
                "thread_id": str(thread_id or "default"),
                "state": "idle",
                "target": "",
                "site": "",
                "url": "",
                "last_action": "",
                "has_thumbnail": False,
                "preview_shielded": False,
                "revision": 0,
            }
        return session.status_snapshot(thread_id)

    def end_activity(self, thread_id: str, *, preserve_takeover: bool = False) -> None:
        with self._lock:
            session = self._shared_session
        if session is not None:
            session.end_activity(thread_id, preserve_takeover=preserve_takeover)

    def take_over(self, thread_id: str) -> bool:
        with self._lock:
            session = self._shared_session
        return bool(session and session.take_over(thread_id))

    def take_screenshot(self, thread_id: str) -> bytes | None:
        with self._lock:
            session = self._shared_session
        return session.take_screenshot(thread_id) if session is not None else None

    def ephemeral_screenshot(self, thread_id: str) -> bytes | None:
        with self._lock:
            session = self._shared_session
        return session.ephemeral_screenshot(thread_id) if session is not None else None

    def kill_session(self, thread_id: str) -> None:
        """Release the tab owned by *thread_id* (if any)."""
        with self._lock:
            session = self._shared_session
        if session is not None:
            session.release_thread(thread_id)

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
    with _history_lock:
        if _HISTORY_PATH.exists():
            try:
                return json.loads(_HISTORY_PATH.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return {}
    return {}


def _save_history(history: dict[str, list[dict]]) -> None:
    with _history_lock:
        tmp_path: pathlib.Path | None = None
        try:
            _HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=f"{_HISTORY_PATH.name}.", suffix=".tmp", dir=_HISTORY_PATH.parent)
            tmp_path = pathlib.Path(name)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(history, handle, default=str)
            tmp_path.replace(_HISTORY_PATH)
        except OSError:
            logger.warning("Failed to save browser history", exc_info=True)
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)


def get_browser_history(thread_id: str) -> list[dict]:
    """Get browser history entries for a thread."""
    return _load_history().get(thread_id, [])


def append_browser_history(thread_id: str, entry: dict) -> None:
    """Append a browser action entry to history for a thread."""
    with _history_lock:
        history = _load_history()
        history.setdefault(thread_id, []).append(entry)
        _save_history(history)


def clear_browser_history(thread_id: str) -> None:
    """Clear browser history for a thread."""
    with _history_lock:
        history = _load_history()
        if thread_id in history:
            del history[thread_id]
            _save_history(history)


# ═════════════════════════════════════════════════════════════════════════════
# HELPER — thread ID resolution
# ═════════════════════════════════════════════════════════════════════════════

def _get_thread_id() -> str:
    """Get the current thread ID from the agent context."""
    try:
        from row_bot.agent import _current_thread_id_var
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
            "Automate a real browser window for web tasks. "
            "Navigate websites, click buttons, fill forms, read web page content. "
            "Uses a persistent profile so logins and cookies are preserved."
        )

    @property
    def enabled_by_default(self) -> bool:
        return True

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
            # Security: reject javascript: URLs
            if url.strip().lower().startswith("javascript:"):
                return "Error: javascript: URLs are not allowed for security reasons."
            if not url.strip().lower().startswith(("http://", "https://")):
                url = "https://" + url

            thread_id = _get_thread_id()
            session = _session_manager.get_session(thread_id)
            policy, reason = _navigation_policy(url, session.current_url(thread_id))
            if policy == "block":
                return f"BLOCKED: {reason}"
            if policy == "ask":
                from row_bot.tools.approval_gate import gate_action

                session.mark_waiting_approval(thread_id, "Approve website navigation")
                blocked = gate_action({
                    "tool": "browser_navigate",
                    "label": "Browser navigation",
                    "action": "navigate",
                    "origin_and_path": _history_url(url),
                    "reason": reason,
                })
                if blocked:
                    session.end_activity(thread_id)
                    return blocked
            result = session.navigate(url, thread_id)

            # Persist to history
            append_browser_history(thread_id, {
                "action": "navigate",
                "url": _history_url(url),
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
            thread_id = _get_thread_id()
            session = _session_manager.get_session(thread_id)
            metadata = session.describe_ref(ref, thread_id)
            if not metadata:
                return f"Error: element ref [{ref}] is stale. Take a new snapshot."
            consequence = _consequential_browser_target(metadata)
            if consequence:
                from row_bot.tools.approval_gate import gate_action

                session.mark_waiting_approval(thread_id, "Approve page action")
                blocked = gate_action({
                    "tool": "browser_click",
                    "label": "Browser consequential action",
                    "action": "click",
                    "target": str(metadata.get("label") or metadata.get("text") or "control")[:160],
                    "reason": consequence,
                })
                if blocked:
                    session.end_activity(thread_id)
                    return blocked
                if session.describe_ref(ref, thread_id) != metadata:
                    session.end_activity(thread_id)
                    return "Error: browser target changed while awaiting approval; take a new snapshot and approve again."
            result = session.click(ref, thread_id)

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
            thread_id = _get_thread_id()
            session = _session_manager.get_session(thread_id)
            metadata = session.describe_ref(ref, thread_id)
            if not metadata:
                return f"Error: element ref [{ref}] is stale. Take a new snapshot."
            consequence = _consequential_browser_target(metadata, submit=submit)
            if consequence:
                from row_bot.tools.approval_gate import gate_action

                session.mark_waiting_approval(thread_id, "Approve form action")
                blocked = gate_action({
                    "tool": "browser_type",
                    "label": "Browser form action",
                    "action": "type_and_submit" if submit else "type",
                    "target": str(metadata.get("label") or metadata.get("text") or "field")[:160],
                    "reason": consequence,
                    "data_summary": f"Text entry ({len(text)} characters; value hidden)",
                })
                if blocked:
                    session.end_activity(thread_id)
                    return blocked
                if session.describe_ref(ref, thread_id) != metadata:
                    session.end_activity(thread_id)
                    return "Error: browser target changed while awaiting approval; take a new snapshot and approve again."
            result = session.type_text(ref, text, submit, thread_id)

            append_browser_history(thread_id, {
                "action": "type",
                "ref": ref,
                "text_length": len(text),
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
            thread_id = _get_thread_id()
            session = _session_manager.get_session(thread_id)
            result = session.scroll(direction, amount, thread_id)

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
            thread_id = _get_thread_id()
            session = _session_manager.get_session(thread_id)
            return session.snapshot(thread_id)

        # ── Back ─────────────────────────────────────────────────────────

        def browser_back() -> str:
            """Go back to the previous page (like pressing the Back button).

            Returns a fresh snapshot of the page after going back.
            """
            thread_id = _get_thread_id()
            session = _session_manager.get_session(thread_id)
            result = session.go_back(thread_id)

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
            # Validate URL for new tab
            if action == "new" and url:
                if url.strip().lower().startswith("javascript:"):
                    return "Error: javascript: URLs are not allowed for security reasons."
                if not url.strip().lower().startswith(("http://", "https://")):
                    url = "https://" + url

            thread_id = _get_thread_id()
            session = _session_manager.get_session(thread_id)
            if action == "new" and url:
                policy, reason = _navigation_policy(url, session.current_url(thread_id))
                if policy == "block":
                    return f"BLOCKED: {reason}"
                if policy == "ask":
                    from row_bot.tools.approval_gate import gate_action

                    session.mark_waiting_approval(thread_id, "Approve new task tab")
                    blocked = gate_action({
                        "tool": "browser_tab",
                        "label": "Browser new-tab navigation",
                        "action": "new_tab",
                        "origin_and_path": _history_url(url),
                        "reason": reason,
                    })
                    if blocked:
                        session.end_activity(thread_id)
                        return blocked
            result = session.tab_action(action, tab_id, url, thread_id)

            append_browser_history(thread_id, {
                "action": f"tab_{action}",
                "tab_id": tab_id,
                "url": _history_url(url) if url else None,
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
                    "Refresh the current browser page's interactive elements "
                    "and ref numbers. Use when refs may be stale after user "
                    "interaction."
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
