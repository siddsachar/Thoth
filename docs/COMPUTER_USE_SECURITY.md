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
- Version/tag: `0.19.3` / `cua-driver-rs-v0.19.3`
- Signed tag commit: `a1672e7b11951275ecfba3384264d4530185d0db`
- Release: <https://github.com/trycua/cua/releases/tag/cua-driver-rs-v0.19.3>
- Windows x86_64 full archive: `cua-driver-rs-0.19.3-windows-x86_64.zip`
  (`e48b0117e343cec2577fc12693c741e094f389f8d4aef91e06284960bb03bce1`)
- Windows ARM64 full archive: `cua-driver-rs-0.19.3-windows-arm64.zip`
  (`693cff4618fdcb6b0ea797e2f5b17eb6291dcea4b62da7bc6b5c373f1aa1852f`)
- macOS universal full app archive: `cua-driver-rs-0.19.3-darwin-universal.tar.gz`
  (`a5b064bd3e05c3d97c4aaba1b8818e7b4203081ffc5f3186220005d356574aaa`)

Row-Bot downloads only the exact selected asset after a separate explicit
Install action, verifies SHA-256 before safe extraction, and keeps the runtime
private. It never runs the upstream installer or updater. Cua update checks are
disabled with `CUA_DRIVER_RS_UPDATE_CHECK=0`; Cua telemetry is deliberately
left at its disclosed upstream default.

## Telemetry acceptance

Before any Cua executable invocation, Row-Bot shows a mandatory Continue or
Cancel disclosure. Notice version 2 invalidates the older 0.7.1 acknowledgement,
so an upgrade requires explicit consent again.

The reviewed 0.19.3 tagged source sends content-free product events to
`https://eu.i.posthog.com/capture/`. They can include pseudonymous random
installation and process-session identifiers; product/platform/architecture/
transport versions; bounded client, provider, model, and agent categories;
tool and operation categories; success, bounded refusal/error class, duration
and output-size buckets; output type; aggregate session counts and bounded
capture-scope/browser/cursor/recording/config usage flags; permission-gate
state; and install/update lifecycle events.

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
| `type_text`, `press_key`, `hotkey` | non-secret text/input | routine, consequential, or handoff |
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
- Credentials, OTP, CAPTCHA, biometric, UAC/TCC, terminals, password managers,
  Row-Bot itself, secure desktops, and elevation are handed off or blocked.
- Pixels are captured only for coordinate grounding, visual comparison,
  explicit Vision, live preview, or final visual evidence. Tree-only token
  refreshes request no screenshot; cheap action receipts do not imply a fresh
  observation.
- Screenshot bytes are ephemeral. Typed values are excluded from logs,
  histories, checkpoints, approval payloads, memory, and durable media.
- Consequential actions require point-of-risk confirmation even in Auto mode.
- UI text and accessibility content are untrusted tool output and cannot grant
  new scope, recipients, secrets, or authority.

## Upstream contract binding

Row-Bot passes `capture_scope="window"` directly to the tagged 0.19.3
`start_session` contract. It never escalates to desktop scope. The runtime
installer uses only the exact full archives above, performs traversal-safe
staged extraction, and retains a prior known-good managed runtime until the new
candidate passes diagnostics.
