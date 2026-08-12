"""Row-Bot UI - Chat screen (thread conversation view).

Extracted from the monolith's ``_build_chat`` inner function.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import re
import sys
import time
import uuid
from datetime import datetime
from typing import Callable

from row_bot.brand import APP_NATIVE_ENV
from nicegui import events, run, ui

from row_bot.ui.state import AppState, P, _active_generations, context_history_present
from row_bot.ui.constants import ALLOWED_UPLOAD_SUFFIXES, welcome_message, EXAMPLE_PROMPTS
from row_bot.ui.render import (
    agent_result_use_prompt,
    render_agent_tool_results,
    render_image_with_save,
    render_skill_load_stub,
)
from row_bot.ui.performance import log_ui_perf
from row_bot.ui.streaming import request_generation_stop
from row_bot.ui.chat_components import create_context_meter
from row_bot.ui.timer_utils import defer_ui
from row_bot.ui.transcript import (
    TRANSCRIPT_CHUNK_TARGET_MS,
    TRANSCRIPT_MAX_CHUNK_MESSAGES,
    TRANSCRIPT_WINDOW_SIZE,
    choose_transcript_window,
    message_key,
    message_keys,
    reset_transcript_request,
)

logger = logging.getLogger(__name__)


def build_chat(
    state: AppState,
    p: P,
    *,
    rebuild_main: Callable,
    rebuild_thread_list: Callable,
    send_message: Callable,
    open_settings: Callable,
    open_export: Callable,
    show_interrupt: Callable,
    add_chat_message: Callable,
    browse_file: Callable,
) -> None:
    """Render the full chat view for the current thread."""
    _shell_started = time.perf_counter()
    p.chat_shell_generation += 1
    _shell_generation = p.chat_shell_generation
    from row_bot.agent import clear_agent_cache
    from row_bot.models import (
        get_current_model, is_cloud_model, get_cloud_provider,
    )
    from row_bot.providers.selection import (
        model_id_from_choice_value,
        provider_display_label,
    )
    from row_bot.threads import (
        create_thread,
        get_thread_skills_override,
    )
    from row_bot.tasks import get_running_tasks, stop_task
    from row_bot.tools import registry as tool_registry
    from row_bot.ui.helpers import attach_thinking_to_message, persist_thread_media_state
    from row_bot.ui.thread_actions import show_rename_thread_dialog

    def _render_parent_agent_strip() -> None:
        from row_bot.ui.agent_drawer import build_parent_agent_drawer

        build_parent_agent_drawer(
            state,
            p,
            rebuild_main=rebuild_main,
            rebuild_thread_list=rebuild_thread_list,
        )

    def _render_goal_progress_panel() -> str | None:
        from row_bot.ui.goal_ui import build_goal_progress_panel

        return build_goal_progress_panel(
            state,
            p,
            rebuild_main=rebuild_main,
            send_message=send_message,
            surface="main",
        )

    def _ask_parent_to_use_agent_result(run_id: str) -> None:
        prompt = agent_result_use_prompt(run_id)
        if not prompt:
            return
        try:
            ui.notify("Asking the parent to use that Agent result.", type="info", close_button=True)
            asyncio.create_task(send_message(prompt))
        except Exception as exc:
            logger.debug("Could not ask parent to use Agent result", exc_info=True)
            ui.notify(f"Could not ask parent to use Agent result: {exc}", type="negative", close_button=True)

    def _open_child_agent_thread(agent_run: dict) -> None:
        from row_bot.ui.agent_drawer import open_agent_thread

        open_agent_thread(
            agent_run,
            state=state,
            p=p,
            rebuild_main=rebuild_main,
            rebuild_thread_list=rebuild_thread_list,
        )

    def _add_chat_message(msg: dict) -> None:
        add_chat_message(
            msg,
            on_use_agent_result=_ask_parent_to_use_agent_result,
            on_open_agent_thread=_open_child_agent_thread,
        )

    def _child_agent_parent_context() -> dict[str, str] | None:
        if not state.thread_id:
            return None
        try:
            from row_bot.agent_runs import get_agent_run_for_thread
            from row_bot.threads import _get_thread_type, get_thread_name

            if _get_thread_type(state.thread_id) != "agent_child":
                return None
            run_row = get_agent_run_for_thread(state.thread_id)
            parent_thread_id = str((run_row or {}).get("parent_thread_id") or "").strip()
            if not parent_thread_id:
                return None
            return {
                "parent_thread_id": parent_thread_id,
                "parent_name": get_thread_name(parent_thread_id) or "Parent thread",
                "run_id": str((run_row or {}).get("id") or ""),
            }
        except Exception:
            logger.debug("Could not resolve child Agent parent context", exc_info=True)
            return None

    def _open_parent_thread(parent_thread_id: str) -> None:
        try:
            from row_bot.memory_extraction import set_active_thread
            from row_bot.threads import (
                _get_thread_approval_mode,
                _get_thread_developer_workspace,
                _get_thread_model_override,
                get_thread_name,
            )
            from row_bot.ui.helpers import load_thread_messages
            from row_bot.ui.voice_lifecycle import stop_voice_for_thread_change

            target = str(parent_thread_id or "").strip()
            if not target:
                return
            prev = state.thread_id
            prev_gen = _active_generations.get(prev) if prev else None
            if prev_gen and str(getattr(prev_gen, "status", "")) == "streaming":
                from row_bot.ui.streaming import _detach_generation

                _detach_generation(prev_gen, state, "back_to_parent_agent_thread")
            stop_voice_for_thread_change(state, p, reason="back_to_parent_agent_thread")
            state.active_designer_project = None
            state.thread_id = target
            state.active_developer_workspace_id = _get_thread_developer_workspace(target) or None
            state.thread_name = get_thread_name(target) or "Parent thread"
            state.thread_model_override = _get_thread_model_override(target)
            state.thread_approval_mode = _get_thread_approval_mode(target)
            state.messages = load_thread_messages(target)
            p.pending_files.clear()
            set_active_thread(target, previous_id=prev)
            rebuild_main()
            rebuild_thread_list()
        except Exception as exc:
            logger.debug("Could not return to Agent parent thread", exc_info=True)
            ui.notify(f"Could not open parent thread: {exc}", type="negative", close_button=True)

    # Header
    _header_started = time.perf_counter()
    running_wfs = get_running_tasks()
    bg = running_wfs.get(state.thread_id)
    child_parent_context = _child_agent_parent_context()

    with ui.row().classes("w-full items-center shrink-0"):
        if bg:
            ui.html(
                f"<h3>{bg['name']} "
                f"<span style='font-size:0.8rem; opacity:0.7;'>"
                f"Running - Step {bg['step']+1}/{bg['total']}</span></h3>",
                sanitize=False,
            )
            def _stop_task_from_header(tid=state.thread_id):
                stop_task(tid)
                ui.notify("Stop signal sent - task will stop after current step.", type="warning")
                rebuild_main()
            ui.button(icon="stop", on_click=_stop_task_from_header).props(
                "round color=red size=sm"
            ).tooltip("Stop task")
        else:
            with ui.row().classes("items-center gap-1 no-wrap flex-grow").style("min-width: 0;"):
                p.chat_header_label = ui.label(str(state.thread_name or "Untitled")).classes("text-h5 ellipsis").style("min-width: 0;")
                if state.thread_id:
                    ui.button(
                        icon="edit",
                        on_click=lambda: show_rename_thread_dialog(
                            thread_id=state.thread_id or "",
                            current_name=str(state.thread_name or ""),
                            state=state,
                            rebuild_thread_list=rebuild_thread_list,
                            rebuild_main=rebuild_main,
                        ),
                    ).props("flat dense round size=sm").tooltip("Rename")
                    from row_bot.ui.profile_picker import build_profile_picker

                    build_profile_picker(
                        state,
                        p=p,
                        rebuild_main=rebuild_main,
                        rebuild_thread_list=rebuild_thread_list,
                        label="Profile",
                        surface="chat",
                    )
                    if child_parent_context:
                        ui.badge(
                            f"Child Agent of {child_parent_context['parent_name']}",
                            color="blue-grey",
                        ).props("outline").classes("ellipsis").style("max-width: min(320px, 38vw);")
                        ui.button(
                            "Back to Parent",
                            icon="arrow_back",
                            on_click=lambda pid=child_parent_context["parent_thread_id"]: _open_parent_thread(pid),
                        ).props("flat dense no-caps")

            # Model selection now lives in the composer, matching Designer.

        if state.messages:
            ui.button(icon="download", on_click=open_export).props("flat round").tooltip("Export")
    log_ui_perf(
        "chat.header.render",
        (time.perf_counter() - _header_started) * 1000.0,
        threshold_ms=200.0,
        thread_id=state.thread_id,
    )

    # Cloud/local model banner
    def _model_surface_placeholder():
        active_model = state.thread_model_override or get_current_model()
        model_label = model_id_from_choice_value(active_model)
        prov = get_cloud_provider(active_model) or "ollama"
        prov_label = provider_display_label(prov)
        cloud = is_cloud_model(active_model)
        return {
            "model": active_model,
            "cloud": cloud,
            "mode": "Checking readiness",
            "icon": "cloud" if cloud else "lock",
            "icon_color": "orange" if cloud else "green",
            "text": (
                f"Using {model_label} via {prov_label} - data is sent to the cloud"
                if cloud
                else f"Using {model_label} via {prov_label} - local/private"
            ),
            "text_class": "text-orange text-sm" if cloud else "text-green text-sm",
            "banner_style": (
                "background: rgba(255, 152, 0, 0.08); "
                "border-radius: 8px; border: 1px solid rgba(255, 152, 0, 0.25);"
            ) if cloud else (
                "background: rgba(76, 175, 80, 0.08); "
                "border-radius: 8px; border: 1px solid rgba(76, 175, 80, 0.25);"
            ),
            "scroll_style": "background: rgba(255, 152, 0, 0.03);" if cloud else "background: rgba(76, 175, 80, 0.03);",
        }

    def _resolve_model_surface():
        started = time.perf_counter()
        phase_timings: dict[str, float | str | bool] = {}
        active_model = state.thread_model_override or get_current_model()
        model_label = model_id_from_choice_value(active_model)
        prov = get_cloud_provider(active_model) or "ollama"
        prov_label = provider_display_label(prov)
        local_execution = False
        mode_label = "Agent Mode"
        try:
            from row_bot.providers.resolution import resolve_provider_config
            from row_bot.providers.readiness import evaluate_runtime_readiness

            provider_config_started = time.perf_counter()
            resolved = resolve_provider_config(active_model, allow_legacy_local=True)
            phase_timings["provider_config_ms"] = (time.perf_counter() - provider_config_started) * 1000.0
            prov = resolved.provider_id
            prov_label = resolved.provider_display_name
            model_label = resolved.runtime_model
            local_execution = resolved.execution_location == "local" or resolved.risk_label == "local_private"
            readiness_started = time.perf_counter()
            runtime = evaluate_runtime_readiness(resolved, refresh_provider_status=False)
            phase_timings["runtime_readiness_ms"] = (time.perf_counter() - readiness_started) * 1000.0
            phase_timings.update({
                f"readiness_{key}": value
                for key, value in getattr(runtime, "timings", {}).items()
            })
            phase_timings["provider_id"] = prov
            phase_timings["model_ref"] = resolved.selection_ref
            phase_timings["refresh_provider_status"] = False
            if runtime.selected_mode == "agent":
                mode_label = "Agent Mode"
            elif runtime.selected_mode == "chat_only":
                mode_label = "Chat Only - tools and actions are off"
            else:
                mode_label = "Unavailable - " + runtime.selection_reason
        except Exception:
            local_execution = not is_cloud_model(active_model)
        phase_timings["total_ms"] = (time.perf_counter() - started) * 1000.0
        if not local_execution and is_cloud_model(active_model):
            try:
                from row_bot.providers.ollama import is_ollama_cloud_offload_model
                if prov == "ollama" and is_ollama_cloud_offload_model(model_label):
                    prov_label = "Ollama Cloud Offload"
            except Exception:
                pass
            return {
                "model": active_model,
                "cloud": True,
                "mode": mode_label,
                "icon": "cloud",
                "icon_color": "orange",
                "text": f"Using {model_label} via {prov_label} - data is sent to the cloud",
                "text_class": "text-orange text-sm",
                "timings": phase_timings,
                "banner_style": (
                    "background: rgba(255, 152, 0, 0.08); "
                    "border-radius: 8px; border: 1px solid rgba(255, 152, 0, 0.25);"
                ),
                "scroll_style": "background: rgba(255, 152, 0, 0.03);",
            }
        return {
            "model": active_model,
            "cloud": False,
            "mode": mode_label,
            "icon": "lock",
            "icon_color": "green",
            "text": f"Using {model_label} via {prov_label} - local/private",
            "text_class": "text-green text-sm",
            "timings": phase_timings,
            "banner_style": (
                "background: rgba(76, 175, 80, 0.08); "
                "border-radius: 8px; border: 1px solid rgba(76, 175, 80, 0.25);"
            ),
            "scroll_style": "background: rgba(76, 175, 80, 0.03);",
        }

    def _render_model_banner(surface: dict) -> None:
        if not p.model_banner_container:
            return
        p.model_banner_container.clear()
        with p.model_banner_container:
            with ui.row().classes("w-full items-center gap-2 q-px-sm q-py-xs").style(surface["banner_style"]):
                if surface["cloud"]:
                    ui.icon("cloud", color=surface["icon_color"]).style("font-size: 1.1rem;")
                else:
                    ui.icon("lock", color=surface["icon_color"]).style("font-size: 1.1rem;")
                ui.label(surface["text"]).classes(surface["text_class"])
                ui.badge(surface.get("mode") or "Agent Mode", color="blue-grey").props("outline dense")

    async def _resolve_and_render_model_surface() -> None:
        started = time.perf_counter()
        surface = await run.io_bound(_resolve_model_surface)
        if p.chat_shell_generation != _shell_generation:
            return
        try:
            from row_bot.models import get_context_policy
            from row_bot.ui.chat_components import notify_context_policy_once

            active_model = state.thread_model_override or get_current_model()
            policy = await run.io_bound(lambda: get_context_policy(active_model))
            notify_context_policy_once(state, policy)
            if p.context_meter:
                p.context_meter.update(
                    state.context_usage,
                    capacity_state=state.context_capacity_state,
                    effective_limit_tokens=state.context_capacity_effective_tokens,
                    has_history=context_history_present(state),
                )
        except Exception:
            logger.debug("Could not render context capacity policy", exc_info=True)
        _render_model_banner(surface)
        if p.chat_scroll:
            p.chat_scroll.style(replace=surface["scroll_style"])
        log_ui_perf(
            "chat.model_surface.resolve",
            (time.perf_counter() - started) * 1000.0,
            threshold_ms=500.0,
            thread_id=state.thread_id,
            **dict(surface.get("timings") or {}),
        )

    def _refresh_model_surface() -> None:
        if p.context_meter:
            p.context_meter.update(
                state.context_usage,
                capacity_state=state.context_capacity_state,
                effective_limit_tokens=state.context_capacity_effective_tokens,
                has_history=context_history_present(state),
            )
        surface = _model_surface_placeholder()
        _render_model_banner(surface)
        if p.chat_scroll:
            p.chat_scroll.style(replace=surface["scroll_style"])
        defer_ui(_resolve_and_render_model_surface, delay=0.05)

    _surface = _model_surface_placeholder()
    p.model_banner_container = ui.column().classes("w-full gap-0")
    _render_model_banner(_surface)
    defer_ui(_resolve_and_render_model_surface, delay=0.05)
    p.parent_agent_strip_container = ui.column().classes("w-full gap-0")

    def _refresh_parent_agent_strip() -> None:
        if p.parent_agent_strip_container is None:
            return
        if p.parent_agent_dialog_open:
            return
        try:
            p.parent_agent_strip_container.clear()
            with p.parent_agent_strip_container:
                _render_parent_agent_strip()
        except Exception:
            logger.debug("Could not refresh parent Agent strip", exc_info=True)

    p.refresh_parent_agent_strip = _refresh_parent_agent_strip
    _refresh_parent_agent_strip()

    def _refresh_goal_strip() -> None:
        if p.goal_strip_container is None:
            return
        try:
            p.goal_strip_container.clear()
            with p.goal_strip_container:
                _render_goal_progress_panel()
        except Exception:
            logger.debug("Could not refresh Goal strip", exc_info=True)

    p.refresh_goal_strip = _refresh_goal_strip

    # Scrollable message area
    p.chat_scroll = ui.scroll_area().classes("w-full flex-grow").style(_surface["scroll_style"])

    with p.chat_scroll:
        p.chat_container = ui.column().classes("w-full gap-2")

    # Render existing messages
    _transcript_started = time.perf_counter()
    _reattach_gen = _active_generations.get(state.thread_id)
    _has_active_gen = (_reattach_gen and _reattach_gen.detached and _reattach_gen.status == "streaming")
    _has_running_task = state.thread_id in get_running_tasks()
    _msgs_to_render = state.messages
    if ((_has_active_gen or _has_running_task)
            and _msgs_to_render
            and _msgs_to_render[-1].get("content", "").startswith(
                "\u26a0\ufe0f The assistant was interrupted")):
        _msgs_to_render = _msgs_to_render[:-1]
    reset_transcript_request(p, state.thread_id)
    _requested_start = (
        p.transcript_requested_start
        if p.transcript_requested_thread_id == state.thread_id
        else None
    )
    _window = choose_transcript_window(
        len(_msgs_to_render),
        requested_start=_requested_start,
        window_size=TRANSCRIPT_WINDOW_SIZE,
    )
    _all_msg_keys = message_keys(_msgs_to_render)
    _display_msgs = _msgs_to_render[_window.start:_window.end]
    _display_keys = _all_msg_keys[_window.start:_window.end]
    p.transcript_thread_id = state.thread_id
    p.transcript_generation += 1
    _render_generation = p.transcript_generation
    p.transcript_rendered_keys = []
    p.transcript_window_start = _window.start
    p.transcript_window_size = _window.visible_count
    p.transcript_total = _window.total

    if _window.older_count:
        with p.chat_container:
            with ui.row().classes("w-full justify-center q-py-sm"):
                _load_label = f"Load earlier messages ({_window.older_count})"

                def _load_earlier() -> None:
                    p.transcript_requested_thread_id = state.thread_id
                    p.transcript_requested_start = max(
                        0,
                        _window.start - TRANSCRIPT_WINDOW_SIZE,
                    )
                    rebuild_main()

                ui.button(_load_label, icon="expand_less", on_click=_load_earlier).props(
                    "flat dense no-caps"
                ).classes("text-grey-5")

    def _render_message_at(local_idx: int) -> None:
        _add_chat_message(_display_msgs[local_idx])
        p.transcript_rendered_keys.append(_display_keys[local_idx])

    # Progressive render
    # For responsiveness on large threads, render the first batch
    # synchronously so the user sees content immediately, then stream
    # the remainder via chained timers (each yields to the event loop
    # so clicks / input remain responsive). Reattach / onboarding /
    # scroll hooks run AFTER all messages are in the DOM so order is
    # preserved.
    _INITIAL_RENDER = min(8, len(_display_msgs))
    for _idx in range(_INITIAL_RENDER):
        _render_message_at(_idx)
    _remaining_start = _INITIAL_RENDER

    # Reattach to running generation
    def _finalize_after_messages() -> None:
        if _reattach_gen and _reattach_gen.detached and _reattach_gen.status == "streaming":
            from row_bot.identity import get_assistant_name as _gan_ra
            from row_bot.ui.status_bar import get_bot_avatar_html as _gba_ra
            _ra_avatar = _gba_ra()
            _ra_name = _gan_ra()
            with p.chat_container:
                with ui.element("div").classes("row-bot-msg-row"):
                    ui.html(f'<div class="row-bot-avatar row-bot-avatar-bot">{_ra_avatar}</div>', sanitize=False)
                    with ui.column().classes("row-bot-msg-body gap-1") as _ra_wrapper:
                        ui.html(
                            '<div class="row-bot-msg-header">'
                            f'<span class="row-bot-msg-name">{_ra_name}</span>'
                            f'<span class="row-bot-msg-stamp">{datetime.now().strftime("%H:%M")}</span>'
                            '</div>',
                            sanitize=False,
                        )
                        _reattach_gen.tool_col = ui.column().classes("w-full gap-1")
                        from row_bot.ui.tool_trace import (
                            display_tool_content,
                            group_tool_results,
                            is_agent_tool_result,
                            is_skill_load_noop_result,
                            parse_skill_load_result,
                            tool_group_status,
                            tool_result_failed,
                        )
                        _agent_tool_results = [
                            _tr for _tr in _reattach_gen.tool_results
                            if isinstance(_tr, dict) and is_agent_tool_result(_tr)
                        ]
                        _skill_payloads = []
                        _seen_skill_ids: set[str] = set()
                        for _tr in _reattach_gen.tool_results:
                            _payload = parse_skill_load_result(_tr) if isinstance(_tr, dict) else None
                            if _payload and _payload["skill_id"] not in _seen_skill_ids:
                                _seen_skill_ids.add(_payload["skill_id"])
                                _skill_payloads.append(_payload)
                        _generic_tool_results = [
                            _tr for _tr in _reattach_gen.tool_results
                            if not (
                                isinstance(_tr, dict)
                                and (
                                    is_agent_tool_result(_tr)
                                    or parse_skill_load_result(_tr)
                                    or is_skill_load_noop_result(_tr)
                                )
                            )
                        ]
                        if _skill_payloads:
                            with _reattach_gen.tool_col:
                                for _payload in _skill_payloads:
                                    render_skill_load_stub(_payload)
                        if _agent_tool_results:
                            with _reattach_gen.tool_col:
                                render_agent_tool_results(
                                    _agent_tool_results,
                                    thread_id=state.thread_id,
                                    on_use_agent_result=_ask_parent_to_use_agent_result,
                                )
                        for _group in group_tool_results(_generic_tool_results):
                            _group_status, _group_icon = tool_group_status(_group.results)
                            with _reattach_gen.tool_col:
                                with ui.expansion(
                                    f"{_group_status} {_group.label}",
                                    icon=_group_icon,
                                ).classes("w-full"):
                                    for _idx, _tr in enumerate(_group.results, start=1):
                                        _item_failed = tool_result_failed(_tr)
                                        with ui.expansion(
                                            (
                                                f"#{_idx} failed"
                                                if _item_failed and _group.count > 1
                                                else f"#{_idx}"
                                                if _group.count > 1
                                                else _group.name
                                            ),
                                            icon="error" if _item_failed else "subdirectory_arrow_right",
                                        ).classes("w-full"):
                                            _disp = display_tool_content(_tr.get("content", ""))
                                            if _disp:
                                                ui.code(_disp).classes("w-full text-xs")
                        for _cj in _reattach_gen.chart_data:
                            try:
                                import plotly.io as _pio
                                _fig = _pio.from_json(_cj)
                                with _reattach_gen.tool_col:
                                    ui.plotly(_fig).classes("w-full")
                            except Exception:
                                logger.debug("Chart rendering failed during reattach", exc_info=True)
                        for _img in _reattach_gen.captured_images:
                            try:
                                with _reattach_gen.tool_col:
                                    render_image_with_save(_img)
                            except Exception:
                                logger.debug("Image rendering failed during reattach", exc_info=True)
                        for _vid in _reattach_gen.captured_videos:
                            try:
                                with _reattach_gen.tool_col:
                                    from row_bot.ui.render import render_video_with_save
                                    render_video_with_save(_vid.get("path", "") if isinstance(_vid, dict) else _vid)
                            except Exception:
                                logger.debug("Video rendering failed during reattach", exc_info=True)
                        if _reattach_gen.thinking_text:
                            with _reattach_gen.tool_col:
                                with ui.expansion(
                                    "\U0001f4ad Thinking", icon="psychology"
                                ).classes("w-full"):
                                    ui.code(
                                        _reattach_gen.thinking_text.strip()[:8_000]
                                    ).classes("w-full text-xs")
                            _reattach_gen.thinking_collapsed = True
                        for _notice in _reattach_gen.warnings:
                            if not isinstance(_notice, dict):
                                continue
                            with ui.card().classes(
                                "w-full bg-amber-1 text-amber-10 border border-amber-4 q-pa-sm"
                            ):
                                ui.label(str(_notice.get("title") or "Memory recall fallback")).classes(
                                    "text-sm font-semibold"
                                )
                                ui.label(str(_notice.get("message") or "")).classes("text-xs")
                                ui.label(f"Reason: {_notice.get('detail') or ''}").classes("text-xs")
                                ui.label(f"Next: {_notice.get('action') or ''}").classes("text-xs font-medium")
                        _reattach_gen.assistant_md = ui.markdown(
                            _reattach_gen.accumulated,
                            extras=['code-friendly', 'fenced-code-blocks', 'tables'],
                        ).classes("row-bot-msg w-full")
                        _reattach_gen.wrapper = _ra_wrapper
                        _reattach_gen.thinking_label = None
                        _reattach_gen.thinking_md = None
            _reattach_gen.detached = False
            _refresh_parent_agent_strip()
            if p.stop_btn:
                p.stop_btn.enable()
        elif _reattach_gen and _reattach_gen.status in ("done", "error", "stopped", "interrupted"):
            final_tool_results = list(_reattach_gen.tool_results or [])
            final_agent_run_ids: list[str] = []
            promoted_agent_run_ids: list[str] = []
            if final_tool_results:
                try:
                    from row_bot.ui.streaming import (
                        _filter_visible_agent_tool_results,
                        _ordered_agent_run_ids_from_tool_results,
                        _refresh_key_for_agent_run_ids,
                    )

                    final_agent_run_ids = _ordered_agent_run_ids_from_tool_results(final_tool_results)
                    if final_agent_run_ids:
                        existing_agent_run_ids = {
                            str(run_id)
                            for msg in state.messages
                            if isinstance(msg, dict)
                            for run_id in (msg.get("agent_run_ids") or [])
                            if str(run_id or "").strip()
                        }
                        promoted_agent_run_ids = [
                            run_id for run_id in final_agent_run_ids
                            if run_id not in existing_agent_run_ids
                        ]
                    filter_messages = state.messages
                    if promoted_agent_run_ids:
                        filter_messages = [
                            *state.messages,
                            {"role": "assistant", "agent_run_ids": promoted_agent_run_ids},
                        ]
                    final_tool_results, _removed_agent_results = _filter_visible_agent_tool_results(
                        filter_messages,
                        final_tool_results,
                    )
                except Exception:
                    logger.debug("Agent tool result promotion during reattach failed", exc_info=True)
                    promoted_agent_run_ids = []
                    final_tool_results = list(_reattach_gen.tool_results or [])
            if (
                _reattach_gen.accumulated
                or final_tool_results
                or promoted_agent_run_ids
                or _reattach_gen.chart_data
                or _reattach_gen.captured_images
                or _reattach_gen.captured_videos
            ):
                a_msg: dict = {"role": "assistant", "content": _reattach_gen.accumulated}
                attach_thinking_to_message(a_msg, _reattach_gen.thinking_text)
                if _reattach_gen.warnings:
                    a_msg["warnings"] = list(_reattach_gen.warnings)
                if promoted_agent_run_ids:
                    a_msg["agent_run_ids"] = promoted_agent_run_ids
                    try:
                        from row_bot.ui.streaming import _refresh_key_for_agent_run_ids

                        a_msg["agent_run_refresh_key"] = _refresh_key_for_agent_run_ids(promoted_agent_run_ids)
                    except Exception:
                        logger.debug("Could not build reattach Agent card refresh key", exc_info=True)
                if final_tool_results:
                    a_msg["tool_results"] = final_tool_results
                if _reattach_gen.chart_data:
                    a_msg["charts"] = _reattach_gen.chart_data
                if _reattach_gen.captured_images:
                    a_msg["images"] = _reattach_gen.captured_images
                    if _reattach_gen.captured_images_persist:
                        a_msg["_media_persist_flags"] = list(_reattach_gen.captured_images_persist)
                        if any(_reattach_gen.captured_images_persist):
                            a_msg["_media_persist"] = True
                if _reattach_gen.captured_videos:
                    a_msg["videos"] = _reattach_gen.captured_videos
                state.messages.append(a_msg)
                persist_thread_media_state(state.thread_id, state.messages)
                _add_chat_message(a_msg)
                p.transcript_rendered_keys.append(message_key(len(state.messages) - 1, a_msg))
                p.transcript_total = len(state.messages)
                p.transcript_window_size = len(p.transcript_rendered_keys)
                if promoted_agent_run_ids:
                    try:
                        from types import SimpleNamespace
                        from row_bot.ui.streaming import (
                            _schedule_agent_tool_result_card_refresh,
                            _schedule_direct_agent_card_refresh,
                        )

                        def _refresh_chat_for_agent_card() -> None:
                            try:
                                rebuild_main(immediate=True, reason="agent_card_refresh")
                            except TypeError:
                                rebuild_main()

                        refresh_cb = SimpleNamespace(
                            rebuild_main=rebuild_main,
                            refresh_chat_messages=_refresh_chat_for_agent_card,
                            refresh_parent_agent_strip=_refresh_parent_agent_strip,
                        )
                        _schedule_direct_agent_card_refresh(
                            state=state,
                            cb=refresh_cb,
                            thread_id=state.thread_id,
                            run_ids=promoted_agent_run_ids,
                        )
                        if final_agent_run_ids:
                            _schedule_agent_tool_result_card_refresh(
                                state=state,
                                cb=refresh_cb,
                                thread_id=state.thread_id,
                                run_ids=final_agent_run_ids,
                            )
                    except Exception:
                        logger.debug("Could not schedule reattach Agent card refresh", exc_info=True)
                elif final_agent_run_ids:
                    try:
                        from types import SimpleNamespace
                        from row_bot.ui.streaming import _schedule_agent_tool_result_card_refresh

                        def _refresh_chat_for_agent_tool_result() -> None:
                            try:
                                rebuild_main(immediate=True, reason="agent_tool_result_refresh")
                            except TypeError:
                                rebuild_main()

                        _schedule_agent_tool_result_card_refresh(
                            state=state,
                            cb=SimpleNamespace(
                                rebuild_main=rebuild_main,
                                refresh_chat_messages=_refresh_chat_for_agent_tool_result,
                                refresh_parent_agent_strip=_refresh_parent_agent_strip,
                            ),
                            thread_id=state.thread_id,
                            run_ids=final_agent_run_ids,
                        )
                    except Exception:
                        logger.debug("Could not schedule reattach Agent tool-result refresh", exc_info=True)
            _active_generations.pop(state.thread_id, None)

        # Onboarding
        if state.show_onboarding:
            from row_bot.identity import get_assistant_name as _gan_ob
            from row_bot.ui.status_bar import get_bot_avatar_html as _gba_ob
            _ob_avatar = _gba_ob()
            _ob_name = _gan_ob()
            with p.chat_container:
                with ui.element("div").classes("row-bot-msg-row"):
                    ui.html(f'<div class="row-bot-avatar row-bot-avatar-bot">{_ob_avatar}</div>', sanitize=False)
                    with ui.column().classes("row-bot-msg-body gap-1"):
                        ui.html(
                            '<div class="row-bot-msg-header">'
                            f'<span class="row-bot-msg-name">{_ob_name}</span>'
                            '</div>',
                            sanitize=False,
                        )
                        _cloud_ob2 = bool(_surface["cloud"])
                        ui.markdown(welcome_message(cloud=_cloud_ob2), extras=['code-friendly', 'fenced-code-blocks', 'tables'])
                        with ui.row().classes("flex-wrap gap-2"):
                            for prompt in EXAMPLE_PROMPTS:
                                def _try_inline(pr=prompt):
                                    state.show_onboarding = False
                                    asyncio.create_task(send_message(pr))
                                ui.button(prompt, on_click=_try_inline).props("flat dense outline").style("text-transform:none;")

                        def _dismiss():
                            state.show_onboarding = False
                            rebuild_main()
                        ui.button("Dismiss", icon="close", on_click=_dismiss).props("flat dense")

        # Interrupt UI
        if state.pending_interrupt:
            show_interrupt(state.pending_interrupt)

        if p.chat_scroll:
            p.chat_scroll.scroll_to(percent=1.0)
            # Client-side auto-scroll: a MutationObserver scrolls to the
            # bottom whenever content changes, unless the user has scrolled up.
            # Uses wheel/touchstart timestamps to distinguish user-initiated
            # scrolls from programmatic ones (MutationObserver).  On Mac
            # WKWebView the old approach of checking scroll position in every
            # scroll event caused a feedback loop because the programmatic
            # scroll fired a scroll event that immediately re-enabled _tSS.
            _sid = p.chat_scroll.id
            ui.run_javascript(f"""(function(){{
                var el = getElement({_sid});
                if (!el || !el.$el) return;
                var c = el.$el.querySelector('.q-scrollarea__container');
                if (!c || !(c instanceof Node)) return;
                el._tSS = true;
                var uTs = 0;
                c.addEventListener('wheel', function() {{ uTs = Date.now(); }}, {{passive:true}});
                c.addEventListener('touchstart', function() {{ uTs = Date.now(); }}, {{passive:true}});
                c.addEventListener('scroll', function() {{
                    if (Date.now() - uTs > 1000) return;
                    el._tSS = (c.scrollHeight - c.scrollTop - c.clientHeight) < 50;
                }});
                new MutationObserver(function() {{
                    if (el._tSS) c.scrollTop = c.scrollHeight;
                }}).observe(c, {{childList: true, subtree: true, characterData: true}});
            }})()""")

    # If there are leftover messages, stream them into the container
    # in small deferred chunks. Each deferred task yields to the event
    # loop so the UI stays responsive without leaving NiceGUI timer
    # elements attached to containers that can be deleted on navigation.
    # Finalize
    # runs after the last chunk so reattach / onboarding land at the
    # bottom of the chat.
    def _log_transcript_render() -> None:
        media_count = 0
        for _msg in _display_msgs:
            media_count += len(_msg.get("images") or [])
            media_count += len(_msg.get("videos") or [])
            media_count += len(_msg.get("charts") or [])
        log_ui_perf(
            "chat.transcript.render",
            (time.perf_counter() - _transcript_started) * 1000.0,
            rows=len(_display_msgs),
            total_rows=len(_msgs_to_render),
            window_start=_window.start,
            media=media_count,
            thread_id=state.thread_id,
        )

    if _remaining_start < len(_display_msgs):
        _chunk_state = {"idx": _remaining_start}
        def _render_next_chunk():
            if (
                p.transcript_thread_id != state.thread_id
                or p.transcript_generation != _render_generation
            ):
                return
            start = _chunk_state["idx"]
            end = start
            chunk_started = time.perf_counter()
            try:
                with p.chat_container:
                    while end < len(_display_msgs):
                        _render_message_at(end)
                        end += 1
                        if end - start >= TRANSCRIPT_MAX_CHUNK_MESSAGES:
                            break
                        elapsed_ms = (time.perf_counter() - chunk_started) * 1000.0
                        if elapsed_ms >= TRANSCRIPT_CHUNK_TARGET_MS:
                            break
            except Exception:
                logger.debug("chunked chat render failed", exc_info=True)
                _finalize_after_messages()
                return
            _chunk_state["idx"] = end
            if end < len(_display_msgs):
                defer_ui(_render_next_chunk)
            else:
                _finalize_after_messages()
                _log_transcript_render()
        defer_ui(_render_next_chunk)
    else:
        _finalize_after_messages()
        _log_transcript_render()

    # File chips (created early so _on_upload can reference)
    # We'll parent these inside the input card below

    _composer_started = time.perf_counter()

    async def _on_upload(e: events.UploadEventArguments):
        data = await e.file.read()
        name = e.file.name
        p.pending_files.append({"name": name, "data": data})
        if hasattr(e, 'sender') and hasattr(e.sender, 'reset'):
            e.sender.reset()
        with p.file_chips_row:
            idx = len(p.pending_files) - 1
            def _remove(i=idx, badge=None):
                if i < len(p.pending_files):
                    p.pending_files.pop(i)
                if badge:
                    badge.delete()
            b = ui.badge(f"Attached: {name} x", color="grey-8").props("outline")
            b.on("click", lambda b=b, i=idx: _remove(i, b))
            b.style("cursor: pointer;")

    _hidden_upload = ui.upload(on_upload=_on_upload, auto_upload=True, multiple=True).classes("hidden")

    if p.chat_upload_js_installed:
        ui.run_javascript(f"window._rowBotUploadId = {_hidden_upload.id};")
    else:
        p.chat_upload_js_installed = True
        ui.run_javascript(f'''
            (() => {{
                window._rowBotUploadId = {_hidden_upload.id};
                if (window._rowBotUploadHooksInstalled) return;
                window._rowBotUploadHooksInstalled = true;
                const body = document.body;
                let overlay = null;
                let dragTimer = null;
                function showOverlay() {{
                    if (overlay) return;
                    overlay = document.createElement("div");
                    overlay.style.cssText = "position:fixed;inset:0;z-index:9999;" +
                        "background:rgba(30,136,229,0.15);border:3px dashed #1e88e5;" +
                        "display:flex;align-items:center;justify-content:center;pointer-events:none;";
                    overlay.innerHTML = '<div style="color:#1e88e5;font-size:1.5rem;font-weight:600;">Drop files here</div>';
                    document.body.appendChild(overlay);
                }}
                function hideOverlay() {{
                    if (overlay) {{ overlay.remove(); overlay = null; }}
                    if (dragTimer) {{ clearTimeout(dragTimer); dragTimer = null; }}
                }}
                body.addEventListener("dragover", (e) => {{
                    e.preventDefault(); showOverlay();
                    if (dragTimer) clearTimeout(dragTimer);
                    dragTimer = setTimeout(hideOverlay, 300);
                }});
                body.addEventListener("dragleave", (e) => {{
                    if (e.relatedTarget === null || !body.contains(e.relatedTarget)) hideOverlay();
                }});
                document.addEventListener("drop", (e) => {{
                    hideOverlay();
                    const inUploader = e.target.closest && e.target.closest('.q-uploader');
                    if (inUploader) return;
                    e.preventDefault();
                    const files = e.dataTransfer?.files;
                    if (!files || files.length === 0) return;
                    const vue = getElement(window._rowBotUploadId);
                    if (vue && vue.$refs.qRef) vue.$refs.qRef.addFiles(files);
                }}, true);
                document.addEventListener("paste", (e) => {{
                    const items = e.clipboardData?.items;
                    if (!items) return;
                    const imageFiles = [];
                    for (const item of items) {{
                        if (item.type.startsWith("image/")) {{
                            const file = item.getAsFile();
                            if (file) {{
                                const ext = file.type.split("/")[1] || "png";
                                const ts = Date.now();
                                const named = new File([file], "pasted_image_" + ts + "." + ext, {{type: file.type}});
                                imageFiles.push(named);
                            }}
                        }}
                    }}
                    if (imageFiles.length === 0) return;
                    e.preventDefault();
                    const vue = getElement(window._rowBotUploadId);
                    if (vue && vue.$refs.qRef) vue.$refs.qRef.addFiles(imageFiles);
                }});
            }})();
        ''')
    # Chat input card
    async def _on_attach():
        if sys.platform == "darwin" and os.environ.get(APP_NATIVE_ENV) == "1":
            path = await browse_file(
                title="Attach file",
                filetypes=[("Supported files", " ".join(f"*.{e}" for e in ALLOWED_UPLOAD_SUFFIXES))],
            )
            if path and os.path.isfile(path):
                name = os.path.basename(path)
                data = await run.io_bound(pathlib.Path(path).read_bytes)
                p.pending_files.append({"name": name, "data": data})
                with p.file_chips_row:
                    idx = len(p.pending_files) - 1
                    def _remove(i=idx, badge=None):
                        if i < len(p.pending_files):
                            p.pending_files.pop(i)
                        if badge:
                            badge.delete()
                    b = ui.badge(f"Attached: {name} x", color="grey-8").props("outline")
                    b.on("click", lambda b=b, i=idx: _remove(i, b))
                    b.style("cursor: pointer;")
        else:
            await ui.run_javascript(
                f"document.getElementById('c{_hidden_upload.id}').querySelector('input[type=file]').click()"
            )

    p.goal_strip_container = ui.column().classes("w-full shrink-0 gap-0 row-bot-goal-strip-slot")
    p.goal_strip_refresh_timer = ui.timer(2.0, _refresh_goal_strip)
    _refresh_goal_strip()

    from row_bot.ui.live_control import build_live_control_dock

    build_live_control_dock(
        state,
        p,
        stop_generation=lambda thread_id: request_generation_stop(
            thread_id,
            state=state,
            p=p,
            reason="live_control",
        ),
    )

    with ui.column().classes("w-full shrink-0 gap-0").style(
        "border: 1px solid rgba(255,255,255,0.15); border-radius: 18px; "
        "background: rgba(255,255,255,0.04); padding: 0; overflow: hidden; "
        "position: relative;"
    ):
        # File chips inside the card (top)
        p.file_chips_row = ui.row().classes("w-full flex-wrap gap-1 q-px-md q-pt-sm")

        def _queue_skill_chip_refresh(text: str) -> None:
            return None

        def _slash_palette_on_text(text: str, cursor: int | None = None) -> None:
            return None

        def _slash_palette_move(delta: int) -> None:
            return None

        def _slash_palette_close() -> None:
            return None

        def _slash_palette_pick_selected() -> None:
            return None

        def _slash_palette_handle_key(key: str) -> None:
            return None

        try:
            import row_bot.skills as _skills_mod
            from row_bot.skills_activation import (
                disable_skill as _disable_chat_skill,
                dismiss_suggestion as _dismiss_skill_suggestion,
                get_activation_snapshot as _get_skill_activation_snapshot,
                list_skill_choices as _list_chat_skill_choices,
                pin_skill as _pin_chat_skill,
                record_accept as _record_skill_accept,
                reset_thread as _reset_chat_skills,
                get_auto_loaded_skill_ids as _get_auto_loaded_skill_ids,
                load_auto_skill as _load_auto_skill,
                remove_auto_loaded_skill as _remove_auto_loaded_skill,
                suggest_skills as _suggest_chat_skills,
            )
            from row_bot.slash_commands import (
                SlashCommandSpec as _SlashCommandSpec,
                filter_command_specs as _filter_slash_commands,
                find_current_slash_token as _find_slash_token,
                get_command_specs as _get_slash_command_specs,
                help_text as _slash_help_text,
                remove_current_slash_token as _remove_slash_token,
                replace_current_slash_token as _replace_slash_token,
            )

            async def _load_skills_for_chips() -> None:
                await run.io_bound(_skills_mod.load_skills)

            if not _skills_mod.skills_loaded():
                defer_ui(_load_skills_for_chips)
            _skills_for_chips = _skills_mod
            _last_user_text = ""
            for _msg in reversed(state.messages or []):
                if _msg.get("role") == "user":
                    _last_user_text = str(_msg.get("content") or "")
                    break
            _enabled_tool_names = [t.name for t in tool_registry.get_enabled_tools()]
            _thread_override = get_thread_skills_override(state.thread_id)
            _skill_snap = _get_skill_activation_snapshot(
                state.thread_id,
                current_text="",
                enabled_tool_names=_enabled_tool_names,
                explicit_override=_thread_override,
            )
            from row_bot.skill_discovery import collect_enabled_skill_records

            _available_skills = collect_enabled_skill_records(load_manual=False)
            _available_skill_ids = {record.name for record in _available_skills}
            _active_skill_names = {"names": list(dict.fromkeys([
                *(
                    name for name in _skill_snap.active
                    if name in _available_skill_ids
                ),
                *(
                    name for name in _get_auto_loaded_skill_ids(state.thread_id)
                    if name in _available_skill_ids
                ),
            ]))}
            _draft_state = {"text": "", "version": 0, "suggestions_suppressed_text": ""}
            _chip_refresh_task: dict[str, asyncio.Task | None] = {"task": None}

            def _ordered_skill_names(names) -> list[str]:
                seen: set[str] = set()
                ordered: list[str] = []
                for name in names:
                    if name and name not in seen:
                        seen.add(name)
                        ordered.append(name)
                return ordered

            def _active_name_set() -> set[str]:
                return set(_active_skill_names.get("names") or [])

            def _get_skill_for_chips(name: str):
                for record in _available_skills:
                    if record.name == name:
                        return record
                return _skills_for_chips.get_skill(name)

            def _skill_source_label(skill) -> str:
                plugin_name = str(getattr(skill, "plugin_name", "") or "").strip()
                if plugin_name:
                    return f"Plugin: {plugin_name}"
                source = str(getattr(skill, "source", "") or "").strip()
                if source.startswith("plugin:"):
                    return f"Plugin: {source.removeprefix('plugin:')}"
                return "Manual"

            def _skill_draft_key(text: str) -> str:
                return re.sub(r"\s+", " ", str(text or "").strip()).lower()

            def _cancel_skill_chip_refresh_task() -> None:
                task = _chip_refresh_task.get("task")
                if task and not task.done():
                    task.cancel()

            def _suppress_skill_suggestions_for_current_draft() -> None:
                _draft_state["suggestions_suppressed_text"] = _skill_draft_key(
                    _draft_state.get("text", "")
                )
                _cancel_skill_chip_refresh_task()

            def _refresh_skill_chips_now() -> None:
                _render_skill_chips(_draft_state.get("text", ""))

            def _use_skill(name: str, *, source: str = "ui") -> None:
                if name.startswith("plugin:"):
                    _load_auto_skill(
                        state.thread_id,
                        name,
                        available_ids=_available_skill_ids,
                    )
                else:
                    _pin_chat_skill(state.thread_id, name)
                    _record_skill_accept(state.thread_id, name, source=source)
                _active_skill_names["names"] = _ordered_skill_names(
                    [*_active_skill_names.get("names", []), name]
                )
                clear_agent_cache()
                if source.startswith("ui"):
                    _suppress_skill_suggestions_for_current_draft()
                _refresh_skill_chips_now()

            def _remove_skill(name: str) -> None:
                if name in set(_get_auto_loaded_skill_ids(state.thread_id)):
                    _remove_auto_loaded_skill(state.thread_id, name)
                else:
                    _disable_chat_skill(state.thread_id, name)
                _active_skill_names["names"] = [
                    active_name
                    for active_name in _active_skill_names.get("names", [])
                    if active_name != name
                ]
                clear_agent_cache()
                _refresh_skill_chips_now()

            def _dismiss_suggestion(name: str) -> None:
                _dismiss_skill_suggestion(state.thread_id, name)
                _suppress_skill_suggestions_for_current_draft()
                _refresh_skill_chips_now()

            def _meaningful_skill_draft(text: str) -> bool:
                return len(str(text or "").strip()) >= 3

            def _suggestions_for_text(text: str, *, limit: int = 3):
                if not _meaningful_skill_draft(text):
                    return []
                if _skill_draft_key(text) == _draft_state.get("suggestions_suppressed_text"):
                    return []
                return _suggest_chat_skills(
                    state.thread_id,
                    text,
                    enabled_tool_names=_enabled_tool_names,
                    extra_excluded=_thread_override or [],
                    limit=limit,
                    trace=False,
                )

            def _open_skill_picker() -> None:
                picker_text = str(_draft_state.get("text") or "").strip() or _last_user_text
                picker_suggestions = _suggestions_for_text(picker_text, limit=3)
                picker_suggestions_by_name = {s.name: s for s in picker_suggestions}
                with ui.dialog() as dlg, ui.card().classes("w-full q-pa-md").style(
                    "min-width: min(720px, 92vw); max-width: 760px;"
                ):
                    with ui.row().classes("w-full items-center"):
                        ui.label("Skills").classes("text-h6")
                        ui.space()
                        ui.button(icon="close", on_click=dlg.close).props("flat round dense")
                    search = ui.input(
                        placeholder="Search skills",
                    ).props("dense outlined clearable").classes("w-full q-mb-sm")
                    skill_list = ui.column().classes("w-full gap-2").style("max-height: 58vh; overflow-y: auto;")

                    def _render_skill_list() -> None:
                        query = str(search.value or "").strip()
                        skill_list.clear()
                        ranked_choices = _list_chat_skill_choices(
                            state.thread_id,
                            query=query,
                            limit=None,
                        )
                        ranked_names = [choice.name for choice in ranked_choices]
                        for record in _available_skills:
                            search_text = f"{record.name} {record.display_name} {record.description}".lower()
                            if record.name not in ranked_names and (not query or query.lower() in search_text):
                                ranked_names.append(record.name)
                        ranked_name_set = set(ranked_names)

                        active_skills = [
                            _get_skill_for_chips(name)
                            for name in _active_skill_names.get("names", [])
                        ]
                        active_skills = [
                            sk for sk in active_skills
                            if sk and (not query or sk.name in ranked_name_set)
                        ]
                        suggested = [
                            s for s in picker_suggestions
                            if not query or s.name in ranked_name_set
                        ]
                        available_by_name = {
                            sk.name: sk
                            for sk in _available_skills
                            if sk.name not in _active_name_set()
                            and sk.name not in picker_suggestions_by_name
                        }
                        available = [
                            available_by_name[name]
                            for name in ranked_names
                            if name in available_by_name
                        ]

                        with skill_list:
                            if active_skills:
                                ui.label("Active in this chat").classes("text-xs text-grey-5 text-uppercase")
                                for sk in active_skills:
                                    with ui.row().classes("w-full items-center no-wrap q-pa-xs rounded-borders"):
                                        with ui.column().classes("gap-0"):
                                            ui.label(f"{sk.icon} {sk.display_name}").classes("text-sm text-weight-medium")
                                            ui.label(_skill_source_label(sk)).classes("text-xs text-grey-6")
                                        ui.space()
                                        ui.button("Remove", icon="close", on_click=lambda _, n=sk.name: (_remove_skill(n), dlg.close())).props(
                                            "flat dense no-caps size=sm"
                                        )
                            if suggested:
                                ui.label("Suggested").classes("text-xs text-grey-5 text-uppercase q-mt-sm")
                                for suggestion in suggested:
                                    with ui.row().classes("w-full items-center no-wrap q-pa-xs rounded-borders"):
                                        with ui.column().classes("gap-0"):
                                            ui.label(f"{suggestion.icon} {suggestion.display_name}").classes("text-sm text-weight-medium")
                                            ui.label(suggestion.reason).classes("text-xs text-grey-6")
                                        ui.space()
                                        ui.button("Use", icon="add", on_click=lambda _, n=suggestion.name: (_use_skill(n, source="ui_picker_suggested"), dlg.close())).props(
                                            "flat dense no-caps size=sm"
                                        )
                                        ui.button("Dismiss", icon="close", on_click=lambda _, n=suggestion.name: (_dismiss_suggestion(n), dlg.close())).props(
                                            "flat dense no-caps size=sm"
                                        )
                            ui.label("Available").classes("text-xs text-grey-5 text-uppercase q-mt-sm")
                            if available:
                                for sk in available:
                                    with ui.row().classes("w-full items-center no-wrap q-pa-xs rounded-borders"):
                                        with ui.column().classes("gap-0"):
                                            ui.label(f"{sk.icon} {sk.display_name}").classes("text-sm text-weight-medium")
                                            ui.label(_skill_source_label(sk)).classes("text-xs text-grey-6")
                                            if sk.description:
                                                ui.label(sk.description).classes("text-xs text-grey-6")
                                        ui.space()
                                        ui.button("Use", icon="add", on_click=lambda _, n=sk.name: (_use_skill(n, source="ui_picker"), dlg.close())).props(
                                            "flat dense no-caps size=sm"
                                        )
                            else:
                                ui.label("No available skills match.").classes("text-grey-6 text-sm")

                    search.on("update:model-value", lambda _: _render_skill_list())
                    _render_skill_list()
                dlg.open()

            _skill_chips_row = ui.row().classes("w-full flex-wrap items-center gap-1 q-px-md q-pt-xs")

            def _render_skill_chips(draft_text: str = "") -> None:
                try:
                    _skill_chips_row.clear()
                    draft_suggestions = _suggestions_for_text(draft_text, limit=3)
                    with _skill_chips_row:
                        ui.button("Skills", icon="auto_fix_high", on_click=_open_skill_picker).props(
                            "outline dense no-caps size=sm"
                        ).classes("text-xs").tooltip("Choose skills for this chat")

                        for _name in _active_skill_names.get("names", []):
                            _skill = _get_skill_for_chips(_name)
                            _label = f"{_skill.icon} {_skill.display_name}" if _skill else _name
                            ui.button(
                                _label,
                                icon="close",
                                on_click=lambda _, n=_name: _remove_skill(n),
                            ).props("outline dense no-caps size=sm").classes("text-xs").tooltip(
                                "Remove skill from this chat"
                            )

                        for _suggestion in draft_suggestions:
                            with ui.button(
                                f"{_suggestion.icon} {_suggestion.display_name}",
                            ).props("flat dense no-caps size=sm").classes("text-xs"):
                                with ui.menu().classes("q-pa-sm"):
                                    ui.label(_suggestion.description or _suggestion.reason).classes(
                                        "text-xs text-grey-5 q-mb-xs"
                                    )
                                    ui.button(
                                        "Use",
                                        icon="add",
                                        on_click=lambda _, n=_suggestion.name: _use_skill(n, source="ui_draft_suggestion"),
                                    ).props("flat dense no-caps size=sm")
                                    ui.button(
                                        "Dismiss",
                                        icon="close",
                                        on_click=lambda _, n=_suggestion.name: _dismiss_suggestion(n),
                                    ).props("flat dense no-caps size=sm")
                except Exception:
                    logger.debug("Smart Skills draft chip refresh failed", exc_info=True)

            def _refresh_skill_chips_from_store() -> None:
                latest = _get_skill_activation_snapshot(
                    state.thread_id,
                    current_text="",
                    enabled_tool_names=_enabled_tool_names,
                    explicit_override=get_thread_skills_override(state.thread_id),
                )
                _active_skill_names["names"] = _ordered_skill_names([
                    *(name for name in latest.active if name in _available_skill_ids),
                    *(
                        name for name in _get_auto_loaded_skill_ids(state.thread_id)
                        if name in _available_skill_ids
                    ),
                ])
                _render_skill_chips(_draft_state.get("text", ""))

            p.refresh_skill_chips = _refresh_skill_chips_from_store
            _render_skill_chips("")

            def _debounced_skill_chip_refresh(version: int) -> None:
                if version != _draft_state.get("version"):
                    return
                _render_skill_chips(_draft_state.get("text", ""))

            def _queue_skill_chip_refresh(text: str) -> None:
                _draft_state["text"] = str(text or "")
                _draft_state["version"] = int(_draft_state.get("version", 0)) + 1
                current_key = _skill_draft_key(_draft_state["text"])
                if current_key != _draft_state.get("suggestions_suppressed_text"):
                    _draft_state["suggestions_suppressed_text"] = ""
                _cancel_skill_chip_refresh_task()
                _chip_refresh_task["task"] = defer_ui(
                    lambda v=int(_draft_state["version"]): _debounced_skill_chip_refresh(v),
                    delay=0.25,
                )

            _slash_palette = {
                "open": False,
                "query": "",
                "index": 0,
                "items": [],
                "cursor": 0,
            }
            _slash_palette_client = ui.context.client
            _slash_palette_col = ui.column().classes("w-full gap-0 q-px-md q-pt-sm")

            def _set_slash_palette_flag(opened: bool) -> None:
                try:
                    _slash_palette_client.run_javascript(
                        f"window._rowBotSlashPaletteOpen = {str(bool(opened)).lower()};"
                    )
                except Exception:
                    logger.debug("Could not update slash palette browser flag", exc_info=True)

            def _set_composer_text(text: str, cursor: int | None = None) -> None:
                value = str(text or "")
                if p.chat_input:
                    p.chat_input.value = value
                    p.chat_input.update()
                _queue_skill_chip_refresh(value)
                cursor = len(value) if cursor is None else max(0, min(int(cursor), len(value)))
                if p.chat_input:
                    payload = json.dumps({"id": p.chat_input.id, "cursor": cursor})
                    ui.run_javascript(
                        f"""(function(p) {{
                            const root = document.getElementById('c' + p.id);
                            const input = root && root.querySelector('textarea');
                            if (!input) return;
                            input.focus();
                            try {{ input.setSelectionRange(p.cursor, p.cursor); }} catch (_) {{}}
                        }})({payload});"""
                    )

            def _close_slash_palette() -> None:
                _slash_palette["open"] = False
                _slash_palette["items"] = []
                _set_slash_palette_flag(False)
                _slash_palette_col.clear()

            def _show_text_dialog(title: str, content: str, *, icon: str = "info") -> None:
                def _normalize_dialog_markdown(raw: str) -> str:
                    lines = str(raw or "").splitlines()
                    normalized: list[str] = []
                    for idx, line in enumerate(lines):
                        stripped = line.strip()
                        if stripped.startswith("**") and stripped.endswith("**"):
                            if normalized and normalized[-1] != "":
                                normalized.append("")
                            normalized.append(stripped)
                            next_line = lines[idx + 1].strip() if idx + 1 < len(lines) else ""
                            if next_line.startswith("- "):
                                normalized.append("")
                            continue
                        normalized.append(line)
                    return "\n".join(normalized)

                with ui.dialog() as dlg, ui.card().classes("q-pa-md").style(
                    "min-width: min(680px, 92vw); max-width: 760px;"
                ):
                    with ui.row().classes("w-full items-center gap-2"):
                        ui.icon(icon, size="sm")
                        ui.label(title).classes("text-h6")
                        ui.space()
                        ui.button(icon="close", on_click=dlg.close).props("flat round dense")
                    ui.separator()
                    ui.markdown(
                        _normalize_dialog_markdown(content),
                        extras=['code-friendly', 'fenced-code-blocks', 'tables'],
                    ).classes("w-full text-sm").style("max-height: 62vh; overflow-y: auto;")
                dlg.open()

            def _append_system_result(title: str, content: str, *, icon: str = "info") -> None:
                _show_text_dialog(title, content, icon=icon)

            async def _new_thread_from_palette() -> None:
                from row_bot.ui.voice_lifecycle import stop_voice_for_thread_change

                tid = uuid.uuid4().hex[:12]
                name = f"Thread {datetime.now().strftime('%b %d, %H:%M')}"
                await run.io_bound(lambda: create_thread(name, thread_id=tid))
                stop_voice_for_thread_change(state, p, reason="slash_new_thread")
                prev = state.thread_id
                prev_gen = _active_generations.get(prev) if prev else None
                if prev_gen and prev_gen.status == "streaming":
                    prev_gen.detached = True
                    if prev_gen.tts_active:
                        state.tts_service.stop()
                        prev_gen.tts_active = False
                state.active_designer_project = None
                state.active_developer_workspace_id = None
                state.thread_id = tid
                state.thread_name = name
                state.messages = []
                state.thread_model_override = ""
                p.pending_files.clear()
                try:
                    from row_bot.memory_extraction import set_active_thread

                    set_active_thread(tid, previous_id=prev)
                except Exception:
                    logger.debug("Could not set active thread for command palette /new", exc_info=True)
                rebuild_main(immediate=True, reason="slash_new")
                rebuild_thread_list()

            def _reset_skills_from_palette() -> None:
                _reset_chat_skills(state.thread_id)
                _reset_snap = _get_skill_activation_snapshot(
                    state.thread_id,
                    current_text="",
                    enabled_tool_names=_enabled_tool_names,
                    explicit_override=_thread_override,
                )
                _active_skill_names["names"] = list(_reset_snap.active)
                clear_agent_cache()
                _refresh_skill_chips_now()
                ui.notify("Skills reset for this chat.", type="info")

            def _run_stop_from_palette() -> None:
                gen = _active_generations.get(state.thread_id)
                if gen:
                    gen.stop_event.set()
                    if p.stop_btn:
                        p.stop_btn.props('icon=hourglass_top')
                    ui.notify("Stop signal sent.", type="warning")
                else:
                    ui.notify("No active generation to stop.", type="info")

            def _remove_token_and_close() -> None:
                text = str(p.chat_input.value or "") if p.chat_input else _draft_state.get("text", "")
                new_text, cursor = _remove_slash_token(text, _slash_palette.get("cursor"))
                _set_composer_text(new_text, cursor)
                _close_slash_palette()

            def _replace_token_with_prefix(prefix: str) -> None:
                text = str(p.chat_input.value or "") if p.chat_input else _draft_state.get("text", "")
                new_text, cursor = _replace_slash_token(text, _slash_palette.get("cursor"), prefix)
                _set_composer_text(new_text, cursor)
                _close_slash_palette()

            def _execute_slash_spec(spec: _SlashCommandSpec) -> None:
                with _slash_palette_client:
                    _execute_slash_spec_in_client(spec)

            def _execute_slash_spec_in_client(spec: _SlashCommandSpec) -> None:
                if spec.handler_key == "activate_skill" and spec.skill_name:
                    _use_skill(spec.skill_name, source="slash_palette")
                    _remove_token_and_close()
                    ui.notify(f"Skill active: {spec.title}", type="positive")
                    return
                if spec.handler_key == "open_skills":
                    _remove_token_and_close()
                    _open_skill_picker()
                    return
                if spec.handler_key == "skill_reset":
                    _remove_token_and_close()
                    _reset_skills_from_palette()
                    return
                if spec.handler_key == "noskill":
                    active = _active_skill_names.get("names", [])
                    if len(active) == 1:
                        _remove_skill(active[0])
                        _remove_token_and_close()
                        ui.notify(f"Removed skill: {active[0]}", type="info")
                    else:
                        _replace_token_with_prefix("/noskill ")
                    return
                if spec.handler_key == "new_thread":
                    _remove_token_and_close()
                    asyncio.create_task(_new_thread_from_palette())
                    return
                if spec.handler_key == "stop_generation":
                    _remove_token_and_close()
                    _run_stop_from_palette()
                    return
                if spec.handler_key == "export":
                    _remove_token_and_close()
                    open_export()
                    return
                if spec.handler_key == "status":
                    _remove_token_and_close()
                    from row_bot.tools.row_bot_status_tool import _row_bot_status

                    _append_system_result("Status", _row_bot_status("overview"))
                    return
                if spec.handler_key == "tools":
                    _remove_token_and_close()
                    from row_bot.tools.row_bot_status_tool import _row_bot_status

                    _append_system_result("Tools", _row_bot_status("tools"))
                    return
                if spec.handler_key == "profiles":
                    _remove_token_and_close()
                    from row_bot.agent_commands import format_agent_profiles

                    _append_system_result("Agent Profiles", format_agent_profiles(), icon="badge")
                    return
                if spec.handler_key == "profile":
                    _replace_token_with_prefix("/profile ")
                    return
                if spec.handler_key == "agents":
                    _remove_token_and_close()
                    from row_bot.agent_commands import format_agents_status

                    _append_system_result(
                        "Agents",
                        format_agents_status(parent_thread_id=state.thread_id),
                        icon="hub",
                    )
                    return
                if spec.handler_key == "goal":
                    _replace_token_with_prefix("/goal ")
                    return
                if spec.handler_key == "help":
                    _remove_token_and_close()
                    _append_system_result("Slash Commands", _slash_help_text(include_skills=True), icon="help")
                    return
                _replace_token_with_prefix(spec.slash + " ")

            def _render_slash_palette() -> None:
                _slash_palette_col.clear()
                items = list(_slash_palette.get("items") or [])
                if not _slash_palette.get("open") or not items:
                    _set_slash_palette_flag(False)
                    return
                _set_slash_palette_flag(True)
                selected_index = int(_slash_palette.get("index", 0) or 0)
                selected_row_id: int | None = None
                with _slash_palette_col:
                    with ui.column().classes("w-full gap-0 row-bot-slash-palette-list").style(
                        "max-height: 270px; overflow-y: auto; "
                        "border: 1px solid rgba(255,255,255,0.14); "
                        "border-radius: 8px; background: rgba(18,18,28,0.98); "
                        "box-shadow: 0 12px 28px rgba(0,0,0,0.35);"
                    ):
                        for idx, spec in enumerate(items):
                            selected = idx == selected_index
                            bg = "rgba(66, 165, 245, 0.18)" if selected else "transparent"
                            row = ui.row().classes(
                                "w-full items-center no-wrap gap-2 cursor-pointer row-bot-slash-palette-row"
                                + (" row-bot-slash-palette-row-selected" if selected else "")
                            ).style(
                                f"padding: 7px 10px; background: {bg}; "
                                "border-radius: 6px; min-height: 42px;"
                            )
                            if selected:
                                selected_row_id = row.id
                            row.on(
                                "mousedown",
                                lambda _e, s=spec: _execute_slash_spec(s),
                                js_handler="""(e) => {
                                    e.preventDefault();
                                    e.stopPropagation();
                                    emit();
                                }""",
                            )
                            with row:
                                if spec.icon and re.match(r"^[a-z0-9_]+$", spec.icon):
                                    ui.icon(spec.icon, size="sm").classes("text-grey-5")
                                else:
                                    ui.label(spec.icon or "*").classes("text-sm")
                                with ui.column().classes("gap-0").style("min-width: 0; flex: 1;"):
                                    with ui.row().classes("items-center gap-2 no-wrap"):
                                        ui.label(spec.slash).classes("text-sm text-weight-medium")
                                        ui.label(spec.title).classes("text-xs text-grey-5 ellipsis")
                                    ui.label(spec.description).classes("text-xs text-grey-6 ellipsis")
                                ui.label(spec.category).classes("text-xs text-grey-7")
                if selected_row_id is not None:
                    _slash_palette_client.run_javascript(
                        f"""setTimeout(() => {{
                            const row = document.getElementById('c{selected_row_id}');
                            if (row) row.scrollIntoView({{block: 'nearest'}});
                        }}, 0);"""
                    )

            def _slash_palette_on_text(text: str, cursor: int | None = None) -> None:
                current = str(text or "")
                _draft_state["text"] = current
                found = _find_slash_token(current, cursor if cursor is not None else len(current))
                if found is None:
                    if _slash_palette.get("open"):
                        _close_slash_palette()
                    return
                _start, _end, query = found
                specs = _get_slash_command_specs(include_skills=True)
                items = _filter_slash_commands(specs, query, limit=max(len(specs), 1))
                if not items:
                    _close_slash_palette()
                    return
                _slash_palette.update({
                    "open": True,
                    "query": query,
                    "index": min(int(_slash_palette.get("index", 0) or 0), len(items) - 1),
                    "items": items,
                    "cursor": cursor if cursor is not None else len(current),
                })
                _render_slash_palette()

            def _slash_palette_move(delta: int) -> None:
                if not _slash_palette.get("open"):
                    return
                items = list(_slash_palette.get("items") or [])
                if not items:
                    _close_slash_palette()
                    return
                _slash_palette["index"] = (int(_slash_palette.get("index", 0) or 0) + delta) % len(items)
                _render_slash_palette()

            def _slash_palette_close() -> None:
                _close_slash_palette()

            def _slash_palette_pick_selected() -> None:
                if not _slash_palette.get("open"):
                    return
                items = list(_slash_palette.get("items") or [])
                if not items:
                    _close_slash_palette()
                    return
                index = int(_slash_palette.get("index", 0) or 0)
                _execute_slash_spec(items[max(0, min(index, len(items) - 1))])

            def _slash_palette_handle_key(key: str) -> None:
                if not _slash_palette.get("open"):
                    return
                normalized = str(key or "")
                if normalized == "ArrowDown":
                    _slash_palette_move(1)
                elif normalized == "ArrowUp":
                    _slash_palette_move(-1)
                elif normalized in {"Enter", "Tab"}:
                    _slash_palette_pick_selected()
                elif normalized == "Escape":
                    _slash_palette_close()

        except Exception:
            logger.debug("Smart Skills composer chips failed to render", exc_info=True)

        create_context_meter(p, state)

        # Textarea
        p.chat_input = (
            ui.textarea(placeholder="Ask anything...")
            .classes("w-full")
            .props('borderless autogrow input-style="padding: 12px 16px 4px 16px; max-height: 200px; overflow-y: auto;"')
            .style("font-size: 0.95rem;")
        )

        def _clear_persisted_thread_draft() -> None:
            try:
                from row_bot.threads import delete_thread_draft

                delete_thread_draft(str(state.thread_id or ""))
            except Exception:
                logger.debug("Could not clear persisted thread draft", exc_info=True)

        try:
            from row_bot.threads import load_thread_draft

            draft = load_thread_draft(str(state.thread_id or ""))
            draft_text = str((draft or {}).get("text") or "")
            if draft_text and not str(p.chat_input.value or "").strip():
                p.chat_input.value = draft_text
                p.chat_input.update()
                _draft_state["text"] = draft_text
                _queue_skill_chip_refresh(draft_text)
                try:
                    p.chat_input.run_method("focus")
                except Exception:
                    pass
        except Exception:
            logger.debug("Could not restore persisted thread draft", exc_info=True)

        def _register_active_voice_binding() -> None:
            from row_bot.voice.actions import ActiveVoiceSurfaceBinding

            thread_id = str(state.thread_id or "")

            def _get_text() -> str:
                return str(p.chat_input.value or "") if p.chat_input is not None else ""

            def _set_text(value: str) -> None:
                if p.chat_input is None:
                    return
                p.chat_input.value = value
                p.chat_input.update()
                try:
                    _queue_skill_chip_refresh(value)
                except Exception:
                    logger.debug("Smart Skills draft suggestions were not synced for voice text", exc_info=True)

            p.active_voice_binding = ActiveVoiceSurfaceBinding(
                surface="normal_chat",
                thread_id=thread_id,
                get_composer_text=_get_text,
                set_composer_text=_set_text,
                send_talk_text=send_message,
            )
            logger.info(
                "voice.realtime.pipeline %s",
                {
                    "stage": "active_voice_surface_bound",
                    "surface": "normal_chat",
                    "thread_id": thread_id,
                },
            )

        _register_active_voice_binding()

        try:
            def _on_composer_value(e) -> None:
                payload = e.args
                if isinstance(payload, dict):
                    text = str(payload.get("value") or "")
                    cursor = payload.get("cursor")
                else:
                    text = str(payload or p.chat_input.value or "")
                    cursor = len(text)
                _queue_skill_chip_refresh(text)
                try:
                    _slash_palette_on_text(text, int(cursor) if cursor is not None else len(text))
                except Exception:
                    logger.debug("Slash command palette update failed", exc_info=True)

            p.chat_input.on(
                "update:model-value",
                _on_composer_value,
                js_handler="""(value) => {
                    const el = this.$refs?.qRef?.nativeEl || this.$el?.querySelector('textarea');
                    emit({value, cursor: el ? el.selectionStart : String(value || '').length});
                }""",
            )
        except Exception:
            logger.debug("Smart Skills draft suggestion handler was not attached", exc_info=True)

        p.chat_input.on(
            "keydown",
            lambda e: _slash_palette_handle_key(e.args.get("key") if isinstance(e.args, dict) else ""),
            js_handler="""(e) => {
                if (!window._rowBotSlashPaletteOpen) return;
                if (!['ArrowDown', 'ArrowUp', 'Enter', 'Tab', 'Escape'].includes(e.key)) return;
                e.preventDefault();
                e.stopPropagation();
                emit({key: e.key});
            }""",
        )

        async def _on_send():
            text = p.chat_input.value
            if text and text.strip():
                p.chat_input.value = ""
                _clear_persisted_thread_draft()
                try:
                    _queue_skill_chip_refresh("")
                except Exception:
                    logger.debug("Smart Skills draft suggestions were not cleared on send", exc_info=True)
                # Re-engage auto-scroll on new message
                if p.chat_scroll:
                    _re = p.chat_scroll.id
                    ui.run_javascript(
                        f"(function(){{ var e=getElement({_re}); if(e) e._tSS=true; }})()"
                    )
                await send_message(text)
            elif p.pending_files:
                p.chat_input.value = ""
                _clear_persisted_thread_draft()
                try:
                    _queue_skill_chip_refresh("")
                except Exception:
                    logger.debug("Smart Skills draft suggestions were not cleared on attachment send", exc_info=True)
                await send_message("")

        # Enter to send; modified Enter keeps native textarea behavior.
        p.chat_input.on(
            "keydown.enter",
            _on_send,
            js_handler="""(e) => {
                if (window._rowBotSlashPaletteOpen) return;
                if (e.shiftKey || e.ctrlKey || e.metaKey || e.altKey) return;
                e.preventDefault();
                emit();
            }""",
        )

        def _on_stop():
            request_generation_stop(state.thread_id, state=state, p=p, reason="desktop_chat")

        # Bottom bar inside card: attach, model/approval, voice, spacer, send, stop
        with ui.row().classes("w-full row-bot-composer-toolbar q-px-sm q-pb-sm q-pt-none gap-1"):
            ui.button(icon="attach_file", on_click=_on_attach).props(
                "flat round dense size=sm"
            ).classes("row-bot-composer-icon-button").tooltip("Attach files")

            ui.element("div").classes("row-bot-composer-left-gap")

            from row_bot.ui.chat_components import (
                _set_dictate_button_active,
                _set_talk_button_active,
                build_composer_policy_cluster,
                ensure_composer_control_css,
            )
            ensure_composer_control_css()

            def _render_model_controls() -> None:
                build_composer_policy_cluster(
                    state,
                    open_settings=open_settings,
                    on_model_switch=_refresh_model_surface,
                    generation_getter=lambda: p.chat_shell_generation,
                    shell_generation=_shell_generation,
                )

            def _refresh_model_controls() -> None:
                _refresh_model_surface()
                container = getattr(p, "model_controls_container", None)
                if container is None:
                    return
                try:
                    container.clear()
                    with container:
                        _render_model_controls()
                except Exception:
                    logger.debug("Composer model controls refresh failed", exc_info=True)

            p.refresh_model_controls = _refresh_model_controls
            p.model_controls_container = ui.row().classes("items-center no-wrap gap-1")
            with p.model_controls_container:
                _render_model_controls()
            from row_bot.ui.voice_realtime_events import make_realtime_event_handler

            _on_realtime_event = make_realtime_event_handler(
                state=state,
                p=p,
                send_message=send_message,
            )


            p.realtime_event_sink = ui.element("div").style("display:none")
            try:
                p.realtime_client = ui.context.client
            except Exception:
                p.realtime_client = None
            p.realtime_event_sink.on(
                "row-bot-realtime-event",
                _on_realtime_event,
                js_handler="(e) => emit(e.detail)",
            )

            async def _on_browser_voice_transcript(e) -> None:
                payload = e.args if isinstance(e.args, dict) else {}
                text = str(payload.get("text") or "").strip()
                mode = str(payload.get("mode") or "talk")
                if not text:
                    return
                if mode == "dictate":
                    p.chat_input.value = text
                    p.chat_input.update()
                    state.voice_enabled = False
                    state.voice_coordinator.stop()
                    if p.dictate_btn:
                        _set_dictate_button_active(p, False)
                    return
                await send_message(text, voice_mode=True)

            def _on_browser_voice_error(e) -> None:
                payload = e.args if isinstance(e.args, dict) else {}
                code = str(payload.get("code") or "microphone_unavailable")
                messages = {
                    "secure_context_required": "Browser microphone access requires HTTPS (localhost is allowed for development).",
                    "whisper_model_missing": "Install the local Whisper model in Voice settings before using browser voice.",
                    "NotAllowedError": "Microphone permission was denied by the browser.",
                }
                ui.notify(
                    messages.get(code, "Browser voice could not start."),
                    type="warning",
                    close_button=True,
                )
                state.voice_enabled = False
                state.voice_coordinator.stop()
                if p.voice_switch:
                    _set_talk_button_active(p, False)
                if p.dictate_btn:
                    _set_dictate_button_active(p, False)

            def _on_browser_voice_state(e) -> None:
                payload = e.args if isinstance(e.args, dict) else {}
                if p.voice_status_label and payload.get("state") == "recording":
                    p.voice_status_label.text = "Listening in this browser…"

            p.browser_voice_event_sink = ui.element("div").style("display:none")
            p.browser_voice_event_sink.on(
                "row-bot-browser-voice-transcript",
                _on_browser_voice_transcript,
                js_handler="(e) => emit(e.detail)",
            )
            p.browser_voice_event_sink.on(
                "row-bot-browser-voice-error",
                _on_browser_voice_error,
                js_handler="(e) => emit(e.detail)",
            )
            p.browser_voice_event_sink.on(
                "row-bot-browser-voice-state",
                _on_browser_voice_state,
                js_handler="(e) => emit(e.detail)",
            )

            def _browser_voice_required() -> bool:
                from row_bot.access.config import DeploymentMode
                from row_bot.ui.access_context import current_access_context

                context = current_access_context()
                return bool(
                    context
                    and (
                        context.deployment_mode is DeploymentMode.SERVER
                        or not context.is_local_owner
                    )
                )

            def _start_browser_voice(mode: str) -> None:
                from row_bot.ui.streaming import run_realtime_client_js
                from row_bot.voice.browser_client import start_browser_voice_capture_js

                _register_active_voice_binding()
                state.voice_input_mode = "dictate" if mode == "dictate" else "talk"
                state.voice_enabled = True
                state.voice_coordinator.start_browser(state.voice_input_mode)
                delivered = run_realtime_client_js(
                    p,
                    start_browser_voice_capture_js(
                        sink_id=p.browser_voice_event_sink.id,
                        mode=state.voice_input_mode,
                    ),
                    context=f"start_browser_{state.voice_input_mode}",
                )
                if not delivered:
                    state.voice_enabled = False
                    state.voice_coordinator.stop()

            def _start_local_talk() -> None:
                if _browser_voice_required():
                    _start_browser_voice("talk")
                    return
                _register_active_voice_binding()
                state.voice_input_mode = "talk"
                state.voice_enabled = True
                state.voice_coordinator.start_talk()
                if p.dictate_btn:
                    _set_dictate_button_active(p, False)

            def _start_realtime_talk() -> None:
                from row_bot.voice.openai_realtime import OpenAIRealtimeProvider
                from row_bot.voice.realtime_client import start_realtime_client_js
                from row_bot.ui.streaming import run_realtime_client_js

                status = OpenAIRealtimeProvider().status()
                if not status.ready:
                    if state.voice_runtime_settings.realtime_fallback_to_local:
                        ui.notify("OpenAI Realtime is not configured. Falling back to local Talk.", type="warning")
                        _start_local_talk()
                    else:
                        ui.notify(status.reason, type="negative", close_button=True)
                        state.voice_enabled = False
                        if p.voice_switch:
                            _set_talk_button_active(p, False)
                    return
                state.voice_input_mode = "talk"
                state.voice_enabled = True
                _register_active_voice_binding()
                session_id = state.voice_coordinator.start_realtime_talk()
                if p.dictate_btn:
                    _set_dictate_button_active(p, False)
                delivered = run_realtime_client_js(
                    p,
                    start_realtime_client_js(
                        sink_id=p.realtime_event_sink.id,
                        session_id=session_id,
                    ),
                    context="start_realtime_talk",
                )
                if not delivered:
                    state.voice_enabled = False
                    state.voice_coordinator.stop()
                    if p.voice_switch:
                        _set_talk_button_active(p, False)

            def _stop_talk() -> None:
                from row_bot.voice.realtime_client import stop_realtime_client_js
                from row_bot.ui.streaming import run_realtime_client_js

                if state.voice_coordinator.transport == "realtime":
                    run_realtime_client_js(p, stop_realtime_client_js(), context="stop_realtime_talk")
                elif state.voice_coordinator.transport == "browser":
                    from row_bot.voice.browser_client import stop_browser_voice_js

                    run_realtime_client_js(
                        p,
                        stop_browser_voice_js(cancel=True),
                        context="stop_browser_talk",
                    )
                state.voice_enabled = False
                state.voice_coordinator.stop()
                binding = getattr(p, "active_voice_binding", None)
                if binding is not None:
                    binding.clear()
                p.active_voice_binding = None

            def _toggle_voice():
                if not (state.voice_enabled and state.voice_input_mode == "talk"):
                    if state.voice_runtime_settings.talk_provider == "openai_realtime":
                        _start_realtime_talk()
                    else:
                        _start_local_talk()
                    _set_talk_button_active(p, state.voice_enabled and state.voice_input_mode == "talk")
                elif state.voice_input_mode == "talk":
                    _stop_talk()
                    _set_talk_button_active(p, False)
            def _toggle_dictate():
                if state.voice_enabled and state.voice_input_mode == "dictate":
                    if state.voice_coordinator.transport == "browser":
                        from row_bot.ui.streaming import run_realtime_client_js
                        from row_bot.voice.browser_client import stop_browser_voice_js

                        run_realtime_client_js(
                            p,
                            stop_browser_voice_js(cancel=True),
                            context="stop_browser_dictation",
                        )
                    state.voice_enabled = False
                    state.voice_coordinator.stop()
                    binding = getattr(p, "active_voice_binding", None)
                    if binding is not None:
                        binding.clear()
                    p.active_voice_binding = None
                    if p.dictate_btn:
                        _set_dictate_button_active(p, False)
                    return
                state.voice_input_mode = "dictate"
                state.voice_enabled = True
                _register_active_voice_binding()
                if _browser_voice_required():
                    _start_browser_voice("dictate")
                else:
                    state.voice_coordinator.start_dictation()
                if p.voice_switch:
                    _set_talk_button_active(p, False)
                if p.dictate_btn:
                    _set_dictate_button_active(p, True)

            p.voice_status_label = ui.label("").classes("text-xs text-grey-6")

            ui.space()  # push right-side items to the right

            with ui.row().classes("items-center row-bot-composer-action-group"):
                with ui.row().classes("items-center row-bot-composer-voice-group"):
                    p.voice_switch = ui.button(icon="record_voice_over", on_click=_toggle_voice).props(
                        "flat round dense size=sm"
                    ).classes("row-bot-composer-icon-button").tooltip("Talk")
                    p.voice_switch.value = False
                    _set_talk_button_active(p, state.voice_enabled and state.voice_input_mode == "talk")
                    p.dictate_btn = ui.button(icon="keyboard_voice", on_click=_toggle_dictate).props(
                        "flat round dense size=sm"
                    ).classes("row-bot-composer-icon-button").tooltip("Dictate into the composer")
                    p.dictate_btn.value = False
                    _set_dictate_button_active(p, state.voice_enabled and state.voice_input_mode == "dictate")

                ui.element("div").classes("row-bot-composer-action-divider")

                ui.button(icon="send", on_click=_on_send).props(
                    "color=primary round dense size=sm"
                ).classes("row-bot-composer-send-button").tooltip("Send")

                p.stop_btn = ui.button(icon="stop", on_click=_on_stop).props(
                    "round dense size=sm"
                ).classes("row-bot-composer-stop-button").tooltip("Stop generation")
            _has_active = state.thread_id in _active_generations
            if not _has_active:
                p.stop_btn.disable()

    log_ui_perf(
        "chat.composer.render",
        (time.perf_counter() - _composer_started) * 1000.0,
        threshold_ms=200.0,
        thread_id=state.thread_id,
    )
    log_ui_perf(
        "chat.shell.render",
        (time.perf_counter() - _shell_started) * 1000.0,
        threshold_ms=500.0,
        thread_id=state.thread_id,
        rows=len(state.messages),
    )
