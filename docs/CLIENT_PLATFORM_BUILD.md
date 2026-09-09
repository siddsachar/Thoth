# Client build and dependency consistency

The client uses Node only for development, validation and asset generation.
Python remains the application host. The existing NiceGUI, access, storage and
installer paths retain their owners; the optional client is mounted at
`/app-v2/`. See [hosting](CLIENT_PLATFORM_HOSTING.md) for delivery and cache rules.

## Verify local output

Build with the pinned frontend toolchain and lockfile:

```powershell
cd frontend
npm ci --ignore-scripts --no-audit --no-fund
npm run check
npm run build:verify
node scripts/asset-manifest.mjs dist --package
cd ..
python scripts/verify_client_assets.py --root frontend/dist
python scripts/verify_client_assets.py --root src/row_bot/static/client-v2 --compare frontend/dist
```

`npm ci` is a dependency installation and should follow the repository's review
and network rules. The Python verification commands are read-only checks; they
do not build, fetch, copy, delete or launch the application. The frontend build
generates `index.html`, hashed assets, `asset-manifest.json` and the private
`.vite/manifest.json`. Packaging must preserve both manifests. Neither manifest
is an HTTP resource.

The Python verifier reuses `row_bot.client_assets.load_client_assets`, so the
build, staging and application serve the same validated bytes. It reports the
verified asset count, byte count and a deterministic SHA-256 of the sorted
asset-name/digest/size inventory. Exit 0 means verification succeeded; exit 1
means a required local build is missing, invalid or different from the requested
comparison source. Failure output omits payload contents and filesystem paths.

`--compare` validates both roots and compares their complete verified asset maps
and the exact bytes of both private manifests. JSON formatting differences also
fail comparison; staged manifests must be copied without changes.
`--strict` additionally rejects files and directories outside the generated
inventory, source maps, tooling, linked paths and reparse points. Development
output may retain old hashed files, but the runtime only exposes inventoried
assets. The persistent package source can also retain old hashed files; compare its current inventory without `--strict`. Installers use `--package-dir` to copy only current assets into a fresh destination, which must pass strict verification. A failed
strict check does not authorize automatic recursive deletion of old output.

## Preserve packaging boundaries

Installers copy `src/row_bot` through the existing application payload manifest.
The staged client belongs at `src/row_bot/static/client-v2`; Node, frontend source,
tests and `node_modules` do not belong in the installed application. Wheel
package-data declarations include the HTML shell, both manifests (including the
hidden `.vite` directory) and hashed asset files. The build-only setuptools
`ClientBuildPy` hook selects only the current inventory and exact private manifest
bytes; stale source assets, source maps and unlisted files do not enter the wheel.
It uses standard-library and setuptools build dependencies, without importing
the application, FastAPI or NiceGUI. Runtime serving still validates the payload
independently through `row_bot.client_assets`.

Each non-editable build creates fresh generated library and wheel staging
directories inside the project's build directory. Earlier output is preserved,
including output from failed builds, and cannot leak into the new wheel through
setuptools' recursive install copy. Missing or malformed required assets fail
before staging begins. Metadata and editable queries permit an absent frontend
payload so the existing NiceGUI source environment can still be installed before
the opt-in frontend is built. The hook includes its own source in an sdist; it is
build tooling and is not added to the installed Python package.

After a production frontend build, verify the source payload, build a wheel with
the already installed build dependencies, and inspect the archive's
`row_bot/static/client-v2/` entries against the inventory. Require exact file-set,
asset-byte and private-manifest-byte equality. Do not infer wheel contents from a
successful source-directory check alone.

Build jobs verify the staged assets before installer compilation or signing.
For a staged platform application, run the source checkout's verification script
with `--root` pointing at that application's `src/row_bot/static/client-v2` and
`--compare` pointing at the original validated frontend output. The verification
script is build tooling and does not need to enter the runtime script inventory.
Actual clean-machine install, upgrade, rollback, signing and native OS behavior
remain separate platform/release checks.

## Compare complete dependency requirements

`pyproject.toml` is the dependency authority. Check that its `all` extra contains
exactly the normalized union of its feature extras:

```powershell
python scripts/dependency_requirements.py
```

The checker uses `packaging.Requirement`, normalized package/extra names,
`SpecifierSet` equality, parsed marker serialization and complete direct URLs.
It keeps version constraints, requested dependency extras and platform markers.
Matching a package name alone is insufficient. Different constraints or markers
are reported for review even if a resolver might choose the same version today.

The check also evaluates ten explicit marker environments: Python 3.12 and 3.13
on Windows AMD64, macOS arm64/x86_64 and Linux x86_64/aarch64. These deterministic
fixtures exercise marker selection; they do not prove wheel availability,
native-library loading or operating-system behavior. No resolver, installation
or live import of optional providers runs during this metadata check.

Exit 0 means the feature union and full distribution match. Exit 1 reports
missing/unexpected package names and the number of affected platform profiles.
Exit 2 means the project metadata could not be parsed. Diagnostics omit direct
URLs because a URL could contain credentials.

After an intentional dependency declaration change, retain the canonical
`uv lock`, generated requirements export, locked sync, runtime dependency
verification and PR matrix workflow from `AGENTS.md`. The normalized check adds
declaration coverage; it does not replace lock/export or platform import checks.
