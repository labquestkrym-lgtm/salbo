# Deployment

> Live trading is disabled by default and forbidden in dev/test (ADR-0003).

## Topology
- **api** — FastAPI control plane (`app.api:create_app`), health/readiness/status.
- **workers** — asyncio loops (market data, hedge, reconciliation) — wired in
  the orchestration stage.
- **postgres** — persistence (orders, fills, snapshots, audit). Migrations via
  Alembic (persistence stage, R25).
- **redis** — optional caching / pub-sub.

## Local (Docker Compose)
```bash
cp .env.example .env        # fill secrets; never commit .env
make up                     # postgres + redis + api
make logs
make down
```

## Configuration
- Secrets via environment only (`.env` locally; a secrets manager in prod).
- Strategy/risk/execution params via `configs/*.yaml` (validated by Pydantic).
- Mandatory risk limits must be non-zero before live (`RiskConfig`).

## Environments
| Environment | Mode(s)            | Real endpoint? |
|-------------|--------------------|----------------|
| development | backtest/paper     | forbidden      |
| test        | backtest           | forbidden      |
| staging     | sandbox            | sandbox only   |
| production  | sandbox/live       | sandbox or live (all gates) |

## Promotion path
backtest → paper → sandbox → live. Do not skip stages. Each promotion requires a
green test suite, a clean reconciliation, and (for live) the six gates of
ADR-0003 including a one-time `LIVE_CONFIRMATION_CODE`.

## Real broker
`RealBrokerAdapter` is a template (raises `NotImplementedError`). Implement it
against the chosen broker only after the above, keeping the construction and
trade guards intact. See [BROKER_ADAPTER.md](BROKER_ADAPTER.md).

## Observability
Structured JSON logs (secret-redacted). Prometheus metrics endpoint and Grafana
dashboards are wired in the operations stage.
