"""Broker adapters: universal interface + concrete implementations."""

from app.brokers.base import BaseBrokerAdapter, BrokerCapabilities
from app.brokers.mock import MockBrokerAdapter

__all__ = ["BaseBrokerAdapter", "BrokerCapabilities", "MockBrokerAdapter"]
