from __future__ import annotations

from row_bot.computer_use.readiness import load_cua_manifest


def test_reviewed_manifest_is_exact_and_never_latest() -> None:
    manifest = load_cua_manifest()
    assert manifest["version"] == "0.20.0"
    assert manifest["tag"] == "cua-driver-rs-v0.20.0"
    assert manifest["commit"] == "bb8c86049cad1bf0853c6d25c03c14875d0d047f"
    assert manifest["license"] == "MIT"
    assert manifest["telemetry_notice_version"] == 2
    assert manifest["reviewed_service_capabilities"] == ["verify_state", "invoke_menu"]
    assert {
        key: value["sha256"]
        for key, value in manifest["assets"].items()
    } == {
        "windows-x86_64": "bd27528e0d81bf78c03cdd77be28a3ea31899a370eaf06938ad21edac73290bd",
        "windows-arm64": "a01686a90725d9c902d558c053a0dd95bd181faff0418d9acb495da63f04a6a1",
        "macos-universal": "d5e61fecebd9a620e50c2b8b608c8e7e8141f74c6faebc2ae9ef5d0d96cce7b8",
    }
    for asset in manifest["assets"].values():
        assert len(asset["sha256"]) == 64
        assert "/cua-driver-rs-v0.20.0/" in asset["url"]
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
