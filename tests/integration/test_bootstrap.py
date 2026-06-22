"""Composition root + FastAPI lifespan wiring."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.bootstrap import build_application
from app.config.settings import AppSettings


def test_build_application_wires_components() -> None:
    app = build_application(AppSettings(api_auth_token="t"))  # type: ignore[arg-type]
    assert app.orchestrator is not None
    assert app.control is not None
    # The API is live and serving the public probe.
    with TestClient(app.api) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/metrics").status_code == 200


def test_telegram_channel_wired_when_configured() -> None:
    # With a bot token + chat id present, the Telegram channel must be attached
    # to the notifier so sandbox fills/hedges actually reach the chat.
    app = build_application(
        AppSettings(  # type: ignore[arg-type]
            api_auth_token="t",
            telegram_bot_token="123:AAtestToken",
            telegram_chat_id="42",
        )
    )
    channel_names = {c.name for c in app.orchestrator._notifier._channels}  # type: ignore[union-attr]
    assert "telegram" in channel_names
    # The owned HTTP client is closed cleanly on lifespan exit.
    with TestClient(app.api) as client:
        assert client.get("/health").status_code == 200


def test_telegram_channel_absent_when_unconfigured() -> None:
    app = build_application(AppSettings(api_auth_token="t"))  # type: ignore[arg-type]
    channel_names = {c.name for c in app.orchestrator._notifier._channels}  # type: ignore[union-attr]
    assert "telegram" not in channel_names


def test_lifespan_starts_and_stops_orchestrator_cleanly() -> None:
    # autostart launches the orchestrator as a background task; entering and
    # exiting the lifespan (TestClient context) must start and cancel it without
    # error. run-state is STOPPED so it just idles over the stream briefly.
    app = build_application(AppSettings(api_auth_token="t"), autostart_orchestrator=True)  # type: ignore[arg-type]
    with TestClient(app.api) as client:
        assert client.get("/health").status_code == 200
    # Clean shutdown (no exception raised on lifespan exit) is the assertion.
