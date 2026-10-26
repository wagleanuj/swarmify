# Stage 1: build the web skin
FROM node:22-bookworm-slim AS web
RUN corepack enable && corepack prepare pnpm@12.15.0 --activate
WORKDIR /src
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY apps/web/package.json apps/web/
RUN pnpm install --frozen-lockfile
COPY apps/web apps/web
RUN pnpm --filter web build

# Stage 2: the api, serving the built skin at /
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY packages packages
COPY apps/api apps/api
RUN uv sync --locked --no-dev --all-packages
COPY --from=web /src/apps/web/dist apps/web/dist
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health')"
CMD ["uvicorn", "swarmify_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
