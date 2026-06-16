# Operations Runbook

> Live is off by default. Treat every production action as irreversible until
> proven otherwise.

## Daily start-up checklist
1. `GET /health` and `GET /ready` are 200.
2. `GET /status` shows the expected `mode` and `live_trading_allowed`.
3. Broker connectivity OK; market data fresh (no staleness alerts).
4. Run reconciliation (`POST /reconcile`) — local vs broker positions match.
5. Confirm risk config is the intended version (`GET /config`).

## Safe live start (all six gates, ADR-0003)
1. `APP_MODE=live`
2. `LIVE_TRADING_ENABLED=true`
3. Mandatory risk limits set & validated (non-zero).
4. Health check passing.
5. Reconciliation clean.
6. `LIVE_CONFIRMATION_CODE` supplied and echoed on `POST /strategy/start`.

If any gate is missing, `GET /status` lists it under `live_trading_blockers`
and the strategy will not start live.

## Monitoring
- Net/cash delta, gamma, vega, theta vs limits.
- Hedge frequency / daily turnover.
- Order reject rate (repeated rejects → kill switch).
- Market-data staleness and gap counters (`FeedHealth`).
- Margin utilization and cash reserve.

## Routine actions
- **Pause** (`POST /strategy/pause`): stop opening new positions; keep managing
  the hedge.
- **Manual hedge** (`POST /hedge`): force a hedge evaluation.
- **Stop** (`POST /strategy/stop`): orderly shutdown.

## Kill switch
`POST /kill-switch` (auth + confirmation). Policy: `HOLD` (default) /
`HEDGE_ONLY` / `FLATTEN_FUTURES` / `CLOSE_ALL`. Default does NOT panic-close
options. See [INCIDENT_RESPONSE.md](INCIDENT_RESPONSE.md).

## Restart / recovery
On restart the OMS rebuilds in-flight orders from persistence and reconciles
with the broker before any new action (`OrderManager.recover`). Do not resume
trading while a critical reconciliation mismatch is unresolved.

## End-of-day
- Record daily P&L and P&L attribution.
- Verify positions/cash/margin reconcile.
- Review hedge count, turnover, slippage vs expectations.
