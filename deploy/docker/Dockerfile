# syntax=docker/dockerfile:1.7

ARG PYTHON_IMAGE=python:3.13.5-slim-bookworm
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.7.22
ARG NODE_IMAGE=node:24.4.1-bookworm-slim

FROM ${UV_IMAGE} AS uv
FROM ${NODE_IMAGE} AS node

FROM ${PYTHON_IMAGE} AS builder

COPY --from=uv /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/row-bot/.venv \
    PLAYWRIGHT_BROWSERS_PATH=/opt/row-bot/playwright-browsers

WORKDIR /build

# Resolve the canonical aggregate feature set before copying application
# sources so ordinary source changes do not invalidate the dependency layer.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --extra all --no-install-project

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --extra all

# The Playwright package in uv.lock selects the exact compatible Chromium and
# ffmpeg revisions. Browser payloads are immutable image dependencies; models
# and user-selected managed runtimes are deliberately not installed here.
RUN --mount=type=cache,target=/root/.cache/ms-playwright \
    /opt/row-bot/.venv/bin/python -m playwright install chromium

ARG ROW_BOT_SOURCE_REVISION=unknown
RUN install -d /opt/row-bot \
    && { \
        printf 'source_revision=%s\n' "${ROW_BOT_SOURCE_REVISION}"; \
        sha256sum uv.lock; \
        /opt/row-bot/.venv/bin/python -c "import json, pathlib, playwright; package = pathlib.Path(playwright.__file__).parent / 'driver' / 'package'; data = json.loads((package / 'browsers.json').read_text()); chromium = next(item for item in data['browsers'] if item['name'] == 'chromium'); print(f\"playwright_chromium_revision={chromium['revision']}\"); print(f\"playwright_chromium_version={chromium['browserVersion']}\")"; \
    } > /opt/row-bot/build-metadata.txt

FROM ${PYTHON_IMAGE} AS runtime

ARG ROW_BOT_UID=10001
ARG ROW_BOT_GID=10001
ARG ROW_BOT_VERSION=0.0.0
ARG ROW_BOT_SOURCE_REVISION=unknown

LABEL org.opencontainers.image.title="Row-Bot authenticated server" \
      org.opencontainers.image.description="Local-first Row-Bot AI assistant in authenticated long-running server mode" \
      org.opencontainers.image.source="https://github.com/siddsachar/row-bot" \
      org.opencontainers.image.version="${ROW_BOT_VERSION}" \
      org.opencontainers.image.revision="${ROW_BOT_SOURCE_REVISION}" \
      io.row-bot.python="3.13.5" \
      io.row-bot.node="24.4.1" \
      io.row-bot.playwright.chromium-revision="1223" \
      io.row-bot.dependency-lock="/opt/row-bot/build-metadata.txt"

ENV HOME=/data \
    PATH=/opt/row-bot/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ROW_BOT_APP_ROOT=/opt/row-bot/app \
    ROW_BOT_CONTAINERIZED=1 \
    ROW_BOT_DATA_DIR=/data \
    ROW_BOT_DEPLOYMENT_MODE=server \
    ROW_BOT_HOST=0.0.0.0 \
    ROW_BOT_PORT=8080 \
    ROW_BOT_BROWSER_HEADLESS=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/row-bot/playwright-browsers \
    XDG_CACHE_HOME=/data/cache \
    HF_HOME=/data/cache/huggingface \
    TORCH_HOME=/data/cache/torch \
    SENTENCE_TRANSFORMERS_HOME=/data/cache/sentence-transformers \
    UV_CACHE_DIR=/data/cache/uv \
    TMPDIR=/data/tmp \
    ROW_BOT_SECRETS_DIR=/run/secrets

COPY --from=node /usr/local /usr/local
COPY --from=uv /uv /uvx /usr/local/bin/
COPY --from=builder /opt/row-bot/.venv /opt/row-bot/.venv
COPY --from=builder /opt/row-bot/playwright-browsers /opt/row-bot/playwright-browsers
COPY --from=builder /opt/row-bot/build-metadata.txt /opt/row-bot/build-metadata.txt

# Expose the locked image-bundled Chromium through the conventional executable
# name used by Kaleido and other local browser consumers. Row-Bot's browser
# automation continues to use the exact Playwright-managed executable directly.
RUN chromium_path="$(find /opt/row-bot/playwright-browsers -type f \
        \( -path '*/chrome-linux/chrome' -o -path '*/chrome-linux64/chrome' \) \
        -print -quit)" \
    && test -n "${chromium_path}" \
    && ln -s "${chromium_path}" /usr/local/bin/google-chrome

# Playwright supplies the native dependency list appropriate to its locked
# revision. The explicit packages cover Row-Bot's media, OpenCV, local audio
# imports, and useful international fonts in the same runtime layer.
RUN python -m playwright install-deps chromium \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        fontconfig \
        fonts-noto-core \
        fonts-noto-color-emoji \
        libgl1 \
        libglib2.0-0 \
        libportaudio2 \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid "${ROW_BOT_GID}" rowbot \
    && useradd --uid "${ROW_BOT_UID}" --gid "${ROW_BOT_GID}" \
        --home-dir /home/rowbot --create-home --shell /usr/sbin/nologin rowbot \
    && install -d -o "${ROW_BOT_UID}" -g "${ROW_BOT_GID}" -m 0700 \
        /data \
        /data/cache \
        /data/cache/huggingface \
        /data/cache/sentence-transformers \
        /data/cache/torch \
        /data/cache/uv \
        /data/runtimes \
        /data/tmp \
        /run/secrets

COPY app.py row-bot.ico RELEASE_NOTES.md requirements.txt /opt/row-bot/app/
COPY static /opt/row-bot/app/static
COPY sounds /opt/row-bot/app/sounds
COPY bundled_skills /opt/row-bot/app/bundled_skills
COPY tool_guides /opt/row-bot/app/tool_guides
COPY scripts/verify_runtime_dependencies.py /opt/row-bot/app/scripts/verify_runtime_dependencies.py

# Fail the image build if any feature in the canonical aggregate set is not
# importable with the final native libraries installed.
RUN python /opt/row-bot/app/scripts/verify_runtime_dependencies.py all \
    && { \
        python --version; \
        uv --version; \
        node --version; \
        npm --version; \
        npx --version; \
    } >> /opt/row-bot/build-metadata.txt

WORKDIR /data
USER 10001:10001

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=3s --start-period=30s --retries=5 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).read()"]

ENTRYPOINT ["row-bot"]
CMD ["serve"]
