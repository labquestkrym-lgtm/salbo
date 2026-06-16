"""Options pricing engine.

Pure ``float``/NumPy math (no I/O, no asyncio). Money-precision conversion to
``Decimal`` happens at the call boundary in higher layers (ADR-0001).

Public API:
    - :class:`Greeks`
    - :func:`bsm` — Black-Scholes-Merton for options on spot (dividend yield).
    - :func:`black76` — Black-76 for options on futures/forwards.
    - :func:`implied_volatility` — robust IV solver with typed guards.
"""

from app.pricing.greeks import Greeks
from app.pricing.implied_vol import implied_volatility
from app.pricing.models import black76, bsm

__all__ = ["Greeks", "black76", "bsm", "implied_volatility"]
