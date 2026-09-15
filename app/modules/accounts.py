"""Driver account registration, login, refresh rotation, logout, and profile."""
import hashlib
import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Response
from pwdlib import PasswordHash
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import Principal, create_access_token, require_role
from app.config import get_settings
from app.db import get_db
from app.models import Device, RefreshToken, User, as_aware, utcnow
from app.schemas import (ChangePasswordIn, LoginIn, RefreshIn, RegisterIn,
                         ResponderCreateIn, ResponderOut, ResponderPatch,
                         TokenOut, UserOut)

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


@router.post("/change-password", response_model=TokenOut)
def change_password(
    body: ChangePasswordIn,
    principal: Principal = Depends(require_role("driver", "responder")),
    db: Session = Depends(get_db),
):
    """Change your own password and return a fresh token pair.

    Every existing refresh token is revoked, so a password change signs out
    every other device — which is the whole point if the old password was
    compromised. A new pair is issued for the caller so the session they are
    sitting in survives.
    """
    if principal.user_id is None:
        raise HTTPException(status_code=401, detail="Not an account session")
    user = db.get(User, principal.user_id)
    if user is None or not user.active:
        raise HTTPException(status_code=401, detail="Account is unavailable")

    if not password_hash.verify(body.current_password, user.password_hash):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    if password_hash.verify(body.new_password, user.password_hash):
        raise HTTPException(status_code=400,
                            detail="New password must differ from the current one")

    now = utcnow()
    user.password_hash = password_hash.hash(body.new_password)
    user.updated_at = now
    # Invalidate access tokens already in circulation, not just refresh tokens.
    user.sessions_valid_from = now
    db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )
    db.flush()

    device = db.get(Device, user.device_id) if user.device_id else None
    return _token_out(db, user, device)


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


# ── Responder team management ────────────────────────────────────────────────
#
# A dispatch team adds its own operators, so these are restricted to signed-in
# responders. Note what is NOT possible: /auth/register mints drivers only, so
# no anonymous caller can ever reach responder rights — the privilege boundary
# stays at "you must already be a responder".


@router.get("/responders", response_model=list[ResponderOut])
def list_responders(
    _: Principal = Depends(require_role("responder")),
    db: Session = Depends(get_db),
):
    rows = db.execute(
        select(User).where(User.role == "responder").order_by(User.created_at)
    ).scalars().all()
    return [ResponderOut.model_validate(u) for u in rows]


@router.post("/responders", response_model=ResponderOut, status_code=201)
def create_responder(
    body: ResponderCreateIn,
    _: Principal = Depends(require_role("responder")),
    db: Session = Depends(get_db),
):
    if db.scalar(select(User.id).where(User.email == body.email)) is not None:
        raise HTTPException(status_code=409, detail="An account already uses that email")
    user = User(
        name=body.name,
        email=body.email,
        phone=body.phone.strip(),
        password_hash=password_hash.hash(body.password),
        role="responder",
        device_id=None,          # responders are not bound to a device
        active=True,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="That account already exists") from exc
    return ResponderOut.model_validate(user)


@router.patch("/responders/{user_id}", response_model=ResponderOut)
def update_responder(
    user_id: str,
    body: ResponderPatch,
    principal: Principal = Depends(require_role("responder")),
    db: Session = Depends(get_db),
):
    user = db.get(User, user_id)
    if user is None or user.role != "responder":
        raise HTTPException(status_code=404, detail="Responder not found")

    # Deactivating yourself would lock you out mid-shift, and if you are the
    # only active operator it would lock everyone out permanently.
    if body.active is False:
        if user.id == principal.user_id:
            raise HTTPException(status_code=409, detail="You cannot deactivate your own account")
        remaining = db.scalar(
            select(func.count()).select_from(User)
            .where(User.role == "responder", User.active.is_(True), User.id != user.id)
        )
        if not remaining:
            raise HTTPException(
                status_code=409,
                detail="This is the last active responder; the console would be unreachable")

    if body.name is not None:
        user.name = body.name.strip() or user.name
    if body.password is not None:
        user.password_hash = password_hash.hash(body.password)
        # Force a fresh sign-in everywhere after a password change.
        db.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=utcnow())
        )
    if body.active is not None:
        user.active = body.active
        if body.active is False:
            db.execute(
                update(RefreshToken)
                .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
                .values(revoked_at=utcnow())
            )

    user.updated_at = utcnow()
    db.commit()
    return ResponderOut.model_validate(user)
