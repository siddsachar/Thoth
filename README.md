<p align="center">
  <img src="docs/thoth_glyph_256.png" alt="Thoth" width="180">
</p>

<h1 align="center">𓁟 Thoth — Personal AI Sovereignty</h1>

<p align="center">
  <a href="https://github.com/siddsachar/Thoth/releases"><img src="https://img.shields.io/github/v/release/siddsachar/Thoth?style=flat&label=release&color=c9a227" alt="Release"></a>
   <a href="https://github.com/siddsachar/Thoth/actions/workflows/ci.yml"><img src="https://github.com/siddsachar/Thoth/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/siddsachar/Thoth?style=flat" alt="License"></a>
  <img src="https://img.shields.io/badge/platform-Windows%20%7C%20macOS-c9a227?style=flat" alt="Platform">
  <img src="https://img.shields.io/badge/tests-All%20Pass-brightgreen?style=flat" alt="Tests">
</p>

Thoth is a **local-first AI assistant built for personal AI sovereignty** — your models, your data, your rules. It combines a powerful ReAct agent with **28 core tools plus auto-generated channel send tools** — web search, email, calendar, file management, shell access, browser automation, vision, image generation, video generation, X (Twitter), long-term memory with a personal knowledge graph, **Designer Studio** (five modes: decks, documents, landing pages, app mockups, and motion storyboards), **advanced workflows** with conditional branching and approval gates, **Thoth Status** self-inspection, configurable identity, an insights engine, habit tracking, and more — plus a **plugin system** with a built-in marketplace and **5 messaging channels** (Telegram, WhatsApp, Discord, Slack, SMS) with full media support, streaming, reactions, and approval routing. Run everything locally via [Ollama](https://ollama.com/), or add opt-in cloud models from **OpenAI**, **Anthropic** (Claude), **Google AI** (Gemini), **xAI** (Grok), and **OpenRouter** (100+ models) when you need frontier reasoning or don't have a GPU. Either way, your data — conversations, memories, documents, designer projects, and history — stays on your machine.

> **Local models are already amazing.** You'll be surprised what a 14B+ local model can do. If you start with cloud models today, and as local models get smarter and hardware gets cheaper, transition to fully local, fully private, fully free AI — seamlessly, with no changes to your setup.

> Governments are investing billions to keep AI infrastructure within their borders. Thoth applies the same principle to the individual — your compute, your data, your choice of model, **accountable to no one but you.**

> **🖥️ One-click install on Windows & macOS** — download, run, done. No terminal, no Docker, no config files. [Get it here.](https://github.com/siddsachar/Thoth/releases)

<table align="center">
  <tr>
    <td align="center"><a href="https://youtu.be/ansfEqAr6g0"><img src="https://img.youtube.com/vi/ansfEqAr6g0/maxresdefault.jpg" width="360" alt="Demo 1"></a></td>
    <td align="center"><a href="https://youtu.be/61JFO0ElgBE"><img src="https://img.youtube.com/vi/61JFO0ElgBE/maxresdefault.jpg" width="360" alt="Demo 2"></a></td>
  </tr>
  <tr>
    <td align="center"><a href="https://youtu.be/dMoSay7uyoc"><img src="https://img.youtube.com/vi/dMoSay7uyoc/maxresdefault.jpg" width="360" alt="Demo 3"></a></td>
    <td align="center"><a href="https://youtu.be/xYJC2IVKH7Y"><img src="https://img.youtube.com/vi/xYJC2IVKH7Y/maxresdefault.jpg" width="360" alt="Demo 4"></a></td>
  </tr>
</table>

### Why the name "Thoth"?

In ancient Egyptian mythology, **Thoth** (𓁟) was the god of wisdom, writing, and knowledge — the divine scribe who recorded all human understanding. Like its namesake, this tool is built to gather, organize, and faithfully retrieve knowledge — while keeping everything under your control.

---

## ✨ Features

> 📖 Every feature below is documented in full technical detail in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

### 🤖 ReAct Agent Architecture

LangGraph-based autonomous agent with **28 core tools plus auto-generated channel tools** — the agent decides which tools to call, how many times, and in what order. Real-time token streaming with thinking model support (DeepSeek-R1, Qwen3, QwQ — collapsible reasoning bubbles). Smart context management via tiktoken: auto-summarization at 80% capacity, proportional tool-output shrinking, and dynamic tool budgets that adapt to available headroom. The prompt stack combines centralized templates with dynamic identity and self-knowledge context so Thoth can describe its own capabilities accurately. Destructive actions require explicit confirmation; orphaned tool calls are auto-repaired; recursive loops are caught with a wind-down warning at 75%.

[Details →](docs/ARCHITECTURE.md#react-agent-architecture)

### 🧠 Long-Term Memory & Knowledge Graph

Thoth builds a **personal knowledge graph** — entities (person, place, event, preference, fact, project, organisation, concept, skill, media) linked by 67 typed directional relations with 60+ aliases (`Dad --[father_of]--> User`), with alias resolution, auto-linking on save, memory decay, and background orphan repair. Vague relation types (`related_to`, `associated_with`, etc.) are automatically rejected; relation pre-normalisation ensures consistent naming. The agent can save, search, link, and explore memories through natural conversation. Graph-enhanced auto-recall retrieves semantically similar entities via FAISS and expands 1 hop in the NetworkX graph before every LLM call. An interactive **Knowledge tab** visualizes the full graph with search, entity-type filters, ego-graph toggle, and clickable detail cards. Background extraction produces structured triples with deterministic cross-category dedup; conservative extraction filters skip workflow threads, truncate assistant messages, and apply an 0.80 confidence floor to prevent junk entities.

[Details →](docs/ARCHITECTURE.md#long-term-memory--knowledge-graph)

### 📚 Wiki Vault (Obsidian Export)

Export the entire knowledge graph as an **Obsidian-compatible markdown vault** — one `.md` file per entity with YAML frontmatter, `[[wiki-links]]`, and per-type indexes. Entities grouped by type (`wiki/person/`, `wiki/project/`, …); sparse entities roll up into index files. Live export on save/delete, full-text search, and conversation export. The agent has 4 sub-tools (`wiki_read`, `wiki_rebuild`, `wiki_stats`, `wiki_export_conversation`) to interact with the vault directly.

[Details →](docs/ARCHITECTURE.md#wiki-vault)

### 🌙 Dream Cycle (Nightly Knowledge Refinement)

A 5-phase background daemon that refines the knowledge graph during idle hours — **merging duplicates** (≥0.93 similarity), **enriching thin descriptions** from conversation context, **decaying stale inferred confidence**, **inferring missing relationships** between co-occurring entities, and **generating actionable insights** from recent system activity. Hub diversity caps, batch rotation, and a 7-day rejection cache ensure high-quality, non-repetitive inferences. Three-layer anti-contamination system prevents cross-entity fact-bleed. Ollama busy check defers cycles when the GPU is actively serving a user request. Configurable dream window; all operations logged to an expandable dream journal in the Activity tab. Manual 🌙 Dream button in the Knowledge graph panel.

[Details →](docs/ARCHITECTURE.md#dream-cycle)

### 📄 Document Knowledge Extraction

Uploaded documents are processed through a **map-reduce LLM pipeline** that extracts structured knowledge into the graph. Documents are split into windows, summarized, then reduced into a coherent article; core entities and relations are extracted with full source provenance. A curated relation vocabulary (67 types + 60 aliases) eliminates unknown-type warnings; entity caps (12 per document), minimum description length (30 chars), hub entity dedup, and self-loop rejection ensure clean output. Supports PDF, DOCX, TXT, Markdown, HTML, and EPUB. Live progress pill in the status bar with phase indicator and stop button. Per-document cleanup removes vector store entries and all extracted entities.

[Details →](docs/ARCHITECTURE.md#document-knowledge-extraction)

### 🤖 Brain Model & Cloud Models

Run **fully local** via Ollama (39 curated tool-calling models) or connect cloud providers — **OpenAI**, **Anthropic** (Claude), **Google AI** (Gemini), **xAI** (Grok), and **OpenRouter** (100+ models) — switchable per-thread and per-task from the GUI. First-launch wizard offers Local or Cloud paths; star favorites for quick access; cloud vision models are auto-detected. Privacy controls disable memory extraction and auto-recall for cloud threads. Smart context trimming reduces token usage and cloud API costs.

[Details →](docs/ARCHITECTURE.md#brain-model--cloud-models)

### 🎤 Voice Input & 🔊 Text-to-Speech

Toggle-based voice input with local **faster-whisper** STT (4 model sizes, CPU-only int8) — no cloud APIs. Neural TTS via **Kokoro** with 10 voices (US/British, male/female), streaming sentence-by-sentence with automatic mic gating during playback. Combine both for a fully hands-free conversational experience.

[Details →](docs/ARCHITECTURE.md#voice-input--text-to-speech)

### 🖥️ Shell Access

Full shell access with **3-tier safety** — safe commands (`ls`, `git status`) auto-execute, moderate commands (`rm`, `pip install`) require confirmation, dangerous commands (`shutdown`, `reboot`, `mkfs`) are blocked outright. Enhanced destructive-command detection for workflow safety-mode integration. Persistent sessions per thread, inline terminal panel, command history saved to disk. Background tasks and workflows support per-task command prefix allowlists.

[Details →](docs/ARCHITECTURE.md#shell-access)

### 🌐 Browser Automation

Autonomous browsing in a **visible Chromium window** — navigate, click, type, scroll, and manage tabs through natural conversation. Accessibility-tree snapshots with numbered element references; per-thread tab isolation; persistent login profile; smart snapshot compression for context efficiency; crash recovery and automatic browser detection (Chrome → Edge → Playwright).

[Details →](docs/ARCHITECTURE.md#browser-automation)

### 👁️ Vision

Camera capture, screen capture, and **workspace image file analysis** via local or cloud vision models. Cloud models with vision capability (GPT-4o, Claude) are auto-detected. Images displayed inline in chat; configurable vision model selection.

[Details →](docs/ARCHITECTURE.md#vision)

### ⚡ Workflows & Scheduling

Advanced **workflow engine** powered by APScheduler with 7 schedule types (daily, weekly, weekdays, weekends, interval, cron, one-shot delay) and a full **step-based pipeline builder**. Five step types — Prompt, Condition, Approval, Subtask, and Notify — with conditional `if_true`/`if_false` branching, approval gates that pause for human decisions, webhook triggers, task-completion triggers, concurrency groups, and per-workflow safety mode (block destructive, require approval, or allow all). Template variables (`{{date}}`, `{{time}}`, `{{step.X.output}}`), channel delivery (Telegram/Email), per-task model override, and configurable background permissions. A redesigned workflow builder UI offers simple and advanced modes with a visual Mermaid flow preview. The Workflow Console surfaces running jobs, approvals, recent history, and insight cards, while pending approvals still surface in the sidebar with badge counts and quick-approve buttons.

[Details →](docs/ARCHITECTURE.md#workflows--scheduling)

### 🎨 Designer Studio

A dedicated design workspace with **five project modes** — slide **decks**, long-form **documents**, interactive **landing pages**, multi-screen **app mockups**, and motion **storyboards**. Each mode has its own canvas rules, template gallery, prompt guardrails, and export targets. Interactive projects (landing, app_mockup, storyboard) run on a sandboxed **runtime bridge** that turns declarative `data-thoth-action` attributes into real in-preview navigation, state toggles, and media playback — no free-form `<script>` from the agent. The designer tool surface includes a full suite of surgical editors (move, replace, duplicate, restyle, refine-text, insert-component), AI content tools (image + video generation, chart insertion, speaker notes), and a **critique-repair loop** that automatically catches overflow, spacing, contrast, hierarchy, and readability issues. Export to PDF / HTML / PNG / PPTX, or publish a self-contained interactive share link. A **mutation diff review dialog** shows exactly what the agent changed, page by page, turn over turn.

[Details →](docs/ARCHITECTURE.md#designer-studio)

### 🪞 Thoth Status & Identity

Self-introspection tool and Preferences UI for checking the current model, channels, tools, memory, logs, Designer projects, and more. Assistant name and personality are configurable, sensitive setting changes require approval, and self-improvement can expose skill-creation and skill-patching tools when enabled.

[Details →](docs/ARCHITECTURE.md#thoth-status--identity)

### 💡 Self-Knowledge & Insights

Prompt-time self-knowledge keeps Thoth aware of its own feature set and live runtime state. Dream Cycle now produces structured insights from recent activity, and the Workflow Console lets you pin, dismiss, and apply them from the UI.

[Details →](docs/ARCHITECTURE.md#self-knowledge--insights)

### 📬 Messaging Channels

A generic **Channel ABC** lets any messaging platform plug into Thoth — channels declare capabilities (photo, voice, documents, reactions, buttons, streaming) and the system auto-generates tools, settings UI, and health checks for each one. **Five channels** ship out of the box:

- **Telegram** — inbound voice transcription (faster-whisper), photo analysis (Vision), document handling with text extraction (PDF/CSV/JSON), emoji reactions (👀/👍/💔), inline keyboard buttons for approvals, streaming responses via progressive message edits
- **WhatsApp** — Node.js bridge (Baileys) with QR-code pairing; inbound/outbound text, photos, documents, and voice; YouTube rich link previews; Markdown-to-WhatsApp formatting; streaming via message edits
- **Discord** — `discord.py` adapter with DM-based messaging; streaming, reactions, typing indicators, slash commands, and media support
- **Slack** — `slack-bolt` adapter with Socket Mode (no webhook needed); DM threading; streaming via `chat.update`; reactions, typing, and file uploads
- **SMS** — Twilio adapter with inbound webhook; outbound via REST API; MMS photo support; auto-tunnel for inbound delivery

All channels share auth utilities, slash commands, approval routing, corrupt-thread repair, and media capture helpers. A **tunnel manager** (ngrok) auto-exposes webhook ports for channels that need inbound delivery. A live **channel monitor** in the sidebar shows status dots, icons, and last-activity times for all configured channels.

[Details →](docs/ARCHITECTURE.md#messaging-channels)

### 🖼️ Image Generation

Generate and edit images via **OpenAI**, **xAI** (Grok Imagine), and **Google** (Imagen 4, Nano Banana) — rendered inline in chat, persisted to disk, and deliverable to any messaging channel. Supports OpenAI (`gpt-image-1`, `gpt-image-1.5`, `gpt-image-1-mini`), xAI (`grok-imagine-image`), and Google (`imagen-4.0-generate-001`, Gemini image models) with configurable size and quality. Edit existing images by referencing the last generation, a pasted attachment, or a file path. Per-provider model picker in Settings → Models.

[Details →](docs/ARCHITECTURE.md#image-generation)

### 🎬 Video Generation

Generate short video clips from text prompts or reference images via **Google Veo** — rendered inline in chat, persisted to disk, and deliverable to supported messaging channels. Designer Studio's **storyboard** mode and **landing** hero slots consume the same provider layer so motion content lands as `asset://` references in preview, presenter mode, and published share links. Text-to-video and image-to-video flows both supported, with provider-side person-generation policy handling.

[Details →](docs/ARCHITECTURE.md#video-generation)

### 🔌 Plugin System & Marketplace

A sandboxed, hot-reloadable **plugin architecture** lets anyone add new tools and skills without touching core code. Plugins declare metadata in `plugin.json`, are security-scanned (no `eval`/`exec`/`subprocess`), and run in a dependency-safe sandbox. A built-in **marketplace** lets users browse, install, update, and uninstall plugins from a curated GitHub-hosted catalog. Plugin settings, API keys, enable/disable toggles, and per-plugin config dialogs are all managed from Settings → Plugins.

[Details →](docs/ARCHITECTURE.md#plugin-system--marketplace)

### ⬆ Auto-Updates

Thoth checks GitHub Releases in the background and surfaces an "⬆ vX.Y.Z" pill in the status bar when a new build is available. Clicking it opens release notes plus Install / Skip / Remind-me-later buttons. Downloads are SHA256-verified against a manifest embedded in each release body and Authenticode/codesign verified before launch. Auto-checking is on by default; if there's no Internet the check fails silently. Channel selection (stable vs beta), skip list, and a manual "Check now" button live in **Settings → Preferences → Updates**. The same flow is exposed to the agent as `thoth_check_for_updates` and `thoth_install_update` (always approval-gated).

[Details →](docs/ARCHITECTURE.md#plugin-system--marketplace)

### 📋 Habit & Health Tracker

Conversational tracking for medications, symptoms, exercise, periods, mood, sleep — *"I took my Lexapro"*, *"Headache level 6"*. Auto-detection with confirmation; 7 built-in analyses (adherence, streaks, numeric stats, trends, co-occurrence, cycle estimation); CSV export chains to Plotly charts. All data in local SQLite, excluded from the memory system.

[Details →](docs/ARCHITECTURE.md#habit--health-tracker)

### 🖥️ Desktop App

Native window via **pywebview** with system tray, splash screen, right-click context menu, and auto-restart. First-launch setup wizard (Local or Cloud). Self-contained one-click installers for **Windows** (Inno Setup) and **macOS** (.app with code signing + notarization) — CI/CD pipeline automates builds, signing, and GitHub Releases.

[Details →](docs/ARCHITECTURE.md#desktop-app)

### 💬 Chat & Conversations

Multi-turn threads with LangGraph checkpointer, auto-naming, per-thread model switching, and export (Markdown, text, PDF via Playwright). Attach images, PDFs, CSV, Excel, JSON — plus clipboard paste and drag-and-drop. **File-on-disk media storage** with two-tier persistence — generated content survives thread deletion, transient captures cleaned up automatically. **Auto-scroll** follows streaming output with user-override (scroll up to pause, new message re-engages). Inline rendering: **Plotly charts**, **Mermaid diagrams** (flowchart, sequence, state, ER, Gantt, mindmap), **YouTube embeds** (including Shorts), and syntax-highlighted code. **Modern chat input** with rounded card layout and inline file chips. **Status monitor panel** with animated avatar, health-check pills, OAuth token monitoring, and one-click diagnosis. **Sidebar channel monitor** with live status dots, icons, and last-activity tracking for all configured channels. Streaming robustness improvements replace silent failures with debug logging; output truncation detection warns when the model hits its token limit.

[Details →](docs/ARCHITECTURE.md#chat--conversations)

### 🔔 Notifications

Desktop notifications, distinct audio chimes (task completion, timer alerts), and contextual in-app toasts — success auto-dismisses, errors persist as red banners. Unified `notify()` call across all channels.

[Details →](docs/ARCHITECTURE.md#notifications)

### 🧩 Bundled Skills

**12 reusable instruction packs** plus **15 tool guides** injected into the system prompt when enabled — each a `SKILL.md` with YAML frontmatter. Manual skills toggle from Settings; tool guides auto-activate when their linked tools are enabled. Create custom skills via the in-app editor or `~/.thoth/skills/`.

| Skill | Description |
|-------|-------------|
| 🧠 **Brain Dump** | Capture unstructured thoughts → organized notes |
| ☀️ **Daily Briefing** | Weather, calendar, and news roundup |
| 📊 **Data Analyst** | Dataset analysis, stats, and Plotly charts |
| 🔬 **Deep Research** | Multi-source research → structured report |
| 🎨 **Design Creator** | Structured workflow for presentations, one-pagers, reports, and marketing layouts in Designer Studio |
| 🗣️ **Humanizer** | Natural human tone — no AI-speak |
| 📚 **Knowledge Base** | Manage the personal knowledge base across graph memories, uploaded documents, and the wiki vault |
| 📋 **Meeting Notes** | Raw notes → actionable minutes |
| 🎯 **Proactive Agent** | Anticipate needs, self-check at milestones |
| 🪞 **Self-Reflection** | Review memory for gaps and contradictions |
| ⚙️ **Task Automation** | Design effective advanced workflows with step pipelines, conditions, and approvals |
| 🌐 **Web Navigator** | Strategic browser automation patterns |

[Details →](docs/ARCHITECTURE.md#bundled-skills)

---

### How does Thoth compare to OpenClaw?

[OpenClaw](https://github.com/openclaw/openclaw) is a popular open-source personal AI assistant. It's a powerful multi-channel gateway built for developers comfortable in the terminal. Here's how the two compare:

| | Thoth | OpenClaw |
|---|---|---|
| **Getting started** | **One-click installer** (`.exe` / `.dmg`) — download, run, done. Built-in setup wizard, no terminal required | `npm install -g openclaw@latest` → CLI onboarding. Requires Node.js 24. Windows needs WSL2 (no native Windows support) |
| **Local AI (offline)** | **Local-first** — Ollama with 39 curated models out of the box. Works fully offline. Cloud is opt-in | Cloud-first design — requires an API key to start. Local model support through provider config |
| **Memory** | **Personal knowledge graph** — 10 entity types, typed directional relations, visual explorer, FAISS semantic search + 1-hop graph expansion, memory decay, orphan repair | Flat markdown files (`MEMORY.md` + daily notes) with semantic search. No structured graph |
| **Knowledge refinement** | **Dream Cycle** — 5-phase nightly engine: duplicate merging (≥0.93 similarity), description enrichment, stale-confidence decay, relationship inference with hub diversity caps and rejection cache, and actionable insight generation. 3-layer anti-contamination system, dream journal | Dreaming (experimental) — Light/Deep/REM phases that promote short-term signals to long-term memory via scoring thresholds |
| **Document intelligence** | **Map-reduce LLM pipeline** — extracts structured entities and relations into the knowledge graph with source provenance. Curated 67-type relation vocabulary, entity caps, self-loop rejection. Supports PDF, DOCX, EPUB, HTML, Markdown | File read/write/edit operations in the workspace |
| **Wiki vault** | **Obsidian-compatible export** — one `.md` per entity with `[[wiki-links]]`, YAML frontmatter, and per-type indexes | Not available |
| **Voice** | **Fully local** — faster-whisper STT + Kokoro TTS with 10 voices. Audio never leaves your machine | ElevenLabs (cloud TTS) + system fallback. Voice Wake on macOS/iOS |
| **Health tracking** | **Built-in tracker** — medications, symptoms, exercise, mood, sleep, periods. Streak analysis, CSV export, Plotly charts | Not available |
| **Tools** | 28 core tools plus auto-generated channel send tools — shell, browser, filesystem, Gmail, Calendar, Designer Studio, Thoth Status, memory graph, image + video generation, and research tools | ~20 built-in tools — exec, browser, web search, canvas, cron, image/music/video generation |
| **Messaging channels** | **5 channels** — Telegram, WhatsApp, Discord, Slack, SMS — all with streaming, reactions, media, and approval routing. Auto-generated per-channel tools. Tunnel manager for webhooks | **23+ channels** — WhatsApp, Telegram, Slack, Discord, Signal, iMessage, Teams, Matrix, IRC, and many more |
| **Autonomous agents** | **Advanced workflows** — step-based pipelines with conditions, approval gates, webhook triggers, concurrency groups, and per-workflow safety mode. Multiple run in parallel with their own persistent threads | Multi-agent routing with isolated sessions per sender/channel |
| **Desktop app** | Native window (pywebview) + system tray on **Windows & macOS**. One-click installers for both | macOS menu bar app. No native Windows app (WSL2 required). iOS & Android companion apps |
| **Designer / Canvas** | Designer Studio for decks, one-pagers, reports, published links, plus Mermaid diagrams and Plotly charts rendered inline | A2UI — agent-driven interactive visual workspace |
| **Plugins** | Sandboxed plugin marketplace with hot-reload and security scanning | npm plugin ecosystem + ClawHub skill registry. Large community catalog |
| **Privacy** | All data local. No account, no server, no telemetry. API keys stored locally — Thoth has no servers | Self-hosted gateway. Data stays on your machine. Some channel integrations require external services |
| **Cost** | **Free** with local models. Cloud: pay-per-token (pennies/conversation) | Free + open source. Requires a cloud API key to function |

> **In short:** OpenClaw is a powerful gateway for developers who want their AI assistant on every messaging platform. Thoth is built for people who want **personal AI sovereignty** — local-first intelligence, a structured knowledge graph that grows with you, one-click setup, and tools that work without touching a terminal. Different philosophies, both open source.

> For comparisons with ChatGPT and other cloud assistants, see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#comparison-with-other-tools).

---

## 🔧 Tools

Thoth's agent has access to 28 core tool modules. Many of them expose multiple operations, and running messaging channels add extra send/photo/document tools automatically. Tools can be enabled or disabled from the Settings panel.

### Search & Knowledge

| Tool | Description | API Key? |
|------|-------------|----------|
| **🔍 Web Search** | Live web search via Tavily for current events, news, real-time data | `TAVILY_API_KEY` |
| **🦆 DuckDuckGo** | Free web search — no API key needed | None |
| **🌐 Wikipedia** | Encyclopedic knowledge with contextual compression | None |
| **📚 Arxiv** | Academic paper search — newest-first sorting, full-text HTML links, arXiv query syntax (`ti:`, `au:`, `abs:`, `cat:`) | None |
| **▶️ YouTube** | Search videos + fetch full transcripts/captions | None |
| **🔗 URL Reader** | Fetch and extract text content from any URL | None |
| **📄 Documents** | Semantic search over your uploaded files (FAISS vector store) | None |
| **📚 Wiki Vault** | Search, read, rebuild, and export the knowledge graph as an Obsidian markdown vault | None |

### Productivity

| Tool | Description | API Key? |
|------|-------------|----------|
| **📧 Gmail** | Search, read, draft, and send emails with file attachments (Google OAuth) | OAuth credentials |
| **📅 Google Calendar** | View, create, update, move, and delete events (Google OAuth) | OAuth credentials |
| **📁 Filesystem** | Sandboxed file operations — read, write, copy, move, delete within a workspace folder; reads PDF, CSV, Excel (.xlsx/.xls), JSON/JSONL, TSV, and image files; images displayed inline in chat; structured data files return schema + stats + preview via pandas; PDF export via `export_to_pdf` (Playwright with fpdf2 fallback) | None |
| **🖥️ Shell** | Execute shell commands with 3-tier safety (safe/moderate/blocked); persistent sessions per thread; user approval for destructive commands; inline terminal panel | None |
| **🌐 Browser** | Autonomous web browsing in a visible Chromium window — navigate, click, type, scroll, snapshot, back, tab management; accessibility-tree snapshots with numbered element references; persistent profile for logins | None |
| **📋 Workflows** | Create, list, update, delete, and run advanced workflows — step-based pipelines with conditions, approvals, triggers, 7 schedule types (daily, weekly, weekdays, weekends, interval, cron, delay), channel delivery, per-task model override | None |
| **📋 Tracker** | Habit/health tracker — log meds, symptoms, exercise, periods; streak, adherence, trend analysis; CSV export | None |
| **📬 Channels** | Auto-generated send/photo/document tools for each running channel (Telegram, WhatsApp, Discord, Slack, SMS); receive voice, photos, and documents with transcription, analysis, and text extraction | Per-channel config |
| **🐦 X (Twitter)** | Grouped read, post, and engage operations for search, timeline, mentions, user info, posting, replies, quotes, likes, reposts, bookmarks, and deletes via OAuth 2.0 PKCE | X API keys |
| **🖼️ Image Generation** | Generate images from text prompts and edit existing images via OpenAI, xAI (Grok Imagine), and Google (Imagen 4, Nano Banana); rendered inline in chat and deliverable to channels | Cloud API key |
| **🎬 Video Generation** | Generate short video clips from text prompts or reference images via Google Veo; rendered inline in chat, used by Designer storyboards, and deliverable to channels | Cloud API key |

### Design & Self-Management

| Tool | Description | API Key? |
|------|-------------|----------|
| **🎨 Designer** | Five-mode design studio (deck / document / landing / app_mockup / storyboard) with interactive runtime bridge, curated templates, AI image + video generation, chart insertion, brand controls, critique-repair loop, published share links, and export to PDF / HTML / PNG / PPTX | None |
| **🪞 Thoth Status** | Query version, model, channels, tools, memory, identity, logs, Designer projects, and more; controlled self-management for selected settings and optional skill creation/patching when self-improvement is enabled | None |

### Computation & Analysis

| Tool | Description | API Key? |
|------|-------------|----------|
| **🧮 Calculator** | Safe math evaluation — arithmetic, trig, logs, factorials, combinatorics | None |
| **🔢 Wolfram Alpha** | Advanced computation, symbolic math, unit conversion, scientific data | `WOLFRAM_ALPHA_APPID` |
| **🌤️ Weather** | Current conditions and multi-day forecasts via Open-Meteo | None |
| **👁️ Vision** | Camera capture, screen capture, and workspace image file analysis via vision model | None |
| **🧠 Memory** | Save, search, update, delete, **link**, and **explore** memories in the knowledge graph | None |
| **🔍 Conversation Search** | Search past conversations by keyword or list all saved threads | None |
| **🖥️ System Info** | OS, CPU, RAM, disk space, IP addresses, battery, and top processes | None |
| **📊 Chart** | Interactive Plotly charts — bar, line, scatter, pie, histogram, box, area, heatmap from data files; PNG export via `save_to_file` | None |

### Safety & Permissions

- **Destructive operations require confirmation**: `workspace_file_delete`, `workspace_move_file`, `run_command` (moderate-risk), `send_gmail_message`, `move_calendar_event`, `delete_calendar_event`, `delete_memory`, `tracker_delete`, `task_delete`
- **Filesystem is sandboxed**: only the configured workspace folder is accessible (defaults to `~/Documents/Thoth`, auto-created on first use)
- **Shell commands are safety-classified**: safe (auto), moderate (confirm), blocked (rejected); high-risk commands like `shutdown`, `reboot`, `mkfs` are blocked outright; moderate commands in background tasks require per-task command prefix allowlists
- **Browser tabs are isolated per thread**: each chat or background task gets its own browser tab; tabs are cleaned up on task completion or thread deletion
- **Background task permissions are configurable per-task**: shell command prefixes and email recipients can be allowlisted in the task editor
- **Gmail/Calendar operations are tiered**: read, compose/write, and destructive tiers can be toggled independently
- **Prompt-injection defence** — 5-layer scanning protects against injection attacks in tool outputs and user inputs: instruction override detection, role impersonation, data exfiltration, encoding evasion, and social engineering patterns
- **Tools can be individually disabled** from Settings to reduce model decision complexity

---

## 🏗️ Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                    NiceGUI Frontend (app.py + ui/ package)              │
│  ┌────────────┐  ┌──────────────────────┐  ┌───────────────────┐   │
│  │  Sidebar   │  │   Chat Interface     │  │   Settings Dialog │   │
│  │  Threads   │  │   Streaming Tokens   │  │   13 Tabs         │   │
│  │  Controls  │  │   Tool Status        │  │   Tool Config     │   │
│  │ Knowledge  │  │ Knowledge Graph View │  │   Cloud Settings  │   │
│  │ Approvals  │  │   Approval Gates     │  │                   │   │
│  └────────────┘  └──────────────────────┘  └───────────────────┘   │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  Status Monitor — Avatar · 17 Health Pills · Diagnosis Btn  │   │
│  └──────────────────────────────────────────────────────────────┘   │
└──────────────────────────┬───────────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────────┐
│               LangGraph ReAct Agent (agent.py)                       │
│                                                                      │
│   create_react_agent() with pre-model message trimming              │
│   System prompt with TOOL USE, MEMORY, SELF-KNOWLEDGE, and CITATION │
│   guidelines                                                        │
│   Interrupt mechanism for destructive action confirmation            │
│   Graph-enhanced auto-recall (semantic + 1-hop expansion)           │
│   Per-thread model override (local or cloud)                        │
│                                                                      │
│   28 core tool modules + plugin tools + auto-generated channel      │
│   tools                                                             │
└───────┬──────────┬──────────┬──────────┬──────────┬─────────────────┘
        │          │          │          │          │
        ▼          ▼          ▼          ▼          ▼
  ┌──────────┐ ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐
  │   LLMs   │ │Knowledge│ │ SQLite │ │ FAISS  │ │External│
  │  Ollama  │ │ Graph  │ │Threads │ │ Vector │ │  APIs  │
  │ + Cloud  │ │(SQLite+│ │(local) │ │ Store  │ │(opt-in)│
  │ (opt-in) │ │NetworkX)│ │        │ │        │ │        │
  └──────────┘ └────────┘ └────────┘ └────────┘ └────────┘
```

> 📖 **Module descriptions, data storage layout, and full system internals →** [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#core-modules)

---

## 💻 System Requirements

### For Local Models (Ollama)

| | Minimum | Recommended |
|--|---------|-------------|
| **OS** | Windows 10/11 (64-bit) or macOS 12+ (Apple Silicon & Intel) | Same |
| **Python** | 3.11+ | 3.11+ |
| **RAM** | 8 GB (for 8B models) | 16–32 GB (for 14B–30B models) |
| **GPU** | Not required — Ollama runs on CPU | NVIDIA 8+ GB VRAM (CUDA) or Apple Silicon — dramatically faster |
| **Disk** | ~5 GB (app + one small model like `qwen3:8b`) | 20+ GB for multiple or larger models |
| **Internet** | Required for install and model download; optional at runtime | Same |

> **Note:** The default local model (`qwen3:14b`, ~9 GB) runs well on CPU with 16 GB RAM, but a GPU makes responses significantly faster. Smaller models like `qwen3:8b` (~5 GB) work on 8 GB RAM machines.

### For Cloud Models Only (No Local GPU Needed)

| Requirement | Details |
|-------------|---------|
| **OS** | Windows 10/11 (64-bit) or macOS 12+ (Apple Silicon & Intel) |
| **Python** | 3.11+ |
| **RAM** | 4 GB |
| **Disk** | ~1 GB (app + packages, no model downloads) |
| **GPU** | Not needed |
| **Internet** | Required (LLM inference happens on the provider's servers) |

> You still need an API key from [OpenAI](https://platform.openai.com/), [Anthropic](https://console.anthropic.com/), [Google AI](https://aistudio.google.com/), [xAI](https://console.x.ai/), or [OpenRouter](https://openrouter.ai/). Cloud models are billed per-token by the provider — typically pennies per conversation.

---

## 📥 One-Click Install

### Windows

1. Download **[ThothSetup_3.17.0.exe](https://github.com/siddsachar/Thoth/releases/latest)** from the latest release
2. Run the installer — it installs Python, Ollama, and all dependencies automatically
3. Launch **Thoth** from the Start Menu or Desktop shortcut

### macOS

1. Download **[Thoth-3.17.0-macOS-arm64.dmg](https://github.com/siddsachar/Thoth/releases/latest)** from the latest release
2. Open the DMG and drag **Thoth.app** into the **Applications** folder
3. Launch **Thoth** from Applications or Launchpad
   - First run may prompt "Thoth is an app downloaded from the internet" → click **Open**
   - First run installs Homebrew (if needed), Python, Ollama, and all dependencies automatically
   - Subsequent launches skip installation and start in ~3 seconds

> **Works on Apple Silicon (M1/M2/M3/M4) and Intel Macs** (macOS 12+). No terminal, no manual setup — just double-click and go.

> **Using cloud models only?** The installer still sets up Ollama by default, but you can skip model downloads. On first launch, choose the **Cloud** setup path, enter your API key, and start chatting — no GPU required.

---

## 📦 Installation (From Source)

> **Prefer a manual install?** A few commands from source:

1. **Install [Ollama](https://ollama.com/)** *(required for local models — skip if using cloud models only)*

2. **Clone the repository**
   ```bash
   git clone https://github.com/siddsachar/Thoth.git
   cd Thoth
   ```

3. **Create and activate a virtual environment**
   ```bash
   python -m venv .venv
   # Windows
   .venv\Scripts\activate
   # macOS / Linux
   source .venv/bin/activate
   ```

4. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

5. **Start Ollama** *(if using local models)*
   ```bash
   ollama serve
   ```

6. **Launch Thoth**
   ```bash
   python launcher.py
   ```
   This starts the system tray icon and opens the app at `http://localhost:8080`.

   Alternatively, run directly without the tray:
   ```bash
   python app.py
   ```

> **First launch:** A setup wizard lets you choose between **Local** (Ollama) and **Cloud** (API key) setup paths. For local, the default brain model (`qwen3:14b`, ~9 GB) is recommended. For cloud, enter your API key (OpenAI, Anthropic, Google AI, xAI, or OpenRouter) and pick a default model.

---

## 🔑 API Key Setup (Optional)

Most tools work without any API keys. For cloud models and enhanced functionality:

### Cloud LLM Providers

| Service | Key | Purpose | How to Get |
|---------|-----|---------|------------|
| **OpenAI** | `OPENAI_API_KEY` | GPT and other OpenAI models | [platform.openai.com](https://platform.openai.com/) |
| **Anthropic** | `ANTHROPIC_API_KEY` | Claude models (direct API) | [console.anthropic.com](https://console.anthropic.com/) |
| **Google AI** | `GOOGLE_API_KEY` | Gemini models (direct API) | [aistudio.google.com](https://aistudio.google.com/) |
| **xAI** | `XAI_API_KEY` | Grok models (direct API) | [console.x.ai](https://console.x.ai/) |
| **OpenRouter** | `OPENROUTER_API_KEY` | 100+ models from all major providers (Claude, Gemini, Llama, etc.) | [openrouter.ai](https://openrouter.ai/) |

Configure cloud keys in **⚙️ Settings → ☁️ Cloud** tab. Keys are stored locally in `~/.thoth/cloud_config.json` — never sent to Thoth's servers (there are none).

### Tool API Keys

| Service | Key | Purpose | How to Get |
|---------|-----|---------|------------|
| **Tavily** | `TAVILY_API_KEY` | Web search (1,000 free searches/month) | [app.tavily.com](https://app.tavily.com/) |
| **Wolfram Alpha** | `WOLFRAM_ALPHA_APPID` | Advanced computation & scientific data | [developer.wolframalpha.com](https://developer.wolframalpha.com/) |

### Channel & Service Keys

| Service | Key | Purpose | How to Get |
|---------|-----|---------|------------|
| **Telegram** | `TELEGRAM_BOT_TOKEN` | Telegram bot messaging | [BotFather](https://t.me/botfather) |
| **Discord** | `DISCORD_BOT_TOKEN` | Discord DM messaging | [Discord Developer Portal](https://discord.com/developers/) |
| **Slack** | `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` | Slack DM messaging (Socket Mode) | [Slack API](https://api.slack.com/apps) |
| **Twilio (SMS)** | `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` | SMS messaging | [twilio.com](https://www.twilio.com/) |
| **X (Twitter)** | `X_CLIENT_ID` / `X_CLIENT_SECRET` | X API v2 (OAuth 2.0 PKCE) | [X Developer Portal](https://developer.x.com/) |
| **ngrok** | `NGROK_AUTHTOKEN` | Tunnel for inbound webhooks (SMS, etc.) | [ngrok.com](https://ngrok.com/) |

Configure channel keys in **⚙️ Settings → 📡 Channels** and **⚙️ Settings → 🔗 Accounts** tabs. Keys are stored locally.

For **Gmail** and **Google Calendar**, you'll need a Google Cloud OAuth `credentials.json` — setup instructions are provided in the respective Settings tabs.

---

## 🚀 Quick Start

### Local Models (Default)

1. **Launch Thoth** and wait for the default model to download (first time only)
2. **Click "＋ New conversation"** in the sidebar
3. **Ask anything** — the agent will automatically choose which tools to use:
   - *"What's the weather in Tokyo?"* → uses Weather tool
   - *"Search for recent papers on transformer architectures"* → uses Arxiv
   - *"Remember that my mom's birthday is March 15"* → saves to Memory
   - *"Read the file report.pdf in my workspace"* → uses Filesystem
   - *"Run git status on my project"* → uses Shell (safe, auto-executes)
   - *"Install pandas with pip"* → uses Shell (moderate, asks for approval)
   - *"What's on my screen right now?"* → uses Vision (screen capture)
   - *"I took my Lexapro"* → asks to log, then saves to Tracker
   - *"Show my headache trends this month"* → uses Tracker + Chart
   - *"Remind me to call the dentist tomorrow at 9am"* → uses Tasks with scheduling
   - *"Create a 6-slide pitch deck for my startup"* → uses Designer
   - *"What did I ask about taxes last week?"* → uses Conversation Search
4. **Open ⚙️ Settings** to configure models, enable/disable tools, and set up integrations

### Cloud Models (No GPU? Start Here)

1. **Launch Thoth** → on the setup wizard, choose **☁️ Cloud**
2. **Enter your API key** (OpenAI, Anthropic, Google AI, xAI, or OpenRouter) → Thoth validates and fetches available models
3. **Pick a default model** (e.g. GPT) and start chatting — no downloads, no GPU needed
4. Switch models per conversation anytime from the chat header dropdown

---

## 🔒 Privacy & Security — Personal AI Sovereignty

**Local models (default):** All LLM inference runs on your machine via Ollama. Documents, memories, and conversations stored locally in `~/.thoth/`. External network calls only when using online tools (web search, Gmail, Calendar) — each individually disableable. No telemetry, no tracking.

**Cloud models (opt-in):** Only the current conversation is sent to the LLM provider (OpenAI, Anthropic, Google AI, xAI, or OpenRouter). Memories, knowledge graph, documents, files, and other conversations never leave your machine. Your API key connects directly to the provider — Thoth has no servers and no middleman.

**Always:** API keys stored locally; no Thoth account required; no sign-up; no server to phone home to. Tools can be individually disabled to control what the agent can access.

---

## 🤝 Contributing

Contributions are welcome. Start with [CONTRIBUTING.md](CONTRIBUTING.md) for the local setup, branch naming, commit format, PR process, and test expectations.

Useful project docs:

- [Branching strategy](docs/BRANCHING.md) — protected `main`, feature branches, hotfixes
- [Release process](docs/RELEASING.md) — version bumps, tags, build artifacts, signing, publishing
- [Security policy](SECURITY.md) — private vulnerability reporting
- [Code of conduct](CODE_OF_CONDUCT.md) — community expectations

All changes should go through a pull request. `main` is intended to stay releasable.

---

## 📄 License

Apache 2.0 — see [LICENSE](LICENSE) for details.

---

## 🙏 Acknowledgements

Built with [NiceGUI](https://nicegui.io/), [LangGraph](https://langchain-ai.github.io/langgraph/), [LangChain](https://python.langchain.com/), [Ollama](https://ollama.com/), [FAISS](https://github.com/facebookresearch/faiss), [Kokoro TTS](https://github.com/thewh1teagle/kokoro-onnx), [faster-whisper](https://github.com/SYSTRAN/faster-whisper), [HuggingFace](https://huggingface.co/), and [tiktoken](https://github.com/openai/tiktoken).
