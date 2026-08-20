from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Iterator, Sequence

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable

from row_bot.cancellation import current_cancellation_scope
from row_bot.providers import claude_subscription as claude_auth

CLAUDE_SUBSCRIPTION_MESSAGES_BASE_URL = claude_auth.CLAUDE_SUBSCRIPTION_API_ROOT_URL

logger = logging.getLogger(__name__)


class ChatClaudeSubscriptionMessages(BaseChatModel):
    """LangChain chat model backed by Claude Subscription OAuth bearer auth."""

    model_name: str
    base_url: str = CLAUDE_SUBSCRIPTION_MESSAGES_BASE_URL
    timeout: float = 120.0
    max_tokens: int = 4096
    anthropic_client: Any | None = None
    client_factory: Any | None = None
    reasoning_plan: Any | None = None

    @property
    def _llm_type(self) -> str:
        return "claude_subscription_messages"

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Any],
        *,
        tool_choice: str | dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Runnable:
        return self.bind(
            tools=[_anthropic_tool(tool) for tool in tools],
            tool_choice=_anthropic_tool_choice(tool_choice),
            **kwargs,
        )

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        request = self._request_kwargs(messages, stop=stop, **kwargs)
        payload = _plain_data(self._create_message(request))
        message = _ai_message_from_response(payload)
        metadata = _response_metadata(payload)
        return ChatResult(generations=[ChatGeneration(message=message)], llm_output=metadata)

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        request = self._request_kwargs(messages, stop=stop, **kwargs)
        tool_blocks: dict[int, dict[str, str]] = {}
        scope = current_cancellation_scope()
        for event in self._stream_message_events(request):
            if scope is not None and scope.is_cancelled():
                return
            payload = _plain_data(event)
            event_type = str(payload.get("type") or "")
            if event_type == "content_block_start":
                index = int(payload.get("index") or 0)
                block = _plain_data(payload.get("content_block"))
                if block.get("type") == "tool_use":
                    tool_blocks[index] = {
                        "id": str(block.get("id") or uuid.uuid4()),
                        "name": _runtime_tool_name(str(block.get("name") or "")),
                    }
                    yield ChatGenerationChunk(message=AIMessageChunk(content="", tool_call_chunks=[{
                        "name": tool_blocks[index]["name"],
                        "args": "",
                        "id": tool_blocks[index]["id"],
                        "index": index,
                    }]))
                continue
            if event_type == "content_block_delta":
                index = int(payload.get("index") or 0)
                delta = _plain_data(payload.get("delta"))
                delta_type = str(delta.get("type") or "")
                if delta_type == "text_delta":
                    text = str(delta.get("text") or "")
                    if not text:
                        continue
                    chunk = ChatGenerationChunk(message=AIMessageChunk(content=text))
                    if run_manager:
                        run_manager.on_llm_new_token(text, chunk=chunk)
                    yield chunk
                    continue
                if delta_type == "input_json_delta":
                    partial = str(delta.get("partial_json") or "")
                    tool = tool_blocks.get(index, {"id": "", "name": ""})
                    yield ChatGenerationChunk(message=AIMessageChunk(content="", tool_call_chunks=[{
                        "name": None,
                        "args": partial,
                        "id": None,
                        "index": index,
                    }]))
                    continue
            if event_type == "error":
                raise RuntimeError(f"Claude Subscription stream failed: {_event_error_message(payload)}")
        if scope is not None and scope.is_cancelled():
            return
        yield ChatGenerationChunk(message=AIMessageChunk(content="", chunk_position="last"))

    def _request_kwargs(
        self,
        messages: list[BaseMessage],
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        system, anthropic_messages = _messages_to_anthropic(messages)
        request: dict[str, Any] = {
            "model": self.model_name,
            "messages": anthropic_messages,
            "max_tokens": int(kwargs.get("max_tokens") or self.max_tokens),
            "system": claude_auth.claude_subscription_compat_system(system),
        }
        tools = [_wire_tool(_anthropic_tool(tool)) for tool in kwargs.get("tools") or []]
        tool_choice = _wire_tool_choice(_anthropic_tool_choice(kwargs.get("tool_choice")))
        if tools and not (tool_choice and tool_choice.get("type") == "none"):
            request["tools"] = tools
            if tool_choice:
                request["tool_choice"] = tool_choice
        if stop:
            request["stop_sequences"] = list(stop)
        if self.reasoning_plan is not None and not self.reasoning_plan.is_default:
            selection = self.reasoning_plan.selection
            capabilities = self.reasoning_plan.capabilities
            if selection.kind == "effort":
                request["output_config"] = {"effort": selection.effort}
                if capabilities and capabilities.thinking_mode == "adaptive":
                    request["thinking"] = {"type": "adaptive"}
            elif selection.kind == "budget":
                request["thinking"] = {"type": "enabled", "budget_tokens": selection.budget}
            elif selection.kind == "off":
                request["thinking"] = {"type": "disabled"}
        return request

    def _create_message(self, request: dict[str, Any]) -> Any:
        client = self._sdk_client()
        try:
            return client.messages.create(**request)
        except Exception as exc:
            if _status_code_from_exception(exc) == 401 and self._refresh_access_token_if_possible():
                client = self._sdk_client()
                return client.messages.create(**request)
            raise RuntimeError(_normalized_exception(exc)) from exc

    def _stream_message_events(self, request: dict[str, Any]) -> Iterator[Any]:
        client = self._sdk_client()
        scope = current_cancellation_scope()
        unregister_callbacks: list[Any] = []
        try:
            stream_context = client.messages.stream(**request)
            with stream_context as stream:
                if scope is not None:
                    unregister_callbacks.extend(_register_stream_cancellation(scope, (stream, stream_context), "claude_subscription_stream"))
                    if scope.is_cancelled():
                        return
                for event in stream:
                    if scope is not None and scope.is_cancelled():
                        return
                    yield event
        except Exception as exc:
            if scope is not None and scope.is_cancelled():
                logger.debug("claude_subscription_stream: stream cancelled model=%s", self.model_name)
                return
            if _status_code_from_exception(exc) == 401 and self._refresh_access_token_if_possible():
                client = self._sdk_client()
                stream_context = client.messages.stream(**request)
                with stream_context as stream:
                    if scope is not None:
                        unregister_callbacks.extend(_register_stream_cancellation(scope, (stream, stream_context), "claude_subscription_retry_stream"))
                        if scope.is_cancelled():
                            return
                    for event in stream:
                        if scope is not None and scope.is_cancelled():
                            return
                        yield event
                return
            logger.warning("claude_subscription_stream: stream failed: %s", exc)
            raise RuntimeError(_normalized_exception(exc)) from exc
        finally:
            for unregister in reversed(unregister_callbacks):
                try:
                    unregister()
                except Exception:
                    logger.debug("claude_subscription_stream: unregister cancellation callback failed", exc_info=True)

    def _sdk_client(self) -> Any:
        if self.anthropic_client is not None:
            return self.anthropic_client
        credentials = claude_auth.claude_subscription_runtime_credentials(refresh_if_needed=True)
        return claude_auth.claude_subscription_sdk_client(
            credentials.access_token,
            base_url=self.base_url,
            timeout=self.timeout,
            client_factory=self.client_factory,
        )

    def _refresh_access_token_if_possible(self) -> bool:
        credentials = claude_auth.claude_subscription_runtime_credentials(refresh_if_needed=False)
        if not credentials.refresh_token:
            return False
        refreshed = claude_auth.refresh_claude_subscription_token(credentials.refresh_token)
        claude_auth.save_claude_subscription_oauth_tokens(refreshed)
        return True


def _plain_data(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_plain_data(item) for item in value]
    if isinstance(value, tuple):
        return [_plain_data(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _plain_data(item) for key, item in value.items()}
    for method_name in ("model_dump", "dict", "to_dict"):
        method = getattr(value, method_name, None)
        if callable(method):
            try:
                return _plain_data(method())
            except TypeError:
                try:
                    return _plain_data(method(mode="json"))
                except Exception:
                    pass
            except Exception:
                pass
    if hasattr(value, "__dict__"):
        return {
            key: _plain_data(item)
            for key, item in vars(value).items()
            if not str(key).startswith("_")
        }
    return value


def _register_stream_cancellation(scope: Any, targets: tuple[Any, ...], label: str) -> list[Any]:
    unregister_callbacks: list[Any] = []
    seen: set[tuple[int, str]] = set()
    for target in targets:
        for method_name in ("close", "cancel"):
            method = getattr(target, method_name, None)
            if not callable(method):
                continue
            key = (id(target), method_name)
            if key in seen:
                continue
            seen.add(key)
            unregister_callbacks.append(scope.register(method, f"{label}.{method_name}"))
    return unregister_callbacks


def _status_code_from_exception(exc: Exception) -> int:
    for attr in ("status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    return int(value or 0)


def _safe_exception_text(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    text = getattr(response, "text", None)
    if not text:
        body = getattr(response, "content", None)
        if isinstance(body, bytes):
            try:
                text = body.decode("utf-8", errors="replace")
            except Exception:
                text = ""
    if not text:
        text = str(exc)
    return str(text or "")[:300].replace("Bearer ", "Bearer [redacted] ")


def _normalized_exception(exc: Exception) -> str:
    status_code = _status_code_from_exception(exc)
    if status_code:
        return _normalized_error(status_code, _safe_exception_text(exc))
    return f"Claude Subscription Messages request failed: {_safe_exception_text(exc)}"


def _normalized_error(status_code: int, detail: str) -> str:
    lowered = str(detail or "").lower()
    suffix = f": {detail}" if detail else ""
    if status_code == 401:
        return "Claude subscription login expired. Reconnect Claude Subscription in Settings -> Providers."
    if status_code == 403:
        return f"Claude subscription access denied or plan not eligible{suffix}"
    if status_code == 429:
        return f"Claude subscription rate/usage limit reached{suffix}"
    if "credit" in lowered or ("usage" in lowered and "exhaust" in lowered):
        return f"Claude subscription usage credits or monthly credit exhausted{suffix}"
    if status_code >= 500:
        return f"Transient Anthropic service error while using Claude Subscription{suffix}"
    return f"Claude Subscription Messages request failed with HTTP {status_code}{suffix}"


def _message_text(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("input") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return str(content or "")


def _text_block(text: str) -> dict[str, str]:
    return {"type": "text", "text": text}


def _image_url_from_block(block: dict[str, Any]) -> str:
    image_url = block.get("image_url")
    if isinstance(image_url, str):
        return image_url
    if isinstance(image_url, dict):
        return str(image_url.get("url") or "")
    image = block.get("image")
    if isinstance(image, str):
        return image
    if isinstance(image, dict):
        return str(image.get("url") or image.get("source") or "")
    source = block.get("source")
    if isinstance(source, dict) and source.get("type") == "url":
        return str(source.get("url") or "")
    return ""


def _image_block(block: dict[str, Any]) -> dict[str, Any] | None:
    url = _image_url_from_block(block)
    if not url:
        source = block.get("source")
        if isinstance(source, dict) and source.get("type") in {"base64", "url"}:
            return {"type": "image", "source": dict(source)}
        return None
    if url.startswith("data:") and ";base64," in url:
        header, data = url.split(";base64,", 1)
        media_type = header.removeprefix("data:") or "image/jpeg"
        return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}
    return {"type": "image", "source": {"type": "url", "url": url}}


def _wire_content_block(block: dict[str, Any]) -> dict[str, Any]:
    payload = dict(block)
    if payload.get("type") == "tool_use" and payload.get("name"):
        payload["name"] = _wire_tool_name(str(payload["name"]))
    return payload


def _message_content_blocks(message: BaseMessage) -> list[dict[str, Any]]:
    content = message.content
    if isinstance(content, str):
        return [_text_block(content)] if content else []
    if not isinstance(content, list):
        text = str(content or "")
        return [_text_block(text)] if text else []

    blocks: list[dict[str, Any]] = []
    for item in content:
        if isinstance(item, str):
            if item:
                blocks.append(_text_block(item))
            continue
        if not isinstance(item, dict):
            text = str(item or "")
            if text:
                blocks.append(_text_block(text))
            continue
        block_type = str(item.get("type") or "")
        if block_type == "text":
            text = item.get("text") or item.get("input") or item.get("content")
            if isinstance(text, str) and text:
                blocks.append(_text_block(text))
            continue
        if block_type in {"image", "image_url", "input_image"} or isinstance(item.get("image"), (dict, str)):
            image = _image_block(item)
            if image:
                blocks.append(image)
            continue
        if block_type in {"tool_use", "tool_result"}:
            blocks.append(_wire_content_block(item))
            continue
        text = item.get("text") or item.get("input") or item.get("content")
        if isinstance(text, str) and text:
            blocks.append(_text_block(text))
    return blocks


def _messages_to_anthropic(messages: list[BaseMessage]) -> tuple[str, list[dict[str, Any]]]:
    system_parts: list[str] = []
    payloads: list[dict[str, Any]] = []
    for message in messages:
        text = _message_text(message)
        if isinstance(message, SystemMessage):
            if text:
                system_parts.append(text)
            continue
        if isinstance(message, HumanMessage):
            role = "user"
            content = _message_content_blocks(message)
        elif isinstance(message, AIMessage):
            role = "assistant"
            content = _message_content_blocks(message)
            content.extend(_ai_tool_use_blocks(message))
        elif isinstance(message, ToolMessage):
            role = "user"
            content = [{
                "type": "tool_result",
                "tool_use_id": str(message.tool_call_id),
                "content": text,
            }]
        else:
            role = getattr(message, "type", "user") or "user"
            content = _message_content_blocks(message)
        if not content and text:
            content = [_text_block(text)]
        payloads.append({"role": role, "content": content})
    return "\n\n".join(system_parts), payloads


def _json_tool_arguments(args: Any) -> dict[str, Any]:
    if isinstance(args, dict):
        return dict(args)
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
            return dict(parsed) if isinstance(parsed, dict) else {"arguments": parsed}
        except Exception:
            return {"arguments": args}
    if args is None:
        return {}
    try:
        return dict(args)
    except Exception:
        return {"arguments": str(args)}


def _wire_tool_name(name: str) -> str:
    return claude_auth.claude_subscription_wire_tool_name(name)


def _runtime_tool_name(name: str) -> str:
    return claude_auth.claude_subscription_runtime_tool_name(name)


def _ai_tool_use_blocks(message: AIMessage) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for tool_call in getattr(message, "tool_calls", []) or []:
        if not isinstance(tool_call, dict):
            continue
        name = str(tool_call.get("name") or "").strip()
        if not name:
            continue
        blocks.append({
            "type": "tool_use",
            "id": str(tool_call.get("id") or uuid.uuid4()),
            "name": _wire_tool_name(name),
            "input": _json_tool_arguments(tool_call.get("args")),
        })
    return blocks


def _anthropic_tool(tool: Any) -> dict[str, Any]:
    if isinstance(tool, dict):
        payload = dict(tool)
    else:
        try:
            from langchain_core.utils.function_calling import convert_to_openai_tool

            payload = dict(convert_to_openai_tool(tool))
        except Exception:
            name = getattr(tool, "name", None) or getattr(tool, "__name__", None) or "tool"
            payload = {"type": "function", "name": str(name), "description": "", "parameters": {"type": "object", "properties": {}}}
    if payload.get("type") == "function" and isinstance(payload.get("function"), dict):
        function = dict(payload["function"])
        payload = {"type": "function", **function}
    if payload.get("type") == "custom":
        return payload
    name = str(payload.get("name") or "tool")
    return {
        "name": name,
        "description": str(payload.get("description") or ""),
        "input_schema": dict(payload.get("input_schema") or payload.get("parameters") or {"type": "object", "properties": {}}),
    }


def _wire_tool(tool: dict[str, Any]) -> dict[str, Any]:
    payload = dict(tool)
    if payload.get("name"):
        payload["name"] = _wire_tool_name(str(payload["name"]))
    return payload


def _anthropic_tool_choice(tool_choice: str | dict[str, Any] | None) -> dict[str, Any] | None:
    if not tool_choice:
        return None
    if isinstance(tool_choice, dict):
        return dict(tool_choice)
    if tool_choice in {"auto", "any", "none"}:
        return {"type": tool_choice}
    return {"type": "tool", "name": str(tool_choice)}


def _wire_tool_choice(tool_choice: dict[str, Any] | None) -> dict[str, Any] | None:
    if not tool_choice:
        return None
    payload = dict(tool_choice)
    if payload.get("type") == "tool" and payload.get("name"):
        payload["name"] = _wire_tool_name(str(payload["name"]))
    return payload


def _ai_message_from_response(payload: dict[str, Any]) -> AIMessage:
    content_items = payload.get("content") if isinstance(payload.get("content"), list) else []
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for item in content_items:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            text_parts.append(str(item.get("text") or ""))
        elif item.get("type") == "tool_use":
            args = item.get("input") if isinstance(item.get("input"), dict) else _json_tool_arguments(item.get("input"))
            tool_calls.append({
                "name": _runtime_tool_name(str(item.get("name") or "")),
                "args": args,
                "id": str(item.get("id") or uuid.uuid4()),
                "type": "tool_call",
            })
    return AIMessage(content="".join(text_parts), tool_calls=tool_calls, response_metadata=_response_metadata(payload))


def _response_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    metadata = {
        "id": payload.get("id") or "",
        "model": payload.get("model") or "",
        "stop_reason": payload.get("stop_reason") or "",
    }
    usage = _plain_data(payload.get("usage"))
    if isinstance(usage, dict):
        metadata["token_usage"] = dict(usage)
    return metadata


def _event_error_message(event: dict[str, Any]) -> str:
    error = event.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("type") or error)
    return str(event.get("message") or event)
