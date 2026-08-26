# Computer Use Beta: architecture and security decision

Status: accepted and implemented for Row-Bot 4.5.0
Implementation: `src/row_bot/computer_use/`,
`src/row_bot/tools/computer_use_tool.py`, and
`src/row_bot/ui/computer_use.py`

## Decision

Row-Bot will expose an opt-in, provider-neutral `computer_use` tool for native
desktop applications. Browser remains the preferred and separate DOM-based
engine for web tasks. Computer Use is restricted to an interactive local UI,
one exclusive task-scoped session, target-window capture, and an allowlisted
subset of the unchanged upstream Cua Driver MCP surface.

The integration does not add Row-Bot telemetry, a Cua fork, unattended host
automation, provider-native computer protocols, a generic multimodal MCP
layer, personal-browser attachment, desktop replay, or a VM backend.

## Reviewed upstream dependency

- Project: Cua Driver Rust, MIT license
- Version/tag: `0.20.0` / `cua-driver-rs-v0.20.0`
- Signed tag commit: `bb8c86049cad1bf0853c6d25c03c14875d0d047f`
- Release: <https://github.com/trycua/cua/releases/tag/cua-driver-rs-v0.20.0>
- Windows x86_64 full archive: `cua-driver-rs-0.20.0-windows-x86_64.zip`
  (`bd27528e0d81bf78c03cdd77be28a3ea31899a370eaf06938ad21edac73290bd`)
- Windows ARM64 full archive: `cua-driver-rs-0.20.0-windows-arm64.zip`
  (`a01686a90725d9c902d558c053a0dd95bd181faff0418d9acb495da63f04a6a1`)
- macOS universal full app archive: `cua-driver-rs-0.20.0-darwin-universal.tar.gz`
  (`d5e61fecebd9a620e50c2b8b608c8e7e8141f74c6faebc2ae9ef5d0d96cce7b8`)

Row-Bot downloads only the exact selected asset after a separate explicit
Install action, verifies SHA-256 before safe extraction, and keeps the runtime
private. It never runs the upstream installer or updater. Cua update checks are
disabled with `CUA_DRIVER_RS_UPDATE_CHECK=0`; Cua telemetry is deliberately
left at its disclosed upstream default.

## Telemetry acceptance

Before any Cua executable invocation, Row-Bot shows a mandatory Continue or
Cancel disclosure. Notice version 2 invalidates the older 0.7.1 acknowledgement,
so an upgrade requires explicit consent again.

The reviewed 0.20.0 tagged source sends content-free product events to
`https://eu.i.posthog.com/capture/`. They can include pseudonymous random
installation and process-session identifiers; product/platform/architecture/
transport versions; bounded client, provider, model, and agent categories;
tool and operation categories; success, bounded refusal/error class, duration
and output-size buckets; output type; aggregate session counts and bounded
window/desktop modality, capture-scope/browser/cursor/recording/config usage
flags; permission-gate state; and install/update lifecycle events. The 0.20.0
delta adds only those aggregate modality flags; it does not add a new
content-bearing telemetry category, so the unreleased notice remains version 2.

The event builders do not receive prompts, tool arguments or results, typed
text, screenshots, accessibility trees, application/window names, URLs,
filenames/paths, raw cursor/config values, or raw errors. Row-Bot does not
disable or rebrand this upstream telemetry. It keeps Cua update checks off and
does not expose Cua recording, browser, desktop-wide, updater, autostart,
clipboard, or arbitrary execution tools, so those features cannot be invoked
through this integration.

## Driver allowlist

The private client permits only:

| Cua tool | Row-Bot use | Policy class |
|---|---|---|
| `list_apps`, `list_windows` | private discovery | observation |
| `get_window_state` | target-window tree and screenshot | observation |
| `launch_app` | allowlisted display-name launch | routine or consequential |
| `bring_to_front` | explicit foreground escalation | always confirm |
| `click`, `double_click`, `right_click` | token-first selection | routine or consequential |
| `type_text`, `set_value`, `press_key`, `hotkey` | non-secret text/input; `type_text` preserves ordinary focused caret/selection insertion and `set_value` performs exact textual replacement | routine, consequential, or handoff |
| `scroll`, `drag` | bounded target-window input | routine |
| `health_report`, `check_permissions` | readiness after disclosure | internal only |
| `start_session`, `end_session` | private window-scoped lifecycle | internal only |
| `verify_state` | one bounded service-derived exact postcondition | internal only |
| `invoke_menu` | exact 1-16-label native menu path | capability-gated and policy-gated |

All recording, desktop capture, browser-page/CDP, arbitrary config, update,
installer, autostart, telemetry mutation, skill, FFmpeg, kill-process, and
maintenance surfaces are forbidden and never model-visible.

## Security invariants

- Computer Use is off by default and unavailable to schedules, channels,
  background workflows, child agents, and headless/server callers.
- The exclusive lease covers discovery, capture, Vision fallback, and input.
- Stop and Take over cancel queued work before mutation is exposed. Take over
  retains a paused lease; Resume requires a fresh observation.
- Target IDs and element tokens are opaque and generation-bound. Target drift,
  reconnects, approval waits, and takeover invalidate them.
- Compact native observations retain the existing 80-element and 12 KiB model
  limits while promoting visible selected elements and reserving a small,
  deterministic document/grid share. They expose only labels plus useful
  `selected=true` or `enabled=false` state; values, geometry, and parent trees
  remain model-hidden.
- `type` is literal keyboard insertion at the current caret or selection. It
  may validate a semantic token but never forwards that token to Cua's
  whole-value path. Horizontal-tab payloads are rejected before approval or
  mutation because native tab behavior cannot promise structured paste or grid
  layout; multiline insertion remains supported.
- `replace_text` requests one exact current editable semantic control through
  the reviewed token-targeted Cua `set_value` path. It requires
  the latest projected token, a supported generic editable role, an enabled
  control, and non-sensitive text; accepts no coordinates; and has no caret,
  label, fuzzy, clipboard, shell, or alternate delivery fallback. Stale,
  unsupported, disabled, refused, and unverifiable cases are never replayed.
  It takes contemporaneous before/after target-window captures and verifies
  only exact target equality corroborated by target-interior display change.
  Provider echoes, unrelated tree churn, focus decoration, and cursor overlays
  cannot establish the requested outcome. Verified scope is only the displayed
  target, never saved, durable, recalculated, submitted, or whole-document state.
- Credentials, OTP, CAPTCHA, biometric, UAC/TCC, terminals, password managers,
  Row-Bot itself, secure desktops, and elevation are handed off or blocked.
- Pixels are captured only for coordinate grounding, visual comparison,
  explicit Vision, live preview, or final visual evidence. Tree-only token
  refreshes request no screenshot; cheap action receipts do not imply a fresh
  observation.
- Screenshot bytes are ephemeral. Typed values are excluded from logs,
  histories, checkpoints, approval payloads, memory, and durable media.
- Consequential actions require point-of-risk confirmation even in Auto mode.
- Unbound Enter/Return remains consequential because it may submit a form or
  dialog. Exact replacement is the atomic usability path when a separate Enter
  would only commit an assumed edit; model-authored expected effects do not
  weaken the Enter policy.
- App-scoped exact-name failures may return at most eight currently running
  canonical app names with running/active metadata for a deliberate retry.
  Row-Bot does not expose unrelated titles, fuzzy-match, auto-select, infer an
  alias, or silently launch a candidate.
- UI text and accessibility content are untrusted tool output and cannot grant
  new scope, recipients, secrets, or authority.
- Prompt-injection pattern matches in native labels and values are advisory
  evidence, not mutation authorization. Row-Bot scans app-authored fields
  independently without treating accessibility roles as instructions, then
  applies the same exact-target, protected-surface, credential, consequential-
  action, and thread approval policy whether or not an advisory is present.
  Model-visible diagnostics contain only deduplicated bounded categories such
  as `explicit_role_marker`, `instruction_override`, `exfiltration_request`,
  or `hidden_control_anomaly`, never the matching value, identity, title,
  token, coordinate, or position.
- Approval interruption emits a privacy-safe pending diagnostic rather than a
  successful completed-action receipt. Resume can dispatch at most once and
  emits one final completed, failed, denied, or cancelled receipt without typed
  text, labels, titles, tokens, coordinates, screenshots, or approval secrets.
- `action_dispatched=true` proves neither focus, caret/selection/navigation nor
  the requested outcome. Dependent mutation chains must use an exact semantic
  action, one necessary fresh confirmation, or stop; after a failed final
  verification, only one materially different safe recovery is allowed.
- Token-based semantic replacement stays on the native fast path unless
  `capture_after=true` and one explicit visual question requests exactly one
  advisory Vision check. Vision is not automatically repeated or parsed as
  authorization or a Boolean postcondition.
- Terminal `hard_blocked` results are reserved for concrete protected targets
  or capabilities and Block approval mode; they remain terminal and must not be
  bypassed by aliases or alternate Computer actions.

Generic Computer Use is not a substitute for a purpose-built structured or
application API for large bulk transformations. Row-Bot exposes no
model-visible clipboard action and does not use hidden shell clipboard commands
to simulate one.

## Upstream contract binding

Row-Bot uses the tagged 0.20.0 lifecycle session while every capture remains
bound to the exact PID/window target. It never calls desktop capture or
escalates to desktop scope. The runtime
installer uses only the exact full archives above, performs traversal-safe
staged extraction, and retains a prior known-good managed runtime until the new
candidate passes diagnostics.
