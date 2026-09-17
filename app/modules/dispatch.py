"""Incident actions (§5.3). Every action writes a dispatch_events row and
broadcasts; the mission timeline is derived from that table, never from
incident columns alone."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from sqlalchemy import select

from app.auth import require_role
from app.db import get_db
from app.models import DispatchEvent, Incident, ResponseUnit, utcnow
from app.modules.movement import clear_dispatch_run
from app.modules.ws import manager
from app.schemas import AcknowledgeIn, DispatchIn, IncidentOut, ResolveIn, UnitOut

router = APIRouter(tags=["dispatch"])

# state machine: which actions are legal from which status
_ALLOWED = {
    "acknowledge": {"new"},
    "assign": {"acknowledged", "dispatched"},
    "en_route": {"acknowledged", "dispatched"},
    "on_scene": {"dispatched"},
    "resolve": {"new", "acknowledged", "dispatched"},
    "false_alarm": {"new", "acknowledged", "dispatched"},
}
# status after each action
_NEXT_STATUS = {
    "acknowledge": "acknowledged",
    "assign": "dispatched",
    "en_route": "dispatched",
    "on_scene": "dispatched",
    "resolve": "resolved",
    "false_alarm": "false_alarm",
}


def _get(db: Session, incident_id: str) -> Incident:
    incident = db.get(Incident, incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    return incident


async def _apply(db: Session, incident: Incident, action: str, actor: str,
                 note: str | None) -> IncidentOut:
    if incident.status not in _ALLOWED[action]:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot {action} an incident in status '{incident.status}'")
    incident.status = _NEXT_STATUS[action]
    now = utcnow()
    if action == "acknowledge":
        incident.acknowledged_at = now
        incident.acknowledged_by = actor
    if action in ("resolve", "false_alarm"):
        incident.resolved_at = now
    if note:
        incident.notes = (incident.notes + "\n" if incident.notes else "") + note
    db.add(DispatchEvent(incident_id=incident.id, actor=actor, action=action, note=note))

    # Closing the incident releases its responders back to their stations —
    # whether it was resolved or judged a false alarm, and regardless of how far
    # along their run they were. This is what makes "on scene -> available"
    # happen automatically once the dispatcher closes the call.
    freed: list[ResponseUnit] = []
    if action in ("resolve", "false_alarm"):
        freed = db.execute(
            select(ResponseUnit).where(ResponseUnit.assigned_incident_id == incident.id)
        ).scalars().all()
        for unit in freed:
            clear_dispatch_run(unit)

    db.commit()
    out = IncidentOut.model_validate(incident)
    await manager.broadcast("incident.updated", out.model_dump())
    for unit in freed:
        await manager.broadcast("unit.updated", UnitOut.model_validate(unit).model_dump())
    return out


@router.post("/incidents/{incident_id}/acknowledge", response_model=IncidentOut)
async def acknowledge(incident_id: str, body: AcknowledgeIn,
                      db: Session = Depends(get_db),
                      _=Depends(require_role("responder"))):
    return await _apply(db, _get(db, incident_id), "acknowledge", body.actor, body.note)


@router.post("/incidents/{incident_id}/dispatch", response_model=IncidentOut)
async def dispatch(incident_id: str, body: DispatchIn,
                   db: Session = Depends(get_db),
                   _=Depends(require_role("responder"))):
    return await _apply(db, _get(db, incident_id), body.action, body.actor, body.note)


@router.post("/incidents/{incident_id}/resolve", response_model=IncidentOut)
async def resolve(incident_id: str, body: ResolveIn,
                  db: Session = Depends(get_db),
                  _=Depends(require_role("responder"))):
    action = "resolve" if body.outcome == "resolved" else "false_alarm"
    return await _apply(db, _get(db, incident_id), action, body.actor, body.note)
