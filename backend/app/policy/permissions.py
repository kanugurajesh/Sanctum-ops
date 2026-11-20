"""Effective-permission math. Pure functions: no DB, no LLM, no I/O.

Constraint keys (stored as JSON on role_permissions / user_permission_overrides):
  max_rows          int   - a single operation may touch at most this many rows (absent = unlimited)
  max_discount_pct  num   - discount ceiling in percent (absent = 100)
  own_records_only  bool  - only rows the user created (absent = false)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping

Constraints = Mapping[str, object]

ROLE_NAMES = ("admin", "billing_manager", "pharmacist", "front_desk", "doctor")

# The full permission catalog. bills.delete exists so it can be named in refusals,
# but no role is granted it.
PERMISSION_CODES = (
    "inventory.read", "inventory.create", "inventory.update", "inventory.delete",
    "bills.read", "bills.create", "bills.discount", "bills.update_status", "bills.delete",
    "users.read", "users.manage_access", "audit.read",
)


def merge_constraints(a: Constraints, b: Constraints) -> dict:
    """Combine two grants of the same permission: the more permissive one wins per key."""
    out: dict = {}
    # max_rows: absent means unlimited, so it only survives if both grants cap it.
    if "max_rows" in a and "max_rows" in b:
        out["max_rows"] = max(int(a["max_rows"]), int(b["max_rows"]))
    # max_discount_pct: absent means 100.
    if "max_discount_pct" in a and "max_discount_pct" in b:
        out["max_discount_pct"] = max(float(a["max_discount_pct"]), float(b["max_discount_pct"]))
    # own_records_only: restriction holds only if every grant has it.
    if a.get("own_records_only") and b.get("own_records_only"):
        out["own_records_only"] = True
    return out


def covers(holder: Constraints, granted: Constraints) -> bool:
    """True if `holder` is at least as permissive as `granted` (used for 'grant only what you hold')."""
    if "max_rows" in holder:
        if "max_rows" not in granted or int(granted["max_rows"]) > int(holder["max_rows"]):
            return False
    if "max_discount_pct" in holder:
        if float(granted.get("max_discount_pct", 100)) > float(holder["max_discount_pct"]):
            return False
    if holder.get("own_records_only") and not granted.get("own_records_only"):
        return False
    return True


@dataclass(frozen=True)
class Override:
    permission: str
    effect: str  # 'grant' | 'deny'
    constraints: Mapping = field(default_factory=dict)


def effective_permissions(
    roles: Iterable[str],
    role_grants: Mapping[str, Mapping[str, Constraints]],
    overrides: Iterable[Override] = (),
) -> dict[str, dict]:
    """permission code -> merged constraints, for a user holding `roles` plus per-user overrides.
    A 'deny' override always wins over role grants."""
    perms: dict[str, dict] = {}
    for role in roles:
        for code, cons in role_grants.get(role, {}).items():
            perms[code] = merge_constraints(perms[code], cons) if code in perms else dict(cons)
    overrides = list(overrides)
    for ov in overrides:
        if ov.effect == "grant":
            perms[ov.permission] = (
                merge_constraints(perms[ov.permission], ov.constraints)
                if ov.permission in perms else dict(ov.constraints)
            )
    for ov in overrides:
        if ov.effect == "deny":
            perms.pop(ov.permission, None)
    return perms


@dataclass(frozen=True)
class PermissionCatalog:
    """Role -> permission -> constraints, as configured in the database."""
    role_grants: Mapping[str, Mapping[str, Constraints]]
    all_permissions: frozenset[str] = frozenset(PERMISSION_CODES)

    def any_role_has(self, permission: str) -> bool:
        return any(permission in grants for grants in self.role_grants.values())

    def role_permissions(self, role: str) -> Mapping[str, Constraints]:
        return self.role_grants.get(role, {})


# The permission matrix from the spec. The seed writes exactly this into the DB;
# the pure policy tests use it directly.
DEFAULT_ROLE_GRANTS: dict[str, dict[str, dict]] = {
    "admin": {
        "inventory.read": {}, "inventory.create": {}, "inventory.update": {}, "inventory.delete": {},
        "bills.read": {}, "bills.create": {}, "bills.discount": {"max_discount_pct": 100},
        "bills.update_status": {}, "users.read": {}, "users.manage_access": {}, "audit.read": {},
    },
    "billing_manager": {
        "bills.read": {}, "bills.create": {}, "bills.discount": {"max_discount_pct": 10},
        "bills.update_status": {},
    },
    "pharmacist": {
        "inventory.read": {}, "inventory.create": {}, "inventory.update": {"max_rows": 500},
    },
    "front_desk": {
        "bills.read": {"own_records_only": True}, "bills.create": {},
    },
    "doctor": {
        "inventory.read": {},
    },
}
