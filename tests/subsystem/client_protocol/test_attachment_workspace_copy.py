from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
from threading import Barrier

import pytest

from row_bot.channels import media

pytestmark = pytest.mark.subsystem


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    from row_bot import conversation_resources
    from row_bot.tools import registry
    root = tmp_path / "workspace"
    monkeypatch.setattr(conversation_resources, "current_execution_context", lambda: None)
    monkeypatch.setattr(registry, "get_tool_config", lambda *args: str(root))
    monkeypatch.setattr(media, "_INBOX_DIR", tmp_path / "inbox")
    return root


def test_simultaneous_materialization_keeps_both_same_name_sources_and_destinations(workspace, monkeypatch):
    from row_bot.file_context import materialize_chat_attachments
    barrier = Barrier(2)
    original_copy = media.copy_to_workspace
    original_open = os.open
    reservation_barrier = Barrier(2)
    monkeypatch.setattr(media.time, "time", lambda: 42)

    def synchronized_copy(*args, **kwargs):
        barrier.wait(timeout=10)  # Both inbox writes finish before either copy reads.
        return original_copy(*args, **kwargs)

    def synchronized_open(path, flags, *args, **kwargs):
        if flags & os.O_CREAT and Path(path) == workspace / "Received Files" / "same.txt":
            reservation_barrier.wait(timeout=10)  # Both choose the same first candidate.
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(media, "copy_to_workspace", synchronized_copy)
    monkeypatch.setattr(os, "open", synchronized_open)

    def run(content):
        files = [{"name": "same.txt", "data": content}]
        manifest = materialize_chat_attachments(files)
        assert "error" not in manifest[0]
        return manifest[0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, (b"first", b"second")))
    assert len({item["saved_path"] for item in results}) == 2
    assert len({item["workspace_path"] for item in results}) == 2
    for result, expected in zip(results, (b"first", b"second")):
        assert Path(result["saved_path"]).read_bytes() == expected
        assert (workspace / result["workspace_path"]).read_bytes() == expected


def test_copy_preserves_existing_file_and_writes_in_bounded_chunks(workspace, tmp_path, monkeypatch):
    received = workspace / "Received Files"
    received.mkdir(parents=True)
    (received / "same.txt").write_bytes(b"existing")
    source = tmp_path / "source.txt"
    content = b"x" * (3 * 65536 + 17)
    source.write_bytes(content)
    sizes = []
    original_write = os.write

    def write(descriptor, value):
        sizes.append(len(value))
        return original_write(descriptor, value)

    monkeypatch.setattr(os, "write", write)
    relative = media.copy_to_workspace(source, "same.txt")
    assert relative == "Received Files/same_1.txt"
    assert (workspace / relative).read_bytes() == content
    assert (received / "same.txt").read_bytes() == b"existing"
    assert max(sizes) == 65536 and sum(sizes) == len(content)


@pytest.mark.parametrize("component", ["workspace", "received"])
def test_real_directory_reparse_is_rejected_before_copy(workspace, tmp_path, component):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = workspace if component == "workspace" else workspace / "Received Files"
    link.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(outside), str(link))
        assert link.is_junction()
    else:
        link.symlink_to(outside, target_is_directory=True)
        assert link.is_symlink()
    source = tmp_path / "source.txt"
    source.write_bytes(b"private source")
    assert media.copy_to_workspace(source, "same.txt") is None
    assert list(outside.iterdir()) == []


def test_leaf_symlink_classification_is_rejected(workspace, tmp_path, monkeypatch):
    received = workspace / "Received Files"
    received.mkdir(parents=True)
    leaf = received / "same.txt"
    leaf.write_bytes(b"existing")
    source = tmp_path / "source.txt"
    source.write_bytes(b"private source")
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == leaf or original(path))
    assert media.copy_to_workspace(source, "same.txt") is None
    assert leaf.read_bytes() == b"existing"


def test_trusted_ancestor_alias_is_canonicalized_without_accepting_managed_root_links(workspace, tmp_path, monkeypatch):
    from row_bot.tools import registry
    actual_parent = tmp_path / "canonical-parent"
    actual_parent.mkdir()
    alias = tmp_path / "os-parent-alias"
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(actual_parent), str(alias))
    else:
        alias.symlink_to(actual_parent, target_is_directory=True)
    monkeypatch.setattr(media, "_INBOX_DIR", alias / "inbox")
    monkeypatch.setattr(registry, "get_tool_config", lambda *args: str(alias / "workspace"))
    source = media.save_inbound_file(b"fixture", "fixture.txt")
    assert source.parent == actual_parent / "inbox"
    relative = media.copy_to_workspace(source, "fixture.txt")
    assert (actual_parent / "workspace" / relative).read_bytes() == b"fixture"


def test_changed_opened_file_identity_copies_no_bytes(workspace, tmp_path, monkeypatch):
    source = tmp_path / "source.txt"
    source.write_bytes(b"private source")
    original_open = os.open
    original_samestat = os.path.samestat
    destination_opened = False
    writes = []

    def open_file(path, flags, *args, **kwargs):
        nonlocal destination_opened
        result = original_open(path, flags, *args, **kwargs)
        if flags & os.O_CREAT:
            destination_opened = True
        return result

    monkeypatch.setattr(os, "open", open_file)
    monkeypatch.setattr(os.path, "samestat", lambda a, b: False if destination_opened else original_samestat(a, b))
    monkeypatch.setattr(os, "write", lambda descriptor, data: writes.append(data) or len(data))
    assert media.copy_to_workspace(source, "same.txt") is None
    assert writes == []
    assert (workspace / "Received Files" / "same.txt").read_bytes() == b""
