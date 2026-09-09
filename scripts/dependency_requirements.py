"""Check exact normalized feature/all requirement consistency in pyproject.toml."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import sys
import tomllib
from typing import Iterable, Mapping

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name


@dataclass(frozen=True)
class NormalizedRequirement:
    """Names/extras/specifiers use packaging's normalization, including PEP 440."""

    name: str
    extras: frozenset[str]
    specifier: SpecifierSet
    marker: str | None
    url: str | None


def normalize_requirement(value: str) -> NormalizedRequirement:
    """Parse a complete PEP 508 requirement without dropping markers or extras."""
    requirement = Requirement(value)
    return NormalizedRequirement(
        canonicalize_name(requirement.name),
        frozenset(canonicalize_name(extra) for extra in requirement.extras),
        requirement.specifier,
        str(requirement.marker) if requirement.marker is not None else None,
        requirement.url,
    )


def requirement_set(
    values: Iterable[str], *, environment: Mapping[str, str] | None = None,
) -> frozenset[NormalizedRequirement]:
    """Keep all requirements, or select a fully specified target's active ones."""
    result = set()
    for value in values:
        parsed = Requirement(value)
        if environment is not None and parsed.marker is not None and not parsed.marker.evaluate(environment):
            continue
        result.add(normalize_requirement(value))
    return frozenset(result)


def aggregate_difference(
    optional: Mapping[str, Iterable[str]], *, environment: Mapping[str, str] | None = None,
) -> tuple[frozenset[NormalizedRequirement], frozenset[NormalizedRequirement]]:
    """Return requirements missing from all and unexpected in all, respectively.

    Exact normalized declarations are compared, rather than guessing whether
    different specifiers or Boolean markers might resolve to compatible wheels.
    Lock/export and actual platform imports remain independent checks.
    """
    expected = requirement_set((value for extra, values in optional.items() if extra != "all" for value in values),
                               environment=environment)
    actual = requirement_set(optional.get("all", ()), environment=environment)
    return expected - actual, actual - expected


def marker_environments() -> tuple[dict[str, str], ...]:
    """Deterministic supported-platform fixtures, with no current-host defaults."""
    result = []
    for version in ("3.12", "3.13"):
        for system, platform, os_name, machine in (
            ("Windows", "win32", "nt", "AMD64"),
            ("Darwin", "darwin", "posix", "arm64"),
            ("Darwin", "darwin", "posix", "x86_64"),
            ("Linux", "linux", "posix", "x86_64"),
            ("Linux", "linux", "posix", "aarch64"),
        ):
            result.append({"implementation_name": "cpython", "implementation_version": version + ".0",
                           "os_name": os_name, "platform_machine": machine, "platform_release": "fixture",
                           "platform_system": system, "platform_version": "fixture",
                           "python_full_version": version + ".0", "python_version": version,
                           "sys_platform": platform, "extra": ""})
    return tuple(result)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path(__file__).resolve().parents[1] / "pyproject.toml")
    args = parser.parse_args(argv)
    try:
        with args.project.open("rb") as handle:
            optional = tomllib.load(handle)["project"]["optional-dependencies"]
        if not isinstance(optional, dict) or "all" not in optional:
            raise ValueError("missing_optional_dependencies")
        missing, unexpected = aggregate_difference(optional)
        profiles = marker_environments()
        profile_failures = sum(any(aggregate_difference(optional, environment=profile)) for profile in profiles)
    except (OSError, ValueError, KeyError, TypeError):
        print("Dependency requirement check failed: invalid project metadata", file=sys.stderr)
        return 2
    if missing or unexpected or profile_failures:
        # Names suffice to locate declarations; direct URLs may contain secrets.
        print(f"Dependency requirements differ: missing={sorted({item.name for item in missing})}; "
              f"unexpected={sorted({item.name for item in unexpected})}; "
              f"affected platform profiles={profile_failures}", file=sys.stderr)
        return 1
    print(f"Dependency requirements consistent: {len(optional) - 1} feature extras; "
          f"{len(profiles)} Windows/macOS/Linux Python profiles")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
