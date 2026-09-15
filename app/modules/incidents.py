"""Query endpoints (§5.2). Excludes manual_panic from nothing here — stats does
that; the feed shows everything, including panic events."""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.auth import require_role
from app.db import get_db
from app.models import Device, Incident, IncidentWindow
from app.schemas import (IncidentDetailOut, IncidentOut, IncidentPage,
                         WindowOut)

router = APIRouter(tags=["incidents"])


@router.get("/incidents", response_model=IncidentPage)
def list_incidents(
    status: str | None = None,
    severity_class: int | None = None,
    device_id: str | None = None,
    accident_confirmed: bool | None = None,
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(get_db),
    _=Depends(require_role("responder")),
):
    q = select(Incident)
    if status:
        q = q.where(Incident.status == status)
    if severity_class is not None:
        q = q.where(Incident.severity_class == severity_class)
    if device_id:
        q = q.join(Device, Device.id == Incident.device_id).where(Device.device_id == device_id)
    if accident_confirmed is not None:
        q = q.where(Incident.accident_confirmed == accident_confirmed)
    if from_:
        q = q.where(Incident.received_at >= from_)
    if to:
        q = q.where(Incident.received_at <= to)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.execute(
        q.order_by(Incident.received_at.desc())
        .offset((page - 1) * page_size).limit(page_size)
    ).scalars().all()
    return IncidentPage(
        items=[IncidentOut.model_validate(r) for r in rows],
        total=total, page=page, page_size=page_size)


@router.get("/incidents/active", response_model=list[IncidentOut])
def active_incidents(db: Session = Depends(get_db), _=Depends(require_role("responder"))):
    """Responder triage queue (§5.4): unresolved, severity first, then recency."""
    rows = db.execute(
        select(Incident)
        .where(Incident.status.notin_(["resolved", "false_alarm"]))
        .order_by(Incident.severity_class.desc().nullslast(),
                  Incident.received_at.desc())
    ).scalars().all()
    return [IncidentOut.model_validate(r) for r in rows]


@router.get("/incidents/{incident_id}", response_model=IncidentDetailOut)
def get_incident(incident_id: str, db: Session = Depends(get_db), _=Depends(require_role("responder"))):
    row = db.execute(
        select(Incident).options(joinedload(Incident.dispatch_events))
        .where(Incident.id == incident_id)
    ).unique().scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    return IncidentDetailOut.model_validate(row)


@router.get("/incidents/{incident_id}/window", response_model=WindowOut)
def get_incident_window(incident_id: str, db: Session = Depends(get_db), _=Depends(require_role("responder"))):
    w = db.get(IncidentWindow, incident_id)
    if w is None:
        raise HTTPException(status_code=404, detail="Window not found")
    return WindowOut(incident_id=incident_id, fs_hz=w.fs_hz,
                     ax=w.ax, ay=w.ay, az=w.az, gx=w.gx, gy=w.gy, gz=w.gz)
