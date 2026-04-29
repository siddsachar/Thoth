# 𓁟 Thoth — Architecture & Detailed Design

> Full technical reference for every feature, module, and subsystem in Thoth.
> For a concise overview, see the [README](../README.md).

---

## Table of Contents

- [ReAct Agent Architecture](#react-agent-architecture)
- [Long-Term Memory & Knowledge Graph](#long-term-memory--knowledge-graph)
- [Wiki Vault](#wiki-vault)
- [Dream Cycle](#dream-cycle)
- [Document Knowledge Extraction](#document-knowledge-extraction)
- [Brain Model & Cloud Models](#brain-model--cloud-models)
- [Voice Input & Text-to-Speech](#voice-input--text-to-speech)
- [Shell Access](#shell-access)
- [Browser Automation](#browser-automation)
- [Vision](#vision)
- [Workflows & Scheduling](#workflows--scheduling)
- [Designer Studio](#designer-studio)
- [Thoth Status & Identity](#thoth-status--identity)
- [Self-Knowledge & Insights](#self-knowledge--insights)
- [Messaging Channels](#messaging-channels)
- [Tunnel Manager](#tunnel-manager)
- [X (Twitter) Tool](#x-twitter-tool)
- [Tool Guides](#tool-guides)
- [Image Generation](#image-generation)
- [Video Generation](#video-generation)
- [MCP Client & External Tools](#mcp-client--external-tools)
- [Migration Wizard](#migration-wizard)
- [Plugin System & Marketplace](#plugin-system--marketplace)
- [Auto-Updates](#auto-updates)
- [Habit & Health Tracker](#habit--health-tracker)
- [Desktop App](#desktop-app)
- [Chat & Conversations](#chat--conversations)
- [Notifications](#notifications)
- [Bundled Skills](#bundled-skills)
- [Core Modules](#core-modules)
- [Data Storage](#data-storage)
- [Comparison with Other Tools](#comparison-with-other-tools)

---

## ReAct Agent Architecture

- **Autonomous tool use** — the agent decides which tools to call, when, and how many times, based on your question
- **30 core tools plus auto-generated channel tools** — web search, email, calendar, file management, shell access, browser automation, vision, image generation, video generation, X (Twitter), a personal knowledge graph, Designer Studio, scheduled workflows, habit tracking, Thoth Status self-inspection, external MCP tools, and more
- **Streaming responses** — tokens stream in real-time with a typing indicator
- **Thinking indicators** — shows when the model is reasoning before responding
- **Smart context management** — automatic conversation summarization compresses older turns when token usage exceeds 80% of the context window, preserving the 5 most recent turns and a running summary; a hard trim at 85% drops oldest messages as a safety net; oversized tool outputs are proportionally shrunk so multi-tool chains fit within context; accurate token counting via tiktoken (cl100k_base)
- **Dynamic tool budgets** — the agent automatically adjusts how many tools are exposed to the model based on available context headroom; when context usage is high, lower-priority tools are temporarily hidden to prevent the system prompt from crowding out conversation history
- **Centralized prompts plus self-knowledge injection** — base prompt templates live in `prompts.py`, while `self_knowledge.py` injects a dynamic identity line, capability manifest, and live runtime state so Thoth can describe itself accurately without stale hard-coded copy
- **Live token counter** — progress bar in the sidebar shows real-time context window usage based on trimmed (model-visible) history
- **Graceful stop & error recovery** — stop button cleanly halts generation with drain timeout; agent tool loops are caught automatically (50-step limit for chat, 100 for workflows) with a wind-down warning at 75%; orphaned tool calls are repaired; API errors are surfaced as persistent red toasts and saved to the conversation checkpoint so they survive thread refresh
- **Workflow cancellation** — running background workflows can be stopped from the chat header, activity panel, or workflow card; cancellation is checked between every LangGraph node for clean shutdown
- **Displaced tool-call auto-repair** — if context trimming displaces tool-call/response pairs, the agent automatically detects and repairs the ordering before the next LLM call; orphaned tool calls trigger an automatic retry
- **Date/time awareness** — current date and time is injected into every LLM call so the model always knows "today"
- **Destructive action confirmation** — dangerous operations (file deletion, sending emails, deleting calendar events, deleting memories, deleting workflows, selected settings changes) require explicit user approval via an interrupt mechanism
- **Workflow-scoped background permissions** — background workflows use a tiered system: safe operations always run, low-risk operations (move file, move calendar, send email) are allowed with optional runtime guards, and irreversible operations (delete file, delete memory) are always blocked; shell commands and email recipients can be allowlisted per-workflow via the editor UI

---

## Long-Term Memory & Knowledge Graph

Thoth doesn't just store isolated facts — it builds a **personal knowledge graph**: a connected web of people, places, preferences, events, and their relationships. Every memory is an entity linked to others through typed relations, so the agent can reason about how things in your life connect.

- **Entity-relation model** — memories are stored as entities with a type, subject, description, aliases, and tags; entities are connected by typed directional relations (e.g. `Dad --[father_of]--> User`, `User --[lives_in]--> London`)
- **10 entity types** — `person`, `preference`, `fact`, `event`, `place`, `project`, `organisation`, `concept`, `skill`, `media`
- **Memory tool** — 7 sub-tools let the agent save, search, list, update, delete, **link**, and **explore** memories through natural conversation
- **Link memories** — the agent can create relationships between any two entities, building a richer graph over time
- **Explore connections** — the agent can traverse the graph outward from any entity, discovering chains of relationships for broad questions like family, work, and projects
- **Interactive memory visualization** — a dedicated **Knowledge** surface renders the entire knowledge graph as an interactive network diagram with search, filters, full-graph / ego-graph toggle, and detail cards
- **Graph-enhanced auto-recall** — before every response, the agent retrieves semantically relevant entities via FAISS and then expands one hop in the graph to surface connected neighbors; recalled memories include their relationship context
- **Automatic memory extraction** — a background process scans past conversations on startup and every 6 hours, extracting entities and relations the agent missed during live conversation; active threads and workflow threads are excluded; assistant messages are truncated to 200 chars to prevent extracting from AI-generated content; an 0.80 confidence floor rejects low-confidence entities
- **Deterministic deduplication** — both live saves and background extraction check for existing entities by normalized subject before creating new entries; cross-category matching prevents fragmentation; alias resolution ensures related names merge; richer content is always kept
- **Vague-type banning** — `related_to`, `associated_with`, `connected_to`, `linked_to`, `has_relation`, `involves`, and `correlates_with` are rejected before saving, preventing noisy low-value edges
- **Relation pre-normalization** — alias forms are canonicalized before ban, confidence, and dedup checks
- **67 valid relation types** — curated vocabulary with 60+ alias mappings plus document-specific relations like `extracted_from`, `uploaded`, `builds_on`, `cites`, `extends`, and `contradicts`
- **Source tracking** — each entity is tagged with its origin (`live`, `extraction`, `dream_*`, or document-derived) for diagnostics
- **Semantic recall** — FAISS vector index with Qwen3-Embedding-0.6B for similarity-based memory retrieval
- **Memory IDs in context** — auto-recalled memories include their IDs so the agent can update or delete specific entries when the user corrects previously saved information
- **Consolidation utilities** — built-in duplicate consolidation merges near-duplicate memories that may accumulate over time
- **Local SQLite + NetworkX + FAISS storage** — entities and relations live in `~/.thoth/memory.db`, mirrored in a NetworkX graph for traversal, with FAISS vectors in `~/.thoth/memory_vectors/`
- **Settings UI** — browse, search, visualize, and bulk-delete memories from the Knowledge tab in Settings, including graph statistics

---

## Wiki Vault

The knowledge graph can be exported as a structured **Obsidian-compatible markdown vault** — one `.md` file per entity with YAML frontmatter, `[[wiki-links]]`, and auto-generated indexes.

- **Vault structure** — entities grouped by type (`wiki/person/`, `wiki/project/`, `wiki/event/`, etc.) with one `.md` file per entity; sparse entities (<20 chars) roll up into `_index.md` per type; per-type indexes and a master `index.md` are auto-generated on rebuild
- **YAML frontmatter** — each article includes `id`, `type`, `subject`, `aliases`, `tags`, `source`, `created`, and `updated` metadata
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
- **Phase 5: Insights analysis** — the system captures a snapshot of recent logs, config, channels, designer activity, memory stats, and existing insights, feeds it to `DREAM_INSIGHTS_PROMPT`, and stores actionable results in `insights.py`
- **Three-layer anti-contamination** — sentence-level excerpt filtering, deterministic post-enrichment validation, and strengthened prompting prevent cross-entity fact bleed
- **Ollama busy check** — queries `/api/ps` before starting; defers if Ollama is actively serving a user request to avoid competing for GPU or CPU
- **Configurable window** — default 1–5 AM local time; checks every 30 minutes if enabled, idle, in window, and not yet run that day
- **Dream journal** — all operations logged to `~/.thoth/dream_journal.json` with cycle ID, summary, duration, merges, enrichments, inferences, insights, and errors
- **Post-cycle rebuilds** — FAISS is rebuilt after the cycle, and the wiki vault is regenerated when enabled so downstream views stay in sync
- **Manual trigger** — a dedicated Dream button in the Knowledge surface can start the cycle immediately
- **Settings UI** — enable/disable toggle, quiet window controls, and last-run summary in the Knowledge tab

---

## Document Knowledge Extraction

Uploaded documents are processed through a three-phase **map-reduce LLM pipeline** that extracts structured knowledge into the graph with full source provenance.

- **Map phase** — each document is split into ~6K-char windows; each window is summarized to 3–5 sentences
- **Reduce phase** — window summaries are combined into a coherent 300–600 word article
- **Extract phase** — core entities and relations are pulled from the final article; extraction is capped at 12 entities per document to prevent over-extraction
- **Curated relation vocabulary** — 67 valid relation types with 60+ alias mappings remove unknown-type churn and keep document-derived edges consistent with live memory extraction
- **Hub entity** — the document itself is saved as a `media` entity; extracted entities link back via `extracted_from` for provenance tracking
- **Quality gates** — minimum description length, self-loop rejection, and vague-relation bans keep output usable
- **Cross-window dedup** — repeated entities across windows are merged before saving
- **Cross-source merge protection** — document-derived content uses a stricter semantic merge threshold when it resembles a personal entity, reducing the chance of impersonal text overwriting personal knowledge
- **Supported formats** — PDF, DOCX, TXT, Markdown, HTML, and EPUB
- **Live progress** — the status bar shows an extraction pill with phase, progress, queue depth, and a stop action
- **Background queue** — worker thread processes one document at a time
- **Per-document cleanup** — removing a document deletes vector-store entries and extracted graph content associated with its source tag

---

## Brain Model & Cloud Models

The brain model is Thoth's default LLM — the model used for conversations, memory extraction, dream analysis, and any thread or workflow without a specific override. It can be a local Ollama model or an opt-in cloud model.

Thoth is built and tested for local models first. Every feature supports local models, and that remains the priority. Local models already handle tool calling, multi-step reasoning, memory extraction, and long conversations well with a 14B+ model.

Cloud models are supported for users without a dedicated GPU, for frontier reasoning on demand, or for trying many providers without downloading large local weights. Thoth supports opt-in cloud models through **OpenAI** (direct API), **Anthropic** (Claude), **Google AI** (Gemini), **xAI** (Grok), and **OpenRouter** (many third-party models), all configured from the GUI.

- **Dynamic model switching** — change the brain model from Settings; choose from curated local models or any connected cloud model
- **Per-thread & per-workflow model override** — conversations and workflows can each run on a different model, with overrides persisted locally
- **Starred models** — favorite cloud models appear alongside local models in the inline picker for quick switching
- **Cost-efficient context management** — smart context trimming compresses older conversation turns and shrinks oversized tool outputs, reducing token usage and API costs for cloud models
- **Curated local models** — only tool-calling-capable local models are surfaced prominently
- **Tool-support validation** — unsupported local models are warned about and can be auto-reverted if they fail a live tool-call check
- **Download buttons** — local models not yet present show download actions with progress
- **Configurable context window** — local and cloud context caps can be set independently; actual model limits are still respected
- **Local & cloud indicators** — the UI clearly distinguishes downloaded local models, missing local models, and connected cloud models
- **Cloud vision detection** — cloud models with image capability are detected and reused by the Vision feature when available

---

## Voice Input & Text-to-Speech

- **Toggle-based voice** — simple manual toggle to start and stop listening, no wake word required
- **4-state pipeline** — stopped → listening → transcribing → muted, with explicit state transitions and mic gating during playback
- **Local speech-to-text** — transcription via faster-whisper (tiny/base/small/medium), CPU-only int8 quantization, no cloud APIs required
- **Voice-aware responses** — voice input is tagged so the agent knows you are speaking and can respond more conversationally
- **Neural TTS** — high-quality text-to-speech via Kokoro, fully offline
- **10 voice options** — US and British English, male and female variants
- **Streaming TTS** — responses are spoken sentence-by-sentence as they stream in
- **Mic gating** — microphone is automatically muted during TTS playback to prevent echo and feedback loops
- **Hands-free mode** — voice input plus streaming TTS creates a fully conversational loop

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
- **History persistence** — shell history is saved per thread in `~/.thoth/shell_history.json`

---

## Browser Automation

- **Full browser automation** — the agent can navigate websites, click elements, fill forms, scroll pages, and manage tabs in a real, visible Chromium window
- **Shared visible browser** — runs with `headless=False` so you can see what the agent is doing and intervene when needed
- **Persistent profile** — cookies, logins, and local storage survive across restarts in `~/.thoth/browser_profile/`
- **Accessibility-tree snapshots** — after every action the tool captures the page's accessibility tree with numbered references so the model can click and type by number
- **Smart snapshot filtering** — deduplicates links, drops hidden elements, and caps interactive elements to keep context under control
- **Snapshot compression** — older browser snapshots are compressed to short stubs while the latest state remains detailed
- **7 browser operations** — navigate, click, type, scroll, snapshot, back, and tab management
- **Per-thread tab isolation** — each chat thread or background workflow gets its own browser tab; tabs are cleaned up on thread deletion or workflow completion
- **Automatic browser detection** — prefers installed Chrome, then Edge on Windows, then Playwright's bundled Chromium
- **Crash recovery** — if the browser closes externally, the next action relaunches it cleanly

---

## Vision

- **Camera analysis** — capture and analyze images from your webcam in real-time
- **Screen capture** — take screenshots and ask questions about what is on your screen
- **Image file analysis** — analyze workspace image files by path without needing a camera or live capture
- **Configurable vision model** — choose from local or cloud-capable vision models
- **Camera selection** — pick which camera to use when multiple devices are present
- **Inline image display** — captured and workspace images are shown inline in chat
- **Cloud vision support** — cloud models with image capability are auto-detected and work alongside local vision models

---

## Workflows & Scheduling

Tasks have been renamed to **Workflows** throughout the application. The workflow engine adds a step-based pipeline runner, delivery routing, approvals, triggers, and safety gating on top of APScheduler.

### Core Engine

- **Unified workflow engine** — named multi-step workflows run sequentially in a fresh or persistent thread and are scheduled through APScheduler
- **7 schedule types** — `daily`, `weekly`, `weekdays`, `weekends`, `interval`, `cron`, and one-shot `delay_minutes`
- **Template variables** — prompts can use `{{date}}`, `{{day}}`, `{{time}}`, `{{month}}`, `{{year}}`, `{{task_id}}`, and `{{step.X.output}}`
- **Per-workflow model override** — each workflow can force a different model, then restore the default after completion
- **Skills and tools overrides** — workflows can narrow the skill set globally and the tool set per step
- **Channel delivery** — workflow output can be delivered to any registered channel via `delivery_channel` and `delivery_target`
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

### Workflow Builder UI

- **Simple/Advanced toggle** — simple mode preserves a single-prompt workflow editor; advanced mode exposes the full step builder
- **Step builder** — reorder, delete, retarget, and retype steps visually
- **Variable insertion menu** — context variables and prior-step outputs can be inserted without hand typing placeholders
- **Flow preview** — Mermaid diagram generated from the step graph with manual refresh
- **Validation** — required-field checks, reference validation, and operator-specific rules run before save

### Approval System

- **Pending approvals panel** — approval cards show task name, request text, and Approve / Deny controls
- **Sidebar badge** — pending approvals surface as a badge and quick actions above the thread list
- **Multi-channel routing** — approvals can be routed through Telegram and desktop notifications with inline action controls
- **Resume integration** — the agent and workflow runtime resume correctly on approve or deny and follow the appropriate branch

### Workflow Console

- **Right-side console** — `ui/command_center.py` exposes running work, approvals, upcoming runs, quick launch actions, recent history, and insights in one drawer
- **Live operational view** — running workflows, background states, and recent outcomes stay visible while you continue chatting elsewhere in the app
- **Insight actions** — insight cards support pin, dismiss, and apply actions directly from the console
- **Journal access** — extraction and dream journals are accessible from the same workflow-centric monitoring surfaces

### Existing Features

- **Always-background execution** — workflows run without blocking the main chat UI
- **Pre-built templates** — ships with starter workflows like daily briefings, research summaries, and reminders
- **Home screen dashboard** — Workflows and Activity tabs show tiles, upcoming runs, run history, channel status, pending approvals, extraction journal, and dream journal
- **Persistent run history** — execution history survives workflow deletion for auditability
- **Monitoring / polling** — interval schedules plus condition steps support ongoing monitors like price checks or release watchers
- **Stop / cancel support** — running workflows can be stopped from the chat header, activity panel, or workflow card

---

## Designer Studio

Designer Studio is Thoth's dedicated visual-authoring subsystem. It spans five distinct **project modes**, a sandboxed interactive runtime, an authoring guardrail stack, and a mutation-reviewable tool surface for editing projects turn over turn.

### Project Modes

Every project is created in one of five modes. Each mode carries its own canvas presets, template gallery, prompt budgets, critique rules, runtime behavior, and export targets.

- **`deck`** — traditional slide decks; 16:9 canvas; ≤5 bullets per slide; PPTX export via `python-pptx` preserves editable text runs, images, and charts
- **`document`** — long-form report / one-pager pages; A4 or letter canvas; 130–160 words per block; PDF export is the primary delivery format
- **`landing`** — interactive marketing landing pages; vertical scroll canvas; CTAs and multi-section hero / feature / pricing layouts; published as interactive HTML
- **`app_mockup`** — multi-screen app prototypes; route-aware navigator so the agent can define screens and declarative navigation between them; runtime bridge turns link / button clicks into in-preview route changes
- **`storyboard`** — motion / ad storyboards; limited to 3–4 blocks per frame to avoid cropping; pairs naturally with the video generation tool for per-frame motion references

### Interactive Runtime

Interactive modes (`landing`, `app_mockup`, `storyboard`) do **not** allow free-form `<script>` from the agent. Behavior is expressed declaratively via `data-thoth-action` attributes and interpreted at runtime by a sandboxed bridge.

- **`designer/runtime/` package** — loads per-project runtime state, resolves route / screen navigation, handles state toggles, controls media playback, and dispatches declarative actions to real DOM operations inside the preview iframe
- **Declarative action grammar** — `data-thoth-action="navigate:screen-id"`, `data-thoth-action="toggle:state-key"`, `data-thoth-action="play:asset-id"`, etc. — the agent authors intent, the runtime executes it safely
- **Shared preview + publish runtime** — the same runtime powers editor preview, presenter mode, and published share links so interactive projects behave identically in all three surfaces

### Project Model & Storage

- **Multi-page / multi-screen projects** — each project stores a page list, canvas dimensions, aspect ratio, mode, title metadata, notes, brand settings, and (for app mockups) a route map
- **Home gallery** — the Home screen includes a dedicated **Designer** tab with recent projects, new-project flows, and quick reopen actions
- **Canvas presets and resizing** — projects can be resized after creation; mode-appropriate presets are offered up front
- **Reference storage** — uploaded briefs, screenshots, and source material are stored as reusable references so future designer sessions can reopen them without reuploading
- **Asset-backed media** — project HTML stores media as `asset://<asset-id>` references rather than brittle placeholder tokens; `designer/render_assets.py` normalizes legacy refs, preserves `data-asset-id`, and hydrates assets for preview, presentation, export, and published output
- **Persistent asset storage** — designer assets live on disk under `~/.thoth/designer/assets/`; projects and references are stored separately
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

## Thoth Status & Identity

Thoth now has a formal self-inspection and self-management surface: a tool for querying its own state, a controlled settings mutation API, and a Preferences UI for identity and self-improvement.

### Status Queries

- **`thoth_status` tool** — read-only introspection across `overview`, `version`, `model`, `channels`, `memory`, `skills`, `tools`, `api_keys`, `identity`, `tasks`, `vision`, `image_gen`, `voice`, `config`, `logs`, `errors`, and `designer`
- **Live runtime visibility** — the tool can report current model/provider, active channels, knowledge graph counts, enabled skills, configured APIs, task state, voice and image settings, and designer project counts
- **Diagnostics access** — recent warnings, errors, and tracebacks can be summarized without opening log files manually

### Controlled Self-Management

- **`thoth_update_setting`** — approved mutations for model switching, assistant name, personality, context caps, dream-cycle controls, skill toggles, tool toggles, image-generation model, manual dream-cycle trigger, and self-improvement toggle
- **Interrupt-gated writes** — all state-changing operations route through explicit user confirmation before they are applied
- **Optional self-improvement toolchain** — when self-improvement is enabled, `thoth_create_skill` and `thoth_patch_skill` become available
- **Skill patch safety** — bundled skills are patched via user-space overrides, not in-place mutation; old versions are backed up under `~/.thoth/skill_versions/`

### Identity & Preferences

- **`identity.py`** — stores assistant name, personality text, and self-improvement flag; sanitizes personality input before save
- **Preferences tab** — Settings exposes name, personality, preview, and self-improvement controls in one place
- **Prompt integration** — the same identity settings are consumed by `self_knowledge.py` so the opening line seen by the model matches what the user configured
- **Parallel UI surface** — the status monitor panel provides a visual health view for the user, while `thoth_status` exposes the same class of state to the agent

---

## Self-Knowledge & Insights

Thoth now carries an explicit self-description into prompts and uses Dream Cycle to turn recent activity into structured insight objects.

### Prompt-Time Self-Knowledge

- **Feature manifest** — `FEATURE_MANIFEST` in `self_knowledge.py` is the canonical inventory of major capabilities used when Thoth explains what it can do
- **Dynamic identity line** — `build_identity_line()` combines the configured assistant name and personality into the opening identity sentence
- **Dynamic state block** — `build_self_knowledge_block()` appends live state like current model, configured providers, entity count, last dream summary, active channels, designer project count, and enabled skills
- **Prompt injection** — the self-knowledge block is added alongside tool, memory, and citation guidance so the model can talk about itself accurately without outdated copy in `prompts.py`

### Insight Generation & Triage

- **Dream snapshot analysis** — Dream Cycle phase 5 captures logs, config, usage signals, and active insights, then runs `DREAM_INSIGHTS_PROMPT`
- **Structured insight store** — `insights.py` persists categorized insights like `error_pattern`, `skill_proposal`, `tool_config`, `knowledge_quality`, `usage_pattern`, and `system_health`
- **Dedup and pruning** — similar titles and semantically overlapping insights are merged; stale insights are auto-pruned; last-analysis time is tracked
- **Pin / dismiss / apply actions** — the Workflow Console exposes user actions for curating the insight list without leaving the app
- **Skill proposals** — insight objects can carry draft skill metadata, which pairs naturally with the self-improvement toolchain when enabled

---

## Messaging Channels

Thoth uses a generic **Channel** abstraction. Any messaging platform can plug in by subclassing the base adapter, declaring capabilities, and registering itself. The system then auto-generates tools, settings UI, monitoring, and approval routing around that channel.

### Channel Architecture

- **`Channel` ABC** — adapters implement lifecycle methods (`start`, `stop`, `is_configured`, `is_running`) plus outbound send methods for text, photos, documents, and approval requests
- **`ChannelCapabilities`** — declarative feature flags describe what each channel supports: photos, documents, voice, buttons, streaming, typing, reactions, and commands
- **Config schema** — each channel declares config fields so Settings can render the right form dynamically
- **Channel registry** — adapters self-register; runtime helpers expose all channels, running channels, configured channels, and delivery routing
- **Shared media pipeline** — inbound audio transcription, image analysis, document extraction, inbox persistence, and workspace copy helpers are centralized in `channels/media.py`
- **Shared utilities** — auth, command handling, approval routing, media capture, and corrupt-thread repair live in reusable channel modules
- **Tool factory** — running channels contribute auto-generated send/photo/document tools through `channels/tool_factory.py`
- **Activity tracking** — per-channel last-activity timestamps drive the sidebar monitor and status surfaces

### Bundled Channels

- **Telegram** — full agent access, streaming edits, voice transcription, photo analysis, document extraction, emoji reactions, inline approval buttons, `/model` support, and HTML-safe formatting
- **WhatsApp** — Baileys bridge with QR pairing, inbound/outbound media, rich YouTube previews, Markdown-to-WhatsApp formatting, and streaming-style updates
- **Discord** — DM-based adapter with reactions, typing indicators, slash-command integration, and media support
- **Slack** — Socket Mode adapter with DM threading, message updates, reactions, typing indicators, and file uploads
- **SMS** — Twilio adapter with inbound webhook support, outbound SMS/MMS, and tunnel-manager integration for public callbacks

### Delivery & Monitoring

- **Auto-generated channel tools** — when a channel is running, the agent gains send/photo/document tools for that channel automatically
- **Approval routing** — approvals can be sent through supported channels with inline action controls
- **Sidebar channel monitor** — the conversation sidebar shows live status dots, icons, display names, and relative last-activity timestamps
- **Auto-start and config persistence** — channel enablement and settings persist to `~/.thoth/channels_config.json`

---

## Tunnel Manager

A provider-agnostic tunnel layer exposes local webhook ports to the internet when a channel needs inbound delivery.

- **Provider abstraction** — `TunnelProvider` defines the backend contract; `NgrokProvider` is the current implementation
- **`TunnelManager` singleton** — manages tunnel lifecycle, per-port allocation, cleanup, and status reporting
- **Automatic use by channels** — channels that need a public callback request a tunnel on start and release it on shutdown
- **Optional app tunneling** — the main Thoth UI can also be exposed intentionally for remote access
- **Settings UI** — tunnel provider, auth token, and active-tunnel status live in the Channels settings surface
- **Health checks** — tunnel status participates in the status monitor and diagnostics flows

---

## X (Twitter) Tool

Thoth integrates with X API v2 through a native httpx-based client, grouped into three high-level tool entry points.

- **3 grouped LangChain tools** — `x_read`, `x_post`, and `x_engage`
- **Read operations** — search, read tweet, timeline, mentions, and user info
- **Post operations** — post tweet, reply, quote, and delete tweet
- **Engagement operations** — like, unlike, repost, unrepost, bookmark, and unbookmark
- **OAuth 2.0 PKCE** — browser-based auth flow with a local callback server and refresh-token support
- **Rate-limit tracking** — per-endpoint rate information is recorded and surfaced in structured error responses
- **Tier discovery** — X tier information is persisted and reused for rate-limit expectations
- **Local token storage** — auth state lives in `~/.thoth/x/`
- **Settings UI** — connect, disconnect, and inspect X auth from Accounts settings

---

## Tool Guides

Tool guides are lightweight `SKILL.md` packages that attach contextual instructions to tools without hard-coding those instructions into the main system prompt.

- **Skill-like format** — each guide is a directory with a `SKILL.md` file and YAML frontmatter, just like a manual skill
- **`tools:` activation field** — guides declare the tools they apply to; when any linked tool is in the active tool belt, the guide is injected automatically
- **Prompt injection** — `prompts.py` discovers active guides and appends them to the system prompt at runtime
- **Invisible to the manual skill toggles** — tool guides are auto-managed and do not clutter the user-facing skill list
- **18 bundled guides** — Browser, Calendar, Chart, Designer, Email, Filesystem, Math, MCP, Shell, Telegram, Thoth Status, Tracker, Updater, Video, Vision, Weather, Wiki, and X
- **Consistency benefits** — guide content can evolve independently of the main prompt, reducing drift and duplicated instructions

---

## Image Generation

Thoth can generate and edit images through multiple cloud providers, render them inline, persist them to disk, and reuse them in designer workflows or channel delivery.

- **Provider support** — OpenAI image models, xAI Grok Imagine, Google Imagen 4, and Gemini image-capable models
- **Generate and edit flows** — prompts can generate a new image or edit the most recent image, an attached image, or an on-disk file
- **Inline rendering** — generated images are surfaced directly in the chat stream without requiring a separate viewer
- **Per-thread persistence** — generated images are saved into Thoth's media storage so they survive refreshes and can be referenced later
- **Channel delivery** — running messaging channels can pick up generated images and send them as photos
- **Designer reuse** — Designer Studio can invoke the same provider layer for slide assets and visual content generation
- **Settings selector** — the active image-generation model is configurable from Settings and queryable through `thoth_status`

---

## Video Generation

Thoth can generate short video clips from text prompts or reference images through Google Veo and xAI Grok Imagine Video for chat use and Designer storyboard workflows.

- **`video_gen_tool`** — top-level agent tool for text-to-video and image-to-video generation
- **Provider support** — Google Veo handles text-to-video and image-to-video with provider-side person-generation policy handling; xAI Grok Imagine Video supports text-to-video and image-to-video with provider-specific aspect ratio, duration, and resolution constraints
- **Inline rendering** — generated clips are surfaced directly in the chat stream with safe media-element hydration
- **Designer integration** — Designer Studio storyboards and landing hero slots can reference generated videos as `asset://` media; motion clips are rendered in preview, presenter mode, and published share links
- **Persistent asset storage** — generated clips are saved to Thoth's media storage so they survive thread refreshes and can be reused across designer projects
- **Channel delivery** — running messaging channels can pick up generated videos and deliver them where supported

---

## MCP Client & External Tools

Thoth includes a guarded Model Context Protocol client that can connect external MCP servers and expose their tools to the ReAct agent without making external servers part of Thoth's trusted core.

### Runtime Model

- **Dedicated package** — `mcp_client/` owns persistent config, marketplace search, dependency checks, safety classification, runtime sessions, logging, result normalization, and curated starter metadata
- **Separate config file** — MCP state is stored in `~/.thoth/mcp_servers.json`, separate from native tool toggles, so malformed or broken MCP config falls back to an empty disabled config instead of damaging normal tool settings
- **Global enable switch** — `enabled` in MCP config is the top-level kill switch. Turning it off stops active sessions, clears the discovered catalog, removes dynamic MCP tools from the agent, and keeps saved server definitions for later
- **Per-server runtime** — each enabled server gets its own `McpServerRuntime` session tracked by status (`connecting`, `connected`, `failed`, `dependency_missing`, `stopped`, `global_disabled`), tool counts, timestamps, transport, and last error
- **Transport support** — stdio, Streamable HTTP, and SSE are supported through the Python MCP SDK. Each server can set command, args, cwd, env, URL, headers, connect timeout, tool timeout, and output limit
- **Non-blocking startup** — `app.py` discovers enabled servers during startup in a guarded path. Exceptions are logged as warnings and do not stop Thoth from launching
- **Shutdown cleanup** — app shutdown calls MCP runtime shutdown to close child sessions and stop external stdio processes

### Dynamic Tool Injection

- **Parent registry tool** — `tools/mcp_tool.py` registers `mcp` / **External MCP Tools** as the native parent tool. It is the stable toggle users see in Settings and Thoth Status
- **Dynamic wrappers** — discovered enabled MCP tools are converted into LangChain `StructuredTool` instances at agent build time, with names generated as `mcp_<server>_<tool>`
- **Schema conversion** — JSON input schemas are converted into Pydantic argument models where possible, with a permissive fallback for complex or invalid schemas
- **Resources and prompts** — servers can optionally expose `list_resources`, `read_resource`, `list_prompts`, and `get_prompt` utility tools through per-server advanced toggles
- **Readable display names** — `agent.py` resolves tool-call UI labels back to the original MCP tool and server name, for example `MCP: microsoft_docs_search (microsoft-learn-mcp)`
- **Result normalization** — MCP text, structured content, resource links, embedded resources, binary/image blocks, empty results, errors, and oversized outputs are normalized before being sent back into the model

### Safety & Trust Boundaries

- **External output is untrusted** — the MCP tool guide tells the agent not to follow instructions found inside MCP results unless they are clearly part of the user's request
- **Native tools stay preferred** — Thoth Memory, Browser, filesystem, document, search, channel, and Designer capabilities remain canonical for Thoth-owned behavior; overlapping MCP servers are treated as external alternatives
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
- **Diagnostics dialog** — Settings can display masked MCP config and live status summary for support/debugging without requiring file edits

### Runtime Requirements

- **Requirement inference** — stdio launch commands infer runtime requirements for `npx`/Node.js, `uvx`/uv, Docker, and Playwright MCP browser dependencies
- **Managed user-space installs** — Thoth can install private Node.js LTS, uv, and Playwright Chromium runtimes under `~/.thoth/runtimes/` and inject those paths only into MCP child process environments
- **Manual complex dependencies** — Docker and other heavyweight system dependencies are surfaced as manual setup requirements with setup links instead of being bundled into Thoth
- **No bundled MCP runtimes** — Thoth depends on the Python MCP SDK, but external server runtimes are resolved at runtime so the app package does not ship Node, uv, Docker, or browser payloads unnecessarily

### Testing & Release Checks

- **Offline regression suite** — `test_mcp_client.py` covers config fallback, secret masking, safety classification, marketplace fallback/filtering, conflict policy, runtime requirement handling, managed environment injection, settings rows, stdio discovery/call, global disable, bad server failure, display names, background safety, and browser-loop handling
- **Opt-in live E2E** — `scripts/mcp_real_world_e2e.py` and `test_mcp_real_world_e2e.py` connect to public MCP servers outside normal CI to validate import, probe, manual tool enablement, dynamic wrapper invocation, and read-only approval classification
- **Maintainer workflow** — MCP-heavy releases run the offline suite first, then the live public E2E check from the repo root

---

## Migration Wizard

Thoth includes a one-time migration wizard for moving selected data from Hermes Agent or OpenClaw into a Thoth data directory without treating legacy state as trusted runtime configuration.

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

- **Explicit apply** — only selected planned/sensitive items are applied. Archive-only items are copied to the report archive, not activated in the live Thoth target
- **Backups first** — existing target files are backed up before overwrite/append/update. Multiple writes to the same target preserve the pre-migration original once per run, and newly created files are not backed up later in the same run
- **Redacted report** — each run writes redacted `plan.json`, `result.json`, `backup_manifest.json`, and `summary.md` under `migration-reports/<timestamp>/`
- **Archive redaction** — JSON and key/value archive snapshots are redacted before being copied into reports; binary or unsupported files are represented by a placeholder instead of raw content
- **MCP import safety** — migrated MCP servers are written disabled. They must be reviewed and enabled from Settings → MCP before any external tools become available to the agent
- **Credential import** — API keys and tokens are off by default and require explicit selection. Reports hide their values; selected keys route through target-profile secure storage via `api_keys.set_key_for_data_dir`, so normal imports use the OS credential store with metadata-only local files when keyring is available

### Testing

- **Focused suites** — `test_migration_core.py`, `test_migration_detection.py`, `test_migration_planner.py`, `test_migration_apply.py`, and `test_migration_wizard_ui.py` cover model invariants, source detection, dry-run planning, wrong-provider rejection, conflict behavior, backups, reports, redaction, daily memory import, and UI helper logic
- **Realistic fixtures** — `migration/fixtures.py` builds multi-month Hermes and OpenClaw homes with fake secrets, memories, skills, channels, MCP servers, approvals, cron/hooks, plugins, sessions, logs, and archive-only state
- **Manual E2E path** — disposable targets under `.tmp/migration-fixtures/` are used for click-through validation before release; the fixture root is ignored by git

---

## Plugin System & Marketplace

A sandboxed, hot-reloadable extension system lets plugins add new tools and skills without modifying the core codebase.

### Plugin Architecture

- **Plugin API** — `PluginAPI` and `PluginTool` are the core abstractions available to plugins
- **Manifest system** — each plugin declares metadata, tools, skills, settings, and dependencies in `plugin.json`
- **Security sandbox** — static scans block dangerous constructs like `eval`, `exec`, and shell escape paths; imports from sensitive core modules are restricted
- **Dependency safety** — plugin dependency installs cannot silently downgrade core packages required by Thoth
- **State persistence** — enablement and non-secret config are stored under `~/.thoth/plugin_state.json`; plugin API-key secrets use the OS credential store with metadata-only `plugin_secrets.json` state and session-only fallback for new saves when keyring is unavailable
- **Hot reload** — Settings can reload plugins without restarting the app; agent caches are cleared automatically
- **Skill auto-discovery** — plugin `skills/` directories are scanned for `SKILL.md` definitions and injected like built-in skills

### Marketplace

- **Marketplace index** — remote plugin catalog fetched from GitHub-hosted JSON with caching and update checks
- **Browse dialog** — search, inspect, and install plugins from within the app
- **Install / update / uninstall** — plugin archives are validated before install and reloaded immediately afterward
- **Per-plugin settings UI** — each installed plugin gets config controls, secret inputs, and enable/disable toggles in Settings

---

## Auto-Updates

`updater.py` polls the GitHub Releases API for the official Thoth repo on a background thread (30-second startup delay, then every 6 hours, with a 24-hour debounce on actual network calls). Checking is on by default; if there is no internet the call fails silently and the next tick retries.

- **Channel** — `stable` (default) hits `/releases/latest`; `beta` walks the top 10 releases and includes pre-releases. Persisted in `~/.thoth/update_config.json`.
- **Manifest verification** — every release body must contain a fenced `<!-- thoth-update-manifest -->` block with SHA256 hashes for each platform asset. Without a manifest entry, `download_update` refuses to install. The CI workflow `.github/workflows/update-manifest.yml` calls `scripts/append_sha_manifest.py` to PATCH the release body once artifacts are uploaded.
- **OS code signature** — Windows installs invoke `signtool.exe verify /pa`; macOS installs invoke `codesign --verify --deep --strict`. Failures abort the install with a visible error.
- **Hand-off** — Windows: `ThothSetup_x.y.z.exe /SILENT /CLOSEAPPLICATIONS /RESTARTAPPLICATIONS`. The `.iss` was extended with `CloseApplications=yes` / `RestartApplications=yes` so Inno Setup can swap files while Thoth is running. macOS: `open <dmg>` and exit; the user drags the new app to `/Applications`.
- **UI** — a green "⬆ vX.Y.Z" status-bar pill appears when a newer release is detected; clicking opens the What's-New dialog (rendered release notes plus Install / Skip / Later buttons). Settings → Preferences → Updates exposes channel selection, "Check for updates", and a list of skipped versions.
- **Agent surface** — `tools/updater_tool.py` registers `thoth_check_for_updates` (read-only) and `thoth_install_update` (interrupt-gated). The dynamic self-knowledge block surfaces "Update available: …" when applicable, and `thoth_status` adds an `updates` category.
- **Dev installs** — when a `.git/` directory sits next to the app source (i.e. running from a checkout), the scheduler is disabled and `thoth_install_update` refuses, so working copies are never overwritten.

---

## Habit & Health Tracker

- **Conversational tracking** — log medications, symptoms, exercise, mood, sleep, periods, and other recurring data in natural language
- **Auto-detect & confirm** — the agent recognizes likely trackable events and asks before writing anything
- **3 tracker operations** — structured logging, flexible querying, and destructive delete with confirmation
- **Built-in analysis** — adherence, streaks, numeric summaries, frequency, day-of-week patterns, cycle estimation, and co-occurrence analysis
- **Trend visualization** — tracker queries can export CSV and chain directly into the chart tool for Plotly output
- **Fully local** — tracker data lives in `~/.thoth/tracker/tracker.db`
- **Memory isolation** — tracker entries are intentionally excluded from the personal knowledge graph

---

## Desktop App

- **Native window** — runs in a desktop window via pywebview rather than depending on a browser tab
- **System tray** — `launcher.py` exposes open and quit controls plus running-state feedback
- **Splash screen** — Tk-based splash with console fallback during startup
- **First-launch setup wizard** — guides the user through Local or Cloud setup paths without touching config files
- **Self-contained installers** — Windows and macOS releases bundle dependencies for one-click setup
- **Auto-restart flow** — closing the native window does not kill the tray-managed app process; reopen is fast
- **Release pipeline** — build, sign, notarize, and publish automation lives in CI

---

## Chat & Conversations

- **Multi-turn threads** — conversation history is stored in SQLite via LangGraph checkpointing and local thread metadata
- **Auto-naming and switching** — threads are named from the conversation and can be reopened, exported, or deleted individually
- **Per-thread model override** — conversations can pin a different local or cloud model than the global default
- **File attachments** — drag-and-drop, clipboard paste, and standard upload flows handle images, PDFs, spreadsheets, JSON, and text
- **Media persistence** — chat media is stored per thread on disk with sidecar metadata; generated content persists more aggressively than transient capture artifacts
- **Inline rich rendering** — Plotly charts, Mermaid diagrams, YouTube embeds, syntax-highlighted code, and images render directly in the transcript
- **Shared chat components** — `ui/chat_components.py` provides the input bar, upload flow, and message container for both the main chat UI and Designer Studio
- **Status monitor panel** — avatar, health-check pills, diagnosis action, and quick settings links surface runtime health at a glance
- **Workflow Console integration** — approvals, recent runs, and insight actions are visible without leaving the conversation experience
- **Output truncation warnings** — the UI warns when a response was cut short by model token limits

---

## Notifications

- **Desktop notifications** — workflow completions, reminders, and other events can raise OS-level notifications
- **Sound effects** — distinct audio chimes are used for workflow completion and timer-like alerts
- **In-app toasts** — lightweight status notifications appear in the UI; errors can remain persistent until dismissed
- **Unified API** — all notification surfaces flow through a single `notify()` entry point
- **Approval awareness** — approval requests can surface as both UI notices and channel-delivered prompts

---

## Bundled Skills

Skills are reusable instruction packs that shape how the agent thinks and responds. Each skill is a `SKILL.md` file with YAML frontmatter (display name, icon, description, required tools, tags) and freeform instructions injected into the system prompt when enabled.

Thoth ships with **12 manual bundled skills** and **18 tool guides**. Manual skills are toggled from Settings; tool guides auto-activate when their linked tools are available.

| Skill | Description |
|-------|-------------|
| **🧠 Brain Dump** | Capture unstructured thoughts and organize them into structured notes saved to memory |
| **☀️ Daily Briefing** | Compile a morning briefing with weather, calendar, and news headlines |
| **📊 Data Analyst** | Analyze datasets, produce statistical summaries, and create insightful charts |
| **🔬 Deep Research** | Perform multi-source research on a topic and produce a structured report |
| **🎨 Design Creator** | Structured workflow for presentations, one-pagers, reports, and visual layouts in Designer Studio |
| **🗣️ Humanizer** | Write in a natural, human tone without AI-speak or corporate filler |
| **📚 Knowledge Base** | Manage the personal knowledge base across graph memories, document intelligence, and the wiki vault |
| **📋 Meeting Notes** | Turn raw notes into actionable minutes with follow-ups and clear structure |
| **🎯 Proactive Agent** | Anticipate user needs, ask clarifying questions, and self-check work at milestones |
| **🪞 Self-Reflection** | Review memory for contradictions, gaps, and stale information |
| **⚙️ Task Automation** | Design effective workflows with steps, conditions, approvals, triggers, and delivery routing |
| **🌐 Web Navigator** | Strategic patterns for browser automation, research, forms, and data extraction |

- **User skills** — custom skills live in `~/.thoth/skills/<name>/SKILL.md`; user skills with the same name as a bundled skill override it
- **In-app skill editor** — skills can be created and edited directly from Settings
- **Per-skill enablement** — only enabled manual skills are injected into the system prompt
- **Per-thread and per-workflow overrides** — skill selection can be narrowed for individual threads and workflows
- **Tool guides remain automatic** — Browser, Calendar, Chart, Designer, Email, Filesystem, Math, MCP, Shell, Telegram, Thoth Status, Tracker, Updater, Video, Vision, Weather, Wiki, and X guides are in the built-in set

---

## Core Modules

| File | Purpose |
|------|---------|
| **`app.py`** + **`ui/`** | NiceGUI application shell, chat surfaces, home tabs, status monitor, workflow console, settings dialog, and native-webview integration points |
| **`designer/`** | Designer Studio subsystem: gallery, editor, tooling, storage, exports, presentation mode, publishing, and asset hydration |
| **`ui/chat_components.py`** | Shared chat input, upload, and message-area components reused by main chat and Designer Studio |
| **`agent.py`** | LangGraph ReAct agent, prompt assembly, streaming event generation, tool routing, interrupt handling, cache clearing, and background execution integration |
| **`threads.py`** | SQLite-backed thread metadata, LangGraph checkpoint wiring, per-thread media storage, and thread-level overrides |
| **`memory.py`** | Backward-compatible memory wrapper that maps legacy memory calls onto the knowledge graph implementation |
| **`knowledge_graph.py`** | Entity/relation store, FAISS-backed recall, NetworkX traversal, deduplication, relation normalization, and graph stats |
| **`wiki_vault.py`** | Obsidian-compatible markdown vault export, indexing, search, and conversation export |
| **`dream_cycle.py`** | Nightly graph refinement engine: merges, enrichment, decay, relation inference, insights analysis, and journal logging |
| **`document_extraction.py`** | Background document map-reduce extraction pipeline with provenance-aware graph writes |
| **`models.py`** | Local and cloud model management, context caps, starred models, provider detection, and model factories |
| **`documents.py`** | Document ingestion, chunking, embedding, vector-store persistence, and per-document cleanup |
| **`voice.py`** | Faster-whisper-based speech input pipeline and voice-state management |
| **`tts.py`** | Kokoro text-to-speech integration, voice catalog, and streaming playback |
| **`vision.py`** | Camera capture, screen capture, and workspace image analysis via local or cloud vision models |
| **`data_reader.py`** | Shared structured-data loader for CSV, TSV, Excel, JSON, and JSONL |
| **`launcher.py`** | Desktop launcher, system tray, splash screen, app lifecycle, and logging bootstrap |
| **`api_keys.py`** + **`secret_store.py`** | API key storage and retrieval for tools and cloud providers, backed by OS keyring with metadata-only local files and legacy plaintext migration |
| **`identity.py`** | Assistant name, personality, and self-improvement preference storage with sanitization |
| **`self_knowledge.py`** | Capability manifest, identity-line builder, live runtime state builder, and prompt-time self-knowledge assembly |
| **`insights.py`** | Structured insight store with dedup, pruning, pin/dismiss/apply state, and last-analysis tracking |
| **`prompts.py`** | Centralized prompt templates including summarization, extraction, and dream-insights analysis |
| **`memory_extraction.py`** | Background conversation scan that extracts entities and relations the live agent did not save |
| **`skills.py`** | Discovery, loading, activation, override, and prompt-building for manual skills and tool guides |
| **`bundled_skills/`** | 12 built-in manual skills as `SKILL.md` packages |
| **`tool_guides/`** | 18 built-in tool-specific auto-activation guides |
| **`tasks.py`** | Workflow engine, SQLite persistence, APScheduler scheduling, pipeline execution, run history, safety mode, and delivery routing |
| **`notifications.py`** | Unified desktop, sound, and toast notification system |
| **`channels/`** | Channel ABC, registry, media helpers, auth helpers, approval routing, command handling, tool generation, and bundled channel adapters |
| **`tunnel.py`** | Tunnel provider abstraction, ngrok integration, and lifecycle manager |
| **`tools/thoth_status_tool.py`** | Self-introspection and controlled self-management tool, including optional self-improvement skill operations |
| **`tools/`** + **`designer/tool.py`** | Self-registering core tool modules, registry, base classes, and LangChain tool conversion |
| **`plugins/`** | Plugin runtime, marketplace client, manifest validation, security scanner, and settings integration |
| **`mcp_client/`** | External Model Context Protocol client: config, runtime sessions, marketplace search, requirements, safety classification, diagnostics, and result normalization |
| **`migration/`** | Hermes/OpenClaw migration models, redaction, source detection, dry-run planning, realistic fixtures, and guarded apply/report generation |
| **`static/`** | Bundled frontend assets such as Mermaid and graph/visualization helpers |
| **`version.py`** | Single source of truth for the current Thoth version |

---

## Data Storage

All user data is stored under `~/.thoth/` (or `%USERPROFILE%\\.thoth\\` on Windows) unless `THOTH_DATA_DIR` is set.

```text
~/.thoth/
├── threads.db                     # Conversation history and LangGraph checkpoints
├── media/                         # Per-thread media files and sidecar metadata
├── memory.db                      # Knowledge graph entities and relations
├── memory_vectors/                # FAISS vectors for semantic memory recall
├── memory_extraction_state.json   # Last extraction metadata
├── extraction_journal.json        # Memory extraction journal
├── dream_config.json              # Dream Cycle settings
├── dream_journal.json             # Dream Cycle run log
├── dream_rejections.json          # Rejected inference-pair cache
├── insights.json                  # Structured insight store
├── api_keys.json                  # API key metadata only; raw key values live in the OS credential store when available
├── cloud_config.json              # Starred cloud models and cloud settings
├── app_config.json                # Onboarding and first-run flags
├── user_config.json               # Avatar preferences, identity, and self-improvement settings
├── channels_config.json           # Channel enablement and per-channel config
├── shell_history.json             # Per-thread shell history
├── skills_config.json             # Manual skill enable/disable state
├── mcp_servers.json               # External MCP server config, global switch, tool enablement, approvals
├── mcp_marketplace_cache.json     # Cached MCP directory search results
├── migration-reports/             # Redacted migration plans, results, summaries, and archive snapshots
├── migration-backups/             # Pre-migration backups of overwritten target files
├── runtimes/                      # Optional user-space runtimes installed by MCP requirement helper
├── skill_versions/                # Skill patch backups for self-improvement flows
├── thoth_app.log                  # Structured application log
├── splash.log                     # Splash-screen diagnostics
├── inbox/                         # Files received via messaging channels
├── browser_profile/               # Persistent Chromium profile
├── browser_history.json           # Browser history and snapshots
├── designer/
│   ├── projects/                  # Designer project JSON files
│   ├── references/                # Designer source/reference uploads
│   ├── assets/                    # Persistent project assets
│   └── published/                 # Published HTML bundles and shareable output
├── tracker/
│   ├── tracker.db                 # Habit and health tracker database
│   └── exports/                   # CSV exports for tracker charts
├── vector_store/                  # Uploaded document vector index
├── gmail/                         # Gmail OAuth tokens
├── calendar/                      # Calendar OAuth tokens
├── wiki/                          # Obsidian-compatible markdown vault export
├── x/                             # X OAuth tokens and tier metadata
├── installed_plugins/             # Marketplace-installed plugins
├── plugin_state.json              # Plugin config and enablement state
├── plugin_secrets.json            # Plugin API-key metadata only; raw key values live in the OS credential store when available
└── kokoro/                        # Kokoro TTS model and voice files
```

> Override the data directory by setting the `THOTH_DATA_DIR` environment variable.

---

## Comparison with Other Tools

### Why not just use another open-source assistant?

Most open-source AI assistants are still **developer tools disguised as products** — CLI-first, config-file-driven, and built around Docker, YAML, and environment variables. Getting started often means cloning repos, editing configs, wiring databases, and debugging dependencies before you can ask a single useful question.

**Thoth is different.** It is packaged as a native desktop experience with one-click installers for Windows and macOS, local-first defaults, and a GUI that exposes models, tools, workflows, channels, Designer Studio, and memory without requiring terminal fluency.

### Why not just use ChatGPT?

| | ChatGPT / Claude / Gemini | Thoth |
|---|---|---|
| **Your data** | Stored on provider servers, subject to their privacy policies | Stays on your machine. With opt-in cloud models, only the current conversation goes to the provider; memories, files, designer projects, and history remain local |
| **Conversations** | Provider-owned chat history | Local SQLite-backed threads, exportable anytime |
| **Cost** | Subscription or provider billing | Free with local models; cloud usage is pay-per-token only when you opt in |
| **Memory** | Limited, opaque, provider-controlled | Personal knowledge graph with entities, relations, visualization, wiki export, and background refinement |
| **Tools** | Limited app integrations and provider-defined plug-ins | 30 core tools plus auto-generated channel tools: shell, browser, filesystem, Gmail, Calendar, memory graph, Designer Studio, Thoth Status, MCP external tools, image generation, video generation, research tools, and more |
| **Customization** | Pick a model and maybe a custom instruction | Swap models per thread or workflow, configure name and personality, build workflows, toggle tools and skills, and enable self-improvement features |
| **Voice** | Usually cloud-processed | Local faster-whisper STT plus Kokoro TTS |
| **Availability** | Internet required | Local models work offline; cloud models are optional |

> **Bottom line:** cloud assistants rent you access to someone else's system. Thoth gives you **personal AI sovereignty** — local-first by default, cloud when you choose it, and all of your durable data under your own control.

### How is Thoth different from OpenClaw?

[OpenClaw](https://github.com/openclaw/openclaw) is a strong open-source personal assistant aimed at multi-channel delivery and developer-centric workflows. The two projects overlap in ambition but optimize for different users.

| | Thoth | OpenClaw |
|---|---|---|
| **Getting started** | One-click installers and GUI-first setup on Windows and macOS | CLI-oriented install flow and heavier terminal expectations |
| **Local AI** | Local-first with Ollama as the default path | More cloud-first in typical setups |
| **Memory** | Typed personal knowledge graph with visualization, wiki export, and structured relations | Simpler text-centric memory patterns |
| **Knowledge refinement** | 5-phase Dream Cycle with merge, enrich, decay, infer, and insight passes | Experimental dreaming-style memory promotion flows |
| **Document intelligence** | Structured graph extraction with provenance, dedup, and relation typing | Strong workspace tools but less graph-centric document knowledge modeling |
| **Designer / Canvas** | Designer Studio for decks, one-pagers, reports, published links, plus inline Mermaid and Plotly rendering | A2UI-style interactive workspace focus |
| **Tools** | 30 core tools plus auto-generated channel send tools, including Designer Studio, Thoth Status, and MCP external tools | Broad built-in toolset with different emphasis |
| **Messaging channels** | 5 bundled channels with streaming, media handling, approvals, and a sidebar monitor | Wider channel catalog and gateway focus |
| **Autonomous workflows** | Step-based workflows with approvals, conditions, triggers, concurrency groups, and safety modes | Strong channel routing and automation, different orchestration model |
| **Desktop experience** | Native Windows and macOS desktop app with tray, splash, and setup wizard | More developer-first and channel-first in practice |
| **Privacy posture** | All durable state local; no Thoth servers | Self-hostable and privacy-conscious, but with a different operational model |

> **In short:** OpenClaw is an excellent multi-channel gateway for developer-heavy setups. Thoth is optimized for **personal AI sovereignty** — local-first memory, structured knowledge, integrated design tools, configurable self-knowledge, and a native desktop experience that does not require living in a terminal.
