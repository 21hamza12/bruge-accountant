# syntax=docker/dockerfile:1

FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.11.8 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Dependencies first so this layer is cached until pyproject.toml or uv.lock change.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
# Only what runs in production. No tests, docs, fixtures or dev tooling (see .dockerignore).
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
USER 10001:10001
EXPOSE 8000
# Shell form so ${PORT} expands (Render injects it); exec so uvicorn is PID 1 and gets SIGTERM.
# hadolint ignore=DL3025
CMD exec uvicorn app.web.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --no-server-header --workers 1
