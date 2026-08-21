# Row-Bot — Architecture & Detailed Design

> Full technical reference for every feature, module, and subsystem in Row-Bot.
> For a concise overview, see the [README](../README.md).

Row-Bot is the renamed successor to Thoth. The product name is also the design
principle for the system: **Reason. Orchestrate. Work.** The architecture keeps
those concerns separate: reasoning lives in provider-aware agent/runtime paths,
orchestration lives in the durable parent/child runtime plus tools, workflows,
channels, skills, plugins, and MCP, and work lives in local data stores owned by
the user. Progressive capability catalogs keep extension scale out of ordinary
prompts, while one capacity-aware preparation pipeline keeps long Agent and Chat
Only conversations bounded and recoverable.

---

## Table of Contents

- [ReAct Agent Architecture](#react-agent-architecture)
- [Context Window Accounting & Rolling Compaction](#context-window-accounting--rolling-compaction)
- [Provider-Aware Reasoning Controls](#provider-aware-reasoning-controls)
- [Generation Cancellation & Stop Propagation](#generation-cancellation--stop-propagation)
- [Agent Execution Budgets & Delegation Capacity](#agent-execution-budgets--delegation-capacity)
- [Durable Parent/Child Agent Orchestration](#durable-parentchild-agent-orchestration)
- [Agent Profiles, Goal Mode & Child Agents](#agent-profiles-goal-mode--child-agents)
- [Long-Term Memory & Knowledge Graph](#long-term-memory--knowledge-graph)
- [Wiki Vault](#wiki-vault)
- [Dream Cycle](#dream-cycle)
- [Document Knowledge Extraction](#document-knowledge-extraction)
- [Brain Model & Providers](#brain-model--providers)
- [Embeddings & Vector Indexing](#embeddings--vector-indexing)
- [Voice Input & Text-to-Speech](#voice-input--text-to-speech)
- [Shell Access](#shell-access)
- [Browser Automation](#browser-automation)
- [Native Computer Use (Beta)](#native-computer-use-beta)
- [Google Calendar](#google-calendar)
- [Vision](#vision)
- [Workflows & Scheduling](#workflows--scheduling)
- [Designer Studio](#designer-studio)
- [Developer Studio](#developer-studio)
- [Custom Tools](#custom-tools)
- [Row-Bot Status & Identity](#row-bot-status--identity)
- [Self-Knowledge & Insights](#self-knowledge--insights)
- [Controlled Self-Evolution](#controlled-self-evolution)
- [Messaging Channels](#messaging-channels)
- [Tunnel Manager](#tunnel-manager)
- [X (Twitter) Tool](#x-twitter-tool)
- [Tool Guides](#tool-guides)
- [Progressive Tool & Skill Discovery](#progressive-tool--skill-discovery)
- [Skills Hub & Skill Activation](#skills-hub--skill-activation)
- [Image Generation](#image-generation)
- [Video Generation](#video-generation)
- [MCP Client & External Tools](#mcp-client--external-tools)
- [Migration Wizard](#migration-wizard)
- [Legacy Thoth Upgrade Policy](#legacy-thoth-upgrade-policy)
- [Plugin System & Marketplace](#plugin-system--marketplace)
- [Auto-Updates](#auto-updates)
- [Habit & Health Tracker](#habit--health-tracker)
- [Desktop App](#desktop-app)
- [Single-Owner Remote Access & Server Mode](#single-owner-remote-access--server-mode)
- [Docker & VPS Runtime](#docker--vps-runtime)
- [Mobile Web Companion](#mobile-web-companion)
- [Chat & Conversations](#chat--conversations)
- [Notifications](#notifications)
- [Stability & Diagnostics](#stability--diagnostics)
- [Bundled Skills](#bundled-skills)
- [Public Docs Site & Automation](#public-docs-site--automation)
- [Core Modules](#core-modules)
- [Data Storage](#data-storage)
- [Comparison with Other Tools](#comparison-with-other-tools)

---

## ReAct Agent Architecture

- **Autonomous tool use** — the agent decides which tools to call, when, and how many times, based on your question
- **30+ core tools plus discoverable external capabilities** — web search, email, calendar, file management, shell access, browser automation, opt-in native Computer Use, vision, image generation, video generation, X (Twitter), a personal knowledge graph, Agent Profiles, Goal Mode, child-agent delegation, Designer Studio, Developer Studio, Custom Tool Builder, scheduled workflows, habit tracking, Row-Bot Status self-inspection, and enabled MCP, plugin, Custom Tool, and channel tools
- **Streaming responses** — tokens stream in real-time with a typing indicator
- **Provider-aware reasoning controls** — exact-model capability metadata exposes only valid Provider default, effort, On/Off, or token-budget choices; the selected value is isolated per thread and provider-qualified model, mapped into the native transport request, and reset safely if the provider rejects it before output
- **Unified context management** — Agent Mode, tool-loop model calls, approval/orchestration resumes, and Chat Only assemble and count the complete next provider request through one capacity-aware preparation path; fixed prompt/tool overhead is checked before history compaction, automatic rolling compaction begins at 75% of the effective limit, and one exact rebuild prevents a stale projection from entering a retry loop
- **Progressive external capability loading** — recommended Auto mode binds permitted core tools plus bounded search/invoke bridges and keeps enabled external schemas in an immutable authorized catalog until needed; eager compatibility mode remains available, and background workflow runs retain an eager snapshot
- **Explicit prompt context/cache sections** — `prompt_context.py` assembles named stable and ephemeral sections so identity, profile, platform, self-knowledge, tool guides, manual skills, plugin skills, turn state, memory recall, Developer context, Designer context, channel state, and history have deterministic cache behavior
- **Runtime readiness routing** — before building the graph, selected models are evaluated for context headroom, provider capability metadata, tool support, and surface requirements; full agent mode, chat-only mode, and blocked states are explicit outcomes rather than accidental provider failures
- **Chat-only runtime** — models that are useful for normal conversation but cannot reliably accept tool schemas use a compact tool-free prompt, a shaped transcript without full tool bodies, and the normal streaming/persistence path
- **Profile- and skill-aware prompting** — Agent Profiles, pinned/manual skills, per-thread/per-workflow overrides, progressively selected task skills, and tool guides are resolved before prompt assembly so the agent receives only the relevant operating instructions for the current surface
- **Parent-led orchestration** — parent agents can delegate focused work to durable required or detached child runs with profile snapshots, context summaries, dependencies, tool allowlists, explicit local-folder or worktree assignments, wait/stop/status controls, and ordered parent events; required results rejoin the same authoritative parent turn instead of becoming disconnected summaries
- **Provider transcript normalization** — model-facing histories are checked for duplicate tool-call IDs, orphan tool results, invalid tool calls, empty assistant turns, and unsafe reasoning/tool artifacts before replay to custom or hosted providers
- **Centralized prompts plus self-knowledge injection** — base prompt templates live in `prompts.py`, while `self_knowledge.py` injects a dynamic identity line, capability manifest, and live runtime state so Row-Bot can describe itself accurately without stale hard-coded copy
- **Event-driven context meter** — the responsive desktop composer shows the estimated complete next input, effective capacity, and automatic compaction threshold; persisted usage restores after reopen, compact desktop layouts preserve the meter in a narrower control row, and mobile renders durable compaction notices without widening the input
- **Generation-scoped stop & error recovery** — each active generation owns a cancellation scope that closes registered provider responses, terminates registered subprocesses, wakes queued state, and stops only generation-linked child runs; checkpointed model-iteration budgets and no-progress detection wind down long or repetitive tool loops; orphaned tool calls are repaired without replay, including interrupted-parent recovery after restart; provider/API errors are surfaced as persistent red toasts and saved to the conversation checkpoint so they survive thread refresh
- **Workflow cancellation** — running background workflows can be stopped from the chat header, activity panel, or workflow card; cancellation is checked between every LangGraph node for clean shutdown
- **Displaced tool-call auto-repair** — if context trimming displaces tool-call/response pairs, the agent automatically detects and repairs the ordering before the next LLM call; orphaned tool calls trigger an automatic retry
- **Grouped tool traces** — repeated tool calls of the same type are grouped into quiet compact expandable rows, keeping long research, browser, and Developer runs readable while preserving individual results and real discovered-integration labels
- **Thinking retention** — non-empty reasoning/thinking text is separated from answer tokens, preserved across streaming, detached reattach, checkpoint loading, and final transcript rendering, and shown in a collapsed Thinking trace without treating reasoning-only chunks as the visible final answer
- **Date/time awareness** — current date and time is injected into every LLM call so the model always knows "today"
- **Destructive action confirmation** — dangerous operations (file deletion, sending emails, deleting calendar events, deleting memories, deleting workflows, selected settings changes) require explicit user approval via an interrupt mechanism
- **Workflow-scoped background permissions** — background workflows use a tiered system: safe operations always run, low-risk operations (move file, move calendar, send email) are allowed with optional runtime guards, and irreversible operations (delete file, delete memory) are always blocked; shell commands and email recipients can be allowlisted per-workflow via the editor UI

---

## Context Window Accounting & Rolling Compaction

Context capacity is resolved before each model call and exact input preparation
is shared across Agent Mode and Chat Only. `models.py` owns the versioned
capacity policy, `agent.py` assembles, counts, compacts, and emits usage events,
`threads.py` persists validated summaries, usage snapshots, and presentation
events, and `ui/chat_components.py` renders the responsive desktop meter.

- **Policy version 3** — `model_settings.json` distinguishes Auto from fixed
  local allocation and represents the provider/custom cap as an optional
  advanced override. Historical local 32K and provider 128K defaults migrate
  to Auto because legacy files cannot distinguish a former default from an
  explicit choice; exact values from 16,384 through 4,194,304 are preserved
  instead of being snapped to a preset
- **Capacity resolution** — `get_context_policy()` combines provider catalog or
  maintained metadata, local requested and observed Ollama allocation, and
  explicit advanced caps. Local Ollama Auto targets 65,536 tokens, bounded by
  the model's native or observed allocation; fixed 32K remains available
- **Custom endpoint authority** — custom/self-hosted endpoints are always
  treated as server-managed. Auto requires native catalog metadata, an exact
  model probe, or a manual declaration; an unknown custom capacity stays
  unavailable instead of inheriting the generic 128K remote fallback. A custom
  cap limits Row-Bot's accounting/compaction authority and does not reconfigure
  the server; llama.cpp capacity is configured with server-side `--ctx-size`,
  not an undocumented chat-completion `n_ctx` field
- **Application authority boundary** — the disclosed 128K fallback for other
  unknown remote models and most provider overrides govern trimming,
  accounting, and compaction only; they are not presented as native provider
  metadata or sent as runtime context parameters
- **Model-scoped probe evidence** — custom endpoint catalog, chat, tool,
  streaming, and context probe results carry the exact model id. Legacy
  unscoped evidence is accepted only when the endpoint exposes one model, so a
  successful tool round-trip for one model cannot make a sibling model Agent
  ready
- **Provider headroom** — combined input/output windows expose 85% as usable
  input, input-only provider limits expose 95%, and both start automatic
  compaction at 75% of the effective limit
- **Complete next-input count** — system and contextual prompts, the projected
  transcript, images, and the exact bound tool schemas are counted together
  with LangChain's deterministic approximate counter. The fixed system/tool
  envelope is preflighted before any history compaction; if it cannot fit, the
  turn stops with the exact token requirement and recovery guidance.
  Provider-reported usage is normalized afterward for diagnostics, not used to
  rewrite the estimate
- **One preparation contract** — Agent Mode pre-model hooks, tool-loop rounds,
  approval/orchestration resumes, and Chat Only call the same preparation and
  compaction functions, preventing the UI estimate from describing a different
  payload than the runtime path
- **Rolling user-led boundaries** — compaction selects complete older groups,
  normally retains at least two recent complete user turns, keeps tool calls
  adjacent to their results, and incrementally incorporates a prior validated
  summary. If that two-group tail cannot fit, all but the newest atomic group
  can age into the summary; an oversized current group still fails bounded
- **Untrusted historical reference** — the summary uses fixed headings for the
  current goal, constraints, completed work, state, relevant details, and next
  step, then enters the provider request inside an explicit untrusted
  `HISTORICAL_CONTEXT` boundary subordinate to the newest raw user instruction
- **Bounded compactor request** — the selected task model summarizes only the
  aged range, output is capped from effective capacity and compacted size,
  large ranges roll through several bounded requests, and Ollama summary calls
  preserve the resolved `num_ctx` while disabling hidden reasoning where needed
- **Revision-safe persistence** — summary state records mode, model/provider,
  checkpoint revision, message boundary/digest, prompt/tool/policy fingerprints,
  and creation time. A per-task lock plus compare-and-swap prevents stale or
  concurrent compaction from replacing a newer conversation
- **Concurrent-winner recovery** — when compare-and-swap loses, the runtime
  accepts only another summary that validates against the same current
  transcript and still creates sufficient safe slack
- **Exact rebuild fallback** — after a successful summary, the runtime rebuilds
  the real provider request and may compact once more if the exact result still
  exceeds the safe limit. Only the final successful summary/usage state is
  saved, preventing intermediate compaction state from repeating on the next
  model pass
- **Durable usage snapshots** — `thread_meta.context_usage_json` stores the
  event-driven estimate with capacity source/state, effective/usable/compact-at
  values, mode, model ref, checkpoint revision, and preparation/policy
  fingerprints; stale projections are cleared when model or prompt policy moves
- **Presentation-only events** — `thread_events` records idempotent compaction
  success or failure at a transcript boundary. These rows are merged into the
  visible timeline but never enter prompts, summaries, or later token counts
- **Channel claim separation** — channel-originated compaction creates one
  separately claimed notification, so a timeline event can be visible locally
  without producing duplicate external messages
- **Safe failure and overflow** — a failed compaction may continue only while
  the unchanged input remains below the usable limit; oversized failures stop
  with recovery guidance, and a provider overflow marks that model unavailable
  for the same session until the model or reviewed override changes. Known
  bounded compaction failures log reason codes, while unexpected failures log
  only the exception class rather than transcript or provider content
- **Privacy boundary** — counting and policy resolution are local. Compaction
  uses the already selected task model, so a cloud-selected task sends the
  bounded aged range to that same provider as part of normal model use

---

## Provider-Aware Reasoning Controls

Reasoning is modeled as exact provider/model capability state rather than one
global switch. `providers/reasoning.py` resolves capabilities, validates and
persists selections, builds a transport-neutral request plan, and owns
display-safe fallback notices. Provider constructors translate that plan into
the native request shape, while returned reasoning is kept distinct from final
answer text.

- **Exact capability resolution** — reasoning metadata comes from the selected
  provider-qualified catalog row where available, then narrowly maintained
  exact-model data or reviewed Ollama/OpenCode family routing. A model without
  supported metadata receives no control rather than a guessed option
- **Typed selections** — `ReasoningSelection` supports Provider default,
  provider-defined effort levels, On, Off, and positive token budgets; each
  choice is validated against the exact model's supported values and bounds
- **Thread and model isolation** — `thread_meta.reasoning_selections_json`
  stores selections by canonical `model:<provider>:<model>` ref. Switching
  models restores that model's own value, and unsupported stale selections are
  cleared back to Provider default with a visible notice
- **Shared visible controls** — desktop Chat, Designer, and Developer composers
  render the same Thinking picker beside Model and Approval controls; the
  compact mobile chat exposes the corresponding control. `/reasoning` can
  inspect or change the current value, and a bare desktop/mobile command opens
  the visible picker
- **Channel command parity** — Telegram, Discord, Slack, WhatsApp, and SMS
  command paths validate `/reasoning default`, provider effort names, `on`,
  `off`, or `budget <tokens>` against the channel thread's selected model
- **Native request mapping** — OpenAI/Codex, Anthropic/Claude Subscription,
  Google, xAI, Ollama/Ollama Cloud, OpenRouter, OpenCode, and custom-compatible
  runtimes receive only their supported effort, thinking, enable/disable, or
  budget fields; request-plan fingerprints also keep model caches isolated
- **Custom endpoint controls** — advanced custom endpoint settings can declare
  Auto/On/Off reasoning, an optional budget, replay support, and bounded extra
  request JSON. Reasoning fields are preserved in provider-facing history only
  when replay was explicitly declared
- **Reasoning stream separation** — native reasoning blocks, compatibility
  metadata, and `<think>` streams are decoded into reasoning events; answer
  text remains a separate stream, and callback isolation prevents duplicate
  token or reasoning events through wrapper layers
- **Single compatibility retry** — `providers/transports/reasoning_fallback.py`
  retries one provider rejection of an explicit reasoning setting with Provider
  default only before any output is emitted. It clears the saved override and
  queues a local notice, but never retries authentication, rate-limit, timeout,
  cancellation, server, or mid-stream failures

---

## Generation Cancellation & Stop Propagation

Stop is modeled as generation-scoped control state rather than a UI-only event.
`cancellation.py` binds one `CancellationScope` to an active generation and lets
provider transports, subprocesses, tools, and child-agent scheduling register
cleanup callbacks against that scope. This keeps cancellation local to the turn
the user stopped while allowing blocked I/O to be interrupted immediately.

- **Scope lifecycle** — `request_generation_stop()` marks the active generation stopped, sets its stop event, cancels its scope, detaches or wakes queued UI state, and leaves unrelated generations and child runs untouched
- **Provider response closure** — `providers/transports/cancellable_http.py`, `anthropic_cancellable.py`, and `openrouter_cancellable.py` register in-flight sync/async responses for closure across direct OpenAI, Anthropic, xAI, MiniMax, OpenRouter, OpenCode, and compatible runtime construction paths
- **Custom transport propagation** — Codex, Claude Subscription, Ollama Cloud, xAI OAuth Responses, and shared OpenAI-compatible transports register their request/stream handles with the same scope
- **Cancellable subprocesses** — `process_cancellation.py` starts process groups, captures stdout/stderr into temporary files, enforces timeouts, and terminates the group when the generation scope is cancelled; file-backed capture lets the directly launched process define completion even when a deliberately detached descendant inherits output handles, and shell/Developer commands return explicit stopped or timed-out results without an unbounded second drain
- **Tool integration** — browser and Computer Use actions, MCP probes/tool calls, shell sessions, Developer runtime processes, and voice-agent turns check or register with the current scope at their blocking boundaries
- **Child-run ownership** — generation-linked async child runs are tracked separately from older or independently started child work, so Stop terminates only the runs created for that generation
- **Cancellation-safe finalization** — UI and channel delivery paths distinguish cancellation from successful final output, clean partial previews, avoid false completion persistence, and retain durable state needed for a later user turn
- **Deterministic coverage** — focused tests close fake HTTP responses, terminate fake subprocess groups, cancel provider and tool waits, wake generation queues, and prove unrelated child runs survive

---

## Agent Execution Budgets & Delegation Capacity

Agent limits are explicit application state rather than hidden framework
recursion constants. `agent_budget.py` owns one checkpoint-safe budget per
logical turn, while `agent_settings.py` owns the application-wide defaults used
by new parent and child runs.

- **Narrow checkpoint schema** — each turn stores a schema version, opaque budget id, logical turn id, maximum and used model iterations, finalization flags, and terminal reason without persisting tool arguments or model input
- **Provider-response charging** — a pre-model hook verifies remaining capacity immediately before a provider call, and a post-model hook charges exactly one iteration only after a successful provider response
- **Derived graph ceiling** — the LangGraph recursion limit is calculated from remaining model iterations, keeping the framework guard above the authoritative product budget instead of using it as the user-visible limit
- **Interrupt and resume continuity** — approvals and other checkpoint interrupts keep the same budget identity and used count; legacy interrupted checkpoints without a budget receive one bounded migration value before resuming
- **Exactly-once terminal response** — budget exhaustion claims one tool-free finalization pass, persists its started/completed state, and prevents duplicate finals when UI, child-run, or resume paths observe the same terminal condition
- **No-progress detection** — exact tool name/argument requests are represented only by a process-keyed HMAC digest; the fourth identical request is blocked and a continued fifth request terminates the turn as `no_progress`
- **Durable progress** — successful model rounds update `agent_runs.model_iterations_used`, the snapshotted maximum, and heartbeat/active-time metadata without copying model inputs into the Agent Run database
- **Typed runtime settings** — `agent_settings.json` stores maximum work rounds, nested child levels, active children per parent, active children across the application, and optional child active-time seconds through validated atomic writes
- **Reviewed defaults** — new runs default to 90 model iterations, one child level, three active children per parent, eight active children globally, and no child timeout
- **FIFO capacity queue** — `agent_runner.py` admits a child only when both parent and global capacity are available; excess children remain queued in order and can still be stopped while waiting
- **Snapshot semantics** — each Agent Run stores the effective settings snapshot it started with, so later Settings changes affect only new runs and cannot alter an interrupted or active run mid-flight
- **Active-time timeout** — the optional child timer starts only after capacity and any write lock are acquired, excludes queue time, and records `timeout` as a durable terminal reason

---

## Durable Parent/Child Agent Orchestration

`agent_orchestrator.py` turns child delegation into a durable parent-led state
machine. One orchestration is bound to a parent thread and generation. The
original parent can suspend after starting required work, wake on durable child
or user events, run more model passes, delegate later waves, and produce the
single authoritative final answer.

- **Versioned orchestration store** — `agent_orchestrations`,
  `agent_orchestration_members`, and `agent_orchestration_messages` live beside
  Agent Runs in `tasks.db`; schema repair and JSON normalization fail toward
  bounded empty values rather than leaving partial state active
- **Generation identity** — concurrent creation for the same parent generation
  resolves to one orchestration, preventing two parent coordinators from owning
  the same turn
- **Required and optional members** — required children participate in the
  parent's completion barrier; detached children remain durable and visible but
  cannot hold the foreground response open
- **Dependency graph and waves** — a child may depend on members from the same
  orchestration, later delegation waves remain attached to the original parent,
  and superseded retry attempts cannot satisfy a barrier twice
- **Ordered parent inbox** — child lifecycle events, approval requests,
  completions, retries, user steering, and stop requests are persisted and
  consumed in order; duplicate callbacks coalesce onto one event identity
- **Live group joins** — `wait_for_required_group()` checkpoints the exact
  required event set so a foreground parent can join children inside one stream
  when they finish promptly and safely suspend when the wait bound is reached
- **Parent lease** — only one background parent runner can claim an
  orchestration at a time; lease release and wake coalescing prevent concurrent
  model passes when several children finish together
- **Checkpoint binding** — suspension records the parent checkpoint id,
  namespace, output metadata, selected model, approval mode, and active
  Developer/Designer resource identity before the foreground generation exits
- **Initial continuation snapshot** — the first asynchronous `delegate_work`
  call stores a whitelist of durable parent config, enabled tools, model,
  approval, workspace/project, channel, and voice fields before ToolNode
  returns; internal Pregel callbacks and other non-serializable graph state are
  excluded
- **Approval continuity** — parent and child approvals persist independently;
  a decision resumes the original checkpoint, and handled approval payloads are
  not reused for later interrupts
- **Steering** — later user input is routed into the active orchestration inbox
  and wakes the parent promptly while child completion events remain ordered and
  coalesced
- **Transient retry** — classified provider/runtime failures can create one
  replacement attempt that inherits the logical member, dependency position,
  and parent return path
- **Exactly-once finalization** — acknowledgement, result synthesis, local chat
  output, channel delivery, and Goal continuation use durable claims and
  idempotency keys so callback races cannot duplicate a final response
- **Surface-aware delivery** — local chat, supported channels, voice, and Buddy
  observe the same durable parent lifecycle; failed channel delivery remains
  retryable without replaying successful output
- **Stop semantics** — an individual member can stop without cancelling its
  siblings, while group stop wakes the parent so it can explain the partial or
  cancelled outcome naturally
- **Restart repair** — startup repair materializes any already-recorded child
  terminal events, marks unsafe in-flight work interrupted, and waits for an
  explicit resume instead of invoking providers during recovery
- **Parent checkpoint repair** — explicit resume reconstructs the recorded
  parent config, closes only unanswered tool calls with an interrupted marker,
  never replays the abandoned call, and refuses recovery if the checkpoint
  cannot be repaired safely
- **Resume revalidation** — explicit resume checks agents, model readiness,
  workspace/worktree state, and the exact Designer project binding before
  requeueing only interrupted required work; if required children are already
  terminal, it wakes the original parent even for an older v2 row whose initial
  continuation snapshot is empty
- **Bounded result packet** — ordered member results and worktree references are
  capped before parent synthesis, preserving useful handoff context without
  allowing unbounded child output into the prompt

---

## Agent Profiles, Goal Mode & Child Agents

Agent Profiles and Goal Mode sit above the base ReAct loop. They let a normal
conversation become a visible, role-aware work session without turning Row-Bot
into an unbounded autonomous system.

### Agent Profiles

- **Profile model** — `agent_profiles.py` stores built-in and user profiles with slug, display name, description, when-to-use guidance, instructions, handoff contract, enabled state, and JSON policy blocks for tools, skills, context, workspace, and approvals
- **Profile snapshots** — each run receives a profile snapshot so long-running child agents keep the instructions and policy they started with even if the profile library changes later
- **Prompt integration** — `agent.py` injects the active profile as a structured system context for normal agent runs and chat-only turns; missing or disabled profiles produce a model-visible warning and user-facing recovery guidance instead of failing silently
- **Thread binding** — `threads.py` stores the selected profile for a thread, while `profile_library.py`, `profile_picker.py`, and slash/profile commands expose selection and review without requiring manual config edits
- **Policy summary** — profile prompt context includes capability, context, workspace, allowed tools, and pinned profile skills so the model sees a compact boundary rather than the entire profile JSON
- **Built-in profiles** — the bundled profile library provides focused roles for research, review, implementation, orchestration, and other repeatable work patterns; users can duplicate or save profiles when a successful run should become reusable guidance

### Goal Mode

- **Durable goal state** — `goals.py` persists the current thread goal with objective, status, progress, evidence, blockers, next step, turn count, max turns, active run id, and completion/block verdict metadata
- **Goal tools** — `tools/goal_tool.py` registers `goal_update` and `goal_status` so agents can report progress, evidence, blockers, or completion through structured state instead of relying only on prose in the transcript
- **Visible progress** — `ui/goal_ui.py`, the Command Center, and streaming/status surfaces show active goal state, grouped goal activity, and blockers while the user continues working elsewhere
- **Channel support** — channel command/runtime paths carry goal context so work started from Telegram, WhatsApp, Discord, Slack, or SMS can still update the same durable thread goal
- **Bounded persistence** — goals are local records, not hidden background mandates; users can clear or replace them, and goal completion is based on evidence plus explicit agent status rather than unchecked self-assertion
- **Orchestration continuation** — a parent orchestration final records one Goal turn, then `after_orchestration_completion()` applies the normal evidence/budget decision and can start the next Goal turn without duplicating the completed answer

### Child-Agent Runs

- **Durable run store** — `agent_runs.py` stores child-agent runs with parent thread id, parent run/message ids, orchestration membership, objective, display name, profile id/snapshot, context summary, enabled tools, model override, skills/tools overrides, approval mode, status, status message, summary, and event history
- **Runner lifecycle** — `agent_runner.py` queues, starts, waits for, stops, and finalizes child runs while preserving terminal states and recent events for parent inspection and returning orchestration members through the durable parent inbox
- **Capacity and timeout policy** — child runs queue against snapshotted per-parent/global concurrency limits, enforce configured nesting depth, and can use an opt-in active-execution timeout that excludes queue time
- **Delegation tools** — `tools/agent_tool.py` exposes `delegate_work`, `agent_status`, `agent_wait`, `agent_stop`, `agent_profiles`, `agent_profile_save`, `agent_message`, and `agent_promote`; delegation can mark work required or detached, attach same-orchestration dependencies, or register one existing `developer_workspace_path` for the child, mutually exclusive with a saved workspace id
- **Parent visibility** — parent agents can inspect one run, list child runs for the current thread, join a required group, wait for a result, or record steering for queued/non-terminal work; compact group/member cards and lifecycle state survive checkpoint reloads and queued parent turns
- **Parent-thread approvals** — child-agent approval requests are serialized through `approval_messages.py` and `agent_run_messages.py`, appended once to the parent thread, and refreshed through run-state keys so background work cannot wait invisibly
- **Approval explanations** — approval cards prefer a bounded, redacted model-supplied reason while preserving the raw command/action payload for the real safety decision; shell and Developer tools accept a dedicated `approval_reason` field
- **Terminal channel notices** — async child completions can enqueue a compact once-only notice for the originating channel; failed delivery remains pending and is reconciled when channels auto-start again
- **Promotion paths** — completed child runs can be promoted into a new Agent Profile or a disabled manual workflow; both paths are approval-gated and leave artifacts reviewable before reuse
- **Tool allowlists** — profile and delegation tool allowlists flow into agent graph construction; plugin and MCP tools honor the allowlist so a child agent can run with a narrower tool surface than the parent
- **Workspace-keyed write locks** — write-capable children assigned to the same Developer workspace still serialize, while children assigned to distinct registered local folders receive distinct lock keys and may run concurrently; changing only the shell CWD never bypasses workspace ownership
- **Status diagnostics** — Row-Bot Status can report agent/profile/run state, making delegated work visible to diagnostics and support flows rather than trapping it in transient UI state
- **Budget diagnostics** — run rows expose safe used/maximum model-iteration counts and terminal reasons, while active settings and dispatcher capacity remain queryable without exposing prompts or tool arguments
- **Durable activity projection** — `get_thread_orchestration_activity()` derives blocking, approval, retry, detached, interrupted, stopped, and terminal state for sidebar, mobile, Agent drawer, Buddy, and streaming surfaces from persisted state rather than UI-local timers

---

## Long-Term Memory & Knowledge Graph

Row-Bot doesn't just store isolated facts — it builds a **personal knowledge graph**: a connected web of people, places, preferences, events, and their relationships. Every memory is an entity linked to others through typed relations, so the agent can reason about how things in your life connect.

- **Entity-relation model** — memories are stored as entities with a type, subject, description, aliases, and tags; entities are connected by typed directional relations (e.g. `Dad --[father_of]--> User`, `User --[lives_in]--> London`)
- **10 entity types** — `person`, `preference`, `fact`, `event`, `place`, `project`, `organisation`, `concept`, `skill`, `media`
- **Memory tool** — 7 sub-tools let the agent save, search, list, update, delete, **link**, and **explore** memories through natural conversation
- **Link memories** — the agent can create relationships between any two entities, building a richer graph over time
- **Explore connections** — the agent can traverse the graph outward from any entity, discovering chains of relationships for broad questions like family, work, and projects
- **Interactive memory visualization** — a dedicated **Knowledge** surface renders the entire knowledge graph as an interactive network diagram with search, filters, full-graph / ego-graph toggle, and detail cards
- **Bounded auto-recall policy** — before every response, `memory_policy.py` builds a deterministic recall query, retrieves candidates, scores them against tier/status/confidence/evidence/recency/query fit, applies a context-aware token budget, and records a compact recall trace for diagnostics
- **Hybrid recall candidates** — recall combines FAISS semantic search, FTS5 lexical search, keyword fallback, and graph-neighbor expansion; strong seed memories can pull in related entities with relation confidence and hop metadata
- **Bounded semantic fallback** — missing, failed, or slow local embedding loads set a structured fallback code and continue through lexical, keyword, and graph retrieval instead of blocking the turn or downloading a model implicitly
- **Single visible fallback notice** — `memory_policy.py` deduplicates one display-safe notice per generation, explains the local model recovery action, and records timing/status metadata without including memory content
- **Recall-safe retrieval** — candidate inspection does not mutate memory state; only memories actually injected into the turn are reinforced with `recalled_at` and recall-count metadata
- **Automatic memory extraction** — a background process scans past conversations on startup and every 6 hours, extracting entities and relations the agent missed during live conversation; active threads and workflow threads are excluded; assistant messages are truncated to 200 chars to prevent extracting from AI-generated content; low-confidence relations are skipped and conflicting facts can be marked for review instead of overwriting high-authority user edits
- **Deterministic deduplication** — both live saves and background extraction check for existing entities by normalized subject before creating new entries; cross-category matching prevents fragmentation; alias resolution ensures related names merge; richer content is always kept
- **Memory evolution metadata** — `memory_evolution.py` normalizes status (`active`, `needs_review`, `superseded`, `archived`), tier (`core`, `semantic`, `episodic`, `resource`), confidence, evidence, source context, manual edits, superseding, archival, and journal entries
- **Vague-type banning** — `related_to`, `associated_with`, `connected_to`, `linked_to`, `has_relation`, `involves`, and `correlates_with` are rejected before saving, preventing noisy low-value edges
- **Relation pre-normalization** — alias forms are canonicalized before ban, confidence, and dedup checks
- **67 valid relation types** — curated vocabulary with 60+ alias mappings plus document-specific relations like `extracted_from`, `uploaded`, `builds_on`, `cites`, `extends`, and `contradicts`
- **Source and audit tracking** — each entity is tagged with its origin (`live`, `extraction`, `dream_*`, document-derived, wiki-synced, or manual) plus audit metadata such as status, tier, confidence, evidence, source context, and user-modified timestamps
- **Semantic and lexical recall indexes** — FAISS vectors are backed by the configured embedding provider, while an optional FTS5 entity index supports exact/keyword recall and fallback search
- **Memory IDs in context** — auto-recalled memories include their IDs so the agent can update or delete specific entries when the user corrects previously saved information
- **Consolidation utilities** — built-in duplicate consolidation merges near-duplicate memories that may accumulate over time
- **Local SQLite + NetworkX + FAISS storage** — entities and relations live in `~/.row-bot/memory.db`, mirrored in a NetworkX graph for traversal, with FAISS vectors in `~/.row-bot/memory_vectors/`
- **Knowledge audit UI** — browse, search, visualize, review, restore, supersede, archive, and bulk-delete memories from the Knowledge tab and entity editor, including graph statistics, audit badges, recall traces, and the memory evolution journal

---

## Wiki Vault

The knowledge graph can be exported as a structured **Obsidian-compatible markdown vault** — one `.md` file per entity with YAML frontmatter, `[[wiki-links]]`, and auto-generated indexes.

- **Vault structure** — entities grouped by type (`wiki/person/`, `wiki/project/`, `wiki/event/`, etc.) with one `.md` file per entity; sparse entities (<20 chars) roll up into `_index.md` per type; per-type indexes and a master `index.md` are auto-generated on rebuild
- **YAML frontmatter** — each article includes `id`, `type`, `subject`, `aliases`, `tags`, `source`, `created`, and `updated` metadata
- **Text-stable entity IDs** — `id` is quoted on export and parsed as text even when an older vault wrote it unquoted, preventing numeric-looking identifiers from being coerced by JSON/YAML-style parsing
- **Wiki-links** — related entities linked via `[[Entity Name]]` syntax, enabling Obsidian backlinks and graph view
- **Connections section** — outgoing and incoming relations listed with arrow notation
- **Live export** — entities are exported on save, deleted on entity removal, and rebuilt on batch operations
- **Search** — full-text search across all `.md` files with title, snippet, and entity ID results
- **Conversation export** — any thread can be exported as a vault-compatible markdown file
- **Agent tools** — `wiki_read`, `wiki_rebuild`, `wiki_stats`, and `wiki_export_conversation` let the agent interact with the vault directly
- **Settings UI** — enable/disable toggle, vault path configuration, stats display, rebuild, and open-folder actions in the Knowledge tab

---

## Dream Cycle

A 5-phase background daemon refines the knowledge graph during idle hours and ends with an insight-generation pass over recent system activity.

- **Phase 1: Duplicate merge** — entities with ≥0.93 semantic similarity and same type are merged; the LLM synthesizes the best description, aliases are unioned, and relations are re-pointed to the survivor
- **Subject-name guard** — entities with different normalized subjects require ≥0.98 similarity to merge, preventing false merges of distinct people or concepts
- **Phase 2: Description enrichment** — thin entities (<80 chars) appearing in multiple conversations get richer descriptions from conversation context and graph neighborhood
- **Phase 3: Confidence decay** — stale `dream_infer` relations older than 90 days lose 10% confidence per cycle; very low-confidence edges are pruned automatically
- **Phase 4: Relationship inference** — co-occurring entity pairs with no meaningful edge are evaluated for a specific typed relation; hub diversity caps, batch rotation, half-overlap reuse, multi-excerpt evidence, and a 7-day rejection cache improve quality and reduce repetition
- **Phase 5: Insights analysis** — the system captures a snapshot of recent logs, provider/model/media configuration, channels, task state, memory stats, skills, and existing insights, feeds it to `DREAM_INSIGHTS_PROMPT`, and stores actionable results in `insights.py`
- **Three-layer anti-contamination** — sentence-level excerpt filtering, deterministic post-enrichment validation, and strengthened prompting prevent cross-entity fact bleed
- **Ollama busy check** — queries `/api/ps` before starting; defers if Ollama is actively serving a user request to avoid competing for GPU or CPU
- **Configurable window** — default 1–5 AM local time; checks every 30 minutes if enabled, idle, in window, and not yet run that day
- **Dream journal** — all operations logged to `~/.row-bot/dream_journal.json` with cycle ID, summary, duration, merges, enrichments, inferences, insights, and errors
- **Post-cycle rebuilds** — FAISS is rebuilt after the cycle, and the wiki vault is regenerated when enabled so downstream views stay in sync
- **Manual trigger** — a dedicated Dream button in the Knowledge surface can start the cycle immediately
- **Settings UI** — enable/disable toggle, quiet window controls, and last-run summary in the Preferences tab

---

## Document Knowledge Extraction

Uploaded documents now move through a durable bounded pipeline before structured
knowledge is committed to the graph. `document_uploads.py` owns disk-first
staging, `document_jobs.py` owns queue state and recovery, `document_index.py`
owns atomically published retrieval shards, and `document_extraction.py` owns
resumable map/reduce knowledge extraction.

- **Streaming staging** — uploads are read in chunks of at most 1 MiB, hashed
  incrementally, capped at 256 MiB per file, and rejected before acceptance when
  they would cross the 2 GiB staging free-space reserve
- **Path containment** — original names are sanitized, stored names include the
  opaque job id, staging/work/completed paths remain under the ingestion root,
  and interrupted cleanup removes only the current job's temporary directory
- **Content identity** — SHA-256 content hashes skip duplicates already indexed
  or selected twice in one batch, while files with the same display name and
  different bytes coexist under stable document IDs
- **Durable batch/job state** — `document_ingestion/jobs.db` records validated
  stages and statuses, per-job progress, batch pause/cancel flags, source paths,
  embedding metadata, error codes, searchable state, and extraction checkpoints
- **Single-flight supervisor** — one process lock and one renewable SQLite lease
  serialize background work; FIFO selection indexes every document in a batch
  before starting extraction so search becomes available promptly
- **Recovery** — restart recovery restarts interrupted index builds, resumes
  extraction from persisted summaries, fails missing sources explicitly, clears
  expired leases, and retires orphan staging/work directories
- **Queue controls** — batches and jobs support persisted pause/resume, cancel,
  retry-failed, clear-finished, and active-document cancellation without using
  sleeps or UI-only state
- **Bounded vector build** — embeddings are requested in batches of at most 32
  chunks and written into document segments capped at 2,000 chunks
- **Atomic publication** — a document is built under unpublished work state and
  becomes searchable only when its shard metadata is committed into the corpus
  manifest; failed manifest replacement leaves the previous corpus visible
- **Deterministic retrieval** — `DocumentVectorStoreFacade` merges compatible
  sharded results with compatible legacy FAISS data, sorts deterministic top-k
  candidates, excludes stale embedding configs, and honors legacy tombstones
- **Rolling map phase** — ordered overlapping text windows are summarized to
  persisted map rows; cancellation and provider failures are checked between
  calls so a retry starts at the next incomplete window
- **Hierarchical reduce phase** — summaries are reduced in persisted groups of
  at most eight until one final article remains, bounding every provider input
  and allowing restart at the next incomplete level/group
- **Extract phase** — core entities and relations are pulled from the final
  article; extraction remains capped at 12 entities per document and uses the
  curated 67-relation vocabulary plus existing quality gates
- **Idempotent finalization** — the document hub, raw Wiki Vault copy, extracted
  graph content, and batch-wide graph/wiki rebuild are committed by document id
  and guarded against duplicate completion callbacks
- **Source provenance** — document hubs and extracted facts retain resource-tier
  metadata, evidence, content identity, stored/original names, and
  `extracted_from` edges without blindly overwriting personal memories
- **Visible operations** — Documents settings shows batches, jobs, progress,
  controls, indexed records, and embedding mismatch warnings; the status bar
  shows the active file/stage and queue/index health covers corruption, missing
  sources, partial shards, and orphan data
- **Supported formats** — PDF, DOCX, TXT, Markdown, HTML, and EPUB remain
  supported through lazy/page-aware loaders where available
- **Per-document cleanup** — removal targets the document id, retires its source,
  deletes only its shard and source-tagged graph content, and releases cached
  embedding resources

---

## Brain Model & Providers

The brain model is Row-Bot's default LLM — the model used for conversations, memory extraction, dream analysis, and any thread, profile, child-agent run, or workflow without a specific override. It is selected during setup or later from Settings, and can come from the supported local runtime, a hosted provider, OpenCode Zen/Go, Requesty, ChatGPT / Codex, Claude Subscription, xAI Grok OAuth, Ollama Cloud, or a custom OpenAI-compatible endpoint.

Row-Bot is local-first in its data model, but model routing is provider-neutral. Local models remain a first-class path for offline and private use, while hosted and self-hosted models can be selected per thread, workflow, Agent Profile, child-agent run, Developer workspace, or media surface. The setup wizard determines the initial default; on the local path, Row-Bot uses one of the models already exposed by the local runtime, with 14B-class models recommended for stronger agent/tool behavior.

Provider models are supported for users without a dedicated GPU, for frontier reasoning on demand, or for trying many providers without downloading large local weights. Row-Bot supports opt-in provider models through **OpenAI** (direct API), **Anthropic** (Claude through the API-key route), **Google AI** (Gemini), **xAI** (direct Grok API), **xAI Grok OAuth** (subscription/OAuth-backed Grok runtime and Grok Imagine media), **MiniMax** (live catalog through the Anthropic-compatible API), **OpenCode** providers, **OpenRouter** (many third-party models), **Atlas Cloud** (OpenAI-compatible access to Atlas-hosted chat, agent, and vision models discovered from the live provider catalog), **Requesty** (OpenAI-compatible model gateway with Requesty-specific catalog metadata), **Ollama Cloud** (direct API and local daemon cloud-tagged models), **ChatGPT / Codex** (subscription-backed Codex models), **Claude Subscription** (subscription-backed Claude models), and Custom/Self-hosted OpenAI-compatible endpoints such as oMLX, LM Studio, vLLM, llama.cpp, LocalAI, LiteLLM, SGLang, or private gateways. Provider connections, health, and credential sources are configured from Settings -> Providers; model catalog browsing, pinning, and defaults live in Settings -> Models.

The `providers/` subsystem now owns provider config, auth metadata, model catalog normalization, capability and reasoning resolution, runtime construction, display-safe status, runtime readiness, and Quick Choices. Model selections are preserved as provider-qualified refs (`model:<provider>:<model>`) at UI and settings boundaries so a local/custom model does not silently fall back to OpenRouter, xAI, or another provider when multiple providers expose the same or unknown bare model id. Existing public functions in `models.py` remain as compatibility facades while provider-backed selection is rolled through the app. Settings -> Models pickers are intentionally Quick Choice surfaces: catalog rows must be pinned before they become everyday Brain, Vision, Image, or Video choices, while the current default can still appear as a fallback value. `providers/model_catalog_cache.py` refreshes hosted-provider and local-runtime catalog rows in the background so Settings can render from cache without blocking on large remote catalogs. Targeted and scheduled refreshes iterate registered providers, including Codex subscription discovery, and commit rows provider-by-provider. Empty, failed, or partially paginated results retain that provider's last-known-good rows; successful results replace only that provider's rows. Provider Settings surfaces whether the current snapshot came from a live refresh, cache, or fallback.

Live catalog fetches obtain secrets through
`providers/auth_store.get_provider_secret()` rather than the legacy generic
environment-key facade. OpenAI, Ollama Cloud, OpenRouter, Requesty, Anthropic,
Google, xAI, MiniMax, OpenCode, and Atlas Cloud can therefore refresh from a
provider-scoped secret saved in Settings even when no duplicate legacy key or
environment variable exists. Catalog rows remain provider-qualified and secret
values never enter cache metadata.

OpenCode Zen and OpenCode Go use a two-source native routing catalog. A refresh
intersects the gateway's live `/models` response with the public
`https://models.dev/api.json` registry, then maps the registry's per-model npm
package to OpenAI Chat, OpenAI Responses, Anthropic Messages, or Google GenAI.
Each provider-qualified row retains context, modalities, tool, streaming, and
reasoning metadata, so a newly listed supported model can become runnable after
refresh without a hard-coded name. The registry is fetched once per full
refresh; valid empty gateway results clear stale rows, gateway/registry failures
preserve the provider's last-known-good cache, cold failures use the static
fallback, and Zen/Go outcomes remain isolated. Cached native routes restore
after restart; older rows use the narrow static classifier, while malformed or
unsupported packages fail closed with display-safe diagnostics.

Runtime readiness is evaluated before agent execution. `providers/readiness.py`, `providers/resolution.py`, and `providers/capability_resolution.py` resolve the selected model/provider, inspect cached capability snapshots, probe uncertain local/custom models when needed, compare the effective context window against tool-schema requirements, and return one of three outcomes: full agent mode, chat-only mode, or blocked with user-facing guidance. Custom probe evidence is scoped to the exact model: a successful chat check makes only that model conversational, and Agent readiness still requires its own successful tool round-trip. Forced-agent surfaces such as workflow execution, approval resumes, and Designer text generation request agent mode explicitly; normal chat can fall back to chat-only mode when a model is conversationally useful but not tool-compatible.

Provider-facing tool schemas are also checked at graph construction time.
`providers/tool_schema.py` extracts each tool's effective JSON schema and applies
a transport-scoped compatibility policy. For Google/Gemini, it validates both
the source schema and the locked Google adapter's converted schema so every
array has usable typed `items`. Optional incompatible tools are omitted in
stable order; an explicitly selected incompatible tool produces a clear build
error. Other transports retain the original tool objects unchanged. Built-in
Gmail recipient arrays and Goal evidence/blocker arrays use concrete string
item schemas while validators preserve compatible legacy input forms.

Atlas Cloud is a first-class provider with provider id `atlascloud`, not a generic custom endpoint profile. `providers/atlascloud.py` owns the Atlas setup copy, auth mapping to `ATLASCLOUD_API_KEY`, live catalog fetch, provider-qualified model refs, chat/agent/vision capability classification, and filtering rules. Atlas chat and multimodal rows can appear in Brain and Vision Quick Choices when their capability snapshot allows that surface, while Atlas image-generation and video-generation rows are intentionally filtered out of chat, agent, and vision pickers for this phase.

Atlas uses the shared OpenAI-compatible transport, but endpoint-specific behavior is scoped by provider id. The transport keeps Atlas tool-call buffering, Claude-shaped stream/error normalization, native tool-history replay cleanup, and Atlas-specific timeout/error text out of OpenRouter, custom endpoints, and other OpenAI-compatible providers unless those providers explicitly opt into the same path.

The shared OpenAI-compatible transport separates a 10-second connect timeout,
10-second pool timeout, 120-second write timeout, and 900-second read-inactivity
timeout. `ROW_BOT_OPENAI_COMPATIBLE_READ_TIMEOUT` can replace only the positive
read limit; invalid, non-finite, zero, or negative values log a warning and use
900 seconds. A timeout or remote-protocol failure retries once only before the
first stream event. After any emitted event the error propagates without replay,
and cancellation is checked before a retry.

Requesty is a first-class OpenAI-compatible provider with provider id `requesty`. `providers/requesty.py` maps Requesty's live `/models` response into Row-Bot model metadata, including `context_window`, `supports_tool_calling`, `supports_reasoning`, `supports_vision`, task, modality, and nested capability fields. Requesty rows use provider-qualified refs and are filtered so embedding, image, video, audio, moderation, realtime, and other non-chat rows do not appear as Brain or agent choices.

xAI has two explicit provider paths. The existing `xai` provider uses `XAI_API_KEY` for direct API access. The new `xai_oauth` provider represents xAI Grok subscription/OAuth access, stores Row-Bot-owned OAuth tokens through the provider auth store, reports token health and OAuth client-id status, and uses provider-qualified refs so OAuth-backed Grok rows never collapse into API-key xAI rows.

`providers/xai_oauth.py` owns the OAuth PKCE flow, token refresh, account/user/email hash metadata, model catalog reads, runtime availability checks, runtime and vision probes, and display-safe status. `providers/transports/xai_oauth_responses.py` adapts the xAI Responses-style runtime into the same chat/model construction boundary as other first-class providers. `providers/xai_catalog.py` merges xAI `/models` and `/language-models` metadata for the API-key path, hides unusable rows, and preserves curated Grok chat and media entries when upstream catalogs omit expected rows.

Grok Imagine media is scoped through the provider/media layer instead of the chat model picker. `providers/xai_media.py` handles xAI image, video, and image-to-video request payloads, while `providers/media.py`, `tools/image_gen_tool.py`, and `tools/video_gen_tool.py` expose `grok-imagine-image`, `grok-imagine-image-quality`, and `grok-imagine-video` only on image/video surfaces when the relevant API-key or OAuth runtime is available.

ChatGPT / Codex is deliberately modeled as a subscription provider, not as another OpenAI API-key route. Direct Codex runtime requires Row-Bot's in-app ChatGPT device-flow sign-in so Row-Bot stores its own runnable OAuth tokens in the local OS credential store. Existing Codex CLI auth files can be referenced only as display-safe metadata: Row-Bot records that the external login exists, path/fingerprint metadata, and broad auth-file shape, but it does not copy runnable tokens from `~/.codex/auth.json`.

Codex runtime uses ChatGPT's subscription/internal Codex backend rather than the public OpenAI API. That means endpoint behavior, catalog shape, auth requirements, rate limits, and model availability may change upstream. When a ChatGPT / Codex model is selected, the current conversation plus model-visible tool context and tool results are sent to ChatGPT / Codex for that turn. Durable Row-Bot data such as memories, documents, files, and other conversations remain local unless explicitly included in the active conversation or surfaced by a tool result.

Claude Subscription is also modeled as a subscription provider, not as another Anthropic API-key route. It uses provider id `claude_subscription` and requires provider-qualified refs such as `model:claude_subscription:claude-sonnet-4-6`; bare `claude-*` ids continue to infer the existing `anthropic` API provider for backward compatibility. Direct runtime requires Row-Bot-owned Claude OAuth tokens or an explicit user import into Row-Bot. External Claude Code files, `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_AUTH_TOKEN`, and related Claude CLI state are discovery/import aids only and are not silently reused for runtime access.

Claude Subscription runtime uses Row-Bot's native Messages transport with OAuth bearer auth, streaming, image input, tool schemas, and tool-result replay through the normal Row-Bot agent loop. It never reads `ANTHROPIC_API_KEY`, never instantiates the Anthropic API-key runtime, and never falls back between the Anthropic API provider and the Claude Subscription provider. Row-Bot supports in-app Claude OAuth and explicit `claude setup-token` import as Row-Bot-owned auth paths; because public Claude subscription app access is still policy-sensitive upstream, this provider is treated as experimental and local-user-owned.

Claude Subscription's Settings card includes a runtime diagnostic that exercises native OAuth chat, a forced Row-Bot tool call, and tool-result replay. The diagnostic result is metadata-only state under `providers.claude_subscription.last_runtime_probe`; failed diagnostics downgrade only Claude Subscription readiness so an account/entitlement failure is visible without changing Anthropic API behavior. Claude Code `claude -p` remains part of the optional Claude Code Delegation workflow, not the Claude Subscription provider transport.

- **Dynamic model switching** — change the brain model from Settings or approved `row_bot_update_setting` calls; choices are validated against pinned local/provider Quick Choices, installed local models, and provider catalogs before saving
- **Per-thread, per-profile, per-child-run & per-workflow model override** — conversations, Agent Profiles, delegated child runs, and workflows can each run on a different model, with overrides persisted locally or snapshotted into the run
- **Quick Choices** — models pinned from the consolidated Models catalog appear in chat, workflow, channel, Designer, status-tool, and Vision pickers when their capability snapshot supports that surface
- **Live provider discovery** — provider catalogs can be refreshed from live APIs where supported; Atlas Cloud, MiniMax, Requesty, Codex, and other registered providers participate in targeted/scheduled refresh, xAI API-key rows merge the provider's available model endpoints, and Claude Subscription/xAI Grok OAuth use live catalog reads only with Row-Bot-owned OAuth so API-key and subscription model paths remain distinct
- **Provider-scoped catalog credentials** — live refresh resolves the same provider secret source used by Settings and runtime construction, allowing provider-only key saves to populate catalogs without mirroring values into a legacy generic key slot
- **Provider-isolated catalog commits** — refreshes preserve last-known-good rows for failed, empty, or partially paginated providers, replace only successfully refreshed provider rows, and record live/cached/fallback provenance for Settings and diagnostics
- **Gemini tool-schema compatibility** — Google-bound tools are checked after the locked adapter conversion for typed array items and valid unions; optional incompatible tools are filtered, explicitly selected incompatible tools fail clearly, and non-Google transports remain unchanged
- **Prompt-cache gating** — `prompt_cache.py` applies Anthropic `cache_control` markers only to eligible stable system content for the direct Anthropic API and normalizes provider cache read/write token metadata for diagnostics
- **Capability-aware surface filtering** — catalog rows carry chat, agent/tool, vision, image, video, context, and provider provenance metadata; Brain, Vision, Image, and Video pickers each filter against the relevant surface instead of treating every remote catalog row as a runnable chat model
- **OpenCode native runtime** — Zen/Go are first-class provider entries whose live gateway rows use models.dev native transport metadata, including Google GenAI, rather than being forced through one generic compatible protocol; provider-isolated cached and static fallbacks keep restart and outage behavior deterministic
- **Provider-aware reasoning** — exact catalog/model metadata determines valid reasoning choices, per-thread selections are converted into native provider request fields, returned reasoning stays separate from answer text, and a rejected explicit value can fall back once to Provider default before output
- **Capacity-aware context management** — complete-input accounting and rolling compaction use native/catalog/observed capacity when available, local Ollama Auto targets 64K, custom endpoints require detected or declared capacity, exact advanced values are preserved, and an unknown non-custom remote model's 128K value is disclosed application authority rather than provider metadata
- **Local catalog accuracy** — installed Ollama chat models remain visible even when their family is newer than Row-Bot's curated tool/vision heuristics, while embedding-like local models are kept out of chat choices and Vision support is only inferred from known metadata/families
- **Native Ollama tool metadata** — `providers/ollama.py` gives an explicit
  `tool_calling` boolean first priority, otherwise consumes the daemon's
  `capabilities` list when present and treats `tools` as authoritative positive
  or negative evidence; curated family detection is used only when native
  metadata is absent
- **Ollama Cloud paths** — direct Ollama Cloud API keys and local daemon `:cloud` models are represented separately while sharing catalog normalization and display metadata; direct API errors are normalized into user-facing provider messages
- **Tool-support validation** — unsupported or uncertain local/custom models are warned about, can be probed with a real tool round-trip, and route to agent, chat-only, or blocked mode based on the result
- **Custom endpoint compatibility profiles** — OpenAI-compatible endpoints can use oMLX, LM Studio, vLLM, llama.cpp, LocalAI, LiteLLM, SGLang, or generic profiles to normalize message content, tool history, unsupported parameters, streaming behavior, reasoning replay, and bounded extra request JSON
- **Custom endpoint probes** — self-hosted/proxy endpoints can be probed for model catalog access, streaming deltas, tool-call round trips, native context metadata, and no-auth behavior; evidence is persisted against the exact tested model for later routing decisions
- **Configurable context window** — Auto/fixed local allocation and the optional advanced provider/custom cap are stored under policy version 3; exact 16,384–4,194,304 values survive round trips, actual model limits are still respected, override caches are invalidated when caps change, and custom caps remain Row-Bot planning limits rather than server reconfiguration
- **OpenAI-compatible timeout/retry contract** — phased connect/write/pool/read limits keep long generations alive without allowing connection setup to hang, and the single retry is restricted to failures before the first stream event so partial assistant or tool output is never replayed
- **Provider transcript hygiene** — provider-facing message histories are normalized to strip invalid tool calls, rewrite duplicate tool-call IDs, drop orphan tool results, flatten tool history for non-tool profiles, consolidate late system messages for Claude-shaped routes through OpenRouter/Requesty, repair Google reasoning blocks, and preserve or suppress reasoning fields depending on endpoint support
- **Local & provider indicators** — the UI clearly distinguishes downloaded local models, missing local models, and connected provider models
- **Provider vision detection** — provider models with image capability are detected and reused by the Vision feature when available; Atlas-hosted multimodal chat models keep Vision capability when metadata or curated provider-family rules support it, and xAI Grok OAuth can run a vision probe to confirm Grok image-input support before advertising Vision readiness

---

## Embeddings & Vector Indexing

Embeddings are configured separately from chat models so users can choose the privacy/performance tradeoff that fits document search, memory recall, and knowledge graph rebuilds.

- **`embedding_config.py`** — persists the selected embedding provider, model, dimension metadata, and privacy-related settings
- **`embedding_providers.py`** — normalizes local and cloud embedding backends behind one interface used by document search, memory recall, and graph/vector rebuilds
- **Local choices** — Qwen3 0.6B, Nomic Embed Text v1.5, and Mixedbread Embed Large v1 are explicit local-cache choices; Mixedbread is the reviewed default
- **Cache-only normal runtime** — local provider construction resolves a cached Hugging Face snapshot with the same GGUF, ONNX, and OpenVINO ignore filters used by explicit download and passes local-only loading flags, so recall, indexing, status checks, startup, and normal background work cannot trigger a surprise download or select a differently shaped snapshot
- **Explicit download/repair** — only the Settings download or repair action permits a remote snapshot fetch; status probes distinguish cached, missing, loading, failed, and ready states without starting a network call
- **Shared asynchronous load** — concurrent recall callers share one background provider build; the first caller receives one bounded grace wait, later callers reuse the same state, and a completed load becomes available without rebuilding
- **Failure cache and fallback contract** — missing, failed, and timeout conditions raise structured `LocalEmbeddingUnavailable` codes, fail fast on later callers, and let memory/document/workflow retrieval continue through bounded non-semantic paths
- **Cloud option** — cloud embeddings can be enabled explicitly in Settings and show privacy copy because document or memory text is sent to the chosen embedding provider
- **Stale-index detection** — vector stores record embedding provider and dimension metadata so Row-Bot can detect when a document or memory index was built with a different embedding configuration
- **Document shard metadata** — every published document shard records the active provider/model/dimension identity; incompatible shards remain visible to health diagnostics but are excluded from retrieval until rebuilt
- **Memory release** — heavy document and extraction jobs release cached embedding resources after use to reduce long-session RSS growth
- **Settings integration** — embedding provider controls live in the model/settings surfaces without overloading the chat model picker

---

## Voice Input & Text-to-Speech

Row-Bot has two voice paths: a local STT/TTS loop for privacy-first dictation
and playback, and a realtime voice runtime for lower-latency conversational
sessions with provider-backed events and action handling. Local Talk and
Dictation can use faster-whisper or the explicitly installed FunASR/SenseVoice
runtime; browser-local remote voice continues to use its cache-only Whisper
path.

- **Toggle-based voice** — simple manual toggle to start and stop listening, no wake word required
- **Classic local pipeline** — stopped -> listening -> transcribing -> muted state transitions keep manual speech input explicit and gate the microphone during playback
- **Local speech-to-text choices** — faster-whisper (tiny/base/small/medium,
  CPU-only int8) remains the default, while SenseVoice Small can be selected
  independently for Talk and Dictation and is applied by `VoiceCoordinator`
  before the local microphone service starts
- **Explicit SenseVoice installation** — `voice/local_provider.py` exposes
  package, platform, model-missing, model-invalid, and ready states without
  importing heavy modules or downloading during probes; only the guarded Voice
  settings action requests the approximately 940 MB ModelScope snapshot
- **Contained offline inference** — only a complete snapshot containing the
  required config and model files under `cache/sensevoice` is accepted and
  persisted in `voice_settings.json`; FunASR receives the verified local path,
  `device="cpu"`, and `disable_update=True`
- **Platform and disclosure boundary** — Intel macOS remains on Whisper because
  matching CPU PyTorch/Torchaudio wheels are unavailable. The UI discloses the
  ModelScope request, SDK user-agent, size, and Apache-2.0 model license before
  install; audio, prompts, and usage data are never sent
- **Neural TTS** — high-quality text-to-speech via Kokoro, fully offline
- **10 voice options** — US and British English, male and female variants
- **Streaming TTS** — responses are spoken sentence-by-sentence as they stream in
- **Mic gating** — microphone is automatically muted during TTS playback to prevent echo and feedback loops
- **Realtime voice runtime** — `voice/realtime_client.py`, `voice/runtime.py`, provider adapters, and UI event presenters coordinate low-latency sessions separately from the classic text-turn pipeline
- **Provider abstraction** — realtime voice providers share a base contract for session setup, input/output events, speech status, and shutdown; OpenAI realtime and local-provider scaffolding are represented through the same runtime boundary
- **Agent bridge** — `voice/agent_bridge.py` maps realtime voice events into Row-Bot agent actions without letting the voice client bypass tool, approval, or runtime readiness policy
- **Voice actions** — `voice/actions.py` keeps action dispatch explicit so voice sessions can request supported app actions through controlled handlers
- **Cue and speech policy** — `voice/cue_policy.py`, `voice/cues.py`, `voice/speech_policy.py`, and `voice/output_controller.py` coordinate conversational cues, spoken output timing, interruption, and playback state
- **UI lifecycle** — `ui/voice_lifecycle.py` and `ui/voice_realtime_events.py` surface session state, provider events, and recovery paths without coupling the chat transcript directly to provider-specific event streams
- **Browser-local remote path** — `voice/browser_client.py` captures microphone audio only in the authenticated browser, while `voice/browser_local.py` validates/decodes it, runs local Whisper, and returns session-scoped Kokoro output without starting the host device microphone service; non-local browser capture requires HTTPS

---

## Shell Access

- **Full shell access** — the agent can run shell commands on your machine through natural conversation
- **Persistent sessions** — `cd`, environment variables, and other shell state persist across commands within a conversation; each thread gets its own isolated shell session
- **3-tier safety classification** — commands are classified as safe, moderate, or blocked before execution
- **Safe commands run instantly** — read-only operations like `ls`, `pwd`, `cat`, `git status`, or `pip list` execute without interruption
- **Dangerous commands require approval** — destructive or system-modifying commands trigger an interrupt so you can accept or reject them
- **Blocked by default** — high-risk commands like `shutdown`, `reboot`, or `mkfs` are rejected outright
- **Background safety integration** — safe commands always execute; moderate commands are blocked by default in workflows but can be allowlisted per workflow; dangerous commands remain blocked
- **Inline terminal panel** — command output appears in a collapsible terminal panel in the chat UI with clear and history controls
- **History persistence** — shell history is saved per thread in `~/.row-bot/shell_history.json`
- **Stop propagation** — shell execution uses `process_cancellation.py`, so cancelling the owning generation terminates the command process group and returns a stopped result without losing persistent-session bookkeeping
- **Detached-child completion** — stdout/stderr use file-backed capture so a
  background process intentionally launched by the direct shell command cannot
  hold the command's pipes or session lock open after the launcher process exits

---

## Browser Automation

- **Full browser automation** — the agent can navigate websites, click elements, fill forms, scroll pages, and manage tabs in a real, visible Chromium window
- **Shared visible browser** — runs with `headless=False` so you can see what the agent is doing and intervene when needed
- **Persistent profile** — cookies, logins, and local storage survive across restarts in `~/.row-bot/browser_profile/`
- **Accessibility-tree snapshots** — after every action the tool captures the page's accessibility tree with numbered references so the model can click and type by number
- **Smart snapshot filtering** — deduplicates links, drops hidden elements, and caps interactive elements to keep context under control
- **Snapshot compression** — older browser snapshots are compressed to short stubs while the latest state remains detailed
- **7 browser operations** — navigate, click, type, scroll, snapshot, back, and tab management
- **Per-thread tab isolation** — each chat thread or background workflow gets its own browser tab; tabs are cleaned up on thread deletion or workflow completion
- **Navigation policy** — destinations are normalized and checked before navigation, keeping blocked schemes and out-of-scope redirects from silently broadening the browser task
- **Consequence policy** — browser mutations share approval/consequence classification with other action tools, while observations and reversible navigation remain distinct
- **Redacted durable history** — typed values and sensitive action payloads are omitted from browser history and tool-trace persistence; the active accessibility snapshot remains model context rather than an audit copy of secrets
- **Live takeover state** — browser tasks use the shared live-control view model for Stop, user takeover, and resume/done state without merging the browser DOM engine with native Computer Use
- **Automatic browser detection** — prefers installed Chrome, then Edge on Windows, then Playwright's bundled Chromium
- **Crash recovery** — if the browser closes externally, the next action relaunches it cleanly
- **Generation cancellation** — browser operations check the active cancellation scope before and during blocking work so Stop can abandon the current action without affecting tabs owned by other threads

---

## Native Computer Use (Beta)

Computer Use is a separate native-desktop engine for Windows and macOS. It does
not reuse the browser's persistent profile or DOM references, and it does not
turn the reviewed Cua Driver into a general MCP server. The accepted dependency
and telemetry decision is documented in
[`COMPUTER_USE_SECURITY.md`](COMPUTER_USE_SECURITY.md).

- **Provider-neutral tool boundary** — `tools/computer_use_tool.py` exposes launch, target-window observation, click, double-click, right-click, type, key/hotkey, scroll, and drag operations independently of the selected chat/agent provider
- **Private Cua client** — `computer_use/client.py` starts the reviewed Cua Driver over a private stdio MCP transport, allows only the required tool names, normalizes results, disables upstream update checks, and never registers the process in the external MCP catalog
- **Pinned runtime manifest** — `computer_use/cua_runtime_manifest.json` records Cua Driver Rust 0.7.1, upstream tag/commit, platform asset URLs, executable candidates, telemetry contract, and SHA-256 values for Windows x86-64, Windows ARM64, and macOS universal
- **Explicit verified install** — `computer_use/readiness.py` downloads only after a user Install/Repair action, verifies the selected asset before safe extraction, writes a private runtime manifest under `runtimes/cua-driver/`, and never invokes Cua's installer or updater
- **Mandatory disclosure gate** — every executable resolution/start path requires the current Cua telemetry notice version in `computer_use_settings.json`; Cancel removes acknowledgement and disables the native tool
- **Third-party telemetry boundary** — the reviewed Cua telemetry includes a pseudonymous Cua installation id, Cua/OS/architecture metadata, event category, CI flag, and timestamp sent to Cua/PostHog. Row-Bot adds no first-party telemetry and keeps prompts, memories, secrets, screenshots, file paths, tool arguments, typed content, and channel data outside that telemetry
- **Exclusive task lease** — `computer_use/service.py` gives one interactive local task ownership of discovery, target capture, Vision fallback, and input; schedules, channels, background workflows, child agents, headless/server callers, and plugin/general MCP callers cannot acquire it
- **Target-window capture** — the session forces window-only capture and a bounded image dimension; desktop-wide capture, recording, browser/CDP, autostart, process-kill, update, maintenance, telemetry mutation, and arbitrary config surfaces remain blocked
- **Generation-bound references** — application/window targets and accessibility elements are opaque, observation-generation-bound tokens invalidated by mutation, reconnect, target drift, approval waits, Stop, and takeover
- **Mutation-observation loop** — every input action requires current scope, policy, target, and element validation and is followed by a fresh target-window observation before the agent can act again
- **Point-of-risk policy** — `computer_use/policy.py` classifies routine, consequential, always-confirm, handoff, and blocked actions. Credentials, OTPs, CAPTCHAs, biometrics, UAC/TCC, terminals, password managers, Row-Bot itself, secure desktops, and elevation cannot be automated
- **Ephemeral privacy** — screenshot bytes are not written to media or checkpoints; typed values are excluded from logs, histories, tool traces, approval payloads, memory, and durable state
- **Vision fallback** — accessibility information remains primary. When it is insufficient, only the current target-window screenshot can be sent to the configured Vision provider, whose local/cloud disclosure is shown before setup
- **Live control UI** — `ui/live_control.py` and `ui/computer_use.py` show sanitized app/state/thumbnail data plus direct Stop, Take over, and Resume actions. Take over cancels queued mutation, pauses the lease, and requires a fresh observation before resume
- **Readiness and recovery** — Settings normalizes disabled, disclosure, unsupported, not-installed, hash/version mismatch, permission, degraded, ready, and failed states; macOS recovery links directly to Accessibility and Screen Recording panes and supports recheck after TCC changes
- **Lifecycle cleanup** — generation Stop, thread cleanup, tool disablement, uninstall, and application shutdown stop the private client, invalidate targets, cancel queued work, and release the lease

---

## Google Calendar

The Calendar tool keeps Google OAuth and operation policy in Row-Bot while
treating Google client objects as request-scoped resources. This avoids sharing
non-thread-safe discovery clients when an agent or ToolNode fans out operations
within one turn.

- **Request-scoped services** — every search, create, update, move, delete, or bulk-create invocation constructs an independent Calendar service from the current credential snapshot
- **Single-flight token refresh** — concurrent callers coordinate one OAuth refresh and persist the refreshed token atomically before building their request-scoped services
- **Mutation serialization** — writes are ordered inside the process so same-turn fan-out cannot race duplicate checks or issue overlapping mutations through shared state
- **Native bulk create** — `create_calendar_events` accepts ordered event inputs, performs duplicate-safe creation, preserves result order, and returns structured success or partial-failure records
- **Ambiguous timeout reconciliation** — create operations derive a stable correlation marker and search for a backend-committed event after a timeout before deciding whether to retry, preventing duplicate calendar entries
- **Bounded retries** — transient SSL and Google backend failures retry with fresh service instances; permanent errors are returned once in structured form
- **Typed operation contracts** — schemas cover calendar selection, timezones, attendees, conference data, notification behavior, update/move/delete fields, and destructive-operation classification
- **Concurrent regression coverage** — deterministic tests exercise parallel reads, eight-way create fan-out, token refresh, transient retries, committed-timeout reconciliation, request scoping, and ordered bulk partial failure

---

## Vision

- **Camera analysis** — capture and analyze images from your webcam in real-time
- **Screen capture** — take screenshots and ask questions about what is on your screen
- **Image file analysis** — analyze workspace image files by path without needing a camera or live capture
- **Configurable vision model** — choose from pinned local or provider-capable Vision Quick Choices, including ChatGPT / Codex, Claude Subscription, and xAI Grok OAuth rows whose catalog metadata or runtime probes report image input support
- **Camera selection** — pick which camera to use when multiple devices are present
- **Inline image display** — captured and workspace images are shown inline in chat
- **Provider vision support** — provider models with image capability are auto-detected and work alongside local vision models; Quick Choice refresh preserves provider-specific Vision metadata instead of downgrading rows through generic text-only heuristics, and xAI Grok OAuth can record a provider-specific vision probe result
- **Atlas Cloud vision boundary** — Atlas-hosted multimodal chat models can be used for Vision when classified as image-capable, but Atlas image-generation and video-generation catalog rows are not exposed through Vision or Brain model surfaces
- **Media-model boundary** — Grok Imagine image/video rows are kept on Image and Video surfaces, not Vision or Brain surfaces, even when they come from the same xAI account
- **Computer Use fallback boundary** — native Computer Use may request Vision only for an ephemeral target-window screenshot when accessibility data is insufficient; the configured Vision provider and local/cloud status remain explicit

---

## Workflows & Scheduling

Tasks have been renamed to **Workflows** throughout the application. The workflow engine adds a step-based pipeline runner, delivery routing, approvals, triggers, and safety gating on top of APScheduler.

### Core Engine

- **Unified workflow engine** — named multi-step workflows run sequentially in a fresh or persistent thread and are scheduled through APScheduler
- **SQLite schema recovery** — `tasks.py` validates the workflow database schema before use, repairs partial schemas in place, backs up and recreates corrupt DBs, and retries schema-related operations once after repair
- **7 schedule types** — `daily`, `weekly`, `weekdays`, `weekends`, `interval`, `cron`, and one-shot `delay_minutes`
- **Template variables** — prompts can use `{{date}}`, `{{day}}`, `{{time}}`, `{{month}}`, `{{year}}`, `{{task_id}}`, and `{{step.X.output}}`
- **Per-workflow model/profile override** — each workflow can force a different model or Agent Profile, then restore the default after completion
- **Skills and tools overrides** — workflows can narrow the skill set globally and the tool set per step; promoted Agent-run workflows preserve the originating profile and safety context for review before enablement
- **Channel delivery** — workflow output can inherit the workflow-level default delivery channels or use a per-workflow override via `delivery_channel` and `delivery_target`; web-app run status is always preserved
- **Persistent threads** — workflows can reuse the same thread across runs to preserve context
- **Notify-only mode** — workflows can skip agent execution and just send notifications
- **Webhook triggers** — workflows can be launched by HTTP webhook with per-workflow secrets
- **Completion triggers** — one workflow can trigger another after finishing
- **Concurrency groups** — related workflows can be serialized so only one runs at a time
- **Safety mode** — `block_destructive`, `require_approval`, and `allow_all` modes control shell, workflow, and channel behavior inside background execution

### Step-Based Pipelines

- **5 step types** — Prompt, Condition, Approval, Subtask, and Notify
- **Conditional branching** — condition steps support `contains`, `not_contains`, `regex`, `json_path`, and `llm_evaluate`, each with `if_true` / `if_false` targets
- **Approval gates** — approval steps pause execution, route requests through channels and desktop notifications, and resume on explicit user approval or denial
- **Prompt chaining** — each step can see previous step output, enabling research → summarize → act patterns
- **Agent-callable workflows** — the task tool can create, update, and run full step graphs programmatically
- **Agent-run promotion** — completed child-agent runs can be promoted into disabled manual workflows, preserving the original objective, context summary, profile reference, skills/tools overrides, model override, and approval mode so the workflow can be reviewed before scheduling or enabling

### Workflow Builder UI

- **Simple/Advanced toggle** — simple mode preserves a single-prompt workflow editor; advanced mode exposes the full step builder
- **Mobile simple editor** — `ui/mobile_workflows.py` provides a full-screen phone editor for safe workflow metadata, prompt steps, schedules, profiles, model overrides, approval policy, persistent threads, channel delivery, and enablement; advanced graph steps are preserved and left for desktop editing
- **Step builder** — reorder, delete, retarget, and retype steps visually
- **Variable insertion menu** — context variables and prior-step outputs can be inserted without hand typing placeholders
- **Flow preview** — Mermaid diagram generated from the step graph with manual refresh
- **Validation** — required-field checks, reference validation, and operator-specific rules run before save
- **Delivery defaults UI** — the Workflows panel exposes a compact default-delivery selector; workflows tied to default update when the global default changes, while explicit overrides remain untouched

### Approval System

- **Pending approvals panel** — approval cards show task or child-agent source, a bounded display-safe reason, request text, and Approve / Deny controls
- **Sidebar badge** — pending approvals surface as a badge and quick actions above the thread list
- **Multi-surface routing** — approvals can be routed through desktop, the mobile Activity surface, Telegram, Discord, Slack, WhatsApp, SMS, and plugin-owned channel paths according to channel capabilities; button-capable adapters render inline controls and text-only adapters require an explicit YES/NO response
- **Resume integration** — the agent and workflow runtime resume correctly on approve or deny and follow the appropriate branch

### Workflow Console

- **Right-side console** — `ui/command_center.py` exposes running work, active goals, child-agent runs, approvals, upcoming runs, quick launch actions, recent history, and insights in one drawer
- **Collapsible layout** — console expansion state persists in browser and pywebview; collapsed state shows compact running/approval/insight badges and attention styling when an approval is waiting
- **Live operational view** — running workflows, goals, child-agent states, background states, and recent outcomes stay visible while you continue chatting elsewhere in the app
- **Insight actions** — insight cards support pin, dismiss, and apply actions directly from the console
- **Journal access** — extraction and dream journals are accessible from the same workflow-centric monitoring surfaces

### Existing Features

- **Always-background execution** — workflows run without blocking the main chat UI
- **Pre-built templates** — seeds five disabled starter workflows across simple and advanced examples; nothing is scheduled or run until the user enables it
- **Home screen dashboard** — Workflows and Activity tabs show tiles, upcoming runs, run history, channel status, pending approvals, extraction journal, and dream journal
- **Persistent run history** — execution history survives workflow deletion for auditability
- **Monitoring / polling** — interval schedules plus condition steps support ongoing monitors like price checks or release watchers
- **Stop / cancel support** — running workflows can be stopped from the chat header, activity panel, or workflow card

---

## Designer Studio

Designer Studio is Row-Bot's dedicated visual-authoring subsystem. It spans five distinct **project modes**, a sandboxed interactive runtime, an authoring guardrail stack, and a mutation-reviewable tool surface for editing projects turn over turn.

### Project Modes

Every project is created in one of five modes. Each mode carries its own canvas presets, template gallery, prompt budgets, critique rules, runtime behavior, and export targets.

- **`deck`** — traditional slide decks; 16:9 canvas; ≤5 bullets per slide; PPTX export via `python-pptx` preserves editable text runs, images, and charts
- **`document`** — long-form report / one-pager pages; A4 or letter canvas; 130–160 words per block; PDF export is the primary delivery format
- **`landing`** — interactive marketing landing pages; vertical scroll canvas; CTAs and multi-section hero / feature / pricing layouts; published as interactive HTML
- **`app_mockup`** — multi-screen app prototypes; route-aware navigator so the agent can define screens and declarative navigation between them; runtime bridge turns link / button clicks into in-preview route changes
- **`storyboard`** — motion / ad storyboards; limited to 3–4 blocks per frame to avoid cropping; pairs naturally with the video generation tool for per-frame motion references

### Interactive Runtime

Interactive modes (`landing`, `app_mockup`, `storyboard`) do **not** allow free-form `<script>` from the agent. Behavior is expressed declaratively via `data-row-bot-action` attributes and interpreted at runtime by a sandboxed bridge.

- **`designer/runtime/` package** — loads per-project runtime state, resolves route / screen navigation, handles state toggles, controls media playback, and dispatches declarative actions to real DOM operations inside the preview iframe
- **Declarative action grammar** — `data-row-bot-action="navigate:screen-id"`, `data-row-bot-action="toggle:state-key"`, `data-row-bot-action="play:asset-id"`, etc. — the agent authors intent, the runtime executes it safely
- **Shared preview + publish runtime** — the same runtime powers editor preview, presenter mode, and published share links so interactive projects behave identically in all three surfaces

### Project Model & Storage

- **Multi-page / multi-screen projects** — each project stores a page list, canvas dimensions, aspect ratio, mode, title metadata, notes, brand settings, and (for app mockups) a route map
- **Home gallery** — the Home screen includes a dedicated **Designer** tab with recent projects, new-project flows, and quick reopen actions
- **Canvas presets and resizing** — projects can be resized after creation; mode-appropriate presets are offered up front
- **Reference storage** — uploaded briefs, screenshots, and source material are stored as reusable references so future designer sessions can reopen them without reuploading
- **Asset-backed media** — project HTML stores media as `asset://<asset-id>` references rather than brittle placeholder tokens; `designer/render_assets.py` normalizes legacy refs, preserves `data-asset-id`, and hydrates assets for preview, presentation, export, and published output
- **Persistent asset storage** — designer assets live on disk under `~/.row-bot/designer/assets/`; projects and references are stored separately
- **Windows-safe writes** — `designer/storage.py` uses temp-file + replace semantics with retry logic to avoid broken saves on Windows file locks

### Editor & Authoring

- **Full-width editor** — `designer/editor.py` switches the app into a full-width editing mode with page / screen navigator, preview, controls, and assistant side chat
- **Shared chat primitives** — the designer editor reuses `ui/chat_components.py` so uploads, input behavior, and chat rendering match the main conversation UI
- **Surgical tool surface** — the designer tool can set, update, add, move, duplicate, and delete pages / screens; move, replace, restyle, and remove individual elements; refine-text-in-place (shorten / expand / simplify / rewrite); insert reusable components; update brand settings; and resize projects
- **Setup flow** — the creation flow captures mode, format, audience, tone, and source brief before generating an initial draft
- **Typed image slots** — templates declare expected image slots by semantic role (hero, thumbnail, icon, background, etc.) so generated imagery lands in intentional places with appropriate aspect ratios
- **Reusable components** — curated insertable blocks like heroes, stat bands, timelines, testimonials, pricing sections, and app shells accelerate common layouts
- **Authoring guardrails** — mode-specific content budgets, no-decorative-overlap rules, horizontal button-row rules, and slot-typed imagery are encoded in `designer/prompt.py` so the agent produces layout-clean output on first draft

### Critique & Repair Loop

- **`designer/critique.py`** — runs deterministic checks for overflow, card-heavy sections, contrast, hierarchy, readability, and spacing on any page
- **Mandatory post-edit critique** — the designer tool automatically critiques after each structural change and applies safe repairs before returning control to the agent
- **Repair operations** — deterministic, side-effect-scoped fixes (e.g. trim overflowing blocks, drop redundant bullets, fix contrast, respace buttons)
- **Review dialog** — a mutation diff view shows exactly what the agent changed on each turn, per page, so the user can accept, revert, or spot-check without hunting through the project

### AI Content

- **AI image generation** — generate slide / page imagery directly inside the designer workflow, routed into typed image slots
- **AI video generation** — storyboard frames and landing hero videos can be generated via the `video_gen_tool` and referenced as `asset://` media
- **Chart insertion** — create charts from inline CSV and place them in a page layout
- **Speaker notes** — generate and persist notes for presenter use

### Presentation, Sharing & Export

- **Presenter mode** — `designer/presentation.py` serves Reveal.js-based presenter mode with notes support (deck mode)
- **Export pipeline** — Playwright drives raster + HTML export; `python-pptx` drives editable PPTX; `weasyprint` / Playwright drive PDF; PNG exports for any page
- **Published share links** — self-contained interactive HTML (with runtime bridge) is mounted under `/published` for direct sharing

---

## Developer Studio

Developer Studio is Row-Bot's code-workspace subsystem. It is not a full IDE; it is a Codex-style agent workbench for connecting local Git repositories, reviewing code, making scoped edits, running tests, preparing branches/commits/PRs, and keeping the user in control through approval modes and an inspector.

### Workspace Model

- **`developer/` package** — owns workspace storage, Git helpers, worktree allocation, runtime profiles, approval policy, sandbox state, tool context, todos, change ledger, inspector snapshots, GitHub helpers, and UI
- **Explicit repo linking** — users open an existing local repo or clone into a folder they choose. Row-Bot stores a workspace link and metadata, not a copy of the repo in app data
- **Child folder registration** — `delegate_work(developer_workspace_path=...)`
  can register one existing local folder and assign its resulting Developer
  workspace id only to that child; missing folders and simultaneous id/path
  inputs fail before a run is created
- **Folder-scoped writer ownership** — Agent Run write locks key on the assigned
  Developer workspace, so distinct folders permit independent parallel writers
  while the same folder retains one writer and a shell CWD change has no effect
  on the lock boundary
- **Code threads** — Developer conversations are tagged as code threads and reopen directly into Developer Studio with the associated workspace context
- **Developer worktrees** — `developer/worktrees.py` allocates durable Git worktrees for threads, child-agent runs, and workflow runs, tracking owner kind/id, project workspace, worktree workspace, branch name, base branch/commit, cleanup state, metadata, and failure preservation in `tasks.db`
- **Worktree seeding** — worktrees can start from the current staged/unstaged/untracked state or from the last commit; seed application uses binary Git patches and safe untracked-file copying while requiring a real repository root
- **Workspace context injection** — Developer turns receive compact hidden context with repo path, branch, dirty state, remote URL, top-level files, approval mode, execution mode, shell guidance, and sandbox state
- **No user-message leakage** — Developer context is injected as model context and is not rendered as part of the visible user message

### Approval Modes & Tooling

- **Mode-specific policy** — read-only, ask-before-changes, auto-edit, and agent-run modes control file writes, shell commands, Git operations, commits, pushes, and PR preparation
- **Native Developer tools** — `tools/developer_tool.py` exposes workspace-scoped operations for repo info, file listing, reads, search, git status, diffs, todos, detected tests, shell commands, patch preview/apply, file writes, branch create/switch, commit, push, fast-forward merge, sandbox imports, and safe revert of agent-owned changes
- **Shell remains available** — Developer-native tools are preferred for repo work, but shell is still available for legitimate project commands and follows Developer approval policy
- **Shared checkpointed work budget** — Developer turns use the application-wide model-iteration setting and preserve workspace state, todos, and diffs when a budget or fallback framework guard ends the turn, so the user can continue from the current checkpoint
- **Tool guide and skills** — the Developer tool guide plus Developer coding/review/PR/custom-tool skills are injected for Developer context without bloating normal chat by default

### Inspector & Live State

- **Developer Inspector** — the right-side inspector shows Overview, Safety Policy, Sandbox, Todos, Changes, Files, Agent Changes, Tests, and GitHub/PR sections
- **Debounced snapshots** — `developer/inspector_snapshot.py` builds lightweight snapshots that the UI can apply without fully rebuilding the chat transcript
- **Resizable panel** — the inspector can be widened for diffs, files, and test output
- **File tree** — Files render as a repo tree instead of a flat list, with generated/build/cache paths filtered out
- **Change ledger** — `developer/change_ledger.py` tracks agent-owned edits, line counts, diffs, and revert eligibility
- **Todo persistence** — `developer/todos.py` stores visible coding plans so long-running work can show current, pending, and completed steps

### Docker Sandbox

- **Execution modes** — Local runs commands in the selected repo folder; Docker Sandbox runs commands in an isolated shadow copy
- **Persistent sandbox container** — `developer/sandbox_runtime.py` manages a per-workspace container and shadow workspace so repeated commands share sandbox state until rebuilt or cleaned
- **Import-gated edits** — sandbox changes become pending patches and only modify the real repo after explicit import
- **Network policy** — Docker Sandbox supports network off, ask, or on. Network/package-install attempts are blocked early when network is off and approval-gated when policy requires it
- **Image selection** — workspaces can choose a Docker image; changing the image cleans the sandbox copy before the next Docker command
- **Local fallback** — users who do not want Docker keep using local execution under the same Developer approval model
- **Official-container boundary** — when `ROW_BOT_CONTAINERIZED=1` marks the official server image, Docker Sandbox selection and retained Docker workspaces fail closed before probing a nested runtime; requested sandbox commands/processes never fall back to local execution, while an explicitly selected Local workspace on a reviewed mounted path remains available
- **Clear startup errors** — stopped Docker, missing local images, and Docker credential-helper problems are reported as actionable sandbox errors

### GitHub & PR Flow

- **GitHub CLI detection** — `developer/github.py` and `developer/executables.py` locate `gh` from common install paths, especially on Windows where PATH can differ between the app and a shell
- **PR helpers** — branch, commit, push, and PR-prep tools are approval-gated and operate inside the selected workspace
- **No hidden remotes** — cloning asks for an explicit destination, and push/PR operations are visible through the active approval mode

---

## Custom Tools

Custom Tools let users convert a GitHub repo, local folder, or current Developer workspace into reusable Row-Bot tools without editing manifests by hand.

### Product Surface

- **Developer home surface** — Custom Tools live under Developer as a global area separate from workspaces, with cards for created tools, source, install path, command count, enablement, test output, and removal
- **Wizard flow** — the guided flow is Source -> Inspect -> Test -> Enable. Users can review proposed commands, run a smoke test, then choose whether the tool is only available in Developer or promoted to normal chat
- **Conversational builder** — `tools/custom_tool_builder_tool.py` exposes one compact agent-facing tool for clone/source setup, draft creation, command refinement, testing, creation, promotion, disable, and removal
- **Source setup hardening** — clone, folder, and current-workspace flows normalize install paths, reuse Developer storage, and keep Git/virtualenv setup errors visible instead of leaving half-created tools
- **Settings integration** — the Custom Tool Builder appears as a Utilities toggle. Disabling it removes the builder from normal chat while keeping the Developer UI available for manual management

### Command Generation & Validation

- **`developer/tool_capsules.py`** — retained internal module name for compatibility; user-facing copy says Custom Tool
- **LLM-assisted proposals** — a lightweight model pass inspects repo files and README content to propose useful, preferably read-only commands. Deterministic fallback remains available if AI analysis fails
- **Draft management** — the builder stores draft IDs so users can review and refine proposed commands across turns before creating the tool
- **Command classification** — commands are tagged by locality/risk and validated for dangerous shell patterns, unreviewed network behavior, write operations, missing placeholders, and malformed command templates
- **Environment validation** — generated commands are smoke-tested against the selected source tree and virtualenv/runtime assumptions before promotion, with failure details preserved for review
- **One-time tests** — local/read-only tests can run directly; network or riskier tests route through the normal approval mechanism
- **Promotion** — promoted Custom Tools register as synthetic plugin-style tools, inherit normal tool enablement, and can be disabled or removed without deleting the source repo

### Trust Boundaries

- **Source transparency** — cards show source URL, local install path, version, command count, and availability
- **No automatic broad enablement** — generated tools are opt-in and are not silently made available to normal chat
- **Repo code is not trusted by default** — proposed commands are reviewed and tested before promotion; users should only promote tools whose behavior they understand

---

## Row-Bot Status & Identity

Row-Bot now has a formal self-inspection and self-management surface: a tool for querying its own state, a controlled settings mutation API, and a Preferences UI for identity and self-improvement.

### Status Queries

- **`row_bot_status` tool** — read-only introspection across `overview`, `version`, `model`, `agents`, `channels`, `memory`, `skills`, `tools`, `mcp`, `providers`, `insights`, `evolution`, `api_keys`, `identity`, `tasks`, `vision`, `image_gen`, `video_gen`, `voice`, `config`, `logs`, `errors`, `updates`, and `designer`
- **Live runtime visibility** — the tool can report current model/provider, catalog/cache status, provider readiness, active goals, Agent Profiles, child-agent runs, delegation capacity and work-round progress, active channels, knowledge graph counts, enabled, pinned, and task-loaded skills, globally configured versus child-bound tool groups, configured APIs, task state, voice/image/video settings, and designer project counts
- **Diagnostics access** — recent warnings, provider/runtime probe failures, status-tray findings, errors, and tracebacks can be summarized without opening log files manually
- **Home health bar parity** — `ui/status_checks.py` and `ui/status_bar.py` expose compact health checks for Ollama, active model, cloud API, tunnel, OAuth accounts, workflows, goals, agents, knowledge, wiki vault, documents, search, skills, tracker, Buddy, MCP, plugins, Computer Use, network, tools, disk, threads DB, FAISS, Dream Cycle, TTS, and logging

### Controlled Self-Management

- **`row_bot_update_setting`** — approved mutations for Brain and Vision model switching, assistant name, personality, context caps, dream-cycle controls, skill toggles, skill pins, tool toggles, image-generation model, video-generation model, manual dream-cycle trigger, and self-improvement toggle
- **Agent and media diagnostics** — status output includes Agent Profile/child-run summaries and provider-aware media rows so xAI Grok OAuth, direct xAI, Google, and OpenAI media availability can be diagnosed without opening Settings
- **Interrupt-gated writes** — all state-changing operations route through explicit user confirmation before they are applied
- **Controlled proposal tools** — when self-improvement is enabled, `row_bot_create_skill`, `row_bot_patch_skill`, `row_bot_send_feedback`, `row_bot_apply_proposal`, `row_bot_reject_proposal`, `row_bot_verify_proposal`, and `row_bot_review_skill_library` operate through the controlled self-evolution store instead of directly mutating state
- **Skill patch safety** — bundled skills are patched via user-space overrides, not in-place mutation; old versions are backed up under `~/.row-bot/skill_versions/`

### Identity & Preferences

- **`identity.py`** — stores assistant name, personality text, and self-improvement flag; sanitizes personality input before save
- **Preferences tab** — Settings exposes name, personality, preview, and self-improvement controls in one place
- **Prompt integration** — the same identity settings are consumed by `self_knowledge.py` so the opening line seen by the model matches what the user configured
- **Parallel UI surface** — the Home health/status bar provides a visual health view for the user, while `row_bot_status` exposes the same class of state to the agent

---

## Self-Knowledge & Insights

Row-Bot now carries an explicit self-description into prompts and uses Dream Cycle to turn recent activity into structured insight objects.

### Prompt-Time Self-Knowledge

- **Feature manifest** — `FEATURE_MANIFEST` in `self_knowledge.py` is the canonical inventory of major capabilities used when Row-Bot explains what it can do
- **Dynamic identity line** — `build_identity_line()` combines the configured assistant name and personality into the opening identity sentence
- **Dynamic state block** — `build_self_knowledge_block()` appends live state like current model, configured providers, entity count, last dream summary, active channels, designer project count, and enabled skills
- **Prompt injection** — the self-knowledge block is added alongside tool, memory, and citation guidance so the model can talk about itself accurately without outdated copy in `prompts.py`

### Insight Generation & Triage

- **Dream snapshot analysis** — Dream Cycle phase 5 captures logs, provider/model/media configuration, usage signals, and active insights, then runs `DREAM_INSIGHTS_PROMPT`
- **Structured insight store** — `insights.py` persists categorized insights like `error_pattern`, `skill_proposal`, `tool_config`, `knowledge_quality`, `usage_pattern`, and `system_health`
- **Dedup and pruning** — similar titles and semantically overlapping insights are merged; stale insights are auto-pruned; last-analysis time is tracked
- **Pin / dismiss / apply actions** — the Workflow Console exposes user actions for curating the insight list without leaving the app
- **Skill proposals** — insight objects can carry draft skill metadata, which pairs naturally with the self-improvement toolchain when enabled

---

## Controlled Self-Evolution

Controlled self-evolution turns insights, repeated user workflows, and explicit self-improvement requests into reviewable proposals. It is intentionally not an autonomous code-rewrite loop: the system records intent, previews the effect, asks for approval before mutating anything, writes audit records, and keeps rollback metadata where mutation is allowed.

- **`evolution.py` engine** — owns proposal creation, validation, status transitions, action runs, rejection memory, curator reports, feedback reports, and persistence under `~/.row-bot/controlled_evolution.json`
- **Proposal lifecycle** — proposals move through `draft`, `ready`, `approved`, `applied`, `verified`, `rejected`, or `failed`; terminal proposals are retained for audit and duplicate-suppression instead of being discarded
- **Proposal types** — supported proposal types include `investigate`, `create_skill`, `patch_skill`, `consolidate_skills`, `send_feedback`, `settings_change`, and `memory_correction`; only a subset performs mutation in this release
- **Insight mapping** — Dream Cycle insights map to proposal types based on category, severity, and whether the issue is a repeated user-facing workflow or a system/provider maintenance concern
- **Investigation threads** — investigation proposals create draft threads with durable prompts so the user can inspect and continue diagnostic work before any state change
- **Skill mutation bounds** — skill creation and patch proposals validate names, block tool-guide mutation, reject likely secrets, cap diff size, back up prior versions, and apply only after approval
- **Feedback reports** — send-feedback proposals produce redacted local Markdown reports and support links without silently posting to a remote service
- **Curator dry-runs** — skill-library review can detect overlapping or stale skills and create proposals without mutating skill files
- **Status and UI integration** — `row_bot_status` exposes the `evolution` category, proposal action tools, recent action runs, rejection memory, and curator dry-runs; Command Center/insight surfaces can display proposal rows next to their originating insight
- **Rejection memory** — rejected proposals are remembered with fingerprints so similar future proposals can cite the prior rejection instead of recreating the same suggestion blindly

---

## Messaging Channels

Row-Bot uses a generic **Channel** abstraction. Native adapters and plugin-owned
channels declare capabilities and register through controlled runtime paths. The
system then auto-generates tools, settings UI, monitoring, and approval routing
around that channel.

### Channel Architecture

- **`Channel` ABC** — adapters implement lifecycle methods (`start`, `stop`, `is_configured`, `is_running`) plus outbound send methods for text, photos, documents, and approval requests
- **`ChannelCapabilities`** — declarative feature flags describe what each channel supports: photos, documents, voice, buttons, streaming, typing, reactions, and commands
- **Config schema** — each channel declares config fields so Settings can render the right form dynamically
- **Channel registry** — adapters self-register; runtime helpers expose all channels, running channels, configured channels, and delivery routing
- **Channel credential store** — `channels/auth_store.py` stores channel secrets through a channel-specific OS keyring path with legacy fallback so running channels survive migrations even if UI fields are intentionally blank
- **Shared media pipeline** — inbound audio transcription, image analysis, document extraction, inbox persistence, and workspace copy helpers are centralized in `channels/media.py`
- **Shared utilities** — auth, command handling, goal/profile-aware runtime context, approval routing, media capture, and corrupt-thread repair live in reusable channel modules
- **Plugin channel bridge** — plugin-owned channels use `plugins/channel_runtime.py` to route inbound text, attachments, approvals, Goal Mode continuation, generated files, typing/stream callbacks, and thread metadata through the same core channel runtime without importing Row-Bot internals
- **Tool factory** — running channels contribute auto-generated send/photo/document tools through `channels/tool_factory.py`
- **Activity tracking** — per-channel last-activity timestamps drive the sidebar monitor and status surfaces
- **Shared streaming engine** — `channels/streaming.py` consumes token, thinking, tool, interrupt, error, and done events; coalesces partial updates by time and character thresholds; sends typing keepalives; caps overflow previews; retries bounded rate limits; splits platform-safe finals; and falls back to a fresh final send when an edit cannot be finalized
- **Transport-local capabilities** — channel adapters implement only start/update/final/cleanup/split primitives while the shared engine owns cadence, finalization, fallback, cancellation, and delivery results
- **Checkpoint persistence** — successful channel delivery repairs a human-only checkpoint with the assistant answer but detects and preserves graph-written assistant messages to avoid duplicates
- **Durable thread notifications** — `channels/thread_notifications.py` stores once-only child-agent and Goal Mode terminal notices, retries failed delivery after channel startup, and suppresses notices for wait-mode results already consumed by the parent
- **Orchestration ownership** — required parent/child groups keep acknowledgement, approval, steering, final output, and retry state in the orchestrator; channel streaming owns transport presentation but cannot synthesize or send a second final independently

### Bundled Channels

- **Telegram** — full agent access, native draft streaming in supported private chats with edit fallback, UTF-16-aware message splitting, voice transcription, photo analysis, document extraction, emoji reactions, inline approval buttons, `/model` support, and HTML-safe formatting; startup retries one transient initialization failure, gives polling one bootstrap retry, cleans partial application state, treats invalid tokens as actionable non-retryable errors, and registers the command menu after polling so a menu failure cannot stop the bot
- **WhatsApp** — Baileys bridge with QR pairing, inbound/outbound media, rich YouTube previews, Markdown-to-WhatsApp formatting, edit streaming, typing updates, split finals, and approval resume
- **Discord** — DM-based edit streaming with typing keepalive, message splitting, fresh-send fallback, reactions, interactive approval buttons, slash-command integration, and media support
- **Slack** — Socket Mode adapter with native stream APIs when supported, edit fallback, bounded retry-after handling, DM threading, Block Kit approvals, reactions, and file uploads
- **SMS** — Twilio adapter with inbound webhook support, outbound SMS/MMS, tunnel-manager integration for public callbacks, message-safe final splitting, and YES/NO approvals; streaming remains off because SMS has no editable partial-message contract

### Delivery & Monitoring

- **Auto-generated channel tools** — when a channel is running, the agent gains send/photo/document tools for that channel automatically
- **Approval routing** — approvals can be sent through supported channels with inline action controls
- **Goal and profile context** — channel-triggered turns carry thread goal/profile context into the normal agent path so Goal Mode and Agent Profile behavior are consistent outside the desktop UI
- **Reasoning command parity** — Telegram, Discord, Slack, WhatsApp, and SMS expose the same exact-model `/reasoning` validation as local chat, persist the choice on the channel thread, and never advertise unsupported values
- **No duplicate finals** — normal turns, Goal Mode callbacks, approval resumes, and plugin-channel turns share a delivery result contract so a successfully streamed final is not sent again by legacy completion code
- **Detached parent delivery** — a parent that suspends for required children saves the originating channel transport and can deliver its later final through that binding; failed sends remain pending with the same idempotency key
- **Cancellation behavior** — cancelled streams clean previews and return a non-finalized delivery result instead of persisting or announcing a false success
- **Sidebar channel monitor** — the conversation sidebar shows live status dots, icons, display names, and relative last-activity timestamps
- **Auto-start and config persistence** — channel enablement and settings persist to `~/.row-bot/channels_config.json`

---

## Tunnel Manager

A provider-agnostic tunnel layer exposes local webhook ports to the internet when a channel needs inbound delivery.

- **Provider abstraction** — `TunnelProvider` defines the backend contract; `NgrokProvider` is the current implementation
- **`TunnelManager` singleton** — manages tunnel lifecycle, per-port allocation, cleanup, and status reporting
- **Automatic use by channels** — channels that need a public callback request a tunnel on start and release it on shutdown
- **Optional app tunneling** — the main Row-Bot UI can be exposed intentionally through a registered managed origin; public ngrok URLs terminate at the authenticated access middleware rather than bypassing owner sessions
- **Responsive auto-start** — startup reports a visible tunnel stage and moves
  the blocking main-app tunnel start onto a worker thread so slow provider setup
  cannot stall the NiceGUI event loop
- **Runtime-policy registration** — a tunnel provider must register its exact origin before exposure and unregister it on stop; invalid URLs, missing runtime policy, or registration failure close the new tunnel and fail without broadening access
- **Tailscale separation** — Tailscale Serve is managed by `access/tailscale.py` as an owner-reviewed private route, not as a generic tunnel provider; only an exact Row-Bot-owned Serve route can augment allowed host/origin/proxy policy
- **Settings UI** — tunnel provider, auth token, and active-tunnel status live in the System settings surface
- **Health checks** — tunnel status participates in the status monitor and diagnostics flows

---

## X (Twitter) Tool

Row-Bot integrates with X API v2 through a native httpx-based client, grouped into three high-level tool entry points.

- **3 grouped LangChain tools** — `x_read`, `x_post`, and `x_engage`
- **Read operations** — search, read tweet, timeline, mentions, and user info
- **Post operations** — post tweet, reply, quote, and delete tweet
- **Engagement operations** — like, unlike, repost, unrepost, bookmark, and unbookmark
- **OAuth 2.0 PKCE** — browser-based auth flow with a local callback server and refresh-token support
- **Rate-limit tracking** — per-endpoint rate information is recorded and surfaced in structured error responses
- **Tier discovery** — X tier information is persisted and reused for rate-limit expectations
- **Local token storage** — auth state lives in `~/.row-bot/x/`
- **Settings UI** — connect, disconnect, and inspect X auth from Accounts settings

---

## Tool Guides

Tool guides are lightweight `SKILL.md` packages that attach contextual instructions to tools without hard-coding those instructions into the main system prompt.

- **Skill-like format** — each guide is a directory with a `SKILL.md` file and YAML frontmatter, just like a manual skill
- **`tools:` activation field** — guides declare the tools they apply to; when any linked tool is in the active tool belt, the guide is injected automatically
- **Prompt injection** — `prompts.py` discovers active guides and appends them to the system prompt at runtime
- **Invisible to the manual skill toggles** — tool guides are auto-managed and do not clutter the user-facing skill list
- **22 bundled guides** — Agents, Browser, Calendar, Chart, Custom Tool Builder, Designer, Developer, Email, Filesystem, Goal, Math, MCP, Shell, Telegram, Row-Bot Status, Tracker, Updater, Video, Vision, Weather, Wiki, and X
- **Consistency benefits** — guide content can evolve independently of the main prompt, reducing drift and duplicated instructions

---

## Progressive Tool & Skill Discovery

Progressive discovery keeps large enabled extension libraries available without
binding every external schema or every enabled skill body to every interactive
model call. `capability_search.py` provides deterministic local ranking,
`tools/discovery.py` builds external-tool bridges, `skill_discovery.py` builds
skill bridges, and `agent.py` assembles one policy-filtered snapshot for the
current parent or child runtime.

- **Core/external boundary** — tools registered as Row-Bot core remain direct
  after normal enablement, profile allow-list, approval, background, and
  provider-schema filtering. MCP, plugin, promoted Custom Tool, and running
  channel tools enter the external catalog with their actual runtime targets
- **Recommended Auto mode** — `tools/registry.py` persists Auto versus eager
  compatibility. Auto binds core tools plus `tool_search`/`tool_invoke`; eager
  binds every authorized external wrapper directly. Background workflow graphs
  retain eager binding for compatibility, while interactive parent and child
  agents honor the selected mode
- **Forced resume continuity** — when an approval or checkpoint resume already
  depends on progressive invocation, graph reconstruction forces the discovery
  surface so the target does not disappear because settings changed mid-turn
- **Frozen authorized snapshot** — discovery bridges close over immutable
  records created after provider compatibility, profile allow-list, approval,
  channel health, plugin/MCP enablement, and child boundary checks. Search cannot
  discover a disabled or unauthorized target
- **Deterministic local ranker** — exact alias/name, prefix, and substring tiers
  precede BM25-style metadata scoring across canonical id, display name, source,
  description, tags, parameter names, and aliases. Results are capped from one
  through five and ties resolve by canonical id
- **Bounded model manifest** — at most 2% of effective context and 8,000
  characters describe available records. If descriptions do not fit, the
  manifest degrades to names/sources, then source counts, then no inline
  manifest; complete search remains available through the bridge
- **Metadata hardening** — control characters and excess whitespace are removed,
  descriptions are capped, and instruction-override patterns replace the
  description with `[description omitted]` before model exposure
- **Name-collision policy** — bridge names, core names, empty names, and every
  ambiguous duplicate external runtime name are omitted rather than selecting
  an arbitrary target
- **Exact tool search result** — `tool_search` returns the real runtime name,
  source, bounded description, and complete effective JSON input schema from
  the immutable snapshot; it grants no execution authority
- **Validated invocation** — `tool_invoke` accepts only an exact returned name,
  revalidates arguments through the target Pydantic model or supported JSON
  Schema subset, then invokes the original wrapped tool under its existing
  interrupt, execution-budget, cancellation, workspace, and provider policy
- **Error containment** — unknown or invalid requests return deterministic
  structured errors; approval interrupts and budget/no-progress exceptions
  propagate through the bridge, while ordinary target exceptions are logged
  without leaking implementation details into the model
- **Real trace identity** — runtime context marks a bridged invocation so live
  and reopened transcript grouping resolves the actual plugin, MCP server,
  Custom Tool, or channel label rather than displaying a generic bridge call
- **Unified skill records** — enabled manual skills and skills contributed by
  enabled plugins remain separate stores but normalize into one snapshot with
  source, root, tags, activation metadata, instructions, and canonical id.
  Plugin bare aliases are accepted only when unique and not shadowed by a
  manual skill id
- **Skill search/load split** — `skill_search` returns bounded metadata rather
  than instructions. `skill_load` activates one exact canonical id or vetted
  alias and optionally reads one strict UTF-8 regular file whose resolved path
  remains inside the skill root
- **Per-task automatic state** — `skills_activation.json` stores automatically
  loaded ids independently for each thread/child task. Reopen restores them,
  duplicate load is a no-op, and an ordered cap retains at most five automatic
  selections while pinned/manual state remains separate
- **Next-pass injection** — a successful skill load records a structured result;
  the complete skill instructions enter the next model pass through the normal
  skill prompt section, and the transcript receives one compact **Using
  _skill_** presentation receipt plus an active chip
- **Parent/child isolation** — child agents build their own tool and skill
  snapshots from their profile, allow-list, approval mode, workspace, budget,
  and task id. A child's automatic skills do not leak to its parent or sibling,
  and instructions cannot grant tools, workspace access, or delegation
- **Settings surface** — **Settings → Tools → Capability loading** exposes Auto
  and eager modes; the former Search tab content now lives under Tools, while
  Skills settings continue to own installation, enablement, and pinning
- **Privacy boundary** — capability ranking is lexical and local. It does not
  contact the selected chat model, an embedding provider, MCP marketplace, or
  Skills Hub source during request-time search

---

## Skills Hub & Skill Activation

Skills Hub is the discovery, import, search, and installation layer for manual skills. It sits above the lower-level `skills.py` loader and `skills_activation.py` runtime selection path: Skills Hub gets skills into the local library, while Smart Skills decides which enabled skills should shape a given turn.

- **Manual skill library** — user-installed skills live under `~/.row-bot/skills/<name>/SKILL.md` and use the same YAML-frontmatter package shape as bundled skills
- **Pinned skill defaults** — `skills.py` tracks pinned manual skills separately from one-off composer choices so default skills can stay available across sessions without forcing every enabled skill into every prompt
- **Smart activation** — `skills_activation.py` resolves enabled skills, explicit `/skill` requests, draft suggestions, per-thread/workflow overrides, and persistent per-task automatic selections before prompt assembly
- **Slash commands** — `slash_commands.py` provides skill-aware chat commands such as using, disabling, or narrowing skills without leaving the conversation
- **Shared composer controls** — `ui/chat_composer_extras.py` gives main chat, Designer Studio, and Developer Studio a common slash palette, skill picker, skill chips, and draft-suggestion path
- **Source adapters** — `skills_hub/` can inspect GitHub repositories, pasted Markdown, direct URLs, well-known skill indexes, and marketplace-style catalogs before installation
- **Import detection** — pasted or linked content is classified before install so a raw `SKILL.md`, a folder-like package, or a catalog entry can route through the right importer
- **Installation search index** — local and remote Skills Hub catalog rows are normalized into searchable records with source, tags, description, install state, and provenance metadata; this install-time browsing path is separate from request-time local progressive discovery
- **Provenance and safety** — installed skills retain origin/source metadata, user overrides take precedence over bundled skills, and user-controlled enablement determines whether manual skill instructions enter the system prompt
- **Testing coverage** — `tests/test_skill_discovery.py`, `tests/test_capability_search.py`, `tests/test_skills_activation.py`, `tests/test_skill_pinning.py`, `tests/test_slash_commands.py`, Skills Hub suites, and UI/transcript/source tests cover ranking, aliases, references, automatic persistence, parent/child isolation, pinning, import detection, source adapters, search, and composer contracts

---

## Image Generation

Row-Bot can generate and edit images through multiple external providers, render them inline, persist them to disk, and reuse them in designer workflows or channel delivery.

- **Provider support** — OpenAI image models, xAI Grok Imagine through direct API keys or xAI Grok OAuth, Google Imagen 4, and Gemini image-capable models
- **Generate and edit flows** — prompts can generate a new image or edit the most recent image, an attached image, or an on-disk file
- **Inline rendering** — generated images are surfaced directly in the chat stream without requiring a separate viewer
- **Per-thread persistence** — generated images are saved into Row-Bot's media storage so they survive refreshes and can be referenced later
- **Channel delivery** — running messaging channels can pick up generated images and send them as photos
- **Designer reuse** — Designer Studio can invoke the same provider layer for slide assets and visual content generation
- **Settings selector** — the active image-generation model is configurable from Settings and queryable through `row_bot_status`
- **Provider boundary** — Atlas Cloud media-generation catalog rows are intentionally not exposed as image-generation choices in this phase; xAI Grok Imagine rows are exposed only when the direct xAI API key or xAI Grok OAuth runtime is available

---

## Video Generation

Row-Bot can generate short video clips from text prompts or reference images through Google Veo and xAI Grok Imagine Video for chat use and Designer storyboard workflows.

- **`video_gen_tool`** — top-level agent tool for text-to-video and image-to-video generation
- **Provider support** — Google Veo handles text-to-video and image-to-video with provider-side person-generation policy handling; xAI Grok Imagine Video supports text-to-video and image-to-video through direct xAI API keys or xAI Grok OAuth with provider-specific aspect ratio, duration, and resolution constraints
- **Inline rendering** — generated clips are surfaced directly in the chat stream with safe media-element hydration
- **Designer integration** — Designer Studio storyboards and landing hero slots can reference generated videos as `asset://` media; motion clips are rendered in preview, presenter mode, and published share links
- **Persistent asset storage** — generated clips are saved to Row-Bot's media storage so they survive thread refreshes and can be reused across designer projects
- **Channel delivery** — running messaging channels can pick up generated videos and deliver them where supported
- **Provider boundary** — Atlas Cloud video-generation catalog rows are intentionally not exposed as video-generation choices in this phase; Grok Imagine Video rows are exposed only on Video surfaces and do not leak into chat, agent, or Vision model pickers

---

## MCP Client & External Tools

Row-Bot includes a guarded Model Context Protocol client that can connect external MCP servers and expose their tools to the ReAct agent without making external servers part of Row-Bot's trusted core.

### Runtime Model

- **Dedicated package** — `mcp_client/` owns persistent config, marketplace search, dependency checks, safety classification, runtime sessions, logging, result normalization, and curated starter metadata
- **Separate config file** — MCP state is stored in `~/.row-bot/mcp_servers.json`, separate from native tool toggles, so malformed or broken MCP config falls back to an empty disabled config instead of damaging normal tool settings
- **Global enable switch** — `enabled` in MCP config is the top-level kill switch. Turning it off stops active sessions, clears the discovered catalog, removes dynamic MCP tools from the agent, and keeps saved server definitions for later
- **Per-server runtime** — each enabled server gets its own `McpServerRuntime` session tracked by status (`connecting`, `connected`, `failed`, `dependency_missing`, `stopped`, `global_disabled`), tool counts, timestamps, transport, and last error
- **Transport support** — stdio, Streamable HTTP, and SSE are supported through the Python MCP SDK. Each server can set command, args, cwd, env, URL, headers, connect timeout, tool timeout, and output limit
- **Non-blocking startup** — `app.py` discovers enabled servers during startup in a guarded path. Exceptions are logged as warnings and do not stop Row-Bot from launching
- **Failed-until-refresh state** — a failed enabled server is not restarted on every discovery pass; it remains failed until the user explicitly refreshes it or changes configuration
- **Generation-aware waits** — probe and tool-call futures poll the active cancellation scope so Stop cancels the pending future and normalizes the result instead of leaving a generation blocked
- **Shutdown cleanup** — app shutdown calls MCP runtime shutdown to close child sessions and stop external stdio processes

### Dynamic Tool Catalog & Injection

- **Parent registry tool** — `tools/mcp_tool.py` registers `mcp` / **External MCP Tools** as the native parent tool. It is the stable toggle users see in Settings and Row-Bot Status
- **Dynamic wrappers** — discovered enabled MCP tools are converted into LangChain `StructuredTool` instances at agent build time, with names generated as `mcp_<server>_<tool>` and retained as the exact runtime targets behind progressive discovery
- **Auto versus eager binding** — recommended Auto mode places enabled MCP wrappers in the authorized external catalog and binds the generic search/invoke bridges; eager compatibility mode binds each wrapper directly after the same provider, profile, and approval filtering
- **Immediate cache invalidation** — MCP add/edit/delete, global/server enablement, tool enablement, and approval-policy changes clear the loaded agent cache so the next graph sees the new external tool surface
- **Schema conversion** — JSON input schemas are converted into Pydantic argument models where possible, with a permissive fallback for complex or invalid schemas
- **Resources and prompts** — servers can optionally expose `list_resources`, `read_resource`, `list_prompts`, and `get_prompt` utility tools through per-server advanced toggles
- **Readable display names** — `agent.py` resolves tool-call UI labels back to the original MCP tool and server name, for example `MCP: microsoft_docs_search (microsoft-learn-mcp)`
- **Discovered-label preservation** — invocation through `tool_invoke` still records the real MCP wrapper identity, so live and restored traces do not collapse under the generic bridge label
- **Result normalization** — MCP text, structured content, resource links, embedded resources, binary/image blocks, empty results, errors, and oversized outputs are normalized before being sent back into the model

### Safety & Trust Boundaries

- **External output is untrusted** — the MCP tool guide tells the agent not to follow instructions found inside MCP results unless they are clearly part of the user's request
- **Catalog metadata is untrusted** — progressive discovery sanitizes MCP names/descriptions, omits instruction-like descriptions, bounds the manifest, rejects ambiguous external names, and does not treat a search result as authorization
- **Native tools stay preferred** — Row-Bot Memory, Browser, Computer Use, filesystem, document, search, channel, and Designer capabilities remain canonical for Row-Bot-owned behavior; overlapping MCP servers are treated as external alternatives
- **Destructive classification** — tool names, descriptions, and MCP annotations are inspected for write/send/delete/run/deploy/payment-style behavior. Destructive tools require approval and are not enabled by default after discovery
- **Approval synchronization** — destructive MCP wrapper names are included in the parent tool's `destructive_tool_names`, so they flow through the existing interrupt approval mechanism
- **Background workflow rules** — MCP destructive tools follow workflow safety mode: approval-required modes interrupt, while explicit allow-all mode can run enabled destructive MCP tools
- **Capability overlap detection** — `mcp_client/conflicts.py` labels MCP servers that overlap native memory, browser, documents, web search, URL reading, channels, or Designer capabilities and forces manual tool selection for overlap/high-risk imports
- **Secret masking** — diagnostics use masked config output so headers, tokens, and environment values are not displayed raw

### Settings UI & Marketplace

- **Settings → MCP** — `ui/mcp_settings.py` provides the user-facing MCP control surface: global enable switch, add server, import config, browse MCP servers, diagnostics, test, refresh, edit, delete, and per-tool controls
- **Disabled-until-tested imports** — manual JSON imports and marketplace entries are saved disabled. Users test the server before enabling it
- **Tool review rows** — after a successful probe, each tool shows name, description, input schema summary, enabled state, destructive badge, approval state, and whether it comes only from saved config or live catalog
- **Marketplace adapters** — `mcp_client/marketplace.py` can search curated starters plus official-style directories, PulseMCP, Smithery, and Glama, with cache and curated fallback when live results fail or ignore the query
- **Starter metadata** — curated entries preserve trust tier, risk level, auth requirement, native overlap, requirements, notes, and install recipe metadata
- **Xquik starter** — the curated Xquik streamable-HTTP entry documents its `x-api-key` header, X/Twitter search/extraction/monitoring capabilities, and high-risk generic executor; private reads, writes, persistent monitors/webhooks, and metered actions remain approval-gated
- **Marketplace edit preservation** — editing an installed catalog server retains its source/catalog metadata while updating runtime fields, so origin, risk, and install context do not disappear after local configuration changes
- **Diagnostics dialog** — Settings can display masked MCP config and live status summary for support/debugging without requiring file edits

### Runtime Requirements

- **Requirement inference** — stdio launch commands infer runtime requirements for `npx`/Node.js, `uvx`/uv, Docker, and Playwright MCP browser dependencies
- **Managed user-space installs** — native/source Row-Bot can install private Node.js LTS, uv, and Playwright Chromium runtimes under `~/.row-bot/runtimes/` and inject those paths only into MCP child process environments
- **Manual complex dependencies** — Docker and other heavyweight host dependencies are surfaced as manual setup requirements in native/source installations rather than installed implicitly
- **Packaging distinction** — native desktop packages resolve external MCP runtimes on demand, while the official complete server image intentionally includes pinned Node, uv/uvx, and the matching Playwright Chromium so container replacement is reproducible without mutating the image after startup
- **Private Cua boundary** — Computer Use reuses managed-runtime and stdio primitives but its pinned driver, tool allowlist, disclosure, and exclusive service are Row-Bot-owned private integration state, not an installed external MCP server

### Testing & Release Checks

- **Offline regression suite** — `tests/test_mcp_client.py` covers config fallback, secret masking, safety classification, marketplace fallback/filtering, conflict policy, runtime requirement handling, managed environment injection, settings rows, stdio discovery/call, global disable, bad server failure, display names, background safety, and browser-loop handling
- **Opt-in live E2E** — `scripts/mcp_real_world_e2e.py` and `tests/test_mcp_real_world_e2e.py` connect to public MCP servers outside normal CI to validate import, probe, manual tool enablement, dynamic wrapper invocation, and read-only approval classification
- **Maintainer workflow** — MCP-heavy releases run the offline suite first, then the live public E2E check from the repo root

---

## Migration Wizard

Row-Bot includes a one-time migration wizard for moving selected data from Hermes Agent or OpenClaw into a Row-Bot data directory without treating legacy state as trusted runtime configuration.

### Flow & UI

- **Preferences launcher** — `ui/settings.py` exposes **Open Migration Wizard** at the bottom of Settings → Preferences. The wizard opens in a maximized dialog so it stays available without occupying a permanent settings tab
- **Three-step flow** — `ui/migration_wizard.py` guides users through source/target selection, read-only scan/review, and explicit apply
- **Provider support** — users choose Hermes Agent or OpenClaw. Defaults point at `~/.hermes` or `~/.openclaw`, but any source and target folder can be selected for disposable test runs
- **Preview controls** — categories and rows show status, selection state, conflict notes, manual-review notes, archive-only behavior, and report paths after apply

### Detection & Planning

- **Pure migration package** — `migration/` owns the feature: `core.py` models plans/items/summaries, `redaction.py` masks secrets, `detection.py` scans sources read-only, `planner.py` builds dry-run plans, `apply.py` writes backups/reports, and `fixtures.py` creates realistic test homes
- **Read-only scan** — detection and planning do not write to either source or target. Existing target files are only inspected to mark conflicts
- **Provider mismatch guard** — Hermes scans reject OpenClaw-looking folders, and OpenClaw scans reject Hermes-looking folders, returning an empty actionable plan instead of partial generic matches
- **Mapped data** — planners can map model/provider config, identity/persona files, long-term memories, OpenClaw daily memory, skills, MCP server definitions, and explicit API key/token entries
- **Risk boundaries** — channel config, approvals, browser/cron/hooks/tools settings, legacy runtime state, logs, sessions, OAuth/auth stores, plugin state, and broad command allowlists are skipped for manual review or copied only into the migration report archive

### Apply, Backups & Reports

- **Explicit apply** — only selected planned/sensitive items are applied. Archive-only items are copied to the report archive, not activated in the live Row-Bot target
- **Backups first** — existing target files are backed up before overwrite/append/update. Multiple writes to the same target preserve the pre-migration original once per run, and newly created files are not backed up later in the same run
- **Redacted report** — each run writes redacted `plan.json`, `result.json`, `backup_manifest.json`, and `summary.md` under `migration-reports/<timestamp>/`
- **Archive redaction** — JSON and key/value archive snapshots are redacted before being copied into reports; binary or unsupported files are represented by a placeholder instead of raw content
- **MCP import safety** — migrated MCP servers are written disabled. They must be reviewed and enabled from Settings → MCP before any external tools become available to the agent
- **Credential import** — API keys and tokens are off by default and require explicit selection. Reports hide their values; selected keys route through target-profile secure storage via `api_keys.set_key_for_data_dir`, so normal imports use the OS credential store with metadata-only local files when keyring is available

### Testing

- **Focused suites** — `tests/test_migration_core.py`, `tests/test_migration_detection.py`, `tests/test_migration_planner.py`, `tests/test_migration_apply.py`, and `tests/test_migration_wizard_ui.py` cover model invariants, source detection, dry-run planning, wrong-provider rejection, conflict behavior, backups, reports, redaction, daily memory import, and UI helper logic
- **Realistic fixtures** — `migration/fixtures.py` builds multi-month Hermes and OpenClaw homes with fake secrets, memories, skills, channels, MCP servers, approvals, cron/hooks, plugins, sessions, logs, and archive-only state
- **Manual E2E path** — disposable targets under `.tmp/migration-fixtures/` are used for click-through validation during migration testing; the fixture root is ignored by git

---

## Legacy Thoth Upgrade Policy

Current Row-Bot releases no longer run the old automatic Thoth-to-Row-Bot rebrand migration during startup. That migration shipped for multiple Row-Bot releases after the v4 rename and has been removed from the hot startup path.

- **Supported path** - users who are still on Thoth or an early Row-Bot build must first install and launch a previous migration-capable Row-Bot release, then upgrade to the current release.
- **Startup posture** - current startup reads Row-Bot data from `ROW_BOT_DATA_DIR` or the default `.row-bot` directory and does not scan, copy, repair, or rewrite `.thoth` data.
- **Legacy plugin posture** - plugin loading can still quarantine old Thoth plugin manifests or code so stale plugins do not break startup.
- **Current migration wizard** - the Preferences migration wizard remains focused on Hermes/OpenClaw archives and is separate from the removed rebrand bridge.

---

## Plugin System & Marketplace

A sandboxed, hot-reloadable extension system lets plugins add native tools,
plugin-packaged MCP-backed tools, bundled skills, and channels without
modifying the core codebase.

### Plugin Architecture

- **Plugin API** — `PluginAPI`, `PluginTool`, and public channel base exports are the abstractions available to plugins through `plugins.api`
- **Manifest system** — each plugin declares v2 metadata, supported surfaces (`native_tools`, `mcp_servers`, `channels`, and `skills`), settings, secrets, auth, permissions, and health checks in `plugin.json`
- **Security sandbox** — AST scans block dangerous constructs like `eval`, `exec`, `subprocess`, shell escape paths, UI frameworks, and imports from Row-Bot internals
- **Dependency safety** — v2 plugins do not install Python dependencies into Row-Bot's runtime environment; plugin-packaged MCP servers provide an external process boundary when needed
- **State persistence** — enablement and non-secret config are stored under `~/.row-bot/plugin_state.json`; plugin API-key secrets use the shared secret store (OS keyring on desktop, encrypted persistent server records when explicitly configured) with metadata-only `plugin_secrets.json` state and session-only fallback when no secure backend is available
- **Hot reload** — Settings can reload plugins without restarting the app; agent caches are cleared automatically
- **Skill auto-discovery** — plugin `skills/` directories are scanned for `SKILL.md` definitions only while the owning plugin is enabled; permitted records join the unified skill snapshot and can be pinned/selected or progressively loaded for the current task without granting the plugin's tools

### Marketplace

- **Marketplace index** — v2 plugin catalog fetched from GitHub-hosted JSON or a local fixture index, with caching, stale-cache fallback, checksums, source metadata, and update checks
- **Browse dialog** — search, inspect, review permissions, and install plugins from within the app
- **Install / update / uninstall** — plugins are validated before install, installed and kept off by default, checksum-verified when catalog data provides a `sha256:` value, and reloaded immediately afterward
- **Native Plugin Center** — one Row-Bot-owned UI renders per-plugin metadata, permissions, settings, secrets, auth, health checks, tools, channels, skills, logs, updates, and enable/disable controls; plugin-owned channels do not render arbitrary custom UI
- **Custom Tool bridge** — promoted Custom Tools are registered through the plugin/tool surface as synthetic local tools so normal chat can use them without adding a separate extension mechanism
- **Public channel API** — channel plugins receive public inbound/outbound dataclasses, attachment helpers, approval resume helpers, pairing/allowlist helpers, and generated webhook URLs through `plugins.api`
- **Plugin webhooks** — `plugins/webhooks.py` registers namespaced webhook routes under `/plugin-webhooks/{plugin_id}/{name}` and disables them when the owning plugin is disabled, unloaded, uninstalled, or fails load
- **Bot Framework auth** — `plugins/bot_framework_auth.py` validates Bot Framework JWTs with OpenID/JWKS discovery, issuer/audience checks, and display-safe error reporting for channel plugins

---

## Auto-Updates

`updater.py` polls the GitHub Releases API for the official Row-Bot repo on a background thread (30-second startup delay, then every 6 hours, with a 24-hour debounce on actual network calls). Checking is on by default; if there is no internet the call fails silently and the next tick retries.

- **Channel** — `stable` (default) hits `/releases/latest`; `beta` walks the top 10 releases and includes pre-releases. Persisted in `~/.row-bot/update_config.json`.
- **Manifest verification** — every release body must contain a fenced `<!-- row-bot-update-manifest -->` block with SHA256 hashes for each platform asset. Without a manifest entry, `download_update` refuses to install. The CI workflow `.github/workflows/update-manifest.yml` calls `scripts/append_sha_manifest.py` to PATCH the release body once artifacts are uploaded. The Linux one-line installer uses the same manifest before running the bundled tarball installer.
- **OS code signature** — Windows installs invoke `signtool.exe verify /pa`; macOS installs invoke `codesign --verify --deep --strict`. Linux tarball installs do not have a universal OS-level signing verifier, so Row-Bot relies on GitHub HTTPS plus the required SHA256 release manifest.
- **Hand-off** — Windows: a detached `update_handoff.py` helper asks the running app to quit, waits for known Row-Bot PIDs and the local port to clear, then starts `Row-Bot-x.y.z-Windows-x64.exe /SILENT /CLOSEAPPLICATIONS /RESTARTAPPLICATIONS`. The `.iss` uses `CloseApplications=yes` / `RestartApplications=yes` so Inno Setup can swap files safely, and repair/upgrade deletes `{app}\python` before recopying the bundled runtime so stray packages installed into embedded Python cannot survive. macOS: `open <dmg>` and exit; the user drags the new app to `/Applications`. Linux: install the verified `Row-Bot-X.Y.Z-Linux-ARCH.tar.gz` into the user XDG release tree, atomically flip `~/.local/share/row-bot/current`, refresh the desktop entry/icon, and restart through `~/.local/bin/row-bot`.
- **UI** — a green "⬆ vX.Y.Z" status-bar pill appears when a newer release is detected; clicking opens the What's-New dialog (rendered release notes plus Install / Skip / Later buttons). Settings → Preferences → Updates exposes channel selection, "Check for updates", and a list of skipped versions.
- **Agent surface** — `tools/updater_tool.py` registers `row_bot_check_for_updates` (read-only) and `row_bot_install_update` (interrupt-gated). The dynamic self-knowledge block surfaces "Update available: …" when applicable, and `row_bot_status` adds an `updates` category.
- **Dev installs** — when a `.git/` directory sits next to the app source (i.e. running from a checkout), the scheduler is disabled and `row_bot_install_update` refuses, so working copies are never overwritten. On Linux, packaged installs are recognized by `install_info.json` with `platform: linux` and `install_kind: xdg-user-tarball`.
- **Container updates** — official Docker deployments do not self-replace through the app updater. Operators explicitly pull a release-pinned image, recreate the Compose service, verify health, and retain the prior tag/digest plus both persistent volumes for rollback.

---

## Habit & Health Tracker

- **Conversational tracking** — log medications, symptoms, exercise, mood, sleep, periods, and other recurring data in natural language
- **Auto-detect & confirm** — the agent recognizes likely trackable events and asks before writing anything
- **3 tracker operations** — structured logging, flexible querying, and destructive delete with confirmation
- **Built-in analysis** — adherence, streaks, numeric summaries, frequency, day-of-week patterns, cycle estimation, and co-occurrence analysis
- **Trend visualization** — tracker queries can export CSV and chain directly into the chart tool for Plotly output
- **Fully local** — tracker data lives in `~/.row-bot/tracker/tracker.db`
- **Memory isolation** — tracker entries are intentionally excluded from the personal knowledge graph

---

## Desktop App

- **Native window** — runs in a desktop window via pywebview on Windows and macOS; Linux defaults to browser mode and can opt into `--native` when the desktop has the required GTK/Qt backend
- **System tray** — `launcher.py` exposes open and quit controls plus running-state feedback on Windows and macOS; Linux defaults to no tray and can opt into `--tray` when AppIndicator/desktop support is available
- **Native macOS tray host** — packaged macOS builds include `installer/macos/RowBotTrayHost.m`, a small native status-item host that keeps tray content visible and avoids fragile cross-platform tray fallbacks
- **Splash screen** — Tk-based loading splash during startup; Tk failures are logged to launcher diagnostics, and the visible console fallback is opt-in for debugging instead of appearing during normal Windows launches
- **Browser splash readiness** — the temporary NiceGUI startup surface polls
  the `/readyz` HTTP status contract and reloads on success without depending on
  a JSON body that the public readiness route does not promise
- **Startup diagnostics** — `startup_diagnostics.py` runs early in `app.py` and probes fragile optional native packages. Missing optional packages are ignored; installed-but-broken packages such as TorchCodec are logged with recovery steps and patched out of optional Transformers availability checks where safe.
- **First-launch setup wizard** — starts with model/provider choice, then migration and setup-center steps for Local, Providers, Custom/Self-hosted, memory/docs, workflows, Agent/Profile surfaces, Designer, Developer, channels, voice, and related setup without touching config files by hand
- **Responsive desktop composer** — container-query breakpoints keep the ordered Model, Thinking, and Approval units, Skills count, context meter, voice state, and stable Send/Stop slot usable below 760 px and 520 px; compact icons retain accessible labels, tooltips, and current-state text
- **Self-contained installers** — Windows and macOS releases bundle dependencies for one-click setup; Linux uses a one-line bootstrapper that verifies and installs the self-contained XDG tarball into user-owned paths
- **Packaged runtime validation** — Windows packaging validates embedded Python, bundled Tk, required native DLLs, and startup smoke paths so splash/picker failures are caught before artifact publication
- **Launcher identity and ports** — the launcher probes `/api/launcher-ping` before reusing port 8080, passes the chosen port through `ROW_BOT_PORT`, and supports explicit `--browser`, `--native`, `--tray`, `--no-tray`, `--server`, `--no-open`, `--port`, and `--host` modes
- **First-run window picker** — launcher-managed native/browser mode selection prefers the Tk picker and fails quickly to a safe default if the helper cannot render, avoiding hidden or blank console prompts on packaged Windows
- **Launcher recovery hints** — when the managed server exits during startup, `launcher.py` tails `~/.row-bot/row_bot_app.log` and emits targeted recovery hints for recognized startup signatures, including broken optional TorchCodec DLL loads in the embedded Windows runtime.
- **Launcher data recovery commands** — `launcher.py --reset-tasks-db`, `--reset-db`, and `--restore-data` back up SQLite DB families before recreating or restoring known task, memory, and thread databases
- **Authenticated server entry point** — `row-bot serve` starts one worker without tray, splash, browser open, or automatic Ollama startup, defaults to loopback, and resolves explicit public-origin/host/proxy policy before the child app starts
- **Docs capture mode** — app-side docs capture hooks expose stable, seeded UI states to local documentation automation without affecting normal runtime startup
- **Computer Use setup** — the System tab keeps browser and native-app automation separate, requires the Cua telemetry acknowledgement before executable use, installs the pinned runtime only on demand, and guides macOS Accessibility/Screen Recording recovery
- **Auto-restart flow** — closing the native window does not kill the tray-managed app process; reopen is fast
- **Release pipeline** — CI builds and verifies platform artifacts and release metadata; Windows signing remains local-only, macOS signing/notarization remains manual, and clean-machine installer validation is a documented release gate rather than an automated readiness claim

---

## Single-Owner Remote Access & Server Mode

The `access/` subsystem replaces presentation-specific mobile authorization with
one owner model shared by full desktop and compact browser layouts. It is not a
multi-user or hostile-tenant boundary: every authenticated browser has the same
product authority, including Settings, while layout selection changes only the
initial presentation.

- **Explicit deployment mode** — `access/config.py` separates `desktop` from
  `server` behavior and keeps reachability independent from authentication;
  desktop direct loopback is the implicit local owner, but server-mode and
  forwarded loopback requests require a session
- **Request context** — `access/request_context.py` canonicalizes host, origin,
  effective client, forwarding trust, route class, session identity, and
  presentation into one immutable context used by HTTP, WebSocket, NiceGUI, and
  server-side UI authorization
- **Versioned access database** — `AccessStore` uses the existing physical
  `mobile.db` for instance identity, invitations, devices, sessions, and bounded
  audit events; schema upgrades are transactional and preserve compatible
  legacy owner records
- **Secret model** — versioned invitation/session tokens contain at least 256
  bits of randomness, only salted hashes reach SQLite, raw invitations are
  printed or shown once, and audit/error detail is redacted and size-bounded
- **Immutable invitation grant** — an invitation fixes canonical origin,
  desktop/compact presentation, trusted/temporary session lifetime, creator,
  and expiry; the claim request cannot broaden those values
- **One-claim concurrency** — inspection is read-only, explicit POST consumes an
  invitation, and concurrent claims serialize so exactly one device/session is
  created; expired, cancelled, locked, or used links render terminal recovery
  without another claim form
- **Session cookies** — HTTPS uses an instance-isolated Secure host-prefixed
  cookie, HTTP uses a separate instance-scoped HttpOnly cookie, SameSite and
  server expiry bound both, and logout clears current plus legacy cookie names
- **Renewal and revocation** — due trusted sessions can extend server and cookie
  expiry through an exact-origin refresh; temporary/early refreshes do not,
  device revocation cascades to all sessions, and active WebSockets observe
  revocation within a bounded interval
- **Unified middleware gate** — `access/middleware.py` authenticates HTTP and
  WebSocket scopes from one runtime-policy snapshot, distinguishes navigation
  redirects from API 401s, validates exact origins and hosts, and rejects
  untrusted forwarded identity
- **Route policy** — public health/connect assets remain minimal, authenticated
  owner routes share one policy, access mutations require same origin, webhooks
  retain route-owned secrets, and launcher operations remain direct-loopback
  only behind a separate ephemeral control secret
- **Neutral connect flow** — unauthenticated pages disclose no instance name,
  route inventory, device list, or configured providers; successful claims
  remove invitation material from visible browser history before redirect
- **Durable route config** — `access/access_routes.py` atomically stores
  local-only versus LAN listen mode plus exact UI-managed trusted origins in
  `access_routes.json`, builds a canonical/deduplicated route inventory, and
  keeps reachability labels free of credentials, queries, tokens, and paths
- **Assigned-interface inventory** — interface discovery reads usable assigned
  IPv4/IPv6 unicast addresses without DNS or outbound probes, excludes loopback,
  link-local, unspecified, and multicast addresses, and retains non-private
  addresses with a stronger externally-routable/plaintext/firewall warning
- **Exact trusted-origin mutation** — an authenticated owner can add one exact
  HTTP(S) scheme/host/optional-port origin. Credentials, paths, queries,
  fragments, wildcards, malformed ports, and other schemes fail validation
  before storage or invitation creation
- **Live runtime policy update** — `RuntimeAccessPolicy` combines immutable
  deployment config with the current configured-origin snapshot. Saving or
  removing an origin updates allowed hosts and public origins immediately for
  new HTTP and WebSocket requests without mutating proxy trust or restarting
- **Managed-policy precedence** — environment origins are displayed as
  externally managed, and `ROW_BOT_ALLOWED_HOSTS` makes UI mutation read-only;
  durable origins never override explicit deployment-owned Host admission
- **Network provisioning boundary** — trusting an origin does not resolve DNS,
  test reachability, provision TLS, configure a reverse proxy/firewall, or
  change the listen address. HTTP remains unencrypted even when authenticated
- **Remote Access settings** — `ui/remote_access_settings.py` exposes current
  browser/logout, invitation creation, trusted-address add/list/confirmed-remove,
  LAN restart flow, reviewed route status and warnings, devices, sessions,
  per-session revoke, device revoke, and explicit Tailscale actions under an
  owner authorization guard
- **Access CLI** — `row-bot access invite|list|revoke|revoke-all|doctor` works
  without importing NiceGUI; doctor checks configuration/database/proxy/Tailscale
  hazards read-only and redacts nested credentials
- **Owned Tailscale Serve** — `access/tailscale.py` detects only after explicit
  user action, produces a reviewable plan, parses structured status, refuses
  Funnel/conflicting routes, applies one exact loopback target, persists an
  ownership fingerprint, updates runtime policy after restart, and removes only
  the exact owned route
- **Launcher recovery** — `access/launcher_control.py` carries one replay-safe
  ephemeral restart channel between launcher and child app so LAN/Tailscale
  changes can request restart without granting remote sessions process control
- **Compatibility migration** — legacy paired-mobile storage migrates in place
  to the single-owner schema, preserving safe owner devices/sessions and the
  `mobile.db` filename while revoking obsolete limited-role state
- **Backup/restore** — launcher database-family recovery includes `mobile.db`,
  WAL/SHM/journal sidecars, and current-copy preservation when restoring a
  backup

---

## Docker & VPS Runtime

The official container path packages the complete Row-Bot server feature set
for long-running, single-owner operation. It preserves local-first reachability
by publishing only to host loopback unless an operator intentionally layers a
private or HTTPS route in front.

- **Multi-stage image** — `deploy/docker/Dockerfile` resolves the locked Python
  environment in a builder, copies immutable application/runtime assets into a
  non-root Python 3.13 image, bundles the matching Playwright Chromium plus
  media/Node/uv helpers, and records lock/revision metadata
- **OCI identity** — version and source-revision build arguments become OCI
  labels and `/opt/row-bot/build-metadata.txt`, allowing release validation to
  compare the image to the exact tag commit
- **Runtime boundary** — UID/GID 10001 runs `row-bot serve`, writes durable state
  only to `/data`, uses `/run/secrets` for read-only secret material, and sets
  `ROW_BOT_CONTAINERIZED=1` so nested Developer Sandbox behavior fails closed
- **Hardened Compose** — `compose.yaml` uses loopback-only port publication,
  read-only rootfs, tmpfs, all-capability drop, `no-new-privileges`, bounded
  JSON logs, a 45-second stop grace, a health check, and `unless-stopped`
  supervision
- **Separate persistent volumes** — `row_bot_data` owns `/data` and
  `row_bot_secrets` owns the encryption key; both are project-scoped so multiple
  Compose projects remain isolated
- **Network-free key initializer** — a short-lived root init service with no
  network and a read-only filesystem creates and permission-hardens one random
  `ROW_BOT_SECRET_STORE_KEY` in the secrets volume before the app starts
- **Encrypted credential backend** — `secret_store.py` uses AES-GCM records under
  `/data/secure-secrets` when the platform keyring is unavailable and the
  persistent server key is mounted; identity-bound filenames, nonces, magic
  version bytes, atomic writes, size/type/symlink checks, and no plaintext
  fallback preserve provider and account credentials across replacement
- **External secret files** — advanced operators can mount an allowlisted
  read-only directory instead of the generated key volume; provider/channel
  resolution reports display-safe source/fingerprint state and fails closed on
  conflicting values
- **Deployment overlays** — `compose.build.yaml` builds from the checkout,
  `compose.vps.yaml` uses host networking with exact public/proxy settings, and
  `compose.secrets.yaml.example` demonstrates an absolute read-only host secret
  directory
- **Host examples** — `deploy/reverse-proxy/Caddyfile.example` and
  `deploy/systemd/row-bot.service.example` keep TLS/reverse-proxy and Compose
  lifecycle responsibilities explicit without modifying firewall policy
- **Operational lifecycle** — the Docker runbooks cover invitation bootstrap,
  health, explicit model downloads, multiple instances, stopped-state backup,
  restore, pull-first upgrade, digest/version rollback, session recovery,
  Developer limits, and deliberate volume removal
- **Owned-resource smoke runner** — `scripts/smoke_docker_server.py` runs a
  prebuilt image on a random loopback port, never pulls/builds, refuses name
  collisions, and removes only its exact labeled container/volume; it requires
  two stable health/readiness samples, gives functional requests bounded
  configurable deadlines, retries transient transport failure only for GET,
  never replays POST access/session mutations, and emits bounded redacted
  container state/log diagnostics on failure
- **Container CI** — `.github/workflows/container.yml` verifies native amd64 and
  arm64 images on relevant pull requests; release jobs validate tag/version/commit
  identity and smoke before GHCR login/push, then publish a multi-architecture
  version manifest and update `latest` only for final releases

---

## Mobile Web Companion

The mobile companion is the compact presentation of the same authenticated
single-owner application. It is served by the running Row-Bot process and adds
no account, cloud relay, sync backend, reduced role, or separate authorization
model: an authenticated phone reads and mutates the same local product state as
the full desktop layout.

### Mobile Shell

- **Presentation routing** — `ui/mobile.py` selects the compact shell for an authenticated compact invitation or explicit `?mobile=1` presentation request; `AccessContext` carries owner identity independently, and an authenticated owner can return to the full layout
- **Phone-native navigation** — Chat, Activity, Workflows, Knowledge, and Settings render as full-height mobile surfaces without desktop drawers, terminal, Buddy, Developer Studio, or Designer Studio chrome
- **Shared durable state** — mobile chat uses the normal thread/checkpoint store, parent orchestration stream, file attachments, profile/model/reasoning selection, manual skills, auto-title rules, and generation controls
- **Conversation list boundary** — internal `agent_child` threads are excluded from Recent chats while their durable activity indicator remains attached to the owning parent conversation
- **Responsive composer** — safe-area-aware left/right/bottom padding, bounded Model and Thinking controls, a contained action row, and non-shrinking send button keep narrow mobile layouts usable
- **Activity surface** — tool approvals, orchestration/child approvals, active chat generations, running/stoppable workflows, and recent workflow history are combined into a compact operational view
- **Safe workflow editing** — the simple mobile editor can create and update prompt workflows while detecting and preserving advanced graph workflows that require desktop controls
- **Settings adapters** — provider cards expose display-safe connection and credential summaries; installed skills can be enabled or pinned; installed plugins can be enabled or disabled when setup is complete; Skills Hub and Plugin Marketplace install/configure/update flows remain desktop-only
- **PWA boundary** — `mobile/routes.py` serves the manifest, service worker, icon metadata, and offline page; the service worker caches only public shell assets and explicitly avoids authenticated/private application routes

### Access & Presentation Integration

- **Shared access service** — compact invitations, claims, sessions, renewal,
  logout, device/session lists, revocation, and audit all use `access/service.py`
  and `AccessStore`; `mobile/*` retains compatibility/PWA helpers rather than a
  second authority model
- **Full owner authority** — desktop and compact invitations differ only in
  initial layout and trusted/temporary lifetime; both produce normal revocable
  owner sessions with complete Settings access
- **Scheme-aware cookies** — HTTPS uses an instance-isolated Secure
  host-prefixed cookie; LAN HTTP uses a separate HttpOnly cookie without
  pretending the transport is secure; SameSite and server expiry apply to both
- **HTTP/WebSocket parity** — access middleware runs before NiceGUI and applies
  the same session and exact-origin gate to page, API, and live socket traffic
- **Forwarded-header defense** — forwarding metadata is accepted only from an
  explicitly trusted proxy CIDR; untrusted or malformed forwarded loopback
  cannot claim desktop owner identity
- **Route discovery** — `access/access_routes.py` combines current listen state,
  verified current-server origin, assigned interface addresses, owned Tailscale
  Serve, separately managed ngrok, UI-managed trusted addresses, and
  environment-managed public origins into a canonical invitation inventory
- **Device/session control** — compact Settings can create both invitation
  layouts, sign out the current browser, list devices and sessions, and revoke
  either immediately through the same owner-guarded service as full Settings
- **Browser-local voice** — compact remote chat captures its own microphone in a
  secure browser context and returns local STT/TTS results to that session
  without activating the host microphone

### Mobile Limitations

- **Host required** — the desktop/server process must remain running and reachable; mobile is not an offline replica even though the PWA provides an offline status page
- **Desktop-oriented rich editors** — the compact presentation exposes full owner Settings and state, but Developer Studio, Designer Studio, Skills Hub browsing/install/create, Plugin Marketplace setup/update/uninstall, and advanced workflow graph editing remain optimized for the full desktop layout
- **No native Computer Use** — mobile, channel, scheduled, workflow, child-agent, and headless/server turns cannot acquire the native desktop-control lease; Computer Use is restricted to the interactive local desktop UI
- **No hidden network enablement** — Row-Bot does not automatically broaden its bind host, install/sign in to Tailscale, enable Funnel, overwrite another Serve route, or trust a reverse proxy. LAN and owned Tailscale changes require explicit reviewed owner actions

---

## Chat & Conversations

- **Multi-turn threads** — conversation history is stored in SQLite via LangGraph checkpointing and local thread metadata
- **Auto-naming and switching** — threads are named from the conversation and can be reopened, exported, or deleted individually; placeholder mobile titles remain auto-renamable while manual names retain ownership and are never overwritten by a later first message
- **Per-thread model override** — conversations can pin a different local or provider model than the global default
- **Per-thread reasoning override** — supported models can pin their own Provider default, effort, On/Off, or budget value in the thread; changing models swaps to the target model's independent saved selection
- **Per-thread Agent Profile** — conversations can carry a selected Agent Profile whose instructions and policy are injected into agent and chat-only turns
- **Goal Mode continuity** — active goals, progress, blockers, and continuation decisions are tied to the thread so long work stays visible across turns
- **Input-level model and reasoning pickers** — the main chat Model and Thinking selectors live in the chat input area, load exact cached capability state immediately, refresh asynchronously, and keep the top bar focused on thread state
- **Desktop context meter** — `ui/chat_components.py` renders the event-driven
  complete-next-input estimate, capacity/threshold state, compaction activity,
  and failure guidance beside the desktop composer without widening the compact
  mobile input surface
- **File attachments** — drag-and-drop, clipboard paste, and standard upload flows handle images, PDFs, spreadsheets, JSON, and text
- **Media persistence** — chat media is stored per thread on disk with sidecar metadata; generated content persists more aggressively than transient capture artifacts
- **Inline rich rendering** — Plotly charts, Mermaid diagrams, YouTube embeds, syntax-highlighted code, and images render directly in the transcript
- **Shared chat components** — `ui/chat_components.py` provides the responsive input bar, exact-model reasoning picker, upload flow, and message container for main chat, Designer Studio, and Developer Studio
- **Bounded transcript rendering** — `ui/transcript.py` chooses a visible window for large threads, exposes load-earlier behavior, and avoids rendering every historic row on initial open
- **Checkpoint loading without graph import** — transcript loaders can read checkpoint messages and token usage without constructing the agent graph, reducing blank-thread and large-thread latency
- **Status monitor panel** — Home health-check pills, diagnosis actions, and quick settings links surface runtime health at a glance
- **Workflow Console integration** — approvals, active goals, child-agent runs, recent workflow runs, and insight actions are visible without leaving the conversation experience
- **Agent drawer and profile UI** — parent orchestration groups, child status, profile selection, Profile Library, dependencies, required/detached state, approval, retry, and recovery activity are exposed through dedicated UI surfaces instead of being hidden in transcript text
- **Compact Agent cards** — direct and delegated children render as compact live lifecycle rows; authoritative run ids are registered at the tool-result boundary, refresh keys ignore heartbeat-only churn, and parent groups retain later waves
- **Orchestration transcript messages** — approval requests, steering, required joins, async detached completions, and final parent output are inserted at the correct turn boundary, deduplicated across live rendering/reload, and carry UI metadata through LangChain checkpoint conversion
- **Durable context events** — compaction success/failure rows are idempotent
  presentation records merged at their message boundary on live render and
  reload; they remain outside checkpoint model messages and token accounting
- **Streaming hardening** — detached streams persist final content and media, grouped tool-call counts update during streaming, thinking text survives reattach/final render, and safe timer helpers avoid UI writes after clients disconnect
- **Quiet trace presentation** — live, pending, completed, failed, grouped, and
  reopened generic tool results share compact secondary row classes, while
  expanded content remains full strength and discovered calls retain the real
  external integration label
- **Live control cards** — browser and Computer Use runs surface a shared sanitized control card with direct Stop/takeover/resume state, while native screenshots remain ephemeral and shielded during user or approval handoff
- **Active-thread feedback** — sidebar and mobile spinners are bound to durable blocking orchestration or genuinely streaming state for the owning parent thread, never detached background work or internal child-thread ordering
- **Output truncation warnings** — the UI warns when a response was cut short by model token limits

---

## Notifications

- **Desktop notifications** — workflow completions, reminders, and other events can raise OS-level notifications
- **Sound effects** — distinct audio chimes are used for workflow completion and timer-like alerts
- **In-app toasts** — lightweight status notifications appear in the UI; errors can remain persistent until dismissed
- **Unified API** — all notification surfaces flow through a single `notify()` entry point
- **Approval awareness** — approval requests can surface as both UI notices and channel-delivered prompts
- **Durable terminal notices** — child-agent and Goal Mode terminal events can be persisted for once-only channel delivery, retried after channel startup, and recorded in the conversation checkpoint after successful send

---

## Stability & Diagnostics

Row-Bot includes a stability layer for the kinds of failures that are hard to catch from normal request logs: UI callback crashes, client-side JavaScript errors, event-loop stalls, memory spikes, and startup/shutdown issues.

- **`stability.py`** — centralizes crash reporting, UI callback error reports, client-side error capture, asyncio exception handling, thread/unraisable hooks, memory snapshots, and event-loop lag logging
- **Launcher diagnostics** — `launcher.py` writes structured launch timing, splash/picker helper failures, server readiness, window-open decisions, and shutdown/update handoff events to `launcher.log`
- **Safe timers** — `ui/timer_utils.py` wraps deferred UI callbacks and polling timers so disconnected clients or deleted NiceGUI slots do not crash the app silently
- **Settings diagnostics** — model settings collection/render phases log timings and memory snapshots, while cached model catalogs and short-lived provider-status caches keep large provider refreshes and OAuth health checks off the critical UI path
- **UI performance helpers** — `ui/performance.py` provides render generation tokens, timed UI sections, slow-section logging, and safe UI callback/task wrappers used by Settings, Knowledge, chat, and graph surfaces
- **Startup sequencing** — startup status covers cached model catalog load, workflow scheduler, deferred orchestration/Agent Run repair, document supervisor recovery, MCP, plugins, channel migration/autostart, registered tunnel startup, and knowledge graph load without invoking providers during recovery
- **Channel and tunnel degradation** — Telegram partial starts are cleaned before
  retry or user action, command-menu registration cannot take polling offline,
  manual channel failures log only channel identity and exception type, and
  registered main-app tunnel auto-start runs off the UI event loop
- **Clean shutdown** — app shutdown attempts ordered channel, tunnel, MCP, scheduler, and process cleanup to reduce locked logs and lingering child processes
- **Cancellation diagnostics** — provider response closure, subprocess termination and file-backed partial output, cancelled MCP futures, channel preview cleanup, and generation-linked child-stop behavior emit scoped diagnostics rather than being misreported as successful completion
- **Computer Use diagnostics** — readiness codes, pinned-runtime verification, permission recovery, private-client shutdown, target invalidation, and lease cleanup are logged without typed values, screenshot bytes, or raw action arguments
- **Task database diagnostics** — Home, Command Center, and Row-Bot Status can report workflow/agent/goal schema state, repair results, and launcher recovery guidance when workflow storage is missing or corrupt
- **Access diagnostics** — `row-bot access doctor`, request rejection logs, route-policy snapshots, Tailscale ownership checks, and session/device health remain display-safe and never serialize invitations, cookies, tokens, or credential values
- **Context diagnostics** — usage snapshots record estimated and confirmed input
  counts, effective/usable/threshold capacity, capacity source/state, model/mode,
  checkpoint revision, and prompt/tool/policy fingerprints without serializing
  the prompt or summary into diagnostic events; bounded compaction failures use
  reason codes and unexpected failures retain only the exception class
- **Document diagnostics** — queue integrity, expired leases, missing sources, staging/work orphans, partial shards, stale embeddings, and compatible legacy vectors are surfaced through Documents health and recover without reading real user data in deterministic tests
- **Frontend error reporting** — browser-side exceptions are reported back into the structured log with enough context to correlate with UI actions
- **Performance probes** — memory RSS/VMS/thread counts, event-loop lag, token-counter refresh, model settings load, Settings tab render generations, transcript rendering, FAISS rebuild, and catalog refresh timings are logged for support investigations

---

## Bundled Skills

Skills are reusable instruction packs that shape how the agent thinks and responds. Each skill is a `SKILL.md` file with YAML frontmatter (display name, icon, description, required tools, tags) and freeform instructions injected into the system prompt when enabled.

Row-Bot ships with **17 manual bundled skills** and **22 tool guides**. Manual skills are toggled from Settings; tool guides auto-activate when their linked tools are available.

| Skill | Description |
|-------|-------------|
| **🧠 Brain Dump** | Capture unstructured thoughts and organize them into structured notes saved to memory |
| **💻 Claude Code Delegation** | Coordinate Claude Code CLI as an external coding agent through Row-Bot's approval-gated shell workflow |
| **☀️ Daily Briefing** | Compile a morning briefing with weather, calendar, and news headlines |
| **📊 Data Analyst** | Analyze datasets, produce statistical summaries, and create insightful charts |
| **💻 Developer Coding** | Plan and implement scoped code changes in Developer Studio using repo-aware tools and approval policy |
| **🔎 Developer Review** | Review code for bugs, regressions, missing tests, and risky behavior before summarizing |
| **🚀 Developer PR Prep** | Prepare branch, test, commit, push, and PR-ready summaries for Developer workspaces |
| **🧩 Developer Custom Tools** | Design, test, and promote Custom Tools from repos or folders without over-broad command surfaces |
| **🔬 Deep Research** | Perform multi-source research on a topic and produce a structured report |
| **🎨 Design Creator** | Structured workflow for presentations, one-pagers, reports, and visual layouts in Designer Studio |
| **🗣️ Humanizer** | Write in a natural, human tone without AI-speak or corporate filler |
| **📚 Knowledge Base** | Manage the personal knowledge base across graph memories, document intelligence, and the wiki vault |
| **📋 Meeting Notes** | Turn raw notes into actionable minutes with follow-ups and clear structure |
| **🎯 Proactive Agent** | Anticipate user needs, ask clarifying questions, and self-check work at milestones |
| **🪞 Self-Reflection** | Review memory for contradictions, gaps, and stale information |
| **⚙️ Task Automation** | Design effective workflows with steps, conditions, approvals, triggers, and delivery routing |
| **🌐 Web Navigator** | Strategic patterns for browser automation, research, forms, and data extraction |

- **Claude Code Delegation** — disabled by default, this skill treats Claude Code CLI as an external coding worker while Row-Bot remains coordinator. It favors bounded `claude -p` print-mode tasks, explicit working-directory checks, `--allowedTools`, turn/budget limits, diff inspection, Row-Bot-side verification, and user approval before write-capable or destructive delegation.
- **User skills** — custom skills live in `~/.row-bot/skills/<name>/SKILL.md`; user skills with the same name as a bundled skill override it
- **In-app skill editor** — skills can be created and edited directly from Settings
- **Skills Hub install path** — Skills Hub installs third-party or user-provided skills into the same user skill library, preserving provenance while keeping bundled skills immutable
- **Per-skill enablement** — only enabled manual skills are injected into the system prompt
- **Pinned defaults** — users and status-tool actions can pin selected skills as default active guidance, while explicit composer/thread/workflow selections still narrow the prompt for a specific context
- **Per-thread, per-workflow, and composer overrides** — skill selection can be narrowed for individual threads and workflows, while chat composer controls expose explicit skill chips and slash-command activation
- **Progressive task activation** — enabled manual and plugin skills can be
  searched locally, loaded for one parent or child task, restored on reopen,
  and shown through one compact receipt; at most five automatically loaded
  skills are retained independently from pinned/manual state
- **Tool guides remain automatic** — Agents, Browser, Calendar, Chart, Custom Tool Builder, Designer, Developer, Email, Filesystem, Goal, Math, MCP, Shell, Telegram, Row-Bot Status, Tracker, Updater, Video, Vision, Weather, Wiki, and X guides are in the built-in set

---

## Public Docs Site & Automation

The hand-curated public landing page under `docs/` remains separate from the
generated public documentation site. Docusaurus source lives under `docs-site/`;
the reviewed static build is synchronized into selected generated directories
under `docs/` and is published through GitHub Pages when the source and artifact
merge to `main`.

- **Docusaurus source** — `docs-site/` contains the documentation application, sidebars, authored/generated MDX, Pagefind integration, brand/static assets, and local preview/build scripts
- **Comprehensive guide structure** — source pages cover getting started, app shell, chat, profiles/goals/agents, workflows, progressive tool/skill discovery, Designer and Developer Studios, integrations, settings, knowledge/wiki/provenance, operations, extension trust, mobile/native behavior, privacy, troubleshooting, concepts, and generated references
- **Operations runbooks** — canonical Remote Access and Docker/VPS guides cover single-owner authority, invitations/sessions, Tailscale and proxy trust, Compose startup, persistent credentials, backups, upgrade/rollback, recovery, and Developer/container boundaries
- **Structured coverage map** — `docs-content/metadata/ui_surfaces.yml` is the authoritative user-facing surface inventory; routes, screenshots or no-image reasons, settings/home/dialog inventories, and how-to metadata feed generation and validation
- **Isolated real UI capture** — `docs_capture.py`, `seed_real_app_demo_data.py`, and `capture_real_ui_screenshots.py` refuse the normal data directory, disable background autostart/network status checks, seed neutral display-only provider/channel/plugin/MCP states, and capture stable desktop/mobile scenarios
- **Screenshot review contract** — capture metadata records stable ids, scenarios, viewport, status, and source route; reviewers check every changed image for private data, misleading state, clipping, and legibility before marking it approved
- **Generation scripts** — `collect_inventory.py`, `generate_mdx.py`, `write_public_user_guide_pages.py`, `generate_llms_txt.py`, and templates turn source/runtime inventories into control-level references, human pages, and machine-readable `llms.txt` / `llms-full.txt`
- **Searchable static output** — the Node build runs Pagefind over Docusaurus output and commits the resulting HTML, assets, screenshot copies, search index, sitemap, and machine-readable files under the generated `docs/` paths used by Pages
- **Canonical marketing routes** — the hand-curated Home, Features, Architecture, Contact, and 404 pages share navigation, metadata, responsive assets, and sitemap ownership; duplicate Docusaurus static shadow copies are excluded
- **Public-site measurement boundary** — shared marketing JavaScript initializes
  Google tag services and records desktop download, Linux install-view, and
  installation-guide actions. This runs only on the public website; application
  runtime, prompts, files, memories, tool arguments, and channels are outside
  the website event contract
- **Deterministic publication sync** — `sync_github_pages.py` refreshes only `docs/assets`, `docs/docs`, `docs/img`, `docs/pagefind`, `docs/search`, and selected machine-readable files; it preserves `docs/index.html`, feature/contact/architecture pages, analytics, contact handling, and shared marketing assets
- **CI verification** — `.github/workflows/docs.yml` uses a pinned build environment, collects inventory, checks generated references, regenerates LLM files before Docusaurus, validates screenshots and source, builds a review report, runs `tests/docs`, builds Docusaurus/Pagefind, normalizes line endings, and verifies the committed Pages artifact structurally
- **Review posture** — documentation generation and publication remain source-controlled and reviewable, do not call live providers/channels/MCP servers during deterministic CI, and remain distinct from installer/release artifact generation

---

## Core Modules

Runtime code is packaged under `src/row_bot`. The paths below are package-relative unless a top-level docs, scripts, installer, or static directory is named explicitly.

| File | Purpose |
|------|---------|
| **`app.py`** + **`ui/`** | NiceGUI application shell, access/request-context routing, full and compact chat surfaces, lazy home tabs, health/status bar, workflow console, Agent drawer/groups, Goal UI, Profile Library, Remote Access, Computer Use setup/live-control surfaces, settings dialog, docs capture hooks, UI performance helpers, and native-webview integration points |
| **`access/`** + **`ui/access_context.py`** + **`ui/remote_access_settings.py`** | Single-owner deployment/request policy, HTTP/WebSocket middleware, invitations, devices, sessions, cookies, assigned-interface and trusted-origin routes, live runtime-policy mutation, managed-policy precedence, CLI/doctor, Tailscale Serve ownership, launcher control, access service/store, UI authorization, and full/compact Remote Access controls |
| **`mobile/`** + **`ui/mobile*.py`** | Compact presentation and legacy access compatibility, PWA endpoints, full-screen shell, chat, browser-local voice hooks, Activity, workflow editor, and phone-safe provider/skill/plugin/settings adapters |
| **`brand.py`** + **`runtime_paths.py`** | Row-Bot product identity, public naming constants, runtime path detection, and packaged/source checkout path helpers |
| **`buddy/`** + **`ui/buddy.py`** | Buddy companion event bus, behavior brain, config, asset validation, Hatch generation, in-app docked/undocked presence, and optional desktop overlay helpers |
| **`designer/`** | Designer Studio subsystem: gallery, editor, tooling, storage, exports, presentation mode, publishing, and asset hydration |
| **`developer/`** | Developer Studio subsystem: workspace links and child-folder registration, folder-scoped writer ownership, Git helpers, durable worktree allocation, approval policy, Docker/local runtime, sandbox state, inspector snapshots, todos, file tree, diffs, GitHub helpers, Custom Tool internals, and UI |
| **`ui/chat_components.py`** | Responsive shared chat input, exact-model Thinking picker, upload, message-area, active-skill chip, stable Send/Stop slot, and desktop context-meter components reused by main chat, Designer Studio, and Developer Studio |
| **`ui/chat_composer_extras.py`** | Shared slash palette, skill picker, skill chips, and composer-level Smart Skills controls reused across chat surfaces |
| **`agent.py`** | LangGraph ReAct agent, prompt assembly, Agent Profile injection, authorized progressive tool/skill snapshot construction, tool allowlist handling, fixed-envelope and complete-input preparation/accounting, recoverable rolling compaction, checkpointed budget hooks/finalization and orphan-only repair, runtime readiness routing, chat-only execution, provider/reasoning transcript normalization, separated answer/thinking streams, context events, interrupt handling, cache clearing, and background execution integration |
| **`agent_budget.py`** + **`agent_settings.py`** | Checkpoint-safe model-iteration budgets, no-progress digests, exactly-once terminal finalization, validated application-wide work/delegation limits, and atomic `agent_settings.json` persistence |
| **`agent_profiles.py`** + **`agent_context.py`** + **`agent_tool_catalog.py`** | Built-in/user Agent Profile registry, profile persistence, profile context assembly, policy blocks, profile search/selection helpers, and tool catalog metadata for scoped delegation |
| **`agent_orchestrator.py`** | Versioned parent-led orchestration, required/detached members, dependencies/waves, ordered thread events, group joins, initial parent continuation snapshots, parent leases/checkpoints, approval/steering resume, orphan-only parent repair, transient retry, stop, exactly-once finalization/delivery, activity projections, and restart repair |
| **`agent_runner.py`** + **`agent_runs.py`** + **`agent_run_messages.py`** | Durable Agent Run FIFO/capacity queue, child-agent nesting/active-time lifecycle, orchestration membership, settings snapshots and model-iteration progress, parent-thread lifecycle/approval/completion messages, run events, parent/child edges, generation-linked stop/wait/resume state, workspace-keyed write locks, stale-run recovery, and shared Agent Run/orchestration tables in `tasks.db` |
| **`approval_messages.py`** | Normalized approval payloads, model-reason preference, redaction/truncation, compact channel/mobile rendering, and display-safe source labels |
| **`cancellation.py`** + **`process_cancellation.py`** | Generation-scoped cleanup registration, provider/tool/browser/Computer Use/process Stop propagation, process-group termination, file-backed output capture for detached descendants, bounded timeout handling, and cancellation results |
| **`goals.py`** | Thread-scoped Goal Mode state, goal commands, progress/evidence/blocker tracking, continuation prompts, verifier decisions, and synchronization with Agent Run status |
| **`approval_policy.py`** + **`tools/approval_gate.py`** | Unified approval modes and tool-level approval gate helpers for chat, Developer, workflows, channels, and promoted tools |
| **`threads.py`** | SQLite-backed thread metadata, LangGraph checkpoint wiring, checkpoint transcript helpers, revision-validated rolling-summary and context-usage persistence, presentation-only thread events and channel claims, per-thread media storage, model/profile overrides, provider-qualified reasoning selections, and cleanup hooks for chat-created agent/goal/skill state |
| **`memory.py`** | Backward-compatible memory wrapper that maps legacy memory calls onto the knowledge graph implementation |
| **`memory_policy.py`** + **`memory_evolution.py`** | Bounded auto-recall scoring/filtering/tracing plus memory status, tier, confidence, evidence, review, superseding, archival, and evolution journal helpers |
| **`knowledge_graph.py`** | Entity/relation store, FAISS and FTS5 recall, NetworkX traversal, deduplication, relation normalization, recall reinforcement, and graph stats |
| **`wiki_vault.py`** | Obsidian-compatible markdown vault export, text-stable entity-ID frontmatter, indexing, search, and conversation export |
| **`dream_cycle.py`** | Nightly graph refinement engine: merges, enrichment, decay, relation inference, insights analysis, proposal seeding, and journal logging |
| **`evolution.py`** | Controlled self-evolution proposal store, validation, action runs, rejection memory, feedback reports, skill mutation bounds, and curator dry-runs |
| **`document_uploads.py`** + **`document_jobs.py`** | Bounded streamed staging, hash/dedup identity, durable SQLite batches/jobs, transitions, leases, progress, pause/cancel/retry, restart/orphan recovery, and single-flight supervisor |
| **`document_index.py`** | Bounded per-document vector segments, atomic corpus manifests, deterministic current/legacy top-k retrieval, stale exclusion, ID-scoped removal, rebuild, cache release, and index health |
| **`document_extraction.py`** | Resumable rolling-map/hierarchical-reduce document extraction, persisted intermediate summaries, cancellation, provider-failure isolation, and idempotent graph/wiki/document finalization |
| **`models.py`** | Local model compatibility facades, versioned exact-value Auto/fixed context policy and migration, 64K local Auto target, custom server-managed capacity, native/requested/observed/fallback capacity resolution, usable-input and compaction thresholds, coalesced OpenCode gateway/models.dev discovery, Quick Choices, provider detection, reasoning-aware model factories, and legacy model APIs |
| **`prompt_context.py`** + **`prompt_cache.py`** | Prompt section assembly, stable/ephemeral section inventory, direct-Anthropic prompt-cache marker gating, stable fingerprint reporting, and provider cache usage normalization |
| **`providers/`** | Provider-scoped auth/secret resolution, normalized model catalogs, provider-isolated last-known-good catalog refresh, native Ollama capability precedence, exact-model capability/reasoning resolution, OpenCode native route metadata, Gemini tool-schema compatibility, Atlas Cloud, Requesty, Claude Subscription, xAI Grok OAuth, xAI media/catalog helpers, provider-qualified resolution, model-scoped readiness/probes, custom endpoint profiles, cancellable reasoning-aware runtime construction, one-shot explicit-reasoning fallback, phased OpenAI-compatible timeout/pre-stream retry, and display-safe provider status |
| **`embedding_config.py`** + **`embedding_providers.py`** | Embedding provider selection, strict cache-only local loading with download-matching snapshot filters, explicit download/repair, shared asynchronous provider state, structured recall fallback, local/cloud backends, vector metadata, and stale-index detection |
| **`documents.py`** | Document loading/chunking facade, bounded shard build/publication, retrieval compatibility, document records, source retirement, per-document cleanup, and vector reset/rebuild |
| **`voice/__init__.py`** | Classic local microphone state machine, faster-whisper and selected SenseVoice dispatch, explicit local model loading, and persisted local voice settings |
| **`voice/`** | Realtime voice runtime, provider contracts and catalog, OpenAI realtime client, browser-local Whisper capture/decoding/transcription/session output, verified FunASR/SenseVoice installation and CPU provider, action dispatch, agent bridge, cue policy, speech policy, and output coordination |
| **`tts.py`** | Kokoro text-to-speech integration, voice catalog, and streaming playback |
| **`vision.py`** | Camera capture, screen capture, and workspace image analysis via local or provider vision models |
| **`computer_use/`** + **`tools/computer_use_tool.py`** + **`ui/computer_use.py`** + **`ui/live_control.py`** | Pinned Cua manifest/private client, disclosure/install/readiness, action policy, exclusive target-window service, native application tool, macOS permission recovery, and ephemeral live-control UI |
| **`data_reader.py`** | Shared structured-data loader for CSV, TSV, Excel, JSON, and JSONL |
| **`data_paths.py`** | Shared Row-Bot data-directory and SQLite path resolution for tasks, memory, threads, access/mobile compatibility, diagnostics, backup, and recovery commands |
| **`docs_capture.py`** | App-side helpers for seeded real UI documentation capture and screenshot automation |
| **`launcher.py`** | Desktop/server launcher, `serve` and `access` CLI integration, host/port/deployment resolution, native/tray selection, splash/window picker, authenticated child environment, loopback-only restart control, app lifecycle, logging, macOS native tray host, and DB-family recovery commands |
| **`update_handoff.py`** | Detached Windows update handoff helper that waits for Row-Bot processes/ports to exit before starting the installer |
| **`stability.py`** | UI callback/error capture, asyncio/thread exception hooks, memory snapshots, event-loop lag logging, and crash diagnostics |
| **`startup_diagnostics.py`** | Early startup probes for optional native packages that can break app import/startup when partially installed |
| **`api_keys.py`** + **`secret_store.py`** | API key storage/retrieval, OS-keyring backend, metadata-only local files, legacy plaintext migration, allowlisted read-only server secret files, encrypted persistent server records keyed from a separate mount, and session-only fallback when no secure backend is configured |
| **`identity.py`** | Assistant name, personality, and self-improvement preference storage with sanitization |
| **`self_knowledge.py`** | Capability manifest, identity-line builder, live runtime state builder, and prompt-time self-knowledge assembly |
| **`insights.py`** | Structured insight store with dedup, pruning, pin/dismiss/apply state, and last-analysis tracking |
| **`prompts.py`** | Centralized prompt templates including summarization, extraction, and dream-insights analysis |
| **`memory_extraction.py`** | Background conversation scan that extracts entities and relations the live agent did not save |
| **`skills.py`** | Discovery, loading, enablement state, pinned defaults, override, and prompt-building for manual skills and tool guides |
| **`capability_search.py`** + **`skill_discovery.py`** + **`skills_activation.py`** + **`slash_commands.py`** | Deterministic local capability ranking, unified enabled manual/plugin skill snapshots, safe progressive search/load bridges and reference confinement, persistent capped per-task automatic activation, pinned/manual defaults, explicit skill commands, draft suggestions, disabled-skill handling, and slash-command parsing |
| **`skills_hub/`** | Skills Hub source adapters, import detection, installers, provenance, scanner, search index, source registry, and UI models |
| **`bundled_skills/`** | 17 built-in manual skills as `SKILL.md` packages |
| **`tool_guides/`** | 22 built-in tool-specific auto-activation guides |
| **`tasks.py`** | Workflow engine, SQLite persistence, schema validation/repair, APScheduler scheduling, profile-first workflow migration, pipeline execution, run history, safety mode, delivery routing, and shared storage connection used by Agent Profiles/Runs/Goals/Developer worktrees |
| **`notifications.py`** | Unified desktop, sound, and toast notification system |
| **`channels/`** | Channel ABC, registry, shared streaming/finalization engine, orchestration-aware delivery, durable thread notifications, checkpoint persistence, media helpers, auth/secret-file helpers, approval routing, command handling, tool generation, plugin-channel bridge integration, and bundled channel adapters |
| **`tunnel.py`** | Tunnel provider abstraction, ngrok integration, registered-origin runtime policy, exact owned-origin cleanup, and lifecycle manager; Tailscale Serve remains in `access/tailscale.py` |
| **`tools/agent_tool.py`** + **`tools/goal_tool.py`** | Child-agent delegation/status/wait/stop/profile/promotion tools plus Goal Mode progress/status tools |
| **`tools/row_bot_status_tool.py`** | Self-introspection and controlled self-management tool, including provider/media diagnostics, agent/goal reporting, skill pinning, and controlled self-evolution proposal operations |
| **`tools/developer_tool.py`** + **`tools/custom_tool_builder_tool.py`** | Developer workspace operations plus hardened conversational Custom Tool creation/testing/promotion surface |
| **`tools/calendar_tool.py`** | Request-scoped Google Calendar services, single-flight OAuth refresh, serialized mutations, bulk create, transient retry, timeout reconciliation, and typed Calendar operations |
| **`tools/`** + **`designer/tool.py`** | Self-registering core tool modules, registry, persisted external loading mode, immutable external capability records, bounded/sanitized manifests, schema-validating search/invoke bridges, base classes, Wikipedia recovery behavior, and LangChain tool conversion |
| **`plugins/`** | Plugin System v2 runtime, marketplace client, manifest validation, security scanner, Plugin Center UI, public API, channel runtime bridge, webhooks, Bot Framework auth helpers, MCP bridge, devtools, templates, and settings integration |
| **`mcp_client/`** | External Model Context Protocol client plus shared managed-runtime primitives: config, runtime sessions, marketplace search, requirements, safety classification, diagnostics, result normalization, and wrappers that enter progressive or eager external loading; private Cua transport remains outside the external MCP registry |
| **`migration/`** | Hermes/OpenClaw migration models, redaction, source detection, dry-run planning, realistic fixtures, guarded apply/report generation, and migration tests |
| **`deploy/docker/`** + **`deploy/reverse-proxy/`** + **`deploy/systemd/`** | Official hardened server image, Compose release/source/VPS/secret variants, persistent credential-key initialization, Caddy proxy, systemd lifecycle, and operator runbook |
| **`.github/workflows/container.yml`** + **`scripts/smoke_docker_server.py`** | Native amd64/arm64 image verification, release identity checks, owned-resource smoke testing with stable readiness, functional deadlines, GET-only transient retry and bounded redacted diagnostics, GHCR version/latest manifest publication, and secret-safe failure handling |
| **`docs-site/`** + **`docs-content/`** + **`scripts/docs/`** | Public docs source, canonical operations guides, coverage metadata, generated MDX/control references, isolated real UI screenshot capture, validation/review, LLM text generation, Pagefind search build, and deterministic GitHub Pages synchronization |
| **`static/`** | Bundled frontend assets such as Mermaid, graph/visualization helpers, and Buddy runtime/motion assets |
| **`version.py`** | Single source of truth for the current Row-Bot version, located at `src/row_bot/version.py` |

---

## Data Storage

All user data is stored under `~/.row-bot/` (or `%USERPROFILE%\\.row-bot\\` on Windows) unless `ROW_BOT_DATA_DIR` is set.

```text
~/.row-bot/
├── threads.db                     # Conversation history, LangGraph checkpoints, per-model reasoning choices, context usage/summary state, and presentation-only thread events
├── media/                         # Per-thread media files and sidecar metadata
├── tasks.db                       # Workflows, schedules, Agent Profiles/Runs/orchestrations, thread goals, Developer worktree ownership, write locks, run history, approvals, and durable channel notification intents
├── memory.db                      # Knowledge graph entities and relations
├── memory_vectors/                # FAISS vectors for semantic memory recall
├── memory_recall_trace.json       # Recent auto-recall decisions, semantic fallback/timing, and include/reject diagnostics
├── memory_evolution_journal.json  # Memory status/tier/review/superseding/audit changes
├── memory_extraction_state.json   # Last extraction metadata
├── extraction_journal.json        # Memory extraction journal
├── dream_config.json              # Dream Cycle settings
├── dream_journal.json             # Dream Cycle run log
├── dream_rejections.json          # Rejected inference-pair cache
├── insights.json                  # Structured insight store
├── controlled_evolution.json      # Controlled self-evolution proposals, action runs, rejection memory, and curator reports
├── feedback_reports/              # Redacted local feedback reports generated from controlled proposals
├── evolution_backups/             # Rollback references for controlled skill patch proposals
├── api_keys.json                  # API key metadata only; raw key values live in the OS credential store when available
├── cloud_config.json              # Legacy provider-model pinning compatibility data
├── providers.json                 # Provider metadata, Quick Choices, compatibility profiles, runtime/vision probe results, OAuth client-id diagnostics, model-count status, and credential fingerprints only
├── model_settings.json            # Current model, context policy v3 exact Auto/fixed modes, advanced provider/custom cap, and compatibility state
├── model_catalog_cache.json        # Background-refreshed provider/local-runtime model catalog rows and refresh diagnostics
├── context_catalog_cache.json      # Cached context-window metadata used before live provider refresh
├── embedding_config.json           # Active embedding provider/model settings
├── voice_settings.json             # Local Whisper selection and verified SenseVoice snapshot path
├── cache/
│   └── sensevoice/                 # Explicitly downloaded and verified local SenseVoice snapshots
├── document_ingestion/
│   ├── jobs.db                     # Durable document batches/jobs, leases, progress, content records, and resumable map/reduce summaries
│   ├── staging/                    # Bounded in-progress upload files
│   ├── work/                       # Unpublished index/extraction work owned by active jobs
│   └── completed/                  # Retained original source files for indexed documents
├── document_index/
│   ├── manifest.json               # Atomically published corpus/document shard inventory
│   ├── documents/                  # Per-document bounded FAISS segments and metadata
│   └── legacy_tombstones.json      # Legacy vector rows intentionally hidden after document removal
├── agent_settings.json             # Application-wide work rounds, child nesting/concurrency, and optional active-time limit
├── computer_use_settings.json      # Cua disclosure version, verified readiness state, and optional reviewed system-binary metadata
├── app_config.json                # Onboarding and first-run flags
├── user_config.json               # Avatar preferences, identity, and self-improvement settings
├── channels_config.json           # Channel enablement and per-channel config
├── mobile.db                      # Single-owner instance, invitation, device, session, revocation, migration, and display-safe access audit records
├── access_routes.json             # Atomic local-only/LAN listen selection plus exact UI-managed trusted origins
├── tailscale_serve_ownership.json # Exact Row-Bot-owned private Serve route fingerprint and target
├── secure-secrets/                # Encrypted provider/account records when a persistent server key is mounted
├── developer/
│   ├── workspaces.json             # Developer workspace links, approval mode, execution mode, sandbox image/network settings
│   ├── tool_capsules.json          # Registered Custom Tools and promotion/enablement metadata
│   ├── custom_tool_drafts.json     # Conversational Custom Tool Builder draft state
│   └── sandboxes/                  # Docker shadow workspaces and per-workspace sandbox state
├── shell_history.json             # Per-thread shell history
├── skills_config.json             # Manual skill enable/disable state
├── skills_activation.json         # Per-task pinned/manual/disabled/dismissed and capped automatically loaded skill state
├── skills/                        # User-installed skills; .hub/ stores Skills Hub lockfile, audit log, and quarantine
├── mcp_servers.json               # External MCP server config, global switch, tool enablement, approvals
├── mcp_marketplace_cache.json     # Cached MCP directory search results
├── migration-reports/             # Redacted migration plans, results, summaries, and archive snapshots
├── migration-backups/             # Pre-migration backups of overwritten target files
├── runtimes/                      # Explicitly installed user-space runtimes for MCP helpers and Computer Use
│   └── cua-driver/                # Pinned, checksum-verified private Cua executable and install manifest
├── skill_versions/                # Skill patch backups for skill hub and controlled self-evolution flows
├── row_bot_app.log                  # Structured application log
├── splash.log                     # Splash-screen diagnostics
├── inbox/                         # Files received via messaging channels
├── browser_profile/               # Persistent Chromium profile
├── browser_history.json           # Display-safe browser history and snapshots with sensitive typed/action values redacted
├── designer/
│   ├── projects/                  # Designer project JSON files
│   ├── references/                # Designer source/reference uploads
│   ├── assets/                    # Persistent project assets
│   └── published/                 # Published HTML bundles and shareable output
├── tracker/
│   ├── tracker.db                 # Habit and health tracker database
│   └── exports/                   # CSV exports for tracker charts
├── vector_store/                  # Legacy monolithic document vectors retained for compatible read/migration
│   └── embedding_metadata.json     # Legacy embedding provider/dimension metadata for stale-index detection
├── gmail/                         # Gmail OAuth tokens
├── calendar/                      # Calendar OAuth tokens
├── wiki/                          # Obsidian-compatible markdown vault export
├── x/                             # X OAuth tokens and tier metadata
├── installed_plugins/             # Marketplace-installed plugins
├── plugin_state.json              # Plugin config, marketplace metadata, health, settings, and enablement state
├── plugin_secrets.json            # Plugin API-key metadata only; raw key values live in the OS credential store when available
├── recovery/                      # Backups created by task/local DB reset and restore helpers
└── kokoro/                        # Kokoro TTS model and voice files
```

> Override the data directory by setting the `ROW_BOT_DATA_DIR` environment variable.

> The official Compose encryption key lives outside `/data` at
> `/run/secrets/ROW_BOT_SECRET_STORE_KEY` in the separate `row_bot_secrets`
> volume. Back up that key volume and the data volume together; the encrypted
> records in `secure-secrets/` are intentionally unreadable without the key.

---

## Comparison with Other Tools

### Why not just use another open-source assistant?

Most open-source AI assistants are still **developer tools disguised as products** — CLI-first and config-file-driven, with manual dependency and deployment assembly before the first useful prompt.

**Row-Bot is different.** It is packaged as a native desktop experience with one-click installers for Windows and macOS, a one-line Linux installer backed by a verified XDG tarball, and an optional official hardened Docker/VPS server path. Local-first defaults, authenticated multi-device access, context-aware long conversations, opt-in native Computer Use, and a GUI expose models, progressively loaded tools and skills, Goal Mode, Agent Profiles, durable parent-led orchestration, workflows, channels, Designer Studio, Developer Studio, Skills Hub, Custom Tools, controlled self-evolution, and memory without requiring terminal fluency.

### Why not just use ChatGPT?

| | ChatGPT / Claude / Gemini | Row-Bot |
|---|---|---|
| **Your data** | Stored on provider servers, subject to their privacy policies | Stays on your machine. With opt-in provider/custom models, only the current conversation and model-visible tool context go to the selected endpoint; memories, files, designer projects, and history remain local unless explicitly included |
| **Conversations** | Provider-owned chat history | Local SQLite-backed threads with per-model reasoning controls, complete-input metering, fixed-envelope checks, recoverable rolling compaction, durable presentation events, and export anytime |
| **Remote access** | Provider-hosted account and cloud relay | Direct connection to your running Row-Bot desktop/server through local, Tailscale, SSH, assigned-interface, or exact trusted HTTP(S) routes, protected by one-time invitations, revocable owner sessions, runtime-updated exact-origin gates, and local auth storage |
| **Cost** | Subscription or provider billing | Free with local models; provider/custom usage is upstream API billing, self-hosted infrastructure, or ChatGPT / Claude / xAI Grok subscription access only when you opt in |
| **Memory** | Limited, opaque, provider-controlled | Personal knowledge graph with entities, relations, bounded recall, audit/review states, visualization, wiki export, and background refinement |
| **Agent orchestration** | One assistant persona with limited delegation visibility | Goal Mode, Agent Profiles, one authoritative parent with required/detached children, dependencies, live multi-wave joins, folder-scoped parallel writers, steering, approvals, orphan-only parent repair, retry/recovery, exactly-once finals, checkpoint-safe budgets, configurable capacity limits, and promoted Agent-run workflows stay local and reviewable |
| **Tools** | Limited app integrations and provider-defined plug-ins | 30+ directly bound core tools plus locally searched enabled MCP, plugin, Custom Tool, channel, and skill catalogs under unchanged profile/approval/workspace boundaries; eager compatibility remains available |
| **Customization** | Pick a model and maybe a custom instruction | Swap provider-qualified models per thread, profile, workflow, child-agent run, or Developer workspace, choose each model's exact reasoning mode and context cap, configure name and personality, choose Auto/eager external capability loading, pin or progressively load task skills, build workflows, install Skills Hub packages, create Custom Tools, and use controlled self-evolution proposals |
| **Voice** | Usually cloud-processed | Local faster-whisper or explicitly installed offline FunASR/SenseVoice STT plus Kokoro TTS, with a separate realtime voice runtime for provider-backed conversational sessions |
| **Availability** | Internet required | Local models work offline; hosted providers and custom endpoints are optional |

> **Bottom line:** cloud assistants rent you access to someone else's system. Row-Bot gives you **personal AI sovereignty** — local durable state, provider choice when you want it, and all of your long-lived data under your own control.

### How is Row-Bot different from OpenClaw?

[OpenClaw](https://github.com/openclaw/openclaw) is a strong open-source personal assistant aimed at multi-channel delivery and developer-centric workflows. The two projects overlap in ambition but optimize for different users.

| | Row-Bot | OpenClaw |
|---|---|---|
| **Getting started** | One-click installers and GUI-first setup on Windows and macOS, plus one-line Linux install with browser-first launch | CLI-oriented install flow and heavier terminal expectations |
| **Model routing** | Local-first data with local, hosted, OpenCode, OpenRouter, Atlas Cloud, xAI Grok OAuth, ChatGPT / Codex, Claude Subscription, Ollama Cloud, provider-scoped live catalogs, native OpenCode transport discovery, exact reasoning/context controls, model-scoped probes, chat-only fallbacks, and phased OpenAI-compatible transport limits in one GUI | More cloud-first in typical setups |
| **Agent orchestration** | Goal Mode, Agent Profiles, durable parent-led required/detached child groups, dependencies, live joins, folder-scoped writer locks, steering, approvals, orphan-only checkpoint repair, retry/recovery, checkpoint-safe limits, profile/tool allowlists, and Agent-run workflow promotion | Different orchestration model with less desktop-first profile/goal visibility |
| **Memory** | Typed personal knowledge graph with bounded recall, audit/review states, visualization, wiki export, and structured relations | Simpler text-centric memory patterns |
| **Knowledge refinement** | 5-phase Dream Cycle with merge, enrich, decay, infer, and insight passes | Experimental dreaming-style memory promotion flows |
| **Document intelligence** | Bounded durable uploads, content dedup, atomic sharded retrieval, resumable extraction, queue recovery/controls, and structured graph provenance | Strong workspace tools but less graph-centric document knowledge modeling |
| **Designer / Canvas** | Designer Studio for decks, one-pagers, reports, published links, plus inline Mermaid and Plotly rendering | A2UI-style interactive workspace focus |
| **Developer / Code** | Developer Studio for Git workspaces with code threads, explicit child-folder assignment, folder-scoped parallel writer ownership, approval modes, file tree, todos, diffs, tests, GitHub/PR prep, worktrees, and optional Docker shadow sandbox | Developer-heavy CLI and terminal-first workflows |
| **Tools** | 30+ core tools plus Agent/Goal tools, Developer-native tools, opt-in native Computer Use, Skills Hub, Custom Tool Builder, promoted Custom Tools, and auto-generated channel tools, with local progressive discovery for enabled MCP/plugin/Custom Tool/channel capabilities and per-task skills | Broad built-in toolset with different emphasis |
| **Messaging channels** | 5 bundled channels with platform-aware live streaming/edit fallback, media handling, interactive/text approvals, durable terminal notices, and a sidebar monitor | Wider channel catalog and gateway focus |
| **Autonomous workflows** | Step-based workflows with approvals, conditions, triggers, concurrency groups, safety modes, Agent Profile overrides, and promotion from completed Agent Runs | Strong channel routing and automation, different orchestration model |
| **Desktop and server experience** | Native Windows/macOS app with tray, setup, responsive Model/Thinking/Approval composer controls, context meter, and opt-in Computer Use; Linux browser-first package; authenticated full/compact remote layouts with exact trusted-address management; and an official hardened multi-architecture Docker/VPS path | More developer-first and channel-first in practice |
| **Privacy posture** | All durable state local; no Row-Bot servers or first-party telemetry; optional Cua Driver telemetry is separately disclosed and consent-gated | Self-hostable and privacy-conscious, but with a different operational model |

> **In short:** OpenClaw is an excellent multi-channel gateway for developer-heavy setups. Row-Bot is optimized for **personal AI sovereignty** — local-first memory, structured knowledge, visible goals, reusable Agent Profiles, durable parent-led orchestration, integrated design and code workspaces, authenticated owner access, user-created tools, configurable self-knowledge, and native desktop plus optional server operation without requiring the terminal as the primary product surface.
