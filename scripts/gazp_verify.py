"""POST-TRADE verification for the GAZP 1-lot manual test (READ-ONLY).

Run AFTER you have bought a single option lot in the T-Invest app. Confirms the
contract-size assumption and that the bot reads the live account correctly —
exercising get_positions and get_fills (until now unverified on the network):

* lists current positions and recent fills (side / qty / price),
* recomputes the bot's greeks for whatever GAZP options you now hold, and
* states what the actual app debit should have been under the 100-shares model.

Places NO orders. After verifying, close the position manually in the app.

    $env:PYTHONPATH = (Get-Location)
    .venv312\\Scripts\\python scripts/gazp_verify.py
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
from app.models import Instrument
from app.portfolio import PortfolioGreeksEngine, PricingInputs, UnderlyingState
from app.pricing import implied_volatility

logger = get_logger("gazp_verify")
_RATE = 0.18


def _mid(q: object) -> Decimal | None:
    bid, ask = getattr(q, "bid", None), getattr(q, "ask", None)
    if bid and ask:
        return (bid + ask) / 2
    return ask or bid


async def _load_chain(a: BaseBrokerAdapter, sym: str, retries: int = 4) -> list[Instrument]:
    """list_instruments, retried past the transient empty-chain ("Stream removed")."""
    insts: list[Instrument] = []
    for attempt in range(retries):
        insts = await a.list_instruments(sym)
        if any(i.asset_class is AssetClass.OPTION for i in insts):
            return insts
        logger.warning("empty_option_chain_retry", attempt=attempt + 1)
        await asyncio.sleep(1.0)
    return insts  # best effort; greeks step will note if still empty


async def main() -> None:
    configure_logging(json_output=False)
    settings = load_settings(os.environ.get("CONFIG_PATH", "configs/production.yaml"))
    sym = settings.params.strategy.symbol
    a = TInvestBrokerAdapter(settings, SystemClock(), sandbox=False)
    await a.connect()
    try:
        print(f"\n=== {sym} 1-LOT POST-TRADE VERIFICATION ===")

        # 1) positions (exercises get_positions)
        positions = await a.get_positions()
        print(f"positions: {len(positions)}")
        for p in positions:
            print(f"  {p.instrument_symbol}  qty={p.quantity}")

        # 2) fills (exercises get_fills)
        try:
            fills = await a.get_fills()
            print(f"fills (last {len(fills)}):")
            for f in fills[-10:]:
                print(f"  {f.instrument_symbol} {f.side.value} qty={f.quantity} "
                      f"price={f.price} -> debit if x100 = {float(f.price) * float(f.quantity) * 100:.2f} RUB")
        except Exception as exc:  # surface the real response-shape error to fix it
            print(f"get_fills ERROR ({type(exc).__name__}): {exc}")

        # 3) greeks for the option positions we now hold
        insts = await _load_chain(a, sym)
        by_symbol = {i.symbol: i for i in insts}
        held_opts = [
            p for p in positions
            if p.instrument_symbol in by_symbol
            and by_symbol[p.instrument_symbol].asset_class is AssetClass.OPTION
        ]
        if not held_opts:
            print("\nNo GAZP option position found yet — buy 1 lot in the app, then re-run.")
            return

        fut = next(i for i in insts if i.asset_class is AssetClass.FUTURE)
        qf = await a.get_quote(fut.symbol)
        forward = float((_mid(qf) or Decimal(0)) / fut.spec.quote_scale)
        sigmas: dict[str, float] = {}
        for p in held_opts:
            leg = by_symbol[p.instrument_symbol]
            q = await a.get_quote(leg.symbol)
            tau = (datetime.combine(leg.expiry, datetime.min.time(), tzinfo=UTC)
                   - datetime.now(UTC)).total_seconds() / (365 * 24 * 3600)
            try:
                sigmas[leg.symbol] = implied_volatility(
                    price=float(_mid(q)), underlying=forward, strike=float(leg.strike),
                    t=tau, rate=_RATE, option_type=leg.option_type, model="black76")
            except Exception:
                sigmas[leg.symbol] = 0.30
        eng = PortfolioGreeksEngine(by_symbol)
        inp = PricingInputs(
            valuation_time=datetime.now(UTC),
            underlying=UnderlyingState(spot=forward, rate=_RATE, dividend_yield=0.0),
            sigma_by_symbol=sigmas, mid_by_symbol={},
            future_multiplier=float(fut.spec.multiplier))
        g = eng.compute(held_opts, inp)
        print(f"\nheld-position greeks (forward {forward:.2f}): "
              f"net_delta_units={g.net_delta_units:.2f} gamma={g.net_gamma_units:.4f} "
              f"vega/pct={float(g.net_vega_per_pct):.1f} cash_delta={float(g.cash_delta):.0f} RUB")
        print("\nCONFIRM: the app debit for your 1-lot buy should match the "
              "'debit if x100' figure above. If so, 1 contract = 100 shares is confirmed.")
        print("Then CLOSE the position in the app (sell the lot you bought).")
    finally:
        await a.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
