from __future__ import annotations

import importlib
import json
import sys
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from row_bot.agent_budget import new_execution_budget


def _fresh_memory_modules(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    import row_bot.knowledge_graph as knowledge_graph
    import row_bot.memory as memory
    import row_bot.memory_policy as memory_policy
    import row_bot.memory_extraction as memory_extraction

    kg = importlib.reload(knowledge_graph)
    mem = importlib.reload(memory)
    policy = importlib.reload(memory_policy)
    extraction = importlib.reload(memory_extraction)
    kg._skip_reindex = True
    monkeypatch.setattr(kg, "semantic_search", lambda *args, **kwargs: [])
    return kg, mem, policy, extraction


def _props(entity: dict) -> dict:
    raw = entity.get("properties", "{}")
    return json.loads(raw or "{}") if isinstance(raw, str) else dict(raw or {})


def test_candidate_retrieval_is_no_touch_and_keyword_fallback_finds_exact_subject(tmp_path, monkeypatch):
    kg, _mem, _policy, _extraction = _fresh_memory_modules(tmp_path, monkeypatch)

    target = kg.save_entity("fact", "Rosebud Code", "The user's code word is rosebud.", source="test")
    other = kg.save_entity("fact", "Unrelated", "The user likes green tea.", source="test")

    candidates = kg.retrieve_memory_candidates(
        "what is my Rosebud Code",
        threshold=0.99,
        include_keyword=True,
    )

    assert [c["id"] for c in candidates][:1] == [target["id"]]
    assert "keyword" in candidates[0]["retrieval_debug"]["sources"]
    assert "recalled_at" not in _props(kg.get_entity(target["id"]))
    assert "recalled_at" not in _props(kg.get_entity(other["id"]))

    kg.touch_recalled([target["id"]])
    assert "recalled_at" in _props(kg.get_entity(target["id"]))
    assert "recalled_at" not in _props(kg.get_entity(other["id"]))


def test_fts_search_and_rebuild_are_no_touch(tmp_path, monkeypatch):
    kg, _mem, _policy, _extraction = _fresh_memory_modules(tmp_path, monkeypatch)
    target = kg.save_entity("fact", "Cerulean Pin", "The user's studio door code is cerulean.", source="test")

    kg._clear_fts_index()
    assert kg.fts_search_entities("Cerulean Pin") == []
    assert kg.rebuild_fts_index() == 1

    hits = kg.fts_search_entities("Cerulean Pin")

    assert [hit["id"] for hit in hits] == [target["id"]]
    assert "recalled_at" not in _props(kg.get_entity(target["id"]))


def test_lexical_seed_expands_graph_without_semantic_seed(tmp_path, monkeypatch):
    kg, _mem, _policy, _extraction = _fresh_memory_modules(tmp_path, monkeypatch)
    project = kg.save_entity("project", "Atlas Project", "The user is building Atlas.", source="test")
    deadline = kg.save_entity("event", "Atlas Review", "Atlas review is on Friday.", source="test")
    kg.add_relation(project["id"], deadline["id"], "has_deadline", confidence=0.9, source="test")

    candidates = kg.retrieve_memory_candidates(
        "Atlas Project",
        threshold=0.99,
        max_results=5,
        include_keyword=True,
    )

    assert any(c["id"] == project["id"] for c in candidates)
    graph_hit = next(c for c in candidates if c["id"] == deadline["id"])
    assert "graph" in graph_hit["retrieval_debug"]["sources"]
    assert "recalled_at" not in _props(kg.get_entity(deadline["id"]))


def test_punctuated_exact_subject_outranks_shorter_parent_subject(tmp_path, monkeypatch):
    kg, _mem, _policy, _extraction = _fresh_memory_modules(tmp_path, monkeypatch)
    parent = kg.save_entity("concept", "Telegram", "The messaging service.", source="test")
    target = kg.save_entity("concept", "Telegram streaming", "Streaming responses through Telegram.", source="test")

    candidates = kg.retrieve_memory_candidates(
        "What do you remember about Telegram streaming?",
        threshold=0.99,
        max_results=5,
        include_keyword=True,
    )

    assert candidates[0]["id"] == target["id"]
    assert candidates[0]["score"] > next(c["score"] for c in candidates if c["id"] == parent["id"])


def test_alias_phrase_with_question_punctuation_outranks_tag_match(tmp_path, monkeypatch):
    kg, _mem, _policy, _extraction = _fresh_memory_modules(tmp_path, monkeypatch)
    tag_match = kg.save_entity("concept", "CLI Overview", "General CLI notes.", tags="secure CLI bridge", source="test")
    target = kg.save_entity(
        "concept",
        "Agent-Native CLI Bridge",
        "Controlled CLI mediation layer.",
        aliases="secure CLI bridge, controlled run_agent_cli interface",
        source="test",
    )

    candidates = kg.retrieve_memory_candidates(
        "What do you remember about secure CLI bridge?",
        threshold=0.99,
        max_results=5,
        include_keyword=True,
    )

    assert candidates[0]["id"] == target["id"]
    assert candidates[0]["score"] > next(c["score"] for c in candidates if c["id"] == tag_match["id"])


def test_graph_enhanced_recall_preserves_touching_wrapper_behavior(tmp_path, monkeypatch):
    kg, _mem, _policy, _extraction = _fresh_memory_modules(tmp_path, monkeypatch)
    target = kg.save_entity("fact", "Alias Target", "Important keyword alpha.", aliases="Secret Alias", source="test")

    results = kg.graph_enhanced_recall("Secret Alias", threshold=0.99, max_results=5)

    assert any(r["id"] == target["id"] for r in results)
    assert "recalled_at" in _props(kg.get_entity(target["id"]))


def test_explicit_memory_search_touches_only_filtered_results(tmp_path, monkeypatch):
    kg, _mem, _policy, _extraction = _fresh_memory_modules(tmp_path, monkeypatch)
    shown = kg.save_entity("fact", "Rosebud Code", "The user's code word is rosebud.", source="test")
    hidden = kg.save_entity("person", "Rosebud Person", "A person with the same keyword.", source="test")

    import row_bot.tools.memory_tool as memory_tool

    result = memory_tool._search_memory("Rosebud", category="fact")

    assert "Rosebud Code" in result
    assert "Rosebud Person" not in result
    assert "recalled_at" in _props(kg.get_entity(shown["id"]))
    assert "recalled_at" not in _props(kg.get_entity(hidden["id"]))


def test_memory_policy_selects_concrete_memory_and_rejects_resource_without_anchor(tmp_path, monkeypatch):
    kg, _mem, policy, _extraction = _fresh_memory_modules(tmp_path, monkeypatch)
    target = kg.save_entity("fact", "Rosebud Code", "The user's code word is rosebud.", source="test")
    resource = kg.save_entity(
        "media",
        "Travel Notes",
        "A document about Lisbon cafes.",
        source="document:travel-notes.pdf",
    )

    decision = policy.build_auto_recall(
        "What is my Rosebud Code?",
        [],
        thread_id="thread-1",
        runtime_surface="agent",
        context_window=32_000,
    )

    assert decision.allowed is True
    assert decision.selected[0]["id"] == target["id"]
    assert decision.trace["selected_count"] >= 1
    assert "recalled_at" not in _props(kg.get_entity(target["id"]))

    monkeypatch.setattr(kg, "retrieve_memory_candidates", lambda *args, **kwargs: [
        {**kg.get_entity(resource["id"]), "score": 0.99, "lexical_score": 0.99, "decay_multiplier": 1.0, "via": "keyword", "relations": []}
    ])
    resource_decision = policy.build_auto_recall(
        "What do I like?",
        [],
        thread_id="thread-1",
        runtime_surface="agent",
        context_window=32_000,
    )

    assert resource_decision.allowed is False
    assert resource_decision.reason == "no_valid_candidates"
    assert resource_decision.trace["rejected"][0]["reason"] == "resource_not_anchored"


def test_memory_policy_skips_greetings_and_runtime_status(tmp_path, monkeypatch):
    kg, _mem, policy, _extraction = _fresh_memory_modules(tmp_path, monkeypatch)
    kg.save_entity("fact", "Rosebud Code", "The user's code word is rosebud.", source="test")

    greeting = policy.build_auto_recall(
        "hi",
        [],
        thread_id="thread-1",
        generation_id="gen-1",
        runtime_surface="agent",
        provider_id="codex",
        model_ref="model:codex:gpt-5.5",
    )
    runtime = policy.build_auto_recall("what model are you using?", [], runtime_surface="agent")

    assert greeting.allowed is False
    assert greeting.reason == "greeting_only"
    assert greeting.trace["thread_id"] == "thread-1"
    assert greeting.trace["generation_id"] == "gen-1"
    assert greeting.trace["provider_id"] == "codex"
    assert greeting.trace["model_ref"] == "model:codex:gpt-5.5"
    assert greeting.trace["memory_recall.query_build_ms"] >= 0
    assert greeting.trace["memory_recall.gating_ms"] >= 0
    assert greeting.trace["memory_recall.total_ms"] >= 0
    assert greeting.trace["memory_recall.retrieve_ms"] == 0
    assert runtime.allowed is False
    assert runtime.reason == "runtime_status_request"


def test_memory_policy_publishes_clear_fallback_warning_with_next_action(tmp_path, monkeypatch):
    kg, _mem, policy, _extraction = _fresh_memory_modules(tmp_path, monkeypatch)
    kg.save_entity("fact", "Atlas", "The user is building Atlas.", source="test")
    notifications = []

    def _fallback_candidates(*_args, diagnostics=None, **_kwargs):
        diagnostics.update({
            "semantic_status": "fallback",
            "semantic_fallback_code": "local_model_missing",
            "semantic_fallback_detail": "The selected model is not cached.",
            "semantic_wait_ms": 2,
        })
        return []

    monkeypatch.setattr(kg, "retrieve_memory_candidates", _fallback_candidates)
    monkeypatch.setattr(
        "row_bot.notifications.notify",
        lambda *args, **kwargs: notifications.append((args, kwargs)),
    )

    decision = policy.build_auto_recall(
        "What do you remember about Atlas?",
        [],
        thread_id="thread-1",
        generation_id="gen-fallback",
        runtime_surface="workflow",
    )
    notices = policy.consume_recall_fallback_notices("gen-fallback")

    assert decision.trace["semantic_status"] == "fallback"
    assert decision.trace["semantic_fallback_code"] == "local_model_missing"
    assert notices[0]["title"] == "Memory recall fallback"
    assert "continued" in notices[0]["message"]
    assert notices[0]["action"].endswith("choose Download model.")
    assert notifications[0][1]["toast_type"] == "warning"
    assert notifications[0][1]["sound"] == "none"


def test_agent_pre_model_trim_injects_policy_block_and_touches_only_selected(tmp_path, monkeypatch):
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    import row_bot.agent as agent
    agent = importlib.reload(agent)

    selected = [{
        "id": "mem123",
        "entity_type": "fact",
        "subject": "Rosebud Code",
        "description": "The user's code word is rosebud.",
        "score": 0.9,
    }]
    touched = []

    Decision = SimpleNamespace(
        allowed=True,
        reason="selected",
        query="what is my code",
        candidates_seen=2,
        trace={"selected_count": 1},
        selected=selected,
    )

    monkeypatch.setattr(agent, "get_context_size", lambda: 32_000)
    monkeypatch.setattr(agent, "_keep_browser_snapshots", lambda: 2)
    monkeypatch.setattr(agent, "is_background_workflow", lambda: False)
    monkeypatch.setattr(agent, "get_current_model", lambda: "gpt-4o")
    monkeypatch.setattr(agent, "is_cloud_model", lambda model: True)
    monkeypatch.setattr(agent, "get_cloud_provider", lambda model: "openai")
    monkeypatch.setattr("row_bot.memory_policy.build_auto_recall", lambda *args, **kwargs: Decision)
    monkeypatch.setattr("row_bot.memory_policy.touch_selected_memories", lambda decision: touched.extend(m["id"] for m in decision.selected))
    monkeypatch.setattr("row_bot.memory_policy.record_recall_trace", lambda decision, **kwargs: None)

    messages = [
        SystemMessage(content="Root system"),
        HumanMessage(content="What is my code?"),
    ]
    result = agent._pre_model_trim({
        "execution_budget": new_execution_budget("memory-recall-trim"),
        "messages": messages,
    })["llm_input_messages"]

    contents = [str(m.content) for m in result]
    recall_idx = next(i for i, content in enumerate(contents) if "Relevant long-term memory" in content)
    human_idx = next(i for i, msg in enumerate(result) if msg.type == "human")
    assert recall_idx < human_idx
    assert "[id=mem123]" in contents[recall_idx]
    assert touched == ["mem123"]


def test_extraction_reads_checkpoint_messages_without_agent_graph(tmp_path, monkeypatch):
    _kg, _mem, _policy, extraction = _fresh_memory_modules(tmp_path, monkeypatch)

    def _boom():
        raise AssertionError("get_agent_graph should not be called")

    fake_agent = SimpleNamespace(get_agent_graph=_boom)
    monkeypatch.setitem(sys.modules, "agent", fake_agent)
    monkeypatch.setitem(sys.modules, "row_bot.agent", fake_agent)
    import row_bot.threads as threads

    monkeypatch.setattr(threads, "get_latest_checkpoint_messages", lambda thread_id: [
        HumanMessage(content="My project is Atlas."),
        AIMessage(content="Got it."),
    ])

    messages = extraction._get_thread_messages("thread-1")

    assert messages == [
        {"role": "user", "content": "My project is Atlas."},
        {"role": "assistant", "content": "Got it."},
    ]


def test_extracted_memory_stores_source_context_properties(tmp_path, monkeypatch):
    kg, _mem, _policy, extraction = _fresh_memory_modules(tmp_path, monkeypatch)

    saved = extraction._dedup_and_save(
        [{"category": "project", "subject": "Atlas", "content": "User is building Atlas."}],
        source="extraction",
        source_context={"thread_id": "thread-abc", "thread_name": "Atlas chat"},
    )

    assert saved == 1
    found = kg.find_by_subject(None, "Atlas")
    props = _props(found)
    assert props["source_thread_id"] == "thread-abc"
    assert props["source_thread_name"] == "Atlas chat"
    assert props["extraction_method"] == "extraction"


def test_memory_policy_records_compact_recall_trace(tmp_path, monkeypatch):
    _kg, _mem, policy, _extraction = _fresh_memory_modules(tmp_path, monkeypatch)
    trace_path = tmp_path / "trace.json"
    monkeypatch.setattr(policy, "_RECALL_TRACE_FILE", trace_path)
    decision = policy.MemoryRecallDecision(
        True,
        "selected",
        "What is my code?",
        [{"id": "mem1"}],
        3,
        {
            "thread_id": "thread-1",
            "generation_id": "gen-1",
            "runtime_surface": "agent",
            "provider_id": "openai",
            "model_ref": "model:openai:gpt-4o",
            "memory_recall.total_pipeline_ms": 12.5,
            "memory_recall.format_ms": 1.5,
            "memory_recall.touch_ms": 0.25,
            "timings_ms": {"query_build": 0.1, "gating": 0.2},
            "top_scores": [{"id": "mem1", "sources": ["keyword"], "final": 0.9}],
        },
    )

    policy.record_recall_trace(decision, block_chars=120)

    rows = json.loads(trace_path.read_text(encoding="utf-8"))
    assert rows[-1]["selected_ids"] == ["mem1"]
    assert rows[-1]["block_chars"] == 120
    assert rows[-1]["generation_id"] == "gen-1"
    assert rows[-1]["provider_id"] == "openai"
    assert rows[-1]["model_ref"] == "model:openai:gpt-4o"
    assert rows[-1]["total_pipeline_ms"] == 12.5
    assert rows[-1]["timings_ms"]["gating"] == 0.2
    assert rows[-1]["top_scores"][0]["sources"] == ["keyword"]
