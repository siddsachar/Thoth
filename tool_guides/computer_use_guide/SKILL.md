---
name: computer_use_guide
display_name: Computer Use Guide
tools:
  - computer_use
---

# COMPUTER USE WORKFLOW

- Prefer Browser for managed pages and Computer Use for native targets; never switch silently. Issue one Computer Use call at a time. Use one app-scoped capture with an exact current `list_apps` name, never parallel app/window discovery.
- `target_id` and tokens live in the current Computer Use generation. A new user turn starts with discovery or app-scoped capture. Say gone or its lease expired only after a previously returned opaque target produces `target_gone`.
- Do not repeat after `app_not_running`, `window_not_found`, protected refusal, or non-retryable native capture. Exact-filter ambiguity permits a returned current token without another capture only for the present observation/lease.
- On stale refusal, capture the exact same target once and retry the same exact action once. If stale repeats, Stop or request Take over. Do not switch action family or delivery engine.
- Token-bound `type` rejects disabled, read-only, secure, protected, or structural targets. Tokenless type is literal text insertion; use `replace_text` for a complete value.
- Separate action dispatched, native state change, and exact postcondition. Read `driver_verdict`, `degraded`, `escalation_recommendation`, `verdict`, and `next_step` literally: `done`, `verify_fresh_state`, `escalate`, or `take_over` applies only to this action receipt.
- Start with `delivery_mode=auto`. Use explicit `delivery_mode=foreground` only for click, double-click, right-click, type, key, or scroll after fresh exact-target evidence and a prior structured recommendation/refusal. Pre-dispatch `background_unavailable` or `foreground_required` permits one same-action foreground rung; a second refusal requires Take over. Never focus first or assume focus persists.
- Text insertion, accepted or uncertain mutations, timeouts, disconnects, and ended sessions must not be replayed. Never replay them. After one fresh unchanged `suspected_noop`, a reversible click permits one alternative exact route: recommended `foreground`, or `px` from a new screenshot-grounded exact-target capture. `page` is observable but unsupported; never use SOM, blind coordinates, Browser/CDP/Page, shell, clipboard, or app-specific fallbacks.
