"""DB loaders for identity and access data. All queries are tenant-scoped
(explicit hospital_id filter + RLS on the connection)."""
from __future__ import annotations

import re

from sqlalchemy import Connection, and_, func, select

from app.db.tables import (
    hospitals, permissions, role_permissions, roles, user_permission_overrides, user_roles, users,
)
from app.policy.permissions import Override, PermissionCatalog, effective_permissions
from app.policy.principal import Principal, TargetUser


def load_catalog(conn: Connection) -> PermissionCatalog:
    rows = conn.execute(
        select(roles.c.name, permissions.c.code, role_permissions.c.constraints)
        .select_from(role_permissions.join(roles, roles.c.id == role_permissions.c.role_id)
                     .join(permissions, permissions.c.id == role_permissions.c.permission_id))
    ).all()
    grants: dict[str, dict[str, dict]] = {}
    for role, code, cons in rows:
        grants.setdefault(role, {})[code] = dict(cons or {})
    all_perms = frozenset(r[0] for r in conn.execute(select(permissions.c.code)).all())
    return PermissionCatalog(role_grants=grants, all_permissions=all_perms)


def _roles_of(conn: Connection, hospital_id: int, user_id: int) -> tuple[str, ...]:
    return tuple(sorted(r[0] for r in conn.execute(
        select(roles.c.name).select_from(user_roles.join(roles, roles.c.id == user_roles.c.role_id))
        .where(user_roles.c.user_id == user_id, user_roles.c.hospital_id == hospital_id)
    ).all()))


def _overrides_of(conn: Connection, hospital_id: int, user_id: int) -> tuple[Override, ...]:
    return tuple(
        Override(code, effect, dict(cons or {}))
        for code, effect, cons in conn.execute(
            select(permissions.c.code, user_permission_overrides.c.effect, user_permission_overrides.c.constraints)
            .select_from(user_permission_overrides.join(
                permissions, permissions.c.id == user_permission_overrides.c.permission_id))
            .where(user_permission_overrides.c.user_id == user_id,
                   user_permission_overrides.c.hospital_id == hospital_id)
            .order_by(permissions.c.code)
        ).all()
    )


def load_principal(conn: Connection, user_id: int, hospital_id: int, catalog: PermissionCatalog) -> Principal | None:
    row = conn.execute(
        select(users.c.id, users.c.name, users.c.email, users.c.is_active,
               hospitals.c.id.label("hid"), hospitals.c.name.label("hname"),
               hospitals.c.city, hospitals.c.code)
        .select_from(users.join(hospitals, hospitals.c.id == users.c.hospital_id))
        .where(users.c.id == user_id, users.c.hospital_id == hospital_id)
    ).mappings().first()
    if row is None or not row["is_active"]:
        return None
    role_names = _roles_of(conn, hospital_id, user_id)
    perms = effective_permissions(role_names, catalog.role_grants, _overrides_of(conn, hospital_id, user_id))
    return Principal(
        user_id=row["id"], hospital_id=row["hid"], name=row["name"], email=row["email"],
        hospital_name=row["hname"], hospital_city=row["city"], hospital_code=row["code"],
        roles=role_names, permissions=perms,
    )


def load_target(conn: Connection, hospital_id: int, user_id: int) -> TargetUser | None:
    row = conn.execute(
        select(users.c.id, users.c.name, users.c.email, users.c.is_active)
        .where(users.c.id == user_id, users.c.hospital_id == hospital_id)
    ).mappings().first()
    if row is None:
        return None
    return TargetUser(
        user_id=row["id"], name=row["name"], email=row["email"], is_active=row["is_active"],
        roles=_roles_of(conn, hospital_id, user_id), overrides=_overrides_of(conn, hospital_id, user_id),
    )


def find_users_by_name(conn: Connection, hospital_id: int, name: str) -> list[TargetUser]:
    """Match on whole words of the name (so 'Ravi' finds 'Ravi Kumar' but not 'Ravindra')."""
    words = [w for w in re.split(r"\s+", name.strip().lower()) if w and w not in {"dr", "dr.", "mr", "ms", "mrs"}]
    if not words:
        return []
    cands = conn.execute(
        select(users.c.id, users.c.name).where(users.c.hospital_id == hospital_id).order_by(users.c.id)
    ).all()
    out = []
    for uid, uname in cands:
        tokens = set(re.split(r"[^a-z0-9]+", uname.lower()))
        if all(w.strip(".") in tokens for w in words):
            t = load_target(conn, hospital_id, uid)
            if t:
                out.append(t)
    return out


def active_admin_count(conn: Connection, hospital_id: int) -> int:
    return conn.execute(
        select(func.count()).select_from(
            user_roles.join(roles, roles.c.id == user_roles.c.role_id)
            .join(users, and_(users.c.id == user_roles.c.user_id))
        ).where(roles.c.name == "admin", users.c.is_active.is_(True), user_roles.c.hospital_id == hospital_id)
    ).scalar_one()


def role_ids(conn: Connection) -> dict[str, int]:
    return {n: i for i, n in conn.execute(select(roles.c.id, roles.c.name)).all()}


def permission_ids(conn: Connection) -> dict[str, int]:
    return {c: i for i, c in conn.execute(select(permissions.c.id, permissions.c.code)).all()}
