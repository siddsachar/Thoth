"""Durable interruption and deletion barriers must survive response/process loss."""
from __future__ import annotations

from langchain_core.messages import HumanMessage
import pytest

from tests.contracts.client_platform.test_headless_lifecycle import command, platform as platform, submit
from tests.contracts.client_platform.test_protocol_boundaries import client as client
from tests.helpers.client_platform_fakes import ScriptedAgentStream, StreamBarrier, fixture_id

pytestmark = pytest.mark.contract


@pytest.mark.parametrize("cut", ["reserve", "checkpoint", "admitted", "started"])
def test_f_p09_restart_reconciles_every_admission_cut_without_dispatch(platform, cut, monkeypatch):
    from row_bot.runtime import admissions
    from row_bot.threads import append_checkpoint_messages, get_latest_checkpoint_revision

    # Explicit process-loss fixture: an epoch change alone does not prove that
    # another process's durable execution lease has stopped running.
    monkeypatch.setattr(admissions, "_owner_alive", lambda row: False)
    prepared = admissions.reserve("conversation-a", fixture_id("interrupted-input"), fixture_id("interrupted-generation"))
    if cut != "reserve":
        assert append_checkpoint_messages("conversation-a", [HumanMessage(content="Synthetic accepted input", id=prepared["submission_id"])])
    if cut in {"admitted", "started"}:
        admissions.admit(prepared["pass_id"], get_latest_checkpoint_revision("conversation-a"))
    if cut == "started":
        admissions.start(prepared["pass_id"], fixture_id("old-execution"), fixture_id("old-epoch"))
    admissions.recover(fixture_id("replacement-epoch"))
    with admissions.transaction() as connection:
        row = connection.execute("SELECT state FROM generation_passes WHERE pass_id=?", (prepared["pass_id"],)).fetchone()
    assert row[0] not in {"admitting", "admitted", "started"}
    assert not platform.registry.active()
    # A new explicit owner request may proceed; recovery itself never executes.
    replacement = admissions.reserve("conversation-a", fixture_id("replacement-input"), fixture_id("replacement-generation"))
    assert replacement["pass_id"] != prepared["pass_id"]


def test_f_p09_delete_closes_admission_before_blocked_producer_acknowledges(platform):
    from row_bot.application.client_platform import ClientPlatformError
    from row_bot.runtime import admissions

    barrier = StreamBarrier()
    fake = ScriptedAgentStream((("token", "Synthetic active work"), barrier))
    receipt = submit(platform, fake, "delete-blocked")
    handle = platform.registry.get(receipt["execution_id"])
    try:
        assert barrier.entered.wait(10)
        result = platform.execute(owner_id="fixture-owner", idempotency_key=fixture_id("delete:key"),
                                  command=command("conversation.delete", "delete"), target="conversation-a")
        assert result["status"] == "DeleteBlocked"
        assert not handle.producer_done.is_set()
        assert admissions.deletion_state("conversation-a") != "active"
    finally:
        barrier.release.set()
        assert handle.producer_done.wait(10)
    another = ScriptedAgentStream((("done", "Must never dispatch"),))
    with pytest.raises(ClientPlatformError, match="conversation_deleting"):
        submit(platform, another, "after-delete")
    assert not another.calls


def test_f_p04_known_rejection_replays_known_rejection_not_uncertain(client):
    request = command("conversation.rename", "known-rejection", {"title": "Must not mutate"}, revision="99")
    request["client_session_id"] = client.headers["X-Client-Session"]
    headers = {"Idempotency-Key": fixture_id("known-rejection:key")}
    first = client.post("/api/v1/conversations/conversation-a/commands", json=request, headers=headers)
    retry = client.post("/api/v1/conversations/conversation-a/commands", json=request, headers=headers)
    assert first.status_code == retry.status_code == 409
    assert first.json()["code"] == retry.json()["code"] == "revision_conflict"
    assert first.json().get("current_revision") == retry.json().get("current_revision") == "0"
