"""Control API: auth, reads, audited/idempotent commands, confirmations (R26)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from fastapi.testclient import TestClient

from app.api import InMemoryAuditSink, InMemoryControlPlane, create_app
from app.brokers.mock import MockBrokerAdapter, MockMarketConfig
from app.config.settings import AppSettings, ParametersFile, RiskConfig
from app.core.clock import SimulatedClock
from app.core.enums import AppMode, Environment, OrderType, Side
from app.models import OrderRequest
from app.risk import KillSwitch, RiskManager

_NOW = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)
_TOKEN = "test-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}


def _build(
    settings: AppSettings | None = None,
) -> tuple[TestClient, InMemoryAuditSink, MockBrokerAdapter]:
    settings = settings or AppSettings(api_auth_token=_TOKEN)  # type: ignore[arg-type]
    clock = SimulatedClock(_NOW)
    broker = MockBrokerAdapter(clock, MockMarketConfig())
    risk = RiskManager(RiskConfig(), KillSwitch(clock))
    control = InMemoryControlPlane(settings, broker, risk)
    audit = InMemoryAuditSink(clock=clock)
    client = TestClient(create_app(settings, control=control, audit=audit, clock=clock))
    return client, audit, broker


def test_health_is_public_but_status_needs_auth() -> None:
    client, _, _ = _build()
    assert client.get("/health").status_code == 200
    assert client.get("/status").status_code == 401  # token configured, none sent
    assert client.get("/status", headers=_AUTH).status_code == 200


def test_reads_return_broker_state() -> None:
    client, _, broker = _build()
    import asyncio

    async def _seed() -> None:
        await broker.connect()
        await broker.place_order(
            OrderRequest(
                client_order_id="x",
                instrument_symbol=broker.future_symbol,
                side=Side.BUY,
                quantity=Decimal("2"),
                order_type=OrderType.MARKET,
            )
        )

    asyncio.run(_seed())
    positions = client.get("/positions", headers=_AUTH).json()
    assert any(p["symbol"] == broker.future_symbol for p in positions)
    fills = client.get("/fills", headers=_AUTH).json()
    assert len(fills) == 1


def test_command_is_audited() -> None:
    client, audit, _ = _build()
    resp = client.post("/strategy/stop", headers=_AUTH)
    assert resp.status_code == 200
    assert [r.action for r in audit.list()] == ["strategy.stop"]


def test_idempotency_key_dedupes_commands() -> None:
    client, audit, _ = _build()
    headers = {**_AUTH, "Idempotency-Key": "abc"}
    client.post("/strategy/pause", headers=headers)
    client.post("/strategy/pause", headers=headers)  # same key -> no second action
    assert [r.action for r in audit.list()] == ["strategy.pause"]


def test_kill_switch_requires_confirmation() -> None:
    client, _, _ = _build()
    assert client.post("/kill-switch", headers=_AUTH, json={"confirm": False}).status_code == 400
    ok = client.post("/kill-switch", headers=_AUTH, json={"confirm": True, "reason": "test"})
    assert ok.status_code == 200
    assert ok.json()["tripped"] is True
    # After tripping, the risk view reflects it.
    assert client.get("/risk", headers=_AUTH).json()["kill_switch_tripped"] is True


def test_start_in_backtest_mode_runs() -> None:
    client, _, _ = _build()
    resp = client.post("/strategy/start", headers=_AUTH, json={})
    assert resp.status_code == 200
    assert resp.json()["run_state"] == "running"


def test_live_start_blocked_without_gates() -> None:
    # Live mode but gates unmet -> 403.
    settings = AppSettings(
        api_auth_token=_TOKEN,
        app_mode=AppMode.LIVE,  # type: ignore[arg-type]
        app_environment=Environment.PRODUCTION,
        live_trading_enabled=False,
    )
    client, _, _ = _build(settings)
    resp = client.post("/strategy/start", headers=_AUTH, json={})
    assert resp.status_code == 403


def test_live_start_requires_confirmation_code() -> None:
    settings = AppSettings(
        api_auth_token=_TOKEN,
        app_mode=AppMode.LIVE,  # type: ignore[arg-type]
        app_environment=Environment.PRODUCTION,
        live_trading_enabled=True,
        live_confirmation_code="the-code",  # type: ignore[arg-type]
    )
    settings.params = ParametersFile()
    settings.params.trading.live_trading_enabled = True
    settings.params.risk = RiskConfig(
        maximum_daily_loss=Decimal("1000"),
        maximum_drawdown=Decimal("2000"),
        maximum_margin_utilization=Decimal("0.5"),
        maximum_cash_delta=Decimal("50000"),
        maximum_gamma=Decimal("500"),
        maximum_vega=Decimal("1000"),
        maximum_futures_position=10,
        stale_market_data_seconds=Decimal("5"),
        maximum_single_order_size=5,
        maximum_orders_per_minute=20,
    )
    client, _, _ = _build(settings)
    # Wrong / missing code -> 400; correct code -> running.
    assert client.post("/strategy/start", headers=_AUTH, json={}).status_code == 400
    ok = client.post("/strategy/start", headers=_AUTH, json={"confirmation_code": "the-code"})
    assert ok.status_code == 200
    assert ok.json()["run_state"] == "running"
