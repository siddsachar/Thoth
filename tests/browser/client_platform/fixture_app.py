"""Disposable real NiceGUI app with scripted external calls for browser QA.

Run only through run_browser.py. This is not an alternate application service:
the real app entry, stores, runtime, projection and NiceGUI renderer are used.
External provider/tool calls are scripted, with explicit producer barriers;
native desktop notification and sound outputs are replaced by counted sinks.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import runpy
import threading
from typing import Any

from fastapi import Header, HTTPException
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from nicegui import app

from tests.helpers.client_platform_fakes import fixture_id


DATA = Path(os.environ["ROW_BOT_DATA_DIR"]).resolve()
TOKEN = os.environ["P1_BROWSER_CONTROL_TOKEN"]
if os.environ.get("ROW_BOT_TEST_MODE") != "1" or not TOKEN or not DATA.name.startswith("data"):
    raise RuntimeError("Browser fixture requires a disposable runner environment")
DATA.mkdir(parents=True, exist_ok=True)


def seed() -> None:
    from scripts.docs import seed_real_app_demo_data as seeder
    from row_bot.docs_capture import default_docs_capture_demo_state
    from row_bot.threads import create_thread

    seeder._seed_app_config(DATA, first_run=False)
    from row_bot.tools.registry import set_tool_config
    set_tool_config("filesystem", "workspace_root", str(DATA / "attachment-workspace"))
    state = default_docs_capture_demo_state()
    state["messages"] = []
    state["threads"] = []
    state["thread_name"] = "Phase 1 browser fixture"
    state["thread_id"] = "p1-browser-a"
    for suffix in ("a", "b"):
        create_thread(thread_id="p1-browser-" + suffix, name="Phase 1 conversation " + suffix.upper(),
                      name_source="manual", seed_default_skills=False)
    # Reuse only safe existing artifact seed and a plain local workspace.
    # The documentation Git-init/commit and external-integration seeds are excluded.
    seeder._seed_designer_project(state)
    from row_bot.developer.state import DeveloperWorkspace
    from row_bot.developer.storage import save_workspace

    workspace = DATA / "fixture-workspace"
    workspace.mkdir(exist_ok=True)
    (workspace / "fixture.txt").write_text("Synthetic local fixture\n", encoding="utf-8")
    save_workspace(DeveloperWorkspace(id="p1-browser-workspace", name="Phase 1 workspace", path=str(workspace)))
    state["developer"]["workspace_id"] = "p1-browser-workspace"
    (DATA / "docs_real_ui_demo_state.json").write_text(json.dumps(state), encoding="utf-8")


_lock = threading.Lock()
_calls: list[dict[str, Any]] = []
_barriers: dict[str, threading.Event] = {}
_sequences: dict[str, int] = {}
_notification_outputs = {"desktop_suppressed": 0, "sound_suppressed": 0}


def _suppress_desktop_notification(title: str, message: str) -> None:
    with _lock:
        _notification_outputs["desktop_suppressed"] += 1


def _suppress_notification_sound(sound: str) -> None:
    with _lock:
        _notification_outputs["sound_suppressed"] += 1


def _record(kind: str, config: dict, case: str) -> dict:
    configurable = config["configurable"]
    thread_id = str(configurable["thread_id"])
    with _lock:
        sequence = _sequences.get(thread_id, 0) + 1
        _sequences[thread_id] = sequence
        call = {"kind": kind, "conversation_id": thread_id, "case": case,
                "sequence": sequence, "entered": True, "quiesced": False,
                "generation_id": str(configurable.get("generation_id") or ""),
                "submission_id": str(configurable.get("platform_submission_id") or ""),
                "barrier_id": fixture_id(f"browser:{thread_id}:{sequence}:barrier")}
        _calls.append(call)
        _barriers[call["barrier_id"]] = threading.Event()
        return call


def stream(text: str, enabled_tools: list[str], config: dict, *, stop_event=None):
    from row_bot.threads import append_checkpoint_messages, get_latest_checkpoint_messages, get_latest_checkpoint_revision

    case = "approval" if "approval" in text else "stop" if "stop" in text else "recovery"
    call = _record("submit", config, case)
    thread_id = call["conversation_id"]
    identity = f"browser:{thread_id}:{call['sequence']}"
    input_id = call["submission_id"] or fixture_id(identity + ":user")
    existing = get_latest_checkpoint_messages(thread_id)
    if not any(str(message.id) in {input_id, "user:submission:" + input_id} for message in existing):
        if not append_checkpoint_messages(thread_id, [HumanMessage(content=text, id=input_id)]):
            raise AssertionError("Browser fixture input checkpoint was not admitted")
    try:
        yield "thinking", None
        if case == "approval":
            tool_id = fixture_id(identity + ":tool")
            append_checkpoint_messages(thread_id, [AIMessage(content="", id=fixture_id(identity + ":assistant-tool"),
                                      tool_calls=[{"id": tool_id, "name": "fixture_action", "args": {}}])])
            yield "tool_call", "fixture_action"
            yield "interrupt", [{"__interrupt_id": fixture_id(identity + ":approval"), "tool": "fixture_action",
                                  "description": "Approve the synthetic fixture action", "args": {}}]
            return
        partial = "Synthetic stream is active."
        yield "token", partial
        barrier = stop_event if case == "stop" and stop_event is not None else _barriers[call["barrier_id"]]
        if not barrier.wait(45):
            raise TimeoutError("Browser fixture producer was never released")
        if stop_event is not None and stop_event.is_set():
            return
        final = partial + " Synthetic stream settled."
        yield "token", " Synthetic stream settled."
        native_id = fixture_id(identity + ":assistant-final")
        append_checkpoint_messages(thread_id, [AIMessage(content=final, id=native_id)])
        yield "output_binding", {"native_message_id": native_id, "checkpoint_revision": get_latest_checkpoint_revision(thread_id)}
        yield "done", final
    finally:
        call["quiesced"] = True


def resume(enabled_tools: list[str], config: dict, approved: bool, *, interrupt_ids=None, stop_event=None):
    from row_bot.threads import append_checkpoint_messages, get_latest_checkpoint_messages, get_latest_checkpoint_revision

    call = _record("resume", config, "approval")
    thread_id = call["conversation_id"]
    try:
        tool = next((message for message in reversed(get_latest_checkpoint_messages(thread_id))
                     if getattr(message, "tool_calls", None)), None)
        if tool:
            append_checkpoint_messages(thread_id, [ToolMessage(content="Synthetic approval accepted" if approved else "Synthetic approval rejected",
                                      tool_call_id=tool.tool_calls[0]["id"], id=fixture_id("browser:" + thread_id + ":tool-result"))])
        yield "tool_done", "fixture_action"
        final = "Synthetic approval resumed." if approved else "Synthetic approval rejected."
        yield "token", final
        native_id = fixture_id(f"browser:{thread_id}:{call['sequence']}:approval-final")
        append_checkpoint_messages(thread_id, [AIMessage(content=final, id=native_id)])
        yield "output_binding", {"native_message_id": native_id, "checkpoint_revision": get_latest_checkpoint_revision(thread_id)}
        yield "done", final
    finally:
        call["quiesced"] = True


def _authorize(value: str) -> None:
    import secrets
    if not secrets.compare_digest(value, TOKEN):
        raise HTTPException(status_code=403)


@app.get("/__p1_fixture/state")
def fixture_state(x_fixture_token: str = Header(default="")) -> dict:
    _authorize(x_fixture_token)
    from row_bot.threads import get_latest_checkpoint_messages
    from row_bot.ui.state import _active_generations

    with _lock:
        calls = [dict(call) for call in _calls]
        notification_outputs = dict(_notification_outputs)
    return {"calls": calls, "external_calls": 0,
            "notification_outputs": notification_outputs,
            "legacy_generations": {str(key): str(value.status) for key, value in _active_generations.items()},
            "checkpoint_ids": {thread_id: [str(message.id) for message in get_latest_checkpoint_messages(thread_id)]
                               for thread_id in ("p1-browser-a", "p1-browser-b")}}


@app.post("/__p1_fixture/release/{barrier_id}")
def release(barrier_id: str, x_fixture_token: str = Header(default="")) -> dict:
    _authorize(x_fixture_token)
    with _lock:
        barrier = _barriers.get(barrier_id)
    if barrier is None:
        raise HTTPException(status_code=404)
    barrier.set()
    return {"released": True}


if __name__ == "__main__":
    from row_bot import notifications

    # Keep notification state, buddy events and in-app toasts real. Install only
    # the native output sinks before seeding or importing the real application.
    notifications._desktop_notify = _suppress_desktop_notification
    notifications._play_sound = _suppress_notification_sound
    seed()
    import row_bot.agent as agent
    from row_bot.providers import readiness
    from row_bot.providers.models import TransportMode

    def fake_agent_readiness(model_ref, **kwargs):
        return readiness.AgentReadinessResult(ready=True, provider_id="fixture", model_id="scripted",
                   runtime_model="scripted", selection_ref=str(model_ref), transport=TransportMode.OLLAMA_CHAT,
                   context_window=131072, tool_calling=True, tool_round_trip=True, streaming=True,
                   credential_status="fixture", capability_source="fixture", confidence="high")

    def fake_chat_readiness(model_ref, **kwargs):
        return readiness.ChatReadinessResult(ready=True, provider_id="fixture", model_id="scripted",
                   runtime_model="scripted", selection_ref=str(model_ref), transport=TransportMode.OLLAMA_CHAT,
                   context_window=131072, streaming=True, credential_status="fixture",
                   capability_source="fixture", confidence="high")

    agent.stream_agent = stream
    agent.resume_stream_agent = resume
    readiness.evaluate_agent_readiness = fake_agent_readiness
    readiness.evaluate_chat_readiness = fake_chat_readiness
    runpy.run_module("row_bot.app", run_name="__main__")
