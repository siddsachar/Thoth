"""Shared slash command and Smart Skills controls for chat composers."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from inspect import isawaitable
from typing import Any, Callable, Literal

from nicegui import run, ui

from row_bot.ui.state import AppState, P, _active_generations
from row_bot.ui.streaming import request_generation_stop
from row_bot.ui.timer_utils import defer_ui

logger = logging.getLogger(__name__)

SkillMode = Literal["chat", "thread_override", "developer"]


@dataclass
class ComposerExtrasConfig:
    """Surface-specific behavior for the shared composer extras."""

    surface: str
    skill_mode: SkillMode = "chat"
    open_export: Callable[[], None] | None = None
    new_thread: Callable[[], Any] | None = None
    on_skills_changed: Callable[[], None] | None = None
    enabled_tool_names: Callable[[], list[str]] | None = None
    last_user_text: Callable[[], str] | None = None
    skill_button_tooltip: str | None = None
    compact_skill_chips: bool = False
    show_draft_suggestions: bool = True


def chat_enabled_tool_names() -> list[str]:
    """Return enabled tool names for Smart Skills suggestions."""

    try:
        from row_bot.tools import registry as tool_registry

        return [tool.name for tool in tool_registry.get_enabled_tools()]
    except Exception:
        logger.debug("Could not read enabled tool names for composer skills", exc_info=True)
        return []


def last_user_message_text(state: AppState) -> str:
    """Return the most recent user message text for picker ranking."""

    for message in reversed(getattr(state, "messages", []) or []):
        if message.get("role") == "user":
            return str(message.get("content") or "")
    return ""


def create_chat_composer_extras(
    state: AppState,
    p: P,
    *,
    surface: str = "chat",
    open_export: Callable[[], None] | None = None,
    new_thread: Callable[[], Any] | None = None,
    on_skills_changed: Callable[[], None] | None = None,
    enabled_tool_names: Callable[[], list[str]] | None = None,
    compact_skill_chips: bool = False,
    show_draft_suggestions: bool = True,
) -> "ComposerExtrasController":
    """Create extras using normal chat/Designer Smart Skills semantics."""

    return ComposerExtrasController(
        state,
        p,
        ComposerExtrasConfig(
            surface=surface,
            skill_mode="chat",
            open_export=open_export,
            new_thread=new_thread,
            on_skills_changed=on_skills_changed,
            enabled_tool_names=enabled_tool_names or chat_enabled_tool_names,
            last_user_text=lambda: last_user_message_text(state),
            skill_button_tooltip="Choose skills for this chat",
            compact_skill_chips=compact_skill_chips,
            show_draft_suggestions=show_draft_suggestions,
        ),
    )


def create_developer_composer_extras(
    state: AppState,
    p: P,
    *,
    new_thread: Callable[[], Any] | None = None,
    on_skills_changed: Callable[[], None] | None = None,
    enabled_tool_names: Callable[[], list[str]] | None = None,
) -> "ComposerExtrasController":
    """Create extras using Developer thread skill override semantics."""

    return ComposerExtrasController(
        state,
        p,
        ComposerExtrasConfig(
            surface="developer",
            skill_mode="developer",
            new_thread=new_thread,
            on_skills_changed=on_skills_changed,
            enabled_tool_names=enabled_tool_names or chat_enabled_tool_names,
            last_user_text=lambda: last_user_message_text(state),
            skill_button_tooltip=(
                "Choose skills for this Developer thread"
            ),
        ),
    )


def create_designer_composer_extras(
    state: AppState,
    p: P,
    *,
    on_skills_changed: Callable[[], None] | None = None,
    enabled_tool_names: Callable[[], list[str]] | None = None,
) -> "ComposerExtrasController":
    """Create extras for Designer, where selected skills are explicit overrides."""

    return ComposerExtrasController(
        state,
        p,
        ComposerExtrasConfig(
            surface="designer",
            skill_mode="thread_override",
            on_skills_changed=on_skills_changed,
            enabled_tool_names=enabled_tool_names or chat_enabled_tool_names,
            last_user_text=lambda: last_user_message_text(state),
            skill_button_tooltip="Choose skills for this Designer thread",
        ),
    )


class ComposerExtrasController:
    """Owns shared slash palette, skill picker, chips, and input key handlers."""

    def __init__(self, state: AppState, p: P, config: ComposerExtrasConfig) -> None:
        self.state = state
        self.p = p
        self.config = config
        self.input = None
        self.client = None
        self.skill_chips_row = None
        self.slash_palette_col = None
        self.available_skills: list[Any] = []
        self.active_skill_names: list[str] = []
        self.draft_state: dict[str, Any] = {
            "text": "",
            "version": 0,
            "suggestions_suppressed_text": "",
        }
        self.chip_refresh_task: asyncio.Task | None = None
        self.slash_palette: dict[str, Any] = {
            "open": False,
            "query": "",
            "index": 0,
            "items": [],
            "cursor": 0,
        }
        self.controls_prepared = False

    def _prepare_controls(self) -> None:
        self.client = ui.context.client
        self._ensure_skills_loaded_async()
        self._refresh_available_skills()
        self.active_skill_names = self._load_active_skill_names()
        self.controls_prepared = True

    def render_before_input(
        self,
        *,
        render_skill_chips: bool = True,
        skill_chips_classes: str = "w-full flex-wrap items-center gap-1 q-px-md q-pt-xs",
        slash_palette_classes: str = "w-full gap-0 q-px-md q-pt-sm",
    ) -> None:
        """Render controls that sit above the textarea inside the composer card."""

        self._prepare_controls()
        if render_skill_chips:
            self.render_skill_chips(classes=skill_chips_classes)
        self.slash_palette_col = ui.column().classes(slash_palette_classes)

    def render_skill_chips(self, *, classes: str = "w-full flex-wrap items-center gap-1 q-px-md q-pt-xs") -> None:
        """Render the Smart Skills chip row into the current UI container."""

        if not self.controls_prepared:
            self._prepare_controls()
        self.skill_chips_row = ui.row().classes(classes)
        self._render_skill_chips(self.draft_state.get("text", ""))

    def attach_input(self, chat_input) -> None:
        """Attach draft and key handlers after the textarea has been created."""

        self.input = chat_input
        try:
            chat_input.on(
                "update:model-value",
                self._on_composer_value,
                js_handler="""(value) => {
                    const el = this.$refs?.qRef?.nativeEl || this.$el?.querySelector('textarea');
                    emit({value, cursor: el ? el.selectionStart : String(value || '').length});
                }""",
            )
        except Exception:
            logger.debug("Smart Skills draft suggestion handler was not attached", exc_info=True)
        chat_input.on(
            "keydown",
            lambda e: self.handle_key(e.args.get("key") if isinstance(e.args, dict) else ""),
            js_handler="""(e) => {
                if (!window._rowBotSlashPaletteOpen) return;
                if (!['ArrowDown', 'ArrowUp', 'Enter', 'Tab', 'Escape'].includes(e.key)) return;
                e.preventDefault();
                e.stopPropagation();
                emit({key: e.key});
            }""",
        )

    def clear_draft_on_send(self) -> None:
        self.queue_skill_chip_refresh("")
        self.close_slash_palette()

    def set_text(self, value: str, *, cursor: int | None = None) -> None:
        self._set_composer_text(value, cursor)

    def handle_key(self, key: str) -> None:
        if not self.slash_palette.get("open"):
            return
        normalized = str(key or "")
        if normalized == "ArrowDown":
            self._slash_palette_move(1)
        elif normalized == "ArrowUp":
            self._slash_palette_move(-1)
        elif normalized in {"Enter", "Tab"}:
            self._slash_palette_pick_selected()
        elif normalized == "Escape":
            self.close_slash_palette()

    def _ensure_skills_loaded_async(self) -> None:
        try:
            import row_bot.skills as skills_mod

            if skills_mod.skills_loaded():
                return

            async def _load() -> None:
                await run.io_bound(skills_mod.load_skills)

            defer_ui(_load)
        except Exception:
            logger.debug("Could not schedule composer skills load", exc_info=True)

    def _refresh_available_skills(self) -> None:
        try:
            from row_bot.skill_discovery import collect_enabled_skill_records

            self.available_skills = collect_enabled_skill_records()
        except Exception:
            logger.debug("Could not load composer skill choices", exc_info=True)
            self.available_skills = []

    def _thread_id(self) -> str:
        return str(getattr(self.state, "thread_id", "") or "default")

    def _enabled_tool_names(self) -> list[str]:
        getter = self.config.enabled_tool_names or chat_enabled_tool_names
        try:
            return list(getter())
        except Exception:
            logger.debug("Could not read composer enabled tool names", exc_info=True)
            return []

    def _thread_override(self) -> list[str] | None:
        try:
            from row_bot.threads import get_thread_skills_override

            return get_thread_skills_override(self._thread_id())
        except Exception:
            logger.debug("Could not read thread skills override", exc_info=True)
            return None

    def _load_active_skill_names(self) -> list[str]:
        if self.config.skill_mode in {"developer", "thread_override"}:
            from row_bot.skills_activation import get_auto_loaded_skill_ids

            names = [
                *(self._thread_override() or []),
                *get_auto_loaded_skill_ids(self._thread_id()),
            ]
            return self._ordered_skill_names(self._filter_enabled_skill_names(names))
        try:
            from row_bot.skills_activation import get_activation_snapshot

            snap = get_activation_snapshot(
                self._thread_id(),
                current_text="",
                enabled_tool_names=self._enabled_tool_names(),
                explicit_override=self._thread_override(),
            )
            from row_bot.skills_activation import get_auto_loaded_skill_ids

            return self._ordered_skill_names([
                *snap.active,
                *get_auto_loaded_skill_ids(self._thread_id()),
            ])
        except Exception:
            logger.debug("Could not read active composer skills", exc_info=True)
            return []

    def _filter_enabled_skill_names(self, names: list[str]) -> list[str]:
        enabled = {getattr(skill, "name", "") for skill in self.available_skills}
        return [name for name in names if name in enabled]

    @staticmethod
    def _ordered_skill_names(names) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for name in names:
            value = str(name or "").strip()
            if value and value not in seen:
                seen.add(value)
                ordered.append(value)
        return ordered

    def _active_name_set(self) -> set[str]:
        return set(self.active_skill_names)

    @staticmethod
    def _skill_draft_key(text: str) -> str:
        return re.sub(r"\s+", " ", str(text or "").strip()).lower()

    def _cancel_skill_chip_refresh_task(self) -> None:
        task = self.chip_refresh_task
        if task and not task.done():
            task.cancel()

    def _suppress_skill_suggestions_for_current_draft(self) -> None:
        self.draft_state["suggestions_suppressed_text"] = self._skill_draft_key(
            self.draft_state.get("text", "")
        )
        self._cancel_skill_chip_refresh_task()

    def _after_skills_changed(self) -> None:
        try:
            from row_bot.agent import clear_agent_cache

            clear_agent_cache()
        except Exception:
            logger.debug("Could not clear agent cache after skill change", exc_info=True)
        if self.config.on_skills_changed:
            try:
                self.config.on_skills_changed()
            except Exception:
                logger.debug("Composer skill change callback failed", exc_info=True)
        self._render_skill_chips(self.draft_state.get("text", ""))

    def refresh_from_store(self) -> None:
        """Refresh skill choices and active chips after a model-side skill load."""

        self._refresh_available_skills()
        self.active_skill_names = self._load_active_skill_names()
        self._render_skill_chips(self.draft_state.get("text", ""))

    def use_skill(self, name: str, *, source: str = "ui") -> None:
        if self.config.skill_mode in {"developer", "thread_override"}:
            current = self._thread_override() or []
            names = self._ordered_skill_names([*current, name])
            self._save_thread_override(names)
            self.active_skill_names = self._filter_enabled_skill_names(names)
        else:
            if name.startswith("plugin:"):
                from row_bot.skills_activation import load_auto_skill

                load_auto_skill(
                    self._thread_id(),
                    name,
                    available_ids={skill.name for skill in self.available_skills},
                )
            else:
                from row_bot.skills_activation import pin_skill, record_accept

                pin_skill(self._thread_id(), name)
                record_accept(self._thread_id(), name, source=source)
            self.active_skill_names = self._ordered_skill_names([*self.active_skill_names, name])
        if source.startswith("ui"):
            self._suppress_skill_suggestions_for_current_draft()
        self._after_skills_changed()

    def remove_skill(self, name: str) -> None:
        if self.config.skill_mode in {"developer", "thread_override"}:
            from row_bot.skills_activation import (
                get_auto_loaded_skill_ids,
                remove_auto_loaded_skill,
            )

            if name in set(get_auto_loaded_skill_ids(self._thread_id())):
                remove_auto_loaded_skill(self._thread_id(), name)
            else:
                current = [item for item in (self._thread_override() or []) if item != name]
                self._save_thread_override(current)
            self.active_skill_names = self._load_active_skill_names()
        else:
            from row_bot.skills_activation import (
                get_auto_loaded_skill_ids,
                remove_auto_loaded_skill,
            )

            if name in set(get_auto_loaded_skill_ids(self._thread_id())):
                remove_auto_loaded_skill(self._thread_id(), name)
            else:
                from row_bot.skills_activation import disable_skill

                disable_skill(self._thread_id(), name)
            self.active_skill_names = [item for item in self.active_skill_names if item != name]
        self._after_skills_changed()

    def _save_thread_override(self, names: list[str] | None) -> None:
        try:
            from row_bot.threads import set_thread_skills_override

            set_thread_skills_override(self._thread_id(), names)
        except Exception:
            logger.debug("Could not write thread skills override", exc_info=True)

    def _dismiss_suggestion(self, name: str) -> None:
        if self.config.skill_mode == "developer":
            self._suppress_skill_suggestions_for_current_draft()
            self._render_skill_chips(self.draft_state.get("text", ""))
            return
        from row_bot.skills_activation import dismiss_suggestion

        dismiss_suggestion(self._thread_id(), name)
        self._suppress_skill_suggestions_for_current_draft()
        self._render_skill_chips(self.draft_state.get("text", ""))

    def _meaningful_skill_draft(self, text: str) -> bool:
        return len(str(text or "").strip()) >= 3

    def _suggestions_for_text(self, text: str, *, limit: int = 3):
        if not self._meaningful_skill_draft(text):
            return []
        if self._skill_draft_key(text) == self.draft_state.get("suggestions_suppressed_text"):
            return []
        try:
            from row_bot.skills_activation import suggest_skills

            return suggest_skills(
                self._thread_id(),
                text,
                enabled_tool_names=self._enabled_tool_names(),
                extra_excluded=self._thread_override() or [],
                limit=limit,
                trace=False,
            )
        except Exception:
            logger.debug("Could not compute composer skill suggestions", exc_info=True)
            return []

    def _list_skill_choices(self, query: str = "", limit: int | None = None):
        from types import SimpleNamespace
        from row_bot.capability_search import search_capabilities

        records = list(self.available_skills)
        if query.strip():
            documents = [record.search_document() for record in records]
            matched_ids = {
                document.canonical_id
                for document in search_capabilities(documents, query, limit=limit or 5)
            }
            records = [record for record in records if record.canonical_id in matched_ids]
        if limit is not None:
            records = records[:limit]
        return [SimpleNamespace(name=record.canonical_id) for record in records]

    def _open_skill_picker(self) -> None:
        self._refresh_available_skills()
        self.active_skill_names = self._load_active_skill_names()
        picker_text = str(self.draft_state.get("text") or "").strip()
        if not picker_text and self.config.last_user_text:
            picker_text = self.config.last_user_text()
        picker_suggestions = self._suggestions_for_text(picker_text, limit=3)
        picker_suggestions_by_name = {s.name: s for s in picker_suggestions}
        title = "Skills"
        with ui.dialog() as dlg, ui.card().classes("w-full q-pa-md").style(
            "min-width: min(720px, 92vw); max-width: 760px;"
        ):
            with ui.row().classes("w-full items-center"):
                ui.label(title).classes("text-h6")
                ui.space()
                ui.button(icon="close", on_click=dlg.close).props("flat round dense")
            if self.config.skill_mode == "developer":
                try:
                    from row_bot.developer.profile import DEVELOPER_AUTO_SKILLS

                    guidance_items = []
                    for name in DEVELOPER_AUTO_SKILLS:
                        skill = self._get_skill(name)
                        if skill:
                            guidance_items.append(f"{skill.icon} {skill.display_name}")
                    if guidance_items:
                        with ui.expansion(
                            "Automatic Developer guidance",
                            icon="tips_and_updates",
                        ).props("dense expand-icon-toggle").classes("w-full q-mb-sm"):
                            ui.label(
                                "Included automatically while the Developer tool is active."
                            ).classes("text-xs text-grey-6 q-mb-xs")
                            for item in guidance_items:
                                ui.label(item).classes("text-xs text-grey-5")
                except Exception:
                    logger.debug("Could not render Developer guidance disclosure", exc_info=True)
            search = ui.input(
                placeholder="Search skills",
            ).props("dense outlined clearable").classes("w-full q-mb-sm")
            skill_list = ui.column().classes("w-full gap-2").style("max-height: 58vh; overflow-y: auto;")

            def _render_skill_list() -> None:
                query = str(search.value or "").strip()
                skill_list.clear()
                ranked_choices = self._list_skill_choices(query=query, limit=None)
                ranked_names = [choice.name for choice in ranked_choices]
                ranked_name_set = set(ranked_names)
                active_skills = [
                    self._get_skill(name)
                    for name in self.active_skill_names
                ]
                active_skills = [
                    skill for skill in active_skills
                    if skill and (not query or skill.name in ranked_name_set)
                ]
                suggested = [
                    suggestion for suggestion in picker_suggestions
                    if not query or suggestion.name in ranked_name_set
                ]
                available_by_name = {
                    skill.name: skill
                    for skill in self.available_skills
                    if skill.name not in self._active_name_set()
                    and skill.name not in picker_suggestions_by_name
                }
                available = [
                    available_by_name[name]
                    for name in ranked_names
                    if name in available_by_name
                ]
                with skill_list:
                    if active_skills:
                        ui.label("Active here").classes("text-xs text-grey-5 text-uppercase")
                        for skill in active_skills:
                            with ui.row().classes("w-full items-center no-wrap q-pa-xs rounded-borders"):
                                with ui.column().classes("gap-0"):
                                    ui.label(f"{skill.icon} {skill.display_name}").classes("text-sm text-weight-medium")
                                    ui.label(self._skill_source_label(skill)).classes("text-xs text-grey-6")
                                ui.space()
                                ui.button(
                                    "Remove",
                                    icon="close",
                                    on_click=lambda _, n=skill.name: (self.remove_skill(n), dlg.close()),
                                ).props("flat dense no-caps size=sm")
                    if suggested:
                        ui.label("Suggested").classes("text-xs text-grey-5 text-uppercase q-mt-sm")
                        for suggestion in suggested:
                            with ui.row().classes("w-full items-center no-wrap q-pa-xs rounded-borders"):
                                with ui.column().classes("gap-0"):
                                    ui.label(f"{suggestion.icon} {suggestion.display_name}").classes("text-sm text-weight-medium")
                                    ui.label(suggestion.reason).classes("text-xs text-grey-6")
                                ui.space()
                                ui.button(
                                    "Use",
                                    icon="add",
                                    on_click=lambda _, n=suggestion.name: (self.use_skill(n, source="ui_picker_suggested"), dlg.close()),
                                ).props("flat dense no-caps size=sm")
                                ui.button(
                                    "Dismiss",
                                    icon="close",
                                    on_click=lambda _, n=suggestion.name: (self._dismiss_suggestion(n), dlg.close()),
                                ).props("flat dense no-caps size=sm")
                    ui.label("Available").classes("text-xs text-grey-5 text-uppercase q-mt-sm")
                    if available:
                        for skill in available:
                            with ui.row().classes("w-full items-center no-wrap q-pa-xs rounded-borders"):
                                with ui.column().classes("gap-0"):
                                    ui.label(f"{skill.icon} {skill.display_name}").classes("text-sm text-weight-medium")
                                    ui.label(self._skill_source_label(skill)).classes("text-xs text-grey-6")
                                    if skill.description:
                                        ui.label(skill.description).classes("text-xs text-grey-6")
                                ui.space()
                                ui.button(
                                    "Use",
                                    icon="add",
                                    on_click=lambda _, n=skill.name: (self.use_skill(n, source="ui_picker"), dlg.close()),
                                ).props("flat dense no-caps size=sm")
                    else:
                        ui.label("No available skills match.").classes("text-grey-6 text-sm")

            search.on("update:model-value", lambda _: _render_skill_list())
            _render_skill_list()
        dlg.open()

    def open_skill_picker(self) -> None:
        """Open the shared skill picker from an external composer control."""

        self._open_skill_picker()

    def _get_skill(self, name: str):
        for skill in self.available_skills:
            if getattr(skill, "name", "") == name:
                return skill
        try:
            import row_bot.skills as skills_mod

            return skills_mod.get_skill(name)
        except Exception:
            logger.debug("Could not get skill %s", name, exc_info=True)
            return None

    @staticmethod
    def _skill_source_label(skill) -> str:
        plugin_name = str(getattr(skill, "plugin_name", "") or "").strip()
        if plugin_name:
            return f"Plugin: {plugin_name}"
        source = str(getattr(skill, "source", "") or "").strip()
        if source.startswith("plugin:"):
            return f"Plugin: {source.removeprefix('plugin:')}"
        return "Manual"

    def _render_skill_chips(self, draft_text: str = "") -> None:
        if self.skill_chips_row is None:
            return
        try:
            self._refresh_available_skills()
            self.skill_chips_row.clear()
            self.active_skill_names = self._ordered_skill_names(self.active_skill_names)
            draft_suggestions = (
                self._suggestions_for_text(draft_text, limit=3)
                if self.config.show_draft_suggestions
                else []
            )
            with self.skill_chips_row:
                if self.config.compact_skill_chips:
                    active_count = len(self.active_skill_names)
                    label = "Skills" if active_count == 0 else f"Skills {active_count}"
                    ui.button(label, icon="auto_fix_high", on_click=self._open_skill_picker).props(
                        "outline dense no-caps size=sm"
                    ).classes("text-xs").tooltip(self.config.skill_button_tooltip or "Choose skills for this chat")
                    for suggestion in draft_suggestions[:1]:
                        with ui.button(
                            f"{suggestion.icon} {suggestion.display_name}",
                        ).props("flat dense no-caps size=sm").classes("text-xs"):
                            with ui.menu().classes("q-pa-sm"):
                                ui.label(suggestion.description or suggestion.reason).classes(
                                    "text-xs text-grey-5 q-mb-xs"
                                )
                                ui.button(
                                    "Use",
                                    icon="add",
                                    on_click=lambda _, n=suggestion.name: self.use_skill(
                                        n,
                                        source="ui_draft_suggestion",
                                    ),
                                ).props("flat dense no-caps size=sm")
                                ui.button(
                                    "Dismiss",
                                    icon="close",
                                    on_click=lambda _, n=suggestion.name: self._dismiss_suggestion(n),
                                ).props("flat dense no-caps size=sm")
                    return
                label = "Skills"
                ui.button(label, icon="auto_fix_high", on_click=self._open_skill_picker).props(
                    "outline dense no-caps size=sm"
                ).classes("text-xs").tooltip(self.config.skill_button_tooltip or "Choose skills for this chat")
                for name in self.active_skill_names:
                    skill = self._get_skill(name)
                    chip_label = f"{skill.icon} {skill.display_name}" if skill else name
                    ui.button(
                        chip_label,
                        icon="close",
                        on_click=lambda _, n=name: self.remove_skill(n),
                    ).props("outline dense no-caps size=sm").classes("text-xs").tooltip(
                        "Remove skill from this composer"
                    )
                for suggestion in draft_suggestions:
                    with ui.button(
                        f"{suggestion.icon} {suggestion.display_name}",
                    ).props("flat dense no-caps size=sm").classes("text-xs"):
                        with ui.menu().classes("q-pa-sm"):
                            ui.label(suggestion.description or suggestion.reason).classes(
                                "text-xs text-grey-5 q-mb-xs"
                            )
                            ui.button(
                                "Use",
                                icon="add",
                                on_click=lambda _, n=suggestion.name: self.use_skill(n, source="ui_draft_suggestion"),
                            ).props("flat dense no-caps size=sm")
                            ui.button(
                                "Dismiss",
                                icon="close",
                                on_click=lambda _, n=suggestion.name: self._dismiss_suggestion(n),
                            ).props("flat dense no-caps size=sm")
        except Exception:
            logger.debug("Smart Skills draft chip refresh failed", exc_info=True)

    def _debounced_skill_chip_refresh(self, version: int) -> None:
        if version != self.draft_state.get("version"):
            return
        self._render_skill_chips(self.draft_state.get("text", ""))

    def queue_skill_chip_refresh(self, text: str) -> None:
        self.draft_state["text"] = str(text or "")
        self.draft_state["version"] = int(self.draft_state.get("version", 0)) + 1
        current_key = self._skill_draft_key(self.draft_state["text"])
        if current_key != self.draft_state.get("suggestions_suppressed_text"):
            self.draft_state["suggestions_suppressed_text"] = ""
        self._cancel_skill_chip_refresh_task()
        self.chip_refresh_task = defer_ui(
            lambda v=int(self.draft_state["version"]): self._debounced_skill_chip_refresh(v),
            delay=0.25,
        )

    def _on_composer_value(self, e) -> None:
        payload = e.args
        if isinstance(payload, dict):
            text = str(payload.get("value") or "")
            cursor = payload.get("cursor")
        else:
            text = str(payload or getattr(self.input, "value", "") or "")
            cursor = len(text)
        self.queue_skill_chip_refresh(text)
        try:
            self.slash_palette_on_text(text, int(cursor) if cursor is not None else len(text))
        except Exception:
            logger.debug("Slash command palette update failed", exc_info=True)

    def _set_slash_palette_flag(self, opened: bool) -> None:
        try:
            client = self.client or ui.context.client
            client.run_javascript(
                f"window._rowBotSlashPaletteOpen = {str(bool(opened)).lower()};"
            )
        except Exception:
            logger.debug("Could not update slash palette browser flag", exc_info=True)

    def _set_composer_text(self, text: str, cursor: int | None = None) -> None:
        value = str(text or "")
        if self.input:
            self.input.value = value
            self.input.update()
        self.queue_skill_chip_refresh(value)
        cursor = len(value) if cursor is None else max(0, min(int(cursor), len(value)))
        if self.input:
            payload = json.dumps({"id": self.input.id, "cursor": cursor})
            ui.run_javascript(
                f"""(function(p) {{
                    const root = document.getElementById('c' + p.id);
                    const input = root && root.querySelector('textarea');
                    if (!input) return;
                    input.focus();
                    try {{ input.setSelectionRange(p.cursor, p.cursor); }} catch (_) {{}}
                }})({payload});"""
            )

    def close_slash_palette(self) -> None:
        self.slash_palette["open"] = False
        self.slash_palette["items"] = []
        self._set_slash_palette_flag(False)
        if self.slash_palette_col is not None:
            self.slash_palette_col.clear()

    def _show_text_dialog(self, title: str, content: str, *, icon: str = "info") -> None:
        def _normalize_dialog_markdown(raw: str) -> str:
            lines = str(raw or "").splitlines()
            normalized: list[str] = []
            for index, line in enumerate(lines):
                stripped = line.strip()
                if stripped.startswith("**") and stripped.endswith("**"):
                    if normalized and normalized[-1] != "":
                        normalized.append("")
                    normalized.append(stripped)
                    next_line = lines[index + 1].strip() if index + 1 < len(lines) else ""
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
                extras=["code-friendly", "fenced-code-blocks", "tables"],
            ).classes("w-full text-sm").style("max-height: 62vh; overflow-y: auto;")
        dlg.open()

    def _remove_token_and_close(self) -> None:
        from row_bot.slash_commands import remove_current_slash_token

        text = str(getattr(self.input, "value", "") or self.draft_state.get("text", ""))
        new_text, cursor = remove_current_slash_token(text, self.slash_palette.get("cursor"))
        self._set_composer_text(new_text, cursor)
        self.close_slash_palette()

    def _replace_token_with_prefix(self, prefix: str) -> None:
        from row_bot.slash_commands import replace_current_slash_token

        text = str(getattr(self.input, "value", "") or self.draft_state.get("text", ""))
        new_text, cursor = replace_current_slash_token(text, self.slash_palette.get("cursor"), prefix)
        self._set_composer_text(new_text, cursor)
        self.close_slash_palette()

    def _run_stop_from_palette(self) -> None:
        result = request_generation_stop(
            self._thread_id(),
            state=self.state,
            p=self.p,
            reason="slash_palette",
        )
        if result.stopped:
            ui.notify("Stop signal sent.", type="warning")
        else:
            ui.notify("No active generation to stop.", type="info")

    def _reset_skills_from_palette(self) -> None:
        if self.config.skill_mode in {"developer", "thread_override"}:
            from row_bot.skills_activation import clear_auto_loaded_skills

            clear_auto_loaded_skills(self._thread_id())
            try:
                from row_bot.skills import get_default_active_skill_names

                self._save_thread_override(get_default_active_skill_names(self.config.surface))
            except Exception:
                logger.debug("Could not resolve surface skill defaults", exc_info=True)
                self._save_thread_override([])
        else:
            from row_bot.skills_activation import reset_thread

            reset_thread(self._thread_id())
        self.active_skill_names = self._load_active_skill_names()
        self._after_skills_changed()
        ui.notify("Skills reset for this thread.", type="info")

    def _run_callback(self, callback: Callable[[], Any]) -> None:
        try:
            result = callback()
            if isawaitable(result):
                asyncio.create_task(result)
        except Exception as exc:
            logger.debug("Composer slash callback failed", exc_info=True)
            ui.notify(str(exc), type="negative", close_button=True)

    def _execute_slash_spec(self, spec) -> None:
        client = self.client or ui.context.client
        with client:
            self._execute_slash_spec_in_client(spec)

    def _execute_slash_spec_in_client(self, spec) -> None:
        if spec.handler_key == "activate_skill" and spec.skill_name:
            self.use_skill(spec.skill_name, source="slash_palette")
            self._remove_token_and_close()
            ui.notify(f"Skill active: {spec.title}", type="positive")
            return
        if spec.handler_key == "open_skills":
            self._remove_token_and_close()
            self._open_skill_picker()
            return
        if spec.handler_key == "skill_reset":
            self._remove_token_and_close()
            self._reset_skills_from_palette()
            return
        if spec.handler_key == "noskill":
            active = list(self.active_skill_names)
            if len(active) == 1:
                self.remove_skill(active[0])
                self._remove_token_and_close()
                ui.notify(f"Removed skill: {active[0]}", type="info")
            else:
                self._replace_token_with_prefix("/noskill ")
            return
        if spec.handler_key == "new_thread":
            if self.config.new_thread:
                self._remove_token_and_close()
                self._run_callback(self.config.new_thread)
            else:
                self._replace_token_with_prefix(spec.slash + " ")
            return
        if spec.handler_key == "stop_generation":
            self._remove_token_and_close()
            self._run_stop_from_palette()
            return
        if spec.handler_key == "export":
            if self.config.open_export:
                self._remove_token_and_close()
                self.config.open_export()
            else:
                self._replace_token_with_prefix(spec.slash + " ")
            return
        if spec.handler_key == "status":
            self._remove_token_and_close()
            from row_bot.tools.row_bot_status_tool import _row_bot_status

            self._show_text_dialog("Status", _row_bot_status("overview"))
            return
        if spec.handler_key == "tools":
            self._remove_token_and_close()
            from row_bot.tools.row_bot_status_tool import _row_bot_status

            self._show_text_dialog("Tools", _row_bot_status("tools"))
            return
        if spec.handler_key == "profiles":
            self._remove_token_and_close()
            from row_bot.agent_commands import format_agent_profiles

            self._show_text_dialog("Agent Profiles", format_agent_profiles(), icon="badge")
            return
        if spec.handler_key == "profile":
            self._replace_token_with_prefix("/profile ")
            return
        if spec.handler_key == "agents":
            self._remove_token_and_close()
            from row_bot.agent_commands import format_agents_status

            self._show_text_dialog(
                "Agents",
                format_agents_status(parent_thread_id=self._thread_id()),
                icon="hub",
            )
            return
        if spec.handler_key == "goal":
            self._replace_token_with_prefix("/goal ")
            return
        if spec.handler_key == "help":
            self._remove_token_and_close()
            from row_bot.slash_commands import help_text

            self._show_text_dialog("Slash Commands", help_text(include_skills=True), icon="help")
            return
        self._replace_token_with_prefix(spec.slash + " ")

    def _render_slash_palette(self) -> None:
        if self.slash_palette_col is None:
            return
        self.slash_palette_col.clear()
        items = list(self.slash_palette.get("items") or [])
        if not self.slash_palette.get("open") or not items:
            self._set_slash_palette_flag(False)
            return
        self._set_slash_palette_flag(True)
        selected_index = int(self.slash_palette.get("index", 0) or 0)
        selected_row_id: int | None = None
        from row_bot.slash_commands import argument_hint

        with self.slash_palette_col:
            with ui.column().classes("w-full gap-0 row-bot-slash-palette-list").style(
                "max-height: 270px; overflow-y: auto; "
                "border: 1px solid rgba(255,255,255,0.14); "
                "border-radius: 8px; background: rgba(18,18,28,0.98); "
                "box-shadow: 0 12px 28px rgba(0,0,0,0.35);"
            ):
                for index, spec in enumerate(items):
                    selected = index == selected_index
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
                        lambda _e, s=spec: self._execute_slash_spec(s),
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
                            hint = argument_hint(spec)
                            detail = f"{spec.description} - {hint}" if hint else spec.description
                            ui.label(detail).classes("text-xs text-grey-6 ellipsis")
                        ui.label(spec.category).classes("text-xs text-grey-7")
        if selected_row_id is not None:
            client = self.client or ui.context.client
            client.run_javascript(
                f"""setTimeout(() => {{
                    const row = document.getElementById('c{selected_row_id}');
                    if (row) row.scrollIntoView({{block: 'nearest'}});
                }}, 0);"""
            )

    def slash_palette_on_text(self, text: str, cursor: int | None = None) -> None:
        from row_bot.slash_commands import (
            filter_command_specs,
            find_current_slash_token,
            get_command_specs,
        )

        current = str(text or "")
        self.draft_state["text"] = current
        found = find_current_slash_token(current, cursor if cursor is not None else len(current))
        if found is None:
            if self.slash_palette.get("open"):
                self.close_slash_palette()
            return
        _start, _end, query = found
        specs = get_command_specs(include_skills=True)
        items = filter_command_specs(specs, query, limit=max(len(specs), 1))
        if not items:
            self.close_slash_palette()
            return
        self.slash_palette.update({
            "open": True,
            "query": query,
            "index": min(int(self.slash_palette.get("index", 0) or 0), len(items) - 1),
            "items": items,
            "cursor": cursor if cursor is not None else len(current),
        })
        self._render_slash_palette()

    def _slash_palette_move(self, delta: int) -> None:
        if not self.slash_palette.get("open"):
            return
        items = list(self.slash_palette.get("items") or [])
        if not items:
            self.close_slash_palette()
            return
        self.slash_palette["index"] = (int(self.slash_palette.get("index", 0) or 0) + delta) % len(items)
        self._render_slash_palette()

    def _slash_palette_pick_selected(self) -> None:
        if not self.slash_palette.get("open"):
            return
        items = list(self.slash_palette.get("items") or [])
        if not items:
            self.close_slash_palette()
            return
        index = int(self.slash_palette.get("index", 0) or 0)
        self._execute_slash_spec(items[max(0, min(index, len(items) - 1))])
