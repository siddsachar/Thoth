from __future__ import annotations

from row_bot.computer_use.readiness import load_cua_manifest


def test_reviewed_manifest_is_exact_and_never_latest() -> None:
    manifest = load_cua_manifest()
    assert manifest["version"] == "0.19.3"
    assert manifest["tag"] == "cua-driver-rs-v0.19.3"
    assert manifest["commit"] == "a1672e7b11951275ecfba3384264d4530185d0db"
    assert manifest["license"] == "MIT"
    assert manifest["telemetry_notice_version"] == 2
    assert manifest["reviewed_service_capabilities"] == ["verify_state", "invoke_menu"]
    assert {
        key: value["sha256"]
        for key, value in manifest["assets"].items()
    } == {
        "windows-x86_64": "e48b0117e343cec2577fc12693c741e094f389f8d4aef91e06284960bb03bce1",
        "windows-arm64": "693cff4618fdcb6b0ea797e2f5b17eb6291dcea4b62da7bc6b5c373f1aa1852f",
        "macos-universal": "a5b064bd3e05c3d97c4aaba1b8818e7b4203081ffc5f3186220005d356574aaa",
    }
    for asset in manifest["assets"].values():
        assert len(asset["sha256"]) == 64
        assert "/cua-driver-rs-v0.19.3/" in asset["url"]
        assert "latest" not in asset["url"]


def test_reviewed_telemetry_contract_is_expanded_but_content_free() -> None:
    manifest = load_cua_manifest()
    categories = " ".join(manifest["telemetry_allowed_categories"]).casefold()
    excluded = {value.casefold() for value in manifest["telemetry_excluded_content"]}
    assert "tool and operation" in categories
    assert "duration" in categories
    assert "aggregate session" in categories
    assert {
        "prompts",
        "tool arguments or results",
        "typed text",
        "screenshots",
        "accessibility trees",
        "application or window names",
        "urls",
        "filenames or paths",
        "raw errors",
    } <= excluded
