from app.auth import Principal
from app.modules.ws import ConnectionManager

from .conftest import HEADERS


def _register(client, *, email="driver@example.com", device_id="ESP32_AUTH_001"):
    return client.post(
        "/api/v1/auth/register",
        json={
            "name": "Ama Mensah",
            "email": email,
            "phone": "+233240000001",
            "password": "correct-horse-42",
            "device_id": device_id,
        },
    )


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_register_login_and_owned_device_access(client):
    registered = _register(client)
    assert registered.status_code == 201
    session = registered.json()
    assert session["token_type"] == "bearer"
    assert session["expires_in"] == 900
    assert session["user"]["email"] == "driver@example.com"
    assert session["user"]["device_id"] == "ESP32_AUTH_001"
    assert "password" not in session["user"]

    auth = _bearer(session["access_token"])
    me = client.get("/api/v1/auth/me", headers=auth)
    assert me.status_code == 200
    assert me.json()["name"] == "Ama Mensah"
    assert client.get(
        "/api/v1/me/protection-status?device_id=ESP32_AUTH_001", headers=auth
    ).status_code == 200

    wrong = client.post(
        "/api/v1/auth/login",
        json={"email": "driver@example.com", "password": "wrong-password"},
    )
    assert wrong.status_code == 401
    logged_in = client.post(
        "/api/v1/auth/login",
        json={"email": "DRIVER@example.com", "password": "correct-horse-42"},
    )
    assert logged_in.status_code == 200


def test_duplicate_emails_are_blocked_but_a_device_can_be_shared(client):
    first = _register(client)
    assert _register(client).status_code == 409

    shared = _register(
        client, email="someone@example.com", device_id="ESP32_AUTH_001"
    )
    assert shared.status_code == 201
    assert shared.json()["user"]["device_id"] == "ESP32_AUTH_001"

    auth = _bearer(first.json()["access_token"])
    assert client.get("/api/v1/incidents", headers=auth).status_code == 403
    assert client.get(
        "/api/v1/me/protection-status?device_id=SOMEONE_ELSE", headers=auth
    ).status_code == 403


def test_driver_can_create_and_read_contacts_with_a_bearer_token(client):
    session = _register(client).json()
    auth = _bearer(session["access_token"])

    created = client.post(
        "/api/v1/devices/ESP32_AUTH_001/contacts",
        headers=auth,
        json={
            "name": "Kojo Mensah",
            "phone": "+233240000099",
            "relationship": "Roommate",
            "priority": 1,
            "active": True,
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["relationship"] == "Roommate"

    listed = client.get(
        "/api/v1/devices/ESP32_AUTH_001/contacts", headers=auth
    )
    assert listed.status_code == 200
    assert [contact["name"] for contact in listed.json()] == ["Kojo Mensah"]


def test_contacts_are_private_between_drivers_sharing_one_esp32(client):
    vanessa = _register(client, email="vanessa@example.com").json()
    friend = _register(client, email="friend@example.com").json()
    vanessa_auth = _bearer(vanessa["access_token"])
    friend_auth = _bearer(friend["access_token"])
    path = "/api/v1/devices/ESP32_AUTH_001/contacts"

    bernardine = client.post(path, headers=vanessa_auth, json={
        "name": "Bernardine", "phone": "+233240000099",
    })
    assert bernardine.status_code == 201
    contact_id = bernardine.json()["id"]

    assert [row["name"] for row in client.get(path, headers=vanessa_auth).json()] == [
        "Bernardine"]
    assert client.get(path, headers=friend_auth).json() == []
    assert client.patch(f"{path}/{contact_id}", headers=friend_auth,
                        json={"name": "Changed"}).status_code == 404
    assert client.delete(f"{path}/{contact_id}", headers=friend_auth).status_code == 404

    friend_contact = client.post(path, headers=friend_auth, json={
        "name": "Friend's contact", "phone": "+233240000088",
    })
    assert friend_contact.status_code == 201
    assert [row["name"] for row in client.get(path, headers=friend_auth).json()] == [
        "Friend's contact"]
    assert [row["name"] for row in client.get(path, headers=vanessa_auth).json()] == [
        "Bernardine"]

    # Dispatchers may inspect the device's full contact roster; a shared,
    # dispatcher-maintained contact must not silently become a driver's own.
    shared = client.post(path, headers=HEADERS, json={
        "name": "Dispatcher contact", "phone": "+233240000077",
    })
    assert shared.status_code == 201
    assert len(client.get(path, headers=HEADERS).json()) == 3
    assert len(client.get(path, headers=friend_auth).json()) == 1


def test_manual_sos_does_not_treat_zero_pair_as_a_gps_fix(client):
    session = _register(client).json()
    auth = _bearer(session["access_token"])
    response = client.post("/api/v1/panic", headers=auth, json={
        "device_id": "ESP32_AUTH_001", "lat": 0, "lon": 0,
    })
    assert response.status_code == 201
    assert response.json()["lat"] is None
    assert response.json()["lon"] is None
    assert response.json()["gps_valid"] is False


def test_refresh_tokens_rotate_detect_reuse_and_logout(client):
    original = _register(client).json()
    refreshed = client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": original["refresh_token"]},
    )
    assert refreshed.status_code == 200
    replacement = refreshed.json()
    assert replacement["refresh_token"] != original["refresh_token"]

    reused = client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": original["refresh_token"]},
    )
    assert reused.status_code == 401
    assert client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": replacement["refresh_token"]},
    ).status_code == 401

    fresh_login = client.post(
        "/api/v1/auth/login",
        json={"email": "driver@example.com", "password": "correct-horse-42"},
    ).json()
    assert client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": fresh_login["refresh_token"]},
    ).status_code == 204
    assert client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": fresh_login["refresh_token"]},
    ).status_code == 401


def test_driver_websocket_receives_owned_device_updates(client):
    session = _register(client).json()
    with client.websocket_connect(
        "/ws/incidents",
        subprotocols=["sentinel-v1", f"bearer.{session['access_token']}"],
    ) as websocket:
        heartbeat = client.post(
            "/api/v1/heartbeat",
            headers=HEADERS,
            json={
                "device_id": "ESP32_AUTH_001",
                "gps": {"lat": 5.6, "lon": -0.18, "valid": True, "satellites": 7},
                "device": {"uptime_s": 10, "rssi": -55},
                "battery_v": 4.0,
            },
        )
        assert heartbeat.status_code == 200
        event = websocket.receive_json()
        assert event["type"] == "device_status"
        assert event["data"]["device_id"] == "ESP32_AUTH_001"


def test_websocket_filter_accepts_only_the_drivers_device():
    driver = Principal(
        role="driver",
        subject="driver@example.com",
        user_id="user-1",
        device_db_id="device-db-1",
        device_id="ESP32_1",
    )
    responder = Principal(role="responder", subject="api-key:responder")
    assert ConnectionManager.can_receive(driver, {"device_id": "ESP32_1"})
    assert ConnectionManager.can_receive(driver, {"device_id": "device-db-1"})
    assert not ConnectionManager.can_receive(driver, {"device_id": "ESP32_2"})
    assert ConnectionManager.can_receive(responder, {"device_id": "ESP32_2"})
