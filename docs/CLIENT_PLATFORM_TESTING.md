# Client platform browser validation

The Phase 2 shell and the existing NiceGUI UI share one real application
process in browser validation. Tests use a new data directory and scripted
provider/tool boundaries. They never attach to a user's running app or browser
profile. Node, Python dependencies and browsers must already be installed using
the repository's reviewed dependency process.

Build a dedicated local fixture bundle from the repository root. Preserve the
caller’s existing environment value when the build finishes:

```powershell
$fixtureBuildSetting = $env:VITE_ENABLE_FIXTURES
try {
    $env:VITE_ENABLE_FIXTURES = '1'
    npm --prefix frontend run build
} finally {
    if ($null -eq $fixtureBuildSetting) {
        Remove-Item Env:VITE_ENABLE_FIXTURES -ErrorAction SilentlyContinue
    } else {
        $env:VITE_ENABLE_FIXTURES = $fixtureBuildSetting
    }
}
```

Fixture transport is activated only by an explicit `fixture` query in that
dedicated bundle. The default route still uses the real local API. The final
production build must be rebuilt without the flag and pass `build:verify`;
never package the test bundle. Then run:

```powershell
.venv/Scripts/python.exe tests/browser/client_foundation/run_browser.py
```

The runner allocates its own loopback port, starts the existing
`tests/browser/client_platform/fixture_app.py`, identifies the actual listener
process, and invokes the frontend's local Playwright CLI. Its child environment
contains only required system launch variables, private data/workspace/cache
paths and synthetic control credentials. It disables autostart and external
network use. Only the exact owned child process trees are stopped. Generated
data is retained for inspection; the runner does not recursively clean existing
directories or synchronize the environment used by the development app.

The default configuration includes Chromium, Firefox and WebKit at 1440×900,
1280×720, 820×1180, 390×844 and 360×800. For an explicitly scoped local check with
an existing Edge installation:

```powershell
.venv/Scripts/python.exe tests/browser/client_foundation/run_browser.py --engine chromium --channel msedge
```

The runner uses the dedicated `.tmp/p2-browser-cache` browser installation;
it does not install engines or inherit a caller's browser-cache setting. Probe
already-installed engines with `probe_browsers.py` in the same directory. Each
probe has its own process deadline and cleanup, so one blocked launch does not
hide the other results. To use Edge for Chromium projects while retaining all
Firefox and WebKit projects, pass `--channel msedge` without `--engine`.

Engine overrides reduce the measured scope. A successful Edge run does not
certify Firefox, WebKit, macOS, Linux, physical mobile devices or the native
pywebview bridge. Missing required engines remain a named validation gap.
Additional Playwright arguments follow `--`, for example `-- --grep bootstrap`.

The unchanged legacy lifecycle assertions run through a destination wrapper:

```powershell
.venv/Scripts/python.exe tests/browser/client_foundation/run_legacy.py
```

It exercises real NiceGUI send/Stop, conversation switching/reload/two viewers,
approval/resume, socket reconnect and Developer/Designer entry with scripted
providers. The wrapper calls the unchanged Phase 1 runner's `main` and changes
only its newly allocated output/data paths, adding a complete Phase 2 source
fingerprint. All new evidence uses
`.local/evidence/unified-client-platform/phase-2/qa/`. Earlier frozen evidence
is preserved.

Each new browser run records source and generated-asset hashes before/after,
Python/browser/project identity, actual listener PID, server readiness and RSS,
test results, screenshots, console exceptions, and a request timeline containing
only path/status. Raw Playwright traces and videos are disabled because traces
can retain authentication headers. No cookie, CSRF proof, fixture-control token,
provider credential or user content belongs in published evidence. Browser
tests reject nonfixture network requests. Named fault scenarios record their
expected failures; other console errors fail validation.

Most functional tests abort external origins through Playwright routing.
Actual page lifecycle and startup performance tests use no routing: Playwright
globally intercepts WebKit requests and disables HTTP cache whenever a route
exists, even if a predicate matches only external URLs. Those tests verify the
real host's strict local-only Content Security Policy before navigation and fail
on observed external requests. The Back regression privately reuses the original
synthetic session proof to confirm the old subscription returns404/not_found;
credentials and cursors are never attached to evidence.

Real-host checks select a seeded conversation using the accepted API, exercise
offline/recovery and browser Back, and verify that the scripted producer was
never invoked. Explicit persisted page lifecycle events separately test observer
resumption; the actual Back check records whether the engine used its back/forward
cache or reloaded the page.

Shared primitive checks include labels/roles, automated WCAG scans, keyboard
focus entry/trap/return, cancel/confirm, scroll lock, compact transformations,
pointer and keyboard resizing, all supported appearances/accents, persisted and
migrated layouts, reduced preferences, forced colors and zoom. Screenshots must
be inspected independently; neither an automated scan nor zero document
overflow proves that every child control is usable.

The automated 200% layout check uses CSS zoom and is recorded as an emulation.
It does not replace physical-device or browser-chrome zoom review. Panel content
and modal footer checks also use hit testing to detect controls that occupy
space but are clipped or covered by another surface.
An additional Chromium case sends emulated touchStart/touchMove/touchEnd to the
actual wide-tablet dock separator; this does not certify a physical device.
Preference evidence records actual media-query/computed results per engine so
unsupported forced-colors emulation is not represented as an operating-system
accessibility pass.

Performance evidence separates server readiness from usable browser rendering.
Use at least five cold contexts, ten warm navigations and twenty interaction
repetitions; retain every sample and report p50/p95/max plus failures. Attribute
browser heap, browser process memory, server RSS and probe memory separately.
The runner's driver-descendant RSS includes browser processes and the Playwright
test worker; it is not a pure browser-heap measurement. Splitter checks verify
visible hit geometry and numeric ARIA ranges as well as pointer/keyboard behavior,
including collapse persistence and restoration of the same panel after reload.

The named resize-work calibration captures only renderer task, frame and user
timing categories through CDP. Its retained trace removes event arguments and
unrelated events; it contains no screenshots, network records or object snapshots.
Per-frame work unions overlapping top-level task intervals, clips them to every
frame/workload boundary, and reports script, style/layout and paint separately.
Actual committed DOM/ARIA geometry is checked alongside the timing evidence.
Display cadence and event-to-next-frame feedback remain separate measurements;
idle cadence is never subtracted or rounded to satisfy a frame-work budget.
The shell's mounted placeholder is not a completed transcript implementation;
Phase 3's real conversation and large-history rendering remain separate gates.

The repository's canonical Python selection authority remains
`scripts/run_test_matrix.py`. A local node-ID overlap is insufficient to remove
CI execution on another platform or under coverage/stress instrumentation.
Retain platform and intentional repetition coverage unless a before/after
equivalence report proves its preservation.
