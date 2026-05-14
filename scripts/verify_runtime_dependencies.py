"""Verify that bundled runtime dependency groups import successfully."""

from __future__ import annotations

import importlib
import sys


GROUPS = {
    "embeddings": (
        "sentence_transformers",
        "langchain_huggingface",
        "transformers",
        "torch",
    ),
    "youtube": (
        "youtube_search",
        "youtube_transcript_api",
    ),
}


def main(argv: list[str]) -> int:
    groups = argv or sorted(GROUPS)
    unknown = [name for name in groups if name not in GROUPS]
    if unknown:
        print(f"Unknown dependency group(s): {', '.join(unknown)}", file=sys.stderr)
        return 2

    failed: list[str] = []
    for group in groups:
        for module in GROUPS[group]:
            try:
                importlib.import_module(module)
            except Exception as exc:
                failed.append(f"{group}:{module}: {type(exc).__name__}: {exc}")

    if failed:
        print("Runtime dependency verification failed:", file=sys.stderr)
        for item in failed:
            print(f"  - {item}", file=sys.stderr)
        return 1

    print(f"Runtime dependency verification passed: {', '.join(groups)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
