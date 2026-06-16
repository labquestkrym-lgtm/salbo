"""Health/readiness/status endpoints, incl. the live-trading safety gate."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.api import create_app
from app.config.settings import AppSettings


def _client() -> TestClient:
    return TestClient(create_app(AppSettings()))


def test_health_ok() -> None:
    resp = _client().get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_ready_reports_mode() -> None:
    resp = _client().get("/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is True
    assert body["mode"] == "backtest"


def test_status_blocks_live_by_default() -> None:
    resp = _client().get("/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["live_trading_allowed"] is False
    assert body["live_trading_blockers"]  # non-empty list of reasons
