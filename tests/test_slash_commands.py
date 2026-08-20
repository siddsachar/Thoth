from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

from row_bot.agent_budget import new_execution_budget


def _trim_state(messages: list, logical_turn_id: str) -> dict:
    return {
        "execution_budget": new_execution_budget(logical_turn_id),
        "messages": messages,
    }


def _reload_skill_command_modules(tmp_path: Path):
    os.environ["ROW_BOT_DATA_DIR"] = str(tmp_path)
    os.environ["ROW_BOT_DATA_DIR"] = str(tmp_path)
    modules = [
        "row_bot.skills",
        "row_bot.skills_activation",
        "row_bot.slash_commands",
    ]
    for name in modules:
        if name in sys.modules:
            importlib.reload(sys.modules[name])
        else:
            importlib.import_module(name)
    import row_bot.skills as skills
    import row_bot.skills_activation as skills_activation
    import row_bot.slash_commands as slash_commands

    return skills, skills_activation, slash_commands


def _write_skill(
    root: Path,
    name: str,
    *,
    display_name: str | None = None,
    description: str = "Test skill",
    enabled_by_default: bool = True,
    tools: list[str] | None = None,
    tags: list[str] | None = None,
) -> None:
    skill_dir = root / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        f"name: {name}",
        f"display_name: {display_name or name.replace('_', ' ').title()}",
        "icon: '*'",
        f"description: {description}",
        f"enabled_by_default: {str(enabled_by_default).lower()}",
        "version: 1.0",
    ]
    if tools:
        lines.append("tools:")
        lines.extend(f"  - {tool}" for tool in tools)
    if tags:
        lines.append("tags:")
        lines.extend(f"  - {tag}" for tag in tags)
    lines.extend(["---", "", f"Instructions for {name}."])
    (skill_dir / "SKILL.md").write_text("\n".join(lines), encoding="utf-8")


def test_registry_aliases_generated_skills_and_hidden_entries(tmp_path):
    _write_skill(
        tmp_path,
        "meeting_notes",
        display_name="Meeting Notes",
        description="Summarize meetings",
    )
    _write_skill(
        tmp_path,
        "off_skill",
        display_name="Off Skill",
        enabled_by_default=False,
    )
    _write_skill(
        tmp_path,
        "browser_guide_like",
        display_name="Browser Guide Like",
        tools=["browser"],
    )
    skills, _activation, slash_commands = _reload_skill_command_modules(tmp_path)
    skills.load_skills()

    specs = slash_commands.get_command_specs()
    by_id = {spec.id: spec for spec in specs}
    assert {"skills", "skill-reset", "noskill", "new", "status", "help"} <= set(by_id)
    assert "model" not in by_id
    assert "settings" not in by_id
    assert slash_commands.resolve_command_token("/model") is None
    assert slash_commands.resolve_command_token("/settings") is None

    assert slash_commands.resolve_command_token("/skill").id == "skills"
    assert slash_commands.resolve_command_text("/skill reset")[0].id == "skill-reset"
    assert slash_commands.resolve_command_token("/meeting-notes").skill_name == "meeting_notes"
    assert slash_commands.resolve_command_token("/meeting_notes").skill_name == "meeting_notes"
    assert slash_commands.resolve_command_token("/off-skill") is None
    assert slash_commands.resolve_command_token("/browser-guide-like") is None

    filtered = slash_commands.filter_command_specs(specs, "meet")
    assert filtered and filtered[0].skill_name == "meeting_notes"

    builtin_specs = slash_commands.get_builtin_commands()
    fuzzy_agent = slash_commands.filter_command_specs(builtin_specs, "agt")
    assert fuzzy_agent and fuzzy_agent[0].slash == "/agent"
    fuzzy_worktree = slash_commands.filter_command_specs(builtin_specs, "wrk")
    assert fuzzy_worktree and fuzzy_worktree[0].slash == "/agent"
    prefix_profile = slash_commands.filter_command_specs(builtin_specs, "prof")
    assert prefix_profile and prefix_profile[0].slash == "/profile"
    assert slash_commands.argument_hint(prefix_profile[0]) == "Type details after the command"

    all_visible = slash_commands.filter_command_specs(specs, "", limit=len(specs))
    assert len(all_visible) == len(specs)
    assert all_visible.index(by_id["help"]) < all_visible.index(by_id["skill:meeting_notes"])

    help_text = slash_commands.help_text()
    assert "**Chat**" in help_text
    assert "**Skills**" in help_text
    assert "`/meeting-notes`" in help_text
    assert "`/model`" not in help_text
    assert "`/settings`" not in help_text


def test_builtin_commands_win_skill_collisions(tmp_path):
    _write_skill(
        tmp_path,
        "status",
        display_name="Status",
        description="A colliding skill",
    )
    skills, _activation, slash_commands = _reload_skill_command_modules(tmp_path)
    skills.load_skills()

    resolved = slash_commands.resolve_command_token("/status")
    assert resolved.id == "status"
    assert resolved.handler_key == "status"
    assert not any(spec.id == "skill:status" for spec in slash_commands.get_command_specs())


def test_reasoning_is_a_builtin_chat_command_without_aliases(tmp_path):
    _skills, _activation, slash_commands = _reload_skill_command_modules(tmp_path)

    spec = slash_commands.resolve_command_token("/reasoning", include_skills=False)

    assert spec is not None
    assert spec.id == "reasoning"
    assert spec.aliases == ()
    assert spec.argument_behavior == "prefix"
    assert "`/reasoning`" in slash_commands.help_text(include_skills=False)


def test_direct_skill_activation_and_reset_dispatch(tmp_path):
    _write_skill(
        tmp_path,
        "deep_research",
        display_name="Deep Research",
        description="Research and summarize sources",
    )
    skills, activation, slash_commands = _reload_skill_command_modules(tmp_path)
    skills.load_skills()

    response = slash_commands.dispatch_text_command("thread-a", "/deep-research")
    assert response and "deep_research" in response
    assert activation.resolve_active_skill_names("thread-a") == ["deep_research"]

    reset = slash_commands.dispatch_text_command("thread-a", "/skill-reset")
    assert reset == "Skills reset for this chat."
    assert activation.resolve_active_skill_names("thread-a") == skills.get_default_active_skill_names("chat")


def test_slash_token_replacement_preserves_draft_text(tmp_path):
    _skills, _activation, slash_commands = _reload_skill_command_modules(tmp_path)

    text = "Please /meeting-notes summarize this"
    cursor = text.index(" summarize")
    new_text, new_cursor = slash_commands.remove_current_slash_token(text, cursor)
    assert new_text == "Please summarize this"
    assert new_cursor <= len(new_text)

    replaced, cursor_after = slash_commands.replace_current_slash_token(
        "Use /nos",
        len("Use /nos"),
        "/noskill ",
    )
    assert replaced == "Use /noskill "
    assert cursor_after == len(replaced)


def test_prompt_injection_and_tool_guide_separation_for_runtime_commands(tmp_path):
    _write_skill(
        tmp_path,
        "alpha_skill",
        display_name="Alpha Skill",
        description="Alpha planning workflow",
        tags=["alpha"],
    )
    _write_skill(
        tmp_path,
        "alpha_tool_guide",
        display_name="Alpha Tool Guide",
        description="Tool guide",
        tools=["browser"],
    )
    skills, activation, slash_commands = _reload_skill_command_modules(tmp_path)
    skills.load_skills()

    command_names = {spec.skill_name for spec in slash_commands.get_command_specs() if spec.skill_name}
    assert "alpha_skill" in command_names
    assert "alpha_tool_guide" not in command_names

    import row_bot.agent as agent
    from langchain_core.messages import HumanMessage

    thread_id = "slash-prompt-thread"
    agent._set_active_runtime_context(thread_id=thread_id, enabled_tool_names=[])
    lean = agent._pre_model_trim(
        _trim_state([HumanMessage(content="alpha planning")], "slash-prompt-lean")
    )
    lean_prompt = "\n".join(str(m.content) for m in lean["llm_input_messages"])
    assert "## Skills" not in lean_prompt
    assert "Instructions for alpha_skill." not in lean_prompt

    slash_commands.dispatch_text_command(thread_id, "/alpha-skill")
    active = agent._pre_model_trim(
        _trim_state([HumanMessage(content="alpha planning")], "slash-prompt-active")
    )
    active_prompt = "\n".join(str(m.content) for m in active["llm_input_messages"])
    assert "Instructions for alpha_skill." in active_prompt
    assert "Instructions for alpha_tool_guide." not in active_prompt

    guide_prompt = skills.get_skills_prompt([], active_tool_names=["browser"])
    assert "Instructions for alpha_tool_guide." in guide_prompt
    assert "## Skills" not in guide_prompt
