"""T-Invest (T-Bank / Tinkoff Investments) broker integration.

The pure conversion/mapping layer (:mod:`conversions`) is fully unit-tested and
backend-agnostic. The networked :class:`TInvestBrokerAdapter` lazily imports the
``tinkoff-investments`` SDK and is NOT exercised in CI — it requires a token and
network access; validate field names against your installed SDK version before
going to sandbox. Live trading stays gated (ADR-0003).
"""

from app.brokers.tinkoff.adapter import TInvestBrokerAdapter
from app.brokers.tinkoff.conversions import (
    decimal_to_quotation,
    money_to_decimal,
    order_status_to_state,
    quotation_to_decimal,
    side_to_direction,
)

__all__ = [
    "TInvestBrokerAdapter",
    "decimal_to_quotation",
    "money_to_decimal",
    "order_status_to_state",
    "quotation_to_decimal",
    "side_to_direction",
]
