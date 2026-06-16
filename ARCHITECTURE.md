# Architecture

> ⚠️ **Financial risk warning.** This software trades derivatives. Options and
> futures can lose money rapidly and without limit on short legs. Nothing here
> is investment advice and no configuration guarantees profit. Live trading is
> **disabled by default** and gated behind multiple explicit confirmations.

## 1. Design goals

1. **Separation of concerns.** Strategy, execution, broker I/O, risk, pricing,
   persistence and the web interface live in different modules and never import
   each other's internals. Dependencies flow inward: adapters depend on core
   abstractions, never the reverse.
2. **One strategy implementation, four run modes.** The same `StrategyEngine`
   runs under `backtest`, `paper`, `sandbox` and `live`. Only the
   `BrokerAdapter` and clock implementation change.
3. **Determinism & testability.** No hidden global mutable state. Time, randomness
   and I/O are injected. The backtester and live runner share the strategy code.
4. **Fail safe, not fail open.** The `RiskManager` can veto any command. Loss of
   data, loss of broker connectivity or position desync trips a kill switch.
5. **Money is `Decimal`.** Prices, quantities and cash use `Decimal`. Only the
   math kernel (Greeks/IV) uses `float`/NumPy, and its outputs are converted at
   the boundary.

## 2. Component map

```
                 +-------------------+
                 |   FastAPI / WS    |  control plane (auth, audit, idempotency)
                 +---------+---------+
                           |
        +------------------+-------------------+
        |               Orchestrator           |  wires DI container, run mode
        +--+-----------+-----------+-----------++
           |           |           |           |
   +-------v--+  +------v-----+ +---v------+ +--v---------+
   | Strategy |  |   Risk     | |  Hedge   | | Reconcile  |
   |  Engine  |  |  Manager   | |  Engine  | |  Service   |
   +----+-----+  +------+-----+ +---+------+ +--+---------+
        |               |           |           |
        +-------+-------+-----+-----+-----+------+
                |             |           |
          +-----v----+  +-----v----+ +----v-----+
          |   OMS    |  | Portfolio| | Pricing  |
          | + Exec   |  |  / Risk  | | + Greeks |
          +-----+----+  |  Engine  | +----+-----+
                |       +-----+----+      |
                |             |           |
          +-----v-------------v-----------v-----+
          |        Market Data Service          |
          |     Instrument Resolver             |
          +------------------+------------------+
                             |
                  +----------v-----------+
                  |   Broker Adapter     |  Base / Mock / Paper / Real
                  +----------+-----------+
                             |
                  +----------v-----------+
                  | Repositories (SQLAlchemy / PostgreSQL) |
                  +-----------------------+
```

### Module responsibilities

| Package            | Responsibility                                                            | Must NOT contain                |
|--------------------|---------------------------------------------------------------------------|---------------------------------|
| `app/config`       | Pydantic Settings, mode/live gating, config versioning                    | trading logic                   |
| `app/core`         | Domain enums, value objects, clock, DI container, exceptions, logging     | broker/db specifics             |
| `app/models`       | Pydantic domain models (Instrument, Order, Fill, Position, Greeks, …)     | persistence ORM, I/O            |
| `app/pricing`      | BSM, Black-76, IV solver, Greeks (pure functions over floats)             | broker, db, asyncio             |
| `app/volatility`   | IV surface: smile per expiry, term structure, interpolation, quality flag | calibration models (later)      |
| `app/instruments`  | Instrument Resolver: underlying↔future↔option-series mapping & validation | pricing                         |
| `app/market_data`  | Streaming quotes, book state, staleness/gap detection, record/replay      | strategy decisions              |
| `app/portfolio`    | Positions, net/cash/futures-equiv Greeks, hedge sizing, stress P&L        | order placement                 |
| `app/risk`         | Limits, kill switches, kill-switch policies                               | broker API calls                |
| `app/execution`    | Straddle leg sequencing, futures hedge execution, slippage control        | strategy selection              |
| `app/brokers`      | `BaseBrokerAdapter` + Mock/Paper/Real adapters                            | strategy/risk logic             |
| `app/strategies`   | `BaseStrategy`, `DeltaHedgedLongStraddleStrategy`, vol forecast models    | direct broker calls             |
| `app/repositories` | Persistence (SQLAlchemy 2.0 async), reconciliation reads                  | business rules                  |
| `app/notifications`| Telegram/email/log fan-out (secret-safe)                                  | trading logic                   |
| `app/api`          | FastAPI routes, WebSocket, auth, audit                                    | strategy internals              |
| `backtest`         | Event-driven engine reusing `StrategyEngine`                              | live broker I/O                 |
| `paper`            | Paper fill simulator over real market data                                | —                               |
| `workers`          | Long-running asyncio tasks (md loop, hedge loop, reconcile loop)          | —                               |

## 3. Run modes

| Mode       | Broker adapter      | Clock          | Market data        | Orders            |
|------------|---------------------|----------------|--------------------|-------------------|
| `backtest` | `MockBrokerAdapter` | simulated      | historical replay  | simulated fills   |
| `paper`    | `PaperBrokerAdapter`| wall clock     | real (read-only)   | simulated fills   |
| `sandbox`  | `RealBrokerAdapter` | wall clock     | broker sandbox     | broker sandbox    |
| `live`     | `RealBrokerAdapter` | wall clock     | broker prod        | broker prod       |

`live` is unreachable unless **all** of: `APP_MODE=live`, `LIVE_TRADING_ENABLED=true`,
validated non-zero risk config, passing health check, successful reconciliation,
and a separate run confirmation code (`LIVE_CONFIRMATION_CODE`). See
[RISK_MANAGEMENT.md](RISK_MANAGEMENT.md) and `app/config`.

## 4. Concurrency model

`asyncio` single event loop. Long-running concerns run as supervised tasks
(`workers/`): market-data ingest, hedge loop, reconciliation loop, metrics. Each
worker is restart-safe and idempotent. Shared state is passed through the DI
container; no module-level mutable singletons.

## 5. Data & money precision

- **`Decimal`**: prices, ticks, cash, margin, P&L, quantities, multipliers.
- **`float`/NumPy**: only inside `app/pricing` and `app/volatility` math kernels.
- Conversion happens at the pricing-engine boundary via explicit quantizers tied
  to each instrument's tick size.

## 6. Persistence

PostgreSQL via SQLAlchemy 2.0 async + Alembic. Every order state transition, fill,
hedge decision, risk snapshot and dangerous API command is persisted for audit and
crash recovery. On restart the OMS rebuilds in-flight order state from the DB and
reconciles against the broker before any new action.

## 7. Architecture Decision Records

See `docs/adr/`. Notable: ADR-0001 (Decimal vs float boundary), ADR-0002
(single strategy code path across modes), ADR-0003 (live-trading multi-gate).
