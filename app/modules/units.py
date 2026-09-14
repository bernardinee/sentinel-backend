"""Emergency service units and dispatch routing.

The dispatcher's core question is not "which unit is nearest" but "which unit
gets there soonest". Those differ in Accra often enough to matter, so
recommendations are ranked by road travel time, not straight-line distance.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_role
from app.db import get_db
from app.models import DispatchEvent, Incident, ResponseUnit, utcnow
from app.modules.routing import haversine_km, road_route, route_many
from app.modules.ws import manager
from app.schemas import (AssignUnitIn, DispatchOption, DispatchOptions,
                         IncidentOut, RouteOut, UnitIn, UnitOut, UnitPatch,
                         UnitStatusIn)

log = logging.getLogger(__name__)
router = APIRouter(tags=["units"])

# Which services a dispatcher should send, by classified severity. Severe
# implies entrapment/fire risk and traffic control; Moderate is medical plus
# scene control. Advisory only — the dispatcher can send anything.
REQUIRED_BY_SEVERITY: dict[int, list[str]] = {
    2: ["AMBULANCE", "FIRE", "POLICE"],
    1: ["AMBULANCE", "POLICE"],
    0: [],
}

# Coarse-filter this many nearest candidates per type before road-routing, so a
# big roster does not fan out into dozens of calls to the public OSRM demo.
ROUTE_CANDIDATES_PER_TYPE = 3

BUSY_STATUSES = {"dispatched", "en_route", "on_scene"}


def _get_unit(db: Session, call_sign: str) -> ResponseUnit:
    unit = db.execute(
        select(ResponseUnit).where(ResponseUnit.call_sign == call_sign)
    ).scalar_one_or_none()
    if unit is None:
        raise HTTPException(status_code=404, detail=f"Unknown unit '{call_sign}'")
    return unit


def _get_incident(db: Session, incident_id: str) -> Incident:
    incident = db.get(Incident, incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    return incident


# ── Roster CRUD (operator configuration) ─────────────────────────────────────

@router.get("/units", response_model=list[UnitOut])
def list_units(db: Session = Depends(get_db), _=Depends(require_role())):
    rows = db.execute(
        select(ResponseUnit).order_by(ResponseUnit.unit_type, ResponseUnit.call_sign)
    ).scalars().all()
    return [UnitOut.model_validate(u) for u in rows]


@router.post("/units", response_model=UnitOut, status_code=201)
def register_unit(body: UnitIn, db: Session = Depends(get_db),
                  _=Depends(require_role("responder"))):
    existing = db.execute(
        select(ResponseUnit).where(ResponseUnit.call_sign == body.call_sign)
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"{body.call_sign} already registered")
    unit = ResponseUnit(
        call_sign=body.call_sign, unit_type=body.unit_type,
        station_name=body.station_name,
        home_lat=body.home_lat, home_lon=body.home_lon,
        crew_size=body.crew_size, contact_phone=body.contact_phone,
        status="available", active=True,
    )
    db.add(unit)
    db.commit()
    return UnitOut.model_validate(unit)


@router.patch("/units/{call_sign}", response_model=UnitOut)
def update_unit(call_sign: str, body: UnitPatch, db: Session = Depends(get_db),
                _=Depends(require_role("responder"))):
    unit = _get_unit(db, call_sign)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(unit, field, value)
    unit.last_update = utcnow()
    db.commit()
    return UnitOut.model_validate(unit)


@router.delete("/units/{call_sign}", status_code=204)
def delete_unit(call_sign: str, db: Session = Depends(get_db),
                _=Depends(require_role("responder"))):
    unit = _get_unit(db, call_sign)
    if unit.status in BUSY_STATUSES:
        raise HTTPException(status_code=409,
                            detail=f"{call_sign} is {unit.status}; clear it first")
    db.delete(unit)
    db.commit()


# ── Dispatch recommendations ─────────────────────────────────────────────────

@router.get("/incidents/{incident_id}/dispatch-options",
            response_model=DispatchOptions)
async def dispatch_options(incident_id: str, db: Session = Depends(get_db),
                           _=Depends(require_role())):
    """Available units ranked by ROAD travel time to the scene.

    Two-stage: haversine to shortlist, then real routing on the shortlist. The
    nearest unit in a straight line is not always the fastest by road, which is
    the whole reason this endpoint exists.
    """
    incident = _get_incident(db, incident_id)
    if incident.lat is None or incident.lon is None:
        raise HTTPException(
            status_code=409,
            detail="Incident has no GPS fix, so units cannot be routed to it")

    required = REQUIRED_BY_SEVERITY.get(incident.severity_class or 0, [])
    units = db.execute(
        select(ResponseUnit).where(
            ResponseUnit.active.is_(True),
            ResponseUnit.status == "available")
    ).scalars().all()

    if not units:
        return DispatchOptions(
            incident_id=incident.id, incident_lat=incident.lat,
            incident_lon=incident.lon, required_types=required, options=[],
            routing_source="none",
            note="No available units on the roster. Register units under Fleet.")

    # stage 1 — shortlist the nearest few of each type
    by_type: dict[str, list[ResponseUnit]] = {}
    for u in units:
        by_type.setdefault(u.unit_type, []).append(u)
    shortlist: list[ResponseUnit] = []
    for type_units in by_type.values():
        type_units.sort(key=lambda u: haversine_km(*u.position(),
                                                   incident.lat, incident.lon))
        shortlist.extend(type_units[:ROUTE_CANDIDATES_PER_TYPE])

    # stage 2 — real road routes for the shortlist only
    routes = await route_many(
        [(u.call_sign, *u.position()) for u in shortlist],
        incident.lat, incident.lon, with_geometry=True)

    options: list[DispatchOption] = []
    for u in shortlist:
        r = routes[u.call_sign]
        options.append(DispatchOption(
            unit=UnitOut.model_validate(u),
            route=RouteOut(**r),
            eta_min=r["duration_min"],
            recommended=False,
        ))
    options.sort(key=lambda o: o.eta_min)

    # mark the fastest unit of each required type as the recommended pick
    for service in required:
        for o in options:
            if o.unit.unit_type == service:
                o.recommended = True
                break

    sources = {o.route.source for o in options}
    routing_source = "osrm" if sources == {"osrm"} else (
        "mixed" if "osrm" in sources else "straight_line")

    note = None
    if routing_source != "osrm":
        note = ("Road routing was unavailable for some units; those ETAs are "
                "straight-line estimates with an urban detour factor.")
    elif not required:
        note = ("This incident is not a confirmed crash, so no service is "
                "required. Units are listed for discretionary dispatch.")

    return DispatchOptions(
        incident_id=incident.id, incident_lat=incident.lat,
        incident_lon=incident.lon, required_types=required,
        options=options, routing_source=routing_source, note=note)


# ── Assignment ───────────────────────────────────────────────────────────────

@router.post("/incidents/{incident_id}/assign-unit", response_model=IncidentOut)
async def assign_unit(incident_id: str, body: AssignUnitIn,
                      db: Session = Depends(get_db),
                      _=Depends(require_role("responder"))):
    incident = _get_incident(db, incident_id)
    unit = _get_unit(db, body.call_sign)

    if incident.status in ("resolved", "false_alarm"):
        raise HTTPException(status_code=409,
                            detail=f"Incident is {incident.status}")
    if unit.status in BUSY_STATUSES and unit.assigned_incident_id != incident.id:
        raise HTTPException(
            status_code=409,
            detail=f"{unit.call_sign} is already {unit.status} on another incident")
    if not unit.active:
        raise HTTPException(status_code=409, detail=f"{unit.call_sign} is out of service")

    eta_note = ""
    if incident.lat is not None and incident.lon is not None:
        r = await road_route(*unit.position(), incident.lat, incident.lon,
                             with_geometry=False)
        eta_note = (f" ETA {r['duration_min']:.0f} min "
                    f"({r['distance_km']:.1f} km by road)"
                    if r["source"] == "osrm" else
                    f" ETA ~{r['duration_min']:.0f} min (estimated)")

    unit.status = "dispatched"
    unit.assigned_incident_id = incident.id
    unit.last_update = utcnow()

    if incident.status == "new":
        incident.acknowledged_at = utcnow()
        incident.acknowledged_by = body.actor
    incident.status = "dispatched"

    note = (body.note or "") + f"Assigned {unit.call_sign} ({unit.unit_type}).{eta_note}"
    db.add(DispatchEvent(incident_id=incident.id, actor=body.actor,
                         action="assign", note=note.strip()))
    db.commit()

    out = IncidentOut.model_validate(incident)
    await manager.broadcast("incident.updated", out.model_dump())
    await manager.broadcast("unit.updated", UnitOut.model_validate(unit).model_dump())
    return out


@router.post("/units/{call_sign}/status", response_model=UnitOut)
async def set_unit_status(call_sign: str, body: UnitStatusIn,
                          db: Session = Depends(get_db),
                          _=Depends(require_role("responder"))):
    """Progress a unit through the response, mirroring it onto the incident
    timeline so the dispatcher sees one story, not two."""
    unit = _get_unit(db, call_sign)
    incident_id = unit.assigned_incident_id

    unit.status = body.status
    unit.last_update = utcnow()

    if body.status in ("available", "out_of_service"):
        unit.assigned_incident_id = None

    if incident_id and body.status in ("en_route", "on_scene"):
        incident = db.get(Incident, incident_id)
        if incident is not None:
            db.add(DispatchEvent(
                incident_id=incident.id, actor=body.actor, action=body.status,
                note=(body.note or f"{unit.call_sign} {body.status.replace('_', ' ')}")))
            if incident.status in ("new", "acknowledged"):
                incident.status = "dispatched"
    db.commit()

    out = UnitOut.model_validate(unit)
    await manager.broadcast("unit.updated", out.model_dump())
    if incident_id:
        incident = db.get(Incident, incident_id)
        if incident is not None:
            await manager.broadcast(
                "incident.updated", IncidentOut.model_validate(incident).model_dump())
    return out
