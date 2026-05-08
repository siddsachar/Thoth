from __future__ import annotations

from collections.abc import Callable

from nicegui import run, ui

from providers.model_catalog import CatalogModelRow, group_rows_by_provider, rows_for_surface


SURFACE_LABELS = {
    "chat": "Chat",
    "vision": "Vision",
    "image": "Image",
    "video": "Video",
}

CATALOG_PROVIDER_ROW_LIMIT = 80


def build_model_catalog_section(
    rows: list[CatalogModelRow],
    *,
    on_set_default: Callable[[str, CatalogModelRow], None] | None = None,
    on_download: Callable[[CatalogModelRow], object] | None = None,
    on_change: Callable[[], None] | None = None,
    initial_open: bool = False,
) -> None:
    state = {"surface": "chat", "query": "", "provider": "", "catalog_open": initial_open}
    pinned_by_ref = {row.selection_ref: set(row.pinned_surfaces) for row in rows}
    defaults_by_ref = {row.selection_ref: set(row.default_surfaces) for row in rows}
    visible_limits: dict[tuple[str, str, str, str], int] = {}
    provider_options = {"": "All providers"}
    for row in rows:
        provider_options.setdefault(row.provider_id, row.provider_display_name or row.provider_id)

    catalog_expansion = ui.expansion("Model Catalog", icon="view_list", value=initial_open).classes("w-full")
    with catalog_expansion:
        ui.label("Browse discovered models by provider. Open one provider or search before rendering model rows.").classes("text-grey-6 text-sm")
        with ui.row().classes("items-center gap-2 w-full"):
            category = ui.toggle(SURFACE_LABELS, value="chat").props("dense unelevated toggle-color=primary")
            provider_filter = ui.select(provider_options, value="", label="Provider").props("dense outlined").classes("min-w-[180px]")
            search = ui.input(placeholder="Search models...").props("dense outlined clearable").classes("min-w-[220px] flex-grow")
        container = ui.column().classes("w-full gap-2 q-mt-sm")

        def _refresh() -> None:
            container.clear()
            if not state["catalog_open"]:
                return
            surface = str(state["surface"] or "chat")
            query = str(state["query"] or "").strip().lower()
            provider = str(state["provider"] or "")
            surface_rows = _filter_catalog_rows(rows, surface=surface, query=query, provider=provider)
            with container:
                if not surface_rows:
                    ui.label("No models match this catalog view.").classes("text-grey-6 text-sm")
                    return
                grouped = group_rows_by_provider(surface_rows)
                if not provider and not query:
                    _render_provider_summaries(grouped, surface)
                    return
                auto_expand_results = bool(provider) or (bool(query) and len(surface_rows) <= CATALOG_PROVIDER_ROW_LIMIT)
                for provider_id, provider_rows in grouped.items():
                    _render_provider_group(provider_id, provider_rows, surface, query, provider, auto_expand_results)

        def _render_provider_summaries(
            grouped: dict[str, list[CatalogModelRow]],
            surface: str,
        ) -> None:
            with ui.column().classes("w-full gap-2"):
                ui.label("Providers").classes("text-subtitle2")
                for provider_id, provider_rows in grouped.items():
                    provider_label = provider_rows[0].provider_display_name or provider_id
                    usable = sum(1 for row in provider_rows if row.runtime_ready and row.configured and row.installed)
                    pinned = sum(1 for row in provider_rows if surface in row.pinned_surfaces)
                    with ui.row().classes("items-center gap-2 no-wrap w-full q-py-sm").style(
                        "border-bottom: 1px solid rgba(148, 163, 184, 0.14);"
                    ):
                        ui.label(provider_rows[0].provider_icon or "AI").classes("text-sm").style("width: 24px; text-align: center;")
                        with ui.column().classes("gap-0").style("min-width: 0; flex: 1;"):
                            ui.label(provider_label).classes("text-sm text-weight-medium")
                            ui.label(f"{len(provider_rows)} {SURFACE_LABELS.get(surface, surface).lower()} model(s) · {usable} ready · {pinned} pinned").classes("text-grey-6 text-xs")
                        if usable == 0:
                            ui.badge("needs setup", color="orange").props("outline dense")
                        elif pinned:
                            ui.badge(f"{pinned} pinned", color="cyan").props("outline dense")
                        ui.button(
                            "Open",
                            icon="chevron_right",
                            on_click=lambda _, pid=provider_id: _open_provider(pid),
                        ).props("flat dense no-caps color=primary")

        def _open_provider(provider_id: str) -> None:
            state["provider"] = provider_id
            provider_filter.value = provider_id
            provider_filter.update()
            visible_limits.clear()
            _refresh()

        def _render_provider_group(
            provider_id: str,
            provider_rows: list[CatalogModelRow],
            surface: str,
            query: str,
            provider: str,
            auto_expand: bool,
        ) -> None:
            provider_label = provider_rows[0].provider_display_name or provider_id
            state_key = (surface, query, provider, provider_id)
            row_container: ui.column | None = None

            def _render_rows() -> None:
                if row_container is None:
                    return
                row_container.clear()
                limit = visible_limits.setdefault(state_key, CATALOG_PROVIDER_ROW_LIMIT)
                visible_rows = _visible_provider_rows(provider_rows, limit)
                with row_container:
                    ui.label(f"Showing {len(visible_rows)} of {len(provider_rows)} models").classes("text-grey-6 text-xs")
                    for row in visible_rows:
                        _render_row(row, surface)
                    if len(visible_rows) < len(provider_rows):
                        remaining = min(CATALOG_PROVIDER_ROW_LIMIT, len(provider_rows) - len(visible_rows))
                        ui.button(
                            f"Show {remaining} more",
                            icon="expand_more",
                            on_click=lambda: _show_more(),
                        ).props("flat dense color=primary").classes("self-start")

            def _show_more() -> None:
                visible_limits[state_key] = visible_limits.get(state_key, CATALOG_PROVIDER_ROW_LIMIT) + CATALOG_PROVIDER_ROW_LIMIT
                _render_rows()

            provider_expansion = ui.expansion(f"{provider_label} ({len(provider_rows)})", value=auto_expand).classes("w-full")
            with provider_expansion:
                row_container = ui.column().classes("w-full gap-1")
            provider_expansion.on_value_change(lambda e: _render_rows() if e.value else None)
            if auto_expand:
                _render_rows()

        def _on_catalog_toggle(e) -> None:
            state["catalog_open"] = bool(e.value)
            _refresh()

        def _on_category(e) -> None:
            state["surface"] = e.value or "chat"
            visible_limits.clear()
            _refresh()

        def _on_provider(e) -> None:
            state["provider"] = e.value or ""
            visible_limits.clear()
            _refresh()

        def _on_search(e) -> None:
            state["query"] = e.value or ""
            visible_limits.clear()
            _refresh()

        catalog_expansion.on_value_change(_on_catalog_toggle)
        category.on_value_change(_on_category)
        provider_filter.on_value_change(_on_provider)
        search.on_value_change(_on_search)

        def _render_row(row: CatalogModelRow, surface: str) -> None:
            pinned = surface in pinned_by_ref.get(row.selection_ref, set())
            is_default = surface in defaults_by_ref.get(row.selection_ref, set())
            can_use = row.runtime_ready and row.configured and row.installed
            with ui.row().classes("items-center gap-2 no-wrap w-full q-py-xs").style("border-bottom: 1px solid rgba(148, 163, 184, 0.12);"):
                ui.label(row.provider_icon or "AI").classes("text-sm").style("width: 22px; text-align: center;")
                with ui.column().classes("gap-0").style("min-width: 0; flex: 1;"):
                    ui.label(row.display_name).classes("text-sm text-weight-medium").style("line-height: 1.15;")
                    if row.display_name != row.model_id:
                        ui.label(row.model_id).classes("text-grey-6 text-xs").style("line-height: 1.15; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;")
                ui.badge(row.provider_id, color="blue-grey").props("outline dense")
                if row.context_window:
                    ui.badge(_ctx_label(row.context_window), color="grey").props("outline dense")
                for category_id in row.categories:
                    ui.badge(SURFACE_LABELS.get(category_id, category_id), color="grey").props("outline dense")
                if row.downloadable:
                    ui.badge("download", color="orange").props("outline dense")
                elif not row.configured:
                    ui.badge("connect", color="orange").props("outline dense")
                elif not row.runtime_ready:
                    ui.badge("unavailable", color="orange").props("outline dense")
                if is_default:
                    ui.badge("default", color="cyan").props("outline dense")
                ui.space()
                if row.status_reason:
                    ui.icon("info", size="xs").tooltip(row.status_reason)
                if row.downloadable and on_download:
                    ui.button(icon="download", on_click=lambda _, r=row: _download(r)).props("flat dense round size=sm color=primary").tooltip("Download model")
                pin_button = ui.button(icon="push_pin", on_click=lambda _, r=row, s=surface: _toggle_pin(r, s)).props(f"flat dense round size=sm color={'primary' if pinned else 'grey'}").tooltip("Remove from this picker" if pinned else "Pin to this picker")
                if not can_use:
                    pin_button.disable()
                default_button = ui.button(icon="check", on_click=lambda _, r=row, s=surface: _set_default(r, s)).props("flat dense round size=sm").tooltip("Set default")
                if not can_use or is_default:
                    default_button.disable()

        def _toggle_pin(row: CatalogModelRow, surface: str) -> None:
            from providers.selection import add_quick_choice_for_model, remove_quick_choice_for_model

            if surface in pinned_by_ref.get(row.selection_ref, set()):
                remove_quick_choice_for_model(row.model_id, provider_id=row.provider_id)
                pinned_by_ref.setdefault(row.selection_ref, set()).discard(surface)
                ui.notify("Removed from picker", type="info")
            else:
                add_quick_choice_for_model(
                    row.model_id,
                    provider_id=row.provider_id,
                    display_name=row.display_name,
                    source="models_catalog",
                    capabilities_snapshot=row.capabilities_snapshot,
                    surface=surface,
                )
                pinned_by_ref.setdefault(row.selection_ref, set()).add(surface)
                ui.notify("Pinned to picker", type="positive")
            if on_change:
                on_change()
            _refresh()

        def _set_default(row: CatalogModelRow, surface: str) -> None:
            from providers.selection import add_quick_choice_for_model

            add_quick_choice_for_model(
                row.model_id,
                provider_id=row.provider_id,
                display_name=row.display_name,
                source="models_catalog_default",
                capabilities_snapshot=row.capabilities_snapshot,
                surface=surface,
            )
            if on_set_default:
                on_set_default(surface, row)
            for ref, surfaces in defaults_by_ref.items():
                surfaces.discard(surface)
            defaults_by_ref.setdefault(row.selection_ref, set()).add(surface)
            pinned_by_ref.setdefault(row.selection_ref, set()).add(surface)
            if on_change:
                on_change()
            _refresh()

        async def _download(row: CatalogModelRow) -> None:
            if not on_download:
                return
            notification = ui.notification(f"Downloading {row.model_id}...", type="ongoing", spinner=True, timeout=None)
            try:
                await run.io_bound(on_download, row)
                notification.dismiss()
                ui.notify(f"{row.model_id} ready", type="positive")
                if on_change:
                    on_change()
            except Exception as exc:
                notification.dismiss()
                ui.notify(f"Download failed: {exc}", type="negative")

        _refresh()


def build_lazy_model_catalog_section(
    load_rows: Callable[[], list[CatalogModelRow]],
    *,
    on_set_default: Callable[[str, CatalogModelRow], None] | None = None,
    on_download: Callable[[CatalogModelRow], object] | None = None,
    on_change: Callable[[], None] | None = None,
) -> None:
    container = ui.column().classes("w-full")
    state = {"loaded": False, "loading": False}

    async def _load() -> None:
        if state["loaded"] or state["loading"]:
            return
        state["loading"] = True
        container.clear()
        with container:
            with ui.expansion("Model Catalog", icon="view_list", value=True).classes("w-full"):
                with ui.row().classes("items-center gap-2 text-grey-6 text-sm"):
                    ui.spinner(size="sm")
                    ui.label("Loading catalog...")
        try:
            rows = await run.io_bound(load_rows)
            state["loaded"] = True
            container.clear()
            with container:
                build_model_catalog_section(
                    rows,
                    on_set_default=on_set_default,
                    on_download=on_download,
                    on_change=on_change,
                    initial_open=True,
                )
        except Exception as exc:
            container.clear()
            with container:
                with ui.expansion("Model Catalog", icon="view_list", value=True).classes("w-full"):
                    ui.label(f"Could not load catalog: {exc}").classes("text-warning text-sm")
                    ui.button("Retry", icon="refresh", on_click=_load).props("flat dense")
        finally:
            state["loading"] = False

    async def _on_catalog_toggle(e) -> None:
        if e.value:
            await _load()

    with container:
        with ui.expansion("Model Catalog", icon="view_list", value=False).classes("w-full") as catalog_expansion:
            with ui.column().classes("gap-2"):
                ui.label("The catalog loads only when opened to keep Settings responsive on large provider/model catalogs.").classes("text-grey-6 text-sm")
                ui.button("Load catalog", icon="view_list", on_click=_load).props("flat dense color=primary")
        catalog_expansion.on_value_change(_on_catalog_toggle)


def _filter_catalog_rows(
    rows: list[CatalogModelRow],
    *,
    surface: str,
    query: str = "",
    provider: str = "",
) -> list[CatalogModelRow]:
    surface_rows = rows_for_surface(rows, surface)
    if provider:
        surface_rows = [row for row in surface_rows if row.provider_id == provider]
    normalized_query = query.strip().lower()
    if normalized_query:
        surface_rows = [
            row for row in surface_rows
            if normalized_query in " ".join([row.display_name, row.model_id, row.provider_display_name, row.provider_id]).lower()
        ]
    return surface_rows


def _visible_provider_rows(provider_rows: list[CatalogModelRow], limit: int) -> list[CatalogModelRow]:
    return provider_rows[:max(0, limit)]


def _ctx_label(ctx: int) -> str:
    if ctx >= 1_000_000:
        return f"{ctx // 1_000_000}M ctx"
    if ctx >= 1_000:
        return f"{ctx // 1_000}K ctx"
    return f"{ctx} ctx"
