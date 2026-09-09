# Row-Bot client foundation

This is one React application. NiceGUI remains the default at `/`; the client is
opt-in at `/app-v2/`. Production runs local built assets and the existing Python
backend. Node is required only for development, tests and installer assembly.

Use Node **24.15.0**, npm **11**, and the checked-in lockfile:

```sh
cd frontend
npm ci --ignore-scripts --no-audit --no-fund
npm run dev
```

Vite binds `127.0.0.1:5173`. Its `/api/v1` proxy targets
`http://127.0.0.1:8080` by default. `ROW_BOT_DEV_BACKEND` may specify another plain
HTTP loopback origin. Credentials, paths, query strings and remote targets are
rejected. The proxy rewrites Origin only in this development server; production
authentication and origin policy are unchanged. Use the usual approved local
backend launch procedure and a private data directory for development fixtures.

```sh
npm run lint
npm run format:check
npm run typecheck
npm test
npm run build
npm run build:verify
node scripts/asset-manifest.mjs dist --package
```

From the repository root, `python scripts/run_client_checks.py` runs all frontend
quality checks and two independent production builds without installing anything.
`uv run python scripts/run_test_matrix.py client-foundation` selects the same
checks. PR/release and changed frontend paths include this lane. Browser tests
use `tests/browser/client_foundation/run_browser.py`; see the testing guide.

`dist/asset-manifest.json` inventories the current build. Previous build files
are retained but are never served unless present in this validated inventory.
`--package` copies the current inventory and private Vite manifest into
`src/row_bot/static/client-v2`. Installer assembly uses `--package-dir` with an
absent destination and fails instead of merging an earlier stage. Do not ship
`node_modules`, source maps or fixture builds.

Only an explicit build with `VITE_ENABLE_FIXTURES=1` plus a `?fixture=normal`
query loads synthetic transport recordings and its debug hook. Other scenarios
are `incompatible`, `unauthorized` and `disconnected`. The capability adapter is
still the real browser adapter; `fixturePlatform=fake` explicitly selects its
deterministic fake. A normal production build eliminates all fixture imports
and globals. Never enable this flag for a distributed build.

Keep module ownership simple:

- `src/api`: the only generated wire client, authentication and event owner.
- `src/platform`: browser/native capability operations; backend media authority.
- `src/ui`: shared tokens, controls and overlay behavior; no domain imports.
- `src/features`: typed presentation and the panel/settings metadata models.
- `src/main.tsx`, `src/runtime.tsx`: one root, router and application lifetime.

Review [dependencies](../docs/CLIENT_PLATFORM_DEPENDENCIES.md) before upgrades.
Vitest's optional OpenTelemetry support was accepted only with instrumentation
disabled and no telemetry packages/exporter. Do not enable it or add reporting
integrations. Lifecycle scripts stay disabled.

Phase 3 handoffs: [protocol and generated-client updates](../docs/CLIENT_PLATFORM_PROTOCOL.md),
[panel contributions](../docs/CLIENT_PLATFORM_PANELS.md),
[product system](../docs/CLIENT_PLATFORM_PRODUCT_SYSTEM.md),
[native capabilities](../docs/CLIENT_PLATFORM_NATIVE.md),
[hosting](../docs/CLIENT_PLATFORM_HOSTING.md),
[payload validation](../docs/CLIENT_PLATFORM_BUILD.md), and
[browser evidence](../docs/CLIENT_PLATFORM_TESTING.md).
