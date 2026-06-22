"""Robustness validation for the ordinary/preferred stat-arb (READ-ONLY).

Guards against the in-sample optimism of statarb_history.py. For each candidate
pair, at a realistic per-leg cost, it reports:

1. Parameter robustness -- a grid over (window, entry_z, exit_z): the fraction of
   combos that are net-positive and the median Sharpe. A real edge is positive
   across most of the grid; an overfit one only at a single setting.
2. Out-of-sample -- P&L/Sharpe on the first vs second half of the history at base
   params. A robust edge shows up in both halves.
3. Tradeability -- the preferred leg's short_enabled_flag and the live quoted
   %spread of each leg (the strategy must short the rich leg; illiquid prefs may
   not be shortable or may have spreads wider than assumed).

    $env:PYTHONPATH = (Get-Location)
    .venv312\\Scripts\\python scripts/statarb_validate.py
"""

from __future__ import annotations

import asyncio
import statistics

import numpy as np
import tinkoff.invest as ti
from scripts.statarb_history import _backtest, _closes

from app.config.settings import load_settings
from app.core.logging import configure_logging, get_logger

logger = get_logger("statarb_validate")

_CANDIDATES = ["NKNC", "MTLR", "KZOS", "RTKM", "BANE"]
_LEG_COST = 0.005   # realistic round-trip-ish for illiquid prefs
_WINDOWS = [40, 60, 80]
_ENTRY_Z = [1.5, 2.0, 2.5]
_EXIT_Z = [0.0, 0.5, 1.0]


def _spread_pct(book: object) -> float | None:
    bids = getattr(book, "bids", []) or []
    asks = getattr(book, "asks", []) or []
    if not bids or not asks:
        return None
    bid = bids[0].price.units + bids[0].price.nano / 1e9
    ask = asks[0].price.units + asks[0].price.nano / 1e9
    mid = (bid + ask) / 2
    return (ask - bid) / mid if mid > 0 else None


async def main() -> None:
    configure_logging(json_output=False)
    s = load_settings()
    tok = s.broker_api_key.get_secret_value()
    async with ti.AsyncClient(tok, target=ti.constants.INVEST_GRPC_API) as cl:
        shares = {x.ticker: x for x in (await cl.instruments.shares()).instruments}
        for base in _CANDIDATES:
            pref = base + "P"
            if base not in shares or pref not in shares:
                print(f"{base}/{pref}: not found")
                continue
            o = await _closes(cl, shares[base].uid, 760)
            p = await _closes(cl, shares[pref].uid, 760)
            common = sorted(set(o) & set(p))
            if len(common) < max(_WINDOWS) + 60:
                print(f"{base}/{pref}: insufficient overlap ({len(common)})")
                continue
            op = np.asarray([o[d] for d in common])
            pp = np.asarray([p[d] for d in common])

            # 1) parameter-grid robustness
            totals, sharpes = [], []
            for w in _WINDOWS:
                for ez in _ENTRY_Z:
                    for xz in _EXIT_Z:
                        r = _backtest(op, pp, window=w, entry_z=ez, exit_z=xz, leg_cost=_LEG_COST)
                        totals.append(r["total"])
                        sharpes.append(r["sharpe"])
            pos_frac = sum(1 for t in totals if t > 0) / len(totals)

            # 2) out-of-sample halves at base params
            mid = len(common) // 2
            h1 = _backtest(op[:mid], pp[:mid], leg_cost=_LEG_COST)
            h2 = _backtest(op[mid:], pp[mid:], leg_cost=_LEG_COST)

            # 3) tradeability
            short_ok = getattr(shares[pref], "short_enabled_flag", None)
            try:
                so = _spread_pct(await cl.market_data.get_order_book(instrument_id=shares[base].uid, depth=1))
                sp = _spread_pct(await cl.market_data.get_order_book(instrument_id=shares[pref].uid, depth=1))
            except Exception:
                so = sp = None

            print(f"\n===== {base}/{pref} ({len(common)} days) =====")
            print(f"grid robustness: {pos_frac * 100:.0f}% of {len(totals)} combos positive | "
                  f"median Sharpe {statistics.median(sharpes):+.2f}")
            print(f"out-of-sample: H1 total {h1['total'] * 100:+.1f}% (Sharpe {h1['sharpe']:+.2f}) | "
                  f"H2 total {h2['total'] * 100:+.1f}% (Sharpe {h2['sharpe']:+.2f})")
            print(f"short pref enabled: {short_ok} | live spread ord "
                  f"{'n/a' if so is None else f'{so * 100:.2f}%'} / pref "
                  f"{'n/a' if sp is None else f'{sp * 100:.2f}%'}")


if __name__ == "__main__":
    asyncio.run(main())
