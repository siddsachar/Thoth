"""Designer — publish self-contained HTML decks as static links."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import pathlib

from row_bot.designer.export import build_html_export
from row_bot.designer.preview import (
    INTERACTIVE_MODES,
    get_preview_chrome,
    render_multi_route_html,
)
from row_bot.designer.state import DesignerProject
from row_bot.designer.storage import DESIGNER_DIR, save_project
from row_bot.app_port import get_app_port
from row_bot.tunnel import tunnel_manager

logger = logging.getLogger(__name__)

PUBLISHED_DIR = DESIGNER_DIR / "published"


def ensure_published_dir() -> pathlib.Path:
    """Create and return the published deck directory."""
    PUBLISHED_DIR.mkdir(parents=True, exist_ok=True)
    return PUBLISHED_DIR


def resolve_publish_path(project: DesignerProject) -> pathlib.Path:
    """Return the stable static-file path for a published project."""
    return ensure_published_dir() / f"{project.id}.html"


def delete_published_project(project_id: str) -> bool:
    """Remove a project's local published HTML without stopping shared tunnels."""

    from row_bot.thread_cleanup import resolve_managed_path

    path = resolve_managed_path(PUBLISHED_DIR, f"{str(project_id or '')}.html")
    if not path.exists():
        return False
    path.unlink()
    return True


def resolve_publish_base_url(ensure_public: bool = True) -> tuple[str, bool]:
    """Return the base URL for published links and whether it is public."""
    app_port = get_app_port()
    public_url = tunnel_manager.get_url(app_port)
    if ensure_public and not public_url and tunnel_manager.is_available():
        try:
            public_url = tunnel_manager.start_tunnel(app_port, label="designer publish")
        except Exception:
            logger.warning("Could not open a public tunnel for designer publishing", exc_info=True)
    if public_url:
        return public_url.rstrip("/"), True
    return f"http://127.0.0.1:{app_port}", False


def build_publish_bytes(project: DesignerProject, pages: str | None = None) -> bytes:
    """Render the publishable HTML bytes for a project.

    Interactive modes (landing / app_mockup / storyboard) use the
    multi-route renderer so the published page carries the runtime
    bridge and behaves like the editor preview. Deck / document modes
    go through the classic export pipeline (with page-range support).
    """
    mode = getattr(project, "mode", "deck")
    if mode in INTERACTIVE_MODES:
        html = render_multi_route_html(project)
        # Guarantee the published document declares UTF-8 at the top of
        # <head> — the static file server does not attach a charset to
        # the Content-Type, so browsers fall back to Windows-1252 and
        # every emoji renders as mojibake ("ðŸ'") if <meta charset>
        # is missing.
        if "<meta charset" not in html.lower():
            if "<head>" in html:
                html = html.replace("<head>", "<head><meta charset=\"utf-8\">", 1)
            else:
                html = "<meta charset=\"utf-8\">" + html
        # Phase 2.2 — app_mockup should publish inside the same phone
        # bezel that the editor preview uses, so the prototype looks
        # like a phone on a desktop browser rather than a full-width web
        # page.
        chrome = get_preview_chrome(project)
        if chrome.get("kind") == "phone":
            html = _wrap_in_phone_bezel(html, chrome, project)
        return html.encode("utf-8")
    return build_html_export(project, pages)


def _wrap_in_phone_bezel(html: str, chrome: dict, project: DesignerProject) -> str:
    """Wrap a published app_mockup document in a phone bezel shell.

    The original document stays unchanged inside an iframe; we layer a
    simple host page around it that paints the bezel. Using an iframe
    keeps the published page's CSS isolated from the host styles so the
    runtime bridge, route host, and mockup CSS keep working exactly as
    they did in the preview.
    """
    import base64

    cw = int(getattr(project, "canvas_width", 390) or 390)
    ch = int(getattr(project, "canvas_height", 844) or 844)
    # The inner document must advertise UTF-8 so the browser decodes
    # the base64 payload correctly. ``build_publish_bytes`` already
    # guarantees this, but we defend against direct callers too.
    if "<meta charset" not in html.lower():
        if "<head>" in html:
            html = html.replace("<head>", "<head><meta charset=\"utf-8\">", 1)
        else:
            html = "<meta charset=\"utf-8\">" + html
    b64 = base64.b64encode(html.encode("utf-8")).decode("ascii")
    bezel_style = chrome.get("bezel_style", "")
    screen_style = chrome.get("screen_style", "")
    notch_style = chrome.get("notch_style", "")
    return (
        "<!DOCTYPE html><html><head>"
        "<meta charset=\"utf-8\">"
        f"<title>{project.name}</title>"
        "<style>"
        "html,body{margin:0;padding:0;background:#0B1220;color:#F8FAFC;"
        "font-family:Inter,system-ui,sans-serif;min-height:100vh;}"
        ".row-bot-stage{display:flex;align-items:center;justify-content:center;"
        "min-height:100vh;padding:40px;}"
        ".row-bot-screen{width:" + str(cw) + "px;height:" + str(ch) + "px;"
        "max-width:100%;}"
        "iframe{border:0;width:100%;height:100%;display:block;background:#000;}"
        "</style>"
        "</head><body>"
        "<div class=\"row-bot-stage\">"
        f"<div style=\"{bezel_style}\">"
        f"<div style=\"{notch_style}\"></div>"
        f"<div class=\"row-bot-screen\" style=\"{screen_style}\">"
        f"<iframe src=\"data:text/html;charset=utf-8;base64,{b64}\" "
        "allow=\"fullscreen\"></iframe>"
        "</div></div></div>"
        "</body></html>"
    )


def publish_project(
    project: DesignerProject,
    pages: str | None = None,
    *,
    ensure_public: bool = True,
) -> dict:
    """Render a self-contained HTML deck and expose it through the app's static route."""
    html_bytes = build_publish_bytes(project, pages)
    publish_path = resolve_publish_path(project)
    publish_path.write_bytes(html_bytes)

    base_url, is_public = resolve_publish_base_url(ensure_public=ensure_public)
    url = f"{base_url}/published/{publish_path.name}"

    project.publish_url = url
    project.published_at = datetime.now(timezone.utc).isoformat()
    save_project(project)

    return {
        "url": url,
        "path": str(publish_path),
        "public": is_public,
        "pages": pages or "all",
        "mode": getattr(project, "mode", "deck"),
    }
