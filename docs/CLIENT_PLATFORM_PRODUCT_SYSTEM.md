# Shared client product system

The opt-in client uses a single semantic product system. Domain features consume
`frontend/src/ui`; they do not introduce separate palettes, overlay stacks or
resizer implementations. The read-only conversation placeholder stays mounted
under route, layout and appearance changes. Chat rendering and real Developer or
Designer panels are later contributions.

## Theme contract

`theme-model.ts` owns version 1 preferences and all palette values. Appearance
is `system`, `light` or `dark`; accents are blue, teal, violet and amber. Density
is comfortable or compact. The local `row-bot.appearance.v1` key contains only
appearance preferences, never backend state or secrets. Version 0 values are
normalized, unknown versions or malformed values reset safely, and unavailable
storage leaves usable session preferences.

The same self-contained `bootstrapTheme` runs inline before the first stylesheet
and in the React provider. It writes semantic CSS variables and root attributes;
the host computes an exact CSP hash for the verified inline script. System colour
and transparency listeners are removed with the provider. Storage events update
other open clients. Features use `useTheme().update`, never write palette values
or this storage key independently.

Use `--canvas`, `--surface`, `--surface-raised`, hover/pressed/disabled surfaces;
primary/secondary/muted/inverse text; subtle/control borders; accent/focus/link/
selection variables; named status, code/syntax, diff, chart and artifact tokens.
Status and diff meaning always includes text or a marker. Do not infer meaning
from an accent alone. The theme contract tests check text at 4.5:1 and essential
non-text controls at 3:1; browser tests verify composited results too.

Base body text is 16/24, code 13/20, and control text 14/20. Comfortable and touch
controls are at least 44px; compact desktop fine-pointer controls may be 32px.
The space scale is 4/8/12/16/24/32/48px; control/panel/dialog radii are 6/10/14px.
Focus uses a visible 2px ring and offset. Background transitions do not animate
theme colours through intermediate low-contrast values.

## Public primitive inventory

| Surface | Contribution contract |
| --- | --- |
| `Button`, `Input`, `Select`, `Field` | Native semantics and labels; primary/secondary/ghost/danger actions; disabled states and named icon buttons. Select stays a native browser control. |
| `Tabs`, `Menu`, `Popup`, `Hint` | Radix owns keyboard, focus and dismissal behavior. Floating surfaces layer above their active task; give each trigger an accessible name. |
| `OverlayProvider`, `useOverlay` | One Radix modal scope with title/description; dialogs, short sheets, navigation drawers and alert-dialog semantics share it. |
| Notifications | `notify` coalesces duplicate text and retains at most three notices. Notices wait while a modal is open, so they cannot cover its footer or consume Escape; Radix pauses dismissal on focus/hover after display. Errors also need a persistent inline recovery action. |
| `Skeleton`, `EmptyState`, `ErrorState`, `Progress` | Name the operation; delay skeleton visuals 150ms with cancellation; never invent percentage progress. Empty states explain a useful next step. |
| `Surface` | Opaque by default. The optional elevated effect has a 96% backing and bounded blur only with supporting CSS and appropriate preferences. |
| Workspace commands | A labeled button at every width and Ctrl/Cmd+K open searchable native controls with keyboard hints and initial search focus. Modified/reserved chords and IME composition pass through. |
| Pane groups | `react-resizable-panels` supplies pointer/touch capture and separator semantics; the typed layout model owns bounds/persistence. |

Do not mount a domain modal inside another modal. `open` replaces a task;
confirmations suspend the still-mounted task content, hide/inert it, and restore
its values and focus on Cancel/Escape/outside click. Only the active task owns a
focus trap and body scroll lock. Confirmation defaults to Cancel; unrelated text
Enter never confirms. The footer remains visible while the body scrolls.
Primary-pointer buttons establish the opener focus even in WebKit, while
respecting compound primitives that prevent the pointer event. Header controls
wrap when zoom or available width requires it; clipping is not a responsive mode.
Menu actions receive their stable trigger for `returnFocusTo` when opening a
task, since the selected menu item disappears. Closing, moving or collapsing a
dock restores a surviving tab or the Open panel trigger. Commands read the
current layout when invoked, including after an open command surface changes
breakpoint.

Desktop dialogs are bounded to 560px and viewport minus 48px. Compact forms use
a full task surface. Short sheets use at most 85dvh; navigation uses a 280px
tablet drawer and a full-screen phone list with Back. Overlay keys allow an owner
to dismiss its responsive surface without dismissing an unrelated task.
Fixed surfaces also respect their containing viewport at zoom; the header text
can wrap and the body scrolls within the height left by the header and footer.
Verify loaded controls and actual viewport hit targets, since a full-page
screenshot can include controls that a scroll-locked user cannot reach.

## Layout and settings contributions

The foundation sidebar previews ten conversations in server order. Show more
reveals the loaded page; Load more keeps the existing cursor continuation. Show
less and section collapse retain the current conversation, including a confirmed
selection outside the loaded page. Expansion is session presentation state and
does not introduce another selection or persistence owner. Activating a row also
returns from gallery/settings routes to the conversation. Titles truncate with
ellipsis while keeping their full accessible name and hover/focus hint. Compact
selection also reveals the conversation through the existing layout focus owner;
panel registrations remain available to reopen and desktop docks stay in place.
Ordinary root-view selections preserve the current browser history and query.
Compact fine-pointer desktop rows may be 36px; comfortable and touch rows stay 44px.
Complete search, pins, grouping and conversation mutations belong to Phase 3.

Tooltip portals use a noninteractive, transformed viewport layer so floating
placement can measure the containing block's scale at page zoom. Content uses
the primitive's available-width measurement and keeps pointer access; the layer
must not intercept underlying controls. Browser regressions verify actual full
title bounds and focus, including narrow WebKit at CSS zoom 2.

The conversation area has a 400px desktop minimum. Navigation defaults to 240px
with 200–320px bounds and a 48px collapsed rail. Side and bottom bounds are in
the panel model, not copied into domain CSS. Keyboard arrows move 16px, Shift
moves 48px, Home/End reach bounds and Enter collapses/restores. Menus provide
resize/move/collapse actions without dragging. Compact resource views become
registered tabs or sheets while preserving instance identity.

Mount a library `Separator` only while its resize surface is available. Keep
the surrounding groups and panels mounted. Hiding a registered separator with
`display: none` prevents the library's geometry-based panel association from
supplying its ARIA control/range values when it later appears. Verify numeric
min/current/max and a live controlled panel after open, resize, restore and
breakpoint changes. Keyboard collapse returns focus to the Open panel trigger
when the side or bottom handle disappears.
Group resize completion also reconciles the library's 48px navigation or 0px
side/bottom drag-collapse sizes into the versioned model, preserving the last
expanded restore size. Test pointer collapse across refresh and restore; a
visually collapsed pane alone does not prove persisted state is correct.

`features/settings/model.ts` is the one navigation/search/label/deep-link map.
It preserves 16 existing leaves in five categories and their legacy aliases.
Unknown setting links return the index. Domain settings remain typed capability
forms; this metadata is not a form schema or another backend settings store.

## Accessibility, effects and visual regression

The `/app-v2/primitives` route exercises every public primitive and token family.
Use it with both appearances, all accents and the five supported viewport sizes.
Reduced motion removes animations; reduced transparency, unsupported filters,
phone layouts, forced colours and `data-low-performance="true"` force opaque
surfaces. The low-performance hook is a presentation input, never native or data
authority. Forced colours preserve system focus/borders. Do not use a screenshot
alone as proof of hit-testing, focus order, minimum size or contrast.

For a visual change, run unit/theme contracts, build the explicit fixture bundle,
and run the isolated browser suite in the testing guide. Inspect full screenshots,
actual control hit tests, console/network evidence, axe results and raw timing
samples at 1440×900, 1280×720, 820×1180, 390×844 and 360×800. Keep failures and
correct the component; do not broaden error allowlists or replace golden evidence
to make a failing check pass. Record source/asset hashes with each evidence cut.
Regenerate the normal production bundle before payload/reproducibility checks.

Browser emulation, CSS zoom, axe and fake native drivers do not certify physical
keyboards/safe areas, screen readers, actual browser chrome zoom or OS dialogs.
Record those manual limitations explicitly in the phase gate. The full transcript,
streaming token DOM, preview isolation and capability-specific UI arrive in their
scheduled phases; foundation measurements must retain that scope distinction.
