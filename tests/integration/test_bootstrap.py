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


def test_lifespan_starts_and_stops_orchestrator_cleanly() -> None:
    # autostart launches the orchestrator as a background task; entering and
    # exiting the lifespan (TestClient context) must start and cancel it without
    # error. run-state is STOPPED so it just idles over the stream briefly.
    app = build_application(AppSettings(api_auth_token="t"), autostart_orchestrator=True)  # type: ignore[arg-type]
    with TestClient(app.api) as client:
        assert client.get("/health").status_code == 200
    # Clean shutdown (no exception raised on lifespan exit) is the assertion.
