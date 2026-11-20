"""Policy-layer unit tests. Pure: no database, no LLM. They build Principals and Operations
directly and assert on Decisions."""
from decimal import Decimal

import pytest

from app.operations.model import Action, BillFilter, InventoryFilter, Operation
from app.policy import engine as policy
from app.policy.engine import Code
from app.policy.permissions import (
    DEFAULT_ROLE_GRANTS, Override, PermissionCatalog, covers, effective_permissions, merge_constraints,
)
from app.policy.principal import Principal, TargetUser

CAT = PermissionCatalog(role_grants=DEFAULT_ROLE_GRANTS)


def P(*roles, uid=1, overrides=()):
    return Principal(
        user_id=uid, hospital_id=1, name="Test", email="t@x", hospital_name="Sunrise Multispecialty Hospital",
        hospital_city="Hyderabad", hospital_code="A", roles=tuple(roles),
        permissions=effective_permissions(roles, DEFAULT_ROLE_GRANTS, overrides),
    )


def T(uid, *roles, overrides=(), name="Ravi Kumar"):
    return TargetUser(uid, name, f"{uid}@x", True, tuple(roles), tuple(overrides))


ADMIN, BM, PHARM, FD, DOC = P("admin"), P("billing_manager", uid=2), P("pharmacist", uid=3), P("front_desk", uid=4), P("doctor", uid=5)


# ------------------------------------------------------------- permission matrix

MATRIX = {
    # permission: (admin, billing_manager, pharmacist, front_desk, doctor)
    "inventory.read": (1, 0, 1, 0, 1),
    "inventory.update": (1, 0, 1, 0, 0),
    "inventory.delete": (1, 0, 0, 0, 0),
    "bills.read": (1, 1, 0, 1, 0),
    "bills.discount": (1, 1, 0, 0, 0),
    "bills.update_status": (1, 1, 0, 0, 0),
    "users.read": (1, 0, 0, 0, 0),
    "users.manage_access": (1, 0, 0, 0, 0),
}


@pytest.mark.parametrize("perm,expected", MATRIX.items())
def test_matrix_matches_spec(perm, expected):
    for principal, allowed in zip((ADMIN, BM, PHARM, FD, DOC), expected):
        assert principal.has(perm) == bool(allowed), (principal.roles, perm)


def test_no_role_has_bills_delete():
    assert not CAT.any_role_has("bills.delete")


def test_constraints_from_matrix():
    assert PHARM.constraints("inventory.update") == {"max_rows": 500}
    assert BM.constraints("bills.discount") == {"max_discount_pct": 10}
    assert ADMIN.constraints("bills.discount") == {"max_discount_pct": 100}
    assert FD.constraints("bills.read") == {"own_records_only": True}


# ------------------------------------------------------------- merging

def test_merge_most_permissive():
    assert merge_constraints({"max_rows": 500}, {}) == {}
    assert merge_constraints({"max_rows": 500}, {"max_rows": 800}) == {"max_rows": 800}
    assert merge_constraints({"max_discount_pct": 10}, {"max_discount_pct": 100}) == {"max_discount_pct": 100}
    assert merge_constraints({"own_records_only": True}, {}) == {}


def test_deny_override_beats_role():
    perms = effective_permissions(["pharmacist"], DEFAULT_ROLE_GRANTS, [Override("inventory.update", "deny")])
    assert "inventory.update" not in perms and "inventory.read" in perms


def test_covers():
    assert covers({}, {"max_rows": 10})
    assert not covers({"max_rows": 500}, {})
    assert covers({"max_discount_pct": 10}, {"max_discount_pct": 5})
    assert not covers({"max_discount_pct": 10}, {"max_discount_pct": 100})
    assert not covers({"own_records_only": True}, {})


# ------------------------------------------------------------- scenario-level policy

def inv_update(**kw):
    kw.setdefault("inventory", InventoryFilter())
    return Operation(action=Action.INVENTORY_UPDATE, scope_all=True, quantity_op="set", quantity_value=5, **kw)


def test_s1_pharmacist_row_cap():
    op = inv_update()
    assert policy.check_request(PHARM, op, CAT).allowed
    d = policy.check_scope(PHARM, op, 650)
    assert not d.allowed and d.code == Code.ROW_CAP and "500" in d.reason
    assert policy.check_scope(PHARM, op, 500).allowed
    assert policy.check_scope(ADMIN, op, 650).allowed


def test_s2_front_desk_cannot_update_inventory():
    d = policy.check_request(FD, inv_update(), CAT)
    assert not d.allowed and d.code == Code.NO_PERMISSION and "inventory.update" in d.reason


def discount(pct):
    return Operation(action=Action.BILLS_DISCOUNT, scope_all=True, bills=BillFilter(), discount_pct=Decimal(pct))


def test_s3_billing_manager_discount_ceiling():
    d = policy.check_request(BM, discount(20), CAT)
    assert not d.allowed and d.code == Code.DISCOUNT_CEILING
    assert any("10%" in s.label for s in d.suggestions)
    assert any(s.kind == "request_approval" for s in d.suggestions)
    assert policy.check_request(BM, discount(10), CAT).allowed
    assert policy.check_request(BM, discount("10.01"), CAT).code == Code.DISCOUNT_CEILING


def test_s4_admin_discount_allowed():
    assert policy.check_request(ADMIN, discount(20), CAT).allowed
    assert policy.check_request(ADMIN, discount(100), CAT).allowed
    assert policy.check_request(ADMIN, discount(101), CAT).code == Code.INVALID_VALUE


def test_s5_admin_revokes_inventory_update_keeps_read():
    op = Operation(action=Action.ACCESS_REVOKE, target_user_name="Ravi", permission="inventory.update")
    ravi = T(3, "pharmacist")
    change = policy.plan_access_change(ADMIN, op, ravi, CAT)
    after = effective_permissions(change.roles_after, CAT.role_grants, change.overrides_after)
    assert "inventory.update" not in after and "inventory.read" in after
    assert policy.check_access_change(ADMIN, op, CAT, ravi, change, 1).allowed


def test_s7_self_lockout():
    op = Operation(action=Action.ACCESS_REVOKE, target_self=True, role="admin")
    me = T(1, "admin", name="Anita")
    change = policy.plan_access_change(ADMIN, op, me, CAT)
    d = policy.check_access_change(ADMIN, op, CAT, me, change, active_admins_in_hospital=2)
    assert not d.allowed and d.code == Code.SELF_LOCKOUT
    op2 = Operation(action=Action.ACCESS_REVOKE, target_self=True, permission="users.manage_access")
    change2 = policy.plan_access_change(ADMIN, op2, me, CAT)
    assert policy.check_access_change(ADMIN, op2, CAT, me, change2, 2).code == Code.SELF_LOCKOUT


def test_s8_privilege_escalation():
    op = Operation(action=Action.ACCESS_GRANT, target_self=True, role="admin")
    d = policy.check_request(BM, op, CAT)
    assert not d.allowed and d.code == Code.PRIVILEGE_ESCALATION


def test_non_admin_cannot_grant_others():
    op = Operation(action=Action.ACCESS_GRANT, target_user_name="Priya", role="admin")
    assert policy.check_request(PHARM, op, CAT).code == Code.NO_PERMISSION


@pytest.mark.parametrize("mention", ["Hospital B", "Lotus Care Clinic", "Vijayawada", "lotus", "Apollo"])
def test_s9_cross_tenant_refused(mention):
    op = inv_update(mentioned_hospital=mention)
    d = policy.check_request(ADMIN, op, CAT)
    assert not d.allowed and d.code == Code.CROSS_TENANT
    assert mention.lower() not in d.reason.lower() or mention.lower() in ADMIN.hospital_name.lower()


@pytest.mark.parametrize("mention", ["Hospital A", "Sunrise", "Sunrise Multispecialty Hospital", "Hyderabad", "our hospital"])
def test_own_hospital_mention_allowed(mention):
    assert policy.check_request(ADMIN, inv_update(mentioned_hospital=mention), CAT).allowed


def test_s9_cross_tenant_message_identical_regardless_of_item():
    a = policy.check_request(ADMIN, inv_update(mentioned_hospital="Hospital B", inventory=InventoryFilter(name_contains="paracetamol")), CAT)
    b = policy.check_request(ADMIN, inv_update(mentioned_hospital="Hospital B", inventory=InventoryFilter(name_contains="unicornium")), CAT)
    assert a.reason == b.reason


def test_s10_pharmacist_can_read_inventory():
    assert policy.check_request(PHARM, Operation(action=Action.INVENTORY_QUERY), CAT).allowed


def test_s11_nobody_can_delete_bills():
    op = Operation(action=Action.BILLS_DELETE, scope_all=True, bills=BillFilter(), requested_permission="bills.delete")
    for p in (ADMIN, BM, PHARM, FD, DOC):
        d = policy.check_request(p, op, CAT)
        assert not d.allowed and d.code == Code.NOT_IN_MATRIX and "bills.delete" in d.reason


def test_unsupported_ops_refused():
    op = Operation(action=Action.UNSUPPORTED, requested_permission="users.delete")
    assert policy.check_request(ADMIN, op, CAT).code == Code.NOT_IN_MATRIX
    assert policy.check_request(ADMIN, Operation(action=Action.UNSUPPORTED), CAT).code == Code.UNSUPPORTED


def test_front_desk_own_records_scope():
    assert policy.row_scope(FD, Operation(action=Action.BILLS_QUERY)).created_by == FD.user_id
    assert policy.row_scope(BM, Operation(action=Action.BILLS_QUERY)).created_by is None


def test_doctor_inventory_read_only():
    assert policy.check_request(DOC, Operation(action=Action.INVENTORY_QUERY), CAT).allowed
    assert policy.check_request(DOC, inv_update(), CAT).code == Code.NO_PERMISSION
    assert policy.check_request(DOC, Operation(action=Action.BILLS_QUERY), CAT).code == Code.NO_PERMISSION


def test_dual_role_union():
    kavya = P("doctor", "pharmacist", uid=6)
    assert kavya.has("inventory.update") and kavya.constraints("inventory.update") == {"max_rows": 500}


# ------------------------------------------------------------- grant only what you hold

def test_admin_cannot_grant_unheld_permission():
    op = Operation(action=Action.ACCESS_GRANT, target_user_name="Ravi", permission="bills.delete")
    ravi = T(3, "pharmacist")
    change = policy.plan_access_change(ADMIN, op, ravi, CAT)
    d = policy.check_access_change(ADMIN, op, CAT, ravi, change, 1)
    assert not d.allowed and d.code == Code.GRANT_NOT_HELD


def test_limited_admin_cannot_grant_looser_constraints():
    # An "admin" whose discount power is capped at 10% must not grant a role with 100%.
    limited = Principal(1, 1, "L", "l@x", "Sunrise Multispecialty Hospital", "Hyderabad", "A", ("admin",),
                        {**effective_permissions(["admin"], DEFAULT_ROLE_GRANTS), "bills.discount": {"max_discount_pct": 10}})
    op = Operation(action=Action.ACCESS_GRANT, target_user_name="Priya", role="admin")
    priya = T(4, "front_desk", name="Priya")
    change = policy.plan_access_change(limited, op, priya, CAT)
    assert policy.check_access_change(limited, op, CAT, priya, change, 1).code == Code.GRANT_NOT_HELD
    op2 = Operation(action=Action.ACCESS_GRANT, target_user_name="Priya", role="billing_manager")
    change2 = policy.plan_access_change(limited, op2, priya, CAT)
    assert policy.check_access_change(limited, op2, CAT, priya, change2, 1).allowed


def test_last_admin_cannot_be_removed():
    other_admin = T(9, "admin", name="Other")
    op = Operation(action=Action.ACCESS_REVOKE, target_user_name="Other", role="admin")
    change = policy.plan_access_change(ADMIN, op, other_admin, CAT)
    assert policy.check_access_change(ADMIN, op, CAT, other_admin, change, 1).code == Code.LAST_ADMIN
    assert policy.check_access_change(ADMIN, op, CAT, other_admin, change, 2).allowed


def test_granted_permission_inherits_granter_constraints():
    op = Operation(action=Action.ACCESS_GRANT, target_user_name="Priya", permission="bills.discount")
    priya = T(4, "front_desk", name="Priya")
    change = policy.plan_access_change(BM, op, priya, CAT)  # BM lacks manage_access; just testing the plan
    grant = [o for o in change.overrides_after if o.permission == "bills.discount"][0]
    assert grant.constraints == {"max_discount_pct": 10}


def test_regrant_after_deny_lifts_block():
    ravi = T(3, "pharmacist", overrides=[Override("inventory.update", "deny")])
    op = Operation(action=Action.ACCESS_GRANT, target_user_name="Ravi", permission="inventory.update")
    change = policy.plan_access_change(ADMIN, op, ravi, CAT)
    after = effective_permissions(change.roles_after, CAT.role_grants, change.overrides_after)
    assert after["inventory.update"] == {"max_rows": 500}
