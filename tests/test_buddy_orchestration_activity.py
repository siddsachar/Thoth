from __future__ import annotations


def test_buddy_reconstructs_and_clears_durable_orchestration_lane(monkeypatch):
    import row_bot.agent_orchestrator as orchestrator
    import row_bot.buddy.brain as brain_mod
    import row_bot.tasks as tasks
    from row_bot.buddy.brain import BuddyBrain
    from row_bot.buddy.events import BuddyEventType

    now = 1000.0
    monkeypatch.setattr(
        brain_mod,
        "get_buddy_config",
        lambda: {"enabled": True, "mode": "sidebar", "pack_id": "glyph"},
    )
    monkeypatch.setattr(brain_mod.time, "time", lambda: now)
    monkeypatch.setattr(tasks, "get_pending_approvals", lambda: [])
    monkeypatch.setattr(tasks, "get_running_tasks", lambda: {})
    activity = {
        "parent-thread": {
            "orchestration_id": "orchestration-1",
            "state": "active",
            "blocking": True,
            "background": False,
            "phase": "child_running",
            "active_members": 2,
            "failed_members": 0,
        }
    }
    monkeypatch.setattr(
        orchestrator,
        "get_thread_orchestration_activity",
        lambda parent_thread_ids=None: dict(activity),
    )
    brain = BuddyBrain()

    now = 1003.0
    busy = brain.resolve(None)

    assert busy.details["event_type"] == BuddyEventType.ORCHESTRATION_ACTIVE.value
    assert busy.message == "Child Agents working"
    assert busy.animation == "think_loop"
    assert busy.details["active_members"] == 2

    activity.clear()
    now = 1009.0
    idle = brain.resolve(None)

    assert idle.message == "Idle"
    assert idle.animation == "idle_breathe"


def test_buddy_labels_detached_orchestration_as_background(monkeypatch):
    import row_bot.agent_orchestrator as orchestrator
    import row_bot.buddy.brain as brain_mod
    import row_bot.tasks as tasks
    from row_bot.buddy.brain import BuddyBrain

    now = 2000.0
    monkeypatch.setattr(
        brain_mod,
        "get_buddy_config",
        lambda: {"enabled": True, "mode": "sidebar", "pack_id": "glyph"},
    )
    monkeypatch.setattr(brain_mod.time, "time", lambda: now)
    monkeypatch.setattr(tasks, "get_pending_approvals", lambda: [])
    monkeypatch.setattr(tasks, "get_running_tasks", lambda: {})
    monkeypatch.setattr(
        orchestrator,
        "get_thread_orchestration_activity",
        lambda parent_thread_ids=None: {
            "parent-thread": {
                "orchestration_id": "orchestration-background",
                "state": "active",
                "blocking": False,
                "background": True,
                "phase": "background",
                "active_members": 1,
                "failed_members": 0,
            }
        },
    )
    brain = BuddyBrain()

    now = 2003.0
    background = brain.resolve(None)

    assert background.message == "Background Agent working"
    assert background.details["background"] is True


def test_buddy_terminal_event_clears_only_exact_orchestration_owner(monkeypatch):
    import row_bot.buddy.brain as brain_mod
    from row_bot.buddy.brain import BuddyBrain
    from row_bot.buddy.events import BuddyEvent, BuddyEventType

    monkeypatch.setattr(
        brain_mod,
        "get_buddy_config",
        lambda: {"enabled": True, "mode": "sidebar", "pack_id": "glyph"},
    )
    brain = BuddyBrain()
    brain.resolve(
        BuddyEvent(
            BuddyEventType.ORCHESTRATION_ACTIVE,
            source="test",
            payload={"orchestration_id": "orch-1", "thread_id": "thread-1"},
            id=1,
        )
    )
    brain.resolve(
        BuddyEvent(
            BuddyEventType.ORCHESTRATION_ACTIVE,
            source="test",
            payload={"orchestration_id": "orch-2", "thread_id": "thread-2"},
            id=2,
        )
    )

    remaining = brain.resolve(
        BuddyEvent(
            BuddyEventType.ORCHESTRATION_DONE,
            source="test",
            payload={"orchestration_id": "orch-1", "thread_id": "thread-1"},
            id=3,
        )
    )

    assert set(brain._active["orchestration"]) == {"orch-2"}
    assert remaining.details["event_type"] == BuddyEventType.ORCHESTRATION_ACTIVE.value


def test_buddy_durable_reconcile_removes_event_owner_without_stale_wait(monkeypatch):
    import row_bot.agent_orchestrator as orchestrator
    import row_bot.buddy.brain as brain_mod
    import row_bot.tasks as tasks
    from row_bot.buddy.brain import BuddyBrain
    from row_bot.buddy.events import BuddyEvent, BuddyEventType

    now = 3000.0
    monkeypatch.setattr(
        brain_mod,
        "get_buddy_config",
        lambda: {"enabled": True, "mode": "sidebar", "pack_id": "glyph"},
    )
    monkeypatch.setattr(brain_mod.time, "time", lambda: now)
    monkeypatch.setattr(tasks, "get_pending_approvals", lambda: [])
    monkeypatch.setattr(tasks, "get_running_tasks", lambda: {})
    monkeypatch.setattr(
        orchestrator,
        "get_thread_orchestration_activity",
        lambda parent_thread_ids=None: {},
    )
    brain = BuddyBrain()
    brain.resolve(
        BuddyEvent(
            BuddyEventType.ORCHESTRATION_ACTIVE,
            source="test",
            payload={"orchestration_id": "event-only", "thread_id": "thread-1"},
            id=1,
        )
    )

    now = 3006.0
    idle = brain.resolve(None)

    assert "orchestration" not in brain._active
    assert idle.message == "Idle"
