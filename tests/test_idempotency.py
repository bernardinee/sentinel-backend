"""Idempotency on event_id (§8.5): ESP32 retries must not create duplicates."""
from unittest.mock import AsyncMock, patch

from tests.conftest import HEADERS, make_event
from tests.test_ingest import ML_OK


def test_duplicate_event_id_returns_existing(client):
    body = make_event(event_id="ESP32_TEST-0001-11111111")
    with patch("app.modules.ingest.classify", new=AsyncMock(return_value=ML_OK)) as mock:
        first = client.post("/api/v1/events", json=body, headers=HEADERS)
        second = client.post("/api/v1/events", json=body, headers=HEADERS)
        third = client.post("/api/v1/events", json=body, headers=HEADERS)
    assert first.status_code == second.status_code == third.status_code == 200
    assert mock.await_count == 1, "inference must not re-run on retries"
    assert first.json()["event_id"] == second.json()["event_id"]

    incidents = client.get("/api/v1/incidents", headers=HEADERS).json()
    assert incidents["total"] == 1, "one shake = one incident card"


def test_retry_after_pending_returns_pending_ack(client):
    """If the first attempt failed classification, the retry returns the stored
    (pending) incident rather than re-ingesting."""
    from app.modules.inference.service import InferenceUnavailable
    body = make_event(event_id="ESP32_TEST-0001-22222222")
    with patch("app.modules.ingest.classify",
               new=AsyncMock(side_effect=InferenceUnavailable("down"))):
        first = client.post("/api/v1/events", json=body, headers=HEADERS)
    assert first.status_code == 200
    assert first.json()["classification_pending"] is True

    with patch("app.modules.ingest.classify", new=AsyncMock(return_value=ML_OK)) as mock:
        second = client.post("/api/v1/events", json=body, headers=HEADERS)
    assert second.status_code == 200
    assert mock.await_count == 0, "idempotent retry must not re-run inference inline"
    incidents = client.get("/api/v1/incidents", headers=HEADERS).json()
    assert incidents["total"] == 1
