# Risk Management

The `RiskManager` (`app/risk`, Stage 5) is an authority that can **veto any
command** issued by the strategy. It is independent of the strategy and the
broker adapter.

## Principles
- **Fail safe, not fail open.** Loss of data, loss of broker connectivity or
  position desync trips a kill switch.
- **No arbitrary defaults.** Mandatory limits default to `0` ("unset"); the app
  refuses live trading until they are explicitly set (`RiskConfig`).
- **No naked short options in v1.**
- **Decimal everywhere** for money/quantity comparisons.

## Hard limits (`RiskConfig`)
Daily loss · drawdown · margin utilization · net delta · cash delta · gamma ·
vega · theta · futures position · option contracts · single-order size ·
turnover · orders/minute · spread · slippage · cash reserve · stale-data age ·
position mismatch tolerance.

Mandatory-for-live subset (validated by `RiskConfig.assert_ready_for_live`):
`maximum_daily_loss`, `maximum_drawdown`, `maximum_margin_utilization`,
`maximum_cash_delta`, `maximum_gamma`, `maximum_vega`, `maximum_futures_position`,
`stale_market_data_seconds`, `maximum_single_order_size`,
`maximum_orders_per_minute`.

## Kill switches
Triggers: market-data loss · broker disconnect · position desync · unknown order
status · daily-loss breach · margin breach · abnormal price move · excessive
spread · invalid volatility · Greeks computation error · repeated rejects ·
system error · manual STOP.

On trip, the kill switch:
1. blocks new positions,
2. cancels active orders,
3. re-discovers actual positions via the broker (reconciliation),
4. applies the configured policy,
5. writes a detailed audit record,
6. sends notifications (secret-safe).

## Kill-switch policies (`KillSwitchPolicy`)
- `HOLD` *(default)* — stop acting, keep positions, alert. Chosen as default
  because panic-closing options in an illiquid book can increase losses.
- `HEDGE_ONLY` — keep options, only manage the futures delta hedge.
- `FLATTEN_FUTURES` — close the futures hedge, keep options.
- `CLOSE_ALL` — close everything (only with confirmed policy).

## Live-trading gate
See ADR-0003 and `AppSettings.live_trading_blockers()`. Six independent
conditions; any one missing blocks live orders. Dev/test environments are
refused outright.
