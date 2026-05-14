from __future__ import annotations

import asyncio
import logging
import time
from functools import partial
from pathlib import Path
from typing import Callable

from nicegui import run, ui

from developer.storage import (
    add_or_update_local_workspace,
    clone_repository,
    detect_git_summary,
    ensure_workspace_thread,
    get_workspace,
    list_clone_parent_folders,
    list_workspaces,
    remove_workspace,
    set_workspace_approval_mode,
    set_workspace_execution_settings,
    suggested_clone_name,
    workspace_updated_label,
)
from developer.tool_capsules import (
    classify_custom_tool_command,
    clone_capsule_repository,
    create_custom_tool_draft,
    create_tool_from_draft,
    custom_tool_command_needs_query,
    DEFAULT_CUSTOM_TOOL_TEST_QUERY,
    delete_custom_tool_draft,
    enable_created_custom_tool_from_draft,
    list_custom_tool_drafts,
    list_capsules,
    promote_capsule,
    promote_created_custom_tool_from_draft,
    propose_capsule_manifest,
    register_capsule,
    remove_capsule,
    run_capsule_command,
    run_custom_tool_test_command,
    set_capsule_enabled,
    write_capsule_manifest,
)
from developer.git import create_branch, suggest_feature_branch
from developer.inspector_snapshot import (
    InspectorSnapshot,
    get_snapshot,
    request_snapshot_refresh,
    update_snapshot_approval_mode,
)
from developer.sandbox import decide_action
from developer.state import DeveloperWorkspace
from ui.chat_components import build_chat_input_bar, build_chat_messages, build_file_upload
from ui.helpers import browse_folder
from ui.state import AppState, P
from ui.timer_utils import safe_timer, safe_ui_task


logger = logging.getLogger(__name__)


_APPROVAL_MODE_HELP: dict[str, str] = {
    "read_only": "Read only. Edits, commands, git changes, and pushes are blocked.",
    "ask": "Reads and safe checks can run. Edits, installs, commits, pushes, and servers ask first.",
    "auto_edit": "Can edit files and create branches. Shell commands, installs, servers, commits, and pushes ask first.",
    "agent_run": "Can edit files, create branches, and start local run loops or dev servers. Installs, commits, and pushes still ask first.",
}


_DEVELOPER_QUICK_ACTIONS: tuple[tuple[str, str, str], ...] = (
    (
        "Review this repo",
        "rate_review",
        "Review this repository for the highest-risk correctness, security, test, and maintainability issues. Inspect the relevant files first, then give findings ordered by severity.",
    ),
    (
        "Fix failing tests",
        "science",
        "Find the failing test surface for this repository, run the relevant test command if one is available, and apply the smallest safe fix after you understand the failure.",
    ),
    (
        "Add a feature",
        "add_task",
        "Help me add a feature to this repository. If the feature is not specified yet, ask me for the feature details before editing.",
    ),
    (
        "Explain architecture",
        "account_tree",
        "Explain this repository's architecture. Inspect the top-level structure and the main entry points, then summarize how the pieces fit together.",
    ),
    (
        "Prepare a PR",
        "merge_type",
        "Review the current diff and prepare a pull request title, summary, test notes, and risk notes. Do not push unless I ask.",
    ),
)


def build_developer_tab(
    state: AppState,
    p: P,
    *,
    rebuild_main: Callable,
    rebuild_thread_list: Callable,
    load_thread_messages: Callable[[str], list[dict]],
) -> None:
    """Render the Developer Studio home tab."""

    async def _open_workspace(workspace: DeveloperWorkspace) -> None:
        from memory_extraction import set_active_thread
        from threads import _get_thread_model_override

        prev = state.thread_id
        thread_id = await run.io_bound(ensure_workspace_thread, workspace.id)
        state.active_designer_project = None
        state.active_developer_workspace_id = workspace.id
        state.thread_id = thread_id
        state.thread_name = f"Developer: {workspace.name}"
        state.thread_model_override = await run.io_bound(_get_thread_model_override, thread_id)
        state.messages = await run.io_bound(load_thread_messages, thread_id)
        p.pending_files.clear()
        set_active_thread(thread_id, previous_id=prev)
        rebuild_main()
        rebuild_thread_list()

    async def _open_path(path: str | None) -> None:
        try:
            workspace = await run.io_bound(add_or_update_local_workspace, path or "")
        except Exception as exc:
            ui.notify(str(exc), type="negative", close_button=True)
            return
        await _open_workspace(workspace)

    async def _clone(repo_url: str | None, parent_path: str | None) -> None:
        try:
            workspace = await run.io_bound(clone_repository, repo_url or "", parent_path or "")
        except Exception as exc:
            ui.notify(str(exc), type="negative", close_button=True)
            return
        ui.notify(f"Cloned {workspace.name}", type="positive")
        await _open_workspace(workspace)

    async def _remove_workspace(workspace: DeveloperWorkspace) -> None:
        try:
            removed = await run.io_bound(remove_workspace, workspace.id)
        except Exception as exc:
            ui.notify(str(exc), type="negative", close_button=True)
            return
        if state.active_developer_workspace_id == removed.id:
            state.active_developer_workspace_id = None
        state.preferred_home_tab = "Developer"
        state.preferred_developer_tab = "Workspaces"
        rebuild_main()
        rebuild_thread_list()
        ui.notify(
            f"Removed {removed.name} from recents. Files and Developer thread history were not touched.",
            type="positive",
        )

    def _show_workspace_dialog() -> None:
        with ui.dialog() as dlg, ui.card().style(
            "width: min(760px, 92vw); padding: 18px; border-radius: 8px; "
            "border: 1px solid rgba(96,165,250,0.25);"
        ):
            with ui.row().classes("w-full items-center justify-between no-wrap"):
                with ui.column().classes("gap-0"):
                    ui.label("New Developer Workspace").classes("text-h6")
                    ui.label("Open a repo you already have, or clone one into a folder you choose.").classes(
                        "text-sm text-grey-6"
                    )
                ui.button(icon="close", on_click=dlg.close).props("flat dense round")

            ui.separator()
            with ui.tabs().classes("w-full") as setup_tabs:
                open_tab = ui.tab("Open Folder", icon="folder_open")
                clone_tab = ui.tab("Clone Repo", icon="download")
            with ui.tab_panels(setup_tabs, value=open_tab).classes("w-full").props("animated"):
                with ui.tab_panel(open_tab).classes("q-pa-none"):
                    with ui.column().classes("w-full gap-3 q-pt-md"):
                        ui.label("Use an existing local repository. Thoth stores only a workspace link.").classes(
                            "text-sm text-grey-6"
                        )
                        path_input = ui.input("Repository folder path").classes("w-full").props("outlined clearable")

                        async def _browse_open_folder() -> None:
                            picked = await browse_folder(
                                "Select repository folder",
                                str(path_input.value or ""),
                            )
                            if picked:
                                path_input.value = picked

                        async def _open_from_dialog() -> None:
                            if not str(path_input.value or "").strip():
                                ui.notify("Choose a repository folder first.", type="warning")
                                return
                            dlg.close()
                            await _open_path(path_input.value)

                        with ui.row().classes("gap-2"):
                            ui.button(
                                "Browse",
                                icon="folder_open",
                                on_click=lambda: safe_ui_task(_browse_open_folder, context="developer browse repository folder"),
                            ).props("outline no-caps")
                            ui.button(
                                "Open Workspace",
                                icon="arrow_forward",
                                on_click=lambda: safe_ui_task(_open_from_dialog, context="developer open workspace"),
                            ).props("color=primary no-caps")

                with ui.tab_panel(clone_tab).classes("q-pa-none"):
                    with ui.column().classes("w-full gap-3 q-pt-md"):
                        ui.label("Clone into an explicit parent folder. Thoth will not use its data directory.").classes(
                            "text-sm text-grey-6"
                        )
                        repo_input = ui.input("Repository URL").classes("w-full").props("outlined clearable")
                        clone_parent = ui.input("Clone into folder").classes("w-full").props("outlined clearable")

                        async def _browse_clone_folder() -> None:
                            picked = await browse_folder(
                                "Select clone parent folder",
                                str(clone_parent.value or ""),
                            )
                            if picked:
                                clone_parent.value = picked

                        parents = list_clone_parent_folders()
                        if parents:
                            with ui.row().classes("w-full flex-wrap gap-1"):
                                for parent in parents[:4]:
                                    ui.button(
                                        parent,
                                        on_click=lambda pth=parent: clone_parent.set_value(pth),
                                    ).props("flat dense no-caps size=sm")

                        def _show_target_hint() -> None:
                            if not repo_input.value or not clone_parent.value:
                                ui.notify("Enter a repo URL and clone folder first.", type="warning")
                                return
                            name = suggested_clone_name(repo_input.value or "")
                            ui.notify(f"Will clone into: {clone_parent.value}\\{name}", type="info")

                        async def _clone_from_dialog() -> None:
                            if not str(repo_input.value or "").strip() or not str(clone_parent.value or "").strip():
                                ui.notify("Enter a repo URL and clone folder first.", type="warning")
                                return
                            dlg.close()
                            await _clone(repo_input.value, clone_parent.value)

                        with ui.row().classes("gap-2"):
                            ui.button(
                                "Browse",
                                icon="folder_open",
                                on_click=lambda: safe_ui_task(_browse_clone_folder, context="developer browse clone folder"),
                            ).props("outline no-caps")
                            ui.button("Preview target", icon="visibility", on_click=_show_target_hint).props(
                                "flat no-caps"
                            )
                            ui.button(
                                "Clone Workspace",
                                icon="download",
                                on_click=lambda: safe_ui_task(_clone_from_dialog, context="developer clone workspace"),
                            ).props("color=primary no-caps")
        dlg.open()

    def _refresh_developer_workspaces() -> None:
        state.preferred_home_tab = "Developer"
        state.preferred_developer_tab = "Workspaces"
        rebuild_main()

    def _refresh_custom_tools() -> None:
        state.preferred_home_tab = "Developer"
        state.preferred_developer_tab = "Custom Tools"
        rebuild_main()

    def _open_custom_tool_wizard() -> None:
        state.preferred_developer_tab = "Custom Tools"
        _show_custom_tool_wizard(state, _refresh_custom_tools)

    with ui.scroll_area().classes("w-full h-full"):
        with ui.column().classes("w-full q-pa-md gap-4"):
            with ui.row().classes("w-full items-center justify-between no-wrap"):
                with ui.column().classes("gap-0"):
                    ui.label("Developer").classes("text-h4")
                    ui.label("Code workspaces, Custom Tools, reviews, tests, and PR prep.").classes("text-sm text-grey-6")
                with ui.row().classes("gap-2"):
                    ui.button("New Custom Tool", icon="extension", on_click=_open_custom_tool_wizard).props("outline no-caps")
                    ui.button("New Workspace", icon="add", on_click=_show_workspace_dialog).props("color=primary no-caps")

            with ui.tabs().classes("w-full") as home_tabs:
                workspaces_tab = ui.tab("Workspaces", icon="code").on(
                    "click", lambda: setattr(state, "preferred_developer_tab", "Workspaces")
                )
                custom_tools_tab = ui.tab("Custom Tools", icon="extension").on(
                    "click", lambda: setattr(state, "preferred_developer_tab", "Custom Tools")
                )
            selected_developer_tab = custom_tools_tab if state.preferred_developer_tab == "Custom Tools" else workspaces_tab
            with ui.tab_panels(home_tabs, value=selected_developer_tab).classes("w-full").props("animated"):
                with ui.tab_panel(workspaces_tab).classes("q-pa-none"):
                    _render_developer_workspaces_home(_show_workspace_dialog, _open_workspace, _remove_workspace)
                with ui.tab_panel(custom_tools_tab).classes("q-pa-none"):
                    _render_custom_tools_home(state, _refresh_custom_tools)

def _render_developer_workspaces_home(show_workspace_dialog: Callable, on_open: Callable, on_remove: Callable) -> None:
    with ui.column().classes("w-full gap-4 q-pt-md"):
        workspaces = list_workspaces()
        if workspaces:
            with ui.row().classes("w-full items-center justify-between"):
                ui.label("Recent Workspaces").classes("text-subtitle1 text-weight-bold")
                ui.badge("Local sandbox", color="grey-8").props("outline")
            with ui.element("div").classes("w-full").style(
                "display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 0.85rem;"
            ):
                for workspace in workspaces[:8]:
                    _render_workspace_card(workspace, on_open=on_open, on_remove=on_remove)
        else:
            with ui.card().classes("w-full").style(
                "border-radius: 8px; border: 1px solid rgba(96,165,250,0.20); padding: 1.2rem;"
            ):
                with ui.row().classes("w-full items-center justify-between gap-4"):
                    with ui.column().classes("gap-1"):
                        ui.icon("code", size="lg").classes("text-primary")
                        ui.label("Connect your first repo").classes("text-h6")
                        ui.label("Developer works from a local folder you choose. Nothing is cloned or edited by default.").classes(
                            "text-sm text-grey-6"
                        )
                    ui.button("Connect Workspace", icon="add", on_click=show_workspace_dialog).props("outline no-caps")
            ui.label("Connect a workspace to unlock repo actions.").classes("text-xs text-grey-6")


def _render_workspace_card(workspace: DeveloperWorkspace, *, on_open: Callable, on_remove: Callable) -> None:
    with ui.card().classes("h-full").style(
        "padding: 0.75rem; border-radius: 8px; cursor: pointer; "
        "border: 1px solid rgba(255,255,255,0.10);"
    ):
        with ui.row().classes("w-full items-center no-wrap gap-2"):
            with ui.row().classes("items-center no-wrap gap-2").style("min-width: 0; flex: 1;").on(
                "click", lambda: safe_ui_task(lambda: on_open(workspace), context="developer open recent workspace")
            ):
                ui.icon("code", size="sm").classes("text-primary")
                ui.label(workspace.name).classes("font-bold ellipsis").style("min-width: 0;")
            ui.button(icon="close", on_click=lambda: safe_ui_task(
                lambda: on_remove(workspace),
                context="developer remove recent workspace",
            )).props(
                "flat dense round size=sm"
            ).tooltip("Remove from Developer recents")
        with ui.column().classes("w-full gap-1").on(
            "click", lambda: safe_ui_task(lambda: on_open(workspace), context="developer open recent workspace")
        ):
            ui.label(workspace.path).classes("text-xs text-grey-6 ellipsis w-full")
            info = workspace_updated_label(workspace)
            if workspace.repo_url:
                info = f"{info} - cloned" if info else "cloned"
            ui.label(info or "Recent workspace").classes("text-xs text-grey-7")


def _current_developer_workspace(state: AppState) -> DeveloperWorkspace | None:
    if not state.active_developer_workspace_id:
        return None
    return get_workspace(state.active_developer_workspace_id)


def _render_custom_tools_home(state: AppState, refresh: Callable) -> None:
    with ui.column().classes("w-full gap-4 q-pt-md"):
        with ui.row().classes("w-full items-center justify-between no-wrap"):
            with ui.column().classes("gap-0"):
                ui.label("Custom Tools").classes("text-subtitle1 text-weight-bold")
                ui.label("Create reusable tools from repos or folders, test them, then enable them in Developer or chat.").classes("text-sm text-grey-6")

        drafts = [draft for draft in list_custom_tool_drafts() if not draft.created_tool_id]
        tools = list_capsules()
        if drafts:
            ui.label("Drafts").classes("text-sm text-weight-medium text-grey-4")
            with ui.element("div").classes("w-full").style(
                "display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 0.85rem;"
            ):
                for draft in drafts:
                    _render_custom_tool_draft_card(draft, refresh)

        if not tools:
            with ui.card().classes("w-full").style(
                "border-radius: 8px; border: 1px solid rgba(96,165,250,0.20); padding: 1.2rem;"
            ):
                with ui.row().classes("w-full items-center gap-4"):
                    with ui.column().classes("gap-1"):
                        ui.icon("extension", size="lg").classes("text-primary")
                        ui.label("No Custom Tools yet").classes("text-h6")
                        ui.label("Give Thoth a repo URL or folder and it will inspect, propose commands, and register the tool after review.").classes("text-sm text-grey-6")
            return

        with ui.element("div").classes("w-full").style(
            "display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 0.85rem;"
        ):
            for tool in tools:
                _render_custom_tool_card(tool, refresh)


def _render_custom_tool_draft_card(draft, refresh: Callable) -> None:
    async def _create_from_draft() -> None:
        try:
            await run.io_bound(create_tool_from_draft, draft.id)
        except Exception as exc:
            ui.notify(str(exc), type="negative", close_button=True)
            return
        ui.notify("Custom Tool created from draft", type="positive")
        refresh()

    async def _delete_draft() -> None:
        try:
            await run.io_bound(delete_custom_tool_draft, draft.id)
        except Exception as exc:
            ui.notify(str(exc), type="negative", close_button=True)
            return
        ui.notify("Custom Tool draft removed", type="positive")
        refresh()

    with ui.card().classes("h-full").style("padding: 0.8rem; border-radius: 8px; border: 1px solid rgba(96,165,250,0.18);"):
        with ui.row().classes("w-full items-start justify-between no-wrap gap-2"):
            with ui.column().classes("gap-1").style("min-width: 0;"):
                with ui.row().classes("items-center gap-2 no-wrap"):
                    ui.icon("edit_note", size="sm").classes("text-blue-4")
                    ui.label(draft.name).classes("font-bold ellipsis").style("min-width: 0;")
                with ui.row().classes("items-center gap-1 flex-wrap"):
                    ui.badge("draft", color="blue").props("outline")
                    ui.badge(draft.status, color="grey").props("outline")
                    ui.badge(f"{len(draft.commands)} command{'s' if len(draft.commands) != 1 else ''}", color="blue-grey").props("outline")
            with ui.button(icon="more_vert").props("flat dense round"):
                with ui.menu():
                    ui.menu_item("Create Tool", on_click=lambda: safe_ui_task(_create_from_draft, context="developer create custom tool draft"))
                    ui.separator()
                    ui.menu_item("Delete Draft", on_click=lambda: safe_ui_task(_delete_draft, context="developer delete custom tool draft"))
        ui.label(draft.source_url).classes("text-xs text-grey-6 ellipsis")
        ui.label(draft.installed_path).classes("text-xs text-grey-7 ellipsis")
        for warning in draft.warnings[:2]:
            ui.label(warning).classes("text-xs text-amber-4")


def _render_custom_tool_card(tool, refresh: Callable) -> None:
    output_holder: dict[str, object] = {}

    def _output_box():
        return output_holder["box"]

    def _show_output(message: str, *, tone: str = "grey-6", busy: bool = False) -> None:
        output_box = _output_box()
        output_box.clear()
        with output_box:
            with ui.row().classes("items-center gap-2"):
                if busy:
                    ui.spinner(size="sm")
                ui.label(message).classes(f"text-xs text-{tone}")

    async def _toggle(enabled: bool) -> None:
        try:
            await run.io_bound(set_capsule_enabled, tool.id, enabled)
            from agent import clear_agent_cache
            clear_agent_cache()
        except Exception as exc:
            ui.notify(str(exc), type="negative", close_button=True)
            return
        ui.notify("Custom Tool enabled" if enabled else "Custom Tool disabled", type="positive")
        refresh()

    async def _confirm_run_once(command: str, meta: dict) -> bool:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[bool] = loop.create_future()
        with ui.dialog().props("persistent") as confirm_dlg, ui.card().classes("q-pa-md").style("min-width: min(620px, 92vw);"):
            with ui.row().classes("items-center gap-2"):
                ui.icon("warning", size="md").classes("text-amber")
                ui.label("Run Custom Tool Command Once").classes("text-h6")
            ui.label(str(meta.get("reason") or "This command needs approval before testing.")).classes("text-sm text-grey-5")
            with ui.column().classes("w-full gap-1 q-mt-sm"):
                ui.badge(str(meta.get("label") or "Review"), color="orange").props("outline")
                ui.label(f"Source: {tool.installed_path}").classes("text-xs text-grey-6")
                ui.label("Docker Sandbox will be used when available; otherwise this runs locally after this one-time approval.").classes("text-xs text-grey-6")
                ui.code(command).classes("w-full text-xs").style("max-width: 100%; max-height: 160px; overflow: auto; white-space: pre-wrap;")

            def _finish(approved: bool) -> None:
                if not future.done():
                    future.set_result(approved)
                confirm_dlg.close()

            with ui.row().classes("w-full justify-end gap-2 q-mt-md"):
                ui.button("Deny", on_click=lambda: _finish(False)).props("flat no-caps")
                ui.button("Run once", on_click=lambda: _finish(True)).props("color=primary no-caps")
        confirm_dlg.open()
        return await future

    async def _test(command: str) -> None:
        meta = classify_custom_tool_command(command)
        approved_once = False
        if meta.get("requires_approval"):
            approved_once = await _confirm_run_once(command, meta)
            if not approved_once:
                _show_output("Test cancelled.", tone="grey-6")
                return
        _show_output("Running command...", busy=True)
        try:
            result = await run.io_bound(
                lambda: run_custom_tool_test_command(
                    tool.id,
                    command,
                    query=DEFAULT_CUSTOM_TOOL_TEST_QUERY,
                    approved_once=approved_once,
                    require_enabled=False,
                )
            )
        except Exception as exc:
            output_box = _output_box()
            output_box.clear()
            with output_box:
                ui.label(str(exc)).classes("text-negative text-xs")
            return
        output_box = _output_box()
        output_box.clear()
        with output_box:
            status_color = "green" if result.ok else "amber"
            ui.badge(f"exit {result.returncode}" if result.ran else "not run", color=status_color).props("outline")
            if result.execution_mode == "docker":
                ui.badge("Docker Sandbox", color="purple").props("outline")
            else:
                ui.badge("Local", color="grey").props("outline")
            text = result.stdout or result.stderr
            if text:
                ui.code(text).classes("w-full text-xs").style("max-width: 100%; max-height: 180px; overflow: auto; white-space: pre-wrap;")

    async def _promote() -> None:
        try:
            promoted = await run.io_bound(promote_capsule, tool.id)
            from agent import clear_agent_cache
            clear_agent_cache()
        except Exception as exc:
            ui.notify(str(exc), type="negative", close_button=True)
            return
        ui.notify(f"{promoted.name} is now available in chat tools", type="positive")
        refresh()

    async def _remove() -> None:
        try:
            await run.io_bound(remove_capsule, tool.id)
            from agent import clear_agent_cache
            clear_agent_cache()
        except Exception as exc:
            ui.notify(str(exc), type="negative", close_button=True)
            return
        ui.notify("Custom Tool removed. Source files were not deleted.", type="positive")
        refresh()

    with ui.card().classes("h-full").style("padding: 0.8rem; border-radius: 8px; border: 1px solid rgba(255,255,255,0.10);"):
        with ui.row().classes("w-full items-start justify-between no-wrap gap-2"):
            with ui.column().classes("gap-1").style("min-width: 0;"):
                with ui.row().classes("items-center gap-2 no-wrap"):
                    ui.icon("extension", size="sm").classes("text-primary")
                    ui.label(tool.name).classes("font-bold ellipsis").style("min-width: 0;")
                with ui.row().classes("items-center gap-1 flex-wrap"):
                    ui.badge("enabled" if tool.enabled else "disabled", color="green" if tool.enabled else "grey").props("outline")
                    if tool.promoted_plugin_id:
                        ui.badge("available in chat", color="blue").props("outline")
                    ui.badge(f"{len(tool.commands)} command{'s' if len(tool.commands) != 1 else ''}", color="blue-grey").props("outline")
            with ui.button(icon="more_vert").props("flat dense round"):
                with ui.menu():
                    ui.menu_item("Disable" if tool.enabled else "Enable", on_click=lambda val=not tool.enabled: safe_ui_task(lambda: _toggle(val), context="developer toggle custom tool card"))
                    ui.menu_item("Use in Chat", on_click=lambda: safe_ui_task(_promote, context="developer promote custom tool card"))
                    ui.separator()
                    ui.menu_item("Remove", on_click=lambda: safe_ui_task(_remove, context="developer remove custom tool card"))
        if tool.version:
            ui.label(f"v{tool.version}").classes("text-xs text-grey-6")
        ui.label(tool.source_url).classes("text-xs text-grey-6 ellipsis")
        ui.label(tool.installed_path).classes("text-xs text-grey-7 ellipsis")
        if tool.commands:
            with ui.expansion(
                f"Commands ({len(tool.commands)})",
                icon="terminal",
                value=False,
            ).classes("w-full q-mt-xs"):
                with ui.column().classes("w-full gap-1"):
                    for command in tool.commands:
                        command_name = str(command.get("name", "Command"))
                        command_text = str(command.get("command", ""))
                        command_description = str(command.get("description", "")).strip()
                        meta = classify_custom_tool_command(command_text)
                        label = str(meta.get("label") or "Review")
                        badge_color = "green" if label == "Local" else ("orange" if meta.get("requires_approval") else "blue-grey")
                        with ui.row().classes("w-full items-center no-wrap gap-2").style("min-width: 0; overflow: hidden;"):
                            ui.icon("terminal", size="xs").classes("text-grey-5")
                            with ui.column().classes("gap-0").style("min-width: 0; flex: 1 1 auto; overflow: hidden;"):
                                with ui.row().classes("items-center no-wrap gap-1").style("min-width: 0; overflow: hidden;"):
                                    ui.label(command_name).classes("text-xs font-bold ellipsis").style("max-width: 100%;")
                                    ui.badge(label, color=badge_color).props("outline").classes("shrink-0")
                                if command_description:
                                    ui.label(command_description).classes("text-xs text-grey-6 ellipsis").style("max-width: 100%;")
                            ui.button(
                                "Run",
                                icon="play_arrow",
                                on_click=lambda cmd=command_text: safe_ui_task(lambda: _test(cmd), context="developer test custom tool card"),
                            ).props("dense flat no-caps size=sm").classes("shrink-0")
        else:
            ui.label("No commands found.").classes("text-xs text-grey-6 q-mt-xs")
        output_holder["box"] = ui.column().classes("w-full gap-1 q-mt-sm")
        with _output_box():
            ui.label("Test output appears here.").classes("text-xs text-grey-6")


def _show_custom_tool_wizard(state: AppState, refresh: Callable) -> None:
    active_workspace = _current_developer_workspace(state)
    with ui.dialog() as dlg, ui.card().classes("q-pa-md").style("min-width: min(900px, 94vw);"):
        with ui.row().classes("w-full items-center justify-between no-wrap"):
            with ui.column().classes("gap-0"):
                ui.label("New Custom Tool").classes("text-h6")
                ui.label("Create a reusable Thoth tool from a repo URL, local folder, or current workspace.").classes("text-sm text-grey-6")
            ui.button(icon="close", on_click=dlg.close).props("flat dense round")

        controls: dict[str, object] = {}
        state_box: dict = {}

        def _control(name: str):
            return controls[name]

        async def _browse_source() -> None:
            source_in = _control("source_in")
            picked = await browse_folder("Select custom tool repo/folder", str(source_in.value or ""))
            if picked:
                source_in.value = picked

        async def _browse_clone_parent() -> None:
            clone_parent = _control("clone_parent")
            picked = await browse_folder("Select clone parent folder", str(clone_parent.value or ""))
            if picked:
                clone_parent.value = picked

        def _render_proposal(proposal) -> None:
            proposal_box = _control("proposal_box")
            proposal_box.clear()
            with proposal_box:
                with ui.row().classes("items-center gap-2 flex-wrap"):
                    ui.badge(proposal.name, color="blue").props("outline")
                    ui.badge(f"v{proposal.version}", color="grey").props("outline")
                    if getattr(proposal, "existing_manifest", False):
                        ui.badge("existing config", color="amber").props("outline")
                ui.label(proposal.installed_path).classes("text-xs text-grey-7 ellipsis")
                for warning in proposal.warnings:
                    ui.label(warning).classes("text-xs text-amber-4")
                ui.label("Proposed commands").classes("text-sm text-weight-medium")
                for command in proposal.commands:
                    with ui.column().classes("w-full gap-1 q-pa-xs").style("border: 1px solid rgba(255,255,255,0.08); border-radius: 6px;"):
                        ui.label(str(command.get("name", "Command"))).classes("text-sm")
                        ui.label(str(command.get("description", ""))).classes("text-xs text-grey-6")
                        ui.code(str(command.get("command", ""))).classes("w-full text-xs")

        def _set_inspect_status(message: str, *, tone: str = "grey-6", busy: bool = False) -> None:
            status = _control("status")
            status.clear()
            with status:
                with ui.row().classes("items-center gap-2"):
                    if busy:
                        ui.spinner(size="sm")
                    ui.label(message).classes(f"text-xs text-{tone}")

        async def _inspect() -> None:
            source_in = _control("source_in")
            clone_parent = _control("clone_parent")
            status = _control("status")
            proposal_box = _control("proposal_box")
            inspect_btn = _control("inspect_btn")
            status.clear()
            proposal_box.clear()
            raw_source = str(source_in.value or "").strip()
            if not raw_source:
                with status:
                    ui.label("Choose a repo URL or local folder first.").classes("text-negative text-xs")
                return
            inspect_btn.disable()
            try:
                source = raw_source
                folder = raw_source
                reused_existing_clone = False
                if source.startswith(("http://", "https://", "git@")):
                    parent = str(clone_parent.value or "").strip()
                    if not parent:
                        raise ValueError("Choose a clone parent folder for repo URLs.")
                    expected_target = Path(parent).expanduser().resolve() / suggested_clone_name(source)
                    reused_existing_clone = expected_target.exists() and expected_target.is_dir()
                    if reused_existing_clone:
                        _set_inspect_status(f"Using existing clone: {expected_target}", tone="amber-4", busy=True)
                    else:
                        _set_inspect_status(f"Cloning into {expected_target}...", busy=True)
                    cloned = await run.io_bound(clone_capsule_repository, source, parent)
                    folder = str(cloned)
                    source_in.value = folder
                else:
                    _set_inspect_status(f"Inspecting local folder: {folder}", busy=True)
                await asyncio.sleep(0)
                _set_inspect_status("Scanning README and project files...", busy=True)
                await asyncio.sleep(0)
                _set_inspect_status("Asking AI to propose safe commands. This can take a minute...", busy=True)
                proposal = await run.io_bound(
                    lambda: create_custom_tool_draft(folder, source_url=source, use_ai=True)
                )
            except Exception as exc:
                logger.exception("Failed to inspect Custom Tool source")
                with status:
                    ui.label(str(exc)).classes("text-negative text-xs")
                return
            finally:
                inspect_btn.enable()
            state_box["draft"] = proposal
            state_box["proposal"] = proposal
            if reused_existing_clone:
                with status:
                    ui.label(f"Using existing clone: {folder}").classes("text-xs text-amber-4")
            else:
                _set_inspect_status(f"Inspection complete: {proposal.name}", tone="positive")
            _render_proposal(proposal)
            stepper.next()

        async def _create() -> None:
            overwrite_sw = _control("overwrite_sw")
            status = _control("status")
            result_box = _control("result_box")
            draft = state_box.get("draft")
            if draft is None:
                await _inspect()
                draft = state_box.get("draft")
            if draft is None:
                return
            result_box.clear()
            status.clear()
            try:
                tool = await run.io_bound(
                    lambda: create_tool_from_draft(draft.id, overwrite=bool(overwrite_sw.value), community=True)
                )
            except Exception as exc:
                logger.exception("Failed to create Custom Tool")
                with result_box:
                    ui.label(str(exc)).classes("text-negative text-xs")
                return
            state_box["tool"] = tool
            with result_box:
                ui.badge(f"created {tool.name}", color="green").props("outline")
                ui.label(f"{len(tool.commands)} command(s) ready for testing.").classes("text-xs text-grey-6")
            _render_wizard_test_panel()
            stepper.next()

        async def _run_smoke_test() -> None:
            tool = state_box.get("tool")
            draft = state_box.get("draft")
            if draft is None or not draft.commands:
                ui.notify("Create a Custom Tool with at least one command first.", type="warning")
                return
            command = next(
                (
                    item
                    for item in draft.commands
                    if not custom_tool_command_needs_query(str(item.get("command", "")))
                    and not classify_custom_tool_command(str(item.get("command", ""))).get("requires_approval")
                ),
                next(
                    (
                        item
                        for item in draft.commands
                        if not classify_custom_tool_command(str(item.get("command", ""))).get("requires_approval")
                    ),
                    draft.commands[0],
                ),
            )
            await _test_wizard_command(str(command.get("name", "")))

        async def _confirm_wizard_run_once(command: str, meta: dict) -> bool:
            loop = asyncio.get_running_loop()
            future: asyncio.Future[bool] = loop.create_future()
            with ui.dialog().props("persistent") as confirm_dlg, ui.card().classes("q-pa-md").style("min-width: min(620px, 92vw);"):
                with ui.row().classes("items-center gap-2"):
                    ui.icon("warning", size="md").classes("text-amber")
                    ui.label("Run Custom Tool Command Once").classes("text-h6")
                ui.label(str(meta.get("reason") or "This command needs approval before testing.")).classes("text-sm text-grey-5")
                with ui.column().classes("w-full gap-1 q-mt-sm"):
                    ui.badge(str(meta.get("label") or "Review"), color="orange").props("outline")
                    ui.label("Thoth will prefer Docker Sandbox. If Docker cannot start, this one approved test may fall back to local execution.").classes("text-xs text-grey-6")
                    ui.code(command).classes("w-full text-xs").style("max-width: 100%; max-height: 160px; overflow: auto; white-space: pre-wrap;")

                def _finish(approved: bool) -> None:
                    if not future.done():
                        future.set_result(approved)
                    confirm_dlg.close()

                with ui.row().classes("w-full justify-end gap-2 q-mt-md"):
                    ui.button("Deny", on_click=lambda: _finish(False)).props("flat no-caps")
                    ui.button("Run once", on_click=lambda: _finish(True)).props("color=primary no-caps")
            confirm_dlg.open()
            return await future

        async def _test_wizard_command(command_name: str) -> None:
            result_box = _control("result_box")
            tool = state_box.get("tool")
            draft = state_box.get("draft")
            if draft is None:
                ui.notify("Inspect the Custom Tool first.", type="warning")
                return
            if not draft.commands:
                ui.notify("Create a Custom Tool with at least one command first.", type="warning")
                return
            if tool is None:
                ui.notify("Create the Custom Tool first.", type="warning")
                return
            command = next(
                (
                    item
                    for item in draft.commands
                    if str(item.get("name", "")).strip().lower() == command_name.strip().lower()
                ),
                draft.commands[0],
            )
            command_text = str(command.get("command", ""))
            meta = classify_custom_tool_command(command_text)
            test_query = ""
            if custom_tool_command_needs_query(command_text):
                query_control = controls.get("test_query")
                test_query = str(getattr(query_control, "value", "") or DEFAULT_CUSTOM_TOOL_TEST_QUERY)
            approved_once = False
            if meta.get("requires_approval"):
                approved_once = await _confirm_wizard_run_once(command_text, meta)
                if not approved_once:
                    result_box.clear()
                    with result_box:
                        ui.badge("not run", color="grey").props("outline")
                        ui.label("Test cancelled.").classes("text-xs text-grey-6")
                    return
            result_box.clear()
            with result_box:
                with ui.row().classes("items-center gap-2"):
                    ui.spinner(size="sm")
                    ui.label(f"Running {command.get('name', 'command')}...").classes("text-xs text-grey-6")
            try:
                result = await run.io_bound(
                    lambda: run_custom_tool_test_command(
                        tool.id,
                        command_text,
                        query=test_query,
                        approved_once=approved_once,
                        require_enabled=False,
                    )
                )
            except Exception as exc:
                ui.notify(str(exc), type="negative", close_button=True)
                return
            result_box.clear()
            with result_box:
                with ui.row().classes("items-center gap-2 flex-wrap"):
                    ui.label(str(command.get("name", "Command"))).classes("text-xs font-bold")
                    ui.badge("Passed" if result.ok else ("Not run" if not result.ran else "Failed"), color="green" if result.ok else "amber").props("outline")
                    if result.ran:
                        ui.badge(f"exit {result.returncode}", color="green" if result.ok else "red").props("outline")
                    ui.badge("Docker Sandbox" if result.execution_mode == "docker" else "Local", color="purple" if result.execution_mode == "docker" else "grey").props("outline")
                text = result.stdout or result.stderr
                if text:
                    ui.code(text).classes("w-full text-xs").style("max-width: 100%; max-height: 180px; overflow: auto; white-space: pre-wrap;")
                else:
                    ui.label("Command passed with no output." if result.ok else "No output.").classes("text-xs text-grey-6")

        def _render_wizard_test_panel() -> None:
            test_box = _control("test_box")
            test_box.clear()
            draft = state_box.get("draft")
            tool = state_box.get("tool")
            with test_box:
                if draft is None or tool is None:
                    ui.label("Create the tool to unlock testing.").classes("text-xs text-grey-6")
                    return
                commands = list(draft.commands)
                if not commands:
                    ui.label("No commands found to test.").classes("text-xs text-grey-6")
                    return
                has_query_commands = any(custom_tool_command_needs_query(str(item.get("command", ""))) for item in commands)
                if has_query_commands:
                    saved_query = str(state_box.get("test_query", DEFAULT_CUSTOM_TOOL_TEST_QUERY) or DEFAULT_CUSTOM_TOOL_TEST_QUERY)
                    controls["test_query"] = ui.input("Test query", value=saved_query).props("dense outlined")

                    def _remember_query(event) -> None:
                        state_box["test_query"] = str(getattr(event, "value", "") or DEFAULT_CUSTOM_TOOL_TEST_QUERY)

                    controls["test_query"].on("update:model-value", _remember_query)
                smoke = next(
                    (
                        item
                        for item in commands
                        if not custom_tool_command_needs_query(str(item.get("command", "")))
                        and not classify_custom_tool_command(str(item.get("command", ""))).get("requires_approval")
                    ),
                    next(
                        (
                            item
                            for item in commands
                            if not classify_custom_tool_command(str(item.get("command", ""))).get("requires_approval")
                        ),
                        commands[0],
                    ),
                )
                smoke_name = str(smoke.get("name", "Command"))
                smoke_text = str(smoke.get("command", ""))
                smoke_meta = classify_custom_tool_command(smoke_text)
                with ui.card().classes("w-full").style("padding: 0.75rem; border-radius: 8px; border: 1px solid rgba(255,255,255,0.10);"):
                    with ui.row().classes("w-full items-center justify-between no-wrap gap-2"):
                        with ui.column().classes("gap-0").style("min-width: 0;"):
                            with ui.row().classes("items-center gap-2 no-wrap"):
                                ui.label("Smoke Test").classes("text-sm font-bold")
                                ui.badge(str(smoke_meta.get("label") or "Review"), color="green" if not smoke_meta.get("requires_approval") else "orange").props("outline")
                                if custom_tool_command_needs_query(smoke_text):
                                    ui.badge("uses test query", color="blue").props("outline")
                            ui.label(smoke_name).classes("text-xs text-grey-5 ellipsis")
                            if smoke.get("description"):
                                ui.label(str(smoke.get("description"))).classes("text-xs text-grey-6 ellipsis")
                        ui.button(
                            "Run Smoke Test",
                            icon="play_arrow",
                            on_click=lambda: safe_ui_task(_run_smoke_test, context="developer custom tool wizard smoke test"),
                        ).props("outline no-caps").classes("shrink-0")
                    with ui.expansion("Show command", icon="terminal", value=False).classes("w-full"):
                        ui.code(smoke_text).classes("w-full text-xs").style("max-width: 100%; max-height: 140px; overflow: auto; white-space: pre-wrap;")
                with ui.expansion(f"All commands ({len(commands)})", icon="terminal", value=False).classes("w-full"):
                    for command in commands:
                        command_name = str(command.get("name", "Command"))
                        command_text = str(command.get("command", ""))
                        meta = classify_custom_tool_command(command_text)
                        with ui.row().classes("w-full items-center no-wrap gap-2").style("min-width: 0; overflow: hidden;"):
                            with ui.column().classes("gap-0").style("min-width: 0; flex: 1 1 auto; overflow: hidden;"):
                                with ui.row().classes("items-center gap-2 no-wrap"):
                                    ui.label(command_name).classes("text-xs font-bold ellipsis")
                                    ui.badge(str(meta.get("label") or "Review"), color="green" if not meta.get("requires_approval") else "orange").props("outline").classes("shrink-0")
                                    if custom_tool_command_needs_query(command_text):
                                        ui.badge("query", color="blue").props("outline").classes("shrink-0")
                                if command.get("description"):
                                    ui.label(str(command.get("description"))).classes("text-xs text-grey-6 ellipsis")
                            ui.button(
                                "Run",
                                icon="play_arrow",
                                on_click=lambda name=command_name: safe_ui_task(lambda: _test_wizard_command(name), context="developer custom tool wizard command test"),
                            ).props("dense flat no-caps size=sm").classes("shrink-0")
                result_box = _control("result_box")
                result_box.clear()
                with result_box:
                    ui.badge("not tested", color="grey").props("outline")
                    ui.label("Run the smoke test or an individual command, then enable when you are comfortable.").classes("text-xs text-grey-6")

        async def _enable_created() -> None:
            tool = state_box.get("tool")
            draft = state_box.get("draft")
            if tool is None:
                ui.notify("Create the Custom Tool first.", type="warning")
                return
            if draft is not None:
                await run.io_bound(enable_created_custom_tool_from_draft, draft.id, True)
            else:
                await run.io_bound(set_capsule_enabled, tool.id, True)
            ui.notify("Custom Tool enabled in Developer", type="positive")
            state_box["needs_refresh"] = True

        async def _promote_created() -> None:
            tool = state_box.get("tool")
            draft = state_box.get("draft")
            if tool is None:
                ui.notify("Create the Custom Tool first.", type="warning")
                return
            if draft is not None:
                promoted = await run.io_bound(promote_created_custom_tool_from_draft, draft.id)
            else:
                promoted = await run.io_bound(promote_capsule, tool.id)
            ui.notify(f"{promoted.name} is now available in chat tools", type="positive")
            state_box["needs_refresh"] = True

        def _close_wizard() -> None:
            dlg.close()
            refresh()

        with ui.stepper().props("vertical").classes("w-full") as stepper:
            with ui.step("Source"):
                ui.label("Choose where the tool comes from. Repo URLs are cloned only into a folder you choose.").classes("text-sm text-grey-6")
                controls["source_in"] = ui.input("Repo URL or local folder").classes("w-full").props("dense outlined")
                controls["clone_parent"] = ui.input("Clone into folder (only for repo URLs)").classes("w-full").props("dense outlined")
                with ui.expansion("Advanced", icon="tune").classes("w-full"):
                    controls["overwrite_sw"] = ui.switch("Replace existing internal config after review", value=False)
                with ui.row().classes("gap-2 flex-wrap"):
                    ui.button("Browse local folder", icon="folder_open", on_click=lambda: safe_ui_task(_browse_source, context="developer browse custom tool source")).props("outline no-caps")
                    ui.button("Browse clone parent", icon="drive_folder_upload", on_click=lambda: safe_ui_task(_browse_clone_parent, context="developer browse custom tool clone parent")).props("outline no-caps")
                    if active_workspace is not None:
                        ui.button("Use current workspace", icon="code", on_click=lambda: _control("source_in").set_value(active_workspace.path)).props("outline no-caps")
                controls["status"] = ui.column().classes("w-full gap-1")
                with _control("status"):
                    ui.label("Start by choosing a repo URL or folder.").classes("text-xs text-grey-6")
                with ui.stepper_navigation():
                    controls["inspect_btn"] = ui.button("Inspect Tool", icon="search", on_click=lambda: safe_ui_task(_inspect, context="developer custom tool wizard inspect")).props("color=primary no-caps")
            with ui.step("Inspect"):
                ui.label("Review the commands Thoth proposed before creating anything.").classes("text-sm text-grey-6")
                controls["proposal_box"] = ui.column().classes("w-full gap-2")
                with _control("proposal_box"):
                    ui.label("No inspection results yet.").classes("text-xs text-grey-6")
                with ui.stepper_navigation():
                    ui.button("Back", on_click=stepper.previous).props("flat no-caps")
                    ui.button("Create Tool", icon="auto_fix_high", on_click=lambda: safe_ui_task(_create, context="developer custom tool wizard create")).props("color=primary no-caps")
            with ui.step("Test"):
                ui.label("Run a quick smoke test, or test commands individually before enabling.").classes("text-sm text-grey-6")
                controls["test_box"] = ui.column().classes("w-full gap-2")
                with _control("test_box"):
                    ui.label("Create the tool to unlock testing.").classes("text-xs text-grey-6")
                controls["result_box"] = ui.column().classes("w-full gap-2")
                with _control("result_box"):
                    ui.label("Test results appear here.").classes("text-xs text-grey-6")
                with ui.stepper_navigation():
                    ui.button("Back", on_click=stepper.previous).props("flat no-caps")
                    ui.button("Next", on_click=stepper.next).props("color=primary no-caps")
            with ui.step("Enable"):
                ui.label("Enable it in Developer, or make it available in normal chat tools.").classes("text-sm text-grey-6")
                with ui.row().classes("gap-2 flex-wrap"):
                    ui.button("Enable in Developer", icon="toggle_on", on_click=lambda: safe_ui_task(_enable_created, context="developer custom tool wizard enable")).props("outline no-caps")
                    ui.button("Use in Chat", icon="extension", on_click=lambda: safe_ui_task(_promote_created, context="developer custom tool wizard promote")).props("outline no-caps")
                    ui.button("Done", icon="check", on_click=_close_wizard).props("color=primary no-caps")
    dlg.open()


def _render_workspace_status_badges(container: object, git_summary: dict, workspace: DeveloperWorkspace) -> None:
    container.clear()
    with container:
        repo_label = "Git repo" if git_summary.get("is_git") else "Folder"
        ui.badge(repo_label, color="green" if git_summary.get("is_git") else "grey").tooltip(
            "This workspace is a Git repository." if git_summary.get("is_git") else "No Git repository detected."
        )
        if git_summary.get("branch"):
            ui.badge(f"Branch: {git_summary['branch']}", color="blue-grey").tooltip("Current Git branch")
        if git_summary.get("dirty"):
            ui.badge("Dirty", color="orange").tooltip("Uncommitted changes are present")
        if git_summary.get("error"):
            ui.badge("Git check failed", color="red").tooltip(str(git_summary.get("error")))
        mode_label = "Docker Sandbox" if workspace.execution_mode == "docker" else "Local"
        mode_tip = (
            "Commands run in a persistent Docker shadow workspace until imported."
            if workspace.execution_mode == "docker"
            else "Commands run directly in the selected local repository."
        )
        ui.badge(mode_label, color="purple" if workspace.execution_mode == "docker" else "grey").tooltip(mode_tip)


def _render_file_tree(
    files: list[str],
    changed_paths: set[str],
    show_file: Callable[[str], object],
) -> None:
    if not files:
        ui.label("No previewable files found.").classes("text-sm text-grey-6")
        return
    nodes, file_node_paths, folder_node_ids = _files_to_tree_nodes(files, changed_paths)
    ui.label(f"Showing {len(files)} files from this workspace.").classes("text-xs text-grey-6")

    def _select(e) -> None:
        file_path = file_node_paths.get(str(e.value or ""))
        if file_path:
            safe_ui_task(lambda: show_file(file_path), context="developer preview file")

    tree = ui.tree(nodes, on_select=_select).classes("w-full developer-file-tree").props(
        "dense no-connectors selected-color=primary"
    )
    tree.expand(sorted(folder_node_ids)[:32])


def _files_to_tree_nodes(
    files: list[str],
    changed_paths: set[str],
) -> tuple[list[dict], dict[str, str], set[str]]:
    roots: dict[str, dict] = {}
    file_node_paths: dict[str, str] = {}
    folder_node_ids: set[str] = set()

    def _child_map(node: dict) -> dict[str, dict]:
        return node.setdefault("_children_by_name", {})

    for raw_path in sorted(files, key=str.lower):
        clean = raw_path.replace("\\", "/").strip("/")
        if not clean:
            continue
        parts = clean.split("/")
        current_children = roots
        parent_id = ""
        for index, part in enumerate(parts):
            is_file = index == len(parts) - 1
            node_id = f"{'file' if is_file else 'dir'}:{clean if is_file else '/'.join(parts[:index + 1])}"
            node = current_children.get(part)
            if node is None:
                node = {
                    "id": node_id,
                    "label": part,
                    "icon": "description" if is_file else "folder",
                }
                current_children[part] = node
            if is_file:
                file_node_paths[node_id] = clean
                if clean in changed_paths:
                    node["icon"] = "edit_note"
                    node["iconColor"] = "orange"
                    node["label"] = f"{part}  *"
                else:
                    node["iconColor"] = "primary"
            else:
                folder_node_ids.add(node_id)
                parent_id = node_id
                node["iconColor"] = "grey-4"
                current_children = _child_map(node)
        if parent_id:
            folder_node_ids.add(parent_id)

    def _finalize(nodes_by_name: dict[str, dict]) -> list[dict]:
        rows: list[dict] = []
        for node in nodes_by_name.values():
            child_map = node.pop("_children_by_name", None)
            if child_map:
                node["children"] = _finalize(child_map)
            rows.append(node)
        rows.sort(key=lambda item: (0 if item.get("children") else 1, str(item.get("label", "")).lower()))
        return rows

    return _finalize(roots), file_node_paths, folder_node_ids


def _build_developer_skill_selector(thread_id: str | None) -> None:
    if not thread_id:
        return
    try:
        import skills as skills_mod
        from agent import clear_agent_cache
        from threads import get_thread_skills_override, set_thread_skills_override
    except Exception:
        return

    skills_mod.load_skills()
    enabled_skills = [sk for sk in skills_mod.get_enabled_skills() if not skills_mod.is_tool_guide(sk)]
    if not enabled_skills:
        ui.button("Extra skills: 0", icon="auto_fix_high").props("flat dense no-caps size=sm disabled").tooltip(
            "No manual skills are enabled in Settings"
        )
        return

    enabled_names = {sk.name for sk in enabled_skills}
    current = get_thread_skills_override(thread_id)
    active_names = (set(current) & enabled_names) if current is not None else set()
    button = ui.button(
        f"Extra skills: {len(active_names)}",
        icon="auto_fix_high",
    ).props("flat dense no-caps size=sm").classes("text-xs").tooltip(
        "Developer starts with normal skills off. Enable extras for this thread here."
    )

    def _update_label() -> None:
        cur = get_thread_skills_override(thread_id)
        active = (set(cur) & enabled_names) if cur is not None else set()
        button.text = f"Extra skills: {len(active)}"
        button.update()

    def _toggle(name: str, value: bool) -> None:
        cur = get_thread_skills_override(thread_id)
        names = set(cur or [])
        if value:
            names.add(name)
        else:
            names.discard(name)
        set_thread_skills_override(thread_id, sorted(names))
        clear_agent_cache()
        _update_label()

    async def _clear() -> None:
        set_thread_skills_override(thread_id, [])
        clear_agent_cache()
        for checkbox in checkboxes.values():
            checkbox.value = False
        _update_label()

    checkboxes = {}
    with button:
        with ui.menu().classes("q-pa-sm"):
            ui.label("Extra skills for Developer").classes("text-xs text-grey-5 q-mb-xs")
            with ui.column().classes("gap-0"):
                for skill in enabled_skills:
                    checkbox = ui.checkbox(
                        f"{skill.icon} {skill.display_name}",
                        value=skill.name in active_names,
                        on_change=lambda e, name=skill.name: _toggle(name, bool(e.value)),
                    ).classes("text-sm")
                    checkboxes[skill.name] = checkbox
            ui.separator()
            ui.button(
                "Clear extras",
                icon="remove_done",
                on_click=lambda: asyncio.create_task(_clear()),
            ).props("flat dense no-caps size=sm")


def build_developer_workspace(
    workspace_id: str,
    *,
    state: AppState,
    p: P,
    send_message: Callable,
    add_chat_message: Callable,
    browse_file: Callable | None,
    open_settings: Callable | None,
    on_back: Callable,
    show_interrupt: Callable | None = None,
) -> None:
    """Render the active Developer workspace shell."""
    workspace = get_workspace(workspace_id)
    if workspace is None:
        state.active_developer_workspace_id = None
        ui.notify("Developer workspace not found.", type="negative")
        on_back()
        return

    git_summary = detect_git_summary(workspace.path)
    workspace_header: dict[str, object] = {}

    inspector_refresh: dict[str, Callable[..., None]] = {}

    def _refresh_header_from_snapshot() -> None:
        if state.active_developer_workspace_id != workspace.id:
            return
        snapshot = get_snapshot(workspace.id, state.thread_id)
        current_workspace = snapshot.workspace if snapshot is not None else workspace
        current_git = snapshot.git_summary if snapshot is not None else detect_git_summary(workspace.path)
        badge_box = workspace_header.get("badges")
        branch_box = workspace_header.get("branch")
        if badge_box is not None:
            _render_workspace_status_badges(badge_box, current_git, current_workspace)
        if branch_box is not None:
            branch_box.clear()
            with branch_box:
                if current_git.get("is_git"):
                    with ui.row().classes("w-full items-end gap-2"):
                        branch_input = ui.input(
                            "Feature branch",
                            value=suggest_feature_branch(current_workspace.name),
                        ).props("dense outlined").style("max-width: 260px;")
                        ui.button(
                            "Create branch",
                            icon="call_split",
                            on_click=lambda: safe_ui_task(
                                lambda: _create_branch(branch_input.value),
                                context="developer create branch",
                            ),
                        ).props("outline no-caps")

    async def _set_mode(value: str) -> None:
        try:
            updated = await run.io_bound(set_workspace_approval_mode, workspace.id, value)
        except Exception as exc:
            ui.notify(str(exc), type="negative", close_button=True)
            return
        workspace.approval_mode = updated.approval_mode
        update_snapshot_approval_mode(workspace.id, state.thread_id, updated.approval_mode)
        request_snapshot_refresh(workspace.id, state.thread_id, reason="approval_mode", debounce=0.1)
        _refresh_header_from_snapshot()
        refresh = inspector_refresh.get("refresh")
        if refresh is not None:
            refresh()
        ui.notify(f"Developer approval mode: {value.replace('_', ' ').title()}", type="info")

    async def _set_execution_mode(value: str) -> None:
        try:
            updated = await run.io_bound(
                set_workspace_execution_settings,
                workspace.id,
                execution_mode=value,
            )
        except Exception as exc:
            ui.notify(str(exc), type="negative", close_button=True)
            return
        workspace.execution_mode = updated.execution_mode
        request_snapshot_refresh(workspace.id, state.thread_id, reason="execution_mode", debounce=0.1)
        _refresh_header_from_snapshot()
        refresh = inspector_refresh.get("refresh")
        if refresh is not None:
            refresh()
        label = "Docker Sandbox" if value == "docker" else "Local"
        ui.notify(f"Developer execution mode: {label}", type="info")

    async def _create_branch(branch_name: str | None) -> None:
        decision = decide_action(workspace.approval_mode, "git_branch")
        if decision.decision == "block":
            ui.notify(decision.reason, type="negative", close_button=True)
            return
        if decision.requires_approval:
            ui.notify(
                "Branch creation requires approval in this mode. Switch to Auto Edit or Agent Run, or create it manually.",
                type="warning",
            )
            return
        try:
            status = await run.io_bound(create_branch, workspace.path, branch_name or "")
        except Exception as exc:
            ui.notify(str(exc), type="negative", close_button=True)
            return
        ui.notify(f"Created branch {status.branch}", type="positive")
        request_snapshot_refresh(workspace.id, state.thread_id, reason="branch_created", debounce=0.05)
        _refresh_header_from_snapshot()
        refresh = inspector_refresh.get("refresh")
        if refresh is not None:
            refresh()

    with ui.row().classes("w-full h-full no-wrap gap-2").style("overflow: hidden;"):
        with ui.column().classes("h-full gap-2").style("min-width: 0; flex: 1; overflow: hidden;"):
            with ui.row().classes("w-full items-center justify-between no-wrap"):
                with ui.column().classes("gap-0").style("min-width: 0;"):
                    with ui.row().classes("items-center gap-2 no-wrap"):
                        ui.button(icon="arrow_back", on_click=on_back).props("flat dense round").tooltip("Back to Developer")
                        ui.label(workspace.name).classes("text-h5 ellipsis")
                    ui.label(workspace.path).classes("text-xs text-grey-6 ellipsis")
                with ui.row().classes("items-center gap-2 no-wrap"):
                    _build_developer_skill_selector(state.thread_id)
                    ui.select(
                        {
                            "read_only": "Read Only",
                            "ask": "Ask Before Changes",
                            "auto_edit": "Auto Edit",
                            "agent_run": "Agent Run",
                        },
                        value=workspace.approval_mode,
                        on_change=lambda e: safe_ui_task(
                            lambda: _set_mode(e.value),
                            context="developer approval mode change",
                        ),
                    ).props("dense outlined").classes("text-xs").style("min-width: 170px;")
                    ui.select(
                        {
                            "local": "Local",
                            "docker": "Docker Sandbox",
                        },
                        value=workspace.execution_mode,
                        on_change=lambda e: safe_ui_task(
                            lambda: _set_execution_mode(e.value),
                            context="developer execution mode change",
                        ),
                    ).props("dense outlined").classes("text-xs").style("min-width: 150px;")
                    ui.badge("Developer Preview", color="grey-8").props("outline")

            workspace_header["badges"] = ui.row().classes("w-full flex-wrap gap-2")
            _render_workspace_status_badges(workspace_header["badges"], git_summary, workspace)
            workspace_header["branch"] = ui.column().classes("w-full gap-1")
            _refresh_header_from_snapshot()

            with ui.row().classes("w-full flex-wrap gap-2"):
                for label, icon, prompt in _DEVELOPER_QUICK_ACTIONS:
                    ui.button(
                        label,
                        icon=icon,
                        on_click=lambda text=prompt: safe_ui_task(
                            lambda: send_message(text),
                            context="developer quick action",
                        ),
                    ).props("flat dense outline no-caps").classes("text-grey-4")

            hidden_upload = build_file_upload(p, state)
            build_chat_messages(
                p,
                state,
                messages=state.messages,
                add_chat_message=add_chat_message,
                placeholder_text="Ask Developer to inspect this repo, plan a change, or review code.",
                cloud_tint=None,
            )
            with p.chat_container:
                p.developer_approval_container = ui.column().classes("w-full gap-1")
            if state.pending_interrupt and show_interrupt is not None:
                try:
                    show_interrupt(state.pending_interrupt)
                except Exception:
                    ui.notify("Approval is pending. Reopen this Developer thread if the approval dialog is not visible.", type="warning")

            async def _send(text: str) -> None:
                await send_message(text)

            build_chat_input_bar(
                p,
                state,
                send_fn=_send,
                hidden_upload=hidden_upload,
                browse_file=browse_file,
                open_settings=open_settings,
                show_model_picker=True,
            )

        inspector_refresh["refresh"] = _build_developer_inspector(
            workspace,
            git_summary,
            state.thread_id,
            state=state,
            add_chat_message=add_chat_message,
        )

        header_seen: dict[str, str] = {"fingerprint": ""}

        def _poll_header() -> None:
            if state.active_developer_workspace_id != workspace.id:
                return
            snapshot = get_snapshot(workspace.id, state.thread_id)
            fingerprint = snapshot.fingerprint if snapshot is not None else ""
            if fingerprint and fingerprint != header_seen["fingerprint"]:
                header_seen["fingerprint"] = fingerprint
                _refresh_header_from_snapshot()

        safe_timer(1.0, _poll_header)


def _build_developer_inspector(
    workspace: DeveloperWorkspace,
    git_summary: dict,
    thread_id: str | None,
    *,
    state: AppState | None = None,
    add_chat_message: Callable | None = None,
) -> Callable[[], None]:
    """Render the Developer Inspector from a background snapshot cache."""
    host = ui.element("div").classes("h-full").style("flex-shrink: 0; overflow: hidden;")
    version_state: dict[str, object] = {"version": -1, "last_request": 0.0, "updater": None}

    def _render(snapshot: InspectorSnapshot | None = None, *, notify: bool = False) -> None:
        if state is not None and state.active_developer_workspace_id != workspace.id:
            return
        started = time.perf_counter()
        snapshot = snapshot or get_snapshot(workspace.id, thread_id)
        if snapshot is None:
            request_snapshot_refresh(workspace.id, thread_id, reason="initial")
            if version_state["version"] == -1:
                host.clear()
                with host:
                    ui.label("Loading Developer Inspector...").classes("text-sm text-grey-6")
            return
        version_state["version"] = snapshot.version
        updater = version_state.get("updater")
        if updater is None:
            host.clear()
            with host:
                updater = _build_developer_inspector_static(
                    snapshot,
                    state=state,
                    add_chat_message=add_chat_message,
                    on_refresh=lambda: _force_refresh(),
                )
            version_state["updater"] = updater
        else:
            updater(snapshot)  # type: ignore[operator]
        elapsed = time.perf_counter() - started
        if elapsed > 0.5:
            logger.warning("perf: developer inspector refresh took %.3fs workspace=%s", elapsed, workspace.id)
        if notify:
            ui.notify("Developer Inspector refreshed", type="positive")

    def _force_refresh() -> None:
        request_snapshot_refresh(workspace.id, thread_id, reason="manual", debounce=0.05)
        version_state["last_request"] = time.time()

    def _poll() -> None:
        if state is not None and state.active_developer_workspace_id != workspace.id:
            return
        now = time.time()
        snapshot = get_snapshot(workspace.id, thread_id)
        if snapshot is None:
            if now - version_state["last_request"] > 1.0:
                request_snapshot_refresh(workspace.id, thread_id, reason="poll_missing")
                version_state["last_request"] = now
            return
        if snapshot.version != version_state["version"]:
            _render(snapshot)
        if now - version_state["last_request"] > 6.0:
            request_snapshot_refresh(workspace.id, thread_id, reason="active_poll", debounce=0.8)
            version_state["last_request"] = now

    request_snapshot_refresh(workspace.id, thread_id, reason="open", debounce=0.05)
    _render()
    safe_timer(1.0, _poll)
    return lambda: _force_refresh()


def _build_developer_inspector_static(
    snapshot: InspectorSnapshot,
    *,
    state: AppState | None = None,
    add_chat_message: Callable | None = None,
    on_refresh: Callable[[], None] | None = None,
) -> Callable[[InspectorSnapshot], None]:
    from developer.edits import revert_change_set
    from developer.github import create_pull_request, get_gh_status, push_current_branch, suggest_pull_request_text
    from developer.review import get_file_diff, list_workspace_files, read_file_preview
    from developer.runtime import (
        run_workspace_command,
        start_workspace_process,
        stop_workspace_processes,
    )
    from developer.sandbox_runtime import cleanup_workspace_sandbox, rebuild_docker_sandbox
    from developer.storage import set_workspace_execution_settings

    workspace = snapshot.workspace
    panel_id = f"developer-inspector-panel-{id(workspace)}"
    resize_id = f"developer-inspector-resizer-{id(workspace)}"

    with ui.row().classes("h-full no-wrap gap-0").style("flex-shrink: 0; overflow: hidden;"):
        drag_handle = ui.element("div").style(
            "width: 8px; cursor: ew-resize; flex-shrink: 0; align-self: stretch; "
            "background: transparent; border-radius: 8px; transition: background 0.15s;"
        )
        drag_handle._props["id"] = resize_id
        drag_handle.on("mouseenter", lambda: drag_handle.style("background: rgba(88,166,255,0.35);"))
        drag_handle.on("mouseleave", lambda: drag_handle.style("background: transparent;"))

        inspector_panel = ui.column().classes("h-full gap-2 thoth-inner-panel").style(
            "width: clamp(560px, 34vw, 680px); min-width: 560px; max-width: 65vw; "
            "overflow-y: auto; padding: 0.75rem;"
        )
        inspector_panel._props["id"] = panel_id

    ui.run_javascript(f"""
    (function() {{
        const handle = document.getElementById({resize_id!r});
        const panel = document.getElementById({panel_id!r});
        const storageKey = 'thothDeveloperInspectorWidth';
        if (!handle || !panel || handle.dataset.thothResizable === '1') return;
        handle.dataset.thothResizable = '1';

        function clampWidth(width) {{
            const viewportMax = Math.max(560, Math.min(920, window.innerWidth * 0.65));
            return Math.max(420, Math.min(viewportMax, width));
        }}

        function applyWidth(width) {{
            const clamped = clampWidth(width);
            panel.style.width = clamped + 'px';
            panel.style.minWidth = clamped + 'px';
            panel.style.maxWidth = '65vw';
        }}

        const saved = Number.parseInt(localStorage.getItem(storageKey) || '', 10);
        if (Number.isFinite(saved)) applyWidth(saved);

        handle.addEventListener('mousedown', function(e) {{
            const startX = e.clientX;
            const startWidth = panel.offsetWidth;
            e.preventDefault();
            e.stopPropagation();
            document.body.style.userSelect = 'none';
            document.body.style.cursor = 'ew-resize';

            function onMove(ev) {{
                const delta = startX - ev.clientX;
                applyWidth(startWidth + delta);
            }}

            function onUp() {{
                document.removeEventListener('mousemove', onMove);
                document.removeEventListener('mouseup', onUp);
                document.body.style.userSelect = '';
                document.body.style.cursor = '';
                localStorage.setItem(storageKey, String(panel.offsetWidth));
                window.dispatchEvent(new Event('resize'));
            }}

            document.addEventListener('mousemove', onMove);
            document.addEventListener('mouseup', onUp);
        }});
    }})();
    """)

    latest: dict[str, InspectorSnapshot] = {"snapshot": snapshot}
    section_keys: dict[str, str] = {}
    section_bodies: dict[str, object] = {}
    file_tree_state: dict[str, object] = {
        "loaded": False,
        "files": [],
        "tree": None,
        "tree_box": None,
        "viewer": None,
        "file_node_paths": {},
    }
    diff_row_state: dict[str, dict[str, bool]] = {}

    def _section_key(payload: object) -> str:
        return repr(payload)

    def _render_if_changed(name: str, payload: object, body: object, renderer: Callable[[], None]) -> None:
        key = _section_key(payload)
        if section_keys.get(name) == key:
            return
        section_keys[name] = key
        body.clear()
        with body:
            renderer()

    def _show_current_workspace() -> DeveloperWorkspace:
        return latest["snapshot"].workspace

    async def _load_diff(path: str, target: object) -> None:
        target.clear()
        with target:
            ui.label("Loading diff...").classes("text-xs text-grey-6")
        diff = await run.io_bound(get_file_diff, _show_current_workspace().path, path)
        target.clear()
        with target:
            if diff:
                ui.code(diff).classes("w-full text-xs")
            else:
                ui.label("No textual diff available. The file may be untracked or binary.").classes("text-xs text-grey-6")

    async def _show_file(path: str, viewer: object) -> None:
        viewer.clear()
        with viewer:
            ui.label(path).classes("text-xs text-grey-6 ellipsis")
            ui.label("Loading file preview...").classes("text-xs text-grey-6")
        try:
            preview = await run.io_bound(read_file_preview, _show_current_workspace().path, path)
        except Exception as exc:
            preview = str(exc)
        viewer.clear()
        with viewer:
            ui.label(path).classes("text-xs text-grey-6 ellipsis")
            ui.code(preview or "Empty file.").classes("w-full text-xs")

    def _apply_file_tree(tree_box: object, viewer: object) -> None:
        files = list(file_tree_state.get("files") or [])
        changed_paths = {item.path for item in latest["snapshot"].changed_files}
        nodes, file_node_paths, folder_node_ids = _files_to_tree_nodes(files, changed_paths)
        file_tree_state["file_node_paths"] = file_node_paths

        def _select(e) -> None:
            path_map = file_tree_state.get("file_node_paths") or {}
            file_path = path_map.get(str(e.value or ""))
            if file_path:
                safe_ui_task(lambda: _show_file(file_path, viewer), context="developer preview file")

        tree = file_tree_state.get("tree")
        if tree is None:
            tree_box.clear()
            with tree_box:
                ui.label(f"Showing {len(files)} files from this workspace.").classes("text-xs text-grey-6")
                tree = ui.tree(nodes, on_select=_select).classes("w-full developer-file-tree").props(
                    "dense no-connectors selected-color=primary"
                )
                tree.expand(sorted(folder_node_ids)[:32])
            file_tree_state["tree"] = tree
        else:
            tree._props["nodes"] = nodes
            tree.update()

    with inspector_panel:
        with ui.row().classes("w-full items-center justify-between no-wrap"):
            ui.label("Developer Inspector").classes("text-h6")
            ui.button(
                icon="refresh",
                on_click=lambda: safe_ui_task(on_refresh, context="developer inspector refresh") if on_refresh else None,
            ).props("flat dense round").tooltip("Refresh inspector")

        for name, label, icon, opened in (
            ("overview", "Overview", "dashboard", True),
            ("safety", "Safety Policy", "shield", True),
            ("sandbox", "Sandbox", "inventory_2", True),
            ("todos", "Todos", "checklist", True),
            ("changes", "Changes", "difference", bool(snapshot.changed_files)),
            ("files", "Files", "folder_open", False),
            ("agent_changes", "Agent Changes", "history", bool(snapshot.agent_changes)),
            ("tests", "Tests", "science", False),
            ("github", "GitHub / PR", "account_tree", False),
        ):
            with ui.expansion(label, icon=icon).classes("w-full").props("default-opened" if opened else ""):
                section_bodies[name] = ui.column().classes("w-full gap-1")

    def _render_sections(next_snapshot: InspectorSnapshot) -> None:
        latest["snapshot"] = next_snapshot
        workspace_now = next_snapshot.workspace
        git_summary = next_snapshot.git_summary
        todos = next_snapshot.todos
        changed_files = next_snapshot.changed_files
        diff_stats = next_snapshot.diff_stats
        agent_changes = next_snapshot.agent_changes
        command_specs = next_snapshot.command_specs
        sandbox_probe = next_snapshot.sandbox_probe
        sandbox_status = next_snapshot.sandbox_status
        sandbox_pending = next_snapshot.sandbox_pending_changes

        def _overview() -> None:
            ui.label(f"Workspace: {workspace_now.name}").classes("text-sm")
            ui.label(f"Mode: {workspace_now.approval_mode.replace('_', ' ').title()}").classes("text-sm text-grey-6")
            ui.label(_APPROVAL_MODE_HELP.get(workspace_now.approval_mode, "")).classes("text-xs text-grey-6")
            if diff_stats and diff_stats.files:
                with ui.row().classes("items-center gap-2"):
                    ui.badge(f"{diff_stats.files} files", color="grey").props("outline")
                    ui.badge(f"+{diff_stats.additions}", color="green").props("outline")
                    ui.badge(f"-{diff_stats.deletions}", color="red").props("outline")
            if git_summary.get("remote"):
                ui.label(f"Remote: {git_summary['remote']}").classes("text-xs text-grey-6 ellipsis")
            if git_summary.get("ahead_behind"):
                ui.label(git_summary["ahead_behind"]).classes("text-xs text-grey-6 ellipsis")
            if git_summary.get("dirty"):
                ui.label("Uncommitted changes detected. Developer will treat this workspace as dirty.").classes("text-xs text-orange-4")

        _render_if_changed("overview", (workspace_now.name, workspace_now.path, workspace_now.approval_mode, git_summary, diff_stats), section_bodies["overview"], _overview)

        def _safety() -> None:
            ui.label(_APPROVAL_MODE_HELP.get(workspace_now.approval_mode, "")).classes("text-xs text-grey-6 q-mb-xs")
            for action in (
                "read",
                "edit",
                "run_safe_command",
                "start_server",
                "run_install",
                "git_commit",
                "git_push",
            ):
                decision = decide_action(workspace_now.approval_mode, action)  # type: ignore[arg-type]
                color = {"allow": "green", "ask": "amber", "block": "red"}.get(decision.decision, "grey")
                with ui.row().classes("w-full items-center justify-between no-wrap"):
                    ui.label(action.replace("_", " ").title()).classes("text-xs")
                    ui.badge(decision.decision, color=color).props("outline")

        _render_if_changed("safety", workspace_now.approval_mode, section_bodies["safety"], _safety)

        def _sandbox() -> None:
            async def _set_network(value: str) -> None:
                try:
                    updated = await run.io_bound(
                        set_workspace_execution_settings,
                        workspace_now.id,
                        sandbox_network=value,
                    )
                except Exception as exc:
                    ui.notify(str(exc), type="negative", close_button=True)
                    return
                latest["snapshot"].workspace.sandbox_network = updated.sandbox_network
                request_snapshot_refresh(workspace_now.id, next_snapshot.thread_id, reason="sandbox_network", debounce=0.1)
                ui.notify(f"Sandbox network: {value}", type="info")

            async def _set_image(value: str) -> None:
                image = str(value or "").strip()
                if not image:
                    ui.notify("Sandbox image cannot be empty", type="warning")
                    return
                try:
                    updated = await run.io_bound(
                        set_workspace_execution_settings,
                        workspace_now.id,
                        sandbox_image=image,
                    )
                except Exception as exc:
                    logger.exception("Failed to save Developer sandbox image for workspace %s", workspace_now.id)
                    ui.notify(str(exc), type="negative", close_button=True)
                    return
                cleanup_failed = False
                try:
                    await run.io_bound(cleanup_workspace_sandbox, workspace_now.id)
                except Exception as exc:
                    cleanup_failed = True
                    logger.exception(
                        "Saved Developer sandbox image but failed to clean sandbox for workspace %s",
                        workspace_now.id,
                    )
                latest["snapshot"].workspace.sandbox_image = updated.sandbox_image
                request_snapshot_refresh(workspace_now.id, next_snapshot.thread_id, reason="sandbox_image", debounce=0.1)
                if cleanup_failed:
                    ui.notify(
                        "Sandbox image saved, but the old sandbox could not be fully cleaned. See logs; close running sandbox commands or restart Docker, then use Clean sandbox copy.",
                        type="warning",
                        close_button=True,
                    )
                else:
                    ui.notify("Sandbox image saved. The next Docker command will use it.", type="positive")

            async def _cleanup() -> None:
                try:
                    removed = await run.io_bound(cleanup_workspace_sandbox, workspace_now.id)
                except Exception as exc:
                    logger.exception("Failed to clean Developer sandbox for workspace %s", workspace_now.id)
                    ui.notify(str(exc), type="negative", close_button=True)
                    return
                ui.notify("Docker Sandbox cleaned up" if removed else "No Docker Sandbox files to clean up", type="positive")
                if on_refresh is not None:
                    on_refresh()

            async def _rebuild() -> None:
                try:
                    await run.io_bound(rebuild_docker_sandbox, workspace_now)
                except Exception as exc:
                    logger.exception("Failed to rebuild Developer sandbox for workspace %s", workspace_now.id)
                    ui.notify(str(exc), type="negative", close_button=True)
                    return
                ui.notify("Docker Sandbox rebuilt from the current repo folder", type="positive")
                if on_refresh is not None:
                    on_refresh()

            with ui.row().classes("items-center gap-2 flex-wrap"):
                ui.badge("Docker Sandbox" if workspace_now.execution_mode == "docker" else "Local", color="purple" if workspace_now.execution_mode == "docker" else "grey").props("outline")
                if workspace_now.execution_mode == "docker":
                    sandbox_ready = bool(sandbox_status and sandbox_status.available)
                    ui.badge("available" if sandbox_ready else "not ready", color="green" if sandbox_ready else "orange").props("outline")
            if workspace_now.execution_mode == "local":
                ui.label("Commands run in the selected repo folder, with Developer approval policy guarding changes.").classes("text-xs text-grey-6")
            else:
                ui.label("Commands run in a Docker shadow copy. The real repo changes only after importing an approved sandbox patch.").classes("text-xs text-grey-6")
                image_input = ui.input(
                    "Sandbox image",
                    value=workspace_now.sandbox_image,
                    placeholder="python:3.11-slim",
                ).props("dense outlined").classes("w-full text-xs")
                with ui.row().classes("items-center gap-2 flex-wrap"):
                    ui.button(
                        "Save image",
                        icon="save",
                        on_click=lambda: safe_ui_task(
                            lambda: _set_image(image_input.value),
                            context="developer sandbox image change",
                        ),
                    ).props("dense outline no-caps size=sm")
                    ui.label("Changing the image cleans the current sandbox copy. Pending imported repo files are not deleted.").classes("text-xs text-grey-7")
                if sandbox_status is not None:
                    with ui.row().classes("items-center gap-2 flex-wrap"):
                        ui.badge("running" if sandbox_status.running else ("stopped" if sandbox_status.exists else "not created"), color="green" if sandbox_status.running else "grey").props("outline")
                        if sandbox_status.container_name:
                            ui.badge(sandbox_status.container_name, color="blue-grey").props("outline")
                    if sandbox_status.shadow_workspace:
                        ui.label(f"Shadow: {sandbox_status.shadow_workspace}").classes("text-xs text-grey-7 ellipsis")
                    if sandbox_status.processes:
                        ui.label("Running sandbox processes").classes("text-xs text-grey-5")
                        for proc in sandbox_status.processes[:5]:
                            ui.label(f"PID {proc.pid}: {proc.command}").classes("text-xs text-grey-6 ellipsis")
                if sandbox_probe.version:
                    ui.label(sandbox_probe.version).classes("text-xs text-grey-7 ellipsis")
                elif sandbox_probe.message:
                    ui.label(sandbox_probe.message).classes("text-xs text-red-4")
                if sandbox_status and sandbox_status.message and not sandbox_status.available:
                    ui.label(sandbox_status.message).classes("text-xs text-orange-4")
                ui.select(
                    {"off": "Network Off", "ask": "Network Ask", "on": "Network On"},
                    value=workspace_now.sandbox_network,
                    on_change=lambda e: safe_ui_task(
                        lambda: _set_network(e.value),
                        context="developer sandbox network change",
                    ),
                ).props("dense outlined").classes("w-full text-xs")
                if sandbox_pending:
                    ui.label("Pending sandbox patches").classes("text-xs text-grey-5")
                    for pending in sandbox_pending[:5]:
                        with ui.column().classes("w-full gap-1"):
                            ui.label(f"{pending.id} · {len(pending.files)} file(s)").classes("text-xs")
                            ui.label(pending.command).classes("text-xs text-grey-6 ellipsis")
                            for path in pending.files[:4]:
                                ui.label(f"- {path}").classes("text-xs text-grey-7")
                else:
                    ui.label("No pending sandbox patches.").classes("text-xs text-grey-6")
                with ui.row().classes("w-full gap-2 flex-wrap"):
                    ui.button(
                        "Rebuild sandbox",
                        icon="refresh",
                        on_click=lambda: safe_ui_task(_rebuild, context="developer rebuild sandbox"),
                    ).props("dense outline no-caps size=sm")
                    ui.button(
                        "Clean sandbox copy",
                        icon="cleaning_services",
                        on_click=lambda: safe_ui_task(_cleanup, context="developer cleanup sandbox"),
                    ).props("dense outline no-caps size=sm")

        _render_if_changed(
            "sandbox",
            (
                workspace_now.execution_mode,
                workspace_now.sandbox_network,
                workspace_now.sandbox_image,
                sandbox_probe,
                sandbox_status,
                [(p.id, p.command, tuple(p.files), p.imported) for p in sandbox_pending],
            ),
            section_bodies["sandbox"],
            _sandbox,
        )

        def _todos() -> None:
            if todos:
                status_color = {"pending": "grey", "in_progress": "blue", "completed": "green", "blocked": "orange"}
                for todo in todos:
                    with ui.row().classes("w-full items-center justify-between no-wrap"):
                        ui.label(todo.label).classes("text-sm ellipsis").style("min-width: 0;")
                        ui.badge(todo.status.replace("_", " "), color=status_color.get(todo.status, "grey")).props("outline")
                    if todo.detail:
                        ui.label(todo.detail).classes("text-xs text-grey-6")
            else:
                ui.label("Todos will appear here as Developer turns a plan into steps.").classes("text-sm text-grey-6")

        _render_if_changed("todos", [(t.id, t.label, t.status, t.detail) for t in todos], section_bodies["todos"], _todos)

        def _changes() -> None:
            if changed_files:
                for changed in changed_files[:12]:
                    with ui.column().classes("w-full gap-1 q-py-xs").style("border-bottom: 1px solid rgba(255,255,255,0.08);"):
                        row_state = diff_row_state.setdefault(changed.path, {"visible": False, "loaded": False})
                        diff_box = ui.column().classes("w-full gap-1")
                        diff_box.set_visibility(bool(row_state.get("visible")))
                        if row_state.get("visible"):
                            row_state["loaded"] = False

                        def _toggle_diff(box=diff_box, path=changed.path, state_row=row_state) -> None:
                            visible = not bool(state_row.get("visible"))
                            state_row["visible"] = visible
                            box.set_visibility(visible)
                            if visible and not state_row.get("loaded"):
                                state_row["loaded"] = True
                                safe_ui_task(
                                    lambda: _load_diff(path, box),
                                    context="developer load file diff",
                                )

                        with ui.row().classes("w-full items-center justify-between no-wrap"):
                            with ui.row().classes("items-center gap-2 no-wrap").style("min-width: 0;"):
                                ui.icon("description", size="xs").classes("text-grey-4")
                                ui.label(f"{changed.status} {changed.path}").classes("text-sm ellipsis").style("min-width: 0;")
                                if changed.additions or changed.deletions:
                                    ui.badge(f"+{changed.additions}", color="green").props("outline")
                                    ui.badge(f"-{changed.deletions}", color="red").props("outline")
                            ui.button(
                                icon="expand_more",
                                on_click=_toggle_diff,
                            ).props("flat dense round").tooltip("Show diff")
                        with diff_box:
                            ui.label("Expand to load diff.").classes("text-xs text-grey-6")
                        if row_state.get("visible") and not row_state.get("loaded"):
                            row_state["loaded"] = True
                            safe_ui_task(
                                lambda path=changed.path, box=diff_box: _load_diff(path, box),
                                context="developer reload visible file diff",
                            )
                if len(changed_files) > 12:
                    ui.label(f"{len(changed_files) - 12} more changed files not shown.").classes("text-xs text-grey-6")
            else:
                ui.label("No changed files detected.").classes("text-sm text-grey-6")

        _render_if_changed("changes", [(c.path, c.status, c.additions, c.deletions) for c in changed_files], section_bodies["changes"], _changes)

        def _files() -> None:
            viewer = ui.column().classes("w-full gap-1")
            tree_box = ui.column().classes("w-full gap-1")
            file_tree_state["viewer"] = viewer
            file_tree_state["tree_box"] = tree_box

            async def _load_files() -> None:
                if not file_tree_state.get("loaded"):
                    tree_box.clear()
                    with tree_box:
                        ui.label("Loading file tree...").classes("text-xs text-grey-6")
                files = await run.io_bound(list_workspace_files, workspace_now.path, limit=160)
                file_tree_state["loaded"] = True
                file_tree_state["files"] = files
                _apply_file_tree(tree_box, viewer)

            ui.button(
                "Load file tree",
                icon="folder_open",
                on_click=lambda: safe_ui_task(_load_files, context="developer load file list"),
            ).props("flat dense no-caps")
            with tree_box:
                ui.label("Load the file tree, then select a file to preview it here.").classes("text-xs text-grey-6")
            with viewer:
                ui.label("File preview will appear here.").classes("text-xs text-grey-6")

        _render_if_changed("files", "static-files-shell", section_bodies["files"], _files)
        if file_tree_state.get("loaded") and file_tree_state.get("tree") is not None:
            tree_box = file_tree_state.get("tree_box")
            viewer = file_tree_state.get("viewer")
            if tree_box is not None and viewer is not None:
                _apply_file_tree(tree_box, viewer)

        def _agent_changes() -> None:
            if agent_changes:
                async def _revert(change_set_id: str) -> None:
                    try:
                        message = await run.io_bound(revert_change_set, workspace_now.id, change_set_id)
                    except Exception as exc:
                        ui.notify(str(exc), type="negative", close_button=True)
                        return
                    ui.notify(message, type="positive")
                    audit_msg = {"role": "assistant", "content": f"Developer Inspector reverted change set `{change_set_id}`. {message}"}
                    if state is not None:
                        state.messages.append(audit_msg)
                    if add_chat_message is not None:
                        add_chat_message(audit_msg)
                    if on_refresh is not None:
                        on_refresh()

                for change_set in agent_changes[:8]:
                    with ui.column().classes("w-full gap-1"):
                        with ui.row().classes("w-full items-center justify-between no-wrap"):
                            ui.label(change_set.summary or change_set.id).classes("text-sm ellipsis").style("min-width: 0;")
                            ui.button(
                                "Revert",
                                icon="undo",
                                on_click=lambda cid=change_set.id: safe_ui_task(
                                    lambda: _revert(cid),
                                    context="developer revert agent changes",
                                ),
                            ).props("dense outline no-caps size=sm")
                        for file_change in change_set.files[:5]:
                            ui.label(f"{file_change.action} {file_change.path}").classes("text-xs text-grey-6")
            else:
                ui.label("Agent-owned patches will appear here after Developer applies changes.").classes("text-sm text-grey-6")

        _render_if_changed("agent_changes", [(c.id, c.summary, c.reverted, [(f.path, f.action) for f in c.files]) for c in agent_changes], section_bodies["agent_changes"], _agent_changes)

        def _tests() -> None:
            output_box = ui.column().classes("w-full gap-1")

            async def _run_detected(command: str, kind: str) -> None:
                output_box.clear()
                with output_box:
                    ui.label(f"{'Starting' if kind == 'server' else 'Running'}: {command}").classes("text-xs text-grey-6")
                try:
                    if kind == "server":
                        result = await run.io_bound(
                            partial(
                                start_workspace_process,
                                workspace_now.path,
                                command,
                                workspace_now.approval_mode,
                                workspace_id=workspace_now.id,
                                thread_id=next_snapshot.thread_id or "",
                            )
                        )
                    else:
                        result = await run.io_bound(
                            run_workspace_command,
                            workspace_now.path,
                            command,
                            workspace_now.approval_mode,
                            workspace_id=workspace_now.id,
                            thread_id=next_snapshot.thread_id or "",
                        )
                except Exception as exc:
                    output_box.clear()
                    with output_box:
                        ui.label(str(exc)).classes("text-negative text-xs")
                    return
                output_box.clear()
                with output_box:
                    if result.ran:
                        ui.badge(f"exit {result.returncode}", color="green" if result.ok else "red").props("outline")
                        if result.stdout:
                            ui.code(result.stdout).classes("w-full text-xs")
                        if result.stderr:
                            ui.code(result.stderr).classes("w-full text-xs")
                    else:
                        ui.badge(result.decision.decision if result.decision else "blocked", color="amber").props("outline")
                        ui.label(result.stderr or "Command was not run.").classes("text-xs text-grey-6")

            if command_specs:
                for spec in command_specs:
                    with ui.row().classes("w-full items-center justify-between no-wrap"):
                        with ui.row().classes("items-center no-wrap gap-2").style("min-width: 0;"):
                            ui.label(spec.label).classes("text-sm ellipsis").style("min-width: 0;")
                            ui.badge(spec.kind, color="blue" if spec.kind == "server" else "grey").props("outline")
                        ui.button(
                            "Start" if spec.kind == "server" else "Run",
                            icon="play_arrow",
                            on_click=lambda cmd=spec.command, kind=spec.kind: safe_ui_task(
                                lambda: _run_detected(cmd, kind),
                                context="developer run detected command",
                            ),
                        ).props("dense outline no-caps size=sm")
                if any(spec.kind == "server" for spec in command_specs):
                    async def _stop_servers() -> None:
                        stopped = await run.io_bound(partial(stop_workspace_processes, workspace_now.path, workspace_id=workspace_now.id))
                        output_box.clear()
                        with output_box:
                            ui.badge(f"stopped {stopped}", color="grey").props("outline")

                    ui.button("Stop servers", icon="stop", on_click=lambda: safe_ui_task(_stop_servers, context="developer stop workspace servers")).props("dense outline no-caps")
            else:
                ui.label("No common test command detected yet.").classes("text-sm text-grey-6")

        _render_if_changed("tests", [(s.label, s.command, s.kind) for s in command_specs], section_bodies["tests"], _tests)

        def _github() -> None:
            status_box = ui.column().classes("w-full gap-1")
            action_box = ui.column().classes("w-full gap-1")
            pr_preview = None
            if git_summary.get("is_git"):
                try:
                    pr_preview = suggest_pull_request_text(workspace_now.path)
                except Exception:
                    pr_preview = None
            pr_title = ui.input("PR title", value=pr_preview.title if pr_preview else "").classes("w-full").props("dense outlined")
            pr_body = ui.textarea("PR body", value=pr_preview.body if pr_preview else "").classes("w-full").props("dense outlined autogrow")

            async def _check_gh() -> None:
                status_box.clear()
                with status_box:
                    ui.label("Checking GitHub CLI...").classes("text-xs text-grey-6")
                status = await run.io_bound(get_gh_status)
                status_box.clear()
                with status_box:
                    if not status.installed:
                        ui.badge("gh missing", color="grey").props("outline")
                        ui.label("Install GitHub CLI, then run `gh auth login` in a terminal.").classes("text-xs text-grey-6")
                        ui.code("winget install --id GitHub.cli").classes("w-full text-xs")
                    elif status.authenticated:
                        label = f"gh authenticated{f' as {status.user}' if status.user else ''}"
                        ui.badge(label, color="green").props("outline")
                        ui.label(status.version).classes("text-xs text-grey-6")
                        if status.path:
                            ui.label(status.path).classes("text-xs text-grey-7 ellipsis")
                    else:
                        ui.badge("gh not signed in", color="amber").props("outline")
                        ui.label(status.message).classes("text-xs text-grey-6")
                        if status.path:
                            ui.label(status.path).classes("text-xs text-grey-7 ellipsis")

            async def _push() -> None:
                action_box.clear()
                with action_box:
                    ui.label("Pushing current branch...").classes("text-xs text-grey-6")
                result = await run.io_bound(partial(push_current_branch, workspace_now.path, workspace_now.approval_mode, confirmed=True))
                action_box.clear()
                with action_box:
                    if result.ran:
                        ui.badge("push complete" if result.ok else "push failed", color="green" if result.ok else "red").props("outline")
                        text = result.stdout or result.stderr
                        if text:
                            ui.code(text).classes("w-full text-xs")
                    else:
                        ui.badge(result.decision.decision if result.decision else "blocked", color="amber").props("outline")
                        ui.label(result.stderr or "Push was not run.").classes("text-xs text-grey-6")

            async def _create_pr() -> None:
                action_box.clear()
                with action_box:
                    ui.label("Creating pull request...").classes("text-xs text-grey-6")
                result = await run.io_bound(partial(create_pull_request, workspace_now.path, workspace_now.approval_mode, title=str(pr_title.value or ""), body=str(pr_body.value or ""), draft=False, confirmed=True))
                action_box.clear()
                with action_box:
                    if result.ran:
                        ui.badge("PR created" if result.ok else "PR failed", color="green" if result.ok else "red").props("outline")
                        if result.url:
                            ui.link(result.url, result.url).classes("text-xs")
                        text = result.stdout or result.stderr
                        if text:
                            ui.code(text).classes("w-full text-xs")
                    else:
                        ui.badge(result.decision.decision if result.decision else "blocked", color="amber").props("outline")
                        ui.label(result.stderr or "Pull request was not created.").classes("text-xs text-grey-6")

            ui.button("Check gh status", icon="verified", on_click=lambda: safe_ui_task(_check_gh, context="developer check gh status")).props("dense outline no-caps")
            with status_box:
                ui.label("GitHub CLI status has not been checked in this session.").classes("text-xs text-grey-6")
            if git_summary.get("is_git"):
                with ui.row().classes("w-full gap-2"):
                    ui.button("Push", icon="upload", on_click=lambda: safe_ui_task(_push, context="developer git push")).props("dense outline no-caps")
                    ui.button("Create PR", icon="merge_type", on_click=lambda: safe_ui_task(_create_pr, context="developer create pr")).props("dense outline no-caps")
            else:
                ui.label("Open a Git repository to enable push and PR actions.").classes("text-sm text-grey-6")

        _render_if_changed("github", (git_summary.get("is_git"), git_summary.get("branch"), git_summary.get("remote")), section_bodies["github"], _github)

    _render_sections(snapshot)
    return _render_sections
