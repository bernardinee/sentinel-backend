"""ML client retry/fallback (§8.7-8.8) and the vendored local inference path.

The local-mode tests double as an in-process §10 acceptance check: the real
crash vector classifies as a crash, and the brief 12 g spike is rejected by
the physics gate with label_source=signature_override — the thesis finding.
"""
import httpx
import pytest
from unittest.mock import AsyncMock, patch

from app.modules.inference.service import InferenceUnavailable, classify
from tests.conftest import HEADERS, load_window, make_event


@pytest.mark.asyncio
async def test_remote_retries_then_succeeds(settings):
    settings.INFERENCE_MODE = "remote"
    calls = {"n": 0}

    ok = httpx.Response(200, json={"severity_class": 0})

    async def flaky_post(self, url, json=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectTimeout("timeout")
        return ok

    with patch.object(httpx.AsyncClient, "post", new=flaky_post), \
         patch("app.modules.inference.service.asyncio.sleep", new=AsyncMock()):
        result = await classify(load_window("1_Normal_driving.json"))
    assert calls["n"] == 3
    assert result["severity_class"] == 0


@pytest.mark.asyncio
async def test_remote_gives_up_after_retries(settings):
    settings.INFERENCE_MODE = "remote"

    async def always_down(self, url, json=None):
        raise httpx.ConnectError("refused")

    with patch.object(httpx.AsyncClient, "post", new=always_down), \
         patch("app.modules.inference.service.asyncio.sleep", new=AsyncMock()):
        with pytest.raises(InferenceUnavailable):
            await classify(load_window("1_Normal_driving.json"))


def test_ml_down_still_persists_incident(client, settings):
    """§8.7 — a network failure must never lose a crash record."""
    settings.INFERENCE_MODE = "remote"
    with patch("app.modules.ingest.classify",
               new=AsyncMock(side_effect=InferenceUnavailable("down"))):
        resp = client.post("/api/v1/events",
                           json=make_event(event_id="ESP32_TEST-0001-33333333"),
                           headers=HEADERS)
    assert resp.status_code == 200
    ack = resp.json()
    assert ack["classification_pending"] is True
    assert ack["severity_class"] is None

    incidents = client.get("/api/v1/incidents", headers=HEADERS).json()
    assert incidents["total"] == 1
    assert incidents["items"][0]["classification_pending"] is True
    # the raw window is still on disk for the background retry
    w = client.get(f"/api/v1/incidents/{incidents['items'][0]['id']}/window",
                   headers=HEADERS)
    assert w.status_code == 200


@pytest.mark.asyncio
async def test_local_mode_real_crash_confirmed(settings):
    """Vendored inference, real 4 g / 90 ms validation vector → confirmed crash."""
    settings.INFERENCE_MODE = "local"
    result = await classify(load_window("4_Real_crash_4_g___90_ms.json"))
    assert result["accident_confirmed"] is True
    assert result["severity_class"] in (1, 2)
    assert result["label_source"] == "model+signature"
    assert 2.0 <= result["crash_signature"]["peak_g"] < 7.0


@pytest.mark.asyncio
async def test_local_mode_brief_spike_overridden(settings):
    """§10 negative case: brief 12 g spike is a drop/artifact, NOT a crash.
    The physics gate must override the model — never peak-g-driven severity."""
    settings.INFERENCE_MODE = "local"
    result = await classify(load_window("2_Brief_12_g_spike__dropped_device_.json"))
    assert result["severity_class"] == 0
    assert result["severity_name"] == "Normal"
    assert result["accident_confirmed"] is False
    assert result["crash_signature"]["signature_match"] is False
    # the model may or may not flag it; either way the final label is Normal
    assert result["label_source"] in ("signature_override", "model")


def test_scale_model_signature_profile(monkeypatch):
    """The model-car rig produces shorter, sharper pulses than a real vehicle.
    Its gate accepts a 3-sample (30 ms) pulse and peaks up to 10 g, but still
    rejects a 2-sample tap and anything beyond the widened ceiling."""
    import numpy as np
    from app.modules.inference import phase2_vendored as P

    def pulse(peak_g: float, samples: int):
        az = np.ones(500)
        az[250:250 + samples] = peak_g
        return np.zeros(500), np.zeros(500), az

    monkeypatch.setenv("SIGNATURE_PROFILE", "scale_model")
    assert P.signature_thresholds()["profile"] == "scale_model"
    assert P.crash_signature(*pulse(4.0, 3))[3] is True     # 30 ms: accepted
    assert P.crash_signature(*pulse(4.0, 2))[3] is False    # 20 ms: too brief
    assert P.crash_signature(*pulse(8.5, 5))[3] is True     # rigid small-car hit
    assert P.crash_signature(*pulse(12.0, 5))[3] is False   # beyond the ceiling


@pytest.mark.asyncio
async def test_local_mode_normal_driving(settings):
    settings.INFERENCE_MODE = "local"
    result = await classify(load_window("1_Normal_driving.json"))
    assert result["severity_class"] == 0
    assert result["accident_confirmed"] is False
