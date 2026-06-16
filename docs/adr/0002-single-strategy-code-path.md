# ADR-0002: One strategy code path across all run modes

- Status: Accepted
- Date: 2026-06-16

## Context
Backtest results are only meaningful if the live strategy behaves identically.
Duplicating logic between a backtester and a live runner is the classic source
of "it worked in backtest" failures.

## Decision
`StrategyEngine` and all decision logic (ATM selection, hedge band, risk checks)
are mode-agnostic. They depend on injected abstractions: `BrokerAdapter`,
`Clock`, `MarketDataSource`. Modes differ only by which concrete implementations
the DI container wires in (see ARCHITECTURE.md §3).

## Consequences
- Backtester is an event loop driving the same engine with a simulated clock and
  `MockBrokerAdapter`/historical replay.
- Any strategy change is automatically reflected in backtest, paper and live.
- Requires disciplined dependency injection and no wall-clock or `time.time()`
  calls outside the `Clock` abstraction.
