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

## Stage 5 — Portfolio & risk
- Portfolio Greeks, hedge sizing (rounding modes), stress P&L, P&L attribution,
  Risk Manager limits, kill switches. — R15, R16, R22, R29.

## Stage 6 — OMS & execution
- Order state machine, idempotency, partial fills, reconciliation, restart
  recovery, `PaperBrokerAdapter`. — R10, R20, R21, R23.

## Stage 7 — Strategy
- `BaseStrategy`, ATM selection, Long Straddle open (two legs), initial hedge,
  delta band, gamma scalping, close rules, vol forecast model. — R17, R18, R19.

## Stage 8 — Backtest & paper
- Event-driven backtester reusing strategy; commissions/slippage/latency/partials;
  metrics, walk-forward, P&L attribution report. — R24.

## Stage 9 — Real broker (no live by default)
- `RealBrokerAdapter` for a chosen broker; sandbox; reconciliation; safe-start
  runbook. Live stays disabled. — R11.

## Definition of done (v1)
The 17 acceptance criteria in the brief, section 11. Tracked in
`REQUIREMENTS_TRACEABILITY.md`.

## Working agreement
- Never claim a command/test ran unless it actually ran (output shown).
- Unavailable external API → mock + explicit limitation note.
- Ambiguity → safest choice + an ADR.
