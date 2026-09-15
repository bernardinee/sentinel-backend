"""Sentinel app hooks (§5.4) — endpoints built now, mobile app consumes later.

manual_panic incidents bypass the model entirely: a human pressed a button.
label_source="manual_panic" marks them so stats can exclude them.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import Principal, ensure_device_access, ensure_device_identifier, require_role
from app.db import get_db
from app.models import (Device, EmergencyContact, Incident, as_aware, utcnow)
from app.modules.devices import OFFLINE_AFTER_S, _get_device
from app.modules.ws import manager
from app.schemas import (ContactIn, ContactOut, ContactPatch, IncidentOut,
                         PanicIn, ProtectionStatus)

router = APIRouter(tags=["sentinel"])


# ── Driver: protection status ────────────────────────────────────────────────

@router.get("/me/protection-status", response_model=ProtectionStatus)
def protection_status(device_id: str, db: Session = Depends(get_db),
                      principal: Principal = Depends(require_role("driver", "responder"))):
    ensure_device_identifier(principal, device_id)
    device = db.execute(
        select(Device).where(Device.device_id == device_id)).scalar_one_or_none()
    if device is None:
        return ProtectionStatus(
            device_id=device_id, registered=False, online=False,
            monitoring_active=False, gps_locked=False, satellites=None,
            last_heartbeat_at=None, last_heartbeat_age_s=None, open_incidents=0)
    age = None
    if device.last_seen_at is not None:
        age = (utcnow() - as_aware(device.last_seen_at)).total_seconds()
    online = age is not None and age <= OFFLINE_AFTER_S
    open_incidents = len(db.execute(
        select(Incident.id).where(
            Incident.device_id == device.id,
            Incident.status.notin_(["resolved", "false_alarm"]))
    ).all())
    return ProtectionStatus(
        device_id=device_id, registered=True, online=online,
        monitoring_active=online,
        gps_locked=bool(device.last_lat is not None and (device.last_satellites or 0) >= 3),
        satellites=device.last_satellites,
        last_heartbeat_at=device.last_seen_at,
        last_heartbeat_age_s=round(age, 1) if age is not None else None,
        open_incidents=open_incidents)


# ── Driver: panic button ─────────────────────────────────────────────────────

@router.post("/panic", response_model=IncidentOut, status_code=201)
async def panic(body: PanicIn, db: Session = Depends(get_db),
                principal: Principal = Depends(require_role("driver", "responder"))):
    ensure_device_identifier(principal, body.device_id)
    device = db.execute(
        select(Device).where(Device.device_id == body.device_id)).scalar_one_or_none()
    if device is None:
        device = Device(device_id=body.device_id, status="unknown")
        db.add(device)
        db.flush()
    incident = Incident(
        event_id=f"panic-{uuid.uuid4()}",
        device_id=device.id,
        detected_at=utcnow(), received_at=utcnow(),
        severity_class=2, severity_name="Severe",
        accident_confirmed=True, label_source="manual_panic",
        classification_pending=False,
        lat=body.lat if body.lat is not None else device.last_lat,
        lon=body.lon if body.lon is not None else device.last_lon,
        gps_valid=body.lat is not None or device.last_lat is not None,
        satellites=device.last_satellites,
        status="new",
        notes=body.note,
    )
    db.add(incident)
    db.commit()
    out = IncidentOut.model_validate(incident)
    await manager.broadcast("incident.created", out.model_dump())
    return out


# ── Driver: own incident history ─────────────────────────────────────────────

@router.get("/me/incidents", response_model=list[IncidentOut])
def my_incidents(device_id: str, db: Session = Depends(get_db),
                 principal: Principal = Depends(require_role("driver", "responder"))):
    device = _get_device(db, device_id)
    ensure_device_access(principal, device)
    rows = db.execute(
        select(Incident).where(Incident.device_id == device.id)
        .order_by(Incident.received_at.desc()).limit(200)
    ).scalars().all()
    return [IncidentOut.model_validate(r) for r in rows]


# ── Emergency contact CRUD (shared with dashboard Screen 4) ──────────────────

@router.get("/devices/{device_id}/contacts", response_model=list[ContactOut])
def list_contacts(device_id: str, db: Session = Depends(get_db),
                  principal: Principal = Depends(require_role("driver", "responder"))):
    device = _get_device(db, device_id)
    ensure_device_access(principal, device)
    rows = db.execute(
        select(EmergencyContact).where(EmergencyContact.device_id == device.id)
        .order_by(EmergencyContact.priority)
    ).scalars().all()
    return [ContactOut.model_validate(r) for r in rows]


@router.post("/devices/{device_id}/contacts", response_model=ContactOut, status_code=201)
def add_contact(device_id: str, body: ContactIn, db: Session = Depends(get_db),
                principal: Principal = Depends(require_role("driver", "responder"))):
    device = _get_device(db, device_id)
    ensure_device_access(principal, device)
    contact = EmergencyContact(
        device_id=device.id, name=body.name, phone=body.phone,
        relationship_=body.relationship, priority=body.priority, active=body.active)
    db.add(contact)
    db.commit()
    return ContactOut.model_validate(contact)


@router.patch("/devices/{device_id}/contacts/{contact_id}", response_model=ContactOut)
def update_contact(device_id: str, contact_id: str, body: ContactPatch,
                   db: Session = Depends(get_db),
                   principal: Principal = Depends(require_role("driver", "responder"))):
    device = _get_device(db, device_id)
    ensure_device_access(principal, device)
    contact = db.get(EmergencyContact, contact_id)
    if contact is None or contact.device_id != device.id:
        raise HTTPException(status_code=404, detail="Contact not found")
    if body.name is not None:
        contact.name = body.name
    if body.phone is not None:
        contact.phone = body.phone
    if body.relationship is not None:
        contact.relationship_ = body.relationship
    if body.priority is not None:
        contact.priority = body.priority
    if body.active is not None:
        contact.active = body.active
    db.commit()
    return ContactOut.model_validate(contact)


@router.delete("/devices/{device_id}/contacts/{contact_id}", status_code=204)
def delete_contact(device_id: str, contact_id: str,
                   db: Session = Depends(get_db),
                   principal: Principal = Depends(require_role("driver", "responder"))):
    device = _get_device(db, device_id)
    ensure_device_access(principal, device)
    contact = db.get(EmergencyContact, contact_id)
    if contact is None or contact.device_id != device.id:
        raise HTTPException(status_code=404, detail="Contact not found")
    db.delete(contact)
    db.commit()
