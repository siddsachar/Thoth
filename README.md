# 𓁟 Thoth — Private AI Assistant

Thoth is a **local-first, privacy-focused AI assistant** that runs entirely on your machine. It combines a powerful ReAct agent with 19 integrated tools — web search, email, calendar, file management, vision, long-term memory, and more — plus Telegram and Email messaging channels, all powered by a locally-running LLM via [Ollama](https://ollama.com/). No data leaves your machine unless you explicitly use an online tool.

### Why not just use ChatGPT?

| | ChatGPT / Claude / Gemini | Thoth |
|---|---|---|
| **Your data** | Sent to cloud servers, used for training | Stays on your machine — always |
| **Conversations** | Owned by the provider, can be deleted or leaked | Stored locally in SQLite, fully yours |
| **Cost** | $20+/month per subscription | Free forever — runs on your own hardware |
| **Memory** | Limited, opaque, provider-controlled | You control what's remembered, searchable, deletable |
| **Tools** | Sandboxed plugins, limited integrations | Direct access to your Gmail, Calendar, filesystem, webcam |
| **Customisation** | Pick a model, write a system prompt | Swap models freely, build multi-step workflows, schedule tasks |
| **Voice** | Cloud-processed speech | Local Whisper STT + Piper TTS — never leaves your mic |
| **Availability** | Requires internet, subject to outages & rate limits | Works offline (core features), no throttling |

> **Bottom line:** Cloud AI assistants rent you access to someone else's computer. Thoth gives you your own.

### Why "Thoth"?

In ancient Egyptian mythology, **Thoth** (𓁟) was the god of wisdom, writing, and knowledge — the divine scribe who recorded all human understanding. Like its namesake, this tool is built to gather, organize, and faithfully retrieve knowledge — while keeping everything under your control.

---

## ✨ Features

### 🤖 ReAct Agent Architecture
- **Autonomous tool use** — the agent decides which tools to call, when, and how many times, based on your question
- **19 tools / 42 sub-tools** — web search, email, calendar, file management, vision, memory, and more (see [full list below](#-tools-19-tools--42-sub-tools))
- **Streaming responses** — tokens stream in real-time with a typing indicator
- **Thinking indicators** — shows when the model is reasoning before responding
- **Smart context management** — conversation history is trimmed to 80% of the context window before each LLM call; oversized tool outputs (e.g. large PDF reads) are proportionally shrunk so multi-file workflows fit within context
- **Live token counter** — progress bar in the sidebar shows real-time context window usage based on trimmed (model-visible) history
- **Graceful error recovery** — agent tool loops are caught automatically with a user-friendly error message; orphaned tool calls are repaired
- **Date/time awareness** — current date and time is injected into every LLM call so the model always knows "today"
- **Destructive action confirmation** — dangerous operations (file deletion, sending emails, deleting calendar events, deleting memories) require explicit user approval via an interrupt mechanism

### 🧠 Long-Term Memory
- **Persistent personal knowledge** — the agent remembers names, birthdays, preferences, projects, and more across conversations
- **6 categories** — `person`, `preference`, `fact`, `event`, `place`, `project`
- **Memory tool** — a dedicated tool lets the agent save, search, update, and delete memories; you can ask it directly — *"Remember that my mom's birthday is March 15"*, *"What do you know about me?"*, *"Delete the memory about my old address"*
- **Automatic memory extraction** — in addition to the tool, a background process scans past conversations on startup and every 6 hours, extracting personal facts and saving them as memories with semantic deduplication — so the agent learns even from things you mentioned in passing
- **Semantic search** — FAISS vector index with Qwen3-Embedding-0.6B for similarity-based memory retrieval (replaces keyword search)
- **Auto-recall** — relevant memories are automatically retrieved and injected into context before every LLM call based on semantic similarity to the current message
- **Local SQLite + FAISS storage** — memories stored in `~/.thoth/memory.db` with vector index in `~/.thoth/memory_vectors/`, never sent to the cloud
- **Settings UI** — browse, search, and bulk-delete memories from the Memory tab in Settings

### 🎤 Voice Input & 🔊 Text-to-Speech
- **Toggle-based voice** — simple manual toggle to start/stop listening, no wake word needed
- **4-state pipeline** — stopped → listening → transcribing → muted, with clean state transitions
- **Local speech-to-text** — transcription via faster-whisper (tiny/base/small/medium models), CPU-only int8 quantization, no cloud APIs
- **Voice-aware responses** — voice input is tagged so the agent knows you're speaking and responds conversationally
- **Neural TTS** — high-quality text-to-speech via Piper TTS, fully offline
- **8 voice options** — US and British English, male and female variants
- **Streaming TTS** — responses are spoken sentence-by-sentence as they stream in
- **Mic gating** — microphone is automatically muted during TTS playback to prevent echo and feedback loops
- **Hands-free mode** — combine voice input + TTS for a fully conversational experience

### 👁️ Vision
- **Camera analysis** — capture and analyze images from your webcam in real-time
- **Screen capture** — take screenshots and ask questions about what's on your screen
- **Configurable vision model** — choose from popular vision models (gemma3, llava, etc.)
- **Camera selection** — pick which camera to use if you have multiple
- **Inline image display** — captured images are shown inline in the chat

### ⚡ Workflows
- **Reusable prompt sequences** — create named, multi-step workflows that run sequentially in a fresh thread
- **Template variables** — use `{{date}}`, `{{day}}`, `{{time}}`, `{{month}}`, `{{year}}` in prompts — replaced at runtime
- **Manual + scheduled execution** — run workflows on demand from the home screen, or schedule them daily/weekly
- **Prompt chaining** — each step sees the output of the previous step, enabling research → summarise → action pipelines
- **Always-background execution** — workflows always run in the background so you can keep chatting; the sidebar shows a ⏳ indicator while running
- **Safety** — destructive tools (send email, delete files, etc.) are automatically excluded from background workflow runs; the LLM adapts by using safe alternatives
- **Pre-built templates** — ships with 4 starter workflows (Daily Briefing, Research Summary, Email Digest, Weekly Review)
- **Full editor** — create, edit, duplicate, and delete workflows from the Settings → Workflows tab
- **Run history** — track past workflow executions with timestamps and step counts

### 📬 Messaging Channels
- **Telegram bot** — connect a Telegram bot via Bot API token; messages are processed by the full ReAct agent with all tools available; each chat gets its own conversation thread
- **Email channel** — polls Gmail for unread emails with `[Thoth]` in the subject line (from your own address only); each Gmail thread gets its own agent conversation thread; replies inline
- **Auto-start** — channels can be set to start automatically when Thoth launches
- **Settings UI** — configure, start/stop, and manage channels from Settings → Channels tab

### 🖥️ Desktop App
- **Native window** — runs in a native OS window via pywebview instead of a browser tab; no browser chrome, feels like a real desktop application
- **Splash screen** — a branded splash screen displays the Thoth logo while the server starts up, then closes automatically when ready
- **System tray** — `launcher.py` runs a pystray system tray icon showing app status (green = running, grey = stopped) with Open / Quit menu
- **Auto-restart** — if the native window is closed, re-opening from the tray relaunches it instantly

### 💬 Chat & Conversations
- **Multi-turn conversational Q&A** with full message history
- **Persistent conversation threads** stored in a local SQLite database via LangGraph checkpointer
- **Auto-naming** — threads are automatically named after the first question
- **Thread switching** — resume any previous conversation seamlessly
- **Thread deletion** — remove individual conversations or delete all at once with confirmation
- **Conversation export** — export any thread as Markdown (.md), plain text (.txt), or PDF (.pdf)
- **File attachments** — attach images (analyzed via vision model), PDFs (text extracted), CSV, Excel, JSON, and text files directly in chat; structured data files return schema + stats + preview via pandas
- **Inline charts** — interactive Plotly charts rendered inline when the agent visualises data (zoom, hover, pan)
- **Inline YouTube embeds** — YouTube links in responses are rendered as playable embedded videos
- **Syntax-highlighted code blocks** — fenced code blocks render with language-aware highlighting and a built-in copy button
- **Onboarding guide** — first-run welcome message with tool overview and clickable example prompts; `?` button in sidebar to re-show anytime
- **Startup health check** — verifies Ollama connectivity and model availability on launch

### 🤖 Brain Model
- **Dynamic model switching** — choose any Ollama-supported model from the Settings panel
- **30+ curated models** — Llama, Qwen, Gemma, Mistral, DeepSeek, Phi, and more
- **Automatic download** — selecting a model you haven't pulled yet triggers an in-app download with live progress
- **Configurable context window** — 4K to 256K tokens via slider
- **Local indicators** — models marked ✅ (downloaded) or ⬇️ (needs download)

### 🔔 Notifications
- **Desktop notifications** — workflow completions and timer expirations trigger a Windows desktop notification with timestamp
- **Sound effects** — distinct audio chimes for workflow completion (two-tone C5→E5) and timer alerts (5-beep A5), played asynchronously
- **In-app toasts** — transient toast messages appear in the UI with contextual emoji icons
- **Unified system** — all notification channels (desktop, sound, toast) fire from a single `notify()` call, keeping notification logic consistent across features

---

## 🔧 Tools (19 Tools / 42 Sub-tools)

Thoth's agent has access to 19 tools that expose 42 individual operations to the model. Tools can be enabled/disabled from the Settings panel.

### Search & Knowledge

| Tool | Description | API Key? |
|------|-------------|----------|
| **🔍 Web Search** | Live web search via Tavily for current events, news, real-time data | `TAVILY_API_KEY` |
| **🦆 DuckDuckGo** | Free web search — no API key needed | None |
| **🌐 Wikipedia** | Encyclopedic knowledge with contextual compression | None |
| **📚 Arxiv** | Academic/scientific paper search | None |
| **▶️ YouTube** | Search videos + fetch full transcripts/captions | None |
| **🔗 URL Reader** | Fetch and extract text content from any URL | None |
| **📄 Documents** | Semantic search over your uploaded files (FAISS vector store) | None |

### Productivity

| Tool | Description | API Key? |
|------|-------------|----------|
| **📧 Gmail** | Search, read, draft, and send emails (Google OAuth) | OAuth credentials |
| **📅 Google Calendar** | View, create, update, move, and delete events (Google OAuth) | OAuth credentials |
| **📁 Filesystem** | Sandboxed file operations — read, write, copy, move, delete within a workspace folder; reads PDF, CSV, Excel (.xlsx/.xls), JSON/JSONL, and TSV files; structured data files return schema + stats + preview via pandas | None |
| **⏰ Timer** | Desktop notification timers (max 24h), with list and cancel | None |

### Computation & Analysis

| Tool | Description | API Key? |
|------|-------------|----------|
| **🧮 Calculator** | Safe math evaluation — arithmetic, trig, logs, factorials, combinatorics | None |
| **🔢 Wolfram Alpha** | Advanced computation, symbolic math, unit conversion, scientific data | `WOLFRAM_ALPHA_APPID` |
| **🌤️ Weather** | Current conditions and multi-day forecasts via Open-Meteo | None |
| **👁️ Vision** | Camera/screen capture and analysis via vision model | None |
| **🧠 Memory** | Save, search, update, and delete long-term personal memories | None |
| **🔍 Conversation Search** | Search past conversations by keyword or list all saved threads | None |
| **🖥️ System Info** | OS, CPU, RAM, disk space, IP addresses, battery, and top processes | None |
| **📊 Chart** | Interactive Plotly charts — bar, line, scatter, pie, histogram, box, area, heatmap from data files | None |

### Safety & Permissions

- **Destructive operations require confirmation**: `file_delete`, `move_file`, `send_gmail_message`, `move_calendar_event`, `delete_calendar_event`, `delete_memory`
- **Filesystem is sandboxed**: only the configured workspace folder is accessible
- **Gmail/Calendar operations are tiered**: read, compose/write, and destructive tiers can be toggled independently
- **Tools can be individually disabled** from Settings to reduce model decision complexity

---

## 🏗️ Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                    NiceGUI Frontend (app_nicegui.py)                 │
│  ┌────────────┐  ┌──────────────────────┐  ┌───────────────────┐   │
│  │  Sidebar   │  │   Chat Interface     │  │   Settings Dialog │   │
│  │  Threads   │  │   Streaming Tokens   │  │   11 Tabs         │   │
│  │  Controls  │  │   Tool Status        │  │   Tool Config     │   │
│  └────────────┘  └──────────────────────┘  └───────────────────┘   │
└──────────────────────────┬───────────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────────┐
│               LangGraph ReAct Agent (agent.py)                       │
│                                                                      │
│   create_react_agent() with pre-model message trimming              │
│   System prompt with TOOL USE, MEMORY, and CITATION guidelines      │
│   Interrupt mechanism for destructive action confirmation            │
│                                                                      │
│   42 LangChain sub-tools from 19 registered tool modules            │
└───────┬──────────┬──────────┬──────────┬──────────┬─────────────────┘
        │          │          │          │          │
        ▼          ▼          ▼          ▼          ▼
  ┌──────────┐ ┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐
  │  Ollama  │ │ Memory │ │ SQLite │ │ FAISS  │ │External│
  │  LLMs    │ │   DB   │ │Threads │ │ Vector │ │  APIs  │
  │(local)   │ │(local) │ │(local) │ │ Store  │ │(opt-in)│
  └──────────┘ └────────┘ └────────┘ └────────┘ └────────┘
```

### Core Modules

| File | Purpose |
|------|---------|
| **`app_nicegui.py`** | NiceGUI UI — chat interface, sidebar thread manager with live token counter, Settings dialog (11 tabs), file attachment handling, streaming event loop with error recovery, export, voice bar |
| **`agent.py`** | LangGraph ReAct agent — system prompt, pre-model context trimming with proportional tool-output shrinking, streaming event generator, interrupt handling for destructive actions, live token usage reporting, contextual compression |
| **`threads.py`** | SQLite-backed thread metadata and `SqliteSaver` checkpointer for persisting LangGraph conversation state |
| **`memory.py`** | Long-term memory with SQLite CRUD and FAISS semantic vector search — save, search, list, update, delete, count across 6 categories; auto-rebuilds vector index on mutations |
| **`models.py`** | Ollama model management — listing, downloading, switching models, context size configuration |
| **`documents.py`** | Document ingestion — PDF/DOCX/TXT loading, chunking, FAISS embedding and storage |
| **`voice.py`** | Local STT pipeline — toggle-based 4-state machine (stopped/listening/transcribing/muted) with faster-whisper CPU-only int8 transcription |
| **`tts.py`** | Piper TTS integration — engine + default voice bundled with installer, additional voices downloaded on demand, streaming sentence-by-sentence playback |
| **`vision.py`** | Camera/screen capture via OpenCV/MSS, image analysis via Ollama vision models |
| **`data_reader.py`** | Shared pandas-based reader for CSV, TSV, Excel, JSON, JSONL — returns schema + stats + preview rows |
| **`launcher.py`** | Desktop launcher — system tray (pystray), native window management (pywebview), splash screen (tkinter subprocess), manages NiceGUI server lifecycle |
| **`api_keys.py`** | API key management — load/save/apply from `~/.thoth/api_keys.json` |
| **`memory_extraction.py`** | Background memory extraction — scans past conversations via LLM, extracts personal facts, deduplicates against existing memories (cosine > 0.85), runs on startup + every 6 hours |
| **`workflows.py`** | Workflow engine — SQLite CRUD, template variable expansion, sequential prompt execution, background runner with threading, scheduled execution (daily/weekly), desktop notifications, 4 default templates |
| **`notifications.py`** | Unified notification system — desktop notifications (plyer), sound effects (winsound), and in-app toast queue; coordinates workflow completion chimes and timer alerts |
| **`channels/`** | Messaging channel adapters — Telegram bot (long polling) and Email channel (Gmail polling), with shared config store |
| **`tools/`** | 19 self-registering tool modules + base class + registry |

### Data Storage

All user data is stored in `~/.thoth/` (`%USERPROFILE%\.thoth\` on Windows):

```
~/.thoth/
├── threads.db              # Conversation history & LangGraph checkpoints
├── memory.db               # Long-term memories
├── memory_vectors/         # FAISS vector index for semantic memory search
├── memory_extraction_state.json  # Tracks last extraction run timestamp
├── api_keys.json           # API keys (Tavily, Wolfram, etc.)
├── app_config.json         # Onboarding / first-run state
├── tools_config.json       # Tool enable/disable state & config
├── model_settings.json     # Selected model & context size
├── tts_settings.json       # Selected TTS voice
├── vision_settings.json    # Vision model & camera selection
├── voice_settings.json     # Whisper model size preference
├── processed_files.json    # Tracks indexed documents
├── workflows.db            # Workflow definitions, schedules & run history
├── timers.sqlite           # Scheduled timer jobs
├── channels_config.json    # Channel settings (Telegram, Email auto-start)
├── vector_store/           # FAISS index for uploaded documents
├── gmail/                  # Gmail OAuth tokens
├── calendar/               # Calendar OAuth tokens
└── piper/                  # Piper TTS engine & voice models
```

> Override the data directory by setting the `THOTH_DATA_DIR` environment variable.

---

## 💻 System Requirements

| Requirement | Minimum |
|-------------|---------|
| **OS** | Windows 10/11 (64-bit) |
| **RAM** | 8 GB (16 GB+ recommended for 14B+ models) |
| **Disk** | ~2 GB for app + packages, ~5–10 GB for LLM models |
| **GPU** | Optional — Ollama uses GPU if available (NVIDIA CUDA or AMD ROCm) |
| **Internet** | Required for installation; optional at runtime (only for online tools) |

---

## 📦 Installation (From Source)

1. **Install [Ollama](https://ollama.com/)** — download and install the Ollama runtime

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

5. **Start Ollama** (if not already running)
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
   python app_nicegui.py
   ```

> **First launch:** The default brain model (`qwen3:14b`) will be downloaded automatically if not already available. This is a one-time ~9 GB download.

---

## 🔑 API Key Setup (Optional)

Most tools work without any API keys. For enhanced functionality:

| Service | Key | Purpose | How to Get |
|---------|-----|---------|-----------|
| **Tavily** | `TAVILY_API_KEY` | Web search (1,000 free searches/month) | [app.tavily.com](https://app.tavily.com/) |
| **Wolfram Alpha** | `WOLFRAM_ALPHA_APPID` | Advanced computation & scientific data | [developer.wolframalpha.com](https://developer.wolframalpha.com/) |

Configure keys in **⚙️ Settings → 🔍 Search** tab. Keys are saved locally to `~/.thoth/api_keys.json`.

For **Gmail** and **Google Calendar**, you'll need a Google Cloud OAuth `credentials.json` — setup instructions are provided in the respective Settings tabs.

---

## 🚀 Quick Start

1. **Launch Thoth** and wait for the default model to download (first time only)
2. **Click "＋ New conversation"** in the sidebar
3. **Ask anything** — the agent will automatically choose which tools to use:
   - *"What's the weather in Tokyo?"* → uses Weather tool
   - *"Search for recent papers on transformer architectures"* → uses Arxiv
   - *"Remember that my mom's birthday is March 15"* → saves to Memory
   - *"Read the file report.pdf in my workspace"* → uses Filesystem
   - *"What's on my screen right now?"* → uses Vision (screen capture)
   - *"Set a timer for 10 minutes"* → uses Timer with desktop notification
   - *"What did I ask about taxes last week?"* → uses Conversation Search
4. **Open ⚙️ Settings** to configure models, enable/disable tools, and set up integrations

---

## 🔒 Privacy & Security

- **All LLM inference runs locally** via Ollama — no data sent to cloud AI providers
- **Documents, memories, and conversations are stored locally** in `~/.thoth/`
- **API keys are stored locally** and only transmitted to their respective services
- **External network calls** are only made when you use online tools (web search, Wikipedia, Arxiv, Gmail, Calendar, Weather) — and each can be individually disabled
- **No telemetry, no tracking, no cloud dependencies** for core functionality

---

## 📄 License

MIT License — see [LICENSE](LICENSE) for details.

---

## 🙏 Acknowledgements

Built with [NiceGUI](https://nicegui.io/), [LangGraph](https://langchain-ai.github.io/langgraph/), [LangChain](https://python.langchain.com/), [Ollama](https://ollama.com/), [FAISS](https://github.com/facebookresearch/faiss), [Piper TTS](https://github.com/rhasspy/piper), [faster-whisper](https://github.com/SYSTRAN/faster-whisper), and [HuggingFace](https://huggingface.co/).
