from scripts.docs.collect_inventory import (
    collect_cli_options,
    collect_environment,
    collect_settings_controls,
)


def test_remote_access_cli_options_are_in_generated_inventory() -> None:
    rows = collect_cli_options()
    commands = {row["command"] for row in rows}

    assert "row-bot serve" in commands
    assert "row-bot access invite" in commands
    assert "row-bot access list" in commands
    assert "row-bot access revoke" in commands
    assert "row-bot access revoke-all" in commands
    assert "row-bot access doctor" in commands
    assert any(
        row["command"] == "row-bot serve" and row["option"] == "--public-url"
        for row in rows
    )
    assert any(
        row["command"] == "row-bot access invite" and row["option"] == "--layout"
        for row in rows
    )


def test_remote_access_environment_is_in_generated_inventory() -> None:
    variables = {row["variable"] for row in collect_environment()}

    assert {
        "ROW_BOT_DEPLOYMENT_MODE",
        "ROW_BOT_PUBLIC_URL",
        "ROW_BOT_ALLOWED_HOSTS",
        "ROW_BOT_TRUSTED_PROXY_CIDRS",
        "ROW_BOT_UNTRUSTED_FORWARDED_ACTION",
        "ROW_BOT_WORKERS",
        "ROW_BOT_SECRETS_DIR",
        "ROW_BOT_BROWSER_HEADLESS",
    } <= variables


def test_system_control_inventory_uses_current_remote_access_surface() -> None:
    system_rows = [
        row for row in collect_settings_controls() if row["tab"] == "System"
    ]
    labels = {row["label"] for row in system_rows}
    sources = {row["source"].split(":", 1)[0] for row in system_rows}

    assert {
        "Invite a device",
        "Check Tailscale status",
        "Review private route",
        "Allow local-network connections",
    } <= labels
    assert "src/row_bot/ui/remote_access_settings.py" in sources
    assert "src/row_bot/ui/mobile_access_settings.py" not in sources
