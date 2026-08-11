# Run Row-Bot with Docker

This deployment runs Row-Bot as a single-owner, multi-device server. It is not
a multi-user or hostile multi-tenant isolation boundary.

Read the public [Docker And VPS Operations](https://row-bot.ai/docs/operations/docker)
guide first for the pull-first deployment, VPS, backup, upgrade, and recovery
workflow. [Remote Access And Server Mode](https://row-bot.ai/docs/operations/remote-access)
explains the shared invitation, session, route, proxy, and browser-voice model.
This repository runbook retains source-build and detailed operator notes.

The example is local-first: port 8080 is published on the Docker host's
`127.0.0.1` only. Every browser, including a browser on that host, must claim an
invitation before it can use server mode. Docker bridge or gateway addresses
never grant owner access.

## Start an isolated instance from GHCR

From the repository root:

```sh
export ROW_BOT_IMAGE=ghcr.io/siddsachar/row-bot:X.Y.Z
docker buildx imagetools inspect "${ROW_BOT_IMAGE}"
docker compose -f deploy/docker/compose.yaml up --detach
docker compose -f deploy/docker/compose.yaml ps
```

Replace `X.Y.Z` with a GitHub Release that has a published Row-Bot container.
A source tag can exist without a container package; `manifest unknown` means
you must choose a release that includes the image or use the source-build
override below.

The default image is `ghcr.io/siddsachar/row-bot:latest`; set `ROW_BOT_IMAGE`
to a release tag or immutable digest for controlled upgrades and rollback. A
fresh `up` pulls an absent image. Later upgrades use an explicit
`docker compose pull` so an ordinary restart does not make a surprise network
request.

The official image is built with Python 3.13 from `pyproject.toml` and
`uv.lock`. It is the complete supported Row-Bot server feature set: all
canonical Python extras, the matching Playwright Chromium, native media
libraries, `uv`/`uvx`, and pinned Node.js with `node`/`npm`/`npx` are installed
in the normal image. There is no separate minimal/full choice and no Python
extra installation is required after startup.

The runtime process has UID/GID 10001, has no Linux capabilities, and writes
durable state under `/data` in the project-scoped `row_bot_data` volume. A
short-lived, network-disabled initializer creates a random encryption key once
in the separate project-scoped `row_bot_secrets` volume; the application mounts
that volume read-only. The application runs in the foreground as
`row-bot serve`; Compose owns restarts.
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

No credential-storage setup is required for the normal Compose path. The
generated key lets a headless container encrypt ChatGPT/Codex tokens and other
owner-entered credentials under `/data/secure-secrets`, so they survive restart
and container replacement. Back up the data and key volumes together. Advanced
operators can replace the generated key volume with the external read-only
secret directory described below.

To build the same full image from the current checkout instead, add the source
override explicitly:

```sh
docker compose \
  -f deploy/docker/compose.yaml \
  -f deploy/docker/compose.build.yaml \
  up --build --detach
```

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
docker compose -f deploy/docker/compose.yaml exec row-bot row-bot access doctor --host 127.0.0.1
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

For a Linux VPS with host Caddy, use `compose.vps.yaml` with Compose 2.24.4 or
newer. It clears inherited port publication, uses host networking, binds
Row-Bot to `127.0.0.1`, requires the exact public URL and allowed host, and
trusts only `127.0.0.1/32`. Terminate TLS at a dedicated origin. Start with
[`../reverse-proxy/Caddyfile.example`](../reverse-proxy/Caddyfile.example) and
configure all three Row-Bot values explicitly:

```text
ROW_BOT_PUBLIC_URL=https://row-bot.example.com
ROW_BOT_ALLOWED_HOSTS=row-bot.example.com
ROW_BOT_TRUSTED_PROXY_CIDRS=127.0.0.1/32
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

The named `/data` volume contains Row-Bot state such as conversations, access
records, configuration, and other private user data. The separate named secret
volume contains the encryption key required to recover credentials stored
under `/data/secure-secrets`. Treat both backups as secrets.

For a consistent offline backup:

1. Run `docker compose -f deploy/docker/compose.yaml stop row-bot`.
2. Archive the project-scoped `row_bot_data` and `row_bot_secrets` volumes with
   your normal encrypted Docker-volume backup tool, or use the network-disabled
   tar-helper procedure in the public guide. Do not use `docker compose cp` for
   the complete data tree on Windows because model caches can contain Linux
   symbolic links.
3. Restart with `docker compose -f deploy/docker/compose.yaml start row-bot`.
4. Test restoration into a separate project name and unpublished port before
   relying on it.

Before an upgrade, back up the volume and record the exact image digest or
source commit used to build it. Set `ROW_BOT_IMAGE` to a pinned version, run
`docker compose -f deploy/docker/compose.yaml pull row-bot`, then run
`docker compose -f deploy/docker/compose.yaml up --detach`. Verify `/healthz`,
`/readyz`, an existing session, and `row-bot access doctor`. For rollback,
stop the service and restore both the previous image and its matching
pre-upgrade data backup; database migrations can make an old image
incompatible with newer state.

Provider and channel credentials must use Row-Bot's supported secret storage.
Do not bake credentials into an image or commit them in an override file. On a
headless host, run `row-bot access doctor` after restart and confirm the
generated or operator-managed encrypted store remains available before calling
the service unattended.

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
The same first-run flow is used by Docker, desktop, and source installs. It
clearly discloses and selects **Mixedbread Embed Large v1**, the recommended
private knowledge model, as a checked-by-default 675 MB download. It is separate
from the chat model and provides semantic memory and document search. The
download starts only when the owner finishes setup and can be skipped; bounded
lexical and graph recall continue until it is installed later from
**Settings -> Documents**. Other model downloads remain explicit owner actions.
Reusing the same `/data` volume keeps those caches available after an offline
restart. Local embeddings use a CPU-only baseline; GPU/CUDA acceleration and
host GPU access are not assumed.

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

The default stack already persists UI-entered secrets through its generated
key volume. For a centrally managed unattended server, you can instead mount
one allowlisted provider or channel secret per file into `/run/secrets`. The
filename is its canonical setting name, such as `OPENAI_API_KEY` or
`TELEGRAM_BOT_TOKEN`; the file contains only the value and an optional trailing
newline. `ROW_BOT_SECRETS_DIR` already points at `/run/secrets`.

This override replaces the automatic key volume, so the host directory must
also contain `ROW_BOT_SECRET_STORE_KEY`. Keep secret files outside the
repository. Copy
`compose.secrets.yaml.example`, set `ROW_BOT_SECRETS_HOST_DIR` to the absolute
private host directory, and add the override explicitly:

```sh
export ROW_BOT_SECRETS_HOST_DIR=/absolute/private/row-bot-secrets
docker compose \
  -f deploy/docker/compose.yaml \
  -f deploy/docker/compose.secrets.yaml.example \
  up --detach
```

The resulting mount is `/run/secrets:ro`.

Create the replacement 32-byte master key as exactly 64 hexadecimal characters:

```sh
sudo install -d -o 10001 -g 10001 -m 0700 "$ROW_BOT_SECRETS_HOST_DIR"
openssl rand -hex 32 | sudo tee \
  "$ROW_BOT_SECRETS_HOST_DIR/ROW_BOT_SECRET_STORE_KEY" >/dev/null
sudo chown 10001:10001 \
  "$ROW_BOT_SECRETS_HOST_DIR/ROW_BOT_SECRET_STORE_KEY"
sudo chmod 0400 \
  "$ROW_BOT_SECRETS_HOST_DIR/ROW_BOT_SECRET_STORE_KEY"
```

For the normal Compose path, the initializer creates this key automatically in
`row_bot_secrets`. With the override, Row-Bot keeps your operator-managed key
read-only in `/run/secrets` and uses it to
encrypt rotating ChatGPT/Codex and other saved credentials under
`/data/secure-secrets`. The encrypted records survive a restart or container
replacement with the same data volume and key. Back up the key separately from
the data volume. A missing key retains the existing session-only behavior; a
changed or invalid key fails closed and cannot overwrite existing records.

Restrict the host directory to the Docker operator and container UID/GID 10001.
Externally managed provider/channel value files remain read-only in Settings
and are not copied into `/data`. A conflicting environment value is an error.
Never put secret values in the Dockerfile, Compose environment, labels, build
arguments, command line, or image layers.

## Operational notes

- `/healthz` is a minimal liveness endpoint. `/readyz` is the readiness check;
  neither should reveal providers, paths, devices, or configuration.
- Stop with `docker compose -f deploy/docker/compose.yaml down`. Both named
  volumes are retained. Adding `--volumes` deletes persistent data and the
  generated encryption key and is intentionally not part of the normal
  procedure.
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

## Trusted sessions and Developer execution

Trusted sessions last up to 30 days. An active authenticated owner UI checks
at startup and every 12 hours; inside the final seven days it renews the trusted
session to 30 days. An inactive browser can still expire, and temporary
12-hour or migrated legacy sessions do not renew. Recover a fully expired
instance with a new one-time invitation from a trusted terminal or SSH session.

Developer execution has three separate cases:

1. A host-installed Row-Bot can use Docker Sandbox when the host runtime is
   available.
2. Inside the official application container, Developer Docker Sandbox is not
   available and requested Docker workspaces fail closed. Never mount the host
   Docker socket. Local mode can operate only on an explicitly mounted path.
3. An approved risky Custom Tool deliberately executes in Local mode inside
   the application container against its selected visible path. It is not a
   nested Docker sandbox or a fallback from a Docker workspace.

## Container verification and publication

`.github/workflows/container.yml` deliberately separates verification from
publication:

- Pull requests that touch container inputs build and smoke native amd64 and
  arm64 images with `push: false`.
- `workflow_dispatch` performs the same verification and does not publish.
- Publishing a GitHub Release builds and smokes both native architectures,
  pushes temporary architecture tags only after each smoke passes, and then
  creates the multi-platform release manifest. Stable releases also update
  `latest`; prereleases do not.

After a release, a maintainer must confirm the GHCR package is public and linked
to this repository, then test an anonymous pull. Verify both platforms and the
manifest digest:

```sh
docker logout ghcr.io
docker buildx imagetools inspect ghcr.io/siddsachar/row-bot:X.Y.Z
docker pull --platform linux/amd64 ghcr.io/siddsachar/row-bot:X.Y.Z
docker pull --platform linux/arm64 ghcr.io/siddsachar/row-bot:X.Y.Z
```

Run the normal authenticated-container smoke on clean native amd64 and arm64
hosts, record the manifest digest in the release notes, and verify an immutable
`ghcr.io/siddsachar/row-bot@sha256:...` pull before recommending the image.
