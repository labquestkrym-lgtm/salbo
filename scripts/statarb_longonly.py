"""Long-only ord/pref tilt -- no shorting (READ-ONLY).

The market-neutral pair needs shorting the rich leg, which the broker disallows
on the robust names. This tests a long-only alternative: hold 50/50 ordinary/
preferred as a baseline, and when the log-spread z-score is extreme, tilt to
100% of the CHEAP leg; revert to 50/50 on mean-reversion.

Reports, per pair, the tilt's ALPHA over the 50/50 baseline (the spread signal's
contribution -- market beta cancels in the difference), its Sharpe, and the
first/second-half split. Long-only carries market risk, so absolute tilt/baseline
returns are shown too. Net of switching cost (leg_cost on each tilt in/out).

    $env:PYTHONPATH = (Get-Location)
    .venv312\\Scripts\\python scripts/statarb_longonly.py
"""

from __future__ import annotations

import asyncio

import numpy as np
import tinkoff.invest as ti
from scripts.statarb_history import _closes

from app.config.settings import load_settings
from app.core.logging import configure_logging, get_logger

logger = get_logger("statarb_longonly")

_TDAYS = 252.0
_WIN, _ENTRY_Z, _EXIT_Z, _LEG_COST = 60, 2.0, 0.5, 0.005
_PAIRS = ["NKNC", "KZOS", "MTLR", "RTKM", "BANE", "SNGS", "TATN"]


def _alpha_curve(ord_px: np.ndarray, pref_px: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Daily alpha (tilt minus 50/50), tilt return, baseline return -- no lookahead:
    the weight for day i+1 is set from the z-score known at day i's close."""
    spread = np.log(ord_px) - np.log(pref_px)
    n = len(spread)
    state = "flat"  # flat=50/50; "ord"=100% ordinary; "pref"=100% preferred
    alpha, tilt, base = [], [], []
    for i in range(_WIN, n - 1):
        w = spread[i - _WIN : i]
        sd = w.std()
        if sd <= 0:
            continue
        z = (spread[i] - w.mean()) / sd
        prev = state
        if state == "flat" and abs(z) >= _ENTRY_Z:
            state = "pref" if z > 0 else "ord"   # z>0: ord rich -> hold the cheap pref
        elif state != "flat" and abs(z) <= _EXIT_Z:
            state = "flat"
        w_ord = 1.0 if state == "ord" else 0.0 if state == "pref" else 0.5
        r_o = ord_px[i + 1] / ord_px[i] - 1.0
        r_p = pref_px[i + 1] / pref_px[i] - 1.0
        t_ret = w_ord * r_o + (1 - w_ord) * r_p
        b_ret = 0.5 * (r_o + r_p)
        cost = _LEG_COST if state != prev else 0.0   # switching turnover
        tilt.append(t_ret - cost)
        base.append(b_ret)
        alpha.append((t_ret - cost) - b_ret)
    return np.asarray(alpha), np.asarray(tilt), np.asarray(base)


def _sharpe(x: np.ndarray) -> float:
    return float(np.mean(x) / np.std(x) * np.sqrt(_TDAYS)) if len(x) and np.std(x) > 0 else 0.0


async def main() -> None:
    configure_logging(json_output=False)
    s = load_settings()
    tok = s.broker_api_key.get_secret_value()
    async with ti.AsyncClient(tok, target=ti.constants.INVEST_GRPC_API) as cl:
        shares = {x.ticker: x.uid for x in (await cl.instruments.shares()).instruments}
        print(f"{'pair':14}{'alpha%':>8}{'aSharpe':>9}{'H1 a%':>8}{'H2 a%':>8}"
              f"{'tilt%':>8}{'base%':>8}")
        for base in _PAIRS:
            pref = base + "P"
            if base not in shares or pref not in shares:
                continue
            o = await _closes(cl, shares[base], 760)
            p = await _closes(cl, shares[pref], 760)
            common = sorted(set(o) & set(p))
            if len(common) < _WIN + 80:
                continue
            op = np.asarray([o[d] for d in common])
            pp = np.asarray([p[d] for d in common])
            a, t, b = _alpha_curve(op, pp)
            if len(a) == 0:
                continue
            mid = len(a) // 2
            print(f"{base + '/' + pref:14}{a.sum() * 100:>7.1f}%{_sharpe(a):>9.2f}"
                  f"{a[:mid].sum() * 100:>7.1f}%{a[mid:].sum() * 100:>7.1f}%"
                  f"{t.sum() * 100:>7.1f}%{b.sum() * 100:>7.1f}%")
        print("\nalpha = tilt minus 50/50 baseline (signal contribution; long-only, no short).")
        print("tilt/base are absolute cumulative returns -- both carry market beta.")


if __name__ == "__main__":
    asyncio.run(main())
