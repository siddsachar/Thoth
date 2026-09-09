"""Run the new shell against one private real NiceGUI/API host.

Uses the unchanged Phase 1 scripted provider fixture, an owned loopback port,
and a fresh data directory. Node and browser dependencies must already exist.
No environment synchronization or browser installation is performed here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import secrets
import socket
import subprocess
import sys
import time
from urllib.request import ProxyHandler, build_opener
import uuid

import psutil


ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
EVIDENCE = ROOT / ".local/evidence/unified-client-platform/phase-2/qa"


def fingerprint() -> dict[str, object]:
    """Identify dirty/new implementation and the exact generated assets."""
    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", "-c", f"safe.directory={ROOT.as_posix()}", "-c", "core.safecrlf=false", *args],
            cwd=ROOT, text=True,
        ).strip()

    scope = ("src", "frontend", "tests", "scripts", "contracts", "docs",
             "pyproject.toml", "uv.lock", "requirements.txt")
    paths = set(git("diff", "--name-only", "--", *scope).splitlines())
    paths.update(git("ls-files", "--others", "--exclude-standard", "--", *scope).splitlines())
    assets = ROOT / "frontend/dist"
    if assets.exists():
        paths.update(path.relative_to(ROOT).as_posix() for path in assets.rglob("*") if path.is_file())
    return {
        "head": git("rev-parse", "HEAD"), "branch": git("branch", "--show-current"),
        "files": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                  if (ROOT / name).is_file() else "deleted" for name in sorted(paths)},
    }


def private_environment(short: Path, port: int, token: str) -> dict[str, str]:
    """Allowlist system launch variables; never copy provider credentials."""
    env = {key: value for key, value in os.environ.items() if key.upper() in {
        "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "APPDATA", "LOCALAPPDATA",
        "USERPROFILE", "PROGRAMFILES", "PROGRAMFILES(X86)", "HOMEDRIVE", "HOMEPATH",
    }}
    for name in ("system-temp", "data", "workspace", "hf-cache"):
        (short / name).mkdir(parents=True)
    env.update({
        "PYTHONPATH": os.pathsep.join([str(HERE / "harness"), str(ROOT / "src"), str(ROOT)]),
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8",
        "ROW_BOT_DATA_DIR": str(short / "data"), "ROW_BOT_WORKSPACE": str(short / "workspace"),
        "ROW_BOT_HOST": "127.0.0.1", "ROW_BOT_PORT": str(port),
        "ROW_BOT_TEST_MODE": "1", "ROW_BOT_DOCS_CAPTURE": "1", "ROW_BOT_DOCS_REAL_DATA": "0",
        "ROW_BOT_DOCS_DISABLE_NETWORK": "1", "ROW_BOT_DOCS_DISABLE_AUTOSTART": "1",
        "ROW_BOT_DOCS_FAKE_PROVIDERS": "1", "ROW_BOT_DOCS_REDUCE_MOTION": "1",
        "P1_BROWSER_CONTROL_TOKEN": token, "P2_ALLOWED_PORT": str(port),
        "ROW_BOT_LAUNCH_SECRET": secrets.token_urlsafe(32),
        "HF_HOME": str(short / "hf-cache"), "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
        "TEMP": str(short / "system-temp"), "TMP": str(short / "system-temp"),
        "GIT_CEILING_DIRECTORIES": str(short), "UV_NO_SYNC": "1", "UV_OFFLINE": "1",
        "PLAYWRIGHT_BROWSERS_PATH": str(ROOT / ".tmp/p2-browser-cache"),
        "USERNAME": "rowbot-fixture", "USER": "rowbot-fixture",
    })
    return env


def stop_owned(process: subprocess.Popen) -> None:
    """Stop only descendants of this exact still-owned child process."""
    if process.poll() is not None:
        return
    try:
        children = psutil.Process(process.pid).children(recursive=True)
    except psutil.NoSuchProcess:
        children = []
    for child in reversed(children):
        try:
            child.terminate()
        except psutil.NoSuchProcess:
            pass
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", default="node")
    parser.add_argument("--engine", choices=("chromium", "firefox", "webkit"))
    parser.add_argument("--channel", help="Explicit installed Chromium channel, such as msedge")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("playwright_args", nargs=argparse.REMAINDER)
    options = parser.parse_args()
    suffix = uuid.uuid4().hex[:8]
    run = EVIDENCE / (time.strftime("browser-%Y%m%d-%H%M%S-") + suffix)
    run.mkdir(parents=True)
    short = ROOT / ".tmp" / ("p2br-" + suffix)
    token = secrets.token_urlsafe(32)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = private_environment(short, port, token)
    base = f"http://127.0.0.1:{port}"
    env.update({"ROW_BOT_BROWSER_BASE_URL": base, "ROW_BOT_BROWSER_EVIDENCE": str(run),
                "ROW_BOT_BROWSER_CONTROL_TOKEN": token})
    if options.engine:
        env["ROW_BOT_BROWSER_ENGINE"] = options.engine
    if options.channel:
        env["ROW_BOT_BROWSER_CHANNEL"] = options.channel
    result: dict[str, object] = {"python": sys.version, "probe_pid": os.getpid(),
                                "source_before": fingerprint(), "port": port,
                                "isolation_root": short.relative_to(ROOT).as_posix(),
                                "host": {"os": platform.platform(), "machine": platform.machine(),
                                         "cpu": platform.processor(), "logical_cpus": psutil.cpu_count(),
                                         "physical_cpus": psutil.cpu_count(logical=False),
                                         "ram_bytes": psutil.virtual_memory().total}}
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    server = None
    browser = None
    opener = build_opener(ProxyHandler({}))
    code = 1
    profile: dict[str, object] = {"samples": 0, "processes": {}, "peak_group_rss_bytes": {}}

    def sample_processes() -> None:
        """Attribute memory to this probe and its own backend/driver/browser tree."""
        sampled = [("probe", psutil.Process(os.getpid()))]
        for role, child in (("server", server), ("driver", browser)):
            if child is None or child.poll() is not None:
                continue
            try:
                owner = psutil.Process(child.pid)
                sampled.append((role, owner))
                sampled.extend(("browser" if role == "driver" else "server", process)
                               for process in owner.children(recursive=True))
            except psutil.NoSuchProcess:
                continue
        totals: dict[str, int] = {}
        for role, process in sampled:
            try:
                rss = process.memory_info().rss
                cpu = process.cpu_times()
                key = str(process.pid)
                previous = profile["processes"].get(key, {})
                profile["processes"][key] = {"role": role, "name": process.name(),
                    "peak_rss_bytes": max(rss, previous.get("peak_rss_bytes", 0)),
                    "cpu_user_seconds": cpu.user, "cpu_system_seconds": cpu.system}
                totals[role] = totals.get(role, 0) + rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        for role, total in totals.items():
            profile["peak_group_rss_bytes"][role] = max(total, profile["peak_group_rss_bytes"].get(role, 0))
        profile["samples"] += 1
    try:
        with (run / "server.log").open("w", encoding="utf-8") as output:
            started = time.perf_counter()
            server = subprocess.Popen(
                [sys.executable, str(ROOT / "tests/browser/client_platform/fixture_app.py")],
                cwd=ROOT, env=env, stdout=output, stderr=subprocess.STDOUT, creationflags=flags,
            )
            result["launcher_pid"] = server.pid
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                if server.poll() is not None:
                    raise RuntimeError("Private fixture exited before readiness")
                try:
                    with opener.open(base + "/readyz", timeout=1) as response:
                        if response.status == 200:
                            break
                except OSError:
                    time.sleep(0.1)
            else:
                raise RuntimeError("Private fixture readiness timed out")
            result["server_readiness_ms"] = (time.perf_counter() - started) * 1000
            processes = [psutil.Process(server.pid), *psutil.Process(server.pid).children(recursive=True)]
            listeners = [p for p in processes if any(
                c.status == psutil.CONN_LISTEN and c.laddr.port == port for c in p.net_connections(kind="tcp"))]
            if len(listeners) != 1:
                raise RuntimeError("Cannot identify the exact owned listening process")
            result["server_pid"] = listeners[0].pid
            result["server_rss_ready_bytes"] = listeners[0].memory_info().rss
            args = options.playwright_args
            if args[:1] == ["--"]:
                args = args[1:]
            argv = [options.node, str(ROOT / "frontend/node_modules/@playwright/test/cli.js"), "test", *args]
            result["playwright_argv"] = argv
            with (run / "playwright.log").open("w", encoding="utf-8") as test_output:
                browser = subprocess.Popen(argv, cwd=ROOT / "frontend", env=env,
                    stdout=test_output, stderr=subprocess.STDOUT, creationflags=flags)
                test_deadline = time.monotonic() + options.timeout
                while browser.poll() is None:
                    sample_processes()
                    if time.monotonic() >= test_deadline:
                        raise RuntimeError("Private browser run timed out")
                    try:
                        code = browser.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        continue
                code = browser.returncode
    except Exception as error:
        result["error"] = str(error)
    finally:
        if browser is not None:
            stop_owned(browser)
        if server is not None:
            stop_owned(server)
        result["exit_code"] = code
        result["process_profile"] = profile
        result["source_after"] = fingerprint()
        result["source_stable"] = result["source_before"] == result["source_after"]
        result["validation_status"] = ("passed" if code == 0 and result["source_stable"]
                                       else "source_changed" if code == 0 else "failed")
        for log in run.glob("*.log"):
            raw = log.read_text(encoding="utf-8", errors="replace").replace(token, "<fixture-control>")
            raw = raw.replace(str(ROOT), "<checkout>")
            raw = re.sub(r"[A-Za-z]:[\\/](?:Users|users)[\\/][^\s\"<>]+", "<profile-path>", raw)
            log.write_text(raw, encoding="utf-8")
        (run / "results.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(f"Browser result: {code}; evidence: {run.relative_to(ROOT).as_posix()}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
