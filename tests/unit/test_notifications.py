"""NotificationService: fan-out, secret redaction, channel isolation (R27)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.config.settings import AppSettings
from app.notifications import (
    CollectingChannel,
    EmailChannel,
    Notification,
    NotificationService,
    Severity,
    TelegramChannel,
)


async def test_fan_out_to_all_channels() -> None:
    a, b = CollectingChannel(), CollectingChannel()
    svc = NotificationService([a, b])
    await svc.started(mode="paper")
    assert a.sent[0].event == "started"
    assert b.sent[0].event == "started"


async def test_secrets_are_redacted_in_message_and_fields() -> None:
    ch = CollectingChannel()
    svc = NotificationService([ch])
    token = "AAbbCCddEEffGGhh1122334455667788xyz"  # long opaque token
    await svc.notify(
        Notification(
            "error",
            Severity.CRITICAL,
            f"failed with token {token}",
            {"api_key": token, "note": f"used {token}"},
        )
    )
    n = ch.sent[0]
    assert token not in n.message
    assert n.fields["api_key"] == "***REDACTED***"
    assert token not in n.fields["note"]


async def test_failing_channel_does_not_break_others() -> None:
    class Boom:
        name = "boom"

        async def send(self, notification: Notification) -> None:
            raise RuntimeError("down")

    good = CollectingChannel()
    svc = NotificationService([Boom(), good])
    await svc.kill_switch("manual_stop", "operator")
    assert good.sent[0].event == "kill_switch"  # delivered despite the failing channel


async def test_convenience_events_and_severity() -> None:
    ch = CollectingChannel()
    svc = NotificationService([ch])
    await svc.daily_pnl(Decimal("-1234.56"))
    await svc.data_loss("feed gap")
    assert ch.sent[0].fields["pnl"] == "-1234.56"
    assert ch.sent[1].severity is Severity.CRITICAL


async def test_telegram_keeps_token_out_of_body() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def post(self, url: str, *, json: dict[str, Any]) -> Any:
            self.calls.append((url, json))
            return None

    client = FakeClient()
    settings = AppSettings(telegram_bot_token="secret-bot-token", telegram_chat_id="123")  # type: ignore[arg-type]
    channel = TelegramChannel.from_settings(settings, client)
    assert channel is not None
    await channel.send(Notification("hedged", Severity.INFO, "Hedge executed", {"contracts": "2"}))
    url, body = client.calls[0]
    assert "secret-bot-token" in url  # token only in URL
    assert "secret-bot-token" not in str(body)
    assert body["chat_id"] == "123"


def test_telegram_disabled_when_unconfigured() -> None:
    assert TelegramChannel.from_settings(AppSettings(), client=None) is None  # type: ignore[arg-type]


def test_email_disabled_when_unconfigured() -> None:
    assert EmailChannel.from_settings(AppSettings()) is None


def test_render_includes_severity_and_fields() -> None:
    n = Notification("hedged", Severity.INFO, "Hedge executed", {"contracts": "2"})
    rendered = n.render()
    assert "[INFO]" in rendered
    assert "contracts=2" in rendered
