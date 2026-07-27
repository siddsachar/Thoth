from __future__ import annotations

from row_bot.computer_use.client import CuaClient
import pytest

from row_bot.computer_use.service import ComputerUseError, ComputerUseService, LeaseOwner
from tests.fixtures.fake_cua import FakeCuaTransport


def test_single_interrupt_resume_is_bound_to_its_langgraph_id(monkeypatch) -> None:
    import row_bot.agent as agent

    inputs = []
    fake_agent = type(
        "FakeAgent",
        (),
        {
            "get_state": lambda self, _config: type("State", (), {"values": {}})(),
            "update_state": lambda self, _config, _values: None,
        },
    )()
    monkeypatch.setattr(agent, "get_agent_graph", lambda *_args, **_kwargs: fake_agent)

    def fake_stream(_graph, input_data, _config, **_kwargs):
        inputs.append(input_data)
        yield ("done", "resumed")

    monkeypatch.setattr(agent, "_stream_graph", fake_stream)

    events = list(
        agent.resume_stream_agent(
            [],
            {
                "configurable": {
                    "thread_id": "thread",
                    "generation_id": "generation",
                }
            },
            True,
            interrupt_ids=["interrupt-one"],
        )
    )

    assert events == [("done", "resumed")]
    assert inputs[0].resume == {"interrupt-one": True}


def test_non_streaming_resume_without_root_objective_uses_empty_fallback(monkeypatch) -> None:
    import row_bot.agent as agent

    state = type("State", (), {"next": (), "tasks": (), "values": {}})()
    fake_agent = type(
        "FakeAgent",
        (),
        {
            "stream": lambda self, *_args, **_kwargs: (),
            "get_state": lambda self, _config: state,
        },
    )()
    monkeypatch.setattr(agent, "get_agent_graph", lambda *_args, **_kwargs: fake_agent)
    monkeypatch.setattr(
        agent,
        "_resume_agent_graph_config",
        lambda _graph, config: config,
    )

    result = agent.resume_invoke_agent(
        [],
        {
            "configurable": {
                "thread_id": "thread",
                "generation_id": "generation",
            }
        },
        True,
        interrupt_ids=["interrupt-one"],
    )

    assert result == "I wasn't able to generate a response."
    assert agent.get_active_runtime_context()["root_objective"] == ""


def test_consequential_approval_payload_is_redacted_and_resume_recaptures(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path))
    from row_bot.computer_use.readiness import acknowledge_disclosure
    acknowledge_disclosure()
    transport = FakeCuaTransport()
    client = CuaClient("fake.exe", transport_factory=lambda *_args: transport)
    approvals = []
    service = ComputerUseService(client_factory=lambda: client, approval_callback=lambda payload: approvals.append(payload) or True)
    owner = LeaseOwner("thread", "generation", "task")
    service.acquire(owner, validate_context=False)
    target = service.list_windows(owner, app="Calculator")[0]["target_id"]
    observation = service.capture(target, owner)
    secret = "private message body"
    service.act("type", target, owner, element_token=observation.elements[1].token, text=secret, expected_effect="Submit message")

    assert secret not in str(approvals)
    type_index = next(i for i, (name, _args) in enumerate(transport.calls) if name == "type_text")
    assert transport.calls[type_index - 1][0] == "get_window_state"
    assert transport.calls[type_index + 1][0] == "get_window_state"


def test_consequential_denial_is_terminal_and_releases_the_lease(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path))
    from row_bot.computer_use.readiness import acknowledge_disclosure

    acknowledge_disclosure()
    transport = FakeCuaTransport()
    client = CuaClient("fake.exe", transport_factory=lambda *_args: transport)
    decisions = iter((True, False))
    service = ComputerUseService(
        client_factory=lambda: client,
        approval_callback=lambda _payload: next(decisions),
    )
    owner = LeaseOwner("thread", "generation", "task")
    service.acquire(owner, validate_context=False)
    target = service.list_windows(owner, app="Calculator")[0]["target_id"]
    observation = service.capture(target, owner)

    with pytest.raises(ComputerUseError, match="denied"):
        service.act(
            "type",
            target,
            owner,
            element_token=observation.elements[1].token,
            text="hidden",
            expected_effect="Submit message",
        )

    snapshot = service.status_snapshot()
    assert snapshot["active"] is False
    assert snapshot["state"] == "ready"
    assert snapshot["has_thumbnail"] is False
    assert "type_text" not in [name for name, _args in transport.calls]
