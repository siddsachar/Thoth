from __future__ import annotations

from row_bot.app_port import ROW_BOT_HOST_ENV
from row_bot.mobile.access_info import bind_allows_remote, build_access_info
from row_bot.mobile.tailscale import TailscaleState


def test_bind_allows_remote_only_for_reachable_hosts() -> None:
    assert bind_allows_remote(None) is False
    assert bind_allows_remote("127.0.0.1") is False
    assert bind_allows_remote("0.0.0.0") is True
    assert bind_allows_remote("192.168.1.10") is True


def test_access_info_marks_lan_setup_needed_without_remote_bind() -> None:
    info = build_access_info(port=8765, bind_host=None, lan_ips=("192.168.1.10",))
    lan = next(candidate for candidate in info["candidates"] if candidate["access_mode"] == "lan")

    assert info["remote_bind_enabled"] is False
    assert lan["url"] == "http://192.168.1.10:8765"
    assert lan["available"] is False
    assert lan["requires_bind"] is True
    assert "HTTP LAN" in lan["warning"]


def test_access_info_uses_loopback_default_from_shared_host_config(monkeypatch) -> None:
    monkeypatch.delenv(ROW_BOT_HOST_ENV, raising=False)

    info = build_access_info(port=8765, lan_ips=("192.168.1.10",))
    lan = next(candidate for candidate in info["candidates"] if candidate["access_mode"] == "lan")

    assert info["bind_host"] == "127.0.0.1"
    assert info["remote_bind_enabled"] is False
    assert lan["available"] is False
    assert lan["requires_bind"] is True


def test_access_info_honors_environment_remote_bind(monkeypatch) -> None:
    monkeypatch.setenv(ROW_BOT_HOST_ENV, " 0.0.0.0 ")

    info = build_access_info(port=8765, lan_ips=("192.168.1.10",))
    lan = next(candidate for candidate in info["candidates"] if candidate["access_mode"] == "lan")

    assert info["bind_host"] == "0.0.0.0"
    assert info["remote_bind_enabled"] is True
    assert lan["available"] is True
    assert lan["requires_bind"] is False


def test_access_info_explicit_none_override_remains_local_only(monkeypatch) -> None:
    monkeypatch.setenv(ROW_BOT_HOST_ENV, "0.0.0.0")

    info = build_access_info(port=8765, bind_host=None, lan_ips=("192.168.1.10",))

    assert info["bind_host"] == "localhost"
    assert info["remote_bind_enabled"] is False


def test_access_info_preserves_multiple_lan_candidates_without_remote_bind() -> None:
    info = build_access_info(port=8765, bind_host=None, lan_ips=("192.168.1.10", "10.0.0.7"))
    lans = [candidate for candidate in info["candidates"] if candidate["access_mode"] == "lan"]

    assert [candidate["url"] for candidate in lans] == [
        "http://192.168.1.10:8765",
        "http://10.0.0.7:8765",
    ]
    assert all(candidate["available"] is False for candidate in lans)
    assert all(candidate["requires_bind"] is True for candidate in lans)


def test_access_info_includes_tailscale_and_ngrok_candidates() -> None:
    state = TailscaleState(
        installed=True,
        logged_in=True,
        tailscale_ips=("100.64.0.10",),
        magicdns_url_candidates=("http://desktop.tail.ts.net:8765",),
        serve_enabled=True,
        serve_url="https://desktop.tail.ts.net",
    )

    info = build_access_info(
        port=8765,
        bind_host="0.0.0.0",
        lan_ips=(),
        ngrok_url="https://rowbot.ngrok-free.app",
        tailscale_state=state,
    )

    urls = {candidate["url"] for candidate in info["candidates"]}
    assert "http://100.64.0.10:8765" in urls
    assert "http://desktop.tail.ts.net:8765" in urls
    assert "https://desktop.tail.ts.net" in urls
    assert "https://rowbot.ngrok-free.app" in urls
