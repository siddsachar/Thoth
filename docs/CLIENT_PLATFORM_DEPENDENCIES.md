# Client dependency review

The frontend is one private application with exact direct versions in
`frontend/package.json` and a single npm lockfile. Node 24.15.0 is build/test
time only. Install with `npm ci --ignore-scripts`; `.npmrc` disables lifecycle
scripts, automatic audit, funding and update notifications. Dependencies and
fonts are never fetched by the production browser. Python dependencies remain
authoritative in pyproject.toml, uv.lock and its generated export.

Reviewed on 2026-09-09 using published npm package metadata, upstream docs,
installed distribution source and an explicit npm audit. React/React DOM,
React Router, Radix primitives and react-resizable-panels are MIT; Lucide is
ISC. TypeScript/Playwright are Apache-2.0, axe is MPL-2.0, other direct
development tools are MIT. The lockfile records every transitive version and
integrity hash; the local gate includes exact inventory and audit results.

| Candidate | Decision and owned adaptation |
| --- | --- |
| Existing NiceGUI/Quasar primitives | Retained for the default host. They depend on NiceGUI element/runtime ownership and cannot supply a standalone React client or its focus lifecycle. No legacy CSS is copied. |
| Radix dialog/menu/popover/tabs/tooltip/toast | Selected individually, avoiding unused components. Upstream supplies ARIA patterns, focus trap/return, Escape, outside interaction and scroll locking. Native HTML selects retain browser behavior. One Radix modal scope supplies dialogs, sheets and alert-dialog semantics; confirmation suspends mounted task content and defaults focus to Cancel. Integrated browser evidence remains required. |
| react-resizable-panels 4.12.4 | Selected for pointer capture, touch, separator semantics and size constraints. Row-Bot owns persisted geometry and the specified 16/48px keyboard increments; conversation children retain identity. No floating window manager. |
| React Router | One basename `/app-v2/`, lazy secondary surfaces and explicit unknown-route recovery. No capability-specific app packages. |
| Lucide | Bundled SVG icons with text/accessible names. No icon/font CDN. |

[Radix accessibility](https://www.radix-ui.com/primitives/docs/overview/accessibility),
[dialog behavior](https://www.radix-ui.com/primitives/docs/components/dialog),
[pane API](https://github.com/bvaughn/react-resizable-panels/blob/main/README.md),
and [Vite requirements](https://vite.dev/guide/) inform the review; upstream
claims do not replace Row-Bot browser tests. Exact versions are frozen in the
lockfile rather than inferred from these moving documentation pages.

The first resolution refused TypeScript 7 because typescript-eslint requires
TypeScript below 6.1. The compatible compiler is 6.0.3; no peer checks were
overridden. Vitest 4.1.11 is the selected test runner, with Vite 7.3.6 and
plugin-react 5.2.0. It fixes the
[redirect mock advisory](https://github.com/vitest-dev/vitest/security/advisories/GHSA-82fw-gwwq-j7x9).
The temporary 3.2.7 resolution is superseded, and its audit is historical evidence.

Vitest includes optional OpenTelemetry instrumentation. Its package archive and
`Traces` constructor were reviewed before execution: SDK/API imports are gated
by `enabled`. The user explicitly accepted this dependency on 2026-09-09 with
telemetry disabled. `vitest.config.ts` sets `experimental.openTelemetry.enabled`
to false, provides no SDK path, and disables API, browser and watch modes.
No OpenTelemetry API, SDK or exporter package is installed. No Row-Bot prompts,
files, memories, secrets, screenshots, tool arguments or channel content may be
sent through dependency telemetry. This is third-party test instrumentation,
not Row-Bot telemetry, and it stays disabled. Future upgrades must repeat the
review before execution. Enabling instrumentation is outside this acceptance.

Production dependencies render locally and do not contact an external service.
The one protocol adapter uses authenticated same-origin requests. Vite HMR is
development-only and binds loopback; Playwright browser installation and npm
audits are explicit developer operations, never application startup actions.
axe runs locally in the test browser without an external reporter. No remote
font, analytics, error-reporting or CDN service is enabled.

Build/install scripts stay disabled: published compiled JS and platform-specific
optional binaries are consumed directly. Do not run dependency `prepare`,
`postinstall`, browser download or update commands implicitly. CI provisions its
declared browser engines explicitly. Bundle size and reproducibility are measured
at the phase gate; package size alone is not performance acceptance. Removing a
dependency requires replacing its concrete consumer and rerunning the relevant
keyboard, theme, layout, overlay and browser evidence.
