"""Write the hand-authored public Row-Bot user guide pages.

The pages are kept in this script so a broad documentation rewrite can stay
consistent across the Docusaurus tree without involving an LLM or network call
in CI. It is intentionally static content: running it rewrites the curated
guide pages only, not the current public site under docs/.
"""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs-site" / "docs"


def page(title: str, description: str, body: str, *, screenshot: bool = False) -> str:
    imports = "\n\nimport Screenshot from '@site/src/components/Screenshot';" if screenshot else ""
    return (
        f"---\n"
        f'title: "{title}"\n'
        f'description: "{description}"\n'
        f"---"
        f"{imports}\n\n"
        f"{body.strip()}\n"
    )


def write(rel: str, title: str, description: str, body: str, *, screenshot: bool = False) -> None:
    path = DOCS / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(page(title, description, body, screenshot=screenshot), encoding="utf-8")


HOME_OVERVIEWS = {
    "workflows": {
        "title": "Home: Workflows",
        "description": "Use the Home Workflows tab as the entry point for saved automations and background agents.",
        "shot": "home-workflows",
        "caption": "The Workflows tab shows saved automations, delivery defaults, run controls, and the entry point for creating a workflow.",
        "deep": "/docs/guides/workflows",
        "deep_label": "Workflows guide",
        "summary": "The Workflows tab is the dashboard for repeatable work. Use it to see existing workflows, run one manually, pause or resume schedules, and choose default delivery channels.",
        "launches": [
            "New Workflow opens the workflow builder.",
            "Run starts the selected workflow with the current settings.",
            "Delivery defaults choose where workflow results should appear.",
            "Edit and delete controls appear on each workflow card when workflows exist.",
        ],
    },
    "designer": {
        "title": "Home: Designer",
        "description": "Use the Home Designer tab as the entry point for Designer Studio projects.",
        "shot": "home-designer",
        "caption": "The Designer tab gathers project creation, recent designs, and the path into Designer Studio.",
        "deep": "/docs/designer/",
        "deep_label": "Designer Studio guide",
        "summary": "The Designer tab is a launcher. It helps you start or reopen design projects, then hands off to Designer Studio for the full editor, preview, brand, and export workflow.",
        "launches": [
            "New design starts a project from a prompt, template, or goal.",
            "Recent projects reopen saved Designer work.",
            "Brand and import actions prepare assets before you enter the editor.",
            "Open in Designer Studio moves from the Home tab into the full workspace.",
        ],
    },
    "developer": {
        "title": "Home: Developer",
        "description": "Use the Home Developer tab as the entry point for code workspaces and Custom Tools.",
        "shot": "home-developer",
        "caption": "The Developer tab starts folder, repository, clone, and Custom Tool work without replacing the full Developer Studio guide.",
        "deep": "/docs/developer/",
        "deep_label": "Developer Studio guide",
        "summary": "The Developer tab is where you choose what code workspace Row-Bot should help with. The full Developer Studio page explains the inspector, chat, sandbox, commands, and review flow.",
        "launches": [
            "Open folder connects an existing local project.",
            "Connect repository attaches a Git repository already on the machine.",
            "Clone repository creates a new local checkout from a remote URL.",
            "Custom Tools opens the builder for reviewed, reusable local tools.",
        ],
    },
    "knowledge": {
        "title": "Home: Knowledge",
        "description": "Use the Home Knowledge tab to review memories, documents, and knowledge graph activity.",
        "shot": "home-knowledge",
        "caption": "The Knowledge tab shows memory and graph review surfaces for the local Row-Bot data set.",
        "deep": "/docs/knowledge/",
        "deep_label": "Knowledge guide",
        "summary": "The Knowledge tab is the review area for information Row-Bot can remember or retrieve. It is useful when you want to check what has been extracted, filter it, or open a detail card before relying on it in chat.",
        "launches": [
            "Filters narrow the list by source, status, or type.",
            "Detail cards show the exact item Row-Bot may use later.",
            "Edit controls let you correct useful records instead of deleting whole history.",
            "Dream cycle controls help review background organization when enabled.",
        ],
    },
    "monitor": {
        "title": "Home: Monitor",
        "description": "Use the Home Monitor tab to inspect logs, journals, channel state, and background activity.",
        "shot": "home-monitor",
        "caption": "The Monitor tab combines recent logs, knowledge extraction, dream cycle, channels, and full-log access.",
        "deep": "/docs/monitor/",
        "deep_label": "Monitor guide",
        "summary": "The Monitor tab is the place to answer: what is Row-Bot doing, what recently happened, and what needs attention? Use it during setup, workflows, channels, and troubleshooting.",
        "launches": [
            "Refresh updates status panels and recent logs.",
            "View Journal opens knowledge extraction or dream cycle history.",
            "View Full Log opens a longer diagnostic log view.",
            "Channel status panels show whether external messaging connectors are running.",
        ],
    },
}


SETTINGS = {
    "providers": {
        "title": "Settings: Providers",
        "desc": "Connect local, hosted, subscription, and custom model providers.",
        "shot": "settings-providers",
        "caption": "The Providers tab shows connection health, provider groups, credential actions, catalog refresh controls, and runtime tests.",
        "overview": "Providers are the model services Row-Bot can call. This tab answers whether each provider is connected, what kind of credential it uses, and whether Row-Bot has enough information to use it for chat, tools, Designer, Developer, voice, or embeddings.",
        "controls": [
            "Connection Status summarizes how many providers are connected, local, API-based, subscription-based, or media-capable.",
            "Local providers show runtimes such as Ollama. Refresh checks what the local service currently exposes.",
            "Subscription accounts show sign-in based providers such as ChatGPT / Codex or Claude Subscription. Use connect, reconnect, test, and refresh actions from the row.",
            "API providers show services that need an API key or compatible endpoint. Their credential buttons open the setup flow for that provider.",
            "Custom endpoint providers let advanced users point Row-Bot at OpenAI-compatible servers such as LM Studio, vLLM, llama.cpp, LocalAI, LiteLLM, or SGLang.",
            "Custom endpoint Advanced settings can describe reasoning mode, a thinking budget, returned reasoning content, and whether preserved reasoning may be replayed to that endpoint.",
            "Runtime tests check whether a provider can handle the kind of requests Row-Bot needs. A failed test keeps the provider visible but may stop Row-Bot from offering it for agent work.",
        ],
        "workflow": [
            "Pick one provider path first: local Ollama for private local runs, a subscription account if you already use one, or an API provider if you prefer hosted models.",
            "Add credentials or sign in only through the provider row you intend to use.",
            "Refresh the provider so Row-Bot can discover available models.",
            "Open Settings -> Models to pin the models you want in the chat picker.",
        ],
        "saved": "Provider connection state is global to the local Row-Bot app. Secrets are stored in the operating system key store when available; Row-Bot settings keep masked status and catalog metadata.",
        "troubleshoot": [
            "If a provider is connected but models do not appear, refresh Providers, then refresh Models.",
            "If a runtime test fails, read the row message before changing credentials; the model may be chat-capable but not tool-capable.",
            "If local Ollama is missing, start Ollama and make sure at least one model is installed.",
        ],
    },
    "models": {
        "title": "Settings: Models",
        "desc": "Choose defaults, pin Quick Choices, review model catalogs, and set Agent runtime limits.",
        "shot": "settings-models",
        "caption": "The Models tab manages defaults, Quick Choices, catalog refreshes, and model readiness details.",
        "overview": "Models are the specific brains exposed by a provider. The Models tab decides which model Row-Bot should use by default and which choices appear quickly in chat and specialist surfaces.",
        "controls": [
            "Default model controls choose what new chats use when a thread has no override.",
            "Quick Choices are pinned models shown in the chat picker for fast switching.",
            "Catalog refresh asks connected providers and local runtimes what models are available now.",
            "Compatibility labels separate ordinary chat models from tool-capable, vision-capable, reasoning, embedding, voice, or media models.",
            "Provider filters and search help find a specific model when many are available.",
            "Warnings explain when a model is visible but not recommended for tool-heavy Agent Mode.",
            "Agent runtime and delegation controls set work rounds, nesting depth, per-parent and app-wide child capacity, and an optional active-time limit for new runs.",
        ],
        "workflow": [
            "Refresh the catalog after connecting a provider.",
            "Pin one everyday chat model and one stronger tool-capable model.",
            "Use provider-qualified names when two providers expose models with similar names.",
            "Return to Chat and pick the pinned model from the model picker.",
            "Keep the recommended Agent runtime limits unless you have a measured reason to change them; active runs retain the snapshot they started with.",
        ],
        "saved": "Default and pinned models are global preferences. A thread can still carry its own model override when you select a model inside that thread.",
        "troubleshoot": [
            "If the picker is empty, connect a provider first.",
            "If a model is missing, refresh the catalog and check whether the provider is disabled.",
            "If a small local model fails before answering, choose a larger context window or a more capable model.",
        ],
    },
    "documents": {
        "title": "Settings: Documents",
        "desc": "Manage document ingestion, extraction, and vector indexing.",
        "shot": "settings-documents",
        "caption": "The Documents tab controls uploads, embedding models, indexed document state, and vector rebuild actions.",
        "overview": "Documents let Row-Bot search files you add to its local document library. This is different from attaching a file to one chat: indexed documents become reusable context for future questions. Mixedbread Embed Large v1 is the recommended local embedding model; it is separate from the chat model and normal use loads it only from Row-Bot's private cache.",
        "controls": [
            "Upload documents adds files to Row-Bot-managed storage for indexing.",
            "Embedding provider and model controls choose how document chunks become searchable vectors.",
            "Download model installs the selected local model, Retry local load retries a cached model, and Repair local model deliberately replaces a broken cache entry.",
            "Dimension override is for advanced embedding models that need a specific vector size.",
            "Batch size controls how much indexing work happens at once.",
            "Auto-unload local embedding resources releases local model memory after heavy document work.",
            "Indexed Documents lists what Row-Bot has processed and whether each item is ready.",
            "Rebuild document vectors repeats indexing when you change embedding settings.",
            "Rebuild memory vectors refreshes memory search with the current embedding settings.",
        ],
        "workflow": [
            "Keep the checked first-launch download, or choose Local runtime model and use Download model before adding a large document library.",
            "Upload a small test document and wait until it is indexed.",
            "Ask Chat a question that should require the document.",
            "Rebuild vectors only when you change embedding model settings or suspect stale search results.",
        ],
        "saved": "Uploaded files, extracted text, vectors, and local embedding model caches are stored under the active Row-Bot data directory. In Docker, the named /data volume preserves the cache. The active chat only sees relevant results when document search is enabled. A cloud embedding provider is opt-in and sends indexed text to that provider; the recommended local model does not.",
        "troubleshoot": [
            "If search misses obvious content, rebuild vectors and check the embedding provider.",
            "If the local model is missing, use Download model. Use Retry local load for a cached model or Repair local model for a damaged download.",
            "If local indexing is slow, lower batch size or enable auto-unload.",
            "If a document contains private material, remove it from the document library before sharing screenshots or logs.",
        ],
    },
    "tools": {
        "title": "Settings: Tools",
        "desc": "Configure progressive external-tool loading, retrieval compression, and search and knowledge tools.",
        "shot": "settings-tools",
        "caption": "The Tools tab includes progressive capability loading, retrieval compression, and search and knowledge tool controls.",
        "overview": "Tools settings control how Row-Bot exposes enabled external capabilities to the model and configure its search and retrieval helpers.",
        "controls": [
            "Auto-select external tools lets Row-Bot search enabled MCP, plugin, Custom Tool, and channel capabilities only when needed.",
            "Load all external tools is the eager compatibility mode and can consume more model context.",
            "Retrieval Compression controls how search results are filtered before reaching the model.",
            "Search and Knowledge Tools enable or disable configured research and reference tools.",
        ],
        "workflow": [
            "Keep Auto-select external tools enabled for normal use.",
            "Enable only the search and integration tools you intend Row-Bot to use.",
            "Use eager mode temporarily when testing compatibility with an older model or integration.",
            "Review approvals before actions that write, send, or change external state.",
        ],
        "saved": "The capability-loading choice and tool settings are local global preferences. Per-task skill activation is stored separately and does not change this tool policy.",
        "troubleshoot": [
            "If an external tool is not found, check its integration configuration and the active Agent Profile.",
            "If a provider rejects tool schemas, choose a compatible tool-capable model or temporarily test eager mode.",
            "If retrieval is noisy, change compression mode or disable unnecessary search tools.",
        ],
    },
    "skills": {
        "title": "Settings: Skills",
        "desc": "Enable, disable, pin, browse, and review Smart Skills.",
        "shot": "settings-skills",
        "caption": "The Skills tab shows installed skills, pinning controls, browsing entry points, and per-skill state.",
        "overview": "Skills are instruction packs that teach Row-Bot how to handle a type of work. They do not run by themselves; enabled skills may be searched and loaded automatically for a matching task.",
        "controls": [
            "Enable and disable controls decide whether Row-Bot may use a skill.",
            "Pinning keeps useful skills easy to find and can make them more likely to be suggested.",
            "Browse Skills opens Skills Hub for installed, bundled, local, and marketplace-style sources.",
            "Skill details show purpose, source, status, and whether the skill is safe to activate.",
            "Search and filters help find skills by task, source, or installed state.",
            "Automatically loaded skills appear as an active chip in the task that selected them.",
        ],
        "workflow": [
            "Browse or search for a skill that matches the work.",
            "Open the detail view before enabling unfamiliar skills.",
            "Enable the skill, then start a chat or workflow that names the task.",
            "Disable skills you no longer want Row-Bot to consider.",
        ],
        "saved": "Skill enablement and pins are local Row-Bot preferences. Automatic selections are isolated per parent task or child Agent, restored when that task reopens, and bounded to five automatically selected skills per task.",
        "troubleshoot": [
            "If Row-Bot ignores a skill, check that it is enabled and relevant to the prompt.",
            "If a skill came from outside the app, review its instructions before enabling it.",
            "If skills make responses too specialized, unpin or disable the extras.",
        ],
    },
    "system": {
        "title": "Settings: System",
        "desc": "Configure Remote Access, workspace paths, browser and Computer Use behavior, tunnels, logs, and updates.",
        "shot": "settings-system",
        "caption": "The System tab contains local runtime choices, workspace access, Remote Access, tunnels, and diagnostic controls.",
        "overview": "System settings describe what the app may access, which trusted owner devices may connect, and how it runs on your machine. This is where you adjust Remote Access, workspace boundaries, shell behavior, browser automation, opt-in native Computer Use, tunnels, and diagnostics.",
        "controls": [
            "Workspace and filesystem controls define where Row-Bot may read or write when tools need local files.",
            "Shell and command controls affect whether command-running tools are available and how approvals apply.",
            "Browser controls affect page-reading and browser automation features.",
            "Computer Use controls keep native application automation separate, off by default, and gated by platform readiness, telemetry disclosure, and a verified optional runtime.",
            "Remote Access creates one-time desktop or compact owner invitations, checks or configures an owned Tailscale Serve route, explicitly enables LAN listening, and revokes devices or sessions.",
            "Window mode chooses whether Row-Bot opens in its own app window, the system browser, or asks at launch.",
            "Tunnel settings are for external callback use cases such as channels that need a public webhook URL.",
            "Log and diagnostic controls help support troubleshooting without changing your model setup.",
        ],
        "workflow": [
            "Keep workspace access narrow for everyday use.",
            "Enable shell or browser automation only when you intend to use those tools.",
            "Enable Computer Use only on an interactive Windows or macOS desktop after reviewing its separate setup and safety guide.",
            "Use [Remote Access](/docs/operations/remote-access) only after choosing the narrowest connection route and the presentation layout for the invited owner device.",
            "Use tunnels only for channels or integrations that explicitly require inbound webhooks.",
            "Collect logs from this tab when troubleshooting startup or provider issues.",
        ],
        "saved": "System settings are global for the active Row-Bot data directory. Remote Access stores hashed invitation and session secrets plus revocable owner-device records in the shared access database. Workspace files are not uploaded to Row-Bot; external providers may still receive content when a model or tool request sends it.",
        "privacy": "Remote reachability never grants access on its own. Every non-local browser needs a Row-Bot session. Treat unused invitation links as passwords, prefer Tailscale or HTTPS over remote LAN HTTP, and revoke lost or retired devices. Every authenticated browser is the same owner; compact and desktop layouts do not change authority. Review credential, account, channel, provider, or tool settings before enabling features that can contact outside services.",
        "troubleshoot": [
            "If Row-Bot cannot read a file, check whether it is inside the allowed workspace.",
            "If Computer Use is unavailable, open its setup card and follow the reported runtime or operating-system permission recovery step.",
            "If a remote URL shows a JSON access error, use the [Remote Access troubleshooting table](/docs/operations/remote-access#troubleshooting) instead of broadly trusting proxy headers or disabling the access gate.",
            "If a tunnel URL is unavailable, check the tunnel provider credentials and whether the tunnel is running.",
            "If the app opens in the wrong place, change Window mode and restart Row-Bot.",
        ],
    },
    "accounts": {
        "title": "Settings: Accounts",
        "desc": "Connect account-level integrations used by tools, setup flows, and channels.",
        "shot": "settings-accounts",
        "caption": "The Accounts tab lists account connections and sign-in actions without exposing private tokens.",
        "overview": "Accounts are service logins Row-Bot can use for tools such as email, calendar, or provider-specific subscription flows. They are separate from ordinary API keys and separate from Row-Bot itself, which does not require a Row-Bot account.",
        "controls": [
            "Connection cards show whether an account is connected, disconnected, or needs attention.",
            "Sign in and reconnect buttons start the provider's account flow.",
            "Disconnect removes Row-Bot's local access to that account.",
            "Status messages explain what a connected account enables.",
        ],
        "workflow": [
            "Connect only the accounts needed for the tasks you plan to run.",
            "Finish the provider sign-in flow in the browser when prompted.",
            "Return to Row-Bot and confirm the account shows connected.",
            "Review tools and approvals before asking Row-Bot to act through that account.",
        ],
        "saved": "Account tokens are stored in the operating system key store when available. Row-Bot keeps local metadata so it can show connection status.",
        "troubleshoot": [
            "If sign-in loops, disconnect and reconnect the account.",
            "If an account tool is unavailable, confirm the account is connected and the related tool is enabled.",
            "If secure storage is unavailable, you may need to sign in again after restart.",
        ],
    },
    "utilities": {
        "title": "Settings: Utilities",
        "desc": "Configure built-in helper tools and productivity utilities.",
        "shot": "settings-utilities",
        "caption": "The Utilities tab controls optional built-in tools and helper features.",
        "overview": "Utilities are smaller helper tools that support everyday work: calculations, file helpers, media helpers, formatting aids, or local integrations. They are useful when Chat needs a precise operation instead of a pure model answer.",
        "controls": [
            "Tool toggles decide which helper utilities Row-Bot may use.",
            "Configuration fields set defaults such as output locations or preferred formats.",
            "Status labels show whether a utility is ready or needs additional setup.",
            "Test or refresh actions confirm that an optional dependency is working.",
        ],
        "workflow": [
            "Enable utilities that match your normal tasks.",
            "Leave unfamiliar action tools disabled until you understand what they can change.",
            "Use approval mode to review file writes, shell actions, or external calls triggered by utilities.",
        ],
        "saved": "Utility settings are global. Tool results are shown in the active conversation and may also create files when the approved tool does so.",
        "troubleshoot": [
            "If a utility does not appear in Chat, confirm it is enabled.",
            "If a utility needs a dependency, follow its status message before retrying.",
            "If an output file is missing, check the allowed workspace and approval history.",
        ],
    },
    "tracker": {
        "title": "Settings: Tracker",
        "desc": "Configure recurring activity, habit, symptom, and health tracking surfaces.",
        "shot": "settings-tracker",
        "caption": "The Tracker tab manages personal tracking categories, views, charts, and logged data.",
        "overview": "Tracker is for structured personal logs such as recurring activities, habits, symptoms, or health events. It gives Row-Bot a more organized way to store and review repeated observations than free-form chat alone.",
        "controls": [
            "Category controls decide what kinds of events can be logged.",
            "View and chart options change how tracker history is summarized.",
            "Import or cleanup actions manage existing tracker data.",
            "Privacy-oriented status text explains that tracker records stay in local app data unless you ask a provider or channel to use them.",
        ],
        "workflow": [
            "Choose the categories you actually want to track.",
            "Log events consistently from Chat or tracker-aware workflows.",
            "Review trends in the tracker view before asking Row-Bot to summarize them.",
            "Remove categories you no longer use to keep prompts focused.",
        ],
        "saved": "Tracker data is local Row-Bot data. It can influence answers only when Row-Bot is allowed to retrieve or summarize it.",
        "troubleshoot": [
            "If a chart is empty, confirm there are saved events in that category.",
            "If a summary seems wrong, inspect the raw tracker entries first.",
            "If privacy matters, do not include tracker details in prompts sent to hosted providers.",
        ],
    },
    "knowledge": {
        "title": "Settings: Knowledge",
        "desc": "Configure memory, graph review, embeddings, document knowledge, and wiki export.",
        "shot": "settings-knowledge",
        "caption": "The Knowledge settings tab controls memory, graph behavior, document knowledge, embeddings, and export options.",
        "overview": "Knowledge settings decide how Row-Bot stores useful information, retrieves it later, and organizes it into a local knowledge graph. This affects what Row-Bot can remember between conversations.",
        "controls": [
            "Memory controls decide whether Row-Bot can save and recall useful facts.",
            "Embedding controls affect how memories and documents become searchable.",
            "Graph review controls decide how extracted entities and relationships are handled.",
            "Dream cycle options control background organization when that feature is enabled.",
            "Wiki export creates local markdown-style views of selected knowledge records.",
        ],
        "workflow": [
            "Enable memory only if you want Row-Bot to remember useful details.",
            "Set embeddings before indexing a large document or memory collection.",
            "Use the Knowledge Home tab to review and correct important entries.",
            "Export a wiki when you want a readable local snapshot outside the app.",
        ],
        "saved": "Knowledge records live in local Row-Bot data. They can be sent to a model provider only as relevant prompt context when you ask Row-Bot to use them.",
        "troubleshoot": [
            "If recall feels stale, refresh vectors or review extraction journals.",
            "If Row-Bot remembers something wrong, edit or remove the knowledge entry.",
            "If background organization is noisy, reduce or disable dream cycle behavior.",
        ],
    },
    "buddy": {
        "title": "Settings: Buddy",
        "desc": "Configure Row-Bot's companion behavior, visibility, look, motion, and generated looks.",
        "shot": "settings-buddy",
        "caption": "The Buddy tab controls the sidebar companion, desktop overlay, personality, look, motion, and optional custom-look generation.",
        "overview": "Buddy is Row-Bot's visual companion. It can live in the sidebar, float in the workspace, or open as a desktop overlay. Buddy reflects app state but does not change model behavior by itself.",
        "controls": [
            "Enable switches decide whether Buddy appears in the sidebar, workspace, or desktop overlay.",
            "Open and close overlay buttons control the separate desktop overlay window.",
            "Companion personality changes the tone of Buddy status cues.",
            "Bubble style changes how visual status appears.",
            "Look cards choose a bundled or custom Buddy appearance.",
            "Concept and style notes describe a custom look when you use Generate full Buddy.",
            "Use still only keeps current art without animated motion.",
            "Retry motion rebuilds motion for an existing look.",
        ],
        "workflow": [
            "Start with the bundled look and sidebar mode.",
            "Enable desktop overlay only if you want Buddy outside the main Row-Bot window.",
            "Generate a custom look after providers and media models are ready.",
            "Use still only if motion generation is unavailable or distracting.",
        ],
        "saved": "Buddy preferences and custom Buddy assets are local app data. Generating a custom look may call a configured media provider depending on your setup.",
        "troubleshoot": [
            "If Buddy does not appear, check the enable switches and refresh the app.",
            "If the overlay is stuck, use Close overlay, then Open overlay again.",
            "If custom generation fails, check provider readiness and try still-only mode.",
        ],
    },
    "voice": {
        "title": "Settings: Voice",
        "desc": "Configure dictation, realtime talk, read-aloud, voice models, devices, and diagnostics.",
        "shot": "settings-voice",
        "caption": "The Voice tab manages Talk, Dictate, read-aloud, voice model readiness, and diagnostics.",
        "overview": "Voice gives Row-Bot microphone input and optional spoken output. Dictate turns speech into text for the composer. Talk is a conversation mode. Realtime voice uses a low-latency provider path when configured.",
        "controls": [
            "Talk settings decide whether spoken input submits directly to Row-Bot.",
            "Dictate settings decide whether speech is inserted into the composer for review before sending.",
            "Read-aloud settings control spoken assistant responses.",
            "Local voice controls use local speech components where available.",
            "Realtime voice controls use provider-backed low-latency voice models.",
            "Device controls select microphone and output devices.",
            "Voice Models shows runtime defaults and provider voice models.",
            "Diagnostics checks local audio and provider readiness.",
        ],
        "workflow": [
            "Use Dictate first if you want to review text before sending.",
            "Use Talk when you want hands-light conversation and are comfortable with immediate submission.",
            "Choose local voice for privacy and offline-style behavior when supported.",
            "Choose realtime voice when latency matters and you accept provider requirements, cost, and internet use.",
        ],
        "saved": "Voice preferences are global. Transcribed text belongs to the active thread once submitted. Provider-backed voice can send audio or transcript data to the selected provider.",
        "troubleshoot": [
            "If the microphone is silent, check the selected input device and browser/app permissions.",
            "If realtime voice is unavailable, configure a compatible provider in Providers.",
            "If Talk submits too quickly, use Dictate mode instead.",
        ],
    },
    "channels": {
        "title": "Settings: Channels",
        "desc": "Configure Telegram, WhatsApp, Discord, Slack, SMS, delivery, and health checks.",
        "shot": "settings-channels",
        "caption": "The Channels tab configures messaging connectors, credentials, tunnel use, pairing, and start/stop controls.",
        "overview": "Channels let Row-Bot receive or send messages through external apps. Use them when you want Row-Bot available outside the desktop window or when workflows should deliver results somewhere specific.",
        "controls": [
            "Each channel expands into credential fields, status, and controls.",
            "Save stores that channel's settings.",
            "Start and Stop control the channel runtime.",
            "Tunnel settings connect the channel to an externally reachable webhook when required.",
            "DM Pairing Code helps approve a user before private-message access is allowed.",
            "Paired Users shows who is approved and allows revocation.",
            "Setup Guide explains provider-specific prerequisites.",
        ],
        "workflow": [
            "Configure tunnel credentials in System if the channel needs a webhook.",
            "Add the channel's required token, URL, or account details.",
            "Save, then Start the channel.",
            "Pair or approve users before trusting inbound private messages.",
            "Use workflow delivery defaults to decide where automated results go.",
        ],
        "saved": "Channel settings are global. Messages sent through a channel leave the local app and follow that platform's rules.",
        "troubleshoot": [
            "If a channel will not start, check required fields and tunnel status.",
            "If messages do not arrive, verify webhook URLs and platform permissions.",
            "If a user should no longer have access, revoke them from Paired Users.",
        ],
    },
    "mcp": {
        "title": "Settings: MCP",
        "desc": "Add, test, import, browse, enable, and troubleshoot external MCP servers.",
        "shot": "settings-mcp",
        "caption": "The MCP tab manages external MCP tool servers, global enablement, per-server settings, tests, imports, and diagnostics.",
        "overview": "MCP, the Model Context Protocol, lets Row-Bot use tools provided by another local or remote server. Treat MCP servers like extensions: only connect servers you trust and understand.",
        "controls": [
            "Enable MCP is the global on/off switch for external MCP tools.",
            "Add Server opens fields for name, transport, command, arguments, or URL.",
            "Import Config accepts a server configuration from another source.",
            "Browse MCP Servers searches directories for server candidates.",
            "Diagnostics opens a health view for troubleshooting.",
            "Per-server enablement decides whether a configured server can provide tools.",
            "Test checks a server before you rely on it.",
            "Edit, refresh, and delete manage saved server definitions.",
            "Approval and advanced options decide how MCP tools interact with Row-Bot's safety policy.",
        ],
        "workflow": [
            "Leave MCP disabled until at least one trusted server is configured.",
            "Add or import a server, then save it disabled first if you are unsure.",
            "Test the server and inspect the available tools.",
            "Enable the server only when you are comfortable with what those tools can access.",
        ],
        "saved": "MCP server definitions are local settings. A server may still access files, accounts, or networks according to its own implementation, so review its command or URL.",
        "troubleshoot": [
            "If a server test fails, check command, arguments, URL, and local dependencies.",
            "If tools do not appear in Chat, confirm the global and server toggles are both enabled.",
            "If a tool asks for risky access, reject the approval and inspect the server config.",
        ],
    },
    "plugins": {
        "title": "Settings: Plugins",
        "desc": "Manage installed plugins, marketplace installs, configuration, and promoted Custom Tools.",
        "shot": "settings-plugins",
        "caption": "The Plugins tab lists installed plugins, marketplace actions, enablement, configuration, and Custom Tool promotion paths.",
        "overview": "Plugins add local bundles of tools, skills, apps, or integrations. They can make Row-Bot more capable, but they should be treated as code that runs with local app permissions.",
        "controls": [
            "Installed plugin cards show version, status, and available actions.",
            "Enable and disable controls decide whether plugin capabilities are active.",
            "Configure opens plugin-specific settings when provided.",
            "Update and remove actions manage local plugin installations.",
            "Marketplace opens discovery and install previews.",
            "Custom Tools promotion turns reviewed local tools into reusable plugin-like capabilities.",
        ],
        "workflow": [
            "Install plugins only from sources you trust.",
            "Read the plugin description and requested fields before enabling it.",
            "Configure required settings, then test with a low-risk prompt.",
            "Disable or remove plugins you no longer use.",
        ],
        "saved": "Installed plugins and plugin settings are local files. Plugin secrets should use secret storage when the plugin supports it.",
        "troubleshoot": [
            "If a plugin does not load, check its manifest and dependency messages.",
            "If plugin tools do not appear, confirm the plugin is enabled and restart if instructed.",
            "If a plugin behaves unexpectedly, disable it first, then inspect its configuration.",
        ],
    },
    "preferences": {
        "title": "Settings: Preferences",
        "desc": "Customize assistant identity, launch behavior, background intelligence, updates, and migration.",
        "shot": "settings-preferences",
        "caption": "The Preferences tab controls user identity, assistant personality, launch behavior, background intelligence, updates, and migration helpers.",
        "overview": "Preferences are personal choices for how Row-Bot should address you, how it should behave at launch, and which background features should be active.",
        "controls": [
            "Name helps Row-Bot address you naturally.",
            "Personality changes the assistant's default tone.",
            "Launch and window preferences decide how the app opens.",
            "Background intelligence controls affect optional automatic review or organization features.",
            "Dream cycle timing controls when background organization may run.",
            "Update controls decide how Row-Bot checks or reports available versions.",
            "Migration helpers support users moving from earlier app data.",
        ],
        "workflow": [
            "Set your name and preferred assistant tone first.",
            "Choose a launch mode that fits how you use the desktop app.",
            "Enable background features only when you want Row-Bot organizing or reviewing local data outside direct chat turns.",
            "Use migration helpers only when moving from an older installation or renamed data set.",
        ],
        "saved": "Preferences are global local settings. They affect new chats and app behavior, while existing threads can still have their own model, profile, or approval choices.",
        "troubleshoot": [
            "If the app opens unexpectedly, adjust launch preferences and restart.",
            "If background activity appears in Monitor, review Preferences and Knowledge settings.",
            "If migration looks incomplete, do not delete old data until you confirm the new app has what you need.",
        ],
    },
}


def settings_page(slug: str, meta: dict[str, object]) -> str:
    controls = "\n".join(f"- {item}" for item in meta["controls"])
    workflow = "\n".join(f"{idx}. {item}" for idx, item in enumerate(meta["workflow"], 1))
    trouble = "\n".join(f"- {item}" for item in meta["troubleshoot"])
    privacy = str(
        meta.get(
            "privacy",
            "Review credential, account, channel, provider, or tool settings "
            "before enabling features that can contact outside services. "
            "Local-only features stay on your machine until you ask Row-Bot "
            "to use a provider, account, channel, MCP server, plugin, or tool "
            "that sends data elsewhere.",
        )
    )
    extra_screenshot = (
        '\n\n<Screenshot id="settings-remote-access" alt="Remote Access controls in '
        'Row-Bot System settings." caption="Remote Access keeps private '
        'reachability, one-time owner invitations, connected devices, and '
        'explicit LAN access in one owner-only section." />'
        if slug == "system"
        else ""
    )
    return f"""
# {meta['title']}

{meta['overview']}

<Screenshot id="{meta['shot']}" alt="{meta['title']} in Row-Bot." caption="{meta['caption']}" />{extra_screenshot}

## Where To Find It

Open **Settings**, then choose **{meta['title'].split(': ', 1)[1]}** from the left tab list.

## Controls

{controls}

## Common Workflow

{workflow}

## What Is Saved

{meta['saved']}

## Privacy And Safety

{privacy}

## Control-Level Reference

The [generated Settings Controls reference](/docs/reference/generated/settings-controls#{slug}-controls) lists the visible controls found in the current application source, including defaults, allowed values when they are declared inline, dependencies, restart notes, security notes, and source locations.

## Troubleshooting

{trouble}
"""


def home_page(slug: str, meta: dict[str, object]) -> str:
    launch = "\n".join(f"- {item}" for item in meta["launches"])
    return f"""
# {meta['title']}

{meta['summary']}

<Screenshot id="{meta['shot']}" alt="{meta['title']} in Row-Bot." caption="{meta['caption']}" />

## What This Tab Is For

Use this Home tab as a quick entry point. It shows current state and launch controls, while the full walkthrough lives in the [{meta['deep_label']}]({meta['deep']}).

## What You Can Launch

{launch}

## What Is Saved

Changes made from this tab apply to the feature it opens: workflows save as local automations, Designer projects save as local design projects, Developer workspaces save as local workspace records, Knowledge changes save to the local knowledge store, and Monitor filters affect the current review view.

## Next Step

Open the [{meta['deep_label']}]({meta['deep']}) when you need the control-by-control guide.
"""


def main() -> int:
    write(
        "index.mdx",
        "Row-Bot Documentation",
        "A complete public user guide for installing, configuring, and using Row-Bot end to end.",
        """
# Row-Bot Documentation

Row-Bot is a local-first AI workbench for people who want provider-aware models, parent-led agents, durable documents, memory, tools, workflows, design, code help, integrations, and voice in one controllable system. Run it as a desktop application or a private authenticated server; this guide explains installation, Docker deployment, owner access, the main interface, settings, and every explicit external route.

These pages describe Row-Bot 4.8.0, the release represented by this source tree.

<Screenshot id="home-knowledge" alt="Row-Bot desktop workspace showing the Knowledge graph, conversations, tools, workflows, channels, Activity Center, and terminal." caption="The Knowledge workspace brings Row-Bot's local graph together with conversations, tools, workflows, channels, activity, approvals, and terminal output." />

## Start Here

- [Getting Started](/docs/getting-started/) explains the install path, first launch, and setup choices.
- [Row-Bot Interface](/docs/app-shell/navigation) tours the sidebar, thread list, Home tabs, Activity Center, Buddy, Settings, and terminal.
- [Chat](/docs/chat/) explains conversations, composer controls, attachments, model selection, approvals, and tool results.
- [Reasoning Controls](/docs/chat/reasoning-controls) explains model-specific effort, thinking toggles, token budgets, and provider-default fallback behavior.
- [Settings](/docs/settings/) explains every configuration tab and what each choice changes.
- [Profiles, Goals, And Agents](/docs/profiles-goals-agents/) explains reusable roles, bounded goals, parent-led work waves, dependencies, and recovery.
- [Remote Access And Server Mode](/docs/operations/remote-access) explains invitations, sessions, Tailscale, LAN, HTTPS proxies, and browser-local voice.
- [Docker And VPS Operations](/docs/operations/docker/) explains the official container, persistent data and secrets, backup, upgrade, and rollback.
- [Computer Use](/docs/computer-use/) explains the opt-in native desktop tool, setup, live controls, and safety boundaries.

## Feature Guides

- [Workflows](/docs/guides/workflows) for repeatable background work and scheduled agents.
- [Reasoning Controls](/docs/chat/reasoning-controls) for choosing the supported reasoning depth of the active model without changing models.
- [Designer Studio](/docs/designer/) for creating pages, slides, mockups, branded assets, and exportable designs.
- [Developer Studio](/docs/developer/) for folders, repositories, code chat, inspectors, commands, and sandbox modes.
- [Knowledge](/docs/knowledge/) for local memory, durable document ingestion, graph review, and background organization.
- [Computer Use](/docs/computer-use/) for target-window automation in native Windows and macOS applications.
- [Compact And Native Desktop Layouts](/docs/mobile-native/) for the same owner product across phone-safe and desktop presentations.
- [Remote Access And Server Mode](/docs/operations/remote-access) for owner invitations, Tailscale, LAN, SSH, Docker, HTTPS proxies, and remote browser voice.
- [Docker And VPS Operations](/docs/operations/docker/) for the official image, hardened Compose, credentials, backup, upgrade, rollback, and recovery.
- [Monitor](/docs/monitor/) for logs, journals, channel state, and background activity.
- [Skills Hub](/docs/skills/) for browsing, enabling, creating, and reviewing skills.
- [Progressive Tools And Skills](/docs/guides/progressive-tools-and-skills) for automatic external-tool discovery, per-task skill selection, compatibility mode, and safety boundaries.
- [Channels](/docs/integrations/channels), [MCP](/docs/integrations/mcp), and [Plugins](/docs/integrations/plugins) for integrations.
- [Extend Row-Bot](/docs/extending/) to choose safely between Skills, Custom Tools, plugins, MCP, channels, and accounts.
- [Operations, Data, And Recovery](/docs/operations/) for backups, restore, updates, repair, uninstall, and diagnostic sharing.
- [Voice and Buddy](/docs/voice-and-buddy/) for speech input, Talk, Dictate, read-aloud, and the visual companion.

## How To Read These Docs

Each major page explains what the feature is, where to find it, the important controls, a common workflow, what is saved, privacy and safety implications, and troubleshooting. Screenshot captions describe the product UI so you can connect the text to what you see in the app.

## References

Use [Reference](/docs/reference/) when you need tables of tools, providers, settings tabs, channels, skills, MCP servers, plugins, storage, or approval behavior. The guided pages should be your first stop; the reference pages are for lookup.
""",
        screenshot=True,
    )

    write(
        "getting-started/index.mdx",
        "Getting Started",
        "Install Row-Bot, complete first launch, and learn the first choices that matter.",
        """
# Getting Started

Start here if you are new to Row-Bot. You only need three things for a useful first session: the app installed, one working model path, and a basic understanding of where Row-Bot stores local data.

## The Short Path

1. Install Row-Bot from the release package for your platform.
2. Launch the app and complete the first-run wizard.
3. Choose one model path: local Ollama, a hosted provider, a subscription account, or a custom endpoint.
4. Send a first chat message.
5. Open Settings later when you are ready for documents, workflows, Designer, Developer, channels, MCP, plugins, Skills, Buddy, or voice.

## Important Concepts

- **Local app** means Row-Bot runs on your machine and opens a local desktop or browser window.
- **Data directory** means the folder where Row-Bot keeps conversations, memories, documents, settings, workflows, logs, Designer projects, Developer workspaces, skills, plugins, and local integration state.
- **Model provider** means the service or local runtime that supplies a model. Ollama is local; API and subscription providers usually use the internet.
- **Tools** are actions Row-Bot can take, such as reading files, searching documents, using a browser, running Developer commands, or sending through a channel.
- **Approvals** are prompts that ask you to review sensitive actions before Row-Bot proceeds.

## Pages In This Section

- [Installation](/docs/getting-started/installation)
- [First Launch](/docs/getting-started/first-launch)
- [Row-Bot Interface](/docs/app-shell/navigation)
""",
    )

    write(
        "getting-started/installation.mdx",
        "Installation",
        "Install Row-Bot from a release package or run it from source with clear next steps.",
        """
# Installation

For ordinary use, install Row-Bot from the latest release package for your operating system. Running from source is useful for contributors, testers, and people who want to inspect or modify the app.

## Install From A Release

1. Download the latest Row-Bot release for Windows, macOS, or Linux.
2. Run the installer or unpack the archive.
3. Launch Row-Bot from the Start Menu, Applications, app launcher, or the provided command.
4. Let the first-run wizard open and choose a model path.

The installed app starts a local Row-Bot process and opens the UI in a desktop window or browser, depending on your platform and launch preference. If the usual port is busy, Row-Bot chooses another local port.

## Run From Source

Use the source path if you are developing Row-Bot or testing changes:

1. Clone the repository.
2. Create a Python environment.
3. Install uv and sync the locked Python dependencies with `uv sync --locked --all-extras --group test`.
4. Install the docs-site dependencies only if you are working on documentation.
5. Run the app entry point from the repository.

`requirements.txt` is a generated pip export from `uv.lock` for installer compatibility. It is available as a fallback for environments that cannot use uv, but source dependency changes should be made in `pyproject.toml`.

Source runs use the same local data concepts as the packaged app. Keep test data separate from personal data when experimenting.

## Local Data Directory

Row-Bot stores local app data under the active Row-Bot data directory. This includes conversations, memories, model/provider metadata, workflows, Designer projects, Developer workspace records, documents, logs, skills, plugins, Buddy assets, channel settings, and MCP settings.

Advanced users can set `ROW_BOT_DATA_DIR` before launch to use a separate data directory for testing or portable runs. Do this before starting the app, and remember that Row-Bot will treat that folder as the active local data store for the whole process.

## After Installing

Continue to [First Launch](/docs/getting-started/first-launch). You can finish optional setup later from the Setup Center and Settings.

The standard desktop install stays on local loopback. To connect another trusted computer or phone, finish local setup first and then follow [Remote Access And Server Mode](/docs/operations/remote-access). For a headless or container deployment, read that guide before publishing a port or creating the first owner invitation.
""",
    )

    write(
        "getting-started/first-launch.mdx",
        "First Launch",
        "Complete the first-run wizard, choose a model path, and revisit setup later.",
        """
# First Launch

The first-run wizard appears until setup is complete. Its job is to help you connect one working model path so Row-Bot can answer a first chat message. Everything else can be configured later.

<Screenshot id="first-launch-setup-wizard" alt="Row-Bot first launch setup wizard." caption="The first-run wizard helps you choose a local, hosted, subscription, or custom model path before optional setup begins." />

## Choose A Model Path

A model is the AI system that writes responses and reasons through tasks. Row-Bot can work with several kinds of model providers:

| Path | Good For | Tradeoffs |
| --- | --- | --- |
| Local Ollama | Privacy-focused local chats and tool use when your machine has enough resources. | Requires installing models locally. Larger models need more memory and disk space. |
| Hosted API provider | Strong models without local downloads. | Requires an API key, internet access, and provider billing. Prompt content can be sent to that provider. |
| Subscription account | Using a supported account-backed provider from inside Row-Bot. | Requires sign-in or token import and follows that provider's account terms. |
| Custom endpoint | Advanced local or self-hosted runtimes such as LM Studio, vLLM, llama.cpp, LocalAI, LiteLLM, or SGLang. | Requires endpoint details and compatible model behavior. Tool use needs enough context and function-calling support. |

For beginners, start with the provider path you already trust. If you are unsure, Ollama is the simplest local-first path, while an API provider is usually the quickest path to strong hosted models.

## Install Private Knowledge Search

Every normal desktop, source, and official Docker install uses the same setup step. The wizard offers **Mixedbread Embed Large v1**, a separate local model for semantic memory and document search, as a checked-by-default 675 MB download. It is not the model that writes chat responses.

The download starts when you finish setup and requires internet access to Hugging Face. Files go into Row-Bot's private cache; documents and memories are not uploaded, and normal recall stays offline after the initial download. Docker keeps this cache in its named `/data` volume.

You can uncheck the option and finish without it. Row-Bot continues with bounded lexical and graph fallback, so chat and memory do not stop working. Later, open [Settings → Documents](/docs/settings/documents) and use **Download model**. **Retry local load** retries an already cached model; **Repair local model** deliberately replaces a damaged download. Rebuild the document and memory vectors after changing the embedding provider or model.

<Screenshot id="settings-documents" alt="Row-Bot Documents settings showing the Mixedbread local embedding model and download, retry, repair, and vector rebuild controls." caption="Settings → Documents shows whether the local embedding model is ready and provides explicit download, retry, repair, and index rebuild actions." />

## Setup Center

<Screenshot id="setup-center" alt="Row-Bot Setup Center." caption="Setup Center lets you finish optional setup areas later without blocking the first chat." />

Setup Center is the place to finish or revisit optional setup after the first launch. It can guide you through models, documents, workflows, Designer, Developer, channels, accounts, MCP, plugins, Buddy, and voice. The wizard gets you started; Setup Center helps you build out the rest of your app over time.

## Privacy, Cost, And Credentials

Local model runs can stay on your machine. Hosted, subscription, realtime voice, web search, account, channel, MCP, and plugin features can contact outside services when configured and used. Provider keys and account tokens should be entered only through the relevant Settings or account flow. Cost depends on the provider or service you choose.

## Troubleshooting

- If the wizard cannot find a local model, start Ollama and install a model first.
- If a hosted provider connects but no models appear, refresh Providers and Models.
- If the private knowledge model download fails, check internet access to Hugging Face and try finishing setup again, or uncheck it and install it later from Settings → Documents.
- If you skip optional setup, open Setup Center or Settings later.
""",
        screenshot=True,
    )

    write(
        "app-shell/navigation.mdx",
        "Row-Bot Interface",
        "Navigate Row-Bot's sidebar, threads, Home tabs, Activity Center, Buddy, Settings, and terminal.",
        """
# Row-Bot Interface

The Row-Bot Interface is the main workspace you see after launch. It combines conversations, Home tabs, status panels, approvals, workflows, Buddy, Settings, and terminal output in one local app window.

<Screenshot id="app-shell-overview" alt="Row-Bot main interface." caption="The main interface shows the left sidebar, Home tabs, central work area, Activity Center, Buddy, Settings, and terminal." />

## Left Sidebar

- **Home** returns to the Home tabs: Workflows, Designer, Developer, Knowledge, and Monitor.
- **New** starts a fresh conversation thread.
- **Conversations** shows recent threads. The sidebar keeps the list short so the workspace stays usable; use **Show all** when you need older threads.
- **Thread menu** on a conversation row opens actions such as rename and delete.
- **Rename** changes the visible thread title without changing the messages.
- **Delete** removes the thread after confirmation. Use it carefully if you still need the conversation history.
- **Agent profiles** opens the profile selector and profile management area.
- **Buddy** appears near the bottom when enabled.
- **Settings** opens the full configuration dialog.

## Show All Threads

When you have more threads than the sidebar shows, use Show All from the conversations area. The dialog is for search and cleanup: find an older thread, reopen it, rename it, or delete it after confirming. Start a new thread when the task has a new goal; continue an existing thread when the earlier context still matters.

## Home Tabs

The central Home area has five tabs:

- **Workflows** for saved automations and background agents.
- **Designer** for starting design projects.
- **Developer** for opening code workspaces and Custom Tools.
- **Knowledge** for memory, document, and graph review.
- **Monitor** for logs, journals, channel state, and background activity.

Home pages in these docs are short entry-point pages. The dedicated feature pages are the full guides.

## Activity Center And Right Drawer

The right side summarizes what needs attention:

- **Current goals and agents** shows running or waiting goal-mode and child-agent activity.
- **Approvals** shows pending approval prompts from chats, child agents, and background work.
- **Workflows** shows active and upcoming workflow state, plus run and new controls.
- **Launch** lets you choose a workflow and run it manually.
- **Insights** expands supporting status and suggestions when available.

Use the Activity Center when you want to know whether Row-Bot is idle, waiting for you, running background work, or ready to launch a workflow. Child-agent approvals also appear in the parent thread so you can decide without leaving the conversation.

## Terminal Panel

The terminal panel at the bottom shows command-oriented output when a tool, Developer workspace, or local runtime exposes it through the UI. It is not a general replacement for your system terminal. Command execution still follows Row-Bot's active tool availability, workspace boundaries, and approval mode.

## Agent Profiles

Agent profiles change how Row-Bot behaves for a thread or specialist run. The sidebar profile control opens the profile selector; the detailed walkthrough is in [Agent Profiles](/docs/app-shell/agent-profiles).

## What Is Saved

Threads, thread names, selected models, profile choices, workflow records, knowledge records, settings, and logs are saved in the local Row-Bot data directory. Some choices are thread-specific; Settings usually changes global app behavior.

## Troubleshooting

- If a thread is missing from the sidebar, open Show All and search.
- If a panel looks stale, use the visible refresh control or reload the app.
- If a button is disabled, check the related Settings tab and Monitor for readiness messages.
""",
        screenshot=True,
    )

    write(
        "app-shell/agent-profiles.mdx",
        "Agent Profiles",
        "Choose and manage Agent Profiles for thread behavior, tool access, skills, and specialist work.",
        """
# Agent Profiles

Agent Profiles are presets for how Row-Bot should act. A profile can change tone, tool availability, skill hints, approval posture, and specialist behavior. Use them when the same app needs to behave differently for research, coding, planning, design, or careful local-only work.

## Where To Find Profiles

Open the **Agent profiles** area in the left sidebar, or use the profile control in a chat header when it is visible. The dialog lets you choose a profile for the current thread and inspect available built-in profiles.

## What The Profile Dialog Does

- **Profile list** shows available built-in and local profiles.
- **Selected profile** is the behavior Row-Bot will use for the current thread or new specialist run.
- **Description** explains what the profile is optimized for.
- **Tools and skills** indicate which capabilities the profile may prefer or restrict.
- **Default behavior** explains whether the profile is meant for general chat, Agent Mode, Developer work, Designer work, or controlled tasks.
- **Save or apply** records the choice for the current thread when the dialog offers it.

## Practical Examples

- Use a research-style profile for web and document synthesis.
- Use a coding profile inside Developer Studio so Row-Bot pays attention to files, commands, tests, and change review.
- Use a cautious profile when you want more approvals and less external activity.
- Use a design profile when you are working in Designer Studio and want visual output iteration.

## What Is Saved

Profile selection can be saved on a thread, so future turns keep the same behavior. Built-in profile definitions are part of Row-Bot; local custom profiles are stored in local app data when supported.

## Privacy And Safety

A profile can influence which tools Row-Bot prefers, but it does not bypass approvals. If a profile enables broader tool use, review approval prompts before allowing file writes, browser actions, shell commands, account actions, MCP calls, plugin tools, or channel sends.

## Troubleshooting

- If Row-Bot behaves too narrowly, switch back to a general profile.
- If a tool is unavailable, check both the profile and the related Settings tab.
- If a profile choice does not stick, confirm you applied it to the active thread.
""",
    )

    write(
        "chat/index.mdx",
        "Chat",
        "Use Row-Bot chat, composer controls, model choices, attachments, tools, approvals, and history.",
        """
# Chat

Chat is the main place to ask Row-Bot for help. A chat thread can stay simple, or it can use models, memory, documents, tools, approvals, attachments, skills, voice, workflows, and specialist profiles.

<Screenshot id="chat-main" alt="Row-Bot chat view with conversation and composer controls." caption="The chat view shows the active conversation, model context, tool-result history, and message composer controls." />

## UI Walkthrough

- **Thread header** shows the current thread title and actions such as rename, profile selection, model state, and export when available.
- **Transcript** contains user messages, assistant responses, tool results, charts, images, reasoning sections, and status messages.
- **Composer** is where you type the next request. Use plain language and include the outcome you want.
- **Send** submits the composer text to the selected model.
- **Stop** appears while Row-Bot is responding and asks the current run to stop.
- **Regenerate or retry controls** appear when a response can be run again.
- **Model picker** chooses the model for this thread. See [Model Picker](/docs/chat/model-picker).
- **Thinking control** appears beside the model only when that exact model exposes supported reasoning choices. See [Reasoning Controls](/docs/chat/reasoning-controls).
- **Approval mode** controls how sensitive actions are reviewed for this thread.
- **Attachments and context controls** add files or local context to the current request.
- **Skills and slash commands** help start structured tasks when available. Enabled skills can also be selected progressively for the current task.
- **Voice buttons** use Dictate or Talk when voice is configured.
- **Tool traces** show what tools Row-Bot used and what came back.
- **Approval prompts** pause gated actions until you approve or reject them. When a child agent needs approval, the prompt appears in the parent thread too.

## Beginner Workflow

1. Start a new thread.
2. Choose a model that is ready for chat.
3. Ask one clear question or task.
4. Attach files only when the task needs them.
5. Review any approval prompt before allowing Row-Bot to act.
6. Rename the thread when it becomes useful enough to keep.

## Power Workflow

1. Choose an Agent Profile for the kind of work.
2. Pick a stronger tool-capable model.
3. Attach documents or enable retrieval only for relevant context.
4. Let Row-Bot use tools, but approve file writes, browser actions, shell commands, account actions, channel sends, MCP calls, and plugin tools deliberately.
5. Export or continue the thread after the work is complete.

## What Is Saved

Thread names, messages, selected model overrides, per-model reasoning selections, approval mode, profile selection, attachments copied into Row-Bot-managed storage, and tool results are saved locally. Some external providers may receive prompt content when you choose their models.

## Privacy And Safety

Local model runs can stay on your machine. Hosted models, web search, browser actions, account tools, MCP servers, plugins, and channels can send data outside the app when configured and used. Row-Bot should not insert secrets into chat; enter credentials only through Settings or the provider's sign-in flow.

## Troubleshooting

- If Row-Bot cannot answer with the current model, choose a more capable model in the picker.
- If the Thinking control is missing or a saved choice resets, read [Reasoning Controls](/docs/chat/reasoning-controls#troubleshooting).
- If attachments are ignored, confirm the files finished uploading and are relevant to the prompt.
- If a tool is unavailable, check Settings, the current Agent Profile, and approval mode.
- If an external tool is enabled but not selected automatically, use a more specific request or review [Progressive Tools And Skills](/docs/guides/progressive-tools-and-skills).
- If a run is stuck, press Stop, then retry with a narrower prompt.
""",
        screenshot=True,
    )

    write(
        "chat/model-picker.mdx",
        "Model Picker",
        "Choose, pin, and troubleshoot Row-Bot models across local, hosted, subscription, and custom providers.",
        """
# Model Picker

The Model Picker chooses which model powers the current chat thread. It is where provider setup becomes visible in day-to-day use.

## Where Models Come From

Models appear after Row-Bot discovers them from a connected provider or local runtime:

- Local Ollama models come from the Ollama service running on your machine.
- API provider models come from a provider connected in Settings -> Providers.
- Subscription models come from a supported account-backed provider after sign-in or token import.
- Custom endpoint models come from the compatible endpoint details you provide.
- Pinned Quick Choices come from Settings -> Models and are shown first for convenience.

## UI Walkthrough

- **Pinned models** are your quick picks for everyday use.
- **Provider groups** keep local, API, subscription, and custom endpoint models understandable.
- **Search** narrows long model lists.
- **Capability labels** help distinguish chat, tool-capable, vision, reasoning, embedding, media, and voice models.
- **Disabled or missing providers** stay out of normal selection until they are configured and healthy.
- **Thread override** means the selected model applies to the active thread without changing your global default.

## Choosing Well

Use a smaller or local model for quick private chats. Use a stronger tool-capable model for workflows, Developer Studio, Designer Studio, long context, or multi-step tool use. For local and self-hosted endpoints, prefer a context window large enough for Row-Bot's instructions and tool schemas.

Reasoning choices belong to the exact provider-qualified model, not just its display name. After choosing a supported model, use the conditional Thinking control described in [Reasoning Controls](/docs/chat/reasoning-controls).

## Pinning Models

Open Settings -> Models, refresh the catalog, and pin the models you use often. Pinning does not create a new model; it just puts an existing discovered model into your Quick Choices.

## Troubleshooting

- If a model is missing, refresh Providers, then refresh Models.
- If a provider is connected but disabled, review the provider row message.
- If a model appears but fails with tools, choose a model labeled or tested for Agent Mode.
- If custom endpoint models look duplicated, use the provider-qualified name.
""",
    )

    write(
        "chat/reasoning-controls.mdx",
        "Reasoning Controls",
        "Choose provider-aware reasoning effort, thinking toggles, and token budgets for the active Row-Bot model.",
        """
# Reasoning Controls

Reasoning controls let you change how much supported models reason before answering without switching models. Row-Bot shows only choices that the exact provider-qualified model reports or that Row-Bot can identify from a maintained model route.

## Where To Find The Control

- In desktop Chat, Designer Studio, and Developer Studio, the **Thinking** control sits beside the model picker in the composer. On narrow desktop windows, the composer progressively compacts labels while keeping each icon paired with its own menu.
- In the compact mobile layout, open **Chat controls**. Reasoning appears below Model when the selected model supports it.
- In Chat or a connected messaging channel, use `/reasoning` to inspect the current choice and the valid choices for the active model.

The control is intentionally absent when Row-Bot does not have exact, actionable reasoning capability data for the selected model. A generic model name or a provider-wide assumption is not enough.

## What The Choices Mean

| Choice | Effect |
| --- | --- |
| Provider default | Sends no per-thread reasoning override and lets the provider or endpoint choose its normal behavior. This is the safest compatibility choice. |
| Low, Medium, High, XHigh, or another effort | Requests one of the exact effort levels supported by the selected model. The list varies by model. |
| On or Off | Enables or disables thinking only when that model exposes a true toggle. Models with mandatory reasoning do not offer Off. |
| Token budget | Sets a positive reasoning-token budget within the minimum and maximum reported for that model. This appears only for budget-capable models. |

More reasoning can increase latency and provider token usage. It can help with planning, coding, analysis, and multi-step tool use, but a higher setting is not automatically better for every request.

## Scope And Persistence

A selection is saved locally for one thread and one exact provider-qualified model. Switching models does not apply an incompatible value to the new model. If you return to a model in the same thread, Row-Bot can restore that model's valid saved choice.

Changing the global default model does not rewrite existing thread choices. Designer Studio and Developer Studio use the same thread-scoped behavior as normal Chat.

## Slash And Channel Commands

Run `/reasoning` with no argument to show the active setting and valid choices. Supported examples include:

```text
/reasoning high
/reasoning default
/reasoning on
/reasoning off
/reasoning budget 4096
```

Row-Bot validates the command against the active model. Unsupported efforts, toggles, or budgets are rejected without replacing the previous valid selection. Messaging channels use their conversation's active thread and model, so the setting remains isolated from unrelated chats.

## Providers And Custom Endpoints

Row-Bot maps native reasoning controls for supported OpenAI and Codex, Anthropic and Claude Subscription, xAI, Google, Ollama and Ollama Cloud, OpenRouter, and compatible endpoint routes when exact capability data is available. Available models and choices can change as provider catalogs change; the control itself is the authoritative list for the selected model.

For a custom OpenAI-compatible endpoint, open **Settings -> Providers**, edit or add the endpoint, then expand **Advanced -> Reasoning**. Keep **Reasoning mode** on Auto unless the endpoint's metadata is missing or wrong. Thinking budget, returned reasoning content, replay, and extra request JSON are advanced compatibility settings that apply to every model exposed by that endpoint. Enable replay only when the endpoint explicitly supports receiving preserved reasoning history.

## Responses And Compatibility Fallback

When a provider returns reasoning content, Row-Bot can stream it separately and retain it as a collapsed **Thinking** section instead of mixing it into the final answer.

If a provider rejects a valid-looking explicit reasoning choice before returning response content, Row-Bot retries once with Provider default, clears the rejected saved override, and shows a notice. Authentication failures, rate limits, timeouts, cancellations, and server failures are not silently retried as reasoning compatibility problems.

## Privacy And Safety

The selected control is stored locally with thread settings, but hosted providers receive the resulting reasoning parameter and may bill for additional tokens. Reasoning content returned by a provider can contain sensitive intermediate material. Do not enable reasoning replay for a custom endpoint unless you trust that endpoint and understand its message format.

## Troubleshooting

- **The control is missing:** confirm the active model is provider-qualified, refresh Providers and Models, and check that its catalog entry exposes exact reasoning capabilities.
- **A choice disappeared after switching models:** choices are model-specific; inspect the new model's menu or run `/reasoning`.
- **A saved choice reset to Provider default:** the provider rejected it or refreshed capability metadata no longer supports it. Read the notice, refresh the catalog, and choose from the current list.
- **A custom endpoint returns reasoning in the wrong place:** review its Advanced Reasoning settings, especially returned reasoning content and replay. Keep replay off unless required.
- **High reasoning is slow or expensive:** choose a lower supported effort, a smaller budget, or Provider default.
""",
    )

    write(
        "chat/tools-approvals-and-terminal.mdx",
        "Tools, Approvals, And Terminal",
        "Understand Row-Bot tools, approval prompts, tool results, and terminal output.",
        """
# Tools, Approvals, And Terminal

Tools are actions Row-Bot can take beyond writing text. They can search documents, read files, use a browser, run Developer commands, create designs, inspect knowledge, call MCP servers, use plugins, or send through channels.

## How Tool Use Appears

When Row-Bot uses a tool, the transcript can show a tool trace or result block. Read it as an activity receipt: what Row-Bot tried, what came back, and whether more action is needed.

## Approval Prompts

Approval prompts appear when an action can change local files, run commands, contact external systems, use accounts, start servers, send messages, call MCP/plugin tools, or do something else that deserves review.

If a child agent needs approval, Row-Bot posts a compact approval prompt in the parent thread, keeps the desktop Activity Center in sync, and routes channel-started work back to the originating channel when possible. The prompt may include a short model-written reason, but Row-Bot's approval policy still decides whether the action is blocked, allowed, or waiting for you.

Before approving, check:

- What action is being requested.
- What file, workspace, account, channel, server, or provider is involved.
- Whether the action can send data outside the app.
- Whether the proposed command or file change matches your request.
- Whether rejecting is safer until you inspect settings.

Approving lets Row-Bot continue that action. Rejecting stops that action and returns control to the conversation. On small screens and in channels, prompts stay brief; open the thread or Activity Center when you need more context.

## Terminal Output

The terminal panel shows command-style output when Row-Bot surfaces it from local tools, Developer Studio, or related runtime activity. It is a review surface, not permission by itself. Commands still follow workspace boundaries, tool availability, and approval mode.

## Common Workflow

1. Ask for a task that may need tools.
2. Let Row-Bot explain the intended action.
3. Review the approval prompt.
4. Approve only if the action matches your intent.
5. Inspect the tool trace or terminal output.
6. Ask Row-Bot to summarize what changed.

## Troubleshooting

- If no approval appears, the action may be read-only or blocked before execution.
- If a tool result is confusing, ask Row-Bot to explain the last tool call.
- If command output is missing, check whether you are in Developer Studio or a tool-capable context.
- If you rejected by mistake, ask Row-Bot to try again and review the next prompt.
""",
    )

    write(
        "home/index.mdx",
        "Home",
        "Use Home tabs as entry points for workflows, Designer, Developer, Knowledge, and Monitor.",
        """
# Home

Home is the launch area for Row-Bot's major work surfaces. It is not meant to replace the detailed guides. Use Home to see current state and start work quickly, then open the dedicated page when you need a walkthrough.

## Home Tabs

- [Workflows](/docs/home/workflows) is the entry point for saved automations.
- [Designer](/docs/home/designer) starts design projects and opens Designer Studio.
- [Developer](/docs/home/developer) connects code workspaces and opens Developer Studio.
- [Knowledge](/docs/home/knowledge) reviews local memory, documents, and graph records.
- [Monitor](/docs/home/monitor) shows logs, journals, channel state, and background activity.

## Dedicated Guides

- [Workflows guide](/docs/guides/workflows)
- [Designer Studio guide](/docs/designer/)
- [Developer Studio guide](/docs/developer/)
- [Knowledge guide](/docs/knowledge/)
- [Monitor guide](/docs/monitor/)
""",
    )

    for slug, meta in HOME_OVERVIEWS.items():
        write(
            f"home/{slug}.mdx",
            str(meta["title"]),
            str(meta["description"]),
            home_page(slug, meta),
            screenshot=True,
        )

    write(
        "guides/workflows.mdx",
        "Workflows",
        "Create, run, schedule, edit, deliver, and troubleshoot Row-Bot workflows.",
        """
# Workflows

Workflows are repeatable Row-Bot tasks. Use them for things you want to run again: a morning brief, inbox follow-up, document digest, research check, report draft, or channel delivery.

For the execution model behind schedules, persistent runs, approvals, and delivery, read [How Background Workflows Run](/docs/concepts/background-workflows).

<Screenshot id="home-workflows" alt="Row-Bot Workflows tab." caption="The Workflows tab shows workflow cards, delivery defaults, run controls, and the New Workflow entry point." />

<Screenshot id="workflow-editor" alt="Row-Bot New Workflow editor." caption="The editor brings prompts, schedules, delivery, Agent Profiles, multi-step behaviour, and approval policy into one reviewable workflow definition." />

## Where To Find Workflows

Open Home -> Workflows. The Home tab is the dashboard; this page is the full guide.

## UI Walkthrough

- **Workflow list** shows saved workflows, status, next run, and quick actions.
- **New Workflow** opens the creation flow.
- **Delivery defaults** choose where results go unless a workflow overrides them.
- **Run** starts the selected workflow manually.
- **Pause/resume** controls whether scheduled runs continue.
- **Edit** opens the saved workflow for changes.
- **Delete** removes a workflow after confirmation.
- **Status labels** show whether a workflow is ready, paused, running, waiting for approval, or needs attention.

## Basic Workflow Creation

1. Click **New Workflow**.
2. Give the workflow a clear name.
3. Describe the task Row-Bot should perform.
4. Choose when it should run: manual, scheduled, or triggered when that option is available.
5. Choose delivery: app only, a channel, or another configured destination.
6. Save the workflow.
7. Run it manually once and review the result.

## Advanced Workflow Creation

Advanced workflows can include richer instructions, required inputs, model/profile choices, delivery overrides, approval rules, and multi-step behavior. Keep each step observable: what information Row-Bot should gather, what it should produce, where it should save or send output, and what requires your approval.

## What Is Saved

Workflow definitions, schedules, run history, delivery defaults, and status are local Row-Bot data. Results may be sent to channels only when configured and selected.

## Safety

Avoid workflows that send messages, write files, start servers, or call external systems without a review point. Use approvals for actions that can affect files, accounts, channels, MCP servers, plugins, or external services.

## Troubleshooting

- If a workflow does not run, check whether it is paused.
- If delivery fails, check the selected channel and its Settings tab.
- If a run waits for approval, decide from the thread prompt or open the Activity Center.
- If results are too broad, split the workflow into smaller steps.
""",
        screenshot=True,
    )

    write(
        "designer/index.mdx",
        "Designer Studio",
        "Create and iterate on design projects, pages, assets, previews, brands, and exports.",
        """
# Designer Studio

Designer Studio helps you create visual work with Row-Bot: pages, slides, mockups, landing-page drafts, branded assets, charts, image or video inserts, and exportable design projects.

<Screenshot id="home-designer" alt="Row-Bot Designer entry point." caption="The Designer Home tab starts projects and opens the full Designer Studio workflow." />

<Screenshot id="designer-editor" alt="Row-Bot Designer Studio editor with a fictional workshop presentation." caption="The full Designer editor combines project chat, page navigation, the live preview, brand and asset controls, and export entry points." />

## Where To Find It

Open Home -> Designer to start or reopen a project. The Home tab is the launcher. Designer Studio is the full workspace for editing, previewing, reviewing, and exporting.

## End-To-End Workflow

1. Start from a prompt, template, goal, or existing project.
2. Describe the audience, tone, length, and outcome.
3. Add reference files or brand details if they matter.
4. Create the project and review the first draft.
5. Use the editor and preview to inspect each page.
6. Ask Row-Bot to revise text, layout, media, charts, or brand details.
7. Review quality, export, or share a preview when ready.

## UI Walkthrough

- **Project gallery** lists recent saved projects.
- **New design flow** collects the goal, template, audience, tone, references, and brand.
- **Editor/canvas** shows the current page or screen.
- **Page navigator** moves through pages and lets you add, delete, reorder, or rename where available.
- **Control panels** expose brand, layout, import, export, share, review, and history actions.
- **Preview/output** lets you inspect the user-facing result before exporting.
- **Save/export** writes project state locally and creates files in the selected format.

## Brand And Assets

Designer can use saved brand presets, extract brand hints from a URL, accept reference uploads, and reuse project assets. Generated images or videos may call configured media providers; local-only design edits stay in local project data.

## Troubleshooting

- If a project starts empty, provide a more specific goal and audience.
- If exports look wrong, preview each page before exporting.
- If media generation is unavailable, check Providers and model capabilities.
- If a brand extraction fails, enter colors, fonts, or notes manually.
""",
        screenshot=True,
    )

    write(
        "developer/index.mdx",
        "Developer Studio",
        "Use Row-Bot for folders, repositories, code chat, inspectors, commands, changes, and sandbox modes.",
        """
# Developer Studio

Developer Studio is Row-Bot's code workspace. Use it when you want Row-Bot to understand a local folder or Git repository, discuss code, inspect files, run approved commands, propose edits, and help review changes.

<Screenshot id="home-developer" alt="Row-Bot Developer entry point." caption="The Developer Home tab opens folders, connects repositories, clones projects, and starts Developer Studio workspaces." />

<Screenshot id="developer-workspace" alt="Row-Bot Developer Studio with an isolated demonstration repository." caption="The Developer workspace keeps repository identity, changed files, inspector state, conversation, commands, and review controls together." />

## Where To Find It

Open Home -> Developer. Choose an existing folder, connect a repository already on your machine, or clone a repository into a local workspace.

## End-To-End Workflow

1. Open or clone a project.
2. Confirm the workspace name, path, branch, and dirty state.
3. Ask Developer chat a code question or assign a change.
4. Review the inspector for files, detected commands, todos, context, and approvals.
5. Approve only the commands or edits you understand.
6. Run tests or checks.
7. Review the changed files before committing or exporting a patch.

## UI Walkthrough

- **Open folder** connects a local project without cloning.
- **Connect repository** uses a local Git checkout.
- **Clone repository** creates a new checkout from a remote URL.
- **Developer chat** is the conversation bound to that workspace.
- **Inspector** shows workspace identity, files, command suggestions, todos, changes, and run state.
- **File/context panel** helps Row-Bot and you see what code is relevant.
- **Command controls** run detected or requested commands through approval policy.
- **Change review** summarizes edits and lets you inspect before accepting next steps.

## Sandbox Modes

Local mode lets Row-Bot operate in the selected workspace with your configured file and command permissions. Docker Sandbox mode, when available on a host installation, isolates command execution in a container and requires an import step before changes affect the real workspace. It requires a supported host Docker runtime and can differ from your local environment.

Inside the official Row-Bot application container, Developer Docker Sandbox is unavailable and a requested Docker workspace fails closed; Row-Bot never probes a nested daemon or silently runs that workspace locally. Local mode remains an explicit choice and can see only workspace paths deliberately mounted into the application container.

An approved risky Custom Tool is a third case: it deliberately executes in Local mode inside the application container against the selected visible Custom Tool path. That behavior is shown in the approval dialog and is not a nested Docker sandbox or fallback from a requested Docker workspace. See [Docker And VPS Operations](/docs/operations/docker#developer-and-headless-boundaries) for the complete container boundary.

For a host installation, start with Local mode on a disposable branch. Use Docker Sandbox when you want stronger isolation and the supported host runtime is available.

## Troubleshooting

- If Developer tools are unavailable, open a Developer workspace first.
- If commands are missing, inspect detected commands or ask Row-Bot to identify the project tooling.
- If a command asks for approval, read the exact command and workspace before approving.
- If Docker sandbox import is offered, review the patch before importing it into the real project.
""",
        screenshot=True,
    )

    write(
        "knowledge/index.mdx",
        "Knowledge",
        "Review and manage Row-Bot memory, documents, knowledge graph records, filters, details, and dream cycle activity.",
        """
# Knowledge

Knowledge is Row-Bot's local memory and retrieval area. It includes saved memories, extracted document information, graph records, entity relationships, and background organization that can help future chats.

For the relationship between thread history, memory, documents, the graph, Wiki Vault, and Dream Cycle, read [How Memory Becomes Knowledge](/docs/concepts/memory-knowledge-and-dream-cycle).

<Screenshot id="home-knowledge" alt="Row-Bot Knowledge tab." caption="The Knowledge tab reviews local memory and graph records with filters and detail surfaces." />

<Screenshot id="settings-knowledge" alt="Row-Bot Knowledge settings." caption="Knowledge settings control memory, embeddings, graph maintenance, document knowledge, and Wiki vault output." />

## Where To Find It

Open Home -> Knowledge. Configure memory and embeddings in Settings -> Knowledge and Settings -> Documents.

## UI Walkthrough

- **List or grid** shows available knowledge records.
- **Filters** narrow records by type, source, status, or search text.
- **Detail cards** show the exact record, source, confidence, timestamps, and related information when available.
- **Edit mode** lets you correct useful information instead of deleting everything around it.
- **Dream cycle controls** review or trigger background organization when enabled.
- **Extraction journal links** connect Knowledge to Monitor so you can see how records were created.

## Common Workflow

1. Open Knowledge after a few useful chats or document imports.
2. Filter to the topic you care about.
3. Open a detail card.
4. Correct or remove misleading records.
5. Ask Chat to use knowledge only after you have verified important entries.

## What Is Saved

Knowledge records are local data. They can be used as context in future chats when memory, document search, or knowledge retrieval is enabled.

## Troubleshooting

- If Knowledge is empty, enable memory or index documents first.
- If records are wrong, edit or remove them.
- If retrieval feels noisy, reduce memory/search settings and review graph entries.

Continue with [Wiki Vault](/docs/knowledge/wiki-vault) and [Provenance And Repair](/docs/knowledge/provenance-repair) for the file-output and recovery workflows.
""",
        screenshot=True,
    )

    write(
        "monitor/index.mdx",
        "Monitor",
        "Inspect Row-Bot logs, journals, workflow state, channels, and background activity.",
        """
# Monitor

Monitor shows what Row-Bot is doing and what recently happened. Use it when a workflow, channel, background job, knowledge extraction, dream cycle, or provider setup needs investigation.

<Screenshot id="home-monitor" alt="Row-Bot Monitor tab." caption="The Monitor tab shows recent logs, journals, channel state, and background activity controls." />

## Where To Find It

Open Home -> Monitor.

## UI Walkthrough

- **System Monitor** is the top-level status area.
- **Refresh** updates visible logs and state.
- **Knowledge Extraction** shows extraction activity and links to the extraction journal.
- **Dream Cycle** shows background organization activity and links to the dream journal.
- **Channels** shows whether configured external message channels are running.
- **Recent Logs** shows current app events with timestamps and severity.
- **View Full Log** opens a longer log view for troubleshooting.
- **Status labels** identify running, idle, disabled, warning, or failed states.

## Logs And Journals

Recent logs help with immediate diagnosis. The knowledge extraction journal explains what Row-Bot extracted from conversations or documents. The dream journal explains background organization attempts. Use journals when you need a narrative of why a knowledge record or background status exists.

## Common Workflow

1. Reproduce or observe the issue.
2. Open Monitor and refresh.
3. Check status labels and recent logs.
4. Open the relevant journal.
5. Copy only non-sensitive details into a support request.

## What Is Saved

Logs and journals are local Row-Bot data. They may include file names, thread names, provider names, channel names, or task summaries, so review them before sharing.

## Troubleshooting

- If logs are empty, reproduce the issue and refresh.
- If background work is disabled, check Settings -> Preferences and Settings -> Knowledge.
- If a channel is stopped, open Settings -> Channels before restarting it.
""",
        screenshot=True,
    )

    write(
        "settings/index.mdx",
        "Settings",
        "Understand every Row-Bot Settings tab and what each configuration area changes.",
        """
# Settings

Settings is Row-Bot's configuration center. It affects model providers, model choices, documents, search, skills, system access, accounts, utilities, tracker, knowledge, Buddy, voice, channels, MCP, plugins, and preferences.

Authenticated owner sessions receive this complete Settings experience in both desktop and compact presentation. On phones and tablets, the layout adapts to the viewport; the available settings and authority do not change.

## How Settings Is Organized

- **Providers** connects local, hosted, subscription, and custom model providers.
- **Models** chooses defaults and pinned Quick Choices.
- **Documents** manages uploads, extraction, embeddings, and vector rebuilds.
- **Tools** controls progressive external capability loading, retrieval compression, and search and knowledge tools.
- **Skills** manages Smart Skills and Skills Hub access.
- **System** configures local access, workspace boundaries, window behavior, tunnels, logs, and diagnostics.
- **Accounts** connects account-level integrations.
- **Utilities** manages built-in helper tools.
- **Tracker** configures structured personal logs.
- **Knowledge** manages memory, graph, embeddings, and wiki export.
- **Buddy** controls the visual companion.
- **Voice** configures Talk, Dictate, read-aloud, models, devices, and diagnostics.
- **Channels** configures external messaging connectors.
- **MCP** manages external MCP tool servers.
- **Plugins** manages installed plugins and Custom Tool promotion.
- **Preferences** changes identity, launch behavior, background intelligence, updates, and migration.

## A Good Setup Order

1. Providers
2. Models
3. Documents and Tools
4. Skills
5. System access
6. Integrations: Accounts, Channels, MCP, Plugins
7. Voice and Buddy
8. Preferences

## Safety Notes

Credentials belong in Providers, Accounts, Channels, MCP, or plugin-specific settings, not in chat messages. Enable only the tools and integrations you intend to use. Review approvals before actions that write files, run commands, use accounts, contact external services, or send messages.
""",
    )

    for slug, meta in SETTINGS.items():
        write(
            f"settings/{slug}.mdx",
            str(meta["title"]),
            str(meta["desc"]),
            settings_page(slug, meta),
            screenshot=True,
        )

    write(
        "integrations/channels.mdx",
        "Channels",
        "Connect Row-Bot to messaging channels, delivery defaults, pairing, tunnels, and safe external behavior.",
        """
# Channels

Channels connect Row-Bot to external messaging platforms such as Telegram, WhatsApp, Discord, Slack, and SMS-style providers when configured. Use channels when you want to message Row-Bot outside the desktop app or send workflow results somewhere specific.

<Screenshot id="settings-channels" alt="Row-Bot Channels settings." caption="Channels settings show connector configuration, credentials, tunnel use, pairing, and start/stop controls." />

## Setup Workflow

1. Open Settings -> Channels.
2. Expand the channel you want.
3. Add the required token, URL, phone/account detail, or provider-specific field.
4. Configure a tunnel in Settings -> System if the channel requires an inbound webhook.
5. Save the channel.
6. Start the channel.
7. Pair or approve users before trusting private messages.
8. Test with a low-risk message.

## Controls

- **Save** stores channel settings.
- **Start** begins the channel runtime.
- **Stop** shuts the channel runtime down.
- **Tunnel controls** connect a channel to a public webhook URL when needed.
- **DM Pairing Code** approves a user before private-message access.
- **Paired Users** lists approved users and lets you revoke access.
- **Setup Guide** explains platform-specific prerequisites.
- **`/reasoning`** shows or changes the reasoning choice for that channel conversation's active model. Run it without an argument to see the exact valid choices.

## Safety

Messages sent through a channel leave the local app. Do not enable a channel until you understand who can message it, what Row-Bot can send back, and whether workflows may deliver results there.

When work starts from a channel and a child agent asks for approval, Row-Bot sends the approval back to that parent channel conversation when the channel supports approval messages. The same approval remains visible in the desktop parent thread and Activity Center.

## Troubleshooting

- If Telegram encounters one transient network failure during initialization, Row-Bot cleans up the partial runtime and retries once. Persistent network failures remain stopped with an actionable diagnostic; invalid bot tokens are not retried.
- If Telegram starts but its command menu cannot be registered, polling remains available and the menu failure is logged as non-fatal.
- If a channel cannot start, check required fields and tunnel state. Automatic tunnel startup runs outside the UI event loop, so a slow tunnel helper should report status without freezing app startup.
- If inbound messages fail, verify webhook URLs and platform permissions.
- If the wrong person has access, revoke them from Paired Users.
""",
        screenshot=True,
    )

    write(
        "integrations/mcp.mdx",
        "MCP",
        "Configure external MCP servers, test tool availability, and manage MCP safety.",
        """
# MCP

MCP lets Row-Bot use tools exposed by external Model Context Protocol servers. An MCP server can be local or remote, simple or powerful. Treat it like an extension with its own access and trust boundary.

<Screenshot id="settings-mcp" alt="Row-Bot MCP settings." caption="MCP settings manage global enablement, server rows, add/import/browse actions, tests, diagnostics, and per-server controls." />

<Screenshot id="mcp-marketplace" alt="Row-Bot Browse MCP Servers marketplace with bundled starter servers." caption="Browse starts with the bundled starter catalogue and exposes publisher, transport, overlap, authentication, and risk labels before import." />

<Screenshot id="mcp-add-server" alt="Row-Bot Add MCP Server dialog." caption="Manual server configuration starts disabled so you can review transport, command or URL, environment, and tool risk before testing." />

## Setup Workflow

1. Open Settings -> MCP.
2. Leave Enable MCP off until a trusted server is configured.
3. Add, import, or browse for a server.
4. Review the command, arguments, URL, transport, and expected tools.
5. Save disabled first if you are unsure.
6. Test the server.
7. Enable the server only after you understand what it can access.

## Controls

- **Enable MCP** turns all external MCP tools on or off.
- **Add Server** opens the manual server form.
- **Import Config** imports server definitions from configuration text.
- **Browse MCP Servers** searches available server directories.
- **Diagnostics** helps explain connection failures.
- **Per-server enablement** decides whether a saved server contributes tools.
- **Test, refresh, edit, delete** manage the server row.

## Safety

MCP tools can read, write, call APIs, or automate services depending on the server. Row-Bot approvals still matter, but you should also trust the server itself before enabling it.

## Troubleshooting

- If a server fails to test, check transport, command, arguments, URL, and dependencies.
- If tools are missing, enable both the global MCP switch and the server.
- If a prompt asks for unexpected access, reject it and inspect the server configuration.
""",
        screenshot=True,
    )

    write(
        "integrations/plugins.mdx",
        "Plugins",
        "Install, enable, configure, update, remove, and troubleshoot Row-Bot plugins.",
        """
# Plugins

Plugins add optional capabilities to Row-Bot: tools, skills, apps, settings, and integration surfaces. Install them only from sources you trust, because they can run code inside the local app environment.

<Screenshot id="settings-plugins" alt="Row-Bot Plugins settings." caption="Plugins settings list installed plugins, marketplace entry points, configuration, enablement, and Custom Tool promotion." />

## Setup Workflow

1. Open Settings -> Plugins.
2. Review installed plugins and marketplace options.
3. Read the plugin description and requested configuration.
4. Install or enable the plugin.
5. Configure required fields.
6. Test with a low-risk prompt.
7. Disable, update, or remove the plugin when needed.

## Controls

- **Installed plugin cards** show version and status.
- **Enable/disable** controls whether plugin capabilities are active.
- **Configure** opens plugin-specific settings.
- **Update/remove** manage the local installation.
- **Marketplace** opens plugin discovery and install previews.
- **Custom Tools** can be promoted after review so they behave like reusable app capabilities.

## Safety

Plugins can add tools that read files, call services, or create outputs. Keep approval mode active for risky actions and disable plugins you do not actively use.

## Troubleshooting

- If a plugin does not load, inspect its manifest and dependency message.
- If plugin tools are missing, confirm the plugin is enabled.
- If a plugin causes errors, disable it, restart Row-Bot if needed, then review configuration.
""",
        screenshot=True,
    )

    write(
        "skills/index.mdx",
        "Skills Hub",
        "Browse, enable, pin, create, edit, and troubleshoot Row-Bot skills.",
        """
# Skills Hub

Skills are instruction packs that teach Row-Bot how to approach a category of work. Skills Hub is where you browse installed skills, inspect details, enable or disable them, pin useful ones, and create or edit local skills.

<Screenshot id="settings-skills" alt="Row-Bot Skills settings and Skills Hub entry points." caption="Skills settings expose installed skills, browse/search entry points, enablement, and pinning controls." />

<Screenshot id="skills-hub" alt="Row-Bot Browse Public Skills marketplace with two fictional skills." caption="Browse Public Skills shows source and review status before a skill is brought into the local library." />

## UI Walkthrough

- **Browse/search** finds bundled, installed, local, and external-source skills.
- **Filters** narrow by source, installed state, or task type.
- **Skill detail** explains purpose, instructions, source, and status.
- **Enable/disable** decides whether Row-Bot may use the skill.
- **Pin** keeps an important skill easy to reach.
- **Create/edit** lets you maintain a local skill for your own workflow.

## Creating A Skill

1. Decide what task the skill should help with.
2. Write concise instructions, examples, and boundaries.
3. Include when the skill should and should not be used.
4. Save it locally.
5. Enable it and test with a small prompt.
6. Revise if Row-Bot overuses or misunderstands it.

## What Skills Change

Skills shape Row-Bot's behavior; they do not automatically grant credentials or bypass approvals. A skill can make Row-Bot more consistent for a task, but tools still need to be enabled and approved where appropriate.

## Troubleshooting

- If a skill is not used, make your prompt match its purpose and confirm it is enabled.
- If a skill is too aggressive, disable or rewrite it with clearer boundaries.
- If an external skill is unfamiliar, inspect it before enabling.
""",
        screenshot=True,
    )

    write(
        "voice-and-buddy/index.mdx",
        "Voice And Buddy",
        "Configure Dictate, Talk, local voice, realtime voice, read-aloud, devices, and Buddy.",
        """
# Voice And Buddy

Voice and Buddy make Row-Bot feel less like a text box and more like a desktop companion. Voice handles speech input and optional spoken output. Buddy is the visual companion that reflects app state and can live in the sidebar, workspace, or desktop overlay.

<Screenshot id="settings-voice" alt="Row-Bot Voice settings." caption="Voice settings control Dictate, Talk, read-aloud, voice models, devices, and diagnostics." />

## Voice Modes

- **Dictate** turns speech into composer text so you can review it before sending.
- **Talk** submits spoken input more directly for conversation.
- **Read-aloud** speaks assistant responses when enabled.
- **Local voice** uses local speech components where available.
- **Realtime voice** uses a compatible provider for lower-latency spoken conversation.

## Choosing A Mode

Use Dictate when accuracy and review matter. Use Talk when you want a faster hands-light conversation. Use local voice when privacy and offline-style behavior matter. Use realtime voice when latency matters and you accept internet access, provider requirements, and provider cost.

## Devices And Diagnostics

Select the microphone and output device in Settings -> Voice. Run diagnostics when audio is silent, delayed, or routed to the wrong device.

<Screenshot id="settings-buddy" alt="Row-Bot Buddy settings." caption="Buddy settings control companion visibility, overlay, personality, look, motion, and optional custom-look generation." />

## Buddy Controls

- **Enable switches** choose sidebar, workspace, or desktop overlay visibility.
- **Open and close overlay** manage the separate desktop companion window.
- **Companion personality** changes the tone of Buddy cues.
- **Bubble style** changes status presentation.
- **Look cards** choose bundled or custom appearances.
- **Generate full Buddy**, **Retry motion**, and **Use still only** manage custom looks.

## Privacy And Safety

Voice input can become chat text. Realtime voice and provider-backed speech may send audio or transcript data to the selected provider. Buddy preferences and assets are local, but custom generation may call a configured media provider.

## Troubleshooting

- If Dictate records nothing, check microphone selection and permissions.
- If Talk sends too quickly, use Dictate instead.
- If realtime voice is unavailable, check Providers and Voice Models.
- If Buddy does not appear, check Buddy enable switches and overlay state.
""",
        screenshot=True,
    )

    write(
        "privacy-safety/index.mdx",
        "Privacy And Safety",
        "Understand local data, provider calls, credentials, approvals, channels, MCP, plugins, and sharing.",
        """
# Privacy And Safety

Row-Bot is local-first: the app and its data live on your machine. Local-first does not mean every feature is offline. Hosted models, web search, browser actions, account tools, channels, MCP servers, plugins, realtime voice, and media providers can send data outside the app when you configure and use them.

## Local Data

Conversations, memories, documents, workflows, logs, Designer projects, Developer workspaces, skills, plugins, Buddy assets, and settings are stored in the active local Row-Bot data directory.

## External Calls

External calls happen when you choose or enable something that needs them: hosted models, subscription providers, API providers, web search, browser automation, account tools, messaging channels, MCP servers, plugin tools, realtime voice, and media generation.

Computer Use is a distinct opt-in boundary. Row-Bot downloads the pinned Cua Driver only after an explicit Install or Repair action, verifies the selected archive, and requires a telemetry disclosure before any executable invocation. The reviewed upstream telemetry includes installation and platform metadata but excludes typed content, screenshots, prompts, files, memories, tool arguments, secrets, and channel content. See [Computer Use](/docs/computer-use/) for the full boundary.

## Credentials

Enter credentials only in the relevant Settings tab or provider sign-in flow. Row-Bot stores secrets in the operating system key store when available and keeps local metadata for status and diagnostics. An explicitly configured server deployment can use a read-only external master-key file to encrypt owner-entered secrets in its persistent data directory; see [Docker And VPS Operations](/docs/operations/docker#read-only-secret-files).

## Remote Access

Remote Access is single-owner and multi-device. A Tailscale, LAN, SSH, Docker, or reverse-proxy route provides reachability only; each non-local browser still needs a one-time invitation and a revocable Row-Bot session.

Every authenticated interactive browser has full owner authority. Phone/tablet and computer choices select compact or desktop presentation only; device metadata and screen size are not permission boundaries. Row-Bot does not provide guest or multi-user sharing.

Invitation links expire after 10 minutes and work once, but should still be treated like passwords until they expire. Row-Bot stores only hashed invitation and session secrets. Session credentials use HttpOnly cookies and are not placed in URLs.

Prefer Tailscale or HTTPS for remote access. Plain LAN HTTP is unencrypted and does not support a remote browser microphone. For an operator-managed proxy, allow only the canonical browser-facing origin and trust only the exact address that connects from the proxy to Row-Bot. Never resolve a forwarding-header error by trusting every private address or a whole container network.

See [Remote Access And Server Mode](/docs/operations/remote-access) for layouts, lifetimes, revocation, Tailscale ownership, server mode, Docker, and recovery.

## Approvals

Use approvals to review file writes, command execution, browser actions, account actions, channel sends, MCP calls, plugin tools, Developer changes, and other sensitive actions. Reject anything that does not match your request.

Approval mode is policy, not a model choice: blocked actions stay blocked, ask-mode actions wait for you, and auto-approved actions follow the configured policy. Some prompts include a short model-written reason for readability, but the underlying approval gate and action details are system-controlled.

## Sharing Logs Or Screenshots

Before sharing logs, screenshots, documents, thread exports, or review packages, check for names, file paths, account names, message contents, tokens, private documents, or misleading real data.

## Safer Defaults

- Start with local models when privacy matters most.
- Keep channels, MCP, and plugins disabled until needed.
- Keep Computer Use disabled until a local interactive task needs native application control.
- Keep Remote Access off until another trusted device needs it; use the narrowest route and the appropriate layout.
- Use narrow workspaces for file tools.
- Review approvals before external or destructive actions.
- Keep a final human review step before publishing screenshots or docs built from a personal app state.
""",
    )

    write(
        "profiles-goals-agents/index.mdx",
        "Profiles, Goals, And Agents",
        "Choose reusable Agent Profiles, run bounded goals, and review delegated parent and child agents.",
        """
# Profiles, Goals, And Agents

These three features work together but solve different problems. An **Agent Profile** is a reusable role and policy. A **goal** is a bounded objective for the current task. An **agent run** is one execution of delegated work, with its own status and evidence.

For the mental model behind delegation and hand-offs, read [How Profiles, Goals, And Agents Work](/docs/concepts/profiles-goals-and-agents).

<Screenshot id="agent-profile-library" alt="Row-Bot Agent Profile library with fictional Research Guide and Project Coordinator profiles." caption="Agent Profiles package reusable instructions, skills, tools, model policy, and approval policy without changing the underlying thread history." />

## Choose Or Create A Profile

1. Open a chat and expand **Agent profiles** in the left sidebar.
2. Choose a built-in profile or create one for a repeated role.
3. Keep the role narrow: describe the outcome, sources it may use, and what it must not do.
4. Review its model, skill, tool, delegation, and approval policy.
5. Save it, then select it before asking for work.

Profiles do not grant capabilities by themselves. A selected tool must still be enabled, available, and allowed by the active approval policy. A profile that can delegate may start child agents, but each run remains visible in the Activity Center and the chat agent strip.

New runs also receive a checkpointed work budget. The Models settings tab shows the recommended application-wide work-round, nesting, concurrency, and optional child active-time limits. Extra children wait in a first-in, first-out queue when capacity is full; changing a setting affects new runs, not work already in progress.

<Screenshot id="goal-and-agents" alt="Row-Bot active goal and delegated agents for a fictional launch checklist." caption="The goal strip records bounded progress while parent and child agent rows show who is running, complete, waiting, or stopped." />

## Run A Goal

A goal is useful when work needs more than one turn. State the objective and an observable finish condition. Row-Bot records progress, blockers, evidence, turns, and status so it can continue without pretending that partial work is complete.

- **Active** means work can continue.
- **Complete** means the objective and required checks are genuinely finished.
- **Blocked** is for a repeated impasse that requires your input or an external change.
- **Stop** ends the active run; it does not delete the thread or its evidence.

## Review Delegated Work

Parent agents coordinate. Child agents handle bounded subtasks. Open an agent row to review its prompt, profile, model, status, result, evidence, and thread. A completed child result is evidence for the parent, not automatic permission to write files, send messages, commit code, or publish.

If a run appears stuck, inspect its last update before stopping it. If several agents edit the same resource, narrow their ownership or run them sequentially. Keep consequential final actions with the parent and a human approval point.

Repeated model-and-tool states are detected before a run can loop indefinitely. The fourth identical no-progress state is blocked; a fifth ends the run cleanly with a durable reason. Reaching a configured work limit also finalizes the run instead of leaving it marked as active.

## What Is Saved

Profiles, goals, agent runs, edges, progress, results, and evidence are stored in the active local data directory. Provider prompts still follow the privacy terms of the model route selected for each run.
""",
        screenshot=True,
    )

    write(
        "knowledge/wiki-vault.mdx",
        "Wiki Vault",
        "Generate and maintain a local Markdown vault from reviewed Row-Bot knowledge.",
        """
# Wiki Vault

Wiki Vault turns selected Knowledge entities and relationships into linked Markdown pages in a folder you control. It is an output of the local knowledge graph, not a replacement for the underlying memory and provenance records.

## Configure The Vault

1. Open **Settings -> Knowledge**.
2. Choose a vault folder that is separate from application code and important personal notes while you test.
3. Enable Wiki output only after reviewing the current Knowledge graph.
4. Run the rebuild or sync action shown in the tab.
5. Open the generated Markdown pages in your preferred editor and check links, titles, and source notes.

The vault contains generated summaries and links. Correct important facts in Knowledge first, then rebuild, so the graph remains the source of truth. Avoid hand-editing generated sections unless the page clearly identifies an area intended for manual notes.

## Updates And Deletions

New or changed entities can update their corresponding pages. Removed or merged records may cause generated pages or links to change on the next rebuild. Back up a vault before a large repair, migration, or rebuild if it also contains your own notes.

## Privacy

The vault is local, but it is deliberately easy to open in other software or sync with a third-party folder. Treat the destination as an export boundary. Review it before placing it in cloud storage, a shared repository, or a publishing system.

## Troubleshooting

- If pages are empty, verify that Knowledge contains reviewed entities and relations.
- If links duplicate, run Knowledge repair before rebuilding the vault.
- If a path is unavailable, choose a writable local folder and try again.
- If private material appears, correct or remove its source record, rebuild, and review the destination before sharing.
""",
    )

    write(
        "knowledge/provenance-repair.mdx",
        "Knowledge Provenance And Repair",
        "Trace where knowledge came from, correct records, repair relations, and rebuild derived outputs safely.",
        """
# Knowledge Provenance And Repair

Knowledge is useful only when you can tell where it came from. Review provenance before relying on a memory, document fact, entity, or relation for consequential work.

## Review A Record

1. Open **Home -> Knowledge** and filter to the topic.
2. Open the record detail.
3. Check its source, timestamps, type, confidence or status, and related entities.
4. Compare important claims with the original conversation or document.
5. Correct, merge, or remove the record if it is misleading.

## Repair Order

Use the smallest repair that fixes the problem:

1. Correct a title, type, description, or relation.
2. Merge a duplicate entity when both records refer to the same thing.
3. Remove a bad relation without deleting sound entities.
4. Rebuild derived indexes if search still returns stale results.
5. Rebuild Wiki Vault output only after the graph is correct.

Monitor's extraction and Dream Cycle journals help explain background changes. A repair should preserve useful source evidence and avoid touching unrelated memories.

## Recovery

Before a large repair or migration, close Row-Bot and back up the active data directory. Restore the whole related data set together rather than mixing database files from different moments. If the app offers a built-in repair action, read its scope and completion message before manually replacing files.

## Safety Boundary

Deleting a thread does not necessarily mean every derived record disappears immediately, and deleting one graph entity can affect linked output. Review the exact record and its relationships first. Never run repair tests against a personal data directory.
""",
    )

    write(
        "computer-use/index.mdx",
        "Computer Use",
        "Set up and safely use Row-Bot's opt-in native Windows and macOS application control.",
        """
# Computer Use

Computer Use is Row-Bot's provider-neutral tool for operating native Windows and macOS applications. It is separate from browser automation: use Browser for web pages and Computer Use only when the task must interact with a desktop application window.

Computer Use is a beta feature. It is off by default, supports one interactive local task at a time, and is unavailable to schedules, channels, background workflows, child agents, plugins, external MCP callers, mobile clients, and headless or server sessions.

## Set It Up

1. Open **Settings -> System -> Browser & Computer Use**.
2. Expand **Computer Use (Beta)** and read the capability and privacy summary.
3. Choose **Install**. Row-Bot shows the required Cua Driver telemetry disclosure before it downloads or starts anything.
4. Choose **Continue** only if you accept the disclosure. Row-Bot downloads the pinned Cua Driver 0.7.1 asset for your platform, verifies its SHA-256, and extracts it into Row-Bot's private data directory.
5. On macOS, grant Accessibility and Screen Recording to the Row-Bot process when prompted, then choose **Recheck**. Restart the same process after changing permissions if the status asks you to.
6. Turn on **Computer Use (Beta)** after the setup card reports ready.

Install, Repair, Reinstall, Remove, and system-binary controls affect only the optional driver. Row-Bot never runs the upstream installer or updater. A custom system binary is accepted only after explicit opt-in and version verification.

## Run A Native Task

Ask from a normal local desktop chat and name the application and desired outcome. Row-Bot discovers applications and windows, acquires one exclusive task lease, and binds actions to the selected target window. It can launch an allowlisted application, observe that window, click, type, press keys, scroll, and drag when the current policy permits the operation.

When accessibility data is insufficient, Row-Bot may send one ephemeral target-window screenshot to the Vision model configured in Settings. The live control card shows sanitized state and a shielded thumbnail; screenshot bytes are not written to chat, durable memory, replay history, logs, or tool results.

## Stop Or Take Over

- **Stop** cancels queued work and releases the session.
- **Take over** cancels queued mutation and pauses the lease so you can interact directly.
- **Resume** requires Row-Bot to observe the target again before it can act.

Window replacement, permission loss, target changes, or driver failure invalidates stale state. Row-Bot must reacquire and observe before another mutation.

## Safety Boundaries

Computer Use blocks terminals, password managers, Row-Bot itself, secure desktops, elevation prompts, security settings, and attempts to handle credentials, one-time codes, CAPTCHAs, biometrics, or operating-system permission dialogs. Consequential actions use approval policy at the point of risk, and external handoff flows stay with the user.

The reviewed Cua telemetry sends a stable random Cua installation ID, Cua version, operating-system name and version, architecture, CI flag, event category, and timestamp to Cua's PostHog endpoint. It excludes usernames, file paths, command or tool arguments, typed content, and screenshots. Row-Bot adds no first-party telemetry and prevents its prompts, files, memories, secrets, screenshots, tool arguments, and channel content from reaching Cua telemetry.

For the exact allowlist and reviewed dependency record, see [Computer Use Beta: architecture and security decision](https://github.com/siddsachar/row-bot/blob/main/docs/COMPUTER_USE_SECURITY.md).

## Troubleshooting

- If setup reports an archive or checksum error, choose **Repair** and retry on a trusted network.
- If macOS reports missing permission, use the provided buttons to open Accessibility and Screen Recording settings, grant the running Row-Bot process, restart it, and recheck.
- If the requested app is blocked, do not work around the policy with a terminal or generic MCP tool. Operate it manually or use a narrower supported application.
- If a session is busy, stop or finish the active Computer Use task before starting another.
- If the target window closes or changes, ask Row-Bot to reacquire it before resuming.
""",
    )

    write(
        "mobile-native/index.mdx",
        "Android And Native Desktop",
        "Use the full Row-Bot owner product from Android in a compact layout and understand native-only desktop behaviour.",
        """
# Android And Native Desktop

Row-Bot's mobile layout gives an authenticated Android browser the full owner product in a compact presentation. Chat, Activity, workflows, Knowledge, and complete phone-safe Settings remain available. The native desktop build adds operating-system integration such as window, tray, microphone, file-picker, and updater behaviour. Layout never changes Row-Bot authority or the approval rules of the underlying task.

<Screenshot id="mobile-chat-list" alt="Android-sized Row-Bot chat list with fictional conversations." caption="Chat starts with recent local threads and a New thread action." />

<Screenshot id="mobile-chat-detail" alt="Android-sized Row-Bot chat detail for a fictional launch checklist." caption="The compact thread view keeps transcript, model context, attachments, approvals, and composer controls within the phone layout." />

## Pair An Android Device

1. On an authorized owner device, open **Settings → System → Remote Access**.
2. Check Tailscale or explicitly enable the narrowest connection route that fits your network.
3. Select **Invite a device**, then choose **Phone or tablet — Compact layout**.
4. Choose a trusted 30-day or temporary 12-hour session and a currently reachable connection route.
5. Create the invitation and open the shown link or QR code on Android.
6. Review the connection page on Android and press **Connect** before the invitation expires.
7. Revoke the device or an individual session from Remote Access settings when it is lost, replaced, or no longer trusted.

<Screenshot id="remote-access-invitation" alt="Remote Access invitation configured for a phone or tablet." caption="Phone and computer choices select presentation only. Every connected browser receives full owner access." />

The invitation expires after 10 minutes and works once. Opening it does not consume it until the recipient presses **Connect**. The resulting owner session is a separate HttpOnly browser credential and never appears in the URL.

Pairing grants access to your local Row-Bot instance. Do not share an invitation, expose the local server broadly, or use remote LAN HTTP on an untrusted network. Tailscale or an operator-managed HTTPS origin is preferred and is required for browser microphone capture away from localhost. See [Remote Access And Server Mode](/docs/operations/remote-access) for the complete connection and recovery model.

<Screenshot id="mobile-activity" alt="Android-sized Row-Bot Activity view." caption="Activity keeps goals, delegated agents, approvals, and attention states available away from the desktop layout." />

<Screenshot id="mobile-workflows" alt="Android-sized Row-Bot Workflows view." caption="The mobile workflow view can inspect and run saved automations while preserving delivery and approval policy." />

<Screenshot id="mobile-knowledge" alt="Android-sized Row-Bot Knowledge view." caption="Knowledge provides a compact entry point to local memory and graph review." />

<Screenshot id="mobile-settings" alt="Android-sized Row-Bot Providers settings." caption="Mobile settings use the same provider categories and credential boundaries as desktop." />

Mobile Settings includes Providers, Models, Knowledge, Buddy, Voice, System and Remote Access, Tracker, Documents, Tools, Skills, Accounts, Channels, Utilities, MCP, Plugins, and Preferences. Rich Developer Studio and Designer Studio editors remain desktop-layout-oriented and show an explanatory notice in compact presentation.

## Native-Only Checks

Some states cannot be reproduced faithfully in browser automation: OS microphone prompts, native file pickers, tray menus, updater/restart dialogs, Computer Use takeover, and physical-device network permission. The public guide documents their intent; release review must test them on the target operating system and a physical Android device.

## Troubleshooting

- If the phone cannot connect, confirm the host, port, access mode, firewall, and network reachability shown on desktop.
- If an invitation expires or was already consumed, create a new one rather than copying cookies or local credential files.
- If the connection page opens but the app remains unavailable, confirm the recipient pressed **Connect** and that the device or session was not revoked.
- If an approval is missing, open Activity and confirm the relevant thread is still active.
- If the mobile layout opens on desktop, use `?mobile=0` or reopen the normal local address. Switching layout does not require a new session.
""",
        screenshot=True,
    )

    write(
        "extending/index.mdx",
        "Extend Row-Bot",
        "Choose between built-in tools, Skills, Custom Tools, plugins, MCP servers, channels, and accounts.",
        """
# Extend Row-Bot

Start with the narrowest extension type that can solve the task. Every extension adds capabilities; some also add code, dependencies, network access, credentials, or external side effects.

For the conceptual differences between tools, Skills, plugins, MCP, channels, and accounts, read [Extensions And Trust Boundaries](/docs/concepts/extensions-and-trust).

| Extension | Use It For | Review Boundary |
| --- | --- | --- |
| Built-in tool | A capability already shipped and maintained with Row-Bot. | Enable only the tools a profile needs; consequential operations still use approval policy. |
| Skill | Reusable instructions and workflow knowledge. | Read the full skill and bundled files before enabling or pinning it. |
| Custom Tool | A reviewed local command or repo-specific helper created in Developer Studio. | Inspect its source, command classification, dependencies, and workspace scope. |
| Plugin | A packaged bundle that can provide tools, skills, channels, MCP configuration, or UI. | Review manifest, source, permissions, dependencies, and update path before enabling. |
| MCP server | Tools exposed by an external local or remote process. | Add disabled, inspect overlap and risk labels, test, then enable only trusted tools. |
| Channel | A messaging adapter for receiving or delivering work. | Verify account, recipients, pairing, tunnel, and delivery defaults before starting. |
| Account | Authorisation for GitHub, Google, X, or another service used by tools. | Review requested scopes and disconnect unused accounts. |

<Screenshot id="skills-hub" alt="Offline Row-Bot Public Skills Hub with two fictional skills." caption="Skills import into the local library off by default so you can inspect them before enabling." />

<Screenshot id="plugin-marketplace" alt="Offline Row-Bot Plugin Marketplace with fictional entries." caption="Marketplace cards expose publisher, verification, capabilities, permissions, and install review before any plugin is added." />

<Screenshot id="mcp-add-server" alt="Row-Bot Add MCP Server dialog." caption="New MCP servers start as explicit configurations; keep them disabled until transport, command or URL, environment, and tool risk are understood." />

<Screenshot id="mcp-marketplace" alt="Row-Bot Browse MCP Servers marketplace with bundled starter servers." caption="The MCP browser makes source, overlap, authentication, transport, and risk visible before you import a disabled server definition." />

## Safe Evaluation Sequence

1. Read the extension description and source.
2. Check requested permissions, credentials, network destinations, commands, and install steps.
3. Install into the local data directory only from a source you trust.
4. Keep it disabled while you inspect configuration and provided capabilities.
5. Test with fictional data and a non-consequential task.
6. Enable the minimum capabilities and keep approval mode at **Ask** until behaviour is familiar.
7. Disable or remove extensions that are no longer maintained or needed.

Third-party dependency telemetry is not Row-Bot telemetry. Review and accept any dependency behaviour before installation, and never allow it to receive Row-Bot prompts, files, memories, secrets, screenshots, tool arguments, or channel content unless that transfer is the explicit feature you chose.
""",
        screenshot=True,
    )

    write(
        "integrations/accounts.mdx",
        "Accounts And Authorisation",
        "Connect and disconnect external accounts while keeping scopes, tokens, and data flow understandable.",
        """
# Accounts And Authorisation

Accounts let selected tools act through services such as GitHub, Google, or X. A model provider account supplies models; an integration account supplies service access. They are configured separately.

## Connect Safely

1. Open **Settings -> Accounts**.
2. Choose the service and read the status and requested purpose.
3. Prefer the supported OAuth or local CLI flow rather than pasting broad tokens.
4. Verify the service, account, and scopes in the external authorisation page.
5. Return to Row-Bot and refresh status.
6. Test a read-only action before approving writes, posts, email, calendar, or repository changes.

<Screenshot id="settings-accounts" alt="Row-Bot Accounts settings with external services disconnected." caption="Account rows keep connection status and actions visible without displaying stored credentials." />

## Tokens And Scopes

Secrets use the configured operating-system secret store when available. Settings show status, not the secret value. A service may still retain its own authorisation grant until you revoke it there. Use the narrowest scopes that support the feature and disconnect access you no longer use.

## Troubleshooting

- Confirm the system browser returned to the same Row-Bot instance that started the flow.
- For GitHub CLI, refresh CLI authorisation after signing in or changing accounts.
- If a callback fails, repeat the supported flow instead of copying browser storage.
- If an action is denied after connection, review both the service scopes and the Row-Bot approval policy.
""",
        screenshot=True,
    )

    write(
        "concepts/index.mdx",
        "How Row-Bot Works",
        "Understand Row-Bot's request, knowledge, agent, workflow, and extension models before configuring them.",
        """
# How Row-Bot Works

These pages explain the ideas behind Row-Bot without walking through every control. Use them when you want to predict what the app will do, where data goes, why work continues in the background, or what an extension is allowed to change.

- [How A Request Runs](/docs/concepts/request-lifecycle) follows a message from a thread through model context, tools, approvals, and saved results.
- [How Memory Becomes Knowledge](/docs/concepts/memory-knowledge-and-dream-cycle) separates thread history, memory, documents, the Knowledge Graph, Wiki Vault, and Dream Cycle.
- [How Profiles, Goals, And Agents Work](/docs/concepts/profiles-goals-and-agents) explains reusable roles, durable objectives, parent coordination, and child-agent hand-offs.
- [How Background Workflows Run](/docs/concepts/background-workflows) explains schedules, run state, approvals, retries, and delivery.
- [Extensions And Trust Boundaries](/docs/concepts/extensions-and-trust) compares built-in tools, Skills, Custom Tools, plugins, MCP, channels, and accounts.

Each page ends with links to the settings and task guides that change the behaviour it describes.
""",
    )

    write(
        "concepts/request-lifecycle.mdx",
        "How A Request Runs",
        "Follow a Row-Bot request through context assembly, a selected model route, tools, approvals, and local history.",
        """
# How A Request Runs

A Row-Bot request is a controlled loop, not a single message sent blindly to every connected service.

1. **A thread provides continuity.** Your message joins the selected conversation. The thread supplies recent history and any thread-specific model or profile choice.
2. **Row-Bot assembles useful context.** Depending on your settings and request, this can include profile instructions, enabled Skills, attachments, and relevant local memory or document results. It does not mean the whole local data directory is attached.
3. **The selected model route receives the model prompt.** A local model keeps that inference on the configured local runtime. A hosted, API, subscription, or remote custom endpoint sends the assembled prompt to that provider under its terms.
4. **The model may answer or ask for a tool.** Row-Bot checks that the tool exists, is enabled for the active profile or workflow, and is permitted by policy.
5. **Consequential actions stop at an approval boundary.** The approval describes the proposed action and target. Rejecting it prevents that action; approving it does not give unrelated future actions permission.
6. **Tool results return to the active run.** A local file tool, browser, channel, account, plugin, or MCP server has its own execution and data boundary. The model can use the result to continue the loop.
7. **The result is recorded locally.** The thread keeps the conversation and run evidence. Optional extraction, memory, and document features decide what becomes reusable knowledge later.

## Three Boundaries To Keep In Mind

| Boundary | What crosses it | What controls it |
| --- | --- | --- |
| Model route | The assembled prompt and later tool results needed for reasoning. | Selected provider/model, thread override, profile or workflow policy. |
| Tool execution | Arguments needed by a local or external capability. | Tool enablement, workspace or account scope, approval policy. |
| Durable knowledge | Reviewed or extracted information that may be recalled in another request. | Memory, Knowledge, document, embedding, and retention settings. |

Local-first describes where Row-Bot and its durable data live. It does not make a hosted model, web tool, channel, remote MCP server, realtime voice provider, or external account local.

## Change How Requests Run

- Choose routes and defaults in [Models And Providers](/docs/configuration/models-and-providers), [Provider Settings](/docs/settings/providers), and [Model Settings](/docs/settings/models).
- Control tools and approvals in [Tools, Approvals, And Terminal](/docs/chat/tools-approvals-and-terminal) and [Privacy And Safety](/docs/privacy-safety/).
- Control reusable context in [Knowledge Settings](/docs/settings/knowledge), [Documents Settings](/docs/settings/documents), and [Skills Settings](/docs/settings/skills).
""",
    )

    write(
        "concepts/memory-knowledge-and-dream-cycle.mdx",
        "How Memory Becomes Knowledge",
        "Understand thread history, memory, documents, the Knowledge Graph, Wiki Vault, and Dream Cycle as related but distinct layers.",
        """
# How Memory Becomes Knowledge

Row-Bot has several kinds of continuity. They work together, but none is a magical copy of everything the app has ever seen.

| Layer | What it represents | How it is used |
| --- | --- | --- |
| Thread history | Messages, attachments, tool traces, approvals, and results in one conversation. | Keeps the current conversation coherent and reviewable. |
| Memory | Reusable facts, preferences, summaries, and events saved or extracted for later recall. | Supplies selected relevant context to future requests when enabled. |
| Documents | Files deliberately added to the document library and split into searchable content. | Finds passages related to a question without placing every document in every prompt. |
| Knowledge Graph | Local entities and directed relationships with source and confidence information. | Connects related people, projects, events, facts, and sources during review and recall. |
| Wiki Vault | Linked Markdown pages generated from reviewed graph material. | Makes a portable, human-browsable projection in a folder you choose. |

## From Conversation To Recall

When extraction is enabled, Row-Bot can identify useful information from eligible conversations or documents and save structured records. Search combines the available lexical, vector, and graph signals to choose candidates relevant to a later request. Recall refreshes useful memories; it does not make every stored item equally likely to appear.

Vector search uses an embedding model, which is separate from the chat model. First launch offers Mixedbread Embed Large v1 as a checked-by-default 675 MB local download. If it is skipped, unavailable, or still loading, Row-Bot continues with bounded lexical and graph fallback instead of silently downloading during a chat. Install, retry, or repair the local model later in [Documents Settings](/docs/settings/documents), then rebuild document and memory vectors after changing models. Cloud embeddings are an explicit alternative that sends indexed text to the selected provider.

The source record matters. A graph relation or memory should remain traceable to the conversation, document, or process that produced it. Correct the underlying Knowledge record before rebuilding derived indexes or Wiki Vault pages.

## What Dream Cycle Does

Dream Cycle is idle-time knowledge maintenance. When enabled and due, it can examine the local knowledge set, merge high-confidence duplicates, refresh summaries from available source context, infer strongly supported relationships, and prune stale low-confidence inferred relations. It records a journal so you can review what happened.

Dream Cycle is not an autonomous second assistant and it does not make external facts true. It should run only during the configured window while the app is idle. Inferred relationships use confidence thresholds, but important knowledge still deserves human review.

## What Wiki Vault Does

Wiki Vault writes a readable view of selected graph material. The graph remains the source of truth; the vault is a generated output. If the destination is synced to a cloud drive, repository, or publishing tool, that folder becomes a separate sharing boundary.

## Configure And Review It

- Review records in [Knowledge](/docs/knowledge/) and investigate changes in [Monitor](/docs/monitor/).
- Configure extraction, recall, embeddings, graph maintenance, Dream Cycle, and Wiki output in [Knowledge Settings](/docs/settings/knowledge).
- Configure indexed files in [Documents Settings](/docs/settings/documents).
- Set up the export folder in [Wiki Vault](/docs/knowledge/wiki-vault) and correct mistakes with [Provenance And Repair](/docs/knowledge/provenance-repair).
- Back up the related local data as one set using [Operations, Data, And Recovery](/docs/operations/).
""",
    )

    write(
        "concepts/profiles-goals-and-agents.mdx",
        "How Profiles, Goals, And Agents Work",
        "Understand reusable Agent Profiles, durable goals, parent coordination, and bounded child-agent delegation.",
        """
# How Profiles, Goals, And Agents Work

Profiles, goals, and agent runs describe different parts of long-running work.

- An **Agent Profile** is a reusable operating role: instructions, model policy, Skills, tools, delegation limits, and approval policy.
- A **goal** is the durable objective and finish condition for a piece of work that may span several turns.
- An **agent run** is one execution attempt with a prompt, model, status, result, and evidence.
- A **parent agent** coordinates the objective and combines results.
- A **child agent** receives a bounded subtask and returns a hand-off to its parent.

## Why Delegate

Delegation is useful when independent research, inspection, or implementation can happen in parallel or needs a specialist profile. It is less useful when agents would edit the same small resource or when only one decision is needed. The parent remains responsible for deciding whether the combined evidence actually completes the goal.

Child agents do not become invisible background permissions. Their runs remain linked to the parent, and a returned result is evidence rather than automatic authorisation to write, send, publish, or declare the goal complete. Tool availability and approvals still apply to the run doing the action.

Delegation capacity is bounded. Each new run snapshots the application-wide work-round, nesting, per-parent concurrency, app-wide concurrency, and optional child active-time settings. When capacity is full, eligible children wait in a first-in, first-out queue rather than bypassing the limits.

## Status And Hand-Offs

An active goal can accumulate progress across turns. Completion should mean the finish condition and required checks are satisfied. A block should identify a real impasse, not merely unfinished work. Stopping an agent ends that run without deleting the parent thread, goal history, or already-recorded evidence.

The work-round budget is checked at model, tool, and resume boundaries so a restart cannot reset it. Repeated no-progress states are blocked and then terminated cleanly, and every terminal path writes one durable final status and reason.

A useful child-agent hand-off says what was checked, what changed, what evidence was found, what remains uncertain, and whether any consequential action still needs the parent or user.

## Configure And Use It

- Create and select roles with [Profiles, Goals, And Agents](/docs/profiles-goals-agents/).
- Choose a profile in a workflow with [Workflows](/docs/guides/workflows).
- Control the models behind runs in [Models And Providers](/docs/configuration/models-and-providers).
- Review waiting work and failures in [Mobile And Native Surfaces](/docs/mobile-native/) or [Monitor](/docs/monitor/).
""",
    )

    write(
        "concepts/background-workflows.mdx",
        "How Background Workflows Run",
        "Understand saved workflow definitions, schedules, isolated runs, approvals, retries, and delivery semantics.",
        """
# How Background Workflows Run

A workflow is a saved task definition. A run is one attempt to execute it. Keeping those separate lets Row-Bot preserve history when you edit the next schedule or retry a failed attempt.

1. **A trigger starts a run.** This can be a manual click, a recurring schedule, a one-time time, or another supported trigger.
2. **The run resolves its policy.** Row-Bot chooses the workflow's Agent Profile, model override, tools, Skills, steps, and approval mode.
3. **The run gets its own state.** Status, progress, step output, logs, and any persistent thread link are recorded so the scheduler can recover cleanly.
4. **Approval can pause the run.** A background action that needs a decision becomes a pending approval instead of silently continuing. Approving resumes that action; rejecting or expiring it leaves a visible run outcome.
5. **Completion and delivery are recorded separately.** A useful result can complete even if an external channel delivery fails, so the app can show the result and the delivery problem honestly.

## Delivery Defaults

The web app always receives run status. External channels are an additional destination.

- **Inherit defaults** means the workflow uses the current workflow-level external channel selection.
- **An explicit empty selection** means web app only, even when a global external default exists.
- **An explicit channel selection** targets those configured running channels.

This distinction prevents a workflow that was deliberately set to app-only from beginning to send externally after someone changes the global default.

## Failure And Retry

A failed run remains evidence; retrying creates another attempt rather than rewriting history. Fix the underlying provider, model, tool, approval, input, or delivery problem first. For multi-step work, keep steps narrow enough that the status identifies where the run stopped.

Scheduled work can outlive the screen where it was created, but it cannot outlive the local Row-Bot process indefinitely. Pausing a workflow prevents future schedule starts; stopping one active run does not necessarily change the saved schedule.

## Configure And Operate It

- Create schedules, profiles, approvals, steps, and delivery in [Workflows](/docs/guides/workflows).
- Configure external destinations in [Channels](/docs/integrations/channels) and [Channel Settings](/docs/settings/channels).
- Review live status in [Monitor](/docs/monitor/) and mobile Activity in [Mobile And Native Surfaces](/docs/mobile-native/).
- Back up workflow definitions and history with [Operations, Data, And Recovery](/docs/operations/).
""",
    )

    write(
        "concepts/extensions-and-trust.mdx",
        "Extensions And Trust Boundaries",
        "Compare Row-Bot tools, Skills, Custom Tools, plugins, MCP servers, channels, and accounts by what they add and where they run.",
        """
# Extensions And Trust Boundaries

Row-Bot can learn a procedure, gain a tool, load code, connect a server, or authorise an account. Those are different changes and deserve different review.

| Capability | What it adds | Main trust question |
| --- | --- | --- |
| Built-in tool | A maintained capability shipped with Row-Bot. | Is it enabled for this profile, and does this action need approval? |
| Skill | Instructions, examples, and supporting files that shape behaviour. | Do the instructions match your intent and avoid unexpected data or tool use? |
| Custom Tool | Reviewed local code or a command exposed as a reusable tool. | What code runs, in which workspace, with which dependencies and command class? |
| Plugin | A packaged extension that can add code, tools, Skills, channels, configuration, or UI. | Do you trust its publisher, source, permissions, dependencies, and update path? |
| MCP server | Tools supplied by a separate local process or remote service. | What can the server access, where does it run, and which of its tools are enabled? |
| Channel | A bridge for incoming messages or outgoing results. | Who can contact it, which conversation receives replies, and may workflows deliver there? |
| Account | Authorisation for a service such as GitHub, Google, or X. | Which service scopes and account data become available to enabled tools? |

## Instructions Are Not Permissions

A Skill or profile can tell the model when to use a capability, but it does not create credentials or bypass Row-Bot's tool and approval policy. Conversely, installing a plugin or MCP server can add real executable capability even when no Skill mentions it. Review both the guidance layer and the execution layer.

## Local Does Not Always Mean Harmless

A local plugin, command, or MCP process can still read files, start programs, or use locally stored credentials. A remote server or account can send data outside the app even if its configuration is stored locally. Use narrow workspaces, least-privilege accounts, disabled-by-default imports, and fictional test data.

Third-party dependency telemetry is not Row-Bot telemetry. Review dependency behaviour before installation and do not expose prompts, files, memories, secrets, screenshots, tool arguments, or channel content unless that transfer is the feature you intentionally selected.

## Browse And Configure Extensions

- Compare extension types and browse screenshots in [Extend Row-Bot](/docs/extending/).
- Browse and review Skills in [Skills Hub](/docs/skills/) and [Skills Settings](/docs/settings/skills).
- Install and control packaged code in [Plugins](/docs/integrations/plugins) and [Plugin Settings](/docs/settings/plugins).
- Browse, add, test, and enable servers in [MCP](/docs/integrations/mcp) and [MCP Settings](/docs/settings/mcp).
- Configure external messaging in [Channels](/docs/integrations/channels) and service authorisation in [Accounts And Authorisation](/docs/integrations/accounts).
""",
    )

    write(
        "operations/index.mdx",
        "Operations, Data, And Recovery",
        "Back up, restore, update, repair, and uninstall Row-Bot without losing track of local data and credentials.",
        """
# Operations, Data, And Recovery

Row-Bot is local-first, so operational safety starts with knowing which data directory and workspace are active. Conversations, settings, memory, Knowledge, workflows, documents, Designer projects, Developer workspace records, skills, plugins, logs, channel state, and MCP configuration can live under that directory. Operating-system key stores may hold credentials separately.

## Remote Access And Server Operations

Use [Remote Access And Server Mode](/docs/operations/remote-access) for the complete guide to one-time invitations, desktop and compact owner sessions, Tailscale Serve, LAN, SSH forwarding, Docker, HTTPS reverse proxies, browser-local voice, access recovery, and proxy error diagnostics.

Use [Docker And VPS Operations](/docs/operations/docker) for pull-first Compose startup, release and digest pins, persistent volumes, offline backup and restore, explicit upgrade and rollback, host Caddy or Tailscale, secret-file mounts, and container-specific Developer boundaries.

Remote access remains off by default in ordinary desktop launches. Server operators should back up access state with the rest of the active data directory, keep one worker, publish one canonical origin, terminate remote traffic with HTTPS, and trust only the exact reverse proxy that connects to Row-Bot.

## Back Up

1. Open Settings and note the active data and workspace paths.
2. Stop running workflows, channels, servers, and active generations.
3. Close Row-Bot so databases are not changing during the copy.
4. Copy the whole active data directory to protected storage.
5. Back up external workspaces, exported Designer files, and Wiki vaults separately when they live elsewhere.
6. Record the Row-Bot version used with the backup.

## Restore

Restore related databases and files as one set while Row-Bot is closed. Point a test launch at the restored directory first, then check conversations, providers, workflows, Knowledge, documents, Designer, Developer, plugins, MCP, and channels. Reconnect secrets that were stored in an OS keychain and were not included in the file backup.

## Update, Repair, And Uninstall

Use the supported package or updater flow for your platform. Back up before a major update. Repair may replace application files but should not be treated as a data backup. Uninstalling the application and deleting the local data directory are separate decisions; review both the application location and the data location before removal.

## Logs And Support

Use Monitor and the local log folder to diagnose startup, provider, workflow, channel, and background failures. Before sharing diagnostics, remove names, paths, account identifiers, document content, prompts, message content, tokens, and screenshots that expose private data.

## Release-Specific Manual Checks

Installer UX, upgrade, repair, uninstall, Windows signing, macOS notarisation, native file dialogs, OS permissions, and clean-machine behaviour require manual testing on release candidates. A successful docs build does not prove those native flows are release-ready.
""",
    )

    write(
        "troubleshooting/index.mdx",
        "Troubleshooting",
        "Resolve setup, model, chat, workflow, Designer, Developer, Knowledge, Monitor, Settings, channel, MCP, plugin, skill, voice, and Buddy issues.",
        """
# Troubleshooting

Start with the visible status text in Row-Bot. Then check the relevant Settings tab and Monitor. Most issues fall into one of four categories: setup is incomplete, a provider or tool is disabled, the wrong model is selected, or Row-Bot is waiting for approval.

## Setup Problems

- Reopen Setup Center if first launch was skipped or incomplete.
- Check Settings -> Providers and Settings -> Models before troubleshooting Chat.
- Check Settings -> System if local files, browser automation, command execution, or tunnels are involved.
- For native application control, open the Computer Use setup card and resolve its driver or operating-system permission status before retrying.

## Model Problems

- Refresh Providers, then refresh Models.
- Choose a tool-capable model for workflows, Designer, Developer, and multi-step tool use.
- For local and custom endpoints, use enough context for Row-Bot's instructions and tool schemas.
- If reasoning choices are missing, rejected, or reset, use the active model's conditional control or `/reasoning`, then follow [Reasoning Controls](/docs/chat/reasoning-controls#troubleshooting).

## Chat And Tool Problems

- Check the model picker, Agent Profile, approval mode, and enabled tools.
- If an action is waiting, review the thread approval prompt or open Activity Center.
- Ask Row-Bot to explain the last tool result if the transcript is unclear.
- If Computer Use is paused, choose Resume only after reviewing the target; if its lease is busy, stop or finish the active native task first.

## Workflow Problems

- Confirm the workflow is not paused.
- Run it manually once before trusting a schedule.
- Check delivery defaults and channel status.

## Designer And Developer Problems

- Designer: add a clearer goal, audience, references, or brand details.
- Developer: confirm a workspace is open before asking for code actions.
- Review commands and patches before approving or importing changes.

## Knowledge And Monitor Problems

- If Knowledge is empty, enable memory or index documents.
- If a record is wrong, edit or remove it.
- Use Monitor logs, extraction journal, dream journal, and View Full Log for background issues.

## Integrations

- Channels need credentials, optional tunnel setup, start/stop state, and user pairing.
- MCP needs the global switch, a trusted server, and a successful test.
- Plugins need installation, enablement, configuration, and sometimes a restart.
- Skills need to be enabled and relevant to the prompt.

## Remote Access And Server Mode

- If the browser shows `untrusted_forwarding_headers`, a proxy supplied forwarding metadata from an address Row-Bot does not trust. Restart after enabling a Row-Bot-owned Tailscale route. For another proxy, configure only its exact connecting address or CIDR; do not trust a broad network.
- If the browser shows `unexpected_host`, add the exact canonical browser-facing host and verify the proxy preserves it.
- If an invitation cannot be claimed, create it for the exact scheme, host, and port currently used by that browser. Expired, consumed, and stale-route invitations must be recreated.
- If Tailscale reports a conflict, Funnel, or an unowned route, inspect that configuration manually. Row-Bot deliberately refuses to reset or overwrite it.
- If a remote microphone is unavailable, use `localhost` or HTTPS, grant browser permission, and install the local voice model explicitly.
- Use `?mobile=1` for compact presentation or `?mobile=0` for desktop presentation. Switching layout does not change the session or its owner authority.
- Run `row-bot access doctor` for a secret-free server and route safety report.

The full route, authentication, Docker, proxy, and recovery guidance is in [Remote Access And Server Mode](/docs/operations/remote-access).

## Voice And Buddy

- Check microphone/output device selection and permissions.
- Use Dictate before Talk if you need review.
- Check provider readiness for realtime voice.
- Toggle Buddy visibility or reopen the overlay if the companion is missing.
""",
    )

    write(
        "reference/index.mdx",
        "Reference",
        "Look up Row-Bot tools, providers, settings, channels, skills, MCP, plugins, data storage, and approvals.",
        """
# Reference

Use the reference pages when you need a table or lookup after reading the guided docs. These pages are more compact and more technical than the walkthroughs.

- [Tools](/docs/reference/generated/tools)
- [Providers](/docs/reference/generated/providers)
- [Settings](/docs/reference/generated/settings)
- [Settings Controls](/docs/reference/generated/settings-controls)
- [Home Tabs](/docs/reference/generated/home-tabs)
- [Channels](/docs/reference/generated/channels)
- [Skills](/docs/reference/generated/skills)
- [MCP](/docs/reference/generated/mcp)
- [Plugins](/docs/reference/generated/plugins)
- [Data Storage](/docs/reference/generated/data-storage)
- [Safety And Approvals](/docs/reference/generated/safety-approvals)
- [Environment And Config](/docs/reference/generated/environment-and-config)
- [CLI](/docs/reference/generated/cli)
- [Screenshots](/docs/reference/generated/screenshots)
""",
    )

    write(
        "configuration/models-and-providers.mdx",
        "Models And Providers",
        "Configure providers, discover models, choose defaults, and pin quick model choices.",
        """
# Models And Providers

Providers are where models come from. Models are the specific choices you use in Chat, workflows, Designer, Developer, voice, and other Row-Bot surfaces.

To understand what leaves the app, when tools run, and where approvals fit, read [How A Request Runs](/docs/concepts/request-lifecycle).

## Recommended Setup Order

1. Open Settings -> Providers.
2. Connect one provider path.
3. Refresh provider health.
4. Open Settings -> Models.
5. Refresh the catalog.
6. Choose a default model.
7. Pin Quick Choices.
8. Test in Chat.

## Choosing A Provider Path

Use local Ollama for local-first privacy and no provider billing. Use an API provider for strong hosted models. Use a subscription account when you already have the supported provider account. Use a custom endpoint for advanced local or self-hosted OpenAI-compatible runtimes.

## Choosing Models

Keep at least one everyday chat model and one stronger tool-capable model pinned. For Developer, Designer, workflows, and complex tools, choose a model that can handle tool calls and enough context.

For models with exact reasoning capabilities, Row-Bot exposes a per-thread [Reasoning control](/docs/chat/reasoning-controls) with only the efforts, toggle, or token-budget choices that model supports. Provider default remains available as the compatibility-safe option.

For Ollama, Row-Bot prefers explicit tool-calling metadata, then the daemon's reported capability list. A reported `tools` capability enables agent use even for a model family newer than Row-Bot's maintained fallback catalogue; an explicit capability list that omits `tools` remains authoritative. Family fallbacks are used only when the daemon provides no capability metadata.

## Troubleshooting

- If a provider connects but has no models, refresh Models.
- If a model fails with tools, choose a tool-capable model.
- If an Ollama model is unexpectedly excluded from agent use, refresh the local catalogue and inspect the capabilities reported by the installed Ollama daemon.
- If a custom endpoint fails, check base URL, model name, API compatibility, and context window.
""",
    )

    write(
        "ui-tour/index.mdx",
        "UI Tour",
        "A short guided tour of the Row-Bot Interface, Chat, Home, Settings, and Activity Center.",
        """
# UI Tour

Use this short tour if you want the quickest mental map before reading detailed pages.

1. Start in the [Row-Bot Interface](/docs/app-shell/navigation) guide to understand the sidebar, Home tabs, Activity Center, Settings, Buddy, and terminal.
2. Read [Chat](/docs/chat/) to understand threads, composer controls, models, attachments, tool traces, and approvals.
3. Visit [Home](/docs/home/) to see how Workflows, Designer, Developer, Knowledge, and Monitor are split.
4. Open [Settings](/docs/settings/) when you are ready to connect providers, documents, search, skills, system access, accounts, channels, MCP, plugins, Buddy, voice, and preferences.
5. Keep [Privacy And Safety](/docs/privacy-safety/) nearby when enabling external services or action tools.
""",
    )

    legacy = {
        "guides/designer-studio.mdx": ("Designer Studio Guide", "/docs/designer/", "Designer Studio"),
        "guides/developer-studio.mdx": ("Developer Studio Guide", "/docs/developer/", "Developer Studio"),
        "guides/skills-plugins-mcp.mdx": ("Skills, Plugins, And MCP Guide", "/docs/skills/", "Skills Hub"),
        "guides/channels-and-voice.mdx": ("Channels And Voice Guide", "/docs/integrations/channels", "Channels"),
    }
    for rel, (title, target, label) in legacy.items():
        write(
            rel,
            title,
            f"Compatibility page pointing to the current {label} guide.",
            f"""
# {title}

This guide has been split into focused pages so each feature has one authoritative walkthrough.

- [{label}]({target})
- [Plugins](/docs/integrations/plugins)
- [MCP](/docs/integrations/mcp)
- [Voice And Buddy](/docs/voice-and-buddy/)
- [Settings](/docs/settings/)
""",
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
