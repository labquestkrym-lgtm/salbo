"""Print your T-Invest accounts (read-only) so you can find BROKER_ACCOUNT_ID.

Connects with the token from .env and lists every account the token can see —
id, name, type and status. It only READS (users.get_accounts); it never places
an order or moves money. Copy the ``id`` of your live brokerage account into
``.env`` as BROKER_ACCOUNT_ID.

Usage (on your machine, in the venv that has the SDK):
    $env:PYTHONPATH = (Get-Location)
    .venv312\\Scripts\\python scripts/tinkoff_accounts.py

The token is read from .env (BROKER_API_KEY); it is never passed on the CLI and
never logged. Requires APP_ENVIRONMENT=production in .env to hit the live API.
"""

from __future__ import annotations

import asyncio

from app.brokers.tinkoff import TInvestBrokerAdapter
from app.config.settings import load_settings
from app.core.clock import SystemClock
from app.core.logging import configure_logging, get_logger

logger = get_logger("tinkoff_accounts")


async def main() -> None:
    configure_logging(json_output=False)
    settings = load_settings()  # reads .env (BROKER_API_KEY, APP_ENVIRONMENT)
    # sandbox=False -> live endpoint. Read-only: we only call users.get_accounts().
    adapter = TInvestBrokerAdapter(settings, SystemClock(), sandbox=False)

    await adapter.connect()
    try:
        resp = await adapter._svc().users.get_accounts()
        accounts = list(resp.accounts)
        if not accounts:
            logger.warning("no_accounts_found", hint="token may lack account access")
            return
        logger.info("accounts_total", count=len(accounts))
        for a in accounts:
            logger.info(
                "account",
                id=str(a.id),  # <-- this is your BROKER_ACCOUNT_ID
                name=str(getattr(a, "name", "")),
                type=str(getattr(a, "type", "")),
                status=str(getattr(a, "status", "")),
            )
    finally:
        await adapter.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
