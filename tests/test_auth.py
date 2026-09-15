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


def test_duplicate_accounts_and_cross_device_access_are_blocked(client):
    first = _register(client)
    assert _register(client).status_code == 409
    assert _register(
        client, email="someone@example.com", device_id="ESP32_AUTH_001"
    ).status_code == 409

    auth = _bearer(first.json()["access_token"])
    assert client.get("/api/v1/incidents", headers=auth).status_code == 403
    assert client.get(
        "/api/v1/me/protection-status?device_id=SOMEONE_ELSE", headers=auth
    ).status_code == 403


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
