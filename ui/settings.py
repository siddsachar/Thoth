"""Thoth UI — Settings dialog with all configuration tabs.

Contains ``open_settings()`` plus 13+ tab builder helpers.
Receives ``state`` and ``p`` explicitly.
"""

from __future__ import annotations

import logging
import os
import pathlib
import tempfile
from datetime import datetime
from typing import Callable

from nicegui import events, run, ui

from ui.state import AppState, P
from ui.constants import ICON_OPTIONS
from ui.helpers import browse_folder, browse_file

logger = logging.getLogger(__name__)


def open_settings(
    state: AppState,
    p: P,
    initial_tab: str = "Models",
) -> None:
    """Build and open the maximised settings dialog.

    Every tab builder is defined locally so it closes over ``state``
    and ``p``.  External deps are imported inside the tab builders to
    keep startup fast.
    """
    # ── imports used across multiple tabs ──
    from api_keys import get_key, set_key, delete_key, key_status, get_cloud_config
    from tools import registry as tool_registry
    from models import (
        _ollama_reachable,
        fetch_trending_ollama_models,
        get_trending_models,
        list_local_models,
        list_cloud_models,
        list_cloud_vision_models,
        list_all_models,
        get_current_model,
        is_cloud_model,
        is_model_local,
        is_tool_compatible,
        check_tool_support,
        pull_model,
        set_model,
        get_provider_emoji,
        get_user_context_size,
        set_context_size,
        get_model_max_context,
        refresh_cloud_models,
        star_cloud_model,
        unstar_cloud_model,
        validate_openrouter_key,
        validate_anthropic_key,
        validate_google_key,
        validate_xai_key,
        CONTEXT_SIZE_OPTIONS,
        CONTEXT_SIZE_LABELS,
        CLOUD_CONTEXT_SIZE_OPTIONS,
        CLOUD_CONTEXT_SIZE_LABELS,
        get_cloud_context_size,
        set_cloud_context_size,
        is_cloud_available,
        _cloud_model_cache,
    )
    from vision import POPULAR_VISION_MODELS
    from documents import load_processed_files, load_and_vectorize_document, reset_vector_store, remove_document

    # ── Recursive reopen helper ──
    def _reopen(tab: str = initial_tab):
        p.settings_dlg.close()
        open_settings(state, p, initial_tab=tab)

    # ── Lazy helpers (deferred to avoid slow import on panel open) ──
    def clear_agent_cache():
        from agent import clear_agent_cache as _cac
        _cac()

    def _secret_status_text(env_var: str) -> str:
        status = key_status(env_var)
        if not status.get("configured"):
            return "Not saved"
        source = status.get("source") or "saved"
        fingerprint = status.get("fingerprint") or "saved"
        if source == "keyring":
            return f"Saved securely ({fingerprint})"
        if source == "environment":
            return f"Set by environment ({fingerprint})"
        if source == "legacy_plaintext":
            return f"Saved in legacy plaintext ({fingerprint})"
        return f"Saved for this session ({fingerprint})"

    def _secret_input(label: str, env_var: str):
        status_label = ui.label(_secret_status_text(env_var)).classes("text-grey-6 text-xs")
        inp = ui.input(
            label,
            value="",
            placeholder="Paste a new value to replace the saved one",
            password=True,
            password_toggle_button=True,
        ).classes("w-full")

        def refresh_status() -> None:
            status_label.text = _secret_status_text(env_var)
            status_label.update()

        return inp, refresh_status

    def _secret_value_or_notify(raw: object, display: str) -> str:
        val = raw.strip() if isinstance(raw, str) else ""
        if not val:
            ui.notify(f"{display} unchanged — enter a new value to replace it", type="info")
        return val

    def _clear_secret(env_var: str, display: str, refresh: Callable[[], None] | None = None) -> None:
        delete_key(env_var)
        clear_agent_cache()
        if refresh:
            refresh()
        ui.notify(f"{display} cleared", type="info")

    # ══════════════════════════════════════════════════════════════════
    # TAB BUILDERS
    # ══════════════════════════════════════════════════════════════════

    def _build_documents_tab() -> None:
        ui.label("📄 Local Documents").classes("text-h6")
        ui.label(
            "Upload your own files (PDF, TXT, DOCX, MD, HTML, EPUB) to build a local knowledge base. "
            "Documents are chunked, vectorized, and stored in a local FAISS database "
            "for fast semantic search. Uploaded documents are also automatically "
            "analyzed to extract entities into your knowledge graph and wiki vault."
        ).classes("text-grey-6 text-sm")

        async def _handle_doc_upload(e: events.UploadEventArguments):
            name = e.file.name
            n = ui.notification(f"📄 Indexing {name}…", type="ongoing", spinner=True, timeout=None)
            # Clear the upload widget file list immediately
            doc_upload.reset()
            data = await e.file.read()
            with tempfile.NamedTemporaryFile(delete=False, suffix=pathlib.Path(name).suffix) as tmp:
                tmp.write(data)
                tmp_path = tmp.name
            try:
                await run.io_bound(load_and_vectorize_document, tmp_path, True, name)
                n.dismiss()
                ui.notify(f"✅ {name} indexed", type="positive")

                # Queue background knowledge extraction
                try:
                    from document_extraction import queue_extraction
                    staging_dir = pathlib.Path.home() / ".thoth" / "doc_staging"
                    staging_dir.mkdir(parents=True, exist_ok=True)
                    staging_path = staging_dir / name
                    import shutil
                    shutil.copy2(tmp_path, staging_path)
                    queue_extraction(str(staging_path), name)
                    ui.notify(f"🧠 Extracting knowledge from {name}…", type="info")
                except Exception as exc:
                    logger.warning("Failed to queue document extraction: %s", exc)
            except Exception as exc:
                n.dismiss()
                ui.notify(f"Failed: {exc}", type="negative")
            finally:
                os.unlink(tmp_path)

        doc_upload = ui.upload(
            label="Upload documents (PDF, DOCX, TXT, MD, HTML, EPUB)",
            on_upload=_handle_doc_upload,
            auto_upload=True,
            multiple=True,
        ).classes("w-full").props('flat bordered hide-upload-btn')

        ui.separator()
        processed = load_processed_files()
        if processed:
            ui.label(f"📚 {len(processed)} indexed document(s)").classes("font-bold")
            for f in sorted(processed):
                with ui.row().classes("items-center gap-1"):
                    ui.label(f"  • {f}").classes("text-sm")

                    def _make_delete(name=f):
                        async def _do_delete():
                            import knowledge_graph as kg
                            n = ui.notification(f"🗑️ Removing {name}…", type="ongoing", spinner=True, timeout=None)
                            try:
                                await run.io_bound(remove_document, name)
                                await run.io_bound(kg.delete_entities_by_source, f"document:{name}")
                                n.dismiss()
                                ui.notify(f"🗑️ Removed {name}", type="info")
                                _reopen("documents")
                            except Exception as exc:
                                n.dismiss()
                                ui.notify(f"Delete failed: {exc}", type="negative")
                        return _do_delete

                    ui.button(icon="delete", on_click=_make_delete(f)).props(
                        "flat dense round size=xs color=negative"
                    ).tooltip(f"Remove {f}")
        else:
            ui.label("No documents indexed yet.").classes("text-grey-6")

        ui.separator()

        _clearing_docs = False

        async def _clear_docs():
            nonlocal _clearing_docs
            if _clearing_docs:
                return
            _clearing_docs = True
            try:
                confirm = await ui.run_javascript(
                    "confirm('Clear ALL documents? This will remove all indexed files and their extracted knowledge. This cannot be undone.')",
                    timeout=30,
                )
                if confirm:
                    import knowledge_graph as kg
                    reset_vector_store()
                    kg.delete_entities_by_source_prefix("document:")
                    ui.notify("🗑️ All documents and extracted knowledge cleared.", type="info")
                    _reopen("documents")
            finally:
                _clearing_docs = False

        ui.button("🗑️ Clear all documents", on_click=_clear_docs).props("flat color=negative")

    # ── Models Tab ───────────────────────────────────────────────────

    def _build_models_tab() -> None:
        _ollama_up = _ollama_reachable()
        fetch_trending_ollama_models()
        trending = get_trending_models()

        ui.label("🤖 Models").classes("text-h6")
        ui.label(
            "Thoth uses two models: a Brain model for reasoning, tool use, "
            "and conversation, and a Vision model for camera-based image "
            "analysis. Local models are served through Ollama; cloud models "
            "use your configured API keys."
        ).classes("text-grey-6 text-sm")

        ui.label("✅ Downloaded  ⬇️ Available  🆕 Trending  ⬡ OpenAI  🌐 OpenRouter").classes("text-xs text-grey-5 q-mt-xs")
        ui.label("🔑 API keys can be managed in the Cloud tab.").classes("text-xs text-grey-5")

        ui.separator()
        ui.label("🧠 Brain Model").classes("text-h6")
        ui.label(
            "The main reasoning model that powers Thoth's conversations and "
            "tool use. Recommended: 14B+ for best accuracy. "
            "Minimum: 8B — smaller models may struggle with complex tasks."
        ).classes("text-grey-6 text-sm")

        local = list_local_models()
        cloud = list_cloud_models()
        current = state.current_model

        if _ollama_up:
            all_models = sorted(set(list_all_models() + cloud))
        else:
            all_models = sorted(set(cloud + ([current] if not is_cloud_model(current) else [])))

        if current not in all_models:
            all_models = sorted(set(all_models + [current]))

        def _model_label(m, local_override=None):
            loc = local_override if local_override is not None else local
            if is_cloud_model(m):
                return f"{get_provider_emoji(m)}  {m}"
            if m in loc:
                warn = '' if is_tool_compatible(m) else '  ⚠️ may not support tools'
                return f"✅  {m}{warn}"
            if m in trending:
                return f"🆕  {m}"
            warn = '' if is_tool_compatible(m) else '  ⚠️ may not support tools'
            return f"⬇️  {m}{warn}"

        model_opts = {m: _model_label(m) for m in all_models}

        model_select = ui.select(
            label="Select model",
            options=model_opts,
            value=current,
        ).classes("w-full").props('use-input input-debounce=300')

        brain_dl_btn = ui.button(f"⬇️ Download {current}").props("color=primary outline")
        brain_dl_btn.visible = _ollama_up and not is_cloud_model(current) and current not in local

        import sys as _sys
        if _sys.platform == "win32":
            _ollama_install_steps = (
                "1. Download Ollama from ollama.com/download\n"
                "2. Run the installer\n"
                "3. Ollama starts automatically — re-open Settings → Models"
            )
        elif _sys.platform == "darwin":
            _ollama_install_steps = (
                "1. Download Ollama from ollama.com/download (or: brew install ollama)\n"
                "2. Run: ollama serve\n"
                "3. Re-open Settings → Models"
            )
        else:
            _ollama_install_steps = (
                "1. Install: curl -fsSL https://ollama.com/install.sh | sh\n"
                "2. Run: ollama serve\n"
                "3. Re-open Settings → Models"
            )
        with ui.card().classes("w-full q-pa-md bg-amber-1") as ollama_guide:
            ui.label("🖥️ Want to use local models?").classes("text-weight-bold text-body1 text-brown-9")
            ui.label(
                "Local models run on your GPU with full privacy — no data leaves your machine. "
                "You need Ollama installed and running."
            ).classes("text-grey-8 text-sm q-mb-xs")
            ui.label(_ollama_install_steps).classes("text-grey-8 text-xs").style("white-space: pre-line")
            ui.link("Download Ollama →", "https://ollama.com/download", new_tab=True).classes("text-sm text-weight-bold")
        ollama_guide.visible = not _ollama_up

        async def _download_brain(e=None):
            sel = model_select.value
            if is_cloud_model(sel):
                ui.notify(f"{get_provider_emoji(sel)} {sel} is a cloud model — no download needed.", type="info")
                brain_dl_btn.visible = False
                return
            if is_model_local(sel):
                ui.notify(f"✅ {sel} is already downloaded.", type="info")
                brain_dl_btn.visible = False
                return
            if not _ollama_reachable():
                ui.notify("❌ Ollama is not running.", type="negative", close_button=True)
                return
            brain_dl_btn.disable()
            n = ui.notification(f"Downloading {sel}…", type="ongoing", spinner=True, timeout=None)
            await run.io_bound(lambda: list(pull_model(sel)))
            n.dismiss()
            ui.notify(f"✅ {sel} ready!", type="positive")
            brain_dl_btn.visible = False
            brain_dl_btn.enable()
            ollama_guide.visible = False
            refreshed_local = list_local_models()
            model_select.options = {m: _model_label(m, refreshed_local) for m in all_models}
            model_select.update()
            set_model(sel)
            state.current_model = sel
            clear_agent_cache()

        brain_dl_btn.on_click(_download_brain)

        _ctx_note_updater = [None]

        async def _on_model_change(e):
            sel = e.value
            if sel == state.current_model:
                return
            prev = state.current_model
            brain_dl_btn.text = f"⬇️ Download {sel}"
            brain_dl_btn.visible = _ollama_up and not is_cloud_model(sel) and not is_model_local(sel)
            if is_cloud_model(sel):
                set_model(sel)
                state.current_model = sel
                clear_agent_cache()
                if _ctx_note_updater[0]:
                    _ctx_note_updater[0]()
                return
            if not is_model_local(sel):
                return
            if not is_tool_compatible(sel):
                ui.notify(f"Checking tool support for {sel}…", type="info")
                ok = await run.io_bound(lambda: check_tool_support(sel))
                if not ok:
                    ui.notify(
                        f"⚠️ {sel} does not support tool calling. Reverting to {prev}.",
                        type="negative", close_button=True, timeout=10000,
                    )
                    model_select.value = prev
                    return
            set_model(sel)
            state.current_model = sel
            clear_agent_cache()
            if not is_cloud_model(sel):
                model_max = await run.io_bound(lambda: get_model_max_context(sel))
                user_val = get_user_context_size()
                if model_max is not None and user_val > model_max:
                    max_lbl = CONTEXT_SIZE_LABELS.get(model_max, f"{model_max:,}")
                    usr_lbl = CONTEXT_SIZE_LABELS.get(user_val, f"{user_val:,}")
                    ui.notify(
                        f"Context capped: {sel} max is {max_lbl} (you selected {usr_lbl}).",
                        type="warning", close_button=True, timeout=8000,
                    )
            if _ctx_note_updater[0]:
                _ctx_note_updater[0]()

        model_select.on_value_change(_on_model_change)

        ui.separator()

        # Context window
        _is_cloud_ctx = is_cloud_model(state.current_model)
        ctx_opts = {v: CONTEXT_SIZE_LABELS.get(v, str(v)) for v in CONTEXT_SIZE_OPTIONS}
        cloud_ctx_opts = {v: CLOUD_CONTEXT_SIZE_LABELS.get(v, str(v))
                         for v in CLOUD_CONTEXT_SIZE_OPTIONS}

        ctx_note = ui.label("").classes("text-xs text-warning")
        ctx_note.visible = False

        def _fmt_ctx(val):
            if val and val >= 1_000_000:
                return f"{val // 1_000_000}M"
            if val and val >= 1_000:
                return f"{val // 1_000}K"
            return "?"

        def _update_ctx_note():
            _cloud = is_cloud_model(state.current_model)
            cloud_ctx_select.visible = _cloud
            ctx_select.visible = not _cloud
            native_max = get_model_max_context()
            if _cloud:
                # Show effective context info below cloud dropdown
                effective = min(get_cloud_context_size(), native_max) if native_max else get_cloud_context_size()
                native_lbl = _fmt_ctx(native_max) if native_max else "?"
                ctx_note.text = f"ℹ️ Model native max: {native_lbl} — effective: {_fmt_ctx(effective)}"
                ctx_note.visible = True
            else:
                user_val = get_user_context_size()
                if native_max is not None and user_val > native_max:
                    max_label = CONTEXT_SIZE_LABELS.get(native_max, f"{native_max:,}")
                    ctx_note.text = f"ℹ️ Model max is {max_label} — trimming will use {max_label}"
                    ctx_note.visible = True
                else:
                    ctx_note.visible = False

        def _on_cloud_ctx_change(e):
            set_cloud_context_size(e.value)
            clear_agent_cache()
            _update_ctx_note()

        def _on_ctx_change(e):
            set_context_size(e.value)
            state.context_size = e.value
            clear_agent_cache()
            _update_ctx_note()
            model_max = get_model_max_context()
            if model_max is not None and e.value > model_max:
                max_lbl = CONTEXT_SIZE_LABELS.get(model_max, f"{model_max:,}")
                usr_lbl = CONTEXT_SIZE_LABELS.get(e.value, f"{e.value:,}")
                ui.notify(
                    f"Context capped: model max is {max_lbl} (you selected {usr_lbl}).",
                    type="warning", close_button=True, timeout=8000,
                )

        cloud_ctx_select = ui.select(
            label="☁️ Cloud context window",
            options=cloud_ctx_opts,
            value=get_cloud_context_size(),
            on_change=_on_cloud_ctx_change,
        ).classes("w-full").tooltip(
            "Caps how much conversation history is sent to the cloud model. "
            "Lower values reduce cost and rate-limit pressure."
        )
        cloud_ctx_select.visible = _is_cloud_ctx

        ctx_select = ui.select(
            label="Local context window",
            options=ctx_opts,
            value=state.context_size,
            on_change=_on_ctx_change,
        ).classes("w-full").tooltip(
            "Controls how many tokens the local model can process. Higher values use more VRAM."
        )
        ctx_select.visible = not _is_cloud_ctx

        _update_ctx_note()
        _ctx_note_updater[0] = _update_ctx_note

        ui.separator()
        ui.label("👁️ Vision Model").classes("text-h6")
        ui.label(
            "The model used for camera and screen capture analysis."
        ).classes("text-grey-6 text-sm")

        vsvc = state.vision_service
        cloud_vision = list_cloud_vision_models()

        if _ollama_up:
            all_vision = sorted(set(
                POPULAR_VISION_MODELS
                + cloud_vision
                + ([vsvc.model] if vsvc.model not in POPULAR_VISION_MODELS and vsvc.model not in cloud_vision else [])
            ))
        else:
            extras = [vsvc.model] if not is_cloud_model(vsvc.model) else []
            all_vision = sorted(set(cloud_vision + extras))

        def _vision_label(m, local_override=None):
            loc = local_override if local_override is not None else local
            if is_cloud_model(m):
                return f"{get_provider_emoji(m)}  {m}"
            if m in loc:
                return f"✅  {m}"
            if m in trending:
                return f"🆕  {m}"
            return f"⬇️  {m}"

        vision_opts = {m: _vision_label(m) for m in all_vision}
        vision_select = ui.select(options=vision_opts, value=vsvc.model).classes("w-full").props('use-input input-debounce=300')

        vision_dl_btn = ui.button(f"⬇️ Download {vsvc.model}").props("color=primary outline")
        vision_dl_btn.visible = _ollama_up and not is_cloud_model(vsvc.model) and vsvc.model not in local

        async def _download_vision(e=None):
            sel = vision_select.value
            if is_cloud_model(sel):
                ui.notify(f"{get_provider_emoji(sel)} {sel} is a cloud model — no download needed.", type="info")
                vision_dl_btn.visible = False
                return
            if is_model_local(sel):
                ui.notify(f"✅ {sel} is already downloaded.", type="info")
                vision_dl_btn.visible = False
                return
            if not _ollama_reachable():
                ui.notify("❌ Ollama is not running.", type="negative", close_button=True)
                return
            vision_dl_btn.disable()
            n = ui.notification(f"Downloading {sel}…", type="ongoing", spinner=True, timeout=None)
            await run.io_bound(lambda: list(pull_model(sel)))
            n.dismiss()
            ui.notify(f"✅ {sel} ready!", type="positive")
            vision_dl_btn.visible = False
            vision_dl_btn.enable()
            refreshed_local = list_local_models()
            vision_select.options = {m: _vision_label(m, refreshed_local) for m in all_vision}
            vision_select.update()
            vsvc.model = sel
            clear_agent_cache()

        vision_dl_btn.on_click(_download_vision)

        async def _on_vision_change(e):
            sel = e.value
            is_cloud = is_cloud_model(sel)
            vision_dl_btn.text = f"⬇️ Download {sel}"
            vision_dl_btn.visible = _ollama_up and not is_cloud and not is_model_local(sel)
            if sel != vsvc.model:
                if is_cloud:
                    vsvc.model = sel
                    clear_agent_cache()
                    return
                if not is_model_local(sel):
                    return
                vsvc.model = sel
                clear_agent_cache()

        vision_select.on_value_change(_on_vision_change)

        from vision import list_cameras
        cameras = list_cameras()
        if cameras:
            cam_opts = {i: f"Camera {i}" for i in cameras}
            ui.select(label="Camera", options=cam_opts, value=vsvc.camera_index,
                      on_change=lambda e: setattr(vsvc, "camera_index", e.value)).classes("w-full")
        else:
            ui.label("No cameras detected.").classes("text-grey-6 text-sm")

        ui.switch("Enable vision", value=vsvc.enabled,
                  on_change=lambda e: setattr(vsvc, "enabled", e.value)
        ).tooltip("Allow the agent to capture images from your webcam.")

        # ── Image Generation ─────────────────────────────────────────
        ui.separator()
        ui.label("🎨 Image Generation").classes("text-h6")
        ui.label(
            "Generate and edit images using AI models. Requires an OpenAI, Google, or xAI API key."
        ).classes("text-grey-6 text-sm")

        from tools.image_gen_tool import get_available_image_models, DEFAULT_MODEL
        _ig_tool = tool_registry.get_tool("image_gen")
        _ig_enabled = tool_registry.is_enabled("image_gen") if _ig_tool else False
        _ig_model = _ig_tool.get_config("model", DEFAULT_MODEL) if _ig_tool else DEFAULT_MODEL

        _ig_model_opts = get_available_image_models()
        if not _ig_model_opts:
            ui.label(
                "⚠️ No API keys configured. Add an OpenAI, Google, or xAI key in the Cloud tab."
            ).classes("text-warning text-sm")
        else:
            # Ensure the current value is in the options (may be from another provider)
            if _ig_model not in _ig_model_opts:
                _ig_model = next(iter(_ig_model_opts))
                if _ig_tool:
                    _ig_tool.set_config("model", _ig_model)
            ui.select(
                label="Image model",
                options=_ig_model_opts,
                value=_ig_model,
                on_change=lambda e: _ig_tool.set_config("model", e.value) if _ig_tool else None,
            ).classes("w-full")

        ui.switch(
            "Enable image generation",
            value=_ig_enabled,
            on_change=lambda e: tool_registry.set_enabled("image_gen", e.value),
        ).tooltip("Allow the agent to generate and edit images.")

        # ── Video Generation ─────────────────────────────────────────
        ui.separator()
        ui.label("🎬 Video Generation").classes("text-h6")
        ui.label(
            "Generate videos and animate images using AI models. Requires a Google or xAI API key."
        ).classes("text-grey-6 text-sm")

        from tools.video_gen_tool import get_available_video_models, DEFAULT_MODEL as _VG_DEFAULT
        _vg_tool = tool_registry.get_tool("video_gen")
        _vg_enabled = tool_registry.is_enabled("video_gen") if _vg_tool else False
        _vg_model = _vg_tool.get_config("model", _VG_DEFAULT) if _vg_tool else _VG_DEFAULT

        _vg_model_opts = get_available_video_models()
        if not _vg_model_opts:
            ui.label(
                "⚠️ No API keys configured. Add a Google or xAI key in the Cloud tab."
            ).classes("text-warning text-sm")
        else:
            if _vg_model not in _vg_model_opts:
                _vg_model = next(iter(_vg_model_opts))
                if _vg_tool:
                    _vg_tool.set_config("model", _vg_model)
            ui.select(
                label="Video model",
                options=_vg_model_opts,
                value=_vg_model,
                on_change=lambda e: _vg_tool.set_config("model", e.value) if _vg_tool else None,
            ).classes("w-full")

        ui.switch(
            "Enable video generation",
            value=_vg_enabled,
            on_change=lambda e: tool_registry.set_enabled("video_gen", e.value),
        ).tooltip("Allow the agent to generate videos and animate images.")

    # ── Cloud Tab ────────────────────────────────────────────────────

    def _build_cloud_tab() -> None:
        ui.label("☁️ Cloud Models").classes("text-h6")
        ui.label(
            "Connect to cloud LLMs via OpenAI, Anthropic, Google, or OpenRouter (100+ models)."
        ).classes("text-grey-6 text-sm")

        _model_list_container = None
        _search_term = {"value": ""}

        def _refresh_model_list():
            nonlocal _model_list_container
            _model_list_container.clear()
            _starred_now = set(get_cloud_config().get("starred_models", []))
            all_models = list_cloud_models()
            if not all_models:
                with _model_list_container:
                    ui.label("No cloud models loaded. Enter a key and click Refresh.").classes("text-grey-6 text-sm")
                return
            q = _search_term["value"].strip().lower()
            if q:
                all_models = [m for m in all_models if q in m.lower()]
            with _model_list_container:
                if q and not all_models:
                    ui.label(f'No models matching "{q}"').classes("text-grey-6 text-sm")
                    return
                for prov_label, prov_key in [("OpenAI", "openai"), ("Anthropic", "anthropic"), ("Google", "google"), ("xAI", "xai"), ("OpenRouter", "openrouter")]:
                    prov_models = [(m, _cloud_model_cache[m]) for m in all_models
                                   if _cloud_model_cache[m]["provider"] == prov_key]
                    if not prov_models:
                        continue
                    ui.label(f"{prov_label} ({len(prov_models)} models)").style(
                        "font-weight: 600; margin-top: 8px;"
                    )
                    for mid, info in prov_models:
                        with ui.row().classes("items-center gap-1 q-py-xs"):
                            is_starred = mid in _starred_now
                            ui.button(
                                icon="star" if is_starred else "star_border",
                                on_click=lambda _, m=mid: _toggle_star(m),
                            ).props("flat dense round size=sm").style(
                                f"color: {'gold' if is_starred else 'grey'};"
                            )
                            _emoji = get_provider_emoji(mid)
                            ui.label(_emoji).style("font-size: 1rem;")
                            ui.label(mid).style("font-weight: 500; font-size: 0.85rem;")
                            ctx_k = info["ctx"] // 1000 if info["ctx"] >= 1000 else info["ctx"]
                            ctx_label = f"{ctx_k}K" if info["ctx"] < 1_000_000 else f"{info['ctx'] // 1_000_000}M"
                            ui.label(f"({ctx_label} ctx)").classes("text-grey-6 text-xs")
                            if mid == get_current_model():
                                ui.badge("DEFAULT", color="cyan").props("dense")
                            else:
                                ui.button(
                                    "Set default", icon="check",
                                    on_click=lambda _, m=mid: _set_default_model(m),
                                ).props("flat dense size=xs")

        def _toggle_star(model_id):
            starred_now = set(get_cloud_config().get("starred_models", []))
            if model_id in starred_now:
                unstar_cloud_model(model_id)
            else:
                star_cloud_model(model_id)
            _refresh_model_list()

        def _set_default_model(model_id):
            set_model(model_id)
            state.current_model = model_id
            clear_agent_cache()
            ui.notify(f"Default model set to {model_id}", type="positive")
            _refresh_model_list()

        async def _do_refresh():
            n = ui.notification("Fetching models…", type="ongoing", spinner=True, timeout=None)
            count = await run.io_bound(refresh_cloud_models)
            n.dismiss()
            ui.notify(f"Found {count} cloud models", type="positive")
            _refresh_model_list()

        # API Keys
        ui.separator()
        with ui.expansion("🔑 OpenAI Direct", icon="key", value=False).classes("w-full"):
            ui.label("Direct access to OpenAI models.").classes("text-grey-6 text-sm")
            oai_input, oai_refresh = _secret_input("OpenAI API Key", "OPENAI_API_KEY")

            async def _save_oai():
                val = _secret_value_or_notify(oai_input.value, "OpenAI key")
                if not val:
                    return
                set_key("OPENAI_API_KEY", val)
                oai_input.value = ""
                oai_input.update()
                oai_refresh()
                ui.notify("OpenAI key saved ✅", type="positive")
                await run.io_bound(refresh_cloud_models)
                _refresh_model_list()
            with ui.row().classes("gap-2"):
                ui.button("Save Key", icon="save", on_click=_save_oai).props("flat dense")
                ui.button("Clear", icon="delete", on_click=lambda: _clear_secret("OPENAI_API_KEY", "OpenAI key", oai_refresh)).props("flat dense color=negative")

        with ui.expansion("🌐 OpenRouter", icon="language", value=False).classes("w-full"):
            ui.label("One key for Claude, Gemini, Llama, and 100+ more.").classes("text-grey-6 text-sm")
            or_input, or_refresh = _secret_input("OpenRouter API Key", "OPENROUTER_API_KEY")

            async def _save_or():
                val = _secret_value_or_notify(or_input.value, "OpenRouter key")
                if not val:
                    return
                valid = await run.io_bound(validate_openrouter_key, val)
                if not valid:
                    ui.notify("❌ Invalid OpenRouter API key", type="negative")
                    return
                set_key("OPENROUTER_API_KEY", val)
                or_input.value = ""
                or_input.update()
                or_refresh()
                ui.notify("OpenRouter key saved ✅", type="positive")
                await run.io_bound(refresh_cloud_models)
                _refresh_model_list()
            with ui.row().classes("gap-2"):
                ui.button("Save Key", icon="save", on_click=_save_or).props("flat dense")
                ui.button("Clear", icon="delete", on_click=lambda: _clear_secret("OPENROUTER_API_KEY", "OpenRouter key", or_refresh)).props("flat dense color=negative")

        with ui.expansion("🔶 Anthropic", icon="smart_toy", value=False).classes("w-full"):
            ui.label("Direct access to Claude models.").classes("text-grey-6 text-sm")
            anth_input, anth_refresh = _secret_input("Anthropic API Key", "ANTHROPIC_API_KEY")

            async def _save_anth():
                val = _secret_value_or_notify(anth_input.value, "Anthropic key")
                if not val:
                    return
                valid = await run.io_bound(validate_anthropic_key, val)
                if not valid:
                    ui.notify("❌ Invalid Anthropic API key", type="negative")
                    return
                set_key("ANTHROPIC_API_KEY", val)
                anth_input.value = ""
                anth_input.update()
                anth_refresh()
                ui.notify("Anthropic key saved ✅", type="positive")
                await run.io_bound(refresh_cloud_models)
                _refresh_model_list()
            with ui.row().classes("gap-2"):
                ui.button("Save Key", icon="save", on_click=_save_anth).props("flat dense")
                ui.button("Clear", icon="delete", on_click=lambda: _clear_secret("ANTHROPIC_API_KEY", "Anthropic key", anth_refresh)).props("flat dense color=negative")

        with ui.expansion("💎 Google AI", icon="diamond", value=False).classes("w-full"):
            ui.label("Direct access to Gemini models.").classes("text-grey-6 text-sm")
            goog_input, goog_refresh = _secret_input("Google AI API Key", "GOOGLE_API_KEY")

            async def _save_goog():
                val = _secret_value_or_notify(goog_input.value, "Google AI key")
                if not val:
                    return
                valid = await run.io_bound(validate_google_key, val)
                if not valid:
                    ui.notify("❌ Invalid Google AI API key", type="negative")
                    return
                set_key("GOOGLE_API_KEY", val)
                goog_input.value = ""
                goog_input.update()
                goog_refresh()
                ui.notify("Google AI key saved ✅", type="positive")
                await run.io_bound(refresh_cloud_models)
                _refresh_model_list()
            with ui.row().classes("gap-2"):
                ui.button("Save Key", icon="save", on_click=_save_goog).props("flat dense")
                ui.button("Clear", icon="delete", on_click=lambda: _clear_secret("GOOGLE_API_KEY", "Google AI key", goog_refresh)).props("flat dense color=negative")

        with ui.expansion("𝕏 xAI", icon="auto_awesome", value=False).classes("w-full"):
            ui.label("Access Grok models for chat and image generation.").classes("text-grey-6 text-sm")
            xai_input, xai_refresh = _secret_input("xAI API Key", "XAI_API_KEY")

            async def _save_xai():
                val = _secret_value_or_notify(xai_input.value, "xAI key")
                if not val:
                    return
                valid = await run.io_bound(validate_xai_key, val)
                if not valid:
                    ui.notify("⚠️ xAI key validation failed — saving anyway. "
                              "Models will appear if the key is valid.",
                              type="warning", timeout=5000)
                set_key("XAI_API_KEY", val)
                xai_input.value = ""
                xai_input.update()
                xai_refresh()
                ui.notify("xAI key saved ✅", type="positive")
                await run.io_bound(refresh_cloud_models)
                _refresh_model_list()
            with ui.row().classes("gap-2"):
                ui.button("Save Key", icon="save", on_click=_save_xai).props("flat dense")
                ui.button("Clear", icon="delete", on_click=lambda: _clear_secret("XAI_API_KEY", "xAI key", xai_refresh)).props("flat dense color=negative")

        # Setup Guide
        ui.separator()
        with ui.expansion("📖 Setup Guide", icon="help_outline").classes("w-full"):
            ui.markdown(
                "### OpenAI Direct\n\n"
                "1. Go to [platform.openai.com](https://platform.openai.com) → API Keys\n"
                "2. Create a new key and paste it above\n\n"
                "### Anthropic (Claude)\n\n"
                "1. Go to [console.anthropic.com](https://console.anthropic.com) → API Keys\n"
                "2. Create a new key and paste it above\n\n"
                "### Google AI (Gemini)\n\n"
                "1. Go to [aistudio.google.com](https://aistudio.google.com/apikey) → Get API Key\n"
                "2. Create a new key and paste it above\n\n"
                "### xAI (Grok)\n\n"
                "1. Go to [console.x.ai](https://console.x.ai) → API Keys\n"
                "2. Create a new key and paste it above\n\n"
                "### OpenRouter\n\n"
                "1. Go to [openrouter.ai](https://openrouter.ai) and create an account\n"
                "2. Navigate to **Keys** → **Create Key** and paste it above\n\n"
                "### Usage\n\n"
                "- ⭐ **Star** models to add them to the chat header model picker\n"
                "- Click **Set default** to use a cloud model as your app-wide default\n"
                "- Use `/model <id>` in Telegram to switch models per-chat\n"
                "- Cloud models appear with provider-specific icons in the sidebar\n"
                "- All API keys are stored locally and never shared"
            )

        # Available Models
        ui.separator()
        with ui.row().classes("items-center gap-2"):
            ui.label("Available Models").style("font-weight: 600;")
            ui.button(icon="refresh", on_click=_do_refresh).props("flat round dense").tooltip(
                "Refresh model list from cloud providers"
            )
        ui.label(
            "⭐ Star models to show them in the thread model picker."
        ).classes("text-grey-6 text-sm")

        def _on_search(e):
            _search_term["value"] = e.value or ""
            _refresh_model_list()

        ui.input(
            placeholder="Search models…",
            on_change=_on_search,
        ).classes("w-full").props("outlined dense clearable").style("max-width: 400px;")

        _model_list_container = ui.column().classes("w-full")

        async def _initial_fetch():
            await run.io_bound(refresh_cloud_models)
            _refresh_model_list()
        ui.timer(0.5, _initial_fetch, once=True)

    # ── Skills Tab ───────────────────────────────────────────────────

    def _build_skills_tab() -> None:
        import skills as skills_mod

        ui.label("✨ Skills").classes("text-h6")
        ui.label(
            "Skills teach the agent step-by-step workflows using your existing tools."
        ).classes("text-grey-6 text-sm")
        ui.separator().classes("q-my-md")

        with ui.row().classes("w-full justify-end q-mb-md"):
            ui.button("Create Skill", icon="add", on_click=lambda: _open_skill_editor()).props("color=primary")

        skills_container = ui.column().classes("w-full gap-2")

        def _refresh_skills_list():
            skills_container.clear()
            all_skills = skills_mod.get_manual_skills()
            if not all_skills:
                with skills_container:
                    ui.label("No skills found. Create one to get started!").classes("text-grey-5 italic")
                return

            with skills_container:
                for sk in all_skills:
                    with ui.card().classes("w-full q-pa-sm"):
                        with ui.row().classes("w-full items-center no-wrap"):
                            ui.switch(
                                "",
                                value=skills_mod.is_enabled(sk.name),
                                on_change=lambda e, n=sk.name: skills_mod.set_enabled(n, e.value),
                            )
                            ui.label(f"{sk.icon} {sk.display_name}").classes("text-body1 text-weight-medium")
                            ui.space()
                            if sk.source == "bundled":
                                ui.badge("Bundled", color="blue-grey").props("outline")
                            else:
                                ui.badge("Custom", color="teal").props("outline")
                            tokens = skills_mod.estimate_tokens([sk.name])
                            if tokens > 0:
                                ui.badge(f"~{tokens} tokens", color="orange").props(
                                    "outline"
                                ).tooltip("Approximate tokens added to context when enabled")
                        ui.label(sk.description).classes("text-grey-6 text-sm q-pl-lg")

                        with ui.row().classes("q-pl-lg q-mt-xs gap-1"):
                            if sk.source == "user":
                                ui.button(
                                    "Edit", icon="edit",
                                    on_click=lambda _, n=sk.name: _open_skill_editor(n),
                                ).props("flat dense size=sm")
                                ui.button(
                                    "Delete", icon="delete",
                                    on_click=lambda _, n=sk.name: _confirm_delete_skill(n),
                                ).props("flat dense size=sm color=negative")
                            else:
                                ui.button(
                                    "Duplicate & Customise", icon="content_copy",
                                    on_click=lambda _, n=sk.name: _duplicate_skill(n),
                                ).props("flat dense size=sm")

        def _open_skill_editor(name=None):
            skill = skills_mod.get_skill(name) if name else None
            is_edit = skill is not None

            with ui.dialog().props("persistent maximized=false") as dlg, ui.card().classes(
                "w-full"
            ).style("min-width: 600px; max-width: 800px;"):
                ui.label(f"{'Edit' if is_edit else 'Create'} Skill").classes("text-h6")
                name_input = ui.input(
                    "Name (identifier)",
                    value=skill.name if skill else "",
                    validation={"Required": lambda v: bool(v.strip())},
                ).classes("w-full")
                if is_edit:
                    name_input.props("readonly")

                display_input = ui.input(
                    "Display Name", value=skill.display_name if skill else "",
                ).classes("w-full")

                _wf_icon_opts = list(ICON_OPTIONS)
                _icon = skill.icon if skill else "✨"
                if _icon not in _wf_icon_opts:
                    _wf_icon_opts.insert(0, _icon)
                with ui.row().classes("w-full items-end gap-4"):
                    icon_sel = ui.select(label="Icon", options=_wf_icon_opts, value=_icon).classes("w-20")
                    desc_input = ui.input(
                        "Description (one line)", value=skill.description if skill else "",
                    ).classes("flex-grow")

                tags_input = ui.input(
                    "Tags (comma-separated)",
                    value=", ".join(skill.tags) if skill and skill.tags else "",
                ).classes("w-full")

                # Tools linking — if set, skill auto-activates when any listed tool is enabled
                from tools import registry as _tools_reg
                _available_tools = sorted([t.name for t in _tools_reg.get_all_tools()])
                _current_tools = list(skill.tools) if skill and skill.tools else []
                tools_select = ui.select(
                    label="Linked Tools (auto-activates when tool is enabled)",
                    options=_available_tools,
                    value=_current_tools,
                    multiple=True,
                ).classes("w-full").props('use-chips clearable')
                ui.label(
                    "Leave empty for a manually-toggled skill. "
                    "Link to tools to auto-activate this skill when those tools are enabled."
                ).classes("text-grey-5 text-xs")

                ui.label("Instructions").classes("text-sm font-bold mt-4")
                instructions_input = ui.textarea(
                    value=skill.instructions if skill else "",
                ).classes("w-full").props('rows="12"')

                def _update_token_est():
                    txt = instructions_input.value or ""
                    est = len(txt) // 4
                    token_label.text = f"~{est} tokens"

                with ui.row().classes("w-full items-center"):
                    token_label = ui.label("~0 tokens").classes("text-grey-5 text-sm")
                    _update_token_est()
                    instructions_input.on("blur", lambda: _update_token_est())

                with ui.row().classes("w-full justify-end gap-2 q-mt-md"):
                    ui.button("Cancel", on_click=dlg.close).props("flat")

                    def _save():
                        _name = name_input.value.strip()
                        _display = display_input.value.strip() or _name.replace("_", " ").title()
                        _desc = desc_input.value.strip()
                        _icon_val = icon_sel.value
                        _instr = instructions_input.value.strip()
                        _tags = [t.strip() for t in tags_input.value.split(",") if t.strip()]
                        _tools = list(tools_select.value) if tools_select.value else None
                        if not _name:
                            ui.notify("Name is required", type="warning")
                            return
                        if not _instr:
                            ui.notify("Instructions are required", type="warning")
                            return
                        if is_edit:
                            skills_mod.update_skill(
                                name=_name, display_name=_display, icon=_icon_val,
                                description=_desc, instructions=_instr, tags=_tags,
                                tools=_tools,
                            )
                            ui.notify(f"✅ Skill '{_display}' updated", type="positive")
                        else:
                            skills_mod.create_skill(
                                name=_name, display_name=_display, icon=_icon_val,
                                description=_desc, instructions=_instr, tags=_tags,
                                tools=_tools,
                            )
                            ui.notify(f"✅ Skill '{_display}' created", type="positive")
                        dlg.close()
                        _refresh_skills_list()

                    ui.button("Save", icon="save", on_click=_save).props("color=primary")

            dlg.open()

        def _confirm_delete_skill(name):
            sk = skills_mod.get_skill(name)
            if not sk:
                return
            with ui.dialog() as dlg, ui.card():
                ui.label(f"Delete skill '{sk.display_name}'?").classes("text-body1")
                ui.label("This will permanently remove the skill files.").classes("text-grey-6 text-sm")
                with ui.row().classes("w-full justify-end gap-2 q-mt-md"):
                    ui.button("Cancel", on_click=dlg.close).props("flat")
                    def _do_delete():
                        skills_mod.delete_skill(name)
                        ui.notify(f"Skill '{sk.display_name}' deleted", type="info")
                        dlg.close()
                        _refresh_skills_list()
                    ui.button("Delete", on_click=_do_delete).props("color=negative")
            dlg.open()

        def _duplicate_skill(name):
            result = skills_mod.duplicate_skill(name)
            if result:
                ui.notify(f"✅ Duplicated as '{result.display_name}'", type="positive")
                _refresh_skills_list()
            else:
                ui.notify("Failed to duplicate skill", type="negative")

        skills_mod.load_skills()
        _refresh_skills_list()

    # ── Search / Tools Tab ───────────────────────────────────────────

    def _build_tools_tab() -> None:
        ui.label("⚡ Retrieval Compression").classes("text-h6")
        ui.label(
            "Controls how search results are filtered before reaching the model. "
            "Off uses the built-in context trimmer. Deep uses extra LLM calls for precise extraction."
        ).classes("text-grey-6 text-sm")
        _comp_options = {"off": "Off (default)", "deep": "Deep (LLM)"}
        ui.select(
            label="Compression mode",
            options=_comp_options,
            value=tool_registry.get_global_config("compression_mode", "off"),
            on_change=lambda e: tool_registry.set_global_config("compression_mode", e.value),
        ).classes("w-60")
        ui.separator().classes("q-my-md")

        ui.label("🔍 Search & Knowledge Tools").classes("text-h6")
        ui.label("Enable or disable search and knowledge tools.").classes("text-grey-6 text-sm")
        ui.separator()

        search_tools = {
            "web_search", "duckduckgo", "wolfram_alpha", "arxiv",
            "wikipedia", "youtube",
        }
        for tool in tool_registry.get_all_tools():
            if tool.name not in search_tools:
                continue
            _build_tool_toggle(tool)
            ui.separator()

    def _build_tool_toggle(tool) -> None:
        ui.switch(
            tool.display_name,
            value=tool_registry.is_enabled(tool.name),
            on_change=lambda e, n=tool.name: tool_registry.set_enabled(n, e.value),
        ).tooltip(tool.description)

        if tool.name == "web_search":
            with ui.expansion("📋 Tavily Setup Instructions"):
                ui.markdown(
                    "1. Go to [app.tavily.com](https://app.tavily.com/) and sign up.\n"
                    "2. Create an API key.\n"
                    "3. Paste the key below.",
                    extras=['code-friendly', 'fenced-code-blocks', 'tables'],
                )
        elif tool.name == "wolfram_alpha":
            with ui.expansion("📋 Wolfram Alpha Setup Instructions"):
                ui.markdown(
                    "1. Go to [developer.wolframalpha.com](https://developer.wolframalpha.com/) and sign up.\n"
                    "2. Click **Get an AppID** and create an app.\n"
                    "3. Paste the AppID below.",
                    extras=['code-friendly', 'fenced-code-blocks', 'tables'],
                )

        if tool.required_api_keys:
            for label, env_var in tool.required_api_keys.items():
                inp, refresh = _secret_input(label, env_var)
                with ui.row().classes("gap-2"):
                    def _save_tool_key(ev=env_var, widget=inp, refresh_status=refresh, display=label):
                        val = _secret_value_or_notify(widget.value, display)
                        if not val:
                            return
                        set_key(ev, val)
                        widget.value = ""
                        widget.update()
                        refresh_status()
                        ui.notify(f"{display} saved", type="positive")

                    ui.button("Save", icon="save", on_click=_save_tool_key).props("flat dense")
                    ui.button(
                        "Clear",
                        icon="delete",
                        on_click=lambda ev=env_var, display=label, refresh_status=refresh: _clear_secret(ev, display, refresh_status),
                    ).props("flat dense color=negative")

        schema = tool.config_schema
        if schema:
            for cfg_key, spec in schema.items():
                cfg_type = spec.get("type", "text")
                cfg_label = spec.get("label", cfg_key)
                cfg_default = spec.get("default")
                current_cfg = tool.get_config(cfg_key, cfg_default)
                if cfg_type == "text":
                    ui.input(
                        cfg_label, value=current_cfg or "",
                        on_change=lambda e, t=tool, k=cfg_key: t.set_config(k, e.value),
                    ).classes("w-full")
                elif cfg_type == "select":
                    options = spec.get("options", [])
                    labels_map = spec.get("labels", {})
                    option_labels = {o: labels_map.get(o, o) for o in options}
                    ui.select(
                        option_labels,
                        value=current_cfg or cfg_default,
                        label=cfg_label,
                        on_change=lambda e, t=tool, k=cfg_key: t.set_config(k, e.value),
                    ).classes("w-full")
                elif cfg_type == "multicheck":
                    options = spec.get("options", [])
                    current_list = current_cfg if isinstance(current_cfg, list) else (cfg_default or [])
                    ui.label(cfg_label).classes("text-sm font-bold mt-2")
                    for opt in options:
                        ui.checkbox(
                            opt, value=opt in current_list,
                            on_change=lambda e, t=tool, k=cfg_key, o=opt, cl=current_list: (
                                cl.append(o) if e.value and o not in cl else (cl.remove(o) if not e.value and o in cl else None),
                                t.set_config(k, list(cl)),
                            ),
                        )

    def _build_ops_checkboxes(groups, current_ops, tool, cfg_key="selected_operations"):
        ui.label("Allowed operations").classes("text-sm font-bold mt-2")
        selected = list(current_ops)

        def _toggle(op, val):
            if val and op not in selected:
                selected.append(op)
            elif not val and op in selected:
                selected.remove(op)
            tool.set_config(cfg_key, list(selected))

        with ui.row().classes("w-full gap-8"):
            for header, ops in groups:
                with ui.column():
                    ui.label(header).classes("font-bold text-sm")
                    for op in ops:
                        ui.checkbox(op, value=op in current_ops,
                                    on_change=lambda e, o=op: _toggle(o, e.value))

    # ── System Access Tab ────────────────────────────────────────────

    def _build_system_access_tab() -> None:
        from tools.filesystem_tool import _SAFE_OPS, _WRITE_OPS, _DESTRUCTIVE_OPS

        ui.label("🖥️ System Access").classes("text-h6")
        ui.label("Give Thoth access to your local system.").classes("text-grey-6 text-sm")

        fs_tool = tool_registry.get_tool("filesystem")
        if not fs_tool:
            ui.label("Filesystem tool not found.").classes("text-negative")
            return

        ui.separator()
        ui.label("📂 Workspace Folder").classes("text-subtitle1 font-bold")
        ui.label(
            "The Filesystem tool is sandboxed to this folder."
        ).classes("text-grey-6 text-xs")

        fs_root_default = fs_tool.config_schema.get("workspace_root", {}).get("default", "")
        current_root = fs_tool.get_config("workspace_root", fs_root_default)
        root_input = ui.input(
            "Workspace folder", value=current_root or "",
            on_change=lambda e: fs_tool.set_config("workspace_root", e.value),
        ).classes("w-full")

        async def _browse_ws():
            folder = await browse_folder("Select Workspace folder", current_root)
            if folder:
                root_input.value = folder
                fs_tool.set_config("workspace_root", folder)

        ui.button("Browse…", on_click=_browse_ws).props("flat dense")

        if current_root and not os.path.isdir(current_root):
            ui.label(f"⚠️ Folder not found: {current_root}").classes("text-warning text-sm")

        # Shell Access
        ui.separator()
        ui.label("🖥️ Shell Access").classes("text-subtitle1 font-bold")
        ui.label("Run shell commands directly on your system.").classes("text-grey-6 text-xs")

        shell_tool = tool_registry.get_tool("shell")
        if shell_tool:
            ui.switch(
                "Enable Shell tool",
                value=tool_registry.is_enabled("shell"),
                on_change=lambda e: tool_registry.set_enabled("shell", e.value),
            ).tooltip(shell_tool.description)

            shell_blocked = shell_tool.get_config("blocked_commands", "")
            ui.input(
                "Additional blocked patterns (comma-separated)",
                value=shell_blocked or "",
                on_change=lambda e: shell_tool.set_config("blocked_commands", e.value),
            ).classes("w-full")
        else:
            ui.label("Shell tool not found.").classes("text-grey-6 text-sm")

        # Browser Automation
        ui.separator()
        ui.label("🌐 Browser Automation").classes("text-subtitle1 font-bold")
        ui.label("Open a real browser window that you and the agent share.").classes("text-grey-6 text-xs")

        browser_tool = tool_registry.get_tool("browser")
        if browser_tool:
            ui.switch(
                "Enable Browser tool",
                value=tool_registry.is_enabled("browser"),
                on_change=lambda e: tool_registry.set_enabled("browser", e.value),
            ).tooltip(browser_tool.description)
        else:
            ui.label("Browser tool not found.").classes("text-grey-6 text-sm")

        # File Operations
        ui.separator()
        ui.label("📁 File Operations").classes("text-subtitle1 font-bold")
        ui.label("Read, write, search, copy, move, and delete files.").classes("text-grey-6 text-xs")

        ui.switch(
            "Enable Filesystem tool",
            value=tool_registry.is_enabled("filesystem"),
            on_change=lambda e: tool_registry.set_enabled("filesystem", e.value),
        ).tooltip(fs_tool.description)

        ops_default = fs_tool.config_schema.get("selected_operations", {}).get("default", [])
        current_ops = fs_tool.get_config("selected_operations", ops_default)
        if not isinstance(current_ops, list):
            current_ops = ops_default
        _build_ops_checkboxes(
            [("Read-only", _SAFE_OPS), ("Write", _WRITE_OPS), ("⚠️ Destructive", _DESTRUCTIVE_OPS)],
            current_ops, fs_tool,
        )

        # ── Logging ──────────────────────────────────────────────────
        ui.separator()
        ui.label("📝 Logging").classes("text-subtitle1 font-bold")
        ui.label(
            "Structured logs are saved daily to ~/.thoth/logs/ (7-day retention)."
        ).classes("text-grey-6 text-xs")

        from logging_config import get_file_log_level, set_file_log_level, get_log_dir

        _level_options = ["DEBUG", "INFO", "WARNING", "ERROR"]
        ui.select(
            _level_options,
            value=get_file_log_level(),
            label="File log level",
            on_change=lambda e: set_file_log_level(e.value),
        ).classes("w-48").tooltip("Minimum severity written to log files")

        async def _open_log_folder():
            import subprocess, sys
            log_dir = str(get_log_dir())
            if sys.platform == "win32":
                subprocess.Popen(["explorer", log_dir])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", log_dir])
            else:
                subprocess.Popen(["xdg-open", log_dir])

        ui.button("Open Log Folder", icon="folder_open", on_click=_open_log_folder).props(
            "flat dense no-caps"
        )

        # ── Window Mode ──────────────────────────────────────────────
        ui.separator()
        ui.label("🪟 Window Mode").classes("text-subtitle1 font-bold")
        ui.label(
            "Choose how Thoth opens on launch. "
            "\"Native Window\" provides a dedicated app window. "
            "\"System Browser\" opens in your default browser "
            "(avoids macOS WebView issues)."
        ).classes("text-grey-6 text-xs")

        from ui.helpers import load_app_config, save_app_config

        _wm_cfg = load_app_config()
        _current_mode = _wm_cfg.get("window_mode", "ask")

        def _on_window_mode_change(e):
            cfg = load_app_config()
            cfg["window_mode"] = e.value
            save_app_config(cfg)

        ui.select(
            {"ask": "Ask on Launch", "native": "Native Window", "browser": "System Browser"},
            value=_current_mode,
            label="Window mode",
            on_change=_on_window_mode_change,
        ).classes("w-64").tooltip("Takes effect on next launch")

        ui.label("Takes effect on next launch.").classes("text-grey-7 text-xs q-mt-xs")

    # ── Google Account Tab (unified Gmail + Calendar) ──────────────

    def _build_google_account_panel() -> None:
        import shutil
        gmail_tool = tool_registry.get_tool("gmail")
        cal_tool = tool_registry.get_tool("calendar")
        if not gmail_tool or not cal_tool:
            ui.label("Gmail or Calendar tool not found.").classes("text-negative")
            return

        # Canonical credentials location
        from tools.gmail_tool import _GMAIL_DIR, DEFAULT_CREDENTIALS_PATH as _GMAIL_CREDS_DEFAULT
        from tools.calendar_tool import DEFAULT_TOKEN_PATH as _CAL_TOKEN_PATH

        def _google_status_text():
            _gmail_ok = gmail_tool.is_authenticated()
            _cal_ok = cal_tool.is_authenticated()
            if _gmail_ok and _cal_ok:
                try:
                    s1, _ = gmail_tool.check_token_health()
                    s2, _ = cal_tool.check_token_health()
                    if s1 in ("valid", "refreshed") and s2 in ("valid", "refreshed"):
                        return "✅ Connected"
                    return "⚠️ Token issue"
                except Exception:
                    return "✅ Connected"
            if not gmail_tool.has_credentials_file():
                return "⚠️ Not configured"
            return "🔑 Not authenticated"

        with ui.expansion(
            f"Google (Gmail & Calendar) — {_google_status_text()}",
            icon="account_circle",
        ).classes("w-full") as google_panel:

            # ── Enable switches ──
            with ui.row().classes("gap-8 items-center"):
                ui.switch(
                    "Gmail",
                    value=tool_registry.is_enabled("gmail"),
                    on_change=lambda e: tool_registry.set_enabled("gmail", e.value),
                ).tooltip(gmail_tool.description)
                ui.switch(
                    "Calendar",
                    value=tool_registry.is_enabled("calendar"),
                    on_change=lambda e: tool_registry.set_enabled("calendar", e.value),
                ).tooltip(cal_tool.description)

            ui.separator()

            # ── Setup wizard (stepper) ──
            with ui.expansion("Setup Guide — first-time setup", icon="help_outline").classes("w-full"):
                with ui.stepper().props("vertical").classes("w-full") as stepper:
                    with ui.step("Create Google Cloud Project"):
                        ui.markdown(
                            "1. Open [Google Cloud Console](https://console.cloud.google.com)\n"
                            "2. Click the project dropdown (top bar) → **New Project**\n"
                            "3. Name it anything (e.g. *Thoth*) → **Create**\n"
                            "4. Make sure the new project is selected in the dropdown",
                        )
                        with ui.stepper_navigation():
                            ui.button("Next", on_click=stepper.next)
                    with ui.step("Enable APIs"):
                        ui.markdown(
                            "1. Go to **APIs & Services → Library**\n"
                            "2. Search for **Gmail API** → click it → **Enable**\n"
                            "3. Search for **Google Calendar API** → click it → **Enable**",
                        )
                        with ui.stepper_navigation():
                            ui.button("Next", on_click=stepper.next)
                            ui.button("Back", on_click=stepper.previous).props("flat")
                    with ui.step("Configure OAuth Consent"):
                        ui.markdown(
                            "1. Go to **APIs & Services → OAuth consent screen**\n"
                            '2. Select **External** → **Create**\n'
                            "3. Fill in App name (e.g. *Thoth*), your email → **Save and Continue**\n"
                            "4. On **Scopes** page → just click **Save and Continue**\n"
                            "5. On **Test users** → **Add Users** → add your Gmail address → **Save**",
                        )
                        with ui.stepper_navigation():
                            ui.button("Next", on_click=stepper.next)
                            ui.button("Back", on_click=stepper.previous).props("flat")
                    with ui.step("Create OAuth Client ID"):
                        ui.markdown(
                            "1. Go to **APIs & Services → Credentials**\n"
                            "2. Click **+ Create Credentials → OAuth client ID**\n"
                            "3. Application type → **Desktop app**\n"
                            "4. Name it anything → **Create**\n"
                            "5. Click **Download JSON** (saves as `client_secret_...json`)",
                        )
                        with ui.stepper_navigation():
                            ui.button("Next", on_click=stepper.next)
                            ui.button("Back", on_click=stepper.previous).props("flat")
                    with ui.step("Select Credentials & Authenticate"):
                        ui.markdown(
                            "Use the **Browse** button below to select the downloaded JSON file, "
                            "then click **Authenticate Google**. A browser window will open for sign-in.",
                        )
                        with ui.stepper_navigation():
                            ui.button("Back", on_click=stepper.previous).props("flat")

            ui.separator()

            # ── Credentials path + browse + auto-copy ──
            creds_default = gmail_tool.config_schema.get("credentials_path", {}).get("default", "")
            current_creds = gmail_tool.get_config("credentials_path", creds_default)
            creds_input = ui.input(
                "credentials.json path", value=current_creds or "",
            ).classes("w-full").props("readonly")

            async def _browse_and_copy():
                path = await browse_file(
                    "Select credentials.json (or client_secret_*.json)",
                    os.path.dirname(current_creds) if current_creds else "",
                    [("JSON files", "*.json")],
                )
                if not path:
                    return
                src = pathlib.Path(path)
                dest = _GMAIL_DIR / "credentials.json"
                # Auto-copy to canonical location if not already there
                if src.resolve() != dest.resolve():
                    try:
                        shutil.copy2(str(src), str(dest))
                        ui.notify(f"Copied to {dest}", type="info")
                    except Exception as exc:
                        ui.notify(f"Copy failed: {exc}", type="negative")
                        # Fall back to using the original path
                        creds_input.value = path
                        gmail_tool.set_config("credentials_path", path)
                        cal_tool.set_config("credentials_path", path)
                        return
                canonical = str(dest)
                creds_input.value = canonical
                gmail_tool.set_config("credentials_path", canonical)
                cal_tool.set_config("credentials_path", canonical)
                ui.notify("Credentials ready — click Authenticate Google", type="positive")

            ui.button("Browse…", on_click=_browse_and_copy, icon="folder_open").props("flat dense")

            ui.separator()

            # ── Combined auth status ──
            _has_creds = gmail_tool.has_credentials_file()
            _gmail_authed = gmail_tool.is_authenticated()
            _cal_authed = cal_tool.is_authenticated()
            _both_authed = _gmail_authed and _cal_authed

            def _show_token_status(label: str, tool, authed: bool):
                if not authed:
                    ui.label(f"⬜ {label} — not authenticated").classes("text-grey-6 text-sm")
                    return
                try:
                    status, detail = tool.check_token_health()
                except Exception:
                    status, detail = "valid", ""
                if status in ("valid", "refreshed"):
                    ui.label(f"✅ {label} — token healthy").classes("text-positive text-sm")
                elif status == "expired":
                    ui.label(f"⚠️ {label} — token expired").classes("text-warning text-sm")
                elif status == "error":
                    ui.label(f"⚠️ {label} — {detail}").classes("text-warning text-sm")
                else:
                    ui.label(f"✅ {label} — connected").classes("text-positive text-sm")

            with ui.column().classes("gap-1"):
                _show_token_status("Gmail", gmail_tool, _gmail_authed)
                _show_token_status("Calendar", cal_tool, _cal_authed)

            # ── Combined authenticate / re-authenticate ──
            def _do_combined_auth():
                """Single OAuth flow with both Gmail + Calendar scopes."""
                from google_auth_oauthlib.flow import InstalledAppFlow
                from tools.gmail_tool import GMAIL_SCOPES, DEFAULT_TOKEN_PATH as _GMAIL_TOKEN
                from tools.calendar_tool import CALENDAR_SCOPES

                creds_path = gmail_tool._get_credentials_path()
                combined_scopes = GMAIL_SCOPES + CALENDAR_SCOPES

                flow = InstalledAppFlow.from_client_secrets_file(creds_path, combined_scopes)
                creds = flow.run_local_server(port=0)

                # Write token to both locations
                pathlib.Path(_GMAIL_TOKEN).parent.mkdir(parents=True, exist_ok=True)
                pathlib.Path(_GMAIL_TOKEN).write_text(creds.to_json())
                pathlib.Path(_CAL_TOKEN_PATH).parent.mkdir(parents=True, exist_ok=True)
                pathlib.Path(_CAL_TOKEN_PATH).write_text(creds.to_json())

            if _has_creds:
                if _both_authed:
                    async def _reauth_google():
                        try:
                            # Remove both tokens
                            for tp in (gmail_tool._get_token_path(), cal_tool._get_token_path()):
                                if os.path.isfile(tp):
                                    os.remove(tp)
                            await run.io_bound(_do_combined_auth)
                            clear_agent_cache()
                            ui.notify("✅ Google account re-authenticated!", type="positive")
                            _reopen("Accounts")
                        except Exception as e:
                            ui.notify(f"Auth failed: {e}", type="negative")

                    ui.button("Re-authenticate Google", on_click=_reauth_google, icon="refresh").props("flat dense")
                else:
                    async def _auth_google():
                        try:
                            await run.io_bound(_do_combined_auth)
                            clear_agent_cache()
                            ui.notify("✅ Google account authenticated!", type="positive")
                            _reopen("Accounts")
                        except Exception as e:
                            ui.notify(f"Auth failed: {e}", type="negative")

                    ui.button("Authenticate Google", on_click=_auth_google, icon="login").props("outlined")
            else:
                ui.label(
                    "Select your credentials file above to get started."
                ).classes("text-grey-6 text-sm")

            # ── Gmail operation checkboxes ──
            ui.separator()
            ui.label("Gmail Operations").classes("text-subtitle2")
            from tools.gmail_tool import _READ_OPS, _COMPOSE_OPS, _SEND_OPS
            ops_default = gmail_tool.config_schema.get("selected_operations", {}).get("default", [])
            current_ops = gmail_tool.get_config("selected_operations", ops_default)
            if not isinstance(current_ops, list):
                current_ops = ops_default
            _build_ops_checkboxes(
                [("Read", _READ_OPS), ("Compose", _COMPOSE_OPS), ("⚠️ Send", _SEND_OPS)],
                current_ops, gmail_tool,
            )

            # ── Calendar operation checkboxes ──
            ui.separator()
            ui.label("Calendar Operations").classes("text-subtitle2")
            from tools.calendar_tool import (
                _READ_OPS as CAL_READ_OPS,
                _WRITE_OPS as CAL_WRITE_OPS,
                _DESTRUCTIVE_OPS as CAL_DESTRUCTIVE_OPS,
            )
            cal_ops_default = cal_tool.config_schema.get("selected_operations", {}).get("default", [])
            current_cal_ops = cal_tool.get_config("selected_operations", cal_ops_default)
            if not isinstance(current_cal_ops, list):
                current_cal_ops = cal_ops_default
            _build_ops_checkboxes(
                [("Read", CAL_READ_OPS), ("Write", CAL_WRITE_OPS), ("⚠️ Destructive", CAL_DESTRUCTIVE_OPS)],
                current_cal_ops, cal_tool,
            )

    # ── Accounts Tab ─────────────────────────────────────────────────

    def _build_accounts_tab() -> None:
        ui.label("👥 Accounts").classes("text-h6")
        ui.label(
            "Connect Google, social media, and other personal accounts."
        ).classes("text-grey-6 text-sm")

        ui.separator()

        _build_google_account_panel()
        _build_x_account_panel()

    def _build_x_account_panel() -> None:
        """Render the X (Twitter) account settings panel."""
        from tools.x_tool import (
            XTool, _READ_OPS as X_READ_OPS, _POST_OPS as X_POST_OPS,
            _ENGAGE_OPS as X_ENGAGE_OPS,
        )

        x_tool = tool_registry.get_tool("x")
        if not x_tool:
            ui.label("X tool not found.").classes("text-negative")
            return

        def _x_status_text():
            if not x_tool.has_credentials():
                return "⚠️ Not configured"
            if not x_tool.is_authenticated():
                return "🔑 Not authenticated"
            status, _ = x_tool.check_token_health()
            if status in ("valid", "refreshed"):
                return "✅ Connected"
            if status == "expired":
                return "⚠️ Token expired"
            return "⚠️ Check status"

        with ui.expansion(
            f"𝕏 X (Twitter) — {_x_status_text()}",
            icon="tag",
        ).classes("w-full") as panel:

            # ── Enable switch ────────────────────────────────────────
            ui.switch(
                "Enable X tool",
                value=tool_registry.is_enabled("x"),
                on_change=lambda e: (
                    tool_registry.set_enabled("x", e.value),
                    clear_agent_cache(),
                ),
            ).tooltip(x_tool.description)

            # ── Setup Guide (collapsible) ────────────────────────────
            with ui.expansion("📖 Setup Guide", icon="help_outline").classes("w-full mt-2"):
                with ui.stepper().props("vertical").classes("w-full") as stepper:
                    with ui.step("Create X Developer Account"):
                        ui.markdown(
                            "1. Go to [developer.x.com](https://developer.x.com)\n"
                            "2. Sign in with your X account\n"
                            "3. Apply for a developer account if you haven't already\n"
                            "4. Go to the **Developer Portal Dashboard**",
                        )
                        with ui.stepper_navigation():
                            ui.button("Next", on_click=stepper.next)
                    with ui.step("Create a Project & App"):
                        ui.markdown(
                            "1. In the Developer Portal, click **+ Create Project**\n"
                            "2. Name it (e.g. *Thoth*) → select a use case → **Next**\n"
                            "3. An App will be created automatically\n"
                            "4. Go to your App's **Settings** tab",
                        )
                        with ui.stepper_navigation():
                            ui.button("Next", on_click=stepper.next)
                            ui.button("Back", on_click=stepper.previous).props("flat")
                    with ui.step("Configure OAuth 2.0"):
                        ui.markdown(
                            "1. Under **User authentication settings**, click **Set up**\n"
                            "2. Enable **OAuth 2.0**\n"
                            "3. App type: **Web App** (or Native App)\n"
                            "4. Callback URL: **`http://127.0.0.1:17638/callback`**\n"
                            "   *(this must match exactly — including the port)*\n"
                            "5. Website URL: any URL (e.g. `https://example.com`)\n"
                            "6. Click **Save**",
                        )
                        with ui.stepper_navigation():
                            ui.button("Next", on_click=stepper.next)
                            ui.button("Back", on_click=stepper.previous).props("flat")
                    with ui.step("Copy Client ID & Secret"):
                        ui.markdown(
                            "1. Go to your App's **Keys and tokens** tab\n"
                            "2. Under **OAuth 2.0 Client ID and Client Secret**:\n"
                            "   - Copy the **Client ID** and paste below\n"
                            "   - Copy the **Client Secret** and paste below\n"
                            "3. Click **Save** below, then **Authenticate**",
                        )
                        with ui.stepper_navigation():
                            ui.button("Back", on_click=stepper.previous).props("flat")

            ui.separator()

            # ── Client ID / Secret fields ────────────────────────────
            id_input, id_refresh = _secret_input("Client ID", "X_CLIENT_ID")
            secret_input, secret_refresh = _secret_input("Client Secret", "X_CLIENT_SECRET")

            # ── Auth status ──────────────────────────────────────────
            status_container = ui.column().classes("gap-1 mt-2")

            def _update_auth_status():
                status_container.clear()
                with status_container:
                    if not x_tool.has_credentials():
                        ui.label("⬜ Not configured — enter Client ID and Secret above").classes(
                            "text-grey-6 text-sm"
                        )
                        return
                    if not x_tool.is_authenticated():
                        ui.label("🔑 Credentials saved — click Authenticate below").classes(
                            "text-info text-sm"
                        )
                        return
                    status, detail = x_tool.check_token_health()
                    if status in ("valid", "refreshed"):
                        username = x_tool.get_authenticated_username()
                        if username:
                            ui.label(f"✅ Authenticated as @{username}").classes(
                                "text-positive text-sm"
                            )
                        else:
                            ui.label("✅ Token healthy").classes("text-positive text-sm")
                    elif status == "expired":
                        ui.label(f"⚠️ Token expired — {detail}").classes(
                            "text-warning text-sm"
                        )
                    else:
                        ui.label(f"⚠️ {detail}").classes("text-warning text-sm")

            _update_auth_status()

            # ── Save / Auth / Re-auth buttons ────────────────────────
            def _save_x_credentials():
                cid = (id_input.value or "").strip()
                csecret = (secret_input.value or "").strip()
                has_id = key_status("X_CLIENT_ID").get("configured")
                has_secret = key_status("X_CLIENT_SECRET").get("configured")
                if not cid and not has_id:
                    ui.notify("Please enter your Client ID", type="warning")
                    return
                if not csecret and not has_secret:
                    ui.notify("Please enter your Client Secret", type="warning")
                    return
                if cid:
                    set_key("X_CLIENT_ID", cid)
                    id_input.value = ""
                    id_input.update()
                    id_refresh()
                if csecret:
                    set_key("X_CLIENT_SECRET", csecret)
                    secret_input.value = ""
                    secret_input.update()
                    secret_refresh()
                clear_agent_cache()
                _update_auth_status()
                _update_buttons()
                _refresh_x_header()
                ui.notify("X credentials saved", type="positive")

            async def _do_x_auth():
                if not x_tool.has_credentials():
                    ui.notify("Please save your Client ID and Secret first", type="warning")
                    return
                try:
                    ui.notify("Opening browser for X authentication…", type="info")
                    await run.io_bound(x_tool.authenticate)
                    clear_agent_cache()
                    _update_auth_status()
                    _update_buttons()
                    _refresh_x_header()
                    ui.notify("✅ X authentication successful!", type="positive")
                except Exception as exc:
                    logger.error("X authentication failed: %s", exc, exc_info=True)
                    ui.notify(f"X authentication failed: {exc}", type="negative")

            async def _do_x_reauth():
                # Remove existing token
                from tools.x_tool import _TOKEN_PATH
                if _TOKEN_PATH.is_file():
                    _TOKEN_PATH.unlink()
                await _do_x_auth()

            buttons_container = ui.row().classes("gap-2 items-center mt-2")

            def _update_buttons():
                buttons_container.clear()
                with buttons_container:
                    ui.button("💾 Save", on_click=_save_x_credentials)
                    if x_tool.has_credentials():
                        if x_tool.is_authenticated():
                            ui.button("🔄 Re-authenticate", on_click=_do_x_reauth).props("flat")
                        else:
                            ui.button("🔑 Authenticate", on_click=_do_x_auth).props("color=positive")

            _update_buttons()

            # ── Operations checkboxes ────────────────────────────────
            ui.separator()

            ui.label("X Operations").classes("text-subtitle2")
            ui.label("Allowed operations").classes("text-sm font-bold mt-2")

            def _make_toggle(selected, cfg_key):
                def _toggle(op, val):
                    if val and op not in selected:
                        selected.append(op)
                    elif not val and op in selected:
                        selected.remove(op)
                    x_tool.set_config(cfg_key, list(selected))
                return _toggle

            with ui.row().classes("w-full gap-8"):
                # Read column
                read_default = x_tool.config_schema.get("read_operations", {}).get("default", [])
                current_read = list(x_tool.get_config("read_operations", read_default))
                if not isinstance(current_read, list):
                    current_read = list(read_default)
                toggle_read = _make_toggle(current_read, "read_operations")
                with ui.column():
                    ui.label("📖 Read").classes("font-bold text-sm")
                    for op in X_READ_OPS:
                        ui.checkbox(op, value=op in current_read,
                                    on_change=lambda e, o=op: toggle_read(o, e.value))

                # Post column
                post_default = x_tool.config_schema.get("post_operations", {}).get("default", [])
                current_post = list(x_tool.get_config("post_operations", post_default))
                if not isinstance(current_post, list):
                    current_post = list(post_default)
                toggle_post = _make_toggle(current_post, "post_operations")
                with ui.column():
                    ui.label("⚠️ Post (requires approval)").classes("font-bold text-sm")
                    for op in X_POST_OPS:
                        ui.checkbox(op, value=op in current_post,
                                    on_change=lambda e, o=op: toggle_post(o, e.value))

                # Engage column
                engage_default = x_tool.config_schema.get("engage_operations", {}).get("default", [])
                current_engage = list(x_tool.get_config("engage_operations", engage_default))
                if not isinstance(current_engage, list):
                    current_engage = list(engage_default)
                toggle_engage = _make_toggle(current_engage, "engage_operations")
                with ui.column():
                    ui.label("👍 Engage").classes("font-bold text-sm")
                    for op in X_ENGAGE_OPS:
                        ui.checkbox(op, value=op in current_engage,
                                    on_change=lambda e, o=op: toggle_engage(o, e.value))

            def _refresh_x_header():
                panel._props["label"] = f"𝕏 X (Twitter) — {_x_status_text()}"
                panel.update()

    # ── Utilities Tab ────────────────────────────────────────────────

    def _build_utilities_tab() -> None:
        ui.label("🔧 Utility Tools").classes("text-h6")
        ui.label("Lightweight productivity tools.").classes("text-grey-6 text-sm")
        ui.separator()
        util_names = ["task", "timer", "url_reader", "calculator", "weather", "chart", "system_info", "conversation_search"]
        for uname in util_names:
            utool = tool_registry.get_tool(uname)
            if utool is None:
                continue
            ui.switch(
                utool.display_name,
                value=tool_registry.is_enabled(uname),
                on_change=lambda e, n=uname: tool_registry.set_enabled(n, e.value),
            ).tooltip(utool.description)
            ui.separator()

    # ── Tracker Tab ──────────────────────────────────────────────────

    def _build_tracker_tab() -> None:
        from tools.tracker_tool import _get_db, _get_all_trackers, _DB_PATH

        ui.label("\U0001f4cb Habit & Health Tracker").classes("text-h6")
        ui.label("Track recurring activities, habits, symptoms, and health events.").classes("text-grey-6 text-sm")

        tracker_tool = tool_registry.get_tool("tracker")
        if not tracker_tool:
            ui.label("Tracker tool not found.").classes("text-negative")
            return

        ui.switch(
            "Enable Habit Tracker",
            value=tool_registry.is_enabled("tracker"),
            on_change=lambda e: tool_registry.set_enabled("tracker", e.value),
        ).tooltip(tracker_tool.description)

        ui.separator()

        try:
            conn = _get_db()
            trackers = _get_all_trackers(conn)
            total_entries = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
            conn.close()
        except Exception:
            trackers = []
            total_entries = 0

        ui.label(f"Active trackers: {len(trackers)}  ·  Total entries: {total_entries}").classes("font-bold")

        if trackers:
            tracker_container = ui.column().classes("w-full")

            def _refresh_trackers():
                tracker_container.clear()
                try:
                    c = _get_db()
                    tlist = _get_all_trackers(c)
                    with tracker_container:
                        if not tlist:
                            ui.label("No trackers yet.").classes("text-grey-6")
                        else:
                            for t in tlist:
                                entry_count = c.execute(
                                    "SELECT COUNT(*) FROM entries WHERE tracker_id = ?",
                                    (t["id"],),
                                ).fetchone()[0]
                                last_entry = c.execute(
                                    "SELECT timestamp FROM entries WHERE tracker_id = ? ORDER BY timestamp DESC LIMIT 1",
                                    (t["id"],),
                                ).fetchone()
                                last_str = last_entry[0][:10] if last_entry else "never"
                                type_badge = t["type"]
                                if t.get("unit"):
                                    type_badge += f" ({t['unit']})"
                                with ui.row().classes("w-full items-center gap-2"):
                                    ui.label(f"● {t['name']}").classes("font-bold")
                                    ui.badge(type_badge).props("outline")
                                    ui.label(f"{entry_count} entries · last: {last_str}").classes("text-xs text-grey-6")
                                ui.separator()
                    c.close()
                except Exception as exc:
                    with tracker_container:
                        ui.label(f"Error loading trackers: {exc}").classes("text-negative")

            _refresh_trackers()

            ui.separator()

            async def _delete_all_tracker_data():
                confirm = await ui.run_javascript(
                    "confirm('Delete ALL tracker data? This cannot be undone.')",
                    timeout=30,
                )
                if confirm:
                    try:
                        c = _get_db()
                        c.execute("DELETE FROM entries")
                        c.execute("DELETE FROM trackers")
                        c.commit()
                        c.close()
                        ui.notify("All tracker data deleted.", type="info")
                        _refresh_trackers()
                    except Exception as exc:
                        ui.notify(f"Error: {exc}", type="negative")

            ui.button("🗑️ Delete All Tracker Data", on_click=_delete_all_tracker_data).props("flat dense color=negative")
        else:
            ui.label("No trackers yet.").classes("text-grey-6 mt-2")

    # ── Knowledge Tab ─────────────────────────────────────────────────

    def _build_knowledge_tab() -> None:
        import knowledge_graph as kg
        import memory as memory_db
        import wiki_vault
        from documents import reset_vector_store

        ui.label("🧠 Knowledge").classes("text-h6")
        ui.label(
            "Thoth builds a knowledge graph from your conversations and documents."
        ).classes("text-grey-6 text-sm")

        mem_tool = tool_registry.get_tool("memory")
        if mem_tool:
            ui.switch(
                "Enable Memory",
                value=tool_registry.is_enabled("memory"),
                on_change=lambda e: tool_registry.set_enabled("memory", e.value),
            )

        ui.separator()

        total = memory_db.count_memories()
        rel_count = kg.count_relations()

        with ui.row().classes("gap-6"):
            ui.label(f"Entities: {total}").classes("font-bold")
            ui.label(f"Relations: {rel_count}").classes("font-bold")

        if total > 0:
            try:
                stats = kg.get_graph_stats()
                type_parts = [f"{t}: {c}" for t, c in sorted(stats.get("entity_types", {}).items())]
                if type_parts:
                    ui.label(f"Types — {', '.join(type_parts)}").classes("text-xs text-grey-6")
                if stats.get("connected_components", 0) > 0:
                    ui.label(
                        f"Knowledge graph — {stats['connected_components']} component(s), "
                        f"largest: {stats['largest_component']} entities, "
                        f"{stats['isolated_entities']} isolated"
                    ).classes("text-xs text-grey-6")
            except Exception:
                pass

        # ── Wiki Vault section ───────────────────────────────────────
        ui.separator()
        ui.label("📚 Wiki Vault").classes("text-subtitle1 font-bold")
        ui.label(
            "Export your knowledge graph as Obsidian-compatible markdown files. "
            "Open the vault in Obsidian, VS Code, or any markdown editor."
        ).classes("text-grey-6 text-sm")

        cfg = wiki_vault._load_config()
        vault_enabled = cfg.get("enabled", False)
        vault_path = cfg.get("vault_path", str(wiki_vault._DATA_DIR / "vault"))

        def _toggle_vault(e):
            wiki_vault.set_enabled(e.value)
            tool_registry.set_enabled("wiki", e.value)
            if e.value:
                ui.notify("Wiki vault enabled — rebuilding…", type="info")
                try:
                    vstats = wiki_vault.rebuild_vault()
                    ui.notify(
                        f"✅ Vault rebuilt: {vstats['exported']} articles",
                        type="positive",
                    )
                except Exception as exc:
                    ui.notify(f"Rebuild failed: {exc}", type="negative")
            else:
                ui.notify("Wiki vault disabled.", type="info")

        ui.switch("Enable Wiki Vault", value=vault_enabled, on_change=_toggle_vault)

        ui.label("Vault Path").classes("font-bold")
        with ui.row().classes("w-full items-center gap-2"):
            path_input = ui.input(value=vault_path).classes("flex-grow")

            async def _browse_vault():
                folder = await browse_folder("Select vault folder")
                if folder:
                    path_input.value = folder

            ui.button("Browse", on_click=_browse_vault).props("flat dense")

            def _apply_path():
                new_path = path_input.value.strip()
                if new_path:
                    wiki_vault.set_vault_path(new_path)
                    ui.notify(f"Vault path set to: {new_path}", type="info")

            ui.button("Apply", on_click=_apply_path).props("flat dense color=primary")

        if vault_enabled:
            vstats = wiki_vault.get_vault_stats()
            with ui.row().classes("gap-6"):
                ui.label(f"Articles: {vstats.get('articles', 0)}").classes("font-bold")
                conv_count = vstats.get('conversations', 0)
                if conv_count > 0:
                    ui.label(f"Conversations: {conv_count}").classes("font-bold")

            # ── Vault sync detection ──────────────────────────────
            edited = wiki_vault.check_vault_sync()
            if edited:
                with ui.card().classes("w-full bg-amber-1 border-l-4").style("border-color: #ff9800"):
                    with ui.row().classes("items-center gap-2"):
                        ui.icon("sync_problem", color="amber-8").classes("text-lg")
                        ui.label(
                            f"{len(edited)} file{'s' if len(edited) != 1 else ''} edited in vault"
                        ).classes("font-bold text-amber-10")
                    ui.label(
                        "These files were modified outside Thoth. "
                        "Sync to import changes into the knowledge graph."
                    ).classes("text-xs text-grey-7")

                    def _sync_vault():
                        try:
                            result = wiki_vault.sync_all_from_vault()
                            ui.notify(
                                f"✅ Synced {result['synced']} file(s) from vault",
                                type="positive",
                            )
                            _reopen("Knowledge")
                        except Exception as exc:
                            ui.notify(f"Sync failed: {exc}", type="negative")

                    ui.button("🔄 Sync from Vault", on_click=_sync_vault).props(
                        "flat color=amber-8"
                    )

            with ui.row().classes("gap-2"):
                def _rebuild():
                    try:
                        result = wiki_vault.rebuild_vault()
                        ui.notify(
                            f"✅ Rebuilt: {result['exported']} articles, "
                            f"{result['sparse']} sparse, "
                            f"{result.get('orphans_removed', 0)} orphans removed",
                            type="positive",
                        )
                        _reopen("Knowledge")
                    except Exception as exc:
                        ui.notify(f"Failed: {exc}", type="negative")

                ui.button("🔄 Rebuild Vault", on_click=_rebuild).props("flat")

                def _open_vault():
                    import platform
                    import subprocess as sp
                    vp = wiki_vault.get_vault_path()
                    if not vp.exists():
                        ui.notify("Vault folder not found.", type="warning")
                        return
                    system = platform.system()
                    try:
                        if system == "Windows":
                            os.startfile(str(vp))
                        elif system == "Darwin":
                            sp.Popen(["open", str(vp)])
                        else:
                            sp.Popen(["xdg-open", str(vp)])
                    except Exception as exc:
                        ui.notify(f"Failed to open: {exc}", type="negative")

                ui.button("📂 Open Vault Folder", on_click=_open_vault).props("flat")

        # ── Dream Cycle section ───────────────────────────────────────
        ui.separator()
        import dream_cycle

        ui.label("🌙 Dream Cycle").classes("text-subtitle1 font-bold")
        ui.label(
            "Nightly background task that merges duplicates, enriches "
            "thin descriptions, and infers missing relationships."
        ).classes("text-grey-6 text-sm")

        dream_cfg = dream_cycle.get_config()

        def _toggle_dream(e):
            dream_cycle.set_enabled(e.value)
            ui.notify(
                "Dream cycle enabled." if e.value else "Dream cycle disabled.",
                type="info",
            )

        ui.switch(
            "Enable Dream Cycle",
            value=dream_cfg.get("enabled", True),
            on_change=_toggle_dream,
        )

        with ui.row().classes("gap-4 items-center"):
            ui.label("Window").classes("text-sm")

            _start_val = f"{dream_cfg.get('window_start', 1):02d}:00"
            with ui.input("Start", value=_start_val).props(
                "dense outlined"
            ).classes("w-28") as _dream_start_input:
                with ui.menu().props("no-parent-event") as _start_menu:
                    ui.time(value=_start_val, mask="HH:00").props(
                        'format24h'
                    ).bind_value(_dream_start_input)
                with _dream_start_input.add_slot("append"):
                    ui.icon("schedule").on("click", _start_menu.open).classes(
                        "cursor-pointer"
                    )

            ui.label("–").classes("text-sm")

            _end_val = f"{dream_cfg.get('window_end', 5):02d}:00"
            with ui.input("End", value=_end_val).props(
                "dense outlined"
            ).classes("w-28") as _dream_end_input:
                with ui.menu().props("no-parent-event") as _end_menu:
                    ui.time(value=_end_val, mask="HH:00").props(
                        'format24h'
                    ).bind_value(_dream_end_input)
                with _dream_end_input.add_slot("append"):
                    ui.icon("schedule").on("click", _end_menu.open).classes(
                        "cursor-pointer"
                    )

            def _on_dream_window_change(_=None):
                try:
                    s = int(_dream_start_input.value.split(":")[0])
                    e = int(_dream_end_input.value.split(":")[0])
                except (ValueError, AttributeError):
                    return
                dream_cycle.set_window(s, e)
                ui.notify(f"Dream window updated: {s:02d}:00 – {e:02d}:00", type="info")

            _dream_start_input.on("update:model-value", _on_dream_window_change)
            _dream_end_input.on("update:model-value", _on_dream_window_change)

        dream_status = dream_cycle.get_dream_status()
        if dream_status.get("last_run"):
            try:
                last_dt = datetime.fromisoformat(dream_status["last_run"])
                ui.label(
                    f"Last run: {last_dt.strftime('%b %d, %I:%M %p')} — "
                    f"{dream_status.get('last_summary', '')}"
                ).classes("text-xs text-grey-6")
            except (ValueError, TypeError):
                pass
        else:
            ui.label("No dream cycles have run yet.").classes("text-xs text-grey-6")

        # ── Browse knowledge ─────────────────────────────────────────
        ui.separator()

        if total > 0:
            from ui.bulk_select import BulkSelect, render_bulk_action_bar
            from ui.confirm import confirm_destructive

            _bulk_mem = BulkSelect()

            cat_options = ["All"] + sorted(memory_db.VALID_CATEGORIES)
            cat_sel = ui.select(label="Filter by category", options=cat_options, value="All").classes("w-full")
            search_input = ui.input("Search knowledge", placeholder="Type a keyword…").classes("w-full")

            with ui.row().classes("w-full items-center justify-end q-mt-xs"):
                _mem_select_btn = ui.button("Select").props(
                    "flat dense no-caps size=sm"
                )

                def _toggle_mem_select():
                    _bulk_mem.toggle_mode()
                    _mem_select_btn.text = (
                        "Done" if _bulk_mem.active else "Select"
                    )
                    _refresh_memories()

                _mem_select_btn.on("click", _toggle_mem_select)

            mem_container = ui.column().classes("w-full")

            def _refresh_memories():
                mem_container.clear()
                cat = None if cat_sel.value == "All" else cat_sel.value
                q = search_input.value
                if q:
                    memories = memory_db.search_memories(q, category=cat)
                else:
                    memories = memory_db.list_memories(category=cat)
                with mem_container:
                    if not memories:
                        ui.label("No matching entries.").classes("text-grey-6")
                    else:
                        for mem in memories:
                            _mem_id = mem["id"]
                            _header_label = (
                                f"**{mem['subject']}** — "
                                f"_{mem.get('category', mem.get('entity_type', ''))}_"
                            )
                            if _bulk_mem.active:
                                with ui.row().classes("w-full items-center no-wrap").style(
                                    "gap: 4px;"
                                ):
                                    _cb = ui.checkbox(
                                        value=_bulk_mem.is_selected(_mem_id),
                                    )
                                    _cb.on(
                                        "update:model-value",
                                        lambda e, i=_mem_id: _bulk_mem.toggle_item(
                                            i, bool(e.args),
                                        ),
                                    )
                                    _entry_container = ui.expansion(
                                        _header_label,
                                    ).classes("col-grow")
                            else:
                                _entry_container = ui.expansion(_header_label).classes("w-full")
                            with _entry_container:
                                content = mem.get("content", mem.get("description", ""))
                                ui.markdown(content, extras=['code-friendly', 'fenced-code-blocks', 'tables'])
                                aliases = mem.get("aliases", "")
                                if aliases:
                                    ui.label(f"Aliases: {aliases}").classes("text-xs text-grey-6")
                                tags = mem.get("tags", "")
                                if tags:
                                    ui.label(f"Tags: {tags}").classes("text-xs text-grey-6")
                                try:
                                    rels = kg.get_relations(mem["id"])
                                    if rels:
                                        rel_lines = []
                                        for r in rels[:5]:
                                            arrow = "→" if r["direction"] == "outgoing" else "←"
                                            rel_lines.append(f"{arrow} {r['relation_type']}: {r['peer_subject']}")
                                        rel_text = " · ".join(rel_lines)
                                        if len(rels) > 5:
                                            rel_text += f" … +{len(rels) - 5} more"
                                        ui.label(f"🔗 {rel_text}").classes("text-xs text-blue-4")
                                except Exception:
                                    pass
                                ui.label(
                                    f"ID: {mem['id']} · Created: {mem['created_at'][:16]} · Updated: {mem['updated_at'][:16]}"
                                ).classes("text-xs text-grey-6")

                                def _del_mem(mid=mem["id"]):
                                    memory_db.delete_memory(mid)
                                    ui.notify("Entry deleted.", type="info")
                                    _refresh_memories()

                                ui.button("🗑️ Delete", on_click=_del_mem).props("flat dense color=negative")

                                def _edit_mem(mid=mem["id"]):
                                    from ui.entity_editor import open_entity_editor
                                    open_entity_editor(mid, on_saved=_refresh_memories)

                                ui.button("✏️ Edit", on_click=_edit_mem).props("flat dense")

            def _do_mem_bulk_delete(ids: list[str]) -> None:
                def _commit():
                    deleted, failures = memory_db.delete_memories(ids)
                    msg = f"🗑️ Deleted {deleted} entr{'ies' if deleted != 1 else 'y'}."
                    if failures:
                        msg += f" {len(failures)} failed."
                    ui.notify(msg, type="negative" if failures else "info")
                    _refresh_memories()

                noun = "entry" if len(ids) == 1 else "entries"
                confirm_destructive(
                    f"Delete {len(ids)} {noun}?",
                    body="This cannot be undone.",
                    on_confirm=_commit,
                )

            render_bulk_action_bar(
                _bulk_mem,
                on_delete=_do_mem_bulk_delete,
                label_singular="entry",
                label_plural="entries",
                on_clear=_refresh_memories,
            )

            cat_sel.on("update:model-value", lambda _: _refresh_memories())
            search_input.on("update:model-value", lambda _: _refresh_memories())
            _refresh_memories()

        # ── Danger zone ──────────────────────────────────────────────
        ui.separator()

        _deleting_knowledge = False

        async def _delete_all_knowledge():
            nonlocal _deleting_knowledge
            if _deleting_knowledge:
                return
            _deleting_knowledge = True
            try:
                confirm = await ui.run_javascript(
                    "confirm('Delete ALL knowledge? This will erase all entities, relations, wiki files, and document indexes. This cannot be undone.')",
                    timeout=30,
                )
                if confirm:
                    memory_db.delete_all_memories()
                    reset_vector_store()
                    wiki_vault.clear_wiki_folder()
                    ui.notify("All knowledge deleted.", type="info")
                    _reopen("Knowledge")
            finally:
                _deleting_knowledge = False

        with ui.row().classes("w-full"):
            ui.button("🗑️ Delete all knowledge", on_click=_delete_all_knowledge).props("flat color=negative")

    # ── Voice Tab ────────────────────────────────────────────────────

    def _build_voice_tab() -> None:
        from voice import get_available_whisper_sizes
        from tts import VOICE_CATALOG

        ui.label("🎤 Voice Input").classes("text-h6")
        ui.label("Talk to Thoth hands-free using voice input.").classes("text-grey-6 text-sm")

        voice_svc = state.voice_service

        whisper_sizes = get_available_whisper_sizes()
        whisper_labels = {
            "tiny": "Tiny (~39 MB, fastest)", "base": "Base (~74 MB, balanced)",
            "small": "Small (~244 MB, accurate)", "medium": "Medium (~769 MB, best accuracy)",
        }
        whisper_opts = {s: whisper_labels.get(s, s) for s in whisper_sizes}
        ui.select(
            label="Whisper model size", options=whisper_opts,
            value=voice_svc.whisper_size,
            on_change=lambda e: setattr(voice_svc, "whisper_size", e.value),
        ).classes("w-full")

        ui.separator()

        ui.label("🔊 Text-to-Speech").classes("text-h6")
        ui.label("Enable text-to-speech to hear Thoth read responses aloud.").classes("text-grey-6 text-sm")

        tts = state.tts_service

        if not tts.is_installed():
            async def _install_kokoro():
                ui.notify("Downloading Kokoro TTS model & voices…", type="ongoing", timeout=0)
                await run.io_bound(tts.download_model)
                ui.notify("✅ Kokoro TTS installed!", type="positive")
                _reopen("Voice")

            ui.button("⬇️ Install Kokoro TTS", on_click=_install_kokoro).classes("w-full")
        else:
            ui.switch("Enable text-to-speech", value=tts.enabled,
                      on_change=lambda e: setattr(tts, "enabled", e.value))

            voice_opts = {v: VOICE_CATALOG.get(v, v) for v in tts.get_installed_voices()}
            if voice_opts:
                ui.select(label="Voice", options=voice_opts, value=tts.voice,
                          on_change=lambda e: setattr(tts, "voice", e.value)).classes("w-full")

            ui.label("Speech speed").classes("text-sm")
            ui.slider(
                min=0.5, max=2.0, step=0.1, value=tts.speed,
                on_change=lambda e: setattr(tts, "speed", e.value),
            ).classes("w-full")

            ui.switch("Auto-speak voice responses", value=tts.auto_speak,
                      on_change=lambda e: setattr(tts, "auto_speak", e.value))

            def _test():
                tts.speak_now("Hello! I'm Thoth, your knowledgeable personal agent.")

            ui.button("🔊 Test voice", on_click=_test).props("flat")

    # ── Channels Tab ─────────────────────────────────────────────────

    def _build_channels_tab() -> None:
        from channels import registry as _ch_registry
        from channels import config as _ch_config
        from tunnel import tunnel_manager

        # ── Tunnel Settings ──────────────────────────────────────
        ui.label("🔗 Tunnel Settings").classes("text-h6")
        ui.label(
            "Securely expose local webhook ports to the internet."
        ).classes("text-grey-6 text-sm")

        with ui.card().classes("w-full q-pa-md q-mb-md"):
            # Provider selector (future: cloudflare, tailscale)
            provider_val = _ch_config.get("tunnel", "provider", "ngrok")
            provider_select = ui.select(
                label="Provider",
                options=["ngrok"],
                value=provider_val,
            ).classes("w-full").style("max-width: 300px")

            # Authtoken
            token_input, token_refresh = _secret_input("Authtoken", "NGROK_AUTHTOKEN")
            token_input.tooltip("Your ngrok authtoken from https://dashboard.ngrok.com/")

            def _save_tunnel_settings():
                _ch_config.set("tunnel", "provider", provider_select.value)
                raw = token_input.value.strip() if isinstance(token_input.value, str) else ""
                if raw:
                    set_key("NGROK_AUTHTOKEN", raw)
                    token_input.value = ""
                    token_input.update()
                    token_refresh()
                ui.notify("Tunnel settings saved", type="positive")
                _refresh_active_tunnels()

            with ui.row().classes("gap-2"):
                ui.button("💾 Save", on_click=_save_tunnel_settings)
                ui.button(
                    "Clear authtoken",
                    icon="delete",
                    on_click=lambda: _clear_secret("NGROK_AUTHTOKEN", "ngrok authtoken", token_refresh),
                ).props("flat color=negative")

            # Active tunnels display
            tunnel_container = ui.column().classes("w-full q-mt-sm")

            def _refresh_active_tunnels():
                tunnel_container.clear()
                with tunnel_container:
                    active = tunnel_manager.active_tunnels()
                    if active:
                        ui.label("Active Tunnels:").classes(
                            "text-weight-medium text-sm"
                        )
                        for port, url in active.items():
                            with ui.row().classes("items-center gap-2"):
                                ui.label(f"Port {port}").classes("text-sm")
                                ui.label("→").classes("text-grey-6 text-sm")
                                url_label = ui.label(url).classes(
                                    "text-sm text-primary"
                                )
                                ui.button(
                                    icon="content_copy",
                                    on_click=lambda u=url: (
                                        ui.run_javascript(
                                            f"navigator.clipboard.writeText('{u}')"
                                        ),
                                        ui.notify("Copied!", type="info"),
                                    ),
                                ).props("flat dense size=xs")
                    else:
                        if tunnel_manager.is_available():
                            ui.label(
                                "No active tunnels — start a channel to open one."
                            ).classes("text-grey-6 text-sm")
                        else:
                            ui.label(
                                "Not configured — paste your authtoken above."
                            ).classes("text-grey-6 text-sm")

            _refresh_active_tunnels()

            # Setup guide
            with ui.expansion("ⓘ Setup Guide").classes("w-full q-mt-sm"):
                ui.markdown(
                    "1. Sign up at [ngrok.com](https://ngrok.com/) (free tier available)\n"
                    "2. Copy your **authtoken** from the "
                    "[dashboard](https://dashboard.ngrok.com/get-started/your-authtoken)\n"
                    "3. Paste it above and click **Save**\n"
                    "4. Start a channel — a tunnel activates automatically\n\n"
                    "*The ngrok binary is downloaded automatically on first use.*",
                    extras=["code-friendly", "fenced-code-blocks"],
                ).classes("text-sm")

            # Main-app tunnel toggle
            ui.separator().classes("q-mt-sm")
            main_app_val = _ch_config.get("tunnel", "tunnel_main_app", False)
            main_app_switch = ui.switch(
                "🌐 Expose task webhook endpoint",
                value=main_app_val,
            )
            main_app_switch.tooltip(
                "Tunnel the main Thoth port so external services can "
                "trigger task webhooks via /api/webhook/{task_id}. "
                "⚠️ This also exposes the web UI via the tunnel URL."
            )

            main_app_url_container = ui.column().classes("w-full")

            async def _on_main_app_toggle(e):
                _ch_config.set("tunnel", "tunnel_main_app", e.args)
                if e.args and tunnel_manager.is_available():
                    try:
                        app_port = 8080
                        url = tunnel_manager.start_tunnel(app_port, label="main_app")
                        main_app_url_container.clear()
                        with main_app_url_container:
                            with ui.row().classes("items-center gap-2"):
                                ui.label(f"{url}/api/webhook/{{task_id}}").classes(
                                    "text-sm text-primary"
                                )
                                ui.button(
                                    icon="content_copy",
                                    on_click=lambda u=url: (
                                        ui.run_javascript(
                                            f"navigator.clipboard.writeText("
                                            f"'{u}/api/webhook/{{task_id}}')"
                                        ),
                                        ui.notify("Copied!", type="info"),
                                    ),
                                ).props("flat dense size=xs")
                        _refresh_active_tunnels()
                    except Exception as exc:
                        ui.notify(f"Tunnel error: {exc}", type="negative")
                elif not e.args:
                    try:
                        app_port = 8080
                        tunnel_manager.stop_tunnel(app_port)
                    except Exception:
                        pass
                    main_app_url_container.clear()
                    _refresh_active_tunnels()

            main_app_switch.on("update:model-value", _on_main_app_toggle)

        ui.separator()

        # ── Messaging Channels ───────────────────────────────────
        ui.label("📱 Messaging Channels").classes("text-h6")
        ui.label("Connect Thoth to external messaging platforms.").classes("text-grey-6 text-sm")

        ui.separator()

        channels = _ch_registry.all_channels()
        if not channels:
            ui.label("No channels registered.").classes("text-grey-6 text-sm")
            return

        for ch in channels:
            _build_channel_panel(ch, _ch_config)

    def _build_channel_panel(ch, _ch_config) -> None:
        """Render a single channel's settings panel, auto-generated from its
        config_fields, capabilities, and setup_guide properties."""

        def _ch_status_text():
            if ch.is_running():
                return "✅ Running"
            if ch.is_configured():
                return "⏸️ Stopped"
            return "⚠️ Not configured"

        icon = ch.icon or "chat"
        with ui.expansion(
            f"{ch.display_name} — {_ch_status_text()}",
            icon=icon,
        ).classes("w-full") as panel:

            # ── Config field inputs ──────────────────────────────────
            field_inputs: dict[str, Any] = {}
            for cf in ch.config_fields:
                if cf.storage == "env" and cf.env_key:
                    val = get_key(cf.env_key) or cf.default
                else:
                    val = _ch_config.get(ch.name, cf.key, cf.default)

                if cf.field_type == "password":
                    if cf.storage == "env" and cf.env_key:
                        inp, refresh = _secret_input(cf.label, cf.env_key)
                        ui.button(
                            "Clear",
                            icon="delete",
                            on_click=lambda ev=cf.env_key, display=cf.label, refresh_status=refresh: _clear_secret(ev, display, refresh_status),
                        ).props("flat dense color=negative")
                    else:
                        inp = ui.input(
                            label=cf.label, value=val or "",
                            password=True, password_toggle_button=True,
                        ).classes("w-full")
                elif cf.field_type == "number":
                    inp = ui.number(
                        label=cf.label, value=val or cf.default,
                    ).classes("w-full")
                elif cf.field_type == "slider":
                    inp = ui.slider(
                        min=cf.slider_min, max=cf.slider_max,
                        step=cf.slider_step, value=val or cf.default,
                    ).classes("w-full")
                else:
                    inp = ui.input(
                        label=cf.label, value=val or "",
                    ).classes("w-full")

                if cf.help_text:
                    inp.tooltip(cf.help_text)
                field_inputs[cf.key] = (cf, inp)

            # ── Status indicator ─────────────────────────────────────
            status_container = ui.row().classes("items-center gap-2 mt-2")
            _update_channel_status(status_container, ch)

            def _refresh_header():
                panel._props["label"] = f"{ch.display_name} — {_ch_status_text()}"
                panel.update()

            # ── Save credentials ─────────────────────────────────────
            def _save_creds(ch=ch, inputs=field_inputs):
                for key, (cf, inp) in inputs.items():
                    raw = inp.value
                    if isinstance(raw, str):
                        raw = raw.strip()
                    if cf.storage == "env" and cf.env_key:
                        if raw:
                            set_key(cf.env_key, str(raw))
                    else:
                        _ch_config.set(ch.name, cf.key, raw)
                _update_channel_status(status_container, ch)
                _refresh_header()
                ui.notify(f"{ch.display_name} credentials saved", type="positive")

            # ── Start / stop ─────────────────────────────────────────
            async def _start_ch(ch=ch, _panel=panel):
                if not ch.is_configured():
                    ui.notify("Please save your credentials first", type="warning")
                    return
                try:
                    ok = await ch.start()
                    if ok:
                        _ch_config.set(ch.name, "auto_start", True)
                        clear_agent_cache()
                        ui.notify(f"✅ {ch.display_name} started!", type="positive")
                    else:
                        ui.notify(f"⚠️ Could not start {ch.display_name}", type="warning")
                except Exception as exc:
                    ui.notify(f"Error starting {ch.display_name}: {exc}", type="negative")
                _update_channel_status(status_container, ch)
                _refresh_header()
                # Keep expansion open so QR code / status is visible
                _panel.open()

            async def _stop_ch(ch=ch):
                try:
                    await ch.stop()
                    _ch_config.set(ch.name, "auto_start", False)
                    clear_agent_cache()
                    ui.notify(f"{ch.display_name} stopped", type="info")
                except Exception as exc:
                    ui.notify(f"Error stopping {ch.display_name}: {exc}", type="negative")
                _update_channel_status(status_container, ch)
                _refresh_header()

            with ui.row().classes("gap-2 items-center"):
                ui.button("💾 Save", on_click=_save_creds)
                ui.button("▶️ Start", on_click=_start_ch).props("color=positive")
                ui.button("⏹️ Stop", on_click=_stop_ch).props("color=negative flat")

            # ── Tunnel toggle (webhook channels only) ────────────────
            if ch.needs_tunnel:
                tunnel_val = _ch_config.get(ch.name, "tunnel_enabled", True)
                tunnel_switch = ui.switch(
                    "🔗 Expose via tunnel",
                    value=tunnel_val,
                )
                tunnel_switch.tooltip(
                    "Automatically open a public tunnel for this channel's "
                    "webhook port when it starts."
                )

                def _on_tunnel_toggle(e, ch=ch):
                    _ch_config.set(ch.name, "tunnel_enabled", e.value)
                    ui.notify(
                        f"Tunnel {'enabled' if e.value else 'disabled'} "
                        f"for {ch.display_name}",
                        type="info",
                    )

                tunnel_switch.on("update:model-value", _on_tunnel_toggle)

                # Show live tunnel URL if active
                from tunnel import tunnel_manager
                t_url = tunnel_manager.get_url(ch.webhook_port or 0)
                if t_url:
                    with ui.row().classes("items-center gap-2"):
                        ui.label("🌐").classes("text-sm")
                        ui.label(t_url).classes("text-sm text-primary")
                        ui.button(
                            icon="content_copy",
                            on_click=lambda u=t_url: (
                                ui.run_javascript(
                                    f"navigator.clipboard.writeText('{u}')"
                                ),
                                ui.notify("Copied!", type="info"),
                            ),
                        ).props("flat dense size=xs")

            # ── Custom UI hook ───────────────────────────────────────
            ch.build_custom_ui(panel)

            # ── DM Pairing Code ──────────────────────────────────────
            with ui.expansion("🔑 DM Pairing Code").classes("w-full mt-2"):
                ui.label(
                    "Generate a one-time code, then DM it to the bot on "
                    f"{ch.display_name} to authorise your account."
                ).classes("text-sm text-grey-6")
                _pair_code_label = ui.label("").classes(
                    "text-h5 text-weight-bold text-center q-my-sm"
                ).style("letter-spacing: 0.3em; user-select: all;")
                _pair_code_label.visible = False

                def _gen_pair_code(ch=ch, lbl=_pair_code_label):
                    from channels.auth import generate_pairing_code
                    code = generate_pairing_code(ch.name)
                    lbl.text = code
                    lbl.visible = True
                    ui.notify(
                        f"Pairing code: {code} — DM this to the bot on {ch.display_name}",
                        type="info",
                        timeout=10000,
                    )

                ui.button(
                    "Generate Code", icon="vpn_key", on_click=_gen_pair_code,
                ).props("flat dense")

            # ── Paired Users ─────────────────────────────────────────
            from channels.auth import get_approved_users, revoke_user, get_user_names
            approved = get_approved_users(ch.name)
            user_names = get_user_names(ch.name) if approved else {}
            if approved:
                with ui.expansion(f"👥 Paired Users ({len(approved)})").classes("w-full mt-2") as paired_exp:
                    _paired_list = ui.column().classes("w-full gap-1")

                    def _render_user_row(uid, container, exp, names):
                        name = names.get(uid, "")
                        with ui.row().classes("items-center w-full justify-between"):
                            if name:
                                ui.label(f"{name}").classes("text-sm")
                                ui.label(f"({uid})").classes("text-xs text-grey-5 font-mono")
                            else:
                                ui.label(uid).classes("text-sm font-mono")

                            def _revoke(
                                ch_name=ch.name,
                                user_id=uid,
                                container=container,
                                exp=exp,
                            ):
                                revoke_user(ch_name, user_id)
                                remaining = get_approved_users(ch_name)
                                updated_names = get_user_names(ch_name)
                                container.clear()
                                if remaining:
                                    exp._props["label"] = f"👥 Paired Users ({len(remaining)})"
                                    exp.update()
                                    with container:
                                        for u in remaining:
                                            _render_user_row(u, container, exp, updated_names)
                                else:
                                    exp.set_visibility(False)
                                ui.notify(f"Revoked {user_id}", type="warning")

                            ui.button(
                                icon="person_remove", on_click=_revoke,
                            ).props("flat dense color=negative size=xs")

                    with _paired_list:
                        for uid in approved:
                            _render_user_row(uid, _paired_list, paired_exp, user_names)

            # ── Setup guide ──────────────────────────────────────────
            guide = ch.setup_guide
            if guide:
                with ui.expansion("ⓘ Setup Guide").classes("w-full mt-2"):
                    ui.markdown(
                        guide,
                        extras=['code-friendly', 'fenced-code-blocks', 'tables'],
                    ).classes("text-sm")

    # ══════════════════════════════════════════════════════════════════
    # STATUS HELPERS (used by Channels tab)
    # ══════════════════════════════════════════════════════════════════

    def _update_channel_status(container, ch):
        container.clear()
        with container:
            if ch.is_running():
                ui.icon("check_circle", color="green").classes("text-lg")
                ui.label(f"{ch.display_name} running").classes("text-green text-sm")
            elif ch.is_configured():
                ui.icon("pause_circle", color="blue").classes("text-lg")
                ui.label("Configured — click Start to begin").classes("text-blue text-sm")
            else:
                ui.icon("warning", color="orange").classes("text-lg")
                ui.label("Not configured").classes("text-orange text-sm")

    # ══════════════════════════════════════════════════════════════════
    # PLUGINS TAB
    # ══════════════════════════════════════════════════════════════════

    def _build_plugins_tab() -> None:
        from plugins.ui_settings import build_plugins_tab as _build_tab

        def _open_marketplace():
            try:
                from plugins.ui_marketplace import open_marketplace_dialog
                open_marketplace_dialog(on_install=lambda: _reopen("Plugins"))
            except Exception as exc:
                logger.warning("Marketplace not available: %s", exc)
                ui.notify("Marketplace not available yet", type="info")

        _build_tab(on_browse_marketplace=_open_marketplace)

    # ══════════════════════════════════════════════════════════════════
    # PREFERENCES TAB
    # ══════════════════════════════════════════════════════════════════

    def _open_migration_wizard_dialog() -> None:
        with ui.dialog().props("maximized") as migration_dlg:
            with ui.card().classes("w-full h-full no-shadow").style(
                "max-width: 64rem; margin: 0 auto;"
            ):
                with ui.row().classes("w-full items-center justify-between px-4 pt-3 pb-1"):
                    with ui.row().classes("items-center gap-2"):
                        ui.icon("move_up", size="sm")
                        ui.label("Migration").classes("text-h5")
                    ui.button(icon="close", on_click=migration_dlg.close).props("flat round size=sm")

                ui.separator()

                with ui.scroll_area().classes("w-full").style("height: calc(100vh - 76px);"):
                    with ui.column().classes("w-full px-6 py-4"):
                        __import__(
                            "ui.migration_wizard",
                            fromlist=["build_migration_wizard_tab"],
                        ).build_migration_wizard_tab()

        migration_dlg.open()

    def _build_preferences_tab() -> None:
        from identity import (
            get_identity_config, save_identity_config,
            sanitize_personality, _DEFAULT_NAME, _PERSONALITY_MAX_LEN,
            is_self_improvement_enabled, set_self_improvement_enabled,
        )

        cfg = get_identity_config()

        ui.label("⚙️ Preferences").classes("text-h6")
        ui.label(
            "Customize the assistant's name and personality."
        ).classes("text-grey-6 text-sm")
        ui.separator()

        # ── Name ─────────────────────────────────────────────────
        ui.label("Assistant name").classes("text-subtitle2 q-mt-sm")

        name_input = ui.input(
            label="Name",
            value=cfg["name"],
            validation={
                "Name cannot be empty": lambda v: bool(v and v.strip()),
            },
        ).classes("w-64")

        def _on_name_change(e):
            val = (e.value or "").strip()
            if not val:
                return
            c = get_identity_config()
            c["name"] = val
            save_identity_config(c)
            clear_agent_cache()

        name_input.on("blur", lambda e: _on_name_change(type("E", (), {"value": name_input.value})))

        ui.separator()

        # ── Personality ──────────────────────────────────────────
        ui.label("Personality").classes("text-subtitle2")
        ui.label(
            "Optional short description of how the assistant should behave. "
            f"Max {_PERSONALITY_MAX_LEN} characters."
        ).classes("text-grey-6 text-xs")

        personality_input = ui.textarea(
            label="Personality",
            value=cfg["personality"],
        ).props(f"maxlength={_PERSONALITY_MAX_LEN} counter").classes("w-full")

        def _on_personality_change(e):
            val = sanitize_personality(e.value or "")
            c = get_identity_config()
            c["personality"] = val
            save_identity_config(c)
            clear_agent_cache()
            if val != (e.value or ""):
                personality_input.set_value(val)
                ui.notify("Some text was removed (disallowed patterns)", type="warning")

        personality_input.on(
            "blur",
            lambda e: _on_personality_change(type("E", (), {"value": personality_input.value})),
        )

        ui.separator()

        # ── Preview ──────────────────────────────────────────────
        ui.label("Preview").classes("text-subtitle2")
        preview = ui.label().classes("text-grey-6 text-sm italic")

        def _update_preview():
            n = (name_input.value or _DEFAULT_NAME).strip() or _DEFAULT_NAME
            p_text = sanitize_personality(personality_input.value or "")
            line = f"You are {n}, a knowledgeable personal assistant with access to tools."
            if p_text:
                line += f" {p_text}"
            preview.set_text(line)

        _update_preview()
        name_input.on("update:model-value", lambda _: _update_preview())
        personality_input.on("update:model-value", lambda _: _update_preview())

        ui.separator()

        # ── Self-Improvement Toggle ──────────────────────────────
        ui.label("Self-Improvement").classes("text-subtitle2")
        ui.label(
            "When enabled, the assistant can create and improve skills, "
            "and receives guidance on how to get better at tasks over time."
        ).classes("text-grey-6 text-xs")

        def _on_self_improve_change(e):
            set_self_improvement_enabled(e.value)
            clear_agent_cache()
            ui.notify(
                "Self-improvement enabled" if e.value else "Self-improvement disabled",
                type="info",
            )

        ui.switch(
            "Enable self-improvement",
            value=is_self_improvement_enabled(),
            on_change=_on_self_improve_change,
        )

        # ── Auto-update section ─────────────────────────────────
        try:
            from ui.update_dialog import build_update_section
            build_update_section()
        except Exception:  # pragma: no cover - defensive
            logger.debug("update section failed to build", exc_info=True)

        ui.separator()

        # ── Migration utility ───────────────────────────────────
        ui.label("Migration").classes("text-subtitle2")
        ui.label(
            "Import selected data from Hermes Agent or OpenClaw when setting up Thoth."
        ).classes("text-grey-6 text-xs")
        ui.button(
            "Open Migration Wizard",
            icon="move_up",
            on_click=_open_migration_wizard_dialog,
        ).props("unelevated no-caps color=primary")

    # ══════════════════════════════════════════════════════════════════
    # DIALOG SHELL
    # ══════════════════════════════════════════════════════════════════

    p.settings_dlg.clear()
    with p.settings_dlg:
        with ui.card().classes("w-full h-full no-shadow").style(
            "max-width: 64rem; margin: 0 auto;"
        ):
            with ui.row().classes("w-full items-center justify-between px-4 pt-3 pb-1"):
                with ui.row().classes("items-center gap-2"):
                    ui.icon("settings", size="sm")
                    ui.label("Settings").classes("text-h5")
                ui.button(icon="close", on_click=p.settings_dlg.close).props("flat round size=sm")

            ui.separator()

            _tab_map = {}
            with ui.splitter(value=18).classes("w-full flex-grow").props(
                "disable"
            ).style("height: calc(100vh - 100px);") as splitter:
                with splitter.before:
                    with ui.tabs().props("vertical").classes("w-full h-full") as tabs:
                        tab_models = ui.tab("Models", icon="smart_toy")
                        tab_cloud = ui.tab("Cloud", icon="cloud")
                        tab_knowledge = ui.tab("Knowledge", icon="psychology")
                        tab_voice = ui.tab("Voice", icon="mic")
                        tab_fs = ui.tab("System", icon="terminal")
                        tab_tracker = ui.tab("Tracker", icon="checklist")
                        tab_docs = ui.tab("Documents", icon="description")
                        tab_tools = ui.tab("Search", icon="search")
                        tab_skills = ui.tab("Skills", icon="auto_fix_high")
                        tab_accounts = ui.tab("Accounts", icon="group")
                        tab_channels = ui.tab("Channels", icon="forum")
                        tab_utils = ui.tab("Utilities", icon="build")
                        tab_mcp = ui.tab("MCP", icon="hub")
                        tab_plugins = ui.tab("Plugins", icon="extension")
                        tab_prefs = ui.tab("Preferences", icon="tune")
                        _tab_map = {
                            "Models": tab_models, "Cloud": tab_cloud,
                            "Knowledge": tab_knowledge,
                            "Voice": tab_voice,
                            "System": tab_fs, "Tracker": tab_tracker,
                            "Documents": tab_docs, "Search": tab_tools,
                            "Skills": tab_skills,
                            "Google": tab_accounts,
                            "Gmail": tab_accounts, "Calendar": tab_accounts,
                            "Accounts": tab_accounts,
                            "Channels": tab_channels, "Utilities": tab_utils,
                            "MCP": tab_mcp,
                            "Migration": tab_prefs,
                            "Plugins": tab_plugins,
                            "Preferences": tab_prefs,
                        }

                _initial = _tab_map.get(initial_tab, tab_models)

                # ── Lazy tab loading (build only visible tab) ──
                _tab_defs = [
                    (tab_docs, "Documents", _build_documents_tab),
                    (tab_models, "Models", _build_models_tab),
                    (tab_cloud, "Cloud", _build_cloud_tab),
                    (tab_tools, "Search", _build_tools_tab),
                    (tab_skills, "Skills", _build_skills_tab),
                    (tab_fs, "System", _build_system_access_tab),
                    (tab_accounts, "Accounts", _build_accounts_tab),
                    (tab_utils, "Utilities", _build_utilities_tab),
                    (tab_tracker, "Tracker", _build_tracker_tab),
                    (tab_knowledge, "Knowledge", _build_knowledge_tab),
                    (tab_voice, "Voice", _build_voice_tab),
                    (tab_channels, "Channels", _build_channels_tab),
                    (tab_mcp, "MCP", lambda: __import__("ui.mcp_settings", fromlist=["build_mcp_settings_tab"]).build_mcp_settings_tab(_reopen)),
                    (tab_plugins, "Plugins", _build_plugins_tab),
                    (tab_prefs, "Preferences", _build_preferences_tab),
                ]
                _built_tabs: set[str] = set()
                _panel_map: dict[str, object] = {}
                _builder_map: dict[str, Callable] = {}

                with splitter.after:
                    with ui.tab_panels(tabs, value=_initial).classes("w-full h-full"):
                        for _t_obj, _t_name, _t_builder in _tab_defs:
                            with ui.tab_panel(_t_obj).classes("px-6 py-4") as _pnl:
                                if _t_obj is _initial:
                                    _t_builder()
                                    _built_tabs.add(_t_name)
                                else:
                                    ui.spinner(size="lg").classes("block mx-auto mt-8")
                            _panel_map[_t_name] = _pnl
                            _builder_map[_t_name] = _t_builder

                    def _on_tab_switch(e):
                        name = e.value if isinstance(e.value, str) else None
                        if name and name not in _built_tabs and name in _builder_map:
                            _built_tabs.add(name)
                            panel = _panel_map[name]
                            panel.clear()
                            with panel:
                                _builder_map[name]()

                    tabs.on_value_change(_on_tab_switch)

    p.settings_dlg.open()
