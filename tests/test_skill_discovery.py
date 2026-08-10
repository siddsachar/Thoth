from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from row_bot.skill_discovery import (
    SkillRecord,
    build_skill_discovery_tools,
    collect_enabled_skill_records,
    read_skill_reference,
    render_active_skills_prompt,
    resolve_skill_aliases,
)


def _record(
    canonical_id: str,
    *,
    alias: str | None = None,
    source: str = "manual",
    root: Path | None = None,
    instructions: str = "Follow these steps.",
) -> SkillRecord:
    return SkillRecord(
        canonical_id=canonical_id,
        alias=alias,
        display_name=canonical_id.rsplit(":", 1)[-1].replace("_", " ").title(),
        icon="✨",
        description=f"Workflow for {canonical_id}",
        tags=("workflow",),
        activation={},
        instructions=instructions,
        source=source,
        root=root,
        plugin_name="Example Plugin" if source.startswith("plugin:") else "",
    )


def _payload(value: str) -> dict:
    return json.loads(value)


def test_collect_enabled_records_unifies_manual_and_plugin_without_guides(monkeypatch, tmp_path) -> None:
    import row_bot.skills as skills
    from row_bot.plugins import registry as plugin_registry

    manual = SimpleNamespace(
        name="manual_skill",
        display_name="Manual Skill",
        icon="✨",
        description="Manual workflow",
        tags=["manual"],
        activation={"keywords": ["manual"]},
        instructions="Manual body",
        source="user",
        path=tmp_path / "manual",
        tools=[],
    )
    guide = SimpleNamespace(name="guide", tools=["filesystem"])
    monkeypatch.setattr(skills, "get_enabled_manual_skills", lambda: [manual, guide])
    monkeypatch.setattr(skills, "is_tool_guide", lambda skill: bool(getattr(skill, "tools", [])))
    monkeypatch.setattr(plugin_registry, "get_enabled_plugin_skill_records", lambda: [{
        "plugin_id": "example",
        "plugin_name": "Example Plugin",
        "name": "plugin_skill",
        "display_name": "Plugin Skill",
        "icon": "🔌",
        "description": "Plugin workflow",
        "tags": ["plugin"],
        "activation": {},
        "instructions": "Plugin body",
        "root": tmp_path / "plugin",
    }])

    records = collect_enabled_skill_records()

    assert [record.canonical_id for record in records] == [
        "manual_skill",
        "plugin:example:plugin_skill",
    ]
    assert records[1].root == tmp_path / "plugin"


def test_plugin_alias_is_available_only_when_unique_and_not_shadowing_manual() -> None:
    records = resolve_skill_aliases([
        _record("lookup"),
        _record("plugin:a:lookup", alias="lookup", source="plugin:a"),
        _record("plugin:a:shared", alias="shared", source="plugin:a"),
        _record("plugin:b:shared", alias="shared", source="plugin:b"),
        _record("plugin:a:unique", alias="unique", source="plugin:a"),
    ])
    aliases = {record.canonical_id: record.alias for record in records}

    assert aliases["plugin:a:lookup"] is None
    assert aliases["plugin:a:shared"] is None
    assert aliases["plugin:b:shared"] is None
    assert aliases["plugin:a:unique"] == "unique"


def test_skill_search_is_bounded_and_never_returns_instructions(tmp_path, monkeypatch) -> None:
    from row_bot import skills_activation

    monkeypatch.setattr(skills_activation, "STATE_PATH", tmp_path / "activation.json")
    monkeypatch.setattr(skills_activation, "DATA_DIR", tmp_path)
    records = [_record(f"plugin:p:research_{index}", alias=f"research_{index}", source="plugin:p") for index in range(8)]
    search, _load = build_skill_discovery_tools(records, thread_id="thread", context_tokens=32_768)

    payload = _payload(search.invoke({"query": "research", "limit": 99}))

    assert payload["ok"] is True
    assert len(payload["results"]) == 5
    assert all("instructions" not in result for result in payload["results"])
    assert _payload(search.invoke({"query": " "}))["error"]["code"] == "invalid_query"


def test_skill_load_acknowledges_once_and_enforces_five_item_lru(tmp_path, monkeypatch) -> None:
    from row_bot import skills_activation

    monkeypatch.setattr(skills_activation, "STATE_PATH", tmp_path / "activation.json")
    monkeypatch.setattr(skills_activation, "DATA_DIR", tmp_path)
    records = [_record(f"skill_{index}") for index in range(6)]
    _search, load = build_skill_discovery_tools(records, thread_id="thread", context_tokens=32_768)

    first = _payload(load.invoke({"name": "skill_0"}))
    repeated = _payload(load.invoke({"name": "skill_0"}))
    for index in range(1, 6):
        latest = _payload(load.invoke({"name": f"skill_{index}"}))

    assert first["kind"] == "skill_loaded" and first["newly_active"] is True
    assert repeated["newly_active"] is False
    assert latest["evicted_skill_id"] == "skill_0"
    assert skills_activation.get_auto_loaded_skill_ids("thread") == [
        "skill_1", "skill_2", "skill_3", "skill_4", "skill_5",
    ]


def test_cached_skill_load_uses_runtime_task_and_keeps_lru_state_isolated(tmp_path, monkeypatch) -> None:
    from row_bot import skills_activation

    monkeypatch.setattr(skills_activation, "STATE_PATH", tmp_path / "activation.json")
    monkeypatch.setattr(skills_activation, "DATA_DIR", tmp_path)
    records = [_record(f"skill_{index}") for index in range(6)]
    _search, load = build_skill_discovery_tools(
        records,
        thread_id="parent-a",
        context_tokens=32_768,
    )

    parent_a = {"configurable": {"thread_id": "parent-a"}}
    parent_b = {"configurable": {"thread_id": "parent-b"}}
    first_a = _payload(load.invoke({"name": "skill_0"}, config=parent_a))
    first_b = _payload(load.invoke({"name": "skill_0"}, config=parent_b))
    for index in range(1, 6):
        latest_a = _payload(load.invoke({"name": f"skill_{index}"}, config=parent_a))

    assert first_a["newly_active"] is True
    assert first_b["newly_active"] is True
    assert latest_a["evicted_skill_id"] == "skill_0"
    assert skills_activation.get_auto_loaded_skill_ids("parent-a") == [
        "skill_1", "skill_2", "skill_3", "skill_4", "skill_5",
    ]
    assert skills_activation.get_auto_loaded_skill_ids("parent-b") == ["skill_0"]


def test_cached_skill_load_isolates_child_like_sibling_tasks_and_hides_runtime_config(tmp_path, monkeypatch) -> None:
    from row_bot import skills_activation

    monkeypatch.setattr(skills_activation, "STATE_PATH", tmp_path / "activation.json")
    monkeypatch.setattr(skills_activation, "DATA_DIR", tmp_path)
    records = [_record("alpha"), _record("beta")]
    _search, load = build_skill_discovery_tools(
        records,
        thread_id="parent-build-task",
        context_tokens=32_768,
        child_boundaries=True,
    )

    child_a = {"configurable": {"thread_id": "parent:agent_child:one"}}
    child_b = {"configurable": {"thread_id": "parent:agent_child:two"}}
    first_a = _payload(load.invoke({"name": "alpha"}, config=child_a))
    first_b = _payload(load.invoke({"name": "beta"}, config=child_b))

    assert first_a["newly_active"] is True
    assert first_b["newly_active"] is True
    assert skills_activation.get_auto_loaded_skill_ids("parent:agent_child:one") == ["alpha"]
    assert skills_activation.get_auto_loaded_skill_ids("parent:agent_child:two") == ["beta"]
    assert skills_activation.get_auto_loaded_skill_ids("parent-build-task") == []
    properties = load.get_input_schema().model_json_schema()["properties"]
    assert set(properties) == {"name", "relative_path"}
    assert "config" not in properties
    assert "thread_id" not in properties


def test_eager_active_skill_reload_does_not_consume_implicit_limit(tmp_path, monkeypatch) -> None:
    from row_bot import skills_activation

    monkeypatch.setattr(skills_activation, "STATE_PATH", tmp_path / "activation.json")
    monkeypatch.setattr(skills_activation, "DATA_DIR", tmp_path)
    record = _record("profile_selected")
    _search, load = build_skill_discovery_tools(
        [record],
        thread_id="thread",
        context_tokens=32_768,
        active_skill_ids=["profile_selected"],
    )

    payload = _payload(load.invoke({"name": "profile_selected"}))

    assert payload["newly_active"] is False
    assert skills_activation.get_auto_loaded_skill_ids("thread") == []


def test_active_prompt_preserves_manual_format_and_labels_plugins() -> None:
    prompt = render_active_skills_prompt([
        _record("manual", instructions="Manual body"),
        _record("plugin:example:plugin", alias="plugin", source="plugin:example", instructions="Plugin body"),
    ])

    assert prompt.startswith("## Skills\n\n")
    assert "### ✨ Manual\nManual body" in prompt
    assert "### ✨ Plugin (plugin: Example Plugin)\nPlugin body" in prompt


def test_reference_read_rejects_invalid_paths_and_returns_complete_utf8(tmp_path) -> None:
    root = tmp_path / "skill"
    root.mkdir()
    (root / "reference.txt").write_bytes(("complete reference\n" * 1000).encode("utf-8"))
    (root / "binary.bin").write_bytes(b"abc\x00def")
    (root / "control.bin").write_bytes(b"abc\x01def")
    (root / "invalid.txt").write_bytes(b"\xff\xfe")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")

    with pytest.raises(ValueError, match="relative"):
        read_skill_reference(root, "")
    with pytest.raises(ValueError, match="relative"):
        read_skill_reference(root, str(outside.resolve()))
    with pytest.raises(ValueError, match="inside"):
        read_skill_reference(root, "../outside.txt")
    with pytest.raises(ValueError, match="regular file"):
        read_skill_reference(root, ".")
    with pytest.raises(ValueError, match="binary"):
        read_skill_reference(root, "binary.bin")
    with pytest.raises(ValueError, match="binary"):
        read_skill_reference(root, "control.bin")
    with pytest.raises(ValueError, match="UTF-8"):
        read_skill_reference(root, "invalid.txt")

    assert read_skill_reference(root, "reference.txt") == "complete reference\n" * 1000


def test_reference_read_rejects_symlink_escape_when_supported(tmp_path) -> None:
    root = tmp_path / "skill"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link = root / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")

    with pytest.raises(ValueError, match="inside"):
        read_skill_reference(root, "link.txt")
