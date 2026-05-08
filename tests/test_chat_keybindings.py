from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_chat_inputs_do_not_use_nicegui_exact_enter_modifier():
    """NiceGUI treats ``exact`` as a key in this app's version, not a modifier."""
    for path in ("ui/chat.py", "ui/chat_components.py"):
        src = _source(path)
        assert "keydown.enter.exact.prevent" not in src


def test_main_and_designer_chat_allow_modified_enter():
    for path in ("ui/chat.py", "ui/chat_components.py", "designer/editor.py"):
        src = _source(path)
        assert "keydown.enter" in src
        assert "e.shiftKey || e.ctrlKey || e.metaKey || e.altKey" in src
        assert "e.preventDefault();" in src
        assert "emit();" in src
