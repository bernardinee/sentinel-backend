"""rc_demo signature profile: provisional thresholds for the RC-car demonstration.

Default stays full_scale; rc_demo decides on the gate alone (no model, no ML API),
labels incidents rc_demo_threshold, and those incidents never reach stats.
"""
from unittest.mock import AsyncMock, patch

import numpy as np
import pytest

from app.modules.inference import phase2_vendored as P
from tests.conftest import HEADERS, make_event
from tests.test_dispatch import ML_OK


def rc_hit(raw_g: float, samples: int = 1, axis: str = "ax") -> dict:
    """RC-scale impact: a 1-3 sample spike on a horizontal axis, gravity on z."""
    w = {k: [0.0] * 500 for k in ("ax", "ay", "az", "gx", "gy", "gz")}
    w["az"] = [1.0] * 500
    for i in range(samples):
        w[axis][250 + i] = raw_g
    return w


def test_default_profile_is_full_scale(monkeypatch):
    monkeypatch.delenv("SIGNATURE_PROFILE", raising=False)
    assert P.active_profile() == ("full_scale", P.FULL_SCALE)
    monkeypatch.setenv("SIGNATURE_PROFILE", "demo")
    assert P.active_profile()[0] == "full_scale"
    assert not P.is_threshold_only()


@pytest.mark.parametrize("raw_g,samples,expected", [
    (3.0, 1, "Normal"),     # tap / put-down: filtered peak 1.6 g
    (4.0, 1, "Normal"),     # filtered 1.9 g, below the 2.0 g floor
    (6.0, 1, "Moderate"),   # soft impact
    (8.0, 2, "Severe"),     # impulse 0.113 g·s
    (16.0, 1, "Severe"),    # full-throttle hit clipping the ±16 g sensor
    (5.0, 2, "Moderate"),
])
def test_rc_demo_thresholds(monkeypatch, raw_g, samples, expected):
    monkeypatch.setenv("SIGNATURE_PROFILE", "rc_demo")
    w = rc_hit(raw_g, samples)
    res = P.run_threshold_only(w["ax"], w["ay"], w["az"], w["gx"], w["gy"], w["gz"])
    assert res["severity_name"] == expected, res["crash_signature"]
    assert res["label_source"] == "rc_demo_threshold" and res["p_crash"] is None
    assert res["signature_profile"]["profile"] == "rc_demo"


def test_rc_demo_env_overrides(monkeypatch):
    monkeypatch.setenv("SIGNATURE_PROFILE", "rc_demo")
    monkeypatch.setenv("RC_DEMO_PEAK_MIN_G", "1.5")
    assert P.active_profile()[1]["peak_min_g"] == 1.5
    w = rc_hit(4.0)
    assert P.run_threshold_only(w["ax"], w["ay"], w["az"], w["gx"], w["gy"], w["gz"])["severity_name"] == "Moderate"


def test_rc_demo_ingest_bypasses_ml_api_and_is_excluded_from_stats(client, monkeypatch):
    # one normal ML-classified incident that must remain the only one in stats
    with patch("app.modules.ingest.classify", new=AsyncMock(return_value=ML_OK)):
        assert client.post("/api/v1/events", json=make_event("ESP32_TEST-0001-00000001"),
                           headers=HEADERS).status_code in (200, 201)

    monkeypatch.setenv("SIGNATURE_PROFILE", "rc_demo")
    with patch("app.modules.inference.service._predict_remote",
               new=AsyncMock(side_effect=AssertionError("ML API must not be called"))):
        r = client.post("/api/v1/events", json=make_event("ESP32_TEST-0001-00000002", window=rc_hit(16.0)),
                        headers=HEADERS)
    assert r.status_code in (200, 201), r.text
    ack = r.json()
    assert ack["severity_name"] == "Severe" and ack["label_source"] == "rc_demo_threshold"

    stats = client.get("/api/v1/stats/summary", headers=HEADERS).json()
    assert stats["total_incidents"] == 1
    assert "rc_demo_threshold" not in stats["by_label_source"]
    feed = client.get("/api/v1/incidents", headers=HEADERS).json()
    assert feed["total"] == 2          # still visible in the feed and dispatch queue
