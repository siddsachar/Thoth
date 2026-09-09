from __future__ import annotations

from pathlib import Path
import tomllib

import pytest

from scripts.dependency_requirements import (
    aggregate_difference, main, marker_environments, normalize_requirement, requirement_set,
)


ROOT = Path(__file__).resolve().parents[2]


def test_normalization_preserves_complete_requirement_semantics() -> None:
    first = normalize_requirement("Demo_pkg[FOO_bar,cli]>=1.0, <2 ; sys_platform == 'win32'")
    second = normalize_requirement('demo-pkg[cli,foo-bar]<2,>=1.0 ; sys_platform == "win32"')
    assert first == second
    assert normalize_requirement("demo==1.0") == normalize_requirement("demo==1.0.0")
    assert first.name == "demo-pkg"
    assert first.extras == {"cli", "foo-bar"}


@pytest.mark.parametrize("aggregate", [
    "demo>=1,<3", "demo[cli]>=1,<2", "demo>=1,<2; sys_platform == 'win32'", "demo>=1,<2",
])
def test_name_only_match_cannot_hide_specifier_extra_or_marker_drift(aggregate: str) -> None:
    feature = "demo[cli]>=1,<2; sys_platform == 'linux'"
    missing, unexpected = aggregate_difference({"feature": [feature], "all": [aggregate]})
    assert missing == {normalize_requirement(feature)}
    assert unexpected == {normalize_requirement(aggregate)}


def test_direct_url_and_dependency_marker_are_not_discarded() -> None:
    assert normalize_requirement("demo @ https://fixture.invalid/a.whl") != normalize_requirement(
        "demo @ https://fixture.invalid/b.whl")
    optional = {"one": ["demo>=1,<2"], "two": ["demo>=1,<2"], "all": ["demo<2,>=1"]}
    assert aggregate_difference(optional) == (frozenset(), frozenset())


def test_platform_markers_are_evaluated_against_explicit_windows_macos_linux_profiles() -> None:
    requirements = ["cocoa>=1,<2; sys_platform == 'darwin'",
                    "terminal>=1,<2; sys_platform == 'win32'",
                    "voice>=1,<2; sys_platform != 'darwin' or platform_machine != 'x86_64'"]
    profiles = marker_environments()
    assert len(profiles) == 10
    assert {profile["python_version"] for profile in profiles} == {"3.12", "3.13"}
    for profile in profiles:
        names = {requirement.name for requirement in requirement_set(requirements, environment=profile)}
        assert ("cocoa" in names) == (profile["sys_platform"] == "darwin")
        assert ("terminal" in names) == (profile["sys_platform"] == "win32")
        assert ("voice" in names) == (profile["sys_platform"] != "darwin" or profile["platform_machine"] != "x86_64")
        assert aggregate_difference({"platform": requirements, "all": requirements}, environment=profile) == (
            frozenset(), frozenset())


def test_repository_all_extra_exactly_matches_feature_requirements() -> None:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        optional = tomllib.load(handle)["project"]["optional-dependencies"]
    assert aggregate_difference(optional) == (frozenset(), frozenset())
    for environment in marker_environments():
        assert aggregate_difference(optional, environment=environment) == (frozenset(), frozenset())


def test_cli_reports_a_fixture_mismatch_and_valid_metadata_without_resolving(tmp_path: Path, capsys) -> None:
    path = tmp_path / "fixture.toml"
    path.write_text('[project.optional-dependencies]\nfeature=["demo[cli]>=1,<2"]\nall=["demo>=1,<2"]\n')
    assert main(["--project", str(path)]) == 1
    assert "demo" in capsys.readouterr().err
    path.write_text('[project.optional-dependencies]\nfeature=["demo[cli]>=1,<2"]\nall=["demo[cli]<2,>=1"]\n')
    assert main(["--project", str(path)]) == 0
    assert "10 Windows/macOS/Linux" in capsys.readouterr().out
    path.write_text("not valid fixture metadata")
    assert main(["--project", str(path)]) == 2
    assert "not valid fixture" not in capsys.readouterr().err
