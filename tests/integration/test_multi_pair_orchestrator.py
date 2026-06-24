"""Multi-pair (portfolio) orchestrator wiring + control-plane snapshot merge.

A full concurrent multi-stream run isn't simulated on the mock (its price state is
shared across streams); per-pair loop correctness is covered by the single-pair
e2e, and the real multi-stream path is validated against the sandbox.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.api.control import InMemoryControlPlane
from app.brokers.mock import MockBrokerAdapter, MockMarketConfig
from app.config.settings import AppSettings, RiskConfig
from app.core.clock import SimulatedClock
from app.risk import KillSwitch, RiskManager
from workers import MultiPairOrchestrator, MultiPairOrchestratorConfig

_NOW = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)


def _control() -> InMemoryControlPlane:
    clock = SimulatedClock(_NOW)
    broker = MockBrokerAdapter(clock, MockMarketConfig())
    return InMemoryControlPlane(AppSettings(), broker, RiskManager(RiskConfig(), KillSwitch(clock)))


def test_control_pair_snapshot_merges_and_aggregates() -> None:
    c = _control()
    c.set_pair_snapshot("A/B", {"pair_z": 1.0, "opened": True, "pair_pnl": "100"})
    c.set_pair_snapshot("C/D", {"pair_z": -2.0, "opened": False, "pair_pnl": "-30"})
    snap = c._greeks_snapshot
    assert snap["available"] is True
    assert set(snap["pairs"]) == {"A/B", "C/D"}
    assert snap["open_pairs"] == 1
    assert float(snap["total_pair_pnl"]) == 70.0  # 100 + (-30)


def test_multi_pair_builds_labeled_subs() -> None:
    clock = SimulatedClock(_NOW)
    broker = MockBrokerAdapter(clock, MockMarketConfig())
    control = InMemoryControlPlane(
        AppSettings(), broker, RiskManager(RiskConfig(), KillSwitch(clock))
    )
    cfg = MultiPairOrchestratorConfig(
        pairs=[("GAZP", "SNGS", 1.0), ("HYDR", "SNGS", 1.02)],
        window=40,
        entry_z=2.0,
        target_notional_per_leg=Decimal("3000"),
        max_contracts_per_leg=2,
    )
    orch = MultiPairOrchestrator(
        clock, broker, RiskManager(RiskConfig(), KillSwitch(clock)), control, cfg
    )
    assert [s._label for s in orch.subs] == ["GAZP/SNGS", "HYDR/SNGS"]
    assert orch.subs[0]._cfg.symbol_a == "GAZP" and orch.subs[0]._cfg.symbol_b == "SNGS"
    assert orch.subs[1]._cfg.beta == 1.02
    # shared params propagate to every sub.
    assert all(s._cfg.window == 40 and s._cfg.max_contracts_per_leg == 2 for s in orch.subs)
    assert orch.trade_count == 0  # nothing run yet
