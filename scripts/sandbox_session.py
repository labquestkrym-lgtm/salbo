"""Run the pairs portfolio in the sandbox for a full session + hourly Telegram report.

One self-contained process (no uvicorn — those background servers were dying): it
runs the MultiPairOrchestrator in-process against the T-Invest sandbox, sends trade
events to Telegram in real time (the orchestrator's notifier), and posts an hourly
portfolio summary (open pairs, per-pair z, open MTM, realized session P&L) to the
same chat.

    $env:PYTHONPATH = (Get-Location)
    .venv312\\Scripts\\python scripts/sandbox_session.py
"""

from __future__ import annotations

# ruff: noqa: RUF001 — this script sends Russian (Cyrillic) Telegram messages.
import asyncio
import os
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from app.api.control import InMemoryControlPlane
from app.bootstrap import _multi_pairs_config
from app.brokers.tinkoff import TInvestBrokerAdapter
from app.config.settings import load_settings
from app.core.clock import SystemClock
from app.core.logging import configure_logging, get_logger
from app.notifications import LogChannel, NotificationChannel, NotificationService, TelegramChannel
from app.risk import KillSwitch, RiskManager
from workers import MultiPairOrchestrator

logger = get_logger("sandbox_session")
_REPORT_EVERY_S = int(os.environ.get("REPORT_EVERY_S", "3600"))
_ACCOUNT = os.environ.get("BROKER_ACCOUNT_ID", "fb57644f-beb2-4b17-b082-2428a7c1a5f3")


async def _tg(client: httpx.AsyncClient, token: str, chat: str, text: str) -> None:
    try:
        await client.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text},
        )
    except Exception as exc:
        logger.warning("telegram_send_failed", error=str(exc))


def _summary(snap: dict[str, Any], cash: Decimal, start_cash: Decimal) -> str:
    now = datetime.now(UTC)
    pairs = snap.get("pairs", {})
    lines = [f"📊 Песочница {((now.hour + 3) % 24):02d}:{now.minute:02d} МСК"]
    for name, p in sorted(pairs.items()):
        flag = "🟢" if p.get("opened") else "⚪"
        lines.append(f"{flag} {name}: z={float(p.get('pair_z', 0)):+.2f} pnl={p.get('pair_pnl', 0)}")
    realized = cash - start_cash
    lines.append(f"открыто пар: {snap.get('open_pairs', 0)} | откр. MTM: {snap.get('total_pair_pnl', 0)} ₽")
    lines.append(f"кэш {cash:.0f} ₽ | реализовано за сессию: {realized:+.0f} ₽")
    return "\n".join(lines)


async def _reporter(
    client: httpx.AsyncClient, token: str, chat: str,
    control: InMemoryControlPlane, broker: TInvestBrokerAdapter, start_cash: Decimal,
) -> None:
    while True:
        await asyncio.sleep(_REPORT_EVERY_S)
        try:
            cash_list = await broker.get_cash()
            cash = next((c.cash for c in cash_list if c.currency == "RUB"), Decimal("0"))
            await _tg(client, token, chat, _summary(control._greeks_snapshot, cash, start_cash))
        except Exception as exc:
            logger.warning("hourly_report_failed", error=str(exc))


async def main() -> None:
    configure_logging(json_output=False)
    settings = load_settings(os.environ.get("CONFIG_PATH", "configs/pairs-paper.yaml"))
    clock = SystemClock()
    broker = TInvestBrokerAdapter(settings, clock, sandbox=True, account_id=_ACCOUNT)
    risk = RiskManager(settings.params.risk, KillSwitch(clock))
    control = InMemoryControlPlane(settings, broker, risk)

    client = httpx.AsyncClient(timeout=10.0)
    channels: list[NotificationChannel] = [LogChannel()]
    tg = TelegramChannel.from_settings(settings, client)
    token = settings.telegram_bot_token.get_secret_value() if settings.telegram_bot_token else ""
    chat = settings.telegram_chat_id or ""
    if tg is not None:
        channels.append(tg)
    notifier = NotificationService(channels)

    orch = MultiPairOrchestrator(
        clock, broker, risk, control, _multi_pairs_config(settings), notifier=notifier
    )
    await broker.connect()
    start_cash = next(
        (c.cash for c in await broker.get_cash() if c.currency == "RUB"), Decimal("0")
    )
    await control.start(confirmation_code=None)  # paper -> RUNNING
    await _tg(
        client, token, chat,
        f"▶️ Старт мониторинга песочницы: {len(orch.subs)} пар, "
        f"кэш {start_cash:.0f} ₽. Сводка раз в {_REPORT_EVERY_S // 60} мин.",
    )
    try:
        await asyncio.gather(orch.run(), _reporter(client, token, chat, control, broker, start_cash))
    finally:
        await client.aclose()
        await broker.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
