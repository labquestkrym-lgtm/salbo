# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first for better layer caching.
COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install ".[dev]"

# Copy source. (backtest/, paper/, workers/, migrations/ are added in later
# stages and will be appended here as they land.)
COPY app ./app
COPY configs ./configs

# Run as a non-root user.
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000

# Default command runs the wired control-plane API (broker + risk + control +
# metrics + orchestrator lifespan). Override for workers/backtest.
CMD ["uvicorn", "app.bootstrap:asgi", "--factory", "--host", "0.0.0.0", "--port", "8000"]
