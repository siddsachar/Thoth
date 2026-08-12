# Row-Bot - Release Notes

---

## Unreleased - Context Window Meter and Rolling Compaction

- The desktop composer now shows an event-driven estimate of the complete next
  model input, including system prompts, images, and bound tool schemas. Mobile
  keeps the narrower composer and shows only durable compaction status rows.
- Agent Mode, tool-loop calls, resumes, and Chat Only share token accounting and
  bounded rolling compaction. Older history is summarized as untrusted reference
  data while recent complete turns and tool-call/result groups remain intact.
- Context settings migrate to policy version 2. The historical local 32K value
  becomes Auto (still requesting 32K, capped by native/observed Ollama capacity),
  and the historical provider 128K value becomes Auto. Because an old explicit
  128K selection cannot be distinguished from the former default, restore it via
  **Settings â†’ Models â†’ Advanced context â†’ Provider context override** if needed.
- Successful compaction produces one durable presentation-only timeline notice
  and, for channel-originated turns, one separately claimed channel notice.
  Context events never enter model prompts, summaries, or token accounting.

## v4.6.0 - Agent Orchestration, Remote Access & Docker Operations

This release builds on v4.5.0 with a durable-agent, document-processing,
multi-device, and server-operations pass. It turns delegated agents into one
recoverable parent-led orchestration, replaces synchronous document loading
with a bounded resumable queue and sharded index, gives one owner authenticated
access from desktop and remote browsers, ships an official hardened Docker/VPS
path, and aligns the chat, mobile, website, documentation, and release
validation surfaces around those capabilities.

### Automatic Agent Orchestration

- **One authoritative parent turn** - keeps the original agent responsible for
  planning, delegated work, follow-up waves, and the final response instead of
  turning child completions into disconnected summaries.
- **Required and background work** - distinguishes child results the parent
  must join from optional detached tasks that remain durable without blocking
  the foreground answer.
- **Dependencies and work waves** - supports ordered child dependencies,
  several rounds of delegation in one parent turn, duplicate-objective checks,
  and the existing per-parent and application-wide capacity limits.
- **Live child joins** - routes child completions, transient retries, approval
  requests, user steering, and stop events through an ordered durable inbox so
  the parent can react without polling or starting a replacement conversation.
- **Checkpoint-safe suspension** - records parent checkpoint identity, pending
  events, approvals, output metadata, and execution budget before a long-running
  parent releases its foreground stream.
- **Exactly-once completion** - uses durable leases and finalization claims to
  prevent duplicate parent runners, acknowledgements, synthesis, channel sends,
  or final chat answers when callbacks race.
- **Recovery and explicit resume** - repairs interrupted work after restart,
  restores already-recorded child terminal events, and revalidates the selected
  agents, model, workspaces, and Designer project before resuming.
- **Controlled retries and stops** - retries one classified transient child
  failure without breaking dependency barriers and supports individual or
  group stop while allowing the parent to close naturally.
- **Bounded context and execution** - orders and caps result packets, preserves
  worktree references, reuses the parent budget across event turns, and retains
  repeated-action termination and exactly-once budget finalization.

### Chat, Goals, Channels & Agent Activity

- **Compact inline Agent cards** - replaces bulky child-run output with compact
  live cards that show queued, running, approval, retry, stopped, failed, and
  completed states inside the conversation.
- **Parent Agent groups** - presents related child work as one durable group,
  streams lifecycle rows as children change, and keeps later delegation waves
  attached to the same parent generation.
- **Shared activity truth** - drives desktop, sidebar, mobile, Agent drawer,
  Buddy, and voice indicators from the same durable orchestration query rather
  than independent timers or optimistic UI state.
- **Transcript-safe restoration** - persists Agent metadata and ordering through
  checkpoints, reloads visible cards without duplicate completion rows, and
  places late child updates before future queued user turns.
- **Approval continuity** - surfaces child approvals in the parent transcript,
  deduplicates repeated approval notices, and resumes the original checkpoint
  after a decision.
- **Goal and workflow continuation** - lets Goal Mode continue after a completed
  orchestration and updates delegated Workflow steps to use the same durable
  parent/child lifecycle.
- **Channel-safe delivery** - carries orchestration acknowledgement, activity,
  approval, completion, and retry semantics through supported channel runtimes
  without sending preview drafts or duplicate final messages.
- **Cross-studio hardening** - protects concurrent Designer projects,
  Developer workspaces, filesystem confirmation, queued messages, tool traces,
  and stop handling while child agents are active.

### Durable Bounded Document Ingestion

- **Persistent batch queue** - replaces synchronous all-in-memory ingestion with
  SQLite-backed batches and jobs that survive restart and expose clear queued,
  indexing, searchable, extracting, completed, failed, cancelled, and duplicate
  states.
- **Bounded safe uploads** - streams files to staging while hashing, caps each
  file at 256 MiB, preserves a 2 GiB disk reserve, contains filenames, isolates
  interrupted cleanup, and allows same-name files when their contents differ.
- **Content deduplication** - skips duplicate bytes across existing documents or
  the same selected batch while retaining stable document IDs and source
  records.
- **Fair single-flight work** - uses a durable FIFO queue, leases, heartbeats,
  and one supervisor, and makes every document in a batch searchable before
  starting the slower extraction phase.
- **Pause, cancel, retry, and repair** - persists queue controls, resumes safe
  extraction checkpoints, restarts incomplete indexing, fails missing sources
  explicitly, and retires orphaned staging or work data.
- **Sharded atomic vectors** - builds embeddings in batches of at most 32 chunks,
  rolls index segments at 2,000 chunks, and publishes a new document manifest
  atomically so failed replacements leave the old searchable corpus intact.
- **Deterministic mixed search** - merges compatible new shards and legacy
  vectors into stable top-k results, excludes stale embedding configurations,
  and supports ID-specific removal, cache release, and bounded rebuilds.
- **Resumable knowledge extraction** - persists rolling map windows and
  hierarchical reductions in groups of at most eight, checks cancellation
  between provider calls, isolates failures by document, and commits document,
  graph, and Wiki Vault results idempotently.
- **Visible queue operations** - adds batch and job progress, pause/resume,
  cancel, retry, clear-finished, indexed-document management, status-bar
  activity, and queue/index health diagnostics to the Documents surface.

### Single-Owner Multi-Device Access

- **One owner, multiple presentations** - treats authenticated desktop, remote
  desktop, and compact/mobile sessions as the same Row-Bot owner; compact mode
  changes layout, not permissions.
- **Authenticated server mode** - adds `row-bot serve` for long-running headless
  operation and keeps server-mode loopback behind authentication instead of
  inheriting the desktop application's implicit local-owner trust.
- **Access management CLI** - adds `row-bot access invite`, listing, revocation,
  and redacted doctor commands without importing the UI runtime or printing a
  stored invitation secret more than once.
- **One-time invitations** - creates origin-bound, expiring invitation links,
  stores only hashes, permits exactly one concurrent claim, and returns terminal
  recovery pages for expired, cancelled, locked, or already-used links.
- **Durable revocable sessions** - adds instance-isolated HttpOnly cookies,
  authoritative server expiry, active renewal, logout, per-session revocation,
  whole-device revocation, pruning, and bounded secret-free audit records.
- **HTTP and WebSocket gate** - applies the same authenticated-owner and exact
  origin rules to normal requests and live connections, validates hosts and
  trusted proxies, and keeps webhook and launcher-control secrets separate.
- **Neutral public boundary** - exposes only minimal health/connect responses to
  unauthenticated callers and prevents invitation, cookie, credential, instance,
  and forwarded-origin details from leaking through errors or logs.
- **Remote Access settings** - adds route status, invitation creation, LAN bind
  controls, current-browser logout, connected devices, active sessions, and
  immediate revocation from both full and compact layouts.
- **Managed private HTTPS** - adds explicit Tailscale detection, reviewed plan
  and apply steps, owned-route conflict/rollback handling, restart-aware policy
  refresh, and precise cleanup of only Row-Bot-managed Serve routes.
- **Browser-local voice** - records in the authenticated browser, transcribes
  locally, and returns session-scoped local speech without starting the host
  microphone service or retaining browser media.
- **Transactional migration and recovery** - preserves legacy mobile owner
  sessions and the existing access database path, and includes the database
  family in backup, restore, and launcher recovery flows.

### Docker, VPS & Secret Persistence

- **Official server image** - adds a locked, multi-stage, non-root image with
  browser support, OCI version/revision labels, a persistent `/data` boundary,
  and a server health probe for amd64 and arm64.
- **Hardened Compose baseline** - publishes to loopback by default, runs with a
  read-only root filesystem, drops capabilities, enables
  `no-new-privileges`, bounds logs, uses tmpfs for ephemeral paths, and provides
  persistent data and secrets volumes.
- **Automatic credential persistence** - initializes an encryption key once in
  the isolated secrets volume and stores provider credentials as encrypted
  records so container replacement does not require re-entering account keys or
  fall back to plaintext.
- **Deployment variants** - adds release-image, source-build, VPS host-network,
  read-only secret-directory, host Caddy, and systemd examples with explicit
  public origins, trusted proxies, and multiple-instance isolation.
- **Complete operator runbook** - documents first invitation, health checks,
  private knowledge model setup, backup/restore, upgrade/rollback, remote voice,
  Tailscale, session recovery, deliberate removal, and credential preservation.
- **Container-aware Developer safety** - refuses nested Docker Sandbox execution
  inside the official container and never silently falls back from a requested
  missing sandbox to local host execution.
- **Owned-resource smoke tests** - adds a deterministic server smoke runner that
  uses a random loopback port, never pulls or builds, redacts secrets, and cleans
  only the exact labeled container and volume it created.
- **Release-only container publication** - verifies native amd64 and arm64
  images on relevant changes, checks release tag/commit identity, smokes images
  before registry login or push, then assembles GHCR multi-architecture version
  manifests and publishes `latest` only for final releases.
- **Reproducible docs publication** - pins the docs build environment,
  regenerates LLM exports before Docusaurus, and normalizes generated artifact
  line endings.

### Mobile, Website & Data Integrity

- **Private startup default restored** - resolves an unspecified bind host to
  direct loopback again while continuing to honor intentional LAN, proxy, and
  server configuration.
- **Safer pairing recovery** - records access-gate rejections and replaces a
  failed or spent pairing form with explicit recovery guidance so it cannot be
  submitted again.
- **Cleaner mobile conversations** - removes internal child-agent threads from
  Recent chats while preserving orchestration activity on the owning parent.
- **Narrow-screen composer fix** - contains the action row, truncates the model
  control, preserves the send button, and respects left, right, and bottom safe
  areas.
- **Evergreen marketing surface** - refreshes the public landing experience with
  progressive enhancement, device-aware desktop install choices, explicit
  mobile/server handoff, complete non-JavaScript fallbacks, and responsive
  navigation.
- **Canonical site navigation** - unifies landing, Features, Architecture,
  Contact, 404, and documentation navigation, metadata, footers, download
  destinations, sitemap entries, and shared assets while removing duplicate
  Docusaurus shadow pages.
- **Wiki Vault ID preservation** - quotes entity IDs in frontmatter and always
  parses `id` as text, preventing numeric-looking IDs from being coerced while
  remaining compatible with legacy unquoted files.

### Tests & Release Validation

- **Orchestration contracts** - covers schema repair, parent/child waves,
  dependencies, capacity, joins, ordered events, approvals, steering, retries,
  stopping, checkpoint identity, exactly-once delivery, and restart recovery.
- **Cross-surface regressions** - verifies Agent cards, transcripts, activity,
  Goals, Workflows, channels, Buddy, voice, Developer, Designer, mobile, and
  filesystem confirmation against the durable runtime.
- **Document durability tests** - covers upload bounds, hashing, deduplication,
  queue transitions, leases, cancellation, restart recovery, sharded publication,
  deterministic retrieval, resumable extraction, and once-only finalization.
- **Access security tests** - covers invitation races, cookie isolation, session
  renewal/revocation, HTTP/WebSocket parity, trusted proxy rules, single-owner
  authority, migrations, backups, Tailscale ownership, and redaction.
- **Container release contracts** - validates Dockerfile/Compose hardening,
  secret persistence, Developer container boundaries, smoke ownership, native
  architecture builds, release identity, and manifest publication order.
- **Documentation contracts** - verifies the Docker and remote-access runbooks,
  canonical marketing routes, mobile handoff, internal links, generated
  references, LLM exports, sitemap ownership, and Pages synchronization.

### Breaking Changes And Caveats

- Every authenticated remote browser is the full Row-Bot owner. Invitation
  links and active browser sessions therefore carry the same authority as the
  local desktop and should be created, shared, and revoked accordingly.
- `row-bot serve` does not grant implicit owner access merely because a request
  arrives from loopback. Create an invitation with `row-bot access invite` and
  authenticate the browser; direct desktop mode retains its local-owner path.
- Direct LAN mode uses unencrypted HTTP. Prefer Tailscale Serve or a correctly
  configured HTTPS reverse proxy outside a trusted private network, and set the
  exact public origin, allowed hosts, and trusted proxy CIDRs.
- Browser microphone capture requires a secure context. Remote voice therefore
  needs HTTPS except for browser-recognized local origins, and local voice model
  downloads remain explicit.
- Docker upgrades must preserve both the data volume and the secrets volume.
  Losing or changing the secrets-volume encryption key makes encrypted account
  credentials unreadable; backing up only `/data` is not a complete credential
  backup.
- Docker Sandbox mode is intentionally unavailable from inside the official
  Row-Bot container. Use an explicitly reviewed local mounted-workspace mode or
  run Developer Studio from the host when isolated Docker execution is needed.
- Document uploads are limited to 256 MiB each and require at least 2 GiB of
  staging free space. Cloud embedding or extraction providers receive document
  chunks only when the user has selected those providers.
- The GHCR image and multi-architecture version manifest become available when
  the GitHub release is published. `latest` is updated only for a non-prerelease
  release; release-pinned Compose deployments should use `4.6.0`.
- Published docs under `docs/assets`, `docs/docs`, `docs/img`, `docs/pagefind`,
  and `docs/search` remain generated artifacts. Update their source and run the
  documented synchronization flow instead of editing them directly.
- Windows signing, macOS notarization, clean-machine installer and container
  smoke tests, previous-version updater checks, and real provider/channel/MCP,
  remote-access, Tailscale, voice, and Computer Use validation remain manual
  release gates after CI artifacts are built.

### Files Changed

| File | Change |
|------|--------|
| `src/row_bot/agent_orchestrator.py`, `agent_runner.py`, `agent_runs.py` | Adds durable parent-led orchestration, ordered events, dependencies, retry, recovery, exactly-once finalization, and worktree-aware results. |
| `src/row_bot/tools/agent_tool.py`, `goals.py`, `tasks.py`, `threads.py` | Connects delegated tools, Goal/Workflow continuation, parent suspension, steering, and transcript-safe orchestration state. |
| `src/row_bot/ui/agent_drawer.py`, `streaming.py`, `transcript.py`, `sidebar.py` | Adds compact Agent groups/cards, live lifecycle rows, shared activity state, and durable transcript restoration. |
| `src/row_bot/document_jobs.py`, `document_uploads.py`, `document_index.py` | Adds bounded staging, durable queue control/recovery, atomic sharded vectors, deterministic retrieval, and health repair. |
| `src/row_bot/document_extraction.py`, `documents.py`, `ui/settings.py`, `ui/status_bar.py` | Adds resumable extraction, idempotent knowledge finalization, document queue operations, progress, and indexed-document management. |
| `src/row_bot/access/*`, `mobile/*`, `ui/access_context.py` | Adds the single-owner access model, invitations, sessions, cookies, middleware, route policy, diagnostics, migration, and compatibility boundary. |
| `src/row_bot/ui/remote_access_settings.py`, `access/tailscale.py`, `tunnel.py` | Adds owner-managed routes, devices/sessions, LAN controls, Tailscale Serve planning/apply/cleanup, and managed-tunnel gating. |
| `src/row_bot/voice/browser_client.py`, `voice/browser_local.py` | Adds secure browser capture, local transcription, and session-scoped browser speech output. |
| `src/row_bot/secret_store.py`, `providers/auth_store.py`, `channels/auth_store.py` | Adds encrypted persistent server credentials, read-only secret files, safe status, and conflict-aware provider/channel resolution. |
| `deploy/docker/*`, `deploy/reverse-proxy/*`, `deploy/systemd/*` | Adds the official image, hardened Compose variants, persistent secrets, Caddy, VPS, systemd, backup, upgrade, and operations examples. |
| `.github/workflows/container.yml`, `scripts/smoke_docker_server.py` | Adds native multi-architecture verification, release-only GHCR publication, manifest policy, and owned-resource container smoke coverage. |
| `src/row_bot/developer/*` | Makes sandbox execution container-aware and fails closed rather than silently substituting local execution. |
| `src/row_bot/ui/mobile.py`, `ui/mobile_chat.py`, `app_port.py`, `launcher.py` | Restores loopback defaults, filters child threads, fixes the compact composer, and supports authenticated headless launch. |
| `src/row_bot/wiki_vault.py` | Preserves numeric-looking entity IDs as text across export, search, and import. |
| `docs-site/docs/operations/*`, `deploy/docker/README.md`, `installer/README.md` | Documents remote access, Docker/VPS operation, credentials, backup/restore, upgrade, recovery, and container limitations. |
| `docs/site.css`, `docs/site.js`, `docs/features.html`, `docs/architecture.html`, `docs/contact.html` | Aligns the evergreen marketing and documentation navigation, mobile handoff, shared styling, metadata, and canonical routes. |
| `tests/subsystem/agents/*`, `tests/subsystem/knowledge_graph/*`, `tests/subsystem/access/*` | Adds deterministic orchestration, document-ingestion, and authenticated-access subsystem coverage. |
| `tests/integration/access/*`, `tests/contracts/installers/*`, `tests/docs/*` | Adds cross-route access security, Docker/release workflow, remote access, and public documentation contracts. |

---

## v4.5.0 - Secure Computer Use, Agent Budgets & Public Documentation

This release builds on v4.4.0 with a native-computer-control, agent-safety,
local-retrieval, and documentation pass. It adds an opt-in Computer Use beta
for native Windows and macOS applications, introduces checkpoint-safe work
budgets and configurable child-agent capacity, makes local embedding recall
recover cleanly when a cached model is unavailable or still loading, hardens
browser and live-control isolation, and publishes a comprehensive searchable
user guide with reviewed screenshots and machine-readable references.

### Native Computer Use (Beta)

- **Native application control** - adds a provider-neutral `computer_use` tool
  for launching allowlisted applications, selecting one target window, reading
  its accessibility tree or screenshot, clicking, typing, pressing keys,
  scrolling, and dragging in native desktop interfaces.
- **Browser remains separate** - keeps DOM-based browser automation as the
  preferred path for websites and routes native applications and operating
  system dialogs through the new Computer Use engine instead.
- **Task-scoped exclusive sessions** - gives one interactive local task an
  exclusive Computer Use lease covering discovery, capture, optional Vision
  fallback, and input, preventing overlapping agents from controlling the
  desktop at the same time.
- **Generation-bound targeting** - uses opaque target and element tokens that
  expire after observations, reconnects, approvals, target drift, and user
  takeover so stale coordinates or accessibility nodes cannot be replayed.
- **Live control card** - shows the active application, bounded ephemeral
  preview, current state, and direct Stop, Take over, and Resume controls in
  chat while shielding the preview during user or approval handoff.
- **Human takeover** - cancels queued mutations before handing control to the
  user, retains the paused lease, and requires a fresh target-window
  observation before automation can resume.
- **Point-of-risk approvals** - classifies routine and consequential desktop
  actions, always confirms foreground escalation and sensitive consequences,
  and hands credentials, OTPs, CAPTCHAs, biometrics, UAC/TCC, terminals,
  password managers, and secure desktops back to the user.
- **Vision fallback** - can send an ephemeral target-window screenshot to the
  configured Vision provider only when accessibility information is
  insufficient, with the active local or cloud disclosure shown in Settings.
- **Interactive-only boundary** - keeps Computer Use unavailable to schedules,
  channels, background workflows, child agents, headless/server callers, and
  plugin or general MCP exposure.

### Computer Use Setup, Privacy & Recovery

- **Off-by-default setup** - adds a state-driven Settings flow for enablement,
  installation, health checks, a Calculator verification run, repair,
  reinstall, removal, and an explicitly configured reviewed system binary.
- **Reviewed Cua runtime** - pins Cua Driver Rust 0.7.1 for Windows x86-64,
  Windows ARM64, and macOS universal, downloads it only after an explicit
  Install action, verifies the platform SHA-256, and extracts it into Row-Bot's
  private data directory without invoking the upstream installer or updater.
- **Mandatory telemetry disclosure** - requires Continue or Cancel before any
  Cua executable invocation and documents the reviewed upstream telemetry
  fields and endpoint; Row-Bot still adds no first-party telemetry.
- **Narrow driver allowlist** - exposes only the reviewed application,
  target-window observation, input, health, permission, configuration, and
  session operations while blocking recording, desktop-wide capture, CDP,
  arbitrary configuration, updates, autostart, telemetry mutation, skills,
  process control, and maintenance surfaces.
- **Ephemeral sensitive data** - excludes typed values from logs, history,
  checkpoints, approval payloads, memory, and durable media, and does not
  persist target-window screenshots.
- **macOS permission recovery** - detects missing Accessibility and Screen
  Recording access, explains the recovery steps, links directly to the
  relevant Privacy & Security panes, and rechecks readiness after permissions
  are granted or the app is re-added.
- **Runtime readiness diagnostics** - distinguishes disabled, disclosure,
  install, permission, test, ready, repair, and unsupported states with
  actionable, non-technical recovery guidance.

### Agent Runtime Budgets & Delegation

- **Checkpoint-safe work budgets** - gives every new logical agent turn an
  explicit model-iteration budget, charges one round per successful model
  response, persists progress through interrupts, and derives the LangGraph
  recursion ceiling from remaining capacity.
- **Graceful budget completion** - reserves an exactly-once, tool-free final
  response when the work-round budget is exhausted instead of surfacing a raw
  recursion error or leaving the run in an ambiguous state.
- **Repeated-action protection** - detects identical tool requests without
  storing their arguments, blocks the fourth repeat, and terminates continued
  no-progress loops on the fifth request.
- **Configurable runtime limits** - adds Settings controls for maximum work
  rounds, nested agent levels, active children per parent, active children
  across the app, and an optional child active-time limit.
- **Reviewed defaults** - starts new runs with 90 work rounds, one child level,
  three active children per parent, eight active children application-wide,
  and no child timeout.
- **Queued child capacity** - queues excess child agents in order until both
  parent and global capacity are available instead of rejecting them, while
  preserving Stop behavior for queued work.
- **Run snapshots and progress** - snapshots effective settings into durable
  Agent Run rows, records safe model-iteration counters and heartbeats, and
  keeps active runs on the limits they started with when Settings change.
- **Optional active-time timeout** - applies the configured child limit only
  while a child is executing, excludes queue time, and records a clear terminal
  timeout reason.

### Local Embeddings & Memory Recall Reliability

- **Working first-run default** - clearly discloses and selects the recommended
  private knowledge model download during first-run setup (about 700 MB), while
  preserving an opt-out and keeping ordinary recall cache-only.
- **Bounded model payload** - excludes unused GGUF, ONNX, and OpenVINO exports
  from local embedding downloads so the default setup fetches the runtime files
  Row-Bot uses instead of the full multi-format model repository.
- **Strict cache-only runtime** - resolves local embedding models from the
  existing Hugging Face cache and sets local-only loading so normal recall,
  indexing, status checks, and startup cannot trigger a surprise model
  download.
- **Explicit download and repair** - keeps network access behind the user-run
  local-model download or repair action and reports whether the selected model
  is cached, missing, loading, ready, or failed.
- **Shared background loading** - coalesces concurrent recall callers onto one
  local embedding load and gives the first recall a bounded grace period rather
  than starting duplicate model loads.
- **Fast deterministic fallback** - remembers missing, failed, or timed-out
  local-model state so later recall attempts fail fast and continue through
  bounded lexical and graph retrieval instead of stalling the response.
- **Visible fallback notice** - emits one safe per-generation notice explaining
  when semantic memory search fell back and points to the local model setup
  action without exposing memory content.
- **Recall diagnostics** - records semantic status, fallback code, wait time,
  retrieval-stage timings, selected counts, and bounded ranking details for
  troubleshooting.
- **Workflow-safe retrieval** - applies the same bounded fallback to agent
  turns and workflow knowledge lookups while preserving existing provenance,
  ranking, and no-mutation recall policy.

### Browser & Interactive Control Hardening

- **Thread and task isolation** - scopes browser tabs and live-control state to
  their owning thread/task and cleans them up when work finishes or a thread is
  removed.
- **Navigation policy** - validates browser destinations and redirects before
  mutation so unsafe or out-of-scope navigation cannot silently broaden an
  agent's authority.
- **Consequence-aware actions** - applies shared approval and consequence
  classification to browser mutations while keeping observation and control
  state explicit.
- **History redaction** - prevents sensitive typed values and unsafe action
  details from leaking into durable browser history or tool traces.
- **Cancellation and readiness** - makes browser operations responsive to Stop,
  reports unavailable runtimes clearly, and keeps takeover/resume state
  consistent with Computer Use.
- **MCP result isolation** - normalizes private Computer Use MCP results without
  registering its driver as a general MCP server or exposing unreviewed tools.

### Public Documentation & Website

- **Comprehensive user guide** - expands the Docusaurus site with concepts,
  request lifecycle, profiles/goals/agents, workflows, integrations, extension
  trust, operations, mobile/native guidance, knowledge provenance and repair,
  wiki vault, settings, and reference pages.
- **Control-level Settings reference** - generates a searchable inventory of
  visible Settings controls, defaults, declared ranges, dependencies, restart
  notes, security notes, and source locations.
- **Reviewed UI screenshot library** - refreshes desktop Settings and Home
  images and adds chat, approval, tool trace, profiles, workflows, Developer,
  Designer, Skills Hub, Plugin Marketplace, MCP, and mobile screenshots from an
  isolated neutral demonstration profile.
- **Safe capture automation** - refuses the normal Row-Bot data directory,
  disables background autostart and network status checks, seeds display-only
  provider/channel/plugin/MCP states, and validates screenshot review metadata.
- **Searchable static publication** - adds Pagefind search, committed GitHub
  Pages output under `docs/`, a generated sitemap, and structural validation of
  the published search artifacts.
- **Machine-readable docs** - publishes `llms.txt` and `llms-full.txt` alongside
  the human documentation and links them from the docs footer.
- **Docs CI coverage** - regenerates inventories and LLM files, checks generated
  references, validates screenshots and source, builds the review report and
  Docusaurus site, runs docs tests, and verifies the committed Pages artifact.
- **Publication synchronization** - adds a deterministic sync command that
  refreshes only generated documentation assets while preserving the
  hand-curated marketing homepage, feature pages, contact form, analytics, and
  shared site assets.
- **Navigation and mobile fixes** - restores reliable Home/Docs navigation,
  aligns the landing, feature, architecture, and contact pages, and improves
  responsive layouts without changing the desktop visual system.
- **Maintenance contract** - documents the authoritative UI coverage map,
  screenshot capture/review workflow, publication steps, and generated-file
  ownership, with repository attributes marking built docs as generated.

### Tests & Release Validation

- **Computer Use contracts** - adds deterministic fake-driver coverage for the
  private client, allowlist, leases, observation tokens, actions, approvals,
  cancellation, takeover, text privacy, Vision fallback, readiness, runtime
  manifest, installer package data, and UI state.
- **Agent budget coverage** - tests checkpoint schema, iteration charging,
  repeated-action termination, exactly-once finalization, settings validation,
  FIFO capacity queues, nesting limits, active-time timeout, and durable run
  progress.
- **Browser regressions** - adds focused cancellation, consequence, redaction,
  live-control, navigation, readiness, and tab-isolation tests.
- **Embedding regressions** - covers cache-only model resolution, shared loads,
  missing/failed/timeout fast paths, explicit repair downloads, fallback
  notices, recall timing, workflow knowledge lookup, and bounded ranking.
- **Documentation automation tests** - validates source coverage, screenshot
  metadata, generated references, Pagefind artifact structure, Pages sync, and
  cross-platform path handling.

### Breaking Changes And Caveats

- Computer Use is a beta, is off by default, and supports reviewed Windows and
  macOS artifacts only. Linux, unattended/background automation, channels,
  schedules, workflows, child agents, and headless callers are not supported.
- Enabling Computer Use requires accepting the separate Cua Driver telemetry
  disclosure. Cua telemetry is third-party behavior, not Row-Bot telemetry;
  canceling the disclosure leaves the tool disabled and does not install or run
  Cua.
- The Computer Use component is downloaded on demand and therefore needs
  network access for initial install or repair. Normal use runs the verified
  private local executable with its updater disabled.
- macOS users must grant Accessibility and Screen Recording access to the
  relevant Row-Bot/Cua application entries. Permission changes may require the
  app entry to be removed, re-added, and rechecked.
- Local embedding models are never downloaded implicitly during recall or
  ordinary startup. First-run setup clearly discloses and selects the
  recommended private knowledge model download by default, with an opt-out. If
  it is skipped or later damaged, use the explicit Settings action; recall
  continues with lexical/graph fallback in the meantime.
- Agent runtime limit changes apply only to new runs. A low work-round or child
  active-time limit can stop long tasks before they finish, while queued child
  time does not count toward the active-time limit.
- Published docs under `docs/assets`, `docs/docs`, `docs/img`, `docs/pagefind`,
  and `docs/search` are generated artifacts. Update their source and run the
  documented synchronization flow instead of editing them directly.
- Windows signing, macOS notarization, clean-machine artifact smoke tests,
  previous-version updater checks, and real provider/channel/MCP/Computer Use
  validation remain manual release gates after CI artifacts are built.

### Files Changed

| File | Change |
|------|--------|
| `src/row_bot/computer_use/*`, `src/row_bot/tools/computer_use_tool.py` | Adds the reviewed Cua client, runtime manifest/install/readiness flow, policy, exclusive service, and model-visible native application tool. |
| `src/row_bot/ui/computer_use.py`, `src/row_bot/ui/live_control.py` | Adds Computer Use Settings, telemetry disclosure, installation/repair, macOS permission recovery, ephemeral preview, and Stop/Take over/Resume UI. |
| `src/row_bot/tools/browser_tool.py`, `src/row_bot/ui/tool_trace.py`, `src/row_bot/ui/streaming.py` | Hardens browser and Computer Use routing, cancellation, navigation, redaction, live-control state, and tool traces. |
| `src/row_bot/agent_budget.py`, `src/row_bot/agent_settings.py`, `src/row_bot/agent.py` | Adds checkpointed model-iteration capacity, repeated-action detection, graceful terminal finalization, and typed local runtime settings. |
| `src/row_bot/agent_runner.py`, `src/row_bot/agent_runs.py`, `src/row_bot/tools/agent_tool.py` | Adds child-agent capacity queues, nesting and active-time limits, settings snapshots, budget progress, and durable terminal reasons. |
| `src/row_bot/ui/settings.py`, `src/row_bot/ui/agent_drawer.py`, `src/row_bot/tools/row_bot_status_tool.py` | Exposes agent runtime limits, Computer Use readiness, and safe live capacity/progress status. |
| `src/row_bot/embedding_providers.py`, `src/row_bot/embedding_config.py`, `src/row_bot/memory_policy.py` | Adds strict cache-only local embedding resolution, shared background loading, explicit download/repair, fallback notices, and diagnostics. |
| `src/row_bot/knowledge_graph.py`, `src/row_bot/documents.py`, `src/row_bot/tasks.py` | Applies bounded semantic fallback and timing to memory, document, and workflow retrieval paths. |
| `src/row_bot/mcp_client/requirements.py`, `runtime.py`, `results.py` | Adds private Cua runtime resolution and result handling without exposing the driver as a general MCP integration. |
| `docs/COMPUTER_USE_SECURITY.md`, `AGENTS.md`, `CONTRIBUTING.md` | Records the accepted Computer Use dependency/telemetry decision and tightens contributor rules for third-party telemetry review. |
| `docs-site/docs/*`, `docs-content/metadata/*`, `docs-site/static/img/screenshots/*` | Expands the public guide, generated references, coverage metadata, and reviewed real-UI screenshot set. |
| `scripts/docs/*`, `docs-content/MAINTENANCE.md`, `.github/workflows/docs.yml` | Adds safe docs capture/generation, review, validation, Pagefind build, GitHub Pages synchronization, and CI verification. |
| `docs/assets/*`, `docs/docs/*`, `docs/img/*`, `docs/pagefind/*`, `docs/search/*` | Publishes the generated Docusaurus, screenshot, search, sitemap, and machine-readable documentation artifacts. |
| `docs/index.html`, `docs/features.html`, `docs/architecture.html`, `docs/contact.html`, `docs/site.css`, `docs/site.js` | Overhauls the 4.4 marketing site, aligns navigation and shared styling, and improves responsive layouts. |
| `src/row_bot/version.py`, `scripts/cut_release.py`, `Start Row-Bot.command`, `installer/*`, `.github/workflows/release.yml` | Prepares the 4.5.0 runtime, CI, Windows, macOS, and Linux release surfaces and keeps the legacy macOS launcher fallback under the canonical release cutter. |
| `tests/contracts/*`, `tests/integration/computer_use/*`, `tests/subsystem/computer_use/*` | Adds deterministic Computer Use contracts, end-to-end fake-driver flows, safety invariants, installer coverage, and UI integration tests. |
| `tests/subsystem/agents/*`, `tests/subsystem/browser/*`, `tests/test_embedding_provider_config.py`, `tests/test_generation_stop.py` | Adds agent-budget, delegation, browser, local-embedding, fallback, and cancellation regression coverage. |

---

## v4.4.0 - Mobile Companion, Channel Streaming & Runtime Reliability

This release builds on v4.3.0 with a mobile-access, messaging, provider, and
runtime-reliability pass. It adds a secure browser-first mobile companion,
brings live agent streaming and interactive approvals to external channels,
surfaces child-agent approvals and completion notices in parent conversations,
makes Stop cancel stalled provider streams and subprocess-backed tools, restores
automatic model discovery with last-known-good catalog protection, hardens
Gemini tool-schema compatibility and Google Calendar concurrency, and expands
the recommended MCP catalog with Xquik.

### Mobile Web Companion

- **Phone-native shell** - adds a full-screen mobile interface with Chat,
  Activity, Workflows, Knowledge, and Settings navigation instead of shrinking
  the desktop drawers and studio chrome onto a phone display.
- **Mobile chat workflow** - adds conversation list/detail views, new-thread
  creation, attachments, skills, profile and model controls, generation status,
  and a persistent Stop control using the same durable threads as desktop.
- **Activity and approvals** - shows active chat generations, running workflows,
  recent workflow runs, tool approvals, and workflow approvals in one phone-safe
  surface with approve, deny, and stop actions.
- **Mobile workflow editor** - supports simple prompt workflows, schedules,
  profiles, model overrides, approval policy, persistent threads, channel
  delivery, enablement, and metadata while preserving advanced graph steps for
  desktop editing.
- **Mobile-safe settings** - adds stacked provider status and credential
  summaries, local skill enable/pin controls, and installed plugin enablement.
  Skills Hub and Plugin Marketplace installation/configuration remain desktop
  actions in Mobile V1.
- **Installable web app** - adds a PWA manifest, service worker, install
  metadata, and offline page while deliberately excluding authenticated and
  private Row-Bot surfaces from service-worker caching.
- **Mobile Access settings** - adds route discovery and phone pairing under
  Settings -> System for local-network access, optional Tailscale direct or
  Serve routes, an existing ngrok tunnel, and advanced custom origins.
- **QR pairing and device control** - creates short-lived single-use QR links,
  stores session tokens in HttpOnly cookies, lists paired phones and access
  events, and lets an authorized user revoke a phone immediately.
- **Remote access gate** - protects remote HTTP and WebSocket traffic while
  keeping direct loopback desktop access open, rejecting forwarded-header
  localhost bypasses, and redirecting unpaired remote clients to pairing.
- **Local-first auth storage** - stores hashed pairing and device secrets,
  device scopes, revocation state, failed-attempt lockouts, and display-safe
  audit events in a dedicated local SQLite database.

### Channel Streaming, Approvals & Delivery

- **Shared streaming engine** - adds deterministic token and tool-event
  delivery with coalesced edits, typing keepalives, bounded previews,
  rate-limit-aware retries, platform-safe splitting, preview cleanup, and final
  send fallback when an edit cannot be completed.
- **Discord streaming** - streams through edited messages, keeps typing active,
  splits long final answers, retries with a fresh final message when necessary,
  and presents interactive approval buttons.
- **Slack native streaming** - uses Slack's native stream APIs when available
  and appropriate, falls back to edited messages when they are not, and adds
  Block Kit approval actions plus bounded retry-after handling.
- **Telegram draft streaming** - uses native message drafts for supported
  private chats, falls back to edit streaming elsewhere, respects UTF-16 message
  limits, and adds inline approve/deny controls.
- **WhatsApp and SMS behavior** - adds WhatsApp edit streaming, typing, split
  finals, and approval resume. SMS intentionally remains final-text-only with
  safe chunking and explicit YES/NO approval handling.
- **Goal Mode and plugin channels** - routes normal turns, Goal Mode turns,
  interrupted/resumed turns, and plugin-owned channel turns through the shared
  delivery path without duplicate final responses.
- **Checkpoint repair** - persists delivered assistant answers when a channel
  checkpoint contains only the human turn, without duplicating an assistant
  message already written by the agent graph.
- **Durable terminal notices** - delivers compact, once-only child-agent and
  Goal Mode completion/failure notices, retains failed notices for retry, and
  reconciles them when configured channels start again.

### Child Agents, Approvals & Generation Control

- **Parent-thread approval surfacing** - appends durable, deduplicated child
  agent approval requests to the parent conversation so background work cannot
  wait invisibly.
- **Clear approval reasons** - prefers the model-supplied reason in approval
  cards, redacts and bounds display text, and retains the raw action payload for
  the actual safety decision.
- **Shell and Developer rationale** - adds an explicit approval-reason field to
  shell and Developer commands so requested command execution can explain why
  it is needed.
- **Child-agent lifecycle messages** - preserves direct and delegated run
  metadata, completion summaries, queued-turn ordering, checkpoint reloads, and
  once-only rendering across parent and child conversations.
- **Shared cancellation scope** - links an active generation to provider
  requests, subprocesses, tool waits, and its spawned child runs so Stop has one
  consistent cancellation path.
- **Stalled provider cancellation** - closes in-flight direct OpenAI,
  Anthropic, xAI, MiniMax, OpenRouter, OpenCode, subscription, and compatible
  transport responses so a blocked network stream no longer leaves a thread
  stuck in Thinking.
- **Tool and process cancellation** - extends Stop to shell commands, Developer
  processes, MCP probes and calls, browser operations, voice turns, and other
  subprocess-backed work, with a clear stopped result where appropriate.
- **Scoped child cancellation** - stops only child runs linked to the cancelled
  generation and wakes/detaches queued generation state without terminating
  unrelated agent work.

### Providers, Models & Tool Schemas

- **Automatic catalog discovery restored** - returns Codex and other registered
  providers to targeted and scheduled model refreshes, including discovery of
  newly available subscription models.
- **Last-known-good catalogs** - preserves provider-specific cached rows when a
  refresh is empty, fails, or loses a later pagination page, and replaces only
  rows for a provider whose refresh completed successfully.
- **Visible catalog provenance** - reports live, cached, and fallback outcomes
  in provider settings so a retained catalog is distinguishable from a fresh
  provider response.
- **Provider-scoped schema policy** - adds a compatibility inspector for tool
  input schemas without rewriting the tools used by unaffected transports.
- **Gemini array compatibility** - checks the effective Google adapter output
  for typed array `items`, filters optional incompatible tools while preserving
  order, and fails clearly when an explicitly requested tool is incompatible.
- **Built-in schema repairs** - gives Gmail recipient arrays and Goal
  evidence/blocker arrays concrete item types while continuing to normalize
  legacy scalar or structured inputs safely.
- **Provider matrix coverage** - expands deterministic and opt-in live-provider
  policy checks for catalog ownership, pagination, routing, current-model
  discovery, media capability, and tool-schema conversion.

### Google Calendar & MCP

- **Request-scoped Calendar clients** - creates an independent Google Calendar
  service for each operation so concurrent tool calls do not share an unsafe
  client instance.
- **Safe concurrent OAuth refresh** - makes Calendar token refresh single-flight
  and atomic when several operations discover an expired token together.
- **Serialized mutations and bulk create** - orders same-turn writes and adds a
  native multi-event create operation that preserves result order and reports
  partial failures clearly.
- **Retry and reconciliation** - retries transient SSL and backend failures with
  fresh services, returns structured permanent errors, and reconciles ambiguous
  timeouts so a backend-committed event is not created twice.
- **Typed Calendar operations** - preserves search, create, update, move, and
  delete while exposing typed attendee, conference, timezone, notification,
  calendar, and bulk-event inputs.
- **Xquik MCP catalog entry** - adds Xquik for X/Twitter data access through
  streamable HTTP with API-key header setup, capability metadata, and explicit
  high-risk approval guidance for its generic executor.
- **MCP configuration integrity** - preserves marketplace metadata when an
  installed server is edited, refreshes cached agent tools after MCP changes,
  normalizes interrupted probes, and avoids repeatedly restarting failed
  servers until an explicit refresh.
- **Generated reference refresh** - updates public generated references for the
  expanded channel, mobile, MCP, provider, approval, settings, storage, and tool
  inventories.

### UI Fixes, Tests & Release Validation

- **Active-thread spinner restored** - returns the sidebar generation spinner
  to the active thread and limits it to genuinely streaming state while
  preserving pinned-thread and recency ordering.
- **Mobile test coverage** - adds deterministic subsystem and integration tests
  for pairing, cookies, access gating, remote routes, PWA privacy, Tailscale,
  chat, workflows, settings, and mobile-shell routing.
- **Channel test coverage** - adds transport and shared-engine tests for edit
  cadence, native fallback, typing, rate limits, overflow, message splitting,
  approvals, checkpoint persistence, Goal Mode, terminal notifications, and
  plugin-channel delivery.
- **Cancellation test coverage** - adds focused tests for provider HTTP
  cancellation, subprocess termination, generation Stop, shell, browser, MCP,
  Developer, voice, and generation-linked child agents.
- **Provider and tool coverage** - adds model-catalog failure/pagination tests,
  Gemini schema contracts, Gmail and Goal schemas, Xquik configuration, and
  Google Calendar concurrency, retry, reconciliation, and bulk-create tests.
- **Run and approval regressions** - expands coverage for queued control
  messages, child-agent lifecycle cards, approval display safety, thread titles,
  tool filtering, and active-run rendering.

### Breaking Changes And Caveats

- Mobile is a companion to a running Row-Bot host, not a separate cloud-hosted
  service. The desktop host must remain running and reachable through the
  selected local network, Tailscale, ngrok, or custom route.
- Remote routes are pairing-gated, but exposing Row-Bot through a public tunnel
  still makes the pairing endpoint internet-reachable. Keep tunnel URLs and
  short-lived pairing links private, review access events, and revoke devices
  that are no longer trusted.
- Mobile V1 does not expose Developer Studio, Designer Studio, Skills Hub
  install/create flows, Plugin Marketplace install/configure/update flows, or
  every advanced workflow graph control. Those remain desktop-only.
- Channel streaming depends on platform capabilities and configuration. Slack
  and Telegram fall back to edit streaming when native streaming is unavailable;
  SMS remains final-text-only by design, and long messages may arrive in parts.
- Gemini now filters optional tools whose effective schemas contain untyped
  arrays. If an incompatible tool was explicitly selected, agent creation fails
  with a schema error instead of sending a request Gemini will reject.
- Xquik's generic executor remains classified high risk. Review its requested
  action and metered/private-data implications before approving it.
- Windows signing, macOS notarization, clean-machine artifact smoke tests,
  previous-version updater checks, and real provider/channel/MCP validation
  remain manual release gates after CI artifacts are built.

### Files Changed

| File | Change |
|------|--------|
| `src/row_bot/mobile/*`, `src/row_bot/ui/mobile*.py`, `src/row_bot/ui/head_html.py` | Adds mobile authentication/storage/routes, access gating, PWA metadata, phone-native chat/activity/workflow/settings surfaces, and mobile layout behavior. |
| `src/row_bot/app.py`, `src/row_bot/ui/settings.py`, `src/row_bot/ui/chat*.py`, `src/row_bot/ui/state.py` | Routes paired phones into the mobile shell, adds Mobile Access settings, and integrates shared mobile chat and generation controls. |
| `src/row_bot/channels/streaming.py`, `src/row_bot/channels/thread_notifications.py` | Adds the shared streaming engine and durable child-agent/Goal Mode channel notification delivery. |
| `src/row_bot/channels/discord_channel.py`, `slack.py`, `telegram.py`, `whatsapp.py`, `sms.py` | Adds platform-aware live delivery, approvals, splitting, fallback, checkpoint persistence, and final-text-only SMS behavior. |
| `src/row_bot/plugins/channel_runtime.py`, `src/row_bot/channels/runtime.py` | Integrates plugin-owned channels and Goal Mode with shared streaming, resume, and terminal-notification behavior. |
| `src/row_bot/agent_run_messages.py`, `approval_messages.py`, `agent_runner.py`, `agent_runs.py`, `tasks.py` | Adds durable parent/child lifecycle and approval messages, display-safe approval reasons, source auditing, and terminal notice state. |
| `src/row_bot/cancellation.py`, `process_cancellation.py`, `src/row_bot/providers/transports/*cancellable*.py` | Adds generation-scoped HTTP and subprocess cancellation and transport adapters for stalled direct-provider streams. |
| `src/row_bot/providers/runtime.py`, `src/row_bot/tools/shell_tool.py`, `browser_tool.py`, `src/row_bot/developer/runtime.py`, `src/row_bot/mcp_client/runtime.py` | Propagates Stop through providers, shell, browser, Developer, and MCP operations. |
| `src/row_bot/providers/model_catalog_cache.py`, `src/row_bot/models.py`, `src/row_bot/providers/codex.py`, `src/row_bot/ui/provider_settings.py` | Restores automatic model discovery, protects last-known-good provider rows, and surfaces live/cached/fallback catalog state. |
| `src/row_bot/providers/tool_schema.py`, `src/row_bot/agent.py`, `src/row_bot/tools/gmail_tool.py`, `goal_tool.py` | Adds provider-scoped schema inspection, Gemini compatibility filtering, and typed built-in array schemas. |
| `src/row_bot/tools/calendar_tool.py` | Adds request-scoped Calendar clients, safe OAuth refresh, serialized writes, bulk create, retries, reconciliation, and typed operation contracts. |
| `src/row_bot/mcp_client/recommended_servers.json`, `config.py`, `conflicts.py`, `runtime.py`, `src/row_bot/ui/mcp_settings.py` | Adds and hardens Xquik marketplace integration, metadata preservation, runtime cache invalidation, and interrupted-probe handling. |
| `src/row_bot/ui/sidebar.py`, `src/row_bot/threads.py`, `src/row_bot/ui/render.py`, `streaming.py` | Restores active-thread activity feedback and hardens titles, run rendering, approvals, and generated response persistence. |
| `README.md`, `docs/ARCHITECTURE.md`, `docs/RELEASING.md`, `docs/SOURCE_LAYOUT.md`, `installer/README.md` | Updates user-facing capabilities, mobile setup/security, architecture ownership, release workflow, packaging coverage, and 4.4.0 installer examples. |
| `docs-site/docs/*`, `scripts/docs/*` | Refreshes user-guide and generated reference content for approvals, channels, MCP, providers, settings, storage, and tools. |
| `src/row_bot/version.py`, `installer/row_bot_setup.iss`, `installer/install_deps.bat`, `installer/Row-Bot.app/Contents/Info.plist`, `.github/workflows/*` | Bumps 4.4.0 version sources, installer copy, workflow defaults, macOS bundle metadata, and the docs CI Python baseline. |
| `scripts/cut_release.py`, `.github/ISSUE_TEMPLATE/bug_report.yml`, `tests/test_brand_constants.py`, `tests/test_linux_support.py` | Extends release automation and contracts for installer copy, user-agent versions, recursive mobile payload coverage, and the 4.4.0 bug-report placeholder. |
| `tests/subsystem/mobile/*`, `tests/integration/mobile/*` | Adds deterministic mobile pairing, security, PWA, route, shell, chat, workflow, and settings coverage. |
| `tests/subsystem/channels/*`, `tests/subsystem/providers/*`, `tests/subsystem/tools/*`, `tests/test_*.py` | Adds focused channel streaming, approval, cancellation, provider catalog/schema, MCP, Calendar, thread, and run-state regression coverage. |

---

## v4.3.0 - Plugin System v2, Requesty, Prompt Cache & Release Hardening

This release builds on v4.2.0 with a broad extension, provider, workflow, and
release-readiness pass. It completes Plugin System v2, adds plugin-owned
channels and marketplace tooling, introduces Requesty as a first-class
provider, makes prompt context and Anthropic prompt-cache behavior explicit,
moves workflows to profile-first agent execution, adds Developer Studio
worktrees, replaces the macOS tray fallback with a native tray host, removes
the old Thoth rebrand migration from startup, and hardens CI, dependency, and
installer validation before the 4.3.0 release.

### Plugin System v2 & Marketplace

- **Manifest v2 contract** - defines `schema_version: 2` plugin manifests with
  supported extension surfaces for native tools, plugin-packaged MCP servers,
  bundled skills, and channels.
- **Declarative plugin metadata** - adds validation for plugin IDs, versions,
  minimum Row-Bot versions, permissions, settings, secrets, auth declarations,
  health checks, and supported `provides` entries.
- **Plugin API expansion** - extends `plugins.api` with public tool, channel,
  attachment, outbound callback, result, settings, secret, webhook, pairing,
  allowlist, and health-check helpers so plugins can integrate without
  importing Row-Bot internals.
- **Native Plugin Center** - expands the in-app plugin UI to render metadata,
  permissions, settings, secrets, auth status, health checks, tools, channels,
  bundled skills, logs, marketplace updates, enablement, reload, and uninstall
  controls from Row-Bot-owned UI.
- **Marketplace install/update flow** - adds cached marketplace indexes,
  checksum-aware local or remote installs, stale-cache fallback, installed
  version/update metadata, disabled-by-default installation, and immediate
  runtime refresh after plugin changes.
- **Plugin developer tools** - adds plugin linking, reload, doctor, validation,
  local marketplace index generation, devtool helpers, and examples for native
  tools, settings/secrets, MCP-backed tools, and fake channels.
- **Plugin templates** - adds native-tool, MCP-tool, and channel templates under
  `src/row_bot/plugins/templates/` for repeatable plugin authoring.
- **Plugin safety sandbox** - tightens loader validation around dangerous
  constructs, unsupported extension surfaces, UI framework imports, Row-Bot
  internal imports, old Thoth manifests, stale code, plugin dependencies, and
  runtime refresh behavior.
- **Plugin skill scoping** - keeps plugin-provided skills tied to plugin
  enablement and Agent Profile tool boundaries, so selected-tool profiles do
  not receive unrelated plugin instructions.
- **Plugin MCP bridge** - lets plugin-packaged MCP server declarations follow
  plugin enablement and appear or disappear with the owning plugin's runtime
  inventory.

### Plugin Channels, Messaging & Bot Auth

- **Public plugin channel runtime** - adds a core bridge that lets plugin-owned
  channels route inbound messages through Row-Bot's normal channel, agent,
  Goal Mode, approval, media, and generated-file delivery paths.
- **Plugin channel attachment handling** - reuses the shared channel media
  pipeline for plugin-channel audio transcription, image analysis, document
  extraction, inbox persistence, workspace copy, size limits, and generated
  image/video/file delivery.
- **Plugin webhooks** - adds namespaced plugin webhook registration under
  `/plugin-webhooks/{plugin_id}/{name}`, with webhook handlers disabled when
  the owning plugin is disabled, unloaded, uninstalled, or fails load.
- **Bot Framework auth helpers** - adds plugin-facing helpers for Bot Framework
  JWT validation, OpenID/JWKS discovery, issuer/audience checks, and
  display-safe failure reporting.
- **Channel registry integration** - teaches the channel registry and tool
  factory how to include plugin-owned channels while preserving native channel
  capability checks and generated send/photo/document tools.
- **Thread-scoped channel commands** - fixes Telegram model switching and
  related channel commands so model changes stay scoped to the active channel
  thread instead of leaking across conversations.
- **Channel output assembly** - centralizes channel answer/tool-report assembly
  so plugin and native channel responses have the same readable final shape.
- **Channel UI polish** - refreshes channel monitor and sidebar status surfaces
  so running native and plugin channels stay visible without crowding the
  conversation list.

### Providers, Prompt Context & Workflow Agents

- **Requesty provider** - adds Requesty as a first-class OpenAI-compatible
  provider with provider definition, setup/settings support, auth mapping,
  catalog URL handling, provider-qualified refs, and routing through the shared
  OpenAI-compatible transport.
- **Requesty catalog normalization** - maps Requesty's `context_window`,
  `supports_tool_calling`, `supports_reasoning`, `supports_vision`,
  modality, task, and metadata fields into Row-Bot capability snapshots.
- **Requesty surface filtering** - filters embedding, audio, image, video,
  moderation, realtime, and other non-chat Requesty rows out of Brain/agent
  surfaces while preserving vision and tool-capable chat models.
- **Provider credential dialogs** - moves provider credential collection into
  row-specific dialogs so Settings can avoid exposing or confusing unrelated
  provider secret fields.
- **Provider selection hardening** - improves provider resolution, catalog
  cache behavior, readiness checks, and Quick Choice compatibility for
  provider-qualified refs, Requesty, xAI OAuth, and existing providers.
- **Explicit prompt context contract** - splits prompt assembly into named
  stable and ephemeral sections so identity, profile, platform, self-knowledge,
  tools, skills, plugins, background overrides, memory recall, date/time,
  Developer, Designer, channel, and history context have deterministic cache
  behavior.
- **Anthropic prompt-cache markers** - applies Anthropic `cache_control`
  markers only to eligible stable system content for the direct Anthropic API,
  while keeping conversation history and ephemeral turn data unmarked.
- **Prompt cache metrics** - normalizes provider prompt-cache read/write token
  counts across common metadata shapes for diagnostics and status reporting.
- **Profile-first workflow agents** - migrates workflows toward Agent Profile
  execution, maps compatible legacy workflow policies to built-in or generated
  profiles, preserves review/blocked status when policy cannot be mapped, and
  surfaces migration notes in workflow editing.
- **Workflow profile defaults** - adds a default workflow Agent Profile path so
  new workflows start from explicit profile policy rather than implicit legacy
  skill/tool snapshots.

### Developer Studio, UI & Desktop Polish

- **Developer worktrees** - adds durable per-thread, child-agent, and workflow
  worktree allocation with owner records, branch naming, base branch/commit
  tracking, cleanup state, metadata, and failure preservation.
- **Worktree seeding** - can seed a Developer worktree from current staged,
  unstaged, and untracked changes or from the last commit, while requiring a
  real Git repository root and safe path handling.
- **Developer workspace UI refresh** - reorganizes Developer Studio around
  workspace identity, thread selection, profile/run controls, branch controls,
  inspector panels, responsive layout, and safer overflow behavior.
- **Recent workspace visibility** - fixes Developer Studio so all recent
  workspaces can be shown instead of being hidden by an overly narrow recent
  list.
- **Sidebar pinning and thread actions** - adds thread pinning, places the pin
  toggle before other row actions, and updates thread action helpers so pinned
  chat and Developer threads sort predictably.
- **Natural progress updates** - improves long-running interactive status text
  so agent runs can report progress in a more human, less repetitive way while
  preserving durable run state.
- **Monochrome sidebar cleanup** - refreshes sidebar iconography, status
  badges, action placement, and monitor styling for a cleaner desktop shell.
- **Streaming/render reliability** - hardens streaming reattach, grouped tool
  trace rendering, final content persistence, generated media handling, and
  disconnected-client timer behavior across chat, Developer, workflows, and
  channels.
- **Native macOS tray host** - adds a native Objective-C macOS tray helper and
  launcher integration so packaged macOS tray content stays visible and uses
  platform-native status item behavior.
- **Startup speed and legacy cleanup** - removes the old automatic
  Thoth-to-Row-Bot rebrand migration and post-migration notice from the hot
  startup path, keeping current startup focused on Row-Bot data only.

### Docs, CI, Packaging & Supply Chain

- **Public docs completion pass** - completes the user-guide pass across
  Docusaurus docs, generated reference pages, settings/home/chat/integration
  pages, real UI screenshot metadata, Pagefind integration, and docs validation.
- **Plugin documentation** - adds the Plugin System v2 technical reference,
  authoring workflow, validation commands, marketplace fixture layout, and
  plugin test guidance.
- **Prompt cache documentation** - adds the prompt context/cache contract with
  stable/ephemeral section inventory, provider gating rules, and verification
  expectations.
- **Installer verification plan** - adds the installer and CI verification plan
  covering local pre-push checks, PR gates, manual Installer Verify workflow,
  release artifact builds, and deferred clean-machine smoke coverage.
- **Test matrix source of truth** - adds `scripts/run_test_matrix.py` with fast,
  contracts, subsystem, coverage, deterministic, installer, app-smoke, PR, and
  release lanes aligned with repository ownership rules.
- **Dependency discipline** - moves dependency truth to `pyproject.toml` and
  `uv.lock`, adds locked `requirements.txt` export/check tooling, uses uv for
  Dependabot updates, and verifies runtime extras before release builds.
- **Supply-chain CI** - adds UV lockfile, OSV scanner, lint advisory/blocking,
  live e2e, and installer verification workflows, with vulnerable-package
  baselines tracked in `osv-scanner.toml`.
- **Release workflow hardening** - updates release CI to run the release matrix,
  verify version consistency, build/smoke Windows, Linux, and macOS artifacts,
  upload SHA256 manifests, and keep Windows signing local-only.
- **Installer payload coverage** - updates Windows, macOS, and Linux builders,
  installer docs, dependency setup, source payload comments, macOS tray
  bundling, Linux smoke paths, and packaged runtime dependency checks.
- **Agent instructions** - adds canonical `AGENTS.md`, a Claude companion file,
  updated contribution guidance, and clearer PR-template release/test prompts.

### Tests & Release Validation

- **Plugin coverage** - adds contract and subsystem tests for manifest v2,
  plugin API, registry/loader, state, installer, marketplace, UI contracts,
  devtools, webhooks, bot auth, plugin channels, plugin MCP tools, and plugin
  skill scoping.
- **Channel coverage** - adds channel contracts, registry tests, approval
  tests, plugin-channel runtime tests, channel model routing tests, and
  streaming/tool-output coverage.
- **Provider coverage** - adds Requesty tests plus broader provider catalog,
  runtime, selection, prompt-cache metric, prompt-cache payload, API-key dialog,
  readiness, and optional dependency coverage.
- **Workflow coverage** - adds profile-first workflow runtime, migration,
  approvals, graph, delegate-agent step, and profile override coverage.
- **Developer coverage** - adds Developer import gate, runtime command/process,
  sandbox, inspector snapshot, worktree manager, workspace thread, and UI
  contract coverage.
- **Installer/release coverage** - adds installer metadata, CLI smoke,
  release workflow contract, coverage summary, test matrix runner, dependency
  metadata, optional import, Linux support, and app port/startup coverage.
- **Migrated subsystem coverage** - adds contracts, fixtures, helper packages,
  source-test maps, legacy inventory, coverage inventory, deterministic
  subsystem lanes, and many focused tests while keeping retired monolith shim
  files out of new substantive coverage.
- **Regression coverage** - adds focused tests for memory graph regressions,
  condition operators, workflow audit fixes, thread pinning, tool config
  isolation, UI home contracts, sidebar grouping, and startup hardening.

### Breaking Changes And Caveats

- Plugin System v2 intentionally supports only native tools, plugin-packaged
  MCP servers, bundled skills, and channels. Plugins cannot add arbitrary app
  panels, custom NiceGUI, JavaScript, provider runtimes, memory providers,
  workflow triggers, general hooks, or custom settings tabs.
- Plugin manifests must use `schema_version: 2` and `provides.native_tools`;
  old Thoth plugin manifests or plugin code that imports Row-Bot internals are
  rejected or quarantined instead of loaded.
- Plugins install disabled by default. Users must review permissions, configure
  required settings/secrets, run health checks where relevant, and enable the
  plugin before it contributes tools, skills, MCP servers, or channels.
- Plugin channel adapters route through Row-Bot core for execution, approvals,
  media, Goal Mode, pairing, and webhook lifecycle. URL-only attachments are
  not fetched by core through the public plugin API.
- Requesty uses provider id `requesty` and provider-qualified refs such as
  `model:requesty:provider/model`. Its catalog is filtered so non-chat media,
  audio, embedding, moderation, and realtime rows do not appear as Brain/agent
  choices.
- Direct Anthropic is the only provider receiving Anthropic prompt-cache
  markers in this rollout. Anthropic-compatible providers such as MiniMax,
  OpenCode Anthropic Messages, and Claude Subscription do not receive
  `cache_control` markers until explicitly proven compatible.
- Current Row-Bot startup no longer runs the old automatic Thoth-to-Row-Bot
  rebrand migration. Users still on Thoth or very early Row-Bot builds should
  first install and launch a previous migration-capable Row-Bot release, then
  upgrade to 4.3.0.
- Windows signing remains local-only, and macOS notarization plus final
  clean-machine Windows/macOS/Linux smoke tests are still manual release gates
  after release artifacts are built.

### Files Changed

| File | Change |
|------|--------|
| `src/row_bot/plugins/manifest.py`, `api.py`, `loader.py`, `registry.py`, `state.py` | Implements Plugin System v2 manifest validation, public plugin API, loader sandboxing, runtime registration, enablement state, and reload behavior. |
| `src/row_bot/plugins/marketplace.py`, `installer.py`, `ui_marketplace.py`, `ui_plugin_dialog.py`, `ui_settings.py` | Adds marketplace caching/install/update flows and expands Plugin Center UI for settings, secrets, auth, health, logs, tools, skills, channels, and enablement. |
| `src/row_bot/plugins/channel_runtime.py`, `webhooks.py`, `bot_framework_auth.py`, `mcp.py`, `devtools.py` | Adds public plugin channel execution, namespaced plugin webhooks, Bot Framework auth validation, plugin MCP integration, and local plugin developer commands. |
| `src/row_bot/plugins/templates/*`, `examples/plugins/*`, `scripts/validate_plugin.py`, `scripts/build_plugin_index.py` | Adds plugin templates, example plugins, standalone plugin validation, and local marketplace index generation. |
| `src/row_bot/channels/*`, `src/row_bot/slash_commands.py`, `src/row_bot/tools/__init__.py` | Integrates plugin-owned channels, shared channel output assembly, generated channel tools, thread-scoped model commands, and channel runtime/tool registration updates. |
| `src/row_bot/providers/requesty.py`, `providers/catalog.py`, `providers/selection.py`, `providers/runtime.py`, `providers/readiness.py` | Adds Requesty provider support and hardens provider catalog normalization, model selection, runtime routing, and readiness behavior. |
| `src/row_bot/prompt_context.py`, `prompt_cache.py`, `agent.py`, `agent_context.py`, `models.py` | Adds explicit prompt section stability, direct-Anthropic cache marker gating, prompt-cache metrics, and model/provider context handling. |
| `src/row_bot/tasks.py`, `src/row_bot/ui/task_dialog.py`, `tests/subsystem/workflows/*` | Moves workflows toward profile-first agent execution with profile migration, defaults, review notes, and focused workflow coverage. |
| `src/row_bot/developer/worktrees.py`, `developer/ui.py`, `developer/runtime.py`, `developer/storage.py`, `developer/git.py` | Adds durable Developer worktrees, worktree seeding, recent workspace fixes, and refreshed Developer Studio UI/runtime behavior. |
| `src/row_bot/ui/sidebar.py`, `thread_actions.py`, `iconography.py`, `streaming.py`, `render.py`, `chat.py`, `command_center.py` | Adds thread pinning and UI polish while hardening streaming, transcript rendering, activity surfaces, and generated media handling. |
| `src/row_bot/launcher.py`, `installer/macos/RowBotTrayHost.m`, `installer/build_mac_app.sh`, `installer/build_mac_release.sh` | Adds native macOS tray hosting and launcher/build integration for visible packaged tray behavior. |
| `src/row_bot/migration/row_bot_legacy_rebrand.py`, `src/row_bot/ui/post_migration.py`, `tests/test_row_bot_legacy_rebrand.py`, `tests/test_post_migration_notice.py` | Removes the old automatic Thoth rebrand migration and post-migration notice from current startup. |
| `.github/workflows/*`, `scripts/run_test_matrix.py`, `scripts/export_locked_requirements.py`, `scripts/verify_runtime_dependencies.py`, `osv-scanner.toml` | Adds release/test matrix automation, lockfile/export checks, runtime dependency verification, OSV scanning, live e2e, installer verify, and release workflow hardening. |
| `pyproject.toml`, `uv.lock`, `requirements.txt`, `.github/dependabot.yml` | Moves dependency ownership to pyproject/uv lockfiles and keeps generated installer requirements in sync. |
| `installer/row_bot_setup.iss`, `installer/README.md`, `installer/build_installer.ps1`, `installer/build_linux_app.sh`, `installer/install_deps.bat` | Updates versioned installer metadata, payload validation, installer docs, Linux smoke paths, and Windows dependency setup. |
| `docs/PLUGIN_SYSTEM_V2.md`, `docs/PROMPT_CONTEXT_AND_CACHE.md`, `docs/INSTALLER_CI_VERIFICATION_PLAN.md`, `docs/RELEASING.md`, `docs/SOURCE_LAYOUT.md` | Adds plugin, prompt-cache, installer/CI verification, release, and source-layout documentation. |
| `docs-site/*`, `docs-content/*`, `scripts/docs/*` | Completes the public docs/user-guide pass, generated reference content, metadata, screenshots, search, and validation tooling. |
| `AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md`, `.github/PULL_REQUEST_TEMPLATE.md` | Adds and updates contributor/agent guidance, release-sensitive testing prompts, and project instructions. |
| `tests/contracts/*`, `tests/subsystem/*`, `tests/fixtures/*`, `tests/helpers/*`, `tests/integration/wiki_vault/*` | Adds deterministic contract, subsystem, fixture, inventory, source-test-map, wiki-vault, installer, provider, plugin, channel, workflow, Developer, MCP, memory, and regression coverage. |
| `tests/test_suite.py`, `tests/integration_tests.py`, `tests/test_memory_e2e.py` | Retires legacy monolith tests into compatibility shims while moved coverage lives in focused deterministic lanes. |

---

## v4.2.0 - Agent Profiles, Goal Mode, xAI Grok & Public Docs

This release builds on v4.1.0 with a larger orchestration and provider pass. It
introduces durable Agent Profiles, Goal Mode, child-agent delegation, first-class
xAI Grok OAuth support, Grok Imagine media generation, a new public docs site,
real UI documentation capture, and several provider/settings hardening fixes
that make model selection, OAuth status, and headless secret handling more
predictable.

### Agent Profiles, Goal Mode & Delegation

- **Agent Profiles runtime** - adds durable Agent Profiles with profile
  instructions, handoff contracts, usage guidance, tool policy, skill policy,
  context policy, workspace policy, approval policy, and enabled/disabled
  state.
- **Thread profile injection** - carries the active profile into normal agent
  runs and chat-only turns, including a structured profile prompt, policy
  summary, and warning path when a selected profile is missing or disabled.
- **Built-in profile library** - adds and refreshes built-in profiles for
  focused roles, including profile metadata, when-to-use guidance, tool
  constraints, and runtime snapshots used by child agents.
- **Profile commands and UI** - adds profile command handling, a Profile
  Library, profile picker, profile summaries, and profile-selection surfaces so
  users can choose or inspect an agent role without editing config files.
- **Goal Mode v1** - introduces durable per-thread goals with objective,
  status, progress, evidence, blockers, next step, turn count, active run id,
  and formatted status output.
- **Goal tool integration** - adds `goal_update` and `goal_status` tools so
  agents can report real progress, blockers, evidence, and completion state
  through the same durable Goal Mode record shown in the UI.
- **Child Agent runs** - adds durable child-agent runs with queued/running/
  terminal states, parent thread linkage, profile snapshots, context summaries,
  status messages, event logs, stop requests, and wait handling.
- **Agent delegation tools** - adds `delegate_work`, `agent_status`,
  `agent_wait`, `agent_stop`, `agent_profiles`, `agent_profile_save`,
  `agent_message`, and `agent_promote` for controlled multi-agent workflows.
- **Agent promotion paths** - lets completed child runs be promoted into a new
  Agent Profile or into a disabled manual workflow that can be reviewed before
  reuse.
- **Tool allowlists** - carries profile/tool allowlists into agent graph
  construction, plugin tools, and MCP tool injection so delegated work can be
  narrower than the parent thread's full tool surface.
- **Write-lock and queue safeguards** - adds active-run queue handling,
  single-writer protections, and approval coverage so child agents do not
  silently collide with parent work.
- **Activity and Goal UI** - adds Goal UI, Agent drawer, channel monitor
  surfaces, Command Center grouping, live profile/run status, and streaming
  updates for long-running delegated work.
- **Channel goal runtime** - extends channel command/runtime paths across
  Discord, Slack, SMS, Telegram, and WhatsApp so channel-driven work can carry
  goal and agent context consistently.

### xAI Grok OAuth Provider

- **xAI Grok first-class provider** - adds `xai_oauth` as a first-class,
  provider-qualified runtime for xAI Grok subscription access, separate from
  the existing xAI API-key provider.
- **OAuth PKCE support** - adds OAuth flow helpers, token storage, refresh
  handling, client-id status reporting, account/user/email hash metadata, and
  token health checks for the xAI Grok runtime.
- **xAI Responses transport** - adds a dedicated xAI OAuth Responses transport
  for chat/model runtime creation, prompt conversion, streaming behavior, and
  response normalization.
- **Model catalog integration** - wires xAI Grok into cloud model refresh,
  provider-qualified model refs, context-window lookup, model availability
  checks, cache entries, and status-only catalog reads.
- **Vision capability probing** - adds xAI OAuth vision probe support so
  vision-capable Grok models can be confirmed and reported instead of relying
  only on static assumptions.
- **Provider readiness and status** - adds runtime availability checks,
  provider status details, token-health timing, last runtime probe, last vision
  probe, model-count status, and OAuth client-id diagnostics.
- **Model picker integration** - adds xAI Grok provider labels, picker icons,
  inactive reasons, canonical provider refs, surface filtering, and short-lived
  provider-status caching for faster settings and picker rendering.
- **Setup and settings integration** - adds xAI Grok provider cards, connect
  state, setup-wizard copy, model settings rows, default handling, and
  provider-specific hardening in Settings.
- **Default/model-settings hardening** - fixes edge cases where xAI OAuth
  defaults, inactive provider state, or unsupported surfaces could leave model
  settings in a misleading state.

### Grok Imagine Media Generation

- **xAI OAuth media runtime** - adds Grok Imagine image and video generation
  through the OAuth-backed xAI Grok provider, including availability checks that
  use OAuth runtime status rather than API-key presence.
- **Curated Grok Imagine models** - adds `grok-imagine-image`,
  `grok-imagine-image-quality`, and `grok-imagine-video` options for xAI and
  xAI Grok media surfaces.
- **xAI media provider module** - adds a dedicated xAI media implementation for
  image, video, and image-to-video request construction and response handling.
- **Media picker filtering** - updates image and video model option builders so
  provider-qualified cache rows and OAuth-backed models appear only on
  compatible media surfaces.
- **Image/video tool integration** - updates image generation, video
  generation, and Row-Bot Status media reporting so xAI Grok media models are
  discoverable and diagnosable.
- **xAI API-key catalog improvements** - merges `/models` and
  `/language-models`, hides unusable rows, preserves curated chat extras, and
  exposes Grok Imagine media rows without leaking them into chat surfaces.
- **Image-to-video request fix** - fixes the xAI image-to-video request body so
  media generation requests match the provider's expected payload shape.

### Provider Secrets, Settings & Model Picker Reliability

- **Session-only secret fallback** - hardens provider secret handling when the
  OS keyring is unavailable, especially on WSL/headless Linux, by allowing
  newly entered secrets to work for the current process while keeping local
  metadata files secret-free.
- **Headless Linux guidance** - updates README and architecture docs to explain
  that persistent provider secrets need Secret Service, KWallet, or another
  secure Python keyring backend.
- **Custom endpoint auth errors** - improves custom OpenAI-compatible endpoint
  behavior when auth is required but no secret is available, returning a
  provider-specific message instead of silently falling through.
- **Provider settings feedback** - updates provider settings and setup wizard
  copy so unavailable keyring state, missing secrets, OAuth state, and provider
  readiness are surfaced more clearly.
- **Read-only picker path** - changes Quick Choice listing to avoid mutating
  provider config during ordinary picker reads, reducing settings churn and
  surprising writes.
- **Surface-aware inactive reasons** - annotates Quick Choices with clearer
  inactive reasons when a model is configured but unsupported on the requested
  chat, agent, vision, image, or video surface.
- **Provider status cache** - adds a short-lived picker cache for provider
  status checks so settings/model surfaces do not repeatedly refresh token
  health during one render pass.

### Public Docs, Website & Automation

- **4.1.0 landing page refresh** - updates the public landing page for the
  published v4.1.0 artifacts, refreshed provider positioning, social preview
  image, Linux install command, accessibility for video facades, and updated
  product demos.
- **Public docs site scaffold** - adds a Docusaurus-based docs site under
  `docs-site` with docs navigation, generated reference pages, custom styling,
  static architecture/contact pages, brand assets, favicon, CNAME, and package
  lockfile.
- **Docs content metadata** - adds structured metadata under `docs-content` for
  settings tabs, home tabs, dialogs, docs routes, UI surfaces, real UI
  surfaces, screenshots, how-to guides, and review status.
- **Real UI screenshots** - adds real app screenshot assets for app shell,
  chat, setup, home tabs, and settings tabs so docs show actual Row-Bot
  surfaces instead of placeholder illustrations.
- **Docs generation pipeline** - adds scripts for inventory collection, MDX
  generation, real UI docs generation, screenshot capture, demo data seeding,
  review report creation, `llms.txt` generation, schema helpers, and public
  docs validation.
- **Docs capture hooks** - adds app-side docs capture support so documentation
  scripts can drive and snapshot stable UI states.
- **Search and navigation** - adds Pagefind search integration, search page,
  generated reference index pages, settings docs, home docs, chat docs,
  integration docs, troubleshooting docs, and skill/plugin/MCP docs.
- **Docs CI workflow** - adds a GitHub Actions docs workflow and automated
  validation tests so generated docs, metadata, and screenshot references can
  be checked before publishing.

### UI, Status & Runtime Polish

- **Agent-aware Row-Bot Status** - expands Row-Bot Status output to include
  agent/profile/run and media-provider reporting used by diagnostics and
  delegated work.
- **Streaming and render updates** - updates streaming, render, transcript,
  tool-trace, and chat component paths so profile context, child-agent
  activity, tool traces, and long-running status updates remain visible.
- **Command Center polish** - reorganizes activity and goal status grouping so
  active goals, child agents, and related runtime events are easier to scan.
- **Home and setup capture stability** - adjusts Home, onboarding center, setup
  wizard, and settings surfaces used by both users and the real UI docs capture
  pipeline.
- **Tunnel and channel coverage** - updates tunnel behavior and channel runtime
  tests to cover the new agent/goal execution paths without weakening existing
  channel safety behavior.
- **Installer source-layout note** - refreshes the Windows installer source
  coverage comment to include the stability module in the packaged recursive
  source include.

### Tests & Release Validation

- **Agent and Goal coverage** - adds focused tests for active-run queues,
  approvals, agent commands, agent context, profiles, runtime profiles,
  runners, durable runs, tool allowlists, write locks, UI contracts, Goal Mode,
  channel goal runtime, and Row-Bot Status agent reporting.
- **xAI OAuth coverage** - adds provider and transport tests for OAuth token
  state, refresh behavior, provider status, model catalog integration,
  runtime availability, vision probing, model defaults, and setup/settings
  contracts.
- **xAI media coverage** - adds tests for xAI media model discovery, Grok
  Imagine image/video options, media tool routing, Row-Bot Status media output,
  and image-to-video payload behavior.
- **Provider secret coverage** - adds tests for auth-store fallback behavior,
  custom endpoint auth requirements, provider selection, provider catalog rows,
  and headless/no-keyring semantics.
- **Model picker and settings coverage** - expands model picker regression and
  settings-overhaul contract tests around provider-qualified refs, inactive
  choices, unsupported surfaces, and xAI OAuth defaults.
- **Public docs automation coverage** - adds docs automation tests for metadata
  inventory, generated pages, validation, screenshot references, and real UI
  docs generation inputs.
- **UI/runtime coverage** - expands tests for app-port stability, app
  hardening, chat tool trace UI, home performance, onboarding, skill
  activation, thinking retention, tunnel management, and status media paths.

### Breaking Changes And Caveats

- xAI Grok OAuth support is separate from the existing xAI API-key provider.
  API keys still use the `xai` provider path; subscription/OAuth-backed Grok
  runtime uses `xai_oauth` provider-qualified model refs.
- xAI Grok runtime and Grok Imagine media require a successful OAuth connection
  and upstream account/model access. If token health fails, related model rows
  may appear inactive until reconnect or refresh succeeds.
- Grok Imagine media models are intentionally scoped to image and video
  surfaces and should not appear as chat, agent, or vision-only models.
- Agent Profiles and Goal Mode create durable local run/goal records. Profile
  changes, promotions, and other destructive agent-management actions remain
  approval-gated.
- On systems without a secure keyring, newly entered provider secrets are
  session-only and must be re-entered after restart unless a secure keyring
  backend is configured.
- The landing-page changes in this commit range prepared the published v4.1.0
  site. Final v4.2.0 artifact links and the website landing-page refresh remain
  release-gate tasks after the release assets are live.

### Files Changed

| File | Change |
|------|--------|
| `src/row_bot/agent.py` | Carries Agent Profile context, tool allowlists, and profile snapshots through agent, chat-only, streaming, and resume paths. |
| `src/row_bot/agent_profiles.py`, `agent_context.py`, `agent_tool_catalog.py` | Adds Agent Profile storage, context assembly, built-in profiles, and tool catalog behavior. |
| `src/row_bot/agent_runner.py`, `agent_runs.py` | Adds durable child-agent spawning, run state, events, wait/stop handling, and parent linkage. |
| `src/row_bot/goals.py` | Adds durable Goal Mode state, progress formatting, evidence/blocker tracking, and current-goal helpers. |
| `src/row_bot/tools/agent_tool.py`, `goal_tool.py` | Adds delegation, run inspection, profile management, promotion, goal update, and goal status tools. |
| `src/row_bot/ui/agent_drawer.py`, `goal_ui.py`, `profile_library.py`, `profile_picker.py` | Adds Agent/Goal/Profile UI surfaces. |
| `src/row_bot/ui/command_center.py`, `streaming.py`, `render.py`, `chat.py`, `sidebar.py`, `tool_trace.py` | Updates activity, streaming, rendering, chat, sidebar, and tool-trace behavior for goals and agents. |
| `src/row_bot/channels/*`, `slash_commands.py`, `tasks.py`, `threads.py` | Updates channel runtime, slash commands, workflows, and thread metadata for goals and profiles. |
| `src/row_bot/providers/xai_oauth.py` | Adds xAI Grok OAuth provider runtime, tokens, catalog, probes, health checks, and status helpers. |
| `src/row_bot/providers/transports/xai_oauth_responses.py` | Adds xAI OAuth Responses chat transport. |
| `src/row_bot/providers/xai_catalog.py`, `xai_media.py` | Adds xAI catalog merging/curation and Grok Imagine media generation support. |
| `src/row_bot/providers/catalog.py`, `model_catalog.py`, `models.py`, `runtime.py`, `selection.py`, `media.py`, `status.py`, `readiness.py` | Wires xAI Grok OAuth, Grok Imagine media, provider-qualified refs, capability classification, readiness, status, and picker behavior into shared provider paths. |
| `src/row_bot/providers/auth_store.py`, `custom.py` | Hardens secret storage fallback and custom endpoint auth-required behavior. |
| `src/row_bot/ui/provider_settings.py`, `settings.py`, `setup_wizard.py`, `chat_components.py` | Updates provider setup/settings/model picker surfaces for xAI Grok OAuth, keyring fallback, and model-setting hardening. |
| `src/row_bot/tools/image_gen_tool.py`, `video_gen_tool.py`, `row_bot_status_tool.py` | Adds xAI Grok media routing and richer status diagnostics. |
| `src/row_bot/docs_capture.py` | Adds app-side support for real UI documentation capture. |
| `docs-site/*`, `docs-content/*`, `scripts/docs/*` | Adds the public docs site, generated docs metadata, real UI screenshot automation, validation, search, and review tooling. |
| `docs/index.html`, `docs/row_bot_preview.png` | Refreshes the public landing page and social preview image for the v4.1.0 site update. |
| `README.md`, `docs/ARCHITECTURE.md`, `installer/row_bot_setup.iss` | Updates keyring/headless guidance, architecture wording, and installer source-layout notes. |
| `tool_guides/agents_guide/SKILL.md`, `goal_guide/SKILL.md`, `row_bot_status_guide/SKILL.md` | Adds and updates tool guidance for agents, goals, and status diagnostics. |
| `tests/*`, `tests/docs/*` | Adds and expands agent, goal, xAI OAuth, xAI media, provider secret, model picker, settings, docs automation, channel runtime, status, and UI regression coverage. |

---

## v4.1.0 - Providers, Controlled Self-Evolution, Skills & Diagnostics

This release builds on v4.0.1 with a broad provider and runtime reliability
pass. It adds first-class Atlas Cloud support, introduces a Claude Subscription
provider path, lands controlled self-evolution, improves skill activation and
pinning, hardens custom tool creation, and fixes several model-picker,
streaming, voice, vision, setup, and diagnostics regressions that surfaced
after the 4.0.0 rebrand.

### Provider Runtime & Model Catalog

- **Atlas Cloud first-class provider** - adds Atlas Cloud as a native provider
  instead of treating it as a generic custom endpoint, with provider identity,
  setup copy, authentication wiring, runtime routing, and model references that
  behave like the existing first-class providers.
- **Atlas Cloud model catalog fetching** - adds live Atlas model discovery,
  cache integration, provider-qualified model refs, catalog refresh handling,
  and status/readiness checks so Atlas models appear through the same catalog
  path as other providers.
- **Atlas Cloud agent capability classification** - maps Atlas-hosted models
  into chat and agent-ready surfaces using the provider's API metadata plus
  curated fallbacks for known frontier/provider families, including OpenAI,
  Anthropic, Gemini, Qwen, Kimi, GLM, MiniMax, DeepSeek, and similar
  tool-capable chat models.
- **Atlas Cloud vision capability support** - classifies Atlas-hosted
  multimodal chat models as vision-capable where supported, including hosted
  OpenAI, Gemini, Anthropic, Qwen-VL, Kimi-VL, GLM vision, and related model
  families.
- **Atlas Cloud media-model filtering** - keeps Atlas image-generation and
  video-generation models out of chat, agent, and vision picker surfaces for
  this phase, preventing non-chat media models from leaking into incompatible
  workflows.
- **Atlas Cloud streaming fixes** - scopes the OpenAI-compatible buffered
  tool-call path so Atlas can stream assistant text after tool calls without
  disturbing existing OpenAI-compatible providers.
- **Atlas Claude transport handling** - adds Atlas-specific Claude behavior for
  streaming, tool-call replay, and native tool-history cleanup so
  Anthropic-hosted models behind Atlas can complete agent turns reliably.
- **OpenAI-compatible transport regression coverage** - expands tests around
  streaming, buffered tool output, Claude-shaped tool calls, and
  provider-specific transport behavior to protect OpenRouter and other existing
  compatible endpoints.
- **Provider capability resolution** - strengthens the shared
  capability-resolution path used by catalogs, readiness checks, vision
  routing, and agent eligibility so provider metadata, curated known-good
  families, and cached model data agree more consistently.
- **Provider status and readiness improvements** - updates provider readiness,
  runtime selection, status reporting, catalog cache behavior, and auth-store
  integration to support the new providers without changing the behavior of
  existing ones.

### Claude Subscription Provider

- **Claude Subscription provider support** - adds a first-class provider path
  for Claude Subscription usage, with provider registration, auth-state
  detection, model references, runtime selection, and setup/status surfaces.
- **Claude Subscription messages transport** - adds a dedicated transport for
  Claude Subscription message exchange, including prompt conversion, tool-call
  handling, and response normalization.
- **Claude Subscription auth and diagnostics** - adds provider
  subscription-auth helpers, external credential handling, readiness checks,
  and tests so the app can report whether the subscription runtime is actually
  available.
- **Provider selection integration** - wires Claude Subscription into
  provider/model selection without taking over Anthropic API-key behavior or
  other Claude-compatible provider paths.

### Controlled Self-Evolution

- **Controlled self-evolution engine** - introduces the first controlled
  self-evolution runtime, with structured change proposals, reviewable
  execution boundaries, persistence, and test coverage.
- **Self-reflection skill updates** - adds bundled self-reflection guidance so
  Row-Bot can reason about improvement opportunities through a constrained
  skill flow instead of ad-hoc code changes.
- **Dream-cycle integration** - connects controlled improvement work into the
  existing dream-cycle and memory-policy systems so reflection output can be
  captured and revisited safely.
- **Prompt and agent integration** - updates agent and prompt wiring so
  self-improvement behavior is explicit, bounded, and aligned with the rest of
  the assistant runtime.
- **Command Center visibility** - adds UI/status hooks for evolution state and
  related activity so controlled self-evolution is observable instead of hidden
  background behavior.

### Skills, Developer Tools & Custom Tool Builder

- **Skill pinning defaults** - adds default pinning behavior and activation
  tests so important skills can remain discoverable and stable across sessions.
- **Skill activation reliability** - improves the skill activation path and
  channel command handling, with coverage for pinned skills, command routing,
  and activation edge cases.
- **Custom tool builder hardening** - strengthens Git and virtualenv handling
  in the custom tool builder so new tool projects are created more reliably
  across local environments.
- **Developer Studio storage and capsules** - improves developer storage, tool
  capsule handling, and Developer Studio UI behavior used by the custom tool
  flow.
- **Tool-builder guidance updates** - refreshes the custom tool builder guide
  to reflect the safer Git/venv workflow and the current implementation.

### Chat, Voice & Model Picker Reliability

- **Anthropic thinking-block normalization** - fixes normalization of
  Anthropic thinking blocks so reasoning content does not corrupt downstream
  transcript handling.
- **Local voice talk submission fix** - repairs local voice talk submission so
  voice input can be sent through the normal chat path again.
- **Ollama vision cache handling** - fixes Ollama vision model detection to
  respect cached capability data instead of losing vision support after catalog
  refreshes.
- **Migration wizard repair** - fixes migration wizard UI and Ollama status
  behavior so setup and upgrade flows do not report misleading provider state.
- **Model-picker regression coverage** - adds tests around chat-only, vision,
  provider readiness, and model-picker behavior to prevent capability labels
  from drifting again.
- **Streaming batcher coverage** - expands streaming tests around batched output
  so incremental rendering remains responsive after provider and tool-call
  changes.
- **Chat keybinding coverage** - adds chat keybinding tests to protect composer
  behavior while provider and streaming internals continue to evolve.

### Insights, Status & Diagnostics

- **Insights status tray diagnostics** - fixes the insights status tray
  diagnostic path so provider and runtime issues are surfaced with more useful
  state.
- **macOS tray reliability** - restores the packaged macOS launcher to the
  pystray green/grey status dots after the native menu-bar icon path proved
  unreliable on test installs.
- **Row-Bot status tool updates** - refreshes the Row-Bot status tool and
  guide, including provider/media reporting paths used during diagnostics.
- **Provider settings and status UI** - updates provider settings, status
  checks, status bar, setup wizard, and related UI state for the new
  provider/runtime readiness model.
- **Home and performance stability** - improves home-screen performance
  behavior and adds UI performance coverage for the post-rebrand shell.
- **Application stability tests** - adds broader app-stability hardening tests
  around setup, settings, provider state, streaming, and catalog interactions.

### Documentation, Website & Architecture

- **Architecture docs refresh** - updates the architecture documentation and
  diagrams to match the Row-Bot rebrand and current runtime/provider structure.
- **Website download links** - updates the docs site download links for the
  v4.0.1 package line.
- **Runtime and provider documentation alignment** - updates README and docs
  surfaces touched by provider setup, installer guidance, and architecture
  diagrams.

### Tests & Release Validation

- **Atlas Cloud coverage** - adds first-class Atlas tests for model catalog
  fetching, capability classification, auth/setup behavior, OpenAI-compatible
  transport behavior, streaming, tool calls, and vision refs.
- **Claude Subscription coverage** - adds Claude Subscription auth, transport,
  provider runtime, and subscription-readiness tests.
- **Controlled self-evolution coverage** - adds controlled self-evolution tests
  around proposal handling, persistence, guardrails, and integration points.
- **Skill and command coverage** - adds tests for skill pinning, skill
  activation, channel skill commands, and custom tool builder flows.
- **Provider runtime coverage** - expands provider catalog, runtime, selection,
  readiness, auth-store, API-key storage, and subscription-auth tests.
- **UI and workflow coverage** - expands tests for migration wizard behavior,
  insights provider status, status media, chat keybindings, streaming batching,
  home performance, and settings/provider contracts.

### Breaking Changes And Caveats

- Atlas Cloud requires an Atlas Cloud API key and a successful catalog refresh
  before its live model list can be used.
- Atlas Cloud image-generation and video-generation models are intentionally
  hidden from chat, agent, and vision surfaces in this phase.
- Atlas capability labels depend on provider metadata plus curated known-good
  model families; newly released Atlas models may need a catalog refresh or
  future classification update before they appear with the most specific
  capability label.
- Claude Subscription support depends on the local subscription auth/runtime
  path being available and should not be confused with the Anthropic API-key
  provider.
- Controlled self-evolution is deliberately constrained to reviewable, bounded
  flows; it is not an unrestricted autonomous code modification mode.
- Custom tool builder reliability still depends on a working local Git and
  Python virtualenv environment.

### Files Changed

| File | Change |
|------|--------|
| `src/row_bot/providers/atlascloud.py` | Adds Atlas Cloud provider definition, setup metadata, catalog fetching, model filtering, and capability classification. |
| `src/row_bot/providers/claude_subscription.py` | Adds Claude Subscription as a first-class provider. |
| `src/row_bot/providers/transports/openai_compatible.py` | Updates OpenAI-compatible streaming/tool-call handling, including Atlas-scoped buffering behavior. |
| `src/row_bot/providers/transports/claude_subscription_messages.py` | Adds Claude Subscription message transport. |
| `src/row_bot/providers/capability_resolution.py` | Adds shared provider capability resolution for chat, agent, and vision readiness. |
| `src/row_bot/providers/catalog.py`, `model_catalog.py`, `model_catalog_cache.py` | Updates provider catalog discovery, caching, and model metadata handling. |
| `src/row_bot/providers/runtime.py`, `readiness.py`, `selection.py`, `status.py` | Updates runtime selection, provider readiness, selection, and status reporting for new provider behavior. |
| `src/row_bot/api_keys.py`, `external_credentials.py`, `providers/auth_store.py` | Updates provider authentication and credential-state handling. |
| `src/row_bot/models.py`, `vision.py` | Updates model refs and vision capability handling. |
| `src/row_bot/evolution.py` | Adds controlled self-evolution engine. |
| `src/row_bot/dream_cycle.py`, `memory_policy.py`, `prompts.py`, `agent.py` | Integrates controlled self-evolution, reflection, and provider/runtime behavior into agent flows. |
| `bundled_skills/self_reflection/SKILL.md` | Adds self-reflection guidance for controlled improvement work. |
| `src/row_bot/skills.py`, `skills_activation.py` | Adds skill pinning defaults and activation improvements. |
| `src/row_bot/developer/storage.py`, `developer/tool_capsules.py`, `developer/ui.py` | Hardens Developer Studio storage, tool capsules, and UI flows. |
| `src/row_bot/tools/custom_tool_builder_tool.py`, `tool_guides/custom_tool_builder_guide/SKILL.md` | Hardens custom tool builder Git/venv flow and updates guidance. |
| `src/row_bot/tools/row_bot_status_tool.py`, `tool_guides/row_bot_status_guide/SKILL.md` | Updates status diagnostics and guide behavior. |
| `src/row_bot/ui/*` | Updates provider settings, setup wizard, status surfaces, streaming, chat, home, sidebar, task dialog, and performance behavior. |
| `src/row_bot/channels/*` | Updates channel skill-command behavior across Discord, Slack, SMS, Telegram, and WhatsApp. |
| `docs/ARCHITECTURE.md`, `docs/index.html`, `README.md`, `installer/README.md` | Refreshes docs, download links, architecture diagrams, and installer guidance. |
| `tests/*` | Adds or expands Atlas Cloud, Claude Subscription, provider runtime, controlled self-evolution, skill pinning, custom tool builder, status, streaming, model picker, migration wizard, and UI performance coverage. |

---

## v4.0.1 - Ollama Model Picker Hotfix

This patch fixes a Settings -> Models regression in v4.0.0 where local Ollama
model selections could fail when Row-Bot saved a provider-qualified family ref
such as `model:ollama:llama3` but the Ollama daemon exposed the installed model
as a tagged runtime name such as `llama3:latest`.

- **Ollama model switching** - local Ollama family aliases now resolve to the
  installed daemon tag when there is one unambiguous match, while explicit tags
  and ambiguous families remain unchanged.
- **Provider runtime coverage** - the same alias resolution now applies through
  both the legacy model helpers and the provider runtime constructor used by
  chat-only/provider-backed local model paths.
- **Regression tests** - added focused coverage for unique and ambiguous Ollama
  family aliases, context lookup, provider-qualified picker values, and provider
  runtime construction.

Fixes #178. Thanks to @lihouwenbin for the original PR and investigation.

---

## v4.0.0 - Row-Bot Rebrand, Skills Hub, Voice, Providers & Installer Reliability

This is the public Row-Bot rebrand release. It moves the app from Thoth to
Row-Bot across product identity, repository metadata, installers, runtime paths,
release artifacts, documentation, launcher behavior, updater contracts, and
user-data locations. Existing 3.x data is preserved through a copy-first
migration path, so users can upgrade without losing rollback access to their old
Thoth data. Beyond the rebrand, v4.0.0 ships major upgrades to Smart Skills,
Skills Hub, realtime voice, provider discovery, approval modes, thread
organization, packaging, startup reliability, and release validation.

### Row-Bot Rebrand

- **Product identity** - app copy, bundled skills, tool guides, docs, release
  workflows, installer scripts, updater metadata, icons, and public repository
  links now use Row-Bot naming.
- **Repository rename support** - canonical public repository references now
  target `github.com/siddsachar/row-bot`.
- **Website contract** - public site configuration now supports `row-bot.ai`
  and the Row-Bot repository identity.
- **Brand assets** - adds Row-Bot glyphs, favicon, installer icon, docs imagery,
  runtime brand helpers, and brand-constant tests.
- **Release asset names** - v4 artifacts use `Row-Bot-X.Y.Z-Windows-x64.exe`,
  `Row-Bot-X.Y.Z-macOS-{arm64|x86_64}.dmg`, and
  `Row-Bot-X.Y.Z-Linux-ARCH.tar.gz`.
- **Linux command rename** - Linux installs expose `row-bot` as the user command
  and use the Row-Bot XDG data tree.
- **macOS app rename** - macOS packaging now builds `Row-Bot.app` instead of the
  legacy Thoth app bundle.
- **Windows launcher rename** - Windows launcher scripts, installer scripts, and
  shortcut entry points now use Row-Bot names.
- **Documentation refresh** - README, release docs, architecture docs, installer
  docs, issue templates, contributing docs, and release workflows now reflect
  the Row-Bot identity.

### Migration From Thoth 3.x

- **Copy-first migration** - Row-Bot reads legacy Thoth data, copies it into the
  new Row-Bot locations, and leaves the old data intact for rollback or manual
  recovery.
- **One-shot migration guard** - migration records completion state so normal
  launches do not repeatedly repair already-migrated data.
- **Legacy data coverage** - provider settings, channels, skills, MCP servers,
  plugins, Buddy assets, Designer workspaces, conversations, memories, tasks,
  media, updater state, and runtime config are covered by migration logic.
- **Plugin manifest repair** - legacy plugin manifests are repaired during
  migration so old minimum-version metadata does not block migrated plugins.
- **Post-migration notice** - adds UI and tests for showing users that migration
  completed and where their legacy data remains.
- **Compatibility fixtures** - migration tests cover copied data, guarded
  reruns, plugin metadata repair, runtime data paths, and Row-Bot brand/runtime
  assets.
- **Manual recovery path** - interrupted migrations can be retried after backing
  up both old and new data directories.

### Source Layout & Runtime Packaging

- **Package source layout** - runtime code now lives under `src/row_bot`, with
  compatibility cleanup for the old root-level layout.
- **Version module relocation** - version metadata now lives under the Row-Bot
  package and release scripts read it from the source-layout path.
- **Payload manifest** - adds app payload manifest generation and packaging
  compatibility checks.
- **Source layout docs** - adds `docs/SOURCE_LAYOUT.md` to document the package
  layout and compatibility expectations.
- **Smoke script updates** - app smoke and release helper scripts now understand
  the source-layout package.
- **Import compatibility coverage** - tests cover runtime imports, package
  metadata, installer payloads, and compatibility shims.

### Provider Runtime & Model Discovery

- **OpenCode providers** - adds first-class OpenCode provider support with
  runtime, selection, auth, catalog, and regression coverage.
- **MiniMax live discovery** - MiniMax models are discovered from the provider
  API instead of requiring hard-coded updates for every new model.
- **MiniMax capability mapping** - discovered MiniMax models are mapped into the
  model catalog with provider capabilities where available.
- **MiniMax stale cleanup** - stale MiniMax models can be removed automatically
  when they are no longer returned by the provider API.
- **Custom endpoint cleanup** - stale custom endpoint model references are
  cleaned up so old provider selections do not linger incorrectly.
- **Custom reasoning fixes** - custom OpenAI-compatible endpoints handle
  reasoning fields more reliably.
- **Custom vision fixes** - custom endpoint vision references and capability
  handling are repaired.
- **OpenAI-compatible transport coverage** - tests expand coverage around custom
  endpoint request shaping, provider refs, model catalog behavior, and
  live-provider discovery.
- **Provider settings updates** - provider UI and runtime settings now better
  reflect Row-Bot naming and newer provider catalog behavior.

### Chat, Attachments & Channels

- **Chat attachment bridge** - fixes the filesystem bridge used by chat
  attachments so local file references survive the intended handoff path.
- **Custom provider channel routing** - channel workflows now route
  custom-provider turns through the correct provider/runtime path.
- **Channel command support** - channel command handling moves into the Row-Bot
  package layout and gains focused coverage.
- **Workflow delivery defaults** - workflow/channel routing preserves approval
  and provider context across resumed turns.
- **Runtime status updates** - Row-Bot status tools and channel/runtime
  diagnostics now report Row-Bot naming and effective runtime state more
  consistently.

### Smart Skills & Skills Hub

- **Smart Skills activation** - adds skill activation logic for suggesting,
  enabling, disabling, and applying manual skills in chat context.
- **Slash command support** - adds slash-command infrastructure and tests for
  skill-aware chat commands.
- **Command palette skills** - command palette integration can surface skills and
  skill actions more directly.
- **Composer skill parity** - Designer and Developer chat composers gain access
  to shared skill and slash-command behavior.
- **Skills Hub marketplace** - adds a Skills Hub for browsing, detecting,
  importing, searching, and installing skills from supported sources.
- **Marketplace sources** - adds source adapters for GitHub, pasted Markdown, URL
  inputs, well-known skill indexes, and marketplace-style catalogs.
- **Import detection** - pasted or linked skill content can be detected and
  normalized before installation.
- **Search index** - Skills Hub includes local search/index helpers for browsing
  available skills.
- **Bundled skill updates** - bundled skills and tool guides are updated for
  Row-Bot naming and newer runtime behavior.
- **Skills tests** - adds broad tests for skills activation, Skills Hub sources,
  import detection, search, UI contracts, and slash commands.

### Realtime Voice

- **Realtime voice overhaul** - adds a new realtime voice runtime with provider
  interfaces, coordinator, client contracts, presenter state, and lifecycle
  helpers.
- **OpenAI realtime support** - adds OpenAI realtime provider/client pieces and
  tests.
- **Voice actions** - adds structured voice action handling so realtime voice can
  interact with Row-Bot behavior more safely.
- **Agent bridge** - realtime voice can bridge into agent/runtime behavior
  through a dedicated layer.
- **Cue policy** - adds conversational cue policy, speech policy, output
  coordination, and realtime event handling.
- **Local voice provider support** - adds local provider scaffolding for voice
  runtime selection.
- **Browser dispatch coverage** - tests cover realtime browser dispatch and
  voice event surfaces.
- **Voice UI lifecycle** - adds UI helpers for voice lifecycle and realtime event
  presentation.

### Approval Modes, Threads & Developer UX

- **Unified approval modes** - approval behavior is consolidated so chat,
  Developer, tools, and workflows can use clearer shared approval semantics.
- **Approval gate tooling** - adds approval-gate helpers for tool execution.
- **Thread rename** - conversations can be renamed and thread rename behavior is
  covered by tests.
- **Thread actions** - adds shared thread-action helpers and tests.
- **Developer grouping** - Developer/code threads are grouped and restored more
  cleanly from the sidebar.
- **Developer workspace state** - Developer UI, storage, thread context, and
  workspace contracts are updated for the new package layout and grouping
  behavior.
- **Sidebar refinements** - sidebar filtering and Developer grouping behavior are
  covered by focused tests.
- **Buddy avatar behavior** - default assistant avatar handling now respects
  selected Buddy identity more consistently.

### Windows Launch, Update & Startup Reliability

- **Launcher diagnostics** - launcher events now write timing and failure details
  to `launcher.log`.
- **Splash hardening** - Tk splash failures are logged, and visible Windows
  console splash fallback is opt-in instead of appearing unexpectedly.
- **Window picker hardening** - first-run picker behavior is hardened to avoid
  blank-console launch paths.
- **Packaged Windows Tk validation** - installer build logic now validates
  bundled Tk support in embedded Python.
- **Native dependency bundling** - Windows Tk smoke checks account for required
  native DLLs, explicit DLL directories, and bundled `zlib`.
- **Ollama startup gating** - batch-level Ollama auto-start is gated behind an
  explicit environment variable.
- **Update handoff helper** - Windows updates now use a detached handoff helper
  so Row-Bot can quit before the installer replaces files.
- **Startup hardening tests** - launcher, splash, update handoff, and startup
  hardening tests are expanded.
- **Packaged launch validation** - Windows installer fixes were verified through
  test-machine install and launch flows.

### Installers, Builds & Release Automation

- **Windows installer rename** - Inno Setup scripts now build Row-Bot branded
  Windows artifacts.
- **Windows embedded runtime fixes** - embedded Python packaging now copies and
  validates required native pieces for Tk and startup smoke checks.
- **macOS packaging fixes** - macOS build scripts understand Row-Bot app naming,
  source layout, and package payload paths.
- **Linux packaging fixes** - Linux build scripts create required package payload
  parent directories and install into the Row-Bot command/data layout.
- **Release workflow updates** - GitHub Actions release workflow now reads
  version metadata from the package layout and builds Row-Bot artifacts.
- **Notarization workflow updates** - notarization submit/check workflows are
  updated for Row-Bot artifact names.
- **Manifest updates** - release manifest helpers and SHA manifest scripts are
  updated for the new artifact contract.
- **Installer docs** - installer README and release docs now document Row-Bot
  artifact names and install behavior.
- **Public site bridge** - public download links temporarily point at the
  published `v3.23.1` Thoth artifacts until v4 artifacts are published.

### Tests & Release Validation

- **Full rebrand audit** - tracked references were audited so remaining legacy
  names are limited to historical release notes, deferred public website
  handoff, and intentional migration compatibility.
- **Compile validation** - source, scripts, and tests were compile-checked after
  rebrand/source-layout work.
- **Full pytest pass** - full test suite passed for release validation, with only
  known warnings/skips and one non-fatal Windows notification thread
  exception after summary.
- **App smoke pass** - `scripts/smoke_app.py` passed against the Row-Bot package
  layout.
- **Focused regression suites** - provider, skills, migration, startup,
  packaging, voice, channel routing, and source-layout tests were added or
  expanded.
- **Live provider validation** - MiniMax live discovery was tested through
  Row-Bot's actual provider system against the real API.
- **Installer validation** - Windows, Linux, and macOS installer build issues
  found during prerelease testing were fixed before final release readiness.
### Breaking Changes And Caveats

- **Manual major-version upgrade** - existing 3.x users should manually install
  Row-Bot v4 for the major rebrand jump. Pre-v4 updater clients expect the old
  Thoth artifact and manifest contract.
- **New data locations** - Row-Bot uses new Row-Bot data paths. Legacy Thoth data
  is copied, not moved.
- **Legacy plugin metadata** - plugins should declare `min_row_bot_version`.
  Legacy plugin manifests are repaired where possible during migration.
- **Artifact names changed** - release assets now use Row-Bot names. Do not
  publish duplicate legacy-named v4 artifacts.
- **Provider discovery depends on APIs** - live model discovery can only reflect
  what providers return through their current APIs.
- **First launch may migrate data** - first v4 launch over a 3.x install can take
  longer while Row-Bot copies and repairs legacy data.

### Files Changed

| File | Change |
|------|--------|
| `src/row_bot/brand.py`, `src/row_bot/runtime_paths.py`, `src/row_bot/version.py`, `static/`, `docs/row_bot_*`, `row-bot.ico` | Row-Bot brand constants, runtime path helpers, version metadata, icons, glyphs, favicon, and docs imagery |
| `src/row_bot/migration/row_bot_legacy_rebrand.py`, `src/row_bot/ui/post_migration.py`, `tests/test_row_bot_legacy_rebrand.py`, `tests/test_post_migration_notice.py`, `tests/test_plugin_manifest_rebrand.py` | Copy-first legacy migration, migration notices, plugin manifest repair, and rebrand compatibility coverage |
| `app.py`, `launcher.py`, `src/row_bot/app.py`, `src/row_bot/launcher.py`, `src/row_bot/__init__.py`, `docs/SOURCE_LAYOUT.md` | Source-layout migration into the `row_bot` package, launcher/app package entry points, and source-layout documentation |
| `src/row_bot/providers/opencode.py`, `src/row_bot/providers/catalog.py`, `src/row_bot/providers/custom.py`, `src/row_bot/providers/model_catalog.py`, `src/row_bot/providers/selection.py`, `src/row_bot/providers/transports/openai_compatible.py` | OpenCode providers, MiniMax live discovery, stale model cleanup, custom endpoint reasoning/vision fixes, and catalog/provider selection updates |
| `src/row_bot/skills_activation.py`, `src/row_bot/slash_commands.py`, `src/row_bot/skills_hub/`, `src/row_bot/ui/chat_composer_extras.py`, `src/row_bot/ui/chat_components.py` | Smart Skills activation, slash commands, Skills Hub marketplace/import/search support, and shared composer skill controls |
| `src/row_bot/voice/`, `src/row_bot/ui/voice_lifecycle.py`, `src/row_bot/ui/voice_realtime_events.py` | Realtime voice runtime, providers, coordinator, agent bridge, action handling, cue/speech policy, and UI event lifecycle |
| `src/row_bot/approval_policy.py`, `src/row_bot/tools/approval_gate.py`, `src/row_bot/threads.py`, `src/row_bot/ui/thread_actions.py`, `src/row_bot/developer/`, `src/row_bot/ui/sidebar.py` | Unified approval modes, approval gates, thread rename/actions, Developer grouping, workspace state, and sidebar refinements |
| `src/row_bot/channels/`, `src/row_bot/tools/row_bot_status_tool.py`, `src/row_bot/ui/status_bar.py` | Channel workflow custom-provider routing, channel commands, Row-Bot status reporting, and Buddy avatar fallback behavior |
| `src/row_bot/update_handoff.py`, `src/row_bot/startup_diagnostics.py`, `installer/launch_row_bot.bat`, `installer/launch_row_bot.vbs`, `installer/build_installer.ps1` | Windows update handoff, startup diagnostics, renamed launch scripts, splash/picker hardening, embedded Tk validation, and native DLL bundling |
| `installer/row_bot_setup.iss`, `installer/build_linux_app.sh`, `installer/build_mac_app.sh`, `installer/build_mac_release.sh`, `installer/install-linux.sh`, `installer/README.md` | Row-Bot Windows, Linux, and macOS packaging, install command naming, source-layout payload handling, and installer documentation |
| `.github/workflows/release.yml`, `.github/workflows/notarize-submit.yml`, `.github/workflows/notarize-check.yml`, `.github/workflows/update-manifest.yml`, `scripts/app_payload_manifest.py`, `scripts/append_sha_manifest.py`, `scripts/cut_release.py` | Release workflow, notarization, update manifest, payload manifest, SHA manifest, and release helper updates |
| `README.md`, `CONTRIBUTING.md`, `docs/RELEASING.md`, `docs/ARCHITECTURE.md`, `docs/CNAME`, `docs/index.html` | Public docs, release docs, architecture docs, Pages domain, and website updates |
| `tests/`, `pytest.ini`, `scripts/smoke_app.py`, `scripts/skills_hub_live_import_matrix.py` | Expanded regression coverage for rebrand, migration, providers, skills, voice, packaging, startup hardening, source layout, and app smoke validation |

## v3.23.1 - Custom Endpoint Tool-Calling Hotfix

This hotfix repairs custom OpenAI-compatible endpoint tool calling for local servers such as LM Studio. Streamed tool-call fragments are now assembled before execution, malformed empty-name fragments are dropped, custom endpoint tool turns fall back to non-stream unless streamed tool calling has been explicitly probed, and endpoint status labels now distinguish local custom endpoints from Ollama.

## v3.23.0 — Provider Runtime, Memory Recall & UI Performance Hardening

This release hardens the runtime paths that were expanded in v3.22.0. The headline work is **provider compatibility**: Thoth now preserves provider-qualified model identity end to end, routes incompatible models into a safer chat-only path, probes custom OpenAI-compatible endpoints before trusting tool support, and normalizes tricky provider transcripts before replay. It also ships a major **memory recall uplift**, with deterministic bounded recall, lexical and graph-expanded candidates, audit metadata, review states, and provenance surfaces. Around that, v3.23.0 makes large transcripts and Settings screens lighter, adds task database recovery, improves local/self-hosted setup, and expands regression coverage around real provider behavior.

### Provider Runtime & Custom Endpoints

- **Provider-qualified model identity** — model choices now keep their provider identity across Settings, catalog pinning, defaults, thread overrides, status displays, setup wizard choices, and runtime construction.
- **No accidental OpenRouter fallback** — unknown bare model IDs no longer silently route to OpenRouter when the original provider cannot be inferred.
- **Runtime readiness routing** — provider/runtime checks now distinguish full agent mode, chat-only mode, and blocked configurations before a broken run starts.
- **Context-window guardrails** — small context windows block agent mode with clearer guidance, while medium windows can use chat-only mode when tool schemas would not fit reliably.
- **Unified context policy** — local, cloud, and custom endpoint context caps now flow through one policy path with model maximums, user caps, and request-time context parameters where supported.
- **Context cache invalidation** — changing local or cloud context settings clears stale LLM clients so subsequent turns use the new limits.
- **Custom endpoint profiles** — OpenAI-compatible endpoints can use profile behavior for common local and proxy servers such as LM Studio, vLLM, llama.cpp, LocalAI, LiteLLM, SGLang, oMLX-style servers, and generic OpenAI-compatible backends.
- **Custom endpoint probing** — self-hosted endpoints can be probed for catalog availability, streaming support, tool-call behavior, and model compatibility, with probe results persisted for later readiness decisions.
- **Native metadata discovery** — LM Studio and llama.cpp metadata paths are used when available to discover context windows and native tool support more accurately.
- **No-auth endpoint support** — local endpoints that do not require API keys can refresh catalogs without unnecessary secret lookups.
- **OpenAI-compatible transport** — adds a dedicated transport for custom OpenAI-compatible chat, streaming, tool serialization, tool-call chunks, reasoning fields, runtime context overrides, and clearer HTTP error messages.
- **Unsupported payload cleanup** — custom endpoint profiles can drop unsupported parameters such as tools, tool choice, parallel tool calls, reasoning, response formats, or tool history when a backend cannot accept them.
- **Tool-call recovery** — local models that emit tool-call envelopes as text or reasoning can be recovered into structured tool calls when safe.
- **Reasoning-only response handling** — reasoning-only outputs after successful tool calls can be promoted into final visible content, while reasoning-only failures after tool errors produce actionable errors instead of silent empty replies.
- **Custom tool validation repair** — local/custom providers can receive a repair message when a tool call misses required fields such as `query`, reducing dead-end schema failures.
- **Ollama tool probing** — unknown or uncertain local Ollama models can be promoted to agent mode only after a real tool round-trip succeeds.
- **Ollama launch cleanup** — the launcher now starts Ollama only when saved Brain or Vision settings actually need local Ollama, and `--no-ollama` forces the skip.
- **Ollama reasoning behavior** — Ollama reasoning is enabled only for detected reasoning models instead of being forced globally.
- **Vision provider refs** — local Vision calls strip provider-qualified Ollama refs at the runtime edge while provider/cloud refs still route through the correct path.
- **Designer runtime readiness** — Designer text refinement and speaker-note generation now use the active model override and verify that the selected model is agent-ready.

### Chat-Only Runtime & Transcript Compatibility

- **Chat-only runtime path** — non-tool or tool-incompatible models can answer normal chat without building the full tool graph.
- **Compact chat-only prompt** — chat-only mode uses a smaller prompt that avoids implying tools, workflows, or task actions are available.
- **Tool-free history shaping** — prior tool turns are summarized for chat-only context without replaying full tool bodies or invalid protocol shapes.
- **Chat-only streaming persistence** — chat-only responses stream and persist through the normal conversation paths.
- **Runtime surface tagging** — chat, channels, workflow approvals, Designer, and forced agent surfaces now tag their runtime mode so provider readiness can make the right routing decision.
- **Provider transcript diagnostics** — model-facing transcripts are inspected for invalid tool calls, duplicate tool IDs, orphan tool results, and reasoning-field hazards.
- **Transcript normalization** — provider-facing messages drop no-op assistant turns, strip invalid tool calls, rewrite duplicate tool-call IDs, drop orphan tool results, and remove unsafe reasoning fields for custom-tool artifacts.
- **Thinking retention** — non-empty thinking/reasoning text is preserved through streaming, reattach, persisted transcript rendering, and final message display.
- **Reasoning-only final guard** — reasoning-only chunks are no longer mistaken for final assistant content when there is no visible answer.
- **Checkpoint transcript loading** — transcript loading can read checkpoint messages and token usage without importing or constructing the agent graph.
- **Legacy checkpoint repair** — checkpoint version values are normalized when older integer versions are encountered.
- **Detached stream finalization** — detached clients can finalize with scoped transcript refreshes instead of rebuilding the full main UI.
- **Optimistic message preservation** — user messages remain visible during detached finalize and reconnect flows.

### Memory Recall & Knowledge Audit

- **Bounded auto-recall policy** — Agent turns now use deterministic memory recall with query building, context-aware token budgeting, scoring, filtering, and trace output.
- **Hybrid recall candidates** — recall combines semantic search, FTS5 lexical search, keyword fallback, and graph-neighbor expansion.
- **Graph-expanded recall** — strong seed memories can pull in related graph nodes with relation confidence and hop metadata.
- **Recall-safe candidate retrieval** — candidate inspection no longer mutates recall timestamps until the final selected memories are injected.
- **Recall reinforcement** — selected memories are touched with `recalled_at` and recall-count metadata after they are actually used.
- **Memory tier scoring** — recall ranks core, semantic, episodic, and resource memories differently based on source, confidence, evidence, recency, and query fit.
- **Status-aware filtering** — archived, needs-review, superseded, stale, weak, greeting-only, runtime-status, and unanchored resource memories are filtered out of normal auto-recall.
- **Recall traces** — recent recall decisions are written to a compact trace file for debugging why memories were included or rejected.
- **FTS5 memory index** — knowledge graph entities now maintain a lexical search index for faster exact/keyword recall.
- **Memory evolution helpers** — new integrity helpers normalize status, tier, confidence, evidence, source context, manual edits, review state, superseding, archival, and journal entries.
- **Memory review states** — memories can now be active, needs review, superseded, or archived without losing the underlying entity.
- **Audit metadata** — extracted, document-derived, wiki-synced, and manually edited memories preserve stronger provenance, confidence, evidence, and source context.
- **Conflict handling** — extraction can mark conflicting memories for review instead of overwriting high-authority user facts.
- **Low-confidence relation filtering** — background extraction skips weak inferred relations instead of adding noisy graph edges.
- **Extraction journal** — memory extraction records run summaries, per-thread details, skipped relations, and extraction outcomes.
- **Resource hub memories** — document extraction creates or updates resource-style hub memories with provenance and audit fields.
- **Wiki sync provenance** — wiki vault sync preserves audit/status metadata and appends memory-evolution journal entries.
- **Knowledge audit UI** — Settings and entity editor surfaces now expose audit badges, filters, review queues, recall traces, and evolution journal entries.
- **Entity review actions** — individual memories/entities can be archived, marked for review, superseded, restored to active, or marked as user-modified from the editor.
- **Memory tool output** — memory search/list/save/update output now includes IDs, status, confidence, tier, and recall-aware results so agents can modify the right memory.

### UI Performance & Transcript Loading

- **UI performance utilities** — adds generation tokens, timed UI sections, slow-section logging, and safe UI callback/task wrappers.
- **Bounded transcript windows** — large conversations render a bounded visible window with an explicit load-earlier path instead of rebuilding every message at once.
- **Async model picker cache** — model picker options are cached and refreshed asynchronously so chat inputs can appear quickly.
- **Model surface placeholders** — chat can render lightweight model/provider placeholders while detailed model status resolves in the background.
- **Generation-safe token counters** — token counter updates are debounced and ignored when they belong to an older render generation.
- **Lazy Home panels** — Home tab panels defer heavier Developer, Designer, Knowledge, and Activity work until opened.
- **Coalesced status refreshes** — Home status pill refreshes are cached and coalesced to reduce repeated expensive checks.
- **Settings generation guards** — Settings tab renders use generation tokens and local error boundaries so stale async work cannot overwrite newer UI.
- **Deferred Settings tabs** — heavier Settings tab content is scheduled lazily instead of blocking the shell.
- **Lazy Knowledge sections** — memory browsing, audit details, relationship loading, recall traces, and journal rows load on demand.
- **Off-UI-loop entity saves** — entity editor saves run off the UI loop and refresh Knowledge state in staged steps.
- **Render instrumentation** — graph chat, streaming, Mermaid rendering, text embeds, transcript rendering, and blank-thread startup now include performance instrumentation.
- **Performance harness** — adds a local harness for profiling real transcripts and blank-thread shells.

### Task Database Recovery

- **Shared data path helpers** — local database paths now resolve through a shared data-path module for tasks, memory, threads, and diagnostics.
- **Task schema validation** — startup/task operations validate required tables and columns before use.
- **In-place schema repair** — partial task databases can be repaired in place while preserving existing rows when possible.
- **Corrupt DB recovery** — corrupt task databases are backed up and recreated with a clean schema.
- **Schema retry wrappers** — task operations retry once after repairing schema-related SQLite errors.
- **Malformed migration tolerance** — workflow-to-task migration skips malformed legacy rows after the destination schema exists.
- **Launcher recovery commands** — `launcher.py --reset-tasks-db`, `--reset-db`, and `--restore-data` can back up and recreate local SQLite stores.
- **WAL/SHM backup coverage** — task, memory, and thread DB backup/restore handles SQLite companion files.
- **Support diagnostics** — Home, Command Center, and `thoth_status` show task-schema state, recovery guidance, last repair, and schema errors.

### Tools, Channels & Runtime Reliability

- **Channel runtime routing** — Telegram, WhatsApp, Discord, Slack, and SMS now mark channel turns as channel/auto runtime, while approval resumes force agent mode.
- **Approval resume routing** — channel approval resumes explicitly request agent mode so tool continuations do not fall into chat-only routing.
- **Wikipedia HTTPS endpoint** — the Wikipedia tool forces the legacy client onto the HTTPS API endpoint.
- **Wikipedia recoverable errors** — upstream JSON/API failures now return a recoverable tool result that tells the agent not to retry the same query blindly.
- **Wikipedia usage guidance** — the tool description now steers broad conceptual questions away from unnecessary encyclopedia lookups.
- **Thoth Status model reporting** — status output reports the effective runtime model/mode more accurately.
- **Thoth Status task reporting** — scheduled-task status now includes schema diagnostics before listing configured tasks.
- **Command Center recovery copy** — task-schema failures point users toward the new launcher recovery command.

### Tests & Release Checks

- **Provider readiness coverage** — tests cover agent/chat-only/block routing, context floors, cached capability snapshots, OpenRouter metadata, Ollama probing, and custom endpoint probing.
- **Custom provider coverage** — tests cover profiles, no-auth endpoints, native metadata discovery, streaming probes, context overrides, and setup wizard payloads.
- **OpenAI-compatible transport coverage** — tests cover request payloads, tool calls, streaming, reasoning-only finals, unsupported parameters, and provider error handling.
- **Provider selection coverage** — tests cover provider-qualified refs, duplicate model IDs across providers, Ollama refs, Quick Choices, and stale capability refresh.
- **Chat-only and transcript coverage** — tests cover chat-only streaming, forced agent surfaces, checkpoint transcript loading, checkpoint version repair, detached finalize, and thinking retention.
- **Memory recall coverage** — tests cover auto-recall scoring, filtering, graph expansion, recall traces, evolution helpers, audit helpers, and memory extraction metadata.
- **UI performance coverage** — tests cover generation tokens, safe UI callbacks, bounded transcript windows, lazy Knowledge surfaces, staged refreshes, and performance harness wiring.
- **Task recovery coverage** — tests cover empty data dirs, partial schemas, corrupt DB recreation, migration tolerance, launcher reset/restore args, and DB-family backup.
- **Tool/runtime regressions** — tests cover Wikipedia recovery, Vision provider refs, Designer routing, Home performance, model picker regressions, and opt-in live provider matrix behavior.
- **Live provider marker** — adds a `live_provider` pytest marker for real configured-provider calls that remain opt-in.

### Release Notes & Risk Notes

- **Custom endpoint compatibility depends on the server** — profiles and probes improve behavior for common OpenAI-compatible servers, but local/proxy backends can still vary in tool syntax, streaming behavior, and context parameter names.
- **Chat-only mode is intentionally limited** — models routed to chat-only mode can answer normal conversation but should not be expected to run tools, workflows, or structured agent actions.
- **Memory recall is more selective** — archived, superseded, weak, or unanchored memories may stop appearing automatically; users can still review and restore memory state from Knowledge surfaces.
- **Task DB recovery backs up before reset** — recovery commands preserve old SQLite files under the local recovery directory, but reset flows can remove active scheduled-task rows from the live DB until restored.
- **Live provider tests are opt-in** — the new live matrix is useful for release validation with configured credentials, but it is not part of the normal offline unit suite.

### Files Changed

| File | Change |
|------|--------|
| `agent.py`, `models.py`, `prompts.py`, `threads.py` | Runtime readiness routing, chat-only execution, provider transcript normalization, thinking retention, context policy usage, and checkpoint transcript helpers |
| `providers/custom.py`, `providers/readiness.py`, `providers/resolution.py`, `providers/runtime.py`, `providers/selection.py`, `providers/tool_protocol.py`, `providers/transports/openai_compatible.py`, `providers/ollama.py` | Provider-qualified resolution, custom endpoint profiles/probes, OpenAI-compatible transport, Ollama probing/reasoning behavior, context overrides, and tool validation repair |
| `ui/setup_wizard.py`, `ui/provider_settings.py`, `ui/model_catalog.py`, `vision.py`, `designer/ai_content.py` | Custom endpoint setup fields, provider-qualified setup selections, async model-picker behavior, Vision provider-ref routing, and Designer model readiness |
| `memory_policy.py`, `memory_evolution.py`, `knowledge_graph.py`, `memory.py`, `memory_extraction.py`, `document_extraction.py`, `wiki_vault.py`, `tools/memory_tool.py` | Bounded recall policy, lexical/graph recall candidates, memory audit metadata, evolution journal, extraction provenance, and memory tool output |
| `ui/knowledge_audit.py`, `ui/entity_editor.py`, `ui/settings.py`, `ui/graph_panel.py` | Knowledge audit helpers, entity review actions, lazy Knowledge settings surfaces, recall traces, and memory evolution journal UI |
| `ui/performance.py`, `ui/transcript.py`, `ui/chat.py`, `ui/chat_components.py`, `ui/render.py`, `ui/streaming.py`, `ui/home.py`, `ui/status_bar.py`, `ui/command_center.py` | UI performance instrumentation, bounded transcript rendering, detached finalize improvements, async picker loading, lazy Home panels, cached status refresh, and task recovery copy |
| `tasks.py`, `data_paths.py`, `launcher.py`, `tools/thoth_status_tool.py` | Task DB schema validation/repair, recovery commands, data path helpers, backup/restore support, and support diagnostics |
| `channels/approval.py`, `channels/telegram.py`, `channels/whatsapp.py`, `channels/discord_channel.py`, `channels/slack.py`, `channels/sms.py` | Runtime surface tagging for channel turns and approval resumes |
| `tools/wikipedia_tool.py` | HTTPS API endpoint forcing, recoverable Wikipedia errors, and safer tool usage guidance |
| `scripts/reasoning_completion_harness.py`, `scripts/ui_performance_harness.py`, `pytest.ini`, `tests/` | Reasoning/runtime harnesses, UI performance harness, live-provider marker, and focused regressions for provider runtime, memory recall, UI performance, task recovery, transcript loading, Vision, and Wikipedia |

---

## v3.22.0 — Developer Studio, Custom Tools, Workflow Delivery & Stability Overhaul

This release turns Thoth into a broader **workbench for chat, workflows, code, documents, and user-built tools**. The headline feature is **Developer Studio**: a Codex-style coding workspace for connecting local Git repositories, reviewing code, planning and applying changes, running tests, preparing PRs, and working inside an optional Docker shadow sandbox. It also adds **Custom Tools**, letting users turn GitHub repos or local folders into reusable Thoth tools through a guided or conversational flow. Around that, v3.22.0 substantially improves workflow delivery defaults, Home status visibility, Settings organization, onboarding, model catalog performance, embedding provider choice, chat tool traces, and app stability diagnostics.

### Developer Studio

- **Developer workspace surface** — adds a new Developer home tab for code workspaces, recent repos, explicit local-folder linking, explicit clone destinations, and code-thread restoration from the sidebar.
- **Code threads** — Developer conversations are marked as code threads, reopen directly into Developer Studio, keep workspace context, and preserve code-specific state separately from normal chat and Designer threads.
- **Repository context injection** — Developer turns receive compact, authoritative workspace context including repo path, branch, dirty state, remote URL, top-level files, approval mode, execution mode, and shell guidance, without showing that context in the user message.
- **Codex-style approval modes** — Developer Studio supports coding approval modes such as read-only, ask before changes, auto edit, and agent run. The mode is changeable at any time and reflected in the Developer Inspector safety policy.
- **Developer-native tools** — adds workspace-scoped tools for repo info, file listing, file reads, search, git status, branch create/switch, commit, push, fast-forward merge, diffs, todos, detected test commands, shell commands, patch preview/apply, file writes, sandbox imports, and agent-owned change reverts.
- **Developer skills and tool guides** — adds Developer-focused bundled skills for coding, review, PR prep, and Custom Tools, plus a concise Developer tool guide. These are wired for Developer context instead of bloating normal chat by default.
- **Developer todo planning** — adds persistent visible todos for coding threads, with status updates surfaced in the inspector so long coding jobs can keep a checkpointed plan.
- **Developer Inspector** — adds a right-side Developer Inspector with Overview, Safety Policy, Sandbox, Todos, Changes, Files, Agent Changes, Tests, and GitHub/PR sections.
- **Live inspector snapshots** — the inspector refreshes from debounced background snapshots instead of full UI rebuilds, preserving expanded sections and reducing disconnect/crash risk during long runs.
- **Resizable inspector** — the Developer Inspector can be widened for diffs, files, and test output without crowding the main chat.
- **File tree view** — the Files section renders a tree-style repo view instead of a flat label list, making larger repos easier to scan.
- **Diff and change review** — changed files show added/removed line counts, per-file diffs, and agent-owned change sets.
- **Safe revert support** — agent-owned edits are recorded and can be reverted when files have not drifted.
- **GitHub CLI integration** — Developer Studio detects `gh` from common Windows install paths and gates PR/push operations through Developer approval policy.
- **Long coding turn budget** — Developer Studio now gets its own recursion/step budget, separate from normal chat and workflows, with Developer-specific wind-down prompts that checkpoint progress instead of failing with a generic tool-loop message.

### Docker Sandbox

- **Optional Docker execution mode** — Developer workspaces can run commands in a Docker shadow copy instead of the real repo folder.
- **Persistent sandbox container** — Docker Sandbox uses a persistent per-workspace container and shadow workspace, so repeated commands share the same sandbox state until cleaned or rebuilt.
- **Import-gated edits** — changes made in Docker Sandbox are recorded as pending patches and only affect the real repo after explicit import.
- **Network policy** — Docker Sandbox can run with network off, ask, or on. Network commands and package installs are blocked early when network is off.
- **Sandbox image selection** — users can choose the Docker image for a workspace; changing it cleans the current sandbox copy before the next Docker command.
- **Sandbox process controls** — long-running sandbox processes can be started and stopped through Developer tooling.
- **Clear Docker startup errors** — stopped Docker Desktop, missing images, and credential-helper failures now produce actionable messages instead of raw pipe/file-not-found errors.
- **Local fallback remains available** — users who do not want Docker can keep using local execution with the existing Developer approval policy.

### Custom Tools

- **Custom Tools product surface** — Developer home now includes a Custom Tools area separate from code workspaces, with cards for created tools, commands, test output, enablement, promotion, and removal.
- **Guided Custom Tool wizard** — adds a Source -> Inspect -> Test -> Enable flow for turning a repo URL, local folder, or current workspace into a reusable Thoth tool.
- **Conversational Custom Tool Builder** — adds one agent-facing `custom_tool_builder` utility so users can ask Thoth to inspect a repo, draft commands, refine them, create the tool, and promote it without manually writing a manifest.
- **LLM-assisted command proposals** — Custom Tool creation can use a lightweight model pass to infer useful read-only commands from a repository, with deterministic fallback when AI analysis is unavailable.
- **Safety validation** — proposed Custom Tool commands are validated for dangerous shell patterns, unreviewed network use, write operations, and missing query placeholders.
- **One-time command tests** — Custom Tools can be tested before enablement; local/read-only commands can run directly, while network or riskier commands route through the normal approval policy.
- **Promotion to normal chat** — tested Custom Tools can be promoted into the plugin/tool surface and optionally made available in normal chat through the Utilities toggle.
- **Plugin integration** — promoted Custom Tools register as synthetic plugin tools, appear in plugin/tool management, and can be disabled or removed safely.
- **Source transparency** — Custom Tool cards show source URL, local install path, version, command count, availability, and enablement state.
- **Terminology cleanup** — user-facing UI uses “Custom Tool” instead of the earlier “capsule” wording.

### Workflow Delivery & Workflow Console

- **Workflow-level delivery defaults** — adds a default delivery channel selector for background workflows so new workflows do not default to every channel.
- **Multi-channel defaults** — default delivery can target multiple configured channels while every workflow still always reports run status to the web app.
- **Per-workflow overrides** — workflows can inherit the global default or keep a specific override; changing the global default updates only workflows tied to default.
- **No extra LLM delivery pass** — delivery defaults reuse existing workflow outputs instead of adding an extra model call.
- **Delivery UI polish** — the workflow delivery control was moved and restyled so it no longer reads as part of the multi-select label.
- **Collapsible workflow console** — the right workflow console can collapse/expand, persists its state, and works in browser and pywebview.
- **Approval attention state** — collapsed workflow console shows an attention state when a workflow approval is waiting.
- **Workflow console compact badges** — collapsed state shows compact badges for running workflows, approvals, and insights while expanded state keeps the normal console layout.
- **Recent and upcoming runs** — workflow console surfaces running, approvals, upcoming scheduled runs, quick launch, and recent runs in a denser layout.
- **Workflow Buddy sync** — Buddy state now clears correctly after workflow approval/denial, timeout, stop, cancel, and successful completion.

### Home Status & Buddy Reliability

- **Expanded Home health bar** — Home status now includes compact icon pills for Ollama, active model, cloud API, tunnel, Gmail OAuth, Calendar OAuth, X OAuth, workflows, knowledge, wiki vault, documents, search, skills, tracker, Buddy, MCP, plugins, network, tools, disk, threads DB, FAISS index, Dream Cycle, TTS, and logging.
- **Accurate document/vector status** — document status pills now use the same indexed-file/vector metadata path as Settings.
- **MCP and plugin visibility** — Home status now covers MCP and plugin health instead of only older core checks.
- **Sleek icon-only pills** — status pills use compact icons with hover tooltips, plus amber/red warning indicators for degraded states.
- **Background progress inside status bar** — document extraction and Buddy generation progress remain inside the Home status area while the icon row stays compact.
- **Buddy state machine cleanup** — Buddy state transitions are more deterministic around workflow approvals, denials, pending states, and workflow endings.
- **Desktop overlay focus behavior** — desktop Buddy more reliably appears when the app is minimized or unfocused and hides when the app returns to focus.

### Chat, Streaming & Tool Traces

- **Shift+Enter newline fix** — Shift+Enter now inserts a newline in chat inputs instead of sending the message, matching normal chat app behavior.
- **Input-level model picker** — the main chat model selector moved into the chat input area to match Designer and reduce top-bar clutter.
- **Cloud/privacy banner refresh** — the banner updates when the model changes from the input picker.
- **Grouped tool calls** — repeated tool calls of the same type are grouped into a single expandable trace instead of flooding the transcript with long repeated lists.
- **Balanced browser traces** — browser automation traces are less screenshot-heavy by default while still preserving final visual context when useful.
- **Live tool-call rendering fixes** — tool-call counts and grouped trace state update during streaming instead of only after a later message or reload.
- **Detached stream recovery** — long streams that detach because the client disconnects now persist media and refresh the transcript without forcing full chat rebuilds.
- **Inline approval backup** — Developer approvals also render inline in the active thread when modal/dialog context is unavailable, reducing hidden approval states.
- **NiceGUI timer hardening** — safe one-shot and polling timer helpers avoid creating UI from deleted slots or disconnected clients.

### Settings & Onboarding

- **Settings information architecture cleanup** — window mode moved from System to Preferences, Dream Cycle moved from Knowledge to Preferences, and tunnel settings moved from Channels to System.
- **Settings polish pass** — remaining settings tabs were updated toward the denser Models/Providers/Buddy style, including Utilities, Search, Tracker, Documents, Voice, Vision, Knowledge, System, and related tabs.
- **Model settings cache path** — Settings can render model selectors from cached catalog data while catalog refresh runs in the background.
- **Single catalog refresh concept** — manual refresh is exposed as one model-catalog action instead of many provider-specific refresh buttons.
- **Provider-first onboarding** — first-run onboarding now starts with model/provider choice before migration and setup checklist steps.
- **Setup Center** — adds a resumable setup center reachable from the sidebar hello button, covering model/provider, migration, memory/docs, workflows, Designer, channels, voice, and related setup.
- **Cleaner onboarding copy** — onboarding removes excessive explanatory text, uses quick setup actions, and routes users to Settings only where deeper configuration is needed.
- **All provider coverage** — first setup includes the current provider family, including ChatGPT / Codex, API-key providers, Ollama/local, custom endpoints, and newer providers.
- **Default workflow templates** — seeds five disabled real-world starter workflows, with three simpler and two advanced examples, so nothing runs on a schedule without user permission.
- **Updated welcome message** — first-run welcome and starter prompts now reflect current Thoth features such as workflows, Designer, Developer, channels, documents, memory, voice, and Custom Tools.

### Models, Providers & Embeddings

- **Provider-qualified model selection** — model choices now preserve provider identity across settings, catalog pinning, defaults, thread overrides, status displays, and runtime construction, preventing local/custom models from silently falling back to OpenRouter.
- **Custom endpoint compatibility profiles** — OpenAI-compatible endpoints now include profile behavior for oMLX, LM Studio, vLLM, llama.cpp, LocalAI, LiteLLM, SGLang, and generic servers, including message normalization, unsupported tool-parameter dropping, and profile-aware context handling.
- **Context override consistency** — local and provider context caps now apply through one policy path, cap to known model/provider maximums, invalidate stale override clients when changed, and pass request-time context parameters for custom endpoints that support them.
- **Non-tool local model guardrails** — native Ollama agent chat now rejects unsupported non-tool models before a broken run, while non-tool custom OpenAI-compatible profiles flatten tool history and omit tool payloads for better server compatibility.
- **Ollama Cloud support** — adds Ollama Cloud as a provider path with direct cloud API transport and support for Ollama daemon cloud-tagged models.
- **Ollama daemon catalog improvements** — installed local models, cloud-tagged local daemon models, library models, families, vision capability, tool capability, and embedding markers are handled more consistently.
- **Ollama vision support paths** — vision-capable Ollama models can be represented through both daemon and direct cloud paths where metadata supports it.
- **Background model catalog cache** — provider and Ollama catalog rows are refreshed in the background and cached for faster Settings loads.
- **Catalog age and refresh state** — model catalog refresh state, cache age, and warnings are tracked for diagnostics and UI display.
- **Provider refresh log noise cleanup** — noisy but non-fatal provider refresh states are preserved without replacing working defaults.
- **Codex SSE diagnostics** — Codex Responses streaming logs start, first delta, completion, and incomplete-stream states more clearly.
- **Configurable embedding providers** — embeddings can now be configured separately from chat models.
- **Local embedding choices** — adds local embedding provider configuration around Qwen, Nomic, and Mixedbread/MXBAI-style models.
- **Cloud embedding option** — supports optional cloud embedding providers with privacy warning copy in Settings.
- **Embedding metadata** — vector stores record embedding provider/dimension metadata and can detect stale indexes when the embedding config changes.
- **Embedding memory release** — heavyweight document and memory extraction paths release cached embedding resources afterward to reduce memory pressure.
- **Document dependency fixes** — adds missing document/embedding support dependencies needed by Markdown and local embedding flows.
- **YouTube transcript packaging** — packages `youtube-transcript-api` so the YouTube transcript tool works in installed builds, not only on the build machine.

### Stability, Startup & Shutdown

- **Stability monitor module** — adds crash reports, UI callback error reports, client-side error capture, asyncio exception handling, thread/unraisable hooks, memory snapshots, and event-loop lag logging.
- **Settings crash diagnostics** — model settings load, collect, and render phases log timings and memory snapshots so large-provider crashes are easier to diagnose.
- **Startup sequencing** — startup now updates splash/status through cached model catalog loading, workflow scheduler start, MCP startup, plugin load, channel migration/autostart, tunnel startup, and knowledge graph load.
- **Clean shutdown work** — app shutdown now attempts ordered channel, tunnel, MCP, and scheduler cleanup to reduce locked log files and lingering processes.
- **Channel credential migration** — channel credentials are migrated into a channel-specific keyring path while preserving legacy fallback if migration fails.
- **Channel status recovery** — channel auth status reporting distinguishes running channels from empty UI fields and legacy keyring fallback.
- **Ngrok log noise handling** — tunnel info logs were reviewed and kept non-fatal while startup/status copy clarifies tunnel state.
- **Windows installer channel inclusion** — installer regressions now ensure new channel auth files are included.
- **Linux native baseline guard** — Linux package builds scan native libraries for unsupported CPU baselines before release upload.

### Tests & Release Checks

- **Developer Studio coverage** — adds phased Developer Studio tests covering workspace setup, approval policy, Git safety, context injection, UI wiring, todos, diffs, tools, Custom Tools, Docker Sandbox, GitHub/PR helpers, and recursion budget.
- **Workflow delivery coverage** — tests default delivery inheritance, overrides, and web-app delivery guarantees.
- **Channel auth coverage** — tests channel keyring migration, fallback, and packaging inclusion.
- **Chat UI coverage** — tests Shift+Enter behavior, grouped tool traces, browser trace behavior, and streaming refresh contracts.
- **Onboarding coverage** — tests setup wizard/center ordering, provider coverage, and starter workflow seeding.
- **Embedding coverage** — tests embedding config, metadata, stale-index detection, and provider switching.
- **Model catalog coverage** — tests background cache shape, refresh behavior, and Ollama/cloud catalog rows.
- **Settings contract coverage** — tests tab moves, section labels, providers guide placement, tunnel relocation, and cloud banner expectations.
- **Home status coverage** — tests expanded status checks, workflow console collapse state, Buddy state transitions, and status accuracy.
- **Stability coverage** — tests performance/stability diagnostics, safe timer behavior, detached stream refresh, and callback error handling.
- **Packaging coverage** — tests YouTube transcript dependency packaging, channel auth store inclusion, Linux native baseline guard, and Windows installer file coverage.
- **Current validation** — the legacy release smoke suite passes with `1885 passed, 0 failed, 5 warnings` after the Developer recursion-budget merge; targeted Developer sandbox + recursion tests pass (`39 passed`).

### Release Notes & Risk Notes

- **Developer Studio is powerful by design** — coding tools can read, edit, run commands, and use Git inside the selected workspace according to the active approval mode. Users should connect only repositories they intend Thoth to inspect or modify.
- **Docker Sandbox is optional** — local execution remains available. Docker Sandbox requires Docker Desktop or a compatible Docker/Podman runtime, a local sandbox image, and enough disk space for shadow workspaces.
- **Custom Tools can execute repo-provided command logic** — Custom Tools are opt-in, testable, removable, and gated by normal tool enablement, but promoted tools should still be reviewed before broad chat availability.
- **Cloud embeddings send text to the chosen provider** — local embeddings remain available for users who want document/vector indexing to stay local.
- **Model catalog freshness is eventually consistent** — cached model rows make Settings faster and more stable, while manual/background refresh updates provider availability after the cache is built.
- **Workflow delivery changes may alter notification volume** — workflows tied to default delivery now follow the workflow-level default instead of sending everywhere.
- **Landing page has been updated for v3.21.0, not yet for v3.22.0 assets** — release download/version links should be updated after v3.22.0 artifacts are published.

### Files Changed

| File | Change |
|------|--------|
| `developer/`, `tools/developer_tool.py`, `tool_guides/developer_guide/`, `bundled_skills/developer_*` | Developer Studio workspace state, tools, approval policy, Git helpers, inspector snapshots, todos, diffs, Docker Sandbox, GitHub helpers, tool guide, and Developer skills |
| `developer/tool_capsules.py`, `tools/custom_tool_builder_tool.py`, `tool_guides/custom_tool_builder_guide/`, `plugins/loader.py`, `plugins/ui_settings.py` | Custom Tool creation, testing, promotion, plugin registration, settings/plugin UI integration, and global builder utility |
| `ui/home.py`, `ui/status_bar.py`, `ui/status_checks.py`, `ui/buddy.py`, `buddy/brain.py` | Home status-bar expansion, workflow console collapse/attention states, Buddy state cleanup, and desktop overlay focus behavior |
| `tasks.py`, `ui/task_dialog.py` | Workflow delivery defaults, per-workflow overrides, web-app run status delivery, and workflow dialog UI polish |
| `ui/chat.py`, `ui/chat_components.py`, `ui/streaming.py`, `ui/tool_trace.py`, `ui/timer_utils.py`, `agent.py` | Chat input model picker, Shift+Enter behavior, grouped tool traces, detached streaming recovery, inline approvals, safe timers, and Developer recursion budget |
| `ui/settings.py`, `ui/setup_wizard.py`, `ui/onboarding_center.py`, `ui/onboarding_state.py`, `ui/model_catalog.py`, `ui/command_center.py`, `ui/sidebar.py` | Settings reorganization/polish, onboarding overhaul, setup center, cached model catalog UI, and Developer/sidebar routing |
| `providers/ollama.py`, `providers/model_catalog.py`, `providers/model_catalog_cache.py`, `providers/transports/ollama_cloud.py`, `providers/runtime.py`, `providers/catalog.py` | Ollama Cloud support, improved Ollama/local/cloud catalog rows, background model catalog cache, and provider runtime wiring |
| `embedding_config.py`, `embedding_providers.py`, `documents.py`, `document_extraction.py`, `memory_extraction.py`, `knowledge_graph.py` | Configurable embedding providers, embedding metadata/stale-index checks, local/cloud embedding support, and memory release after heavy extraction |
| `channels/auth_store.py`, `channels/*.py`, `app.py` | Channel credential keyring migration, channel startup/status cleanup, startup sequencing, and shutdown cleanup |
| `stability.py`, `launcher.py`, `ui/head_html.py` | Crash reporting, client-side error capture, performance snapshots, event-loop lag logging, startup/shutdown diagnostics, and frontend error reporting |
| `installer/thoth_setup.iss`, `installer/build_linux_app.sh`, `.github/workflows/release.yml`, `scripts/check_linux_native_baseline.py`, `requirements.txt` | Packaging updates for channel auth, YouTube transcripts, embedding/document dependencies, and Linux native CPU-baseline guard |
| `tests/`, `tests/test_suite.py` | New focused regressions for Developer Studio, Docker Sandbox, Custom Tools, workflow delivery, onboarding, model catalog cache, embeddings, settings contracts, status checks, channel auth, chat traces, stability, and packaging |

---

## v3.21.0 — Buddy Companion, Model Picker Polish & Linux Startup Reliability

This release adds Thoth's **Buddy companion foundation**, a local-first animated presence that can live in the app sidebar, move around the workspace, and optionally open as a native desktop overlay. It also tightens Settings -> Models behavior, improves provider and Vision model selection, and hardens packaged startup on Windows and Linux so optional native dependency failures are easier to diagnose and less likely to block launch.

### Buddy Companion Foundation

- **Buddy subsystem** — adds a prompt-generated Buddy architecture with a thread-safe event bus, deterministic behavior brain, persistent config, pack validation, Hatch art/motion generation, canvas playback/effects, one dockable in-app Buddy, and a separate desktop overlay surface.
- **Live Thoth awareness** — Buddy receives chat streaming, thinking, tool, approval, workflow, notification, and voice-state events from existing runtime paths.
- **Single configured identity** — Buddy surfaces no longer render a separate companion name or duplicate Buddy-name setting; the assistant identity remains owned by Preferences, while Buddy UI focuses on state, personality, and motion.
- **Desktop overlay route** — adds `/buddy-overlay` plus pywebview helpers for a named Buddy window where native overlay support is available.

### Buddy Motion & UI Polish

- **Generated animation boundary** — Buddy ships with bundled first-party `glyph`, `lumen`, `ember`, `pixel`, `sprout`, and `orbit` motion packs. Hatch-generated custom Buddy art and compact image-to-video motion packs are copied into Thoth's served Buddy assets, while normal playback switches locally across idle, thinking, working, approval, success, and error states without runtime model calls.
- **Generated pack quality** — Hatch prompts request keyable backgrounds, frame padding, and rim-lit dark edges so generated packs preserve character detail during transparency compositing; Google Veo starts are paced during fresh bundled regenerations, and runtime corner-keying is gentler so bundled pack edges stay intact.
- **Motion semantics** — approval, denial, timeout, cancellation, interruption, and completion states now map to explicit Buddy clips; MP4 playback crossfades state changes, smooths loop restarts, and replays idle motion periodically without looking busy.
- **Dockable in-app presence** — Buddy starts inside a sidebar home circle, can be dragged into the workspace, leaves the sidebar dock visibly empty while away, snaps home when released near the dock, and returns home on app restart instead of persisting a stray position.
- **Settings polish** — Buddy Settings groups where Buddy appears, behavior, look, and generated-motion guidance in a dense Models-tab-style layout. Visual pack selection uses preview tiles, clears stale Hatch overrides when a bundled pack is selected, and refreshes existing in-app and desktop clients.
- **Hatch save recovery** — saving Buddy settings now preserves freshly generated Hatch art and motion pointers instead of falling back to the selected bundled pack. Hatch outputs are promoted into selectable user packs, still-only generated art remains valid when motion is poor or unavailable, generated packs can be switched back to still-only mode, generated Hatch packs can be deleted from the picker, motion retry regenerates the selected user-generated still without overwriting the selectable pack manifest, and new motion requests use provider-compatible 5-second clips. Full Buddy generation now runs as a background job with Home status-bar progress, completion notifications, and private baked-in still/video prompts so simple user concepts do not turn into pose sheets; the visible concept field stays clean while internal personality/style guidance remains private. Generated Hatch motion now preserves full-frame opaque stills and uses the same cover-framed corner-keying path as bundled motion packs, while transparent stills are composited onto a stable keyable background before video generation; existing Hatch packs whose manifest was overwritten by retry metadata are recovered when loaded, and stopping a workflow immediately moves Buddy out of the running-workflow state.

### Buddy Desktop Overlay Reliability

- **Native overlay stability** — desktop Buddy preserves important approval, denial, workflow, and error bubbles even in Quiet mode, keeps bubbles visible across rapid state settling, applies first-paint transparent document styling, and reveals only after the transparent Buddy document has painted.
- **Window creation fallback** — the native overlay retries with simpler pywebview options if a backend rejects transparency or hidden-window hints, avoids snapshot pushes into deleted NiceGUI clients, and guards startup health-check results so transient `None` values cannot crash the native window.
- **Workflow state cleanup** — approval denials and timeouts clear approval and workflow activity immediately; denied, timed-out, stopped, or cancelled workflow endings clear Buddy workflow-step state; successful multi-step workflow endings emit `done` instead of a misleading cancellation.

### Models, Vision & Settings Reliability

- **Settings and timer stability** — Settings -> Models opens the provider/model catalog lazily and caps provider rows so very large catalogs no longer crash the UI, while NiceGUI one-shot and polling timers clean up when clients disconnect or parent slots are deleted instead of flooding logs with deleted-slot errors.
- **Model catalog and picker clarity** — installed local Ollama chat models appear in Settings -> Models even when their family is not yet in Thoth's curated tool/vision capability lists. Brain and Vision pickers now make it clear that catalog rows must be pinned before they appear as everyday choices.
- **ChatGPT / Codex Vision pins** — Codex Vision pins keep their provider-specific image-input capability during Quick Choice refreshes, and the Codex Responses transport preserves multimodal image blocks so captured screenshots are sent to Codex Vision models instead of being flattened to text-only requests.
- **Vision and setting updates** — `thoth_update_setting` validates Brain and Vision model changes against Quick Choices, installed local Ollama models, and provider catalog rows before saving, exposes an explicit `vision_model` setting, and rejects invented or unavailable model names with actionable guidance.

### Linux & Startup Reliability

- **Linux launcher install-path fix** — the generated Linux launcher resolves installed symlink chains before computing the app root, so `~/.local/bin/thoth` starts the packaged app from `~/.local/share/thoth/current`; release CI smokes through the installed user launcher path.
- **Linux packaged startup resilience** — packaged Linux launches now report startup log tails, child-process exit details, configurable `THOTH_STARTUP_TIMEOUT`, and targeted hints for native OpenCV/FAISS/NumPy dependency failures. Camera and screenshot capture degrade gracefully if OpenCV/MSS cannot import instead of blocking app startup.
- **Linux native CPU-baseline compatibility** — packaged Python builds now keep NumPy below the newer Linux x86_64 wheel line that can require `x86-64-v2` CPU instructions, and Linux package builds scan embedded native libraries for `x86-64-v2/v3/v4` requirements before upload to prevent startup crashes on older x86_64 machines.
- **Linux installer UX hardening** — source-checkout builds support the root-level `bash build_linux_app.sh <version>` support command, install success messages print `~/.local/bin/thoth` when `~/.local/bin` is not on `PATH`, and maintainer docs distinguish unreleased tarball testing from the one-line installer that resolves published GitHub Release assets.
- **Optional native package diagnostics** — startup detects installed-but-broken optional native packages such as TorchCodec, logs a concrete recovery command, and makes Transformers treat broken TorchCodec as unavailable instead of letting optional audio/video helpers crash Thoth during startup.
- **Windows embedded-Python repair hardening** — Windows installer repair/upgrade replaces the bundled `{app}\python` runtime before copying the new payload, preventing manually installed or corrupted packages from surviving an over-the-top reinstall.

### Tests & Release Checks

- **Buddy coverage** — focused tests cover core event/config/asset behavior, Hatch motion activation, UTF-8 config loading, UI wiring, event source hooks, runtime fallback behavior, dockable in-app behavior, built-in motion semantics, and packaging inclusion. Manual-style browser smokes verify docked, undocked, and overlay playback from the bundled pack.
- **Reliability coverage** — startup hardening tests cover broken TorchCodec detection, Linux native dependency recovery hints, NumPy `x86-64-v2` startup failures, launcher log-tail diagnostics, Windows installer embedded-Python replacement, app import smoke, Settings -> Models catalog bounds and picker guidance, status-tool model validation, safe timer cleanup, and installed Linux launcher symlink/default invocation resolution.
- **Provider/Vision coverage** — provider tests cover ChatGPT / Codex Vision Quick Choice capability retention and Codex Responses multimodal image payload preservation.
- **Release smoke** — release and CI workflows build Windows, macOS, and Linux artifacts for v3.21.0, run focused startup/provider suites before installer builds, and smoke the installed Linux launcher path.
- **Test layout cleanup** — root-level test files now live under `tests/`, pytest discovers that folder by default, CI/release workflows call the moved paths, and installer regressions assert the `tests/` tree is not shipped in Windows, Linux, or macOS packages.
- **Current validation** — focused release tests pass (`110 passed, 2 skipped`), the legacy release smoke suite reports `ALL TESTS PASSED!`, full `pytest -q` passes (`255 passed, 3 skipped`), `git diff --check` is clean, stale-version search only finds the previous release's historical changelog section, and `docs/index.html` remains untouched.

### Release Notes & Risk Notes

- **Desktop overlay support varies by platform** — Buddy's in-app surface is the primary supported experience; the native transparent desktop overlay depends on pywebview/backend support and may fall back to simpler window options.
- **Generated Buddy assets are optional** — bundled motion packs run locally with no model call; Hatch-generated Buddy art/motion requires the configured image/video generation providers and their normal quotas/rate limits.
- **Linux native capture dependencies are optional** — missing OpenCV/MSS native libraries should not block startup, but camera and screenshot tools remain unavailable until the relevant platform packages are installed.
- **Landing page update deferred** — `docs/index.html` is intentionally not updated in this release-prep pass; download links and website version text will be updated separately after the v3.21.0 release assets are published.

### Files Changed

| File | Change |
|------|--------|
| `buddy/`, `static/buddy/`, `ui/buddy.py` | Buddy event/config/runtime surfaces, bundled motion packs, in-app docked/undocked UI, and desktop overlay route/runtime assets |
| `ui/settings.py`, `ui/model_catalog.py`, `providers/selection.py`, `providers/catalog.py`, `providers/codex.py`, `models.py` | Settings -> Models stability, picker clarity, Codex Vision capability retention, and provider/model catalog refinements |
| `providers/transports/codex_responses.py`, `vision.py`, `tools/thoth_status_tool.py` | Codex multimodal image payload preservation, startup-safe Vision capture backends, and controlled Brain/Vision setting updates |
| `launcher.py`, `startup_diagnostics.py`, `requirements.txt`, `installer/thoth_setup.iss`, `installer/install_deps.bat` | Startup diagnostics, Linux readiness failure context, Linux native CPU-baseline packaging guard, Windows embedded-Python repair, and optional native package recovery hints |
| `installer/build_linux_app.sh`, `installer/install-linux.sh`, `build_linux_app.sh`, `.github/workflows/release.yml`, `.github/workflows/ci.yml` | Linux launcher symlink resolution, root build wrapper, installed launcher smoke, and release/CI packaging checks |
| `docs/RELEASING.md`, `installer/README.md`, `README.md`, `docs/ARCHITECTURE.md` | Release checklist, installer, architecture, and user-facing Linux/provider/model guidance updates |
| `tests/`, `pytest.ini` | Focused startup/Linux/provider/model-selection regressions, release-smoke coverage, moved test discovery, and installer exclusion guards |

---

## v3.20.0 — Linux Support, MiniMax, Custom Setup, Linux & Ollama Reliability

This release extends the provider runtime work with **MiniMax** as a first-class API-key provider, a cleaner first-run path for custom OpenAI-compatible endpoints, real Linux packaging, and stronger local Ollama connection handling for Windows and custom host setups.

### 🐧 Linux Support

- **Self-contained Linux tarball** — releases now include `Thoth-X.Y.Z-Linux-x86_64.tar.gz`, built with python-build-standalone and the same source-copy contract as the macOS app bundle
- **One-line Linux install** — `installer/install-linux.sh` lets users install with a single `curl ... | bash` command while still verifying the release tarball SHA256 before running the bundled installer
- **XDG user install** — `install.sh` installs under `~/.local/share/thoth/releases/<version>`, updates `~/.local/share/thoth/current`, creates `~/.local/bin/thoth`, and installs a freedesktop desktop entry plus icon
- **Browser-first baseline** — Linux opens in the system browser by default and does not require pywebview, GTK/Qt, AppIndicator, or tray libraries to run
- **Optional native/tray modes** — `launcher.py --native` and `launcher.py --tray` remain available for Linux desktops with the relevant system libraries
- **Server mode** — `launcher.py --server --no-open --port <port>` supports headless Linux smoke and server-style launches
- **Linux updater path** — the updater can select Linux tarball assets, verify the SHA256 release manifest, install into the user-owned release tree, flip the `current` symlink, and restart through `~/.local/bin/thoth`
- **Headless keyring handling** — WSL and server Linux environments without Secret Service/KWallet now treat secure storage as unavailable without traceback spam; new secrets remain session-only rather than falling back to plaintext files

### 🧠 Providers & Setup

- **MiniMax provider support** — MiniMax M2 models can be connected as a first-class API-key provider through MiniMax's Anthropic-compatible endpoint, with catalog rows, provider labels, setup/settings key entry, `MINIMAX_API_KEY` support, and runtime routing through the existing Anthropic transport
- **Anthropic-compatible transport cleanup** — MiniMax now uses the same consolidated system-message handling required by Anthropic-style Messages APIs, avoiding failures from multiple non-consecutive system messages
- **MiniMax key validation** — credentials accepted by MiniMax but blocked by the documented insufficient-balance response are treated as valid credentials with a billing/account warning instead of as invalid keys
- **Custom/Self-hosted setup path** — first-run setup now supports Custom/Self-hosted OpenAI-compatible endpoints such as LM Studio alongside the normal Providers path for API-key users

### 🖥️ Ollama & Native Launcher Reliability

- **Ollama host parsing** — `OLLAMA_HOST` values with explicit ports and URL forms are parsed correctly for local daemon checks instead of assuming the default `11434` port
- **Ollama wildcard-host compatibility** — when `OLLAMA_HOST` is set to a bind wildcard such as `0.0.0.0` or `::`, Thoth now connects through a loopback client endpoint while preserving the configured port, so setup, model listing, downloads, local chat, vision, and dream-cycle busy checks do not incorrectly report Ollama as disconnected
- **Local vision model catalog restore** — Ollama and Custom/Self-hosted OpenAI-compatible catalogs now infer vision support for local model families such as Gemma 3, LLaVA variants, Moondream, MiniCPM-V, and Qwen-VL, so LM Studio and installed Ollama vision models appear in the Vision tab again
- **Free-port launcher startup** — the desktop launcher now verifies that a listener on `8080` is actually Thoth before reusing it; if another local service owns the port, Thoth starts on the next available local port instead of opening the foreign service
- **Session port source of truth** — the launcher passes the selected port through `THOTH_PORT`, and the NiceGUI app, main-app tunnel, SMS webhook registration, workflow webhook route, Settings tunnel toggle, and Designer published-link fallback all use that active app port
- **Launcher identity probe** — `/api/launcher-ping` lets the tray distinguish an existing Thoth instance from unrelated services while preserving direct `python app.py` launches on port `8080` by default
- **Linux-safe launcher modes** — the launcher now has explicit `--browser`, `--native`, `--tray`, `--no-tray`, `--server`, `--no-open`, `--port`, and `--host` flags; Windows and macOS keep their existing tray-first behavior while Linux defaults to browser/no-tray
- **Wayland clipboard fallback** — native-window clipboard access tries `wl-paste` before the existing `xclip` fallback on Linux

### 🧪 Tests & Release Checks

- **MiniMax provider coverage** — focused tests cover provider catalog wiring, runtime construction, key validation behavior, setup/settings surfaces, static model rows, and Anthropic-compatible message consolidation
- **Ollama endpoint regressions** — provider runtime tests cover `OLLAMA_HOST` variants including custom ports, URL forms, `0.0.0.0`, and IPv6 wildcard binds
- **Vision catalog regressions** — provider catalog tests cover installed/recommended Ollama vision rows plus LM Studio-style custom endpoint models with sparse OpenAI-compatible metadata
- **Launcher/app-port coverage** — app-port tests validate dynamic port selection, Thoth identity probing, and active-port propagation
- **Linux smoke coverage** — Ubuntu CI now launches the app and checks `/api/launcher-ping`; release CI builds the Linux tarball, unpacks it, runs the packaged launcher in server mode, and checks both `/api/launcher-ping` and the root UI page
- **Current validation** — focused Linux/app-port/secret-storage regression tests pass locally; full `test_suite.py` and `pytest -q` remain final release-gate checks before publishing artifacts

### ⚠️ Release Notes & Risk Notes

- **LM Studio custom endpoint smoke** — when testing LM Studio through the Custom/Self-hosted setup path, load the selected model with enough context for Thoth's agent prompt and enabled tool schemas. A `4096` context can fail with a misleading prompt-template error such as `No user query found in messages`; `32768` is a practical smoke-test baseline.

### 📁 Files Changed

| File | Change |
|------|--------|
| `models.py` | MiniMax static catalog rows, normalized Ollama endpoint handling, explicit Ollama client/base URL routing, local model listing/download/tool checks, and context lookup fixes |
| `providers/catalog.py`, `providers/auth_store.py`, `providers/runtime.py`, `providers/ollama.py` | MiniMax provider definition, `MINIMAX_API_KEY` mapping, Anthropic-compatible runtime routing, normalized Ollama runtime base URL construction, and local/custom vision catalog inference |
| `ui/setup_wizard.py`, `ui/settings.py` | MiniMax key entry plus Custom/Self-hosted setup and settings alignment |
| `vision.py`, `dream_cycle.py` | Local Ollama vision and busy-check calls now use the normalized client endpoint |
| `app_port.py`, `launcher.py`, `app.py` | Dynamic app-port selection, `THOTH_PORT` propagation, Thoth identity probing, and active-port NiceGUI startup |
| `installer/build_linux_app.sh`, `installer/install-linux.sh`, `.github/workflows/release.yml`, `.github/workflows/update-manifest.yml` | Linux tarball packaging, one-line installer bootstrap, release artifact upload, packaged smoke, and SHA256 manifest inclusion |
| `channels/sms.py`, `designer/publish.py`, `ui/settings.py` | Main-app tunnel, SMS webhook, Designer published-link, and Settings tunnel controls now follow the active app port |
| `test_provider_*.py`, `test_app_port.py`, `test_linux_support.py`, `test_suite.py` | MiniMax, custom setup, Ollama endpoint, Linux packaging/updater/launcher, and app-port regression coverage |

---

## v3.19.0 — Provider Runtime Foundation & ChatGPT / Codex

Thoth's model layer has been rebuilt around a first-class **provider runtime**. API-key providers, local Ollama models, custom OpenAI-compatible endpoints, media providers, and ChatGPT / Codex subscription access now flow through one provider-aware catalog and picker system instead of a mix of legacy cloud lists, starred models, and per-screen dropdown logic.

This release also adds **ChatGPT / Codex** as a distinct subscription-backed provider. It is intentionally separate from OpenAI API-key access: Codex uses an in-app ChatGPT sign-in, keeps Thoth-owned tokens in the OS credential store, and labels duplicate model names as `OpenAI API` versus `ChatGPT / Codex` so users always know which route they are using.

### 🧠 Provider Runtime Foundation

- **New `providers/` subsystem** — provider definitions, metadata-only config, keyring-backed provider secrets, catalog normalization, runtime construction, status summaries, error normalization, Quick Choices, custom endpoint support, and routing-profile foundations now live in one dedicated package
- **Provider runtime facade** — OpenAI, OpenRouter, Anthropic, Google AI, xAI, custom OpenAI-compatible endpoints, Ollama catalog rows, and ChatGPT / Codex all route through a shared runtime layer while preserving the public `models.py` compatibility API
- **Stable model refs** — provider-backed picker values use refs such as `model:openai:gpt-5.5` and `model:codex:gpt-5.5`, keeping identical raw model IDs distinct across providers
- **Provider-aware labels** — duplicate model names now show route labels such as `GPT-5.5 — OpenAI API` and `GPT-5.5 — ChatGPT / Codex` in chat, Designer, workflow, status, and settings pickers
- **Metadata-only provider config** — `providers.json` stores provider state, Quick Choices, catalog cache, fingerprints, and status metadata; raw API keys and OAuth tokens stay in the OS credential store when available
- **Status and insight awareness** — Thoth Status now exposes provider-aware model/runtime context and an `insights` category, while Dream Cycle Phase 5 includes model/provider/media context in its system snapshot before generating actionable insights
- **Custom endpoint foundation** — custom OpenAI-compatible endpoints can be saved, refreshed, and surfaced as provider catalog rows without overloading the built-in OpenAI provider

### ⚙️ Settings → Providers & Settings → Models

- **Providers tab cleanup** — the old Cloud surface is now **Providers**. It focuses on provider connection state, API keys, ChatGPT / Codex sign-in, health, refresh, setup guidance, and custom endpoint management
- **Models tab ownership** — model browsing, raw provider catalogs, local Ollama catalog rows, pin/unpin actions, defaults, and Quick Choices now live in **Settings → Models**
- **Consolidated Model Catalog** — a category-first catalog groups Brain, Vision, Image, and Video-capable rows by provider, with inline actions for pinning, setting defaults, downloading local models, and clearing disabled reasons
- **Polished Defaults panel** — Brain, Vision, Image, and Video defaults use compact provider/local badges, context controls, enable switches, and empty states that point users to the catalog instead of scattering model controls across tabs
- **First-run setup alignment** — setup now offers migration before model setup, supports the Providers path for API-key users, and points users to Settings → Models for exact model pinning after launch

### 💬 Picker Unification

- **One picker source** — chat header overrides, live chat model override, background workflow model override, Designer inline model selection, Telegram `/model`, and Thoth Status model updates all use the same provider-aware Quick Choice helpers
- **Surface-specific choices** — Brain, Vision, Image, and Video surfaces filter models by capability so media-only models do not leak into normal chat and chat-only models do not appear as image/video options
- **Legacy compatibility** — existing starred cloud models and bare model IDs are migrated or resolved without breaking saved settings, while new provider-backed selections preserve their provider route
- **Runtime banner cleanup** — chat status now uses dynamic provider display labels, so custom providers and ChatGPT / Codex show accurate route names instead of hardcoded cloud labels

### 🔐 ChatGPT / Codex Subscription Provider

- **In-app ChatGPT sign-in** — direct Codex runtime requires Thoth's device-flow ChatGPT sign-in and stores Thoth-owned OAuth tokens in the OS credential store
- **CLI auth boundary** — external Codex CLI auth files are display-safe metadata/reference hints only. Thoth can show that a CLI login exists, but it does not copy runnable tokens from `~/.codex/auth.json`
- **Live Codex catalog** — ChatGPT / Codex catalog discovery uses `https://chatgpt.com/backend-api/codex/models?client_version=1.0.0` when OAuth runtime credentials are present, caches display-safe metadata, filters hidden/internal rows, and falls back to documented subscription models when live discovery is unavailable
- **Responses transport** — `ChatCodexResponses` handles the ChatGPT/Codex Responses SSE backend, bearer/account headers, streaming text, function-call chunks, tool-call replay, and 401 refresh retry behavior
- **Tool-call parity** — Codex streaming now emits LangChain tool-call chunks, so normal chat can execute tools instead of ending with empty assistant messages when Codex asks for workspace/tool context
- **Current-turn fallback** — checkpoint fallback only uses an AI answer from the current submitted turn, preventing stale prior assistant text from being replayed after an empty streaming turn

### 🖼️ Media Providers & Model Catalog

- **Image/video model routing** — image generation and video generation models participate in provider-aware selection, catalog pinning, and surface filtering
- **Provider media status** — Thoth Status and Models settings can report media provider availability and selected image/video models without treating media rows as Brain models
- **Ollama catalog parity** — downloadable Ollama rows appear as non-runnable catalog entries until installed, with local download actions in the Models catalog
- **Vision reuse** — provider models with image capability can be detected and reused by the Vision feature alongside local vision models

### 🎨 Designer & Streaming Reliability

- **Detached stream cleanup** — long Designer/browser sessions now clear terminal active-generation bookkeeping when the graph finishes, even if the browser client disconnects during streaming
- **Final-response hydration** — detached completions reload active thread messages from LangGraph checkpoints before rebuilding the UI, so final assistant prose appears after reconnect instead of being hidden behind stale in-memory state
- **Stored HTML normalization** — Designer project HTML no longer persists render-time `data:image/...base64` payloads; stored projects keep canonical `asset://...` references while preview/export resolves assets at render time
- **Preview timer cleanup** — Designer preview polling timers deactivate on client disconnect or deleted-parent errors instead of continuing to touch removed NiceGUI clients
- **Stale-run recovery** — sending a new Designer/chat message can drop stale terminal generation entries while still blocking truly live runs

### 💻 Claude Code Delegation Skill

- **New bundled skill** — `bundled_skills/claude_code_delegation/SKILL.md` teaches Thoth how to coordinate Claude Code CLI as an external coding worker for implementation, review, refactor, and larger repository tasks
- **Thoth remains coordinator** — the skill keeps Thoth responsible for scoping the request, checking local state, choosing the narrowest Claude Code tool permissions, inspecting diffs, running verification, and explaining results to the user
- **Approval-gated shell workflow** — Claude Code runs through Thoth's shell workflow with explicit working-directory checks, bounded print-mode commands, `--allowedTools`, `--max-turns`, optional budget limits, and no permission bypass unless the user explicitly asks
- **Secret and safety boundaries** — the skill warns not to forward API keys, Thoth memory, private notes, or sensitive user data to Claude Code unless explicitly requested, and it forbids destructive git, deploy, production migration, and secret-handling delegation without clear user approval
- **Interactive mode guidance** — print mode is preferred; interactive/tmux-style Claude Code orchestration is documented as advanced and best suited to macOS/Linux/WSL2 with explicit cleanup

### 🧪 Tests & Release Checks

- **Focused provider suites** — new provider tests cover config normalization/masking, keyring namespace storage and chunking, provider catalog inference, model selection refs, media model filtering, custom endpoints, runtime construction, and ChatGPT / Codex OAuth/catalog/transport behavior
- **Bundled skill coverage** — the main suite validates `claude_code_delegation` as a bundled skill and checks the skill parser/discovery path that loads it
- **Designer regressions** — `test_suite.py` covers detached finalization cleanup, stale terminal generation recovery, deleted-client detach detection, Designer asset canonicalization, preview timer cleanup, and checkpoint hydration for detached final answers
- **Release smoke** — `test_suite.py` validates v3.19.0 version consistency across `version.py`, Windows installer, macOS app plist, CI release workflow, bug report template, and install dependencies
- **Packaging smoke** — Windows installer coverage includes recursive `providers/` plus `ui/model_catalog.py` and `ui/provider_settings.py`; macOS app packaging includes `providers` and the full `ui` package
- **Clean first-run smoke** — a temporary `THOTH_DATA_DIR` import/config check confirms setup wizard and provider config load cleanly before any provider state exists
- **Final validation** — direct `test_suite.py` passes with the release-smoke checks, and full `pytest -q` passes with `159 passed, 1 skipped`

### ⚠️ Release Notes & Risk Notes

- **Codex runtime sign-in** — ChatGPT / Codex models only run after an in-app ChatGPT sign-in stores Thoth-owned OAuth tokens in the local OS credential store
- **Subscription backend risk** — ChatGPT / Codex uses ChatGPT's subscription/internal Codex backend rather than the public OpenAI API. The endpoint, catalog shape, auth requirements, rate limits, and model availability may change upstream without the same stability guarantees as the public API
- **Privacy** — when a ChatGPT / Codex model is selected, the current conversation plus model-visible tool context and tool results are sent to ChatGPT / Codex for that turn. Durable Thoth data such as memories, documents, files, and other conversations stay local unless explicitly included in the active conversation or exposed through a tool result
- **Manual smoke still required** — before publishing installers, run clean-machine Windows/macOS smoke for first launch, Settings → Providers, Settings → Models catalog/pinning/defaults, ChatGPT / Codex sign-in/status, shared model pickers, and a long Designer/browser task with reconnect

### 📁 Files Changed

| File | Change |
|------|--------|
| **`providers/`** | **New** — provider definitions, config, auth store, catalog normalization, runtime facade, status summaries, Quick Choices, custom endpoints, media helpers, Ollama catalog integration, Codex OAuth/catalog/runtime support, and transport adapters |
| **`providers/transports/codex_responses.py`** | **New** — ChatGPT / Codex Responses transport with SSE streaming, tool-call chunks, tool-call replay, and auth-refresh retry support |
| **`ui/provider_settings.py`** | **New** — Settings → Providers connection, credential, ChatGPT sign-in, health, refresh, setup, and custom endpoint UI |
| **`ui/model_catalog.py`** | **New** — consolidated Settings → Models catalog UI for provider/local rows, pinning, defaults, downloads, and surface filtering |
| `models.py` | Provider-aware model refs, runtime/provider/context resolution, Quick Choice compatibility, legacy selection handling, and local/provider facade updates |
| `ui/settings.py` | Providers/Models split, polished model defaults panel, catalog embedding, media defaults, and provider-aware picker wiring |
| `ui/chat.py`, `ui/chat_components.py`, `ui/task_dialog.py` | Shared provider-aware model picker options and dynamic provider labels for chat, Designer, and workflow/background overrides |
| `channels/telegram.py` | `/model` command uses provider Quick Choices instead of legacy starred cloud models |
| `tools/image_gen_tool.py`, `tools/video_gen_tool.py`, `tools/thoth_status_tool.py` | Media model provider selection, image/video status reporting, and model-setting updates through shared provider selection helpers |
| `tool_guides/thoth_status_guide/SKILL.md`, `bundled_skills/self_reflection/SKILL.md`, `dream_cycle.py`, `insights.py` | Status guide, self-reflection, and Dream Cycle insight snapshot alignment with provider runtime, media defaults, and active insight status |
| `agent.py` | Current-turn-only checkpoint fallback for empty streaming turns so stale prior answers are not replayed |
| `bundled_skills/claude_code_delegation/SKILL.md` | **New** — approval-gated Claude Code CLI delegation workflow for coding, review, and refactor tasks |
| `designer/editor.py`, `designer/preview.py`, `ui/streaming.py` | Designer asset canonicalization, deleted-client detection, detached completion hydration, active-generation cleanup, and preview timer disconnect handling |
| `ui/setup_wizard.py` | Provider path copy and Quick Choice seeding aligned with Settings → Models ownership |
| `installer/thoth_setup.iss`, `installer/build_mac_app.sh`, `installer/README.md` | Provider runtime/UI packaging, v3.19.0 installer docs, clean first-run and Codex credential-boundary notes |
| `README.md`, `docs/ARCHITECTURE.md`, `docs/RELEASING.md`, `docs/index.html` | User-facing provider/Codex docs, architecture notes, release checklist updates, and v3.19.0 download/version references |
| `test_provider_*.py`, `test_thoth_status_media.py`, `test_suite.py`, `pytest.ini`, `scripts/dummy_openai_endpoint.py` | Focused provider/media/runtime/Codex tests, release smoke checks, pytest ignore config, and local OpenAI-compatible dummy endpoint for manual custom-provider testing |

---

## v3.18.0 — External MCP Tools, Migration Wizard & Secure API Keys

Thoth now has a full **Model Context Protocol client** for connecting external MCP servers as native dynamic tools without letting a broken server take down the app. This release also adds a guarded **Hermes/OpenClaw migration wizard** in Preferences, moves normal core and plugin API-key saves into the OS credential store, and fixes a cloud-model default regression where a saved GPT/Claude/Gemini/Grok/OpenRouter model could be replaced by a local Ollama fallback when the cloud-model cache was empty.

The MCP runtime supports stdio, Streamable HTTP, and SSE transports; handles tool, resource, and prompt surfaces; classifies destructive tools; routes risky actions through Thoth's existing interrupt approvals; and keeps all external server config isolated in `mcp_servers.json`. Marketplace search can pull from curated starters plus MCP directories, while dependency handling covers common user-space runtimes such as Node.js, uv, and Playwright Chromium, leaving heavier requirements like Docker as clear manual setup tasks.

### 🔌 MCP Client & Dynamic Tools

- **New `mcp_client/` subsystem** — persistent config, runtime sessions, result normalization, safety classification, marketplace discovery, dependency checks, and structured diagnostics live under a dedicated package instead of being mixed into built-in tool code
- **Native parent tool** — new `tools/mcp_tool.py` registers **External MCP Tools** as the parent toggle; actual MCP server tools are injected dynamically through `as_langchain_tools()` after discovery
- **Namespaced tool wrappers** — MCP tools are exposed as `mcp_<server>_<tool>` so external tool names cannot collide with native tools or each other
- **Transport support** — stdio, Streamable HTTP, and SSE servers are supported through the Python MCP SDK, with per-server connect timeout, tool timeout, output cap, environment, headers, working directory, and command/URL settings
- **Resources and prompts** — MCP resources and prompts can be exposed as optional utility tools per server, separately from the server's normal tool list
- **Model-facing output normalization** — text, structured content, embedded resources, links, image/binary blocks, errors, empty responses, and oversized outputs are normalized before they reach the LLM

### 🛡️ Safety, Permissions & Fault Isolation

- **Global kill switch** — disabling MCP stops active sessions, clears the runtime catalog, removes dynamic MCP tools from the agent, and keeps the saved server configuration for later re-enable
- **Per-server and per-tool toggles** — users can enable the MCP client globally, then choose exactly which servers and tools are active
- **Destructive-tool gates** — tools whose names, descriptions, or MCP annotations indicate write/send/delete/run/deploy/payment-style behavior require approval; destructive tools are not auto-enabled after discovery
- **Native capability overlap labels** — servers that overlap built-in Thoth memory, browser, filesystem/document, web search, channel, or Designer capabilities are labeled and require manual tool selection
- **Untrusted external output handling** — MCP guide instructions tell the agent to treat MCP results as untrusted external content and prefer native Thoth tools for Thoth-owned capabilities
- **Startup-safe design** — missing SDKs, bad JSON config, missing commands, failed child processes, broken endpoints, and server connection failures degrade to status rows and logs instead of blocking Thoth startup
- **Shutdown cleanup** — app shutdown now closes MCP child sessions so external stdio processes are not left behind

### 🧭 Settings UI, Import & Marketplace Search

- **Settings → MCP tab** — add/edit/import/test/refresh/delete MCP servers from the GUI with the same simple enable-checkbox pattern as built-in tools
- **Tool review surface** — after a successful test, discovered tools show descriptions, input schema summaries, destructive/approval badges, enable checkboxes, and approval toggles for non-destructive tools
- **Import disabled by default** — JSON imports and marketplace entries are saved disabled until tested and reviewed
- **Curated starter catalog** — recommended entries cover common external MCP use cases while preserving risk, trust, overlap, and requirement metadata
- **Directory search** — marketplace search can use official registry-style sources plus PulseMCP, Smithery, and Glama adapters with cache/curated fallback when live sources are unavailable or irrelevant
- **Diagnostics dialog** — masked config plus live runtime status are available from the MCP settings surface for troubleshooting without exposing secrets

### 🔄 Hermes & OpenClaw Migration Wizard

- **Preferences-launched wizard** — Settings → Preferences now exposes an **Open Migration Wizard** action instead of a permanent top-level migration tab, keeping this one-time setup flow out of the main settings sidebar
- **Supported sources** — detects and plans imports from Hermes Agent (`.hermes`) and OpenClaw (`.openclaw`, legacy `.clawdbot` / `.moltbot`) with provider-mismatch guards so the wrong source type does not produce a misleading partial plan
- **Preview-first flow** — scan builds a dry-run plan only; apply requires explicit review and writes only the currently selected items
- **Mapped data** — imports identity/persona files, memory files, daily OpenClaw memory, skills, model/provider settings, disabled MCP server definitions, and opt-in API keys/tokens
- **Manual-review boundaries** — channels, approvals, browser, cron, hooks, tools, broad runtime state, and unknown/risky source data stay skipped or archive-only instead of being activated live
- **Backups and reports** — existing targets are backed up before replacement, repeated writes to the same target preserve the original once per run, and every apply writes `plan.json`, `result.json`, `backup_manifest.json`, and `summary.md`
- **Secret redaction** — migration reports redact secret-shaped values and archive snapshots redact JSON/key-value files; API key import remains an explicit opt-in
- **MCP safety** — migrated MCP server definitions stay disabled until reviewed, so a bad imported server cannot break startup or automatically expose risky external tools

### 🔐 API Key Secure Storage

- **OS credential store** — saved core and plugin API keys now use the platform keyring through `keyring` instead of normal plaintext JSON storage
- **Metadata-only file** — `~/.thoth/api_keys.json` stores saved-state, keyring service, timestamps, and masked fingerprints, not raw API key values
- **Plugin secret parity** — plugin-declared API keys use the same keyring-backed path with metadata-only `plugin_secrets.json` state
- **Legacy migration** — existing plaintext `api_keys.json` files are imported into the keyring on load; if the OS keyring is unavailable, Thoth keeps legacy keys readable with a warning instead of crashing startup
- **No silent plaintext fallback** — new saves during keyring failure become session-only rather than creating new plaintext API-key files
- **Safer Settings UI** — saved keys are not prefilled into password fields; blank inputs leave existing keys unchanged and clear actions are explicit
- **Migration integration** — selected Hermes/OpenClaw API keys route through target-profile secure storage and migration reports remain redacted

### 🧠 Cloud Model Defaults

- **Cache-empty provider inference** — GPT, Claude, Gemini, Grok, and slash-style OpenRouter model IDs are recognized as cloud models even before the provider cache has been refreshed
- **Default preservation** — `refresh_cloud_models()` no longer rewrites a saved cloud default to a local Ollama fallback simply because keys, network access, or provider discovery are temporarily unavailable
- **Regression coverage** — `test_suite.py` now checks provider inference, cache-empty cloud detection, and preservation of saved cloud defaults such as `gpt-5.5`

### ⚙️ Runtime Requirements

- **Requirement detection** — stdio servers infer required launchers from commands such as `npx`, `uvx`, and `docker`, plus Playwright browser requirements for Playwright MCP
- **Managed easy installs** — Thoth can install private user-space Node.js LTS, uv, and Playwright Chromium runtimes under `~/.thoth/runtimes/` without packaging those runtimes inside Thoth
- **Manual complex installs** — non-trivial system dependencies such as Docker are surfaced with setup guidance instead of being bundled or silently installed
- **Managed environment injection** — resolved runtime paths are added only to the MCP child process environment, avoiding global PATH mutation

### 🧠 Agent, Status & Guide Integration

- **Tool display names** — dynamic MCP calls render with readable labels such as `MCP: microsoft_docs_search (microsoft-learn-mcp)` in tool-call UI
- **Browser-loop controls** — MCP browser tools participate in Thoth's browser snapshot trimming and loop-control logic so long browsing runs do not flood context
- **Background workflow safety** — destructive MCP tools respect the workflow safety mode: approval-required modes interrupt, while explicit allow-all mode can run enabled destructive MCP tools
- **Thoth Status integration** — `thoth_update_setting` can enable/disable the global MCP client through the normal tool-toggle path, keeping the parent registry tool and runtime state synchronized
- **New MCP tool guide** — `tool_guides/mcp_guide/SKILL.md` documents when to use external MCP tools, how to treat MCP output, how to handle MCP errors, and how global disable behaves

### 🧪 Tests & Release Checks

- **Focused offline suite** — new `test_mcp_client.py` covers bad config fallback, secret masking, destructive detection, marketplace fallback/filtering, conflict policy, runtime requirement inference, managed runtime env injection, settings rows, stdio discovery/call, tool enable/approval toggles, global MCP disable, bad server failure, display names, background safety, and MCP browser loop controls
- **Opt-in live suite** — new `test_mcp_real_world_e2e.py` plus `scripts/mcp_real_world_e2e.py` validate real public MCP servers outside normal CI, including Microsoft Learn and Context7
- **Main regression coverage** — `test_suite.py` includes MCP modules in import/consistency checks and validates the focused MCP test files are part of the tracked suite
- **Migration regression suite** — new `test_migration_core.py`, `test_migration_detection.py`, `test_migration_planner.py`, `test_migration_apply.py`, and `test_migration_wizard_ui.py` cover source detection, wrong-provider guards, dry-run planning, conflict behavior, backup/report generation, redaction, daily memory import, UI helper behavior, and Preferences placement
- **API key storage suite** — new `test_api_key_storage.py` covers keyring-backed writes, metadata-only files, legacy plaintext migration, keyring-unavailable fallback, session-only new saves, and delete behavior

### 📁 Files Changed

| File | Change |
|------|--------|
| **`mcp_client/`** | **New** — isolated MCP client package for config, runtime sessions, marketplace search, requirement handling, safety classification, logging, conflicts, result normalization, and curated starters |
| **`tools/mcp_tool.py`** | **New** — parent External MCP Tools registry entry that injects dynamic MCP LangChain tools |
| **`ui/mcp_settings.py`** | **New** — Settings → MCP tab with add/import/browse/test/refresh/delete flows, requirement install buttons, diagnostics, and per-tool enable/approval controls |
| **`tool_guides/mcp_guide/SKILL.md`** | **New** — agent guidance for safe use of external MCP tools and global MCP disable semantics |
| **`test_mcp_client.py`** | **New** — offline MCP regression suite focused on robustness and failure isolation |
| **`test_mcp_real_world_e2e.py`** | **New** — opt-in unittest wrapper for live public MCP checks |
| **`scripts/mcp_real_world_e2e.py`** | **New** — maintainer release check for real MCP endpoints and dynamic wrapper invocation |
| **`migration/`** | **New** — pure models, redaction, source detection, realistic fixtures, dry-run planners, and guarded apply/report engine for Hermes/OpenClaw migrations |
| **`ui/migration_wizard.py`** | **New** — Preferences-launched scan/review/apply wizard with category summaries, selection controls, conflict handling, and report path display |
| **`test_migration_*.py`** | **New** — focused migration coverage for models, detection, planners, apply/report behavior, and UI helpers |
| **`secret_store.py`** | **New** — small platform-keyring wrapper with data-directory-scoped service names and testable backend hooks |
| **`test_api_key_storage.py`** | **New** — focused API key storage regression suite for keyring, legacy migration, metadata redaction, and fallback behavior |
| `agent.py` | Treats MCP tool output as untrusted, resolves readable MCP tool labels, and applies browser-loop handling to MCP browser tools |
| `app.py` | Starts MCP discovery non-fatally during startup and closes MCP sessions during shutdown |
| `api_keys.py` | Moves normal saved API keys to secure keyring storage, keeps compatibility helpers, migrates legacy plaintext, and supports migration imports into target data directories |
| `plugins/state.py` | Moves plugin-declared API-key secrets to the same keyring-backed storage model with metadata-only local state and session-only fallback for new saves when keyring is unavailable |
| `plugins/ui_plugin_dialog.py` | Stops prefilling saved plugin secrets, shows configured state, and adds explicit clear controls |
| `models.py` | Infers common cloud model providers without relying on a populated cache and preserves saved cloud defaults during refresh failures/cache misses |
| `ui/settings.py` | Adds the Preferences migration launcher while preserving old `Migration` deep-link routing to Preferences; key inputs now show masked saved-state instead of prefilled secrets |
| `tools/thoth_status_tool.py` | Synchronizes the `mcp` tool toggle with the global MCP client switch |
| `tool_guides/thoth_status_guide/SKILL.md` | Documents MCP global toggle behavior through Thoth Status |
| `test_suite.py` | Adds model-default regression checks for cloud provider inference and refresh preservation |
| `requirements.txt` | Adds the Python MCP SDK, LangChain MCP adapter dependencies, and `keyring` |
| `installer/thoth_setup.iss` | Bundles the new MCP client package, MCP settings UI, MCP parent tool, guide, migration package/UI, and secure secret-store helper |

## v3.17.0 — Designer Studio II: Interactive Modes, Video Gen & Review Flow

Designer graduates from a single-mode deck authoring tool into a full multi-mode design studio. Five project modes now ship — **deck**, **document**, **landing page**, **app mockup**, and **storyboard** — each with its own canvas rules, prompt guardrails, and curated template gallery. Interactive modes (landing / app_mockup / storyboard) run on a new sandboxed **runtime bridge** that turns declarative `data-row-bot-action` attributes into real in-preview navigation, state toggles, and media playback without letting the agent write free-form `<script>`. A new **video generation tool** joins image generation as a first-class asset producer. A surgical tool surface — move, duplicate, restyle, refine-text, add-chart, insert-component, critique-page, apply-repairs — lets the agent edit pages without rewriting HTML. A new **review dialog** and **mutation diff** show exactly what changed turn over turn. The agent is bound by mode-specific **content budgets** and a **post-critique repair loop** so pages no longer clip at the canvas edge. Editable PPTX export is rewritten around isolated-page rasters so overlapping text no longer bleeds between slides. Designer assets now live under a **shared `utils/`** layer, and the home UI picks up bulk-select, skeleton loading, confirm dialogs, and richer sidebar + status bar states. Outside Designer, Thoth ships its own **in-app auto-updater** — packaged Windows and macOS builds now check GitHub Releases in the background, surface an `⬆ vX.Y.Z` pill in the status bar, and install SHA256- and code-signature-verified updates without leaving the app.

### 🎨 Designer — Five Modes & Interactive Runtime

Designer is no longer deck-only. Each mode has dedicated canvas semantics, prompt guidance, template gallery, and preview behaviour.

- **Five project modes** — `deck`, `document`, `landing`, `app_mockup`, `storyboard` with mode-aware canvas rules (fixed-slide vs. scrollable vs. device viewport) and mode-aware prompt injection
- **Interactive runtime bridge** — new `designer/runtime/` ships a sandboxed JS + CSS bridge loaded into preview, export, and published output; the agent uses declarative `data-row-bot-action="navigate:…"`, `toggle_state:…`, and `play_media:…` attributes instead of `<script>` tags or `on*` handlers
- **Multi-route / multi-screen projects** — `designer/page_navigator.py`, `designer/route_graph.py`, and `designer/interaction.py` let landing, app_mockup, and storyboard projects chain screens with real transitions (`fade`, `slide_left`, `slide_up`) and state-scoped overlays
- **Hotspot recorder** — new `designer/hotspot_recorder.py` turns a click on any preview element into a wired navigate or toggle action without hand-editing HTML
- **Command palette** — new `designer/command_palette.py` gives keyboard-driven access to every designer tool
- **Zero-state quick-starts** — new `designer/zero_state.py` shows per-mode starter chips when a project is empty; the first-draft CTA and quick-start panel now dismiss themselves as soon as the user commits
- **Template gallery overhaul** — `designer/template_gallery.py` and `designer/templates.py` add curated starters for every mode (pitch deck, brief, landing hero, three-route app scaffold, four-shot storyboard, SaaS dashboard, and more), upload-as-template, brand preset selection, and attached-file persistence through the setup flow

### 🖼️ Media, Image Placement & Video Generation

Designer media becomes more structured and more correct — agent-generated images land in the right containers, and video is a first-class asset type.

- **Typed image slots + 5-path resolver** — `designer/ai_content.py` now fills images through (1) `data-row-bot-image-slot` / `data-row-bot-shot-visual` typed slots, (2) `position="replace:SELECTOR"` targeting, (3) heuristic empty-placeholder detection covering hero/product/photo/recipe/screen/phone/card visuals, (4) blank `<img>` replacement, and (5) an overlay fallback as a last resort
- **Correct cover sizing** — when a wrapped image fragment drops into a slot, `object-fit:cover` is applied to the actual `<img>`/`<video>` element instead of an outer `<div>` where the property has no effect; stale width/height/margin from authored placeholders is stripped
- **Video generation tool** — new `tools/video_gen_tool.py` plus `tool_guides/video_guide/` give Thoth a first-class video-generation surface with provider routing and documented guidance
- **Editable PPTX raster isolation** — `designer/export.py` opens a fresh Playwright page per slide raster, captures `src`/`objectFit`/`objectPosition` for `<img>` and `svgOuterHTML` for inline `<svg>`, and renders at `device_scale_factor=2` so structured PPTX exports match the preview without text bleeding between slides
- **Shared media helpers** — new `utils/media.py` and `utils/text.py` centralise asset normalisation used by Designer, export, and channels

### 🧠 Agent Authoring Guardrails & Critique Loop

Mode-specific content budgets and a mandatory repair loop stop the agent from shipping clipped, overlapping, or cramped pages.

- **Content budgets per mode** — `designer/prompt.py` now enforces explicit per-page limits: document (130–160 words, ≥32–48 px bottom padding), deck (≤5 bullets, heading ≤4.5rem, 64–96 px edges), storyboard (one eyebrow + heading + paragraph + ≤2 metadata cards + direction + footer strip), landing (responsive sections with real padding), app_mockup (fixed device viewport with status/tab chrome)
- **Authoring rules** — explicit guidance forbids decorative CSS art stacked on top of heading text, requires horizontal button rows with distinct ghost/outline secondaries, and mandates typed image slots over absolute-positioned overlays
- **Post-critique repair loop** — after major rewrites the agent must call `designer_critique_page` and then either `designer_apply_repairs(["overflow"])` or split content to an additional page via `designer_add_page`; no mode ships a clipped page
- **Expanded overflow detection** — `designer/critique.py` now flags card-heavy pages (≥7 card-like `<div>`s) in addition to section-heavy pages, catching storyboards and dashboards the old heuristic missed
- **Brand lint** — new `designer/brand_lint.py` catches hardcoded colours and fonts before they leave the tool
- **Mutation diff + review dialog** — new `designer/mutation_diff.py`, `designer/review.py`, and `designer/review_dialog.py` let the user inspect exactly what the agent changed, page by page, turn over turn

### 🧰 New Designer Tooling

The agent gets a surgical tool surface so it can make targeted edits instead of rewriting full-page HTML.

- **Critique + repair** — `designer_critique_page` reports hierarchy / overflow / contrast / readability / spacing findings; `designer_apply_repairs` applies deterministic fixes for selected categories
- **Curated blocks** — `designer_insert_component` inserts hero callouts, stats bands, testimonials, pricing cards, and timeline steps from a shared component library
- **Surgical element edits** — `designer_move_image`, `designer_replace_image`, `designer_move_element`, `designer_duplicate_element`, `designer_restyle_element`, and `designer_refine_text` preserve existing layout and assets
- **Interactive mode tools** — `designer_add_screen`, `designer_link_screens`, `designer_set_interaction`, `designer_preview_screen`, `designer_reorder_routes`, and `designer_set_mode` drive multi-route editing through the runtime bridge
- **Share, publish, resize** — `designer_publish_link`, `designer_resize_project`, `designer_share_link`, and QR helpers in new `designer/qr_utils.py` round out the share-and-ship path

### 💬 UI & Workflow Updates

The home surface and chat stack pick up quality-of-life improvements shared by Designer and the main chat.

- **Bulk select + confirm dialogs** — new `ui/bulk_select.py` and `ui/confirm.py` give every list-based surface batch actions with a consistent confirm flow
- **Skeleton loading** — new `ui/skeleton.py` renders placeholder blocks while Designer gallery cards, threads, and published links hydrate
- **Timer utilities** — new `ui/timer_utils.py` standardises polling and debounce patterns used across the home tab and designer editor
- **Sidebar + command center** — `ui/sidebar.py` adds richer thread controls, batch operations, and pinned-thread affordances; `ui/command_center.py` tightens the insights panel
- **Chat + streaming refresh** — `ui/chat.py`, `ui/streaming.py`, and `ui/render.py` polish the streaming message area, attachment handling, and tool-call rendering
- **Home tab stability** — `ui/home.py` now polls for the chat container mount before dispatching an initial build so first-draft messages no longer race the UI

### 🛰️ Channels

All four messaging adapters pick up attachment and stability fixes introduced alongside the video generation tool.

- **Shared media capture** — new `channels/media_capture.py` centralises image/audio/document handling reused by Discord, Slack, Telegram, and WhatsApp
- **Discord** — voice-warning suppression and richer attachment flows in `channels/discord_channel.py`
- **Slack / Telegram / WhatsApp** — consistent media attachment, streaming-edit, and link-preview handling across `channels/slack.py`, `channels/telegram.py`, `channels/whatsapp.py`, and the WhatsApp `channels/whatsapp_bridge/bridge.js`

### 🔧 Tools & Status

- **Video generation tool** — new `tools/video_gen_tool.py` with matching `tool_guides/video_guide/`
- **Thoth Status** — `tools/thoth_status_tool.py` adds double-gated model normalisation, video-generation status, and syncs with the updated tool guide in `tool_guides/thoth_status_guide/SKILL.md`
- **Browser tool** — `tools/browser_tool.py` stability fixes
- **X tool** — `tools/x_tool.py` adds OAuth rate-limit-aware health checks
- **Registry** — `tools/__init__.py` and `tools/registry.py` register the video tool alongside existing surfaces

### 🐛 Bug Fixes

- **First-draft CTA persistence** — the "Build First Draft" button and quick-start panel now delete themselves as soon as the user clicks, instead of lingering over the chat thread
- **First-draft references** — attached files are persisted as project references before the first turn so their extracted content reaches the agent on the initial build
- **Upload handler** — Designer file upload switches to `e.file.read()` with an async/sync dual path, fixing `AttributeError: UploadEventArguments.name` and `NoneType context manager` crashes on the first upload
- **Initial chat container race** — `ui/home.py` polls up to 5 s for the chat container to mount before sending the initial build message instead of silently dropping it
- **PPTX text duplication** — editable PPTX exports no longer overlay overlapping text between slides; each raster is rendered in an isolated Playwright page
- **Image overlay fallback** — agent-generated images land in the correct container via typed slots and heuristic placeholder detection instead of floating as absolute overlays on top of existing content
- **Slot sizing** — inner `<img>` elements get proper `width:100%;height:100%;object-fit:cover` styles; previously `object-fit` was applied to a wrapper `<div>` where it had no effect

### ⬆ In-App Auto-Updates

Thoth now ships its own background updater so users on packaged Windows / macOS builds get new releases without ever leaving the app.

- **Background scheduler** — new stdlib-only `updater.py` polls the GitHub Releases API on a daemon thread (30-second startup delay, every 6 hours, 24-hour debounce). Checking is on by default; if there's no Internet the call fails silently and the next tick retries. Dev installs (running from a `.git/` checkout) are detected and the scheduler is skipped
- **Status-bar pill** — `ui/status_bar.py` renders an `⬆ vX.Y.Z` chip when a newer release is available; the pill subscribes to updater state-change notifications and clears itself when the user installs or skips
- **What's-New dialog** — new `ui/update_dialog.py` shows the release notes, an **Install now** primary action, and **Skip this version** / **Remind me later** secondary actions; skipped versions and dismissed banners persist to `~/.thoth/update_config.json`
- **Settings surface** — Settings → Preferences → Updates exposes channel selection (stable / beta), the skip list with one-click un-skip, a manual **Check now** button, and the last-checked / last-success timestamps
- **SHA256-verified downloads** — every release body embeds a `<!-- row-bot-update-manifest -->` fenced block (`schema: 1`, per-asset sha256). The updater downloads the platform-specific asset, computes its hash, and refuses to launch the installer on mismatch
- **OS code-signature gate** — Windows runs `signtool.exe verify /pa` (Authenticode); macOS runs `codesign --verify --deep --strict`. A failed signature check aborts the install before the OS installer is launched
- **Per-platform asset routing** — Windows expects `ThothSetup_X.Y.Z.exe`, macOS expects `Thoth-X.Y.Z-macOS-{arm64|x86_64}.dmg`; arch detection picks the right Mac asset automatically
- **Hand-off** — Windows launches the Inno Setup installer in silent mode (`/SILENT /CLOSEAPPLICATIONS /RESTARTAPPLICATIONS`) so it can swap files in place and re-launch Thoth; macOS opens the verified DMG
- **Agent surface** — new `tools/updater_tool.py` registers `thoth_check_for_updates` (read-only) and `thoth_install_update` (interrupt-gated). The dynamic self-knowledge block surfaces "Update available: …" when applicable, and `thoth_status` adds an `updates` category
- **Release plumbing** — new `scripts/append_sha_manifest.py` computes and patches the manifest block into a published GitHub release body; new `.github/workflows/update-manifest.yml` runs it automatically on `release: [published, edited]`; new `.github/workflows/notarize-submit.yml` and `.github/workflows/notarize-check.yml` handle Apple notarization with stapling

### 🧰 Other Changes

- **Shared utilities** — new top-level `utils/` package with `media.py` and `text.py` consolidates helpers previously duplicated across Designer, channels, and export
- **Designer storage & history** — `designer/storage.py` and `designer/history.py` tighten snapshot handling and Windows-safe atomic writes
- **Prompt scaffolding** — `prompts.py`, `self_knowledge.py`, and `memory.py` feed richer identity, self-knowledge, and threads context into the agent
- **Installer packaging** — `installer/thoth_setup.iss` bumps to v3.17.0 and bundles the new Designer runtime assets and video-guide skills
- **Skills & guides** — `bundled_skills/design_creator/SKILL.md` and `tool_guides/designer_guide/SKILL.md` are rewritten around the five modes, typed image slots, and the critique-repair loop

### 🧪 Tests

- **Regression expansion** — `test_suite.py` picks up **~2,700 new lines** of coverage, heavily focused on Designer modes, runtime bridge, export isolation, image slot resolution, critique thresholds, and the video generation tool
- **Section 73: Auto-Update** — 16 dedicated tests covering the updater public API, config persistence, manifest parsing and SHA256 verification, per-platform asset selection, channel filtering and skip-list handling, state transitions, dev-install detection, and `thoth_check_for_updates` / `thoth_install_update` tool registration

### 📁 Files Changed

| File | Change |
|------|--------|
| **`designer/runtime/`** | **New** — sandboxed JS + CSS runtime bridge loaded into preview, export, and published output |
| **`designer/brand_lint.py`** | **New** — catches hardcoded colours / fonts before they leave the tool |
| **`designer/command_palette.py`** | **New** — keyboard-driven access to every designer tool |
| **`designer/hotspot_recorder.py`** | **New** — click-to-wire navigate / toggle actions in preview |
| **`designer/mutation_diff.py`** | **New** — turn-over-turn diff of agent-authored page changes |
| **`designer/qr_utils.py`** | **New** — QR helpers for shareable published links |
| **`designer/review.py`** | **New** — review model + state for inspecting agent mutations |
| **`designer/review_dialog.py`** | **New** — UI surface for the review flow |
| **`designer/route_graph.py`** | **New** — multi-route graph model for interactive modes |
| **`designer/zero_state.py`** | **New** — per-mode quick-start chip suggestions |
| **`tools/video_gen_tool.py`** | **New** — first-class video generation tool |
| **`tool_guides/video_guide/`** | **New** — tool-usage guide for video generation |
| **`utils/`** | **New** — shared `media.py` / `text.py` helpers used by Designer, export, and channels |
| **`ui/bulk_select.py`** | **New** — batch-select primitive shared across home surfaces |
| **`ui/confirm.py`** | **New** — standard confirm-dialog helper |
| **`ui/skeleton.py`** | **New** — skeleton loading blocks for galleries and lists |
| **`ui/timer_utils.py`** | **New** — polling / debounce utilities |
| `designer/ai_content.py` | 5-path image slot resolver, `object-fit:cover` applied to the real media element, stripped stale author styles |
| `designer/editor.py` | First-draft CTA + quick-start panel self-dismiss; reference-aware send path; richer chat wiring |
| `designer/export.py` | Isolated-page raster per slide for editable PPTX; captures `objectFit` / `objectPosition` / `svgOuterHTML` |
| `designer/templates.py` | Curated starters for every mode (pitch, brief, landing hero, three-route app, four-shot storyboard, SaaS dashboard, more) |
| `designer/template_gallery.py` | Five-mode gallery, upload-as-template, brand preset selector, attached-file persistence |
| `designer/tool.py` | Adds critique / repair / insert-component / move / duplicate / restyle / refine-text / video-gen / interactive-mode tools |
| `designer/prompt.py` | Mode-specific canvas rules and content budgets, authoring guardrails, post-critique repair loop |
| `designer/critique.py` | Overflow detection now also flags card-heavy pages |
| `designer/page_navigator.py` | Interactive-mode route switching and preview wiring |
| `designer/preview.py` | Multi-route interactive rendering, runtime bridge injection |
| `designer/state.py` | Five-mode project model and interactive-mode metadata |
| `designer/storage.py` | Tighter atomic writes and history handling |
| `designer/interaction.py` | Declarative data-attribute action model |
| `designer/presentation.py` | Presenter-mode support for storyboard and multi-route projects |
| `designer/publish.py` · `designer/share_dialog.py` | Published-link + QR share flow |
| `designer/setup_flow.py` · `designer/briefing.py` · `designer/session.py` · `designer/home_tab.py` · `designer/history.py` · `designer/render_assets.py` · `designer/html_ops.py` | Five-mode setup, briefing, session, home-tab, history, asset hydration, and HTML-ops refinements |
| `channels/media_capture.py` | **New** — shared channel media helper |
| `channels/discord_channel.py` · `channels/slack.py` · `channels/telegram.py` · `channels/whatsapp.py` · `channels/whatsapp_bridge/bridge.js` | Media capture, voice-warning suppression, and attachment fixes |
| `ui/home.py` | Initial-build chat container polling; designer tab refinements |
| `ui/sidebar.py` · `ui/command_center.py` · `ui/chat.py` · `ui/streaming.py` · `ui/render.py` · `ui/settings.py` · `ui/status_bar.py` · `ui/head_html.py` · `ui/helpers.py` · `ui/export.py` · `ui/state.py` | Sidebar batch actions, skeleton/confirm plumbing, chat streaming polish, settings and status bar updates |
| `tools/__init__.py` · `tools/registry.py` | Registers the video generation tool |
| `tools/browser_tool.py` · `tools/x_tool.py` | Browser stability + X OAuth rate-limit health check |
| `tool_guides/designer_guide/SKILL.md` · `tool_guides/thoth_status_guide/SKILL.md` | Rewritten for five modes, typed slots, critique-repair loop, and video generation |
| `bundled_skills/design_creator/SKILL.md` | Updated authoring behaviour for the five modes |
| `agent.py` · `app.py` · `dream_cycle.py` · `memory.py` · `tasks.py` · `threads.py` · `self_knowledge.py` | Agent / app / dream-cycle / memory / tasks / threads / self-knowledge refinements |
| `installer/thoth_setup.iss` | v3.17.0 packaging with bundled runtime and video guide; bundles `updater.py`, `ui/update_dialog.py`, `tools/updater_tool.py`, and `scripts/append_sha_manifest.py`; `CloseApplications=yes` / `RestartApplications=yes` for in-place auto-update swap |
| **`updater.py`** | **New** — stdlib-only background update scheduler with channel selection, manifest verification, and OS code-signature gating |
| **`ui/update_dialog.py`** | **New** — What's-New dialog with Install / Skip / Remind-me-later flow |
| **`tools/updater_tool.py`** | **New** — agent-surface tools `thoth_check_for_updates` and `thoth_install_update` |
| **`scripts/append_sha_manifest.py`** | **New** — computes SHA256 of release assets and patches the `<!-- row-bot-update-manifest -->` block into the GitHub release body |
| **`.github/workflows/update-manifest.yml`** | **New** — runs `append_sha_manifest.py` automatically on `release: [published, edited]` |
| **`.github/workflows/notarize-submit.yml`** · **`.github/workflows/notarize-check.yml`** | **New** — Apple notarization submit + poll + staple workflow |
| `.github/workflows/release.yml` | Builds Windows installer + signed macOS DMG/PKG; Authenticode signing block staged for Certum cert |
| `app.py` | Calls `start_update_scheduler()` at boot |
| `tools/thoth_status_tool.py` | Adds `updates` status category alongside double-gated model normalisation and video-generation status |
| `self_knowledge.py` | Dynamic self-knowledge block surfaces "Update available: …" when the updater has detected one |
| `docs/ARCHITECTURE.md` | Updated for the new Designer runtime, utilities layout, and Auto-Updates subsystem |
| `README.md` | Adds the **⬆ Auto-Updates** section |
| `test_suite.py` | ~2,700 lines of new Designer / runtime / export / image-slot / critique / video-tool coverage; **Section 73** adds 16 auto-update tests |

## v3.16.0 — Designer Studio, Self-Aware Status & Insight Engine

Thoth gains a full **Designer Studio** for building multi-page presentations, one-pagers, marketing material, and reports inside the app. Designer ships with a home-screen gallery, unified setup flow, live editor, brand controls, reusable components, AI image generation, chart embedding, presenter mode, published deck links, and export to **PDF / HTML / PNG / PPTX**. Outside Designer, Thoth becomes more **self-aware** — a new **Thoth Status** tool can inspect live configuration, tools, channels, logs, and Designer state, **identity** is now configurable, and the dream cycle now produces actionable **insights** surfaced in the Workflow Console. The home UI gains a dedicated **Designer** tab, a new **status bar** with configurable avatar and live health pills, and extracted shared chat components used by both the main chat and Designer. Ships with major regression expansion, including dedicated coverage for Designer Studio, identity, self-knowledge, Thoth Status, and insights, bringing the suite to **1751 PASS / 0 FAIL / 4 WARN**.

### 🎨 Designer Studio

A complete in-app design subsystem for decks, one-pagers, reports, and marketing layouts.

- **New `designer/` subsystem** — gallery, setup flow, editor, preview, export, publish, presentation, history, references, storage, AI content, and brand modules across ~35 new files
- **Home-screen Designer tab** — `ui/home.py` adds a first-class Designer surface alongside Workflows, Knowledge, and Activity, with a project gallery and direct launch into the editor
- **Unified project setup flow** — template selection, aspect ratio / canvas sizing, project brief capture, and create-only vs create-and-build-first-draft flows are handled in one entry point instead of separate dialogs
- **Live multi-page editor** — page navigator, canvas resize controls, interactive preview editing, in-place text editing, undo/redo, and snapshot history support iterative design work without leaving the app
- **Brand system** — brand presets, color/font controls, logo placement, and brand-variable injection allow consistent styling across pages and exports
- **Reusable design blocks** — curated components, critique helpers, and deterministic repair flows support structured layout building and safe cleanup passes
- **AI-assisted content tools** — generate images, refine copy, add charts, generate notes, and update individual pages without rewriting whole projects
- **Presentation and sharing** — presenter mode, separate audience window support, publishable deck links, and export to PDF / HTML / PNG / PPTX complete the end-to-end workflow

### 🖼️ Asset-Backed Design Media

Designer media now uses persisted asset references instead of bloating project HTML with inline image payloads.

- **Canonical asset refs** — stored project HTML now uses `asset://<asset-id>` references for generated images, inserted images, replaced images, and charts
- **Persistent media storage** — Designer assets are stored on disk per project and hydrated for preview, export, publish, and presentation output when needed
- **Compatibility normalization** — render and load paths tolerate legacy data URIs, legacy `asset:` schemes, and malformed “asset-like” identifiers instead of failing hard or leaving broken placeholders
- **Compact project state** — `designer_get_project` and stored page HTML stay small and structured because binary image data no longer rides inside every page update
- **Windows-safe atomic writes** — project and asset persistence now use unique temp files plus replace retries for common Windows file-lock cases, avoiding temp-file races and placeholder-only failures

### 🪞 Self-Aware Status & Identity

Thoth can now inspect and describe its own state more accurately, and selected self-management actions are exposed through a dedicated tool.

- **New `thoth_status` tool** — query version, model, channels, memory, skills, tools, API keys, tasks, voice, image generation, video generation, config, logs, errors, and Designer project state from one place
- **Controlled self-management** — `thoth_update_setting` can change selected settings such as model, context caps, dream-cycle settings, tool toggles, skill toggles, image-generation model, video-generation model, and self-improvement mode with explicit approval
- **Identity module** — new `identity.py` persists assistant name and personality in user config, sanitizes prompt-injection-like text, and stores the self-improvement toggle
- **Dynamic prompt identity** — `prompts.py` now builds the agent system prompt from the configured identity instead of relying solely on the static fallback string
- **Self-improvement hooks** — Thoth Status can create new user skills and patch existing skills with backups and bundled-skill overrides when self-improvement is enabled
- **New tool guide** — `tool_guides/thoth_status_guide/SKILL.md` documents when to query status, when to inspect logs, and how controlled setting changes should be handled

### 🧠 Self-Knowledge, Memory & Insights

The agent now has a richer internal model of its own capabilities, and the dream cycle can turn system observations into actionable follow-up.

- **Self-knowledge manifest** — new `self_knowledge.py` defines a structured feature manifest, dynamic state block, and identity line so the agent can answer “what can you do?” more consistently
- **Designer and self-awareness prompting** — prompt scaffolding now includes self-knowledge and a dedicated `DREAM_INSIGHTS_PROMPT` for turning recent system activity into structured insight objects
- **Insights engine** — new `insights.py` persists, deduplicates, prunes, pins, dismisses, and applies insights across categories including error patterns, skill proposals, tool configuration, knowledge quality, usage patterns, and system health
- **Workflow Console integration** — `ui/command_center.py` adds an Insights panel with dismiss, pin, investigate, and apply actions, including one-click application of auto-fixable skill proposals
- **Richer memory / graph metadata** — memory extraction and knowledge-graph flows now support aliases, source metadata, stronger relation handling, and updated self-knowledge integration

### 💬 UI & Workflow Updates

- **Shared chat primitives** — new `ui/chat_components.py` extracts the chat message area, upload flow, and input bar into reusable components shared by the main chat and Designer
- **Status bar overhaul** — `ui/status_bar.py` replaces the old home logo section with a configurable avatar, cached health pills, and a diagnosis button wired into `ui/status_checks.py`
- **Dynamic health checks** — status checks now cover model availability, cloud APIs, channels, tunnel state, scheduler status, memory extraction freshness, and OAuth-backed tools with consistent `CheckResult` handling
- **Settings wiring** — settings now expose identity configuration and related persistence instead of treating the assistant name/personality as static
- **Design workflow guidance** — new bundled `design_creator` skill plus the Designer tool guide help steer presentation and layout workflows more consistently

### 🐛 Bug Fixes

- **Designer image placeholders** — preview and editor rendering now correctly hydrate persisted Designer image assets instead of leaving placeholder-only image blocks when stored HTML contains non-canonical asset references
- **Gallery preview accuracy** — Designer gallery cards load the real current first-page content instead of relying on stale summary HTML
- **Atomic save collisions on Windows** — overlapping project saves no longer collide on a shared temp path; unique temp files and replace retries handle common `WinError 2`, `WinError 5`, and `WinError 32` cases more safely
- **Graceful legacy asset handling** — malformed legacy base64 payloads, invalid legacy logo data, and unresolved legacy asset refs degrade safely instead of breaking the whole project render path

### 🔧 Other Changes

- **Tool registration** — `tools/__init__.py` now registers both the new Thoth Status tool and the Designer tool on startup
- **Local presentation assets** — `static/reveal/` and `static/fonts/` add self-hosted presentation/runtime assets for Designer export and presentation flows
- **Version single source of truth** — new `version.py` centralizes the app version string for reuse across the app and tools
- **Installer / packaging updates** — installer and requirements changes pull the new Designer and self-awareness surfaces into the packaged app

### 🧪 Tests

- **Dedicated Designer coverage** — `test_suite.py` adds Section 72 for Designer Studio, covering imports, setup flow, component rendering, storage, prompt building, preview, export, tool registration, and asset-backed media behavior
- **Self-awareness coverage** — new tests cover `identity.py`, `self_knowledge.py`, `tools/thoth_status_tool.py`, and `insights.py`, including prompt injection sanitization, status categories, insight CRUD, and semantic deduplication
- **Designer E2E plan** — `docs/DESIGNER_E2E_TEST.md` documents the manual end-to-end validation path for gallery, editor, branding, AI content, export, and presenter mode
- **Regression expansion** — the combined suite now validates the new Designer, self-awareness, and insights surfaces end to end, reaching **1751 PASS / 0 FAIL / 4 WARN**

### 📁 Files Changed

| File | Change |
|------|--------|
| **`designer/`** | **New** — full Designer subsystem: gallery, setup, editor, preview, export, publish, presentation, history, references, storage, assets, and AI content |
| **`tools/thoth_status_tool.py`** | **New** — self-introspection and controlled self-management tool |
| **`identity.py`** | **New** — persistent assistant name / personality configuration with sanitization |
| **`self_knowledge.py`** | **New** — feature manifest, identity line, and self-knowledge prompt block |
| **`insights.py`** | **New** — persisted dream-cycle insights engine with dedup / apply flows |
| **`ui/chat_components.py`** | **New** — shared chat UI components for main chat and Designer |
| **`bundled_skills/design_creator/SKILL.md`** | **New** — behavior skill for structured design workflows |
| **`tool_guides/designer_guide/SKILL.md`** | **New** — tool-usage guide for Designer workflows |
| **`tool_guides/thoth_status_guide/SKILL.md`** | **New** — tool-usage guide for self-status and self-management |
| `app.py` | Launches the dedicated Designer editor flow and published-deck setup |
| `dream_cycle.py` | Insight generation and refinement updates |
| `knowledge_graph.py` | Richer metadata, relation handling, and graph-side refinements |
| `memory_extraction.py` | Updated extraction flow and self-knowledge integration |
| `prompts.py` | Dynamic identity prompt construction and dream insights prompt |
| `skills.py` | Self-improvement and guide-related skill plumbing |
| `tools/__init__.py` | Registers `thoth_status` and the Designer tool |
| `ui/home.py` | Adds the Designer tab, gallery launch, and editor entry flow |
| `ui/command_center.py` | Adds the Insights panel and insight actions |
| `ui/settings.py` | Identity and related settings persistence wiring |
| `ui/status_bar.py` | Replaces the old logo area with avatar + health status UI |
| `ui/status_checks.py` | Expanded health checks for channels, tunnel, model, OAuth, and memory extraction |
| `requirements.txt` | Dependency updates for the new Designer and self-awareness surfaces |
| `test_suite.py` | Dedicated Designer and self-awareness regression coverage |
| `docs/DESIGNER_E2E_TEST.md` | **New** — manual Designer end-to-end test plan |

## v3.15.0 — Multi-Channel Messaging, X Tool, Tunnels & Tool Guides

Thoth goes **multi-channel** — four new messaging adapters join Telegram: **WhatsApp** (via Baileys bridge with QR pairing), **Discord**, **Slack**, and **SMS** (Twilio). All five channels share full parity: streaming responses, typing indicators, reactions, media capture, slash commands, and thread management. A new **tunnel manager** (ngrok) auto-exposes webhook ports so channels like SMS receive inbound messages without manual port forwarding. The **X (Twitter) tool** adds native read, post, and engagement capabilities via OAuth 2.0 PKCE. A **tool guides** system auto-injects per-tool usage instructions into the system prompt when tools are enabled, replacing 120+ lines of hardcoded prompt text. The **sidebar** gains a live **channel health monitor** with status dots, icons, and last-activity tracking. A **chat input redesign** wraps the composer in a modern card layout. Generated images now **persist to disk** so channels can send them after generation. Ships with **~76 net new tests** across 3 sections, covering the X tool, finish-reason detection, and tunnel infrastructure.

### 📡 Multi-Channel Messaging

Four new channel adapters give Thoth the same conversational experience across five platforms.

- **WhatsApp** — Node.js bridge powered by Baileys v6 with QR code pairing (displayed in Settings); inbound/outbound text, photos, documents, and voice; YouTube URL extraction with rich link previews via oEmbed + thumbnail fetch; Markdown-to-WhatsApp formatting with table conversion; streaming responses via rate-limited message edits; typing indicators and emoji reactions
- **Discord** — `discord.py` adapter with DM-based messaging; OAuth bot token authentication with numeric User ID gating; streaming via message edits; reactions, typing, slash commands, and media support
- **Slack** — `slack-bolt` adapter with Socket Mode for zero-webhook operation; DM threading; streaming responses via `chat.update`; reactions, typing, and file uploads
- **SMS** — Twilio adapter with inbound webhook receiver; outbound via Twilio REST API; MMS photo support; requires tunnel for inbound delivery
- **Channel parity** — all 5 channels share media capture helpers (`channels/media_capture.py`), auth utilities (`channels/auth.py`), slash command handling (`channels/commands.py`), corrupt-thread detection (`channels/thread_repair.py`), approval routing (`channels/approval.py`), and YouTube URL extraction (`channels/__init__.py`)
- **Auto-start** — each channel has an `auto_start` config flag; `app.py` imports all five adapters at startup and starts configured channels automatically
- **Channel tool factory** — `channels/tool_factory.py` delegates target resolution to each channel's `get_default_target()` method, replacing hardcoded Telegram-only logic; running channels auto-inject their send/photo/document tools into the agent graph

### 🔗 Tunnel Manager

A provider-agnostic tunnel infrastructure for exposing local webhook ports to the internet.

- **`tunnel.py`** — `TunnelProvider` ABC with `NgrokProvider` implementation using `pyngrok`; thread-safe `TunnelManager` singleton
- **Auto-lifecycle** — channels call `tunnel_manager.start_tunnel(port)` on start and `stop_tunnel(port)` on shutdown; orphaned ngrok processes killed at app startup via `kill_stale_ngrok()`
- **Settings UI** — Tunnel Settings section in the Channels tab with provider picker (ngrok), auth token input, active tunnel display, and optional main-app tunnel toggle
- **Health check** — `check_tunnel()` in `status_checks.py` reports active tunnel count and URLs

### 🐦 X (Twitter) Tool

Native X API v2 integration with OAuth 2.0 PKCE authentication — no external CLI or tweepy dependency.

- **13 tools** — `x_get_timeline`, `x_get_user_tweets`, `x_search`, `x_get_tweet`, `x_post_tweet`, `x_reply`, `x_retweet`, `x_like`, `x_unlike`, `x_get_mentions`, `x_get_followers`, `x_get_following`, `x_get_user`
- **OAuth 2.0 PKCE flow** — browser-based authorization with local HTTP callback server; token persistence and auto-refresh; tier detection (Free/Basic/Pro) with endpoint gating
- **Rate limiting** — per-endpoint rate limit tracking with automatic backoff and retry
- **Settings UI** — Accounts tab with X (Twitter) panel showing connection status, API key configuration with step-by-step setup guide, and Connect/Disconnect buttons

### 📘 Tool Guides

A new skill category that auto-injects per-tool usage instructions into the system prompt.

- **`tool_guides/` directory** — 13 SKILL.md files (browser, calendar, chart, email, filesystem, math, shell, telegram, tracker, vision, weather, wiki, x) containing focused tool-usage instructions
- **`tools` field in SKILL.md** — skills can now declare linked tools; when any linked tool is enabled, the guide auto-activates without manual toggling
- **Prompt cleanup** — 120+ lines of hardcoded tool instructions removed from `AGENT_SYSTEM_PROMPT` in `prompts.py`; replaced by dynamically-injected tool guides that only appear when relevant tools are enabled
- **UI separation** — `get_manual_skills()` returns only user-created skills for the Settings Skills tab; tool guides are hidden from manual toggle but always active when their tools are on
- **Skill editor** — new "Linked Tools" multi-select field with chip display for creating tool-linked skills

### 📊 Sidebar Channel Monitor

A live channel health panel in the sidebar, replacing the status bar channel pills.

- **Channel monitor panel** — renders below the conversation list with status dots (green = running, amber = stopped, grey = not configured), channel-specific icons, display names, and relative last-activity times ("2m ago", "1h ago")
- **Activity tracker** — `channels/base.py` tracks `record_activity()` / `get_last_activity()` per channel; all 5 channel handlers call `record_activity()` on each inbound message
- **5-second polling** — `ui.timer(5.0)` refreshes the panel; click any row to open Settings
- **Status bar cleanup** — channel pills filtered from `_render_pills()` in `ui/status_bar.py`; channel health checks remain in the diagnosis dialog

### 💬 Chat Input Redesign

The chat composer is modernized with a card-based layout.

- **Rounded card** — input wrapped in a styled column with `border-radius: 18px`, subtle border, and translucent background
- **File chips inside card** — attached file chips render inside the input card instead of a separate row above
- **Auto-scroll fix** — `wheel`/`touchstart` timestamp tracking prevents the MutationObserver feedback loop on Mac WKWebView that caused auto-scroll to fight user scrolling

### 🖥️ Native App Improvements

- **External link handling** — links in chat now open in the system browser instead of navigating in-app; in pywebview mode, routes through `JsApi.open_url()` via `window.pywebview.api`
- **Context menu paste fix** — right-click Paste now correctly focuses the target element before inserting text; fallback to `document.execCommand('paste')` on clipboard API failure
- **Viewport lock** — `html, body { overflow: hidden }` prevents page-level scrolling in the native window
- **Layout padding** — bottom padding added to prevent chat input from touching the window edge

### 🖼️ Image Persistence to Disk

Generated and edited images now persist to the per-thread media directory.

- **`_save_image_to_disk()`** — saves base64 image data to `~/.thoth/media/{thread_id}/gen_NNN.png` (or `edit_NNN.png`) using the existing media pipeline from `threads.py`
- **All providers** — OpenAI, xAI, and Google image gen/edit paths now call `_save_image_to_disk()` after generation; the saved path is included in the tool result so channels can reference it for sending photos
- **Received files** — `channels/media.py` gains `copy_to_workspace()` to copy inbound attachments into the filesystem-tool workspace (`Received Files/`) with dedup

### ⚡ Streaming on Telegram

Telegram responses now stream live instead of waiting for the full answer.

- **Placeholder + edit pattern** — sends a "⏳" placeholder message, then progressively edits it with accumulated tokens and tool status lines
- **Rate-limited edits** — `_tg_edit_consumer()` edits at most every 1.5 seconds to respect Telegram API rate limits; uses a `queue.Queue` bridge between the sync agent executor and the async Telegram event loop
- **Overflow protection** — if the accumulated text exceeds `MAX_TG_MESSAGE_LEN`, streaming stops editing and the final response is sent as a fresh split message

### ⚠️ Finish-Reason Detection

- **`_finish_reason` tracking** — `_stream_graph()` now reads `response_metadata.finish_reason` from each streaming chunk
- **Truncation warning** — when `finish_reason == "length"`, appends a user-visible warning: "⚠️ This response was cut short by the model's output token limit"

### 🐛 Bug Fixes

- **YouTube Shorts URLs** — `youtube.com/shorts/` pattern added to all 3 Python regexes (`channels/__init__.py`, `ui/render.py`, `ui/constants.py`) and the bridge; previously Shorts links were silently dropped
- **Thread ordering** — WhatsApp and Discord `_get_or_create_thread` now always call `_save_thread_meta`, not just for new threads; conversations correctly reorder by last message in the sidebar
- **Channel thread icons** — WhatsApp (📲 / `forum`), Discord (🎮 / `sports_esports`), and SMS (`textsms`) threads show correct icons in the sidebar
- **Sidebar thread limit** — `SIDEBAR_MAX_THREADS` bumped from 8 to 10
- **OAuth token message** — "re-authenticate in Settings → Google" corrected to "Settings → Accounts"
- **Search tools filter** — Settings → Search & Knowledge now uses an allowlist (`web_search`, `duckduckgo`, `wolfram_alpha`, `arxiv`, `wikipedia`, `youtube`) instead of a blocklist, preventing new tools from being silently hidden

### 🔧 Other Changes

- **Settings → Accounts tab** — Google Account panel refactored into `_build_google_account_panel()` with live status text; new X (Twitter) panel with OAuth flow and tier detection
- **Settings → Channels tab** — dynamic `_build_channel_panel(ch)` renders auto-generated config UI for any registered channel using `config_fields`; tunnel settings section
- **Channel `webhook_port` / `needs_tunnel`** — new properties on `Channel` ABC for channels that need inbound webhooks
- **Channel `get_default_target()`** — new method on `Channel` ABC; replaces hardcoded Telegram-only target resolution in tool factory
- **`check_channels()` returns list** — `run_all_checks()` and `run_light_checks()` now handle list-returning check functions via `isinstance(result, list)` flattening
- **Deleted `tools/telegram_tool.py`** — 244 lines removed; Telegram send/photo/document tools now generated dynamically by the channel tool factory
- **Requirements** — 5 new dependencies (`slack-bolt`, `twilio`, `discord.py`, `pyngrok`, `qrcode`)

### 🧪 Tests

- **~76 net new tests** across 3 new sections (65–67), updating and expanding existing sections
- **Section 65: X (Twitter) Tool** — OAuth token management, API tier detection, tool registration, rate limiting, Settings Accounts tab with X section
- **Section 66: Streaming Finish-Reason Detection** — `_finish_reason` tracking in `_stream_graph`, truncation warning injection, `response_metadata` parsing
- **Section 67: Tunnel & Webhook Infrastructure** — `tunnel.py` module structure, `TunnelManager` singleton, ngrok provider, Settings tunnel section, channel `needs_tunnel` / `webhook_port` properties
- **Existing section updates** — removed obsolete Telegram-specific prompt tests (TELEGRAM MESSAGING, EMAIL ATTACHMENTS sections moved to tool guides); updated approval channel tests

### 📁 Files Changed

| File | Change |
|------|--------|
| **`tunnel.py`** | **New** — Tunnel manager with ngrok provider |
| **`tools/x_tool.py`** | **New** — X (Twitter) tool with 13 API endpoints and OAuth 2.0 PKCE |
| **`channels/whatsapp.py`** | **New** — WhatsApp channel adapter (Baileys bridge) |
| **`channels/discord_channel.py`** | **New** — Discord channel adapter |
| **`channels/slack.py`** | **New** — Slack channel adapter (Socket Mode) |
| **`channels/sms.py`** | **New** — SMS/Twilio channel adapter |
| **`channels/auth.py`** | **New** — Shared channel auth utilities |
| **`channels/commands.py`** | **New** — Shared slash command handling |
| **`channels/approval.py`** | **New** — Approval routing for channels |
| **`channels/media_capture.py`** | **New** — Media capture helpers |
| **`channels/thread_repair.py`** | **New** — Corrupt-thread detection |
| **`channels/whatsapp_bridge/`** | **New** — Node.js bridge (bridge.js, package.json) |
| **`tool_guides/`** | **New** — 13 tool guide SKILL.md files |
| `channels/__init__.py` | YouTube URL extraction + Shorts regex |
| `channels/base.py` | Activity tracker, `webhook_port`, `needs_tunnel`, `get_default_target()` |
| `channels/media.py` | `copy_to_workspace()` for received files |
| `channels/telegram.py` | Streaming via edit consumer, media capture refactor, thread repair import |
| `channels/tool_factory.py` | Delegated target resolution to `get_default_target()` |
| `agent.py` | Channel tool injection, `finish_reason` tracking, truncation warning |
| `app.py` | 5-channel imports, tunnel startup/shutdown, OAuth label fix |
| `launcher.py` | `JsApi.open_url()` for native external links |
| `prompts.py` | Removed 120+ lines of hardcoded tool instructions |
| `skills.py` | Tool guides system: `is_tool_guide()`, `_is_tool_guide_active()`, `get_manual_skills()`, linked tools in skill editor |
| `tools/image_gen_tool.py` | `_save_image_to_disk()` for all providers |
| `tools/telegram_tool.py` | **Deleted** — replaced by channel tool factory |
| `ui/sidebar.py` | Channel monitor panel, channel thread icons |
| `ui/settings.py` | Accounts tab (Google + X), Channels tab with dynamic panels, tunnel settings, skill tool linking |
| `ui/chat.py` | Card-based input layout, auto-scroll wheel/touch fix, tool guide filtering |
| `ui/status_bar.py` | Channel pill filtering |
| `ui/status_checks.py` | `check_channels()`, `check_tunnel()`, list-result flattening |
| `ui/head_html.py` | External link handler, viewport lock, paste fix |
| `ui/constants.py` | YouTube Shorts pattern, `SIDEBAR_MAX_THREADS = 10` |
| `ui/render.py` | YouTube Shorts embed regex |
| `ui/helpers.py` | Helper additions |
| `ui/home.py` | Layout cleanup |
| `ui/command_center.py` | Minor adjustments |
| `ui/terminal_widget.py` | Terminal widget updates |
| `ui/state.py` | State field update |
| `ui/task_dialog.py` | Task dialog tweaks |
| `tools/__init__.py` | Registry update |
| `tools/wiki_tool.py` | Minor cleanup |
| `requirements.txt` | 5 new deps: `slack-bolt`, `twilio`, `discord.py`, `pyngrok`, `qrcode` |
| `test_suite.py` | Sections 65–67, existing section updates |
| `integration_tests.py` | New integration tests |
| `.gitignore` | New ignore entries |
| `installer/thoth_setup.iss` | Installer updates |
| `installer/build_mac_app.sh` | Mac build updates |
| `docs/ARCHITECTURE.md` | Architecture doc updates |
| `bundled_skills/*.md` | Skill description trims |

---

## v3.14.0 — Multi-Provider Cloud, xAI Integration, Workflow Console & UI Polish

Thoth becomes truly **multi-provider** — Anthropic (Claude), Google (Gemini), and xAI (Grok) join OpenAI and OpenRouter as first-class cloud providers with key validation, model fetching, and live model pickers. **Image generation** expands to xAI's Grok Imagine and Google's Imagen 4 / Nano Banana families. A new **media storage architecture** replaces in-memory base64 with file-on-disk persistence and two-tier cleanup, laying the foundation for video generation. A new **Workflow Console** replaces the right drawer with a professional operations panel. The **terminal architecture** is refactored into a modular PTY bridge. **Prompt-injection defences** add 5-layer scanning. The UI receives a polish pass — auto-scroll, inline image rendering fixes, and sidebar refinements. Ships with **172 new tests** across 4 sections, bringing the total to **1526 PASS**, 0 FAIL, 3 WARN.

### ☁️ Multi-Provider Cloud Support

Anthropic, Google AI, and xAI are now first-class cloud providers alongside OpenAI and OpenRouter.

- **Anthropic (Claude)** — API key configuration, validation via `/v1/models`, paginated model fetching with `after_id`, context size from `max_input_tokens`, skip list for non-chat models (embed, tokenizer)
- **Google (Gemini)** — API key configuration, validation via Generative Language API, model fetching with pagination, skip list for non-chat models (embed, aqa, imagen, veo, tts)
- **xAI (Grok)** — API key configuration, validation via `/v1/language-models`, model fetching, Grok 4/3/2 context-size catalog (up to 2M tokens), non-chat model filtering (image/video generation models excluded from chat picker)
- **Provider-aware UI** — cloud status banner shows provider name and emoji (⬡ OpenAI, 💎 Google, 𝕏 xAI); `is_cloud_model()` expanded to detect all providers; model picker refreshes all configured providers
- **LLM instantiation** — `ChatAnthropic`, `ChatGoogleGenerativeAI`, and `ChatXAI` LangChain adapters with proper API key injection

### 🎨 Image Generation — xAI & Google Expansion

Image generation gains two new provider families and architectural improvements.

- **xAI Grok Imagine** — `grok-imagine-image` model with aspect ratio and resolution mapping; quality-to-resolution conversion (`low` → 1k, `high` → 2k)
- **Google Nano Banana** — `gemini-3.1-flash-image-preview`, `gemini-3-pro-image-preview`, `gemini-2.5-flash-image` via `generate_content` API with `response_modalities=['IMAGE']`; supports both generation and editing
- **Google Imagen 4** — `imagen-4.0-generate-001`, `imagen-4.0-fast-generate-001`, `imagen-4.0-ultra-generate-001` via dedicated `generate_images` API; generation only
- **Per-provider model picker** — Settings → Models shows only models for providers with configured API keys
- **Image cache preservation** — cached images now persist within the same thread across turns (no longer cleared on each message); only cleared on thread switch

### 🖥️ Workflow Console

The right drawer is redesigned as a professional operations panel.

- **Workflow Console** — renamed from "Workflows Command Center"; heading with "Background Agents" subtitle
- **5-section layout** — Running (with live progress bars and log), Approvals, Upcoming, Quick Launch (dropdown + Run / + New), Recent Runs
- **Auto-refresh** — 3-second timer syncs the workflow dropdown with running state
- **440px drawer width** — widened from 380px for comfortable content display

### 🖥️ Terminal Architecture

A modular terminal backend replacing inline shell rendering.

- **`terminal_bridge.py`** — PTY communication bridge between the UI and system shell
- **`terminal_pty.py`** — portable PTY backend with process lifecycle management
- **`ui/terminal_widget.py`** — NiceGUI terminal widget with scroll area and command history
- **Terminal panel removal** — the old inline terminal rendering block in `_handle_tool_done` is removed; shell output now shows in the standard tool expansion

### 🛡️ Prompt-Injection Defence

5-layer scanning protects against prompt injection attacks in tool outputs and user inputs.

- **Layer 1: Instruction override detection** — catches "ignore previous instructions", "you are now", "new system prompt" patterns
- **Layer 2: Role impersonation** — detects attempts to impersonate system, assistant, or admin roles
- **Layer 3: Data exfiltration** — flags suspicious URLs with long query strings, base64 segments, or encoded credentials
- **Layer 4: Encoding evasion** — detects base64-encoded instruction overrides and Unicode homoglyph substitution
- **Layer 5: Social engineering** — catches urgency phrases, authority claims, and compliance pressure

### 🔄 Auto-Scroll

Chat window auto-scroll now works reliably using a client-side MutationObserver pattern.

- **Default ON** — chat auto-scrolls to the bottom as tokens stream in
- **User override** — scrolling up more than 50px from the bottom disables auto-scroll; it stays where you put it
- **Auto-reset** — sending a new message or starting a new generation re-engages auto-scroll
- **Client-side only** — no Python round-trips; MutationObserver watches DOM changes and scrolls via native `scrollTop`, matching the pattern used by NiceGUI's own `ui.log` component

### � Media Storage Architecture

A new file-on-disk media system replaces the old in-memory base64 approach, unifying image and future video storage with two-tier persistence.

- **File-on-disk storage** — all media (generated images, captures, attachments) saved to `~/.thoth/media/{thread_id}/` with sequential filenames (`gen_001.png`, `cap_002.png`); sidecar `.media.json` tracks entries per message with type, path, and persist flag
- **Sidecar format v2** — `{version: 2, entries: [{idx, role, sig, media: [{type, path, persist}]}]}` replaces old `.images.json`; clean cut with no backward-compatibility code
- **Two-tier persistence** — Tier 1 (generated content: image gen, video gen, plugin output) survives thread deletion; Tier 2 (captures: vision, browser, filesystem, attachments) cleaned up with thread
- **Thread deletion cleanup** — deletes sidecar + Tier 2 files; preserves Tier 1 files on disk; removes empty media directories
- **6 image sources tagged** — Image Gen Tool (Tier 1), Vision Tool (Tier 2), Browser Tool (Tier 2), Filesystem Tool (Tier 2), Plugin `__IMAGE__` (Tier 1), User Attachments (Tier 2)
- **Hydration on thread load** — `_hydrate_thread_media()` reads files from disk and converts to base64 for display; replaces old in-memory-only approach

### 🐛 Bug Fixes

- **Inline image rendering** — `_handle_tool_done` now extracts `raw_name` from tool-done events and uses it for all tool identity checks (`generate_image`, `edit_image`, `browser_*`, `workspace_read_file`, `analyze_image`); previously these compared display names against raw function names and never matched, so generated images, browser screenshots, vision captures, and filesystem images were never rendered inline
- **Workflow "(paused)" label** — `_resume_pipeline()` and `_resume_graph_interrupted()` now strip the "(paused)" suffix from thread names on resume

### 🔧 Other Changes

- **Persistent logging** — `logging_config.py` with centralized configuration; Settings → Logging section with level picker and Open Folder button; Activity panel "Recent Logs" section
- **Knowledge graph entity editor** — `ui/entity_editor.py` for inline entity editing in the graph panel
- **Wiki vault expansion** — +213 lines of vault management improvements
- **Dream cycle tuning** — additional quality fixes validated by new test section
- **Sidebar polish** — wave hand icon shrunk (1.4 → 1.1rem), gear icon enlarged (1.25rem) and converted to icon-only round button; "Settings" text label removed
- **"Workflows Running"** — sidebar avatar badge renamed from "Tasks Running"
- **"No workflows running"** — empty-state placeholder renamed in Workflow Console
- **Browser tool** — +36 lines of browser automation additions
- **Memory tool** — +70 lines of memory operations
- **Shell tool** — +39 lines of safety classification improvements
- **Task tool** — persistent thread support in tool schemas
- **Requirements** — 6 new dependencies (`langchain-anthropic`, `langchain-google-genai`, `langchain-xai`, and others)

### 🧪 Tests

- **172 new tests** across 4 sections (50–51, 52 expansion, 57), bringing the total to **1526 PASS**, 0 FAIL, 3 WARN
- **Section 50: Prompt-Injection Defence** — 5-layer scanning: instruction override, role impersonation, data exfiltration, encoding evasion, social engineering; clean text passthrough; warning format validation
- **Section 51: Persistent Logging** — logging config, level picker, file handler, Settings UI section, Activity panel Recent Logs section
- **Section 52 expansion** — xAI provider, Google Imagen 4 + Nano Banana models, per-provider model registry, aspect ratio mapping, image cache thread preservation, key validation, model fetching
- **Section 57: Dream Cycle Tuning** — quality fix validations

### 📁 Files Changed

| File | Change |
|------|--------|
| **`logging_config.py`** | **New** — Centralized logging configuration |
| **`terminal_bridge.py`** | **New** — PTY communication bridge |
| **`terminal_pty.py`** | **New** — Portable PTY backend |
| **`ui/command_center.py`** | **New** — Workflow Console right drawer (5-section layout, auto-refresh, quick launch) |
| **`ui/entity_editor.py`** | **New** — Knowledge graph entity editor |
| **`ui/terminal_widget.py`** | **New** — Terminal widget component |
| `models.py` | Anthropic, Google, xAI providers — key validation, model fetching, LLM instantiation, context-size catalog |
| `api_keys.py` | New API key entries for Anthropic, Google, xAI |
| `tools/image_gen_tool.py` | xAI Grok Imagine, Google Imagen 4 + Nano Banana, per-provider model registry, image cache preservation |
| `ui/streaming.py` | Removed `_smart_scroll()`, terminal panel block; added `raw_tool_name` for tool identity checks; media persist tiers |
| `ui/chat.py` | MutationObserver auto-scroll injection; media persistence updates |
| `ui/sidebar.py` | "Workflows Running" badge; icon sizing polish; Settings button icon-only |
| `ui/home.py` | "Background Agents" subtitle; log viewer sizing; Recent Logs section |
| `ui/settings.py` | Anthropic/Google/xAI key sections; image-gen model picker; logging section |
| `ui/state.py` | `command_center_col` field; `_auto_scroll` removed |
| `ui/render.py` | Filename→base64 resolution; `__IMAGE__` marker rendering |
| `ui/helpers.py` | `persist_thread_media_state` rename; media persist flags |
| `ui/status_bar.py` | Status bar restructuring |
| `ui/status_checks.py` | OAuth health check improvements |
| `ui/graph_panel.py` | Entity editor integration |
| `ui/setup_wizard.py` | Wizard updates |
| `ui/task_dialog.py` | Task dialog additions |
| `agent.py` | `raw_name` in `tool_done` events; `_resolve_tool_display_name` mapping |
| `tasks.py` | "(paused)" label cleanup on resume; `_prepare_task_thread` refactor |
| `prompts.py` | Prompt-injection defence layers; prompt refinements |
| `threads.py` | `_MEDIA_DIR`, `save_thread_media()`, `load_thread_media()`, `save_media_file()`, `load_media_file()`, two-tier `_delete_thread` cleanup; thread summary fields |
| `knowledge_graph.py` | Graph refactoring |
| `memory_extraction.py` | Extraction updates |
| `dream_cycle.py` | Dream cycle tuning |
| `wiki_vault.py` | Vault expansion |
| `channels/telegram.py` | Channel updates |
| `tools/base.py` | Base tool changes |
| `tools/browser_tool.py` | Browser automation additions |
| `tools/memory_tool.py` | Memory operations |
| `tools/shell_tool.py` | Safety classification |
| `tools/task_tool.py` | Persistent thread in schemas |
| `tools/wiki_tool.py` | Wiki tool cleanup |
| `requirements.txt` | 6 new dependencies |
| `bundled_skills/*.md` | Skill description tweaks |
| `test_suite.py` | 172 new tests in sections 50–51, 52 expansion, 57 |
| `integration_tests.py` | New integration tests |
| `test_memory_e2e.py` | Memory e2e updates |

---

## v3.13.0 — Advanced Workflows, Approval Gates & Memory Overhaul

Tasks evolve into **advanced workflows** with step-based pipelines, conditional branching, and approval gates. The **dream cycle** gets a comprehensive quality overhaul — hub diversity caps, batch rotation, rejection caching, confidence decay, and Ollama busy checks. **Memory extraction** gains vague-type banning, relation pre-normalisation, and cross-source merge protection. **Document extraction** is hardened with entity caps, description quality gates, self-loop rejection, and a curated relation vocabulary that eliminates 96% of unknown-type warnings. Ships with **221 new tests** across 3 sections, bringing the total to **1354 PASS**, 0 FAIL, 1 WARN.

### 🔀 Advanced Workflow Builder

Tasks are renamed to **Workflows** throughout the application and gain a full pipeline builder with branching logic.

- **Step-based pipelines** — 5 step types: Prompt, Condition, Approval, Subtask, and Notify; each step can reference previous step output via `{{step.X.output}}` variables
- **Conditional branching** — `if_true` / `if_false` routing with expression operators: contains, regex, JSON path, and LLM evaluation
- **Approval gates** — workflows pause at approval steps and wait for human decision; configurable timeout with `if_approved` / `if_denied` routing
- **Webhook triggers** — workflows can be triggered via `POST /api/webhook/<task_id>` with auto-generated secrets for authentication
- **Task-completion triggers** — one workflow can trigger another on completion, enabling chained automation
- **Concurrency groups** — prevent parallel execution of related workflows; only one workflow per group runs at a time
- **Safety mode** — per-workflow setting: block destructive tools, require approval on destructive, or allow all; enforced across shell, task, and channel tools
- **Tools override** — per-step tool selection with auto-detection from prompt content
- **Agent-callable** — the task tool now accepts step definitions, triggers, safety mode, and concurrency group for programmatic workflow creation

### 🏗️ Workflow Builder UI

A redesigned task dialog with simple and advanced modes.

- **Simple/Advanced toggle** — simple mode preserves the existing single-prompt interface; advanced mode exposes the full pipeline builder
- **Step builder** — drag-to-reorder, delete, type-change for each step; visual condition builder with operator picker, JSON path input, and LLM question textarea
- **Variable insertion menu** — `{{step.X.output}}`, `{{date}}`, `{{time}}`, and context variables insertable via dropdown
- **Flow preview** — Mermaid diagram generated from step graph with refresh button
- **Validation** — required field checks, reference validation, and operator-specific rules before save

### ✋ Approval Gates

Built-in pause/resume for human decisions on destructive or high-stakes actions.

- **Pending approvals panel** — Activity tab shows pending approval cards with task name, message, and Approve / Deny buttons; auto-refreshes every 5 seconds
- **Sidebar badge** — orange count badge on the Home button when approvals are pending; compact approval strip above the thread list with quick-approve buttons
- **Multi-channel routing** — approval requests sent to configured channels (Telegram, desktop notifications) with inline keyboard buttons
- **Agent integration** — agent checks pending approvals before resuming; routes to `if_approved` or `if_denied` step based on user response

### 🧠 Dream Cycle Quality Overhaul

A comprehensive quality improvement to the dream inference engine, validated across three 5-cycle test rounds.

- **Hub diversity cap** — limits any single entity to at most 3 appearances across inferred pairs per cycle, preventing popular entities from monopolising inferences
- **Batch rotation** — stored offset with half-overlap ensures fresh entity pairs each cycle instead of re-evaluating the same 50 oldest entities
- **Rejection cache** — pairs rejected by the LLM are cached for 7 days in `dream_rejections.json`; avoids wasting LLM calls on previously rejected combinations
- **Pre-flight merge check** — before inferring a relation between entities A and B, checks if A's description already mentions B's subject (likely already merged); skips if so
- **Skip vague edges** — dream inference skips existing vague relation types (`related_to`, `associated_with`, etc.) when checking for existing connections
- **Multi-excerpt evidence** — inference prompt now receives multiple conversation excerpts per entity pair for richer context
- **Confidence decay** — new Phase 3 in the dream cycle: relations older than 90 days lose 10% confidence per cycle; relations below 0.3 are pruned automatically
- **Ollama busy check** — queries `/api/ps` before starting a dream cycle; defers if Ollama is actively serving a user request to avoid GPU competition
- **`uses` prompt tightening** — rule 6 in the inference prompt: "`uses` means actively employs as a tool, dependency, or platform — NOT merely mentions, searches for, or discusses"
- **🌙 Dream button** — manual dream cycle trigger in the graph panel; async execution with status notifications

### 🔬 Memory Extraction Hardening

Improvements to the background conversation extraction pipeline.

- **Vague-type ban** — `related_to`, `associated_with`, `connected_to`, `linked_to`, `has_relation`, `involves`, and `correlates_with` are rejected before saving, preventing noisy low-value edges
- **Relation pre-normalisation** — `normalize_relation_type()` is called before any checks (ban, confidence gate), ensuring aliases like `is_father_of` are canonicalised to `father_of` before evaluation
- **Cross-source merge protection** — when a document entity matches a personal entity via FAISS semantic search, the similarity threshold is raised from 0.80 to 0.90 to prevent impersonal document content from overwriting personal memories

### 📄 Document Extraction Improvements

Quality gates and relation vocabulary cleanup for the document map-reduce pipeline, validated by extracting 5 representative test documents (research paper, architecture doc, meeting notes, product spec, book chapter).

- **Curated relation vocabulary** — 6 new types added to `VALID_RELATION_TYPES`: `extracted_from`, `uploaded`, `builds_on`, `cites`, `extends`, `contradicts`; 4 alias mappings: `published_by → authored`, `implements → uses`, `used_by → uses`, `references → cites`; eliminates 96% of unknown-type warnings in existing document data
- **Prompt cleanup** — `DOC_EXTRACT_PROMPT` no longer suggests banned types (`related_to`, `associated_with`) or direction-confusing types (`used_by`); replaced with valid alternatives; confidence floor aligned from 0.5 to 0.6
- **Hub entity dedup** — `extract_from_document` checks for an existing media entity with `find_by_subject` before creating a new one; updates the existing hub on re-upload instead of creating a duplicate
- **Entity cap** — extracted entities capped at 12 per document; prevents LLM over-extraction on long documents
- **Min description length** — entities with descriptions shorter than 30 characters are rejected as thin stubs
- **Self-loop rejection** — `add_relation()` now blocks relations where source and target are the same entity (e.g. `Autonomous Agents → used_by → Autonomous Agents`)

### 🔧 Other Changes

- **Workflows rename** — "Tasks" renamed to "Workflows" throughout the UI (sidebar, home page, dialogs)
- **Web search tool** — replaced LangChain TavilySearchResults wrapper with direct TavilyClient API calls for faster execution
- **Shell tool safety** — enhanced destructive-command detection for safety-mode enforcement in workflows
- **Streaming robustness** — replaced silent exception swallowing with `logger.debug()` calls; pending tools tracked via dict instead of DOM search; Mermaid rendering uses `suppressErrors: true`
- **Compression mode** — removed "Smart" option; now "Off (default)" and "Deep (LLM)" only
- **Dream window picker** — interactive HH:00 time inputs for configuring the dream schedule
- **Extraction journal** — viewer button in Activity tab showing detailed extraction stats per thread
- **Dream journal** — expandable entries showing merges, enrichments, inferred relations, and errors per cycle
- **Graph panel** — "Show All" restyled as button; dream button added
- **Mac installer** — test files (`test_suite.py`, `test_memory_e2e.py`, `integration_tests.py`) and dev scripts (`_*.py`) excluded from build

### 🧪 Tests

- **221 new tests** across 3 sections (48–49), bringing the total to **1354 PASS**, 0 FAIL, 1 WARN
- **Section 48: Dream Cycle & Extraction Improvements** (13 tests, 48a–48am) — extraction vague-type rejection, extraction pre-normalisation, pre-flight merge check, `uses` prompt tightening, dream button in graph panel, Ollama busy check, confidence decay
- **Section 49: Document Extraction Improvements** (13 tests, 49a–49m) — document relation types in `VALID_RELATION_TYPES`, alias mappings, normalisation, self-loop rejection, prompt cleanup, hub entity dedup, entity cap, min description length, cross-source merge threshold, functional self-loop test, cross-window dedup merge test
- **5-document extraction verification** — research paper, architecture doc, meeting notes, product spec, book chapter; 49 entities, 101 relations, 0 unknown types, 0 self-loops, 0 banned types, 0 thin descriptions (perfect score)

### 📁 Files Changed

| File | Change |
|------|--------|
| `tasks.py` | Step-based pipelines, conditions, approvals, webhooks, triggers, concurrency groups, safety mode |
| `agent.py` | Approval-gate integration, step branching execution, safety-mode tool filtering |
| `dream_cycle.py` | 4-phase engine: hub cap, batch rotation, rejection cache, pre-flight merge, Ollama busy check, confidence decay |
| `memory_extraction.py` | Vague-type ban, relation pre-normalisation, cross-source merge threshold |
| `document_extraction.py` | Hub dedup, entity cap, min description length, quality gates |
| `knowledge_graph.py` | 6 new relation types, 4 aliases, self-loop rejection |
| `prompts.py` | Dream inference rules, DOC_EXTRACT_PROMPT relation cleanup + confidence floor |
| `ui/task_dialog.py` | Simple/advanced workflow builder, step editor, condition builder, flow preview |
| `ui/home.py` | Pending approvals panel, extraction journal, dream journal, workflows rename |
| `ui/sidebar.py` | Approval badge, approval strip |
| `ui/graph_panel.py` | Dream button, Show All button |
| `ui/settings.py` | Compression mode redesign, dream window time picker |
| `ui/streaming.py` | Logging, pending tools tracking, Mermaid robustness |
| `ui/render.py` | Minor rendering tweaks |
| `ui/head_html.py` | HTML additions |
| `ui/chat.py` | Minor fix |
| `ui/state.py` | State addition |
| `channels/telegram.py` | Multi-channel approval routing, safety mode enforcement |
| `channels/base.py` | Approval notification interface |
| `tools/shell_tool.py` | Safety classification for approval gates |
| `tools/task_tool.py` | Agent-callable workflow builder with steps schema |
| `tools/web_search_tool.py` | Direct TavilyClient API calls |
| `tools/base.py` | Tool registry updates |
| `tools/documents_tool.py` | Minor tweak |
| `tools/memory_tool.py` | Addition |
| `tools/registry.py` | Registry updates |
| `tools/wikipedia_tool.py` | Minor fix |
| `tools/gmail_tool.py` | Cleanup (−35 lines) |
| `notifications.py` | Approval notification support |
| `app.py` | Webhook trigger endpoint |
| `bundled_skills/task_automation/SKILL.md` | Advanced workflow documentation and examples |
| `installer/build_mac_app.sh` | Exclude test files from Mac build |
| `test_suite.py` | 221 new tests in sections 48–49 |

---

## v3.12.0 — Plugin System, Multi-Channel Architecture & Image Generation

Thoth gains a full **plugin architecture** with a built-in **marketplace**, a **multi-channel messaging framework** that abstracts Telegram behind a generic Channel ABC (ready for Slack, Discord, and more), a complete **Telegram upgrade** with voice transcription, photo analysis, document extraction, and emoji reactions, an **image generation tool** powered by OpenAI/OpenRouter, a **Google Account setup wizard**, and expanded **task delivery** to any channel. Ships with **168 new tests** across 8 sections, bringing the total to **1133 PASS**, 0 FAIL, 2 WARN.

### 🔌 Plugin Architecture

A self-contained plugin runtime in `plugins/` handles the full lifecycle — discovery, validation, sandboxing, loading, and teardown.

- **Plugin API** — `PluginAPI` bridge object and `PluginTool` base class are the only core imports a plugin needs; provides `get_config()`, `set_config()`, `get_secret()`, `set_secret()`, `register_tool()`, `register_skill()`
- **Manifest system** — each plugin declares metadata in `plugin.json`: ID, version, author, description, tools, skills, settings schema, API keys, and Python dependencies; validated against a strict schema (ID regex, semver, required fields)
- **Security sandbox** — static scan blocks `eval()`, `exec()`, `os.system()`, `subprocess`, and `__import__()`; import guard prevents loading from core modules (`tools`, `agent`, `models`, `ui`); `register()` call has a 5-second timeout
- **Dependency safety** — freezes core dependency versions before installing plugin deps; blocks downgrades that could break Thoth
- **State persistence** — enable/disable state and config values are stored in `plugin_state.json`; plugin API-key secrets use the OS credential store with metadata in `plugin_secrets.json` under `~/.thoth/`
- **Hot reload** — "Reload Plugins" button in Settings clears the registry and re-runs discovery without restarting the app; agent cache is invalidated automatically
- **Skill auto-discovery** — `SKILL.md` files in a plugin's `skills/` directory are detected and injected into the agent's system prompt alongside built-in skills
- **Version gating** — plugins declare `min_thoth_version`; loader rejects incompatible plugins with a clear error message

### 🏪 Plugin Marketplace

A browse-and-install marketplace powered by a GitHub-hosted `index.json` catalog.

- **Marketplace client** — fetches and caches the plugin index with TTL-based refresh; provides search, tag filtering, and update detection
- **Browse dialog** — NiceGUI dialog with search bar, tag filter pills, and one-click install buttons
- **Install/update/uninstall** — downloads plugin archives, validates before install, manages `~/.thoth/installed_plugins/`; duplicate installs rejected; security violations block installation
- **Update detection** — `check_updates()` compares installed versions against the marketplace index

### ⚙️ Plugin Settings UI

A dedicated **Plugins** tab in Settings for managing all installed plugins.

- **Card grid** — each plugin rendered as a card with icon, name, version badge, description, tool/skill count badges, and enable/disable toggle
- **Missing API key warnings** — cards show a warning badge when required secrets are not configured
- **Per-plugin config dialog** — opens plugin details with API key inputs, settings controls, tools/skills list, and actions (update, uninstall)
- **Empty state** — "No plugins installed" with a marketplace call-to-action

### 🔗 Plugin API v2

Plugin tools gain richer return types and safety metadata.

- **`_run()` method** — plugins can now override `_run()` instead of `execute()` for a cleaner interface; base class handles argument parsing and error wrapping
- **`background_allowed` flag** — plugin tools declare whether they are safe to run in background task workflows; defaults to `False`
- **`destructive` flag** — marks tools that perform irreversible actions; gated from background execution unless explicitly allowed
- **Rich returns** — tool results can include structured data (dicts, lists) that the agent interprets contextually

### 🖼️ Image Generation Tool

Generate and edit images via OpenAI/OpenRouter, rendered inline in chat.

- **`generate_image`** — creates images from text prompts; supports `gpt-image-1`, `gpt-image-1.5`, `gpt-image-1-mini` models with configurable size and quality
- **`edit_image`** — modifies existing images; sources: `"last"` (most recent generation), filename (from attachment cache), or file path on disk
- **Side-channel rendering** — `_last_generated_image` passed to UI streaming layer for inline display; cleared after use
- **Attachment cache** — pasted/attached images stored in `_image_cache` (populated by `ui/streaming.py`) so the agent can reference them by filename
- **Model selector** — configurable in Settings → Models; default `openai/gpt-image-1.5`

### 📡 Channel Architecture (Multi-Channel Foundation)

A generic channel abstraction that decouples messaging from any single platform.

- **`Channel` ABC** — abstract base class all channel adapters inherit from; lifecycle methods `start()`, `stop()`, `is_configured()`, `is_running()`; outbound methods `send_message()`, `send_photo()`, `send_document()`, `send_approval_request()`
- **`ChannelCapabilities`** — declarative feature flags per channel (photo in/out, voice in, document in, buttons, streaming, reactions, slash commands); UI and tool factory read capabilities to auto-generate tooling
- **`ConfigField`** — describes user-configurable fields that render automatically in the Settings UI
- **Channel registry** — `register()`, `all_channels()`, `running_channels()`, `configured_channels()`; central routing via `deliver(channel_name, target, text)` with validation
- **Shared media pipeline** — `channels/media.py` provides `transcribe_audio()` (faster-whisper), `analyze_image()` (Vision service), `extract_document_text()` (PDF/CSV/JSON/plain-text), and `save_inbound_file()` — reusable by any channel
- **Tool factory** — `channels/tool_factory.py` auto-generates LangChain tools (`send_{name}_message`, `send_{name}_photo`, `send_{name}_document`) for each registered channel based on its capabilities; Pydantic input schemas; multi-strategy file path resolution
- **Channel config** — `channels/config.py` provides per-channel key-value store in `~/.thoth/channels_config.json`

### 📱 Telegram Upgrade

Telegram evolves from a basic text relay into a full-featured channel with rich media handling.

- **Voice messages** — inbound voice/audio transcribed via faster-whisper through the shared media pipeline; transcript sent to agent as user text
- **Photo messages** — inbound photos analyzed via Vision service; analysis sent to agent with optional caption
- **Document handling** — inbound documents saved to `~/.thoth/inbox/`, text extracted (PDF, CSV, JSON, plain text), file path + extracted content + caption sent to agent as one message
- **Image generation delivery** — `_grab_generated_image()` retrieves the last generated image from the image gen side-channel and sends it as a photo in Telegram
- **Emoji reactions** — real-time status feedback using Telegram's native reaction API: 👀 (processing), 👍 (success), 💔 (error); graceful fallback if bot lacks permission
- **Interrupt approval** — tool calls requiring human approval render as inline keyboard buttons in Telegram (Approve / Deny)
- **Auto-recovery** — handles orphaned tool calls gracefully; offers fresh thread on persistent failures
- **Bot commands** — registered with BotFather for discoverability

### 🔑 Google Account Setup Wizard

A unified setup flow for Google OAuth (Gmail + Calendar) in the Settings UI.

- **Step-by-step wizard** — guides users through creating OAuth credentials, downloading `credentials.json`, and completing the authorization flow
- **Token health checks** — periodic validation (every 6 hours) with silent refresh; desktop notifications on token expiry
- **Unified section** — Gmail and Calendar OAuth managed together under a single "Google Account" settings section

### 📋 Task System Enhancements

- **Delivery channels** — tasks can route results to Telegram (or future channels) via `delivery_channel` / `delivery_target` fields with validation
- **Model override** — per-task LLM selection via `model_override` field
- **Persistent threads** — `persistent_thread_id` reuses the same conversation thread across task runs
- **Notify-only mode** — `notify_only` flag fires a notification without agent invocation
- **Skills override** — `skills_override` for per-task skill selection
- **Schema migration** — new columns added to tasks table with automatic migration from old `workflows.db`

### 🔗 Core Integration

The plugin system and channel framework touch a minimal set of core files.

- **`app.py`** — calls `load_plugins()` at startup; auto-starts configured channels; periodic OAuth token health check (every 6 hours)
- **`agent.py`** — injects plugin tools + channel tools into the LangChain tools list; plugin skills into the system prompt; `clear_agent_cache()` exported for plugin/channel reload
- **`ui/settings.py`** — Plugins tab, marketplace dialog, Google Account wizard, channel configuration sections

### 🐛 Bug Fixes

- **Telegram reactions not appearing** — 🔄/✅/❌ are not in Telegram's supported reaction set; swapped to 👀/👍/💔 which are supported natively
- **Image generation not shown in Telegram** — `_grab_generated_image()` now retrieves the side-channel image and sends it as a photo
- **"Tool limit reached" false message** — misleading error when tool calls completed normally; message removed
- **Document text extraction for Telegram** — inbound documents now have text extracted and included in the agent message
- **Plugin dependency install crash** — `install_dependencies()` returns `tuple[bool, str]` but installer called `.ok`/`.conflicts` on it; fixed with proper tuple unpacking
- **No auto-reload after marketplace install** — installed plugins now trigger full plugin reload + agent cache clear so tools are available immediately
- **Plugin tools not reaching agent after reload** — agent cache key only includes core tool names; `clear_agent_cache()` now called in both manual reload and marketplace install flows

### 🧪 Tests

- **168 new tests** across 8 sections (49–56), bringing the total to **1133 PASS**, 0 FAIL, 2 WARN
- **Section 49: Plugin System** (25 tests) — imports, manifest validation, PluginAPI, PluginTool, state, secrets, registry, security scan, full lifecycle, broken plugin handling, disabled plugins, skills prompt, unregister, state cleanup, agent/app source verification
- **Section 50: Plugin Settings UI** (7 tests) — UI module imports, `_get_missing_keys` logic, callability checks, settings wiring, AST parse validation
- **Section 51: Marketplace & Installer** (19 tests) — marketplace parse/search/tags/entries, installer install/update/uninstall, duplicate rejection, security violation blocking, update detection
- **Section 52: Image Generation Tool** (31 tests) — model registry, provider detection, input schemas, generate/edit tool creation, side-channel image retrieval, attachment cache, base64 data-URI rendering, config parsing
- **Section 53: Plugin API v2** (17 tests) — `_run()` override, `background_allowed`/`destructive` flags, rich return types, backward compatibility with `execute()`
- **Section 54: Google Account Setup** (17 tests) — OAuth wizard flow, token validation, credential file handling, unified settings section, periodic health check
- **Section 55: Channel Infrastructure** (26 tests) — Channel ABC, ChannelCapabilities, ConfigField, registry lifecycle, media pipeline (transcribe/analyze/extract), tool factory generation, delivery routing and validation
- **Section 56: Telegram Phase 1** (26 tests) — voice/photo/document inbound handling, reaction emoji (👀/👍/💔), image gen delivery, interrupt buttons, auto-recovery, bot command registration

### 🔄 Other Changes

- **License** — switched from MIT to Apache 2.0 across the entire project
- **`channels/email.py` removed** — replaced by the generic channel architecture
- **`ui/render.py`** — `render_image_with_save()` for inline image thumbnails with download; `autolink_urls()` for bare URL wrapping
- **`ui/streaming.py`** — image generation side-channel capture; tool result image extraction pipeline
- **`ui/helpers.py`** — thread reload now filters empty-content AI messages that caused rendering errors

### 📁 Files Changed

| File | Change |
|------|--------|
| **`plugins/__init__.py`** | **New** — Package init; re-exports `load_plugins` and `get_load_summary` |
| **`plugins/api.py`** | **New** — Plugin author API: `PluginAPI` bridge and `PluginTool` base class |
| **`plugins/loader.py`** | **New** — Plugin discovery, validation, security scan, loading with timeout |
| **`plugins/manifest.py`** | **New** — Manifest parser and schema validator for `plugin.json` |
| **`plugins/registry.py`** | **New** — Plugin tool/skill registry with collision detection |
| **`plugins/state.py`** | **New** — State persistence for enable/disable, config, and secrets |
| **`plugins/sandbox.py`** | **New** — Dependency safety: freeze core deps, block downgrades |
| **`plugins/installer.py`** | **New** — Install, update, uninstall; fixed tuple unpacking in `_install_plugin_deps()` |
| **`plugins/marketplace.py`** | **New** — Marketplace client: fetch index, search, check updates |
| **`plugins/ui_settings.py`** | **New** — Plugins tab: card grid, reload button, missing key warnings; `clear_agent_cache()` on reload |
| **`plugins/ui_plugin_dialog.py`** | **New** — Per-plugin config dialog: details, API keys, settings, actions |
| **`plugins/ui_marketplace.py`** | **New** — Marketplace browse dialog; `_reload_plugins_and_agent()` auto-reload after install |
| **`channels/base.py`** | **New** — Channel ABC, `ChannelCapabilities`, `ConfigField` |
| **`channels/registry.py`** | **New** — Channel registry: register, discover, route, validate delivery |
| **`channels/media.py`** | **New** — Shared media pipeline: transcribe, analyze, extract, save |
| **`channels/tool_factory.py`** | **New** — Auto-generate LangChain tools per channel from capabilities |
| **`channels/config.py`** | Per-channel key-value config store |
| **`channels/telegram.py`** | Full upgrade: voice/photo/document inbound, reactions, image gen delivery, interrupt buttons |
| **`channels/email.py`** | **Removed** — replaced by generic channel architecture |
| **`tools/image_gen_tool.py`** | **New** — Image generation + editing via OpenAI/OpenRouter with side-channel rendering |
| **`tools/__init__.py`** | Added `image_gen_tool` import for registry auto-registration |
| **`app.py`** | Plugin loading, channel auto-start loop, periodic OAuth health check |
| **`agent.py`** | Plugin + channel tool injection; `clear_agent_cache()` export; background workflow gating |
| **`tasks.py`** | Delivery channels, model override, persistent threads, notify-only, skills override, schema migration |
| **`ui/settings.py`** | Plugins tab, marketplace dialog, Google Account wizard, channel config sections |
| **`ui/render.py`** | `render_image_with_save()`, `autolink_urls()` for inline images and URL linking |
| **`ui/streaming.py`** | Image gen side-channel capture, tool result image extraction |
| **`ui/helpers.py`** | Thread reload filters empty-content AI messages |
| **`ui/chat.py`** | Minor fix for drag-drop handler |
| **`ui/home.py`** | Removed legacy email status references |
| **`ui/status_checks.py`** | Removed legacy email health-check pill |
| **`LICENSE`** | MIT → Apache 2.0 |
| **`NOTICE`** | **New** — Apache 2.0 attribution file |
| **`test_suite.py`** | 168 new tests in sections 49–56 |

---

## v3.11.0 — Wiki Vault, Dream Cycle, Document Extraction & Knowledge Consolidation

Three major knowledge systems land in this release. **Wiki Vault** exports the entire knowledge graph as an Obsidian-compatible markdown vault with YAML frontmatter, wiki-links, and per-type indexes. **Dream Cycle** runs nightly background refinement — merging duplicate entities, enriching thin descriptions from conversation context, and inferring missing relationships — with a three-layer anti-contamination system that prevents cross-entity fact-bleed. **Document Knowledge Extraction** processes uploaded documents through a map-reduce LLM pipeline, extracting entities and relations into the knowledge graph with full source provenance. The Settings UI consolidates all knowledge features under a unified **Knowledge tab**, the graph panel gains source filtering and recency glow, and the status bar grows to 17 health-check pills.

### 📚 Wiki Vault (Obsidian Export)

The knowledge graph can now be exported as a structured markdown vault, compatible with Obsidian, VS Code, and any markdown editor.

- **Vault structure** — entities grouped by type (`wiki/person/`, `wiki/project/`, `wiki/event/`, etc.) with one `.md` file per entity; sparse entities (<20 chars) roll up into `_index.md` per type; per-type indexes and a master `index.md` auto-generated on rebuild
- **YAML frontmatter** — each article includes `id`, `type`, `subject`, `aliases`, `tags`, `source`, `created`, `updated` metadata
- **Wiki-links** — related entities linked via `[[Entity Name]]` syntax, enabling Obsidian backlinks and graph view
- **Connections section** — outgoing and incoming relations listed with arrow notation
- **Live export** — entities are exported on save (≥20 chars), deleted on entity removal, and rebuilt on batch operations
- **Search** — full-text search across all `.md` files with title, snippet, and entity ID results
- **Conversation export** — any thread can be exported as a vault-compatible markdown file
- **Agent tool** — 5 sub-tools (`wiki_search`, `wiki_read`, `wiki_rebuild`, `wiki_stats`, `wiki_export_conversation`) let the agent interact with the vault
- **Settings UI** — enable/disable toggle, vault path configuration with Browse button, stats display, rebuild and open-folder buttons

### 🌙 Dream Cycle (Nightly Knowledge Refinement)

A background daemon refines the knowledge graph during idle hours, running three non-destructive operations.

- **Duplicate merge** — entities with ≥0.93 semantic similarity and same type are merged; LLM synthesizes the best description, aliases are unioned, relations re-pointed to the survivor
- **Description enrichment** — thin entities (<80 chars) appearing in 2+ conversations get richer descriptions from conversation context and relationship graph
- **Relationship inference** — co-occurring entity pairs with no existing edge are evaluated for a meaningful connection (tagged `source="dream_infer"`)
- **Three-layer anti-contamination** — (1) sentence-level excerpt filtering extracts only sentences mentioning the target entity, (2) deterministic post-enrichment cross-entity validation scans LLM output for unrelated entity subjects and rejects contaminated results before DB write, (3) strengthened prompt with concrete negative examples and subject-name substitution
- **Subject-name guard** — entities with different normalized subjects require ≥0.98 similarity to merge, preventing false merges of distinct people/concepts
- **Configurable window** — default 1–5 AM local time; checks every 30 minutes if conditions met (enabled, in window, idle, not yet run today)
- **Dream journal** — all operations logged to `~/.thoth/dream_journal.json` with cycle ID, summary, and duration; viewable in the Activity tab
- **Settings UI** — enable/disable toggle, window display, last run summary in the Knowledge tab
- **Status pill** — new Dream Cycle health-check pill shows enabled state and last run time

### 📄 Document Knowledge Extraction (Map-Reduce Pipeline)

Uploaded documents are now processed through a three-phase LLM pipeline that extracts structured knowledge.

- **Map phase** — document split into ~6K-char windows; each window summarized to 3–5 sentences
- **Reduce phase** — window summaries combined into a coherent 300–600 word article
- **Extract phase** — core entities and relations pulled from the final article; 3–8 entities per document
- **Hub entity** — the document itself is saved as a `media` entity; extracted entities linked via `extracted_from` relation for provenance
- **Cross-window dedup** — entities with the same subject across windows are merged before saving
- **Live progress** — status bar shows pulsing progress pill with phase indicator, progress bar, queue count, and stop button (updates every 2 seconds)
- **Background queue** — documents queued for processing; worker thread handles one at a time
- **New file formats** — document upload now supports `.md`, `.html`, and `.epub` in addition to PDF, DOCX, and TXT
- **Per-document cleanup** — individual document delete button removes vector store entries and all extracted entities with matching source tag; bulk "Clear all documents" removes everything with `document:*` prefix

### 🧠 Knowledge Tab Consolidation

All knowledge management features are unified under a single **Knowledge** settings tab.

- **Renamed** — "Memory" tab → "Knowledge" tab throughout settings, home Activity panel, and status pills
- **Unified sections** — Memory Extraction settings, Wiki Vault settings, Dream Cycle settings, and Danger Zone all in one place
- **Activity panel** — shows extraction counters (threads scanned, entities saved, islands repaired), Dream Cycle window/status/last run, and up to 3 recent dream journal entries
- **Danger zone** — "Delete all knowledge" now clears entities, vector store, and wiki vault folder in one operation with confirmation dialog

### 🕸️ Knowledge Graph Visualization Enhancements

The graph panel gains filtering tools and visual indicators for entity provenance and recency.

- **Source filter pills** — toggleable `💬 chat` and `📄 documents` buttons filter nodes by origin
- **Recency glow** — node border width and color reflect how recently the entity was updated: bright amber (≤7 days), orange (7–30 days), dim brown (30–90 days), stale grey (90+ days)
- **User hub toggle** — show or hide the central User node
- **Hide unlinked toggle** — hide entities connected only to the User node, revealing natural clusters
- **Source border style** — document-sourced entities render with dashed borders
- **Detail card** — now shows source label and recency (e.g., "📄 document · 1 day ago")
- **Edge IDs** — `graph_to_vis_json()` now includes `id` field on edges for stable updates

### 🔗 Memory Tool Improvements

- **Subject-name arguments** — `link_memories` and `explore_connections` now accept entity **names** (preferred) instead of hex IDs; `_resolve_entity()` helper looks up by name first, falls back to ID
- **Contradiction detection** — `save_memory` runs LLM-based contradiction check before updating; if a conflict is detected, the agent returns a warning and asks the user which version is correct
- **Cross-entity overwrite guard** — system prompt guardrail prevents `update_memory` from overwriting a memory belonging to a different subject than the one being discussed
- **Retry on parallel calls** — `link_memories` includes 0.5s retry delay for parallel tool invocations that race against entity creation

### 🔧 Rendering Fixes

- **Mermaid diagram extraction** — fenced mermaid blocks are now extracted from text *before* `markdown2` processing (which was mangling them), rendered as `<pre class="mermaid">` elements, and processed by `mermaid.js` with a 100ms post-render delay
- **Streaming finalization** — all streamed messages now get unconditionally re-rendered at finalization (was previously gated on YouTube/mermaid detection), fixing code block syntax highlighting that only appeared on refresh

### 📊 Status Monitor Updates

- **17 health-check pills** — 3 new checks for Dream Cycle, TTS (Kokoro), and Wiki Vault; total up from 14
- **Renamed** — "Memory" pill → "Knowledge" pill
- **Tab routing fixes** — Disk pill now links to System tab; FAISS Index pill no longer links anywhere (informational only)
- **Extraction progress pill** — live document extraction progress with phase, bar, queue count, and stop button

### 📋 Bundled Skills Updated

- **Knowledge Base** — new bundled skill guiding the agent through the unified knowledge system (graph + documents + wiki)
- **Self-Reflection** — updated to reference `wiki_search` and `wiki_rebuild` for the reflection cycle
- **Deep Research** — added "Check Existing Knowledge" and "Save Key Findings" steps
- **Brain Dump** — added "Check Existing Knowledge" step to prevent duplicating facts
- **Meeting Notes** — references knowledge graph and wiki linking
- **Tool fields removed** — `tools:` field removed from all updated skill frontmatters (skills auto-discover tools)

### 🧪 Tests

- **974 PASS**, 0 FAIL, 1 WARN (up from 886 in v3.10.0)
- New: Wiki Vault (74 tests), Auto-Recall improvements, Wiki Tool (5 sub-tools), Bundled Skills validation, Document Knowledge Extraction (map-reduce, dedup, queue, cleanup), Wiki Cleanup & Knowledge Tab consolidation, Dream Cycle (config, journal, safety checks, 14 assertions), Status monitor count updates

### 📁 Files Changed

| File | Change |
|------|--------|
| **`wiki_vault.py`** | **New** — Obsidian-compatible markdown vault export: per-entity articles, YAML frontmatter, wiki-links, indexes, search, conversation export |
| **`tools/wiki_tool.py`** | **New** — Agent tool with 5 sub-tools: wiki_search, wiki_read, wiki_rebuild, wiki_stats, wiki_export_conversation |
| **`dream_cycle.py`** | **New** — Nightly knowledge refinement daemon: merge, enrich, infer with 3-layer anti-contamination, configurable window, dream journal |
| **`document_extraction.py`** | **New** — Background map-reduce LLM pipeline: split → summarize → extract entities; queue-based with live progress |
| **`bundled_skills/knowledge_base/SKILL.md`** | **New** — Bundled skill for the unified knowledge system |
| **`prompts.py`** | 8 new prompt templates: DOC_MAP/REDUCE/EXTRACT, DREAM_MERGE/ENRICH/INFER, updated EXTRACTION_PROMPT (10 entity types), cross-entity guardrail in UPDATING MEMORIES, search_documents→documents fix |
| **`knowledge_graph.py`** | `delete_entities_by_source()`, `delete_entities_by_source_prefix()`, `repair_graph_islands()`, edge IDs in vis JSON, `_updated_at`/`_source` fields on nodes, wiki vault auto-export on save/delete |
| **`documents.py`** | New loaders for `.md`, `.html`, `.epub`; `remove_document()` with source cleanup |
| **`tools/memory_tool.py`** | `_resolve_entity()` name-first lookup, `_check_contradiction()` LLM call, subject-name arguments on link/explore, 0.5s retry |
| **`memory_extraction.py`** | Calls `repair_graph_islands()`, extraction status counters (threads_scanned, entities_saved, islands_repaired) |
| **`ui/settings.py`** | Knowledge tab consolidation (Memory+Wiki+Dream Cycle), document upload triggers extraction queue, per-doc delete, Wiki Vault section, Dream Cycle section, danger zone clears wiki |
| **`ui/home.py`** | Activity panel: extraction counters, Dream Cycle status/journal, renamed Memory→Knowledge |
| **`ui/graph_panel.py`** | Source filter pills, recency glow, user hub toggle, hide unlinked toggle, source border style, detail card enhancements |
| **`ui/render.py`** | `_MERMAID_FENCE_RE`, `_split_mermaid()`, mermaid extraction before markdown2, `<pre class="mermaid">` rendering |
| **`ui/streaming.py`** | Unconditional re-render at finalization, `mermaid.run()` with 100ms delay |
| **`ui/status_bar.py`** | Document extraction progress pill with phase/bar/stop button |
| **`ui/status_checks.py`** | 3 new checks (Dream Cycle, TTS, Wiki Vault), Memory→Knowledge rename, Disk→System tab, FAISS unlinked |
| **`ui/chat.py`** | Drag-drop safety timer, document-level drop handler with Quasar guard |
| **`app.py`** | `start_dream_loop()` at startup |
| **`tools/__init__.py`** | `wiki_tool` import for registry auto-registration |
| **`bundled_skills/self_reflection/SKILL.md`** | References wiki_search + wiki_rebuild, removed tools field |
| **`bundled_skills/deep_research/SKILL.md`** | Added Check Existing Knowledge + Save Key Findings steps |
| **`bundled_skills/brain_dump/SKILL.md`** | Added Check Existing Knowledge step |
| **`bundled_skills/meeting_notes/SKILL.md`** | References knowledge graph + wiki linking |
| **`test_suite.py`** | 88 new tests across 7 sections (42–48); check count updates |
| **`integration_tests.py`** | New integration tests for document extraction + wiki vault |

---

## v3.10.0 — Status Monitor, Mermaid Diagrams, Image Persistence, Vision Files & Rich PDF Export

The home screen gets an interactive **status monitor panel** — a frosted-glass bar with an animated avatar, 14 health-check pills, and a one-click diagnosis button. Images now **survive thread reload** — pasted, captured, and attached images are persisted in per-thread sidecar files and rehydrated when you revisit a conversation. **Mermaid diagram rendering** brings flowcharts, sequence diagrams, and state diagrams to life inline in chat via mermaid.js. The **vision tool** gains `source='file'` for analyzing workspace image files by path, and the **filesystem tool** displays images inline when read. **PDF export** is upgraded to Playwright (headless Chromium) for full Unicode, emoji, chart, and styled markdown support. **OAuth token health checks** proactively validate Gmail and Calendar tokens at startup with silent refresh and periodic re-validation. A rewritten **Arxiv tool**, **clipboard image paste**, **right-click context menu** (pywebview), and a knowledge graph **opacity-based filter** round out the release.

### 📊 Status Monitor Panel

- **Animated avatar** — customizable emoji with conic-gradient spinning ring, ECG-synced glow pulses, and subtle wobble; ring color picker with 15 presets; config persisted in `~/.thoth/user_config.json`
- **14 health-check pills** — two centered rows covering Ollama, Active Model, Cloud API, Email, Telegram, Gmail OAuth, Calendar OAuth, Task Scheduler, Memory Extraction, Disk Space, Threads DB, FAISS Index, Document Store, and Network; color-coded (green/amber/red/grey) with tooltip detail
- **Click-to-settings** — clicking any pill opens the relevant settings tab
- **Diagnosis button** — runs all 14 checks on demand (icon spins during execution), opens a dialog with expandable results per service and a copy-to-clipboard report
- **ECG background** — animated heart-rate-monitor line scrolls behind the frosted-glass panel
- **Light/heavy check split** — 4 instant checks (Ollama, Model, Cloud API, Memory Extraction) always fresh; 10 heavier checks (network, OAuth, disk, DB) cached for 5 minutes

### 🖼️ Image Persistence

Images in chat messages now survive thread reload and app restart.

- **Per-thread sidecar files** — image payloads (base64) are saved to `~/.thoth/thread_ui/<thread_id>.images.json` alongside conversation checkpoints
- **Signature-based hydration** — on reload, images are matched back to their messages using content signatures with index fallback for checkpoint-reconstructed user messages
- **All image types covered** — pasted images, vision captures, browser screenshots, and file attachments are all persisted
- **Cleanup on delete** — sidecar files are removed when a thread is deleted
- **MIME-aware data URIs** — PNG, JPEG, GIF, and WebP images are detected by magic bytes and rendered with the correct MIME type

### 📊 Mermaid Diagram Rendering

Mermaid diagrams now render as interactive visual diagrams inline in chat.

- **mermaid.js integration** — bundled `static/mermaid.min.js` loaded in head HTML with `securityLevel: 'strict'` and dark theme
- **Auto-fence detection** — `_auto_fence_mermaid()` in `ui/render.py` detects unfenced Mermaid syntax (graph, flowchart, sequenceDiagram, classDiagram, erDiagram, stateDiagram, gantt, mindmap, timeline, pie) and wraps it in ` ```mermaid ` fences before rendering
- **Streaming support** — `_format_assistant_markdown()` chains auto-fence + URL auto-linking on all streaming `set_content()` calls
- **Post-render swap** — after markdown rendering, `<pre><code class="language-mermaid">` blocks are swapped to `<div class="mermaid-rendered">` and processed by `mermaid.run()`
- **Chart tool guard** — requests for Mermaid diagram types (flow, sequence, state, ER, etc.) in `create_chart` are caught early with a helpful error message redirecting to fenced Mermaid blocks

### 👁️ Vision: Image File Analysis

The vision tool now analyzes image files in the workspace without needing a camera or screen capture.

- **`source='file'` parameter** — new source option on `analyze_image` with `file_path` argument for workspace-relative or absolute paths
- **Path resolution** — tries absolute path, then workspace root (from filesystem tool config), then current working directory
- **Prompt routing** — system prompt updated to guide the model: use `source='file'` for workspace images, don't re-analyze already-attached images
- **Filesystem inline display** — `workspace_read_file` on image files (PNG, JPEG, GIF, WebP, BMP, TIFF, SVG) displays the image inline in chat and returns a hint to use `analyze_image` for content analysis

### 📄 Rich PDF Export

PDF export upgraded from basic fpdf2 text to full-fidelity Playwright rendering.

- **Playwright-first** — conversation export and `export_to_pdf` filesystem tool both use headless Chromium for full Unicode, emoji, embedded images, Plotly charts, styled markdown tables, and syntax-highlighted code blocks
- **Automatic fallback** — if Playwright is unavailable, falls back to the basic fpdf2 text-only renderer
- **Separate browser instance** — PDF rendering uses `headless=True` in a thread pool worker — does not interfere with the visible BrowserTool browser
- **Professional styling** — A4 layout, system fonts, color-coded roles (blue for User, gold for Thoth), collapsible tool-result blocks, responsive images

### 🔑 OAuth Token Health Checks

Gmail and Calendar OAuth tokens are now proactively monitored.

- **Startup check** — on launch, enabled Gmail/Calendar tools have their tokens validated; expired access tokens are silently refreshed
- **Periodic re-check** — APScheduler job runs every 6 hours to catch tokens that expire mid-session
- **Granular status** — `check_token_health()` on both tools returns `valid`, `refreshed`, `expired`, `missing`, or `error` with detail
- **Settings UI feedback** — Gmail and Calendar settings tabs show token status (healthy, refreshed, expired, error) instead of a generic "✅ Authenticated"
- **User-facing warnings** — expired tokens trigger desktop notifications and in-app toasts with re-authentication instructions

### 📚 Arxiv Tool Rewrite

The Arxiv tool is rewritten from scratch — no longer uses `ArxivRetriever`.

- **Direct `arxiv` package** — uses `arxiv.Client` with rate-limiting (`delay_seconds=3.0`) and retries
- **Newest-first sorting** — results sorted by `SubmittedDate` descending
- **Rich output** — title, authors (truncated at 5 with "et al."), published date, primary category, abstract, full-text HTML link, PDF link, and source URL per result
- **Version-stripped HTML URLs** — `arxiv.org/html/<id>` links strip the version suffix for clean access
- **Query syntax hints** — tool description mentions `ti:`, `au:`, `abs:`, `cat:` arXiv query syntax

### 📋 Clipboard Image Paste

- **Ctrl+V paste support** — paste images directly from the clipboard into chat; images are converted to file uploads with timestamped names (e.g. `pasted_image_1712345678.png`)
- **Singleton listener** — paste handler installs once and reads the dynamic upload widget ID, surviving thread switches without duplicate bindings

### 🖱️ Right-Click Context Menu (pywebview)

- **Custom context menu** — Cut, Copy, Paste, and Select All in the native desktop window, since pywebview suppresses the browser's default context menu
- **pywebview-only** — only activates inside pywebview; normal browsers keep their native context menu
- **Clipboard integration** — Paste reads from `navigator.clipboard` and inserts via `execCommand`

### 🕸️ Knowledge Graph Filter Overhaul

- **Opacity-based filtering** — search and entity-type filters now dim non-matching nodes/edges (opacity 0.12) instead of rebuilding the entire network, preserving layout stability and spatial context
- **Edge dimming** — edges between non-matching nodes fade to 0.06 opacity; edges connecting two matching nodes stay fully visible

### 🔧 Other Improvements

- **Immediate user message rendering** — file attachments are now processed asynchronously; the user message (with 📎 badges and image thumbnails) appears instantly while vision analysis runs in the background with a "🔍 Analyzing image..." indicator
- **Browser screenshot persistence** — browser screenshots taken during tool execution are added to `captured_images`, persisted via the image sidecar system, and restored on reload
- **Terminal chevron fix** — inline terminal panel expand/collapse chevron direction corrected (was inverted)
- **Drag-and-drop singleton** — drag-and-drop file handler installs once and reads the dynamic upload widget ID, preventing duplicate handlers across thread switches
- **Context window minimum** — minimum context size raised from 4K to 16K tokens; legacy values below 16K auto-clamp
- **Notify-only tasks** — tasks with `notify_only` flag skip thread creation, reducing clutter for simple timer/notification tasks
- **Skill editor simplified** — removed tool-dependency checkboxes from the skill editor UI (tools declared in SKILL.md frontmatter are informational, not enforced)

### 🧪 Tests

- **886 PASS**, 0 FAIL, 1 WARN (up from 842 in v3.9.0)
- New: Status monitor panel (20 tests), OAuth token health checks (7 tests), Arxiv tool rewrite (6 tests), image persistence & hydration, Mermaid auto-fence, PDF export (Playwright + fallback), filesystem image display, vision file analysis, streaming format pipeline, badge parsing

### 📁 Files Changed

| File | Change |
|------|--------|
| **`ui/status_checks.py`** | **New** — 14 health-check functions with `CheckResult` dataclass, `ALL_CHECKS`/`LIGHT_CHECKS`/`HEAVY_CHECKS` registries |
| **`ui/status_bar.py`** | **New** — Status bar UI: avatar, pills, diagnosis dialog, ECG animation, avatar picker |
| **`ui/home.py`** | Logo replaced with `build_status_bar()` call; `open_settings` callback wired in |
| **`threads.py`** | Per-thread image sidecar I/O (`save_thread_ui_images`, `load_thread_ui_images`, `_thread_ui_images_path`); cleanup in `_delete_thread` |
| **`ui/helpers.py`** | `persist_thread_image_state()`, `_hydrate_thread_images()` with signature + index matching; `strip_file_context` badge parsing for "ALREADY ANALYZED" markers; Playwright-based `_render_pdf_playwright()` conversation PDF export with `_build_conversation_html()`; fpdf2 fallback |
| **`ui/render.py`** | `_img_data_uri()` MIME detection; `_auto_fence_mermaid()` with `_MERMAID_START_RE` and `_is_mermaid_continuation_line()`; Mermaid post-render JS swap; wired into `render_text_with_embeds` and `render_message_content` |
| **`ui/streaming.py`** | `_format_assistant_markdown()` chains auto-fence + autolink on all streaming content; `_img_data_uri()` for screenshot display; `persist_thread_image_state` calls after user/assistant messages; filesystem image display via `get_and_clear_displayed_image()`; immediate user message rendering with async file processing; Mermaid post-render JS |
| **`ui/chat.py`** | Clipboard image paste JS listener; drag-and-drop singleton fix; `persist_thread_image_state` on detached generation reattach; terminal chevron direction fix |
| **`ui/head_html.py`** | `mermaid.min.js` script tag + `mermaid.initialize()` with dark theme and strict security; `.mermaid-rendered` CSS; right-click context menu JS (pywebview-only) |
| **`ui/graph_panel.py`** | Opacity-based filter/search using `ds.update()` instead of network rebuild |
| **`ui/settings.py`** | Gmail/Calendar token health status display; skill editor: removed tool-dependency checkboxes, moved Create button to top |
| **`tools/arxiv_tool.py`** | Full rewrite — `execute()` using `arxiv.Client` directly; removed `get_retriever`/`ArxivRetriever`; newest-first sorting, HTML links, rate limiting |
| **`tools/chart_tool.py`** | `_MERMAID_DIAGRAM_TYPES` guard in `_create_chart`; updated tool description to exclude Mermaid |
| **`tools/filesystem_tool.py`** | Image file inline display via `_last_displayed_image` buffer + `get_and_clear_displayed_image()`; Playwright-first `export_to_pdf` with fpdf2 fallback |
| **`tools/vision_tool.py`** | `source='file'` + `file_path` parameter on `analyze_image`; updated schema and description |
| **`tools/gmail_tool.py`** | `_check_google_token()` with silent refresh; `check_token_health()` method |
| **`tools/calendar_tool.py`** | `_check_google_token()` with silent refresh; `check_token_health()` method |
| **`tools/memory_tool.py`** | `explore_connections` description updated to "Mermaid graph diagram" |
| **`tools/browser_tool.py`** | Minor cleanup |
| **`vision.py`** | `source='file'` support in `capture_and_analyze()`; `_analyze_from_file()` and `_resolve_image_path()` helpers; source-aware question prefixes |
| **`app.py`** | `_check_oauth_tokens()` startup check; `_periodic_oauth_check()` scheduled every 6 h; passes `open_settings` to `build_home()` |
| **`models.py`** | Removed 4K/8K context options; auto-clamp legacy values below 16K |
| **`prompts.py`** | Vision `source='file'` routing; attached image "do NOT re-analyze" guidance; `workspace_read_file` image support mention |
| **`tasks.py`** | `notify_only` tasks skip thread creation |
| **`skills.py`** | Removed tool-dependency enforcement from `update_skill`/`create_skill` |
| **`ui/export.py`** | Minor fix |
| **`ui/sidebar.py`** | Minor update |
| **`ui/setup_wizard.py`** | Minor fix |
| **`static/mermaid.min.js`** | **New** — bundled Mermaid.js library |
| **`test_suite.py`** | 46 new tests covering status monitor (20), OAuth, Arxiv, image persistence, Mermaid, PDF, filesystem images, vision files, streaming |
| **`README.md`** | Updated for all new features; test badge 842→868; version references updated |

---

## v3.9.0 — Modular UI, Thinking Models & Cloud Model Expansion

Thoth's monolithic 6,500-line frontend is now a **clean modular architecture** — `app.py` + a `ui/` package of 15 focused modules. **Thinking model support** lands with full reasoning-token extraction, collapsible thinking bubbles, and persistence across thread reloads. **OpenRouter gets first-class support** via `ChatOpenRouter`, and a new **Data Analyst** bundled skill rounds out the skill library to 10. Multiple rendering fixes (URL auto-linking, YouTube embeds) and a privacy improvement round out the release.

### 🏗️ UI Modularization

The monolith `app_nicegui.py` (6,535 lines) has been replaced by `app.py` + `ui/` package using a strangler-fig migration pattern.

- **15 focused modules** — `state.py` (dataclasses), `constants.py`, `head_html.py`, `helpers.py` (config, file processing, exports), `render.py` (message rendering), `streaming.py` (generation consumer, send/interrupt), `setup_wizard.py`, `settings.py`, `graph_panel.py` (knowledge graph vis), `sidebar.py`, `home.py`, `tasks_ui.py`, `voice_bar.py`, `export.py`, `__init__.py`
- **Zero functionality loss** — every feature from the monolith is preserved; all imports resolve cleanly
- **Launcher updated** — `launcher.py`, both installer scripts (Windows ISS + macOS build), CI workflow, test suite, and all documentation updated to reference the new entry point

### 💡 Thinking Model Support

Full support for reasoning models (DeepSeek-R1, Qwen3, QwQ, etc.) across local and cloud providers.

- **Reasoning token extraction** — `additional_kwargs["reasoning_content"]` is extracted from streaming chunks before content, surfacing the model's chain-of-thought in real time
- **`reasoning=True`** — all four `ChatOllama` instantiation sites now enable native reasoning mode
- **`<think>` tag stripping** — models that embed `<think>…</think>` blocks in content have them separated into thinking tokens and stripped from the visible response
- **Collapsible thinking bubble** — during streaming, thinking content displays live in italic at 55% opacity, then auto-collapses into a `💭 Thinking` expansion with `psychology` icon when the real response begins
- **Thinking persistence on thread reload** — `load_thread_messages()` now recovers reasoning content from both `additional_kwargs` and `<think>` tags in the LangGraph checkpoint; historical messages render a collapsed thinking expansion matching the live-streaming style

### ☁️ Cloud Model Expansion

- **ChatOpenRouter** — OpenRouter models now use `langchain-openrouter`'s dedicated `ChatOpenRouter` class instead of the generic `ChatOpenAI` wrapper, enabling proper provider-specific features
- **New dependency** — `langchain-openrouter` added to `requirements.txt`

### 📊 Data Analyst Skill

- **New bundled skill** — `bundled_skills/data_analyst/SKILL.md` (v1.1) — guides the agent through dataset analysis, statistical summaries, and insightful Plotly chart creation
- **10 bundled skills total** — Brain Dump, Daily Briefing, Data Analyst, Deep Research, Humanizer, Meeting Notes, Proactive Agent, Self-Reflection, Task Automation, Web Navigator

### 🔗 Rendering Fixes

- **URL auto-linking** — bare `https://` URLs in messages now automatically render as clickable links; a regex preprocessor safely skips URLs already inside markdown links, angle brackets, inline code, or fenced code blocks
- **YouTube embed fix** — `render_text_with_embeds()` rewritten to match the full `**[text](youtube_url)**` context, eliminating `**` and `)**` artifacts that appeared when YouTube links were wrapped in markdown bold/link syntax

### 📊 Chart Tool Fixes

- **Reliable chart rendering** — chart tool improvements for consistent Plotly chart creation and inline display

### 🔒 Privacy

- **User content removed from logs** — `send_message()` no longer logs `agent_input_preview` (the first 200 characters of the user's message); log now shows only file names and content lengths

### 📁 Housekeeping

- **`workflows.py` removed** — fully superseded by `tasks.py` since v3.5.0; dead code deleted
- **Version bump** — v3.8.0 → v3.9.0 across installers, CI, documentation, and landing page
- **Test suite** — all `app_nicegui` references updated to `app`

### 📁 Files Changed

| File | Change |
|------|--------|
| **`app.py`** | **Renamed** from `app_v2.py` — modular entry point, port 8080, title "Thoth" |
| **`ui/`** | **New** — 15-module UI package extracted from monolith |
| **`app_nicegui.py`** | **Deleted** — archived as `.bak` |
| **`workflows.py`** | **Deleted** — dead code, superseded by `tasks.py` |
| **`agent.py`** | Thinking/reasoning token extraction from `additional_kwargs["reasoning_content"]`; `<think>` tag separation |
| **`models.py`** | `reasoning=True` on all `ChatOllama` calls; `ChatOpenRouter` for OpenRouter cloud models |
| **`requirements.txt`** | Added `langchain-openrouter` |
| **`tools/chart_tool.py`** | Chart creation and rendering fixes |
| **`prompts.py`** | System prompt refinements |
| **`bundled_skills/data_analyst/`** | **New** — Data Analyst skill v1.1 |
| **`launcher.py`** | References updated `app_nicegui.py` → `app.py` |
| **`installer/thoth_setup.iss`** | Version 3.9.0; `app_nicegui.py` → `app.py`; added `ui\` package (15 files) |
| **`installer/build_mac_app.sh`** | Version 3.9.0; added `ui` to rsync; removed `app.py` from skip list |
| **`installer/build_installer.ps1`** | Version 3.9.0 |
| **`.github/workflows/release.yml`** | `DEFAULT_VERSION` → 3.9.0 |
| **`test_suite.py`** | 67× `app_nicegui` → `app`; docstring version v3.9.0 |
| **`README.md`** | Architecture diagram, module table, installer filenames updated; skills count 10; models.py description updated |
| **`docs/index.html`** | Download links v3.9.0; skills 9→10; new Thinking Models feature card; footer version |
| **`installer/README.md`** | Version reference updated |
| **`memory.py`**, **`tts.py`**, **`tasks.py`**, **`vision.py`** | Comment/docstring references updated |

---

## v3.8.0 — Bundled Skills, Memory Intelligence & Self-Contained Installers

Thoth ships with **9 bundled skills** — reusable instruction packs that shape how the agent thinks and responds. The memory system gets smarter with **auto-linking, FAISS fallback search, background orphan repair, and memory decay**. Token counting is now accurate via **tiktoken**, and the agent dynamically adjusts its tool set based on available context. Installers are now fully **self-contained** (no post-install downloads), and a new **CI/CD pipeline** automates builds, code signing, notarization, and GitHub Releases.

### 🧩 Bundled Skills Engine

New `skills.py` engine and `bundled_skills/` directory — a system for packaging and injecting domain-specific instructions into the agent's behavior.

- **SKILL.md format** — each skill is a Markdown file with YAML frontmatter (`display_name`, `icon`, `description`, `tools`, `tags`, `version`, `author`, `enabled_by_default`) followed by freeform instructions
- **9 bundled skills** — 🧠 Brain Dump, ☀️ Daily Briefing, 🔬 Deep Research, 🗣️ Humanizer, 📋 Meeting Notes, 🎯 Proactive Agent, 🪞 Self-Reflection, ⚙️ Task Automation, 🌐 Web Navigator
- **Two-tier discovery** — bundled skills ship read-only in `<app_root>/bundled_skills/`; user skills in `~/.thoth/skills/` override bundled skills by name
- **Prompt injection** — enabled skills have their instructions injected into the system prompt before every LLM call
- **Per-skill enable/disable** — toggle skills from Settings → Skills tab; config persisted in `~/.thoth/skills_config.json`
- **Tool-aware** — each skill declares the tools it uses (`tools` field in frontmatter)
- **In-app skill editor** — create and edit user skills from Settings → Skills with a visual form — name, icon, description, tools, and freeform instructions; no need to manually create `SKILL.md` files
- **Cache & reload** — skills are cached in memory after first load; `load_skills(force_refresh=True)` forces a re-scan

### 🧠 Memory Intelligence

Four improvements to the knowledge graph that make memory recall smarter and the graph healthier.

- **Auto-link on save** — when a new entity is saved, the engine automatically scans existing entities for potential relationships and creates links, building the knowledge graph organically without manual `link_memories` calls
- **FAISS fallback search** — if the primary semantic recall returns no results above the 0.80 similarity threshold, a broader relaxed search is attempted automatically; prevents empty recall on edge-case queries
- **Background orphan repair** — a periodic background process detects entities with zero relationships and attempts to link them to related entities, keeping the knowledge graph connected over time
- **Memory decay** — memories that haven't been recalled recently are gradually deprioritized in retrieval results, ensuring frequently relevant information surfaces first

### 📏 Accurate Token Counting & Dynamic Tool Budgets

Context window management is now more precise and adaptive.

- **tiktoken integration** — token counting uses OpenAI's `tiktoken` library (cl100k_base encoding) instead of character-based estimates; the live token counter and all trimming decisions are now accurate to the token
- **Dynamic tool budgets** — the agent automatically adjusts how many tools are exposed to the model based on available context headroom; when context usage is high, lower-priority tools are temporarily hidden to prevent the system prompt from crowding out conversation history
- **Cloud model context fix** — `contextvars.ContextVar` now correctly propagates model overrides through the full agent pipeline, fixing a bug where cloud model threads could miscalculate available context

### 📦 Self-Contained Installers

Both Windows and macOS installers now bundle all dependencies at build time — no post-install downloads.

- **Windows (`build_installer.ps1`)** — patches Python's `._pth` file, installs pip, and runs `pip install -r requirements.txt` into the bundled Python during the build step; `install_deps.bat` and `get-pip.py` removed from the installer
- **macOS (`build_mac_app.sh`)** — new self-contained build script using python-build-standalone; downloads a standalone Python, installs all pip deps, assembles a `.app` bundle with entitlements, code-signs, and creates a `.pkg` installer
- **Inno Setup (`thoth_setup.iss`)** — updated to include `bundled_skills/` and `workflows.py`; removed post-install dependency download steps

### 🔄 CI/CD Pipeline

New `.github/workflows/release.yml` — automated build, sign, notarize, and release.

- **Trigger** — tag push (`v*`) or manual `workflow_dispatch`
- **Test stage** — runs full test suite before building
- **Parallel builds** — Windows (Inno Setup) and macOS (build_mac_app.sh) build in parallel
- **macOS code signing** — signs the `.app` and `.pkg` with Apple Developer certificates (Application + Installer)
- **macOS notarization** — submits the `.pkg` to Apple for notarization and staples the ticket
- **GitHub Release** — creates a draft release with both platform installers attached
- **6 GitHub secrets** — `APPLE_CERTIFICATE_P12`, `APPLE_INSTALLER_P12`, `APPLE_CERT_PASSWORD`, `APPLE_ID`, `APPLE_TEAM_ID`, `APPLE_APP_PASSWORD`

### 🐛 Bug Fixes

- **Cloud model override propagation** — `contextvars.ContextVar` replaces thread-local storage for model overrides, fixing context window miscalculation in cloud model threads
- **User entity prompt** — memory extraction prompt updated to fix entity naming for the canonical "User" node
- **Memory content merge** — fixed a bug where merging duplicate entities could lose content from the richer entry

### 🌐 Per-Thread Browser Tabs & Background Browsing

Browser automation now works in background tasks. Each thread (interactive chat or scheduled task) gets its own isolated browser tab.

- **Per-thread tab isolation** — replaced the single shared page with a `_thread_pages` dict; each thread claims or creates its own tab; the agent never hijacks tabs belonging to other threads
- **Blank-page-only claiming** — only pages at `about:blank` or `chrome://newtab/` are eligible for claiming; pages with content from prior sessions are never auto-claimed
- **Background browsing** — removed `_block_if_background()` entirely; browser tools now work in background tasks through per-thread tab isolation
- **Browser crash recovery** — if the browser is closed externally, a `disconnected` handler detects it, clears stale state, and the next browser action automatically relaunches the session
- **Retry on close** — `_run_on_pw_thread()` catches "has been closed" errors, resets the session, and retries once
- **Tab cleanup on task completion** — `run_task_background` finally block calls `kill_session(thread_id)` to close the task's tab
- **Screenshot thread-awareness** — `take_screenshot(thread_id)` uses a new `get_page_for_screenshot()` that never creates tabs or steals focus from other threads

### 📊 Monitoring / Polling Tasks

New task pattern for monitoring conditions and self-disabling when met.

- **`{{task_id}}` template variable** — `expand_template_vars()` now supports `{{task_id}}`; lets prompts reference their own task for self-management
- **System prompt triage** — 4-line monitoring hint helps the agent distinguish "check X and notify me when Y" (monitoring task) from simple reminders
- **SKILL.md guidance** — Task Automation skill gained items 17–21: interval schedules, conditional prompts, persistent threads, polling template, self-disable vs self-delete

### 🔴 Error Notification Improvements

API errors are now visible, persistent, and survive thread refresh.

- **Red persistent toast** — `notify()` gained a `toast_type` parameter; API errors fire `toast_type="negative"` → red banner, no auto-dismiss, close button
- **Error persistence in checkpoint** — error messages are written to the LangGraph checkpoint via `update_state()` so they appear when the thread is refreshed or revisited
- **Content normalization** — `_normalise_content()` handles gpt-5.4 list-type `AIMessage.content` in streaming and memory extraction

### 🛡️ Agent Robustness

- **Recursion limits** — raised from 25 to 50 (interactive) / 100 (background tasks); wind-down warning injected at 75% asking the model to wrap up; 4× repeated tool-call loop detection
- **Thread rendering fix** — `load_thread_messages()` now handles interrupted tool-call loops (orphaned `ToolMessage` without matching `AIMessage`)

### 🧪 Tests

- **842 PASS**, 0 FAIL, 2 WARN (up from 841 in v3.8.0 baseline)
- New: per-thread tab isolation test (19g), `{{task_id}}` expansion test (24j2)
- Updated: `kill_session` assertion (19e), security audit assertion (32g)
- Removed: `_block_if_background` test (replaced by per-thread tabs)
- Context-size-aware browser snapshot test scaling

### 📁 Files Changed

| File | Change |
|------|--------|
| **`skills.py`** | **New** — skills engine: YAML frontmatter parsing, bundled + user skill discovery, enable/disable config, prompt building, caching |
| **`bundled_skills/`** | **New** — 9 skill directories, each with `SKILL.md` (Brain Dump, Daily Briefing, Deep Research, Humanizer, Meeting Notes, Proactive Agent, Self-Reflection, Task Automation, Web Navigator) |
| **`agent.py`** | Dynamic tool budgets based on context headroom; tiktoken-based token counting; `contextvars.ContextVar` for model override propagation; skills prompt injection in pre-model hook; content normalization for list-type `AIMessage.content`; API error surfacing with `toast_type="negative"`; recursion limits 50/100 with wind-down and loop detection |
| **`app_nicegui.py`** | Thread rendering fix for interrupted tool loops; error persistence to LangGraph checkpoint via `update_state()`; red persistent error toasts; screenshot passes `thread_id`; `AIMessage` import |
| **`notifications.py`** | `toast_type` parameter on `notify()` (default `"positive"`); toast queue carries `toast_type`; `drain_toasts()` returns dicts with type |
| **`tools/browser_tool.py`** | Per-thread tab isolation (`_thread_pages` dict, `_BLANK_URLS` claiming filter); `get_page_for_screenshot()`; `release_thread()`; crash recovery (`_on_close` handler, retry logic); removed `_block_if_background()`; all 7 actions accept `thread_id` |
| **`tasks.py`** | `{{task_id}}` in `expand_template_vars()`; browser tab cleanup in finally block |
| **`tools/task_tool.py`** | `_TaskCreateInput.prompts` description mentions `{{task_id}}` |
| **`prompts.py`** | 4-line monitoring/polling triage hint; `{{task_id}}` in template variables list |
| **`bundled_skills/task_automation/SKILL.md`** | Monitoring / Polling section (items 17–21) |
| **`memory_extraction.py`** | Content normalization for list-type `AIMessage.content`; user entity prompt fix; content merge bug fix |
| **`knowledge_graph.py`** | Auto-link on save; FAISS fallback search with relaxed threshold; background orphan repair; memory decay scoring |
| **`models.py`** | `contextvars.ContextVar` for cloud model override |
| **`installer/build_installer.ps1`** | Pre-installs pip deps at build time; patches `._pth` file |
| **`installer/build_mac_app.sh`** | **New** — self-contained macOS build with python-build-standalone, code signing, `.pkg` creation |
| **`installer/entitlements.plist`** | **New** — macOS hardened runtime entitlements |
| **`installer/thoth_setup.iss`** | Removed post-install downloads; added `bundled_skills/` and `workflows.py` |
| **`.github/workflows/release.yml`** | **New** — CI/CD: test → build → sign → notarize → GitHub Release |
| **`.gitignore`** | Added `installer/apple_signing/` |
| **`test_suite.py`** | ~101 new tests across skills, memory intelligence, tool budgets, tiktoken, per-thread tabs, `{{task_id}}`, error persistence |
| **`requirements.txt`** | Added `tiktoken` |
| **`README.md`** | Added Skills section, updated Memory/Agent/Architecture docs, browser per-thread tabs, monitoring/polling tasks, error notification improvements, updated safety section, test count badge |

---

## v3.7.0 — Cloud-Primary Mode, Per-Thread Model Switching & Task Stop

Thoth now works **without Ollama**. Connect your OpenAI or OpenRouter API key and use cloud models (GPT-4o, Claude, Gemini, etc.) as your default — or mix cloud and local models across different conversations. A new **per-thread model picker** lets you switch models mid-conversation, and a **task stop** feature lets you cancel running tasks at any point.

### ☁️ Cloud-Primary Mode

New `models.py` cloud engine — Thoth can now run entirely on cloud LLMs with no local Ollama dependency.

- **Dual-provider support** — connect OpenAI (direct API) and/or OpenRouter (100+ models from all major providers); keys stored in `api_keys.json` and managed via Settings → Cloud
- **Setup wizard** — fresh installs present two paths: **🖥️ Local (Ollama)** or **☁️ Cloud (API key)**; cloud path validates keys, fetches available models, and lets you pick a default — no Ollama needed
- **Starred models** — star your favorite cloud models in Settings → Cloud; starred models appear in the chat header model picker alongside local models
- **Cloud-first startup** — when the default model is cloud, Thoth skips Ollama auto-start entirely; no "Ollama not found" warnings on machines without it
- **Context-size catalog** — OpenRouter model metadata is cached locally; for OpenAI models (which don't expose context length), a built-in heuristic table covers GPT-4o/4.1/4.5/5, o1/o3/o4, Claude 2–4, and Gemini 2–3 families
- **Cloud vision detection** — cloud models with vision capability (e.g. `gpt-4o`, `claude-3.5-sonnet`) are auto-detected from provider metadata; the vision tool works seamlessly with cloud models
- **Privacy controls** — Settings → Cloud includes toggles for auto-recall, memory extraction, and conversation history; memory extraction defaults to OFF for cloud threads

### 🔀 Per-Thread Model Switching

Every conversation can now use a different model — cloud or local.

- **Chat header model picker** — dropdown in the chat header shows: "Default (current model)" + starred cloud models + local Ollama models; selecting a model sets the override for that thread only
- **Thread-level persistence** — `model_override` column added to `thread_meta` (auto-migrated); overrides survive app restarts
- **Cloud warning banner** — when a thread uses a cloud model, a colored banner shows: "☁️ Using gpt-4o via OpenAI — data is sent to the cloud"
- **Sidebar icons** — threads show ☁️ (cyan) for cloud models, 🖥️ (grey) for local models
- **Reset to default** — selecting "Default" in the picker clears the override; thread reverts to the app-wide default model
- **Summarization uses override** — context compression uses the thread's override model, not the global default
- **Telegram /model command** — `/model` lists available models; `/model gpt-4o` switches; `/model default` resets; invalid model names show an error with available options

### ⏹️ Task Stop / Cancel

Running tasks can now be stopped from the UI at any point during execution.

- **Node-level cancellation** — when a task has a `stop_event`, `invoke_agent()` uses `agent.stream(stream_mode="updates")` instead of `agent.invoke()`, checking the stop event between every LangGraph node; tasks stop between steps, not mid-LLM-call
- **`TaskStoppedError`** — new exception raised when a stop is detected; caught by the task runner for clean shutdown
- **`stop_task(thread_id)`** — signals the stop event for a running task; returns `True` if found
- **Three stop buttons** — red stop button in: (1) chat header when viewing a running task's thread, (2) Activity tab "Running Now" section per task, (3) task card (replaces the play button while running)
- **Stopped state** — stopped tasks are recorded as status "stopped" in run history; thread is renamed with "(stopped)"; orange `stop_circle` icon in Recent Runs; notification sent; delivery and auto-delete are skipped
- **Delete stops task** — deleting a thread while a task is running now signals `stop_task()` first; thread stays deleted (no ghost re-creation)
- **Thread existence guard** — task completion/stop handlers check if the thread still exists before renaming, preventing `INSERT ON CONFLICT` from re-creating deleted threads
- **Orphaned tool-call repair** — if stopped mid-tool-call, orphaned tool calls are auto-repaired before the thread is finalized
- **Backward compatible** — when `stop_event` is `None` (chat, Telegram, CLI), `invoke_agent()` uses the original `agent.invoke()` path unchanged

### 🔧 Displaced Tool-Call Repair

New repair logic in `invoke_agent()` fixes a class of LangGraph checkpoint corruption bugs.

- **Problem** — `trim_messages` or checkpoint corruption can displace `ToolMessage` responses away from their parent `AIMessage` with `tool_calls`, violating OpenAI's strict ordering requirement (tool_calls must be immediately followed by their ToolMessages)
- **Fix** — after trimming, a scan detects AIMessages whose tool_calls are not immediately followed by matching ToolMessages; stubs are injected in the correct position and displaced originals are removed
- **Auto-retry on orphan errors** — both `invoke_agent()` and `_stream_graph()` catch "tool_call without response" errors, run `repair_orphaned_tool_calls()`, and retry once automatically

### ⚡ FAISS Rebuild Optimization

Reduced redundant FAISS index rebuilds during memory extraction.

- **Before** — `_dedup_and_save()` called `rebuild_index()` at the end of each thread's extraction; processing 4 threads meant 4 full FAISS rebuilds (re-embedding all entities each time)
- **After** — `rebuild_index()` moved to `run_extraction()`, called once after all threads are processed; per-entity upserts are still suppressed via `_skip_reindex` during batch processing
- **Incremental upsert** — new `_upsert_index()` in `knowledge_graph.py` adds/updates a single entity vector without rebuilding the entire index; used for individual memory saves outside of batch extraction

### 🐛 Bug Fixes

- **Scheduled tasks missing thread** — `_on_task_fire()` now calls `_save_thread_meta()` and `_set_thread_model_override()` before `run_task_background()`, matching the manual-run handler; previously scheduled tasks never created a `thread_meta` row, so threads never appeared in the sidebar and the completion handler's `_thread_exists()` guard silently skipped the final save
- **Telegram displaced tool_call** — Telegram channel now propagates `model_override` from thread config to the LangGraph configurable, fixing "tool_call without response" errors when using cloud models via Telegram
- **Memory system concurrent access** — additional `threading.Lock()` protection around FAISS operations during incremental upserts
- **Email channel import** — fixed minor import path issue in `channels/email.py`
- **Conversation search tool** — minor fix for result formatting
- **Voice module** — minor compatibility fix

### 🧪 Tests

- **745 PASS**, 0 FAIL, 2 WARN (up from 676 in v3.6.0)
- New test sections: Cloud model engine (model detection, provider routing, context heuristics, starred models, vision detection)
- New test sections: Per-thread model override (DB migration, override persistence, picker logic, cloud banner, sidebar icons)
- New test sections: Task stop (TaskStoppedError, stop_event propagation, stop_task(), get_running_task_thread(), stopped state handling, thread existence guard, delete-while-running)
- New test sections: Displaced tool-call repair (stub injection, displaced ToolMessage removal, ordering validation)
- New test sections: FAISS incremental upsert, rebuild optimization
- Extended integration tests for cloud model routing and Telegram /model command

### 📁 Files Changed

| File | Change |
|------|--------|
| **`models.py`** | **Major** — cloud model engine: dual-provider support (OpenAI + OpenRouter), model fetching/caching, starred models, context-size catalog + heuristics, cloud vision detection, `get_llm_for()` / `_get_cloud_llm()` / `is_cloud_model()` / `get_cloud_provider()` |
| **`agent.py`** | **Major** — `TaskStoppedError` exception; `invoke_agent()` rewritten with `stop_event` param and node-level streaming path; displaced tool-call repair after `trim_messages`; auto-retry on orphan errors in both `invoke_agent()` and `_stream_graph()`; cloud model override support in agent/summarizer |
| **`tasks.py`** | **Major** — `stop_task()`, `get_running_task_thread()`, `stop_event` in `_active_runs`, `TaskStoppedError` handling, `_thread_exists()` guard on thread rename, stopped state (status, naming, notification, skip delivery); `_on_task_fire()` now saves thread meta + model override before launching background run |
| **`app_nicegui.py`** | **Major** — cloud setup wizard, Settings → Cloud tab, chat header model picker, cloud warning banner, sidebar cloud/local icons; task stop buttons (3 locations), `stop_task()` in delete handlers, delayed refresh timer; privacy toggles |
| **`threads.py`** | `model_override` column with auto-migration; `_get_thread_model_override()` / `_set_thread_model_override()` |
| **`api_keys.py`** | OpenAI + OpenRouter key definitions; `cloud_config.json` management (starred models, privacy toggles) |
| **`channels/telegram.py`** | `/model` command (list, set, reset); model override propagation to LangGraph config |
| **`memory_extraction.py`** | FAISS rebuild moved from per-thread `_dedup_and_save()` to single call in `run_extraction()` |
| **`knowledge_graph.py`** | `_upsert_index()` for incremental FAISS updates; additional thread-safety |
| **`vision.py`** | Cloud vision model compatibility |
| **`test_suite.py`** | ~67 new tests across cloud, model switching, task stop, tool-call repair, FAISS optimization |
| **`requirements.txt`** | Added `openai` |
| **`installer/*`** | Version bump to 3.7.0; cloud-aware launcher (skip Ollama warning when cloud default) |
| **`.github/workflows/ci.yml`** | CI updates for cloud test coverage |
| **`.gitignore`** | New ignore patterns |

---

## v3.6.0 — Knowledge Graph, Memory Visualization & Triple Extraction

Thoth now builds a **personal knowledge graph** from your conversations — a connected web of people, places, facts, and their relationships. Memories are no longer isolated records: they are linked entities that the agent can traverse, explore, and reason about. A new interactive **Memory tab** visualizes the graph in real time, and the extraction pipeline now produces structured triples (entity + relation + entity) instead of flat facts.

### 🕸️ Knowledge Graph Engine

New `knowledge_graph.py` — the foundation for all memory storage, replacing the standalone SQLite + FAISS implementation that lived in `memory.py`.

- **Entity-relation model** — every memory is now an entity with a type, subject, description, aliases, tags, and structured properties; entities are connected by typed, directional relations (e.g. `Dad --[father_of]--> User`, `User --[lives_in]--> London`)
- **Triple storage** — SQLite `entities` + `relations` tables with full CRUD; WAL mode for concurrent reads; cascade delete removes orphaned relations when an entity is deleted
- **NetworkX in-memory graph** — a `DiGraph` mirror of the database, rebuilt on startup, used for all traversals and pathfinding; updated atomically on every write
- **FAISS vector index** — unchanged Qwen3-Embedding-0.6B embeddings for semantic similarity; now indexes entity descriptions from the graph layer
- **Alias resolution** — entities can have comma-separated aliases (e.g. "Mom, Mother, Mama"); `find_by_subject()` checks both the `subject` column and the `aliases` column via normalized substring matching, preventing duplicates across names
- **Graph-enhanced recall** — `graph_enhanced_recall(query, top_k, threshold, hops)` first retrieves semantically similar entities via FAISS, then expands N hops in the NetworkX graph to include connected neighbors; the agent sees both the entity and the relationships that connect it
- **Backward-compatible wrapper** — `memory.py` is now a thin delegation layer (~80 lines) that maps legacy column names (`category` to `entity_type`, `content` to `description`) so all existing callers (agent, tools, extraction, UI) work without changes
- **Graph statistics** — `get_graph_stats()` returns entity count, relation count, connected components, and category breakdown for the Settings panel and Memory tab

### 🗺️ Interactive Memory Visualization

A new **Memory tab** on the home screen renders the knowledge graph as an interactive network diagram using vis-network.

- **vis-network integration** — bundled `vis-network.min.js` (9.1.9), served as a static file; renders a force-directed physics simulation in a full-height dark canvas
- **Color-coded entity types** — each category (person, place, fact, preference, event, project) has a distinct color; relation edges show their type as a label
- **Search bar** — live client-side filtering; type a name and the graph highlights matching nodes and fades everything else
- **Entity-type filter buttons** — toggle visibility of entire categories (e.g. show only people and places); buttons are generated dynamically from the data
- **Full map / ego-graph toggle** — switch between the complete graph and a focused 2-hop neighborhood around a selected node
- **Clickable detail card** — clicking a node shows a floating card with the entity's type, description, aliases, tags, source, and a list of all its relationships
- **Fit-to-view button** — resets the camera to fit all visible nodes
- **Live refresh** — graph data is reloaded from the database every time you switch to the Memory tab, so newly extracted entities appear immediately
- **Stats bar** — shows total memories and connections at the top of the panel; expanded stats in Settings show connected components and category breakdown

### 🔗 Memory Tool: Link & Explore

Two new sub-tools on the Memory tool give the agent direct access to the knowledge graph:

- **`link_memories`** — create a typed relationship between any two entities by ID; the agent can say *"Link Mom to Mom's Birthday Party with relation has_event"*; validates both entities exist and returns a confirmation with the relation details
- **`explore_connections`** — traverse the graph outward from an entity; returns all neighbors up to N hops with their relationship types and details; useful for questions like *"Tell me about my family"* or *"What do you know about my work?"*; capped at 3 hops to prevent excessive traversal

### 🧬 Triple-Based Extraction Pipeline

The background extraction pipeline now produces structured triples instead of flat entity records.

- **Entity + Relation extraction** — the LLM prompt now asks for two types of objects: entities (category/subject/content/aliases) and relations (relation_type/source_subject/target_subject/confidence); a worked example in the prompt guides the model
- **"User" entity convention** — the user is always represented by the entity with subject "User"; when the user says *"My name is Alex"*, extraction creates an alias on the User entity rather than a separate "Alex" entity; all user-facing relations use "User" as the source or target
- **Relation type taxonomy** — the prompt includes 30+ suggested relation types across family, social, location, work, preference, and temporal categories, encouraging consistent labeling
- **Two-pass dedup** — Pass 1 saves/updates entities while building a `subject-to-id` map (pre-populated with the User entity), with alias merging; Pass 2 resolves relation subjects to entity IDs and creates relations in the graph
- **Cross-category dedup** — `find_by_subject(None, subject)` searches across all categories, so a "Dad" stored as `person` won't be duplicated when extraction classifies a related fact as `event`
- **Alias-as-list fix** — handles LLMs that return aliases as a JSON array instead of a comma-separated string

### 🔄 Agent Recall Upgrade

Auto-recall now uses the knowledge graph instead of flat semantic search.

- **Graph-enhanced auto-recall** — before every LLM call, the agent retrieves relevant entities via `graph_enhanced_recall()` with 1-hop expansion, so related entities are surfaced alongside direct matches
- **Relation context in recalled memories** — recalled memories now include their graph connections (e.g. "connected via: Dad --> father_of --> User"), giving the agent richer context for answering relational questions
- **System prompt update** — new BUILDING CONNECTIONS and EXPLORING CONNECTIONS sections guide the agent on when to use `link_memories` and `explore_connections`

### 🐛 Bug Fixes

- **Aliases-as-list crash** — fixed `AttributeError` when the extraction LLM returned aliases as a JSON array instead of a comma-separated string
- **Extraction relation resolution** — relations with unresolvable subjects (no matching entity in the DB or current batch) are silently skipped instead of crashing
- **Memory visualization toolbar reliability** — fixed intermittent loss of filter buttons and broken Fit button on the Memory tab; root cause was `ui.add_body_html()` accumulating persistent `<script>` tags on every panel rebuild, causing racing IIFE closures with stale data; replaced with `ui.run_javascript()` (no persistent tags), added teardown that destroys the old vis.Network and cancels stale boot timers, moved vis-network library load to `<head>` (once per page), and made `thothGraphRedraw` perform a full reinit (filter pills + event handlers + network) instead of just re-creating the network
- **Email channel feedback loop** — sent replies weren't marked as read, so the Email channel re-processed its own outbound messages in an infinite loop; fixed by calling `_mark_as_read(service, sent_id)` after both `_send_reply()` and `_send_reply_and_get_id()`
- **macOS MPS/FAISS crash** — `HuggingFaceEmbeddings` defaulted to MPS on Apple Silicon, causing dtype mismatches when FAISS (CPU-only) consumed the tensors; fixed by forcing `model_kwargs={"device": "cpu"}` in `documents.py`
- **FAISS concurrent-access crash** — concurrent calls to `rebuild_index()` and `semantic_search()` could corrupt the in-memory FAISS index; fixed by adding a `threading.Lock()` around all FAISS read/write operations in `knowledge_graph.py`
- **Conversation export 0-byte files on Windows** — thread names containing colons (from timestamps like `02:20 AM`) caused NTFS Alternate Data Streams instead of normal files; exports appeared as 0-byte files with no extension; fixed by sanitizing `\ / : * ? " < > |` from export filenames before writing

### 🚀 Out-of-Box Tool Defaults

Three tools that previously required manual setup are now **enabled by default** on fresh installs, with sensible defaults that work immediately.

- **Filesystem** — enabled by default; workspace auto-defaults to `~/Documents/Thoth` (created on first use); `move_file` added to default operations (protected by interrupt gate — user must approve before execution); `file_delete` still requires opt-in
- **Shell** — enabled by default; already has 3-tier safety (safe commands auto-execute, moderate commands require user approval via interrupt, dangerous commands are blocked outright)
- **Browser** — enabled by default; lazy-launched on first use (no overhead if unused); uses system Chrome/Edge if available, falls back to Playwright's bundled Chromium

### 📬 Telegram Tool & File Pipeline

New **Telegram tool** (`tools/telegram_tool.py`) — the agent can now send messages, photos, and documents to any Telegram chat via the configured bot.

- **3 sub-tools** — `send_telegram_message`, `send_telegram_photo`, `send_telegram_document`; all accept a `chat_id` parameter (defaults to the configured channel)
- **File path resolution** — workspace-relative paths are automatically resolved to absolute paths before sending; works for both Telegram and Gmail attachments
- **Chart PNG export** — `save_to_file` parameter on the Chart tool lets the agent save charts as PNG files (via kaleido) for attaching to messages or emails
- **PDF export** — new `export_to_pdf` operation on the Filesystem tool creates PDF reports from text content (via fpdf2)
- **Gmail attachments** — `send_gmail_message` and `create_gmail_draft` now accept an `attachments` list; files are MIME-encoded and attached via `_build_mime_message()`; missing files are silently skipped with a warning in the message body

### 📨 Channel Resilience & Interrupt Handling

Both the Telegram and Email channels now handle interrupts (destructive action approvals) robustly, with matching logic across both adapters.

- **List-of-dicts interrupt data** — `_format_interrupt()` handles both single interrupt dicts and lists of dicts (produced by multi-step tool chains); extracts the description from each item
- **Interrupt ID propagation** — `_extract_interrupt_ids()` pulls tool-call IDs from interrupt data for correct LangGraph `resume()` targeting; both `_resume_agent_sync()` implementations pass `interrupt_ids` to avoid replaying stale interrupts
- **Corrupt thread recovery** — both channels detect corrupt checkpoints (orphaned tool calls without results) via `_is_corrupt_thread_error()` pattern matching; users receive a friendly message asking them to start a new thread instead of a raw traceback
- **HTML formatting** — Telegram channel formats agent responses as HTML (`parse_mode="HTML"`) with proper escaping for special characters
- **Email sender filter** — the Email channel only processes messages from the authenticated user's own address (`from:{my_email}` in the Gmail query), preventing unauthorized triggering

### 🔒 Task-Scoped Background Permissions

Background tasks now support fine-grained permission controls for operations that would normally require interactive approval.

- **Tiered tool filtering** — background tasks no longer blanket-strip all destructive tools; instead, a tiered system applies:
  - **Always allowed in background**: `workspace_move_file`, `move_calendar_event`, `send_gmail_message` (low-risk or guarded at runtime)
  - **Allowed with runtime guard**: `run_command` (shell) checks against a per-task command prefix allowlist; `send_gmail_message` checks against a per-task recipient allowlist
  - **Always blocked in background**: `workspace_file_delete`, `delete_calendar_event`, `delete_memory`, `tracker_delete`, `task_delete` (irreversible)
- **Per-task allowlists** — two new fields on each task: `allowed_commands` (shell command prefixes) and `allowed_recipients` (email addresses); stored as JSON arrays in `tasks.db`
- **Shell tool runtime guard** — in background mode, commands classified as `needs_approval` are checked against `allowed_commands` (case-insensitive prefix match); blocked patterns (e.g. `rm -rf`) are still rejected before the allowlist check; safe commands (e.g. `dir`, `echo`) always execute
- **Gmail tool runtime guard** — in background mode, all recipients (to/cc/bcc) are validated against `allowed_recipients` (case-insensitive); any disallowed recipient blocks the send
- **UI configuration** — the task editor has a new "🔒 Background permissions (optional)" expandable section with two textareas (one-per-line entry); if the allowlist is blank and the task needs the operation, it fails with a user-friendly error directing the user to configure permissions in the task editor
- **No LLM awareness required** — the agent writes prompts naturally; the permission system operates transparently at the tool execution layer

### 🛡️ Security: ContextVar Background Flag

Fixed a critical security issue where the background-mode flag did not propagate to LangGraph executor threads.

- **Bug**: `threading.local()` was used for `_tlocal.background_workflow`, but LangGraph runs tool functions in separate executor threads where `threading.local()` values are not inherited — so `is_background_workflow()` always returned `False` in tool execution, bypassing background safety gates
- **Fix**: Replaced with `ContextVar` (`_background_workflow_var`), which correctly propagates to child threads via Python's `contextvars` module; updated all 6 references across `agent.py`, `tasks.py`, and `workflows.py`
- **Impact**: Shell tool and Gmail tool background guards now work correctly; `_wrap_with_interrupt_gate()` properly detects background mode in executor threads

### 🧪 Tests

- **676 PASS**, 0 FAIL, 2 WARN (up from 408 in v3.5.0)
- 3 new offline test sections: Knowledge Graph core (section 26, 55 tests), Graph Visualization (section 27, 28 tests — includes 7 visualization reliability regression tests), Triple Extraction (section 28, 18 tests)
- Section 30: File & Messaging Pipeline (30 tests) — Telegram tool, file resolution, chart PNG export, PDF export, Gmail attachments, channel interrupt handling, corrupt thread recovery
- Section 31: Task-scoped background permissions (15 tests) — allowlist columns, ContextVar propagation, shell prefix matching, Gmail recipient checks, UI permission fields
- Section 32: Security audit (12 tests) — ContextVar usage verification, background flag propagation, interactive channel safety, blocked pattern enforcement
- Section 33: Tool default configuration (8 tests) — filesystem/shell/browser enabled by default, default workspace auto-creation, DEFAULT_OPERATIONS validation, interrupt gate coverage
- Section 34: Export filename sanitization (8 tests) — colon replacement, emoji preservation, all illegal-char removal, pathlib suffix correctness, edge cases
- New `integration_tests.py` — 15-section integration test suite (~122 tests) that runs against a live Ollama instance; covers agent routing, memory CRUD, knowledge graph relations, extraction pipeline, task engine, TTS, tool functions, edge cases, extended tool sub-tools (shell classify, filesystem sandbox, chart pipeline, PDF export), channel utilities (Telegram message splitting & HTML formatting), background permissions & ContextVars, bug-fix verifications, and tool default validations; supports `--fast` (skip LLM tests) and `--section N` (run one section)

### 📁 Files Changed

| File | Change |
|------|--------|
| **`knowledge_graph.py`** | **New** — entity-relation graph engine with SQLite + NetworkX + FAISS; `threading.Lock()` around FAISS operations for thread safety |
| **`static/vis-network.min.js`** | **New** — bundled vis-network 9.1.9 for graph visualization |
| **`integration_tests.py`** | **New** — 15-section live integration test suite (~122 tests) |
| **`tools/telegram_tool.py`** | **New** — Telegram messaging tool with 3 sub-tools (send message, photo, document) |
| **`memory.py`** | Refactored from ~530 lines of standalone SQLite+FAISS to ~80-line wrapper delegating to `knowledge_graph.py`; all public signatures unchanged |
| **`agent.py`** | Auto-recall switched to `graph_enhanced_recall()` with 1-hop expansion; tiered background tool filtering with `_ALWAYS_ALLOWED_BG` set; `_background_workflow_var` ContextVar replaces `threading.local()`; interrupt gate reads ContextVar in executor threads |
| **`tools/memory_tool.py`** | 2 new sub-tools: `link_memories` and `explore_connections`; imports `knowledge_graph` |
| **`tools/shell_tool.py`** | Background mode: runtime allowlist check against `_task_allowed_commands_var` for `needs_approval` commands; blocked patterns still enforced first; enabled by default |
| **`tools/gmail_tool.py`** | `send_gmail_message` / `create_gmail_draft`: `attachments` parameter with MIME encoding; background mode: recipient allowlist check against `_task_allowed_recipients_var` |
| **`tools/chart_tool.py`** | `save_to_file` parameter on `_create_chart` for PNG export via kaleido |
| **`tools/filesystem_tool.py`** | New `export_to_pdf` operation (via fpdf2); enabled by default with auto-workspace (`~/Documents/Thoth`); `move_file` added to default operations |
| **`channels/telegram.py`** | List-of-dicts interrupt handling; corrupt thread recovery; HTML formatting; interrupt ID propagation |
| **`channels/email.py`** | List-of-dicts interrupt handling; corrupt thread recovery; interrupt ID propagation; sender-only filter; feedback-loop fix (`_mark_as_read` on sent replies) |
| **`prompts.py`** | System prompt: BUILDING CONNECTIONS + EXPLORING CONNECTIONS sections; BACKGROUND TASK PERMISSIONS note. Extraction prompt: rewritten for triple extraction with User entity convention, relation taxonomy, and worked example |
| **`memory_extraction.py`** | Two-pass pipeline (entities then relations); alias merging; `subject-to-id` map with User pre-population; aliases-as-list fix |
| **`tasks.py`** | `allowed_commands` and `allowed_recipients` columns with DB migration; `run_task_background` sets ContextVars; `_background_workflow_var.set(True)` |
| **`workflows.py`** | `_background_workflow_var.set(True)` (ContextVar migration) |
| **`app_nicegui.py`** | Memory tab with vis-network graph visualization; task editor "🔒 Background permissions" section with allowlist textareas; visualization toolbar reliability fix; export filename sanitization (`_safe_filename`) for Windows NTFS compatibility |
| **`tools/browser_tool.py`** | Enabled by default |
| **`documents.py`** | Forced `model_kwargs={"device": "cpu"}` on `HuggingFaceEmbeddings` to prevent MPS/FAISS crash on Apple Silicon |
| **`requirements.txt`** | Added `networkx`, `fpdf2` |
| **`test_suite.py`** | 8 new sections (26-28, 30-34), ~238 new test assertions |
| **`.gitignore`** | Added `_*.py` and `seed_knowledge_graph.py` |

---

## v3.5.0 — Task Engine, Channel Delivery & Configurable Compression

Complete rewrite of the automation engine — workflows and timers are replaced by a unified **Task Engine** with APScheduler, 7 schedule types, per-task model override, channel delivery (Telegram / Email), persistent run history, a redesigned home screen dashboard, and configurable retrieval compression.

### ⚡ Task Engine (replaces Workflows + Timer)

The old `workflows.py` + `timer_tool.py` are replaced by a single `tasks.py` module backed by APScheduler.

- **7 schedule types** — `daily`, `weekly`, `weekdays`, `weekends`, `interval` (minutes), `cron` (full cron expression), `delay_minutes` (one-shot quick timer with notify-only)
- **SQLite persistence** — `tasks.db` with `tasks` + `task_runs` tables; all schedule formats, delivery config, and model override stored per task
- **Auto-migration** — on first launch, existing `workflows.db` entries are migrated to `tasks.db` automatically; old daily/weekly schedules map to the new types
- **APScheduler integration** — tasks are registered as APScheduler jobs on startup; fire times, pause/resume, and next-run queries come from the scheduler directly
- **Per-task model override** — each task can specify a different LLM; the engine loads the override model, runs the task, then restores the default; retry fallback if the override model fails (HTTP 500)
- **Template variables** — `{{date}}`, `{{day}}`, `{{time}}`, `{{month}}`, `{{year}}` expanded at runtime in prompt steps
- **5 default templates** — Daily Briefing, Research Summary, Email Digest, Weekly Review, and Quick Reminder (new)
- **Run history persistence** — `task_runs` rows survive task deletion (no FK cascade); `get_recent_runs()` uses LEFT JOIN + COALESCE so history displays even after the parent task is removed
- **Status tracking** — each run records `status` (`completed` / `failed` / `completed_delivery_failed`), `status_message`, `task_name`, and `task_icon` columns

### 📋 Task Tool (replaces Timer Tool)

New `tools/task_tool.py` with 5 sub-tools (up from 3 in the old timer):

- `task_create` — create a scheduled task with any of the 7 trigger types
- `task_list` — list all tasks with next fire times
- `task_update` — update task name, prompts, schedule, delivery, or model override
- `task_run_now` — execute a task immediately
- `task_delete` — delete a task (requires user confirmation via interrupt gate)

### 📡 Channel Delivery

Tasks can now deliver their output to a messaging channel after execution.

- **`delivery_channel`** + **`delivery_target`** fields on each task — supports `telegram` (chat ID) and `email` (address + subject)
- **`_validate_delivery()`** — pre-flight check ensures the channel is configured and reachable before the task runs
- **`_deliver_to_channel()`** — sends the task's last LLM response to the configured channel; returns `(status, message)` tuple
- **`completed_delivery_failed`** status — task succeeds but delivery fails (channel error, empty response, etc.)
- **Telegram `send_outbound(chat_id, text)`** — new method on the Telegram channel; captures the bot event loop; RuntimeError guard for missing loop
- **Email `send_outbound(to, subject, body)`** — new method on the Email channel; sends via Gmail OAuth

### 🏠 Dashboard Redesign

- **Tabbed home screen** — two tabs: **⚡ Tasks** (task tiles with edit/run/delete) and **📋 Activity** (monitoring panel)
- **Task Edit dialog** — inline editor for name, icon, prompts, schedule, delivery channel, and model override
- **Activity panel** — 5 sections: Running Now (progress + spinner), Upcoming (next fire times from APScheduler), Recent Runs (last 10 with ✅/❌/⏳ icons), Memory Extraction status, Channel status (🟢/🔴)
- **Settings Workflows tab removed** — 12 → 11 settings tabs; task management moved to the home screen
- **Wider layout** — `max-w-5xl` → `max-w-7xl` for better use of wide screens

### 🔍 Configurable Retrieval Compression

Retrieval-based tools (Documents, Wikipedia, Arxiv, Web Search) now support 3 compression modes, selectable from Settings → Search:

- **Smart** (default) — `EmbeddingsFilter` with cosine similarity threshold 0.5; fast, no extra LLM call; preserves source metadata and citations
- **Deep** — `LLMChainExtractor`; sends each retrieved document through the LLM for precise extraction; slower but highest relevance
- **Off** — no compression; returns raw retrieved chunks as-is

Global config stored in `tools_config.json` under the `"global"` key via `registry.get_global_config()` / `set_global_config()`.

### 🐛 Bug Fixes

- **Model override 500 errors** — retry fallback when per-task model fails to load
- **Context size cap** — `get_llm_for()` uses `min(model_max, user_setting)` to prevent context overflows
- **Model swap during override tasks** — `_model_override_var` ContextVar propagates override model name to `_get_compressor()` and `_do_summarize()`, preventing GPU model eviction
- **Delivery content bug** — `invoke_agent()` returns `str`, not `dict`; fixed `isinstance(result, dict)` check that was always False
- **Empty delivery** — tasks now deliver even when `last_response` is empty (falls back to status message)
- **Telegram error propagation** — `send_outbound` now properly raises on failure instead of silently swallowing errors
- **Email error propagation** — same fix for the Email channel

### 🧪 Tests

- **408 PASS**, 0 FAIL, 2 WARN (up from 322)
- 4 new test sections: Task Tool (§21, 11 tests), Activity Tab (§22, 10 tests), Channel Delivery (§23, 20 tests), Task Engine + Compression (§24–25, 45 tests)

### 📁 Files Changed

| File | Change |
|------|--------|
| **`tasks.py`** | **New** — unified task engine replacing `workflows.py` + `timer_tool.py` |
| **`tools/task_tool.py`** | **New** — 5 sub-tools for task CRUD + execute |
| **`tools/timer_tool.py`** | **Deleted** — subsumed by `task_tool.py` |
| **`agent.py`** | `_model_override_var` ContextVar; `_get_compressor()` rewritten with 3 modes (Smart/Deep/Off); `EmbeddingsFilter` import; multi-interrupt support |
| **`app_nicegui.py`** | Tabbed home screen (Tasks + Activity); Task Edit dialog; Settings tabs 12→11; Retrieval Compression selector; wider layout |
| **`channels/telegram.py`** | New `send_outbound()` with RuntimeError guard |
| **`channels/email.py`** | New `send_outbound()` via Gmail OAuth |
| **`models.py`** | `get_llm_for()` context cap with `min(model_max, user_setting)` |
| **`prompts.py`** | Removed timer instructions; added TASKS & REMINDERS section (~45 lines) |
| **`tools/registry.py`** | Global config: `get_global_config()` / `set_global_config()` |
| **`tools/__init__.py`** | `timer_tool` → `task_tool` import swap |
| **`memory_extraction.py`** | New `get_extraction_status()` |
| **`installer/thoth_setup.iss`** | `workflows.py` → `tasks.py`, `timer_tool.py` → `task_tool.py` |
| **`test_suite.py`** | 4 new sections (§21–25), 86 new tests |

---

## v3.4.0 — Browser Automation

Full browser automation via Playwright, giving the agent the ability to navigate websites, click elements, fill forms, and manage tabs in a visible Chromium window — plus browser snapshot compression for long browsing sessions and a fix for the gold color regression.

### 🌐 Browser Tool

A new `browser_tool.py` module gives the agent 7 browser sub-tools for autonomous web browsing in a real, visible browser window.

- **Shared visible browser** — runs with `headless=False` so the user can see what the agent is doing and intervene (e.g. type passwords, solve CAPTCHAs)
- **Persistent profile** — `launch_persistent_context()` stores cookies, logins, and localStorage in `~/.thoth/browser_profile/` so sites stay logged-in across restarts
- **Accessibility-tree snapshots** — after every action the tool captures the page's accessibility tree, assigning numbered references (`[1]`, `[2]`, …) to interactive elements so the model can click/type by number
- **Smart snapshot filtering** — deduplicates links, drops hidden elements, soft-caps at 100 interactive elements, and truncates at 25K chars to stay within context limits
- **7 sub-tools**:
  - `browser_navigate` — go to a URL
  - `browser_click` — click an interactive element by its reference number
  - `browser_type` — type text into an input element by reference number
  - `browser_scroll` — scroll the page up or down
  - `browser_snapshot` — take a fresh accessibility snapshot of the current page
  - `browser_back` — go back one page in browser history
  - `browser_tab` — manage tabs (list, switch, new, close)
- **Browser channel detection** — automatically detects installed Chrome, then Edge (Windows), then falls back to Playwright's bundled Chromium
- **PID-scoped crash recovery** — detects stale browser processes from previous crashes and cleans up the profile lock before relaunching
- **Background workflow blocking** — browser actions are blocked when running inside a background workflow

### 🧠 Browser Snapshot Compression

Long browsing sessions (6–10+ actions) can produce 150K+ characters of accessibility snapshots, easily overflowing the context window. A new pre-model trimming pass compresses older browser results.

- **Keep last 2 snapshots in full** — the two most recent browser tool results are sent to the LLM unmodified
- **Compact stubs for older results** — older snapshots are replaced with a one-line stub containing the URL, page title, and action name (`[Prior browser navigate — URL: …, Title: …. Full snapshot omitted to save context.]`)
- **Checkpoint preservation** — only the LLM-visible copy is trimmed; full snapshots remain in the conversation checkpoint for the UI

### 🎨 Gold Color Fix

- **Root cause** — NiceGUI 3.8.0's `ui.html()` defaults to `sanitize=True`, which uses the browser's `setHTML()` Sanitizer API; a WebView2 auto-update between March 12–18 enabled the Sanitizer, which strips inline `style` attributes — breaking all gold-colored text
- **Fix** — added `sanitize=False` to all 18 `ui.html()` calls in `app_nicegui.py` to bypass the Sanitizer API

### 🛠️ Other Improvements

- **Sidebar tagline** — changed from *"Your Knowledgeable Personal Agent"* to *"Personal AI Sovereignty"*
- **System prompt updates** — `prompts.py` updated with BROWSER AUTOMATION routing rules, guiding the agent to use `browser_*` tools when the user mentions browsing and `read_url` only for raw text extraction
- **Test suite** — 293 → 322 tests (added browser tool registration, sub-tool count, snapshot filtering, crash recovery, tab management, and channel detection tests)

### Files Changed

| File | Change |
|------|--------|
| **`tools/browser_tool.py`** | **New** — browser automation tool with `BrowserSession`, `_detect_channel()`, 7 sub-tools, accessibility snapshot with smart filtering, PID-scoped crash recovery, persistent profile |
| **`agent.py`** | Browser snapshot compression in `_pre_model_trim()` — keeps last 2 full, stubs older snapshots |
| **`app_nicegui.py`** | `sanitize=False` on all 18 `ui.html()` calls (gold fix); sidebar tagline changed to *"Personal AI Sovereignty"* |
| **`tools/__init__.py`** | Added `browser_tool` import |
| **`prompts.py`** | BROWSER AUTOMATION routing rules in system prompt |
| **`requirements.txt`** | Added `playwright~=1.58` |
| **`test_suite.py`** | Browser tool tests (293 → 322) |

---

## v3.3.0 — Shell Access & Stop Button

Full shell access with safety classification, a reliable stop button with clean generation cancellation, and filesystem sandboxing improvements.

### 🖥️ Shell Tool

A new `shell_tool.py` module gives the agent the ability to run shell commands on the user's machine — making Thoth a true system assistant.

- **Persistent sessions** — each conversation thread gets its own shell session; `cd`, environment variables, and other state persists across commands
- **3-tier safety classification** — every command is classified before execution:
  - **Safe** (auto-executes) — read-only commands like `ls`, `pwd`, `cat`, `git status`, `pip list`, `echo`, `df`
  - **Moderate** (user approval required) — system-modifying commands like `pip install`, `apt`, `brew`, `kill`, `chmod`, `rm`
  - **Blocked** (rejected outright) — dangerous commands like `shutdown`, `reboot`, `mkfs`, `:(){ :|:& };:`
- **Background workflow blocking** — shell commands are automatically blocked when running inside a background workflow to prevent unattended destructive actions
- **Inline terminal panel** — command output appears in a collapsible terminal panel in the chat UI with clear and history controls
- **History persistence** — command history is saved per-thread in `~/.thoth/shell_history.json` and reloaded when you revisit a conversation
- **Session cleanup** — shell sessions and history entries are cleaned up when threads are deleted

### ⏹️ Stop Button Overhaul

The stop button has been rebuilt from scratch for reliable generation cancellation.

- **`threading.Event` cancellation** — replaces the old boolean flag with a proper `threading.Event` for race-free stop signalling
- **Drain mechanism** — after stop is signalled, the consumer drains the streaming queue until the producer's sentinel `None` arrives or a 30-second timeout expires, preventing stale tokens from leaking into the next generation
- **Checkpoint marker** — a `⏹️ *[Stopped]*` marker is appended to the conversation checkpoint so thread reloads show that a generation was interrupted (works for both mid-thinking and mid-tool-call stops)
- **Orphaned tool call repair** — `repair_orphaned_tool_calls()` now unconditionally appends the stop marker, fixing mid-tool-call stops where no orphans exist but the generation was still interrupted
- **UI feedback** — stop button shows an hourglass icon during the drain phase

### 📁 Filesystem Sandboxing

- **`workspace_*` tool renaming** — all filesystem tools are now prefixed with `workspace_` (e.g. `workspace_read_file`, `workspace_list_directory`) so the LLM understands their scope is limited to the configured workspace folder
- **Out-of-workspace rejection** — file operations targeting paths outside the workspace are rejected with a clear error message directing the agent to use `run_command` instead
- **Filesystem vs Shell routing rules** — the system prompt now includes explicit routing guidelines: `workspace_*` tools for files inside the workspace, `run_command` for anything outside

### 🛠️ Other Improvements

- **Settings tab reorder** — the 12 Settings tabs have been reordered for better workflow (Models first, then Memory, Voice, Workflows, System, Tracker, etc.)
- **System tab** — the old "Filesystem" settings tab has been renamed to "System" with a terminal icon, now containing both filesystem workspace configuration and shell settings
- **Terminal panel UI** — inline terminal panel in chat with toggle bar, auto-show on shell output, clear button, and history reload on thread switch
- **Agent prompt updates** — `prompts.py` updated with FILESYSTEM vs SHELL ROUTING rules, destructive tool name updates, and shell usage guidance
- **Test suite** — 270 → 293 tests (added shell tool tests, stop button tests, filesystem sandboxing tests)

### Files Changed

| File | Change |
|------|--------|
| **`tools/shell_tool.py`** | **New** — shell tool with `ShellSession`, `ShellSessionManager`, `classify_command()`, 3-tier safety, persistent sessions, history |
| **`agent.py`** | `threading.Event` stop mechanism, `repair_orphaned_tool_calls()` with unconditional stop marker, `AIMessage` import, `raw_name` in tool_done payload |
| **`app_nicegui.py`** | Stop button drain mechanism, inline terminal panel, System tab rename, settings tab reorder, shell cleanup on thread delete, `code-friendly` markdown extra |
| **`tools/filesystem_tool.py`** | `_is_outside_workspace()` guard, `workspace_*` renaming, out-of-workspace rejection |
| **`tools/__init__.py`** | Added `shell_tool` import |
| **`prompts.py`** | FILESYSTEM vs SHELL ROUTING rules, destructive tool name updates |
| **`test_suite.py`** | Shell tool tests, stop button tests, filesystem sandboxing tests (270 → 293) |

---

## v3.2.0 — Smart Context & Memory Overhaul

Automatic conversation summarization for unlimited conversation length, a complete rewrite of the memory deduplication system, and centralized prompt management.

### 🧠 Memory System Overhaul

The memory deduplication pipeline has been completely rewritten to fix a critical bug where background extraction could create duplicates or update the wrong memory.

#### Deterministic Dedup (replaces semantic dedup)
- **`find_by_subject()` for live saves** — when the agent saves a memory, an exact normalised-subject lookup (SQL) checks if one already exists in the same category; if it does, the richer content is kept silently — no duplicates created
- **Cross-category dedup for extraction** — background extraction now passes `category=None` to `find_by_subject()`, matching against all categories. This prevents fragmentation when the extraction LLM classifies a fact differently than the live tool (e.g. a birthday saved as `person/Dad` won't be re-created as `event/Dad`)
- **Why not semantic?** — semantic similarity (cosine) proved unreliable for dedup: short extracted content ("Priya") vs rich live content ("User's sister is named Priya and she lives in Manchester") scored only 0.78 — well below any safe threshold. Semantic search remains the right tool for *recall*; deterministic SQL is the right tool for *dedup*

#### Source Tracking
- **`source` column** — every memory is tagged `live` (agent during chat) or `extraction` (background scanner) for diagnostics
- **Migration** — existing databases are automatically migrated via `ALTER TABLE`

#### Active Thread Exclusion
- **`set_active_thread()` API** — the UI layer tells the extractor which thread is currently active; background extraction skips it to avoid race conditions with the live agent

#### Extended Update
- **`update_memory()`** — now accepts optional `subject`, `tags`, `category`, and `source` keyword arguments, not just content

#### Consolidation
- **`consolidate_duplicates(threshold)`** — utility to scan and merge near-duplicate memories that may have accumulated over time

#### Auto-Recall with IDs
- **Memory IDs in context** — auto-recalled memories now include their IDs (`[id=abc123]`) so the agent can use `update_memory` or `delete_memory` with the exact ID when the user corrects or retracts previously saved information

#### Prompt Guidance
- **DEDUPLICATION section** — system prompt tells the agent that `save_memory` handles dedup automatically
- **UPDATING MEMORIES section** — system prompt instructs the agent to use `update_memory` with the recalled ID for corrections, not create a new memory

### 📝 Context Summarization

A new automatic summarization system that compresses older conversation turns, enabling effectively unlimited conversation length within any context window.

- **Automatic trigger** — when token usage exceeds 80% of the context window, a background summarization compresses older conversation turns into a running summary
- **Protected turns** — the 5 most recent turns are never summarized, preserving immediate conversational context
- **Hard trim safety net** — a secondary 85% budget drops the oldest non-protected messages if summarization alone isn't enough
- **Transparent** — the summary is injected as a system message; the user experience is seamless

### 📄 Centralized Prompts

- **New `prompts.py` module** — all LLM prompts extracted from inline strings into a single file: `AGENT_SYSTEM_PROMPT`, `EXTRACTION_PROMPT`, `SUMMARIZATION_PROMPT`
- **Easier tuning** — modify agent behavior, extraction rules, or summarization instructions in one place

### 🛠️ Other Improvements

- **URL Reader** — `MAX_CHARS` increased from 12,000 → 30,000 for more complete page reads
- **System prompt polish** — improved URL reader guidance, documents tool instructions, YouTube transcript handling, consolidated honesty directives
- **Test suite** — 233 → 270 tests (added context summarization tests + 40 memory system integrity tests)

### Files Changed

| File | Change |
|------|--------|
| **`prompts.py`** | **New** — centralized LLM prompts |
| **`memory.py`** | `source` column, `find_by_subject()`, `find_duplicate()`, `consolidate_duplicates()`, `_normalize_subject()`, extended `update_memory()` and `save_memory()` |
| **`memory_extraction.py`** | `_dedup_and_save()` rewritten (deterministic dedup), `set_active_thread()` API, active thread exclusion |
| **`tools/memory_tool.py`** | `_save_memory()` rewritten with deterministic dedup via `find_by_subject()` |
| **`agent.py`** | Context summarization (`_maybe_summarize()`, `_pre_model_trim()`), auto-recall with memory IDs, prompts extracted to `prompts.py` |
| **`app_nicegui.py`** | `set_active_thread()` wired into thread management |
| **`tools/url_reader_tool.py`** | `MAX_CHARS` 12K → 30K |
| **`test_suite.py`** | Sections 16 (context summarization) and 17 (memory integrity) added |

---

## v3.1.0 — macOS Support & Kokoro TTS

Cross-platform macOS support and a complete TTS engine migration from Piper to Kokoro.

### 🍎 macOS Support

- **Native macOS installer** — `Start Thoth.command` — double-click in Finder to install and launch; auto-installs Homebrew, Python 3.12, and Ollama if not present
- **Apple Silicon & Intel** — works on M1/M2/M3/M4 and Intel Macs (macOS 12+)
- **Thoth.app bundle** — auto-generated `.app` with option to copy to /Applications for Dock/Launchpad access
- **CI-built macOS zip** — GitHub Actions builds the macOS release on a real macOS runner with correct Unix permissions
- **Cross-platform codebase** — all Python modules updated to work on both Windows and macOS (platform-specific imports, path handling, sound playback)

### 🔊 Kokoro TTS (replaces Piper)

- **New TTS engine** — Kokoro TTS via ONNX Runtime replaces Piper TTS on all platforms
- **Cross-platform** — Kokoro runs natively on Windows, macOS (Apple Silicon & Intel), and Linux — Piper only worked on Windows/Linux
- **10 built-in voices** — 5 American (4 female, 1 male), 3 American male, 1 British female, 1 British male (up from 8 Piper voices)
- **Auto-download** — model files (~169 MB) are downloaded automatically on first TTS use; no bundling required in the installer
- **Same streaming UX** — sentence-by-sentence playback, mic gating, code block skipping — all preserved
- **Smaller installer** — Windows installer reduced from ~90 MB to ~30 MB (Piper engine + voice no longer bundled)

### 🛠️ Infrastructure

- **CI updated** — GitHub Actions `ci.yml` now includes a `build-mac-release` job that builds the macOS zip on `macos-latest` and uploads as an artifact
- **Test suite** — 205 tests passing (added Kokoro TTS tests, all platforms)
- **Windows installer** — Piper download steps removed from `build_installer.ps1` and `thoth_setup.iss`

---

## v3.0.0 — NiceGUI, Messaging Channels & Habit Tracker

Complete frontend rewrite from Streamlit to NiceGUI, new messaging channel adapters for Telegram and Email, and a conversational habit/health tracking system.

### 📋 Habit & Health Tracker

A new conversational tracker for logging and analysing recurring activities — medications, symptoms, exercise, periods, mood, sleep, or anything you want to track over time.

#### Tracking
- **Natural-language logging** — tell the agent *"I took my Lexapro"* or *"Headache level 6"* and it offers to log the entry; no forms or dashboards needed
- **Auto-create trackers** — trackers are created on first mention; supports boolean, numeric, duration, and categorical types
- **Backfill** — log entries with a past timestamp: *"I took my meds at 8am"*
- **3 sub-tools** — `tracker_log` (structured input), `tracker_query` (free-text read-only), `tracker_delete` (destructive, requires confirmation via interrupt)

#### Analysis
- **7 built-in analyses** — adherence rate, current/longest streaks, numeric stats (mean/min/max/σ), frequency (per week/month), day-of-week distribution, cycle estimation (period tracking), co-occurrence between any two trackers
- **Trend queries** — *"Show my headache trends this month"* returns stats + exports CSV for charting
- **Chart chaining** — CSV exports are passed to the existing Chart tool for interactive Plotly visualisations (bar, line, scatter, etc.)
- **Co-occurrence** — *"Do headaches correlate with my period?"* compares two trackers within a configurable time window

#### Privacy & Integration
- **Fully local** — SQLite database at `~/.thoth/tracker/tracker.db`; CSV exports in `~/.thoth/tracker/exports/`
- **Memory separation** — tracker data is excluded from the memory extraction system; logging meds won't pollute your personal knowledge base
- **Agent prompt integration** — system prompt instructs the agent to confirm before logging and to chain to `create_chart` for visual outputs

### 🎯 Context-Size Capping

- **Automatic model-max enforcement** — if you select a context window larger than the model's native maximum (e.g. 64K on a 40K-max model), trimming and the token counter automatically use the model's actual limit instead of the user-selected value
- **Model metadata query** — `get_model_max_context()` queries Ollama's `show()` API for the model's `context_length` and caches the result per model
- **Toast notifications** — a warning toast appears when changing models or context size if the selection exceeds the model's native max, explaining which value will actually be used
- **Settings info label** — the Models tab shows an inline note below the context selector when capping is active

---

### 🖥️ NiceGUI Frontend

The entire UI has been rewritten using [NiceGUI](https://nicegui.io/), replacing Streamlit. The new frontend runs on port **8080** and offers a faster, more responsive experience with true real-time streaming.

- **Full feature parity** — all existing functionality ported: chat interface, sidebar thread manager, settings dialog (now 11 tabs), file attachments, streaming, voice bar, export, workflows
- **Real-time updates** — no more page reloads; token streaming, tool status, and toast notifications update instantly via websocket
- **System tray launcher** — `launcher.py` updated to manage the NiceGUI process
- **Native desktop window** — runs in a native OS window via pywebview instead of a browser tab; `--native` flag passed by default from the launcher
- **Two-tier splash screen** — branded splash (dark background, gold Thoth logo, animated loading indicator) displays while the server starts; tries tkinter GUI first, falls back to a console-based splash if tkinter is unavailable; runs as an isolated subprocess to avoid Tcl/threading conflicts with pystray; self-closes when port 8080 responds
- **First-launch setup wizard** — on first run, a guided dialog lets the user pick a brain model and vision model and download them before the main UI loads
- **Explicit download buttons** — model downloads in Settings are triggered by dedicated Download buttons instead of auto-downloading on selection

### 📬 Messaging Channels

New `channels/` package with two messaging channel adapters:

#### Telegram Bot
- **Long-polling adapter** — connect a Telegram bot via Bot API token
- **Full agent access** — messages are processed by the same ReAct agent with all tools available
- **Thread per chat** — each Telegram chat gets its own conversation thread with a 📱 icon
- **Settings UI** — configure bot token, start/stop, and auto-start on launch from Settings → Channels tab

#### Email Channel
- **Gmail polling** — polls inbox at configurable intervals for new messages
- **OAuth 2.0 authentication** — uses existing Gmail OAuth credentials with re-authenticate button
- **Smart filtering** — responds only to emails from approved senders list
- **Thread per sender** — each email sender gets a dedicated thread with a 📧 icon
- **Auto-start** — channels can be set to auto-start when Thoth launches

### 🔧 Infrastructure

- **Version bump** — v2.2.0 → v3.0.0
- **Installer updated** — Inno Setup script updated for NiceGUI, channels package included; `._pth` patched at install time to add the app directory for channels import; tkinter bundled from system Python for embedded environment
- **Dependencies** — `streamlit` replaced by `nicegui`; `pywebview` added for native window; `pythonnet` added for Python 3.14 compatibility; added missing packages (`apscheduler`, `plyer`, `youtube-search`, `numpy`, `requests`, `pydantic`) to `requirements.txt`
- **Structured logging** — comprehensive `logging` added across 14 modules (`models`, `tts`, `threads`, `api_keys`, `documents`, `agent`, `app_nicegui`, `tools/registry`, `tools/base`, `tools/gmail_tool`, `tools/calendar_tool`, `tools/weather_tool`, `tools/conversation_search_tool`, `tools/system_info_tool`); all output written to `~/.thoth/thoth_app.log` via stderr capture
- **Log noise suppression** — noisy third-party loggers (`httpx`, `httpcore`, `urllib3`, `sentence_transformers`, `transformers`, `huggingface_hub`, `googleapiclient`, `primp`, `ddgs`, `nicegui`, `uvicorn`, etc.) silenced to WARNING+; tqdm/safetensors weight-loading spam suppressed by redirecting stderr during embedding model init; `OPENCV_LOG_LEVEL=ERROR` set at startup
- **Ollama launch fix** — launcher starts `ollama app.exe` (tray icon) instead of bare `ollama serve` for proper Windows integration
- **Unicode fix** — `PYTHONIOENCODING=utf-8` set at startup to prevent cp1252 crashes on non-ASCII model output
- **Lazy FAISS initialization** — embedding model and vector store are now lazy-loaded via getter functions to avoid double-initialization caused by NiceGUI's `multiprocessing.Process` (Windows spawn) re-importing the module
- **Old Streamlit app** — `app.py` kept in repo but git-ignored; not deleted

---

## v2.2.0 — Workflows

A new workflow engine for reusable, multi-step prompt sequences with scheduling support.

---

### ⚡ Workflow Engine

Create named workflows — ordered sequences of prompts that run in a fresh conversation thread. Each step sees the output of the previous one, enabling chained research → summarisation → action pipelines.

#### Core Features
- **Multi-step prompt sequences** — define 1+ prompts that execute sequentially in a single thread
- **Template variables** — `{{date}}`, `{{day}}`, `{{time}}`, `{{month}}`, `{{year}}` are replaced at runtime
- **Live streaming** — workflows stream in real-time with a step progress indicator in the chat header
- **Background completion** — navigate away mid-workflow and it continues silently; the sidebar shows a running indicator
- **Desktop notifications** — scheduled and background runs trigger a Windows notification on completion

#### Scheduling
- **Daily schedule** — run a workflow automatically at a specific time every day
- **Weekly schedule** — run on a specific day and time each week
- **Scheduler engine** — background thread checks for due workflows every 60 seconds
- **Enable/disable** — toggle scheduled workflows on or off without deleting the schedule

#### UI
- **Home screen tiles** — workflows appear as clickable cards on the home screen (no thread selected) with Run buttons
- **Inline quick-create** — create new workflows directly from the home screen
- **Settings → Workflows tab** — full management view with name, icon, description, prompt editor (add/remove/reorder steps), schedule config, run history
- **Duplicate & Delete** — one-click workflow cloning and deletion
- **Run history** — past executions shown per workflow with timestamps, step counts, and status

#### Pre-built Templates
Ships with 4 starter workflows that can be customised or deleted:
- **📰 Daily Briefing** — top news + weather + today's calendar (3 steps)
- **🔬 Research Summary** — search latest AI developments + summarise with citations (2 steps)
- **📧 Email Digest** — check Gmail inbox + summarise by priority (2 steps)
- **📋 Weekly Review** — past week's calendar events + review and recommendations (2 steps)

#### Safety
- **Destructive tool exclusion** — background workflow runs automatically exclude destructive tools (send email, delete files, etc.) so they can never execute unattended; the LLM adapts by using safe alternatives (e.g. creating a draft instead of sending)
- **Scheduler double-fire prevention** — `last_run` is set immediately when a scheduled workflow triggers, before execution begins, preventing duplicate runs within the cooldown window

### 🔔 Unified Notification System

A new `notifications.py` module replaces scattered notification calls with a single `notify()` function that fires across three channels simultaneously:

- **Desktop notifications** — via plyer, with timestamped messages showing when the task actually completed
- **Sound effects** — via winsound (lazy-imported for cross-platform safety), played asynchronously in a background thread
- **In-app toasts** — queued for the next Streamlit rerun via `drain_toasts()`, with emoji icons

#### Sound Files
- `sounds/workflow.wav` — two-tone chime (C5→E5) on workflow completion
- `sounds/timer.wav` — 5-beep alert (A5) for timer expiration

Both generated as clean sine-wave tones via Python's `wave` module.

### 🎨 UI Polish

- **Sidebar running indicator** — simplified from step count (`⏳ 2/4`) to just `⏳` since the sidebar doesn't auto-refresh
- **Settings tab renamed** — "🎛️ Preferences" → "🎤 Voice" to better describe the tab's contents
- **Workflow emoji picker** — replaced free-text icon input with a selectbox of 20 curated emojis
- **Streamlit sidebar toggle** — added `.streamlit/config.toml` with `toolbarMode = "minimal"` and `hideTopBar = true`

### 📦 Dependency & Compatibility

- **`streamlit>=1.45`** pinned in `requirements.txt` for `st.tabs` stability
- **`winsound` lazy import** — non-Windows platforms gracefully skip sound playback instead of crashing

#### Technical Details
- **New modules** — `workflows.py` (workflow engine + scheduler), `notifications.py` (unified notify + toast queue)
- **New assets** — `sounds/workflow.wav`, `sounds/timer.wav`
- **New config** — `.streamlit/config.toml` (sidebar/toolbar settings)
- **Prompt chaining** — first step streams live, subsequent steps continue via `stream_agent` or fall back to `invoke_agent` in background
- **Thread naming** — workflow threads are prefixed with ⚡ and include the workflow name and timestamp
- **Settings tab count** — Settings dialog now has 10 tabs (added Workflows, renamed Preferences → Voice)
- **Background flag** — `threading.local()` (`_tlocal`) flags background workflows; agent graph cache key includes `bg:{True/False}` for separate tool sets
- **Timer tool updated** — replaced inline `_notify()` with `notifications.notify()` for consistent sound + desktop + toast

---

## v2.1.0 — Semantic Memory & Voice Simplification

A major upgrade to the memory system and a complete simplification of the voice pipeline.

---

### 🧠 Semantic Memory System

The memory system has been upgraded from keyword-based search to full **FAISS semantic vector search** with automatic recall and background extraction.

#### Semantic Search
- **FAISS vector index** — memories are now embedded with `Qwen3-Embedding-0.6B` and stored in a FAISS index at `~/.thoth/memory_vectors/`
- **Cosine similarity search** — `semantic_search()` replaces the old keyword `LIKE` queries for much better recall on indirect/paraphrased queries
- **Auto-rebuild** — the FAISS index automatically rebuilds on any memory mutation (save, update, delete)

#### Auto-Recall
- **Automatic memory injection** — before every LLM call, the current user message is embedded and the top-5 most relevant memories (threshold ≥ 0.35) are injected as a system message
- **Assertive phrasing** — recalled memories are presented as "You KNOW the following facts about this user" so the model treats them as ground truth
- **System prompt reinforcement** — the agent is explicitly instructed to save buried personal info alongside other requests

#### Background Memory Extraction
- **LLM-powered extraction** — on startup and every 6 hours, past conversations are scanned by the LLM to extract personal facts (names, preferences, projects, etc.)
- **Semantic deduplication** — extracted facts are compared against existing memories using cosine similarity; duplicates (> 0.85) update existing entries, novel facts create new ones
- **Incremental scanning** — only conversations updated since the last extraction run are processed
- **State persistence** — extraction timestamps tracked in `~/.thoth/memory_extraction_state.json`
- **New module** — `memory_extraction.py` added to the codebase

### 🎤 Voice Pipeline Simplification

The voice pipeline has been completely rewritten for reliability and simplicity.

#### What Changed
- **Removed wake word detection** — no more OpenWakeWord, ONNX models, or "Hey Jarvis"/"Hey Mycroft" activation
- **Removed `wake_models/` directory** — deleted all bundled ONNX wake word model files
- **Removed auto-timeout and heartbeat** — no more inactivity timer or browser heartbeat polling
- **Removed follow-up mode** — no more timed mic re-open window after TTS playback
- **Removed tool call announcements** — TTS no longer speaks tool names aloud during execution

#### New Design
- **Toggle-based activation** — simple manual toggle to start/stop listening
- **4-state machine** — clean state transitions: `stopped` → `listening` → `transcribing` → `muted`
- **CPU-only Whisper** — faster-whisper runs exclusively on CPU with int8 quantization for consistent performance
- **Medium model support** — added `medium` to the Whisper model size options (tiny/base/small/medium)
- **Voice-aware responses** — voice input is tagged with a system hint so the agent responds conversationally
- **Status safety net** — auto-unmutes when TTS finishes but pipeline state is stuck on "muted"

### 🔊 TTS Markdown-to-Speech Improvements

The `_MD_STRIP` regex pipeline in `tts.py` has been overhauled for cleaner speech output:
- Fixed bold/italic/strikethrough pattern ordering (triple before double before single)
- Added black circle, middle dot, and additional bullet character stripping
- Added numbered list prefix stripping (both `1.` and `1)` styles)
- Moved bullet stripping before emphasis patterns to prevent partial matches
- Removed broken `_italic_` pattern

### 🚀 Startup UX Revamp

- **Live progress steps** — replaced generic "Loading models…" spinner with `st.status` widget showing each initialization step (core modules, documents, models, API keys, voice/TTS, vision, memory extraction)
- **No flicker on reruns** — startup UI only shows on first run; thread switches and page reruns skip it entirely via session state gate
- **Clean banner removal** — startup status wrapped in `st.empty()` placeholder for clean removal after load

### 🧹 Cleanup

- **Deleted `wake_models/` directory** — removed all bundled ONNX wake word model files (alexa, hey_jarvis, hey_mycroft, hey_thought)
- **Cleaned installer references** — removed wake_models from `installer/thoth_setup.iss` and `installer/README.md`
- **Removed OpenWakeWord dependency** — no longer referenced in codebase or acknowledgements

### 📦 Data Storage Updates

Two new entries in `~/.thoth/`:
- `memory_vectors/` — FAISS index (`index.faiss`) and ID mapping (`id_map.json`) for semantic memory search
- `memory_extraction_state.json` — tracks last extraction run timestamp per thread

### 🧹 Codebase Changes

- **Added**: `memory_extraction.py` (background extraction + dedup + periodic timer)
- **Updated**: `memory.py` (FAISS vector index, `semantic_search()`, `_rebuild_memory_index()`, shared embedding model)
- **Updated**: `agent.py` (auto-recall injection in `_pre_model_trim`, updated system prompt for memory awareness)
- **Updated**: `voice.py` (complete rewrite — 4-state toggle machine, CPU-only int8 Whisper, no wake word)
- **Updated**: `tts.py` (overhauled `_MD_STRIP` patterns, removed tool call announcements)
- **Updated**: `app.py` (startup UX revamp, memory extraction integration, voice simplification)
- **Updated**: `tools/memory_tool.py` (`search_memory` now uses `semantic_search()`)
- **Updated**: `installer/thoth_setup.iss` (removed wake_models references)
- **Updated**: `installer/README.md` (removed wake_models from bundled files)
- **Deleted**: `wake_models/` directory (4 ONNX files)

---

## v2.0.0 — ReAct Agent Rewrite

**A complete architectural overhaul.** Thoth v2 replaces the original RAG pipeline with a fully autonomous ReAct agent that can reason, use tools, and carry persistent memory across conversations.

---

### 🏗️ Architecture: RAG Pipeline → ReAct Agent

The original Thoth (v1.x) used a custom LangGraph `StateGraph` with three nodes (`needs_context` → `get_context` → `generate_answer`) to decide whether retrieval was needed, fetch context, and generate cited answers. This worked well for Q&A but couldn't take actions, compose emails, manage files, or remember things.

**Thoth v2** replaces this with a LangGraph `create_react_agent()` — a reasoning loop where the LLM autonomously decides which tools to call, interprets results, and continues until it has a complete answer. The agent can chain multiple tools, retry with different queries, and combine information from several sources in a single turn.

Key changes:
- **`rag.py` removed** — the custom RAG state machine is gone
- **`agent.py` added** — new ReAct agent with system prompt, pre-model message trimming, streaming event generator, and interrupt mechanism
- **Smart context management** — pre-model hook trims history to 80% of context window; oversized tool outputs (e.g. multiple PDFs) are proportionally shrunk so multi-file workflows fit; file reads capped at 80K characters
- **Tool system** — new `tools/` package with `BaseTool` ABC, auto-registration registry, and 19 self-registering tool modules
- **42 sub-tools** exposed to the model (up from 4 retrieval sources)

### 🔧 17 Integrated Tools

Every tool is a self-registering module in `tools/` with configurable enable/disable, API key management, and optional sub-tool selection.

#### Search & Knowledge (7 tools)
- **🔍 Web Search** — Tavily-powered live web search with contextual compression
- **🦆 DuckDuckGo** — free web search fallback, no API key required
- **🌐 Wikipedia** — encyclopedic knowledge retrieval with compression
- **📚 Arxiv** — academic paper search with source URL rewriting
- **▶️ YouTube** — video search + full transcript/caption fetching
- **🔗 URL Reader** — fetch and extract clean text from any web page
- **📄 Documents** — semantic search over user-uploaded files via FAISS vector store

#### Productivity (4 tools)
- **📧 Gmail** — search, read, draft, and send emails via Google OAuth; operations tiered into read/compose/send with individual toggles
- **📅 Google Calendar** — view, search, create, update, move, and delete events via Google OAuth; shares credentials with Gmail
- **📁 Filesystem** — sandboxed file operations (read, write, copy, move, delete) within a user-configured workspace folder; reads PDF, CSV, Excel (.xlsx/.xls), JSON/JSONL, and TSV files; structured data files parsed with pandas (schema + stats + preview); large reads capped at 80K chars; operations tiered into safe/write/destructive
- **⏰ Timer** — desktop notification timers with SQLite persistence via APScheduler; supports set, list, and cancel

#### Computation & Analysis (6 tools)
- **🧮 Calculator** — safe math evaluation via simpleeval — arithmetic, trig, logs, factorials, combinatorics, all `math` module functions
- **🔢 Wolfram Alpha** — advanced computation, symbolic math, unit/currency conversion, scientific data, chemistry, physics
- **🌤️ Weather** — current conditions and multi-day forecasts via Open-Meteo (free, no API key); includes geocoding, wind direction, and WMO weather code descriptions
- **👁️ Vision** — camera capture and screen capture with analysis via Ollama vision models; configurable camera and vision model selection
- **🧠 Memory** — persistent personal knowledge base with save, search, list, update, and delete operations across 6 categories
- **🔍 Conversation Search** — natural language search across all past conversations; keyword matching over checkpoint history with thread names and dates
- **🖥️ System Info** — full system snapshot via psutil: OS, CPU, RAM, disk space per drive, local & public IP, battery status, and top 10 processes by CPU usage
- **📊 Chart** — interactive Plotly charts from data files; structured spec tool supporting bar, horizontal_bar, line, scatter, pie, donut, histogram, box, area, and heatmap; reads from workspace files or cached attachments; auto-picks columns when x/y are omitted; dark theme with interactive zoom/hover/pan

### 🧠 Long-Term Memory

A completely new feature. The agent can now remember personal information across conversations:

- **6 categories**: `person`, `preference`, `fact`, `event`, `place`, `project`
- **Agent-driven saving** — the agent recognizes when you share something worth remembering and saves it automatically
- **Cross-conversation recall** — search and retrieve memories from any conversation
- **Full CRUD** — save, search, list, update, and delete memories via natural language
- **SQLite storage** at `~/.thoth/memory.db` with WAL mode
- **Settings UI** — browse, search, filter by category, and bulk-delete from the Memory tab
- **Destructive confirmation** — deleting memories requires explicit user approval

### 👁️ Vision System

New camera and screen capture integration:

- **Webcam analysis** — *"What's in front of me?"*, *"Read this document I'm holding up"*
- **Screen capture** — *"What's on my screen?"*, *"Describe what I'm looking at"*
- **Configurable models** — choose from gemma3, llava, and other Ollama vision models
- **Multi-camera support** — select which camera to use from Settings
- **Inline display** — captured images appear in the chat alongside the analysis

### 🎤 Voice Input

Fully local, hands-free voice interaction:

- **Wake word detection** — 2 built-in wake words (Hey Jarvis, Hey Mycroft) via OpenWakeWord ONNX models
- **Speech-to-text** — faster-whisper with selectable model size (tiny/base/small)
- **Configurable sensitivity** — wake word threshold slider (0.1–0.95)
- **Audio chime** on wake word detection
- **Voice bar UI** — shows listening/transcribing status with real-time feedback
- **Mic gating** — microphone automatically muted during TTS playback to prevent echo and feedback loops
- **Follow-up mode** — after TTS finishes speaking, the mic re-opens briefly so you can ask follow-up questions without re-triggering the wake word

### 🔊 Text-to-Speech

Neural speech synthesis, fully offline:

- **Piper TTS engine** — bundled with installer at the time (engine + default voice); additional voices downloaded from HuggingFace on demand *(replaced by Kokoro TTS in v3.1.0)*
- **8 voices** — US and British English, male and female variants *(expanded to 10 voices with Kokoro in v3.1.0)*
- **Streaming playback** — responses spoken sentence-by-sentence as tokens stream in
- **Smart truncation** — long responses are summarized aloud with full text in the app
- **Code block skipping** — TTS intelligently skips fenced code blocks
- **Mic gating integration** — coordinates with voice input to mute mic during playback and re-enable after

### 💬 Chat Improvements

- **Streaming responses** — tokens appear in real-time with a typing indicator animation
- **Thinking indicators** — "Working…" status when the model is reasoning
- **Tool call status** — expandable status widgets showing which tools are being called and their results
- **Inline YouTube embeds** — YouTube URLs in responses render as playable embedded videos
- **Syntax-highlighted code blocks** — fenced code blocks render with language-aware highlighting and a built-in copy button via `st.code()`
- **File attachments** — drag-and-drop images, PDFs, CSV, Excel, JSON, and text files into the chat input; images analyzed via vision model, PDFs text-extracted, structured data files parsed with pandas (schema + stats + preview), text files injected as context
- **Inline charts** — interactive Plotly charts rendered inline in chat when the Chart tool is used; charts persist across page reloads; dark theme with zoom/hover/pan
- **Image captions** — user-attached images display as "📎 Attached image", vision captures display as "📷 Captured image"
- **Onboarding guide** — first-run welcome message with tool categories, settings guidance, voice tips, and file attachment instructions; 6 clickable example prompts; `?` button in sidebar to re-display; persistence via `~/.thoth/app_config.json`
- **Startup health check** — verifies Ollama connectivity and model availability on launch with user-friendly error messages
- **Conversation export** — export threads as Markdown, plain text, or PDF with formatted role headers and timestamps
- **Stop generation** — circular stop button to cancel streaming at any time- **Live token counter** — gold-themed progress bar in the sidebar showing real-time context window usage based on trimmed (model-visible) history
- **Truncation warnings** — inline warnings when file content was truncated to fit context
- **Error recovery** — agent tool loops (GraphRecursionError) are caught gracefully with a user-friendly message; orphaned tool calls are automatically repaired
### 🛡️ Destructive Action Confirmation

The agent now uses LangGraph's `interrupt()` mechanism to pause and ask for user confirmation before performing dangerous operations:

- File deletion and moves (Filesystem)
- Sending emails (Gmail)
- Moving and deleting calendar events (Calendar)
- Deleting memories (Memory)

The user sees a confirmation dialog with the action details and can approve or deny.

### ⚙️ Settings Overhaul

The Settings dialog has been expanded from a simple panel to a **9-tab dialog**:

1. **🤖 Models** — brain model selection, context window slider, vision model selection, camera picker
2. **🔍 Search** — toggle and configure search tools (Web Search, DuckDuckGo, Wikipedia, Arxiv, YouTube, Wolfram Alpha) with inline API key inputs and setup instructions
3. **📄 Local Documents** — upload, index, and manage documents for the FAISS vector store
4. **📁 Filesystem** — workspace folder picker, operation tier checkboxes (read/write/destructive)
5. **📧 Gmail** — OAuth setup with step-by-step instructions, credentials path picker, authentication status, operation tier checkboxes
6. **📅 Calendar** — OAuth setup (shared credentials with Gmail), authentication, operation tiers
7. **🔧 Utilities** — toggle Timer, URL Reader, Calculator, Weather tools
8. **🧠 Memory** — enable/disable, browse stored memories, search, filter by category, bulk delete
9. **🏛️ Preferences** — voice input (wake word, Whisper model, sensitivity), TTS (voice selection, speed) *(TTS engine changed to Kokoro in v3.1.0)*

### 🖥️ System Tray Launcher

`launcher.py` provides a system tray experience:

- **Tray icon** with color-coded voice state (green = listening, yellow = processing, grey = off)
- **Manages Streamlit subprocess** on port 8501
- **Auto-opens browser** on launch
- **Polls `~/.thoth/status.json`** for live state updates
- **Graceful shutdown** — clean process termination on Quit

### 📦 Data Storage

All user data now lives in `~/.thoth/`:

- `threads.db` — conversation history and LangGraph checkpoints
- `memory.db` — long-term memories (new)
- `api_keys.json` — API keys
- `tools_config.json` — tool enable/disable state and configuration (new)
- `model_settings.json` — selected model and context size (new)
- `processed_files.json` — tracked indexed documents
- `status.json` — voice state for system tray (new)
- `timers.sqlite` — scheduled timer jobs (new)
- `gmail/` — Gmail OAuth tokens (new)
- `calendar/` — Calendar OAuth tokens (new)
- `piper/` — Piper TTS engine and voice models *(replaced by `kokoro/` in v3.1.0)*

### 🧹 Codebase Changes

- **Removed**: `rag.py` (old RAG pipeline — dead code, no longer imported)
- **Added**: `agent.py`, `memory.py`, `voice.py`, `tts.py`, `vision.py`, `launcher.py`
- **Added**: `tools/` package with 16 tool modules, `base.py` (ABC), `registry.py` (auto-registration)
- **Updated**: `app.py` (complete UI rewrite — streaming, voice bar, Settings dialog, export, attachments)
- **Updated**: `threads.py` (added `_delete_thread`, `pick_or_create_thread`)
- **Updated**: `models.py` (added context size management, vision model support)
- **Updated**: `documents.py` (moved vector store to `~/.thoth/`)
- **Default model**: Changed from `qwen3:8b` to `qwen3:14b`

---

## v1.1.0 — Sharpened Recall

### RAG Pipeline Improvements
- Contextual compression retrieval — each retriever wrapped with `ContextualCompressionRetriever` + `LLMChainExtractor`
- Query rewriting — follow-up questions automatically rewritten into standalone search queries
- Parallel retrieval — all enabled sources queried simultaneously via `ThreadPoolExecutor`
- Context deduplication — embedding-based cosine similarity at within-retrieval and cross-turn levels
- Character-based context & message trimming
- Smarter context assessment — embedding similarity check before LLM fallback

### UI Improvements
- Auto-scroll to show new messages and thinking spinner

---

## v1.0.0 — Initial Release

- Multi-turn conversational Q&A with persistent threads
- 4 retrieval sources: Documents (FAISS), Wikipedia, Arxiv, Web Search (Tavily)
- Source citations on every answer
- Document upload and indexing (PDF, DOCX, TXT)
- Dynamic Ollama model switching with auto-download
- In-app API key management
- LangGraph RAG state machine (`needs_context` → `get_context` → `generate_answer`)
