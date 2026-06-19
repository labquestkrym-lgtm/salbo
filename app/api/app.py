"""FastAPI application factory (R26 + R28).

Liveness/readiness are public (for probes); everything else requires a bearer
token. Dangerous commands (start/stop/pause/hedge/kill-switch/reconcile) are
authenticated, audited, and idempotent (via an ``Idempotency-Key`` header), and
``kill-switch`` requires an explicit confirmation. Live start additionally
enforces the live-trading gate and confirmation code (ADR-0003).
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from pydantic import BaseModel
from starlette.types import Lifespan

from app import __version__
from app.api.audit import AuditSink, InMemoryAuditSink
from app.api.control import ConfirmationRequiredError, ControlPlane
from app.config.settings import AppSettings, load_settings
from app.core.clock import Clock, SystemClock
from app.core.exceptions import LiveTradingNotAuthorizedError, ReconciliationError, TradingBotError
from app.core.logging import configure_logging, get_logger
from app.observability.metrics import Metrics

logger = get_logger(__name__)


class StartRequest(BaseModel):
    confirmation_code: str | None = None


class KillSwitchRequest(BaseModel):
    confirm: bool = False
    reason: str = ""


def create_app(
    settings: AppSettings | None = None,
    *,
    control: ControlPlane | None = None,
    audit: AuditSink | None = None,
    clock: Clock | None = None,
    metrics: Metrics | None = None,
    ws_interval_seconds: float = 1.0,
    lifespan: Lifespan[FastAPI] | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    clock = clock or SystemClock()
    audit = audit or InMemoryAuditSink(clock=clock)
    metrics = metrics or Metrics()
    configure_logging()
    app = FastAPI(title="trading-bot", version=__version__, lifespan=lifespan)
    idempotency: dict[str, dict[str, Any]] = {}

    def require_auth(authorization: Annotated[str | None, Header()] = None) -> str:
        token = settings.api_auth_token.get_secret_value()
        if not token:
            raise HTTPException(status_code=503, detail="API auth not configured")
        if authorization != f"Bearer {token}":
            raise HTTPException(status_code=401, detail="unauthorized")
        return "operator"

    def require_control() -> ControlPlane:
        if control is None:
            raise HTTPException(status_code=503, detail="control plane not wired")
        return control

    AuthDep = Annotated[str, Depends(require_auth)]
    CtrlDep = Annotated[ControlPlane, Depends(require_control)]

    async def run_command(
        *,
        actor: str,
        action: str,
        detail: str,
        idem_key: str | None,
        command: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        # On an idempotent hit we return the cached result WITHOUT building or
        # awaiting the command coroutine (passing a factory avoids creating —
        # and leaking — a coroutine that would never be awaited).
        if idem_key and idem_key in idempotency:
            return idempotency[idem_key]
        result = await command()
        audit.record(actor=actor, action=action, detail=detail)
        metrics.record_command(action)
        if idem_key:
            idempotency[idem_key] = result
        return result

    # --- public probes ------------------------------------------------------
    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/ready")
    async def ready() -> dict[str, Any]:
        return {
            "ready": True,
            "mode": settings.app_mode.value,
            "environment": settings.app_environment.value,
        }

    @app.get("/metrics")
    async def prometheus_metrics() -> Response:
        # Unauthenticated for scraping; metrics carry no secrets (R30).
        return Response(content=metrics.render(), media_type=metrics.content_type)

    # --- authenticated reads ------------------------------------------------
    @app.get("/status")
    async def status(_: AuthDep, ctrl: CtrlDep) -> dict[str, Any]:
        return await ctrl.status()

    @app.get("/positions")
    async def positions(_: AuthDep, ctrl: CtrlDep) -> list[dict[str, Any]]:
        return await ctrl.positions()

    @app.get("/orders")
    async def orders(_: AuthDep, ctrl: CtrlDep) -> list[dict[str, Any]]:
        return await ctrl.orders()

    @app.get("/fills")
    async def fills(_: AuthDep, ctrl: CtrlDep) -> list[dict[str, Any]]:
        return await ctrl.fills()

    @app.get("/portfolio/greeks")
    async def portfolio_greeks(_: AuthDep, ctrl: CtrlDep) -> dict[str, Any]:
        return await ctrl.greeks()

    @app.get("/portfolio/pnl")
    async def portfolio_pnl(_: AuthDep, ctrl: CtrlDep) -> dict[str, Any]:
        return await ctrl.pnl()

    @app.get("/risk")
    async def risk(_: AuthDep, ctrl: CtrlDep) -> dict[str, Any]:
        return await ctrl.risk()

    @app.get("/strategy")
    async def strategy(_: AuthDep, ctrl: CtrlDep) -> dict[str, Any]:
        return await ctrl.strategy()

    @app.get("/config")
    async def config(_: AuthDep) -> dict[str, Any]:
        return {
            "mode": settings.app_mode.value,
            "environment": settings.app_environment.value,
            "live_trading_blockers": settings.live_trading_blockers(),
            "risk": settings.params.risk.model_dump(mode="json"),
        }

    # --- authenticated commands (audited, idempotent) ----------------------
    @app.post("/strategy/start")
    async def strategy_start(
        body: StartRequest,
        actor: AuthDep,
        ctrl: CtrlDep,
        idempotency_key: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        try:
            return await run_command(
                actor=actor,
                action="strategy.start",
                detail=f"mode={settings.app_mode.value}",
                idem_key=idempotency_key,
                command=lambda: ctrl.start(confirmation_code=body.confirmation_code),
            )
        except ConfirmationRequiredError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except LiveTradingNotAuthorizedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @app.post("/strategy/stop")
    async def strategy_stop(
        actor: AuthDep,
        ctrl: CtrlDep,
        idempotency_key: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        return await run_command(
            actor=actor,
            action="strategy.stop",
            detail="",
            idem_key=idempotency_key,
            command=ctrl.stop,
        )

    @app.post("/strategy/pause")
    async def strategy_pause(
        actor: AuthDep,
        ctrl: CtrlDep,
        idempotency_key: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        return await run_command(
            actor=actor,
            action="strategy.pause",
            detail="",
            idem_key=idempotency_key,
            command=ctrl.pause,
        )

    @app.post("/hedge")
    async def hedge(
        actor: AuthDep,
        ctrl: CtrlDep,
        idempotency_key: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        return await run_command(
            actor=actor,
            action="hedge",
            detail="manual hedge",
            idem_key=idempotency_key,
            command=ctrl.hedge,
        )

    @app.post("/kill-switch")
    async def kill_switch(
        body: KillSwitchRequest,
        actor: AuthDep,
        ctrl: CtrlDep,
        idempotency_key: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        if not body.confirm:
            raise HTTPException(status_code=400, detail="kill-switch requires confirm=true")
        result = await run_command(
            actor=actor,
            action="kill_switch",
            detail=body.reason or "manual",
            idem_key=idempotency_key,
            command=lambda: ctrl.trip_kill_switch(reason=body.reason or "manual"),
        )
        metrics.kill_switch_trips_total.inc()
        metrics.set_kill_switch(True)
        return result

    @app.post("/reconcile")
    async def reconcile(
        actor: AuthDep,
        ctrl: CtrlDep,
        idempotency_key: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        try:
            return await run_command(
                actor=actor,
                action="reconcile",
                detail="",
                idem_key=idempotency_key,
                command=ctrl.reconcile,
            )
        except ReconciliationError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    # --- WebSocket stream (quotes/positions/greeks/pnl/risk) ---------------
    @app.websocket("/ws/stream")
    async def ws_stream(websocket: WebSocket) -> None:
        token = settings.api_auth_token.get_secret_value()
        if not token or websocket.headers.get("authorization") != f"Bearer {token}":
            await websocket.close(code=1008)  # policy violation (unauthorized)
            return
        if control is None:
            await websocket.close(code=1011)  # internal error (not wired)
            return
        await websocket.accept()
        try:
            while True:
                await websocket.send_json(
                    {
                        "status": await control.status(),
                        "positions": await control.positions(),
                        "risk": await control.risk(),
                        "greeks": await control.greeks(),
                        "pnl": await control.pnl(),
                    }
                )
                await asyncio.sleep(ws_interval_seconds)
        except WebSocketDisconnect:
            return

    @app.exception_handler(TradingBotError)
    async def _domain_error(_: Any, exc: TradingBotError) -> Any:
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=400, content={"detail": str(exc)})

    logger.info(
        "api_started",
        mode=settings.app_mode.value,
        live_trading_allowed=settings.is_live_trading_allowed(),
    )
    return app
