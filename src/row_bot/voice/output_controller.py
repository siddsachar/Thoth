from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Any, Callable

from row_bot.voice.cue_policy import VoiceCuePolicy
from row_bot.voice.cues import VoiceCue

logger = logging.getLogger(__name__)


@dataclass
class VoiceOutputController:
    mode: str
    policy: VoiceCuePolicy
    tts_service: Any = None
    realtime_speaker: Callable[..., bool | None] | None = None
    failed_cue_keys: set[tuple[str, str]] = field(default_factory=set)

    @classmethod
    def for_generation(
        cls,
        *,
        voice_mode: bool,
        transport: str,
        tts_service: Any,
        realtime_speaker: Callable[..., bool | None] | None,
        now: Callable[[], float],
    ) -> "VoiceOutputController":
        if not voice_mode:
            mode = "off"
        elif transport == "realtime":
            mode = "realtime"
        elif bool(getattr(tts_service, "enabled", False)):
            mode = "normal"
        else:
            mode = "off"
        return cls(
            mode=mode,
            policy=VoiceCuePolicy(mode=mode, now=now),
            tts_service=tts_service,
            realtime_speaker=realtime_speaker,
        )

    def speak_cue(self, cue: VoiceCue, *, generation_elapsed: float = 0.0, user_speaking: bool = False) -> bool:
        cue_key = (str(cue.type), str(cue.text))
        if cue_key in self.failed_cue_keys:
            return False
        if not self.policy.should_speak(
            cue,
            generation_elapsed=generation_elapsed,
            user_speaking=user_speaking,
        ):
            return False
        self.policy.record_spoken(cue)
        try:
            spoken = self._speak_cue_text(cue)
        except Exception:
            logger.warning("Voice cue speech failed", exc_info=True)
            self.failed_cue_keys.add(cue_key)
            return False
        if not spoken:
            self.failed_cue_keys.add(cue_key)
            return False
        return True

    def speak_final(self, text: str) -> bool:
        clean = str(text or "").strip()
        if not clean:
            return False
        try:
            return self._speak_final_text(clean)
        except Exception:
            logger.warning("Voice final speech failed", exc_info=True)
            return False

    def _speak_cue_text(self, cue: VoiceCue) -> bool:
        text = cue.text
        clean = str(text or "").strip()
        if not clean:
            return False
        if self.mode == "realtime":
            if not self.realtime_speaker:
                return False
            origin = str(getattr(cue.type, "value", cue.type))
            return self._call_realtime_speaker(clean, origin=origin) is not False
        if self.mode == "normal":
            if not self.tts_service:
                return False
            self.tts_service.speak_streaming(clean)
            return True
        return False

    def _speak_final_text(self, text: str) -> bool:
        clean = str(text or "").strip()
        if not clean:
            return False
        if self.mode == "realtime":
            if not self.realtime_speaker:
                return False
            return self._call_realtime_speaker(clean, origin="final") is not False
        if self.mode == "normal":
            if not self.tts_service:
                return False
            self.tts_service.flush_streaming(clean)
            return True
        return False

    def _call_realtime_speaker(self, text: str, *, origin: str) -> bool | None:
        if not self.realtime_speaker:
            return False
        try:
            return self.realtime_speaker(text, origin=origin)
        except TypeError:
            return self.realtime_speaker(text)


def speak_orchestration_final(
    delivery_context: dict[str, Any],
    text: str,
    *,
    tts_service: Any,
    realtime_speaker: Callable[..., bool | None] | None,
    now: Callable[[], float],
) -> bool:
    """Speak a detached orchestration final only for its originating voice turn."""

    if not bool((delivery_context or {}).get("voice_mode")):
        return False
    controller = VoiceOutputController.for_generation(
        voice_mode=True,
        transport=str((delivery_context or {}).get("voice_transport") or "normal"),
        tts_service=tts_service,
        realtime_speaker=realtime_speaker,
        now=now,
    )
    return controller.speak_final(text)
