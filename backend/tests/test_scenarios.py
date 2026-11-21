"""The 12 required scenarios, end to end (normalize -> policy -> DB -> preview -> confirm -> audit).
The LLM is replaced by StaticParser holding the intent Gemini is expected to propose, so these
tests exercise every layer *except* language understanding (covered by eval/run_eval.py)."""
from datetime import date

import pytest
from sqlalchemy import text

from app.operations.dates import resolve, today_ist
from app.orchestrator import ConfirmError
from app.seed import INJECTION_ITEM
from tests.conftest import (
    ADMIN_A, ADMIN_B, BILLING_A, DOCTOR_A, FRONTDESK_A, FRONTDESK2_A, PHARMACIST_A,
    make_orchestrator, owner_scalar, principal,
)

INTENTS = {
    "Bulk update the entire inventory with quantity 5":
        {"action": "inventory.update", "scope_all": True, "quantity_op": "set", "quantity_value": 5},
    "Set all inventory quantities to 5":
        {"action": "inventory.update", "scope_all": True, "quantity_op": "set", "quantity_value": 5},
    "Give 20% discount on all bills from last month":
        {"action": "bills.discount", "scope_all": True, "discount_pct": 20, "date_range": {"preset": "last_month"}},
    "Remove inventory edit access from Ravi":
        {"action": "access.revoke", "target_user_name": "Ravi", "permission": "inventory.update"},
    "Give Ravi access": {"action": "access.grant", "target_user_name": "Ravi"},
    "Remove my admin access": {"action": "access.revoke", "target_self": True, "role": "admin"},
    "Make me an admin": {"action": "access.grant", "target_self": True, "role": "admin"},
    "Update stock of paracetamol at Hospital B to 100":
        {"action": "inventory.update", "item_name_contains": "paracetamol", "quantity_op": "set",
         "quantity_value": 100, "mentioned_hospital": "Hospital B"},
    "Show items expiring in the next 30 days":
        {"action": "inventory.query", "date_range": {"preset": "next_n_days", "n": 30}},
    "Delete all bills": {"action": "bills.delete", "scope_all": True, "requested_permission": "bills.delete"},
    "Summarise low-stock items": {"action": "inventory.query", "low_stock": True, "mode": "summary"},
}


@pytest.fixture
def orch():
    return make_orchestrator(INTENTS)


def audit_rows(request_id):
    with __import__("app.db.engine", fromlist=["owner_engine"]).owner_engine().begin() as c:
        return c.execute(text("SELECT * FROM audit_log WHERE request_id = :r ORDER BY id"), {"r": request_id}).mappings().all()


def no_business_table_queries(statements, table):
    return not any(table in s for s in statements)


# ---------------------------------------------------------------------------- 1

def test_s1_pharmacist_bulk_update_refused_over_cap(orch, fresh_db):
    r = orch.handle(principal(PHARMACIST_A), "Bulk update the entire inventory with quantity 5")
    expected = owner_scalar("SELECT count(*) FROM inventory_items WHERE hospital_id=1 AND quantity <> 5")
    assert f"change {expected} rows" in r.message
    assert r.type == "refusal" and r.refusal.code == "row_cap_exceeded"
    assert "500" in r.refusal.reason and "category" in r.refusal.reason.lower()
    assert r.refusal.suggestions, "should suggest narrowing by category"
    assert owner_scalar("SELECT count(*) FROM pending_operations") == 0
    assert audit_rows(r.request_id)[0]["decision"] == "refused"


def test_s1_admin_same_query_previews_confirms_executes_logs(orch, fresh_db):
    p = principal(ADMIN_A)
    r = orch.handle(p, "Bulk update the entire inventory with quantity 5")
    assert r.type == "preview"
    pv = r.preview
    expected = owner_scalar("SELECT count(*) FROM inventory_items WHERE hospital_id=1 AND quantity <> 5")
    assert pv.rows_affected == expected > 600
    assert len(pv.samples) == 5 and all(s.after["quantity"] == 5 for s in pv.samples)
    assert pv.requires_typed_count and pv.money_impact
    # nothing written yet
    assert owner_scalar("SELECT count(*) FROM inventory_items WHERE hospital_id=1 AND quantity <> 5") == expected

    with pytest.raises(ConfirmError):
        orch.confirm(p, pv.operation_id, typed_count=None)          # "yes" is not enough
    with pytest.raises(ConfirmError):
        orch.confirm(p, pv.operation_id, typed_count=expected - 1)  # wrong count
    done = orch.confirm(p, pv.operation_id, typed_count=expected)
    assert done.type == "executed" and done.executed.rows_affected == expected
    assert owner_scalar("SELECT count(*) FROM inventory_items WHERE hospital_id=1 AND quantity <> 5") == 0
    assert owner_scalar("SELECT count(*) FROM inventory_items WHERE hospital_id=2 AND quantity = 5") < 50  # B untouched
    rows = audit_rows(r.request_id)
    # the two wrong-count attempts above are audited too
    assert [x["decision"] for x in rows] == ["previewed", "refused", "refused", "executed"]
    ex = rows[-1]
    assert ex["rows_affected"] == expected and ex["raw_query"] and ex["parsed_intent"]["action"] == "inventory.update"
    assert len(ex["before_snapshot"]["rows"]) == expected and len(ex["after_snapshot"]["rows"]) == expected


# ---------------------------------------------------------------------------- 2

def test_s2_front_desk_refused_without_touching_inventory(orch, sql_log):
    r = orch.handle(principal(FRONTDESK_A), "Set all inventory quantities to 5")
    assert r.type == "refusal" and r.refusal.code == "no_permission" and "inventory.update" in r.refusal.reason
    assert no_business_table_queries(sql_log, "inventory_items")
    assert audit_rows(r.request_id)[0]["reason_code"] == "no_permission"


# ---------------------------------------------------------------------------- 3

def test_s3_billing_manager_20pct_refused_offers_10(orch, sql_log):
    r = orch.handle(principal(BILLING_A), "Give 20% discount on all bills from last month")
    assert r.type == "refusal" and r.refusal.code == "discount_ceiling"
    labels = [s.label for s in r.refusal.suggestions]
    assert any("10%" in l for l in labels)
    offer = [s for s in r.refusal.suggestions if "10%" in s.label and s.kind == "query"][0]
    lm = resolve("last_month", today_ist())
    assert "10%" in offer.message and lm.start.isoformat() in offer.message
    assert any(s.kind == "request_approval" and s.audit_id for s in r.refusal.suggestions)
    assert no_business_table_queries(sql_log, "bills")


# ---------------------------------------------------------------------------- 4

def test_s4_admin_20pct_last_month_ist(orch, fresh_db):
    p = principal(ADMIN_A)
    r = orch.handle(p, "Give 20% discount on all bills from last month")
    assert r.type == "preview"
    lm = resolve("last_month", today_ist())
    assert r.preview.date_range == lm.label and "(IST)" in r.preview.date_range
    expected = owner_scalar(
        "SELECT count(*) FROM bills WHERE hospital_id=1 AND bill_date BETWEEN :s AND :e "
        "AND status NOT IN ('cancelled','refunded') AND discount_pct < 20", s=lm.start, e=lm.end)
    assert r.preview.rows_affected == expected > 50
    assert r.preview.money_impact.startswith("-₹")
    assert all(s.after["discount_pct"] == "20.00" for s in r.preview.samples)
    done = orch.confirm(p, r.preview.operation_id, typed_count=expected)
    assert done.type == "executed"
    assert owner_scalar(
        "SELECT count(*) FROM bills WHERE hospital_id=1 AND bill_date BETWEEN :s AND :e "
        "AND status NOT IN ('cancelled','refunded') AND discount_pct < 20", s=lm.start, e=lm.end) == 0
    assert owner_scalar(
        "SELECT count(*) FROM bills WHERE hospital_id=1 AND discount_pct = 20 "
        "AND net_amount <> round(gross_amount * 0.8, 2)") == 0


# ---------------------------------------------------------------------------- 5

def test_s5_admin_removes_inventory_edit_from_ravi(orch, fresh_db):
    p = principal(ADMIN_A)
    r = orch.handle(p, "Remove inventory edit access from Ravi")
    assert r.type == "preview"
    a = r.preview.access
    assert a.user_name == "Ravi Kumar" and a.roles_before == ["pharmacist"]
    assert a.removed == ["inventory.update"] and "inventory.read" in a.permissions_after
    assert not r.preview.requires_typed_count
    assert principal(PHARMACIST_A).has("inventory.update")      # unchanged until confirmed
    done = orch.confirm(p, r.preview.operation_id, typed_count=None)
    assert done.type == "executed"
    ravi = principal(PHARMACIST_A)
    assert not ravi.has("inventory.update") and ravi.has("inventory.read")
    # and the policy now refuses Ravi's own inventory writes
    r2 = orch.handle(ravi, "Bulk update the entire inventory with quantity 5")
    assert r2.type == "refusal" and r2.refusal.code == "no_permission"


def test_s5b_two_ravis_asks_which_one(orch):
    before = owner_scalar("SELECT count(*) FROM pending_operations")
    r = orch.handle(principal(ADMIN_B), "Remove inventory edit access from Ravi")
    assert r.type == "clarification"
    assert len(r.clarification.options) == 2
    assert {"Ravi Teja", "Ravi Shankar"} <= {o.label.split(" — ")[0] for o in r.clarification.options}
    assert owner_scalar("SELECT count(*) FROM pending_operations") == before


# ---------------------------------------------------------------------------- 6

def test_s6_give_ravi_access_asks_which(orch):
    before = owner_scalar("SELECT count(*) FROM pending_operations")
    r = orch.handle(principal(ADMIN_A), "Give Ravi access")
    assert r.type == "clarification" and "which access" in r.message.lower()
    assert r.clarification.options
    assert owner_scalar("SELECT count(*) FROM pending_operations") == before
    assert audit_rows(r.request_id)[0]["decision"] == "clarified"


# ---------------------------------------------------------------------------- 7, 8

def test_s7_self_lockout(orch):
    r = orch.handle(principal(ADMIN_A), "Remove my admin access")
    assert r.type == "refusal" and r.refusal.code == "self_lockout"
    assert principal(ADMIN_A).has("users.manage_access")


def test_s8_privilege_escalation(orch):
    r = orch.handle(principal(BILLING_A), "Make me an admin")
    assert r.type == "refusal" and r.refusal.code == "privilege_escalation"
    assert "admin" not in principal(BILLING_A).roles


# ---------------------------------------------------------------------------- 9

def test_s9_cross_tenant_refused_without_lookup(orch, sql_log):
    r = orch.handle(principal(ADMIN_A), "Update stock of paracetamol at Hospital B to 100")
    assert r.type == "refusal" and r.refusal.code == "cross_tenant"
    assert no_business_table_queries(sql_log, "inventory_items")
    for word in ("lotus", "exist", "found", "paracetamol"):
        assert word not in r.message.lower()


# ---------------------------------------------------------------------------- 10

def test_s10_pharmacist_expiring_items_read_only(orch):
    before = owner_scalar("SELECT count(*) FROM pending_operations")
    r = orch.handle(principal(PHARMACIST_A), "Show items expiring in the next 30 days")
    assert r.type == "answer"
    today = today_ist()
    expected = owner_scalar(
        "SELECT count(*) FROM inventory_items WHERE hospital_id=1 AND expiry_date BETWEEN :s AND :e",
        s=today, e=resolve("next_n_days", today, n=30).end)
    assert r.answer.total == expected > 0
    ids = [row[0] for row in r.answer.table.rows]
    assert owner_scalar("SELECT count(*) FROM inventory_items WHERE id = ANY(:ids) AND hospital_id <> 1", ids=ids) == 0
    assert r.answer.date_range and "(IST)" in r.answer.date_range
    assert owner_scalar("SELECT count(*) FROM pending_operations") == before


# ---------------------------------------------------------------------------- 11

def test_s11_delete_all_bills_refused(orch, sql_log):
    r = orch.handle(principal(ADMIN_A), "Delete all bills")
    assert r.type == "refusal" and r.refusal.code == "not_in_matrix" and "bills.delete" in r.refusal.reason
    assert no_business_table_queries(sql_log, "bills")


# ---------------------------------------------------------------------------- 12

def test_s12_injection_in_data_is_just_data(orch):
    roles_before = owner_scalar("SELECT count(*) FROM user_roles")
    pending_before = owner_scalar("SELECT count(*) FROM pending_operations WHERE status = 'pending'")
    orch.parser.calls.clear()
    r = orch.handle(principal(PHARMACIST_A), "Summarise low-stock items")
    assert r.type == "answer"
    names = [row[1] for row in r.answer.table.rows]
    assert INJECTION_ITEM in names                         # shown as data
    assert orch.parser.calls == ["Summarise low-stock items"]  # LLM called once, on the user's text only
    assert owner_scalar("SELECT count(*) FROM user_roles") == roles_before
    assert owner_scalar("SELECT count(*) FROM user_roles ur JOIN roles r ON r.id = ur.role_id "
                        "WHERE ur.user_id = :u AND r.name = 'admin'", u=FRONTDESK2_A) == 0
    assert owner_scalar("SELECT count(*) FROM pending_operations WHERE status = 'pending'") == pending_before
