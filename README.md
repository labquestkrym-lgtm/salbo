# trading-bot — Delta-Hedged Long Straddle with Adaptive Gamma Scalping

> ## ⚠️ Financial risk warning
> This software trades options and futures. Derivatives can lose money rapidly.
> No configuration, model, or backtest in this repository guarantees profit; a
> volatility forecast is **not** a promise of returns. Backtest and synthetic
> results are **not** evidence of live profitability. **Live trading is disabled
> by default** and gated behind multiple independent safety checks. Use at your
> own risk, only with capital you can afford to lose, and only after validating
> in backtest → paper → sandbox.

## Purpose
A modular, testable options trading system. The first strategy is a
**delta-hedged long straddle** that scalps gamma by re-hedging the futures leg
when portfolio delta leaves an adaptive band. The same strategy code runs in
four modes — `backtest`, `paper`, `sandbox`, `live` — differing only by the
injected broker adapter and clock (see [ARCHITECTURE.md](ARCHITECTURE.md), ADR-0002).

## Current status
Foundation in place and verified:
- Planning docs + ADRs ([ARCHITECTURE](ARCHITECTURE.md),
  [REQUIREMENTS_TRACEABILITY](REQUIREMENTS_TRACEABILITY.md),
  [IMPLEMENTATION_PLAN](IMPLEMENTATION_PLAN.md), `docs/adr/`).
- Typed configuration with the **live-trading multi-gate** (`app/config`).
- Structured JSON logging with secret redaction (`app/core`).
- **Pricing engine**: Black-Scholes-Merton, Black-76, robust implied-vol solver,
  full Greeks incl. vanna/volga/charm/speed (`app/pricing`).
- Health/readiness/status API (`app/api`).
- Test suite (unit + property-based + integration) — all green.

See `REQUIREMENTS_TRACEABILITY.md` for the per-requirement status matrix and the
remaining stages.

## Requirements
- Python 3.12+ (developed/verified here on 3.14).
- Docker + Docker Compose (for the full stack).
- PostgreSQL 16, Redis 7 (provided by Compose).

## Installation
```bash
make install            # creates .venv and installs dev deps
cp .env.example .env     # then edit secrets — NEVER commit .env
```

## Configuration
- **Secrets** → `.env` (gitignored). See `.env.example`.
- **Strategy / risk / execution parameters** → `configs/*.yaml`, validated by
  Pydantic. Mandatory risk limits are left at `0` ("unset") in
  `configs/example.yaml` on purpose — the app refuses to trade live until they
  are set to real, analyzed values.

## Running tests
```bash
make test     # pytest (unit + property-based + integration)
make check    # format + lint (ruff) + types (mypy) + tests
```

## Running a backtest
```bash
# (Stage 8) event-driven backtester reusing the live strategy code.
# python -m backtest --config configs/example.yaml
```
Backtesting accounts for bid/ask, commissions, slippage, latency, partial fills,
expiries and gaps. It never fills at the last price. See [BACKTESTING.md](BACKTESTING.md).

## Running paper mode
```bash
# (Stage 6/8) real market data, simulated fills.
# APP_MODE=paper python -m workers.run --config configs/example.yaml
```

## Safe live-mode startup
Live trading requires **all** of (ADR-0003):
1. `APP_MODE=live`
2. `LIVE_TRADING_ENABLED=true`
3. Validated, non-zero mandatory risk limits
4. Passing health check
5. Successful position reconciliation
6. `LIVE_CONFIRMATION_CODE` supplied and echoed on `POST /strategy/start`

In `development`/`test` environments live mode is refused outright. See
[RISK_MANAGEMENT.md](RISK_MANAGEMENT.md) (operational runbooks land in Stage 9).

## Emergency stop
`POST /kill-switch` (auth + confirmation). Policy is configurable:
`HOLD` (default) / `HEDGE_ONLY` / `FLATTEN_FUTURES` / `CLOSE_ALL`. Options are
**not** panic-closed by default — forced exits into illiquid books can worsen
losses. See [RISK_MANAGEMENT.md](RISK_MANAGEMENT.md).

## Crash recovery
On restart the OMS rebuilds in-flight order state from PostgreSQL and reconciles
against the broker before any new action. No fill is ever assumed without broker
confirmation.

## Docker
```bash
make up       # docker compose up -d --build  (postgres + redis + api)
make logs
make down
```

## Documentation
Available now:
[ARCHITECTURE](ARCHITECTURE.md) ·
[RISK_MANAGEMENT](RISK_MANAGEMENT.md) ·
[STRATEGY](STRATEGY.md) ·
ADRs in `docs/adr/`

Written alongside their stages (see [IMPLEMENTATION_PLAN](IMPLEMENTATION_PLAN.md)):
`BROKER_ADAPTER.md` (Stage 4/9), `BACKTESTING.md` (Stage 8), `DEPLOYMENT.md` /
`OPERATIONS.md` / `INCIDENT_RESPONSE.md` (Stage 9).
