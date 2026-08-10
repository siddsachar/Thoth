"""Central registry for Row-Bot channel adapters."""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from row_bot.channels.base import Channel

log = logging.getLogger("row_bot.channels.registry")


@dataclass(frozen=True)
class ChannelSource:
    kind: str = "core"
    plugin_id: str = ""
    label: str = ""


_channels: dict[str, "Channel"] = {}
_channel_sources: dict[str, ChannelSource] = {}


def clear_agent_cache_if_loaded() -> None:
    """Invalidate agent graphs without importing the agent runtime at startup."""

    seen_modules: set[int] = set()
    for module_name in ("row_bot.agent", "agent"):
        agent_mod = sys.modules.get(module_name)
        if agent_mod is None or id(agent_mod) in seen_modules:
            continue
        seen_modules.add(id(agent_mod))
        clear_cache = getattr(agent_mod, "clear_agent_cache", None)
        if callable(clear_cache):
            try:
                clear_cache()
            except Exception:
                log.debug("Agent cache invalidation failed", exc_info=True)


def register(channel: "Channel", *, source: ChannelSource | None = None) -> None:
    """Register a channel adapter instance."""

    source = source or ChannelSource()
    existing_source = _channel_sources.get(channel.name)
    if existing_source and existing_source != source:
        raise ValueError(
            f"Channel '{channel.name}' is already registered by "
            f"{existing_source.kind}:{existing_source.plugin_id or 'core'}"
        )
    _channels[channel.name] = channel
    _channel_sources[channel.name] = source
    log.debug("Registered channel: %s from %s", channel.name, source)
    clear_agent_cache_if_loaded()


def get(name: str) -> "Channel | None":
    """Return the channel with *name*, or ``None``."""

    return _channels.get(name)


def get_source(name: str) -> ChannelSource:
    """Return source metadata for a registered channel."""

    return _channel_sources.get(name, ChannelSource())


def allows_custom_ui(name: str) -> bool:
    """Return True when a channel may render custom settings UI."""

    return get_source(name).kind != "plugin"


def all_channels() -> list["Channel"]:
    """Return all registered channels in insertion order."""

    return list(_channels.values())


def running_channels() -> list["Channel"]:
    """Return only channels that are currently active."""

    return [channel for channel in _channels.values() if channel.is_running()]


def configured_channels() -> list["Channel"]:
    """Return only channels that have valid configuration."""

    return [channel for channel in _channels.values() if channel.is_configured()]


def unregister(name: str) -> None:
    """Remove a channel registration."""

    existed = name in _channels or name in _channel_sources
    _channels.pop(name, None)
    _channel_sources.pop(name, None)
    if existed:
        clear_agent_cache_if_loaded()


def unregister_plugin_channels(plugin_id: str) -> None:
    """Remove all channels owned by a plugin."""

    for name, source in list(_channel_sources.items()):
        if source.kind == "plugin" and source.plugin_id == plugin_id:
            unregister(name)


def deliver(channel_name: str, target: str | int, text: str) -> tuple[str, str]:
    """Deliver a message via *channel_name*."""

    channel = _channels.get(channel_name)
    if channel is None:
        return "delivery_failed", f"Unknown channel: {channel_name}"
    if not channel.is_running():
        return "delivery_failed", f"{channel.display_name} is not running"
    try:
        channel.send_message(target, text)
        return "delivered", f"Delivered via {channel.display_name}"
    except Exception as exc:
        log.warning("Delivery via %s failed: %s", channel_name, exc)
        return "delivery_failed", f"{channel.display_name} delivery failed: {exc}"


def validate_delivery(channel_name: str | None, target: str | None) -> None:
    """Raise ``ValueError`` if delivery settings are invalid."""

    if not channel_name and not target:
        return

    if channel_name and not target and channel_name != "telegram":
        raise ValueError(
            f"delivery_channel is '{channel_name}' but delivery_target is empty."
        )
    if target and not channel_name:
        raise ValueError("delivery_target is set but delivery_channel is empty.")

    channel = _channels.get(channel_name)
    if channel is None:
        known = list(_channels.keys())
        raise ValueError(
            f"Unknown delivery channel '{channel_name}'. Registered: {known}"
        )

    if channel_name == "telegram":
        return
    if "@" in str(target or ""):
        return
    if target:
        return


def _reset() -> None:
    """Clear all channels (testing only)."""

    _channels.clear()
    _channel_sources.clear()
