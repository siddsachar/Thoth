"""Designer — template gallery modal for creating new projects."""

from __future__ import annotations

import logging
from typing import Callable

from row_bot.brand import APP_BRAND_ACCENT, APP_BRAND_ACCENT_RGB
from nicegui import run, ui

from row_bot.designer.brand import extract_brand_from_url, get_all_presets
from row_bot.designer.briefing import brief_has_build_content
from row_bot.designer.setup_flow import (
    DESIGNER_MODE_PICKER_CHOICES,
    MODE_CHOICE_AUTO,
    canvas_choices_for_mode,
    default_project_name_for_template,
    infer_output_type_for_template,
    prepare_project_creation,
    resolve_project_brand,
)
from row_bot.designer.state import DesignerProject, ProjectBrief, ASPECT_RATIOS, default_aspect_for_mode
from row_bot.designer.storage import save_project
from row_bot.designer.templates import get_templates, get_template, get_template_categories, get_templates_for_mode
from row_bot.designer.ui_theme import (
    SECTION_LABEL_CLASSES,
    SECTION_LABEL_STYLE,
    dialog_card_style,
    style_choice_button,
    style_ghost_button,
    style_primary_button,
    style_secondary_button,
    surface_style,
)

logger = logging.getLogger(__name__)

_OUTPUT_TYPE_OPTIONS = [
    "Presentation",
    "Pitch deck",
    "One-pager",
    "Status report",
    "Landing page",
    "Social media set",
    "Wireframe kit",
]
_AUDIENCE_OPTIONS = [
    "Investors",
    "Customers",
    "Executives",
    "Internal team",
    "Partners",
    "Prospects",
]
_TONE_OPTIONS = [
    "Confident",
    "Modern",
    "Editorial",
    "Formal",
    "Bold",
    "Friendly",
]
_LENGTH_OPTIONS = [
    "3 slides",
    "5 slides",
    "10 slides",
    "1 page",
    "Short overview",
    "Detailed walkthrough",
]

# Phase 2.3.I (dialog v3) \u2014 mode-tailored example prompts for the
# "Example prompts" inspiration chip. Keys match mode IDs.
_EXAMPLE_PROMPTS_BY_MODE = {
    "deck": [
        "A pitch deck for a B2B SaaS Series A raise — 10 slides, investor-ready.",
        "An internal product launch readout — status, risks, next milestones.",
        "A quarterly business review deck — wins, lowlights, and a clear ask.",
        "A conference keynote on the future of AI agents — bold, editorial.",
        "A customer case study deck — problem, solution, measured results.",
    ],
    "document": [
        "A quarterly status report for a platform team — goals, progress, risks.",
        "A one-page product requirements doc for a new feature.",
        "A research brief summarising 3 recent papers with implications.",
        "A meeting recap with decisions, action items, and open questions.",
        "A 2-page marketing one-pager for a B2B analytics product.",
    ],
    "landing": [
        "A landing page for a fitness app launch — hero, features, pricing, FAQ.",
        "A SaaS free-trial landing page with strong social proof.",
        "A course registration page with curriculum and instructor bio.",
        "A product waitlist page with email capture and feature preview.",
        "An event landing page with agenda, speakers, and registration.",
    ],
    "app_mockup": [
        "A task manager with projects, quick-add, and a today view.",
        "A travel planner: itinerary, budget, and map-based discovery.",
        "A personal finance dashboard with accounts, spending, and goals.",
        "A habit tracker with streaks, reminders, and weekly insights.",
        "A recipe app with meal planner, shopping list, and macros.",
    ],
    "storyboard": [
        "A 4-shot product demo: problem, aha-moment, result, CTA.",
        "A 6-shot explainer for a new feature — setup to success.",
        "A 3-shot onboarding sequence — welcome, first win, habit.",
        "A before/after transformation in 4 frames with clear contrast.",
        "A day-in-the-life mini-story for the target persona.",
    ],
}


def show_template_gallery(
    *,
    on_project_created: Callable[[DesignerProject], None],
) -> None:
    """Backward-compatible wrapper for the unified new-project dialog."""

    show_new_project_dialog(
        on_project_created=on_project_created,
        initial_template_id="blank_deck",
    )


def show_blank_canvas_picker(
    *,
    on_project_created: Callable[[DesignerProject], None],
) -> None:
    """Backward-compatible wrapper for the unified new-project dialog."""

    show_new_project_dialog(
        on_project_created=on_project_created,
        initial_template_id="blank_deck",
    )


def show_new_project_dialog(
    *,
    on_project_created: Callable,
    initial_template_id: str = "blank_deck",
) -> None:
    """Open the unified Designer setup dialog."""

    templates = get_templates()
    categories = ["All", *get_template_categories()]
    presets = get_all_presets()
    selected_template_id = {
        "value": initial_template_id if get_template(initial_template_id) else "blank_canvas"
    }
    # Phase 2.3.I (dialog v3) — default to "All" so the user sees the
    # full library up front. Category is preserved across mode changes
    # when still populated in the new mode.
    selected_category = {"value": "All"}
    selected_preset = {"value": "Default Dark"}
    # Phase 2.3.C — mode picker seeds from the selected template's
    # declared mode; there is no "auto" in the UI picker.
    _initial_tmpl_mode = (get_template(selected_template_id["value"]) or get_template("blank_canvas")).mode or "deck"
    selected_mode = {"value": _initial_tmpl_mode}

    # Phase 2.3.D — helper to pick the default starter template for a
    # given mode. Each mode's "blank_<mode>" starter wins; falls back
    # to the first template in the mode slice, else blank_canvas.
    _STARTER_BY_MODE = {
        "deck":       "blank_deck",
        "document":   "blank_document",
        "landing":    "blank_landing",
        "app_mockup": "blank_app_mockup",
        "storyboard": "blank_storyboard",
    }
    def _starter_for_mode(mode: str) -> str:
        starter_id = _STARTER_BY_MODE.get(mode)
        if starter_id and get_template(starter_id):
            return starter_id
        mode_slice = get_templates_for_mode(mode)
        if mode_slice:
            return mode_slice[0].id
        return "blank_canvas"
    extracted_brand = {"value": None}
    extraction_label = {"value": "Using preset brand."}
    current_effective_brand = {
        "value": resolve_project_brand(preset_name=selected_preset["value"])
    }
    last_default_name = {
        "value": default_project_name_for_template(selected_template_id["value"])
    }
    last_default_ratio = {
        "value": (get_template(selected_template_id["value"]) or get_template("blank_canvas")).aspect_ratio
    }

    def _selected_template():
        return get_template(selected_template_id["value"]) or get_template("blank_canvas")

    def _resolved_output_type(raw_value: str = "") -> str:
        if selected_template_id["value"] == "blank_canvas":
            return raw_value.strip()
        return infer_output_type_for_template(selected_template_id["value"])

    def _brand_preview_html(label: str) -> str:
        brand = current_effective_brand["value"]
        return (
            '<div style="padding:16px;border-radius:18px;border:1px solid rgba(148,163,184,0.14);'
            'background:linear-gradient(180deg, rgba(15,23,42,0.62), rgba(15,23,42,0.36));">'
            f'<div style="font-size:0.76rem;letter-spacing:0.1em;text-transform:uppercase;opacity:0.72;margin-bottom:10px;color:#94a3b8;">{label}</div>'
            '<div style="display:flex;gap:8px;margin-bottom:10px;">'
            f'<div style="width:30px;height:30px;border-radius:10px;background:{brand.primary_color};box-shadow:inset 0 1px 0 rgba(255,255,255,0.25);"></div>'
            f'<div style="width:30px;height:30px;border-radius:10px;background:{brand.secondary_color};box-shadow:inset 0 1px 0 rgba(255,255,255,0.25);"></div>'
            f'<div style="width:30px;height:30px;border-radius:10px;background:{brand.accent_color};box-shadow:inset 0 1px 0 rgba(255,255,255,0.25);"></div>'
            f'<div style="width:30px;height:30px;border-radius:10px;background:{brand.bg_color};border:1px solid rgba(255,255,255,0.12);"></div>'
            '</div>'
            f'<div style="font-size:0.84rem;opacity:0.9;color:#e2e8f0;">{brand.heading_font} / {brand.body_font}</div>'
            '</div>'
        )

    # Phase 2.3.H (dialog v2) — Mode bar promoted to top, segmented
    # pills. Right column collapses to Project + Description + Brand
    # cards with optional fields tucked behind expansions. Selected
    # template summary card is replaced by a one-line caption beneath
    # the grid. Footer is a sticky [Cancel] [Create →] row; Create
    # disabled until a project name exists.
    with ui.dialog().props("maximized") as dlg, ui.card().classes(
        "w-full h-full no-wrap column"
    ).style(dialog_card_style(max_width="1320px", height="calc(100vh - 48px)", padding="20px 24px 16px")):
        # ─── Header ──────────────────────────────────────────────────
        with ui.row().classes("w-full items-center justify-between no-wrap"):
            with ui.column().classes("gap-0"):
                ui.label("New Design").classes("text-h5 text-weight-bold")
                ui.label("Pick what you're making, then describe the first draft.").classes(
                    "text-sm text-grey-5"
                )
            close_btn = ui.button(icon="close", on_click=dlg.close).props("flat dense round")
            style_ghost_button(close_btn, compact=True)

        # ─── Mode bar (segmented pills) ──────────────────────────────
        # "What are you making?" is the primary control. Seeded from
        # the selected template's mode. When changed, it filters the
        # gallery and resets the canvas control.
        with ui.column().classes("w-full gap-1 q-mt-sm"):
            ui.label("What are you making?").classes("text-xs text-grey-5").style(
                "letter-spacing:0.12em;text-transform:uppercase;"
            )
            _mode_options = {key: label for key, label in DESIGNER_MODE_PICKER_CHOICES}
            mode_toggle = ui.toggle(
                options=_mode_options,
                value=selected_mode["value"],
                on_change=lambda e: _on_mode_change(e),
            ).props(
                "color=blue-grey-9 text-color=blue-3 "
                "toggle-color=primary toggle-text-color=white "
                "unelevated dense spread no-caps"
            ).classes("w-full").style(
                f"--q-primary:{APP_BRAND_ACCENT};"
            )

        ui.separator().classes("q-my-sm")

        # ─── Two columns ─────────────────────────────────────────────
        with ui.splitter(value=58).classes("w-full flex-grow").style("min-height: 0;") as splitter:
            # LEFT: gallery
            with splitter.before:
                with ui.column().classes("w-full h-full gap-2 no-wrap").style("padding-right: 14px; min-height: 0;"):
                    ui.label("TEMPLATE LIBRARY").classes("text-xs text-grey-5").style(
                        "letter-spacing:0.12em;"
                    )
                    with ui.row().classes("w-full flex-wrap gap-2") as _category_row:
                        pass
                    with ui.scroll_area().classes("w-full flex-grow").style("min-height: 0;"):
                        _grid = ui.column().classes("w-full")
                    _caption = ui.label("").classes("text-sm text-grey-4 q-mt-xs").style(
                        "min-height:1.2rem;"
                    )

            # RIGHT: project / description / brand
            with splitter.after:
                with ui.scroll_area().classes("w-full h-full").style("padding-left: 14px; min-height: 0;"):
                    with ui.column().classes("w-full gap-4"):
                        # ── Project card ────────────────────────────
                        with ui.card().classes("w-full").style(surface_style(padding="18px", strong=True)):
                            ui.label("PROJECT BASICS").classes("text-xs text-grey-5 q-mb-sm").style(
                                "letter-spacing:0.12em;"
                            )
                            name_input = ui.input(
                                label="Project name",
                                value=default_project_name_for_template(selected_template_id["value"]),
                            ).props("dense outlined").classes("w-full")

                            # Phase 2.3.E — canvas control is scoped to
                            # the selected mode. Re-rendered whenever
                            # mode changes. Per-mode sticky pick so
                            # flipping mode away and back restores the
                            # user's previous override.
                            _canvas_per_mode = {}
                            _canvas_label_map = {
                                "deck":       "Canvas ratio",
                                "document":   "Page size",
                                "landing":    "Page width",
                                "app_mockup": "Device",
                                "storyboard": "Frame ratio",
                            }
                            _canvas_host = ui.column().classes("w-full q-mt-sm gap-0")
                            ratio_select_holder = {"select": None}

                            def _render_canvas_control() -> None:
                                mode = selected_mode["value"] or "deck"
                                choices = canvas_choices_for_mode(mode)
                                valid_keys = [k for k, _ in choices]
                                current = _canvas_per_mode.get(mode)
                                if current not in valid_keys:
                                    tmpl_ratio = _selected_template().aspect_ratio
                                    current = tmpl_ratio if tmpl_ratio in valid_keys else default_aspect_for_mode(mode)
                                    if current not in valid_keys:
                                        current = valid_keys[0] if valid_keys else "16:9"
                                _canvas_per_mode[mode] = current
                                _canvas_host.clear()
                                with _canvas_host:
                                    _sel = ui.select(
                                        {k: lbl for k, lbl in choices},
                                        value=current,
                                        label=_canvas_label_map.get(mode, "Canvas"),
                                        on_change=lambda e: _canvas_per_mode.__setitem__(
                                            selected_mode["value"] or "deck",
                                            e.value or current,
                                        ),
                                    ).props("dense outlined").classes("w-full")
                                    ratio_select_holder["select"] = _sel

                            _render_canvas_control()

                        # ── Description (bare, primary) ─────────────
                        ui.label("BUILD BRIEF").classes("text-xs text-grey-5").style(
                            "letter-spacing:0.12em;"
                        )

                        # Phase 2.3.I (dialog v3) — Inspiration chip row
                        # sits just above the textarea. "Example prompts"
                        # opens a mode-tailored menu; "From files" scrolls
                        # to the attach drop zone.
                        inspiration_row_host = ui.row().classes(
                            "w-full items-center gap-2 q-mb-xs"
                        ).style("flex-wrap:wrap;")

                        build_input = ui.textarea(
                            label="Describe the first draft",
                            placeholder="What should the AI build? Be as specific as you want.",
                        ).props('outlined autogrow input-style="min-height: 140px;"').classes("w-full")

                        # Phase 2.3.I (dialog v3) — Reference files drop
                        # zone. Accepts images/PDFs/text/data. Files are
                        # buffered here as {"name", "data"} dicts and
                        # passed to on_project_created as staged_files.
                        staged_files: list[dict] = []
                        attach_anchor = ui.column().classes("w-full q-mt-sm gap-1")
                        with attach_anchor:
                            with ui.row().classes("w-full items-center justify-between"):
                                attach_label = ui.label(
                                    "REFERENCE FILES (OPTIONAL)"
                                ).classes("text-xs text-grey-5").style(
                                    "letter-spacing:0.12em;"
                                )
                                attach_count = ui.label("").classes(
                                    "text-xs text-grey-5"
                                )
                            chips_host = ui.row().classes(
                                "w-full items-center gap-2"
                            ).style("flex-wrap:wrap;")

                            def _render_chips() -> None:
                                chips_host.clear()
                                attach_count.text = (
                                    f"{len(staged_files)} attached" if staged_files else ""
                                )
                                with chips_host:
                                    for idx, item in enumerate(staged_files):
                                        def _remove(i=idx):
                                            staged_files.pop(i)
                                            _render_chips()
                                        ui.chip(
                                            item["name"],
                                            icon="attach_file",
                                            removable=True,
                                            on_value_change=lambda e, _fn=_remove: (
                                                _fn() if not e.value else None
                                            ),
                                        ).props("dense").style(
                                            f"background:rgba({APP_BRAND_ACCENT_RGB},0.14);"
                                            f"color:{APP_BRAND_ACCENT};"
                                            f"border:1px solid rgba({APP_BRAND_ACCENT_RGB},0.34);"
                                        )

                            async def _handle_upload(e) -> None:
                                file_obj = getattr(e, "file", None)
                                file_name = (
                                    getattr(file_obj, "name", None)
                                    or getattr(e, "name", None)
                                    or "file"
                                )
                                data = b""
                                try:
                                    if file_obj is not None and hasattr(file_obj, "read"):
                                        raw = file_obj.read()
                                        data = await raw if hasattr(raw, "__await__") else raw
                                    elif hasattr(e, "content") and hasattr(e.content, "read"):
                                        data = e.content.read()
                                except Exception as exc:
                                    logger.warning("Upload read failed for %s: %s", file_name, exc)
                                    data = b""
                                if not data:
                                    ui.notify(f"Could not read {file_name!r}.", type="warning")
                                    return
                                if len(staged_files) >= 8:
                                    ui.notify("Maximum 8 files.", type="warning")
                                    return
                                staged_files.append({"name": file_name, "data": bytes(data)})
                                _render_chips()
                                upload_widget.reset()

                            upload_widget = ui.upload(
                                label="Drop files here or click to browse",
                                multiple=True,
                                auto_upload=True,
                                max_file_size=15 * 1024 * 1024,
                                max_files=8,
                                on_upload=_handle_upload,
                            ).props(
                                'accept=".pdf,.png,.jpg,.jpeg,.webp,.gif,.txt,.md,.csv,'
                                '.json,.xlsx,.xls,.docx" flat bordered'
                            ).classes("w-full").style(
                                "border:1px dashed rgba(148,163,184,0.25);"
                                "border-radius:12px;background:rgba(15,23,42,0.3);"
                            )
                            _render_chips()

                        # Render inspiration chips now that build_input +
                        # attach_anchor exist (closures reference them).
                        def _apply_example_prompt(text: str) -> None:
                            existing = (build_input.value or "").strip()
                            build_input.value = (
                                text if not existing else f"{existing}\n\n{text}"
                            )
                            build_input.update()
                            _refresh_footer_buttons()

                        def _open_examples_menu() -> None:
                            # Fresh menu each click for mode-specific content.
                            prompts = _EXAMPLE_PROMPTS_BY_MODE.get(
                                selected_mode["value"] or "deck", []
                            )
                            with examples_btn:
                                with ui.menu() as _menu:
                                    if not prompts:
                                        ui.menu_item("No examples for this mode.")
                                    for p_text in prompts:
                                        ui.menu_item(
                                            p_text,
                                            on_click=lambda t=p_text: (
                                                _apply_example_prompt(t),
                                                _menu.close(),
                                            ),
                                        ).classes("text-sm").style("max-width:560px;")
                                    _menu.open()

                        def _open_goal_dialog() -> None:
                            with ui.dialog() as _gd, ui.card().style(
                                surface_style(padding="18px", strong=True)
                            ).classes("w-96"):
                                ui.label("Start from a goal").classes("text-subtitle1 text-weight-bold")
                                audience_gi = ui.input(
                                    label="Audience",
                                    placeholder="Who is this for?",
                                ).props("dense outlined").classes("w-full q-mt-sm")
                                problem_gi = ui.input(
                                    label="Problem",
                                    placeholder="What pain point does this solve?",
                                ).props("dense outlined").classes("w-full q-mt-xs")
                                outcome_gi = ui.input(
                                    label="Desired outcome",
                                    placeholder="What should the reader/viewer do?",
                                ).props("dense outlined").classes("w-full q-mt-xs")

                                def _fill_from_goal() -> None:
                                    aud = (audience_gi.value or "").strip()
                                    prob = (problem_gi.value or "").strip()
                                    out = (outcome_gi.value or "").strip()
                                    parts = []
                                    if aud:
                                        parts.append(f"Audience: {aud}.")
                                    if prob:
                                        parts.append(f"Problem: {prob}.")
                                    if out:
                                        parts.append(f"Desired outcome: {out}.")
                                    if parts:
                                        _apply_example_prompt(" ".join(parts))
                                    _gd.close()

                                with ui.row().classes("w-full justify-end gap-2 q-mt-md"):
                                    _gcancel = ui.button("Cancel", on_click=_gd.close)
                                    style_ghost_button(_gcancel, compact=True)
                                    _gfill = ui.button("Add to description", on_click=_fill_from_goal)
                                    style_primary_button(_gfill, compact=True)
                            _gd.open()

                        def _scroll_to_attach() -> None:
                            ui.run_javascript(
                                "document.querySelector('.q-uploader')?."
                                "scrollIntoView({behavior:'smooth',block:'center'});"
                            )

                        with inspiration_row_host:
                            examples_btn = ui.button(
                                "Example prompts",
                                icon="lightbulb",
                                on_click=_open_examples_menu,
                            ).props("flat dense no-caps")
                            goal_btn = ui.button(
                                "Start from a goal",
                                icon="flag",
                                on_click=_open_goal_dialog,
                            ).props("flat dense no-caps")
                            attach_chip_btn = ui.button(
                                "From files",
                                icon="attach_file",
                                on_click=_scroll_to_attach,
                            ).props("flat dense no-caps")
                            for _b in (examples_btn, goal_btn, attach_chip_btn):
                                _b.style(
                                    f"color:{APP_BRAND_ACCENT};"
                                    f"border:1px solid rgba({APP_BRAND_ACCENT_RGB},0.30);"
                                    "border-radius:999px;padding:2px 10px;"
                                )

                        with ui.expansion("Add audience, tone, length, references", icon="add").classes(
                            "w-full"
                        ).props("dense").style("border:1px dashed rgba(148,163,184,0.18);border-radius:12px;"):
                            with ui.row().classes("w-full gap-3 q-mt-sm"):
                                audience_input = ui.select(
                                    _AUDIENCE_OPTIONS,
                                    label="Audience",
                                    with_input=True,
                                    new_value_mode="add",
                                ).props("dense outlined").classes("col")
                                tone_input = ui.select(
                                    _TONE_OPTIONS,
                                    label="Tone",
                                    with_input=True,
                                    new_value_mode="add",
                                ).props("dense outlined").classes("col")
                            length_input = ui.select(
                                _LENGTH_OPTIONS,
                                label="Length or scope",
                                with_input=True,
                                new_value_mode="add",
                            ).props("dense outlined").classes("w-full q-mt-sm")
                            refs_input = ui.textarea(
                                label="Reference notes or URLs",
                                placeholder="Inspiration, constraints, talking points, links.",
                            ).props('outlined autogrow input-style="min-height: 80px;"').classes("w-full q-mt-sm")

                        # ── Brand card ──────────────────────────────
                        with ui.card().classes("w-full").style(surface_style(padding="18px")):
                            ui.label("BRAND SETUP").classes("text-xs text-grey-5 q-mb-sm").style(
                                "letter-spacing:0.12em;"
                            )
                            preset_select = ui.select(
                                sorted(presets.keys()),
                                value=selected_preset["value"],
                                label="Brand preset",
                                on_change=lambda e: _on_preset_change(e),
                            ).props("dense outlined").classes("w-full")
                            brand_status = ui.label(extraction_label["value"]).classes(
                                "text-xs text-grey-5 q-mt-sm"
                            )
                            brand_preview = ui.html(
                                _brand_preview_html(extraction_label["value"]),
                                sanitize=False,
                            ).classes("w-full q-mt-xs")
                            with ui.expansion(
                                "Extract brand from URL", icon="language"
                            ).classes("w-full q-mt-sm").props("dense").style(
                                "border:1px dashed rgba(148,163,184,0.18);border-radius:12px;"
                            ):
                                brand_url_input = ui.input(
                                    label="Brand URL",
                                    placeholder="https://example.com",
                                ).props("dense outlined").classes("w-full")
                                with ui.row().classes("w-full items-center gap-2 q-mt-sm"):
                                    extract_btn = ui.button(
                                        "Extract Brand",
                                        icon="language",
                                    )
                                    style_primary_button(extract_btn, compact=True)
                                    clear_extract_btn = ui.button(
                                        "Use Preset Instead",
                                        icon="restart_alt",
                                    )
                                    style_secondary_button(clear_extract_btn, compact=True)

                        def _refresh_brand_preview() -> None:
                            current_effective_brand["value"] = resolve_project_brand(
                                preset_name=selected_preset["value"],
                                extracted_brand=extracted_brand["value"],
                            )
                            brand_status.text = extraction_label["value"]
                            brand_preview.set_content(_brand_preview_html(extraction_label["value"]))
                            clear_extract_btn.set_visibility(extracted_brand["value"] is not None)

                        def _on_preset_change(e) -> None:
                            selected_preset["value"] = e.value or "Default Dark"
                            if extracted_brand["value"] is None:
                                extraction_label["value"] = f"Using preset: {selected_preset['value']}"
                            _refresh_brand_preview()

                        extraction_label["value"] = f"Using preset: {selected_preset['value']}"
                        _refresh_brand_preview()

                        def _sync_template_dependent_fields() -> None:
                            tmpl = _selected_template()
                            next_default_name = default_project_name_for_template(tmpl.id)
                            current_name = (name_input.value or "").strip()
                            if not current_name or current_name == last_default_name["value"]:
                                name_input.value = next_default_name
                                name_input.update()
                            last_default_name["value"] = next_default_name

                            # Phase 2.3.E — canvas control now lives in
                            # _render_canvas_control; nudge the sticky
                            # pick for this mode to the template's
                            # aspect when it's a valid choice. Manual
                            # overrides remain sticky.
                            mode = selected_mode["value"] or "deck"
                            valid = [k for k, _ in canvas_choices_for_mode(mode)]
                            if tmpl.aspect_ratio in valid:
                                existing = _canvas_per_mode.get(mode)
                                last_default = last_default_ratio["value"]
                                if not existing or existing == last_default:
                                    _canvas_per_mode[mode] = tmpl.aspect_ratio
                                    _render_canvas_control()
                            last_default_ratio["value"] = tmpl.aspect_ratio

                        def _on_mode_change(e) -> None:
                            new_mode = e.value or "deck"
                            if new_mode == selected_mode["value"]:
                                return
                            selected_mode["value"] = new_mode
                            # Phase 2.3.D — if the currently-selected
                            # template doesn't belong to the new mode,
                            # swap to that mode's blank starter.
                            current_tmpl = _selected_template()
                            if (current_tmpl.mode or "deck") != new_mode:
                                selected_template_id["value"] = _starter_for_mode(new_mode)
                            # Phase 2.3.I (dialog v3) — PRESERVE the
                            # current category when still populated in
                            # the new mode. Fall back to "All" only if
                            # the selected category has 0 items here.
                            _cats_in_mode = {
                                t.category for t in get_templates_for_mode(new_mode)
                                if not getattr(t, "hidden_from_gallery", False)
                            }
                            if (
                                selected_category["value"] != "All"
                                and selected_category["value"] not in _cats_in_mode
                            ):
                                selected_category["value"] = "All"
                            _sync_template_dependent_fields()
                            _render_canvas_control()  # Phase 2.3.E
                            _render_category_buttons()
                            _render_template_summary()
                            _render_template_grid()

                        async def _extract_brand() -> None:
                            url = (brand_url_input.value or "").strip()
                            if not url:
                                ui.notify("Enter a brand URL first.", type="warning")
                                return
                            if not url.startswith(("http://", "https://")):
                                url = "https://" + url
                            extract_btn.disable()
                            extract_btn.set_text("Extracting...")
                            try:
                                result = await run.io_bound(lambda: extract_brand_from_url(url))
                            finally:
                                extract_btn.enable()
                                extract_btn.set_text("Extract Brand from URL")
                            if result is None:
                                ui.notify("Could not extract a brand from that URL.", type="negative")
                                return
                            extracted_brand["value"] = result
                            extraction_label["value"] = f"Using extracted brand from {url}"
                            _refresh_brand_preview()
                            ui.notify("Brand extracted and ready for setup.", type="positive")

                        def _clear_extracted_brand() -> None:
                            extracted_brand["value"] = None
                            extraction_label["value"] = f"Using preset: {selected_preset['value']}"
                            _refresh_brand_preview()

                        extract_btn.on_click(_extract_brand)
                        clear_extract_btn.on_click(_clear_extracted_brand)
                        clear_extract_btn.set_visibility(False)
                        _sync_template_dependent_fields()

                        def _build_brief() -> ProjectBrief:
                            # Phase 2.3.C — output_type is no longer a
                            # UI field. The brief prompt derives its
                            # label from project.mode in briefing.py
                            # (2.3.F). Keep the field in the dataclass
                            # empty so legacy JSON persistence still
                            # round-trips.
                            return ProjectBrief(
                                output_type="",
                                audience=(audience_input.value or "").strip(),
                                tone=(tone_input.value or "").strip(),
                                length=(length_input.value or "").strip(),
                                build_description=(build_input.value or "").strip(),
                                brand_url=(brand_url_input.value or "").strip(),
                                brand_preset=selected_preset["value"],
                                reference_notes=(refs_input.value or "").strip(),
                            )

                        def _create(auto_build: bool = False) -> None:
                            brief = _build_brief()
                            if auto_build and not brief_has_build_content(brief):
                                ui.notify(
                                    "Add brief details before creating the first draft.",
                                    type="warning",
                                )
                                return
                            project, initial_prompt = prepare_project_creation(
                                selected_template_id["value"],
                                aspect_ratio=(
                                    _canvas_per_mode.get(selected_mode["value"] or "deck")
                                    or _selected_template().aspect_ratio
                                ),
                                project_name=name_input.value or "",
                                brief=brief,
                                preset_name=selected_preset["value"],
                                extracted_brand=extracted_brand["value"],
                                auto_build=auto_build,
                                mode=selected_mode["value"] or "deck",
                            )
                            save_project(project)
                            dlg.close()
                            # Phase 2.3.I (dialog v3) — staged files are
                            # forwarded to the caller. New signature:
                            # on_project_created(project, initial_prompt, staged_files).
                            # Fall back gracefully if the caller only
                            # accepts the legacy 1- or 2-arg signatures.
                            try:
                                on_project_created(project, initial_prompt, staged_files)
                            except TypeError:
                                try:
                                    on_project_created(project, initial_prompt)
                                except TypeError:
                                    on_project_created(project)

        # ─── Sticky footer (dialog v3 — dual buttons) ────────────────
        ui.separator().classes("q-my-sm")
        with ui.row().classes("w-full items-center justify-between no-wrap"):
            footer_hint = ui.label(
                "Create starts an empty project. Add a description to also build the first draft."
            ).classes("text-xs text-grey-5")
            with ui.row().classes("items-center gap-2"):
                cancel_btn = ui.button("Cancel", on_click=dlg.close)
                style_ghost_button(cancel_btn)
                create_btn = ui.button(
                    "Create",
                    on_click=lambda: _create(False),
                )
                style_secondary_button(create_btn)
                build_btn = ui.button(
                    "Create & Build First Draft →",
                    icon="auto_awesome",
                    on_click=lambda: _create(True),
                )
                style_primary_button(build_btn)
                build_btn_tooltip = ui.tooltip(
                    "Add a description above to build a first draft."
                )

        def _refresh_footer_buttons() -> None:
            has_name = bool((name_input.value or "").strip())
            has_brief = brief_has_build_content(_build_brief())
            if has_name:
                create_btn.enable()
            else:
                create_btn.disable()
            if has_name and has_brief:
                build_btn.enable()
                build_btn.props(remove="disable")
                build_btn_tooltip.set_visibility(False)
            else:
                build_btn.disable()
                build_btn_tooltip.set_visibility(True)

        name_input.on("update:model-value", lambda _: _refresh_footer_buttons())
        build_input.on("update:model-value", lambda _: _refresh_footer_buttons())
        audience_input.on("update:model-value", lambda _: _refresh_footer_buttons())
        tone_input.on("update:model-value", lambda _: _refresh_footer_buttons())
        length_input.on("update:model-value", lambda _: _refresh_footer_buttons())
        refs_input.on("update:model-value", lambda _: _refresh_footer_buttons())
        brand_url_input.on("update:model-value", lambda _: _refresh_footer_buttons())
        preset_select.on("update:model-value", lambda _: _refresh_footer_buttons())
        _refresh_footer_buttons()

        def _render_category_buttons() -> None:
            # Phase 2.3.H — show only categories that have at least one
            # template in the current mode slice, with item counts.
            _category_row.clear()
            mode_slice = [
                t for t in get_templates_for_mode(selected_mode["value"])
                if not getattr(t, "hidden_from_gallery", False)
            ]
            counts = {}
            for t in mode_slice:
                counts[t.category] = counts.get(t.category, 0) + 1
            ordered = ["All"] + [c for c in get_template_categories() if counts.get(c, 0) > 0]
            with _category_row:
                for cat in ordered:
                    n = len(mode_slice) if cat == "All" else counts.get(cat, 0)
                    is_active = cat == selected_category["value"]

                    def _choose_category(category=cat):
                        selected_category["value"] = category
                        _render_category_buttons()
                        _render_template_grid()

                    category_btn = ui.button(
                        f"{cat} ({n})", on_click=_choose_category
                    )
                    style_choice_button(category_btn, active=is_active)

        def _render_template_summary() -> None:
            # Phase 2.3.H — replaced the boxed summary card with a
            # one-line caption beneath the grid.
            tmpl = _selected_template()
            _caption.text = f"Selected: {tmpl.icon}  {tmpl.name} — {tmpl.description}"

        def _render_template_grid() -> None:
            # Phase 2.3.D — mode-first filter, category-second.
            mode_slice = [t for t in get_templates_for_mode(selected_mode["value"]) if not getattr(t, "hidden_from_gallery", False)]
            filtered = mode_slice
            if selected_category["value"] != "All":
                filtered = [t for t in mode_slice if t.category == selected_category["value"]]

            _grid.clear()
            with _grid:
                with ui.element("div").classes("w-full").style(
                    "display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));"
                    " gap: 16px; padding: 8px 2px 12px;"
                ):
                    for tmpl in filtered:
                        is_selected = tmpl.id == selected_template_id["value"]
                        border = "1px solid rgba(245,158,11,0.45)" if is_selected else "1px solid rgba(148,163,184,0.12)"
                        background = (
                            "linear-gradient(180deg, rgba(30,41,59,0.95), rgba(15,23,42,0.88))"
                            if is_selected
                            else "linear-gradient(180deg, rgba(15,23,42,0.64), rgba(15,23,42,0.46))"
                        )
                        shadow = "0 18px 36px rgba(245,158,11,0.12)" if is_selected else "0 10px 24px rgba(2,6,23,0.18)"

                        def _select_template(template_id=tmpl.id):
                            selected_template_id["value"] = template_id
                            _sync_template_dependent_fields()
                            _render_template_summary()
                            _render_template_grid()

                        with ui.card().classes("h-full cursor-pointer q-pa-sm").style(
                            f"border:{border}; background:{background}; box-shadow:{shadow}; border-radius: 20px;"
                        ).on("click", lambda _, _fn=_select_template: _fn()):
                            with ui.column().classes("w-full gap-2"):
                                with ui.row().classes("w-full items-center justify-between"):
                                    ui.label(tmpl.icon).style("font-size: 1.45rem;")
                                    ui.badge(tmpl.category).props("outline").style(
                                        "color: #cbd5e1; border-color: rgba(148,163,184,0.18);"
                                    )
                                ui.label(tmpl.name).classes("text-subtitle2 text-weight-bold")
                                ui.label(tmpl.description).classes("text-sm text-grey-5")
                                with ui.row().classes("w-full items-center justify-between"):
                                    ui.label(f"{len(tmpl.pages)} page{'s' if len(tmpl.pages) != 1 else ''}").classes("text-xs text-grey-6")
                                    ui.label(tmpl.aspect_ratio).classes("text-xs text-grey-6")
                                ui.label(infer_output_type_for_template(tmpl.id)).classes("text-xs").style(
                                    f"color: {APP_BRAND_ACCENT}; font-weight: 600;"
                                )

        _render_category_buttons()
        _render_template_summary()
        _render_template_grid()

    dlg.open()
