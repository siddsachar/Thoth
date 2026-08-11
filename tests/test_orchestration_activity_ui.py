from __future__ import annotations

from pathlib import Path


def test_sidebar_activity_label_only_spins_for_blocking_orchestration() -> None:
    from row_bot.ui.sidebar import _orchestration_activity_label

    assert _orchestration_activity_label(
        {"state": "active", "blocking": True, "phase": "child_running"}
    ) == "Child Agents working"
    assert _orchestration_activity_label(
        {"state": "active", "background": True, "phase": "background"}
    ) == "Background Agent working"
    assert _orchestration_activity_label(
        {"state": "terminal", "phase": "completed"}
    ) == ""

    source = Path("src/row_bot/ui/sidebar.py").read_text(encoding="utf-8")
    assert "get_thread_orchestration_activity" in source
    assert 'bool(agent_activity.get("blocking"))' in source
    assert "_render_thread_activity_indicator(activity_label)" in source


def test_mobile_thread_lists_use_same_durable_blocking_activity() -> None:
    mobile_chat = Path("src/row_bot/ui/mobile_chat.py").read_text(encoding="utf-8")
    mobile = Path("src/row_bot/ui/mobile.py").read_text(encoding="utf-8")

    for source in (mobile_chat, mobile):
        assert "get_thread_orchestration_activity" in source
        assert 'agent_activity.get("blocking")' in source
        assert 'aria-label="Child Agents working"' in source


def test_agent_drawer_and_streaming_share_durable_orchestration_state() -> None:
    drawer = Path("src/row_bot/ui/agent_drawer.py").read_text(encoding="utf-8")
    streaming = Path("src/row_bot/ui/streaming.py").read_text(encoding="utf-8")

    assert "get_thread_orchestration_activity" in drawer
    assert '"later_wave_parent": "Preparing next wave"' in drawer
    done_block = streaming.split('elif event_type == "done":', 1)[1].split(
        'elif event_type == "orchestration_waiting":', 1
    )[0]
    finalization_block = streaming.split(
        "if _orchestration_suspended:", 1
    )[1].split('_voice_diag("generation_finalizing")', 1)[0]
    assert "GENERATION_DONE" not in done_block
    assert "ORCHESTRATION_ACTIVE" in finalization_block
    assert "GENERATION_DONE" in finalization_block


def test_agent_poll_refresh_keys_ignore_heartbeat_and_route_semantic_changes() -> None:
    from row_bot.ui.streaming import _agent_poll_refresh_keys

    run = {
        "id": "run-1",
        "status": "running",
        "status_message": "Working",
        "summary": "",
        "error": "",
        "turns_used": 1,
        "finished_at": "",
        "stop_requested": 0,
        "updated_at": "first",
        "heartbeat_at": "first",
        "active_seconds": 1.0,
        "model_iterations_used": 1,
    }
    orchestration = {
        "id": "orch-1",
        "status": "waiting_children",
        "parent_state": "waiting",
        "error_message": "",
        "completed_at": "",
        "updated_at": "first",
    }
    activity = {
        "orchestration_id": "orch-1",
        "state": "active",
        "blocking": True,
        "background": False,
        "phase": "child_running",
        "active_members": 1,
        "failed_members": 0,
    }
    base = _agent_poll_refresh_keys(
        run_rows=[run],
        orchestration_rows=[orchestration],
        orchestration_messages=[],
        activity=activity,
        checkpoint_revision="checkpoint-1",
    )

    volatile = _agent_poll_refresh_keys(
        run_rows=[
            {
                **run,
                "updated_at": "second",
                "heartbeat_at": "second",
                "active_seconds": 99.0,
                "model_iterations_used": 9,
            }
        ],
        orchestration_rows=[{**orchestration, "updated_at": "second"}],
        orchestration_messages=[],
        activity=activity,
        checkpoint_revision="checkpoint-1",
    )
    assert volatile == base

    checkpoint = _agent_poll_refresh_keys(
        run_rows=[run],
        orchestration_rows=[orchestration],
        orchestration_messages=[],
        activity=activity,
        checkpoint_revision="checkpoint-2",
    )
    assert checkpoint["sidebar"] == base["sidebar"]
    assert checkpoint["strip"] == base["strip"]
    assert checkpoint["transcript"] != base["transcript"]

    summary = _agent_poll_refresh_keys(
        run_rows=[{**run, "summary": "Finished one visible step"}],
        orchestration_rows=[orchestration],
        orchestration_messages=[],
        activity=activity,
        checkpoint_revision="checkpoint-1",
    )
    assert summary["sidebar"] == base["sidebar"]
    assert summary["strip"] != base["strip"]
    assert summary["transcript"] != base["transcript"]

    later_wave = _agent_poll_refresh_keys(
        run_rows=[run],
        orchestration_rows=[orchestration],
        orchestration_messages=[],
        activity={**activity, "phase": "later_wave_parent", "active_members": 0},
        checkpoint_revision="checkpoint-1",
    )
    assert later_wave["sidebar"] != base["sidebar"]
    assert later_wave["strip"] != base["strip"]
    assert later_wave["transcript"] == base["transcript"]

    approval_wait = _agent_poll_refresh_keys(
        run_rows=[run],
        orchestration_rows=[orchestration],
        orchestration_messages=[],
        activity={**activity, "phase": "approval_wait"},
        checkpoint_revision="checkpoint-1",
    )
    assert approval_wait["sidebar"] != base["sidebar"]
    assert approval_wait["strip"] != base["strip"]
    assert approval_wait["transcript"] != base["transcript"]
