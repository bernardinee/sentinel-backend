"""Device endpoints (§5.2): status, last fix, heartbeat history."""
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import Principal, ensure_device_access, require_role
from app.db import get_db
from app.models import Device, DeviceHeartbeat, as_aware, utcnow
from app.schemas import DeviceOut, HeartbeatOut

router = APIRouter(tags=["devices"])

OFFLINE_AFTER_S = 90  # 3 missed 30 s heartbeats


def _with_liveness(device: Device) -> DeviceOut:
    out = DeviceOut.model_validate(device)
    if device.last_seen_at is not None:
        age = (utcnow() - as_aware(device.last_seen_at)).total_seconds()
        out.status = "online" if age <= OFFLINE_AFTER_S else "offline"
    return out


def _get_device(db: Session, device_id: str) -> Device:
    device = db.execute(
        select(Device).where(Device.device_id == device_id)).scalar_one_or_none()
    if device is None:
        raise HTTPException(status_code=404, detail=f"Unknown device '{device_id}'")
    return device


@router.get("/devices", response_model=list[DeviceOut])
def list_devices(db: Session = Depends(get_db), _=Depends(require_role("responder"))):
    rows = db.execute(select(Device).order_by(Device.device_id)).scalars().all()
    return [_with_liveness(d) for d in rows]


@router.get("/devices/{device_id}", response_model=DeviceOut)
def get_device(
    device_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_role("driver", "responder")),
):
    device = _get_device(db, device_id)
    ensure_device_access(principal, device)
    return _with_liveness(device)


@router.get("/devices/{device_id}/heartbeats", response_model=list[HeartbeatOut])
def device_heartbeats(
    device_id: str,
    hours: int = Query(default=24, ge=1, le=24 * 7),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_role("driver", "responder")),
):
    device = _get_device(db, device_id)
    ensure_device_access(principal, device)
    since = utcnow() - timedelta(hours=hours)
    rows = db.execute(
        select(DeviceHeartbeat)
        .where(DeviceHeartbeat.device_id == device.id, DeviceHeartbeat.at >= since)
        .order_by(DeviceHeartbeat.at.asc())
    ).scalars().all()
    return [HeartbeatOut.model_validate(r) for r in rows]
