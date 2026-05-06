"""Buddy desktop overlay helpers."""

from __future__ import annotations


def overlay_url(port: int) -> str:
    return f"http://127.0.0.1:{int(port)}/buddy-overlay"


def open_buddy_overlay(port: int, *, width: int = 260, height: int = 260) -> bool:
    try:
        import webview
        from launcher import _JS_API, _NAMED_WINDOWS
    except Exception:
        return False

    url = overlay_url(port)
    existing = _NAMED_WINDOWS.get("buddy")
    if existing is not None:
        try:
            existing.load_url(url)
            existing.show()
            return True
        except Exception:
            _NAMED_WINDOWS.pop("buddy", None)

    kwargs = {
        "title": "Buddy",
        "url": url,
        "width": int(width),
        "height": int(height),
        "js_api": _JS_API,
    }
    for key, value in {"frameless": True, "on_top": True, "easy_drag": True}.items():
        kwargs[key] = value
    try:
        window = webview.create_window(**kwargs)
    except TypeError:
        kwargs.pop("on_top", None)
        kwargs.pop("easy_drag", None)
        window = webview.create_window(**kwargs)
    _NAMED_WINDOWS["buddy"] = window
    try:
        window.events.closed += lambda *_args: _NAMED_WINDOWS.pop("buddy", None)
    except Exception:
        pass
    return True


def close_buddy_overlay() -> bool:
    try:
        from launcher import _NAMED_WINDOWS
    except Exception:
        return False
    window = _NAMED_WINDOWS.pop("buddy", None)
    if window is None:
        return False
    try:
        window.destroy()
    except Exception:
        return False
    return True
