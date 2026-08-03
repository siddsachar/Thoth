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

Full screenshot recapture is local-only for now:

```powershell
python scripts\docs\seed_real_app_demo_data.py --scenario full --data-dir docs-build\demo-data
python scripts\docs\capture_real_ui_screenshots.py --scenario full
```
