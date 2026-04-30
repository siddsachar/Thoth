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

Thoth is a **local-first AI assistant for personal AI sovereignty**: a desktop agent with memory, tools, workflows, design creation, messaging, plugins, and optional cloud models while your durable data stays on your machine.

It runs fully local through [Ollama](https://ollama.com/) with 39 curated tool-calling models, or you can opt into OpenAI, Anthropic, Google AI, xAI, and OpenRouter when you want frontier reasoning or do not have a GPU. API keys are stored in the OS credential store when available; Thoth has no account system, server, or telemetry pipeline.

> **🖥️ One-click install on Windows & macOS** — download, run, done. No terminal, Docker, or config files required. [Get it here.](https://github.com/siddsachar/Thoth/releases)

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

## ✨ Why Thoth Stands Out

> Full subsystem details live in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

### Memory That Becomes A Knowledge Graph

Thoth stores durable knowledge as entities and typed relationships, not just chat snippets. It can save, search, link, explore, visualize, and export your knowledge graph as an Obsidian-compatible wiki vault, while background extraction and Dream Cycle refine duplicates, stale confidence, missing relationships, and actionable insights.

### Designer Studio, Media, And Documents

Designer Studio creates decks, documents, landing pages, app mockups, and storyboards with a sandboxed interactive runtime, critique-repair loop, editable exports, and shareable HTML. The same media layer supports image generation/editing with OpenAI, Google, and xAI, plus video generation through Google Veo and xAI Grok Imagine Video.

### Real Computer Use, With Guardrails

The LangGraph ReAct agent has 30 core tool modules plus auto-generated channel tools. It can browse in a visible Chromium window, use the shell, manage files, search the web, read documents, send email, manage calendar events, create charts, inspect system status, and call external MCP tools. Destructive actions are approval-gated, shell commands are safety-classified, the filesystem is sandboxed, and tools can be disabled individually.

### Workflows That Run On Their Own

Advanced workflows add schedules, webhook triggers, condition branches, approval steps, subtasks, notification-only runs, concurrency groups, per-workflow model/tool/skill overrides, and background safety modes. The Workflow Console shows running jobs, approvals, recent history, and insights without leaving chat.

### Native, Multi-Channel, Local-First

Thoth ships as a desktop app for Windows and macOS with one-click installers, tray integration, setup wizard, voice input, local Kokoro TTS, desktop notifications, and 5 bundled messaging channels: Telegram, WhatsApp, Discord, Slack, and SMS. Channels share media handling, streaming, approvals, health checks, and optional tunnel support.

### Extensible Without Giving Up Control

Plugins add tools and skills through a sandboxed marketplace; MCP servers add external tools with per-server and per-tool review; the migration wizard imports selected Hermes/OpenClaw data with backups and redacted reports. Core and plugin API keys use the OS credential store when available, with metadata-only files in Thoth's data directory.

## Feature Map

| Area | What Thoth Includes |
|------|---------------------|
| **Agent & Models** | LangGraph ReAct agent, streaming, thinking bubbles, smart context trimming, 39 curated Ollama models, opt-in OpenAI/Anthropic/Google/xAI/OpenRouter cloud models, per-thread and per-workflow model overrides |
| **Memory & Knowledge** | Personal knowledge graph, FAISS semantic recall, 67 typed relations, graph visualization, Obsidian wiki export, document extraction, Dream Cycle refinement, self-knowledge, and insights |
| **Design & Media** | Designer Studio, PDFs/HTML/PNG/PPTX export, published interactive links, image generation/editing, video generation, chart insertion, Mermaid/Plotly rendering, and media persistence |
| **Tools** | 30 core tools covering search, browser, shell, filesystem, documents, Gmail, Calendar, X, memory, workflows, tracker, image/video, vision, status, MCP, updates, computation, weather, charts, and system info |
| **Automation** | Scheduled workflows, step pipelines, conditions, approvals, subtasks, webhooks, task-completion triggers, notifications, channel delivery, run history, and safety modes |
| **Channels & Voice** | Telegram, WhatsApp, Discord, Slack, SMS, local faster-whisper STT, Kokoro TTS, media intake, reactions, streaming, approval routing, and tunnel manager |
| **Platform & Extensibility** | Native desktop app, one-click installers, auto-updates, plugin marketplace, MCP client, migration wizard, configurable identity, secure API-key storage, 13 manual skills, and 18 tool guides |

[Detailed architecture and subsystem reference →](docs/ARCHITECTURE.md)

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
| **Tools** | 30 core tools plus auto-generated channel send tools — shell, browser, filesystem, Gmail, Calendar, Designer Studio, Thoth Status, memory graph, MCP external tools, image + video generation, and research tools | ~20 built-in tools — exec, browser, web search, canvas, cron, image/music/video generation |
| **Messaging channels** | **5 channels** — Telegram, WhatsApp, Discord, Slack, SMS — all with streaming, reactions, media, and approval routing. Auto-generated per-channel tools. Tunnel manager for webhooks | **23+ channels** — WhatsApp, Telegram, Slack, Discord, Signal, iMessage, Teams, Matrix, IRC, and many more |
| **Autonomous agents** | **Advanced workflows** — step-based pipelines with conditions, approval gates, webhook triggers, concurrency groups, and per-workflow safety mode. Multiple run in parallel with their own persistent threads | Multi-agent routing with isolated sessions per sender/channel |
| **Desktop app** | Native window (pywebview) + system tray on **Windows & macOS**. One-click installers for both | macOS menu bar app. No native Windows app (WSL2 required). iOS & Android companion apps |
| **Designer / Canvas** | Designer Studio for decks, one-pagers, reports, published links, plus Mermaid diagrams and Plotly charts rendered inline | A2UI — agent-driven interactive visual workspace |
| **Plugins** | Sandboxed plugin marketplace with hot-reload and security scanning | npm plugin ecosystem + ClawHub skill registry. Large community catalog |
| **Privacy** | All data local. No account, no server, no telemetry. API keys use the OS credential store when available — Thoth has no servers | Self-hosted gateway. Data stays on your machine. Some channel integrations require external services |
| **Cost** | **Free** with local models. Cloud: pay-per-token (pennies/conversation) | Free + open source. Requires a cloud API key to function |

> **In short:** OpenClaw is a powerful gateway for developers who want their AI assistant on every messaging platform. Thoth is built for people who want **personal AI sovereignty** — local-first intelligence, a structured knowledge graph that grows with you, one-click setup, and tools that work without touching a terminal. Different philosophies, both open source.

> For comparisons with ChatGPT and other cloud assistants, see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#comparison-with-other-tools).

---

## 🔧 Tools

Thoth's agent has access to 30 core tool modules. Many of them expose multiple operations, and running messaging channels add extra send/photo/document tools automatically. Tools can be enabled or disabled from the Settings panel.

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
| **🎬 Video Generation** | Generate short video clips from text prompts or reference images via Google Veo and xAI Grok Imagine Video; rendered inline in chat, used by Designer storyboards, and deliverable to channels | Cloud API key |
| **🔌 External MCP Tools** | Connect external Model Context Protocol servers as dynamic namespaced tools; supports stdio, Streamable HTTP, and SSE; per-server and per-tool toggles; destructive-tool approval gates; curated starter import and diagnostics | Optional per server |

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
- **MCP tools are opt-in and isolated**: imported servers stay disabled until tested, external tools are namespaced, destructive MCP tools require approval, and broken MCP servers degrade to diagnostics instead of startup failure
- **Prompt-injection defence** — 5-layer scanning protects against injection attacks in tool outputs and user inputs: instruction override detection, role impersonation, data exfiltration, encoding evasion, and social engineering patterns
- **Tools can be individually disabled** from Settings to reduce model decision complexity

---

## 🏗️ Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                    NiceGUI Frontend (app.py + ui/ package)              │
│  ┌────────────┐  ┌──────────────────────┐  ┌───────────────────┐   │
│  │  Sidebar   │  │   Chat Interface     │  │   Settings Dialog │   │
│  │  Threads   │  │   Streaming Tokens   │  │   14 Tabs         │   │
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
│   30 core tool modules + plugin tools + external MCP tools +        │
│   auto-generated channel tools                                      │
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

1. Download the latest **[Windows installer (.exe)](https://github.com/siddsachar/Thoth/releases/latest)**
2. Run the installer — it installs Python, Ollama, and all dependencies automatically
3. Launch **Thoth** from the Start Menu or Desktop shortcut

### macOS

1. Download the latest **[macOS DMG](https://github.com/siddsachar/Thoth/releases/latest)**
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

Configure cloud keys in **⚙️ Settings → ☁️ Cloud** tab. Keys are stored in your OS credential store (Windows Credential Manager, macOS Keychain, or Linux Secret Service/KWallet where available); `~/.thoth/api_keys.json` keeps only local metadata such as saved-state and masked fingerprints. Keys are never sent to Thoth's servers (there are none).

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

Configure channel keys in **⚙️ Settings → 📡 Channels** and **⚙️ Settings → 🔗 Accounts** tabs. Saved keys use the same local OS credential store when available.

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

**Always:** Core and plugin API keys are stored locally in your OS credential store when available, with only masked metadata in Thoth's data folder. No Thoth account required; no sign-up; no server to phone home to. Tools can be individually disabled to control what the agent can access.

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
