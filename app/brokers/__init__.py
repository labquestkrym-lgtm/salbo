"""Broker adapters: universal interface + concrete implementations."""

from app.brokers.base import BaseBrokerAdapter, BrokerCapabilities
from app.brokers.mock import MockBrokerAdapter
from app.brokers.paper import PaperBrokerAdapter, PaperFillConfig

__all__ = [
    "BaseBrokerAdapter",
    "BrokerCapabilities",
    "MockBrokerAdapter",
    "PaperBrokerAdapter",
    "PaperFillConfig",
]
