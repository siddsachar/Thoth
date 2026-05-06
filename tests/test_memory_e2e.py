"""End-to-end memory system integration tests.

Tests the full memory pipeline with real LLM calls against the live database:
  1. Entity CRUD via memory.py wrapper
  2. Knowledge graph operations (relations, search, graph traversal)
  3. LLM-based memory extraction (quality + accuracy)
  4. Deduplication pipeline (_dedup_and_save)
  5. Dream cycle phases (merge, enrich, infer)
  6. FAISS semantic search accuracy
  7. Graph integrity (MultiDiGraph, locking, island repair)
  8. Document extraction pipeline (if applicable)

All test entities use the ``__MTEST_`` prefix and are cleaned up after each
section.  The script operates on the live database and uses real LLM calls.
Thoth should NOT be running while these tests execute.

Usage:
    python test_memory_e2e.py                # run all sections
    python test_memory_e2e.py --section 3    # run only section 3
    python test_memory_e2e.py --fast         # skip LLM-dependent sections
    python test_memory_e2e.py --keep         # don't clean up test entities
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
import traceback
import uuid
from pathlib import Path

os.environ.setdefault("PYTHONIOENCODING", "utf-8")

PROJECT_ROOT = Path(__file__).resolve().parent
os.chdir(PROJECT_ROOT)
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ── CLI args ─────────────────────────────────────────────────────────────────
_section_filter: int | None = None
_fast_mode = False
_keep_data = False

for i, arg in enumerate(sys.argv[1:], 1):
    if arg == "--fast":
        _fast_mode = True
    elif arg == "--keep":
        _keep_data = True
    elif arg == "--section" and i < len(sys.argv) - 1:
        _section_filter = int(sys.argv[i + 1])

# ── Test infrastructure ──────────────────────────────────────────────────────
PREFIX = "__MTEST_"
_results: list[tuple[str, str, str]] = []
_cleanup_ids: list[str] = []
_pass = 0
_fail = 0
_warn = 0
_skip = 0


def record(status: str, label: str, detail: str = ""):
    global _pass, _fail, _warn, _skip
    if status == "PASS":
        _pass += 1
    elif status == "FAIL":
        _fail += 1
    elif status == "WARN":
        _warn += 1
    elif status == "SKIP":
        _skip += 1
    _results.append((status, label, detail))
    icon = {"PASS": "  ✅", "FAIL": "  ❌", "WARN": "  ⚠️ ", "SKIP": "  ⏭️ "}.get(status, "  ?")
    line = f"{icon} {label}"
    if detail:
        line += f"  —  {detail}"
    print(line)


def _tag() -> str:
    return uuid.uuid4().hex[:6]


def _cleanup():
    """Remove all test entities created during the run."""
    if _keep_data or not _cleanup_ids:
        return
    import knowledge_graph as kg
    try:
        conn = sqlite3.connect(kg.DB_PATH)
        for eid in _cleanup_ids:
            conn.execute("DELETE FROM relations WHERE source_id = ? OR target_id = ?", (eid, eid))
            conn.execute("DELETE FROM entities WHERE id = ?", (eid,))
        conn.commit()
        conn.close()
    except Exception:
        for eid in _cleanup_ids:
            try:
                kg.delete_entity(eid)
            except Exception:
                pass
    _cleanup_ids.clear()


def _should_run(section: int) -> bool:
    return _section_filter is None or _section_filter == section


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 1 · Prerequisites & Connectivity
# ═════════════════════════════════════════════════════════════════════════════

def section_1():
    print("\n" + "=" * 70)
    print("SECTION 1 · Prerequisites & Connectivity")
    print("=" * 70)

    # 1a. Core imports
    try:
        import knowledge_graph as kg
        import memory as mem
        record("PASS", "1a: core imports (knowledge_graph, memory)")
    except Exception as e:
        record("FAIL", "1a: core imports", str(e))
        return False

    # 1b. Database exists and has tables
    try:
        conn = sqlite3.connect(kg.DB_PATH)
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        conn.close()
        has_entities = "entities" in tables
        has_relations = "relations" in tables
        if has_entities and has_relations:
            record("PASS", f"1b: DB tables present at {kg.DB_PATH}")
        else:
            record("FAIL", "1b: missing tables", str(tables))
            return False
    except Exception as e:
        record("FAIL", "1b: DB connectivity", str(e))
        return False

    # 1c. LLM reachable (skip in fast mode)
    if _fast_mode:
        record("SKIP", "1c: LLM check (fast mode)")
    else:
        try:
            from models import get_current_model, get_llm_for
            from langchain_core.messages import HumanMessage
            model = get_current_model()
            llm = get_llm_for(model)
            resp = llm.invoke([HumanMessage(content="Reply with just the word: OK")])
            text = resp.content if isinstance(resp.content, str) else str(resp.content)
            if "ok" in text.lower():
                record("PASS", f"1c: LLM reachable ({model})")
            else:
                record("WARN", f"1c: LLM replied but unexpected", text[:80])
        except Exception as e:
            record("FAIL", "1c: LLM unreachable", str(e))
            return False

    # 1d. FAISS available
    try:
        import faiss  # noqa: F401
        record("PASS", "1d: FAISS importable")
    except ImportError:
        record("WARN", "1d: FAISS not installed — semantic search tests will fail")

    # 1e. Embedding model loads
    try:
        from documents import get_embedding_model
        emb = get_embedding_model()
        vec = emb.embed_query("test")
        if len(vec) > 0:
            record("PASS", f"1e: embedding model loaded (dim={len(vec)})")
        else:
            record("FAIL", "1e: embedding returned empty vector")
    except Exception as e:
        record("FAIL", "1e: embedding model", str(e))

    # 1f. Entity count baseline
    try:
        count = kg.count_entities()
        rel_count = kg.count_relations()
        record("PASS", f"1f: baseline — {count} entities, {rel_count} relations")
    except Exception as e:
        record("FAIL", "1f: entity count", str(e))

    return True


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 2 · Entity CRUD (full lifecycle)
# ═════════════════════════════════════════════════════════════════════════════

def section_2():
    print("\n" + "=" * 70)
    print("SECTION 2 · Entity CRUD Lifecycle")
    print("=" * 70)

    import knowledge_graph as kg
    import memory as mem

    t = _tag()

    # 2a. Create entity via memory.py wrapper
    try:
        e = mem.save_memory("person", f"{PREFIX}Alice_{t}", f"Alice is a software engineer", tags="test")
        _cleanup_ids.append(e["id"])
        assert e["entity_type"] == "person"
        assert e["subject"] == f"{PREFIX}Alice_{t}"
        assert "software engineer" in e["description"]
        record("PASS", "2a: save_memory creates entity with correct fields")
    except Exception as e:
        record("FAIL", "2a: save_memory", str(e))
        return

    alice_id = _cleanup_ids[-1]

    # 2b. get_entity returns the same entity
    try:
        fetched = kg.get_entity(alice_id)
        assert fetched is not None
        assert fetched["id"] == alice_id
        assert fetched["subject"] == f"{PREFIX}Alice_{t}"
        record("PASS", "2b: get_entity returns correct entity")
    except Exception as e:
        record("FAIL", "2b: get_entity", str(e))

    # 2c. find_by_subject (deterministic lookup)
    try:
        found = mem.find_by_subject(None, f"{PREFIX}Alice_{t}")
        assert found is not None
        assert found["id"] == alice_id
        record("PASS", "2c: find_by_subject finds entity")
    except Exception as e:
        record("FAIL", "2c: find_by_subject", str(e))

    # 2d. find_by_subject is case-insensitive
    try:
        found = mem.find_by_subject(None, f"{PREFIX}alice_{t}")
        assert found is not None and found["id"] == alice_id
        record("PASS", "2d: find_by_subject is case-insensitive")
    except Exception as e:
        record("FAIL", "2d: find_by_subject case insensitive", str(e))

    # 2e. Update entity
    try:
        updated = mem.update_memory(
            alice_id,
            "Alice is a senior software engineer at Acme Corp",
            aliases="Ally, Al",
        )
        assert updated is not None
        assert "senior" in updated["description"]
        assert "Ally" in updated.get("aliases", "")
        record("PASS", "2e: update_memory updates description + aliases")
    except Exception as e:
        record("FAIL", "2e: update_memory", str(e))

    # 2f. find_by_subject resolves aliases
    try:
        found = mem.find_by_subject(None, "Ally")
        assert found is not None and found["id"] == alice_id
        record("PASS", "2f: find_by_subject resolves alias 'Ally'")
    except Exception as e:
        record("FAIL", "2f: alias resolution", str(e))

    # 2g. list_memories with category filter
    try:
        persons = mem.list_memories(category="person", limit=500)
        test_persons = [p for p in persons if PREFIX in p.get("subject", "")]
        assert len(test_persons) >= 1
        record("PASS", f"2g: list_memories(person) returns {len(test_persons)} test entities")
    except Exception as e:
        record("FAIL", "2g: list_memories", str(e))

    # 2h. search_memories (text search)
    try:
        results = mem.search_memories(f"{PREFIX}Alice_{t}")
        found_ids = [r["id"] for r in results]
        assert alice_id in found_ids
        record("PASS", "2h: search_memories finds entity by subject text")
    except Exception as e:
        record("FAIL", "2h: search_memories", str(e))

    # 2i. Create second entity
    try:
        e2 = mem.save_memory("place", f"{PREFIX}Berlin_{t}", "Capital of Germany", tags="test")
        _cleanup_ids.append(e2["id"])
        record("PASS", "2i: second entity created")
    except Exception as e:
        record("FAIL", "2i: second entity", str(e))
        return

    berlin_id = e2["id"]

    # 2j. Add relation between entities
    try:
        rel = kg.add_relation(alice_id, berlin_id, "lives_in", source="test", confidence=0.9)
        assert rel is not None
        assert rel["relation_type"] == "lives_in"
        assert rel["source_id"] == alice_id
        assert rel["target_id"] == berlin_id
        record("PASS", "2j: add_relation creates link")
    except Exception as e:
        record("FAIL", "2j: add_relation", str(e))

    # 2k. get_relations returns the link
    try:
        rels = kg.get_relations(alice_id, direction="both")
        lives_in = [r for r in rels if r.get("relation_type") == "lives_in"]
        assert len(lives_in) >= 1
        assert lives_in[0]["peer_id"] == berlin_id
        record("PASS", "2k: get_relations finds lives_in link")
    except Exception as e:
        record("FAIL", "2k: get_relations", str(e))

    # 2l. Parallel relations (MultiDiGraph bug fix)
    try:
        rel2 = kg.add_relation(alice_id, berlin_id, "born_in", source="test", confidence=0.7)
        assert rel2 is not None
        rels = kg.get_relations(alice_id, direction="both")
        rel_types = {r["relation_type"] for r in rels if r["peer_id"] == berlin_id}
        assert "lives_in" in rel_types and "born_in" in rel_types, \
            f"Expected both lives_in and born_in, got {rel_types}"
        record("PASS", "2l: parallel relations (lives_in + born_in) both stored")
    except Exception as e:
        record("FAIL", "2l: parallel relations", str(e))

    # 2m. Delete relation
    try:
        rel_id = rel2["id"]
        deleted = kg.delete_relation(rel_id)
        assert deleted is True
        rels = kg.get_relations(alice_id, direction="both")
        born_in = [r for r in rels if r.get("relation_type") == "born_in" and r["peer_id"] == berlin_id]
        assert len(born_in) == 0
        record("PASS", "2m: delete_relation removes link")
    except Exception as e:
        record("FAIL", "2m: delete_relation", str(e))

    # 2n. Delete entity cascades relations
    try:
        count_before = len(kg.get_relations(berlin_id, direction="both"))
        deleted = mem.delete_memory(alice_id)
        assert deleted is True
        rels_after = kg.get_relations(berlin_id, direction="both")
        test_rels = [r for r in rels_after if r["peer_id"] == alice_id]
        assert len(test_rels) == 0
        _cleanup_ids.remove(alice_id)
        record("PASS", "2n: delete_memory cascades — relations removed")
    except Exception as e:
        record("FAIL", "2n: delete cascade", str(e))

    # 2o. User entity deduplication
    try:
        user1 = kg.find_by_subject(None, "User")
        if user1:
            # Attempting to save another person named "User" should redirect to update
            e_user = kg.save_entity("person", "User", "Extra info about user", source="test")
            assert e_user["id"] == user1["id"], "User save should redirect to update"
            record("PASS", "2o: User entity deduplication works")
        else:
            record("SKIP", "2o: no existing User entity to test deduplication")
    except Exception as e:
        record("FAIL", "2o: User deduplication", str(e))

    # 2p. Invalid entity_type rejected
    try:
        mem.save_memory("invalid_type", f"{PREFIX}Bad_{t}", "Should fail")
        record("FAIL", "2p: invalid entity_type was accepted")
    except ValueError:
        record("PASS", "2p: invalid entity_type raises ValueError")
    except Exception as e:
        record("FAIL", "2p: unexpected error for invalid type", str(e))

    _cleanup()


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 3 · Semantic Search & FAISS
# ═════════════════════════════════════════════════════════════════════════════

def section_3():
    print("\n" + "=" * 70)
    print("SECTION 3 · Semantic Search & FAISS")
    print("=" * 70)

    import knowledge_graph as kg
    import memory as mem

    t = _tag()

    # Create test entities with semantically distinct descriptions
    entities = {}
    test_data = [
        ("person", f"{PREFIX}Chef_{t}", "Professional chef specializing in Italian cuisine and pasta making"),
        ("skill", f"{PREFIX}Python_{t}", "Python programming language used for data science and web development"),
        ("place", f"{PREFIX}Tokyo_{t}", "Capital city of Japan known for technology and sushi restaurants"),
        ("project", f"{PREFIX}Rover_{t}", "Mars rover project for NASA exploring the red planet surface"),
    ]

    for etype, subj, desc in test_data:
        try:
            e = mem.save_memory(etype, subj, desc, tags="test")
            _cleanup_ids.append(e["id"])
            entities[subj] = e
        except Exception as exc:
            record("FAIL", f"3-setup: creating {subj}", str(exc))
            _cleanup()
            return

    # Rebuild FAISS index to include new entities
    try:
        kg.rebuild_index()
        record("PASS", "3-setup: FAISS index rebuilt")
    except Exception as e:
        record("FAIL", "3-setup: FAISS rebuild", str(e))
        _cleanup()
        return

    # 3a. Semantic search finds relevant entity
    try:
        results = kg.semantic_search("Italian food and cooking", top_k=5, threshold=0.3)
        found_subjects = [r.get("subject", "") for r in results]
        chef_found = any(f"{PREFIX}Chef_{t}" in s for s in found_subjects)
        if chef_found:
            record("PASS", "3a: semantic search 'Italian food' finds Chef")
        else:
            record("FAIL", "3a: semantic search did not find Chef", str(found_subjects[:3]))
    except Exception as e:
        record("FAIL", "3a: semantic search", str(e))

    # 3b. Semantic search ranks correctly
    try:
        results = kg.semantic_search("programming code software", top_k=10, threshold=0.2)
        found_subjects = [r.get("subject", "") for r in results]
        python_idx = next((i for i, s in enumerate(found_subjects) if f"{PREFIX}Python_{t}" in s), None)
        chef_idx = next((i for i, s in enumerate(found_subjects) if f"{PREFIX}Chef_{t}" in s), None)
        if python_idx is not None:
            if chef_idx is None or python_idx < chef_idx:
                record("PASS", f"3b: 'programming' ranks Python (#{python_idx}) above Chef")
            else:
                record("WARN", f"3b: Python ranked #{python_idx}, Chef #{chef_idx}")
        else:
            record("FAIL", "3b: Python not found in search results")
    except Exception as e:
        record("FAIL", "3b: semantic ranking", str(e))

    # 3c. find_duplicate detects near-duplicate
    try:
        dup = kg.find_duplicate("person", f"{PREFIX}Chef_{t}", "A professional chef who makes Italian pasta")
        if dup and dup["id"] == entities[f"{PREFIX}Chef_{t}"]["id"]:
            record("PASS", "3c: find_duplicate detects semantic match")
        else:
            record("WARN", "3c: find_duplicate did not match", str(dup))
    except Exception as e:
        record("FAIL", "3c: find_duplicate", str(e))

    # 3d. Semantic search with very low threshold catches everything
    try:
        results = kg.semantic_search(f"{PREFIX}Chef_{t}", top_k=50, threshold=0.01)
        test_found = [r for r in results if PREFIX in r.get("subject", "")]
        if len(test_found) >= 3:
            record("PASS", f"3d: low-threshold search returns {len(test_found)} test entities")
        else:
            record("WARN", f"3d: only {len(test_found)} test entities found at threshold=0.01")
    except Exception as e:
        record("FAIL", "3d: low threshold search", str(e))

    # 3e. graph_enhanced_recall with 1-hop expansion
    try:
        # First add a relation so graph expansion has something to traverse
        chef_id = entities[f"{PREFIX}Chef_{t}"]["id"]
        tokyo_id = entities[f"{PREFIX}Tokyo_{t}"]["id"]
        kg.add_relation(chef_id, tokyo_id, "works_in", source="test", confidence=0.9)

        results = kg.graph_enhanced_recall(
            "Italian cooking chef", top_k=5, threshold=0.2, hops=1,
        )
        found_subjects = [r.get("subject", "") for r in results]
        chef_found = any(f"{PREFIX}Chef_{t}" in s for s in found_subjects)
        tokyo_found = any(f"{PREFIX}Tokyo_{t}" in s for s in found_subjects)

        if chef_found:
            record("PASS", "3e: graph_enhanced_recall finds seed entity (Chef)")
        else:
            record("FAIL", "3e: graph_enhanced_recall missed Chef")

        if tokyo_found:
            record("PASS", "3e+: graph_enhanced_recall finds 1-hop neighbor (Tokyo)")
        else:
            record("WARN", "3e+: 1-hop neighbor Tokyo not in recall results")
    except Exception as e:
        record("FAIL", "3e: graph_enhanced_recall", str(e))

    # 3f. After deleting entity, rebuild removes it from FAISS
    try:
        rover_id = entities[f"{PREFIX}Rover_{t}"]["id"]
        kg.delete_entity(rover_id)
        _cleanup_ids.remove(rover_id)
        kg.rebuild_index()

        results = kg.semantic_search(f"{PREFIX}Rover_{t} Mars rover", top_k=10, threshold=0.1)
        rover_found = any(f"{PREFIX}Rover_{t}" in r.get("subject", "") for r in results)
        if not rover_found:
            record("PASS", "3f: deleted entity absent from FAISS after rebuild")
        else:
            record("FAIL", "3f: deleted entity still in FAISS")
    except Exception as e:
        record("FAIL", "3f: FAISS cleanup after delete", str(e))

    _cleanup()


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 4 · Graph Structure & Traversal
# ═════════════════════════════════════════════════════════════════════════════

def section_4():
    print("\n" + "=" * 70)
    print("SECTION 4 · Graph Structure & Traversal")
    print("=" * 70)

    import knowledge_graph as kg

    t = _tag()

    # Build a small graph: A --knows--> B --works_at--> C --located_in--> D
    nodes = {}
    node_data = [
        ("person", f"{PREFIX}Ada_{t}", "Computer scientist"),
        ("person", f"{PREFIX}Bob_{t}", "Software engineer"),
        ("organisation", f"{PREFIX}Acme_{t}", "Tech company"),
        ("place", f"{PREFIX}London_{t}", "City in England"),
    ]

    for etype, subj, desc in node_data:
        try:
            e = kg.save_entity(etype, subj, desc, source="test")
            _cleanup_ids.append(e["id"])
            nodes[subj] = e
        except Exception as exc:
            record("FAIL", f"4-setup: {subj}", str(exc))
            _cleanup()
            return

    ada = nodes[f"{PREFIX}Ada_{t}"]
    bob = nodes[f"{PREFIX}Bob_{t}"]
    acme = nodes[f"{PREFIX}Acme_{t}"]
    london = nodes[f"{PREFIX}London_{t}"]

    try:
        kg.add_relation(ada["id"], bob["id"], "knows", source="test")
        kg.add_relation(bob["id"], acme["id"], "works_at", source="test")
        kg.add_relation(acme["id"], london["id"], "located_in", source="test")
    except Exception as e:
        record("FAIL", "4-setup: relations", str(e))
        _cleanup()
        return

    # 4a. get_neighbors 1-hop
    try:
        neighbors = kg.get_neighbors(ada["id"], hops=1)
        neighbor_ids = {n["id"] for n in neighbors}
        assert bob["id"] in neighbor_ids, "Bob should be 1-hop from Ada"
        assert acme["id"] not in neighbor_ids, "Acme should NOT be 1-hop from Ada"
        record("PASS", "4a: get_neighbors(1-hop) returns direct neighbor only")
    except Exception as e:
        record("FAIL", "4a: get_neighbors 1-hop", str(e))

    # 4b. get_neighbors 2-hop
    try:
        neighbors = kg.get_neighbors(ada["id"], hops=2)
        neighbor_ids = {n["id"] for n in neighbors}
        assert bob["id"] in neighbor_ids
        assert acme["id"] in neighbor_ids, "Acme should be 2-hops from Ada"
        record("PASS", "4b: get_neighbors(2-hop) returns transitive neighbors")
    except Exception as e:
        record("FAIL", "4b: get_neighbors 2-hop", str(e))

    # 4c. get_shortest_path
    try:
        path = kg.get_shortest_path(ada["id"], london["id"])
        if path:
            path_ids = [p["id"] for p in path]
            assert ada["id"] == path_ids[0], "Path should start with Ada"
            assert london["id"] == path_ids[-1], "Path should end with London"
            assert len(path_ids) >= 2, "Path should have at least 2 nodes"
            record("PASS", f"4c: shortest path Ada→London has {len(path_ids)} nodes")
        else:
            record("FAIL", "4c: get_shortest_path returned None")
    except Exception as e:
        record("FAIL", "4c: get_shortest_path", str(e))

    # 4d. get_subgraph
    try:
        sub = kg.get_subgraph(ada["id"], hops=2)
        node_ids = {n["id"] for n in sub["nodes"]}
        assert ada["id"] in node_ids
        assert bob["id"] in node_ids
        assert acme["id"] in node_ids
        assert len(sub["edges"]) >= 2
        record("PASS", f"4d: get_subgraph has {len(sub['nodes'])} nodes, {len(sub['edges'])} edges")
    except Exception as e:
        record("FAIL", "4d: get_subgraph", str(e))

    # 4e. to_mermaid generates valid diagram
    try:
        mermaid = kg.to_mermaid(entity_id=ada["id"], hops=3)
        assert "graph" in mermaid.lower() or "flowchart" in mermaid.lower()
        assert f"{PREFIX}Ada_{t}" in mermaid or ada["id"] in mermaid
        record("PASS", "4e: to_mermaid generates diagram")
    except Exception as e:
        record("FAIL", "4e: to_mermaid", str(e))

    # 4f. graph_to_vis_json includes test nodes
    try:
        vis = kg.graph_to_vis_json(entity_id=ada["id"], hops=3)
        test_labels = [n["label"] for n in vis["nodes"] if PREFIX in n.get("label", "")]
        assert len(test_labels) >= 3
        assert len(vis["edges"]) >= 2
        record("PASS", f"4f: vis_json has {len(test_labels)} test nodes")
    except Exception as e:
        record("FAIL", "4f: graph_to_vis_json", str(e))

    # 4g. get_graph_stats
    try:
        stats = kg.get_graph_stats()
        assert stats["total_entities"] > 0
        assert stats["total_relations"] > 0
        assert "entity_types" in stats
        record("PASS", f"4g: graph_stats — {stats['total_entities']} entities, {stats['total_relations']} relations")
    except Exception as e:
        record("FAIL", "4g: get_graph_stats", str(e))

    # 4h. MultiDiGraph parallel edges in NetworkX
    try:
        g = kg._ensure_graph()
        # Add second relation between same pair
        kg.add_relation(ada["id"], bob["id"], "mentors", source="test")

        g = kg._ensure_graph()
        edge_count = g.number_of_edges(ada["id"], bob["id"])
        assert edge_count >= 2, f"Expected ≥2 edges Ada→Bob, got {edge_count}"
        record("PASS", f"4h: MultiDiGraph stores {edge_count} parallel edges")
    except Exception as e:
        record("FAIL", "4h: MultiDiGraph parallel edges", str(e))

    _cleanup()


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 5 · LLM Memory Extraction (quality assessment)
# ═════════════════════════════════════════════════════════════════════════════

def section_5():
    print("\n" + "=" * 70)
    print("SECTION 5 · LLM Memory Extraction Quality")
    print("=" * 70)

    if _fast_mode:
        record("SKIP", "5: LLM extraction (fast mode)")
        return

    from memory_extraction import _extract_from_conversation

    # ── Test conversation with clear, unambiguous facts ──────────────
    t = _tag()
    conversation = (
        f"User: Hey, I just started a new job at {PREFIX}NovaTech_{t}. "
        f"I'm working as a data scientist there. The office is in {PREFIX}Munich_{t}.\n"
        f"Assistant: Congratulations on the new job! Data science is exciting.\n"
        f"User: Thanks! My manager is {PREFIX}Sarah_{t}. She's great — "
        f"we actually went to the same university, {PREFIX}MIT_{t}.\n"
        f"Assistant: That's a nice coincidence!\n"
        f"User: Yeah. I'm also learning {PREFIX}Kubernetes_{t} for our deployment pipeline. "
        f"And I've been reading '{PREFIX}CleanCode_{t}' by Robert Martin.\n"
        f"Assistant: Great book choice!"
    )

    # ── 5a. Extraction returns non-empty results ─────────────────────
    try:
        extracted = _extract_from_conversation(conversation)
        assert isinstance(extracted, list), "Expected list"
        assert len(extracted) > 0, "Expected non-empty extraction"
        record("PASS", f"5a: extracted {len(extracted)} items from conversation")
    except Exception as e:
        record("FAIL", "5a: extraction call failed", str(e))
        return

    # Separate entities vs relations
    entities = [e for e in extracted if e.get("category")]
    relations = [e for e in extracted if e.get("relation_type")]

    print(f"\n    📊 Extraction breakdown: {len(entities)} entities, {len(relations)} relations")
    for ent in entities:
        print(f"       Entity: [{ent.get('category')}] {ent.get('subject')} — {ent.get('content', '')[:60]}")
    for rel in relations:
        print(f"       Rel: {rel.get('source_subject')} --[{rel.get('relation_type')}]--> {rel.get('target_subject')}")
    print()

    # ── 5b. Entity structure validation ──────────────────────────────
    try:
        for ent in entities:
            assert ent.get("category") in {
                "person", "preference", "fact", "event", "place", "project",
                "organisation", "concept", "skill", "media",
            }, f"Invalid category: {ent.get('category')}"
            assert ent.get("subject"), "Entity missing subject"
            assert ent.get("content"), "Entity missing content"
        record("PASS", "5b: all entities have valid category/subject/content")
    except Exception as e:
        record("FAIL", "5b: entity structure", str(e))

    # ── 5c. Relation structure validation ────────────────────────────
    try:
        for rel in relations:
            assert rel.get("relation_type"), "Relation missing relation_type"
            assert rel.get("source_subject"), "Relation missing source_subject"
            assert rel.get("target_subject"), "Relation missing target_subject"
            # Relation type should be snake_case
            rt = rel["relation_type"]
            assert re.match(r'^[a-z][a-z0-9_]*$', rt), f"Relation type not snake_case: {rt}"
        if relations:
            record("PASS", f"5c: all {len(relations)} relations have valid structure")
        else:
            record("WARN", "5c: no relations extracted — LLM missed them")
    except Exception as e:
        record("FAIL", "5c: relation structure", str(e))

    # ── 5d-5k. Quality checks — did the LLM find the expected facts? ─
    extracted_subjects = {e.get("subject", "").lower().replace(PREFIX.lower(), "").split("_")[0] for e in entities}
    extracted_subjects |= {e.get("subject", "").lower() for e in entities}

    # Map of expected facts → whether found
    expected = {
        "company":     ("novatech", "organisation|fact"),
        "city":        ("munich", "place"),
        "manager":     ("sarah", "person"),
        "university":  ("mit", "organisation"),
        "skill":       ("kubernetes", "skill|concept"),
        "book":        ("cleancode", "media"),
    }

    found_count = 0
    for label, (keyword, _cats) in expected.items():
        # Check if any extracted entity's subject contains the keyword
        matched = False
        for ent in entities:
            subj = ent.get("subject", "").lower()
            # Strip prefix for matching
            subj_clean = subj.replace(PREFIX.lower(), "")
            if keyword in subj_clean or keyword in subj:
                matched = True
                break
        letter = chr(ord('d') + found_count)
        if matched:
            found_count += 1
            record("PASS", f"5{letter}: LLM extracted '{keyword}' entity")
        else:
            record("WARN", f"5{letter}: LLM missed '{keyword}' entity")

    # ── 5j. User entity uses "User" subject (not the user's name) ───
    try:
        user_entities = [e for e in entities if e.get("subject", "").lower() == "user"]
        if user_entities:
            record("PASS", f"5j: 'User' entity found ({len(user_entities)} entries)")
        else:
            # Acceptable — the LLM might omit the User entity if it's just updating
            record("WARN", "5j: no 'User' entity in extraction — may be ok for this conversation")
    except Exception as e:
        record("FAIL", "5j: User entity check", str(e))

    # ── 5k. Relation quality — at least some logical connections ─────
    try:
        rel_pairs = [(r.get("source_subject", ""), r.get("target_subject", "")) for r in relations]
        logical_rels = 0
        for src, tgt in rel_pairs:
            src_l, tgt_l = src.lower(), tgt.lower()
            # User → company (works at)
            if "user" in src_l and "novatech" in tgt_l:
                logical_rels += 1
            elif "user" in src_l and "munich" in tgt_l:
                logical_rels += 1
            elif "sarah" in src_l or "sarah" in tgt_l:
                logical_rels += 1
            elif "user" in src_l and ("kubernetes" in tgt_l or "cleancode" in tgt_l):
                logical_rels += 1

        if logical_rels >= 2:
            record("PASS", f"5k: {logical_rels} logically correct relations found")
        elif logical_rels >= 1:
            record("WARN", f"5k: only {logical_rels} logical relation found")
        elif relations:
            record("WARN", f"5k: {len(relations)} relations extracted but none matched expected patterns")
        else:
            record("WARN", "5k: no relations extracted at all")
    except Exception as e:
        record("FAIL", "5k: relation quality check", str(e))


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 6 · Dedup & Save Pipeline
# ═════════════════════════════════════════════════════════════════════════════

def section_6():
    print("\n" + "=" * 70)
    print("SECTION 6 · Dedup & Save Pipeline")
    print("=" * 70)

    if _fast_mode:
        record("SKIP", "6: dedup pipeline (fast mode)")
        return

    from memory_extraction import _extract_from_conversation, _dedup_and_save
    import knowledge_graph as kg

    t = _tag()

    # ── Create a conversation with known facts ───────────────────────
    conversation = (
        f"User: My sister is {PREFIX}Emma_{t}. She's a doctor at {PREFIX}StMary_{t} hospital.\n"
        f"Assistant: That's wonderful! What kind of doctor?\n"
        f"User: She's a pediatrician. She lives in {PREFIX}Dublin_{t}.\n"
        f"Assistant: Dublin is a great city!"
    )

    # 6a. Extract
    try:
        extracted = _extract_from_conversation(conversation)
        assert len(extracted) > 0
        record("PASS", f"6a: extracted {len(extracted)} items")
    except Exception as e:
        record("FAIL", "6a: extraction", str(e))
        return

    # 6b. Save via _dedup_and_save
    try:
        baseline = kg.count_entities()
        saved = _dedup_and_save(extracted, source="test_e2e")
        assert saved > 0, "Expected at least 1 saved item"
        after = kg.count_entities()
        new_count = after - baseline
        record("PASS", f"6b: _dedup_and_save saved {saved} items ({new_count} new entities)")
    except Exception as e:
        record("FAIL", "6b: _dedup_and_save", str(e))
        return

    # Track for cleanup
    all_entities = kg.list_entities(limit=10000)
    for ent in all_entities:
        if PREFIX in ent.get("subject", "") or ent.get("source") == "test_e2e":
            if ent["id"] not in _cleanup_ids:
                _cleanup_ids.append(ent["id"])

    # 6c. Entities actually in DB
    try:
        test_entities = [e for e in all_entities if PREFIX in e.get("subject", "")]
        subjects_in_db = {e["subject"].lower() for e in test_entities}
        emma_found = any(f"emma_{t}".lower() in s for s in subjects_in_db)
        if emma_found:
            record("PASS", "6c: Emma entity found in DB after dedup_and_save")
        else:
            record("WARN", f"6c: Emma not found — LLM may have used different subject. Found: {subjects_in_db}")
    except Exception as e:
        record("FAIL", "6c: DB verification", str(e))

    # 6d. Relations created between entities
    try:
        found_test_rels = False
        for ent in test_entities:
            rels = kg.get_relations(ent["id"])
            if rels:
                found_test_rels = True
                break
        if found_test_rels:
            record("PASS", "6d: relations created for test entities")
        else:
            record("WARN", "6d: no relations found for test entities")
    except Exception as e:
        record("FAIL", "6d: relation check", str(e))

    # 6e. Re-run dedup — should NOT create duplicates
    try:
        count_before = kg.count_entities()
        saved2 = _dedup_and_save(extracted, source="test_e2e")
        count_after = kg.count_entities()
        new = count_after - count_before
        if new == 0:
            record("PASS", f"6e: re-run created 0 new entities (dedup works)")
        else:
            # Track new entities for cleanup
            new_entities = kg.list_entities(limit=10000)
            for ent in new_entities:
                if PREFIX in ent.get("subject", "") and ent["id"] not in _cleanup_ids:
                    _cleanup_ids.append(ent["id"])
            record("FAIL", f"6e: dedup created {new} duplicate entities")
    except Exception as e:
        record("FAIL", "6e: dedup idempotency", str(e))

    # 6f. _skip_reindex flag is False after _dedup_and_save
    try:
        assert kg._skip_reindex is False, f"_skip_reindex is {kg._skip_reindex}"
        record("PASS", "6f: _skip_reindex reset to False after _dedup_and_save")
    except Exception as e:
        record("FAIL", "6f: _skip_reindex state", str(e))

    # 6g. Content merging — extract slightly different facts and verify merge
    try:
        updated_conv = (
            f"User: My sister {PREFIX}Emma_{t} also teaches at the university part-time.\n"
            f"Assistant: Oh she's busy!"
        )
        extracted2 = _extract_from_conversation(updated_conv)
        if extracted2:
            saved3 = _dedup_and_save(extracted2, source="test_e2e")
            # Check that Emma's description was enriched, not duplicated
            count_final = kg.count_entities()
            emma_entities = [
                e for e in kg.list_entities(limit=10000)
                if PREFIX in e.get("subject", "") and f"emma_{t}".lower() in e.get("subject", "").lower()
            ]
            if len(emma_entities) == 1:
                desc = emma_entities[0].get("description", "")
                if "teach" in desc.lower() or "university" in desc.lower():
                    record("PASS", "6g: content merge — new info appended to existing entity")
                else:
                    record("WARN", "6g: content merge — entity exists but new info not visible in description")
            elif len(emma_entities) == 0:
                record("WARN", "6g: Emma entity not found for merge test")
            else:
                record("FAIL", f"6g: content merge created {len(emma_entities)} Emma entities (expected 1)")
        else:
            record("SKIP", "6g: second extraction returned empty")
    except Exception as e:
        record("FAIL", "6g: content merge", str(e))

    _cleanup()


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 7 · Dream Cycle (merge, enrich, infer)
# ═════════════════════════════════════════════════════════════════════════════

def section_7():
    print("\n" + "=" * 70)
    print("SECTION 7 · Dream Cycle Phases")
    print("=" * 70)

    if _fast_mode:
        record("SKIP", "7: dream cycle (fast mode)")
        return

    import knowledge_graph as kg
    import dream_cycle as dc

    t = _tag()

    # ── 7a. Dream config load/save ───────────────────────────────────
    try:
        cfg = dc.get_config()
        assert isinstance(cfg, dict)
        assert "merge_threshold" in cfg
        assert "enrich_min_chars" in cfg
        record("PASS", f"7a: dream config — merge_threshold={cfg.get('merge_threshold')}")
    except Exception as e:
        record("FAIL", "7a: dream config", str(e))

    # ── 7b. _find_thin_entities ──────────────────────────────────────
    try:
        from dream_cycle import _find_thin_entities
        batch = [
            {"id": "a", "description": "Short"},
            {"id": "b", "description": "This is a longer description that has enough characters to pass"},
        ]
        thin = _find_thin_entities(batch, min_chars=30)
        assert len(thin) == 1 and thin[0]["id"] == "a"
        record("PASS", "7b: _find_thin_entities filters correctly")
    except Exception as e:
        record("FAIL", "7b: _find_thin_entities", str(e))

    # ── 7c. _merge_entities with real LLM ────────────────────────────
    try:
        from dream_cycle import _merge_entities

        # Create two near-duplicate entities
        e1 = kg.save_entity("person", f"{PREFIX}JohnSmith_{t}", "Software developer who builds web apps", source="test")
        _cleanup_ids.append(e1["id"])
        import time; time.sleep(0.05)  # Ensure different created_at
        e2 = kg.save_entity("person", f"{PREFIX}John_{t}", "Developer working on web applications", source="test")
        _cleanup_ids.append(e2["id"])

        # Add a relation to e2 so we can check re-pointing
        placeholder = kg.save_entity("organisation", f"{PREFIX}WebCo_{t}", "Web company", source="test")
        _cleanup_ids.append(placeholder["id"])
        kg.add_relation(e2["id"], placeholder["id"], "works_at", source="test")

        result = _merge_entities(e1, e2)
        if result:
            assert result["survivor_id"] == e1["id"], "Older entity should survive"
            assert result.get("duplicate_id") == e2["id"]

            # Check that the duplicate is actually deleted
            deleted_check = kg.get_entity(e2["id"])
            assert deleted_check is None, "Duplicate should be deleted"

            # Check that relation was re-pointed to survivor
            rels = kg.get_relations(e1["id"])
            works_at = [r for r in rels if r.get("relation_type") == "works_at"]
            if works_at:
                record("PASS", "7c: _merge_entities — merged, relation re-pointed")
            else:
                record("WARN", "7c: merge succeeded but relation not re-pointed")

            # Remove e2 from cleanup (already deleted by merge)
            if e2["id"] in _cleanup_ids:
                _cleanup_ids.remove(e2["id"])
        else:
            record("WARN", "7c: _merge_entities returned None — LLM may have declined")
    except Exception as e:
        record("FAIL", "7c: _merge_entities", str(e))

    # ── 7d. _enrich_entity with real LLM ─────────────────────────────
    try:
        from dream_cycle import _enrich_entity

        thin_entity = kg.save_entity("person", f"{PREFIX}Tina_{t}", "Friend of user", source="test")
        _cleanup_ids.append(thin_entity["id"])

        excerpts = [
            f"User: I had lunch with {PREFIX}Tina_{t} yesterday. She's a data analyst at Google.",
            f"User: {PREFIX}Tina_{t} recommended a great podcast about AI. She's always up to date on tech.",
        ]

        result = _enrich_entity(thin_entity, excerpts)
        if result:
            assert result["new_length"] > result["old_length"], "Enriched description should be longer"
            # Verify the entity was actually updated in DB
            updated = kg.get_entity(thin_entity["id"])
            assert len(updated["description"]) > len("Friend of user")
            record("PASS", f"7d: _enrich_entity — {result['old_length']}→{result['new_length']} chars")
        else:
            record("WARN", "7d: _enrich_entity returned None — LLM may have declined")
    except Exception as e:
        record("FAIL", "7d: _enrich_entity", str(e))

    # ── 7e. _infer_relation with real LLM ────────────────────────────
    try:
        from dream_cycle import _infer_relation

        e_person = kg.save_entity("person", f"{PREFIX}Dave_{t}", "User's colleague at work", source="test")
        _cleanup_ids.append(e_person["id"])
        e_place = kg.save_entity("place", f"{PREFIX}Sydney_{t}", "City in Australia", source="test")
        _cleanup_ids.append(e_place["id"])

        excerpt = (
            f"User: {PREFIX}Dave_{t} just moved to {PREFIX}Sydney_{t} for a new position. "
            f"He's going to work at the Sydney office."
        )

        result = _infer_relation(e_person, e_place, excerpt, confidence=0.8, co_occurrence_count=2)
        if result:
            assert result.get("relation_type"), "Inferred relation should have type"
            assert result.get("confidence"), "Inferred relation should have confidence"
            assert result["relation_type"] not in ("related_to", "associated_with"), \
                f"Inferred vague type '{result['relation_type']}' should be rejected"
            # Verify relation exists in DB
            rels = kg.get_relations(e_person["id"], direction="both")
            peer_ids = {r["peer_id"] for r in rels}
            # Check both directions since LLM controls directionality
            if e_place["id"] in peer_ids:
                record("PASS", f"7e: _infer_relation created '{result['relation_type']}' "
                       f"(conf={result['confidence']:.2f})")
            else:
                # LLM may have swapped direction — check from place side
                rels2 = kg.get_relations(e_place["id"], direction="both")
                peer_ids2 = {r["peer_id"] for r in rels2}
                if e_person["id"] in peer_ids2:
                    record("PASS", f"7e: _infer_relation created '{result['relation_type']}' "
                           f"(conf={result['confidence']:.2f}, direction swapped by LLM)")
                else:
                    record("WARN", "7e: _infer_relation reported success but relation not in DB")
        else:
            record("WARN", "7e: _infer_relation returned None — LLM found no relation")
    except Exception as e:
        record("FAIL", "7e: _infer_relation", str(e))

    # ── 7f. Dream journal ────────────────────────────────────────────
    try:
        journal = dc.get_journal(limit=5)
        assert isinstance(journal, list)
        record("PASS", f"7f: dream journal accessible — {len(journal)} entries")
    except Exception as e:
        record("FAIL", "7f: dream journal", str(e))

    _cleanup()


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 8 · Full Extraction Pipeline (run_extraction flow)
# ═════════════════════════════════════════════════════════════════════════════

def section_8():
    print("\n" + "=" * 70)
    print("SECTION 8 · Full Extraction Pipeline Simulation")
    print("=" * 70)

    if _fast_mode:
        record("SKIP", "8: full extraction pipeline (fast mode)")
        return

    from memory_extraction import _extract_from_conversation, _dedup_and_save
    import knowledge_graph as kg

    t = _tag()

    # Simulate what run_extraction does: multiple conversations → extract → dedup
    conversations = [
        # Conv 1: Family info
        (
            f"User: My mom {PREFIX}Maria_{t} lives in {PREFIX}Rome_{t}. She's a retired teacher.\n"
            f"Assistant: That sounds lovely!\n"
            f"User: Yeah, she taught history for 30 years at {PREFIX}RomaUniv_{t}.\n"
            f"Assistant: Impressive career!"
        ),
        # Conv 2: Overlapping + new info (dedup test)
        (
            f"User: I'm planning to visit my mother {PREFIX}Maria_{t} in {PREFIX}Rome_{t} next month.\n"
            f"Assistant: That'll be nice!\n"
            f"User: She's also writing a book about {PREFIX}MedievalHistory_{t}.\n"
            f"Assistant: Fascinating topic!"
        ),
        # Conv 3: Completely different topic
        (
            f"User: I just adopted a cat named {PREFIX}Whiskers_{t}.\n"
            f"Assistant: How cute! What breed?\n"
            f"User: She's a British Shorthair. Also I've started playing {PREFIX}Chess_{t} competitively.\n"
            f"Assistant: Great hobbies!"
        ),
    ]

    statuses: list[str] = []

    def on_status(msg: str):
        statuses.append(msg)
        print(f"    📡 {msg}")

    # 8a. Process multiple conversations
    total_saved = 0
    all_extracted = []
    try:
        baseline = kg.count_entities()
        for i, conv in enumerate(conversations):
            extracted = _extract_from_conversation(conv)
            all_extracted.extend(extracted)
            on_status(f"Conv {i+1}: extracted {len(extracted)} items")

        on_status(f"Total extracted: {len(all_extracted)} items")
        record("PASS", f"8a: extracted {len(all_extracted)} items from {len(conversations)} conversations")
    except Exception as e:
        record("FAIL", "8a: multi-conversation extraction", str(e))
        return

    # 8b. Batch save with dedup
    try:
        kg._skip_reindex = True
        try:
            saved = _dedup_and_save(all_extracted, source="test_pipeline")
        finally:
            kg._skip_reindex = False

        after = kg.count_entities()
        new_count = after - baseline
        record("PASS", f"8b: batch save — {saved} returned, {new_count} new entities in DB")
    except Exception as e:
        record("FAIL", "8b: batch save", str(e))
        return

    # Track for cleanup
    for ent in kg.list_entities(limit=10000):
        if PREFIX in ent.get("subject", "") or ent.get("source") == "test_pipeline":
            if ent["id"] not in _cleanup_ids:
                _cleanup_ids.append(ent["id"])

    # 8c. Rebuild FAISS and verify
    try:
        kg.rebuild_index()
        record("PASS", "8c: FAISS rebuild after batch save")
    except Exception as e:
        record("FAIL", "8c: FAISS rebuild", str(e))

    # 8d. Cross-conversation dedup verification
    try:
        # Maria appears in conv 1 AND conv 2 — should be one entity
        # LLM may drop PREFIX, so search broadly by source + subject keyword
        maria_entities = [
            e for e in kg.list_entities(limit=10000)
            if (e.get("source") == "test_pipeline"
                and "maria" in e.get("subject", "").lower())
        ]
        if len(maria_entities) == 1:
            desc = maria_entities[0].get("description", "")
            record("PASS", f"8d: cross-conv dedup — 1 Maria entity (desc={len(desc)} chars)")
        elif len(maria_entities) == 0:
            record("WARN", "8d: Maria entity not found (LLM may have used different subject)")
        else:
            record("FAIL", f"8d: cross-conv dedup failed — {len(maria_entities)} Maria entities")
    except Exception as e:
        record("FAIL", "8d: cross-conv dedup", str(e))

    # 8e. Content from conv 2 merged into Maria entity
    try:
        if maria_entities and len(maria_entities) == 1:
            desc = maria_entities[0].get("description", "").lower()
            has_teacher = "teacher" in desc or "taught" in desc or "history" in desc
            has_book = "book" in desc or "writing" in desc or "medieval" in desc
            if has_teacher and has_book:
                record("PASS", "8e: Maria description merged info from both conversations")
            elif has_teacher:
                record("WARN", "8e: Maria has teacher info but not book info (partial merge)")
            else:
                record("WARN", "8e: Maria description missing expected content")
        else:
            record("SKIP", "8e: Maria not found — skipping merge check")
    except Exception as e:
        record("FAIL", "8e: content merge check", str(e))

    # 8f. Different entities from conv 3 also saved
    try:
        all_test = [e for e in kg.list_entities(limit=10000)
                    if PREFIX in e.get("subject", "") or e.get("source") == "test_pipeline"]
        test_subjects = {e["subject"].lower() for e in all_test}
        whiskers_found = any("whiskers" in s for s in test_subjects)
        chess_found = any("chess" in s for s in test_subjects)
        if whiskers_found and chess_found:
            record("PASS", "8f: conv 3 entities (Whiskers, Chess) saved independently")
        elif whiskers_found or chess_found:
            record("WARN", f"8f: partial — Whiskers={whiskers_found}, Chess={chess_found}")
        else:
            record("WARN", "8f: conv 3 entities not found (LLM may have used different subjects)")
    except Exception as e:
        record("FAIL", "8f: conv 3 entities", str(e))

    # Track all pipeline entities for cleanup
    for ent in all_test:
        if ent["id"] not in _cleanup_ids:
            _cleanup_ids.append(ent["id"])

    # 8g. Relations link entities correctly
    try:
        total_test_rels = 0
        for ent in all_test:
            rels = kg.get_relations(ent["id"])
            total_test_rels += len(rels)
        if total_test_rels >= 3:
            record("PASS", f"8g: {total_test_rels} relations across test entities")
        elif total_test_rels >= 1:
            record("WARN", f"8g: only {total_test_rels} relations (expected ≥3)")
        else:
            record("WARN", "8g: no relations between test entities")
    except Exception as e:
        record("FAIL", "8g: relation check", str(e))

    # 8h. Semantic search finds entities from any conversation
    try:
        results = kg.semantic_search("retired teacher history Rome", top_k=10, threshold=0.2)
        maria_found = any("maria" in r.get("subject", "").lower() for r in results)
        if maria_found:
            record("PASS", "8h: semantic search finds Maria via 'retired teacher history Rome'")
        else:
            record("WARN", "8h: semantic search did not find Maria")
    except Exception as e:
        record("FAIL", "8h: semantic search", str(e))

    _cleanup()


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 9 · Graph Integrity & Thread Safety
# ═════════════════════════════════════════════════════════════════════════════

def section_9():
    print("\n" + "=" * 70)
    print("SECTION 9 · Graph Integrity & Thread Safety")
    print("=" * 70)

    import knowledge_graph as kg
    import threading

    t = _tag()

    # 9a. _graph_lock is RLock (reentrant)
    try:
        lock_type = type(kg._graph_lock).__name__
        assert "RLock" in lock_type or "_RLock" in lock_type, f"Expected RLock, got {lock_type}"
        # Verify reentrant: acquire twice without deadlock
        kg._graph_lock.acquire()
        kg._graph_lock.acquire()
        kg._graph_lock.release()
        kg._graph_lock.release()
        record("PASS", f"9a: _graph_lock is reentrant ({lock_type})")
    except Exception as e:
        record("FAIL", "9a: _graph_lock type", str(e))

    # 9b. _graph is MultiDiGraph
    try:
        import networkx as nx
        g = kg._ensure_graph()
        assert isinstance(g, nx.MultiDiGraph)
        record("PASS", "9b: _graph is MultiDiGraph")
    except Exception as e:
        record("FAIL", "9b: MultiDiGraph check", str(e))

    # 9c. Concurrent entity creation (thread safety)
    try:
        errors = []
        created_ids = []
        lock = threading.Lock()

        def create_entity(idx):
            try:
                e = kg.save_entity("fact", f"{PREFIX}Thread{idx}_{t}", f"Fact #{idx} from thread test", source="test")
                with lock:
                    created_ids.append(e["id"])
            except Exception as exc:
                with lock:
                    errors.append(str(exc))

        threads = [threading.Thread(target=create_entity, args=(i,)) for i in range(10)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=30)

        _cleanup_ids.extend(created_ids)

        if errors:
            record("FAIL", f"9c: concurrent creation — {len(errors)} errors", errors[0])
        elif len(created_ids) == 10:
            record("PASS", "9c: 10 concurrent entity creations succeeded")
        else:
            record("WARN", f"9c: only {len(created_ids)}/10 created")
    except Exception as e:
        record("FAIL", "9c: concurrent creation", str(e))

    # 9d. Concurrent relation creation
    try:
        if len(created_ids) >= 4:
            rel_errors = []
            rel_count = [0]

            def create_relation(i):
                try:
                    src = created_ids[i]
                    tgt = created_ids[(i + 1) % len(created_ids)]
                    kg.add_relation(src, tgt, f"test_rel_{i}", source="test")
                    with lock:
                        rel_count[0] += 1
                except Exception as exc:
                    with lock:
                        rel_errors.append(str(exc))

            threads = [threading.Thread(target=create_relation, args=(i,)) for i in range(8)]
            for th in threads:
                th.start()
            for th in threads:
                th.join(timeout=30)

            if rel_errors:
                record("FAIL", f"9d: concurrent relations — {len(rel_errors)} errors", rel_errors[0])
            else:
                record("PASS", f"9d: {rel_count[0]} concurrent relation creations succeeded")
        else:
            record("SKIP", "9d: not enough entities for concurrent relation test")
    except Exception as e:
        record("FAIL", "9d: concurrent relations", str(e))

    # 9e. NetworkX graph mirrors DB state
    try:
        g = kg._ensure_graph()
        db_count = kg.count_entities()
        graph_nodes = g.number_of_nodes()
        # They should be close (graph may have slight lag)
        diff = abs(db_count - graph_nodes)
        if diff <= 2:
            record("PASS", f"9e: DB={db_count}, graph={graph_nodes} entities (diff={diff})")
        else:
            record("WARN", f"9e: DB={db_count} vs graph={graph_nodes} entities (diff={diff})")
    except Exception as e:
        record("FAIL", "9e: DB/graph sync", str(e))

    # 9f. DB relation count vs graph edge count
    try:
        db_rels = kg.count_relations()
        graph_edges = g.number_of_edges()
        diff = abs(db_rels - graph_edges)
        if diff <= 2:
            record("PASS", f"9f: DB={db_rels}, graph={graph_edges} relations (diff={diff})")
        else:
            record("WARN", f"9f: DB={db_rels} vs graph={graph_edges} relations (diff={diff})")
    except Exception as e:
        record("FAIL", "9f: DB/graph relation sync", str(e))

    _cleanup()


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 10 · Edge Cases & Robustness
# ═════════════════════════════════════════════════════════════════════════════

def section_10():
    print("\n" + "=" * 70)
    print("SECTION 10 · Edge Cases & Robustness")
    print("=" * 70)

    import knowledge_graph as kg
    import memory as mem
    from memory_extraction import _dedup_and_save

    t = _tag()

    # 10a. Empty extraction saves nothing
    try:
        count_before = kg.count_entities()
        saved = _dedup_and_save([], source="test")
        count_after = kg.count_entities()
        assert saved == 0
        assert count_after == count_before
        record("PASS", "10a: empty extraction — 0 saved, no side effects")
    except Exception as e:
        record("FAIL", "10a: empty extraction", str(e))

    # 10b. Malformed extraction data handled gracefully
    # Test each bad input individually — some may raise, which is acceptable
    bad_inputs = [
        ({"category": "person"},                                  "missing subject+content"),
        ({"subject": "test", "content": "test"},                  "missing category"),
        ({"category": "INVALID", "subject": "test", "content": "test"}, "invalid category"),
        ({},                                                      "empty dict"),
        ({"category": "person", "subject": "", "content": "x"},  "empty subject"),
    ]
    crashes = []
    saved_any = False
    for bad, label in bad_inputs:
        try:
            count_before = kg.count_entities()
            _dedup_and_save([bad], source="test")
            count_after = kg.count_entities()
            if count_after > count_before:
                saved_any = True
                for ent in kg.list_entities(limit=10000):
                    if ent.get("source") == "test" and ent["id"] not in _cleanup_ids:
                        _cleanup_ids.append(ent["id"])
        except Exception:
            pass  # crashing on malformed input is acceptable
    if not saved_any:
        record("PASS", "10b: malformed data — nothing saved (correct)")
    else:
        record("WARN", "10b: some malformed data was saved")

    # 10c. Very long description handled
    try:
        long_desc = "A" * 10000
        e = mem.save_memory("fact", f"{PREFIX}Long_{t}", long_desc, tags="test")
        _cleanup_ids.append(e["id"])
        fetched = kg.get_entity(e["id"])
        assert len(fetched["description"]) >= 5000  # should store most of it
        record("PASS", f"10c: long description stored ({len(fetched['description'])} chars)")
    except Exception as e:
        record("FAIL", "10c: long description", str(e))

    # 10d. Unicode/emoji handling
    try:
        e = mem.save_memory("fact", f"{PREFIX}Uni_{t}", "User likes 日本語 and 🎵 music", tags="test")
        _cleanup_ids.append(e["id"])
        fetched = kg.get_entity(e["id"])
        assert "日本語" in fetched["description"]
        record("PASS", "10d: unicode + emoji preserved in description")
    except Exception as e:
        record("FAIL", "10d: unicode handling", str(e))

    # 10e. _normalize_subject consistency
    try:
        assert kg._normalize_subject("  Hello  World  ") == "hello world"
        assert kg._normalize_subject("UPPER") == "upper"
        assert kg._normalize_subject("  ") == ""
        record("PASS", "10e: _normalize_subject handles whitespace + case")
    except Exception as e:
        record("FAIL", "10e: _normalize_subject", str(e))

    # 10f. extract_json_block robustness
    try:
        assert kg.extract_json_block('Here is the data: [{"a":1}]') == '[{"a":1}]'
        assert kg.extract_json_block('No JSON here') is None
        assert kg.extract_json_block('[{"nested": [1,2,3]}]') == '[{"nested": [1,2,3]}]'
        assert kg.extract_json_block('prefix {"key": "val"} suffix', "{") == '{"key": "val"}'
        # Greedy trap: should NOT match across two separate JSON blocks
        trap = 'Here [1,2] and then [3,4] end'
        result = kg.extract_json_block(trap, "[")
        assert result == "[1,2]", f"Greedy trap: got {result}"
        record("PASS", "10f: extract_json_block handles edge cases")
    except Exception as e:
        record("FAIL", "10f: extract_json_block", str(e))

    # 10g. delete_entity returns False for nonexistent ID
    try:
        result = kg.delete_entity("nonexistent_id_12345")
        assert result is False
        record("PASS", "10g: delete_entity returns False for missing ID")
    except Exception as e:
        record("FAIL", "10g: delete_entity missing ID", str(e))

    # 10h. add_relation rejects nonexistent entity IDs
    try:
        result = kg.add_relation("fake_src_123", "fake_tgt_456", "test_rel")
        # Should either return None or raise — both are acceptable
        if result is None:
            record("PASS", "10h: add_relation returns None for fake IDs")
        else:
            record("WARN", "10h: add_relation accepted fake IDs")
    except Exception:
        record("PASS", "10h: add_relation rejects fake IDs with exception")

    # 10i. Consolidate duplicates
    try:
        # Create two entities with very similar content
        e1 = mem.save_memory("fact", f"{PREFIX}Dup1_{t}", "The user enjoys drinking green tea every morning", tags="test")
        _cleanup_ids.append(e1["id"])
        e2 = mem.save_memory("fact", f"{PREFIX}Dup1_{t}", "User likes to drink green tea in the morning", tags="test")
        _cleanup_ids.append(e2["id"])

        kg.rebuild_index()
        count_before = kg.count_entities()
        merged = kg.consolidate_duplicates(threshold=0.88)
        count_after = kg.count_entities()

        if merged > 0:
            record("PASS", f"10i: consolidate_duplicates merged {merged} entities")
        else:
            record("WARN", "10i: consolidate_duplicates merged 0 — threshold may be too high")
    except Exception as e:
        record("FAIL", "10i: consolidate_duplicates", str(e))

    _cleanup()


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 11 · LLM Extraction Stress Tests
# ═════════════════════════════════════════════════════════════════════════════

def section_11():
    print("\n" + "=" * 70)
    print("SECTION 11 · LLM Extraction Stress Tests")
    print("=" * 70)

    if _fast_mode:
        record("SKIP", "11: stress tests (fast mode)")
        return

    from memory_extraction import _extract_from_conversation

    # 11a. Empty conversation
    try:
        result = _extract_from_conversation("")
        assert isinstance(result, list)
        assert len(result) == 0, "Empty conversation should yield empty list"
        record("PASS", "11a: empty conversation returns []")
    except Exception as e:
        record("FAIL", "11a: empty conversation", str(e))

    # 11b. Conversation with no extractable facts
    try:
        result = _extract_from_conversation(
            "User: What's 2+2?\nAssistant: 4.\nUser: Thanks!"
        )
        if len(result) == 0:
            record("PASS", "11b: trivial conversation returns empty (no false positives)")
        else:
            record("WARN", f"11b: trivial conversation extracted {len(result)} items (possible false positive)")
            for item in result:
                print(f"       FP: {item}")
    except Exception as e:
        record("FAIL", "11b: trivial conversation", str(e))

    # 11c. Conversation with correction — should NOT extract wrong version
    t = _tag()
    try:
        result = _extract_from_conversation(
            f"User: My birthday is March 15.\n"
            f"Assistant: I'll remember that.\n"
            f"User: Actually wait, I made a mistake. My birthday is March 20.\n"
            f"Assistant: Updated!"
        )
        # Should only have March 20, not March 15
        entities = [e for e in result if e.get("category")]
        for ent in entities:
            content = ent.get("content", "").lower()
            if "march 15" in content and "march 20" not in content:
                record("FAIL", "11c: LLM extracted the WRONG date (March 15)")
                break
            elif "march 20" in content:
                record("PASS", "11c: LLM correctly extracted only corrected date (March 20)")
                break
        else:
            if entities:
                record("WARN", "11c: birthday entity found but couldn't verify date")
            else:
                record("WARN", "11c: no birthday entity extracted")
    except Exception as e:
        record("FAIL", "11c: correction handling", str(e))

    # 11d. Dense conversation with many facts
    try:
        dense = (
            "User: Let me tell you about my family. "
            "My wife Lisa is a nurse at General Hospital. "
            "We have two kids — Jake who's 8 and Sophie who's 12. "
            "We live in Portland, Oregon. "
            "I work at Intel as a chip designer. "
            "My hobby is woodworking and I'm building a bookshelf. "
            "I'm also training for a marathon next spring. "
            "My favorite book is Dune by Frank Herbert.\n"
            "Assistant: That's quite a full life!"
        )
        result = _extract_from_conversation(dense)
        entities = [e for e in result if e.get("category")]
        relations = [e for e in result if e.get("relation_type")]

        print(f"\n    📊 Dense extraction: {len(entities)} entities, {len(relations)} relations")
        for ent in entities:
            print(f"       [{ent.get('category')}] {ent.get('subject')}")

        # Expect at least 5 entities from this very rich conversation
        if len(entities) >= 5:
            record("PASS", f"11d: dense conversation — {len(entities)} entities (≥5 expected)")
        elif len(entities) >= 3:
            record("WARN", f"11d: dense conversation — only {len(entities)} entities (expected ≥5)")
        else:
            record("FAIL", f"11d: dense conversation — only {len(entities)} entities")

        # Expect at least 3 relations
        if len(relations) >= 3:
            record("PASS", f"11d+: dense conversation — {len(relations)} relations (≥3 expected)")
        elif len(relations) >= 1:
            record("WARN", f"11d+: dense conversation — only {len(relations)} relations (expected ≥3)")
        else:
            record("WARN", "11d+: dense conversation — no relations extracted")

    except Exception as e:
        record("FAIL", "11d: dense conversation", str(e))

    # 11e. Conversation referencing tool results — should NOT extract tool data
    try:
        result = _extract_from_conversation(
            "User: What's the weather in London?\n"
            "Assistant: Based on the weather data, it's currently 15°C and partly cloudy in London. "
            "The humidity is 72% and wind is 12 km/h from the west.\n"
            "User: OK thanks. Can you search for flight prices to Paris?\n"
            "Assistant: I found several flights: BA304 at £120, AF1234 at €95..."
        )
        if len(result) == 0:
            record("PASS", "11e: tool-result conversation — correctly returned [] (no user facts)")
        else:
            record("WARN", f"11e: tool-result conversation extracted {len(result)} items (possible false positive)")
    except Exception as e:
        record("FAIL", "11e: tool-result filtering", str(e))

    # 11f. Alias extraction quality
    try:
        result = _extract_from_conversation(
            "User: My mom Sarah — we call her Sally — just turned 60.\n"
            "Assistant: Happy birthday to her!"
        )
        entities = [e for e in result if e.get("category")]
        mom_entities = [e for e in entities if any(
            name in e.get("subject", "").lower()
            for name in ["mom", "sarah", "sally", "mother"]
        )]
        if mom_entities:
            aliases = mom_entities[0].get("aliases", "")
            has_aliases = bool(aliases and len(aliases) > 2)
            if has_aliases:
                record("PASS", f"11f: alias extraction — aliases='{aliases}'")
            else:
                record("WARN", f"11f: mom entity found but no aliases (subject={mom_entities[0].get('subject')})")
        else:
            record("WARN", "11f: no mom/Sarah entity found")
    except Exception as e:
        record("FAIL", "11f: alias extraction", str(e))


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 12 · Repair & Maintenance Operations
# ═════════════════════════════════════════════════════════════════════════════

def section_12():
    print("\n" + "=" * 70)
    print("SECTION 12 · Repair & Maintenance Operations")
    print("=" * 70)

    import knowledge_graph as kg

    t = _tag()

    # 12a. repair_graph_islands removed (no longer auto-bridges)
    if not callable(getattr(kg, "repair_graph_islands", None)):
        record("PASS", "12a: repair_graph_islands correctly removed")
    else:
        record("FAIL", "12a: repair_graph_islands should be removed")

    # 12b. Verify vague relation types are rejected by add_relation
    try:
        orphan = kg.save_entity("fact", f"{PREFIX}Orphan_{t}", "Isolated test fact", source="test")
        _cleanup_ids.append(orphan["id"])

        # Create a second entity to test vague relation rejection
        target = kg.save_entity("fact", f"{PREFIX}Target_{t}", "Target test fact", source="test")
        _cleanup_ids.append(target["id"])

        vague_result = kg.add_relation(orphan["id"], target["id"], "related_to", source="test")
        if vague_result is None:
            record("PASS", "12b: vague relation type 'related_to' correctly rejected")
        else:
            record("FAIL", "12b: vague relation type 'related_to' should be rejected")
    except Exception as e:
        record("FAIL", "12b: vague relation rejection", str(e))

    # 12c. get_connected_components
    try:
        components = kg.get_connected_components()
        if components:
            sizes = sorted([len(c) for c in components], reverse=True)
            record("PASS", f"12c: {len(components)} components — largest={sizes[0]}, sizes={sizes[:5]}")
        else:
            record("WARN", "12c: no connected components found")
    except Exception as e:
        record("FAIL", "12c: get_connected_components", str(e))

    _cleanup()


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 13 · Document Loading & Chunking
# ═════════════════════════════════════════════════════════════════════════════

_TEST_DOC_NAME = f"{PREFIX}test_document.txt"

_TEST_DOC_CONTENT = """\
Project Aurora: Annual Research Report 2025

Executive Summary

Project Aurora is a collaborative research initiative led by Dr. Elena Vasquez \
at the Zurich Institute of Advanced Computing (ZIAC). The project focuses on \
developing novel quantum error-correction algorithms that can operate at room \
temperature. Funded by the European Research Council with a grant of €4.2 million, \
Aurora has produced three peer-reviewed papers in Nature Quantum and attracted \
partnerships with IBM Research and the Max Planck Institute.

Key Findings

The team demonstrated a 47% improvement in qubit coherence times using a \
topological code approach. Lead researcher Dr. Marcus Chen developed a new \
stabilizer formalism called "Adaptive Surface Codes" (ASC) that reduces the \
overhead of error correction by 30% compared to standard surface codes.

Dr. Sofia Andersson, the project's experimental physicist, successfully \
implemented ASC on a 72-qubit superconducting processor at the ZIAC quantum lab \
in Zurich, Switzerland. This marks the first real-world validation of the \
ASC approach.

The project also explored applications in drug discovery, collaborating with \
Novartis to simulate molecular interactions for Alzheimer's treatment candidates. \
Initial results show that quantum-enhanced simulations can identify binding \
affinities 100x faster than classical methods.

Team and Organization

Dr. Elena Vasquez (Principal Investigator) — Quantum computing researcher with \
20 years of experience, previously at MIT and CERN.
Dr. Marcus Chen (Lead Theorist) — Specialist in quantum error correction, \
author of the ASC formalism.
Dr. Sofia Andersson (Experimental Lead) — Expert in superconducting qubit \
fabrication and measurement.

The team operates within ZIAC's Quantum Computing Division in Zurich and \
maintains active collaborations with IBM Research in Yorktown Heights, New York \
and the Max Planck Institute in Garching, Germany.

Future Directions

Aurora Phase 2 (2026-2028) will focus on scaling ASC to 1000+ qubit systems \
and establishing a quantum advantage benchmark for pharmaceutical applications. \
The team plans to open-source the ASC software toolkit on GitHub by Q3 2026.
"""


def _write_test_doc() -> str:
    """Write the test document to a temp file and return its path."""
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".txt", prefix=PREFIX)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(_TEST_DOC_CONTENT)
    return path


def section_13():
    print("\n" + "=" * 70)
    print("SECTION 13 · Document Loading & Chunking")
    print("=" * 70)

    from documents import load_document_text, get_vector_store
    from document_extraction import _split_into_windows

    test_path = _write_test_doc()

    try:
        # 13a. load_document_text returns full text + title
        try:
            full_text, title = load_document_text(test_path)
            assert isinstance(full_text, str) and len(full_text) > 500
            assert title  # derived from filename stem
            record("PASS", f"13a: load_document_text — {len(full_text)} chars, title='{title}'")
        except Exception as e:
            record("FAIL", "13a: load_document_text", str(e))
            return

        # 13b. Content integrity — key phrases survive loading
        try:
            checks = ["Dr. Elena Vasquez", "Zurich Institute", "Adaptive Surface Codes",
                       "Novartis", "IBM Research", "Max Planck"]
            missing = [c for c in checks if c not in full_text]
            if not missing:
                record("PASS", "13b: all 6 key phrases preserved in loaded text")
            else:
                record("FAIL", f"13b: missing phrases: {missing}")
        except Exception as e:
            record("FAIL", "13b: content integrity", str(e))

        # 13c. _split_into_windows — single window for small doc
        try:
            windows = _split_into_windows(full_text)
            assert len(windows) >= 1
            total_chars = sum(len(w) for w in windows)
            # With overlap, total chars >= original text
            assert total_chars >= len(full_text)
            record("PASS", f"13c: split into {len(windows)} window(s), total={total_chars} chars")
        except Exception as e:
            record("FAIL", "13c: _split_into_windows", str(e))

        # 13d. Window overlap — consecutive windows share content
        try:
            if len(windows) >= 2:
                w1_end = windows[0][-500:]
                w2_start = windows[1][:500]
                # The overlap region should share characters
                overlap_found = False
                for length in range(50, 501, 50):
                    if windows[0][-length:] in windows[1]:
                        overlap_found = True
                        break
                if overlap_found:
                    record("PASS", "13d: window overlap verified between consecutive windows")
                else:
                    record("FAIL", "13d: no overlap found between windows")
            else:
                record("SKIP", "13d: only 1 window — overlap test N/A")
        except Exception as e:
            record("FAIL", "13d: window overlap", str(e))

        # 13e. Large document splitting
        try:
            big_text = "X" * 20000
            big_windows = _split_into_windows(big_text, window_size=6000, overlap=500)
            assert len(big_windows) >= 3, f"Expected ≥3 windows for 20k chars, got {len(big_windows)}"
            # Each window should be ≤ window_size
            for i, w in enumerate(big_windows):
                assert len(w) <= 6000, f"Window {i} exceeds 6000 chars: {len(w)}"
            record("PASS", f"13e: 20k text → {len(big_windows)} windows, all ≤6000 chars")
        except Exception as e:
            record("FAIL", "13e: large doc splitting", str(e))

        # 13f. Empty/tiny text handled
        try:
            tiny = _split_into_windows("Hello")
            assert len(tiny) == 1 and tiny[0] == "Hello"
            empty = _split_into_windows("")
            assert len(empty) == 1 and empty[0] == ""
            record("PASS", "13f: tiny/empty text → 1 window each")
        except Exception as e:
            record("FAIL", "13f: tiny text handling", str(e))

        # 13g. Unsupported file type rejected
        try:
            load_document_text("fake_file.xyz")
            record("FAIL", "13g: unsupported type was accepted")
        except ValueError:
            record("PASS", "13g: unsupported file type raises ValueError")
        except Exception as e:
            record("FAIL", "13g: unexpected error for bad type", str(e))

    finally:
        try:
            os.unlink(test_path)
        except OSError:
            pass


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 14 · Document Extraction Pipeline (LLM)
# ═════════════════════════════════════════════════════════════════════════════

def section_14():
    print("\n" + "=" * 70)
    print("SECTION 14 · Document Extraction Pipeline (LLM)")
    print("=" * 70)

    if _fast_mode:
        record("SKIP", "14: document extraction (fast mode)")
        return

    from document_extraction import (
        _map_summarize_window, _reduce_summaries,
        _extract_from_summary, _cross_window_dedup,
        extract_from_document, _split_into_windows,
    )
    from documents import load_document_text
    import knowledge_graph as kg

    test_path = _write_test_doc()
    t = _tag()
    statuses: list[str] = []

    def on_status(msg: str):
        statuses.append(msg)
        print(f"    📡 {msg}")

    try:
        full_text, title = load_document_text(test_path)

        # 14a. MAP step — summarize a window
        try:
            summary = _map_summarize_window(full_text[:6000], title, 1, 1)
            assert isinstance(summary, str) and len(summary) > 50
            record("PASS", f"14a: MAP summary — {len(summary)} chars")
        except Exception as e:
            record("FAIL", "14a: MAP step", str(e))
            return

        # 14b. REDUCE step — combine summaries into article
        try:
            windows = _split_into_windows(full_text)
            summaries = []
            for i, w in enumerate(windows, 1):
                s = _map_summarize_window(w, title, i, len(windows))
                if s:
                    summaries.append(s)
            article = _reduce_summaries(title, summaries)
            assert isinstance(article, str) and len(article) > 100
            print(f"    📄 Article: {len(article)} chars from {len(summaries)} summaries")
            record("PASS", f"14b: REDUCE article — {len(article)} chars")
        except Exception as e:
            record("FAIL", "14b: REDUCE step", str(e))
            return

        # 14c. Article quality — key concepts preserved through MAP-REDUCE
        try:
            article_lower = article.lower()
            expected_concepts = {
                "aurora": "project name",
                "quantum": "core topic",
                "vasquez": "principal investigator",
                "zurich": "location",
            }
            found = {k: v for k, v in expected_concepts.items() if k in article_lower}
            missing = {k: v for k, v in expected_concepts.items() if k not in article_lower}

            if len(found) >= 3:
                record("PASS", f"14c: article preserves {len(found)}/4 key concepts")
            else:
                record("WARN", f"14c: only {len(found)}/4 key concepts — missing: {list(missing.values())}")
        except Exception as e:
            record("FAIL", "14c: article quality", str(e))

        # 14d. EXTRACT step — pull entities from article
        try:
            extracted = _extract_from_summary(title, article)
            assert isinstance(extracted, list) and len(extracted) > 0
            entities = [e for e in extracted if e.get("category")]
            relations = [e for e in extracted if e.get("relation_type")]
            print(f"    📊 Extraction: {len(entities)} entities, {len(relations)} relations")
            for ent in entities:
                print(f"       [{ent.get('category')}] {ent.get('subject')} — {ent.get('content', '')[:60]}")
            record("PASS", f"14d: EXTRACT — {len(entities)} entities, {len(relations)} relations")
        except Exception as e:
            record("FAIL", "14d: EXTRACT step", str(e))
            return

        # 14e. Entity structure validation
        try:
            valid_cats = {"person", "preference", "fact", "event", "place", "project",
                          "organisation", "concept", "skill", "media"}
            for ent in entities:
                assert ent["category"] in valid_cats, f"Invalid category: {ent['category']}"
                assert ent.get("subject"), "Missing subject"
                assert ent.get("content"), "Missing content"
            record("PASS", "14e: all extracted entities have valid structure")
        except Exception as e:
            record("FAIL", "14e: entity structure", str(e))

        # 14f. Expected entities — LLM should find key people and orgs
        try:
            subjects_lower = {e.get("subject", "").lower() for e in entities}
            all_content = " ".join(e.get("content", "").lower() for e in entities)
            all_text = " ".join(subjects_lower) + " " + all_content

            expected_finds = {
                "vasquez": "PI (person)",
                "aurora": "project name",
                "zurich": "location or org",
            }
            found = {k: v for k, v in expected_finds.items() if k in all_text}
            for keyword, label in found.items():
                record("PASS", f"14f: LLM extracted '{keyword}' ({label})")
            for keyword, label in expected_finds.items():
                if keyword not in found:
                    record("WARN", f"14f: LLM missed '{keyword}' ({label})")
        except Exception as e:
            record("FAIL", "14f: expected entities", str(e))

        # 14g. _cross_window_dedup merges duplicates
        try:
            dupes = [
                {"category": "person", "subject": "Dr. Chen", "content": "A theorist", "aliases": ""},
                {"category": "person", "subject": "Dr. Chen", "content": "Expert in QEC", "aliases": "Marcus"},
                {"relation_type": "works_at", "source_subject": "Chen",
                 "target_subject": "ZIAC", "confidence": 0.9},
            ]
            deduped = _cross_window_dedup(dupes)
            chen_entities = [e for e in deduped if e.get("subject", "").lower() == "dr. chen"]
            rels = [e for e in deduped if e.get("relation_type")]

            assert len(chen_entities) == 1, f"Expected 1 merged Chen entity, got {len(chen_entities)}"
            merged_content = chen_entities[0].get("content", "")
            assert "theorist" in merged_content.lower() and "qec" in merged_content.lower(), \
                "Merged content should contain info from both entries"
            merged_aliases = chen_entities[0].get("aliases", "")
            assert "Marcus" in merged_aliases, f"Alias 'Marcus' not in merged: '{merged_aliases}'"
            assert len(rels) == 1, "Relation should pass through unchanged"
            record("PASS", "14g: _cross_window_dedup merges entities, preserves relations")
        except Exception as e:
            record("FAIL", "14g: _cross_window_dedup", str(e))

        # 14h. Full pipeline: extract_from_document end-to-end
        try:
            baseline = kg.count_entities()

            result = extract_from_document(test_path, _TEST_DOC_NAME, on_status=on_status)

            assert result["status"] == "completed", f"Status: {result['status']}, error: {result.get('error')}"
            assert result["entities_saved"] > 0, "Expected at least 1 entity saved"
            after = kg.count_entities()
            new_count = after - baseline

            print(f"    📊 Pipeline result: {result['entities_saved']} saved, {new_count} new entities")
            record("PASS", f"14h: full pipeline — {result['entities_saved']} entities, status=completed")
        except Exception as e:
            record("FAIL", "14h: full pipeline", str(e))
            return

        # 14i. Hub entity created (media type, source=document:...)
        try:
            source_label = f"document:{_TEST_DOC_NAME}"
            all_ents = kg.list_entities(limit=10000)
            hub_candidates = [
                e for e in all_ents
                if e.get("source") == source_label and e.get("entity_type") == "media"
            ]
            assert len(hub_candidates) >= 1, f"No media hub entity with source={source_label}"
            hub = hub_candidates[0]
            assert len(hub.get("description", "")) > 100, "Hub article too short"
            record("PASS", f"14i: hub entity created — '{hub['subject']}', {len(hub['description'])} char article")
        except Exception as e:
            record("FAIL", "14i: hub entity", str(e))

        # 14j. Extracted entities linked to hub via extracted_from
        try:
            hub_id = hub["id"]
            rels = kg.get_relations(hub_id, direction="both")
            extracted_from_rels = [r for r in rels if r.get("relation_type") == "extracted_from"]
            uploaded_rels = [r for r in rels if r.get("relation_type") == "uploaded"]

            if extracted_from_rels:
                record("PASS", f"14j: {len(extracted_from_rels)} entities linked via 'extracted_from'")
            else:
                record("WARN", "14j: no 'extracted_from' relations found")

            if uploaded_rels:
                record("PASS", f"14j+: User→hub 'uploaded' relation exists")
            else:
                record("WARN", "14j+: no 'uploaded' relation from User")
        except Exception as e:
            record("FAIL", "14j: extracted_from links", str(e))

        # 14k. Source tagging — all entities have document source
        try:
            doc_entities = [e for e in all_ents if e.get("source") == source_label]
            assert len(doc_entities) >= 2, f"Expected ≥2 entities with source={source_label}"
            record("PASS", f"14k: {len(doc_entities)} entities tagged source='{source_label}'")
        except Exception as e:
            record("FAIL", "14k: source tagging", str(e))

        # 14l. Status callbacks fired during pipeline
        try:
            assert len(statuses) >= 3, f"Expected ≥3 status callbacks, got {len(statuses)}"
            has_loading = any("loading" in s.lower() or "summariz" in s.lower() for s in statuses)
            has_extract = any("extract" in s.lower() for s in statuses)
            if has_loading and has_extract:
                record("PASS", f"14l: {len(statuses)} status callbacks (loading + extracting)")
            else:
                record("WARN", f"14l: {len(statuses)} callbacks but missing expected phases")
        except Exception as e:
            record("FAIL", "14l: status callbacks", str(e))

    finally:
        try:
            os.unlink(test_path)
        except OSError:
            pass
        # Don't cleanup yet — section 15 & 16 use these entities


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 15 · Document FAISS & Retrieval
# ═════════════════════════════════════════════════════════════════════════════

def section_15():
    print("\n" + "=" * 70)
    print("SECTION 15 · Document FAISS & Retrieval")
    print("=" * 70)

    from documents import (
        get_vector_store, load_and_vectorize_document,
        is_file_processed, load_processed_files,
    )

    test_path = _write_test_doc()

    try:
        # 15a. load_and_vectorize_document indexes chunks
        try:
            load_and_vectorize_document(test_path, skip_if_processed=False,
                                        display_name=_TEST_DOC_NAME)
            assert is_file_processed(_TEST_DOC_NAME)
            record("PASS", "15a: document vectorized and marked as processed")
        except Exception as e:
            record("FAIL", "15a: load_and_vectorize_document", str(e))
            return

        # 15b. Vector store contains chunks with correct metadata
        try:
            vs = get_vector_store()
            found_chunks = 0
            if hasattr(vs, "docstore") and hasattr(vs.docstore, "_dict"):
                for doc_id, doc in vs.docstore._dict.items():
                    if getattr(doc, "metadata", {}).get("source") == _TEST_DOC_NAME:
                        found_chunks += 1
            assert found_chunks > 0, "No chunks found with test document source"
            record("PASS", f"15b: {found_chunks} chunks in FAISS with source='{_TEST_DOC_NAME}'")
        except Exception as e:
            record("FAIL", "15b: chunk metadata", str(e))

        # 15c. Similarity search returns relevant chunks
        try:
            results = vs.similarity_search("quantum error correction algorithms", k=5)
            test_results = [r for r in results
                            if getattr(r, "metadata", {}).get("source") == _TEST_DOC_NAME]
            if test_results:
                snippet = test_results[0].page_content[:100]
                record("PASS", f"15c: similarity search found {len(test_results)} chunks — '{snippet}...'")
            else:
                record("WARN", "15c: similarity search didn't return test doc chunks in top 5")
        except Exception as e:
            record("FAIL", "15c: similarity search", str(e))

        # 15d. Similarity search with score
        try:
            results_with_score = vs.similarity_search_with_score(
                "Dr. Elena Vasquez quantum research", k=5,
            )
            test_results = [
                (doc, score) for doc, score in results_with_score
                if getattr(doc, "metadata", {}).get("source") == _TEST_DOC_NAME
            ]
            if test_results:
                best_score = test_results[0][1]
                record("PASS", f"15d: scored search — {len(test_results)} hits, best score={best_score:.4f}")
            else:
                record("WARN", "15d: scored search didn't return test doc chunks")
        except Exception as e:
            record("FAIL", "15d: scored search", str(e))

        # 15e. skip_if_processed prevents re-indexing
        try:
            chunk_count_before = 0
            if hasattr(vs, "docstore") and hasattr(vs.docstore, "_dict"):
                chunk_count_before = sum(
                    1 for doc in vs.docstore._dict.values()
                    if getattr(doc, "metadata", {}).get("source") == _TEST_DOC_NAME
                )
            load_and_vectorize_document(test_path, skip_if_processed=True,
                                        display_name=_TEST_DOC_NAME)
            chunk_count_after = sum(
                1 for doc in vs.docstore._dict.values()
                if getattr(doc, "metadata", {}).get("source") == _TEST_DOC_NAME
            )
            if chunk_count_after == chunk_count_before:
                record("PASS", f"15e: skip_if_processed prevented duplicate indexing ({chunk_count_after} chunks)")
            else:
                record("FAIL", f"15e: duplicate indexing — {chunk_count_before}→{chunk_count_after}")
        except Exception as e:
            record("FAIL", "15e: skip_if_processed", str(e))

        # 15f. Processed files list
        try:
            processed = load_processed_files()
            assert _TEST_DOC_NAME in processed
            record("PASS", f"15f: processed_files contains test doc ({len(processed)} total)")
        except Exception as e:
            record("FAIL", "15f: processed files", str(e))

        # 15g. Retriever interface (used by agent DocumentsTool)
        try:
            retriever = vs.as_retriever(search_kwargs={"k": 3})
            docs = retriever.invoke("quantum computing Zurich")
            assert isinstance(docs, list)
            test_docs = [d for d in docs
                         if getattr(d, "metadata", {}).get("source") == _TEST_DOC_NAME]
            if test_docs:
                record("PASS", f"15g: retriever returns {len(test_docs)} relevant chunks")
            else:
                record("WARN", "15g: retriever didn't return test doc in top 3")
        except Exception as e:
            record("FAIL", "15g: retriever interface", str(e))

    finally:
        try:
            os.unlink(test_path)
        except OSError:
            pass


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 16 · Document Cleanup & Source Deletion
# ═════════════════════════════════════════════════════════════════════════════

def section_16():
    print("\n" + "=" * 70)
    print("SECTION 16 · Document Cleanup & Source Deletion")
    print("=" * 70)

    from documents import (
        get_vector_store, remove_document,
        is_file_processed, load_processed_files,
    )
    import knowledge_graph as kg

    source_label = f"document:{_TEST_DOC_NAME}"

    # 16a. Count entities with document source before cleanup
    try:
        all_ents = kg.list_entities(limit=10000)
        doc_entities_before = [e for e in all_ents if e.get("source") == source_label]
        doc_count = len(doc_entities_before)
        assert doc_count > 0, "No document entities exist to test cleanup"
        record("PASS", f"16a: {doc_count} entities with source='{source_label}' before cleanup")
    except Exception as e:
        record("FAIL", "16a: pre-cleanup count", str(e))
        return

    # 16b. Count relations with document source
    try:
        conn = __import__("sqlite3").connect(kg.DB_PATH)
        conn.row_factory = __import__("sqlite3").Row
        doc_rels = conn.execute(
            "SELECT COUNT(*) FROM relations WHERE source = ?", (source_label,)
        ).fetchone()[0]
        conn.close()
        record("PASS", f"16b: {doc_rels} relations with source='{source_label}'")
    except Exception as e:
        record("FAIL", "16b: relation count", str(e))

    # 16c. delete_entities_by_source removes all document entities
    try:
        deleted = kg.delete_entities_by_source(source_label)
        assert deleted == doc_count, f"Deleted {deleted}, expected {doc_count}"
        record("PASS", f"16c: delete_entities_by_source removed {deleted} entities")
    except Exception as e:
        record("FAIL", "16c: delete_entities_by_source", str(e))

    # 16d. Entities are actually gone from DB
    try:
        remaining = [
            e for e in kg.list_entities(limit=10000)
            if e.get("source") == source_label
        ]
        assert len(remaining) == 0, f"{len(remaining)} entities still remain"
        record("PASS", "16d: 0 document entities remain in DB after deletion")
    except Exception as e:
        record("FAIL", "16d: entity cleanup verification", str(e))

    # 16e. Relations with document source also cleaned up
    try:
        conn = __import__("sqlite3").connect(kg.DB_PATH)
        remaining_rels = conn.execute(
            "SELECT COUNT(*) FROM relations WHERE source = ?", (source_label,)
        ).fetchone()[0]
        conn.close()
        if remaining_rels == 0:
            record("PASS", "16e: 0 document relations remain after deletion")
        else:
            record("WARN", f"16e: {remaining_rels} orphan relations remain")
    except Exception as e:
        record("FAIL", "16e: relation cleanup", str(e))

    # 16f. NetworkX graph nodes removed
    try:
        g = kg._ensure_graph()
        doc_ids = {e["id"] for e in doc_entities_before}
        still_in_graph = {eid for eid in doc_ids if eid in g}
        if not still_in_graph:
            record("PASS", "16f: deleted entities removed from NetworkX graph")
        else:
            record("FAIL", f"16f: {len(still_in_graph)} deleted nodes still in graph")
    except Exception as e:
        record("FAIL", "16f: graph cleanup", str(e))

    # 16g. remove_document cleans FAISS chunks
    try:
        vs = get_vector_store()
        # Count chunks before
        chunks_before = 0
        if hasattr(vs, "docstore") and hasattr(vs.docstore, "_dict"):
            chunks_before = sum(
                1 for doc in vs.docstore._dict.values()
                if getattr(doc, "metadata", {}).get("source") == _TEST_DOC_NAME
            )

        removed = remove_document(_TEST_DOC_NAME)

        chunks_after = 0
        if hasattr(vs, "docstore") and hasattr(vs.docstore, "_dict"):
            chunks_after = sum(
                1 for doc in vs.docstore._dict.values()
                if getattr(doc, "metadata", {}).get("source") == _TEST_DOC_NAME
            )

        if chunks_after == 0 and chunks_before > 0:
            record("PASS", f"16g: remove_document cleared {chunks_before} FAISS chunks")
        elif chunks_before == 0:
            record("WARN", "16g: no FAISS chunks existed (section 15 may not have run)")
        else:
            record("FAIL", f"16g: {chunks_after} FAISS chunks still remain")
    except Exception as e:
        record("FAIL", "16g: FAISS chunk removal", str(e))

    # 16h. Processed files list updated
    try:
        if not is_file_processed(_TEST_DOC_NAME):
            record("PASS", "16h: test doc removed from processed files list")
        else:
            record("FAIL", "16h: test doc still in processed files list")
    except Exception as e:
        record("FAIL", "16h: processed files cleanup", str(e))

    # 16i. delete_entities_by_source_prefix
    try:
        # Create a few entities with a test document source prefix
        t = _tag()
        test_sources = [f"document:{PREFIX}docA_{t}.pdf", f"document:{PREFIX}docB_{t}.pdf"]
        created = []
        for src in test_sources:
            e = kg.save_entity("fact", f"{PREFIX}PrefixTest_{t}_{src[-10:]}",
                               "Test entity for prefix deletion", source=src)
            created.append(e["id"])

        deleted = kg.delete_entities_by_source_prefix(f"document:{PREFIX}")
        assert deleted >= len(created), f"Deleted {deleted}, expected ≥{len(created)}"

        # Verify they're gone
        for eid in created:
            assert kg.get_entity(eid) is None, f"Entity {eid} still exists"

        record("PASS", f"16i: delete_entities_by_source_prefix removed {deleted} entities")
    except Exception as e:
        record("FAIL", "16i: source prefix deletion", str(e))

    # 16j. delete_entities_by_source returns 0 for non-existent source
    try:
        deleted = kg.delete_entities_by_source("document:nonexistent_file_12345.pdf")
        assert deleted == 0
        record("PASS", "16j: delete_entities_by_source returns 0 for missing source")
    except Exception as e:
        record("FAIL", "16j: empty deletion", str(e))

if __name__ == "__main__":
    print("\n" + "=" * 70)
    print("  THOTH MEMORY SYSTEM — END-TO-END INTEGRATION TESTS")
    print("=" * 70)
    if _fast_mode:
        print("  ⚡ FAST MODE — skipping LLM-dependent tests")
    if _keep_data:
        print("  📌 KEEP MODE — test entities will NOT be cleaned up")
    if _section_filter:
        print(f"  🎯 Running section {_section_filter} only")
    print()

    start_time = time.time()

    sections = [
        (1, section_1),
        (2, section_2),
        (3, section_3),
        (4, section_4),
        (5, section_5),
        (6, section_6),
        (7, section_7),
        (8, section_8),
        (9, section_9),
        (10, section_10),
        (11, section_11),
        (12, section_12),
        (13, section_13),
        (14, section_14),
        (15, section_15),
        (16, section_16),
    ]

    for num, func in sections:
        if not _should_run(num):
            continue
        try:
            result = func()
            # Section 1 returns bool — if False, abort
            if num == 1 and result is False:
                print("\n  ⛔ Prerequisites failed — aborting remaining tests")
                break
        except Exception:
            print(f"\n  💥 Section {num} crashed:")
            traceback.print_exc()
            record("FAIL", f"section-{num}-crash", traceback.format_exc()[-200:])

    elapsed = time.time() - start_time

    # ── Summary ──────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    total = _pass + _fail + _warn + _skip
    print(f"  ✅ PASS: {_pass}")
    print(f"  ❌ FAIL: {_fail}")
    print(f"  ⚠️  WARN: {_warn}")
    if _skip:
        print(f"  ⏭️  SKIP: {_skip}")
    print(f"  Total: {total}  ({elapsed:.1f}s)")

    if _fail > 0:
        print("\nFAILED TESTS:")
        for status, label, detail in _results:
            if status == "FAIL":
                line = f"  ❌ {label}"
                if detail:
                    line += f": {detail[:120]}"
                print(line)

    if _warn > 0:
        print("\nWARNINGS:")
        for status, label, detail in _results:
            if status == "WARN":
                line = f"  ⚠️  {label}"
                if detail:
                    line += f": {detail[:120]}"
                print(line)

    print("=" * 70)

    if _fail > 0:
        print(f"\n⛔ {_fail} TEST(S) FAILED")
        sys.exit(1)
    else:
        print(f"\n✅ ALL {_pass} TESTS PASSED" + (f" ({_warn} warnings)" if _warn else ""))
        sys.exit(0)
