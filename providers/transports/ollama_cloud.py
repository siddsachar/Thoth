from __future__ import annotations

import json
from contextlib import nullcontext
from typing import Any, Iterator, Sequence

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable


OLLAMA_CLOUD_BASE_URL = "https://ollama.com"


def normalize_ollama_cloud_api_key(api_key: str | None) -> str:
    """Return a bare Ollama Cloud API key suitable for a Bearer header."""
    value = str(api_key or "").strip().strip('"').strip("'")
    lower = value.lower()
    if lower.startswith("bearer "):
        value = value[7:].strip()
    return value


def normalize_ollama_cloud_model_name(model_name: str | None) -> str:
    """Return the direct Ollama Cloud API model name.

    The local Ollama daemon uses tags such as ``gpt-oss:120b-cloud`` for
    cloud offload. The native ``https://ollama.com/api/chat`` endpoint uses
    the corresponding direct model name without the ``-cloud`` suffix.
    """
    value = str(model_name or "").strip()
    if ":" not in value:
        return value
    family, tag = value.rsplit(":", 1)
    if tag.lower() == "cloud":
        return family
    if tag.lower().endswith("-cloud"):
        return f"{family}:{tag[:-6]}"
    return value


class ChatOllamaCloud(BaseChatModel):
    """LangChain chat model backed by Ollama Cloud's native chat API."""

    model_name: str
    api_key: str
    base_url: str = OLLAMA_CLOUD_BASE_URL
    timeout: float = 120.0
    http_client: Any | None = None

    @property
    def _llm_type(self) -> str:
        return "ollama_cloud_chat"

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Any],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable:
        return self.bind(tools=[_ollama_tool(tool) for tool in tools], tool_choice=tool_choice or "auto", **kwargs)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        body = self._request_body(messages, stream=False, **kwargs)
        response = self._post(body)
        payload = response.json()
        message_payload = payload.get("message") if isinstance(payload.get("message"), dict) else {}
        content = str(message_payload.get("content") or "")
        tool_calls = _tool_calls_from_ollama(message_payload.get("tool_calls") or [])
        metadata = _response_metadata(payload)
        message = AIMessage(content=content, tool_calls=tool_calls, response_metadata=metadata)
        return ChatResult(generations=[ChatGeneration(message=message)], llm_output=metadata)

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        body = self._request_body(messages, stream=True, **kwargs)
        tool_index = 0
        for payload in self._iter_stream_events(body):
            message_payload = payload.get("message") if isinstance(payload.get("message"), dict) else {}
            content = str(message_payload.get("content") or "")
            if content:
                chunk = ChatGenerationChunk(message=AIMessageChunk(content=content))
                if run_manager:
                    run_manager.on_llm_new_token(content, chunk=chunk)
                yield chunk
            for call in message_payload.get("tool_calls") or []:
                chunk_payload = _tool_call_chunk_from_ollama(call, tool_index)
                if chunk_payload:
                    tool_index += 1
                    yield ChatGenerationChunk(message=AIMessageChunk(content="", tool_call_chunks=[chunk_payload]))
            if payload.get("done"):
                break
        yield ChatGenerationChunk(message=AIMessageChunk(content="", chunk_position="last"))

    def _request_body(self, messages: list[BaseMessage], *, stream: bool, **kwargs: Any) -> dict[str, Any]:
        accepts_tools = _model_accepts_tools(self.model_name)
        body: dict[str, Any] = {
            "model": normalize_ollama_cloud_model_name(self.model_name),
            "messages": [_ollama_message(message, include_tool_fields=accepts_tools) for message in messages],
            "stream": stream,
        }
        tools = kwargs.get("tools") or []
        if tools and accepts_tools:
            body["tools"] = [_ollama_tool(tool) for tool in tools]
            tool_choice = kwargs.get("tool_choice")
            if tool_choice and tool_choice != "auto":
                body["tool_choice"] = tool_choice
        return body

    def _post(self, body: dict[str, Any]) -> Any:
        client = self.http_client or _new_http_client(self.timeout)
        owns_client = self.http_client is None
        try:
            response = client.post(
                _chat_url(self.base_url),
                json=body,
                headers=self._headers(),
                timeout=self.timeout,
            )
            _raise_for_status(response)
            return response
        finally:
            if owns_client:
                client.close()

    def _iter_stream_events(self, body: dict[str, Any]) -> Iterator[dict[str, Any]]:
        client = self.http_client or _new_http_client(self.timeout)
        owns_client = self.http_client is None
        try:
            context = client.stream(
                "POST",
                _chat_url(self.base_url),
                json=body,
                headers=self._headers(),
                timeout=self.timeout,
            ) if hasattr(client, "stream") else nullcontext(client.post(
                _chat_url(self.base_url),
                json=body,
                headers=self._headers(),
                timeout=self.timeout,
            ))
            with context as response:
                _raise_for_status(response)
                for line in response.iter_lines():
                    if not line:
                        continue
                    if isinstance(line, bytes):
                        line = line.decode("utf-8", errors="replace")
                    try:
                        payload = json.loads(str(line))
                    except json.JSONDecodeError:
                        continue
                    if isinstance(payload, dict):
                        yield payload
        finally:
            if owns_client:
                client.close()

    def _headers(self) -> dict[str, str]:
        api_key = normalize_ollama_cloud_api_key(self.api_key)
        return {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }


def _new_http_client(timeout: float) -> Any:
    import httpx

    return httpx.Client(timeout=timeout)


def _chat_url(base_url: str) -> str:
    return f"{str(base_url or OLLAMA_CLOUD_BASE_URL).rstrip('/')}/api/chat"


def _raise_for_status(response: Any) -> None:
    status_code = int(getattr(response, "status_code", 0) or 0)
    if status_code < 200 or status_code >= 300:
        if status_code == 401:
            raise RuntimeError(
                "Ollama Cloud rejected the API key (HTTP 401). "
                "Update the Ollama Cloud API key in Settings -> Providers."
            )
        if status_code == 403:
            detail = _safe_response_text(response)
            suffix = f" Details: {detail}" if detail else ""
            raise RuntimeError(
                "Ollama Cloud refused this request (HTTP 403). "
                "The API key is being sent, but this account may not have access to the selected model "
                "or direct Ollama Cloud API usage. Try a different Ollama Cloud direct model, check the "
                "Ollama account/API key, or use local Ollama :cloud models through the signed-in Ollama daemon."
                f"{suffix}"
            )
        if status_code >= 500:
            detail = _safe_response_text(response)
            suffix = f" Details: {detail}" if detail else ""
            raise RuntimeError(
                f"Ollama Cloud returned a server error (HTTP {status_code}). "
                "The key was accepted, but the selected model or request shape failed upstream. "
                "Try a documented direct Ollama Cloud model such as gpt-oss:20b, or use local Ollama "
                ":cloud models through the signed-in Ollama daemon."
                f"{suffix}"
            )
        if status_code == 400:
            detail = _safe_response_text(response)
            suffix = f" Details: {detail}" if detail else ""
            raise RuntimeError(
                "Ollama Cloud rejected the chat request (HTTP 400). "
                "The selected model may not accept this message/tool payload. "
                "Try a simple chat with a direct Ollama Cloud model such as gpt-oss:20b, "
                "or use local Ollama :cloud models through the signed-in Ollama daemon."
                f"{suffix}"
            )
        raise RuntimeError(f"Ollama Cloud request failed with HTTP {status_code}: {_safe_response_text(response)}")


def _safe_response_text(response: Any) -> str:
    try:
        payload = response.json()
        if isinstance(payload, dict):
            for key in ("error", "message", "detail"):
                value = payload.get(key)
                if value:
                    return str(value)[:300]
            return str(payload)[:300]
    except Exception:
        pass
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


def _message_images(message: BaseMessage) -> list[str]:
    content = message.content
    images: list[str] = []
    if not isinstance(content, list):
        return images
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "image_url":
            continue
        image_url = item.get("image_url")
        url = ""
        if isinstance(image_url, dict):
            url = str(image_url.get("url") or "")
        elif isinstance(image_url, str):
            url = image_url
        if not url:
            continue
        if url.startswith("data:image") and "," in url:
            images.append(url.split(",", 1)[1])
        else:
            images.append(url)
    return images


def _ollama_message(message: BaseMessage, *, include_tool_fields: bool = True) -> dict[str, Any]:
    role = "user"
    if isinstance(message, SystemMessage):
        role = "system"
    elif isinstance(message, AIMessage):
        role = "assistant"
    elif isinstance(message, ToolMessage):
        role = "tool"
    elif isinstance(message, HumanMessage):
        role = "user"
    content = _message_text(message)
    if not include_tool_fields and isinstance(message, ToolMessage):
        name = str(getattr(message, "name", "") or getattr(message, "tool_call_id", "") or "tool")
        payload = {"role": "user", "content": f"[Tool result from {name}]: {content}"}
        return payload
    payload: dict[str, Any] = {"role": role, "content": content}
    images = _message_images(message)
    if images:
        payload["images"] = images
    if include_tool_fields and isinstance(message, AIMessage) and getattr(message, "tool_calls", None):
        payload["tool_calls"] = [
            {
                "type": "function",
                "function": {
                    "index": index,
                    "name": call.get("name") or "",
                    "arguments": call.get("args") or {},
                }
            }
            for index, call in enumerate(message.tool_calls)
            if isinstance(call, dict)
        ]
    if include_tool_fields and isinstance(message, ToolMessage):
        payload["tool_name"] = str(getattr(message, "name", "") or getattr(message, "tool_call_id", "") or "")
    return payload


def _ollama_tool(tool: dict[str, Any] | type | Any) -> dict[str, Any]:
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
    return {
        "type": "function",
        "function": {
            "name": str(name),
            "description": str(description or ""),
            "parameters": parameters,
        },
    }


def _model_accepts_tools(model_name: str) -> bool:
    normalized = normalize_ollama_cloud_model_name(model_name)
    try:
        from providers.ollama import is_ollama_tool_capable

        return is_ollama_tool_capable(normalized)
    except Exception:
        return False


def _tool_calls_from_ollama(calls: list[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, call in enumerate(calls):
        chunk = _tool_call_chunk_from_ollama(call, index)
        if not chunk:
            continue
        args = chunk["args"]
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        result.append({
            "name": chunk["name"],
            "args": args if isinstance(args, dict) else {},
            "id": chunk["id"],
            "type": "tool_call",
        })
    return result


def _tool_call_chunk_from_ollama(call: Any, index: int) -> dict[str, Any] | None:
    if not isinstance(call, dict):
        return None
    function = call.get("function") if isinstance(call.get("function"), dict) else call
    name = str(function.get("name") or "").strip()
    if not name:
        return None
    arguments = function.get("arguments") or {}
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments)
    call_id = str(call.get("id") or f"ollama_cloud_call_{index}")
    return {
        "name": name,
        "args": arguments,
        "id": call_id,
        "index": index,
    }


def _response_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for key in (
        "model",
        "created_at",
        "done_reason",
        "total_duration",
        "load_duration",
        "prompt_eval_count",
        "prompt_eval_duration",
        "eval_count",
        "eval_duration",
    ):
        if key in payload:
            metadata[key] = payload[key]
    if "prompt_eval_count" in payload or "eval_count" in payload:
        metadata["token_usage"] = {
            "prompt_tokens": int(payload.get("prompt_eval_count") or 0),
            "completion_tokens": int(payload.get("eval_count") or 0),
            "total_tokens": int(payload.get("prompt_eval_count") or 0) + int(payload.get("eval_count") or 0),
        }
    return metadata
