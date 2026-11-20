"""Read-only answers. Results are rendered by code into tables and summary lines; they are
never sent back to the LLM, so text stored in the database (e.g. an item called "Ignore
previous instructions…") is only ever displayed as data."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import Connection, func, select

from app.config import get_settings
from app.db.tables import approval_requests, bills, inventory_items, users
from app.execution import directory
from app.execution.queries import bill_conditions, inventory_conditions
from app.operations.model import Operation
from app.policy.engine import RowScope
from app.policy.permissions import PermissionCatalog
from app.policy.principal import Principal, TargetUser
from app.responses import AnswerBody, TableOut, fmt_inr


def _limit() -> int:
    return get_settings().max_answer_rows


def inventory_answer(conn: Connection, p: Principal, op: Operation, today: date) -> AnswerBody:
    t = inventory_items
    f = op.inventory
    conds = inventory_conditions(p.hospital_id, f, today)
    total = conn.execute(select(func.count()).select_from(t).where(*conds)).scalar_one()
    value = conn.execute(select(func.coalesce(func.sum(t.c.quantity * t.c.unit_price), 0)).where(*conds)).scalar_one()
    by_cat = conn.execute(
        select(t.c.category, func.count()).where(*conds).group_by(t.c.category).order_by(func.count().desc())
    ).all()

    if f and f.low_stock:
        title = "Low-stock items"
        order = [(t.c.quantity - t.c.reorder_level).asc(), t.c.name]
    elif f and (f.expiry or f.expired):
        title = "Expired items" if f.expired else "Items expiring"
        order = [t.c.expiry_date.asc(), t.c.name]
    else:
        title = "Inventory items"
        order = [t.c.name, t.c.id]

    summary = [f"{total} matching item(s) in {p.hospital_name}.",
               f"Stock value: {fmt_inr(value)}."]
    if f and f.expiry:
        summary.insert(0, f"Expiry window: {f.expiry.label}.")
    if by_cat and (op.mode in ("summary", "count") or (f and f.low_stock)):
        summary.append("By category: " + ", ".join(f"{c} ({n})" for c, n in by_cat[:12]) + ".")
    if f and f.low_stock and total:
        out_of_stock = conn.execute(select(func.count()).where(*conds, t.c.quantity == 0)).scalar_one()
        summary.append(f"{out_of_stock} item(s) are completely out of stock.")

    table = None
    if op.mode != "count" and total:
        rows = conn.execute(
            select(t.c.id, t.c.name, t.c.category, t.c.batch_no, t.c.expiry_date, t.c.quantity,
                   t.c.reorder_level, t.c.unit_price).where(*conds).order_by(*order).limit(_limit())
        ).all()
        table = TableOut(
            columns=["ID", "Name", "Category", "Batch", "Expiry", "Qty", "Reorder level", "Unit price"],
            rows=[[r.id, r.name, r.category, r.batch_no, r.expiry_date.isoformat(), r.quantity,
                   r.reorder_level, fmt_inr(r.unit_price)] for r in rows],
        )
    notes = [f"Showing the first {_limit()} of {total}."] if total > _limit() and table else []
    return AnswerBody(title=title, summary=summary, table=table, total=total,
                      date_range=f.expiry.label if f and f.expiry else None, notes=notes)


def bills_answer(conn: Connection, p: Principal, op: Operation, scope: RowScope) -> AnswerBody:
    t = bills
    f = op.bills
    conds = bill_conditions(p.hospital_id, f, scope)
    agg = conn.execute(
        select(func.count(), func.coalesce(func.sum(t.c.gross_amount), 0),
               func.coalesce(func.sum(t.c.net_amount), 0)).where(*conds)
    ).one()
    total, gross, net = agg[0], Decimal(agg[1]), Decimal(agg[2])
    by_status = conn.execute(select(t.c.status, func.count()).where(*conds).group_by(t.c.status)).all()
    summary = []
    if f and f.bill_date:
        summary.append(f"Date range: {f.bill_date.label}.")
    summary += [f"{total} bill(s). Gross {fmt_inr(gross)}, discounts {fmt_inr(gross - net)}, net {fmt_inr(net)}."]
    if by_status:
        summary.append("By status: " + ", ".join(f"{s} ({n})" for s, n in sorted(by_status)) + ".")
    notes = []
    if scope.created_by is not None:
        notes.append("Your role can only see bills you created.")
    table = None
    if op.mode != "count" and total:
        rows = conn.execute(
            select(t.c.id, t.c.bill_date, t.c.patient_name, t.c.gross_amount, t.c.discount_pct,
                   t.c.net_amount, t.c.status).where(*conds).order_by(t.c.bill_date.desc(), t.c.id.desc()).limit(_limit())
        ).all()
        table = TableOut(
            columns=["Bill", "Date", "Patient", "Gross", "Discount %", "Net", "Status"],
            rows=[[r.id, r.bill_date.isoformat(), r.patient_name, fmt_inr(r.gross_amount),
                   f"{r.discount_pct}", fmt_inr(r.net_amount), r.status] for r in rows],
        )
        if total > _limit():
            notes.append(f"Showing the latest {_limit()} of {total}.")
    return AnswerBody(title="Bills", summary=summary, table=table, total=total,
                      date_range=f.bill_date.label if f and f.bill_date else None, notes=notes)


def users_answer(conn: Connection, p: Principal, catalog: PermissionCatalog) -> AnswerBody:
    ids = [r[0] for r in conn.execute(
        select(users.c.id).where(users.c.hospital_id == p.hospital_id).order_by(users.c.id)).all()]
    rows = []
    for uid in ids:
        t = directory.load_target(conn, p.hospital_id, uid)
        blocked = [o.permission for o in t.overrides if o.effect == "deny"]
        rows.append([t.user_id, t.name, t.email, ", ".join(t.roles) or "—",
                     ", ".join(blocked) or "—", "active" if t.is_active else "inactive"])
    return AnswerBody(title=f"Staff at {p.hospital_name}", summary=[f"{len(rows)} user(s)."],
                      table=TableOut(columns=["ID", "Name", "Email", "Roles", "Blocked permissions", "Status"], rows=rows),
                      total=len(rows))


def access_answer(target: TargetUser, catalog: PermissionCatalog) -> AnswerBody:
    perms = target.permissions(catalog)
    rows = [[code, ", ".join(f"{k}={v}" for k, v in cons.items()) or "—"] for code, cons in sorted(perms.items())]
    summary = [f"Roles: {', '.join(target.roles) or 'none'}."]
    if target.overrides:
        summary.append("Per-user overrides: " + ", ".join(f"{o.effect} {o.permission}" for o in target.overrides) + ".")
    return AnswerBody(title=f"Access for {target.name}", summary=summary,
                      table=TableOut(columns=["Permission", "Limits"], rows=rows), total=len(rows))


def approvals_answer(conn: Connection, p: Principal, can_decide: bool) -> AnswerBody:
    t = approval_requests
    q = select(t.c.id, t.c.summary, t.c.status, t.c.created_at, users.c.name).select_from(
        t.join(users, users.c.id == t.c.requested_by)).where(t.c.hospital_id == p.hospital_id)
    if not can_decide:
        q = q.where(t.c.requested_by == p.user_id)
    rows = conn.execute(q.order_by(t.c.id.desc()).limit(_limit())).all()
    pending = sum(1 for r in rows if r.status == "pending")
    return AnswerBody(
        title="Approval requests",
        summary=[f"{pending} pending request(s)." + (" Say 'approve request N' to review one." if can_decide and pending else "")],
        table=TableOut(columns=["Request", "Summary", "Requested by", "Status", "Created"],
                       rows=[[r.id, r.summary, r.name, r.status, f"{r.created_at:%d %b %Y %H:%M}"] for r in rows]),
        total=len(rows),
    )


HELP_BY_PERMISSION = {
    "inventory.read": "Look up stock: 'show items expiring in the next 30 days', 'summarise low-stock items'",
    "inventory.update": "Change stock: 'set quantity of paracetamol to 100', 'increase antibiotics by 10'",
    "inventory.delete": "Remove items: 'delete expired items in category vaccines'",
    "bills.read": "Look up bills: 'show pending bills from this week'",
    "bills.discount": "Discounts: 'give 10% discount on bills from last month'",
    "bills.update_status": "Bill status: 'mark bills of patient Rao from today as paid'",
    "users.read": "Staff: 'list users', 'what access does Ravi have'",
    "users.manage_access": "Access: 'remove inventory edit access from Ravi', 'make Priya a billing manager'",
}


def help_answer(p: Principal) -> AnswerBody:
    lines = [v for k, v in HELP_BY_PERMISSION.items() if p.has(k)]
    lines.append("Anyone: 'what can I do?', 'undo my last change', 'show my approval requests'")
    return AnswerBody(
        title=f"What you can do as {', '.join(r.replace('_', ' ') for r in p.roles) or 'a user'}",
        summary=lines,
        notes=["Every change is previewed first and only runs after you confirm."],
    )
