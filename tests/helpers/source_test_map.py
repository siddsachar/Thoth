from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path


def normalize_repo_path(path: str | Path) -> str:
    return str(path).replace("\\", "/").lstrip("./")


@dataclass(frozen=True)
class SourceTestRule:
    name: str
    patterns: tuple[str, ...]
    test_paths: tuple[str, ...]
    reason: str

    def matches(self, path: str | Path) -> bool:
        normalized = normalize_repo_path(path)
        return any(fnmatch(normalized, pattern) for pattern in self.patterns)


SOURCE_TEST_RULES: tuple[SourceTestRule, ...] = (
    SourceTestRule(
        "prompt_context",
        (
            "src/row_bot/agent.py",
            "src/row_bot/prompts.py",
            "src/row_bot/self_knowledge.py",
            "src/row_bot/prompt_context.py",
            "src/row_bot/prompt_cache.py",
        ),
        (
            "tests/subsystem/agents",
            "tests/subsystem/providers/test_prompt_cache_payloads.py",
            "tests/subsystem/providers/test_prompt_cache_metrics.py",
            "tests/test_provider_runtime.py",
            "tests/test_chat_only_runtime.py",
            "tests/test_agent_runtime_profiles.py",
            "tests/test_agent_tool_filtering.py",
            "tests/test_memory_recall_uplift.py",
            "tests/test_skills_activation.py",
            "tests/test_slash_commands.py",
        ),
        "Prompt assembly changes need stable/ephemeral context, provider cache marker, Chat Only, memory, and skill regressions.",
    ),
    SourceTestRule(
        "providers",
        (
            "src/row_bot/models.py",
            "src/row_bot/providers/**",
            "src/row_bot/tools/image_gen_tool.py",
            "src/row_bot/tools/video_gen_tool.py",
        ),
        (
            "tests/contracts/test_provider_contract.py",
            "tests/subsystem/providers",
            "tests/test_provider_catalog.py",
            "tests/test_provider_runtime.py",
            "tests/test_provider_selection.py",
            "tests/test_provider_resolution.py",
            "tests/test_xai_media.py",
        ),
        "Provider and media runtime changes need fake-provider contracts plus focused provider regressions.",
    ),
    SourceTestRule(
        "tools_memory",
        ("src/row_bot/tools/memory_tool.py",),
        (
            "tests/subsystem/tools",
            "tests/subsystem/knowledge_graph",
            "tests/test_memory_recall_uplift.py",
            "tests/test_memory_evolution.py",
        ),
        "Memory tool changes need graph-backed tool contracts and deterministic memory regressions.",
    ),
    SourceTestRule(
        "tools_calendar",
        ("src/row_bot/tools/calendar_tool.py",),
        ("tests/subsystem/tools/test_calendar_tool_contracts.py",),
        "Calendar changes need fake-service concurrency, idempotency, retry, and mutation contracts.",
    ),
    SourceTestRule(
        "tools_gmail",
        ("src/row_bot/tools/gmail_tool.py",),
        ("tests/subsystem/tools/test_gmail_tool_contracts.py",),
        "Gmail changes need deterministic input-schema and provider-conversion contracts.",
    ),
    SourceTestRule(
        "startup_runtime",
        (
            "src/row_bot/app_port.py",
            "src/row_bot/app.py",
            "src/row_bot/launcher.py",
            "src/row_bot/startup_diagnostics.py",
            "src/row_bot/ui/state.py",
            "src/row_bot/tools/vision_tool.py",
            "src/row_bot/vision_runtime.py",
            "scripts/smoke_app.py",
        ),
        (
            "tests/test_app_port.py",
            "tests/test_startup_hardening.py",
            "tests/test_ui_performance.py",
            "tests/subsystem/mobile",
            "tests/integration/mobile",
        ),
        "Startup host, app shell, and smoke harness changes need import, readiness, UI performance, and mobile exposure regressions.",
    ),
    SourceTestRule(
        "remote_access_server",
        (
            "src/row_bot/access/**",
            "src/row_bot/ui/access_context.py",
            "src/row_bot/ui/remote_access_settings.py",
            "src/row_bot/ui/settings.py",
            "src/row_bot/voice/browser_local.py",
            "src/row_bot/voice/browser_client.py",
            "src/row_bot/voice/coordinator.py",
            "src/row_bot/voice/output_controller.py",
            "src/row_bot/tts.py",
            "src/row_bot/secret_store.py",
            "src/row_bot/providers/auth_store.py",
            "src/row_bot/channels/auth_store.py",
            "deploy/**",
            "dockerignore",
            "scripts/smoke_remote_access.py",
        ),
        (
            "tests/subsystem/access",
            "tests/integration/access",
            "tests/subsystem/mobile",
            "tests/integration/mobile",
            "tests/contracts/installers/test_remote_access_deployment_contract.py",
            "tests/test_browser_local_voice.py",
            "tests/test_secret_store.py",
            "tests/test_provider_auth_store.py",
            "tests/test_channel_auth_store.py",
        ),
        "Remote access, server policy, deployment examples, and smoke changes need access security, legacy-mobile compatibility, and deployment contracts.",
    ),
    SourceTestRule(
        "mobile_owner_access",
        (
            "src/row_bot/mobile/**",
            "src/row_bot/ui/mobile*.py",
            "src/row_bot/ui/settings.py",
        ),
        (
            "tests/subsystem/mobile",
            "tests/integration/mobile",
        ),
        "Compact owner UI changes need auth, access-gate, pairing, PWA, Settings, and route coverage.",
    ),
    SourceTestRule(
        "chat_composer",
        ("src/row_bot/ui/chat_composer_extras.py",),
        (
            "tests/subsystem/mobile",
            "tests/test_slash_commands.py",
            "tests/test_skills_activation.py",
        ),
        "Shared composer extras affect slash commands, Smart Skills, and mobile chat controls.",
    ),
    SourceTestRule(
        "live_control",
        (
            "src/row_bot/ui/live_control.py",
            "src/row_bot/ui/chat.py",
            "src/row_bot/ui/chat_components.py",
            "src/row_bot/ui/mobile_chat.py",
        ),
        (
            "tests/subsystem/computer_use",
            "tests/integration/computer_use",
            "tests/subsystem/browser",
            "tests/test_browser_cancellation.py",
            "tests/test_chat_tool_trace_ui.py",
            "tests/subsystem/mobile",
        ),
        "Persistent Browser/Computer controls affect desktop, shared, and mobile chat shells plus engine-specific cancellation and privacy.",
    ),
    SourceTestRule(
        "channels",
        ("src/row_bot/channels/**",),
        (
            "tests/contracts/test_channel_contract.py",
            "tests/subsystem/channels",
            "tests/test_channel_goal_runtime.py",
            "tests/test_channel_streaming.py",
        ),
        "Channel adapter changes need fake-channel contracts and channel runtime regressions.",
    ),
    SourceTestRule(
        "buddy_lifecycle",
        (
            "src/row_bot/buddy/brain.py",
            "src/row_bot/buddy/events.py",
        ),
        (
            "tests/test_buddy_core.py",
            "tests/test_generation_stop.py",
        ),
        "Buddy lifecycle events must clear active generation and tool lanes exactly once when a generation stops.",
    ),
    SourceTestRule(
        "approvals",
        (
            "src/row_bot/approval_messages.py",
            "src/row_bot/tools/shell_tool.py",
            "src/row_bot/tools/developer_tool.py",
            "src/row_bot/ui/command_center.py",
            "src/row_bot/ui/helpers.py",
            "src/row_bot/ui/mobile.py",
            "src/row_bot/ui/render.py",
        ),
        (
            "tests/test_approval_messages.py",
            "tests/test_shell_approval_policy.py",
            "tests/test_agent_approvals.py",
            "tests/test_active_run_queue.py",
            "tests/test_agent_ui_contracts.py",
            "tests/subsystem/channels/test_channel_approvals.py",
            "tests/subsystem/mobile",
        ),
        "Approval presentation and approval actions need policy, child-agent, UI, mobile, and channel coverage.",
    ),
    SourceTestRule(
        "workflows",
        ("src/row_bot/tasks.py", "src/row_bot/workflows/**", "src/row_bot/agents/**"),
        (
            "tests/subsystem/workflows",
            "tests/test_tasks_schema_recovery.py",
            "tests/test_workflow_delivery_defaults.py",
            "tests/test_goal_mode.py",
        ),
        "Task, workflow, and agent orchestration changes need resume/approval and schema coverage.",
    ),
    SourceTestRule(
        "threads",
        ("src/row_bot/threads.py",),
        (
            "tests/test_developer_workspace_threads.py",
            "tests/test_thread_approval_mode.py",
            "tests/test_thread_pinning.py",
            "tests/test_thread_rename.py",
            "tests/test_sidebar_developer_grouping.py",
        ),
        "Thread metadata changes need Developer grouping, approval, rename, and sidebar regressions.",
    ),
    SourceTestRule(
        "agent_profile_workflows",
        (
            "src/row_bot/agent_commands.py",
            "src/row_bot/agent_context.py",
            "src/row_bot/agent_budget.py",
            "src/row_bot/agent_settings.py",
            "src/row_bot/agent_run_messages.py",
            "src/row_bot/agent_runner.py",
            "src/row_bot/agent_orchestrator.py",
            "src/row_bot/agent_profiles.py",
            "src/row_bot/agent_runs.py",
            "src/row_bot/tools/agent_tool.py",
            "src/row_bot/tools/goal_tool.py",
            "src/row_bot/tools/task_tool.py",
            "src/row_bot/tools/row_bot_status_tool.py",
            "src/row_bot/ui/streaming.py",
            "src/row_bot/ui/agent_drawer.py",
            "src/row_bot/ui/settings.py",
            "src/row_bot/ui/task_dialog.py",
        ),
        (
            "tests/test_agent_commands.py",
            "tests/test_agent_context.py",
            "tests/test_agent_profiles.py",
            "tests/test_agent_runner.py",
            "tests/test_agent_tool.py",
            "tests/test_agent_runs.py",
            "tests/test_agent_runtime_profiles.py",
            "tests/subsystem/agents",
            "tests/test_active_run_queue.py",
            "tests/test_chat_tool_trace_ui.py",
            "tests/test_row_bot_status_agents.py",
            "tests/test_skill_pinning.py",
            "tests/subsystem/tools/test_goal_tool_contracts.py",
            "tests/subsystem/channels/test_channel_thread_notifications.py",
            "tests/subsystem/workflows",
        ),
        "Agent Profile, Agent Run/orchestration, workflow UI, and agent-facing workflow tools need profile/runtime and workflow regressions.",
    ),
    SourceTestRule(
        "mcp",
        (
            "src/row_bot/mcp_client/**",
            "src/row_bot/ui/mcp_settings.py",
        ),
        (
            "tests/contracts/test_mcp_contract.py",
            "tests/subsystem/mcp",
            "tests/test_mcp_client.py",
        ),
        "MCP changes need fake transport, safety, and client coverage.",
    ),
    SourceTestRule(
        "computer_use",
        (
            "src/row_bot/computer_use/**",
            "src/row_bot/tools/computer_use_tool.py",
            "src/row_bot/ui/computer_use.py",
            "src/row_bot/ui/live_control.py",
            "docs/COMPUTER_USE_SECURITY.md",
        ),
        (
            "tests/contracts/test_computer_use_contract.py",
            "tests/subsystem/computer_use",
            "tests/integration/computer_use",
        ),
        "Computer Use changes need offline driver contracts plus lease, policy, privacy, cancellation, approval, Vision, lifecycle, and UI integration coverage.",
    ),
    SourceTestRule(
        "browser_automation",
        ("src/row_bot/tools/browser_tool.py", "src/row_bot/ui/live_control.py"),
        (
            "tests/subsystem/browser",
            "tests/test_browser_cancellation.py",
            "tests/test_chat_tool_trace_ui.py",
        ),
        "Browser automation changes need privacy, consequence, navigation, tab-isolation, runtime-readiness, cancellation, and trace regressions.",
    ),
    SourceTestRule(
        "plugins",
        (
            "src/row_bot/app.py",
            "src/row_bot/plugins/**",
            "src/row_bot/tools/row_bot_status_tool.py",
            "src/row_bot/ui/status_checks.py",
            "scripts/validate_plugin.py",
            "scripts/build_plugin_index.py",
            "examples/plugins/**",
            "docs/PLUGIN_SYSTEM_V2.md",
            "docs/ARCHITECTURE.md",
        ),
        (
            "tests/contracts/plugins/test_plugin_api_contract.py",
            "tests/subsystem/plugins",
            "tests/subsystem/mcp/test_mcp_runtime_tools.py",
            "tests/subsystem/channels/test_channel_registry.py",
            "tests/test_row_bot_status_agents.py",
        ),
        "Plugin runtime, marketplace, templates, examples, and docs need plugin contracts plus MCP/channel integration coverage.",
    ),
    SourceTestRule(
        "migration",
        ("src/row_bot/migration/**",),
        (
            "tests/test_migration_core.py",
            "tests/test_migration_detection.py",
            "tests/test_migration_planner.py",
            "tests/test_migration_apply.py",
            "tests/test_migration_wizard_ui.py",
        ),
        "Migration wizard changes need deterministic core, detection, planning, apply, and UI coverage.",
    ),
    SourceTestRule(
        "document_ingestion",
        (
            "src/row_bot/document_jobs.py",
            "src/row_bot/document_uploads.py",
            "src/row_bot/document_index.py",
            "src/row_bot/document_extraction.py",
            "src/row_bot/documents.py",
            "src/row_bot/tools/documents_tool.py",
            "src/row_bot/ui/status_bar.py",
            "src/row_bot/ui/status_checks.py",
        ),
        (
            "tests/subsystem/knowledge_graph/test_document_ingestion_jobs.py",
            "tests/subsystem/knowledge_graph/test_document_index_shards.py",
            "tests/subsystem/knowledge_graph/test_document_extraction_resume.py",
            "tests/test_memory_evolution.py",
            "tests/test_embedding_provider_config.py",
            "tests/test_home_status_workflow_buddy.py",
            "tests/test_settings_overhaul_contracts.py",
        ),
        "Document ingestion changes need durable queue, bounded parsing/indexing, resumable extraction, retrieval compatibility, and UI/status coverage.",
    ),
    SourceTestRule(
        "memory_and_knowledge",
        (
            "src/row_bot/knowledge_graph.py",
            "src/row_bot/memory.py",
            "src/row_bot/memory_*.py",
            "src/row_bot/dream_cycle.py",
            "src/row_bot/wiki_vault.py",
            "src/row_bot/documents.py",
            "src/row_bot/embedding_config.py",
            "src/row_bot/embedding_providers.py",
        ),
        (
            "tests/subsystem/knowledge_graph",
            "tests/subsystem/dream_cycle",
            "tests/test_memory_recall_uplift.py",
            "tests/test_memory_evolution.py",
            "tests/test_knowledge_audit.py",
            "tests/test_embedding_provider_config.py",
        ),
        "Memory, knowledge graph, document, and Dream Cycle changes need deterministic recall and graph coverage.",
    ),
    SourceTestRule(
        "developer_studio",
        ("src/row_bot/developer/**",),
        (
            "tests/subsystem/developer",
            "tests/test_agent_write_locks.py",
            "tests/test_approval_policy.py",
        ),
        "Developer Studio changes need sandbox, import-gate, and approval-policy coverage.",
    ),
    SourceTestRule(
        "designer",
        ("src/row_bot/designer/**",),
        (
            "tests/subsystem/designer",
            "tests/test_developer_studio_phase2.py",
        ),
        "Designer changes need export and deterministic snapshot smoke coverage.",
    ),
    SourceTestRule(
        "installer_and_release",
        (
            ".github/workflows/**",
            "installer/**",
            "src/row_bot/updater.py",
            "scripts/coverage_summary.py",
            "scripts/smoke_app.py",
            "scripts/verify_runtime_dependencies.py",
            "scripts/export_locked_requirements.py",
            "scripts/app_payload_manifest.py",
            "scripts/cut_release.py",
            "pyproject.toml",
            "uv.lock",
            "requirements.txt",
        ),
        (
            "tests/subsystem/installer",
            "tests/subsystem/updater",
            "tests/contracts/installers",
        ),
        "Installer, release, dependency, and workflow changes need matrix and package contract coverage.",
    ),
    SourceTestRule(
        "tests",
        ("tests/**", "pytest.ini"),
        (
            "tests/subsystem/test_coverage_inventory.py",
            "tests/subsystem/test_legacy_inventory.py",
            "tests/subsystem/test_source_test_map.py",
        ),
        "Test architecture changes need inventory and source-to-test map validation.",
    ),
)


@dataclass(frozen=True)
class ChangeSelection:
    changed_files: tuple[str, ...]
    test_paths: tuple[str, ...]
    matched_rules: tuple[str, ...]
    unmatched_files: tuple[str, ...]
    reasons: tuple[str, ...]


def select_tests_for_changes(
    changed_files: list[str] | tuple[str, ...],
) -> ChangeSelection:
    normalized_files = tuple(
        normalize_repo_path(path) for path in changed_files if str(path).strip()
    )
    selected: list[str] = []
    matched_rules: list[str] = []
    reasons: list[str] = []
    unmatched: list[str] = []

    for changed in normalized_files:
        matches = [rule for rule in SOURCE_TEST_RULES if rule.matches(changed)]
        if not matches:
            unmatched.append(changed)
            continue
        for rule in matches:
            if rule.name not in matched_rules:
                matched_rules.append(rule.name)
                reasons.append(rule.reason)
            for test_path in rule.test_paths:
                if test_path not in selected:
                    selected.append(test_path)

    return ChangeSelection(
        changed_files=normalized_files,
        test_paths=tuple(selected),
        matched_rules=tuple(matched_rules),
        unmatched_files=tuple(unmatched),
        reasons=tuple(reasons),
    )
