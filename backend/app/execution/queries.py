"""WHERE-clause builders. hospital_id is always the first condition and always comes from
the Principal, never from the operation. Only allowlisted columns are referenced."""
from __future__ import annotations

from datetime import date

from sqlalchemy import ColumnElement

from app.db.tables import bills, inventory_items
from app.operations.model import Action, BillFilter, InventoryFilter
from app.policy.engine import RowScope


def _like(v: str) -> str:
    v = v.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{v}%"


def inventory_conditions(hospital_id: int, f: InventoryFilter | None, today: date) -> list[ColumnElement]:
    t = inventory_items
    conds: list[ColumnElement] = [t.c.hospital_id == hospital_id]
    if f is None:
        return conds
    if f.name_contains:
        conds.append(t.c.name.ilike(_like(f.name_contains), escape="\\"))
    if f.category:
        conds.append(t.c.category.ilike(_like(f.category), escape="\\"))
    if f.batch_no:
        conds.append(t.c.batch_no.ilike(f.batch_no))
    if f.expiry:
        conds.append(t.c.expiry_date.between(f.expiry.start, f.expiry.end))
    if f.expired:
        conds.append(t.c.expiry_date < today)
    if f.low_stock:
        conds.append(t.c.quantity <= t.c.reorder_level)
    if f.quantity_lt is not None:
        conds.append(t.c.quantity < f.quantity_lt)
    if f.quantity_gt is not None:
        conds.append(t.c.quantity > f.quantity_gt)
    if f.ids:
        conds.append(t.c.id.in_(f.ids))
    return conds


# Discounts / status changes never silently touch closed bills unless the user asked for a status.
_CLOSED = ("cancelled", "refunded")


def bill_conditions(
    hospital_id: int, f: BillFilter | None, scope: RowScope, action: Action | None = None,
) -> list[ColumnElement]:
    t = bills
    conds: list[ColumnElement] = [t.c.hospital_id == hospital_id]
    if scope.created_by is not None:
        conds.append(t.c.created_by == scope.created_by)
    if f is not None:
        if f.patient_name_contains:
            conds.append(t.c.patient_name.ilike(_like(f.patient_name_contains), escape="\\"))
        if f.status:
            conds.append(t.c.status == f.status)
        if f.bill_date:
            conds.append(t.c.bill_date.between(f.bill_date.start, f.bill_date.end))
        if f.amount_gt is not None:
            conds.append(t.c.gross_amount > f.amount_gt)
        if f.amount_lt is not None:
            conds.append(t.c.gross_amount < f.amount_lt)
        if f.ids:
            conds.append(t.c.id.in_(f.ids))
    if action == Action.BILLS_DISCOUNT and not (f and f.status):
        conds.append(t.c.status.not_in(_CLOSED))
    return conds
