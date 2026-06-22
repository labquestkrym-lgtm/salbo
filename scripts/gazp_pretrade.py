"""PRE-TRADE snapshot for the GAZP 1-lot manual test (READ-ONLY).

Shows exactly what you should see BEFORE placing a single manual order in the
T-Invest app, so the 1-lot test is controlled:

* the resolved ATM straddle (nearest expiry in the configured DTE window),
* live quotes for the future and both option legs,
* the bot's Black-76 greeks for a 1-lot long straddle (IV backed out of the
  market mid), and
* the EXPECTED cash charge if you BUY 1 call at the ask — this is the
  contract-size test: a charge near (ask x 100) confirms 1 contract = 100
  shares; a charge near the ask alone would mean 1 share.

Places NO orders. Run it, eyeball the numbers, then buy 1 lot in the app.

    $env:PYTHONPATH = (Get-Location)
    .venv312\\Scripts\\python scripts/gazp_pretrade.py
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from decimal import Decimal

from app.brokers.base import BaseBrokerAdapter
from app.brokers.tinkoff import TInvestBrokerAdapter
from app.config.settings import load_settings
from app.core.clock import SystemClock
from app.core.enums import AssetClass
from app.core.logging import configure_logging, get_logger
from app.instruments import InstrumentResolver
from app.models import Instrument, Position
from app.portfolio import PortfolioGreeksEngine, PricingInputs, UnderlyingState
from app.pricing import implied_volatility

logger = get_logger("gazp_pretrade")

_RATE = 0.18  # ~RU key-rate ballpark, for discounting only (Black-76 carry b=0)
_MIN_DTE, _MAX_DTE = 10, 45


def _mid(q: object) -> Decimal | None:
    bid, ask = getattr(q, "bid", None), getattr(q, "ask", None)
    if bid and ask:
        return (bid + ask) / 2
    return ask or bid


async def _load_chain(a: BaseBrokerAdapter, sym: str, retries: int = 4) -> list[Instrument]:
    """list_instruments, retried — the options_by call transiently returns
    "Stream removed", yielding an empty (futures-only) universe."""
    for attempt in range(retries):
        insts = await a.list_instruments(sym)
        if any(i.asset_class is AssetClass.OPTION for i in insts):
            return insts
        logger.warning("empty_option_chain_retry", attempt=attempt + 1)
        await asyncio.sleep(1.0)
    raise RuntimeError(f"no option chain for {sym} after {retries} attempts (transient gRPC?)")


async def main() -> None:
    configure_logging(json_output=False)
    # Load the strategy params (symbol etc.) from the YAML — without this the
    # symbol is the "PLACEHOLDER" default. Override with CONFIG_PATH if needed.
    settings = load_settings(os.environ.get("CONFIG_PATH", "configs/production.yaml"))
    sym = settings.params.strategy.symbol
    a = TInvestBrokerAdapter(settings, SystemClock(), sandbox=False)
    await a.connect()
    try:
        insts = await _load_chain(a, sym)
        r = InstrumentResolver(insts)
        today = SystemClock().now().date()
        exps = r.expiries(sym)
        win = [e for e in exps if _MIN_DTE <= (e - today).days <= _MAX_DTE]
        exp = min(win) if win else exps[0]
        fut = r.nearest_future(sym, on_or_after=exp)
        scale = fut.spec.quote_scale
        qf = await a.get_quote(fut.symbol)
        forward = float((_mid(qf) or Decimal(0)) / scale)
        strikes = r.strikes(sym, exp)
        strike = min(strikes, key=lambda k: abs(k - Decimal(str(forward))))
        st = r.resolve_straddle_on_future(sym, expiry=exp, strike=strike)
        mult = float(st.call.spec.multiplier)
        qc, qp = await a.get_quote(st.call.symbol), await a.get_quote(st.put.symbol)

        print(f"\n=== {sym} 1-LOT PRE-TRADE SNAPSHOT ===")
        print(f"expiry {exp} ({(exp - today).days} DTE) | per-unit forward {forward:.2f} "
              f"| ATM strike {strike} | contract multiplier {mult:g}")
        print(f"future {fut.symbol}  bid={qf.bid} ask={qf.ask}  (quote_scale {scale})")
        print(f"CALL   {st.call.symbol}  bid={qc.bid} ask={qc.ask}")
        print(f"PUT    {st.put.symbol}  bid={qp.bid} ask={qp.ask}")

        # Back out IV per leg from the market mid (Black-76, underlying = forward).
        sigmas: dict[str, float] = {}
        for leg, q in ((st.call, qc), (st.put, qp)):
            mid = _mid(q)
            tau = (datetime.combine(exp, datetime.min.time(), tzinfo=UTC)
                   - datetime.now(UTC)).total_seconds() / (365 * 24 * 3600)
            try:
                iv = implied_volatility(price=float(mid), underlying=forward,
                                        strike=float(leg.strike), t=tau, rate=_RATE,
                                        option_type=leg.option_type, model="black76")
            except Exception:
                iv = 0.30
            sigmas[leg.symbol] = iv
        print(f"backed-out IV: call {sigmas[st.call.symbol] * 100:.1f}%  "
              f"put {sigmas[st.put.symbol] * 100:.1f}%")

        eng = PortfolioGreeksEngine({i.symbol: i for i in insts})
        inp = PricingInputs(
            valuation_time=datetime.now(UTC),
            underlying=UnderlyingState(spot=forward, rate=_RATE, dividend_yield=0.0),
            sigma_by_symbol=sigmas, mid_by_symbol={}, future_multiplier=mult,
        )
        g = eng.compute(
            [Position(instrument_symbol=st.call.symbol, quantity=Decimal("1")),
             Position(instrument_symbol=st.put.symbol, quantity=Decimal("1"))], inp)
        print(f"\n1-lot LONG STRADDLE greeks: net_delta_units={g.net_delta_units:.2f} "
              f"gamma={g.net_gamma_units:.4f} vega/pct={float(g.net_vega_per_pct):.1f} "
              f"theta/day={float(g.net_theta_per_day):.1f}")
        print(f"straddle cash_delta={float(g.cash_delta):.0f} RUB")

        call_ask = float(qc.ask or 0)
        print("\n--- CONTRACT-SIZE TEST -------------------------------------------")
        print(f"If you BUY 1 {sym} CALL strike {strike} exp {exp} at ask {call_ask}:")
        print(f"  expected debit if 1 contract = {mult:g} shares : ~{call_ask * mult:.2f} RUB")
        print(f"  (if instead only ~{call_ask:.2f} RUB is debited, 1 contract = 1 share)")
        print("Place that single BUY in the T-Invest app, then run scripts/gazp_verify.py.")
    finally:
        await a.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
