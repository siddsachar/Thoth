# 𓁟 Thoth — Release Notes

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
