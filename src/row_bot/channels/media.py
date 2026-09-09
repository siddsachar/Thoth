"""
Row-Bot – Shared Media Pipeline
================================
Common helpers for processing inbound media from any channel:

* **Voice notes** → transcribe via faster-whisper
* **Photos / images** → analyse via Vision service
* **Documents / files** → save to inbox directory

Every channel's inbound handler calls these instead of re-implementing
the same logic.
"""

from __future__ import annotations

import logging
import os
import pathlib
import re
import tempfile
import time

from row_bot.data_paths import get_row_bot_data_dir

log = logging.getLogger("row_bot.channels.media")

_DATA_DIR = get_row_bot_data_dir()
_INBOX_DIR = _DATA_DIR / "inbox"


def _safe_filename(filename: str) -> str:
    """Return a basename safe to place under Row-Bot-managed folders."""
    name = pathlib.Path(str(filename or "attachment")).name.strip()
    if not name:
        name = "attachment"
    name = re.sub(r"[\x00-\x1f<>:\"/\\|?*]+", "_", name)
    name = name.strip(" .")
    return name or "attachment"


# ── Voice → Text ─────────────────────────────────────────────────────

def transcribe_audio(data: bytes, file_ext: str = ".ogg") -> str:
    """Transcribe audio bytes to text via faster-whisper.

    Parameters
    ----------
    data : bytes
        Raw audio file contents (OGG/Opus, WebM, MP3, WAV, …).
    file_ext : str
        File extension hint so faster-whisper picks the right decoder.

    Returns
    -------
    str
        Transcribed text (empty string on failure).
    """
    from row_bot.voice import get_voice_service

    svc = get_voice_service()
    svc._ensure_whisper()

    # Write to temp file — faster-whisper.transcribe() accepts paths
    # and internally uses ffmpeg to decode any format.
    with tempfile.NamedTemporaryFile(suffix=file_ext, delete=False) as tmp:
        tmp.write(data)
        tmp_path = tmp.name

    try:
        segs, _ = svc._whisper_model.transcribe(
            tmp_path, beam_size=5, language="en", vad_filter=True,
        )
        text = " ".join(s.text.strip() for s in segs).strip()
        log.info("Transcribed audio (%d bytes) → %d chars", len(data), len(text))
        return text
    except Exception as exc:
        log.error("Audio transcription failed: %s", exc)
        return ""
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ── Photo → Analysis ─────────────────────────────────────────────────

def analyze_image(data: bytes,
                  question: str = "Describe this image in detail.") -> str:
    """Analyse image bytes via the Vision service.

    Returns the model's text description (empty on failure).
    """
    try:
        from row_bot.vision import VisionService
        svc = VisionService()
        result = svc.analyze(data, question)
        log.info("Image analysis (%d bytes) → %d chars", len(data), len(result))
        return result
    except Exception as exc:
        log.error("Image analysis failed: %s", exc)
        return ""


# ── Document / File → Save & Extract ─────────────────────────────────

def save_inbound_file(data: bytes, filename: str) -> pathlib.Path:
    """Persist an inbound file to ``~/.row-bot/inbox/``.

    Returns the absolute ``Path`` to the saved file.
    """
    from uuid import uuid4
    from row_bot.application.attachments import _managed_root, _safe_path, _write

    inbox = _managed_root(_INBOX_DIR)
    inbox.mkdir(parents=True, exist_ok=True)
    _safe_path(inbox, inbox)
    # Unique ownership begins at exclusive creation, including same-name files
    # received by different conversations during the same clock tick.
    while True:
        dest = inbox / f"{int(time.time())}_{uuid4().hex}_{_safe_filename(filename)}"
        try:
            _write(inbox, dest, data)
            break
        except FileExistsError:
            continue
    log.info("Saved inbound file: %s (%d bytes)", dest, len(data))
    return dest


# File-type sets (mirrors ui/constants.py — kept here to avoid UI dependency)
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
_DATA_EXTENSIONS = {".csv", ".tsv", ".xlsx", ".xls", ".json", ".jsonl"}
_TEXT_EXTENSIONS = {
    ".txt", ".md", ".py", ".js", ".ts", ".html", ".css", ".xml", ".yaml",
    ".yml", ".toml", ".ini", ".cfg", ".log", ".sh", ".bat", ".ps1", ".sql",
    ".r", ".java", ".c", ".cpp", ".h", ".cs", ".go", ".rs", ".rb", ".php",
    ".swift", ".kt", ".lua", ".pl",
}


def extract_document_text(data: bytes, filename: str,
                          max_chars: int = 80_000) -> str:
    """Extract readable text from a file's raw bytes.

    Supports PDF, plain-text / code, and tabular data files.
    Returns the extracted text, or an empty string if the file type is
    unsupported or extraction fails.
    """
    import io

    suffix = pathlib.Path(filename).suffix.lower()

    # ── PDF ───────────────────────────────────────────────────────────
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            pages: list[str] = []
            for i, page in enumerate(reader.pages):
                text = page.extract_text() or ""
                if text.strip():
                    pages.append(f"--- Page {i + 1} ---\n{text}")
                if sum(len(p) for p in pages) > max_chars:
                    pages.append(
                        f"[Truncated — {len(reader.pages)} pages total, "
                        f"showing first {i + 1}]"
                    )
                    break
            content = "\n".join(pages) if pages else ""
            if content:
                log.info("Extracted PDF text: %s (%d chars from %d pages)",
                         filename, len(content), len(reader.pages))
            return content
        except Exception as exc:
            log.error("PDF text extraction failed for %s: %s", filename, exc)
            return ""

    # ── Tabular data (CSV, Excel, JSON) ──────────────────────────────
    if suffix in _DATA_EXTENSIONS:
        try:
            from row_bot.data_reader import read_data_file
            buf = io.BytesIO(data)
            summary = read_data_file(buf, name=filename, max_chars=max_chars)
            log.info("Extracted data file: %s (%d chars)", filename, len(summary))
            return summary
        except Exception as exc:
            log.error("Data file extraction failed for %s: %s", filename, exc)
            return ""

    # ── Plain text / code ────────────────────────────────────────────
    if suffix in _TEXT_EXTENSIONS:
        try:
            text = data.decode("utf-8", errors="replace")
            if len(text) > max_chars:
                text = text[:max_chars] + f"\n[Truncated — {len(data):,} bytes total]"
            log.info("Read text file: %s (%d chars)", filename, len(text))
            return text
        except Exception as exc:
            log.error("Text file read failed for %s: %s", filename, exc)
            return ""

    return ""


# ── Copy to workspace ────────────────────────────────────────────────

_RECEIVED_FOLDER = "Received Files"


def copy_to_workspace(saved_path: pathlib.Path, workspace_filename: str | None = None) -> str | None:
    """Copy an inbox file into the filesystem-tool workspace so the agent
    can re-read it without escaping the sandbox.

    Returns the *workspace-relative* path (e.g. ``Received Files/doc.pdf``)
    on success, or ``None`` if the workspace is not configured or the copy
    fails.
    """
    import stat

    from row_bot.application.attachments import AttachmentError, _managed_root, _open, _safe_path

    from row_bot.conversation_resources import current_execution_context, ResourceError
    context = current_execution_context()
    binding = context.resolve("workspace") if context is not None else None
    if binding is not None:
        from row_bot.developer.storage import get_workspace
        workspace = get_workspace(binding.resource_id)
        if workspace is None:
            raise ResourceError("resource_unavailable")
        root = workspace.path
    else:
        try:
            from row_bot.tools.registry import get_tool_config
            root = get_tool_config("filesystem", "workspace_root", "")
            if not root:
                root = str(pathlib.Path.home() / "Documents" / "Row-Bot")
        except Exception:
            root = str(pathlib.Path.home() / "Documents" / "Row-Bot")

    descriptor = -1
    dest: pathlib.Path | None = None
    try:
        workspace_root = _managed_root(pathlib.Path(root))
        workspace_root.mkdir(parents=True, exist_ok=True)
        _safe_path(workspace_root, workspace_root)
        root_identity = workspace_root.stat()
        dest_dir = workspace_root / _RECEIVED_FOLDER
        _safe_path(workspace_root, dest_dir)
        dest_dir.mkdir(exist_ok=True)
        _safe_path(workspace_root, dest_dir)
        directory_identity = dest_dir.stat()

        def validate_destination() -> None:
            _safe_path(workspace_root, dest_dir)
            if (not os.path.samestat(root_identity, workspace_root.stat())
                    or not os.path.samestat(directory_identity, dest_dir.stat())):
                raise AttachmentError("action_denied")
            if descriptor >= 0 and dest is not None:
                _safe_path(workspace_root, dest)
                if not os.path.samestat(os.fstat(descriptor), dest.stat()):
                    raise AttachmentError("action_denied")

        source_path = saved_path.absolute()
        with _open(source_path.parent, source_path) as source:
            source_identity = os.fstat(source.fileno())
            if not stat.S_ISREG(source_identity.st_mode):
                raise AttachmentError("action_denied")
            name = _safe_filename(workspace_filename or saved_path.name)
            stem, suffix = pathlib.Path(name).stem, pathlib.Path(name).suffix
            counter = 0
            while True:
                validate_destination()
                dest = dest_dir / (name if counter == 0 else f"{stem}_{counter}{suffix}")
                _safe_path(workspace_root, dest)
                try:
                    descriptor = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                         | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0), 0o600)
                    break
                except FileExistsError:
                    counter += 1
            # Check the opened identity before any bytes and each bounded write.
            # A renamed directory or reparse point must not redirect a copy.
            validate_destination()
            remaining = source_identity.st_size
            while remaining:
                data = source.read(min(65536, remaining))
                if not data:
                    raise AttachmentError("revision_conflict")
                view = memoryview(data)
                while view:
                    validate_destination()
                    written = os.write(descriptor, view)
                    if not written:
                        raise OSError("workspace_copy_failed")
                    view = view[written:]
                remaining -= len(data)
            current_source = os.fstat(source.fileno())
            if (source.read(1) or current_source.st_size != source_identity.st_size
                    or current_source.st_mtime_ns != source_identity.st_mtime_ns):
                raise AttachmentError("revision_conflict")
            validate_destination()
            os.fsync(descriptor)
        rel = f"{_RECEIVED_FOLDER}/{dest.name}"
        log.info("Copied to workspace: %s", rel)
        return rel
    except Exception as exc:
        # Remove only this attempt's unchanged, still-contained partial file.
        if descriptor >= 0 and dest is not None:
            try:
                validate_destination()
                os.close(descriptor)
                descriptor = -1
                dest.unlink()
            except (OSError, AttachmentError):
                pass
        log.warning("Could not copy file to workspace: %s", exc)
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
