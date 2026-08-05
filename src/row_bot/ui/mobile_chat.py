"""Mobile-native chat list, detail, and composer surfaces."""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from nicegui import ui

from row_bot.approval_policy import DEFAULT_APPROVAL_MODE, normalize_approval_mode
from row_bot.memory_extraction import set_active_thread
from row_bot.threads import (
    _clear_thread_agent_profile,
    _get_thread_approval_mode,
    _get_thread_agent_profile,
    _get_thread_model_override,
    _list_threads,
    _set_thread_approval_mode,
    _set_thread_agent_profile,
    _set_thread_model_override,
    create_thread,
    get_thread_name,
)
from row_bot.ui.chat_components import build_chat_messages, build_file_upload
from row_bot.ui.chat_composer_extras import create_chat_composer_extras
from row_bot.ui.streaming import request_generation_stop
from row_bot.ui.state import AppState, P, _active_generations
from row_bot.ui.voice_lifecycle import stop_voice_for_thread_change

logger = logging.getLogger(__name__)


def set_mobile_chat_mode(state: AppState, mode: str) -> None:
    """Set the mobile chat mode while keeping the active thread intact."""
    normalized = "thread" if mode == "thread" else "threads"
    setattr(state, "mobile_chat_mode", normalized)


def mobile_chat_mode(state: AppState) -> str:
    mode = str(getattr(state, "mobile_chat_mode", "") or "")
    if mode not in {"threads", "thread"}:
        mode = "thread" if state.thread_id else "threads"
        set_mobile_chat_mode(state, mode)
    if mode == "thread" and not state.thread_id:
        mode = "threads"
        set_mobile_chat_mode(state, mode)
    return mode


def format_mobile_timestamp(value: Any) -> str:
    """Return a compact timestamp label for mobile thread rows."""
    raw = str(value or "").strip()
    if not raw:
        return "No activity yet"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw[:19].replace("T", " ")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc).astimezone()
    else:
        parsed = parsed.astimezone()
    now = datetime.now(parsed.tzinfo)
    if parsed.date() == now.date():
        return f"Today, {parsed:%H:%M}"
    days = (now.date() - parsed.date()).days
    if days == 1:
        return f"Yesterday, {parsed:%H:%M}"
    if parsed.year == now.year:
        return parsed.strftime("%b %d, %H:%M")
    return parsed.strftime("%Y-%m-%d")


def open_thread_on_mobile(
    thread_id: str,
    *,
    state: AppState,
    p: P,
    load_thread_messages: Callable[[str], list[dict]],
    rebuild_main: Callable[..., None],
) -> None:
    """Open a thread in the mobile detail view."""
    target = str(thread_id or "").strip()
    if not target:
        return
    previous = state.thread_id
    stop_voice_for_thread_change(state, p, reason="mobile_thread_change")
    state.active_designer_project = None
    state.active_developer_workspace_id = None
    state.thread_id = target
    state.thread_name = get_thread_name(target) or "Untitled"
    state.thread_model_override = _get_thread_model_override(target)
    state.thread_approval_mode = _get_thread_approval_mode(target)
    state.messages = load_thread_messages(target)
    p.pending_files.clear()
    set_active_thread(target, previous_id=previous)
    set_mobile_chat_mode(state, "thread")
    setattr(state, "mobile_view", "Chat")
    rebuild_main(immediate=True, reason="mobile_open_thread")


def create_mobile_thread(
    *,
    state: AppState,
    p: P,
    load_thread_messages: Callable[[str], list[dict]],
    rebuild_main: Callable[..., None],
) -> str:
    """Create and open an empty mobile chat thread before composing."""
    previous = state.thread_id
    stop_voice_for_thread_change(state, p, reason="mobile_new_thread")
    previous_generation = _active_generations.get(previous) if previous else None
    if previous_generation and getattr(previous_generation, "status", "") == "streaming":
        previous_generation.detached = True
        if getattr(previous_generation, "tts_active", False):
            try:
                state.tts_service.stop()
            except Exception:
                logger.debug("Could not stop TTS while opening mobile thread", exc_info=True)
            previous_generation.tts_active = False
    tid = uuid.uuid4().hex[:12]
    name = f"\U0001f4f1 Thread {datetime.now().strftime('%b %d, %H:%M')}"
    approval = normalize_approval_mode(
        getattr(state, "thread_approval_mode", "") or DEFAULT_APPROVAL_MODE,
        DEFAULT_APPROVAL_MODE,
    )
    create_thread(name, thread_id=tid, approval_mode=approval)
    state.active_designer_project = None
    state.active_developer_workspace_id = None
    state.thread_id = tid
    state.thread_name = name
    state.thread_model_override = ""
    state.thread_approval_mode = approval
    state.messages = load_thread_messages(tid)
    p.pending_files.clear()
    set_active_thread(tid, previous_id=previous)
    set_mobile_chat_mode(state, "thread")
    setattr(state, "mobile_view", "Chat")
    rebuild_main(immediate=True, reason="mobile_new_thread")
    return tid


def _model_options_for_mobile(state: AppState) -> dict[str, str]:
    from row_bot.providers.selection import list_model_choice_options

    include_values = []
    if state.thread_model_override:
        include_values.append(state.thread_model_override)
    options: dict[str, str] = {"__default__": "Default"}
    try:
        for option in list_model_choice_options("chat", include_values=include_values, include_inactive=True):
            value = str(option.get("value") or "")
            if value:
                options[value] = str(option.get("label") or value)
    except Exception:
        logger.debug("Could not load mobile chat model options", exc_info=True)
    if state.thread_model_override and state.thread_model_override not in options:
        options[state.thread_model_override] = state.thread_model_override
    return options


def _current_model_label(state: AppState) -> str:
    if state.thread_model_override:
        return state.thread_model_override.replace("model:", "")
    return f"Default - {str(state.current_model or '').replace('model:', '')}"


def _compact_model_label(state: AppState) -> str:
    label = _current_model_label(state)
    if " - " in label:
        label = label.split(" - ", 1)[-1]
    return label


def _model_policy_summary(state: AppState) -> tuple[str, str, str]:
    try:
        from row_bot.models import is_cloud_model

        model_value = state.thread_model_override or state.current_model
        if is_cloud_model(model_value):
            return "cloud", "Cloud", "Messages may be sent to the selected provider."
    except Exception:
        logger.debug("Could not resolve mobile model policy", exc_info=True)
    return "computer", "Local/default", "Thread uses local or default model policy."


def _agent_profile_options_for_mobile() -> dict[str, str]:
    options = {"__default__": "Default agent profile"}
    try:
        from row_bot.agent_profiles import list_agent_profiles

        for profile in list_agent_profiles(enabled_only=True, include_builtins=True):
            profile_id = str(profile.get("id") or "").strip()
            if not profile_id:
                continue
            options[profile_id] = str(profile.get("display_name") or profile.get("slug") or profile_id)
    except Exception:
        logger.debug("Could not load mobile agent profile options", exc_info=True)
    return options


def _current_agent_profile_value(state: AppState) -> str:
    if not state.thread_id:
        return "__default__"
    try:
        pointer = _get_thread_agent_profile(state.thread_id)
    except Exception:
        logger.debug("Could not read mobile thread agent profile", exc_info=True)
        return "__default__"
    return str(pointer.get("id") or pointer.get("slug") or "__default__")


def _current_agent_profile_label(state: AppState) -> str:
    value = _current_agent_profile_value(state)
    if value == "__default__":
        return "Default profile"
    try:
        from row_bot.agent_profiles import get_agent_profile

        profile = get_agent_profile(value, enabled_only=False)
        if profile:
            return str(profile.get("display_name") or profile.get("slug") or value)
    except Exception:
        logger.debug("Could not resolve mobile agent profile label", exc_info=True)
    return value


def _stop_generation(state: AppState, p: P | None = None) -> None:
    result = request_generation_stop(state.thread_id, state=state, p=p, reason="mobile_chat")
    ui.notify(
        "Stop signal sent" if result.stopped else "No active generation to stop",
        type="warning" if result.stopped else "info",
    )


def _mobile_generation_active(state: AppState) -> bool:
    thread_id = state.thread_id or ""
    generation = _active_generations.get(thread_id)
    status = str(getattr(generation, "status", "") or "")
    return status in {"streaming", "queued", "paused"}


def _build_file_chips(p: P) -> None:
    p.file_chips_row = ui.row().classes("row-bot-mobile-file-chips w-full flex-wrap gap-1")
    if not p.pending_files:
        return
    for index, item in enumerate(list(p.pending_files)):
        name = str(item.get("name") or "Attachment")

        def _remove(i: int = index, badge: Any = None) -> None:
            if i < len(p.pending_files):
                p.pending_files.pop(i)
            if badge is not None:
                badge.delete()

        badge = ui.badge(f"{name} x", color="grey-8").props("outline")
        badge.on("click", lambda _e=None, i=index, b=badge: _remove(i, b))
        badge.style("cursor: pointer; max-width: 100%; overflow: hidden; text-overflow: ellipsis;")


def _build_chat_controls_dialog(
    state: AppState,
    *,
    open_settings: Callable[..., None],
    rebuild_main: Callable[..., None],
    composer_extras: Any | None = None,
) -> None:
    dialog = ui.dialog().props("position=bottom")
    with dialog:
        with ui.card().classes("row-bot-mobile-sheet w-full no-shadow").props(
            "data-docs-id=mobile-chat-controls"
        ):
            with ui.row().classes("w-full items-center justify-between no-wrap"):
                ui.label("Chat controls").classes("text-subtitle1")
                ui.button(icon="close", on_click=dialog.close).props("flat dense round")
            ui.label("These apply to the active thread.").classes("text-grey-6 text-sm")
            policy_icon, policy_label, policy_detail = _model_policy_summary(state)
            with ui.element("div").classes("row-bot-mobile-policy-chip"):
                ui.icon(policy_icon)
                with ui.column().classes("gap-0").style("min-width: 0;"):
                    ui.label(policy_label).classes("text-xs text-weight-medium")
                    ui.label(policy_detail).classes("text-grey-6 text-xs")

            model_options = _model_options_for_mobile(state)
            model_value = state.thread_model_override or "__default__"
            model_select = ui.select(
                options=model_options,
                value=model_value if model_value in model_options else "__default__",
                label="Model",
            ).classes("w-full").props("outlined dense")
            approval_select = ui.select(
                options={
                    "block": "Block risky tools",
                    "approve": "Ask before action tools",
                    "allow_all": "Allow action tools",
                },
                value=state.thread_approval_mode or "block",
                label="Approval mode",
            ).classes("w-full").props("outlined dense")
            profile_options = _agent_profile_options_for_mobile()
            profile_value = _current_agent_profile_value(state)
            profile_select = ui.select(
                options=profile_options,
                value=profile_value if profile_value in profile_options else "__default__",
                label="Agent profile",
            ).classes("w-full").props("outlined dense")

            def _open_skills() -> None:
                if composer_extras is None:
                    ui.notify("Open an active thread composer first.", type="info")
                    return
                dialog.close()
                composer_extras.open_skill_picker()

            ui.button(
                "Skills",
                icon="auto_fix_high",
                on_click=_open_skills,
            ).props("outline dense no-caps").classes("w-full")

            def _save_controls() -> None:
                if not state.thread_id:
                    ui.notify("Open a thread before saving controls.", type="warning")
                    return
                model_value = str(model_select.value or "")
                model_override = "" if model_value == "__default__" else model_value
                _set_thread_model_override(state.thread_id, model_override)
                _set_thread_approval_mode(state.thread_id, str(approval_select.value or "block"))
                profile_value = str(profile_select.value or "__default__")
                if profile_value == "__default__":
                    _clear_thread_agent_profile(state.thread_id)
                else:
                    _set_thread_agent_profile(state.thread_id, profile_value)
                state.thread_model_override = model_override
                state.thread_approval_mode = str(approval_select.value or "block")
                dialog.close()
                rebuild_main(immediate=True, reason="mobile_chat_controls")

            with ui.row().classes("w-full justify-between items-center gap-2"):
                ui.button(
                    "Full settings",
                    icon="settings",
                    on_click=lambda: (dialog.close(), open_settings("Providers")),
                ).props("flat dense no-caps")
                ui.button("Apply", icon="check", on_click=_save_controls).props(
                    "unelevated dense no-caps color=primary"
                )
    dialog.open()


def _build_mobile_thread_composer(
    state: AppState,
    p: P,
    *,
    send_message: Callable[..., Any],
    open_settings: Callable[..., None],
    rebuild_main: Callable[..., None],
    composer_extras: Any | None,
) -> None:
    hidden_upload = build_file_upload(p, state)
    with ui.column().classes("row-bot-mobile-composer w-full gap-1").props(
        "data-docs-id=mobile-chat-composer"
    ):
        from row_bot.ui.live_control import build_live_control_dock

        build_live_control_dock(
            state,
            p,
            stop_generation=lambda thread_id: request_generation_stop(
                thread_id,
                state=state,
                p=p,
                reason="mobile_live_control",
            ),
        )
        _build_file_chips(p)
        text_input = ui.textarea(placeholder="Ask anything...").classes("w-full").props(
            'borderless autogrow input-style="min-height: 44px; max-height: 108px; overflow-y: auto;"'
        )
        if composer_extras is not None:
            try:
                composer_extras.render_before_input(
                    render_skill_chips=False,
                    slash_palette_classes="w-full gap-0",
                )
            except Exception:
                logger.debug("Mobile composer extras failed to render", exc_info=True)
        if composer_extras is not None:
            try:
                composer_extras.attach_input(text_input)
            except Exception:
                logger.debug("Mobile composer extras failed to attach input", exc_info=True)
        with ui.row().classes("row-bot-mobile-action-row w-full items-center gap-1 no-wrap"):
            ui.button(
                icon="attach_file",
                on_click=lambda: ui.run_javascript(
                    f"document.getElementById('c{hidden_upload.id}').querySelector('input[type=file]').click()"
                ),
            ).props("flat dense round").tooltip("Attach")
            ui.button(
                _compact_model_label(state),
                icon="tune",
                on_click=lambda: _build_chat_controls_dialog(
                    state,
                    open_settings=open_settings,
                    rebuild_main=rebuild_main,
                    composer_extras=composer_extras,
                ),
            ).props("flat dense no-caps").classes("row-bot-mobile-model-pill").tooltip("Controls")
            ui.space()
            if composer_extras is not None:
                with ui.element("div").classes("row-bot-mobile-skill-chip-slot"):
                    try:
                        composer_extras.render_skill_chips(
                            classes="row-bot-mobile-skill-chip-row flex-nowrap items-center gap-1"
                        )
                    except Exception:
                        logger.debug("Mobile composer skill chips failed to render", exc_info=True)
            p.stop_btn = ui.button(icon="stop", on_click=lambda: _stop_generation(state, p)).props(
                "flat dense round color=warning"
            ).tooltip("Stop")
            if not _mobile_generation_active(state):
                p.stop_btn.disable()

            async def _submit_async(*, voice_mode: bool = False) -> None:
                text = str(text_input.value or "").strip()
                if not text and not p.pending_files:
                    return
                if not state.thread_id:
                    ui.notify("Create or open a thread before sending.", type="warning")
                    return
                text_input.set_value("")
                if composer_extras is not None:
                    try:
                        composer_extras.clear_draft_on_send()
                    except Exception:
                        logger.debug("Mobile composer extras failed to clear draft", exc_info=True)
                state.active_designer_project = None
                state.active_developer_workspace_id = None
                result = (
                    send_message(text, voice_mode=True)
                    if voice_mode
                    else send_message(text)
                )
                if inspect.isawaitable(result):
                    await result
                set_mobile_chat_mode(state, "thread")

            def _submit() -> None:
                asyncio.create_task(_submit_async())

            async def _on_browser_voice_transcript(e) -> None:
                payload = e.args if isinstance(e.args, dict) else {}
                text = str(payload.get("text") or "").strip()
                if not text:
                    return
                text_input.value = text
                text_input.update()
                await _submit_async(voice_mode=True)

            def _on_browser_voice_error(e) -> None:
                payload = e.args if isinstance(e.args, dict) else {}
                code = str(payload.get("code") or "")
                message = (
                    "Browser microphone access requires HTTPS."
                    if code == "secure_context_required"
                    else "Browser voice is unavailable. Check microphone permission and local model status."
                )
                ui.notify(message, type="warning", close_button=True)
                state.voice_enabled = False
                state.voice_coordinator.stop()

            browser_voice_sink = ui.element("div").style("display:none")
            browser_voice_sink.on(
                "row-bot-browser-voice-transcript",
                _on_browser_voice_transcript,
                js_handler="(e) => emit(e.detail)",
            )
            browser_voice_sink.on(
                "row-bot-browser-voice-error",
                _on_browser_voice_error,
                js_handler="(e) => emit(e.detail)",
            )
            try:
                p.realtime_client = ui.context.client
            except Exception:
                p.realtime_client = None

            def _start_browser_voice() -> None:
                from row_bot.ui.streaming import run_realtime_client_js
                from row_bot.voice.browser_client import start_browser_voice_capture_js

                state.voice_enabled = True
                state.voice_input_mode = "talk"
                state.voice_coordinator.start_browser("talk")
                run_realtime_client_js(
                    p,
                    start_browser_voice_capture_js(
                        sink_id=browser_voice_sink.id,
                        mode="talk",
                    ),
                    context="start_mobile_browser_talk",
                )

            text_input.on(
                "keydown.enter",
                _submit,
                js_handler="""(e) => {
                    if (window._rowBotSlashPaletteOpen) return;
                    if (e.shiftKey || e.ctrlKey || e.metaKey || e.altKey) return;
                    e.preventDefault();
                    emit();
                }""",
            )
            ui.button(
                icon="keyboard_voice",
                on_click=_start_browser_voice,
            ).props("flat dense round").tooltip("Talk using this browser")
            ui.button(icon="send", on_click=_submit).props("unelevated round color=primary").tooltip("Send")


def build_mobile_thread_list(
    state: AppState,
    p: P,
    *,
    load_thread_messages: Callable[[str], list[dict]],
    open_settings: Callable[..., None],
    rebuild_main: Callable[..., None],
) -> None:
    set_mobile_chat_mode(state, "threads")
    with ui.scroll_area().classes("row-bot-mobile-content"):
        with ui.column().classes("row-bot-mobile-pad w-full gap-3"):
            with ui.row().classes("w-full items-center justify-between no-wrap"):
                with ui.column().classes("gap-0").style("min-width: 0;"):
                    ui.label("Chats").classes("text-h6")
                    ui.label("Start something new or continue a thread.").classes("text-grey-6 text-xs")
                ui.button(
                    icon="settings",
                    on_click=lambda: open_settings("Providers"),
                ).props("flat dense round").tooltip("Settings")

            ui.button(
                "New thread",
                icon="add",
                on_click=lambda: create_mobile_thread(
                    state=state,
                    p=p,
                    load_thread_messages=load_thread_messages,
                    rebuild_main=rebuild_main,
                ),
            ).props("unelevated no-caps color=primary").classes("w-full")

            ui.label("Recent chats").classes("text-subtitle2 q-mt-sm")
            try:
                threads = _list_threads(include_details=True)[:20]
            except Exception:
                logger.warning("Could not list mobile chat threads", exc_info=True)
                threads = []
            try:
                from row_bot.agent_orchestrator import get_thread_orchestration_activity

                orchestration_activity = get_thread_orchestration_activity(
                    [str(row[0]) for row in threads]
                )
            except Exception:
                logger.debug("Could not load mobile Agent activity", exc_info=True)
                orchestration_activity = {}
            if not threads:
                with ui.element("div").classes("row-bot-mobile-empty"):
                    ui.icon("chat_bubble_outline").classes("text-grey-6")
                    ui.label("No chats yet.").classes("text-subtitle2")
                    ui.label("Create a new thread to start mobile chat.").classes(
                        "text-grey-6 text-sm"
                    )
            for row in threads:
                thread_id = str(row[0])
                name = str(row[1] or "Untitled")
                updated = format_mobile_timestamp(row[3] if len(row) > 3 else "")
                agent_activity = orchestration_activity.get(thread_id) or {}
                agent_is_blocking = bool(
                    agent_activity.get("blocking")
                    and str(agent_activity.get("state") or "") == "active"
                )
                with ui.row().classes("row-bot-mobile-thread-row w-full items-center gap-2 no-wrap"):
                    if agent_is_blocking:
                        ui.spinner("oval", size="xs", color="primary").props(
                            'role="status" aria-label="Child Agents working"'
                        ).tooltip("Child Agents working")
                    else:
                        ui.icon("chat_bubble_outline").classes("text-grey-6")
                    with ui.column().classes("gap-0").style("min-width: 0; flex: 1;"):
                        ui.label(name).classes("text-sm text-weight-medium ellipsis")
                        ui.label(updated).classes("text-grey-6 text-xs")
                    ui.button(
                        icon="chevron_right",
                        on_click=lambda tid=thread_id: open_thread_on_mobile(
                            tid,
                            state=state,
                            p=p,
                            load_thread_messages=load_thread_messages,
                            rebuild_main=rebuild_main,
                        ),
                    ).props("flat dense round").tooltip("Open")


def build_mobile_thread_detail(
    state: AppState,
    p: P,
    *,
    send_message: Callable[..., Any],
    add_chat_message: Callable[[dict], Any],
    load_thread_messages: Callable[[str], list[dict]],
    open_settings: Callable[..., None],
    rebuild_main: Callable[..., None],
) -> None:
    set_mobile_chat_mode(state, "thread")
    composer_extras = create_chat_composer_extras(
        state,
        p,
        surface="chat",
        compact_skill_chips=True,
        show_draft_suggestions=False,
        new_thread=lambda: create_mobile_thread(
            state=state,
            p=p,
            load_thread_messages=load_thread_messages,
            rebuild_main=rebuild_main,
        ),
    )
    with ui.column().classes("row-bot-mobile-chat-detail w-full no-wrap").props(
        "data-docs-id=mobile-chat-detail"
    ):
        with ui.row().classes("row-bot-mobile-detail-header w-full items-center gap-2 no-wrap"):
            ui.button(
                icon="arrow_back",
                on_click=lambda: (set_mobile_chat_mode(state, "threads"), rebuild_main(immediate=True, reason="mobile_chat_back")),
            ).props("flat dense round").tooltip("Back to chats")
            with ui.column().classes("gap-0").style("min-width: 0; flex: 1;"):
                ui.label(state.thread_name or "Untitled").classes("text-subtitle1 ellipsis")

        with ui.column().classes("row-bot-mobile-message-area w-full"):
            build_chat_messages(
                p,
                state,
                messages=state.messages,
                add_chat_message=add_chat_message,
                placeholder_text="Ask anything...",
            )

        _build_mobile_thread_composer(
            state,
            p,
            send_message=send_message,
            open_settings=open_settings,
            rebuild_main=rebuild_main,
            composer_extras=composer_extras,
        )


def build_mobile_chat(
    state: AppState,
    p: P,
    *,
    send_message: Callable[..., Any],
    load_thread_messages: Callable[[str], list[dict]],
    add_chat_message: Callable[[dict], Any],
    open_settings: Callable[..., None],
    rebuild_main: Callable[..., None],
) -> None:
    """Build the mobile chat surface with list/detail navigation."""
    if mobile_chat_mode(state) == "thread" and state.thread_id:
        build_mobile_thread_detail(
            state,
            p,
            send_message=send_message,
            add_chat_message=add_chat_message,
            load_thread_messages=load_thread_messages,
            open_settings=open_settings,
            rebuild_main=rebuild_main,
        )
        return
    build_mobile_thread_list(
        state,
        p,
        load_thread_messages=load_thread_messages,
        open_settings=open_settings,
        rebuild_main=rebuild_main,
    )
