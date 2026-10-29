# The Compose `chatterbox` profile, for hosts without an Apple GPU: CPU wheels, slow but whole.
# On a Mac run it natively instead (`mise run speech`) so it gets the GPU.
FROM python:3.12-slim
# OpenMP, which CTranslate2 (faster-whisper) loads
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
  && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /usr/local/bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy HF_HOME=/models
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project
COPY server.py wordmatch.py ./
EXPOSE 8881
CMD ["/app/.venv/bin/uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8881"]
