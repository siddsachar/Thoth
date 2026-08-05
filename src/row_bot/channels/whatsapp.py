"""
Row-Bot – WhatsApp Channel Adapter
====================================
WhatsApp bot using Baileys via a Node.js bridge subprocess.
Communicates with the Node.js process over stdin/stdout JSON-RPC.
No browser required — connects via WebSocket directly.

Setup:
    1. Click **▶️ Start** in Settings → Channels → WhatsApp
       (Node.js is auto-downloaded if not found on first start)
    2. Scan the QR code displayed in the Row-Bot UI with your phone
       (WhatsApp → Linked Devices → Link a Device)
    5. Once authenticated, messages will be bridged to Row-Bot

The bridge process manages WhatsApp authentication, QR code
generation, and message passing.  Session data is persisted in
``~/.row-bot/whatsapp_session/`` so you only need to scan once.
Self-chat is supported — message yourself to talk to the agent.

Required keys (stored via api_keys):
    WHATSAPP_USER_PHONE  – Your phone number for auto-auth (E.164, optional)
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import platform
import queue
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

from row_bot.channels.base import Channel, ChannelCapabilities, ConfigField
from row_bot.channels.auth_store import get_channel_secret
from row_bot.channels import commands as ch_commands
from row_bot.channels import auth as ch_auth
from row_bot.channels import config as ch_config
from row_bot.channels import runtime as ch_runtime
from row_bot.channels.streaming import (
    ChannelDeliveryResult,
    ChannelStreamConfig,
    ChannelStreamConsumer,
    default_split_text,
)
from row_bot.data_paths import get_row_bot_data_dir
from row_bot.runtime_paths import app_root
from row_bot.threads import _save_thread_meta

log = logging.getLogger("row_bot.whatsapp")

WA_MAX_MESSAGE_LEN = 3900


def _agent_mod():
    import row_bot.agent as agent_mod

    return agent_mod

# ──────────────────────────────────────────────────────────────────────
# Markdown → WhatsApp formatting converter
# ──────────────────────────────────────────────────────────────────────
import re as _re


def _convert_tables(text: str) -> str:
    """Convert markdown pipe-tables to a WhatsApp-friendly label: value list."""
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        # Detect table header row: | col | col | ... |
        if _re.match(r"^\|(.+\|)+$", line):
            # Read header
            headers = [c.strip() for c in line.strip("|").split("|")]
            i += 1
            # Skip separator row (| --- | --- |)
            if i < len(lines) and _re.match(r"^\|[\s:_-]+\|", lines[i].strip()):
                i += 1
            # Read data rows
            while i < len(lines):
                row = lines[i].strip()
                if not _re.match(r"^\|(.+\|)+$", row):
                    break
                cells = [c.strip() for c in row.strip("|").split("|")]
                if len(headers) == 2 and len(cells) >= 2:
                    # Key-value table — first cell is the label
                    out.append(f"{cells[0]}: {cells[1]}")
                else:
                    # Multi-column — use header: value pairs
                    parts = []
                    for h, c in zip(headers, cells):
                        if h and c:
                            parts.append(f"{h}: {c}")
                        elif c:
                            parts.append(c)
                    out.append("\n".join(parts) if len(parts) > 2 else "  ".join(parts))
                i += 1
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out)


def _md_to_whatsapp(text: str) -> str:
    """Convert standard Markdown to WhatsApp formatting.

    WhatsApp uses: *bold*, _italic_, ~strikethrough~, ```code```.
    Markdown uses: **bold**, *italic*, ~~strikethrough~~, ```code```.
    """
    # Convert markdown tables to label: value lists
    text = _convert_tables(text)

    # Protect fenced code blocks from further transforms
    blocks: list[str] = []
    def _stash_block(m):
        blocks.append(m.group(0))
        return f"\x00CODE{len(blocks) - 1}\x00"
    text = _re.sub(r"```[\s\S]*?```", _stash_block, text)

    # Protect inline code
    inlines: list[str] = []
    def _stash_inline(m):
        inlines.append(m.group(0))
        return f"\x00INLINE{len(inlines) - 1}\x00"
    text = _re.sub(r"`[^`]+`", _stash_inline, text)

    # Markdown links [text](url) → text (url)
    text = _re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)
    # Strikethrough ~~text~~ → ~text~
    text = _re.sub(r"~~(.+?)~~", r"~\1~", text)
    # Bold **text** → *text*  (must come before italic)
    text = _re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    # Italic *text* → _text_  (only standalone single asterisks)
    text = _re.sub(r"(?<!\w)\*([^*]+?)\*(?!\w)", r"_\1_", text)
    # Headings (# ... at start of line) → bold
    text = _re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", text, flags=_re.MULTILINE)

    # Restore code
    for i, blk in enumerate(blocks):
        text = text.replace(f"\x00CODE{i}\x00", blk)
    for i, inl in enumerate(inlines):
        text = text.replace(f"\x00INLINE{i}\x00", inl)

    return text


# ──────────────────────────────────────────────────────────────────────
# Module-level state
# ──────────────────────────────────────────────────────────────────────
_bridge_proc: subprocess.Popen | None = None
_running = False
_authenticated = False
_qr_code: str | None = None            # Latest QR code string for UI display
_reader_thread: threading.Thread | None = None
_writer_lock = threading.Lock()
_pending_interrupts: dict[str, dict] = {}
_pending_task_approvals: dict[str, dict] = {}  # chat_id → {resume_token, task_name, _ts}
_pending_lock = threading.Lock()
_msg_id_counter = 0
_pending_responses: dict[int, threading.Event] = {}
_response_data: dict[int, dict] = {}

BRIDGE_DIR = Path(__file__).parent / "whatsapp_bridge"
DATA_DIR = get_row_bot_data_dir()
SESSION_DIR = DATA_DIR / "whatsapp_session"
NODE_DIR = DATA_DIR / "node"            # Auto-downloaded portable Node.js
NODE_VERSION = "v22.15.0"               # Pinned LTS version


# ──────────────────────────────────────────────────────────────────────
# Node.js auto-provisioning
# ──────────────────────────────────────────────────────────────────────
def _node_binary_name() -> str:
    """Return the node binary filename for the current OS."""
    return "node.exe" if sys.platform == "win32" else "node"


def _npm_command(node_dir: Path) -> list[str]:
    """Return the npm command for a given Node.js directory."""
    if sys.platform == "win32":
        npm = node_dir / "npm.cmd"
        return [str(npm)]
    npm = node_dir / "bin" / "npm"
    return [str(npm)]


def _node_download_url() -> str:
    """Build the download URL for the portable Node.js archive."""
    machine = platform.machine().lower()
    if sys.platform == "win32":
        arch = "x64" if machine in ("amd64", "x86_64") else "arm64"
        return (
            f"https://nodejs.org/dist/{NODE_VERSION}/"
            f"node-{NODE_VERSION}-win-{arch}.zip"
        )
    elif sys.platform == "darwin":
        arch = "arm64" if machine == "arm64" else "x64"
        return (
            f"https://nodejs.org/dist/{NODE_VERSION}/"
            f"node-{NODE_VERSION}-darwin-{arch}.tar.gz"
        )
    else:  # Linux
        arch = "x64" if machine in ("x86_64", "amd64") else "arm64"
        return (
            f"https://nodejs.org/dist/{NODE_VERSION}/"
            f"node-{NODE_VERSION}-linux-{arch}.tar.xz"
        )


def _download_node(dest_dir: Path) -> Path:
    """Download and extract portable Node.js to *dest_dir*.

    Returns the path to the inner node directory, e.g.
    ``~/.row-bot/node/node-v22.15.0-win-x64/``.

    Raises ``RuntimeError`` on failure.
    """
    url = _node_download_url()
    dest_dir.mkdir(parents=True, exist_ok=True)
    archive_name = url.rsplit("/", 1)[-1]
    archive_path = dest_dir / archive_name

    log.info("Downloading Node.js from %s …", url)
    try:
        urllib.request.urlretrieve(url, str(archive_path))
    except Exception as exc:
        raise RuntimeError(
            f"Failed to download Node.js from {url}: {exc}\n"
            "Install Node.js v18+ manually from https://nodejs.org"
        ) from exc

    log.info("Extracting %s …", archive_name)
    try:
        if archive_name.endswith(".zip"):
            with zipfile.ZipFile(archive_path) as zf:
                zf.extractall(dest_dir)
        elif archive_name.endswith(".tar.gz"):
            with tarfile.open(archive_path, "r:gz") as tf:
                tf.extractall(dest_dir)
        elif archive_name.endswith(".tar.xz"):
            with tarfile.open(archive_path, "r:xz") as tf:
                tf.extractall(dest_dir)
        else:
            raise RuntimeError(f"Unknown archive format: {archive_name}")
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Failed to extract {archive_name}: {exc}") from exc
    finally:
        archive_path.unlink(missing_ok=True)

    # Find the extracted directory (e.g. node-v22.15.0-win-x64)
    inner = archive_name
    for suffix in (".zip", ".tar.gz", ".tar.xz"):
        inner = inner.removesuffix(suffix)
    inner_dir = dest_dir / inner
    if not inner_dir.is_dir():
        # Fallback: find any node-* directory
        candidates = [d for d in dest_dir.iterdir()
                       if d.is_dir() and d.name.startswith("node-")]
        if candidates:
            inner_dir = candidates[0]
        else:
            raise RuntimeError(
                f"Could not find extracted Node.js directory in {dest_dir}"
            )

    log.info("Node.js installed at %s", inner_dir)
    return inner_dir


def _find_node_in_dir(base: Path) -> Path | None:
    """Find the node binary inside a Node.js directory (portable layout)."""
    name = _node_binary_name()
    # Windows: node-vXX-win-x64/node.exe
    direct = base / name
    if direct.is_file():
        return direct
    # Unix: node-vXX-linux-x64/bin/node
    in_bin = base / "bin" / name
    if in_bin.is_file():
        return in_bin
    # Check subdirectories (e.g. ~/.row-bot/node/node-v22.15.0-win-x64/)
    for child in base.iterdir():
        if child.is_dir() and child.name.startswith("node-"):
            for loc in (child / name, child / "bin" / name):
                if loc.is_file():
                    return loc
    return None


def _get_node_dir_from_binary(node_binary: Path) -> Path:
    """Given a node binary path, return the directory containing npm."""
    # Windows: node.exe is at <dir>/node.exe, npm.cmd at <dir>/npm.cmd
    # Unix: node is at <dir>/bin/node, npm at <dir>/bin/npm
    if node_binary.parent.name == "bin":
        return node_binary.parent.parent
    return node_binary.parent


def _ensure_node() -> tuple[str, Path]:
    """Resolve a working Node.js binary.

    Search order:
    1. Bundled Node at ``{app_dir}/node/``  (Windows installer)
    2. System ``node`` on PATH
    3. Cached portable at ``~/.row-bot/node/``
    4. Auto-download to ``~/.row-bot/node/``

    Returns ``(node_cmd, node_dir)`` where *node_cmd* is the full path
    to the node binary and *node_dir* is its parent directory (for npm).

    Raises ``RuntimeError`` if Node.js cannot be found or provisioned.
    """
    # 1. Bundled node (installer deploy)
    app_dir = app_root()
    bundled = app_dir / "node"
    if bundled.is_dir():
        found = _find_node_in_dir(bundled)
        if found:
            log.debug("Using bundled Node.js: %s", found)
            return str(found), _get_node_dir_from_binary(found)

    # 2. System node on PATH
    system_node = shutil.which("node")
    if system_node:
        try:
            result = subprocess.run(
                [system_node, "--version"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                ver = result.stdout.strip()
                log.debug("Using system Node.js %s: %s", ver, system_node)
                return system_node, _get_node_dir_from_binary(Path(system_node))
        except Exception:
            pass  # Broken install, fall through

    # 3. Cached portable in ~/.row-bot/node/
    if NODE_DIR.is_dir():
        found = _find_node_in_dir(NODE_DIR)
        if found:
            log.debug("Using cached portable Node.js: %s", found)
            return str(found), _get_node_dir_from_binary(found)

    # 4. Auto-download
    log.info("Node.js not found — downloading portable runtime…")
    inner_dir = _download_node(NODE_DIR)
    found = _find_node_in_dir(inner_dir)
    if not found:
        found = _find_node_in_dir(NODE_DIR)
    if not found:
        raise RuntimeError(
            "Downloaded Node.js but could not find the binary. "
            "Install Node.js v18+ manually from https://nodejs.org"
        )
    return str(found), _get_node_dir_from_binary(found)


# ──────────────────────────────────────────────────────────────────────
# Helpers — credentials
# ──────────────────────────────────────────────────────────────────────
def _get_user_phone() -> str:
    return get_channel_secret("whatsapp", "WHATSAPP_USER_PHONE")


# ──────────────────────────────────────────────────────────────────────
# Configuration & lifecycle helpers
# ──────────────────────────────────────────────────────────────────────
def is_configured() -> bool:
    """WhatsApp is configured if the bridge directory exists."""
    return (BRIDGE_DIR / "bridge.js").exists()


def is_running() -> bool:
    return _running and _authenticated


def get_qr_code() -> str | None:
    """Return the latest QR code string (for UI display), or None."""
    return _qr_code


# ──────────────────────────────────────────────────────────────────────
# Thread management
# ──────────────────────────────────────────────────────────────────────
def _make_thread_id(chat_id: str) -> str:
    # WhatsApp chat IDs are like "1234567890@c.us"
    safe = chat_id.replace("@", "_").replace(".", "_")
    return f"whatsapp_{safe}"


def _get_or_create_thread(chat_id: str) -> str:
    thread_id = _make_thread_id(chat_id)
    try:
        from row_bot.tasks import record_thread_channel_ref

        record_thread_channel_ref(
            thread_id,
            channel="whatsapp",
            target=chat_id,
            external_conversation_id=str(chat_id),
        )
    except Exception:
        log.debug("WhatsApp thread channel ref skipped", exc_info=True)
    _save_thread_meta(
        thread_id,
        "📲 WhatsApp conversation",
        seed_default_skills=True,
    )  # creates or bumps updated_at
    return thread_id


def _new_thread(chat_id: str) -> str:
    """Force-create a new thread for this chat."""
    import time
    suffix = str(int(time.time()))
    thread_id = f"wa_{chat_id}_{suffix}"
    try:
        from row_bot.tasks import record_thread_channel_ref

        record_thread_channel_ref(
            thread_id,
            channel="whatsapp",
            target=chat_id,
            external_conversation_id=str(chat_id),
        )
    except Exception:
        log.debug("WhatsApp thread channel ref skipped", exc_info=True)
    _save_thread_meta(thread_id, "WhatsApp conversation", seed_default_skills=True)
    return thread_id


# ──────────────────────────────────────────────────────────────────────
# Agent execution
# ──────────────────────────────────────────────────────────────────────
def build_channel_runtime_config(config: dict, purpose: str) -> dict:
    from row_bot.channels.runtime import approval_mode_for_config

    if purpose == "approval":
        runtime_surface = "approval"
        runtime_mode = "agent"
    else:
        runtime_surface = "channel"
        runtime_mode = "auto"
    return {
        **config,
        "configurable": {
            **(config.get("configurable") or {}),
            "runtime_surface": runtime_surface,
            "runtime_mode": runtime_mode,
            "channel_streaming": purpose != "approval",
            "approval_mode": approval_mode_for_config(config),
        },
    }


def _run_agent_sync(user_text: str, config: dict,
                    event_queue=None) -> tuple[str, dict | None, list[bytes], list[str]]:
    from row_bot.tools import registry as tool_registry
    from row_bot.channels.media_capture import grab_vision_capture, grab_generated_image, grab_generated_video

    agent_mod = _agent_mod()
    config = build_channel_runtime_config(config, "message")
    from row_bot.channels import runtime as orchestration_runtime

    config = orchestration_runtime.prepare_channel_turn_config(config, user_text)
    enabled = [t.name for t in tool_registry.get_enabled_tools()]
    full_answer: list[str] = []
    tool_reports: list[str] = []
    interrupt_data: dict | None = None
    used_vision = False
    used_image_gen = False
    used_video_gen = False

    for event_type, payload in agent_mod.stream_agent(user_text, enabled, config):
        if event_queue is not None:
            event_queue.put((event_type, payload))
        if event_type == "token":
            full_answer.append(payload)
        elif event_type == "tool_call":
            tool_reports.append(f"🔧 Using {payload}…")
        elif event_type == "tool_done":
            name = payload['name'] if isinstance(payload, dict) else payload
            raw_name = payload.get('raw_name', '') if isinstance(payload, dict) else ''
            tool_reports.append(f"✅ {name} done")
            if name in ("analyze_image", "👁️ Vision"):
                used_vision = True
            if raw_name in ("generate_image", "edit_image"):
                used_image_gen = True
            if raw_name in ("generate_video", "animate_image"):
                used_video_gen = True
        elif event_type == "interrupt":
            interrupt_data = payload
        elif event_type == "error":
            full_answer.append(f"⚠️ Error: {payload}")
        elif event_type == "done":
            if payload and not full_answer:
                full_answer.append(payload)

    if event_queue is not None:
        event_queue.put(None)  # sentinel

    from row_bot.channels.agent_output import assemble_agent_answer

    answer = assemble_agent_answer("".join(full_answer), tool_reports)

    captured_images: list[bytes] = []
    captured_video_paths: list[str] = []
    if used_vision:
        img = grab_vision_capture()
        if img:
            captured_images.append(img)
    if used_image_gen:
        img = grab_generated_image()
        if img:
            captured_images.append(img)
    if used_video_gen:
        vid_path = grab_generated_video()
        if vid_path:
            captured_video_paths.append(vid_path)
    return answer or "_(No response)_", interrupt_data, captured_images, captured_video_paths


def _resume_agent_sync(
    config: dict,
    approved: bool,
    *,
    interrupt_ids: list[str] | None = None,
    event_queue=None,
) -> tuple[str, dict | None, list[bytes], list[str]]:
    """Resume a paused agent after interrupt approval/denial."""
    agent_mod = _agent_mod()
    config = build_channel_runtime_config(config, "approval")
    from row_bot.tools import registry as tool_registry
    from row_bot.channels.media_capture import grab_vision_capture, grab_generated_image, grab_generated_video

    enabled = [t.name for t in tool_registry.get_enabled_tools()]
    full_answer: list[str] = []
    tool_reports: list[str] = []
    interrupt_data: dict | None = None
    used_vision = False
    used_image_gen = False
    used_video_gen = False

    for event_type, payload in agent_mod.resume_stream_agent(
        enabled, config, approved, interrupt_ids=interrupt_ids
    ):
        if event_queue is not None:
            event_queue.put((event_type, payload))
        if event_type == "token":
            full_answer.append(payload)
        elif event_type == "tool_call":
            tool_reports.append(f"🔧 Using {payload}…")
        elif event_type == "tool_done":
            name = payload['name'] if isinstance(payload, dict) else payload
            raw_name = payload.get('raw_name', '') if isinstance(payload, dict) else ''
            tool_reports.append(f"✅ {name} done")
            if name in ("analyze_image", "👁️ Vision"):
                used_vision = True
            if raw_name in ("generate_image", "edit_image"):
                used_image_gen = True
            if raw_name in ("generate_video", "animate_image"):
                used_video_gen = True
        elif event_type == "interrupt":
            interrupt_data = payload
        elif event_type == "error":
            full_answer.append(f"⚠️ Error: {payload}")
        elif event_type == "done":
            if payload and not full_answer:
                full_answer.append(payload)

    if event_queue is not None:
        event_queue.put(None)

    from row_bot.channels.agent_output import assemble_agent_answer

    answer = assemble_agent_answer("".join(full_answer), tool_reports)

    captured_images: list[bytes] = []
    captured_video_paths: list[str] = []
    if used_vision:
        img = grab_vision_capture()
        if img:
            captured_images.append(img)
    if used_image_gen:
        img = grab_generated_image()
        if img:
            captured_images.append(img)
    if used_video_gen:
        vid_path = grab_generated_video()
        if vid_path:
            captured_video_paths.append(vid_path)
    return answer or "_(No response)_", interrupt_data, captured_images, captured_video_paths


# ──────────────────────────────────────────────────────────────────────
# Bridge communication (JSON-RPC over stdin/stdout)
# ──────────────────────────────────────────────────────────────────────
def _send_to_bridge(method: str, params: dict | None = None) -> int:
    """Send a JSON-RPC message to the bridge process. Returns message ID."""
    global _msg_id_counter
    if _bridge_proc is None or _bridge_proc.stdin is None:
        raise RuntimeError("WhatsApp bridge not running")

    _msg_id_counter += 1
    msg_id = _msg_id_counter

    message = json.dumps({
        "id": msg_id,
        "method": method,
        "params": params or {},
    }) + "\n"

    with _writer_lock:
        _bridge_proc.stdin.write(message)
        _bridge_proc.stdin.flush()

    return msg_id


def _send_and_wait(method: str, params: dict | None = None,
                   timeout: float = 15.0) -> dict | None:
    """Send a JSON-RPC message and wait for the response (blocking)."""
    global _msg_id_counter
    if _bridge_proc is None or _bridge_proc.stdin is None:
        raise RuntimeError("WhatsApp bridge not running")

    event = threading.Event()
    with _writer_lock:
        _msg_id_counter += 1
        msg_id = _msg_id_counter
        _pending_responses[msg_id] = event
        message = json.dumps({
            "id": msg_id,
            "method": method,
            "params": params or {},
        }) + "\n"
        _bridge_proc.stdin.write(message)
        _bridge_proc.stdin.flush()
    try:
        if event.wait(timeout):
            return _response_data.pop(msg_id, None)
        log.warning("Timeout waiting for bridge response to %s (id=%d)", method, msg_id)
        return None
    finally:
        _pending_responses.pop(msg_id, None)
        _response_data.pop(msg_id, None)


def _handle_bridge_message(data: dict) -> None:
    """Process a message received from the bridge."""
    global _authenticated, _qr_code

    msg_type = data.get("type", "")

    if msg_type == "qr":
        first_qr = _qr_code is None
        _qr_code = data.get("qr", "")
        if first_qr:
            log.info("WhatsApp QR code ready — scan with phone")
        else:
            log.debug("WhatsApp QR code refreshed")

    elif msg_type == "ready":
        _authenticated = True
        _qr_code = None
        log.info("WhatsApp authenticated and ready")

    elif msg_type == "disconnected":
        _authenticated = False
        log.warning("WhatsApp disconnected: %s", data.get("reason", ""))

    elif msg_type == "message":
        # Inbound message from a WhatsApp user
        threading.Thread(
            target=_process_inbound,
            args=(data,),
            daemon=True,
            name="wa-inbound",
        ).start()

    elif msg_type == "response":
        # Response to a send command
        msg_id = data.get("id")
        if msg_id in _pending_responses:
            _response_data[msg_id] = data
            _pending_responses[msg_id].set()

    elif msg_type == "auth_failure":
        _authenticated = False
        _qr_code = None
        log.warning("WhatsApp auth failed (stale session cleared): %s",
                    data.get("error", ""))

    elif msg_type == "error":
        log.error("Bridge error: %s", data.get("error", ""))


def _process_inbound(data: dict) -> None:
    """Process an inbound WhatsApp message (runs in thread)."""
    chat_id = data.get("from", "")
    body = data.get("body", "").strip()
    has_media = data.get("hasMedia", False)
    msg_key = data.get("msgKey")  # WAMessageKey for reactions

    if not chat_id:
        return

    # Auth check — self-chat is always authorised (phone owner)
    if not data.get("isSelfChat") and not _is_authorised(chat_id):
        push_name = data.get("pushName", "")
        if body and ch_auth.verify_pairing_code("whatsapp", chat_id, body,
                                                 display_name=push_name):
            _send_message_sync(chat_id, "✅ Paired! You can now chat with Row-Bot.")
            return
        # Don't respond to unauthorised users
        return

    from row_bot.channels.base import record_activity
    record_activity("whatsapp")

    # Slash command dispatch
    if body:
        _cmd_thread_id = (
            _get_or_create_thread(chat_id)
            if ch_commands.is_thread_scoped_command(body)
            else None
        )
        goal_start = ch_runtime.prepare_channel_goal_start(body, _cmd_thread_id)
        if goal_start is not None and _cmd_thread_id:
            config = {"configurable": {"thread_id": _cmd_thread_id}}
            _goal_run_turn, _goal_send_text = _whatsapp_goal_callbacks(chat_id)
            _send_message_sync(chat_id, ch_runtime.format_goal_started_ack(goal_start))
            result = ch_runtime.run_channel_goal_sync(
                channel_name="whatsapp",
                thread_id=_cmd_thread_id,
                config=config,
                first_prompt=goal_start.prompt,
                run_turn=_goal_run_turn,
                send_text=_goal_send_text,
            )
            if result.interrupt_data:
                with _pending_lock:
                    _pending_interrupts[chat_id] = {
                        "data": result.interrupt_data, "config": config
                    }
                from row_bot.channels import approval as approval_helpers
                detail = approval_helpers.format_interrupt_text(result.interrupt_data)
                _send_message_sync(chat_id, detail + "\n\nReply YES or NO.")
            return
        cmd_response = ch_commands.dispatch("whatsapp", body, thread_id=_cmd_thread_id)
        if cmd_response is not None:
            _send_message_sync(chat_id, cmd_response)
            return

    # Handle media
    if has_media:
        _handle_media(chat_id, data, body)
        return

    if not body:
        return

    # Check pending task approvals first (background workflows)
    from row_bot.channels import approval as approval_helpers
    with _pending_lock:
        task_approval = _pending_task_approvals.get(chat_id)
    if task_approval:
        decision = approval_helpers.is_approval_text(body)
        if decision is None:
            _send_message_sync(chat_id, "Approval pending. Reply YES or NO.")
            return
        if decision is not None:
            with _pending_lock:
                _pending_task_approvals.pop(chat_id, None)
            try:
                from row_bot.tasks import respond_to_approval
                result = respond_to_approval(
                    task_approval["resume_token"], decision,
                    note=f"{'Approved' if decision else 'Denied'} via WhatsApp",
                    source="whatsapp",
                )
                action = "✅ Approved" if decision else "❌ Denied"
                if result:
                    _send_message_sync(chat_id,
                                       f"{action} — {task_approval['task_name']}")
                else:
                    _send_message_sync(chat_id,
                                       "⚠️ Could not process approval — it may have expired.")
            except Exception as exc:
                log.error("Task approval error: %s", exc)
                _send_message_sync(chat_id,
                                   f"⚠️ Error processing approval: {exc}")
            return

    # Check pending live-chat interrupts
    with _pending_lock:
        interrupt = _pending_interrupts.get(chat_id)

    if interrupt:
        decision = approval_helpers.is_approval_text(body)
        if decision is None:
            _send_message_sync(chat_id, "Approval pending. Reply YES or NO.")
            return
        with _pending_lock:
            interrupt = _pending_interrupts.pop(chat_id, None)
        if interrupt is None:
            _send_message_sync(chat_id, "This approval is no longer pending.")
            return
        approved = bool(decision)
        config = interrupt.get("config", {})
        interrupt_ids = approval_helpers.extract_interrupt_ids(
            interrupt.get("data")
        )
        ch_runtime.resolve_goal_approval_for_config(config, approved)
        answer, new_interrupt, captured, captured_video_paths, delivery = _run_whatsapp_stream_resume_sync(
            chat_id, config, approved, interrupt_ids=interrupt_ids
        )
        if answer and not delivery.delivered:
            _send_message_sync(chat_id, answer)
        _send_whatsapp_media(chat_id, captured, captured_video_paths)
        captured = []
        captured_video_paths = []
        for img_bytes in captured:
            try:
                import tempfile
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                    tmp.write(img_bytes)
                    tmp_path = tmp.name
                _send_media_sync(chat_id, tmp_path, caption="🖼️ Image")
                os.unlink(tmp_path)
            except Exception as exc:
                log.warning("Failed to send WhatsApp image: %s", exc)
        for vpath in captured_video_paths:
            try:
                _send_media_sync(chat_id, vpath, caption="🎬 Video")
            except Exception as exc:
                log.warning("Failed to send WhatsApp video: %s", exc)
        if new_interrupt:
            with _pending_lock:
                _pending_interrupts[chat_id] = {
                    "data": new_interrupt, "config": config
                }
            detail = approval_helpers.format_interrupt_text(new_interrupt)
            _send_message_sync(chat_id,
                               detail + "\n\nReply YES or NO.")
        elif answer and not ch_runtime.channel_turn_is_suspended(answer):
            thread_id = ch_runtime.thread_id_from_config(config)
            if thread_id:
                _goal_run_turn, _goal_send_text = _whatsapp_goal_callbacks(chat_id)
                goal_result = ch_runtime.continue_channel_goal_after_turn_sync(
                    channel_name="whatsapp",
                    thread_id=thread_id,
                    config=config,
                    assistant_text=answer,
                    interrupt_data=None,
                    run_turn=_goal_run_turn,
                    send_text=_goal_send_text,
                )
                if goal_result.interrupt_data:
                    with _pending_lock:
                        _pending_interrupts[chat_id] = {
                            "data": goal_result.interrupt_data, "config": config
                        }
                    detail = approval_helpers.format_interrupt_text(goal_result.interrupt_data)
                    _send_message_sync(chat_id,
                                       detail + "\n\nReply YES or NO.")
        return

    # Normal message
    thread_id = _get_or_create_thread(chat_id)
    config = {"configurable": {"thread_id": thread_id}}

    # React + typing indicator
    if msg_key:
        _react_sync(chat_id, msg_key, "👀")
    _typing_sync(chat_id)

    try:
        answer, interrupt_data, captured_images, captured_video_paths, delivery = (
            _run_whatsapp_stream_turn_sync(chat_id, body, config)
        )
    except Exception:
        log.warning("Shared WhatsApp streaming failed; falling back to legacy path", exc_info=True)
    else:
        if msg_key:
            _react_sync(chat_id, msg_key, "\U0001f44d")

        from row_bot.channels import extract_youtube_urls
        clean_answer, yt_urls = extract_youtube_urls(answer) if answer else ("", [])

        if delivery.delivered and not interrupt_data:
            for url in yt_urls:
                _send_raw_sync(chat_id, url)
            _send_whatsapp_media(chat_id, captured_images, captured_video_paths)
            return

        if clean_answer and not delivery.delivered:
            _send_message_sync(chat_id, clean_answer)
        for url in yt_urls:
            _send_raw_sync(chat_id, url)
        _send_whatsapp_media(chat_id, captured_images, captured_video_paths)

        if interrupt_data:
            with _pending_lock:
                _pending_interrupts[chat_id] = {
                    "data": interrupt_data, "config": config
                }
            from row_bot.channels import approval as approval_helpers
            detail = approval_helpers.format_interrupt_text(interrupt_data)
            _send_message_sync(chat_id, detail + "\n\nReply YES or NO.")
        return

    # Send placeholder for streaming edits
    placeholder_key = _send_message_sync(chat_id, "⏳", wait_key=True)

    eq: queue.Queue = queue.Queue()
    consumer_thread: threading.Thread | None = None
    streamed_display: str | None = None

    if placeholder_key:
        consumer_thread = threading.Thread(
            target=lambda: None,  # replaced below
            daemon=True,
        )
        # We need the consumer result, so use a mutable container
        _consumer_result: list[str | None] = [None]

        def _run_consumer():
            _consumer_result[0] = _wa_edit_consumer(chat_id, placeholder_key, eq)

        consumer_thread = threading.Thread(target=_run_consumer, daemon=True)
        consumer_thread.start()

    try:
        answer, interrupt_data, captured_images, captured_video_paths = _run_agent_sync(body, config, eq)
    except Exception as exc:
        log.error("Agent error for chat %s: %s", chat_id, exc)
        # Drain queue so consumer exits
        eq.put(None)
        if consumer_thread:
            consumer_thread.join(timeout=5)

        from row_bot.channels.thread_repair import is_corrupt_thread_error
        if is_corrupt_thread_error(exc):
            try:
                from row_bot.agent import repair_orphaned_tool_calls
                repair_orphaned_tool_calls(None, config)
                log.info("Repaired orphaned tool calls for chat %s, retrying", chat_id)
                answer, interrupt_data, captured_images, captured_video_paths = _run_agent_sync(body, config)
            except Exception as retry_exc:
                log.error("Retry after repair failed: %s", retry_exc)
                if msg_key:
                    _react_sync(chat_id, msg_key, "💔")
                _send_message_sync(chat_id,
                    "⚠️ The previous conversation had a stuck tool call "
                    "and couldn't be repaired.\n"
                    "🆕 I've started a fresh thread — please resend your message.")
                thread_id = _new_thread(chat_id)
                return
            # Repair succeeded — send answer normally (no streaming for retry)
            if msg_key:
                _react_sync(chat_id, msg_key, "👍")
            if placeholder_key:
                _edit_message_sync(chat_id, placeholder_key, answer or "_(done)_")
            elif answer and not ch_runtime.channel_turn_is_suspended(answer):
                _send_message_sync(chat_id, answer)
            for img_bytes in captured_images:
                try:
                    import tempfile
                    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                        tmp.write(img_bytes)
                        tmp_path = tmp.name
                    _send_media_sync(chat_id, tmp_path, caption="🖼️ Image")
                    os.unlink(tmp_path)
                except Exception as img_exc:
                    log.warning("Failed to send WhatsApp image: %s", img_exc)
            for vpath in captured_video_paths:
                try:
                    _send_media_sync(chat_id, vpath, caption="🎬 Video")
                except Exception as vid_exc:
                    log.warning("Failed to send WhatsApp video: %s", vid_exc)
            if interrupt_data:
                with _pending_lock:
                    _pending_interrupts[chat_id] = {
                        "data": interrupt_data, "config": config
                    }
                from row_bot.channels import approval as approval_helpers
                detail = approval_helpers.format_interrupt_text(interrupt_data)
                _send_message_sync(chat_id, detail + "\n\nReply YES or NO.")
            return
        else:
            if msg_key:
                _react_sync(chat_id, msg_key, "💔")
            if placeholder_key:
                _edit_message_sync(chat_id, placeholder_key, f"⚠️ Error: {exc}")
            else:
                _send_message_sync(chat_id, f"⚠️ Error: {exc}")
            return

    # Wait for consumer to finish
    if consumer_thread:
        consumer_thread.join(timeout=10)
        streamed_display = _consumer_result[0]

    # Success reaction
    if msg_key:
        _react_sync(chat_id, msg_key, "👍")

    # Extract YouTube URLs so they can be sent separately for auto-preview
    from row_bot.channels import extract_youtube_urls
    if ch_runtime.channel_turn_is_suspended(answer):
        return
    _, yt_urls = extract_youtube_urls(answer) if answer else ("", [])

    # If streaming covered the full response and no interrupt, just send images/URLs
    if streamed_display and not interrupt_data:
        for url in yt_urls:
            _send_raw_sync(chat_id, url)
        for img_bytes in captured_images:
            try:
                import tempfile
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                    tmp.write(img_bytes)
                    tmp_path = tmp.name
                _send_media_sync(chat_id, tmp_path, caption="🖼️ Image")
                os.unlink(tmp_path)
            except Exception as exc:
                log.warning("Failed to send WhatsApp image: %s", exc)
        for vpath in captured_video_paths:
            try:
                _send_media_sync(chat_id, vpath, caption="🎬 Video")
            except Exception as exc:
                log.warning("Failed to send WhatsApp video: %s", exc)
        return

    # Streaming not used or placeholder not available — send normally
    clean_answer, _ = extract_youtube_urls(answer) if answer else ("", [])
    if placeholder_key and clean_answer:
        _edit_message_sync(chat_id, placeholder_key, clean_answer)
    elif clean_answer:
        _send_message_sync(chat_id, clean_answer)
    for url in yt_urls:
        _send_raw_sync(chat_id, url)

    for img_bytes in captured_images:
        try:
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                tmp.write(img_bytes)
                tmp_path = tmp.name
            _send_media_sync(chat_id, tmp_path, caption="🖼️ Image")
            os.unlink(tmp_path)
        except Exception as exc:
            log.warning("Failed to send WhatsApp image: %s", exc)

    for vpath in captured_video_paths:
        try:
            _send_media_sync(chat_id, vpath, caption="🎬 Video")
        except Exception as exc:
            log.warning("Failed to send WhatsApp video: %s", exc)

    if interrupt_data:
        with _pending_lock:
            _pending_interrupts[chat_id] = {
                "data": interrupt_data, "config": config
            }
        from row_bot.channels import approval as approval_helpers
        detail = approval_helpers.format_interrupt_text(interrupt_data)
        _send_message_sync(chat_id,
                            detail + "\n\nReply YES or NO.")


def _cache_inbound_image(thread_id: str, filename: str, data: bytes) -> str:
    """Make an inbound image available to image/video tools for this thread."""
    cache_name = Path(filename or "image").name or "image"
    try:
        import row_bot.tools.image_gen_tool as _igt_mod

        same_thread = (_igt_mod._image_cache_thread_id == thread_id)
        if not same_thread:
            _igt_mod._image_cache.clear()
        _igt_mod._image_cache_thread_id = thread_id
        _igt_mod._image_cache[cache_name] = data
    except Exception:
        log.debug("Failed to cache inbound WhatsApp image '%s'", cache_name, exc_info=True)
    return cache_name


def _handle_media(chat_id: str, data: dict, caption: str) -> None:
    """Process media attachments from WhatsApp."""
    from row_bot.channels.media import (
        transcribe_audio, analyze_image, save_inbound_file,
        extract_document_text, copy_to_workspace,
    )

    media_type = data.get("mediaType", "")
    filename = data.get("filename", "file")
    # Media data is base64-encoded by the bridge
    import base64
    media_b64 = data.get("mediaData", "")
    if not media_b64:
        return
    file_data = base64.b64decode(media_b64)

    thread_id = _get_or_create_thread(chat_id)
    config = {"configurable": {"thread_id": thread_id}}

    if media_type.startswith("audio/"):
        ext = Path(filename).suffix or ".ogg"
        transcript = transcribe_audio(file_data, ext)
        if transcript:
            user_text = f"[Voice message]: {transcript}"
            if caption:
                user_text += f"\n\nCaption: {caption}"
            answer, _, _, _ = _run_agent_sync(user_text, config)
            if answer and not ch_runtime.channel_turn_is_suspended(answer):
                _send_message_sync(chat_id, answer)

    elif media_type.startswith("image/"):
        cache_name = _cache_inbound_image(thread_id, filename, file_data)
        analysis = analyze_image(file_data, caption or "Describe this image")
        if analysis or caption:
            if analysis:
                user_text = (
                    f"[Attached image: {cache_name} — ALREADY ANALYZED, do NOT call analyze_image]\n"
                    f"{analysis}"
                )
            else:
                user_text = f"[Attached image: {cache_name}]"
            if caption:
                user_text += f"\n\nUser said: {caption}"
            answer, _, _, _ = _run_agent_sync(user_text, config)
            if answer and not ch_runtime.channel_turn_is_suspended(answer):
                _send_message_sync(chat_id, answer)

    else:
        saved = save_inbound_file(file_data, filename)
        extracted = extract_document_text(file_data, filename)
        ws_rel = copy_to_workspace(saved)

        parts = [f"[User sent a file: {filename}]"]
        if ws_rel:
            parts.append(f"Workspace path: {ws_rel}")
        else:
            parts.append(f"Saved to: {saved}")
        if extracted:
            parts.append(
                f"\n--- File content ---\n{extracted}"
                f"\n--- End of file ---")
        else:
            parts.append(
                "(Could not extract text — file may be "
                "image-based or unsupported format)")
        if caption:
            parts.append(f"\nUser's message: {caption}")

        user_text = "\n".join(parts)
        answer, _, _, _ = _run_agent_sync(user_text, config)
        if answer and not ch_runtime.channel_turn_is_suspended(answer):
            _send_message_sync(chat_id, answer)


# ──────────────────────────────────────────────────────────────────────
# Sync message helpers
# ──────────────────────────────────────────────────────────────────────
def _react_sync(chat_id: str, msg_key: dict, emoji: str) -> None:
    """Send a reaction emoji on a message (blocking, fire-and-forget)."""
    try:
        _send_to_bridge("send_reaction", {
            "chatId": chat_id, "msgKey": msg_key, "emoji": emoji,
        })
    except Exception as exc:
        log.debug("Failed to send WhatsApp reaction: %s", exc)


def _typing_sync(chat_id: str, presence: str = "composing") -> None:
    """Show typing indicator (lasts ~10s on the other side)."""
    try:
        _send_to_bridge("send_presence", {
            "chatId": chat_id, "presence": presence,
        })
    except Exception as exc:
        log.debug("Failed to send WhatsApp typing: %s", exc)


# ──────────────────────────────────────────────────────────────────────
# Streaming: rate-limited edit consumer (runs in thread)
# ──────────────────────────────────────────────────────────────────────
_WA_STREAM_EDIT_INTERVAL = 2.0  # WhatsApp rate-limits edits harder


class WhatsAppStreamTransport:
    """WhatsApp bridge transport for the shared channel streaming engine."""

    transport_name = "whatsapp:edit"

    def __init__(self, chat_id: str) -> None:
        self.chat_id = str(chat_id)

    async def send_typing(self) -> None:
        await asyncio.to_thread(_typing_sync, self.chat_id)

    async def start(self, text: str):
        return await asyncio.to_thread(
            _send_message_sync,
            self.chat_id,
            str(text or "") or " ",
            wait_key=True,
        )

    async def update(self, handle, text: str, *, final: bool = False):
        if final:
            resp = await asyncio.to_thread(
                _send_and_wait,
                "edit_message",
                {
                    "chatId": self.chat_id,
                    "msgKey": handle,
                    "text": _md_to_whatsapp(str(text or "")),
                },
                10.0,
            )
            if not resp or not resp.get("ok"):
                raise RuntimeError("WhatsApp final edit was not confirmed")
            return handle
        await asyncio.to_thread(_edit_message_sync, self.chat_id, handle, str(text or ""))
        return handle

    async def send_final(self, text: str) -> list[Any]:
        refs: list[Any] = []
        for chunk in self.split_text(str(text or "")):
            ref = await asyncio.to_thread(
                _send_message_sync,
                self.chat_id,
                chunk,
                wait_key=True,
            )
            refs.append(ref or "send_message")
        return refs

    async def cleanup_preview(self, handle) -> None:
        return None

    def split_text(self, text: str) -> list[str]:
        return default_split_text(str(text or ""), WA_MAX_MESSAGE_LEN)


def _cfg_float(channel_config: dict, key: str, default: float) -> float:
    try:
        return float(channel_config.get(key, default))
    except Exception:
        return default


def _cfg_int(channel_config: dict, key: str, default: int) -> int:
    try:
        return int(channel_config.get(key, default))
    except Exception:
        return default


def _whatsapp_stream_config() -> ChannelStreamConfig:
    try:
        channel_config = ch_config.get_all("whatsapp")
    except Exception:
        channel_config = {}
    enabled = bool(channel_config.get("streaming.enabled", True))
    return ChannelStreamConfig(
        channel="whatsapp",
        transport_mode=str(channel_config.get("streaming.transport", "edit") if enabled else "off"),
        update_interval_s=_cfg_float(channel_config, "streaming.update_interval_s", _WA_STREAM_EDIT_INTERVAL),
        max_stale_interval_s=_cfg_float(channel_config, "streaming.max_stale_interval_s", 6.0),
        min_update_chars=_cfg_int(channel_config, "streaming.min_update_chars", 40),
        typing_interval_s=_cfg_float(channel_config, "streaming.typing_interval_s", 7.0),
        cursor=str(channel_config.get("streaming.cursor", "...")),
        max_message_units=WA_MAX_MESSAGE_LEN,
        fresh_final_after_s=_cfg_float(channel_config, "streaming.fresh_final_after_s", 60.0),
        sparse_progress=bool(channel_config.get("streaming.sparse_progress", True)),
    )


async def _stream_agent_turn_to_whatsapp(
    chat_id: str,
    user_text: str,
    config: dict,
) -> tuple[str, dict | None, list[bytes], list[str], ChannelDeliveryResult]:
    loop = asyncio.get_event_loop()
    event_queue: queue.Queue = queue.Queue()
    stream_config = _whatsapp_stream_config()
    consumer = ChannelStreamConsumer(WhatsAppStreamTransport(chat_id), stream_config)
    agent_future = loop.run_in_executor(None, _run_agent_sync, user_text, config, event_queue)
    consumer_task = asyncio.ensure_future(
        consumer.consume_queue(event_queue, final_text_source=agent_future)
    )
    try:
        answer, interrupt_data, captured_images, captured_video_paths = await agent_future
        delivery = await consumer_task
        ch_runtime.persist_channel_assistant_message(
            config,
            answer or delivery.final_text,
            channel_name="whatsapp",
            delivery=delivery,
        )
        return answer, interrupt_data, captured_images, captured_video_paths, delivery
    except Exception:
        if not consumer_task.done():
            consumer_task.cancel()
        try:
            while True:
                event_queue.get_nowait()
        except Exception:
            pass
        raise


async def _stream_agent_resume_to_whatsapp(
    chat_id: str,
    config: dict,
    approved: bool,
    *,
    interrupt_ids: list[str] | None = None,
) -> tuple[str, dict | None, list[bytes], list[str], ChannelDeliveryResult]:
    loop = asyncio.get_event_loop()
    event_queue: queue.Queue = queue.Queue()
    stream_config = _whatsapp_stream_config()
    consumer = ChannelStreamConsumer(WhatsAppStreamTransport(chat_id), stream_config)
    agent_future = loop.run_in_executor(
        None,
        lambda: _resume_agent_sync(
            config,
            approved,
            interrupt_ids=interrupt_ids,
            event_queue=event_queue,
        ),
    )
    consumer_task = asyncio.ensure_future(
        consumer.consume_queue(event_queue, final_text_source=agent_future)
    )
    try:
        answer, interrupt_data, captured_images, captured_video_paths = await agent_future
        delivery = await consumer_task
        ch_runtime.persist_channel_assistant_message(
            config,
            answer or delivery.final_text,
            channel_name="whatsapp",
            delivery=delivery,
        )
        return answer, interrupt_data, captured_images, captured_video_paths, delivery
    except Exception:
        if not consumer_task.done():
            consumer_task.cancel()
        try:
            while True:
                event_queue.get_nowait()
        except Exception:
            pass
        raise


def _run_whatsapp_stream_turn_sync(
    chat_id: str,
    user_text: str,
    config: dict,
) -> tuple[str, dict | None, list[bytes], list[str], ChannelDeliveryResult]:
    return asyncio.run(_stream_agent_turn_to_whatsapp(chat_id, user_text, config))


def _run_whatsapp_stream_resume_sync(
    chat_id: str,
    config: dict,
    approved: bool,
    *,
    interrupt_ids: list[str] | None = None,
) -> tuple[str, dict | None, list[bytes], list[str], ChannelDeliveryResult]:
    return asyncio.run(
        _stream_agent_resume_to_whatsapp(
            chat_id,
            config,
            approved,
            interrupt_ids=interrupt_ids,
        )
    )


def _send_whatsapp_media(
    chat_id: str,
    captured_images: list[bytes],
    captured_video_paths: list[str] | None = None,
) -> None:
    for img_bytes in captured_images:
        try:
            import tempfile

            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                tmp.write(img_bytes)
                tmp_path = tmp.name
            _send_media_sync(chat_id, tmp_path, caption="Image")
            os.unlink(tmp_path)
        except Exception as exc:
            log.warning("Failed to send WhatsApp image: %s", exc)
    for vpath in captured_video_paths or []:
        try:
            _send_media_sync(chat_id, vpath, caption="Video")
        except Exception as exc:
            log.warning("Failed to send WhatsApp video: %s", exc)


def _whatsapp_goal_callbacks(chat_id: str):
    state: dict[str, ChannelDeliveryResult | None] = {"delivery": None}

    def _goal_run_turn(prompt: str, cfg: dict):
        answer, interrupt_data, captured_images, captured_video_paths, delivery = (
            _run_whatsapp_stream_turn_sync(chat_id, prompt, cfg)
        )
        state["delivery"] = delivery
        _send_whatsapp_media(chat_id, captured_images, captured_video_paths)
        return answer, interrupt_data

    def _goal_send_text(message: str) -> None:
        delivery = state.get("delivery")
        if (
            delivery
            and delivery.delivered
            and str(delivery.final_text or "").strip() == str(message or "").strip()
        ):
            state["delivery"] = None
            return
        state["delivery"] = None
        _send_message_sync(chat_id, message)

    return _goal_run_turn, _goal_send_text


def _wa_edit_consumer(chat_id: str, msg_key: dict,
                      event_queue: queue.Queue) -> str | None:
    """Read events from queue and edit the WhatsApp placeholder progressively.

    Runs in a thread.  Returns the final display text, or None on error.
    """
    accumulated = ""
    tool_lines: list[str] = []
    last_edit = 0.0

    while True:
        try:
            event = event_queue.get(timeout=0.3)
        except Exception:
            continue
        if event is None:
            break

        event_type, payload = event
        if event_type == "token":
            accumulated += payload
        elif event_type == "tool_call":
            tool_lines.append(f"🔧 Using {payload}…")
        elif event_type == "tool_done":
            name = payload['name'] if isinstance(payload, dict) else payload
            tool_lines.append(f"✅ {name} done")

        now = time.monotonic()
        if now - last_edit >= _WA_STREAM_EDIT_INTERVAL:
            display = _build_stream_display(tool_lines, accumulated)
            if display:
                try:
                    _edit_message_sync(chat_id, msg_key, display)
                except Exception:
                    pass
                last_edit = now

    # Final edit — use _send_and_wait so we know if it succeeded
    display = _build_stream_display(tool_lines, accumulated)
    if display:
        try:
            resp = _send_and_wait("edit_message", {
                "chatId": chat_id, "msgKey": msg_key,
                "text": _md_to_whatsapp(display),
            }, timeout=10.0)
            if not resp or not resp.get("ok"):
                log.warning("Final streaming edit not confirmed, will resend")
                return None
        except Exception as exc:
            log.warning("Final streaming edit failed: %s", exc)
            return None
    return display or None


def _build_stream_display(tool_lines: list[str], accumulated: str) -> str:
    parts = []
    if tool_lines:
        parts.append("\n".join(tool_lines))
    if accumulated:
        parts.append(accumulated)
    return ("\n\n".join(parts)) if parts else ""


def _edit_message_sync(chat_id: str, msg_key: dict, text: str) -> None:
    """Edit a previously sent message (blocking, fire-and-forget)."""
    try:
        _send_to_bridge("edit_message", {
            "chatId": chat_id, "msgKey": msg_key, "text": _md_to_whatsapp(text),
        })
    except Exception as exc:
        log.debug("Failed to edit WhatsApp message: %s", exc)


def _send_message_sync(chat_id: str, text: str, *, wait_key: bool = False) -> dict | None:
    """Send a text message via the bridge (blocking).

    If *wait_key* is True, waits for the bridge response and returns the
    sent message key (a dict with remoteJid/id/fromMe) for later edits.
    """
    try:
        if wait_key:
            resp = _send_and_wait("send_message", {
                "chatId": chat_id, "text": _md_to_whatsapp(text),
            })
            return resp.get("msgKey") if resp else None
        _send_to_bridge("send_message", {"chatId": chat_id, "text": _md_to_whatsapp(text)})
    except Exception as exc:
        log.warning("Failed to send WhatsApp message: %s", exc)
    return None


def _send_raw_sync(chat_id: str, text: str) -> None:
    """Send a message without the Row-Bot prefix (for link previews)."""
    try:
        _send_to_bridge("send_message", {"chatId": chat_id, "text": text, "raw": True})
    except Exception as exc:
        log.warning("Failed to send raw WhatsApp message: %s", exc)


def _send_media_sync(chat_id: str, file_path: str,
                     caption: str | None = None) -> None:
    """Send a media file via the bridge and wait for send confirmation."""
    try:
        p = Path(file_path)
        ext = p.suffix.lower()
        params = {
            "chatId": chat_id,
            "filename": p.name,
            "caption": caption or "",
        }
        timeout = 45.0
        if ext in {".mp4", ".mov", ".avi", ".mkv"}:
            # Send videos by path so the bridge can stream/upload directly
            # without base64-encoding large files through stdin.
            params["filePath"] = str(p)
            timeout = 120.0
        else:
            params["mediaData"] = base64.b64encode(p.read_bytes()).decode()

        resp = _send_and_wait("send_media", params, timeout=timeout)
        if not resp:
            raise RuntimeError(f"Timeout waiting for bridge response while sending '{p.name}'")
        if not resp.get("ok"):
            raise RuntimeError(resp.get("error") or f"Bridge rejected media '{p.name}'")
    except Exception as exc:
        log.warning("Failed to send WhatsApp media: %s", exc)
        raise


def send_outbound(chat_id: str, text: str) -> None:
    """Public API: send a text message."""
    if not _running:
        raise RuntimeError("WhatsApp bridge is not running")
    _send_message_sync(chat_id, text)


def send_photo(chat_id: str, file_path: str,
               caption: str | None = None) -> None:
    """Public API: send a photo."""
    if not _running:
        raise RuntimeError("WhatsApp bridge is not running")
    _send_media_sync(chat_id, file_path, caption)


def send_document(chat_id: str, file_path: str,
                  caption: str | None = None) -> None:
    """Public API: send a document."""
    send_photo(chat_id, file_path, caption)


# ──────────────────────────────────────────────────────────────────────
# Auth
# ──────────────────────────────────────────────────────────────────────
def _is_authorised(chat_id: str) -> bool:
    """Check if a WhatsApp chat ID is authorised."""
    allowed = _get_user_phone()
    if allowed:
        # Compare normalised phone numbers
        normalised_allowed = allowed.replace("+", "").replace("-", "").replace(" ", "")
        normalised_chat = chat_id.split("@")[0] if "@" in chat_id else chat_id
        if normalised_chat == normalised_allowed:
            return True
    return ch_auth.is_user_approved("whatsapp", chat_id)


# ──────────────────────────────────────────────────────────────────────
# Bridge lifecycle
# ──────────────────────────────────────────────────────────────────────
def _reader_loop() -> None:
    """Read JSON messages from bridge stdout in a background thread."""
    global _running
    proc = _bridge_proc
    if proc is None or proc.stdout is None:
        return

    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                _handle_bridge_message(data)
            except json.JSONDecodeError:
                log.debug("Non-JSON bridge output: %s", line[:200])
    except Exception as exc:
        log.error("Bridge reader error: %s", exc)
    finally:
        _running = False
        log.info("WhatsApp bridge reader stopped")


async def start_bot() -> bool:
    """Start the WhatsApp bridge subprocess."""
    global _bridge_proc, _running, _reader_thread, _authenticated, _qr_code

    if _running:
        log.info("WhatsApp bridge already running")
        return True

    bridge_script = BRIDGE_DIR / "bridge.js"
    if not bridge_script.exists():
        log.warning("WhatsApp bridge not found at %s", bridge_script)
        return False

    # Resolve Node.js (bundled → system → cached → auto-download)
    try:
        node_cmd, node_dir = _ensure_node()
    except RuntimeError as exc:
        log.error("%s", exc)
        return False

    npm_cmd = _npm_command(node_dir)

    # Directory containing the node binary — needed for PATH on all platforms
    # (on Unix node_dir is the parent of bin/, so node_dir alone won't work)
    _node_bin_dir = str(Path(node_cmd).parent)

    # Check for node_modules (verify it actually has packages, not just empty dir)
    node_modules = BRIDGE_DIR / "node_modules"
    _needs_install = (
        not node_modules.exists()
        or not (node_modules / ".package-lock.json").exists()
    )
    if _needs_install:
        log.info("Installing WhatsApp bridge dependencies...")
        # Put portable Node.js on PATH so npm can find the `node` binary.
        install_env = {**os.environ, "PATH": _node_bin_dir + os.pathsep + os.environ.get("PATH", "")}
        try:
            result = subprocess.run(
                npm_cmd + ["install"],
                cwd=str(BRIDGE_DIR),
                capture_output=True, text=True, timeout=180,
                shell=(sys.platform == "win32"),
                env=install_env,
                encoding="utf-8", errors="replace",
            )
            if result.returncode != 0:
                log.error("npm install failed (rc=%d): %s",
                          result.returncode, result.stderr[:500])
                return False
        except Exception as exc:
            log.error("Failed to install bridge dependencies: %s", exc)
            return False

    # Ensure session directory exists
    SESSION_DIR.mkdir(parents=True, exist_ok=True)

    try:
        # Ensure portable Node.js is on PATH for bridge subprocess
        bridge_env = {
            **os.environ,
            "WA_SESSION_DIR": str(SESSION_DIR),
            "PATH": _node_bin_dir + os.pathsep + os.environ.get("PATH", ""),
        }
        _bridge_proc = subprocess.Popen(
            [node_cmd, str(bridge_script)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8", errors="replace",
            cwd=str(BRIDGE_DIR),
            env=bridge_env,
        )

        _running = True
        _authenticated = False
        _qr_code = None

        # Start reader thread
        _reader_thread = threading.Thread(
            target=_reader_loop, daemon=True, name="wa-bridge-reader"
        )
        _reader_thread.start()

        # Start stderr reader to log bridge errors
        _STDERR_INFO_PREFIXES = (
            "[wa-bridge] WhatsApp connected",
            "[wa-bridge] QR code generated",
            "[wa-bridge] Starting WhatsApp bridge",
            "[wa-bridge] Logged out",
            "[wa-bridge] Session cleared",
            "[wa-bridge] Connection closed",
            "[wa-bridge] stdin closed",
            "[wa-bridge] SIGTERM",
            "[wa-bridge] SIGINT",
            "[wa-bridge] Shutting down",
        )
        # Baileys / libsignal noise — normal session renegotiation;
        # downgrade to DEBUG so they don't clutter the log.
        _STDERR_NOISE_PATTERNS = (
            "Bad MAC",
            "MessageCounterError",
            "Key used already or never filled",
            "Failed to decrypt message",
            "Closing open session in favor of incoming prekey bundle",
            "Session error",
            "session_cipher.js",
            "queue_job.js",
            "crypto.js",
            "at Object.verifyMAC",
            "at SessionCipher",
            "at async ",
            "[debug]",
        )

        def _stderr_reader():
            try:
                for line in _bridge_proc.stderr:
                    line = line.strip()
                    if not line:
                        continue
                    # Known info messages → DEBUG
                    if any(line.startswith(p) for p in _STDERR_INFO_PREFIXES):
                        log.debug("bridge: %s", line[:300])
                    # Baileys/libsignal session noise → DEBUG
                    elif any(p in line for p in _STDERR_NOISE_PATTERNS):
                        log.debug("bridge noise: %s", line[:300])
                    else:
                        log.warning("bridge stderr: %s", line[:500])
            except Exception:
                pass

        threading.Thread(
            target=_stderr_reader, daemon=True, name="wa-bridge-stderr"
        ).start()

        log.info("WhatsApp bridge started (PID %d)", _bridge_proc.pid)
        return True

    except Exception as exc:
        log.error("Failed to start WhatsApp bridge: %s", exc)
        _running = False
        return False


def clear_session() -> None:
    """Delete cached WhatsApp session data so the next start gets a fresh QR."""
    if SESSION_DIR.exists():
        import shutil
        shutil.rmtree(SESSION_DIR, ignore_errors=True)
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        log.info("Cleared WhatsApp session data")
    else:
        log.info("No WhatsApp session data to clear")


async def stop_bot() -> None:
    """Stop the WhatsApp bridge subprocess."""
    global _bridge_proc, _running, _reader_thread, _authenticated, _qr_code

    if not _running and _bridge_proc is None:
        return

    _running = False
    _authenticated = False
    _qr_code = None

    if _bridge_proc:
        try:
            # Graceful shutdown: send command via stdin so bridge can
            # close the WebSocket and exit cleanly.
            if _bridge_proc.stdin:
                try:
                    _bridge_proc.stdin.write(
                        json.dumps({"id": "shutdown", "method": "shutdown", "params": {}}) + "\n"
                    )
                    _bridge_proc.stdin.flush()
                except OSError:
                    pass  # stdin already closed
            _bridge_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            log.warning("Bridge did not exit gracefully, killing")
            _bridge_proc.kill()
        except Exception as exc:
            log.warning("Error stopping bridge: %s", exc)
        _bridge_proc = None

    _reader_thread = None
    log.info("WhatsApp bridge stopped")


# ──────────────────────────────────────────────────────────────────────
# Channel class
# ──────────────────────────────────────────────────────────────────────

_send_photo_fn = send_photo
_send_document_fn = send_document


class WhatsAppChannel(Channel):
    """WhatsApp channel adapter via Baileys Node.js bridge."""

    @property
    def name(self) -> str:
        return "whatsapp"

    @property
    def display_name(self) -> str:
        return "WhatsApp"

    @property
    def icon(self) -> str:
        return "chat"

    @property
    def setup_guide(self) -> str:
        return (
            "### Quick Setup\n"
            "1. Click **▶️ Start** below\n"
            "   _(Node.js is auto-installed if not found — first start may take ~30s)_\n"
            "2. Scan the **QR code** with your phone:\n"
            "   WhatsApp → Settings → Linked Devices → Link a Device\n"
            "3. Once authenticated, messages will be bridged to Row-Bot\n"
            "4. (Optional) Set your phone number below for auto-auth"
        )

    @property
    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            photo_in=True,
            voice_in=True,
            document_in=True,
            photo_out=True,
            document_out=True,
            buttons=False,
            streaming=True,
            typing=True,
            reactions=True,
            slash_commands=True,   # Text-based /commands
        )

    @property
    def config_fields(self) -> list[ConfigField]:
        return [
            ConfigField(
                key="user_phone",
                label="Your Phone Number (optional)",
                field_type="text",
                storage="env",
                env_key="WHATSAPP_USER_PHONE",
                help_text="Your phone in E.164 format for auto-auth (or use DM pairing)",
            ),
        ]

    async def start(self) -> bool:
        return await start_bot()

    async def stop(self) -> None:
        await stop_bot()

    def is_configured(self) -> bool:
        return is_configured()

    def is_running(self) -> bool:
        return is_running()

    def get_default_target(self) -> str:
        phone = _get_user_phone()
        if phone:
            normalised = phone.replace("+", "").replace("-", "").replace(" ", "")
            return f"{normalised}@c.us"
        approved = ch_auth.get_approved_users("whatsapp")
        if approved:
            return approved[0]
        raise RuntimeError(
            "No WhatsApp user configured — set WHATSAPP_USER_PHONE or pair via DM"
        )

    def send_message(self, target: str | int, text: str) -> None:
        send_outbound(str(target), text)

    def send_photo(self, target: str | int, file_path: str,
                   caption: str | None = None) -> None:
        _send_photo_fn(str(target), file_path, caption=caption)

    def send_document(self, target: str | int, file_path: str,
                      caption: str | None = None) -> None:
        _send_document_fn(str(target), file_path, caption=caption)

    def send_approval_request(self, target: str | int,
                              interrupt_data: Any,
                              config: dict) -> str | None:
        """Send a task-approval prompt and store pending state."""
        task_name = config.get("task_name", "Unknown task")
        message = config.get("message", "Approval required.")
        resume_token = config.get("resume_token", "")
        text = (
            f"⚠️ *Approval Required*\n"
            f"*Task:* {task_name}\n"
            f"{message}\n\n"
            "Reply *yes* or *no*."
        )
        try:
            send_outbound(str(target), text)
            import time as _time
            with _pending_lock:
                _pending_task_approvals[str(target)] = {
                    "resume_token": resume_token,
                    "task_name": task_name,
                    "_ts": _time.time(),
                }
            return str(target)
        except Exception as exc:
            log.warning("Failed to send WhatsApp approval: %s", exc)
            return None

    def update_approval_message(self, message_ref: str,
                                status: str,
                                source: str = "") -> None:
        """Clear pending task approval and notify that it was resolved elsewhere."""
        with _pending_lock:
            _pending_task_approvals.pop(message_ref, None)
        action = "Approved ✅" if status == "approved" else "Denied ❌"
        try:
            send_outbound(message_ref,
                          f"{action} (via {source})" if source else action)
        except Exception:
            pass

    def build_custom_ui(self, container) -> None:
        """Show live QR code in the settings panel when authenticating."""
        from nicegui import ui
        from row_bot.ui.timer_utils import safe_timer

        with container:
            qr_container = ui.column().classes(
                "w-full items-center gap-2"
            ).style("min-height: 40px;")

        _last_qr: list[str | None] = [None]
        _last_auth: list[bool | None] = [None]

        def _refresh_qr():
            qr = get_qr_code()
            running = _running          # bridge process alive (not is_running which requires auth)
            auth = running and _authenticated

            if _last_auth[0] is not None and qr == _last_qr[0] and auth == _last_auth[0]:
                return
            _last_qr[0] = qr
            _last_auth[0] = auth

            log.debug("QR UI refresh: running=%s auth=%s qr=%s",
                      running, auth, bool(qr))

            qr_container.clear()
            with qr_container:
                if auth:
                    ui.icon("check_circle").classes(
                        "text-green-5"
                    ).style("font-size: 32px;")
                    ui.label("WhatsApp connected").classes(
                        "text-sm text-green-5"
                    )
                elif qr:
                    ui.label(
                        "Scan this QR code with WhatsApp:"
                    ).classes("text-sm text-grey-4")
                    try:
                        import qrcode as _qr_lib
                        img = _qr_lib.make(qr, box_size=6, border=2)
                        buf = io.BytesIO()
                        img.save(buf, format="PNG")
                        b64 = base64.b64encode(buf.getvalue()).decode()
                        ui.image(
                            f"data:image/png;base64,{b64}"
                        ).style(
                            "width: 260px; height: 260px;"
                            " image-rendering: pixelated;"
                        )
                    except ImportError:
                        ui.code(qr).classes("text-xs")
                    ui.label(
                        "WhatsApp → Settings → Linked Devices → Link a Device"
                    ).classes("text-xs text-grey-6")
                elif running:
                    ui.spinner("dots", size="lg").classes("text-primary")
                    ui.label("Waiting for QR code...").classes(
                        "text-sm text-grey-5"
                    )

        def _reset_session():
            """Clear session and restart for a fresh QR."""
            clear_session()
            ui.notify("Session cleared — restart WhatsApp to scan a new QR",
                      type="info")

        ui.button(
            "Reset Session",
            on_click=_reset_session,
            icon="restart_alt",
        ).props("flat dense size=sm").classes("text-grey-5")

        _refresh_qr()
        safe_timer(2.0, _refresh_qr)


# ──────────────────────────────────────────────────────────────────────
# Register with channel registry
# ──────────────────────────────────────────────────────────────────────
_whatsapp_channel = WhatsAppChannel()

try:
    from row_bot.channels import registry as _ch_registry
    _ch_registry.register(_whatsapp_channel)
except Exception:
    pass
