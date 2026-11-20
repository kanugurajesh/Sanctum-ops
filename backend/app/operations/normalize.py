"""ParsedIntent (LLM proposal) -> Operation (typed, validated by code).

Returns the Operation plus, when something is missing or ambiguous, a clarification.
The orchestrator runs the permission check on the Operation *before* asking the
clarifying question, so a user who could never do the action gets a refusal, not a question.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from app.agent.intent import IntentAction, ParsedIntent
from app.operations import dates
from app.operations.model import Action, BillFilter, InventoryFilter, Operation

INVENTORY_ACTIONS = {Action.INVENTORY_QUERY, Action.INVENTORY_UPDATE, Action.INVENTORY_DELETE}
BILL_ACTIONS = {Action.BILLS_QUERY, Action.BILLS_DISCOUNT, Action.BILLS_UPDATE_STATUS, Action.BILLS_DELETE}


@dataclass
class Option:
    label: str
    message: str


@dataclass
class Clarification:
    kind: str                 # e.g. 'unclear', 'scope', 'value', 'access_unspecified', 'target', 'dates'
    question: str
    options: list[Option] = field(default_factory=list)


def _clean(s: str | None) -> str | None:
    if s is None:
        return None
    s = s.strip()
    return s[:100] or None


def _dec(v: float | None) -> Decimal | None:
    if v is None:
        return None
    try:
        return Decimal(str(v)).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def _parse_iso(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def normalize(intent: ParsedIntent, today: date) -> tuple[Operation, Clarification | None]:
    if intent.action == IntentAction.clarify:
        op = Operation(action=Action.HELP, mentioned_hospital=_clean(intent.mentioned_hospital))
        return op, Clarification(
            "unclear",
            intent.clarification_question or "Could you say a little more about what you want to do?",
        )

    action = Action(intent.action.value)
    clar: Clarification | None = None

    date_range = None
    if intent.date_range:
        try:
            date_range = dates.resolve(
                intent.date_range.preset.value, today, n=intent.date_range.n,
                start=_parse_iso(intent.date_range.start), end=_parse_iso(intent.date_range.end),
            )
        except ValueError:
            clar = Clarification("dates", "I couldn't work out the exact dates. Which date range do you mean?")

    op = Operation(
        action=action,
        mode=(intent.mode.value if intent.mode else "list"),
        scope_all=bool(intent.scope_all),
        quantity_op=intent.quantity_op.value if intent.quantity_op else None,
        quantity_value=intent.quantity_value,
        price_op=intent.price_op.value if intent.price_op else None,
        price_value=_dec(intent.price_value),
        discount_pct=_dec(intent.discount_pct),
        new_status=intent.new_status.value if intent.new_status else None,
        target_self=bool(intent.target_self),
        target_user_id=intent.target_user_id,
        target_user_name=_clean(intent.target_user_name),
        role=intent.role.value if intent.role else None,
        permission=intent.permission.value if intent.permission else None,
        approval_request_id=intent.approval_request_id,
        mentioned_hospital=_clean(intent.mentioned_hospital),
        requested_permission=_clean(intent.requested_permission),
    )
    if action == Action.BILLS_DELETE:
        op.requested_permission = "bills.delete"

    if action in INVENTORY_ACTIONS:
        op.inventory = InventoryFilter(
            name_contains=_clean(intent.item_name_contains), category=_clean(intent.category),
            batch_no=_clean(intent.batch_no), expiry=date_range, expired=bool(intent.expired),
            low_stock=bool(intent.low_stock), quantity_lt=intent.quantity_lt,
            quantity_gt=intent.quantity_gt, ids=intent.item_ids or None,
        )
    elif action in BILL_ACTIONS:
        op.bills = BillFilter(
            patient_name_contains=_clean(intent.patient_name_contains),
            status=intent.bill_status.value if intent.bill_status else None,
            bill_date=date_range, amount_gt=_dec(intent.amount_gt), amount_lt=_dec(intent.amount_lt),
            ids=intent.bill_ids or None,
        )

    if clar is None:
        clar = _missing_details(op)
    return op, clar


def _missing_details(op: Operation) -> Clarification | None:
    a = op.action
    # Writes must say which rows: a filter, or an explicit "all".
    if a in (Action.INVENTORY_UPDATE, Action.INVENTORY_DELETE) and op.inventory.is_empty() and not op.scope_all:
        return Clarification("scope", "Which items should this apply to? For example a name, a category, "
                                      "or say 'all items' if you really mean the whole inventory.")
    if a in (Action.BILLS_DISCOUNT, Action.BILLS_UPDATE_STATUS) and op.bills.is_empty() and not op.scope_all:
        return Clarification("scope", "Which bills should this apply to? For example a date range like "
                                      "'last month', a status, or a patient name.")
    if a == Action.INVENTORY_UPDATE:
        if op.quantity_op is None and op.price_op is None:
            return Clarification("value", "What should change — the quantity or the unit price, and to what value?")
        if op.quantity_op and (op.quantity_value is None or op.quantity_value < 0):
            return Clarification("value", "What quantity should I use? It must be zero or more.")
        if op.price_op and (op.price_value is None or op.price_value < 0):
            return Clarification("value", "What price change should I use?")
    if a == Action.BILLS_DISCOUNT and op.discount_pct is None:
        return Clarification("value", "What discount percentage should I apply?")
    if a == Action.BILLS_UPDATE_STATUS and op.new_status is None:
        return Clarification("value", "Which status should the bills be set to: pending, paid, cancelled or refunded?",
                             [Option(s.title(), f"Set them to {s}") for s in ("pending", "paid", "cancelled", "refunded")])
    if a in (Action.ACCESS_GRANT, Action.ACCESS_REVOKE):
        if not (op.target_self or op.target_user_name or op.target_user_id):
            return Clarification("target", "Whose access should I change?")
        if not (op.role or op.permission):
            return Clarification("access_unspecified", "Which access do you mean?")
    if a in (Action.APPROVALS_APPROVE, Action.APPROVALS_REJECT) and op.approval_request_id is None:
        return Clarification("approval_id", "Which approval request do you mean? Say e.g. 'approve request 3'.")
    return None
