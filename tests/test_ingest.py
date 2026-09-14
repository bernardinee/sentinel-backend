"""Ingest validation (§5.1, §8.2, §8.3): sample count, fs, units — fail loudly."""
from unittest.mock import AsyncMock, patch

from tests.conftest import HEADERS, load_window, make_event

ML_OK = {
    "severity_class": 1, "severity_name": "Moderate", "confidence": 0.91,
    "p_crash": 0.93, "model_severity": "Moderate", "accident_confirmed": True,
    "probabilities": {"Normal": 0.07, "Moderate": 0.91, "Severe": 0.02},
    "crash_signature": {"peak_g": 4.0, "excursion_ms": 90.0,
                        "impulse_gs": 0.3, "signature_match": True},
    "unit_scale_applied": 1.0, "label_source": "model+signature",
    "inference_time_ms": 5.0,
}


def test_requires_api_key(client):
    resp = client.post("/api/v1/events", json=make_event())
    assert resp.status_code == 401


def test_valid_event_persists_and_classifies(client):
    with patch("app.modules.ingest.classify", new=AsyncMock(return_value=ML_OK)):
        resp = client.post("/api/v1/events", json=make_event(), headers=HEADERS)
    assert resp.status_code == 200, resp.text
    ack = resp.json()
    # flat ACK contract for the ESP32
    assert ack["severity_class"] == 1
    assert ack["severity_name"] == "Moderate"
    assert ack["accident_confirmed"] is True
    assert ack["label_source"] == "model+signature"
    assert not any(isinstance(v, dict) for v in ack.values()), "ACK must be flat"

    incidents = client.get("/api/v1/incidents", headers=HEADERS).json()
    assert incidents["total"] == 1
    incident = incidents["items"][0]
    assert incident["peak_g"] == 4.0
    assert incident["status"] == "new"

    # raw window persisted (§4 — the evidence trail)
    w = client.get(f"/api/v1/incidents/{incident['id']}/window", headers=HEADERS)
    assert w.status_code == 200
    assert len(w.json()["ax"]) == 500


def test_wrong_sample_count_rejected(client):
    window = load_window("1_Normal_driving.json")
    window["ax"] = window["ax"][:499]
    resp = client.post("/api/v1/events", json=make_event(window=window), headers=HEADERS)
    assert resp.status_code == 422
    assert "500" in resp.text


def test_wrong_fs_rejected(client):
    body = make_event()
    body["window"]["fs_hz"] = 50
    resp = client.post("/api/v1/events", json=body, headers=HEADERS)
    assert resp.status_code == 422


def test_wrong_units_flag_rejected(client):
    body = make_event()
    body["window"]["units_accel"] = "m/s2"
    resp = client.post("/api/v1/events", json=body, headers=HEADERS)
    assert resp.status_code == 422


def test_ms2_data_rejected_with_named_cause(client):
    """§8.2 — data that LOOKS like m/s² is rejected, never silently rescaled."""
    window = load_window("1_Normal_driving.json")
    scaled = {k: [v * 9.80665 for v in window[k]] if k.startswith("a") else window[k]
              for k in window}
    resp = client.post("/api/v1/events", json=make_event(window=scaled), headers=HEADERS)
    assert resp.status_code == 422
    assert "m/s²" in resp.text or "m/s" in resp.text


def test_heartbeat_updates_device(client):
    body = {"device_id": "ESP32_TEST",
            "gps": {"lat": 5.65, "lon": -0.18, "valid": True, "satellites": 6},
            "device": {"uptime_s": 123, "free_heap": 140000, "rssi": -55},
            "battery_v": 3.9}
    resp = client.post("/api/v1/heartbeat", json=body, headers=HEADERS)
    assert resp.status_code == 200
    devices = client.get("/api/v1/devices", headers=HEADERS).json()
    assert len(devices) == 1
    d = devices[0]
    assert d["status"] == "online"
    assert d["last_satellites"] == 6

    hb = client.get("/api/v1/devices/ESP32_TEST/heartbeats?hours=1", headers=HEADERS).json()
    assert len(hb) == 1
    assert hb[0]["battery_v"] == 3.9
