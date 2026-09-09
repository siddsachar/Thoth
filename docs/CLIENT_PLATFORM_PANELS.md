# Panel contributions

`frontend/src/features/panels/model.ts` owns presentation-only descriptors,
instances and pure layout transitions. `panelRegistry` contains bundled fake
information, activity and document examples. It maps known kind IDs to resource
requirements, capability IDs and compact presentation. An API response cannot
register a component, script, HTML, path or route. Unknown kinds remain safe
unsupported placeholders. Real Developer/Designer contributions wait for their
implementation phases.

A descriptor is declared once in `api/types.ts` and reexported by the panel
model. It has `panel_kind`, a safe display title, optional opaque
`resource_ref`, resource kind/revision, subresource key and capability requirements.
An instance adds an ID, side/bottom placement and visibility. Dedupe uses
kind/resource/subresource; explicit duplicate views share resource truth.
`panelStatus` distinguishes missing, stale, unauthorized, unsupported and
unavailable capability using current caller-supplied read-only resource facts.
The registry never authorizes a backend operation or becomes a resource store.

The application retains one `PanelLayout` and calls `openPanel`, `focusPanel`,
`closePanel`, `movePanel`, `resizeRegion`, `toggleRegion` and `resetLayout`.
The persistent conversation remains outside the panel tree. A layout transition
does not select a conversation, discard a draft, rebind resources or stop work.
The maximum is 20 open local instances. Closing focuses the next instance or
returns to the conversation when none remain.

`regionBounds` and `reconcileLayout` implement bounded geometry. Navigation is
200–320 pixels (240 default), side is at least 320 (420 default) and at most
720/55% of available width, and bottom is at least 160 (240 default), capped at
45% height while preserving conversation space. The conversation keeps its
400-pixel desktop minimum, with navigation collapsing before impossible splits.
Tablet/phone transform the same instances into tabs/sheets. UI primitives own
the accessible pointer/keyboard separator, focus and modal behavior.

`persistLayout` serializes schema version 1; `layoutStorageKey(profile,width)`
separates client profiles and desktop/tablet/phone classes. `restoreLayout`
validates bounded input, migrates version-0 scalar region sizes, retains valid
known or unsupported descriptors, clamps current bounds and safely resets
unrecognized versions. Reset deletes only presentation state. Storage denial
must be caught by the application and never block usable defaults.

`suggestPanel` is advisory: it adds a deduplicated bounded suggestion without
opening/focusing/moving a panel. An explicit Open action can call `openPanel`
and dismiss the suggestion. The accepted v1 event union has no `panel.suggested`
wire variant; the controller's `suggestPanel(ClientPanelSuggestion)` feeds a
bounded local advisory list and fixture exercise. Suggestions for other known
conversations remain associated with those conversations; they never change
selection. A future wire addition must be negotiated/generated alongside
Python DTOs and compatibility fixtures, never accepted as an unknown critical
event. Suggestions cannot grant authority or change selected conversation.

`PanelSubscriptions.acquire(key, subscribe, notify, visible)` shares one source
for visible references to a resource. Unchanged revisions do not notify;
hidden references do not render. Showing a reference reconciles its current
revision, and the final hidden/released reference stops its source. Every
handle exposes idempotent `release` and `setVisible`. The source must publish
its current revision on subscribe; it reuses the existing protocol/Inspector
owner instead of creating a competing poller. Synchronous first notifications
may close a panel; returned cleanup still runs. Tests count zero hidden work
over 60 fake-clock seconds and no owned source/timer growth across 100 cycles.
