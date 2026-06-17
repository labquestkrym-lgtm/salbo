"""Prometheus metrics (R30).

Each :class:`Metrics` owns a private ``CollectorRegistry`` so multiple app
instances (and tests) never collide on the global default registry. Gauges that
depend on live analytics are pushed in by the orchestration loop; counters are
incremented at the call sites (commands, fills, hedges, kill-switch trips).
Metrics carry no secrets.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, generate_latest

_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.commands_total = Counter(
            "tradingbot_commands_total",
            "Control-plane commands executed",
            ["action"],
            registry=self.registry,
        )
        self.fills_total = Counter(
            "tradingbot_fills_total", "Fills observed", registry=self.registry
        )
        self.hedges_total = Counter(
            "tradingbot_hedges_total", "Hedge orders placed", registry=self.registry
        )
        self.kill_switch_trips_total = Counter(
            "tradingbot_kill_switch_trips_total", "Kill switch trips", registry=self.registry
        )
        self.net_delta_units = Gauge(
            "tradingbot_net_delta_units",
            "Portfolio net delta (underlying units)",
            registry=self.registry,
        )
        self.net_gamma_units = Gauge(
            "tradingbot_net_gamma_units", "Portfolio net gamma (units)", registry=self.registry
        )
        self.cash_delta = Gauge(
            "tradingbot_cash_delta", "Portfolio cash delta (currency)", registry=self.registry
        )
        self.kill_switch_tripped = Gauge(
            "tradingbot_kill_switch_tripped",
            "1 if the kill switch is tripped, else 0",
            registry=self.registry,
        )

    @property
    def content_type(self) -> str:
        return _CONTENT_TYPE

    def record_command(self, action: str) -> None:
        self.commands_total.labels(action=action).inc()

    def set_portfolio(
        self, *, net_delta_units: float, net_gamma_units: float, cash_delta: float
    ) -> None:
        self.net_delta_units.set(net_delta_units)
        self.net_gamma_units.set(net_gamma_units)
        self.cash_delta.set(cash_delta)

    def set_kill_switch(self, tripped: bool) -> None:
        self.kill_switch_tripped.set(1.0 if tripped else 0.0)

    def render(self) -> bytes:
        return generate_latest(self.registry)
