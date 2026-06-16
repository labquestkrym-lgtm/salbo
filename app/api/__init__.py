"""FastAPI control plane (health/readiness first; trading endpoints later)."""

from app.api.app import create_app

__all__ = ["create_app"]
