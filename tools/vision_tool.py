"""Vision tool — let the agent see through the user's camera or screen.

The agent calls ``analyze_image`` when it needs to look at something
the user is showing to the camera, read their screen, or answer a
visual question.  The tool captures a frame from the webcam or a
screenshot of the primary monitor, sends it to a lightweight vision
model (e.g. gemma3:4b) via Ollama, and returns the description.
"""

from __future__ import annotations

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from tools.base import BaseTool
from tools import registry

# The VisionService singleton is created in app.py and stored in
# st.session_state.  We import capture + analyze at call-time so the
# tool works outside of Streamlit too (falls back to a local instance).
_vision_service = None


def set_vision_service(svc):
    """Called by app.py to inject the shared VisionService instance."""
    global _vision_service
    _vision_service = svc


def _get_vision_service():
    global _vision_service
    if _vision_service is None:
        from vision import VisionService
        _vision_service = VisionService()
    return _vision_service


# ── Tool implementation ──────────────────────────────────────────────────────

def _analyze_image(question: str, source: str = "camera") -> str:
    """Capture an image from the user's camera or screen and analyze it."""
    svc = _get_vision_service()
    return svc.capture_and_analyze(question, source=source)


# ── Registration ─────────────────────────────────────────────────────────────

class VisionTool(BaseTool):

    @property
    def name(self) -> str:
        return "vision"

    @property
    def display_name(self) -> str:
        return "👁️ Vision"

    @property
    def description(self) -> str:
        return (
            "See through the user's camera or capture their screen to describe "
            "what's visible, read text from printed documents or on-screen content, "
            "identify objects, and answer visual questions."
        )

    @property
    def enabled_by_default(self) -> bool:
        return True

    def as_langchain_tools(self) -> list:

        class _AnalyzeInput(BaseModel):
            question: str = Field(
                description=(
                    "The question about what the camera or screen shows. "
                    "Be specific — e.g. 'What text is on the paper?', "
                    "'Describe the object in front of the camera', "
                    "'What error is on the screen?'."
                )
            )
            source: str = Field(
                default="camera",
                description=(
                    "Where to capture the image from. Use 'camera' when the "
                    "user asks you to look at something physical (an object, "
                    "a document, themselves). Use 'screen' when the user asks "
                    "about their screen, monitor, display, or something "
                    "shown on their computer."
                ),
            )

        return [
            StructuredTool.from_function(
                func=_analyze_image,
                name="analyze_image",
                description=(
                    "Capture an image from the user's webcam or their screen "
                    "and analyze it. Use this when the user asks you to look "
                    "at something, read text from a document, screen, or image, "
                    "identify an object, or answer any visual question. "
                    "Set source='screen' when the user refers to their screen, "
                    "monitor, or display; otherwise default to source='camera'."
                ),
                args_schema=_AnalyzeInput,
            ),
        ]

    def execute(self, query: str) -> str:
        return _analyze_image(query, source="camera")


registry.register(VisionTool())
