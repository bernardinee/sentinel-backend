"""Mobile-app hooks and their public response contract."""

from tests.conftest import HEADERS


def _register_device(client) -> None:
    response = client.post(
        "/api/v1/heartbeat",
        headers=HEADERS,
        json={
            "device_id": "ESP32_TEST",
            "gps": {
                "lat": 5.65,
                "lon": -0.18,
                "valid": True,
                "satellites": 6,
            },
            "device": {"uptime_s": 123, "free_heap": 140000, "rssi": -55},
            "battery_v": 3.9,
        },
    )
    assert response.status_code == 200


def test_contact_crud_uses_public_relationship_field(client):
    """Never leak the ORM-only `relationship_` attribute to either client."""
    _register_device(client)

    created = client.post(
        "/api/v1/devices/ESP32_TEST/contacts",
        headers=HEADERS,
        json={
            "name": "Ama Driver",
            "phone": "+233241112222",
            "relationship": "Sister",
            "priority": 1,
            "active": True,
        },
    )
    assert created.status_code == 201
    contact = created.json()
    assert contact["relationship"] == "Sister"
    assert "relationship_" not in contact

    patched = client.patch(
        f"/api/v1/devices/ESP32_TEST/contacts/{contact['id']}",
        headers=HEADERS,
        json={"relationship": "Emergency contact"},
    )
    assert patched.status_code == 200
    assert patched.json()["relationship"] == "Emergency contact"

    listed = client.get(
        "/api/v1/devices/ESP32_TEST/contacts", headers=HEADERS
    ).json()
    assert listed[0]["relationship"] == "Emergency contact"

    deleted = client.delete(
        f"/api/v1/devices/ESP32_TEST/contacts/{contact['id']}", headers=HEADERS
    )
    assert deleted.status_code == 204
