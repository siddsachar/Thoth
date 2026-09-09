from __future__ import annotations

import base64
import json

import pytest

from row_bot.application import live_content
from row_bot.api.v1.schemas import LazyContent

pytestmark = pytest.mark.subsystem


@pytest.fixture
def store(tmp_path):
    result = live_content.LiveContentStore(root=tmp_path, validate=lambda _: None)
    yield result
    result.close()


def _read(store, *, conversation="fixture", reference="live:pass:segment", limit=65536):
    data = bytearray()
    cursor = None
    while True:
        page = store.read_page(conversation, reference, limit, cursor)
        LazyContent.model_validate(page)
        part = base64.b64decode(page["data"])
        assert len(part) <= limit
        data.extend(part)
        if not page["has_more"]:
            assert page["next_cursor"] is None
            break
        cursor = page["next_cursor"]
    return json.loads(data)


def test_large_public_text_uses_bounded_pages_and_exact_json_escaping(store):
    chunks = ["a" * (2 * 1024 * 1024), '\n"\\\x00中文😀', " final"]
    for part in chunks:
        store.append("fixture", "live:pass:segment", part)
    assert _read(store) == [{"type": "text", "text": "".join(chunks)}]
    assert max(len(part) for part in live_content._encoded_parts("\x00" * 20000)) <= 24576


def test_live_cursor_pins_revision_and_rejects_foreign_or_restarted_owner(store, tmp_path):
    store.append("fixture", "live:pass:segment", "first text")
    page = store.read_page("fixture", "live:pass:segment", 5)
    store.append("fixture", "live:pass:segment", "more")
    with pytest.raises(live_content.LiveContentError, match="cursor_expired"):
        store.read_page("fixture", "live:pass:segment", 5, page["next_cursor"])
    store.append("other", "live:pass:segment", "foreign")
    fresh = store.read_page("fixture", "live:pass:segment", 5)
    with pytest.raises(live_content.LiveContentError, match="cursor_expired"):
        store.read_page("other", "live:pass:segment", 5, fresh["next_cursor"])
    restarted = live_content.LiveContentStore(root=tmp_path, validate=lambda _: None)
    with pytest.raises(live_content.LiveContentError, match="cursor_expired"):
        restarted.read_page("fixture", "live:pass:segment", 5, fresh["next_cursor"])


def test_settlement_discards_only_owned_spool_and_storage_bound_is_atomic(store, monkeypatch):
    store.append("fixture", "live:pass:segment", "first")
    store.append("fixture", "live:other:segment", "second")
    file = store._spools[("fixture", "live:pass:segment")].file
    store.discard("fixture", "live:pass:segment")
    assert file.closed
    assert _read(store, reference="live:other:segment") == [{"type": "text", "text": "second"}]
    monkeypatch.setattr(live_content, "MAX_SPOOL_BYTES", 40)
    with pytest.raises(live_content.LiveContentError, match="projection_storage_limit"):
        store.append("fixture", "live:other:segment", "x" * 100)
    assert _read(store, reference="live:other:segment") == [{"type": "text", "text": "second"}]
    store.close()
    assert store._total == 0


def test_live_reference_recovery_is_conversation_scoped_and_retires_with_owner(store):
    store.append("fixture", "live:pass:segment", "first")
    store.append("fixture", "live:other:segment", "second")
    store.append("foreign", "live:private:segment", "foreign")
    assert store.references("fixture") == ["live:other:segment", "live:pass:segment"]
    assert store.references("missing") == []
    store.discard("fixture", "live:pass:segment")
    assert store.references("fixture") == ["live:other:segment"]
    store.close()
    assert store.references("fixture") == []


@pytest.mark.parametrize("conversation,reference", [("../fixture", "live:pass:segment"),
    ("fixture", "live:pass:../segment"), ("fixture", "private-path")])
def test_invalid_spool_reference_never_creates_files(store, conversation, reference):
    with pytest.raises(live_content.LiveContentError, match="not_found"):
        store.append(conversation, reference, "private")
    assert not store._spools
