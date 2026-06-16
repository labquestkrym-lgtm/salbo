"""Delta-Hedged Long Straddle with adaptive gamma scalping (R17).

Decision logic only — coordination/execution is done by the orchestrator using
the OMS, StraddleExecutor, PortfolioGreeksEngine, HedgeEngine and RiskManager.

Entry: a qualifying ATM strike exists AND the volatility forecast signals that
expected realized vol exceeds implied vol plus cost/uncertainty buffers.

Exit: time stop (<= min DTE), profit target, or loss stop. Risk-manager vetoes
and kill switches are enforced by the orchestrator, above this logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.core.enums import OrderType, Side
from app.models import Instrument, OrderRequest
from app.strategies.atm import ATMSelection, StrikeCandidate, select_atm_strike
from app.strategies.base import BaseStrategy, EntryDecision, ExitDecision
from app.strategies.vol_forecast import ForecastConfig, VolatilityForecast


@dataclass(frozen=True, slots=True)
class StraddleEntryParams:
    contracts: int = 1
    max_spread_fraction: Decimal = Decimal("0")
    max_strike_distance: Decimal = Decimal("0")
    min_volume: int = 0
    min_open_interest: int = 0


@dataclass(frozen=True, slots=True)
class StraddleExitParams:
    min_days_to_expiry: int = 0
    profit_target: Decimal | None = None
    loss_stop: Decimal | None = None  # positive magnitude


class DeltaHedgedLongStraddleStrategy(BaseStrategy):
    name = "delta_hedged_long_straddle"

    def __init__(
        self,
        *,
        entry_params: StraddleEntryParams,
        exit_params: StraddleExitParams,
        forecast_config: ForecastConfig,
    ) -> None:
        self._entry = entry_params
        self._exit = exit_params
        self._fc = forecast_config

    def evaluate_entry(  # type: ignore[override]
        self,
        *,
        spot: Decimal,
        candidates: list[StrikeCandidate],
        forecast: VolatilityForecast,
        implied_vol: float,
    ) -> EntryDecision:
        selection = select_atm_strike(
            spot,
            candidates,
            max_spread_fraction=self._entry.max_spread_fraction,
            max_strike_distance=self._entry.max_strike_distance,
            min_volume=self._entry.min_volume,
            min_open_interest=self._entry.min_open_interest,
        )
        if selection is None:
            return EntryDecision(False, "no qualifying ATM strike (liquidity/spread)")
        if not forecast.is_buy_signal(implied_vol, self._fc):
            edge = forecast.edge_over(implied_vol, self._fc)
            return EntryDecision(False, f"vol edge {edge:.4f} <= 0", selection)
        return EntryDecision(True, "vol signal + liquid ATM", selection)

    def evaluate_exit(  # type: ignore[override]
        self,
        *,
        now: date,
        expiry: date,
        unrealized_pnl: Decimal,
    ) -> ExitDecision:
        dte = (expiry - now).days
        if dte <= self._exit.min_days_to_expiry:
            return ExitDecision(True, f"time stop: {dte} DTE <= {self._exit.min_days_to_expiry}")
        if self._exit.profit_target is not None and unrealized_pnl >= self._exit.profit_target:
            return ExitDecision(True, f"profit target hit ({unrealized_pnl})")
        if self._exit.loss_stop is not None and unrealized_pnl <= -self._exit.loss_stop:
            return ExitDecision(True, f"loss stop hit ({unrealized_pnl})")
        return ExitDecision(False, "hold")

    def build_leg_requests(
        self,
        selection: ATMSelection,
        call: Instrument,
        put: Instrument,
        *,
        id_prefix: str,
    ) -> tuple[OrderRequest, OrderRequest]:
        """Buy both legs with marketable limits at the offer (cross to fill,
        never a plain market order on options)."""
        qty = Decimal(self._entry.contracts)
        call_ask = selection.candidate.call_quote.ask
        put_ask = selection.candidate.put_quote.ask
        if call_ask is None or put_ask is None:
            raise ValueError("selected strike lost its two-sided market")
        call_req = OrderRequest(
            client_order_id=f"{id_prefix}-call",
            instrument_symbol=call.symbol,
            side=Side.BUY,
            quantity=qty,
            order_type=OrderType.MARKETABLE_LIMIT,
            limit_price=call_ask,
        )
        put_req = OrderRequest(
            client_order_id=f"{id_prefix}-put",
            instrument_symbol=put.symbol,
            side=Side.BUY,
            quantity=qty,
            order_type=OrderType.MARKETABLE_LIMIT,
            limit_price=put_ask,
        )
        return call_req, put_req
