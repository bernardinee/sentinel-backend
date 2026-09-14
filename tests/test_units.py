"""Unit roster and dispatch routing.

Routing is stubbed so the suite never depends on the public OSRM demo being
reachable; the stub returns distinct durations so the RANKING logic is what is
actually under test.
"""
from unittest.mock import AsyncMock, patch

from tests.conftest import HEADERS, make_event
from tests.test_ingest import ML_OK


def _register(client, call_sign, unit_type, lat, lon, station="Station"):
    return client.post("/api/v1/units", headers=HEADERS, json={
        "call_sign": call_sign, "unit_type": unit_type, "station_name": station,
        "home_lat": lat, "home_lon": lon, "crew_size": 3,
    })


def _incident(client) -> str:
    with patch("app.modules.ingest.classify", new=AsyncMock(return_value=ML_OK)):
        client.post("/api/v1/events", json=make_event(), headers=HEADERS)
    return client.get("/api/v1/incidents", headers=HEADERS).json()["items"][0]["id"]


def test_register_and_list_units(client):
    assert _register(client, "AMB-01", "AMBULANCE", 5.6510, -0.1870).status_code == 201
    assert _register(client, "AMB-01", "AMBULANCE", 5.65, -0.18).status_code == 409

    units = client.get("/api/v1/units", headers=HEADERS).json()
    assert len(units) == 1
    assert units[0]["status"] == "available"
    assert units[0]["unit_type"] == "AMBULANCE"


def test_options_ranked_by_road_time_not_distance(client):
    """The nearest unit in a straight line is not always the fastest by road —
    ranking must follow the route, not the crow flight."""
    # AMB-FAR is further away but has a much faster road route.
    _register(client, "AMB-NEAR", "AMBULANCE", 5.6590, -0.1820, "Near but slow")
    _register(client, "AMB-FAR", "AMBULANCE", 5.7000, -0.2500, "Far but fast")
    _register(client, "POL-01", "POLICE", 5.6480, -0.1880, "Police")
    iid = _incident(client)

    async def fake_route(from_lat, from_lon, to_lat, to_lon, with_geometry=True):
        slow = abs(from_lat - 5.6590) < 0.001
        return {"distance_km": 1.0 if slow else 9.0,
                "duration_min": 22.0 if slow else 7.0,
                "geometry": [[from_lon, from_lat], [to_lon, to_lat]],
                "source": "osrm"}

    with patch("app.modules.routing.road_route", new=fake_route):
        resp = client.get(f"/api/v1/incidents/{iid}/dispatch-options", headers=HEADERS)

    assert resp.status_code == 200
    data = resp.json()
    calls = [o["unit"]["call_sign"] for o in data["options"]]
    assert calls[0] == "AMB-FAR", "must rank by travel time, not distance"

    # ML_OK is Moderate -> ambulance + police advised
    assert set(data["required_types"]) == {"AMBULANCE", "POLICE"}
    rec = {o["unit"]["call_sign"] for o in data["options"] if o["recommended"]}
    assert rec == {"AMB-FAR", "POL-01"}, "fastest of each required type is flagged"


def test_options_fall_back_when_routing_is_down(client):
    _register(client, "AMB-01", "AMBULANCE", 5.6510, -0.1870)
    iid = _incident(client)

    import httpx

    async def boom(self, url, params=None):
        raise httpx.ConnectError("osrm down")

    with patch.object(httpx.AsyncClient, "get", new=boom):
        data = client.get(f"/api/v1/incidents/{iid}/dispatch-options",
                          headers=HEADERS).json()

    assert data["routing_source"] == "straight_line"
    assert data["options"][0]["route"]["source"] == "straight_line"
    assert data["options"][0]["eta_min"] > 0
    assert "estimate" in (data["note"] or "")


def test_assign_unit_updates_both_sides(client):
    _register(client, "AMB-01", "AMBULANCE", 5.6510, -0.1870)
    iid = _incident(client)

    r = client.post(f"/api/v1/incidents/{iid}/assign-unit", headers=HEADERS,
                    json={"call_sign": "AMB-01", "actor": "dispatcher"})
    assert r.status_code == 200
    assert r.json()["status"] == "dispatched"
    assert r.json()["acknowledged_by"] == "dispatcher"

    unit = client.get("/api/v1/units", headers=HEADERS).json()[0]
    assert unit["status"] == "dispatched"
    assert unit["assigned_incident_id"] == iid

    detail = client.get(f"/api/v1/incidents/{iid}", headers=HEADERS).json()
    assign = [e for e in detail["dispatch_events"] if e["action"] == "assign"]
    assert len(assign) == 1
    assert "AMB-01" in assign[0]["note"]


def test_busy_unit_cannot_take_a_second_incident(client):
    _register(client, "AMB-01", "AMBULANCE", 5.6510, -0.1870)
    first = _incident(client)
    client.post(f"/api/v1/incidents/{first}/assign-unit", headers=HEADERS,
                json={"call_sign": "AMB-01", "actor": "d"})

    with patch("app.modules.ingest.classify", new=AsyncMock(return_value=ML_OK)):
        client.post("/api/v1/events", headers=HEADERS,
                    json=make_event(event_id="ESP32_TEST-0001-99999999"))
    second = client.get("/api/v1/incidents", headers=HEADERS).json()["items"][0]["id"]

    r = client.post(f"/api/v1/incidents/{second}/assign-unit", headers=HEADERS,
                    json={"call_sign": "AMB-01", "actor": "d"})
    assert r.status_code == 409


def test_unit_status_progression_and_clear(client):
    _register(client, "AMB-01", "AMBULANCE", 5.6510, -0.1870)
    iid = _incident(client)
    client.post(f"/api/v1/incidents/{iid}/assign-unit", headers=HEADERS,
                json={"call_sign": "AMB-01", "actor": "d"})

    for status in ("en_route", "on_scene"):
        r = client.post("/api/v1/units/AMB-01/status", headers=HEADERS,
                        json={"status": status, "actor": "AMB-01"})
        assert r.status_code == 200
        assert r.json()["status"] == status

    detail = client.get(f"/api/v1/incidents/{iid}", headers=HEADERS).json()
    actions = [e["action"] for e in detail["dispatch_events"]]
    assert actions == ["assign", "en_route", "on_scene"]

    # clearing frees the unit for the next call
    r = client.post("/api/v1/units/AMB-01/status", headers=HEADERS,
                    json={"status": "available", "actor": "d"})
    assert r.json()["status"] == "available"
    assert r.json()["assigned_incident_id"] is None


def test_incident_without_gps_cannot_be_routed(client):
    _register(client, "AMB-01", "AMBULANCE", 5.6510, -0.1870)
    body = make_event(event_id="ESP32_TEST-0001-77777777")
    body["gps"] = {"valid": False}
    with patch("app.modules.ingest.classify", new=AsyncMock(return_value=ML_OK)):
        client.post("/api/v1/events", json=body, headers=HEADERS)
    iid = client.get("/api/v1/incidents", headers=HEADERS).json()["items"][0]["id"]

    r = client.get(f"/api/v1/incidents/{iid}/dispatch-options", headers=HEADERS)
    assert r.status_code == 409
    assert "GPS" in r.json()["detail"]
