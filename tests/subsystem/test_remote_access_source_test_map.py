from tests.helpers.source_test_map import select_tests_for_changes


def test_remote_access_runtime_and_ui_select_security_and_compatibility_tests() -> None:
    selection = select_tests_for_changes(
        [
            "src/row_bot/access/policy.py",
            "src/row_bot/ui/access_context.py",
            "src/row_bot/ui/remote_access_settings.py",
            "scripts/smoke_remote_access.py",
        ]
    )

    assert "remote_access_server" in selection.matched_rules
    assert "tests/subsystem/access" in selection.test_paths
    assert "tests/integration/access" in selection.test_paths
    assert "tests/subsystem/mobile" in selection.test_paths
    assert "tests/integration/mobile" in selection.test_paths
    assert not selection.unmatched_files


def test_remote_access_deployment_artifacts_select_installer_contract() -> None:
    selection = select_tests_for_changes(
        [
            ".dockerignore",
            "deploy/docker/Dockerfile",
            "deploy/docker/compose.yaml",
            "deploy/reverse-proxy/Caddyfile.example",
            "deploy/systemd/row-bot.service.example",
        ]
    )

    assert selection.matched_rules == ("remote_access_server",)
    assert (
        "tests/contracts/installers/test_remote_access_deployment_contract.py"
        in selection.test_paths
    )
    assert not selection.unmatched_files


def test_container_marker_and_smoke_select_all_crossed_owners() -> None:
    selection = select_tests_for_changes(
        [
            "src/row_bot/runtime_paths.py",
            "src/row_bot/developer/sandbox_runtime.py",
            "scripts/smoke_docker_server.py",
        ]
    )

    assert "remote_access_server" in selection.matched_rules
    assert "developer_studio" in selection.matched_rules
    assert "installer_and_release" in selection.matched_rules
    assert "tests/subsystem/access" in selection.test_paths
    assert "tests/subsystem/developer" in selection.test_paths
    assert "tests/subsystem/installer" in selection.test_paths
    assert "tests/contracts/installers" in selection.test_paths
    assert not selection.unmatched_files
