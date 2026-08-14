"""Row-Bot modular NiceGUI frontend.

Refactored UI using the ``ui/`` package.

Run:   python app.py              →   http://localhost:8080
    ROW_BOT_PORT=8081 python app.py → http://localhost:8081
"""

from __future__ import annotations

import asyncio
import builtins
from contextlib import contextmanager
import hmac
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

_APP_BOOT_STARTED = time.perf_counter()
_LAUNCH_SESSION_ID = os.environ.get("ROW_BOT_LAUNCH_SESSION_ID", "")
_FIRST_LAUNCHER_PING_LOGGED = False
_DISCORD_BENIGN_VOICE_LOGGERS = (
    "discord.client",
    "discord.gateway",
)

# ── Configure root logger (same as production app) ──────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stderr,
)
for _noisy in ("httpx", "httpcore", "urllib3", "asyncio", "multipart",
               "watchfiles", "nicegui", "uvicorn.error", "uvicorn.access",
               "sentence_transformers", "transformers", "huggingface_hub",
               "googleapiclient", "googleapiclient.discovery_cache",
               "primp", "ddgs", "ddgs.ddgs", "faster_whisper",
               "streamlit", "kaleido", "choreographer", "pyngrok",
               "pyngrok.process", "pyngrok.process.ngrok"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
for _discord_noisy in _DISCORD_BENIGN_VOICE_LOGGERS:
    logging.getLogger(_discord_noisy).setLevel(logging.ERROR)

os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
from row_bot.brand import APP_BRAND_ACCENT, APP_DISPLAY_NAME, APP_PING_ID, APP_USER_AGENT
from row_bot.data_paths import get_row_bot_data_dir
from row_bot.access.launcher_control import LAUNCH_SECRET_ENV
from row_bot.docs_capture import (
    configure_docs_capture_state,
    docs_capture_bootstrap_html,
    docs_capture_disable_autostart,
    docs_capture_query_params,
    docs_capture_reduce_motion_css,
    is_docs_capture,
)
from row_bot.runtime_paths import static_dir
from row_bot.version import __version__ as _app_version
os.environ.setdefault("USER_AGENT", APP_USER_AGENT)

logger = logging.getLogger(__name__)


def _app_boot_event(event: str, **fields) -> None:
    elapsed_ms = (time.perf_counter() - _APP_BOOT_STARTED) * 1000.0
    compact = " ".join(f"{key}={value}" for key, value in fields.items() if value is not None)
    logger.info(
        "app.boot.%s elapsed_ms=%.1f session=%s%s%s",
        event,
        elapsed_ms,
        _LAUNCH_SESSION_ID,
        " " if compact else "",
        compact,
    )


_app_boot_event("module_logger_ready", python=sys.executable, cwd=os.getcwd())


def _safe_console_print(message: object) -> None:
    text = str(message)
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        builtins.print(text)
    except UnicodeEncodeError:
        builtins.print(text.encode(encoding, errors="replace").decode(encoding, errors="replace"))

from row_bot.stability import (
    install_asyncio_exception_handler,
    mark_shutdown,
    record_client_error,
    setup_stability_monitoring,
    start_performance_monitor,
    stop_performance_monitor,
)

setup_stability_monitoring()

try:
    from row_bot.startup_diagnostics import preflight_optional_native_packages

    preflight_optional_native_packages(logger)
except Exception:
    logger.debug("Startup diagnostics failed", exc_info=True)

from nicegui import ui, app, run
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from row_bot.app_port import get_app_host, get_app_port
from row_bot.ui.performance import log_ui_perf
from row_bot.ui.timer_utils import deactivate_on_disconnect, defer_ui, safe_timer, safe_ui_task

_APP_PORT = get_app_port()
_APP_HOST = get_app_host()


@app.post("/api/voice/realtime/client-secret")
async def _voice_realtime_client_secret() -> dict:
    """Mint a browser-safe OpenAI Realtime client secret.

    The long-lived provider key stays on the server. The browser receives only
    the ephemeral credential returned by OpenAI.
    """
    try:
        from row_bot.voice.openai_realtime import OpenAIRealtimeProvider, ROW_BOT_REALTIME_INSTRUCTIONS
        from row_bot.voice.runtime import load_voice_runtime_settings

        voice_settings = load_voice_runtime_settings()
        return OpenAIRealtimeProvider(
            model=voice_settings.talk_model,
            voice=voice_settings.realtime_voice,
        ).create_client_secret(
            instructions=ROW_BOT_REALTIME_INSTRUCTIONS,
        )
    except Exception as exc:
        logger.warning("OpenAI Realtime client secret creation failed", exc_info=True)
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _browser_voice_context(request: Request):
    from row_bot.access.policy import context_from_scope, require_authenticated_owner

    context = require_authenticated_owner(request.scope)
    host = str(request.url.hostname or "").strip("[]").lower()
    if context.scheme != "https" and host not in {"localhost", "127.0.0.1", "::1"}:
        raise HTTPException(status_code=400, detail="secure_context_required")
    session_key = context.session_id or (
        f"local:{context.transport_peer}:{context.origin}"
    )
    return context, session_key


async def _bounded_request_body(request: Request, maximum: int) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > maximum:
                raise HTTPException(status_code=413, detail="request_too_large")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid_content_length") from exc
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > maximum:
            raise HTTPException(status_code=413, detail="request_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def _browser_voice_error_response(exc: Exception) -> JSONResponse:
    from row_bot.voice.browser_local import BrowserVoiceError

    if isinstance(exc, BrowserVoiceError):
        return JSONResponse(
            {"ok": False, "error": exc.code},
            status_code=exc.status_code,
            headers={"Cache-Control": "no-store"},
        )
    logger.warning("Browser-local voice operation failed", exc_info=True)
    return JSONResponse(
        {"ok": False, "error": "voice_operation_failed"},
        status_code=503,
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/voice/local/status")
async def _browser_voice_status(request: Request) -> JSONResponse:
    _context, _session_key = _browser_voice_context(request)
    from row_bot.voice.browser_local import get_browser_local_voice_service

    return JSONResponse(
        get_browser_local_voice_service().status(),
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/voice/local/transcribe")
async def _browser_voice_transcribe(request: Request) -> JSONResponse:
    _context, session_key = _browser_voice_context(request)
    from row_bot.voice.browser_local import (
        MAX_INPUT_BYTES,
        get_browser_local_voice_service,
    )

    payload = await _bounded_request_body(request, MAX_INPUT_BYTES)
    try:
        text = await asyncio.to_thread(
            get_browser_local_voice_service().transcribe,
            session_key,
            payload,
            request.headers.get("content-type", ""),
        )
    except Exception as exc:
        return _browser_voice_error_response(exc)
    return JSONResponse(
        {"ok": True, "text": text},
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/voice/local/synthesize")
async def _browser_voice_synthesize(request: Request) -> Response:
    _context, session_key = _browser_voice_context(request)
    raw = await _bounded_request_body(request, 16 * 1024)
    try:
        import json as _json

        body = _json.loads(raw.decode("utf-8"))
        text = str(body.get("text") or "") if isinstance(body, dict) else ""
    except (UnicodeDecodeError, ValueError):
        return JSONResponse(
            {"ok": False, "error": "invalid_json"},
            status_code=400,
            headers={"Cache-Control": "no-store"},
        )
    from row_bot.voice.browser_local import get_browser_local_voice_service

    try:
        audio = await asyncio.to_thread(
            get_browser_local_voice_service().synthesize,
            session_key,
            text,
        )
    except Exception as exc:
        return _browser_voice_error_response(exc)
    return Response(
        content=audio,
        media_type="audio/wav",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": 'inline; filename="row-bot-voice.wav"',
        },
    )


@app.post("/api/voice/local/models/{model_name}/install")
async def _browser_voice_install_model(
    request: Request,
    model_name: str,
) -> JSONResponse:
    _context, session_key = _browser_voice_context(request)
    from row_bot.voice.browser_local import get_browser_local_voice_service

    service = get_browser_local_voice_service()
    installers = {
        "whisper": service.install_whisper,
        "kokoro": service.install_kokoro,
    }
    install = installers.get(str(model_name or "").lower())
    if install is None:
        return JSONResponse(
            {"ok": False, "error": "unknown_voice_model"},
            status_code=404,
            headers={"Cache-Control": "no-store"},
        )
    try:
        await asyncio.to_thread(install, session_key)
    except Exception as exc:
        return _browser_voice_error_response(exc)
    return JSONResponse(
        {"ok": True, "model": model_name},
        headers={"Cache-Control": "no-store"},
    )


# ── Patch NiceGUI JSON serializer for surrogate safety ───────────────────────
# PDF text extraction and web scraping can inject lone UTF-16 surrogates
# (U+D800–U+DFFF) into tool results stored in LangGraph checkpoints.
# orjson (used by NiceGUI's socketio transport) rejects these with
# "surrogates not allowed".  This patch catches the error and strips
# surrogates on retry — zero cost for clean data.
def _patch_json_serializer() -> None:
    import nicegui.json as _nj
    import nicegui.json.orjson_wrapper as _ow
    from row_bot.utils.text import _SURROGATE_RE as _SURR

    _orig = _ow.dumps

    def _strip(obj):
        # Only recurse into EXACT built-in container types.  Subclasses
        # (e.g. ``nicegui.classes.Classes``/``ObservableList``) require
        # extra constructor kwargs and must not be reinstantiated here —
        # doing so raises ``TypeError`` and crashes the outbox emit loop.
        t = type(obj)
        if t is str:
            return _SURR.sub('', obj) if _SURR.search(obj) else obj
        if t is dict:
            # Build a new dict (orjson already failed on this one, safe).
            return {_strip(k): _strip(v) for k, v in obj.items()}
        if t is list:
            # Mutate in place — cheaper and keeps identity stable.
            for i, v in enumerate(obj):
                obj[i] = _strip(v)
            return obj
        if t is tuple:
            return tuple(_strip(v) for v in obj)
        # Unknown type (Classes, Props, custom Element, datetime, etc.):
        # leave untouched.  If it still breaks orjson the original
        # TypeError propagates — matches pre-patch behaviour.
        return obj

    def _safe_dumps(obj, *args, **kwargs):
        try:
            return _orig(obj, *args, **kwargs)
        except (TypeError, UnicodeEncodeError):
            return _orig(_strip(obj), *args, **kwargs)

    _ow.dumps = _safe_dumps
    _nj.dumps = _safe_dumps

_patch_json_serializer()


# ── UI package ───────────────────────────────────────────────────────────────
from row_bot.ui.state import (
    AppState, GenerationState, P,
    _active_generations,
    active_context_identity,
    cache_and_project_context_usage,
    canonical_context_model_ref,
    clear_context_usage_projection,
    context_history_present,
    startup_ready, startup_status, startup_warnings,
)
from row_bot.ui.constants import EXAMPLE_PROMPTS, welcome_message
from row_bot.ui.helpers import (
    is_first_run, is_setup_complete, mark_onboarding_seen,
    load_thread_messages, browse_file,
)
from row_bot.ui.head_html import inject_head_html
from row_bot.ui.setup_wizard import show_setup_wizard
from row_bot.ui.render import (
    add_chat_message,
    agent_result_use_prompt,
    render_text_with_embeds,
)
from row_bot.ui.export import open_export
from row_bot.ui.graph_panel import build_graph_panel
from row_bot.ui.task_dialog import show_task_dialog
from row_bot.ui.sidebar import build_sidebar
from row_bot.ui.command_center import build_command_center
from row_bot.ui.settings import open_settings
from row_bot.ui.mobile import build_mobile_shell
from row_bot.ui.streaming import (
    Callbacks,
    _agent_poll_refresh_keys,
    _append_async_delegated_agent_completion_messages,
    _sync_thread_approval_messages,
    _thread_has_attached_live_generation,
    _update_direct_agent_refresh_keys,
    build_interrupt_dialog,
    send_message,
)
from row_bot.ui.home import build_home
from row_bot.ui.chat import build_chat
from row_bot.ui.transcript import (
    TRANSCRIPT_CHUNK_TARGET_MS,
    TRANSCRIPT_MAX_CHUNK_MESSAGES,
    choose_transcript_window,
    common_key_prefix,
    match_durable_orchestration_outputs,
    message_key,
    message_keys,
    rendered_window_matches,
    transcript_message_child_bounds,
    upsert_durable_transcript_message,
)

# ── Backend imports ──────────────────────────────────────────────────────────
from row_bot.threads import build_auto_thread_title, rename_thread, should_auto_rename_thread
from row_bot.models import (
    get_current_model, is_cloud_model, is_cloud_available,
    is_model_local, refresh_cloud_models,
)
from row_bot.api_keys import apply_keys
from row_bot.memory_extraction import (
    mark_user_activity, schedule_idle_extraction, start_periodic_extraction, set_active_thread,
)
from row_bot.dream_cycle import start_dream_loop
from row_bot.tasks import (
    ensure_task_schema,
    seed_default_tasks,
    start_task_scheduler,
    get_running_tasks,
    stop_task,
)
from row_bot.notifications import drain_toasts

# ── Channels ─────────────────────────────────────────────────────────────────
from row_bot.channels import config as _ch_config
from row_bot.channels import registry as _ch_registry

_CHANNEL_MODULES = (
    "row_bot.channels.telegram",
    "row_bot.channels.slack",
    "row_bot.channels.sms",
    "row_bot.channels.discord_channel",
    "row_bot.channels.whatsapp",
)


def _load_channel_modules() -> list[str]:
    """Import channel adapters so installed extras self-register."""
    import importlib

    skipped: list[str] = []
    for module_name in _CHANNEL_MODULES:
        try:
            importlib.import_module(module_name)
        except ImportError as exc:
            skipped.append(f"{module_name}: {exc}")
            logger.info("Optional channel module skipped: %s (%s)", module_name, exc)
    return skipped


def _startup_fields(**fields) -> str:
    parts: list[str] = []
    for key, value in fields.items():
        if value is None:
            continue
        text = str(value).replace("\n", " ")[:200]
        parts.append(f"{key}={text}")
    return " ".join(parts)


@contextmanager
def _startup_phase(name: str, **fields):
    started = time.perf_counter()
    try:
        yield
    except Exception:
        duration_ms = (time.perf_counter() - started) * 1000.0
        logger.info(
            "startup.phase name=%s duration_ms=%.1f success=false %s",
            name,
            duration_ms,
            _startup_fields(**fields),
        )
        raise
    else:
        duration_ms = (time.perf_counter() - started) * 1000.0
        logger.info(
            "startup.phase name=%s duration_ms=%.1f success=true %s",
            name,
            duration_ms,
            _startup_fields(**fields),
        )


def _schedule_background_task(coro, *, name: str):
    task = asyncio.create_task(coro, name=name)

    def _log_failure(done_task: asyncio.Task) -> None:
        if done_task.cancelled():
            return
        try:
            exc = done_task.exception()
        except Exception as callback_exc:
            logger.debug("Could not inspect background task %s: %s", name, callback_exc)
            return
        if exc is not None:
            logger.warning("Background startup task %s failed: %s", name, exc, exc_info=exc)

    task.add_done_callback(_log_failure)
    return task


async def _repair_orchestration_recovery_batch(after_id: str = "") -> None:
    """Run one post-ready local repair batch and continue only if needed."""

    from row_bot.agent_orchestrator import repair_interrupted_orchestrations_batch

    result = await asyncio.to_thread(
        repair_interrupted_orchestrations_batch,
        limit=20,
        after_id=after_id,
    )
    if int(result.get("processed") or 0):
        logger.info("Deferred orchestration recovery: %s", result)
    if result.get("has_more"):
        await asyncio.sleep(0)
        _schedule_background_task(
            _repair_orchestration_recovery_batch(
                str(result.get("next_cursor") or after_id)
            ),
            name="row-bot-orchestration-recovery-next",
        )


async def _auto_start_channel_background(channel, _st) -> None:
    channel_name = str(getattr(channel, "name", "") or "")
    display_name = str(getattr(channel, "display_name", channel_name) or channel_name)
    started = time.perf_counter()
    ok = False
    try:
        ok = bool(await channel.start())
        if ok:
            _safe_console_print(f"[startup] ✅ {display_name} auto-started")
            from row_bot.channels.registry import clear_agent_cache_if_loaded

            clear_agent_cache_if_loaded()
            try:
                from row_bot.channels.thread_notifications import reconcile_pending_channel_notifications

                delivered = await asyncio.to_thread(reconcile_pending_channel_notifications, 25)
                from row_bot.agent_orchestrator import retry_pending_deliveries

                await asyncio.to_thread(retry_pending_deliveries, 25)
                if delivered:
                    logger.info(
                        "startup.channel.pending_notifications_delivered channel=%s count=%d",
                        channel_name,
                        delivered,
                    )
            except Exception:
                logger.debug("Channel notification reconciliation failed", exc_info=True)
        else:
            _st.startup_warnings.append(
                f"⚠️ {display_name} failed to auto-start — check Settings → Channels"
            )
    except Exception as exc:
        _st.startup_warnings.append(
            f"⚠️ {display_name} failed to auto-start: {exc}"
        )
        logger.warning("Channel auto-start failed for %s: %s", channel_name, exc)
    finally:
        logger.info(
            "startup.channel.auto_start channel=%s duration_ms=%.1f ok=%s",
            channel_name,
            (time.perf_counter() - started) * 1000.0,
            ok,
        )


async def _auto_start_channels_background(channels: list, _st) -> None:
    logger.info("startup.channels.auto_start_begin count=%d", len(channels))
    for channel in channels:
        await _auto_start_channel_background(channel, _st)
    try:
        from row_bot.channels.thread_notifications import reconcile_pending_channel_notifications

        delivered = await asyncio.to_thread(reconcile_pending_channel_notifications, 50)
        from row_bot.agent_orchestrator import retry_pending_deliveries

        await asyncio.to_thread(retry_pending_deliveries, 50)
        if delivered:
            logger.info("startup.channels.pending_notifications_delivered count=%d", delivered)
    except Exception:
        logger.debug("Channel notification reconciliation after auto-start failed", exc_info=True)
    logger.info("startup.channels.auto_start_complete count=%d", len(channels))


def _schedule_auto_start_channels(channels: list, _st):
    if not channels:
        logger.info("startup.channels.auto_start_none")
        return None
    for channel in channels:
        logger.info(
            "startup.channel.auto_start_scheduled channel=%s",
            getattr(channel, "name", ""),
        )
    return _schedule_background_task(
        _auto_start_channels_background(channels, _st),
        name="row-bot-channel-autostart",
    )


async def _prewarm_agent_graph_background() -> None:
    with _startup_phase("agent_graph_prewarm", background=True):
        from row_bot.agent import get_agent_graph
        from row_bot.providers.readiness import AgentCompatibilityError

        try:
            await asyncio.to_thread(get_agent_graph)
        except AgentCompatibilityError as exc:
            logger.info("startup.agent_graph_prewarm skipped=agent_not_ready reason=%s", exc)


def _schedule_agent_graph_prewarm():
    if os.environ.get("ROW_BOT_DEPLOYMENT_MODE", "desktop").strip().lower() == "server":
        logger.info("startup.agent_graph_prewarm skipped=server_mode")
        return None
    return _schedule_background_task(
        _prewarm_agent_graph_background(),
        name="row-bot-agent-graph-prewarm",
    )


async def _prewarm_local_embeddings_background() -> None:
    """Warm cached local embeddings only when semantic memory can use them."""
    with _startup_phase("local_embedding_prewarm", background=True):
        from row_bot.embedding_config import get_embedding_config

        config = get_embedding_config()
        if config.get("provider") != "local":
            logger.info("startup.local_embedding_prewarm skipped=cloud_provider")
            return
        from row_bot import knowledge_graph

        vector_status = await asyncio.to_thread(knowledge_graph.memory_vector_status)
        entity_count = await asyncio.to_thread(knowledge_graph.count_entities)
        if not vector_status.get("ready") or entity_count <= 0:
            logger.info(
                "startup.local_embedding_prewarm skipped=memory_index_%s entities=%d",
                vector_status.get("state"),
                entity_count,
            )
            return
        from row_bot.embedding_providers import start_local_embedding_load

        load_status = start_local_embedding_load()
        logger.info(
            "startup.local_embedding_prewarm state=%s model=%s",
            load_status.get("state"),
            load_status.get("model_key"),
        )


def _schedule_local_embedding_prewarm():
    return _schedule_background_task(
        _prewarm_local_embeddings_background(),
        name="row-bot-local-embedding-prewarm",
    )


# ═════════════════════════════════════════════════════════════════════════════
# SINGLETON STATE
# ═════════════════════════════════════════════════════════════════════════════

state = AppState()
state.show_onboarding = is_first_run()


# ═════════════════════════════════════════════════════════════════════════════
# OAUTH TOKEN HEALTH
# ═════════════════════════════════════════════════════════════════════════════

def _check_oauth_tokens(_st=None) -> list[str]:
    """Check Gmail & Calendar OAuth tokens if those tools are enabled.

    Attempts silent refresh when possible.  Returns a list of warning
    strings (empty if everything is healthy).  If *_st* is provided,
    warnings are also appended to ``_st.startup_warnings``.
    """
    from row_bot.tools import registry as _reg
    warnings: list[str] = []

    for tool_name, display in [("gmail", "Gmail"), ("calendar", "Calendar"), ("x", "X (Twitter)")]:
        if not _reg.is_enabled(tool_name):
            continue
        tool = _reg.get_tool(tool_name)
        if tool is None or not tool.is_authenticated():
            continue
        try:
            status, detail = tool.check_token_health()
            if status in ("valid", "refreshed"):
                label = "token healthy" if status == "valid" else "token refreshed"
                _safe_console_print(f"[oauth] ✅ {display} {label}")
            elif status == "expired":
                msg = f"⚠️ {display} token expired — re-authenticate in Settings → Accounts"
                warnings.append(msg)
                _safe_console_print(f"[oauth] {msg}")
            elif status == "error":
                msg = f"⚠️ {display} token error: {detail}"
                warnings.append(msg)
                _safe_console_print(f"[oauth] {msg}")
        except Exception as exc:
            logger.warning("OAuth check failed for %s: %s", display, exc)

    if _st is not None:
        _st.startup_warnings.extend(warnings)
    return warnings


def _check_github_account_health(_st=None) -> list[str]:
    """Check configured GitHub credentials without warning for anonymous use."""
    warnings: list[str] = []
    try:
        import row_bot.github_account as github_account

        status = github_account.get_verified_github_account_status(use_cache=True)
        if status.source and status.state in {
            github_account.GITHUB_STATE_INVALID_TOKEN,
            github_account.GITHUB_STATE_RATE_LIMITED,
            github_account.GITHUB_STATE_SECONDARY_LIMITED,
            github_account.GITHUB_STATE_OFFLINE,
        }:
            msg = f"GitHub account needs attention: {status.settings_message or status.message}"
            warnings.append(msg)
            _safe_console_print(f"[github] {msg}")
        elif status.connected:
            user = f" as {status.user}" if status.user else ""
            _safe_console_print(f"[github] GitHub API healthy{user}")
    except Exception as exc:
        logger.warning("GitHub account health check failed: %s", exc)

    if _st is not None:
        _st.startup_warnings.extend(warnings)
    return warnings


def _periodic_oauth_check():
    """Background OAuth health check — runs every 6 hours."""
    warnings = _check_oauth_tokens()
    warnings.extend(_check_github_account_health())
    if warnings:
        from row_bot.notifications import notify as _oauth_notify
        for msg in warnings:
            _oauth_notify("Account Issue", msg, sound="default",
                          icon="⚠️", toast_type="warning")


# ═════════════════════════════════════════════════════════════════════════════
# STARTUP / SHUTDOWN
# ═════════════════════════════════════════════════════════════════════════════

@app.on_startup
async def on_startup():
    import row_bot.ui.state as _st

    _st.startup_ready = False
    _st.startup_status = "Starting Row-Bot..."
    _app_boot_event("startup_shell_ready", host=_APP_HOST, port=_APP_PORT)
    logger.info(
        "%s startup shell ready; scheduling background startup (session=%s)",
        APP_DISPLAY_NAME,
        os.environ.get("ROW_BOT_LAUNCH_SESSION_ID", ""),
    )
    asyncio.create_task(_run_startup_sequence_guarded(), name="row-bot-startup-sequence")


async def _run_startup_sequence_guarded():
    startup_total_started = time.perf_counter()
    try:
        await _run_startup_sequence()
    except Exception as exc:
        import row_bot.ui.state as _st

        _st.startup_status = f"Startup error: {exc}"
        _st.startup_warnings.append(str(exc))
        logger.info(
            "startup.phase name=startup_sequence_total duration_ms=%.1f success=false",
            (time.perf_counter() - startup_total_started) * 1000.0,
        )
        logger.exception("%s background startup failed", APP_DISPLAY_NAME)
    else:
        logger.info(
            "startup.phase name=startup_sequence_total duration_ms=%.1f success=true",
            (time.perf_counter() - startup_total_started) * 1000.0,
        )


async def _run_startup_sequence():
    _app_boot_event("startup_sequence_start")
    install_asyncio_exception_handler()
    start_performance_monitor()
    # Attach persistent file logging (daily JSONL to the Row-Bot data dir).
    from row_bot.logging_config import setup_file_logging
    with _startup_phase("file_logging"):
        setup_file_logging()

    if docs_capture_disable_autostart():
        import row_bot.ui.state as _st

        _st.startup_status = "Docs capture ready"
        _st.startup_ready = True
        _safe_console_print("[startup] Docs capture enabled - background autostart skipped")
        _app_boot_event("startup_docs_capture_ready")
        return

    try:
        from row_bot.startup_diagnostics import preflight_required_runtime_packages
        with _startup_phase("required_runtime_diagnostics"):
            preflight_required_runtime_packages(logger)
    except Exception:
        logger.debug("Required runtime diagnostics failed", exc_info=True)

    # One-shot: clear project_id on thread_meta rows whose designer
    # project JSON is missing. Prevents the "All Conversations" view
    # from showing threads that claim to belong to a deleted project.
    try:
        from row_bot.threads import sweep_orphan_project_ids
        with _startup_phase("orphan_project_sweep"):
            sweep_orphan_project_ids()
    except Exception:
        logger.exception("Orphan project_id sweep failed")

    import row_bot.ui.state as _st

    logger.info("%s startup initiated", APP_DISPLAY_NAME)
    try:
        from row_bot.data_paths import describe_data_paths
        logger.info("%s data paths: %s", APP_DISPLAY_NAME, describe_data_paths())
    except Exception:
        logger.debug("Could not describe %s data paths", APP_DISPLAY_NAME, exc_info=True)

    def _set(msg: str):
        _st.startup_status = msg
        _app_boot_event("startup_phase", status=msg)
        _safe_console_print(f"[startup] {msg}")

    _set("🔑 Applying API keys…")
    with _startup_phase("apply_keys"):
        await asyncio.to_thread(apply_keys)

    if is_cloud_available():
        _set("☁️ Loading cached model catalog...")
        with _startup_phase("load_cached_model_catalog"):
            state.current_model = get_current_model()

    _set("🔄 Scheduling memory extraction…")
    with _startup_phase("memory_extraction_scheduler"):
        await asyncio.to_thread(start_periodic_extraction)
        await asyncio.to_thread(schedule_idle_extraction)

    _set("Recovering document ingestion…")
    try:
        from row_bot.document_jobs import ensure_document_supervisor

        with _startup_phase("document_ingestion_supervisor"):
            await asyncio.to_thread(ensure_document_supervisor)
    except Exception as exc:
        logger.warning(
            "Document ingestion startup recovery skipped (non-fatal): %s",
            exc,
        )
        _st.startup_warnings.append(
            "Document ingestion could not start. Existing document search remains available; "
            "restart Row-Bot or review System Diagnosis before uploading more files."
        )

    _set("🌙 Starting dream cycle daemon…")
    with _startup_phase("dream_cycle_daemon"):
        await asyncio.to_thread(start_dream_loop)

    _set("⬆ Starting auto-update scheduler…")
    try:
        from row_bot.updater import start_update_scheduler
        with _startup_phase("update_scheduler"):
            await asyncio.to_thread(start_update_scheduler)
    except Exception as exc:
        logger.warning("Updater scheduler failed to start (non-fatal): %s", exc)

    _set("⚡ Loading workflows…")
    try:
        with _startup_phase("workflow_scheduler"):
            await asyncio.to_thread(ensure_task_schema)
            await asyncio.to_thread(lambda: (seed_default_tasks(), start_task_scheduler()))
    except Exception as exc:
        logger.warning("Workflow startup skipped after task DB repair failure: %s", exc)
        _st.startup_warnings.append(
            "Workflow data is temporarily unavailable. "
            "Run launcher.py --reset-tasks-db if it does not recover after restart."
        )

    _set("Recovering Agent runs...")
    try:
        from row_bot.agent_runs import recover_stale_agent_runs

        with _startup_phase("agent_run_recovery"):
            recovery = await asyncio.to_thread(recover_stale_agent_runs)
        if any(int(value or 0) for value in recovery.values()):
            logger.info("Agent Run startup recovery: %s", recovery)
    except Exception as exc:
        logger.warning("Agent Run startup recovery skipped (non-fatal): %s", exc)

    # ── Load Plugins ────────────────────────────────────────────────────────
    _set("🔌 Loading plugins…")
    try:
        from row_bot.plugins.loader import refresh_plugin_runtime
        with _startup_phase("plugin_runtime_refresh"):
            results = await asyncio.to_thread(
                refresh_plugin_runtime,
                "startup",
                discover_mcp=False,
                clear_agent=False,
            )
        loaded = sum(1 for r in results if r.success and not getattr(r, "stale", False))
        failed = sum(1 for r in results if not r.success)
        stale = sum(1 for r in results if getattr(r, "stale", False))
        if loaded or failed or stale:
            _safe_console_print(
                f"[startup] 🔌 Plugins: {loaded} loaded, {failed} failed, {stale} stale"
            )
        for r in results:
            if not r.success and r.error:
                _st.startup_warnings.append(f"⚠️ Plugin '{r.plugin_id}' failed: {r.error}")
            elif getattr(r, "stale", False):
                _st.startup_warnings.append(
                    f"⚠️ Legacy plugin '{r.plugin_id}' moved to stale plugins."
                )
    except Exception as exc:
        logger.warning("Plugin loading failed (non-fatal): %s", exc)

    _set("🔌 Starting MCP servers…")
    try:
        from row_bot.mcp_client.runtime import discover_enabled_servers
        with _startup_phase("mcp_discovery"):
            await asyncio.to_thread(discover_enabled_servers)
    except Exception as exc:
        logger.warning("MCP startup skipped (non-fatal): %s", exc)

    # Prepare channels via registry. Actual live channel handshakes run after
    # core UI readiness so slow providers do not block the local app shell.
    _set("📡 Preparing channels…")
    # Ensure channel modules are imported so they self-register.
    with _startup_phase("channel_module_import"):
        skipped_channels = _load_channel_modules()
    for skipped_channel in skipped_channels:
        _st.startup_warnings.append(
            f"Channel adapter unavailable: {skipped_channel}. "
            "Install the channels extra to enable it."
        )
    try:
        from row_bot.channels.auth_store import migrate_legacy_channel_secrets
        with _startup_phase("channel_secret_migration"):
            migrated = await asyncio.to_thread(
                migrate_legacy_channel_secrets,
                _ch_registry.all_channels(),
            )
        if migrated.get("migrated"):
            _safe_console_print(
                f"[startup] 🔐 Migrated {migrated['migrated']} channel credential(s) "
                "to channel keyring"
            )
        if migrated.get("failed"):
            logger.warning(
                "Channel credential migration skipped %s field(s); legacy fallback remains active",
                migrated["failed"],
            )
    except Exception as exc:
        logger.warning(
            "Channel credential migration skipped; legacy fallback remains active: %s",
            exc,
        )
    auto_start_channels = []
    with _startup_phase("channel_auto_start_plan"):
        for _ch in _ch_registry.all_channels():
            auto_start = bool(_ch_config.get(_ch.name, "auto_start", False))
            logger.info(
                "startup.channel.auto_start_config channel=%s enabled=%s",
                _ch.name,
                auto_start,
            )
            if auto_start:
                auto_start_channels.append(_ch)

    # Auto-start tunnel if it was enabled before restart
    _main_app_tunnel = _ch_config.get("tunnel", "tunnel_main_app", False)
    if isinstance(_main_app_tunnel, list) and _main_app_tunnel:
        _main_app_tunnel = _main_app_tunnel[0] is True
        _ch_config.set("tunnel", "tunnel_main_app", _main_app_tunnel)
    if _main_app_tunnel is True:
        _set("🌐 Starting remote access tunnel…")
        try:
            from row_bot.tunnel import tunnel_manager
            with _startup_phase("main_app_tunnel_autostart"):
                if tunnel_manager.is_available():
                    await asyncio.to_thread(tunnel_manager.start_tunnel, _APP_PORT, label="main_app")
                    _safe_console_print(f"[startup] ✅ Main-app tunnel auto-started on port {_APP_PORT}")
                else:
                    _status_code, status_detail = tunnel_manager.status()
                    warning = f"Tunnel auto-start skipped: {status_detail}"
                    logger.warning(warning)
                    _st.startup_warnings.append(f"⚠️ {warning}")
        except Exception as exc:
            _st.startup_warnings.append(f"⚠️ Tunnel failed to auto-start: {exc}")

    # ── Proactive OAuth token health check ───────────────────────────
    with _startup_phase("oauth_token_health_check"):
        await asyncio.to_thread(_check_oauth_tokens, _st)
    with _startup_phase("github_account_health_check"):
        await asyncio.to_thread(_check_github_account_health, _st)

    # Schedule periodic re-check every 6 hours
    try:
        from row_bot.tasks import _get_scheduler
        with _startup_phase("oauth_periodic_scheduler"):
            _sched = _get_scheduler()
            _sched.add_job(
                _periodic_oauth_check,
                trigger="interval",
                hours=6,
                id="oauth_token_health",
                replace_existing=True,
                coalesce=True,
                max_instances=1,
            )
        _safe_console_print("[startup] ⏱️ OAuth periodic check scheduled (every 6 h)")
    except Exception as exc:
        logger.warning("Could not schedule periodic OAuth check: %s", exc)

    try:
        from datetime import datetime, timedelta
        from row_bot.tasks import _get_scheduler
        from row_bot.threads import cleanup_old_checkpoints

        def _run_checkpoint_cleanup() -> None:
            try:
                from row_bot.memory_extraction import is_app_idle
                if not is_app_idle():
                    logger.info("Checkpoint cleanup deferred; app is active")
                    return
                cleanup_old_checkpoints()
            except Exception:
                logger.debug("Checkpoint cleanup failed", exc_info=True)

        with _startup_phase("checkpoint_cleanup_scheduler"):
            _sched = _get_scheduler()
            _sched.add_job(
                _run_checkpoint_cleanup,
                trigger="interval",
                hours=6,
                id="checkpoint_cleanup",
                replace_existing=True,
                coalesce=True,
                max_instances=1,
                next_run_time=datetime.now() + timedelta(minutes=10),
            )
        _safe_console_print("[startup] 🧹 Checkpoint cleanup scheduled (idle, every 6 h)")
    except Exception as exc:
        logger.warning("Could not schedule checkpoint cleanup: %s", exc)

    # PTY bridge is started lazily when the user first opens the
    # terminal panel (ui/terminal_widget._wire_pty).  This ensures the
    # initial shell prompt flows through the registered xterm.js
    # callback instead of being consumed before the UI connects.
    _safe_console_print("[startup] 💻 Terminal bridge deferred to first panel open")

    # ── Idle browser-tab eviction ────────────────────────────────────
    try:
        from row_bot.tasks import _get_scheduler
        def _evict_idle_browser_tabs() -> None:
            try:
                from row_bot.tools.browser_tool import get_session_manager as _get_bs_mgr

                closed = _get_bs_mgr().evict_idle(ttl_seconds=600.0)
                if closed:
                    logger.info("browser: evicted %d idle tab(s)", closed)
            except Exception:
                logger.debug("browser idle eviction failed", exc_info=True)

        with _startup_phase("browser_idle_eviction_scheduler"):
            _sched = _get_scheduler()
            _sched.add_job(
                _evict_idle_browser_tabs,
                trigger="interval",
                minutes=5,
                id="browser_idle_eviction",
                replace_existing=True,
                coalesce=True,
                max_instances=1,
            )
        _safe_console_print("[startup] ⏱️ Browser idle-tab eviction scheduled (every 5 min, 10 min TTL)")
    except Exception as exc:
        logger.warning("Could not schedule browser idle eviction: %s", exc)

    _set("✅ Ready")
    _st.startup_ready = True
    _schedule_background_task(
        _repair_orchestration_recovery_batch(),
        name="row-bot-orchestration-recovery",
    )
    _schedule_agent_graph_prewarm()
    _schedule_local_embedding_prewarm()
    _schedule_auto_start_channels(auto_start_channels, _st)
    _app_boot_event("startup_sequence_complete")
    logger.info("%s startup complete", APP_DISPLAY_NAME)


# ── Webhook API Route ────────────────────────────────────────────────────────


def _launcher_request_authorized(request: Request) -> bool:
    """Validate the ephemeral secret shared only with the owning launcher."""
    expected = os.environ.get(LAUNCH_SECRET_ENV, "")
    authorization = str(request.headers.get("authorization") or "")
    return bool(
        len(expected) >= 32
        and hmac.compare_digest(authorization, f"Bearer {expected}")
    )


async def _launcher_ping_handler(request: Request) -> JSONResponse:  # noqa: ARG001
    """Identify this process to the desktop launcher."""
    if not _launcher_request_authorized(request):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    global _FIRST_LAUNCHER_PING_LOGGED
    if not _FIRST_LAUNCHER_PING_LOGGED:
        _FIRST_LAUNCHER_PING_LOGGED = True
        _app_boot_event("first_launcher_ping", port=_APP_PORT)
    return JSONResponse({"app": APP_PING_ID, "version": _app_version, "port": _APP_PORT})


async def _startup_state_handler(request: Request) -> JSONResponse:  # noqa: ARG001
    """Expose startup state for the browser-side splash handoff."""
    if not _launcher_request_authorized(request):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    import row_bot.ui.state as _st

    return JSONResponse({
        "ready": bool(_st.startup_ready),
        "status": str(_st.startup_status or ""),
        "warnings": len(_st.startup_warnings),
    })


async def _health_handler(request: Request) -> JSONResponse:  # noqa: ARG001
    """Expose process liveness without provider, route, or user details."""
    return JSONResponse(
        {"ok": True, "status": "alive"},
        headers={"Cache-Control": "no-store"},
    )


async def _ready_handler(request: Request) -> JSONResponse:  # noqa: ARG001
    """Expose only whether application startup reached its ready state."""
    import row_bot.ui.state as _st

    ready = bool(_st.startup_ready)
    return JSONResponse(
        {"ok": ready, "status": "ready" if ready else "starting"},
        status_code=200 if ready else 503,
        headers={"Cache-Control": "no-store"},
    )


async def _webhook_handler(request: Request) -> JSONResponse:
    """Handle POST /api/webhook/{task_id} for webhook-triggered tasks."""
    task_id = request.path_params.get("task_id", "")
    secret = request.query_params.get("secret")
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    from row_bot.tasks import handle_webhook
    result = handle_webhook(task_id, secret=secret, payload=payload)
    status_code = 200 if result.get("status") == "ok" else 400
    return JSONResponse(result, status_code=status_code)


async def _client_error_handler(request: Request) -> JSONResponse:
    """Capture browser-side JavaScript errors for local diagnostics."""
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if isinstance(payload, dict) and payload.get("kind") == "activity":
        mark_user_activity(str(payload.get("event") or "client activity"))
    record_client_error(payload if isinstance(payload, dict) else {"payload": payload})
    return JSONResponse({"ok": True})


_shutdown_cleanup_started = False


async def _cleanup_runtime(reason: str = "shutdown") -> None:
    """Stop long-lived helpers before the process exits."""
    global _shutdown_cleanup_started
    if _shutdown_cleanup_started:
        return
    _shutdown_cleanup_started = True

    cleanup_started = time.perf_counter()
    stop_performance_monitor()
    mark_shutdown(reason)
    _safe_console_print(f"[shutdown] Cleaning up sessions ({reason})...")
    try:
        # Stop channels before tunnels so webhook/socket clients can close cleanly.
        for _ch in _ch_registry.all_channels():
            try:
                if _ch.is_running():
                    await asyncio.wait_for(_ch.stop(), timeout=10)
                    _safe_console_print(f"[shutdown] {_ch.display_name} channel stopped")
            except asyncio.TimeoutError:
                _safe_console_print(f"[shutdown] {_ch.display_name} channel cleanup timed out")
            except Exception as exc:
                _safe_console_print(f"[shutdown] {_ch.display_name} channel cleanup error: {exc}")
    except Exception as exc:
        _safe_console_print(f"[shutdown] Channel registry cleanup error: {exc}")
    try:
        from row_bot.tools.browser_tool import get_session_manager as _get_bsm
        _get_bsm().kill_all()
        _safe_console_print("[shutdown] Browser session closed")
    except Exception as exc:
        _safe_console_print(f"[shutdown] Browser cleanup error: {exc}")
    try:
        from row_bot.tools.shell_tool import get_session_manager as _get_ssm
        _get_ssm().kill_all()
        _safe_console_print("[shutdown] Shell sessions closed")
    except Exception as exc:
        _safe_console_print(f"[shutdown] Shell cleanup error: {exc}")
    try:
        from row_bot.terminal_bridge import TerminalBridge
        if TerminalBridge.has_instance():
            TerminalBridge.destroy()
            _safe_console_print("[shutdown] Terminal bridge destroyed")
    except Exception as exc:
        _safe_console_print(f"[shutdown] Terminal bridge cleanup error: {exc}")
    try:
        from row_bot.tunnel import tunnel_manager
        tunnel_manager.stop_all()
        _safe_console_print("[shutdown] Tunnels closed")
    except Exception as exc:
        _safe_console_print(f"[shutdown] Tunnel cleanup error: {exc}")
    try:
        from row_bot.mcp_client.runtime import shutdown as _mcp_shutdown
        _mcp_shutdown()
        _safe_console_print("[shutdown] MCP sessions closed")
    except Exception as exc:
        _safe_console_print(f"[shutdown] MCP cleanup error: {exc}")
    _safe_console_print(f"[shutdown] Done in {(time.perf_counter() - cleanup_started):.1f}s")


async def _launcher_shutdown_handler(request: Request) -> JSONResponse:
    """Local launcher hook used for tray quit and updater handoff."""
    if not _launcher_request_authorized(request):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)

    async def _shutdown_soon() -> None:
        await asyncio.sleep(0.1)
        await _cleanup_runtime("launcher")
        logger.info("Launcher shutdown complete; exiting process")
        os._exit(0)

    asyncio.create_task(_shutdown_soon())
    return JSONResponse({"ok": True})


app.add_route("/api/launcher-ping", _launcher_ping_handler, methods=["GET"])
app.add_route("/api/startup-state", _startup_state_handler, methods=["GET"])
app.add_route("/api/launcher-shutdown", _launcher_shutdown_handler, methods=["POST"])
app.add_route("/api/webhook/{task_id}", _webhook_handler, methods=["POST"])
app.add_route("/api/client-error", _client_error_handler, methods=["POST"])
app.add_route("/healthz", _health_handler, methods=["GET"])
app.add_route("/readyz", _ready_handler, methods=["GET"])

from row_bot.access.middleware import AccessMiddleware
from row_bot.access.config import AccessConfig, canonical_host
from row_bot.access.access_routes import (
    AccessRouteConfigStore,
    discover_private_lan_addresses,
)
from row_bot.access.routes import register_access_routes
from row_bot.access.tailscale import (
    TailscaleOwnershipStore,
    augment_access_config_for_owned_tailscale,
)
from row_bot.access.runtime_policy import RuntimeAccessPolicy
from row_bot.mobile.routes import register_mobile_routes

_access_route_store = AccessRouteConfigStore()
_access_route_config = _access_route_store.load_or_default()
_host_admission_managed_externally = "ROW_BOT_ALLOWED_HOSTS" in os.environ
_access_config = AccessConfig.from_env()
_access_config = augment_access_config_for_owned_tailscale(
    _access_config,
    ownership=TailscaleOwnershipStore().load(),
    app_port=get_app_port(),
)
if (
    not _host_admission_managed_externally
    and _access_route_config.lan_enabled
):
    from dataclasses import replace as _dataclass_replace

    _lan_hosts = tuple(
        canonical_host(address, allow_port=False)
        for address in discover_private_lan_addresses()
    )
    _access_config = _dataclass_replace(
        _access_config,
        allowed_hosts=tuple(dict.fromkeys((*_access_config.allowed_hosts, *_lan_hosts))),
    )
_access_registration = register_access_routes(app, config=_access_config)
_runtime_access_policy = RuntimeAccessPolicy(
    _access_registration.config,
    configured_origins=(
        ()
        if _host_admission_managed_externally
        else _access_route_config.configured_origins
    ),
)
from row_bot.tunnel import tunnel_manager as _managed_tunnel_manager

_managed_tunnel_manager.set_managed_origin_registrar(_runtime_access_policy)
register_mobile_routes(app)
app.add_middleware(
    AccessMiddleware,
    runtime_policy=_runtime_access_policy,
    session_authenticator=_access_registration.authenticator,
)


@app.on_shutdown
async def on_shutdown():
    try:
        from row_bot.computer_use.service import shutdown_computer_use

        shutdown_computer_use()
    except Exception:
        logger.debug("Computer Use shutdown failed", exc_info=True)
    await _cleanup_runtime()


# MAIN PAGE
# ═════════════════════════════════════════════════════════════════════════════

@ui.page("/")
async def index():
    from row_bot.access.request_context import AuthenticationKind
    from row_bot.access.service import SESSION_REFRESH_POLL_INTERVAL
    import row_bot.ui.state as _st
    from row_bot.ui.access_context import access_context_from_client

    ui.dark_mode(True)
    _docs_query = docs_capture_query_params(ui.context.client)
    _access_context = access_context_from_client(ui.context.client)
    _mobile_client = bool(
        _access_context and _access_context.presentation.value == "compact"
    )

    if (
        _access_context is not None
        and _access_context.authentication_kind is AuthenticationKind.SESSION
    ):
        _refresh_interval_ms = int(
            SESSION_REFRESH_POLL_INTERVAL.total_seconds() * 1000
        )
        ui.add_head_html(f"""
        <script>
        (() => {{
          if (window.__rowBotSessionRefreshInstalled) return;
          window.__rowBotSessionRefreshInstalled = true;
          let stopped = false;
          let interval = null;
          const stop = () => {{
            stopped = true;
            if (interval !== null) window.clearInterval(interval);
          }};
          const refresh = async () => {{
            if (stopped) return;
            try {{
              const response = await fetch('/api/access/session/refresh', {{
                method: 'POST',
                credentials: 'same-origin',
                cache: 'no-store',
              }});
              if (response.status === 401 || response.status === 403) stop();
            }} catch (_) {{
              // A network failure waits for the next bounded interval.
            }}
          }};
          const start = () => {{
            refresh();
            interval = window.setInterval(refresh, {_refresh_interval_ms});
          }};
          window.addEventListener('beforeunload', stop, {{once: true}});
          if (document.readyState === 'loading') {{
            document.addEventListener('DOMContentLoaded', start, {{once: true}});
          }} else {{
            start();
          }}
        }})();
        </script>
        """)

    # ── Global panel card style ──────────────────────────────────────────
    ui.add_head_html("""
    <style>
    :root {
        --row-bot-left-drawer-width: 280px;
        --row-bot-command-center-width: 440px;
    }
    .row-bot-main-shell {
        box-sizing: border-box;
        width: 100%;
        max-width: 100%;
        min-width: 0;
        overflow: hidden;
        padding-left: var(--row-bot-main-left-correction, 0px);
        padding-right: var(--row-bot-main-right-correction, 0px);
        transition: padding-left 120ms ease, padding-right 120ms ease;
    }
    .row-bot-main-card {
        box-sizing: border-box;
    }
    .row-bot-panel-card {
        border: 1px solid rgba(255,255,255,0.07) !important;
        box-shadow: 4px 0 16px rgba(0,0,0,0.45),
                    -4px 0 16px rgba(0,0,0,0.45),
                    0 4px 12px rgba(0,0,0,0.35) !important;
    }
    .row-bot-inner-panel {
        background: linear-gradient(
            180deg,
            rgba(255,255,255,0.05) 0%,
            rgba(255,255,255,0.015) 100%
        );
        border: 1px solid rgba(255,255,255,0.09);
        border-top-color: rgba(255,255,255,0.14);
        border-bottom-color: rgba(0,0,0,0.15);
        border-radius: 10px;
        padding: 8px 10px;
        box-shadow: inset 0 1px 0 rgba(255,255,255,0.07),
                    inset 0 -1px 0 rgba(0,0,0,0.12),
                    0 3px 10px rgba(0,0,0,0.35),
                    0 1px 3px rgba(0,0,0,0.2);
    }
    @media (max-width: 900px) {
        .row-bot-main-shell {
            width: 100%;
            max-width: 100%;
        }
    }
    </style>
    <script>
    (() => {
      if (window.__rowBotDrawerOverlapGuardInstalled) return;
      window.__rowBotDrawerOverlapGuardInstalled = true;

      const visibleRect = (element) => {
        if (!element) return null;
        const rect = element.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0) return null;
        const style = window.getComputedStyle(element);
        if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity || '1') === 0) return null;
        return rect;
      };

      const drawerRoot = (selector, fallbackSelector) => {
        const marker = document.querySelector(selector);
        if (marker) {
          return marker.closest('.q-drawer') || marker;
        }
        return document.querySelector(fallbackSelector);
      };

      const apply = () => {
        const shell = document.querySelector('.row-bot-main-shell');
        if (!shell) return;
        const shellRect = shell.getBoundingClientRect();
        const leftRect = visibleRect(drawerRoot('[data-row-bot-left-drawer="1"]', '.q-drawer--left'));
        const rightRect = visibleRect(drawerRoot('[data-workflow-console-drawer="1"]', '.q-drawer--right'));
        const leftOverlap = leftRect ? Math.max(0, Math.ceil(leftRect.right - shellRect.left)) : 0;
        const rightOverlap = rightRect ? Math.max(0, Math.ceil(shellRect.right - rightRect.left)) : 0;
        shell.style.setProperty('--row-bot-main-left-correction', `${leftOverlap}px`);
        shell.style.setProperty('--row-bot-main-right-correction', `${rightOverlap}px`);
      };

      const schedule = () => {
        window.requestAnimationFrame(apply);
        window.setTimeout(apply, 75);
        window.setTimeout(apply, 250);
      };

      window.__rowBotApplyDrawerOverlapGuard = schedule;
      window.addEventListener('resize', schedule, {passive: true});
      window.addEventListener('orientationchange', schedule, {passive: true});
      document.addEventListener('visibilitychange', schedule);
      if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', schedule, {once: true});
      } else {
        schedule();
      }
    })();
    </script>
    """)
    _docs_capture_css = docs_capture_reduce_motion_css()
    if _docs_capture_css:
        ui.add_head_html(_docs_capture_css)
    _docs_capture_bootstrap = docs_capture_bootstrap_html()
    if _docs_capture_bootstrap:
        ui.add_body_html(_docs_capture_bootstrap)

    # ── Startup splash (poll until backend is ready) ─────────────────────
    if not _st.startup_ready:
        with ui.column().classes("absolute-center items-center gap-4"):
            ui.image("/static/row_bot_glyph_256.png").style("width: 144px; height: 144px; object-fit: contain;")
            ui.label(APP_DISPLAY_NAME).style(
                f"font-size: 1.6rem; font-weight: 700; letter-spacing: 0.1em; color: {APP_BRAND_ACCENT};"
            )
            status_label = ui.label(_st.startup_status).classes("text-grey-5 text-sm")
            ui.spinner("dots", size="1.5rem", color="grey-6")

        def _poll_ready():
            status_label.text = _st.startup_status
            status_label.update()
            if _st.startup_ready:
                _poll_timer.deactivate()
                ui.run_javascript("window.location.reload()")

        ui.run_javascript("""
        (() => {
          if (window.__rowBotStartupPollInstalled) return;
          window.__rowBotStartupPollInstalled = true;
          const poll = async () => {
            try {
              const response = await fetch('/readyz', {cache: 'no-store'});
              if (response.ok) {
                window.location.reload();
              }
            } catch (_) {}
          };
          const interval = setInterval(poll, 750);
          window.addEventListener('beforeunload', () => clearInterval(interval), {once: true});
          poll();
        })();
        """)

        _poll_timer = safe_timer(0.3, _poll_ready)
        return

    # ── Startup warnings ─────────────────────────────────────────────────
    if _st.startup_warnings:
        for msg in _st.startup_warnings:
            logger.warning("Startup warning shown to user: %s", msg)
            ui.notify(msg, type="warning", timeout=8000, close_button=True)
        _st.startup_warnings.clear()

    # ── Head HTML (styles, highlight.js, vis-network) ────────────────────
    inject_head_html()

    # ── Per-client element holder ────────────────────────────────────────
    p = P()
    p.pending_files = []

    # Pre-create dialogs (modules call .clear() + .open() on these)
    p.settings_dlg = ui.dialog().props("maximized transition-show=fade transition-hide=fade data-docs-id=settings-dialog")
    p.export_dlg = ui.dialog().props("data-docs-id=export-dialog")
    p.task_dlg = ui.dialog().props("persistent data-docs-id=workflow-editor")

    # ── Health check ─────────────────────────────────────────────────────
    def _run_health_check() -> tuple[bool, str]:
        current = get_current_model()
        if is_cloud_model(current):
            if not is_cloud_available():
                return False, "Provider model selected but no credential configured. Open Settings → Providers."
            return True, ""
        from row_bot.models import _ollama_reachable
        if not _ollama_reachable():
            return False, "Cannot connect to Ollama. Make sure it is running (`ollama serve`)."
        if not is_model_local(current):
            return False, f"Model {current} is not exposed by the Ollama daemon. Manage local models in Ollama, then refresh."
        return True, ""

    if is_docs_capture():
        health_result = (True, "")
    else:
        try:
            health_result = await run.io_bound(_run_health_check)
        except Exception as exc:
            logger.warning("Startup health check failed", exc_info=True)
            health_result = (False, str(exc) or "Startup health check failed.")
    if not isinstance(health_result, tuple) or len(health_result) != 2:
        logger.warning("Startup health check returned invalid result: %r", health_result)
        health_result = (True, "")
    ok, err = health_result
    if not ok and is_setup_complete():
        ui.notify(err, type="negative", timeout=0, close_button=True)

    # ── Setup wizard gate ────────────────────────────────────────────────
    _docs_force_setup_wizard = (
        is_docs_capture()
        and _docs_query.get("docs_surface") == "first-launch-setup-wizard"
    )
    if _docs_force_setup_wizard or not is_setup_complete():
        async def _on_wizard_finish():
            state.current_model = get_current_model()
            if getattr(state, "open_setup_center_on_next_load", False):
                ui.navigate.reload()
            else:
                ui.navigate.to("/")

        await show_setup_wizard(state, on_finish=_on_wizard_finish)
        return

    _docs_capture_intent = configure_docs_capture_state(
        state,
        _docs_query,
        load_messages=load_thread_messages,
    )

    # ── Build Callbacks bundle ───────────────────────────────────────────
    cb = Callbacks()
    p.streaming_callbacks = cb
    # Slots wired after layout is built (forward declarations)

    # ── Wrappers that close over (state, p, cb) ─────────────────────────
    def _open_settings(initial_tab: str = "Providers"):
        if _access_context is None or not _access_context.authenticated:
            ui.notify("Owner authentication is required.", type="warning")
            return
        open_settings(
            state,
            p,
            initial_tab,
            mobile=_mobile_client,
            runtime_access_policy=_runtime_access_policy,
        )

    def _open_export():
        open_export(state, p)

    def _send_message(
        text: str,
        voice_mode: bool = False,
        *,
        internal_goal_continuation: bool = False,
    ):
        if not internal_goal_continuation:
            mark_user_activity("send message")
        return send_message(
            text,
            state=state,
            p=p,
            cb=cb,
            voice_mode=voice_mode,
            internal_goal_continuation=internal_goal_continuation,
        )

    def _ask_parent_to_use_agent_result(run_id: str) -> None:
        prompt = agent_result_use_prompt(run_id)
        if not prompt:
            return
        try:
            ui.notify("Asking the parent to use that Agent result.", type="info", close_button=True)
            asyncio.create_task(_send_message(prompt))
        except Exception as exc:
            logger.debug("Could not ask parent to use Agent result", exc_info=True)
            ui.notify(f"Could not ask parent to use Agent result: {exc}", type="negative", close_button=True)

    async def _send_active_voice_message(text: str, *, voice_mode: bool = False):
        binding = getattr(p, "active_voice_binding", None)
        if binding is not None and binding.is_current(state.thread_id):
            return await binding.send_talk(text)
        surface = _active_voice_surface()
        if surface == "normal_chat":
            logger.info(
                "voice.realtime.pipeline %s",
                {
                    "stage": "active_voice_surface_fallback",
                    "surface": surface,
                    "thread_id": state.thread_id,
                    "text_chars": len(str(text or "")),
                    "voice_mode": True,
                },
            )
            return await _send_message(text, voice_mode=True)
        logger.info(
            "voice.realtime.pipeline %s",
            {
                "stage": "active_voice_surface_missing",
                "surface": surface,
                "thread_id": state.thread_id,
                "text_chars": len(str(text or "")),
                "voice_mode": voice_mode,
            },
        )
        return None

    def _active_voice_surface() -> str:
        binding = getattr(p, "active_voice_binding", None)
        if binding is not None and binding.is_current(state.thread_id):
            return str(binding.surface or "")
        if getattr(state, "active_developer_workspace_id", None):
            return "developer"
        if getattr(state, "active_designer_project", None):
            return "designer"
        return "normal_chat"

    from row_bot.voice.agent_bridge import VoiceAgentBridge
    _voice_bridge = VoiceAgentBridge(
        send_message=_send_active_voice_message,
        active_generation=lambda: _active_generations.get(state.thread_id),
        surface=_active_voice_surface,
        thread_id=lambda: state.thread_id or "",
    )

    def _show_task_dialog(task, on_done):
        show_task_dialog(task, on_done, state=state, p=p)

    # ══════════════════════════════════════════════════════════════════════
    # LAYOUT
    # ══════════════════════════════════════════════════════════════════════

    # ── Sidebar (left drawer) ────────────────────────────────────────────
    if _mobile_client:
        rebuild_thread_list = lambda: None
    else:
        rebuild_thread_list = build_sidebar(
            state, p,
            rebuild_main=lambda **kw: _rebuild_main(**kw),
            open_settings=_open_settings,
            load_thread_messages=load_thread_messages,
        )

    # ── Main content column ──────────────────────────────────────────────
    from row_bot.ui.terminal_widget import build_terminal_panel
    from row_bot.tools import registry as _tool_registry

    _main_shell_classes = "row-bot-main-shell row-bot-mobile-root" if _mobile_client else "row-bot-main-shell"
    _main_shell = ui.element("div").classes(_main_shell_classes).props("data-docs-id=app-main-shell")
    with _main_shell:
        if _mobile_client:
            _outer = ui.column().classes("row-bot-mobile-outer w-full no-wrap").style(
                "height: 100dvh; overflow: hidden; padding: 0; margin: 0;"
            )
        else:
            _outer = ui.column().classes(
                "w-full max-w-7xl mx-auto px-4 no-wrap row-bot-panel-card row-bot-main-card"
            ).style(
                "height: calc(100vh - 16px); overflow: hidden; padding-bottom: 12px;"
                " border-radius: 12px; margin-top: 8px;"
            )
        with _outer:
            p.main_col = ui.column().classes("w-full no-wrap flex-grow").props("data-docs-id=main-content").style(
                "overflow: hidden;"
            )
        # Terminal panel — inline, pushes chat content up when expanded
            if not _mobile_client:
                build_terminal_panel(p, state, _tool_registry)

    # ── Command Center (right drawer) ───────────────────────────────
    if not _mobile_client:
        build_command_center(
            state, p,
            rebuild_main=lambda **kw: _rebuild_main(**kw),
            rebuild_thread_list=rebuild_thread_list,
            show_task_dialog=_show_task_dialog,
            load_thread_messages=load_thread_messages,
            open_settings=_open_settings,
        )
        from row_bot.ui.buddy import build_in_app_buddy
        build_in_app_buddy()
    # Generation counter — every ``_rebuild_main`` bumps this. A
    # deferred hydration compares its captured id; if another rebuild
    # started in the meantime, the stale hydration aborts.
    _rebuild_gen = [0]

    def _load_active_context_projection() -> dict | None:
        """Load a current or explicitly last-measured display snapshot."""
        thread_id, model_ref = active_context_identity(state)
        if not thread_id or not model_ref:
            clear_context_usage_projection(state)
            return None
        try:
            from row_bot.threads import load_context_usage

            current = state.context_usage
            if (
                isinstance(current, dict)
                and state.context_usage_thread_id == thread_id
                and canonical_context_model_ref(state.context_usage_model_ref) == model_ref
                and canonical_context_model_ref(current.get("model_ref")) == model_ref
                and thread_id in _active_generations
            ):
                return current
            clear_context_usage_projection(state)
            usage = load_context_usage(
                thread_id,
                expected={"model_ref": model_ref},
                allow_stale=True,
            )
        except Exception:
            logger.debug("Context snapshot load failed", exc_info=True)
            usage = None
        if usage is not None:
            cache_and_project_context_usage(state, thread_id, usage)
        return state.context_usage

    def _rebuild_main(immediate: bool = False, reason: str = "unspecified") -> None:
        """Rebuild the main content column.

        By default paints a lightweight skeleton first and defers the
        real view build to the next tick so the browser can paint the
        skeleton frame — makes thread switches / home / gallery feel
        instant on slow views.

        Pass ``immediate=True`` when the caller needs ``p.chat_container``
        (or similar) to exist synchronously after the call returns (e.g.
        when creating a thread on first message send).
        """
        if p.main_col is None:
            return
        _load_active_context_projection()
        _started = time.perf_counter()
        # Designer needs full width; other views use centered max-w-7xl
        if _mobile_client:
            pass
        elif state.active_designer_project is not None or state.active_developer_workspace_id is not None:
            _outer.classes(remove="max-w-7xl mx-auto px-4", add="px-2")
        else:
            _outer.classes(remove="px-2", add="max-w-7xl mx-auto px-4")

        def _view_name() -> str:
            if _mobile_client:
                return "mobile"
            if state.active_designer_project is not None:
                return "designer"
            if state.active_developer_workspace_id is not None:
                return "developer"
            if state.thread_id is None:
                return "home"
            return "chat"

        def _build_standard_chat() -> None:
            build_chat(
                state, p,
                rebuild_main=_rebuild_main,
                rebuild_thread_list=rebuild_thread_list,
                send_message=_send_message,
                open_settings=_open_settings,
                open_export=_open_export,
                show_interrupt=cb.show_interrupt,
                add_chat_message=(
                    lambda msg, **kwargs: add_chat_message(
                        msg,
                        p,
                        state.thread_id,
                        **kwargs,
                    )
                ),
                browse_file=browse_file,
            )

        def _build_real() -> None:
            if p.main_col is None:
                return
            _real_started = time.perf_counter()
            try:
                p.main_col.props(f"data-docs-id={_view_name()}-surface")
            except Exception:
                logger.debug("Could not update docs capture surface selector", exc_info=True)
            with p.main_col:
                if _mobile_client:
                    build_mobile_shell(
                        state, p,
                        rebuild_main=_rebuild_main,
                        rebuild_thread_list=rebuild_thread_list,
                        send_message=_send_message,
                        show_task_dialog=_show_task_dialog,
                        load_thread_messages=load_thread_messages,
                        open_settings=_open_settings,
                        show_interrupt=cb.show_interrupt,
                        add_chat_message=lambda msg, **kwargs: add_chat_message(
                            msg,
                            p,
                            state.thread_id,
                            **kwargs,
                        ),
                    )
                elif state.active_designer_project is not None:
                    from row_bot.designer.editor import build_designer_editor

                    def _exit_designer():
                        from row_bot.ui.voice_lifecycle import stop_voice_for_thread_change

                        stop_voice_for_thread_change(state, p, reason="exit_designer")
                        state.active_designer_project = None
                        state.thread_id = None
                        state.thread_name = None
                        state.messages = []
                        state.preferred_home_tab = "Designer"
                        _rebuild_main()

                    build_designer_editor(
                        state.active_designer_project,
                        on_back=_exit_designer,
                        send_message=_send_message,
                        p=p,
                        state=state,
                        add_chat_message=lambda msg, **kwargs: add_chat_message(
                            msg,
                            p,
                            state.thread_id,
                            **kwargs,
                        ),
                        browse_file=browse_file,
                        open_settings=_open_settings,
                        rebuild_main=lambda **kw: _rebuild_main(**kw),
                        rebuild_thread_list=rebuild_thread_list,
                    )
                elif state.active_developer_workspace_id is not None:
                    from row_bot.developer.ui import build_developer_workspace

                    def _exit_developer():
                        from row_bot.ui.voice_lifecycle import stop_voice_for_thread_change

                        stop_voice_for_thread_change(state, p, reason="exit_developer")
                        prev = state.thread_id
                        state.active_developer_workspace_id = None
                        state.thread_id = None
                        state.thread_name = None
                        state.messages = []
                        set_active_thread(None, previous_id=prev)
                        state.preferred_home_tab = "Developer"
                        _rebuild_main()

                    build_developer_workspace(
                        state.active_developer_workspace_id,
                        state=state,
                        p=p,
                        send_message=_send_message,
                        add_chat_message=lambda msg, **kwargs: add_chat_message(
                            msg,
                            p,
                            state.thread_id,
                            **kwargs,
                        ),
                        browse_file=browse_file,
                        open_settings=_open_settings,
                        show_interrupt=cb.show_interrupt,
                        on_back=_exit_developer,
                        rebuild_main=lambda **kw: _rebuild_main(**kw),
                        rebuild_thread_list=rebuild_thread_list,
                    )
                elif state.thread_id is None:
                    build_home(
                        state, p,
                        rebuild_main=_rebuild_main,
                        rebuild_thread_list=rebuild_thread_list,
                        send_message=_send_message,
                        show_task_dialog=_show_task_dialog,
                        build_graph_panel=build_graph_panel,
                        is_first_run=is_first_run,
                        mark_onboarding_seen=mark_onboarding_seen,
                        load_thread_messages=load_thread_messages,
                        open_settings=_open_settings,
                    )
                else:
                    _build_standard_chat()
            log_ui_perf(
                "app.main.rebuild.build",
                (time.perf_counter() - _real_started) * 1000.0,
                threshold_ms=500.0,
                reason=reason,
                view=_view_name(),
                immediate=immediate,
            )

        # Immediate path — build real view synchronously, no skeleton.
        if immediate:
            _rebuild_gen[0] += 1
            p.main_col.clear()
            _build_real()
            log_ui_perf(
                "app.main.rebuild.immediate",
                (time.perf_counter() - _started) * 1000.0,
                threshold_ms=500.0,
                reason=reason,
                view=_view_name(),
                generation=_rebuild_gen[0],
            )
            return

        # ── Phase 1: paint a skeleton IMMEDIATELY (same tick) ────────
        # Gives instant visual feedback. The real view is built in a
        # deferred timer so the browser can render this paint first.
        _rebuild_gen[0] += 1
        _my_gen = _rebuild_gen[0]
        from row_bot.ui.skeleton import (
            show_gallery_skeleton,
            show_chat_skeleton,
            show_home_skeleton,
            show_generic_skeleton,
        )
        p.main_col.clear()
        with p.main_col:
            if _mobile_client:
                show_generic_skeleton()
            elif state.active_designer_project is not None:
                show_generic_skeleton()
            elif state.active_developer_workspace_id is not None:
                show_generic_skeleton()
            elif state.thread_id is None:
                # Home view — pick based on preferred tab
                if getattr(state, "preferred_home_tab", None) == "Designer":
                    show_gallery_skeleton()
                else:
                    show_home_skeleton()
            else:
                show_chat_skeleton()
        log_ui_perf(
            "app.main.rebuild.skeleton",
            (time.perf_counter() - _started) * 1000.0,
            threshold_ms=200.0,
            reason=reason,
            view=_view_name(),
            generation=_my_gen,
        )

        # ── Phase 2: hydrate real view on next tick ──────────────────
        def _hydrate() -> None:
            _hydrate_started = time.perf_counter()
            # Stale — another _rebuild_main happened after us.
            if _my_gen != _rebuild_gen[0]:
                log_ui_perf(
                    "app.main.rebuild.hydrate.stale",
                    (time.perf_counter() - _hydrate_started) * 1000.0,
                    threshold_ms=200.0,
                    reason=reason,
                    view=_view_name(),
                    generation=_my_gen,
                )
                return
            if p.main_col is None:
                return
            try:
                p.main_col.clear()
                _build_real()
                log_ui_perf(
                    "app.main.rebuild.hydrate",
                    (time.perf_counter() - _hydrate_started) * 1000.0,
                    threshold_ms=500.0,
                    reason=reason,
                    view=_view_name(),
                    generation=_my_gen,
                )
            except Exception:
                logger.exception("_rebuild_main hydration failed")

        # 0.01 s is short enough to feel instant but long enough to let
        # the browser paint the skeleton frame. Use a deferred task
        # instead of a one-shot NiceGUI timer so a stale rebuild cannot
        # leave a timer bound to a deleted parent slot.
        defer_ui(_hydrate)

    # ── Interrupt dialog ─────────────────────────────────────────────────
    show_interrupt = build_interrupt_dialog(state, p, cb)

    # ── Wire callback bundle ─────────────────────────────────────────────
    cb.rebuild_main = _rebuild_main
    cb.rebuild_thread_list = rebuild_thread_list
    cb.show_interrupt = show_interrupt
    cb.update_token_counter = lambda: _update_token_counter()
    def _mark_chat_message_rendered(msg: dict) -> None:
        if p.transcript_thread_id != state.thread_id:
            return
        try:
            idx = len(state.messages) - 1
            if idx < 0:
                return
            key = message_key(idx, msg)
            if key not in p.transcript_rendered_keys:
                p.transcript_rendered_keys.append(key)
            p.transcript_total = len(state.messages)
            p.transcript_window_size = len(p.transcript_rendered_keys)
        except Exception:
            logger.debug("Transcript render-state mark failed", exc_info=True)

    def _add_chat_message_and_track(msg: dict) -> Any | None:
        message_row = add_chat_message(
            msg,
            p,
            state.thread_id,
            on_use_agent_result=_ask_parent_to_use_agent_result,
        )
        _mark_chat_message_rendered(msg)
        return message_row

    def _insert_chat_message_before_live_row(msg: dict, live_row: Any) -> None:
        if (
            p.chat_container is None
            or p.transcript_thread_id != state.thread_id
        ):
            return
        live_slot = getattr(live_row, "parent_slot", None)
        if (
            live_slot is None
            or getattr(live_slot, "parent", None) is not p.chat_container
        ):
            return
        live_children = list(getattr(live_slot, "children", []) or [])
        if live_row not in live_children:
            return
        message_row = add_chat_message(
            msg,
            p,
            state.thread_id,
            on_use_agent_result=_ask_parent_to_use_agent_result,
        )
        if message_row is None:
            return
        message_row.move(p.chat_container, live_children.index(live_row))
        all_keys = message_keys(state.messages)
        window_start = max(
            0,
            min(int(p.transcript_window_start or 0), len(all_keys)),
        )
        p.transcript_rendered_keys = all_keys[window_start:]
        p.transcript_total = len(state.messages)
        p.transcript_window_size = len(p.transcript_rendered_keys)

    cb.add_chat_message = _add_chat_message_and_track
    cb.mark_chat_message_rendered = _mark_chat_message_rendered
    cb.insert_chat_message_before_live_row = _insert_chat_message_before_live_row
    cb.render_text_with_embeds = render_text_with_embeds
    cb.refresh_parent_agent_strip = lambda: (
        p.refresh_parent_agent_strip()
        if callable(getattr(p, "refresh_parent_agent_strip", None))
        else None
    )
    cb.refresh_goal_strip = lambda: (
        p.refresh_goal_strip()
        if callable(getattr(p, "refresh_goal_strip", None))
        else None
    )
    cb.refresh_model_controls = lambda: (
        p.refresh_model_controls()
        if callable(getattr(p, "refresh_model_controls", None))
        else None
    )

    def _refresh_chat_messages() -> None:
        """Synchronize the active transcript without a full visible rebuild."""
        if p.chat_container is None:
            return
        started = time.perf_counter()
        all_keys = message_keys(state.messages)
        rendered_keys = list(p.transcript_rendered_keys or [])
        same_thread = p.transcript_thread_id == state.thread_id
        window_start = int(p.transcript_window_start or 0)

        def _scroll_bottom() -> None:
            if p.chat_scroll:
                p.chat_scroll.scroll_to(percent=1.0)

        def _log_sync(mode: str, rows: int) -> None:
            try:
                from row_bot.ui.performance import log_ui_perf

                log_ui_perf(
                    "chat.transcript.sync",
                    (time.perf_counter() - started) * 1000.0,
                    mode=mode,
                    rows=rows,
                    total_rows=len(state.messages),
                    thread_id=state.thread_id,
                )
            except Exception:
                logger.debug("Transcript sync perf logging failed", exc_info=True)

        if (
            same_thread
            and rendered_keys
            and rendered_window_matches(rendered_keys, all_keys, start=window_start)
        ):
            rendered_end = window_start + len(rendered_keys)
            missing = list(enumerate(state.messages[rendered_end:], start=rendered_end))
            if not missing:
                _log_sync("noop", 0)
                return

            p.transcript_generation += 1
            sync_generation = p.transcript_generation
            chunk_state = {"idx": 0}

            def _append_chunk() -> None:
                if (
                    p.transcript_generation != sync_generation
                    or p.transcript_thread_id != state.thread_id
                ):
                    return
                start_idx = chunk_state["idx"]
                end_idx = start_idx
                chunk_started = time.perf_counter()
                try:
                    with p.chat_container:
                        while end_idx < len(missing):
                            msg_index, msg = missing[end_idx]
                            add_chat_message(
                                msg,
                                p,
                                state.thread_id,
                                on_use_agent_result=_ask_parent_to_use_agent_result,
                            )
                            p.transcript_rendered_keys.append(message_key(msg_index, msg))
                            end_idx += 1
                            if end_idx - start_idx >= TRANSCRIPT_MAX_CHUNK_MESSAGES:
                                break
                            elapsed_ms = (time.perf_counter() - chunk_started) * 1000.0
                            if elapsed_ms >= TRANSCRIPT_CHUNK_TARGET_MS:
                                break
                except Exception:
                    logger.debug("Transcript tail append failed", exc_info=True)
                    return
                chunk_state["idx"] = end_idx
                p.transcript_total = len(state.messages)
                p.transcript_window_size = len(p.transcript_rendered_keys)
                if end_idx < len(missing):
                    defer_ui(_append_chunk)
                else:
                    _scroll_bottom()
                    _log_sync("append_tail", len(missing))

            _append_chunk()
            return

        if same_thread and rendered_keys:
            desired_keys = all_keys[window_start:]
            prefix_count = common_key_prefix(rendered_keys, desired_keys)
            slot = getattr(p.chat_container, "default_slot", None)
            children = list(getattr(slot, "children", []) or [])
            if prefix_count > 0 and len(children) >= len(rendered_keys):
                try:
                    child_start, child_end = transcript_message_child_bounds(
                        len(children),
                        len(rendered_keys),
                        prefix_count,
                    )
                    for child in reversed(children[child_start:child_end]):
                        child.delete()
                    p.transcript_rendered_keys = rendered_keys[:prefix_count]
                    missing = list(
                        enumerate(
                            state.messages[window_start + prefix_count:],
                            start=window_start + prefix_count,
                        )
                    )
                    p.transcript_generation += 1
                    sync_generation = p.transcript_generation
                    chunk_state = {"idx": 0}

                    def _append_suffix_chunk() -> None:
                        if (
                            p.transcript_generation != sync_generation
                            or p.transcript_thread_id != state.thread_id
                        ):
                            return
                        start_idx = chunk_state["idx"]
                        end_idx = start_idx
                        chunk_started = time.perf_counter()
                        try:
                            with p.chat_container:
                                while end_idx < len(missing):
                                    msg_index, msg = missing[end_idx]
                                    add_chat_message(
                                        msg,
                                        p,
                                        state.thread_id,
                                        on_use_agent_result=_ask_parent_to_use_agent_result,
                                    )
                                    p.transcript_rendered_keys.append(
                                        message_key(msg_index, msg)
                                    )
                                    end_idx += 1
                                    if end_idx - start_idx >= TRANSCRIPT_MAX_CHUNK_MESSAGES:
                                        break
                                    elapsed_ms = (
                                        time.perf_counter() - chunk_started
                                    ) * 1000.0
                                    if elapsed_ms >= TRANSCRIPT_CHUNK_TARGET_MS:
                                        break
                        except Exception:
                            logger.debug(
                                "Transcript suffix reconcile failed",
                                exc_info=True,
                            )
                            return
                        chunk_state["idx"] = end_idx
                        p.transcript_total = len(state.messages)
                        p.transcript_window_size = len(
                            p.transcript_rendered_keys
                        )
                        if end_idx < len(missing):
                            defer_ui(_append_suffix_chunk)
                        else:
                            _scroll_bottom()
                            _log_sync("reconcile_suffix", len(missing))

                    _append_suffix_chunk()
                    return
                except Exception:
                    logger.debug(
                        "Transcript suffix pruning failed",
                        exc_info=True,
                    )

        # Rare fallback: the rendered transcript no longer matches state
        # (for example after a stale client handle or a cross-thread race).
        # Keep this scoped to the transcript and bounded to the latest window.
        p.transcript_generation += 1
        sync_generation = p.transcript_generation
        window = choose_transcript_window(len(state.messages))
        display_msgs = state.messages[window.start:window.end]
        display_keys = all_keys[window.start:window.end]
        p.transcript_thread_id = state.thread_id
        p.transcript_window_start = window.start
        p.transcript_window_size = window.visible_count
        p.transcript_total = window.total
        p.transcript_rendered_keys = []
        p.chat_container.clear()
        if not display_msgs:
            with p.chat_container:
                ui.label("Ask anything...").classes("text-grey-5 text-sm q-pa-md")
            _log_sync("empty", 0)
            return

        chunk_state = {"idx": 0}

        def _reconcile_chunk() -> None:
            if (
                p.transcript_generation != sync_generation
                or p.transcript_thread_id != state.thread_id
            ):
                return
            start_idx = chunk_state["idx"]
            end_idx = start_idx
            chunk_started = time.perf_counter()
            try:
                with p.chat_container:
                    while end_idx < len(display_msgs):
                        add_chat_message(
                            display_msgs[end_idx],
                            p,
                            state.thread_id,
                            on_use_agent_result=_ask_parent_to_use_agent_result,
                        )
                        p.transcript_rendered_keys.append(display_keys[end_idx])
                        end_idx += 1
                        if end_idx - start_idx >= TRANSCRIPT_MAX_CHUNK_MESSAGES:
                            break
                        elapsed_ms = (time.perf_counter() - chunk_started) * 1000.0
                        if elapsed_ms >= TRANSCRIPT_CHUNK_TARGET_MS:
                            break
            except Exception:
                logger.debug("Transcript reconcile failed", exc_info=True)
                return
            chunk_state["idx"] = end_idx
            if end_idx < len(display_msgs):
                defer_ui(_reconcile_chunk)
            else:
                _scroll_bottom()
                _log_sync("reconcile_window", len(display_msgs))

        defer_ui(_reconcile_chunk)

    cb.refresh_chat_messages = _refresh_chat_messages

    # ── Timers ───────────────────────────────────────────────────────────

    _last_agent_run_refresh = {
        "thread_id": "",
        "sidebar": "",
        "strip": "",
        "transcript": "",
    }
    _last_orchestration_transcript = {"thread_id": "", "key": ""}

    def _current_agent_run_refresh_key(tid: str) -> dict[str, str]:
        if not tid:
            return {"sidebar": "", "strip": "", "transcript": ""}
        try:
            from row_bot.agent_runs import list_agent_runs

            rows = list_agent_runs(parent_thread_id=tid, kind="subagent", limit=12)
        except Exception:
            logger.debug("Agent run page refresh poll failed", exc_info=True)
            rows = []
        checkpoint_revision = ""
        try:
            from row_bot.threads import get_latest_checkpoint_revision

            checkpoint_revision = get_latest_checkpoint_revision(tid)
        except Exception:
            logger.debug("Checkpoint revision refresh poll failed", exc_info=True)
        orchestration_rows: list[dict] = []
        orchestration_messages: list[dict] = []
        activity: dict[str, Any] = {}
        try:
            from row_bot.agent_orchestrator import (
                get_thread_orchestration_activity,
                list_messages,
                list_orchestrations,
            )

            activity = get_thread_orchestration_activity([tid]).get(tid, {})
            orchestration_rows = list_orchestrations(parent_thread_id=tid, limit=5)
            for orchestration in orchestration_rows:
                for message in list_messages(str(orchestration.get("id") or "")):
                    if str(message.get("kind") or "") not in {
                        "acknowledgement",
                        "final",
                        "parent_progress",
                        "parent_final",
                    }:
                        continue
                    orchestration_messages.append(message)
        except Exception:
            logger.debug("Orchestration page refresh poll failed", exc_info=True)
        return _agent_poll_refresh_keys(
            run_rows=rows,
            orchestration_rows=orchestration_rows,
            orchestration_messages=orchestration_messages,
            activity=activity,
            checkpoint_revision=checkpoint_revision,
        )

    def _reload_completed_orchestration_transcript(tid: str) -> str:
        """Merge the latest durable parent output after a detached wake."""

        try:
            from row_bot.agent_orchestrator import (
                list_messages,
                list_orchestrations,
                record_message,
            )
            from row_bot.ui.helpers import load_thread_messages
            from row_bot.threads import get_latest_checkpoint_revision

            rows = list_orchestrations(parent_thread_id=tid, limit=1)
            if not rows:
                return "processed_no_change"
            orchestration = rows[0]
            status = str(orchestration.get("status") or "")
            output_messages = list_messages(
                str(orchestration.get("id") or ""),
                kinds=["final", "parent_progress", "parent_final"],
            )
            latest_output = output_messages[-1] if output_messages else {}
            output_key = str(latest_output.get("id") or "")
            output_kind = str(latest_output.get("kind") or "").removeprefix("parent_")
            loaded_messages = load_thread_messages(tid)
            orchestration_messages = match_durable_orchestration_outputs(
                loaded_messages,
                output_messages,
                orchestration_id=str(orchestration.get("id") or ""),
            )
            checkpoint_keys = [
                str(
                    msg.get("channel_notification_key")
                    or msg.get("approval_request_id")
                    or msg.get("durable_key")
                    or ""
                )
                for msg in orchestration_messages
            ]
            if latest_output and not orchestration_messages:
                return "retry"
            if latest_output and output_key not in checkpoint_keys:
                return "retry"
            refresh_key = "|".join(
                [
                    status,
                    output_key,
                    get_latest_checkpoint_revision(tid),
                    *[key for key in checkpoint_keys if key],
                ]
            )
            if (
                _last_orchestration_transcript.get("thread_id") == tid
                and _last_orchestration_transcript.get("key") == refresh_key
            ):
                return "processed_no_change"
            transcript_changed = False
            for incoming_output in orchestration_messages:
                changed, index = upsert_durable_transcript_message(
                    state.messages,
                    incoming_output,
                )
                if index < 0:
                    return "retry"
                transcript_changed = transcript_changed or changed
            if transcript_changed:
                state.cache_active_messages()
            _last_orchestration_transcript["thread_id"] = tid
            _last_orchestration_transcript["key"] = refresh_key
            delivery_context = orchestration.get("delivery_context_json") or {}
            if (
                latest_output
                and output_kind == "final"
                and status in {"completed", "completed_partial", "failed", "stopped"}
                and delivery_context.get("voice_mode")
            ):
                spoken_rows = list_messages(
                    str(orchestration.get("id") or ""),
                    kinds=["voice_final"],
                )
                already_spoken = any(
                    str(row.get("delivery_status") or "") == "delivered"
                    for row in spoken_rows
                )
                if not already_spoken:
                    from row_bot.voice.output_controller import speak_orchestration_final

                    def _realtime_speaker(text: str, *, origin: str = "final") -> bool:
                        from row_bot.ui.streaming import run_realtime_client_js
                        if str(delivery_context.get("voice_transport") or "") == "browser":
                            from row_bot.voice.browser_client import speak_browser_voice_js

                            return run_realtime_client_js(
                                p,
                                speak_browser_voice_js(text),
                                context="orchestration_final_browser_voice",
                            )
                        from row_bot.voice.realtime_client import send_realtime_run_event_js

                        return run_realtime_client_js(
                            p,
                            send_realtime_run_event_js(
                                text,
                                origin=origin,
                                thread_id=tid,
                                generation_id=str(
                                    orchestration.get("parent_generation_id") or ""
                                ),
                            ),
                            context="orchestration_final_voice",
                        )

                    spoken = speak_orchestration_final(
                        delivery_context,
                        str(latest_output.get("content") or ""),
                        tts_service=state.tts_service,
                        realtime_speaker=_realtime_speaker,
                        now=time.perf_counter,
                    )
                    if spoken:
                        record_message(
                            str(orchestration.get("id") or ""),
                            kind="voice_final",
                            content=str(latest_output.get("content") or ""),
                            message_id=(
                                f"orchestration:{orchestration.get('id')}:voice_final"
                            ),
                            delivery_status="delivered",
                        )
            if transcript_changed:
                return "processed_changed"
            return "processed_no_change"
        except Exception:
            logger.debug("Completed orchestration transcript reload failed", exc_info=True)
            return "retry"

    def _current_child_agent_run_ids(tid: str) -> list[str]:
        if not tid:
            return []
        try:
            from row_bot.agent_runs import list_agent_runs

            return [
                str(row.get("id") or "").strip()
                for row in list_agent_runs(parent_thread_id=tid, kind="subagent", limit=12)
                if str(row.get("id") or "").strip()
            ]
        except Exception:
            logger.debug("Agent run id poll failed", exc_info=True)
            return []

    def _thread_has_live_generation(tid: str) -> bool:
        return _thread_has_attached_live_generation(tid)

    def _poll_agent_card_refresh() -> None:
        tid = str(state.thread_id or "")
        if not tid:
            _last_agent_run_refresh.update(
                {"thread_id": "", "sidebar": "", "strip": "", "transcript": ""}
            )
            return
        keys = _current_agent_run_refresh_key(tid)
        thread_changed = _last_agent_run_refresh.get("thread_id") != tid
        sidebar_changed = thread_changed or (
            keys["sidebar"] != _last_agent_run_refresh.get("sidebar")
        )
        strip_changed = thread_changed or (
            keys["strip"] != _last_agent_run_refresh.get("strip")
        )
        transcript_inspection_needed = thread_changed or (
            keys["transcript"] != _last_agent_run_refresh.get("transcript")
        )
        _last_agent_run_refresh.update({
            "thread_id": tid,
            "sidebar": keys["sidebar"],
            "strip": keys["strip"],
        })
        if sidebar_changed:
            try:
                rebuild_thread_list()
            except Exception:
                logger.debug("Agent activity sidebar refresh failed", exc_info=True)
        if strip_changed:
            try:
                if callable(getattr(p, "refresh_parent_agent_strip", None)):
                    p.refresh_parent_agent_strip()
            except Exception:
                logger.debug("Parent Agent strip poll refresh failed", exc_info=True)
        if not transcript_inspection_needed:
            return
        child_run_ids = _current_child_agent_run_ids(tid)
        live_generation = _thread_has_live_generation(tid)
        approval_changed = _sync_thread_approval_messages(
            state=state,
            cb=cb,
            thread_id=tid,
            run_ids=child_run_ids,
            refresh_transcript=live_generation,
        )
        if live_generation:
            return
        reload_result = _reload_completed_orchestration_transcript(tid)
        if reload_result != "retry":
            _last_agent_run_refresh["transcript"] = keys["transcript"]
        transcript_changed = reload_result == "processed_changed"
        completion_changed = False
        card_changed = False
        try:
            card_changed, _terminal = _update_direct_agent_refresh_keys(
                state.messages,
                child_run_ids,
            )
            completion_changed = _append_async_delegated_agent_completion_messages(
                state.messages,
                candidate_run_ids=child_run_ids,
                checkpoint_thread_id=tid,
            )
            if (
                transcript_changed
                or card_changed
                or approval_changed
                or completion_changed
            ):
                state.cache_active_messages()
        except Exception:
            logger.debug("Delegated Agent transcript repair failed", exc_info=True)
        visible_changed = (
            transcript_changed
            or card_changed
            or approval_changed
            or completion_changed
        )
        if (
            not visible_changed
            or p.chat_container is None
            or p.transcript_thread_id != tid
        ):
            return
        try:
            _refresh_chat_messages()
        except Exception:
            logger.debug("Agent card transcript poll refresh failed", exc_info=True)

    def _poll_notifications() -> None:
        for t in drain_toasts():
            _tkw = {"type": t.get("type", "info"), "close_button": True}
            if t.get("persistent"):
                _tkw["timeout"] = 0
            else:
                _tkw["timeout"] = 5000
            ui.notify(t["message"], **_tkw)
            rebuild_thread_list()

    _last_buddy_voice_state = [""]

    def _poll_voice() -> None:
        if not state.voice_enabled:
            if p.voice_status_label:
                p.voice_status_label.text = ""
            _last_buddy_voice_state[0] = ""
            return

        svc = state.voice_coordinator
        new_status = svc.get_status()
        st = svc.state
        if svc.transport == "realtime":
            if p.voice_status_label:
                if st == "connecting":
                    p.voice_status_label.text = "Connecting realtime Talk..."
                elif st in {"connected", "listening"}:
                    p.voice_status_label.text = "Realtime Talk - listening"
                elif st == "thinking":
                    p.voice_status_label.text = "Thinking..."
                elif st == "waiting_for_approval":
                    p.voice_status_label.text = "Waiting for approval..."
                elif st == "speaking":
                    p.voice_status_label.text = "Speaking..."
                elif st == "error":
                    p.voice_status_label.text = f"Realtime error: {new_status or 'connection failed'}"
                else:
                    p.voice_status_label.text = str(new_status or st)
            return
        if st != _last_buddy_voice_state[0]:
            _last_buddy_voice_state[0] = st
            try:
                from row_bot.buddy.events import BuddyEventType, emit_buddy_event
                if st in {"listening", "transcribing", "muted"}:
                    emit_buddy_event(
                        BuddyEventType.VOICE_LISTENING,
                        source="app.voice",
                        payload={"label": "Listening" if st == "listening" else "Voice active", "state": st},
                    )
            except Exception:
                logger.debug("Buddy voice event failed", exc_info=True)
        if p.voice_status_label:
            if st == "listening":
                p.voice_status_label.text = "🔴 Listening — speak now…"
            elif st == "transcribing":
                p.voice_status_label.text = "⏳ Processing…"
            elif st == "muted":
                tts = state.tts_service
                if tts and not tts.is_speaking:
                    svc.unmute()
                    p.voice_status_label.text = "🔴 Listening — speak now…"
                else:
                    p.voice_status_label.text = "🔇 Speaking…"
            elif st == "stopped":
                p.voice_status_label.text = f"⚫ {new_status or 'Stopped'}"

        if p.voice_status_label and state.voice_input_mode == "dictate":
            if st == "listening":
                p.voice_status_label.text = "Dictating - speak now..."
            elif st == "transcribing":
                p.voice_status_label.text = "Transcribing dictation..."
            elif st == "muted":
                p.voice_status_label.text = "Adding dictation..."
        elif p.voice_status_label and state.voice_input_mode == "talk":
            gen = _active_generations.get(state.thread_id)
            if gen:
                if gen.interrupt_data or state.pending_interrupt:
                    p.voice_status_label.text = "Waiting for approval..."
                elif gen.pending_tools:
                    tool_names = {str(tool.get("name") or "") for tool in gen.pending_tools.values() if isinstance(tool, dict)}
                    if any("browser" in name.lower() for name in tool_names):
                        p.voice_status_label.text = "Using Browser..."
                    else:
                        p.voice_status_label.text = "Using Tool..."
                elif state.tts_service and state.tts_service.is_speaking:
                    p.voice_status_label.text = "Speaking..."
                else:
                    p.voice_status_label.text = "Thinking..."

        text = svc.get_transcription()
        if text and state.voice_input_mode == "dictate":
            binding = getattr(p, "active_voice_binding", None)
            if binding is not None and binding.is_current(state.thread_id):
                binding.append_dictation(text)
            elif p.chat_input:
                from row_bot.voice.actions import append_dictation_text

                p.chat_input.value = append_dictation_text(str(p.chat_input.value or ""), text)
                p.chat_input.update()
            else:
                logger.info(
                    "voice.realtime.pipeline %s",
                    {
                        "stage": "dictation_dropped_stale_binding",
                        "thread_id": state.thread_id,
                        "text_chars": len(str(text or "")),
                    },
                )
                svc.unmute()
                return
            svc.unmute()
            if p.voice_status_label:
                p.voice_status_label.text = "Dictation added to composer"
        elif text:
            if state.tts_service and state.tts_service.enabled:
                state.tts_service.stop()
            if state.thread_id and should_auto_rename_thread(state.thread_id, state.thread_name):
                state.thread_name = rename_thread(
                    state.thread_id,
                    build_auto_thread_title(text, current_name=state.thread_name),
                    source="auto",
                )
                rebuild_thread_list()
            asyncio.create_task(_voice_bridge.submit_user_transcript(text))

    def _update_token_counter() -> None:
        """Render only a validated persisted/event snapshot; never recount on a timer."""
        usage = _load_active_context_projection()
        if p.context_meter:
            p.context_meter.update(
                usage,
                capacity_state=state.context_capacity_state,
                effective_limit_tokens=state.context_capacity_effective_tokens,
                has_history=context_history_present(state),
            )

    _notification_timer = safe_timer(1.0, _poll_notifications)
    _voice_timer = safe_timer(0.3, _poll_voice)
    _agent_card_timer = safe_timer(1.0, _poll_agent_card_refresh)
    deactivate_on_disconnect(_notification_timer, _voice_timer, _agent_card_timer)

    # ── Build initial view ───────────────────────────────────────────────
    _rebuild_main()
    if is_docs_capture() and _docs_capture_intent:
        _settings_tab = _docs_capture_intent.get("settings_tab")
        _dialog = _docs_capture_intent.get("dialog")
        if _settings_tab:
            defer_ui(lambda tab=_settings_tab: _open_settings(tab), delay=0.2)
        elif _dialog == "setup-center":
            def _open_docs_setup_center() -> None:
                from row_bot.ui.onboarding_center import show_setup_center

                show_setup_center(
                    open_settings=_open_settings,
                    rebuild_main=_rebuild_main,
                    state=state,
                )

            defer_ui(_open_docs_setup_center, delay=0.2)
        elif _dialog == "export":
            defer_ui(_open_export, delay=0.25)
        elif _dialog == "workflow-editor":
            defer_ui(lambda: _show_task_dialog(None, lambda: _rebuild_main()), delay=0.25)
        elif _dialog == "skills-hub":
            from row_bot.skills_hub.ui import open_skills_hub_dialog

            defer_ui(open_skills_hub_dialog, delay=0.25)
        elif _dialog == "plugin-marketplace":
            from row_bot.plugins.ui_marketplace import open_marketplace_dialog

            defer_ui(open_marketplace_dialog, delay=0.25)
        elif _dialog == "mcp-add-server":
            from row_bot.ui.mcp_settings import _open_server_dialog

            defer_ui(lambda: _open_server_dialog(lambda: None), delay=0.25)
        elif _dialog == "mcp-marketplace":
            from row_bot.ui.mcp_settings import _open_marketplace_dialog

            defer_ui(lambda: _open_marketplace_dialog(lambda: None), delay=0.25)
        elif _dialog == "approval":
            defer_ui(
                lambda: show_interrupt(
                    {
                        "description": "Write the reviewed launch summary to the isolated demo workspace.",
                        "target": "%ROW_BOT_DATA_DIR%/docs-demo-workspace/launch-summary.md",
                        "data_summary": "Five fictional checklist items; no account or customer data.",
                        "reversible": True,
                    }
                ),
                delay=0.25,
            )
    try:
        from row_bot.ui.onboarding_state import consume_setup_center_on_next_load

        should_open_setup_center = consume_setup_center_on_next_load()
    except Exception:
        logger.exception("Failed to consume setup center launch request")
        should_open_setup_center = False
    should_open_setup_center = bool(getattr(state, "open_setup_center_on_next_load", False)) or should_open_setup_center
    if should_open_setup_center:
        state.open_setup_center_on_next_load = False
        client = ui.context.client

        def _open_setup_center_after_first_run() -> None:
            from row_bot.ui.onboarding_center import show_setup_center

            with client:
                show_setup_center(
                    open_settings=_open_settings,
                    rebuild_main=_rebuild_main,
                    state=state,
                )

        defer_ui(_open_setup_center_after_first_run, delay=0.15)
    _update_token_counter()


@ui.page("/buddy-overlay")
async def buddy_overlay():
    ui.dark_mode(True)
    from row_bot.ui.buddy import build_buddy_overlay_page
    build_buddy_overlay_page()


# ═════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═════════════════════════════════════════════════════════════════════════════

if __name__ in {"__main__", "__mp_main__"}:
    _static_dir = str(static_dir())
    if os.path.isdir(_static_dir):
        app.add_static_files("/static", _static_dir)

    from row_bot.buddy.assets import buddy_static_dir

    _buddy_static_dir = buddy_static_dir()
    _buddy_static_dir.mkdir(parents=True, exist_ok=True)
    app.add_static_files("/_buddy", str(_buddy_static_dir))

    from row_bot.designer.publish import ensure_published_dir

    _published_dir = str(ensure_published_dir())
    app.add_static_files("/published", _published_dir)

    # Serve per-thread media files (generated videos, etc.)
    from row_bot.threads import _MEDIA_DIR
    app.add_static_files("/_media", str(_MEDIA_DIR))

    # Serve user-downloaded font cache
    _font_cache = str(get_row_bot_data_dir() / "font_cache")
    if os.path.isdir(_font_cache):
        app.add_static_files("/_fonts/cache", _font_cache)

    _native = "--native" in sys.argv
    _show = "--show" in sys.argv and not _native

    _run_kwargs = {
        "title": APP_DISPLAY_NAME,
        "host": _APP_HOST,
        "port": _APP_PORT,
        "dark": True,
        "favicon": Path(_static_dir) / "favicon.ico",
        "reload": False,
        "show": _show,
        "native": _native,
        "proxy_headers": False,
        "window_size": (1280, 900) if _native else None,
    }
    ui.run(**_run_kwargs)
