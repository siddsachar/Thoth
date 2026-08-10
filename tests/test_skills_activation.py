from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

from row_bot.agent_budget import new_execution_budget


def _trim_state(messages: list, logical_turn_id: str) -> dict:
    return {
        "execution_budget": new_execution_budget(logical_turn_id),
        "messages": messages,
    }


def _reload_skill_modules(tmp_path: Path):
    os.environ["ROW_BOT_DATA_DIR"] = str(tmp_path)
    os.environ["ROW_BOT_DATA_DIR"] = str(tmp_path)
    for name in ("row_bot.skills", "row_bot.skills_activation", "row_bot.prompts", "row_bot.agent"):
        if name in sys.modules:
            importlib.reload(sys.modules[name])
        else:
            importlib.import_module(name)
    import row_bot.skills as skills
    import row_bot.skills_activation as skills_activation

    return skills, skills_activation


def _write_skill(
    root: Path,
    name: str,
    *,
    description: str,
    enabled_by_default: bool = True,
    tags: list[str] | None = None,
    tools: list[str] | None = None,
    activation: dict[str, list[str]] | None = None,
    instructions: str | None = None,
) -> None:
    skill_dir = root / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        f"name: {name}",
        f"display_name: {name.replace('_', ' ').title()}",
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
    if activation:
        lines.append("activation:")
        for key, values in activation.items():
            lines.append(f"  {key}:")
            lines.extend(f"    - {value}" for value in values)
    lines.extend(["---", "", instructions or f"Instructions for {name}."])
    (skill_dir / "SKILL.md").write_text("\n".join(lines), encoding="utf-8")


def _disable_bundled_manual_skills(skills) -> None:
    for skill in skills.get_manual_skills():
        if getattr(skill, "source", "") == "bundled":
            skills.set_enabled(skill.name, False)


def test_parse_skill_commands(tmp_path):
    _skills, activation = _reload_skill_modules(tmp_path)

    assert activation.parse_skill_command("/skills").action == "list"
    assert activation.parse_skill_command("/skill off").action == "off"
    assert activation.parse_skill_command("/skill reset").action == "reset"
    assert activation.parse_skill_command("/skill-reset").action == "reset"
    assert activation.parse_skill_command("/skillreset").action == "reset"
    assert activation.parse_skill_command("/skill_reset").action == "reset"
    cmd = activation.parse_skill_command("/skill once research_brief")
    assert cmd.action == "unsupported_once"
    cmd = activation.parse_skill_command("/noskill research_brief")
    assert cmd.action == "disable"
    assert cmd.name == "research_brief"
    cmd = activation.parse_skill_command("/skills meeting notes")
    assert cmd.action == "list"
    assert cmd.name == "meeting notes"


def test_bundled_self_reflection_uses_mirror_emoji():
    from row_bot.skills import _parse_skill_md

    skill = _parse_skill_md(Path("bundled_skills/self_reflection/SKILL.md"), source="bundled")

    assert skill is not None
    assert skill.icon == "🪞"


def test_thread_scoped_state_off_and_reset(tmp_path):
    _write_skill(tmp_path, "research_brief", description="Research and summarize sources", tags=["research"])
    skills, activation = _reload_skill_modules(tmp_path)
    skills.load_skills()

    assert "research_brief" in activation.apply_skill_command("thread-a", "/skill research_brief")
    assert activation.resolve_active_skill_names("thread-a") == ["research_brief"]
    assert activation.resolve_active_skill_names("thread-b") == []

    activation.apply_skill_command("thread-a", "/noskill research_brief")
    assert activation.resolve_active_skill_names("thread-a") == []

    response = activation.apply_skill_command("thread-a", "/skill once research_brief")
    assert response and "not supported" in response
    assert activation.resolve_active_skill_names("thread-a") == []

    activation.apply_skill_command("thread-a", "/skill off")
    assert activation.get_activation_snapshot("thread-a", current_text="research sources").smart_off is True
    assert activation.suggest_skills("thread-a", "research sources") == []

    activation.apply_skill_command("thread-a", "/skill reset")
    assert activation.get_activation_snapshot("thread-a").smart_off is False


def test_deterministic_suggestions_and_tool_guide_separation(tmp_path):
    _write_skill(
        tmp_path,
        "meeting_notes",
        description="Summarize meeting transcripts into decisions and action items",
        tags=["meeting", "notes"],
    )
    _write_skill(
        tmp_path,
        "weather_guide_like",
        description="Weather tool guide should not be suggested as a manual skill",
        tools=["weather"],
    )
    skills, activation = _reload_skill_modules(tmp_path)
    skills.load_skills()

    suggestions = activation.suggest_skills(
        "thread-a",
        "Please summarize these meeting notes and action items",
        enabled_tool_names=["weather"],
    )
    names = [item.name for item in suggestions]
    assert "meeting_notes" in names
    assert "weather_guide_like" not in names

    activation.dismiss_suggestion("thread-a", "meeting_notes")
    names_after_dismiss = [
        item.name for item in activation.suggest_skills("thread-a", "meeting notes")
    ]
    assert "meeting_notes" not in names_after_dismiss


def test_suggestions_do_not_use_enabled_tool_metadata(tmp_path):
    _write_skill(
        tmp_path,
        "browser_advice",
        description="General browsing workflow",
        tools=["browser"],
    )
    _write_skill(
        tmp_path,
        "web_research",
        description="Research websites and summarize findings",
        tags=["research", "web"],
    )
    skills, activation = _reload_skill_modules(tmp_path)
    skills.load_skills()

    suggestions = activation.suggest_skills(
        "thread-a",
        "research websites and summarize findings",
        enabled_tool_names=["browser"],
    )
    names = [item.name for item in suggestions]
    assert "web_research" in names
    assert "browser_advice" not in names
    assert all("enabled tools match" not in item.reason for item in suggestions)


def test_suggestions_ignore_common_stopwords(tmp_path):
    _write_skill(
        tmp_path,
        "research_brief",
        description="Research sources and summarize findings",
        tags=["research"],
    )
    _write_skill(
        tmp_path,
        "meeting_notes",
        description="Meeting decisions and action items",
        tags=["meeting", "notes"],
    )
    skills, activation = _reload_skill_modules(tmp_path)
    skills.load_skills()

    assert activation.suggest_skills("thread-a", "and the this with for") == []
    names = [
        item.name
        for item in activation.suggest_skills(
            "thread-a",
            "turn meeting notes into decisions and action items",
        )
    ]
    assert names and names[0] == "meeting_notes"


def test_suggestions_use_instruction_headings_and_body_for_sparse_skills(tmp_path):
    _write_skill(
        tmp_path,
        "office_docx",
        description="Generic office helper",
        instructions=(
            "Instructions for office documents.\n\n"
            "## Tracked Changes\n"
            "Read comments, revisions, and tracked changes from DOCX files.\n\n"
            "## Converting To Images\n"
            "Convert document pages to image previews for review workflows."
        ),
    )
    _write_skill(
        tmp_path,
        "general_writer",
        description="Generic writing helper",
        instructions="Write concise prose for ordinary messages.",
    )
    skills, activation = _reload_skill_modules(tmp_path)
    skills.load_skills()

    suggestions = activation.suggest_skills(
        "thread-a",
        "read tracked changes from this docx and convert pages to images",
    )

    assert suggestions
    assert suggestions[0].name == "office_docx"
    assert "general_writer" not in {item.name for item in suggestions}


def test_sparse_imported_style_skill_matches_without_activation_metadata(tmp_path):
    _write_skill(
        tmp_path,
        "crontab_generate",
        description="Crontab expression generator",
        tags=["converted"],
        instructions=(
            "Converted from a public agent profile.\n\n"
            "## Instructions\n"
            "Create cron schedules and validate crontab expressions for recurring jobs."
        ),
    )
    skills, activation = _reload_skill_modules(tmp_path)
    skills.load_skills()

    suggestions = activation.suggest_skills(
        "thread-a",
        "generate a cron expression for every monday morning",
    )

    assert suggestions
    assert suggestions[0].name == "crontab_generate"


def test_shared_instruction_terms_do_not_create_broad_suggestions(tmp_path):
    for name in ("general_helper", "productivity_helper", "writing_helper"):
        _write_skill(
            tmp_path,
            name,
            description="Generic helper",
            instructions=(
                "## Workflow\n"
                "Help create, review, improve, and organize content for routine work."
            ),
        )
    skills, activation = _reload_skill_modules(tmp_path)
    skills.load_skills()
    _disable_bundled_manual_skills(skills)

    assert activation.suggest_skills("thread-a", "help me review and improve this content") == []


def test_skill_choice_search_uses_shared_weighted_matcher(tmp_path):
    _write_skill(
        tmp_path,
        "meeting_notes",
        description="Generic productivity helper",
        instructions=(
            "## Action Items\n"
            "Extract decisions, owners, follow ups, and deadlines from meeting transcripts."
        ),
    )
    _write_skill(
        tmp_path,
        "research_brief",
        description="Research sources and produce a brief",
        tags=["research"],
    )
    skills, activation = _reload_skill_modules(tmp_path)
    skills.load_skills()

    choices = activation.list_skill_choices("thread-a", query="owners and deadlines")

    assert choices
    assert choices[0].name == "meeting_notes"


def test_chat_skill_picker_uses_shared_ranked_choices():
    src = Path("src/row_bot/ui/chat_composer_extras.py").read_text(encoding="utf-8")

    assert "list_skill_choices" in src
    assert "def _matches(skill)" not in src


def test_chat_suppresses_draft_suggestions_after_use_or_dismiss():
    src = Path("src/row_bot/ui/chat_composer_extras.py").read_text(encoding="utf-8")

    assert "suggestions_suppressed_text" in src
    assert "def _suppress_skill_suggestions_for_current_draft" in src
    assert "_cancel_skill_chip_refresh_task()" in src
    assert "source.startswith(\"ui\")" in src


def test_studio_composers_wire_shared_slash_and_skill_extras():
    components_src = Path("src/row_bot/ui/chat_components.py").read_text(encoding="utf-8")
    extras_src = Path("src/row_bot/ui/chat_composer_extras.py").read_text(encoding="utf-8")
    designer_src = Path("src/row_bot/designer/editor.py").read_text(encoding="utf-8")
    developer_src = Path("src/row_bot/developer/ui.py").read_text(encoding="utf-8")
    agent_src = Path("src/row_bot/agent.py").read_text(encoding="utf-8")

    assert "composer_extras: Any | None = None" in components_src
    assert "composer_extras.render_before_input()" in components_src
    assert "composer_extras.attach_input(p.chat_input)" in components_src
    assert "window._rowBotSlashPaletteOpen" in components_src
    assert "class ComposerExtrasController" in extras_src
    assert "row-bot-slash-palette-list" in extras_src
    assert "create_designer_composer_extras" in designer_src
    assert "create_developer_composer_extras" in developer_src
    assert "set_thread_skills_override" in extras_src
    assert 'skill_mode="developer"' in extras_src
    assert 'skill_mode="thread_override"' in extras_src
    assert "_resolve_active_skill_records" in agent_src
    assert "elif override is not None" in agent_src


def test_activation_metadata_drives_suggestions_without_prompt_bloat(tmp_path):
    _write_skill(
        tmp_path,
        "meeting_notes",
        description="Generic productivity helper",
        tags=["productivity"],
        activation={
            "phrases": ["meeting notes"],
            "keywords": ["decisions", "action items"],
            "negative_phrases": ["competitor research"],
            "examples": ["Summarize these meeting notes and extract action items"],
        },
    )
    skills, activation = _reload_skill_modules(tmp_path)
    skills.load_skills()
    _disable_bundled_manual_skills(skills)

    skill = skills.get_skill("meeting_notes")
    assert skill.activation["phrases"] == ["meeting notes"]

    suggestions = activation.suggest_skills(
        "thread-a",
        "Summarize these meeting notes and extract action items",
    )
    assert suggestions and suggestions[0].name == "meeting_notes"
    assert activation.suggest_skills("thread-a", "competitor research report") == []

    import row_bot.agent as agent
    from langchain_core.messages import HumanMessage

    thread_id = "metadata-prompt-thread"
    agent._set_active_runtime_context(thread_id=thread_id, enabled_tool_names=[])
    lean = agent._pre_model_trim(
        _trim_state([HumanMessage(content="meeting notes")], "skill-metadata-lean")
    )
    lean_prompt = "\n".join(str(m.content) for m in lean["llm_input_messages"])
    assert "## Skills" not in lean_prompt
    assert "Instructions for meeting_notes." not in lean_prompt
    assert "action items" not in lean_prompt

    activation.pin_skill(thread_id, "meeting_notes")
    active = agent._pre_model_trim(
        _trim_state([HumanMessage(content="meeting notes")], "skill-metadata-active")
    )
    active_prompt = "\n".join(str(m.content) for m in active["llm_input_messages"])
    assert "Instructions for meeting_notes." in active_prompt
    assert "Summarize these meeting notes and extract action items" not in active_prompt


def test_live_draft_suggestions_can_skip_trace_writes(tmp_path):
    _write_skill(
        tmp_path,
        "meeting_notes",
        description="Generic productivity helper",
        activation={
            "phrases": ["meeting notes"],
            "keywords": ["action items"],
        },
    )
    skills, activation = _reload_skill_modules(tmp_path)
    skills.load_skills()

    suggestions = activation.suggest_skills(
        "thread-a",
        "Summarize these meeting notes and extract action items",
        trace=False,
    )
    assert suggestions and suggestions[0].name == "meeting_notes"
    assert activation._load_store()["telemetry"]["traces"] == []

    activation.suggest_skills(
        "thread-a",
        "Summarize these meeting notes and extract action items",
    )
    traces = activation._load_store()["telemetry"]["traces"]
    assert len(traces) == 1
    assert traces[0]["event"] == "suggest"


def test_bundled_real_world_suggestion_matrix(tmp_path):
    skills, activation = _reload_skill_modules(tmp_path)
    skills.load_skills()

    enabled_names = [
        "meeting_notes",
        "deep_research",
        "brain_dump",
        "humanizer",
        "task_automation",
        "web_navigator",
        "self_reflection",
        "knowledge_base",
        "data_analyst",
        "design_creator",
    ]
    for name in enabled_names:
        skills.set_enabled(name, True)
        assert skills.is_enabled(name)

    cases = [
        (
            "Summarize these meeting notes and extract action items",
            "meeting_notes",
            {"deep_research"},
        ),
        (
            "I need to research competitors and produce a structured report",
            "deep_research",
            {"meeting_notes"},
        ),
        ("I just need to dump a bunch of messy thoughts", "brain_dump", set()),
        ("Make this sound more human and less corporate", "humanizer", set()),
        ("Help me set up a recurring task every Monday", "task_automation", set()),
        ("Browse this site and extract pricing info", "web_navigator", set()),
        (
            "Review what you know about me and clean stale memory",
            "self_reflection",
            set(),
        ),
        ("Analyze this CSV and chart trends", "data_analyst", set()),
        ("Create a one-page product concept", "design_creator", set()),
    ]
    for index, (prompt, expected, not_expected) in enumerate(cases):
        thread_id = f"matrix-{index}"
        suggestions = activation.suggest_skills(thread_id, prompt, limit=3)
        names = [item.name for item in suggestions]
        assert names, prompt
        assert names[0] == expected
        assert not not_expected.intersection(names)

    for index, prompt in enumerate(("hi", "what can you do?", "explain this concept")):
        assert activation.suggest_skills(f"generic-{index}", prompt) == []


def test_library_off_skills_are_not_selectable_or_suggested(tmp_path):
    _write_skill(
        tmp_path,
        "meeting_notes",
        description="Summarize meeting transcripts into decisions and action items",
        tags=["meeting", "notes"],
        enabled_by_default=False,
    )
    skills, activation = _reload_skill_modules(tmp_path)
    skills.load_skills()

    response = activation.apply_skill_command("thread-a", "/skill meeting_notes")
    assert response and "off in the Skills library" in response
    assert activation.resolve_active_skill_names("thread-a") == []
    assert activation.suggest_skills("thread-a", "meeting notes and decisions") == []

    skills.set_enabled("meeting_notes", True)
    response = activation.apply_skill_command("thread-a", "/skill meeting_notes")
    assert response and "meeting_notes" in response
    assert activation.resolve_active_skill_names("thread-a") == ["meeting_notes"]


def test_channel_skill_choices_are_deterministic_and_exclude_guides(tmp_path):
    _write_skill(
        tmp_path,
        "research_brief",
        description="Research sources and produce a brief",
        tags=["research"],
    )
    _write_skill(
        tmp_path,
        "research_notes",
        description="Organize research notes",
        tags=["research"],
    )
    _write_skill(
        tmp_path,
        "browser_guide",
        description="Browser tool guide",
        tools=["browser"],
    )
    _write_skill(
        tmp_path,
        "off_skill",
        description="Disabled library skill",
        enabled_by_default=False,
    )
    skills, activation = _reload_skill_modules(tmp_path)
    skills.load_skills()

    choices = activation.list_skill_choices("thread-a")
    names = {choice.name for choice in choices}
    assert "research_brief" in names
    assert "research_notes" in names
    assert "browser_guide" not in names
    assert "off_skill" not in names

    matches = activation.match_skill_choices("research", thread_id="thread-a")
    assert [choice.name for choice in matches] == ["research_brief", "research_notes"]

    result = activation.apply_channel_skill_command("thread-a", "/skill research")
    assert result is not None
    assert result.kind == "choices"
    assert "Multiple skills match" in result.text
    assert activation.resolve_active_skill_names("thread-a") == []

    result = activation.apply_channel_skill_command("thread-a", "/skill research-brief")
    assert result is not None
    assert result.kind == "activated"
    assert activation.resolve_active_skill_names("thread-a") == ["research_brief"]

    result = activation.apply_channel_skill_command("thread-a", "/noskill")
    assert result is not None
    assert result.kind == "disabled"
    assert activation.resolve_active_skill_names("thread-a") == []


def test_channel_skill_list_filters_and_reset_aliases(tmp_path):
    _write_skill(tmp_path, "meeting_notes", description="Summarize meetings")
    _write_skill(tmp_path, "deep_research", description="Research reports")
    skills, activation = _reload_skill_modules(tmp_path)
    skills.load_skills()

    result = activation.apply_channel_skill_command("thread-a", "/skills meeting")
    assert result is not None
    assert result.kind == "list"
    assert [choice.name for choice in result.choices] == ["meeting_notes"]
    assert "deep_research" not in result.text

    activation.apply_channel_skill_command("thread-a", "/skill meeting_notes")
    assert activation.resolve_active_skill_names("thread-a") == ["meeting_notes"]
    for reset_text in ("/skill reset", "/skill-reset", "/skillreset", "/skill_reset"):
        activation.apply_channel_skill_command("thread-a", "/skill meeting_notes")
        result = activation.apply_channel_skill_command("thread-a", reset_text)
        assert result is not None
        assert result.kind == "reset"
        assert activation.resolve_active_skill_names("thread-a") == skills.get_default_active_skill_names("chat")


def test_background_resolution_is_explicit_only(tmp_path):
    _write_skill(tmp_path, "research_brief", description="Research sources", tags=["research"])
    skills, activation = _reload_skill_modules(tmp_path)
    skills.load_skills()

    activation.pin_skill("thread-a", "research_brief")
    assert activation.resolve_active_skill_names("thread-a", is_background=True) == []
    assert activation.resolve_active_skill_names(
        "thread-a",
        explicit_override=["research_brief"],
        is_background=True,
    ) == ["research_brief"]


def test_manual_skill_crud_strips_tools_metadata(tmp_path):
    skills, _activation = _reload_skill_modules(tmp_path)
    skills.load_skills()

    created = skills.create_skill(
        name="toolish_manual",
        display_name="Toolish Manual",
        icon="spark",
        description="Manual skill that should stay manual",
        instructions="Manual instructions.",
        tools=["browser"],
        tags=["manual"],
    )
    assert created.tools == []
    assert not skills.is_tool_guide(created)
    assert "tools:" not in (created.path / "SKILL.md").read_text(encoding="utf-8")

    updated = skills.update_skill("toolish_manual", instructions="Updated.", tools=["browser"])
    assert updated.tools == []
    assert not skills.is_tool_guide(updated)
    assert "tools:" not in (updated.path / "SKILL.md").read_text(encoding="utf-8")


def test_tool_guide_prompt_injection_stays_tool_bound(tmp_path):
    skills, _activation = _reload_skill_modules(tmp_path)
    skills.load_skills()

    no_guides = skills.get_skills_prompt([], active_tool_names=[])
    assert "BROWSER AUTOMATION" not in no_guides

    browser_guide = skills.get_skills_prompt([], active_tool_names=["browser"])
    assert "BROWSER AUTOMATION" in browser_guide
    assert "## Skills" not in browser_guide

    manual_with_guide = skills.get_skills_prompt(
        ["deep_research"],
        active_tool_names=["browser"],
    )
    assert "BROWSER AUTOMATION" in manual_with_guide
    assert "Deep Research" in manual_with_guide
    assert "## Skills" in manual_with_guide


def test_bundled_manual_skills_default_enabled_without_tool_guides(tmp_path):
    skills, _activation = _reload_skill_modules(tmp_path)
    skills.load_skills()

    assert skills.is_enabled("meeting_notes") is True
    assert skills.is_enabled("deep_research") is True
    tool_guides = {skill.name for skill in skills.get_all_skills() if skills.is_tool_guide(skill)}
    assert "browser_guide" in tool_guides
    assert skills.is_enabled("browser_guide") is False


def test_bundled_manual_default_migration_is_one_time(tmp_path):
    skills, _activation = _reload_skill_modules(tmp_path)
    config_path = tmp_path / "skills_config.json"
    config_path.write_text(
        json.dumps({"skills": {"meeting_notes": False}}),
        encoding="utf-8",
    )

    skills.load_skills()

    assert skills.is_enabled("meeting_notes") is True
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved[skills.BUNDLED_MANUAL_DEFAULTS_CONFIG_KEY] is True

    skills.set_enabled("meeting_notes", False)
    skills, _activation = _reload_skill_modules(tmp_path)
    skills.load_skills()

    assert skills.is_enabled("meeting_notes") is False


def test_agent_prompt_is_lean_until_chat_skills_are_active(tmp_path):
    _write_skill(tmp_path, "alpha_skill", description="Alpha planning workflow", tags=["alpha"])
    _write_skill(tmp_path, "beta_skill", description="Beta review workflow", tags=["beta"])
    skills, activation = _reload_skill_modules(tmp_path)
    skills.load_skills()

    import row_bot.agent as agent
    from langchain_core.messages import HumanMessage

    thread_id = "prompt-thread"
    agent._set_active_runtime_context(thread_id=thread_id, enabled_tool_names=[])

    lean = agent._pre_model_trim(
        _trim_state([HumanMessage(content="hello")], "skills-prompt-lean")
    )
    lean_prompt = "\n".join(str(m.content) for m in lean["llm_input_messages"])
    assert "## Skills" not in lean_prompt
    assert "Instructions for alpha_skill." not in lean_prompt
    assert "Instructions for beta_skill." not in lean_prompt
    assert "BROWSER AUTOMATION" not in lean_prompt

    activation.pin_skill(thread_id, "alpha_skill")
    one_skill = agent._pre_model_trim(
        _trim_state([HumanMessage(content="use alpha")], "skills-prompt-alpha")
    )
    one_skill_prompt = "\n".join(str(m.content) for m in one_skill["llm_input_messages"])
    assert "## Skills" in one_skill_prompt
    assert "Instructions for alpha_skill." in one_skill_prompt
    assert "Instructions for beta_skill." not in one_skill_prompt

    activation.pin_skill(thread_id, "beta_skill")
    two_skills = agent._pre_model_trim(
        _trim_state([HumanMessage(content="use both")], "skills-prompt-both")
    )
    two_skill_prompt = "\n".join(str(m.content) for m in two_skills["llm_input_messages"])
    assert "Instructions for alpha_skill." in two_skill_prompt
    assert "Instructions for beta_skill." in two_skill_prompt

    activation.disable_skill(thread_id, "alpha_skill")
    beta_only = agent._pre_model_trim(
        _trim_state([HumanMessage(content="beta only")], "skills-prompt-beta")
    )
    beta_only_prompt = "\n".join(str(m.content) for m in beta_only["llm_input_messages"])
    assert "Instructions for alpha_skill." not in beta_only_prompt
    assert "Instructions for beta_skill." in beta_only_prompt


def test_channel_dispatch_applies_skill_to_thread(tmp_path):
    _write_skill(tmp_path, "research_brief", description="Research sources", tags=["research"])
    skills, activation = _reload_skill_modules(tmp_path)
    skills.load_skills()

    from row_bot.channels import commands

    response = commands.dispatch("sms", "/skill research_brief", thread_id="sms_1")
    assert response and "research_brief" in response
    assert activation.resolve_active_skill_names("sms_1") == ["research_brief"]


def test_smart_off_retains_implicit_skill_but_disables_future_discovery(tmp_path, monkeypatch):
    _skills, activation = _reload_skill_modules(tmp_path)
    import row_bot.agent as agent
    import row_bot.skill_discovery as discovery

    record = discovery.SkillRecord(
        canonical_id="implicit",
        alias=None,
        display_name="Implicit",
        icon="*",
        description="Implicit skill",
        tags=(),
        activation={},
        instructions="IMPLICIT_BODY",
        source="manual",
        root=tmp_path,
    )
    monkeypatch.setattr(discovery, "collect_enabled_skill_records", lambda: [record])
    activation.load_auto_skill("smart-off", "implicit", available_ids={"implicit"})
    activation.set_smart_off("smart-off", True)
    agent._set_active_runtime_context(thread_id="smart-off", runtime_surface="agent")

    authorized, active_ids, discoverable, _fingerprint = agent._build_runtime_skill_snapshot()

    assert [item.canonical_id for item in authorized] == ["implicit"]
    assert active_ids == ("implicit",)
    assert discoverable is False


def test_noskill_removes_task_local_plugin_skill(tmp_path, monkeypatch):
    _skills, activation = _reload_skill_modules(tmp_path)
    import row_bot.skill_discovery as discovery

    record = discovery.SkillRecord(
        canonical_id="plugin:demo:review",
        alias="review",
        display_name="Plugin Review",
        icon="*",
        description="Review skill",
        tags=(),
        activation={},
        instructions="review",
        source="plugin:demo",
        root=tmp_path,
        plugin_name="Demo",
    )
    monkeypatch.setattr(discovery, "collect_enabled_skill_records", lambda: [record])
    activation.load_auto_skill(
        "plugin-thread",
        record.canonical_id,
        available_ids={record.canonical_id},
    )

    result = activation.apply_channel_skill_command("plugin-thread", "/noskill review")

    assert result.kind == "disabled"
    assert result.selected_skill == "plugin:demo:review"
    assert activation.get_auto_loaded_skill_ids("plugin-thread") == []


def test_deleting_thread_removes_implicit_activation_state(tmp_path, monkeypatch):
    _skills, activation = _reload_skill_modules(tmp_path)
    import row_bot.threads as threads

    threads = importlib.reload(threads)
    thread_id = threads.create_thread("Delete skill state", seed_default_skills=False)
    activation.load_auto_skill(thread_id, "implicit", available_ids={"implicit"})

    threads._delete_thread(thread_id)

    assert activation.get_auto_loaded_skill_ids(thread_id) == []
