"""Designer project-brief helpers.

Builds the canonical initial user request used to generate the first draft
from a project's stored setup brief.
"""

from __future__ import annotations

from row_bot.designer.state import DESIGNER_MODES, DesignerProject, ProjectBrief


def brief_has_build_content(brief: ProjectBrief | None) -> bool:
    """Use the same persisted-brief rule for setup UI and project creation."""

    return bool(brief and not brief.is_empty())


def project_has_build_brief(project: DesignerProject) -> bool:
    """Return True when the project has any stored setup brief content."""

    return brief_has_build_content(project.brief)


def build_initial_design_request(project: DesignerProject) -> str:
    """Build the canonical first-draft request from the project's stored brief."""

    brief = project.brief or ProjectBrief()
    # Phase 2.3.F — Derive the output label from project.mode rather
    # than the legacy free-text brief.output_type field. The mode is
    # set by the setup dialog (2.3.C) and is the single source of
    # truth. If brief.output_type still carries a value (legacy JSON,
    # or a tool caller) we honour it as an override.
    mode = getattr(project, "mode", "deck") or "deck"
    mode_info = DESIGNER_MODES.get(mode, DESIGNER_MODES["deck"])
    mode_label = mode_info["label"]
    output_type = brief.output_type.strip() or mode_label.lower()
    lines = [
        f"Create the first draft of this {output_type} using the current project as the starting point.",
    ]

    if mode != "deck":
        lines.append(
            f"Project type: {mode_info['label']} (mode='{mode}'). "
            "Follow the interactive-runtime rules — navigation and state "
            "toggles must use data-row-bot-action attributes only, never raw "
            "JavaScript."
        )

    if project.template_id and not project.template_id.startswith("blank_"):
        lines.append(
            "Use the selected template as a starting structure, but replace placeholder content with real content tailored to the brief."
        )
    else:
        lines.append(
            "Turn the current blank starting point into a real first draft with complete content and layout."
        )

    if brief.build_description.strip():
        lines.append(f"What to build: {brief.build_description.strip()}")
    if brief.audience.strip():
        lines.append(f"Audience: {brief.audience.strip()}")
    if brief.tone.strip():
        lines.append(f"Tone: {brief.tone.strip()}")
    if brief.length.strip():
        lines.append(f"Desired length or scope: {brief.length.strip()}")
    if brief.reference_notes.strip():
        lines.append(f"Reference notes: {brief.reference_notes.strip()}")
    if brief.brand_preset.strip():
        lines.append(f"The project was set up with the brand preset '{brief.brand_preset.strip()}'.")
    if brief.brand_url.strip():
        lines.append(f"Brand source URL provided at setup: {brief.brand_url.strip()}")

    if project.brand is not None:
        lines.append("A brand is already configured on the project. Use it consistently across the design.")

    lines.append(
        "Produce a real, editable first draft the user can refine further, not a placeholder shell or generic outline."
    )
    return "\n".join(lines)
