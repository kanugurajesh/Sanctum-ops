"""The request flow (text -> intent -> policy -> preview -> confirm -> commit -> audit).

    1. parse        LLM proposes a typed intent (agent/)            - no DB, no identity
    2. normalize    code validates it into an Operation (operations/)
    3. check_request  permission, tenant, value ceilings (policy/)  - before touching business tables
    4. clarify      missing / ambiguous details -> question
    5. plan         dry run: exact rows, before/after, ₹ impact (execution/plans.py)
    6. check_scope  row caps on the measured row count (policy/)
    7. preview      stored as a pending operation; nothing written to business tables
    8. confirm      re-check everything, re-plan under row locks, verify nothing changed,
                    apply + audit in ONE transaction

Every exit (answer, clarification, refusal, preview, executed, cancelled, failed) writes an
audit_log row.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable
from uuid import UUID, uuid4

from sqlalchemy import Connection, insert, select, update

from app.agent.parser import IntentParser, ParserError, Turn
from app.audit import record
from app.config import get_settings
from app.db.engine import tenant_tx
from app.db.tables import approval_requests, audit_log, conversation_turns, pending_operations
from app.execution import directory, reads
from app.execution.executor import (
    StaleDataError, already_undone, apply_plan, find_last_change, lock_access, plan_undo,
)
from app.execution.plans import (
    WritePlan, plan_access, plan_bills_discount, plan_bills_status, plan_inventory_delete,
    plan_inventory_update,
)
from app.operations.dates import IST, today_ist
from app.operations.model import Action, Operation
from app.operations.normalize import Clarification, Option, normalize
from app.policy import engine as policy
from app.policy.permissions import PermissionCatalog, ROLE_NAMES, covers
from app.policy.principal import Principal, TargetUser
from app.responses import (
    AgentResponse, AnswerBody, ClarificationBody, ExecutedBody, OptionOut, PreviewBody, RefusalBody,
    SampleRow, SuggestionOut, fmt_inr, fmt_pct, jsonable,
)

log = logging.getLogger(__name__)
CLARIFY_MARK = "[clarification] "


class ConfirmError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass
class Ctx:
    p: Principal
    request_id: UUID
    raw: str
    intent: dict | None = None
    op: Operation | None = None
    catalog: PermissionCatalog | None = None
    conversation_id: UUID | None = None
    extra_audit: dict = field(default_factory=dict)


class Orchestrator:
    def __init__(self, parser: IntentParser, clock: Callable[[], datetime] | None = None):
        self.parser = parser
        self.clock = clock or (lambda: datetime.now(tz=IST))

    # ================================================================== chat entry point

    def handle(self, p: Principal, text: str, conversation_id: UUID | None = None) -> AgentResponse:
        ctx = Ctx(p=p, request_id=uuid4(), raw=(text or "").strip()[:2000], conversation_id=conversation_id)
        if not ctx.raw:
            return AgentResponse(type="error", request_id=ctx.request_id, message="Please type a request.")
        try:
            return self._handle(ctx)
        except Exception as e:  # never leave an attempt unaudited
            log.exception("request failed")
            with tenant_tx(p.hospital_id) as conn:
                aid = record(conn, p, request_id=ctx.request_id, raw_query=ctx.raw, decision="failed",
                             action=ctx.op.action.value if ctx.op else None, parsed_intent=ctx.intent,
                             reason=f"internal error: {type(e).__name__}", reason_code="error")
            return AgentResponse(type="error", request_id=ctx.request_id, audit_id=aid, intent=ctx.intent,
                                 message="Something went wrong while handling that request. Nothing was changed.")

    def _handle(self, ctx: Ctx) -> AgentResponse:
        p = ctx.p
        history = self._history(p, ctx.conversation_id)
        self._save_turn(ctx, "user", ctx.raw)

        # 1. LLM proposes
        try:
            intent = self.parser.parse(ctx.raw, history)
        except ParserError as e:
            with tenant_tx(p.hospital_id) as conn:
                aid = record(conn, p, request_id=ctx.request_id, raw_query=ctx.raw, decision="failed",
                             reason=str(e), reason_code="parser_error")
            return AgentResponse(type="error", request_id=ctx.request_id, message=str(e), audit_id=aid)
        ctx.intent = intent.model_dump(mode="json", exclude_none=True)

        # 2. code validates
        today = today_ist(self.clock())
        op, clar = normalize(intent, today)
        ctx.op = op

        with tenant_tx(p.hospital_id) as conn:
            ctx.catalog = directory.load_catalog(conn)

        # 3. policy, before any business-table query
        decision = policy.check_request(p, op, ctx.catalog)
        if not decision.allowed:
            return self._refuse(ctx, decision)

        # 4. clarify instead of guessing
        if clar:
            return self._clarify(ctx, clar)

        if op.action == Action.HELP:
            return self._answer(ctx, reads.help_answer(p), "Here is what you can do.")
        if op.is_write:
            return self._write(ctx, today)
        return self._read(ctx, today)

    # ================================================================== reads

    def _read(self, ctx: Ctx, today) -> AgentResponse:
        p, op = ctx.p, ctx.op
        with tenant_tx(p.hospital_id) as conn:
            if op.action == Action.INVENTORY_QUERY:
                body = reads.inventory_answer(conn, p, op, today)
            elif op.action == Action.BILLS_QUERY:
                body = reads.bills_answer(conn, p, op, policy.row_scope(p, op))
            elif op.action == Action.USERS_QUERY:
                body = reads.users_answer(conn, p, ctx.catalog)
            elif op.action == Action.ACCESS_SHOW:
                if not (op.target_self or op.target_user_name or op.target_user_id):
                    op.target_self = True
                target = self._resolve_target(conn, ctx)
                if isinstance(target, Clarification):
                    return self._clarify(ctx, target, conn)
                if target.user_id != p.user_id and not p.has("users.read"):
                    return self._refuse(ctx, policy.Decision.deny(
                        policy.Code.NO_PERMISSION, "You can only view your own access."), conn)
                body = reads.access_answer(target, ctx.catalog)
            elif op.action == Action.APPROVALS_LIST:
                body = reads.approvals_answer(conn, p, can_decide=p.has("users.manage_access"))
            else:
                return self._refuse(ctx, policy.Decision.deny(policy.Code.UNSUPPORTED, "Unsupported request."), conn)
            return self._answer(ctx, body, body.summary[0] if body.summary else body.title, conn)

    # ================================================================== writes (preview)

    def _write(self, ctx: Ctx, today) -> AgentResponse:
        p, op = ctx.p, ctx.op
        with tenant_tx(p.hospital_id) as conn:
            approval_id = None
            if op.action == Action.APPROVALS_REJECT:
                return self._reject_approval(ctx, conn)
            if op.action == Action.APPROVALS_APPROVE:
                req = self._load_approval(conn, p, op.approval_request_id)
                if isinstance(req, str):
                    return self._answer(ctx, AnswerBody(title="Approval request", summary=[req]), req, conn)
                approval_id = req["id"]
                inner = Operation.model_validate(req["operation"])
                d = policy.check_request(p, inner, ctx.catalog)
                if not d.allowed:
                    return self._refuse(ctx, d, conn)
                ctx.op = op = inner
            if op.action == Action.UNDO:
                last = find_last_change(conn, p)
                if last is None or last["action"] == Action.UNDO.value or already_undone(conn, p, last["id"]):
                    msg = ("You have no change that can be undone." if last is None else
                           "Your last change has already been undone; only your most recent change can be undone.")
                    return self._answer(ctx, AnswerBody(title="Undo", summary=[msg]), msg, conn)
                op.undo_audit_id = last["id"]

            result = self._build_plan(conn, ctx, op, today, lock=False)
            if isinstance(result, AgentResponse):
                return result
            plan = result

            if plan.count == 0:
                if plan.entity == "access":
                    msg, notes = f"Nothing to change: {plan.notes[0]}", []
                else:
                    msg, notes = "No matching rows in your hospital need this change, so nothing was changed.", plan.notes
                body = AnswerBody(title=plan.title, summary=[msg], notes=notes,
                                  date_range=plan.date_range.label if plan.date_range else None)
                return self._answer(ctx, body, msg, conn)

            d = self._scope_check(ctx, op, plan)
            if not d.allowed:
                return self._refuse(ctx, d, conn)

            return self._preview(ctx, conn, op, plan, approval_id)

    def _build_plan(self, conn: Connection, ctx: Ctx, op: Operation, today, lock: bool) -> WritePlan | AgentResponse:
        """Shared by preview and confirm. Returns a plan, or a response (refusal/clarification)."""
        p = ctx.p
        scope = policy.row_scope(p, op)
        if op.action == Action.INVENTORY_UPDATE:
            return plan_inventory_update(conn, p, op, today, lock)
        if op.action == Action.INVENTORY_DELETE:
            return plan_inventory_delete(conn, p, op, today, lock)
        if op.action == Action.BILLS_DISCOUNT:
            return plan_bills_discount(conn, p, op, scope, lock)
        if op.action == Action.BILLS_UPDATE_STATUS:
            return plan_bills_status(conn, p, op, scope, lock)
        if op.action in (Action.ACCESS_GRANT, Action.ACCESS_REVOKE):
            target = self._resolve_target(conn, ctx)
            if isinstance(target, Clarification):
                return self._clarify(ctx, target, conn)
            if lock:
                lock_access(conn, target.user_id)
                target = directory.load_target(conn, p.hospital_id, target.user_id)
            plan = plan_access(p, op, target, ctx.catalog)
            d = policy.check_access_change(p, op, ctx.catalog, target, plan.access_change,
                                           directory.active_admin_count(conn, p.hospital_id))
            if not d.allowed:
                return self._refuse(ctx, d, conn)
            return plan
        if op.action == Action.UNDO:
            row = conn.execute(select(audit_log).where(
                audit_log.c.id == op.undo_audit_id, audit_log.c.user_id == p.user_id)).mappings().first()
            if row is None:
                return self._refuse(ctx, policy.Decision.deny(policy.Code.UNSUPPORTED, "That change cannot be undone."), conn)
            row = dict(row)
            original = Operation(action=Action(row["action"]))
            d = policy.check_request(p, original, ctx.catalog)
            if not d.allowed:
                return self._refuse(ctx, d, conn)
            try:
                plan = plan_undo(conn, p, row, ctx.catalog, lock=lock)
            except StaleDataError as e:
                return self._refuse(ctx, policy.Decision.deny(
                    policy.Code.UNSUPPORTED, f"Can't undo safely: {e}. Nothing was changed."), conn)
            if plan.entity == "access" and plan.target is not None:
                d = policy.check_access_change(p, Operation(action=Action.UNDO), ctx.catalog, plan.target,
                                               plan.access_change, directory.active_admin_count(conn, p.hospital_id))
                if not d.allowed:
                    return self._refuse(ctx, d, conn)
            if plan.entity == "bills" and plan.changes:
                max_pct = max((c.after or {}).get("discount_pct", 0) for c in plan.changes)
                if p.has("bills.discount"):
                    d = policy.check_discount(p, max_pct)
                    if not d.allowed:
                        return self._refuse(ctx, d, conn)
            return plan
        return self._refuse(ctx, policy.Decision.deny(policy.Code.UNSUPPORTED, "That operation is not supported."), conn)

    def _scope_check(self, ctx: Ctx, op: Operation, plan: WritePlan) -> policy.Decision:
        check_op = op if op.action != Action.UNDO else Operation(action=Action(
            self._undo_original_action(plan, op)))
        hint, cap = None, ctx.p.constraints(policy.required_permission(check_op) or "").get("max_rows")
        if plan.category_counts and cap:
            fits = [(c, n) for c, n in sorted(plan.category_counts.items(), key=lambda x: -x[1]) if n <= int(cap)]
            if fits:
                hint = "Try narrowing it, e.g. by category: " + ", ".join(f"{c} ({n})" for c, n in fits[:6]) + "."
        d = policy.check_scope(ctx.p, check_op, plan.count, hint)
        if not d.allowed and plan.category_counts and cap:
            for c, n in sorted(plan.category_counts.items(), key=lambda x: -x[1]):
                if n <= int(cap) and len(d.suggestions) < 3:
                    d.suggestions.append(policy.Suggestion(label=f"Only {c} ({n} items)",
                                                           message=f"{ctx.raw.rstrip('.')} in category {c}"))
        return d

    @staticmethod
    def _undo_original_action(plan: WritePlan, op: Operation) -> str:
        return {"inventory_items": "inventory.update", "bills": "bills.discount",
                "access": "access.grant"}.get(plan.entity, "inventory.update")

    def _preview(self, ctx: Ctx, conn: Connection, op: Operation, plan: WritePlan, approval_id: int | None) -> AgentResponse:
        s = get_settings()
        p = ctx.p
        op_id = uuid4()
        expires = datetime.now(tz=timezone.utc) + timedelta(minutes=s.pending_ttl_minutes)
        typed = plan.count > s.typed_count_threshold
        samples = [SampleRow(id=c.id, label=c.label, before=jsonable(self._display(c.before, plan)),
                             after=jsonable(self._display(c.after, plan))) for c in plan.changes[:s.sample_size]]
        body = PreviewBody(
            operation_id=op_id, action=op.action.value if op.action != Action.UNDO else "undo",
            title=plan.title, rows_affected=plan.count, changed_fields=plan.changed_fields, samples=samples,
            date_range=plan.date_range.label if plan.date_range else None,
            money_label=plan.money_label, money_impact=plan.money_text(),
            requires_typed_count=typed, expires_at=expires, notes=plan.notes, access=plan.access,
        )
        conn.execute(insert(pending_operations).values(
            id=op_id, hospital_id=p.hospital_id, user_id=p.user_id, request_id=ctx.request_id,
            raw_query=ctx.raw, action=op.action.value, operation=jsonable(op.model_dump(mode="json")),
            parsed_intent=ctx.intent, target_ids=plan.fingerprint(), expected_count=plan.count,
            requires_typed_count=typed, preview=jsonable(body.model_dump(mode="json")),
            approval_request_id=approval_id, status="pending", expires_at=expires,
        ))
        aid = record(conn, p, request_id=ctx.request_id, raw_query=ctx.raw, decision="previewed",
                     action=op.action.value, parsed_intent=ctx.intent, resolved_operation=op.model_dump(mode="json"),
                     reason=plan.title, rows_affected=plan.count, operation_id=op_id)
        confirm = (f"Type {plan.count} to confirm." if typed else "Confirm to apply.")
        money = f" {plan.money_label}: {plan.money_text()}." if plan.money_impact is not None else ""
        dr = f" Dates: {plan.date_range.label}." if plan.date_range else ""
        return AgentResponse(type="preview", request_id=ctx.request_id, intent=ctx.intent, preview=body, audit_id=aid,
                             message=f"{plan.title} — {plan.count} row(s) will change.{dr}{money} {confirm}")

    @staticmethod
    def _display(row: dict | None, plan: WritePlan) -> dict | None:
        if row is None:
            return None
        if plan.entity == "access":
            return row
        keep = ["name", "category", "batch_no", "expiry_date", "quantity", "unit_price"] if plan.entity == "inventory_items" \
            else ["patient_name", "bill_date", "gross_amount", "discount_pct", "net_amount", "status"]
        return {k: row[k] for k in keep if k in row}

    # ================================================================== confirm / cancel

    def confirm(self, p: Principal, operation_id: UUID, typed_count: int | None) -> AgentResponse:
        """Re-validates with the *current* session principal and data, then executes atomically."""
        with tenant_tx(p.hospital_id) as conn:
            row = self._load_pending(conn, p, operation_id)
            if row["requires_typed_count"] and typed_count != row["expected_count"]:
                raise ConfirmError(
                    f"This change affects {row['expected_count']} rows. Type exactly {row['expected_count']} to confirm.")
            if row["expires_at"] < datetime.now(tz=timezone.utc):
                conn.execute(update(pending_operations).where(pending_operations.c.id == operation_id)
                             .values(status="expired", resolved_at=datetime.now(tz=timezone.utc)))
                record(conn, p, request_id=row["request_id"], raw_query=row["raw_query"], decision="expired",
                       action=row["action"], operation_id=operation_id, reason="preview expired before confirmation")
                return AgentResponse(type="error", request_id=row["request_id"],
                                     message="This preview has expired. Please send the request again.")

        ctx = Ctx(p=p, request_id=row["request_id"], raw=row["raw_query"], intent=row["parsed_intent"])
        op = Operation.model_validate(row["operation"])
        ctx.op = op
        today = today_ist(self.clock())
        try:
            with tenant_tx(p.hospital_id) as conn:     # ---- the one transaction
                row = self._load_pending(conn, p, operation_id, lock=True)
                ctx.catalog = directory.load_catalog(conn)
                if op.action != Action.UNDO:    # undo re-checks the original action inside _build_plan
                    d = policy.check_request(p, op, ctx.catalog)
                    if not d.allowed:
                        raise _Abort(d.reason, d.code.value)
                result = self._build_plan(conn, ctx, op, today, lock=True)
                if isinstance(result, AgentResponse):
                    raise _Abort(result.message, "recheck_failed")
                plan = result
                if plan.fingerprint() != row["target_ids"]:
                    raise _Abort("The data changed after the preview (rows were added, removed or edited). "
                                 "Nothing was changed — please run the request again to get a fresh preview.", "stale")
                d = self._scope_check(ctx, op, plan)
                if not d.allowed:
                    raise _Abort(d.reason, d.code.value)

                n = apply_plan(conn, p, plan)
                before, after = self._snapshots(plan)
                aid = record(conn, p, request_id=row["request_id"], raw_query=row["raw_query"], decision="executed",
                             action=row["action"], parsed_intent=row["parsed_intent"],
                             resolved_operation=op.model_dump(mode="json"), reason=plan.title,
                             rows_affected=n, before=before, after=after, operation_id=operation_id)
                conn.execute(update(pending_operations).where(pending_operations.c.id == operation_id)
                             .values(status="executed", resolved_at=datetime.now(tz=timezone.utc)))
                if row["approval_request_id"]:
                    conn.execute(update(approval_requests).where(
                        approval_requests.c.id == row["approval_request_id"], approval_requests.c.status == "pending",
                    ).values(status="approved", decided_by=p.user_id, decided_at=datetime.now(tz=timezone.utc)))
        except (_Abort, StaleDataError) as e:
            code = e.code if isinstance(e, _Abort) else "stale"
            msg = str(e) if isinstance(e, _Abort) else f"The data changed after the preview ({e}). Nothing was changed."
            self._fail_pending(p, operation_id, row, msg, code)
            return AgentResponse(type="error", request_id=row["request_id"], message=msg)
        except Exception as e:
            log.exception("execution failed")
            self._fail_pending(p, operation_id, row, f"execution error: {type(e).__name__}", "error")
            return AgentResponse(type="error", request_id=row["request_id"],
                                 message="The change failed and was rolled back. Nothing was changed.")

        return AgentResponse(
            type="executed", request_id=row["request_id"], audit_id=aid,
            message=f"Done. {plan.title} — {n} row(s) changed. Logged as audit #{aid}.",
            executed=ExecutedBody(operation_id=operation_id, title=plan.title, rows_affected=n, audit_id=aid,
                                  undoable=op.action != Action.UNDO),
        )

    def cancel(self, p: Principal, operation_id: UUID) -> AgentResponse:
        with tenant_tx(p.hospital_id) as conn:
            row = self._load_pending(conn, p, operation_id, lock=True)
            conn.execute(update(pending_operations).where(pending_operations.c.id == operation_id)
                         .values(status="cancelled", resolved_at=datetime.now(tz=timezone.utc)))
            aid = record(conn, p, request_id=row["request_id"], raw_query=row["raw_query"], decision="cancelled",
                         action=row["action"], parsed_intent=row["parsed_intent"], operation_id=operation_id,
                         reason="user cancelled at preview; nothing written")
        return AgentResponse(type="cancelled", request_id=row["request_id"], audit_id=aid,
                             message="Cancelled. Nothing was changed.")

    def _load_pending(self, conn: Connection, p: Principal, operation_id: UUID, lock: bool = False) -> dict:
        q = select(pending_operations).where(
            pending_operations.c.id == operation_id,
            pending_operations.c.user_id == p.user_id,          # only the user who previewed can confirm
            pending_operations.c.hospital_id == p.hospital_id)
        if lock:
            q = q.with_for_update()
        row = conn.execute(q).mappings().first()
        if row is None:
            raise ConfirmError("No such pending operation.", 404)
        if row["status"] != "pending":
            raise ConfirmError(f"This operation is already {row['status']}.", 409)
        return dict(row)

    def _fail_pending(self, p: Principal, operation_id: UUID, row: dict, msg: str, code: str) -> None:
        with tenant_tx(p.hospital_id) as conn:
            conn.execute(update(pending_operations).where(pending_operations.c.id == operation_id)
                         .values(status="failed", resolved_at=datetime.now(tz=timezone.utc)))
            record(conn, p, request_id=row["request_id"], raw_query=row["raw_query"], decision="failed",
                   action=row["action"], parsed_intent=row["parsed_intent"], operation_id=operation_id,
                   reason=msg, reason_code=code)

    @staticmethod
    def _snapshots(plan: WritePlan) -> tuple[dict, dict]:
        if plan.entity == "access":
            ch = plan.changes[0]
            return {"entity": "access", "state": ch.before}, {"entity": "access", "state": ch.after}
        return ({"entity": plan.entity, "rows": [c.before for c in plan.changes if c.before is not None]},
                {"entity": plan.entity, "rows": [c.after for c in plan.changes if c.after is not None]})

    # ================================================================== approvals

    def request_approval(self, p: Principal, audit_id: int) -> AgentResponse:
        """Turn one of the user's own ceiling refusals into an approval request for an admin."""
        request_id = uuid4()
        with tenant_tx(p.hospital_id) as conn:
            row = conn.execute(select(audit_log).where(
                audit_log.c.id == audit_id, audit_log.c.user_id == p.user_id,
                audit_log.c.decision == "refused", audit_log.c.reason_code == policy.Code.DISCOUNT_CEILING.value,
            )).mappings().first()
            if row is None:
                raise ConfirmError("Only your own refused over-limit discount requests can be sent for approval.", 404)
            op = Operation.model_validate(row["resolved_operation"])
            dr = f" for bills dated {op.bills.bill_date.label}" if op.bills and op.bills.bill_date else ""
            summary = f"{fmt_pct(op.discount_pct)} discount{dr} (requested: \"{row['raw_query'][:120]}\")"
            rid = conn.execute(insert(approval_requests).values(
                hospital_id=p.hospital_id, requested_by=p.user_id, raw_query=row["raw_query"],
                action=op.action.value, operation=row["resolved_operation"], summary=summary,
            ).returning(approval_requests.c.id)).scalar_one()
            aid = record(conn, p, request_id=request_id, raw_query=row["raw_query"], decision="executed",
                         action="approvals.request", resolved_operation=row["resolved_operation"],
                         reason=f"approval request #{rid} created")
        msg = f"Sent to an admin as approval request #{rid}. Nothing is changed until an admin approves and confirms it."
        return AgentResponse(type="answer", request_id=request_id, audit_id=aid, message=msg,
                             answer=AnswerBody(title="Approval requested", summary=[msg, summary]))

    def _load_approval(self, conn: Connection, p: Principal, rid: int | None) -> dict | str:
        row = conn.execute(select(approval_requests).where(
            approval_requests.c.id == rid, approval_requests.c.hospital_id == p.hospital_id)).mappings().first()
        if row is None:
            return f"There is no approval request #{rid} in your hospital."
        if row["status"] != "pending":
            return f"Approval request #{rid} is already {row['status']}."
        return dict(row)

    def _reject_approval(self, ctx: Ctx, conn: Connection) -> AgentResponse:
        p, op = ctx.p, ctx.op
        req = self._load_approval(conn, p, op.approval_request_id)
        if isinstance(req, str):
            return self._answer(ctx, AnswerBody(title="Approval request", summary=[req]), req, conn)
        inner = Operation.model_validate(req["operation"])
        d = policy.check_request(p, inner, ctx.catalog)
        if not d.allowed:
            return self._refuse(ctx, d, conn)
        conn.execute(update(approval_requests).where(approval_requests.c.id == req["id"]).values(
            status="rejected", decided_by=p.user_id, decided_at=datetime.now(tz=timezone.utc)))
        msg = f"Approval request #{req['id']} rejected. Nothing was changed in bills."
        aid = record(conn, p, request_id=ctx.request_id, raw_query=ctx.raw, decision="executed",
                     action=Action.APPROVALS_REJECT.value, parsed_intent=ctx.intent,
                     resolved_operation=op.model_dump(mode="json"), reason=msg, rows_affected=1)
        return AgentResponse(type="answer", request_id=ctx.request_id, intent=ctx.intent, audit_id=aid, message=msg,
                             answer=AnswerBody(title="Approval rejected", summary=[msg]))

    # ================================================================== targets & clarifications

    def _resolve_target(self, conn: Connection, ctx: Ctx) -> TargetUser | Clarification:
        p, op = ctx.p, ctx.op
        if op.target_self:
            return directory.load_target(conn, p.hospital_id, p.user_id)
        if op.target_user_id is not None:
            t = directory.load_target(conn, p.hospital_id, op.target_user_id)
            if t is not None and (not op.target_user_name or
                                  all(w.lower() in t.name.lower() for w in op.target_user_name.split())):
                return t
        name = op.target_user_name or ""
        matches = directory.find_users_by_name(conn, p.hospital_id, name) if name else []
        if len(matches) == 1:
            return matches[0]
        base = self._rephrase_without_name(ctx)
        if not matches:
            who = f"anyone called '{name}'" if name else "that user"
            return Clarification("target", f"I couldn't find {who} in {p.hospital_name}. "
                                           "Who do you mean? (Full name helps.)")
        return Clarification(
            "target",
            f"There are {len(matches)} people called '{name}' in {p.hospital_name}. Which one do you mean?",
            [Option(f"{m.name} — {', '.join(m.roles) or 'no role'} ({m.email})",
                    f"{base} — I mean {m.name} (user #{m.user_id})") for m in matches],
        )

    @staticmethod
    def _rephrase_without_name(ctx: Ctx) -> str:
        return ctx.raw.rstrip(".?! ")

    def _access_options(self, ctx: Ctx) -> Clarification:
        """'Give Ravi access' -> which access? Offer only roles/permissions this admin could grant."""
        p, op = ctx.p, ctx.op
        with tenant_tx(p.hospital_id) as conn:
            target = self._resolve_target(conn, ctx)
        if isinstance(target, Clarification):
            name, roles_now = op.target_user_name or "them", ()
        else:
            name, roles_now = target.name, target.roles
        options: list[Option] = []
        if op.action == Action.ACCESS_GRANT:
            for role in ROLE_NAMES:
                grants = ctx.catalog.role_permissions(role)
                if role not in roles_now and all(p.has(k) and covers(p.constraints(k), c) for k, c in grants.items()):
                    options.append(Option(f"{role.replace('_', ' ').title()} role", f"Give {name} the {role} role"))
            for perm in ("inventory.read", "inventory.update", "bills.read", "bills.discount"):
                if p.has(perm):
                    options.append(Option(f"Only {perm}", f"Give {name} {perm} permission"))
            q = f"Which access should {name} get? Pick a role or a single permission."
        else:
            for role in roles_now:
                options.append(Option(f"Remove {role.replace('_', ' ')} role", f"Remove the {role} role from {name}"))
            q = f"Which access should I remove from {name}?"
        return Clarification("access_unspecified", q, options[:8])

    # ================================================================== exits (each one audits)

    def _answer(self, ctx: Ctx, body: AnswerBody, message: str, conn: Connection | None = None) -> AgentResponse:
        def go(c: Connection) -> int:
            return record(c, ctx.p, request_id=ctx.request_id, raw_query=ctx.raw, decision="answered",
                          action=ctx.op.action.value if ctx.op else None, parsed_intent=ctx.intent,
                          resolved_operation=ctx.op.model_dump(mode="json") if ctx.op else None,
                          reason=message[:500], rows_affected=0)
        aid = self._in_tx(ctx, conn, go)
        self._save_turn(ctx, "assistant", message, conn)
        return AgentResponse(type="answer", request_id=ctx.request_id, intent=ctx.intent, answer=body,
                             message=message, audit_id=aid)

    def _clarify(self, ctx: Ctx, clar: Clarification, conn: Connection | None = None) -> AgentResponse:
        if clar.kind == "access_unspecified" and not clar.options and ctx.op is not None:
            clar = self._access_options(ctx)

        def go(c: Connection) -> int:
            return record(c, ctx.p, request_id=ctx.request_id, raw_query=ctx.raw, decision="clarified",
                          action=ctx.op.action.value if ctx.op else None, parsed_intent=ctx.intent,
                          reason=clar.question, reason_code=clar.kind)
        aid = self._in_tx(ctx, conn, go)
        self._save_turn(ctx, "assistant", CLARIFY_MARK + clar.question, conn)
        return AgentResponse(
            type="clarification", request_id=ctx.request_id, intent=ctx.intent, message=clar.question, audit_id=aid,
            clarification=ClarificationBody(question=clar.question,
                                            options=[OptionOut(label=o.label, message=o.message) for o in clar.options]),
        )

    def _refuse(self, ctx: Ctx, d: policy.Decision, conn: Connection | None = None) -> AgentResponse:
        def go(c: Connection) -> int:
            return record(c, ctx.p, request_id=ctx.request_id, raw_query=ctx.raw, decision="refused",
                          action=ctx.op.action.value if ctx.op else None, parsed_intent=ctx.intent,
                          resolved_operation=ctx.op.model_dump(mode="json") if ctx.op else None,
                          reason=d.reason, reason_code=d.code.value)
        aid = self._in_tx(ctx, conn, go)
        self._save_turn(ctx, "assistant", d.reason, conn)
        suggestions = []
        for s in d.suggestions:
            if s.kind == "request_approval":
                suggestions.append(SuggestionOut(label=s.label, kind=s.kind, audit_id=aid))
            else:
                msg = s.message
                if msg is None and d.code == policy.Code.DISCOUNT_CEILING and ctx.op and ctx.op.discount_pct is not None:
                    msg = self._rewrite_pct(ctx, s.label)
                if msg:
                    suggestions.append(SuggestionOut(label=s.label, message=msg))
        return AgentResponse(type="refusal", request_id=ctx.request_id, intent=ctx.intent, audit_id=aid,
                             message=d.reason, refusal=RefusalBody(code=d.code.value, reason=d.reason, suggestions=suggestions))

    @staticmethod
    def _rewrite_pct(ctx: Ctx, label: str) -> str | None:
        m = re.search(r"(\d+(?:\.\d+)?)%", label)
        if not m:
            return None
        new = m.group(1)
        op = ctx.op
        if op.bills and op.bills.bill_date:
            dr = op.bills.bill_date
            return f"Give {new}% discount on all bills dated from {dr.start.isoformat()} to {dr.end.isoformat()}"
        out, n = re.subn(r"\d+(?:\.\d+)?\s*(%|percent|pct|శాతం|प्रतिशत|pratishat|shatam)", f"{new}%",
                         ctx.raw, count=1, flags=re.IGNORECASE)
        return out if n else None

    def _in_tx(self, ctx: Ctx, conn: Connection | None, fn) -> int:
        if conn is not None:
            return fn(conn)
        with tenant_tx(ctx.p.hospital_id) as c:
            return fn(c)

    # ================================================================== conversation memory

    def _history(self, p: Principal, conversation_id: UUID | None) -> list[Turn]:
        """Only the chain of (user request -> our clarifying question) that is still open."""
        if conversation_id is None:
            return []
        with tenant_tx(p.hospital_id) as conn:
            rows = conn.execute(select(conversation_turns.c.role, conversation_turns.c.content).where(
                conversation_turns.c.conversation_id == conversation_id,
                conversation_turns.c.user_id == p.user_id,
            ).order_by(conversation_turns.c.id.desc()).limit(8)).all()
        out: list[Turn] = []
        i = 0
        while i + 1 < len(rows) and rows[i].role == "assistant" and rows[i].content.startswith(CLARIFY_MARK) \
                and rows[i + 1].role == "user":
            out[:0] = [Turn("user", rows[i + 1].content), Turn("assistant", rows[i].content[len(CLARIFY_MARK):])]
            i += 2
        return out

    def _save_turn(self, ctx: Ctx, role: str, content: str, conn: Connection | None = None) -> None:
        if ctx.conversation_id is None:
            return
        values = dict(hospital_id=ctx.p.hospital_id, user_id=ctx.p.user_id, conversation_id=ctx.conversation_id,
                      role=role, content=content[:2000])
        if conn is not None:
            conn.execute(insert(conversation_turns).values(**values))
        else:
            with tenant_tx(ctx.p.hospital_id) as c:
                c.execute(insert(conversation_turns).values(**values))


class _Abort(Exception):
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code
