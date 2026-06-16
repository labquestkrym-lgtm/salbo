# ADR-0001: Decimal for money, float only inside the math kernel

- Status: Accepted
- Date: 2026-06-16

## Context
Binary floating point cannot represent decimal cash amounts exactly. But the
options math (BSM/Black-76, IV root finding, Greeks) is defined over reals and
relies on NumPy/SciPy, which are `float64`.

## Decision
- All money, prices, ticks, quantities, multipliers, margin and P&L use
  `decimal.Decimal`, persisted in `NUMERIC` columns.
- The pricing/volatility kernels operate on `float`. Conversion happens at an
  explicit boundary: inputs `Decimal → float` just before pricing; outputs
  `float → Decimal` quantized to the instrument's tick/precision.
- No arithmetic mixing of `Decimal` and `float` outside that boundary.

## Consequences
- Exact cash accounting and audit; reproducible P&L attribution.
- A small, well-tested conversion layer (`app/pricing/quantize.py`) is the only
  place the two worlds meet.
