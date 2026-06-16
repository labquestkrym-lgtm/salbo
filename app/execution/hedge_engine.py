"""Hedge Engine (R19): when and how much to re-hedge the futures leg.

Primary mode is an **adaptive delta band**: do nothing while net delta stays
inside a trigger band around the target; when it leaves, trade futures to bring
delta back to either zero or the inner band. The band adapts to gamma, realized
vol and transaction cost. A **cost gate** blocks hedges whose expected benefit
does not exceed expected transaction cost (except in emergency). Cooldown and
rate limits prevent over-trading.

This module decides; the OMS/execution layer places the resulting order.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.core.enums import RoundingMode
from app.portfolio.hedge_sizing import compute_hedge_contracts


@dataclass(frozen=True, slots=True)
class HedgeBandConfig:
    target_delta_units: float = 0.0
    base_trigger_units: float = 0.0  # band half-width in underlying units (>0 to enable)
    hedge_to_zero: bool = True  # else hedge to inner band edge
    inner_band_units: float = 0.0
    min_futures_trade: int = 1
    max_futures_trade: int = 1_000_000
    cooldown_seconds: float = 0.0
    max_hedges_per_minute: int = 0  # 0 = unlimited
    min_expected_benefit: Decimal = Decimal("0")  # currency
    rounding_mode: RoundingMode = RoundingMode.NEAREST


@dataclass(frozen=True, slots=True)
class HedgeDecision:
    should_hedge: bool
    contracts: int
    reason: str
    target_delta_units: float
    expected_cost: Decimal = Decimal("0")
    expected_benefit: Decimal = Decimal("0")


class HedgeEngine:
    def __init__(self, config: HedgeBandConfig) -> None:
        self._cfg = config
        self._last_hedge_at: datetime | None = None
        self._hedge_times: list[datetime] = []

    def adaptive_trigger(
        self, *, gamma_units: float, realized_vol: float, spread_cost: Decimal
    ) -> float:
        """Tighten the band as gamma/vol rise (re-hedge sooner to capture
        convexity); widen it as transaction cost rises (avoid churn)."""
        base = self._cfg.base_trigger_units
        if base <= 0:
            return base
        vol_factor = 1.0 / (1.0 + 4.0 * max(realized_vol, 0.0))  # higher vol -> tighter
        gamma_factor = 1.0 / (1.0 + abs(gamma_units) / 1000.0)  # higher gamma -> tighter
        cost_factor = 1.0 + float(spread_cost)  # higher cost -> wider
        return base * vol_factor * gamma_factor * cost_factor

    def decide(
        self,
        *,
        current_delta_units: float,
        future_multiplier: float,
        now: datetime,
        future_delta_per_unit: float = 1.0,
        gamma_units: float = 0.0,
        realized_vol: float = 0.0,
        cost_per_contract: Decimal = Decimal("0"),
        spread_cost: Decimal = Decimal("0"),
        emergency: bool = False,
    ) -> HedgeDecision:
        cfg = self._cfg
        target = cfg.target_delta_units
        deviation = current_delta_units - target
        trigger = self.adaptive_trigger(
            gamma_units=gamma_units, realized_vol=realized_vol, spread_cost=spread_cost
        )

        if not emergency and trigger > 0 and abs(deviation) <= trigger:
            return HedgeDecision(False, 0, "within delta band", target)

        if not emergency and not self._rate_ok(now):
            return HedgeDecision(False, 0, "cooldown/rate limit", target)

        # Hedge target: all the way to zero, or just inside the band edge.
        if cfg.hedge_to_zero:
            hedge_target = target
        else:
            edge = cfg.inner_band_units if cfg.inner_band_units > 0 else trigger
            hedge_target = target + (edge if deviation > 0 else -edge)

        sizing = compute_hedge_contracts(
            current_delta_units=current_delta_units,
            target_delta_units=hedge_target,
            future_multiplier=future_multiplier,
            rounding_mode=cfg.rounding_mode,
            future_delta_per_unit=future_delta_per_unit,
        )
        contracts = sizing.contracts
        if contracts == 0:
            return HedgeDecision(False, 0, "rounds to zero contracts", hedge_target)
        if abs(contracts) < cfg.min_futures_trade and not emergency:
            return HedgeDecision(False, 0, "below min_futures_trade", hedge_target)
        if abs(contracts) > cfg.max_futures_trade:
            contracts = cfg.max_futures_trade if contracts > 0 else -cfg.max_futures_trade

        expected_cost = cost_per_contract * abs(contracts) + spread_cost
        # Benefit proxy: delta units neutralized * a unit risk price (spread_cost
        # per unit as a conservative stand-in). Documented simplification.
        neutralized = abs(deviation) - abs(sizing.residual_delta_units)
        expected_benefit = Decimal(str(max(neutralized, 0.0))) * spread_cost

        if (
            not emergency
            and expected_cost > expected_benefit
            and expected_benefit < cfg.min_expected_benefit
        ):
            return HedgeDecision(
                False, 0, "benefit below cost", hedge_target, expected_cost, expected_benefit
            )

        self._record_hedge(now)
        return HedgeDecision(
            True, contracts, "rehedge", hedge_target, expected_cost, expected_benefit
        )

    def _rate_ok(self, now: datetime) -> bool:
        cfg = self._cfg
        if (
            cfg.cooldown_seconds > 0
            and self._last_hedge_at is not None
            and (now - self._last_hedge_at).total_seconds() < cfg.cooldown_seconds
        ):
            return False
        if cfg.max_hedges_per_minute > 0:
            recent = [t for t in self._hedge_times if (now - t).total_seconds() <= 60.0]
            if len(recent) >= cfg.max_hedges_per_minute:
                return False
        return True

    def _record_hedge(self, now: datetime) -> None:
        self._last_hedge_at = now
        self._hedge_times.append(now)
        self._hedge_times = [t for t in self._hedge_times if (now - t).total_seconds() <= 60.0]
