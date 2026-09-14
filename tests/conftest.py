"""Test fixtures. SQLite file DB per session so no Docker/Postgres is needed;
the schema is identical because models use portable column types."""
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_sentinel.db")
os.environ.setdefault("API_KEY", "test-key")
os.environ.setdefault("ROLE_KEYS", json.dumps({"device-key": "device", "driver-key": "driver"}))
os.environ.setdefault("INFERENCE_MODE", "remote")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import Base, engine  # noqa: E402
from app.main import app  # noqa: E402

HEADERS = {"X-API-Key": "test-key"}
SAMPLES = Path(__file__).resolve().parents[1] / "scripts" / "samples"


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def settings():
    s = get_settings()
    before = s.model_dump()
    yield s
    for k, v in before.items():
        setattr(s, k, v)


def load_window(name: str) -> dict:
    return json.loads((SAMPLES / name).read_text())


def make_event(event_id: str = "ESP32_TEST-0001-00000001",
               window: dict | None = None, **overrides) -> dict:
    window = window or load_window("4_Real_crash_4_g___90_ms.json")
    body = {
        "device_id": "ESP32_TEST",
        "event_id": event_id,
        "detected_at": "2026-09-14T10:00:00Z",
        "trigger": {"peak_g": 4.0, "jerk_gs": 120.0},
        "gps": {"lat": 5.6581, "lon": -0.1812, "valid": True, "satellites": 7,
                "speed_kmh": 42.3},
        "device": {"uptime_s": 1000, "free_heap": 150000, "rssi": -60,
                   "firmware_version": "2.0.0-test"},
        "window": {"fs_hz": 100, "units_accel": "g", "units_gyro": "deg_s", **window},
    }
    body.update(overrides)
    return body
