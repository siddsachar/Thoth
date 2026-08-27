"""Bounded, ephemeral Managed Browser observations and exact target tokens."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import secrets
from typing import Any, Callable
from urllib.parse import urlparse, urlunparse

from row_bot.automation.contracts import ObservationStatus


COLLECTOR_LIMIT = 1_000
COLLECTOR_BYTES = 1_048_576
FIELD_LIMIT = 512
PROJECTION_LIMIT = 160
PROJECTION_BYTES = 32_768

_INTERACTIVE_SELECTOR = ", ".join(
    (
        "a[href]", "button", "input", "textarea", "select", "summary",
        '[role="button"]', '[role="link"]', '[role="tab"]', '[role="menuitem"]',
        '[role="checkbox"]', '[role="radio"]', '[role="combobox"]',
        '[role="textbox"]', '[role="searchbox"]', '[contenteditable="true"]',
    )
)

_METADATA_SCRIPT = r"""
el => {
  const rect = el.getBoundingClientRect();
  const style = window.getComputedStyle(el);
  const tag = String(el.tagName || '').toLowerCase();
  const role = String(el.getAttribute('role') || '');
  const label = String(
    el.getAttribute('aria-label') || el.getAttribute('title') ||
    el.getAttribute('placeholder') || el.innerText ||
    el.getAttribute('alt') || el.getAttribute('name') || ''
  ).trim();
  return {
    attached: Boolean(el.isConnected),
    visible: rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden',
    tag, role,
    type: String(el.getAttribute('type') || ''),
    label,
    href: String(el.getAttribute('href') || ''),
    download: Boolean(el.hasAttribute('download')),
    form_action: String(el.form ? (el.form.getAttribute('action') || '') : ''),
    disabled: Boolean(el.disabled || el.getAttribute('aria-disabled') === 'true'),
    value_length: el.value === undefined ? 0 : String(el.value).length,
    in_dialog: Boolean(el.closest('[role="dialog"],dialog,[role="menu"],[role="tablist"]')),
  };
}
"""


def public_aria_snapshot_supported(_page: Any) -> bool:
    """The public API cannot map text snapshots back to exact handles safely."""

    return False


def _bounded(value: object) -> str:
    return str(value or "")[:FIELD_LIMIT]


def _safe_href(value: object) -> str:
    raw = _bounded(value)
    try:
        parsed = urlparse(raw)
        if parsed.scheme in {"http", "https"}:
            return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))[:FIELD_LIMIT]
    except Exception:
        pass
    return raw[:160]


def _metadata(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or not raw.get("attached") or not raw.get("visible"):
        return None
    metadata = {
        "tag": _bounded(raw.get("tag")).casefold(),
        "role": _bounded(raw.get("role")).casefold(),
        "type": _bounded(raw.get("type")).casefold(),
        "label": _bounded(raw.get("label")),
        "href": _safe_href(raw.get("href")),
        "download": bool(raw.get("download")),
        "form_action": _safe_href(raw.get("form_action")),
        "disabled": bool(raw.get("disabled")),
        "value_length": max(0, min(int(raw.get("value_length") or 0), 1_000_000_000)),
        "in_dialog": bool(raw.get("in_dialog")),
    }
    if not metadata["tag"] and not metadata["role"]:
        return None
    return metadata


def target_fingerprint(metadata: dict[str, Any]) -> str:
    fields = (
        metadata.get("tag"), metadata.get("role"), metadata.get("type"),
        metadata.get("label"), metadata.get("href"), metadata.get("download"),
        metadata.get("form_action"), metadata.get("disabled"),
    )
    return hashlib.sha256(repr(fields).encode("utf-8")).hexdigest()[:24]


@dataclass
class BrowserTarget:
    token: str
    handle: Any
    metadata: dict[str, Any]
    fingerprint: str

    def dispose(self) -> None:
        try:
            self.handle.dispose()
        except Exception:
            pass


@dataclass
class BrowserObservation:
    task_id: str
    page_identity: str
    navigation_generation: int
    context_generation: int
    revision: int
    url: str
    title: str
    targets: dict[str, BrowserTarget] = field(default_factory=dict)
    status: ObservationStatus = field(default_factory=lambda: ObservationStatus(0))

    def dispose(self) -> None:
        for target in self.targets.values():
            target.dispose()
        self.targets.clear()


class StaleBrowserObservation(RuntimeError):
    code = "stale_observation"


class BrowserObservationRegistry:
    """Own exact Playwright handles only for the current task snapshot."""

    def __init__(self, *, token_factory: Callable[[], str] | None = None) -> None:
        self._current: dict[str, BrowserObservation] = {}
        self._revisions: dict[str, int] = {}
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(9))
        self.observation_count = 0

    def current(self, task_id: str) -> BrowserObservation | None:
        return self._current.get(str(task_id))

    def invalidate(
        self,
        task_id: str,
        *,
        dispose: bool = True,
    ) -> BrowserObservation | None:
        previous = self._current.pop(str(task_id), None)
        if previous is not None and dispose:
            previous.dispose()
        return previous

    def invalidate_all(self, *, dispose: bool = True) -> list[BrowserObservation]:
        previous: list[BrowserObservation] = []
        for task_id in list(self._current):
            observation = self.invalidate(task_id, dispose=dispose)
            if observation is not None:
                previous.append(observation)
        return previous

    def observe(
        self,
        page: Any,
        *,
        task_id: str,
        page_identity: str,
        navigation_generation: int,
        context_generation: int,
    ) -> BrowserObservation:
        task_id = str(task_id)
        self.invalidate(task_id)
        revision = self._revisions.get(task_id, 0) + 1
        self._revisions[task_id] = revision
        handles = list(page.query_selector_all(_INTERACTIVE_SELECTOR))
        received = len(handles)
        targets: dict[str, BrowserTarget] = {}
        semantic_bytes = 0
        locally_filtered = 0
        limit_reasons: set[str] = set()
        candidates: list[tuple[int, int, Any, dict[str, Any]]] = []
        for index, handle in enumerate(handles):
            if len(candidates) >= COLLECTOR_LIMIT:
                locally_filtered += 1
                limit_reasons.add("element_limit")
                try:
                    handle.dispose()
                except Exception:
                    pass
                continue
            try:
                metadata = _metadata(handle.evaluate(_METADATA_SCRIPT))
            except Exception:
                metadata = None
            if metadata is None:
                locally_filtered += 1
                try:
                    handle.dispose()
                except Exception:
                    pass
                continue
            size = len(repr(metadata).encode("utf-8"))
            if semantic_bytes + size > COLLECTOR_BYTES:
                locally_filtered += 1
                limit_reasons.add("semantic_byte_limit")
                try:
                    handle.dispose()
                except Exception:
                    pass
                continue
            semantic_bytes += size
            priority = self._priority(metadata)
            candidates.append((priority, index, handle, metadata))
        candidates.sort(key=lambda item: (-item[0], item[1]))
        projected = candidates[:PROJECTION_LIMIT]
        projected_bytes = 0
        for _priority, _index, handle, metadata in projected:
            token = f"b{revision}_{self._token_factory()}"
            rendered = self._render_line(token, metadata)
            rendered_size = len(rendered.encode("utf-8"))
            if projected_bytes + rendered_size > PROJECTION_BYTES - 4_096:
                limit_reasons.add("projection_byte_limit")
                handle.dispose()
                locally_filtered += 1
                continue
            projected_bytes += rendered_size
            if token in targets:
                token = f"{token}-{_index}"
            targets[token] = BrowserTarget(token, handle, metadata, target_fingerprint(metadata))
        retained_handles = {id(target.handle) for target in targets.values()}
        for _priority, _index, handle, _metadata_value in candidates:
            if id(handle) not in retained_handles:
                try:
                    handle.dispose()
                except Exception:
                    pass
        if len(candidates) > PROJECTION_LIMIT:
            locally_filtered += len(candidates) - PROJECTION_LIMIT
            limit_reasons.add("projection_element_limit")
        observation = BrowserObservation(
            task_id=task_id,
            page_identity=page_identity,
            navigation_generation=navigation_generation,
            context_generation=context_generation,
            revision=revision,
            url=_safe_href(getattr(page, "url", ""))[:2048],
            title=self._page_title(page),
            targets=targets,
            status=ObservationStatus(
                revision=revision,
                backend_declared_count=received,
                backend_received_count=received,
                backend_filtered_count=0,
                locally_validated_count=len(candidates),
                projected_count=len(targets),
                locally_filtered_count=locally_filtered,
                backend_limited=False,
                local_limit_reasons=tuple(limit_reasons),
            ),
        )
        self._current[task_id] = observation
        self.observation_count += 1
        return observation

    @staticmethod
    def _page_title(page: Any) -> str:
        try:
            return str(page.title() or "")[:512]
        except Exception:
            return ""

    @staticmethod
    def _priority(metadata: dict[str, Any]) -> int:
        score = 0
        if metadata.get("in_dialog"):
            score += 100
        if metadata.get("label"):
            score += 30
        if metadata.get("tag") in {"input", "textarea", "select", "button"}:
            score += 20
        if metadata.get("role") in {"button", "textbox", "combobox", "menuitem", "tab"}:
            score += 15
        if metadata.get("disabled"):
            score -= 50
        return score

    @staticmethod
    def _render_line(token: str, metadata: dict[str, Any]) -> str:
        role = metadata.get("role") or metadata.get("tag") or "control"
        label = metadata.get("label") or ""
        suffix = f' "{label}"' if label else ""
        if metadata.get("href"):
            suffix += f" -> {metadata['href']}"
        if metadata.get("value_length"):
            suffix += f" value_length={metadata['value_length']}"
        return f"[{token}] {role}{suffix}"

    def format(self, observation: BrowserObservation) -> str:
        lines = [
            f"URL: {observation.url}",
            f"Title: {observation.title}",
            (
                "Interactive elements "
                f"({observation.status.projected_count}; revision={observation.revision}; "
                f"received={observation.status.backend_received_count}; "
                f"filtered={observation.status.locally_filtered_count}; "
                f"provenance={observation.status.provenance}):"
            ),
        ]
        lines.extend(f"  {self._render_line(token, target.metadata)}" for token, target in observation.targets.items())
        return "\n".join(lines)

    def resolve(
        self,
        token: str,
        *,
        task_id: str,
        page_identity: str,
        navigation_generation: int,
        context_generation: int,
    ) -> BrowserTarget:
        observation = self._current.get(str(task_id))
        if (
            observation is None
            or observation.page_identity != page_identity
            or observation.navigation_generation != navigation_generation
            or observation.context_generation != context_generation
        ):
            raise StaleBrowserObservation("stale observation")
        target = observation.targets.get(str(token))
        if target is None:
            raise StaleBrowserObservation("stale observation")
        try:
            current = _metadata(target.handle.evaluate(_METADATA_SCRIPT))
        except Exception as exc:
            raise StaleBrowserObservation("stale observation") from exc
        if current is None or target_fingerprint(current) != target.fingerprint:
            raise StaleBrowserObservation("stale observation")
        return target
