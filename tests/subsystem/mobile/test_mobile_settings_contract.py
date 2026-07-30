from __future__ import annotations

from pathlib import Path

from row_bot.ui.mobile_access_settings import (
    _lan_candidates,
    _preferred_pairing_origin,
    _recommended_pairing_candidate,
    _route_kind,
    _route_title,
)


def test_system_settings_include_remote_access_section() -> None:
    settings_src = Path("src/row_bot/ui/settings.py").read_text(encoding="utf-8")
    access_settings_src = Path("src/row_bot/ui/remote_access_settings.py").read_text(
        encoding="utf-8"
    )

    assert "build_remote_access_settings_section" in settings_src
    assert "Remote Access" in access_settings_src
    assert "Create invitation" in access_settings_src
    assert "Computer — Desktop layout" in access_settings_src
    assert "Phone or tablet — Compact layout" in access_settings_src
    assert "full owner access" in access_settings_src
    assert "Connected devices and sessions" in access_settings_src
    assert "Tailscale" in access_settings_src
    assert "Local network" in access_settings_src
    assert "generate_qr_png_b64" in access_settings_src
    assert "service.revoke_device" in access_settings_src
    assert "Sign out this browser" in access_settings_src
    assert "/api/access/logout" in access_settings_src
    assert "mobile: bool = False" in settings_src
    assert "row-bot-settings-mobile-shell" in settings_src
    assert "data-mobile-settings=true" in settings_src
    assert "ui.splitter(value=18)" in settings_src
    assert "settings-mobile-section-select" in settings_src


def test_mobile_access_settings_default_flow_hides_raw_connection_details() -> None:
    mobile_settings_src = Path("src/row_bot/ui/mobile_access_settings.py").read_text(
        encoding="utf-8"
    )

    assert "Pair a phone" in mobile_settings_src
    assert "Advanced connection details" in mobile_settings_src
    assert "All access candidates" in mobile_settings_src
    assert "Access candidates" not in mobile_settings_src
    assert mobile_settings_src.index("Pair a phone") < mobile_settings_src.index(
        "All access candidates"
    )
    assert mobile_settings_src.index(
        "Advanced connection details"
    ) < mobile_settings_src.index("Custom origin")
    assert "Create pairing QR" not in mobile_settings_src


def test_mobile_access_settings_use_lan_terminology() -> None:
    mobile_settings_src = Path("src/row_bot/ui/mobile_access_settings.py").read_text(
        encoding="utf-8"
    )

    assert "LAN" in mobile_settings_src
    assert "Wi-Fi" not in mobile_settings_src
    assert "Wifi" not in mobile_settings_src
    assert "wifi" not in mobile_settings_src


def test_mobile_access_route_helpers_use_plain_titles_and_lan_grouping() -> None:
    candidates = [
        {"access_mode": "localhost", "available": True, "url": "http://127.0.0.1:8080"},
        {
            "access_mode": "lan",
            "available": False,
            "url": "http://192.168.68.87:8080",
            "requires_bind": True,
        },
        {
            "access_mode": "ngrok",
            "available": True,
            "url": "https://rowbot.ngrok-free.dev",
        },
    ]

    assert _route_kind(candidates[0]) == "desktop"
    assert _route_title(candidates[0]) == "Desktop only"
    assert _route_kind(candidates[1]) == "lan"
    assert _route_title(candidates[1]) == "LAN"
    assert _route_kind(candidates[2]) == "public"
    assert _route_title(candidates[2]) == "Public tunnel"
    assert _lan_candidates(candidates) == [candidates[1]]
    assert _recommended_pairing_candidate(candidates) == candidates[2]


def test_mobile_settings_use_mobile_safe_provider_skill_plugin_sections() -> None:
    settings_src = Path("src/row_bot/ui/settings.py").read_text(encoding="utf-8")
    mobile_settings_src = Path("src/row_bot/ui/mobile_settings.py").read_text(
        encoding="utf-8"
    )

    assert "build_mobile_providers_settings" in settings_src
    assert "build_mobile_skills_settings" in settings_src
    assert "build_mobile_plugins_settings" in settings_src
    assert (
        '("Providers", "cloud", lambda: build_mobile_providers_settings' in settings_src
    )
    assert '("Skills", "auto_fix_high", build_mobile_skills_settings)' in settings_src
    assert '("Plugins", "extension", build_mobile_plugins_settings)' in settings_src
    assert "row-bot-mobile-provider-card" in mobile_settings_src
    assert "Secret values are never shown" in mobile_settings_src


def test_mobile_settings_keep_marketplaces_desktop_only() -> None:
    mobile_settings_src = Path("src/row_bot/ui/mobile_settings.py").read_text(
        encoding="utf-8"
    )
    settings_src = Path("src/row_bot/ui/settings.py").read_text(encoding="utf-8")

    assert "Skills Hub is desktop-only in Mobile V1" in mobile_settings_src
    assert "Plugin Marketplace is desktop-only in Mobile V1" in mobile_settings_src
    assert "open_skills_hub_dialog" not in mobile_settings_src
    assert "open_marketplace_dialog" not in mobile_settings_src
    assert "open_skills_hub_dialog" in settings_src
    assert "open_marketplace_dialog" in settings_src


def test_mobile_settings_keep_local_skill_management_controls() -> None:
    mobile_settings_src = Path("src/row_bot/ui/mobile_settings.py").read_text(
        encoding="utf-8"
    )

    assert "skills_mod.set_enabled" in mobile_settings_src
    assert "skills_mod.set_pinned" in mobile_settings_src
    assert 'ui.switch("", value=is_enabled' in mobile_settings_src
    assert '["All", "Enabled", "Pinned", "Custom", "Public"]' in mobile_settings_src
    assert (
        "Mobile keeps local enable, disable, and pin controls." in mobile_settings_src
    )


def test_mobile_settings_keep_local_plugin_enablement_controls() -> None:
    mobile_settings_src = Path("src/row_bot/ui/mobile_settings.py").read_text(
        encoding="utf-8"
    )

    assert "_can_enable_plugin" in mobile_settings_src
    assert "plugin_state.set_plugin_enabled" in mobile_settings_src
    assert 'button_label = "Disable" if enabled else "Enable"' in mobile_settings_src
    assert '["All", "Enabled", "Disabled", "Setup needed"]' in mobile_settings_src
    assert (
        "Mobile keeps installed plugin enable and disable controls."
        in mobile_settings_src
    )


def test_pairing_origin_prefers_reachable_remote_candidate() -> None:
    candidates = [
        {
            "access_mode": "localhost",
            "available": True,
            "url": "http://127.0.0.1:8080",
        },
        {
            "access_mode": "lan",
            "available": False,
            "url": "http://192.168.68.87:8080",
        },
        {
            "access_mode": "ngrok",
            "available": True,
            "url": "https://viability-pulverize-proxy.ngrok-free.dev",
        },
    ]

    assert (
        _preferred_pairing_origin(candidates)
        == "https://viability-pulverize-proxy.ngrok-free.dev"
    )


def test_pairing_origin_falls_back_to_localhost_when_no_remote_is_ready() -> None:
    candidates = [
        {
            "access_mode": "localhost",
            "available": True,
            "url": "http://127.0.0.1:8080",
        },
        {
            "access_mode": "lan",
            "available": False,
            "url": "http://192.168.68.87:8080",
        },
    ]

    assert _preferred_pairing_origin(candidates) == "http://127.0.0.1:8080"


def test_pairing_origin_uses_full_mobile_route_priority() -> None:
    candidates = [
        {"access_mode": "localhost", "available": True, "url": "http://127.0.0.1:8080"},
        {"access_mode": "lan", "available": True, "url": "http://192.168.68.87:8080"},
        {
            "access_mode": "tailscale-direct",
            "available": True,
            "url": "http://100.64.0.10:8080",
        },
        {
            "access_mode": "ngrok",
            "available": True,
            "url": "https://rowbot.ngrok-free.dev",
        },
        {
            "access_mode": "tailscale-serve",
            "available": True,
            "url": "https://rowbot.tail.ts.net",
        },
    ]

    assert _preferred_pairing_origin(candidates) == "https://rowbot.tail.ts.net"
    assert _preferred_pairing_origin(candidates[:-1]) == "https://rowbot.ngrok-free.dev"
    assert _preferred_pairing_origin(candidates[:3]) == "http://100.64.0.10:8080"
    assert _preferred_pairing_origin(candidates[:2]) == "http://192.168.68.87:8080"
