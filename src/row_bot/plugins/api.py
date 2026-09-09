"""Plugin API and PluginTool base class.

This module defines the interface that plugin authors use — it is the ONLY
module plugins are allowed to import from Row-Bot core.  Everything else
(tools/, ui/, agent.py, etc.) is off-limits.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs

from langchain_core.tools import StructuredTool

from row_bot.channels.base import Channel, ChannelCapabilities, ConfigField

logger = logging.getLogger(__name__)

__all__ = [
    "Channel",
    "ChannelAttachment",
    "ChannelAttachmentResult",
    "ChannelCapabilities",
    "ChannelInboundMessage",
    "ChannelOutboundCallbacks",
    "ChannelRunResult",
    "ConfigField",
    "BotFrameworkAuthResult",
    "PluginAPI",
    "PluginTool",
    "PluginWebhookRequest",
    "PluginWebhookResponse",
    "verify_bot_framework_jwt",
]


@dataclass
class ChannelAttachment:
    """Public description of one inbound channel attachment."""

    id: str = ""
    filename: str = "attachment"
    content_type: str = ""
    size_bytes: int = 0
    data: bytes | None = None
    local_path: str = ""
    url: str = ""
    kind: str = "file"
    caption: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChannelInboundMessage:
    """Public message envelope passed from a plugin channel into Row-Bot."""

    channel_name: str
    external_conversation_id: str
    sender_id: str
    text: str = ""
    sender_display_name: str = ""
    platform_message_id: str = ""
    platform_thread_id: str = ""
    conversation_type: str = ""
    is_direct: bool = False
    is_mention: bool = False
    attachments: list[ChannelAttachment] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChannelOutboundCallbacks:
    """Platform-specific send/edit callbacks owned by a plugin channel."""

    send_text: Callable[[str], Awaitable[Any] | Any]
    send_typing: Callable[[], Awaitable[Any] | Any] | None = None
    start_stream: Callable[[str], Awaitable[Any] | Any] | None = None
    update_stream: Callable[[Any, str], Awaitable[Any] | Any] | None = None
    finish_stream: Callable[[Any, str], Awaitable[Any] | Any] | None = None
    send_photo: Callable[[str, str | None], Awaitable[Any] | Any] | None = None
    send_document: Callable[[str, str | None], Awaitable[Any] | Any] | None = None
    send_approval_request: (
        Callable[[Any, dict], Awaitable[str | None] | str | None] | None
    ) = None
    update_approval_message: Callable[[str, str, str], Awaitable[Any] | Any] | None = None


@dataclass
class ChannelRunResult:
    """Structured result returned after Row-Bot handles a plugin channel turn."""

    thread_id: str
    answer: str = ""
    handled: bool = False
    command: bool = False
    interrupted: bool = False
    interrupt_data: Any | None = None
    generated_files: list[str] = field(default_factory=list)
    error: str = ""


@dataclass
class ChannelAttachmentResult:
    """Text/file outcome from shared inbound attachment processing."""

    prompt_text: str = ""
    saved_path: str = ""
    workspace_path: str = ""
    content_type: str = ""
    kind: str = "file"
    error: str = ""


@dataclass
class PluginWebhookRequest:
    """Public request object for plugin webhook handlers."""

    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body: bytes
    client_host: str = ""

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8") if self.body else "{}")

    def form(self) -> dict[str, Any]:
        parsed = parse_qs(self.body.decode("utf-8", errors="replace"), keep_blank_values=True)
        return {
            key: values[0] if len(values) == 1 else values
            for key, values in parsed.items()
        }


@dataclass
class PluginWebhookResponse:
    """Public response object returned by plugin webhook handlers."""

    status_code: int = 200
    body: str | bytes = ""
    media_type: str = "text/plain"
    headers: dict[str, str] = field(default_factory=dict)


# ═════════════════════════════════════════════════════════════════════════════
# Bot Framework auth helpers
# ═════════════════════════════════════════════════════════════════════════════
@dataclass
class BotFrameworkAuthResult:
    """Result from verifying a Bot Framework Connector JWT."""

    ok: bool
    claims: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    status_code: int = 401
    service_url: str = ""
    key_id: str = ""


def verify_bot_framework_jwt(
    authorization_header: str,
    *,
    app_id: str,
    service_url: str = "",
    channel_id: str = "",
    required_endorsement: str = "",
    metadata_url: str = "",
    issuer: str = "",
    timeout_seconds: float = 5.0,
) -> BotFrameworkAuthResult:
    """Verify a Bot Framework Connector bearer token for plugin channels."""
    from row_bot.plugins.bot_framework_auth import verify_bot_framework_jwt as _verify

    return _verify(
        authorization_header,
        app_id=app_id,
        service_url=service_url,
        channel_id=channel_id,
        required_endorsement=required_endorsement,
        metadata_url=metadata_url,
        issuer=issuer,
        timeout_seconds=timeout_seconds,
    )


# PluginAPI - the object passed to register()
class PluginAPI:
    """Bridge between a plugin and Row-Bot's plugin infrastructure.

    An instance is created per-plugin and passed to the plugin's
    ``register(api)`` function.  The plugin uses it to:

    - Register tools and skills
    - Read/write its own configuration
    - Read its own secrets (API keys)
    - Get its plugin ID and data directory
    """

    def __init__(
        self,
        plugin_id: str,
        plugin_dir: "Any",       # pathlib.Path — avoid import for type stub
        state_backend: "Any",    # plugins.state module
        *,
        staged: bool = False,
    ):
        self._plugin_id = plugin_id
        self._plugin_dir = plugin_dir
        self._state = state_backend
        self._registered_tools: list[PluginTool] = []
        self._registered_skills: list[dict] = []
        self._registered_channels: list[Any] = []
        self._registered_webhooks: list[tuple] = []
        self._registration_lock = threading.RLock()
        self._registration_revoked = False
        self._registration_sealed = False
        self._staged = staged

    def _check_active(self, *, registration: bool = False) -> None:
        if self._registration_revoked or (registration and self._registration_sealed):
            raise RuntimeError("Plugin registration is no longer active")
        enabled = getattr(self._state, "is_plugin_enabled", None)
        if enabled is not None and not enabled(self._plugin_id):
            raise RuntimeError("Plugin is disabled")

    def _revoke(self) -> None:
        with self._registration_lock:
            self._registration_revoked = True
            self._registered_webhooks.clear()

    def _check_dispatch(self) -> None:
        with self._registration_lock:
            self._check_active()
            if self._staged:
                raise RuntimeError("Plugin registration is not published")

    def _publish_webhooks(self) -> None:
        from row_bot.plugins.webhooks import register_plugin_webhook

        with self._registration_lock:
            self._check_active()
            self._registration_sealed = True
            for name, handler, methods, maximum in self._registered_webhooks:
                register_plugin_webhook(self._plugin_id, name, handler,
                                        methods=methods, max_body_bytes=maximum)
            self._registered_webhooks.clear()
            self._staged = False

    @property
    def plugin_id(self) -> str:
        return self._plugin_id

    @property
    def plugin_dir(self) -> Any:
        """Path to the plugin's installation directory."""
        return self._plugin_dir

    # ── Tool & Skill Registration ────────────────────────────────────────
    def register_tool(self, tool: "PluginTool") -> None:
        """Register a tool with Row-Bot. Called inside register()."""
        with self._registration_lock:
            self._check_active(registration=True)
            self._registered_tools.append(tool)
        logger.debug("Plugin '%s' registered tool: %s", self._plugin_id, tool.name)

    def register_skill(self, skill_info: dict) -> None:
        """Register a skill dict with Row-Bot. Usually auto-discovered from skills/."""
        with self._registration_lock:
            self._check_active(registration=True)
            self._registered_skills.append(skill_info)
        logger.debug("Plugin '%s' registered skill: %s",
                      self._plugin_id, skill_info.get("name", "?"))

    def register_channel(self, channel: Any) -> None:
        """Register a Row-Bot channel adapter owned by this plugin."""
        with self._registration_lock:
            self._check_active(registration=True)
            self._registered_channels.append(channel)
        logger.debug(
            "Plugin '%s' registered channel: %s",
            self._plugin_id,
            getattr(channel, "name", "?"),
        )

    # ── Configuration ────────────────────────────────────────────────────
    def get_config(self, key: str, default: Any = None) -> Any:
        """Read a configuration value for this plugin."""
        self._check_active()
        return self._state.get_plugin_config(self._plugin_id, key, default)

    def set_config(self, key: str, value: Any) -> None:
        """Write a configuration value for this plugin."""
        self._check_active()
        self._state.set_plugin_config(self._plugin_id, key, value)

    # ── Secrets ──────────────────────────────────────────────────────────
    def get_secret(self, key: str) -> str | None:
        """Read a secret (API key) for this plugin."""
        self._check_active()
        return self._state.get_plugin_secret(self._plugin_id, key)

    def set_secret(self, key: str, value: str) -> None:
        """Write a secret (API key) for this plugin."""
        self._check_active()
        self._state.set_plugin_secret(self._plugin_id, key, value)

    async def handle_channel_message(
        self,
        message: ChannelInboundMessage,
        callbacks: ChannelOutboundCallbacks,
        *,
        channel: Channel | None = None,
        enabled_tool_names: list[str] | None = None,
        stream: bool | None = None,
        approval_context: dict[str, Any] | None = None,
    ) -> ChannelRunResult:
        """Route an inbound plugin-channel message through Row-Bot core."""
        self._check_dispatch()
        from row_bot.plugins.channel_runtime import handle_plugin_channel_message

        return await handle_plugin_channel_message(
            plugin_id=self._plugin_id,
            message=message,
            callbacks=callbacks,
            channel=channel,
            enabled_tool_names=enabled_tool_names,
            stream=stream,
            approval_context=approval_context,
        )

    async def handle_channel_approval(
        self,
        *,
        channel_name: str,
        thread_id: str,
        approved: bool,
        callbacks: ChannelOutboundCallbacks,
        interrupt_ids: list[str] | None = None,
        source: str = "",
    ) -> ChannelRunResult:
        """Resume an interrupted plugin-channel agent turn."""
        self._check_dispatch()
        from row_bot.plugins.channel_runtime import handle_plugin_channel_approval

        return await handle_plugin_channel_approval(
            plugin_id=self._plugin_id,
            channel_name=channel_name,
            thread_id=thread_id,
            approved=approved,
            callbacks=callbacks,
            interrupt_ids=interrupt_ids,
            source=source,
        )

    def process_channel_attachment(
        self,
        attachment: ChannelAttachment,
        *,
        question: str = "",
        max_chars: int = 80000,
    ) -> ChannelAttachmentResult:
        """Process an inbound attachment through Row-Bot's shared media pipeline."""
        self._check_dispatch()
        from row_bot.plugins.channel_runtime import process_plugin_channel_attachment

        return process_plugin_channel_attachment(
            attachment,
            question=question,
            max_chars=max_chars,
        )

    def record_channel_activity(self, channel_name: str) -> None:
        self._check_dispatch()
        from row_bot.channels.base import record_activity

        record_activity(channel_name)

    def generate_channel_pairing_code(self, channel_name: str) -> str:
        self._check_dispatch()
        from row_bot.channels import auth as channel_auth

        return channel_auth.generate_pairing_code(channel_name)

    def verify_channel_pairing_code(
        self,
        channel_name: str,
        user_id: str,
        code: str,
        *,
        display_name: str = "",
    ) -> bool:
        self._check_active()
        from row_bot.channels import auth as channel_auth

        return channel_auth.verify_pairing_code(
            channel_name,
            user_id,
            code,
            display_name=display_name,
        )

    def is_channel_user_approved(self, channel_name: str, user_id: str) -> bool:
        self._check_active()
        from row_bot.channels import auth as channel_auth

        return channel_auth.is_user_approved(channel_name, user_id)

    def get_channel_approved_users(self, channel_name: str) -> list[str]:
        self._check_active()
        from row_bot.channels import auth as channel_auth

        return channel_auth.get_approved_users(channel_name)

    def revoke_channel_user(self, channel_name: str, user_id: str) -> bool:
        self._check_active()
        from row_bot.channels import auth as channel_auth

        return channel_auth.revoke_user(channel_name, user_id)

    def register_webhook_route(
        self,
        name: str,
        handler: Callable[
            [PluginWebhookRequest],
            Awaitable[PluginWebhookResponse] | PluginWebhookResponse,
        ],
        *,
        methods: list[str] | None = None,
        max_body_bytes: int = 1048576,
    ) -> str:
        from row_bot.plugins.webhooks import register_plugin_webhook
        with self._registration_lock:
            self._check_active(registration=True)
            from row_bot.plugins.webhooks import _normalize_methods, _normalize_body_limit, webhook_path
            webhook_path(self._plugin_id, name)
            _normalize_methods(methods)
            _normalize_body_limit(max_body_bytes)
            if not callable(handler) or any(item[0] == name for item in self._registered_webhooks):
                raise ValueError("Invalid or duplicate webhook registration")
            original_handler = handler

            async def guarded_handler(request: PluginWebhookRequest) -> PluginWebhookResponse:
                import inspect
                self._check_dispatch()
                result = original_handler(request)
                return await result if inspect.isawaitable(result) else result

            if self._staged:
                self._registered_webhooks.append((name, guarded_handler, methods, max_body_bytes))
                return self.get_webhook_path(name)
            return register_plugin_webhook(
                self._plugin_id, name, guarded_handler,
                methods=methods, max_body_bytes=max_body_bytes,
            )

    def get_webhook_path(self, name: str) -> str:
        from row_bot.plugins.webhooks import webhook_path

        return webhook_path(self._plugin_id, name)

    def get_webhook_url(self, name: str, *, start_tunnel: bool = False) -> str:
        self._check_active()
        from row_bot.plugins.webhooks import webhook_url

        return webhook_url(self._plugin_id, name, start_tunnel=start_tunnel)

    # -- Channel Auth -----------------------------------------------------
    def verify_bot_framework_jwt(
        self,
        authorization_header: str,
        *,
        app_id: str,
        service_url: str = "",
        channel_id: str = "",
        required_endorsement: str = "",
        metadata_url: str = "",
        issuer: str = "",
        timeout_seconds: float = 5.0,
    ) -> BotFrameworkAuthResult:
        """Verify a Bot Framework Connector bearer token for this plugin."""
        self._check_dispatch()
        return verify_bot_framework_jwt(
            authorization_header,
            app_id=app_id,
            service_url=service_url,
            channel_id=channel_id,
            required_endorsement=required_endorsement,
            metadata_url=metadata_url,
            issuer=issuer,
            timeout_seconds=timeout_seconds,
        )

    # -- Runtime Context --------------------------------------------------
    def is_background_workflow(self) -> bool:
        """Return True when the current tool call is running from a background task."""
        try:
            from row_bot.agent import is_background_workflow
            return bool(is_background_workflow())
        except Exception as exc:
            logger.debug("Plugin background workflow check unavailable: %s", exc)
            return False

    def get_allowed_recipients(self) -> list[str]:
        """Return email recipients approved for the current background task."""
        try:
            from row_bot.agent import _task_allowed_recipients_var
            recipients = _task_allowed_recipients_var.get() or []
            return [str(recipient) for recipient in recipients]
        except Exception as exc:
            logger.debug("Plugin allowed recipients unavailable: %s", exc)
            return []


# ═════════════════════════════════════════════════════════════════════════════
# PluginTool — base class for plugin tools
# ═════════════════════════════════════════════════════════════════════════════
class PluginTool:
    """Base class for plugin-provided tools.

    Mirrors the core ``BaseTool`` interface but lives entirely in the
    plugin namespace.  Plugin authors subclass this and register
    instances via ``api.register_tool(MyTool(api))``.
    """

    def __init__(self, plugin_api: PluginAPI):
        self.plugin_api = plugin_api

    # ── Identity (override in subclass) ──────────────────────────────────
    @property
    def name(self) -> str:
        """Internal unique identifier. Must be lowercase with underscores."""
        raise NotImplementedError

    @property
    def display_name(self) -> str:
        """Human-readable label shown in Settings and agent output."""
        raise NotImplementedError

    @property
    def description(self) -> str:
        """One-line description passed to the agent for tool selection."""
        return ""

    # ── Destructive / background permissions ────────────────────────────
    @property
    def destructive_tool_names(self) -> set[str]:
        """Names of sub-tools requiring user confirmation before execution.

        Override in subclasses that expose multiple LangChain tools via
        ``as_langchain_tools()`` and need some of them gated by a
        LangGraph ``interrupt()`` (e.g. send_email, delete_event).
        """
        return set()

    @property
    def background_allowed_tool_names(self) -> set[str]:
        """Destructive sub-tools allowed in background tasks.

        These tools must implement their own runtime permission checks
        (e.g. validating recipients against ``_task_allowed_recipients_var``).
        Only meaningful for tools that are also in ``destructive_tool_names``.
        """
        return set()

    # ── Execution ────────────────────────────────────────────────────────
    def execute(self, query: str) -> str:
        """Run the tool and return a text result.

        Must handle its own errors gracefully — return error messages
        instead of raising exceptions.
        """
        raise NotImplementedError(f"{type(self).__name__} must implement execute()")

    # ── Rich return helpers ──────────────────────────────────────────────
    @staticmethod
    def image_result(b64: str, text: str = "") -> str:
        """Wrap a base64-encoded image so the UI renders it inline."""
        return f"__IMAGE__:{b64}\n\n{text}" if text else f"__IMAGE__:{b64}"

    @staticmethod
    def html_result(html: str, text: str = "") -> str:
        """Wrap HTML content so the UI renders it inline."""
        return f"__HTML__:{html}\n\n{text}" if text else f"__HTML__:{html}"

    @staticmethod
    def chart_result(plotly_json: str, text: str = "") -> str:
        """Wrap a Plotly figure JSON so the UI renders an interactive chart."""
        return f"__CHART__:{plotly_json}\n\n{text}" if text else f"__CHART__:{plotly_json}"

    # ── LangChain Bridge ─────────────────────────────────────────────────
    def as_langchain_tool(self) -> StructuredTool:
        """Convert to a LangChain StructuredTool for the agent."""
        tool_instance = self

        def _run(query: str) -> str:
            try:
                return tool_instance.execute(query)
            except Exception as exc:
                logger.error("Plugin tool '%s' error: %s", tool_instance.name,
                             exc, exc_info=True)
                return f"Error in {tool_instance.display_name}: {exc}"

        return StructuredTool.from_function(
            func=_run,
            name=self.name,
            description=f"{self.display_name}: {self.description}",
        )

    def as_langchain_tools(self) -> list:
        """Return one or more LangChain tools. Override for multi-tool plugins."""
        return [self.as_langchain_tool()]
