"""Dispatch state machine (§5.3): every action writes dispatch_events and the
timeline derives from that table."""
from unittest.mock import AsyncMock, patch

from tests.conftest import HEADERS, make_event
from tests.test_ingest import ML_OK


def _create_incident(client) -> str:
    with patch("app.modules.ingest.classify", new=AsyncMock(return_value=ML_OK)):
        client.post("/api/v1/events", json=make_event(), headers=HEADERS)
    return client.get("/api/v1/incidents", headers=HEADERS).json()["items"][0]["id"]


def test_full_lifecycle(client):
    iid = _create_incident(client)

    r = client.post(f"/api/v1/incidents/{iid}/acknowledge",
                    json={"actor": "operator-1", "note": "on it"}, headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["status"] == "acknowledged"
    assert r.json()["acknowledged_by"] == "operator-1"

    r = client.post(f"/api/v1/incidents/{iid}/dispatch",
                    json={"actor": "operator-1", "action": "en_route"}, headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["status"] == "dispatched"

    r = client.post(f"/api/v1/incidents/{iid}/dispatch",
                    json={"actor": "unit-7", "action": "on_scene"}, headers=HEADERS)
    assert r.status_code == 200

    r = client.post(f"/api/v1/incidents/{iid}/resolve",
                    json={"actor": "operator-1", "outcome": "resolved"}, headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["status"] == "resolved"
    assert r.json()["resolved_at"] is not None

    detail = client.get(f"/api/v1/incidents/{iid}", headers=HEADERS).json()
    actions = [e["action"] for e in detail["dispatch_events"]]
    assert actions == ["acknowledge", "en_route", "on_scene", "resolve"]


def test_illegal_transitions_rejected(client):
    iid = _create_incident(client)

    # on_scene before dispatch
    r = client.post(f"/api/v1/incidents/{iid}/dispatch",
                    json={"actor": "x", "action": "on_scene"}, headers=HEADERS)
    assert r.status_code == 409

    client.post(f"/api/v1/incidents/{iid}/acknowledge",
                json={"actor": "x"}, headers=HEADERS)
    # double acknowledge
    r = client.post(f"/api/v1/incidents/{iid}/acknowledge",
                    json={"actor": "x"}, headers=HEADERS)
    assert r.status_code == 409

    client.post(f"/api/v1/incidents/{iid}/resolve",
                json={"actor": "x", "outcome": "false_alarm"}, headers=HEADERS)
    # act on a closed incident
    r = client.post(f"/api/v1/incidents/{iid}/dispatch",
                    json={"actor": "x", "action": "assign"}, headers=HEADERS)
    assert r.status_code == 409


def test_false_alarm_path(client):
    iid = _create_incident(client)
    r = client.post(f"/api/v1/incidents/{iid}/resolve",
                    json={"actor": "op", "outcome": "false_alarm", "note": "bench tap"},
                    headers=HEADERS)
    assert r.status_code == 200
    assert r.json()["status"] == "false_alarm"

    active = client.get("/api/v1/incidents/active", headers=HEADERS).json()
    assert active == []


def test_panic_excluded_from_stats(client):
    _create_incident(client)
    r = client.post("/api/v1/panic",
                    json={"device_id": "ESP32_TEST", "lat": 5.65, "lon": -0.18},
                    headers=HEADERS)
    assert r.status_code == 201
    assert r.json()["label_source"] == "manual_panic"

    stats = client.get("/api/v1/stats/summary", headers=HEADERS).json()
    # the ML incident counts; the panic one must NOT contaminate model stats
    assert stats["total_incidents"] == 1
    assert "manual_panic" not in stats["by_label_source"]

    # but it IS in the feed and the active queue
    incidents = client.get("/api/v1/incidents", headers=HEADERS).json()
    assert incidents["total"] == 2
    active = client.get("/api/v1/incidents/active", headers=HEADERS).json()
    assert len(active) == 2
