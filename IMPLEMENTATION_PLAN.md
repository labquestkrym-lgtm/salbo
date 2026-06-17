# Implementation Plan

The same `StrategyEngine` code runs in every mode; only adapters/clock change.
Each stage ends with: format (ruff format) → lint (ruff) → types (mypy) → tests
(pytest) → docs update → a single focused git commit.

## Stage 1 — Requirements analysis & planning ✅ (this commit)
- Repo analysis (empty: only `README.md`).
- `ARCHITECTURE.md`, `REQUIREMENTS_TRACEABILITY.md`, `IMPLEMENTATION_PLAN.md`.
- ADR-0001..0003.
- **Risks closed:** scope ambiguity, money-precision policy, live-trading gating policy.

## Stage 2 — Skeleton, config, tooling
- Package tree under `app/`, `backtest/`, `paper/`, `workers/`, `tests/`.
- `pyproject.toml` (pinned deps), `.gitignore`, `.env.example`, `Makefile`.
- `app/config`: Pydantic Settings, `AppMode`, **live-trading multi-gate**.
- `app/core`: enums, value objects, clock, exceptions, JSON logging, DI container.
- Docker, docker-compose (app + postgres), Dockerfile.
- `app/api`: `/health`, `/ready` minimal.
- pytest infra + first config tests.
- **Risks closed:** R1, R2, R28 (partial), R30 (partial).

## Stage 3 — Math kernel  ◀ implemented alongside Stage 2 in first delivery
- `app/pricing`: Black-Scholes-Merton, Black-76, IV solver, full Greeks
  (delta/gamma/theta/vega/rho/vanna/volga/charm/speed), lot/multiplier scaling.
- Unit + property-based tests (put-call parity, finite-difference Greeks).
- **Risks closed:** R3, R4, R5, R6, R7.

## Stage 4 — Market data & instruments ✅
- Domain models (`app/models`: Instrument/ContractSpec/Quote/Position/Order/Fill).
- `BaseBrokerAdapter` (universal interface) + `MockBrokerAdapter` (deterministic
  GBM underlying, carry-priced future, flat-IV option chain, bid/ask fills).
- `InstrumentResolver` (underlying↔future↔option-series mapping + validation;
  preserves differing option/future multipliers).
- `MarketDataService` (book state, staleness, gap/duplicate/out-of-order
  detection, crossed-market flag) + JSONL recorder/replayer.
- 18 new tests. — R7 (partial), R8 (mock), R9, R12, R13.

## Stage 5 — Portfolio & risk ✅
- `PortfolioGreeksEngine`: net/cash/futures-equivalent delta, gamma, $-gamma,
  theta/day, vega/pt, vanna, volga, gross exposure (per-instrument multipliers).
- `compute_hedge_contracts`: dimensionally-checked sizing with 5 rounding modes.
- `attribute_pnl`: delta/gamma/theta/vega + hedge/basis/fees/spread/slippage +
  residual (explained + residual == total).
- `RiskManager` + latching `KillSwitch`: hard limits, critical-breach trip,
  HOLD-by-default policy, external-event triggers, order-size veto.
- 19 new tests. — R7, R15, R16, R22, R29.

## Stage 6 — OMS & execution ✅
- `OrderManager` state machine: validated transitions, idempotent submit,
  persisted `OrderEvent`s, no assumed fills, broker resync, `recover()` after
  restart, overfill guard; `OrderStore` protocol + in-memory impl.
- `reconcile_positions` + `ReconciliationService` (trips POSITION_DESYNC).
- `StraddleExecutor`: both-legs-or-rollback, sequential/parallel, one-leg unwind.
- `PaperBrokerAdapter`: bid/ask fills, latency ticks, partial fills by book
  size, slippage, commission; driven by `process_pending`.
- 23 new tests. — R10, R20, R21, R23.

## Stage 7 — Strategy ✅
- `vol_forecast`: HV/EWMA/Parkinson/Garman-Klass/RV estimators + ATR; blended
  `RealizedVolForecastModel`; buy-vol signal E[RV] > IV + cost/uncertainty.
- `select_atm_strike`: liquidity/spread/proximity/volume/OI filters.
- `HedgeEngine`: adaptive delta band, hedge-to-zero/inner-band, cost gate,
  cooldown + per-minute rate limit.
- `DeltaHedgedLongStraddleStrategy`: entry (vol signal + liquid ATM), exit
  (time/profit/loss), marketable-limit leg builder.
- End-to-end test: ATM -> open straddle -> Greeks -> hedge -> delta reduced,
  commissions charged (acceptance criteria 5-11).
- 24 new tests. — R17, R18, R19.

## Stage 8 — Backtest & paper ✅
- `backtest/metrics.py`: total/annualized return, Sharpe, Sortino, max drawdown,
  Calmar, win rate, profit factor (degenerate-input safe).
- `backtest/walk_forward.py`: rolling train/validation/out-of-sample windows.
- `backtest/engine.py`: event-driven run over the mock stream reusing the live
  strategy/hedge/OMS code; bid/ask fills, commissions, slippage, mark-to-market;
  emits `BacktestReport` (incl. hedge count, delta deviation).
- 15 new tests incl. end-to-end run (acceptance criterion 16).
- Paper mode reuses the PaperBroker fill model from Stage 6. — R24.

## Stage 9 — Real broker (no live by default) ✅
- `RealBrokerAdapter` template: every method raises NotImplementedError; two
  guards — construction (refuse non-sandbox in dev/test) and trade (refuse
  order methods on a non-sandbox endpoint unless all live gates pass).
- Operational docs: DEPLOYMENT.md, OPERATIONS.md, INCIDENT_RESPONSE.md.
- 6 new guard tests. Live remains disabled by default. — R11.

## Stage R25 — Persistence ✅
- SQLAlchemy 2.0 async ORM (instruments, orders, order_events, fills, positions,
  portfolio_snapshots, audit_log, config_versions); `DecimalText` stores money
  exactly on any backend (ADR-0001).
- `Database` (async engine/session), `OrderRepository`, `PositionRepository`.
- Alembic (`alembic.ini`, `migrations/env.py`, initial migration `0001`).
- Tests: Decimal-exact round-trips + Alembic `upgrade head` applies (SQLite;
  prod targets PostgreSQL/asyncpg — Docker not available in this environment).

## Stage R14 — Volatility surface ✅
- `app/volatility`: per-expiry `InterpolatedSmile` (IV solved from bid/ask/mid,
  keyed on log-moneyness, flat extrapolation, never negative), `VolatilitySurface`
  with ATM term structure + total-variance interpolation across maturities,
  quality flags (crossed/illiquid/unsolvable -> unreliable), `SmileModel`
  protocol so SVI/SABR slot in later. 7 tests.

## Stage R27 — Notifications ✅
- `app/notifications`: `NotificationService` fan-out with mandatory secret
  redaction (message + field values), per-channel error isolation, and typed
  event helpers (start/stop, leg fill, hedge, limit breach, data loss, desync,
  kill switch, daily P&L, error). Channels: `LogChannel`, `CollectingChannel`
  (tests), `TelegramChannel` (token only in URL), `EmailChannel` (SMTP, off-loop).
  8 tests. Live network channels not exercised (no creds) — documented.

## Remaining (post-v1 wiring)
- R26 trading API endpoints + WebSocket + auth/audit; R30 Prometheus endpoint.
  A DB-backed `OrderStore` (async) and the orchestration worker loop tie the live
  runner together. `docker compose up` + `alembic upgrade head` need a Docker
  host (unavailable here).

## Definition of done (v1)
The 17 acceptance criteria in the brief, section 11. Tracked in
`REQUIREMENTS_TRACEABILITY.md`.

## Working agreement
- Never claim a command/test ran unless it actually ran (output shown).
- Unavailable external API → mock + explicit limitation note.
- Ambiguity → safest choice + an ADR.
