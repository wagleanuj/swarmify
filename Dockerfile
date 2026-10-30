# The web skin, built and served by Caddy in the `web` service (compose.yaml), so a UI-only
# change rebuilds only that image and never restarts the api and its runs.
FROM node:22-bookworm-slim AS web-build
RUN corepack enable && corepack prepare pnpm@12.15.0 --activate
WORKDIR /src
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/web/package.json apps/web/
COPY packages/connectors/sandbox/package.json packages/connectors/sandbox/
RUN pnpm install --frozen-lockfile --filter web
COPY apps/web apps/web
RUN pnpm --filter web build

# The connector sandbox's launcher and Anthropic's sandbox-runtime it runs on (ADR 0021), alone.
FROM node:22-bookworm-slim AS sandbox-build
RUN corepack enable && corepack prepare pnpm@12.15.0 --activate
WORKDIR /src
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/web/package.json apps/web/
COPY packages/connectors/sandbox/package.json packages/connectors/sandbox/
RUN pnpm install --frozen-lockfile --prod --filter swarmify-sandbox

FROM caddy:2.10.2-alpine AS web
COPY docker/Caddyfile /etc/caddy/Caddyfile
COPY --from=web-build /src/apps/web/dist /srv

# The api (and graph-writer): the default target, so it stays last. No web skin in it.
# uv's managed Python, not Debian's: DBOS needs SQLite 3.42 or later (`unixepoch('subsec')`) and
# bookworm ships 3.40. uv and Python are mise.toml's versions.
FROM debian:bookworm-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /uvx /usr/local/bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON=3.12.15 UV_PYTHON_PREFERENCE=only-managed \
    UV_PYTHON_INSTALL_DIR=/opt/python PATH="/app/.venv/bin:$PATH"
# The shared libraries and fonts a package's headless browser needs (a package installs the
# browser itself, ADR 0026); bubblewrap, socat and ripgrep for the OS sandbox every connector
# process and a Build Session's Bash run in (ADRs 0018, 0021), which the `sandbox` service of
# compose.yaml starts; the font cache made now, which a sandboxed browser cannot write
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libdbus-1-3 libatk1.0-0 libgbm-dev libasound2 libxrandr2 libxkbcommon-dev \
    libxfixes3 libxcomposite1 libxdamage1 libatk-bridge2.0-0 libpango-1.0-0 libcairo2 libcups2 \
    fonts-noto-core fonts-noto-color-emoji fontconfig \
    bubblewrap socat ripgrep \
  && rm -rf /var/lib/apt/lists/* \
  && fc-cache -f
# Node and npm, for the packages that install with them (ADR 0026)
COPY --from=node:22-bookworm-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:22-bookworm-slim /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
  && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
  && node --version && npm --version
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY packages packages
COPY apps/api apps/api
# The default Connector Packages, installed from here into /data/packages on startup (ADR 0023)
COPY connectors connectors
COPY --from=sandbox-build /src/node_modules node_modules
COPY --from=sandbox-build /src/packages/connectors/sandbox/node_modules packages/connectors/sandbox/node_modules
COPY docker/needs.sh docker/claude-in-runner docker/
RUN uv sync --locked --no-dev --all-packages \
  && python -c "import sqlite3; assert sqlite3.sqlite_version_info >= (3, 42), sqlite3.sqlite_version"
# The Claude Code CLI the Agent SDK bundles, on PATH for graphify's claude-cli backend
RUN ln -s "$(python -c 'import claude_agent_sdk, pathlib; print(pathlib.Path(claude_agent_sdk.__file__).parent / "_bundled/claude")')" \
    /usr/local/bin/claude \
  && claude --version
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')"
CMD ["uvicorn", "--factory", "swarmify_api.main:create_app", "--host", "0.0.0.0", "--port", "8000"]
