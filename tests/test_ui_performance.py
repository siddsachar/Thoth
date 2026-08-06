from __future__ import annotations

import logging


def test_load_generation_tokens() -> None:
    from row_bot.ui.performance import LoadGeneration

    gen = LoadGeneration()
    assert gen.current == 0
    first = gen.next()
    assert first == 1
    assert gen.is_current(first)
    second = gen.invalidate()
    assert second == 2
    assert not gen.is_current(first)
    assert gen.is_current(second)


def test_warn_if_slow_logs_warning(caplog) -> None:
    from row_bot.ui.performance import warn_if_slow

    with caplog.at_level(logging.WARNING, logger="row_bot.ui.performance"):
        assert warn_if_slow("unit.test", 12.0, threshold_ms=1.0, rows=3)

    assert "unit.test" in caplog.text
    assert "rows=3" in caplog.text


def test_timed_ui_section_logs_elapsed(caplog) -> None:
    from row_bot.ui.performance import timed_ui_section

    with caplog.at_level(logging.INFO, logger="row_bot.ui.performance"):
        with timed_ui_section("unit.section", threshold_ms=10_000):
            pass

    assert "unit.section" in caplog.text


def test_safe_ui_callback_records_error(monkeypatch) -> None:
    from row_bot.ui.performance import safe_ui_callback

    recorded: list[tuple[str, str]] = []

    def fake_record(context: str, exc: BaseException) -> None:
        recorded.append((context, str(exc)))

    import row_bot.stability as stability

    monkeypatch.setattr(stability, "record_ui_callback_error", fake_record)

    def boom() -> None:
        raise RuntimeError("broken callback")

    wrapped = safe_ui_callback("unit callback", boom, notify=False)
    assert wrapped() is None
    assert recorded == [("unit callback", "broken callback")]


def test_transcript_window_bounds_large_threads() -> None:
    from row_bot.ui.transcript import choose_transcript_window, message_keys

    small = choose_transcript_window(10, window_size=4)
    assert small.start == 0
    assert small.end == 10

    large = choose_transcript_window(100, window_size=25)
    assert large.start == 75
    assert large.end == 100
    assert large.older_count == 75

    expanded = choose_transcript_window(100, requested_start=50, window_size=25)
    assert expanded.start == 50
    assert expanded.end == 100

    keys = message_keys(
        [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
        ]
    )
    assert keys[0].startswith("0:user:")
    assert keys[1].startswith("1:assistant:")


def test_transcript_message_key_tracks_approval_state_not_durable_identity() -> None:
    from row_bot.ui.transcript import durable_message_key, message_key

    pending = {
        "role": "assistant",
        "content": "Parent Agent requests approval",
        "approval_request_id": "approval-parent-1",
        "approval_status": "pending",
        "approval_resume_token": "opaque-token",
    }
    handled = {
        **pending,
        "approval_status": "denied",
        "approval_resume_token": "",
    }

    assert durable_message_key(pending) == durable_message_key(handled)
    assert message_key(0, pending) != message_key(0, handled)
