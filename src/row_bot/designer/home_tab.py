"""Designer — home screen gallery tab showing project cards."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

from nicegui import ui

from row_bot.designer.state import BrandConfig
from row_bot.designer.storage import list_projects, load_project, delete_project, duplicate_project, delete_projects
from row_bot.designer.thumbnail import compute_thumbnail_dimensions, render_static_page_thumbnail
from row_bot.designer.ui_theme import style_primary_button

logger = logging.getLogger(__name__)


def build_designer_tab(
    *,
    on_open_project: Callable,
    on_refresh: Callable | None = None,
) -> None:
    """Render the Designer gallery inside a home-screen tab panel.

    Parameters
    ----------
    on_open_project : Callable[[DesignerProject], None]
        Called when the user clicks a project card or creates a new one.
    on_refresh : Callable | None
        Called to rebuild the whole home view (after delete, etc.).
    """
    from row_bot.ui.bulk_select import (
        BulkSelect,
        _run_bulk_operation,
        render_bulk_action_bar,
    )
    from row_bot.ui.confirm import confirm_destructive

    bulk = BulkSelect()

    with ui.scroll_area().classes("w-full h-full"):
        with ui.column().classes("w-full q-pa-sm gap-0"):
            # Header row
            with ui.row().classes("w-full items-center justify-between"):
                with ui.column().classes("gap-0"):
                    ui.label("🎨 Designer").classes("text-h5")
                    ui.label("Visual Designs & Presentations").classes(
                        "text-xs text-grey-6"
                    ).style("margin-top: -2px; letter-spacing: 0.3px;")

                with ui.row().classes("gap-2"):
                    # Project cards grid needs a forward ref so Select can
                    # rebuild it when the mode flips.
                    _grid_ref: list = [None]
                    projects = list_projects()

                    select_btn = ui.button("Select").props(
                        "flat dense no-caps size=sm"
                    )
                    if not projects:
                        select_btn.set_visibility(False)

                    def _toggle_select():
                        bulk.toggle_mode()
                        select_btn.text = "Done" if bulk.active else "Select"
                        _rebuild_grid()

                    select_btn.on("click", _toggle_select)

                    def _new_design():
                        from row_bot.designer.template_gallery import show_new_project_dialog
                        show_new_project_dialog(on_project_created=on_open_project)

                    new_design_btn = ui.button(
                        "New Design", icon="add",
                        on_click=_new_design,
                    )
                    style_primary_button(new_design_btn, compact=True)

            ui.separator().classes("q-my-sm")

            # Project cards grid
            if projects:
                grid = ui.element("div").classes("w-full").style(
                    "display: grid;"
                    "grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));"
                    "gap: 0.75rem;"
                )
                _grid_ref[0] = grid

                def _rebuild_grid() -> None:
                    grid.clear()
                    with grid:
                        for proj_summary in projects:
                            _render_project_card(
                                proj_summary,
                                on_open=on_open_project,
                                on_refresh=on_refresh,
                                bulk=bulk,
                            )

                _rebuild_grid()

                def _do_bulk_delete(ids: list[str]) -> None:
                    noun = "design" if len(ids) == 1 else "designs"

                    async def _commit() -> None:
                        deleted, failures = await _run_bulk_operation(
                            lambda: delete_projects(ids),
                            progress_label=f"Deleting {len(ids)} {noun}…",
                        )
                        msg = f"🗑️ Deleted {deleted} design{'s' if deleted != 1 else ''}."
                        if failures:
                            msg += f" {len(failures)} failed."
                        ui.notify(msg, type="negative" if failures else "info")
                        if on_refresh:
                            on_refresh()

                    confirm_destructive(
                        f"Delete {len(ids)} {noun}?",
                        body=(
                            "Delete these designs, their assets, history, published copies, "
                            "and linked conversations?"
                        ),
                        on_confirm=_commit,
                    )

                render_bulk_action_bar(
                    bulk,
                    on_delete=_do_bulk_delete,
                    label_singular="design",
                    label_plural="designs",
                    on_clear=_rebuild_grid,
                )
            else:
                def _rebuild_grid() -> None:  # no-op when empty
                    pass
                with ui.column().classes("w-full items-center q-mt-lg"):
                    ui.icon("design_services", size="3rem").classes("text-grey-6")
                    ui.label(
                        "No design projects yet"
                    ).classes("text-grey-5 text-lg q-mt-sm")
                    ui.label(
                        "Click 'New Design' to start from a template or blank canvas."
                    ).classes("text-grey-6 text-sm")


def _render_project_card(
    summary: dict,
    *,
    on_open: Callable,
    on_refresh: Callable | None,
    bulk=None,
) -> None:
    """Render a single project card in the gallery grid.

    When ``bulk`` is provided and active, the card shows a checkbox
    overlay and card clicks toggle selection instead of opening.
    """
    from row_bot.ui.bulk_select import _bind_bulk_selection_checkbox

    proj_id = summary["id"]
    name = summary.get("name", "Untitled")
    page_count = summary.get("page_count", 0)
    ratio = summary.get("aspect_ratio", "16:9")
    updated = summary.get("updated_at", "")
    # NOTE: We deliberately do NOT call ``load_project`` here — rendering the
    # card only needs ``summary['preview_html']`` (written on every save).
    # Eagerly loading every project on gallery open made the view appear
    # frozen for 50-300 ms per card. The click handler does the load.

    # Format date
    date_str = ""
    if updated:
        try:
            dt = datetime.fromisoformat(updated)
            date_str = dt.strftime("%b %d, %I:%M %p")
        except (ValueError, TypeError):
            date_str = updated[:10]

    selecting = bool(bulk and bulk.active)

    def _open_this():
        if selecting:
            bulk.toggle_item(proj_id)
            return
        project = load_project(proj_id)
        if project:
            on_open(project)
        else:
            ui.notify("Project not found.", type="negative")

    with ui.card().classes("h-full").style(
        "padding: 0.75rem; cursor: pointer; transition: border-color 0.2s;"
        " position: relative;"
    ).on("click", _open_this):
        if selecting:
            # Checkbox overlay top-left
            with ui.element("div").style(
                "position: absolute; top: 6px; left: 6px; z-index: 5;"
                "background: rgba(15,23,42,0.85); border-radius: 4px;"
                "padding: 2px;"
            ):
                cb = ui.checkbox(value=bulk.is_selected(proj_id))
                _bind_bulk_selection_checkbox(cb, bulk, proj_id)
        # Use the cached summary preview (updated on every save_project).
        preview_html = summary.get("preview_html", "")
        preview_title = summary.get("preview_title", name)
        brand_dict = summary.get("brand")
        brand = BrandConfig.from_dict(brand_dict) if brand_dict else None
        canvas_width = int(summary.get("canvas_width", 1920) or 1920)
        canvas_height = int(summary.get("canvas_height", 1080) or 1080)
        thumb_height = 80
        thumb_width, _ = compute_thumbnail_dimensions(canvas_width, canvas_height, thumb_height)

        with ui.element("div").classes("w-full flex justify-center q-mb-xs"):
            with ui.element("div").style(
                f"width: {thumb_width}px; height: {thumb_height}px; border-radius: 8px; "
                "overflow: hidden; position: relative; background: #0F172A; "
                "border: 1px solid rgba(255,255,255,0.08);"
            ):
                render_static_page_thumbnail(
                    frame_id=f"gallery-preview-{proj_id[:8]}",
                    page_html=preview_html,
                    brand=brand,
                    project=None,
                    page_index=0,
                    canvas_width=canvas_width,
                    canvas_height=canvas_height,
                    preview_height=thumb_height,
                    empty_label=preview_title,
                )

        ui.label(name).classes("font-bold text-center w-full").style(
            "font-size: 0.85rem; line-height: 1.2; overflow: hidden; "
            "text-overflow: ellipsis; white-space: nowrap;"
        )

        info = f"{page_count} page{'s' if page_count != 1 else ''} · {ratio}"
        if date_str:
            info += f" · {date_str}"
        ui.label(info).classes("text-xs text-grey-6 text-center w-full")

        # Action buttons
        with ui.row().classes("w-full items-center justify-center gap-1").style(
            "margin-top: 4px;"
        ):
            def _dup(pid=proj_id):
                new_proj = duplicate_project(pid)
                if new_proj:
                    ui.notify(f"Duplicated as '{new_proj.name}'", type="positive")
                    if on_refresh:
                        on_refresh()

            ui.button(icon="content_copy").on(
                "click.stop", _dup
            ).props("flat dense round size=sm").tooltip("Duplicate")

            def _del(pid=proj_id, pname=name):
                from row_bot.ui.confirm import confirm_destructive

                def _confirm(p=pid):
                    delete_project(p)
                    ui.notify("🗑️ Project deleted.", type="negative")
                    if on_refresh:
                        on_refresh()

                confirm_destructive(
                    f"Delete '{pname}'?",
                    body="Delete this design, its assets, history, published copy, and linked conversation?",
                    on_confirm=_confirm,
                )

            ui.button(icon="delete").on(
                "click.stop", _del
            ).props("flat dense round size=sm").tooltip("Delete").style("color: #888;")
