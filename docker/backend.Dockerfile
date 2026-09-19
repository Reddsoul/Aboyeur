# Backend image: the aboyeur package + backend/ FastAPI app, one uvicorn worker.
# Build context is the repo root (compose sets it), so both packages and the
# Macro Kit component library under aboyeur/data ship in the image.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder
WORKDIR /app
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PROJECT_ENVIRONMENT=/opt/venv
COPY pyproject.toml uv.lock ./
# Dependencies first (cached layer), project second.
RUN uv sync --frozen --no-dev --no-install-project
COPY aboyeur ./aboyeur
COPY backend ./backend
COPY legal ./legal
RUN uv sync --frozen --no-dev

FROM python:3.12-slim-bookworm
WORKDIR /app
ENV PATH=/opt/venv/bin:$PATH PYTHONPATH=/app PYTHONUNBUFFERED=1
RUN useradd --create-home --uid 10001 app && mkdir -p /data /corpus && chown app /data
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app /app
USER app
EXPOSE 8000
# Single worker on purpose: one shared SQLite file, one process (aboyeur/db.py).
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
