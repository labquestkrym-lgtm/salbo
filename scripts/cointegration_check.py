"""Statistical hardening for the futures pairs candidates (READ-ONLY).

For each candidate pair it reports:
1. ADF t-stat on the traded log-spread log(A)-log(B): more negative => rejects a
   unit root => the spread is mean-reverting (stationary). Critical values
   (constant, no trend): -3.43 (1%), -2.86 (5%), -2.57 (10%).
2. Mean-reversion half-life in trading days (from an AR(1) fit).
3. Engle-Granger hedge ratio beta (OLS log(A)~log(B)) and the ADF on its residual
   -- if beta is far from 1, the tradeable combo isn't a 1:1 spread.
4. Live quoted %spread of each leg's front FORTS future (grounds the friction).

ADF is implemented on numpy (no statsmodels). Compare the stat to the critical
values above, not to a normal p-value.

    $env:PYTHONPATH = (Get-Location)
    .venv312\\Scripts\\python scripts/cointegration_check.py
"""

from __future__ import annotations

import asyncio

import numpy as np
import tinkoff.invest as ti
from scripts.statarb_history import _closes

from app.config.settings import load_settings
from app.core.logging import configure_logging, get_logger

logger = get_logger("cointegration_check")

_PAIRS = [("LKOH", "SNGS"), ("NLMK", "CHMF"), ("GAZP", "NVTK")]
_ADF_5PCT = -2.86


def _adf_tstat(s: np.ndarray, lags: int = 1) -> float:
    """ADF t-stat for gamma in  ds_t = a + gamma*s_{t-1} + sum delta_j ds_{t-j}."""
    s = np.asarray(s, dtype=float)
    ds = np.diff(s)
    m = len(ds)
    p = lags
    y = ds[p:]
    cols = [np.ones(m - p), s[p:m]]
    for j in range(1, p + 1):
        cols.append(ds[p - j : m - j])
    x = np.column_stack(cols)
    beta, *_ = np.linalg.lstsq(x, y, rcond=None)
    resid = y - x @ beta
    dof = (m - p) - x.shape[1]
    if dof <= 0:
        return 0.0
    sigma2 = float(resid @ resid) / dof
    cov = sigma2 * np.linalg.inv(x.T @ x)
    return float(beta[1] / np.sqrt(cov[1, 1]))


def _half_life(s: np.ndarray) -> float:
    s = np.asarray(s, dtype=float)
    x = np.column_stack([np.ones(len(s) - 1), s[:-1]])
    beta, *_ = np.linalg.lstsq(x, s[1:], rcond=None)
    b = beta[1]
    return float(-np.log(2) / np.log(b)) if 0.0 < b < 1.0 else float("inf")


def _eg_beta(log_a: np.ndarray, log_b: np.ndarray) -> tuple[float, np.ndarray]:
    x = np.column_stack([np.ones(len(log_b)), log_b])
    beta, *_ = np.linalg.lstsq(x, log_a, rcond=None)
    resid = log_a - x @ beta
    return float(beta[1]), resid


async def _front_future_spread(cl: ti.AsyncClient, ticker: str) -> float | None:
    futs = [f for f in (await cl.instruments.futures()).instruments if str(f.basic_asset) == ticker]
    if not futs:
        return None
    f = sorted(futs, key=lambda x: x.expiration_date)[0]
    try:
        book = await cl.market_data.get_order_book(instrument_id=f.uid, depth=1)
    except Exception:
        return None
    if not book.bids or not book.asks:
        return None
    bid = book.bids[0].price.units + book.bids[0].price.nano / 1e9
    ask = book.asks[0].price.units + book.asks[0].price.nano / 1e9
    mid = (bid + ask) / 2
    return (ask - bid) / mid if mid > 0 else None


async def main() -> None:
    configure_logging(json_output=False)
    s = load_settings()
    tok = s.broker_api_key.get_secret_value()
    async with ti.AsyncClient(tok, target=ti.constants.INVEST_GRPC_API) as cl:
        uids = {x.ticker: x.uid for x in (await cl.instruments.shares()).instruments}
        for a_t, b_t in _PAIRS:
            if a_t not in uids or b_t not in uids:
                print(f"{a_t}/{b_t}: not found")
                continue
            a = await _closes(cl, uids[a_t], 760)
            b = await _closes(cl, uids[b_t], 760)
            common = sorted(set(a) & set(b))
            ap = np.asarray([a[d] for d in common])
            bp = np.asarray([b[d] for d in common])
            spread = np.log(ap) - np.log(bp)
            adf = _adf_tstat(spread)
            hl = _half_life(spread)
            eg_beta, eg_resid = _eg_beta(np.log(ap), np.log(bp))
            adf_eg = _adf_tstat(eg_resid)
            so = await _front_future_spread(cl, a_t)
            sp = await _front_future_spread(cl, b_t)
            verdict = "STATIONARY (reject unit root)" if adf < _ADF_5PCT else "not stationary at 5%"
            print(f"\n===== {a_t}/{b_t} ({len(common)} days) =====")
            print(f"log-spread ADF t={adf:+.2f} (5% crit -2.86) -> {verdict}")
            print(f"half-life: {hl:.1f} trading days | EG beta {eg_beta:.2f}, residual ADF t={adf_eg:+.2f}")
            print(f"front-future spread: {a_t} "
                  f"{'n/a' if so is None else f'{so * 100:.2f}%'} | {b_t} "
                  f"{'n/a' if sp is None else f'{sp * 100:.2f}%'}")


if __name__ == "__main__":
    asyncio.run(main())
