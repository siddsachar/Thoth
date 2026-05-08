"""Designer — full-screen editor layout with chat pane, preview, and navigator."""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

from nicegui import events, run, ui

from designer.references import delete_project_reference, persist_project_references
from designer.render_assets import normalize_project_inline_assets
from designer.state import DesignerProject, normalize_designer_mode
from designer.storage import save_project
from designer.briefing import build_initial_design_request, project_has_build_brief
from designer.preview import build_preview
from designer.page_navigator import build_page_navigator
from designer.interaction import patch_html_text
from designer.ui_theme import (
    dialog_card_style,
    style_ghost_button,
    style_primary_button,
    style_secondary_button,
    surface_style,
)

logger = logging.getLogger(__name__)


def _canonicalize_stored_image_refs(project: DesignerProject) -> bool:
    """Keep stored project HTML on asset refs, not render-time data URIs."""
    return normalize_project_inline_assets(project)


def build_designer_editor(
    project: DesignerProject,
    *,
    on_back: Callable,
    send_message: Callable,
    p=None,
    state=None,
    add_chat_message: Callable | None = None,
    browse_file: Callable | None = None,
    open_settings: Callable | None = None,
) -> None:
    """Render the full-screen designer editor.

    Parameters
    ----------
    project : DesignerProject
        The project to edit.
    on_back : Callable
        Called when user clicks the back button (returns to gallery).
    send_message : Callable
        ``async def send_message(text: str)`` — sends a chat message
        through the agent with designer context.
    p : P | None
        Per-client page element references.  When provided the editor
        wires its own chat widgets into ``p`` so the streaming system
        can render assistant messages.
    state : AppState | None
        Per-client application state.
    add_chat_message : Callable | None
        ``lambda msg: add_chat_message(msg, p, thread_id)`` for rendering.
    browse_file : Callable | None
        Native file browser (macOS).
    open_settings : Callable | None
        Opens the settings dialog (for "More models…").
    """
    from designer.session import (
        get_undo_stack,
        prepare_project_mutation,
        set_active_project,
    )
    set_active_project(project)

    def _repair_reference_images() -> None:
        if _canonicalize_stored_image_refs(project):
            save_project(project)
            logger.info("Canonicalized Designer image references for project %s", project.id)

    _repair_reference_images()

    # ── Interaction callbacks ─────────────────────────────────────
    _click_info: list[dict | None] = [None]
    _preview_ref: list[dict | None] = [None]
    _nav_ref: list[dict | None] = [None]
    _notes_title_ref: list[ui.label | None] = [None]
    _notes_status_ref: list[ui.label | None] = [None]
    _notes_input_ref: list[ui.textarea | None] = [None]
    _notes_generate_btn_ref: list[ui.button | None] = [None]
    _references_ref: list[ui.column | None] = [None]
    _existing_messages = state.messages if state is not None else []

    def _render_first_draft_cta() -> None:
        if _existing_messages:
            return
        # Only show the CTA when the user actually wrote a build
        # description in the setup dialog.  Other brief fields (brand
        # preset, tone, audience) on their own produce a prompt with
        # no subject — "Turn the current blank starting point into a
        # real first draft" with nothing to draft from.  When there's
        # no description we fall through to the zero-state quick-
        # action chips instead.
        brief = project.brief
        if brief is None or not brief.build_description.strip():
            return

        request_text = build_initial_design_request(project)
        description = brief.build_description.strip() or brief.output_type

        cta_card = ui.card().classes("w-full q-ma-sm").style(
            surface_style(padding="16px", strong=True)
            + "background: linear-gradient(135deg, rgba(245,158,11,0.14), rgba(37,99,235,0.08));"
            + "border: 1px solid rgba(245,158,11,0.2);"
        )
        with cta_card:
            ui.label("Ready to build the first draft").classes("font-bold")
            ui.label(
                description or "Use the saved setup brief to generate the first real draft."
            ).classes("text-xs text-grey-4")

            build_btn = ui.button("Build First Draft", icon="auto_awesome")
            style_primary_button(build_btn, compact=True)

            async def _run_first_draft() -> None:
                build_btn.disable()
                # Remove the CTA card immediately so the user sees the
                # chat start; if the send fails we'll still have the
                # message in the thread explaining what happened.
                try:
                    cta_card.delete()
                except Exception:
                    pass
                try:
                    # Route through the same path as the normal send so
                    # any pending attached files are persisted as
                    # project references and their extracted content
                    # reaches the agent on this very first turn.
                    await _send_with_references(request_text)
                except Exception:
                    # If the send raised before producing a visible
                    # message, re-enable the button on the (now
                    # removed) card is pointless — just log and move on.
                    logger.exception("First-draft send failed")

            build_btn.on_click(lambda: asyncio.create_task(_run_first_draft()))

    def _render_zero_state_quick_actions() -> None:
        """Show per-mode quick-start chips when the project is empty.

        Only renders if there are no existing messages, no build-brief CTA
        will render, and the project has no meaningful content yet.
        """
        if _existing_messages:
            return
        brief = project.brief
        if brief is not None and brief.build_description.strip():
            # First-draft CTA takes priority when a real description
            # was supplied.
            return

        from designer.zero_state import get_quick_actions, is_project_empty

        if not is_project_empty(project, _existing_messages):
            return

        actions = get_quick_actions(project)
        if not actions:
            return

        qs_card = ui.card().classes("w-full q-ma-sm").style(
            surface_style(padding="14px", strong=True)
            + "background: linear-gradient(135deg, rgba(37,99,235,0.10), rgba(148,163,184,0.06));"
            + "border: 1px solid rgba(37,99,235,0.18);"
        )
        with qs_card:
            ui.label("Quick start").classes("font-bold")
            ui.label(
                "Pick a starting point — we'll pre-fill the message and the "
                "agent takes over."
            ).classes("text-xs text-grey-4")

            with ui.column().classes("w-full gap-1 q-mt-xs"):
                for action in actions:
                    def _make_handler(prompt: str):
                        async def _run() -> None:
                            # Dismiss the quick-start card as soon as
                            # the user commits to a starting point so
                            # the chat area takes focus.
                            try:
                                qs_card.delete()
                            except Exception:
                                pass
                            await _send_with_references(prompt)
                        return lambda: asyncio.create_task(_run())

                    btn = ui.button(
                        action.label, icon=action.icon,
                        on_click=_make_handler(action.prompt),
                    )
                    style_secondary_button(btn, compact=True)
                    btn.classes("w-full justify-start")

    def _on_element_click(detail: dict):
        """Handle click on an element in the preview iframe."""
        _click_info[0] = detail
        tag = detail.get("tag", "?")
        text = detail.get("text", "")[:40]
        logger.debug("Element clicked: <%s> %s", tag, text)

        # Phase 2.2.J — in interactive modes, offer a hotspot recorder.
        try:
            from designer.hotspot_recorder import (
                build_hotspot_recorder_spec,
                is_interactive_project,
                record_hotspot,
            )
        except Exception:
            return
        if not is_interactive_project(project):
            return
        spec = build_hotspot_recorder_spec(project, detail or {})
        if not spec.get("available"):
            return
        _open_hotspot_recorder_popover(spec, record_hotspot)

    def _open_hotspot_recorder_popover(spec: dict, record_fn) -> None:
        """Render the hotspot-recorder popover dialog."""
        action_choices = spec["action_choices"]
        route_choices = spec["route_choices"]

        selected_action = {"value": action_choices[0][0]}
        selected_route = {"value": route_choices[0][0] if route_choices else ""}
        target_input = {"value": ""}

        with ui.dialog() as dlg, ui.card().style(
            "min-width: 420px; max-width: 540px; padding: 14px 16px;"
        ):
            header = spec.get("element_tag", "element") or "element"
            preview_text = spec.get("element_text", "")
            ui.label(f"Link interaction to <{header}>").classes(
                "text-h6 text-weight-bold"
            )
            if preview_text:
                ui.label(f"\u201c{preview_text}\u201d").classes(
                    "text-xs text-grey-5"
                )
            ui.label(
                f"On screen: {spec['source_route']}"
            ).classes("text-xs text-grey-5")
            if spec.get("existing_action"):
                ui.label(
                    f"Current action: {spec['existing_action']}"
                ).classes("text-xs text-amber-5")
            ui.separator()

            action_select = ui.select(
                options={key: label for key, label in action_choices},
                value=selected_action["value"],
                label="Action",
                on_change=lambda e: (
                    selected_action.__setitem__("value", e.value),
                    _render_detail_row(),
                ),
            ).classes("w-full")

            detail_row = ui.column().classes("w-full gap-1")

            def _render_detail_row() -> None:
                detail_row.clear()
                with detail_row:
                    if selected_action["value"] == "navigate":
                        if route_choices:
                            ui.select(
                                options={k: v for k, v in route_choices},
                                value=selected_route["value"] or route_choices[0][0],
                                label="Target screen",
                                on_change=lambda e: selected_route.__setitem__(
                                    "value", e.value
                                ),
                            ).classes("w-full")
                        else:
                            ui.label(
                                "No other screens to navigate to yet."
                            ).classes("text-xs text-amber-5")
                    elif selected_action["value"] in {"toggle_state", "play_media"}:
                        if selected_action["value"] == "toggle_state":
                            ui.label(
                                "State key — a short name you give this on/off "
                                "flag (e.g. menu-open, dark, cart-open). Clicking "
                                "flips it; CSS rules keyed on "
                                "[data-thoth-state~=\u201ckey\u201d] respond."
                            ).classes("text-xs text-grey-5")
                            placeholder = "state key (e.g. menu-open)"
                            label_text = "State key"
                        else:
                            ui.label(
                                "Asset id — the data-thoth-id of a <video> or "
                                "<audio> element that should start playing."
                            ).classes("text-xs text-grey-5")
                            placeholder = "asset id"
                            label_text = "Asset id"
                        ui.input(
                            label=label_text,
                            placeholder=placeholder,
                            value=target_input["value"],
                            on_change=lambda e: target_input.__setitem__(
                                "value", e.value
                            ),
                        ).classes("w-full")
                    else:  # clear
                        ui.label(
                            "Clears any existing interaction on this element."
                        ).classes("text-xs text-grey-5")

            _render_detail_row()

            with ui.row().classes("w-full justify-end gap-2 q-mt-sm"):
                ui.button("Cancel", on_click=dlg.close).props("flat")

                def _confirm() -> None:
                    action = selected_action["value"]
                    if action == "navigate":
                        tgt = selected_route["value"]
                    elif action in {"toggle_state", "play_media"}:
                        tgt = target_input["value"]
                    else:
                        tgt = ""
                    prepare_project_mutation(project, f"hotspot_{action}")
                    ok, msg = record_fn(
                        project,
                        source_route=spec["source_route"],
                        selector=spec["selector"],
                        action=action,
                        target=tgt,
                    )
                    if ok:
                        save_project(project)
                        _refresh_editor()
                        ui.notify(msg, type="positive")
                        dlg.close()
                    else:
                        ui.notify(msg, type="negative")

                ui.button("Apply", on_click=_confirm).props("color=primary")

        dlg.open()

    def _on_text_edit(detail: dict):
        """Handle inline text edit from the preview iframe."""
        xpath = detail.get("xpath", "")
        tag = detail.get("tag", "")
        old_text = detail.get("oldText", "")
        new_text = detail.get("newText", "")
        if not old_text or not new_text or old_text == new_text:
            return

        idx = max(0, min(project.active_page, len(project.pages) - 1))
        page = project.pages[idx]

        prepare_project_mutation(project, f"inline_text_edit_page_{idx}")

        page.html = patch_html_text(page.html, xpath, tag, old_text, new_text)
        page.thumbnail_b64 = None
        save_project(project)
        _refresh_editor()
        logger.info("Inline text edit applied on page %d <%s>", idx, tag)

    def _refresh_references_panel() -> None:
        if _references_ref[0] is None:
            return
        _references_ref[0].clear()
        with _references_ref[0]:
            if not project.references:
                ui.label(
                    "No saved references yet. Files you attach here become reusable project references after send."
                ).classes("text-xs text-grey-5")
                return

            for reference in project.references:
                with ui.card().classes("w-full q-pa-sm").style(
                    "background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.08);"
                ):
                    with ui.row().classes("w-full items-start no-wrap").style("gap: 8px;"):
                        with ui.column().classes("flex-grow gap-0"):
                            ui.label(reference.name).classes("text-sm text-weight-medium")
                            meta = f"{reference.kind}"
                            if reference.suffix:
                                meta += f" · {reference.suffix}"
                            if reference.size_bytes:
                                meta += f" · {max(1, round(reference.size_bytes / 1024))} KB"
                            ui.label(meta).classes("text-xs text-grey-5")
                            if reference.summary:
                                ui.label(reference.summary).classes("text-xs text-grey-4").style(
                                    "white-space: normal; line-height: 1.35;"
                                )

                        def _remove_reference(ref_id=reference.id):
                            removed = delete_project_reference(project, ref_id)
                            if removed is None:
                                return
                            project.manual_edits.append(
                                f"User removed project reference {removed.name}."
                            )
                            save_project(project)
                            _refresh_references_panel()

                        ui.button(icon="delete", on_click=_remove_reference).props(
                            "flat dense round color=grey-6"
                        ).tooltip("Remove reference")

    def _active_page_state():
        if not project.pages:
            return None
        idx = max(0, min(project.active_page, len(project.pages) - 1))
        return idx, project.pages[idx]

    def _refresh_notes_panel() -> None:
        current = _active_page_state()
        if current is None:
            return
        idx, page = current
        if _notes_title_ref[0] is not None:
            _notes_title_ref[0].text = f"Speaker Notes · Page {idx + 1}: {page.title}"
            _notes_title_ref[0].update()
        if _notes_status_ref[0] is not None:
            _notes_status_ref[0].text = (
                f"{len(page.notes.split())} words saved"
                if page.notes.strip() else
                "No speaker notes yet"
            )
            _notes_status_ref[0].update()
        if _notes_input_ref[0] is not None and (_notes_input_ref[0].value or "") != (page.notes or ""):
            _notes_input_ref[0].value = page.notes or ""
            _notes_input_ref[0].update()

    def _save_notes(_e=None) -> None:
        current = _active_page_state()
        if current is None or _notes_input_ref[0] is None:
            return
        idx, page = current
        new_notes = (_notes_input_ref[0].value or "").strip()
        if new_notes == (page.notes or "").strip():
            _refresh_notes_panel()
            return
        prepare_project_mutation(project, f"edit_notes_page_{idx}")
        page.notes = new_notes
        project.manual_edits.append(
            f"User edited speaker notes for page {idx + 1} \"{page.title}\"."
        )
        save_project(project)
        _refresh_notes_panel()
        if _nav_ref[0]:
            _nav_ref[0]["refresh"]()

    async def _generate_notes_for_active_page() -> None:
        current = _active_page_state()
        if current is None:
            return
        idx, page = current
        if not page.html.strip():
            ui.notify("The active page has no content to summarize.", type="warning")
            return
        try:
            from designer.ai_content import generate_speaker_notes
            from designer.html_ops import summarize_page_html

            if _notes_generate_btn_ref[0] is not None:
                _notes_generate_btn_ref[0].disable()
            if _notes_status_ref[0] is not None:
                _notes_status_ref[0].text = "Generating speaker notes…"
                _notes_status_ref[0].update()
            summary = summarize_page_html(page.html)
            generated = await run.io_bound(generate_speaker_notes, page.title, summary, page.notes)
            generated = (generated or "").strip()
            if not generated:
                ui.notify("No speaker notes were generated.", type="warning")
                _refresh_notes_panel()
                return
            if generated == (page.notes or "").strip():
                ui.notify("Speaker notes are already up to date.", type="info")
                _refresh_notes_panel()
                return
            prepare_project_mutation(project, f"generate_notes_page_{idx}")
            page.notes = generated
            project.manual_edits.append(
                f"User generated speaker notes for page {idx + 1} \"{page.title}\"."
            )
            save_project(project)
            _refresh_notes_panel()
            if _nav_ref[0]:
                _nav_ref[0]["refresh"]()
            ui.notify("Speaker notes generated.", type="positive")
        except Exception as exc:
            logger.exception("Failed to generate speaker notes")
            ui.notify(f"Failed to generate speaker notes: {exc}", type="negative")
            _refresh_notes_panel()
        finally:
            if _notes_generate_btn_ref[0] is not None:
                _notes_generate_btn_ref[0].enable()

    # ── Header bar ────────────────────────────────────────────────────
    with ui.row().classes("w-full items-center shrink-0").style(
        "padding: 8px 16px; background: rgba(0,0,0,0.5); "
        "border-bottom: 1px solid rgba(255,255,255,0.08);"
    ):
        def _go_back():
            # Pin the home tab back to Designer before rebuilding so the
            # user returns to the gallery instead of bouncing through the
            # default "Workflows" tab.
            try:
                from ui.state import state as _state
                _state.preferred_home_tab = "Designer"
            except Exception:
                pass
            set_active_project(None)
            on_back()

        ui.button(icon="arrow_back", on_click=_go_back).props(
            "flat dense round"
        ).tooltip("Back to Gallery")

        # Editable project name
        name_input = ui.input(value=project.name).props(
            "dense borderless"
        ).classes("text-h6").style(
            "flex: 1; max-width: 400px; font-weight: 600;"
        )

        def _refresh_editor(*, force_preview: bool = False):
            try:
                name_input.value = project.name
                name_input.update()
            except RuntimeError:
                pass
            _refresh_notes_panel()
            if _preview_ref[0]:
                try:
                    _preview_ref[0]["refresh"](force=force_preview)
                except RuntimeError:
                    pass
            if _nav_ref[0]:
                try:
                    _nav_ref[0]["refresh"]()
                except RuntimeError:
                    pass

        def _rename(_e=None):
            new_name = name_input.value.strip() if name_input.value else ""
            if new_name and new_name != project.name:
                prepare_project_mutation(project, "rename_project")
                project.name = new_name
                save_project(project)
                # Keep the linked thread name in sync
                if project.thread_id:
                    from threads import _save_thread_meta
                    _save_thread_meta(project.thread_id, f"🎨 {new_name}")
                _refresh_editor()

        name_input.on("blur", _rename)
        name_input.on("keydown.enter", lambda e: name_input.run_method("blur"))

        ui.element("div").style("flex: 1;")  # spacer

        # ── Mode-aware toolbar gating ──────────────────────────────
        # Each button below is shown only for modes where it makes
        # sense. Modes: deck, landing, app_mockup, storyboard, document.
        from designer.preview import INTERACTIVE_MODES as _INT_MODES
        _project_mode = getattr(project, "mode", "deck") or "deck"

        # Edit / Preview toggle — only for interactive modes. In Edit
        # mode clicks drive the authoring bridge (hotspot recorder,
        # inline text edit). In Preview mode clicks flow through to the
        # runtime bridge so prototypes can be exercised in place.
        if _project_mode in _INT_MODES:
            _mode_toggle_ref: list = [None]
            _mode_hint_ref: list = [None]

            def _apply_mode(e):
                which = e.value if hasattr(e, "value") else e
                is_preview = (which == "Preview")
                ref = _preview_ref[0]
                if ref and "set_preview_mode" in ref:
                    try:
                        ref["set_preview_mode"](is_preview)
                    except Exception:
                        pass
                # Update status label so the user gets unambiguous feedback
                # even if the Quasar selected-state styling is subtle.
                hint = _mode_hint_ref[0]
                if hint is not None:
                    try:
                        if is_preview:
                            hint.text = "Preview — clicks trigger prototype"
                            hint.classes(replace="text-xs text-primary q-ml-sm")
                        else:
                            hint.text = "Edit — click to select / edit"
                            hint.classes(replace="text-xs text-grey-6 q-ml-sm")
                    except Exception:
                        pass
                try:
                    ui.notify(
                        "Preview mode enabled — clicks run interactions."
                        if is_preview else
                        "Edit mode — clicks select elements.",
                        type="info", position="bottom", timeout=1500,
                    )
                except Exception:
                    pass

            with ui.row().classes("items-center gap-0").style("margin-right: 8px;"):
                _mode_toggle_ref[0] = ui.toggle(
                    ["Edit", "Preview"],
                    value="Edit",
                    on_change=_apply_mode,
                ).props(
                    # Quasar QBtnToggle: `color` = unselected fill,
                    # `toggle-color` = selected fill. Without splitting
                    # these the two segments look identical.
                    "dense no-caps rounded unelevated "
                    "color=grey-9 text-color=grey-4 "
                    "toggle-color=primary toggle-text-color=white"
                ).tooltip(
                    "Edit = click to select / edit · Preview = clicks "
                    "trigger interactions (links, toggles, navigation)"
                )
                _mode_hint_ref[0] = ui.label(
                    "Edit — click to select / edit"
                ).classes("text-xs text-grey-6 q-ml-sm")

        # Present — deck + storyboard only. app_mockup / landing /
        # document have no slide semantics.
        if _project_mode in {"deck", "storyboard"}:
            async def _show_presentation():
                from designer.presentation import show_presentation
                await show_presentation(project)

            ui.button(icon="play_arrow", on_click=_show_presentation).props(
                "outline dense round color=grey-6"
            ).tooltip("Present")

        # Brand — all modes
        def _show_brand():
            from designer.brand_dialog import show_brand_dialog
            show_brand_dialog(project, on_apply=_refresh_editor)

        ui.button(icon="palette", on_click=_show_brand).props(
            "outline dense round color=grey-6"
        ).tooltip("Brand & Theme")

        # Curated blocks — deck + landing only. The bundled blocks are
        # desktop-slide / marketing-page oriented (Story, Evidence,
        # Conversion). They don't fit app_mockup (mobile), storyboard
        # (film), or document (long-form copy).
        if _project_mode in {"deck", "landing"}:
            def _show_blocks():
                from designer.components import list_components, render_component_html
                from designer.html_ops import insert_component_in_html

                components = list_components()
                categories = []
                for component in components:
                    if component.category not in categories:
                        categories.append(component.category)

                with ui.dialog() as dlg, ui.card().style(
                    dialog_card_style(min_width="820px", max_width="980px", max_height="84vh")
                ):
                    ui.label("Curated Blocks").classes("text-h6 text-weight-bold")
                    ui.label(
                        "Insert reusable sections into the active page. These blocks are brand-aware and remain editable with the normal Designer tools."
                    ).classes("text-sm text-grey-5 q-mb-sm")

                    with ui.tabs().classes("w-full") as tabs:
                        for category in categories:
                            ui.tab(category, label=category)

                    with ui.tab_panels(tabs, value=categories[0]).classes("w-full"):
                        for category in categories:
                            with ui.tab_panel(category):
                                with ui.grid(columns=2).classes("w-full gap-3"):
                                    for component in [c for c in components if c.category == category]:
                                        with ui.card().classes("q-pa-md").style(
                                            "background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.08);"
                                        ):
                                            ui.label(component.label).classes("text-subtitle1 text-weight-medium")
                                            ui.label(component.description).classes("text-sm text-grey-5")
                                            if component.tags:
                                                with ui.row().classes("w-full flex-wrap gap-1 q-mt-sm"):
                                                    for tag in component.tags:
                                                        ui.badge(tag, color="grey-8").props("outline")

                                            def _insert_curated_block(component_name=component.name):
                                                idx = max(0, min(project.active_page, len(project.pages) - 1))
                                                prepare_project_mutation(project, f"insert_component_{component_name}")
                                                page = project.pages[idx]
                                                component_html = render_component_html(component_name)
                                                page.html, element_id, selector_hint = insert_component_in_html(
                                                    page.html,
                                                    component_html,
                                                    component_name,
                                                )
                                                page.thumbnail_b64 = None
                                                project.manual_edits.append(
                                                    f"User inserted the {component_name} curated block on page {idx + 1}."
                                                )
                                                save_project(project)
                                                _refresh_editor(force_preview=True)
                                                logger.info(
                                                    "Inserted curated block %s on page %d (%s / %s)",
                                                    component_name,
                                                    idx + 1,
                                                    element_id,
                                                    selector_hint,
                                                )
                                                dlg.close()

                                            insert_btn = ui.button(
                                                "Insert on current page",
                                                icon="add_box",
                                                on_click=_insert_curated_block,
                                            ).classes("q-mt-sm")
                                            style_secondary_button(insert_btn, compact=True)

                    with ui.row().classes("w-full justify-end q-mt-sm"):
                        close_blocks_btn = ui.button("Close", on_click=dlg.close)
                        style_ghost_button(close_blocks_btn)

                dlg.open()

            ui.button(icon="view_quilt", on_click=_show_blocks).props(
                "outline dense round color=grey-6"
            ).tooltip("Curated Blocks")

        # Import PPTX / DOCX — deck + document only.
        if _project_mode in {"deck", "document"}:
            def _show_import():
                from designer.import_dialog import show_import_dialog
                show_import_dialog(project, on_done=_refresh_editor)

            ui.button(icon="upload_file", on_click=_show_import).props(
                "outline dense round color=grey-6"
            ).tooltip("Import PPTX / DOCX")

        # History button
        def _show_history():
            _open_history_dialog(project, on_restore=_refresh_editor)

        ui.button(icon="history", on_click=_show_history).props(
            "outline dense round color=grey-6"
        ).tooltip("Version History")

        # Unified Review & Repair button (replaces Page Review + Brand Lint)
        def _open_review() -> None:
            from designer.review_dialog import open_review_dialog

            async def _send(text: str) -> None:
                await send_message(text)

            open_review_dialog(
                project,
                refresh_editor=_refresh_editor,
                send_agent_message=_send,
            )

        ui.button(icon="rule", on_click=_open_review).props(
            "outline dense round color=grey-6"
        ).tooltip("Review & Repair (Ctrl/Cmd+Shift+R)")

        # Undo / Redo
        def _undo():
            stack = get_undo_stack()
            if stack and stack.undo(project):
                project.manual_edits.append(
                    f"User pressed Undo. Now {len(project.pages)} pages."
                )
                save_project(project)
                _refresh_editor(force_preview=True)

        def _redo():
            stack = get_undo_stack()
            if stack and stack.redo(project):
                project.manual_edits.append(
                    f"User pressed Redo. Now {len(project.pages)} pages."
                )
                save_project(project)
                _refresh_editor(force_preview=True)

        def _open_command_palette() -> None:
            from designer.command_palette import open_command_palette
            from designer.tool import DesignerTool

            try:
                tools = DesignerTool().as_langchain_tools()
                tool_names = [t.name for t in tools]
            except Exception:
                tool_names = []

            def _prefill(text: str) -> None:
                try:
                    if p is not None and getattr(p, "chat_input", None) is not None:
                        p.chat_input.value = text
                        try:
                            p.chat_input.run_method("focus")
                        except Exception:
                            pass
                except Exception:
                    pass

            def _navigate(idx: int) -> None:
                if 0 <= idx < len(project.pages):
                    project.active_page = idx
                    save_project(project)
                    _refresh_editor(force_preview=True)

            open_command_palette(
                project,
                tool_names=tool_names,
                prefill_input=_prefill,
                on_navigate_page=_navigate,
            )

        def _handle_designer_shortcut(e):
            shortcut = (e.args or {}).get("shortcut")
            if shortcut == "redo":
                _redo()
            elif shortcut == "undo":
                _undo()
            elif shortcut == "palette":
                _open_command_palette()
            elif shortcut == "review":
                _open_review()

        ui.keyboard(repeating=False).on(
            "key",
            _handle_designer_shortcut,
            js_handler="""(e) => {
                const key = (e.key || '').toLowerCase();
                if (e.action !== 'keydown') return;
                if (!(e.ctrlKey || e.metaKey)) return;
                if (key === 'k') {
                    const ae = document.activeElement;
                    const tag = ae ? (ae.tagName || '').toLowerCase() : '';
                    const editable = ae && (ae.isContentEditable || tag === 'input' || tag === 'textarea');
                    if (editable) return;
                    emit({shortcut: 'palette'});
                    e.event.preventDefault();
                    return;
                }
                if (key === 'r' && e.shiftKey) {
                    const ae = document.activeElement;
                    const tag = ae ? (ae.tagName || '').toLowerCase() : '';
                    const editable = ae && (ae.isContentEditable || tag === 'input' || tag === 'textarea');
                    if (editable) return;
                    emit({shortcut: 'review'});
                    e.event.preventDefault();
                    return;
                }
                if (key !== 'z') return;
                emit({shortcut: e.shiftKey ? 'redo' : 'undo'});
                e.event.preventDefault();
            }""",
        )

        ui.button(icon="search", on_click=_open_command_palette).props(
            "flat dense round color=grey-6"
        ).tooltip("Command palette (Ctrl/Cmd+K)")

        ui.button(icon="undo", on_click=_undo).props(
            "flat dense round color=grey-6"
        ).tooltip("Undo (Ctrl/Cmd+Z)")

        ui.button(icon="redo", on_click=_redo).props(
            "flat dense round color=grey-6"
        ).tooltip("Redo (Ctrl/Cmd+Shift+Z)")

        # Export button
        def _show_export():
            from designer.export_dialog import show_export_dialog
            show_export_dialog(project)

        def _show_share():
            from designer.share_dialog import show_share_dialog
            show_share_dialog(project)

        share_btn = ui.button("Share", icon="share", on_click=_show_share)
        style_secondary_button(share_btn, compact=True)

        export_btn = ui.button("Export", icon="download", on_click=_show_export)
        style_primary_button(export_btn, compact=True)

    # ── Main content: splitter (chat left, preview+nav right) ─────────
    with ui.splitter(value=35).classes("w-full flex-grow").style(
        "overflow: hidden;"
    ) as splitter:

        # ── Left pane: Chat ──────────────────────────────────────────
        with splitter.before:
            with ui.column().classes("w-full h-full").style(
                "background: rgba(0,0,0,0.3);"
            ):
                if state is not None and p is not None:
                    from ui.chat_components import (
                        build_chat_messages,
                        build_file_upload,
                        build_chat_input_bar,
                    )

                    _render_first_draft_cta()
                    _render_zero_state_quick_actions()

                    async def _send_with_references(text: str) -> None:
                        pending_snapshot = [
                            {
                                "name": item.get("name", ""),
                                "data": bytes(item.get("data", b"")),
                            }
                            for item in p.pending_files
                            if item.get("name") and item.get("data")
                        ]
                        if pending_snapshot:
                            added_refs = await run.io_bound(
                                persist_project_references,
                                project,
                                pending_snapshot,
                                state.vision_service,
                                state.attached_data_cache,
                                state.thread_model_override or None,
                            )
                            if added_refs:
                                added_names = ", ".join(ref.name for ref in added_refs[:4])
                                if len(added_refs) > 4:
                                    added_names += ", ..."
                                project.manual_edits.append(
                                    f"User added {len(added_refs)} project reference(s): {added_names}."
                                )
                                save_project(project)
                                _refresh_references_panel()
                        await send_message(text)

                    # File upload (hidden widget + drag-drop + paste)
                    _hidden_upload = build_file_upload(p, state)

                    with ui.expansion("References", icon="collections_bookmark").classes(
                        "w-full shrink-0"
                    ).props("dense default-opened"):
                        with ui.row().classes("w-full items-center justify-between").style("gap: 8px;"):
                            with ui.column().classes("gap-0"):
                                ui.label("Project references").classes("text-sm text-weight-medium")
                                ui.label(
                                    "Use the normal attach flow below. Attached files are saved here after you send your next Designer message."
                                ).classes("text-xs text-grey-5")

                            async def _open_reference_upload() -> None:
                                await ui.run_javascript(
                                    f"document.getElementById('c{_hidden_upload.id}').querySelector('input[type=file]').click()"
                                )

                            ui.button("Add files", icon="attach_file").props(
                                "flat dense no-caps color=grey-6"
                            ).on_click(lambda: asyncio.create_task(_open_reference_upload()))

                        _references_ref[0] = ui.column().classes("w-full gap-2 q-mt-sm")
                        _refresh_references_panel()

                    # Render messages from the thread (state.messages)
                    _msgs = state.messages or []
                    build_chat_messages(
                        p, state,
                        messages=_msgs,
                        add_chat_message=add_chat_message,
                        placeholder_text="Describe what you want to create or change.",
                    )

                    # Full input bar (textarea, attach, voice, send, stop, model picker)
                    build_chat_input_bar(
                        p, state,
                        send_fn=_send_with_references,
                        hidden_upload=_hidden_upload,
                        browse_file=browse_file,
                        open_settings=open_settings,
                        show_model_picker=True,
                    )
                else:
                    # Fallback: minimal chat using the current thread messages when available.
                    _fallback_messages = state.messages if state is not None else []
                    _render_first_draft_cta()
                    _render_zero_state_quick_actions()
                    with ui.scroll_area().classes("w-full flex-grow") as _fb_scroll:
                        _fb_container = ui.column().classes("w-full q-pa-sm gap-2")
                        if p is not None:
                            p.chat_container = _fb_container
                            p.chat_scroll = _fb_scroll
                        with _fb_container:
                            for msg in _fallback_messages:
                                _render_chat_bubble(msg)
                            if not _fallback_messages:
                                ui.label(
                                    "Describe what you want to create or change."
                                ).classes("text-grey-5 text-sm q-pa-md")

                    with ui.row().classes("w-full items-end shrink-0").style(
                        "padding: 8px; border-top: 1px solid rgba(255,255,255,0.08);"
                    ):
                        _fb_input = ui.textarea(
                            placeholder="Describe your design…"
                        ).props("dense outlined autogrow").classes("flex-grow").style(
                            "max-height: 120px;"
                        )

                        async def _fb_send():
                            text = _fb_input.value.strip()
                            if not text:
                                return
                            _fb_input.value = ""
                            await send_message(text)

                        fallback_send_btn = ui.button(icon="send", on_click=_fb_send)
                        style_primary_button(fallback_send_btn, compact=True, round=True)
                        _fb_input.on(
                            "keydown.enter",
                            _fb_send,
                            js_handler="""(e) => {
                                if (e.shiftKey || e.ctrlKey || e.metaKey || e.altKey) return;
                                e.preventDefault();
                                emit();
                            }""",
                        )

        # ── Right pane: Preview + Navigator ──────────────────────────
        with splitter.after:
            with ui.column().classes("w-full h-full no-wrap").style("overflow: hidden; min-height: 0;"):
                # Preview area (takes most space)
                # Page navigator strip (built first, wired to preview below)
                def _on_nav_from_preview():
                    """Called by preview timer when page structure changes."""
                    if _nav_ref[0]:
                        _nav_ref[0]["refresh"]()

                with ui.element("div").classes("w-full").style(
                    "flex: 1 1 auto; min-height: 0; overflow: hidden;"
                ):
                    _preview_ref[0] = build_preview(
                        project,
                        on_element_click=_on_element_click,
                        on_text_edit=_on_text_edit,
                        on_undo_shortcut=_undo,
                        on_redo_shortcut=_redo,
                        on_navigate=_on_nav_from_preview,
                    )

                # Speaker notes are only relevant for deck-style modes
                # that export to PPTX notes slides / presenter mode.
                # Landing pages, app mockups, and documents have no
                # presenter surface, so the panel is hidden there to
                # cut clutter.
                _notes_modes = {"deck", "storyboard"}
                _show_notes = normalize_designer_mode(project.mode) in _notes_modes

                if _show_notes:
                    with ui.card().classes("w-full q-ma-sm q-mt-xs shrink-0").style(
                        "min-height: 220px; max-height: 34vh; overflow: hidden; "
                        "background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.08);"
                    ):
                        with ui.column().classes("w-full h-full").style("gap: 8px; min-height: 0;"):
                            with ui.row().classes("w-full items-center justify-between"):
                                _notes_title_ref[0] = ui.label("Speaker Notes").classes("text-sm text-weight-medium")
                                _notes_status_ref[0] = ui.label("").classes("text-xs text-grey-5")
                                with ui.row().classes("items-center gap-1"):
                                    ui.button(
                                        "Save",
                                        icon="save",
                                        on_click=_save_notes,
                                    ).props("flat dense no-caps color=grey-6")
                                    _notes_generate_btn_ref[0] = ui.button(
                                        "Generate Notes",
                                        icon="auto_awesome",
                                        on_click=_generate_notes_for_active_page,
                                    )
                                    style_primary_button(_notes_generate_btn_ref[0], compact=True)
                            ui.label(
                                "Notes are saved per page and appear in presenter mode and PPTX notes slides."
                            ).classes("text-xs text-grey-5")
                            _notes_input_ref[0] = ui.textarea(
                                placeholder="Add speaker notes for the active page…",
                            ).props(
                                'dense outlined input-style="min-height: 180px; max-height: 100%; overflow: auto;"'
                            ).classes("w-full").style("flex: 1 1 auto; min-height: 180px;")
                            _notes_input_ref[0].on("blur", _save_notes)
                            _refresh_notes_panel()

                def _on_page_change():
                    if _preview_ref[0]:
                        _preview_ref[0]["refresh"]()
                    _refresh_notes_panel()

                nav = build_page_navigator(
                    project,
                    on_page_change=_on_page_change,
                )
                _nav_ref[0] = nav


def _render_chat_bubble(msg: dict) -> None:
    """Render a single chat message bubble."""
    role = msg.get("role", "user")
    content = msg.get("content", "")
    # Shared bubble styling that guarantees long content wraps instead
    # of producing a horizontal scroll bar when the chat pane is narrow.
    _bubble_wrap = (
        "max-width: 85%; min-width: 0; white-space: pre-wrap; "
        "word-break: break-word; overflow-wrap: anywhere; overflow-x: hidden;"
    )
    if role == "user":
        with ui.row().classes("w-full justify-end").style("min-width: 0;"):
            ui.label(content).classes("q-pa-sm thoth-designer-bubble").style(
                "background: rgba(37,99,235,0.2); border-radius: 12px 12px 0 12px; "
                + _bubble_wrap
            )
    else:
        with ui.row().classes("w-full").style("min-width: 0;"):
            ui.markdown(content).classes("q-pa-sm thoth-designer-bubble").style(
                "background: rgba(255,255,255,0.05); border-radius: 12px 12px 12px 0; "
                + _bubble_wrap
            )


def _open_history_dialog(
    project: DesignerProject,
    on_restore: Callable[[], None] | None = None,
) -> None:
    """Open a dialog showing version history snapshots with restore buttons."""
    from designer.history import list_snapshots, restore_snapshot
    from designer.session import prepare_project_mutation
    from designer.storage import save_project
    from datetime import datetime

    with ui.dialog() as dlg, ui.card().style(
        dialog_card_style(min_width="450px", max_width="560px", max_height="80vh")
    ):
        ui.label("Version History").classes("text-h6 text-weight-bold q-mb-sm")
        snaps = list_snapshots(project.id)

        if not snaps:
            ui.label("No version history yet. History is saved automatically "
                     "before each change.").classes("text-grey-5 text-sm")
        else:
            ui.label(f"{len(snaps)} snapshot(s)").classes("text-grey-5 text-sm q-mb-sm")
            with ui.scroll_area().style("max-height: 400px;"):
                for snap in snaps:
                    ts = snap.get("timestamp", 0)
                    try:
                        dt = datetime.fromtimestamp(float(ts))
                        time_str = dt.strftime("%b %d, %H:%M:%S")
                    except (ValueError, OSError):
                        time_str = str(ts)
                    label = snap.get("label", "")
                    pages = snap.get("page_count", "?")

                    with ui.row().classes("w-full items-center gap-2").style(
                        "padding: 6px 8px; background: rgba(255,255,255,0.04);"
                        "border-radius: 6px; margin-bottom: 4px;"
                    ):
                        with ui.column().classes("flex-grow").style("gap: 0;"):
                            ui.label(time_str).classes("text-sm")
                            desc = f"{pages} pages"
                            if label:
                                desc += f" · {label}"
                            ui.label(desc).classes("text-xs text-grey-5")

                        def _restore(sid=snap["id"]):
                            prepare_project_mutation(project, f"restore_snapshot_{sid}")
                            if restore_snapshot(project, sid):
                                project.manual_edits.append(
                                    f"User restored version snapshot {sid}."
                                )
                                save_project(project)
                                if on_restore:
                                    on_restore(force_preview=True)
                            dlg.close()

                        restore_btn = ui.button("Restore", on_click=_restore)
                        style_secondary_button(restore_btn, compact=True)

        with ui.row().classes("w-full justify-end q-mt-md"):
            close_history_btn = ui.button("Close", on_click=dlg.close)
            style_ghost_button(close_history_btn)

    dlg.open()
