# Row-Bot Docs Site

This Docusaurus project is the source for Row-Bot's public documentation. Its
build is synchronized into the generated documentation-owned paths under
`docs/`, which GitHub Pages publishes alongside the hand-curated marketing
pages at the site root.

Do not edit generated files under `docs/docs`, `docs/assets`, `docs/img`,
`docs/pagefind`, or `docs/search` by hand. Change this source and use the
documented build/synchronization workflow. The root marketing files
(`index.html`, `features.html`, `architecture.html`, `contact.html`,
`404.html`, `site.css`, `site.js`, and their media) are deliberately outside
the synchronization script's ownership.

## Local Preview

```powershell
cd docs-site
npm ci
npm run start
```

## Validation

```powershell
python scripts\docs\collect_inventory.py --out docs-build\inventory
python scripts\docs\capture_real_ui_screenshots.py --validate-only
python scripts\docs\validate_public_docs.py
cd docs-site
npm run build:ci
```

### Build-Dependency Audit Note

As of 2026-08-27, `npm audit --omit=dev` reports 17 high-severity dependency
entries that all roll up to two denial-of-service advisories for
`image-size@2.0.2` through Docusaurus's MDX loader:
[GHSA-w3rx-r6r6-pgpr](https://github.com/advisories/GHSA-w3rx-r6r6-pgpr)
and [GHSA-5p2g-fcmc-qvqq](https://github.com/advisories/GHSA-5p2g-fcmc-qvqq).
The audit currently reports no installable fix. This dependency runs while
building the static documentation from repository-owned images; it is not part
of the Row-Bot application runtime or the published static JavaScript bundle.

Do not process unreviewed ICNS, JXL, HEIF, or HEIC files in the documentation
build. Re-run the audit before each release and remove this note as soon as a
compatible patched Docusaurus dependency is available. Do not use a forced
audit fix or an unreviewed fork merely to silence the report.

## Publishable GitHub Pages Output

The checked-in `docs/` tree must be generated with the pinned Linux, CPU
architecture, and Node.js container used by `.github/workflows/docs.yml`.
Pagefind and bundled asset filenames are platform-dependent, so output
generated in a different environment can look correct locally but fail
`sync_github_pages.py --check` on CI.

The repository's `.gitattributes` keeps documentation build inputs and
generated text output at LF line endings on every platform. Existing Windows
clones created before that policy was added should refresh the affected files
from Git before regenerating the published tree, while preserving or committing
any local documentation edits.

On Windows, use Docker Desktop from the repository root to produce the
canonical artifact:

```powershell
docker run --rm --platform linux/amd64 --mount "type=bind,src=$PWD,dst=/repo" `
  --env NO_UPDATE_NOTIFIER=1 node:20.20.2-bookworm `
  bash -lc "ln -sf /usr/bin/python3 /usr/local/bin/python && cd /repo/docs-site && npm ci && npm run build:ci && cd /repo && python scripts/docs/sync_github_pages.py"
```

Run the same command with `sync_github_pages.py --check` after generation, or
let the Docs workflow perform the final Linux check. Commit all resulting
changes under the synchronization-owned paths; do not hand-edit them.

Full screenshot recapture is local-only for now:

```powershell
python scripts\docs\seed_real_app_demo_data.py --scenario full --data-dir docs-build\demo-data
python scripts\docs\capture_real_ui_screenshots.py --scenario full
```
