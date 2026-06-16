# Broker Adapter

A broker adapter translates between a broker's API and our domain models. It
contains **no** strategy, risk, or persistence logic (separation of concerns).

## Interface (`app/brokers/base.py`)
`BaseBrokerAdapter` (ABC, all trading methods `async`):

- **Lifecycle**: `connect`, `disconnect`, `is_connected`, `reconnect`.
- **Reference data**: `list_instruments`, `get_contract_spec`, `get_trading_session`.
- **Market data**: `get_quote`, `stream_quotes` (async iterator with monotonic
  `sequence` for gap detection).
- **Account**: `get_positions`, `get_cash`, `get_margin`.
- **Orders**: `place_order`, `cancel_order`, `get_order`, `get_open_orders`,
  `get_fills`, optional `modify_order`.

`BrokerCapabilities` advertises optional features (order modify, Greeks/IV,
streaming, open interest) so higher layers adapt instead of assuming.

## Implementations
| Adapter            | Status   | Purpose                                                    |
|--------------------|----------|------------------------------------------------------------|
| `MockBrokerAdapter`| ✅ Stage 4 | Deterministic GBM market for backtests/tests; bid/ask fills |
| `PaperBrokerAdapter`| Stage 6  | Real market data, simulated fills (queue/latency/partials) |
| `RealBrokerAdapter` | Stage 9  | A concrete broker; live disabled by default (ADR-0003)     |

## Rules every adapter must follow
- **Never** assume option and futures lot sizes/multipliers are equal — always
  read them from `ContractSpec`.
- **Never** treat `last` as a guaranteed execution price; fills cross bid/ask.
- Carry monotonic `sequence` numbers on the quote stream.
- Reject duplicate `client_order_id` (idempotency).
- Surface partial fills; never assume a fill without broker confirmation.
- In `development`/`test`, a `RealBrokerAdapter` must refuse a non-sandbox
  endpoint.
