"""Composition root: wire the whole system into one process.

Builds the broker, risk manager / kill switch, control plane, metrics, audit and
the orchestration worker, then exposes a FastAPI app whose lifespan optionally
runs the orchestrator as a supervised background task. This is the single
in-process entry point (``uvicorn app.bootstrap:asgi --factory``).

Only the Mock broker is wired here; paper/sandbox/real adapters slot in by broker
name once their data feeds are configured. Live trading stays gated (ADR-0003).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
from fastapi import FastAPI

from app.api.app import create_app
from app.api.audit import InMemoryAuditSink
from app.api.control import InMemoryControlPlane
from app.brokers.mock import MockBrokerAdapter, MockMarketConfig
from app.config.settings import AppSettings, load_settings
from app.core.clock import SystemClock
from app.core.logging import get_logger
from app.notifications import (
    EmailChannel,
    LogChannel,
    NotificationChannel,
    NotificationService,
    TelegramChannel,
)
from app.observability import Metrics
from app.risk import KillSwitch, RiskManager
from workers import Orchestrator, OrchestratorConfig

logger = get_logger(__name__)


@dataclass(slots=True)
class Application:
    """Bundle of wired components (handy for tests and the ASGI factory)."""

    settings: AppSettings
    api: FastAPI
    control: InMemoryControlPlane
    metrics: Metrics
    orchestrator: Orchestrator


def build_application(
    settings: AppSettings | None = None,
    *,
    autostart_orchestrator: bool = False,
    notifier: NotificationService | None = None,
) -> Application:
    settings = settings or load_settings()
    clock = SystemClock()
    broker = MockBrokerAdapter(clock, MockMarketConfig())
    kill_switch = KillSwitch(clock, policy=settings.params.risk.kill_switch_policy)
    risk = RiskManager(settings.params.risk, kill_switch)
    control = InMemoryControlPlane(settings, broker, risk)
    metrics = Metrics()
    audit = InMemoryAuditSink(clock=clock)
    # Always log notifications; add email and Telegram when configured. Telegram
    # needs a long-lived HTTP client whose lifecycle we own (closed in lifespan).
    telegram_client: httpx.AsyncClient | None = None
    if notifier is None:
        channels: list[NotificationChannel] = [LogChannel()]
        email = EmailChannel.from_settings(settings)
        if email is not None:
            channels.append(email)
        telegram_client = httpx.AsyncClient(timeout=10.0)
        telegram = TelegramChannel.from_settings(settings, telegram_client)
        if telegram is not None:
            channels.append(telegram)
        notifier = NotificationService(channels)
    orchestrator = Orchestrator(
        clock, broker, risk, control, OrchestratorConfig(), metrics=metrics, notifier=notifier
    )

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        task: asyncio.Task[None] | None = None
        if autostart_orchestrator:
            logger.info("orchestrator_autostart")
            task = asyncio.create_task(orchestrator.run())
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if telegram_client is not None:
                await telegram_client.aclose()
            logger.info("application_shutdown")

    api = create_app(
        settings, control=control, audit=audit, clock=clock, metrics=metrics, lifespan=lifespan
    )
    return Application(
        settings=settings, api=api, control=control, metrics=metrics, orchestrator=orchestrator
    )


def asgi() -> FastAPI:
    """ASGI factory for ``uvicorn app.bootstrap:asgi --factory``."""
    return build_application().api
