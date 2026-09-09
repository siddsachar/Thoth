"""Attachments cross admission and enter the actual provider input once."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from tests.contracts.client_platform.test_headless_lifecycle import platform, command  # noqa: F401
from tests.helpers.client_platform_fakes import ScriptedAgentStream, CheckpointCommit


def test_actual_submission_consumes_text_data_and_configured_vision_after_admission(platform, monkeypatch):
    from row_bot.application.attachments import register_attachment
    from row_bot.application.attachment_context import current_caches
    from row_bot import vision_runtime, threads
    calls = []
    def analyze(data, prompt):
        assert platform.registry.active("conversation-a")
        calls.append((data, prompt))
        return "Synthetic green rectangle"
    monkeypatch.setattr(vision_runtime, "get_vision_service", lambda: SimpleNamespace(enabled=True, analyze=analyze))
    monkeypatch.setattr("row_bot.file_context.file_budget", lambda *_: 10000)
    files = {"readme.txt": b"Synthetic attached text", "data.csv": b"x,y\n1,2\n", "image.png": b"\x89PNG\r\n\x1a\nfixture"}
    refs = [register_attachment("conversation-a", name, data)["attachment_ref"] for name, data in files.items()]
    fake = ScriptedAgentStream((CheckpointCommit((AIMessage(content="Complete", id="attachment-final"),), "attachment-final"), ("done", "Complete")))
    def provider(text, tools, config, **kwargs):
        assert "Synthetic attached text" in text and "Synthetic green rectangle" in text
        assert "data.csv" in text and "ALREADY ANALYZED" in text
        assert current_caches().data["data.csv"] == files["data.csv"]
        assert current_caches().images["image.png"] == files["image.png"]
        yield from fake.stream(text, tools, config, **kwargs)
    platform.stream_factory = provider
    receipt = platform.execute(owner_id="fixture", idempotency_key="attached", target="conversation-a",
        command=command("conversation.submit", "attached", {"text": "Read these files", "attachment_refs": refs,
            "submission_id": "attachment-input", "model_selection": {"provider_id": "fixture", "model_ref": "fixture::model"}}))
    handle = platform.registry.get(receipt["execution_id"])
    assert handle.producer_done.wait(20) and handle.status == "completed"
    messages = threads.get_latest_checkpoint_messages("conversation-a")
    assert len([message for message in messages if message.id == "attachment-input"]) == 1
    assert "Synthetic attached text" in messages[0].content
    assert len(calls) == 1 and len(fake.calls) == 1


def test_attachment_preflight_rejects_aggregate_before_reading_bytes(platform, monkeypatch):
    from row_bot.application import attachments
    monkeypatch.setattr(attachments, "inspect_attachment", lambda *_: {"size_bytes": 25 * 1024 * 1024})
    monkeypatch.setattr(attachments, "read_attachment", lambda *_: pytest.fail("aggregate must reject before full read"))
    with pytest.raises(ValueError, match="payload_too_large"):
        platform._start("conversation-a", {"attachment_refs": ["conversation-a:fixture"] * 5,
                        "model_selection": {"provider_id": "fixture", "model_ref": "fixture::model"}}, resume=False)
    assert not platform.registry.active()


def test_stop_before_producer_entry_finalizes_admission_without_attachment_reads(platform, monkeypatch):
    from row_bot.application.attachments import register_attachment
    from row_bot.application import attachments
    reference = register_attachment("conversation-a", "readme.txt", b"Synthetic")["attachment_ref"]
    launch = platform.registry.launch
    def cancelled_launch(handle, producer, **kwargs):
        handle.cancel_scope.stop_event.set()
        return launch(handle, producer, **kwargs)
    monkeypatch.setattr(platform.registry, "launch", cancelled_launch)
    monkeypatch.setattr(attachments, "read_attachment", lambda *_: pytest.fail("cancelled generation must not read attachment bytes"))
    receipt = platform._start("conversation-a", {"attachment_refs": [reference], "text": "Synthetic",
                              "model_selection": {"provider_id": "fixture", "model_ref": "fixture::model"}}, resume=False)
    handle = platform.registry.get(receipt["execution_id"])
    assert handle.producer_done.wait(10) and handle.status == "stopped"
    from row_bot.runtime import admissions
    assert admissions.queued_submission_ids("conversation-a") == []
