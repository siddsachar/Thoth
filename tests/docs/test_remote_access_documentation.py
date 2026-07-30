from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def test_readme_documents_remote_access_routes_and_security_boundaries() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    for expected in (
        "single-owner, multi-device",
        "invitation",
        "session",
        "Tailscale Serve",
        "Direct LAN",
        "SSH tunnel",
        "Docker",
        "HTTPS reverse proxy or VPS",
        "row-bot serve",
        "row-bot access invite",
        "row-bot access doctor",
        "ROW_BOT_TRUSTED_PROXY_CIDRS",
        "Back up the complete active `ROW_BOT_DATA_DIR`",
    ):
        assert expected in readme

    assert "never installs Tailscale, signs in, enables Funnel" in readme
    assert "Plain HTTP is unencrypted" in readme
    assert "If the owner loses every browser session" in readme


def test_packaging_and_source_docs_cover_access_ownership() -> None:
    installer = (ROOT / "installer" / "README.md").read_text(encoding="utf-8")
    layout = (ROOT / "docs" / "SOURCE_LAYOUT.md").read_text(encoding="utf-8")

    assert "authenticated access/server package" in installer
    assert "does not bundle or install Tailscale" in installer
    assert "Docker image contains no" in installer
    assert "`src/row_bot/access/` owns" in layout
    assert "`src/row_bot/ui/remote_access_settings.py`" in layout
    assert "`scripts/smoke_remote_access.py`" in layout
    assert "physical access database remains `mobile.db`" in layout
    assert "`tailscale_serve_ownership.json`" in layout


def test_public_docs_metadata_owns_remote_access_sources() -> None:
    guides = yaml.safe_load(
        (ROOT / "docs-content" / "metadata" / "how_to_guides.yml").read_text(
            encoding="utf-8"
        )
    )["guides"]
    surfaces = yaml.safe_load(
        (ROOT / "docs-content" / "metadata" / "ui_surfaces.yml").read_text(
            encoding="utf-8"
        )
    )["surfaces"]

    guide = guides["configure-remote-access"]
    assert guide["route"] == "/docs/operations/remote-access"
    assert "src/row_bot/access/" in guide["sources"]
    assert "deploy/docker/README.md" in guide["sources"]
    assert (
        "src/row_bot/ui/remote_access_settings.py"
        in surfaces["settings_system"]["source_files"]
    )
    assert surfaces["settings_remote_access"]["screenshot_id"] == (
        "settings-remote-access"
    )
    assert surfaces["remote_access_invitation"]["screenshot_id"] == (
        "remote-access-invitation"
    )


def test_public_remote_access_guide_matches_current_security_and_runtime() -> None:
    guide = (
        ROOT / "docs-site" / "docs" / "operations" / "remote-access.mdx"
    ).read_text(encoding="utf-8")

    for expected in (
        "single-owner, multi-device",
        'Screenshot id="settings-remote-access"',
        'Screenshot id="remote-access-invitation"',
        "expires after 10 minutes",
        "Tailscale Serve",
        "does not install Tailscale",
        "only loopback connections from the local Tailscale proxy are trusted",
        "Direct LAN",
        "SSH tunnel",
        "row-bot serve",
        "ROW_BOT_TRUSTED_PROXY_CIDRS",
        "complete supported server feature set",
        "Voice From A Remote Browser",
        "`untrusted_forwarding_headers`",
        "row-bot access doctor",
    ):
        assert expected in guide

    assert "Do not fix this by trusting a broad network." in guide
    assert "enable Funnel" in guide


def test_remote_access_screenshots_are_public_and_linked() -> None:
    screenshots = yaml.safe_load(
        (ROOT / "docs-content" / "metadata" / "screenshots.yml").read_text(
            encoding="utf-8"
        )
    )["screenshots"]

    settings = screenshots["settings-remote-access"]
    invitation = screenshots["remote-access-invitation"]
    assert settings["public_asset"] is True
    assert settings["source"] == "isolated-demo-data"
    assert settings["capture_selector"] == (
        '[data-docs-id="remote-access-settings"]'
    )
    assert "/docs/operations/remote-access" in settings["docs_pages"]
    assert invitation["capture_selector"] == (
        '[data-docs-id="remote-access-invitation"]'
    )
    assert "/docs/mobile-native/" in invitation["docs_pages"]
