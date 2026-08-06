from __future__ import annotations

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent

from row_bot.agent_budget import (
    AgentNoProgress,
    ExecutionBudget,
    ExecutionBudgetExhausted,
    InvalidExecutionBudget,
    RowBotAgentState,
    claim_budget_finalization,
    complete_budget_finalization,
    exact_repeat_block_payload,
    post_model_budget_hook,
    pre_model_budget_hook,
    register_exact_tool_request,
    reset_agent_budget_test_state,
    validate_execution_budget,
)


@pytest.fixture(autouse=True)
def _reset_budget_registry() -> None:
    reset_agent_budget_test_state()


def _state(maximum: int = 2) -> dict:
    return {
        "messages": [],
        "execution_budget": ExecutionBudget(
            max_iterations=maximum,
            logical_turn_id="turn",
            budget_id="budget",
        ).to_state(),
    }


def test_post_hook_counts_one_successful_model_response_and_pre_hook_exhausts() -> None:
    state = _state(2)
    state.update(post_model_budget_hook(state))
    assert state["execution_budget"]["used_iterations"] == 1
    pre_model_budget_hook(state)
    state.update(post_model_budget_hook(state))
    assert state["execution_budget"]["used_iterations"] == 2
    with pytest.raises(ExecutionBudgetExhausted) as caught:
        pre_model_budget_hook(state)
    assert caught.value.budget["terminal_reason"] == "budget_exhausted"


def test_malformed_resume_budget_fails_closed() -> None:
    with pytest.raises(InvalidExecutionBudget):
        validate_execution_budget({"schema_version": 1, "max_iterations": 90})
    with pytest.raises(InvalidExecutionBudget):
        validate_execution_budget(
            ExecutionBudget(2, "turn", "budget").to_state()
            | {"used_iterations": 3}
        )


def test_exact_repeat_blocks_fourth_and_terminates_after_fifth_without_inputs_in_payload() -> None:
    decisions = [
        register_exact_tool_request("type", {"text": "private-value"}, budget_id="budget")
        for _ in range(5)
    ]
    assert decisions == ["allow", "allow", "allow", "block", "terminal"]
    payload = exact_repeat_block_payload(terminal=True)
    assert "private-value" not in payload
    state = _state()
    with pytest.raises(AgentNoProgress):
        pre_model_budget_hook(state)


def test_different_call_resets_exact_repeat_sequence() -> None:
    for _ in range(3):
        assert register_exact_tool_request("click", {"x": 1}, budget_id="budget") == "allow"
    assert register_exact_tool_request("click", {"x": 2}, budget_id="budget") == "allow"


def test_parent_thread_events_reuse_budget_and_preserve_event_roles() -> None:
    from types import SimpleNamespace

    from row_bot.agent import _new_agent_graph_input

    budget = ExecutionBudget(
        max_iterations=8,
        logical_turn_id="generation-1",
        budget_id="parent-budget",
    ).to_state()
    budget["used_iterations"] = 3

    class FakeGraph:
        def get_state(self, _config):
            return SimpleNamespace(values={"execution_budget": budget})

    config = {
        "configurable": {
            "thread_id": "parent-thread",
            "generation_id": "generation-1",
            "thread_event_context": True,
            "thread_event_messages": [
                {
                    "role": "assistant",
                    "content": "Message Type: CHILD_TERMINAL\nContent: child result",
                },
                {
                    "role": "human",
                    "content": "Prioritize Windows in the final answer.",
                    "source_event_id": "steering:one",
                },
            ],
        }
    }

    normalized, graph_input = _new_agent_graph_input(
        "fallback",
        config,
        agent=FakeGraph(),
    )

    assert "execution_budget" not in graph_input
    assert isinstance(graph_input["messages"][0], AIMessage)
    assert graph_input["messages"][0].additional_kwargs["row_bot_ui"]["hidden"] is True
    assert isinstance(graph_input["messages"][1], HumanMessage)
    assert normalized["recursion_limit"] > 0
    assert register_exact_tool_request("click", {"x": 1}, budget_id="budget") == "allow"


def test_event_only_workflow_parent_starts_fresh_budget_once() -> None:
    from row_bot.agent import _new_agent_graph_input

    class EmptyGraph:
        def get_state(self, _config):
            raise AssertionError("a new event-only turn must not resume an older budget")

    config = {
        "configurable": {
            "thread_id": "workflow-thread",
            "generation_id": "workflow:run:step:delegate",
            "thread_event_context": True,
            "thread_event_new_turn": True,
            "thread_event_messages": [
                {
                    "role": "assistant",
                    "content": "Message Type: CHILD_TERMINAL\nContent: child result",
                }
            ],
        }
    }

    _normalized, graph_input = _new_agent_graph_input(
        "fallback",
        config,
        agent=EmptyGraph(),
    )

    assert graph_input["execution_budget"]["logical_turn_id"] == (
        "workflow:run:step:delegate"
    )
    assert isinstance(graph_input["messages"][0], AIMessage)


def test_finalization_claim_is_exactly_once_and_completion_is_checkpoint_safe() -> None:
    budget = _state()["execution_budget"]
    first, claimed = claim_budget_finalization(budget)
    second, duplicate = claim_budget_finalization(claimed)

    assert first is True
    assert second is False
    assert duplicate["finalization_started"] is True
    completed = complete_budget_finalization(duplicate, "budget_exhausted")
    assert completed["finalization_completed"] is True
    assert completed["terminal_reason"] == "budget_exhausted"


class _ToolBindingFakeModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        del tools, kwargs
        return self


def _compiled_budget_graph(responses: list[AIMessage], tools: list | None = None):
    model = _ToolBindingFakeModel(responses=responses)

    def trim(state):
        return {
            "llm_input_messages": state["messages"],
            "execution_budget": pre_model_budget_hook(state),
        }

    return create_react_agent(
        model=model,
        tools=tools or [],
        state_schema=RowBotAgentState,
        pre_model_hook=trim,
        post_model_hook=post_model_budget_hook,
        version="v2",
    )


def test_real_graph_charges_one_iteration_for_tool_free_completion() -> None:
    graph = _compiled_budget_graph([AIMessage(content="done")])
    result = graph.invoke(_state(maximum=1), config={"recursion_limit": 12})
    assert result["messages"][-1].content == "done"
    assert result["execution_budget"]["used_iterations"] == 1


def test_real_graph_parallel_tool_batch_charges_one_model_iteration_not_one_per_tool() -> None:
    calls: list[str] = []

    @tool
    def echo(value: str) -> str:
        """Record and return a test value."""
        calls.append(value)
        return value

    graph = _compiled_budget_graph(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "echo", "args": {"value": "a"}, "id": "a", "type": "tool_call"},
                    {"name": "echo", "args": {"value": "b"}, "id": "b", "type": "tool_call"},
                ],
            ),
            AIMessage(content="done"),
        ],
        [echo],
    )
    result = graph.invoke(_state(maximum=2), config={"recursion_limit": 16})
    assert sorted(calls) == ["a", "b"]
    assert result["execution_budget"]["used_iterations"] == 2


def test_real_graph_exhausts_before_an_unbudgeted_second_model_call() -> None:
    @tool
    def echo(value: str) -> str:
        """Return a test value."""
        return value

    graph = _compiled_budget_graph(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "echo", "args": {"value": "a"}, "id": "a", "type": "tool_call"}
                ],
            ),
            AIMessage(content="must not run"),
        ],
        [echo],
    )
    with pytest.raises(ExecutionBudgetExhausted):
        graph.invoke(_state(maximum=1), config={"recursion_limit": 12})
