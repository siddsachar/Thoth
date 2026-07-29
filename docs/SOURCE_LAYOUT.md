# Source Layout And Packaging

Row-Bot runtime source lives in `src/row_bot/`. New application modules and
runtime packages should be added there and imported as `row_bot.*`.

The repository root still keeps a small supported launch surface:

- `app.py` runs `row_bot.app` from a checkout or packaged payload.
- `launcher.py` imports and calls `row_bot.launcher.main`.
- Root payload assets such as `static/`, `sounds/`, `bundled_skills/`,
  `tool_guides/`, `pyproject.toml`, `uv.lock`, generated `requirements.txt`,
  and `row-bot.ico` remain at the root for the v4 packaging contract.

Do not add implementation code to the root wrappers. If code needs to be shared
with tests, installers, or scripts, place it under `src/row_bot/` or in a
non-runtime helper under `scripts/`.

## Running From Source

From the repository root:

```powershell
python -m venv .venv
.\.venv\Scripts\activate
python -m pip install "uv>=0.7,<1.0"
uv sync --locked --all-extras --group test
uv run python launcher.py
```

`pyproject.toml` owns direct dependencies and extras. `requirements.txt` is generated from `uv.lock` with `python scripts/export_locked_requirements.py` for pip-based installers and should not be edited by hand.

Authenticated server/headless mode:

```powershell
uv run python launcher.py serve --port 8080
```

The installed entry point is `row-bot serve`. The older
`python launcher.py --server --no-open` combination remains a deprecated
compatibility path; new scripts and service definitions should use the
subcommand. Server mode requires an authenticated browser even over loopback.

Direct NiceGUI launch remains supported through the root wrapper:

```powershell
python app.py
```

Tests import package modules through `pytest.ini`, which sets `pythonpath =
src`. Ad hoc scripts that import `row_bot.*` directly should either run through
the root launchers, set `PYTHONPATH=src`, or insert the checkout's `src/`
directory explicitly.

## Remote Access And Server Ownership

Remote-access implementation is intentionally separated by responsibility:

- `src/row_bot/access/` owns deployment configuration, request provenance,
  origin/Host/proxy checks, invitations, devices, sessions, cookies,
  capabilities, diagnostics, Tailscale Serve control, CLI helpers, and access
  routes.
- `src/row_bot/ui/remote_access_settings.py` owns the owner-facing Remote
  Access settings surface. `src/row_bot/ui/access_context.py` exposes the
  request-scoped capability boundary used by UI handlers.
- `src/row_bot/mobile/` keeps the companion UI and compatibility imports. The
  versioned access policy and durable store live under `row_bot.access`; do not
  reintroduce a second mobile-only authorization policy.
- `src/row_bot/app.py`, `src/row_bot/app_port.py`, and
  `src/row_bot/launcher.py` are shared integration points for deployment mode,
  middleware, child-process restart, and CLI dispatch.
- `deploy/docker/` contains the hardened image, loopback-published Compose
  example, and operator runbook. `deploy/reverse-proxy/` and `deploy/systemd/`
  contain reviewed examples for operator-managed server deployments.
- `scripts/smoke_remote_access.py` is the credential-leak-safe access smoke
  harness. It must always use an isolated data directory.

The physical access database remains `mobile.db` so existing companion records
can migrate in place. `access_routes.json` stores the durable local/LAN listen
choice. `tailscale_serve_ownership.json` stores no bearer credential; it proves
the exact private Serve route Row-Bot created so startup can apply only the
matching loopback proxy policy and disable only an unchanged owned route. All
three files are private local state under `ROW_BOT_DATA_DIR`; none belongs in
source control, images, or test fixtures containing real user data.

Source-to-test ownership for these paths lives in
`tests/helpers/source_test_map.py`. Focused coverage belongs in
`tests/subsystem/access/`, `tests/integration/access/`, and the deployment
contracts under `tests/contracts/installers/`.

## Packaging Payloads

`scripts/app_payload_manifest.py` is the source of truth for application payload
coverage. The manifest currently separates the package into:

- `payload_dirs`: recursive runtime package directories, currently
  `src/row_bot`.
- `asset_dirs`: root asset directories copied beside the launchers, currently
  `static`, `sounds`, `bundled_skills`, and `tool_guides`.
- `root_python_files`: supported root Python launch wrappers discovered from the
  repository root, excluding debug, test, and harness files.
- `root_files`: root files required by packaged apps, including
  `pyproject.toml`, `uv.lock`, `requirements.txt`, and `row-bot.ico`.
- `runtime_script_files`: package-time runtime verification scripts.

Windows packaging uses `installer/row_bot_setup.iss` to recursively include
`src/row_bot`. macOS and Linux builders call `scripts/app_payload_manifest.py`
for root files, runtime scripts, payload directories, and asset directories.
Packaging tests in `tests/test_linux_support.py` and `tests/test_suite.py`
assert those contracts.

Because `src/row_bot` is recursive, the 4.5.0 Agent execution budget and
settings modules, native Computer Use package and pinned runtime manifest,
cache-only embedding fallback, access/server package, Remote Access UI, mobile
companion, cancellation helpers, provider transports, and channel streaming
modules require no per-file installer entries. Deployment examples under
`deploy/` are source-distribution/operator artifacts rather than runtime Python
packages. `tests/test_linux_support.py` keeps required runtime packages in the
cross-platform payload inventory, while
`tests/subsystem/installer/test_computer_use_package_data.py` verifies that the
Computer Use JSON manifest survives both wheel and installer packaging.

## Local Cleanup Candidates

These paths are generated local state and should stay untracked:

- `installer/build/`
- `dist/`
- `.tmp/`, `.tmp_pytest/`, `.testtmp/`, `.pytest_cache/`, and `test-results/`
- `.streamlit/` from the old Streamlit app
- `src/row_bot/channels/whatsapp_bridge/node_modules/`

Do not recursively delete ignored/generated directories during implementation
work unless the owner explicitly confirms it is safe. Report them as cleanup
candidates instead.
