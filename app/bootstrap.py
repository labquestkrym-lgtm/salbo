"""Composition root: wire the whole system into one process.

Builds the broker, risk manager / kill switch, control plane, metrics, audit and
the orchestration worker, then exposes a FastAPI app whose lifespan optionally
runs the orchestrator as a supervised background task. This is the single
in-process entry point (``uvicorn app.bootstrap:asgi --factory``).

The broker is selected by ``BROKER_NAME`` (mock | tinkoff). The orchestration
loop is auto-started only for brokers whose data feed + instrument universe are
wired for the straddle strategy (currently the mock); other brokers still serve
the control plane (positions/quotes) against their endpoint. Live trading stays
gated (ADR-0003).
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
from app.brokers.base import BaseBrokerAdapter
from app.brokers.mock import MockBrokerAdapter, MockMarketConfig
from app.config.settings import AppSettings, load_settings
from app.core.clock import Clock, SystemClock
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


def _build_broker(settings: AppSettings, clock: Clock) -> tuple[BaseBrokerAdapter, bool]:
    """Return ``(broker, can_run_strategy)``. The second flag marks whether the
    orchestration loop can drive this broker.

    Mock: always (streaming feed + full option universe). T-Invest: only when ALL
    live gates pass — the sandbox exposes no options, so the straddle can only
    run against the (live-gated) production endpoint; sandbox/dev runs serve the
    control plane (positions/quotes) only."""
    name = settings.broker_name.lower()
    if name == "tinkoff":
        from app.brokers.tinkoff import TInvestBrokerAdapter

        live_ok = settings.is_live_trading_allowed()
        return TInvestBrokerAdapter(settings, clock, sandbox=not live_ok), live_ok
    return MockBrokerAdapter(clock, MockMarketConfig()), True


def _orchestrator_config(settings: AppSettings) -> OrchestratorConfig:
    """Per-broker orchestration config. T-Invest trades FORTS-style options on
    the future (Black-76); the strategy underlying comes from the YAML params."""
    strategy = settings.params.strategy
    if settings.broker_name.lower() == "tinkoff":
        return OrchestratorConfig(
            symbol=strategy.symbol,
            options_on_futures=True,
            entry_contracts=strategy.contracts,
            hedge_to_zero=strategy.hedge_to_zero,
        )
    return OrchestratorConfig()


@dataclass(slots=True)
class Application:
    """Bundle of wired components (handy for tests and the ASGI factory)."""

    settings: AppSettings
    api: FastAPI
    control: InMemoryControlPlane
    metrics: Metrics
    orchestrator: Orchestrator
    broker: BaseBrokerAdapter


def build_application(
    settings: AppSettings | None = None,
    *,
    autostart_orchestrator: bool = False,
    notifier: NotificationService | None = None,
) -> Application:
    settings = settings or load_settings()
    clock = SystemClock()
    broker, can_run_strategy = _build_broker(settings, clock)
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
        clock,
        broker,
        risk,
        control,
        _orchestrator_config(settings),
        metrics=metrics,
        notifier=notifier,
    )

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        task: asyncio.Task[None] | None = None
        if autostart_orchestrator and can_run_strategy:
            logger.info("orchestrator_autostart", broker=settings.broker_name)
            task = asyncio.create_task(orchestrator.run())
        elif autostart_orchestrator:
            logger.warning(
                "orchestrator_autostart_skipped",
                broker=settings.broker_name,
                reason="strategy loop not wired for this broker",
            )
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
        settings=settings,
        api=api,
        control=control,
        metrics=metrics,
        orchestrator=orchestrator,
        broker=broker,
    )


def asgi() -> FastAPI:
    """ASGI factory for ``uvicorn app.bootstrap:asgi --factory``."""
    return build_application().api
