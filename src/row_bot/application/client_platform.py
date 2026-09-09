"""Application services shared by authenticated clients and legacy adapters."""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import threading
import uuid
from collections.abc import Callable
from contextlib import closing
from datetime import datetime, timezone
from dataclasses import asdict
from typing import Any

from row_bot.runtime import admissions
from row_bot.runtime.executions import generation_registry
from row_bot.projection.conversation import conversation_projection


class ClientPlatformError(ValueError):
    def __init__(self, code: str, current_revision: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.current_revision = current_revision


_COMMAND_LOCK = threading.RLock()


class ClientPlatformService:
    def __init__(self, stream_factory: Callable | None = None,
                 resume_factory: Callable | None = None) -> None:
        self.stream_factory = stream_factory
        self.resume_factory = resume_factory
        self.registry = generation_registry
        self.projection = conversation_projection

    @property
    def instance_id(self) -> str:
        return admissions.instance_identity()

    @property
    def server_epoch(self) -> str:
        return self.registry.server_epoch

    def admit_execution(self, conversation_id: str, config: dict, *, text: str | None = None,
                        cancel_scope: Any = None) -> Any:
        """Single admission path for the API and retained NiceGUI producer."""
        from langchain_core.messages import HumanMessage
        from row_bot import threads
        from row_bot.models import get_current_model
        from row_bot.providers.selection import model_choice_value
        with _COMMAND_LOCK:
            configurable = config.setdefault("configurable", {})
            configurable["model_override"] = model_choice_value(
                configurable.get("model_override") or get_current_model())
            self._metadata(conversation_id)
            threads.migrate_checkpoint_message_ids(conversation_id)
            if self.registry.active(conversation_id):
                raise ClientPlatformError("generation_active")
            submission_id = str(configurable.get("platform_submission_id") or uuid.uuid4())
            generation_id = str(configurable.get("generation_id") or uuid.uuid4())
            admitted = admissions.reserve(conversation_id, submission_id, generation_id,
                                           command_id=str(configurable.get("platform_command_id") or ""))
            if text is not None and not threads.append_checkpoint_messages(
                    conversation_id, [HumanMessage(content=text, id=submission_id)]):
                raise ClientPlatformError("checkpoint_unavailable")
            admissions.admit(admitted["pass_id"], threads.get_latest_checkpoint_revision(conversation_id))
            handle = self.registry.register(conversation_id, generation_id=generation_id,
                                            pass_id=admitted["pass_id"], cancel_scope=cancel_scope)
            admissions.start(handle.pass_id, handle.execution_id, handle.server_epoch)
            handle.segment_id = admissions.start_segment(handle.pass_id)
            handle.input_checkpoint_revision = threads.get_latest_checkpoint_revision(conversation_id)
            handle.model_ref = str(configurable.get("model_override") or "")
            handle.runtime_surface = str(configurable.get("runtime_surface") or "normal_chat")
            configurable.update({"generation_id": generation_id, "platform_submission_id": submission_id,
                                 "platform_pass_id": handle.pass_id, "platform_segment_id": handle.segment_id})
            self.projection.publish(conversation_id, "generation.state", handle.view())
            self._publish_queue(conversation_id)
            return handle

    def _publish_queue(self, conversation_id: str) -> None:
        self.projection.publish(conversation_id, "queue.updated", {
            "submission_ids": admissions.queued_submission_ids(conversation_id),
            "revision": str(int(self.projection.snapshot(conversation_id)["projection_revision"]) + 1)})

    def finish_execution(self, handle: Any, status: str) -> None:
        # Admission cannot overtake terminal publication. The actual worker's
        # acknowledgement is the final operation after durable/projector cleanup.
        with _COMMAND_LOCK:
            if handle.producer_done.is_set():
                return
            status = "stopped" if handle.cancel_scope.is_cancelled() else status
            self._refresh_checkpoint(handle.conversation_id)
            from row_bot.application.live_content import discard, references
            for reference in references(handle.conversation_id):
                if reference.startswith(f"live:{handle.pass_id}:"):
                    self.projection.settle_live(handle.conversation_id, "assistant:" + reference, adopted=False)
                    discard(handle.conversation_id, reference)
            status = admissions.finish(handle.pass_id, status, boundary={
                "execution_id": handle.execution_id, "server_epoch": handle.server_epoch,
                "segment_id": handle.segment_id, "message_id": handle.output_message_id,
                "checkpoint_revision": handle.output_checkpoint_revision})
            self._publish_queue(handle.conversation_id)
            final_view = {**handle.view(), "status": status, "revision": str(handle.revision + 1),
                          "quiesced": True, "cleanup_complete": True, "can_stop": False}
            self.projection.publish(handle.conversation_id, "generation.state", final_view)
            self.registry.finish(handle, status=status)

    def _metadata(self, conversation_id: str) -> dict:
        from row_bot import threads
        threads._ensure_thread_db()
        with closing(sqlite3.connect(threads.DB_PATH)) as conn, conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM thread_meta WHERE thread_id=?", (conversation_id,)).fetchone()
            if not row:
                raise ClientPlatformError("not_found")
            return dict(row)

    def get_conversation(self, conversation_id: str) -> dict:
        row = self._metadata(conversation_id)
        from row_bot.conversation_resources import list_bindings
        resources = list_bindings(conversation_id)
        return {"id": conversation_id, "revision": str(row["client_revision"]),
                "title": row["name"], "pinned": bool(row["pinned_at"]),
                "generation_state": [h.view() for h in self.registry.active(conversation_id)],
                "resource_bindings": [asdict(binding) for binding in resources.bindings]}

    def register_attachment(self, *, owner_id: str, idempotency_key: str, command_id: str,
                            client_session_id: str, conversation_id: str, name: str,
                            data: bytes, mime_type: str, validate: Callable[[], None] | None = None) -> dict:
        from row_bot.application.attachments import register_attachment, read_attachment
        self._metadata(conversation_id)
        command = {"command_id": command_id, "client_session_id": client_session_id,
                   "type": "attachment.register", "payload": {"name": name, "mime_type": mime_type,
                   "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}}
        with _COMMAND_LOCK:
            try:
                if validate:
                    validate()
                replay = admissions.claim_command(owner_id, idempotency_key, command, conversation_id)
                if replay:
                    metadata, _ = read_attachment(replay["attachment_ref"])
                    return metadata
                if validate:
                    validate()
                metadata = register_attachment(conversation_id, name, data, mime_type)
                admissions.complete_command(owner_id, idempotency_key,
                    {"command_id": command_id, "attachment_ref": metadata["attachment_ref"], "status": "completed"})
                return metadata
            except admissions.AdmissionError as exc:
                raise ClientPlatformError(str(exc)) from exc

    def list_conversations(self, limit: int = 50, cursor: str | None = None) -> dict:
        from row_bot import threads
        threads._ensure_thread_db()
        limit = min(200, max(1, limit))
        after = ""
        if cursor:
            try:
                after = base64.urlsafe_b64decode(cursor.encode()).decode()
            except Exception as exc:
                raise ClientPlatformError("cursor_expired") from exc
        with closing(sqlite3.connect(threads.DB_PATH)) as conn, conn:
            rows = conn.execute("SELECT thread_id FROM thread_meta WHERE thread_id>? ORDER BY thread_id LIMIT ?",
                                (after, limit + 1)).fetchall()
        more = len(rows) > limit
        selected = rows[:limit]
        return {"items": [self.get_conversation(row[0]) for row in selected], "has_more": more,
                "next_cursor": base64.urlsafe_b64encode(selected[-1][0].encode()).decode() if more else None}

    def _refresh_checkpoint(self, conversation_id: str) -> None:
        from row_bot import threads
        from row_bot.runtime.checkpoint_reader import open_checkpoint
        with threads.checkpoint_mutation(conversation_id):
            threads.migrate_checkpoint_message_ids(conversation_id)
            with open_checkpoint(conversation_id) as reader:
                if reader and reader.revision != self.projection.snapshot(conversation_id)["checkpoint_revision"]:
                    rows = [reader.public_row(record) for _, record in reader.records(start=-100)]
                    self.projection.install_rows(conversation_id, reader.revision, rows)

    def snapshot(self, conversation_id: str) -> dict:
        from row_bot.application.live_content import references
        with _COMMAND_LOCK:
            self._metadata(conversation_id)
            self._refresh_checkpoint(conversation_id)
            snapshot = self.projection.snapshot(conversation_id)
            present = {row["id"] for row in snapshot["rows"]}
            for reference in references(conversation_id)[-100:]:
                row_id = "assistant:" + reference
                if row_id not in present:
                    row = {"id": row_id, "role": "assistant", "blocks": [],
                           "content_status": "lazy", "content_ref": reference,
                           "render_revision": snapshot["projection_revision"]}
                    snapshot["rows"].append(row)
                    while len(json.dumps(snapshot["rows"], ensure_ascii=False).encode()) > self.projection.MAX_CONTENT_BYTES:
                        snapshot["rows"].pop(0)
            return snapshot

    def transcript(self, conversation_id: str, limit: int = 100, cursor: str | None = None) -> dict:
        snap = self.snapshot(conversation_id)
        from row_bot.runtime.checkpoint_reader import open_checkpoint
        offset = 0
        if cursor:
            try:
                pinned, offset = json.loads(base64.urlsafe_b64decode(cursor.encode()))
                offset = int(offset)
                if pinned != snap["checkpoint_revision"] or offset < 0:
                    raise ValueError()
            except Exception as exc:
                raise ClientPlatformError("cursor_expired") from exc
        bounded = []
        size = 0
        more, end = False, offset
        with open_checkpoint(conversation_id, snap["checkpoint_revision"]) as reader:
            if reader:
                for index, record in reader.records(start=offset):
                    row = reader.public_row(record)
                    encoded_size = len(json.dumps(row, ensure_ascii=False).encode())
                    if len(bounded) >= min(100, max(1, limit)) or (bounded and size + encoded_size > 256 * 1024):
                        more = True
                        break
                    bounded.append(row)
                    size += encoded_size
                    end = index + 1
        return {**{k: v for k, v in snap.items() if k != "rows"}, "rows": bounded,
                "has_more": more, "previous_cursor": None,
                "next_cursor": base64.urlsafe_b64encode(json.dumps([snap["checkpoint_revision"], end]).encode()).decode() if more else None}

    def lazy_content(self, conversation_id: str, message_id: str, limit_bytes: int = 65536,
                     cursor: str | None = None) -> dict:
        """Read exact public content from a checkpoint pinned by an opaque cursor."""
        self._metadata(conversation_id)
        if message_id.startswith("live:"):
            from row_bot.application.live_content import read_page
            return read_page(conversation_id, message_id, limit_bytes=limit_bytes, cursor=cursor)
        from row_bot.runtime.checkpoint_reader import open_checkpoint
        with open_checkpoint(conversation_id) as reader:
            if reader is None:
                raise ClientPlatformError("not_found")
            revision, offset = reader.revision, 0
            if cursor:
                try:
                    pinned, identity, offset = json.loads(base64.urlsafe_b64decode(cursor))
                    if pinned != revision or identity != message_id or not isinstance(offset, int) or offset < 0:
                        raise ValueError()
                except Exception as exc:
                    raise ClientPlatformError("cursor_expired") from exc
            native_id = message_id.removesuffix(":tool_calls")
            matches = [record for _, record in reader.records() if record["message_id"] == native_id]
            if len(matches) != 1:
                raise ClientPlatformError("not_found")
            limit_bytes = min(65536, max(1, limit_bytes))
            selected, position, more = bytearray(), 0, False
            chunks = reader.tool_ids_chunks(matches[0]) if message_id.endswith(":tool_calls") else reader.content_chunks(matches[0])
            for chunk in chunks:
                end = position + len(chunk)
                if end > offset:
                    available = chunk[max(0, offset - position):]
                    take = min(limit_bytes - len(selected), len(available))
                    selected.extend(available[:take])
                    if take < len(available):
                        more = True
                        break
                position = end
        next_cursor = base64.urlsafe_b64encode(json.dumps([revision, message_id, offset + len(selected)]).encode()).decode() if more else None
        return {"conversation_id": conversation_id, "content_ref": message_id,
                "checkpoint_revision": revision, "encoding": "base64", "media_type": "application/json",
                "data": base64.b64encode(selected).decode(), "has_more": more, "next_cursor": next_cursor}

    def events_since(self, conversation_id: str, cursor: str | None = None) -> dict:
        self._metadata(conversation_id)
        return self.projection.events_since(conversation_id, cursor)

    def receipt(self, owner_id: str, command_id: str) -> dict:
        result = admissions.receipt(owner_id, command_id)
        if result is None:
            raise ClientPlatformError("not_found")
        return result

    def execute(self, *, owner_id: str, idempotency_key: str, command: dict, target: str,
                validate: Callable[[], None] | None = None) -> dict:
        with _COMMAND_LOCK:
            claimed = False
            try:
                if validate:
                    validate()
                replay = admissions.claim_command(owner_id, idempotency_key, command, target)
                if replay is not None:
                    return replay
                claimed = True
                if validate:
                    validate()
                result = self._execute(command, target)
                result["command_id"] = command["command_id"]
                return admissions.complete_command(owner_id, idempotency_key, result)
            except admissions.AdmissionError as exc:
                if claimed:
                    admissions.reject_command(owner_id, idempotency_key, str(exc), exc.current_revision)
                raise ClientPlatformError(str(exc), exc.current_revision) from exc
            except ClientPlatformError as exc:
                if claimed and exc.code != "checkpoint_unavailable":
                    admissions.reject_command(owner_id, idempotency_key, exc.code, exc.current_revision)
                raise

    def _execute(self, command: dict, target: str) -> dict:
        from row_bot import threads
        payload = command.get("payload") or {}
        kind = command["type"]
        if kind == "conversation.create":
            conversation_id = str(uuid.uuid4())
            threads._save_thread_meta(conversation_id, str(payload.get("title") or "New conversation"))
            return {"conversation_id": conversation_id, "revision": "0", "status": "completed"}
        if kind == "approval.resolve":
            approval = self.get_approval(target)
            if str(command.get("expected_revision")) != approval["revision"]:
                raise ClientPlatformError("revision_conflict", approval["revision"])
            return self._resolve_approval(target, payload)
        row = self._metadata(target)
        expected = command.get("expected_revision")
        if expected is None or str(expected) != str(row["client_revision"]):
            raise ClientPlatformError("revision_conflict", str(row["client_revision"]))
        if kind in {"conversation.rename", "conversation.pin"}:
            with closing(sqlite3.connect(threads.DB_PATH)) as conn, conn:
                if kind.endswith("rename"):
                    field, value = "name", str(payload.get("title") or "").strip()[:120]
                else:
                    field, value = "pinned_at", datetime.now(timezone.utc).isoformat() if payload.get("pinned") else ""
                changed = conn.execute(f"UPDATE thread_meta SET {field}=?,client_revision=client_revision+1 WHERE thread_id=? AND client_revision=?",
                                       (value, target, row["client_revision"])).rowcount
                if not changed:
                    raise ClientPlatformError("revision_conflict")
            return {"conversation_id": target, "revision": str(row["client_revision"] + 1), "status": "completed"}
        if kind in {"conversation.bind", "conversation.unbind"}:
            from row_bot.conversation_resources import bind, unbind, ResourceError
            try:
                if kind.endswith("unbind"):
                    resources = unbind(target, str(payload["binding_id"]), expected_revision=int(expected))
                else:
                    resources = bind(target, str(payload["kind"]), str(payload["resource_id"]),
                                     role=str(payload.get("role") or "context"), expected_revision=int(expected),
                                     expected_resource_revision=payload.get("expected_resource_revision"))
            except ResourceError as exc:
                raise ClientPlatformError(exc.code, str(exc.current_revision) if exc.current_revision is not None else None) from exc
            self.projection.publish(target, "resource.changed", {"revision": str(resources.revision)})
            return {"conversation_id": target, "revision": str(resources.revision), "status": "completed"}
        if kind in {"conversation.submit", "conversation.resume"}:
            if kind == "conversation.resume":
                from row_bot.tasks import _get_conn
                with _get_conn() as conn:
                    pending = conn.execute("SELECT 1 FROM approval_requests WHERE source_thread_id=? AND resume_kind='conversation' AND status='pending'", (target,)).fetchone()
                if pending:
                    raise ClientPlatformError("approval_required")
            return self._start(target, payload, resume=kind.endswith("resume"), command_id=str(command["command_id"]))
        if kind == "conversation.stop":
            self.registry.stop(target)
            for handle in self.registry.active(target):
                self.projection.publish(target, "generation.state", handle.view())
            return {"conversation_id": target, "status": "cancel_requested"}
        if kind == "conversation.steer":
            from row_bot.agent_orchestrator import get_active_orchestration, route_parent_steering
            orchestration = get_active_orchestration(target)
            if not orchestration:
                raise ClientPlatformError("generation_not_steerable")
            routed = route_parent_steering(parent_thread_id=target,
                incoming_generation_id=str(payload.get("steering_id") or command["command_id"]),
                content=str(payload.get("text") or ""))
            if not routed:
                raise ClientPlatformError("generation_not_steerable")
            return {"conversation_id": target, "status": "accepted"}
        if kind == "conversation.delete":
            from row_bot.thread_cleanup import delete_thread
            admissions.close_admission(target)
            self.registry.stop(target, reason="delete")
            if self.registry.active(target):
                return {"conversation_id": target, "status": "DeleteBlocked"}
            result = delete_thread(target)
            return {"conversation_id": target, "status": "DeleteCompleted" if result.deleted else "DeleteBlocked"}
        raise ClientPlatformError("invalid_command")

    def _start(self, conversation_id: str, payload: dict, *, resume: bool, command_id: str = "",
               approval_context: dict | None = None) -> dict:
        if self.registry.active(conversation_id):
            raise ClientPlatformError("generation_active")
        selection = payload.get("model_selection") or {}
        model_ref = str(selection.get("model_ref") or "")
        provider_id = str(selection.get("provider_id") or "")
        if not model_ref or not provider_id:
            raise ClientPlatformError("model_selection_required")
        from row_bot.providers.selection import parse_model_ref, model_choice_value
        parsed = parse_model_ref(model_ref)
        if parsed and parsed[0] != provider_id:
            raise ClientPlatformError("model_selection_mismatch")
        model_ref = model_choice_value(model_ref, provider_id=provider_id)
        submission_id = str(payload.get("submission_id") or uuid.uuid4())
        generation_id = str(uuid.uuid4())
        text = str(payload.get("text") or "")
        attachment_refs = list(payload.get("attachment_refs") or ())
        if len(attachment_refs) > 32:
            raise ClientPlatformError("payload_too_large")
        if attachment_refs:
            from row_bot.application.attachments import inspect_attachment, UPLOAD_BATCH_BYTES
            total_size = 0
            for reference in attachment_refs:
                if not str(reference).startswith(conversation_id + ":"):
                    raise ClientPlatformError("action_denied")
                total_size += int(inspect_attachment(reference)["size_bytes"])
                if total_size > UPLOAD_BATCH_BYTES:
                    raise ClientPlatformError("payload_too_large")
        config = {"configurable": {"thread_id": conversation_id, "runtime_surface": "normal_chat",
                  "runtime_mode": "agent", "generation_id": generation_id,
                  "platform_submission_id": submission_id,
                  "platform_command_id": command_id,
                  "model_override": model_ref}}
        handle = self.admit_execution(conversation_id, config, text=None if resume else text)
        admitted = {"pass_id": handle.pass_id, "submission_id": submission_id, "generation_id": generation_id}

        def producer() -> None:
            status = "interrupted"
            try:
                self.registry.check_dispatch(handle)
                files = []
                if attachment_refs:
                    from row_bot.application.attachments import read_attachment
                    for reference in attachment_refs:
                        self.registry.check_dispatch(handle)
                        metadata, data = read_attachment(reference)
                        files.append({"name": metadata["name"], "data": data})
                from row_bot.agent import stream_agent, resume_stream_agent
                from row_bot.tools import registry as tool_registry
                enabled = [tool.name for tool in tool_registry.get_enabled_tools()]
                from row_bot.conversation_resources import execution_context
                from row_bot.application.attachment_context import prepared_attachments
                self.registry.check_dispatch(handle)
                with execution_context(conversation_id), prepared_attachments(conversation_id, files, model_ref=model_ref) as attachment_context:
                    self.registry.check_dispatch(handle)
                    prepared_text = text + ("\n\n" + attachment_context if attachment_context else "")
                    if attachment_context and not resume:
                        from row_bot.threads import replace_admitted_human_content
                        handle.input_checkpoint_revision = replace_admitted_human_content(
                            conversation_id, submission_id, prepared_text, expected_revision=handle.input_checkpoint_revision)
                    self.registry.check_dispatch(handle)
                    events = ((self.resume_factory or resume_stream_agent)(enabled, config,
                              bool((approval_context or {}).get("approved")),
                              interrupt_ids=(approval_context or {}).get("interrupt_ids"), stop_event=handle.cancel_scope.stop_event)
                              if resume else (self.stream_factory or stream_agent)(prepared_text, enabled, config, stop_event=handle.cancel_scope.stop_event))
                    for event in events:
                        self.registry.check_dispatch(handle)
                        self.observe_event(conversation_id, event, handle)
                        if event[0] == "done":
                            status = "completed"
                        elif event[0] == "interrupt":
                            status = "waiting_approval"
                        elif event[0] == "error":
                            status = "interrupted"
            except InterruptedError:
                status = "stopped"
            except Exception:
                self.projection.publish(conversation_id, "generation.error", {"code": "generation_failed"})
            finally:
                self.finish_execution(handle, status)
        def start_failed(_exc: BaseException) -> None:
            try:
                self.projection.publish(conversation_id, "generation.error", {"code": "generation_failed"})
            finally:
                self.finish_execution(handle, "interrupted")
        try:
            self.registry.launch(handle, producer, on_entry_failure=start_failed)
        except (RuntimeError, OSError) as exc:
            if handle.producer_done.is_set():
                raise ClientPlatformError("generation_failed") from exc
            raise
        return {"conversation_id": conversation_id, **admitted, "execution_id": handle.execution_id,
                "status": "accepted"}

    def observe_event(self, conversation_id: str, event: tuple, handle: Any) -> None:
        with _COMMAND_LOCK:
            self._observe_event_locked(conversation_id, event, handle)

    def _observe_event_locked(self, conversation_id: str, event: tuple, handle: Any) -> None:
        kind, payload = event
        if kind == "platform_segment":
            handle.segment_id = str(payload["segment_id"])
            handle.segment_committed = False
        elif kind == "token":
            if handle.segment_committed:
                handle.segment_id = admissions.start_segment(handle.pass_id)
                handle.segment_committed = False
            row_id = f"assistant:live:{handle.pass_id}:{handle.segment_id}"
            text = str(payload)
            from row_bot.application.live_content import append
            append(conversation_id, f"live:{handle.pass_id}:{handle.segment_id}", text)
            for offset in range(0, len(text), 8192):
                self.projection.publish(conversation_id, "transcript.delta", {
                    "pass_id": handle.pass_id, "segment_id": handle.segment_id, "row_id": row_id,
                    "render_revision": str(int(self.projection.snapshot(conversation_id)["projection_revision"]) + 1),
                    "public_text_delta": text[offset:offset + 8192]})
        elif kind == "output_binding":
            from row_bot.projection.canonical import exact_assistant_equal
            from row_bot import threads
            native_id = str(payload.get("native_message_id") or "")
            revision = str(payload.get("checkpoint_revision") or "")
            snapshot = self.projection.snapshot(conversation_id)
            target_segment = str(payload.get("segment_id") or handle.segment_id)
            row_id = f"assistant:live:{handle.pass_id}:{target_segment}"
            live = next((r for r in snapshot["rows"] if r["id"] == row_id), None)
            from row_bot.runtime.checkpoint_reader import open_checkpoint
            native_row = None
            native_text_only = False
            with open_checkpoint(conversation_id, revision) as reader:
                if reader:
                    matches = [record for _, record in reader.records() if record["message_id"] == native_id]
                    if len(matches) == 1:
                        native_row = reader.public_row(matches[0])
                        native_text_only = reader.text_only(matches[0])
            adopted = bool(native_row and native_row["role"] == "assistant" and live
                           and live.get("content_status") != "lazy" and native_row.get("content_status") != "lazy"
                           and native_text_only and not native_row["tool_call_ids"] and not native_row.get("tool_calls_ref")
                           and exact_assistant_equal(live["blocks"], native_row["blocks"]))
            if native_row and native_row["role"] == "assistant" and revision:
                if handle.segment_committed and not payload.get("segment_id"):
                    target_segment = handle.segment_id = admissions.start_segment(handle.pass_id)
                admissions.bind_output(handle.pass_id, target_segment, native_id, revision, snapshot["projection_revision"])
                if target_segment == handle.segment_id:
                    handle.segment_committed = True
                    handle.output_message_id = native_id
                    handle.output_checkpoint_revision = revision
            self._refresh_checkpoint(conversation_id)
            self.projection.settle_live(conversation_id, row_id, adopted=adopted)
            from row_bot.application.live_content import discard
            discard(conversation_id, f"live:{handle.pass_id}:{target_segment}")
        elif kind in {"tool_call", "tool_done"}:
            getter = getattr(payload, "get", lambda key, default="": default)
            self.projection.publish(conversation_id, "tool.activity", {
                "state": kind, "tool_call_id": str(getter("tool_call_id") or getter("id") or ""),
                "message_id": str(getter("message_id") or ""),
                "pass_id": handle.pass_id, "segment_id": handle.segment_id})
            if kind == "tool_done":
                for metadata in getter("media", []) or []:
                    self.projection.publish(conversation_id, metadata["type"], {
                        **metadata["payload"],
                        "tool_call_id": str(getter("tool_call_id") or ""),
                        "message_id": str(getter("message_id") or "")})
                if getter("media_error"):
                    self.projection.publish(conversation_id, "generation.error", {"code": "media_unavailable"})
                content = str(getter("content") or "")
                if content.startswith("__IMAGE__:"):
                    from row_bot.application.attachments import register_attachment
                    encoded = content[len("__IMAGE__:"):].split("\n\n", 1)[0]
                    if len(encoded) <= 36 * 1024 * 1024:
                        data = base64.b64decode(encoded, validate=True)
                        metadata = register_attachment(conversation_id, f"generated-{uuid.uuid4().hex}.png", data, "image/png")
                        self.projection.publish(conversation_id, "media.available", {
                            "media_ref": metadata["attachment_ref"], "mime_type": metadata["mime_type"],
                            "tool_call_id": str(getter("tool_call_id") or getter("id") or ""),
                            "message_id": str(getter("message_id") or "")})
        elif kind == "thinking":
            self.projection.publish(conversation_id, "generation.activity", {"state": "thinking"})
        elif kind == "interrupt":
            from row_bot.tasks import create_approval_request
            from row_bot.providers.selection import parse_model_ref
            from row_bot import threads
            parsed = parse_model_ref(handle.model_ref)
            items = payload if isinstance(payload, list) else [payload]
            interrupt_ids = [str(item["__interrupt_id"]) for item in items
                             if isinstance(item, dict) and item.get("__interrupt_id")]
            context = {"model_selection": {"provider_id": parsed[0] if parsed else "", "model_ref": handle.model_ref},
                       "interrupt_ids": interrupt_ids, "interrupt": payload,
                       "checkpoint_revision": threads.get_latest_checkpoint_revision(conversation_id), "pass_id": handle.pass_id}
            _, handle.approval_id = create_approval_request(
                handle.pass_id, "", "conversation", "Approval required", resume_kind="conversation",
                source_thread_id=conversation_id, parent_thread_id=conversation_id,
                approval_payload_json=context)
            handle.status = "waiting_approval"
            self.projection.publish(conversation_id, "approval.required", {
                "status": "waiting_approval", "approval_id": handle.approval_id})
        elif kind == "error":
            self.projection.publish(conversation_id, "generation.error", {"code": "generation_failed"})

    def get_approval(self, approval_id: str) -> dict:
        from row_bot.tasks import _get_conn
        with _get_conn() as conn:
            row = conn.execute("SELECT * FROM approval_requests WHERE id=?", (approval_id,)).fetchone()
        if row is None:
            raise ClientPlatformError("not_found")
        return {"id": row["id"], "status": row["status"], "revision": "0" if row["status"] == "pending" else "1",
                "expires_at": row["timeout_at"], "policy_revision": "1", "action_digest": admissions.keyed_digest(dict(row))}

    def claim_legacy_approval(self, approval_id: str, conversation_id: str, approved: bool) -> dict:
        """Consume the same durable approval before the retained renderer resumes."""
        from row_bot.tasks import _get_conn, claim_conversation_approval
        with _COMMAND_LOCK:
            with _get_conn() as conn:
                row = conn.execute("SELECT * FROM approval_requests WHERE id=?", (approval_id,)).fetchone()
            if not row or row["source_thread_id"] != conversation_id:
                raise ClientPlatformError("approval_already_resolved")
            if admissions.deletion_state(conversation_id) != "active":
                raise ClientPlatformError("conversation_deleting")
            if self.registry.active(conversation_id):
                raise ClientPlatformError("generation_active")
            claimed = claim_conversation_approval(approval_id, approved,
                expected_payload=row["approval_payload_json"], expected_timeout=row["timeout_at"])
            if not claimed:
                raise ClientPlatformError("approval_already_resolved")
            return json.loads(claimed["approval_payload_json"])

    def pending_approval_model_ref(self, approval_id: str, conversation_id: str) -> str:
        """Read the frozen selection for the retained client's readiness check."""
        from row_bot.tasks import _get_conn
        with _COMMAND_LOCK, _get_conn() as conn:
            row = conn.execute("SELECT approval_payload_json FROM approval_requests WHERE id=? AND source_thread_id=? "
                               "AND resume_kind='conversation' AND status='pending'", (approval_id, conversation_id)).fetchone()
            if not row:
                raise ClientPlatformError("approval_already_resolved")
            context = json.loads(row["approval_payload_json"])
            return str(context["model_selection"]["model_ref"])

    def _resolve_approval(self, approval_id: str, payload: dict) -> dict:
        with _COMMAND_LOCK:
            return self._resolve_approval_locked(approval_id, payload)

    def _resolve_approval_locked(self, approval_id: str, payload: dict) -> dict:
        from row_bot.tasks import _get_conn, respond_to_approval
        with _get_conn() as conn:
            row = conn.execute("SELECT * FROM approval_requests WHERE id=?", (approval_id,)).fetchone()
        if not row or row["status"] != "pending":
            raise ClientPlatformError("approval_already_resolved")
        if row["resume_kind"] == "conversation":
            conversation_id = str(row["source_thread_id"])
            context = self.claim_legacy_approval(approval_id, conversation_id, payload.get("decision") == "approve")
            result = self._start(conversation_id, {"model_selection": context["model_selection"]}, resume=True,
                                 approval_context={"approved": payload.get("decision") == "approve",
                                                   "interrupt_ids": context["interrupt_ids"]})
            return {**result, "approval_id": approval_id}
        if not respond_to_approval(row["resume_token"], payload.get("decision") == "approve"):
            raise ClientPlatformError("approval_already_resolved")
        return {"approval_id": approval_id, "status": "completed"}


client_platform_service = ClientPlatformService()
