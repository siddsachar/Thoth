"""Managed Browser service: Playwright lifecycle, task pages, actions, recovery."""

from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import platform
import queue
import shutil
import threading
import time
from typing import Any
from urllib.parse import urlparse

from row_bot.automation.contracts import (
    ActionReceipt,
    AutomationSurface,
    sanitize_automation_error,
)
from row_bot.browser.observation import BrowserObservationRegistry, StaleBrowserObservation
from row_bot.browser.policy import history_url
from row_bot.browser.runtime import (
    check_managed_browser_runtime,
    check_packaged_browser_runtime,
    ensure_profile_engine,
)
from row_bot.cancellation import current_cancellation_scope
from row_bot.data_paths import get_row_bot_data_dir


logger = logging.getLogger(__name__)
PROFILE_DIR = get_row_bot_data_dir() / "browser_profile"
VIEWPORT = {"width": 1280, "height": 900}


def browser_runs_headless() -> bool:
    return str(os.environ.get("ROW_BOT_BROWSER_HEADLESS") or "").strip().casefold() in {"1", "true", "yes", "on"}


def _installed_channel() -> str | None:
    """Read-only discovery; never launches a probe browser."""

    requested = str(os.environ.get("ROW_BOT_BROWSER_CHANNEL") or "").strip().casefold()
    if requested in {"chrome", "msedge"}:
        return requested
    system = platform.system().casefold()
    candidates: tuple[tuple[str, str], ...]
    if system == "windows":
        candidates = (
            ("chrome", r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            ("chrome", r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
            ("msedge", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        )
    elif system == "darwin":
        candidates = (("chrome", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),)
    else:
        candidates = (("chrome", shutil.which("google-chrome") or ""),)
    return next((channel for channel, path in candidates if path and Path(path).is_file()), None)


@dataclass
class BrowserWorkItem:
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


class ManagedBrowserService:
    """One persistent Row-Bot context with exact per-task page ownership."""

    _BLANK_URLS = frozenset({"", "about:blank", "chrome://newtab/", "edge://newtab/"})

    def __init__(self) -> None:
        self._pw = None
        self._context = None
        self._launched = False
        self._closed = False
        self._launch_error: BaseException | None = None
        self._context_generation = 0
        self._page_sequence = 0
        self._page_identities: dict[Any, str] = {}
        self._navigation_generations: dict[Any, int] = {}
        self._page_owners: dict[Any, str] = {}
        self._thread_pages: dict[str, Any] = {}
        self._thread_pages_last_used: dict[str, float] = {}
        self._work_q: queue.Queue = queue.Queue()
        self._pw_thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._observations = BrowserObservationRegistry()
        self._activity_lock = threading.RLock()
        self._activity_by_thread: dict[str, dict[str, Any]] = {}
        self._activity_revision = 0
        self._activity_listeners: list[Any] = []
        self._preview_by_thread: dict[str, bytes] = {}
        self._preview_shielded: set[str] = set()
        self.browser_tool_calls = 0
        self._preview_capture_count = 0

    def performance_snapshot(self) -> dict[str, int]:
        return {
            "browser_tool_calls": self.browser_tool_calls,
            "semantic_observations": self._observations.observation_count,
            "preview_captures": self._preview_capture_count,
            "vision_calls": 0,
        }

    def _page_identity(self, page: Any) -> str:
        identity = self._page_identities.get(page)
        if identity is None:
            self._page_sequence += 1
            identity = f"page-{self._page_sequence}"
            self._page_identities[page] = identity
            self._navigation_generations.setdefault(page, 0)
        return identity

    def _invalidate(self, thread_id: str) -> None:
        self._observations.invalidate(thread_id)

    def _invalidate_all(self) -> None:
        self._observations.invalidate_all()

    def _bump_navigation(self, page: Any) -> None:
        self._navigation_generations[page] = self._navigation_generations.get(page, 0) + 1
        owner = self._page_owners.get(page)
        if owner:
            self._invalidate(owner)

    def _observe(self, page: Any, thread_id: str) -> str:
        try:
            observation = self._observations.observe(
                page,
                task_id=thread_id,
                page_identity=self._page_identity(page),
                navigation_generation=self._navigation_generations.get(page, 0),
                context_generation=self._context_generation,
            )
            return self._observations.format(observation)
        except AttributeError:
            # Compatibility for small deterministic fakes that model only tabs.
            return f"URL: {str(getattr(page, 'url', '') or '')}\nTitle: {self._safe_title(page)}\nInteractive elements (0):"

    @staticmethod
    def _safe_title(page: Any) -> str:
        try:
            return str(page.title() or "")[:160]
        except Exception:
            return ""

    @staticmethod
    def _dom_signature(page: Any) -> tuple[int, int, int]:
        """Cheap non-content change signal; never serializes values or page text."""

        try:
            value = page.evaluate(
                """() => [
                  document.querySelectorAll('a[href],button,input,textarea,select,[role]').length,
                  document.querySelectorAll('dialog[open],[role="dialog"],[role="menu"]').length,
                  document.body ? document.body.childElementCount : 0
                ]"""
            )
            if isinstance(value, list) and len(value) == 3:
                return tuple(max(0, int(item)) for item in value)
        except Exception:
            pass
        return (0, 0, 0)

    def _resolve(self, token: str, thread_id: str):
        page = self._get_page_for_thread(thread_id)
        target = self._observations.resolve(
            str(token),
            task_id=thread_id,
            page_identity=self._page_identity(page),
            navigation_generation=self._navigation_generations.get(page, 0),
            context_generation=self._context_generation,
        )
        return page, target

    @staticmethod
    def _error(action: str, exc: BaseException | str) -> str:
        error = sanitize_automation_error(AutomationSurface.BROWSER, action, exc)
        return f"ERROR [{error.code}]: {error.remediation}"

    @staticmethod
    def _receipt(action: str, revision: int, *, effect: str = "confirmed", verified: bool | None = None) -> str:
        receipt = ActionReceipt(
            surface=AutomationSurface.BROWSER,
            target_id="managed-page",
            action_family=action,
            revision=revision,
            backend_effect=effect,
            route="python-playwright",
            delivery="exact_handle",
            verified_outcome=verified,
        )
        return "Action receipt: " + json.dumps(receipt.to_dict(compatibility=False), sort_keys=True)

    # ---- Local metadata/live control -------------------------------------------------

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
        return str(urlparse(str(url or "")).hostname or "New tab")[:120]

    def _publish_activity(self, thread_id: str, *, state: str, action: str = "", page: Any = None, active: bool = True) -> None:
        thread_id = str(thread_id or "default")
        with self._activity_lock:
            previous = dict(self._activity_by_thread.get(thread_id) or {})
            url = str(previous.get("url") or "")
            title = str(previous.get("title") or "")
            if page is not None:
                url = str(getattr(page, "url", "") or "")
                title = self._safe_title(page)
            if url != str(previous.get("url") or ""):
                self._preview_by_thread.pop(thread_id, None)
                self._preview_shielded.discard(thread_id)
            self._activity_revision += 1
            value = {
                "engine": "browser", "surface": "browser", "active": bool(active),
                "paused": state == "waiting_user", "thread_id": thread_id, "state": state,
                "target": title or self._site_label(url), "site": self._site_label(url), "url": url,
                "last_action": str(action or previous.get("last_action") or "")[:160],
                "has_thumbnail": thread_id in self._preview_by_thread,
                "preview_shielded": thread_id in self._preview_shielded,
                "revision": self._activity_revision,
            }
            if active:
                self._activity_by_thread[thread_id] = value
            else:
                self._activity_by_thread.pop(thread_id, None)
            listeners = list(self._activity_listeners)
        for callback in listeners:
            try:
                callback(dict(value))
            except Exception:
                logger.debug("Browser activity listener failed", exc_info=True)

    def status_snapshot(self, thread_id: str) -> dict[str, Any]:
        with self._activity_lock:
            value = self._activity_by_thread.get(str(thread_id or "default"))
            if value:
                return dict(value)
            return {
                "engine": "browser", "surface": "browser", "active": False, "paused": False,
                "thread_id": str(thread_id or "default"), "state": "idle", "target": "", "site": "",
                "url": "", "last_action": "", "has_thumbnail": False, "preview_shielded": False,
                "revision": self._activity_revision,
            }

    def end_activity(self, thread_id: str, *, preserve_takeover: bool = False) -> None:
        if preserve_takeover and self.status_snapshot(thread_id).get("state") == "waiting_user":
            return
        with self._activity_lock:
            self._preview_by_thread.pop(str(thread_id or "default"), None)
            self._preview_shielded.discard(str(thread_id or "default"))
        self._publish_activity(thread_id, state="idle", active=False)

    def ephemeral_screenshot(self, thread_id: str) -> bytes | None:
        with self._activity_lock:
            return self._preview_by_thread.get(str(thread_id or "default"))

    def _set_preview_frame(self, thread_id: str, image: bytes | None, *, shielded: bool) -> None:
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
            self._publish_activity(thread_id, state=str(snapshot.get("state") or "observing"), action=str(snapshot.get("last_action") or ""))

    def mark_waiting_approval(self, thread_id: str, action: str) -> None:
        self._invalidate(thread_id)
        self._publish_activity(thread_id, state="waiting_approval", action=action)

    def _run_activity(self, thread_id: str, action: str, fn: Any) -> Any:
        self.browser_tool_calls += 1
        self._publish_activity(thread_id, state="acting", action=action)
        try:
            result = self._run_on_pw_thread(fn)
            if result == "Browser action stopped by user.":
                self._invalidate(thread_id)
            return result
        except BaseException as exc:
            self._publish_activity(thread_id, state="needs_attention", action=action)
            return self._error(action, exc)

    # ---- Playwright lifecycle ---------------------------------------------------------

    def _launch_context(self) -> None:
        from playwright.sync_api import sync_playwright

        ensure_profile_engine(PROFILE_DIR)
        self._pw = sync_playwright().start()
        common: dict[str, Any] = {
            "user_data_dir": str(PROFILE_DIR), "headless": browser_runs_headless(),
            "viewport": VIEWPORT,
            "args": ["--disable-blink-features=AutomationControlled", "--no-first-run", "--no-default-browser-check"],
        }
        channel = _installed_channel()
        attempts: list[dict[str, Any]] = []
        if channel:
            attempts.append({**common, "channel": channel})
        readiness = check_packaged_browser_runtime()
        if not readiness.ready:
            readiness = check_managed_browser_runtime()
        if readiness.ready:
            attempts.append({**common, "executable_path": readiness.executable_path})
        if not attempts:
            raise RuntimeError("Managed Chromium is not installed; use the explicit Browser Automation installer.")
        last: BaseException | None = None
        for launch_kwargs in attempts[:2]:
            try:
                self._context = self._pw.chromium.launch_persistent_context(**launch_kwargs)
                break
            except BaseException as exc:
                last = exc
        if self._context is None:
            raise RuntimeError("No reviewed browser runtime could be launched.") from last
        self._context_generation += 1
        self._launched = True
        self._register_context_handlers()

    def _register_context_handlers(self) -> None:
        def on_page(page: Any) -> None:
            owner = None
            try:
                opener = page.opener()
                owner = self._page_owners.get(opener)
            except Exception:
                owner = None
            if owner:
                self._page_owners[page] = owner
                self._thread_pages[owner] = page
                self._page_identity(page)
                self._invalidate(owner)

        try:
            self._context.on("page", on_page)
        except Exception:
            pass
        try:
            self._context.browser.on("disconnected", self._on_disconnected)
        except Exception:
            pass

    def _on_disconnected(self, *_args: Any) -> None:
        self._launched = False
        self._invalidate_all()
        self._thread_pages.clear()
        self._page_owners.clear()
        self._page_identities.clear()
        self._navigation_generations.clear()
        with self._activity_lock:
            self._preview_by_thread.clear()
            self._preview_shielded.clear()
        try:
            self._work_q.put(None)
        except Exception:
            pass

    def _pw_loop(self) -> None:
        self._launch_error = None
        try:
            self._launch_context()
            self._ready.set()
        except BaseException as exc:
            self._launch_error = exc
            self._ready.set()
            try:
                if self._pw:
                    self._pw.stop()
            except Exception:
                pass
            self._pw = None
            self._context = None
            return
        while True:
            item = self._work_q.get()
            if item is None:
                break
            if not item.begin_dispatch():
                continue
            try:
                result = item.fn()
                if not item.future.cancelled():
                    item.future.set_result(result)
            except BaseException as exc:
                if not item.future.cancelled():
                    item.future.set_exception(exc)
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

    def _start(self) -> None:
        if self._pw_thread is not None and self._pw_thread.is_alive() and self._launched:
            return
        if self._pw_thread is not None and self._pw_thread.is_alive():
            self._pw_thread.join(timeout=10)
        self._invalidate_all()
        self._thread_pages.clear()
        self._page_owners.clear()
        self._ready.clear()
        self._work_q = queue.Queue()
        self._pw_thread = threading.Thread(target=self._pw_loop, daemon=True, name="row-bot-pw")
        self._pw_thread.start()
        if not self._ready.wait(timeout=60) or not self._launched:
            raise RuntimeError("Browser failed to launch.") from self._launch_error

    def _run_on_pw_thread(self, fn: Any) -> Any:
        if self._closed:
            raise RuntimeError("BrowserSession is closed")
        scope = current_cancellation_scope()
        if scope is not None and scope.is_cancelled():
            return "Browser action stopped by user."
        self._start()
        future: concurrent.futures.Future = concurrent.futures.Future()
        work = BrowserWorkItem(fn, future, scope)
        self._work_q.put(work)
        unregister = scope.register(work.cancel, "browser_work.cancel") if scope is not None else None
        try:
            deadline = time.monotonic() + 120
            while True:
                if scope is not None and scope.is_cancelled():
                    work.cancel()
                    return "Browser action stopped by user."
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("browser work timeout")
                try:
                    return future.result(timeout=min(0.1, remaining))
                except concurrent.futures.TimeoutError:
                    continue
        finally:
            if unregister:
                unregister()

    # ---- Task page ownership ----------------------------------------------------------

    def _get_page_for_thread(self, thread_id: str):
        page = self._thread_pages.get(thread_id)
        if page is not None:
            try:
                if not page.is_closed():
                    self._thread_pages_last_used[thread_id] = time.monotonic()
                    return page
            except Exception:
                pass
            self._thread_pages.pop(thread_id, None)
            self._invalidate(thread_id)
        for candidate in self._context.pages:
            try:
                if candidate not in self._page_owners and not candidate.is_closed() and candidate.url in self._BLANK_URLS:
                    self._thread_pages[thread_id] = candidate
                    self._page_owners[candidate] = thread_id
                    self._thread_pages_last_used[thread_id] = time.monotonic()
                    self._page_identity(candidate)
                    return candidate
            except Exception:
                continue
        page = self._context.new_page()
        self._thread_pages[thread_id] = page
        self._page_owners[page] = thread_id
        self._thread_pages_last_used[thread_id] = time.monotonic()
        self._page_identity(page)
        return page

    @property
    def page(self):
        return self._get_page_for_thread("default")

    def get_page_for_screenshot(self, thread_id: str | None = None):
        if thread_id:
            page = self._thread_pages.get(thread_id)
            return page if page is not None and not page.is_closed() else None
        for page in reversed(list(self._thread_pages.values())):
            try:
                if not page.is_closed():
                    return page
            except Exception:
                pass
        return None

    def release_thread(self, thread_id: str) -> None:
        self._invalidate(thread_id)
        if not self._launched or self._closed:
            self.end_activity(thread_id)
            return
        def release() -> None:
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
        try:
            self._run_on_pw_thread(release)
        except Exception:
            pass
        self.end_activity(thread_id)

    def evict_idle(self, ttl_seconds: float = 600.0) -> int:
        if not self._launched or self._closed:
            return 0
        try:
            from row_bot.ui.state import _active_generations
            active = set(_active_generations)
        except Exception:
            active = set()
        cutoff = time.monotonic() - ttl_seconds
        expired = [task for task, used in self._thread_pages_last_used.items() if task not in active and used < cutoff]
        for task in expired:
            self.release_thread(task)
        return len(expired)

    def close(self) -> None:
        self._closed = True
        self._invalidate_all()
        for task in list(self._activity_by_thread):
            self.end_activity(task)
        try:
            self._work_q.put(None)
        except Exception:
            pass
        if self._pw_thread and self._pw_thread.is_alive():
            self._pw_thread.join(timeout=10)
        self._thread_pages.clear()
        self._page_owners.clear()
        self._page_identities.clear()
        self._navigation_generations.clear()

    # ---- Public actions ---------------------------------------------------------------

    def current_url(self, thread_id: str = "default") -> str:
        if not self._launched or self._closed:
            return ""
        return str(self._run_on_pw_thread(lambda: getattr(self._thread_pages.get(thread_id), "url", "")))

    def bring_to_front(self, thread_id: str = "default") -> bool:
        if not self._launched or self._closed:
            return False
        def front() -> bool:
            page = self._thread_pages.get(thread_id)
            if page is None or page.is_closed():
                return False
            self._invalidate(thread_id)
            page.bring_to_front()
            self._publish_activity(thread_id, state="waiting_user", action="You took over this tab", page=page)
            return True
        return bool(self._run_on_pw_thread(front))

    take_over = bring_to_front

    def describe_ref(self, ref: str, thread_id: str = "default") -> dict[str, Any]:
        def describe() -> dict[str, Any]:
            try:
                _page, target = self._resolve(str(ref), thread_id)
                return dict(target.metadata)
            except StaleBrowserObservation:
                return {}
        return dict(self._run_on_pw_thread(describe) or {})

    def navigate(self, url: str, thread_id: str = "default") -> str:
        def action() -> str:
            page = self._get_page_for_thread(thread_id)
            page.bring_to_front()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                self._bump_navigation(page)
                result = self._observe(page, thread_id)
                self._publish_activity(thread_id, state="observing", action="Opened website", page=page)
                return result
            except BaseException as exc:
                return self._error("navigate", exc)
        return self._run_activity(thread_id, "Open website", action)

    def click(self, ref: str, thread_id: str = "default") -> str:
        def action() -> str:
            try:
                page, target = self._resolve(str(ref), thread_id)
            except BaseException as exc:
                return self._error("click", exc)
            observation = self._observations.current(thread_id)
            revision = observation.revision if observation else 0
            old_url = str(getattr(page, "url", "") or "")
            old_pages = len(self._context.pages)
            old_dom = self._dom_signature(page)
            try:
                target.handle.click(timeout=5_000)
            except BaseException as exc:
                self._invalidate(thread_id)
                return self._error("click", exc)
            self._invalidate(thread_id)
            changed = (
                old_url != str(getattr(page, "url", "") or "")
                or len(self._context.pages) != old_pages
                or self._dom_signature(page) != old_dom
            )
            if changed:
                self._bump_navigation(page)
                result = self._observe(self._thread_pages.get(thread_id, page), thread_id)
            else:
                result = self._receipt("click", revision)
            self._publish_activity(thread_id, state="observing", action="Clicked a page control", page=page)
            return result
        return self._run_activity(thread_id, "Click page control", action)

    def type_text(self, ref: str, text: str, submit: bool = False, thread_id: str = "default") -> str:
        def action() -> str:
            try:
                page, target = self._resolve(str(ref), thread_id)
            except BaseException as exc:
                return self._error("type", exc)
            observation = self._observations.current(thread_id)
            revision = observation.revision if observation else 0
            try:
                target.handle.fill(text, timeout=5_000)
                if submit:
                    target.handle.press("Enter", timeout=5_000)
            except BaseException as exc:
                self._invalidate(thread_id)
                return self._error("type", exc)
            self._invalidate(thread_id)
            if submit:
                self._bump_navigation(page)
                result = self._observe(self._thread_pages.get(thread_id, page), thread_id)
            else:
                result = self._receipt("type", revision)
            self._publish_activity(thread_id, state="observing", action="Entered text (value hidden)", page=page)
            return result
        return self._run_activity(thread_id, "Enter text (value hidden)", action)

    def scroll(self, direction: str = "down", amount: int = 3, thread_id: str = "default") -> str:
        def action() -> str:
            page = self._get_page_for_thread(thread_id)
            try:
                page.mouse.wheel(0, (-1 if direction == "up" else 1) * max(1, min(int(amount), 20)) * 400)
            except BaseException as exc:
                return self._error("scroll", exc)
            self._invalidate(thread_id)
            result = self._observe(page, thread_id)
            self._publish_activity(thread_id, state="observing", action="Scrolled page", page=page)
            return result
        return self._run_activity(thread_id, "Scroll page", action)

    def snapshot(self, thread_id: str = "default") -> str:
        def action() -> str:
            page = self._get_page_for_thread(thread_id)
            result = self._observe(page, thread_id)
            self._publish_activity(thread_id, state="observing", action="Checked current page", page=page)
            return result
        return self._run_activity(thread_id, "Check current page", action)

    def go_back(self, thread_id: str = "default") -> str:
        def action() -> str:
            page = self._get_page_for_thread(thread_id)
            try:
                page.go_back(wait_until="domcontentloaded", timeout=10_000)
            except BaseException as exc:
                return self._error("back", exc)
            self._bump_navigation(page)
            result = self._observe(page, thread_id)
            self._publish_activity(thread_id, state="observing", action="Went back", page=page)
            return result
        return self._run_activity(thread_id, "Go back", action)

    def tab_action(self, action: str = "list", tab_id: int | None = None, url: str | None = None, thread_id: str = "default") -> str:
        def perform() -> str:
            current = self._get_page_for_thread(thread_id)
            pages = [page for page, owner in self._page_owners.items() if owner == thread_id and not page.is_closed()]
            if action == "list":
                lines = [f"Open tabs ({len(pages)}):"]
                for index, page in enumerate(pages):
                    marker = " <- active" if page == current else ""
                    lines.append(f"  [{index}] {history_url(str(page.url))[:2048]} - {self._safe_title(page)}{marker}")
                return "\n".join(lines)
            if action in {"switch", "close"} and (tab_id is None or tab_id < 0 or tab_id >= len(pages)):
                return f"Invalid tab_id. Use 0-{len(pages) - 1}."
            if action == "switch":
                selected = pages[int(tab_id)]
                self._thread_pages[thread_id] = selected
                self._invalidate(thread_id)
                selected.bring_to_front()
                return f"Switched to tab [{tab_id}].\n\n{self._observe(selected, thread_id)}"
            if action == "new":
                page = self._context.new_page()
                self._page_owners[page] = thread_id
                self._thread_pages[thread_id] = page
                self._page_identity(page)
                self._invalidate(thread_id)
                page.bring_to_front()
                if url:
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                        self._bump_navigation(page)
                    except BaseException as exc:
                        return self._error("new_tab", exc)
                return f"Opened new tab [{len(pages)}].\n\n{self._observe(page, thread_id)}"
            if action == "close":
                if len(pages) <= 1:
                    return "Cannot close the last tab."
                selected = pages[int(tab_id)]
                self._page_owners.pop(selected, None)
                if self._thread_pages.get(thread_id) == selected:
                    self._thread_pages.pop(thread_id, None)
                self._invalidate(thread_id)
                selected.close()
                active = self._get_page_for_thread(thread_id)
                return f"Closed tab [{tab_id}].\n\n{self._observe(active, thread_id)}"
            return "Unknown tab action. Use list/switch/new/close."
        return self._run_activity(thread_id, f"Tab action: {action}", perform)

    def take_screenshot(self, thread_id: str | None = None) -> bytes | None:
        """UI preview only; actions and observations never call this method."""

        if not self._launched or self._closed:
            return None
        try:
            def capture():
                page = self.get_page_for_screenshot(thread_id)
                if page is None:
                    return None, True
                protected = page.query_selector(
                    'input[type="password"], input[autocomplete="one-time-code"], '
                    'input[autocomplete="cc-number"], input[autocomplete="cc-csc"]'
                )
                if protected is not None:
                    return None, True
                self._preview_capture_count += 1
                return page.screenshot(type="png"), False
            image, shielded = self._run_on_pw_thread(capture)
            if thread_id is not None:
                self._set_preview_frame(thread_id, image, shielded=bool(shielded))
            return image
        except Exception:
            if thread_id is not None:
                self._set_preview_frame(thread_id, None, shielded=True)
            return None


BrowserSession = ManagedBrowserService


class BrowserSessionManager:
    def __init__(self) -> None:
        self._shared_session: BrowserSession | None = None
        self._lock = threading.Lock()
        self._activity_listeners: list[Any] = []

    @property
    def _sessions(self) -> dict[str, BrowserSession]:
        return {"__shared__": self._shared_session} if self._shared_session else {}

    def has_active_session(self) -> bool:
        return self._shared_session is not None

    def get_session(self, thread_id: str = "") -> BrowserSession:
        with self._lock:
            if self._shared_session is None:
                self._shared_session = BrowserSession()
                for listener in self._activity_listeners:
                    self._shared_session.add_activity_listener(listener)
            return self._shared_session

    def add_activity_listener(self, callback: Any) -> Any:
        with self._lock:
            self._activity_listeners.append(callback)
            session = self._shared_session
            if session:
                session.add_activity_listener(callback)
        def remove() -> None:
            with self._lock:
                if callback in self._activity_listeners:
                    self._activity_listeners.remove(callback)
                current = self._shared_session
            if current:
                current._remove_activity_listener(callback)
        return remove

    def status_snapshot(self, thread_id: str) -> dict[str, Any]:
        with self._lock:
            session = self._shared_session
        return session.status_snapshot(thread_id) if session else BrowserSession().status_snapshot(thread_id)

    def end_activity(self, thread_id: str, *, preserve_takeover: bool = False) -> None:
        with self._lock:
            session = self._shared_session
        if session:
            session.end_activity(thread_id, preserve_takeover=preserve_takeover)

    def take_over(self, thread_id: str) -> bool:
        with self._lock:
            session = self._shared_session
        return bool(session and session.take_over(thread_id))

    def take_screenshot(self, thread_id: str) -> bytes | None:
        with self._lock:
            session = self._shared_session
        return session.take_screenshot(thread_id) if session else None

    def ephemeral_screenshot(self, thread_id: str) -> bytes | None:
        with self._lock:
            session = self._shared_session
        return session.ephemeral_screenshot(thread_id) if session else None

    def kill_session(self, thread_id: str) -> None:
        with self._lock:
            session = self._shared_session
        if session:
            session.release_thread(thread_id)

    def kill_all(self) -> None:
        with self._lock:
            session, self._shared_session = self._shared_session, None
        if session:
            session.close()
