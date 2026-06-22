"""Send a test message to the configured Telegram chat to verify delivery.

Turnkey check that the Telegram notification channel is wired correctly before
running the bot in sandbox — once this lands in your chat, real fills/hedges will
too (the orchestrator emits ``leg_filled`` / ``hedged`` through the same channel).

Usage (on your machine):
    # put your bot token + chat id in .env:
    #   TELEGRAM_BOT_TOKEN=123456:AA...
    #   TELEGRAM_CHAT_ID=123456789
    .venv\\Scripts\\python scripts/telegram_check.py

The token is read from the environment (.env); it is never passed on the CLI and
never logged (it lives only in the request URL, never in the message body).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx

# Allow running as a plain script (`python scripts/telegram_check.py`) without an
# editable install by putting the repo root on the import path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config.settings import load_settings
from app.core.logging import configure_logging, get_logger
from app.notifications import Notification, Severity, TelegramChannel

logger = get_logger("telegram_check")


async def main() -> None:
    configure_logging(json_output=False)
    settings = load_settings()  # reads .env (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)

    async with httpx.AsyncClient(timeout=10.0) as client:
        channel = TelegramChannel.from_settings(settings, client)
        if channel is None:
            logger.error(
                "telegram_not_configured",
                hint="set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env",
            )
            return
        await channel.send(
            Notification(
                "telegram_check",
                Severity.INFO,
                "Salbo: Telegram канал подключён. Сделки будут падать сюда.",
                {"mode": settings.app_mode.value},
            )
        )
        logger.info("telegram_check_sent", chat_id=settings.telegram_chat_id)


if __name__ == "__main__":
    asyncio.run(main())
