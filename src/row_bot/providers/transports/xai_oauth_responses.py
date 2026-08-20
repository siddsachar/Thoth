from __future__ import annotations

import json
import logging
import re
import time
import uuid
from contextlib import nullcontext
from typing import Any, Iterator, Sequence

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable
from pydantic import Field

from row_bot.cancellation import current_cancellation_scope
from row_bot.providers import xai_oauth as xai_auth

logger = logging.getLogger(__name__)


class ChatXAIOAuthResponses(BaseChatModel):
    """LangChain chat model backed by xAI OAuth bearer auth and Responses."""

    model_name: str
    base_url: str = Field(default_factory=xai_auth.xai_oauth_base_url)
    timeout: float = 120.0
    http_client: Any | None = None
    reasoning_plan: Any | None = None

    @property
    def _llm_type(self) -> str:
        return "xai_oauth_responses"

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Any],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable:
        return self.bind(tools=[_responses_tool(tool) for tool in tools], tool_choice=tool_choice or "auto", **kwargs)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        events = list(self._iter_response_events(self._request_body(messages, stop=stop, **kwargs)))
        content = _assistant_text_from_events(events)
        tool_calls = _tool_calls_from_events(events)
        metadata = _response_metadata_from_events(events)
        message = AIMessage(content=content, tool_calls=tool_calls, response_metadata=metadata)
        return ChatResult(generations=[ChatGeneration(message=message)], llm_output=metadata)

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        body = self._request_body(messages, stop=stop, **kwargs)
        saw_text_delta = False
        tool_index = 0
        started = time.perf_counter()
        logger.info("xai_oauth_sse: stream start model=%s", self.model_name)
        scope = current_cancellation_scope()
        for event in self._iter_response_events(body):
            if scope is not None and scope.is_cancelled():
                return
            event_type = event.get("type")
            if event_type == "response.output_text.delta":
                delta = str(event.get("delta") or "")
                if not delta:
                    continue
                saw_text_delta = True
                chunk = ChatGenerationChunk(message=AIMessageChunk(content=delta))
                if run_manager:
                    run_manager.on_llm_new_token(delta, chunk=chunk)
                yield chunk
                continue
            if event_type != "response.output_item.done":
                continue
            item = event.get("item") if isinstance(event.get("item"), dict) else {}
            if item.get("type") == "function_call":
                chunk_payload = _tool_call_chunk_from_item(item, tool_index)
                if chunk_payload:
                    tool_index += 1
                    yield ChatGenerationChunk(message=AIMessageChunk(content="", tool_call_chunks=[chunk_payload]))
                continue
            if not saw_text_delta and item.get("type") == "message" and item.get("role") == "assistant":
                for text in _assistant_text_parts_from_message_item(item):
                    if not text:
                        continue
                    chunk = ChatGenerationChunk(message=AIMessageChunk(content=text))
                    if run_manager:
                        run_manager.on_llm_new_token(text, chunk=chunk)
                    yield chunk
        if scope is not None and scope.is_cancelled():
            return
        logger.info("xai_oauth_sse: stream complete after %.3fs model=%s", time.perf_counter() - started, self.model_name)
        yield ChatGenerationChunk(message=AIMessageChunk(content="", chunk_position="last"))

    def _request_body(
        self,
        messages: list[BaseMessage],
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        instructions, input_items = _messages_to_responses_input(messages)
        body: dict[str, Any] = {
            "model": self.model_name,
            "instructions": instructions,
            "input": input_items,
            "store": False,
            "stream": True,
        }
        tools = [_responses_tool(tool) for tool in kwargs.get("tools") or []]
        if tools:
            body["tools"] = tools
            body["tool_choice"] = kwargs.get("tool_choice") or "auto"
            body["parallel_tool_calls"] = bool(kwargs.get("parallel_tool_calls", True))
        if stop:
            body["stop"] = list(stop)
        if "reasoning" in kwargs and kwargs["reasoning"] is not None:
            body["reasoning"] = kwargs["reasoning"]
        if self.reasoning_plan is not None and not self.reasoning_plan.is_default:
            selection = self.reasoning_plan.selection
            if selection.kind == "effort":
                body["reasoning"] = {"effort": selection.effort}
        return body

    def _iter_response_events(self, body: dict[str, Any]) -> Iterator[dict[str, Any]]:
        client = self.http_client or _new_http_client(self.timeout)
        owns_client = self.http_client is None
        scope = current_cancellation_scope()
        unregister_callbacks: list[Any] = []
        try:
            if scope is not None and owns_client:
                close_client = getattr(client, "close", None)
                if callable(close_client):
                    unregister_callbacks.append(scope.register(close_client, "xai_oauth_client.close"))
            if _body_has_input_image(body):
                json_body = dict(body)
                json_body.pop("stream", None)
                yield from self._post_response_events(client, json_body, scope=scope)
                return
            retry_after_refresh = False
            with self._stream_once(client, body) as response:
                if scope is not None:
                    close_response = getattr(response, "close", None)
                    if callable(close_response):
                        unregister_callbacks.append(scope.register(close_response, "xai_oauth_response.close"))
                    if scope.is_cancelled():
                        return
                if int(getattr(response, "status_code", 0) or 0) == 401:
                    if scope is not None and scope.is_cancelled():
                        return
                    retry_after_refresh = self._refresh_access_token_if_possible()
                    if not retry_after_refresh:
                        self._raise_for_status(response)
                else:
                    self._raise_for_status(response)
                    yield from _iter_sse_events(response, scope=scope)
            if scope is not None and scope.is_cancelled():
                return
            if retry_after_refresh:
                with self._stream_once(client, body) as response:
                    if scope is not None:
                        close_response = getattr(response, "close", None)
                        if callable(close_response):
                            unregister_callbacks.append(scope.register(close_response, "xai_oauth_retry_response.close"))
                        if scope.is_cancelled():
                            return
                    self._raise_for_status(response)
                    yield from _iter_sse_events(response, scope=scope)
        except Exception as exc:
            if scope is not None and scope.is_cancelled():
                logger.debug("xai_oauth_sse: stream cancelled model=%s", self.model_name)
                return
            logger.warning("xai_oauth_sse: stream failed: %s", exc)
            raise
        finally:
            for unregister in reversed(unregister_callbacks):
                try:
                    unregister()
                except Exception:
                    logger.debug("xai_oauth_sse: unregister cancellation callback failed", exc_info=True)
            if owns_client:
                client.close()

    def _post_response_events(self, client: Any, body: dict[str, Any], *, scope: Any | None = None) -> Iterator[dict[str, Any]]:
        response = client.post(
            _responses_url(self.base_url),
            json=body,
            headers=self._headers(stream=False),
            timeout=self.timeout,
        )
        if scope is not None and scope.is_cancelled():
            return
        if int(getattr(response, "status_code", 0) or 0) == 401:
            if scope is not None and scope.is_cancelled():
                return
            if not self._refresh_access_token_if_possible():
                self._raise_for_status(response)
            response = client.post(
                _responses_url(self.base_url),
                json=body,
                headers=self._headers(stream=False),
                timeout=self.timeout,
            )
        if scope is not None and scope.is_cancelled():
            return
        self._raise_for_status(response)
        yield from _events_from_response_payload(_json_response(response))

    def _stream_once(self, client: Any, body: dict[str, Any]) -> Any:
        kwargs = {
            "json": body,
            "headers": self._headers(),
            "timeout": self.timeout,
        }
        url = _responses_url(self.base_url)
        if hasattr(client, "stream"):
            return client.stream("POST", url, **kwargs)
        return nullcontext(client.post(url, **kwargs))

    def _refresh_access_token_if_possible(self) -> bool:
        credentials = xai_auth.xai_oauth_runtime_credentials(refresh_if_needed=False)
        if not credentials.refresh_token:
            return False
        refreshed = xai_auth.refresh_xai_oauth_token(credentials.refresh_token)
        xai_auth.save_xai_oauth_tokens(refreshed)
        return True

    def _raise_for_status(self, response: Any) -> None:
        status_code = int(getattr(response, "status_code", 0) or 0)
        if status_code < 200 or status_code >= 300:
            raise RuntimeError(_normalized_error(status_code, _safe_response_text(response)))

    def _headers(self, *, stream: bool = True) -> dict[str, str]:
        credentials = xai_auth.xai_oauth_runtime_credentials(refresh_if_needed=True)
        if not credentials.access_token:
            raise RuntimeError("xAI OAuth access token is missing. Connect xAI Grok in Settings -> Providers.")
        return {
            "Authorization": f"Bearer {credentials.access_token}",
            "Accept": "text/event-stream" if stream else "application/json",
            "Content-Type": "application/json",
            "User-Agent": xai_auth.xai_oauth_user_agent(),
        }


def _new_http_client(timeout: float) -> Any:
    import httpx

    return httpx.Client(timeout=timeout)


def _responses_url(base_url: str) -> str:
    return f"{xai_auth.xai_oauth_base_url(base_url).rstrip('/')}/responses"


def _safe_response_text(response: Any) -> str:
    try:
        if getattr(response, "is_stream_consumed", False) is False and hasattr(response, "read"):
            response.read()
    except Exception:
        pass
    try:
        payload = response.json()
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                return _redact_detail(str(error.get("message") or error.get("code") or error))
            for key in ("message", "detail"):
                if payload.get(key):
                    return _redact_detail(str(payload.get(key)))
    except Exception:
        pass
    try:
        return _redact_detail(str(getattr(response, "text", "") or ""))
    except Exception:
        return ""


def _json_response(response: Any) -> dict[str, Any]:
    try:
        payload = response.json()
    except Exception as exc:
        raise RuntimeError(f"xAI OAuth Responses response was not JSON: {_redact_detail(str(exc))}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("xAI OAuth Responses response was not a JSON object.")
    return payload


def _redact_detail(value: str, *, limit: int = 300) -> str:
    text = str(value or "")
    text = re.sub(r"(?i)Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [redacted]", text)
    text = re.sub(
        r"(?i)(\"?(?:access_token|refresh_token|id_token|authorization)\"?\s*[:=]\s*)\"?[^\",\s}]+",
        r"\1[redacted]",
        text,
    )
    text = re.sub(r"(?i)(code=)[^&\s]+", r"\1[redacted]", text)
    return text[:limit]


def _normalized_error(status_code: int, detail: str) -> str:
    suffix = f": {detail}" if detail else ""
    lowered = str(detail or "").lower()
    if status_code == 401:
        return "xAI OAuth login expired. Reconnect xAI Grok in Settings -> Providers."
    if status_code == 403:
        return f"xAI OAuth access denied or subscription not eligible. The separate xAI API key provider remains available in Settings -> Providers{suffix}"
    if status_code == 429:
        return f"xAI OAuth rate or usage limit reached{suffix}"
    if "credit" in lowered or ("usage" in lowered and "exhaust" in lowered):
        return f"xAI OAuth usage credits or monthly allowance exhausted{suffix}"
    if status_code >= 500:
        return f"Transient xAI service error while using xAI OAuth{suffix}"
    return f"xAI OAuth Responses request failed with HTTP {status_code}{suffix}"


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


def _content_item(text: str, *, output: bool = False) -> dict[str, str]:
    return {"type": "output_text" if output else "input_text", "text": text}


def _image_url_from_block(block: dict[str, Any]) -> str:
    image_url = block.get("image_url")
    if isinstance(image_url, str):
        return image_url
    if isinstance(image_url, dict):
        return str(image_url.get("url") or "")
    image = block.get("image")
    if isinstance(image, dict):
        return str(image.get("url") or "")
    if isinstance(image, str):
        return image
    return ""


def _input_image_item(block: dict[str, Any]) -> dict[str, Any] | None:
    url = _image_url_from_block(block)
    file_id = str(block.get("file_id") or "").strip()
    if not url and not file_id:
        return None
    item: dict[str, Any] = {"type": "input_image"}
    if url:
        item["image_url"] = url
    if file_id:
        item["file_id"] = file_id
    detail = block.get("detail")
    if detail is None and isinstance(block.get("image_url"), dict):
        detail = block["image_url"].get("detail")
    if detail:
        item["detail"] = detail
    return item


def _body_has_input_image(body: dict[str, Any]) -> bool:
    def _content_has_image(content: Any) -> bool:
        if isinstance(content, dict):
            return content.get("type") == "input_image"
        if isinstance(content, list):
            return any(_content_has_image(item) for item in content)
        return False

    for item in body.get("input") or []:
        if not isinstance(item, dict):
            continue
        if _content_has_image(item.get("content")):
            return True
    return False


def _message_content_items(message: BaseMessage, *, output: bool = False) -> list[dict[str, Any]]:
    content = message.content
    if isinstance(content, str):
        return [_content_item(content, output=output)] if content else []
    if not isinstance(content, list):
        text = str(content or "")
        return [_content_item(text, output=output)] if text else []

    items: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, str):
            if block:
                items.append(_content_item(block, output=output))
            continue
        if not isinstance(block, dict):
            text = str(block or "")
            if text:
                items.append(_content_item(text, output=output))
            continue
        block_type = str(block.get("type") or "")
        if block_type in {"text", "input_text", "output_text"}:
            text = block.get("text") or block.get("input") or block.get("content")
            if isinstance(text, str) and text:
                items.append(_content_item(text, output=output))
            continue
        if block_type in {"image_url", "input_image"} or isinstance(block.get("image"), (dict, str)):
            image_item = _input_image_item(block)
            if image_item:
                items.append(image_item)
            continue
        text = block.get("text") or block.get("input") or block.get("content")
        if isinstance(text, str) and text:
            items.append(_content_item(text, output=output))
    return items


def _messages_to_responses_input(messages: list[BaseMessage]) -> tuple[str, list[dict[str, Any]]]:
    instructions: list[str] = []
    input_items: list[dict[str, Any]] = []
    for message in messages:
        text = _message_text(message)
        if isinstance(message, SystemMessage):
            if text:
                instructions.append(text)
            continue
        if isinstance(message, HumanMessage):
            role = "user"
            output = False
        elif isinstance(message, AIMessage):
            if text:
                input_items.append({
                    "type": "message",
                    "role": "assistant",
                    "content": [_content_item(text, output=True)],
                })
            input_items.extend(_ai_tool_call_items(message))
            continue
        elif isinstance(message, ToolMessage):
            input_items.append({
                "type": "function_call_output",
                "call_id": message.tool_call_id,
                "output": text,
            })
            continue
        else:
            role = getattr(message, "type", "user") or "user"
            output = role == "assistant"
        content = _message_content_items(message, output=output)
        if not content and text:
            content = [_content_item(text, output=output)]
        input_items.append({"type": "message", "role": role, "content": content})
    return "\n\n".join(instructions), input_items


def _json_tool_arguments(args: Any) -> str:
    if isinstance(args, str):
        return args
    if args is None:
        return "{}"
    try:
        return json.dumps(args, separators=(",", ":"))
    except TypeError:
        return json.dumps({"arguments": str(args)}, separators=(",", ":"))


def _ai_tool_call_items(message: AIMessage) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for tool_call in getattr(message, "tool_calls", []) or []:
        if not isinstance(tool_call, dict):
            continue
        call_id = str(tool_call.get("id") or "").strip()
        name = str(tool_call.get("name") or "").strip()
        if not call_id or not name:
            continue
        items.append({
            "type": "function_call",
            "call_id": call_id,
            "name": name,
            "arguments": _json_tool_arguments(tool_call.get("args")),
        })
    return items


def _responses_tool(tool: Any) -> dict[str, Any]:
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
    payload.setdefault("type", "function")
    payload.setdefault("description", "")
    payload.setdefault("parameters", {"type": "object", "properties": {}})
    return payload


def _iter_sse_events(response: Any, *, scope: Any | None = None) -> Iterator[dict[str, Any]]:
    current_event = ""
    data_lines: list[str] = []
    for raw_line in response.iter_lines():
        if scope is not None and scope.is_cancelled():
            return
        line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else str(raw_line)
        if not line.strip():
            event = _sse_event_from_lines(current_event, data_lines)
            if event is not None:
                yield event
            current_event = ""
            data_lines = []
            continue
        if line.startswith("event:"):
            current_event = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            data_lines.append(line.split(":", 1)[1].strip())
    if scope is not None and scope.is_cancelled():
        return
    event = _sse_event_from_lines(current_event, data_lines)
    if event is not None:
        yield event


def _sse_event_from_lines(event_name: str, data_lines: list[str]) -> dict[str, Any] | None:
    if not data_lines:
        return None
    data = "\n".join(data_lines).strip()
    if not data or data == "[DONE]":
        return None
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    payload.setdefault("type", event_name)
    if payload.get("type") == "response.failed":
        raise RuntimeError(f"xAI OAuth Responses stream failed: {_response_error_message(payload)}")
    return payload


def _events_from_response_payload(payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for item in payload.get("output") or []:
        if isinstance(item, dict):
            yield {"type": "response.output_item.done", "item": item}
    if payload.get("error"):
        raise RuntimeError(f"xAI OAuth Responses failed: {_response_error_message({'response': payload})}")
    yield {"type": "response.completed", "response": payload}


def _response_error_message(payload: dict[str, Any]) -> str:
    response = payload.get("response") if isinstance(payload.get("response"), dict) else {}
    error = response.get("error") if isinstance(response.get("error"), dict) else payload.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or "response.failed")[:300]
    return "response.failed"


def _assistant_text_from_events(events: list[dict[str, Any]]) -> str:
    delta_parts = [str(event.get("delta") or "") for event in events if event.get("type") == "response.output_text.delta"]
    if delta_parts:
        return "".join(delta_parts)
    parts: list[str] = []
    for event in events:
        if event.get("type") != "response.output_item.done":
            continue
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        if item.get("type") != "message" or item.get("role") != "assistant":
            continue
        parts.extend(_assistant_text_parts_from_message_item(item))
    return "".join(parts)


def _tool_calls_from_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for event in events:
        if event.get("type") != "response.output_item.done":
            continue
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        if item.get("type") != "function_call":
            continue
        arguments = item.get("arguments") or "{}"
        try:
            args = json.loads(arguments) if isinstance(arguments, str) else dict(arguments)
        except Exception:
            args = {"arguments": arguments}
        calls.append({
            "name": str(item.get("name") or ""),
            "args": args,
            "id": str(item.get("call_id") or item.get("id") or uuid.uuid4()),
            "type": "tool_call",
        })
    return calls


def _tool_call_chunk_from_item(item: dict[str, Any], index: int) -> dict[str, Any] | None:
    name = str(item.get("name") or "").strip()
    call_id = str(item.get("call_id") or item.get("id") or "").strip()
    if not name or not call_id:
        return None
    arguments = item.get("arguments") or "{}"
    if not isinstance(arguments, str):
        arguments = _json_tool_arguments(arguments)
    return {"name": name, "args": arguments, "id": call_id, "index": index}


def _assistant_text_parts_from_message_item(item: dict[str, Any]) -> list[str]:
    parts: list[str] = []
    for content_item in item.get("content") or []:
        if isinstance(content_item, dict) and content_item.get("type") == "output_text":
            parts.append(str(content_item.get("text") or ""))
    return parts


def _response_metadata_from_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    for event in reversed(events):
        if event.get("type") != "response.completed":
            continue
        response = event.get("response") if isinstance(event.get("response"), dict) else {}
        metadata: dict[str, Any] = {"response_id": response.get("id") or ""}
        usage = response.get("usage")
        if isinstance(usage, dict):
            metadata["token_usage"] = dict(usage)
        return metadata
    return {}
