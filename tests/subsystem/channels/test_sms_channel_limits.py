from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest


pytestmark = pytest.mark.subsystem


class FakeSMSRequest:
    def __init__(self, *, sender: str, body: str, sid: str = "sid-1") -> None:
        self.headers = {"content-length": "0"}
        self.client = SimpleNamespace(host="127.0.0.1")
        self.url = "https://example.invalid/sms"
        self._form = {"From": sender, "Body": body, "MessageSid": sid}

    async def form(self) -> dict[str, str]:
        return dict(self._form)


def test_sms_capabilities_stay_final_text_only() -> None:
    from row_bot.channels.sms import SMSChannel

    caps = SMSChannel().capabilities

    assert caps.buttons is False
    assert caps.streaming is False
    assert caps.typing is False
    assert caps.reactions is False
    assert caps.slash_commands is True


def test_sms_split_keeps_chunks_within_limit() -> None:
    from row_bot.channels.sms import SMS_MAX_LEN, _split_sms

    chunks = _split_sms("word " * 500)

    assert len(chunks) > 1
    assert all(len(chunk) <= SMS_MAX_LEN for chunk in chunks)
    assert "".join(chunk.replace("\n", "") for chunk in chunks).replace(" ", "").startswith("word")


def test_sms_pending_interrupt_rejects_unrecognized_text_without_agent_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from row_bot.channels import sms

    replies: list[str] = []

    monkeypatch.setenv("SMS_INSECURE_NO_SIGNATURE", "true")
    monkeypatch.setattr(sms, "_running", True)
    monkeypatch.setattr(sms, "_is_authorised", lambda _phone: True)
    monkeypatch.setattr(sms, "_send_reply", lambda _phone, text: replies.append(text))
    monkeypatch.setattr(
        sms,
        "_run_agent_sync",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("agent should not run")),
    )
    sms._rate_limits.clear()
    sms._seen_sids.clear()
    with sms._pending_lock:
        sms._pending_interrupts.clear()
        sms._pending_interrupts["+15551234567"] = {
            "data": {"tool": "shell", "description": "Run command"},
            "config": {"configurable": {"thread_id": "sms-thread"}},
        }

    response = asyncio.run(
        sms._handle_inbound_sms(
            FakeSMSRequest(sender="+15551234567", body="maybe", sid="sid-pending")
        )
    )

    assert response.status_code == 200
    assert replies == ["Approval pending. Reply YES or NO."]
    with sms._pending_lock:
        assert "+15551234567" in sms._pending_interrupts

    with sms._pending_lock:
        sms._pending_interrupts.clear()


def test_sms_preserves_compaction_notice_as_separate_delivery(monkeypatch) -> None:
    from row_bot.channels import sms
    from row_bot.tools import registry as tool_registry

    fake_agent = SimpleNamespace(
        stream_agent=lambda *_args, **_kwargs: iter([
            ("compaction_succeeded", {"event_id": 17, "display_copy": "Context compacted"}),
            ("token", "answer"),
            ("done", "answer"),
        ])
    )
    monkeypatch.setattr(sms, "_agent_mod", lambda: fake_agent)
    monkeypatch.setattr(tool_registry, "get_enabled_tools", lambda: [])

    answer, interrupt, notices = sms._run_agent_sync(
        "hello",
        {"configurable": {"thread_id": "sms-thread"}},
    )

    assert answer == "answer"
    assert interrupt is None
    assert notices == [{"event_id": 17, "display_copy": "Context compacted"}]
