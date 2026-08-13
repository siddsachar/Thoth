from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from row_bot.voice.local_provider import (
    DEFAULT_FUNASR_MODEL,
    FUNASR_MODEL_LABEL,
    is_funasr_available,
)
from row_bot.voice.openai_realtime import (
    DEFAULT_REALTIME_MODEL,
    DEFAULT_REALTIME_VOICE,
    OpenAIRealtimeProvider,
)


VoiceCapability = Literal["talk", "dictation", "speech_output"]


@dataclass(frozen=True)
class VoiceModelOption:
    model_id: str
    label: str
    provider_id: str
    capability: VoiceCapability
    is_default: bool = False
    ready: bool = True
    reason: str = ""


@dataclass(frozen=True)
class VoiceProviderCapability:
    provider_id: str
    label: str
    ready: bool
    reason: str
    local: bool
    talk_models: tuple[VoiceModelOption, ...] = ()
    dictation_models: tuple[VoiceModelOption, ...] = ()
    speech_output_models: tuple[VoiceModelOption, ...] = ()
    default_talk_model: str = ""
    default_dictation_model: str = ""
    default_speech_output_model: str = ""
    default_voice: str = ""

    def models_for(self, capability: VoiceCapability) -> tuple[VoiceModelOption, ...]:
        if capability == "talk":
            return self.talk_models
        if capability == "dictation":
            return self.dictation_models
        return self.speech_output_models


def build_voice_provider_catalog(
    *,
    voice_service=None,
    tts_service=None,
) -> tuple[VoiceProviderCapability, ...]:
    local_whisper_size = str(getattr(voice_service, "whisper_size", "base") or "base")
    local_funasr_ready = is_funasr_available()
    local_funasr_reason = (
        f"Local FunASR transcription with {DEFAULT_FUNASR_MODEL}."
        if local_funasr_ready
        else "Install the voice extra with FunASR to use local SenseVoice transcription."
    )
    local_tts_ready = (
        bool(tts_service.is_installed()) if tts_service is not None else False
    )
    local_voice = str(getattr(tts_service, "voice", "af_heart") or "af_heart")
    local = VoiceProviderCapability(
        provider_id="local",
        label="Local",
        ready=True,
        reason="Local microphone transcription is available.",
        local=True,
        talk_models=(
            VoiceModelOption(
                model_id=f"local-whisper-{local_whisper_size}",
                label=f"Local Whisper {local_whisper_size}",
                provider_id="local",
                capability="talk",
                is_default=True,
                reason="Microphone transcription, then normal Row-Bot agent handling.",
            ),
            VoiceModelOption(
                model_id="local-funasr-sensevoice",
                label=FUNASR_MODEL_LABEL,
                provider_id="local",
                capability="talk",
                ready=local_funasr_ready,
                reason=local_funasr_reason,
            ),
        ),
        dictation_models=(
            VoiceModelOption(
                model_id=f"local-whisper-{local_whisper_size}",
                label=f"Local Whisper {local_whisper_size}",
                provider_id="local",
                capability="dictation",
                is_default=True,
                reason="Speech-to-text only; Send is still required.",
            ),
            VoiceModelOption(
                model_id="local-funasr-sensevoice",
                label=FUNASR_MODEL_LABEL,
                provider_id="local",
                capability="dictation",
                ready=local_funasr_ready,
                reason=local_funasr_reason,
            ),
        ),
        speech_output_models=(
            VoiceModelOption(
                model_id="local-kokoro",
                label="Local Kokoro",
                provider_id="local",
                capability="speech_output",
                is_default=True,
                ready=local_tts_ready,
                reason="Installed locally."
                if local_tts_ready
                else "Install Kokoro to use local speech output.",
            ),
        ),
        default_talk_model=f"local-whisper-{local_whisper_size}",
        default_dictation_model=f"local-whisper-{local_whisper_size}",
        default_speech_output_model="local-kokoro",
        default_voice=local_voice,
    )

    realtime_status = OpenAIRealtimeProvider().status()
    openai_realtime = VoiceProviderCapability(
        provider_id="openai_realtime",
        label="OpenAI Realtime",
        ready=realtime_status.ready,
        reason=realtime_status.reason,
        local=False,
        talk_models=(
            VoiceModelOption(
                model_id=DEFAULT_REALTIME_MODEL,
                label=f"{DEFAULT_REALTIME_MODEL} (default)",
                provider_id="openai_realtime",
                capability="talk",
                is_default=True,
                ready=realtime_status.ready,
                reason="Realtime voice-agent session routed through the Row-Bot consult/control bridge.",
            ),
        ),
        default_talk_model=DEFAULT_REALTIME_MODEL,
        default_voice=DEFAULT_REALTIME_VOICE,
    )
    return (local, openai_realtime)


def provider_options_for_capability(
    catalog: tuple[VoiceProviderCapability, ...],
    capability: VoiceCapability,
) -> dict[str, str]:
    return {
        provider.provider_id: provider.label
        for provider in catalog
        if provider.models_for(capability)
    }


def model_options_for_capability(
    catalog: tuple[VoiceProviderCapability, ...],
    capability: VoiceCapability,
    provider_id: str,
) -> dict[str, str]:
    for provider in catalog:
        if provider.provider_id == provider_id:
            return {
                model.model_id: model.label for model in provider.models_for(capability)
            }
    return {}


def selected_or_default_model(
    catalog: tuple[VoiceProviderCapability, ...],
    capability: VoiceCapability,
    provider_id: str,
    selected_model: str,
) -> str:
    options = model_options_for_capability(catalog, capability, provider_id)
    if selected_model in options:
        return selected_model
    for provider in catalog:
        if provider.provider_id != provider_id:
            continue
        if capability == "talk":
            return provider.default_talk_model
        if capability == "dictation":
            return provider.default_dictation_model
        return provider.default_speech_output_model
    return selected_model
