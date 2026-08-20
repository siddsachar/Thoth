"""Provider-scoped validation for tool input schemas.

Tool producers use provider-neutral JSON Schema.  Provider transports can
support narrower dialects, so compatibility is enforced only at the final
model-binding boundary rather than mutating the shared declarations.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from pydantic import BaseModel
from pydantic.v1 import BaseModel as BaseModelV1

from row_bot.providers.models import TransportMode


@dataclass(frozen=True)
class ToolSchemaIssue:
    tool_name: str
    path: str
    code: str
    detail: str

    def format(self) -> str:
        return f"{self.tool_name}: {self.detail} at {self.path}"


@dataclass(frozen=True)
class ToolSchemaCompatibilityResult:
    tools: tuple[Any, ...]
    issues: tuple[ToolSchemaIssue, ...]
    rejected_tool_names: tuple[str, ...]
    enforced: bool


class ToolSchemaCompatibilityError(ValueError):
    """Raised when explicitly requested tools cannot bind to a provider."""

    def __init__(self, transport: TransportMode, issues: Sequence[ToolSchemaIssue]):
        self.transport = transport
        self.issues = tuple(issues)
        details = "; ".join(issue.format() for issue in self.issues)
        super().__init__(f"Tool schema is incompatible with {transport.value}: {details}")


_STRICT_ARRAY_ITEM_TRANSPORTS = frozenset({TransportMode.GOOGLE_GENAI})


def tool_input_schema(tool: Any) -> dict[str, Any]:
    """Return a tool's effective provider-facing input schema."""
    args_schema = getattr(tool, "args_schema", None)
    if isinstance(args_schema, Mapping):
        return dict(args_schema)
    if isinstance(args_schema, type) and issubclass(args_schema, BaseModel):
        return args_schema.model_json_schema()
    if isinstance(args_schema, type) and issubclass(args_schema, BaseModelV1):
        return args_schema.schema()

    get_input_schema = getattr(tool, "get_input_schema", None)
    if callable(get_input_schema):
        schema_model = get_input_schema()
        if isinstance(schema_model, type) and issubclass(schema_model, BaseModel):
            return schema_model.model_json_schema()
        if isinstance(schema_model, type) and issubclass(schema_model, BaseModelV1):
            return schema_model.schema()
        model_json_schema = getattr(schema_model, "model_json_schema", None)
        if callable(model_json_schema):
            schema = model_json_schema()
            if isinstance(schema, Mapping):
                return dict(schema)
    raise TypeError("tool does not expose a JSON input schema")


def _usable_items_schema(items: Any) -> bool:
    if not isinstance(items, Mapping) or not items:
        return False
    if "$ref" in items or "type" in items:
        return True
    for keyword in ("anyOf", "oneOf", "allOf"):
        branches = items.get(keyword)
        if isinstance(branches, list) and branches and all(
            _usable_items_schema(branch) for branch in branches
        ):
            return True
    return bool("enum" in items or "const" in items)


def _path_part(value: Any) -> str:
    text = str(value)
    if text.isidentifier():
        return f".{text}"
    return f"[{text!r}]"


def _array_item_issues(tool_name: str, schema: Any) -> tuple[ToolSchemaIssue, ...]:
    issues: list[ToolSchemaIssue] = []
    seen: set[int] = set()

    def visit(node: Any, path: str) -> None:
        if isinstance(node, Mapping):
            node_id = id(node)
            if node_id in seen:
                return
            seen.add(node_id)
            node_type = node.get("type")
            is_array = node_type == "array" or (
                isinstance(node_type, list) and "array" in node_type
            )
            if is_array:
                items = node.get("items")
                if not _usable_items_schema(items):
                    issues.append(
                        ToolSchemaIssue(
                            tool_name=tool_name,
                            path=f"{path}.items",
                            code="array_items_required",
                            detail="array parameter has no usable items schema",
                        )
                    )
            for key, value in node.items():
                visit(value, path + _path_part(key))
        elif isinstance(node, list):
            for index, value in enumerate(node):
                visit(value, f"{path}[{index}]")

    visit(schema, "$")
    return tuple(issues)


def inspect_tool_schema(tool: Any) -> tuple[ToolSchemaIssue, ...]:
    """Inspect a tool without applying any provider policy."""
    tool_name = str(getattr(tool, "name", "") or "<unnamed>")
    try:
        schema = tool_input_schema(tool)
    except Exception as exc:
        return (
            ToolSchemaIssue(
                tool_name=tool_name,
                path="$",
                code="schema_unavailable",
                detail=f"input schema could not be generated ({type(exc).__name__})",
            ),
        )
    return _array_item_issues(tool_name, schema)


def inspect_google_tool_schemas(
    tools: Iterable[Any],
) -> dict[str, tuple[ToolSchemaIssue, ...]]:
    """Inspect schemas produced by the locked Gemini adapter in one batch.

    The adapter can change schema shape while translating valid JSON Schema,
    so validate the effective consolidated-SDK declarations before binding.
    """
    tool_list = tuple(tools)
    try:
        # Coverage/source discovery can import Row-Bot's optional MCP package
        # before google-genai. Ensure Pydantic's lazy RootModel module remains
        # registered for google-genai's optional MCP types during that sequence.
        import pydantic.root_model  # noqa: F401
        from google.genai import types
        from langchain_google_genai._function_utils import (
            convert_to_genai_function_declarations,
        )
    except Exception as exc:
        detail = f"Gemini input schema could not be generated ({type(exc).__name__})"
        return {
            str(getattr(tool, "name", "") or "<unnamed>"): (
                ToolSchemaIssue(
                    tool_name=str(getattr(tool, "name", "") or "<unnamed>"),
                    path="$",
                    code="gemini_schema_conversion_failed",
                    detail=detail,
                ),
            )
            for tool in tool_list
        }

    def inspect_converted(converted) -> dict[str, tuple[ToolSchemaIssue, ...]]:
        result: dict[str, tuple[ToolSchemaIssue, ...]] = {}

        declarations = [
            declaration
            for converted_tool in converted
            for declaration in (converted_tool.function_declarations or ())
        ]
        for declaration in declarations:
            tool_name = str(declaration.name or "<unnamed>")
            issues: list[ToolSchemaIssue] = []

            def visit(node: Any, path: str) -> None:
                if isinstance(node, Mapping):
                    if node.get("type") == types.Type.ARRAY:
                        items = node.get("items")
                        if not isinstance(items, Mapping) or not items:
                            issues.append(
                                ToolSchemaIssue(
                                    tool_name=tool_name,
                                    path=f"{path}.items",
                                    code="array_items_required",
                                    detail="Gemini conversion produced an array parameter with no items schema",
                                )
                            )
                    for key, value in node.items():
                        visit(value, path + _path_part(key))
                elif isinstance(node, list):
                    for index, value in enumerate(node):
                        visit(value, f"{path}[{index}]")

            schema = declaration.parameters.model_dump(exclude_none=True)
            visit(schema, "$")
            if issues:
                result[tool_name] = tuple(issues)
        return result

    try:
        return inspect_converted(convert_to_genai_function_declarations(tool_list))
    except Exception:
        # A single malformed extension must not hide otherwise valid tools.
        result: dict[str, tuple[ToolSchemaIssue, ...]] = {}
        for tool in tool_list:
            tool_name = str(getattr(tool, "name", "") or "<unnamed>")
            try:
                converted = convert_to_genai_function_declarations([tool])
                result.update(inspect_converted(converted))
            except Exception as exc:
                result[tool_name] = (
                    ToolSchemaIssue(
                        tool_name=tool_name,
                        path="$",
                        code="gemini_schema_conversion_failed",
                        detail=f"Gemini input schema could not be generated ({type(exc).__name__})",
                    ),
                )
        return result


def inspect_google_tool_schema(tool: Any) -> tuple[ToolSchemaIssue, ...]:
    """Inspect one tool's effective Gemini schema."""
    tool_name = str(getattr(tool, "name", "") or "<unnamed>")
    return inspect_google_tool_schemas([tool]).get(tool_name, ())


def apply_tool_schema_compatibility(
    tools: Iterable[Any],
    transport: TransportMode | str,
    *,
    explicitly_requested_names: Iterable[str] = (),
) -> ToolSchemaCompatibilityResult:
    """Apply the selected transport's schema policy without rewriting tools.

    Non-Google transports are observation-only: their original tool objects and
    order are returned even when the generic inspection finds weak schemas.
    """
    tool_list = tuple(tools)
    try:
        resolved_transport = (
            transport
            if isinstance(transport, TransportMode)
            else TransportMode(str(transport))
        )
    except ValueError:
        resolved_transport = TransportMode.OPENAI_CHAT

    issues_by_name: dict[str, tuple[ToolSchemaIssue, ...]] = {}
    all_issues: list[ToolSchemaIssue] = []
    google_issues_by_name = (
        inspect_google_tool_schemas(tool_list)
        if resolved_transport in _STRICT_ARRAY_ITEM_TRANSPORTS
        else {}
    )
    for tool in tool_list:
        issues = list(inspect_tool_schema(tool))
        name = str(getattr(tool, "name", "") or "<unnamed>")
        issues.extend(google_issues_by_name.get(name, ()))
        deduplicated = tuple(
            {
                (issue.code, issue.path): issue
                for issue in issues
            }.values()
        )
        if not deduplicated:
            continue
        issues_by_name[name] = deduplicated
        all_issues.extend(deduplicated)

    if resolved_transport not in _STRICT_ARRAY_ITEM_TRANSPORTS:
        return ToolSchemaCompatibilityResult(
            tools=tool_list,
            issues=tuple(all_issues),
            rejected_tool_names=(),
            enforced=False,
        )

    rejected_names = tuple(
        dict.fromkeys(
            str(getattr(tool, "name", "") or "<unnamed>")
            for tool in tool_list
            if str(getattr(tool, "name", "") or "<unnamed>") in issues_by_name
        )
    )
    explicit = {str(name) for name in explicitly_requested_names}
    explicit_issues = [
        issue
        for name in rejected_names
        if name in explicit
        for issue in issues_by_name[name]
    ]
    if explicit_issues:
        raise ToolSchemaCompatibilityError(resolved_transport, explicit_issues)

    compatible = tuple(
        tool
        for tool in tool_list
        if str(getattr(tool, "name", "") or "<unnamed>") not in issues_by_name
    )
    if tool_list and not compatible:
        raise ToolSchemaCompatibilityError(resolved_transport, all_issues)
    return ToolSchemaCompatibilityResult(
        tools=compatible,
        issues=tuple(all_issues),
        rejected_tool_names=rejected_names,
        enforced=True,
    )
