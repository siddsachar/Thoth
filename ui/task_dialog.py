"""Thoth UI — task editor dialog.

Self-contained task creation / editing dialog.  Receives ``state`` and
``p`` explicitly, and a callback ``on_done`` to notify the caller when
the dialog completes.
"""

from __future__ import annotations

import copy
import json
import logging
from datetime import datetime
from typing import Callable

from nicegui import ui

from ui.state import AppState, P
from ui.constants import ICON_OPTIONS
from ui.timer_utils import safe_timer

logger = logging.getLogger(__name__)

# Step type options for the step builder
_STEP_TYPES = {
    "prompt": "Prompt",
    "condition": "Condition",
    "approval": "Approval",
    "subtask": "Sub-agent",
    "notify": "Notify",
}
_STEP_TYPE_ICONS = {
    "prompt": "💬",
    "condition": "🔀",
    "approval": "⏸️",
    "subtask": "🤖",
    "notify": "📢",
}

_CONDITION_OPERATORS = [
    "contains:", "not_contains:", "equals:", "matches:",
    "gt:", "lt:", "gte:", "lte:",
    "length_gt:", "length_lt:",
    "empty", "not_empty", "true", "false",
    "json:", "llm:",
]

_SIMPLE_CONDITION_OPS = {
    "contains:": "Contains",
    "not_contains:": "Not contains",
    "equals:": "Equals",
    "matches:": "Matches (regex)",
    "gt:": "Greater than",
    "lt:": "Less than",
    "gte:": "≥ Greater or equal",
    "lte:": "≤ Less or equal",
    "length_gt:": "Length >",
    "length_lt:": "Length <",
    "empty": "Is empty",
    "not_empty": "Is not empty",
    "true": "Always true",
    "false": "Always false",
    "json:": "🔎 JSON field check",
    "llm:": "🤖 LLM evaluation",
}
_NO_VALUE_OPS = {"empty", "not_empty", "true", "false"}
# Operators that use the standard single-value input
_STANDARD_VALUE_OPS = {
    "contains:", "not_contains:", "equals:", "matches:",
    "gt:", "lt:", "gte:", "lte:", "length_gt:", "length_lt:",
}
# Sub-operators available inside json: conditions (same basic ops)
_JSON_SUB_OPS = {
    "contains:": "Contains",
    "not_contains:": "Not contains",
    "equals:": "Equals",
    "matches:": "Matches (regex)",
    "gt:": "Greater than",
    "lt:": "Less than",
    "gte:": "≥ Greater or equal",
    "lte:": "≤ Less or equal",
    "empty": "Is empty",
    "not_empty": "Is not empty",
}
_JSON_NO_VALUE_SUB_OPS = {"empty", "not_empty"}


def _parse_condition_expr(expr: str):
    """Parse a simple condition into (operator, value/extra). Returns (None, '') for complex."""
    if not expr:
        return (None, "")
    # json:<path>:<sub_op>[:<value>]
    if expr.startswith("json:"):
        return ("json:", expr[len("json:"):])
    # llm:<question>
    if expr.startswith("llm:"):
        return ("llm:", expr[len("llm:"):])
    for op in ("not_contains:", "contains:", "not_empty", "empty",
               "equals:", "matches:", "length_gt:", "length_lt:",
               "gte:", "lte:", "gt:", "lt:", "true", "false"):
        if expr == op:  # no-value ops
            return (op, "")
        if expr.startswith(op) and op.endswith(":"):
            return (op, expr[len(op):])
    return (None, "")


def show_task_dialog(
    task: dict | None,
    on_done: Callable[[], None],
    *,
    state: AppState,
    p: P,
) -> None:
    """Open the task editor dialog.

    *task=None* → create mode (blank fields).
    *task=dict* → edit mode (pre-populated).
    """
    from models import get_current_model
    from providers.selection import list_model_choice_options, model_choice_value
    from tasks import (
        get_run_history,
        create_task,
        update_task,
        delete_task,
        duplicate_task,
        list_tasks,
        get_global_safety_mode,
        detect_circular_subtasks,
        generate_webhook_secret,
        get_workflow_draft,
        save_workflow_draft,
        delete_workflow_draft,
    )

    is_new = task is None
    title = "New Workflow" if is_new else "Edit Workflow"

    # Editable data holders
    _name = task["name"] if task else ""
    _icon = task["icon"] if task else "⚡"
    _desc = task.get("description", "") if task else ""
    _enabled = task.get("enabled", True) if task else True
    _model_ov = task.get("model_override") or "" if task else ""
    _prompts_data: list[str] = list(task["prompts"]) if task else [""]
    _safety_mode = (task.get("safety_mode") or get_global_safety_mode()) if task else "block"
    _steps_data: list[dict] = copy.deepcopy(task.get("steps") or []) if task else []
    _concurrency_group = (task.get("concurrency_group") or "") if task else ""
    _trigger_data: dict | None = task.get("trigger") if task else None
    _tools_override_data = task.get("tools_override") if task else None
    _skills_override_data = task.get("skills_override") if task else None
    _persistent_enabled = bool(task.get("persistent_thread_id")) if task else False

    _draft_key = task["id"] if task else None
    _draft_timer_ref = {"timer": None, "closing": False, "last": "", "baseline": ""}

    _draft_info = get_workflow_draft(_draft_key)
    _draft_payload = (_draft_info or {}).get("payload") if _draft_info else {}
    _draft_restored = bool(_draft_payload)
    if isinstance(_draft_payload, dict) and _draft_payload:
        _name = str(_draft_payload.get("name", _name) or "")
        _icon = str(_draft_payload.get("icon", _icon) or "⚡")
        _desc = str(_draft_payload.get("description", _desc) or "")
        _enabled = bool(_draft_payload.get("enabled", _enabled))
        _model_ov = str(_draft_payload.get("model_override") or "")
        _prompts_data = list(_draft_payload.get("prompts") or _prompts_data)
        _steps_data = copy.deepcopy(_draft_payload.get("steps") or _steps_data)
        _safety_mode = str(_draft_payload.get("safety_mode") or _safety_mode or "block")
        _concurrency_group = str(_draft_payload.get("concurrency_group") or _concurrency_group or "")
        _trigger_data = _draft_payload.get("trigger") if isinstance(_draft_payload.get("trigger"), dict) else _trigger_data
        _tools_override_data = _draft_payload.get("tools_override")
        _skills_override_data = _draft_payload.get("skills_override")
        _persistent_enabled = bool(_draft_payload.get("persistent_enabled", _persistent_enabled))

    # Determine if we should show advanced mode initially
    _has_advanced = bool(task and any(
        s.get("type") not in ("prompt", None) for s in _steps_data
    )) if _steps_data else False
    if isinstance(_draft_payload, dict) and "advanced" in _draft_payload:
        _has_advanced = bool(_draft_payload.get("advanced"))

    # Parse schedule
    _current_sched = str(_draft_payload.get("schedule") or "") if isinstance(_draft_payload, dict) and _draft_payload.get("schedule") is not None else ((task.get("schedule") or "") if task else "")
    if _current_sched.startswith("daily"):
        _sched_mode = "Daily"
    elif _current_sched.startswith("weekly"):
        _sched_mode = "Weekly"
    elif _current_sched.startswith("interval_minutes"):
        _sched_mode = "Interval (min)"
    elif _current_sched.startswith("interval"):
        _sched_mode = "Interval (hrs)"
    elif _current_sched.startswith("cron"):
        _sched_mode = "Cron"
    else:
        _sched_mode = "Manual"

    _sched_time = "08:00"
    _sched_day = "mon"
    _sched_interval = "1"
    _sched_cron = ""
    _FULL_TO_ABBR = {
        "monday": "mon", "tuesday": "tue", "wednesday": "wed",
        "thursday": "thu", "friday": "fri", "saturday": "sat",
        "sunday": "sun",
    }
    if _current_sched.startswith("daily:"):
        _sched_time = _current_sched.split(":", 1)[1]
    elif _current_sched.startswith("weekly:"):
        parts = _current_sched.split(":")
        if len(parts) >= 3:
            raw_day = parts[1].lower()
            _sched_day = _FULL_TO_ABBR.get(raw_day, raw_day[:3] if len(raw_day) > 3 else raw_day)
            _sched_time = f"{parts[2]}:{parts[3]}" if len(parts) >= 4 else "08:00"
    elif _current_sched.startswith("interval_minutes:"):
        _sched_interval = _current_sched.split(":", 1)[1]
    elif _current_sched.startswith("interval:"):
        _sched_interval = _current_sched.split(":", 1)[1]
    elif _current_sched.startswith("cron:"):
        _sched_cron = _current_sched.split(":", 1)[1]

    # Parse delivery
    _del_channel = (task.get("delivery_channel") or "") if task else ""
    _del_target = (task.get("delivery_target") or "") if task else ""
    _task_channels = task.get("channels") if task else None  # None = workflow default
    if isinstance(_draft_payload, dict) and _draft_payload:
        _del_channel = _draft_payload.get("delivery_channel") or _del_channel
        _del_target = _draft_payload.get("delivery_target") or _del_target
        if "channels" in _draft_payload:
            _task_channels = _draft_payload.get("channels")
    if task and _task_channels is None and _del_channel:
        _task_channels = [_del_channel]

    p.task_dlg.clear()
    with p.task_dlg, ui.card().classes("q-pa-none").style(
        "width: 860px; max-width: 92vw; height: 90vh; max-height: 94vh;"
        "border-radius: 16px; overflow: hidden;"
        "background: #1a1a2e; border: 1px solid #2a2a4a;"
        "display: flex; flex-direction: column;"
    ):
        # ── Header ──
        with ui.row().classes("w-full items-center q-pa-md").style(
            "background: linear-gradient(135deg, #2d1b00 0%, #1a1a2e 100%);"
            "border-bottom: 1px solid #3d2e00;"
        ):
            ui.icon("edit_note", size="28px", color="amber")
            ui.label(title).style(
                "font-size: 1.15rem; font-weight: 700; color: #f0c040; margin-left: 8px;"
            )

        # ── Body (scrollable) ──
        if _draft_restored:
            with ui.row().classes("w-full items-center gap-2 q-px-md q-py-sm").style(
                "background: rgba(59, 130, 246, 0.12); border-bottom: 1px solid rgba(96, 165, 250, 0.22);"
            ):
                ui.icon("history", size="sm").classes("text-blue-3")
                updated = (_draft_info or {}).get("updated_at") or "recently"
                ui.label(f"Recovered unsaved draft from {updated}").classes("text-blue-2 text-sm")
                ui.space()

                def _discard_recovered_draft():
                    _draft_timer_ref["closing"] = True
                    _timer = _draft_timer_ref.get("timer")
                    if _timer:
                        _timer.deactivate()
                    delete_workflow_draft(_draft_key)
                    p.task_dlg.close()
                    show_task_dialog(task, on_done, state=state, p=p)

                ui.button("Discard", icon="delete", on_click=_discard_recovered_draft).props("flat dense no-caps color=negative")

        with ui.scroll_area().style("flex: 1; min-height: 0;"):
            with ui.column().classes("w-full q-pa-lg gap-3"):
                # Name + Icon row
                with ui.row().classes("w-full items-center gap-2"):
                    _wf_icon_opts = list(ICON_OPTIONS)
                    if _icon not in _wf_icon_opts:
                        _wf_icon_opts.insert(0, _icon)
                    icon_sel = ui.select(
                        label="Icon", options=_wf_icon_opts, value=_icon,
                    ).classes("w-20")
                    name_input = ui.input(
                        "Name *", value=_name,
                    ).classes("flex-grow")

                desc_input = ui.input(
                    "Description (optional)", value=_desc,
                ).classes("w-full")

                # Enabled toggle + Safety mode
                with ui.row().classes("w-full items-center gap-4"):
                    enabled_switch = ui.switch("Enabled", value=_enabled)
                    safety_sel = ui.select(
                        label="Safety mode",
                        options={
                            "block": "🛡️ Block (no destructive tools)",
                            "approve": "⏸️ Approve (pause for approval)",
                            "allow_all": "⚡ Allow All (unrestricted)",
                        },
                        value=_safety_mode,
                    ).classes("w-64").tooltip(
                        "Block: strips destructive tools. "
                        "Approve: pauses for your approval before destructive actions. "
                        "Allow All: unrestricted (legacy default)."
                    )

                # Model override dropdown
                _default_label = "__default__"
                _model_opts_map = {_default_label: f"Default — {get_current_model()}"}
                for _option in list_model_choice_options("workflow", include_values=[_model_ov] if _model_ov else []):
                    _value = str(_option.get("value") or "")
                    if _value:
                        _model_opts_map[_value] = str(_option.get("label") or _value)
                _model_ov_value = model_choice_value(_model_ov)
                _model_val = _model_ov_value if _model_ov_value in _model_opts_map else _default_label
                model_sel = ui.select(
                    _model_opts_map, value=_model_val, label="Model",
                ).classes("w-full").tooltip(
                    "Choose which LLM runs this task. "
                    "Only tool-compatible models are listed."
                )

                # Persistent thread toggle
                persistent_toggle = ui.switch(
                    "Keep conversation history across runs",
                    value=_persistent_enabled,
                ).tooltip(
                    "When enabled, all runs share one thread so the agent "
                    "can see prior outputs. Useful for monitoring/polling tasks."
                )

                ui.separator()

                # ── Mode toggle: Simple ↔ Advanced ──
                with ui.row().classes("w-full items-center gap-3"):
                    ui.label("Steps").style(
                        "font-weight: 600; font-size: 0.9rem; color: #d0d0e0;"
                    )
                    ui.element("div").classes("flex-grow")
                    advanced_switch = ui.switch(
                        "Advanced", value=_has_advanced,
                    ).style("color: #f0c040;").tooltip(
                        "Toggle between simple prompt list and "
                        "advanced step builder with conditions, "
                        "approvals, and subtasks."
                    )

                # ── Simple mode container (prompt list) ──
                simple_container = ui.column().classes("w-full")

                # ── Advanced mode container (step builder) ──
                advanced_container = ui.column().classes("w-full")

                # ──── Simple mode: prompts editor ────
                prompt_inputs: list[ui.textarea] = []

                def _build_simple_mode():
                    simple_container.clear()
                    prompt_inputs.clear()
                    with simple_container:
                        ui.label(
                            "Leave empty for notification-only tasks (reminders). "
                            "Variables: {{date}}, {{day}}, {{time}}, {{month}}, {{year}}"
                        ).style("font-size: 0.75rem; color: #666;")

                        for i, p_text in enumerate(_prompts_data):
                            with ui.card().classes("w-full q-pa-sm q-mb-xs").style(
                                "background: #0f0f1e; border: 1px solid #2a2a4a; "
                                "border-radius: 8px;"
                            ):
                                with ui.row().classes("w-full items-start gap-1"):
                                    ta = ui.textarea(
                                        f"Step {i+1}", value=p_text,
                                    ).classes("flex-grow")
                                    prompt_inputs.append(ta)
                                    if len(_prompts_data) > 1:
                                        def _remove(idx=i):
                                            for j, _ta in enumerate(prompt_inputs):
                                                if j < len(_prompts_data):
                                                    _prompts_data[j] = _ta.value
                                            _prompts_data.pop(idx)
                                            _build_simple_mode()
                                        ui.button(
                                            icon="close", on_click=_remove,
                                        ).props("flat dense round")

                        def _add():
                            for j, _ta in enumerate(prompt_inputs):
                                if j < len(_prompts_data):
                                    _prompts_data[j] = _ta.value
                            _prompts_data.append("")
                            _build_simple_mode()

                        ui.button("＋ Add step", on_click=_add).props("flat dense")

                # ──── Advanced mode: step builder ────
                _step_editors: list[dict] = []  # holds references to UI elements per step

                def _build_advanced_mode():
                    advanced_container.clear()
                    _step_editors.clear()
                    with advanced_container:
                        ui.label(
                            "Build a pipeline with prompts, conditions, "
                            "approvals, subtasks, and notifications."
                        ).style("font-size: 0.75rem; color: #666;")

                        # If no steps and we have prompts, seed from prompts
                        if not _steps_data and _prompts_data:
                            for j, _ta in enumerate(prompt_inputs):
                                if j < len(_prompts_data):
                                    _prompts_data[j] = _ta.value
                            from tasks import _prompts_to_steps
                            _steps_data.clear()
                            _steps_data.extend(_prompts_to_steps(
                                [p for p in _prompts_data if p.strip()]
                            ))

                        _reassign_step_ids()

                        for idx, step in enumerate(_steps_data):
                            _build_step_card(idx, step, advanced_container)

                        def _add_step():
                            _sync_step_data_from_editors()
                            _reassign_step_ids()
                            new_id = f"prompt_{len(_steps_data) + 1}"
                            _steps_data.append({
                                "id": new_id,
                                "type": "prompt",
                                "prompt": "",
                            })
                            _reassign_step_ids()
                            _build_advanced_mode()

                        with ui.row().classes("w-full gap-2"):
                            ui.button("＋ Add step", on_click=_add_step).props(
                                "flat dense no-caps"
                            ).style("color: #f0c040;")

                        # ── Flow preview (Mermaid) ──
                        _flow_container = ui.column().classes("w-full")

                        def _render_flow_preview():
                            """Render a Mermaid flowchart of current steps."""
                            _sync_step_data_from_editors()
                            _flow_container.clear()
                            if not _steps_data:
                                return
                            from tasks import generate_pipeline_mermaid
                            mermaid_src = generate_pipeline_mermaid(_steps_data)
                            with _flow_container:
                                ui.html(
                                    f'<div class="mermaid-rendered">'
                                    f'<pre class="mermaid">{mermaid_src}</pre></div>',
                                    sanitize=False,
                                )
                            ui.run_javascript(
                                "if (typeof mermaid !== 'undefined') {"
                                "  mermaid.run({nodes: document.querySelectorAll('pre.mermaid')});"
                                "}"
                            )

                        with advanced_container:
                            with ui.expansion("🗺️ Flow preview").classes("w-full"):
                                ui.button(
                                    "🔄 Refresh", on_click=_render_flow_preview,
                                ).props("flat dense no-caps").style(
                                    "font-size: 0.8rem; color: #888;"
                                )
                                _flow_container

                def _build_var_textarea(
                    label: str, value: str, idx: int,
                    classes: str = "w-full", props: str = 'rows="3"',
                ):
                    """Build a textarea with {⋯} variable-insert button.

                    Returns the textarea element.  A clickable button opens
                    a grouped menu of available template variables.
                    Selecting an item appends the ``{{var}}`` token to the
                    textarea value.
                    """
                    # ── Collect available variables ──────────────────
                    pipeline_vars: list[tuple[str, str]] = []
                    if idx > 0:
                        pipeline_vars.append(
                            ("prev_output", "Previous step's output")
                        )
                    for j in range(idx):
                        s = _steps_data[j]
                        sid = s.get("id", f"step_{j+1}")
                        st = s.get("type", "prompt")
                        if st == "notify":
                            continue
                        sicon = _STEP_TYPE_ICONS.get(st, "❓")
                        if st == "prompt":
                            prev = (s.get("prompt") or "")[:20]
                        elif st == "condition":
                            prev = (s.get("condition") or "")[:20]
                        elif st == "approval":
                            prev = "Approval gate"
                        elif st == "subtask":
                            prev = "Sub-workflow"
                        else:
                            prev = ""
                        desc = f"{sicon} #{j+1}"
                        if prev:
                            desc += f" — {prev}"
                        pipeline_vars.append(
                            (f"step.{sid}.output", desc)
                        )

                    datetime_vars = [
                        ("date", "e.g. April 09, 2026"),
                        ("day", "e.g. Wednesday"),
                        ("time", "e.g. 09:40 AM"),
                        ("month", "e.g. April"),
                        ("year", "e.g. 2026"),
                    ]
                    context_vars = [
                        ("task_id", "This workflow's ID"),
                        ("parent_output", "Parent workflow output"),
                    ]

                    async def _do_insert(var_key):
                        token = "{{" + var_key + "}}"
                        cur = ta.value or ""
                        # Read last-known cursor position saved by
                        # client-side JS (persists after blur)
                        try:
                            pos = await ui.run_javascript(
                                f'window._thothCur{ta.id} ?? -1',
                                timeout=0.5,
                            )
                        except Exception:
                            pos = -1
                        if isinstance(pos, int) and 0 <= pos <= len(cur):
                            before = cur[:pos]
                            after = cur[pos:]
                            if before and not before.endswith(
                                (" ", "\n", "{")
                            ):
                                token = " " + token
                            ta.value = before + token + after
                        else:
                            # Fallback: append
                            if cur and not cur.endswith((" ", "\n", "{")):
                                token = " " + token
                            ta.value = cur + token

                    # ── Build UI ────────────────────────────────────
                    ta = ui.textarea(
                        label=label, value=value,
                    ).classes(classes).props(props)

                    with ui.row().classes(
                        "w-full items-center gap-1 q-mt-xs"
                    ).style("flex-wrap: wrap; min-height: 24px;"):
                        btn = ui.button(
                            icon="data_object",
                        ).props(
                            "flat dense round size=xs color=grey-6"
                        ).tooltip("Insert variable")
                        # Capture cursor position on mousedown (fires
                        # BEFORE blur leaves the textarea, so
                        # document.activeElement is still the <textarea>
                        # and selectionStart is accurate).
                        btn.on('mousedown', js_handler=(
                            f'() => {{'
                            f'var ae = document.activeElement;'
                            f'if (ae && ae.tagName === "TEXTAREA") {{'
                            f'window._thothCur{ta.id} = ae.selectionStart;'
                            f'}}'
                            f'}}'
                        ))

                        with ui.menu().props("auto-close") as var_menu:
                            # Pipeline section
                            ui.item_label("Pipeline").props(
                                "header"
                            ).style(
                                "font-size: 0.7rem; color: #999; "
                                "padding: 4px 12px; min-height: 0;"
                            )
                            if not pipeline_vars:
                                with ui.item().props("disable"):
                                    with ui.item_section():
                                        ui.item_label(
                                            "No preceding steps"
                                        ).style(
                                            "font-size: 0.8rem; "
                                            "color: #555; "
                                            "font-style: italic;"
                                        )
                            for vk, vd in pipeline_vars:
                                with ui.item(
                                    on_click=lambda _vk=vk: _do_insert(_vk)
                                ):
                                    with ui.item_section():
                                        ui.item_label(
                                            "{{" + vk + "}}"
                                        ).style(
                                            "font-size: 0.8rem; "
                                            "font-family: monospace;"
                                        )
                                        ui.item_label(vd).props(
                                            "caption"
                                        ).style("font-size: 0.7rem;")
                            ui.separator()

                            # Date & Time section
                            ui.item_label("Date & Time").props(
                                "header"
                            ).style(
                                "font-size: 0.7rem; color: #999; "
                                "padding: 4px 12px; min-height: 0;"
                            )
                            for vk, vd in datetime_vars:
                                with ui.item(
                                    on_click=lambda _vk=vk: _do_insert(_vk)
                                ):
                                    with ui.item_section():
                                        ui.item_label(
                                            "{{" + vk + "}}"
                                        ).style(
                                            "font-size: 0.8rem; "
                                            "font-family: monospace;"
                                        )
                                        ui.item_label(vd).props(
                                            "caption"
                                        ).style("font-size: 0.7rem;")
                            ui.separator()

                            # Context section
                            ui.item_label("Context").props(
                                "header"
                            ).style(
                                "font-size: 0.7rem; color: #999; "
                                "padding: 4px 12px; min-height: 0;"
                            )
                            for vk, vd in context_vars:
                                with ui.item(
                                    on_click=lambda _vk=vk: _do_insert(_vk)
                                ):
                                    with ui.item_section():
                                        ui.item_label(
                                            "{{" + vk + "}}"
                                        ).style(
                                            "font-size: 0.8rem; "
                                            "font-family: monospace;"
                                        )
                                        ui.item_label(vd).props(
                                            "caption"
                                        ).style("font-size: 0.7rem;")
                        btn.on_click(var_menu.open)

                        # Show compact hint for discoverability
                        ui.label(
                            "Click {⋯} to insert variables"
                        ).style(
                            "font-size: 0.65rem; color: #555; "
                            "font-style: italic; margin-left: 4px;"
                        )

                    return ta

                def _build_step_card(idx: int, step: dict,
                                     parent_container) -> None:
                    """Build a single step card in the step builder."""
                    stype = step.get("type", "prompt")
                    step_icon = _STEP_TYPE_ICONS.get(stype, "❓")
                    step_id = step.get("id", f"step_{idx + 1}")

                    with ui.card().classes("w-full q-pa-sm q-mb-xs").style(
                        "background: #0f0f1e; border: 1px solid #2a2a4a; "
                        "border-radius: 8px;"
                    ):
                        editors: dict = {"step_data": step}

                        # Header: type badge + ID + move/delete buttons
                        with ui.row().classes("w-full items-center gap-2"):
                            ui.label(f"{step_icon}").style("font-size: 1.1rem;")
                            editors["type_sel"] = ui.select(
                                options=_STEP_TYPES, value=stype,
                                on_change=lambda e, i=idx: _change_step_type(i, e.value),
                            ).classes("w-32").props("dense")
                            ui.label(f"#{idx + 1}").style(
                                "font-size: 0.75rem; color: #ffa500; "
                                "font-weight: bold; min-width: 24px; "
                                "text-align: center;"
                            )
                            ui.label(step_id).style(
                                "font-size: 0.7rem; color: #888; "
                                "background: #1a1a3a; padding: 2px 8px; "
                                "border-radius: 4px; font-family: monospace;"
                            ).tooltip("Auto-generated step ID")

                            ui.element("div").classes("flex-grow")

                            if idx > 0:
                                ui.button(
                                    icon="arrow_upward",
                                    on_click=lambda i=idx: _move_step(i, -1),
                                ).props("flat dense round size=sm")
                            if idx < len(_steps_data) - 1:
                                ui.button(
                                    icon="arrow_downward",
                                    on_click=lambda i=idx: _move_step(i, 1),
                                ).props("flat dense round size=sm")
                            ui.button(
                                icon="close",
                                on_click=lambda i=idx: _remove_step(i),
                            ).props("flat dense round size=sm").style("color: #ff6b6b;")

                        # Helper: short preview label for a step
                        def _step_preview(s, j):
                            sid = s.get("id", f"step_{j+1}")
                            sicon = _STEP_TYPE_ICONS.get(s.get("type", "prompt"), "❓")
                            st = s.get("type", "prompt")
                            if st == "prompt":
                                txt = (s.get("prompt") or "")[:30]
                                return f"{sicon} #{j+1} — {txt}" if txt else f"{sicon} #{j+1}"
                            elif st == "condition":
                                txt = (s.get("condition") or "")[:25]
                                return f"{sicon} #{j+1} — {txt}" if txt else f"{sicon} #{j+1}"
                            elif st == "subtask":
                                return f"{sicon} #{j+1} Sub-agent"
                            elif st == "approval":
                                return f"{sicon} #{j+1} Approval"
                            elif st == "notify":
                                ch = s.get("channel", "")
                                return f"{sicon} #{j+1} Notify ({ch})" if ch else f"{sicon} #{j+1} Notify"
                            return f"{sicon} #{j+1}"

                        # Type-specific fields
                        if stype == "prompt":
                            editors["prompt"] = _build_var_textarea(
                                "Prompt *", step.get("prompt", ""),
                                idx, props='rows="3"',
                            )
                            with ui.expansion("⚙️ Step settings").classes("w-full"):
                                with ui.row().classes("gap-2"):
                                    editors["on_error"] = ui.select(
                                        label="On error",
                                        options=["stop", "skip"],
                                        value=step.get("on_error", "skip"),
                                    ).classes("w-28").props("dense")
                                    editors["max_retries"] = ui.number(
                                        label="Max retries",
                                        value=step.get("max_retries", 2),
                                        min=1, max=10,
                                    ).classes("w-24").props("dense")
                                    editors["retry_delay"] = ui.number(
                                        label="Retry delay (s)",
                                        value=step.get("retry_delay_seconds", 5),
                                        min=0, max=300,
                                    ).classes("w-28").props("dense")
                                _next_opts = {
                                    "__next__": "➡️ Next step (continue)",
                                }
                                for j, s in enumerate(_steps_data):
                                    if j == idx:
                                        continue
                                    sid = s.get("id", f"step_{j+1}")
                                    _next_opts[sid] = _step_preview(s, j)
                                _next_opts["end"] = "🛑 End workflow"
                                _next_val = step.get("next") or "__next__"
                                editors["next"] = ui.select(
                                    label="Then go to →",
                                    options=_next_opts,
                                    value=_next_val if _next_val in _next_opts else "__next__",
                                ).classes("w-56").props("dense")

                        elif stype == "condition":
                            _cond_expr = step.get("condition", "")
                            _parsed_op, _parsed_val = _parse_condition_expr(_cond_expr)
                            _is_advanced_cond = (
                                _parsed_op is None and bool(_cond_expr)
                            )

                            with ui.column().classes("w-full gap-1"):
                                cond_adv_switch = ui.switch(
                                    "Advanced", value=_is_advanced_cond,
                                ).props("dense").style("font-size: 0.8rem;")

                                # ── Visual builder container ──
                                _builder_col = ui.column().classes("w-full gap-1")

                                # Parse json: extra fields from stored value
                                _json_path_init = ""
                                _json_sub_op_init = "equals:"
                                _json_sub_val_init = ""
                                if _parsed_op == "json:" and _parsed_val:
                                    _jp = _parsed_val.split(":", 2)
                                    _json_path_init = _jp[0] if len(_jp) >= 1 else ""
                                    if len(_jp) >= 2:
                                        # sub-expression is everything after path:
                                        _sub_expr = ":".join(_jp[1:])
                                        _found_sub = False
                                        for _sop in ("not_contains:", "contains:",
                                                     "not_empty", "empty",
                                                     "equals:", "matches:",
                                                     "gte:", "lte:", "gt:", "lt:"):
                                            if _sub_expr == _sop:
                                                _json_sub_op_init = _sop
                                                _found_sub = True
                                                break
                                            if _sub_expr.startswith(_sop) and _sop.endswith(":"):
                                                _json_sub_op_init = _sop
                                                _json_sub_val_init = _sub_expr[len(_sop):]
                                                _found_sub = True
                                                break
                                        if not _found_sub:
                                            _json_sub_op_init = "equals:"

                                with _builder_col:
                                    # ── Standard ops row ──
                                    _std_row = ui.row().classes("w-full gap-2")
                                    with _std_row:
                                        _cond_op_sel = ui.select(
                                            label="Operator",
                                            options=_SIMPLE_CONDITION_OPS,
                                            value=_parsed_op or "contains:",
                                        ).classes("w-48").props("dense")
                                        _cond_val_input = ui.input(
                                            "Value", value=_parsed_val if _parsed_op in _STANDARD_VALUE_OPS else "",
                                        ).classes("flex-grow").props("dense")

                                    # ── JSON fields row ──
                                    _json_row = ui.row().classes("w-full gap-2")
                                    with _json_row:
                                        _json_path_input = ui.input(
                                            "JSON path *", value=_json_path_init,
                                        ).classes("w-40").props("dense").tooltip(
                                            "Dot-separated path, e.g. status, data.results.0.name"
                                        )
                                        _json_sub_op_sel = ui.select(
                                            label="Operator",
                                            options=_JSON_SUB_OPS,
                                            value=_json_sub_op_init,
                                        ).classes("w-40").props("dense")
                                        _json_sub_val_input = ui.input(
                                            "Value", value=_json_sub_val_init,
                                        ).classes("flex-grow").props("dense")

                                    # ── LLM question row ──
                                    _llm_row = ui.column().classes("w-full")
                                    with _llm_row:
                                        _llm_question_input = ui.textarea(
                                            "Question for LLM *",
                                            value=_parsed_val if _parsed_op == "llm:" else "",
                                        ).classes("w-full").props('rows="2" dense')
                                        ui.label(
                                            "All step outputs are provided as context automatically."
                                        ).style("font-size: 0.7rem; color: #888;")

                                    # ── Compound hint ──
                                    _compound_hint = ui.label(
                                        "For compound conditions (AND/OR chaining), use Advanced mode."
                                    ).style("font-size: 0.7rem; color: #f0ad4e;")

                                # ── Raw expression input ──
                                editors["condition"] = ui.input(
                                    "Condition expression",
                                    value=_cond_expr,
                                ).classes("w-full").props("dense").tooltip(
                                    "e.g. contains:urgent, gt:50, empty, "
                                    "json:status:equals:success, llm:Is the sentiment positive?"
                                )

                                # ── Sync helpers ──
                                def _sync_cond_builder(
                                    _op=_cond_op_sel,
                                    _val=_cond_val_input,
                                    _raw=editors["condition"],
                                    _jp=_json_path_input,
                                    _jso=_json_sub_op_sel,
                                    _jsv=_json_sub_val_input,
                                    _lq=_llm_question_input,
                                ):
                                    op = _op.value
                                    if op == "json:":
                                        path = _jp.value or ""
                                        sub_op = _jso.value or "equals:"
                                        sub_val = _jsv.value or ""
                                        if sub_op in _JSON_NO_VALUE_SUB_OPS:
                                            _raw.set_value(f"json:{path}:{sub_op}")
                                        else:
                                            _raw.set_value(f"json:{path}:{sub_op}{sub_val}")
                                    elif op == "llm:":
                                        _raw.set_value(f"llm:{_lq.value or ''}")
                                    elif op in _NO_VALUE_OPS:
                                        _raw.set_value(op)
                                    else:
                                        _raw.set_value(f"{op}{_val.value or ''}")

                                def _update_field_visibility(op_value):
                                    """Show/hide fields based on selected operator."""
                                    is_std = op_value in _STANDARD_VALUE_OPS
                                    is_no_val = op_value in _NO_VALUE_OPS
                                    is_json = op_value == "json:"
                                    is_llm = op_value == "llm:"
                                    _cond_val_input.set_visibility(is_std)
                                    _json_row.set_visibility(is_json)
                                    _llm_row.set_visibility(is_llm)
                                    _compound_hint.set_visibility(is_json or is_llm)
                                    # Hide json sub-value for no-value sub-ops
                                    if is_json:
                                        _json_sub_val_input.set_visibility(
                                            _json_sub_op_sel.value not in _JSON_NO_VALUE_SUB_OPS
                                        )

                                _cond_op_sel.on_value_change(
                                    lambda e: (
                                        _update_field_visibility(e.value),
                                        _sync_cond_builder(),
                                    )
                                )
                                _cond_val_input.on_value_change(
                                    lambda e: _sync_cond_builder()
                                )
                                _json_path_input.on_value_change(
                                    lambda e: _sync_cond_builder()
                                )
                                _json_sub_op_sel.on_value_change(
                                    lambda e: (
                                        _json_sub_val_input.set_visibility(
                                            e.value not in _JSON_NO_VALUE_SUB_OPS
                                        ),
                                        _sync_cond_builder(),
                                    )
                                )
                                _json_sub_val_input.on_value_change(
                                    lambda e: _sync_cond_builder()
                                )
                                _llm_question_input.on_value_change(
                                    lambda e: _sync_cond_builder()
                                )

                                # Toggle advanced/visual
                                def _toggle_cond_adv(
                                    e, _bc=_builder_col, _ri=editors["condition"],
                                ):
                                    _bc.set_visibility(not e.value)
                                    _ri.set_visibility(e.value)

                                cond_adv_switch.on_value_change(_toggle_cond_adv)
                                _builder_col.set_visibility(not _is_advanced_cond)
                                editors["condition"].set_visibility(
                                    _is_advanced_cond
                                )
                                # Set initial field visibility
                                _init_op = _parsed_op or "contains:"
                                _cond_val_input.set_visibility(_init_op in _STANDARD_VALUE_OPS)
                                _json_row.set_visibility(_init_op == "json:")
                                _llm_row.set_visibility(_init_op == "llm:")
                                _compound_hint.set_visibility(_init_op in ("json:", "llm:"))
                                if _init_op == "json:":
                                    _json_sub_val_input.set_visibility(
                                        _json_sub_op_init not in _JSON_NO_VALUE_SUB_OPS
                                    )

                            with ui.row().classes("gap-2"):
                                # Build rich step options for if_true/if_false

                                _jump_opts = {
                                    "__next__": "➡️ Next step (continue)",
                                }
                                for j, s in enumerate(_steps_data):
                                    if j == idx:
                                        continue  # Can't jump to self
                                    sid = s.get("id", f"step_{j+1}")
                                    _jump_opts[sid] = _step_preview(s, j)
                                _jump_opts["end"] = "🛑 End workflow"

                                _if_true_val = step.get("if_true") or "__next__"
                                _if_false_val = step.get("if_false") or "__next__"

                                editors["if_true"] = ui.select(
                                    label="If true →",
                                    options=_jump_opts,
                                    value=_if_true_val if _if_true_val in _jump_opts else "__next__",
                                ).classes("w-56").props("dense")
                                editors["if_false"] = ui.select(
                                    label="If false →",
                                    options=_jump_opts,
                                    value=_if_false_val if _if_false_val in _jump_opts else "__next__",
                                ).classes("w-56").props("dense")

                        elif stype == "approval":
                            editors["message"] = _build_var_textarea(
                                "Approval message *",
                                step.get("message", ""),
                                idx, props='rows="2" dense',
                            )
                            editors["timeout_min"] = ui.number(
                                label="Timeout (minutes, 0=forever)",
                                value=step.get("timeout_minutes", 30),
                                min=0, max=1440,
                            ).classes("w-48").props("dense")

                            # Approval branching
                            _appr_jump_opts = {
                                "__next__": "➡️ Next step (continue)",
                            }
                            for j, s in enumerate(_steps_data):
                                if j == idx:
                                    continue
                                sid = s.get("id", f"step_{j+1}")
                                _appr_jump_opts[sid] = _step_preview(s, j)
                            _appr_jump_opts["end"] = "🛑 End workflow"

                            _appr_val = step.get("if_approved") or "__next__"
                            _deny_val = step.get("if_denied") or "end"

                            with ui.row().classes("gap-2"):
                                editors["if_approved"] = ui.select(
                                    label="If approved →",
                                    options=_appr_jump_opts,
                                    value=_appr_val if _appr_val in _appr_jump_opts else "__next__",
                                ).classes("w-56").props("dense")
                                editors["if_denied"] = ui.select(
                                    label="If denied →",
                                    options=_appr_jump_opts,
                                    value=_deny_val if _deny_val in _appr_jump_opts else "end",
                                ).classes("w-56").props("dense")

                        elif stype == "subtask":
                            ui.label(
                                "Run another workflow as a sub-agent. "
                                "Its output becomes this step's output."
                            ).style("font-size: 0.75rem; color: #666;")
                            all_tasks = list_tasks()
                            _task_opts = {
                                t["id"]: f"{t['icon']} {t['name']}"
                                for t in all_tasks
                                if t.get("id") != (task["id"] if task else None)
                            }
                            editors["task_id"] = ui.select(
                                label="Workflow to run *",
                                options=_task_opts,
                                value=step.get("task_id") or None,
                            ).classes("w-full").props("dense clearable")
                            editors["pass_output"] = ui.switch(
                                "Pass output to sub-agent",
                                value=step.get("pass_output", True),
                            )
                            editors["on_error"] = ui.select(
                                label="On failure",
                                options=["stop", "skip"],
                                value=step.get("on_error", "stop"),
                            ).classes("w-28").props("dense")
                            _sub_next_opts = {
                                "__next__": "➡️ Next step (continue)",
                            }
                            for j, s in enumerate(_steps_data):
                                if j == idx:
                                    continue
                                sid = s.get("id", f"step_{j+1}")
                                _sub_next_opts[sid] = _step_preview(s, j)
                            _sub_next_opts["end"] = "🛑 End workflow"
                            _sub_next_val = step.get("next") or "__next__"
                            editors["next"] = ui.select(
                                label="Then go to →",
                                options=_sub_next_opts,
                                value=_sub_next_val if _sub_next_val in _sub_next_opts else "__next__",
                            ).classes("w-56").props("dense")

                        elif stype == "notify":
                            editors["message"] = _build_var_textarea(
                                "Notification message *",
                                step.get("message", ""),
                                idx, props='rows="2" dense',
                            )
                            from channels import registry as _notify_ch_reg
                            _notify_ch_names = ["desktop"] + [
                                ch.name
                                for ch in _notify_ch_reg.configured_channels()
                            ]
                            _ch_raw = step.get("channel", "desktop")
                            _ch_val = _ch_raw if _ch_raw in _notify_ch_names else "desktop"
                            editors["channel"] = ui.select(
                                label="Channel",
                                options=_notify_ch_names,
                                value=_ch_val,
                            ).classes("w-36").props("dense")
                            if _ch_raw != _ch_val:
                                ui.label(
                                    f'⚠️ Unknown channel "{_ch_raw}" — defaulted to desktop'
                                ).style(
                                    "font-size: 0.7rem; color: #ff9800; "
                                    "margin-top: -4px;"
                                )
                            _nfy_next_opts = {
                                "__next__": "➡️ Next step (continue)",
                            }
                            for j, s in enumerate(_steps_data):
                                if j == idx:
                                    continue
                                sid = s.get("id", f"step_{j+1}")
                                _nfy_next_opts[sid] = _step_preview(s, j)
                            _nfy_next_opts["end"] = "🛑 End workflow"
                            _nfy_next_val = step.get("next") or "__next__"
                            editors["next"] = ui.select(
                                label="Then go to →",
                                options=_nfy_next_opts,
                                value=_nfy_next_val if _nfy_next_val in _nfy_next_opts else "__next__",
                            ).classes("w-56").props("dense")

                        _step_editors.append(editors)

                def _reassign_step_ids():
                    """Re-assign step IDs based on type + position and update
                    any if_true/if_false references to match."""
                    from tasks import assign_step_ids
                    assign_step_ids(_steps_data)

                def _change_step_type(idx: int, new_type: str):
                    _sync_step_data_from_editors()
                    _steps_data[idx]["type"] = new_type
                    _reassign_step_ids()
                    _build_advanced_mode()

                def _move_step(idx: int, direction: int):
                    _sync_step_data_from_editors()
                    new_idx = idx + direction
                    if 0 <= new_idx < len(_steps_data):
                        _steps_data[idx], _steps_data[new_idx] = (
                            _steps_data[new_idx], _steps_data[idx]
                        )
                    _reassign_step_ids()
                    _build_advanced_mode()

                def _remove_step(idx: int):
                    _sync_step_data_from_editors()
                    _steps_data.pop(idx)
                    _reassign_step_ids()
                    _build_advanced_mode()

                def _sync_step_data_from_editors():
                    """Sync UI editor values back into _steps_data."""
                    for i, ed in enumerate(_step_editors):
                        if i >= len(_steps_data):
                            break
                        s = _steps_data[i]
                        stype = s.get("type", "prompt")
                        if stype == "prompt" and "prompt" in ed:
                            s["prompt"] = ed["prompt"].value
                            if "on_error" in ed:
                                s["on_error"] = ed["on_error"].value
                            if "max_retries" in ed:
                                s["max_retries"] = int(ed["max_retries"].value or 1)
                            if "retry_delay" in ed:
                                s["retry_delay_seconds"] = int(ed["retry_delay"].value or 5)
                            if "next" in ed:
                                v = ed["next"].value
                                s["next"] = "" if v == "__next__" else (v or "")
                        elif stype == "condition":
                            if "condition" in ed:
                                s["condition"] = ed["condition"].value
                            if "if_true" in ed:
                                v = ed["if_true"].value
                                s["if_true"] = "" if v == "__next__" else (v or "")
                            if "if_false" in ed:
                                v = ed["if_false"].value
                                s["if_false"] = "" if v == "__next__" else (v or "")
                        elif stype == "approval":
                            if "message" in ed:
                                s["message"] = ed["message"].value
                            if "timeout_min" in ed:
                                s["timeout_minutes"] = int(ed["timeout_min"].value or 30)
                            if "if_approved" in ed:
                                v = ed["if_approved"].value
                                s["if_approved"] = "" if v == "__next__" else (v or "")
                            if "if_denied" in ed:
                                v = ed["if_denied"].value
                                s["if_denied"] = "" if v == "__next__" else (v or "")
                        elif stype == "subtask":
                            if "task_id" in ed:
                                s["task_id"] = ed["task_id"].value or ""
                            if "pass_output" in ed:
                                s["pass_output"] = ed["pass_output"].value
                            if "on_error" in ed:
                                s["on_error"] = ed["on_error"].value
                            if "next" in ed:
                                v = ed["next"].value
                                s["next"] = "" if v == "__next__" else (v or "")
                        elif stype == "notify":
                            if "message" in ed:
                                s["message"] = ed["message"].value
                            if "channel" in ed:
                                s["channel"] = ed["channel"].value
                            if "next" in ed:
                                v = ed["next"].value
                                s["next"] = "" if v == "__next__" else (v or "")

                # Toggle handler
                def _on_mode_toggle(e):
                    if e.value:
                        # Switching to advanced — sync prompts first
                        for j, _ta in enumerate(prompt_inputs):
                            if j < len(_prompts_data):
                                _prompts_data[j] = _ta.value
                        _build_advanced_mode()
                        simple_container.set_visibility(False)
                        advanced_container.set_visibility(True)
                        advanced_extras_container.set_visibility(True)
                    else:
                        # Switching to simple — sync steps to prompts
                        _sync_step_data_from_editors()
                        from tasks import _steps_to_prompts
                        converted = _steps_to_prompts(_steps_data)
                        if converted:
                            _prompts_data.clear()
                            _prompts_data.extend(converted)
                        _build_simple_mode()
                        simple_container.set_visibility(True)
                        advanced_container.set_visibility(False)
                        advanced_extras_container.set_visibility(False)

                advanced_switch.on_value_change(_on_mode_toggle)

                # Initial render
                if _has_advanced:
                    _build_advanced_mode()
                    simple_container.set_visibility(False)
                    advanced_container.set_visibility(True)
                else:
                    _build_simple_mode()
                    simple_container.set_visibility(True)
                    advanced_container.set_visibility(False)

                ui.separator()

                # Schedule section
                ui.label("Schedule").style(
                    "font-weight: 600; font-size: 0.9rem; color: #d0d0e0;"
                )

                sched_options = ["Manual", "Daily", "Weekly", "Interval (hrs)", "Interval (min)", "Cron"]
                day_options = {
                    "mon": "Monday", "tue": "Tuesday", "wed": "Wednesday",
                    "thu": "Thursday", "fri": "Friday", "sat": "Saturday",
                    "sun": "Sunday",
                }

                with ui.column().classes("w-full gap-2"):
                    sched_sel = ui.select(
                        label="Type", options=sched_options, value=_sched_mode,
                    ).classes("w-48")

                    sched_time_input = ui.input(
                        label="Time", value=_sched_time,
                    ).classes("w-28").props('mask="##:##" placeholder="HH:MM"')
                    sched_time_input.visible = _sched_mode in ("Daily", "Weekly")

                    sched_day_sel = ui.select(
                        label="Day", options=day_options, value=_sched_day,
                    ).classes("w-36")
                    sched_day_sel.visible = _sched_mode == "Weekly"

                    sched_interval_input = ui.input(
                        label="Every", value=_sched_interval,
                    ).classes("w-28")
                    sched_interval_input.visible = _sched_mode in ("Interval (hrs)", "Interval (min)")

                    sched_cron_input = ui.input(
                        label="Cron expression", value=_sched_cron,
                    ).classes("w-full")
                    sched_cron_input.visible = _sched_mode == "Cron"

                def _on_sched_change(e):
                    sched_time_input.visible = e.value in ("Daily", "Weekly")
                    sched_day_sel.visible = e.value == "Weekly"
                    sched_interval_input.visible = e.value in ("Interval (hrs)", "Interval (min)")
                    sched_cron_input.visible = e.value == "Cron"

                sched_sel.on_value_change(_on_sched_change)

                ui.separator()

                # Pipeline settings vars — declared here, UI rendered
                # inside advanced_extras_container below.
                _trig_type = (_trigger_data or {}).get("type", "")
                _trig_target = (_trigger_data or {}).get("target_task", "")
                _trig_secret = (_trigger_data or {}).get("secret", "")

                # Channels (collapsed) - unified for delivery + approvals
                _ch_mode_sel = None
                _ch_select = None
                with ui.expansion("Channels (optional)").classes("w-full"):
                    ui.label(
                        "Choose external channels for task output and approval "
                        "requests. Web app always receives run status."
                    ).style("font-size: 0.75rem; color: #666;")

                    from channels import registry as _ch_registry
                    _available_channels = _ch_registry.configured_channels()
                    _channel_options = {
                        _ch.name: _ch.display_name
                        for _ch in _available_channels
                    }
                    _selected_channels = [
                        name for name in (_task_channels or [])
                        if name in _channel_options
                    ]
                    if _task_channels is None:
                        _channel_mode = "Use workflow default"
                    elif _task_channels:
                        _channel_mode = "Custom channels"
                    else:
                        _channel_mode = "Web app only"

                    _ch_mode_sel = ui.select(
                        label="Delivery mode",
                        options=[
                            "Use workflow default",
                            "Custom channels",
                            "Web app only",
                        ],
                        value=_channel_mode,
                    ).classes("w-full").props("dense outlined")
                    _ch_select = ui.select(
                        label="External channels",
                        options=_channel_options,
                        value=_selected_channels,
                        multiple=True,
                    ).classes("w-full").props("dense outlined use-chips clearable")
                    if not _channel_options:
                        _ch_select.props("disable")
                    _ch_select.visible = _channel_mode == "Custom channels"

                    def _on_channel_mode_change(e):
                        _ch_select.visible = e.value == "Custom channels"

                    _ch_mode_sel.on_value_change(_on_channel_mode_change)

                    if not _available_channels:
                        ui.label(
                            "No external channels configured. Workflows still "
                            "update the web app."
                        ).classes("text-xs text-grey-6 q-ml-md")

                    # Legacy compat: old single-channel workflows are migrated
                    # to the unified channels field on save.
                    del_ch_sel = type("_Compat", (), {"value": None})()
                # ── Advanced-only extras: Tools & Skills overrides ──────
                from tools import registry as _tool_registry
                _all_tools = _tool_registry.get_enabled_tools()
                _task_tools_override = _tools_override_data
                _all_tool_names = [t.name for t in _all_tools]
                # If override is set, use it; otherwise all checked
                _task_tools_active = (
                    set(_task_tools_override)
                    if _task_tools_override is not None
                    else set(_all_tool_names)
                )
                _tool_checkboxes: dict = {}
                _always_on_tools = {"conversation_search", "memory"}

                advanced_extras_container = ui.column().classes("w-full")
                advanced_extras_container.set_visibility(_has_advanced)

                with advanced_extras_container:
                    # ── Pipeline settings (concurrency group + trigger) ──
                    with ui.expansion("⚙️ Pipeline settings (optional)").classes("w-full"):
                        ui.label(
                            "Configure concurrency groups, completion triggers, "
                            "and webhook endpoints."
                        ).style("font-size: 0.75rem; color: #666;")

                        conc_group_input = ui.input(
                            "Concurrency group",
                            value=_concurrency_group,
                        ).classes("w-full").props("dense").tooltip(
                            "Tasks in the same concurrency group will not run "
                            "simultaneously. Leave empty for no grouping. "
                            "'local_gpu' is auto-assigned for local models."
                        )

                        ui.label("Trigger").style(
                            "font-size: 0.8rem; color: #c0c0d0; margin-top: 8px;"
                        )
                        _trig_type_options = {
                            "": "None",
                            "task_complete": "When another task completes",
                            "webhook": "Webhook (HTTP POST)",
                        }
                        trigger_type_sel = ui.select(
                            label="Trigger type",
                            options=_trig_type_options,
                            value=_trig_type,
                        ).classes("w-64").props("dense")

                        # -- task_complete: source task selector --
                        all_tasks_for_trigger = list_tasks()
                        _trig_task_opts = {
                            t["id"]: f"{t['icon']} {t['name']}"
                            for t in all_tasks_for_trigger
                            if t.get("id") != (task["id"] if task else None)
                        }
                        _trig_target_val = (
                            _trig_target if _trig_target in _trig_task_opts
                            else None
                        )
                        trigger_target_sel = ui.select(
                            label="Run when this task completes *",
                            options=_trig_task_opts,
                            value=_trig_target_val,
                        ).classes("w-full").props("dense")
                        trigger_target_sel.set_visibility(
                            _trig_type == "task_complete"
                        )

                        # -- webhook: secret + URL display --
                        from tasks import generate_webhook_secret
                        _wh_container = ui.column().classes(
                            "w-full gap-1"
                        )
                        _wh_container.set_visibility(_trig_type == "webhook")
                        with _wh_container:
                            if not _trig_secret and _trig_type == "webhook":
                                _trig_secret = generate_webhook_secret()
                            webhook_secret_input = ui.input(
                                "Secret",
                                value=_trig_secret,
                            ).classes("w-full").props(
                                "dense readonly"
                            ).tooltip(
                                "Auto-generated secret. Include as ?secret= "
                                "query param when calling the webhook."
                            )
                            if task and task.get("id"):
                                _wh_url = (
                                    f"/api/webhook/{task['id']}"
                                    f"?secret={_trig_secret}"
                                )
                                ui.label(f"POST {_wh_url}").style(
                                    "font-size: 0.7rem; color: #888; "
                                    "font-family: monospace; word-break: break-all;"
                                )
                            else:
                                ui.label(
                                    "Webhook URL will be shown after saving."
                                ).style(
                                    "font-size: 0.7rem; color: #888;"
                                )

                        def _on_trigger_type_change(e):
                            trigger_target_sel.set_visibility(
                                e.value == "task_complete"
                            )
                            _wh_container.set_visibility(
                                e.value == "webhook"
                            )
                            # Auto-generate secret when switching to webhook
                            if (e.value == "webhook"
                                    and not webhook_secret_input.value):
                                webhook_secret_input.set_value(
                                    generate_webhook_secret()
                                )

                        trigger_type_sel.on_value_change(
                            _on_trigger_type_change
                        )

                    # ── Tools override ──
                    with ui.expansion("🔧 Tools override (optional)").classes("w-full"):
                        ui.label(
                            "Choose which tools are available when this task runs. "
                            "Use Auto-detect to suggest tools from your step prompts."
                        ).style("font-size: 0.75rem; color: #666;")

                        def _auto_detect_tools():
                            """Re-scan all step prompts and update tool checkboxes."""
                            _sync_step_data_from_editors()
                            all_prompts = []
                            for s in _steps_data:
                                stype = s.get("type", "prompt")
                                if stype == "prompt":
                                    all_prompts.append(s.get("prompt", ""))
                                elif stype == "condition":
                                    all_prompts.append(s.get("condition", ""))
                                elif stype in ("approval", "notify"):
                                    all_prompts.append(s.get("message", ""))
                            from tasks import infer_tools_for_prompt
                            suggested = set(infer_tools_for_prompt(
                                all_prompts, _all_tool_names,
                            ))
                            for tname, cb in _tool_checkboxes.items():
                                if tname in _always_on_tools:
                                    continue  # Always-on, don't touch
                                cb.set_value(tname in suggested)
                            _detect_count = len(suggested)
                            ui.notify(
                                f"🔍 Auto-detected {_detect_count} tool(s)",
                                type="info",
                            )

                        with ui.row().classes("w-full items-center gap-2"):
                            ui.button(
                                "🔍 Auto-detect from steps",
                                on_click=_auto_detect_tools,
                            ).props("flat dense no-caps").style(
                                "color: #f0c040; font-size: 0.8rem;"
                            ).tooltip(
                                "Analyze all step prompts and suggest "
                                "which tools are needed."
                            )

                        for _tool in _all_tools:
                            _is_always = _tool.name in _always_on_tools
                            _tcb = ui.checkbox(
                                f"{_tool.display_name}",
                                value=(_tool.name in _task_tools_active) or _is_always,
                            ).classes("text-sm")
                            if _is_always:
                                _tcb.props("disable")
                                _tcb.tooltip("Always included (core agent tool)")
                            _tool_checkboxes[_tool.name] = _tcb

                # Skills override (advanced only) ─────────────────────
                import skills as _task_skills_mod
                _task_skills_mod.load_skills()
                _task_all_skills = [s for s in _task_skills_mod.get_enabled_skills()
                                    if not _task_skills_mod.is_tool_guide(s)]
                _task_sk_override = _skills_override_data
                _task_enabled_names = set(sk.name for sk in _task_all_skills)
                _task_sk_active = (
                    set(_task_sk_override) & _task_enabled_names
                    if _task_sk_override is not None
                    else set(_task_enabled_names)
                )
                _task_sk_checkboxes: dict = {}
                if _task_all_skills:
                    with advanced_extras_container:
                        with ui.expansion("✨ Skills override (optional)").classes("w-full"):
                            ui.label(
                                "Choose which skills are active when this task runs. "
                                "Leave unchecked to use the global default."
                            ).style("font-size: 0.75rem; color: #666;")
                            for _tsk in _task_all_skills:
                                _tcb = ui.checkbox(
                                    f"{_tsk.icon} {_tsk.display_name}",
                                    value=_tsk.name in _task_sk_active,
                                ).classes("text-sm")
                                _task_sk_checkboxes[_tsk.name] = _tcb

                # Run history (edit mode only)
                if not is_new:
                    runs = get_run_history(task["id"], limit=5)
                    if runs:
                        with ui.expansion("📜 Recent runs").classes("w-full"):
                            for r in runs:
                                r_icon = "✅" if r["status"] == "completed" else (
                                    "🔄" if r["status"] == "running" else "❌"
                                )
                                started = datetime.fromisoformat(
                                    r["started_at"]
                                ).strftime("%b %d, %I:%M %p")
                                ui.label(
                                    f"{r_icon} {started} — "
                                    f"{r['steps_done']}/{r['steps_total']} steps"
                                ).classes("text-xs")

        def _current_schedule_value():
            sv = sched_sel.value
            if sv == "Daily":
                t = sched_time_input.value.strip() or "08:00"
                return f"daily:{t}"
            if sv == "Weekly":
                t = sched_time_input.value.strip() or "08:00"
                d = sched_day_sel.value or "mon"
                return f"weekly:{d}:{t}"
            if sv == "Interval (hrs)":
                v = sched_interval_input.value.strip() or "1"
                return f"interval:{v}"
            if sv == "Interval (min)":
                v = sched_interval_input.value.strip() or "30"
                return f"interval_minutes:{v}"
            if sv == "Cron":
                v = sched_cron_input.value.strip()
                return f"cron:{v}" if v else None
            return None

        def _current_channels_value():
            if _ch_mode_sel and _ch_mode_sel.value == "Web app only":
                return []
            if _ch_mode_sel and _ch_mode_sel.value == "Custom channels":
                return list(_ch_select.value or [])
            return None

        def _current_trigger_value():
            if not trigger_type_sel.value:
                return None
            cur_trigger = {"type": trigger_type_sel.value}
            if trigger_type_sel.value == "task_complete":
                cur_trigger["target_task"] = trigger_target_sel.value or ""
            elif trigger_type_sel.value == "webhook":
                cur_trigger["secret"] = webhook_secret_input.value or ""
            return cur_trigger

        def _current_tools_override_value(is_advanced: bool):
            if not (is_advanced and _tool_checkboxes):
                return None
            checked = [n for n, cb in _tool_checkboxes.items() if cb.value]
            if set(checked) == set(_all_tool_names):
                return None
            return checked if checked else []

        def _current_skills_override_value(is_advanced: bool):
            if not (is_advanced and _task_sk_checkboxes):
                return None
            checked = [n for n, cb in _task_sk_checkboxes.items() if cb.value]
            return checked if checked else []

        def _collect_draft_payload():
            is_advanced = bool(advanced_switch.value)
            if is_advanced:
                _sync_step_data_from_editors()
                from tasks import _steps_to_prompts
                clean_prompts = _steps_to_prompts(_steps_data) or []
                cur_steps = copy.deepcopy(_steps_data)
            else:
                for j, _ta in enumerate(prompt_inputs):
                    if j < len(_prompts_data):
                        _prompts_data[j] = _ta.value
                clean_prompts = [pp for pp in _prompts_data if str(pp).strip()]
                cur_steps = []

            cur_model_ov = model_sel.value if model_sel.value != "__default__" else None
            return {
                "advanced": is_advanced,
                "name": name_input.value.strip(),
                "icon": icon_sel.value or "⚡",
                "description": desc_input.value.strip(),
                "enabled": bool(enabled_switch.value),
                "model_override": cur_model_ov,
                "prompts": clean_prompts,
                "steps": cur_steps,
                "safety_mode": safety_sel.value if safety_sel.value else "block",
                "schedule": _current_schedule_value(),
                "delivery_channel": del_ch_sel.value or None,
                "delivery_target": None,
                "channels": _current_channels_value(),
                "concurrency_group": conc_group_input.value.strip() or None,
                "trigger": _current_trigger_value(),
                "tools_override": _current_tools_override_value(is_advanced),
                "skills_override": _current_skills_override_value(is_advanced),
                "persistent_enabled": bool(persistent_toggle.value),
            }

        def _stop_draft_timer():
            _draft_timer_ref["closing"] = True
            _timer = _draft_timer_ref.get("timer")
            if _timer:
                _timer.deactivate()

        def _close_dialog():
            _stop_draft_timer()
            p.task_dlg.close()

        def _cancel_dialog():
            _stop_draft_timer()
            delete_workflow_draft(_draft_key)
            p.task_dlg.close()

        def _autosave_draft():
            if _draft_timer_ref["closing"]:
                return
            try:
                payload = _collect_draft_payload()
                serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
                if not _draft_timer_ref["baseline"]:
                    _draft_timer_ref["baseline"] = serialized
                    _draft_timer_ref["last"] = serialized
                    return
                if serialized == _draft_timer_ref["baseline"]:
                    if _draft_timer_ref["last"] != serialized:
                        delete_workflow_draft(_draft_key)
                    _draft_timer_ref["last"] = serialized
                    return
                if serialized == _draft_timer_ref["last"]:
                    return
                save_workflow_draft(_draft_key, payload)
                _draft_timer_ref["last"] = serialized
            except Exception:
                logger.debug("Workflow draft autosave failed", exc_info=True)

        try:
            _initial_draft_payload = _collect_draft_payload()
            _initial_draft = json.dumps(
                _initial_draft_payload,
                ensure_ascii=False,
                sort_keys=True,
            )
            _draft_timer_ref["baseline"] = _initial_draft
            _draft_timer_ref["last"] = _initial_draft
        except Exception:
            logger.debug("Workflow draft baseline capture failed", exc_info=True)

        _draft_timer_ref["timer"] = safe_timer(2.0, _autosave_draft)

        # ── Footer ──
        with ui.row().classes("w-full items-center q-pa-md gap-2").style(
            "border-top: 1px solid #2a2a4a;"
        ):
            # Left cluster — duplicate + delete (edit only)
            if not is_new:
                def _dup_task():
                    _stop_draft_timer()
                    duplicate_task(task["id"])
                    p.task_dlg.close()
                    on_done()

                def _del_task():
                    _stop_draft_timer()
                    delete_workflow_draft(_draft_key)
                    delete_task(task["id"])
                    p.task_dlg.close()
                    on_done()

                ui.button("📋 Duplicate", on_click=_dup_task).props(
                    "flat no-caps"
                ).style("font-size: 0.85rem;")
                ui.button("🗑️ Delete", on_click=_del_task).props(
                    "flat no-caps"
                ).style("color: #ff6b6b; font-size: 0.85rem;")

            # Spacer
            ui.element("div").classes("flex-grow")

            # Right cluster — cancel + save
            ui.button("Cancel", on_click=_cancel_dialog).props(
                "flat no-caps"
            ).style(
                "color: #8888aa; font-weight: 600; font-size: 0.9rem;"
                "padding: 8px 20px; border-radius: 8px;"
            )

            def _save():
                # Determine mode and sync data
                is_advanced = advanced_switch.value
                if is_advanced:
                    _sync_step_data_from_editors()
                    _reassign_step_ids()
                    # Derive prompts from steps for backward compat
                    from tasks import _steps_to_prompts
                    clean_prompts = _steps_to_prompts(_steps_data) or []
                    cur_steps = list(_steps_data)
                else:
                    # Sync prompt textareas
                    for j, _ta in enumerate(prompt_inputs):
                        if j < len(_prompts_data):
                            _prompts_data[j] = _ta.value
                    clean_prompts = [pp for pp in _prompts_data if pp.strip()]
                    cur_steps = []

                cur_name = name_input.value.strip()
                cur_safety = safety_sel.value if safety_sel.value else "block"

                # ── Validation ──────────────────────────────────────
                errors: list[str] = []

                if not cur_name:
                    errors.append("Name is required.")

                if is_advanced and cur_steps:
                    all_ids = {s.get("id", "") for s in cur_steps}
                    all_ids.add("end")
                    for si, s in enumerate(cur_steps, 1):
                        stype = s.get("type", "prompt")
                        if stype == "prompt":
                            if not (s.get("prompt") or "").strip():
                                errors.append(f"Step {si}: Prompt text is required.")
                        elif stype == "condition":
                            cond = s.get("condition", "")
                            if not cond.strip():
                                errors.append(f"Step {si}: Condition expression is required.")
                            else:
                                # Validate operator-specific requirements
                                for op in ("json:",):
                                    if cond.startswith(op):
                                        rest = cond[len(op):]
                                        if not rest or rest.startswith(":"):
                                            errors.append(f"Step {si}: JSON path is required.")
                                        break
                                if cond.startswith("llm:") and not cond[4:].strip():
                                    errors.append(f"Step {si}: LLM question is required.")
                                # Check value-requiring operators
                                for vop in ("contains:", "not_contains:", "equals:",
                                            "matches:", "gt:", "lt:", "gte:", "lte:",
                                            "length_gt:", "length_lt:"):
                                    if cond.startswith(vop) and not cond[len(vop):].strip():
                                        errors.append(f"Step {si}: Value is required for '{vop[:-1]}' operator.")
                                        break
                            # Validate if_true / if_false — at least one must branch
                            it = s.get("if_true", "")
                            iff = s.get("if_false", "")
                            if not it and not iff:
                                errors.append(
                                    f"Step {si}: At least one branch (If true / If false) "
                                    f"must jump to a step or End."
                                )
                            # Validate references exist
                            for field_name, ref in [("If true", it), ("If false", iff)]:
                                if ref and ref not in all_ids:
                                    errors.append(
                                        f"Step {si}: {field_name} references "
                                        f"'{ref}' which doesn't exist."
                                    )
                        elif stype == "approval":
                            if not (s.get("message") or "").strip():
                                errors.append(f"Step {si}: Approval message is required.")
                            # Validate if_approved / if_denied references
                            for field_name, ref in [
                                ("If approved", s.get("if_approved", "")),
                                ("If denied", s.get("if_denied", "")),
                            ]:
                                if ref and ref not in all_ids:
                                    errors.append(
                                        f"Step {si}: {field_name} references "
                                        f"'{ref}' which doesn't exist."
                                    )
                        elif stype == "subtask":
                            if not s.get("task_id"):
                                errors.append(f"Step {si}: Workflow to run is required.")
                        elif stype == "notify":
                            if not (s.get("message") or "").strip():
                                errors.append(f"Step {si}: Notification message is required.")

                # Validate trigger target
                if (trigger_type_sel.value == "task_complete"
                        and not trigger_target_sel.value):
                    errors.append("Trigger: Target task is required for 'task_complete' trigger.")

                if errors:
                    ui.notify(
                        "⚠️ " + errors[0],
                        type="negative",
                        position="top",
                        close_button=True,
                    )
                    if len(errors) > 1:
                        for e in errors[1:]:
                            ui.notify(e, type="warning", position="top")
                    return
                # ── End validation ──────────────────────────────────

                # Build schedule string
                sv = sched_sel.value
                final_schedule = None
                if sv == "Daily":
                    t = sched_time_input.value.strip() or "08:00"
                    final_schedule = f"daily:{t}"
                elif sv == "Weekly":
                    t = sched_time_input.value.strip() or "08:00"
                    d = sched_day_sel.value or "mon"
                    final_schedule = f"weekly:{d}:{t}"
                elif sv == "Interval (hrs)":
                    v = sched_interval_input.value.strip() or "1"
                    final_schedule = f"interval:{v}"
                elif sv == "Interval (min)":
                    v = sched_interval_input.value.strip() or "30"
                    final_schedule = f"interval_minutes:{v}"
                elif sv == "Cron":
                    v = sched_cron_input.value.strip()
                    if v:
                        final_schedule = f"cron:{v}"

                cur_icon = icon_sel.value or "⚡"
                cur_desc = desc_input.value.strip()
                cur_enabled = enabled_switch.value
                cur_del_ch = del_ch_sel.value or None
                cur_del_tgt = None

                # Parse channels (unified selector)
                cur_channels = None  # None = workflow default
                if _ch_mode_sel and _ch_mode_sel.value == "Web app only":
                    cur_channels = []
                elif _ch_mode_sel and _ch_mode_sel.value == "Custom channels":
                    cur_channels = list(_ch_select.value or [])
                cur_model_ov = model_sel.value if model_sel.value != "__default__" else None

                # Parse tools override (advanced mode only)
                cur_tools_override = None
                if is_advanced and _tool_checkboxes:
                    _checked_tools = [
                        n for n, cb in _tool_checkboxes.items() if cb.value
                    ]
                    # If all tools are checked, save as None (= use all)
                    if set(_checked_tools) == set(_all_tool_names):
                        cur_tools_override = None
                    else:
                        cur_tools_override = _checked_tools if _checked_tools else []

                # Parse skills override (advanced mode only)
                cur_skills_override = None
                if is_advanced and _task_sk_checkboxes:
                    _checked = [n for n, cb in _task_sk_checkboxes.items() if cb.value]
                    cur_skills_override = _checked if _checked else []

                # Pipeline settings
                cur_conc_group = conc_group_input.value.strip() or None
                cur_trigger = None
                if trigger_type_sel.value:
                    cur_trigger = {"type": trigger_type_sel.value}
                    if trigger_type_sel.value == "task_complete":
                        cur_trigger["target_task"] = (
                            trigger_target_sel.value or ""
                        )
                    elif trigger_type_sel.value == "webhook":
                        cur_trigger["secret"] = (
                            webhook_secret_input.value or ""
                        )

                try:
                    if is_new:
                        _notify_only = len(clean_prompts) == 0 and not cur_steps
                        _p_thread_id = None
                        if persistent_toggle.value:
                            import uuid as _uuid
                            _p_thread_id = f"pt_{_uuid.uuid4().hex[:10]}"
                        create_task(
                            name=cur_name,
                            prompts=clean_prompts,
                            description=cur_desc,
                            icon=cur_icon,
                            schedule=final_schedule,
                            notify_only=_notify_only,
                            delivery_channel=cur_del_ch,
                            delivery_target=cur_del_tgt,
                            model_override=cur_model_ov,
                            persistent_thread_id=_p_thread_id,
                            skills_override=cur_skills_override,
                            steps=cur_steps if cur_steps else None,
                            safety_mode=cur_safety,
                            concurrency_group=cur_conc_group,
                            trigger=cur_trigger,
                            tools_override=cur_tools_override,
                            channels=cur_channels,
                        )
                        all_t = list_tasks()
                        if all_t:
                            newest = all_t[-1]
                            if not cur_enabled:
                                update_task(newest["id"], enabled=False)
                        ui.notify("✅ Task created", type="positive")
                    else:
                        updates = {}
                        if cur_name != task["name"]:
                            updates["name"] = cur_name
                        if cur_icon != task["icon"]:
                            updates["icon"] = cur_icon
                        if cur_desc != (task.get("description") or ""):
                            updates["description"] = cur_desc
                        if clean_prompts != task["prompts"]:
                            updates["prompts"] = clean_prompts
                        if cur_steps != (task.get("steps") or []):
                            updates["steps"] = cur_steps
                        if cur_safety != (task.get("safety_mode") or "block"):
                            updates["safety_mode"] = cur_safety
                        if final_schedule != task.get("schedule"):
                            updates["schedule"] = final_schedule
                        if cur_enabled != task.get("enabled", True):
                            updates["enabled"] = cur_enabled
                        if cur_del_ch != task.get("delivery_channel"):
                            updates["delivery_channel"] = cur_del_ch
                        if cur_del_tgt != task.get("delivery_target"):
                            updates["delivery_target"] = cur_del_tgt
                        if cur_model_ov != (task.get("model_override") or None):
                            updates["model_override"] = cur_model_ov
                        if cur_skills_override != task.get("skills_override"):
                            updates["skills_override"] = cur_skills_override
                        if cur_conc_group != (task.get("concurrency_group") or None):
                            updates["concurrency_group"] = cur_conc_group
                        if cur_trigger != (task.get("trigger") or None):
                            updates["trigger"] = cur_trigger
                        if cur_tools_override != task.get("tools_override"):
                            updates["tools_override"] = cur_tools_override
                        if cur_channels != task.get("channels"):
                            updates["channels"] = cur_channels
                        # Persistent thread toggle
                        _want_persistent = persistent_toggle.value
                        _had_persistent = bool(task.get("persistent_thread_id"))
                        if _want_persistent != _had_persistent:
                            if _want_persistent:
                                import uuid as _uuid
                                updates["persistent_thread_id"] = f"pt_{_uuid.uuid4().hex[:10]}"
                            else:
                                updates["persistent_thread_id"] = None

                        if updates:
                            update_task(task["id"], **updates)
                            ui.notify("💾 Saved", type="positive")
                        else:
                            ui.notify("No changes.", type="info")
                except ValueError as ve:
                    ui.notify(str(ve), type="negative")
                    return

                delete_workflow_draft(_draft_key)
                _close_dialog()
                on_done()

            ui.button("Save", on_click=_save).props(
                "unelevated no-caps"
            ).style(
                "background: #2d8a4e; color: white; font-weight: 600;"
                "font-size: 0.9rem; padding: 8px 28px; border-radius: 8px;"
            )

    p.task_dlg.open()
