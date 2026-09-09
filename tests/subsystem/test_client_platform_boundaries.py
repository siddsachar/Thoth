from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

from scripts.check_client_platform_boundaries import boundary_paths, inspect_source

pytestmark = pytest.mark.subsystem


@pytest.mark.parametrize("statement", [
    "from nicegui import run", "from row_bot import ui", "import row_bot.ui.state",
    'importlib.import_module("row_bot.designer.editor")', '__import__("webview")',
])
def test_presentation_dependency_fails_headless_boundary(statement):
    assert any(item.code == "CP001" for item in inspect_source(statement))


def test_public_boundary_type_annotations_are_required():
    failures = inspect_source("def submit(text):\n    return text\n")
    assert [item.code for item in failures] == ["CP002"]
    assert inspect_source("def submit(text: str) -> str:\n    return text\n") == []


def test_real_headless_scopes_have_no_layer_or_annotation_regression():
    violations = {path.as_posix(): inspect_source(path.read_text(encoding="utf-8")) for path in boundary_paths()}
    assert not {path: items for path, items in violations.items() if items}


def test_legacy_message_helpers_reexport_one_pure_implementation():
    from row_bot import message_projection
    from row_bot.ui import helpers
    assert helpers.langchain_messages_to_ui_messages is message_projection.langchain_messages_to_ui_messages
    assert helpers.strip_file_context is message_projection.strip_file_context


def test_real_headless_generation_and_shutdown_with_presentation_imports_unavailable(tmp_path):
    script = textwrap.dedent('''
        import asyncio
        import importlib.abc
        import sys
        from uuid import uuid4
        class NoPresentation(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path, target=None):
                if fullname == "nicegui" or fullname.startswith(("nicegui.", "row_bot.ui")):
                    raise ImportError("Presentation is unavailable in this headless host")
        sys.meta_path.insert(0, NoPresentation())
        from row_bot.message_projection import langchain_messages_to_ui_messages
        from langchain_core.messages import AIMessage
        assert langchain_messages_to_ui_messages([AIMessage(content="Fixture", id="native-fixture")])[0]["checkpoint_message_id"] == "native-fixture"
        from row_bot import threads
        from row_bot.application.client_platform import ClientPlatformService
        from row_bot.application.lifecycle import ApplicationLifecycle
        from row_bot.api.v1.routes import create_client_platform_app
        native_id = str(uuid4())
        def producer(text, enabled_tools, config, **kwargs):
            yield "token", "Synthetic headless result"
            tid = config["configurable"]["thread_id"]
            assert threads.append_checkpoint_messages(tid, [AIMessage(content="Synthetic headless result", id=native_id)])
            yield "output_binding", {"native_message_id": native_id, "checkpoint_revision": threads.get_latest_checkpoint_revision(tid)}
            yield "done", "Synthetic headless result"
        service = ClientPlatformService(stream_factory=producer)
        create_client_platform_app(service)
        lifecycle = ApplicationLifecycle(registry=service.registry)
        def execute(kind, target, payload, revision="0"):
            return service.execute(owner_id="fixture-owner", idempotency_key=str(uuid4()), target=target,
                command={"command_id": str(uuid4()), "client_session_id": str(uuid4()),
                         "type": kind, "expected_revision": revision, "payload": payload})
        async def run():
            await lifecycle.startup()
            created = execute("conversation.create", "new", {"title": "Headless fixture"})
            tid = created["conversation_id"]
            receipt = execute("conversation.submit", tid, {"submission_id": str(uuid4()), "text": "Synthetic input",
                "attachment_refs": [], "model_selection": {"provider_id": "fixture", "model_ref": "fixture/model"}})
            handle = service.registry.get(receipt["execution_id"])
            assert await asyncio.to_thread(handle.producer_done.wait, 10)
            assert handle.status == "completed", handle.status
            rows = service.transcript(tid)["rows"]
            assert any(row.get("message_id") == native_id for row in rows)
            assert (await lifecycle.shutdown())["status"] == "quiesced"
        asyncio.run(run())
        assert "nicegui" not in sys.modules
        print("Headless generation and shutdown passed without presentation imports")
    ''')
    env = {**os.environ, "ROW_BOT_DATA_DIR": str(tmp_path / "headless-data"), "ROW_BOT_TEST_MODE": "1"}
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
