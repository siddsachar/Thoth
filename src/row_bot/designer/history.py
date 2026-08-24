"""Designer — version history snapshots and in-memory undo/redo stack.

Snapshot storage: ~/.row-bot/designer/history/{project_id}/{timestamp}.json
Each snapshot stores the full pages list + brand config at that point in time.

UndoStack: in-memory ring buffer (per project session) for fast undo/redo.
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from row_bot.designer.state import DesignerProject, DesignerPage, BrandConfig
from row_bot.designer.storage import DATA_DIR

logger = logging.getLogger(__name__)

HISTORY_DIR = DATA_DIR / "designer" / "history"
MAX_SNAPSHOTS = 50
MAX_UNDO = 50


@dataclass
class ProjectState:
    """Full undoable designer document state."""

    name: str
    pages: list[dict]
    brand: Optional[dict]
    active_page: int
    aspect_ratio: str
    canvas_width: int
    canvas_height: int


def capture_project_state(project: DesignerProject) -> ProjectState:
    """Capture the current designer document state."""

    return ProjectState(
        name=project.name,
        pages=[p.to_dict() for p in project.pages],
        brand=project.brand.to_dict() if project.brand else None,
        active_page=project.active_page,
        aspect_ratio=project.aspect_ratio,
        canvas_width=project.canvas_width,
        canvas_height=project.canvas_height,
    )


def project_state_from_dict(
    data: dict,
    project: DesignerProject | None = None,
) -> ProjectState:
    """Build a project state from serialized history data."""

    return ProjectState(
        name=data.get("name", project.name if project else "Untitled Project"),
        pages=data.get("pages", []),
        brand=data.get("brand"),
        active_page=data.get("active_page", project.active_page if project else 0),
        aspect_ratio=data.get(
            "aspect_ratio",
            project.aspect_ratio if project else "16:9",
        ),
        canvas_width=data.get(
            "canvas_width",
            project.canvas_width if project else 1920,
        ),
        canvas_height=data.get(
            "canvas_height",
            project.canvas_height if project else 1080,
        ),
    )


def apply_project_state(project: DesignerProject, state: ProjectState) -> None:
    """Apply a captured designer document state back onto a project."""

    project.name = state.name
    project.pages = [DesignerPage.from_dict(p) for p in state.pages]
    if not project.pages:
        project.pages = [DesignerPage()]
    project.active_page = min(state.active_page, len(project.pages) - 1)
    project.aspect_ratio = state.aspect_ratio
    project.canvas_width = state.canvas_width
    project.canvas_height = state.canvas_height
    if state.brand:
        project.brand = BrandConfig.from_dict(state.brand)
    else:
        project.brand = None


# ═══════════════════════════════════════════════════════════════════════
# PERSISTENT SNAPSHOTS  (disk-based version history)
# ═══════════════════════════════════════════════════════════════════════

def _project_history_dir(project_id: str) -> Path:
    d = HISTORY_DIR / project_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def snapshot(project: DesignerProject, label: str = "", *,
              author: str = "user") -> str:
    """Save a snapshot of the current project state. Returns the snapshot ID.

    ``author`` is ``"user"`` for UI-initiated edits and ``"agent"`` for
    snapshots taken immediately before an agent tool call mutates the
    project. Phase 2.2.L uses this to surface a diff of the last agent
    change in the editor.
    """
    ts = f"{time.time():.6f}"
    state = capture_project_state(project)
    snap = {
        "id": ts,
        "label": label,
        "author": (author or "user").strip().lower() or "user",
        "timestamp": ts,
        **asdict(state),
    }
    d = _project_history_dir(project.id)
    path = d / f"{ts}.json"
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False)
        logger.debug("Saved snapshot %s for project %s", ts, project.id)
    except Exception:
        logger.exception("Failed to save snapshot for %s", project.id)
        return ""
    # Auto-prune old snapshots
    _prune_snapshots(project.id)
    return ts


def _prune_snapshots(project_id: str) -> None:
    """Keep only the newest MAX_SNAPSHOTS snapshots."""
    d = _project_history_dir(project_id)
    files = sorted(d.glob("*.json"), key=lambda p: p.stem)
    if len(files) > MAX_SNAPSHOTS:
        for old in files[: len(files) - MAX_SNAPSHOTS]:
            try:
                old.unlink()
            except OSError:
                pass


def list_snapshots(project_id: str) -> list[dict]:
    """Return snapshot summaries (newest first).

    Each dict has: id, label, timestamp, name, page_count.
    """
    d = _project_history_dir(project_id)
    results = []
    for path in sorted(d.glob("*.json"), reverse=True):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            results.append({
                "id": data.get("id", path.stem),
                "label": data.get("label", ""),
                "author": (data.get("author") or "user").strip().lower() or "user",
                "timestamp": float(data.get("timestamp", path.stem)),
                "name": data.get("name", ""),
                "page_count": len(data.get("pages", [])),
            })
        except Exception:
            logger.warning("Skipping corrupt snapshot: %s", path.name)
    return results


def read_snapshot(project_id: str, snapshot_id: str) -> Optional[dict]:
    """Load the raw JSON of a single snapshot, or ``None`` if missing."""
    d = _project_history_dir(project_id)
    path = d / f"{snapshot_id}.json"
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        logger.exception("Failed to read snapshot %s", snapshot_id)
        return None


def restore_snapshot(project: DesignerProject, snapshot_id: str) -> bool:
    """Restore a project from a snapshot. Returns True on success."""
    d = _project_history_dir(project.id)
    path = d / f"{snapshot_id}.json"
    if not path.exists():
        logger.warning("Snapshot %s not found for project %s", snapshot_id, project.id)
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        logger.exception("Failed to read snapshot %s", snapshot_id)
        return False

    apply_project_state(project, project_state_from_dict(data, project))
    logger.info("Restored project %s from snapshot %s", project.id, snapshot_id)
    return True


def delete_history(project_id: str) -> None:
    """Delete all snapshots for a project."""
    from row_bot.thread_cleanup import resolve_managed_path

    d = resolve_managed_path(HISTORY_DIR, str(project_id or ""))
    if d.exists():
        import shutil
        shutil.rmtree(d)


# ═══════════════════════════════════════════════════════════════════════
# IN-MEMORY UNDO / REDO STACK  (per editor session)
# ═══════════════════════════════════════════════════════════════════════

class UndoStack:
    """Per-project in-memory undo/redo stack."""

    def __init__(self, maxlen: int = MAX_UNDO):
        self._undo: deque[ProjectState] = deque(maxlen=maxlen)
        self._redo: deque[ProjectState] = deque(maxlen=maxlen)

    def _capture(self, project: DesignerProject) -> ProjectState:
        return capture_project_state(project)

    def _apply(self, project: DesignerProject, snap: ProjectState) -> None:
        apply_project_state(project, snap)

    def push(self, project: DesignerProject) -> None:
        """Capture current state before a mutation. Clears redo stack."""
        self._undo.append(self._capture(project))
        self._redo.clear()

    def undo(self, project: DesignerProject) -> bool:
        """Undo last change. Returns True if successful."""
        if not self._undo:
            return False
        self._redo.append(self._capture(project))
        snap = self._undo.pop()
        self._apply(project, snap)
        return True

    def redo(self, project: DesignerProject) -> bool:
        """Redo last undone change. Returns True if successful."""
        if not self._redo:
            return False
        self._undo.append(self._capture(project))
        snap = self._redo.pop()
        self._apply(project, snap)
        return True

    @property
    def can_undo(self) -> bool:
        return len(self._undo) > 0

    @property
    def can_redo(self) -> bool:
        return len(self._redo) > 0

    def clear(self) -> None:
        self._undo.clear()
        self._redo.clear()
