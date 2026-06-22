"""Connect to the T-Invest SANDBOX and run read-only checks.

Turnkey entry point to validate the T-Invest adapter end-to-end against the
sandbox. Requires Python 3.12, the SDK, and a sandbox token — none of which are
available in the build environment, so this is NOT run in CI.

Usage (on your machine):
    py -3.12 -m venv .venv
    .venv\\Scripts\\python -m pip install -e ".[dev,tinkoff]"
    # put your sandbox token in .env:  BROKER_API_KEY=t.xxxxxxxx
    .venv\\Scripts\\python scripts/tinkoff_sandbox_check.py

The token is read from the environment (.env); it is never passed on the CLI and
never logged. Sandbox uses virtual money — no real orders are placed.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

from app.brokers.tinkoff import TInvestBrokerAdapter
from app.config.settings import load_settings
from app.core.clock import SystemClock
from app.core.logging import configure_logging, get_logger

logger = get_logger("tinkoff_sandbox_check")


async def main() -> None:
    configure_logging(json_output=False)
    settings = load_settings()  # reads .env (BROKER_API_KEY, etc.)
    adapter = TInvestBrokerAdapter(settings, SystemClock(), sandbox=True)

    await adapter.connect()
    try:
        accounts = await adapter.get_sandbox_accounts()
        account_id = accounts[0] if accounts else await adapter.open_sandbox_account()
        logger.info("using_sandbox_account", account_id=account_id)
        adapter._account_id = account_id

        await adapter.sandbox_pay_in(Decimal("1000000"), currency="rub")
        cash = await adapter.get_cash()
        logger.info("cash", balances=[f"{c.cash} {c.currency}" for c in cash])

        instruments = await adapter.list_instruments()
        logger.info("instruments_total", count=len(instruments))
        for inst in instruments[:5]:
            logger.info(
                "instrument",
                symbol=inst.symbol,
                asset_class=inst.asset_class.value,
                multiplier=str(inst.spec.multiplier),
                tick_size=str(inst.spec.tick_size),
            )
            quote = await adapter.get_quote(inst.symbol)
            logger.info("quote", symbol=inst.symbol, bid=str(quote.bid), ask=str(quote.ask))

        positions = await adapter.get_positions()
        logger.info("positions", count=len(positions))
        logger.info("sandbox_check_ok")
    finally:
        await adapter.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
