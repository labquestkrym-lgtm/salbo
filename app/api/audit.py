"""Audit log for control-plane actions (R26).

Every dangerous/trading command is recorded (who, what, when, detail). The sink
is an abstraction so it can be backed by the DB (`audit_log` table) in
production; the in-memory sink is used in tests and as a safe default.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from app.core.clock import Clock
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class AuditRecord:
    timestamp: datetime
    actor: str
    action: str
    detail: str


class AuditSink(Protocol):
    def record(self, *, actor: str, action: str, detail: str) -> AuditRecord: ...
    def list(self) -> list[AuditRecord]: ...


@dataclass(slots=True)
class InMemoryAuditSink:
    clock: Clock
    records: list[AuditRecord] = field(default_factory=list)

    def record(self, *, actor: str, action: str, detail: str) -> AuditRecord:
        rec = AuditRecord(timestamp=self.clock.now(), actor=actor, action=action, detail=detail)
        self.records.append(rec)
        logger.info("audit", actor=actor, action=action, detail=detail)
        return rec

    def list(self) -> list[AuditRecord]:
        return list(self.records)
