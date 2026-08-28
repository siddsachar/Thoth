"""Capture and validate real Row-Bot UI screenshots with Playwright."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import yaml
from PIL import Image, ImageStat


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


MANIFEST = ROOT / "docs-content" / "metadata" / "screenshots.yml"
POLICIES = ROOT / "docs-content" / "metadata" / "screenshot_policies.yml"
OUTPUT_ROOT = ROOT / "docs-site" / "static" / "img" / "screenshots" / "real-ui"
RAW_OUTPUT_ROOT = ROOT / "docs-build" / "reports" / "real-data-screenshots"
REPORT = ROOT / "docs-build" / "reports" / "screenshots.json"
REVIEW_REPORT = ROOT / "docs-build" / "reports" / "public-docs-screenshot-review.md"
DOM_ROOT = ROOT / "docs-build" / "reports" / "real-ui-dom"
LOG_ROOT = ROOT / "docs-build" / "logs"

VIEWPORTS = {
    "desktop": {"width": 3840, "height": 2160},
    "wide": {"width": 3840, "height": 2160},
    "mobile": {"width": 390, "height": 844},
}

MINIMUM_VIEWPORTS = {
    "desktop": {"width": 1280, "height": 720},
    "wide": {"width": 1280, "height": 720},
    "mobile": {"width": 320, "height": 568},
}

SECRET_PATTERNS = [
    "sk-",
    "ghp_",
    "xoxb-",
    "AIza",
    "BEGIN PRIVATE KEY",
    "C:\\Users\\",
    "/Users/",
]
LAUNCH_SECRET_ENV = "ROW_BOT_LAUNCH_SECRET"
SCREENSHOT_POLICY_FIELDS = frozenset(
    {"capture_policy", "dimension_policy", "curated_sha256"}
)


def _is_hand_curated(shot: dict[str, Any]) -> bool:
    return str(shot.get("capture_policy") or "automated") == "hand-curated"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_screenshot_policies() -> dict[str, Any]:
    data = yaml.safe_load(POLICIES.read_text(encoding="utf-8")) or {}
    screenshots = data.get("screenshots", {})
    return screenshots if isinstance(screenshots, dict) else {}


def _apply_screenshot_policies(
    manifest: dict[str, Any],
    policies: dict[str, Any] | None = None,
) -> dict[str, Any]:
    selected_policies = _load_screenshot_policies() if policies is None else policies
    merged: dict[str, Any] = {}
    for shot_id, shot in manifest.items():
        if not isinstance(shot, dict):
            merged[shot_id] = shot
            continue
        selected = dict(shot)
        policy = selected_policies.get(shot_id, {})
        if isinstance(policy, dict):
            selected.update(
                {key: policy[key] for key in SCREENSHOT_POLICY_FIELDS if key in policy}
            )
        merged[shot_id] = selected
    return merged


def _load_manifest() -> dict[str, Any]:
    data = yaml.safe_load(MANIFEST.read_text(encoding="utf-8")) or {}
    screenshots = data.get("screenshots", {})
    if not isinstance(screenshots, dict):
        return {}
    return _apply_screenshot_policies(screenshots)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _wait_ping(port: int, proc: subprocess.Popen, timeout: float, *, launcher_secret: str) -> None:
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/api/launcher-ping"
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {launcher_secret}"},
    )
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"app exited during startup with code {proc.returncode}")
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                body = response.read(512).decode("utf-8", errors="replace")
            if response.status == 200 and "row-bot" in body.lower():
                return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError(f"/api/launcher-ping did not respond on port {port}")


def _managed_chromium() -> str:
    try:
        from row_bot.mcp_client.requirements import playwright_browser_executable_path

        path = Path(playwright_browser_executable_path())
        if path.is_file():
            return str(path)
    except Exception:
        pass
    for candidate in (
        Path(os.environ.get("PROGRAMFILES", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(os.environ.get("PROGRAMFILES", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
        Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
        Path("C:/Program Files/Microsoft/Edge/Application/msedge.exe"),
        Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
    ):
        if candidate.is_file():
            return str(candidate)
    return ""


def _real_user_data_dir() -> Path:
    from row_bot.brand import default_data_dir

    return default_data_dir().expanduser().resolve()


def _browser_type(pw):
    launch_options: dict[str, Any] = {"headless": True}
    executable = _managed_chromium()
    if executable:
        launch_options["executable_path"] = executable
    try:
        return pw.chromium.launch(**launch_options)
    except Exception:
        if executable:
            return pw.chromium.launch(headless=True)
        raise


def _seed(data_dir: Path, scenario: str) -> None:
    subprocess.run(
        [
            sys.executable,
            "scripts/docs/seed_real_app_demo_data.py",
            "--data-dir",
            str(data_dir),
            "--scenario",
            scenario,
        ],
        cwd=str(ROOT),
        check=True,
    )


def _launch_app(
    port: int,
    data_dir: Path,
    stack: ExitStack,
    *,
    real_data: bool = False,
) -> tuple[subprocess.Popen, str]:
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    stdout_path = LOG_ROOT / "docs_capture_stdout.log"
    stderr_path = LOG_ROOT / "docs_capture_stderr.log"
    stdout_file = stack.enter_context(stdout_path.open("w", encoding="utf-8"))
    stderr_file = stack.enter_context(stderr_path.open("w", encoding="utf-8"))
    launcher_secret = secrets.token_urlsafe(32)
    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": os.pathsep.join([str(SRC), str(ROOT), os.environ.get("PYTHONPATH", "")]),
        "ROW_BOT_PORT": str(port),
        "ROW_BOT_HOST": "127.0.0.1",
        "ROW_BOT_DATA_DIR": str(data_dir),
        "ROW_BOT_DOCS_CAPTURE": "1",
        "ROW_BOT_DOCS_FIXED_NOW": "2026-06-18T09:00:00Z",
        "ROW_BOT_DOCS_DISABLE_NETWORK": "1",
        "ROW_BOT_DOCS_DISABLE_AUTOSTART": "1",
        "ROW_BOT_DOCS_REDUCE_MOTION": "1",
        # Fake choices are safe only with the isolated demo profile. Exposing
        # them while an explicitly authorized real profile is mounted can
        # make UI select hydration look like a user model change.
        "ROW_BOT_DOCS_FAKE_PROVIDERS": "0" if real_data else "1",
        "ROW_BOT_DOCS_REAL_DATA": "1" if real_data else "0",
        LAUNCH_SECRET_ENV: launcher_secret,
    }
    proc = subprocess.Popen(
        [sys.executable, "app.py"],
        cwd=str(ROOT),
        env=env,
        stdout=stdout_file,
        stderr=stderr_file,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return proc, launcher_secret


def _validate_image(path: Path, shot: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not path.exists():
        return [f"missing image {path}"]
    if path.stat().st_size < 8_000:
        errors.append("image file is unexpectedly small")
    try:
        with Image.open(path) as image:
            width, height = image.size
            viewport_name = str(shot.get("viewport") or "desktop")
            expected = VIEWPORTS.get(viewport_name, VIEWPORTS["desktop"])
            expected_size = (int(expected["width"]), int(expected["height"]))
            dimension_policy = str(shot.get("dimension_policy") or "viewport")
            minimum = MINIMUM_VIEWPORTS.get(viewport_name, MINIMUM_VIEWPORTS["desktop"])
            minimum_size = (int(minimum["width"]), int(minimum["height"]))
            if dimension_policy == "flexible":
                if width < minimum_size[0] or height < minimum_size[1]:
                    errors.append(
                        f"image dimensions {width}x{height} are below the {viewport_name} "
                        f"minimum {minimum_size[0]}x{minimum_size[1]}"
                    )
            elif dimension_policy != "viewport":
                errors.append(f"unknown dimension policy {dimension_policy}")
            elif (width, height) != expected_size:
                errors.append(
                    f"image dimensions {width}x{height} do not match {viewport_name} "
                    f"standard {expected_size[0]}x{expected_size[1]}"
                )
            variance = sum(ImageStat.Stat(image.convert("RGB")).var) / 3
            if variance < 12:
                errors.append(f"image appears blank or flat (variance {variance:.2f})")
    except Exception as exc:
        errors.append(f"could not inspect image: {exc}")
    expected_sha256 = str(shot.get("curated_sha256") or "").strip().lower()
    if expected_sha256 and _sha256(path) != expected_sha256:
        errors.append("hand-curated image does not match its approved SHA-256")
    combined = " ".join(str(shot.get(key, "")) for key in ("id", "title", "output", "alt", "route"))
    for pattern in SECRET_PATTERNS:
        if pattern.lower() in combined.lower():
            errors.append(f"metadata contains blocked pattern {pattern}")
    return errors


def _publish_capture(source: Path, destination: Path) -> None:
    """Publish one rendered capture without rewriting an in-use asset in place."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    staged = destination.with_name(
        f".{destination.name}.{secrets.token_hex(6)}.tmp"
    )
    try:
        shutil.copyfile(source, staged)
        os.replace(staged, destination)
    finally:
        staged.unlink(missing_ok=True)


def _write_dom_snapshot(page, shot_id: str, shot: dict[str, Any], selector: str) -> None:
    DOM_ROOT.mkdir(parents=True, exist_ok=True)
    text = ""
    html = ""
    try:
        locator = page.locator(selector).first
        text = locator.inner_text(timeout=2_000)
        html = locator.evaluate("(el) => el.outerHTML.slice(0, 20000)", timeout=2_000)
    except Exception as exc:
        text = f"DOM snapshot failed: {exc}"
    try:
        viewport_state = page.evaluate(
            """() => ({
                scrollX: window.scrollX,
                scrollY: window.scrollY,
                scrolling: Array.from(document.querySelectorAll('*'))
                    .filter(el => el.scrollTop || el.scrollLeft)
                    .slice(0, 20)
                    .map(el => ({
                        tag: el.tagName,
                        id: el.id || '',
                        className: typeof el.className === 'string' ? el.className.slice(0, 240) : '',
                        scrollTop: el.scrollTop,
                        scrollLeft: el.scrollLeft,
                        clientHeight: el.clientHeight,
                        scrollHeight: el.scrollHeight,
                    })),
                probes: ['[data-docs-id="settings-dialog"] .text-h5',
                         '[data-docs-id="settings-dialog"] .nicegui-row',
                         '[data-docs-id="settings-dialog"] .q-tab--active']
                    .map(selector => {
                        const el = document.querySelector(selector);
                        if (!el) return {selector, missing: true};
                        const rect = el.getBoundingClientRect();
                        const style = getComputedStyle(el);
                        return {selector, text: (el.innerText || '').slice(0, 120),
                                rect: {x: rect.x, y: rect.y, width: rect.width, height: rect.height},
                                display: style.display, visibility: style.visibility,
                                opacity: style.opacity, color: style.color, zIndex: style.zIndex};
                    }),
            })"""
        )
    except Exception:
        viewport_state = {}
    payload = {
        "id": shot_id,
        "route": shot.get("route", "/"),
        "selector": selector,
        "text": text,
        "html_excerpt": html,
        "viewport_state": viewport_state,
    }
    (DOM_ROOT / f"{shot_id}.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _wait_for_text(page, text: str, timeout: int = 15_000) -> None:
    try:
        page.wait_for_function(
            "(needle) => document.body && document.body.innerText && document.body.innerText.includes(needle)",
            arg=text,
            timeout=timeout,
        )
    except Exception as exc:
        raise RuntimeError(f"Timed out waiting for visible text: {text}") from exc


def _run_action(page, action: dict[str, Any], base_url: str) -> None:
    if "goto" in action:
        page.goto(base_url + str(action["goto"]), wait_until="networkidle", timeout=30_000)
    elif "wait_for_selector" in action:
        page.wait_for_selector(str(action["wait_for_selector"]), timeout=15_000)
    elif "wait_for_text" in action:
        _wait_for_text(page, str(action["wait_for_text"]))
    elif "click_selector" in action:
        page.locator(str(action["click_selector"])).first.click(timeout=15_000)
    elif "click_text" in action:
        page.get_by_text(str(action["click_text"]), exact=False).first.click(timeout=15_000)
    elif "fill" in action:
        target = action["fill"] if isinstance(action["fill"], dict) else {}
        page.locator(str(target.get("selector") or "")).first.fill(str(target.get("value") or ""))
    elif "press" in action:
        target = action["press"] if isinstance(action["press"], dict) else {}
        page.locator(str(target.get("selector") or "body")).first.press(str(target.get("key") or "Enter"))
    elif "open_settings_tab" in action:
        page.goto(base_url + f"/?settings_tab={action['open_settings_tab']}", wait_until="networkidle", timeout=30_000)
    elif "open_home_tab" in action:
        page.goto(base_url + f"/?home_tab={action['open_home_tab']}", wait_until="networkidle", timeout=30_000)
    elif "open_dialog" in action:
        page.goto(base_url + f"/?dialog={action['open_dialog']}", wait_until="networkidle", timeout=30_000)
    elif "expand" in action:
        page.get_by_text(str(action["expand"]), exact=False).first.click(timeout=15_000)
    elif "scroll_into_view" in action:
        page.locator(str(action["scroll_into_view"])).first.scroll_into_view_if_needed(timeout=15_000)
    elif "screenshot" in action or "dom_snapshot" in action:
        return
    else:
        raise ValueError(f"Unsupported screenshot action: {action}")


def _capture_one(
    browser,
    port: int,
    shot_id: str,
    shot: dict[str, Any],
    *,
    real_data: bool = False,
) -> dict[str, Any]:
    if _is_hand_curated(shot):
        return _preserved_record(shot_id, shot)
    viewport = VIEWPORTS.get(str(shot.get("viewport") or "desktop"), VIEWPORTS["desktop"])
    page = browser.new_page(viewport=viewport)
    base_url = f"http://127.0.0.1:{port}"
    route = str(shot.get("route") or "/")
    output = OUTPUT_ROOT / str(shot.get("output") or f"{shot_id}.png")
    raw_output = RAW_OUTPUT_ROOT / str(shot.get("output") or f"{shot_id}.png")
    errors: list[str] = []
    try:
        if "/docs-mode/" in route:
            raise RuntimeError("fake docs-mode screenshot routes are forbidden")
        page.goto(base_url + route, wait_until="networkidle", timeout=30_000)
        for action in shot.get("actions") or []:
            if isinstance(action, dict):
                _run_action(page, action, base_url)
        wait_for = str(shot.get("wait_for") or shot.get("capture_selector") or "[data-docs-id=\"app-shell\"]")
        page.wait_for_selector(wait_for, timeout=20_000)
        for text in shot.get("expected_text") or []:
            _wait_for_text(page, str(text))
        selector = str(shot.get("capture_selector") or wait_for)
        # Selecting a lower settings tab can let the browser scroll the outer
        # document while bringing the active tab into view. Public captures
        # should always include the application and dialog headers.
        reset_outer_scroll = (
            "() => { const active = document.activeElement; if (active && active.blur) active.blur(); "
            "window.scrollTo(0, 0); document.documentElement.scrollTop = 0; document.body.scrollTop = 0; }"
        )
        page.evaluate(reset_outer_scroll)
        page.wait_for_timeout(250)
        page.evaluate(reset_outer_scroll)
        _write_dom_snapshot(page, shot_id, shot, selector)
        masks = []
        for mask_selector in shot.get("masks") or []:
            try:
                masks.extend(page.locator(str(mask_selector)).all())
            except Exception:
                pass
        raw_output.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(
            path=str(raw_output),
            animations="disabled",
            full_page=False,
            mask=masks,
        )
        validation_output = raw_output
        if not real_data:
            _publish_capture(raw_output, output)
            validation_output = output
        errors.extend(_validate_image(validation_output, {"id": shot_id, **shot}))
    except Exception as exc:
        # Preserve enough evidence to diagnose a failed public capture without
        # having to rerun it interactively. These files stay under docs-build.
        try:
            selector = str(shot.get("capture_selector") or "body")
            _write_dom_snapshot(page, f"{shot_id}-failed", shot, selector)
            diagnostic = REPORT.parent / "failed-screenshots" / f"{shot_id}.png"
            diagnostic.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(diagnostic), animations="disabled", full_page=False)
        except Exception:
            pass
        errors.append(str(exc))
    finally:
        page.close()
    return {
        "id": shot_id,
        "title": shot.get("title", shot_id),
        "status": "ok" if not errors else "failed",
        "deferred": False,
        "review_status": shot.get("review_status", "needs-review"),
        "source": shot.get("source", ""),
        "public_asset": shot.get("public_asset", False),
        "route": route,
        "output": str(
            (raw_output if real_data else output).relative_to(ROOT)
        ).replace("\\", "/"),
        "raw_output": str(raw_output.relative_to(ROOT)).replace("\\", "/"),
        "errors": errors,
    }


def _blocked_record(shot_id: str, shot: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "id": shot_id,
        "title": shot.get("title", shot_id),
        "status": "deferred",
        "deferred": True,
        "review_status": shot.get("review_status", "needs-review"),
        "source": shot.get("source", ""),
        "public_asset": shot.get("public_asset", False),
        "reason": reason,
        "output": str((OUTPUT_ROOT / str(shot.get("output") or f"{shot_id}.png")).relative_to(ROOT)).replace("\\", "/"),
        "errors": [],
    }


def _preserved_record(shot_id: str, shot: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": shot_id,
        "title": shot.get("title", shot_id),
        "status": "ok",
        "deferred": False,
        "preserved": True,
        "review_status": shot.get("review_status", "needs-review"),
        "source": shot.get("source", ""),
        "public_asset": shot.get("public_asset", False),
        "reason": "hand-curated asset preserved",
        "output": str(
            (OUTPUT_ROOT / str(shot.get("output") or f"{shot_id}.png")).relative_to(ROOT)
        ).replace("\\", "/"),
        "errors": [],
    }


def _write_report(records: list[dict[str, Any]], *, mode: str) -> dict[str, Any]:
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    summary = {
        "mode": mode,
        "total": len(records),
        "captured": sum(
            1
            for item in records
            if item.get("status") == "ok"
            and not item.get("deferred")
            and not item.get("preserved")
        ),
        "preserved": sum(1 for item in records if item.get("preserved")),
        "failed": sum(1 for item in records if item.get("status") == "failed"),
        "deferred": sum(1 for item in records if item.get("deferred")),
        "records": records,
    }
    REPORT.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _write_review_report(summary)
    print(f"Wrote screenshot report to {REPORT}")
    for item in records:
        if item.get("errors"):
            print(f"ERROR {item['id']}: {'; '.join(item['errors'])}")
    return summary


def _write_review_report(summary: dict[str, Any]) -> None:
    REVIEW_REPORT.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Public Docs Screenshot Review",
        "",
        "Status: manual visual review required before publishing these screenshots.",
        "",
        f"- Mode: {summary.get('mode', '')}",
        f"- Total records: {summary.get('total', 0)}",
        f"- Captured or validated: {summary.get('captured', 0)}",
        f"- Hand-curated assets preserved: {summary.get('preserved', 0)}",
        f"- Failed: {summary.get('failed', 0)}",
        f"- Deferred: {summary.get('deferred', 0)}",
        "",
        "## Screenshot Records",
        "",
        "| ID | Status | Review | Source | Public asset | Output | Notes |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in summary.get("records", []):
        notes = "; ".join(item.get("errors") or []) or str(item.get("reason") or "")
        lines.append(
            "| {id} | {status} | {review} | {source} | {public_asset} | {output} | {notes} |".format(
                id=str(item.get("id", "")),
                status=str(item.get("status", "")),
                review=str(item.get("review_status", "needs-review")),
                source=str(item.get("source", "")),
                public_asset=str(item.get("public_asset", "")),
                output=str(item.get("output", "")),
                notes=notes.replace("|", "\\|"),
            )
        )
    lines.extend(
        [
            "",
            "## Manual Gate",
            "",
            "- Review every image for private names, credentials, paths, account labels, channel names, document content, or misleading real data.",
            "- Mark screenshots as approved, replace, crop, or redact in `docs-content/metadata/screenshots.yml` after review.",
            "- Do not publish or link this docs-site from the current public site until the review gate is complete.",
        ]
    )
    REVIEW_REPORT.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def validate_committed(manifest: dict[str, Any]) -> dict[str, Any]:
    records = []
    for shot_id, shot in manifest.items():
        if not isinstance(shot, dict):
            continue
        if shot.get("status") == "deferred":
            errors = []
            if not shot.get("reason"):
                errors.append("deferred screenshot is missing reason")
            if not shot.get("follow_up"):
                errors.append("deferred screenshot is missing follow_up")
            records.append(
                {
                    "id": shot_id,
                    "status": "failed" if errors else "deferred",
                    "deferred": True,
                    "review_status": shot.get("review_status", "needs-review"),
                    "source": shot.get("source", ""),
                    "public_asset": shot.get("public_asset", False),
                    "errors": errors,
                    "output": str((OUTPUT_ROOT / str(shot.get("output") or f"{shot_id}.png")).relative_to(ROOT)).replace("\\", "/"),
                }
            )
            continue
        output = OUTPUT_ROOT / str(shot.get("output") or f"{shot_id}.png")
        errors = _validate_image(output, {"id": shot_id, **shot})
        records.append(
            {
                "id": shot_id,
                "status": "ok" if not errors else "failed",
                "deferred": False,
                "review_status": shot.get("review_status", "needs-review"),
                "source": shot.get("source", ""),
                "public_asset": shot.get("public_asset", False),
                "errors": errors,
                "output": str(output.relative_to(ROOT)).replace("\\", "/"),
            }
        )
    return _write_report(records, mode="validate")


def _filter_manifest(manifest: dict[str, Any], source_filter: str) -> dict[str, Any]:
    if not source_filter or source_filter == "all":
        return manifest
    return {
        shot_id: shot
        for shot_id, shot in manifest.items()
        if isinstance(shot, dict) and str(shot.get("source") or "") == source_filter
    }


def _filter_scenario(manifest: dict[str, Any], scenario: str) -> dict[str, Any]:
    normalized = str(scenario or "full").strip().lower()
    if normalized in {"all", "full"}:
        return manifest
    aliases = {"android": "mobile"}
    normalized = aliases.get(normalized, normalized)
    return {
        shot_id: shot
        for shot_id, shot in manifest.items()
        if isinstance(shot, dict) and str(shot.get("scenario") or "configured") == normalized
    }


def _filter_ids(manifest: dict[str, Any], screenshot_ids: set[str] | None) -> dict[str, Any]:
    if not screenshot_ids:
        return manifest
    return {shot_id: shot for shot_id, shot in manifest.items() if shot_id in screenshot_ids}


def _real_data_shot(shot: dict[str, Any]) -> dict[str, Any]:
    """Remove deterministic-demo assertions while retaining stable surface anchors."""

    selected = dict(shot)
    actions: list[dict[str, Any]] = []
    for action in shot.get("actions") or []:
        if not isinstance(action, dict) or "wait_for_text" in action:
            continue
        if str(action.get("click_text") or "") == "filesystem.search":
            actions.append({"click_selector": '[data-docs-id="tool-trace"]'})
        else:
            actions.append(dict(action))
    selected["actions"] = actions
    selected["expected_text"] = []
    return selected


def _safe_capture_data_dir(
    data_dir: Path | None,
    *,
    authorize_real_data: bool = False,
) -> tuple[Path, tempfile.TemporaryDirectory[str] | None]:
    if data_dir is None:
        temp_root = ROOT / "docs-build" / "capture-data"
        temp_root.mkdir(parents=True, exist_ok=True)
        temp_dir = tempfile.TemporaryDirectory(
            prefix="row_bot_docs_capture_",
            dir=temp_root,
            ignore_cleanup_errors=True,
        )
        return Path(temp_dir.name).resolve(), temp_dir
    resolved = data_dir.expanduser().resolve()
    if resolved == _real_user_data_dir():
        if not authorize_real_data:
            raise RuntimeError(
                "Refusing to use the normal Row-Bot data directory for documentation capture without "
                "the explicit --authorize-real-data-capture opt-in. Omit --data-dir for an isolated "
                "temporary directory or choose a dedicated demo directory."
            )
        return resolved, None
    if authorize_real_data:
        raise RuntimeError(
            "--authorize-real-data-capture is accepted only for the exact normal Row-Bot data directory."
        )
    return resolved, None


def capture(
    manifest: dict[str, Any],
    *,
    scenario: str,
    timeout: float = 90.0,
    data_dir: Path | None = None,
    seed_demo_data: bool = True,
    use_temp_data: bool = True,
    source_filter: str = "all",
    screenshot_ids: set[str] | None = None,
    authorize_real_data: bool = False,
) -> dict[str, Any]:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    RAW_OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    DOM_ROOT.mkdir(parents=True, exist_ok=True)
    if authorize_real_data and (use_temp_data or seed_demo_data):
        raise RuntimeError(
            "Authorized real-data capture requires retained data and --no-seed-demo-data."
        )
    manifest = _apply_screenshot_policies(manifest)
    manifest = _filter_manifest(manifest, source_filter)
    manifest = _filter_scenario(manifest, scenario)
    manifest = _filter_ids(manifest, screenshot_ids)
    records: list[dict[str, Any]] = [
        _preserved_record(shot_id, shot)
        for shot_id, shot in manifest.items()
        if isinstance(shot, dict) and _is_hand_curated(shot)
    ]
    manifest = {
        shot_id: shot
        for shot_id, shot in manifest.items()
        if not isinstance(shot, dict) or not _is_hand_curated(shot)
    }
    if not manifest:
        return _write_report(records, mode="capture")
    data_dir, temp_dir = _safe_capture_data_dir(
        None if use_temp_data else data_dir,
        authorize_real_data=authorize_real_data,
    )
    data_dir.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    if _port_open(port):
        raise RuntimeError(f"selected port {port} is already in use")

    proc: subprocess.Popen | None = None
    try:
        if seed_demo_data:
            _seed(data_dir, scenario)
        with ExitStack() as stack:
            proc, launcher_secret = _launch_app(
                port,
                data_dir,
                stack,
                real_data=authorize_real_data,
            )
            _wait_ping(port, proc, timeout, launcher_secret=launcher_secret)
            try:
                from playwright.sync_api import sync_playwright
            except Exception as exc:
                for shot_id, shot in manifest.items():
                    records.append(_blocked_record(shot_id, shot, f"Playwright import failed: {exc}"))
                return _write_report(records, mode="capture")
            with sync_playwright() as pw:
                try:
                    browser = _browser_type(pw)
                except Exception as exc:
                    for shot_id, shot in manifest.items():
                        records.append(_blocked_record(shot_id, shot, f"Chromium launch failed: {exc}"))
                    return _write_report(records, mode="capture")
                try:
                    for shot_id, shot in manifest.items():
                        if not isinstance(shot, dict):
                            continue
                        if shot.get("status") == "deferred":
                            records.append(_blocked_record(shot_id, shot, str(shot.get("reason") or "deferred")))
                            continue
                        runtime_shot = _real_data_shot(shot) if authorize_real_data else shot
                        records.append(
                            _capture_one(
                                browser,
                                port,
                                shot_id,
                                runtime_shot,
                                real_data=authorize_real_data,
                            )
                        )
                finally:
                    browser.close()
    finally:
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        if temp_dir is not None:
            temp_dir.cleanup()
    return _write_report(records, mode="capture")


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture or validate real Row-Bot UI screenshots")
    parser.add_argument("--scenario", default="full")
    parser.add_argument("--data-dir", default=None)
    parser.add_argument(
        "--keep-demo-data",
        action="store_true",
        help="Use --data-dir as a dedicated retained demo directory instead of an automatic temporary directory",
    )
    parser.add_argument(
        "--ids",
        nargs="*",
        default=None,
        help="Capture only the listed screenshot IDs (advanced debugging and review iteration)",
    )
    parser.add_argument(
        "--no-seed-demo-data",
        action="store_true",
        help="Skip deterministic seeding (advanced debugging only)",
    )
    parser.add_argument(
        "--source-filter",
        default="all",
        help="Capture only screenshots with this metadata source, or 'all'",
    )
    parser.add_argument(
        "--authorize-real-data-capture",
        action="store_true",
        help=(
            "Explicitly authorize capture against the exact normal Row-Bot data directory; "
            "requires --data-dir, --keep-demo-data, and --no-seed-demo-data"
        ),
    )
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--timeout", type=float, default=90.0)
    args = parser.parse_args()
    manifest = _load_manifest()
    if args.validate_only:
        summary = validate_committed(manifest)
    else:
        summary = capture(
            manifest,
            scenario=str(args.scenario or "full"),
            timeout=args.timeout,
            data_dir=Path(args.data_dir).resolve() if args.data_dir else None,
            seed_demo_data=not bool(args.no_seed_demo_data),
            use_temp_data=not bool(args.keep_demo_data),
            source_filter=str(args.source_filter or "all"),
            screenshot_ids=set(args.ids or []) or None,
            authorize_real_data=bool(args.authorize_real_data_capture),
        )
    return 1 if summary.get("failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
