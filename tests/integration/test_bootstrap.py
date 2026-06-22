"""Composition root + FastAPI lifespan wiring."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.bootstrap import asgi, build_application
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


def test_tinkoff_broker_drives_options_on_futures_strategy() -> None:
    # Selecting the T-Invest broker must wire the orchestrator for the
    # options-on-futures (Black-76) straddle, with the underlying from YAML.
    app = build_application(
        AppSettings(  # type: ignore[arg-type]
            api_auth_token="t",
            broker_name="tinkoff",
            broker_api_key="t.dummy",
            broker_account_id="acc",
        )
    )
    assert app.broker.name == "tinkoff"
    assert app.orchestrator._cfg.options_on_futures is True
    assert app.orchestrator._cfg.symbol == app.settings.params.strategy.symbol
    # In dev with live gates unmet the adapter stays on the sandbox endpoint.
    assert app.broker._sandbox is True  # type: ignore[attr-defined]


def test_asgi_factory_loads_config_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The live runner is configured via CONFIG_PATH; without this the YAML risk
    # limits / mode are never loaded and live can never start.
    cfg = tmp_path / "params.yaml"
    cfg.write_text(
        "app:\n  environment: development\n  mode: paper\nrisk:\n  maximum_daily_loss: 12345\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONFIG_PATH", str(cfg))
    monkeypatch.setenv("API_AUTH_TOKEN", "tok")
    monkeypatch.setenv("AUTOSTART_ORCHESTRATOR", "false")
    api = asgi()
    with TestClient(api) as client:
        resp = client.get("/config", headers={"Authorization": "Bearer tok"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["mode"] == "paper"  # came from the YAML, not the default
        assert body["risk"]["maximum_daily_loss"] == "12345"


def test_lifespan_starts_and_stops_orchestrator_cleanly() -> None:
    # autostart launches the orchestrator as a background task; entering and
    # exiting the lifespan (TestClient context) must start and cancel it without
    # error. run-state is STOPPED so it just idles over the stream briefly.
    app = build_application(AppSettings(api_auth_token="t"), autostart_orchestrator=True)  # type: ignore[arg-type]
    with TestClient(app.api) as client:
        assert client.get("/health").status_code == 200
    # Clean shutdown (no exception raised on lifespan exit) is the assertion.
