"""Shared test helpers."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure the project root is importable when pytest is run from anywhere.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@pytest.fixture(autouse=True)
def _isolate_settings_from_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests must be deterministic regardless of a developer's local ``.env``
    (which may hold real broker/Telegram secrets). Disable dotenv loading so
    ``AppSettings()`` uses only explicit kwargs + defaults during tests."""
    from app.config.settings import AppSettings

    monkeypatch.setitem(AppSettings.model_config, "env_file", None)
