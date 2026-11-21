"""The JSON schema the LLM (Groq or Gemini) must fill in. This is a *proposal* only: it carries no identity,
no role, no hospital id and no SQL. Code validates and converts it into an Operation."""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class IntentAction(str, Enum):
    inventory_query = "inventory.query"
    inventory_update = "inventory.update"
    inventory_delete = "inventory.delete"
    bills_query = "bills.query"
    bills_discount = "bills.discount"
    bills_update_status = "bills.update_status"
    bills_delete = "bills.delete"
    users_query = "users.query"
    access_show = "access.show"
    access_grant = "access.grant"
    access_revoke = "access.revoke"
    undo = "undo"
    approvals_list = "approvals.list"
    approvals_approve = "approvals.approve"
    approvals_reject = "approvals.reject"
    help = "help"
    unsupported = "unsupported"
    clarify = "clarify"


class DatePreset(str, Enum):
    today = "today"
    yesterday = "yesterday"
    tomorrow = "tomorrow"
    this_week = "this_week"
    last_week = "last_week"
    this_month = "this_month"
    last_month = "last_month"
    this_year = "this_year"
    last_year = "last_year"
    last_n_days = "last_n_days"
    next_n_days = "next_n_days"
    on_date = "on_date"
    custom = "custom"


class QueryMode(str, Enum):
    list = "list"
    count = "count"
    summary = "summary"


class QuantityOp(str, Enum):
    set = "set"
    increase = "increase"
    decrease = "decrease"


class PriceOp(str, Enum):
    set = "set"
    increase_pct = "increase_pct"
    decrease_pct = "decrease_pct"


class BillStatus(str, Enum):
    pending = "pending"
    paid = "paid"
    cancelled = "cancelled"
    refunded = "refunded"


class RoleName(str, Enum):
    admin = "admin"
    billing_manager = "billing_manager"
    pharmacist = "pharmacist"
    front_desk = "front_desk"
    doctor = "doctor"


class PermissionCode(str, Enum):
    inventory_read = "inventory.read"
    inventory_create = "inventory.create"
    inventory_update = "inventory.update"
    inventory_delete = "inventory.delete"
    bills_read = "bills.read"
    bills_create = "bills.create"
    bills_discount = "bills.discount"
    bills_update_status = "bills.update_status"
    bills_delete = "bills.delete"
    users_read = "users.read"
    users_manage_access = "users.manage_access"
    audit_read = "audit.read"


class DateSpec(BaseModel):
    preset: DatePreset
    n: int | None = Field(default=None, description="Number of days for last_n_days / next_n_days")
    start: str | None = Field(default=None, description="YYYY-MM-DD, only for on_date/custom when the user typed explicit dates")
    end: str | None = Field(default=None, description="YYYY-MM-DD, only for custom")


class ParsedIntent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: IntentAction
    clarification_question: str | None = Field(default=None, description="Only when action=clarify")
    mode: QueryMode | None = None
    scope_all: bool | None = Field(default=None, description="True only if the user explicitly said all/entire/every")

    # inventory filters
    item_name_contains: str | None = None
    category: str | None = None
    batch_no: str | None = None
    expired: bool | None = None
    low_stock: bool | None = None
    quantity_lt: int | None = None
    quantity_gt: int | None = None
    item_ids: list[int] | None = None

    # bill filters
    patient_name_contains: str | None = None
    bill_status: BillStatus | None = None
    amount_gt: float | None = None
    amount_lt: float | None = None
    bill_ids: list[int] | None = None

    # time window: bill_date for bills, expiry_date for inventory
    date_range: DateSpec | None = None

    # write values
    quantity_op: QuantityOp | None = None
    quantity_value: int | None = None
    price_op: PriceOp | None = None
    price_value: float | None = None
    discount_pct: float | None = None
    new_status: BillStatus | None = None

    # access management
    target_self: bool | None = Field(default=None, description="True when the user refers to themselves (me/my/myself)")
    target_user_name: str | None = None
    target_user_id: int | None = None
    role: RoleName | None = None
    permission: PermissionCode | None = None

    approval_request_id: int | None = None
    mentioned_hospital: str | None = Field(default=None, description="Any hospital/clinic named in the request, verbatim")
    requested_permission: str | None = Field(default=None, description="For unsupported actions: closest permission code, e.g. users.delete")
