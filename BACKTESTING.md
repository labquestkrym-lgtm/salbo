# Backtesting

> Backtest and synthetic results are **not** evidence of live profitability.
> Do not tune parameters on one period and present that as an edge.

## Principle
The backtester runs the **same** strategy, hedge, OMS and risk code as live
(ADR-0002). Only the clock and broker adapter differ. This prevents
"works-in-backtest-only" divergence.

## What it accounts for
- Bid/ask fills (never the last price); commissions; slippage (crossing the
  spread); partial fills; multipliers; expiries; gaps. Mark-to-market uses mid.

## Components (`backtest/`)
- `metrics.py` — total/annualized return, Sharpe, Sortino, max drawdown, Calmar,
  win rate, profit factor. Degenerate inputs return 0 rather than NaN.
- `walk_forward.py` — rolling (train, validation, out-of-sample) windows so
  parameters are never judged on the data they were tuned on.
- `engine.py` — `BacktestEngine` drives the mock market stream through
  MarketDataService → strategy entry → hedge loop → mark-to-market, emitting a
  `BacktestReport`.

## Running
```python
from app.brokers.mock import MockBrokerAdapter, MockMarketConfig
from app.core.clock import SimulatedClock
from backtest import BacktestEngine, BacktestConfig
from datetime import datetime, UTC

clock = SimulatedClock(datetime(2026, 1, 5, 15, 0, tzinfo=UTC))
broker = MockBrokerAdapter(clock, MockMarketConfig(annual_vol=0.5, option_iv=0.2, dt_seconds=3600))
report = await BacktestEngine(clock, broker, BacktestConfig(steps=300)).run()
print(report.summary())
```

## Data
For real option data, the engine consumes the same `Quote` stream the live
adapters produce. A recorded JSONL session (`MarketDataRecorder`) can be replayed
via `app.market_data.replay_quotes`. A synthetic generator (the mock) exists only
to exercise infrastructure — never to claim profitability.

## Report metrics
`total_return`, `annualized_return`, `sharpe`, `sortino`, `max_drawdown`,
`calmar`, `commissions`, `slippage`, `hedge_count`, `avg/max_abs_delta_deviation`,
`equity_curve`. Trade-level win rate / profit factor are available in `metrics`.
