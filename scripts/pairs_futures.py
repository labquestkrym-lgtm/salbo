"""Pairs stat-arb on liquid names tradeable via FORTS futures (READ-ONLY).

The ord/pref edge died only because the broker bans shorting the (illiquid) pref.
Liquid large caps that have FORTS futures are freely shortable on either leg and
have tight spreads -- so a mean-reverting spread between two such names is
tradeable. The signal is built from the (continuous) share history; execution
would be via the corresponding futures.

For each same-sector pair this runs the SAME mean-reversion backtest WITH the
validation built in up front (so we don't get fooled by in-sample again):
full-sample Sharpe, parameter-grid robustness, and first/second-half out-of-sample.
A real candidate is positive in-sample, robust across the grid, and positive in
BOTH halves.

    $env:PYTHONPATH = (Get-Location)
    .venv312\\Scripts\\python scripts/pairs_futures.py
"""

from __future__ import annotations

import asyncio
import os

import numpy as np
import tinkoff.invest as ti
from scripts.statarb_history import _backtest, _closes

from app.config.settings import load_settings
from app.core.logging import configure_logging, get_logger

logger = get_logger("pairs_futures")

# Liquid futures: ~1-tick spread + commission per leg/side. Override LEG_COST to stress.
_LEG_COST = float(os.environ.get("LEG_COST", "0.001"))
_WINDOWS = [40, 60, 80]
_ENTRY_Z = [1.5, 2.0, 2.5]
_EXIT_Z = [0.0, 0.5, 1.0]
# Same-sector pairs whose legs both have liquid FORTS futures (freely shortable).
_PAIRS = [
    ("LKOH", "ROSN"), ("LKOH", "SNGS"), ("ROSN", "SNGS"), ("LKOH", "TATN"),
    ("GAZP", "NVTK"), ("SBER", "VTBR"), ("GMKN", "ALRS"), ("MAGN", "NLMK"),
    ("NLMK", "CHMF"), ("MAGN", "CHMF"), ("PLZL", "POLY"), ("ROSN", "TATN"),
]


async def main() -> None:
    configure_logging(json_output=False)
    s = load_settings()
    tok = s.broker_api_key.get_secret_value()
    async with ti.AsyncClient(tok, target=ti.constants.INVEST_GRPC_API) as cl:
        uids = {x.ticker: x.uid for x in (await cl.instruments.shares()).instruments}
        print(f"{'pair':14}{'days':>6}{'full Sh':>9}{'grid+%':>8}{'H1 Sh':>8}{'H2 Sh':>8}"
              f"{'ann%':>8}")
        for a_t, b_t in _PAIRS:
            if a_t not in uids or b_t not in uids:
                print(f"{a_t}/{b_t}: not found")
                continue
            a = await _closes(cl, uids[a_t], 760)
            b = await _closes(cl, uids[b_t], 760)
            common = sorted(set(a) & set(b))
            if len(common) < max(_WINDOWS) + 80:
                print(f"{a_t}/{b_t}: insufficient overlap ({len(common)})")
                continue
            ap = np.asarray([a[d] for d in common])
            bp = np.asarray([b[d] for d in common])

            full = _backtest(ap, bp, leg_cost=_LEG_COST)
            totals = [
                _backtest(ap, bp, window=w, entry_z=ez, exit_z=xz, leg_cost=_LEG_COST)["total"]
                for w in _WINDOWS for ez in _ENTRY_Z for xz in _EXIT_Z
            ]
            pos = sum(1 for t in totals if t > 0) / len(totals)
            mid = len(common) // 2
            h1 = _backtest(ap[:mid], bp[:mid], leg_cost=_LEG_COST)
            h2 = _backtest(ap[mid:], bp[mid:], leg_cost=_LEG_COST)
            print(f"{a_t + '/' + b_t:14}{len(common):>6}{full['sharpe']:>9.2f}"
                  f"{pos * 100:>7.0f}%{h1['sharpe']:>8.2f}{h2['sharpe']:>8.2f}"
                  f"{full['ann'] * 100:>7.1f}%")
        print("\nCandidate = full Sharpe>0 AND grid+% high AND BOTH halves Sharpe>0.")
        print("Both legs tradeable via FORTS futures (freely shortable); friction ~0.1%/leg.")


if __name__ == "__main__":
    asyncio.run(main())
