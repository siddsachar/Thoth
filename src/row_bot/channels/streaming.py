"""Shared deterministic streaming engine for messaging channels."""

from __future__ import annotations

import asyncio
import inspect
import logging
import queue
import time
from collections.abc import AsyncIterable, Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

from row_bot.channels.agent_output import assemble_agent_answer

log = logging.getLogger(__name__)

ORCHESTRATION_SUSPENDED_FINAL = "__ROW_BOT_ORCHESTRATION_SUSPENDED__"


@dataclass(frozen=True)
class ChannelStreamConfig:
    channel: str
    transport_mode: str = "edit"
    update_interval_s: float = 0.8
    max_stale_interval_s: float | None = 3.0
    min_update_chars: int = 24
    typing_interval_s: float | None = None
    cursor: str = "..."
    max_message_units: int = 4000
    fresh_final_after_s: float | None = None
    sparse_progress: bool = True
    retry_backoff_s: float = 0.25
    max_retry_attempts: int = 2


@dataclass
class ChannelDeliveryResult:
    delivered: bool
    streamed: bool
    finalized: bool
    fallback_sent: bool
    transport: str
    final_text: str
    error: str | None = None
    platform_message_refs: list[str] = field(default_factory=list)


class ChannelStreamTransport(Protocol):
    async def send_typing(self) -> None: ...
    async def start(self, text: str) -> Any: ...
    async def update(self, handle: Any, text: str, *, final: bool = False) -> Any: ...
    async def send_final(self, text: str) -> list[Any]: ...
    async def cleanup_preview(self, handle: Any) -> None: ...
    def split_text(self, text: str) -> list[str]: ...


class ChannelRateLimitError(RuntimeError):
    """Raised by transports when a bounded retry-after delay is available."""

    def __init__(self, message: str = "rate limited", *, retry_after: float = 0.0) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def default_split_text(text: str, max_units: int) -> list[str]:
    value = str(text or "")
    if not value:
        return []
    if len(value) <= max_units:
        return [value]
    chunks: list[str] = []
    remaining = value
    while remaining:
        if len(remaining) <= max_units:
            chunks.append(remaining)
            break
        split_at = max_units
        paragraph = remaining.rfind("\n\n", 0, max_units)
        if paragraph > max_units // 2:
            split_at = paragraph + 2
        else:
            line = remaining.rfind("\n", 0, max_units)
            if line > max_units // 2:
                split_at = line + 1
            else:
                space = remaining.rfind(" ", 0, max_units)
                if space > max_units // 2:
                    split_at = space + 1
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip()
    return chunks


def extract_agent_final_text(result: Any) -> str:
    if isinstance(result, tuple):
        return str(result[0] or "") if result else ""
    return str(result or "")


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


class ChannelStreamConsumer:
    """Consume agent stream events and deliver platform-safe channel updates."""

    def __init__(
        self,
        transport: ChannelStreamTransport,
        config: ChannelStreamConfig,
        *,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.transport = transport
        self.config = config
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self.answer_tokens: list[str] = []
        self.tool_reports: list[str] = []
        self.status_lines: list[str] = []
        self.interrupt_data: Any | None = None
        self._handle: Any | None = None
        self._started_at = self._monotonic()
        self._last_update_at = 0.0
        self._last_update_len = 0
        self._last_sent_display = ""
        self._stream_started = False
        self._stream_disabled = str(config.transport_mode or "").lower() == "off"
        self._overflow_preview_active = False
        self._last_overflow_preview = ""
        self._final_source_done_at: float | None = None
        self._next_typing_at: float | None = (
            self._started_at if config.typing_interval_s else None
        )

    async def consume_events(
        self,
        events: AsyncIterable[tuple[str, Any]] | Iterable[tuple[str, Any]],
        *,
        final_text: str | None = None,
        final_text_source: Awaitable[Any] | None = None,
        final_text_extractor: Callable[[Any], str] = extract_agent_final_text,
    ) -> ChannelDeliveryResult:
        try:
            async for event_type, payload in self._aiter_events(events):
                await self._maybe_send_typing()
                self._record_event(event_type, payload)
                await self._maybe_update_partial()
            await self._maybe_send_typing()
            resolved_final = await self._resolve_final_text(
                final_text=final_text,
                final_text_source=final_text_source,
                final_text_extractor=final_text_extractor,
            )
            finalize_started = self._monotonic()
            result = await self._finalize(resolved_final)
            duration_ms = (self._monotonic() - finalize_started) * 1000.0
            log.info(
                "Channel stream finalize completed channel=%s transport=%s "
                "delivered=%s fallback=%s chars=%d duration_ms=%.1f",
                self.config.channel,
                str(getattr(self.transport, "transport_name", self.config.transport_mode)),
                result.delivered,
                result.fallback_sent,
                len(result.final_text or ""),
                duration_ms,
            )
            return result
        except asyncio.CancelledError:
            await self._cleanup_preview()
            return self._result(
                delivered=False,
                streamed=self._stream_started,
                finalized=False,
                fallback_sent=False,
                final_text=self._current_final_text(final_text),
                error="cancelled",
            )

    async def consume_queue(
        self,
        event_queue: queue.Queue[Any],
        *,
        poll_interval_s: float = 0.2,
        final_text: str | None = None,
        final_text_source: Awaitable[Any] | None = None,
        final_text_extractor: Callable[[Any], str] = extract_agent_final_text,
    ) -> ChannelDeliveryResult:
        async def _events():
            while True:
                await self._maybe_send_typing()
                if self._final_text_source_done(final_text_source):
                    self._mark_final_source_done()
                    drained = self._drain_queue(event_queue)
                    if drained:
                        log.debug(
                            "Channel stream drained queued events after final source completed "
                            "channel=%s drained=%d",
                            self.config.channel,
                            drained,
                        )
                    break
                try:
                    item = await asyncio.to_thread(event_queue.get, True, poll_interval_s)
                except queue.Empty:
                    continue
                if item is None:
                    break
                yield item

        return await self.consume_events(
            _events(),
            final_text=final_text,
            final_text_source=final_text_source,
            final_text_extractor=final_text_extractor,
        )

    async def _aiter_events(
        self,
        events: AsyncIterable[tuple[str, Any]] | Iterable[tuple[str, Any]],
    ):
        if hasattr(events, "__aiter__"):
            async for item in events:  # type: ignore[union-attr]
                yield item
            return
        for item in events:  # type: ignore[union-attr]
            yield item

    def _record_event(self, event_type: str, payload: Any) -> None:
        if event_type == "token":
            self.answer_tokens.append(str(payload or ""))
        elif event_type in {"thinking", "thinking_token"}:
            if not self.config.sparse_progress and payload:
                self.status_lines.append(str(payload))
        elif event_type == "tool_call":
            label = self._tool_label(payload)
            self.tool_reports.append(f"Using {label}...")
            if not self.config.sparse_progress:
                self.status_lines.append(f"Using {label}...")
        elif event_type == "tool_done":
            label = self._tool_label(payload)
            self.tool_reports.append(f"{label} done")
            if not self.config.sparse_progress:
                self.status_lines.append(f"{label} done")
        elif event_type == "summarizing":
            if not self.config.sparse_progress:
                self.status_lines.append("Summarizing...")
        elif event_type == "interrupt":
            self.interrupt_data = payload
        elif event_type == "error":
            self.answer_tokens.append(f"\nWarning: Error: {payload}")
        elif event_type == "done" and payload and not self.answer_tokens:
            self.answer_tokens.append(str(payload))

    def _tool_label(self, payload: Any) -> str:
        if isinstance(payload, dict):
            return str(
                payload.get("name")
                or payload.get("display_name")
                or payload.get("raw_name")
                or "tool"
            )
        return str(payload or "tool")

    def _display_text(self) -> str:
        parts: list[str] = []
        if self.status_lines and not self.config.sparse_progress:
            parts.extend(line for line in self.status_lines[-4:] if str(line).strip())
        answer = "".join(self.answer_tokens).strip()
        if answer:
            parts.append(answer)
        return "\n".join(parts).strip()

    def _current_final_text(self, override: str | None = None) -> str:
        if override is not None:
            return str(override or "").strip()
        answer = assemble_agent_answer("".join(self.answer_tokens), self.tool_reports).strip()
        if answer:
            return answer
        if self.interrupt_data:
            return ""
        return "_(No response)_"

    async def _resolve_final_text(
        self,
        *,
        final_text: str | None,
        final_text_source: Awaitable[Any] | None,
        final_text_extractor: Callable[[Any], str],
    ) -> str:
        if final_text is not None:
            return str(final_text or "").strip()
        if final_text_source is not None:
            try:
                if self._final_text_source_done(final_text_source):
                    self._mark_final_source_done()
                if self._final_source_done_at is not None:
                    log.info(
                        "Channel stream final source ready channel=%s transport=%s "
                        "lag_ms=%.1f",
                        self.config.channel,
                        str(getattr(self.transport, "transport_name", self.config.transport_mode)),
                        (self._monotonic() - self._final_source_done_at) * 1000.0,
                    )
                result = await asyncio.shield(final_text_source)
                extracted = str(final_text_extractor(result) or "").strip()
                if extracted:
                    return extracted
            except Exception:
                log.debug("Could not resolve final channel stream text", exc_info=True)
        return self._current_final_text()

    async def _maybe_send_typing(self) -> None:
        if self._next_typing_at is None:
            return
        now = self._monotonic()
        if now < self._next_typing_at:
            return
        try:
            await self._call_with_retry(self.transport.send_typing)
        except Exception:
            log.debug("Channel typing keepalive failed", exc_info=True)
        interval = float(self.config.typing_interval_s or 0)
        self._next_typing_at = now + interval if interval > 0 else None

    async def _ensure_started(self, text: str) -> bool:
        if self._stream_disabled:
            return False
        if self._stream_started:
            return self._handle is not None
        try:
            self._handle = await self._call_with_retry(self.transport.start, text)
            self._stream_started = True
            return self._handle is not None
        except Exception:
            log.debug("Channel stream start failed; falling back to final send", exc_info=True)
            self._stream_disabled = True
            return False

    async def _maybe_update_partial(self) -> None:
        display = self._display_text()
        if not display:
            return
        partial = self._partial_text(display)
        if not partial:
            return
        if partial == self._last_sent_display:
            return
        if not await self._ensure_started(partial):
            return
        now = self._monotonic()
        should_update = not self._last_sent_display
        if not should_update:
            chars_ready = (
                len(display) - self._last_update_len >= int(self.config.min_update_chars)
            )
            elapsed = now - self._last_update_at
            interval_ready = elapsed >= float(self.config.update_interval_s)
            stale_interval = self.config.max_stale_interval_s
            stale_ready = (
                stale_interval is not None
                and float(stale_interval) > 0
                and elapsed >= float(stale_interval)
            )
            should_update = (chars_ready and interval_ready) or stale_ready
        if not should_update:
            return
        try:
            await self._call_with_retry(self.transport.update, self._handle, partial, final=False)
            self._last_sent_display = partial
            self._last_update_at = now
            self._last_update_len = len(display)
        except Exception:
            log.debug("Channel stream update failed; final send fallback will be used", exc_info=True)
            self._stream_disabled = True

    def _partial_text(self, display: str) -> str:
        cursor = str(self.config.cursor or "")
        partial = f"{display}{cursor}" if cursor else display
        if len(self._split_text(partial)) <= 1:
            self._overflow_preview_active = False
            self._last_overflow_preview = ""
            return partial
        partial = ""
        freeze_overflow_preview = bool(
            getattr(self.transport, "freeze_overflow_preview", False)
        )
        if (
            freeze_overflow_preview
            and self._overflow_preview_active
            and self._last_overflow_preview
            and self._last_sent_display == self._last_overflow_preview
        ):
            return self._last_overflow_preview
        builder = getattr(self.transport, "build_overflow_preview", None)
        if callable(builder):
            try:
                partial = str(builder(display, cursor=cursor) or "")
            except Exception:
                log.debug("Channel overflow preview builder failed", exc_info=True)
                partial = ""
        if not partial:
            partial = self._generic_overflow_preview(display, cursor=cursor)
        if len(self._split_text(partial)) <= 1:
            if not self._overflow_preview_active:
                self._overflow_preview_active = True
                log.info(
                    "Channel stream preview exceeded one message; continuing capped preview "
                    "channel=%s transport=%s chars=%d",
                    self.config.channel,
                    str(getattr(self.transport, "transport_name", self.config.transport_mode)),
                    len(display),
                )
            self._last_overflow_preview = partial
            return partial
        chunks = self._split_text(partial)
        return chunks[0] if chunks else ""

    def _generic_overflow_preview(self, display: str, *, cursor: str) -> str:
        note = "Streaming preview; final answer will be sent in parts.\n\n...\n"
        budget = max(1, int(self.config.max_message_units) - len(note) - len(cursor))
        tail = str(display or "")[-budget:]
        candidate = f"{note}{tail.lstrip()}{cursor}"
        while len(candidate) > 1 and len(self._split_text(candidate)) > 1:
            if not tail:
                break
            tail = tail[max(1, len(tail) // 10):]
            candidate = f"{note}{tail.lstrip()}{cursor}"
        return candidate

    async def _finalize(self, final_text: str) -> ChannelDeliveryResult:
        final_text = str(final_text or "").strip()
        if final_text == ORCHESTRATION_SUSPENDED_FINAL:
            await self._cleanup_preview()
            return self._result(
                delivered=True,
                streamed=self._stream_started,
                finalized=True,
                fallback_sent=False,
                final_text="",
            )
        if not final_text:
            return self._result(
                delivered=False,
                streamed=self._stream_started,
                finalized=False,
                fallback_sent=False,
                final_text=final_text,
                error="empty final text",
            )
        chunks = self._split_text(final_text)
        force_final_send = bool(getattr(self.transport, "final_delivery", "") == "send")
        if self.config.fresh_final_after_s is not None:
            force_final_send = force_final_send or (
                self._monotonic() - self._started_at >= float(self.config.fresh_final_after_s)
            )
        if len(chunks) > 1:
            force_final_send = True
        if self._stream_disabled or self._handle is None or force_final_send:
            return await self._send_final_and_cleanup(final_text)
        try:
            await self._call_with_retry(self.transport.update, self._handle, final_text, final=True)
            return self._result(
                delivered=True,
                streamed=self._stream_started,
                finalized=True,
                fallback_sent=False,
                final_text=final_text,
            )
        except Exception as exc:
            log.debug("Channel final stream update failed; trying final send", exc_info=True)
            return await self._send_final_and_cleanup(final_text, previous_error=exc)

    async def _send_final_and_cleanup(
        self,
        final_text: str,
        *,
        previous_error: Exception | None = None,
    ) -> ChannelDeliveryResult:
        try:
            refs = await _maybe_await(self.transport.send_final(final_text))
            await self._cleanup_preview()
            log.info(
                "Channel stream final delivery succeeded channel=%s transport=%s "
                "chars=%d chunks=%d streamed=%s fallback=%s",
                self.config.channel,
                str(getattr(self.transport, "transport_name", self.config.transport_mode)),
                len(final_text),
                len(self._split_text(final_text)),
                self._stream_started,
                True,
            )
            return self._result(
                delivered=True,
                streamed=self._stream_started,
                finalized=True,
                fallback_sent=True,
                final_text=final_text,
                refs=[str(ref) for ref in refs or []],
            )
        except Exception as exc:
            await self._cleanup_preview()
            error = str(exc) or str(previous_error or "")
            return self._result(
                delivered=False,
                streamed=self._stream_started,
                finalized=False,
                fallback_sent=previous_error is not None,
                final_text=final_text,
                error=error,
            )

    async def _cleanup_preview(self) -> None:
        if self._handle is None:
            return
        try:
            await _maybe_await(self.transport.cleanup_preview(self._handle))
        except Exception:
            log.debug("Channel stream preview cleanup failed", exc_info=True)

    def _split_text(self, text: str) -> list[str]:
        try:
            chunks = self.transport.split_text(text)
        except Exception:
            chunks = default_split_text(text, self.config.max_message_units)
        return chunks or ([] if not str(text or "") else [str(text)])

    @staticmethod
    def _final_text_source_done(final_text_source: Awaitable[Any] | None) -> bool:
        if final_text_source is None:
            return False
        done = getattr(final_text_source, "done", None)
        if not callable(done):
            return False
        try:
            if not bool(done()):
                return False
        except Exception:
            return False
        cancelled = getattr(final_text_source, "cancelled", None)
        try:
            if callable(cancelled) and bool(cancelled()):
                return False
        except Exception:
            return False
        exception = getattr(final_text_source, "exception", None)
        if callable(exception):
            try:
                return exception() is None
            except Exception:
                return False
        return True

    def _mark_final_source_done(self) -> None:
        if self._final_source_done_at is None:
            self._final_source_done_at = self._monotonic()

    @staticmethod
    def _drain_queue(event_queue: queue.Queue[Any]) -> int:
        drained = 0
        while True:
            try:
                event_queue.get_nowait()
                drained += 1
            except queue.Empty:
                return drained

    async def _call_with_retry(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        attempts = max(0, int(self.config.max_retry_attempts))
        last_error: Exception | None = None
        for attempt in range(attempts + 1):
            try:
                return await _maybe_await(func(*args, **kwargs))
            except Exception as exc:
                delay = self._retry_delay(exc, attempt)
                if delay is None or attempt >= attempts:
                    raise
                last_error = exc
                await self._sleep(delay)
        if last_error is not None:
            raise last_error
        return await _maybe_await(func(*args, **kwargs))

    def _retry_delay(self, exc: Exception, attempt: int) -> float | None:
        retry_after = getattr(exc, "retry_after", None)
        if retry_after is not None:
            try:
                return max(0.0, float(retry_after))
            except Exception:
                pass
        text = str(exc).lower()
        if "rate" in text or "retry" in text or "flood" in text:
            return float(self.config.retry_backoff_s) * (2 ** attempt)
        return None

    def _result(
        self,
        *,
        delivered: bool,
        streamed: bool,
        finalized: bool,
        fallback_sent: bool,
        final_text: str,
        error: str | None = None,
        refs: list[str] | None = None,
    ) -> ChannelDeliveryResult:
        return ChannelDeliveryResult(
            delivered=delivered,
            streamed=streamed,
            finalized=finalized,
            fallback_sent=fallback_sent,
            transport=str(getattr(self.transport, "transport_name", self.config.transport_mode)),
            final_text=final_text,
            error=error,
            platform_message_refs=list(refs or []),
        )
