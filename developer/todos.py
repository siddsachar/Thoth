from __future__ import annotations

import json
import logging
import os
import pathlib
import tempfile
import uuid

from developer.state import DeveloperTodo, TodoStatus
from developer.storage import DEVELOPER_DIR

logger = logging.getLogger(__name__)

TODOS_DIR = DEVELOPER_DIR / "todos"


def _ensure_dirs() -> None:
    TODOS_DIR.mkdir(parents=True, exist_ok=True)


def _todo_path(thread_id: str) -> pathlib.Path:
    safe = "".join(ch for ch in str(thread_id) if ch.isalnum() or ch in {"_", "-"})
    return TODOS_DIR / f"{safe or 'unknown'}.json"


def _write_json_atomic(path: pathlib.Path, payload: dict) -> None:
    _ensure_dirs()
    fd: int | None = None
    tmp_path: pathlib.Path | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
        tmp_path = pathlib.Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = None
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        tmp_path.replace(path)
    finally:
        if fd is not None:
            os.close(fd)
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                logger.debug("Failed to clean Developer todo temp file %s", tmp_path, exc_info=True)


def list_todos(thread_id: str | None) -> list[DeveloperTodo]:
    if not thread_id:
        return []
    path = _todo_path(thread_id)
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Failed to load Developer todos for %s", thread_id)
        return []
    rows = payload.get("todos", []) if isinstance(payload, dict) else []
    todos: list[DeveloperTodo] = []
    for raw in rows:
        if isinstance(raw, dict):
            try:
                todos.append(DeveloperTodo.from_dict(raw))
            except Exception:
                logger.debug("Skipping invalid Developer todo: %r", raw, exc_info=True)
    return todos


def save_todos(thread_id: str, todos: list[DeveloperTodo]) -> None:
    if not thread_id:
        raise ValueError("thread_id is required")
    _write_json_atomic(_todo_path(thread_id), {"todos": [todo.to_dict() for todo in todos]})


def replace_todos_from_labels(thread_id: str, labels: list[str]) -> list[DeveloperTodo]:
    todos = [
        DeveloperTodo(id=uuid.uuid4().hex[:10], label=label.strip())
        for label in labels
        if label and label.strip()
    ]
    save_todos(thread_id, todos)
    return todos


def set_todo_status(thread_id: str, todo_id: str, status: TodoStatus) -> list[DeveloperTodo]:
    if status not in {"pending", "in_progress", "completed", "blocked"}:
        raise ValueError(f"Unknown todo status: {status}")
    todos = list_todos(thread_id)
    for todo in todos:
        if todo.id == todo_id:
            todo.status = status
            break
    save_todos(thread_id, todos)
    return todos

