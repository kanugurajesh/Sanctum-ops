"""The policy layer: decides whether an operation may run. Pure functions only.

Inputs are the authenticated Principal (from the session), the typed Operation, the permission
catalog and, for some checks, facts the execution layer measured (row counts, the target user).
Nothing here reads model output as authority: the Operation says what is *requested*, the
Principal says what is *allowed*.

Check order in the request flow (see orchestrator.py):
  1. check_request  - before any business-table query: tenant, permission, value ceilings
  2. check_access_change - after resolving the target user (access ops only)
  3. check_scope    - after counting affected rows: per-operation row caps
  row_scope         - extra WHERE clauses the executor must apply (own-records-only)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from app.operations.model import Action, Operation
from app.policy.permissions import (
    Override, PermissionCatalog, ROLE_NAMES, covers, effective_permissions,
)
from app.policy.principal import Principal, TargetUser

ACTION_PERMISSION: dict[Action, str | None] = {
    Action.INVENTORY_QUERY: "inventory.read",
    Action.INVENTORY_UPDATE: "inventory.update",
    Action.INVENTORY_DELETE: "inventory.delete",
    Action.BILLS_QUERY: "bills.read",
    Action.BILLS_DISCOUNT: "bills.discount",
    Action.BILLS_UPDATE_STATUS: "bills.update_status",
    Action.BILLS_DELETE: "bills.delete",
    Action.USERS_QUERY: "users.read",
    Action.ACCESS_SHOW: "users.read",
    Action.ACCESS_GRANT: "users.manage_access",
    Action.ACCESS_REVOKE: "users.manage_access",
    Action.APPROVALS_LIST: None,      # anyone may list approvals relevant to them
    Action.APPROVALS_APPROVE: None,   # checked against the approved operation itself
    Action.APPROVALS_REJECT: None,
    Action.UNDO: None,                # checked against the original operation's permission
    Action.HELP: None,
    Action.UNSUPPORTED: None,
}


class Code(str, Enum):
    OK = "ok"
    CROSS_TENANT = "cross_tenant"
    NO_PERMISSION = "no_permission"
    NOT_IN_MATRIX = "not_in_matrix"
    UNSUPPORTED = "unsupported"
    PRIVILEGE_ESCALATION = "privilege_escalation"
    SELF_LOCKOUT = "self_lockout"
    LAST_ADMIN = "last_admin"
    GRANT_NOT_HELD = "grant_not_held"
    DISCOUNT_CEILING = "discount_ceiling"
    ROW_CAP = "row_cap_exceeded"
    INVALID_VALUE = "invalid_value"
    TARGET_INACTIVE = "target_inactive"


@dataclass
class Suggestion:
    label: str
    message: str | None = None        # a chat message the UI can send on click
    kind: str = "query"               # 'query' | 'request_approval'


@dataclass
class Decision:
    allowed: bool
    code: Code = Code.OK
    reason: str = ""
    suggestions: list[Suggestion] = field(default_factory=list)

    @classmethod
    def allow(cls) -> "Decision":
        return cls(True)

    @classmethod
    def deny(cls, code: Code, reason: str, suggestions: list[Suggestion] | None = None) -> "Decision":
        return cls(False, code, reason, suggestions or [])


@dataclass(frozen=True)
class RowScope:
    """Extra row restrictions the executor must apply on top of hospital_id."""
    created_by: int | None = None


def _roles_text(p: Principal) -> str:
    return ", ".join(r.replace("_", " ") for r in p.roles) or "no role"


# ---------------------------------------------------------------- tenant

_GENERIC_SELF = {"our hospital", "this hospital", "my hospital", "here", "own hospital", "our clinic", "this clinic",
                 # Hinglish / romanised Telugu
                 "hamara hospital", "hamare hospital", "apna hospital", "apne hospital", "yahan",
                 "mana hospital", "maa hospital", "mana clinic", "maa clinic", "ikkada"}
_FILLER = {"hospital", "clinic", "the", "at", "in", "of", "multispecialty", "mein", "me", "lo", "ka", "ki", "ke"}
# Non-Latin words that may appear around a hospital name ("హాస్పిటల్ B లో"). Any OTHER non-Latin
# word could be a hospital name we cannot match, so it makes the mention count as foreign.
_NATIVE_FILLER = {"హాస్పిటల్", "ఆసుపత్రి", "ఆస్పత్రి", "క్లినిక్", "లో", "అస్పతాల్",
                  "अस्पताल", "हॉस्पिटल", "क्लिनिक", "में", "का", "की", "के"}


def _tokens(s: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", s.lower()) if t}


def _unmatched_native_words(s: str) -> bool:
    # Split on whitespace/punctuation only: \w does not cover Indic vowel signs and would cut words up.
    words = re.split(r"[\s,.;:!?()\[\]/'\"\-]+", s)
    return any(w not in _NATIVE_FILLER for w in words if w and not w.isascii())


def mentions_own_hospital(mention: str, p: Principal) -> bool:
    """Does a hospital name mentioned in the request refer to the user's own hospital?
    Anything we cannot positively match to the user's own hospital is treated as foreign,
    including names in a script we cannot match (e.g. 'లోటస్'): fail closed."""
    if mention.strip().lower() in _GENERIC_SELF:
        return True
    if _unmatched_native_words(mention):
        return False
    if not _tokens(mention):
        return True
    mt = _tokens(mention) - _FILLER
    if not mt:
        return True  # e.g. just "hospital"
    own = _tokens(p.hospital_name) | _tokens(p.hospital_city) | {p.hospital_code.lower()}
    return mt <= own


# ---------------------------------------------------------------- stage 1

def required_permission(op: Operation) -> str | None:
    if op.action == Action.UNSUPPORTED:
        return op.requested_permission
    return ACTION_PERMISSION.get(op.action)


def check_request(p: Principal, op: Operation, catalog: PermissionCatalog) -> Decision:
    """Runs before any query against business tables."""
    # Tenant: a request that names another hospital is refused without looking anything up,
    # so the answer cannot reveal whether data exists there.
    if op.mentioned_hospital and not mentions_own_hospital(op.mentioned_hospital, p):
        return Decision.deny(
            Code.CROSS_TENANT,
            f"You can only view or change data for your own hospital ({p.hospital_name}). "
            "Requests about other hospitals are not allowed, so nothing was looked up.",
        )

    if op.action == Action.UNSUPPORTED:
        perm = op.requested_permission
        if perm and not catalog.any_role_has(perm):
            return Decision.deny(
                Code.NOT_IN_MATRIX,
                f"No role in the permission matrix has `{perm}`, so this operation cannot be "
                "performed by anyone through this assistant.",
            )
        if perm and not p.has(perm):
            return Decision.deny(Code.NO_PERMISSION, f"Your role ({_roles_text(p)}) does not have `{perm}`.")
        return Decision.deny(Code.UNSUPPORTED, "That operation is not supported by this assistant yet.")

    # Viewing your own access is always allowed.
    if op.action == Action.ACCESS_SHOW and op.target_self:
        return Decision.allow()

    perm = required_permission(op)
    if perm is None:
        return Decision.allow()

    # Asking to change your own access without users.manage_access is privilege escalation.
    if op.action in (Action.ACCESS_GRANT, Action.ACCESS_REVOKE) and op.target_self and not p.has(perm):
        return Decision.deny(
            Code.PRIVILEGE_ESCALATION,
            "You cannot change your own roles or permissions — that would be privilege escalation. "
            "Ask a hospital admin if you need more access.",
        )

    if not catalog.any_role_has(perm):
        return Decision.deny(
            Code.NOT_IN_MATRIX,
            f"No role in the permission matrix has `{perm}`, so this operation cannot be "
            "performed by anyone through this assistant.",
        )
    if not p.has(perm):
        return Decision.deny(Code.NO_PERMISSION, f"Your role ({_roles_text(p)}) does not have `{perm}`.")

    return _check_values(p, op)


def _check_values(p: Principal, op: Operation) -> Decision:
    if op.action == Action.BILLS_DISCOUNT and op.discount_pct is not None:
        return check_discount(p, op.discount_pct, op)
    return Decision.allow()


def check_discount(p: Principal, pct: Decimal, op: Operation | None = None) -> Decision:
    if pct < 0 or pct > 100:
        return Decision.deny(Code.INVALID_VALUE, "A discount must be between 0% and 100%.")
    ceiling = Decimal(str(p.constraints("bills.discount").get("max_discount_pct", 100)))
    if pct > ceiling:
        suggestions = [Suggestion(label=f"Apply {_fmt_pct(ceiling)}% instead", message=None)]
        suggestions.append(Suggestion(label=f"Request admin approval for {_fmt_pct(pct)}%", kind="request_approval"))
        return Decision.deny(
            Code.DISCOUNT_CEILING,
            f"A {_fmt_pct(pct)}% discount exceeds your limit of {_fmt_pct(ceiling)}% "
            f"({_roles_text(p)}). I can apply {_fmt_pct(ceiling)}% instead, or send this to an admin for approval.",
            suggestions,
        )
    return Decision.allow()


def _fmt_pct(v: Decimal) -> str:
    return f"{v.normalize():f}" if v == v.to_integral() else f"{v:f}"


# ---------------------------------------------------------------- stage 3

def check_scope(p: Principal, op: Operation, affected_rows: int, narrowing_hint: str | None = None) -> Decision:
    """Row caps (e.g. pharmacist: max 500 rows per inventory.update)."""
    perm = required_permission(op)
    if perm is None:
        return Decision.allow()
    cap = p.constraints(perm).get("max_rows")
    if cap is not None and affected_rows > int(cap):
        hint = f" {narrowing_hint}" if narrowing_hint else " Narrow it down, for example by category."
        return Decision.deny(
            Code.ROW_CAP,
            f"This would change {affected_rows} rows, but your role ({_roles_text(p)}) may change at most "
            f"{cap} rows per operation with `{perm}`.{hint}",
        )
    return Decision.allow()


def row_scope(p: Principal, op: Operation) -> RowScope:
    perm = required_permission(op)
    if perm and p.constraints(perm).get("own_records_only"):
        return RowScope(created_by=p.user_id)
    return RowScope()


# ---------------------------------------------------------------- access management

@dataclass(frozen=True)
class AccessChange:
    roles_after: tuple[str, ...]
    overrides_after: tuple[Override, ...]
    noop: bool
    description: str


def plan_access_change(
    p: Principal, op: Operation, target: TargetUser, catalog: PermissionCatalog,
) -> AccessChange:
    """Compute the target's roles/overrides after the change (pure)."""
    held_before = target.permissions(catalog)
    roles = list(target.roles)
    ovs = {o.permission: o for o in target.overrides}
    desc = ""
    noop = False
    if op.action == Action.ACCESS_GRANT and op.role:
        if op.role in roles:
            noop, desc = True, f"{target.name} already has the {op.role} role."
        else:
            roles.append(op.role)
            desc = f"Add role {op.role} to {target.name}."
    elif op.action == Action.ACCESS_REVOKE and op.role:
        if op.role not in roles:
            noop, desc = True, f"{target.name} does not have the {op.role} role."
        else:
            roles.remove(op.role)
            desc = f"Remove role {op.role} from {target.name}."
    elif op.action == Action.ACCESS_GRANT and op.permission:
        perm = op.permission
        existing = ovs.get(perm)
        if existing and existing.effect == "deny":
            del ovs[perm]
            if perm not in effective_permissions(roles, catalog.role_grants, ovs.values()):
                ovs[perm] = Override(perm, "grant", dict(p.constraints(perm)))
            desc = f"Lift the block on {perm} for {target.name}."
        elif perm in held_before:
            noop, desc = True, f"{target.name} already has {perm}."
        else:
            # A granted permission carries the granter's own constraints: never looser.
            ovs[perm] = Override(perm, "grant", dict(p.constraints(perm)))
            desc = f"Grant {perm} to {target.name}."
    elif op.action == Action.ACCESS_REVOKE and op.permission:
        perm = op.permission
        existing = ovs.get(perm)
        if existing and existing.effect == "deny":
            noop, desc = True, f"{perm} is already blocked for {target.name}."
        elif perm not in held_before:
            noop, desc = True, f"{target.name} does not have {perm}."
        else:
            if existing and existing.effect == "grant":
                del ovs[perm]
            if perm in effective_permissions(roles, catalog.role_grants, ovs.values()):
                ovs[perm] = Override(perm, "deny", {})  # still granted by a role: block it
            desc = f"Remove {perm} from {target.name} (other permissions are unchanged)."
    return AccessChange(tuple(roles), tuple(ovs.values()), noop, desc)


def check_access_change(
    p: Principal, op: Operation, catalog: PermissionCatalog, target: TargetUser,
    change: AccessChange, active_admins_in_hospital: int,
) -> Decision:
    if op.role and op.role not in ROLE_NAMES:
        return Decision.deny(Code.INVALID_VALUE, f"Unknown role `{op.role}`.")
    if op.permission and op.permission not in catalog.all_permissions:
        return Decision.deny(Code.INVALID_VALUE, f"Unknown permission `{op.permission}`.")
    if not target.is_active and op.action == Action.ACCESS_GRANT:
        return Decision.deny(Code.TARGET_INACTIVE, f"{target.name}'s account is inactive.")

    is_self = target.user_id == p.user_id
    before = target.permissions(catalog)
    after = effective_permissions(change.roles_after, catalog.role_grants, change.overrides_after)

    # Grant only what you hold, with constraints no looser than your own.
    for perm, cons in after.items():
        gained = perm not in before or not covers(before[perm], cons)
        if gained and (not p.has(perm) or not covers(p.constraints(perm), cons)):
            return Decision.deny(
                Code.GRANT_NOT_HELD,
                f"You can only grant permissions you hold yourself (with the same or tighter limits). "
                f"This change would give {target.name} `{perm}`"
                + (f" with limits {cons}" if cons else "") + ", which you do not have.",
            )

    # No self-lockout: you may not remove your own ability to manage access (incl. admin role).
    if is_self and "users.manage_access" in before and "users.manage_access" not in after:
        return Decision.deny(
            Code.SELF_LOCKOUT,
            "You cannot remove your own admin access — that would lock you out of access management. "
            "Another admin must do this.",
        )
    if is_self and op.action == Action.ACCESS_REVOKE and op.role == "admin":
        return Decision.deny(Code.SELF_LOCKOUT, "You cannot remove your own admin role. Another admin must do this.")

    # Never leave a hospital without an admin.
    if "admin" in target.roles and "admin" not in change.roles_after and active_admins_in_hospital <= 1:
        return Decision.deny(Code.LAST_ADMIN, f"{target.name} is the only active admin of this hospital.")

    return Decision.allow()
