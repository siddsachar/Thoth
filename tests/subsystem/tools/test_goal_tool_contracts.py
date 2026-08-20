from __future__ import annotations

import pytest

from row_bot.tools.goal_tool import GoalTool, _GoalUpdateInput
from row_bot.tools.row_bot_status_tool import RowBotStatusTool


pytestmark = pytest.mark.subsystem


def test_goal_arrays_advertise_string_items_through_google_conversion() -> None:
    from google.genai import types
    from langchain_google_genai._function_utils import convert_to_genai_function_declarations

    schema = _GoalUpdateInput.model_json_schema()

    for field_name in ("evidence", "blockers"):
        field_schema = schema["properties"][field_name]
        assert field_schema["type"] == "array"
        assert field_schema["items"] == {"type": "string"}

    converted_tools = convert_to_genai_function_declarations(
        GoalTool().as_langchain_tools() + RowBotStatusTool().as_langchain_tools()
    )
    declarations = [
        declaration
        for converted_tool in converted_tools
        for declaration in (converted_tool.function_declarations or ())
    ]
    goal_update = next(item for item in declarations if item.name == "goal_update")
    assert goal_update.parameters.properties["evidence"].items.type == types.Type.STRING
    assert goal_update.parameters.properties["blockers"].items.type == types.Type.STRING


def test_goal_arrays_normalize_legacy_rich_values_deterministically() -> None:
    model = _GoalUpdateInput.model_validate({
        "evidence": ["kept", {"z": 2, "a": 1}, 7, True, None],
        "blockers": {"decision": "needed", "priority": 2},
    })

    assert model.evidence == ["kept", '{"a":1,"z":2}', "7", "true", "null"]
    assert model.blockers == ['{"decision":"needed","priority":2}']


def test_goal_array_defaults_are_not_shared() -> None:
    first = _GoalUpdateInput()
    second = _GoalUpdateInput()

    first.evidence.append("first")
    first.blockers.append("blocked")

    assert second.evidence == []
    assert second.blockers == []
