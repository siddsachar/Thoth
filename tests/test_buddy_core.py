from __future__ import annotations

import importlib
import pathlib
import threading

import pytest


@pytest.fixture(autouse=True)
def _isolate_buddy_durable_activity(monkeypatch):
    """Keep Buddy unit tests independent of stale deterministic-lane state."""

    import row_bot.agent_orchestrator as orchestrator
    import row_bot.tasks as tasks

    monkeypatch.setattr(tasks, "get_pending_approvals", lambda: [])
    monkeypatch.setattr(tasks, "get_running_tasks", lambda: {})
    monkeypatch.setattr(orchestrator, "get_thread_orchestration_activity", lambda: {})


def test_buddy_event_bus_assigns_ids_and_retains_recent_events():
    from row_bot.buddy.events import BuddyEventBus, BuddyEventType

    bus = BuddyEventBus(max_events=2)
    first = bus.emit(BuddyEventType.GENERATION_STARTED, source="test", payload={"label": "Thinking"})
    second = bus.emit(BuddyEventType.TOKEN, source="test")
    third = bus.emit(BuddyEventType.GENERATION_DONE, source="test")

    assert (first.id, second.id, third.id) == (1, 2, 3)
    assert [event.id for event in bus.recent()] == [2, 3]
    assert bus.latest() == third


def test_buddy_brain_maps_events_to_runtime_friendly_state(monkeypatch):
    import row_bot.buddy.brain as brain_mod
    from row_bot.buddy.brain import BuddyBrain
    from row_bot.buddy.events import BuddyEvent, BuddyEventType

    monkeypatch.setattr(
        brain_mod,
        "get_buddy_config",
        lambda: {"enabled": True, "mode": "sidebar", "pack_id": "glyph"},
    )
    brain = BuddyBrain()
    state = brain.resolve(BuddyEvent(BuddyEventType.APPROVAL_NEEDED, source="test", payload={"label": "Needs approval"}, id=7))

    assert state.event_id == 7
    assert state.mood == "concerned"
    assert state.animation == "tap_glass"
    assert state.alert > 80
    assert state.message == "Needs approval"


def test_buddy_brain_decays_transient_actions_back_to_idle(monkeypatch):
    import row_bot.buddy.brain as brain_mod
    from row_bot.buddy.brain import BuddyBrain
    from row_bot.buddy.events import BuddyEvent, BuddyEventType

    now = 1000.0
    monkeypatch.setattr(
        brain_mod,
        "get_buddy_config",
        lambda: {"enabled": True, "mode": "sidebar", "pack_id": "glyph"},
    )
    monkeypatch.setattr(brain_mod.time, "time", lambda: now)
    brain = BuddyBrain()

    done = brain.resolve(BuddyEvent(BuddyEventType.GENERATION_DONE, source="test", id=8))
    assert done.animation == "celebrate_small"

    now = 1005.0
    idle = brain.resolve(None)

    assert idle.event_id == 8
    assert idle.animation == "idle_breathe"
    assert idle.message == "Idle"


def test_buddy_brain_returns_to_active_generation_after_tool_finishes(monkeypatch):
    import row_bot.buddy.brain as brain_mod
    from row_bot.buddy.brain import BuddyBrain
    from row_bot.buddy.events import BuddyEvent, BuddyEventType

    now = 1000.0
    monkeypatch.setattr(
        brain_mod,
        "get_buddy_config",
        lambda: {"enabled": True, "mode": "sidebar", "pack_id": "glyph"},
    )
    monkeypatch.setattr(brain_mod.time, "time", lambda: now)
    brain = BuddyBrain()

    brain.resolve(BuddyEvent(BuddyEventType.GENERATION_STARTED, source="test", id=9))
    brain.resolve(BuddyEvent(BuddyEventType.TOOL_STARTED, source="test", id=10))
    tool_done = brain.resolve(BuddyEvent(BuddyEventType.TOOL_FINISHED, source="test", id=11))
    assert tool_done.animation == "nod"

    now = 1003.0
    active = brain.resolve(None)

    assert active.animation == "lean_in"
    assert active.message == "Thinking"

    brain.resolve(BuddyEvent(BuddyEventType.GENERATION_DONE, source="test", id=12))
    now = 1006.0
    idle = brain.resolve(None)

    assert idle.animation == "idle_breathe"


def test_buddy_brain_clears_approval_on_resolution(monkeypatch):
    import row_bot.buddy.brain as brain_mod
    from row_bot.buddy.brain import BuddyBrain
    from row_bot.buddy.events import BuddyEvent, BuddyEventType

    now = 1000.0
    monkeypatch.setattr(
        brain_mod,
        "get_buddy_config",
        lambda: {"enabled": True, "mode": "sidebar", "pack_id": "glyph"},
    )
    monkeypatch.setattr(brain_mod.time, "time", lambda: now)
    brain = BuddyBrain()

    brain.resolve(BuddyEvent(BuddyEventType.GENERATION_STARTED, source="test", id=12))
    brain.resolve(BuddyEvent(BuddyEventType.APPROVAL_NEEDED, source="test", id=13))
    now = 1010.0
    approval = brain.resolve(None)

    assert approval.animation == "tap_glass"

    denied = brain.resolve(BuddyEvent(BuddyEventType.APPROVAL_DENIED, source="test", id=14))
    assert denied.animation == "pause"

    now = 1013.0
    active = brain.resolve(None)

    assert active.animation == "lean_in"
    assert active.message == "Thinking"

    brain.resolve(BuddyEvent(BuddyEventType.GENERATION_DONE, source="test", id=15))
    now = 1016.0
    idle = brain.resolve(None)

    assert idle.animation == "idle_breathe"


def test_buddy_brain_clears_legacy_generic_approval_after_resolution(monkeypatch):
    import row_bot.buddy.brain as brain_mod
    from row_bot.buddy.brain import BuddyBrain
    from row_bot.buddy.events import BuddyEvent, BuddyEventType

    now = 1000.0
    monkeypatch.setattr(
        brain_mod,
        "get_buddy_config",
        lambda: {"enabled": True, "mode": "sidebar", "pack_id": "glyph"},
    )
    monkeypatch.setattr(brain_mod.time, "time", lambda: now)
    brain = BuddyBrain()

    brain.resolve(BuddyEvent(BuddyEventType.WORKFLOW_STARTED, source="test", payload={"thread_id": "thread-1", "label": "Approval Flow"}, id=30))
    brain.resolve(BuddyEvent(BuddyEventType.APPROVAL_NEEDED, source="test", payload={"approval_id": "approval-1", "run_id": "run-1", "task_id": "task-1", "label": "Real approval"}, id=31))
    brain.resolve(BuddyEvent(BuddyEventType.APPROVAL_NEEDED, source="test", payload={"task_id": "task-1", "thread_id": "thread-1", "label": "Needs approval"}, id=32))
    approved = brain.resolve(BuddyEvent(BuddyEventType.APPROVAL_APPROVED, source="test", payload={"approval_id": "approval-1", "run_id": "run-1", "task_id": "task-1", "label": "Approved"}, id=33))

    assert approved.animation == "nod"

    done = brain.resolve(BuddyEvent(BuddyEventType.WORKFLOW_DONE, source="test", payload={"thread_id": "thread-1", "label": "Approval Flow done"}, id=34))
    assert done.animation == "celebrate_big"

    now = 1004.0
    idle = brain.resolve(None)

    assert idle.animation == "idle_breathe"
    assert idle.message == "Idle"


def test_buddy_brain_clears_resume_token_only_approval_after_resolution(monkeypatch):
    import row_bot.buddy.brain as brain_mod
    from row_bot.buddy.brain import BuddyBrain
    from row_bot.buddy.events import BuddyEvent, BuddyEventType

    now = 1000.0
    monkeypatch.setattr(
        brain_mod,
        "get_buddy_config",
        lambda: {"enabled": True, "mode": "sidebar", "pack_id": "glyph"},
    )
    monkeypatch.setattr(brain_mod.time, "time", lambda: now)
    brain = BuddyBrain()

    brain.resolve(BuddyEvent(
        BuddyEventType.APPROVAL_NEEDED,
        source="tasks",
        payload={"resume_token": "resume-1", "label": "Approve step"},
        id=40,
    ))
    now = 1001.0
    approval = brain.resolve(None)

    assert approval.animation == "tap_glass"

    resolved = brain.resolve(BuddyEvent(
        BuddyEventType.APPROVAL_APPROVED,
        source="tasks",
        payload={"resume_token": "resume-1", "label": "Approved"},
        id=41,
    ))

    assert resolved.animation == "nod"

    now = 1009.0
    idle = brain.resolve(None)

    assert idle.animation == "idle_breathe"
    assert idle.message == "Idle"


def test_buddy_reconcile_clears_stale_durable_approval(monkeypatch):
    import row_bot.buddy.brain as brain_mod
    from row_bot.buddy.brain import BuddyBrain
    from row_bot.buddy.events import BuddyEvent, BuddyEventType
    import row_bot.tasks as tasks

    now = 1000.0
    monkeypatch.setattr(
        brain_mod,
        "get_buddy_config",
        lambda: {"enabled": True, "mode": "sidebar", "pack_id": "glyph"},
    )
    monkeypatch.setattr(brain_mod.time, "time", lambda: now)
    monkeypatch.setattr(tasks, "get_pending_approvals", lambda: [])
    monkeypatch.setattr(tasks, "get_running_tasks", lambda: {})
    brain = BuddyBrain()

    brain.resolve(BuddyEvent(
        BuddyEventType.APPROVAL_NEEDED,
        source="tasks",
        payload={
            "approval_id": "approval-1",
            "resume_token": "resume-1",
            "run_id": "run-1",
            "task_id": "task-1",
            "label": "Approve step",
        },
        id=42,
    ))
    now = 1007.0
    idle = brain.resolve(None)

    assert idle.animation == "idle_breathe"
    assert idle.message == "Idle"


def test_buddy_reconcile_keeps_transient_chat_approval(monkeypatch):
    import row_bot.buddy.brain as brain_mod
    from row_bot.buddy.brain import BuddyBrain
    from row_bot.buddy.events import BuddyEvent, BuddyEventType
    import row_bot.tasks as tasks

    now = 1000.0
    monkeypatch.setattr(
        brain_mod,
        "get_buddy_config",
        lambda: {"enabled": True, "mode": "sidebar", "pack_id": "glyph"},
    )
    monkeypatch.setattr(brain_mod.time, "time", lambda: now)
    monkeypatch.setattr(tasks, "get_pending_approvals", lambda: [])
    monkeypatch.setattr(tasks, "get_running_tasks", lambda: {})
    brain = BuddyBrain()

    brain.resolve(BuddyEvent(
        BuddyEventType.APPROVAL_NEEDED,
        source="ui.streaming",
        payload={"thread_id": "thread-1", "label": "Approval pending"},
        id=43,
    ))
    now = 1007.0
    approval = brain.resolve(None)

    assert approval.animation == "tap_glass"
    assert approval.message == "Approval pending"


def test_buddy_tick_processes_approval_resolution_before_later_workflow_events(monkeypatch):
    import row_bot.buddy.brain as brain_mod
    from row_bot.buddy.brain import BuddyBrain
    from row_bot.buddy.events import BuddyEventBus, BuddyEventType

    now = 1000.0
    bus = BuddyEventBus()
    monkeypatch.setattr(
        brain_mod,
        "get_buddy_config",
        lambda: {"enabled": True, "mode": "sidebar", "pack_id": "glyph"},
    )
    monkeypatch.setattr(brain_mod, "get_buddy_event_bus", lambda: bus)
    monkeypatch.setattr(brain_mod.time, "time", lambda: now)
    brain = BuddyBrain()

    bus.emit(BuddyEventType.WORKFLOW_STARTED, source="test", payload={"thread_id": "thread-1", "label": "Approval Flow"})
    bus.emit(BuddyEventType.APPROVAL_NEEDED, source="test", payload={"approval_id": "approval-1", "run_id": "run-1", "task_id": "task-1", "label": "Real approval"})
    bus.emit(BuddyEventType.APPROVAL_APPROVED, source="test", payload={"approval_id": "approval-1", "run_id": "run-1", "task_id": "task-1", "label": "Approved"})
    bus.emit(BuddyEventType.WORKFLOW_STARTED, source="test", payload={"thread_id": "thread-1", "label": "Approval Flow"})
    state = bus.emit(BuddyEventType.WORKFLOW_DONE, source="test", payload={"thread_id": "thread-1", "label": "Approval Flow done"})

    done = brain.tick()

    assert done.event_id == state.id
    assert done.animation == "celebrate_big"
    assert done.message == "Approval Flow done"

    now = 1004.0
    idle = brain.tick()

    assert idle.animation == "idle_breathe"
    assert idle.message == "Idle"


def test_buddy_brain_clears_workflow_after_denied_workflow_cancelled(monkeypatch):
    import row_bot.buddy.brain as brain_mod
    from row_bot.buddy.brain import BuddyBrain
    from row_bot.buddy.events import BuddyEvent, BuddyEventType

    now = 1000.0
    monkeypatch.setattr(
        brain_mod,
        "get_buddy_config",
        lambda: {"enabled": True, "mode": "sidebar", "pack_id": "glyph"},
    )
    monkeypatch.setattr(brain_mod.time, "time", lambda: now)
    brain = BuddyBrain()

    brain.resolve(BuddyEvent(BuddyEventType.WORKFLOW_STARTED, source="test", id=20))
    brain.resolve(BuddyEvent(BuddyEventType.WORKFLOW_STEP, source="test", payload={"label": "Step 1"}, id=21))
    brain.resolve(BuddyEvent(BuddyEventType.APPROVAL_NEEDED, source="test", id=22))
    brain.resolve(BuddyEvent(BuddyEventType.APPROVAL_DENIED, source="test", id=23))
    cancelled = brain.resolve(BuddyEvent(BuddyEventType.WORKFLOW_CANCELLED, source="test", payload={"label": "Workflow denied"}, id=24))

    assert cancelled.animation == "pause"
    assert cancelled.message == "Workflow denied"

    now = 1003.0
    idle = brain.resolve(None)

    assert idle.animation == "idle_breathe"
    assert idle.message == "Idle"


def test_buddy_brain_denied_approval_clears_active_workflow(monkeypatch):
    import row_bot.buddy.brain as brain_mod
    from row_bot.buddy.brain import BuddyBrain
    from row_bot.buddy.events import BuddyEvent, BuddyEventType

    now = 1000.0
    monkeypatch.setattr(
        brain_mod,
        "get_buddy_config",
        lambda: {"enabled": True, "mode": "sidebar", "pack_id": "glyph"},
    )
    monkeypatch.setattr(brain_mod.time, "time", lambda: now)
    brain = BuddyBrain()

    brain.resolve(BuddyEvent(BuddyEventType.WORKFLOW_STARTED, source="test", id=20))
    brain.resolve(BuddyEvent(BuddyEventType.WORKFLOW_STEP, source="test", payload={"label": "Middle step"}, id=21))
    denied = brain.resolve(BuddyEvent(BuddyEventType.APPROVAL_DENIED, source="test", payload={"label": "Denied"}, id=22))

    assert denied.animation == "pause"
    assert denied.message == "Denied"

    now = 1003.0
    idle = brain.resolve(None)

    assert idle.animation == "idle_breathe"
    assert idle.message == "Idle"


def test_resume_pipeline_denial_emits_buddy_workflow_cancelled(monkeypatch):
    import row_bot.buddy.events as buddy_events
    import row_bot.notifications as notifications
    import row_bot.tasks as tasks
    import row_bot.threads as threads
    from row_bot.buddy.events import BuddyEventType
    from row_bot.tools import registry

    emitted = []
    statuses = []
    finishes = []

    monkeypatch.setattr(registry, "get_enabled_tools", lambda: [])
    monkeypatch.setattr(
        tasks,
        "_load_pipeline_state",
        lambda token: {
            "run_id": "run-1",
            "task_id": "task-1",
            "thread_id": "thread-1",
            "current_step_index": 0,
            "step_outputs": {},
            "config": {},
        },
    )
    monkeypatch.setattr(
        tasks,
        "get_task",
        lambda task_id: {
            "id": task_id,
            "name": "Denied Workflow",
            "steps": [{"id": "approval_1", "type": "approval", "if_denied": "end"}],
        },
    )
    monkeypatch.setattr(tasks, "_update_pipeline_status", lambda run_id, status: statuses.append((run_id, status)))
    monkeypatch.setattr(
        tasks,
        "_finish_run",
        lambda run_id, status="completed", status_message="": finishes.append((run_id, status, status_message)),
    )
    monkeypatch.setattr(threads, "_list_threads", lambda: [])
    monkeypatch.setattr(threads, "_save_thread_meta", lambda *args, **kwargs: None)
    monkeypatch.setattr(notifications, "notify", lambda **kwargs: None)
    monkeypatch.setattr(
        buddy_events,
        "emit_buddy_event",
        lambda event_type, *, source, payload: emitted.append((event_type, source, payload)),
    )

    tasks._resume_pipeline("resume-token", approved=False)

    assert statuses == [("run-1", "stopped")]
    assert finishes == [("run-1", "stopped", "Approval denied by user")]
    assert emitted == [
        (
            BuddyEventType.WORKFLOW_CANCELLED,
            "tasks",
            {"task_id": "task-1", "thread_id": "thread-1", "label": "Workflow denied"},
        )
    ]


def test_buddy_config_round_trips_to_temp_data_dir(monkeypatch, tmp_path):
    import row_bot.buddy.config as config_mod

    monkeypatch.setattr(config_mod, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(config_mod, "_BUDDY_CONFIG_PATH", tmp_path / "buddy_config.json")

    saved = config_mod.set_buddy_config("placement", "desktop")
    loaded = config_mod.get_buddy_config()

    assert saved["placement"] == "desktop"
    assert loaded["placement"] == "desktop"
    assert loaded["visible"] is True
    assert loaded["pack_id"] == "glyph"


def test_buddy_config_loads_utf8_bom_file(monkeypatch, tmp_path):
    import row_bot.buddy.config as config_mod

    config_path = tmp_path / "buddy_config.json"
    monkeypatch.setattr(config_mod, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(config_mod, "_BUDDY_CONFIG_PATH", config_path)
    config_path.write_text('{"floating_enabled": true, "display_name": "BOM Buddy"}', encoding="utf-8-sig")

    loaded = config_mod.get_buddy_config()

    assert loaded["placement"] == "docked"
    assert "floating_enabled" not in loaded
    assert loaded["display_name"] == "BOM Buddy"


def test_buddy_hatch_preview_generation_saves_preview(monkeypatch, tmp_path):
    import row_bot.buddy.assets as assets_mod
    import row_bot.buddy.config as config_mod
    import row_bot.buddy.hatch as hatch_mod

    monkeypatch.setattr(config_mod, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(config_mod, "_BUDDY_CONFIG_PATH", tmp_path / "buddy_config.json")
    monkeypatch.setattr(hatch_mod, "_DATA_DIR", tmp_path / "hatches")
    monkeypatch.setattr(assets_mod, "_BUDDY_STATIC_DIR", tmp_path / "buddy_static")
    monkeypatch.setattr(assets_mod, "_USER_PACKS_DIR", tmp_path / "buddy_static" / "packs")
    config_mod.save_buddy_config({
        "active_hatch_motion": "old.mp4",
        "active_hatch_motion_pack": "old-manifest.json",
        "latest_hatch_motion": "old-source.mp4",
        "latest_hatch_motion_pack": "old-source-manifest.json",
    })

    def _fake_generate(prompt: str, size: str = "auto", quality: str = "auto") -> str:
        return "Image generated successfully"

    monkeypatch.setattr("row_bot.tools.image_gen_tool._generate_image", _fake_generate)
    monkeypatch.setattr("row_bot.tools.image_gen_tool.get_and_clear_last_image", lambda: "iVBORw0KGgo=")

    draft = hatch_mod.generate_hatch_preview("tiny gold owl", pack_id="glyph")

    assert draft.status == "preview_generated"
    assert draft.preview_path.endswith("preview.png")
    assert "tiny gold owl" in draft.prompt
    assert pathlib.Path(draft.preview_path).exists()
    assert (tmp_path / "buddy_static" / "generated" / "current.png").exists()
    assert (tmp_path / "buddy_static" / "packs" / draft.pack_id / "manifest.json").exists()
    pack = assets_mod.load_buddy_pack(draft.pack_id)
    assert pack.runtime == "generated_still"
    assert pack.status == "available"
    assert assets_mod.static_url_for_path(pack.preview_path) == f"/_buddy/packs/{draft.pack_id}/preview.png"
    cfg = config_mod.get_buddy_config()
    assert cfg["pack_id"] == draft.pack_id
    assert "active_hatch_motion_pack" not in cfg
    assert "latest_hatch_motion_pack" not in cfg


def test_buddy_hatch_buddy_generation_activates_motion(monkeypatch, tmp_path):
    import row_bot.buddy.assets as assets_mod
    import row_bot.buddy.config as config_mod
    import row_bot.buddy.hatch as hatch_mod
    import row_bot.tools.video_gen_tool as video_mod

    monkeypatch.setattr(config_mod, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(config_mod, "_BUDDY_CONFIG_PATH", tmp_path / "buddy_config.json")
    monkeypatch.setattr(hatch_mod, "_DATA_DIR", tmp_path / "hatches")
    monkeypatch.setattr(assets_mod, "_BUDDY_STATIC_DIR", tmp_path / "buddy_static")
    monkeypatch.setattr(assets_mod, "_USER_PACKS_DIR", tmp_path / "buddy_static" / "packs")

    monkeypatch.setattr("row_bot.tools.image_gen_tool._generate_image", lambda *args, **kwargs: "Image generated")
    monkeypatch.setattr("row_bot.tools.image_gen_tool.get_and_clear_last_image", lambda: "iVBORw0KGgo=")

    generated_filenames: list[str] = []

    def _fake_animate(prompt: str, image_source: str = "last", duration_seconds: int = 8, aspect_ratio: str = "16:9", resolution: str = "720p") -> str:
        output_dir = video_mod._video_output_dir_var.get()
        output_filename = video_mod._video_output_filename_var.get()
        assert output_dir is not None
        assert output_filename is not None
        output_dir.mkdir(parents=True, exist_ok=True)
        motion_path = output_dir / output_filename
        motion_path.write_bytes(b"MP4")
        generated_filenames.append(output_filename)
        video_mod._last_generated_video = {
            "path": str(motion_path),
            "filename": motion_path.name,
            "provider": "test",
            "model": "fake",
            "duration": duration_seconds,
            "mode": "image-to-video",
        }
        assert pathlib.Path(image_source).exists()
        assert aspect_ratio == "1:1"
        assert duration_seconds == 5
        return "Video generated"

    monkeypatch.setattr("row_bot.tools.video_gen_tool._animate_image", _fake_animate)

    draft = hatch_mod.generate_hatch_buddy("tiny gold owl", pack_id="glyph")

    assert draft.status == "motion_pack_generated"
    assert pathlib.Path(draft.motion_path).exists()
    assert pathlib.Path(draft.active_motion_path).exists()
    assert pathlib.Path(draft.motion_pack_path).exists()
    assert pathlib.Path(draft.active_motion_pack_path).exists()
    assert set(generated_filenames) == {"idle.mp4", "thinking.mp4", "working.mp4", "approval.mp4", "success.mp4", "error.mp4"}
    assert (tmp_path / "buddy_static" / "generated" / "current.mp4").exists()
    assert (tmp_path / "buddy_static" / "generated" / "motions" / "idle.mp4").exists()
    assert (tmp_path / "buddy_static" / "generated" / "motions" / "success.mp4").exists()
    cfg = config_mod.get_buddy_config()
    assert cfg["active_hatch_motion"].endswith("current.mp4")
    assert cfg["active_hatch_motion_pack"].endswith("manifest.json")
    assert cfg["active_hatch_motion_clips"]["thinking"].endswith("thinking.mp4")
    assert cfg["pack_id"] == draft.pack_id
    pack = assets_mod.load_buddy_pack(draft.pack_id)
    assert pack.runtime == "generated_motion_pack"
    assert pack.status == "available"
    assert (tmp_path / "buddy_static" / "packs" / draft.pack_id / "preview.png").exists()
    assert (tmp_path / "buddy_static" / "packs" / draft.pack_id / "motions" / "idle.mp4").exists()


def test_buddy_hatch_motion_pack_can_force_fresh_generation(monkeypatch, tmp_path):
    import row_bot.buddy.assets as assets_mod
    import row_bot.buddy.config as config_mod
    import row_bot.buddy.hatch as hatch_mod
    import row_bot.tools.video_gen_tool as video_mod

    monkeypatch.setattr(config_mod, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(config_mod, "_BUDDY_CONFIG_PATH", tmp_path / "buddy_config.json")
    monkeypatch.setattr(assets_mod, "_BUDDY_STATIC_DIR", tmp_path / "buddy_static")

    preview = tmp_path / "hatches" / "hatch-fresh" / "preview.png"
    preview.parent.mkdir(parents=True)
    preview.write_bytes(b"PNG")
    motion_dir = preview.parent / "motions"
    motion_dir.mkdir()
    old_idle = motion_dir / "idle.mp4"
    old_idle.write_bytes(b"OLD")
    generated_filenames: list[str] = []

    def _fake_animate(prompt: str, image_source: str = "last", duration_seconds: int = 8, aspect_ratio: str = "16:9", resolution: str = "720p") -> str:
        output_dir = video_mod._video_output_dir_var.get()
        output_filename = video_mod._video_output_filename_var.get()
        assert output_dir is not None
        assert output_filename is not None
        output_dir.mkdir(parents=True, exist_ok=True)
        motion_path = output_dir / output_filename
        motion_path.write_bytes(f"NEW:{output_filename}".encode("utf-8"))
        generated_filenames.append(output_filename)
        video_mod._last_generated_video = {"path": str(motion_path), "filename": motion_path.name}
        return "Video generated"

    monkeypatch.setattr("row_bot.tools.video_gen_tool._animate_image", _fake_animate)

    draft = hatch_mod.generate_hatch_motion_pack("tiny gold owl", preview, pack_id="glyph", reuse_existing=False)

    assert draft.status == "motion_pack_generated"
    assert old_idle.read_bytes() == b"NEW:idle.mp4"
    assert set(generated_filenames) == {"idle.mp4", "thinking.mp4", "working.mp4", "approval.mp4", "success.mp4", "error.mp4"}


def test_buddy_hatch_retry_motion_preserves_user_pack_manifest(monkeypatch, tmp_path):
    import json
    import row_bot.buddy.assets as assets_mod
    import row_bot.buddy.config as config_mod
    import row_bot.buddy.hatch as hatch_mod
    import row_bot.tools.video_gen_tool as video_mod

    monkeypatch.setattr(config_mod, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(config_mod, "_BUDDY_CONFIG_PATH", tmp_path / "buddy_config.json")
    monkeypatch.setattr(hatch_mod, "_DATA_DIR", tmp_path / "hatches")
    monkeypatch.setattr(assets_mod, "_BUDDY_STATIC_DIR", tmp_path / "buddy_static")
    monkeypatch.setattr(assets_mod, "_USER_PACKS_DIR", tmp_path / "buddy_static" / "packs")

    pack_dir = tmp_path / "buddy_static" / "packs" / "hatch-existing"
    pack_dir.mkdir(parents=True)
    preview = pack_dir / "preview.png"
    preview.write_bytes(b"PNG")

    def _fake_animate(prompt: str, image_source: str = "last", duration_seconds: int = 8, aspect_ratio: str = "16:9", resolution: str = "720p") -> str:
        output_dir = video_mod._video_output_dir_var.get()
        output_filename = video_mod._video_output_filename_var.get()
        assert output_dir is not None
        assert output_filename is not None
        output_dir.mkdir(parents=True, exist_ok=True)
        motion_path = output_dir / output_filename
        motion_path.write_bytes(f"MP4:{output_filename}".encode("utf-8"))
        video_mod._last_generated_video = {"path": str(motion_path), "filename": motion_path.name}
        return "Video generated"

    monkeypatch.setattr("row_bot.tools.video_gen_tool._animate_image", _fake_animate)

    draft = hatch_mod.generate_hatch_motion_pack("A tiny dog named Honey", preview, pack_id="hatch-existing", reuse_existing=False)

    pack_manifest = json.loads((pack_dir / "manifest.json").read_text(encoding="utf-8"))
    assert draft.status == "motion_pack_generated"
    assert pack_manifest["runtime"] == "generated_motion_pack"
    assert set(pack_manifest["clips"]) == {"idle", "thinking", "working", "approval", "success", "error"}
    assert (tmp_path / "hatches" / "hatch-existing" / "manifest.json").exists()
    pack = assets_mod.load_buddy_pack("hatch-existing")
    assert pack.runtime == "generated_motion_pack"
    assert pack.status == "available"
    assert len(pack.motion_clips) == 6


def test_buddy_hatch_google_pacing_only_applies_to_real_google_generator(monkeypatch):
    import row_bot.buddy.hatch as hatch_mod

    monkeypatch.setattr(hatch_mod, "_configured_video_model", lambda: "google/veo-3.1-generate-preview")
    monkeypatch.setenv("ROW_BOT_BUDDY_GOOGLE_VIDEO_SPACING_SECONDS", "15")

    assert hatch_mod._motion_request_spacing_seconds() == 15
    assert hatch_mod._motion_request_spacing_seconds(lambda: None) == 0


def test_buddy_loader_recovers_overwritten_hatch_motion_pack_manifest(monkeypatch, tmp_path):
    import json
    import row_bot.buddy.assets as assets_mod

    monkeypatch.setattr(assets_mod, "_BUDDY_STATIC_DIR", tmp_path / "buddy_static")
    monkeypatch.setattr(assets_mod, "_USER_PACKS_DIR", tmp_path / "buddy_static" / "packs")

    pack_dir = tmp_path / "buddy_static" / "packs" / "hatch-legacy"
    motion_dir = pack_dir / "motions"
    motion_dir.mkdir(parents=True)
    (pack_dir / "preview.png").write_bytes(b"PNG")
    clips = {}
    for clip_id in assets_mod.REQUIRED_MOTION_CLIPS:
        (motion_dir / f"{clip_id}.mp4").write_bytes(b"MP4")
        clips[clip_id] = {"id": clip_id, "label": clip_id.title(), "path": f"{clip_id}.mp4", "animations": [clip_id], "duration_seconds": 5}
    (motion_dir / "manifest.json").write_text(json.dumps({"default_clip": "idle", "clips": clips}), encoding="utf-8")
    (pack_dir / "manifest.json").write_text(
        json.dumps(
            {
                "id": "hatch-legacy",
                "status": "motion_pack_generated",
                "preview_path": str(pack_dir / "preview.png"),
                "motion_pack_path": str(motion_dir / "manifest.json"),
            }
        ),
        encoding="utf-8",
    )

    pack = assets_mod.load_buddy_pack("hatch-legacy")

    assert pack.runtime == "generated_motion_pack"
    assert pack.status == "available"
    assert pack.preview_path == (pack_dir / "preview.png").resolve()
    assert set(pack.motion_clips) == assets_mod.REQUIRED_MOTION_CLIPS


def test_buddy_loader_maps_stale_legacy_absolute_hatch_paths(monkeypatch, tmp_path):
    import json
    import row_bot.buddy.assets as assets_mod

    row_bot_static = tmp_path / ".row-bot" / "buddy"
    monkeypatch.setattr(assets_mod, "_BUDDY_STATIC_DIR", row_bot_static)
    monkeypatch.setattr(assets_mod, "_USER_PACKS_DIR", row_bot_static / "packs")

    pack_dir = row_bot_static / "packs" / "hatch-legacy"
    motion_dir = pack_dir / "motions"
    motion_dir.mkdir(parents=True)
    (pack_dir / "preview.png").write_bytes(b"PNG")
    clips = {}
    for clip_id in assets_mod.REQUIRED_MOTION_CLIPS:
        (motion_dir / f"{clip_id}.mp4").write_bytes(b"MP4")
        clips[clip_id] = {"id": clip_id, "label": clip_id.title(), "path": f"{clip_id}.mp4", "animations": [clip_id], "duration_seconds": 5}
    (motion_dir / "manifest.json").write_text(json.dumps({"default_clip": "idle", "clips": clips}), encoding="utf-8")

    legacy_pack_dir = tmp_path / "old-data" / "buddy" / "packs" / "hatch-legacy"
    (pack_dir / "manifest.json").write_text(
        json.dumps(
            {
                "id": "hatch-legacy",
                "status": "motion_pack_generated",
                "preview_path": str(legacy_pack_dir / "preview.png"),
                "motion_pack_path": str(legacy_pack_dir / "motions" / "manifest.json"),
            }
        ),
        encoding="utf-8",
    )

    pack = assets_mod.load_buddy_pack("hatch-legacy")

    assert not (tmp_path / "old-data").exists()
    assert pack.status == "available"
    assert pack.preview_path == (pack_dir / "preview.png").resolve()
    assert pack.motion_pack_path == (motion_dir / "manifest.json").resolve()
    assert set(pack.motion_clips) == assets_mod.REQUIRED_MOTION_CLIPS


def test_buddy_hatch_motion_pack_activation_copies_manifest_and_clips(monkeypatch, tmp_path):
    import json
    import row_bot.buddy.assets as assets_mod
    import row_bot.buddy.config as config_mod
    import row_bot.buddy.hatch as hatch_mod

    monkeypatch.setattr(config_mod, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(config_mod, "_BUDDY_CONFIG_PATH", tmp_path / "buddy_config.json")
    monkeypatch.setattr(assets_mod, "_BUDDY_STATIC_DIR", tmp_path / "buddy_static")

    pack_dir = tmp_path / "draft" / "motions"
    pack_dir.mkdir(parents=True)
    for name in ["idle.mp4", "thinking.mp4"]:
        (pack_dir / name).write_bytes(b"MP4")
    manifest = {
        "schema": 1,
        "default_clip": "idle",
        "animation_map": {"idle_breathe": "idle", "think_loop": "thinking"},
        "clips": {
            "idle": {"label": "Idle", "path": "idle.mp4"},
            "thinking": {"label": "Thinking", "path": "thinking.mp4"},
        },
    }
    manifest_path = pack_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    active_manifest = hatch_mod.activate_hatch_motion_pack(manifest_path)

    assert active_manifest == tmp_path / "buddy_static" / "generated" / "motions" / "manifest.json"
    assert (tmp_path / "buddy_static" / "generated" / "current.mp4").exists()
    assert (tmp_path / "buddy_static" / "generated" / "motions" / "thinking.mp4").exists()
    cfg = config_mod.get_buddy_config()
    assert cfg["active_hatch_motion_clips"]["idle"].endswith("idle.mp4")


def test_buddy_hatch_activation_copies_existing_preview(monkeypatch, tmp_path):
    import row_bot.buddy.assets as assets_mod
    import row_bot.buddy.config as config_mod
    import row_bot.buddy.hatch as hatch_mod

    monkeypatch.setattr(config_mod, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(config_mod, "_BUDDY_CONFIG_PATH", tmp_path / "buddy_config.json")
    monkeypatch.setattr(assets_mod, "_BUDDY_STATIC_DIR", tmp_path / "buddy_static")

    preview = tmp_path / "old-preview.png"
    preview.write_bytes(b"PNG")

    active_path = hatch_mod.activate_hatch_art(preview)

    assert active_path == tmp_path / "buddy_static" / "generated" / "current.png"
    assert active_path.read_bytes() == b"PNG"


def test_buddy_hatch_motion_activation_copies_existing_video(monkeypatch, tmp_path):
    import row_bot.buddy.assets as assets_mod
    import row_bot.buddy.config as config_mod
    import row_bot.buddy.hatch as hatch_mod

    monkeypatch.setattr(config_mod, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(config_mod, "_BUDDY_CONFIG_PATH", tmp_path / "buddy_config.json")
    monkeypatch.setattr(assets_mod, "_BUDDY_STATIC_DIR", tmp_path / "buddy_static")

    motion = tmp_path / "old-motion.mp4"
    motion.write_bytes(b"MP4")

    active_path = hatch_mod.activate_hatch_motion(motion)

    assert active_path == tmp_path / "buddy_static" / "generated" / "current.mp4"
    assert active_path.read_bytes() == b"MP4"


def test_builtin_buddy_pack_contract_is_explicit():
    from row_bot.buddy.assets import REQUIRED_MOTION_CLIPS, list_buddy_packs, load_buddy_pack, static_url_for_path

    expected_pack_ids = {"ember", "glyph", "lumen", "orbit", "pixel", "sprout"}
    expected_animation_map = {
        "idle_breathe": "idle",
        "wake": "idle",
        "ping": "idle",
        "listen": "idle",
        "lean_in": "thinking",
        "think_loop": "thinking",
        "type_follow": "thinking",
        "tool_peek": "working",
        "pack_bag": "working",
        "step_check": "working",
        "nod": "working",
        "tap_glass": "approval",
        "pause": "error",
        "celebrate_small": "success",
        "celebrate_big": "success",
        "worry": "error",
        "sleep": "error",
    }

    discovered_pack_ids = {pack.id for pack in list_buddy_packs()}
    assert expected_pack_ids.issubset(discovered_pack_ids)

    for pack_id in sorted(expected_pack_ids):
        pack = load_buddy_pack(pack_id)

        assert pack.runtime == "generated_motion_pack"
        assert pack.preview_path.exists()
        assert pack.motion_pack_path.exists()
        assert REQUIRED_MOTION_CLIPS.issubset(set(pack.motion_clips))
        assert all(path.exists() and path.stat().st_size > 0 for path in pack.motion_clips.values())
        assert pack.default_clip == "idle"
        assert expected_animation_map.items() <= pack.animation_map.items()
        assert pack.animation_map["pause"] != "approval"
        assert pack.animation_map["pause"] != "success"
        assert static_url_for_path(pack.preview_path) == f"/static/buddy/builtins/{pack_id}/preview.png"
        assert static_url_for_path(pack.motion_clips["success"]) == f"/static/buddy/builtins/{pack_id}/motions/success.mp4"
        assert pack.status == "available"
        assert pack.message == "Ready"


def test_buddy_hatch_motion_map_keeps_denial_out_of_approval_clip():
    import row_bot.buddy.hatch as hatch_mod

    approval_spec = next(spec for spec in hatch_mod.MOTION_CLIP_SPECS if spec.id == "approval")
    error_spec = next(spec for spec in hatch_mod.MOTION_CLIP_SPECS if spec.id == "error")

    assert hatch_mod.MOTION_ANIMATION_MAP["tap_glass"] == "approval"
    assert hatch_mod.MOTION_ANIMATION_MAP["pause"] == "error"
    assert "pause" not in approval_spec.animations
    assert "pause" in error_spec.animations


def test_buddy_hatch_motion_specs_use_provider_supported_duration():
    import row_bot.buddy.hatch as hatch_mod

    assert all(spec.duration_seconds >= 5 for spec in hatch_mod.MOTION_CLIP_SPECS)


def test_buddy_hatch_image_prompt_requests_single_avatar_not_pose_sheet():
    import row_bot.buddy.hatch as hatch_mod

    prompt = hatch_mod._buddy_image_prompt("A tiny dog named Honey")

    assert "exactly one" in prompt
    assert "single centered avatar portrait" in prompt
    assert "Do not create a sprite sheet" in prompt
    assert "contact sheet" in prompt
    assert "grid" in prompt
    assert "multiple poses" in prompt
    assert "idle, thinking" not in prompt


def test_buddy_hatch_motion_prompt_keeps_avatar_framing_stable():
    import row_bot.buddy.hatch as hatch_mod

    prompt = hatch_mod._buddy_motion_prompt("A tiny dog named Honey", "idle")

    assert "Create one video clip only" in prompt
    assert "single identity and framing reference" in prompt
    assert "Lock the virtual camera" in prompt
    assert "no zooming" in prompt
    assert "fit inside a rounded avatar border" in prompt
    assert "at least 18 percent empty margin" in prompt
    assert "no flicker" in prompt
    assert "no background pulsing" in prompt
    assert "no size changes" in prompt
    assert "Do not create a sprite sheet" in prompt
    assert "no transparent background" in prompt
    assert "no alpha checkerboard" in prompt
    assert "no white checkerboard pattern" in prompt


def test_buddy_hatch_preserves_opaque_full_frame_motion_source(tmp_path):
    import row_bot.buddy.hatch as hatch_mod
    from PIL import Image

    preview = tmp_path / "preview.png"
    image = Image.new("RGBA", (1024, 1024), (235, 235, 235, 255))
    image.paste((240, 120, 30, 255), (430, 320, 590, 720))
    image.save(preview)

    motion_source = hatch_mod._prepare_motion_source_image(preview)

    assert motion_source.name == "motion_source.png"
    with Image.open(motion_source) as prepared:
        assert prepared.mode == "RGB"
        assert prepared.size == (1024, 1024)
        assert prepared.getpixel((0, 0)) == (235, 235, 235)
        assert prepared.getpixel((512, 512)) == (240, 120, 30)


def test_buddy_hatch_composites_transparent_motion_source(tmp_path):
    import row_bot.buddy.hatch as hatch_mod
    from PIL import Image

    preview = tmp_path / "preview.png"
    image = Image.new("RGBA", (300, 600), (0, 0, 0, 0))
    image.paste((240, 120, 30, 255), (60, 80, 240, 560))
    image.save(preview)

    motion_source = hatch_mod._prepare_motion_source_image(preview)

    assert motion_source.name == "motion_source.png"
    with Image.open(motion_source) as prepared:
        assert prepared.mode == "RGB"
        assert prepared.size == (1024, 1024)
        assert prepared.getpixel((0, 0)) == hatch_mod._MOTION_SOURCE_BACKGROUND[:3]


def test_stop_task_emits_buddy_cancel_immediately(monkeypatch):
    import row_bot.tasks as tasks_mod

    seen: list[tuple[str, dict[str, str]]] = []

    def _fake_emit(status: str, **payload: str) -> None:
        seen.append((status, payload))

    stop_event = threading.Event()
    monkeypatch.setattr(tasks_mod, "_emit_buddy_workflow_event", _fake_emit)
    with tasks_mod._active_lock:
        tasks_mod._active_runs["thread-1"] = {
            "task_id": "task-1",
            "name": "Daily Briefing",
            "stop_event": stop_event,
        }
    try:
        assert tasks_mod.stop_task("thread-1") is True
    finally:
        with tasks_mod._active_lock:
            tasks_mod._active_runs.pop("thread-1", None)

    assert stop_event.is_set()
    assert seen == [("cancelled", {"task_id": "task-1", "thread_id": "thread-1", "label": "Stopping Daily Briefing"})]


def test_buddy_hatch_background_job_starts_without_blocking(monkeypatch):
    import row_bot.buddy.hatch as hatch_mod

    with hatch_mod._JOB_LOCK:
        hatch_mod._CURRENT_JOB.clear()

    started = {"value": False}

    class FakeThread:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def start(self):
            started["value"] = True

    monkeypatch.setattr(hatch_mod.threading, "Thread", FakeThread)

    queued = hatch_mod.start_hatch_generation_job("A tiny dog named Honey", pack_id="glyph")
    running = hatch_mod.get_hatch_generation_status()

    assert started["value"] is True
    assert queued["status"] == "queued"
    assert running["status"] == "running"
    assert running["mode"] == "full"
    assert running["total_clips"] == len(hatch_mod.MOTION_CLIP_SPECS)
    try:
        hatch_mod.start_hatch_generation_job("Another Buddy", pack_id="glyph")
    except RuntimeError as exc:
        assert "already running" in str(exc)
    else:
        raise AssertionError("start_hatch_generation_job allowed overlapping generation")

    with hatch_mod._JOB_LOCK:
        hatch_mod._CURRENT_JOB.clear()


def test_buddy_hatch_can_switch_user_pack_back_to_still_only(monkeypatch, tmp_path):
    import row_bot.buddy.assets as assets_mod
    import row_bot.buddy.hatch as hatch_mod

    monkeypatch.setattr(assets_mod, "_BUDDY_STATIC_DIR", tmp_path / "buddy_static")
    monkeypatch.setattr(assets_mod, "_USER_PACKS_DIR", tmp_path / "buddy_static" / "packs")

    preview = tmp_path / "preview.png"
    preview.write_bytes(b"PNG")
    pack_id = hatch_mod.use_hatch_still_only("hatch-123", preview, prompt="A tiny dog named Honey")

    assert pack_id == "hatch-123"
    pack = assets_mod.load_buddy_pack(pack_id)
    assert pack.runtime == "generated_still"
    assert pack.status == "available"
    assert pack.name == "Honey"
    assert not pack.motion_clips


def test_buddy_can_delete_generated_hatch_pack(monkeypatch, tmp_path):
    import row_bot.buddy.assets as assets_mod
    import row_bot.buddy.hatch as hatch_mod

    monkeypatch.setattr(assets_mod, "_BUDDY_STATIC_DIR", tmp_path / "buddy_static")
    monkeypatch.setattr(assets_mod, "_USER_PACKS_DIR", tmp_path / "buddy_static" / "packs")

    preview = tmp_path / "preview.png"
    preview.write_bytes(b"PNG")
    pack_id = hatch_mod.use_hatch_still_only("hatch-123", preview, prompt="A tiny dog named Honey")
    pack_dir = assets_mod._USER_PACKS_DIR / pack_id

    assert pack_dir.exists()
    assert assets_mod.delete_generated_buddy_pack(pack_id) == pack_id
    assert not pack_dir.exists()


def test_buddy_delete_generated_pack_rejects_bundled_pack_ids():
    import row_bot.buddy.assets as assets_mod

    try:
        assets_mod.delete_generated_buddy_pack("glyph")
    except ValueError as exc:
        assert "Only generated Hatch packs" in str(exc)
    else:
        raise AssertionError("delete_generated_buddy_pack accepted a bundled pack id")


def test_buddy_brain_decays_stale_event_to_idle(monkeypatch):
    import row_bot.buddy.brain as brain_mod
    from row_bot.buddy.brain import BuddyBrain
    from row_bot.buddy.events import BuddyEvent, BuddyEventType

    monkeypatch.setattr(
        brain_mod,
        "get_buddy_config",
        lambda: {"enabled": True, "mode": "sidebar", "pack_id": "glyph"},
    )
    brain = BuddyBrain()
    brain.resolve(BuddyEvent(BuddyEventType.GENERATION_STARTED, source="test", id=9))
    monkeypatch.setattr(brain_mod.time, "time", lambda: brain.state.updated_at + 121)

    state = brain.resolve(None)

    assert state.animation == "idle_breathe"
    assert state.message == "Idle"


def test_buddy_rive_asset_install_uses_user_static_dir(monkeypatch, tmp_path):
    import row_bot.buddy.assets as assets_mod

    user_static = tmp_path / "buddy"
    monkeypatch.setattr(assets_mod, "_BUDDY_STATIC_DIR", user_static)
    monkeypatch.setattr(assets_mod, "_USER_PACKS_DIR", user_static / "packs")

    source = tmp_path / "export.riv"
    source.write_bytes(b"RIVE")

    pack = assets_mod.install_buddy_rive_asset(source, pack_id="glyph")

    assert pack.status == "available"
    assert pack.riv_path.exists()
    assert assets_mod.pack_static_url(pack) == "/_buddy/packs/glyph/buddy.riv"
    assert assets_mod.load_buddy_pack("glyph").status == "available"


def test_buddy_import_surface_is_stable():
    buddy = importlib.import_module("row_bot.buddy")

    assert hasattr(buddy, "BuddyEventType")
    assert hasattr(buddy, "emit_buddy_event")
    assert callable(buddy.get_buddy_snapshot)
