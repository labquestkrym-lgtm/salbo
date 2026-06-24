"""Market-wide cointegrated-pair scan + portfolio backtest (READ-ONLY).

Raises activity the RIGHT way: instead of trading one pair faster (intraday loses),
trade MANY daily pairs at once. Scans all pairs across a liquid FORTS universe,
keeps the robust cointegrated ones (ADF on the 1:1 log-spread + both out-of-sample
halves positive), and backtests an equal-weight PORTFOLIO of the top names —
reporting combined trades, annualized return, Sharpe and max drawdown.

Daily signal from continuous share candles; execution via the futures (shortable).
Multiple testing across many pairs inflates in-sample luck, so the OOS filter and
forward (sandbox) validation still matter.

    $env:PYTHONPATH = (Get-Location)
    .venv312\\Scripts\\python scripts/pairs_scan.py
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, date, datetime, timedelta

import numpy as np
import tinkoff.invest as ti

from app.config.settings import load_settings
from app.core.logging import configure_logging, get_logger

logger = get_logger("pairs_scan")

_TDAYS = 252.0
_WINDOW, _ENTRY_Z, _EXIT_Z, _LEG_COST = 60, 2.0, 0.5, 0.002
_DAYS = int(os.environ.get("DAYS", "730"))
_TARGET = (
    ti.constants.INVEST_GRPC_API
    if os.environ.get("TARGET") == "prod"
    else ti.constants.INVEST_GRPC_API_SANDBOX
)
_TOP_K = int(os.environ.get("TOP_K", "10"))
# Liquid MOEX names that have FORTS futures (freely shortable both legs).
_UNIVERSE = [
    "SBER", "GAZP", "LKOH", "ROSN", "SNGS", "TATN", "GMKN", "NVTK", "PLZL", "ALRS",
    "MGNT", "MTSS", "VTBR", "CHMF", "NLMK", "MAGN", "RUAL", "AFLT", "MOEX", "PHOR",
    "SIBN", "AFKS", "IRAO", "HYDR", "RTKM", "PIKK", "SELG", "BSPB", "UPRO", "FEES",
]


def _adf_tstat(s: np.ndarray) -> float:
    ds = np.diff(s)
    m = len(ds)
    y = ds[1:]
    x = np.column_stack([np.ones(m - 1), s[1:m], ds[:-1]])
    beta, *_ = np.linalg.lstsq(x, y, rcond=None)
    resid = y - x @ beta
    dof = (m - 1) - x.shape[1]
    if dof <= 0:
        return 0.0
    cov = (float(resid @ resid) / dof) * np.linalg.inv(x.T @ x)
    return float(beta[1] / np.sqrt(cov[1, 1]))


def _run(spread: np.ndarray) -> tuple[dict, np.ndarray]:
    """z-score reversion backtest; returns metrics + per-step net P&L array."""
    n = len(spread)
    pos = 0.0
    pnl = np.zeros(n)
    seg = 0.0
    trades = wins = 0
    for i in range(_WINDOW, n):
        w = spread[i - _WINDOW : i]
        sd = w.std()
        if sd <= 0:
            continue
        z = (spread[i] - w.mean()) / sd
        day = pos * (spread[i] - spread[i - 1])
        seg += day
        cost = 0.0
        if pos == 0.0 and abs(z) >= _ENTRY_Z:
            pos = -float(np.sign(z))
            seg = 0.0
            cost = 2 * _LEG_COST
            trades += 1
        elif pos != 0.0 and abs(z) <= _EXIT_Z:
            cost = 2 * _LEG_COST
            if seg - 4 * _LEG_COST > 0:
                wins += 1
            pos = 0.0
        pnl[i] = day - cost
    eq = np.cumsum(pnl)
    rets = np.diff(eq)
    sharpe = float(np.mean(rets) / np.std(rets) * np.sqrt(_TDAYS)) if np.std(rets) > 0 else 0.0
    return ({"trades": trades, "win": wins / trades if trades else 0.0,
             "total": float(eq[-1]) if len(eq) else 0.0, "sharpe": sharpe}, pnl)


def _sharpe(x: np.ndarray) -> float:
    return float(np.mean(x) / np.std(x) * np.sqrt(_TDAYS)) if len(x) and np.std(x) > 0 else 0.0


async def _closes(cl: ti.AsyncClient, uid: str) -> dict[date, float]:
    now = datetime.now(UTC)
    for attempt in range(4):
        try:
            out: dict[date, float] = {}
            async for c in cl.get_all_candles(
                instrument_id=uid, from_=now - timedelta(days=_DAYS),
                interval=ti.CandleInterval.CANDLE_INTERVAL_DAY,
            ):
                out[c.time.date()] = c.close.units + c.close.nano / 1e9
            return out
        except Exception:
            await asyncio.sleep(20 * (attempt + 1))
    return {}


async def main() -> None:
    configure_logging(json_output=False)
    s = load_settings()
    tok = s.broker_api_key.get_secret_value()
    async with ti.AsyncClient(tok, target=_TARGET) as cl:
        uids = {x.ticker: x.uid for x in (await cl.instruments.shares()).instruments}
        data: dict[str, dict[date, float]] = {}
        for t in _UNIVERSE:
            if t in uids:
                c = await _closes(cl, uids[t])
                if len(c) > _WINDOW + 120:
                    data[t] = c
        tickers = sorted(data)
        print(f"universe with history: {len(tickers)} names, {len(tickers) * (len(tickers) - 1) // 2} pairs")

        cand: list[tuple] = []  # (min_half_sharpe, name, metrics, pnl_by_date)
        for i in range(len(tickers)):
            for j in range(i + 1, len(tickers)):
                ta, tb = tickers[i], tickers[j]
                common = sorted(set(data[ta]) & set(data[tb]))
                if len(common) < _WINDOW + 120:
                    continue
                ap = np.asarray([data[ta][d] for d in common])
                bp = np.asarray([data[tb][d] for d in common])
                spread = np.log(ap) - np.log(bp)
                if _adf_tstat(spread) > -3.4:  # require ~1% cointegration
                    continue
                full, pnl = _run(spread)
                mid = len(spread) // 2
                h1, _ = _run(spread[:mid])
                h2, _ = _run(spread[mid:])
                if full["total"] <= 0 or h1["sharpe"] <= 0 or h2["sharpe"] <= 0 or full["trades"] < 6:
                    continue
                cand.append((min(h1["sharpe"], h2["sharpe"]), f"{ta}/{tb}", full,
                             dict(zip(common, pnl, strict=True))))

        cand.sort(key=lambda x: x[0], reverse=True)
        print(f"\nrobust cointegrated + both-OOS-positive pairs: {len(cand)}")
        print(f"{'pair':14}{'trades':>7}{'win%':>6}{'total%':>8}{'Sharpe':>8}{'minHalf':>8}")
        for minh, name, m, _ in cand[:_TOP_K]:
            print(f"{name:14}{m['trades']:>7}{m['win'] * 100:>5.0f}%{m['total'] * 100:>7.1f}%"
                  f"{m['sharpe']:>8.2f}{minh:>8.2f}")

        # equal-weight portfolio of the top-K
        top = cand[:_TOP_K]
        if top:
            all_dates = sorted({d for _, _, _, pbd in top for d in pbd})
            port = np.array([
                np.mean([pbd.get(d, 0.0) for _, _, _, pbd in top]) for d in all_dates
            ])
            eq = np.cumsum(port)
            mdd = float(np.max(np.maximum.accumulate(eq) - eq)) if len(eq) else 0.0
            ann = float(eq[-1] / (len(all_dates) / _TDAYS)) if all_dates else 0.0
            total_trades = sum(m["trades"] for _, _, m, _ in top)
            print(f"\n=== PORTFOLIO (equal-weight top {len(top)}) ===")
            print(f"trades total {total_trades} (~{total_trades / (len(all_dates) / _TDAYS):.0f}/yr, "
                  f"~{total_trades / max(len(all_dates), 1) * 1:.2f}/day) | "
                  f"ann {ann * 100:.1f}% | Sharpe {_sharpe(port):.2f} | maxDD {mdd * 100:.1f}%")


if __name__ == "__main__":
    asyncio.run(main())
