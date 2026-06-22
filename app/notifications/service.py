"""Notification fan-out (R27).

Sends operational events to one or more channels. Every message (and every
field value) is passed through secret redaction before leaving the process, so
API keys / tokens are never transmitted (a hard requirement). A failing channel
never breaks the others or the caller — delivery is best-effort and isolated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from app.core.logging import get_logger, is_sensitive_key, redact_secrets

logger = get_logger(__name__)


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class Notification:
    event: str
    severity: Severity
    message: str
    fields: dict[str, str] = field(default_factory=dict)

    def redacted(self) -> Notification:
        safe_fields = {
            k: ("***REDACTED***" if is_sensitive_key(k) else redact_secrets(str(v)))
            for k, v in self.fields.items()
        }
        return Notification(
            event=self.event,
            severity=self.severity,
            message=redact_secrets(self.message),
            fields=safe_fields,
        )

    def render(self) -> str:
        head = f"[{self.severity.value.upper()}] {self.message}"
        if not self.fields:
            return head
        body = " ".join(f"{k}={v}" for k, v in self.fields.items())
        return f"{head} | {body}"


class NotificationChannel(Protocol):
    name: str

    async def send(self, notification: Notification) -> None: ...


class LogChannel:
    """Always-on channel that writes through the structured logger."""

    name = "log"

    def __init__(self) -> None:
        self._log = get_logger("notifications")

    async def send(self, notification: Notification) -> None:
        level = {
            Severity.INFO: self._log.info,
            Severity.WARNING: self._log.warning,
            Severity.CRITICAL: self._log.error,
        }[notification.severity]
        level(notification.event, message=notification.message, **notification.fields)


class CollectingChannel:
    """In-memory channel for tests."""

    name = "collecting"

    def __init__(self) -> None:
        self.sent: list[Notification] = []

    async def send(self, notification: Notification) -> None:
        self.sent.append(notification)


class NotificationService:
    def __init__(self, channels: list[NotificationChannel]) -> None:
        self._channels = channels

    async def notify(self, notification: Notification) -> None:
        safe = notification.redacted()
        for channel in self._channels:
            try:
                await channel.send(safe)
            except Exception as exc:
                logger.warning("notification_channel_failed", channel=channel.name, error=str(exc))

    # --- convenience events (R27 list) -------------------------------------
    async def started(self, mode: str) -> None:
        await self.notify(
            Notification("started", Severity.INFO, "Strategy started", {"mode": mode})
        )

    async def stopped(self, reason: str = "") -> None:
        await self.notify(
            Notification("stopped", Severity.INFO, "Strategy stopped", {"reason": reason})
        )

    async def leg_filled(self, symbol: str, quantity: str, price: str) -> None:
        await self.notify(
            Notification(
                "leg_filled",
                Severity.INFO,
                "Leg filled",
                {"symbol": symbol, "quantity": quantity, "price": price},
            )
        )

    async def hedged(self, symbol: str, side: str, contracts: int, price: str, reason: str) -> None:
        await self.notify(
            Notification(
                "hedged",
                Severity.INFO,
                "Hedge executed",
                {
                    "symbol": symbol,
                    "side": side,
                    "contracts": str(contracts),
                    "price": price,
                    "reason": reason,
                },
            )
        )

    async def position_closed(self, reason: str, legs: str) -> None:
        await self.notify(
            Notification(
                "position_closed",
                Severity.INFO,
                "Position closed",
                {"reason": reason, "legs": legs},
            )
        )

    async def limit_breach(self, detail: str) -> None:
        await self.notify(
            Notification("limit_breach", Severity.WARNING, "Risk limit breach", {"detail": detail})
        )

    async def data_loss(self, detail: str) -> None:
        await self.notify(
            Notification("data_loss", Severity.CRITICAL, "Market data lost", {"detail": detail})
        )

    async def desync(self, detail: str) -> None:
        await self.notify(
            Notification("desync", Severity.CRITICAL, "Position desync", {"detail": detail})
        )

    async def kill_switch(self, trigger: str, detail: str = "") -> None:
        await self.notify(
            Notification(
                "kill_switch",
                Severity.CRITICAL,
                f"Kill switch tripped: {trigger}",
                {"detail": detail},
            )
        )

    async def daily_pnl(self, pnl: Decimal) -> None:
        await self.notify(Notification("daily_pnl", Severity.INFO, "Daily P&L", {"pnl": str(pnl)}))

    async def error(self, detail: str) -> None:
        await self.notify(Notification("error", Severity.CRITICAL, "Error", {"detail": detail}))
