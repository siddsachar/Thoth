<p align="center">
  <img src="docs/row_bot_glyph_256.png" alt="Row-Bot" width="180">
</p>

<h1 align="center">Row-Bot</h1>

<p align="center"><sub>(formerly Thoth)</sub></p>

<p align="center">
   <a href="https://github.com/siddsachar/row-bot/releases"><img src="https://img.shields.io/github/v/release/siddsachar/row-bot?style=flat&label=release&color=4F78A4" alt="Release"></a>
   <a href="https://github.com/siddsachar/row-bot/actions/workflows/ci.yml"><img src="https://github.com/siddsachar/row-bot/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
   <a href="LICENSE"><img src="https://img.shields.io/github/license/siddsachar/row-bot?style=flat" alt="License"></a>
   <img src="https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-4F78A4?style=flat" alt="Platform">
</p>

Row-Bot is a local-first desktop AI assistant for doing real work with models,
memory, and tools. Its name is the operating model: **Reason** through messy
context, **Orchestrate** tools and model providers, and **Work** inside the
files, repos, workflows, and channels you choose.

It combines chat, durable memory, tool use, Agent Profiles, Goal Mode,
automatic parent-led agent orchestration, profile-first workflows, Developer
Studio, Designer Studio, Smart Skills, Skills Hub, Custom Tools, Plugin System
v2, progressive external-tool and skill discovery, context metering and rolling
compaction, provider-aware reasoning controls, messaging channels,
authenticated multi-device owner access with durable trusted addresses,
opt-in native Computer Use, realtime voice, and provider-aware model routing.
Durable app data stays local by default.

For larger tasks, Row-Bot can keep a visible goal, run the thread through a
focused Agent Profile, and orchestrate scoped child agents for research, review,
implementation, or follow-up work. The original parent remains responsible for
joining required results and answering, while durable checkpoints preserve
approvals, steering, retries, stops, and recovery. Checkpoint-safe work budgets
and application-wide delegation limits keep long or repetitive runs bounded and
visible. Parallel writers can be assigned to distinct existing local folders as
separate Developer workspaces; folder-scoped locks let those children work
concurrently while preserving one writer at a time inside any shared folder.
If an app restart interrupts delegation or owned shell work, Row-Bot closes
unanswered tool calls without replaying them and resumes the saved parent when
its required child results are ready.

Recommended Auto capability loading keeps permitted core tools directly
available and searches enabled MCP, plugin, Custom Tool, and channel
capabilities only when a request needs them. Enabled manual and plugin skills
can be selected for the current parent or child task under the same profile,
approval, workspace, and execution-budget boundaries. For long conversations,
the responsive desktop composer meters the complete next model input and
Row-Bot can compact complete older turns into durable untrusted reference
context while preserving the newest turn and atomic tool-call/result groups.
It validates the rebuilt prompt before saving and fails with an exact capacity
message when the fixed prompt and tool schemas cannot fit the selected window.

Choose the model path that fits the task: local models through
[Ollama](https://ollama.com/); provider keys for OpenAI, Anthropic, Google AI,
xAI, MiniMax, OpenRouter,
[Atlas Cloud](https://www.atlascloud.ai/?utm_source=github&utm_medium=link&utm_campaign=row_bot),
[Requesty](https://requesty.ai/),
Ollama Cloud, OpenCode Zen, and OpenCode Go; subscription or OAuth sign-in for
ChatGPT / Codex, Claude Subscription, and xAI Grok; or custom
OpenAI-compatible endpoints such as oMLX, LM Studio, vLLM, llama.cpp, LocalAI,
LiteLLM, and SGLang. Row-Bot keeps provider identity, capability labels,
reasoning choices, context limits, media surfaces, and chat-only fallbacks
explicit so local, hosted, subscription, and self-hosted models can sit side by
side.

Row-Bot itself has no account system, no Row-Bot-hosted inference server, and
no first-party telemetry pipeline. Provider calls go to the provider or
endpoint you choose, and provider keys, OAuth tokens, and subscription tokens
are stored in the OS credential store when available. Official Docker
deployments use a separate persistent encryption-key volume and encrypted
credential records instead. The optional Computer Use beta depends on Cua
Driver, whose separately disclosed upstream telemetry must be accepted before
Row-Bot installs or invokes it.

Download the latest installer from [GitHub Releases](https://github.com/siddsachar/row-bot/releases). Windows and macOS use one-click installers. Linux has a one-line user installer.

<table align="center">
  <tr>
    <td align="center"><a href="https://youtu.be/GA2Tnlt4jNk"><img src="https://img.youtube.com/vi/GA2Tnlt4jNk/maxresdefault.jpg" width="360" alt="Turn Research Into a Client-Ready Report with Row-Bot"></a><br><sub><a href="https://youtu.be/GA2Tnlt4jNk">Turn Research Into a Client-Ready Report with Row-Bot</a></sub></td>
    <td align="center"><a href="https://youtu.be/wOUSGTyfEpk"><img src="https://img.youtube.com/vi/wOUSGTyfEpk/maxresdefault.jpg" width="360" alt="Turn Your Inbox Into an Action Plan with Row-Bot"></a><br><sub><a href="https://youtu.be/wOUSGTyfEpk">Turn Your Inbox Into an Action Plan with Row-Bot</a></sub></td>
  </tr>
  <tr>
    <td align="center"><a href="https://youtu.be/Vuk2xz-vPcA"><img src="https://img.youtube.com/vi/Vuk2xz-vPcA/maxresdefault.jpg" width="360" alt="Create a Background AI Workflow with Row-Bot"></a><br><sub><a href="https://youtu.be/Vuk2xz-vPcA">Create a Background AI Workflow with Row-Bot</a></sub></td>
    <td align="center"><a href="https://youtu.be/hRLuOEqbsds"><img src="https://img.youtube.com/vi/hRLuOEqbsds/maxresdefault.jpg" width="360" alt="Create Launch Campaign Designs with Row-Bot Designer Studio"></a><br><sub><a href="https://youtu.be/hRLuOEqbsds">Create Launch Campaign Designs with Row-Bot Designer Studio</a></sub></td>
  </tr>
</table>

## What You Get

| Area | Details |
|------|---------|
| Agent orchestration | LangGraph ReAct agent, Goal Mode, Agent Profiles, Profile Library, automatic parent-led child-agent orchestration, required and detached work, dependency ordering, multi-wave live joins, ordered steering and approvals, transient retry, folder-scoped parallel writers, orphan-only checkpoint repair, explicit parent restart recovery, compact Agent groups and cards, exactly-once completion, checkpoint-safe work budgets, repeated-action protection, configurable nesting/concurrency/active-time limits, profile/tool allowlists, promoted Agent-run workflows, generation-scoped cancellation, complete-input context metering, fixed-envelope preflight, recoverable capacity-aware rolling compaction, and per-thread, per-workflow, per-profile, and per-Developer model overrides. |
| Models and providers | Provider-qualified model selection, exact per-thread/per-model reasoning effort, toggle, and budget controls, readiness routing, chat-only fallback for non-tool models, chat/agent/vision/image/video capability labels, native Ollama tool-capability detection with maintained-family fallback, model-scoped custom endpoint profiles and probes, detected/manual/custom context caps, provider-scoped credential-backed live catalog discovery with last-known-good preservation, xAI Grok OAuth, ChatGPT / Codex and Claude Subscription providers, native OpenCode gateway discovery and per-model transport routing, provider-scoped tool-schema compatibility, phased OpenAI-compatible timeouts with safe pre-stream retry, prompt-cache diagnostics, and background model cache. |
| Memory and knowledge | Personal knowledge graph, 10 entity types, 67 typed relations, bounded semantic/lexical/graph recall, a disclosed checked-by-default local embedding setup download, cache-only normal recall, explicit repair, fast lexical/graph fallback, durable bounded document batches, streamed upload hashing and deduplication, atomic sharded vectors, resumable extraction, queue controls and health repair, audit and review states, recall traces, graph visualization, Obsidian-compatible wiki export with source provenance, Dream Cycle refinement, duplicate merging, stale-confidence decay, relationship inference, self-knowledge, insights, and conversation search. |
| Tools | 30+ core tool modules for web search, DuckDuckGo, Wikipedia, arXiv, YouTube transcripts, URL reading, documents, wiki vault, Gmail, Google Calendar, filesystem, shell, visible browser automation, opt-in native Computer Use, workflows, Goal Mode, child-agent delegation, tracker, channels, X, image generation/editing, video generation, MCP, Developer Studio, Designer Studio, Custom Tool Builder, status, calculator, Wolfram Alpha, weather, vision, memory, system info, and charts. Recommended Auto loading keeps core profile tools direct and searches enabled external MCP, plugin, Custom Tool, and channel schemas on demand; eager compatibility mode remains available. File tools read PDF, CSV, Excel, JSON, JSONL, TSV, and image files, with schema, stats, previews, and PDF export where supported. |
| Developer Studio | Local Git workspace linking and cloning, code threads, explicit existing-folder assignment for child Agents, folder-scoped writer locks, per-thread and child-agent worktrees, repo inspector, file tree, diffs, todos, tests, branch, commit, push and PR prep, approval modes, and optional Docker Sandbox with a shadow workspace and explicit import back into the real repo. Docker Sandbox intentionally fails closed inside the official Row-Bot server container instead of nesting or falling back silently. |
| Designer Studio | Decks, documents, landing pages, app mockups, and storyboards with a sandboxed interactive runtime, templates, brand controls, critique and repair, AI image and video generation, chart insertion, Mermaid and Plotly rendering, shareable HTML, and export to PDF, HTML, PNG, and PPTX. |
| Workflows | Scheduled runs, webhook triggers, task-completion triggers, step pipelines, conditions, approvals, subtasks, notification-only runs, concurrency groups, delivery defaults, profile-first workflow agents, promoted Agent-run workflows, per-workflow model/tool/skill/profile overrides, safety modes, run status, run history, upcoming runs, and a Workflow Console. |
| Controlled self-evolution | Structured self-reflection, bounded change proposals, reviewable execution boundaries, persistence, Dream Cycle and memory integration, and Command Center/status visibility for improvement work that stays explicit and auditable. |
| Channels and voice | Telegram, WhatsApp, Discord, Slack, SMS, and plugin-owned channels with platform-aware live streaming, typing and edit fallbacks, interactive approvals, durable child-agent and Goal Mode notices, media intake, voice transcription, document extraction, health checks, auto-generated send/photo/document tools, and optional tunnel support. SMS remains final-text-only. Realtime voice adds provider-backed voice sessions, action handling, speech/cue policy, and local faster-whisper or FunASR/SenseVoice STT plus Kokoro TTS options. |
| Platform and app | Native desktop app plus authenticated single-owner desktop and compact browser access; one-time invitations, durable revocable sessions, exact trusted-address add/remove controls, assigned-interface route discovery, route and device management, Tailscale Serve, browser-local voice, and strict HTTP/WebSocket origin gates; authenticated headless `serve` mode; official hardened Docker/VPS deployment and multi-architecture GHCR images; opt-in Computer Use setup, live takeover, and permission recovery on Windows and macOS; installable PWA support; tray integration on Windows and macOS; local browser-first Linux launch; Home status surfaces; recovery tools; verified auto-updates; and a searchable public user guide. |
| Extensibility | Smart Skills, pinned skills, slash commands, Skills Hub browsing/import/search, automatic per-task discovery of enabled manual and plugin skills, Plugin System v2 for native tools, MCP-backed tools, bundled skills, and channels, sandboxed Plugin Center and marketplace, bundled skills and tool guides, Agent Profiles, child-agent tools, Goal Mode tools, external MCP clients over stdio, Streamable HTTP, and SSE, Custom Tools from repos or folders, hardened Custom Tool Builder setup, Claude Code Delegation through an approval-gated CLI worker, migration from selected Hermes/OpenClaw data, setup center, identity settings, and stability diagnostics. |

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full subsystem reference.

## Install

### Windows

1. Download the latest [Windows installer](https://github.com/siddsachar/row-bot/releases/latest).
2. Run it. The installer bundles the embedded Python runtime, app source, and Python dependencies. Ollama is optional and only needed for local models.
3. Launch Row-Bot from the Start Menu or desktop shortcut.

User data lives in `%USERPROFILE%\.row-bot`. Repairing or upgrading replaces the bundled runtime and preserves your data. Startup logs are written to `%USERPROFILE%\.row-bot\row_bot_app.log`, including recovery hints for known optional audio package issues such as TorchCodec.

### macOS

1. Download the latest [macOS DMG](https://github.com/siddsachar/row-bot/releases/latest).
2. Drag `Row-Bot.app` into Applications.
3. Launch Row-Bot from Applications or Launchpad.

The first run may ask you to confirm that the app was downloaded from the internet. The packaged app uses its bundled Python runtime and dependencies, and it starts Ollama if Ollama is already installed. Apple Silicon and Intel Macs are supported on macOS 12+.

If you only want provider models or a custom endpoint, you can skip model downloads during setup.

### Linux

Run:

```bash
curl -fsSL https://raw.githubusercontent.com/siddsachar/row-bot/main/installer/install-linux.sh | bash
```

To install a specific version:

```bash
curl -fsSL https://raw.githubusercontent.com/siddsachar/row-bot/main/installer/install-linux.sh | bash -s -- 4.8.0
```

The installer downloads the release tarball, verifies its SHA256 from the GitHub release manifest, installs under `~/.local/share/row-bot`, creates `~/.local/bin/row-bot`, and stores user data in `~/.row-bot`. The default Linux build opens in your system browser. Native window and tray support are available when the required GTK, Qt, and AppIndicator libraries are installed.

Manual tarball install:

```bash
tar -xzf Row-Bot-X.Y.Z-Linux-x86_64.tar.gz
cd Row-Bot-X.Y.Z-Linux-x86_64
./install.sh
row-bot
```

If `~/.local/bin` is not on `PATH`, run `~/.local/bin/row-bot` or add it to your shell profile. On Linux, provider secrets use Secret Service or KWallet when available. WSL and headless systems can run without a keyring, but new secrets are session-only until secure storage is configured. For persistence in headless Linux, run Row-Bot inside a D-Bus session with a Secret Service backend such as `gnome-keyring-daemon`, or explicitly configure another secure Python keyring backend such as an encrypted file keyring.

For browser automation, Chromium may need distro packages that the tarball cannot install. If Playwright reports missing dependencies, run the command it prints, or use `python -m playwright install --with-deps chromium` from a source checkout.

### Docker / Headless Server

The official image is a complete single-owner server deployment for amd64 and
arm64. From the repository root, pin the release and start the hardened
loopback-only Compose profile:

```bash
export ROW_BOT_IMAGE=ghcr.io/siddsachar/row-bot:4.8.0
docker compose -f deploy/docker/compose.yaml up --detach
docker compose -f deploy/docker/compose.yaml ps
```

After the service is healthy, create the first one-time owner invitation from a
trusted terminal:

```bash
docker compose -f deploy/docker/compose.yaml exec row-bot \
  row-bot access invite --layout desktop --origin http://127.0.0.1:8080
```

Compose persists app data and a separately mounted encryption key in two named
volumes. Back up both together. The container does not grant owner access from a
Docker bridge address, and every browser must authenticate. Read
[Docker And VPS Operations](https://row-bot.ai/docs/operations/docker) before
adding LAN, Tailscale, SSH, reverse-proxy, or public VPS reachability.

### Upgrading from Thoth 3.x

Row-Bot v4 is the renamed successor to Thoth. Current Row-Bot releases no
longer run the old automatic Thoth-to-Row-Bot rebrand migration during startup.
That migration shipped for multiple Row-Bot releases after the v4 rename and
has now been removed from the hot startup path.

Users still on Thoth or an early Row-Bot build should first install and launch
a previous migration-capable Row-Bot release, then upgrade to the current
release. The current Migration Wizard remains focused on selected
Hermes/OpenClaw data and is separate from the removed Thoth rebrand bridge.

## Quick Start

On first launch, Row-Bot opens a setup wizard. Pick one of three paths:

| Mode | Use it when | Setup |
|------|-------------|-------|
| Local | You want inference and embeddings on your machine. | Choose a local runtime, download a recommended model such as `qwen3:14b` or a smaller model such as `qwen3:8b`, then start chatting. Ollama is the supported local runtime today. |
| Providers | You want hosted models, frontier reasoning, media generation, or no local model download. | Add an OpenAI, Anthropic, Google AI, xAI, MiniMax, OpenRouter, Atlas Cloud, Requesty, Ollama Cloud, OpenCode Zen, or OpenCode Go key, refresh live catalogs where available, pick a default model, and save Quick Choices. ChatGPT / Codex, Claude Subscription, and xAI Grok OAuth sign-in are available in Settings after launch. |
| Custom/Self-hosted | You run oMLX, LM Studio, vLLM, llama.cpp, LocalAI, LiteLLM, SGLang, or a private gateway. | Enter an OpenAI-compatible base URL such as `http://127.0.0.1:1234/v1`, choose the closest compatibility profile, add a key if your server requires one, fetch models, probe the exact model, verify or declare its server context, and choose a default. |

For routine chats, use Row-Bot normally. For longer work, create a Goal so
progress and blockers stay visible, choose an Agent Profile for the role you
want, and let the parent orchestrate child agents when subtasks can run
separately under tighter tool and approval boundaries. Required children join
the same live parent turn; detached children can continue without blocking it.
When independent children need to write in parallel, give each one a distinct
existing local folder as its Developer workspace. Changing only a shell working
directory does not partition writer ownership, and children assigned to the
same folder still serialize. Use child worktrees when the task instead needs
Git-backed branch isolation.

Capability loading defaults to the recommended Auto mode. Core tools permitted
by the active Agent Profile stay directly available; enabled external tools and
skills are searched locally and loaded for the current task as needed. Use
`Settings -> Tools -> Capability loading` to select eager compatibility mode
for an older provider or integration that requires every external schema.

Supported models expose a **Thinking** control beside the desktop model picker
and inside compact mobile Chat controls. The available effort, On/Off, and
token-budget choices come from the exact provider-qualified model and persist
only for that model in that chat. Use `/reasoning` in Chat or a connected
messaging channel to inspect or change the same setting; Provider default sends
no per-thread override and remains the compatibility choice.

To use the same running Row-Bot from another computer, phone, or tablet, open
`Settings -> System -> Remote Access` on an authorized owner device. Select a
current route or add one exact trusted HTTP or HTTPS address, create a one-time
invitation with either desktop or compact presentation, then open or scan it on
the new device. Both layouts receive the complete owner product, including
Settings. The host must remain running, every resulting device session can be
revoked independently, and a saved trusted address can be removed when the
route is no longer used.

To let an interactive local task operate a native Windows or macOS app, open
`Settings -> System -> Browser & Computer Use`. Review the Cua Driver telemetry
notice, explicitly install the checksum-verified private runtime, grant the
requested operating-system permissions, and complete the Calculator test.
Computer Use is off by default and unavailable to channels, schedules,
background workflows, child agents, and headless/server callers. Browser
automation remains the preferred tool for websites.

Common first prompts:

- `Remember that my mom's birthday is March 15`
- `Search for recent papers on transformer architectures`
- `Read report.pdf in my workspace`
- `Run git status on my project`
- `Open Calculator and work out the total from these figures`
- `Create a goal to update the release docs and track blockers`
- `Delegate competitor research to a focused child agent and summarize the risks`
- `Create a six-slide pitch deck for my startup`
- `Show my headache trends this month`
- `Remind me to call the dentist tomorrow at 9am`
- `Review this repo and suggest the highest-risk issues`
- `Turn this GitHub repo into a Custom Tool`
- `What did I ask about taxes last week?`

For local and self-hosted servers, keep context on Auto or choose a verified
window large enough for Row-Bot's complete prompt, images, history, and tool
schemas. Ollama Auto targets 65,536 tokens and remains capped by native or
observed capacity; fixed 32K is still available for smaller-memory setups.
Custom endpoints use detected or manually declared server capacity in Auto and
stay unavailable when that capacity is unknown until you declare it or set a
verified Custom cap. Exact advanced values from 16,384 through 4,194,304 tokens
are accepted, but an app cap does not reconfigure the server: set llama.cpp
`--ctx-size` or the equivalent server option separately. The desktop meter
shows the estimated next input and automatic rolling compaction begins at 75
percent of the effective window when capacity is known. Models that are useful
for normal conversation but not reliable with tools can still run through
chat-only mode.

## Remote Access And Server Mode

Row-Bot is a **single-owner, multi-device** application. Remote Access lets one
owner use the same local instance from several trusted browsers; it is not a
multi-user hosting or tenant-isolation system. Normal desktop launches remain
local-first and keep the existing loopback owner experience. First-class server
mode is stricter: every browser must authenticate, including a browser reaching
the server through loopback.

An invitation and a session are different:

- An **invitation** is a secret link used to add one device. It expires after
  10 minutes and can be consumed only once, when the recipient explicitly
  presses **Connect**.
- A **session** is the separate HttpOnly browser credential created after the
  invitation is accepted. A trusted-device session lasts up to 30 days; a
  temporary session lasts up to 12 hours. Sessions survive normal restarts and
  can be revoked without changing any invitation.
- A **desktop** or **compact** invitation changes only the initial presentation.
  Every authenticated browser receives full owner access, including Settings.
  Rich Developer and Designer editors remain desktop-layout-oriented.

Treat an unused invitation like a password until it expires. Row-Bot stores
only hashed invitation and session secrets in the existing `mobile.db` access
database; raw session secrets are never placed in URLs.

### Choose A Reachability Path

| Path | Recommended use | Boundary |
|------|-----------------|----------|
| Local desktop | The browser and Row-Bot run on the same computer. | Default desktop launches stay on loopback; no remote listener is enabled. |
| Tailscale Serve | Recommended private access across trusted computers and networks. | Tailnet reachability does not replace a Row-Bot invitation or session. Row-Bot never installs Tailscale, signs in, enables Funnel, resets Serve, or overwrites another app's route automatically. |
| Direct LAN | A trusted network where the host can listen on an assigned interface address. | Enabling LAN is explicit and may restart the child app. Plain HTTP is unencrypted, and a non-private interface address may be externally routable: verify firewall exposure and prefer Tailscale or HTTPS outside a trusted LAN. A reachable client still is not authorized. |
| Trusted hostname or address | An exact DNS alias, hostname, or browser-facing IP and optional port that you already control. | Saving the origin changes Row-Bot Host/origin admission only. It does not configure DNS, TLS, a proxy, the firewall, reachability, or the listen address. |
| SSH tunnel | An operator can SSH to a Linux host or VPS and wants no externally published Row-Bot port. | Keep Row-Bot on loopback and forward a workstation port. The Row-Bot session gate still applies. |
| Docker | A headless, isolated, reproducible instance. | The supplied Compose file publishes to host loopback by default and persists `/data`; Docker bridge or gateway addresses never count as owner identity. |
| HTTPS reverse proxy or VPS | An operator manages DNS, TLS, proxy trust, backups, and upgrades. | Use one dedicated canonical origin and trust only the exact proxy address or CIDR that connects to Row-Bot. |

Settings shows reachability and authorization separately. “LAN enabled” or an
active Tailscale route means a browser can reach the connection screen, not
that the browser is trusted.

From the invitation dialog, **Add another trusted address** accepts one exact
`http://` or `https://` origin, saves it in `access_routes.json`, applies it to
new HTTP and WebSocket admission immediately, and creates the invitation. Saved
addresses survive restart and can be removed with confirmation; current access
through a removed address may stop immediately. Credentials, paths, queries,
fragments, wildcards, malformed ports, and non-HTTP(S) schemes are rejected.
Origins from `ROW_BOT_PUBLIC_ORIGINS` are visible but read-only, and setting
`ROW_BOT_ALLOWED_HOSTS` makes Host admission externally managed so the UI
cannot override deployment policy.

The complete public guide, including current UI screenshots, invitation
layouts and lifetimes, Tailscale ownership, browser-local voice, recovery, and
proxy diagnostics, is [Remote Access And Server Mode](https://row-bot.ai/docs/operations/remote-access).
Docker and VPS operators should use the pull-first
[Docker And VPS Operations](https://row-bot.ai/docs/operations/docker) runbook.

When Row-Bot creates and verifies its own Tailscale Serve route, it persists an
exact ownership record and restarts the child app. Startup then trusts only the
local loopback proxy for that exact app port and allows only the verified owned
`.ts.net` host and HTTPS origin. If the launcher is unavailable, Settings asks
for a manual restart. Manually managed Tailscale and reverse-proxy routes still
require explicit public-origin, allowed-host, and trusted-proxy configuration.

For a zero-configuration SSH path, start Row-Bot on the remote host and forward
it from the workstation:

```bash
row-bot serve --host 127.0.0.1 --port 8080
ssh -N -L 18080:127.0.0.1:8080 user@row-bot-host
```

Then create an invitation on the host for the browser-facing origin:

```bash
row-bot access invite --layout desktop --origin http://127.0.0.1:18080
```

For direct server operation, `row-bot serve` defaults to loopback, one worker,
no tray or splash, no opened browser, and no automatic Ollama startup. Bind
more broadly only after choosing the route and canonical origin:

```bash
row-bot serve \
  --host 127.0.0.1 \
  --port 8080 \
  --public-url https://row-bot.example.com \
  --allowed-host row-bot.example.com \
  --trusted-proxy 127.0.0.1/32
```

The legacy `row-bot --server --no-open` form remains as a deprecated
compatibility path and now uses authenticated server behavior. New scripts and
service definitions should use `row-bot serve`.

Useful offline access-management commands are:

```bash
row-bot access invite --layout desktop --origin https://row-bot.example.com
row-bot access invite --layout compact --temporary --origin https://row-bot.example.com
row-bot access list
row-bot access revoke DEVICE_ID
row-bot access revoke SESSION_ID --session
row-bot access revoke-all --yes
row-bot access doctor
```

If the owner loses every browser session, use a trusted local terminal, SSH
session, or `docker compose exec` to create a new one-time computer invitation.
The list and doctor commands never print reusable session secrets. Invitation
commands are the only commands that print the raw one-time link, so keep their
terminal output and scrollback private.

Docker users should follow the public
[Docker And VPS Operations](https://row-bot.ai/docs/operations/docker) guide.
The repository's [`deploy/docker/README.md`](deploy/docker/README.md) retains
source-build and operator detail. Both preserve the loopback-only Compose
default, explicit invitation bootstrap, and separate persistent data and
encryption-key volumes. The normal Compose path creates its encryption key once
without additional credential-store setup and stores owner-entered credentials
as encrypted records under `/data/secure-secrets`.
`deploy/reverse-proxy/Caddyfile.example` and
`deploy/systemd/row-bot.service.example` are reviewed starting points for
operator-managed VPS deployments.

For a reverse proxy, configure `ROW_BOT_PUBLIC_URL`,
`ROW_BOT_ALLOWED_HOSTS`, and `ROW_BOT_TRUSTED_PROXY_CIDRS` explicitly. Do not
trust a broad Docker network or accept caller-supplied forwarding headers.
Keep NiceGUI at one worker, preserve WebSocket upgrades, and terminate public
traffic with HTTPS. Public ingress also requires operator-owned rate limiting,
firewall policy, log review, and recovery procedures; Row-Bot does not modify
the host firewall.

Remote browser voice captures the requesting browser's microphone, performs
transcription in Row-Bot with the selected local STT engine (Whisper by default,
or SenseVoice after its explicit installation), and returns local Kokoro audio
to that same browser. Remote microphone capture requires HTTPS; plain LAN HTTP
is not a secure browser context. Voice model downloads remain explicit and
OpenAI Realtime voice remains a separate provider feature.

SenseVoice installation is an explicit ~940 MB download from
[ModelScope](https://modelscope.cn/models/iic/SenseVoiceSmall) under the model's
Apache-2.0 license. ModelScope receives the download request and standard SDK
user-agent; Row-Bot sends no audio, prompts, or usage data, and normal inference
uses only the verified local snapshot with update checks disabled. SenseVoice is
unavailable on Intel macOS because matching PyTorch/Torchaudio CPU wheels are not
published for that runtime; local Whisper remains available.

Back up the complete active `ROW_BOT_DATA_DIR` while Row-Bot is stopped, and
protect that backup as private user data. It contains conversations,
configuration, instance identity, invitations, devices, and sessions. OS
keyring credentials may need a separate recovery plan. Docker deployments must
back up the data and encryption-key volumes together; either one alone is
insufficient to recover saved provider credentials. Test restore and rollback
with an isolated instance before relying on a backup.

## Models, Keys, and Integrations

Most tools work without API keys. Add keys only for the providers and integrations you use.

Model catalog browsing, pinning, defaults, and Quick Choices live in
`Settings -> Models`. Model choices stay provider-qualified, so the same model
ID from a local runtime, OpenRouter, Atlas Cloud, Requesty, xAI API keys, xAI Grok OAuth,
a custom endpoint, or a direct provider remains distinct. Row-Bot also tracks
whether a selected model is ready for full agent/tool use, supports vision,
belongs on an image or video surface, should run chat-only, or needs a larger
context window or different endpoint profile. Live catalogs such as Atlas Cloud,
MiniMax, and Requesty refresh through the same provider path, xAI API-key
catalogs merge the available xAI model endpoints, and media-generation rows are
filtered out of chat, agent, and vision model surfaces. Automatic targeted and
scheduled refreshes preserve each provider's last-known-good rows when a fetch
is empty, fails, or stops part-way through pagination; Settings labels live,
cached, and fallback catalog outcomes.

OpenCode Zen and OpenCode Go refresh each gateway's live `/models` list and
intersect it with OpenCode's public native routing registry. Row-Bot persists
the resulting provider-qualified context, modality, tool, streaming, reasoning,
and OpenAI Chat, OpenAI Responses, Anthropic Messages, or Google GenAI transport
metadata, so newly listed supported models can become usable without a
hardcoded name update. A failed gateway or registry refresh preserves the last
known good rows; a cold failure uses the bundled static fallback.

Live catalog refreshes resolve credentials through the same provider-scoped
auth store used by Settings. A key saved in the provider dialog can therefore
refresh its catalog and populate provider-qualified rows immediately without a
duplicate legacy environment-key entry.

For local Ollama models, Row-Bot uses the daemon's native capability metadata
when it is available. A new model family that advertises `tools` can therefore
enter Agent mode without waiting for a maintained family-list update or an
unnecessary live probe, while a model whose native metadata omits `tools` is not
promoted merely because its name resembles a historically tool-capable family.

Reasoning controls are exact-model capabilities rather than a provider-wide
switch. A supported selection is stored locally per chat and canonical model,
mapped into that transport's native request shape, and shown as a separate
collapsed Thinking section when replayable reasoning content is returned. If a
provider rejects an explicit choice before returning output, Row-Bot retries
once with Provider default, clears the rejected choice, and tells you; it does
not replay authentication, rate-limit, timeout, cancellation, server, or
mid-stream failures.

| Service | Key or setup | Used for |
|---------|--------------|----------|
| OpenAI | `OPENAI_API_KEY` | OpenAI models and image tools. |
| ChatGPT / Codex | In-app ChatGPT sign-in | Subscription-backed Codex models through ChatGPT's internal backend. |
| Claude Subscription | In-app Claude OAuth or explicit setup-token import | Subscription-backed Claude models through Row-Bot-owned OAuth. This is separate from Anthropic API. |
| Anthropic | `ANTHROPIC_API_KEY` | Claude models through the direct API. |
| Google AI | `GOOGLE_API_KEY` | Gemini models, Imagen, and Veo. |
| xAI | `XAI_API_KEY` | Grok models, Grok Imagine, and Grok Imagine Video through the direct xAI API. |
| xAI Grok | In-app xAI Grok OAuth | OAuth-backed Grok chat, vision, and Grok Imagine media through the xAI Grok provider. This is separate from `XAI_API_KEY`. |
| MiniMax | `MINIMAX_API_KEY` | Current MiniMax models through the Anthropic-compatible API, discovered from the live provider catalog where available. |
| OpenRouter | `OPENROUTER_API_KEY` | Access to 100+ provider models. |
| OpenCode Zen | `OPENCODE_ZEN_API_KEY` | OpenCode Zen gateway models discovered from the live gateway and native routing registry. |
| OpenCode Go | `OPENCODE_GO_API_KEY` | OpenCode Go gateway models discovered independently from the live gateway and native routing registry. |
| [Atlas Cloud](https://www.atlascloud.ai/?utm_source=github&utm_medium=link&utm_campaign=row_bot) | `ATLASCLOUD_API_KEY` | OpenAI-compatible access to Atlas-hosted chat, agent, and vision models, discovered from the live provider catalog. Image and video generation rows are not exposed as chat models. |
| [Requesty](https://requesty.ai/) | `REQUESTY_API_KEY` | OpenAI-compatible access to Requesty's model gateway, with live catalog normalization for context windows, tool support, vision support, and chat-surface filtering. |
| Ollama Cloud | `OLLAMA_CLOUD_API_KEY` or local daemon sign-in | Direct Ollama Cloud models and cloud-tagged daemon models. |
| Custom OpenAI-compatible endpoint | Base URL and optional key | Self-hosted or proxy models through profiles for oMLX, LM Studio, vLLM, llama.cpp, LocalAI, LiteLLM, SGLang, and generic servers. |
| Tavily | `TAVILY_API_KEY` | Live web search. |
| Wolfram Alpha | `WOLFRAM_ALPHA_APPID` | Symbolic math, unit conversion, and scientific data. |
| Telegram | `TELEGRAM_BOT_TOKEN` | Telegram bot messaging. |
| Discord | `DISCORD_BOT_TOKEN` | Discord DM messaging. |
| Slack | `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` | Slack DM messaging through Socket Mode. |
| Twilio | `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` | SMS. |
| X | `X_CLIENT_ID` / `X_CLIENT_SECRET` | X API v2 OAuth 2.0 PKCE for search, timeline, mentions, posting, replies, quotes, likes, reposts, bookmarks, and deletes. |
| Xquik MCP | Xquik `x-api-key` header in MCP settings | Recommended remote MCP option for X/Twitter search, extraction, monitoring, and connected-account operations. Its generic executor is high risk and remains approval-gated. |
| Tailscale | Optional local Tailscale install | Private Serve reachability for authenticated owner devices without making Tailscale a Row-Bot dependency. Row-Bot does not install it, sign in, or enable Funnel. |
| ngrok | `NGROK_AUTHTOKEN` | Tunnels for inbound webhooks. |
| Gmail and Google Calendar | Google Cloud OAuth `credentials.json` | Email search/read/draft/send and request-scoped calendar search/create/bulk-create/update/move/delete with safe concurrent token refresh. |

Configure providers in Settings, Channels, and Accounts. Keys and in-app ChatGPT / Codex, Claude Subscription, and xAI Grok OAuth tokens are stored in Windows Credential Manager, macOS Keychain, or Linux Secret Service/KWallet when available. If secure storage is unavailable, newly entered secrets are usable for the current Row-Bot process only and must be re-entered after restart unless a secure keyring backend is configured. The official Compose path automatically creates a persistent `ROW_BOT_SECRET_STORE_KEY` in its isolated secrets volume and uses it to encrypt owner-entered credentials under `/data/secure-secrets`; advanced operators can replace that volume with an explicit read-only secret file. Back up the key and data together; see [Docker And VPS Operations](https://row-bot.ai/docs/operations/docker#account-credential-persistence-is-automatic). `~/.row-bot/api_keys.json` and `~/.row-bot/providers.json` keep metadata only, such as saved state, provider status, Quick Choices, compatibility profiles, runtime and vision probe results, OAuth client-id diagnostics, model-count status, and masked fingerprints.

OpenAI-compatible chat requests use a 900-second read-inactivity timeout. Set
`ROW_BOT_OPENAI_COMPATIBLE_READ_TIMEOUT` to a positive number of seconds to
override it for all OpenAI-compatible endpoints.

Atlas Cloud uses an OpenAI-compatible API, but Row-Bot treats it as a
first-class provider with its own setup, auth, catalog refresh, provider
identity, capability labels, streaming behavior, and chat/agent/vision surface
filtering.

Requesty is also a first-class OpenAI-compatible provider path. It uses
provider-qualified refs, maps Requesty's catalog metadata into Row-Bot
capability labels, and filters non-chat media/audio/embedding/realtime rows out
of Brain and agent choices.

xAI Grok OAuth is also a first-class provider path. It keeps subscription/OAuth
Grok runtime state separate from `XAI_API_KEY`, reports token health and runtime
readiness in Settings, and scopes Grok Imagine image/video models to media
surfaces instead of chat and agent pickers.

Custom/Self-hosted endpoints keep server capacity and Row-Bot's planning cap
separate. Auto uses model metadata or the endpoint's manually declared native
limit and does not invent a generic context window. Probe evidence is scoped to
the exact tested model, so one successful model cannot promote an untested
sibling to Agent mode or streaming tool use. Advanced provider settings also
offer explicit reasoning compatibility controls; enable reasoning replay only
when the endpoint documents and accepts its preserved thinking format.

Embedding providers are configured separately from chat models. Local
embeddings are available for private document and vector indexing and load
strictly from the existing local Hugging Face cache during normal use. First-run
setup offers **Mixedbread Embed Large v1** as a checked-by-default, disclosed
675 MB download; it can be skipped. Download or repair a local embedding model
later from Settings; if it is missing, failed, or still loading, bounded memory
recall continues with lexical and graph fallback instead of silently
downloading or blocking the turn. Optional cloud embeddings show a privacy
warning because document text is sent to the selected embedding provider.

External Codex CLI and Claude Code login files are metadata/reference only. Row-Bot can detect that a CLI login exists, but direct Codex runtime requires the in-app ChatGPT sign-in and direct Claude Subscription runtime requires Row-Bot-owned Claude OAuth or an explicit user import. Row-Bot does not copy runnable tokens from `~/.codex/auth.json` or `~/.claude/*`, and Claude Subscription never falls back to `ANTHROPIC_API_KEY`.

Claude Subscription supports two Row-Bot-owned auth paths in Settings -> Providers: in-app Claude OAuth, or explicit import of a token printed by `claude setup-token`. The setup-token path is a user paste/import action; Row-Bot still does not silently read Claude Code environment variables or credential files.

After connecting Claude Subscription, Settings -> Providers can run a Claude Subscription runtime test that checks native OAuth chat, a forced Row-Bot tool call, and tool-result replay. A failed runtime test is stored as provider metadata and prevents Row-Bot from advertising Claude Subscription as tool-ready until it is fixed or reconnected; `claude -p` remains a separate Claude Code delegation path, not the provider runtime.

## Tools and Safety

Row-Bot's tools can be enabled or disabled from Settings. In the recommended
Auto mode, core tools stay directly available under the active profile while
enabled external MCP, plugin, Custom Tool, and channel tools are searched and
selected only when a request needs them. Search runs locally over bounded
metadata and never enables an integration or authorizes execution; the selected
target keeps its exact schema, real trace label, approval mode, execution
budget, cancellation, and workspace boundary. Eager compatibility mode can
still bind every enabled external schema.

Many tools expose multiple operations, Agent Profiles and Goal Mode add
orchestration-specific tools, Developer Studio adds code-specific tools, Skills
Hub can add manual or automatically selected skills, Custom Tools can be
promoted after review, and running channels add send/photo/document tools
automatically. Automatically loaded skills are scoped to one parent or child
task, persist when that task is reopened, and appear as a compact **Using
_skill_** receipt and active chip. Collapsed tool traces use a quieter compact
row while keeping the full result available inside the expansion.

| Group | Included tools |
|-------|----------------|
| Search and knowledge | Tavily web search, DuckDuckGo, Wikipedia, arXiv, YouTube transcripts, URL reader, document search, wiki vault, memory graph, and conversation search. |
| Productivity | Gmail, Google Calendar, filesystem, shell, visible Chromium browser automation, opt-in native Computer Use on Windows and macOS, workflows, goals, tracker, channel tools, and X. |
| Media and design | Designer Studio, image generation/editing through OpenAI, Google, xAI API keys, and xAI Grok OAuth, video generation through Google Veo and xAI Grok Imagine Video, chart insertion, Mermaid, Plotly, and media persistence. |
| Developer and extensibility | Developer Studio, Agent Profiles, child-agent delegation, Goal Mode tools, Custom Tool Builder, promoted Custom Tools, external MCP tools, plugin tools, Claude Code Delegation, and Row-Bot Status. |
| Analysis | Calculator, Wolfram Alpha, weather, vision for camera/screen/workspace images, system info, and Plotly charts with PNG export. |

Safety controls are built into the tool layer:

- Destructive operations require confirmation, including file delete/move, moderate-risk shell commands, Gmail send, calendar move/delete, memory delete, tracker delete, and task delete.
- Computer Use is off by default, interactive-local-only, restricted to one
  task-scoped target window, and protected by an allowlisted Cua surface,
  generation-bound targets, point-of-risk confirmation, ephemeral screenshots,
  typed-value redaction, and direct Stop/Take over/Resume controls.
- Every logical agent turn has a checkpointed model-iteration budget. Exact
  repeated tool actions are blocked before they can loop indefinitely, and
  Settings can cap nested depth, per-parent and global child concurrency, and
  optional child active time.
- Child-agent approval requests are surfaced in the parent thread and durable
  channel/mobile approval surfaces instead of waiting invisibly in a background
  run.
- Write-capable child Agents retain one writer per assigned workspace. Distinct
  registered folders may run concurrently; a shared folder stays serialized,
  and changing a shell CWD does not bypass the workspace lock.
- Stop propagates through the active generation to stalled provider responses,
  shell and Developer subprocesses, MCP and browser waits, voice turns, and
  generation-linked child agents without cancelling unrelated runs.
- Filesystem access is sandboxed to the configured workspace folder, which defaults to `~/Documents/Row-Bot`.
- Shell commands are classified as safe, moderate, or blocked. High-risk commands such as `shutdown`, `reboot`, and `mkfs` are blocked.
- Background workflows can have per-task command prefix and email-recipient allowlists.
- Agent Profiles and child-agent runs can narrow tools through profile/tool allowlists.
- Browser tabs are isolated per thread and cleaned up when tasks or threads finish.
- Developer Studio has its own approval modes for edits, commands, Git operations, commits, pushes, and PR prep.
- Docker Sandbox is opt-in and runs commands in a shadow workspace until you explicitly import changes.
- Agent Profile saves, child-agent promotion, and Agent-run workflow promotion stay reviewable and approval-gated.
- Controlled self-evolution produces bounded, reviewable proposals and does not
  silently modify repos or app code.
- Smart Skills, slash commands, and Skills Hub imports stay user-controlled; installed skills can be enabled, disabled, reviewed, and removed. Enabled skills can be discovered progressively and loaded per parent task or child Agent, but skill instructions cannot add tools or relax profile, approval, execution-budget, or workspace boundaries.
- Plugin System v2 supports only native tools, plugin-packaged MCP servers,
  bundled skills, and channels. Plugins install disabled by default, cannot add
  arbitrary app UI or provider runtimes, and are enabled only after permission,
  settings, secrets, and health review in Plugin Center.
- Custom Tools are reviewed, smoke-tested, enabled, promoted, disabled, and removed without deleting their source repos.
- Gmail and Calendar permissions are tiered for read, compose/write, and destructive actions.
- MCP servers stay disabled until tested. External tools are namespaced, destructive MCP tools require approval, and broken servers degrade to diagnostics instead of blocking startup.
- Progressive capability search operates only on enabled, profile-permitted
  external tools and enabled skills. Loading a skill grants no tool, approval,
  workspace, or delegation authority, and parent, child, and sibling task state
  remain isolated.
- Remote HTTP and WebSocket access is session-gated. Invitations are
  short-lived and single-use, device and session secrets are hashed at rest in
  `mobile.db`, sessions use HttpOnly cookies, forwarded localhost headers do
  not bypass the gate, and devices or individual sessions can be revoked from
  Remote Access settings.
- Prompt-injection defense scans tool outputs and user inputs for instruction override attempts, role impersonation, data exfiltration, encoding evasion, and social engineering patterns.

## Architecture

Row-Bot is organized around reasoning, orchestration, and work: Agent Profiles,
Goal Mode, a durable parent/child orchestrator, checkpoint-safe agent budgets,
explicit prompt context/cache sections, a durable document queue and sharded
retrieval index, memory, profile-first workflows, the single-owner access gate
with runtime-managed exact origins and full/compact browser shells,
authenticated server mode, progressive capability catalogs and bridges, one
complete-input context preparation and rolling-compaction pipeline with explicit
capacity sources and fixed-envelope preflight, exact per-thread/per-model
reasoning plans, native OpenCode catalog-to-transport routing, separate browser
and native Computer Use engines, shared channel streaming, Designer Studio,
Developer Studio worktrees and folder-scoped child writers, provider runtime
and bounded subprocess cancellation, interrupted-parent checkpoint repair,
Plugin System v2/MCP boundaries, and safety controls.

Explore the visual architecture gallery: [docs/architecture.html](docs/architecture.html)

Read the full architecture reference: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#core-modules)

Read the complete public user guide: [row-bot.ai/docs](https://row-bot.ai/docs/)

Review the Docusaurus docs source and local preview instructions:
[docs-site/README.md](docs-site/README.md)

<table>
   <tr>
      <td align="center"><a href="docs/Core_Agent_Arch.png"><img src="docs/Core_Agent_Arch.png" width="360" alt="Row-Bot core agent architecture"></a><br><strong>Core Agent</strong></td>
      <td align="center"><a href="docs/Context_Arch.png"><img src="docs/Context_Arch.png" width="360" alt="Row-Bot context architecture"></a><br><strong>Context Assembly</strong></td>
   </tr>
   <tr>
      <td align="center"><a href="docs/Knowledge_Graph_Arch.png"><img src="docs/Knowledge_Graph_Arch.png" width="360" alt="Row-Bot knowledge graph architecture"></a><br><strong>Knowledge Graph</strong></td>
      <td align="center"><a href="docs/Workflows_Arch.png"><img src="docs/Workflows_Arch.png" width="360" alt="Row-Bot background workflow architecture"></a><br><strong>Background Workflows</strong></td>
   </tr>
   <tr>
      <td align="center"><a href="docs/Multi_Channel_Arch.png"><img src="docs/Multi_Channel_Arch.png" width="360" alt="Row-Bot multi-channel architecture"></a><br><strong>Multi-Channel Runtime</strong></td>
      <td align="center"><a href="docs/Designer_Studio_Arch.png"><img src="docs/Designer_Studio_Arch.png" width="360" alt="Row-Bot Designer Studio architecture"></a><br><strong>Designer Studio</strong></td>
   </tr>
   <tr>
      <td align="center"><a href="docs/Developer_Studio_Arch.png"><img src="docs/Developer_Studio_Arch.png" width="360" alt="Row-Bot Developer Studio architecture"></a><br><strong>Developer Studio</strong></td>
      <td align="center"><a href="docs/Skills_System_Arch.png"><img src="docs/Skills_System_Arch.png" width="360" alt="Row-Bot skills system architecture"></a><br><strong>Skills System</strong></td>
   </tr>
   <tr>
      <td align="center"><a href="docs/Safety_Privacy_Arch.png"><img src="docs/Safety_Privacy_Arch.png" width="360" alt="Row-Bot safety privacy and control architecture"></a><br><strong>Safety, Privacy &amp; Control</strong></td>
      <td align="center"><a href="docs/Self_Evolution_Arch.png"><img src="docs/Self_Evolution_Arch.png" width="360" alt="Row-Bot self-evolution architecture"></a><br><strong>Self-Evolution</strong></td>
   </tr>
</table>

## System Requirements

| Setup | Minimum | Recommended |
|-------|---------|-------------|
| Local model runtime | Windows 10/11 64-bit, macOS 12+, or glibc Linux x86_64; Python 3.12+ for source installs; 8 GB RAM for 8B models; about 5 GB disk for the app and one small model; internet for install and model download. | 16 to 32 GB RAM for 14B to 30B models; NVIDIA GPU with 8+ GB VRAM or Apple Silicon for much faster inference; 20+ GB disk for multiple or larger models. Ollama Auto targets 64K context and may require a fixed 32K setting on tighter-memory systems. |
| Provider/custom models only | Windows 10/11 64-bit, macOS 12+, or glibc Linux x86_64; Python 3.12+ for source installs; 4 GB RAM; about 1 GB disk; internet for provider inference. | No GPU required. Use this path if you do not want local model downloads. |
| Optional SenseVoice STT | Windows, Linux, or Apple Silicon macOS; the voice runtime; internet for the explicit approximately 940 MB ModelScope snapshot download plus space for the matching CPU PyTorch/Torchaudio stack. | Optional and off until installed from Voice settings. Normal transcription is local and cache-only afterwards. Intel macOS is unsupported; local Whisper remains available. |
| Computer Use beta | Windows 10/11 x86-64 or ARM64, or macOS 12+ on Intel/Apple Silicon; interactive local UI; internet for the explicit Cua Driver install or repair; Accessibility and Screen Recording permission on macOS. | Optional and off by default. Browser automation remains preferred for websites; Linux and unattended/background use are not supported. |
| Developer Sandbox | Docker Desktop or a compatible Docker/Podman runtime. | Optional. Developer Studio also works with local execution in the selected repo. |
| Docker / headless server | Docker Engine with Compose v2 on an amd64 or arm64 Linux host; enough persistent storage for `/data`, the encryption-key volume, documents, models, and backups. | Pin `ghcr.io/siddsachar/row-bot:4.8.0`, keep the default loopback publication, and use Tailscale or an operator-managed HTTPS proxy for remote reachability. |
| Public docs site | Node.js 20+ and npm. | Optional. Used only for local Docusaurus docs preview and generated-docs validation. |

Your default Brain model is set by the setup wizard. If you choose the local path, Row-Bot uses one of the models already exposed by your local runtime; 14B-class models are recommended for stronger agent/tool behavior, while smaller 8B-class models are better for 8 GB machines. Hosted and custom endpoint setups can skip local model downloads entirely.

## From Source

Install [Ollama](https://ollama.com/) first if you want Row-Bot's supported local model runtime. Provider-only and custom-endpoint setups can skip local model downloads.

```bash
git clone https://github.com/siddsachar/row-bot.git
cd row-bot
python -m venv .venv
```

Activate the environment:

```bash
# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate
```

Install dependencies and launch with the locked dependency set:

```bash
python -m pip install "uv>=0.7,<1.0"
uv sync --locked --all-extras --group test
uv run python launcher.py
```

`requirements.txt` is kept only as a generated pip-compatible export for installers and repair helpers. If you cannot use uv, the fallback is:

```bash
python -m pip install -r requirements.txt
python launcher.py
```

On Windows and macOS, `launcher.py` starts the tray icon and opens the app on the first available local port, normally `http://localhost:8080`. On Linux it opens in the browser without a tray by default. If port 8080 is busy, Row-Bot picks the next free port.

Authenticated headless/server mode:

```bash
uv run python launcher.py serve --port 8080
```

The installed console entry point is `row-bot serve`. The legacy
`python launcher.py --server --no-open` form remains temporarily compatible but
is deprecated.

Direct app launch:

```bash
python app.py
```

Direct launches default to `http://localhost:8080`. Set `ROW_BOT_PORT` to choose a different port.

Dependency edits go through `pyproject.toml`, not `requirements.txt`:

```bash
# edit pyproject.toml
uv lock
python scripts/export_locked_requirements.py
uv sync --locked --all-extras --group test
uv run python scripts/verify_runtime_dependencies.py all
```

Supported optional extras are `voice`, `designer`, `browser`, `channels`, `mcp`, `developer`, `local-embeddings`, and `media`. Development and packaged app builds use `all`; lightweight source installs can choose only the extras they need. The `voice` and `all` extras include the platform-supported FunASR, ModelScope, and matching CPU PyTorch/Torchaudio runtime, but the SenseVoice model snapshot is still downloaded only through the explicit Voice settings action.

Recovery helpers:

```bash
python launcher.py --reset-tasks-db
python launcher.py --reset-db
python launcher.py --restore-data
```

These commands back up local SQLite files before recreating or restoring known task, memory, and thread databases.

Docs site preview:

```bash
cd docs-site
npm ci
npm run start
```

## Privacy

Local model runs stay on your machine. Documents, memories, conversations,
knowledge graph data, Agent Profiles, Goal Mode records, child-agent run
history, workflows, plugin state, logs, and user settings are stored locally
under `~/.row-bot` or the platform-specific Row-Bot app data paths used by the
installer. Current Row-Bot startup reads Row-Bot data only and no longer scans,
copies, repairs, or rewrites old `.thoth` data.

Invitations, devices, hashed session credentials, revocation state, and
display-safe access events are stored locally in `mobile.db`. Remote access has
no Row-Bot cloud relay: each browser connects directly to the running Row-Bot
host through the route you choose. Every authenticated browser is the full
owner, so keep invitation links and public origins private and revoke devices or
sessions you no longer trust. In Docker, saved provider credentials are
encrypted locally with the key from the separate secrets volume.

Computer Use is also opt-in. Row-Bot downloads the reviewed Cua Driver 0.7.1
asset only after a separate Install action, verifies its SHA-256, and keeps the
runtime private with upstream update checks disabled. Cua Driver has separately
disclosed third-party telemetry; Row-Bot requires acceptance before any Cua
process starts and does not send prompts, memories, secrets, screenshots, tool
arguments, typed content, or channel data to that telemetry. Target-window
screenshots are ephemeral, and typed values are excluded from durable history,
logs, checkpoints, approvals, memory, and media.

Provider and custom models are opt-in. When selected, the current conversation, model-visible tool context, and tool results are sent to that endpoint. Memories, documents, files, graph data, and other conversations stay local unless you explicitly include them in the current conversation or expose them through a tool result. Memory recall happens locally before any selected memory is inserted into the active turn.

Reasoning choices are stored locally per chat and model, but the selected
provider receives the resulting reasoning parameter and may bill for additional
tokens. Returned reasoning can contain sensitive intermediate material. Custom
endpoint reasoning replay remains off unless you explicitly declare support;
enable it only for an endpoint whose format and trust boundary you understand.
An explicit OpenCode catalog refresh contacts both the configured Zen or Go
gateway and the public native routing registry to resolve current model routes.

External-tool and skill matching also happens locally over enabled capability
metadata. Search does not contact an embedding or model provider. When rolling
compaction is needed, Row-Bot uses the task's selected model to summarize a
bounded older conversation range; a cloud-selected task therefore sends that
range to the already selected cloud provider as part of normal model use.

Installing SenseVoice is a separate explicit network action to ModelScope and
discloses the model size, license, and SDK request before it begins. Row-Bot
persists only a verified contained snapshot, disables FunASR update checks, and
sends no audio, prompts, or usage data during installation or normal offline
transcription.

Developer Studio only touches repos you link, clone, or explicitly allocate as
worktrees. Local execution runs in that repo or worktree. Docker Sandbox runs
in a shadow copy and requires explicit import before changing the real repo.
Inside the official server container, nested Docker Sandbox mode is unavailable
and fails closed rather than substituting local execution.
Skills Hub imports, plugins, Custom Tools, Agent Profiles, child-agent
promotion, and promoted Agent workflows are opt-in, testable or reviewable,
removable where applicable, and only affect normal chat or workflows after you
enable, select, or promote them.

Row-Bot does not require a Row-Bot account, and there is no Row-Bot-hosted middleman for provider calls.

The public `row-bot.ai` website is separate from the Row-Bot application. Its
marketing pages load Google tag services and measure page/download,
Linux-install-view, and installation-guide interactions. The installed
application does not load that site tag and still has no Row-Bot first-party
telemetry pipeline.

## Project Docs

- [Complete public user guide](https://row-bot.ai/docs/)
- [Remote Access and server mode](https://row-bot.ai/docs/operations/remote-access)
- [Docker and VPS operations](https://row-bot.ai/docs/operations/docker)
- [Progressive tools and skills](https://row-bot.ai/docs/guides/progressive-tools-and-skills)
- [Reasoning controls](https://row-bot.ai/docs/chat/reasoning-controls)
- [Release notes](RELEASE_NOTES.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Visual architecture gallery](docs/architecture.html)
- [Public docs site source](docs-site/README.md)
- [Computer Use security decision](docs/COMPUTER_USE_SECURITY.md)
- [Plugin System v2](docs/PLUGIN_SYSTEM_V2.md)
- [Prompt context and cache contract](docs/PROMPT_CONTEXT_AND_CACHE.md)
- [Contributing guide](CONTRIBUTING.md)
- [Branching strategy](docs/BRANCHING.md)
- [Release process](docs/RELEASING.md)
- [Installer and CI verification plan](docs/INSTALLER_CI_VERIFICATION_PLAN.md)
- [Source layout and packaging](docs/SOURCE_LAYOUT.md)
- [Security policy](SECURITY.md)
- [Code of conduct](CODE_OF_CONDUCT.md)

All changes should go through a pull request. `main` is intended to stay releasable.

## License

Apache 2.0. See [LICENSE](LICENSE).

## Acknowledgements

Built with [NiceGUI](https://nicegui.io/), [LangGraph](https://langchain-ai.github.io/langgraph/), [LangChain](https://python.langchain.com/), [Ollama](https://ollama.com/), [FAISS](https://github.com/facebookresearch/faiss), [Cua Driver](https://github.com/trycua/cua), [Kokoro TTS](https://github.com/thewh1teagle/kokoro-onnx), [faster-whisper](https://github.com/SYSTRAN/faster-whisper), [FunASR/SenseVoice](https://github.com/modelscope/FunASR), [HuggingFace](https://huggingface.co/), and [tiktoken](https://github.com/openai/tiktoken).
