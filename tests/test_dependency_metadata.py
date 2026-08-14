from __future__ import annotations

import re
import tomllib
from pathlib import Path
from types import SimpleNamespace

import yaml


ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
REQUIRED_EXTRAS = {
    "voice",
    "designer",
    "browser",
    "channels",
    "mcp",
    "developer",
    "local-embeddings",
    "media",
    "all",
}
HIGH_RISK_DIRECT_DEPENDENCIES = {
    "anthropic",
    "discord-py",
    "faiss-cpu",
    "faster-whisper",
    "funasr",
    "google-genai",
    "huggingface-hub",
    "kokoro-onnx",
    "langchain",
    "langchain-anthropic",
    "langchain-classic",
    "langchain-community",
    "langchain-core",
    "langchain-google-genai",
    "langchain-huggingface",
    "langchain-mcp-adapters",
    "langchain-ollama",
    "langchain-openai",
    "langchain-openrouter",
    "langchain-xai",
    "langgraph",
    "langgraph-checkpoint-sqlite",
    "mcp",
    "modelscope",
    "nicegui",
    "numpy",
    "openai",
    "playwright",
    "pydantic",
    "pywebview",
    "pywinpty",
    "python-telegram-bot",
    "sentence-transformers",
    "slack-bolt",
    "tokenizers",
    "torch",
    "torchaudio",
    "transformers",
    "twilio",
}


def _load_pyproject() -> dict:
    with PYPROJECT.open("rb") as handle:
        return tomllib.load(handle)


def _normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _dependency_name(requirement: str) -> str:
    match = re.match(r"\s*([A-Za-z0-9_.-]+)", requirement)
    assert match, f"could not parse dependency name from {requirement!r}"
    return _normalize_name(match.group(1))


def _has_upper_bound_or_pin(requirement: str) -> bool:
    requirement = requirement.split(";", 1)[0]
    return any(operator in requirement for operator in ("<", "==", "==="))


def _direct_dependency_map(pyproject: dict) -> dict[str, str]:
    project = pyproject["project"]
    dependencies = list(project.get("dependencies", []))
    for extra_deps in project.get("optional-dependencies", {}).values():
        dependencies.extend(extra_deps)
    return {_dependency_name(dep): dep for dep in dependencies}


def test_pyproject_declares_row_bot_package_metadata():
    pyproject = _load_pyproject()
    project = pyproject["project"]

    assert project["name"] == "row-bot"
    assert project["requires-python"] == ">=3.12,<3.14"
    assert "version" in project["dynamic"]
    assert pyproject["tool"]["setuptools"]["dynamic"]["version"] == {
        "attr": "row_bot.version.__version__"
    }
    assert project["scripts"]["row-bot"] == "row_bot.launcher:main"


def test_required_extras_exist_and_all_extra_covers_them():
    optional = _load_pyproject()["project"]["optional-dependencies"]

    assert REQUIRED_EXTRAS <= set(optional)
    all_names = {_dependency_name(dep) for dep in optional["all"]}
    for extra in REQUIRED_EXTRAS - {"all"}:
        extra_names = {_dependency_name(dep) for dep in optional[extra]}
        assert extra_names <= all_names, f"{extra} dependencies missing from all extra"


def test_voice_extra_includes_funasr_runtime_dependencies():
    voice = _load_pyproject()["project"]["optional-dependencies"]["voice"]
    voice_dependencies = {_dependency_name(dep): dep for dep in voice}

    assert {"funasr", "modelscope", "torch", "torchaudio"} <= set(
        voice_dependencies
    )
    assert voice_dependencies["torch"].split(";", 1)[0].strip() == "torch==2.11.0"
    assert (
        voice_dependencies["torchaudio"].split(";", 1)[0].strip()
        == "torchaudio==2.11.0"
    )
    for name in ("funasr", "modelscope", "torch", "torchaudio"):
        assert "platform_machine != 'x86_64'" in voice_dependencies[name]


def test_locked_torch_and_torchaudio_versions_match_exactly():
    with (ROOT / "uv.lock").open("rb") as handle:
        packages = tomllib.load(handle)["package"]

    def _locked_versions(name: str) -> set[str]:
        return {
            str(package["version"]).split("+", 1)[0]
            for package in packages
            if package["name"] == name
        }

    assert _locked_versions("torch") == {"2.11.0"}
    assert _locked_versions("torchaudio") == {"2.11.0"}


def test_high_risk_direct_dependencies_have_upper_bounds_or_pins():
    dependency_map = _direct_dependency_map(_load_pyproject())

    missing = sorted(HIGH_RISK_DIRECT_DEPENDENCIES - set(dependency_map))
    assert not missing, f"high-risk dependencies missing from project metadata: {missing}"

    unbounded = sorted(
        name
        for name in HIGH_RISK_DIRECT_DEPENDENCIES
        if not _has_upper_bound_or_pin(dependency_map[name])
    )
    assert not unbounded, f"high-risk dependencies missing upper bounds or pins: {unbounded}"


def test_macos_appkit_dependencies_are_direct_and_darwin_scoped():
    dependency_map = _direct_dependency_map(_load_pyproject())

    for name in ("pyobjc-core", "pyobjc-framework-cocoa"):
        requirement = dependency_map[name]
        assert "sys_platform == 'darwin'" in requirement
        assert _has_upper_bound_or_pin(requirement)


def test_lockfile_and_generated_requirements_are_committed():
    requirements = ROOT / "requirements.txt"

    assert (ROOT / "uv.lock").is_file()
    assert requirements.is_file()
    assert requirements.read_text(encoding="utf-8").startswith(
        "# This file is generated from pyproject.toml and uv.lock.\n"
        "# Do not edit by hand.\n"
    )


def test_dependabot_uses_uv_for_python_updates():
    dependabot = yaml.safe_load((ROOT / ".github/dependabot.yml").read_text(encoding="utf-8"))
    updates = dependabot["updates"]
    ecosystems = {entry["package-ecosystem"]: entry for entry in updates}

    assert "uv" in ecosystems
    assert "pip" not in ecosystems
    assert ecosystems["uv"]["directory"] == "/"
    assert ecosystems["uv"]["labels"] == ["dependencies", "python"]
    assert ecosystems["github-actions"]["labels"] == ["dependencies", "github-actions"]


def test_payload_manifest_includes_dependency_provenance_files():
    import scripts.app_payload_manifest as manifest

    assert {"pyproject.toml", "uv.lock", "requirements.txt"} <= set(manifest.ROOT_FILES)


def test_ci_security_and_installer_dependency_hooks_are_wired():
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    release = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    lint = (ROOT / ".github/workflows/lint.yml").read_text(encoding="utf-8")
    lock_check = ROOT / ".github/workflows/uv-lockfile-check.yml"
    osv = ROOT / ".github/workflows/osv-scanner.yml"
    windows_build = (ROOT / "installer/build_installer.ps1").read_text(encoding="utf-8")
    mac_build = (ROOT / "installer/build_mac_app.sh").read_text(encoding="utf-8")
    linux_build = (ROOT / "installer/build_linux_app.sh").read_text(encoding="utf-8")
    legacy_deps = (ROOT / "installer/install_deps.bat").read_text(encoding="utf-8")
    windows_installer = (ROOT / "installer/row_bot_setup.iss").read_text(encoding="utf-8")

    assert "uv sync --locked --all-extras --group test" in ci
    assert "uv sync --locked --all-extras --group test" in release
    assert "uv sync --locked --group lint" in lint
    assert lock_check.is_file()
    assert ".github/dependabot.yml" in lock_check.read_text(encoding="utf-8")
    assert "uv lock --check" in lock_check.read_text(encoding="utf-8")
    assert "export_locked_requirements.py --check" in lock_check.read_text(encoding="utf-8")
    assert osv.is_file()
    assert "uv.lock" in osv.read_text(encoding="utf-8")
    assert "OSV" in osv.read_text(encoding="utf-8")

    for source in (windows_build, mac_build, linux_build, legacy_deps):
        assert "locked Python packages from requirements.txt" in source
        assert "verify_runtime_dependencies.py" in source
        assert " all" in source

    assert 'Source: "..\\pyproject.toml"' in windows_installer
    assert 'Source: "..\\uv.lock"' in windows_installer


def test_runtime_verifier_presence_checks_pystray_on_headless_linux(monkeypatch):
    import scripts.verify_runtime_dependencies as verifier

    monkeypatch.setattr(verifier.platform, "system", lambda: "Linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(
        verifier.importlib,
        "import_module",
        lambda module: (_ for _ in ()).throw(AssertionError(module)),
    )
    monkeypatch.setattr(verifier.importlib.util, "find_spec", lambda module: object())

    verifier._verify_module("pystray")


def test_runtime_verifier_voice_group_covers_supported_funasr_stack():
    import scripts.verify_runtime_dependencies as verifier

    expected = {"funasr", "modelscope", "torch", "torchaudio"}
    if verifier.FUNASR_VOICE_MODULES:
        assert expected <= set(verifier.GROUPS["voice"])
    else:
        assert expected.isdisjoint(verifier.GROUPS["voice"])


def test_runtime_verifier_checks_appkit_modules_on_macos(monkeypatch):
    import importlib

    import scripts.verify_runtime_dependencies as verifier

    imported = []
    original_import_module = importlib.import_module

    monkeypatch.setattr(verifier.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        verifier.importlib,
        "import_module",
        lambda module: imported.append(module) or SimpleNamespace()
        if module in {"AppKit", "Foundation", "objc", "PyObjCTools.AppHelper"}
        else original_import_module(module),
    )

    for module in ("AppKit", "Foundation", "objc", "PyObjCTools.AppHelper"):
        verifier._verify_module(module)

    assert imported == ["AppKit", "Foundation", "objc", "PyObjCTools.AppHelper"]

def test_runtime_verifier_reports_missing_and_broken_funasr_imports(
    monkeypatch,
    capsys,
):
    import scripts.verify_runtime_dependencies as verifier

    monkeypatch.setitem(verifier.GROUPS, "voice", ("funasr", "torchaudio"))

    def _verify(module: str) -> None:
        if module == "funasr":
            raise ModuleNotFoundError("No module named 'funasr'")
        raise RuntimeError("torchaudio native library failed to load")

    monkeypatch.setattr(verifier, "_verify_module", _verify)

    assert verifier.main(["voice"]) == 1
    error = capsys.readouterr().err
    assert "voice:funasr: ModuleNotFoundError" in error
    assert "voice:torchaudio: RuntimeError" in error
