"""System Info tool — one-shot system overview via psutil.

Returns OS, CPU, RAM, disk, network, battery, and top processes
in a single call so the agent can reason over whichever section
is relevant to the user's question.
"""

from __future__ import annotations

import os
import platform
import socket
import time

import psutil

from langchain_core.tools import StructuredTool

from tools.base import BaseTool
from tools import registry


# ── Helpers ──────────────────────────────────────────────────────────────────

def _fmt_bytes(n: int | float) -> str:
    """Human-readable bytes (e.g. 16.2 GB)."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _fmt_uptime(seconds: float) -> str:
    d = int(seconds // 86400)
    h = int((seconds % 86400) // 3600)
    m = int((seconds % 3600) // 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    parts.append(f"{m}m")
    return " ".join(parts)


def _get_public_ip() -> str:
    """Fetch public IP via a lightweight HTTP call. Returns 'N/A' on failure."""
    try:
        import urllib.request
        with urllib.request.urlopen("https://api.ipify.org", timeout=3) as r:
            return r.read().decode().strip()
    except Exception:
        return "N/A"


def _get_local_ip() -> str:
    """Get the primary local IP address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ── Main function ────────────────────────────────────────────────────────────

def _get_system_info(query: str = "") -> str:
    """Collect and return a full system info snapshot."""
    sections: list[str] = []

    # ── OS & Machine ────────────────────────────────────────────────────
    uname = platform.uname()
    boot = psutil.boot_time()
    uptime = time.time() - boot
    sections.append(
        f"[System]\n"
        f"  OS: {uname.system} {uname.release} ({uname.version})\n"
        f"  Machine: {uname.machine}\n"
        f"  Hostname: {uname.node}\n"
        f"  Username: {os.getlogin()}\n"
        f"  Uptime: {_fmt_uptime(uptime)}"
    )

    # ── CPU ──────────────────────────────────────────────────────────────
    cpu_freq = psutil.cpu_freq()
    freq_str = f"{cpu_freq.current:.0f} MHz" if cpu_freq else "N/A"
    sections.append(
        f"[CPU]\n"
        f"  Processor: {platform.processor() or uname.processor or 'N/A'}\n"
        f"  Cores: {psutil.cpu_count(logical=False)} physical, {psutil.cpu_count()} logical\n"
        f"  Frequency: {freq_str}\n"
        f"  Usage: {psutil.cpu_percent(interval=0.5):.1f}%"
    )

    # ── Memory ───────────────────────────────────────────────────────────
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    sections.append(
        f"[Memory]\n"
        f"  RAM: {_fmt_bytes(mem.used)} / {_fmt_bytes(mem.total)} ({mem.percent}% used)\n"
        f"  Available: {_fmt_bytes(mem.available)}\n"
        f"  Swap: {_fmt_bytes(swap.used)} / {_fmt_bytes(swap.total)} ({swap.percent}% used)"
    )

    # ── Disk ─────────────────────────────────────────────────────────────
    disk_lines = []
    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
            disk_lines.append(
                f"  {part.device} ({part.mountpoint}): "
                f"{_fmt_bytes(usage.used)} / {_fmt_bytes(usage.total)} "
                f"({usage.percent}% used, {_fmt_bytes(usage.free)} free) "
                f"[{part.fstype}]"
            )
        except (PermissionError, OSError):
            disk_lines.append(f"  {part.device} ({part.mountpoint}): access denied")
    sections.append("[Disk]\n" + "\n".join(disk_lines) if disk_lines else "[Disk]\n  No partitions found")

    # ── Network ──────────────────────────────────────────────────────────
    local_ip = _get_local_ip()
    public_ip = _get_public_ip()
    sections.append(
        f"[Network]\n"
        f"  Local IP: {local_ip}\n"
        f"  Public IP: {public_ip}\n"
        f"  Hostname: {socket.gethostname()}"
    )

    # ── Battery ──────────────────────────────────────────────────────────
    battery = psutil.sensors_battery()
    if battery:
        plug = "plugged in" if battery.power_plugged else "on battery"
        time_left = ""
        if battery.secsleft > 0 and not battery.power_plugged:
            time_left = f", ~{_fmt_uptime(battery.secsleft)} remaining"
        sections.append(
            f"[Battery]\n"
            f"  {battery.percent}% ({plug}{time_left})"
        )
    else:
        sections.append("[Battery]\n  No battery detected (desktop)")

    # ── Top processes (by CPU) ───────────────────────────────────────────
    procs = []
    for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
        try:
            info = p.info
            procs.append(info)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    # Sort by CPU, then memory as tiebreaker
    procs.sort(key=lambda x: (x.get("cpu_percent") or 0, x.get("memory_percent") or 0), reverse=True)
    top = procs[:10]
    proc_lines = []
    for p in top:
        proc_lines.append(
            f"  PID {p['pid']:>6}  CPU {p.get('cpu_percent', 0):5.1f}%  "
            f"MEM {p.get('memory_percent', 0):5.1f}%  {p.get('name', '?')}"
        )
    sections.append("[Top Processes (by CPU)]\n" + "\n".join(proc_lines) if proc_lines else "[Processes]\n  Unable to list")

    return "\n\n".join(sections)


# ── Tool class ───────────────────────────────────────────────────────────────

class SystemInfoTool(BaseTool):

    @property
    def name(self) -> str:
        return "system_info"

    @property
    def display_name(self) -> str:
        return "🖥️ System Info"

    @property
    def description(self) -> str:
        return (
            "Get system information: OS, CPU, RAM, disk space, "
            "network (IP addresses), battery status, and top processes."
        )

    @property
    def enabled_by_default(self) -> bool:
        return True

    @property
    def required_api_keys(self) -> dict[str, str]:
        return {}

    def execute(self, query: str) -> str:
        return _get_system_info(query)

    def as_langchain_tool(self):
        return StructuredTool.from_function(
            func=_get_system_info,
            name="get_system_info",
            description=(
                "Get a full snapshot of the local system: OS, hostname, uptime, "
                "CPU (cores, frequency, usage), RAM, disk space per drive, "
                "network IP addresses, battery status, and top 10 processes "
                "by CPU usage. Use when the user asks about their computer, "
                "disk space, IP address, system resources, running processes, "
                "or any hardware/OS question about this machine."
            ),
        )


registry.register(SystemInfoTool())
