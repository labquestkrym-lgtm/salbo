"""Structured JSON logging with secret redaction.

Never log tokens/secrets. A processor scrubs known-sensitive keys and any value
that looks like a long opaque token before rendering JSON.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import structlog

_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "api_secret",
        "secret",
        "password",
        "token",
        "auth_token",
        "bot_token",
        "confirmation_code",
        "authorization",
    }
)

# Heuristic: long base64/hex-ish opaque strings get masked even in free text.
_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_\-]{24,}\b")
_REDACTED = "***REDACTED***"


def redact_secrets(text: str) -> str:
    """Mask long opaque token-like substrings in free text. Reusable by any
    outbound channel (logs, notifications) so secrets never leave the process."""
    return _TOKEN_RE.sub(_REDACTED, text)


def is_sensitive_key(key: str) -> bool:
    return key.lower() in _SENSITIVE_KEYS


def _redact(_logger: object, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    for key in list(event_dict.keys()):
        if key.lower() in _SENSITIVE_KEYS:
            event_dict[key] = _REDACTED
    msg = event_dict.get("event")
    if isinstance(msg, str):
        event_dict["event"] = _TOKEN_RE.sub(_REDACTED, msg)
    return event_dict


def configure_logging(*, level: str = "INFO", json_output: bool = True) -> None:
    """Configure structlog + stdlib logging once at startup."""
    logging.basicConfig(format="%(message)s", level=getattr(logging, level.upper(), logging.INFO))

    # Quiet libraries that log request URLs at INFO — those URLs can embed
    # secrets (e.g. httpx logs the Telegram bot-token URL). Secrets must never
    # reach the logs.
    for noisy in ("httpx", "httpcore", "grpc", "tinkoff"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _redact,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processors.append(
        structlog.processors.JSONRenderer() if json_output else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
