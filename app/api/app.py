"""FastAPI application factory.

Stage 2 exposes liveness/readiness/status. Trading endpoints (positions, orders,
greeks, kill-switch, …) are added in later stages and will require auth + audit.
The ``/status`` response always advertises whether live trading is permitted so
operators can see the safety state at a glance.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from app import __version__
from app.config.settings import AppSettings, load_settings
from app.core.logging import configure_logging, get_logger

logger = get_logger(__name__)


def create_app(settings: AppSettings | None = None) -> FastAPI:
    settings = settings or load_settings()
    configure_logging()
    app = FastAPI(title="trading-bot", version=__version__)

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Liveness: the process is up."""
        return {"status": "ok", "version": __version__}

    @app.get("/ready")
    async def ready() -> dict[str, Any]:
        """Readiness: configuration is valid and the mode is known.

        (Broker/DB connectivity checks are wired in once those services exist.)
        """
        return {
            "ready": True,
            "mode": settings.app_mode.value,
            "environment": settings.app_environment.value,
        }

    @app.get("/status")
    async def status() -> dict[str, Any]:
        """Operational status incl. the live-trading safety gate."""
        return {
            "version": __version__,
            "mode": settings.app_mode.value,
            "environment": settings.app_environment.value,
            "live_trading_allowed": settings.is_live_trading_allowed(),
            "live_trading_blockers": settings.live_trading_blockers(),
        }

    logger.info(
        "api_started",
        mode=settings.app_mode.value,
        environment=settings.app_environment.value,
        live_trading_allowed=settings.is_live_trading_allowed(),
    )
    return app
