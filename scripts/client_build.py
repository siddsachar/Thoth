"""Select verified frontend package data for setuptools without runtime imports.

This build-only selector checks local paths, sizes, digests and manifest links.
It does not grant file, session, native or HTTP authority: the application still
uses row_bot.client_assets to validate and serve its independently loaded bytes.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from setuptools.command.build_py import build_py
from setuptools.errors import SetupError


_CLIENT = Path("static/client-v2")
_PRIVATE = ("asset-manifest.json", ".vite/manifest.json")
_HASHED = re.compile(r"assets/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_.-]+-[A-Za-z0-9_-]{8,}\.(?:js|css|svg|png|jpg|jpeg|webp|ico|woff2?)$")
_DIGEST = re.compile(r"[a-f0-9]{64}")


def _unlinked(path: Path, *, directory: bool = False) -> os.stat_result:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError("linked build path")
    if directory and not stat.S_ISDIR(info.st_mode):
        raise ValueError("invalid build directory")
    return info


def _read(root: Path, name: str, maximum: int) -> bytes:
    parts = name.split("/")
    if any(value in name for value in ("\\", "%", ":", "\x00")) or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("invalid build path")
    path = root
    for part in parts:
        path /= part
        info = _unlinked(path)
    if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
        raise ValueError("invalid build file")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("invalid build path")
    with path.open("rb") as stream:
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError("build file too large")
    return data


def select_client_payload(root: Path) -> dict[str, bytes]:
    """Return only the current inventory and exact private manifest bytes.

    Unlisted source files are deliberately ignored; they are never copied into
    a wheel. Missing or inconsistent inventoried files fail the build safely.
    """
    try:
        _unlinked(root, directory=True)
        inventory_bytes = _read(root, _PRIVATE[0], 256 * 1024)
        inventory = json.loads(inventory_bytes)
        if (not isinstance(inventory, dict) or set(inventory) != {"version", "files"}
                or type(inventory["version"]) is not int or inventory["version"] != 1):
            raise ValueError("invalid inventory")
        entries = inventory["files"]
        if not isinstance(entries, dict) or "index.html" not in entries or not 2 <= len(entries) <= 512:
            raise ValueError("invalid inventory")
        payload: dict[str, bytes] = {}
        total = 0
        for name, entry in entries.items():
            if name != "index.html" and not _HASHED.fullmatch(name):
                raise ValueError("invalid asset name")
            if (not isinstance(entry, dict) or set(entry) != {"sha256", "size"}
                    or type(entry["size"]) is not int or not 0 <= entry["size"] <= 8 * 1024 * 1024
                    or not isinstance(entry["sha256"], str) or not _DIGEST.fullmatch(entry["sha256"])):
                raise ValueError("invalid asset entry")
            total += entry["size"]
            if total > 32 * 1024 * 1024:
                raise ValueError("build too large")
            data = _read(root, name, 256 * 1024 if name == "index.html" else 8 * 1024 * 1024)
            if len(data) != entry["size"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
                raise ValueError("asset integrity mismatch")
            payload[name] = data
        vite_bytes = _read(root, _PRIVATE[1], 256 * 1024)
        vite = json.loads(vite_bytes)
        if (not isinstance(vite, dict) or not isinstance(vite.get("index.html"), dict)
                or vite["index.html"].get("isEntry") is not True):
            raise ValueError("invalid Vite manifest")
        for entry in vite.values():
            if not isinstance(entry, dict) or entry.get("file") not in payload:
                raise ValueError("invalid Vite entry")
            for key in ("css", "assets", "imports", "dynamicImports"):
                values = entry.get(key, [])
                if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
                    raise ValueError("invalid Vite references")
                targets = vite if key in {"imports", "dynamicImports"} else payload
                if any(value not in targets for value in values):
                    raise ValueError("missing Vite reference")
        return {**payload, _PRIVATE[0]: inventory_bytes, _PRIVATE[1]: vite_bytes}
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise SetupError("Client package data is missing or invalid; rebuild the local frontend payload") from exc


class ClientBuildPy(build_py):
    """Build wheels from current asset bytes into fresh generated output only."""

    def finalize_options(self) -> None:
        super().finalize_options()
        # PEP 420 discovery can otherwise treat generated directories as Python
        # packages and admit unlisted .py files independently of package_data.
        self.packages = [name for name in self.packages or []
                         if name != "row_bot.static.client-v2" and not name.startswith("row_bot.static.client-v2.")]

    def find_data_files(self, package: str, src_dir: str) -> list[str]:
        files = super().find_data_files(package, src_dir)
        root = Path(self.get_package_dir("row_bot")) / _CLIENT
        absolute = root.absolute()
        files = [name for name in files if not Path(name).absolute().is_relative_to(absolute)]
        if package == "row_bot":
            payload = getattr(self, "_selected_payload", None)
            if payload is None and (root.exists() or root.is_symlink()):
                payload = select_client_payload(root)
            # Metadata/editable queries can precede the opt-in frontend build.
            # A real wheel still requires the payload at the start of run().
            files.extend(str(root / name) for name in payload or {})
        return files

    def get_source_files(self) -> list[str]:
        # Keep the cmdclass import available when a wheel is built from an sdist.
        return [*super().get_source_files(), "scripts/client_build.py"]

    def run(self) -> None:
        if self.editable_mode:
            return super().run()
        payload = select_client_payload(Path(self.get_package_dir("row_bot")) / _CLIENT)
        self._selected_payload = payload
        build = self.get_finalized_command("build")
        base = Path(build.build_base).absolute()
        project = Path(self.get_package_dir("row_bot")).absolute().parent.parent
        if not base.is_relative_to(project):
            raise SetupError("Client build output must stay inside the source project")
        for directory in (project, *[project.joinpath(*base.relative_to(project).parts[:index])
                                      for index in range(1, len(base.relative_to(project).parts) + 1)]):
            if directory.exists() or directory.is_symlink():
                _unlinked(directory, directory=True)
        base.mkdir(parents=True, exist_ok=True)
        fresh = Path(tempfile.mkdtemp(prefix="client-wheel-", dir=base))
        self.build_lib = build.build_lib = build.build_purelib = str(fresh / "lib")
        # install_lib copies a build tree recursively; changing only the data
        # list cannot exclude stale outputs from earlier or failed builds.
        wheel = self.distribution.command_obj.get("bdist_wheel")
        if wheel is not None:
            wheel.bdist_dir = str(fresh / "wheel")
        self.__dict__.pop("data_files", None)
        super().run()
        destination = Path(self.build_lib) / "row_bot" / _CLIENT
        for name, data in payload.items():
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
