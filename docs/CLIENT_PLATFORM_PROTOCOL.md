# Client protocol owner

The opt-in client consumes `contracts/client-platform/v1/typescript/client.ts`.
The Python DTOs and `scripts/generate_client_platform_contracts.py` remain the
canonical schema and generator. After an approved contract change, run
`python scripts/generate_client_platform_contracts.py`, then the same command
with `--check`, the protocol Python contracts and frontend checks. Never edit
the generated client by hand. Optional AbortSignals propagate through HTTP,
upload, download and observation calls. Only subscription deletion also exposes
an optional final keepalive flag for terminal cleanup.

`frontend/src/api/index.ts` exposes `ClientController`, `HttpTransport`, public
wire types and `createClientController`. The application creates one controller.
React consumes its stable `subscribe` and `getSnapshot` functions through
`useSyncExternalStore`; components never create transports or raw connections.
The owner holds the session proof only in memory. Public state excludes the
CSRF token. Browser/native media adapters receive the controller's `upload`
and `download` methods, so they use the same authenticated transport.

`start()` performs version/capability negotiation and reads the first bounded
conversation page. `selectConversation(id)` aborts the previous queries and
fences late completions by request identity. `loadMoreConversations()` and
`loadMoreTranscript()` follow opaque continuation cursors; transcript materialization
retains at most 200 rows. Selection belongs to each client, while the server
owns conversation, resource, checkpoint and generation truth. The shell is
read-only; complete transcript rendering/search/scroll anchoring belongs to
the conversation vertical slice.

Each selected view obtains one atomic subscription snapshot/cursor cut. The
owner installs that cut before acknowledgement or suffix application. Events
deduplicate by identity and projection revision, check source-stream sequence
ranges and epoch, and never move an applied cursor backwards. Checkpoint,
resource, settlement, epoch and gap changes resubscribe to authoritative state.
Three resets without progress fail closed. This conservative reconciliation
keeps exact live/native adoption in the existing server owner.

Authenticated fetch SSE is preferred. Two streaming failures switch to polling
using the same subscription and applied cursor. SSE and polling never run in
parallel. Read retry delays are bounded by 1, 2, 4, 8, 15 and 30 seconds with
bounded jitter; exhausted retries leave an explicit disconnected state. Polling
backs off from 2 to 30 seconds while idle. A valid poll page may contain 4096
events; processing yields between slices of at most 256. Authentication and
compatibility failures halt observation and clear protected views. Raw response
titles, exception messages, paths and tracebacks are never displayed.

`setVisible(false)` releases content observation; return obtains a fresh cut.
`setOnline(false)` suspends all network reads/retries and fences pending results
while retaining the last confirmed view and selected conversation. It clears
the session proof; only the opaque client-session identity is retained for a
fresh access-checked handshake. Returning online reauthenticates, releases
retired observation subscriptions, and resynchronizes the selected conversation.
The cleanup list is bounded to 256 subscription IDs and never contains commands.
Genuine authentication loss forgets the resume identity and clears protected
metadata. The page lifecycle uses suspension for cached back/forward visits;
ordinary page disposal remains terminal. Neither recovery path replays commands.
`dispose()` aborts queries and observation, makes one best-effort authenticated,
bodyless subscription DELETE with `keepalive: true`, and clears listeners/session
proof. That final release can outlive document teardown; it does not share the
aborted client lifetime and is never retried after disposal. Ordinary observation
replacement remains abortable. Neither cleanup path stops backend work. The
controller exposes content-free counters for bounded event/reconciliation tests.
If terminal release cannot arrive, disconnect still releases the active stream
slot; subscription metadata remains until its client session expires and the next
authenticated lookup or handshake prunes it. Current server bounds are 12 hours
per session, 256 sessions, 1024 subscriptions total and 32 per session. Offline
disposal makes no request. Cached-page suspension reuses its opaque session
identity and drains retired subscriptions after fresh authentication instead.
Upload, download and receipt calls combine the caller's cancellation signal with
the controller lifetime. Authentication loss aborts that lifetime. Responses are
checked against the authentication generation after completion, so a transport
that finishes after cancellation or reconnection cannot expose an old result.

Commands require an explicit command, client-session and idempotency identity.
`command(target, command, key)` shares simultaneous identical intent and rejects
same-key different input. The bounded in-memory claim cache retains a SHA-256
verifier and outcome promise, not serialized prompt bodies. The submitted DTO is
copied before asynchronous verification so caller mutation cannot change intent.
Verification captures the current authentication lifetime; cancellation before
dispatch never submits the command. A response from a command already dispatched
under an old lifetime remains uncertain and cannot revoke a newer connection.
Reconnection never submits commands. A failed promise
remains failed until the caller deliberately uses `retryCommand` with the exact
same target/body/key; callers should consult `receipt(command_id)` and display
uncertain outcomes before offering retry. The server's immutable idempotency
binding and current policy remain authoritative. There is no offline mutation
queue. The read-only shell exposes none of these mutation actions.

`suggestPanel(ClientPanelSuggestion)` is a local advisory presentation hook.
Its bounded, deduplicated `state.suggestions` list is separate from the accepted
wire event union. A suggestion carries a known conversation/revision and typed
descriptor; stale revisions are discarded. It never changes selection or
projection, opens a panel, requests data or grants authority. The shell supplies
explicit Open and Dismiss actions; `dismissSuggestion` removes the chip. This
hook and fixture tests let the shell establish advisory behavior without
accepting an unsupported v1 event. Authentication failure clears suggestions
alongside other protected metadata.

Fixture builds opt in with `VITE_ENABLE_FIXTURES=1` and explicitly select a
fixture scenario. Production excludes the recording chunk. The fixture transport
implements the same interface and consumes F-P01 through F-P10 directly from
the accepted recordings. Its clock advances explicitly and monotonically;
timer resolution never determines fixture order. F-P10 remains an outcome
contract, not evidence of later storage/channel implementation. Unit tests
exercise decoding, response-loss identity, replay/gaps, reset, independent
selection, complete 1005-row cursor navigation, 60-second idle and 100
observation cycles. Browser evidence is maintained separately from these tests.

For foundation calibration, the fixture transport exposes
`setTranscriptSize(1000 | 10000)`: it creates accepted-shape text rows per
100-row page and counts pages/rows delivered without retaining the entire
synthetic history. `emitTextDelta(text)` supplies a canonical monotonic delta
and updates the fixture's authoritative snapshot. `expireNextReplay()` emits
one canonical reset on the next observation. These are fixture-only controls;
they establish cache/continuation and shell workload measurements, not the later
full-transcript rendering, media, search, copy or scroll-anchor gate.
