"""Prepare Thoth release version bumps.

Usage:
    python scripts/cut_release.py 3.18.0
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def replace_once(path: Path, pattern: str, replacement: str) -> None:
    text = path.read_text(encoding="utf-8")
    new_text, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise SystemExit(f"Expected exactly one match in {path}: {pattern}")
    path.write_text(new_text, encoding="utf-8")


def validate_version(version: str) -> None:
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-(?:alpha|beta|rc)\.\d+)?", version):
        raise SystemExit("Version must look like 3.18.0 or 3.18.0-beta.1")


def main() -> None:
    parser = argparse.ArgumentParser(description="Bump Thoth release version files.")
    parser.add_argument("version", help="New version, e.g. 3.18.0")
    args = parser.parse_args()
    validate_version(args.version)

    version = args.version
    replace_once(ROOT / "version.py", r'__version__ = "[^"]+"', f'__version__ = "{version}"')
    replace_once(
        ROOT / "installer" / "thoth_setup.iss",
        r'#define MyAppVersion\s+"[^"]+"',
        f'#define MyAppVersion   "{version}"',
    )
    replace_once(
        ROOT / ".github" / "workflows" / "release.yml",
        r'THOTH_VERSION: "[^"]+"',
        f'THOTH_VERSION: "{version}"',
    )

    print(f"Prepared release version {version}")
    print("Next steps:")
    print("1. Update RELEASE_NOTES.md")
    print("2. Open a release-prep PR")
    print(f"3. After merge: git tag -a v{version} -m \"v{version}\" && git push origin v{version}")


if __name__ == "__main__":
    main()
