"""Responder movement + automatic status progression.

Status advances on the response clock (dispatched -> en_route -> on_scene) with
no dispatcher clicks, and closing the incident releases the responders. Time is
driven by back-dating `dispatched_at`, never by sleeping.
"""
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest

from app.db import SessionLocal
from app.models import ResponseUnit, utcnow
from app.modules.movement import (MOBILIZE_S, advance_units_once,
                                  response_phase)
from tests.conftest import HEADERS, make_event
from tests.test_ingest import ML_OK

ROUTE = [[-0.187, 5.60], [-0.180, 5.62], [-0.170, 5.64]]


async def fake_route(from_lat, from_lon, to_lat, to_lon, with_geometry=True):
    return {"distance_km": 5.0, "duration_min": 10.0,  # 600 s trip
            "geometry": ROUTE, "source": "osrm"}


def _register(client, call_sign="AMB-01"):
    return client.post("/api/v1/units", headers=HEADERS, json={
        "call_sign": call_sign, "unit_type": "AMBULANCE",
        "station_name": "S", "home_lat": 5.65, "home_lon": -0.187, "crew_size": 3})


def _incident(client) -> str:
    with patch("app.modules.ingest.classify", new=AsyncMock(return_value=ML_OK)):
        client.post("/api/v1/events", json=make_event(), headers=HEADERS)
    return client.get("/api/v1/incidents", headers=HEADERS).json()["items"][0]["id"]


def _assign(client, iid, call_sign="AMB-01"):
    with patch("app.modules.units.road_route", new=fake_route):
        return client.post(f"/api/v1/incidents/{iid}/assign-unit", headers=HEADERS,
                           json={"call_sign": call_sign, "actor": "d"})


def _backdate(call_sign: str, seconds: float) -> None:
    with SessionLocal() as db:
        u = db.execute(
            ResponseUnit.__table__.select().where(
                ResponseUnit.call_sign == call_sign)).first()
        unit = db.get(ResponseUnit, u.id)
        unit.dispatched_at = utcnow() - timedelta(seconds=seconds)
        db.commit()


def test_response_phase_projects_status():
    def unit(elapsed):
        return ResponseUnit(
            call_sign="X", unit_type="AMBULANCE", station_name="S",
            home_lat=5.65, home_lon=-0.187, status="dispatched",
            route_geometry=ROUTE, route_eta_s=600.0,
            dispatched_at=utcnow() - timedelta(seconds=elapsed))

    assert response_phase(unit(2))[0] == "dispatched"            # still mobilising
    status, frac = response_phase(unit(MOBILIZE_S + 300))        # halfway
    assert status == "en_route" and 0.4 < frac < 0.6
    assert response_phase(unit(MOBILIZE_S + 700))[0] == "on_scene"

    idle = ResponseUnit(call_sign="Y", unit_type="FIRE", station_name="S",
                        home_lat=5.6, home_lon=-0.1, status="available")
    assert response_phase(idle) == ("available", 0.0)


def test_assign_stores_route_and_clock(client):
    _register(client)
    iid = _incident(client)
    _assign(client, iid)
    unit = client.get("/api/v1/units", headers=HEADERS).json()[0]
    assert unit["status"] == "dispatched"
    assert unit["route_eta_s"] == 600.0
    assert len(unit["route_geometry"]) == len(ROUTE)
    assert unit["dispatched_at"] is not None


@pytest.mark.asyncio
async def test_units_auto_advance_without_clicks(client):
    _register(client)
    iid = _incident(client)
    _assign(client, iid)

    # a moment in: en route
    _backdate("AMB-01", MOBILIZE_S + 300)
    with SessionLocal() as db:
        await advance_units_once(db)
    assert client.get("/api/v1/units", headers=HEADERS).json()[0]["status"] == "en_route"

    # past the ETA: on scene, again with no dispatcher action
    _backdate("AMB-01", MOBILIZE_S + 700)
    with SessionLocal() as db:
        await advance_units_once(db)
    assert client.get("/api/v1/units", headers=HEADERS).json()[0]["status"] == "on_scene"

    detail = client.get(f"/api/v1/incidents/{iid}", headers=HEADERS).json()
    auto = [e for e in detail["dispatch_events"] if e["actor"] == "auto"]
    assert {e["action"] for e in auto} == {"en_route", "on_scene"}


@pytest.mark.asyncio
async def test_auto_advance_never_regresses_a_manual_jump(client):
    _register(client)
    iid = _incident(client)
    _assign(client, iid)
    # dispatcher jumps straight to on_scene
    client.post("/api/v1/units/AMB-01/status", headers=HEADERS,
                json={"status": "on_scene", "actor": "d"})
    # the clock still says "en route", but the mover must not pull it back
    _backdate("AMB-01", MOBILIZE_S + 300)
    with SessionLocal() as db:
        await advance_units_once(db)
    assert client.get("/api/v1/units", headers=HEADERS).json()[0]["status"] == "on_scene"


def test_resolving_incident_frees_its_units(client):
    _register(client, "AMB-01")
    _register(client, "POL-01")
    iid = _incident(client)
    _assign(client, iid, "AMB-01")
    _assign(client, iid, "POL-01")

    r = client.post(f"/api/v1/incidents/{iid}/resolve", headers=HEADERS,
                    json={"actor": "d", "outcome": "resolved"})
    assert r.status_code == 200

    for u in client.get("/api/v1/units", headers=HEADERS).json():
        assert u["status"] == "available"
        assert u["assigned_incident_id"] is None
        assert u["dispatched_at"] is None
        assert u["route_geometry"] is None


def test_false_alarm_also_frees_units(client):
    _register(client)
    iid = _incident(client)
    _assign(client, iid)
    client.post(f"/api/v1/incidents/{iid}/resolve", headers=HEADERS,
                json={"actor": "d", "outcome": "false_alarm"})
    assert client.get("/api/v1/units", headers=HEADERS).json()[0]["status"] == "available"


def test_dispatch_config_is_served(client):
    cfg = client.get("/api/v1/dispatch/config", headers=HEADERS).json()
    assert cfg["mobilize_s"] == MOBILIZE_S
    assert cfg["sim_speed"] >= 1.0
