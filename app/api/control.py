"""Control plane (R26): the service the API queries and commands.

The API layer is thin and broker/strategy-agnostic — it depends on this
``ControlPlane`` interface. ``InMemoryControlPlane`` wires it to a broker, the
risk manager / kill switch and a strategy run-state, and enforces the
live-trading gate on ``start``. The full live orchestrator implements the same
interface later (ADR-0002), so the API does not change.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol

from app.brokers.base import BaseBrokerAdapter
from app.config.settings import AppSettings
from app.core.enums import KillSwitchPolicy
from app.core.exceptions import LiveTradingNotAuthorizedError
from app.risk import KillSwitchTrigger, RiskManager


class StrategyRunState(StrEnum):
    STOPPED = "stopped"
    RUNNING = "running"
    PAUSED = "paused"


class ConfirmationRequiredError(Exception):
    """A dangerous command lacked its required confirmation."""


class ControlPlane(Protocol):
    async def status(self) -> dict[str, Any]: ...
    async def positions(self) -> list[dict[str, Any]]: ...
    async def orders(self) -> list[dict[str, Any]]: ...
    async def fills(self) -> list[dict[str, Any]]: ...
    async def greeks(self) -> dict[str, Any]: ...
    async def pnl(self) -> dict[str, Any]: ...
    async def risk(self) -> dict[str, Any]: ...
    async def strategy(self) -> dict[str, Any]: ...
    async def start(self, *, confirmation_code: str | None) -> dict[str, Any]: ...
    async def stop(self) -> dict[str, Any]: ...
    async def pause(self) -> dict[str, Any]: ...
    async def hedge(self) -> dict[str, Any]: ...
    async def trip_kill_switch(self, *, reason: str) -> dict[str, Any]: ...
    async def reconcile(self) -> dict[str, Any]: ...


class InMemoryControlPlane:
    """Backed by a broker + risk manager. Greeks/P&L snapshots are pushed in by
    the (future) orchestration loop; here they default to 'unavailable'."""

    def __init__(
        self, settings: AppSettings, broker: BaseBrokerAdapter, risk_manager: RiskManager
    ) -> None:
        self._settings = settings
        self._broker = broker
        self._risk = risk_manager
        self._run_state = StrategyRunState.STOPPED
        self._greeks_snapshot: dict[str, Any] = {"available": False}
        self._pnl_snapshot: dict[str, Any] = {"available": False}

    @property
    def run_state(self) -> StrategyRunState:
        return self._run_state

    # Hooks for the orchestrator to publish latest analytics.
    def set_greeks_snapshot(self, snapshot: dict[str, Any]) -> None:
        self._greeks_snapshot = snapshot

    def set_pair_snapshot(self, label: str, payload: dict[str, Any]) -> None:
        """Merge one pair's snapshot into a multi-pair view (used by the portfolio
        orchestrator so concurrent pairs don't overwrite each other's analytics)."""
        snap = self._greeks_snapshot
        if not snap.get("available"):
            snap = {"available": True, "pairs": {}}
        snap.setdefault("pairs", {})[label] = payload
        pairs = snap["pairs"]
        snap["open_pairs"] = sum(1 for p in pairs.values() if p.get("opened"))
        snap["total_pair_pnl"] = str(sum(float(p.get("pair_pnl", 0)) for p in pairs.values()))
        self._greeks_snapshot = snap

    def set_pnl_snapshot(self, snapshot: dict[str, Any]) -> None:
        self._pnl_snapshot = snapshot

    async def status(self) -> dict[str, Any]:
        return {
            "mode": self._settings.app_mode.value,
            "environment": self._settings.app_environment.value,
            "run_state": self._run_state.value,
            "live_trading_allowed": self._settings.is_live_trading_allowed(),
            "kill_switch_tripped": self._risk.kill_switch.is_tripped,
        }

    async def positions(self) -> list[dict[str, Any]]:
        return [
            {
                "symbol": p.instrument_symbol,
                "quantity": str(p.quantity),
                "average_price": str(p.average_price),
            }
            for p in await self._broker.get_positions()
        ]

    async def orders(self) -> list[dict[str, Any]]:
        return [
            {
                "client_order_id": o.request.client_order_id,
                "symbol": o.request.instrument_symbol,
                "side": o.request.side.value,
                "state": o.state.value,
                "filled_quantity": str(o.filled_quantity),
            }
            for o in await self._broker.get_open_orders()
        ]

    async def fills(self) -> list[dict[str, Any]]:
        return [
            {
                "client_order_id": f.client_order_id,
                "symbol": f.instrument_symbol,
                "side": f.side.value,
                "quantity": str(f.quantity),
                "price": str(f.price),
                "commission": str(f.commission),
            }
            for f in await self._broker.get_fills()
        ]

    async def greeks(self) -> dict[str, Any]:
        return self._greeks_snapshot

    async def pnl(self) -> dict[str, Any]:
        return self._pnl_snapshot

    async def risk(self) -> dict[str, Any]:
        ks = self._risk.kill_switch
        return {
            "kill_switch_tripped": ks.is_tripped,
            "policy": ks.policy.value,
            "events": [{"trigger": e.trigger.value, "detail": e.detail} for e in ks.events],
        }

    async def strategy(self) -> dict[str, Any]:
        return {"run_state": self._run_state.value}

    async def start(self, *, confirmation_code: str | None) -> dict[str, Any]:
        if self._risk.kill_switch.is_tripped:
            raise LiveTradingNotAuthorizedError("kill switch is tripped; reset before starting")
        blockers = self._settings.live_trading_blockers()
        # In non-live modes (backtest/paper/sandbox) we may start without the live gate.
        if self._settings.app_mode.value == "live":
            if blockers:
                raise LiveTradingNotAuthorizedError("; ".join(blockers))
            expected = (
                self._settings.live_confirmation_code.get_secret_value()
                if self._settings.live_confirmation_code
                else None
            )
            if not confirmation_code or confirmation_code != expected:
                raise ConfirmationRequiredError("live start requires the confirmation code")
        self._run_state = StrategyRunState.RUNNING
        return {"run_state": self._run_state.value}

    async def stop(self) -> dict[str, Any]:
        self._run_state = StrategyRunState.STOPPED
        return {"run_state": self._run_state.value}

    async def pause(self) -> dict[str, Any]:
        self._run_state = StrategyRunState.PAUSED
        return {"run_state": self._run_state.value}

    async def hedge(self) -> dict[str, Any]:
        # A real hedge is computed by the orchestrator; here we acknowledge the
        # command (the worker loop performs the actual sizing/execution).
        return {"accepted": True, "run_state": self._run_state.value}

    async def trip_kill_switch(self, *, reason: str) -> dict[str, Any]:
        self._risk.trip(KillSwitchTrigger.MANUAL_STOP, reason)
        self._run_state = StrategyRunState.STOPPED
        policy: KillSwitchPolicy = self._risk.kill_switch.policy
        return {"tripped": True, "policy": policy.value, "run_state": self._run_state.value}

    async def reconcile(self) -> dict[str, Any]:
        broker_positions = await self._broker.get_positions()
        return {
            "ok": True,
            "broker_positions": [
                {"symbol": p.instrument_symbol, "quantity": str(p.quantity)}
                for p in broker_positions
            ],
        }
