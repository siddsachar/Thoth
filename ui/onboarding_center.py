"""Resumable onboarding Setup Center."""

from __future__ import annotations

from typing import Callable

from nicegui import ui

from ui.onboarding_state import (
    SETUP_STEPS,
    dismiss_onboarding_home_card,
    mark_onboarding_step,
    onboarding_progress,
)


_INTENT_STEP_PRIORITY: dict[str, tuple[str, ...]] = {
    "chat": ("models", "tools"),
    "research": ("knowledge", "tools", "extensions"),
    "workflows": ("workflows", "channels", "accounts"),
    "designer": ("designer", "knowledge"),
    "channels": ("channels", "accounts"),
    "local": ("models", "knowledge"),
}


def _priority_steps_for_profile(profile: list[str]) -> list[str]:
    priority: list[str] = []
    for intent in profile:
        for step in _INTENT_STEP_PRIORITY.get(str(intent), ()):
            if step in SETUP_STEPS and step not in priority:
                priority.append(step)
    return priority


def _ordered_setup_steps(progress: dict) -> list[tuple[str, dict[str, str]]]:
    priority = _priority_steps_for_profile(progress.get("profile") or [])
    ordered = priority + [step for step in SETUP_STEPS if step not in priority]
    return [(step, SETUP_STEPS[step]) for step in ordered]


def _step_icon(step: str) -> str:
    return {
        "models": "smart_toy",
        "knowledge": "psychology",
        "workflows": "bolt",
        "designer": "design_services",
        "channels": "forum",
        "accounts": "group",
        "tools": "construction",
        "extensions": "extension",
        "voice": "mic",
        "final": "task_alt",
    }.get(step, "radio_button_unchecked")


def _settings_tab_for_step(step: str) -> str | None:
    return {
        "models": "Models",
        "knowledge": "Documents",
        "channels": "Channels",
        "accounts": "Accounts",
        "tools": "Search",
        "extensions": "MCP",
        "voice": "Voice",
    }.get(step)


def _missing_starter_workflow_count() -> int:
    try:
        from tasks import _DEFAULT_TASKS, list_tasks

        existing = {str(t.get("name") or "") for t in list_tasks()}
        return sum(1 for template in _DEFAULT_TASKS if template["name"] not in existing)
    except Exception:
        return 0


def show_setup_center(
    *,
    open_settings: Callable[[str], None] | None = None,
    rebuild_main: Callable[[], None] | None = None,
    state=None,
) -> None:
    """Open the guided, resumable onboarding checklist."""

    progress = onboarding_progress()
    completed = set(progress["completed_steps"])
    skipped = set(progress["skipped_steps"])
    priority_steps = set(_priority_steps_for_profile(progress.get("profile") or []))

    def _refresh(dialog) -> None:
        dialog.close()
        show_setup_center(open_settings=open_settings, rebuild_main=rebuild_main, state=state)

    with ui.dialog().props("maximized") as dialog:
        with ui.card().classes("w-full h-full no-shadow").style(
            "max-width: 78rem; margin: 0 auto; background: #151515;"
        ):
            with ui.row().classes("w-full items-center justify-between px-5 pt-4 pb-2"):
                with ui.column().classes("gap-0"):
                    ui.label("Setup Center").classes("text-h4 text-weight-medium")
                    ui.label(
                        "Finish the parts of Thoth you want now. Everything here is optional after model setup."
                    ).classes("text-grey-6 text-sm")
                ui.button(icon="close", on_click=dialog.close).props("flat round size=sm")

            ui.separator()

            with ui.scroll_area().classes("w-full").style("height: calc(100vh - 92px);"):
                with ui.column().classes("w-full gap-4 q-pa-md"):
                    with ui.row().classes("w-full items-center gap-3"):
                        ui.linear_progress(
                            value=(progress["done"] / progress["total"]) if progress["total"] else 1,
                            show_value=False,
                        ).classes("col")
                        ui.badge(
                            f"{progress['done']} of {progress['total']} handled",
                            color="blue-grey",
                        ).props("outline")
                    if priority_steps:
                        priority_titles = [
                            SETUP_STEPS[step]["title"]
                            for step, _meta in _ordered_setup_steps(progress)
                            if step in priority_steps
                        ]
                        ui.label(
                            f"Recommended from your choices: {', '.join(priority_titles)}. "
                            "All setup areas remain available."
                        ).classes("text-grey-6 text-sm")

                    with ui.element("div").classes("w-full").style(
                        "display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 12px;"
                    ):
                        for step, meta in _ordered_setup_steps(progress):
                            is_done = step in completed
                            is_skipped = step in skipped
                            is_priority = step in priority_steps
                            status = "Done" if is_done else "Skipped" if is_skipped else "Open"
                            color = (
                                "#22c55e"
                                if is_done
                                else "#94a3b8"
                                if is_skipped
                                else "#38bdf8"
                                if is_priority
                                else "#f59e0b"
                            )
                            with ui.card().classes("q-pa-md").style(
                                "border-radius: 8px; background: rgba(255,255,255,0.035); "
                                f"border: 1px solid {color}55;"
                            ):
                                with ui.row().classes("w-full items-start gap-2 no-wrap"):
                                    ui.icon(_step_icon(step)).style(f"color: {color}; font-size: 24px;")
                                    with ui.column().classes("gap-1").style("min-width: 0; flex: 1;"):
                                        with ui.row().classes("w-full items-center justify-between gap-2"):
                                            ui.label(meta["title"]).classes("text-subtitle1 text-weight-medium")
                                            with ui.row().classes("items-center gap-1"):
                                                if is_priority and not is_done and not is_skipped:
                                                    ui.badge("Recommended", color="light-blue").props("outline")
                                                ui.badge(
                                                    status,
                                                    color="green" if is_done else "grey" if is_skipped else "orange",
                                                )
                                        ui.label(meta["description"]).classes("text-grey-6 text-sm")

                                with ui.row().classes("w-full items-center justify-end gap-1 q-mt-sm"):
                                    tab = _settings_tab_for_step(step)
                                    if step == "workflows":
                                        missing_starters = _missing_starter_workflow_count()
                                        if missing_starters:
                                            def _add_templates(s=step):
                                                from tasks import add_default_workflow_templates

                                                created = add_default_workflow_templates()
                                                mark_onboarding_step(s)
                                                ui.notify(
                                                    f"Added {created} starter workflow{'s' if created != 1 else ''}",
                                                    type="positive" if created else "info",
                                                )
                                                if rebuild_main:
                                                    rebuild_main()
                                                _refresh(dialog)

                                            ui.button(
                                                f"Add {missing_starters} starter{'s' if missing_starters != 1 else ''}",
                                                icon="add_task",
                                                on_click=_add_templates,
                                            ).props("flat dense no-caps color=primary")
                                        else:
                                            def _open_workflows(s=step):
                                                mark_onboarding_step(s)
                                                dialog.close()
                                                if rebuild_main:
                                                    rebuild_main()

                                            ui.button(
                                                "Review",
                                                icon="open_in_new",
                                                on_click=_open_workflows,
                                            ).props("flat dense no-caps color=primary")
                                    elif step == "designer":
                                        def _go_designer(s=step):
                                            mark_onboarding_step(s)
                                            if state is not None:
                                                state.thread_id = None
                                                state.thread_name = None
                                                state.messages = []
                                                state.active_designer_project = None
                                                state.preferred_home_tab = "Designer"
                                            if rebuild_main:
                                                rebuild_main()
                                            dialog.close()

                                        ui.button(
                                            "Open Designer",
                                            icon="design_services",
                                            on_click=_go_designer,
                                        ).props("flat dense no-caps color=primary")
                                    elif tab and open_settings:
                                        def _open(tab_name=tab, s=step):
                                            mark_onboarding_step(s)
                                            dialog.close()
                                            open_settings(tab_name)

                                        ui.button(
                                            "Open",
                                            icon="open_in_new",
                                            on_click=_open,
                                        ).props("flat dense no-caps color=primary")
                                    else:
                                        ui.button(
                                            "Mark done",
                                            icon="check",
                                            on_click=lambda s=step: (
                                                mark_onboarding_step(s),
                                                _refresh(dialog),
                                            ),
                                        ).props("flat dense no-caps color=primary")

                                    ui.button(
                                        "Skip",
                                        icon="skip_next",
                                        on_click=lambda s=step: (
                                            mark_onboarding_step(s, skipped=True),
                                            _refresh(dialog),
                                        ),
                                    ).props("flat dense no-caps")

                    with ui.row().classes("w-full justify-between items-center q-mt-sm"):
                        ui.button(
                            "Hide Home reminder",
                            icon="visibility_off",
                            on_click=lambda: (
                                dismiss_onboarding_home_card(),
                                rebuild_main() if rebuild_main else None,
                                dialog.close(),
                            ),
                        ).props("flat dense no-caps")
                        ui.button("Done", icon="check_circle", on_click=dialog.close).props(
                            "color=primary no-caps"
                        )

    dialog.open()
