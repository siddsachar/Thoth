from __future__ import annotations


def test_media_native_id_does_not_follow_identical_text_or_old_position(monkeypatch):
    from row_bot import threads
    from row_bot.ui.helpers import _hydrate_thread_media

    monkeypatch.setattr(threads, "load_media_file", lambda *_: b"fixture image")
    monkeypatch.setattr(threads, "load_thread_media", lambda _: {"version": 2, "entries": [
        {"idx": 0, "role": "assistant", "message_id": "native-second", "sig": "unused",
         "media": [{"type": "image", "path": "gen_1.png", "persist": True}]},
    ]})
    rows = [{"role": "assistant", "content": "same", "checkpoint_message_id": native_id}
            for native_id in ("native-first", "native-second")]
    assert _hydrate_thread_media("fixture", rows)[1]["images"] == ["gen_1.png"]
    assert "images" not in rows[0]
    rows.pop()
    assert "images" not in _hydrate_thread_media("fixture", rows)[0]


def test_new_sidecar_records_native_identity(monkeypatch):
    from row_bot import threads
    from row_bot.ui.helpers import persist_thread_media_state
    saved = []
    monkeypatch.setattr(threads, "save_thread_media", lambda _, data: saved.append(data))
    persist_thread_media_state("fixture", [{"role": "assistant", "content": "same",
                                           "checkpoint_message_id": "native-first", "images": ["gen_1.png"]}])
    assert saved[0]["entries"][0]["message_id"] == "native-first"


def test_detached_native_identity_has_no_content_fallback(monkeypatch):
    from row_bot.ui import helpers
    rows = [{"role": "assistant", "content": "same", "checkpoint_message_id": "native-first"},
            {"role": "assistant", "content": "same", "checkpoint_message_id": "native-second"}]
    saved = []
    monkeypatch.setattr(helpers, "load_thread_messages", lambda _: rows)
    monkeypatch.setattr(helpers, "persist_thread_media_state", lambda _, messages: saved.append(messages))
    assert not helpers.persist_detached_thread_media("fixture", "same", images=["gen_1.png"], message_id="missing")
    assert not saved
    assert helpers.persist_detached_thread_media("fixture", "same", images=["gen_1.png"], message_id="native-first")
    assert rows[0]["images"] == ["gen_1.png"]
    assert "images" not in rows[1]
