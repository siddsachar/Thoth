from __future__ import annotations

import concurrent.futures
import threading

from row_bot.computer_use.service import ComputerUseService, LeaseOwner


OWNER = LeaseOwner("vision-thread", "vision-generation", "vision-task")


class _Vision:
    _model = "local::fake-vision"

    def __init__(self) -> None:
        self.calls = []

    def analyze(self, image_bytes: bytes, question: str) -> str:
        self.calls.append((image_bytes, question))
        return "The Equals button is visible."


def test_visual_question_uses_ephemeral_capture_only_when_requested(fake_client) -> None:
    vision = _Vision()
    service = ComputerUseService(client_factory=lambda: fake_client, approval_callback=lambda _payload: True, vision_service=vision)
    service.acquire(OWNER, validate_context=False)
    target = service.list_windows(OWNER, app="Calculator")[0]["target_id"]
    service.capture(target, OWNER)
    assert vision.calls == []
    observed = service.capture(target, OWNER, visual_question="Where is Equals?")
    assert len(vision.calls) == 1
    assert vision.calls[0][0] == observed.screenshot
    assert "Equals button" in observed.model_text()
    assert "base64" not in observed.model_text()


def test_launch_app_can_vision_ground_its_single_fresh_capture(fake_client) -> None:
    vision = _Vision()
    service = ComputerUseService(
        client_factory=lambda: fake_client,
        approval_callback=lambda _payload: True,
        vision_service=vision,
    )
    service.acquire(OWNER, validate_context=False)

    windows = service.launch_app(
        "Calculator",
        OWNER,
        visual_question="Identify the screenshot-local control region.",
    )

    assert windows
    observed = service.current_observation(windows[0]["target_id"])
    assert observed is not None
    assert len(vision.calls) == 1
    assert vision.calls[0][0] == observed.screenshot
    assert "Equals button" in observed.model_text()


def test_semantic_element_action_honors_one_explicit_post_action_vision_call(fake_client) -> None:
    vision = _Vision()
    service = ComputerUseService(
        client_factory=lambda: fake_client,
        approval_callback=lambda _payload: True,
        vision_service=vision,
    )
    service.acquire(OWNER, validate_context=False)
    target = service.list_windows(OWNER, app="Calculator")[0]["target_id"]
    observed = service.capture(target, OWNER)

    service.act(
        "click",
        target,
        OWNER,
        element_token=observed.elements[0].token,
        capture_after=True,
        visual_question="Confirm the semantic button changed visually.",
    )

    assert len(vision.calls) == 1
    assert vision.calls[0][1] == "Confirm the semantic button changed visually."


def test_initial_app_capture_defers_vision_then_target_capture_calls_it_once(
    fake_client,
) -> None:
    vision = _Vision()
    service = ComputerUseService(
        client_factory=lambda: fake_client,
        approval_callback=lambda _payload: True,
        vision_service=vision,
    )

    initial = service.capture(
        owner=OWNER,
        app="Calculator",
        visual_question="Premature visual request",
    )
    grounded = service.capture(
        initial.target.target_id,
        OWNER,
        visual_question="Where is Equals?",
    )

    assert initial.vision_deferred is True
    assert len(vision.calls) == 1
    assert vision.calls[0][0] == grounded.screenshot


def test_validated_preview_is_published_before_blocked_vision_returns(
    fake_client,
) -> None:
    class _BlockingVision:
        _model = "local::blocking-vision"

        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()

        def analyze(self, _image: bytes, _question: str) -> str:
            self.started.set()
            assert self.release.wait(timeout=5)
            return "Vision finished."

    vision = _BlockingVision()
    service = ComputerUseService(
        client_factory=lambda: fake_client,
        approval_callback=lambda _payload: True,
        vision_service=vision,
    )
    initial = service.capture(owner=OWNER, app="Calculator")
    published = threading.Event()
    snapshots: list[dict] = []

    def listener(snapshot: dict) -> None:
        snapshots.append(snapshot)
        if snapshot.get("has_thumbnail"):
            published.set()

    service.add_listener(listener)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            service.capture,
            initial.target.target_id,
            OWNER,
            visual_question="Where is Equals?",
        )
        assert published.wait(timeout=5)
        assert vision.started.wait(timeout=5)
        assert future.done() is False
        assert service.ephemeral_screenshot()
        preview_revision = snapshots[-1]["revision"]
        vision.release.set()
        observed = future.result(timeout=5)

    assert observed.vision_text
    assert snapshots[-1]["revision"] > preview_revision
