from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import logging
from typing import Any, Mapping

from row_bot.providers.selection import model_ref, parse_model_ref

logger = logging.getLogger(__name__)

PROVIDER_DEFAULT = "provider_default"
SELECTION_KINDS = frozenset({PROVIDER_DEFAULT, "effort", "on", "off", "budget"})
_EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
_EFFORT_LABELS = {"xhigh": "XHigh"}


@dataclass(frozen=True)
class ReasoningCapabilities:
    supported_efforts: tuple[str, ...] = ()
    default_effort: str = ""
    default_enabled: bool | None = None
    can_disable: bool = False
    mandatory: bool = False
    supports_budget: bool = False
    budget_min: int = 1
    budget_max: int = 0
    thinking_mode: str = "none"
    source: str = ""
    revision: str = ""
    request_style: str = ""

    def __post_init__(self) -> None:
        efforts = []
        for value in self.supported_efforts:
            effort = str(value or "").strip().lower()
            if effort and effort not in efforts:
                efforts.append(effort)
        efforts.sort(key=lambda value: (_EFFORT_ORDER.index(value) if value in _EFFORT_ORDER else 999, value))
        object.__setattr__(self, "supported_efforts", tuple(efforts))
        object.__setattr__(self, "default_effort", str(self.default_effort or "").strip().lower())
        object.__setattr__(self, "budget_min", max(0, int(self.budget_min or 0)))
        object.__setattr__(self, "budget_max", max(0, int(self.budget_max or 0)))
        object.__setattr__(self, "thinking_mode", str(self.thinking_mode or "none").strip().lower())
        object.__setattr__(self, "request_style", str(self.request_style or "").strip().lower())

    @property
    def controllable(self) -> bool:
        return bool(
            self.supported_efforts
            or self.supports_budget
            or self.thinking_mode == "toggle"
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "supported_efforts": list(self.supported_efforts),
            "default_effort": self.default_effort,
            "default_enabled": self.default_enabled,
            "can_disable": self.can_disable,
            "mandatory": self.mandatory,
            "supports_budget": self.supports_budget,
            "budget_min": self.budget_min,
            "budget_max": self.budget_max,
            "thinking_mode": self.thinking_mode,
            "source": self.source,
            "revision": self.revision,
            "request_style": self.request_style,
        }

    @classmethod
    def from_json(cls, value: Any) -> ReasoningCapabilities | None:
        if not isinstance(value, Mapping):
            return None
        efforts = value.get("supported_efforts") or value.get("supportedEfforts") or ()
        if isinstance(efforts, str):
            efforts = (efforts,)
        elif not isinstance(efforts, (list, tuple, set, frozenset)):
            efforts = ()
        supports_budget = value.get("supports_budget")
        if supports_budget is None:
            supports_budget = value.get("supports_max_tokens")
        mandatory = bool(value.get("mandatory"))
        can_disable = value.get("can_disable")
        if can_disable is None:
            can_disable = value.get("default_enabled") is not None and not mandatory
        default_enabled = value.get("default_enabled") if isinstance(value.get("default_enabled"), bool) else None
        inferred_mode = "manual" if supports_budget else "toggle" if default_enabled is not None and not efforts else "none"
        caps = cls(
            supported_efforts=tuple(str(item) for item in efforts if str(item)),
            default_effort=str(value.get("default_effort") or value.get("defaultEffort") or ""),
            default_enabled=default_enabled,
            can_disable=bool(can_disable),
            mandatory=mandatory,
            supports_budget=bool(supports_budget),
            budget_min=_positive_int(value.get("budget_min"), default=1, allow_zero=True),
            budget_max=_positive_int(value.get("budget_max") or value.get("max_tokens"), default=0, allow_zero=True),
            thinking_mode=str(value.get("thinking_mode") or inferred_mode),
            source=str(value.get("source") or "catalog_metadata"),
            revision=str(value.get("revision") or ""),
            request_style=str(value.get("request_style") or ""),
        )
        return caps if caps.controllable else None


@dataclass(frozen=True)
class ReasoningSelection:
    kind: str = PROVIDER_DEFAULT
    effort: str = ""
    budget: int = 0

    def __post_init__(self) -> None:
        kind = str(self.kind or PROVIDER_DEFAULT).strip().lower()
        if kind not in SELECTION_KINDS:
            raise ValueError(f"Unknown reasoning selection kind: {kind}")
        effort = str(self.effort or "").strip().lower()
        budget = int(self.budget or 0)
        if kind == "effort" and not effort:
            raise ValueError("An effort selection requires an effort value.")
        if kind == "budget" and budget <= 0:
            raise ValueError("A reasoning budget must be a positive integer.")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "effort", effort if kind == "effort" else "")
        object.__setattr__(self, "budget", budget if kind == "budget" else 0)

    @property
    def is_default(self) -> bool:
        return self.kind == PROVIDER_DEFAULT

    @property
    def label(self) -> str:
        if self.is_default:
            return "Provider default"
        if self.kind == "effort":
            return _EFFORT_LABELS.get(self.effort, self.effort.title())
        if self.kind == "budget":
            return f"Budget {self.budget}"
        return self.kind.title()

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"kind": self.kind}
        if self.kind == "effort":
            payload["effort"] = self.effort
        elif self.kind == "budget":
            payload["budget"] = self.budget
        return payload

    @classmethod
    def from_json(cls, value: Any) -> ReasoningSelection:
        if value in (None, "", PROVIDER_DEFAULT, "default", "auto"):
            return cls()
        if isinstance(value, str):
            raw = value.strip().lower()
            if raw in {"on", "off"}:
                return cls(kind=raw)
            return cls(kind="effort", effort=raw)
        if not isinstance(value, Mapping):
            raise ValueError("Reasoning selection must be an object.")
        return cls(
            kind=str(value.get("kind") or PROVIDER_DEFAULT),
            effort=str(value.get("effort") or ""),
            budget=int(value.get("budget") or 0),
        )


@dataclass(frozen=True)
class ReasoningRequestPlan:
    model_ref: str
    selection: ReasoningSelection = ReasoningSelection()
    capabilities: ReasoningCapabilities | None = None

    @property
    def is_default(self) -> bool:
        return self.selection.is_default

    @property
    def fingerprint(self) -> str:
        if self.is_default:
            return "reasoning:provider-default"
        raw = json.dumps(
            {"model_ref": self.model_ref, "selection": self.selection.to_json()},
            sort_keys=True,
            separators=(",", ":"),
        )
        return "reasoning:" + sha256(raw.encode("utf-8")).hexdigest()[:16]


def validate_reasoning_selection(
    selection: ReasoningSelection,
    capabilities: ReasoningCapabilities | None,
) -> ReasoningSelection:
    if selection.is_default:
        return selection
    if capabilities is None or not capabilities.controllable:
        raise ValueError("Reasoning controls are not available for this model.")
    if selection.kind == "effort":
        if selection.effort not in capabilities.supported_efforts:
            raise ValueError(f"Reasoning effort '{selection.effort}' is not supported by this model.")
    elif selection.kind == "off":
        if capabilities.mandatory or not capabilities.can_disable:
            raise ValueError("Reasoning cannot be disabled for this model.")
    elif selection.kind == "on":
        if capabilities.thinking_mode != "toggle":
            raise ValueError("This model does not expose an On/Off reasoning control.")
    elif selection.kind == "budget":
        if not capabilities.supports_budget:
            raise ValueError("This model does not support a reasoning budget.")
        if selection.budget < capabilities.budget_min:
            raise ValueError(f"Reasoning budget must be at least {capabilities.budget_min}.")
        if capabilities.budget_max and selection.budget > capabilities.budget_max:
            raise ValueError(f"Reasoning budget must be at most {capabilities.budget_max}.")
    return selection


def reasoning_choices(capabilities: ReasoningCapabilities | None) -> tuple[ReasoningSelection, ...]:
    if capabilities is None or not capabilities.controllable:
        return ()
    choices = [ReasoningSelection()]
    if capabilities.thinking_mode == "toggle":
        choices.append(ReasoningSelection(kind="on"))
    choices.extend(ReasoningSelection(kind="effort", effort=value) for value in capabilities.supported_efforts)
    if capabilities.can_disable and not capabilities.mandatory:
        choices.append(ReasoningSelection(kind="off"))
    return tuple(choices)


def resolve_reasoning_capabilities(provider_id: str, model_id: str) -> ReasoningCapabilities | None:
    provider = "ollama" if str(provider_id or "") == "local" else str(provider_id or "").strip()
    model = str(model_id or "").strip()
    if not provider or not model:
        return None
    try:
        from row_bot.providers.capability_resolution import resolve_capability_metadata

        resolved = resolve_capability_metadata(provider, model)
        live = ReasoningCapabilities.from_json(resolved.snapshot.get("reasoning"))
        if live:
            if not live.source:
                live = ReasoningCapabilities(**{**live.to_json(), "source": resolved.source})
            live = _with_default_request_style(live, provider)
            if provider.startswith("custom_openai_") or provider in {"requesty", "atlascloud", "minimax"}:
                return live if live.request_style else None
            return live
    except Exception:
        logger.debug("Reasoning capability cache lookup failed for %s/%s", provider, model, exc_info=True)
    fallback = _EXACT_REASONING_CAPABILITIES.get((provider, model))
    if fallback:
        return fallback
    if provider in {"opencode_zen", "opencode_go"}:
        return _opencode_fallback_capabilities(provider, model)
    if provider in {"ollama", "ollama_cloud"}:
        return _ollama_fallback_capabilities(model)
    return None


def reasoning_metadata_for_catalog(
    provider_id: str,
    model_id: str,
    metadata: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    raw = metadata or {}
    nested = raw.get("reasoning")
    caps = ReasoningCapabilities.from_json(nested)
    if caps:
        return _with_default_request_style(caps, provider_id).to_json()
    fallback = _EXACT_REASONING_CAPABILITIES.get((provider_id, model_id))
    if fallback:
        return fallback.to_json()
    if provider_id in {"opencode_zen", "opencode_go"}:
        fallback = _opencode_fallback_capabilities(provider_id, model_id)
    elif provider_id in {"ollama", "ollama_cloud"}:
        fallback = _ollama_fallback_capabilities(model_id)
    return fallback.to_json() if fallback else None


def resolve_reasoning_capabilities_for_ref(value: str | None) -> ReasoningCapabilities | None:
    parsed = parse_model_ref(str(value or ""))
    return resolve_reasoning_capabilities(*parsed) if parsed else None


def request_plan_for(
    thread_id: str | None,
    canonical_model_ref: str,
) -> ReasoningRequestPlan:
    caps = resolve_reasoning_capabilities_for_ref(canonical_model_ref)
    selection = ReasoningSelection()
    if thread_id and caps:
        try:
            from row_bot.threads import get_thread_reasoning_selection, set_thread_reasoning_selection

            saved = get_thread_reasoning_selection(thread_id, canonical_model_ref)
            if saved is not None:
                selection = ReasoningSelection.from_json(saved)
                try:
                    validate_reasoning_selection(selection, caps)
                except ValueError:
                    set_thread_reasoning_selection(thread_id, canonical_model_ref, None)
                    queue_reasoning_notice(
                        thread_id,
                        {
                            "kind": "stale_selection",
                            "model_ref": canonical_model_ref,
                            "message": "The saved reasoning setting is no longer supported; Provider default is active.",
                        },
                    )
                    selection = ReasoningSelection()
        except Exception:
            logger.warning("Could not load reasoning selection for thread %s", thread_id, exc_info=True)
    key = (str(thread_id or ""), canonical_model_ref)
    if key in _suppressed_overrides:
        selection = ReasoningSelection()
    return ReasoningRequestPlan(canonical_model_ref, selection, caps)


def canonical_reasoning_model_ref(provider_id: str, model_id: str) -> str:
    return model_ref(str(provider_id or "").strip(), str(model_id or "").strip())


def selection_from_command(argument: str) -> ReasoningSelection:
    parts = str(argument or "").strip().lower().split()
    if not parts:
        raise ValueError("missing")
    if parts[0] in {"default", "auto"} and len(parts) == 1:
        return ReasoningSelection()
    if parts[0] in {"on", "off"} and len(parts) == 1:
        return ReasoningSelection(kind=parts[0])
    if parts[0] == "budget" and len(parts) == 2:
        try:
            return ReasoningSelection(kind="budget", budget=int(parts[1]))
        except (TypeError, ValueError) as exc:
            raise ValueError("Reasoning budget must be a positive integer.") from exc
    if len(parts) == 1:
        return ReasoningSelection(kind="effort", effort=parts[0])
    raise ValueError("Use /reasoning default, an effort, on, off, or budget <tokens>.")


def format_reasoning_choices(capabilities: ReasoningCapabilities) -> str:
    values = ["default"]
    if capabilities.thinking_mode == "toggle":
        values.append("on")
    values.extend(capabilities.supported_efforts)
    if capabilities.can_disable and not capabilities.mandatory:
        values.append("off")
    if capabilities.supports_budget:
        values.append("budget <tokens>")
    return ", ".join(values)


def apply_reasoning_command(thread_id: str, canonical_model_ref: str, argument: str) -> str:
    caps = resolve_reasoning_capabilities_for_ref(canonical_model_ref)
    if caps is None or not caps.controllable:
        return f"Reasoning controls are not available for {canonical_model_ref}."
    from row_bot.threads import get_thread_reasoning_selection, set_thread_reasoning_selection

    if not str(argument or "").strip():
        saved = get_thread_reasoning_selection(thread_id, canonical_model_ref)
        try:
            selection = validate_reasoning_selection(ReasoningSelection.from_json(saved), caps)
        except ValueError:
            set_thread_reasoning_selection(thread_id, canonical_model_ref, None)
            selection = ReasoningSelection()
        return (
            f"Reasoning for {canonical_model_ref}: {selection.label}. "
            f"Valid choices: {format_reasoning_choices(caps)}."
        )
    try:
        selection = validate_reasoning_selection(selection_from_command(argument), caps)
    except ValueError as exc:
        return f"{exc} Valid choices for {canonical_model_ref}: {format_reasoning_choices(caps)}."
    set_thread_reasoning_selection(
        thread_id,
        canonical_model_ref,
        None if selection.is_default else selection.to_json(),
    )
    clear_reasoning_suppression(thread_id, canonical_model_ref)
    return f"Reasoning for {canonical_model_ref}: {selection.label}."


_reasoning_notices: dict[str, list[dict[str, Any]]] = {}
_suppressed_overrides: set[tuple[str, str]] = set()


def queue_reasoning_notice(thread_id: str | None, payload: Mapping[str, Any]) -> None:
    if not thread_id:
        return
    _reasoning_notices.setdefault(str(thread_id), []).append(dict(payload))


def consume_reasoning_notices(thread_id: str | None) -> list[dict[str, Any]]:
    if not thread_id:
        return []
    return _reasoning_notices.pop(str(thread_id), [])


def suppress_reasoning_override(thread_id: str | None, canonical_model_ref: str) -> None:
    _suppressed_overrides.add((str(thread_id or ""), canonical_model_ref))


def clear_reasoning_suppression(thread_id: str | None, canonical_model_ref: str) -> None:
    _suppressed_overrides.discard((str(thread_id or ""), canonical_model_ref))


def _positive_int(value: Any, *, default: int, allow_zero: bool = False) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    if parsed > 0 or (allow_zero and parsed == 0):
        return parsed
    return default


def _caps(
    efforts: tuple[str, ...] = (),
    *,
    style: str,
    can_disable: bool = False,
    mandatory: bool = False,
    thinking_mode: str = "none",
    supports_budget: bool = False,
    budget_min: int = 1,
    budget_max: int = 0,
) -> ReasoningCapabilities:
    return ReasoningCapabilities(
        supported_efforts=efforts,
        can_disable=can_disable,
        mandatory=mandatory,
        supports_budget=supports_budget,
        budget_min=budget_min,
        budget_max=budget_max,
        thinking_mode=thinking_mode,
        source="documented_exact_fallback",
        revision="2026-08-19",
        request_style=style,
    )


_OPENAI_EFFORTS = ("minimal", "low", "medium", "high")
_CODEX_EFFORTS = ("low", "medium", "high", "xhigh")
_ANTHROPIC_EFFORTS = ("low", "medium", "high", "max")

_EXACT_REASONING_CAPABILITIES: dict[tuple[str, str], ReasoningCapabilities] = {}

for _provider in ("openai",):
    for _model in ("gpt-5", "gpt-5-mini", "gpt-5-nano"):
        _EXACT_REASONING_CAPABILITIES[(_provider, _model)] = _caps(_OPENAI_EFFORTS, style="openai")
    for _model in ("o3", "o3-mini", "o4-mini"):
        _EXACT_REASONING_CAPABILITIES[(_provider, _model)] = _caps(("low", "medium", "high"), style="openai")

for _provider in ("codex",):
    for _model in (
        "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5", "gpt-5.4",
        "gpt-5.4-mini", "gpt-5.3-codex", "gpt-5.3-codex-spark", "gpt-5.2",
    ):
        _EXACT_REASONING_CAPABILITIES[(_provider, _model)] = _caps(_CODEX_EFFORTS, style="openai_responses")

for _provider in ("anthropic", "claude_subscription"):
    for _model in ("claude-sonnet-4-6", "claude-opus-4-8"):
        _EXACT_REASONING_CAPABILITIES[(_provider, _model)] = _caps(
            _ANTHROPIC_EFFORTS,
            style="anthropic",
            thinking_mode="adaptive",
        )

for _provider in ("xai", "xai_oauth"):
    for _model in ("grok-4.5",):
        _EXACT_REASONING_CAPABILITIES[(_provider, _model)] = _caps(("low", "medium", "high"), style="xai", mandatory=True)
    for _model in ("grok-4.6",):
        _EXACT_REASONING_CAPABILITIES[(_provider, _model)] = _caps(("low", "medium", "high", "xhigh"), style="xai", mandatory=True)
    for _model in ("grok-3-mini", "grok-3-mini-fast"):
        _EXACT_REASONING_CAPABILITIES[(_provider, _model)] = _caps(("low", "high"), style="xai", mandatory=True)

for _model, _minimum, _maximum, _disable in (
    ("gemini-2.5-pro", 128, 32768, False),
    ("gemini-2.5-flash", 0, 24576, True),
    ("gemini-2.5-flash-lite", 0, 24576, True),
):
    _EXACT_REASONING_CAPABILITIES[("google", _model)] = _caps(
        style="google_budget",
        can_disable=_disable,
        thinking_mode="manual",
        supports_budget=True,
        budget_min=_minimum,
        budget_max=_maximum,
    )
for _model in ("gemini-3-flash-preview", "gemini-3.1-pro-preview"):
    _EXACT_REASONING_CAPABILITIES[("google", _model)] = _caps(("low", "high"), style="google_level")


def _with_default_request_style(caps: ReasoningCapabilities, provider_id: str) -> ReasoningCapabilities:
    if caps.request_style:
        return caps
    style = {
        "openrouter": "openrouter",
        "openai": "openai",
        "codex": "openai_responses",
        "xai": "xai",
        "xai_oauth": "xai_responses",
        "anthropic": "anthropic",
        "claude_subscription": "anthropic",
        "google": "google_level",
        "ollama": "ollama",
        "ollama_cloud": "ollama",
    }.get(provider_id, "")
    return ReasoningCapabilities(**{**caps.to_json(), "request_style": style})


def _ollama_fallback_capabilities(model_id: str) -> ReasoningCapabilities | None:
    try:
        from row_bot.providers.ollama import is_ollama_reasoning_model, normalize_ollama_family

        family = normalize_ollama_family(model_id)
        if family == "gpt-oss":
            return _caps(("low", "medium", "high"), style="ollama", mandatory=True)
        if is_ollama_reasoning_model(model_id):
            return _caps(style="ollama", can_disable=True, thinking_mode="toggle")
    except Exception:
        return None
    return None


def _opencode_fallback_capabilities(provider_id: str, model_id: str) -> ReasoningCapabilities | None:
    try:
        from row_bot.providers.models import TransportMode
        from row_bot.providers.opencode import opencode_known_route

        route = opencode_known_route(provider_id, model_id)
        if route is None:
            return None
        if route.transport == TransportMode.OPENAI_RESPONSES:
            base = _EXACT_REASONING_CAPABILITIES.get(("codex", model_id)) or _EXACT_REASONING_CAPABILITIES.get(("openai", model_id))
        elif route.transport == TransportMode.ANTHROPIC_MESSAGES:
            base = _EXACT_REASONING_CAPABILITIES.get(("anthropic", model_id))
        else:
            base = None
        if base:
            return ReasoningCapabilities(**{**base.to_json(), "source": "opencode_exact_route"})
    except Exception:
        return None
    return None
