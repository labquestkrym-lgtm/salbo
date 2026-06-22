"""Historical edge check for the delta-hedged long straddle (READ-ONLY).

Answers the question the live test did NOT: is buying vol on these names
profitable, net of frictions? Pulls ~2y of real daily candles for SBER & GAZP
from T-Invest and:

1. Realized vol (RV): distribution + whether trailing-30d RV predicts the next
   30d RV (the skill our naive entry forecast relies on).
2. A delta-hedged long ATM straddle simulated on the REAL price path (BSM, daily
   rehedge, option + hedge frictions), entered every few days and held ~21
   trading days, swept across a range of ENTRY IV. Output: the break-even entry
   IV (mean net P&L = 0) vs the realized vol actually delivered.

Caveat: historical option IV is not available from the API, so entry IV is a
swept parameter, not market data. The variance risk premium (IV > RV on average)
means real entry IV sits ABOVE realized — read the break-even accordingly.

    $env:PYTHONPATH = (Get-Location)
    .venv312\\Scripts\\python scripts/vol_history.py
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import numpy as np
import tinkoff.invest as ti

from app.config.settings import load_settings
from app.core.enums import OptionType
from app.core.logging import configure_logging, get_logger
from app.pricing import bsm

logger = get_logger("vol_history")

_TDAYS = 252.0
_HOLD = 21          # trading days held (~1 month option)
_RV_WIN = 21        # realized-vol window
_RATE = 0.18
# Frictions calibrated to observed GAZP quotes:
_OPT_SPREAD = 0.04  # half-spread per option leg, as fraction of premium (entry & exit)
_HEDGE_COST = 0.001  # per hedge rebalance, fraction of traded share notional
_ENTRY_STEP = 3     # start a new straddle every N trading days
_IV_GRID = [0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80]


async def _closes(cl: ti.AsyncClient, uid: str, days: int) -> tuple[list, np.ndarray]:
    now = datetime.now(UTC)
    frm = now - timedelta(days=days)
    dts, px = [], []
    async for c in cl.get_all_candles(
        instrument_id=uid, from_=frm, to=now, interval=ti.CandleInterval.CANDLE_INTERVAL_DAY
    ):
        dts.append(c.time.date())
        px.append(c.close.units + c.close.nano / 1e9)
    return dts, np.asarray(px, dtype=float)


def _realized_vol(logret: np.ndarray) -> float:
    if len(logret) < 2:
        return 0.0
    return float(np.sqrt(_TDAYS * np.mean(logret**2)))


def _forecast_skill(px: np.ndarray) -> tuple[float, float, float]:
    """corr(trailing RV, forward RV); plus mean trailing vs mean forward when the
    trailing RV is in its top quartile (does buying after high vol pay?)."""
    r = np.diff(np.log(px))
    trail, fwd = [], []
    for i in range(_RV_WIN, len(r) - _HOLD):
        trail.append(_realized_vol(r[i - _RV_WIN : i]))
        fwd.append(_realized_vol(r[i : i + _HOLD]))
    trail, fwd = np.asarray(trail), np.asarray(fwd)
    if len(trail) < 10:
        return 0.0, 0.0, 0.0
    corr = float(np.corrcoef(trail, fwd)[0, 1])
    hi = trail >= np.quantile(trail, 0.75)
    return corr, float(fwd[hi].mean()), float(fwd.mean())


def _straddle_net_pct(px: np.ndarray, start: int, iv: float) -> float | None:
    """Net P&L of a daily delta-hedged long ATM straddle entered at `start`,
    held _HOLD trading days, priced/hedged at constant `iv`, as a fraction of the
    premium paid. Includes option entry/exit spread and per-rehedge cost."""
    if start + _HOLD >= len(px):
        return None
    s0 = float(px[start])
    k = s0
    c0 = bsm(spot=s0, strike=k, t=_HOLD / _TDAYS, rate=_RATE, sigma=iv, option_type=OptionType.CALL)
    p0 = bsm(spot=s0, strike=k, t=_HOLD / _TDAYS, rate=_RATE, sigma=iv, option_type=OptionType.PUT)
    prem = c0.price + p0.price
    if prem <= 0:
        return None
    cash = -prem - _OPT_SPREAD * prem        # buy straddle + entry half-spread
    h_prev = 0.0
    for d in range(_HOLD):
        s = float(px[start + d])
        tau = max((_HOLD - d) / _TDAYS, 1e-6)
        delta = (
            bsm(spot=s, strike=k, t=tau, rate=_RATE, sigma=iv, option_type=OptionType.CALL).delta
            + bsm(spot=s, strike=k, t=tau, rate=_RATE, sigma=iv, option_type=OptionType.PUT).delta
        )
        h = -delta                            # hedge shares to neutralize straddle delta
        trade = h - h_prev
        cash -= trade * s                     # rebalance hedge
        cash -= abs(trade) * s * _HEDGE_COST  # hedge friction
        h_prev = h
    st = float(px[start + _HOLD])
    intrinsic = abs(st - k)                   # straddle value at exit (~intrinsic)
    cash += intrinsic - _OPT_SPREAD * prem    # sell straddle + exit half-spread
    cash += h_prev * st                       # close hedge
    cash -= abs(h_prev) * st * _HEDGE_COST
    return cash / prem


def _simulate(px: np.ndarray, iv: float) -> tuple[float, float, float]:
    res = [_straddle_net_pct(px, i, iv) for i in range(_RV_WIN, len(px) - _HOLD, _ENTRY_STEP)]
    arr = np.asarray([x for x in res if x is not None])
    if len(arr) == 0:
        return 0.0, 0.0, 0.0
    return float(arr.mean()), float(np.median(arr)), float((arr > 0).mean())


async def main() -> None:
    configure_logging(json_output=False)
    s = load_settings()
    tok = s.broker_api_key.get_secret_value()
    async with ti.AsyncClient(tok, target=ti.constants.INVEST_GRPC_API) as cl:
        shares = {x.ticker: x.uid for x in (await cl.instruments.shares()).instruments
                  if x.ticker in ("SBER", "GAZP")}
        for tic, uid in shares.items():
            dts, px = await _closes(cl, uid, days=760)
            if len(px) < _RV_WIN + _HOLD + 10:
                print(f"{tic}: not enough history ({len(px)} candles)")
                continue
            r = np.diff(np.log(px))
            rv_all = _realized_vol(r)
            corr, fwd_hi, fwd_mean = _forecast_skill(px)
            print(f"\n===== {tic} =====  {dts[0]} .. {dts[-1]}  ({len(px)} daily candles)")
            print(f"realized vol (whole period, annualized): {rv_all * 100:.1f}%")
            print(f"trailing->forward RV corr: {corr:+.2f}  "
                  f"(skill of buying on high vol)")
            print(f"forward 30d RV when trailing in top quartile: {fwd_hi * 100:.1f}%  "
                  f"vs unconditional {fwd_mean * 100:.1f}%")
            print(f"{'entry IV':>9}{'mean net %prem':>16}{'median':>10}{'win rate':>10}")
            prev_mean = None
            breakeven = None
            for iv in _IV_GRID:
                m, med, win = _simulate(px, iv)
                print(f"{iv * 100:>8.0f}%{m * 100:>15.1f}%{med * 100:>9.1f}%{win * 100:>9.0f}%")
                if prev_mean is not None and prev_mean >= 0 >= m and breakeven is None:
                    # linear interp of the IV where mean net P&L crosses zero
                    lo = _IV_GRID[_IV_GRID.index(iv) - 1]
                    breakeven = lo + (iv - lo) * (prev_mean / (prev_mean - m))
                prev_mean = m
            if breakeven:
                print(f"~break-even entry IV (mean P&L=0): {breakeven * 100:.0f}%  "
                      f"-> profitable only if you buy BELOW this")


if __name__ == "__main__":
    asyncio.run(main())
