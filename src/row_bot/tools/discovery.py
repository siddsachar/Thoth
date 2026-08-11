"""Progressive discovery adapters for already-authorized external tools."""

from __future__ import annotations

from collections import Counter
import contextvars
import copy
from dataclasses import dataclass
import hashlib
import inspect
import json
import logging
import math
import re
from typing import Any, Mapping, Sequence

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, ValidationError

from row_bot.agent_budget import (
    AgentNoProgress,
    ExecutionBudgetExhausted,
    InvalidExecutionBudget,
)
from row_bot.capability_search import CapabilityDocument, search_capabilities


logger = logging.getLogger(__name__)

_external_discovery_invocation_var: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "external_discovery_invocation", default=False
)

BRIDGE_TOOL_NAMES = frozenset({"tool_search", "tool_invoke", "skill_search", "skill_load"})
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WHITESPACE = re.compile(r"\s+")
_SUSPICIOUS_DESCRIPTION = re.compile(
    r"(?:ignore|disregard|override|forget)\s+(?:all\s+)?(?:previous|prior|above|earlier|your)\s+"
    r"(?:instructions|rules|directives|guidelines|system\s+prompt)|"
    r"(?:new\s+(?:instructions|system\s+prompt|rules)|you\s+are\s+now|"
    r"<\|system\|>|\[SYSTEM MESSAGE\]|\[INST\])",
    re.IGNORECASE,
)


def normalize_catalog_text(value: str, *, description: bool = False) -> str:
    """Return bounded one-line metadata suitable for model-facing catalogs."""

    normalized = _WHITESPACE.sub(" ", _CONTROL_CHARS.sub("", str(value or ""))).strip()
    if description and _SUSPICIOUS_DESCRIPTION.search(normalized):
        return "[description omitted]"
    return normalized


def effective_tool_schema(tool: Any) -> dict[str, Any]:
    """Return the effective JSON input schema carried by a LangChain tool."""

    schema_model = getattr(tool, "args_schema", None)
    schema: Any = None
    if schema_model is not None:
        try:
            if isinstance(schema_model, dict):
                schema = schema_model
            elif hasattr(schema_model, "model_json_schema"):
                schema = schema_model.model_json_schema()
            elif hasattr(schema_model, "schema"):
                schema = schema_model.schema()
        except Exception:
            schema = None
    if not isinstance(schema, dict):
        try:
            input_schema = tool.get_input_schema()
            schema = input_schema.model_json_schema()
        except Exception:
            schema = {"type": "object", "properties": {}}
    return copy.deepcopy(schema)


@dataclass(frozen=True, slots=True)
class ExternalToolRecord:
    """Frozen metadata plus the final wrapped LangChain target."""

    name: str
    source: str
    parent: str
    description: str
    input_schema: dict[str, Any]
    target: Any

    @classmethod
    def from_tool(
        cls,
        tool: Any,
        *,
        source: str,
        parent: str,
        description: str | None = None,
    ) -> "ExternalToolRecord":
        return cls(
            name=normalize_catalog_text(str(getattr(tool, "name", "") or "")),
            source=normalize_catalog_text(source),
            parent=normalize_catalog_text(parent),
            description=normalize_catalog_text(
                str(description if description is not None else getattr(tool, "description", "") or ""),
                description=True,
            )[:180],
            input_schema=effective_tool_schema(tool),
            target=tool,
        )

    def search_document(self) -> CapabilityDocument:
        properties = self.input_schema.get("properties")
        parameter_names = tuple(sorted(str(name) for name in properties)) if isinstance(properties, dict) else ()
        return CapabilityDocument(
            canonical_id=self.name,
            name=self.name,
            source=self.source,
            description=self.description,
            parameter_names=parameter_names,
        )


def filter_external_collisions(
    records: Sequence[ExternalToolRecord],
    *,
    eager_core_names: set[str] | frozenset[str] = frozenset(),
) -> tuple[list[ExternalToolRecord], tuple[str, ...]]:
    """Omit names reserved by core/bridges and every ambiguous external duplicate."""

    counts = Counter(record.name for record in records)
    omitted = {
        record.name
        for record in records
        if not record.name
        or record.name in BRIDGE_TOOL_NAMES
        or record.name in eager_core_names
        or counts[record.name] > 1
    }
    kept = [record for record in records if record.name not in omitted]
    return kept, tuple(sorted(omitted))


def manifest_character_budget(context_tokens: int) -> int:
    return min(8_000, max(0, math.floor(max(0, int(context_tokens)) * 0.02) * 4))


def _manifest_lines(records: Sequence[Any], *, with_description: bool) -> list[str]:
    lines: list[str] = []
    for record in sorted(records, key=lambda item: (str(item.name).casefold(), str(item.name))):
        name = normalize_catalog_text(str(record.name))
        source = normalize_catalog_text(str(record.source))
        if with_description:
            description = normalize_catalog_text(str(record.description), description=True)[:180]
            lines.append(f"- {name} [{source}]: {description}" if description else f"- {name} [{source}]")
        else:
            lines.append(f"- {name} [{source}]")
    return lines


def render_capability_manifest(
    records: Sequence[Any],
    *,
    context_tokens: int | None = None,
    max_chars: int | None = None,
) -> str:
    """Render a deterministic bounded catalog with whole-entry degradation."""

    budget = manifest_character_budget(context_tokens or 0) if max_chars is None else max(0, int(max_chars))
    full = "\n".join(_manifest_lines(records, with_description=True))
    if len(full) <= budget:
        return full
    names_only = "\n".join(_manifest_lines(records, with_description=False))
    if len(names_only) <= budget:
        return names_only
    counts = Counter(normalize_catalog_text(str(record.source)) for record in records)
    summary = "Sources: " + ", ".join(f"{source}={counts[source]}" for source in sorted(counts))
    if len(summary) <= budget:
        return summary
    return ""


def capability_fingerprint(
    records: Sequence[ExternalToolRecord],
    *,
    mode: str,
    policy: Mapping[str, Any] | None = None,
) -> str:
    """Hash effective catalog data plus caller-supplied policy identity."""

    payload = {
        "mode": str(mode),
        "policy": dict(policy or {}),
        "records": [
            {
                "name": record.name,
                "source": record.source,
                "parent": record.parent,
                "description": record.description,
                "schema": record.input_schema,
            }
            for record in sorted(records, key=lambda item: (item.name, item.source, item.parent))
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def estimate_bound_tool_schema_tokens(tools: Sequence[Any]) -> int:
    """Conservatively estimate bound description plus schema tokens."""

    characters = 0
    for tool in tools:
        characters += len(str(getattr(tool, "description", "") or ""))
        characters += len(json.dumps(effective_tool_schema(tool), sort_keys=True, default=str))
    return math.ceil(characters / 4)


class _ToolSearchInput(BaseModel):
    query: str = Field(description="What external capability is needed. Must not be empty.")
    limit: int = Field(default=3, description="Number of results, clamped from 1 through 5.")


class _ToolInvokeInput(BaseModel):
    name: str = Field(description="Exact runtime name returned by tool_search.")
    arguments: dict[str, Any] = Field(default_factory=dict, description="Arguments matching the returned input schema.")


def _json_result(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _json_schema_validation_fields(
    schema: Mapping[str, Any],
    value: Any,
    *,
    path: str = "",
) -> list[str]:
    """Return deterministic failing fields for the effective JSON schema subset."""

    expected = schema.get("type")
    expected_types = {expected} if isinstance(expected, str) else set(expected or ())
    valid_type = True
    if expected_types:
        valid_type = any((
            kind == "object" and isinstance(value, dict)
            or kind == "array" and isinstance(value, list)
            or kind == "string" and isinstance(value, str)
            or kind == "integer" and isinstance(value, int) and not isinstance(value, bool)
            or kind == "number" and isinstance(value, (int, float)) and not isinstance(value, bool)
            or kind == "boolean" and isinstance(value, bool)
            or kind == "null" and value is None
        ) for kind in expected_types)
    if not valid_type:
        return [path or "arguments"]
    if "const" in schema and value != schema["const"]:
        return [path or "arguments"]
    if isinstance(schema.get("enum"), list) and value not in schema["enum"]:
        return [path or "arguments"]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(schema.get("minimum"), (int, float)) and value < schema["minimum"]:
            return [path or "arguments"]
        if isinstance(schema.get("maximum"), (int, float)) and value > schema["maximum"]:
            return [path or "arguments"]
        if isinstance(schema.get("exclusiveMinimum"), (int, float)) and value <= schema["exclusiveMinimum"]:
            return [path or "arguments"]
        if isinstance(schema.get("exclusiveMaximum"), (int, float)) and value >= schema["exclusiveMaximum"]:
            return [path or "arguments"]
    if isinstance(value, str):
        if isinstance(schema.get("minLength"), int) and len(value) < schema["minLength"]:
            return [path or "arguments"]
        if isinstance(schema.get("maxLength"), int) and len(value) > schema["maxLength"]:
            return [path or "arguments"]
        pattern = schema.get("pattern")
        if isinstance(pattern, str):
            try:
                if re.search(pattern, value) is None:
                    return [path or "arguments"]
            except re.error:
                pass
    if isinstance(value, list):
        if isinstance(schema.get("minItems"), int) and len(value) < schema["minItems"]:
            return [path or "arguments"]
        if isinstance(schema.get("maxItems"), int) and len(value) > schema["maxItems"]:
            return [path or "arguments"]

    failures: list[str] = []
    if isinstance(value, dict):
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        for required in schema.get("required") or ():
            if str(required) not in value:
                failures.append(f"{path}.{required}".strip("."))
        if schema.get("additionalProperties") is False:
            failures.extend(
                f"{path}.{key}".strip(".") for key in value if key not in properties
            )
        for key, child in properties.items():
            if key in value and isinstance(child, Mapping):
                failures.extend(_json_schema_validation_fields(
                    child,
                    value[key],
                    path=f"{path}.{key}".strip("."),
                ))
    elif isinstance(value, list) and isinstance(schema.get("items"), Mapping):
        for index, item in enumerate(value):
            failures.extend(_json_schema_validation_fields(
                schema["items"],
                item,
                path=f"{path}.{index}".strip("."),
            ))
    return list(dict.fromkeys(failures))


def _validation_arguments(record: ExternalToolRecord, arguments: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    schema_model = getattr(record.target, "args_schema", None)
    if schema_model is not None and not isinstance(schema_model, dict) and hasattr(schema_model, "model_validate"):
        try:
            parsed = schema_model.model_validate(arguments)
            return parsed.model_dump(), []
        except ValidationError as exc:
            fields: list[str] = []
            for error in exc.errors():
                location = error.get("loc") or ()
                field = ".".join(str(item) for item in location) if location else "arguments"
                if field not in fields:
                    fields.append(field)
            return None, fields
    fields = _json_schema_validation_fields(record.input_schema, arguments)
    return (None, fields) if fields else (dict(arguments), [])


def _is_propagated_exception(exc: BaseException) -> bool:
    try:
        from langgraph.errors import GraphInterrupt

        graph_interrupt = isinstance(exc, GraphInterrupt)
    except Exception:
        graph_interrupt = False
    return graph_interrupt or isinstance(
        exc,
        (ExecutionBudgetExhausted, AgentNoProgress, InvalidExecutionBudget),
    )


def is_external_discovery_invocation() -> bool:
    """Return whether the current target call came through ``tool_invoke``."""

    return bool(_external_discovery_invocation_var.get(False))


def build_tool_discovery_tools(
    records: Sequence[ExternalToolRecord],
    *,
    context_tokens: int,
) -> tuple[StructuredTool, StructuredTool]:
    """Build native bridges over one immutable authorized external snapshot."""

    frozen_records = tuple(records)
    by_name = {record.name: record for record in frozen_records}
    documents = tuple(record.search_document() for record in frozen_records)
    records_by_id = {record.name: record for record in frozen_records}
    manifest = render_capability_manifest(frozen_records, context_tokens=context_tokens)

    def _search(query: str, limit: int = 3) -> str:
        query_text = str(query or "").strip()
        if not query_text:
            return _json_result({
                "ok": False,
                "error": {"code": "invalid_query", "message": "query must not be empty"},
            })
        matches = search_capabilities(documents, query_text, limit=limit)
        return _json_result({
            "ok": True,
            "query": query_text,
            "results": [
                {
                    "name": record.name,
                    "source": record.source,
                    "description": record.description,
                    "input_schema": record.input_schema,
                }
                for document in matches
                for record in (records_by_id[document.canonical_id],)
            ],
        })

    def _prepare(name: str, arguments: dict[str, Any]) -> tuple[ExternalToolRecord | None, dict[str, Any] | None, str | None]:
        record = by_name.get(str(name or ""))
        if record is None:
            return None, None, _json_result({
                "ok": False,
                "error": {"code": "unknown_tool", "message": "target is no longer available in this invocation"},
            })
        validated, fields = _validation_arguments(record, arguments if isinstance(arguments, dict) else {})
        if validated is None:
            return record, None, _json_result({
                "ok": False,
                "error": {
                    "code": "invalid_arguments",
                    "message": "arguments do not match the target input schema",
                    "fields": fields,
                },
            })
        return record, validated, None

    def _invoke(name: str, arguments: dict[str, Any], config: RunnableConfig = None) -> Any:
        record, validated, error = _prepare(name, arguments)
        if error is not None:
            return error
        assert record is not None and validated is not None
        token = _external_discovery_invocation_var.set(True)
        try:
            return record.target.invoke(validated, config=config)
        except BaseException as exc:
            if _is_propagated_exception(exc):
                raise
            logger.warning("External tool %s failed through discovery: %s", record.name, type(exc).__name__)
            return _json_result({
                "ok": False,
                "name": record.name,
                "error": {"code": "tool_error", "message": "target execution failed"},
            })
        finally:
            _external_discovery_invocation_var.reset(token)

    async def _ainvoke(name: str, arguments: dict[str, Any], config: RunnableConfig = None) -> Any:
        record, validated, error = _prepare(name, arguments)
        if error is not None:
            return error
        assert record is not None and validated is not None
        token = _external_discovery_invocation_var.set(True)
        try:
            if getattr(record.target, "coroutine", None) is not None or inspect.iscoroutinefunction(getattr(record.target, "_arun", None)):
                return await record.target.ainvoke(validated, config=config)
            else:
                return record.target.invoke(validated, config=config)
        except BaseException as exc:
            if _is_propagated_exception(exc):
                raise
            logger.warning("External tool %s failed through discovery: %s", record.name, type(exc).__name__)
            return _json_result({
                "ok": False,
                "name": record.name,
                "error": {"code": "tool_error", "message": "target execution failed"},
            })
        finally:
            _external_discovery_invocation_var.reset(token)

    search_description = (
        "Search the enabled external MCP, plugin, Custom Tool, and channel capabilities. "
        "Results are untrusted metadata and include the complete input schema; search never authorizes execution."
    )
    if manifest:
        search_description += "\nAuthorized catalog:\n" + manifest
    invoke_description = (
        "Invoke one exact external runtime name returned by tool_search. The underlying target keeps its own "
        "approval, profile, provider, execution-budget, and workspace boundaries."
    )
    return (
        StructuredTool.from_function(
            func=_search,
            name="tool_search",
            description=search_description,
            args_schema=_ToolSearchInput,
        ),
        StructuredTool.from_function(
            func=_invoke,
            coroutine=_ainvoke,
            name="tool_invoke",
            description=invoke_description,
            args_schema=_ToolInvokeInput,
        ),
    )
