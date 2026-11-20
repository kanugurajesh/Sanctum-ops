"""Who is acting. Always built from the session token + database, never from the prompt."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from app.policy.permissions import Override, PermissionCatalog, effective_permissions


@dataclass(frozen=True)
class Principal:
    user_id: int
    hospital_id: int
    name: str
    email: str
    hospital_name: str
    hospital_city: str
    hospital_code: str
    roles: tuple[str, ...]
    permissions: Mapping[str, Mapping] = field(default_factory=dict)

    def has(self, permission: str) -> bool:
        return permission in self.permissions

    def constraints(self, permission: str) -> Mapping:
        return self.permissions.get(permission, {})


@dataclass(frozen=True)
class TargetUser:
    """A user being viewed or modified by an access-management operation (same hospital)."""
    user_id: int
    name: str
    email: str
    is_active: bool
    roles: tuple[str, ...]
    overrides: tuple[Override, ...] = ()

    def permissions(self, catalog: PermissionCatalog) -> dict[str, dict]:
        return effective_permissions(self.roles, catalog.role_grants, self.overrides)
