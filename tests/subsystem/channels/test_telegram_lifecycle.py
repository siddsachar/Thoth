from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from telegram.error import InvalidToken, TimedOut

from row_bot.channels import telegram


FAKE_TOKEN = "123456:FAKE_SECRET_TOKEN"


class FakeBot:
    def __init__(
        self,
        events: list[str],
        *,
        command_error: Exception | None = None,
    ) -> None:
        self.events = events
        self.command_error = command_error
        self.command_calls = 0
        self.shutdown_calls = 0

    async def set_my_commands(self, commands: Any) -> None:
        self.command_calls += 1
        self.events.append("commands")
        if self.command_error is not None:
            raise self.command_error

    async def shutdown(self) -> None:
        self.shutdown_calls += 1
        self.events.append("bot_shutdown")


class FakeUpdater:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.running = False
        self.polling_calls: list[dict[str, Any]] = []
        self.stop_calls = 0

    async def start_polling(self, **kwargs: Any) -> None:
        self.polling_calls.append(kwargs)
        self.events.append("polling")
        self.running = True

    async def stop(self) -> None:
        self.stop_calls += 1
        self.running = False
        self.events.append("updater_stop")


class FakeApplication:
    def __init__(
        self,
        initialize_outcomes: list[Exception | None],
        *,
        command_error: Exception | None = None,
    ) -> None:
        self.events: list[str] = []
        self.initialize_outcomes = list(initialize_outcomes)
        self.initialize_calls = 0
        self.start_calls = 0
        self.stop_calls = 0
        self.shutdown_calls = 0
        self.running = False
        self.bot = FakeBot(self.events, command_error=command_error)
        self.updater = FakeUpdater(self.events)
        self.handlers: list[Any] = []

    def add_handler(self, handler: Any) -> None:
        self.handlers.append(handler)

    async def initialize(self) -> None:
        self.initialize_calls += 1
        self.events.append("initialize")
        outcome = self.initialize_outcomes.pop(0)
        if outcome is not None:
            raise outcome

    async def start(self) -> None:
        self.start_calls += 1
        self.running = True
        self.events.append("start")

    async def stop(self) -> None:
        self.stop_calls += 1
        self.running = False
        self.events.append("app_stop")

    async def shutdown(self) -> None:
        self.shutdown_calls += 1
        self.events.append("app_shutdown")


class FakeApplicationBuilder:
    def __init__(self, applications: list[FakeApplication]) -> None:
        self.applications = applications

    def token(self, token: str) -> FakeApplicationBuilder:
        assert token == FAKE_TOKEN
        return self

    def build(self) -> FakeApplication:
        return self.applications.pop(0)


@pytest.fixture(autouse=True)
def reset_telegram_lifecycle():
    telegram._app = None
    telegram._running = False
    telegram._bot_loop = None
    yield
    telegram._app = None
    telegram._running = False
    telegram._bot_loop = None


def configure_applications(
    monkeypatch: pytest.MonkeyPatch,
    applications: list[FakeApplication],
) -> None:
    builder = FakeApplicationBuilder(applications)

    class FakeApplicationAPI:
        @staticmethod
        def builder() -> FakeApplicationBuilder:
            return builder

    monkeypatch.setattr(telegram, "Application", FakeApplicationAPI)
    monkeypatch.setattr(telegram, "is_configured", lambda: True)
    monkeypatch.setattr(telegram, "_get_bot_token", lambda: FAKE_TOKEN)
    monkeypatch.setattr(telegram, "_INITIALIZE_RETRY_DELAY_SECONDS", 0)


def test_initialize_network_error_retries_once_and_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = FakeApplication([TimedOut("temporary failure"), None])
    configure_applications(monkeypatch, [application])

    result = asyncio.run(telegram.start_bot())

    assert result is True
    assert application.initialize_calls == 2
    assert application.updater.polling_calls == [
        {"drop_pending_updates": True, "bootstrap_retries": 1}
    ]
    assert telegram._app is application
    assert telegram._running is True
    assert telegram._bot_loop is not None


def test_initialize_network_error_twice_cleans_partial_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_error = TimedOut("persistent temporary failure")
    application = FakeApplication([original_error, original_error])
    configure_applications(monkeypatch, [application])

    with pytest.raises(TimedOut) as exc_info:
        asyncio.run(telegram.start_bot())

    assert exc_info.value is original_error
    assert application.initialize_calls == 2
    assert application.shutdown_calls == 1
    assert application.bot.shutdown_calls == 1
    assert telegram._app is None
    assert telegram._running is False
    assert telegram._bot_loop is None


def test_invalid_token_is_not_retried_and_surfaces_safe_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = FakeApplication(
        [InvalidToken(f"The token `{FAKE_TOKEN}` was rejected by the server.")]
    )
    configure_applications(monkeypatch, [application])

    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(telegram.start_bot())

    assert application.initialize_calls == 1
    assert FAKE_TOKEN not in str(exc_info.value)
    assert application.shutdown_calls == 1
    assert application.bot.shutdown_calls == 1
    assert telegram._app is None
    assert telegram._running is False
    assert telegram._bot_loop is None


def test_command_registration_failure_does_not_stop_polling(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    application = FakeApplication(
        [None],
        command_error=TimedOut("command registration timed out"),
    )
    configure_applications(monkeypatch, [application])

    async def run_case() -> bool:
        result = await telegram.start_bot()
        await asyncio.sleep(0)
        return result

    with caplog.at_level(logging.WARNING, logger="row_bot.telegram"):
        result = asyncio.run(run_case())

    assert result is True
    assert application.events.index("polling") < application.events.index("commands")
    assert application.bot.command_calls == 1
    assert telegram._app is application
    assert telegram._running is True
    assert "Could not register bot commands (TimedOut)" in caplog.text


def test_fresh_application_can_start_after_failed_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_error = TimedOut("startup still unavailable")
    failed_application = FakeApplication([original_error, original_error])
    succeeding_application = FakeApplication([None])
    configure_applications(
        monkeypatch,
        [failed_application, succeeding_application],
    )

    async def run_case() -> bool:
        with pytest.raises(TimedOut):
            await telegram.start_bot()
        assert telegram._app is None
        return await telegram.start_bot()

    result = asyncio.run(run_case())

    assert result is True
    assert failed_application.bot.shutdown_calls == 1
    assert succeeding_application.initialize_calls == 1
    assert telegram._app is succeeding_application
    assert telegram._running is True
