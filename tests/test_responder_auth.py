"""Responder accounts: device-less sessions, and the privilege boundary.

The security property that matters here is that a responder account cannot be
created through the API. `/auth/register` mints drivers only — if it could mint
responders, anyone reaching the API could grant themselves dispatch control of
the whole fleet.
"""
from unittest.mock import AsyncMock, patch

from pwdlib import PasswordHash
from sqlalchemy import select

from app.db import SessionLocal
from app.models import User
from tests.conftest import HEADERS, make_event
from tests.test_ingest import ML_OK

PASSWORD = "control-room-secret-1"


def _make_responder(email: str = "ops@sentinel.gh") -> str:
    """Provision a responder the way scripts/create_responder.py does."""
    with SessionLocal() as db:
        user = User(
            name="Control Room", email=email, phone="",
            password_hash=PasswordHash.recommended().hash(PASSWORD),
            role="responder", device_id=None, active=True,
        )
        db.add(user)
        db.commit()
        return user.id


def _login(client, email: str = "ops@sentinel.gh") -> dict:
    r = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()


def test_responder_can_sign_in_without_a_device(client):
    _make_responder()
    body = _login(client)
    assert body["user"]["role"] == "responder"
    assert body["user"]["device_id"] is None, "responders are not bound to a device"
    assert body["access_token"] and body["refresh_token"]


def test_access_token_authorises_dispatch_actions(client):
    _make_responder()
    token = _login(client)["access_token"]
    auth = {"Authorization": f"Bearer {token}"}

    # the dashboard's whole surface must work on the Bearer token alone
    assert client.get("/api/v1/units", headers=auth).status_code == 200
    assert client.get("/api/v1/incidents", headers=auth).status_code == 200
    assert client.get("/api/v1/stats/summary", headers=auth).status_code == 200

    me = client.get("/api/v1/auth/me", headers=auth)
    assert me.status_code == 200
    assert me.json()["role"] == "responder"

    with patch("app.modules.ingest.classify", new=AsyncMock(return_value=ML_OK)):
        client.post("/api/v1/events", json=make_event(), headers=HEADERS)
    iid = client.get("/api/v1/incidents", headers=auth).json()["items"][0]["id"]

    r = client.post(f"/api/v1/incidents/{iid}/acknowledge",
                    json={"actor": "control-room"}, headers=auth)
    assert r.status_code == 200
    assert r.json()["status"] == "acknowledged"


def test_register_cannot_create_a_responder(client):
    """The privilege boundary: self-registration yields a driver, never a
    responder, whatever the caller asks for."""
    r = client.post("/api/v1/auth/register", json={
        "name": "Attacker", "email": "attacker@example.com",
        "phone": "+233000000000", "password": "hunter2hunter2",
        "device_id": "ESP32_ATTACK", "role": "responder",   # ignored
    })
    assert r.status_code == 201
    assert r.json()["user"]["role"] == "driver"

    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == "attacker@example.com"))
        assert user.role == "driver"

    token = r.json()["access_token"]
    denied = client.post("/api/v1/units", headers={"Authorization": f"Bearer {token}"},
                         json={"call_sign": "AMB-99", "unit_type": "AMBULANCE",
                               "station_name": "x", "home_lat": 5.6, "home_lon": -0.1})
    assert denied.status_code == 403, "a driver must not manage the fleet"


def test_refresh_rotates_for_a_responder(client):
    _make_responder()
    first = _login(client)
    r = client.post("/api/v1/auth/refresh",
                    json={"refresh_token": first["refresh_token"]})
    assert r.status_code == 200
    second = r.json()
    assert second["refresh_token"] != first["refresh_token"]
    assert second["user"]["device_id"] is None

    # the rotated token is dead, and reuse revokes the family
    reuse = client.post("/api/v1/auth/refresh",
                        json={"refresh_token": first["refresh_token"]})
    assert reuse.status_code == 401


def test_deactivated_responder_is_locked_out(client):
    user_id = _make_responder()
    token = _login(client)["access_token"]
    auth = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/v1/units", headers=auth).status_code == 200

    with SessionLocal() as db:
        db.get(User, user_id).active = False
        db.commit()

    # an already-issued token stops working immediately, not at expiry
    assert client.get("/api/v1/units", headers=auth).status_code == 401
    assert client.post("/api/v1/auth/login",
                       json={"email": "ops@sentinel.gh",
                             "password": PASSWORD}).status_code == 401


def test_bad_password_is_rejected(client):
    _make_responder()
    r = client.post("/api/v1/auth/login",
                    json={"email": "ops@sentinel.gh", "password": "wrong-password"})
    assert r.status_code == 401
