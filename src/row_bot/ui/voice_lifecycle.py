from __future__ import annotations

import logging
from typing import Any

from row_bot.ui.state import AppState, P

logger = logging.getLogger(__name__)


def stop_voice_for_thread_change(state: AppState, p: P | None, *, reason: str = "thread_change") -> bool:
    """Stop active Talk/Dictate when navigation moves to another thread."""
    stopped = False
    try:
        if getattr(state, "tts_service", None):
            state.tts_service.stop()
    except Exception:
        logger.debug("TTS stop during thread change failed", exc_info=True)

    coordinator = getattr(state, "voice_coordinator", None)
    if coordinator and getattr(coordinator, "is_running", False):
        transport = getattr(coordinator, "transport", "")
        if transport == "realtime" and p is not None:
            try:
                from row_bot.ui.streaming import run_realtime_client_js
                from row_bot.voice.realtime_client import stop_realtime_client_js

                run_realtime_client_js(
                    p,
                    stop_realtime_client_js(),
                    context=f"stop_realtime_{reason}",
                )
            except Exception:
                logger.debug("Realtime browser stop during thread change failed", exc_info=True)
        elif transport == "browser" and p is not None:
            try:
                from row_bot.ui.streaming import run_realtime_client_js
                from row_bot.voice.browser_client import stop_browser_voice_js

                run_realtime_client_js(
                    p,
                    stop_browser_voice_js(cancel=True),
                    context=f"stop_browser_{reason}",
                )
            except Exception:
                logger.debug("Browser voice stop during thread change failed", exc_info=True)
        try:
            coordinator.stop()
        except Exception:
            logger.debug("Voice coordinator stop during thread change failed", exc_info=True)
        stopped = True

    if getattr(state, "voice_enabled", False):
        stopped = True
    state.voice_enabled = False
    state.voice_input_mode = "talk"

    if p is not None:
        binding = getattr(p, "active_voice_binding", None)
        if binding is not None:
            try:
                binding.clear()
            except Exception:
                logger.debug("Voice binding clear during thread change failed", exc_info=True)
        try:
            p.active_voice_binding = None
        except Exception:
            logger.debug("Voice binding reset during thread change failed", exc_info=True)

        switch = getattr(p, "voice_switch", None)
        if switch is not None:
            try:
                switch.value = False
                try:
                    switch.props("color=blue-grey-3 icon=record_voice_over")
                except Exception:
                    logger.debug("Voice button props update during thread change failed", exc_info=True)
                switch.update()
            except Exception:
                logger.debug("Voice button update during thread change failed", exc_info=True)

    if stopped:
        logger.info(
            "voice.realtime.pipeline %s",
            {
                "stage": "voice_stopped_for_thread_change",
                "reason": reason,
                "transport": getattr(coordinator, "transport", "") if coordinator else "",
            },
        )
    return stopped
