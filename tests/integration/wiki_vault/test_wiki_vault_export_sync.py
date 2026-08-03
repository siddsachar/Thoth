from __future__ import annotations

import ast
import inspect
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest


pytestmark = [pytest.mark.integration, pytest.mark.subsystem]

REPO_ROOT = Path(__file__).resolve().parents[3]


def _entity(**overrides: Any) -> dict[str, Any]:
    base = {
        "id": "entity-1",
        "entity_type": "person",
        "subject": "Bob",
        "description": "Bob has enough deterministic content to become a wiki article.",
        "aliases": "Bobby, Robert",
        "tags": "family, friend",
        "properties": json.dumps({"status": "active", "confidence": 0.9}),
        "source": "test",
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-02T00:00:00",
    }
    base.update(overrides)
    return base


def test_wiki_vault_filename_frontmatter_and_markdown_rendering(wiki_stack: dict[str, Any]) -> None:
    wiki_vault = wiki_stack["wiki_vault"]
    wiki_vault.set_enabled(True)

    assert wiki_vault._safe_filename("Hello World") == "Hello World"
    assert wiki_vault._safe_filename('A<B>C:D"E') == "A_B_C_D_E"
    assert wiki_vault._safe_filename("   spaces   ") == "spaces"
    assert wiki_vault._safe_filename("") == "unnamed"
    assert len(wiki_vault._safe_filename("x" * 200)) <= 120

    entity = _entity()
    assert wiki_vault._entity_md_path(entity).name == "Bob.md"
    assert "person" in str(wiki_vault._entity_md_path(entity))

    frontmatter = wiki_vault._render_frontmatter(entity)
    assert frontmatter.startswith("---")
    assert frontmatter.endswith("---")
    assert 'id: "entity-1"' in frontmatter
    assert 'type: person' in frontmatter
    assert 'subject: "Bob"' in frontmatter
    assert "Bobby" in frontmatter
    assert "family" in frontmatter
    assert "confidence" in frontmatter

    markdown = wiki_vault.render_entity_md(entity)
    assert "<!-- Auto-generated" in markdown
    assert "sync back" in markdown
    assert "# Bob" in markdown
    assert "Bob has enough deterministic content" in markdown


def test_wiki_vault_frontmatter_preserves_json_like_ids(wiki_stack: dict[str, Any]) -> None:
    wiki_vault = wiki_stack["wiki_vault"]
    wiki_vault.set_enabled(True)

    for entity_id in ("25e872019289", "123456789012"):
        entity = _entity(id=entity_id, subject=f"Entity {entity_id}")
        md_path = wiki_vault.export_entity(entity)
        assert md_path is not None

        exported = md_path.read_text(encoding="utf-8")
        assert f'id: "{entity_id}"' in exported
        assert wiki_vault.parse_entity_md(md_path)["id"] == entity_id

        legacy_unquoted = exported.replace(f'id: "{entity_id}"', f"id: {entity_id}")
        md_path.write_text(legacy_unquoted, encoding="utf-8")
        assert wiki_vault.parse_entity_md(md_path)["id"] == entity_id


def test_wiki_vault_config_export_search_read_delete_and_stats(wiki_stack: dict[str, Any]) -> None:
    wiki_vault = wiki_stack["wiki_vault"]
    vault = wiki_stack["vault"]

    assert wiki_vault.is_enabled() is False
    assert wiki_vault.export_entity(_entity()) is None

    wiki_vault.set_enabled(True)
    assert wiki_vault.is_enabled() is True
    assert wiki_vault.get_vault_path() == vault.resolve()

    entity = _entity()
    md_path = wiki_vault.export_entity(entity)
    assert md_path is not None
    assert md_path.exists()
    assert md_path.read_text(encoding="utf-8").startswith("<!-- Auto-generated")

    sparse = _entity(id="entity-sparse", subject="Tiny", description="Short")
    assert wiki_vault.export_entity(sparse) is None
    assert not wiki_vault._entity_md_path(sparse).exists()

    hits = wiki_vault.search_vault("Bob")
    assert hits
    assert hits[0]["title"] == "Bob"
    assert hits[0]["entity_id"] == "entity-1"
    assert wiki_vault.search_vault("zzzz-no-hit") == []

    article = wiki_vault.read_article("Bob")
    assert article is not None
    assert "# Bob" in article

    conv_path = wiki_vault.export_conversation(
        "thread-1",
        [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ],
        "Test Chat",
    )
    assert conv_path is not None
    conv_text = conv_path.read_text(encoding="utf-8")
    assert "**You:** Hello" in conv_text
    assert "**Row-Bot:** Hi there" in conv_text

    stats = wiki_vault.get_vault_stats()
    assert stats["articles"] >= 1
    assert stats["conversations"] == 1
    assert stats["enabled"] is True

    wiki_vault.delete_entity_md(entity)
    assert not md_path.exists()


def test_wiki_vault_indexes_rebuild_and_orphan_cleanup(wiki_stack: dict[str, Any]) -> None:
    kg = wiki_stack["kg"]
    wiki_vault = wiki_stack["wiki_vault"]
    wiki_vault.set_enabled(True)

    bob = kg.save_entity("person", "Bob", "Bob has enough content for a full article.", source="test")
    tiny = kg.save_entity("person", "Tiny", "Short", source="test")
    ada = kg.save_entity("concept", "Ada", "Ada concept has enough content for a full article.", source="test")

    type_index = wiki_vault._render_type_index("person", [bob, tiny])
    assert "# Person" in type_index
    assert "[[Bob]]" in type_index
    assert "## Quick Notes" in type_index
    assert "**Tiny**" in type_index

    master_index = wiki_vault._render_master_index([bob, tiny, ada])
    assert "# Row-Bot Knowledge Base" in master_index
    assert "3 entities" in master_index

    orphan = wiki_stack["vault"] / "wiki" / "person" / "Orphan.md"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text("# Orphan\n", encoding="utf-8")

    stats = wiki_vault.rebuild_vault()

    assert stats["total"] == 3
    assert stats["exported"] == 2
    assert stats["sparse"] == 1
    assert stats["types"] == 2
    assert stats["orphans_removed"] == 1
    assert not orphan.exists()
    assert (wiki_stack["vault"] / "wiki" / "person" / "_index.md").exists()
    assert (wiki_stack["vault"] / "wiki" / "index.md").exists()


def test_wiki_cleanup_preserves_raw_and_conversations_and_ui_uses_knowledge_tab(
    wiki_stack: dict[str, Any],
) -> None:
    wiki_vault = wiki_stack["wiki_vault"]
    wiki_vault.set_enabled(True)
    vault = wiki_stack["vault"]

    (vault / "wiki" / "person").mkdir(parents=True, exist_ok=True)
    (vault / "wiki" / "person" / "Alice.md").write_text("test", encoding="utf-8")
    (vault / "wiki" / "concept").mkdir(parents=True, exist_ok=True)
    (vault / "wiki" / "concept" / "AI.md").write_text("test", encoding="utf-8")
    (vault / "wiki" / "index.md").write_text("master", encoding="utf-8")
    (vault / "raw").mkdir(parents=True, exist_ok=True)
    (vault / "raw" / "upload.pdf").write_text("fake pdf", encoding="utf-8")
    (vault / "conversations").mkdir(parents=True, exist_ok=True)
    (vault / "conversations" / "chat.md").write_text("conversation", encoding="utf-8")

    assert wiki_vault.clear_wiki_folder() == 3
    assert not (vault / "wiki" / "person" / "Alice.md").exists()
    assert not (vault / "wiki" / "concept" / "AI.md").exists()
    assert not (vault / "wiki" / "index.md").exists()
    assert (vault / "raw" / "upload.pdf").exists()
    assert (vault / "conversations" / "chat.md").exists()

    kg_source = (REPO_ROOT / "src" / "row_bot" / "knowledge_graph.py").read_text(encoding="utf-8")
    delete_all = kg_source.split("def delete_all_entities", 1)[1].split("\ndef ", 1)[0]
    assert "wiki_vault" in delete_all
    assert "clear_wiki_folder" in delete_all

    settings_source = (REPO_ROOT / "src" / "row_bot" / "ui" / "settings.py").read_text(encoding="utf-8")
    home_source = (REPO_ROOT / "src" / "row_bot" / "ui" / "home.py").read_text(encoding="utf-8")
    status_checks_source = (REPO_ROOT / "src" / "row_bot" / "ui" / "status_checks.py").read_text(encoding="utf-8")
    ast.parse(settings_source)
    ast.parse(home_source)
    ast.parse(status_checks_source)

    assert "_build_knowledge_tab" in settings_source
    assert "_build_memory_tab" not in settings_source
    assert "_build_wiki_tab" not in settings_source
    assert "tab_knowledge" in settings_source
    assert "tab_mem" not in settings_source
    assert "tab_wiki" not in settings_source
    assert '"Knowledge"' in settings_source
    assert "confirm(" in settings_source.split("_delete_all_knowledge", 1)[1].split("\n\n", 1)[0]
    assert "reset_vector_store" in settings_source.split("_delete_all_knowledge", 1)[1][:800]
    assert "clear_wiki_folder" in settings_source.split("_delete_all_knowledge", 1)[1][:800]
    assert "confirm(" in settings_source.split("_clear_docs", 1)[1].split("\n\n", 1)[0]
    assert 'ui.tab("Knowledge"' in home_source
    assert 'ui.tab("Memory"' not in home_source
    assert "Knowledge Extraction" in home_source
    assert 'settings_tab="Memory"' not in status_checks_source
    assert 'settings_tab="Knowledge"' in status_checks_source


def test_wiki_vault_parse_check_sync_import_and_batch_sync(wiki_stack: dict[str, Any]) -> None:
    kg = wiki_stack["kg"]
    memory = wiki_stack["memory"]
    wiki_vault = wiki_stack["wiki_vault"]
    wiki_vault.set_enabled(True)

    result = memory.save_memory(
        "person",
        "VaultAlice",
        "VaultAlice is a deterministic vault sync integration test person.",
        tags="vault",
    )
    entity_id = result["id"]
    entity = kg.get_entity(entity_id)
    md_path = wiki_vault.export_entity(entity)
    assert md_path is not None

    parsed = wiki_vault.parse_entity_md(md_path)
    assert parsed is not None
    assert parsed["id"] == entity_id
    assert parsed["entity_type"] == "person"
    assert parsed["subject"] == "VaultAlice"
    assert "vault sync" in parsed["description"]

    original_text = md_path.read_text(encoding="utf-8")
    edited_text = original_text.replace(
        "deterministic vault sync integration test person",
        "deterministic vault sync integration test person EDITED IN VAULT",
    )
    md_path.write_text(edited_text, encoding="utf-8")
    future = time.time() + 10
    os.utime(md_path, (future, future))

    out_of_sync = wiki_vault.check_vault_sync()
    assert any(item["entity_id"] == entity_id for item in out_of_sync)

    assert wiki_vault.import_from_vault(entity_id, md_path) is True
    imported = kg.get_entity(entity_id)
    assert "EDITED IN VAULT" in imported["description"]
    props = imported["properties"]
    if isinstance(props, str):
        props = json.loads(props)
    assert props["status"] == "active"

    second = memory.save_memory(
        "place",
        "VaultPlace",
        "VaultPlace is a deterministic batch sync integration test place.",
    )
    second_md = wiki_vault.export_entity(kg.get_entity(second["id"]))
    assert second_md is not None
    second_md.write_text(second_md.read_text(encoding="utf-8") + "\nBatch edit.\n", encoding="utf-8")
    os.utime(second_md, (time.time() + 20, time.time() + 20))

    sync_result = wiki_vault.sync_all_from_vault()
    assert sync_result["synced"] >= 1
    assert sync_result["failed"] == 0


def test_wiki_tool_contract_and_removed_search_tool(wiki_stack: dict[str, Any]) -> None:
    kg = wiki_stack["kg"]
    wiki_vault = wiki_stack["wiki_vault"]
    wiki_vault.set_enabled(True)
    kg.save_entity("person", "WikiToolBob", "WikiToolBob has enough content for a wiki article.")
    wiki_vault.rebuild_vault()

    from row_bot.tools.wiki_tool import WikiTool

    tool = WikiTool()
    langchain_tools = tool.as_langchain_tools()
    names = {t.name for t in langchain_tools}

    assert names == {"wiki_read", "wiki_rebuild", "wiki_stats", "wiki_export_conversation"}
    assert "wiki_search" not in names
    assert "Found" in tool.execute("WikiToolBob")

    read_tool = next(t for t in langchain_tools if t.name == "wiki_read")
    assert "# WikiToolBob" in read_tool.invoke({"subject": "WikiToolBob"})

    stats_tool = next(t for t in langchain_tools if t.name == "wiki_stats")
    assert "Wiki Vault Status" in stats_tool.invoke({})


def test_knowledge_editability_hybrid_search_and_ui_wiring(wiki_stack: dict[str, Any]) -> None:
    kg = wiki_stack["kg"]
    memory = wiki_stack["memory"]
    memory_tool = wiki_stack["memory_tool"]

    result = memory.save_memory(
        "concept",
        "HybridKeywordNeedle",
        "HybridKeywordNeedle tests SQL keyword fallback retrieval.",
        tags="hybrid",
    )
    entity_id = result["id"]

    updated = kg.update_entity(
        entity_id,
        "HybridKeywordNeedle has an updated description.",
        subject="HybridKeywordNeedleRenamed",
        entity_type="skill",
        aliases="NeedleAlias",
        tags="hybrid,tool",
    )
    assert updated is not None
    assert updated["subject"] == "HybridKeywordNeedleRenamed"
    assert updated["entity_type"] == "skill"
    assert "NeedleAlias" in updated["aliases"]

    sql_hits = kg.search_entities("HybridKeywordNeedleRenamed")
    assert entity_id in {hit["id"] for hit in sql_hits}

    recall_hits = kg.graph_enhanced_recall("NeedleAlias", top_k=10, threshold=0.0)
    assert any(hit["id"] == entity_id for hit in recall_hits)

    update_result = memory_tool._update_memory(
        entity_id,
        "HybridKeywordNeedle updated via memory tool.",
        subject="HybridKeywordNeedleTooled",
        entity_type="concept",
        aliases="ToolAlias",
        tags="tool_tag",
    )
    assert "updated successfully" in update_result.lower()
    after_tool = kg.get_entity(entity_id)
    assert after_tool["subject"] == "HybridKeywordNeedleTooled"
    assert after_tool["entity_type"] == "concept"
    assert "ToolAlias" in after_tool["aliases"]
    assert "tool_tag" in after_tool["tags"]

    search_result = memory_tool._search_memory("HybridKeywordNeedleTooled")
    parsed_results = json.loads(search_result)
    assert parsed_results[0]["id"] == entity_id
    assert {"id", "category", "subject", "content"} <= set(parsed_results[0])

    from row_bot.tools.memory_tool import _UpdateMemoryInput
    from row_bot.ui.entity_editor import open_entity_editor

    assert set(_UpdateMemoryInput.model_fields) == {
        "memory_id",
        "content",
        "subject",
        "entity_type",
        "aliases",
        "tags",
    }
    sig = inspect.signature(open_entity_editor)
    assert "entity_id" in sig.parameters
    assert "on_saved" in sig.parameters

    prompts_src = (REPO_ROOT / "src" / "row_bot" / "prompts.py").read_text(encoding="utf-8")
    settings_src = (REPO_ROOT / "src" / "row_bot" / "ui" / "settings.py").read_text(encoding="utf-8")
    graph_panel_src = (REPO_ROOT / "src" / "row_bot" / "ui" / "graph_panel.py").read_text(encoding="utf-8")
    status_checks_src = (REPO_ROOT / "src" / "row_bot" / "ui" / "status_checks.py").read_text(encoding="utf-8")
    entity_editor_src = (REPO_ROOT / "src" / "row_bot" / "ui" / "entity_editor.py").read_text(encoding="utf-8")
    for source in (settings_src, graph_panel_src, status_checks_src, entity_editor_src):
        ast.parse(source)

    assert "wiki_search" not in prompts_src
    assert "check_vault_sync" in settings_src
    assert "sync_all_from_vault" in settings_src
    assert "Sync from Vault" in settings_src
    assert "graph-edit-trigger" in graph_panel_src
    assert "entity_editor" in graph_panel_src
    assert "check_vault_sync" in status_checks_src
    assert "edited in vault" in status_checks_src
    assert "Audit and Provenance" in entity_editor_src
    assert "mark_user_modified" in entity_editor_src
    assert "set_status" in entity_editor_src
