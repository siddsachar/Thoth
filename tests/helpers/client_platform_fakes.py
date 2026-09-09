"""Scripted external execution for tests of the real client-platform services.

These fakes replace provider/tool calls only. Checkpoint mutations go through
``row_bot.threads``; application services, task receipts and projection owners
remain the production implementations. Explicit Events select contested
interleavings without sleeps or a live account.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import threading
from typing import Any, Iterator
from uuid import NAMESPACE_URL, uuid5


def fixture_id(name: str) -> str:
    """Return a reproducible opaque identity for a named synthetic fixture."""
    return str(uuid5(NAMESPACE_URL, "row-bot-client-platform-fixture:" + name))


@dataclass
class StreamBarrier:
    """Block an actual producer until the test explicitly releases it.

    ``release_on_cancel`` models a cancellable provider transport; False
    models an uninterruptible effect whose Stop acknowledgement is not proof
    of quiescence. Timeouts only bound failed tests and never order success.
    """

    release_on_cancel: bool = False
    entered: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    exited: threading.Event = field(default_factory=threading.Event)
    timeout_seconds: float = 10.0

    def wait(self, stop_event: threading.Event | None) -> None:
        self.entered.set()
        try:
            target = stop_event if self.release_on_cancel and stop_event is not None else self.release
            if not target.wait(self.timeout_seconds):
                raise TimeoutError("Synthetic provider barrier was not released")
        finally:
            self.exited.set()


@dataclass(frozen=True)
class CheckpointCommit:
    """Append explicit native messages at this exact scripted boundary."""

    messages: tuple[Any, ...]
    output_message_id: str | None = None


@dataclass(frozen=True)
class ToolMediaResult:
    """Supply one external tool result to the real scoped media capture owner."""

    message_id: str
    tool_call_id: str
    pending_image: str


ScriptStep = tuple[str, Any] | StreamBarrier | CheckpointCommit | ToolMediaResult


class ScriptedAgentStream:
    """Existing ``stream_agent`` / ``resume_stream_agent`` factory signatures.

    A script must explicitly contain checkpoint commits and done/interrupt
    events. The fake never invents a durable final or supplies a replacement
    generation service. Calls and emitted events are recorded independently
    so subscriber replay can assert it never dispatches another producer.
    """

    def __init__(self, *scripts: tuple[ScriptStep, ...]) -> None:
        self._scripts = deque(scripts)
        self._lock = threading.Lock()
        self.calls: list[dict[str, Any]] = []
        self.events: list[tuple[str, Any]] = []
        self.checkpoint_commits: list[dict[str, Any]] = []
        self.started = threading.Event()
        self.quiesced = threading.Event()
        self.external_call_count = 0

    def add_script(self, *steps: ScriptStep) -> None:
        with self._lock:
            self._scripts.append(tuple(steps))

    def stream(
        self,
        text: str,
        enabled_tools: list[str],
        config: dict[str, Any],
        *,
        stop_event: threading.Event | None = None,
    ) -> Iterator[tuple[str, Any]]:
        yield from self._run(config, stop_event=stop_event, kind="submit")

    def resume(
        self,
        enabled_tools: list[str],
        config: dict[str, Any],
        approved: bool,
        *,
        interrupt_ids: list[str] | None = None,
        stop_event: threading.Event | None = None,
    ) -> Iterator[tuple[str, Any]]:
        yield from self._run(config, stop_event=stop_event, kind="resume",
                             approved=approved, interrupt_ids=tuple(interrupt_ids or ()))

    def _run(
        self,
        config: dict[str, Any],
        *,
        stop_event: threading.Event | None,
        kind: str,
        **details: Any,
    ) -> Iterator[tuple[str, Any]]:
        configurable = config.get("configurable", {})
        thread_id = str(configurable["thread_id"])
        with self._lock:
            if not self._scripts:
                raise AssertionError("Unexpected provider dispatch: no scripted invocation remains")
            steps = self._scripts.popleft()
            self.calls.append({"kind": kind, "conversation_id": thread_id,
                               "generation_id": configurable.get("generation_id"),
                               "submission_id": configurable.get("platform_submission_id"),
                               "stop_event": stop_event, **details})
            self.quiesced.clear()
        self.started.set()
        try:
            for step in steps:
                if isinstance(step, StreamBarrier):
                    step.wait(stop_event)
                    continue
                if stop_event is not None and stop_event.is_set():
                    return
                if isinstance(step, ToolMediaResult):
                    from row_bot.application.attachment_context import current_caches
                    from row_bot.application.generated_media import capture_generated_media

                    caches = current_caches()
                    if caches is None or caches.conversation_id != thread_id:
                        raise AssertionError("Tool fixture requires the actual execution attachment scope")
                    caches.pending_image = step.pending_image
                    step = ("tool_done", {"tool_call_id": step.tool_call_id, "message_id": step.message_id,
                                          "media": capture_generated_media(thread_id, caches)})
                if isinstance(step, CheckpointCommit):
                    from row_bot.threads import append_checkpoint_messages, get_latest_checkpoint_revision

                    if not append_checkpoint_messages(thread_id, list(step.messages)):
                        raise AssertionError("Synthetic checkpoint commit was rejected")
                    with self._lock:
                        self.checkpoint_commits.append({"conversation_id": thread_id,
                                                       "message_ids": [message.id for message in step.messages],
                                                       "checkpoint_revision": get_latest_checkpoint_revision(thread_id)})
                    if step.output_message_id is not None:
                        if not any(message.id == step.output_message_id and message.type == "ai"
                                   for message in step.messages):
                            raise AssertionError("Output binding must name an exact AI message in this commit")
                        event = ("output_binding", {"native_message_id": step.output_message_id,
                                                    "checkpoint_revision": get_latest_checkpoint_revision(thread_id)})
                        with self._lock:
                            self.events.append(event)
                        yield event
                    continue
                with self._lock:
                    self.events.append(step)
                yield step
        finally:
            self.quiesced.set()


class FakeMonotonicClock:
    """Manually advanced deadline clock; test success never depends on sleep."""

    def __init__(self, initial: float = 1_000.0) -> None:
        self.value = initial

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("A monotonic fixture clock cannot move backwards")
        self.value += seconds


class RecordedProtocolTrace:
    """Record synthetic, schema-checked wire values without process-specific IDs.

    This helper never starts a service or invents evidence. The caller must
    obtain values from the real application path (or explicitly label a later
    phase's DTO-only contract). Writes are opt-in and confined to fixture roots.
    """

    def __init__(self, fixture: str, *, kind: str = "service_recording") -> None:
        self.fixture = fixture
        self.kind = kind
        self.records: list[dict[str, Any]] = []
        self.barriers: list[str] = []
        self.assertions: list[str] = []
        self._identities: dict[str, str] = {}
        self._policy_revisions: dict[str, str] = {}

    def _identity(self, original: str) -> str:
        return self._identities.setdefault(
            original, fixture_id(f"{self.fixture}:recorded-identity:{len(self._identities)}"))

    def normalize(self, value: Any, *, key: str = "") -> Any:
        import re
        if isinstance(value, dict):
            return {name: self.normalize(item, key=name) for name, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.normalize(item) for item in value]
        if not isinstance(value, str):
            return value
        if key == "policy_revision":
            # This is a process-keyed opaque revision, not an ordered domain
            # counter. Preserve equality while retaining the decimal wire type.
            return self._policy_revisions.setdefault(value, str(len(self._policy_revisions) + 1))
        if key in {"cursor", "next_cursor", "previous_cursor", "csrf_token", "nonce"} and value:
            # Opaque proofs are transport artifacts, never executable credentials
            # in a portable fixture. Preserve equality/references, not HMACs.
            return self._identity(value)
        if key in {"expires_at", "created_at", "updated_at"} and value:
            return "2030-01-01T00:00:00+00:00"
        if re.fullmatch(r"[0-9a-f]{12}", value):
            return self._identity(value)  # Existing approval owner uses short random IDs.
        return re.sub(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
                      lambda match: self._identity(match.group()), value)

    def record(self, schema: str, value: Any) -> int:
        self.records.append({"schema": schema, "value": self.normalize(value)})
        return len(self.records) - 1

    def document(self, root: Any, *, final_snapshot: dict | None = None,
                 delivery_order: list[int] | None = None) -> dict:
        import hashlib
        import json
        from pathlib import Path

        root = Path(root)
        source_paths = [*root.joinpath("src/row_bot/application").glob("*.py"),
                        *root.joinpath("src/row_bot/runtime").glob("*.py"),
                        *root.joinpath("src/row_bot/projection").glob("*.py"),
                        *root.joinpath("src/row_bot/api/v1").glob("*.py"),
                        root / "src/row_bot/tasks.py", root / "src/row_bot/threads.py",
                        root / "src/row_bot/conversation_resources.py", Path(__file__)]
        fingerprints = {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in sorted(source_paths) if path.exists()}
        return {
            "recording_version": 1, "protocol_version": "1.0", "fixture_id": self.fixture,
            "kind": self.kind, "deterministic_seed": self.fixture + ":v1",
            "source": {"base_commit": "272a0add768f7badc2536fa21e30e538a08833de",
                       "files": fingerprints,
                       "sha256": hashlib.sha256(json.dumps(fingerprints, sort_keys=True).encode()).hexdigest()},
            "records": self.records, "delivery_order": delivery_order or list(range(len(self.records))),
            "barriers": self.barriers, "assertions": self.assertions,
            "allowed_external_effects": [],
            "bounds": {"quiescence_timeout_seconds": 10, "event_bytes": 65536},
            "expected_final_snapshot": self.normalize(final_snapshot) if final_snapshot is not None else None,
        }

    @staticmethod
    def validate(root: Any, document: dict) -> None:
        import json
        from pathlib import Path
        from jsonschema import Draft202012Validator, FormatChecker

        root = Path(root)
        fixture_root = root / "contracts/client-platform/v1/fixtures"
        shape = json.loads((fixture_root / "recording.schema.json").read_text(encoding="utf-8"))
        Draft202012Validator(shape).validate(document)
        for record in document["records"]:
            schema = json.loads((root / "contracts/client-platform/v1/schema" / (record["schema"] + ".schema.json")).read_text(encoding="utf-8"))
            Draft202012Validator(schema, format_checker=FormatChecker()).validate(record["value"])
            if record["schema"] == "Event":
                assert len(json.dumps(record["value"], ensure_ascii=False).encode()) <= 65536
                assert int(record["value"]["source_sequence_start"]) <= int(record["value"]["source_sequence_end"])
        assert all(0 <= index < len(document["records"]) for index in document["delivery_order"])

    @staticmethod
    def write(root: Any, document: dict) -> Any:
        import json
        import os
        from pathlib import Path

        if os.environ.get("ROW_BOT_RECORD_PROTOCOL_FIXTURES") != "1":
            raise RuntimeError("Recording writes require the explicit fixture-generation environment flag")
        root = Path(root).resolve()
        RecordedProtocolTrace.validate(root, document)
        destination = (root / "contracts/client-platform/v1/fixtures" / (document["fixture_id"] + ".json")).resolve()
        if not destination.is_relative_to(root / "contracts/client-platform/v1/fixtures"):
            raise ValueError("Fixture destination escaped its assigned root")
        destination.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return destination
