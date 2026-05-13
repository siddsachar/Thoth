from __future__ import annotations

import hashlib
import json
import os
import pathlib
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass

from developer.storage import DEVELOPER_DIR


LEDGER_PATH = DEVELOPER_DIR / "change_ledger.json"


@dataclass(frozen=True)
class FileChange:
    path: str
    action: str
    before_hash: str
    after_hash: str
    before_text: str | None = None
    patch: str = ""


@dataclass(frozen=True)
class ChangeSet:
    id: str
    workspace_id: str
    thread_id: str
    created_at: float
    summary: str
    files: list[FileChange]
    reverted: bool = False
    reviewed: bool = False


def _sha(text: str | None) -> str:
    if text is None:
        return ""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def text_hash(text: str | None) -> str:
    return _sha(text)


def _load() -> dict:
    if not LEDGER_PATH.exists():
        return {"change_sets": []}
    try:
        data = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"change_sets": []}
    if not isinstance(data, dict):
        return {"change_sets": []}
    data.setdefault("change_sets", [])
    return data


def _save(data: dict) -> None:
    DEVELOPER_DIR.mkdir(parents=True, exist_ok=True)
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=LEDGER_PATH.name, suffix=".tmp", dir=str(LEDGER_PATH.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp_name, LEDGER_PATH)
    finally:
        try:
            pathlib.Path(tmp_name).unlink(missing_ok=True)
        except Exception:
            pass


def _from_dict(raw: dict) -> ChangeSet:
    files = [FileChange(**item) for item in raw.get("files", []) if isinstance(item, dict)]
    return ChangeSet(
        id=str(raw.get("id", "")),
        workspace_id=str(raw.get("workspace_id", "")),
        thread_id=str(raw.get("thread_id", "")),
        created_at=float(raw.get("created_at", 0) or 0),
        summary=str(raw.get("summary", "")),
        files=files,
        reverted=bool(raw.get("reverted", False)),
        reviewed=bool(raw.get("reviewed", False)),
    )


def record_change_set(
    *,
    workspace_id: str,
    thread_id: str,
    summary: str,
    files: list[FileChange],
) -> ChangeSet:
    change_set = ChangeSet(
        id=uuid.uuid4().hex[:12],
        workspace_id=workspace_id,
        thread_id=thread_id,
        created_at=time.time(),
        summary=summary,
        files=files,
    )
    data = _load()
    rows = data.get("change_sets", [])
    rows.append(asdict(change_set))
    data["change_sets"] = rows[-500:]
    _save(data)
    return change_set


def list_change_sets(
    *,
    workspace_id: str | None = None,
    thread_id: str | None = None,
    include_reverted: bool = False,
) -> list[ChangeSet]:
    rows: list[ChangeSet] = []
    for raw in _load().get("change_sets", []):
        if not isinstance(raw, dict):
            continue
        change_set = _from_dict(raw)
        if workspace_id and change_set.workspace_id != workspace_id:
            continue
        if thread_id and change_set.thread_id != thread_id:
            continue
        if change_set.reverted and not include_reverted:
            continue
        rows.append(change_set)
    rows.sort(key=lambda item: item.created_at, reverse=True)
    return rows


def mark_reverted(change_set_id: str) -> None:
    data = _load()
    for raw in data.get("change_sets", []):
        if isinstance(raw, dict) and raw.get("id") == change_set_id:
            raw["reverted"] = True
    _save(data)
