"""Run real NiceGUI browser lifecycle cases with disposable fake-provider data.

This optional script uses the already-installed Playwright and Edge. It never
installs dependencies or attaches to an existing app/browser/user profile.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import sys
import time
import traceback
import uuid
from urllib.request import Request, urlopen

import psutil
from playwright.sync_api import expect, sync_playwright


ROOT = Path(__file__).resolve().parents[3]
QA = ROOT / ".local/evidence/unified-client-platform/phase-1/qa"
TOKEN = secrets.token_urlsafe(32)
RUN = QA / ("browser-" + time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6])
SHORT = ROOT / ".tmp" / ("p1br-" + uuid.uuid4().hex[:8])


def clean(value: object) -> str:
    text = str(value).replace(TOKEN, "<fixture-control>").replace(str(ROOT), "<checkout>")
    return re.sub(r"[A-Za-z]:[\\/](?:Users|users)[\\/][^\s\"<>]+", "<profile-path>", text)


def fingerprint() -> dict:
    spec = importlib.util.spec_from_file_location("phase1_focused_runner", QA / "run_focused.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.source_fingerprint()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", default="send-stream-stop,switch-reload-two-viewers,approval-pause-resume,socket-reconnect,developer-designer-entry")
    options = parser.parse_args()
    selected_cases = set(options.cases.split(","))
    RUN.mkdir(parents=True)
    SHORT.mkdir(parents=True)
    (SHORT / "system-temp").mkdir()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    env = {key: value for key, value in os.environ.items() if key.upper() in {
        "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "APPDATA", "LOCALAPPDATA",
        "USERPROFILE", "PROGRAMFILES", "PROGRAMFILES(X86)", "HOMEDRIVE", "HOMEPATH"}}
    env.update({"PYTHONPATH": os.pathsep.join([str(QA / "harness"), str(ROOT / "src"), str(ROOT)]),
                "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8",
                "ROW_BOT_DATA_DIR": str(SHORT / "data"), "ROW_BOT_WORKSPACE": str(SHORT / "workspace"),
                "ROW_BOT_HOST": "127.0.0.1", "ROW_BOT_PORT": str(port),
                "ROW_BOT_TEST_MODE": "1", "ROW_BOT_DOCS_CAPTURE": "1", "ROW_BOT_DOCS_REAL_DATA": "0",
                "ROW_BOT_DOCS_DISABLE_NETWORK": "1", "ROW_BOT_DOCS_DISABLE_AUTOSTART": "1",
                "ROW_BOT_DOCS_FAKE_PROVIDERS": "1", "ROW_BOT_DOCS_REDUCE_MOTION": "1",
                "P1_BROWSER_CONTROL_TOKEN": TOKEN, "P1_ALLOWED_PORT": str(port),
                "ROW_BOT_LAUNCH_SECRET": secrets.token_urlsafe(32),
                "HF_HOME": str(SHORT / "hf-cache"), "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
                "TEMP": str(SHORT / "system-temp"), "TMP": str(SHORT / "system-temp"),
                "GIT_CEILING_DIRECTORIES": str(SHORT), "UV_NO_SYNC": "1", "UV_OFFLINE": "1"})
    result = {"python": sys.version, "source_before": fingerprint(), "probe_pid": os.getpid(),
              "viewport": [1440, 900], "appearance": "NiceGUI existing dark/blue", "port": port,
              "records": [], "console": [], "page_errors": [], "blocked_external": [], "websocket_events": []}

    def save():
        (RUN / "results.json").write_text(json.dumps(result, indent=2), encoding="utf-8")

    def state():
        request = Request(base + "/__p1_fixture/state", headers={"X-Fixture-Token": TOKEN})
        with urlopen(request, timeout=3) as response:
            return json.load(response)

    def release(barrier_id):
        request = Request(base + "/__p1_fixture/release/" + barrier_id, data=b"", method="POST",
                          headers={"X-Fixture-Token": TOKEN, "Origin": base})
        with urlopen(request, timeout=3) as response:
            return json.load(response)

    def await_state(predicate, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snapshot = state()
            if predicate(snapshot):
                return snapshot
            # This is browser/HTTP observation polling, not a deterministic
            # contested-interleaving sleep. Provider progress uses barriers.
            time.sleep(0.05)
        raise AssertionError("Browser backend state condition timed out")

    process = None
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        with (RUN / "server.log").open("w", encoding="utf-8") as output:
            started = time.perf_counter()
            process = subprocess.Popen([sys.executable, str(Path(__file__).with_name("fixture_app.py"))],
                                       cwd=ROOT, env=env, stdout=output, stderr=subprocess.STDOUT, creationflags=flags)
            result["launcher_pid"] = process.pid
            for _ in range(120):
                if process.poll() is not None:
                    raise AssertionError("Disposable NiceGUI child exited before readiness")
                try:
                    with urlopen(base + "/readyz", timeout=1) as response:
                        if response.status == 200:
                            break
                except Exception:
                    time.sleep(0.25)
            else:
                raise AssertionError("Disposable NiceGUI readiness timed out")
            result["readiness_ms"] = round((time.perf_counter() - started) * 1000, 2)
            candidates = [psutil.Process(process.pid), *psutil.Process(process.pid).children(recursive=True)]
            result["app_processes"] = [{"pid": candidate.pid, "name": candidate.name(),
                                        "rss_mb": round(candidate.memory_info().rss / 1048576, 2)} for candidate in candidates]
            # Windows venv python.exe can be a redirector. Attribute server RSS
            # only to the verified listening-port owner, never the last child.
            listeners = [candidate for candidate in candidates
                         if any(connection.status == psutil.CONN_LISTEN and connection.laddr.port == port
                                for connection in candidate.net_connections(kind="tcp"))]
            if len(listeners) == 1:
                result["server_pid"] = listeners[0].pid
                result["server_rss_ready_mb"] = round(listeners[0].memory_info().rss / 1048576, 2)
            else:
                result["server_pid"] = None
                result["server_rss_ready_mb"] = None
                result["process_attribution_error"] = "Expected one owned listening-port process"
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel="msedge", headless=True,
                            args=["--disable-background-networking", "--disable-component-update"])
                result["browser_version"] = browser.version
                context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme="dark",
                                              reduced_motion="reduce", service_workers="block")

                def route(request):
                    if request.request.url.startswith(base + "/") or request.request.url.startswith(("data:", "blob:")):
                        request.continue_()
                    else:
                        result["blocked_external"].append(request.request.url)
                        request.abort()

                context.route("**/*", route)

                def new_page():
                    page = context.new_page()
                    page.on("console", lambda event: result["console"].append({"type": event.type, "text": clean(event.text)}))
                    page.on("pageerror", lambda error: result["page_errors"].append(clean(error)))
                    def on_socket(socket):
                        record = {"open": True, "closed": False, "frames_received": 0, "frames_sent": 0}
                        result["websocket_events"].append(record)
                        socket.on("close", lambda: record.update(closed=True))
                        socket.on("framereceived", lambda _: record.update(frames_received=record["frames_received"] + 1))
                        socket.on("framesent", lambda _: record.update(frames_sent=record["frames_sent"] + 1))
                    page.on("websocket", on_socket)
                    return page

                page = new_page()
                page.goto(base + "/?docs_surface=chat-main&thread_id=p1-browser-a", wait_until="domcontentloaded")
                page.get_by_placeholder("Do anything…").wait_for(state="visible", timeout=15000)

                def send(text):
                    field = page.get_by_placeholder("Do anything…")
                    field.fill(text)
                    field.press("Enter")

                def record_case(name, action):
                    if name not in selected_cases:
                        return
                    rec = {"id": name, "console_start": len(result["console"])}
                    result["records"].append(rec)
                    try:
                        action()
                        rec["status"] = "passed"
                    except Exception as error:
                        rec["status"] = "failed"
                        rec["error"] = clean(error)
                        rec["error_type"] = type(error).__name__
                        rec["traceback"] = clean(traceback.format_exc())
                    rec["backend"] = state()
                    rec["console_end"] = len(result["console"])
                    rec["screenshot"] = name + ".png"
                    page.screenshot(path=str(RUN / rec["screenshot"]), mask=[page.locator("[data-sensitive]"),
                                    page.get_by_text(re.compile(r"[A-Za-z]:[\\/]"))])
                    save()
                    if rec["status"] == "failed":
                        raise AssertionError(name + " failed; retained evidence")

                def stream_stop():
                    before_count = len(state()["calls"])
                    send("Phase 1 stop fixture")
                    await_state(lambda snapshot: len(snapshot["calls"]) == before_count + 1)
                    page.get_by_text("Synthetic stream is active.", exact=True).wait_for(timeout=15000)
                    page.locator(".row-bot-composer-stop-button").click()
                    final = await_state(lambda snapshot: snapshot["calls"][-1]["quiesced"])
                    assert len(final["calls"]) == before_count + 1

                record_case("send-stream-stop", stream_stop)

                def switch_reload_two_viewers():
                    before_count = len(state()["calls"])
                    send("Phase 1 recovery fixture")
                    snapshot = await_state(lambda value: len(value["calls"]) == before_count + 1)
                    barrier_id = snapshot["calls"][-1]["barrier_id"]
                    page.get_by_text("Phase 1 conversation B", exact=True).first.click()
                    assert len(state()["calls"]) == before_count + 1
                    page.get_by_text("Phase 1 conversation A", exact=True).first.click()
                    page.reload(wait_until="domcontentloaded")
                    page.get_by_placeholder("Do anything…").wait_for(timeout=15000)
                    assert len(state()["calls"]) == before_count + 1
                    viewer = new_page()
                    viewer.goto(base + "/?docs_surface=chat-main&thread_id=p1-browser-a", wait_until="domcontentloaded")
                    viewer.get_by_placeholder("Do anything…").wait_for(timeout=15000)
                    release(barrier_id)
                    await_state(lambda value: not value["legacy_generations"])
                    for observer in (page, viewer):
                        observer.get_by_text("Synthetic stream is active. Synthetic stream settled.", exact=True).wait_for(timeout=15000)
                        expect(observer.get_by_text("Synthetic stream is active. Synthetic stream settled.", exact=True)).to_have_count(1, timeout=15000)
                        expect(observer.get_by_text("Phase 1 recovery fixture", exact=True)).to_have_count(1, timeout=15000)
                    assert len(state()["calls"]) == before_count + 1
                    viewer.close()

                record_case("switch-reload-two-viewers", switch_reload_two_viewers)

                def approval():
                    before_count = len(state()["calls"])
                    send("Phase 1 approval fixture")
                    page.get_by_text("Approve the synthetic fixture action", exact=False).wait_for(timeout=15000)
                    await_state(lambda value: not value["legacy_generations"])
                    expect(page.get_by_text("Phase 1 approval fixture", exact=True)).to_have_count(1, timeout=15000)
                    page.screenshot(path=str(RUN / "approval-paused.png"), mask=[page.locator("[data-sensitive]"),
                                    page.get_by_text(re.compile(r"[A-Za-z]:[\\/]"))])
                    page.get_by_role("button", name="Approve", exact=True).click()
                    page.get_by_text("Synthetic approval resumed.", exact=True).wait_for(timeout=15000)
                    snapshot = await_state(lambda value: len(value["calls"]) == before_count + 2
                                           and value["calls"][-1]["quiesced"] and not value["legacy_generations"])
                    assert [call["kind"] for call in snapshot["calls"]][-2:] == ["submit", "resume"]
                    expect(page.get_by_text("Phase 1 approval fixture", exact=True)).to_have_count(1, timeout=15000)
                    expect(page.get_by_text("Synthetic approval resumed.", exact=True)).to_have_count(1, timeout=15000)

                record_case("approval-pause-resume", approval)

                def socket_reconnect():
                    before_count = len(state()["calls"])
                    send("Phase 1 reconnect fixture")
                    snapshot = await_state(lambda value: len(value["calls"]) == before_count + 1)
                    barrier_id = snapshot["calls"][-1]["barrier_id"]
                    draft = "Unsent synthetic reconnect draft"
                    page.get_by_placeholder("Do anything…").fill(draft)
                    page.evaluate("() => { window.socket.disconnect(); return true; }")
                    page.wait_for_function("window.socket.connected === false")
                    release(barrier_id)
                    await_state(lambda value: value["calls"][-1]["quiesced"])
                    page.evaluate("() => { window.socket.connect(); return true; }")
                    page.wait_for_function("window.socket.connected === true")
                    # The earlier recovery case has the same final text: assert
                    # exactly one extra native result after this reconnection.
                    expected_count = 2 if "switch-reload-two-viewers" in selected_cases else 1
                    await_state(lambda value: not value["legacy_generations"])
                    # NiceGUI may report an outbox rewind and navigate during
                    # socket recovery. Keep the exact assertions across that
                    # bounded reload instead of observing a transient empty DOM.
                    expect(page.get_by_text("Synthetic stream is active. Synthetic stream settled.", exact=True)).to_have_count(expected_count, timeout=15000)
                    expect(page.get_by_text("Phase 1 reconnect fixture", exact=True)).to_have_count(1, timeout=15000)
                    composer = page.get_by_placeholder("Do anything…")
                    expect(composer).to_be_visible(timeout=15000)
                    expect(composer).to_have_value(draft, timeout=15000)
                    result["reconnect_observation"] = {"draft_restored": True, "expected_final_count": expected_count,
                                                       "expected_user_count": 1, "producer_calls": len(state()["calls"])}
                    assert len(state()["calls"]) == before_count + 1
                    page.get_by_placeholder("Do anything…").fill("")

                record_case("socket-reconnect", socket_reconnect)

                def legacy_entries():
                    page.goto(base + "/?docs_surface=developer-workspace", wait_until="domcontentloaded")
                    page.locator('[data-docs-id="developer-workspace"]').wait_for(timeout=15000)
                    page.screenshot(path=str(RUN / "developer-entry.png"), mask=[page.locator("[data-sensitive]"),
                                    page.get_by_text(re.compile(r"[A-Za-z]:[\\/]"))])
                    page.goto(base + "/?docs_surface=designer-editor", wait_until="domcontentloaded")
                    page.locator('[data-docs-id="designer-editor"]').wait_for(timeout=15000)

                record_case("developer-designer-entry", legacy_entries)
                context.close()
                browser.close()
    except Exception as error:
        result["fatal"] = clean(error)
    finally:
        if process is not None and process.poll() is None:
            try:
                children = psutil.Process(process.pid).children(recursive=True)
                for child in reversed(children):
                    child.terminate()
                process.terminate()
                process.wait(timeout=10)
            except (psutil.NoSuchProcess, subprocess.TimeoutExpired):
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
        server_log = RUN / "server.log"
        if server_log.exists():
            server_log.write_text(clean(server_log.read_text(encoding="utf-8", errors="replace")), encoding="utf-8")
        result["source_after"] = fingerprint()
        result["source_stable"] = result["source_before"] == result["source_after"]
        result["console_error_count"] = sum(event["type"] == "error" for event in result["console"])
        result["page_error_count"] = len(result["page_errors"])
        save()
    print(json.dumps({"evidence": RUN.relative_to(ROOT).as_posix(), "fatal": result.get("fatal"),
                      "cases": [{"id": rec["id"], "status": rec.get("status")} for rec in result["records"]]}, indent=2))
    return 1 if result.get("fatal") or any(rec.get("status") != "passed" for rec in result["records"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
