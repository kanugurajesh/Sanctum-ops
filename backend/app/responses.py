"""API response shapes. The agent always returns exactly one of these types."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

ResponseType = Literal["answer", "clarification", "refusal", "preview", "executed", "cancelled", "error"]


class OptionOut(BaseModel):
    label: str
    message: str


class SuggestionOut(BaseModel):
    label: str
    message: str | None = None
    kind: str = "query"            # 'query' -> send message; 'request_approval' -> POST /approvals
    audit_id: int | None = None


class TableOut(BaseModel):
    columns: list[str]
    rows: list[list[Any]]


class AnswerBody(BaseModel):
    title: str
    summary: list[str] = Field(default_factory=list)
    table: TableOut | None = None
    total: int | None = None
    date_range: str | None = None
    notes: list[str] = Field(default_factory=list)


class ClarificationBody(BaseModel):
    question: str
    options: list[OptionOut] = Field(default_factory=list)


class RefusalBody(BaseModel):
    code: str
    reason: str
    suggestions: list[SuggestionOut] = Field(default_factory=list)


class SampleRow(BaseModel):
    id: int
    label: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None


class AccessPreview(BaseModel):
    user_id: int
    user_name: str
    email: str
    roles_before: list[str]
    roles_after: list[str]
    permissions_before: dict[str, dict]
    permissions_after: dict[str, dict]
    added: list[str]
    removed: list[str]


class PreviewBody(BaseModel):
    operation_id: UUID
    action: str
    title: str
    rows_affected: int
    changed_fields: list[str]
    samples: list[SampleRow]
    date_range: str | None = None
    money_label: str | None = None
    money_impact: str | None = None
    requires_typed_count: bool
    expires_at: datetime
    notes: list[str] = Field(default_factory=list)
    access: AccessPreview | None = None


class ExecutedBody(BaseModel):
    operation_id: UUID
    title: str
    rows_affected: int
    audit_id: int
    undoable: bool = True


class AgentResponse(BaseModel):
    type: ResponseType
    request_id: UUID
    message: str
    intent: dict | None = None
    answer: AnswerBody | None = None
    clarification: ClarificationBody | None = None
    refusal: RefusalBody | None = None
    preview: PreviewBody | None = None
    executed: ExecutedBody | None = None
    audit_id: int | None = None


# ------------------------------------------------------------- formatting helpers

def fmt_inr(amount: Decimal | float | int, signed: bool = False) -> str:
    """Indian digit grouping: ₹12,34,567.89"""
    d = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    sign = "-" if d < 0 else ("+" if signed and d > 0 else "")
    d = abs(d)
    whole, frac = f"{d:.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"{sign}₹{whole}.{frac}"


def fmt_pct(v: Decimal | float | int) -> str:
    """20.00 -> '20%', 12.50 -> '12.5%'"""
    d = Decimal(str(v)).normalize()
    return f"{d:f}%"


def jsonable(v: Any) -> Any:
    if isinstance(v, dict):
        return {k: jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [jsonable(x) for x in v]
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    if isinstance(v, UUID):
        return str(v)
    return v
