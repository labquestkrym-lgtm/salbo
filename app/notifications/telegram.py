"""Telegram notification channel (R27).

The bot token lives only in the request URL, never in the message body or logs.
The HTTP client is injected (an ``httpx.AsyncClient`` in production), which also
keeps the channel testable without network access.
"""

from __future__ import annotations

from typing import Any, Protocol

from app.core.logging import get_logger
from app.notifications.service import Notification

logger = get_logger(__name__)


class AsyncPoster(Protocol):
    async def post(self, url: str, *, json: dict[str, Any]) -> Any: ...


class TelegramChannel:
    name = "telegram"

    def __init__(self, token: str, chat_id: str, client: AsyncPoster) -> None:
        self._token = token
        self._chat_id = chat_id
        self._client = client

    @property
    def _url(self) -> str:
        return f"https://api.telegram.org/bot{self._token}/sendMessage"

    async def send(self, notification: Notification) -> None:
        # Body carries only chat_id + the already-redacted text; never the token.
        response = await self._client.post(
            self._url, json={"chat_id": self._chat_id, "text": notification.render()}
        )
        # Surface a misconfigured token / chat_id (HTTP 4xx/5xx) so the
        # NotificationService logs it instead of silently dropping the message.
        # Guarded with getattr so test doubles returning ``None`` still work.
        raise_for_status = getattr(response, "raise_for_status", None)
        if callable(raise_for_status):
            raise_for_status()

    @classmethod
    def from_settings(cls, settings: Any, client: AsyncPoster) -> TelegramChannel | None:
        token = settings.telegram_bot_token
        chat = settings.telegram_chat_id
        if token is None or not token.get_secret_value() or not chat:
            logger.info("telegram_channel_disabled")
            return None
        return cls(token.get_secret_value(), chat, client)
