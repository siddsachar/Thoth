"""Small controller + helper for list multi-select and bulk actions.

``BulkSelect`` holds the transient state for a single list view:
  - whether selection mode is active (shows checkboxes)
  - which item ids are currently selected

``render_bulk_action_bar`` paints a sticky bottom bar when items are
selected and invokes the supplied delete callback.

The controller is UI-framework-agnostic; it stores plain Python state
and fires a change callback. Consumers own the rendering of checkboxes
and the list itself.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, TypeVar

from nicegui import run, ui


_ResultT = TypeVar("_ResultT")


class BulkSelect:
    """Selection state for a single list view."""

    def __init__(self) -> None:
        self._active: bool = False
        self._selected: set[str] = set()
        self._on_change: Callable[[], None] | None = None

    # ── public state ────────────────────────────────────────────────
    @property
    def active(self) -> bool:
        return self._active

    @property
    def selected(self) -> set[str]:
        return self._selected

    @property
    def count(self) -> int:
        return len(self._selected)

    # ── mutations ───────────────────────────────────────────────────
    def on_change(self, cb: Callable[[], None]) -> None:
        self._on_change = cb

    def _emit(self) -> None:
        if self._on_change:
            try:
                self._on_change()
            except Exception:
                import logging
                logging.getLogger(__name__).exception(
                    "BulkSelect on_change callback failed"
                )

    def toggle_mode(self) -> None:
        """Flip selection mode. Clears the selection when turning off."""
        self._active = not self._active
        if not self._active:
            self._selected.clear()
        self._emit()

    def set_mode(self, active: bool) -> None:
        if self._active == active:
            return
        self._active = active
        if not active:
            self._selected.clear()
        self._emit()

    def toggle_item(self, item_id: str, value: bool | None = None) -> None:
        if value is None:
            if item_id in self._selected:
                self._selected.discard(item_id)
            else:
                self._selected.add(item_id)
        elif value:
            self._selected.add(item_id)
        else:
            self._selected.discard(item_id)
        self._emit()

    def is_selected(self, item_id: str) -> bool:
        return item_id in self._selected

    def clear(self) -> None:
        if not self._selected:
            return
        self._selected.clear()
        self._emit()

    def select_many(self, ids: list[str] | set[str]) -> None:
        previous = len(self._selected)
        self._selected.update(str(item_id) for item_id in ids)
        if len(self._selected) == previous:
            return
        self._emit()

    def deselect_many(self, ids: list[str] | set[str]) -> None:
        previous = len(self._selected)
        self._selected.difference_update(str(item_id) for item_id in ids)
        if len(self._selected) == previous:
            return
        self._emit()


def _bind_bulk_selection_checkbox(
    checkbox: Any,
    bulk: BulkSelect,
    item_id: str,
) -> None:
    """Keep a checkbox, selection count, and destructive target in sync."""

    checkbox.on_value_change(
        lambda event: bulk.toggle_item(item_id, bool(event.value))
    )
    checkbox.on("click", js_handler="(event) => event.stopPropagation()")


async def _run_bulk_operation(
    operation: Callable[[], _ResultT],
    *,
    progress_label: str,
) -> _ResultT:
    """Run blocking bulk work off the UI loop behind a progress dialog."""

    with ui.dialog().props(
        "persistent no-esc-dismiss no-backdrop-dismiss"
    ) as progress_dialog:
        with ui.card().classes("items-center gap-3 q-pa-lg").style(
            "min-width: 300px;"
        ):
            ui.spinner("oval", size="lg", color="primary")
            ui.label(progress_label).classes("text-sm font-medium")
            ui.label("Please keep Row-Bot open while cleanup finishes.").classes(
                "text-xs text-grey-6"
            )

    progress_dialog.open()
    # Give NiceGUI a scheduling turn to paint the dialog before starting work.
    await asyncio.sleep(0)
    try:
        return await run.io_bound(operation)
    finally:
        progress_dialog.close()
        progress_dialog.delete()


def render_bulk_action_bar(
    bulk: BulkSelect,
    *,
    on_delete: Callable[[list[str]], None],
    label_singular: str = "item",
    label_plural: str | None = None,
    delete_label: str = "Delete",
    on_clear: Callable[[], None] | None = None,
) -> ui.row:
    """Render a small action bar shown while items are selected.

    ``on_clear`` runs AFTER the selection is emptied — surfaces should
    pass their list-rebuild fn here so rendered checkboxes reflect the
    cleared state (a plain ``bulk.clear()`` only mutates the set).

    Returns the outer ``ui.row`` so callers can further style/position
    it. The row is hidden while the selection is empty so it doesn't
    jump in and out of the DOM on each toggle.
    """
    plural = label_plural or f"{label_singular}s"

    container = ui.row().classes(
        "items-center justify-between q-py-sm q-px-md"
    ).style(
        "position: fixed; left: 50%; bottom: 16px;"
        "transform: translateX(-50%); z-index: 2000;"
        "min-width: 320px; max-width: 90vw;"
        "background: rgba(24,24,27,0.96);"
        "border: 1px solid rgba(239,68,68,0.45);"
        "border-radius: 10px;"
        "box-shadow: 0 6px 20px rgba(0,0,0,0.45);"
        "gap: 16px;"
    )

    def _do_clear() -> None:
        bulk.clear()
        if on_clear:
            try:
                on_clear()
            except Exception:
                import logging
                logging.getLogger(__name__).exception(
                    "bulk action bar on_clear callback failed"
                )

    with container:
        count_lbl = ui.label("").classes("text-sm")
        with ui.row().classes("gap-2 items-center"):
            ui.button("Clear", on_click=_do_clear).props(
                "flat dense no-caps size=sm"
            )
            ui.button(
                delete_label,
                icon="delete",
                on_click=lambda: on_delete(sorted(bulk.selected)),
            ).props("flat dense no-caps size=sm color=red")

    def _refresh() -> None:
        n = bulk.count
        visible = bool(n > 0 and bulk.active)
        container.set_visibility(visible)
        noun = label_singular if n == 1 else plural
        count_lbl.text = f"{n} {noun} selected"

    _refresh()
    # Chain onto any existing change callback rather than overwrite
    prev_cb = bulk._on_change  # noqa: SLF001 — intentional
    def _chained() -> None:
        _refresh()
        if prev_cb:
            prev_cb()
    bulk.on_change(_chained)

    return container
