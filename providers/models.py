from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class AuthMethod(StrEnum):
    NONE = "none"
    API_KEY = "api_key"
    OAUTH_DEVICE = "oauth_device"
    OAUTH_PKCE = "oauth_pkce"
    EXTERNAL_CLI = "external_cli"
    CUSTOM = "custom"


class TransportMode(StrEnum):
    OPENAI_CHAT = "openai_chat"
    OPENAI_RESPONSES = "openai_responses"
    OLLAMA_CHAT = "ollama_chat"
    OLLAMA_CLOUD_CHAT = "ollama_cloud_chat"
    ANTHROPIC_MESSAGES = "anthropic_messages"
    GOOGLE_GENAI = "google_genai"
    GOOGLE_CLOUDCODE = "google_cloudcode"
    COPILOT = "copilot"
    EXTERNAL_PROCESS = "external_process"


class ProviderHealth(StrEnum):
    UNKNOWN = "unknown"
    CONNECTED = "connected"
    MISSING_AUTH = "missing_auth"
    DISABLED = "disabled"
    ERROR = "error"


class ModelModality(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"


class ModelTask(StrEnum):
    CHAT = "chat"
    RESPONSES = "responses"
    IMAGE_GENERATION = "image_generation"
    IMAGE_EDIT = "image_edit"
    VIDEO_GENERATION = "video_generation"
    EMBEDDING = "embedding"
    TRANSCRIPTION = "transcription"
    TTS = "tts"
    MODERATION = "moderation"
    REALTIME = "realtime"
    COMPUTER_USE = "computer_use"


@dataclass(frozen=True)
class ProviderDefinition:
    id: str
    display_name: str
    auth_methods: tuple[AuthMethod, ...]
    default_transport: TransportMode
    base_url: str = ""
    risk_label: str = "api_key"
    supports_catalog: bool = True
    experimental: bool = False
    icon: str = ""


@dataclass
class ProviderConnection:
    provider_id: str
    auth_method: AuthMethod = AuthMethod.NONE
    health: ProviderHealth = ProviderHealth.UNKNOWN
    configured: bool = False
    source: str = ""
    fingerprint: str = ""
    scopes: list[str] = field(default_factory=list)
    expires_at: str = ""
    external_reference_label: str = ""
    last_error: str = ""
    updated_at: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "auth_method": self.auth_method.value,
            "health": self.health.value,
            "configured": self.configured,
            "source": self.source,
            "fingerprint": self.fingerprint,
            "scopes": list(self.scopes),
            "expires_at": self.expires_at,
            "external_reference_label": self.external_reference_label,
            "last_error": self.last_error,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class ModelInfo:
    provider_id: str
    model_id: str
    display_name: str
    context_window: int
    transport: TransportMode
    capabilities: frozenset[str] = frozenset()
    input_modalities: frozenset[str] = frozenset((ModelModality.TEXT.value,))
    output_modalities: frozenset[str] = frozenset((ModelModality.TEXT.value,))
    tasks: frozenset[str] = frozenset((ModelTask.CHAT.value,))
    tool_calling: bool | None = None
    streaming: bool | None = None
    endpoint_compatibility: frozenset[TransportMode] = frozenset()
    billing_label: str = ""
    source_confidence: str = "inferred"
    last_verified_at: str = ""
    risk_label: str = "api_key"
    source: str = "catalog"

    @property
    def selection_ref(self) -> str:
        return f"model:{self.provider_id}:{self.model_id}"

    def capability_snapshot(self) -> dict[str, Any]:
        return {
            "capabilities": sorted(self.capabilities),
            "input_modalities": sorted(self.input_modalities),
            "output_modalities": sorted(self.output_modalities),
            "tasks": sorted(self.tasks),
            "tool_calling": self.tool_calling,
            "streaming": self.streaming,
            "endpoint_compatibility": [mode.value for mode in self.endpoint_compatibility],
            "transport": self.transport.value,
            "billing_label": self.billing_label,
            "source_confidence": self.source_confidence,
            "last_verified_at": self.last_verified_at,
        }


@dataclass
class RoutingProfile:
    id: str
    display_name: str
    description: str = ""
    enabled: bool = True
    primary: str = ""
    fallbacks: list[str] = field(default_factory=list)
    triggers: list[str] = field(default_factory=list)
    data_policy: str = "allow_api_key"
    max_fallbacks_per_turn: int = 1
    task_routes: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "description": self.description,
            "enabled": self.enabled,
            "primary": self.primary,
            "fallbacks": list(self.fallbacks),
            "triggers": list(self.triggers),
            "data_policy": self.data_policy,
            "max_fallbacks_per_turn": self.max_fallbacks_per_turn,
            "task_routes": dict(self.task_routes),
        }
