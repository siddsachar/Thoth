from __future__ import annotations

import contextvars
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest

from row_bot.application import attachment_context as context

pytestmark = pytest.mark.subsystem


def test_preparation_uses_existing_materializer_vision_and_data_owner(monkeypatch):
    from row_bot import vision_runtime
    from row_bot.tools import chart_tool, image_gen_tool, video_gen_tool

    calls = []
    files = [{"name": "plot.png", "data": b"image"}, {"name": "plot.csv", "data": b"x,y\n1,2"}]
    vision = SimpleNamespace(enabled=True, analyze=lambda data, prompt: calls.append((data, prompt)) or "A blue graph")
    monkeypatch.setattr(vision_runtime, "get_vision_service", lambda: vision)
    def materialize(items):
        calls.append("materialize")
        for item in items:
            item["workspace_path"] = "Received Files/" + item["name"]
        return [{"name": item["name"]} for item in items]
    monkeypatch.setattr(context, "materialize_chat_attachments", materialize)
    monkeypatch.setattr("row_bot.file_context.file_budget", lambda model: 10000)
    monkeypatch.setattr("row_bot.data_reader.read_data_file", lambda *args, **kwargs: "1 row, columns x, y")
    monkeypatch.setattr(image_gen_tool, "_image_cache", {"plot.png": b"other-view"})
    monkeypatch.setattr(chart_tool, "_attachment_cache", {"plot.csv": b"x,y\n9,9"})
    with context.prepared_attachments("first", files, model_ref="fixture:model") as text:
        assert calls[0] == "materialize"
        assert calls[1][0] == b"image"
        assert "A blue graph" in text and "ALREADY ANALYZED" in text
        assert "Received Files/plot.csv" in text and "1 row" in text
        assert text.startswith("<row_bot_attachment_context>")
        assert image_gen_tool._resolve_image_source("plot.png") == b"image"
        assert video_gen_tool._resolve_image_source("plot.png") == b"image"
        assert chart_tool._load_data("plot.csv", None).iloc[0]["x"] == 1
    assert image_gen_tool._image_cache["plot.png"] == b"other-view"
    assert chart_tool._attachment_cache["plot.csv"] == b"x,y\n9,9"
    assert context.current_caches() is None


def test_empty_execution_isolates_legacy_and_nested_execution_media(monkeypatch):
    from row_bot.tools import image_gen_tool, video_gen_tool

    monkeypatch.setattr(image_gen_tool, "_image_cache", {"__last_generated__": b"legacy"})
    monkeypatch.setattr(image_gen_tool, "_last_generated_image", "legacy-pending")
    monkeypatch.setattr(video_gen_tool, "_last_generated_video", {"filename": "legacy.mp4"})
    with context.prepared_attachments("outer", []) as text:
        assert text == ""
        assert image_gen_tool.get_and_clear_last_image() is None
        assert video_gen_tool.get_and_clear_last_video() is None
        with pytest.raises(ValueError, match="No previously generated"):
            image_gen_tool._resolve_image_source("last")
        image_gen_tool._execution_image_cache()["__last_generated__"] = b"outer"
        image_gen_tool._set_pending_image("outer-pending")
        video_gen_tool._set_pending_video({"filename": "outer.mp4"})
        with context.prepared_attachments("inner", []):
            assert image_gen_tool.get_and_clear_last_image() is None
            assert video_gen_tool.get_and_clear_last_video() is None
        assert image_gen_tool.get_and_clear_last_image() == "outer-pending"
        assert video_gen_tool.get_and_clear_last_video() == {"filename": "outer.mp4"}
        assert contextvars.copy_context().run(image_gen_tool._resolve_image_source, "last") == b"outer"
    assert image_gen_tool.get_and_clear_last_image() == "legacy-pending"
    assert video_gen_tool.get_and_clear_last_video() == {"filename": "legacy.mp4"}


def test_preparation_failure_restores_execution_context(monkeypatch):
    monkeypatch.setattr(context, "materialize_chat_attachments", lambda files: [{"error": "private path"}])
    with pytest.raises(ValueError, match="^attachment_materialization_failed$"):
        with context.prepared_attachments("fixture", [{"name": "file.txt", "data": b"data"}]):
            pytest.fail("Failed materialization must not dispatch")
    assert context.current_caches() is None


def test_simultaneous_execution_caches_do_not_cross_conversations():
    from row_bot.tools import image_gen_tool
    barrier = Barrier(2)
    def run(identifier):
        with context.prepared_attachments(identifier, []):
            image_gen_tool._execution_image_cache()["same.png"] = identifier.encode()
            image_gen_tool._set_pending_image(identifier)
            barrier.wait(timeout=10)
            return (image_gen_tool._resolve_image_source("same.png"),
                    image_gen_tool.get_and_clear_last_image())
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(run, ["first", "second"])) == [(b"first", "first"), (b"second", "second")]


def test_parallel_tool_media_slots_preserve_call_identity_and_shared_inputs():
    from row_bot.tools import image_gen_tool, video_gen_tool
    barrier = Barrier(2)
    def call(identifier):
        with context.tool_attachment_scope() as current:
            assert current.conversation_id == "fixture"
            assert image_gen_tool._resolve_image_source("input.png") == b"input"
            image_gen_tool._set_pending_image(identifier)
            video_gen_tool._set_pending_video({"filename": identifier})
            barrier.wait(timeout=10)
            return image_gen_tool.get_and_clear_last_image(), video_gen_tool.get_and_clear_last_video()
    with context.prepared_attachments("fixture", []):
        image_gen_tool._execution_image_cache()["input.png"] = b"input"
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = [pool.submit(contextvars.copy_context().run, call, value) for value in ("first", "second")]
            assert [task.result() for task in pending] == [
                ("first", {"filename": "first"}), ("second", {"filename": "second"})]
        assert image_gen_tool.get_and_clear_last_image() is None
        assert video_gen_tool.get_and_clear_last_video() is None
        assert image_gen_tool._resolve_image_source("input.png") == b"input"
    with context.tool_attachment_scope() as current:
        assert current is None


def test_scoped_image_and_video_saves_use_execution_owned_unique_output(monkeypatch):
    import base64
    from row_bot.application import generated_media
    from row_bot.tools import image_gen_tool, video_gen_tool
    calls = []
    def save(conversation_id, data, *, prefix, extension):
        calls.append((conversation_id, data, prefix, extension))
        return "fixture-output"
    monkeypatch.setattr(generated_media, "save_generated_output", save)
    with context.prepared_attachments("fixture", []):
        assert image_gen_tool._save_image_to_disk(base64.b64encode(b"image").decode()) == "fixture-output"
        assert video_gen_tool._save_video_to_disk(b"video") == "fixture-output"
    assert calls == [("fixture", b"image", "gen", "png"), ("fixture", b"video", "vid", "mp4")]


def test_materialization_uses_bound_workspace_and_revalidates(monkeypatch, tmp_path):
    from row_bot.channels import media
    from row_bot import conversation_resources as resources
    from row_bot.developer import storage

    calls = []
    workspace = tmp_path / "bound"
    source = tmp_path / "source.txt"
    source.write_text("fixture")
    def resolve(kind):
        calls.append(kind)
        return SimpleNamespace(resource_id="bound-workspace")
    monkeypatch.setattr(resources, "current_execution_context", lambda: SimpleNamespace(resolve=resolve))
    monkeypatch.setattr(storage, "get_workspace", lambda identifier: SimpleNamespace(path=str(workspace)))
    relative = media.copy_to_workspace(source, "attachment.txt")
    assert (workspace / relative).read_text() == "fixture"
    assert calls == ["workspace"]
    monkeypatch.setattr(storage, "get_workspace", lambda identifier: None)
    with pytest.raises(resources.ResourceError, match="resource_unavailable"):
        media.copy_to_workspace(source, "attachment.txt")
