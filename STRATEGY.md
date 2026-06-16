# Strategy: Delta-Hedged Long Straddle with Adaptive Gamma Scalping

> Not investment advice. A volatility forecast does not guarantee profit.

## Thesis
Buy an ATM straddle (long call + long put, same strike & expiry) when expected
realized volatility exceeds implied volatility plus cost buffers. Stay delta
neutral by trading the corresponding future, monetizing realized volatility
through gamma scalping while paying theta.

## Lifecycle
1. **Select underlying** and a permitted expiry within
   `[min_days_to_expiry, max_days_to_expiry]`.
2. **Select ATM strike** — not merely the nearest numeric strike, but the best
   strike by liquidity, spread, presence of both call & put, and IV quality.
3. **Open both legs** (call + put). The structure is *not* considered open until
   **both** fills are confirmed; one-leg risk is bounded by
   `max_seconds_between_legs` with rollback / emergency hedge on failure.
4. **Compute portfolio delta** from per-option deltas × multipliers × quantities.
5. **Initial hedge** in the future to reach `target_delta`.
6. **Continuously recompute Greeks** from live quotes (mid, never last).
7. **Re-hedge** when delta leaves the band (see Hedge Engine).
8. **Attribute P&L** into delta / gamma / theta / vega / hedge / basis / fees /
   spread / slippage.
9. **Close** per strategy + risk rules (profit target, time/DTE stop,
   vol-regime change, risk veto).

## Entry signal (`VolatilityForecastModel`)
```
expected_realized_vol > implied_vol + transaction_cost_buffer + model_uncertainty_buffer
```
First version combines HV / EWMA / Parkinson / Garman-Klass / realized vol / ATR
and intraday vol. Thresholds are configurable and exposed to the backtester.

## Hedge Engine (`HedgeMode`)
Modes: `time`, `absolute_delta`, `cash_delta`, `underlying_move`, `atr`,
`combined`, `adaptive_delta_band` *(default)*.

Delta-band logic:
- `target_delta` (typically 0), a trigger band, and a hedge target
  (`hedge_to_zero` vs `hedge_to_inner_band`).
- Constraints: `minimum_futures_trade`, `maximum_futures_trade`, `cooldown`,
  `minimum_expected_benefit`, `maximum_hedges_per_minute`, `maximum_daily_turnover`.
- Adaptive threshold reacts to gamma, IV, realized vol, spread, commissions,
  liquidity, time-to-expiry and market speed.
- **Never hedge when expected benefit < transaction cost** (except emergency).

## Hedge sizing (`RoundingMode`)
```
portfolio_delta_units = Σ (quantity × option_delta × option_multiplier)
target_futures = round( -(portfolio_delta_units - target_delta_units) / futures_multiplier )
```
Dimensions are checked explicitly; option and futures multipliers/lots are never
assumed equal. Rounding modes: `nearest`, `floor`, `ceil`, `conservative`
(toward more hedge), `minimal_turnover` (toward less trade).

## Extensibility
`BaseStrategy` is designed so later structures slot in without touching
execution/risk: long strangle, short straddle with protective wings, iron
butterfly/condor, vertical/calendar/diagonal spreads, risk reversal, skew and
term-structure trades, delta-neutral volatility arbitrage.
