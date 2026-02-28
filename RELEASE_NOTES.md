# 𓁟 Thoth v1.1.0 — Sharpened Recall

**Thoth** is a local-first, privacy-focused knowledge agent that combines Retrieval-Augmented Generation (RAG) with multi-source information retrieval. Ask questions, upload your own documents, and get cited answers — all powered by a locally-running LLM via [Ollama](https://ollama.com/).

---

## ✨ What's New in v1.1.0

### RAG Pipeline Improvements

- **Contextual compression retrieval** — each retriever is now wrapped with a `ContextualCompressionRetriever` + `LLMChainExtractor` that filters and extracts only query-relevant content per document, replacing the previous single-pass LLM compression of concatenated results
- **Query rewriting** — follow-up questions with pronouns or references (e.g., "how are they related?") are automatically rewritten into standalone search queries using conversation history, so retrievers receive semantically complete queries
- **Parallel retrieval** — all enabled retrieval sources are queried simultaneously via `ThreadPoolExecutor`, reducing total retrieval time from the sum of all sources to the time of the slowest one
- **Context deduplication** — embedding-based cosine similarity deduplication at two levels:
  - *Within-retrieval*: removes near-duplicate documents returned by different sources in the same query
  - *Cross-turn*: prevents adding context that is too similar to already-accumulated context from previous turns
- **Character-based context & message trimming** — context entries and message history are trimmed to fit within a character budget (1 token ≈ 4.5 characters), preventing context window overflow in long conversations
- **Smarter context assessment** — `needs_context` now checks existing context relevance via fast embedding similarity before falling back to an LLM call, reducing unnecessary retrieval and LLM invocations

### UI Improvements

- **Auto-scroll** — the chat area now automatically scrolls to show new messages and the "Thinking…" spinner

---

## ✨ Features

- **Conversational Q&A** — multi-turn chat with persistent threads stored locally
- **Multi-source retrieval** — pulls context from your documents, Wikipedia, Arxiv, and the web (via Tavily)
- **Source citations** — every answer cites where each fact came from
- **Document management** — upload and index PDF, DOCX, DOC, and TXT files into a local FAISS vector store
- **Dynamic model switching** — choose from 30+ curated Ollama models, downloaded automatically on demand
- **Smart context assessment** — an LLM decides whether additional retrieval is needed before searching
- **In-app API key management** — configure API keys from the Settings panel, no file editing needed
- **Fully local** — your data stays on your machine. No cloud. No telemetry.

---

## 💻 System Requirements

| Requirement | Minimum |
|-------------|---------|
| **OS** | Windows 10/11 (64-bit) |
| **RAM** | 8 GB (16 GB recommended for larger models) |
| **Disk** | ~2 GB for app + Python packages, ~5 GB for default LLM model |
| **Internet** | Required during installation and for web/Wikipedia/Arxiv retrieval |

---

## 📦 Installation

### Option A: Windows Installer (Recommended)

1. **Download** `ThothSetup_1.1.0.exe` from the Assets section below
2. **Run the installer** — it will:
   - Install [Ollama](https://ollama.com/) silently (local LLM engine)
   - Install an embedded Python runtime and all required packages
   - Create Start Menu and Desktop shortcuts
3. **Wait for setup to complete** — package installation may take several minutes depending on your internet connection
4. **Launch Thoth** from the Start Menu or Desktop shortcut

> ⚠️ **Windows SmartScreen:** Since the installer is not code-signed, Windows may show a "Windows protected your PC" warning. Click **More info → Run anyway** to proceed.

### Option B: Run from Source

1. Install [Python 3.11+](https://python.org) and [Ollama](https://ollama.com/)
2. Clone the repository:
   ```bash
   git clone https://github.com/yourusername/thoth.git
   cd thoth
   ```
3. Create and activate a virtual environment:
   ```bash
   python -m venv .venv
   # Windows
   .venv\Scripts\activate
   # macOS / Linux
   source .venv/bin/activate
   ```
4. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
5. Start Ollama:
   ```bash
   ollama serve
   ```
6. Launch the app:
   ```bash
   streamlit run app.py
   ```

---

## 🔑 First-Time Setup: Configuring Your Tavily API Key

Thoth uses [Tavily](https://tavily.com/) for web search retrieval. You'll need a free API key to use the **🔍 Web Search** feature.

1. **Get a free Tavily API key** at [https://app.tavily.com/sign-in](https://app.tavily.com/sign-in)
2. **Launch Thoth** — the app opens in your browser at `http://localhost:8501`
3. In the **left sidebar**, click the **⚙️ Settings** button at the bottom
4. Scroll down to the **API Keys** section
5. Paste your Tavily key into the **Tavily (TAVILY_API_KEY)** field
6. The key is saved automatically and will persist across restarts

> **Note:** Web search works without a key configured — it will simply return no results. All other retrieval sources (documents, Wikipedia, Arxiv) work without any API key.

---

## 🚀 Usage Guide

### Starting a Conversation
1. Click **＋ New conversation** in the sidebar
2. Type your question in the chat input at the bottom
3. Thoth retrieves context from enabled sources and generates a cited answer
4. The thread is automatically named after your first question

### Uploading Documents
1. In the **📄 Documents** panel on the right, click the upload area
2. Select one or more PDF, DOCX, DOC, or TXT files
3. Files are chunked, embedded, and indexed into the local vector store
4. Uploaded documents are immediately available for retrieval in all conversations

### Switching Models
1. Open **⚙️ Settings** in the sidebar
2. Select a model from the dropdown — models marked ✅ are already downloaded, ⬇️ will be downloaded automatically
3. The default model is **qwen3:8b** (~5 GB download on first run)

### Configuring Retrieval Sources
In **⚙️ Settings**, toggle each source on/off:
- **📄 Documents** — search your uploaded files
- **🌐 Wikipedia** — search Wikipedia articles
- **📚 Arxiv** — search academic papers
- **🔍 Web Search** — search the web via Tavily (requires API key)

### Managing Conversations
- Click any conversation in the sidebar to resume it
- Click 🗑️ next to a thread to delete it
- All conversations persist across app restarts

---

## 📁 What Gets Installed

```
C:\Program Files\Thoth\
├── launch_thoth.bat          # Starts Ollama + the Streamlit app
├── python\                   # Embedded Python 3.13 runtime
│   └── Lib\site-packages\    # All Python packages
└── app\                      # Application source code
    ├── app.py, rag.py, ...
    └── vector_store\         # Your document index (created on first use)
```

Ollama is installed system-wide to `%LOCALAPPDATA%\Ollama`.

---

## 🔒 Privacy & Security

- **All LLM inference runs locally** via Ollama — no data is sent to cloud AI providers
- **Documents are indexed locally** in a FAISS vector store on your machine
- **API keys are stored locally** in `api_keys.json` — never transmitted except to their respective services
- **External network calls** are only made to: Tavily (web search), Wikipedia API, Arxiv API — and only when those sources are enabled

---

## 🐛 Known Issues

- **First launch is slow** — the default model (`qwen3:8b`, ~5 GB) must be downloaded by Ollama on first run
- **Windows SmartScreen warning** — the installer is not code-signed; click "More info → Run anyway"
- **Large documents** — very large PDFs may take a while to chunk and embed

---

## 📄 License

MIT License — see [LICENSE](LICENSE) for details.

---

## 🙏 Acknowledgements

Built with [Streamlit](https://streamlit.io/), [LangChain](https://python.langchain.com/), [LangGraph](https://langchain-ai.github.io/langgraph/), [Ollama](https://ollama.com/), [FAISS](https://github.com/facebookresearch/faiss), and [HuggingFace Transformers](https://huggingface.co/).
