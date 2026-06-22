"""Historical backtest of the ordinary/preferred stat-arb (READ-ONLY).

The pair of ordinary and preferred shares of one issuer is highly correlated and
roughly market-neutral; their spread mean-reverts. Strategy: when the log-spread
z-score is extreme, short the rich leg / long the cheap leg (dollar-neutral),
close on reversion. Doesn't pay a variance premium and holds for days -- far more
friction-tolerant than the straddle.

Pulls ~2y of real daily candles for every ordinary/preferred pair available on
the account and reports trades, win rate, annualized return, Sharpe and max
drawdown, net of per-leg spread + commission on entry and exit.

    $env:PYTHONPATH = (Get-Location)
    .venv312\\Scripts\\python scripts/statarb_history.py
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta

import numpy as np
import tinkoff.invest as ti

from app.config.settings import load_settings
from app.core.logging import configure_logging, get_logger

logger = get_logger("statarb_history")

_TDAYS = 252.0
_WIN = 60            # rolling window for the z-score
_ENTRY_Z = 2.0      # enter when |z| exceeds this
_EXIT_Z = 0.5       # close when |z| falls back inside this
# Per leg, per side: half-spread + commission (a round trip = 4x). Liquid names
# ~0.0015; illiquid prefs are wider -- override with LEG_COST to stress-test.
_LEG_COST = float(os.environ.get("LEG_COST", "0.0015"))
# Candidate ordinary tickers whose preferred is "<TICKER>P" on MOEX.
_PAIRS = ["SBER", "SNGS", "TATN", "BANE", "RTKM", "MTLR", "NKNC", "KZOS", "LSNG"]


async def _closes(cl: ti.AsyncClient, uid: str, days: int) -> dict:
    now = datetime.now(UTC)
    out: dict = {}
    async for c in cl.get_all_candles(
        instrument_id=uid, from_=now - timedelta(days=days),
        interval=ti.CandleInterval.CANDLE_INTERVAL_DAY,
    ):
        out[c.time.date()] = c.close.units + c.close.nano / 1e9
    return out


def _backtest(ord_px: np.ndarray, pref_px: np.ndarray) -> dict:
    spread = np.log(ord_px) - np.log(pref_px)
    n = len(spread)
    pos = 0.0          # 0 flat, +1 long spread (long ord/short pref), -1 short spread
    equity = [0.0]     # cumulative net P&L in log-spread units
    seg_pnl = 0.0      # P&L of the currently open trade (for win-rate)
    seg_results: list[float] = []
    for i in range(_WIN, n):
        w = spread[i - _WIN : i]
        mu, sd = w.mean(), w.std()
        if sd <= 0:
            equity.append(equity[-1])
            continue
        z = (spread[i] - mu) / sd
        daily = pos * (spread[i] - spread[i - 1])   # mark-to-market of the held position
        seg_pnl += daily
        cost = 0.0
        if pos == 0.0 and abs(z) >= _ENTRY_Z:
            pos = -float(np.sign(z))                # fade the deviation
            seg_pnl = 0.0
            cost = 2 * _LEG_COST                    # enter both legs
        elif pos != 0.0 and abs(z) <= _EXIT_Z:
            cost = 2 * _LEG_COST                    # exit both legs
            seg_results.append(seg_pnl - 4 * _LEG_COST)
            pos = 0.0
        equity.append(equity[-1] + daily - cost)
    eq = np.asarray(equity)
    rets = np.diff(eq)
    sharpe = float(np.mean(rets) / np.std(rets) * np.sqrt(_TDAYS)) if np.std(rets) > 0 else 0.0
    mdd = float(np.max(np.maximum.accumulate(eq) - eq)) if len(eq) else 0.0
    wins = sum(1 for x in seg_results if x > 0)
    ann = float(eq[-1] / (n / _TDAYS)) if n else 0.0
    return {
        "trades": len(seg_results), "win": (wins / len(seg_results) if seg_results else 0.0),
        "total": float(eq[-1]), "ann": ann, "sharpe": sharpe, "mdd": mdd,
    }


async def main() -> None:
    configure_logging(json_output=False)
    s = load_settings()
    tok = s.broker_api_key.get_secret_value()
    async with ti.AsyncClient(tok, target=ti.constants.INVEST_GRPC_API) as cl:
        shares = {x.ticker: x.uid for x in (await cl.instruments.shares()).instruments}
        print(f"{'pair':14}{'days':>6}{'trades':>8}{'win%':>7}{'totalP&L':>10}"
              f"{'ann%':>8}{'Sharpe':>8}{'maxDD':>8}")
        for base in _PAIRS:
            pref = base + "P"
            if base not in shares or pref not in shares:
                continue
            o = await _closes(cl, shares[base], 760)
            p = await _closes(cl, shares[pref], 760)
            common = sorted(set(o) & set(p))
            if len(common) < _WIN + 40:
                print(f"{base}/{pref:9} insufficient overlap ({len(common)})")
                continue
            ord_px = np.asarray([o[d] for d in common])
            pref_px = np.asarray([p[d] for d in common])
            r = _backtest(ord_px, pref_px)
            print(f"{base + '/' + pref:14}{len(common):>6}{r['trades']:>8}"
                  f"{r['win'] * 100:>6.0f}%{r['total'] * 100:>9.1f}%"
                  f"{r['ann'] * 100:>7.1f}%{r['sharpe']:>8.2f}{r['mdd'] * 100:>7.1f}%")
        print("\nP&L is in log-spread units ~= return on 1x gross per leg (dollar-neutral).")
        print("Net of per-leg spread+commission (4x leg-cost per round trip).")


if __name__ == "__main__":
    asyncio.run(main())
