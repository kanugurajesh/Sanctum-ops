"""Append-only audit trail. Every exit of the request flow calls record()."""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Connection, insert

from app.db.tables import audit_log
from app.policy.principal import Principal
from app.responses import jsonable


def record(
    conn: Connection, p: Principal, *, request_id: UUID, raw_query: str, decision: str,
    action: str | None = None, parsed_intent: Any = None, resolved_operation: Any = None,
    reason: str | None = None, reason_code: str | None = None, rows_affected: int = 0,
    before: Any = None, after: Any = None, operation_id: UUID | None = None,
) -> int:
    return conn.execute(
        insert(audit_log).values(
            hospital_id=p.hospital_id,     # from the session principal, never from input
            user_id=p.user_id,
            request_id=request_id, operation_id=operation_id,
            raw_query=raw_query[:4000],
            parsed_intent=jsonable(parsed_intent), resolved_operation=jsonable(resolved_operation),
            action=action, decision=decision, reason=reason, reason_code=reason_code,
            rows_affected=rows_affected, before_snapshot=jsonable(before), after_snapshot=jsonable(after),
        ).returning(audit_log.c.id)
    ).scalar_one()
