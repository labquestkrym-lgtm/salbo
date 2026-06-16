"""Long-straddle strategy decision logic (R17)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from app.core.enums import AssetClass, OptionStyle, OptionType, OrderType, PricingModel, Side
from app.models import ContractSpec, Instrument, Quote
from app.strategies.atm import StrikeCandidate
from app.strategies.long_straddle import (
    DeltaHedgedLongStraddleStrategy,
    StraddleEntryParams,
    StraddleExitParams,
)
from app.strategies.vol_forecast import ForecastConfig, VolatilityForecast

_T = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)
_OPT_SPEC = ContractSpec(
    tick_size=Decimal("0.05"),
    tick_value=Decimal("5"),
    lot_size=100,
    multiplier=Decimal("100"),
    currency="USD",
)


def _strategy() -> DeltaHedgedLongStraddleStrategy:
    return DeltaHedgedLongStraddleStrategy(
        entry_params=StraddleEntryParams(contracts=2),
        exit_params=StraddleExitParams(
            min_days_to_expiry=7, profit_target=Decimal("500"), loss_stop=Decimal("300")
        ),
        forecast_config=ForecastConfig(transaction_cost_buffer=0.02, model_uncertainty_buffer=0.02),
    )


def _candidate() -> StrikeCandidate:
    q = Quote(
        instrument_symbol="o",
        timestamp=_T,
        bid=Decimal("5.00"),
        ask=Decimal("5.10"),
        bid_size=Decimal("10"),
        ask_size=Decimal("10"),
    )
    return StrikeCandidate(strike=Decimal("100"), call_quote=q, put_quote=q)


def test_entry_requires_both_liquidity_and_vol_signal() -> None:
    strat = _strategy()
    cand = _candidate()
    spot = Decimal("100")
    high = VolatilityForecast(expected_rv=0.40)
    low = VolatilityForecast(expected_rv=0.18)
    assert strat.evaluate_entry(spot=spot, candidates=[cand], forecast=high, implied_vol=0.20).enter
    # Liquid ATM but no vol edge -> no entry.
    assert not strat.evaluate_entry(
        spot=spot, candidates=[cand], forecast=low, implied_vol=0.20
    ).enter
    # Vol edge but no liquid strike -> no entry.
    assert not strat.evaluate_entry(spot=spot, candidates=[], forecast=high, implied_vol=0.20).enter


def test_exit_time_stop() -> None:
    strat = _strategy()
    d = strat.evaluate_exit(
        now=date(2026, 1, 25), expiry=date(2026, 1, 30), unrealized_pnl=Decimal("0")
    )
    assert d.exit and "time stop" in d.reason


def test_exit_profit_and_loss() -> None:
    strat = _strategy()
    far = date(2026, 6, 30)
    assert strat.evaluate_exit(now=date(2026, 1, 5), expiry=far, unrealized_pnl=Decimal("600")).exit
    assert strat.evaluate_exit(
        now=date(2026, 1, 5), expiry=far, unrealized_pnl=Decimal("-400")
    ).exit
    assert not strat.evaluate_exit(
        now=date(2026, 1, 5), expiry=far, unrealized_pnl=Decimal("100")
    ).exit


def test_build_leg_requests_uses_marketable_limit_at_offer() -> None:
    strat = _strategy()
    cand = _candidate()
    from app.strategies.atm import select_atm_strike

    sel = select_atm_strike(Decimal("100"), [cand])
    assert sel is not None
    call = Instrument(
        symbol="C",
        underlying_symbol="XYZ",
        asset_class=AssetClass.OPTION,
        spec=_OPT_SPEC,
        expiry=date(2026, 2, 1),
        option_type=OptionType.CALL,
        strike=Decimal("100"),
        option_style=OptionStyle.EUROPEAN,
        pricing_model=PricingModel.BLACK_SCHOLES_MERTON,
    )
    put = call.model_copy(update={"symbol": "P", "option_type": OptionType.PUT})
    call_req, put_req = strat.build_leg_requests(sel, call, put, id_prefix="x")
    assert call_req.order_type is OrderType.MARKETABLE_LIMIT
    assert call_req.side is Side.BUY
    assert call_req.limit_price == Decimal("5.10")  # the offer
    assert call_req.quantity == Decimal("2")
    assert put_req.client_order_id == "x-put"
