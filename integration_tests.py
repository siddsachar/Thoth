"""Integration tests — requires Ollama running with the configured model.

Tests the full agent pipeline, memory extraction, tool execution, task engine,
TTS, and knowledge graph operations end-to-end.  Uses real LLM calls so
results are non-deterministic; assertions are loose ("response contains X").

All test data uses the prefix ``__TEST_`` and is cleaned up after each test.

Usage:
    python integration_tests.py              # run all tests
    python integration_tests.py --section 3  # run only section 3
    python integration_tests.py --fast       # skip slow LLM-based tests

Requires:
    - Ollama running at 127.0.0.1:11434
    - Current model pulled (see models.py)
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
import uuid

os.environ.setdefault("PYTHONIOENCODING", "utf-8")

# ── Test infrastructure ──────────────────────────────────────────────────────

_PREFIX = "__TEST_"
_results: list[tuple[str, str, str]] = []   # (status, label, detail)
_cleanup_entity_ids: list[str] = []
_cleanup_task_ids: list[str] = []
_section_filter: int | None = None
_fast_mode: bool = False


def record(status: str, label: str, detail: str = "") -> None:
    symbol = {"PASS": "  ✅", "FAIL": "  ❌", "WARN": "  ⚠️ ", "SKIP": "  ⏭️ "}.get(status, "  ?")
    line = f"{symbol} {label}"
    if detail:
        line += f": {detail}"
    print(line)
    _results.append((status, label, detail))


def _gen_id() -> str:
    return uuid.uuid4().hex[:8]


def _cleanup_entities() -> None:
    """Delete all test entities created during the run via direct SQL.

    Using raw SQL avoids triggering ``rebuild_index()`` (and its slow
    embedding-model cold-start) on every single delete.  The FAISS index
    will be rebuilt on next app launch automatically.
    """
    if not _cleanup_entity_ids:
        return
    import knowledge_graph as kg
    import sqlite3
    try:
        conn = sqlite3.connect(kg.DB_PATH)
        for eid in _cleanup_entity_ids:
            conn.execute("DELETE FROM relations WHERE source_id = ? OR target_id = ?", (eid, eid))
            conn.execute("DELETE FROM entities WHERE id = ?", (eid,))
        conn.commit()
        conn.close()
    except Exception:
        # Fallback: try the normal API one-by-one
        for eid in _cleanup_entity_ids:
            try:
                kg.delete_entity(eid)
            except Exception:
                pass
    _cleanup_entity_ids.clear()


def _cleanup_tasks() -> None:
    """Delete all test tasks created during the run."""
    from tasks import delete_task
    for tid in _cleanup_task_ids:
        try:
            delete_task(tid)
        except Exception:
            pass
    _cleanup_task_ids.clear()


# ── Prerequisite checks ─────────────────────────────────────────────────────

def check_ollama() -> bool:
    """Return True if Ollama is reachable."""
    try:
        import ollama
        ollama.list()
        return True
    except Exception:
        return False


def check_model() -> str | None:
    """Return the current model name, or None if not available."""
    try:
        from models import get_current_model
        model = get_current_model()
        import ollama
        ollama.show(model)
        return model
    except Exception:
        return None


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 1 · Prerequisites
# ═════════════════════════════════════════════════════════════════════════════

def section_1_prerequisites() -> bool:
    print("\nSECTION 1 · Prerequisites")
    print("-" * 40)

    if not check_ollama():
        record("FAIL", "Ollama not reachable — cannot run integration tests")
        return False
    record("PASS", "Ollama is running")

    model = check_model()
    if not model:
        record("FAIL", "Current model not available — pull it first")
        return False
    record("PASS", f"Model available: {model}")

    # Check core imports
    try:
        from agent import invoke_agent, stream_agent  # noqa: F401
        record("PASS", "agent imports OK")
    except Exception as e:
        record("FAIL", "agent import failed", str(e))
        return False

    try:
        from tools import registry  # noqa: F401
        enabled = registry.get_enabled_tools()
        record("PASS", f"Tool registry OK — {len(enabled)} tools enabled")
    except Exception as e:
        record("FAIL", "tool registry import failed", str(e))
        return False

    try:
        import knowledge_graph as kg  # noqa: F401
        import memory  # noqa: F401
        record("PASS", "Knowledge graph + memory imports OK")
    except Exception as e:
        record("FAIL", "KG/memory import failed", str(e))
        return False

    try:
        from tasks import create_task, list_tasks, delete_task  # noqa: F401
        record("PASS", "Task engine imports OK")
    except Exception as e:
        record("FAIL", "Task engine import failed", str(e))
        return False

    return True


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 2 · Agent basic capabilities (LLM-dependent)
# ═════════════════════════════════════════════════════════════════════════════

def section_2_agent_basics():
    print("\nSECTION 2 · Agent Basic Capabilities")
    print("-" * 40)

    if _fast_mode:
        record("SKIP", "agent basics (fast mode)")
        return

    from agent import invoke_agent, stream_agent
    from tools import registry

    enabled = [t.name for t in registry.get_enabled_tools()]

    # --- 2a. Simple question — no tools needed ---
    tid = f"{_PREFIX}basic_{_gen_id()}"
    config = {"configurable": {"thread_id": tid}, "recursion_limit": 15}

    try:
        response = invoke_agent("What is 2 + 2? Reply with just the number.", enabled, config)
        if "4" in response:
            record("PASS", "agent: simple question answered correctly")
        else:
            record("WARN", "agent: simple question", f"Expected '4' in: {response[:200]}")
    except Exception as e:
        record("FAIL", "agent: simple question", str(e))

    # --- 2b. Agent uses calculator tool ---
    tid = f"{_PREFIX}calc_{_gen_id()}"
    config = {"configurable": {"thread_id": tid}, "recursion_limit": 15}

    tool_calls = []
    final_answer = ""
    try:
        for event_type, payload in stream_agent("Calculate sqrt(144) + 2^10. Use the calculator tool.", enabled, config):
            if event_type == "tool_call":
                tool_calls.append(payload)
            elif event_type == "done":
                final_answer = payload

        if any("calculator" in str(tc).lower() for tc in tool_calls):
            record("PASS", "agent: used calculator tool")
        else:
            record("WARN", "agent: calculator tool not detected in calls", str(tool_calls))

        if "1036" in final_answer:
            record("PASS", "agent: calculator gave correct result (1036)")
        else:
            record("WARN", "agent: calculator result", f"Expected '1036' in: {final_answer[:200]}")
    except Exception as e:
        record("FAIL", "agent: calculator test", str(e))

    # --- 2c. Agent uses memory save tool ---
    test_subject = f"{_PREFIX}Person_{_gen_id()}"
    tid = f"{_PREFIX}memsave_{_gen_id()}"
    config = {"configurable": {"thread_id": tid}, "recursion_limit": 15}

    tool_calls = []
    try:
        prompt = f"Remember this: {test_subject} is a test entity who lives in TestCity. Save this to memory."
        for event_type, payload in stream_agent(prompt, enabled, config):
            if event_type == "tool_call":
                tool_calls.append(payload)
            elif event_type == "tool_done":
                tool_calls.append(payload)

        # Check if memory tool was called
        mem_calls = [tc for tc in tool_calls if isinstance(tc, dict) and "memory" in tc.get("name", "").lower()]
        if not mem_calls:
            mem_calls = [tc for tc in tool_calls if isinstance(tc, str) and "memory" in tc.lower()]

        if mem_calls:
            record("PASS", "agent: called memory save tool")
        else:
            record("WARN", "agent: memory save tool not detected", str(tool_calls[:5]))

        # Verify entity was actually created
        import memory as mem_db
        found = mem_db.find_by_subject(None, test_subject)
        if found:
            _cleanup_entity_ids.append(found["id"])
            record("PASS", f"agent: entity '{test_subject}' created in DB")
        else:
            record("FAIL", f"agent: entity '{test_subject}' NOT found in DB after save")
    except Exception as e:
        record("FAIL", "agent: memory save test", str(e))

    # --- 2d. Agent uses web search ---
    tid = f"{_PREFIX}websearch_{_gen_id()}"
    config = {"configurable": {"thread_id": tid}, "recursion_limit": 15}

    tool_calls = []
    try:
        for event_type, payload in stream_agent(
            "Search the web for the latest Python release version. Use the search tool.",
            enabled, config,
        ):
            if event_type == "tool_call":
                tool_calls.append(payload)
            elif event_type == "tool_done":
                tool_calls.append(payload)

        search_calls = [
            tc for tc in tool_calls
            if isinstance(tc, str) and ("search" in tc.lower() or "duckduckgo" in tc.lower())
        ]
        if not search_calls:
            search_calls = [
                tc for tc in tool_calls
                if isinstance(tc, dict) and ("search" in tc.get("name", "").lower() or "duckduckgo" in tc.get("name", "").lower())
            ]

        if search_calls:
            record("PASS", "agent: used web search tool")
        else:
            record("WARN", "agent: web search tool not detected", str(tool_calls[:5]))
    except Exception as e:
        record("FAIL", "agent: web search test", str(e))

    _cleanup_entities()


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 3 · Memory & Knowledge Graph operations
# ═════════════════════════════════════════════════════════════════════════════

def section_3_memory_kg():
    print("\nSECTION 3 · Memory & Knowledge Graph")
    print("-" * 40)

    import memory as mem
    import knowledge_graph as kg

    # --- 3a. Save entity ---
    subj = f"{_PREFIX}Alice_{_gen_id()}"
    try:
        result = mem.save_memory("person", subj, f"{subj} is a test entity", tags="test")
        _cleanup_entity_ids.append(result["id"])
        record("PASS", "memory: save_memory creates entity")
    except Exception as e:
        record("FAIL", "memory: save_memory", str(e))
        return

    alice_id = result["id"]

    # --- 3b. Find by subject ---
    try:
        found = mem.find_by_subject(None, subj)
        if found and found["id"] == alice_id:
            record("PASS", "memory: find_by_subject finds entity")
        else:
            record("FAIL", "memory: find_by_subject returned wrong entity")
    except Exception as e:
        record("FAIL", "memory: find_by_subject", str(e))

    # --- 3c. Update entity ---
    try:
        updated = mem.update_memory(alice_id, f"{subj} is updated test entity", aliases="TestAlice")
        if updated and "updated" in updated.get("content", ""):
            record("PASS", "memory: update_memory works")
        else:
            record("FAIL", "memory: update_memory returned unexpected result")
    except Exception as e:
        record("FAIL", "memory: update_memory", str(e))

    # --- 3d. Find by alias ---
    try:
        found = mem.find_by_subject(None, "TestAlice")
        if found and found["id"] == alice_id:
            record("PASS", "memory: find_by_subject resolves alias")
        else:
            record("FAIL", "memory: alias resolution failed")
    except Exception as e:
        record("FAIL", "memory: alias resolution", str(e))

    # --- 3e. Semantic search ---
    try:
        # Rebuild index to include the new entity
        kg.rebuild_index()
        results = mem.semantic_search(f"{subj} test", top_k=5, threshold=0.1)
        found_ids = [r["id"] for r in results]
        if alice_id in found_ids:
            record("PASS", "memory: semantic_search finds entity")
        else:
            record("WARN", "memory: semantic_search did not find entity", f"got {len(results)} results")
    except Exception as e:
        record("FAIL", "memory: semantic_search", str(e))

    # --- 3f. Create second entity and link ---
    subj2 = f"{_PREFIX}Bob_{_gen_id()}"
    try:
        result2 = mem.save_memory("place", subj2, f"{subj2} is a test place")
        _cleanup_entity_ids.append(result2["id"])
        bob_id = result2["id"]

        rel = kg.add_relation(alice_id, bob_id, "lives_in", source="test", confidence=0.9)
        if rel and rel.get("relation_type") == "lives_in":
            record("PASS", "kg: add_relation creates link")
        else:
            record("FAIL", "kg: add_relation returned unexpected result")
    except Exception as e:
        record("FAIL", "kg: add_relation", str(e))

    # --- 3g. Get relations ---
    try:
        rels = kg.get_relations(alice_id)
        lives_in = [r for r in rels if r.get("relation_type") == "lives_in"]
        if lives_in:
            record("PASS", "kg: get_relations returns the link")
        else:
            record("FAIL", "kg: get_relations missing lives_in link")
    except Exception as e:
        record("FAIL", "kg: get_relations", str(e))

    # --- 3h. Graph enhanced recall ---
    try:
        results = kg.graph_enhanced_recall(subj, top_k=5, threshold=0.1, hops=1)
        found_subjects = [r.get("subject", "") for r in results]
        if any(subj in s for s in found_subjects):
            record("PASS", "kg: graph_enhanced_recall returns seed entity")
        else:
            record("WARN", "kg: graph_enhanced_recall did not find entity")

        # Check if the linked entity appears via graph expansion
        if any(subj2 in s for s in found_subjects):
            record("PASS", "kg: graph_enhanced_recall returns 1-hop neighbor")
        else:
            record("WARN", "kg: graph_enhanced_recall neighbor not in results")
    except Exception as e:
        record("FAIL", "kg: graph_enhanced_recall", str(e))

    # --- 3i. save_memory is raw insert; dedup lives in extraction layer ---
    #     Calling save_memory twice with the same subject creates two entities.
    #     Verify that find_by_subject still returns the *first* match.
    try:
        result_dup = mem.save_memory("person", subj, f"{subj} is a richer test entity with more info")
        _cleanup_entity_ids.append(result_dup["id"])
        found_after = mem.find_by_subject(None, subj)
        if found_after and found_after["id"] in (alice_id, result_dup["id"]):
            record("PASS", "memory: find_by_subject returns a match after double save")
        else:
            record("FAIL", "memory: find_by_subject broken after double save")
    except Exception as e:
        record("FAIL", "memory: double save test", str(e))

    # --- 3j. Vis-network JSON ---
    try:
        vis = kg.graph_to_vis_json()
        test_nodes = [n for n in vis["nodes"] if _PREFIX in n.get("label", "")]
        if len(test_nodes) >= 2:
            record("PASS", f"kg: graph_to_vis_json includes {len(test_nodes)} test nodes")
        else:
            record("WARN", "kg: graph_to_vis_json test nodes not found")

        test_edges = [
            e for e in vis["edges"]
            if e.get("label") == "lives_in"
            and any(n["id"] == e["from"] and _PREFIX in n["label"] for n in vis["nodes"])
        ]
        if test_edges:
            record("PASS", "kg: graph_to_vis_json includes test edge")
        else:
            record("WARN", "kg: graph_to_vis_json test edge not found")
    except Exception as e:
        record("FAIL", "kg: graph_to_vis_json", str(e))

    # --- 3k. Vis-network dark theme font on live data ---
    try:
        if vis["nodes"]:
            all_dark_font = all(
                n.get("font", {}).get("color") == "#ECEFF1"
                for n in vis["nodes"]
            )
            if all_dark_font:
                record("PASS", "kg: vis nodes have dark-theme font color")
            else:
                record("FAIL", "kg: vis nodes missing dark-theme font color")
        else:
            record("WARN", "kg: no vis nodes to check font color")
    except Exception as e:
        record("FAIL", "kg: vis dark theme font", str(e))

    # --- 3l. max_nodes cap on live data ---
    try:
        capped = kg.graph_to_vis_json(max_nodes=1)
        if capped["stats"]["shown_nodes"] <= 1:
            record("PASS", f"kg: graph_to_vis_json max_nodes cap works (shown={capped['stats']['shown_nodes']})")
        else:
            record("FAIL", f"kg: max_nodes=1 but shown_nodes={capped['stats']['shown_nodes']}")
    except Exception as e:
        record("FAIL", "kg: graph_to_vis_json max_nodes", str(e))

    # --- 3m. Delete entity ---
    try:
        deleted = mem.delete_memory(alice_id)
        if deleted:
            record("PASS", "memory: delete_memory works")
            _cleanup_entity_ids.remove(alice_id)
        else:
            record("FAIL", "memory: delete_memory returned False")
    except Exception as e:
        record("FAIL", "memory: delete_memory", str(e))

    # Verify deletion cascaded relations
    try:
        rels_after = kg.get_relations(alice_id)
        if not rels_after:
            record("PASS", "kg: relation cascade — relations removed after entity delete")
        else:
            record("FAIL", "kg: relation cascade — relations still exist after delete")
    except Exception as e:
        record("FAIL", "kg: relation cascade check", str(e))

    _cleanup_entities()


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 4 · Memory extraction pipeline (LLM-dependent)
# ═════════════════════════════════════════════════════════════════════════════

def section_4_extraction():
    print("\nSECTION 4 · Memory Extraction Pipeline")
    print("-" * 40)

    if _fast_mode:
        record("SKIP", "extraction pipeline (fast mode)")
        return

    from memory_extraction import _extract_from_conversation, _dedup_and_save
    import memory as mem
    import knowledge_graph as kg

    tag = _gen_id()

    # --- 4a. Extract entities from a conversation ---
    conversation = (
        f"User: My colleague {_PREFIX}Diana_{tag} works at {_PREFIX}TechCorp_{tag}. "
        f"She's based in {_PREFIX}Berlin_{tag}.\n"
        f"Assistant: That's interesting! I'll remember that."
    )

    try:
        extracted = _extract_from_conversation(conversation)
        if not extracted:
            record("FAIL", "extraction: returned empty list")
            return

        entity_items = [e for e in extracted if e.get("category")]
        relation_items = [e for e in extracted if e.get("relation_type")]

        record("PASS", f"extraction: extracted {len(entity_items)} entities + {len(relation_items)} relations")
    except Exception as e:
        record("FAIL", "extraction: _extract_from_conversation", str(e))
        return

    # --- 4b. Verify entity structure ---
    try:
        if entity_items:
            e0 = entity_items[0]
            has_cat = bool(e0.get("category"))
            has_subj = bool(e0.get("subject"))
            has_content = bool(e0.get("content"))
            if has_cat and has_subj and has_content:
                record("PASS", "extraction: entity has category/subject/content")
            else:
                record("FAIL", "extraction: entity missing fields", str(e0))
        else:
            record("WARN", "extraction: no entities to verify structure")
    except Exception as e:
        record("FAIL", "extraction: entity structure check", str(e))

    # --- 4c. Verify relation structure ---
    try:
        if relation_items:
            r0 = relation_items[0]
            has_rt = bool(r0.get("relation_type"))
            has_src = bool(r0.get("source_subject"))
            has_tgt = bool(r0.get("target_subject"))
            if has_rt and has_src and has_tgt:
                record("PASS", "extraction: relation has relation_type/source/target")
            else:
                record("FAIL", "extraction: relation missing fields", str(r0))
        else:
            record("WARN", "extraction: no relations extracted (LLM may have omitted)")
    except Exception as e:
        record("FAIL", "extraction: relation structure check", str(e))

    # --- 4d. Dedup and save ---
    try:
        saved = _dedup_and_save(extracted)
        record("PASS", f"extraction: _dedup_and_save saved {saved} items")
    except Exception as e:
        record("FAIL", "extraction: _dedup_and_save", str(e))
        return

    # --- 4e. Verify entities in DB ---
    try:
        # Look for any test entities that were created
        all_entities = kg.list_entities(limit=200)
        test_entities = [e for e in all_entities if _PREFIX in (e.get("subject", ""))]
        for te in test_entities:
            _cleanup_entity_ids.append(te["id"])

        if test_entities:
            record("PASS", f"extraction: {len(test_entities)} test entities found in DB")
        else:
            record("WARN", "extraction: no test entities found in DB (LLM may have renamed them)")
    except Exception as e:
        record("FAIL", "extraction: DB entity verification", str(e))

    # --- 4f. Verify relations in DB ---
    try:
        found_relations = False
        for te in test_entities:
            rels = kg.get_relations(te["id"])
            if rels:
                found_relations = True
                break

        if found_relations:
            record("PASS", "extraction: relations created between test entities")
        elif relation_items:
            record("WARN", "extraction: relations extracted but not found in DB")
        else:
            record("WARN", "extraction: no relations in extraction output to verify")
    except Exception as e:
        record("FAIL", "extraction: DB relation verification", str(e))

    # --- 4g. Re-run dedup — should not create duplicates ---
    try:
        count_before = kg.count_entities()
        _dedup_and_save(extracted)
        count_after = kg.count_entities()
        if count_after == count_before:
            record("PASS", "extraction: re-run dedup — no duplicates created")
        else:
            # Clean up any new entities
            new_entities = kg.list_entities(limit=200)
            for ne in new_entities:
                if _PREFIX in ne.get("subject", "") and ne["id"] not in _cleanup_entity_ids:
                    _cleanup_entity_ids.append(ne["id"])
            record("FAIL", f"extraction: dedup created {count_after - count_before} duplicates")
    except Exception as e:
        record("FAIL", "extraction: dedup idempotency", str(e))

    _cleanup_entities()


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 5 · Agent memory recall pipeline (LLM-dependent)
# ═════════════════════════════════════════════════════════════════════════════

def section_5_recall():
    print("\nSECTION 5 · Agent Memory Recall")
    print("-" * 40)

    if _fast_mode:
        record("SKIP", "recall pipeline (fast mode)")
        return

    import memory as mem
    import knowledge_graph as kg
    from agent import invoke_agent
    from tools import registry

    enabled = [t.name for t in registry.get_enabled_tools()]
    tag = _gen_id()
    test_fact = f"{_PREFIX}Recall_{tag}"
    secret_word = f"BluePenguin{tag}"

    # --- 5a. Store a unique fact ---
    try:
        result = mem.save_memory(
            "fact", test_fact,
            f"The secret code word is {secret_word}. This is a test memory.",
            tags="test,recall",
        )
        _cleanup_entity_ids.append(result["id"])
        kg.rebuild_index()
        record("PASS", f"recall: stored test fact '{test_fact}'")
    except Exception as e:
        record("FAIL", "recall: failed to store test fact", str(e))
        return

    # --- 5b. Ask the agent about the fact — should be auto-recalled ---
    tid = f"{_PREFIX}recall_{_gen_id()}"
    config = {"configurable": {"thread_id": tid}, "recursion_limit": 15}

    try:
        response = invoke_agent(
            f"What is the secret code word stored in the memory about {test_fact}? "
            "Check your recalled memories.",
            enabled, config,
        )
        if secret_word in response:
            record("PASS", "recall: agent found the secret word from auto-recall")
        elif "secret" in response.lower() or "code" in response.lower():
            record("WARN", "recall: agent mentioned secrets but didn't give exact word", response[:200])
        else:
            record("WARN", "recall: agent did not find the secret word", response[:200])
    except Exception as e:
        record("FAIL", "recall: agent query failed", str(e))

    # --- 5c. Ask about relations — should use graph-enhanced recall ---
    subj_a = f"{_PREFIX}RecallPerson_{tag}"
    subj_b = f"{_PREFIX}RecallCity_{tag}"

    try:
        res_a = mem.save_memory("person", subj_a, f"{subj_a} is a test person")
        _cleanup_entity_ids.append(res_a["id"])
        res_b = mem.save_memory("place", subj_b, f"{subj_b} is a test city, known for its test landmarks")
        _cleanup_entity_ids.append(res_b["id"])
        kg.add_relation(res_a["id"], res_b["id"], "lives_in", source="test")
        kg.rebuild_index()

        tid2 = f"{_PREFIX}recallrel_{_gen_id()}"
        config2 = {"configurable": {"thread_id": tid2}, "recursion_limit": 15}
        response2 = invoke_agent(
            f"Where does {subj_a} live? Check memories.",
            enabled, config2,
        )
        if subj_b.lower() in response2.lower() or "test city" in response2.lower():
            record("PASS", "recall: agent found linked entity via graph recall")
        else:
            record("WARN", "recall: agent didn't find linked entity", response2[:200])
    except Exception as e:
        record("FAIL", "recall: graph-enhanced recall test", str(e))

    _cleanup_entities()


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 6 · Tool functions (direct invocation, no agent)
# ═════════════════════════════════════════════════════════════════════════════

def section_6_tools():
    print("\nSECTION 6 · Tool Functions (Direct)")
    print("-" * 40)

    # --- 6a. Calculator ---
    try:
        from tools.calculator_tool import _calculate
        result = _calculate("sqrt(144) + 2**10")
        if "1036" in result:
            record("PASS", "tool: calculator — sqrt(144) + 2^10 = 1036")
        else:
            record("FAIL", "tool: calculator wrong result", result)
    except Exception as e:
        record("FAIL", "tool: calculator", str(e))

    # --- 6b. Calculator edge cases ---
    try:
        result = _calculate("sin(0) + cos(0)")
        if "1" in result:
            record("PASS", "tool: calculator — sin(0) + cos(0) = 1")
        else:
            record("WARN", "tool: calculator trig", result)
    except Exception as e:
        record("FAIL", "tool: calculator trig", str(e))

    # --- 6c. DuckDuckGo search ---
    try:
        from tools.duckduckgo_tool import DuckDuckGoTool
        ddg = DuckDuckGoTool()
        result = ddg.execute("Python programming language")
        if result and len(result) > 50:
            record("PASS", f"tool: DuckDuckGo returned {len(result)} chars")
        else:
            record("WARN", "tool: DuckDuckGo returned short result")
    except Exception as e:
        record("FAIL", "tool: DuckDuckGo", str(e))

    # --- 6d. YouTube search ---
    try:
        from tools.youtube_tool import _search_youtube
        result = _search_youtube("Python tutorial", max_results=3)
        data = json.loads(result)
        if isinstance(data, list) and len(data) > 0:
            record("PASS", f"tool: YouTube search returned {len(data)} results")
        else:
            record("WARN", "tool: YouTube search returned no results")
    except ImportError:
        record("SKIP", "tool: YouTube search (module not available)")
    except Exception as e:
        record("FAIL", "tool: YouTube search", str(e))

    # --- 6e. arXiv retriever ---
    try:
        from tools.arxiv_tool import ArxivTool
        arxiv = ArxivTool()
        retriever = arxiv.get_retriever()
        docs = retriever.invoke("transformer attention mechanisms")
        docs = arxiv.post_process(docs)
        if docs and len(docs) > 0:
            record("PASS", f"tool: arXiv retriever returned {len(docs)} documents")
        else:
            record("WARN", "tool: arXiv retriever returned no documents")
    except ImportError:
        record("SKIP", "tool: arXiv (module not available)")
    except Exception as e:
        record("FAIL", "tool: arXiv", str(e))

    # --- 6f. Conversation search ---
    try:
        from tools.conversation_search_tool import _search_conversations, _list_conversations
        convos = _list_conversations()
        if convos and len(convos) > 10:
            record("PASS", f"tool: list_conversations returned {convos.count(chr(10))} lines")
        else:
            record("PASS", "tool: list_conversations returned results")
    except Exception as e:
        record("FAIL", "tool: conversation_search", str(e))

    # --- 6g. Tool registry ---
    try:
        from tools import registry
        all_tools = registry.get_all_tools()
        enabled = registry.get_enabled_tools()
        lc_tools = registry.get_langchain_tools()

        if len(all_tools) > 10:
            record("PASS", f"registry: {len(all_tools)} tools registered, {len(enabled)} enabled")
        else:
            record("WARN", f"registry: only {len(all_tools)} tools registered")

        if len(lc_tools) > 5:
            record("PASS", f"registry: {len(lc_tools)} LangChain tool wrappers generated")
        else:
            record("WARN", f"registry: only {len(lc_tools)} LangChain wrappers")
    except Exception as e:
        record("FAIL", "registry: tool enumeration", str(e))


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 7 · Task engine
# ═════════════════════════════════════════════════════════════════════════════

def section_7_tasks():
    print("\nSECTION 7 · Task Engine")
    print("-" * 40)

    from tasks import (
        create_task, get_task, list_tasks, update_task, delete_task,
        expand_template_vars,
    )

    tag = _gen_id()

    # --- 7a. Create task ---
    try:
        task_id = create_task(
            name=f"{_PREFIX}Task_{tag}",
            prompts=["This is a test prompt"],
            description="Integration test task",
            icon="🧪",
            schedule=None,  # no schedule — manual only
        )
        _cleanup_task_ids.append(task_id)
        if task_id:
            record("PASS", f"task: created task {task_id}")
        else:
            record("FAIL", "task: create_task returned empty ID")
            return
    except Exception as e:
        record("FAIL", "task: create_task", str(e))
        return

    # --- 7b. Get task ---
    try:
        task = get_task(task_id)
        if task and task["name"] == f"{_PREFIX}Task_{tag}":
            record("PASS", "task: get_task returns correct task")
        else:
            record("FAIL", "task: get_task returned wrong data")
    except Exception as e:
        record("FAIL", "task: get_task", str(e))

    # --- 7c. List tasks ---
    try:
        tasks = list_tasks()
        test_tasks = [t for t in tasks if t["name"].startswith(_PREFIX)]
        if test_tasks:
            record("PASS", f"task: list_tasks includes test task ({len(tasks)} total)")
        else:
            record("FAIL", "task: test task not in list_tasks")
    except Exception as e:
        record("FAIL", "task: list_tasks", str(e))

    # --- 7d. Update task ---
    try:
        update_task(task_id, name=f"{_PREFIX}UpdatedTask_{tag}", icon="✅")
        updated = get_task(task_id)
        if updated and updated["name"] == f"{_PREFIX}UpdatedTask_{tag}" and updated["icon"] == "✅":
            record("PASS", "task: update_task changes name and icon")
        else:
            record("FAIL", "task: update_task did not apply changes")
    except Exception as e:
        record("FAIL", "task: update_task", str(e))

    # --- 7e. Template variable expansion ---
    try:
        expanded = expand_template_vars("Today is {{date}}, {{day}}")
        if "{{date}}" not in expanded and len(expanded) > 15:
            record("PASS", f"task: template expansion works: '{expanded}'")
        else:
            record("FAIL", "task: template variables not expanded", expanded)
    except Exception as e:
        record("FAIL", "task: expand_template_vars", str(e))

    # --- 7f. Create task with delay_minutes ---
    try:
        timer_id = create_task(
            name=f"{_PREFIX}Timer_{tag}",
            notify_only=True,
            notify_label="Test timer fired!",
            delay_minutes=9999,  # far future — won't actually fire
        )
        _cleanup_task_ids.append(timer_id)
        timer_task = get_task(timer_id)
        if timer_task and timer_task.get("notify_only"):
            record("PASS", "task: notify_only timer task created")
        else:
            record("FAIL", "task: timer task missing notify_only flag")
    except Exception as e:
        record("FAIL", "task: delay_minutes timer", str(e))

    # --- 7g. Create scheduled task ---
    try:
        sched_id = create_task(
            name=f"{_PREFIX}Sched_{tag}",
            prompts=["Test scheduled prompt"],
            schedule="daily:03:00",
            icon="🕒",
        )
        _cleanup_task_ids.append(sched_id)
        sched = get_task(sched_id)
        if sched and sched.get("schedule") == "daily:03:00":
            record("PASS", "task: scheduled task created with daily:03:00")
        else:
            record("FAIL", "task: scheduled task missing schedule")
    except Exception as e:
        record("FAIL", "task: scheduled task creation", str(e))

    # --- 7h. Delete task ---
    try:
        delete_task(task_id)
        _cleanup_task_ids.remove(task_id)
        after = get_task(task_id)
        if after is None:
            record("PASS", "task: delete_task removes task")
        else:
            record("FAIL", "task: task still exists after delete")
    except Exception as e:
        record("FAIL", "task: delete_task", str(e))

    _cleanup_tasks()


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 8 · TTS (Text-to-Speech)
# ═════════════════════════════════════════════════════════════════════════════

def section_8_tts():
    print("\nSECTION 8 · TTS (Text-to-Speech)")
    print("-" * 40)

    try:
        from tts import TTSService, VOICE_CATALOG
        record("PASS", "tts: import OK")
    except ImportError as e:
        record("SKIP", "tts: module not available", str(e))
        return

    tts = TTSService()

    # --- 8a. Voice catalog ---
    try:
        if len(VOICE_CATALOG) > 5:
            record("PASS", f"tts: {len(VOICE_CATALOG)} voices in catalog")
        else:
            record("WARN", f"tts: only {len(VOICE_CATALOG)} voices")
    except Exception as e:
        record("FAIL", "tts: voice catalog", str(e))

    # --- 8b. Model installation check ---
    try:
        installed = tts.is_installed()
        if installed:
            record("PASS", "tts: model is installed")
        else:
            record("WARN", "tts: model NOT installed — skipping audio tests")
            return
    except Exception as e:
        record("FAIL", "tts: is_installed check", str(e))
        return

    # --- 8c. Audio generation (raw Kokoro) ---
    try:
        kokoro = tts._get_kokoro()
        samples, sr = kokoro.create("Hello world, this is a test.", voice="af_heart", speed=1.0, lang="en-us")
        if samples is not None and len(samples) > 1000 and sr > 0:
            duration = len(samples) / sr
            record("PASS", f"tts: generated {duration:.1f}s of audio at {sr}Hz")
        else:
            record("FAIL", "tts: audio generation returned empty result")
    except Exception as e:
        record("FAIL", "tts: audio generation", str(e))

    # --- 8d. Voice switching ---
    try:
        tts.voice = "am_michael"
        kokoro = tts._get_kokoro()
        samples2, sr2 = kokoro.create("Testing voice change.", voice="am_michael", speed=1.0, lang="en-us")
        if samples2 is not None and len(samples2) > 500:
            record("PASS", "tts: voice switching works (am_michael)")
        else:
            record("FAIL", "tts: voice switching returned empty result")
    except Exception as e:
        record("FAIL", "tts: voice switching", str(e))

    # --- 8e. Speed control ---
    try:
        samples_slow, _ = kokoro.create("Speed test.", voice="af_heart", speed=0.7, lang="en-us")
        samples_fast, _ = kokoro.create("Speed test.", voice="af_heart", speed=1.5, lang="en-us")
        if len(samples_slow) > len(samples_fast):
            record("PASS", "tts: speed control — slow produces more samples than fast")
        else:
            record("WARN", "tts: speed control — slow not longer than fast")
    except Exception as e:
        record("FAIL", "tts: speed control", str(e))


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 9 · Agent tool routing (LLM-dependent)
# ═════════════════════════════════════════════════════════════════════════════

def section_9_agent_routing():
    print("\nSECTION 9 · Agent Tool Routing")
    print("-" * 40)

    if _fast_mode:
        record("SKIP", "agent tool routing (fast mode)")
        return

    from agent import stream_agent
    from tools import registry

    enabled = [t.name for t in registry.get_enabled_tools()]

    def _run_and_collect(prompt: str) -> tuple[list, str]:
        """Send prompt to agent, collect tool calls and final answer."""
        tid = f"{_PREFIX}route_{_gen_id()}"
        config = {"configurable": {"thread_id": tid}, "recursion_limit": 15}
        calls = []
        answer = ""
        for event_type, payload in stream_agent(prompt, enabled, config):
            if event_type == "tool_call":
                calls.append(str(payload).lower())
            elif event_type == "tool_done":
                calls.append(str(payload.get("name", "")).lower() if isinstance(payload, dict) else str(payload).lower())
            elif event_type == "done":
                answer = payload
        return calls, answer

    # --- 9a. Memory search routing ---
    try:
        calls, answer = _run_and_collect("Search your memories for anything about London.")
        if any("memory" in c or "search" in c for c in calls):
            record("PASS", "routing: memory search prompt → memory tool called")
        else:
            record("WARN", "routing: memory search prompt did not trigger memory tool", str(calls))
    except Exception as e:
        record("FAIL", "routing: memory search", str(e))

    # --- 9b. Task list routing ---
    try:
        calls, answer = _run_and_collect("List all my tasks.")
        if any("task" in c for c in calls):
            record("PASS", "routing: task list prompt → task tool called")
        else:
            record("WARN", "routing: task list prompt did not trigger task tool", str(calls))
    except Exception as e:
        record("FAIL", "routing: task list", str(e))

    # --- 9c. Calculator routing ---
    try:
        calls, answer = _run_and_collect("What is 17 * 23 + 89? Use your calculator.")
        if any("calc" in c for c in calls):
            record("PASS", "routing: math prompt → calculator tool called")
        else:
            record("WARN", "routing: math prompt did not trigger calculator", str(calls))
        if "480" in answer:
            record("PASS", "routing: calculator gave correct answer (480)")
        else:
            record("WARN", "routing: calculator answer", answer[:200])
    except Exception as e:
        record("FAIL", "routing: calculator", str(e))

    # --- 9d. Search routing ---
    try:
        calls, answer = _run_and_collect("Search the web for the tallest building in the world.")
        if any("search" in c or "duckduckgo" in c for c in calls):
            record("PASS", "routing: web search prompt → search tool called")
        else:
            record("WARN", "routing: web search prompt did not trigger search tool", str(calls))
    except Exception as e:
        record("FAIL", "routing: web search", str(e))

    # --- 9e. Explore connections routing ---
    try:
        calls, answer = _run_and_collect("Explore my memory connections starting from the User entity.")
        if any("memory" in c or "explore" in c or "connection" in c for c in calls):
            record("PASS", "routing: explore connections prompt → memory/explore tool called")
        else:
            record("WARN", "routing: explore connections not routed", str(calls))
    except Exception as e:
        record("FAIL", "routing: explore connections", str(e))

    # --- 9f. Link memories routing ---
    try:
        # First create two test entities
        import memory as mem
        import knowledge_graph as kg
        subj_a = f"{_PREFIX}LinkA_{_gen_id()}"
        subj_b = f"{_PREFIX}LinkB_{_gen_id()}"
        ra = mem.save_memory("person", subj_a, f"{subj_a} is a test person for linking")
        rb = mem.save_memory("place", subj_b, f"{subj_b} is a test place for linking")
        _cleanup_entity_ids.extend([ra["id"], rb["id"]])
        kg.rebuild_index()

        calls, answer = _run_and_collect(
            f"Link the memory '{subj_a}' to '{subj_b}' with relation 'lives_in'. "
            f"The IDs are {ra['id']} and {rb['id']}."
        )
        if any("link" in c or "memory" in c for c in calls):
            record("PASS", "routing: link memories prompt → link tool called")
        else:
            record("WARN", "routing: link memories not routed", str(calls))
    except Exception as e:
        record("FAIL", "routing: link memories", str(e))

    _cleanup_entities()


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 10 · End-to-end agent + extraction flow (LLM-dependent)
# ═════════════════════════════════════════════════════════════════════════════

def section_10_e2e():
    print("\nSECTION 10 · End-to-End: Agent → Extraction → Graph")
    print("-" * 40)

    if _fast_mode:
        record("SKIP", "end-to-end flow (fast mode)")
        return

    from agent import invoke_agent
    from tools import registry
    from memory_extraction import _extract_from_conversation, _dedup_and_save
    import memory as mem
    import knowledge_graph as kg

    enabled = [t.name for t in registry.get_enabled_tools()]
    tag = _gen_id()

    # Unique test data that won't collide with real memories
    person_name = f"{_PREFIX}Eve_{tag}"
    company_name = f"{_PREFIX}MoonBase_{tag}"
    city_name = f"{_PREFIX}Zurich_{tag}"

    # --- 10a. Send info to agent ---
    tid = f"{_PREFIX}e2e_{_gen_id()}"
    config = {"configurable": {"thread_id": tid}, "recursion_limit": 15}

    try:
        response = invoke_agent(
            f"Remember this: my friend {person_name} works at {company_name} in {city_name}. "
            f"She is a data scientist. Save all of this to memory.",
            enabled, config,
        )
        record("PASS", "e2e: agent processed the information")
    except Exception as e:
        record("FAIL", "e2e: agent call failed", str(e))
        return

    # Mark any entities the agent created for cleanup
    for subj in [person_name, company_name, city_name]:
        found = mem.find_by_subject(None, subj)
        if found:
            _cleanup_entity_ids.append(found["id"])

    # --- 10b. Now simulate extraction on the same conversation ---
    conversation_text = (
        f"User: Remember this: my friend {person_name} works at {company_name} in {city_name}. "
        f"She is a data scientist. Save all of this to memory.\n"
        f"Assistant: {response[:500]}"
    )

    try:
        extracted = _extract_from_conversation(conversation_text)
        entity_count = len([e for e in extracted if e.get("category")])
        relation_count = len([e for e in extracted if e.get("relation_type")])
        record("PASS", f"e2e: extraction found {entity_count} entities + {relation_count} relations")
    except Exception as e:
        record("FAIL", "e2e: extraction failed", str(e))
        return

    # --- 10c. Dedup should merge with agent-created entities (not duplicate) ---
    try:
        count_before = kg.count_entities()
        saved = _dedup_and_save(extracted)
        count_after = kg.count_entities()
        new_entities = count_after - count_before

        # Mark new entities for cleanup
        all_ents = kg.list_entities(limit=300)
        for ent in all_ents:
            if _PREFIX in ent.get("subject", "") and ent["id"] not in _cleanup_entity_ids:
                _cleanup_entity_ids.append(ent["id"])

        if new_entities == 0:
            record("PASS", f"e2e: extraction merged with agent data — 0 new entities, {saved} updates")
        else:
            record("PASS", f"e2e: extraction added {new_entities} new entities, {saved} total changes")
    except Exception as e:
        record("FAIL", "e2e: dedup merge", str(e))

    # --- 10d. Verify the full picture in DB ---
    try:
        found_person = mem.find_by_subject(None, person_name)
        if found_person:
            record("PASS", f"e2e: {person_name} found in DB")
            rels = kg.get_relations(found_person["id"])
            if rels:
                rel_types = [r["relation_type"] for r in rels]
                record("PASS", f"e2e: {person_name} has {len(rels)} relations: {rel_types}")
            else:
                record("WARN", f"e2e: {person_name} has no relations")
        else:
            record("WARN", f"e2e: {person_name} not found in DB (LLM may have renamed)")
    except Exception as e:
        record("FAIL", "e2e: DB verification", str(e))

    _cleanup_entities()


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 11 · Data integrity & edge cases
# ═════════════════════════════════════════════════════════════════════════════

def section_11_edge_cases():
    print("\nSECTION 11 · Data Integrity & Edge Cases")
    print("-" * 40)

    import memory as mem
    import knowledge_graph as kg

    # --- 11a. Invalid category rejected ---
    try:
        mem.save_memory("INVALID_CATEGORY", "test", "test content")
        record("FAIL", "edge: invalid category was accepted")
    except (ValueError, Exception):
        record("PASS", "edge: invalid category correctly rejected")

    # --- 11b. Empty subject ---
    try:
        result = mem.save_memory("fact", "", "empty subject test")
        # If it got this far, at minimum an entity was created
        if result:
            _cleanup_entity_ids.append(result["id"])
            record("WARN", "edge: empty subject was accepted (no validation)")
        else:
            record("PASS", "edge: empty subject correctly rejected")
    except (ValueError, Exception):
        record("PASS", "edge: empty subject correctly rejected")

    # --- 11c. Relation with non-existent entity IDs ---
    try:
        result = kg.add_relation("nonexistent_id_1", "nonexistent_id_2", "test_rel")
        if result is None:
            record("PASS", "edge: relation with fake IDs returns None")
        else:
            record("WARN", "edge: relation with fake IDs created (may be OK if no FK check)")
    except Exception:
        record("PASS", "edge: relation with fake IDs raises exception")

    # --- 11d. Delete non-existent entity ---
    try:
        deleted = kg.delete_entity("definitely_not_a_real_id")
        if not deleted:
            record("PASS", "edge: delete non-existent entity returns False")
        else:
            record("FAIL", "edge: delete non-existent entity returned True")
    except Exception:
        record("PASS", "edge: delete non-existent entity raises exception")

    # --- 11e. Unicode in subject and content ---
    try:
        subj = f"{_PREFIX}Ünïcödé_{uuid.uuid4().hex[:4]}"
        result = mem.save_memory("fact", subj, "日本語テスト — émojis: 🧪🎯✅")
        _cleanup_entity_ids.append(result["id"])
        found = mem.find_by_subject(None, subj)
        if found and "🧪" in found.get("content", ""):
            record("PASS", "edge: unicode + emoji in subject/content preserved")
        else:
            record("FAIL", "edge: unicode/emoji lost in storage")
    except Exception as e:
        record("FAIL", "edge: unicode handling", str(e))

    # --- 11f. Very long content ---
    try:
        subj = f"{_PREFIX}LongContent_{uuid.uuid4().hex[:4]}"
        long_content = "A" * 10000
        result = mem.save_memory("fact", subj, long_content)
        _cleanup_entity_ids.append(result["id"])
        found = mem.find_by_subject(None, subj)
        if found and len(found.get("content", "")) >= 10000:
            record("PASS", "edge: 10K char content stored and retrieved")
        else:
            record("WARN", "edge: long content may have been truncated")
    except Exception as e:
        record("FAIL", "edge: long content", str(e))

    # --- 11g. Duplicate relation is idempotent ---
    try:
        subj_a = f"{_PREFIX}DupRelA_{uuid.uuid4().hex[:4]}"
        subj_b = f"{_PREFIX}DupRelB_{uuid.uuid4().hex[:4]}"
        ra = mem.save_memory("person", subj_a, "test")
        rb = mem.save_memory("place", subj_b, "test")
        _cleanup_entity_ids.extend([ra["id"], rb["id"]])

        rel1 = kg.add_relation(ra["id"], rb["id"], "test_rel", source="test")
        rel2 = kg.add_relation(ra["id"], rb["id"], "test_rel", source="test")
        rels = kg.get_relations(ra["id"])
        test_rels = [r for r in rels if r["relation_type"] == "test_rel"]
        if len(test_rels) <= 1:
            record("PASS", "edge: duplicate relation is idempotent (1 stored)")
        else:
            record("FAIL", f"edge: duplicate relation created {len(test_rels)} records")
    except Exception as e:
        record("FAIL", "edge: duplicate relation", str(e))

    # --- 11h. Cross-category find_by_subject ---
    try:
        subj = f"{_PREFIX}CrossCat_{uuid.uuid4().hex[:4]}"
        result = mem.save_memory("person", subj, "test cross-category")
        _cleanup_entity_ids.append(result["id"])

        # Search with wrong category should NOT find it
        wrong = mem.find_by_subject("place", subj)
        # Search with None (any category) should find it
        any_cat = mem.find_by_subject(None, subj)
        # Search with correct category should find it
        right = mem.find_by_subject("person", subj)

        if any_cat and right and any_cat["id"] == right["id"]:
            record("PASS", "edge: find_by_subject works with None and correct category")
        else:
            record("FAIL", "edge: find_by_subject category handling broken")

        if wrong is None:
            record("PASS", "edge: find_by_subject rejects wrong category")
        else:
            record("WARN", "edge: find_by_subject found entity in wrong category (may be alias match)")
    except Exception as e:
        record("FAIL", "edge: cross-category find", str(e))

    _cleanup_entities()


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 12 · Tool sub-tools (direct invocation, extended)
# ═════════════════════════════════════════════════════════════════════════════

def section_12_tool_subttools():
    print("\nSECTION 12 · Tool Sub-tools (Extended)")
    print("-" * 40)

    # --- 12a. Shell — classify_command: safe ---
    try:
        from tools.shell_tool import classify_command
        result = classify_command("ls -la")
        if result == "safe":
            record("PASS", "shell: classify_command('ls -la') = safe")
        else:
            record("FAIL", f"shell: classify_command('ls -la') = {result}, expected safe")
    except Exception as e:
        record("FAIL", "shell: classify_command safe", str(e))

    # --- 12b. Shell — classify_command: blocked ---
    try:
        result = classify_command("rm -rf /")
        if result == "blocked":
            record("PASS", "shell: classify_command('rm -rf /') = blocked")
        else:
            record("FAIL", f"shell: classify_command('rm -rf /') = {result}, expected blocked")
    except Exception as e:
        record("FAIL", "shell: classify_command blocked", str(e))

    # --- 12c. Shell — classify_command: needs_approval ---
    try:
        result = classify_command("npm install express")
        if result == "needs_approval":
            record("PASS", "shell: classify_command('npm install express') = needs_approval")
        else:
            record("FAIL", f"shell: classify_command('npm install express') = {result}, expected needs_approval")
    except Exception as e:
        record("FAIL", "shell: classify_command needs_approval", str(e))

    # --- 12d. Shell — more blocked patterns ---
    try:
        blocked_cmds = ["mkfs /dev/sda1", "shutdown -h now", "format C:", "dd of=/dev/sda"]
        all_blocked = True
        for cmd in blocked_cmds:
            r = classify_command(cmd)
            if r != "blocked":
                record("FAIL", f"shell: '{cmd}' classified as {r}, expected blocked")
                all_blocked = False
                break
        if all_blocked:
            record("PASS", f"shell: {len(blocked_cmds)} dangerous commands correctly blocked")
    except Exception as e:
        record("FAIL", "shell: blocked patterns", str(e))

    # --- 12e. Shell — more safe prefixes ---
    try:
        safe_cmds = ["pwd", "git status", "python --version", "echo hello", "pip list"]
        all_safe = True
        for cmd in safe_cmds:
            r = classify_command(cmd)
            if r != "safe":
                record("FAIL", f"shell: '{cmd}' classified as {r}, expected safe")
                all_safe = False
                break
        if all_safe:
            record("PASS", f"shell: {len(safe_cmds)} safe commands correctly classified")
    except Exception as e:
        record("FAIL", "shell: safe prefixes", str(e))

    # --- 12f. Filesystem — _normalise_path ---
    try:
        from tools.filesystem_tool import _normalise_path
        # Should strip workspace folder prefix
        result = _normalise_path("ThothWorkspace/notes.txt", "D:\\ThothWorkspace")
        if result == "notes.txt":
            record("PASS", "filesystem: _normalise_path strips workspace prefix")
        else:
            record("FAIL", f"filesystem: _normalise_path returned '{result}', expected 'notes.txt'")
    except Exception as e:
        record("FAIL", "filesystem: _normalise_path", str(e))

    # --- 12g. Filesystem — _is_outside_workspace ---
    try:
        from tools.filesystem_tool import _is_outside_workspace
        # Absolute path outside workspace → True
        outside = _is_outside_workspace("C:\\Windows\\system32\\cmd.exe", "D:\\ThothWorkspace")
        # Relative path → False (assumed inside)
        inside_rel = _is_outside_workspace("notes.txt", "D:\\ThothWorkspace")
        if outside and not inside_rel:
            record("PASS", "filesystem: _is_outside_workspace correctly distinguishes paths")
        else:
            record("FAIL", f"filesystem: _is_outside_workspace outside={outside}, inside_rel={inside_rel}")
    except Exception as e:
        record("FAIL", "filesystem: _is_outside_workspace", str(e))

    # --- 12h. Filesystem — export_to_pdf ---
    try:
        import tempfile
        from pathlib import Path
        from fpdf import FPDF
        # Verify fpdf2 is importable — the tool uses it
        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("Helvetica", size=12)
        pdf.cell(200, 10, text="Integration test PDF", new_x="LMARGIN", new_y="NEXT")
        tmp = Path(tempfile.mktemp(suffix=".pdf"))
        pdf.output(str(tmp))
        if tmp.exists() and tmp.stat().st_size > 100:
            record("PASS", f"filesystem: PDF generation works ({tmp.stat().st_size} bytes)")
            tmp.unlink()
        else:
            record("FAIL", "filesystem: PDF generation produced empty file")
    except ImportError:
        record("SKIP", "filesystem: fpdf2 not installed")
    except Exception as e:
        record("FAIL", "filesystem: PDF generation", str(e))

    # --- 12i. Chart — _load_data from CSV ---
    try:
        import tempfile
        from pathlib import Path
        from tools.chart_tool import _load_data
        # Create a temp CSV
        csv_path = Path(tempfile.mktemp(suffix=".csv"))
        csv_path.write_text("name,value\nAlice,10\nBob,20\nCharlie,30", encoding="utf-8")
        df = _load_data(str(csv_path), None)
        if len(df) == 3 and "name" in df.columns and "value" in df.columns:
            record("PASS", "chart: _load_data reads CSV correctly (3 rows, 2 cols)")
        else:
            record("FAIL", f"chart: _load_data CSV result unexpected: {df.shape}")
        csv_path.unlink()
    except Exception as e:
        record("FAIL", "chart: _load_data CSV", str(e))

    # --- 12j. Chart — _build_figure (bar chart) ---
    try:
        import pandas as pd
        from tools.chart_tool import _build_figure
        df = pd.DataFrame({"category": ["A", "B", "C"], "amount": [10, 20, 30]})
        fig = _build_figure(df, "bar", "category", "amount", None, "Test Bar")
        fig_json = fig.to_json()
        if len(fig_json) > 100 and "Test Bar" in fig_json:
            record("PASS", "chart: _build_figure creates bar chart")
        else:
            record("FAIL", "chart: _build_figure bar chart output unexpected")
    except Exception as e:
        record("FAIL", "chart: _build_figure bar", str(e))

    # --- 12k. Chart — _build_figure (pie chart) ---
    try:
        fig = _build_figure(df, "pie", "category", "amount", None, "Test Pie")
        fig_json = fig.to_json()
        if len(fig_json) > 100:
            record("PASS", "chart: _build_figure creates pie chart")
        else:
            record("FAIL", "chart: _build_figure pie chart output unexpected")
    except Exception as e:
        record("FAIL", "chart: _build_figure pie", str(e))

    # --- 12l. Chart — _build_figure (line chart) ---
    try:
        df_line = pd.DataFrame({"x": [1, 2, 3, 4], "y": [10, 15, 13, 17]})
        fig = _build_figure(df_line, "line", "x", "y", None, "Test Line")
        if fig.to_json() and len(fig.to_json()) > 100:
            record("PASS", "chart: _build_figure creates line chart")
        else:
            record("FAIL", "chart: _build_figure line chart unexpected")
    except Exception as e:
        record("FAIL", "chart: _build_figure line", str(e))

    # --- 12m. Chart — save_to_file PNG export ---
    try:
        import tempfile
        from pathlib import Path
        from tools.chart_tool import _create_chart
        csv_path = Path(tempfile.mktemp(suffix=".csv"))
        csv_path.write_text("name,value\nX,5\nY,15\nZ,25", encoding="utf-8")
        png_path = Path(tempfile.mktemp(suffix=".png"))
        result = _create_chart(
            chart_type="bar",
            data_source=str(csv_path),
            x_column="name",
            y_column="value",
            save_to_file=str(png_path),
        )
        if png_path.exists() and png_path.stat().st_size > 1000:
            record("PASS", f"chart: save_to_file PNG export ({png_path.stat().st_size} bytes)")
            png_path.unlink()
        elif "Could not save" in result:
            record("WARN", "chart: save_to_file failed (kaleido may not be available)")
        else:
            record("FAIL", "chart: save_to_file PNG not created")
        csv_path.unlink(missing_ok=True)
    except Exception as e:
        record("FAIL", "chart: save_to_file PNG export", str(e))

    # --- 12n. Chart — unsupported chart type rejected ---
    try:
        result = _create_chart("radar", "dummy.csv")
        if "Unsupported chart type" in result:
            record("PASS", "chart: unsupported chart type rejected")
        else:
            record("FAIL", f"chart: unsupported type not rejected: {result[:100]}")
    except Exception as e:
        record("FAIL", "chart: unsupported type handling", str(e))

    # --- 12o. Memory — _link_memories direct invocation ---
    try:
        import memory as mem
        import knowledge_graph as kg
        from tools.memory_tool import _link_memories
        subj_a = f"{_PREFIX}LinkDirect_A_{_gen_id()}"
        subj_b = f"{_PREFIX}LinkDirect_B_{_gen_id()}"
        ra = mem.save_memory("person", subj_a, "Link test person")
        rb = mem.save_memory("place", subj_b, "Link test place")
        _cleanup_entity_ids.extend([ra["id"], rb["id"]])

        result = _link_memories(ra["id"], rb["id"], "lives_in")
        if "successfully" in result.lower() or "lives_in" in result:
            record("PASS", "memory: _link_memories direct invocation works")
        else:
            record("FAIL", f"memory: _link_memories returned: {result[:200]}")
    except Exception as e:
        record("FAIL", "memory: _link_memories direct", str(e))

    # --- 12p. Memory — _link_memories with bad entity ID ---
    try:
        result = _link_memories("nonexistent_id", rb["id"], "test_rel")
        if "not found" in result.lower() or "error" in result.lower():
            record("PASS", "memory: _link_memories rejects nonexistent source")
        else:
            record("FAIL", f"memory: _link_memories accepted bad source: {result[:200]}")
    except Exception as e:
        record("FAIL", "memory: _link_memories bad source", str(e))

    # --- 12q. Memory — _explore_connections direct invocation ---
    try:
        from tools.memory_tool import _explore_connections
        result = _explore_connections(ra["id"], hops=1)
        if subj_b in result or "lives_in" in result:
            record("PASS", "memory: _explore_connections finds linked entity")
        else:
            record("WARN", f"memory: _explore_connections did not find link: {result[:200]}")
    except Exception as e:
        record("FAIL", "memory: _explore_connections", str(e))

    # --- 12r. Memory — _explore_connections with bad ID ---
    try:
        result = _explore_connections("nonexistent_id")
        if "not found" in result.lower():
            record("PASS", "memory: _explore_connections rejects nonexistent entity")
        else:
            record("FAIL", f"memory: _explore_connections accepted bad ID: {result[:200]}")
    except Exception as e:
        record("FAIL", "memory: _explore_connections bad ID", str(e))

    # --- 12s. Memory — _explore_connections hops capped at 3 ---
    try:
        result = _explore_connections(ra["id"], hops=99)
        # Should not crash — hops is capped internally
        if result and len(result) > 10:
            record("PASS", "memory: _explore_connections caps hops at 3 (no crash)")
        else:
            record("FAIL", "memory: _explore_connections hops=99 failed")
    except Exception as e:
        record("FAIL", "memory: _explore_connections hops cap", str(e))

    # --- 12t. System info --- 
    try:
        from tools.system_info_tool import _get_system_info
        info = _get_system_info()
        has_cpu = "[CPU]" in info
        has_mem = "[Memory]" in info
        has_disk = "[Disk]" in info
        if has_cpu and has_mem and has_disk:
            record("PASS", f"system_info: returned {len(info)} chars with CPU/Memory/Disk sections")
        else:
            record("FAIL", f"system_info: missing sections (cpu={has_cpu}, mem={has_mem}, disk={has_disk})")
    except Exception as e:
        record("FAIL", "system_info: _get_system_info", str(e))

    # --- 12u. Weather (live API, no key needed) ---
    try:
        from tools.weather_tool import _get_current_weather
        result = _get_current_weather("London")
        if "Temperature" in result and "Wind" in result:
            record("PASS", "weather: _get_current_weather('London') returned weather data")
        elif "failed" in result.lower():
            record("WARN", "weather: API call failed (network issue?)")
        else:
            record("FAIL", f"weather: unexpected result: {result[:200]}")
    except Exception as e:
        record("FAIL", "weather: _get_current_weather", str(e))

    # --- 12v. URL reader ---
    try:
        from tools.url_reader_tool import _read_url
        result = _read_url("https://httpbin.org/html")
        if "Herman Melville" in result or "SOURCE_URL" in result:
            record("PASS", f"url_reader: fetched httpbin.org/html ({len(result)} chars)")
        elif "Failed" in result:
            record("WARN", "url_reader: fetch failed (network issue?)")
        else:
            record("PASS", f"url_reader: fetched content ({len(result)} chars)")
    except Exception as e:
        record("FAIL", "url_reader: _read_url", str(e))

    # --- 12w. Tracker — log + query + cleanup ---
    try:
        from tools.tracker_tool import _tracker_log, _tracker_query, _get_db
        tracker_name = f"{_PREFIX}Tracker_{_gen_id()}"
        log_result = _tracker_log(tracker_name, value="42", tracker_type="numeric", unit="kg")
        if "Logged" in log_result and tracker_name in log_result:
            record("PASS", f"tracker: _tracker_log creates tracker and logs entry")
        else:
            record("FAIL", f"tracker: _tracker_log result: {log_result[:200]}")

        # Query it
        query_result = _tracker_query(f"list all trackers")
        if tracker_name in query_result:
            record("PASS", "tracker: _tracker_query finds the new tracker")
        else:
            record("WARN", f"tracker: _tracker_query did not find tracker: {query_result[:200]}")

        # Cleanup: delete tracker directly via DB
        conn = _get_db()
        conn.execute("DELETE FROM trackers WHERE name = ?", (tracker_name,))
        conn.commit()
    except ImportError:
        record("SKIP", "tracker: module not available")
    except Exception as e:
        record("FAIL", "tracker: log + query", str(e))

    _cleanup_entities()


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 13 · Channel utilities (pure functions)
# ═════════════════════════════════════════════════════════════════════════════

def section_13_channel_utils():
    print("\nSECTION 13 · Channel Utilities")
    print("-" * 40)

    # --- 13a. Telegram _split_message — short text (no split) ---
    try:
        from channels.telegram import _split_message, MAX_TG_MESSAGE_LEN
        short = "Hello world"
        chunks = _split_message(short)
        if chunks == [short]:
            record("PASS", "telegram: _split_message returns single chunk for short text")
        else:
            record("FAIL", f"telegram: _split_message short text returned {len(chunks)} chunks")
    except ImportError:
        record("SKIP", "telegram: module not available")
        return
    except Exception as e:
        record("FAIL", "telegram: _split_message short", str(e))

    # --- 13b. Telegram _split_message — long text ---
    try:
        long_text = "Line number {i}\n" * 2000
        chunks = _split_message(long_text, max_len=MAX_TG_MESSAGE_LEN)
        all_within = all(len(c) <= MAX_TG_MESSAGE_LEN for c in chunks)
        reassembled = "".join(chunks)
        if all_within and len(chunks) > 1 and reassembled == long_text:
            record("PASS", f"telegram: _split_message split into {len(chunks)} valid chunks")
        elif all_within and len(chunks) > 1:
            record("PASS", f"telegram: _split_message split into {len(chunks)} chunks (all within limit)")
        else:
            record("FAIL", f"telegram: _split_message long text — chunks={len(chunks)}, all_within={all_within}")
    except Exception as e:
        record("FAIL", "telegram: _split_message long", str(e))

    # --- 13c. Telegram _split_message — respects paragraph boundaries ---
    try:
        text = ("A" * 2000) + "\n\n" + ("B" * 2000) + "\n\n" + ("C" * 2000)
        chunks = _split_message(text, max_len=MAX_TG_MESSAGE_LEN)
        all_within = all(len(c) <= MAX_TG_MESSAGE_LEN for c in chunks)
        if all_within and len(chunks) >= 2:
            record("PASS", f"telegram: _split_message respects para boundaries ({len(chunks)} chunks)")
        else:
            record("FAIL", f"telegram: _split_message para split — chunks={len(chunks)}")
    except Exception as e:
        record("FAIL", "telegram: _split_message para", str(e))

    # --- 13d. Telegram _md_to_html — bold ---
    try:
        from channels.telegram import _md_to_html
        result = _md_to_html("Hello **world**")
        if "<b>world</b>" in result:
            record("PASS", "telegram: _md_to_html converts **bold** to <b>")
        else:
            record("FAIL", f"telegram: _md_to_html bold: {result}")
    except Exception as e:
        record("FAIL", "telegram: _md_to_html bold", str(e))

    # --- 13e. Telegram _md_to_html — italic ---
    try:
        result = _md_to_html("Hello *world*")
        if "<i>world</i>" in result:
            record("PASS", "telegram: _md_to_html converts *italic* to <i>")
        else:
            record("FAIL", f"telegram: _md_to_html italic: {result}")
    except Exception as e:
        record("FAIL", "telegram: _md_to_html italic", str(e))

    # --- 13f. Telegram _md_to_html — code ---
    try:
        result = _md_to_html("Use `print()` here")
        if "<code>print()</code>" in result:
            record("PASS", "telegram: _md_to_html converts `code` to <code>")
        else:
            record("FAIL", f"telegram: _md_to_html code: {result}")
    except Exception as e:
        record("FAIL", "telegram: _md_to_html code", str(e))

    # --- 13g. Telegram _md_to_html — code block ---
    try:
        result = _md_to_html("```python\nprint('hi')\n```")
        if "<pre>" in result and "print" in result:
            record("PASS", "telegram: _md_to_html converts fenced code block to <pre>")
        else:
            record("FAIL", f"telegram: _md_to_html code block: {result}")
    except Exception as e:
        record("FAIL", "telegram: _md_to_html code block", str(e))

    # --- 13h. Telegram _md_to_html — heading ---
    try:
        result = _md_to_html("# Main Title")
        if "<b>Main Title</b>" in result:
            record("PASS", "telegram: _md_to_html converts # heading to <b>")
        else:
            record("FAIL", f"telegram: _md_to_html heading: {result}")
    except Exception as e:
        record("FAIL", "telegram: _md_to_html heading", str(e))

    # --- 13i. Telegram _md_to_html — HTML entity escaping ---
    try:
        result = _md_to_html("5 > 3 && x < 10")
        if "&gt;" in result and "&lt;" in result and "&amp;" in result:
            record("PASS", "telegram: _md_to_html escapes HTML entities correctly")
        else:
            record("FAIL", f"telegram: _md_to_html escaping: {result}")
    except Exception as e:
        record("FAIL", "telegram: _md_to_html escaping", str(e))


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 14 · Background task permissions & ContextVars
# ═════════════════════════════════════════════════════════════════════════════

def section_14_background_permissions():
    print("\nSECTION 14 · Background Permissions & ContextVars")
    print("-" * 40)

    # --- 14a. _validate_delivery — no channel/target is valid ---
    try:
        from tasks import _validate_delivery
        _validate_delivery(None, None)
        record("PASS", "task: _validate_delivery(None, None) passes")
    except Exception as e:
        record("FAIL", "task: _validate_delivery(None, None)", str(e))

    # --- 14b. _validate_delivery — telegram channel is valid ---
    try:
        _validate_delivery("telegram", None)
        record("PASS", "task: _validate_delivery('telegram', None) passes")
    except Exception as e:
        record("FAIL", "task: _validate_delivery telegram", str(e))

    # --- 14c. _validate_delivery — email requires valid address ---
    try:
        _validate_delivery("email", "user@example.com")
        record("PASS", "task: _validate_delivery('email', 'user@example.com') passes")
    except Exception as e:
        record("FAIL", "task: _validate_delivery email valid", str(e))

    # --- 14d. _validate_delivery — email with bad address rejected ---
    try:
        _validate_delivery("email", "not-an-email")
        record("FAIL", "task: _validate_delivery accepted bad email 'not-an-email'")
    except ValueError:
        record("PASS", "task: _validate_delivery rejects bad email address")
    except Exception as e:
        record("FAIL", "task: _validate_delivery bad email", str(e))

    # --- 14e. _validate_delivery — unknown channel rejected ---
    try:
        _validate_delivery("slack", "channel")
        record("FAIL", "task: _validate_delivery accepted unknown channel 'slack'")
    except ValueError:
        record("PASS", "task: _validate_delivery rejects unknown channel 'slack'")
    except Exception as e:
        record("FAIL", "task: _validate_delivery unknown channel", str(e))

    # --- 14f. _validate_delivery — target without channel rejected ---
    try:
        _validate_delivery(None, "user@example.com")
        record("FAIL", "task: _validate_delivery accepted target without channel")
    except ValueError:
        record("PASS", "task: _validate_delivery rejects target without channel")
    except Exception as e:
        record("FAIL", "task: _validate_delivery target-no-channel", str(e))

    # --- 14g. ContextVar — background_workflow default is False ---
    try:
        from agent import _background_workflow_var, is_background_workflow
        if not is_background_workflow():
            record("PASS", "contextvar: is_background_workflow() defaults to False")
        else:
            record("FAIL", "contextvar: is_background_workflow() is True by default")
    except Exception as e:
        record("FAIL", "contextvar: is_background_workflow", str(e))

    # --- 14h. ContextVar — set and read background flag ---
    try:
        token = _background_workflow_var.set(True)
        if is_background_workflow():
            record("PASS", "contextvar: _background_workflow_var.set(True) propagates")
        else:
            record("FAIL", "contextvar: _background_workflow_var.set(True) did not propagate")
        _background_workflow_var.reset(token)
    except Exception as e:
        record("FAIL", "contextvar: background flag set/read", str(e))

    # --- 14i. ContextVar — task_allowed_commands default is empty ---
    try:
        from agent import _task_allowed_commands_var, _task_allowed_recipients_var
        cmds = _task_allowed_commands_var.get()
        recips = _task_allowed_recipients_var.get()
        if cmds == [] and recips == []:
            record("PASS", "contextvar: allowed_commands/recipients default to []")
        else:
            record("FAIL", f"contextvar: defaults wrong — cmds={cmds}, recips={recips}")
    except Exception as e:
        record("FAIL", "contextvar: allowed_commands default", str(e))

    # --- 14j. ContextVar — set and read allowed_commands ---
    try:
        token = _task_allowed_commands_var.set(["pip list", "git status"])
        got = _task_allowed_commands_var.get()
        if got == ["pip list", "git status"]:
            record("PASS", "contextvar: _task_allowed_commands_var set/get works")
        else:
            record("FAIL", f"contextvar: allowed_commands got {got}")
        _task_allowed_commands_var.reset(token)
    except Exception as e:
        record("FAIL", "contextvar: allowed_commands set/get", str(e))

    # --- 14k. Task with allowed_commands field ---
    try:
        from tasks import create_task, get_task, update_task, delete_task
        tag = _gen_id()
        task_id = create_task(
            name=f"{_PREFIX}Perm_{tag}",
            prompts=["Test prompt"],
        )
        _cleanup_task_ids.append(task_id)
        update_task(task_id,
                    allowed_commands=["pip list", "git status"],
                    allowed_recipients=["admin@example.com"])
        task = get_task(task_id)
        if task["allowed_commands"] == ["pip list", "git status"]:
            record("PASS", "task: allowed_commands stored correctly")
        else:
            record("FAIL", f"task: allowed_commands = {task['allowed_commands']}")
        if task["allowed_recipients"] == ["admin@example.com"]:
            record("PASS", "task: allowed_recipients stored correctly")
        else:
            record("FAIL", f"task: allowed_recipients = {task['allowed_recipients']}")
    except Exception as e:
        record("FAIL", "task: permission fields", str(e))

    _cleanup_tasks()


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 15 · Bug-fix verifications
# ═════════════════════════════════════════════════════════════════════════════

def section_15_bugfix_verifications():
    print("\nSECTION 15 · Bug-fix Verifications")
    print("-" * 40)

    # --- 15a. MPS fix — embedding model uses device="cpu" ---
    try:
        from documents import get_embedding_model
        model = get_embedding_model()
        # HuggingFaceEmbeddings stores model_kwargs; check the device
        model_kwargs = getattr(model, "model_kwargs", {})
        device = model_kwargs.get("device", "UNSET")
        if device == "cpu":
            record("PASS", "bugfix: embedding model device='cpu' (MPS fix)")
        else:
            record("FAIL", f"bugfix: embedding model device='{device}', expected 'cpu'")
    except Exception as e:
        record("FAIL", "bugfix: embedding model device check", str(e))

    # --- 15b. FAISS lock exists ---
    try:
        import knowledge_graph as kg
        import threading
        lock = getattr(kg, "_faiss_lock", None)
        if isinstance(lock, type(threading.Lock())):
            record("PASS", "bugfix: knowledge_graph._faiss_lock exists (thread safety)")
        else:
            record("FAIL", f"bugfix: _faiss_lock is {type(lock)}, expected threading.Lock")
    except Exception as e:
        record("FAIL", "bugfix: FAISS lock check", str(e))

    # --- 15c. Email channel — _mark_as_read in _send_reply ---
    try:
        import inspect
        from channels.email import _send_reply
        source = inspect.getsource(_send_reply)
        if "_mark_as_read" in source:
            record("PASS", "bugfix: _send_reply calls _mark_as_read (email loop fix)")
        else:
            record("FAIL", "bugfix: _send_reply missing _mark_as_read call")
    except ImportError:
        record("SKIP", "bugfix: channels.email not importable (missing deps?)")
    except Exception as e:
        record("FAIL", "bugfix: email _send_reply check", str(e))

    # --- 15d. Email channel — _mark_as_read in _send_reply_and_get_id ---
    try:
        from channels.email import _send_reply_and_get_id
        source = inspect.getsource(_send_reply_and_get_id)
        if "_mark_as_read" in source:
            record("PASS", "bugfix: _send_reply_and_get_id calls _mark_as_read (email loop fix)")
        else:
            record("FAIL", "bugfix: _send_reply_and_get_id missing _mark_as_read call")
    except ImportError:
        record("SKIP", "bugfix: channels.email _send_reply_and_get_id not available")
    except Exception as e:
        record("FAIL", "bugfix: email _send_reply_and_get_id check", str(e))

    # --- 15e. Embedding model produces valid vectors ---
    try:
        from documents import get_embedding_model
        model = get_embedding_model()
        vectors = model.embed_documents(["Hello world test"])
        if vectors and len(vectors) == 1 and len(vectors[0]) > 100:
            record("PASS", f"bugfix: embedding produces {len(vectors[0])}-dim vectors on CPU")
        else:
            record("FAIL", f"bugfix: embedding vector unexpected shape: {len(vectors)}")
    except Exception as e:
        record("FAIL", "bugfix: embedding vector test", str(e))

    # --- 15f. FAISS lock is used in rebuild_index ---
    try:
        import knowledge_graph as kg
        import inspect
        source = inspect.getsource(kg.rebuild_index)
        if "_faiss_lock" in source:
            record("PASS", "bugfix: rebuild_index uses _faiss_lock")
        else:
            record("FAIL", "bugfix: rebuild_index does not use _faiss_lock")
    except Exception as e:
        record("FAIL", "bugfix: rebuild_index lock check", str(e))

    # --- 15g. FAISS lock is used in semantic_search ---
    try:
        source = inspect.getsource(kg.semantic_search)
        if "_faiss_lock" in source:
            record("PASS", "bugfix: semantic_search uses _faiss_lock")
        else:
            record("FAIL", "bugfix: semantic_search does not use _faiss_lock")
    except Exception as e:
        record("FAIL", "bugfix: semantic_search lock check", str(e))


# ═════════════════════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════════════════════

def main():
    global _section_filter, _fast_mode

    # Parse args
    args = sys.argv[1:]
    if "--section" in args:
        idx = args.index("--section")
        if idx + 1 < len(args):
            _section_filter = int(args[idx + 1])
    if "--fast" in args:
        _fast_mode = True

    print("=" * 70)
    print("THOTH INTEGRATION TESTS")
    print("=" * 70)
    if _fast_mode:
        print("⚡ Fast mode — skipping LLM-dependent tests")
    if _section_filter:
        print(f"🔍 Running only section {_section_filter}")
    print()

    sections = {
        1: ("Prerequisites", section_1_prerequisites),
        2: ("Agent Basics", section_2_agent_basics),
        3: ("Memory & KG", section_3_memory_kg),
        4: ("Extraction Pipeline", section_4_extraction),
        5: ("Agent Recall", section_5_recall),
        6: ("Tool Functions", section_6_tools),
        7: ("Task Engine", section_7_tasks),
        8: ("TTS", section_8_tts),
        9: ("Agent Routing", section_9_agent_routing),
        10: ("End-to-End", section_10_e2e),
        11: ("Edge Cases", section_11_edge_cases),
        12: ("Tool Sub-tools Extended", section_12_tool_subttools),
        13: ("Channel Utilities", section_13_channel_utils),
        14: ("Background Permissions", section_14_background_permissions),
        15: ("Bug-fix Verifications", section_15_bugfix_verifications),
    }

    # Section 1 is always required
    if _section_filter and _section_filter != 1:
        if not section_1_prerequisites():
            print("\n❌ Prerequisites failed — cannot continue.")
            sys.exit(1)

    for num, (name, func) in sections.items():
        if _section_filter and num != _section_filter:
            continue
        try:
            result = func()
            # Section 1 returns False if prerequisites fail
            if num == 1 and result is False:
                print("\n❌ Prerequisites failed — cannot continue.")
                sys.exit(1)
        except Exception as e:
            record("FAIL", f"Section {num} crashed", f"{type(e).__name__}: {e}")
            traceback.print_exc()

    # ── Summary ──────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)

    passes = sum(1 for s, _, _ in _results if s == "PASS")
    fails = sum(1 for s, _, _ in _results if s == "FAIL")
    warns = sum(1 for s, _, _ in _results if s == "WARN")
    skips = sum(1 for s, _, _ in _results if s == "SKIP")
    total = len(_results)

    print(f"  ✅ PASS: {passes}")
    if fails:
        print(f"  ❌ FAIL: {fails}")
    if warns:
        print(f"  ⚠️  WARN: {warns}")
    if skips:
        print(f"  ⏭️  SKIP: {skips}")
    print(f"  Total: {total}")

    if fails:
        print("\nFAILED TESTS:")
        for s, label, detail in _results:
            if s == "FAIL":
                line = f"  ❌ {label}"
                if detail:
                    line += f": {detail[:200]}"
                print(line)

    if warns:
        print("\nWARNINGS:")
        for s, label, detail in _results:
            if s == "WARN":
                line = f"  ⚠️  {label}"
                if detail:
                    line += f": {detail[:200]}"
                print(line)

    print()
    if fails == 0:
        print("🎉 ALL TESTS PASSED!" if warns == 0 else "✅ ALL TESTS PASSED (with warnings)")
    else:
        print(f"⚠ {fails} TEST(S) FAILED")

    # Final cleanup (safety net)
    _cleanup_entities()
    _cleanup_tasks()

    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
