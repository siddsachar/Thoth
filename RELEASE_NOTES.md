# 𓁟 Thoth — Release Notes

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

- **Wake word detection** — 4 built-in wake words (Hey Jarvis, Hey Mycroft, Alexa, Hey Thought) via OpenWakeWord ONNX models
- **Speech-to-text** — faster-whisper with selectable model size (tiny/base/small)
- **Configurable sensitivity** — wake word threshold slider (0.1–0.95)
- **Audio chime** on wake word detection
- **Voice bar UI** — shows listening/transcribing status with real-time feedback
- **Mic gating** — microphone automatically muted during TTS playback to prevent echo and feedback loops
- **Follow-up mode** — after TTS finishes speaking, the mic re-opens briefly so you can ask follow-up questions without re-triggering the wake word

### 🔊 Text-to-Speech

Neural speech synthesis, fully offline:

- **Piper TTS engine** — auto-downloaded from HuggingFace on first use (~50 MB)
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
