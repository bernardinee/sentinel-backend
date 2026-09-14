"""Shared-secret API-key auth with a role stub (§5.4 'write the seam, not the door').

Every route depends on `require_role(...)`, which yields a Principal. Swapping in
real JWT auth later means replacing `resolve_principal` only — no route changes.
"""
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Security
from fastapi.security import APIKeyHeader

from app.config import get_settings

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


@dataclass(frozen=True)
class Principal:
    role: str  # driver | responder | device


def resolve_principal(api_key: str | None = Security(api_key_header)) -> Principal:
    if not api_key:
        raise HTTPException(status_code=401, detail="Missing X-API-Key header")
    role = get_settings().role_key_map().get(api_key)
    if role is None:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return Principal(role=role)


def require_role(*roles: str):
    """Dependency factory. `require_role()` (no args) accepts any valid key."""

    def checker(principal: Principal = Depends(resolve_principal)) -> Principal:
        if roles and principal.role not in roles:
            raise HTTPException(status_code=403, detail=f"Role '{principal.role}' not permitted")
        return principal

    return checker
