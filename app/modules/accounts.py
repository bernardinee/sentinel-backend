"""Driver account registration, login, refresh rotation, logout, and profile."""
import hashlib
import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Response
from pwdlib import PasswordHash
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import Principal, create_access_token, require_role
from app.config import get_settings
from app.db import get_db
from app.models import Device, RefreshToken, User, as_aware, utcnow
from app.schemas import LoginIn, RefreshIn, RegisterIn, TokenOut, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])
password_hash = PasswordHash.recommended()
_dummy_hash = password_hash.hash("sentinel-dummy-password")


def _digest(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _user_out(user: User, device: Device | None) -> UserOut:
    return UserOut(
        id=user.id,
        name=user.name,
        email=user.email,
        phone=user.phone,
        role=user.role,
        device_id=device.device_id if device is not None else None,
    )


def _new_refresh_token(user: User) -> tuple[str, RefreshToken]:
    raw = secrets.token_urlsafe(48)
    return raw, RefreshToken(
        user_id=user.id,
        token_hash=_digest(raw),
        expires_at=utcnow() + timedelta(days=get_settings().REFRESH_TOKEN_DAYS),
    )


def _token_out(db: Session, user: User, device: Device | None) -> TokenOut:
    raw_refresh, stored_refresh = _new_refresh_token(user)
    db.add(stored_refresh)
    db.commit()
    access, expires_in = create_access_token(user, device)
    return TokenOut(
        access_token=access,
        refresh_token=raw_refresh,
        expires_in=expires_in,
        user=_user_out(user, device),
    )


@router.post("/register", response_model=TokenOut, status_code=201)
def register(body: RegisterIn, db: Session = Depends(get_db)):
    email = body.email.strip().lower()
    if db.scalar(select(User.id).where(User.email == email)) is not None:
        raise HTTPException(status_code=409, detail="An account already uses that email")

    device = db.scalar(select(Device).where(Device.device_id == body.device_id))
    if device is None:
        device = Device(device_id=body.device_id, status="unknown")
        db.add(device)
        db.flush()
    if db.scalar(select(User.id).where(User.device_id == device.id)) is not None:
        raise HTTPException(status_code=409, detail="That Sentinel device is already linked")

    user = User(
        name=body.name.strip(),
        email=email,
        phone=body.phone.strip(),
        password_hash=password_hash.hash(body.password),
        role="driver",
        device_id=device.id,
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Account or device already exists") from exc
    return _token_out(db, user, device)


@router.post("/login", response_model=TokenOut)
def login(body: LoginIn, db: Session = Depends(get_db)):
    email = body.email.strip().lower()
    user = db.scalar(select(User).where(User.email == email))
    candidate_hash = user.password_hash if user is not None else _dummy_hash
    valid = password_hash.verify(body.password, candidate_hash)
    if user is None or not valid or not user.active:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    device = db.get(Device, user.device_id) if user.device_id else None
    if user.device_id and device is None:
        raise HTTPException(status_code=409, detail="Account device is unavailable")
    return _token_out(db, user, device)


@router.post("/refresh", response_model=TokenOut)
def refresh(body: RefreshIn, db: Session = Depends(get_db)):
    digest = _digest(body.refresh_token)
    stored = db.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == digest).with_for_update()
    )
    if stored is None:
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    now = utcnow()
    if stored.revoked_at is not None:
        db.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == stored.user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        db.commit()
        raise HTTPException(status_code=401, detail="Refresh token reuse detected")
    if as_aware(stored.expires_at) <= now:
        stored.revoked_at = now
        db.commit()
        raise HTTPException(status_code=401, detail="Refresh token expired")

    user = db.get(User, stored.user_id)
    if user is None or not user.active:
        raise HTTPException(status_code=401, detail="Account is unavailable")
    device = db.get(Device, user.device_id) if user.device_id else None
    if user.device_id and device is None:
        raise HTTPException(status_code=409, detail="Account device is unavailable")

    raw_refresh, replacement = _new_refresh_token(user)
    stored.revoked_at = now
    stored.replaced_by_hash = replacement.token_hash
    db.add(replacement)
    db.commit()
    access, expires_in = create_access_token(user, device)
    return TokenOut(
        access_token=access,
        refresh_token=raw_refresh,
        expires_in=expires_in,
        user=_user_out(user, device),
    )


@router.post("/logout", status_code=204)
def logout(body: RefreshIn, db: Session = Depends(get_db)):
    stored = db.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == _digest(body.refresh_token))
    )
    if stored is not None and stored.revoked_at is None:
        stored.revoked_at = utcnow()
        db.commit()
    return Response(status_code=204)


@router.get("/me", response_model=UserOut)
def me(
    principal: Principal = Depends(require_role("driver", "responder")),
    db: Session = Depends(get_db),
):
    """Profile for whoever is signed in. Serves both roles, so the dashboard
    can restore a session on reload the same way the mobile app does."""
    if principal.user_id is None:
        raise HTTPException(status_code=401, detail="Not an account session")
    user = db.get(User, principal.user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="Account is unavailable")
    device = db.get(Device, principal.device_db_id) if principal.device_db_id else None
    return _user_out(user, device)
