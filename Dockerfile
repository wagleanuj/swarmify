# Stage 1: build the web skin
FROM node:22-bookworm-slim AS web
RUN corepack enable && corepack prepare pnpm@12.15.0 --activate
WORKDIR /src
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/web/package.json apps/web/
RUN pnpm install --frozen-lockfile
COPY apps/web apps/web
RUN pnpm --filter web build

# Stage 2: Remotion's Chrome Headless Shell, downloaded once by the Remotion CLI itself
FROM node:22-bookworm-slim AS chrome
WORKDIR /remotion
RUN npm init -y >/dev/null && npm install --no-fund --no-audit @remotion/cli@4.0.538 \
  && npx remotion browser ensure \
  && mv node_modules/.remotion/chrome-headless-shell /chrome-headless-shell

# Stage 3: the api, serving the built skin at /
# uv's managed Python, not Debian's: DBOS needs SQLite 3.42 or later (`unixepoch('subsec')`) and
# bookworm ships 3.40. uv and Python are mise.toml's versions.
FROM debian:bookworm-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /uvx /usr/local/bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON=3.12.15 UV_PYTHON_PREFERENCE=only-managed \
    UV_PYTHON_INSTALL_DIR=/opt/python PATH="/app/.venv/bin:$PATH"
# Chrome's shared libraries, as Remotion's Docker guide lists them for Debian; bubblewrap and
# socat for the sandbox of the remotion_build stage (the Agent SDK's for its Bash, ours for the
# render)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libdbus-1-3 libatk1.0-0 libgbm-dev libasound2 libxrandr2 libxkbcommon-dev \
    libxfixes3 libxcomposite1 libxdamage1 libatk-bridge2.0-0 libpango-1.0-0 libcairo2 libcups2 \
    fonts-noto-core fonts-noto-color-emoji \
    bubblewrap socat \
  && rm -rf /var/lib/apt/lists/*
# Node and npx for the Remotion render connector (`npx remotion render`)
COPY --from=web /usr/local/bin/node /usr/local/bin/node
COPY --from=web /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
  && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
# On PATH, the render connector hands it to Remotion as --browser-executable. The binary is
# chrome-headless-shell on amd64 and headless_shell on arm64.
COPY --from=chrome /chrome-headless-shell /opt/chrome-headless-shell
RUN ln -s "$(find /opt/chrome-headless-shell -type f \( -name chrome-headless-shell -o -name headless_shell \))" \
    /usr/local/bin/chrome-headless-shell \
  && chrome-headless-shell --version
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY packages packages
COPY apps/api apps/api
RUN uv sync --locked --no-dev --all-packages \
  && python -c "import sqlite3; assert sqlite3.sqlite_version_info >= (3, 42), sqlite3.sqlite_version"
# The Claude Code CLI the Agent SDK bundles, on PATH for graphify's claude-cli backend
RUN ln -s "$(python -c 'import claude_agent_sdk, pathlib; print(pathlib.Path(claude_agent_sdk.__file__).parent / "_bundled/claude")')" \
    /usr/local/bin/claude \
  && claude --version
COPY --from=web /src/apps/web/dist apps/web/dist
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')"
CMD ["uvicorn", "--factory", "swarmify_api.main:create_app", "--host", "0.0.0.0", "--port", "8000"]
