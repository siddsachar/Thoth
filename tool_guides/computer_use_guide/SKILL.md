---
name: computer_use_guide
display_name: Computer Use Guide
tools:
  - computer_use
---

# COMPUTER USE WORKFLOW

- Prefer Browser for managed pages and Computer Use for native targets. Never switch silently. Issue one Computer Use call at a time. Use one app-scoped capture with an exact current `list_apps` name, never parallel app/window discovery.
- `target_id` and tokens live in the current Computer Use generation. A new user turn starts with discovery or app-scoped capture. Diagnose gone or its lease expired only when a previously returned opaque target produces `target_gone`.
- Do not repeat after `app_not_running`, `window_not_found`, protected refusal, or non-retryable native capture. Never infer protection or lease expiry from refusal.
- Capture once; exact-filter misses preserve the unfiltered capture. An ambiguity's returned current token works on the next action without another capture only for the present observation/lease.
- On independent stale refusal, capture the exact same target once and retry the same exact action once. If stale repeats, stop or request Take over. Do not switch action family or delivery engine. Row-Bot never replays mutations.
- Token-bound `type` rejects disabled, read-only, secure, protected, or structural targets. Tokenless `type` is literal current-caret insertion; use `replace_text` for a complete value.
- Separate action dispatched, native state changed/unchanged/unknown, and exact postcondition verified/not verified; none proves the intended outcome.
- Text insertion, `replace_text`, uncertain keys, destructive actions, and consequential actions must not be replayed. A reversible click with unchanged fresh state permits one alternative exact route grounded in current evidence. For asynchronous navigation, use one bounded `wait`, then capture. Never use fuzzy controls, blind coordinates, shell, clipboard, CDP, or app-specific APIs.
- Only pre-dispatch `background_unavailable` or `foreground_required` permits one unprepared same-action foreground retry for `type`, `key`, `scroll`, or Calculator-only `key_sequence`. Second refusal is terminal. Refusal never authorizes scripting, hidden APIs, or app automation; Stop or Take over for protected, credential, approval, or hard-blocked states.
