from __future__ import annotations

import pathlib
import tomllib

import pytest
import yaml


pytestmark = [pytest.mark.subsystem, pytest.mark.installer]


def test_project_entrypoint_points_to_launcher() -> None:
    data = tomllib.loads(pathlib.Path("pyproject.toml").read_text(encoding="utf-8"))

    assert data["project"]["scripts"]["row-bot"] == "row_bot.launcher:main"


def test_linux_installer_contract_preserves_user_data_and_verifies_dependencies() -> None:
    build_script = pathlib.Path("installer/build_linux_app.sh").read_text(encoding="utf-8")
    install_script = pathlib.Path("installer/install-linux.sh").read_text(encoding="utf-8")

    assert "scripts/verify_runtime_dependencies.py" in build_script
    assert "User data in ~/.row-bot was left untouched." in build_script
    assert "LAUNCH_CMD=\"row-bot\"" in install_script
    assert "mktemp -d" in install_script


def test_macos_installer_uses_native_tray_host() -> None:
    build_script = pathlib.Path("installer/build_mac_app.sh").read_text(encoding="utf-8")
    host_source = pathlib.Path("installer/macos/RowBotTrayHost.m").read_text(encoding="utf-8")

    assert 'HOST_SOURCE="$SCRIPT_DIR/macos/RowBotTrayHost.m"' in build_script
    assert "xcrun clang" in build_script
    assert "-fobjc-arc" in build_script
    assert "-fblocks" in build_script
    assert "-framework Cocoa" in build_script
    assert "-mmacosx-version-min=11.0" in build_script
    assert 'cat > "$MACOS_DIR/row-bot"' not in build_script
    assert '"$MACOS_DIR/row-bot" --self-test' in build_script
    assert "<key>LSUIElement</key>" in build_script
    assert "<true/>" in build_script.split("<key>LSUIElement</key>", 1)[1].split("<key>", 1)[0]

    assert "NSStatusBar" in host_source
    assert "statusItemWithLength" in host_source
    assert '@"launcher.py", @"--no-tray", @"--native"' in host_source
    assert "PYTHONDONTWRITEBYTECODE" in host_source
    assert "launcher_state.json" in host_source
    assert "window_pid" in host_source
    assert "--self-test" in host_source


def test_macos_installer_verify_smokes_native_host() -> None:
    workflow = pathlib.Path(".github/workflows/installer-verify.yml").read_text(encoding="utf-8")

    assert 'APP_EXEC="$APP_PATH/Contents/MacOS/row-bot"' in workflow
    assert 'file "$APP_EXEC" | grep -q "Mach-O"' in workflow
    assert 'plutil -lint "$APP_PATH/Contents/Info.plist"' in workflow
    assert '"$APP_EXEC" --self-test' in workflow


def test_ci_declares_subsystem_and_smoke_lanes() -> None:
    ci = pathlib.Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "scripts/run_test_matrix.py contract-subsystem" in ci
    assert "scripts/run_test_matrix.py pr" in ci
    assert "scripts/run_test_matrix.py legacy-parity" not in ci
    assert "migrated-subsystem-coverage" in ci
    assert "scripts/smoke_app.py --port 8090 --timeout 120" in ci


def test_client_build_action_pins_tools_and_preserves_manifests() -> None:
    action = yaml.load(pathlib.Path(".github/actions/build-client/action.yml").read_text(encoding="utf-8"),
                       Loader=yaml.BaseLoader)
    steps = action["runs"]["steps"]
    setup = next(step for step in steps if step.get("uses", "").startswith("actions/setup-node@"))
    assert setup["with"]["node-version"] == "24.15.0"
    assert setup["with"]["package-manager-cache"] == "false"
    install = next(step for step in steps if "npm ci" in step.get("run", ""))
    assert "--ignore-scripts" in install["run"]
    assert "--no-audit" in install["run"] and "--no-fund" in install["run"]
    assert install["env"] == {"npm_config_audit": "false", "npm_config_fund": "false"}
    build = next(step for step in steps if "npm run build" in step.get("run", ""))
    assert "node scripts/asset-manifest.mjs dist --package" in build["run"]
    assert steps.index(install) < steps.index(build)
    packager = pathlib.Path("frontend/scripts/asset-manifest.mjs").read_text(encoding="utf-8")
    assert "'.vite/manifest.json'" in packager
    assert "await mkdir(destination);" in packager
    assert "--package-dir" in packager


@pytest.mark.parametrize("workflow,jobs", [
    ("ci.yml", ["smoke", "full-test"]),
    ("release.yml", ["release-preflight", "build-windows", "build-linux", "build-macos"]),
    ("installer-verify.yml", ["verify-windows", "verify-linux", "verify-macos"]),
])
def test_fresh_checkout_ci_and_installer_jobs_build_client_assets_first(workflow: str, jobs: list[str]) -> None:
    data = yaml.load(pathlib.Path(".github/workflows", workflow).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    for name in jobs:
        steps = data["jobs"][name]["steps"]
        checkout = next(i for i, step in enumerate(steps) if step.get("uses", "").startswith("actions/checkout@"))
        build = next(i for i, step in enumerate(steps) if step.get("uses") == "./.github/actions/build-client")
        consumer = next(i for i, step in enumerate(steps) if any(command in step.get("run", "") for command in (
            "scripts/run_test_matrix.py", "installer/build_", "installer\\build_")))
        assert checkout < build < consumer


def test_installer_stages_are_verified_before_compile_sign_or_package() -> None:
    windows = pathlib.Path("installer/build_installer.ps1").read_text(encoding="utf-8")
    assert '[guid]::NewGuid()' in windows
    assert '--package-dir $ClientAssetStage' in windows
    assert windows.index('--compare $ClientBuild --strict') < windows.index('& $Iscc ')
    assert '"/DClientAssetDir=$ClientAssetStage"' in windows
    inno = pathlib.Path("installer/row_bot_setup.iss").read_text(encoding="utf-8")
    assert 'static\\client-v2\\*' in inno
    assert 'Source: "{#ClientAssetDir}\\*"' in inno
    for name in ("build_mac_app.sh", "build_linux_app.sh"):
        script = pathlib.Path("installer", name).read_text(encoding="utf-8")
        assert "--exclude='/static/client-v2/***'" in script
        assert '--package-dir "$CLIENT_STAGE"' in script
        assert '--root "$CLIENT_STAGE" --compare "$CLIENT_BUILD" --strict' in script
        assert script.index('--package-dir "$CLIENT_STAGE"') < script.index('--root "$CLIENT_STAGE"')
    # Build-time validation never expands the installed runtime script inventory.
    from scripts.app_payload_manifest import RUNTIME_SCRIPT_FILES
    assert "scripts/verify_client_assets.py" not in RUNTIME_SCRIPT_FILES
