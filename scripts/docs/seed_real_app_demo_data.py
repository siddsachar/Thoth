"""Seed deterministic safe data for real Row-Bot docs screenshots."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed real app demo data for docs capture")
    parser.add_argument("--data-dir", required=True, help="Temporary ROW_BOT_DATA_DIR to seed")
    parser.add_argument("--scenario", default="full", help="Demo scenario to seed")
    return parser.parse_args()


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _seed_app_config(data_dir: Path, *, first_run: bool) -> None:
    if first_run:
        config = {
            "onboarding_seen": False,
            "setup_complete": False,
            "window_mode": "browser",
        }
    else:
        config = {
            "onboarding_seen": True,
            "setup_complete": True,
            "onboarding_version": 3,
            "onboarding_profile": ["chat", "research", "workflows", "designer", "developer"],
            "onboarding_completed_steps": ["models", "knowledge", "workflows"],
            "onboarding_skipped_steps": [],
            "onboarding_dismissed_home_card": False,
            "window_mode": "browser",
        }
    _write_json(data_dir / "app_config.json", config)


def _seed_threads_sqlite(data_dir: Path, state: dict) -> None:
    db = data_dir / "threads.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    now = datetime(2026, 6, 18, 9, 0, tzinfo=timezone.utc).isoformat()
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS thread_meta "
            "(thread_id TEXT PRIMARY KEY, name TEXT, created_at TEXT, updated_at TEXT, "
            "model_override TEXT DEFAULT '', skills_override TEXT DEFAULT '', summary TEXT DEFAULT '', "
            "summary_msg_count INTEGER DEFAULT 0, project_id TEXT DEFAULT '', thread_type TEXT DEFAULT '', "
            "developer_workspace_id TEXT DEFAULT '', approval_mode TEXT DEFAULT '', name_source TEXT DEFAULT '', "
            "agent_profile_id TEXT DEFAULT '', agent_profile_slug TEXT DEFAULT '')"
        )
        for row in state.get("threads", []):
            thread_id = str(row.get("id") or "")
            name = str(row.get("name") or thread_id)
            kind = str(row.get("kind") or "chat")
            if not thread_id:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO thread_meta "
                "(thread_id, name, created_at, updated_at, model_override, thread_type, approval_mode, name_source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    thread_id,
                    name,
                    now,
                    now,
                    str(state.get("model") or ""),
                    "workflow" if kind == "workflow" else "",
                    "approve",
                    "manual",
                ),
            )
        conn.commit()


def _seed_checkpoint_messages(state: dict) -> None:
    try:
        from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
        from row_bot.threads import append_checkpoint_messages
    except Exception:
        return
    messages = []
    for item in state.get("messages", []):
        role = item.get("role")
        content = str(item.get("content") or "")
        if role == "user":
            messages.append(HumanMessage(content=content))
            continue
        for tool in item.get("tool_results", []) or []:
            messages.append(
                ToolMessage(
                    content=str(tool.get("content") or ""),
                    name=str(tool.get("name") or "tool"),
                    tool_call_id=str(tool.get("name") or "tool").replace(".", "_"),
                )
            )
        messages.append(AIMessage(content=content))
    append_checkpoint_messages(str(state.get("thread_id") or "docs-demo-chat"), messages)


def _seed_demo_files(data_dir: Path, state: dict) -> None:
    workspace = data_dir / "docs-demo-workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "launch-checklist.md").write_text(
        "# Demo Launch Checklist\n\n"
        "- Confirm local model path.\n"
        "- Review approvals before file writes.\n"
        "- Keep channels disabled until credentials are added.\n",
        encoding="utf-8",
    )
    (workspace / "support-faq.md").write_text(
        "# Support FAQ\n\nAll demo records use example.com addresses and fake provider states.\n",
        encoding="utf-8",
    )
    docs_dir = data_dir / "documents"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "Launch brief.txt").write_text(
        "Demo launch brief for Row-Bot public documentation screenshots.\n",
        encoding="utf-8",
    )
    _write_json(data_dir / "provider_catalog_cache.json", {"providers": state.get("providers", [])})
    _write_json(data_dir / "docs_demo_review_state.json", state)


def _seed_profiles_goals_and_agents(state: dict) -> None:
    from row_bot.agent_profiles import save_agent_profile
    from row_bot.agent_runs import create_agent_run, create_agent_run_edge, finish_agent_run, record_agent_run_progress
    from row_bot.goals import start_goal

    for profile in state.get("profiles", []):
        save_agent_profile(
            id=profile["id"],
            slug=profile["slug"],
            display_name=profile["display_name"],
            description=profile["description"],
            capability=profile["capability"],
            enabled=True,
            tool_policy_json={"allow": ["documents", "memory"], "deny": []},
            skill_policy_json={"allow": ["research"], "deny": []},
            context_policy_json={"mode": "summary"},
            workspace_policy_json={"mode": "read_only", "lock": False},
            model_policy_json={"model": "ollama/llama3.1:8b"},
            approval_policy_json={"mode": "approve"},
            output_contract_json={"summary": True, "tests": False},
            limits_json={"max_turns": 8, "timeout_seconds": 600},
        )

    goal = start_goal(
        state["goal"]["thread_id"],
        state["goal"]["objective"],
        max_turns=state["goal"]["max_turns"],
    )
    with sqlite3.connect(Path(os.environ["ROW_BOT_DATA_DIR"]) / "tasks.db") as conn:
        conn.execute(
            "UPDATE thread_goals SET turns_used = ?, last_progress = ?, blockers_json = ?, "
            "evidence_json = ?, last_reason = ? WHERE id = ?",
            (
                state["goal"]["turns_used"],
                state["goal"]["progress"][-1],
                json.dumps(state["goal"]["blockers"]),
                json.dumps(state["goal"]["progress"]),
                "Checklist draft is ready for review.",
                goal["id"],
            ),
        )

    parent = state["agents"][0]
    create_agent_run(
        run_id=parent["id"],
        kind=parent["kind"],
        status=parent["status"],
        parent_thread_id=parent["thread_id"],
        thread_id=parent["thread_id"],
        display_name=parent["display_name"],
        prompt="Coordinate the fictional launch review.",
        profile_id="docs-profile-project",
        summary=parent["summary"],
        max_turns=6,
        turns_used=2,
    )
    for child in state["agents"][1:]:
        create_agent_run(
            run_id=child["id"],
            kind=child["kind"],
            status="running" if child["status"] == "completed" else child["status"],
            parent_run_id=parent["id"],
            root_run_id=parent["id"],
            parent_thread_id=parent["thread_id"],
            thread_id=child["thread_id"],
            display_name=child["display_name"],
            prompt=child["summary"],
            profile_id="docs-profile-research",
            max_turns=4,
        )
        create_agent_run_edge(parent["id"], child["id"])
        record_agent_run_progress(child["id"], steps_done=2 if child["status"] == "completed" else 1, steps_total=2)
        if child["status"] == "completed":
            finish_agent_run(child["id"], "completed", summary=child["summary"])


def _seed_workflows(state: dict) -> None:
    from row_bot import tasks

    brief_id = tasks.create_task(
        "Morning Brief",
        prompts=["Summarise the two local demo documents.", "Prepare a concise morning brief."],
        description="A safe scheduled summary from local demo sources.",
        schedule="0 8 * * 1-5",
        safety_mode="approve",
        channels=[],
    )
    approval_id = tasks.create_task(
        "Launch Summary",
        description="Draft then approve a fictional launch summary.",
        steps=[
            {"id": "draft", "type": "prompt", "prompt": "Draft the launch summary.", "next": "review"},
            {"id": "review", "type": "approval", "message": "Approve writing the demo summary?", "next": "publish"},
            {"id": "publish", "type": "prompt", "prompt": "Write the approved summary to the demo workspace."},
        ],
        advanced_mode=True,
        safety_mode="approve",
        channels=[],
    )
    research_id = tasks.create_task(
        "Research Digest",
        prompts=["Search the local demo knowledge graph.", "Summarise matching evidence."],
        description="A manual research workflow with a safe retry example.",
        safety_mode="block",
        channels=None,
    )
    state["workflows"][0]["id"] = brief_id
    state["workflows"][1]["id"] = approval_id
    state["workflows"][2]["id"] = research_id

    complete_run = tasks._record_run_start(brief_id, "docs-workflow-complete", 2, "Morning Brief", "bolt")
    tasks._update_run_progress(complete_run, 2)
    tasks._finish_run(complete_run, "completed", "Demo brief created successfully.")
    failed_run = tasks._record_run_start(research_id, "docs-workflow-failed", 2, "Research Digest", "science")
    tasks._update_run_progress(failed_run, 1)
    tasks._finish_run(failed_run, "failed", "Demo source unavailable; safe to retry.")
    pending_run = tasks._record_run_start(approval_id, "docs-workflow-approval", 3, "Launch Summary", "approval")
    tasks._update_run_progress(pending_run, 1)
    tasks.create_approval_request(
        pending_run,
        approval_id,
        "review",
        "Approve writing the fictional launch summary to the demo workspace?",
        timeout_minutes=60,
        source_label="Launch Summary",
        source_thread_id="docs-workflow-approval",
        approval_payload_json={"action": "write demo summary", "path": "%ROW_BOT_DATA_DIR%/docs-demo-workspace/summary.md"},
    )


def _seed_knowledge_and_wiki(data_dir: Path, state: dict) -> None:
    from row_bot import knowledge_graph as kg
    from row_bot import wiki_vault

    previous = kg._skip_reindex
    kg._skip_reindex = True
    by_declared_id: dict[str, str] = {}
    try:
        for item in state["knowledge"]["entities"]:
            entity = kg.save_entity(
                item["type"],
                item["subject"],
                item["description"],
                tags="demo,documentation",
                properties={"provenance": "docs demo fixture", "reviewed": True},
                source="docs-demo",
            )
            by_declared_id[item["id"]] = entity["id"]
        for source_id, target_id, relation in state["knowledge"]["relations"]:
            kg.add_relation(
                by_declared_id[source_id],
                by_declared_id[target_id],
                relation,
                confidence=0.95,
                properties={"provenance": "docs demo fixture"},
                source="docs-demo",
            )
    finally:
        kg._skip_reindex = previous
    wiki_vault.set_vault_path(str(data_dir / "wiki-vault"))
    wiki_vault.set_enabled(True)
    wiki_vault.rebuild_vault()
    _write_json(
        data_dir / "dream_config.json",
        {"enabled": True, "window_start": 1, "window_end": 5, "last_run": "2026-06-18"},
    )
    _write_json(
        data_dir / "dream_journal.json",
        [
            {"timestamp": "2026-06-18T02:10:00Z", "phase": "review", "message": "Reviewed four demo entities."},
            {"timestamp": "2026-06-18T02:10:01Z", "phase": "complete", "message": "No duplicates required merging."},
        ],
    )


def _seed_developer_workspace(data_dir: Path, state: dict) -> None:
    from row_bot.developer.storage import add_or_update_local_workspace

    repo = data_dir / "docs-demo-workspace" / "demo-release-notes"
    repo.mkdir(parents=True, exist_ok=True)
    readme = repo / "README.md"
    readme.write_text("# Demo release notes\n\nA fictional local repository for documentation capture.\n", encoding="utf-8")
    test_file = repo / "test_demo.py"
    test_file.write_text("def test_demo_checklist():\n    assert ['review', 'approve', 'write'][-1] == 'write'\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Row-Bot Docs Demo"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "docs-demo@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "add", "README.md", "test_demo.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "Seed fictional demo project"], cwd=repo, check=True)
    subprocess.run(["git", "switch", "-q", "-c", "docs/demo-checklist"], cwd=repo, check=True)
    readme.write_text(
        "# Demo release notes\n\nA fictional local repository for documentation capture.\n\n"
        "TODO: Add the recovery note after review.\n",
        encoding="utf-8",
    )
    workspace = add_or_update_local_workspace(str(repo))
    state["developer"]["workspace_id"] = workspace.id
    _write_json(
        data_dir / "developer" / "docs_demo_inspector.json",
        {"todos": [{"label": state["developer"]["todo"], "status": "in_progress"}], "test_output": state["developer"]["test_output"]},
    )


def _seed_designer_project(state: dict) -> None:
    from row_bot.designer.state import BrandConfig, DesignerAsset, DesignerPage, DesignerProject, ProjectBrief
    from row_bot.designer.storage import save_project

    project = DesignerProject(
        id=state["designer"]["project_id"],
        name=state["designer"]["name"],
        mode="deck",
        template_id="clean-presentation",
        pages=[
            DesignerPage(
                title="Welcome",
                notes="Open with the workshop purpose and a friendly local-first message.",
                html="<section style='height:100%;display:grid;place-content:center;background:#0f172a;color:#f8fafc;font-family:Inter'><div><p style='color:#60a5fa'>COMMUNITY WORKSHOP</p><h1 style='font-size:64px'>Build a safer local AI workflow</h1><p style='font-size:26px;color:#cbd5e1'>A fictional Row-Bot Designer project</p></div></section>",
            ),
            DesignerPage(
                title="Review steps",
                notes="Explain review, approval, and recovery.",
                html="<section style='height:100%;padding:72px;background:#111827;color:#f9fafb;font-family:Inter'><h1>Three review steps</h1><ol style='font-size:34px;line-height:1.8'><li>Check local sources</li><li>Approve consequential actions</li><li>Keep a recovery path</li></ol></section>",
            ),
        ],
        brand=BrandConfig(primary_color="#2563EB", accent_color="#22C55E"),
        brief=ProjectBrief(output_type="Presentation", audience="Community organisers", tone="Clear and welcoming", length="2 slides"),
        assets=[DesignerAsset(id="asset-demo-chart", kind="chart", label="Workshop readiness", mime_type="application/json", filename="readiness-chart.json")],
        thread_id="docs-designer-thread",
    )
    save_project(project)


def _seed_integrations_and_mobile(data_dir: Path, state: dict) -> None:
    from row_bot.mcp_client.config import save_config
    from row_bot.mobile.store import MobileAuthStore
    from row_bot.providers.config import load_provider_config, save_provider_config
    from row_bot.providers.custom import normalize_custom_endpoint

    provider_cfg = load_provider_config()
    provider_cfg["custom_endpoints"] = [
        normalize_custom_endpoint(
            {
                "id": "docs-local-endpoint",
                "name": "Demo Local Endpoint",
                "base_url": "http://127.0.0.1:11435/v1",
                "profile": "openai_compatible",
                "transport": "openai_chat",
                "auth_required": False,
                "execution_location": "local",
                "risk_label": "local_private",
                "enabled": True,
                "capability_probe": False,
                "models": [{"id": "demo-chat", "model_id": "demo-chat", "display_name": "Demo Chat"}],
            }
        )
    ]
    save_provider_config(provider_cfg)

    save_config(
        {
            "enabled": False,
            "marketplace": {"enabled": True, "sources": ["official"]},
            "servers": {
                "Demo GitHub MCP": {
                    "enabled": False,
                    "transport": "stdio",
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-github"],
                    "trust_level": "standard",
                    "source": {"catalog": "docs-demo"},
                },
                "Demo Browser MCP": {
                    "enabled": False,
                    "transport": "stdio",
                    "command": "npx",
                    "args": ["-y", "@playwright/mcp"],
                    "trust_level": "standard",
                    "source": {"catalog": "docs-demo"},
                },
            },
        }
    )

    plugin_dir = data_dir / "installed_plugins" / "docs-demo-crm"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    _write_json(
        plugin_dir / "plugin.json",
        {
            "schema_version": 2,
            "id": "docs-demo-crm",
            "name": "Demo CRM Lookup",
            "version": "1.0.0",
            "min_row_bot_version": "4.7.1",
            "author": {"name": "Row-Bot Docs Demo"},
            "description": "An inert fictional plugin used only for documentation capture.",
            "provides": {"native_tools": [], "mcp_servers": [], "channels": [], "skills": []},
            "permissions": [],
            "settings": {},
            "secrets": {},
            "auth": {},
            "health_checks": [],
        },
    )

    store = MobileAuthStore(data_dir / "mobile.db")
    store.create_device(
        device_id="docs-demo-android",
        display_name=state["mobile"]["device_name"],
        token_hash="demo-token-hash-not-a-secret",
        token_salt="demo-token-salt",
        user_agent="Android Demo Browser",
        paired_from="192.0.2.10",
        access_mode="trusted_lan",
        now=datetime(2026, 6, 18, 8, 45, tzinfo=timezone.utc),
    )
    for index, event in enumerate(state["mobile"]["events"]):
        store.log_event(
            event.lower().replace(" ", "_"),
            event_id=f"docs-mobile-event-{index}",
            device_id="docs-demo-android",
            ip="192.0.2.10",
            user_agent="Android Demo Browser",
            detail={"display": event, "source": "docs demo"},
            now=datetime(2026, 6, 18, 8, 46 + index, tzinfo=timezone.utc),
        )


def main() -> int:
    args = _parse_args()
    data_dir = Path(args.data_dir).resolve()
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    os.environ["ROW_BOT_DATA_DIR"] = str(data_dir)
    os.environ.setdefault("ROW_BOT_DOCS_CAPTURE", "1")
    os.environ.setdefault("ROW_BOT_DOCS_FIXED_NOW", "2026-06-18T09:00:00Z")

    from row_bot.docs_capture import (
        SCENARIOS,
        default_docs_capture_demo_state,
        scan_demo_data_safety,
        write_docs_capture_demo_state,
    )

    scenario = str(args.scenario or "full")
    if scenario not in SCENARIOS:
        raise SystemExit(f"Unknown scenario {scenario!r}. Expected one of: {', '.join(sorted(SCENARIOS))}")
    first_run = scenario == "first-run"
    data_dir.mkdir(parents=True, exist_ok=True)
    state_path = write_docs_capture_demo_state(data_dir, scenario=scenario)
    state = default_docs_capture_demo_state()
    state["scenario"] = scenario
    _seed_app_config(data_dir, first_run=first_run)
    if not first_run:
        _seed_threads_sqlite(data_dir, state)
        _seed_checkpoint_messages(state)
        _seed_demo_files(data_dir, state)
        _seed_profiles_goals_and_agents(state)
        _seed_workflows(state)
        _seed_knowledge_and_wiki(data_dir, state)
        _seed_developer_workspace(data_dir, state)
        _seed_designer_project(state)
        _seed_integrations_and_mobile(data_dir, state)
        state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    errors = scan_demo_data_safety(data_dir)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print(f"Seeded real docs demo data in {data_dir}")
    print(f"Wrote demo state to {state_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
