"""A driver's app and the responder dashboard share one incident lifecycle."""
from unittest.mock import AsyncMock, patch

from tests.conftest import HEADERS


def test_mobile_panic_and_dashboard_dispatch_stay_in_sync(client):
    registered = client.post("/api/v1/auth/register", json={
        "name": "Vanessa Driver",
        "email": "vanessa@example.com",
        "phone": "+233240000001",
        "password": "correct-horse-42",
        "device_id": "ESP32_SYNC_001",
    })
    assert registered.status_code == 201
    bearer = {"Authorization": f"Bearer {registered.json()['access_token']}"}

    with client.websocket_connect(
        "/ws/incidents",
        subprotocols=["sentinel-v1", f"bearer.{registered.json()['access_token']}"],
    ) as driver_socket, client.websocket_connect(
        "/ws/incidents?api_key=test-key"
    ) as dashboard_socket:
        panic = client.post("/api/v1/panic", headers=bearer, json={
            "device_id": "ESP32_SYNC_001", "lat": 5.65, "lon": -0.18,
        })
        assert panic.status_code == 201
        incident_id = panic.json()["id"]
        for socket in (driver_socket, dashboard_socket):
            event = socket.receive_json()
            assert event["type"] == "incident.created"
            assert event["data"]["id"] == incident_id

        acknowledged = client.post(
            f"/api/v1/incidents/{incident_id}/acknowledge",
            headers=HEADERS, json={"actor": "Dispatcher"},
        )
        assert acknowledged.status_code == 200
        assert driver_socket.receive_json()["data"]["status"] == "acknowledged"
        assert dashboard_socket.receive_json()["data"]["status"] == "acknowledged"

        unit = client.post("/api/v1/units", headers=HEADERS, json={
            "call_sign": "AMB-SYNC-1", "unit_type": "AMBULANCE",
            "station_name": "Test station", "home_lat": 5.66,
            "home_lon": -0.19,
        })
        assert unit.status_code == 201
        route = {"distance_km": 2.0, "duration_min": 5.0,
                 "geometry": [], "source": "straight_line"}
        with patch("app.modules.units.road_route", new=AsyncMock(return_value=route)):
            assigned = client.post(
                f"/api/v1/incidents/{incident_id}/assign-unit",
                headers=HEADERS,
                json={"call_sign": "AMB-SYNC-1", "actor": "Dispatcher"},
            )
        assert assigned.status_code == 200
        assert driver_socket.receive_json()["data"]["status"] == "dispatched"
        assert dashboard_socket.receive_json()["type"] == "incident.updated"
        assert dashboard_socket.receive_json()["type"] == "unit.updated"

        en_route = client.post("/api/v1/units/AMB-SYNC-1/status", headers=HEADERS,
                               json={"status": "en_route", "actor": "AMB-SYNC-1"})
        assert en_route.status_code == 200
        assert driver_socket.receive_json()["type"] == "incident.updated"
        assert dashboard_socket.receive_json()["type"] == "unit.updated"
        assert dashboard_socket.receive_json()["type"] == "incident.updated"

        scene = client.post("/api/v1/units/AMB-SYNC-1/status", headers=HEADERS,
                            json={"status": "on_scene", "actor": "AMB-SYNC-1"})
        assert scene.status_code == 200
        assert driver_socket.receive_json()["type"] == "incident.updated"
        assert dashboard_socket.receive_json()["type"] == "unit.updated"
        assert dashboard_socket.receive_json()["type"] == "incident.updated"

        history = client.get("/api/v1/me/incidents?device_id=ESP32_SYNC_001",
                             headers=bearer)
        assert history.status_code == 200
        item = history.json()[0]
        assert item["id"] == incident_id
        assert item["acknowledged_at"] is not None
        assert item["assigned_at"] is not None
        assert item["en_route_at"] is not None
        assert item["arrived_at"] is not None
        assert item["responding_units"] == ["AMB-SYNC-1"]

        responder_view = client.get(f"/api/v1/incidents/{incident_id}",
                                    headers=HEADERS)
        assert responder_view.status_code == 200
        assert [event["action"] for event in
                responder_view.json()["dispatch_events"]] == [
                    "acknowledge", "assign", "en_route", "on_scene"]

        contact = client.post(
            "/api/v1/devices/ESP32_SYNC_001/contacts", headers=bearer,
            json={"name": "Emergency contact", "phone": "+233240000099",
                  "relationship": "Family"},
        )
        assert contact.status_code == 201
        for socket in (driver_socket, dashboard_socket):
            change = socket.receive_json()
            assert change["type"] == "contact.updated"
            assert change["data"] == {"device_id": item["device_id"]}

        changed = client.patch(
            f"/api/v1/devices/ESP32_SYNC_001/contacts/{contact.json()['id']}",
            headers=HEADERS, json={"name": "Updated contact"},
        )
        assert changed.status_code == 200
        for socket in (driver_socket, dashboard_socket):
            assert socket.receive_json()["type"] == "contact.updated"
        listed = client.get("/api/v1/devices/ESP32_SYNC_001/contacts",
                            headers=bearer)
        assert listed.json()[0]["name"] == "Updated contact"

        deleted = client.delete(
            f"/api/v1/devices/ESP32_SYNC_001/contacts/{contact.json()['id']}",
            headers=bearer,
        )
        assert deleted.status_code == 204
        for socket in (driver_socket, dashboard_socket):
            assert socket.receive_json()["type"] == "contact.updated"


def test_driver_cannot_read_another_devices_incidents(client):
    registration = client.post("/api/v1/auth/register", json={
        "name": "Vanessa Driver", "email": "vanessa@example.com",
        "phone": "+233240000001", "password": "correct-horse-42",
        "device_id": "ESP32_SYNC_001",
    }).json()
    bearer = {"Authorization": f"Bearer {registration['access_token']}"}
    response = client.get("/api/v1/me/incidents?device_id=ESP32_OTHER",
                          headers=bearer)
    assert response.status_code in (403, 404)
