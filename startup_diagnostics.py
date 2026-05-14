"""Startup diagnostics for optional native dependencies.

Thoth ships a self-contained Python runtime, but user-approved shell commands
can still install extra packages into that runtime. A broken optional native
package can then crash startup before the UI has a chance to explain the fix.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


logger = logging.getLogger(__name__)

RUNTIME_DEPENDENCY_GROUPS = {
    "embeddings": (
        "sentence_transformers",
        "langchain_huggingface",
        "transformers",
        "torch",
    ),
}


@dataclass(frozen=True)
class OptionalPackageIssue:
    package: str
    error: str
    recovery_hint: str


def preflight_required_runtime_packages(
    log: logging.Logger | None = None,
    groups: tuple[str, ...] = ("embeddings",),
) -> dict[str, list[str]]:
    """Log missing required runtime packages without importing heavy modules."""
    active_log = log or logger
    missing_by_group: dict[str, list[str]] = {}
    for group in groups:
        packages = RUNTIME_DEPENDENCY_GROUPS.get(group, ())
        missing = [
            package
            for package in packages
            if importlib.util.find_spec(package) is None
        ]
        if missing:
            missing_by_group[group] = missing

    active_log.info(
        "Runtime diagnostics: python=%s prefix=%s install_root=%s",
        sys.executable,
        sys.prefix,
        _install_root_hint(),
    )
    for group, missing in missing_by_group.items():
        active_log.error(
            "Required %s runtime package(s) missing from active Python: %s",
            group,
            ", ".join(missing),
        )
    return missing_by_group


def preflight_optional_native_packages(
    log: logging.Logger | None = None,
    packages: tuple[str, ...] = ("torchcodec",),
) -> list[OptionalPackageIssue]:
    """Probe optional native packages that are known to fail at import time.

    Missing optional packages are fine. Installed-but-broken packages are logged
    with recovery instructions and, where possible, hidden from libraries that
    treat mere package presence as availability.
    """
    active_log = log or logger
    issues: list[OptionalPackageIssue] = []
    for package in packages:
        if importlib.util.find_spec(package) is None:
            continue
        try:
            importlib.import_module(package)
        except Exception as exc:  # noqa: BLE001 - native loaders raise varied errors
            _clear_partial_import(package)
            if package == "torchcodec":
                _disable_transformers_torchcodec(active_log)
            issue = OptionalPackageIssue(
                package=package,
                error=f"{type(exc).__name__}: {exc}",
                recovery_hint=_recovery_hint(package),
            )
            issues.append(issue)
            active_log.warning(
                "Optional package '%s' is installed but cannot be imported: %s. %s",
                issue.package,
                issue.error,
                issue.recovery_hint,
            )
    return issues


def _install_root_hint() -> str:
    import os

    root = os.environ.get("THOTH_INSTALL_ROOT")
    if root:
        return root
    try:
        return str(Path(sys.executable).resolve().parents[2])
    except Exception:
        return ""


def _clear_partial_import(package: str) -> None:
    prefix = package + "."
    for module_name in list(sys.modules):
        if module_name == package or module_name.startswith(prefix):
            sys.modules.pop(module_name, None)


def _recovery_hint(package: str) -> str:
    if package == "torchcodec":
        site_packages = Path(sys.executable).resolve().parent / "Lib" / "site-packages"
        return (
            "Thoth does not require TorchCodec for built-in TTS. Close Thoth and run "
            f'"{sys.executable}" -m pip uninstall -y torchcodec. '
            "If pip cannot remove it, delete the torchcodec package and "
            f"torchcodec-*.dist-info from {site_packages}."
        )
    return f'Close Thoth and remove the optional package with "{sys.executable}" -m pip uninstall -y {package}.'


def _disable_transformers_torchcodec(log: logging.Logger) -> None:
    """Make Transformers treat broken TorchCodec as unavailable.

    Transformers checks package presence for TorchCodec in a few audio/video
    paths. When the distribution is present but the DLL cannot load, returning
    False avoids optional audio/video helpers choosing that backend later.
    """

    @lru_cache(maxsize=None)
    def _torchcodec_unavailable() -> bool:
        return False

    try:
        import transformers.utils as transformers_utils
        import transformers.utils.import_utils as import_utils
    except Exception as exc:  # noqa: BLE001 - diagnostics must never break startup
        log.debug("Could not patch Transformers TorchCodec availability: %s", exc)
        return

    try:
        original = getattr(import_utils, "is_torchcodec_available", None)
        if hasattr(original, "cache_clear"):
            original.cache_clear()
        import_utils.is_torchcodec_available = _torchcodec_unavailable
        transformers_utils.is_torchcodec_available = _torchcodec_unavailable
        if hasattr(import_utils, "BACKENDS_MAPPING") and "torchcodec" in import_utils.BACKENDS_MAPPING:
            import_utils.BACKENDS_MAPPING["torchcodec"] = (
                _torchcodec_unavailable,
                getattr(import_utils, "TORCHCODEC_IMPORT_ERROR", "TorchCodec is unavailable."),
            )
        for module_name in (
            "transformers.audio_utils",
            "transformers.video_utils",
            "transformers.video_processing_utils",
            "transformers.pipelines.automatic_speech_recognition",
            "transformers.pipelines.audio_classification",
        ):
            module = sys.modules.get(module_name)
            if module is not None:
                setattr(module, "is_torchcodec_available", _torchcodec_unavailable)
    except Exception as exc:  # noqa: BLE001
        log.debug("Could not complete Transformers TorchCodec patch: %s", exc)
