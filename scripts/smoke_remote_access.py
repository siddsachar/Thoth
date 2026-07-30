"""Deterministic, offline smoke check for access CLI and server-mode contracts."""

from __future__ import annotations

import gc
from io import StringIO
import json
from pathlib import Path
import tempfile
from urllib.parse import parse_qs, urlsplit

from row_bot.access.cli import (
    build_remote_access_parser,
    dispatch_access_command,
    resolve_serve_options,
)
from row_bot.access.service import AccessService
from row_bot.access.store import AccessStore


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="row-bot-remote-access-smoke-") as raw_dir:
        data_dir = Path(raw_dir)
        parser = build_remote_access_parser()
        invite_args = parser.parse_args(
            (
                "access",
                "invite",
                "--data-dir",
                str(data_dir),
                "--layout",
                "desktop",
                "--origin",
                "https://row-bot-smoke.invalid",
                "--json",
            )
        )
        invitation_output = StringIO()
        invitation_errors = StringIO()
        if (
            dispatch_access_command(
                invite_args,
                stdout=invitation_output,
                stderr=invitation_errors,
            )
            != 0
        ):
            raise RuntimeError("invitation smoke failed")
        invitation = json.loads(invitation_output.getvalue())["invitation"]
        raw_token = parse_qs(
            urlsplit(invitation["invitation_url"]).query
        )["invitation"][0]

        service = AccessService(AccessStore(data_dir / "mobile.db"))
        claim = service.claim_invitation(
            raw_token,
            intended_origin="https://row-bot-smoke.invalid",
            display_name="Smoke browser",
        )
        if service.validate_session(claim.session_token) is None:
            raise RuntimeError("session validation smoke failed")

        list_output = StringIO()
        list_errors = StringIO()
        if (
            dispatch_access_command(
                parser.parse_args(("access", "list", "--json")),
                service=service,
                stdout=list_output,
                stderr=list_errors,
            )
            != 0
        ):
            raise RuntimeError("access list smoke failed")
        serialized_list = list_output.getvalue()
        if raw_token in serialized_list or claim.session_token in serialized_list:
            raise RuntimeError("access list exposed a raw credential")

        serve = resolve_serve_options(
            parser.parse_args(("serve",)),
            environ={"ROW_BOT_DATA_DIR": str(data_dir)},
        )
        if (
            serve.deployment_mode.value != "server"
            or serve.open_browser
            or serve.tray
            or serve.splash
            or serve.auto_start_ollama
        ):
            raise RuntimeError("serve defaults are not safely headless")

        # sqlite3 context managers commit but do not close; collect short-lived
        # connection objects before Windows removes the isolated smoke tree.
        del service
        gc.collect()

    print("remote access smoke: 1 invitation, 1 session, no credential leakage")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
