"""Authentication and authorization shared by HTTP and WebSocket routes.

Responder/device integrations can continue to use scoped API keys. Driver
accounts use short-lived signed access tokens plus rotating refresh tokens.
"""
import secrets
from dataclasses import dataclass
from datetime import timedelta

import jwt
from fastapi import Depends, HTTPException, Security
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from jwt import InvalidTokenError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import Device, User, as_aware, utcnow

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
bearer_header = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Principal:
    role: str  # driver | responder | device
    subject: str
    user_id: str | None = None
    device_db_id: str | None = None
    device_id: str | None = None


def create_access_token(user: User, device: Device | None) -> tuple[str, int]:
    """Mint an access token. `device` is None for responders, who dispatch
    across the whole fleet rather than owning a single node."""
    settings = get_settings()
    now = utcnow()
    expires_in = settings.ACCESS_TOKEN_MINUTES * 60
    token = jwt.encode(
        {
            "sub": user.id,
            "role": user.role,
            "did": device.id if device is not None else None,
            "device_id": device.device_id if device is not None else None,
            "type": "access",
            "iat": now,
            "exp": now + timedelta(seconds=expires_in),
            "iss": settings.JWT_ISSUER,
            "aud": settings.JWT_AUDIENCE,
        },
        settings.jwt_signing_key(),
        algorithm="HS256",
    )
    return token, expires_in


def principal_from_access_token(token: str, db: Session) -> Principal:
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.jwt_signing_key(),
            algorithms=["HS256"],
            audience=settings.JWT_AUDIENCE,
            issuer=settings.JWT_ISSUER,
            options={"require": ["sub", "exp", "iat", "type"]},
        )
    except InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired access token") from exc
    if payload.get("type") != "access":
        raise HTTPException(status_code=401, detail="Invalid access token")
    user = db.get(User, str(payload["sub"]))
    if user is None or not user.active:
        raise HTTPException(status_code=401, detail="Account is unavailable")

    # Tokens minted before the account's cutoff are dead, so a password change
    # takes effect at once rather than after the access token expires.
    cutoff = as_aware(user.sessions_valid_from)
    if cutoff is not None and int(payload.get("iat", 0)) < int(cutoff.timestamp()):
        raise HTTPException(status_code=401, detail="Session ended; sign in again")

    device = db.get(Device, user.device_id) if user.device_id else None
    if user.device_id and device is None:
        raise HTTPException(status_code=401, detail="Account device is unavailable")
    # Re-check the claims against the live row so a token stops working the
    # moment a role is changed or a device is re-linked.
    if payload.get("role") != user.role:
        raise HTTPException(status_code=401, detail="Stale access token")
    if payload.get("did") != (device.id if device is not None else None):
        raise HTTPException(status_code=401, detail="Stale access token")
    return Principal(
        role=user.role,
        subject=user.email,
        user_id=user.id,
        device_db_id=device.id if device is not None else None,
        device_id=device.device_id if device is not None else None,
    )


def principal_from_api_key(api_key: str) -> Principal:
    for candidate, role in get_settings().role_key_map().items():
        if secrets.compare_digest(api_key, candidate):
            return Principal(role=role, subject=f"api-key:{role}")
    raise HTTPException(status_code=401, detail="Invalid API key")


def resolve_principal(
    credentials: HTTPAuthorizationCredentials | None = Security(bearer_header),
    api_key: str | None = Security(api_key_header),
    db: Session = Depends(get_db),
) -> Principal:
    if credentials is not None:
        if credentials.scheme.lower() != "bearer":
            raise HTTPException(status_code=401, detail="Unsupported authorization scheme")
        return principal_from_access_token(credentials.credentials, db)
    if api_key:
        return principal_from_api_key(api_key)
    raise HTTPException(status_code=401, detail="Authentication required")


def require_role(*roles: str):
    """Dependency factory. `require_role()` (no args) accepts any principal."""

    def checker(principal: Principal = Depends(resolve_principal)) -> Principal:
        if roles and principal.role not in roles:
            raise HTTPException(status_code=403, detail=f"Role '{principal.role}' not permitted")
        return principal

    return checker


def ensure_device_access(principal: Principal, device: Device) -> None:
    if principal.role == "responder":
        return
    if principal.role == "driver" and principal.device_db_id == device.id:
        return
    if principal.role == "device" and principal.device_id == device.device_id:
        return
    raise HTTPException(status_code=403, detail="This account cannot access that device")


def ensure_device_identifier(principal: Principal, device_id: str) -> None:
    if principal.role == "responder":
        return
    if principal.device_id == device_id:
        return
    raise HTTPException(status_code=403, detail="This account cannot access that device")
