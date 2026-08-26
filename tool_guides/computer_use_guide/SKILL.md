---
name: computer_use_guide
display_name: Computer Use Guide
tools:
  - computer_use
---

# COMPUTER USE WORKFLOW

- Route structured tools first, Browser for ordinary pages in Row-Bot's managed browser, and Computer Use for one exact native app or window. Never silently switch engines after an error.
- Capture once, then use the current semantic token directly. Recapture only after a stale refusal or when the next decision needs new state.
- Token-bound `type` lets Cua attempt the current control, including combo controls, cells, data items, and unknown interactive roles. Row-Bot locally rejects only explicit disabled, read-only, secure, protected, and clearly structural targets.
- Tokenless `type` is one Cua insertion at the current focused caret or selection. Use `replace_text` for one exact complete value.
- A stale refusal may include fresh controls. Use one returned current token on the next turn; Row-Bot did not replay the refused mutation.
- Treat delivered/unverified as useful delivery, not failure. Do not repeat that exact uncertain insertion, but keep Enter, navigation, later fields, capture, and truthful final answers moving.
- `verified_scope=exact_value` proves only that one control contains the requested value. It does not prove evaluation, commit, navigation, or overall task completion.
- Prefer semantic tokens and the exact capture filter to coordinate guessing in large trees. Request Vision only when semantic state cannot answer the next decision or the user asked for visual inspection.
- Routine actions use one Cua call with no hidden capture. Only an explicit pre-dispatch background refusal permits one foreground type call; never add focus/click/key preparation or replay.
- Stop or use Take over for protected, credential, approval, or hard-blocked states instead of retrying around policy.
