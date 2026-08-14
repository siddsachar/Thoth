"""Verify that required runtime dependency groups import successfully."""

from __future__ import annotations

import importlib
import importlib.util
import os
import platform
import sys


MACOS_APPKIT_MODULES = (
    "AppKit",
    "Foundation",
    "objc",
    "PyObjCTools.AppHelper",
) if platform.system() == "Darwin" else ()

FUNASR_VOICE_MODULES = () if (
    platform.system() == "Darwin"
    and platform.machine().lower() in {"amd64", "x86_64"}
) else (
    "funasr",
    "modelscope",
    "torch",
    "torchaudio",
)

GROUPS = {
    "core": (
        "nicegui",
        "fastapi",
        "starlette",
        "ollama",
        "langchain",
        "langchain_core",
        "langchain_classic",
        "langchain_community",
        "langchain_google_community",
        "langchain_ollama",
        "langchain_openai",
        "langchain_anthropic",
        "langchain_google_genai",
        "langchain_openrouter",
        "langchain_xai",
        "langchain_text_splitters",
        "langgraph",
        "langgraph.checkpoint.sqlite",
        "tiktoken",
        "openai",
        "anthropic",
        "google.genai",
        "tavily",
        "wikipedia",
        "arxiv",
        "duckduckgo_search",
        "ddgs",
        "networkx",
        "simpleeval",
        "apscheduler",
        "plyer",
        "numpy",
        "pydantic",
        "requests",
        "httpx",
        "yaml",
        "bs4",
        "keyring",
        "wolframalpha",
        "psutil",
        "pystray",
        "PIL",
        "qrcode",
        "webview",
        *MACOS_APPKIT_MODULES,
    ),
    "voice": (
        "sounddevice",
        "faster_whisper",
        "kokoro_onnx",
        *FUNASR_VOICE_MODULES,
    ),
    "designer": (
        "fpdf",
        "pptx",
        "docx",
        "pandas",
        "plotly",
        "kaleido",
    ),
    "browser": (
        "playwright.sync_api",
    ),
    "channels": (
        "telegram",
        "slack_bolt",
        "twilio",
        "discord",
        "pyngrok",
    ),
    "mcp": (
        "mcp",
        "langchain_mcp_adapters",
    ),
    "developer": (
        "winpty",
    ) if platform.system() == "Windows" else (),
    "local-embeddings": (
        "faiss",
        "sentence_transformers",
        "huggingface_hub",
        "langchain_huggingface",
        "transformers",
        "tokenizers",
        "einops",
        "torch",
    ),
    "media": (
        "cv2",
        "mss",
        "youtube_search",
        "youtube_transcript_api",
        "pypdf",
        "pandas",
        "openpyxl",
        "xlrd",
        "plotly",
        "kaleido",
    ),
}

ALIASES = {
    "embeddings": ("local-embeddings",),
    "providers": ("core",),
    "tools": ("media", "browser", "designer"),
    "youtube": ("media",),
}

INSTALL_HINTS = {
    "core": "uv sync --locked or pip install -e .",
    "voice": "uv sync --locked --extra voice or pip install -e .[voice]",
    "designer": "uv sync --locked --extra designer or pip install -e .[designer]",
    "browser": "uv sync --locked --extra browser or pip install -e .[browser]",
    "channels": "uv sync --locked --extra channels or pip install -e .[channels]",
    "mcp": "uv sync --locked --extra mcp or pip install -e .[mcp]",
    "developer": "uv sync --locked --extra developer or pip install -e .[developer]",
    "local-embeddings": (
        "uv sync --locked --extra local-embeddings or pip install -e .[local-embeddings]"
    ),
    "media": "uv sync --locked --extra media or pip install -e .[media]",
    "all": "uv sync --locked --all-extras or pip install -e .[all]",
}

HEADLESS_LINUX_PRESENCE_CHECKS = {
    "pystray",
}


def _expand_groups(groups: list[str]) -> list[str]:
    expanded: list[str] = []
    for group in groups:
        if group == "all":
            expanded.extend(GROUPS)
        elif group in ALIASES:
            expanded.extend(ALIASES[group])
        else:
            expanded.append(group)
    return list(dict.fromkeys(expanded))


def _is_headless_linux() -> bool:
    return (
        platform.system() == "Linux"
        and not os.environ.get("DISPLAY")
        and not os.environ.get("WAYLAND_DISPLAY")
    )


def _verify_module(module: str) -> None:
    if module in HEADLESS_LINUX_PRESENCE_CHECKS and _is_headless_linux():
        if importlib.util.find_spec(module) is None:
            raise ModuleNotFoundError(f"No module named '{module}'")
        return
    importlib.import_module(module)


def main(argv: list[str]) -> int:
    requested_groups = argv or ["all"]
    expanded_groups = _expand_groups(requested_groups)
    known_names = set(GROUPS) | set(ALIASES) | {"all"}
    unknown = [name for name in requested_groups if name not in known_names]
    if unknown:
        print(
            f"Unknown dependency group(s): {', '.join(unknown)}. "
            f"Known groups: {', '.join(sorted(known_names))}",
            file=sys.stderr,
        )
        return 2

    failed: list[str] = []
    for group in expanded_groups:
        for module in GROUPS[group]:
            try:
                _verify_module(module)
            except Exception as exc:
                failed.append(
                    f"{group}:{module}: {type(exc).__name__}: {exc} "
                    f"(install hint: {INSTALL_HINTS.get(group, INSTALL_HINTS['all'])})"
                )

    if failed:
        print("Runtime dependency verification failed:", file=sys.stderr)
        for item in failed:
            print(f"  - {item}", file=sys.stderr)
        return 1

    total_imports = sum(len(GROUPS[group]) for group in expanded_groups)
    alias_note = ""
    if requested_groups != expanded_groups:
        alias_note = f" (expanded from: {', '.join(requested_groups)})"
    print(
        "Runtime dependency verification passed: "
        f"{', '.join(expanded_groups)}{alias_note} "
        f"({total_imports} imports checked)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
