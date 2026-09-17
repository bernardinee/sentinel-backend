"""Responder movement + automatic status progression.

This system does not (yet) receive real GPS from responder vehicles, so a
dispatched unit's status between dispatch and arrival is PROJECTED from two real
quantities — the road route returned by OSRM and the real dispatch time — never
randomised or seeded. When responder vehicles carry their own trackers, this
projection is simply replaced by their reported positions and statuses.

The projection drives two things:
  • the unit's status: dispatched -> en_route -> on_scene, advanced here;
  • the on-map pin, which the dashboard animates along the same stored route,
    from `dispatched_at`, using `route_eta_s` — see lib/units.ts.

Both sides share the timing below (MOBILIZE_S and the sim speed), which the
dashboard reads from GET /dispatch/config so the two never drift.
"""
import logging
import os

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DispatchEvent, Incident, ResponseUnit, as_aware, utcnow
from app.modules.ws import manager
from app.schemas import IncidentOut, UnitOut

log = logging.getLogger(__name__)

# Seconds a crew takes to roll after being dispatched, before it is "en route".
MOBILIZE_S = 8.0

# Only these statuses are auto-advanced; a manual jump ahead (or a freed unit)
# is left alone.
ADVANCING = ("dispatched", "en_route")
_RANK = {"dispatched": 0, "en_route": 1, "on_scene": 2}


def sim_speed() -> float:
    """Demonstration time-compression. 1.0 = real time (the honest default);
    a higher value plays the response back faster for a live demo without
    touching the real ETAs shown to the dispatcher. Never below 1.0."""
    try:
        return max(1.0, float(os.environ.get("DISPATCH_SIM_SPEED", "1.0")))
    except ValueError:
        return 1.0


def response_phase(unit: ResponseUnit, now=None) -> tuple[str, float]:
    """Projected (status, fraction_along_route) for `unit`.

    fraction is 0 while mobilising, ramps 0->1 while en route, and is 1 on
    scene. A unit with no route to travel keeps its own status at fraction 0.
    """
    if unit.route_eta_s is None or unit.dispatched_at is None:
        return unit.status, 0.0
    now = now or utcnow()
    elapsed = (now - as_aware(unit.dispatched_at)).total_seconds()
    travel = max(1.0, unit.route_eta_s / sim_speed())
    if elapsed < MOBILIZE_S:
        return "dispatched", 0.0
    if elapsed < MOBILIZE_S + travel:
        return "en_route", (elapsed - MOBILIZE_S) / travel
    return "on_scene", 1.0


async def advance_units_once(db: Session) -> int:
    """Advance every responding unit whose projected status is past its stored
    one. Position is deliberately NOT written here — the unit's routing origin
    stays its station so its drawn route is stable, and the pin's movement lives
    entirely in the dashboard's interpolation. Returns the count that changed.
    """
    units = db.execute(
        select(ResponseUnit).where(
            ResponseUnit.assigned_incident_id.is_not(None),
            ResponseUnit.status.in_(ADVANCING),
            ResponseUnit.route_eta_s.is_not(None),
        )
    ).scalars().all()

    now = utcnow()
    advanced: list[ResponseUnit] = []
    touched_incidents: set[str] = set()
    for u in units:
        phase, _ = response_phase(u, now)
        if _RANK[phase] > _RANK.get(u.status, 0):
            u.status = phase
            u.last_update = now
            db.add(DispatchEvent(
                incident_id=u.assigned_incident_id, actor="auto", action=phase,
                note=f"{u.call_sign} {phase.replace('_', ' ')} (auto)"))
            advanced.append(u)
            if u.assigned_incident_id:
                touched_incidents.add(u.assigned_incident_id)

    if not advanced:
        return 0

    db.commit()
    for u in advanced:
        await manager.broadcast("unit.updated", UnitOut.model_validate(u).model_dump())
    for iid in touched_incidents:
        inc = db.get(Incident, iid)
        if inc is not None:
            await manager.broadcast(
                "incident.updated", IncidentOut.model_validate(inc).model_dump())
    return len(advanced)


def clear_dispatch_run(unit: ResponseUnit) -> None:
    """Reset a unit to idle: drop its route, dispatch clock and live position so
    it returns to its station on the map."""
    unit.status = "available"
    unit.assigned_incident_id = None
    unit.dispatched_at = None
    unit.route_geometry = None
    unit.route_eta_s = None
    unit.current_lat = None
    unit.current_lon = None
    unit.last_update = utcnow()
