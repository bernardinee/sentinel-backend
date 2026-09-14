"""Replay a RECORDED window through the full ingest path (development tool).

Usage:
    python scripts/replay.py samples/4_Real_crash_4_g___90_ms.json
    python scripts/replay.py <capture.json> --backend http://localhost:8080 \
        --api-key change-me-sentinel-dev-key --device ESP32_ACC_001 [--lat 5.6581 --lon -0.1812]

The input file must contain ax/ay/az/gx/gy/gz arrays of exactly 500 samples in
g / deg/s — i.e. a stored capture, exactly what the ESP32 would send.

NOTE (§8.1): this is a development/bench tool that replays stored captures.
It never fabricates sensor data, and it is not part of the demonstration path —
the acceptance test uses the live device.

The bundled samples/ windows are the project's Phase-2 validation vectors
(from the ML API's Postman suite), including the negative case
"2_Brief_12_g_spike__dropped_device_" that must classify as signature_override.
"""
import argparse
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("window_file", help="JSON file with ax..gz arrays (500 samples)")
    ap.add_argument("--backend", default="http://localhost:8080")
    ap.add_argument("--api-key", default="change-me-sentinel-dev-key")
    ap.add_argument("--device", default="ESP32_ACC_001")
    ap.add_argument("--lat", type=float, default=None)
    ap.add_argument("--lon", type=float, default=None)
    ap.add_argument("--event-id", default=None,
                    help="Reuse an event_id to demonstrate idempotency")
    args = ap.parse_args()

    data = json.loads(Path(args.window_file).read_text())
    for k in ("ax", "ay", "az", "gx", "gy", "gz"):
        if k not in data or len(data[k]) != 500:
            print(f"ERROR: {k} missing or not 500 samples", file=sys.stderr)
            return 2

    mags = [(x * x + y * y + z * z) ** 0.5
            for x, y, z in zip(data["ax"], data["ay"], data["az"])]
    peak = max(mags)

    event_id = args.event_id or f"replay-{uuid.uuid4().hex[:12]}"
    body = {
        "device_id": args.device,
        "event_id": event_id,
        "detected_at": datetime.now(timezone.utc).isoformat(),
        "trigger": {"peak_g": round(peak, 3), "jerk_gs": 0.0},
        "gps": ({"lat": args.lat, "lon": args.lon, "valid": True, "satellites": 7}
                if args.lat is not None else {"valid": False}),
        "device": {"firmware_version": "replay", "uptime_s": 0},
        "window": {"fs_hz": 100, "units_accel": "g", "units_gyro": "deg_s", **data},
    }

    t0 = time.perf_counter()
    resp = httpx.post(f"{args.backend}/api/v1/events", json=body,
                      headers={"X-API-Key": args.api_key}, timeout=30.0)
    dt = (time.perf_counter() - t0) * 1000
    print(f"HTTP {resp.status_code} in {dt:.0f} ms  (event_id={event_id})")
    print(json.dumps(resp.json(), indent=2))
    return 0 if resp.status_code == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
