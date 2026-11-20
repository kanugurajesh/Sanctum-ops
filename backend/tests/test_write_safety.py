"""Write safety, tenant isolation, audit integrity, undo and approval routing."""
import pytest
import sqlalchemy
from sqlalchemy import text

import app.orchestrator as orchestrator_module
from app.db.engine import app_engine, owner_engine, tenant_tx
from app.orchestrator import ConfirmError
from tests.conftest import (
    ADMIN_A, ADMIN_B, BILLING_A, FRONTDESK_A, PHARMACIST_A, make_orchestrator, owner_scalar, principal,
)

INTENTS = {
    "Set paracetamol quantity to 77":
        {"action": "inventory.update", "item_name_contains": "paracetamol", "quantity_op": "set", "quantity_value": 77},
    "Set all inventory quantities to 5":
        {"action": "inventory.update", "scope_all": True, "quantity_op": "set", "quantity_value": 5},
    "Give 20% discount on all bills from last month":
        {"action": "bills.discount", "scope_all": True, "discount_pct": 20, "date_range": {"preset": "last_month"}},
    "Show all bills": {"action": "bills.query", "scope_all": True},
    "Undo my last change": {"action": "undo"},
    "Approve request 1": {"action": "approvals.approve", "approval_request_id": 1},
    "Remove inventory edit access from Ravi":
        {"action": "access.revoke", "target_user_name": "Ravi", "permission": "inventory.update"},
    "Update quantities": {"action": "inventory.update", "quantity_op": "set", "quantity_value": 3},
    "Show inventory item 1301": {"action": "inventory.query", "item_ids": [1301]},
}


@pytest.fixture
def orch():
    return make_orchestrator(INTENTS)


PARA_77 = "SELECT count(*) FROM inventory_items WHERE hospital_id=1 AND name ILIKE '%paracetamol%' AND quantity=77"


# ------------------------------------------------------------- atomicity

def test_failure_midway_leaves_db_unchanged(orch, fresh_db, monkeypatch):
    p = principal(ADMIN_A)
    r = orch.handle(p, "Set all inventory quantities to 5")
    n = r.preview.rows_affected
    original_record = orchestrator_module.record

    def exploding_record(conn, principal_, **kw):
        if kw.get("decision") == "executed":   # all UPDATEs already ran in this transaction
            raise RuntimeError("simulated crash after the updates")
        return original_record(conn, principal_, **kw)

    monkeypatch.setattr(orchestrator_module, "record", exploding_record)
    out = orch.confirm(p, r.preview.operation_id, typed_count=n)
    assert out.type == "error" and "rolled back" in out.message
    assert owner_scalar("SELECT count(*) FROM inventory_items WHERE hospital_id=1 AND quantity <> 5") == n
    assert owner_scalar("SELECT status FROM pending_operations WHERE id=:i", i=r.preview.operation_id) == "failed"
    assert owner_scalar("SELECT count(*) FROM audit_log WHERE request_id=:r AND decision='failed'", r=r.request_id) == 1


def test_stale_preview_is_rejected(orch, fresh_db):
    p = principal(ADMIN_A)
    r = orch.handle(p, "Set paracetamol quantity to 77")
    victim = r.preview.samples[0].id
    with owner_engine().begin() as c:  # someone else edits a targeted row after the preview
        c.execute(text("UPDATE inventory_items SET quantity = quantity + 1, version = version + 1 WHERE id=:i"), {"i": victim})
    out = orch.confirm(p, r.preview.operation_id, typed_count=None)
    assert out.type == "error" and "changed" in out.message
    assert owner_scalar(PARA_77) == 0


def test_cancel_writes_nothing_but_is_audited(orch, fresh_db):
    p = principal(ADMIN_A)
    r = orch.handle(p, "Set paracetamol quantity to 77")
    out = orch.cancel(p, r.preview.operation_id)
    assert out.type == "cancelled"
    assert owner_scalar(PARA_77) == 0
    assert owner_scalar("SELECT decision FROM audit_log WHERE request_id=:r ORDER BY id DESC LIMIT 1", r=r.request_id) == "cancelled"
    with pytest.raises(ConfirmError):
        orch.confirm(p, r.preview.operation_id, typed_count=None)


def test_small_write_needs_no_typed_count(orch, fresh_db):
    p = principal(ADMIN_A)
    r = orch.handle(p, "Set paracetamol quantity to 77")
    assert 0 < r.preview.rows_affected <= 50 and not r.preview.requires_typed_count
    assert orch.confirm(p, r.preview.operation_id, typed_count=None).type == "executed"


def test_only_the_previewing_user_can_confirm(orch, fresh_db):
    r = orch.handle(principal(ADMIN_A), "Set paracetamol quantity to 77")
    with pytest.raises(ConfirmError) as e:
        orch.confirm(principal(PHARMACIST_A), r.preview.operation_id, typed_count=None)
    assert e.value.status == 404
    with pytest.raises(ConfirmError):
        orch.confirm(principal(ADMIN_B), r.preview.operation_id, typed_count=None)


def test_permission_rechecked_at_confirm(orch, fresh_db):
    """Pharmacist previews, an admin revokes inventory.update, then the confirm must fail."""
    ravi = principal(PHARMACIST_A)
    r = orch.handle(ravi, "Set paracetamol quantity to 77")
    assert r.type == "preview"
    admin = principal(ADMIN_A)
    rv = orch.handle(admin, "Remove inventory edit access from Ravi")
    orch.confirm(admin, rv.preview.operation_id, typed_count=None)
    out = orch.confirm(principal(PHARMACIST_A), r.preview.operation_id, typed_count=None)
    assert out.type == "error"
    assert owner_scalar(PARA_77) == 0


def test_write_without_scope_asks(orch):
    r = orch.handle(principal(ADMIN_A), "Update quantities")
    assert r.type == "clarification"


# ------------------------------------------------------------- tenant isolation

def test_rls_hides_other_hospital_even_with_explicit_filter():
    with tenant_tx(1) as c:
        assert c.execute(text("SELECT count(*) FROM inventory_items WHERE hospital_id = 2")).scalar() == 0
        assert c.execute(text("SELECT count(*) FROM bills WHERE hospital_id = 2")).scalar() == 0
        assert c.execute(text("SELECT count(*) FROM users WHERE hospital_id = 2")).scalar() == 0
        assert c.execute(text("SELECT count(*) FROM inventory_items")).scalar() > 600


def test_rls_fails_closed_without_tenant():
    with app_engine().begin() as c:
        assert c.execute(text("SELECT count(*) FROM inventory_items")).scalar() == 0


def test_rls_blocks_cross_tenant_write():
    with pytest.raises(sqlalchemy.exc.ProgrammingError):
        with tenant_tx(1) as c:
            c.execute(text("INSERT INTO audit_log (hospital_id, user_id, request_id, raw_query, decision) "
                           "VALUES (2, 8, gen_random_uuid(), 'x', 'answered')"))
    with tenant_tx(1) as c:
        res = c.execute(text("UPDATE inventory_items SET quantity = 0 WHERE hospital_id = 2"))
        assert res.rowcount == 0


def test_item_ids_from_other_hospital_are_invisible(orch):
    b_item = owner_scalar("SELECT min(id) FROM inventory_items WHERE hospital_id=2")
    INTENTS[f"Show inventory item {b_item}"] = {"action": "inventory.query", "item_ids": [b_item]}
    r = make_orchestrator(INTENTS).handle(principal(ADMIN_A), f"Show inventory item {b_item}")
    assert r.type == "answer" and r.answer.total == 0


def test_app_role_cannot_delete_bills_or_tamper_audit():
    with pytest.raises(sqlalchemy.exc.ProgrammingError):
        with tenant_tx(1) as c:
            c.execute(text("DELETE FROM bills"))
    with pytest.raises(sqlalchemy.exc.ProgrammingError):
        with tenant_tx(1) as c:
            c.execute(text("UPDATE audit_log SET reason = 'x'"))
    with pytest.raises(sqlalchemy.exc.DBAPIError, match="append-only"):
        with owner_engine().begin() as c:           # even the owner hits the append-only trigger
            c.execute(text("INSERT INTO audit_log (hospital_id, user_id, request_id, raw_query, decision) "
                           "VALUES (1, 1, gen_random_uuid(), 'x', 'answered')"))
            c.execute(text("DELETE FROM audit_log"))


def test_front_desk_sees_only_own_bills(orch):
    r = orch.handle(principal(FRONTDESK_A), "Show all bills")
    assert r.type == "answer"
    assert r.answer.total == owner_scalar("SELECT count(*) FROM bills WHERE hospital_id=1 AND created_by=:u", u=FRONTDESK_A)
    assert r.answer.total < owner_scalar("SELECT count(*) FROM bills WHERE hospital_id=1")


# ------------------------------------------------------------- undo

def test_undo_restores_before_snapshot(orch, fresh_db):
    p = principal(ADMIN_A)
    before = owner_scalar("SELECT sum(quantity) FROM inventory_items WHERE hospital_id=1 AND name ILIKE '%paracetamol%'")
    r = orch.handle(p, "Set paracetamol quantity to 77")
    orch.confirm(p, r.preview.operation_id, typed_count=None)
    assert owner_scalar(PARA_77) > 0
    u = orch.handle(p, "Undo my last change")
    assert u.type == "preview" and u.preview.action == "undo"
    assert orch.confirm(p, u.preview.operation_id, typed_count=None).type == "executed"
    assert owner_scalar("SELECT sum(quantity) FROM inventory_items WHERE hospital_id=1 AND name ILIKE '%paracetamol%'") == before
    again = orch.handle(p, "Undo my last change")
    assert again.type == "answer" and "already been undone" in again.message


def test_undo_refused_if_rows_touched_since(orch, fresh_db):
    p = principal(ADMIN_A)
    r = orch.handle(p, "Set paracetamol quantity to 77")
    orch.confirm(p, r.preview.operation_id, typed_count=None)
    with owner_engine().begin() as c:
        c.execute(text("UPDATE inventory_items SET quantity = 1, version = version + 1 "
                       "WHERE id = (SELECT min(id) FROM inventory_items WHERE hospital_id=1 AND name ILIKE '%paracetamol%')"))
    u = orch.handle(p, "Undo my last change")
    assert u.type == "refusal" and "modified" in u.message


# ------------------------------------------------------------- approval routing

def test_billing_manager_over_limit_routes_to_admin(orch, fresh_db):
    bm = principal(BILLING_A)
    r = orch.handle(bm, "Give 20% discount on all bills from last month")
    sugg = [s for s in r.refusal.suggestions if s.kind == "request_approval"][0]
    req = orch.request_approval(bm, sugg.audit_id)
    assert "approval request #1" in req.message
    with pytest.raises(ConfirmError):                   # can't reuse someone else's refusal
        orch.request_approval(principal(ADMIN_A), sugg.audit_id)

    admin = principal(ADMIN_A)
    pv = orch.handle(admin, "Approve request 1")
    assert pv.type == "preview" and pv.preview.action == "bills.discount"
    n = pv.preview.rows_affected
    assert orch.confirm(admin, pv.preview.operation_id, typed_count=n).type == "executed"
    assert owner_scalar("SELECT status FROM approval_requests WHERE id=1") == "approved"


def test_billing_manager_cannot_approve_own_request(orch, fresh_db):
    bm = principal(BILLING_A)
    r = orch.handle(bm, "Give 20% discount on all bills from last month")
    sugg = [s for s in r.refusal.suggestions if s.kind == "request_approval"][0]
    orch.request_approval(bm, sugg.audit_id)
    out = orch.handle(bm, "Approve request 1")
    assert out.type == "refusal" and out.refusal.code == "discount_ceiling"


# ------------------------------------------------------------- audit completeness

def test_every_chat_attempt_writes_audit(orch, fresh_db):
    p = principal(ADMIN_A)
    start = owner_scalar("SELECT count(*) FROM audit_log")
    orch.handle(p, "Show all bills")                     # answered
    orch.handle(p, "Update quantities")                  # clarified
    orch.handle(principal(FRONTDESK_A), "Set all inventory quantities to 5")  # refused
    orch.handle(p, "Set paracetamol quantity to 77")      # previewed
    orch.handle(p, "this text is not in the static parser")  # parser failure
    assert owner_scalar("SELECT count(*) FROM audit_log") == start + 5
    decisions = [r for (r,) in owner_engine().connect().execute(
        text("SELECT decision FROM audit_log ORDER BY id DESC LIMIT 5")).all()]
    assert sorted(decisions) == sorted(["answered", "clarified", "refused", "previewed", "failed"])
