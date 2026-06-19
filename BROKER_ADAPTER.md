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
| `RealBrokerAdapter` | Stage 9  | Guarded template; live disabled by default (ADR-0003)      |
| `TInvestBrokerAdapter`| concrete | T-Invest (T-Bank); lazy SDK import, sandbox-first, gated   |

## T-Invest adapter (`app/brokers/tinkoff/`)
Concrete adapter for the T-Invest (T-Bank / Tinkoff Investments) gRPC API.

- **Tested core** (`conversions.py`, `instruments.py`): exact `Quotation`/
  `MoneyValue` ↔ `Decimal`, order-direction/status mapping, and
  future/option → `Instrument`/`ContractSpec` mapping (unit-tested with fakes).
- **Adapter** (`adapter.py`): lazily imports `tinkoff-investments`
  (`pip install ".[tinkoff]"`), so the project imports/tests without it.
  Guards: non-sandbox endpoint refused in dev/test; order methods require all
  live gates unless on sandbox. `order_id = client_order_id` gives broker-side
  idempotency.
- **Not CI-exercised**: networked methods need a token + network. Validate SDK
  call signatures and instrument field names against your installed version on
  **sandbox** first. Streaming / fills / margin / trading-schedule are left as
  explicit `NotImplementedError` until validated.

## Rules every adapter must follow
- **Never** assume option and futures lot sizes/multipliers are equal — always
  read them from `ContractSpec`.
- **Never** treat `last` as a guaranteed execution price; fills cross bid/ask.
- Carry monotonic `sequence` numbers on the quote stream.
- Reject duplicate `client_order_id` (idempotency).
- Surface partial fills; never assume a fill without broker confirmation.
- In `development`/`test`, a `RealBrokerAdapter` must refuse a non-sandbox
  endpoint.
