"""Null-island (0,0) coordinates must never be stored as a real GPS fix.

A receiver with no lock — or the mobile app firing a panic before it has one —
can still emit (0, 0), a point in the Atlantic off West Africa. If that were
kept, the dispatch map would fly into the ocean instead of showing 'no fix'.
"""
from unittest.mock import AsyncMock, patch

from app.geo import clean_fix, has_fix
from tests.conftest import HEADERS, make_event
from tests.test_ingest import ML_OK


def test_has_fix_rejects_null_island():
    assert has_fix(5.6581, -0.1812) is True
    assert has_fix(0.0, 0.0) is False
    assert has_fix(None, None) is False
    assert has_fix(5.6581, None) is False
    assert clean_fix(0.0, 0.0) == (None, None)
    assert clean_fix(5.6581, -0.1812) == (5.6581, -0.1812)


def test_event_with_zero_gps_is_stored_as_no_fix(client):
    body = make_event(event_id="ESP32_TEST-0001-000000ZZ",
                      gps={"lat": 0.0, "lon": 0.0, "valid": True, "satellites": 0})
    with patch("app.modules.ingest.classify", new=AsyncMock(return_value=ML_OK)):
        r = client.post("/api/v1/events", json=body, headers=HEADERS)
    assert r.status_code == 200

    inc = client.get("/api/v1/incidents", headers=HEADERS).json()["items"][0]
    assert inc["lat"] is None and inc["lon"] is None
    assert inc["gps_valid"] is False


def test_real_gps_still_stored(client):
    with patch("app.modules.ingest.classify", new=AsyncMock(return_value=ML_OK)):
        client.post("/api/v1/events", json=make_event(), headers=HEADERS)
    inc = client.get("/api/v1/incidents", headers=HEADERS).json()["items"][0]
    assert inc["lat"] == 5.6581 and inc["lon"] == -0.1812
    assert inc["gps_valid"] is True
