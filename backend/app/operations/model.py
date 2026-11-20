"""Typed operations: the only thing the policy and execution layers ever act on.

The LLM's ParsedIntent is converted into an Operation by `operations.normalize` (code).
Every field here is a closed enum or a typed scalar; nothing is free-form SQL.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class Action(str, Enum):
    INVENTORY_QUERY = "inventory.query"
    INVENTORY_UPDATE = "inventory.update"
    INVENTORY_DELETE = "inventory.delete"
    BILLS_QUERY = "bills.query"
    BILLS_DISCOUNT = "bills.discount"
    BILLS_UPDATE_STATUS = "bills.update_status"
    BILLS_DELETE = "bills.delete"
    USERS_QUERY = "users.query"
    ACCESS_SHOW = "access.show"
    ACCESS_GRANT = "access.grant"
    ACCESS_REVOKE = "access.revoke"
    UNDO = "undo"
    APPROVALS_LIST = "approvals.list"
    APPROVALS_APPROVE = "approvals.approve"
    APPROVALS_REJECT = "approvals.reject"
    HELP = "help"
    UNSUPPORTED = "unsupported"


READ_ACTIONS = {
    Action.INVENTORY_QUERY, Action.BILLS_QUERY, Action.USERS_QUERY, Action.ACCESS_SHOW,
    Action.APPROVALS_LIST, Action.HELP,
}
WRITE_ACTIONS = {
    Action.INVENTORY_UPDATE, Action.INVENTORY_DELETE, Action.BILLS_DISCOUNT,
    Action.BILLS_UPDATE_STATUS, Action.BILLS_DELETE, Action.ACCESS_GRANT, Action.ACCESS_REVOKE,
    Action.UNDO, Action.APPROVALS_APPROVE, Action.APPROVALS_REJECT,
}

BILL_STATUSES = ("pending", "paid", "cancelled", "refunded")


class DateRange(BaseModel):
    """Inclusive date range, already resolved in Asia/Kolkata."""
    start: date
    end: date
    preset: str = "custom"

    @property
    def label(self) -> str:
        fmt = lambda d: f"{d.day} {d.strftime('%b %Y')}"  # noqa: E731
        if self.start == self.end:
            return f"{fmt(self.start)} (IST)"
        return f"{fmt(self.start)} – {fmt(self.end)} (IST)"


class InventoryFilter(BaseModel):
    name_contains: str | None = None
    category: str | None = None
    batch_no: str | None = None
    expiry: DateRange | None = None          # expiry_date within range
    expired: bool = False                    # expiry_date before today
    low_stock: bool = False                  # quantity <= reorder_level
    quantity_lt: int | None = None
    quantity_gt: int | None = None
    ids: list[int] | None = None

    def is_empty(self) -> bool:
        return not any([
            self.name_contains, self.category, self.batch_no, self.expiry, self.expired,
            self.low_stock, self.quantity_lt is not None, self.quantity_gt is not None, self.ids,
        ])


class BillFilter(BaseModel):
    patient_name_contains: str | None = None
    status: Literal["pending", "paid", "cancelled", "refunded"] | None = None
    bill_date: DateRange | None = None
    amount_gt: Decimal | None = None
    amount_lt: Decimal | None = None
    ids: list[int] | None = None

    def is_empty(self) -> bool:
        return not any([
            self.patient_name_contains, self.status, self.bill_date,
            self.amount_gt is not None, self.amount_lt is not None, self.ids,
        ])


class Operation(BaseModel):
    action: Action
    mode: Literal["list", "count", "summary"] = "list"
    scope_all: bool = False                  # user explicitly said "all"/"entire"

    inventory: InventoryFilter | None = None
    bills: BillFilter | None = None

    quantity_op: Literal["set", "increase", "decrease"] | None = None
    quantity_value: int | None = None
    price_op: Literal["set", "increase_pct", "decrease_pct"] | None = None
    price_value: Decimal | None = None
    discount_pct: Decimal | None = None
    new_status: Literal["pending", "paid", "cancelled", "refunded"] | None = None

    # access management
    target_self: bool = False
    target_user_id: int | None = None
    target_user_name: str | None = None
    role: str | None = None
    permission: str | None = None

    approval_request_id: int | None = None
    mentioned_hospital: str | None = None
    requested_permission: str | None = None  # for unsupported operations, e.g. "bills.delete"

    # set by code, never by the LLM
    undo_audit_id: int | None = Field(default=None)

    @property
    def is_write(self) -> bool:
        return self.action in WRITE_ACTIONS
