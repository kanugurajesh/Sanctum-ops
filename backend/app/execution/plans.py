"""Write planning (the dry run).

A WritePlan lists every row that will change with its exact before/after values. The *same*
plan function runs for the preview and again (with row locks) at confirm time, and the
executor writes exactly the `after` values of the plan, so what you previewed is what runs.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import Connection, func, select

from app.db.tables import bills, inventory_items
from app.execution import directory
from app.execution.queries import bill_conditions, inventory_conditions
from app.operations.model import Action, DateRange, Operation
from app.policy.engine import AccessChange, RowScope, plan_access_change
from app.policy.permissions import PermissionCatalog
from app.policy.principal import Principal, TargetUser
from app.responses import AccessPreview, fmt_inr, fmt_pct, jsonable

CENT = Decimal("0.01")


@dataclass
class RowChange:
    id: int
    label: str
    before: dict | None      # None => row will be inserted (undo of a delete)
    after: dict | None       # None => row will be deleted


@dataclass
class WritePlan:
    entity: str              # 'inventory_items' | 'bills' | 'access'
    action: Action
    title: str
    changes: list[RowChange]
    changed_fields: list[str]
    date_range: DateRange | None = None
    money_label: str | None = None
    money_impact: Decimal | None = None
    notes: list[str] = field(default_factory=list)
    access: AccessPreview | None = None
    access_change: AccessChange | None = None
    target: TargetUser | None = None
    category_counts: dict[str, int] = field(default_factory=dict)

    @property
    def count(self) -> int:
        return len(self.changes)

    def fingerprint(self) -> list:
        """Identity of the exact rows+versions this plan touches; compared at confirm time."""
        if self.entity == "access":
            state = json.dumps(jsonable(self.changes[0].before), sort_keys=True)
            return [[self.changes[0].id, hashlib.sha256(state.encode()).hexdigest()[:16]]]
        return sorted([c.id, (c.before or {}).get("version")] for c in self.changes)

    def money_text(self) -> str | None:
        return fmt_inr(self.money_impact, signed=True) if self.money_impact is not None else None


def _q2(v: Decimal) -> Decimal:
    return v.quantize(CENT, rounding=ROUND_HALF_UP)


def _item_label(r: dict) -> str:
    return f"{r['name']} (batch {r['batch_no']})"


def _bill_label(r: dict) -> str:
    return f"Bill #{r['id']} · {r['patient_name']} · {r['bill_date']:%d %b %Y}"


# ------------------------------------------------------------------ inventory

INVENTORY_VIEW = ("id", "name", "category", "batch_no", "expiry_date", "quantity", "unit_price",
                  "reorder_level", "version")


def _inventory_rows(conn: Connection, p: Principal, op: Operation, today: date, lock: bool) -> list[dict]:
    t = inventory_items
    q = select(*[t.c[c] for c in INVENTORY_VIEW]).where(
        *inventory_conditions(p.hospital_id, op.inventory, today)).order_by(t.c.id)
    if lock:
        q = q.with_for_update()
    return [dict(r) for r in conn.execute(q).mappings().all()]


def plan_inventory_update(conn: Connection, p: Principal, op: Operation, today: date, lock: bool = False) -> WritePlan:
    rows = _inventory_rows(conn, p, op, today, lock)
    changes, fields_used, impact, clamped, unchanged = [], set(), Decimal("0"), 0, 0
    cats: Counter = Counter()
    for r in rows:
        after = dict(r)
        if op.quantity_op:
            v = op.quantity_value or 0
            newq = v if op.quantity_op == "set" else r["quantity"] + v if op.quantity_op == "increase" else r["quantity"] - v
            if newq < 0:
                newq, clamped = 0, clamped + 1
            after["quantity"] = newq
        if op.price_op:
            v = op.price_value or Decimal("0")
            if op.price_op == "set":
                newp = v
            elif op.price_op == "increase_pct":
                newp = r["unit_price"] * (Decimal("100") + v) / Decimal("100")
            else:
                newp = r["unit_price"] * (Decimal("100") - v) / Decimal("100")
            after["unit_price"] = _q2(max(newp, Decimal("0")))
        if after["quantity"] == r["quantity"] and after["unit_price"] == r["unit_price"]:
            unchanged += 1
            continue
        for f in ("quantity", "unit_price"):
            if after[f] != r[f]:
                fields_used.add(f)
        after["version"] = r["version"] + 1
        impact += after["quantity"] * after["unit_price"] - r["quantity"] * r["unit_price"]
        cats[r["category"]] += 1
        changes.append(RowChange(r["id"], _item_label(r), r, after))
    what = []
    if op.quantity_op:
        what.append({"set": f"set quantity to {op.quantity_value}", "increase": f"increase quantity by {op.quantity_value}",
                     "decrease": f"decrease quantity by {op.quantity_value}"}[op.quantity_op])
    if op.price_op:
        what.append({"set": f"set unit price to {fmt_inr(op.price_value)}",
                     "increase_pct": f"raise unit price by {fmt_pct(op.price_value)}",
                     "decrease_pct": f"cut unit price by {fmt_pct(op.price_value)}"}[op.price_op])
    notes = []
    if unchanged:
        notes.append(f"{unchanged} matching item(s) already have this value and are left unchanged.")
    if clamped:
        notes.append(f"{clamped} item(s) would go below zero; their quantity will be set to 0.")
    if op.inventory and op.inventory.expiry:
        notes.append(f"Expiry window: {op.inventory.expiry.label}")
    return WritePlan(
        entity="inventory_items", action=op.action,
        title=f"Inventory update: {' and '.join(what)}",
        changes=changes, changed_fields=sorted(fields_used),
        date_range=op.inventory.expiry if op.inventory else None,
        money_label="Change in stock value", money_impact=_q2(impact), notes=notes,
        category_counts=dict(cats),
    )


def plan_inventory_delete(conn: Connection, p: Principal, op: Operation, today: date, lock: bool = False) -> WritePlan:
    rows = _inventory_rows(conn, p, op, today, lock)
    cats: Counter = Counter(r["category"] for r in rows)
    impact = -sum((r["quantity"] * r["unit_price"] for r in rows), Decimal("0"))
    return WritePlan(
        entity="inventory_items", action=op.action, title="Delete inventory items",
        changes=[RowChange(r["id"], _item_label(r), r, None) for r in rows],
        changed_fields=["(row deleted)"], money_label="Stock value removed", money_impact=_q2(impact),
        notes=["Deleted items can be restored with 'undo my last change' until someone re-uses them."],
        category_counts=dict(cats),
    )


# ------------------------------------------------------------------ bills

BILL_VIEW = ("id", "patient_name", "bill_date", "gross_amount", "discount_pct", "net_amount", "status",
             "created_by", "version")


def _bill_rows(conn: Connection, p: Principal, op: Operation, scope: RowScope, lock: bool) -> list[dict]:
    t = bills
    q = select(*[t.c[c] for c in BILL_VIEW]).where(
        *bill_conditions(p.hospital_id, op.bills, scope, op.action)).order_by(t.c.bill_date, t.c.id)
    if lock:
        q = q.with_for_update()
    return [dict(r) for r in conn.execute(q).mappings().all()]


def plan_bills_discount(conn: Connection, p: Principal, op: Operation, scope: RowScope, lock: bool = False) -> WritePlan:
    rows = _bill_rows(conn, p, op, scope, lock)
    pct = op.discount_pct
    changes, impact, already = [], Decimal("0"), 0
    for r in rows:
        if r["discount_pct"] >= pct:
            already += 1
            continue
        new_net = _q2(r["gross_amount"] * (Decimal("100") - pct) / Decimal("100"))
        after = dict(r, discount_pct=pct, net_amount=new_net, version=r["version"] + 1)
        impact += new_net - r["net_amount"]
        changes.append(RowChange(r["id"], _bill_label(r), r, after))
    notes = [f"Sets the discount to {fmt_pct(pct)} of the gross amount and recalculates the net amount."]
    if already:
        notes.append(f"{already} matching bill(s) already have a discount of {fmt_pct(pct)} or more and are left unchanged.")
    if not (op.bills and op.bills.status):
        excluded = conn.execute(
            select(func.count()).select_from(bills).where(
                *bill_conditions(p.hospital_id, op.bills, scope, None), bills.c.status.in_(("cancelled", "refunded")))
        ).scalar_one()
        if excluded:
            notes.append(f"{excluded} cancelled/refunded bill(s) in this range are excluded.")
    return WritePlan(
        entity="bills", action=op.action, title=f"Apply {fmt_pct(pct)} discount",
        changes=changes, changed_fields=["discount_pct", "net_amount"],
        date_range=op.bills.bill_date if op.bills else None,
        money_label="Total change in billed amount", money_impact=_q2(impact), notes=notes,
    )


def plan_bills_status(conn: Connection, p: Principal, op: Operation, scope: RowScope, lock: bool = False) -> WritePlan:
    rows = _bill_rows(conn, p, op, scope, lock)
    changes, same = [], 0
    for r in rows:
        if r["status"] == op.new_status:
            same += 1
            continue
        changes.append(RowChange(r["id"], _bill_label(r), r, dict(r, status=op.new_status, version=r["version"] + 1)))
    total = sum((c.before["net_amount"] for c in changes), Decimal("0"))
    notes = [f"Net amount of the affected bills: {fmt_inr(total)} (amounts are not changed)."]
    if same:
        notes.append(f"{same} matching bill(s) are already '{op.new_status}' and are left unchanged.")
    return WritePlan(
        entity="bills", action=op.action, title=f"Set bill status to '{op.new_status}'",
        changes=changes, changed_fields=["status"], date_range=op.bills.bill_date if op.bills else None,
        notes=notes,
    )


# ------------------------------------------------------------------ access

def access_state(t: TargetUser) -> dict:
    return {
        "user_id": t.user_id,
        "roles": sorted(t.roles),
        "overrides": sorted(
            [{"permission": o.permission, "effect": o.effect, "constraints": dict(o.constraints)} for o in t.overrides],
            key=lambda o: o["permission"]),
    }


def plan_access(p: Principal, op: Operation, target: TargetUser, catalog: PermissionCatalog) -> WritePlan:
    change = plan_access_change(p, op, target, catalog)
    return _access_plan_from_change(op.action, target, change, catalog, "Access change")


def _access_plan_from_change(action: Action, target: TargetUser, change: AccessChange,
                             catalog: PermissionCatalog, title: str) -> WritePlan:
    from app.policy.permissions import effective_permissions
    before_perms = target.permissions(catalog)
    after_perms = effective_permissions(change.roles_after, catalog.role_grants, change.overrides_after)
    after_target = TargetUser(target.user_id, target.name, target.email, target.is_active,
                              tuple(sorted(change.roles_after)), change.overrides_after)
    preview = AccessPreview(
        user_id=target.user_id, user_name=target.name, email=target.email,
        roles_before=sorted(target.roles), roles_after=sorted(change.roles_after),
        permissions_before=before_perms, permissions_after=after_perms,
        added=sorted(set(after_perms) - set(before_perms)),
        removed=sorted(set(before_perms) - set(after_perms)),
    )
    changes = [] if change.noop else [
        RowChange(target.user_id, f"{target.name} <{target.email}>", access_state(target), access_state(after_target))]
    return WritePlan(
        entity="access", action=action, title=f"{title}: {change.description}",
        changes=changes, changed_fields=["roles", "permission overrides"],
        notes=[change.description], access=preview, access_change=change, target=target,
    )
