from __future__ import annotations

from typing import Any

import pytest
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from row_bot.providers.models import TransportMode
from row_bot.providers.tool_schema import (
    ToolSchemaCompatibilityError,
    apply_tool_schema_compatibility,
    inspect_tool_schema,
)


pytestmark = pytest.mark.subsystem


class _TypedArgs(BaseModel):
    values: list[str] = Field(default_factory=list)


class _UntypedArgs(BaseModel):
    values: list[Any] = Field(default_factory=list)


class _NestedUntypedArgs(BaseModel):
    payload: dict[str, list[Any]] = Field(default_factory=dict)


class _DescriptionOnlyItemsArgs(BaseModel):
    model_config = {
        "json_schema_extra": {
            "properties": {
                "values": {
                    "type": "array",
                    "items": {"description": "not a type"},
                }
            }
        }
    }

    values: list[str] = Field(default_factory=list)


class _StringOrArrayArgs(BaseModel):
    to: str | list[str]


def _tool(name: str, schema: type[BaseModel]) -> StructuredTool:
    def _run(**_kwargs: Any) -> str:
        return "ok"

    return StructuredTool.from_function(
        func=_run,
        name=name,
        description=f"{name} schema test",
        args_schema=schema,
    )


def test_inspector_reports_exact_nested_array_item_path() -> None:
    issues = inspect_tool_schema(_tool("nested", _NestedUntypedArgs))

    assert [(issue.code, issue.path) for issue in issues] == [
        ("array_items_required", "$.properties.payload.additionalProperties.items"),
    ]


def test_inspector_rejects_non_empty_but_unusable_items_schema() -> None:
    issues = inspect_tool_schema(_tool("description_only", _DescriptionOnlyItemsArgs))

    assert [(issue.code, issue.path) for issue in issues] == [
        ("array_items_required", "$.properties.values.items"),
    ]


def test_google_filters_invalid_optional_tool_and_preserves_order() -> None:
    first = _tool("first", _TypedArgs)
    malformed = _tool("malformed", _UntypedArgs)
    last = _tool("last", _TypedArgs)

    result = apply_tool_schema_compatibility(
        [first, malformed, last],
        TransportMode.GOOGLE_GENAI,
    )

    assert result.enforced is True
    assert result.tools == (first, last)
    assert result.rejected_tool_names == ("malformed",)
    assert result.issues[0].path == "$.properties.values.items"


def test_google_preserves_valid_union_after_consolidated_adapter_conversion() -> None:
    valid = _tool("valid", _TypedArgs)
    union_tool = _tool("union_tool", _StringOrArrayArgs)

    assert inspect_tool_schema(union_tool) == ()
    result = apply_tool_schema_compatibility(
        [valid, union_tool],
        TransportMode.GOOGLE_GENAI,
    )

    assert result.tools == (valid, union_tool)
    assert result.rejected_tool_names == ()
    assert result.issues == ()


def test_google_fails_for_explicitly_requested_invalid_tool() -> None:
    malformed = _tool("malformed", _UntypedArgs)

    with pytest.raises(ToolSchemaCompatibilityError, match=r"malformed.*values\.items"):
        apply_tool_schema_compatibility(
            [malformed],
            TransportMode.GOOGLE_GENAI,
            explicitly_requested_names={"malformed"},
        )


@pytest.mark.parametrize(
    "transport",
    [
        TransportMode.OPENAI_CHAT,
        TransportMode.OPENAI_RESPONSES,
        TransportMode.ANTHROPIC_MESSAGES,
        TransportMode.OLLAMA_CHAT,
        TransportMode.OLLAMA_CLOUD_CHAT,
    ],
)
def test_non_google_transports_keep_original_tools_unchanged(transport: TransportMode) -> None:
    first = _tool("first", _TypedArgs)
    malformed = _tool("malformed", _UntypedArgs)
    original = [first, malformed]

    result = apply_tool_schema_compatibility(original, transport)

    assert result.enforced is False
    assert result.tools == tuple(original)
    assert result.rejected_tool_names == ()
    assert result.issues
    assert all(actual is expected for actual, expected in zip(result.tools, original, strict=True))


def test_empty_tool_list_remains_valid_for_google() -> None:
    result = apply_tool_schema_compatibility([], TransportMode.GOOGLE_GENAI)

    assert result.enforced is True
    assert result.tools == ()
    assert result.issues == ()


def test_all_core_builtin_arrays_have_typed_items() -> None:
    from row_bot.tools import registry

    failures: list[str] = []
    declaration_count = 0
    for owner in registry.get_all_tools():
        for tool in owner.as_langchain_tools():
            declaration_count += 1
            for issue in inspect_tool_schema(tool):
                if issue.code == "array_items_required":
                    failures.append(issue.format())

    assert declaration_count >= 60
    assert failures == []


def test_locked_google_conversion_keeps_items_for_every_core_builtin_array() -> None:
    from google.genai import types
    from langchain_google_genai._function_utils import convert_to_genai_function_declarations
    from row_bot.tools import registry

    tools = [
        tool
        for owner in registry.get_all_tools()
        for tool in owner.as_langchain_tools()
    ]
    converted_tools = convert_to_genai_function_declarations(tools)
    declarations = [
        declaration
        for converted_tool in converted_tools
        for declaration in (converted_tool.function_declarations or ())
    ]
    failures: list[str] = []

    def visit(tool_name: str, node: Any, path: str) -> None:
        if isinstance(node, dict):
            if node.get("type") == types.Type.ARRAY and not node.get("items"):
                failures.append(f"{tool_name}: {path}.items")
            for key, value in node.items():
                visit(tool_name, value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                visit(tool_name, value, f"{path}[{index}]")

    for declaration in declarations:
        schema = declaration.parameters.model_dump(exclude_none=True)
        visit(declaration.name, schema, "$")

    assert len(declarations) >= 60
    assert failures == []
