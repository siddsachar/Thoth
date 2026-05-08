from __future__ import annotations

import json
import logging
import time
import uuid
from contextlib import nullcontext
from typing import Any, Iterator, Sequence

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable
from pydantic import Field

from providers import codex as codex_auth

CODEX_RESPONSES_BASE_URL = "https://chatgpt.com/backend-api/codex"
CODEX_ORIGINATOR = "codex_cli_rs"
CODEX_USER_AGENT = "codex_cli_rs/0.0.0 (thoth)"
CODEX_INCLUDE = ["reasoning.encrypted_content"]

logger = logging.getLogger(__name__)


class ChatCodexResponses(BaseChatModel):
    """LangChain chat model backed by ChatGPT/Codex's Responses endpoint."""

    model_name: str
    base_url: str = CODEX_RESPONSES_BASE_URL
    timeout: float = 120.0
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    installation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    http_client: Any | None = None

    @property
    def _llm_type(self) -> str:
        return "codex_responses"

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
        events = self._request_events(messages, **kwargs)
        content = _assistant_text_from_events(events)
        tool_calls = _tool_calls_from_events(events)
        response_metadata = _response_metadata_from_events(events)
        message = AIMessage(content=content, tool_calls=tool_calls, response_metadata=response_metadata)
        return ChatResult(generations=[ChatGeneration(message=message)], llm_output=response_metadata)

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        body = self._request_body(messages, **kwargs)
        saw_text_delta = False
        tool_index = 0
        started = time.perf_counter()
        first_delta_logged = False
        logger.info("codex_sse: stream start model=%s", self.model_name)
        for event in self._iter_response_events(body):
            event_type = event.get("type")
            if event_type == "response.output_text.delta":
                delta = str(event.get("delta") or "")
                if not delta:
                    continue
                if not first_delta_logged:
                    first_delta_logged = True
                    logger.info("codex_sse: first delta after %.3fs", time.perf_counter() - started)
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
        logger.info("codex_sse: stream complete after %.3fs", time.perf_counter() - started)
        yield ChatGenerationChunk(message=AIMessageChunk(content="", chunk_position="last"))

    def _request_events(self, messages: list[BaseMessage], **kwargs: Any) -> list[dict[str, Any]]:
        body = self._request_body(messages, **kwargs)
        return list(self._iter_response_events(body))

    def _request_body(self, messages: list[BaseMessage], **kwargs: Any) -> dict[str, Any]:
        instructions, input_items = _messages_to_responses_input(messages)
        tools = [_responses_tool(tool) for tool in kwargs.get("tools") or []]
        return {
            "model": self.model_name,
            "instructions": instructions,
            "input": input_items,
            "tools": tools,
            "tool_choice": kwargs.get("tool_choice") or "auto",
            "parallel_tool_calls": bool(kwargs.get("parallel_tool_calls", True)),
            "reasoning": kwargs.get("reasoning"),
            "store": False,
            "stream": True,
            "include": list(kwargs.get("include") or CODEX_INCLUDE),
            "prompt_cache_key": self.session_id,
            "client_metadata": {"x-codex-installation-id": self.installation_id},
        }

    def _post(self, body: dict[str, Any]) -> Any:
        response = self._post_once(body)
        if int(getattr(response, "status_code", 0) or 0) == 401:
            credentials = codex_auth.codex_runtime_credentials(refresh_if_needed=False)
            if credentials.refresh_token:
                refreshed = codex_auth.refresh_codex_token(credentials.refresh_token)
                codex_auth.save_codex_oauth_tokens(refreshed)
                response = self._post_once(body)
        status_code = int(getattr(response, "status_code", 0) or 0)
        if status_code < 200 or status_code >= 300:
            raise RuntimeError(f"Codex Responses request failed with HTTP {status_code}: {_safe_response_text(response)}")
        return response

    def _iter_response_events(self, body: dict[str, Any]) -> Iterator[dict[str, Any]]:
        client = self.http_client or _new_http_client(self.timeout)
        owns_client = self.http_client is None
        try:
            retry_after_refresh = False
            with self._stream_once(client, body) as response:
                if int(getattr(response, "status_code", 0) or 0) == 401:
                    retry_after_refresh = self._refresh_access_token_if_possible()
                    if not retry_after_refresh:
                        self._raise_for_status(response)
                else:
                    self._raise_for_status(response)
                    yield from _iter_sse_events(response)
            if retry_after_refresh:
                with self._stream_once(client, body) as response:
                    self._raise_for_status(response)
                    yield from _iter_sse_events(response)
        except Exception as exc:
            logger.warning("codex_sse: stream failed: %s", exc)
            raise
        finally:
            if owns_client:
                client.close()

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
        credentials = codex_auth.codex_runtime_credentials(refresh_if_needed=False)
        if not credentials.refresh_token:
            return False
        refreshed = codex_auth.refresh_codex_token(credentials.refresh_token)
        codex_auth.save_codex_oauth_tokens(refreshed)
        return True

    def _raise_for_status(self, response: Any) -> None:
        status_code = int(getattr(response, "status_code", 0) or 0)
        if status_code < 200 or status_code >= 300:
            raise RuntimeError(f"Codex Responses request failed with HTTP {status_code}: {_safe_response_text(response)}")

    def _post_once(self, body: dict[str, Any]) -> Any:
        client = self.http_client or _new_http_client(self.timeout)
        owns_client = self.http_client is None
        try:
            return client.post(
                _responses_url(self.base_url),
                json=body,
                headers=self._headers(),
                timeout=self.timeout,
            )
        finally:
            if owns_client:
                client.close()

    def _headers(self) -> dict[str, str]:
        credentials = codex_auth.codex_runtime_credentials(refresh_if_needed=True)
        if not credentials.access_token:
            raise RuntimeError("Codex access token is missing. Connect ChatGPT in Settings -> Providers.")
        if not credentials.account_id:
            raise RuntimeError("Codex account id is missing. Reconnect ChatGPT in Settings -> Providers.")
        return {
            "Authorization": f"Bearer {credentials.access_token}",
            "ChatGPT-Account-ID": credentials.account_id,
            "originator": CODEX_ORIGINATOR,
            "session_id": self.session_id,
            "User-Agent": CODEX_USER_AGENT,
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
        }


def _new_http_client(timeout: float) -> Any:
    import httpx

    return httpx.Client(timeout=timeout)


def _responses_url(base_url: str) -> str:
    return f"{str(base_url or CODEX_RESPONSES_BASE_URL).rstrip('/')}/responses"


def _safe_response_text(response: Any) -> str:
    try:
        return str(getattr(response, "text", "") or "")[:300]
    except Exception:
        return ""


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
        if block_type in {"image_url", "input_image"} or isinstance(block.get("image"), dict):
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
        if not content:
            content = [_content_item(text, output=output)]
        input_items.append({
            "type": "message",
            "role": role,
            "content": content,
        })
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


def _parse_sse_response(response: Any) -> list[dict[str, Any]]:
    return list(_iter_sse_events(response))


def _iter_sse_events(response: Any) -> Iterator[dict[str, Any]]:
    current_event = ""
    data_lines: list[str] = []
    for raw_line in response.iter_lines():
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
    event = _sse_event_from_lines(current_event, data_lines)
    if event is not None:
        yield event


def _append_sse_event(events: list[dict[str, Any]], event_name: str, data_lines: list[str]) -> None:
    event = _sse_event_from_lines(event_name, data_lines)
    if event is not None:
        events.append(event)


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
    if isinstance(payload, dict):
        payload.setdefault("type", event_name)
        if payload.get("type") == "response.failed":
            raise RuntimeError(f"Codex Responses stream failed: {_response_error_message(payload)}")
        return payload
    return None


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
        for content_item in item.get("content") or []:
            if isinstance(content_item, dict) and content_item.get("type") == "output_text":
                parts.append(str(content_item.get("text") or ""))
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
    return {
        "name": name,
        "args": arguments,
        "id": call_id,
        "index": index,
    }


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
