---
name: computer_use_guide
display_name: Computer Use Guide
tools:
  - computer_use
---

# COMPUTER USE WORKFLOW

- Prefer structured tools, Browser for managed pages, and Computer Use for native targets. Never switch silently.
- Issue one Computer Use call at a time. Prefer one app-scoped capture with an exact current `list_apps` name, not parallel app/window discovery.
- `target_id` and tokens live in the current Computer Use generation. A new user turn starts with discovery or app-scoped capture. Diagnose gone or its lease expired only when a previously returned opaque target produces `target_gone`.
- Do not repeat acquisition after `app_not_running`, `window_not_found`, protected-target refusal, or non-retryable native capture failure. Never infer elevation, protection, or lease expiry from an unknown native refusal.
- Capture once. Exact-filter misses or ambiguity preserve the current unfiltered capture; use its tokens or revise the filter. Stale refusals may return controls; Row-Bot never replays them.
- Token-bound `type` uses the current control; reject disabled, read-only, secure, protected, or structural targets. Tokenless `type` is one literal current-caret insertion. Use `replace_text` for a complete value.
- Separate action dispatched, native state changed/unchanged/unknown, and exact postcondition verified/not verified. Neither proves the intended outcome; claim only observed evidence.
- Text insertion, `replace_text`, uncertain keys, destructive actions, and consequential actions must not be replayed. After a reversible click with unchanged fresh state, allow one alternative exact route grounded in current evidence. For asynchronous navigation, use one bounded `wait`, then capture. Never use fuzzy controls, blind coordinates, shell, clipboard, CDP, or app-specific APIs.
- `verified_scope=exact_value` proves that value. Prefer semantic tokens and zero-Vision routine flows; ask one concrete pixel-only question when needed.
- Only pre-dispatch `background_unavailable` or `foreground_required` permits one same-action foreground retry for `type`, `key`, or Calculator-only `key_sequence`, without preparation. Second refusal is terminal. Refusal never authorizes scripting, hidden APIs, or app automation; Stop or Take over for protected, credential, approval, or hard-blocked states.
