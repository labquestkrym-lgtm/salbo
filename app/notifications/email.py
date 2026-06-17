"""Email notification channel (R27, additional channel).

A minimal SMTP channel using the stdlib, run off the event loop via a thread so
it never blocks. Credentials come from settings; they are never placed in the
message body. Disabled (``from_settings`` returns ``None``) unless host/from/to
are configured.
"""

from __future__ import annotations

import asyncio
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any

from app.core.logging import get_logger
from app.notifications.service import Notification

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class _SmtpConfig:
    host: str
    port: int
    username: str | None
    password: str | None
    sender: str
    recipient: str


class EmailChannel:
    name = "email"

    def __init__(self, config: _SmtpConfig) -> None:
        self._cfg = config

    async def send(self, notification: Notification) -> None:
        await asyncio.to_thread(self._send_sync, notification)

    def _send_sync(self, notification: Notification) -> None:
        msg = EmailMessage()
        msg["From"] = self._cfg.sender
        msg["To"] = self._cfg.recipient
        msg["Subject"] = f"[{notification.severity.value.upper()}] {notification.event}"
        msg.set_content(notification.render())
        with smtplib.SMTP(self._cfg.host, self._cfg.port, timeout=10) as smtp:
            smtp.starttls()
            if self._cfg.username and self._cfg.password:
                smtp.login(self._cfg.username, self._cfg.password)
            smtp.send_message(msg)

    @classmethod
    def from_settings(cls, settings: Any) -> EmailChannel | None:
        if not (settings.smtp_host and settings.smtp_from and settings.smtp_to):
            logger.info("email_channel_disabled")
            return None
        username = settings.smtp_username.get_secret_value() if settings.smtp_username else None
        password = settings.smtp_password.get_secret_value() if settings.smtp_password else None
        return cls(
            _SmtpConfig(
                host=settings.smtp_host,
                port=settings.smtp_port,
                username=username,
                password=password,
                sender=settings.smtp_from,
                recipient=settings.smtp_to,
            )
        )
