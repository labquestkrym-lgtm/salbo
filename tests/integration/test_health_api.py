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


def test_protected_endpoint_requires_auth() -> None:
    # /status needs a bearer token; default settings have no token configured.
    resp = _client().get("/status")
    assert resp.status_code == 503  # auth not configured
