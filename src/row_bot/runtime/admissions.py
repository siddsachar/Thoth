"""Content-free client admissions and command receipts in the existing tasks DB."""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import uuid
from collections.abc import Iterator

_LOCK = threading.RLock()


class AdmissionError(ValueError):
    def __init__(self, code: str, current_revision: str | None = None) -> None:
        super().__init__(code)
        self.current_revision = current_revision


@contextlib.contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    from row_bot.tasks import _get_conn

    with _LOCK:
        conn = _get_conn()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS client_instance (
                    id INTEGER PRIMARY KEY CHECK(id=1), instance_id TEXT NOT NULL, secret TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS client_commands (
                    owner_id TEXT NOT NULL, key TEXT NOT NULL, command_id TEXT NOT NULL,
                    target TEXT NOT NULL, type TEXT NOT NULL, verifier TEXT NOT NULL,
                    status TEXT NOT NULL, result_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY(owner_id,key), UNIQUE(owner_id,command_id));
                CREATE TABLE IF NOT EXISTS conversation_lifecycle (
                    conversation_id TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT 'active',
                    next_sequence INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS generation_passes (
                    pass_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                    submission_id TEXT NOT NULL UNIQUE, generation_id TEXT NOT NULL,
                    admission_sequence INTEGER NOT NULL, state TEXT NOT NULL,
                    checkpoint_revision TEXT NOT NULL DEFAULT '', execution_id TEXT NOT NULL DEFAULT '',
                    lease_epoch TEXT NOT NULL DEFAULT '', terminal_status TEXT NOT NULL DEFAULT '',
                    UNIQUE(conversation_id,admission_sequence));
                CREATE TABLE IF NOT EXISTS generation_segments (
                    segment_id TEXT PRIMARY KEY, pass_id TEXT NOT NULL, invocation_id TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL, native_message_id TEXT NOT NULL DEFAULT '',
                    checkpoint_revision TEXT NOT NULL DEFAULT '', live_revision TEXT NOT NULL DEFAULT '',
                    canonical_version TEXT NOT NULL DEFAULT '1');
                CREATE TABLE IF NOT EXISTS conversation_deletion_receipts (
                    conversation_id TEXT PRIMARY KEY, outcome TEXT NOT NULL);
            """)
            conn.execute("INSERT OR IGNORE INTO client_instance VALUES(1,?,?)",
                         (str(uuid.uuid4()), secrets.token_hex(32)))
            if "command_id" not in {row[1] for row in conn.execute("PRAGMA table_info(generation_passes)")}:
                conn.execute("ALTER TABLE generation_passes ADD COLUMN command_id TEXT NOT NULL DEFAULT ''")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(generation_passes)")}
            for name, definition in (("owner_pid", "INTEGER NOT NULL DEFAULT 0"), ("owner_birth", "REAL NOT NULL DEFAULT 0")):
                if name not in columns:
                    conn.execute(f"ALTER TABLE generation_passes ADD COLUMN {name} {definition}")
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()


def instance_identity() -> str:
    with transaction() as conn:
        return str(conn.execute("SELECT instance_id FROM client_instance WHERE id=1").fetchone()[0])


def claim_command(owner_id: str, key: str, command: dict, target: str) -> dict | None:
    with transaction() as conn:
        secret = conn.execute("SELECT secret FROM client_instance WHERE id=1").fetchone()[0]
        semantic = {key: value for key, value in command.items() if key != "client_session_id"}
        canonical = json.dumps({"target": target, "command": semantic}, sort_keys=True,
                               ensure_ascii=False, separators=(",", ":")).encode()
        verifier = hmac.new(bytes.fromhex(secret), canonical, hashlib.sha256).hexdigest()
        existing = conn.execute("SELECT * FROM client_commands WHERE owner_id=? AND key=?",
                                (owner_id, key)).fetchone()
        if existing:
            if not hmac.compare_digest(str(existing["verifier"]), verifier):
                raise AdmissionError("idempotency_mismatch")
            if existing["status"] == "completed":
                return json.loads(existing["result_json"])
            if existing["status"] == "rejected":
                failure = json.loads(existing["result_json"])
                raise AdmissionError(failure["code"], failure.get("current_revision"))
            raise AdmissionError("operation_uncertain")
        try:
            conn.execute("INSERT INTO client_commands(owner_id,key,command_id,target,type,verifier,status) "
                         "VALUES(?,?,?,?,?,?,'admitting')",
                         (owner_id, key, command["command_id"], target, command["type"], verifier))
        except sqlite3.IntegrityError as exc:
            raise AdmissionError("idempotency_mismatch") from exc
    return None


def complete_command(owner_id: str, key: str, result: dict) -> dict:
    with transaction() as conn:
        conn.execute("UPDATE client_commands SET status='completed',result_json=? WHERE owner_id=? AND key=?",
                     (json.dumps(result, separators=(",", ":")), owner_id, key))
    return result


def reject_command(owner_id: str, key: str, code: str, current_revision: str | None = None) -> None:
    with transaction() as conn:
        conn.execute("UPDATE client_commands SET status='rejected',result_json=? WHERE owner_id=? AND key=? AND status='admitting'",
                     (json.dumps({"code": code, "current_revision": current_revision}), owner_id, key))


def receipt(owner_id: str, command_id: str) -> dict | None:
    with transaction() as conn:
        row = conn.execute("SELECT status,result_json FROM client_commands WHERE owner_id=? AND command_id=?",
                           (owner_id, command_id)).fetchone()
        return ({"status": row["status"], **json.loads(row["result_json"])} if row else None)


def reserve(conversation_id: str, submission_id: str, generation_id: str, *, command_id: str = "") -> dict:
    with transaction() as conn:
        if conn.execute("SELECT 1 FROM conversation_deletion_receipts WHERE conversation_id=?", (conversation_id,)).fetchone():
            raise AdmissionError("conversation_deleting")
        conn.execute("INSERT OR IGNORE INTO conversation_lifecycle(conversation_id) VALUES(?)", (conversation_id,))
        lifecycle = conn.execute("SELECT * FROM conversation_lifecycle WHERE conversation_id=?", (conversation_id,)).fetchone()
        if lifecycle["state"] != "active":
            raise AdmissionError("conversation_deleting")
        pending = conn.execute("SELECT 1 FROM generation_passes WHERE conversation_id=? AND state IN ('admitting','admitted','started')",
                               (conversation_id,)).fetchone()
        if pending:
            raise AdmissionError("generation_active")
        sequence = int(lifecycle["next_sequence"]) + 1
        pass_id = str(uuid.uuid4())
        conn.execute("UPDATE conversation_lifecycle SET next_sequence=? WHERE conversation_id=?", (sequence, conversation_id))
        conn.execute("INSERT INTO generation_passes(pass_id,conversation_id,submission_id,generation_id,admission_sequence,state) VALUES(?,?,?,?,?,'admitting')",
                     (pass_id, conversation_id, submission_id, generation_id, sequence))
        conn.execute("UPDATE generation_passes SET command_id=? WHERE pass_id=?", (command_id, pass_id))
        return {"pass_id": pass_id, "submission_id": submission_id, "generation_id": generation_id,
                "admission_sequence": str(sequence)}


def admit(pass_id: str, checkpoint_revision: str) -> None:
    with transaction() as conn:
        row = conn.execute("SELECT p.*,l.state AS lifecycle FROM generation_passes p JOIN conversation_lifecycle l USING(conversation_id) WHERE pass_id=?", (pass_id,)).fetchone()
        if not row or row["lifecycle"] != "active":
            raise AdmissionError("conversation_deleting")
        if conn.execute("UPDATE generation_passes SET state='admitted',checkpoint_revision=? WHERE pass_id=? AND state='admitting'", (checkpoint_revision, pass_id)).rowcount != 1:
            raise AdmissionError("admission_conflict")


def start(pass_id: str, execution_id: str, epoch: str) -> None:
    import os
    import psutil
    with transaction() as conn:
        if conn.execute("UPDATE generation_passes SET state='started',execution_id=?,lease_epoch=?,owner_pid=?,owner_birth=? WHERE pass_id=? AND state='admitted' AND EXISTS(SELECT 1 FROM conversation_lifecycle l WHERE l.conversation_id=generation_passes.conversation_id AND l.state='active')", (execution_id, epoch, os.getpid(), psutil.Process().create_time(), pass_id)).rowcount != 1:
            raise AdmissionError("admission_conflict")


def finish(pass_id: str, status: str, *, boundary: dict | None = None) -> str:
    with transaction() as conn:
        if status == "completed":
            proof = boundary or {}
            matched = conn.execute("SELECT 1 FROM generation_passes p JOIN generation_segments s ON s.pass_id=p.pass_id "
                "WHERE p.pass_id=? AND p.state='started' AND p.execution_id=? AND p.lease_epoch=? "
                "AND s.segment_id=? AND s.state='committed' AND s.native_message_id=? AND s.checkpoint_revision=?",
                (pass_id, proof.get("execution_id", ""), proof.get("server_epoch", ""), proof.get("segment_id", ""),
                 proof.get("message_id", ""), proof.get("checkpoint_revision", ""))).fetchone()
            if not matched:
                status = "interrupted"
        conn.execute("UPDATE generation_passes SET state=?,terminal_status=? WHERE pass_id=? AND state='started'",
                     ("terminal" if status in {"completed", "stopped", "waiting_approval"} else "interrupted", status, pass_id))
        conn.execute("UPDATE generation_segments SET state='interrupted' WHERE pass_id=? AND state='started'", (pass_id,))
    return status


def recover(epoch: str) -> None:
    """Record process loss without calling providers or replaying accepted work."""
    from row_bot import threads
    with transaction() as conn:
        pending = [dict(row) for row in conn.execute(
            "SELECT * FROM generation_passes WHERE state IN ('admitting','admitted') OR (state='started' AND lease_epoch!=?)", (epoch,))]
    for row in pending:
        if row["state"] == "started" and _owner_alive(row):
            continue
        saved = threads.checkpointer.get_tuple({"configurable": {"thread_id": row["conversation_id"], "checkpoint_ns": ""}})
        messages = saved.checkpoint.get("channel_values", {}).get("messages", []) if saved else []
        matching_input = any(str(getattr(message, "id", "")) == row["submission_id"] for message in messages)
        state = "interrupted" if matching_input or row["state"] != "admitting" else "cancelled"
        revision = str(saved.config["configurable"]["checkpoint_id"]) if saved else ""
        with transaction() as conn:
            changed = conn.execute("UPDATE generation_passes SET state=?,terminal_status=?,checkpoint_revision=? WHERE pass_id=? AND state=? AND lease_epoch=?",
                (state, state, revision, row["pass_id"], row["state"], row["lease_epoch"])).rowcount
            if changed and row["command_id"]:
                result = {"command_id": row["command_id"], "conversation_id": row["conversation_id"],
                          "submission_id": row["submission_id"], "generation_id": row["generation_id"],
                          "pass_id": row["pass_id"], "status": "accepted" if matching_input else "rejected"}
                conn.execute("UPDATE client_commands SET status='completed',result_json=? WHERE command_id=? AND target=? AND status='admitting'",
                             (json.dumps(result, separators=(",", ":")), row["command_id"], row["conversation_id"]))


def _owner_alive(row: dict) -> bool:
    """PID reuse does not prove ownership; inaccessible owners stay unproven."""
    import psutil
    if not row.get("owner_pid") or not row.get("owner_birth"):
        return True
    try:
        return abs(psutil.Process(int(row["owner_pid"])).create_time() - float(row["owner_birth"])) < 0.001
    except psutil.NoSuchProcess:
        return False
    except psutil.Error:
        return True


def keyed_digest(value: dict) -> str:
    """Opaque equality proof over private authoritative action state."""
    with transaction() as conn:
        secret = bytes.fromhex(conn.execute("SELECT secret FROM client_instance WHERE id=1").fetchone()[0])
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hmac.new(secret, canonical, hashlib.sha256).hexdigest()


def queued_submission_ids(conversation_id: str) -> list[str]:
    with transaction() as conn:
        return [str(row[0]) for row in conn.execute("SELECT submission_id FROM generation_passes WHERE conversation_id=? "
                "AND state IN ('admitting','admitted','started') ORDER BY admission_sequence LIMIT 256", (conversation_id,))]


def start_segment(pass_id: str) -> str:
    segment_id = str(uuid.uuid4())
    with transaction() as conn:
        conn.execute("INSERT INTO generation_segments(segment_id,pass_id,invocation_id,state) VALUES(?,?,?,'started')",
                     (segment_id, pass_id, str(uuid.uuid4())))
    return segment_id


def bind_output(pass_id: str, segment_id: str, message_id: str, revision: str, live_revision: str) -> None:
    with transaction() as conn:
        changed = conn.execute("UPDATE generation_segments SET state='committed',native_message_id=?,checkpoint_revision=?,live_revision=? WHERE segment_id=? AND pass_id=? AND state='started'",
                               (message_id, revision, live_revision, segment_id, pass_id)).rowcount
        if not changed:
            raise AdmissionError("output_binding_conflict")


def deletion_state(conversation_id: str) -> str:
    with transaction() as conn:
        if conn.execute("SELECT 1 FROM conversation_deletion_receipts WHERE conversation_id=?", (conversation_id,)).fetchone():
            return "physical_delete_ready"
        row = conn.execute("SELECT state FROM conversation_lifecycle WHERE conversation_id=?", (conversation_id,)).fetchone()
        return str(row[0]) if row else "active"


def close_admission(conversation_id: str) -> None:
    with transaction() as conn:
        conn.execute("INSERT OR IGNORE INTO conversation_lifecycle(conversation_id) VALUES(?)", (conversation_id,))
        conn.execute("UPDATE conversation_lifecycle SET state='admission_closed' WHERE conversation_id=? AND state='active'", (conversation_id,))
        conn.execute("UPDATE generation_passes SET state='cancelled' WHERE conversation_id=? AND state IN ('admitting','admitted')", (conversation_id,))


def advance_deletion(conversation_id: str, phase: str) -> None:
    phases = ("active", "admission_closed", "stop_requested", "producer_released", "children_cleaned", "resources_cleaned", "physical_delete_ready")
    if phase not in phases:
        raise AdmissionError("invalid_deletion_phase")
    with transaction() as conn:
        row = conn.execute("SELECT state FROM conversation_lifecycle WHERE conversation_id=?", (conversation_id,)).fetchone()
        if row and phases.index(str(row[0])) < phases.index(phase):
            conn.execute("UPDATE conversation_lifecycle SET state=? WHERE conversation_id=?", (phase, conversation_id))


def unproven_producer(conversation_id: str) -> bool:
    with transaction() as conn:
        return conn.execute("SELECT 1 FROM generation_passes WHERE conversation_id=? AND state='started'", (conversation_id,)).fetchone() is not None


def deletion_completed(conversation_id: str) -> None:
    with transaction() as conn:
        conn.execute("INSERT OR IGNORE INTO conversation_deletion_receipts VALUES(?,'DeleteCompleted')", (conversation_id,))
        conn.execute("DELETE FROM conversation_lifecycle WHERE conversation_id=?", (conversation_id,))


def deletion_receipt(conversation_id: str) -> str | None:
    with transaction() as conn:
        row = conn.execute("SELECT outcome FROM conversation_deletion_receipts WHERE conversation_id=?", (conversation_id,)).fetchone()
        return str(row[0]) if row else None


def reopen_completed_conversation(conversation_id: str) -> None:
    """Permit an explicit channel recreation only after completed deletion."""
    with transaction() as conn:
        pending = conn.execute("SELECT 1 FROM generation_passes WHERE conversation_id=? AND state IN ('admitting','admitted','started')", (conversation_id,)).fetchone()
        lifecycle = conn.execute("SELECT state FROM conversation_lifecycle WHERE conversation_id=?", (conversation_id,)).fetchone()
        completed = conn.execute("SELECT 1 FROM conversation_deletion_receipts WHERE conversation_id=?", (conversation_id,)).fetchone()
        if (pending and completed) or (lifecycle and lifecycle[0] != "active" and not completed):
            raise AdmissionError("conversation_deleting")
        if completed:
            sequence = conn.execute("SELECT COALESCE(MAX(admission_sequence),0) FROM generation_passes WHERE conversation_id=?", (conversation_id,)).fetchone()[0]
            conn.execute("DELETE FROM conversation_deletion_receipts WHERE conversation_id=?", (conversation_id,))
            conn.execute("INSERT INTO conversation_lifecycle(conversation_id,state,next_sequence) VALUES(?,'active',?) ON CONFLICT(conversation_id) DO UPDATE SET state='active',next_sequence=excluded.next_sequence", (conversation_id, sequence))
