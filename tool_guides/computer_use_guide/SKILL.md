---
name: computer_use_guide
display_name: Computer Use Guide
tools:
  - computer_use
---

# COMPUTER USE WORKFLOW

- Prefer structured tools, then Browser for managed pages, then Computer Use for exact native targets. Never switch engines silently.
- `target_id` and tokens work only in the current Computer Use generation/lease. A new user turn starts with current discovery or app-scoped capture; `target_gone` means gone or its lease expired.
- Capture once. Exact-filter misses and ambiguity leave the fresh unfiltered capture current; use returned tokens or revise the filter without rediscovery. Stale refusals may likewise return controls, and Row-Bot never replays them.
- Token-bound `type` uses the current control; disabled, read-only, secure, protected, and structural targets are rejected. Tokenless `type` is one literal current-caret insertion, not structured layout. Use `replace_text` for one complete value.
- Read three evidence levels separately: action dispatched, native state changed/unchanged/unknown, and exact postcondition verified/not verified. Dispatch or change never proves the intended outcome. Final answers claim only tested capabilities and observed evidence.
- Text insertion, `replace_text`, uncertain keys, destructive actions, and consequential actions must not be replayed. After a reversible click and explicitly unchanged fresh state, one alternative exact route grounded in current evidence is allowed. If navigation may be asynchronous, use one bounded `wait` and capture first. Never cycle routes or use fuzzy controls, blind coordinates, shell, clipboard, CDP, or app-specific APIs.
- `verified_scope=exact_value` proves only that control value, not submission, navigation, playback, or overall completion. Prefer semantic tokens and zero-Vision routine flows; ask one concrete pixel-only question only when needed.
- Only pre-dispatch `background_unavailable` or `foreground_required` permits one same-action foreground retry for `type`, `key`, or Calculator-only `key_sequence`. Add no focus, click, capture, coordinates, shell, clipboard, or preparation. A second refusal is terminal and may require Take over.
- Refusal never authorizes scripting, hidden APIs, or app automation. Stop or Take over for protected, credential, approval, or hard-blocked states.
