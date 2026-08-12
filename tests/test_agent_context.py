from __future__ import annotations

import importlib


def _profile(max_context_tokens: int = 0, default_context_mode: str = "focused") -> dict:
    return {
        "id": "builtin:review",
        "slug": "review",
        "display_name": "Review",
        "instructions": "Review carefully.",
        "handoff_contract": "Return findings first.",
        "context_policy_json": {
            "default_context_mode": default_context_mode,
            "max_context_tokens": max_context_tokens,
        },
    }


def _patch_parent_context(monkeypatch, messages=None, summary="Parent summary."):
    import row_bot.threads as threads

    monkeypatch.setattr(
        threads,
        "get_latest_checkpoint_messages",
        lambda thread_id: list(messages or [
            {"role": "user", "content": "Please inspect auth."},
            {"role": "assistant", "content": "I will review it."},
            {"role": "user", "content": "Focus on token handling."},
        ]),
    )
    monkeypatch.setattr(
        threads,
        "load_validated_summary_state",
        lambda thread_id, mode, messages=None: (
            {"summary": summary, "boundary_message_count": 3}
            if mode == "agent"
            else None
        ),
    )


def test_focused_context_excludes_parent_transcript(monkeypatch):
    import row_bot.agent_context as agent_context

    agent_context = importlib.reload(agent_context)
    _patch_parent_context(monkeypatch)

    packet = agent_context.build_child_agent_prompt(
        objective="Review auth.",
        profile_snapshot=_profile(),
        context="Changed files: auth.py",
        context_mode="focused",
        parent_thread_id="parent-thread",
    )

    assert packet["mode"] == "focused"
    assert "CONTEXT PACKET" in packet["prompt"]
    assert "Changed files: auth.py" in packet["prompt"]
    assert "RECENT PARENT TURNS" not in packet["prompt"]
    assert "Please inspect auth" not in packet["prompt"]


def test_recent_context_includes_summary_and_recent_turns(monkeypatch):
    import row_bot.agent_context as agent_context

    agent_context = importlib.reload(agent_context)
    _patch_parent_context(monkeypatch)

    packet = agent_context.build_child_agent_prompt(
        objective="Review auth.",
        profile_snapshot=_profile(),
        context_mode="recent",
        parent_thread_id="parent-thread",
    )

    assert packet["mode"] == "recent"
    assert "PARENT SUMMARY" in packet["prompt"]
    assert "Parent summary." in packet["prompt"]
    assert '<HISTORICAL_CONTEXT untrusted="true">' in packet["prompt"]
    assert "RECENT PARENT TURNS" in packet["prompt"]
    assert "User: Focus on token handling." in packet["prompt"]


def test_child_prompt_includes_runtime_model_boundary(monkeypatch):
    import row_bot.agent_context as agent_context

    agent_context = importlib.reload(agent_context)
    _patch_parent_context(monkeypatch)

    packet = agent_context.build_child_agent_prompt(
        objective="Review auth.",
        profile_snapshot=_profile(),
        context_mode="focused",
        parent_thread_id="parent-thread",
        model_override="model:claude_subscription:claude-opus-4-8",
    )

    assert "RUNTIME MODEL:" in packet["prompt"]
    assert "Runtime model override: model:claude_subscription:claude-opus-4-8" in packet["prompt"]
    assert "Do not call row_bot_update_setting(setting='model')" in packet["prompt"]


def test_full_context_falls_back_to_recent_when_over_budget(monkeypatch):
    import row_bot.agent_context as agent_context

    agent_context = importlib.reload(agent_context)
    messages = [
        {"role": "user", "content": "x" * 400},
        {"role": "assistant", "content": "y" * 400},
        {"role": "user", "content": "z" * 400},
    ]
    _patch_parent_context(monkeypatch, messages=messages)

    packet = agent_context.build_child_agent_prompt(
        objective="Review huge transcript.",
        profile_snapshot=_profile(max_context_tokens=20),
        context_mode="full",
        parent_thread_id="parent-thread",
    )

    assert packet["mode"] == "recent"
    assert packet["fallback"] == "full_to_recent_summary"
    assert "CONTEXT FALLBACK" in packet["prompt"]
    assert "FULL PARENT TRANSCRIPT" not in packet["prompt"]


def test_empty_and_auto_modes(monkeypatch):
    import row_bot.agent_context as agent_context

    agent_context = importlib.reload(agent_context)
    _patch_parent_context(monkeypatch)

    empty = agent_context.build_child_agent_prompt(
        objective="Start fresh.",
        profile_snapshot=_profile(),
        context="Should not be included.",
        context_mode="empty",
        parent_thread_id="parent-thread",
    )
    assert empty["mode"] == "empty"
    assert "Should not be included." not in empty["prompt"]

    auto_with_context = agent_context.build_child_agent_prompt(
        objective="Use supplied facts.",
        profile_snapshot=_profile(default_context_mode="auto"),
        context="A focused fact.",
        context_mode="auto",
        parent_thread_id="parent-thread",
    )
    assert auto_with_context["mode"] == "focused"

    auto_with_transcript = agent_context.build_child_agent_prompt(
        objective="Use recent thread.",
        profile_snapshot=_profile(default_context_mode="auto"),
        context_mode="auto",
        parent_thread_id="parent-thread",
    )
    assert auto_with_transcript["mode"] == "recent"


def test_message_to_text_handles_common_shapes():
    import row_bot.agent_context as agent_context

    assert agent_context.message_to_text({"role": "user", "content": "hello"}) == "User: hello"
    assert agent_context.message_to_text(("assistant", "hi")) == "Assistant: hi"
