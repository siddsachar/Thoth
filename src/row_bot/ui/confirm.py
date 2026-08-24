"""Reusable destructive-action confirmation dialog.

A small helper that renders a consistent "are you sure?" modal with a
cancel button and a red confirm button. Several surfaces (workflows,
designer projects, bulk deletes) previously inlined near-identical
dialogs; this module unifies them without changing visual behaviour.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import inspect

from nicegui import ui


async def _invoke_confirmation_callback(
    callback: Callable[[], None | Awaitable[None]],
) -> None:
    """Invoke a confirmation callback and await asynchronous work."""

    result = callback()
    if inspect.isawaitable(result):
        await result


def confirm_destructive(
    title: str,
    body: str = "This cannot be undone.",
    *,
    confirm_label: str = "Delete",
    cancel_label: str = "Cancel",
    on_confirm: Callable[[], None | Awaitable[None]],
    min_width: str = "320px",
) -> None:
    """Open a modal asking the user to confirm a destructive action.

    Parameters
    ----------
    title:
        Dialog heading, e.g. ``"Delete 3 threads?"``.
    body:
        Subtitle / explanation line.
    confirm_label:
        Label for the red confirm button.
    cancel_label:
        Label for the neutral cancel button.
    on_confirm:
        Callable invoked after the user confirms. The dialog is closed
        before ``on_confirm`` runs so failures in the callback do not
        leave a stranded modal.
    min_width:
        CSS min-width for the dialog card.
    """
    with ui.dialog() as dlg, ui.card().style(f"min-width: {min_width};"):
        ui.label(title).classes("font-bold")
        if body:
            ui.label(body).classes("text-grey-6 text-xs")
        with ui.row().classes("w-full justify-end mt-2"):
            ui.button(cancel_label, on_click=dlg.close).props(
                "flat dense no-caps"
            )

            async def _go() -> None:
                dlg.close()
                try:
                    await _invoke_confirmation_callback(on_confirm)
                except Exception:  # pragma: no cover — logged by caller
                    import logging
                    logging.getLogger(__name__).exception(
                        "confirm_destructive on_confirm failed"
                    )

            ui.button(confirm_label, on_click=_go).props(
                "flat dense no-caps color=red"
            )
    dlg.open()
