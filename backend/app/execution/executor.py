"""Applies a WritePlan inside the caller's transaction, and builds undo plans.

Every UPDATE/DELETE is keyed by (id, hospital_id, version) and must hit exactly one row;
otherwise StaleDataError is raised and the caller's transaction rolls back as a whole.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import Connection, delete, func, insert, select, text, update

from app.db.tables import audit_log, bills, inventory_items, user_permission_overrides, user_roles
from app.execution import directory
from app.execution.plans import (
    BILL_VIEW, INVENTORY_VIEW, RowChange, WritePlan, _bill_label, _item_label, access_state,
)
from app.operations.model import Action
from app.policy.engine import AccessChange
from app.policy.permissions import Override, PermissionCatalog
from app.policy.principal import Principal

TABLES = {"inventory_items": inventory_items, "bills": bills}
UNDOABLE_ACTIONS = {
    Action.INVENTORY_UPDATE.value, Action.INVENTORY_DELETE.value, Action.BILLS_DISCOUNT.value,
    Action.BILLS_UPDATE_STATUS.value, Action.ACCESS_GRANT.value, Action.ACCESS_REVOKE.value,
}


class StaleDataError(Exception):
    pass


def lock_access(conn: Connection, user_id: int) -> None:
    """Serialise concurrent access changes for one user (transaction-scoped advisory lock)."""
    conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('access:' || :u))"), {"u": str(user_id)})


def apply_plan(conn: Connection, p: Principal, plan: WritePlan) -> int:
    if plan.entity in TABLES:
        return _apply_rows(conn, p, plan)
    if plan.entity == "access":
        ch = plan.changes[0]
        _apply_access(conn, p, ch.id, ch.before, ch.after)
        return 1
    raise ValueError(f"unknown entity {plan.entity}")


def _apply_rows(conn: Connection, p: Principal, plan: WritePlan) -> int:
    t = TABLES[plan.entity]
    for ch in plan.changes:
        if ch.after is None:
            res = conn.execute(delete(t).where(
                t.c.id == ch.id, t.c.hospital_id == p.hospital_id, t.c.version == ch.before["version"]))
        elif ch.before is None:
            exists = conn.execute(select(t.c.id).where(t.c.id == ch.id)).first()
            if exists:
                raise StaleDataError(f"row {ch.id} already exists")
            vals = {k: v for k, v in ch.after.items() if k in t.c}
            vals.update(hospital_id=p.hospital_id, updated_at=func.now())
            res = conn.execute(insert(t).values(**vals))
        else:
            vals = {k: v for k, v in ch.after.items()
                    if k in t.c and k not in ("id", "version") and v != ch.before.get(k)}
            vals.update(version=ch.after["version"], updated_at=func.now())
            res = conn.execute(update(t).where(
                t.c.id == ch.id, t.c.hospital_id == p.hospital_id, t.c.version == ch.before["version"]
            ).values(**vals))
        if res.rowcount != 1:
            raise StaleDataError(f"{plan.entity} row {ch.id} changed since the preview")
    return len(plan.changes)


def _apply_access(conn: Connection, p: Principal, user_id: int, before: dict, after: dict) -> None:
    rids = directory.role_ids(conn)
    pids = directory.permission_ids(conn)
    br, ar = set(before["roles"]), set(after["roles"])
    for r in br - ar:
        res = conn.execute(delete(user_roles).where(
            user_roles.c.user_id == user_id, user_roles.c.role_id == rids[r], user_roles.c.hospital_id == p.hospital_id))
        if res.rowcount != 1:
            raise StaleDataError("roles changed since the preview")
    for r in ar - br:
        conn.execute(insert(user_roles).values(
            user_id=user_id, role_id=rids[r], hospital_id=p.hospital_id, granted_by=p.user_id))
    bo = {o["permission"]: o for o in before["overrides"]}
    ao = {o["permission"]: o for o in after["overrides"]}
    for perm, o in bo.items():
        if ao.get(perm) != o:
            conn.execute(delete(user_permission_overrides).where(
                user_permission_overrides.c.user_id == user_id,
                user_permission_overrides.c.permission_id == pids[perm],
                user_permission_overrides.c.hospital_id == p.hospital_id))
    for perm, o in ao.items():
        if bo.get(perm) != o:
            conn.execute(insert(user_permission_overrides).values(
                hospital_id=p.hospital_id, user_id=user_id, permission_id=pids[perm],
                effect=o["effect"], constraints=o["constraints"], granted_by=p.user_id))


# ------------------------------------------------------------------ undo

def _restore_types(entity: str, d: dict) -> dict:
    out = dict(d)
    for k in ("expiry_date", "bill_date"):
        if isinstance(out.get(k), str):
            out[k] = date.fromisoformat(out[k])
    for k in ("unit_price", "gross_amount", "discount_pct", "net_amount"):
        if out.get(k) is not None and not isinstance(out[k], Decimal):
            out[k] = Decimal(str(out[k]))
    return out


def find_last_change(conn: Connection, p: Principal) -> dict | None:
    """The user's most recent executed change in this hospital (any action)."""
    row = conn.execute(
        select(audit_log).where(
            audit_log.c.user_id == p.user_id, audit_log.c.hospital_id == p.hospital_id,
            audit_log.c.decision == "executed",
            audit_log.c.action.in_(UNDOABLE_ACTIONS | {Action.UNDO.value}),
        ).order_by(audit_log.c.id.desc()).limit(1)
    ).mappings().first()
    return dict(row) if row else None


def already_undone(conn: Connection, p: Principal, audit_id: int) -> bool:
    return conn.execute(
        select(func.count()).select_from(audit_log).where(
            audit_log.c.hospital_id == p.hospital_id, audit_log.c.decision == "executed",
            audit_log.c.action == Action.UNDO.value,
            audit_log.c.resolved_operation["undo_audit_id"].astext == str(audit_id),
        )
    ).scalar_one() > 0


def plan_undo(conn: Connection, p: Principal, audit_row: dict, catalog: PermissionCatalog,
              lock: bool = False) -> WritePlan:
    """Build a plan that restores the before-snapshot, or raise StaleDataError if any of
    those rows were modified after the original change."""
    before, after = audit_row["before_snapshot"] or {}, audit_row["after_snapshot"] or {}
    entity = before.get("entity")
    title = f"Undo: {audit_row.get('reason') or audit_row['action']}"
    if entity in TABLES:
        t = TABLES[entity]
        view = INVENTORY_VIEW if entity == "inventory_items" else BILL_VIEW
        label = _item_label if entity == "inventory_items" else _bill_label
        b_rows = {r["id"]: _restore_types(entity, r) for r in before.get("rows", [])}
        a_rows = {r["id"]: _restore_types(entity, r) for r in after.get("rows", []) if r}
        ids = sorted(set(b_rows) | set(a_rows))
        q = select(*[t.c[c] for c in view]).where(t.c.hospital_id == p.hospital_id, t.c.id.in_(ids))
        if lock:
            q = q.with_for_update()
        current = {r["id"]: dict(r) for r in conn.execute(q).mappings().all()}
        changes = []
        for i in ids:
            orig, done, cur = b_rows.get(i), a_rows.get(i), current.get(i)
            if done is None:  # row was deleted: must still be absent
                if cur is not None:
                    raise StaleDataError(f"row {i} has been re-created since")
                changes.append(RowChange(i, label(orig), None, orig))
            else:
                if cur is None or cur["version"] != done["version"]:
                    raise StaleDataError(f"{label(done)} was modified after your change")
                restored = {k: orig[k] for k in view if k in orig}
                restored["version"] = cur["version"] + 1
                changes.append(RowChange(i, label(cur), cur, restored))
        fields = ["(row restored)"] if any(c.before is None for c in changes) else sorted(
            {k for c in changes for k in c.after if k != "version" and c.after[k] != c.before.get(k)})
        return WritePlan(entity=entity, action=Action.UNDO, title=title, changes=changes, changed_fields=fields,
                         notes=[f"Restores the state from before change #{audit_row['id']} "
                                f"({audit_row['created_at']:%d %b %Y %H:%M} UTC)."])
    if entity == "access":
        uid = before["state"]["user_id"]
        if lock:
            lock_access(conn, uid)
        target = directory.load_target(conn, p.hospital_id, uid)
        if target is None or access_state(target) != after["state"]:
            raise StaleDataError("that user's access has been changed again since")
        from app.execution.plans import _access_plan_from_change
        b = before["state"]
        change = AccessChange(
            roles_after=tuple(b["roles"]),
            overrides_after=tuple(Override(o["permission"], o["effect"], o["constraints"]) for o in b["overrides"]),
            noop=False, description=f"Restore {target.name}'s access to how it was before change #{audit_row['id']}.",
        )
        return _access_plan_from_change(Action.UNDO, target, change, catalog, "Undo")
    raise StaleDataError("that change cannot be undone")
