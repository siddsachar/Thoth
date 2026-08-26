---
name: computer_use_guide
display_name: Computer Use Guide
tools:
  - computer_use
---

# COMPUTER USE WORKFLOW

- Route structured tools first, Browser for ordinary pages in Row-Bot's managed browser, and Computer Use for one exact native app or window. Never silently switch engines after an error.
- Acquire one app-scoped observation. Use only the newest target and token generation, and recapture only when the next decision requires fresh state.
- A token on `type` only validates an already-selected, enabled caret-bearing control. It never selects, focuses, or retargets the destination.
- Use `replace_text` directly to set one exact complete value in a supported writable semantic field, document item, or grid item.
- To move ordinary insertion to another control, explicitly click its exact semantic token, obtain fresh native selected/caret evidence, then call `type`. Row-Bot never performs those steps automatically.
- Fail closed on stale, unselected, unknown, ambiguous, disabled, unsupported, or unverified state. Do not substitute typing, keys, coordinates, labels, clipboard, shell, or application APIs.
- Keep dispatch, driver verdict, displayed-target evidence, verified scope, suspected no-op, and completion distinct. Vision is advisory; `stop` never turns uncertainty into success.
- Prefer semantic tokens to coordinates. Request Vision only for necessary coordinate grounding or one explicit final visual check.
- Stop or use Take over for protected, credential, approval, hard-blocked, or unresolved states instead of retrying around policy.
