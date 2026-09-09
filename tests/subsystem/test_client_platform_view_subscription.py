from __future__ import annotations

import asyncio
import threading

import pytest

from row_bot.projection.conversation import ConversationProjection
from row_bot.ui.legacy_adapter.view_subscription import LegacyViewSubscription


@pytest.mark.parametrize("large,trailing_live", [(False, True), (True, True), (True, False)])
def test_actual_page_reconciliation_replaces_user_once_and_preserves_history_control(large, trailing_live):
    """Execute the actual page callback without starting the application host."""
    import ast
    import logging
    from pathlib import Path
    import time
    from types import SimpleNamespace

    from row_bot.ui import transcript

    tree = ast.parse((Path(__file__).parents[2] / "src/row_bot/app.py").read_text(encoding="utf-8"))
    callback = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.FunctionDef) and node.name == "_refresh_chat_messages")

    class Container:
        def __init__(self):
            self.default_slot = SimpleNamespace(children=[])

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def clear(self):
            for child in list(self.default_slot.children):
                child.delete()

    container = Container()

    class Row:
        def __init__(self, content, *, history=False):
            self.content = content
            self._props = {"data-transcript-prefix-control": True} if history else {}
            self.deleted = False
            container.default_slot.children.append(self)

        def delete(self):
            self.deleted = True
            container.default_slot.children.remove(self)

    count, start = (120, 60) if large else (4, 0)
    old = [{"role": "user", "content": f"synthetic-{index}", "message_id": str(index)}
           for index in range(count)]
    new = [dict(item) for item in old]
    new[-1]["content"] = "canonical attachment or approval display"
    history = Row("Load earlier messages", history=True) if large else None
    for item in old[start:]:
        Row(item["content"])
    if trailing_live:
        Row("untracked live approval row")
    p = SimpleNamespace(chat_container=container, chat_scroll=None, transcript_thread_id="conversation",
                        transcript_rendered_keys=transcript.message_keys(old)[start:],
                        transcript_window_start=start, transcript_generation=0)
    state = SimpleNamespace(thread_id="conversation", messages=new)
    namespace = {name: getattr(transcript, name) for name in (
        "message_keys", "message_key", "rendered_window_matches", "common_key_prefix",
        "transcript_message_child_bounds", "choose_transcript_window", "TRANSCRIPT_MAX_CHUNK_MESSAGES",
        "TRANSCRIPT_CHUNK_TARGET_MS")}
    namespace.update(p=p, state=state, time=time, logger=logging.getLogger(__name__),
                     defer_ui=lambda work: work(), _ask_parent_to_use_agent_result=lambda *_: None,
                     add_chat_message=lambda msg, *_args, **_kwargs: Row(msg["content"]))
    exec(compile(ast.Module(body=[callback], type_ignores=[]), "<actual-page-callback>", "exec"), namespace)
    namespace["_refresh_chat_messages"]()
    children = container.default_slot.children
    assert [row.content for row in children if row is not history] == [item["content"] for item in new[start:]]
    assert p.transcript_rendered_keys == transcript.message_keys(new)[start:]
    if history is not None:
        assert children[0] is history and not history.deleted
        assert p.transcript_window_start == start


@pytest.mark.parametrize("outcome", ["resume", "disconnect", "selection", "timeout"])
def test_actual_reconnect_callback_waits_for_replay_and_fences_stale_page(outcome, monkeypatch):
    import ast
    from pathlib import Path
    from types import SimpleNamespace
    from row_bot import threads

    async def scenario():
        entered, replayed = asyncio.Event(), asyncio.Event()
        operations = []
        saved = []
        errors = []
        composer = SimpleNamespace(id=1, value="server draft")
        p = SimpleNamespace(chat_input=composer)
        state = SimpleNamespace(thread_id="a")
        gate = {"force_render": False, "epoch": 0}

        class Client:
            has_socket_connection = True

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        client = Client()

        async def roundtrip(_script, *, timeout):
            assert timeout == 5.0
            entered.set()
            await replayed.wait()
            operations.append("old replay delivered")
            if outcome == "timeout":
                raise TimeoutError("synthetic disconnected browser")
            return {"element_id": "c1", "value": "unsent browser draft"}

        subscription = SimpleNamespace(close=lambda: operations.append("close"),
                                       reconnect=lambda tid: operations.append("rebuild:" + tid))
        timer = SimpleNamespace(deactivate=lambda: operations.append("deactivate"),
                                activate=lambda: operations.append("activate"))
        monkeypatch.setattr(threads, "save_thread_draft", lambda *args, **kwargs: saved.append((args, kwargs)))
        namespace = dict(_projection_reconnect=gate, _projection_client=client,
                         _view_subscription=subscription, _projection_timer=timer, p=p, state=state,
                         ui=SimpleNamespace(run_javascript=roundtrip),
                         _projection_view_error=lambda *args: errors.append(args))
        tree = ast.parse((Path(__file__).parents[2] / "src/row_bot/app.py").read_text(encoding="utf-8"))
        callbacks = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                     and node.name in {"_reconnect_projection_view", "_disconnect_projection_view"}]
        assert len(callbacks) == 2
        exec(compile(ast.Module(body=callbacks, type_ignores=[]), "<actual-reconnect-callbacks>", "exec"), namespace)
        pending = asyncio.create_task(namespace["_reconnect_projection_view"]())
        await asyncio.wait_for(entered.wait(), 2)
        assert operations == ["close", "deactivate"]
        assert not gate["force_render"] and not saved
        if outcome == "disconnect":
            client.has_socket_connection = False
            namespace["_disconnect_projection_view"]()
        elif outcome == "selection":
            state.thread_id = "b"
            p.chat_input = SimpleNamespace(id=2, value="other conversation draft")
        replayed.set()
        await asyncio.wait_for(pending, 2)
        if outcome in {"resume", "selection"}:
            assert operations[-3:] == ["old replay delivered", "rebuild:" + state.thread_id, "activate"]
            assert gate["force_render"]
        else:
            assert not any(item.startswith("rebuild:") for item in operations)
            assert "activate" not in operations and not gate["force_render"]
        if outcome == "resume":
            assert composer.value == "unsent browser draft"
            assert saved == [(("a", "unsent browser draft"), {"source": "normal_chat"})]
        else:
            assert not saved
            assert composer.value == "server draft"
        assert errors == ([("a", "reconnect_unavailable")] if outcome == "timeout" else [])
    asyncio.run(scenario())


def test_each_viewer_receives_completed_checkpoint_without_consuming_other_view():
    async def scenario():
        projection = ConversationProjection("fixture-epoch")
        observed = [[], []]
        ready = [asyncio.Event(), asyncio.Event()]
        viewers = []
        for index in range(2):
            def apply(target, messages, index=index):
                observed[index].append((target, messages))
                ready[index].set()
            viewer = LegacyViewSubscription(projection, lambda _: [{"content": "durable final"}], apply)
            viewer.observe("first")
            viewers.append(viewer)
        projection.install_checkpoint("first", "checkpoint-1", [])
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in ready)), 2)
        assert observed == [[("first", [{"content": "durable final"}])]] * 2
        viewers[0].close()
        ready[1].clear()
        projection.install_checkpoint("first", "checkpoint-2", [])
        await asyncio.wait_for(ready[1].wait(), 2)
        assert len(observed[0]) == 1
        assert len(observed[1]) == 2
        ready[0].clear()
        viewers[0].reconnect("first")
        await asyncio.wait_for(ready[0].wait(), 2)
        assert len(observed[0]) == 2
        viewers[0].close()
        viewers[1].close()
    asyncio.run(scenario())


def test_switch_during_checkpoint_read_cannot_apply_to_new_conversation():
    async def scenario():
        projection = ConversationProjection("fixture-epoch")
        entered, release = threading.Event(), threading.Event()
        ready = asyncio.Event()
        observed = []
        def load(target):
            if target == "first":
                entered.set()
                assert release.wait(2)
            return [{"content": target}]
        def apply(target, messages):
            observed.append((target, messages))
            ready.set()
        viewer = LegacyViewSubscription(projection, load, apply)
        viewer.observe("first")
        projection.install_checkpoint("first", "checkpoint-1", [])
        assert await asyncio.to_thread(entered.wait, 2)
        viewer.observe("second")
        projection.install_checkpoint("second", "checkpoint-2", [])
        release.set()
        await asyncio.wait_for(ready.wait(), 2)
        assert observed == [("second", [{"content": "second"}])]
        viewer.close()
    asyncio.run(scenario())


def test_running_producer_checkpoint_waits_for_terminal_event():
    async def scenario():
        projection = ConversationProjection("fixture-epoch")
        ready = asyncio.Event()
        loads = []
        def load(target):
            loads.append(target)
            return []
        viewer = LegacyViewSubscription(projection, load, lambda *_: ready.set())
        viewer.observe("first")
        projection.publish("first", "generation.state", {"status": "running"})
        projection.install_checkpoint("first", "checkpoint-1", [])
        barrier = asyncio.Event()
        asyncio.get_running_loop().call_soon(barrier.set)
        await barrier.wait()
        assert not loads
        projection.publish("first", "generation.state", {"status": "completed"})
        await asyncio.wait_for(ready.wait(), 2)
        assert loads == ["first"]
        viewer.close()
    asyncio.run(scenario())


def test_checkpoint_reload_waits_for_legacy_renderer_to_finish_final_row():
    async def scenario():
        projection = ConversationProjection("fixture-epoch")
        ready = asyncio.Event()
        renderer_finished = False
        loads = []
        def load(target):
            loads.append(target)
            return []
        viewer = LegacyViewSubscription(projection, load, lambda *_: ready.set(),
                                        ready=lambda _: renderer_finished)
        viewer.observe("first")
        projection.install_checkpoint("first", "checkpoint-1", [])
        barrier = asyncio.Event()
        asyncio.get_running_loop().call_soon(barrier.set)
        await barrier.wait()
        assert not loads
        renderer_finished = True
        viewer.observe("first")
        await asyncio.wait_for(ready.wait(), 2)
        assert loads == ["first"]
        viewer.close()
    asyncio.run(scenario())


def test_failed_checkpoint_load_has_bounded_retries_and_reconnect_retries_fresh():
    async def scenario():
        projection = ConversationProjection("fixture-epoch")
        attempts = []
        errors = []
        finished = asyncio.Event()
        def load(target):
            attempts.append(target)
            if len(attempts) <= 3:
                raise OSError("synthetic private path must not be displayed")
            return []
        viewer = LegacyViewSubscription(projection, load, lambda *_: finished.set(),
                                        on_error=lambda *args: errors.append(args))
        projection.install_checkpoint("first", "checkpoint-1", [])
        viewer.observe("first")
        for _ in range(3):
            barrier = asyncio.Event()
            asyncio.get_running_loop().call_soon(barrier.set)
            await barrier.wait()
            if viewer._task:
                await asyncio.wait_for(viewer._task, 2)
            viewer.observe("first")
        assert attempts == ["first"] * 3
        assert errors == [("first", "checkpoint_unavailable")]
        viewer.close()
        viewer.reconnect("first")
        await asyncio.wait_for(finished.wait(), 2)
        assert len(attempts) == 4
        viewer.close()
    asyncio.run(scenario())
