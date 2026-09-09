# Unified client hosting

The default `/` route remains NiceGUI. The optional `/app-v2/` shell and
authenticated `/api/v1/` routes run in that same Python process and use the
existing access middleware, application services and data. Selecting a client
does not copy or migrate user data. Node is needed for build/test work only.

| Route | Behavior |
| --- | --- |
| `/` | Existing NiceGUI application |
| `/app-v2` | Redirect to `/app-v2/` |
| `/app-v2/` and extensionless HTML navigation below it | Local shell, `Cache-Control: no-store` |
| `/app-v2/assets/<hashed-name>` | Verified local assets, private immutable caching |
| `/api/v1/` including events | Existing authenticated protocol, origin/CSRF/revocation controls |
| `/connect`, `/mobile/pair`, existing mobile session/connect routes | Existing compatibility behavior and access classification |

`row_bot.client_assets.install_client_assets(app)` adds only the two opt-in
routes. It does not replace lifespan or exception handlers. The existing
middleware authenticates navigation/assets just as it authenticates the API.
The helper itself also requires the resolved access context; installing it
without access middleware fails closed. Unauthenticated server-mode navigation
goes through `/connect`; direct desktop loopback retains its existing policy.

In a source checkout the default asset root is `frontend/dist`. Installed and
frozen builds use `row_bot/static/client-v2` beside the Python modules.
`asset_root=Path(...)` is an explicit test/composition override, not an HTTP
parameter. Missing or invalid assets return a safe 503 only for the preview;
NiceGUI remains available. Build assets before starting the backend. Rebuilding
requires restarting the **owned development host** to load the new immutable
snapshot; never restart a daily-use application as part of a test.

The production inventory is `asset-manifest.json`:

```json
{"version":1,"files":{"index.html":{"sha256":"<64 lowercase hex>","size":123},"assets/index-<Vite hash>.js":{"sha256":"<64 lowercase hex>","size":456}}}
```

The build also produces `.vite/manifest.json`. Both files are private build
metadata, never served. The inventory includes `index.html` and every permitted
hashed script/style/font/image; source maps, arbitrary files and directories
are not exposed. The loader checks Vite import relationships, file sizes and
SHA-256 digests before serving. It rejects traversal, encoded separators,
symlinks/junctions/reparse points and unrecognized asset names. It bounds each
asset to 8 MiB, the HTML shell to 256 KiB, the complete snapshot to 32 MiB and
the inventory to 512 files. These are frontend build bounds, not user attachment
limits. Verified bytes remain immutable in memory; later path changes cannot
serve different bytes under the old digest.

Shell CSP limits scripts to local assets and exact hashes of the build's
inline bootstrap. Frames and objects are forbidden; styles permit inline
geometry needed by accessible overlay primitives. `nosniff`, frame denial and
no-referrer headers apply. Unknown asset paths return 404 instead of HTML.
History fallback requires extensionless `Accept: text/html` navigation. There
is no service worker, external font, runtime CDN or authenticated-response cache
in this foundation.

For local development, use the frontend's documented development command and
its loopback proxy. The proxy must rewrite the Origin to the selected private
backend origin together with Host and must only target that explicitly owned
loopback process. It must never broaden the production allowed-host/origin
configuration, add CORS wildcards, forward arbitrary destinations, or expose
HMR publicly. A production build uses same-origin relative API URLs without a
proxy. See `frontend/README.md` for exact commands and build/payload checks.

Installer integration copies the verified build and its two manifests into
the existing static payload. Python/runtime extras, NiceGUI assets, launch
defaults, paired-session database and installed-data readers remain owned by
their current modules. Real clean-machine installer/upgrade/rollback checks
are separate platform gates.
