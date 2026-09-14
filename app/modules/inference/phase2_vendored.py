"""VENDORED from cloud_api/app.py (accident-severity-api, Phase 2/3 corrected).

This file is a verbatim port of the ML API's inference path so that
INFERENCE_MODE=local runs the EXACT same code the remote API runs — same 20 Hz
Butterworth low-pass, same 25 features, same signature gate, same calibrated
model. Reimplementing any of this would introduce train/serve skew (§8.8).

DO NOT "improve" the decision logic. The signature gate replacing the old
"peak >= 7 g -> Severe" escalation is a thesis finding (§1.3): real crashes
live in the 2-7 g band with 40-250 ms transients; severity is graded by
impulse (delta-v proxy), never by peak height alone.

Model artifacts are lazy-loaded so importing this module (e.g. in tests or in
remote mode) does not require the joblib files to be present.
"""
import json
import os
import time
from pathlib import Path

import numpy as np
from scipy import stats, signal

ART = Path(__file__).resolve().parents[3] / "artifacts"
TARGET_FS, WINDOW_SAMPLES = 100, 500
CLASS_NAMES = ["Normal", "Moderate", "Severe"]

# ── crash-signature physics thresholds (from Phase 2 / VZCrash) ──────────────
PEAK_MIN_G, PEAK_MAX_G = 2.0, 7.0
TRANSIENT_MIN_MS = float(os.environ.get("SIG_TRANSIENT_MIN_MS", "40"))
TRANSIENT_MAX_MS = 250.0
G = 9.80665
BUTTER_CUTOFF_HZ = 20.0
_SOS = signal.butter(4, BUTTER_CUTOFF_HZ / (TARGET_FS / 2.0), btype="low", output="sos")

_model = None
_features: list[str] | None = None
_crash_alert_threshold: float | None = None


def _load():
    global _model, _features, _crash_alert_threshold
    if _model is None:
        import joblib
        _model = joblib.load(ART / "phase2_xgboost_calibrated.joblib")
        _features = json.load(open(ART / "phase2_feature_names.json"))
        try:
            _cal = json.load(open(ART / "task6_calibration.json"))
            _crash_alert_threshold = float(
                _cal["recommended_thresholds"]["crash_alert_high_precision_0p90"])
        except Exception:
            _crash_alert_threshold = 0.5
    return _model, _features, _crash_alert_threshold


def _lp(x):
    """Zero-phase 20 Hz low-pass; no-op if too short."""
    return signal.sosfiltfilt(_SOS, x) if len(x) > 15 else x


def _safe(f, a, d=0.0):
    try:
        v = float(f(a))
        return v if np.isfinite(v) else d
    except Exception:
        return d


def _spec(a):
    v = np.abs(np.fft.rfft(a))
    v[0] = 0.0
    return float(np.sum(v ** 2))


def normalize_to_g(ax, ay, az):
    """Defensive: convert accel to g if the window looks like m/s² (gravity ~9.8)."""
    mag = np.sqrt(ax ** 2 + ay ** 2 + az ** 2)
    scale = G if np.median(mag) > 5.0 else 1.0
    return ax / scale, ay / scale, az / scale, scale


def features_25(ax, ay, az, gx):
    a_mag = np.sqrt(ax ** 2 + ay ** 2 + az ** 2)
    return {
        "ax_mean": ax.mean(), "ax_std": ax.std(), "ax_min": ax.min(), "ax_max": ax.max(),
        "ax_peak": np.abs(ax).max(), "ax_spectral_energy": _spec(ax),
        "ay_mean": ay.mean(), "ay_std": ay.std(), "ay_min": ay.min(), "ay_max": ay.max(),
        "ay_rms": np.sqrt((ay ** 2).mean()), "ay_kurt": _safe(stats.kurtosis, ay),
        "ay_peak": np.abs(ay).max(), "ay_spectral_energy": _spec(ay),
        "az_mean": az.mean(), "az_std": az.std(), "az_rms": np.sqrt((az ** 2).mean()),
        "az_spectral_energy": _spec(az),
        "a_mag_std": a_mag.std(), "a_mag_skew": _safe(stats.skew, a_mag),
        "a_mag_kurt": _safe(stats.kurtosis, a_mag), "a_mag_spectral_energy": _spec(a_mag),
        "gx_mean": gx.mean(), "gx_kurt": _safe(stats.kurtosis, gx),
        "mean_jerk": np.abs(np.diff(ax)).mean(),
    }


def crash_signature(ax, ay, az):
    """Physics cross-check: peak_g, longest excursion >2g (ms), impulse (g·s)."""
    mag = np.sqrt(ax ** 2 + ay ** 2 + az ** 2)
    peak = float(mag.max())
    above = mag >= PEAK_MIN_G
    longest = run = 0
    for a in above:
        run = run + 1 if a else 0
        longest = max(longest, run)
    longest_ms = longest * 1000.0 / TARGET_FS
    impulse = float(np.sum(mag[above] - 1.0) / TARGET_FS) if above.any() else 0.0
    is_sig = (PEAK_MIN_G <= peak < PEAK_MAX_G and TRANSIENT_MIN_MS <= longest_ms <= TRANSIENT_MAX_MS)
    return peak, longest_ms, impulse, bool(is_sig)


def run_inference(ax, ay, az, gx, gy, gz) -> dict:
    """Identical decision path to the remote /predict endpoint."""
    t0 = time.perf_counter()
    model, features, crash_alert_threshold = _load()
    ax, ay, az, scale = normalize_to_g(
        np.asarray(ax, float), np.asarray(ay, float), np.asarray(az, float))
    ax, ay, az = _lp(ax), _lp(ay), _lp(az)  # 20 Hz LP to match training
    gx = np.asarray(gx, float)
    feat = features_25(ax, ay, az, gx)
    X = np.nan_to_num(np.array([[feat[n] for n in features]], float))
    proba = model.predict_proba(X)[0]
    model_label = int(np.argmax(proba))
    p_crash = float(proba[1] + proba[2])
    peak, longest_ms, impulse, is_sig = crash_signature(ax, ay, az)

    # ── decision: model P(crash) AND physics signature must agree ────────────
    model_says_crash = p_crash >= crash_alert_threshold
    if model_says_crash and is_sig:
        label = model_label if model_label >= 1 else 1
        label_source = "model+signature"
    else:
        label = 0  # physics override -> Normal
        label_source = "signature_override" if model_says_crash else "model"
    accident_confirmed = bool(label >= 1)
    return {
        "severity_class": label, "severity_name": CLASS_NAMES[label],
        "confidence": round(float(proba[label]), 4),
        "p_crash": round(p_crash, 4),
        "model_severity": CLASS_NAMES[model_label],
        "accident_confirmed": accident_confirmed,
        "probabilities": {CLASS_NAMES[i]: round(float(proba[i]), 4) for i in range(3)},
        "crash_signature": {"peak_g": round(peak, 3), "excursion_ms": round(longest_ms, 1),
                            "impulse_gs": round(impulse, 3), "signature_match": is_sig},
        "unit_scale_applied": scale, "label_source": label_source,
        "inference_time_ms": round((time.perf_counter() - t0) * 1000, 2),
    }
