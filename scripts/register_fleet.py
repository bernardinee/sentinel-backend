"""Register the responder fleet roster (operator configuration, run once).

This is NOT sensor data and it is NOT part of the demonstration path — it is the
dispatcher's roster, the same kind of configuration as emergency contacts. The
stations below are real Greater Accra emergency facilities at their real
coordinates; call signs and crew sizes are placeholders for the operator to
adjust to the actual fleet.

    python scripts/register_fleet.py --backend http://localhost:8080 \
        --api-key change-me-sentinel-dev-key
"""
import argparse
import json
import sys
import urllib.error
import urllib.request

# (call_sign, type, station, lat, lon, crew)
FLEET = [
    # ── Ambulances: National Ambulance Service posts at major hospitals ──────
    ("AMB-01", "AMBULANCE", "Korle Bu Teaching Hospital",        5.5364, -0.2265, 3),
    ("AMB-02", "AMBULANCE", "37 Military Hospital",              5.5893, -0.1866, 3),
    ("AMB-03", "AMBULANCE", "Greater Accra Regional (Ridge)",    5.5645, -0.1969, 3),
    ("AMB-04", "AMBULANCE", "Legon Hospital",                    5.6510, -0.1870, 2),
    ("AMB-05", "AMBULANCE", "La General Hospital",               5.5570, -0.1650, 2),
    ("AMB-06", "AMBULANCE", "Achimota Hospital",                 5.6150, -0.2280, 2),

    # ── Ghana National Fire Service ─────────────────────────────────────────
    ("FIRE-01", "FIRE", "Accra Central Fire Station",            5.5480, -0.2050, 6),
    ("FIRE-02", "FIRE", "Airport Fire Station",                  5.6050, -0.1720, 6),
    ("FIRE-03", "FIRE", "Madina Fire Station",                   5.6836, -0.1660, 5),

    # ── Ghana Police Service ────────────────────────────────────────────────
    ("POL-01", "POLICE", "Accra Central Police Station",         5.5520, -0.1980, 4),
    ("POL-02", "POLICE", "Legon Police Station",                 5.6480, -0.1880, 4),
    ("POL-03", "POLICE", "Madina Police Station",                5.6800, -0.1670, 4),
    ("POL-04", "POLICE", "Dansoman Police Station",              5.5520, -0.2620, 4),

    # ── Heavy rescue / extrication ──────────────────────────────────────────
    ("RES-01", "RESCUE", "NADMO Accra Operations",               5.5700, -0.2020, 5),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--backend", default="http://localhost:8080")
    ap.add_argument("--api-key", default="change-me-sentinel-dev-key")
    args = ap.parse_args()

    created = skipped = failed = 0
    for call_sign, unit_type, station, lat, lon, crew in FLEET:
        body = json.dumps({
            "call_sign": call_sign, "unit_type": unit_type,
            "station_name": station, "home_lat": lat, "home_lon": lon,
            "crew_size": crew,
        }).encode()
        req = urllib.request.Request(
            f"{args.backend}/api/v1/units", data=body, method="POST",
            headers={"Content-Type": "application/json", "X-API-Key": args.api_key})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                if r.status == 201:
                    print(f"  registered {call_sign:8s} {unit_type:10s} {station}")
                    created += 1
        except urllib.error.HTTPError as e:
            if e.code == 409:
                print(f"  exists     {call_sign}")
                skipped += 1
            else:
                print(f"  FAILED     {call_sign}: {e.code} {e.read()[:120]}",
                      file=sys.stderr)
                failed += 1
        except Exception as exc:
            print(f"  FAILED     {call_sign}: {exc}", file=sys.stderr)
            failed += 1

    print(f"\n{created} registered, {skipped} already present, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
