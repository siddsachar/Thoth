"""Synchronize the Docusaurus build into the legacy GitHub Pages tree.

GitHub Pages serves this repository from ``main:/docs``.  The root of that
directory also contains the Row-Bot marketing site, so this script owns only
the Docusaurus support directories and selected top-level documentation files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BUILD_DIR = ROOT / "docs-site" / "build"
DEFAULT_PUBLISH_DIR = ROOT / "docs"

OWNED_DIRECTORIES = ("assets", "docs", "img", "pagefind", "search")
OWNED_FILES = (
    "llms-full.txt",
    "llms.txt",
    "sitemap.xml",
)
OBSOLETE_FILES = ("docs.html", "search.html")
HAND_CURATED_MARKETING_FILES = (
    "404.html",
    "architecture.html",
    "contact.html",
    "features.html",
    "index.html",
    "site.css",
    "site.js",
    "favicon.ico",
    "row_bot_glyph.png",
    "row_bot_glyph_256.png",
    "row_bot_hero.webp",
    "row_bot_preview.png",
    "row_bot_workspace_4_4_v2.png",
    "Context_Arch.png",
    "Core_Agent_Arch.png",
    "Designer_Studio_Arch.png",
    "Developer_Studio_Arch.png",
    "Knowledge_Graph_Arch.png",
    "Multi_Channel_Arch.png",
    "Safety_Privacy_Arch.png",
    "Self_Evolution_Arch.png",
    "Skills_System_Arch.png",
    "Workflows_Arch.png",
)


def _direct_child(root: Path, name: str) -> Path:
    resolved_root = root.resolve()
    target = (root / name).resolve()
    if target.parent != resolved_root:
        raise ValueError(f"Refusing to operate outside {resolved_root}: {target}")
    return target


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _directory_manifest(path: Path) -> dict[str, str]:
    if not path.is_dir():
        return {}
    return {
        item.relative_to(path).as_posix(): _digest(item)
        for item in sorted(path.rglob("*"))
        if item.is_file()
    }


def _pagefind_shape(path: Path) -> dict[str, object]:
    entry = json.loads((path / "pagefind-entry.json").read_text(encoding="utf-8"))
    languages = entry.get("languages", {})
    shape = {
        "version": entry.get("version"),
        "languages": {
            language: details.get("page_count")
            for language, details in sorted(languages.items())
        },
        "fragments": len(list((path / "fragment").glob("*.pf_fragment"))),
        "indexes": len(list((path / "index").glob("*.pf_index"))),
        "metadata": len(list(path.glob("*.pf_meta"))),
        "wasm": sorted(item.name for item in path.glob("*.pagefind")),
        "runtime": sorted(item.name for item in path.glob("pagefind*.js")),
        "styles": sorted(item.name for item in path.glob("pagefind*.css")),
    }
    if (
        not shape["version"]
        or not shape["languages"]
        or not shape["fragments"]
        or not shape["indexes"]
        or not shape["metadata"]
        or not shape["wasm"]
        or not shape["runtime"]
        or not shape["styles"]
    ):
        raise ValueError(f"Incomplete Pagefind artifact at {path}")
    return shape


def _check_pagefind(build_dir: Path, publish_dir: Path) -> list[str]:
    source = build_dir / "pagefind"
    target = _direct_child(publish_dir, "pagefind")
    if not target.is_dir():
        return [f"Missing published directory: {target}"]
    try:
        source_shape = _pagefind_shape(source)
        target_shape = _pagefind_shape(target)
    except (FileNotFoundError, json.JSONDecodeError, AttributeError, ValueError) as exc:
        return [f"Invalid Pagefind artifact: {exc}"]

    errors: list[str] = []
    if source_shape != target_shape:
        errors.append(f"Published Pagefind index shape is stale: {target}")
    return errors


def validate_sources(build_dir: Path) -> list[str]:
    errors: list[str] = []
    collisions = sorted(set(OWNED_FILES) & set(HAND_CURATED_MARKETING_FILES))
    if collisions:
        errors.append(f"Documentation ownership overlaps marketing files: {collisions}")
    for name in OWNED_DIRECTORIES:
        if not (build_dir / name).is_dir():
            errors.append(f"Missing build directory: {build_dir / name}")
    for name in OWNED_FILES:
        if not (build_dir / name).is_file():
            errors.append(f"Missing build file: {build_dir / name}")
    return errors


def check_sync(build_dir: Path, publish_dir: Path) -> list[str]:
    errors = validate_sources(build_dir)
    if errors:
        return errors

    for name in OWNED_DIRECTORIES:
        if name == "pagefind":
            errors.extend(_check_pagefind(build_dir, publish_dir))
            continue
        source = _directory_manifest(build_dir / name)
        target_path = _direct_child(publish_dir, name)
        target = _directory_manifest(target_path)
        if not target_path.is_dir():
            errors.append(f"Missing published directory: {target_path}")
        elif source != target:
            errors.append(f"Published directory is stale: {target_path}")

    for name in OWNED_FILES:
        source = build_dir / name
        target = _direct_child(publish_dir, name)
        if not target.is_file():
            errors.append(f"Missing published file: {target}")
        elif _digest(source) != _digest(target):
            errors.append(f"Published file is stale: {target}")
    for name in OBSOLETE_FILES:
        target = _direct_child(publish_dir, name)
        if target.exists():
            errors.append(f"Obsolete published file remains: {target}")
    return errors


def sync(build_dir: Path, publish_dir: Path) -> None:
    errors = validate_sources(build_dir)
    if errors:
        raise FileNotFoundError("\n".join(errors))

    publish_dir.mkdir(parents=True, exist_ok=True)
    for name in OWNED_DIRECTORIES:
        target = _direct_child(publish_dir, name)
        if target.exists():
            if not target.is_dir():
                raise ValueError(f"Expected a directory at {target}")
            shutil.rmtree(target)
        shutil.copytree(build_dir / name, target)

    for name in OWNED_FILES:
        shutil.copy2(build_dir / name, _direct_child(publish_dir, name))
    for name in OBSOLETE_FILES:
        target = _direct_child(publish_dir, name)
        if target.exists():
            if not target.is_file():
                raise ValueError(f"Expected a file at {target}")
            target.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", type=Path, default=DEFAULT_BUILD_DIR)
    parser.add_argument("--publish-dir", type=Path, default=DEFAULT_PUBLISH_DIR)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    build_dir = args.build_dir.resolve()
    publish_dir = args.publish_dir.resolve()
    if args.check:
        errors = check_sync(build_dir, publish_dir)
        if errors:
            print("\n".join(errors))
            return 1
        print("GitHub Pages documentation artifact is current.")
        return 0

    sync(build_dir, publish_dir)
    print(f"Synchronized documentation artifact to {publish_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
