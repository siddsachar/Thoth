from __future__ import annotations

from datetime import datetime, timezone
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
from urllib.parse import parse_qs, urlsplit

from row_bot.access.cli import (
    build_remote_access_parser,
    dispatch_access_command,
)
from row_bot.access.models import AccessProfile
from row_bot.access.service import AccessService
from row_bot.access.store import AccessStore


NOW = datetime(2026, 7, 27, 9, 0, tzinfo=timezone.utc)
ORIGIN = "https://row-bot.example"


def _parse(*arguments: str):
    return build_remote_access_parser().parse_args(arguments)


def _dispatch(args, *, service=None, environ=None):
    stdout = StringIO()
    stderr = StringIO()
    code = dispatch_access_command(
        args,
        service=service,
        environ=environ,
        stdout=stdout,
        stderr=stderr,
        now=NOW,
    )
    return code, stdout.getvalue(), stderr.getvalue()


def test_parser_exposes_stable_serve_and_access_help(capsys) -> None:
    parser = build_remote_access_parser()

    help_text = parser.format_help()
    assert "{serve,access}" in help_text
    assert "authenticated headless server" in help_text
    assert "authenticated device access" in help_text

    invite = _parse(
        "access",
        "invite",
        "--profile",
        "computer",
        "--origin",
        ORIGIN,
        "--temporary",
        "--json",
    )
    assert invite.command == "access"
    assert invite.access_command == "invite"
    assert invite.profile == "computer"
    assert invite.temporary is True
    assert invite.json_output is True
    assert capsys.readouterr().out == ""


def test_importing_access_cli_does_not_import_nicegui() -> None:
    code = (
        "import sys; "
        "before=set(sys.modules); "
        "import row_bot.access.cli; "
        "after=set(sys.modules)-before; "
        "raise SystemExit(1 if any(n == 'nicegui' or n.startswith('nicegui.') "
        "for n in after) else 0)"
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[3],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_invite_json_emits_secret_link_once_and_persists_only_hashes(tmp_path) -> None:
    args = _parse(
        "access",
        "invite",
        "--data-dir",
        str(tmp_path),
        "--profile",
        "computer",
        "--origin",
        ORIGIN,
        "--temporary",
        "--name",
        "Office browser",
        "--json",
    )

    code, output, errors = _dispatch(args)

    assert code == 0
    assert errors == ""
    payload = json.loads(output)
    invitation = payload["invitation"]
    assert invitation["profile"] == "computer"
    assert invitation["access_profile"] == "owner"
    assert invitation["session_lifetime"] == "temporary"
    assert invitation["name"] == "Office browser"
    assert output.count("rbi_") == 1
    raw_token = parse_qs(
        urlsplit(invitation["invitation_url"]).query
    )["invitation"][0]
    assert raw_token.startswith("rbi_")
    assert raw_token not in (tmp_path / "mobile.db").read_bytes().decode(
        "latin-1",
        errors="ignore",
    )


def test_text_invite_prints_one_secret_link(tmp_path) -> None:
    args = _parse(
        "access",
        "invite",
        "--data-dir",
        str(tmp_path),
        "--profile",
        "companion",
        "--origin",
        ORIGIN,
    )

    code, output, errors = _dispatch(args)

    assert code == 0
    assert errors == ""
    assert "Profile: companion" in output
    assert output.count("rbi_") == 1
    assert "token:" not in output.lower()


def test_list_never_serializes_verifiers_or_raw_tokens(tmp_path) -> None:
    service = AccessService(AccessStore(tmp_path / "mobile.db"))
    created = service.create_invitation(
        profile=AccessProfile.OWNER,
        intended_origin=ORIGIN,
        now=NOW,
    )
    claim = service.claim_invitation(
        created.token,
        intended_origin=ORIGIN,
        display_name="Laptop",
        now=NOW,
    )

    code, output, errors = _dispatch(
        _parse("access", "list", "--json"),
        service=service,
    )

    assert code == 0
    assert errors == ""
    payload = json.loads(output)
    assert payload["devices"][0]["display_name"] == "Laptop"
    assert payload["sessions"][0]["id"] == claim.session.id
    assert created.token not in output
    assert claim.session_token not in output
    assert "token_hash" not in output
    assert "token_salt" not in output
    assert "secret_hash" not in output
    assert "secret_salt" not in output


def test_revoke_and_revoke_all_are_explicit(tmp_path) -> None:
    service = AccessService(AccessStore(tmp_path / "mobile.db"))
    created = service.create_invitation(
        profile=AccessProfile.OWNER,
        intended_origin=ORIGIN,
        now=NOW,
    )
    claim = service.claim_invitation(
        created.token,
        intended_origin=ORIGIN,
        display_name="Laptop",
        now=NOW,
    )

    denied, denied_output, denied_error = _dispatch(
        _parse("access", "revoke-all", "--json"),
        service=service,
    )
    assert denied == 2
    assert denied_output == ""
    assert json.loads(denied_error)["error"] == "confirmation_required"
    assert service.validate_session(claim.session_token, now=NOW) is not None

    revoked, output, errors = _dispatch(
        _parse(
            "access",
            "revoke",
            claim.session.id,
            "--session",
            "--json",
        ),
        service=service,
    )
    assert revoked == 0
    assert errors == ""
    assert json.loads(output)["revoked"] is True
    assert service.validate_session(claim.session_token, now=NOW) is None


def test_doctor_json_redacts_malformed_secret_bearing_values(tmp_path) -> None:
    secret = "never-print-this-secret"
    args = _parse(
        "access",
        "doctor",
        "--data-dir",
        str(tmp_path),
        "--deployment-mode",
        "server",
        "--host",
        "0.0.0.0",
        "--public-url",
        f"https://operator:{secret}@row-bot.example/?token={secret}",
        "--trusted-proxy",
        f"{secret}/bad-cidr",
        "--json",
    )

    code, output, errors = _dispatch(args, environ={})

    assert code == 1
    assert errors == ""
    assert secret not in output
    report = json.loads(output)
    assert report["ok"] is False
    check_ids = {check["id"] for check in report["checks"]}
    assert {"public_url", "trusted_proxies", "public_binding"} <= check_ids


def test_doctor_does_not_create_access_database(tmp_path) -> None:
    args = _parse(
        "access",
        "doctor",
        "--data-dir",
        str(tmp_path),
        "--json",
    )

    code, output, errors = _dispatch(args, environ={})

    assert code == 0
    assert errors == ""
    assert json.loads(output)["ok"] is True
    assert not (tmp_path / "mobile.db").exists()
