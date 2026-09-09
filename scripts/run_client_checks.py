"""Run the locked frontend checks without installation or a live application."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"


def check_commands(quality_only: bool = False) -> tuple[tuple[str, ...], ...]:
    """Return explicit local Node entry points; never invoke npm installation."""
    quality = (
        ("node_modules/eslint/bin/eslint.js", "."),
        ("scripts/check-boundaries.mjs",),
        ("node_modules/prettier/bin/prettier.cjs", "--check", "."),
        ("node_modules/typescript/bin/tsc", "--noEmit"),
        ("node_modules/vitest/vitest.mjs", "run"),
    )
    return quality if quality_only else (*quality,
        ("node_modules/vite/bin/vite.js", "build"),
        ("scripts/asset-manifest.mjs",),
        ("scripts/verify-build.mjs",),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quality-only", action="store_true")
    args = parser.parse_args(argv)
    node = shutil.which("node")
    if node is None or not (FRONTEND / "node_modules/typescript/bin/tsc").is_file():
        parser.error("Install the documented Node version and run npm ci --ignore-scripts in frontend first")
    # Production checks cannot accidentally retain a developer fixture build flag.
    environment = {key: value for key, value in os.environ.items()
                   if key not in {"VITE_ENABLE_FIXTURES", "NODE_OPTIONS", "OTEL_SERVICE_NAME"}
                   and not key.startswith("OTEL_")}
    for command in check_commands(args.quality_only):
        print("Client check: " + " ".join(command), flush=True)
        completed = subprocess.run([node, *command], cwd=FRONTEND, env=environment, check=False)
        if completed.returncode:
            return completed.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
