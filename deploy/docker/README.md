# Run Row-Bot with Docker

This deployment runs Row-Bot as a single-owner, multi-device server. It is not
a multi-user or hostile multi-tenant isolation boundary.

Read the public
[Remote Access And Server Mode guide](../../docs-site/docs/operations/remote-access.mdx)
first for the shared invitation, session, route, proxy, and browser-voice model.
This runbook contains the Docker-specific commands and operational details.

The example is local-first: port 8080 is published on the Docker host's
`127.0.0.1` only. Every browser, including a browser on that host, must claim an
invitation before it can use server mode. Docker bridge or gateway addresses
never grant owner access.

## Start an isolated instance

From the repository root:

```sh
docker compose -f deploy/docker/compose.yaml up --build --detach
docker compose -f deploy/docker/compose.yaml ps
```

The image is built with Python 3.13 from `pyproject.toml` and `uv.lock`. It is
the complete supported Row-Bot server feature set: all canonical Python extras,
the matching Playwright Chromium, native media libraries, `uv`/`uvx`, and a
pinned Node.js LTS with `node`/`npm`/`npx` are installed in the normal image.
There is no separate minimal/full choice and no Python extra installation is
required after startup.

The runtime process has UID/GID 10001, has no Linux capabilities, and writes
durable state under `/data` in the project-scoped `row_bot_data` volume. The
application runs in the foreground as `row-bot serve`; Compose owns restarts.
Python and browser assets under `/opt` are immutable. Build metadata at
`/opt/row-bot/build-metadata.txt` records the dependency-lock digest and the
Playwright Chromium revision. The base image versions are pinned in the
Dockerfile; Debian Bookworm system packages still follow the configured
security repositories when the image is rebuilt, so retain the resulting
image digest for repeatable rollback.

Wait for the service to report `healthy`, then open
`http://127.0.0.1:8080`. A fresh server shows the connection page, not the
Row-Bot application.

The image and Compose file contain no invitation, provider credential, or
reusable session. Do not add those values as Docker build arguments, image
environment variables, or Compose labels.

## Authorize a browser

Create an invitation explicitly from a trusted terminal. The command prints a
one-time secret link, so keep terminal output and scrollback private.

Full Row-Bot for a computer:

```sh
docker compose -f deploy/docker/compose.yaml exec row-bot \
  row-bot access invite --layout desktop --origin http://127.0.0.1:8080
```

Full Row-Bot for a phone or tablet, using the compact layout:

```sh
docker compose -f deploy/docker/compose.yaml exec row-bot \
  row-bot access invite --layout compact --origin http://127.0.0.1:8080
```

Opening an invitation displays a confirmation page. It is consumed only when
the recipient presses **Connect**. The resulting browser session is separate,
persistent, and revocable. Both layouts represent the same owner and expose the
same product authority, including Settings.

Use the normal access commands for recovery and device management:

```sh
docker compose -f deploy/docker/compose.yaml exec row-bot row-bot access list
docker compose -f deploy/docker/compose.yaml exec row-bot row-bot access doctor
docker compose -f deploy/docker/compose.yaml exec row-bot row-bot access revoke DEVICE_ID
docker compose -f deploy/docker/compose.yaml exec row-bot row-bot access revoke-all
```

The list and doctor commands do not print raw invitation or session secrets.

## Publish intentionally

The default host publication is:

```text
127.0.0.1:8080 -> container port 8080
```

Keep this default for SSH forwarding, a host reverse proxy, or local use. For
an SSH tunnel from a workstation:

```sh
ssh -N -L 18080:127.0.0.1:8080 user@row-bot-host
```

Create the invitation for the browser-facing origin
`http://127.0.0.1:18080`.

For intentional LAN access, bind a specific private address rather than every
interface where possible:

```sh
ROW_BOT_BIND_ADDRESS=192.168.1.20 docker compose \
  -f deploy/docker/compose.yaml up --detach
```

PowerShell equivalent:

```powershell
$env:ROW_BOT_BIND_ADDRESS = "192.168.1.20"
docker compose -f deploy/docker/compose.yaml up --detach
```

LAN HTTP is unencrypted. Anyone who can reach the port can see the neutral
connection page, but a valid Row-Bot session is still required. Prefer
Tailscale or an HTTPS reverse proxy on shared or untrusted networks. Configure
the host firewall yourself; Row-Bot and this Compose file do not modify it.

For public hosting, keep the published port on loopback and terminate TLS at a
dedicated origin. Start with
[`../reverse-proxy/Caddyfile.example`](../reverse-proxy/Caddyfile.example) and
configure all three Row-Bot values explicitly:

```text
ROW_BOT_PUBLIC_URL=https://row-bot.example.com
ROW_BOT_ALLOWED_HOSTS=row-bot.example.com
ROW_BOT_TRUSTED_PROXY_CIDRS=<exact proxy address or CIDR>
```

Trust only the address that actually connects to Row-Bot. Do not add a whole
Docker private-address range merely because the proxy runs in a container.
Wrong Host, Origin, or untrusted forwarded metadata must remain rejected.

## Run two or more instances

Compose project names isolate the containers, networks, and named volumes.
Use a different host port for each project:

```sh
docker compose --project-name row-bot-main \
  -f deploy/docker/compose.yaml up --detach

ROW_BOT_HOST_PORT=8081 docker compose --project-name row-bot-lab \
  -f deploy/docker/compose.yaml up --detach
```

PowerShell equivalent for the second instance:

```powershell
$env:ROW_BOT_HOST_PORT = "8081"
docker compose --project-name row-bot-lab `
  -f deploy/docker/compose.yaml up --detach
```

Create invitations with `--origin http://127.0.0.1:8080` and
`--origin http://127.0.0.1:8081`, respectively. Each fresh data volume contains
its own persisted instance identity, so the browser cookie names and sessions
do not collide even though both origins use the same hostname. Do not clone one
instance's access database into another unless you are intentionally restoring
that same instance.

## Back up, restore, and upgrade

The named `/data` volume is the complete persistent Row-Bot state for this
deployment. It can include conversations, access records, configuration, and
other private user data. Treat backups as secrets.

For a consistent offline backup:

1. Run `docker compose -f deploy/docker/compose.yaml stop row-bot`.
2. Archive the project-scoped `row_bot_data` volume with your normal encrypted
   Docker-volume backup tool.
3. Restart with `docker compose -f deploy/docker/compose.yaml start row-bot`.
4. Test restoration into a separate project name and unpublished port before
   relying on it.

Before an upgrade, back up the volume and record the exact image digest or
source commit used to build it. Build or pull a pinned version, then run
`docker compose -f deploy/docker/compose.yaml up --detach`. Verify `/healthz`,
`/readyz`, an existing session, and `row-bot access doctor`. For rollback,
stop the service and restore both the previous image and its matching
pre-upgrade data backup; database migrations can make an old image
incompatible with newer state.

Provider and channel credentials must use Row-Bot's supported secret storage.
Do not bake credentials into an image or commit them in an override file. On a
headless host, run `row-bot access doctor` after restart and resolve any
warning about process-only secret persistence before calling the service
unattended.

## Complete server features and explicit downloads

Installing support is not the same as configuring or starting a feature. The
image contains the Python packages for local voice, Designer and document
export, browser automation, channels, ngrok, MCP, local embeddings, and media.
It also contains their shared native prerequisites. Startup does **not**
download models, install arbitrary MCP servers, contact a model provider, send
a channel message, start ngrok, or start another tunnel.

Playwright Chromium is bundled under
`/opt/row-bot/playwright-browsers` and runs headlessly in server mode. Browser
automation therefore does not reinstall Chromium into the data volume.
Interactive downloads chosen later by the owner are kept in the named volume:

```text
/data/cache/huggingface
/data/cache/sentence-transformers
/data/cache/torch
/data/cache/uv
/data/runtimes
/data/tmp
```

Whisper, Kokoro, embedding, and other model files are not baked into the image.
Their first download must be an explicit owner action and may require several
gigabytes of persistent volume capacity. Reusing the same `/data` volume makes
those caches available after an offline restart. Local embeddings use a
CPU-only baseline; GPU/CUDA acceleration and host GPU access are not assumed.

The included `uv`/`uvx` and `node`/`npm`/`npx` commands support explicitly
approved stdio MCP runtimes. They are general runtimes, not preinstalled MCP
servers. A selected server may still require its own external executable;
install it explicitly into `/data/runtimes` or add a narrowly scoped read-only
mount. Docker itself is intentionally unavailable inside the container: never
mount the host Docker socket merely to satisfy an MCP server.

Telegram, Slack, Twilio, Discord, and pyngrok support are installed but
unconfigured. Row-Bot does not connect a channel, send a message, install an
ngrok binary, supply ngrok credentials, or open a tunnel until the owner
configures and explicitly invokes that action.

## Voice from a remote browser

Browser-local voice captures the requesting browser's microphone, performs
local Whisper transcription in the server, synthesizes local Kokoro audio, and
plays the result only in that same browser. The container does not use the
Docker host's microphone or speakers. Model installation remains explicit and
the model cache persists under `/data`.

Browsers allow microphone capture on `localhost` or in a secure HTTPS context.
Plain HTTP on a remote LAN address is not sufficient; publish Row-Bot through
Tailscale HTTPS or a correctly configured HTTPS reverse proxy before using a
remote microphone. Browser permission is requested only after the user starts
voice capture. OpenAI Realtime voice is a separate, explicitly configured
provider/network feature; local browser voice does not silently fall back to
it.

## Read-only secret files

For an unattended server, mount one allowlisted provider or channel secret per
file into `/run/secrets`. The filename is its canonical setting name, such as
`OPENAI_API_KEY` or `TELEGRAM_BOT_TOKEN`; the file contains only the value and
an optional trailing newline. `ROW_BOT_SECRETS_DIR` already points at
`/run/secrets`.

Keep secret files outside the repository and add a private Compose override:

```yaml
services:
  row-bot:
    volumes:
      - /absolute/private/row-bot-secrets:/run/secrets:ro
```

Restrict the host directory to the account that operates Docker. Row-Bot reads
this source without copying its values into `/data`; externally managed values
remain read-only in Settings. A conflicting environment value is an error.
Never put secret values in the Dockerfile, Compose environment, labels, build
arguments, command line, or image layers.

## Operational notes

- `/healthz` is a minimal liveness endpoint. `/readyz` is the readiness check;
  neither should reveal providers, paths, devices, or configuration.
- Stop with `docker compose -f deploy/docker/compose.yaml down`. The named data
  volume is retained. Adding `--volumes` deletes persistent state and is
  intentionally not part of the normal procedure.
- The container has a read-only root filesystem and a writable, `noexec` `/tmp`
  tmpfs. Python runtime temp files use `/data/tmp` so local phonemizer libraries
  can be loaded without weakening the shared `/tmp` mount.
  Add only narrowly scoped mounts needed for files you deliberately want
  Row-Bot to use.
- Chromium receives a private 256 MiB `/dev/shm`; Compose does not use host IPC,
  privileged mode, host devices, the Docker socket, or a disabled browser
  sandbox.
- Native tray/windows and native desktop Computer Use are unavailable in a
  headless container. No physical host microphone, speaker, camera, display,
  or screen is mounted by default. Browser-local voice and bundled headless
  browser automation remain available.
- Host fonts, a desktop display server, and GPU acceleration are not assumed.
  The image supplies its own common fonts but rendering can differ from a
  desktop that has additional fonts.
- NiceGUI runs as a single process. Do not scale this service to multiple
  workers against the same data volume.
- Public HTTPS hosting remains an explicit operator choice. Review rate limits,
  recovery, logs, backups, and reverse-proxy behavior before exposing it.
