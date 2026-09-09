from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from threading import Barrier

import pytest

from row_bot.application import attachments, generated_media
from row_bot.application.attachment_context import AttachmentExecutionCaches
from row_bot.api.v1.schemas import MediaAvailable, MediaError
from tests.subsystem.client_protocol.test_protocol_application import service  # noqa: F401

pytestmark = pytest.mark.subsystem


@pytest.fixture
def conversation(service):
    from row_bot import threads
    threads._save_thread_meta("fixture", "Fixture")
    folder = threads._MEDIA_DIR / "fixture"
    folder.mkdir()
    return folder


def test_generated_outputs_use_owned_opaque_refs_and_leave_original_video(conversation):
    image = b"\x89PNG\r\n\x1a\nfixture"
    video = b"\x00\x00\x00\x18ftypisom" + b"fixture"
    source = conversation / "private-provider-file.mp4"
    source.write_bytes(video)
    cache = AttachmentExecutionCaches("fixture", pending_image=base64.b64encode(image).decode(),
                                      pending_video={"path": str(source), "provider": "private"})
    results = generated_media.capture_generated_media("fixture", cache)
    assert [item["type"] for item in results] == ["media.available", "media.available"]
    for result, content in zip(results, (image, video)):
        MediaAvailable.model_validate(result["payload"])
        assert attachments.read_attachment(result["payload"]["media_ref"])[1] == content
    assert results[1]["payload"]["mime_type"] == "video/mp4"
    assert source.read_bytes() == video
    assert "private" not in json.dumps(results)
    assert str(conversation) not in json.dumps(results)
    assert cache.pending_image is None and cache.pending_video is None
    assert generated_media.capture_generated_media("fixture", cache) == []


def test_oversized_video_preflight_never_reads_or_deletes_source(conversation, monkeypatch):
    source = conversation / "fixture.mp4"
    data = b"\x00\x00\x00\x18ftypisom" + b"fixture"
    source.write_bytes(data)
    monkeypatch.setattr(generated_media, "MAX_ATTACHMENT_BYTES", 12)
    original_open = generated_media._open
    class SizeOnly:
        def __init__(self):
            self.file = original_open(conversation, source)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.file.close()
        def fileno(self):
            return self.file.fileno()
        def read(self, size):
            pytest.fail("Oversized generated video read before size admission")
    monkeypatch.setattr(generated_media, "_open", lambda *args: SizeOnly())
    result = generated_media.capture_generated_media("fixture", AttachmentExecutionCaches(
        "fixture", pending_video={"path": str(source)}))
    assert result == [{"type": "media.error", "payload": {"code": "payload_too_large"}}]
    MediaError.model_validate(result[0]["payload"])
    assert source.read_bytes() == data


def test_other_conversation_video_is_rejected_without_open_and_image_success_survives(conversation, monkeypatch):
    foreign = conversation.parent / "other"
    foreign.mkdir()
    source = foreign / "private.mp4"
    source.write_bytes(b"\x00\x00\x00\x18ftypisom")
    monkeypatch.setattr(generated_media, "_open", lambda *args: pytest.fail("Opened a foreign media path"))
    cache = AttachmentExecutionCaches("fixture", pending_image=base64.b64encode(b"\x89PNG\r\n\x1a\nimage").decode(),
                                      pending_video={"path": str(source)})
    outcomes = generated_media.capture_generated_media("fixture", cache)
    assert outcomes[0]["type"] == "media.available"
    assert outcomes[1] == {"type": "media.error", "payload": {"code": "media_unavailable"}}
    assert "private" not in json.dumps(outcomes)


def test_reparse_generated_video_and_foreign_cache_fail_closed(conversation, monkeypatch):
    source = conversation / "video.mp4"
    source.write_bytes(b"\x00\x00\x00\x18ftypisom")
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == source or original(path))
    for cache in (AttachmentExecutionCaches("fixture", pending_video={"path": str(source)}),
                  AttachmentExecutionCaches("other", pending_image="aW1hZ2U=")):
        assert generated_media.capture_generated_media("fixture", cache) == [
            {"type": "media.error", "payload": {"code": "media_unavailable"}}]
    assert source.read_bytes() == b"\x00\x00\x00\x18ftypisom"


def test_invalid_and_oversized_encoded_image_fail_without_registering(conversation, monkeypatch):
    monkeypatch.setattr(generated_media, "MAX_ATTACHMENT_BYTES", 3)
    monkeypatch.setattr(generated_media, "register_attachment", lambda *args: pytest.fail("Registered rejected bytes"))
    for value, expected in (("not base64!", "payload_too_large"), ("!!!!", "media_unavailable"),
                            (base64.b64encode(b"1234").decode(), "payload_too_large")):
        result = generated_media.capture_generated_media("fixture", AttachmentExecutionCaches("fixture", pending_image=value))
        assert result == [{"type": "media.error", "payload": {"code": expected}}]


def test_parallel_original_generated_outputs_have_distinct_exclusive_owners(conversation, monkeypatch):
    barrier = Barrier(2)
    original_write = generated_media._write
    def synchronized_write(root, path, data):
        barrier.wait(timeout=10)
        return original_write(root, path, data)
    monkeypatch.setattr(generated_media, "_write", synchronized_write)
    def save(data):
        return generated_media.save_generated_output("fixture", data, prefix="vid", extension="mp4")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, (b"first original", b"second original")))
    assert len(set(results)) == 2
    assert [Path(path).read_bytes() for path in results] == [b"first original", b"second original"]
    assert all(Path(path).parent == conversation and Path(path).name.startswith("vid_") for path in results)


def test_original_save_preserves_large_file_and_capture_limit_is_separate(conversation, monkeypatch):
    data = b"\x00\x00\x00\x18ftypisom" + b"larger than fixture transport bound"
    monkeypatch.setattr(generated_media, "MAX_ATTACHMENT_BYTES", 12)
    path = generated_media.save_generated_output("fixture", data, prefix="vid", extension="mp4")
    result = generated_media.capture_generated_media("fixture", AttachmentExecutionCaches(
        "fixture", pending_video={"path": path}))
    assert result == [{"type": "media.error", "payload": {"code": "payload_too_large"}}]
    assert Path(path).read_bytes() == data


@pytest.mark.parametrize("prefix,extension", [("../outside", "mp4"), ("vid", "../mp4"), ("vid", ".mp4")])
def test_original_save_rejects_path_components(conversation, prefix, extension):
    with pytest.raises(attachments.AttachmentError, match="invalid_command"):
        generated_media.save_generated_output("fixture", b"fixture", prefix=prefix, extension=extension)
    assert list(conversation.iterdir()) == []
