from __future__ import annotations

from row_bot.designer.briefing import brief_has_build_content, project_has_build_brief
from row_bot.designer.prompt import build_designer_prompt
from row_bot.designer.setup_flow import prepare_project_creation
from row_bot.designer.state import DesignerProject, ProjectBrief


def test_document_prompt_allows_brief_progress_without_repeating_body() -> None:
    project = DesignerProject(name="Document Prompt Contract", mode="document")

    prompt = build_designer_prompt(project)

    assert "Brief live progress updates" in prompt
    assert "inspecting, editing" in prompt
    assert "never repeat the full document body in" in prompt
    assert "do not paste document/page body content into chat" in prompt
    assert "designer_set_pages" in prompt
    assert "designer_update_page" in prompt


def test_valid_designer_brief_enables_combined_create_and_first_draft() -> None:
    brief = ProjectBrief(audience="Engineering leaders", tone="Concise")

    project, initial_prompt = prepare_project_creation(
        "blank_deck",
        project_name="Valid brief",
        brief=brief,
        auto_build=True,
        mode="deck",
    )

    assert brief_has_build_content(brief) is True
    assert project_has_build_brief(project) is True
    assert initial_prompt is not None
    assert "Engineering leaders" in initial_prompt
