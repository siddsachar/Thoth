<p align="center">
  <img src="docs/thoth_glyph_256.png" alt="Thoth" width="180">
</p>

<h1 align="center">Thoth</h1>

<p align="center">
   <a href="https://github.com/siddsachar/Thoth/releases"><img src="https://img.shields.io/github/v/release/siddsachar/Thoth?style=flat&label=release&color=c9a227" alt="Release"></a>
   <a href="https://github.com/siddsachar/Thoth/actions/workflows/ci.yml"><img src="https://github.com/siddsachar/Thoth/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
   <a href="LICENSE"><img src="https://img.shields.io/github/license/siddsachar/Thoth?style=flat" alt="License"></a>
   <img src="https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-c9a227?style=flat" alt="Platform">
</p>

Thoth is a local-first desktop AI assistant. It gives you chat, memory, tools, workflows, Developer Studio, Designer Studio, Custom Tools, plugins, messaging channels, and optional cloud models while keeping durable data on your machine.

It can run fully local with [Ollama](https://ollama.com/), 39 curated tool-calling models, local embeddings, and Ollama Cloud models exposed through a signed-in daemon. You can also opt into OpenAI, Anthropic, Google AI, xAI, MiniMax, OpenRouter, Ollama Cloud direct API, custom OpenAI-compatible endpoints, and ChatGPT / Codex subscription models.

The Thoth app has no account system, no Thoth-hosted server, and no telemetry pipeline. Provider keys and subscription tokens are stored in the OS credential store when available.

Download the latest installer from [GitHub Releases](https://github.com/siddsachar/Thoth/releases). Windows and macOS use one-click installers. Linux has a one-line user installer.

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

## What You Get

| Area | Details |
|------|---------|
| Agent and models | LangGraph ReAct agent, streaming responses, thinking bubbles, smart context trimming, 39 curated Ollama models, local and daemon-backed Ollama Cloud models, Ollama Cloud direct API, provider models, custom endpoints, ChatGPT / Codex subscription models, background model catalog cache, and per-thread, per-workflow, and per-Developer model overrides. |
| Memory and knowledge | Personal knowledge graph, 10 entity types, 67 typed relations, FAISS semantic recall, 1-hop graph expansion, graph visualization, Obsidian-compatible wiki export, document extraction with source provenance, Dream Cycle refinement, duplicate merging, stale-confidence decay, relationship inference, self-knowledge, insights, and conversation search. |
| Tools | 30+ core tool modules for web search, DuckDuckGo, Wikipedia, arXiv, YouTube transcripts, URL reading, documents, wiki vault, Gmail, Google Calendar, filesystem, shell, browser automation, workflows, tracker, channels, X, image generation/editing, video generation, MCP, Developer Studio, Designer Studio, Custom Tool Builder, status, calculator, Wolfram Alpha, weather, vision, memory, system info, and charts. File tools read PDF, CSV, Excel, JSON, JSONL, TSV, and image files, with schema, stats, previews, and PDF export where supported. |
| Developer Studio | Local Git workspace linking and cloning, code threads, repo inspector, file tree, diffs, todos, tests, branch, commit, push and PR prep, approval modes, and optional Docker Sandbox with a shadow workspace and explicit import back into the real repo. |
| Designer Studio | Decks, documents, landing pages, app mockups, and storyboards with a sandboxed interactive runtime, templates, brand controls, critique and repair, AI image and video generation, chart insertion, Mermaid and Plotly rendering, shareable HTML, and export to PDF, HTML, PNG, and PPTX. |
| Workflows | Scheduled runs, webhook triggers, task-completion triggers, step pipelines, conditions, approvals, subtasks, notification-only runs, concurrency groups, delivery defaults, per-workflow model/tool/skill overrides, safety modes, run status, run history, upcoming runs, and a Workflow Console. |
| Channels and voice | Telegram, WhatsApp, Discord, Slack, and SMS with streaming, reactions, media intake, voice transcription, document extraction, approval routing, health checks, auto-generated send/photo/document tools, and optional tunnel support. Voice uses local faster-whisper STT and Kokoro TTS with 10 voices. |
| Platform and app | Native desktop app, setup wizard, tray integration on Windows and macOS, desktop notifications, local browser-first Linux launch, optional Linux native window/tray mode, Home status bar for models, OAuth, MCP, plugins, documents, workflows, Buddy, logging, disk, and other local systems, plus verified auto-updates. |
| Extensibility | Sandboxed plugin marketplace, bundled skills and tool guides, external MCP clients over stdio, Streamable HTTP, and SSE, Custom Tools from repos or folders, Claude Code Delegation through an approval-gated CLI worker, migration from selected Hermes/OpenClaw data, setup center, identity settings, and stability diagnostics. |

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full subsystem reference.

## Install

### Windows

1. Download the latest [Windows installer](https://github.com/siddsachar/Thoth/releases/latest).
2. Run it. The installer bundles the embedded Python runtime, app source, and Python dependencies. Ollama is optional and only needed for local models.
3. Launch Thoth from the Start Menu or desktop shortcut.

User data lives in `%USERPROFILE%\.thoth`. Repairing or upgrading replaces the bundled runtime and preserves your data. Startup logs are written to `%USERPROFILE%\.thoth\thoth_app.log`, including recovery hints for known optional audio package issues such as TorchCodec.

### macOS

1. Download the latest [macOS DMG](https://github.com/siddsachar/Thoth/releases/latest).
2. Drag `Thoth.app` into Applications.
3. Launch Thoth from Applications or Launchpad.

The first run may ask you to confirm that the app was downloaded from the internet. The packaged app uses its bundled Python runtime and dependencies, and it starts Ollama if Ollama is already installed. Apple Silicon and Intel Macs are supported on macOS 12+.

If you only want provider models or a custom endpoint, you can skip model downloads during setup.

### Linux

Run:

```bash
curl -fsSL https://raw.githubusercontent.com/siddsachar/Thoth/main/installer/install-linux.sh | bash
```

To install a specific version:

```bash
curl -fsSL https://raw.githubusercontent.com/siddsachar/Thoth/main/installer/install-linux.sh | bash -s -- 3.22.0
```

The installer downloads the release tarball, verifies its SHA256 from the GitHub release manifest, installs under `~/.local/share/thoth`, creates `~/.local/bin/thoth`, and stores user data in `~/.thoth`. The default Linux build opens in your system browser. Native window and tray support are available when the required GTK, Qt, and AppIndicator libraries are installed.

Manual tarball install:

```bash
tar -xzf Thoth-X.Y.Z-Linux-x86_64.tar.gz
cd Thoth-X.Y.Z-Linux-x86_64
./install.sh
thoth
```

If `~/.local/bin` is not on `PATH`, run `~/.local/bin/thoth` or add it to your shell profile. On Linux, provider secrets use Secret Service or KWallet when available. WSL and headless systems can run without a keyring, but new secrets are session-only until secure storage is configured.

For browser automation, Chromium may need distro packages that the tarball cannot install. If Playwright reports missing dependencies, run the command it prints, or use `python -m playwright install --with-deps chromium` from a source checkout.

## Quick Start

On first launch, Thoth opens a setup wizard. Pick one of three paths:

| Mode | Use it when | Setup |
|------|-------------|-------|
| Local | You want models and embeddings on your machine. | Choose Ollama, download the default `qwen3:14b` brain model or a smaller model such as `qwen3:8b`, then start chatting. |
| Providers | You do not have a local GPU or want frontier models. | Add an OpenAI, Anthropic, Google AI, xAI, MiniMax, OpenRouter, or Ollama Cloud key, pick a default model, and save Quick Choices. ChatGPT / Codex sign-in is available in Settings after launch. |
| Custom/Self-hosted | You run LM Studio, vLLM, LocalAI, or a private gateway. | Enter an OpenAI-compatible base URL such as `http://127.0.0.1:1234/v1`, add a key if your server requires one, fetch models, and choose a default. |

Common first prompts:

- `Remember that my mom's birthday is March 15`
- `Search for recent papers on transformer architectures`
- `Read report.pdf in my workspace`
- `Run git status on my project`
- `Create a six-slide pitch deck for my startup`
- `Show my headache trends this month`
- `Remind me to call the dentist tomorrow at 9am`
- `Review this repo and suggest the highest-risk issues`
- `Turn this GitHub repo into a Custom Tool`
- `What did I ask about taxes last week?`

For LM Studio and similar local servers, use a context window large enough for Thoth's agent prompt and tool schemas. A `4096` context can fail before the first chat turn with misleading prompt-template errors. `32768` is a practical starting point.

## Models, Keys, and Integrations

Most tools work without API keys. Add keys only for the providers and integrations you use.

| Service | Key or setup | Used for |
|---------|--------------|----------|
| OpenAI | `OPENAI_API_KEY` | OpenAI models and image tools. |
| ChatGPT / Codex | In-app ChatGPT sign-in | Subscription-backed Codex models through ChatGPT's internal backend. |
| Anthropic | `ANTHROPIC_API_KEY` | Claude models through the direct API. |
| Google AI | `GOOGLE_API_KEY` | Gemini models, Imagen, and Veo. |
| xAI | `XAI_API_KEY` | Grok models, Grok Imagine, and Grok Imagine Video. |
| MiniMax | `MINIMAX_API_KEY` | MiniMax M2 models through the Anthropic-compatible API. |
| OpenRouter | `OPENROUTER_API_KEY` | Access to 100+ provider models. |
| Ollama Cloud | `OLLAMA_CLOUD_API_KEY` or local daemon sign-in | Direct Ollama Cloud models and cloud-tagged daemon models. |
| Tavily | `TAVILY_API_KEY` | Live web search. |
| Wolfram Alpha | `WOLFRAM_ALPHA_APPID` | Symbolic math, unit conversion, and scientific data. |
| Telegram | `TELEGRAM_BOT_TOKEN` | Telegram bot messaging. |
| Discord | `DISCORD_BOT_TOKEN` | Discord DM messaging. |
| Slack | `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` | Slack DM messaging through Socket Mode. |
| Twilio | `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` | SMS. |
| X | `X_CLIENT_ID` / `X_CLIENT_SECRET` | X API v2 OAuth 2.0 PKCE for search, timeline, mentions, posting, replies, quotes, likes, reposts, bookmarks, and deletes. |
| ngrok | `NGROK_AUTHTOKEN` | Tunnels for inbound webhooks. |
| Gmail and Google Calendar | Google Cloud OAuth `credentials.json` | Email search/read/draft/send and calendar view/create/update/move/delete. |

Configure providers in Settings, Channels, and Accounts. Keys and in-app ChatGPT / Codex tokens are stored in Windows Credential Manager, macOS Keychain, or Linux Secret Service/KWallet when available. `~/.thoth/api_keys.json` and `~/.thoth/providers.json` keep metadata only, such as saved state, provider status, Quick Choices, and masked fingerprints.

Embedding providers are configured separately from chat models. Local embeddings are available for private document and vector indexing. Optional cloud embeddings show a privacy warning because document text is sent to the selected embedding provider.

External Codex CLI login files are metadata/reference only. Thoth can detect that a CLI login exists, but direct Codex runtime requires the in-app ChatGPT sign-in and does not copy runnable tokens from `~/.codex/auth.json`.

## Tools and Safety

Thoth's tools can be enabled or disabled from Settings. Many tools expose multiple operations, Developer Studio adds code-specific tools, Custom Tools can be promoted after review, and running channels add send/photo/document tools automatically.

| Group | Included tools |
|-------|----------------|
| Search and knowledge | Tavily web search, DuckDuckGo, Wikipedia, arXiv, YouTube transcripts, URL reader, document search, wiki vault, memory graph, and conversation search. |
| Productivity | Gmail, Google Calendar, filesystem, shell, visible Chromium browser automation, workflows, tracker, channel tools, and X. |
| Media and design | Designer Studio, image generation/editing through OpenAI, Google, and xAI, video generation through Google Veo and xAI Grok Imagine Video, chart insertion, Mermaid, Plotly, and media persistence. |
| Developer and extensibility | Developer Studio, Custom Tool Builder, promoted Custom Tools, external MCP tools, plugin tools, Claude Code Delegation, and Thoth Status. |
| Analysis | Calculator, Wolfram Alpha, weather, vision for camera/screen/workspace images, system info, and Plotly charts with PNG export. |

Safety controls are built into the tool layer:

- Destructive operations require confirmation, including file delete/move, moderate-risk shell commands, Gmail send, calendar move/delete, memory delete, tracker delete, and task delete.
- Filesystem access is sandboxed to the configured workspace folder, which defaults to `~/Documents/Thoth`.
- Shell commands are classified as safe, moderate, or blocked. High-risk commands such as `shutdown`, `reboot`, and `mkfs` are blocked.
- Background workflows can have per-task command prefix and email-recipient allowlists.
- Browser tabs are isolated per thread and cleaned up when tasks or threads finish.
- Developer Studio has its own approval modes for edits, commands, Git operations, commits, pushes, and PR prep.
- Docker Sandbox is opt-in and runs commands in a shadow workspace until you explicitly import changes.
- Custom Tools are reviewed, smoke-tested, enabled, promoted, disabled, and removed without deleting their source repos.
- Gmail and Calendar permissions are tiered for read, compose/write, and destructive actions.
- MCP servers stay disabled until tested. External tools are namespaced, destructive MCP tools require approval, and broken servers degrade to diagnostics instead of blocking startup.
- Prompt-injection defense scans tool outputs and user inputs for instruction override attempts, role impersonation, data exfiltration, encoding evasion, and social engineering patterns.

## Architecture

Thoth is organized around local orchestration, context assembly, memory, workflows, channels, Designer Studio, Developer Studio, plugin/MCP boundaries, and safety controls.

Explore the visual architecture gallery: [docs/architecture.html](docs/architecture.html)

Read the full architecture reference: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#core-modules)

<table>
   <tr>
      <td align="center"><a href="docs/Core_Agent_arch.jpg"><img src="docs/Core_Agent_arch.jpg" width="360" alt="Thoth core agent architecture"></a><br><strong>Core Agent</strong></td>
      <td align="center"><a href="docs/Context_arch.jpg"><img src="docs/Context_arch.jpg" width="360" alt="Thoth context architecture"></a><br><strong>Context Assembly</strong></td>
   </tr>
   <tr>
      <td align="center"><a href="docs/Memory_arch.jpg"><img src="docs/Memory_arch.jpg" width="360" alt="Thoth memory system architecture"></a><br><strong>Memory System</strong></td>
      <td align="center"><a href="docs/Workflows_arch.jpg"><img src="docs/Workflows_arch.jpg" width="360" alt="Thoth background workflow architecture"></a><br><strong>Background Workflows</strong></td>
   </tr>
   <tr>
      <td align="center"><a href="docs/Channels_arch.jpg"><img src="docs/Channels_arch.jpg" width="360" alt="Thoth multi-channel architecture"></a><br><strong>Multi-Channel Runtime</strong></td>
      <td align="center"><a href="docs/Designer_arch.jpg"><img src="docs/Designer_arch.jpg" width="360" alt="Thoth Designer Studio architecture"></a><br><strong>Designer Studio</strong></td>
   </tr>
   <tr>
      <td align="center"><a href="docs/Safety_arch.jpg"><img src="docs/Safety_arch.jpg" width="360" alt="Thoth safety privacy and control architecture"></a><br><strong>Safety, Privacy &amp; Control</strong></td>
      <td align="center"><a href="docs/Self_Aware_arch.jpg"><img src="docs/Self_Aware_arch.jpg" width="360" alt="Thoth self-awareness and self-management architecture"></a><br><strong>Self-Awareness</strong></td>
   </tr>
</table>

## System Requirements

| Setup | Minimum | Recommended |
|-------|---------|-------------|
| Local Ollama models | Windows 10/11 64-bit, macOS 12+, or glibc Linux x86_64; Python 3.11+; 8 GB RAM for 8B models; about 5 GB disk for the app and one small model; internet for install and model download. | 16 to 32 GB RAM for 14B to 30B models; NVIDIA GPU with 8+ GB VRAM or Apple Silicon for much faster inference; 20+ GB disk for multiple or larger models. |
| Provider/custom models only | Windows 10/11 64-bit, macOS 12+, or glibc Linux x86_64; Python 3.11+; 4 GB RAM; about 1 GB disk; internet for provider inference. | No GPU required. Use this path if you do not want local model downloads. |
| Developer Sandbox | Docker Desktop or a compatible Docker/Podman runtime. | Optional. Developer Studio also works with local execution in the selected repo. |

The default local brain model is `qwen3:14b`, about 9 GB. It works on CPU with 16 GB RAM, but a GPU is faster. Smaller models such as `qwen3:8b`, about 5 GB, are better for 8 GB machines.

## From Source

Install [Ollama](https://ollama.com/) first if you want local models. Provider-only and custom-endpoint setups can skip local model downloads.

```bash
git clone https://github.com/siddsachar/Thoth.git
cd Thoth
python -m venv .venv
```

Activate the environment:

```bash
# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate
```

Install dependencies and launch:

```bash
pip install -r requirements.txt
python launcher.py
```

On Windows and macOS, `launcher.py` starts the tray icon and opens the app on the first available local port, normally `http://localhost:8080`. On Linux it opens in the browser without a tray by default. If port 8080 is busy, Thoth picks the next free port.

Headless Linux/server mode:

```bash
python launcher.py --server --no-open --port 8080
```

Direct app launch:

```bash
python app.py
```

Direct launches default to `http://localhost:8080`. Set `THOTH_PORT` to choose a different port.

## Privacy

Local models run through Ollama on your machine. Documents, memories, conversations, knowledge graph data, workflows, logs, and user settings are stored locally under `~/.thoth` or the platform-specific app data paths used by the installer.

Provider and custom models are opt-in. When selected, the current conversation, model-visible tool context, and tool results are sent to that endpoint. Memories, documents, files, graph data, and other conversations stay local unless you explicitly include them in the current conversation or expose them through a tool result.

Developer Studio only touches repos you link or clone. Local execution runs in that repo. Docker Sandbox runs in a shadow copy and requires explicit import before changing the real repo. Custom Tools are opt-in, testable, removable, and only appear in normal chat after promotion.

Thoth does not require a Thoth account, and there is no Thoth-hosted middleman for provider calls.

## Project Docs

- [Architecture](docs/ARCHITECTURE.md)
- [Visual architecture gallery](docs/architecture.html)
- [Contributing guide](CONTRIBUTING.md)
- [Branching strategy](docs/BRANCHING.md)
- [Release process](docs/RELEASING.md)
- [Security policy](SECURITY.md)
- [Code of conduct](CODE_OF_CONDUCT.md)

All changes should go through a pull request. `main` is intended to stay releasable.

## License

Apache 2.0. See [LICENSE](LICENSE).

## Acknowledgements

Built with [NiceGUI](https://nicegui.io/), [LangGraph](https://langchain-ai.github.io/langgraph/), [LangChain](https://python.langchain.com/), [Ollama](https://ollama.com/), [FAISS](https://github.com/facebookresearch/faiss), [Kokoro TTS](https://github.com/thewh1teagle/kokoro-onnx), [faster-whisper](https://github.com/SYSTRAN/faster-whisper), [HuggingFace](https://huggingface.co/), and [tiktoken](https://github.com/openai/tiktoken).
