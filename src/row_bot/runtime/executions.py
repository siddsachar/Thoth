"""One process owner for producer cancellation and acknowledged quiescence.

Domain records stay in their existing stores. Handles deliberately contain no
prompt, transcript, tool arguments, page, or renderer objects.
"""

from __future__ import annotations

import contextlib
import contextvars
import threading
import time
import uuid
from dataclasses import dataclass, field
from collections.abc import Callable, Iterator

from row_bot.cancellation import CancellationScope, use_cancellation_scope


@dataclass
class ExecutionHandle:
    execution_id: str
    conversation_id: str
    generation_id: str
    pass_id: str
    domain: str
    domain_id: str
    server_epoch: str
    cancel_scope: CancellationScope
    deadline: float | None = None
    status: str = "running"
    revision: int = 1
    approval_id: str = ""
    producer_done: threading.Event = field(default_factory=threading.Event)
    cleanup_complete: bool = False
    external_outcome: str = "known_not_sent"
    segment_id: str = ""
    segment_committed: bool = False
    model_ref: str = ""
    runtime_surface: str = "normal_chat"
    invocation_started: bool = False
    output_message_id: str = ""
    output_checkpoint_revision: str = ""
    input_checkpoint_revision: str = ""


    def view(self) -> dict:
        return {
            "execution_id": self.execution_id,
            "conversation_id": self.conversation_id,
            "generation_id": self.generation_id,
            "pass_id": self.pass_id,
            "status": self.status,
            "revision": str(self.revision),
            "cancel_requested": self.cancel_scope.is_cancelled(),
            "quiesced": self.producer_done.is_set(),
            "cleanup_complete": self.cleanup_complete,
            "external_outcome": self.external_outcome,
            "approval_id": self.approval_id or None,
            "can_stop": not self.producer_done.is_set(),
            "segment_id": self.segment_id or None,
        }


_current_execution: contextvars.ContextVar[ExecutionHandle | None] = contextvars.ContextVar("row_bot_execution", default=None)


def current_execution() -> ExecutionHandle | None:
    return _current_execution.get()


class _EntryRejected(Exception):
    """Internal unwind marker after the domain has acknowledged entry failure."""


class _WorkerStartGate:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.entered = False
        self.rejected = False

    def enter(self) -> bool:
        with self.lock:
            if self.rejected:
                return False
            self.entered = True
            return True

    def reject_unstarted(self, worker: threading.Thread) -> bool:
        with self.lock:
            if self.entered or worker.ident is not None or worker.is_alive():
                return False
            self.rejected = True
            return True


class GenerationRuntimeRegistry:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.server_epoch = str(uuid.uuid4())
        self.clock = clock
        self._lock = threading.RLock()
        self._handles: dict[str, ExecutionHandle] = {}
        self._closed = False

    def register(self, conversation_id: str, *, generation_id: str = "",
                 pass_id: str = "", domain: str = "conversation", domain_id: str = "",
                 stop_event: threading.Event | None = None,
                 cancel_scope: CancellationScope | None = None,
                 deadline: float | None = None) -> ExecutionHandle:
        with self._lock:
            if self._closed:
                raise ValueError("runtime_closed")
            if any(h.conversation_id == conversation_id and h.domain == domain
                   and h.domain_id == domain_id and not h.producer_done.is_set()
                   for h in self._handles.values()):
                raise ValueError("execution_already_active")
            handle = ExecutionHandle(
                str(uuid.uuid4()), conversation_id, generation_id or str(uuid.uuid4()),
                pass_id or str(uuid.uuid4()), domain, domain_id, self.server_epoch,
                cancel_scope or CancellationScope(stop_event), deadline,
            )
            self._handles[handle.execution_id] = handle
            return handle

    def active(self, conversation_id: str = "") -> tuple[ExecutionHandle, ...]:
        with self._lock:
            return tuple(h for h in self._handles.values()
                         if not h.producer_done.is_set()
                         and (not conversation_id or h.conversation_id == conversation_id))

    def get(self, execution_id: str) -> ExecutionHandle | None:
        with self._lock:
            return self._handles.get(execution_id)

    def stop(self, conversation_id: str, *, reason: str = "user") -> bool:
        handles = self.active(conversation_id)
        for handle in handles:
            self.cancel(handle, reason=reason)
        return bool(handles)

    def cancel(self, handle: ExecutionHandle, *, reason: str = "user") -> None:
        with self._lock:
            if handle.producer_done.is_set():
                return
            handle.status = "stopping"
            handle.revision += 1
        handle.cancel_scope.cancel(reason)

    def check_dispatch(self, handle: ExecutionHandle) -> None:
        if handle.deadline is not None and self.clock() >= handle.deadline:
            self.cancel(handle, reason="deadline")
        if handle.cancel_scope.is_cancelled():
            handle.cancel_scope.cancel(handle.cancel_scope.reason or "stop_event")
            raise InterruptedError("execution_cancelled")

    def finish(self, handle: ExecutionHandle, *, status: str = "completed") -> None:
        """Called only by the producer after its owned cleanup has returned."""
        with self._lock:
            if handle.producer_done.is_set():
                return
            handle.status = "stopped" if handle.cancel_scope.is_cancelled() else status
            handle.cleanup_complete = True
            handle.producer_done.set()
            handle.revision += 1
            # Finished handles are retained only as bounded diagnostic facts.
            finished = [h for h in self._handles.values() if h.producer_done.is_set()]
            for old in finished[:-256]:
                self._handles.pop(old.execution_id, None)

    @contextlib.contextmanager
    def ownership(self, handle: ExecutionHandle, *, check_entry: bool = True) -> Iterator[ExecutionHandle]:
        status = "completed"
        token = _current_execution.set(handle)
        try:
            with use_cancellation_scope(handle.cancel_scope):
                try:
                    if check_entry:
                        self.check_dispatch(handle)
                    yield handle
                except BaseException:
                    status = "interrupted"
                    raise
        finally:
            _current_execution.reset(token)
            if not handle.producer_done.is_set():
                self.finish(handle, status=status)

    def _guard_thread_start(self, worker: threading.Thread, handle: ExecutionHandle,
                            gate: _WorkerStartGate,
                            on_entry_failure: Callable[[BaseException], None] | None) -> threading.Thread:
        original_start = worker.start
        start_lock = threading.Lock()
        failed = False
        def start() -> None:
            nonlocal failed
            with start_lock:
                if failed:
                    raise RuntimeError("execution_start_failed")
                try:
                    original_start()
                except (RuntimeError, OSError) as exc:
                    # Native Thread.start failures before an OS thread exists
                    # prove no target can run. Interrupts or started workers do
                    # not establish quiescence and retain their existing owner.
                    if gate.reject_unstarted(worker):
                        failed = True
                        with self.ownership(handle, check_entry=False):
                            if on_entry_failure is not None:
                                on_entry_failure(exc)
                            raise
                    raise
        worker.start = start
        return worker

    def launch(self, handle: ExecutionHandle, producer: Callable[[], None], *,
               on_entry_failure: Callable[[BaseException], None] | None = None) -> threading.Thread:
        gate = _WorkerStartGate()
        def owned() -> None:
            if not gate.enter():
                return
            # Application producers own durable finalization in their finally
            # block and check cancellation before their first operation.
            with self.ownership(handle, check_entry=False):
                producer()
        worker = threading.Thread(target=owned, name=f"row-bot-{handle.domain}", daemon=True)
        self._guard_thread_start(worker, handle, gate, on_entry_failure)
        worker.start()
        return worker

    def thread(self, *, target: Callable, args: tuple = (), conversation_id: str,
               stop_event: threading.Event, domain: str, domain_id: str = "",
               generation_id: str = "", name: str = "row-bot-worker",
               resource_context: bool = False,
               on_entry_failure: Callable[[BaseException], None] | None = None) -> threading.Thread:
        """Build an owned thread for retained domain runners before it starts."""
        try:
            handle = self.register(conversation_id, stop_event=stop_event, domain=domain,
                                   domain_id=domain_id, generation_id=generation_id)
        except BaseException as exc:
            existing_owner = any(h.domain == domain and h.domain_id == domain_id
                                 for h in self.active(conversation_id))
            if on_entry_failure is not None and not existing_owner:
                on_entry_failure(exc)
            raise
        gate = _WorkerStartGate()
        def owned() -> None:
            if not gate.enter():
                return
            try:
                with self.ownership(handle, check_entry=False), contextlib.ExitStack() as resources:
                    try:
                        self.check_dispatch(handle)
                        if resource_context:
                            from row_bot.conversation_resources import execution_context
                            resources.enter_context(execution_context(conversation_id))
                    except BaseException as exc:
                        if on_entry_failure is None:
                            raise
                        on_entry_failure(exc)
                        # Unwind resources and cancellation context before the
                        # registry publishes its one quiescence acknowledgment.
                        raise _EntryRejected from None
                    target(*args)
            except _EntryRejected:
                return
        worker = threading.Thread(target=owned, daemon=True, name=name)
        return self._guard_thread_start(worker, handle, gate, on_entry_failure)

    def shutdown(self) -> tuple[dict, ...]:
        with self._lock:
            self._closed = True
        handles = self.active()
        for handle in handles:
            self.cancel(handle, reason="shutdown")
        # Unacknowledged producers remain registered; timeout is not cleanup.
        return tuple(h.view() for h in handles)


generation_registry = GenerationRuntimeRegistry()
