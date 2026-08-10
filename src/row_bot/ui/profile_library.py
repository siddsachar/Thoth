"""Agent Profile library, view dialog, and simplified editor UI."""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Sequence

from nicegui import ui
from nicegui.context import context

from row_bot.ui.state import AppState, P, _active_generations

logger = logging.getLogger(__name__)

_CAPABILITY_OPTIONS = {
    "read_only": "Read-only behavior",
    "write_capable": "Write-capable behavior",
    "orchestrator": "Coordinator behavior",
}

_CONTEXT_OPTIONS = {
    "auto": "Automatic",
    "focused": "Focused context",
    "recent": "Recent conversation",
    "empty": "Minimal context",
    "full": "Full thread",
    "resume": "Resume child context",
}

_WORKSPACE_OPTIONS = {
    "auto": "Use surface default",
    "read_only": "No file edits",
    "single_writer": "Use parent working copy",
    "worktree": "Use Worktree",
}

_APPROVAL_OPTIONS = {
    "inherit": "Inherit thread approval",
    "block": "Block risky actions",
    "approve": "Ask before risky actions",
    "allow_all": "Auto-approve within parent limit",
}

_TOOL_ACCESS_OPTIONS = {
    "inherit": "Inherit enabled tools",
    "select": "Selected tools only",
}

_TOOL_GROUP_NAMES = ("Core", "MCP", "Plugins", "Custom Tools", "Unavailable")

_CAPABILITY_HELP = {
    "read_only": "Behavioral instruction to inspect and reason. Selected tools are the hard boundary.",
    "write_capable": "May change files or state when the selected or inherited tools allow it.",
    "orchestrator": "May coordinate work and agents within approval, workspace, and tool limits.",
}

_CONTEXT_HELP = {
    "auto": "Row-Bot chooses the smallest useful context packet.",
    "focused": "Starts with only the relevant task, files, and constraints.",
    "recent": "Includes the recent conversation window.",
    "empty": "Starts clean except for the task and profile instructions.",
    "full": "Uses the full available thread context when it fits.",
    "resume": "Uses the child agent's existing context when resuming work.",
}

_WORKSPACE_HELP = {
    "auto": "Uses the current chat, workflow, or studio workspace behavior.",
    "read_only": "Can inspect workspace files but should not edit them.",
    "single_writer": "Can edit the parent working copy, with writer locking for write-capable helpers.",
    "worktree": "Gives this helper its own local git Worktree before it edits files.",
}

_APPROVAL_HELP = {
    "inherit": "Uses the current thread or workspace approval mode.",
    "block": "Blocks risky actions even if the parent is looser.",
    "approve": "Asks before risky actions, capped by the parent mode.",
    "allow_all": "Auto-approves only when the parent mode also allows it.",
}

_PROFILE_LIBRARY_DRAWER_CSS = """
<style>
[data-profile-library-drawer="1"],
.q-drawer:has(> .q-drawer__content.row-bot-profile-library-drawer) {
  left: 300px !important;
  top: 8px !important;
  bottom: 8px !important;
  width: min(460px, calc(100vw - 324px)) !important;
  height: calc(100vh - 16px) !important;
  padding: 0 !important;
  overflow: hidden !important;
  z-index: 2200 !important;
  border-radius: 8px !important;
  border: 1px solid rgba(96, 165, 250, 0.28) !important;
}
@media (max-width: 760px) {
  [data-profile-library-drawer="1"],
  .q-drawer:has(> .q-drawer__content.row-bot-profile-library-drawer) {
    left: 0 !important;
    top: 0 !important;
    bottom: 0 !important;
    width: min(460px, 100vw) !important;
    height: 100vh !important;
    border-radius: 0 !important;
  }
}
</style>
"""


def _friendly_shortcut(value: str) -> str:
    """Return a backend-valid profile shortcut from a display name."""

    try:
        from row_bot.agent_profiles import normalize_profile_slug

        slug = normalize_profile_slug(value)
    except Exception:
        slug = re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")
    if not slug:
        return ""
    if not slug[0].isalpha():
        slug = f"profile_{slug}"
    if len(slug) == 1:
        slug = f"{slug}_profile"
    return slug[:64]


def _json_field(profile: dict, key: str) -> dict:
    value = profile.get(key)
    return dict(value) if isinstance(value, dict) else {}


def _selected_list(value: Any) -> list[str]:
    if isinstance(value, list):
        items = value
    elif isinstance(value, tuple):
        items = list(value)
    elif value:
        items = [value]
    else:
        items = []
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _tool_items(current_allow: Sequence[str] | None = None) -> list[dict[str, Any]]:
    try:
        from row_bot.agent_tool_catalog import list_agent_tool_catalog

        records = list_agent_tool_catalog(include_unavailable=True)
    except Exception:
        logger.debug("Could not load enabled tools for Agent Profile editor", exc_info=True)
        records = []
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        tool_id = str(record.get("id") or "").strip()
        if not tool_id:
            continue
        seen.add(tool_id)
        items.append({
            "name": tool_id,
            "label": str(record.get("label") or tool_id),
            "description": str(record.get("description") or ""),
            "source": str(record.get("source") or "core"),
            "group": str(record.get("group") or "Core"),
            "selectable": bool(record.get("selectable", True)),
            "enabled": bool(record.get("enabled", True)),
        })
    for tool_id in _selected_list(current_allow):
        if tool_id in seen:
            continue
        items.append({
            "name": tool_id,
            "label": tool_id,
            "description": "Previously selected, but this tool is not currently available.",
            "source": "unknown",
            "group": "Unavailable",
            "selectable": True,
            "enabled": False,
        })
    return items


def _skill_items(current_skills: list[str] | None = None) -> list[dict[str, Any]]:
    try:
        from row_bot import skills as skills_mod
        from row_bot.skill_discovery import collect_enabled_skill_records

        records = collect_enabled_skill_records()
        items = [
            {
                "name": record.canonical_id,
                "label": record.display_name,
                "description": record.description,
                "icon": record.icon or "auto_fix_high",
                "source": record.plugin_name or record.source,
                "pinned": (
                    not record.canonical_id.startswith("plugin:")
                    and bool(skills_mod.is_pinned(record.canonical_id))
                ),
                "available": True,
            }
            for record in records
        ]
        seen = {item["name"] for item in items}
        for skill_id in _selected_list(current_skills):
            if skill_id in seen:
                continue
            items.append({
                "name": skill_id,
                "label": skill_id,
                "description": "Previously selected, but this skill is not currently available.",
                "icon": "warning_amber",
                "source": "Unavailable",
                "pinned": False,
                "available": False,
            })
    except Exception:
        logger.debug("Could not load skills for Agent Profile editor", exc_info=True)
        items = []
    return sorted(items, key=lambda item: item["label"].lower())


def _policy_summary(profile: dict) -> dict[str, Any]:
    tool_policy = _json_field(profile, "tool_policy_json")
    skill_policy = _json_field(profile, "skill_policy_json")
    context_policy = _json_field(profile, "context_policy_json")
    workspace_policy = _json_field(profile, "workspace_policy_json")
    return {
        "capability": str(tool_policy.get("capability") or "read_only"),
        "allow_tools": _selected_list(tool_policy.get("allow_tools")),
        "skills": _selected_list(skill_policy.get("skills_override")),
        "context": str(context_policy.get("default_context_mode") or "auto"),
        "workspace": str(workspace_policy.get("workspace_mode_default") or "auto"),
        "tool_scope": "selected" if _selected_list(tool_policy.get("allow_tools")) else "inherit",
    }


def _profile_group(profile: dict) -> str:
    if profile.get("source") == "builtin":
        ui_json = _json_field(profile, "ui_json")
        group = str(ui_json.get("group") or "").strip()
        return group or "Everyday"
    scope = str(profile.get("scope") or "user")
    if scope == "workspace":
        return "Workspace Profiles"
    if scope == "plugin":
        return "Plugin Profiles"
    if scope == "imported":
        return "Imported Profiles"
    return "My Profiles"


def _profile_ref(profile: dict) -> str:
    return str(profile.get("id") or profile.get("slug") or "")


def _create_profile_chat_thread(profile: dict) -> tuple[str, str]:
    from row_bot.threads import create_thread, set_thread_skills_override

    display = str(profile.get("display_name") or profile.get("slug") or "Agent")
    title = f"{display} chat"
    thread_id = create_thread(
        title,
        agent_profile_id=str(profile.get("id") or ""),
        agent_profile_slug=str(profile.get("slug") or ""),
    )
    profile_skills = _selected_list(
        _json_field(profile, "skill_policy_json").get("skills_override")
    )
    set_thread_skills_override(thread_id, profile_skills or None)
    return thread_id, title


def _start_profile_chat(
    profile: dict,
    *,
    state: AppState | None,
    p: P | None,
    rebuild_main: Callable[..., None] | None,
    rebuild_thread_list: Callable[[], None] | None,
) -> None:
    if state is None or p is None:
        return
    try:
        from row_bot.memory_extraction import set_active_thread

        thread_id, title = _create_profile_chat_thread(profile)
        display = str(profile.get("display_name") or profile.get("slug") or "Agent")
        prev = state.thread_id
        prev_gen = _active_generations.get(prev) if prev else None
        if prev_gen and prev_gen.status == "streaming":
            prev_gen.detached = True
            if prev_gen.tts_active:
                state.tts_service.stop()
                prev_gen.tts_active = False
        state.active_designer_project = None
        state.active_developer_workspace_id = None
        state.thread_id = thread_id
        state.thread_name = title
        state.thread_model_override = ""
        state.messages = []
        p.pending_files.clear()
        set_active_thread(thread_id, previous_id=prev)
        ui.notify(f"Started {display} chat", type="positive")
        if rebuild_main is not None:
            rebuild_main()
        if rebuild_thread_list is not None:
            rebuild_thread_list()
    except Exception as exc:
        logger.warning("Could not start profile chat: %s", exc)
        ui.notify(f"Could not start Agent Profile chat: {exc}", type="negative")


def open_profile_editor_dialog(
    profile: dict | None = None,
    *,
    on_saved: Callable[[], None] | None = None,
) -> None:
    """Open the simplified Agent Profile create/edit dialog."""

    from row_bot.agent_profiles import save_agent_profile

    existing = dict(profile or {})
    is_edit = bool(existing)
    tool_policy = _json_field(existing, "tool_policy_json")
    skill_policy = _json_field(existing, "skill_policy_json")
    context_policy = _json_field(existing, "context_policy_json")
    workspace_policy = _json_field(existing, "workspace_policy_json")
    approval_policy = _json_field(existing, "approval_policy_json")

    current_allow = _selected_list(tool_policy.get("allow_tools"))
    initial_tool_access = "select" if current_allow else "inherit"

    purpose_value = str(existing.get("description") or existing.get("when_to_use") or "")
    routing_value = str(existing.get("when_to_use") or "")
    if routing_value == purpose_value:
        routing_value = ""

    tool_items = _tool_items(current_allow)
    tool_names = [item["name"] for item in tool_items if item.get("selectable", True)]
    initial_tool_selection = set(current_allow or tool_names)
    current_skills = _selected_list(skill_policy.get("skills_override"))
    skill_items = _skill_items(current_skills)
    initial_skill_selection = (
        set(current_skills)
        if is_edit
        else {item["name"] for item in skill_items if item.get("pinned")}
    )

    with ui.dialog() as dlg, ui.card().classes("q-pa-md").style(
        "width: min(920px, 96vw); max-height: min(88vh, 860px); overflow-y: auto;"
    ):
        ui.label("Edit Agent Profile" if is_edit else "Create Agent Profile").classes(
            "text-subtitle1 font-bold"
        )
        ui.label(
            "Profiles set behavior and optional tool boundaries; selected tools are enforced, inherited tools are broad/default."
        ).classes("text-xs text-grey-6")

        display_name = ui.input(
            "Name",
            value=str(existing.get("display_name") or ""),
            placeholder="Example: Careful reviewer",
        ).classes("w-full").props("dense outlined")
        shortcut = ui.input(
            "Profile command",
            value=str(existing.get("slug") or ""),
            placeholder="Used by /profile review and /agent review <task>",
        ).classes("w-full").props("dense outlined")
        purpose = ui.textarea(
            "Purpose",
            value=purpose_value,
            placeholder="What this profile is good at, in one or two sentences.",
        ).classes("w-full").props("dense outlined autogrow")
        instructions = ui.textarea(
            "Instructions",
            value=str(existing.get("instructions") or ""),
            placeholder=(
                "Detailed behavior, style, constraints, output format, things to avoid, "
                "and success criteria for this specialist."
            ),
        ).classes("w-full").props("dense outlined rows=8")

        def _policy_select(
            options: dict[str, str],
            *,
            value: str,
            label: str,
            help_map: dict[str, str],
        ):
            with ui.column().classes("w-full gap-0"):
                control = ui.select(options, value=value, label=label).props(
                    "dense outlined options-dense"
                ).classes("w-full")
                help_label = ui.label(help_map.get(value, "")).classes(
                    "text-xs text-grey-6 q-mt-xs"
                )

                def _update_help(e) -> None:
                    help_label.text = help_map.get(str(e.value or ""), "")
                    help_label.update()

                control.on_value_change(_update_help)
                return control

        with ui.grid(columns=2).classes("w-full gap-3 q-mt-sm"):
            capability = _policy_select(
                _CAPABILITY_OPTIONS,
                value=str(tool_policy.get("capability") or "read_only"),
                label="Capability",
                help_map=_CAPABILITY_HELP,
            )
            context_mode = _policy_select(
                _CONTEXT_OPTIONS,
                value=str(context_policy.get("default_context_mode") or "auto"),
                label="Context",
                help_map=_CONTEXT_HELP,
            )
            workspace_mode = _policy_select(
                _WORKSPACE_OPTIONS,
                value=str(workspace_policy.get("workspace_mode_default") or "read_only"),
                label="File editing workspace",
                help_map=_WORKSPACE_HELP,
            )
            approval_mode = _policy_select(
                _APPROVAL_OPTIONS,
                value=str(approval_policy.get("mode") or "inherit"),
                label="Approval",
                help_map=_APPROVAL_HELP,
            )

        tool_checkboxes: dict[str, Any] = {}
        with ui.column().classes("w-full gap-2 q-mt-md"):
            ui.label("Tools").classes("text-xs font-bold text-grey-5")
            ui.label(
                "Inherit all globally enabled tools, or select exactly which tools this profile may use. Selected tools are the hard runtime boundary."
            ).classes("text-xs text-grey-6")
            tool_access = ui.toggle(
                _TOOL_ACCESS_OPTIONS,
                value=initial_tool_access,
            ).props("dense no-caps toggle-color=primary")
            tool_list = ui.column().classes("w-full gap-1 q-pa-sm rounded-borders").style(
                "border: 1px solid rgba(255,255,255,0.16); max-height: 220px; overflow-y: auto;"
            )
            tool_list.set_visibility(initial_tool_access == "select")
            with tool_list:
                if tool_items:
                    current_group = ""
                    for item in tool_items:
                        group = str(item.get("group") or "Core")
                        if group != current_group:
                            current_group = group
                            ui.label(group).classes("text-xs font-bold text-grey-5 q-mt-xs")
                        with ui.row().classes("w-full items-start no-wrap gap-2 q-py-xs"):
                            selectable = bool(item.get("selectable", True))
                            checkbox = ui.checkbox(
                                value=selectable and item["name"] in initial_tool_selection
                            ).props("dense")
                            if selectable:
                                tool_checkboxes[item["name"]] = checkbox
                            else:
                                checkbox.props("disable")
                            with ui.column().classes("gap-0").style("min-width: 0;"):
                                ui.label(item["label"]).classes("text-sm text-weight-medium")
                                source = str(item.get("source") or "").strip()
                                meta = item["name"]
                                if source:
                                    meta = f"{meta} - {source}"
                                if not bool(item.get("enabled", True)) and selectable:
                                    meta += " - currently unavailable"
                                if item.get("description"):
                                    meta = f"{meta} - {item['description']}"
                                ui.label(meta).classes("text-xs text-grey-6").style("white-space: normal;")
                else:
                    ui.label("No enabled tools are available.").classes("text-sm text-grey-6")

            def _on_tool_access_change(e) -> None:
                tool_list.set_visibility(str(e.value or "") == "select")

            tool_access.on_value_change(_on_tool_access_change)

        skill_checkboxes: dict[str, Any] = {}
        with ui.column().classes("w-full gap-2 q-mt-md"):
            ui.label("Pinned skills for this profile").classes("text-xs font-bold text-grey-5")
            ui.label(
                "Selected skills start active when this profile is used. Tool access remains the hard runtime boundary."
            ).classes("text-xs text-grey-6")
            with ui.column().classes("w-full gap-1 q-pa-sm rounded-borders").style(
                "border: 1px solid rgba(255,255,255,0.16); max-height: 240px; overflow-y: auto;"
            ):
                if skill_items:
                    for item in skill_items:
                        with ui.row().classes("w-full items-start no-wrap gap-2 q-py-xs"):
                            checkbox = ui.checkbox(value=item["name"] in initial_skill_selection).props("dense")
                            skill_checkboxes[item["name"]] = checkbox
                            with ui.column().classes("gap-0").style("min-width: 0;"):
                                prefix = f"{item['icon']} " if item.get("icon") else ""
                                ui.label(f"{prefix}{item['label']}").classes("text-sm text-weight-medium")
                                meta = item["name"]
                                source = str(item.get("source") or "").strip()
                                if source:
                                    meta += f" - {source}"
                                if item.get("pinned"):
                                    meta += " - pinned globally"
                                if item.get("description"):
                                    meta += f" - {item['description']}"
                                ui.label(meta).classes("text-xs text-grey-6").style("white-space: normal;")
                else:
                    ui.label("No enabled skills are available.").classes("text-sm text-grey-6")

        with ui.expansion("Advanced", icon="tune", value=False).classes("w-full q-mt-sm"):
            routing_hint = ui.textarea(
                "Routing hint",
                value=routing_value,
                placeholder="Optional: when another agent should choose this profile.",
            ).classes("w-full").props("dense outlined autogrow")

        shortcut_touched = {"value": bool(existing.get("slug"))}

        def _mark_shortcut_touched(_e=None) -> None:
            shortcut_touched["value"] = True

        def _auto_shortcut(e) -> None:
            if shortcut_touched["value"]:
                return
            shortcut.value = _friendly_shortcut(str(e.value or ""))
            shortcut.update()

        shortcut.on_value_change(_mark_shortcut_touched)
        display_name.on_value_change(_auto_shortcut)

        with ui.row().classes("w-full justify-end gap-2 q-mt-md"):
            ui.button("Cancel", on_click=dlg.close).props("flat dense no-caps")

            def _save() -> None:
                try:
                    name_value = str(display_name.value or "").strip()
                    shortcut_value = str(shortcut.value or "").strip() or _friendly_shortcut(name_value)
                    purpose_text = str(purpose.value or "").strip()
                    route_text = str(routing_hint.value or "").strip() or purpose_text
                    allow_tools = (
                        [
                            name
                            for name, checkbox in tool_checkboxes.items()
                            if bool(getattr(checkbox, "value", False))
                        ]
                        if str(tool_access.value or "inherit") == "select"
                        else []
                    )
                    selected_skills = [
                        name
                        for name, checkbox in skill_checkboxes.items()
                        if bool(getattr(checkbox, "value", False))
                    ]
                    selected_capability = str(capability.value or "read_only")
                    selected_workspace = str(workspace_mode.value or "read_only")
                    payload = dict(existing)
                    payload.update({
                        "slug": shortcut_value,
                        "display_name": name_value,
                        "description": purpose_text,
                        "when_to_use": route_text,
                        "instructions": str(instructions.value or ""),
                        "tool_policy_json": {
                            "capability": selected_capability,
                            "allow_tools": allow_tools,
                            "allow_delegation": bool(tool_policy.get("allow_delegation", False)),
                        },
                        "skill_policy_json": {"skills_override": selected_skills},
                        "context_policy_json": {
                            **context_policy,
                            "default_context_mode": context_mode.value or "auto",
                        },
                        "workspace_policy_json": {
                            **workspace_policy,
                            "workspace_mode_default": selected_workspace,
                            "write_lock_required": selected_capability in {"write_capable", "orchestrator"},
                            "worktree_allowed": selected_workspace == "worktree",
                        },
                        "approval_policy_json": {
                            **approval_policy,
                            "mode": approval_mode.value or "inherit",
                        },
                        "scope": existing.get("scope") or "user",
                        "source": existing.get("source") or "user_created",
                        "enabled": bool(existing.get("enabled", True)),
                    })
                    save_agent_profile(payload)
                    ui.notify("Agent Profile saved", type="positive")
                    dlg.close()
                    if on_saved is not None:
                        on_saved()
                except Exception as exc:
                    ui.notify(f"Could not save Agent Profile: {exc}", type="negative")

            ui.button("Save", on_click=_save).props("unelevated dense no-caps color=primary")

    dlg.open()


def open_profile_view_dialog(
    profile_or_ref: dict | str,
    *,
    state: AppState | None = None,
    p: P | None = None,
    rebuild_main: Callable[..., None] | None = None,
    rebuild_thread_list: Callable[[], None] | None = None,
    on_refresh: Callable[[], None] | None = None,
) -> None:
    """Open a read-only Agent Profile dialog."""

    try:
        from row_bot.agent_profiles import duplicate_agent_profile, get_agent_profile

        if isinstance(profile_or_ref, dict):
            profile = dict(profile_or_ref)
        else:
            profile = get_agent_profile(str(profile_or_ref or ""), enabled_only=False) or {}
    except Exception as exc:
        ui.notify(f"Could not open Agent Profile: {exc}", type="negative")
        return
    if not profile:
        ui.notify("Agent Profile not found", type="warning")
        return

    policy = _policy_summary(profile)
    is_builtin = profile.get("source") == "builtin"
    title = str(profile.get("display_name") or profile.get("slug") or "Agent Profile")

    with ui.dialog() as dlg, ui.card().classes("q-pa-md").style(
        "width: min(780px, 96vw); max-height: min(86vh, 760px); overflow-y: auto;"
    ):
        with ui.row().classes("w-full items-start no-wrap gap-2"):
            ui.icon(str(_json_field(profile, "ui_json").get("icon") or "badge"), size="sm").classes("text-primary")
            with ui.column().classes("gap-0").style("min-width: 0; flex: 1;"):
                ui.label(title).classes("text-subtitle1 font-bold")
                ui.label(f"Profile command: {profile.get('slug') or ''}").classes("text-xs text-grey-6")
            ui.badge("Built-in" if is_builtin else "Custom", color="primary" if is_builtin else "grey-7").props("outline")
        purpose = str(profile.get("description") or profile.get("when_to_use") or "").strip()
        if purpose:
            ui.label("Purpose").classes("text-xs font-bold text-grey-5 q-mt-sm")
            ui.label(purpose).classes("text-sm")
        instructions = str(profile.get("instructions") or "").strip()
        if instructions:
            ui.label("Instructions").classes("text-xs font-bold text-grey-5 q-mt-sm")
            ui.label(instructions).classes("text-sm").style("white-space: pre-wrap;")

        with ui.grid(columns=2).classes("w-full gap-2 q-mt-sm"):
            ui.label(f"Behavior: {_CAPABILITY_OPTIONS.get(policy['capability'], policy['capability'])}").classes("text-xs")
            ui.label(f"Context: {_CONTEXT_OPTIONS.get(policy['context'], policy['context'])}").classes("text-xs")
            ui.label(f"Workspace: {_WORKSPACE_OPTIONS.get(policy['workspace'], policy['workspace'])}").classes("text-xs")
            ui.label(f"Scope: {profile.get('scope') or 'user'}").classes("text-xs")

        def _list_line(label: str, values: list[str]) -> None:
            text = ", ".join(values) if values else "None"
            ui.label(f"{label}: {text}").classes("text-xs text-grey-6").style("white-space: normal;")

        ui.separator().classes("q-my-sm")
        if policy["allow_tools"]:
            _list_line("Tool boundary", policy["allow_tools"])
        else:
            ui.label(
                "Tool boundary: inherits all globally enabled tools (broad/default; no profile allow-list)."
            ).classes("text-xs text-grey-6").style("white-space: normal;")
        _list_line("Pinned skills", policy["skills"])

        with ui.row().classes("w-full justify-end gap-2 q-mt-md"):
            ui.button("Close", on_click=dlg.close).props("flat dense no-caps")

            def _duplicate() -> None:
                try:
                    duplicate = duplicate_agent_profile(_profile_ref(profile))
                    ui.notify("Agent Profile duplicated", type="positive")
                    dlg.close()
                    if on_refresh is not None:
                        on_refresh()
                    open_profile_editor_dialog(duplicate, on_saved=on_refresh)
                except Exception as exc:
                    ui.notify(f"Could not duplicate Agent Profile: {exc}", type="negative")

            ui.button("Duplicate to customize", icon="content_copy", on_click=_duplicate).props(
                "outline dense no-caps"
            )
            if state is not None and p is not None:
                ui.button(
                    "Start chat",
                    icon="chat",
                    on_click=lambda: (
                        dlg.close(),
                        _start_profile_chat(
                            profile,
                            state=state,
                            p=p,
                            rebuild_main=rebuild_main,
                            rebuild_thread_list=rebuild_thread_list,
                        ),
                    ),
                ).props("outline dense no-caps")
            if not is_builtin:
                ui.button(
                    "Edit",
                    icon="edit",
                    on_click=lambda: (
                        dlg.close(),
                        open_profile_editor_dialog(profile, on_saved=on_refresh),
                    ),
                ).props("unelevated dense no-caps color=primary")
    dlg.open()


def build_profile_library(
    state: AppState,
    p: P,
    *,
    rebuild_main: Callable[..., None],
    rebuild_thread_list: Callable[[], None],
) -> None:
    """Render the durable Agent Profile library for the left sidebar."""

    ui.add_head_html(_PROFILE_LIBRARY_DRAWER_CSS)
    container = ui.column().classes("w-full gap-0")
    panel_state: dict[str, Any] = {"drawer": None, "body": None, "open": False}
    toggle_btn_ref: dict[str, Any] = {"button": None}
    summary_label_ref: dict[str, Any] = {"label": None}

    def _load_profiles() -> list[dict]:
        try:
            from row_bot.agent_profiles import list_agent_profiles

            return list_agent_profiles(enabled_only=False, include_builtins=True)
        except Exception as exc:
            logger.warning("Agent Profile Library unavailable: %s", exc)
            return []

    def _summary(profiles: list[dict]) -> str:
        builtins = sum(1 for profile in profiles if profile.get("source") == "builtin")
        custom = max(0, len(profiles) - builtins)
        bits = []
        if builtins:
            bits.append(f"{builtins} built-in")
        if custom:
            bits.append(f"{custom} custom")
        return " | ".join(bits) if bits else "No profiles yet"

    def _render_profile_rows(
        profiles: list[dict],
        *,
        on_refresh: Callable[[], None],
    ) -> None:
        from row_bot.agent_profiles import delete_agent_profile, duplicate_agent_profile, save_agent_profile

        if not profiles:
            ui.label("No Agent Profiles available").classes("text-xs text-grey-7 q-ml-sm").style(
                "opacity: 0.6;"
            )
            return
        grouped: dict[str, list[dict]] = {}
        for profile in profiles:
            grouped.setdefault(_profile_group(profile), []).append(profile)
        for group_name in (
            "Everyday",
            "Work",
            "Creative",
            "Developer",
            "Advanced/Internal",
            "My Profiles",
            "Workspace Profiles",
            "Plugin Profiles",
            "Imported Profiles",
        ):
            rows = grouped.get(group_name) or []
            if not rows:
                continue
            with ui.expansion(group_name, icon="badge", value=(group_name == "Everyday")).classes("w-full"):
                for profile in rows:
                    is_builtin = profile.get("source") == "builtin"
                    enabled = bool(profile.get("enabled", True))
                    policy = _policy_summary(profile)
                    ui_json = _json_field(profile, "ui_json")
                    desc = str(profile.get("description") or "")
                    tool_count = len(policy["allow_tools"])
                    skill_count = len(policy["skills"])
                    chips = [
                        _CAPABILITY_OPTIONS.get(policy["capability"], policy["capability"]),
                        _CONTEXT_OPTIONS.get(policy["context"], policy["context"]),
                    ]
                    if tool_count:
                        chips.append(f"{tool_count} selected tools")
                    else:
                        chips.append("inherits enabled tools")
                    if skill_count:
                        chips.append(f"{skill_count} skills")

                    with ui.element("div").classes("w-full q-py-xs").style(
                        "display: grid; grid-template-columns: 22px minmax(0, 1fr) auto; "
                        "column-gap: 8px; align-items: center; min-width: 0;"
                    ):
                        ui.icon(str(ui_json.get("icon") or "smart_toy"), size="xs").classes(
                            "text-primary" if enabled else "text-grey-6"
                        )
                        with ui.column().classes("gap-0").style("min-width: 0; overflow: hidden;"):
                            with ui.row().classes("items-center no-wrap gap-1").style("min-width: 0;"):
                                ui.label(
                                    str(profile.get("display_name") or profile.get("slug"))
                                ).classes("text-xs font-bold ellipsis").style("min-width: 0;")
                                if not enabled:
                                    ui.badge("off", color="warning").props("outline dense").style("flex-shrink: 0;")
                            if desc:
                                ui.label(desc).classes("text-xs text-grey-6 ellipsis").style("max-width: 100%;")
                            ui.label(" | ".join(chips)).classes("text-xs text-grey-7 ellipsis").style("max-width: 100%;")
                        with ui.row().classes("items-center no-wrap gap-0").style("flex-shrink: 0;"):
                            if enabled:
                                ui.button(
                                    icon="chat",
                                    on_click=lambda p0=profile: _start_profile_chat(
                                        p0,
                                        state=state,
                                        p=p,
                                        rebuild_main=rebuild_main,
                                        rebuild_thread_list=rebuild_thread_list,
                                    ),
                                ).props("round flat dense size=xs").tooltip("Start chat")
                            ui.button(
                                icon="visibility",
                                on_click=lambda p0=profile: open_profile_view_dialog(
                                    p0,
                                    state=state,
                                    p=p,
                                    rebuild_main=rebuild_main,
                                    rebuild_thread_list=rebuild_thread_list,
                                    on_refresh=on_refresh,
                                ),
                            ).props("round flat dense size=xs").tooltip("View Profile")
                            ui.button(
                                icon="content_copy",
                                on_click=lambda p0=profile: _duplicate_profile_from_panel(p0),
                            ).props("round flat dense size=xs").tooltip("Duplicate")
                            if not is_builtin:
                                ui.button(
                                    icon="edit",
                                    on_click=lambda p0=profile: open_profile_editor_dialog(
                                        p0,
                                        on_saved=on_refresh,
                                    ),
                                ).props("round flat dense size=xs").tooltip("Edit")
                                ui.button(
                                    icon="toggle_on" if enabled else "toggle_off",
                                    on_click=lambda p0=profile: (
                                        save_agent_profile({**p0, "enabled": not bool(p0.get("enabled", True))}),
                                        on_refresh(),
                                    ),
                                ).props("round flat dense size=xs").tooltip("Disable" if enabled else "Enable")
                                ui.button(
                                    icon="delete",
                                    on_click=lambda p0=profile: (
                                        delete_agent_profile(_profile_ref(p0)),
                                        ui.notify("Agent Profile deleted", type="info"),
                                        on_refresh(),
                                    ),
                                ).props("round flat dense size=xs color=negative").tooltip("Delete")

    def _set_profile_panel_open(opened: bool) -> None:
        panel_state["open"] = bool(opened)
        toggle_btn = toggle_btn_ref.get("button")
        if toggle_btn is not None:
            toggle_btn._props["icon"] = "chevron_left" if panel_state["open"] else "chevron_right"
            toggle_btn.update()

    def _refresh_sidebar_summary(profiles: list[dict] | None = None) -> None:
        label = summary_label_ref.get("label")
        if label is not None:
            label.set_text(_summary(profiles if profiles is not None else _load_profiles()))

    def _rebuild_profile_panel(profiles: list[dict] | None = None) -> None:
        panel_body = panel_state.get("body")
        if panel_body is None:
            return
        panel_body.clear()
        with panel_body:
            _render_profile_rows(
                profiles if profiles is not None else _load_profiles(),
                on_refresh=lambda: _refresh_profile_panel_after_action(),
            )

    def _refresh_profile_surfaces(profiles: list[dict] | None = None) -> None:
        refreshed = profiles if profiles is not None else _load_profiles()
        _refresh_sidebar_summary(refreshed)
        _rebuild_profile_panel(refreshed)

    def _close_profile_panel() -> None:
        drawer = panel_state.get("drawer")
        if drawer is not None:
            try:
                drawer.hide()
            except Exception:
                logger.debug("Profile panel close failed", exc_info=True)
        _set_profile_panel_open(False)

    def _ensure_profile_panel() -> Any:
        if panel_state.get("drawer") is not None:
            return panel_state["drawer"]

        with context.client.content:
            with ui.left_drawer(value=False, fixed=True, bordered=True, elevated=True).style(
                "left: 300px; width: min(460px, calc(100vw - 324px)); "
                "height: calc(100vh - 16px); top: 8px; bottom: 8px; "
                "padding: 0; overflow: hidden; z-index: 2200; "
                "border-radius: 8px; border: 1px solid rgba(96, 165, 250, 0.28);"
            ).classes("row-bot-panel-card row-bot-profile-library-drawer").props(
                "overlay no-swipe-open no-swipe-close no-swipe-backdrop behavior=desktop width=460"
            ) as drawer:
                drawer._props["data-profile-library-drawer"] = "1"
                drawer.on(
                    "update:model-value",
                    lambda e: _set_profile_panel_open(bool(getattr(e, "value", False))),
                )
                with ui.column().classes("w-full no-wrap").style("height: 100%; overflow: hidden;"):
                    with ui.row().classes("w-full items-center no-wrap gap-1 q-pa-sm q-pb-xs"):
                        ui.icon("badge", size="xs").classes("text-primary")
                        ui.label("Agent profiles").classes("text-subtitle2 font-bold")
                        ui.space()
                        ui.button(
                            icon="add",
                            on_click=lambda: open_profile_editor_dialog(
                                None,
                                on_saved=lambda: _refresh_profile_panel_after_action(),
                            ),
                        ).props("round flat dense size=xs").tooltip("Create Agent Profile")
                        ui.button(
                            icon="close",
                            on_click=_close_profile_panel,
                        ).props("round flat dense size=xs").tooltip("Close")
                    panel_state["body"] = ui.column().classes("w-full gap-0 q-px-sm q-pb-sm").style(
                        "overflow-y: auto; min-height: 0;"
                    )
        panel_state["drawer"] = drawer
        _rebuild_profile_panel()
        return drawer

    def _refresh_profile_panel_after_action() -> None:
        drawer = _ensure_profile_panel()
        _refresh_profile_surfaces()
        try:
            drawer.show()
        except Exception:
            logger.debug("Profile panel reopen failed after refresh", exc_info=True)
        _set_profile_panel_open(True)

    def _duplicate_profile_from_panel(profile: dict) -> None:
        try:
            from row_bot.agent_profiles import duplicate_agent_profile

            duplicate_agent_profile(_profile_ref(profile))
            ui.notify("Agent Profile duplicated", type="positive")
            _refresh_profile_panel_after_action()
        except Exception as exc:
            ui.notify(f"Could not duplicate Agent Profile: {exc}", type="negative")

    def _toggle_profile_panel() -> None:
        if panel_state["open"]:
            _close_profile_panel()
            return
        drawer = _ensure_profile_panel()
        _rebuild_profile_panel()
        drawer.show()
        _set_profile_panel_open(True)

    def _rebuild_library() -> None:
        container.clear()
        profiles = _load_profiles()
        with container:
            with ui.row().classes("w-full items-center no-wrap gap-1"):
                ui.icon("badge", size="xs").classes("text-primary")
                ui.label("Agent profiles").classes("text-subtitle2")
                ui.space()
                ui.button(
                    icon="add",
                    on_click=lambda: open_profile_editor_dialog(None, on_saved=_refresh_profile_surfaces),
                ).props("round flat dense size=xs").tooltip("Create Agent Profile")
                toggle_btn_ref["button"] = ui.button(
                    icon="chevron_left" if panel_state["open"] else "chevron_right",
                    on_click=_toggle_profile_panel,
                ).props("round flat dense size=xs data-docs-id=profile-library-toggle").tooltip(
                    "Toggle Agent profiles panel"
                )
            summary_label_ref["label"] = ui.label(_summary(profiles)).classes(
                "text-xs text-grey-7 q-ml-sm"
            ).style("opacity: 0.72;")

    _rebuild_library()
