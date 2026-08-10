from __future__ import annotations

import json
import logging
import math
import os
import re
from contextlib import nullcontext
from typing import Any, Iterator, Sequence

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable
from pydantic import Field

from row_bot.cancellation import current_cancellation_scope

logger = logging.getLogger(__name__)

OPENAI_COMPATIBLE_READ_TIMEOUT_ENV = "ROW_BOT_OPENAI_COMPATIBLE_READ_TIMEOUT"
DEFAULT_OPENAI_COMPATIBLE_READ_TIMEOUT = 900.0
OPENAI_COMPATIBLE_CONNECT_TIMEOUT = 10.0
OPENAI_COMPATIBLE_WRITE_TIMEOUT = 120.0
OPENAI_COMPATIBLE_POOL_TIMEOUT = 10.0
OPENAI_COMPATIBLE_STREAM_ATTEMPTS = 2


def _default_read_timeout() -> float:
    raw = str(os.environ.get(OPENAI_COMPATIBLE_READ_TIMEOUT_ENV) or "").strip()
    if not raw:
        return DEFAULT_OPENAI_COMPATIBLE_READ_TIMEOUT
    try:
        timeout = float(raw)
    except ValueError:
        timeout = 0.0
    if math.isfinite(timeout) and timeout > 0:
        return timeout
    logger.warning(
        "Ignoring invalid %s; using %.0f seconds",
        OPENAI_COMPATIBLE_READ_TIMEOUT_ENV,
        DEFAULT_OPENAI_COMPATIBLE_READ_TIMEOUT,
    )
    return DEFAULT_OPENAI_COMPATIBLE_READ_TIMEOUT


class ChatOpenAICompatible(BaseChatModel):
    """OpenAI-compatible chat transport with endpoint-specific normalization."""

    model_name: str
    api_key: str = "not-needed"
    base_url: str
    endpoint: dict[str, Any] = Field(default_factory=dict)
    timeout: float = Field(default_factory=_default_read_timeout)
    http_client: Any | None = None

    @property
    def _llm_type(self) -> str:
        return "openai_compatible_chat"

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Any],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable:
        return self.bind(tools=[_openai_tool(tool) for tool in tools], tool_choice=tool_choice or "auto", **kwargs)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        body = self._request_body(messages, stream=False, stop=stop, **kwargs)
        response = self._post(body)
        payload = response.json()
        choice = _first_choice(payload)
        message_payload = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        content = _choice_content(message_payload, choice)
        known_tool_names = _known_tool_names(kwargs.get("tools") or [])
        content_text_calls: list[dict[str, Any]] = []
        if known_tool_names:
            cleaned_content, content_text_calls = _clean_text_tool_calls(
                content,
                known_tool_names=known_tool_names,
            )
            content = "" if content_text_calls else cleaned_content
        tool_calls = _tool_calls_from_openai(message_payload.get("tool_calls") or [])
        metadata = _response_metadata(payload)
        additional_kwargs = {}
        reasoning = _reasoning_content(message_payload, choice)
        if reasoning:
            additional_kwargs["reasoning_content"] = reasoning
        if not tool_calls:
            tool_calls = content_text_calls or _recover_text_tool_calls_for_empty_response(
                content=content,
                reasoning=reasoning,
                known_tool_names=known_tool_names,
            )
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=content, tool_calls=tool_calls, additional_kwargs=additional_kwargs, response_metadata=metadata))],
            llm_output=metadata,
        )

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        tools = kwargs.get("tools") or []
        if not _endpoint_streaming_supported(self.endpoint, has_tools=bool(tools)):
            yield from self._stream_via_non_stream(messages, stop=stop, **kwargs)
            return
        body = self._request_body(messages, stream=True, stop=stop, **kwargs)
        logger.info(
            "custom_openai_stream: start provider=%s model=%s base_url=%s profile=%s messages=%d roles=%s content_chars=%d tools=%d",
            self.endpoint.get("provider_id") or "custom",
            self.model_name,
            self.base_url,
            self.endpoint.get("profile") or "generic",
            len(body.get("messages") or []),
            ",".join(str(item.get("role") or "?") for item in body.get("messages") or [] if isinstance(item, dict)),
            sum(len(str(item.get("content") or "")) for item in body.get("messages") or [] if isinstance(item, dict)),
            len(body.get("tools") or []),
        )
        content_seen = False
        reasoning_seen = False
        tool_seen = False
        payload_seen = False
        tool_assembler = _StreamToolCallAssembler(
            provider=str(self.endpoint.get("provider_id") or "custom"),
            model=self.model_name,
        )
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        atlas_endpoint = _is_atlascloud_endpoint(self.endpoint)
        atlas_anthropic_route = _is_atlascloud_anthropic_route(self.endpoint, self.model_name)
        atlas_stream_visible_after_tool = atlas_endpoint and bool(tools) and _history_has_tool_result(messages)
        buffered_content_for_tools = bool(tools) and not atlas_stream_visible_after_tool
        anthropic_tool_assembler = _AnthropicStreamToolCallAssembler(
            provider=str(self.endpoint.get("provider_id") or "custom"),
            model=self.model_name,
        )
        scope = current_cancellation_scope()
        for payload in self._iter_stream_events(body):
            if scope is not None and scope.is_cancelled():
                return
            payload_seen = True
            if atlas_anthropic_route and _is_anthropic_stream_payload(payload):
                event_payload = _anthropic_event_payload(payload)
                event_type = _anthropic_event_type(event_payload)
                if event_type == "error":
                    raise RuntimeError(_atlascloud_stream_error_message(self.model_name, event_payload))
                if event_type == "content_block_start":
                    block = event_payload.get("content_block") if isinstance(event_payload.get("content_block"), dict) else {}
                    block_type = str(block.get("type") or "").strip()
                    index = _anthropic_content_block_index(event_payload)
                    if block_type == "tool_use":
                        anthropic_tool_assembler.add_start(index, block)
                    elif block_type == "text":
                        content = str(block.get("text") or "")
                        if content:
                            content_seen = True
                            content_parts.append(content)
                            suppress_marker = atlas_endpoint and bool(tools) and _contains_text_tool_call_marker(content)
                            visible_content = "" if buffered_content_for_tools or suppress_marker else content
                            chunk = ChatGenerationChunk(message=AIMessageChunk(content=visible_content))
                            if visible_content and run_manager:
                                run_manager.on_llm_new_token(visible_content, chunk=chunk)
                            yield chunk
                    elif block_type in {"thinking", "redacted_thinking"}:
                        reasoning = str(block.get("thinking") or block.get("text") or "")
                        if reasoning:
                            reasoning_seen = True
                            reasoning_parts.append(reasoning)
                            yield ChatGenerationChunk(message=AIMessageChunk(content="", additional_kwargs={"reasoning_content": reasoning}))
                    continue
                if event_type == "content_block_delta":
                    delta = event_payload.get("delta") if isinstance(event_payload.get("delta"), dict) else {}
                    delta_type = str(delta.get("type") or "").strip()
                    if delta_type == "text_delta":
                        content = str(delta.get("text") or "")
                        if content:
                            content_seen = True
                            content_parts.append(content)
                            raw_preview = "".join(content_parts)
                            suppress_marker = atlas_endpoint and bool(tools) and _contains_text_tool_call_marker(raw_preview)
                            visible_content = "" if buffered_content_for_tools or suppress_marker else content
                            chunk = ChatGenerationChunk(message=AIMessageChunk(content=visible_content))
                            if visible_content and run_manager:
                                run_manager.on_llm_new_token(visible_content, chunk=chunk)
                            yield chunk
                    elif delta_type == "thinking_delta":
                        reasoning = str(delta.get("thinking") or "")
                        if reasoning:
                            reasoning_seen = True
                            reasoning_parts.append(reasoning)
                            yield ChatGenerationChunk(message=AIMessageChunk(content="", additional_kwargs={"reasoning_content": reasoning}))
                    elif delta_type == "input_json_delta":
                        anthropic_tool_assembler.add_delta(
                            _anthropic_content_block_index(event_payload),
                            str(delta.get("partial_json") or ""),
                        )
                    continue
                if event_type in {"message_start", "message_delta", "message_stop", "content_block_stop", "ping"}:
                    continue
            choice = _first_choice(payload)
            delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
            content = str(delta.get("content") or "")
            reasoning = _reasoning_content(delta, choice)
            if content or reasoning:
                content_seen = content_seen or bool(content)
                reasoning_seen = reasoning_seen or bool(reasoning)
                if content:
                    content_parts.append(content)
                if reasoning:
                    reasoning_parts.append(reasoning)
                additional_kwargs = {"reasoning_content": reasoning} if reasoning else {}
                raw_preview = "".join(content_parts)
                suppress_marker = atlas_endpoint and bool(tools) and _contains_text_tool_call_marker(raw_preview)
                visible_content = "" if buffered_content_for_tools or suppress_marker else content
                chunk = ChatGenerationChunk(message=AIMessageChunk(content=visible_content, additional_kwargs=additional_kwargs))
                if visible_content and run_manager:
                    run_manager.on_llm_new_token(visible_content, chunk=chunk)
                yield chunk
            for call in delta.get("tool_calls") or []:
                tool_assembler.add(call)
        if scope is not None and scope.is_cancelled():
            return
        finalized_tool_calls = tool_assembler.finalize()
        if atlas_anthropic_route:
            finalized_tool_calls.extend(anthropic_tool_assembler.finalize())
        known_tool_names = _known_tool_names(tools)
        raw_content = "".join(content_parts)
        if known_tool_names:
            cleaned_content, content_text_calls = _clean_text_tool_calls(
                raw_content,
                known_tool_names=known_tool_names,
            )
        else:
            cleaned_content, content_text_calls = raw_content, []
        if buffered_content_for_tools and cleaned_content and not finalized_tool_calls and not content_text_calls:
            yield ChatGenerationChunk(message=AIMessageChunk(content=cleaned_content))
        for chunk_payload in finalized_tool_calls:
            tool_seen = True
            yield ChatGenerationChunk(message=AIMessageChunk(content="", tool_call_chunks=[chunk_payload]))
        if not finalized_tool_calls:
            recovered_calls = content_text_calls or _recover_text_tool_calls_for_empty_response(
                content=cleaned_content,
                reasoning="".join(reasoning_parts),
                known_tool_names=known_tool_names,
            )
            for index, call in enumerate(recovered_calls):
                chunk_payload = _tool_call_chunk_from_parsed(call, index)
                tool_seen = True
                logger.info(
                    "custom_openai_stream: recovered text tool call provider=%s model=%s tool=%s",
                    self.endpoint.get("provider_id") or "custom",
                    self.model_name,
                    call.get("name"),
                )
                yield ChatGenerationChunk(message=AIMessageChunk(content="", tool_call_chunks=[chunk_payload]))
        if not content_seen and not reasoning_seen and not tool_seen:
            if payload_seen and atlas_anthropic_route:
                raise RuntimeError(
                    "Atlas Cloud returned streaming events for "
                    f"{self.model_name}, but Row-Bot could not find supported text, reasoning, or tool-call deltas. "
                    "Skipped non-stream fallback to avoid a long Claude timeout."
                )
            logger.warning(
                "custom_openai_stream: empty stream; retrying non-stream fallback provider=%s model=%s payload_seen=%s",
                self.endpoint.get("provider_id") or "custom",
                self.model_name,
                payload_seen,
            )
            yield from self._stream_via_non_stream(messages, stop=stop, **kwargs)
            return
        if not content_seen:
            logger.warning(
                "custom_openai_stream: completed without content provider=%s model=%s reasoning_seen=%s tool_seen=%s",
                self.endpoint.get("provider_id") or "custom",
                self.model_name,
                reasoning_seen,
                tool_seen,
            )
        else:
            logger.info(
                "custom_openai_stream: complete provider=%s model=%s reasoning_seen=%s tool_seen=%s",
                self.endpoint.get("provider_id") or "custom",
                self.model_name,
                reasoning_seen,
                tool_seen,
            )
        yield ChatGenerationChunk(message=AIMessageChunk(content="", chunk_position="last"))

    def _stream_via_non_stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        logger.info(
            "custom_openai_stream: using non-stream fallback provider=%s model=%s base_url=%s",
            self.endpoint.get("provider_id") or "custom",
            self.model_name,
            self.base_url,
        )
        result = self._generate(messages, stop=stop, **kwargs)
        message = result.generations[0].message if result.generations else AIMessage(content="")
        reasoning = str((getattr(message, "additional_kwargs", None) or {}).get("reasoning_content") or "")
        content = str(getattr(message, "content", "") or "")
        tool_calls = getattr(message, "tool_calls", []) or []
        known_tool_names = _known_tool_names(kwargs.get("tools") or [])
        content_text_calls: list[dict[str, Any]] = []
        if known_tool_names:
            cleaned_content, content_text_calls = _clean_text_tool_calls(
                content,
                known_tool_names=known_tool_names,
            )
            content = "" if content_text_calls else cleaned_content
        if content_text_calls and not tool_calls:
            tool_calls = content_text_calls
        if reasoning:
            yield ChatGenerationChunk(message=AIMessageChunk(content="", additional_kwargs={"reasoning_content": reasoning}))
        if content or tool_calls:
            yield ChatGenerationChunk(message=AIMessageChunk(content=content, tool_calls=tool_calls))
        if not content and not tool_calls:
            logger.warning(
                "custom_openai_stream: non-stream fallback returned no content provider=%s model=%s reasoning_seen=%s tool_seen=%s",
                self.endpoint.get("provider_id") or "custom",
                self.model_name,
                bool(reasoning),
                bool(tool_calls),
            )
        yield ChatGenerationChunk(message=AIMessageChunk(content="", chunk_position="last"))

    def _request_body(
        self,
        messages: list[BaseMessage],
        *,
        stream: bool,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        accepts_tools = _endpoint_accepts_tools(self.endpoint)
        include_tool_history = accepts_tools and _use_native_tool_history(self.endpoint, self.model_name)
        body: dict[str, Any] = {
            "model": self.model_name,
            "messages": _openai_messages(messages, endpoint=self.endpoint, include_tool_fields=include_tool_history),
            "stream": stream,
        }
        if stop:
            body["stop"] = stop
        extra_body = self.endpoint.get("extra_body")
        if isinstance(extra_body, dict):
            body.update(extra_body)
        _apply_reasoning_request_config(body, self.endpoint)
        self._apply_context_override(body)
        tools = kwargs.get("tools") or []
        if tools and accepts_tools:
            body["tools"] = [_openai_tool(tool) for tool in tools]
            tool_choice = kwargs.get("tool_choice")
            if tool_choice:
                body["tool_choice"] = tool_choice
        for key in ("temperature", "top_p", "max_tokens", "presence_penalty", "frequency_penalty"):
            if key in kwargs and kwargs[key] is not None:
                body[key] = kwargs[key]
        if self.endpoint.get("drop_unsupported_params", True):
            _drop_empty_or_unsupported(body, accepts_tools=accepts_tools)
        return body

    def _post(self, body: dict[str, Any]) -> Any:
        client = self.http_client or _new_http_client(self.timeout)
        owns_client = self.http_client is None
        request_timeout = _httpx_timeout(self.timeout)
        try:
            response = client.post(
                _chat_url(self.base_url),
                json=body,
                headers=self._headers(),
                timeout=request_timeout,
            )
            _raise_for_status(response, self.endpoint)
            return response
        finally:
            if owns_client:
                client.close()

    def _iter_stream_events(self, body: dict[str, Any]) -> Iterator[dict[str, Any]]:
        import httpx

        scope = current_cancellation_scope()
        for attempt in range(1, OPENAI_COMPATIBLE_STREAM_ATTEMPTS + 1):
            if scope is not None and scope.is_cancelled():
                return
            produced = 0
            try:
                for payload in self._iter_stream_events_once(body):
                    produced += 1
                    yield payload
                return
            except (httpx.TimeoutException, httpx.RemoteProtocolError) as exc:
                if scope is not None and scope.is_cancelled():
                    return
                if produced or attempt >= OPENAI_COMPATIBLE_STREAM_ATTEMPTS:
                    raise
                logger.warning(
                    "custom_openai_stream: transport failed before first event; retrying "
                    "provider=%s model=%s attempt=%d/%d error=%s",
                    self.endpoint.get("provider_id") or "custom",
                    self.model_name,
                    attempt + 1,
                    OPENAI_COMPATIBLE_STREAM_ATTEMPTS,
                    type(exc).__name__,
                )

    def _iter_stream_events_once(self, body: dict[str, Any]) -> Iterator[dict[str, Any]]:
        client = self.http_client or _new_http_client(self.timeout)
        owns_client = self.http_client is None
        request_timeout = _httpx_timeout(self.timeout)
        scope = current_cancellation_scope()
        unregister_callbacks: list[Any] = []
        raw_lines = 0
        data_lines = 0
        decoded_events = 0
        done_seen = False
        try:
            context = client.stream(
                "POST",
                _chat_url(self.base_url),
                json=body,
                headers=self._headers(),
                timeout=request_timeout,
            ) if hasattr(client, "stream") else nullcontext(client.post(
                _chat_url(self.base_url),
                json=body,
                headers=self._headers(),
                timeout=request_timeout,
            ))
            with context as response:
                _raise_for_status(response, self.endpoint)
                if scope is not None:
                    close_response = getattr(response, "close", None)
                    if callable(close_response):
                        unregister_callbacks.append(scope.register(close_response, "openai_compatible_response.close"))
                    if owns_client:
                        close_client = getattr(client, "close", None)
                        if callable(close_client):
                            unregister_callbacks.append(scope.register(close_client, "openai_compatible_client.close"))
                for line in response.iter_lines():
                    if scope is not None and scope.is_cancelled():
                        return
                    if line:
                        raw_lines += 1
                        text = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else str(line)
                        stripped = text.strip()
                        if stripped.startswith("data:"):
                            data_lines += 1
                            if stripped[5:].strip() == "[DONE]":
                                done_seen = True
                    payload = _decode_sse_line(line)
                    if payload:
                        decoded_events += 1
                        yield payload
        except Exception:
            if scope is not None and scope.is_cancelled():
                logger.debug("custom_openai_stream: stream cancelled provider=%s model=%s", self.endpoint.get("provider_id") or "custom", self.model_name)
                return
            raise
        finally:
            cancelled = scope is not None and scope.is_cancelled()
            for unregister in reversed(unregister_callbacks):
                try:
                    unregister()
                except Exception:
                    logger.debug("custom_openai_stream: unregister cancellation callback failed", exc_info=True)
            if cancelled:
                logger.debug(
                    "custom_openai_stream: cancelled provider=%s model=%s raw_lines=%d data_lines=%d decoded=%d",
                    self.endpoint.get("provider_id") or "custom",
                    self.model_name,
                    raw_lines,
                    data_lines,
                    decoded_events,
                )
            elif decoded_events == 0:
                logger.warning(
                    "custom_openai_stream: no decoded SSE events provider=%s model=%s raw_lines=%d data_lines=%d done_seen=%s",
                    self.endpoint.get("provider_id") or "custom",
                    self.model_name,
                    raw_lines,
                    data_lines,
                    done_seen,
                )
            else:
                logger.debug(
                    "custom_openai_stream: decoded SSE events provider=%s model=%s raw_lines=%d data_lines=%d decoded=%d done_seen=%s",
                    self.endpoint.get("provider_id") or "custom",
                    self.model_name,
                    raw_lines,
                    data_lines,
                    decoded_events,
                    done_seen,
                )
            if owns_client:
                client.close()

    def _headers(self) -> dict[str, str]:
        headers = dict(self.endpoint.get("headers") or {})
        if self.api_key:
            header_name = str(self.endpoint.get("api_key_header") or "Authorization")
            headers[header_name] = f"Bearer {self.api_key}" if header_name.lower() == "authorization" else self.api_key
        headers.setdefault("Accept", "application/json")
        headers.setdefault("Content-Type", "application/json")
        return headers

    def _apply_context_override(self, body: dict[str, Any]) -> None:
        if not self.endpoint.get("supports_runtime_context_override"):
            return
        param_name = str(self.endpoint.get("context_param_name") or "").strip()
        if not param_name:
            return
        try:
            from row_bot.models import get_context_size
            from row_bot.providers.selection import model_ref

            provider_id = str(self.endpoint.get("provider_id") or "").strip()
            model_name = model_ref(provider_id, self.model_name) if provider_id else self.model_name
            context_size = get_context_size(model_name)
        except Exception:
            return
        if context_size and context_size > 0:
            body[param_name] = int(context_size)


def _openai_messages(messages: list[BaseMessage], *, endpoint: dict[str, Any], include_tool_fields: bool) -> list[dict[str, Any]]:
    ordered = _system_first(messages) if endpoint.get("system_message_mode") == "system_first" else list(messages)
    return [_openai_message(message, endpoint=endpoint, include_tool_fields=include_tool_fields) for message in ordered]


def _system_first(messages: list[BaseMessage]) -> list[BaseMessage]:
    system = [message for message in messages if isinstance(message, SystemMessage)]
    rest = [message for message in messages if not isinstance(message, SystemMessage)]
    if not system:
        return rest
    if len(system) == 1:
        return [system[0], *rest]
    content = "\n\n".join(_message_text(message) for message in system if _message_text(message).strip())
    return [SystemMessage(content=content), *rest]


def _openai_message(message: BaseMessage, *, endpoint: dict[str, Any], include_tool_fields: bool) -> dict[str, Any]:
    role = "user"
    if isinstance(message, SystemMessage):
        role = "system"
    elif isinstance(message, AIMessage):
        role = "assistant"
    elif isinstance(message, ToolMessage):
        role = "tool"
    elif isinstance(message, HumanMessage):
        role = "user"
    content = _message_content(message, endpoint)
    if not include_tool_fields and isinstance(message, ToolMessage):
        name = str(getattr(message, "name", "") or getattr(message, "tool_call_id", "") or "tool")
        return {"role": "user", "content": f"[Tool result from {name}]: {_message_text(message)}"}
    payload: dict[str, Any] = {"role": role, "content": content}
    if include_tool_fields and isinstance(message, AIMessage) and getattr(message, "tool_calls", None):
        payload["tool_calls"] = [_tool_call_to_openai(call, index) for index, call in enumerate(message.tool_calls) if isinstance(call, dict)]
    reasoning = _message_reasoning_for_replay(message, endpoint)
    if reasoning:
        payload["reasoning_content"] = reasoning
    if include_tool_fields and isinstance(message, ToolMessage):
        payload["tool_call_id"] = str(getattr(message, "tool_call_id", "") or getattr(message, "name", "") or "tool")
    return payload


def _message_reasoning_for_replay(message: BaseMessage, endpoint: dict[str, Any]) -> str:
    if not isinstance(message, AIMessage):
        return ""
    if not endpoint.get("supports_reasoning_replay"):
        return ""
    for call in getattr(message, "tool_calls", None) or []:
        if isinstance(call, dict) and str(call.get("id") or "").startswith("text_call_"):
            return ""
    additional_kwargs = getattr(message, "additional_kwargs", None) or {}
    reasoning = additional_kwargs.get("reasoning_content") or additional_kwargs.get("reasoning")
    if not isinstance(reasoning, str) or not reasoning:
        return ""
    if "<tool_call>" in reasoning or "<function=" in reasoning:
        return ""
    return reasoning


def _message_content(message: BaseMessage, endpoint: dict[str, Any]) -> Any:
    if endpoint.get("message_content_mode") == "string_text" and not _message_has_multimodal_content(message):
        return _message_text(message)
    normalized = _normalize_openai_content_parts(message.content)
    if normalized is not None:
        return normalized
    return message.content


def _message_has_multimodal_content(message: BaseMessage) -> bool:
    content = message.content
    if isinstance(content, list):
        return any(_normalize_image_part(item) is not None for item in content if isinstance(item, dict))
    return False


def _normalize_openai_content_parts(content: Any) -> list[dict[str, Any]] | None:
    if not isinstance(content, list):
        return None
    parts: list[dict[str, Any]] = []
    for item in content:
        if isinstance(item, str):
            if item:
                parts.append({"type": "text", "text": item})
            continue
        if not isinstance(item, dict):
            continue
        image_part = _normalize_image_part(item)
        if image_part is not None:
            parts.append(image_part)
            continue
        text = item.get("text") or item.get("input") or item.get("content")
        if isinstance(text, str) and text:
            parts.append({"type": "text", "text": text})
    return parts


def _normalize_image_part(item: dict[str, Any]) -> dict[str, Any] | None:
    part_type = str(item.get("type") or "").strip()
    if part_type not in {"image_url", "input_image", "image"} and not any(key in item for key in ("image_url", "image")):
        return None

    url = ""
    image_url = item.get("image_url")
    image = item.get("image")
    if isinstance(image_url, str):
        url = image_url
    elif isinstance(image_url, dict):
        raw = image_url.get("url")
        if isinstance(raw, str):
            url = raw
    if not url:
        if isinstance(image, str):
            url = image
        elif isinstance(image, dict):
            raw = image.get("url")
            if isinstance(raw, str):
                url = raw
    if not url:
        raw_url = item.get("url")
        if isinstance(raw_url, str):
            url = raw_url
    url = str(url or "").strip()
    if not url:
        return None
    return {"type": "image_url", "image_url": {"url": url}}


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


def _is_atlascloud_endpoint(endpoint: dict[str, Any]) -> bool:
    provider_id = str(endpoint.get("provider_id") or "").strip().lower()
    profile = str(endpoint.get("profile") or "").strip().lower()
    return provider_id == "atlascloud" or profile == "atlascloud"


def _is_atlascloud_anthropic_route(endpoint: dict[str, Any], model_name: str) -> bool:
    return _is_atlascloud_endpoint(endpoint) and str(model_name or "").strip().lower().startswith("anthropic/")


def _history_has_tool_result(messages: Sequence[BaseMessage]) -> bool:
    return any(isinstance(message, ToolMessage) for message in messages)


def _contains_text_tool_call_marker(text: str) -> bool:
    raw = str(text or "")
    return "<tool_call" in raw or "<function=" in raw


def _anthropic_event_payload(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    if isinstance(data, dict):
        merged = dict(data)
        if not merged.get("type") and payload.get("event"):
            merged["type"] = payload.get("event")
        return merged
    return payload


def _anthropic_event_type(payload: dict[str, Any]) -> str:
    return str(payload.get("type") or payload.get("event") or "").strip()


def _is_anthropic_stream_payload(payload: dict[str, Any]) -> bool:
    event_payload = _anthropic_event_payload(payload)
    event_type = _anthropic_event_type(event_payload)
    return event_type in {
        "message_start",
        "message_delta",
        "message_stop",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "ping",
        "error",
    }


def _anthropic_content_block_index(payload: dict[str, Any]) -> int:
    try:
        return int(payload.get("index") or 0)
    except (TypeError, ValueError):
        return 0


def _atlascloud_stream_error_message(model_name: str, payload: dict[str, Any]) -> str:
    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    error_type = str(error.get("type") or payload.get("type") or "stream_error")
    message = str(error.get("message") or payload.get("message") or "unknown streaming error")
    return f"Atlas Cloud stream error for {model_name}: {error_type}: {message}"


def _endpoint_accepts_tools(endpoint: dict[str, Any]) -> bool:
    return True


def _use_native_tool_history(endpoint: dict[str, Any], model_name: str) -> bool:
    if _is_atlascloud_anthropic_route(endpoint, model_name):
        return False
    mode = str(endpoint.get("tool_history_mode") or "native_required").strip().lower()
    return mode in {"", "native_required", "native"}


def _endpoint_streaming_supported(endpoint: dict[str, Any], *, has_tools: bool = False) -> bool:
    probe = endpoint.get("last_probe") if isinstance(endpoint.get("last_probe"), dict) else {}
    if probe.get("streaming_ok") is False:
        return False
    provider_id = str(endpoint.get("provider_id") or "")
    if has_tools and provider_id.startswith("custom_openai_"):
        if probe.get("streaming_tool_calling") is True:
            return True
        logger.info(
            "custom_openai_stream: tool request using non-stream fallback provider=%s streaming_tool_calling=%s",
            provider_id or "custom",
            probe.get("streaming_tool_calling"),
        )
        return False
    return True


def _merge_chat_template_kwargs(body: dict[str, Any], values: dict[str, Any]) -> None:
    existing = body.get("chat_template_kwargs")
    merged = dict(existing) if isinstance(existing, dict) else {}
    merged.update(values)
    body["chat_template_kwargs"] = merged


def _apply_reasoning_request_config(body: dict[str, Any], endpoint: dict[str, Any]) -> None:
    mode = str(endpoint.get("reasoning_mode") or "auto").strip().lower()
    profile = str(endpoint.get("profile") or "").strip().lower()
    template_profiles = {"llama_cpp", "vllm", "sglang"}
    if mode == "off":
        if profile in template_profiles or endpoint.get("supports_reasoning_content"):
            _merge_chat_template_kwargs(body, {"enable_thinking": False})
        body.pop("reasoning", None)
        body.pop("reasoning_effort", None)
        return
    budget = endpoint.get("thinking_budget")
    if budget not in (None, "", 0):
        try:
            parsed = int(budget)
        except (TypeError, ValueError):
            parsed = 0
        if parsed > 0 and (profile in template_profiles or endpoint.get("supports_reasoning_content")):
            _merge_chat_template_kwargs(body, {"thinking_budget": parsed})


class _StreamToolCallAssembler:
    def __init__(self, *, provider: str, model: str):
        self.provider = provider
        self.model = model
        self._order: list[int] = []
        self._calls: dict[int, dict[str, Any]] = {}

    def add(self, call: Any) -> None:
        if not isinstance(call, dict):
            return
        stream_index = self._stream_index(call)
        if stream_index not in self._calls:
            self._calls[stream_index] = {
                "index": stream_index,
                "id": "",
                "type": "",
                "name": "",
                "arguments": [],
            }
            self._order.append(stream_index)
        state = self._calls[stream_index]
        if call.get("id") and not state["id"]:
            state["id"] = str(call.get("id"))
        if call.get("type") and not state["type"]:
            state["type"] = str(call.get("type"))
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        if function.get("name") and not state["name"]:
            state["name"] = str(function.get("name")).strip()
        if "arguments" in function:
            arguments = function.get("arguments")
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments if arguments is not None else {})
            state["arguments"].append(arguments)

    def finalize(self) -> list[dict[str, Any]]:
        chunks: list[dict[str, Any]] = []
        for stream_index in self._order:
            state = self._calls[stream_index]
            name = str(state.get("name") or "").strip()
            if not name:
                logger.warning(
                    "custom_openai_stream: dropped streamed tool call without name provider=%s model=%s index=%s args_chars=%d",
                    self.provider,
                    self.model,
                    stream_index,
                    len("".join(state.get("arguments") or [])),
                )
                continue
            call_id = str(state.get("id") or f"openai_call_{stream_index}")
            chunks.append({
                "name": name,
                "args": "".join(state.get("arguments") or []),
                "id": call_id,
                "index": int(stream_index),
            })
        return chunks

    def _stream_index(self, call: dict[str, Any]) -> int:
        raw_index = call.get("index")
        if raw_index is None:
            if len(self._order) == 1:
                return self._order[0]
            return len(self._order)
        try:
            return int(raw_index)
        except (TypeError, ValueError):
            return len(self._order)


class _AnthropicStreamToolCallAssembler:
    def __init__(self, *, provider: str, model: str):
        self.provider = provider
        self.model = model
        self._order: list[int] = []
        self._calls: dict[int, dict[str, Any]] = {}

    def add_start(self, stream_index: int, block: dict[str, Any]) -> None:
        state = self._state(stream_index)
        if block.get("id") and not state["id"]:
            state["id"] = str(block.get("id"))
        if block.get("name") and not state["name"]:
            state["name"] = str(block.get("name")).strip()
        input_value = block.get("input")
        if isinstance(input_value, dict):
            state["input"] = input_value

    def add_delta(self, stream_index: int, partial_json: str) -> None:
        state = self._state(stream_index)
        if partial_json:
            state["arguments"].append(partial_json)

    def finalize(self) -> list[dict[str, Any]]:
        chunks: list[dict[str, Any]] = []
        for stream_index in self._order:
            state = self._calls[stream_index]
            name = str(state.get("name") or "").strip()
            if not name:
                logger.warning(
                    "custom_openai_stream: dropped anthropic streamed tool call without name provider=%s model=%s index=%s args_chars=%d",
                    self.provider,
                    self.model,
                    stream_index,
                    len("".join(state.get("arguments") or [])),
                )
                continue
            arguments = "".join(state.get("arguments") or [])
            if not arguments:
                input_value = state.get("input") if isinstance(state.get("input"), dict) else {}
                arguments = json.dumps(input_value)
            chunks.append({
                "name": name,
                "args": arguments,
                "id": str(state.get("id") or f"anthropic_call_{stream_index}"),
                "index": int(stream_index),
            })
        return chunks

    def _state(self, stream_index: int) -> dict[str, Any]:
        if stream_index not in self._calls:
            self._calls[stream_index] = {
                "index": stream_index,
                "id": "",
                "name": "",
                "input": {},
                "arguments": [],
            }
            self._order.append(stream_index)
        return self._calls[stream_index]


def _drop_empty_or_unsupported(body: dict[str, Any], *, accepts_tools: bool) -> None:
    if not accepts_tools:
        body.pop("tools", None)
        body.pop("tool_choice", None)
    for key in ("parallel_tool_calls", "reasoning", "reasoning_effort", "response_format"):
        if key in body and body[key] in (None, "", {}, []):
            body.pop(key, None)


def _openai_tool(tool: dict[str, Any] | type | Any) -> dict[str, Any]:
    if isinstance(tool, dict) and tool.get("type") == "function" and isinstance(tool.get("function"), dict):
        return tool
    try:
        from langchain_core.utils.function_calling import convert_to_openai_tool

        converted = convert_to_openai_tool(tool)
        if isinstance(converted, dict):
            return converted
    except Exception:
        pass
    if isinstance(tool, dict):
        return tool
    name = getattr(tool, "name", tool.__class__.__name__)
    description = getattr(tool, "description", "")
    schema = getattr(tool, "args_schema", None)
    parameters = schema.model_json_schema() if hasattr(schema, "model_json_schema") else {"type": "object", "properties": {}}
    return {"type": "function", "function": {"name": str(name), "description": str(description or ""), "parameters": parameters}}


def _tool_call_to_openai(call: dict[str, Any], index: int) -> dict[str, Any]:
    return {
        "id": str(call.get("id") or f"call_{index}"),
        "type": "function",
        "function": {
            "name": str(call.get("name") or ""),
            "arguments": json.dumps(call.get("args") or {}),
        },
    }


def _tool_calls_from_openai(calls: list[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, call in enumerate(calls):
        if not isinstance(call, dict):
            continue
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        args = function.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        result.append({
            "name": str(function.get("name") or ""),
            "args": args if isinstance(args, dict) else {},
            "id": str(call.get("id") or f"call_{index}"),
            "type": "tool_call",
        })
    return result


def _tool_call_chunk_from_openai(call: Any, index: int) -> dict[str, Any] | None:
    if not isinstance(call, dict):
        return None
    function = call.get("function") if isinstance(call.get("function"), dict) else {}
    name = str(function.get("name") or "").strip()
    arguments = function.get("arguments") or ""
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments)
    call_index = call.get("index")
    if call_index is None:
        call_index = index
    call_id = str(call.get("id") or f"openai_call_{call_index}")
    if not name and not arguments:
        return None
    return {
        "name": name,
        "args": arguments,
        "id": call_id,
        "index": int(call_index),
    }


def _tool_call_chunk_from_parsed(call: dict[str, Any], index: int) -> dict[str, Any]:
    return {
        "name": str(call.get("name") or ""),
        "args": json.dumps(call.get("args") if isinstance(call.get("args"), dict) else {}),
        "id": str(call.get("id") or f"text_call_{index}"),
        "index": int(index),
    }


_TEXT_TOOL_CALL_RE = re.compile(
    r"<tool_call>\s*<function=(?P<name>[A-Za-z0-9_.:-]+)>(?P<body>.*?)</function>\s*</tool_call>",
    re.DOTALL,
)
_TEXT_TOOL_PARAM_RE = re.compile(
    r"<parameter=(?P<name>[A-Za-z0-9_.:-]+)>(?P<value>.*?)</parameter>",
    re.DOTALL,
)


def _known_tool_names(tools: Sequence[dict[str, Any] | type | Any]) -> set[str]:
    names: set[str] = set()
    for tool in tools:
        try:
            normalized = _openai_tool(tool)
        except Exception:
            normalized = tool
        function = normalized.get("function") if isinstance(normalized, dict) and isinstance(normalized.get("function"), dict) else {}
        name = str(function.get("name") or getattr(tool, "name", "") or "").strip()
        if name:
            names.add(name)
    return names


def _recover_text_tool_calls_for_empty_response(
    *,
    content: str,
    reasoning: str,
    known_tool_names: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Recover local-model text tool calls only when no final text exists.

    Some small OpenAI-compatible local models emit a native tool call first, then
    after a validation repair emit an XML-ish tool-call envelope inside
    reasoning_content. When there is no user-visible content, treating that
    envelope as a real tool call lets the graph continue without adding prompt
    bloat or touching normal provider output.
    """
    if str(content or "").strip():
        return []
    calls = _text_tool_calls_from_content(reasoning)
    if known_tool_names:
        calls = [call for call in calls if str(call.get("name") or "") in known_tool_names]
    return _dedupe_text_tool_calls([call for call in calls if str(call.get("name") or "").strip()])


def _clean_text_tool_calls(
    text: str,
    *,
    known_tool_names: set[str] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Remove XML-ish text tool-call blocks from visible content.

    Local OpenAI-compatible servers sometimes put legacy tool-call envelopes in
    assistant text instead of native ``tool_calls``.  The envelope should be
    recovered for execution when it names a known bound tool, but the raw XML-ish
    block is never user-facing answer text.
    """

    raw = str(text or "")
    calls: list[dict[str, Any]] = []

    def _replace(match: re.Match[str]) -> str:
        parsed = _text_tool_call_from_match(match, len(calls))
        if parsed is None:
            return ""
        if known_tool_names and str(parsed.get("name") or "") not in known_tool_names:
            return ""
        calls.append(parsed)
        return ""

    cleaned = _TEXT_TOOL_CALL_RE.sub(_replace, raw)
    return cleaned, _dedupe_text_tool_calls(calls)


def _dedupe_text_tool_calls(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for call in calls:
        name = str(call.get("name") or "").strip()
        if not name:
            continue
        args = call.get("args") if isinstance(call.get("args"), dict) else {}
        signature = f"{name}:{json.dumps(args, sort_keys=True, default=str)}"
        if signature in seen:
            continue
        seen.add(signature)
        next_call = dict(call)
        next_call["id"] = f"text_call_{len(deduped)}"
        deduped.append(next_call)
    return deduped


def _text_tool_calls_from_content(text: str) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for match in _TEXT_TOOL_CALL_RE.finditer(str(text or "")):
        call = _text_tool_call_from_match(match, len(calls))
        if call is not None:
            calls.append(call)
    return _dedupe_text_tool_calls(calls)


def _text_tool_call_from_match(match: re.Match[str], index: int) -> dict[str, Any] | None:
    name = str(match.group("name") or "").strip()
    if not name:
        return None
    args: dict[str, Any] = {}
    body = match.group("body") or ""
    for param in _TEXT_TOOL_PARAM_RE.finditer(body):
        param_name = str(param.group("name") or "").strip()
        if not param_name:
            continue
        args[param_name] = str(param.group("value") or "").strip()
    return {
        "name": name,
        "args": args,
        "id": f"text_call_{index}",
        "type": "tool_call",
    }


def _new_http_client(timeout: float) -> Any:
    import httpx

    return httpx.Client(timeout=_httpx_timeout(timeout))


def _httpx_timeout(read_timeout: float) -> Any:
    import httpx

    return httpx.Timeout(
        connect=OPENAI_COMPATIBLE_CONNECT_TIMEOUT,
        read=float(read_timeout),
        write=OPENAI_COMPATIBLE_WRITE_TIMEOUT,
        pool=OPENAI_COMPATIBLE_POOL_TIMEOUT,
    )


def _chat_url(base_url: str) -> str:
    return f"{str(base_url or '').rstrip('/')}/chat/completions"


def _raise_for_status(response: Any, endpoint: dict[str, Any]) -> None:
    status_code = int(getattr(response, "status_code", 0) or 0)
    if 200 <= status_code < 300 or status_code == 0:
        return
    name = str(endpoint.get("display_name") or endpoint.get("name") or "custom endpoint")
    detail = _safe_response_text(response)
    suffix = f" Details: {detail}" if detail else ""
    if status_code == 400:
        raise RuntimeError(
            f"{name} rejected the chat request (HTTP 400). The endpoint profile or selected model "
            f"may not accept this message/tool payload.{suffix}"
        )
    raise RuntimeError(f"{name} request failed with HTTP {status_code}.{suffix}")


def _safe_response_text(response: Any) -> str:
    def _message_from_payload(payload: Any) -> str:
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict) and error.get("message"):
                return str(error.get("message"))[:300]
            for key in ("message", "detail"):
                if payload.get(key):
                    return str(payload.get(key))[:300]
            return str(payload)[:300]
        return ""

    try:
        payload = response.json()
        message = _message_from_payload(payload)
        if message:
            return message
    except Exception:
        pass
    try:
        raw = response.read() if hasattr(response, "read") else b""
        if isinstance(raw, bytes):
            text = raw.decode("utf-8", errors="replace")
        else:
            text = str(raw or "")
        if text:
            try:
                message = _message_from_payload(json.loads(text))
                if message:
                    return message
            except json.JSONDecodeError:
                pass
            return text[:300]
    except Exception:
        pass
    try:
        return str(getattr(response, "text", "") or "")[:300]
    except Exception:
        return ""


def _first_choice(payload: dict[str, Any]) -> dict[str, Any]:
    choices = payload.get("choices") if isinstance(payload, dict) else []
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        return choices[0]
    return {}


def _choice_content(message_payload: dict[str, Any], choice: dict[str, Any]) -> str:
    content = message_payload.get("content")
    if isinstance(content, str):
        return content
    text = choice.get("text")
    return str(text or "")


def _reasoning_content(message_payload: dict[str, Any], choice: dict[str, Any]) -> str:
    reasoning = message_payload.get("reasoning_content")
    if isinstance(reasoning, str):
        return reasoning
    reasoning = message_payload.get("reasoning")
    if isinstance(reasoning, str):
        return reasoning
    reasoning = choice.get("reasoning_content")
    return reasoning if isinstance(reasoning, str) else ""


def _response_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    if "model" in payload:
        metadata["model"] = payload["model"]
    usage = payload.get("usage")
    if isinstance(usage, dict):
        metadata["token_usage"] = usage
    return metadata


def _decode_sse_line(line: Any) -> dict[str, Any] | None:
    if not line:
        return None
    if isinstance(line, bytes):
        line = line.decode("utf-8", errors="replace")
    text = str(line).strip()
    if text.startswith("data:"):
        text = text[5:].strip()
    if text == "[DONE]":
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None
