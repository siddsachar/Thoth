# 𓁟 Thoth — Release Notes

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

- **Piper TTS engine** — bundled with installer (engine + default voice); additional voices downloaded from HuggingFace on demand
- **8 voices** — US and British English, male and female variants
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
9. **🎛️ Preferences** — voice input (wake word, Whisper model, sensitivity), TTS (voice selection, Piper install, speed)

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
- `piper/` — Piper TTS engine and voice models (new)

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
