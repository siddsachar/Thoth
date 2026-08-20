from __future__ import annotations

from typing import Any

from langchain_core.tools import StructuredTool

from row_bot.tools.gmail_tool import _CreateDraftInput, _SendMessageInput


def _tool(name: str, schema) -> StructuredTool:
    def _run(**kwargs: Any) -> str:
        return str(kwargs)

    return StructuredTool.from_function(
        func=_run,
        name=name,
        description=f"{name} schema contract",
        args_schema=schema,
    )


def test_gmail_recipient_fields_advertise_typed_arrays() -> None:
    for schema in (_CreateDraftInput, _SendMessageInput):
        properties = schema.model_json_schema()["properties"]
        assert properties["to"]["type"] == "array"
        assert properties["to"]["items"] == {"type": "string"}
        for field_name in ("cc", "bcc"):
            array_branch = next(
                branch
                for branch in properties[field_name]["anyOf"]
                if branch.get("type") == "array"
            )
            assert array_branch["items"] == {"type": "string"}


def test_gmail_recipient_fields_keep_legacy_scalar_inputs() -> None:
    model = _CreateDraftInput.model_validate({
        "message": "Hello",
        "subject": "Subject",
        "to": "to@example.com",
        "cc": "cc@example.com",
        "bcc": ["bcc@example.com"],
    })

    assert model.to == ["to@example.com"]
    assert model.cc == ["cc@example.com"]
    assert model.bcc == ["bcc@example.com"]


def test_gmail_tools_retain_items_after_locked_google_conversion() -> None:
    from google.genai import types
    from langchain_google_genai._function_utils import convert_to_genai_function_declarations

    converted_tools = convert_to_genai_function_declarations([
        _tool("create_gmail_draft", _CreateDraftInput),
        _tool("send_gmail_message", _SendMessageInput),
    ])
    declarations = [
        declaration
        for converted_tool in converted_tools
        for declaration in (converted_tool.function_declarations or ())
    ]

    for declaration in declarations:
        properties = declaration.parameters.model_dump(exclude_none=True)["properties"]
        for field_name in ("to", "cc", "bcc", "attachments"):
            assert properties[field_name]["type"] == types.Type.ARRAY
            assert properties[field_name]["items"]["type"] == types.Type.STRING
