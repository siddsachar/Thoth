from __future__ import annotations

import asyncio
import queue
from collections.abc import Iterable
from typing import Any

import pytest

from row_bot.channels.streaming import (
    ChannelRateLimitError,
    ChannelStreamConfig,
    ChannelStreamConsumer,
)


pytestmark = pytest.mark.subsystem


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds)


class FakeTransport:
    def __init__(self, *, max_units: int = 200) -> None:
        self.max_units = max_units
        self.operations: list[tuple[str, Any]] = []
        self.fail_next_update_rate_limit: float | None = None
        self.fail_final_update = False
        self.fail_final_send = False

    async def send_typing(self) -> None:
        self.operations.append(("typing", None))

    async def start(self, text: str) -> str:
        self.operations.append(("start", text))
        return "handle-1"

    async def update(self, handle: Any, text: str, *, final: bool = False) -> Any:
        if self.fail_next_update_rate_limit is not None:
            retry_after = self.fail_next_update_rate_limit
            self.fail_next_update_rate_limit = None
            raise ChannelRateLimitError("rate limited", retry_after=retry_after)
        if final and self.fail_final_update:
            raise RuntimeError("final edit failed")
        self.operations.append(("update_final" if final else "update", text))
        return handle

    async def send_final(self, text: str) -> list[Any]:
        if self.fail_final_send:
            raise RuntimeError("send failed")
        refs = []
        for index, chunk in enumerate(self.split_text(text), start=1):
            self.operations.append(("send_final", chunk))
            refs.append(f"message-{index}")
        return refs

    async def cleanup_preview(self, handle: Any) -> None:
        self.operations.append(("cleanup", handle))

    def split_text(self, text: str) -> list[str]:
        value = str(text)
        if not value:
            return []
        return [
            value[index:index + self.max_units]
            for index in range(0, len(value), self.max_units)
        ]


async def _events(items: Iterable[tuple[str, Any]], clock: FakeClock | None = None):
    for event_type, payload in items:
        if event_type == "advance":
            assert clock is not None
            clock.advance(float(payload))
            continue
        yield event_type, payload


def _consumer(
    transport: FakeTransport,
    clock: FakeClock,
    **overrides: Any,
) -> ChannelStreamConsumer:
    config = ChannelStreamConfig(
        channel="fake",
        transport_mode="edit",
        update_interval_s=overrides.pop("update_interval_s", 999.0),
        max_stale_interval_s=overrides.pop("max_stale_interval_s", 3.0),
        min_update_chars=overrides.pop("min_update_chars", 999),
        typing_interval_s=overrides.pop("typing_interval_s", None),
        cursor=overrides.pop("cursor", "..."),
        max_message_units=overrides.pop("max_message_units", transport.max_units),
        fresh_final_after_s=overrides.pop("fresh_final_after_s", None),
        sparse_progress=overrides.pop("sparse_progress", True),
        retry_backoff_s=overrides.pop("retry_backoff_s", 0.01),
        max_retry_attempts=overrides.pop("max_retry_attempts", 2),
    )
    assert not overrides
    return ChannelStreamConsumer(
        transport,
        config,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )


def test_engine_starts_once_shows_cursor_and_finalizes_without_cursor() -> None:
    async def run_case():
        clock = FakeClock()
        transport = FakeTransport()
        consumer = _consumer(transport, clock)
        result = await consumer.consume_events(_events([
            ("token", "hel"),
            ("token", "lo"),
        ]))
        return result, transport.operations

    result, operations = asyncio.run(run_case())

    assert result.delivered is True
    assert result.streamed is True
    assert result.finalized is True
    assert result.final_text == "hello"
    assert [op for op, _payload in operations].count("start") == 1
    assert ("update", "hel...") in operations
    assert operations[-1] == ("update_final", "hello")


def test_engine_coalesces_minimum_buffered_characters_until_interval() -> None:
    async def run_case():
        clock = FakeClock()
        transport = FakeTransport()
        consumer = _consumer(
            transport,
            clock,
            min_update_chars=1,
            update_interval_s=1.0,
            max_stale_interval_s=30.0,
        )
        await consumer.consume_events(_events([
            ("token", "a"),
            ("token", "b"),
        ]))
        return transport.operations

    operations = asyncio.run(run_case())

    assert ("update", "a...") in operations
    assert ("update", "ab...") not in operations
    assert operations[-1] == ("update_final", "ab")


def test_engine_updates_when_buffer_and_interval_are_ready() -> None:
    async def run_case():
        clock = FakeClock()
        transport = FakeTransport()
        consumer = _consumer(
            transport,
            clock,
            min_update_chars=1,
            update_interval_s=1.0,
            max_stale_interval_s=30.0,
        )
        await consumer.consume_events(_events([
            ("token", "a"),
            ("advance", 1.1),
            ("token", "b"),
        ], clock))
        return transport.operations

    operations = asyncio.run(run_case())

    assert ("update", "a...") in operations
    assert ("update", "ab...") in operations


def test_engine_allows_stale_update_for_slow_streams() -> None:
    async def run_case():
        clock = FakeClock()
        transport = FakeTransport()
        consumer = _consumer(
            transport,
            clock,
            min_update_chars=999,
            update_interval_s=0.5,
            max_stale_interval_s=2.0,
        )
        await consumer.consume_events(_events([
            ("token", "a"),
            ("advance", 2.1),
            ("token", "b"),
        ], clock))
        return transport.operations

    operations = asyncio.run(run_case())

    assert ("update", "a...") in operations
    assert ("update", "ab...") in operations


def test_engine_updates_by_interval_without_duplicate_noop_updates() -> None:
    async def run_case():
        clock = FakeClock()
        transport = FakeTransport()
        consumer = _consumer(transport, clock, update_interval_s=1.0)
        await consumer.consume_events(_events([
            ("token", "a"),
            ("advance", 2.0),
            ("thinking", "still thinking"),
            ("advance", 2.0),
            ("token", "b"),
        ], clock))
        return transport.operations

    operations = asyncio.run(run_case())

    assert operations.count(("update", "a...")) == 1
    assert ("update", "ab...") in operations


def test_engine_sends_typing_keepalive_on_schedule() -> None:
    async def run_case():
        clock = FakeClock()
        transport = FakeTransport()
        consumer = _consumer(transport, clock, typing_interval_s=4.0)
        await consumer.consume_events(_events([
            ("token", "a"),
            ("advance", 5.0),
            ("token", "b"),
        ], clock))
        return transport.operations

    operations = asyncio.run(run_case())

    assert [op for op, _payload in operations].count("typing") == 2


def test_engine_retries_rate_limited_updates_with_backoff() -> None:
    async def run_case():
        clock = FakeClock()
        transport = FakeTransport()
        transport.fail_next_update_rate_limit = 2.0
        consumer = _consumer(transport, clock)
        result = await consumer.consume_events(_events([("token", "hello")]))
        return result, transport.operations, clock.sleeps

    result, operations, sleeps = asyncio.run(run_case())

    assert result.delivered is True
    assert sleeps == [2.0]
    assert ("update", "hello...") in operations


def test_engine_splits_final_output_over_platform_limit() -> None:
    async def run_case():
        clock = FakeClock()
        transport = FakeTransport(max_units=5)
        consumer = _consumer(transport, clock)
        result = await consumer.consume_events(
            _events([("token", "abc")]),
            final_text="abcdefghi",
        )
        return result, transport.operations

    result, operations = asyncio.run(run_case())

    assert result.delivered is True
    assert result.fallback_sent is True
    assert [payload for op, payload in operations if op == "send_final"] == ["abcde", "fghi"]
    assert ("cleanup", "handle-1") in operations


def test_engine_continues_capped_preview_after_partial_exceeds_one_message() -> None:
    async def run_case():
        clock = FakeClock()
        transport = FakeTransport(max_units=80)
        consumer = _consumer(
            transport,
            clock,
            min_update_chars=1,
            update_interval_s=0.0,
        )
        result = await consumer.consume_events(
            _events([
                ("token", "a" * 100),
                ("token", "b" * 20),
            ]),
            final_text=("a" * 100) + ("b" * 20),
        )
        return result, transport.operations

    result, operations = asyncio.run(run_case())

    updates = [payload for op, payload in operations if op == "update"]

    assert result.delivered is True
    assert result.fallback_sent is True
    assert updates
    assert all("Streaming preview; final answer will be sent in parts." in text for text in updates)
    assert "b" * 10 in updates[-1]


def test_engine_skips_partial_backlog_when_final_source_is_done() -> None:
    async def run_case():
        clock = FakeClock()
        transport = FakeTransport()
        consumer = _consumer(transport, clock, min_update_chars=1, update_interval_s=0.0)
        event_queue: queue.Queue[Any] = queue.Queue()
        for _index in range(100):
            event_queue.put(("token", "x"))
        event_queue.put(None)
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        future.set_result("final answer")
        result = await consumer.consume_queue(
            event_queue,
            poll_interval_s=0.01,
            final_text_source=future,
        )
        return result, transport.operations, event_queue.empty()

    result, operations, queue_empty = asyncio.run(run_case())

    assert result.delivered is True
    assert result.fallback_sent is True
    assert queue_empty is True
    assert [op for op, _payload in operations if op in {"start", "update"}] == []
    assert operations == [("send_final", "final answer")]


def test_engine_falls_back_to_final_send_when_final_update_fails() -> None:
    async def run_case():
        clock = FakeClock()
        transport = FakeTransport()
        transport.fail_final_update = True
        consumer = _consumer(transport, clock)
        result = await consumer.consume_events(_events([("token", "final")]))
        return result, transport.operations

    result, operations = asyncio.run(run_case())

    assert result.delivered is True
    assert result.fallback_sent is True
    assert ("send_final", "final") in operations


def test_engine_marks_delivery_failed_when_update_and_fallback_fail() -> None:
    async def run_case():
        clock = FakeClock()
        transport = FakeTransport()
        transport.fail_final_update = True
        transport.fail_final_send = True
        consumer = _consumer(transport, clock)
        return await consumer.consume_events(_events([("token", "final")]))

    result = asyncio.run(run_case())

    assert result.delivered is False
    assert result.finalized is False
    assert "send failed" in (result.error or "")


def test_engine_handles_cancellation_without_false_final_success() -> None:
    async def cancelling_events():
        yield "token", "partial"
        raise asyncio.CancelledError()

    async def run_case():
        clock = FakeClock()
        transport = FakeTransport()
        consumer = _consumer(transport, clock)
        result = await consumer.consume_events(cancelling_events())
        return result, transport.operations

    result, operations = asyncio.run(run_case())

    assert result.delivered is False
    assert result.finalized is False
    assert result.error == "cancelled"
    assert [op for op, _payload in operations if op == "send_final"] == []


def test_compaction_notice_is_separate_and_claimed_exactly_once(monkeypatch) -> None:
    claims: set[int] = set()
    completions: list[tuple[int, list[str]]] = []

    def claim(event_id: int, channel: str):
        if event_id in claims:
            return None
        claims.add(event_id)
        assert channel == "fake"
        return {
            "display_copy": (
                "Context compacted â€” Older conversation was summarized so this chat can continue. "
                "Recent messages were preserved."
            )
        }

    monkeypatch.setattr("row_bot.threads.claim_thread_event_delivery", claim)
    monkeypatch.setattr(
        "row_bot.threads.complete_thread_event_delivery",
        lambda event_id, platform_refs=None, error="": completions.append(
            (event_id, list(platform_refs or []))
        ),
    )

    async def run_case():
        first_transport = FakeTransport(max_units=500)
        second_transport = FakeTransport(max_units=500)
        payload = {
            "event_id": 42,
            "display_copy": "must be read from persisted event",
        }
        first = _consumer(first_transport, FakeClock())
        second = _consumer(second_transport, FakeClock())
        first_result = await first.consume_events(_events([
            ("compaction_succeeded", payload),
            ("token", "answer"),
        ]))
        second_result = await second.consume_events(_events([
            ("compaction_succeeded", payload),
            ("token", "resumed answer"),
        ]))
        return first_result, second_result, first_transport.operations, second_transport.operations

    first_result, second_result, first_ops, second_ops = asyncio.run(run_case())

    notice = (
        "Context compacted â€” Older conversation was summarized so this chat can continue. "
        "Recent messages were preserved."
    )
    assert first_result.final_text == "answer"
    assert second_result.final_text == "resumed answer"
    assert ("send_final", notice) in first_ops
    assert ("send_final", notice) not in second_ops
    assert completions == [(42, ["message-1"])]
