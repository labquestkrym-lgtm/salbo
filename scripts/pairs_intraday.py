"""Intraday pairs mean-reversion backtest (READ-ONLY) -- hunting 2-3+ trades/day.

The daily pair trade is too infrequent (~10 trades / 2y). The same cointegrated
spread also oscillates intraday around its short-term mean, giving many more
signals. This backtests the z-score reversion on INTRADAY bars (hourly / 15-min)
for the validated pairs, reporting trades/day, win rate, net return and an
annualized Sharpe at futures friction. Signal from continuous share candles;
execution would be via the futures.

NOT a profitability promise -- it measures net-of-cost edge and frequency.

    $env:PYTHONPATH = (Get-Location)
    .venv312\\Scripts\\python scripts/pairs_intraday.py
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta

import numpy as np
import tinkoff.invest as ti

from app.config.settings import load_settings
from app.core.logging import configure_logging, get_logger

logger = get_logger("pairs_intraday")

_LEG_COST = float(os.environ.get("LEG_COST", "0.001"))  # per leg/side at futures friction
_WINDOW = int(os.environ.get("WINDOW", "60"))           # bars in the z-score window
_ENTRY_Z = float(os.environ.get("ENTRY_Z", "2.0"))
_EXIT_Z = float(os.environ.get("EXIT_Z", "0.5"))
_DAYS = int(os.environ.get("DAYS", "45"))
# Prod gRPC has been refusing new connections (token connection throttle); the
# sandbox endpoint serves the same historical candles. Override with TARGET=prod.
_TARGET = (
    ti.constants.INVEST_GRPC_API
    if os.environ.get("TARGET") == "prod"
    else ti.constants.INVEST_GRPC_API_SANDBOX
)
_PAIRS = [("LKOH", "SNGS"), ("NLMK", "CHMF"), ("ROSN", "SNGS"), ("LKOH", "ROSN")]
_INTERVAL_MAP = {
    "hour": ti.CandleInterval.CANDLE_INTERVAL_HOUR,
    "15min": ti.CandleInterval.CANDLE_INTERVAL_15_MIN,
    "5min": ti.CandleInterval.CANDLE_INTERVAL_5_MIN,
}
_INTERVAL_NAME = os.environ.get("INTERVAL", "hour")
_INTERVALS = {_INTERVAL_NAME: _INTERVAL_MAP[_INTERVAL_NAME]}


async def _intraday(cl: ti.AsyncClient, uid: str, interval: object) -> dict[datetime, float]:
    now = datetime.now(UTC)
    out: dict[datetime, float] = {}
    async for c in cl.get_all_candles(
        instrument_id=uid, from_=now - timedelta(days=_DAYS), interval=interval
    ):
        out[c.time] = c.close.units + c.close.nano / 1e9
    return out


def _backtest(spread: np.ndarray, bars_per_year: float) -> dict:
    n = len(spread)
    pos = 0.0
    equity = [0.0]
    seg_pnl = 0.0
    seg: list[float] = []
    for i in range(_WINDOW, n):
        w = spread[i - _WINDOW : i]
        sd = w.std()
        if sd <= 0:
            equity.append(equity[-1])
            continue
        z = (spread[i] - w.mean()) / sd
        daily = pos * (spread[i] - spread[i - 1])
        seg_pnl += daily
        cost = 0.0
        if pos == 0.0 and abs(z) >= _ENTRY_Z:
            pos = -float(np.sign(z))
            seg_pnl = 0.0
            cost = 2 * _LEG_COST
        elif pos != 0.0 and abs(z) <= _EXIT_Z:
            cost = 2 * _LEG_COST
            seg.append(seg_pnl - 4 * _LEG_COST)
            pos = 0.0
        equity.append(equity[-1] + daily - cost)
    eq = np.asarray(equity)
    rets = np.diff(eq)
    sharpe = float(np.mean(rets) / np.std(rets) * np.sqrt(bars_per_year)) if np.std(rets) > 0 else 0.0
    mdd = float(np.max(np.maximum.accumulate(eq) - eq)) if len(eq) else 0.0
    wins = sum(1 for x in seg if x > 0)
    return {
        "trades": len(seg), "win": (wins / len(seg) if seg else 0.0),
        "total": float(eq[-1]), "sharpe": sharpe, "mdd": mdd,
    }


async def main() -> None:
    configure_logging(json_output=False)
    s = load_settings()
    tok = s.broker_api_key.get_secret_value()
    async with ti.AsyncClient(tok, target=_TARGET) as cl:
        uids = {x.ticker: x.uid for x in (await cl.instruments.shares()).instruments}
        print(f"window={_WINDOW} entry_z={_ENTRY_Z} exit_z={_EXIT_Z} leg_cost={_LEG_COST} days={_DAYS}")
        print(f"{'pair':12}{'intv':>7}{'bars':>7}{'days':>6}{'trades':>8}{'t/day':>7}"
              f"{'win%':>6}{'total%':>8}{'Sharpe':>8}")
        for interval_name, interval in _INTERVALS.items():
            for a_t, b_t in _PAIRS:
                if a_t not in uids or b_t not in uids:
                    continue
                a = await _intraday(cl, uids[a_t], interval)
                b = await _intraday(cl, uids[b_t], interval)
                common = sorted(set(a) & set(b))
                if len(common) < _WINDOW + 40:
                    print(f"{a_t + '/' + b_t:12}{interval_name:>7}  thin ({len(common)})")
                    continue
                ap = np.asarray([a[t] for t in common])
                bp = np.asarray([b[t] for t in common])
                spread = np.log(ap) - np.log(bp)
                n_days = len({t.date() for t in common})
                bars_per_day = len(common) / max(n_days, 1)
                r = _backtest(spread, bars_per_year=bars_per_day * 252)
                tpd = r["trades"] / max(n_days, 1)
                print(f"{a_t + '/' + b_t:12}{interval_name:>7}{len(common):>7}{n_days:>6}"
                      f"{r['trades']:>8}{tpd:>7.2f}{r['win'] * 100:>5.0f}%{r['total'] * 100:>7.1f}%"
                      f"{r['sharpe']:>8.2f}")


if __name__ == "__main__":
    asyncio.run(main())
