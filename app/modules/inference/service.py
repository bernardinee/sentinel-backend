"""Inference dispatcher: remote ML API (with retries) or in-process local mode.

Remote failures raise InferenceUnavailable — the caller persists the incident
as classification_pending and a background task retries later (§8.7). A network
failure must never lose a crash record.
"""
import asyncio
import logging

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

RETRIES = 2  # total attempts = 1 + RETRIES
BACKOFF_S = [1.0, 3.0]


class InferenceUnavailable(Exception):
    pass


async def _predict_remote(window: dict) -> dict:
    settings = get_settings()
    url = settings.ML_API_URL.rstrip("/") + "/predict"
    payload = {k: window[k] for k in ("ax", "ay", "az", "gx", "gy", "gz")}
    last_exc: Exception | None = None
    for attempt in range(1 + RETRIES):
        try:
            async with httpx.AsyncClient(timeout=settings.ML_TIMEOUT_S) as client:
                resp = await client.post(url, json=payload)
            if resp.status_code == 200:
                return resp.json()
            # 4xx is a contract violation — retrying will not help.
            if 400 <= resp.status_code < 500:
                raise InferenceUnavailable(
                    f"ML API rejected request ({resp.status_code}): {resp.text[:200]}")
            last_exc = InferenceUnavailable(f"ML API {resp.status_code}")
        except InferenceUnavailable:
            raise
        except Exception as exc:  # timeout, DNS, connection reset …
            last_exc = exc
        if attempt < RETRIES:
            await asyncio.sleep(BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)])
    raise InferenceUnavailable(str(last_exc))


def _predict_local(window: dict) -> dict:
    from app.modules.inference import phase2_vendored
    return phase2_vendored.run_inference(
        window["ax"], window["ay"], window["az"],
        window["gx"], window["gy"], window["gz"])


async def classify(window: dict) -> dict:
    """window: dict with ax..gz lists of 500 floats. Returns ML API response shape."""
    mode = get_settings().INFERENCE_MODE.lower()
    if mode == "local":
        return await asyncio.to_thread(_predict_local, window)
    return await _predict_remote(window)


async def ml_health() -> dict:
    """Proxy of the ML API /health for the dashboard top bar."""
    settings = get_settings()
    if settings.INFERENCE_MODE.lower() == "local":
        try:
            from app.modules.inference import phase2_vendored
            _, features, thr = phase2_vendored._load()
            return {"status": "healthy", "mode": "local",
                    "model": "phase2_xgboost_calibrated (local)",
                    "n_features": len(features), "crash_alert_threshold": thr,
                    "taxonomy": "signature+impulse (v2)"}
        except Exception as exc:
            return {"status": "error", "mode": "local", "error": str(exc)}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(settings.ML_API_URL.rstrip("/") + "/health")
        data = resp.json()
        data["mode"] = "remote"
        return data
    except Exception as exc:
        return {"status": "unreachable", "mode": "remote", "error": str(exc)}
