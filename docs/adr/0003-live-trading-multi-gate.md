# ADR-0003: Live trading requires multiple independent gates

- Status: Accepted
- Date: 2026-06-16

## Context
Accidentally sending real orders is catastrophic. A single boolean flag is too
easy to flip by mistake or leave on.

## Decision
Reaching live trading requires **all** of the following, validated at startup
and re-checked before the first order:

1. `APP_MODE=live`
2. `LIVE_TRADING_ENABLED=true`
3. Risk config fully populated and validated (no zero/placeholder mandatory
   limits) — see `RiskConfig.assert_ready_for_live()`.
4. Passing health check (broker connectivity, market data freshness, DB).
5. Successful position reconciliation against the broker.
6. A separate one-time confirmation code `LIVE_CONFIRMATION_CODE` supplied via
   env and echoed on the protected `POST /strategy/start` call.

In `development` and `test` environments, constructing a `RealBrokerAdapter`
pointed at a non-sandbox endpoint raises immediately.

## Consequences
- Defense in depth; no single misconfiguration enables live orders.
- Mock/paper/backtest workflows are unaffected and remain the default.
